"""Build-time preload and offline verification for Concat's supported GPU ASR models.

Model files are downloaded into the Docker image, not committed into Git.
This utility never runs a transcription on the CPU; its decode test exercises
the PyAV compatibility path without requiring GPU hardware.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import wave
from pathlib import Path

# Keep in sync with caption_jobs.MODELS. All supported sizes are image-bundled.
BUNDLED_MODELS = ("tiny", "base", "small", "medium", "large-v3", "turbo")
REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")


def check_model(model: str, cache: Path, *, offline: bool) -> dict:
    from faster_whisper import download_model

    directory = Path(download_model(model, cache_dir=str(cache), local_files_only=offline))
    missing = [name for name in REQUIRED_FILES
               if not (directory / name).is_file() or (directory / name).stat().st_size == 0]
    if missing:
        raise RuntimeError(f"ASR model {model} is incomplete: {', '.join(missing)}")
    return {
        "model": model,
        "snapshot": directory.name,
        "weights_bytes": (directory / "model.bin").stat().st_size,
    }


def verify_audio_decoder() -> None:
    """Catch faster-whisper 1.2.1 / PyAV 19's metadata_errors failure."""
    from faster_whisper.audio import decode_audio

    with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
        with wave.open(audio.name, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\x00\x00" * 1600)
        decoded = decode_audio(audio.name, sampling_rate=16000)
        if len(decoded) != 1600:
            raise RuntimeError(f"GPU ASR audio decoder returned {len(decoded)} samples, expected 1600")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("/worker/asr-models"))
    parser.add_argument("--mode", choices=("download", "verify"), required=True)
    args = parser.parse_args()

    if args.mode == "verify":
        # No network access allowed at verification or inference time.
        os.environ["HF_HUB_OFFLINE"] = "1"
    else:
        args.cache.mkdir(parents=True, exist_ok=True)

    records = [check_model(model, args.cache, offline=args.mode == "verify")
               for model in BUNDLED_MODELS]
    verify_audio_decoder()

    if args.mode == "download":
        (args.cache / "bundled-models.json").write_text(
            json.dumps({"schema": 1, "models": records}, indent=2) + "\n", encoding="utf-8")
    print(f"Concat offline GPU ASR {args.mode} PASS: " +
          ", ".join(entry["model"] for entry in records))


if __name__ == "__main__":
    main()
