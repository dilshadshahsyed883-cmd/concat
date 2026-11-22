"""Validate native Rust canvas fixtures and deterministic geometry without video processing."""
from copy import deepcopy
import json
import os
import tempfile
from pathlib import Path
import unittest
from cloud_render.concat.hybrid.canvas_shapes import parse_shape, parse_mask, mask_coverage
from cloud_render.concat.hybrid.project import load_project, validate_gpu_manifest
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
        self.assertEqual(len(files),16)
        for file in files:
            with self.subTest(file=file.name):
                document = json.loads(file.read_text())
                validate_gpu_manifest(document)
                timeline = document['timelines'][0]
                clips = timeline['clips']
                # These fixtures come from the native Canvas preset application,
                # not from a reconstructed preview. The GPU project consumer
                # must see compositing layers and user-authored text only.
                self.assertFalse(any(c.get('canvasRole') in ('headline', 'headlineText')
                                     for c in clips))
                self.assertFalse(any(t.get('canvasRole') in ('banner', 'headline')
                                     for t in timeline['tracks']))
                self.assertTrue(any(c.get('canvasRole') == 'backdrop' for c in clips))
                # A frame is optional for blur, floating-card, photo-print,
                # cinema and editorial layouts. All still preserve the
                # original clip and provide a native backdrop.
                if file.name.startswith(('canvas-1-', 'canvas-2-', 'canvas-4-')):
                    self.assertTrue(any(c.get('canvasRole') == 'frame' for c in clips))
                self.assertEqual(len([c for c in clips if c.get('canvasRole') == 'badgeText']), 1)
                # Native fixtures point to source.mp4. The worker's manifest
                # loader resolves real ZIP-relative paths; supply the expected
                # placeholder in this isolated test rather than changing the
                # worker's safe media-path validation.
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'concat.json').write_text(json.dumps(document))
                    (root / 'source.mp4').write_bytes(b'canvas fixture placeholder')
                    plan = load_project(root, lambda _: None)
                self.assertGreaterEqual(len(plan.lanes), 2)

    def test_owned_dense_motion_native_fixture_matches_worker_planner(self):
        directory = os.environ.get('CONCAT_CANVAS_FIXTURE_DIR')
        if not directory:
            self.skipTest('Run native project tests with CONCAT_CANVAS_FIXTURE_DIR first')
        contract = json.loads((Path(directory) / 'generated-motion-contract.json').read_text())
        document = contract['document']
        clip = document['timelines'][0]['clips'][0]
        self.assertIn('generatedMotionV1', clip)
        self.assertEqual(clip['keys'], clip['generatedMotionV1']['keys'])
        validate_gpu_manifest(document)
        # Placeholder bytes suffice: this checks planning, without decoding or GPU IO.
        document['media'][0]['path'] = 'fixture.mp4'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'concat.json').write_text(json.dumps(document))
            (root / 'fixture.mp4').write_bytes(b'native motion contract placeholder')
            plan = load_project(root, lambda _: None)
        picture = next(picture for lane in plan.lanes for picture in lane if picture.id == clip['id'])
        self.assertEqual(sum(map(len, picture.keys.values())), len(clip['keys']))
        for expected in contract['samples']:
            actual = picture.transform(expected['frame'])
            for channel in ('scale', 'pan_x', 'pan_y', 'opacity'):
                self.assertAlmostEqual(getattr(actual, channel), expected[channel], places=5)

if __name__=='__main__':unittest.main()
