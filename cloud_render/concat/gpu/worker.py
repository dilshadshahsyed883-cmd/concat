"""CUDA/NVENC-only HTTP worker for native Concat Stage 1 project bundles.

Retired: native concat-cli/Vulkan/wgpu renderer and CPU Vulkan fallback.
Reuse existing authenticated uploads, job state and verified VPS delivery.
Unsupported edits fail closed; retained source bundles are never removed.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cloud_render.concat.hybrid.contract import (
    BUNDLE_VERSION, HYBRID_MODE, RENDER_MODES, WORKER_API_VERSION,
)
from cloud_render.concat.project_bundle import restore, MAX_PROJECT_MANIFEST_BYTES
from cloud_render.concat.gpu.resumable import ResumableUpload, UploadError

IDS = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
DIGESTS = re.compile(r"^[a-f0-9]{64}$")
CHUNK = 8 * 1024 * 1024
MAX_BUNDLE = 4 * 1024 * 1024 * 1024
MAX_QUEUE = 20
MP4_SIGNATURE = b"ftyp"


class DirectReceiver(urllib.request.HTTPRedirectHandler):
    """VPS ingest must respond at the configured authenticated origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("VPS ingest redirected instead of verifying the upload")


class WorkerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(CHUNK), b""):
            h.update(piece)
    return h.hexdigest()


def check_id(job: str) -> str:
    if not IDS.fullmatch(job):
        raise WorkerError(400, "Invalid job ID")
    return job



