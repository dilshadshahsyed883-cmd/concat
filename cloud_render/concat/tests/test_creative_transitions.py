"""Creative transition catalogue + deterministic scalar fixture verification."""
import math
import json
from pathlib import Path
import tomllib
import unittest
from cloud_render.concat.hybrid.creative_transitions import CREATIVE_PRESETS, CREATIVE_IDS, CREATIVE_KINDS, mask_at
from cloud_render.concat.hybrid.capcut_transitions import CAPCUT_IDS
from cloud_render.concat.hybrid.contract import FrameSpan, cuda
from cloud_render.concat.hybrid.transitions import Transition, render_transition, NATIVE_TRANSITIONS
from cloud_render.concat.hybrid.project import validate_gpu_manifest
from cloud_render.concat.tests.test_gpu_compat_validation import minimal_project

class CreativeFixtureTests(unittest.TestCase):
    def test_catalogue_is_distinct_and_original_twenty_are_preserved(self):
        self.assertFalse(set(CREATIVE_IDS)&set(CAPCUT_IDS))
        self.assertEqual(len(CAPCUT_IDS),20)
        self.assertEqual(len(set(CREATIVE_IDS)),9)
        self.assertTrue(set(CREATIVE_IDS)<=set(NATIVE_TRANSITIONS))
        profile=json.loads((Path(__file__).parents[1]/'hybrid/gpu_ui_capabilities.json').read_text())
        self.assertTrue(set(CREATIVE_IDS)<=set(profile['transitions']))
        packages=Path(__file__).parents[3]/'upstream/src/crates/concat-effects/packages'
        for slug,name in CREATIVE_PRESETS:
            manifest=tomllib.loads((packages/('concat.'+slug)/'effect.toml').read_text())
            self.assertEqual(manifest['effect']['name'],name)
            self.assertEqual(manifest['effect']['kind'],'transition')
            self.assertEqual(manifest['param'][0]['default'],70)
            self.assertIn('fn transition(', (packages/('concat.'+slug)/'effect.wgsl').read_text())

    def test_endpoints_finite_deterministic_masks_and_midpoint_distinction(self):
        masks={}
        for kind in CREATIVE_KINDS:
            grid=[(x/31,y/23) for y in range(24) for x in range(32)]
            self.assertTrue(all(mask_at(kind,u,v,0)==0 for u,v in grid))
            self.assertTrue(all(mask_at(kind,u,v,1)==1 for u,v in grid))
            masks[kind]=tuple(round(mask_at(kind,u,v,.5),5) for u,v in grid)
            self.assertTrue(all(math.isfinite(x) and 0<=x<=1 for x in masks[kind]))
            self.assertTrue(any(0<x for x in masks[kind]))
            self.assertTrue(any(x<1 for x in masks[kind]))
            self.assertEqual(masks[kind],tuple(round(mask_at(kind,u,v,.5),5) for u,v in grid))
        self.assertEqual(len(set(masks.values())),len(CREATIVE_KINDS))
        self.assertNotEqual(masks['paper-rip-diagonal'],masks['diagonal-wipe'])

    def test_preflight_accepts_every_creative_id(self):
        for ident in CREATIVE_IDS:
            document=minimal_project();timeline=document['timelines'][0]
            timeline['clips'][0]['duration']=2
            timeline['clips'].append(dict(id='next',kind='image',trackId='video',mediaId='photo',start=2.,duration=2.,transitionIn=dict(id=ident,duration=.5,intensity=.3)))
            validate_gpu_manifest(document)

    def test_native_package_progress_uses_the_full_half_open_span(self):
        for kind in CREATIVE_KINDS:
            cut=Transition(kind,FrameSpan(10,20))
            self.assertEqual(cut.progress(10),0)
            self.assertEqual(cut.progress(15),.5)
            self.assertEqual(cut.progress(19),.9)
            self.assertEqual(cut.progress(20),1)

    def test_parameters_fail_closed_before_cuda(self):
        for value in [True,-.1,1.1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError): mask_at('brush-stroke',.5,.5,value)
            with self.assertRaises(ValueError): mask_at('brush-stroke',.5,.5,.5,value)
        with self.assertRaises(ValueError): mask_at('unknown',.5,.5,.5)

class CreativeCudaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try: cls.cp=cuda()
        except RuntimeError as error: raise unittest.SkipTest(str(error)) from error
    def test_all_new_kernel_modes_are_finite_and_sources_immutable(self):
        cp=self.cp;a=cp.zeros((24,32,4),dtype=cp.float32);b=cp.zeros_like(a)
        a[...,0]=1;b[...,2]=1;a[...,3]=b[...,3]=1
        for kind in CREATIVE_KINDS:
            cut=Transition(kind,FrameSpan(0,11))
            self.assertIs(render_transition(a,b,cut,0),a)
            self.assertIs(render_transition(a,b,cut,11),b)
            rendered=render_transition(a,b,cut,5)
            self.assertTrue(bool(cp.isfinite(rendered).all()))
            self.assertTrue(bool(((rendered>=0)&(rendered<=1)).all()))
            self.assertTrue(bool((a[...,0]==1).all()))

if __name__=='__main__':unittest.main()
