"""Physical motion presets must survive the native CUDA worker preflight.

The desktop samples deterministic physical curves into ordinary Concat ClipKey
records. No worker-side preset implementation or CPU fallback is needed.
"""
import unittest

from cloud_render.concat.tests.test_gpu_compat_validation import minimal_project, load_fixture
from cloud_render.concat.hybrid.project import validate_gpu_manifest


class PhysicsGpuContractTests(unittest.TestCase):
    def test_native_physics_keys_validate_and_sample_correctly(self):
        # Simulates the native editor's physical spring zoom + pendulum keys.
        document = minimal_project()
        clip = document["timelines"][0]["clips"][0]
        clip["keys"] = [
            {"property": "scale", "at": 0.0, "value": 1.0, "ease": "linear"},
            {"property": "scale", "at": .25, "value": 1.13, "ease": "linear"},
            {"property": "scale", "at": .5, "value": 1.10, "ease": "linear"},
            {"property": "scale", "at": 29/30, "value": 1.12, "ease": "linear"},
            {"property": "rotation", "at": 0.0, "value": 9.0, "ease": "linear"},
            {"property": "rotation", "at": .5, "value": -.2, "ease": "linear"},
            {"property": "rotation", "at": 29/30, "value": 0.1, "ease": "linear"},
        ]
        self.assertEqual(validate_gpu_manifest(document)["id"], "timeline")
        plan = load_fixture(document)
        picture = next(layer for lane in plan.lanes for layer in lane
                       if layer.id == "photo1")
        self.assertAlmostEqual(picture.transform(0).scale, 1.0, places=6)
        self.assertAlmostEqual(picture.transform(0).rotation_degrees, 9.0, places=6)
        self.assertAlmostEqual(picture.transform(15).scale, 1.10, places=6)
        self.assertAlmostEqual(picture.transform(29).scale, 1.12, places=6)
        self.assertAlmostEqual(picture.transform(29).rotation_degrees, 0.1, places=6)

    def test_image_and_video_share_the_same_supported_physics_key_properties(self):
        document = minimal_project()
        clip = document["timelines"][0]["clips"][0]
        clip["keys"] = [
            {"property": "offsetY", "at": 0.0, "value": -.18},
            {"property": "offsetY", "at": .5, "value": -.04},
            {"property": "offsetY", "at": 29/30, "value": 0.0},
            {"property": "offsetX", "at": 0.0, "value": -.14},
            {"property": "offsetX", "at": 29/30, "value": 0.0},
        ]
        validate_gpu_manifest(document)
        picture = next(layer for lane in load_fixture(document).lanes for layer in lane)
        self.assertAlmostEqual(picture.transform(0).pan_y, -.18)
        self.assertAlmostEqual(picture.transform(29).pan_x, 0.0)
        document["media"][0].update(kind="video", path="video.mp4")
        clip["kind"] = "video"
        self.assertEqual(validate_gpu_manifest(document)["id"], "timeline")


if __name__ == "__main__":
    unittest.main()
