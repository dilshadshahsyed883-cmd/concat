"""Execute every native preset adapter path; this is not a rendered-video test."""
import argparse,json,subprocess
from pathlib import Path
def main():
    p=argparse.ArgumentParser();p.add_argument('--helper',type=Path,required=True);p.add_argument('--report',type=Path);args=p.parse_args()
    def call(value):
        r=subprocess.run([str(args.helper.resolve())],input=json.dumps(value,allow_nan=False),text=True,encoding='utf8',capture_output=True,timeout=30,check=True)
        return json.loads(r.stdout)
    catalogue=call({'operation':'catalogue'})['data'];clip={'id':'c1','kind':'image','trackId':'t1','mediaId':'m1','start':0,'duration':4,'scale':1}
    motion=[p for p in catalogue['presets'] if p['category']!='adjustment'];assert sorted(p['nativeIndex'] for p in motion)==list(range(59))
    checks=[]
    for preset in catalogue['presets']:
        result=call({'clip':clip,'frame':1/30,'preset':preset['id'],'controls':{}});assert result['ok'],(preset,result)
        assert result['data']['op'] in ['applyGeneratedMotion','applyCanvasPreset'];checks.append(preset['id'])
    manual={**clip,'keys':[{'property':'scale','at':0,'value':1}]}
    assert not call({'clip':manual,'frame':1/30,'preset':'motion.push-in','controls':{}})['ok']
    for controls in [{'strength':99},{'unknownControl':1}]:assert not call({'clip':clip,'frame':1/30,'preset':'motion.push-in','controls':controls})['ok']
    canvas=next(p for p in motion if p['category']=='canvas')['id'];mask={'shape':'rectangle','x':0,'y':0,'width':.8,'height':.7,'rotation':0,'roundness':.04,'feather':.02}
    result=call({'clip':{**clip,'mask':mask},'frame':1/30,'preset':canvas,'controls':{}});assert result['ok'],result
    assert result['data']['canvasStyle'][1]==mask,'Applying Canvas motion must retain a manual mask'
    report={'classification':'real native command generation; no video rendering','presets':checks,'manual_keys_protected':True,'manual_mask_preserved':True,'invalid_controls_rejected':True}
    if args.report:args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2))
    print('PASS native adapter paths:',len(checks),'presets; native indices, manual keys/masks and control validation')
if __name__=='__main__':main()
