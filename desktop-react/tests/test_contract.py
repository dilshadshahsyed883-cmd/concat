import json,sys,importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'desktop-react'))
from host.api import Api,public_api,save_json

def test_only_explicit_rpc_methods_are_exposed():
    api=Api(settings=Path('/nonexistent/settings.json'))
    exposed=vars(public_api(api))
    assert 'native_invoke' in exposed and 'native_state' in exposed
    assert 'shutdown' not in exposed and '_call' not in exposed and 'window' not in exposed
    assert 'advanced_editor' not in exposed

def test_settings_are_atomic_and_preserve_unicode(tmp_path):
    path=tmp_path/'preferences.json';value={'favorites':{'basic':['motion.push-in']},'name':'Café'}
    save_json(path,value);assert json.loads(path.read_text())==value;assert not list(tmp_path.glob('*.tmp'))

def test_generated_adapters_cover_public_callbacks():
    src=ROOT/'upstream/src/crates/concat';script=src/'tools/generate_react_wire.py'
    spec=importlib.util.spec_from_file_location('wire',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    schema=module.parse(src/'ui')
    assert len(schema['targets']['Editor']['properties'])>150
    assert sum(len(t['callbacks']) for t in schema['targets'].values())>=350
    assert module.generate(schema)==(src/'src/react_wire.rs').read_text()
    assert schema==json.loads((src/'src/react_schema.json').read_text())
    for name in ['captions-begin','speech-begin','voiceover-action','open-settings']:
        assert any(c['name']==name for c in schema['targets']['App']['callbacks'])
