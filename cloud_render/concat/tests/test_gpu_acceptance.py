import shutil
import tempfile
import unittest
from pathlib import Path

from cloud_render.concat.gpu.acceptance import ffmpeg, generate_inputs, inspect_cut_output


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Needs FFmpeg tooling")
class AcceptanceInspectorTests(unittest.TestCase):
    def test_inspector_verifies_picture_and_audio_timing(self):
        # Software encoding here supplies a known reference for the inspector.
        # The acceptance CLI exclusively renders through the CUDA worker.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generate_inputs(root)
            output = root / "known-reference.mp4"
            ffmpeg("-i", str(root / "red.mp4"), "-i", str(root / "blue.mp4"),
                   "-i", str(root / "voice.wav"), "-filter_complex",
                   "[0:v][1:v]concat=n=2:v=1:a=0,scale=1920:1080[v]",
                   "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset",
                   "ultrafast", "-threads", "2", "-c:a", "aac", str(output))
            self.assertEqual(inspect_cut_output(output)["frames"], 60)
            reversed_output = root / "wrong-order.mp4"
            ffmpeg("-i", str(root / "blue.mp4"), "-i", str(root / "red.mp4"),
                   "-i", str(root / "voice.wav"), "-filter_complex",
                   "[0:v][1:v]concat=n=2:v=1:a=0,scale=1920:1080[v]",
                   "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset",
                   "ultrafast", "-threads", "2", "-c:a", "aac", str(reversed_output))
            with self.assertRaisesRegex(ValueError, "pictures"):
                inspect_cut_output(reversed_output)
