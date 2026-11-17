"""Validate native Rust canvas fixtures and deterministic geometry without video processing."""
import json
import os
from pathlib import Path
import unittest
from cloud_render.concat.hybrid.canvas_shapes import parse_shape, parse_mask, mask_coverage
from cloud_render.concat.hybrid.project import validate_gpu_manifest

class CanvasPresetContracts(unittest.TestCase):
    def test_native_rectangle_and_rounded_mask_geometry(self):
        shape=parse_shape({'kind':'square','width':.9,'height':.8,'cornerRadius':.04})
        self.assertEqual((shape.width,shape.height),(.9,.8))
        mask=parse_mask({'shape':'rectangle','width':1.,'height':1.6,'roundness':.2})
        self.assertGreater(mask_coverage(mask,.5,.5,720,1280),.99)
        self.assertLess(mask_coverage(mask,0,0,720,1280),.01)
        with self.assertRaises(ValueError):parse_mask({'shape':'circle'})
        with self.assertRaises(ValueError):parse_shape({'kind':'square','width':float('nan')})
    def test_rust_generated_portrait_and_landscape_canvases(self):
        directory=os.environ.get('CONCAT_CANVAS_FIXTURE_DIR')
        if not directory:self.skipTest('Run Rust canvas fixture tests with CONCAT_CANVAS_FIXTURE_DIR to validate cross-language serialization')
        files=list(Path(directory).glob('canvas-*.json'))
        self.assertEqual(len(files),6)
        for file in files:
            with self.subTest(file=file.name):validate_gpu_manifest(json.loads(file.read_text()))

if __name__=='__main__':unittest.main()
