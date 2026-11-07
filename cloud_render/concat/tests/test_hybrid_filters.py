import os
from array import array
from pathlib import Path
import tempfile
import unittest

from cloud_render.concat.hybrid.contract import cuda
from cloud_render.concat.hybrid.filters import CubeLut, FilterSettings, apply


def identity_cube():
    return ["TITLE \"Test # identity\"", "LUT_3D_SIZE 2"] + [
        f"{r} {g} {b}" for b in range(2) for g in range(2) for r in range(2)
    ]


class FilterValidationTests(unittest.TestCase):
    def test_rejects_nonfinite_and_out_of_range_settings(self):
        for settings in ({"brightness": float("nan")}, {"contrast": -1}, {"exposure": 9},
                         {"saturation": float("inf")}, {"temperature": -2},
                         {"blur_radius": True}, {"blur_radius": 17}, {"sharpen": 5}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                FilterSettings(**settings)

    def test_cube_retains_red_fastest_axis_order(self):
        lut = CubeLut.parse(identity_cube())
        values = array("f")
        values.frombytes(lut.entries)
        self.assertEqual(lut.size, 2)
        self.assertEqual(lut.domain_min, (0, 0, 0))
        self.assertEqual(lut.domain_max, (1, 1, 1))
        self.assertEqual(tuple(values[0:12]), (0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0))

    def test_cube_accepts_utf8_bom_and_asymmetric_domain(self):
        rows = identity_cube()
        rows[2:2] = ["DOMAIN_MIN -1 -2 -3", "DOMAIN_MAX 1 2 3"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "grade.cube")
            path.write_text("\n".join(rows), encoding="utf-8-sig")
            lut = CubeLut.load(path)
        self.assertEqual(lut.domain_min, (-1, -2, -3))
        self.assertEqual(lut.domain_max, (1, 2, 3))

    def test_rejects_malformed_cube(self):
        rows = identity_cube()
        invalid = [
            rows[:-1], rows + ["1 1 1"], rows[:-1] + ["1 1 nan"],
            ["LUT_1D_SIZE 2", *rows], ["DOMAIN_MAX 0 1 1", *rows],
            ["LUT_3D_SIZE 10000", *rows],
        ]
        for cube in invalid:
            with self.subTest(cube=cube), self.assertRaises(ValueError):
                CubeLut.parse(cube)


class CudaFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cp = cuda()
        except RuntimeError as error:
            if os.getenv("CONCAT_REQUIRE_CUDA") == "1":
                raise
            raise unittest.SkipTest(str(error)) from error

    def test_color_pipeline_preserves_alpha_and_input(self):
        cp = self.cp
        frame = cp.array([[[0.25, 0.25, 0.25, 0.4]]], dtype=cp.float32)
        original = frame.copy()
        result = apply(frame, FilterSettings(brightness=0.1, contrast=0.5,
                                             exposure=1, temperature=0.4))
        cp.testing.assert_allclose(result, cp.array([[[0.66, 0.6, 0.54, 0.4]]]),
                                   atol=1e-6)
        cp.testing.assert_array_equal(frame, original)
        self.assertIs(apply(frame), frame)

    def test_saturation_zero_uses_luminance(self):
        cp = self.cp
        frame = cp.array([[[1, 0, 0, 1]]], dtype=cp.float32)
        result = apply(frame, FilterSettings(saturation=0))
        cp.testing.assert_allclose(result, cp.array([[[0.2126, 0.2126, 0.2126, 1]]]),
                                   atol=1e-6)

    def test_alpha_weighted_blur_and_sharpen(self):
        cp = self.cp
        frame = cp.array([[[1, 0, 0, 0], [0, 0, 0.6, 1], [0, 0, 0, 1]]],
                         dtype=cp.float32)
        blurred = apply(frame, FilterSettings(blur_radius=1))
        self.assertAlmostEqual(float(blurred[0, 1, 2]), 0.3, places=5)
        self.assertEqual(float(blurred[0, 1, 0]), 0.0)
        cp.testing.assert_array_equal(blurred[..., 3], frame[..., 3])
        sharpened = apply(frame, FilterSettings(sharpen=1))
        self.assertAlmostEqual(float(sharpened[0, 1, 2]), 0.9, places=5)

    def test_trilinear_identity_with_asymmetric_channels_and_strides(self):
        cp = self.cp
        lut = CubeLut.parse(identity_cube()).upload()
        frame = cp.array([[[0.13, 0.46, 0.71, 0.5], [0.9, 0.25, 0, 1]]],
                         dtype=cp.float32)[:, ::-1]
        cp.testing.assert_allclose(apply(frame, lut=lut), frame, atol=1e-6)


if __name__ == "__main__":
    unittest.main()
