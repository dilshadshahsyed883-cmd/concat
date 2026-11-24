#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate typed JSON adapters from Concat's authoritative public Slint interface.
No editing formulas or controller implementations are generated here.
"""
import argparse,json,re
from pathlib import Path
EXCLUDED={'Fmt','Curves','Payload','Focus','I18n','Theme','ParamFormat','GpuCompatibility','Panes'}
KEYWORDS={'type','ref','match','loop','move','self','Self','in','mod','use','where','fn','pub','crate','async','await','box','dyn','const','trait','return','impl'}
def ident(s):
    s=s.replace('-','_');return 'r#'+s if s in KEYWORDS else s
def variant(s):return ''.join(w[:1].upper()+w[1:] for w in re.split('[-_]',s))
def clean(s):
    # Preserve strings while stripping comments; public declarations contain no comment syntax inside strings.
    return re.sub(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*',lambda m:m[0] if m[0].startswith('"') else '',s,flags=re.S)
def declarations(text):
    pattern=re.compile(r'(?P<export>export\s+)?(?P<kind>struct|enum|global|component)\s+(?P<name>[\w-]+)(?:\s+inherits\s+\w+)?\s*\{')
    out=[]
    for m in pattern.finditer(text):
        depth=1;i=m.end();quoted=False;escaped=False
        while i<len(text) and depth:
            c=text[i]
            if quoted:
                if escaped:escaped=False
                elif c=='\\':escaped=True
                elif c=='"':quoted=False
            elif c=='"':quoted=True
            elif c=='{':depth+=1
            elif c=='}':depth-=1
            i+=1
        if depth:raise ValueError('Unclosed '+m['name'])
        out.append((m['kind'],m['name'],text[m.end():i-1],bool(m['export'])))
    return out

def parse(root):
    types={};targets={};locations={}
    app=(root/'app.slint').read_text(encoding='utf8');exported=set(re.findall(r'\b[\w-]+\b',app[app.index('export {'):app.index('export component')]))
    for path in sorted(root.rglob('*.slint')):
        for kind,name,body,export in declarations(clean(path.read_text(encoding='utf8'))):
            if kind in ['struct','enum']:
                if kind=='struct':definition={'kind':kind,'fields':dict(re.findall(r'([\w-]+)\s*:\s*(\[[\w-]+\]|[\w-]+)\s*,?',body))}
                else:definition={'kind':kind,'values':re.findall(r'[\w-]+',body)}
                if name in types and types[name]!=definition:continue # Private widget names are outside the public type closure.
                types[name]=definition;locations[name]=path.relative_to(root).as_posix()
            elif kind=='global' and export and name in exported and name not in EXCLUDED or kind=='component' and name=='App':
                props=re.findall(r'\b(?:in-out|in|out)\s+property\s*<\s*([^>]+)\s*>\s*([\w-]+)',body)
                props={field:typ.strip() for typ,field in props}
                callbacks=[]
                for callback,args,result in re.findall(r'\bcallback\s+([\w-]+)\s*\(([^)]*)\)\s*(?:->\s*([\w-]+))?\s*;',body,re.S):
                    arguments=[]
                    if args.strip():
                        for arg in args.split(','):
                            n,t=arg.split(':',1);arguments.append({'name':n.strip(),'type':t.strip()})
                    if any(a['type']=='data-transfer' for a in arguments):continue
                    callbacks.append({'name':callback,'arguments':arguments,'result':result or 'void'})
                targets[name]={'properties':props,'callbacks':callbacks}
    required=set()
    def visit(typ):
        if typ.startswith('['):return visit(typ[1:-1].strip())
        if typ in required or typ not in types:return
        required.add(typ)
        if types[typ]['kind']=='struct':
            for t in types[typ]['fields'].values():visit(t)
    for t in targets.values():
        for typ in t['properties'].values():visit(typ)
        for c in t['callbacks']:
            for a in c['arguments']:visit(a['type'])
            visit(c['result'])
    return {'version':1,'targets':targets,'types':{k:types[k] for k in sorted(required)},'locations':{k:locations[k] for k in sorted(required)}}

def rust_type(t):
    if t.startswith('['):return 'slint::ModelRc<'+rust_type(t[1:-1])+ '>'
    return {'int':'i32','float':'f32','length':'f32','physical-length':'f32','duration':'i64','angle':'f32','bool':'bool','string':'slint::SharedString','color':'slint::Color','brush':'slint::Brush','image':'slint::Image','void':'()'}.get(t,'crate::ui::'+t)

def generate(schema):
    lines=['// Generated from the public Slint interface. Do not edit.','use slint::{ComponentHandle,Global,Model};','use serde_json::{Value,json};','use super::{Wire,IMAGES};']
    for name,spec in schema['types'].items():
        ty='crate::ui::'+name
        if spec['kind']=='enum':
            to=','.join(f'{ty}::{variant(v)}=>json!({json.dumps(v)})' for v in spec['values'])
            fr=','.join(f'{json.dumps(v)}=>Ok({ty}::{variant(v)}),' + f'{json.dumps(str(i))}=>Ok({ty}::{variant(v)})' for i,v in enumerate(spec['values']))
            lines.append(f'impl Wire for {ty} {{fn wire(&self,_:&str)->Value{{match self{{{to},_=>Value::Null}}}} fn read(v:&Value)->Result<Self,String>{{let word=v.as_str().map(str::to_owned).unwrap_or_else(||v.to_string());match word.as_str(){{{fr},_=>Err(format!("Invalid {name}: {{word}}"))}}}}}}')
        else:
            to=','.join(f'{json.dumps(k)}:self.{ident(k)}.wire(&format!("{{}}.{k}",key))' for k in spec['fields'])
            fr=''.join(f'if let Some(x)=map.get({json.dumps(k)}){{out.{ident(k)}=<{rust_type(t)} as Wire>::read(x)?;}}' for k,t in spec['fields'].items())
            lines.append(f'impl Wire for {ty} {{fn wire(&self,key:&str)->Value{{json!({{{to}}})}}fn read(v:&Value)->Result<Self,String>{{let map=v.as_object().ok_or("{name} must be an object")?;let mut out=Self::default();{fr}Ok(out)}}}}')
    lines.append('pub fn state(app:&crate::ui::App)->Value {IMAGES.with(|images|images.borrow_mut().clear());let mut result=serde_json::Map::new();')
    for target,spec in schema['targets'].items():
        owner='app' if target=='App' else 'app.global::<crate::ui::'+target+'>()'
        fields=','.join(f'{json.dumps(k)}:{owner}.get_{ident(k)}().wire({json.dumps(target+"."+k)})' for k in spec['properties'])
        lines.append(f'result.insert({json.dumps(target)}.into(),json!({{{fields}}}));')
    lines.append('Value::Object(result)}')
    lines.append('pub fn invoke(app:&crate::ui::App,target:&str,callback:&str,args:&[Value],props:&std::collections::BTreeMap<String,Value>)->Result<Value,String>{match target{')
    for target,spec in schema['targets'].items():
        owner='app' if target=='App' else 'app.global::<crate::ui::'+target+'>()'
        lines.append(json.dumps(target)+'=>{for (name,value) in props {match name.as_str(){')
        for name,t in spec['properties'].items():lines.append(f'{json.dumps(name)}=>{owner}.set_{ident(name)}(<{rust_type(t)} as Wire>::read(value)?),')
        lines.append('_=>return Err(format!("Unknown property: {name}")),}}match callback{')
        lines.append('""=>Ok(Value::Null),')
        for c in spec['callbacks']:
            argv=','.join(f'<{rust_type(a["type"])} as Wire>::read(args.get({i}).ok_or("Missing argument {a["name"]}")?)?' for i,a in enumerate(c['arguments']))
            lines.append(f'{json.dumps(c["name"])}=>{{if args.len()!={len(c["arguments"])}{{return Err("Incorrect argument count".into());}}let value={owner}.invoke_{ident(c["name"])}({argv});Ok(value.wire("result"))}},')
        lines.append('_=>Err(format!("Unknown callback: {callback}")),}},')
    lines.append('_=>Err(format!("Unknown target: {target}")),}}')
    return '\n'.join(lines)+'\n'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--ui',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--schema',type=Path,required=True);args=parser.parse_args();schema=parse(args.ui);args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(generate(schema));args.schema.write_text(json.dumps(schema,indent=2));print(f"Generated {sum(len(t['callbacks']) for t in schema['targets'].values())} callback adapters and {len(schema['types'])} types")
if __name__=='__main__':main()
