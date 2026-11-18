"""Offline ASR image packaging contracts; no Hugging Face downloads in unit tests."""
from __future__ import annotations

import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_render.concat.gpu import asr_models

ROOT = Path(__file__).resolve().parents[1]
GPU = ROOT / "cloud_render" / "concat" / "gpu"


class BundledAsrTests(unittest.TestCase):
    def test_image_preloads_every_selectable_gpu_caption_model(self):
        source = ast.parse((GPU / "caption_jobs.py").read_text(encoding="utf-8"))
        supported = next(ast.literal_eval(item.value) for item in source.body
                         if isinstance(item, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "MODELS"
                                 for target in item.targets))
        self.assertEqual(set(asr_models.BUNDLED_MODELS), supported)
        self.assertEqual(len(asr_models.BUNDLED_MODELS), len(supported))

    def test_offline_verification_never_downloads_model(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "cached"
            base.mkdir()
            for name in asr_models.REQUIRED_FILES:
                (base / name).write_bytes(b"fixture")
            calls = []
            def fake_download(model, *, cache_dir, local_files_only):
                calls.append((model, cache_dir, local_files_only))
                return str(base)
            module = types.ModuleType("faster_whisper")
            module.download_model = fake_download
            with patch.dict(sys.modules, {"faster_whisper": module}):
                for model in asr_models.BUNDLED_MODELS:
                    record = asr_models.check_model(model, Path(temp), offline=True)
                    self.assertGreater(record["weights_bytes"], 0)
            self.assertEqual([call[0] for call in calls], list(asr_models.BUNDLED_MODELS))
            self.assertTrue(all(call[2] for call in calls))

    def test_missing_weight_or_tokenizer_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "config.json").write_bytes(b"{}")
            module = types.ModuleType("faster_whisper")
            module.download_model = lambda *a, **kw: str(base)
            with patch.dict(sys.modules, {"faster_whisper": module}):
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    asr_models.check_model("small", base, offline=True)

    def test_dockerfile_pins_pyav_and_forbids_runtime_downloads(self):
        dockerfile = (GPU / "Dockerfile").read_text(encoding="utf-8")
        requirements = (ROOT / "cloud_render/concat/hybrid/requirements-asr.txt").read_text()
        runtime = (GPU / "caption_jobs.py").read_text(encoding="utf-8")
        self.assertIn("av==18.1.0", requirements)
        self.assertIn("--mode download", dockerfile)
        self.assertIn("--mode verify", dockerfile)
        self.assertIn("HF_HUB_OFFLINE=1", dockerfile)
        self.assertIn("local_files_only=True", runtime)

    def test_decoder_compatibility_error_is_a_build_failure(self):
        module = types.ModuleType("faster_whisper")
        audio = types.ModuleType("faster_whisper.audio")
        audio.decode_audio = lambda *a, **kw: (_ for _ in ()).throw(
            TypeError("open() got an unexpected keyword argument 'metadata_errors'"))
        with patch.dict(sys.modules, {"faster_whisper": module, "faster_whisper.audio": audio}):
            with self.assertRaisesRegex(TypeError, "metadata_errors"):
                asr_models.verify_audio_decoder()


if __name__ == "__main__":
    unittest.main()
