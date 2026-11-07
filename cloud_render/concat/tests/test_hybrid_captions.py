import unittest
from fractions import Fraction

from cloud_render.concat.hybrid.captions import (
    CaptionRenderer, CaptionStyle, Cue, WordTiming, _glyph_texture, parse_srt,
)
from cloud_render.concat.hybrid.contract import FrameRate, cuda


SRT = (
    "\ufeff2\r\n00:00:01,200 --> 00:00:02,000\r\nLast line\r\n"
    "\r\n1\r\n00:00:00,500 --> 00:00:01,500\r\nSAME\r\nSAME\r\n"
)
WORDS = (WordTiming("SAME", 500, 800), WordTiming("SAME", 900, 1200))


class CaptionTimingTests(unittest.TestCase):
    def test_parses_multiline_and_sorts_overlapping_cues(self):
        first, last = parse_srt(SRT, {1: WORDS})
        self.assertEqual((first.number, first.text, first.words), (1, "SAME\nSAME", WORDS))
        self.assertEqual((last.number, last.start_ms, last.end_ms), (2, 1200, 2000))
        renderer = CaptionRenderer((last, first))
        rate = FrameRate(10)
        self.assertEqual(renderer.active(4, rate), ())
        self.assertEqual(renderer.active(5, rate), ((first, 0),))
        self.assertEqual(renderer.active(8, rate), ((first, None),))
        self.assertEqual(renderer.active(9, rate), ((first, 1),))
        self.assertEqual(renderer.active(12, rate), ((first, None), (last, None)))
        self.assertEqual(renderer.active(15, rate), ((last, None),))
        self.assertEqual(renderer.active(20, rate), ())

    def test_fractional_frame_rate_never_rounds_early(self):
        cue = Cue(1, 100, 101, "A")
        renderer = CaptionRenderer((cue,))
        rate = FrameRate(30000, 1001)
        self.assertEqual(rate.seconds(3), Fraction(1001, 10000))
        self.assertEqual(renderer.active(2, rate), ())
        self.assertEqual(renderer.active(3, rate), ((cue, None),))
        self.assertEqual(renderer.active(4, rate), ())

    def test_rejects_invalid_srt_and_invalid_word_timing(self):
        for bad in ("x", "1\n00:00:60,000 --> 00:01:10,000\nA",
                    "1\n00:00:01,000 --> 00:00:00,000\nA"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_srt(bad)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            parse_srt(SRT.replace("\r\n1\r\n", "\r\n2\r\n"))
        with self.assertRaisesRegex(ValueError, "unknown"):
            parse_srt(SRT, {3: WORDS})
        with self.assertRaisesRegex(ValueError, "match"):
            parse_srt(SRT, {1: (WordTiming("OTHER", 500, 800), WORDS[1])})
        with self.assertRaisesRegex(ValueError, "overlap"):
            parse_srt(SRT, {1: (WordTiming("SAME", 500, 950), WORDS[1])})
        self.assertEqual(parse_srt(" \n"), ())

    def test_glyphs_highlight_the_requested_repeated_word(self):
        cue = Cue(1, 500, 1500, "SAME SAME", WORDS)
        style = CaptionStyle(font_size=18, background=(0, 0, 0, 127),
                             highlight=(255, 0, 0, 255), margin=8)
        first = _glyph_texture(cue, 0, style, 256, 80)
        second = _glyph_texture(cue, 1, style, 256, 80)
        self.assertEqual(first.getpixel((0, 0)), style.background)
        for image, left in ((first, True), (second, False)):
            red = [(x, y) for y in range(image.height) for x in range(image.width)
                   if image.getpixel((x, y)) == style.highlight]
            self.assertTrue(red)
            self.assertTrue(all((x < image.width / 2) == left for x, _ in red))
        self.assertIn(style.outline, tuple(first.getpixel((x, y)) for y in range(first.height) for x in range(first.width)))

    def test_wraps_text_and_validates_style(self):
        cue = Cue(1, 0, 1000, "ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT")
        style = CaptionStyle(font_size=14, margin=6, padding=4)
        compact = _glyph_texture(cue, None, style, 100, 180)
        wide = _glyph_texture(cue, None, style, 500, 180)
        self.assertGreater(compact.height, wide.height)
        with self.assertRaisesRegex(ValueError, "narrow"):
            _glyph_texture(cue, None, style, 12, 180)
        with self.assertRaisesRegex(ValueError, "RGBA"):
            CaptionStyle(fill=(256, 0, 0, 255))


class CaptionCudaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cp = cuda()
        except RuntimeError as error:
            raise unittest.SkipTest(str(error)) from error

    def test_cuda_alpha_blend_cache_and_frame_timing(self):
        cp = self.cp
        frame = cp.zeros((90, 160, 4), dtype=cp.float32)
        frame[..., 2:4] = .5
        cue = Cue(1, 100, 300, "Visible")
        style = CaptionStyle(font_size=14, background=(255, 0, 0, 128),
                             margin=6, padding=4)
        renderer = CaptionRenderer((cue,), style)
        self.assertIs(renderer.apply(frame, 0, FrameRate(10)), frame)
        output = renderer.apply(frame, 1, FrameRate(10))
        self.assertIsInstance(output, cp.ndarray)
        self.assertEqual(output.device.id, frame.device.id)
        tile = next(iter(renderer._textures.values()))
        y = frame.shape[0] - style.margin - tile.shape[0]
        x = (frame.shape[1] - tile.shape[1]) // 2
        alpha = 128 / 255
        expected_alpha = alpha + .5 * (1 - alpha)
        cp.testing.assert_allclose(output[y, x], cp.asarray(
            [alpha / expected_alpha, 0, .25 * (1 - alpha) / expected_alpha, expected_alpha]))
        cp.testing.assert_array_equal(frame[y, x], cp.asarray([0, 0, .5, .5]))
        repeated = renderer.apply(frame, 2, FrameRate(10))
        cp.testing.assert_array_equal(output, repeated)
        self.assertIs(next(iter(renderer._textures.values())), tile)
        self.assertIs(renderer.apply(frame, 3, FrameRate(10)), frame)

    def test_accepts_only_gpu_rgba(self):
        cp = self.cp
        renderer = CaptionRenderer(())
        with self.assertRaises(TypeError):
            renderer.apply([[[]]], 0, FrameRate(10))
        with self.assertRaises(ValueError):
            renderer.apply(cp.empty((4, 4, 3), dtype=cp.float32), 0, FrameRate(10))
