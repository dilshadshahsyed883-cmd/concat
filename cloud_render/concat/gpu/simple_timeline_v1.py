#!/usr/bin/env python3
"""Experimental Stage 1 CUDA/NVENC cut timeline + narration.

Strict standalone acceptance CLI; DOES NOT replace native Concat/concat.json.
No software video-frame filters or CPU visual fallback are permitted.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import subprocess
from pathlib import Path


class GPUPlanError(ValueError):
    pass


def check_shape(data, allowed, required, name):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise GPUPlanError(f"{name}: unsupported or missing fields")


def media_path(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise GPUPlanError("Media paths must be relative")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise GPUPlanError("Media path escapes project folder")
    return path


def probe(ffprobe, path):
    try:
        run = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
                             text=True, capture_output=True, timeout=30, check=True)
        return json.loads(run.stdout)["streams"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise GPUPlanError(f"Cannot probe {path.name}") from None


def fps(value):
    try:
        a, b = value.split("/")
        return int(a) / int(b)
    except (AttributeError, ValueError, ZeroDivisionError):
        return 0


def positive(value, name, minimum=0):
    if type(value) not in (float, int) or not math.isfinite(value) or value < minimum:
        raise GPUPlanError(f"{name}: invalid value")
    return float(value)


def command_for(project, timeline, target, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    root = Path(project).expanduser().resolve(strict=True)
    spec = json.loads(Path(timeline).read_text(encoding="utf-8"))
    check_shape(spec, ("version", "width", "height", "fps", "clips", "voice", "video_bitrate"),
                ("version", "width", "height", "fps", "clips", "voice"), "timeline")
    if (type(spec["version"]) is not int or spec["version"] != 1 or
        type(spec["width"]) is not int or spec["width"] != 1920 or
        type(spec["height"]) is not int or spec["height"] != 1080 or
        type(spec["fps"]) is not int or spec["fps"] != 30):
        raise GPUPlanError("Stage 1 is strictly version 1 / 1080p / 30 fps")
    clips = spec["clips"]
    if not isinstance(clips, list) or not 1 <= len(clips) <= 80:
        raise GPUPlanError("Stage 1 requires 1–80 clips")
    bitrate = spec.get("video_bitrate", 6000000)
    if type(bitrate) is not int or not 2000000 <= bitrate <= 12000000:
        raise GPUPlanError("video_bitrate must be 2–12 Mbps")

    args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-init_hw_device", "cuda=gpu:0", "-filter_hw_device", "gpu"]
    filters = []
    total_frames = 0
    for i, clip in enumerate(clips):
        check_shape(clip, ("path", "kind", "duration", "source_start"),
                    ("path", "kind", "duration"), f"clip {i}")
        path = media_path(root, clip["path"])
        duration = positive(clip["duration"], "duration", 1 / 30)
        frames = round(duration * 30)
        if frames < 1 or abs(duration * 30 - frames) > .001:
            raise GPUPlanError("Duration must align to 30-fps frames")
        total_frames += frames
        if total_frames > 30 * 3600:
            raise GPUPlanError("Stage 1 is limited to 60 minutes")
        v = next((s for s in probe(ffprobe, path) if s.get("codec_type") == "video"), None)
        if not v:
            raise GPUPlanError(f"Clip {i} has no video stream")
        w, h = v.get("width", 0), v.get("height", 0)
        if not w or not h or abs(w / h - 16 / 9) > .015:
            raise GPUPlanError("Stage 1 requires 16:9 input; no silent stretching")
        if clip["kind"] == "image":
            if ("source_start" in clip or path.suffix.lower() not in (".jpg", ".jpeg") or
                    v.get("codec_name") != "mjpeg"):
                raise GPUPlanError("Images must be JPEG with CUDA MJPEG decode")
            args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                     "-c:v", "mjpeg_cuvid", "-loop", "1", "-framerate", "30",
                     "-t", str(frames / 30), "-i", str(path)]
        elif clip["kind"] == "video":
            if (path.suffix.lower() not in (".mp4", ".mov", ".mkv") or
                v.get("codec_name") not in ("h264", "hevc") or
                abs(fps(v.get("avg_frame_rate")) - 30) > .01 or
                abs(fps(v.get("r_frame_rate")) - 30) > .01):
                raise GPUPlanError("Videos must be 30-fps H.264 or HEVC")
            start = positive(clip.get("source_start", 0), "source_start")
            args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                     "-ss", str(start), "-t", str(frames / 30), "-i", str(path)]
        else:
            raise GPUPlanError("Unsupported clip kind (only image/video cuts)")
        filters.append(f"[{i}:v:0]scale_cuda=w=1920:h=1080:format=nv12,"
                       f"trim=end_frame={frames},setpts=PTS-STARTPTS[v{i}]")

    voice = media_path(root, spec["voice"])
    if voice.suffix.lower() not in (".mp3", ".wav", ".m4a", ".aac") or not any(
        s.get("codec_type") == "audio" for s in probe(ffprobe, voice)
    ):
        raise GPUPlanError("Required voice track missing/unsupported")
    args += ["-i", str(voice)]
    filters.append("".join(f"[v{i}]" for i in range(len(clips))) +
                   f"concat=n={len(clips)}:v=1:a=0[outv]")
    seconds = total_frames / 30
    final = Path(target).expanduser().absolute()
    temp = final.with_name(final.stem + ".gpu-partial.mp4")
    if (final.exists() or final.is_symlink() or temp.exists() or temp.is_symlink() or
            not final.parent.is_dir()):
        raise GPUPlanError("Output must be new; existing output/partial preserved")
    args += ["-filter_complex", ";".join(filters), "-map", "[outv]",
             "-map", f"{len(clips)}:a:0", "-c:v", "h264_nvenc", "-preset", "p4",
             "-b:v", str(bitrate), "-maxrate", str(int(bitrate * 1.25)),
             "-bufsize", str(2 * bitrate), "-r", "30",
             "-c:a", "aac", "-b:a", "160k",
             "-af", f"apad,atrim=duration={seconds},asetpts=PTS-STARTPTS",
             "-t", str(seconds), "-movflags", "+faststart", str(temp)]
    return args, temp, final, seconds


def render(project, timeline, target, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    args, temp, final, duration = command_for(project, timeline, target, ffmpeg, ffprobe)
    try:
        process = subprocess.run(args, capture_output=True, text=True, timeout=7200)
        if process.returncode:
            raise RuntimeError("GPU render failed; CPU visual fallback forbidden: " +
                               process.stderr[-1000:])
        info = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(temp)],
            capture_output=True, text=True, timeout=30, check=True)
        result = json.loads(info.stdout)
        streams = result.get("streams", [])
        if not (any(s.get("codec_type") == "video" and s.get("codec_name") == "h264"
                    and s.get("width") == 1920 and s.get("height") == 1080 for s in streams)
                and any(s.get("codec_type") == "audio" and s.get("codec_name") == "aac"
                        for s in streams)
                and abs(float(result["format"]["duration"]) - duration) <= .25):
            raise RuntimeError("Rendered video/audio/length verification failed")
        os.replace(temp, final)
        return {"output": str(final), "duration": duration,
                "bytes": final.stat().st_size, "video": "h264_nvenc", "audio": "aac-cpu"}
    finally:
        if temp.is_file():
            temp.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir")
    parser.add_argument("timeline_json")
    parser.add_argument("output_mp4")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    if options.dry_run:
        command, _, _, seconds = command_for(options.project_dir, options.timeline_json,
                                               options.output_mp4)
        print(json.dumps({"command": command, "duration": seconds}, indent=2))
    else:
        print(json.dumps(render(options.project_dir, options.timeline_json,
                                options.output_mp4), indent=2))
