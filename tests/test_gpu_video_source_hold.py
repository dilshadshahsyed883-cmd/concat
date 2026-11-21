"""Source exhaustion holds the final GPU frame without shortening the timeline."""
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace as Item
import unittest
from unittest.mock import patch

from cloud_render.concat.hybrid.render import Sources


class Decoder:
    def __init__(self, count):
        self.count = count
        self.reads = []
    def __len__(self):
        return self.count
    def frame(self, index):
        if not 0 <= index < self.count:
            raise AssertionError("Out-of-range decoder request")
        self.reads.append(index)
        return index


def clip(*, ident="c24", first=2371, end=2456, source_start=.007784, speed=1.0):
    return Item(id=ident, path=Path("/media/source.mp4"),
                span=Item(start=first, end=end), source_start=source_start, speed=speed)


class VideoSourceHoldTests(unittest.TestCase):
    def source(self, count=55, rate=Fraction(25)):
        source = Sources(Path("/unused"))
        decoder = Decoder(count)
        source.decoders[Path("/media/source.mp4")] = decoder
        source.rates[Path("/media/source.mp4")] = rate
        return source, decoder

    def test_s3_clip_c24_keeps_all_85_timeline_frames_and_holds_frame_54(self):
        source, decoder = self.source()
        item = clip()
        with patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame):
            with self.assertLogs("cloud_render.concat.hybrid.render", level="WARNING") as logs:
                frames = [source.video(item, i) for i in range(item.span.start, item.span.end)]
        self.assertEqual(len(frames), 85)
        self.assertEqual(frames[0], 0)
        self.assertEqual(frames[-1], 54)
        self.assertTrue(all(frame == 54 for frame in frames[66:]))
        self.assertEqual(decoder.reads.count(54), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("clip=c24", logs.output[0])
        self.assertIn("available_frames=55", logs.output[0])
        self.assertIn("available_seconds=2.200000", logs.output[0])

    def test_mismatch_is_logged_at_first_frame_before_the_source_runs_out(self):
        source, _ = self.source()
        with patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame):
            with self.assertLogs("cloud_render.concat.hybrid.render", level="WARNING") as logs:
                self.assertEqual(source.video(clip(), 2371), 0)
        self.assertIn("Holding last video frame", logs.output[0])

    def test_valid_25fps_mapping_and_repeated_frame_cache_are_unchanged(self):
        source, decoder = self.source(100)
        item = clip(first=0, end=30, source_start=0)
        with patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame):
            with self.assertNoLogs("cloud_render.concat.hybrid.render", level="WARNING"):
                frames = [source.video(item, i) for i in range(30)]
        self.assertEqual(frames, [int(i * 25 / 30 + 1e-7) for i in range(30)])
        self.assertEqual(len(decoder.reads), len(set(frames)))

    def test_speed_and_trim_exhaustion_still_preserve_requested_output_length(self):
        source, _ = self.source()
        item = clip(first=0, end=30, source_start=1.2, speed=2)
        with patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame):
            with self.assertLogs("cloud_render.concat.hybrid.render", level="WARNING"):
                frames = [source.video(item, i) for i in range(30)]
        self.assertEqual(frames[0], 30)
        self.assertEqual(frames[-1], 54)
        self.assertEqual(len(frames), 30)

    def test_source_start_beyond_eof_holds_one_real_frame_for_the_whole_clip(self):
        source, decoder = self.source()
        item = clip(first=0, end=30, source_start=3)
        with patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame):
            with self.assertLogs("cloud_render.concat.hybrid.render", level="WARNING"):
                self.assertEqual([source.video(item, i) for i in range(30)], [54]*30)
        self.assertEqual(decoder.reads, [54])

    def test_transition_tail_preserves_existing_last_frame_hold(self):
        source, decoder = self.source()
        with patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame):
            with self.assertNoLogs("cloud_render.concat.hybrid.render", level="WARNING"):
                self.assertEqual(source.video(clip(), 3000, tail=True), 54)
                self.assertEqual(source.video(clip(), 3001, tail=True), 54)
        self.assertEqual(decoder.reads, [54])

    def test_one_frame_video_can_fill_a_longer_clip(self):
        source, decoder = self.source(1)
        item = clip(first=0, end=90, source_start=0)
        with patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame):
            with self.assertLogs("cloud_render.concat.hybrid.render", level="WARNING"):
                self.assertEqual([source.video(item, i) for i in range(90)], [0]*90)
        self.assertEqual(decoder.reads, [0])

    def test_zero_decodable_frames_fail_instead_of_requesting_frame_minus_one(self):
        for tail in (False, True):
            with self.subTest(tail=tail):
                source, decoder = self.source(0)
                with self.assertRaisesRegex(ValueError, "no decodable frames"):
                    source.video(clip(), 2371, tail=tail)
                self.assertEqual(decoder.reads, [])

    def test_each_short_clip_is_logged_once_even_when_the_source_is_shared(self):
        source, _ = self.source()
        with patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame):
            with self.assertLogs("cloud_render.concat.hybrid.render", level="WARNING") as logs:
                for ident in ("c24", "other"):
                    item = clip(ident=ident)
                    for i in range(item.span.start, item.span.end):
                        source.video(item, i)
        self.assertEqual(len(logs.output), 2)


if __name__ == "__main__":
    unittest.main()
