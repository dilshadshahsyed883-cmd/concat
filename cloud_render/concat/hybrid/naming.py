"""Portable friendly filenames, separate from security-sensitive job paths."""
from __future__ import annotations
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote

MAX_BASENAME_UTF16=120
_INVALID=set('<>:"/\\|?*')
_RESERVED=re.compile(r'^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$',re.I)
def _limit(value,maximum=MAX_BASENAME_UTF16):
    out=[];used=0
    for c in value:
        units=2 if ord(c)>0xffff else 1
        if used+units>maximum:break
        out.append(c);used+=units
    return ''.join(out)
def basename(name):
    value=''.join('_' if c in _INVALID or unicodedata.category(c) in ('Cc','Cs') else c
                  for c in str(name or '').strip()).rstrip('. ')
    value=_limit(value).rstrip('. ') or 'Untitled'
    if _RESERVED.fullmatch(value.split('.',1)[0]):value='_'+_limit(value,MAX_BASENAME_UTF16-1)
    return value
def audio_stem(path):
    name=re.split(r'[/\\]',str(path))[-1];dot=name.rfind('.')
    return basename(name[:dot] if dot>0 else name)
def _filename(name,extension):
    value=str(name or '').strip()
    if value.lower().endswith(extension):value=value[:-len(extension)]
    return basename(value)+extension
def zip_name(name):return _filename(name,'.zip')
def mp4_name(name):return _filename(name,'.mp4')
def numbered(base,number):
    safe=basename(base)
    if number<=1:return safe
    suffix=f' ({number})'
    return _limit(safe,MAX_BASENAME_UTF16-len(suffix)).rstrip('. ')+suffix

def project_names(document,fallback='Untitled'):
    """Preserve the canonical display name; sanitize only filenames/labels.

    An explicit outputBasename override wins. Absent that, renaming the saved
    project changes subsequent output suggestions without a separate migration.
    """
    document=document if isinstance(document,dict) else {}
    name=document.get('name')
    if not isinstance(name,str) or not name.strip():
        timelines=document.get('timelines') or []
        selected=next((t for t in timelines if isinstance(t,dict) and t.get('id')==document.get('activeTimelineId')),None)
        if selected is None:selected=next((t for t in timelines if isinstance(t,dict)),{})
        name=selected.get('name') or fallback
    name=name if isinstance(name,str) and name.strip() else fallback
    override=document.get('outputBasename')
    chosen=override if isinstance(override,str) and override.strip() else name
    base=basename(chosen)
    return {'project_name':name,'display_name':basename(name),'output_basename':base,
            'bundle_filename':zip_name(base),'output_filename':mp4_name(base)}

def read_project_names(root,fallback='Untitled'):
    import json
    root=Path(root)
    manifest=next((root/name for name in ('concat.json','wolfcut.json') if (root/name).is_file()),None)
    if manifest is None:return project_names({},fallback)
    return project_names(json.loads(manifest.read_text(encoding='utf-8-sig')),fallback)

def content_disposition(filename):
    """RFC 6266/5987: no raw Unicode or newline enters an HTTP header."""
    safe=mp4_name(filename)
    return "attachment; filename=\"output.mp4\"; filename*=UTF-8''"+quote(safe,safe='')
