"""The CUDA planner must use identical shared native animation generation."""
import copy
import os
import shutil
import unittest
from unittest.mock import patch

from cloud_render.concat.hybrid.project import validate_gpu_manifest
from cloud_render.concat.tests.test_gpu_compat_validation import minimal_project, load_fixture

BINARY = os.environ.get("CONCAT_MOTION_EXPAND") or shutil.which("concat-motion-expand")
CONTROLS = {"strength": 1.0, "duration": 1.0, "speed": 1.0, "intensity": 1.0}


def parameterized_project(preset="motion.push-in", controls=None):
    doc = minimal_project()
    doc["version"] = 2
    clip = doc["timelines"][0]["clips"][0]
    clip.pop("keys", None)
    clip["generatedMotionV1"] = {
        "version": 1, "preset": preset, "parameterOnly": True, "style": None,
        "recipe": {"version": 1, "frameSeconds": 1 / 30, "controls": controls or CONTROLS},
    }
    return doc


class ParameterizedMotionTests(unittest.TestCase):
    def test_unexpanded_motion_fails_closed_without_generator(self):
        with patch.dict(os.environ, {"CONCAT_MOTION_EXPAND": "/nonexistent/concat-motion-expand"}):
            with self.assertRaisesRegex(ValueError, "parameterized animation"):
                validate_gpu_manifest(parameterized_project())

    def test_version_one_rejects_compact_marker(self):
        doc = parameterized_project()
        doc["version"] = 1
        with self.assertRaisesRegex(ValueError, "Unexpanded parameterized motion"):
            validate_gpu_manifest(doc)

    @unittest.skipUnless(BINARY, "shared Rust generator not built")
    def test_generated_basic_motion_matches_native_keys_on_every_frame(self):
        doc = parameterized_project()
        expanded = validate_gpu_manifest(doc)
        keys = expanded["clips"][0]["keys"]
        self.assertEqual(len(keys), 2)
        self.assertAlmostEqual(keys[0]["value"], 1.0)
        self.assertAlmostEqual(keys[-1]["value"], 1.18)
        native = copy.deepcopy(doc)
        native["version"] = 1
        clip = native["timelines"][0]["clips"][0]
        del clip["generatedMotionV1"]
        clip["keys"] = keys
        a = next(p for lane in load_fixture(doc).lanes for p in lane if p.id == "photo1")
        b = next(p for lane in load_fixture(native).lanes for p in lane if p.id == "photo1")
        for frame in range(30):
            self.assertEqual(a.transform(frame), b.transform(frame))

    @unittest.skipUnless(BINARY, "shared Rust generator not built")
    def test_physics_and_canvas_regenerate(self):
        physics = validate_gpu_manifest(parameterized_project("physics.spring-zoom"))
        self.assertGreater(len(physics["clips"][0]["keys"]), 2)
        canvas_controls = {
            "start_scale": .7, "middle_scale": 1.0, "peak_time": .5, "auto_fit": True,
            "end_scale": .7, "duration_seconds": 3.0, "entry_seconds": .5, "hold_seconds": 0.0,
            "exit_seconds": .5, "focus_middle": [0, 0], "focus_hold_seconds": .5,
            "delay_seconds": 0.0, "speed": 1.0, "strength": 1.0, "intensity": 1.0,
            "blur": .65, "reverse": False, "start_anchor": None, "end_anchor": None,
            "easing": "Smooth", "seed": 18446744073709551598,
        }
        canvas = validate_gpu_manifest(parameterized_project("canvas-motion.breathing-loop", canvas_controls))
        scale = [k["value"] for k in canvas["clips"][0]["keys"] if k["property"] == "scale"]
        self.assertAlmostEqual(scale[0], .7)
        self.assertAlmostEqual(max(scale), 1.0)
        self.assertAlmostEqual(scale[-1], .7)
        self.assertGreater(len(scale), 3)

    @unittest.skipUnless(BINARY, "shared Rust generator not built")
    def test_corrupt_parameters_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "parameterized animation"):
            validate_gpu_manifest(parameterized_project("motion.unknown"))
