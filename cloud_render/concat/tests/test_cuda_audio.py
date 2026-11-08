"""Audio GPU mixer contract: CPU-only tests inject NumPy, never a production fallback."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import numpy as np
except ImportError:
    np = None

from cloud_render.concat.hybrid.cuda_audio import (
    FRAME_SAMPLES, decoder_command, mixed_pcm, mux_cuda_audio,
)


class FakeGpu:
    """NumPy API substitute used exclusively in tests without NVIDIA hardware."""
    if np is not None:
        float32 = np.float32
        zeros = staticmethod(np.zeros)
        arange = staticmethod(np.arange)
        full = staticmethod(np.full)
        clip = staticmethod(np.clip)
        asarray = staticmethod(np.asarray)
        asnumpy = staticmethod(np.asarray)


@unittest.skipUnless(np is not None and shutil.which("ffmpeg"), "FFmpeg/NumPy required")
class CudaAudioContractTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "tone.wav"
        with wave.open(str(self.path), "wb") as wav:
            wav.setframerate(48_000)
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.writeframes((np.ones((48_000, 2), dtype=np.int16) * 8192).tobytes())

    def sound(self, **fields):
        values = dict(path=self.path, start=0, source_start=0., seconds=.4,
                      speed=1., stream=None, volume=.5, fade_in=0., fade_out=0.)
        values.update(fields)
        return SimpleNamespace(**values)

    def test_gain_fade_and_frame_aligned_delay(self):
        sound = self.sound(start=3, seconds=.2, fade_in=.1, fade_out=.1)
        pcm = np.frombuffer(b"".join(mixed_pcm([sound], 30, backend=FakeGpu,
                                                 block_samples=7000)), dtype="<f4")
        pcm = pcm.reshape((-1, 2))
        self.assertEqual(pcm.shape, (48_000, 2))
        self.assertTrue(np.all(pcm[:3*FRAME_SAMPLES] == 0))
        self.assertAlmostEqual(float(pcm[3*FRAME_SAMPLES, 0]), 0, places=3)
        self.assertAlmostEqual(float(pcm[3*FRAME_SAMPLES+4800, 0]), .125, delta=.003)
        self.assertAlmostEqual(float(pcm[3*FRAME_SAMPLES+9599, 0]), 0, delta=.003)
        self.assertTrue(np.all(pcm[3*FRAME_SAMPLES+9600:] == 0))

    def test_additive_mix_is_independent_of_chunk_boundary(self):
        sound = self.sound(volume=.4)
        small = np.frombuffer(b"".join(mixed_pcm([sound, sound], 30,
                          backend=FakeGpu, block_samples=900)), dtype="<f4")
        large = np.frombuffer(b"".join(mixed_pcm([sound, sound], 30,
                          backend=FakeGpu, block_samples=4096)), dtype="<f4")
        self.assertTrue(np.array_equal(small, large))
        self.assertAlmostEqual(float(small[0]), .2, delta=.002)

    def test_bad_source_fails_without_silent_audio_loss(self):
        sound = self.sound(path=Path(self.folder.name)/"missing.wav")
        with self.assertRaisesRegex(RuntimeError, "Audio decode failed"):
            list(mixed_pcm([sound], 30, backend=FakeGpu))

    def test_decode_keeps_pitch_preserving_tempo_and_selected_stream(self):
        cmd = decoder_command(self.sound(stream=2, speed=4.0, source_start=1.0))
        self.assertIn("0:2", cmd)
        self.assertIn("-ss", cmd)
        self.assertIn("atempo=2.000000000", " ".join(cmd))
        self.assertNotIn("amix", " ".join(cmd))

    def test_cuda_unavailable_does_not_fall_back_to_cpu_mixing(self):
        with patch("cloud_render.concat.hybrid.cuda_audio.cuda",
                   side_effect=RuntimeError("CUDA unavailable")):
            with self.assertRaisesRegex(RuntimeError, "CUDA unavailable"):
                list(mixed_pcm([self.sound()], 30))

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe required")
    def test_video_unchanged_and_audio_aac_mux_timestamp(self):
        raw = Path(self.folder.name) / "source.h264"
        result = Path(self.folder.name) / "result.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                        "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=30",
                        "-frames:v", "30", "-an", "-c:v", "libx264",
                        "-preset", "ultrafast", "-bf", "0", "-pix_fmt", "yuv420p",
                        "-f", "h264", "-y", str(raw)], check=True,
                       capture_output=True, timeout=60)
        mux_cuda_audio(raw, result, [self.sound(seconds=1.)], 30, backend=FakeGpu)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams",
                                "-of", "json", str(result)], check=True,
                               capture_output=True, text=True, timeout=20)
        streams = json.loads(probe.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next(s for s in streams if s["codec_type"] == "audio")
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(video["avg_frame_rate"], "30/1")
        self.assertEqual(video["nb_frames"], "30")
        self.assertEqual(audio["codec_name"], "aac")
        self.assertLess(abs(float(video["start_time"])-float(audio["start_time"])), 1/30)
        self.assertLess(abs(float(audio["duration"])-1), 1/30)


if __name__ == "__main__":
    unittest.main()
