"""Regressions for the GPU caption audio decoder and default small model.

These source/contract tests run on CPU CI. The published image additionally
performs a real faster-whisper WAV decode in tools/release_image_smoke.py.
"""
from pathlib import Path
import unittest

from cloud_render.concat.gpu.caption_jobs import validate_request


ROOT = Path(__file__).resolve().parents[3]


class AsrPyavCompatibilityTests(unittest.TestCase):
    def test_pin_matches_faster_whisper_metadata_errors_compatibility(self):
        path = ROOT / "cloud_render/concat/hybrid/requirements-asr.txt"
        dependencies = [line.strip() for line in path.read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]
        self.assertIn("faster-whisper==1.2.1", dependencies)
        self.assertEqual(
            [line for line in dependencies if line.startswith(("av==", "av<", "av>", "av~="))],
            ["av==18.1.0"],
            "Do not let PyAV 19+ break faster-whisper audio decoding",
        )

    def test_small_remains_default_and_preloaded(self):
        request = validate_request({"version": 1, "sourceClipId": "narration"})
        self.assertEqual(request["model"], "small")
        dockerfile = (ROOT / "cloud_render/concat/gpu/Dockerfile").read_text()
        self.assertIn("download_model('small'", dockerfile)

    def test_published_image_must_decode_real_wav_before_push(self):
        smoke = (ROOT / "tools/release_image_smoke.py").read_text()
        self.assertIn("from faster_whisper.audio import decode_audio", smoke)
        self.assertIn("samples = decode_audio(", smoke)
        self.assertIn("versions['av'] == '18.1.0'", smoke)
        self.assertIn("'asr_decoder_smoke': 'passed'", smoke)


if __name__ == "__main__":
    unittest.main()
