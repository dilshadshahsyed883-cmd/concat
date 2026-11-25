"""React workbench over the existing Concat engine and native preset generator."""
import base64,collections,functools,json,os,subprocess,sys,threading
from pathlib import Path
from types import SimpleNamespace
from .transport import McpEngine,EngineError
ROOT=Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local/share'))/'Concat'/'ReactUI'

def rpc(fn):
    @functools.wraps(fn)
    def run(self,*a,**kw):
        try:
            with self.lock:return {'ok':True,'data':fn(self,*a,**kw)}
        except Exception as e:return {'ok':False,'error':str(e)}
    run.desktop_rpc=True;return run

def save_json(path,obj):
    import tempfile
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:json.dump(obj,f,ensure_ascii=False,indent=2,allow_nan=False)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def public_api(api):return SimpleNamespace(**{n:getattr(api,n) for n in dir(api) if not n.startswith('_') and getattr(getattr(api,n),'desktop_rpc',False)})

class Api:
    def __init__(self,engine=None,presets=None,settings=None):
        self.window=None;self.lock=threading.RLock();self.engine=engine;self.engine_path=None;self.preset_path=Path(presets) if presets else None;self.path='';self.view=None;self.dirty=False;self.revision=0;self.jobs={};self.thumb_cache={};self.settings_path=Path(settings) if settings else ROOT/'preferences.json'
        try:self.prefs=json.loads(self.settings_path.read_text(encoding='utf8'))
        except (OSError,ValueError):self.prefs={}
        self.prefs.setdefault('favorites',{});self.prefs.setdefault('defaults',{});self.prefs.setdefault('lockedTracks',[]);self.prefs.setdefault('lockedProjects',{})
    def _discover(self):
        base=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))
        path=os.environ.get('CONCAT_NATIVE_EXE') or self.prefs.get('engine')
        if path:self.engine_path=Path(path).resolve()
        else:self.engine_path=next((p for p in (base/'native').rglob('concat.exe')),None)
        if not self.preset_path:self.preset_path=next((p for p in (base/'native').rglob('concat-ui-presets.exe')),None)
        if self.engine is None:
            if not self.engine_path:raise EngineError('Bundled Concat engine not found. Set its location in Settings.')
            self.engine=McpEngine(self.engine_path)
    def _call(self,name,args=None,raw=False):
        if self.engine is None:self._discover()
        return self.engine.call(name,args or {},raw=raw)
    def _helper(self,obj):
        if not self.preset_path or not self.preset_path.exists():raise EngineError('Native preset adapter is missing from this build.')
        result=subprocess.run([str(self.preset_path)],input=json.dumps(obj,allow_nan=False),capture_output=True,text=True,encoding='utf8',timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise EngineError(result.stderr[-1200:] or 'Native preset adapter failed.')
        value=json.loads(result.stdout)
        if not value.get('ok'):raise EngineError(value.get('error','Preset refused.'))
        return value['data']
    def _dialog(self,save=False,folder=False,multiple=False,types=(),filename=''):
        import webview
        if not self.window:raise EngineError('File chooser requires desktop mode.')
        kind=webview.SAVE_DIALOG if save else webview.FOLDER_DIALOG if folder else webview.OPEN_DIALOG
        value=self.window.create_file_dialog(kind,allow_multiple=multiple,file_types=types,save_filename=filename)
        return list(value) if isinstance(value,(tuple,list)) else [value] if value else []
    def _active(self):
        if not self.view:raise EngineError('Open a project first.')
        p=self.view['project'];return next((x for x in p['timelines'] if x['id']==p['activeTimelineId']),p['timelines'][0])
    def _state(self):return {'path':self.path,'view':self.view,'dirty':self.dirty,'revision':self.revision,'preferences':self.prefs,'appearance':getattr(self,'appearance',{}),'transitionDefaults':getattr(self,'native_transition',{})}
    def _refresh(self,changed=False):
        if self.path:self.view=self._call('project_get',{'path':self.path})
        self._lock_scope()
        if changed:self.dirty=True
        self.revision+=1;self.thumb_cache.clear();return self._state()
    def _lock_scope(self):
        key=self.path+'#'+(self.view['project']['activeTimelineId'] if self.view else '')
        if key==getattr(self,'_locks_key',None):return
        self._locks_key=key;self.prefs['lockedTracks']=self.prefs['lockedProjects'].get(key,[])
        if self.prefs['lockedTracks'] and self.view and self.engine:
            tracks=self._active()['tracks'];locked=set(self.prefs['lockedTracks'])
            for i,t in enumerate(tracks):
                self.engine.request('desktop/invoke',{'target':'Editor','callback':'track-flag-changed','arguments':[len(tracks)-1-i,t['visible'],t['muted'],t['id'] in locked]})
    def _edit(self,command):
        if not self.path:raise EngineError('Open a project first.')
        self._check_locks(command)
        self._call('edit_apply',{'path':self.path,'command':command});return self._refresh(True)
    def _check_locks(self,command):
        locked=set(self.prefs.get('lockedTracks',[]))
        if not locked:return
        clips={c['id']:c for c in self._active()['clips']}
        def visit(value):
            if isinstance(value,list):
                for v in value:visit(v)
            elif isinstance(value,dict):
                if value.get('op')=='setTrackFlag':return
                if value.get('trackId') in locked:raise ValueError('This track is locked.')
                ids=[value.get('clipId')]+value.get('clipIds',[])+value.get('targets',[])
                if any(clips.get(i,{}).get('trackId') in locked for i in ids):raise ValueError('The edit includes a locked clip.')
                if value.get('ripple'):raise ValueError('Unlock tracks before applying a ripple edit.')
                if value.get('op') in ['removeTimeline','clearTimeline']:raise ValueError('Unlock tracks before removing timeline content.')
                for v in value.values():visit(v)
        visit(command)
    @rpc
    def boot(self):
        self._discover();self.motion=self._helper({'operation':'catalogue'});self.catalogue=self._call('effects_list');self._native_snapshot()
        return {**self._state(),'version':self._call('concat_version'),'recents':self._call('project_list'),'catalogue':self.catalogue,'motion':self.motion,'tools':[{'name':v['name'],'title':v.get('title',v['name']),'description':v.get('description',''),'inputSchema':v.get('inputSchema',{})} for v in self.engine.tools.values()]}
    @rpc
    def state(self):return self._state()
    @rpc
    def open_project(self,path=None):
        if not path:
            paths=self._dialog(folder=True)
            if not paths:return None
            path=paths[0]
        if self.path and self.path!=path:self._save_close()
        self._call('project_open',{'path':str(Path(path).resolve())});self.path=str(Path(path).resolve());self.dirty=False;return self._refresh()
    @rpc
    def new_project(self,name,location=None):
        if not name.strip():raise ValueError('Project name is required.')
        if not location:
            paths=self._dialog(folder=True)
            if not paths:return None
            location=paths[0]
        if self.path:self._save_close()
        self._call('project_create',{'location':location,'name':name});native=self.engine.request('desktop/state',{});self.path=native['path'];self.view=native['view'];self.dirty=False;return self._refresh()
    def _save_close(self):
        if self.path:
            self._call('project_close',{'path':self.path,'save':True});self.path='';self.view=None;self.dirty=False
    @rpc
    def close_project(self):
        self._save_close();return self._state()
    @rpc
    def save_project(self):
        if not self.path:raise EngineError('No open project.')
        self._call('project_save',{'path':self.path});self.dirty=False;return self._refresh()
    @rpc
    def import_media(self):
        paths=self._dialog(multiple=True,types=('Media files (*.mp4;*.mov;*.mkv;*.webm;*.png;*.jpg;*.jpeg;*.webp;*.wav;*.mp3;*.flac)',))
        for p in paths:self._call('media_import',{'path':self.path,'file':p})
        return self._refresh(bool(paths))
    @rpc
    def add_media(self,ids,start,track=None):return self._edit({'op':'addClipsInSequence','mediaIds':ids,'start':start,'trackId':track,'ripple':False})
    @rpc
    def edit(self,command):return self._edit(command)
    @rpc
    def history(self,redo=False):self._call('edit_redo' if redo else 'edit_undo',{'path':self.path});return self._refresh(True)
    @rpc
    def preset(self,category,preset,ids,controls=None,all_clips=False):
        timeline=self._active();locked=self.prefs.get('lockedTracks',[]);clips=[c for c in timeline['clips'] if c['trackId'] not in locked and (all_clips or c['id'] in ids) and c['kind'] in ['image','video']]
        if not clips:raise ValueError('Select an unlocked image or video clip.')
        commands=[];video=timeline.get('video',self.view['settings']);frame=video.get('rateDen',1)/video.get('rateNum',30)
        for c in clips:
            if preset=='none' and category=='adjustment':commands.append({'op':'clearCanvasPresets','targets':[c['id']],'lockedTracks':locked})
            else:commands.append(self._helper({'clip':c,'frame':frame,'category':category,'preset':preset,'controls':controls or {}}))
        return self._edit({'op':'batch','commands':commands})
    @rpc
    def load_preset_controls(self,preset):
        p=next((p for p in self.motion['presets'] if p['id']==preset),None)
        if not p or p['category']=='adjustment':return {}
        settings=self.engine.request('desktop/animation-default',{'id':preset})
        return settings['canvas' if p['category']=='canvas' else 'physics']
    @rpc
    def reset_preset_default(self,category,preset):
        if category in ['basic','physics','canvas'] and preset!='none':
            settings=self.engine.request('desktop/animation-default',{'id':preset,'reset':True})
            controls=settings['canvas' if category=='canvas' else 'physics']
        else:controls=getattr(self,'motion',{}).get('defaults',{}).get(category,{})
        if category=='transition':self.engine.request('desktop/invoke',{'target':'Editor','callback':'transition-default-reset','arguments':[]})
        self.prefs['defaults'].pop(category,None);save_json(self.settings_path,self.prefs)
        return {'controls':controls,'state':self._native_snapshot()['state']}
    @rpc
    def effect(self,kind,package,params,ids,all_clips=False,duration=.9,intensity=.7):
        timeline=self._active();locked=self.prefs.get('lockedTracks',[]);commands=[]
        for c in timeline['clips']:
            if c['trackId'] in locked or not(all_clips or c['id'] in ids):continue
            if kind in ['transition','effect'] and c['kind'] not in ['image','video','text','shape']:continue
            if kind=='audio' and c['kind'] not in ['audio','video']:continue
            if kind=='transition':patch={'transitionIn':{'id':package,'duration':float(duration),'intensity':float(intensity),'params':params} if package!='none' else None}
            else:
                field='filters' if kind=='audio' else 'videoEffects';old=list(c.get(field,[]));old=[e for e in old if e['id']!=package]
                patch={field:[] if package=='none' else old+[{'id':package,'params':params,'enabled':True}]}
            commands.append({'op':'updateClip','clipId':c['id'],'patch':patch})
        if not commands:raise ValueError('Select an unlocked clip.')
        return self._edit({'op':'batch','commands':commands})
    @rpc
    def frame(self,time,width=960):
        v=self._active().get('video',self.view['settings']);height=max(1,round(width*v['height']/v['width']))
        result=self._call('preview_frame',{'path':self.path,'time':float(time),'width':width,'height':height},raw=True)
        block=next((x for x in result.get('content',[]) if x.get('type')=='image'),None)
        if not block:raise EngineError('Engine returned no preview frame.')
        return 'data:'+block['mimeType']+';base64,'+block['data']
    @rpc
    def thumbnail(self,clip_id):
        c=next(c for c in self._active()['clips'] if c['id']==clip_id);key=(self.revision,clip_id)
        if key not in self.thumb_cache:
            result=self.frame(c['start']+min(.2,c['duration']/2),240)
            if not result['ok']:raise EngineError(result['error'])
            self.thumb_cache[key]=result['data']
        return self.thumb_cache[key]
    @rpc
    def export(self,preview=False):
        if preview:
            import tempfile
            folder=ROOT/'previews';folder.mkdir(parents=True,exist_ok=True);fd,name=tempfile.mkstemp(dir=folder,suffix='.mp4');os.close(fd);Path(name).unlink();output=name
        else:
            paths=self._dialog(save=True,types=('MP4 video (*.mp4)',),filename='Concat-export.mp4')
            if not paths:return None
            output=paths[0]
            if Path(output).exists():raise ValueError('Choose a new output filename.')
        args={'path':self.path,'output':output}
        if preview:
            v=self._active().get('video',self.view['settings']);args.update(width=640,height=max(2,round(640*v['height']/v['width']/2)*2),preset='ultrafast',crf=28)
        result=self._call('export_start',args);self.jobs[result['job']]={'preview':preview,'output':output};return result
    @rpc
    def job_status(self,job):
        result=self._call('export_status',{'job':job})
        if result['state']=='done' and self.jobs.get(job,{}).get('preview'):
            p=Path(self.jobs[job]['output'])
            if p.stat().st_size>128*1024*1024:raise ValueError('Playback proxy exceeds the preview limit; export the project to play it locally.')
            result['video']='data:video/mp4;base64,'+base64.b64encode(p.read_bytes()).decode()
        return result
    @rpc
    def cancel_export(self,job):return self._call('export_cancel',{'job':job})
    @rpc
    def preferences(self,changes):
        allowed={'favorites','defaults','lockedTracks'}
        if set(changes)-allowed:raise ValueError('Unknown preference.')
        if 'defaults' in changes:
            presets={p['id']:p for p in getattr(self,'motion',{}).get('presets',[])}
            for category,value in changes['defaults'].items():
                before=self.prefs['defaults'].get(category)
                if value==before:continue
                if category=='transition':
                    callback='transition-default-save' if value else 'transition-default-reset';args=[value.get('duration',.9),value.get('intensity',.7)] if value else []
                    self.engine.request('desktop/invoke',{'target':'Editor','callback':callback,'arguments':args})
                ident=(value or before or {}).get('preset')
                if ident in presets and category in ['basic','physics','canvas']:
                    args={'id':ident,'controls':value['controls']} if value else {'id':ident,'reset':True}
                    self.engine.request('desktop/animation-default',args)
        if 'favorites' in changes:
            if not isinstance(changes['favorites'],dict):raise ValueError('Favorites must be grouped by category.')
            presets={p['id']:p for p in getattr(self,'motion',{}).get('presets',[])}
            for category,ids in changes['favorites'].items():
                if not isinstance(ids,list) or not all(isinstance(i,str) for i in ids):raise ValueError('Favorites must be preset IDs.')
                old=set(self.prefs['favorites'].get(category,[]));new=set(ids)
                for ident in sorted(old^new):
                    if ident in presets:
                        p=presets[ident];target='CanvasPresets' if category=='adjustment' else 'Keyframes';callback='toggle-favourite' if category=='adjustment' else 'toggle-physics-favourite';args=[p['nativeIndex']]
                    else:target='Library';callback='favourite';args=[ident,ident in new]
                    self.engine.request('desktop/invoke',{'target':target,'callback':callback,'arguments':args})
        if 'lockedTracks' in changes and (not isinstance(changes['lockedTracks'],list) or not all(isinstance(i,str) for i in changes['lockedTracks'])):raise ValueError('Track locks must be a list of track IDs.')
        if 'lockedTracks' in changes and self.view:
            tracks=self._active()['tracks'];locked=set(changes['lockedTracks'])
            for i,t in enumerate(tracks):
                self.engine.request('desktop/invoke',{'target':'Editor','callback':'track-flag-changed','arguments':[len(tracks)-1-i,t['visible'],t['muted'],t['id'] in locked]})
        if 'lockedTracks' in changes:self.prefs['lockedProjects'][getattr(self,'_locks_key',self.path)]=changes['lockedTracks']
        self.prefs.update(changes);save_json(self.settings_path,self.prefs);return self._state()
    @rpc
    def native_state(self):return self._native_snapshot()
    def _native_snapshot(self):
        if self.engine is None:self._discover()
        value=self.engine.request('desktop/state',{})
        self.path=value.get('path') or '';self.view=value.get('view');self._lock_scope()
        self.appearance=value.get('appearance',{})
        editor=value.get('services',{}).get('Editor',{})
        self.native_transition={'duration':editor.get('transition-draft-duration',.9),'intensity':editor.get('transition-draft-intensity',.7)}
        favorites=value.get('favorites',{})
        if hasattr(self,'motion'):
            for category in ['basic','physics','canvas','adjustment']:
                saved=set(favorites.get('canvas' if category=='adjustment' else 'animation',[]))
                self.prefs['favorites'][category]=[p['id'] for p in self.motion['presets'] if p['category']==category and (('canvas:'+p['id'].split('.')[-1]) if category=='adjustment' else p['id']) in saved]
        if hasattr(self,'catalogue'):
            saved=set(favorites.get('packages',[]))
            for category in ['effect','transition','audio']:
                self.prefs['favorites'][category]=[p['id'] for p in self.catalogue if (p['kind']==category or category=='effect' and p['kind']=='filter') and p['id'] in saved]
        return {**value,'state':self._state()}
    @rpc
    def poll(self):
        events=self.engine.request('desktop/events',{})
        self.dirty=events.get('dirty',self.dirty)
        key=(events.get('path'),events.get('revision'),events.get('dirty'))
        if key!=getattr(self,'_native_revision',None):
            self._native_revision=key
            self.revision+=1;self.thumb_cache.clear()
            events['state']=self._native_snapshot()['state']
        return events
    @rpc
    def native_schema(self):
        if self.engine is None:self._discover()
        return self.engine.request('desktop/schema',{})
    @rpc
    def native_image(self,key):return self.engine.request('desktop/image',{'key':key})
    @rpc
    def quit(self):
        self._shutdown()
        if self.window:self.window.destroy()
        return True
    @rpc
    def key(self,event):
        if set(event)-{'key','shift','alt','ctrl','meta'}:raise ValueError('Invalid key event.')
        return self.engine.request('desktop/key',event)
    @rpc
    def stage(self,action,arguments):
        if action not in ['stage-pressed','stage-grip-pressed','stage-dragged','stage-released']:raise ValueError('Invalid stage gesture.')
        return self.engine.request('desktop/invoke',{'target':'Editor','callback':action,'arguments':arguments})
    @rpc
    def playback_toggle(self):
        self.engine.request('desktop/invoke',{'target':'Editor','callback':'play-toggled','arguments':[]})
        return self.engine.request('desktop/playback',{})
    @rpc
    def playback_status(self):return self.engine.request('desktop/playback',{})
    @rpc
    def seek(self,time):
        self.engine.request('desktop/invoke',{'target':'Editor','callback':'seek','arguments':[float(time)]})
        return True
    @rpc
    def native_invoke(self,target,callback,arguments=None,properties=None):
        if self.engine is None:self._discover()
        reply=self.engine.request('desktop/invoke',{'target':target,'callback':callback,'arguments':arguments or [],'properties':properties or {}})
        self.dirty=True;self.revision+=1;self.thumb_cache.clear()
        return {**self._native_snapshot(),'result':reply.get('value')}
    @rpc
    def select_native(self,ids):
        # Use the original timeline selection controller before opening an inspector/service.
        if not ids:self.engine.request('desktop/invoke',{'target':'Editor','callback':'shortcut','arguments':['deselect']})
        for i,ident in enumerate(ids):
            self.engine.request('desktop/invoke',{'target':'Editor','callback':'clip-pressed','arguments':[ident,i>0,0]})
            self.engine.request('desktop/invoke',{'target':'Editor','callback':'clip-released','arguments':[]})
        return True
    @rpc
    def queue_add_files(self):
        if self.engine is None:self._discover()
        paths=self._dialog(multiple=True,types=('GPU project ZIP (*.zip)',))
        for path in paths:
            self.engine.request('desktop/invoke',{'target':'CloudGpu','callback':'queue-add','arguments':[path]})
        return self._native_snapshot()
    @rpc
    def choose_engine(self):
        if self.path:raise ValueError('Close the current project before changing its engine.')
        paths=self._dialog(types=('Concat engine (*.exe)',))
        if paths:
            if self.engine:self.engine.close();self.engine=None
            self.prefs['engine']=paths[0];save_json(self.settings_path,self.prefs)
        return True
    @rpc
    def run_tool(self,name,arguments):
        # Complete native schema remains available for expert integrations; regular UI uses typed actions.
        result=self._call(name,arguments)
        if self.path:self._refresh(name not in ['project_get','timeline_read','effects_list','concat_version','export_status'])
        return {'result':result,'state':self._state()}
    def shutdown(self):
        try:
            return self._shutdown()
        except Exception as e:
            if self.window:
                import webview
                self.window.evaluate_js('window.dispatchEvent(new CustomEvent("concat-close-error",{detail:'+json.dumps(str(e))+'}))')
            return False
    def _shutdown(self):
        with self.lock:
            if self.path and self.engine:self._save_close()
            if self.engine:self.engine.close();self.engine=None
