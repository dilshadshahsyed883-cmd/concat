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
        self.window=None;self.lock=threading.RLock();self.engine=engine;self.engine_path=None;self.preset_path=Path(presets) if presets else None;self.native=None;self.path='';self.view=None;self.dirty=False;self.revision=0;self.jobs={};self.thumb_cache={};self.settings_path=Path(settings) if settings else ROOT/'preferences.json'
        try:self.prefs=json.loads(self.settings_path.read_text(encoding='utf8'))
        except (OSError,ValueError):self.prefs={}
        self.prefs.setdefault('favorites',{});self.prefs.setdefault('defaults',{});self.prefs.setdefault('lockedTracks',[])
    def _discover(self):
        base=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
        path=os.environ.get('CONCAT_NATIVE_EXE') or self.prefs.get('engine')
        if path:self.engine_path=Path(path).resolve()
        else:self.engine_path=next((p for p in (base/'native').rglob('concat.exe')),None)
        if not self.preset_path:self.preset_path=next((p for p in (base/'native').rglob('concat-ui-presets.exe')),None)
        if self.engine is None:
            if not self.engine_path:raise EngineError('Bundled Concat engine not found. Set its location in Settings.')
            self.engine=McpEngine(self.engine_path)
    def _call(self,name,args=None,raw=False):
        if self.native and self.native.poll() is None:raise EngineError('Project is open in the advanced editor. Save and close that window, then choose Return to workspace.')
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
    def _state(self):return {'path':self.path,'view':self.view,'dirty':self.dirty,'revision':self.revision,'preferences':self.prefs,'nativeOpen':bool(self.native and self.native.poll() is None)}
    def _refresh(self,changed=False):
        if self.path:self.view=self._call('project_get',{'path':self.path})
        if changed:self.dirty=True
        self.revision+=1;self.thumb_cache.clear();return self._state()
    def _edit(self,command):
        if not self.path:raise EngineError('Open a project first.')
        self._call('edit_apply',{'path':self.path,'command':command});return self._refresh(True)
    @rpc
    def boot(self):
        self._discover();return {**self._state(),'version':self._call('concat_version'),'recents':self._call('project_list'),'catalogue':self._call('effects_list'),'motion':self._helper({'operation':'catalogue'}),'tools':[{'name':v['name'],'title':v.get('title',v['name']),'description':v.get('description',''),'inputSchema':v.get('inputSchema',{})} for v in self.engine.tools.values()]}
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
        result=self._call('project_create',{'location':location,'name':name});self.path=str(Path(location)/name);self.dirty=False;return self._refresh()
    def _save_close(self):
        if self.path:
            self._call('project_close',{'path':self.path,'save':True});self.path='';self.view=None;self.dirty=False
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
    def effect(self,kind,package,params,ids,all_clips=False,duration=.9):
        timeline=self._active();locked=self.prefs.get('lockedTracks',[]);commands=[]
        for c in timeline['clips']:
            if c['trackId'] in locked or not(all_clips or c['id'] in ids):continue
            if kind=='transition':patch={'transitionIn':{'id':package,'duration':float(duration),'params':params} if package!='none' else None}
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
        self.prefs.update(changes);save_json(self.settings_path,self.prefs);return self._state()
    @rpc
    def native_state(self):
        if self.engine is None:self._discover()
        value=self.engine.request('desktop/state',{})
        self.path=value.get('path') or '';self.view=value.get('view')
        return {**value,'state':self._state()}
    @rpc
    def native_schema(self):
        if self.engine is None:self._discover()
        return self.engine.request('desktop/schema',{})
    @rpc
    def native_invoke(self,target,callback,arguments=None,properties=None):
        if self.engine is None:self._discover()
        self.engine.request('desktop/invoke',{'target':target,'callback':callback,'arguments':arguments or [],'properties':properties or {}})
        self.dirty=True;self.revision+=1;self.thumb_cache.clear()
        return self.native_state()['data']
    @rpc
    def select_native(self,ids):
        # Use the original timeline selection controller before opening an inspector/service.
        for i,ident in enumerate(ids):
            self.engine.request('desktop/invoke',{'target':'Editor','callback':'clip-pressed','arguments':[ident,i>0,0]})
        return True
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
            if self.native and self.native.poll() is None:return
            if self.path and self.engine:self._save_close()
            if self.engine:self.engine.close();self.engine=None
