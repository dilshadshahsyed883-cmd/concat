"""Run a two-cut CUDA Stage 1 render with generated fixtures and inspect the MP4.

Usage: python -m cloud_render.concat.gpu.acceptance NEW_RESULTS_DIRECTORY
"""
from __future__ import annotations

import argparse
import array
import json
import subprocess
import time
from fractions import Fraction
from pathlib import Path

from cloud_render.concat.gpu.simple_timeline_v1 import render

FPS = 30
WIDTH, HEIGHT = 1920, 1080
FRAMES = 60


def ffmpeg(*args: str) -> bytes:
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *args],
                          capture_output=True, check=True, timeout=60).stdout


def generate_inputs(root: Path) -> Path:
    for color in ("red", "blue"):
        ffmpeg("-f", "lavfi", "-i", f"color={color}:s=320x180:r={FPS}:d=1",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / f"{color}.mp4"))
    ffmpeg("-f", "lavfi", "-i",
           r"aevalsrc=if(gte(t\,1)\,0.4*sin(2*PI*440*t)\,0):s=48000:d=2",
           str(root / "voice.wav"))
    plan = root / "timeline.json"
    plan.write_text(json.dumps({
        "version": 1, "width": WIDTH, "height": HEIGHT, "fps": FPS,
        "clips": [{"path": f"{c}.mp4", "kind": "video", "duration": 1}
                  for c in ("red", "blue")],
        "voice": "voice.wav",
    }), encoding="utf-8")
    return plan


def inspect_cut_output(output: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-count_frames",
                             "-show_streams", "-of", "json", str(output)],
                            capture_output=True, check=True, timeout=60)
    streams = json.loads(result.stdout)["streams"]
    video = [s for s in streams if s["codec_type"] == "video"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    if len(video) != 1 or len(audio) != 1:
        raise ValueError("Expected one video stream and one narration stream")
    v, a = video[0], audio[0]
    if (v["codec_name"], v["width"], v["height"], int(v.get("nb_read_frames", 0)),
            Fraction(v["avg_frame_rate"])) != ("h264", WIDTH, HEIGHT, FRAMES, FPS):
        raise ValueError("Unexpected video codec, dimensions, frame count or rate")
    if a["codec_name"] != "aac" or abs(float(a["duration"]) - FRAMES / FPS) > 1 / FPS:
        raise ValueError("Narration codec or duration is incorrect")

    samples = []
    for at in ("0.75", "1.25"):
        pixel = ffmpeg("-ss", at, "-i", str(output), "-an", "-frames:v", "1",
                       "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")
        if len(pixel) != 3:
            raise ValueError("Could not sample the output picture")
        pcm = ffmpeg("-ss", at, "-i", str(output), "-vn", "-t", "0.1",
                     "-ac", "1", "-ar", "48000", "-f", "s16le", "-")
        amplitudes = array.array("h", pcm)
        if not amplitudes:
            raise ValueError("Could not sample the output audio")
        samples.append({"at": at, "rgb": list(pixel),
                        "peak_pcm16": max(abs(n) for n in amplitudes)})
    red, blue = samples
    if not (red["rgb"][0] > 170 and red["rgb"][2] < 70
            and blue["rgb"][2] > 170 and blue["rgb"][0] < 70):
        raise ValueError("Output pictures do not match the cut timeline")
    if red["peak_pcm16"] > 500 or blue["peak_pcm16"] < 5000:
        raise ValueError("Narration transition does not align to the picture cut")
    return {"video_codec": v["codec_name"], "audio_codec": a["codec_name"],
            "resolution": [WIDTH, HEIGHT], "frames": FRAMES, "fps": FPS,
            "audio_duration": float(a["duration"]), "samples": samples}


def run(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=False)
    timeline = generate_inputs(root)
    output = root / "render.mp4"
    started = time.perf_counter()
    render_info = render(root, timeline, output)
    elapsed = time.perf_counter() - started
    evidence = {**inspect_cut_output(output), "render": render_info,
                "elapsed_seconds": round(elapsed, 3),
                "frames_per_second": round(FRAMES / elapsed, 2)}
    (root / "acceptance.json").write_text(json.dumps(evidence, indent=2) + "\n",
                                          encoding="utf-8")
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_directory", type=Path)
    print(json.dumps(run(parser.parse_args().results_directory), indent=2))
