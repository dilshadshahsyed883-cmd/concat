import unittest

from cloud_render.concat.hybrid.animations import (
    Animation,
    Keyframe,
    Transform,
    apply_transform,
    render_animation,
)
from cloud_render.concat.hybrid.contract import cuda


class AnimationTimingTests(unittest.TestCase):
    def test_clamps_and_interpolates_all_fields(self):
        first = Transform()
        last = Transform(x=8, y=-6, pan_x=0.5, pan_y=-0.25, zoom=3,
                         scale=2, rotation_degrees=180, opacity=0.5)
        animation = Animation((Keyframe(10, first), Keyframe(20, last)))
        self.assertEqual(animation.sample(0), first)
        self.assertEqual(animation.sample(10), first)
        self.assertEqual(animation.sample(15), Transform(
            x=4, y=-3, pan_x=0.25, pan_y=-0.125, zoom=2, scale=1.5,
            rotation_degrees=90, opacity=0.75))
        self.assertEqual(animation.sample(20), last)
        self.assertEqual(animation.sample(900), last)

    def test_easing_is_owned_by_left_keyframe(self):
        expected = {"linear": 4, "ease-in": 1, "ease-out": 7,
                    "ease-in-out": 2.5, "hold": 0}
        for easing, x in expected.items():
            with self.subTest(easing=easing):
                animation = Animation((Keyframe(0, Transform(), easing),
                                       Keyframe(8, Transform(x=16))))
                self.assertEqual(animation.sample(2).x, x)
                self.assertEqual(animation.sample(8).x, 16)

    def test_rejects_invalid_transforms_and_timing(self):
        for values in ({"zoom": 0}, {"scale": -1}, {"x": float("inf")},
                       {"opacity": 1.1}, {"pan_y": float("nan")}, {"opacity": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Transform(**values)
        key = Keyframe(4, Transform())
        for keys in ((), (key, key), (key, Keyframe(3, Transform()))):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                Animation(keys)
        for frame in (-1, 0.5, True):
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                Animation((key,)).sample(frame)
        with self.assertRaises(ValueError):
            Keyframe(0, Transform(), "unknown")


class AnimationGpuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cp = cuda()
        except RuntimeError as error:
            raise unittest.SkipTest(str(error)) from error

    def test_timed_pan_translation_and_opacity(self):
        cp = self.cp
        image = cp.zeros((4, 4, 4), dtype=cp.float32)
        image[1, 0] = cp.asarray([1, 0, 0, 1], dtype=cp.float32)
        animation = Animation((Keyframe(0, Transform()),
                               Keyframe(4, Transform(pan_x=0.5, opacity=0.5))))
        frame = render_animation(image, animation, 2)
        self.assertIsInstance(frame, cp.ndarray)
        self.assertEqual(frame[1, 1].get().tolist(), [1, 0, 0, 0.75])
        self.assertEqual(frame[1, 0].get().tolist(), [0, 0, 0, 0])
        self.assertEqual(image[1, 0].get().tolist(), [1, 0, 0, 1])

    def test_alpha_correct_interpolation_and_clockwise_rotation(self):
        cp = self.cp
        image = cp.zeros((3, 3, 4), dtype=cp.float32)
        image[1, 2] = cp.asarray([1, 0, 0, 1], dtype=cp.float32)
        image[1, 1] = cp.asarray([0, 1, 0, 0], dtype=cp.float32)
        frame = apply_transform(image, Transform(x=-0.5))
        cp.testing.assert_allclose(frame[1, 1], cp.asarray([1, 0, 0, 0.5]), atol=1e-6)
        rotated = apply_transform(image, Transform(rotation_degrees=90))
        cp.testing.assert_allclose(rotated[2, 1], cp.asarray([1, 0, 0, 1]), atol=1e-6)

    def test_zoom_keeps_center_and_crops_edges(self):
        cp = self.cp
        image = cp.zeros((5, 5, 4), dtype=cp.float32)
        image[2, 2] = cp.asarray([0, 0, 1, 1], dtype=cp.float32)
        image[0, 0] = cp.asarray([1, 0, 0, 1], dtype=cp.float32)
        frame = apply_transform(image, Transform(zoom=2))
        cp.testing.assert_allclose(frame[2, 2], cp.asarray([0, 0, 1, 1]), atol=1e-6)
        cp.testing.assert_allclose(frame[0, 0], cp.zeros(4), atol=1e-6)
