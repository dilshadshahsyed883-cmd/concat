"""Regression for timestamping the GPU encoder's raw H.264 bytestream.

No NVIDIA device is required; FFmpeg produces a short CPU test fixture only.
The production renderer still uses NVDEC/CUDA/NVENC for video frames.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_render.concat.hybrid.render import mux, verify


class MuxVerificationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "FFmpeg and ffprobe are required")
    def test_raw_h264_mux_has_exact_30fps_and_frame_count(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            elementary = root / "source.h264"
            output = root / "output.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=30",
                "-frames:v", "30", "-an", "-c:v", "libx264", "-preset",
                "ultrafast", "-bf", "0", "-pix_fmt", "yuv420p",
                "-f", "h264", "-y", str(elementary),
            ], check=True, capture_output=True, timeout=60)
            mux(elementary, output, [], 30)
            verify(output, 30, False)
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,avg_frame_rate,nb_frames",
                "-of", "json", str(output),
            ], check=True, capture_output=True, text=True, timeout=20)
            video = json.loads(probe.stdout)["streams"][0]
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual(video["avg_frame_rate"], "30/1")
            self.assertEqual(video["nb_frames"], "30")

    def test_bad_mp4_reports_expected_and_observed_fields(self):
        streams = {"streams": [{"codec_type": "video", "codec_name": "h264",
                                "width": 1920, "height": 1080,
                                "avg_frame_rate": "2700000/89999",
                                "nb_frames": "1080", "duration": "35.999978"}]}
        with patch("cloud_render.concat.hybrid.render.invoke",
                   return_value=json.dumps(streams)):
            with self.assertRaisesRegex(RuntimeError, "2700000/89999") as error:
                verify(Path("not-actually-opened.mp4"), 1080, False)
            self.assertIn("30/1", str(error.exception))


if __name__ == "__main__":
    unittest.main()
