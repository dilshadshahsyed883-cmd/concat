"""Disk-backed, SHA256-checked resumable project ZIP upload for Salad Gateway.

The gateway has a bounded request timeout. No individual upload request is
larger than 8 MiB. Uploaded chunks survive failed HTTP requests and API restarts
on the same container volume; the caller may retry an acknowledged chunk.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import time
from pathlib import Path

from cloud_render.concat.hybrid.contract import RENDER_MODES

CHUNK_BYTES = 8 * 1024 * 1024
MAX_BYTES = 4 * 1024 * 1024 * 1024
MAX_ACTIVE_UPLOADS = 10
MAX_JOBS = 20
ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
SHA = re.compile(r"^[a-f0-9]{64}$")


class UploadError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ResumableUpload:
    def __init__(self, worker):
        self.worker = worker
        self.uploads = worker.root / "uploads"
        if self.uploads.is_symlink():
            raise RuntimeError("Upload directory cannot be a symlink")
        self.uploads.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.uploads.chmod(0o700)

    def _id(self, job):
        if not isinstance(job, str) or not ID.fullmatch(job):
            raise UploadError(400, "Invalid job ID")
        return job

    def _directory(self, job):
        path = self.uploads / self._id(job)
        if path.is_symlink():
            raise UploadError(409, "Unsafe upload directory")
        return path

    def _manifest(self, job):
        root = self._directory(job)
        path = root / "manifest.json"
        if not path.is_file() or path.is_symlink():
            raise UploadError(404, "Unknown upload")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise UploadError(409, "Corrupted upload manifest") from None
        if (not isinstance(manifest, dict)
                or not isinstance(manifest.get("size"), int)
                or not 0 < manifest["size"] <= MAX_BYTES
                or not isinstance(manifest.get("sha256"), str)
                or not SHA.fullmatch(manifest["sha256"])):
            raise UploadError(409, "Invalid upload manifest")
        return manifest

    def _part_count(self, size):
        return (size + CHUNK_BYTES - 1) // CHUNK_BYTES

    def _status(self, job):
        m = self._manifest(job)
        root = self._directory(job)
        parts = self._part_count(m["size"])
        found = []
        for index in range(parts):
            path = root / f"part-{index:04d}.bin"
            expected = min(CHUNK_BYTES, m["size"] - index * CHUNK_BYTES)
            if path.is_file() and not path.is_symlink() and path.stat().st_size == expected:
                found.append(index)
        return {"job": job, "state": "uploading", "size": m["size"],
                "sha256": m["sha256"], "render_mode": m.get("render_mode", "native"),
                "chunk_size": CHUNK_BYTES,
                "total_chunks": parts, "uploaded": found, "remaining": parts - len(found)}

    def status(self, job):
        with self.worker.lock:
            return self._status(job)

    def begin(self, job, size, sha256, render_mode="cuda-stage1"):
        with self.worker.lock:
            self._id(job)
            if render_mode not in RENDER_MODES:
                raise UploadError(400, "Native Vulkan engine retired; use cuda-stage1 or cuda-hybrid-v1")
            if type(size) is not int or not 0 < size <= MAX_BYTES:
                raise UploadError(400, "Invalid ZIP size")
            if not isinstance(sha256, str) or not SHA.fullmatch(sha256):
                raise UploadError(400, "Invalid ZIP checksum")
            if self.worker._path(job).exists() or (self.worker.inputs / job).exists():
                raise UploadError(409, "Job ID already submitted")
            root = self._directory(job)
            if root.exists():
                manifest = self._manifest(job)
                if (manifest["size"] != size or manifest["sha256"] != sha256 or
                    manifest.get("render_mode", "native") != render_mode):
                    raise UploadError(409, "Existing upload does not match the requested ZIP")
                return self._status(job)
            unfinished = sum(path.is_dir() for path in self.uploads.iterdir())
            if unfinished >= MAX_ACTIVE_UPLOADS:
                raise UploadError(429, "Too many incomplete uploads")
            in_flight = sum(1 for path in self.worker.state.glob("*.json")
                            if self.worker._read_state(path) in ("queued", "running", "delivering"))
            if in_flight >= MAX_JOBS:
                raise UploadError(429, "Render queue is full")
            root.mkdir(mode=0o700)
            manifest = {"size": size, "sha256": sha256, "created": int(time.time()),
                        "render_mode": render_mode}
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return self._status(job)

    def put_chunk(self, job, index, stream, length, checksum):
        with self.worker.lock:
            status = self._status(job)
            if type(index) is not int or not 0 <= index < status["total_chunks"]:
                raise UploadError(400, "Invalid chunk index")
            expected = min(CHUNK_BYTES, status["size"] - index * CHUNK_BYTES)
            if type(length) is not int or length != expected:
                raise UploadError(400, "Chunk length does not match expected offset")
            if not isinstance(checksum, str) or not SHA.fullmatch(checksum):
                raise UploadError(400, "Invalid chunk SHA256")
            body = bytearray()
            while len(body) < expected:
                piece = stream.read(min(expected - len(body), 1024 * 1024))
                if not piece:
                    raise UploadError(400, "Incomplete chunk")
                body.extend(piece)
            digest = _sha(body)
            if not hmac.compare_digest(digest, checksum):
                raise UploadError(422, "Chunk SHA256 mismatch")
            root = self._directory(job)
            dest = root / f"part-{index:04d}.bin"
            if dest.exists():
                if dest.is_symlink() or dest.stat().st_size != expected:
                    raise UploadError(409, "Existing chunk has invalid size")
                if not hmac.compare_digest(_sha(dest.read_bytes()), checksum):
                    raise UploadError(409, "Existing chunk has different content")
                return self._status(job)
            temp = root / f".part-{index:04d}.tmp"
            # Remove an uncommitted part left by an interrupted write/restart.
            # Only an atomically renamed .bin file counts as verified.
            temp.unlink(missing_ok=True)
            try:
                with temp.open("xb") as output:
                    output.write(body)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, dest)
            finally:
                temp.unlink(missing_ok=True)
            return self._status(job)

    def complete(self, job):
        with self.worker.lock:
            self._id(job)
            if self.worker._path(job).exists():
                return self.worker.status(job)  # Idempotent after lost HTTP response
            status = self._status(job)
            # Do not silently convert an old native upload to CUDA. Keep
            # its original verified parts and require a new CUDA job ID.
            if status["render_mode"] not in RENDER_MODES:
                raise UploadError(409, "Legacy native Vulkan upload retired; existing chunks preserved")
            if status["remaining"]:
                raise UploadError(409, f"Missing {status['remaining']} ZIP chunks")
            root = self._directory(job)
            directory = self.worker.inputs / job
            if directory.exists() or directory.is_symlink():
                raise UploadError(409, "Target job directory exists")
            directory.mkdir(mode=0o700)
            target = directory / "project.zip"
            staged = directory / "project.zip.tmp"
            try:
                digest = hashlib.sha256()
                length = 0
                with staged.open("xb") as output:
                    for index in range(status["total_chunks"]):
                        source = root / f"part-{index:04d}.bin"
                        if source.is_symlink() or not source.is_file():
                            raise UploadError(409, "Missing or unsafe ZIP chunk")
                        with source.open("rb") as stream:
                            for piece in iter(lambda: stream.read(1024 * 1024), b""):
                                digest.update(piece)
                                output.write(piece)
                                length += len(piece)
                    output.flush()
                    os.fsync(output.fileno())
                if length != status["size"] or not hmac.compare_digest(digest.hexdigest(), status["sha256"]):
                    raise UploadError(422, "Assembled ZIP SHA256 mismatch")
                os.replace(staged, target)
                self.worker._save(job, {"job": job, "state": "queued",
                     "size": length, "sha256": status["sha256"],
                     "created": int(time.time()), "error": "",
                     "render_mode": status.get("render_mode", "native")})
                self.worker.pool.submit(self.worker._process, job)
            except BaseException:
                if not self.worker._path(job).exists():
                    shutil.rmtree(directory)
                raise
            # Only delete resumable source after full verification and queue acceptance.
            shutil.rmtree(root)
            return self.worker.status(job)
