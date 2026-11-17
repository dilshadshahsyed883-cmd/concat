"""Validate native Rust canvas fixtures and deterministic geometry without video processing."""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from cloud_render.concat.hybrid.canvas_shapes import parse_shape, parse_mask, mask_coverage
from cloud_render.concat.hybrid.project import validate_gpu_manifest
from cloud_render.concat.tests.test_gpu_compat_validation import minimal_project, load_fixture

class CanvasPresetContracts(unittest.TestCase):
    def test_native_rectangle_and_rounded_mask_geometry(self):
        shape=parse_shape({'kind':'square','width':.9,'height':.8,'cornerRadius':.04})
        self.assertEqual((shape.width,shape.height),(.9,.8))
        mask=parse_mask({'shape':'rectangle','width':1.,'height':1.6,'roundness':.2})
        self.assertGreater(mask_coverage(mask,.5,.5,720,1280),.99)
        self.assertLess(mask_coverage(mask,0,0,720,1280),.01)
        with self.assertRaises(ValueError):parse_mask({'shape':'circle'})
        with self.assertRaises(ValueError):parse_shape({'kind':'square','width':float('nan')})
    def test_mixed_presets_keep_ordinary_media_as_primary_background_lane(self):
        document = minimal_project()
        timeline = document['timelines'][0]
        foreground = timeline['clips'][0]
        foreground['background'] = {'kind':'none'}
        foreground['canvasPreset'] = {'preset':3}
        companion = deepcopy(foreground)
        companion.update(id='canvas-bg', trackId='canvas-track', canvasOwner='photo1',
                         canvasRole='backdrop', background={'kind':'blur','amount':.75})
        ordinary = deepcopy(foreground)
        ordinary.pop('canvasPreset')
        ordinary.update(id='ordinary', start=1., background={'kind':'blur','amount':.4})
        timeline['tracks'].insert(0, {'id':'canvas-track','visible':True})
        timeline['clips'].extend([companion, ordinary])
        plan = load_fixture(document)
        self.assertEqual(plan.background_rank, 1)
        owned = plan.lanes[0][0]
        self.assertTrue(owned.canvas_companion)
        self.assertTrue(owned.canvas_backdrop)
        self.assertEqual(owned.background.amount, .75)
        normal = next(c for c in plan.lanes[plan.background_rank] if c.id == 'ordinary')
        self.assertFalse(normal.canvas_companion)
        self.assertFalse(normal.canvas_backdrop)
        self.assertEqual(normal.background.amount, .4)

    def test_rust_generated_portrait_and_landscape_canvases(self):
        directory=os.environ.get('CONCAT_CANVAS_FIXTURE_DIR')
        if not directory:self.skipTest('Run Rust canvas fixture tests with CONCAT_CANVAS_FIXTURE_DIR to validate cross-language serialization')
        files=list(Path(directory).glob('canvas-*.json'))
        self.assertEqual(len(files),6)
        for file in files:
            with self.subTest(file=file.name):validate_gpu_manifest(json.loads(file.read_text()))

if __name__=='__main__':unittest.main()
