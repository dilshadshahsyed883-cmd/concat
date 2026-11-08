"""Native Concat Canvas contract, fail-closed validation and optional CUDA pixels."""
import importlib.util
import unittest

from cloud_render.concat.hybrid.canvas import (CanvasBackground, fitted_rect,
                                                make_background, parse_background)
from cloud_render.concat.hybrid.project import (validate_gpu_manifest, _plan_project)
from cloud_render.concat.tests.test_gpu_compat_validation import minimal_project


def with_gpu():
    if importlib.util.find_spec("cupy") is None:
        return False
    try:
        import cupy
        return cupy.cuda.runtime.getDeviceCount() > 0
    except Exception:
        return False


class CanvasValidationTests(unittest.TestCase):
    def test_native_color_blur_and_none(self):
        self.assertIsNone(parse_background({"kind": "none"}))
        self.assertIsNone(parse_background(None))
        self.assertEqual(parse_background({"kind": "blur", "amount": .75}),
                         CanvasBackground("blur", .75))
        self.assertEqual(parse_background({"kind": "color", "color": "#ff8040"}).color,
                         (1., 128/255, 64/255))
        self.assertEqual(parse_background({"kind": "color", "color": "#0000ff80"}).color,
                         (0., 0., 1.))

    def test_invalid_canvas_fail_closed(self):
        bad = ({"kind": "image", "mediaId": "photo"},
               {"kind": "blur", "amount": -1},
               {"kind": "blur", "amount": 1.001},
               {"kind": "blur", "amount": float("nan")},
               {"kind": "blur", "amount": True},
               {"kind": "blur", "amount": "20"},
               {"kind": "color", "color": "red"},
               {"kind": "color", "color": "#gg0000"},
               {"kind": "color", "color": "#112233", "opacity": .5})
        for background in bad:
            with self.subTest(background=background), self.assertRaises(ValueError):
                parse_background(background)
        for background in bad:
            document = minimal_project()
            document["timelines"][0]["background"] = background
            with self.subTest(manifest=background), self.assertRaises(ValueError):
                validate_gpu_manifest(document)

    def test_inheritance_selected_all_and_keyframes(self):
        doc = minimal_project()
        timeline = doc["timelines"][0]
        timeline["background"] = {"kind": "blur", "amount": .34}
        first = timeline["clips"][0]
        first["rotation"] = 12
        first["offsetX"] = -.25
        first["keys"] = [{"property": "scale", "at": 0., "value": 1.},
                         {"property": "scale", "at": 1., "value": 1.5}]
        timeline["clips"].append({
            "id": "photo2", "kind": "image", "trackId": "video",
            "mediaId": "photo", "start": 1., "duration": 1.,
            "background": {"kind": "color", "color": "#0000ff"}
        })
        validate_gpu_manifest(doc)
        plan = _plan_project(doc, timeline, lambda _: __import__("pathlib").Path("photo.jpg"))
        self.assertEqual(plan.background_rank, 0)
        a, b = plan.lanes[0]
        self.assertEqual(a.background, CanvasBackground("blur", .34))
        self.assertEqual(b.background, CanvasBackground("color", color=(0., 0., 1.)))
        self.assertEqual(a.transform(0).rotation_degrees, 12)
        self.assertEqual(a.transform(0).pan_x, -.25)
        self.assertGreater(a.transform(29).scale, a.transform(0).scale)
        # Native Apply-to-All clears clip overrides and sets timeline style.
        b.background = a.background
        self.assertEqual(a.background, b.background)

    def test_blur_is_applied_to_background_copy_not_keyframed_foreground(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from cloud_render.concat.hybrid.render import picture

        bg = CanvasBackground("blur", .4)
        animated = object()  # Represents an evaluated frame-specific transform.
        clip = SimpleNamespace(kind="image", path="photo.jpg", flip_h=False, flip_v=False,
                               background=bg, filters=(),
                               transform=lambda index: animated)
        sources = SimpleNamespace(image=Mock(return_value="original-frame"),
                                  backdrop=Mock(return_value="blurred-background"))
        with patch("cloud_render.concat.hybrid.render.apply_transform",
                   return_value="sharp-animated-foreground") as transform:
            foreground, background = picture(sources, clip, 21, return_background=True)
        sources.backdrop.assert_called_once_with(clip, "original-frame")
        transform.assert_called_once_with("original-frame", animated)
        self.assertEqual(foreground, "sharp-animated-foreground")
        self.assertEqual(background, "blurred-background")

    def test_contain_geometry_for_landscape_portrait_and_square(self):
        self.assertEqual(fitted_rect(1920, 1080, 1920, 1080), (0, 0, 1920, 1080))
        self.assertEqual(fitted_rect(1080, 1920, 1920, 1080), (656, 0, 608, 1080))
        self.assertEqual(fitted_rect(1000, 1000, 1920, 1080), (420, 0, 1080, 1080))
        for values in ((0, 100, 1920, 1080), (100, -1, 1920, 1080)):
            with self.assertRaises(ValueError):
                fitted_rect(*values)

    def test_canvas_not_allowed_on_audio(self):
        doc = minimal_project()
        doc["media"].append({"id": "audio", "kind": "audio", "path": "a.mp3"})
        doc["timelines"][0]["clips"].append({
            "id": "audio1", "kind": "audio", "trackId": "video",
            "mediaId": "audio", "start": 0., "duration": 1.,
            "background": {"kind": "blur", "amount": .5}
        })
        with self.assertRaisesRegex(ValueError, "Canvas background"):
            validate_gpu_manifest(doc)


@unittest.skipUnless(with_gpu(), "CUDA is required for pixel equivalence tests")
class CanvasCudaTests(unittest.TestCase):
    def test_blur_background_keeps_foreground_bytes_unchanged(self):
        import cupy as cp
        from cloud_render.concat.hybrid.animations import Transform, apply_transform
        from cloud_render.concat.hybrid.compositor import Layer, composite
        foreground = cp.zeros((108, 192, 4), dtype=cp.float32)
        foreground[20:88, 48:144, :3] = 1.
        foreground[20:88, 48:144, 3] = 1.
        original = foreground.copy()
        bg = make_background(foreground, (1080, 1920),
                             CanvasBackground("blur", .3))
        moved = apply_transform(foreground, Transform(pan_x=.2, rotation_degrees=15))
        output = composite([Layer(bg), Layer(moved)], width=192, height=108)
        self.assertEqual(bg.shape, original.shape)
        self.assertTrue(bool(cp.allclose(original, foreground)))
        self.assertTrue(bool(cp.all(output[:, :, 3] > .99)))
        self.assertFalse(bool(cp.allclose(output, moved)))


if __name__ == "__main__":
    unittest.main()
