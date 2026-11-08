"""CPU-only verification for opt-in GPU stage timing and source-frame reuse."""
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from cloud_render.concat.hybrid.performance import StageProfiler
from cloud_render.concat.hybrid.render import Sources


class StageProfilerTests(TestCase):
    def test_opt_in_sampling_never_synchronizes_during_normal_render(self):
        synchronize = Mock()
        profiler = StageProfiler(synchronize=synchronize, sample_every=2)
        with profiler.stage("decode", 1):
            pass
        self.assertEqual(profiler.summary(), {})
        synchronize.assert_not_called()

    def test_sampling_is_bounded_and_reports_actual_stages(self):
        synchronize = Mock()
        profiler = StageProfiler(True, synchronize=synchronize, sample_every=2)
        for frame in range(6):
            with profiler.stage("decode", frame):
                pass
        profiler.count("cache_hit")
        report = profiler.summary()
        self.assertEqual(report["stage_ms"]["decode"]["samples"], 3)
        self.assertEqual(synchronize.call_count, 6)
        self.assertEqual(report["counters"]["cache_hit"], 1)
        self.assertIn("diagnostic-only", report["mode"])

    def test_uncaught_error_does_not_hide_stage_time(self):
        profiler = StageProfiler(True)
        with self.assertRaisesRegex(ValueError, "bad input"):
            with profiler.stage("validate"):
                raise ValueError("bad input")
        self.assertEqual(profiler.summary()["stage_ms"]["validate"]["samples"], 1)

    def test_bad_sample_interval_is_rejected(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StageProfiler(sample_every=value)


class SourceFrameCacheTests(TestCase):
    def setUp(self):
        self.source = Path("video.mp4")
        self.clip = SimpleNamespace(path=self.source,
                                    span=SimpleNamespace(start=0),
                                    speed=1.0, source_start=0.0)
        self.decoder = Mock()
        self.decoder.__len__ = Mock(return_value=240)
        self.decoder.frame.side_effect = lambda number: "frame:" + str(number)

    def test_24fps_on_30fps_timeline_reuses_exact_decoded_frame(self):
        profiler = StageProfiler(True)
        with (patch("cloud_render.concat.hybrid.render.Nvdec", return_value=self.decoder),
              patch("cloud_render.concat.hybrid.render.invoke",
                    return_value='{"streams":[{"avg_frame_rate":"24/1","r_frame_rate":"24/1"}]}'),
              patch("cloud_render.concat.hybrid.render.fitted",
                    side_effect=lambda frame: "fit-" + frame) as fit):
            source = Sources(Path("."), profiler=profiler)
            results = [source.video(self.clip, i) for i in range(5)]
        self.assertEqual(results, ["fit-frame:0", "fit-frame:0",
                                   "fit-frame:1", "fit-frame:2", "fit-frame:3"])
        self.assertEqual(self.decoder.frame.call_count, 4)
        self.assertEqual(fit.call_count, 4)
        self.assertEqual(profiler.summary()["counters"]["video_frame_cache_hit"], 1)

    def test_two_clip_uses_of_same_frame_hit_cache(self):
        with (patch("cloud_render.concat.hybrid.render.Nvdec", return_value=self.decoder),
              patch("cloud_render.concat.hybrid.render.invoke",
                    return_value='{"streams":[{"avg_frame_rate":"30/1","r_frame_rate":"30/1"}]}'),
              patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda x: x)):
            source = Sources(Path("."))
            self.assertEqual(source.video(self.clip, 8), source.video(self.clip, 8))
            self.decoder.frame.assert_called_once_with(8)

    def test_cache_has_fixed_capacity_for_large_timelines(self):
        with (patch("cloud_render.concat.hybrid.render.Nvdec", return_value=self.decoder),
              patch("cloud_render.concat.hybrid.render.invoke",
                    return_value='{"streams":[{"avg_frame_rate":"30/1","r_frame_rate":"30/1"}]}'),
              patch("cloud_render.concat.hybrid.render.fitted", side_effect=lambda x: x)):
            source = Sources(Path("."))
            for i in range(100):
                source.video(self.clip, i)
            self.assertLessEqual(len(source.video_frames), 2)
            source.video(self.clip, 0)
            self.assertEqual(self.decoder.frame.call_count, 101)
