"""Real Windows integration gate. Uses compiled engine, never mocked rendering."""
import argparse,json,sys,tempfile,subprocess,time,traceback,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from host.transport import McpEngine
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--engine',type=Path,required=True);parser.add_argument('--presets',type=Path,required=True);parser.add_argument('--report',type=Path,required=True);a=parser.parse_args();checks=[];engine=None
    def check(name,fn):
        value=fn();checks.append({'name':name,'status':'passed'});print('PASS',name,flush=True);return value
    def helper(value):
        p=subprocess.run([str(a.presets.resolve())],input=json.dumps(value),text=True,encoding='utf8',capture_output=True,check=True,timeout=30);r=json.loads(p.stdout);assert r['ok'],r;return r['data']
    folder=tempfile.mkdtemp(prefix='concat-react-smoke-')
    try:
        if True:
            from PIL import Image,ImageDraw
            pic=Path(folder)/'source.png';im=Image.new('RGB',(640,360),'#eaf2ff');d=ImageDraw.Draw(im);d.rectangle((80,60,560,300),fill='#3979ef');d.text((160,160),'Concat native rendering',fill='white');im.save(pic)
            engine=check('native bridge initialize',lambda:McpEngine(a.engine.resolve(),timeout=180))
            schema=check('native service schema',lambda:engine.request('desktop/schema',{}));assert sum(len(t['callbacks']) for t in schema['targets'].values())>=350
            for target,name,args in [('App','open-settings',[]),('App','settings-closed',[])]:
                check(target+'.'+name,lambda:engine.request('desktop/invoke',{'target':target,'callback':name,'arguments':args}))
            original=engine.request('desktop/animation-default',{'id':'motion.push-in'})
            changed={**original['physics'],'duration':.4}
            engine.request('desktop/animation-default',{'id':'motion.push-in','controls':changed})
            assert engine.request('desktop/animation-default',{'id':'motion.push-in'})['physics']['duration']==.4
            engine.request('desktop/animation-default',{'id':'motion.push-in','controls':original['physics']})
            checks.append({'name':'native preset default save and restore','status':'passed'})
            favorites=engine.request('desktop/state',{})['favorites']['animation']
            engine.request('desktop/invoke',{'target':'Keyframes','callback':'toggle-physics-favourite','arguments':[6]})
            assert ('motion.push-in' in engine.request('desktop/state',{})['favorites']['animation']) != ('motion.push-in' in favorites)
            engine.request('desktop/invoke',{'target':'Keyframes','callback':'toggle-physics-favourite','arguments':[6]})
            checks.append({'name':'native favorite persistence controller','status':'passed'})
            engine.call('project_create',{'location':folder,'name':'React integration'})
            state=engine.request('desktop/state',{});path=state['path'];assert state['view']['project']['timelines']
            engine.call('media_import',{'path':path,'file':str(pic)})
            view=engine.call('project_get',{'path':path});mid=view['project']['media'][0]['id']
            engine.call('edit_apply',{'path':path,'command':{'op':'addClipsInSequence','mediaIds':[mid],'start':0,'trackId':None,'ripple':False}})
            def current():
                v=engine.call('project_get',{'path':path});p=v['project'];t=next(t for t in p['timelines'] if t['id']==p['activeTimelineId']);return v,t['clips'][0]
            engine.request('desktop/invoke',{'target':'Editor','callback':'stage-pressed','arguments':[.5,.5,False]})
            engine.request('desktop/invoke',{'target':'Editor','callback':'stage-dragged','arguments':[.6,.5,False]})
            engine.call('project_get',{'path':path})
            engine.request('desktop/invoke',{'target':'Editor','callback':'stage-released','arguments':[]})
            assert current()[1].get('offsetX',0)>.05,'A read during a stage gesture must not discard its native preview state'
            engine.call('edit_undo',{'path':path})
            checks.append({'name':'stage gesture survives concurrent project reads and is undoable','status':'passed'})
            assert abs(engine.request('desktop/zoom',{'pixels':75,'anchor':0})-75)<.01
            _,initial=current()
            engine.request('desktop/timeline-drop',{'id':initial['id'],'start':2.5,'track':initial['trackId'],'copy':False})
            assert abs(current()[1]['start']-2.5)<1/30,'Timeline drops must use the native drag planner'
            engine.call('edit_undo',{'path':path})
            checks.append({'name':'native timeline drop and zoom routing','status':'passed'})
            check('open native captions on live project',lambda:engine.request('desktop/invoke',{'target':'Editor','callback':'captions','arguments':[]}));engine.request('desktop/invoke',{'target':'App','callback':'captions-closed','arguments':[]})
            view,clip=current();frame=view['settings']['rateDen']/view['settings']['rateNum'];catalogue=helper({'operation':'catalogue'})
            for preset in catalogue['presets']:
                if preset['category']=='adjustment':continue
                view,clip=current();command=helper({'clip':clip,'frame':frame,'preset':preset['id'],'controls':{}})
                check('native preset '+preset['id'],lambda:engine.call('edit_apply',{'path':path,'command':command}))
            picture=check('real rendered PNG preview',lambda:engine.call('preview_frame',{'path':path,'time':.25,'width':320,'height':180},raw=True))
            import base64,io
            pixels=Image.open(io.BytesIO(base64.b64decode(next(b['data'] for b in picture['content'] if b['type']=='image'))));assert pixels.size==(320,180)
            check('undo',lambda:engine.call('edit_undo',{'path':path}));check('redo',lambda:engine.call('edit_redo',{'path':path}))
            check('save',lambda:engine.call('project_save',{'path':path}));before=current()[0]['project'];engine.call('project_close',{'path':path,'save':True});engine.call('project_open',{'path':path});assert current()[0]['project']==before;checks.append({'name':'save close reopen preserves project','status':'passed'})
            output=Path(folder)/'render.mp4';job=engine.call('export_start',{'path':path,'output':str(output),'width':320,'height':180})
            deadline=time.monotonic()+180
            while True:
                status=engine.call('export_status',{'job':job['job']})
                if status['state']!='running':break
                if time.monotonic()>deadline:raise TimeoutError('Real render timed out')
                time.sleep(.25)
            assert status['state']=='done',status;assert output.stat().st_size>100;checks.append({'name':'real local MP4 export','status':'passed'});a.report.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(output,a.report.parent/'native-render-sample.mp4')
            cancel=engine.call('export_start',{'path':path,'output':str(Path(folder)/'cancelled.mp4'),'width':1920,'height':1080});engine.call('export_cancel',{'job':cancel['job']})
            deadline=time.monotonic()+40
            while True:
                status=engine.call('export_status',{'job':cancel['job']})
                if status['state']!='running':break
                assert time.monotonic()<deadline,'Cancellation timed out';time.sleep(.1)
            assert status['state']=='cancelled',status;checks.append({'name':'real render cancellation','status':'passed'})
    except Exception as e:
        checks.append({'name':'integration','status':'failed','error':str(e)});traceback.print_exc();raise
    finally:
        if engine:
            for line in engine.logs:print(line,file=sys.stderr)
            engine.close()
        shutil.rmtree(folder,ignore_errors=True)
        a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps({'real_tests':checks,'gpu_execution':'unverified without CUDA device','webview_ui':'separate smoke gate'},indent=2))
if __name__=='__main__':main()