def concat_stage1_plan(project: Path) -> Path:
    """Strict adapter for v0.2.8 Concat cuts+one narration track.

    Read the *restored* native concat.json; never mutate its original semantics.
    Reject unsupported effects, gaps, overlays or audio processing, rather than
    render something that differs from what the Windows editor shows.
    """
    root = project.resolve(strict=True)
    docpath = next((root / n for n in ("concat.json", "wolfcut.json")
                    if (root / n).is_file()), None)
    if not docpath:
        raise ValueError("CUDA Stage 1: Concat project manifest missing")
    document = json.loads(docpath.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or document.get("version", 1) != 1:
        raise ValueError("CUDA Stage 1: unsupported Concat project version")
    timelines = document.get("timelines")
    if isinstance(timelines, list) and timelines:
        active = document.get("activeTimelineId")
        matches = [x for x in timelines if isinstance(x, dict) and x.get("id") == active]
        timeline = matches[0] if len(matches) == 1 else timelines[0]
    else:
        timeline = document
    if not isinstance(timeline, dict):
        raise ValueError("CUDA Stage 1: invalid timeline")
    video = timeline.get("video") or document.get("video") or {}
    if (not isinstance(video, dict) or video.get("width") != 1920 or
        video.get("height") != 1080 or video.get("rateNum") != 30 or
        video.get("rateDen") != 1 or video.get("colorSpace", "sdr") != "sdr"):
        raise ValueError("CUDA Stage 1 requires 1080p30 SDR Concat timeline")
    if (timeline.get("background") or {}).get("kind", "none") != "none":
        raise ValueError("CUDA Stage 1: timeline background effect unsupported")
    tracks = timeline.get("tracks")
    clips = timeline.get("clips")
    media = document.get("media")
    if not all(isinstance(x, list) for x in (tracks, clips, media)):
        raise ValueError("CUDA Stage 1: incomplete Concat document")
    track_map = {t["id"]: t for t in tracks if isinstance(t, dict) and isinstance(t.get("id"), str)}
    media_map = {m["id"]: m for m in media if isinstance(m, dict) and isinstance(m.get("id"), str)}
    if not track_map or not media_map or not clips:
        raise ValueError("CUDA Stage 1: timeline has no playable media")

    from math import isfinite
    def number(value, default=0.0):
        if value is None:
            return default
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError("CUDA Stage 1: invalid clip number")
        return float(value)

    visuals, sound = [], []
    visual_tracks = set()
    for clip in clips:
        if not isinstance(clip, dict):
            raise ValueError("CUDA Stage 1: invalid clip")
        kind = clip.get("kind")
        if kind not in ("image", "video", "audio"):
            raise ValueError("CUDA Stage 1: unsupported title/layer/shape clip")
        track = track_map.get(clip.get("trackId"))
        if track is None:
            raise ValueError("CUDA Stage 1: missing clip track")
        if kind in ("video", "image") and track.get("visible", True) is False:
            raise ValueError("CUDA Stage 1: hidden visual track unsupported")
        if kind == "audio" and track.get("muted", False):
            raise ValueError("CUDA Stage 1: muted narration track unsupported")
        media_item = media_map.get(clip.get("mediaId"))
        if not media_item:
            raise ValueError("CUDA Stage 1: missing media identity")
        path = Path(str(media_item.get("path", ""))).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("CUDA Stage 1: media escapes restored project")
        if media_item.get("placeholder"):
            raise ValueError("CUDA Stage 1: unfilled placeholder")
        if kind == "video" and media_item.get("hasAudio") and clip.get("muted") is not True:
            raise ValueError("CUDA Stage 1: mute embedded video audio before using one narration track")

        # Features not represented by simple_timeline_v1 are hard rejected.
        for key in ("keys", "filters", "videoEffects", "speedCurve",
                    "transitionIn", "crop", "cutout", "mask", "stabilize",
                    "enhanced", "text", "shape", "background", "flipH", "flipV"):
            if clip.get(key):
                raise ValueError(f"CUDA Stage 1: unsupported Concat feature {key}")
        for key, default in (("speed", 1), ("scale", 1), ("stretchX", 1),
                             ("stretchY", 1), ("opacity", 1), ("volume", 1),
                             ("offsetX", 0), ("offsetY", 0),
                             ("rotation", 0), ("fadeIn", 0), ("fadeOut", 0)):
            if abs(number(clip.get(key), default) - default) > 1e-7:
                raise ValueError(f"CUDA Stage 1: unsupported Concat adjustment {key}")
        if clip.get("blend") not in (None, "", "normal"):
            raise ValueError("CUDA Stage 1: blend mode unsupported")
        start = number(clip.get("start"))
        duration = number(clip.get("duration"))
        source_start = number(clip.get("sourceStart"))
        if start < 0 or duration <= 0 or source_start < 0:
            raise ValueError("CUDA Stage 1: invalid clip timing")
        if any(abs(v * 30 - round(v * 30)) > .001 for v in (start, duration, source_start)):
            raise ValueError("CUDA Stage 1: clip timing must align to 30 fps")
        if clip.get("audioStream") not in (None, 0):
            raise ValueError("CUDA Stage 1: alternate audio stream unsupported")
        relative = path.relative_to(root).as_posix()
        if kind in ("image", "video"):
            visual_tracks.add(clip.get("trackId"))
            visuals.append((start, {"path": relative, "kind": kind, "duration": duration,
                                    **({"source_start": source_start} if kind == "video" else {})}))
            if kind == "image" and source_start != 0:
                raise ValueError("CUDA Stage 1: image in-point unsupported")
        else:
            if source_start:
                raise ValueError("CUDA Stage 1: narration trim unsupported")
            sound.append((start, duration, relative))
    if len(visual_tracks) != 1 or len(sound) != 1 or len(visuals) > 80:
        raise ValueError("CUDA Stage 1 supports one picture track and one narration clip")
    ordered = sorted(visuals, key=lambda x: x[0])
    expected = 0.0
    for start, clip in ordered:
        if abs(start - expected) > .001:
            raise ValueError("CUDA Stage 1: video gaps/overlaps unsupported")
        expected += clip["duration"]
    if sound[0][0] != 0 or sound[0][1] > expected + 1 / 30 + .001:
        raise ValueError("CUDA Stage 1: one narration clip must start at zero and fit the video")

    spec = {"version": 1, "width": 1920, "height": 1080, "fps": 30,
            "clips": [clip for _, clip in ordered], "voice": sound[0][2]}
    # Generated on the already-restored temporary project, never the PC project.
    output = root / ".gpu-stage1-timeline.json"
    if output.exists() or output.is_symlink():
        raise ValueError("CUDA Stage 1: generated plan path already occupied")
    output.write_text(json.dumps(spec, sort_keys=True), encoding="utf-8")
    return output


class RenderWorker:
    def __init__(self, root: Path, *, token: str,
                 receiver: str = "", receiver_token: str = "", renderer=None,
                 deliver=None):
        if len(token) < 32:
            raise RuntimeError("RENDERER_API_TOKEN must have at least 32 characters")
        self.root = root.resolve()
        self.token = token
        self.receiver = receiver.rstrip("/")
        self.receiver_token = receiver_token
        # The optional renderer override is for offline tests only.
        self.renderer = renderer
        self.deliver = deliver or self._deliver
        self.lock = threading.RLock()
        # Two executor slots; the runtime setting controls how many renders start.
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="concat-export")
        self.slots = threading.Condition(self.lock)
        self.active_renders = 0
        self.concurrency = 1
        self.concurrency_path = self.root / "render-concurrency.json"
        self.state = self.root / "state"
        self.inputs = self.root / "jobs"
        self.outputs = self.root / "outputs"
        for folder in (self.root, self.state, self.inputs, self.outputs):
            if folder.is_symlink():
                raise RuntimeError("Worker folders must not be symlinks")
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
        if self.concurrency_path.is_file() and not self.concurrency_path.is_symlink():
            try:
                saved = json.loads(self.concurrency_path.read_text(encoding="utf-8"))
                if type(saved.get("concurrency")) is int and saved["concurrency"] in (1, 2):
                    self.concurrency = saved["concurrency"]
            except (OSError, ValueError, AttributeError):
                pass
        self.spool = ResumableUpload(self)
        # Do not pretend a stopped or interrupted render succeeded.
        for path in self.state.glob("*.json"):
            if not IDS.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                item = json.loads(path.read_text())
                if item.get("state") in ("queued", "running", "delivering"):
                    self._save(path.stem, {**item, "state": "failed",
                        "error": "Interrupted by worker restart; source bundle retained"})
            except (OSError, ValueError):
                continue

    def concurrency_status(self) -> dict:
        with self.slots:
            return {"concurrency": self.concurrency, "max_concurrency": 2,
                    "active_renders": self.active_renders}

    def set_concurrency(self, value: int) -> dict:
        if type(value) is not int or value not in (1, 2):
            raise WorkerError(400, "Render concurrency must be exactly 1 or 2")
        with self.slots:
            temp = self.concurrency_path.with_suffix(".tmp")
            temp.write_text(json.dumps({"concurrency": value}), encoding="utf-8")
            os.replace(temp, self.concurrency_path)
            self.concurrency = value
            self.slots.notify_all()
            return self.concurrency_status()

    def _path(self, job: str) -> Path:
        return self.state / (check_id(job) + ".json")

    def _save(self, job: str, record: dict):
        path = self._path(job)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)

    def status(self, job: str) -> dict:
        path = self._path(job)
        if not path.is_file() or path.is_symlink():
            raise WorkerError(404, "Unknown job")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise WorkerError(500, "Corrupted job state") from None

    def _change(self, job: str, **fields):
        with self.lock:
            item = self.status(job)
            item.update(fields)
            self._save(job, item)

    def ready(self) -> bool:
        # Actual NVIDIA hardware capability is verified during the render.
        return bool(shutil.which("ffmpeg") and shutil.which("ffprobe") and self.state.is_dir())

    def accept(self, job: str, stream, length: int, checksum: str,
               render_mode: str = "cuda-stage1") -> dict:
        job = check_id(job)
        if render_mode not in RENDER_MODES:
            raise WorkerError(400, "Native Vulkan engine retired; use cuda-stage1 or cuda-hybrid-v1")
        if not 0 < length <= MAX_BUNDLE or not DIGESTS.fullmatch(checksum):
            raise WorkerError(400, "Invalid bundle size or SHA256")
        with self.lock:
            if self._path(job).exists() or (self.inputs / job).exists():
                raise WorkerError(409, "Job ID already exists")
            busy = sum(1 for path in self.state.glob("*.json")
                       if self._read_state(path) in ("queued", "running", "delivering"))
            if busy >= MAX_QUEUE:
                raise WorkerError(429, "Worker queue is full")
            directory = self.inputs / job
            directory.mkdir(mode=0o700)
            target = directory / "project.zip"
            try:
                h = hashlib.sha256()
                remaining = length
                with target.open("xb") as output:
                    while remaining:
                        part = stream.read(min(CHUNK, remaining))
                        if not part:
                            raise WorkerError(400, "Incomplete project upload")
                        output.write(part)
                        h.update(part)
                        remaining -= len(part)
                    output.flush()
                    os.fsync(output.fileno())
                if not hmac.compare_digest(h.hexdigest(), checksum):
                    raise WorkerError(422, "Bundle SHA256 mismatch")
                self._save(job, {"job": job, "state": "queued", "size": length,
                                 "sha256": checksum, "created": int(time.time()), "error": "",
                                 "render_mode": render_mode})
            except BaseException:
                shutil.rmtree(directory)
                raise
            self.pool.submit(self._process, job)
            return self.status(job)

    @staticmethod
    def _read_state(path: Path) -> str:
        try:
            return json.loads(path.read_text()).get("state", "")
        except (OSError, ValueError):
            return ""

    @staticmethod
    def _performance_metrics(report, elapsed):
        """Persist actual rendering time without including upload or VPS delivery.

        Frame count and video seconds come from the renderer's verified output
        report. Older/mocked renderers without a report keep elapsed time only.
        """
        elapsed = max(float(elapsed), 0.001)
        metrics = {"render_time_seconds": round(elapsed, 3)}
        if not isinstance(report, dict):
            return metrics
        frames = report.get("frames")
        seconds = report.get("seconds", report.get("duration"))
        if (type(frames) is not int or frames <= 0 or
                type(seconds) not in (int, float) or
                not math.isfinite(seconds) or seconds <= 0):
            return metrics
        return {
            **metrics,
            "rendered_frames": frames,
            "video_duration_seconds": round(seconds, 3),
            "average_fps": round(frames / elapsed, 2),
            "speed_multiplier": round(seconds / elapsed, 2),
        }

    def _process(self, job: str):
        # A queued task may occupy a thread, but cannot enter the renderer
        # until a slot is available. Lowering 2 -> 1 never kills an active job.
        with self.slots:
            while self.active_renders >= self.concurrency:
                self.slots.wait()
            self.active_renders += 1
        try:
            self._process_active(job)
        finally:
            with self.slots:
                self.active_renders -= 1
                self.slots.notify_all()

    def _process_active(self, job: str):
        directory = self.inputs / job
        output = self.outputs / (job + ".mp4")
        try:
            self._change(job, state="running")
            extracted = directory / "project"
            restore(directory / "project.zip", extracted)
            mode = self.status(job).get("render_mode", "native")
            if mode not in RENDER_MODES:
                raise RuntimeError("Legacy native Vulkan jobs are retired. Source bundle retained")
            render_started = time.perf_counter()
            if self.renderer is not None:
                report = self.renderer(extracted, output)
            elif mode == HYBRID_MODE:
                report = self._hybrid_render(job, extracted, output)
            else:
                report = self._cuda_render(extracted, output)
            elapsed = time.perf_counter() - render_started
            if not output.is_file() or output.is_symlink() or output.stat().st_size < 12:
                raise RuntimeError("Concat did not produce an MP4")
            with output.open("rb") as handle:
                header = handle.read(12)
            if header[4:8] != MP4_SIGNATURE:
                raise RuntimeError("Concat output MP4 signature invalid")
            self._change(job, state="rendered", output_size=output.stat().st_size,
                         output_sha256=digest_file(output),
                         **self._performance_metrics(report, elapsed))
            if self.receiver:
                self._change(job, state="delivering")
                self.deliver(job, output)
                self._change(job, state="delivered")
                # GPU side cache only: VPS has confirmed the complete file.
                output.unlink()
                shutil.rmtree(directory)
        except Exception as exc:
            # Source bundle and output, if any, remain for diagnosis/retry.
            prior = self.status(job)
            self._change(job, state="delivery_failed" if prior["state"] == "delivering" else "failed",
                         error=str(exc)[:500])

    def _hybrid_render(self, job: str, project: Path, output: Path):
        call = [sys.executable, "-m", "cloud_render.concat.hybrid.render", str(project), str(output)]
        # Stream progress and skipped effects to container logs while rendering.
        log_file = self.state / f"{check_id(job)}.render.log"
        with log_file.open("w", encoding="utf-8") as log:
            outcome = subprocess.run(call, stdout=log, stderr=None,
                                     timeout=int(os.getenv("CONCAT_RENDER_TIMEOUT", "7200")))
        if outcome.returncode:
            raise RuntimeError(f"CUDA hybrid renderer exited with code {outcome.returncode}. Check the Salad worker logs.")
        report = log_file.read_text(encoding="utf-8").strip()
        if report:
            print(f"GPU job {job}: {report}", flush=True)
            try:
                return json.loads(report)
            except ValueError:
                # A valid MP4 must not be lost solely because old renderers
                # did not return a machine-readable performance summary.
                return None
        return None

    def _cuda_render(self, project: Path, output: Path):
        plan = concat_stage1_plan(project)
        # The native Concat FFmpeg shared libraries are intentionally incompatible
        # with system FFmpeg CUDA. Clear ONLY in this subprocess, not globally.
        env = os.environ.copy()
        env.pop("LD_LIBRARY_PATH", None)
        call = ["python3", "-m", "cloud_render.concat.gpu.simple_timeline_v1",
                str(project), str(plan), str(output)]
        outcome = subprocess.run(call, capture_output=True, text=True,
                                 env=env, timeout=int(os.getenv("CONCAT_RENDER_TIMEOUT", "7200")))
        if outcome.returncode:
            raise RuntimeError("CUDA Stage 1 failed: " + outcome.stderr[-700:])
        try:
            result = json.loads(outcome.stdout)
        except ValueError:
            return None
        # Stage 1 is strictly 30 FPS and verifies the produced MP4.
        duration = result.get("duration") if isinstance(result, dict) else None
        if (type(duration) in (int, float) and math.isfinite(duration)
                and duration > 0):
            return {"frames": round(duration * 30), "seconds": duration}
        return None

    def _deliver(self, job: str, output: Path):
        if not self.receiver.startswith("https://") or not self.receiver_token:
            raise RuntimeError("HTTPS VPS receiver and RENDER_OUTPUT_TOKEN required")
        host = urllib.parse.urlsplit(self.receiver)
        if host.username or host.password or host.query or host.fragment or host.path not in ("", "/"):
            raise RuntimeError("VPS receiver must be an HTTPS origin, not a URL path")
        length = output.stat().st_size
        checksum = digest_file(output)
        base = self.receiver + "/api/cloud-render/ingest/" + job + "/chunk"
        opener = urllib.request.build_opener(DirectReceiver())
        with output.open("rb") as stream:
            for index in range((length + CHUNK - 1) // CHUNK):
                chunk = stream.read(CHUNK)
                url = base + "?" + urllib.parse.urlencode({
                    "index": index, "name": job + ".mp4", "size": length, "sha256": checksum})
                request = urllib.request.Request(url, data=chunk, method="POST",
                    headers={"X-Cloud-Render-Token": self.receiver_token,
                             "Content-Type": "application/octet-stream"})
                with opener.open(request, timeout=120) as response:
                    payload = json.load(response)
                if not payload.get("ok"):
                    raise RuntimeError("VPS chunk receiver rejected upload")
                if index == (length + CHUNK - 1) // CHUNK - 1 and payload.get("state") != "ready":
                    raise RuntimeError("VPS did not verify finished MP4")

    def retry_delivery(self, job: str) -> dict:
        with self.lock:
            entry = self.status(job)
            if entry["state"] not in ("rendered", "delivery_failed") or not self.receiver:
                raise WorkerError(409, "Job is not awaiting delivery")
            target = self.outputs / (job + ".mp4")
            if not target.is_file() or target.is_symlink():
                raise WorkerError(409, "Output missing")
            self._change(job, state="delivering", error="")
            self.pool.submit(self._retry, job, target)
            return self.status(job)

    def _retry(self, job: str, output: Path):
        try:
            self.deliver(job, output)
            self._change(job, state="delivered")
            output.unlink()
            shutil.rmtree(self.inputs / job, ignore_errors=True)
        except Exception as exc:
            self._change(job, state="delivery_failed", error=str(exc)[:500])


class SaladDualStackHTTPServer(ThreadingHTTPServer):
    """Listen on IPv6 for Salad Gateway and IPv4 for local/CI clients.

    Salad Gateway forwards requests over IPv6 to the container. Explicitly
    clear IPV6_V6ONLY so the existing IPv4 health checks still work.
    Fail startup instead of silently exposing a broken IPv4-only gateway.
    """
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = "ConcatGPU/1"
    protocol_version = "HTTP/1.1"

    @property
    def worker(self) -> RenderWorker:
        return self.server.worker

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorize(self):
        expected = "Bearer " + self.worker.token
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            raise WorkerError(401, "Bearer token required")

    def _route(self, action: str):
        path = urllib.parse.urlsplit(self.path).path
        if action == "GET" and path == "/health/live":
            return self._json(200, {"ok": True})
        if action == "GET" and path == "/health/ready":
            ok = self.worker.ready()
            return self._json(200 if ok else 503, {"ok": ok, "engine": "cuda-nvenc-stage1", "cuda_stage1": True, "native_vulkan": False})
        self._authorize()
        if action == "GET" and path == "/api/capabilities":
            return self._json(200, {"api_version": WORKER_API_VERSION,
                                    "bundle_versions": [BUNDLE_VERSION],
                                    "render_modes": list(RENDER_MODES),
                                    "validation_version": 1,
                                    "max_project_manifest_bytes": MAX_PROJECT_MANIFEST_BYTES})
        if path == "/api/config/concurrency":
            if action == "GET":
                return self._json(200, self.worker.concurrency_status())
            if action != "POST":
                raise WorkerError(405, "Method not allowed")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise WorkerError(415, "Concurrency setting requires JSON")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise WorkerError(400, "Invalid concurrency Content-Length") from None
            if not 0 < length <= 100:
                raise WorkerError(400, "Invalid concurrency request")
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                raise WorkerError(400, "Invalid concurrency JSON") from None
            if not isinstance(data, dict) or set(data) != {"concurrency"}:
                raise WorkerError(400, "Concurrency requires exactly one setting")
            return self._json(200, self.worker.set_concurrency(data["concurrency"]))
        if action == "POST" and path == "/api/validate":
            # Read only the manifest, never accept media before validation.
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise WorkerError(415, "GPU validation requires JSON")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise WorkerError(400, "Invalid GPU validation Content-Length") from None
            if length <= 0:
                raise WorkerError(400, "GPU project JSON is empty")
            if length > MAX_PROJECT_MANIFEST_BYTES:
                raise WorkerError(413, f"GPU project JSON is {length} bytes; maximum is {MAX_PROJECT_MANIFEST_BYTES} bytes (64 MiB)")
            try:
                manifest = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise WorkerError(400, "Invalid Concat project JSON (syntax, UTF-8 or nesting depth)") from None
            from cloud_render.concat.hybrid.project import validate_gpu_manifest
            try:
                validate_gpu_manifest(manifest)
            except (ValueError, KeyError, TypeError) as exc:
                return self._json(200, {"ok": False, "errors": [str(exc)]})
            return self._json(200, {"ok": True, "profile": "cuda-hybrid-v1",
                                    "project_bytes": length})

        if action == "GET" and path == "/api/diagnostics/gpu":
            return self._json(200, {"engine": "cuda-nvenc-stage1",
                                    "cuda_stage1": True, "native_vulkan": False,
                                    "ffmpeg_available": bool(shutil.which("ffmpeg")),
                                    "ffprobe_available": bool(shutil.which("ffprobe"))})
        upload = re.fullmatch(
            r"/api/uploads/([A-Za-z0-9_-]{1,80})(?:/chunks/([0-9]{1,4})|/(complete))?", path)
        if upload:
            job, part, complete = upload.groups()
            if action == "GET" and part is None and complete is None:
                return self._json(200, self.worker.spool.status(job))
            if action == "POST" and part is None and complete is None:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise WorkerError(415, "Start upload must be application/json")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise WorkerError(400, "Invalid Content-Length") from None
                if not 0 < length <= 1024:
                    raise WorkerError(400, "Invalid upload manifest length")
                try:
                    manifest = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    raise WorkerError(400, "Invalid upload manifest JSON") from None
                if not isinstance(manifest, dict):
                    raise WorkerError(400, "Upload manifest must be an object")
                return self._json(200, self.worker.spool.begin(
                    job, manifest.get("size"), manifest.get("sha256"),
                    manifest.get("render_mode", "cuda-stage1")))
            if action == "PUT" and part is not None:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/octet-stream":
                    raise WorkerError(415, "Chunk must be application/octet-stream")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise WorkerError(400, "Invalid Content-Length") from None
                return self._json(200, self.worker.spool.put_chunk(
                    job, int(part), self.rfile, length,
                    self.headers.get("X-Content-SHA256", "")))
            if action == "POST" and complete is not None:
                return self._json(202, self.worker.spool.complete(job))
            raise WorkerError(405, "Method not allowed")
        m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]{1,80})(/output|/retry-delivery)?", path)
        if not m:
            raise WorkerError(404, "Unknown route")
        job, suffix = m.groups()
        if action == "GET" and not suffix:
            return self._json(200, self.worker.status(job))
        if action == "POST" and not suffix:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/zip":
                raise WorkerError(415, "Upload must be application/zip")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise WorkerError(400, "Invalid Content-Length") from None
            entry = self.worker.accept(job, self.rfile, length,
                                       self.headers.get("X-Content-SHA256", ""),
                                       self.headers.get("X-Render-Mode", "cuda-stage1"))
            return self._json(202, entry)
        if action == "POST" and suffix == "/retry-delivery":
            return self._json(202, self.worker.retry_delivery(job))
        if action == "GET" and suffix == "/output":
            item = self.worker.status(job)
            output = self.worker.outputs / (job + ".mp4")
            if item["state"] not in ("rendered", "delivery_failed") or not output.is_file() or output.is_symlink():
                raise WorkerError(404, "Output unavailable")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(output.stat().st_size))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with output.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, CHUNK)
            return
        raise WorkerError(405, "Method not allowed")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def _handle(self, action):
        self.connection.settimeout(180)
        try:
            self._route(action)
        except (WorkerError, UploadError) as exc:
            self.close_connection = True
            self._json(exc.status, {"ok": False, "error": str(exc)})
        except Exception:
            self.close_connection = True
            self._json(500, {"ok": False, "error": "Internal worker failure"})

    def log_message(self, format, *args):
        # Do not log Authorization headers, receiver tokens or URLs with secrets.
        super().log_message(format, *args)


def main():
    token = os.getenv("RENDERER_API_TOKEN", "")
    worker = RenderWorker(Path(os.getenv("CONCAT_WORK_ROOT", "/worker")),
                          token=token,
                          receiver=os.getenv("CLOUD_RENDER_VPS_BASE_URL", ""),
                          receiver_token=os.getenv("RENDER_OUTPUT_TOKEN", ""))
    port = int(os.getenv("PORT", "8080"))
    server = SaladDualStackHTTPServer(("::", port), Handler)
    server.worker = worker
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        worker.pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
