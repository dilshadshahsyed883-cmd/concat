import os
import unittest

from cloud_render.concat.hybrid.compositor import Layer, _clip_axis, composite
from cloud_render.concat.hybrid.contract import cuda


class CompositorValidationTests(unittest.TestCase):
    def test_layer_settings(self):
        for x in (True, 0.25):
            with self.subTest(x=x), self.assertRaises(TypeError):
                Layer(None, x=x)
        for opacity in (-0.01, 1.01, float("nan"), True):
            with self.subTest(opacity=opacity), self.assertRaises(ValueError):
                Layer(None, opacity=opacity)

    def test_clipping_maps_to_the_source_rectangle(self):
        self.assertEqual(_clip_axis(-2, 4, 5), (slice(0, 2), slice(2, 4)))
        self.assertEqual(_clip_axis(3, 4, 5), (slice(3, 5), slice(0, 2)))
        self.assertIsNone(_clip_axis(-4, 4, 5))
        self.assertIsNone(_clip_axis(5, 4, 5))

    def test_bad_canvas_rejected_before_gpu_allocation(self):
        with self.assertRaisesRegex(ValueError, "dimensions"):
            composite([], width=0, height=1)
        with self.assertRaisesRegex(ValueError, "Background"):
            composite([], width=2, height=1, background=(1, 0, 0, 2))


class CudaCompositorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cp = cuda()
        except RuntimeError as error:
            if os.getenv("CONCAT_REQUIRE_CUDA") == "1":
                raise
            raise unittest.SkipTest(str(error)) from error

    def test_ordered_straight_alpha(self):
        cp = self.cp
        bottom = cp.array([[[1, 0, 0, 0.5]]], dtype=cp.float32)
        top = cp.array([[[0, 0, 1, 1]]], dtype=cp.float32)
        output = composite([Layer(bottom), Layer(top, opacity=0.5)], width=1, height=1)
        self.assertIsInstance(output, cp.ndarray)
        cp.testing.assert_allclose(output, cp.array([[[1 / 3, 0, 2 / 3, 0.75]]]))
        reverse = composite([Layer(top, opacity=0.5), Layer(bottom)], width=1, height=1)
        cp.testing.assert_allclose(reverse, cp.array([[[2 / 3, 0, 1 / 3, 0.75]]]))
        cp.testing.assert_array_equal(bottom, cp.array([[[1, 0, 0, 0.5]]]))

    def test_crops_strided_texture_and_keeps_background(self):
        cp = self.cp
        texture = cp.array([[[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1]]],
                           dtype=cp.float32)
        output = composite([Layer(texture[:, ::-1], x=-2, y=1),
                            Layer(texture, x=9)],
                           width=2, height=2, background=(0, 0, 1, 1))
        cp.testing.assert_array_equal(output, cp.array([
            [[0, 0, 1, 1], [0, 0, 1, 1]],
            [[1, 0, 0, 1], [0, 0, 1, 1]],
        ]))

    def test_invisible_pixels_do_not_contaminate_canvas(self):
        cp = self.cp
        transparent = cp.array([[[1, 0, 0, 0]]], dtype=cp.float32)
        result = composite([Layer(transparent)], width=1, height=1)
        cp.testing.assert_array_equal(result, cp.zeros_like(transparent))


if __name__ == "__main__":
    unittest.main()
