"""Simulated engine responses: host routing/safety only, never rendering evidence."""
import copy,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from host.api import Api
from host.transport import EngineError

VIEW={'settings':{'name':'Fixture','width':1920,'height':1080,'rateNum':30,'rateDen':1},'project':{'activeTimelineId':'tl1','timelines':[{'id':'tl1','tracks':[{'id':'t1','visible':True,'muted':False},{'id':'t2','visible':True,'muted':False}],'clips':[{'id':'c1','trackId':'t1','kind':'image','start':0,'duration':5},{'id':'c2','trackId':'t2','kind':'image','start':0,'duration':5}]}],'media':[],'futureVendorField':{'keep':'exactly'}},'canUndo':True,'canRedo':False}
class Engine:
    def __init__(self):self.calls=[];self.fail='';self.revision=1;self.tools={}
    def request(self,method,params):
        self.calls.append((method,copy.deepcopy(params)))
        if self.fail:raise EngineError(self.fail)
        if method=='desktop/state':return {'path':'fixture','view':copy.deepcopy(VIEW),'services':{},'appearance':{'dark':False}}
        if method=='desktop/events':return {'path':'fixture','revision':self.revision,'dirty':False}
        return {'value':42}
    def call(self,name,args=None,raw=False):
        self.calls.append((name,copy.deepcopy(args)))
        if self.fail:raise EngineError(self.fail)
        return copy.deepcopy(VIEW) if name=='project_get' else {}
    def close(self):self.calls.append(('close',{}))

def fixture(tmp_path):
    engine=Engine();api=Api(engine=engine,settings=tmp_path/'preferences.json');api.path='fixture';api.view=copy.deepcopy(VIEW);api.prefs['lockedTracks']=['t1'];api._locks_key='fixture#tl1';return api,engine

def test_nested_batch_refuses_locked_clip_before_any_engine_mutation(tmp_path):
    api,engine=fixture(tmp_path)
    result=api.edit({'op':'batch','commands':[{'op':'updateClip','clipId':'c2','patch':{'scale':2}},{'op':'removeClips','clipIds':['c1']}]})
    assert not result['ok'] and 'locked' in result['error'];assert engine.calls==[]

def test_track_visibility_remains_available_when_track_is_locked(tmp_path):
    api,engine=fixture(tmp_path)
    result=api.edit({'op':'setTrackFlag','trackId':'t1','flag':'visible','value':False})
    assert result['ok'];assert engine.calls[0][0]=='edit_apply'

def test_failed_save_close_keeps_project_and_refuses_window_shutdown(tmp_path):
    api,engine=fixture(tmp_path);engine.fail='Disk is full'
    result=api.close_project();assert result=={'ok':False,'error':'Disk is full'}
    assert api.path=='fixture' and api.view==VIEW;assert api.shutdown() is False

def test_native_errors_are_not_replaced_with_missing_envelope_errors(tmp_path):
    api,engine=fixture(tmp_path);engine.fail='Renderer unavailable'
    assert api.native_invoke('Editor','captions')['error']=='Renderer unavailable'

def test_native_selection_uses_integer_edge_and_preserves_additive_selection(tmp_path):
    api,engine=fixture(tmp_path);assert api.select_native(['c1','c2'])['ok']
    assert engine.calls[0][1]['arguments']==['c1',False,0]
    assert engine.calls[1][1]['callback']=='clip-released'
    assert engine.calls[2][1]['arguments']==['c2',True,0]

def test_native_callback_result_is_returned_to_typed_service_form(tmp_path):
    api,engine=fixture(tmp_path)
    result=api.native_invoke('Keyframes','has-animation-default',[6]);assert result['ok'] and result['data']['result']==42

def test_host_does_not_reconstruct_or_drop_unknown_project_fields(tmp_path):
    api,engine=fixture(tmp_path);result=api.native_state()
    assert result['data']['state']['view']['project']['futureVendorField']=={'keep':'exactly'}

def test_native_default_save_and_favorite_routes_keep_original_store(tmp_path):
    api,engine=fixture(tmp_path);api.motion={'presets':[{'id':'motion.push-in','category':'basic','nativeIndex':6}]}
    result=api.preferences({'favorites':{'basic':['motion.push-in']},'defaults':{'basic':{'preset':'motion.push-in','controls':{'strength':1,'duration':.4,'speed':1,'intensity':1}}}})
    assert result['ok'];assert any(m=='desktop/animation-default' for m,_ in engine.calls)
    assert any(p.get('target')=='Keyframes' and p.get('arguments')==[6] for _,p in engine.calls)

def test_preferences_reject_secret_fields_and_malformed_locks(tmp_path):
    api,engine=fixture(tmp_path)
    assert not api.preferences({'apiToken':'do-not-write'})['ok']
    assert not api.preferences({'lockedTracks':'t1'})['ok']
    assert not api.settings_path.exists()
