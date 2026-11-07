"""CPU-only Stage 1 contract tests; never claim this proves Salad NVENC."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_render.concat.gpu.simple_timeline_v1 import GPUPlanError, command_for


class StageOneContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("photo.jpg", "clip.mp4", "voice.mp3"):
            (self.root / name).write_bytes(b"fixture")
        self.timeline = self.root / "timeline.json"
        self.output = self.root / "output.mp4"
        self.plan = {"version": 1, "width": 1920, "height": 1080, "fps": 30,
                     "clips": [{"path": "photo.jpg", "kind": "image", "duration": 2},
                               {"path": "clip.mp4", "kind": "video", "duration": 3}],
                     "voice": "voice.mp3"}

    def fake_probe(self, ffprobe, path):
        if path.suffix == ".jpg":
            return [{"codec_type": "video", "codec_name": "mjpeg",
                     "width": 1920, "height": 1080}]
        if path.suffix == ".mp4":
            return [{"codec_type": "video", "codec_name": "h264",
                     "width": 1920, "height": 1080,
                     "avg_frame_rate": "30/1", "r_frame_rate": "30/1"}]
        return [{"codec_type": "audio", "codec_name": "mp3"}]

    def build(self):
        self.timeline.write_text(json.dumps(self.plan))
        with patch("cloud_render.concat.gpu.simple_timeline_v1.probe",
                   side_effect=self.fake_probe):
            return command_for(self.root, self.timeline, self.output)

    def test_gpu_cut_and_voice_command(self):
        args, _, _, seconds = self.build()
        self.assertEqual(seconds, 5)
        graph = args[args.index("-filter_complex") + 1]
        self.assertEqual(graph.count("scale_cuda="), 2)
        self.assertIn("concat=n=2:v=1:a=0", graph)
        self.assertIn("h264_nvenc", args)
        self.assertIn("mjpeg_cuvid", args)
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        for cpu_filter in ("zoompan", "xfade", "subtitles", "drawtext",
                           "hwdownload", "scale=", "overlay=", "format=rgba"):
            self.assertNotIn(cpu_filter, graph)

    def test_unsupported_effect_rejected(self):
        self.plan["clips"][0]["transition"] = "wipe"
        with self.assertRaisesRegex(GPUPlanError, "unsupported"):
            self.build()

    def test_missing_media_rejected(self):
        self.plan["clips"][0]["path"] = "missing.jpg"
        with self.assertRaises(FileNotFoundError):
            self.build()

    def test_path_traversal_rejected(self):
        self.plan["clips"][0]["path"] = "../other.jpg"
        with self.assertRaises((GPUPlanError, FileNotFoundError)):
            self.build()

    def test_invalid_frame_duration_rejected(self):
        self.plan["clips"][0]["duration"] = 1.01
        with self.assertRaisesRegex(GPUPlanError, "align"):
            self.build()

    def test_wrong_aspect_ratio_rejected(self):
        with patch("cloud_render.concat.gpu.simple_timeline_v1.probe",
                   return_value=[{"codec_type": "video", "codec_name": "mjpeg",
                                  "width": 1080, "height": 1920}]):
            self.timeline.write_text(json.dumps(self.plan))
            with self.assertRaisesRegex(GPUPlanError, "16:9"):
                command_for(self.root, self.timeline, self.output)

    def test_preserves_existing_output(self):
        self.output.write_bytes(b"original")
        with self.assertRaisesRegex(GPUPlanError, "must be new"):
            self.build()
        self.assertEqual(self.output.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
