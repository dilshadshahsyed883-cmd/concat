"""Regression tests for keeping timeline length when a CFR video source ends.

These tests patch NVDEC and fitting so they run without a GPU; the renderer
still uses the actual NVDEC frame count and performs normal compositing on GPU.
"""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cloud_render.concat.hybrid.contract import FrameSpan
from cloud_render.concat.hybrid.render import Sources


class FakeDecoder:
    def __init__(self, length):
        self.frames = list(range(length))
        self.requested = []

    def __len__(self):
        return len(self.frames)

    def frame(self, index):
        self.requested.append(index)
        return self.frames[index]


class LastFrameHoldTests(unittest.TestCase):
    def make_clip(self, *, span=(0, 90), start=0.0, speed=1.0):
        return SimpleNamespace(
            id="clip-1", path=Path("/media/short.mp4"),
            span=FrameSpan(*span), source_start=start, speed=speed)

    def run_frames(self, length, indices, *, clip=None, source_fps=30):
        source = Sources(Path("/tmp"))
        decoder = FakeDecoder(length)
        info = json.dumps({"streams": [{
            "avg_frame_rate": f"{source_fps}/1",
            "r_frame_rate": f"{source_fps}/1"}]})
        clip = clip or self.make_clip()
        with (patch("cloud_render.concat.hybrid.render.invoke", return_value=info),
              patch("cloud_render.concat.hybrid.render.Nvdec", return_value=decoder),
              patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda frame: frame),
              patch("cloud_render.concat.hybrid.render.LOG.warning") as warning):
            output = [source.video(clip, frame, tail=(frame >= clip.span.end))
                      for frame in indices]
        return output, decoder.requested, warning.call_count

    def test_normal_clip_holds_last_actual_frame_instead_of_crashing(self):
        # A 1-second, 30-frame video is used for a 3-second timeline.
        # Later timeline frames reuse frame 29, not nonexistent frames.
        output, requested, warnings = self.run_frames(
            30, [0, 28, 29, 30, 45, 89])
        self.assertEqual(output, [0, 28, 29, 29, 29, 29])
        self.assertEqual(requested, [0, 28, 29])
        self.assertEqual(warnings, 1)

    def test_transition_tail_holds_frame_when_beyond_clip_end(self):
        output, requested, warnings = self.run_frames(
            30, [29, 91, 94])
        self.assertEqual(output, [29, 29, 29])
        self.assertEqual(requested, [29])
        self.assertEqual(warnings, 1)

    def test_trim_and_speed_still_clamp_to_final_frame(self):
        clip = self.make_clip(span=(0, 30), start=0.2, speed=2.0)
        output, requested, warnings = self.run_frames(
            15, [0, 3, 5, 20, 29], clip=clip)
        self.assertEqual(output, [6, 12, 14, 14, 14])
        self.assertEqual(requested, [6, 12, 14])
        self.assertEqual(warnings, 1)

    def test_short_24fps_video_in_30fps_timeline(self):
        output, requested, warnings = self.run_frames(
            5, [0, 3, 5, 6, 7, 10], source_fps=24)
        self.assertEqual(output, [0, 2, 4, 4, 4, 4])
        self.assertEqual(requested, [0, 2, 4])
        self.assertEqual(warnings, 1)

    def test_empty_video_is_rejected_before_negative_frame_index(self):
        with self.assertRaisesRegex(ValueError, "video has no decodable frames"):
            self.run_frames(0, [0])

    def test_fully_available_video_is_not_held(self):
        output, requested, warnings = self.run_frames(
            90, [0, 30, 89])
        self.assertEqual(output, [0, 30, 89])
        self.assertEqual(requested, [0, 30, 89])
        self.assertEqual(warnings, 0)


if __name__ == "__main__":
    unittest.main()
