"""Read-only dashboard composition of verified owner-scoped Concat/Uploader data.

Adapters are registered by Agents 2 and 3 on app.state. This module never
reads the upstream ownerless SQLite stores, accepts an owner query parameter,
or calls legacy global statistics endpoints.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
import inspect
import re
from typing import Any

from starlette.concurrency import run_in_threadpool

_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_RENDER_STATES = {"pending", "queued", "downloading", "rendering", "uploading to gpu",
                  "downloading output", "completed", "failed", "cancelled", "canceled"}
_UPLOAD_STATES = {"pending", "queued", "uploading", "scheduled", "processing",
                  "completed", "failed", "cancelled", "canceled", "waiting network"}
_QUEUE_STATES = {"running", "stopped", "paused", "idle", "disabled", "unknown"}


class SourceUnavailable(RuntimeError):
    """A required owner-safe producer is not registered or not reachable."""


class InvalidSnapshot(ValueError):
    """Provider violated its owner, verification, or data-shape contract."""


def _owned(data: Any, username: str) -> Mapping:
    if not isinstance(data, Mapping):
        raise InvalidSnapshot("Expected a snapshot object")
    owner = data.get("owner_username")
    if not isinstance(owner, str) or owner.casefold() != username.casefold():
        raise InvalidSnapshot("Snapshot owner mismatch")
    return data


def _natural(data: Mapping, key: str) -> int:
    value = data.get(key)
    if type(value) is not int or value < 0:
        raise InvalidSnapshot("Missing or invalid aggregate count")
    return value


def _totals(data: Any) -> dict:
    if not isinstance(data, Mapping):
        raise InvalidSnapshot("Missing owner-scoped totals")
    result = {key: _natural(data, key) for key in
              ("total", "completed", "queued", "active", "failed")}
    if result["completed"] > result["total"]:
        raise InvalidSnapshot("Completed exceeds total")
    return result


def _string(value: Any, limit: int = 180) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise InvalidSnapshot("Missing or invalid record field")
    if any(ord(char) < 32 for char in value):
        raise InvalidSnapshot("Control characters in record field")
    return value


def _when(value: Any) -> str | None:
    if value is None:
        return None
    raw = _string(value, 48)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Timezone required")
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise InvalidSnapshot("Invalid activity timestamp") from exc


def _records(value: Any, username: str, limit: int, *, kind: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > limit:
        raise InvalidSnapshot("Records must be a bounded list")
    output = []
    for item in value:
        row = _owned(item, username)
        status = _string(row.get("status" if kind != "video" else "state"), 48).lower()
        if kind == "render" and status not in _RENDER_STATES:
            raise InvalidSnapshot("Unknown render state")
        if kind == "upload" and status not in _UPLOAD_STATES:
            raise InvalidSnapshot("Unknown upload state")
        if kind == "video":
            if status != "ready" or not isinstance(row.get("sha256"), str) or not _SHA256.fullmatch(row["sha256"]):
                raise InvalidSnapshot("Media must be verified and ready")
        entry = {
            "id": _string(str(row.get("id")) if row.get("id") is not None else None, 128),
            "title": _string(row.get("name") or row.get("title") or row.get("id"), 180),
            "status": status,
            "updated_at": _when(row.get("updated_at") or row.get("created_at")),
        }
        output.append(entry)
    return output


def _queue(value: Any) -> dict:
    if not isinstance(value, Mapping):
        raise InvalidSnapshot("Missing queue status")
    state = _string(value.get("state"), 48).lower()
    if state not in _QUEUE_STATES:
        raise InvalidSnapshot("Unknown queue state")
    return {"state": state, "queued": _natural(value, "queued"),
            "active": _natural(value, "active")}


def _gpu(value: Any) -> dict:
    if not isinstance(value, Mapping):
        raise InvalidSnapshot("Missing GPU status")
    available = value.get("available")
    if available is not None and type(available) is not bool:
        raise InvalidSnapshot("GPU available must be boolean or unknown")
    return {"available": available, "status": _string(value.get("status"), 80),
            "checked_at": _when(value.get("checked_at"))}


async def _fetch(source: Any, username: str, limit: int) -> Mapping:
    fn = getattr(source, "dashboard_snapshot", None)
    if not callable(fn):
        raise SourceUnavailable("Dashboard provider not registered")
    try:
        if inspect.iscoroutinefunction(fn):
            result = await fn(owner_username=username, limit=limit)
        else:
            result = await run_in_threadpool(fn, owner_username=username, limit=limit)
            if inspect.isawaitable(result):
                result = await result
    except (SourceUnavailable, InvalidSnapshot):
        raise
    except Exception as exc:
        raise SourceUnavailable("Dashboard provider unavailable") from exc
    return _owned(result, username)


def _activity(renders: list[dict], uploads: list[dict], videos: list[dict],
              limit: int) -> list[dict]:
    combined = [
        {"kind": kind, **row}
        for kind, rows in (("render", renders), ("upload", uploads), ("video", videos))
        for row in rows
    ]
    return sorted(combined, key=lambda row: row["updated_at"] or "", reverse=True)[:limit]


async def get_overview(app: Any, owner_username: str, *, limit: int = 8) -> dict:
    """Compose two independently owner-filtered producer snapshots.

    Providers MUST use SQL/engine predicates on the supplied owner_username
    for totals and rows. Owner fields checked here are a second safety gate,
    not a substitute for query-layer isolation.
    """
    if not (1 <= limit <= 25):
        raise ValueError("Dashboard limit out of range")
    scene = getattr(app.state, "concat_dashboard_provider", None)
    uploader = getattr(app.state, "uploader_dashboard_provider", None)
    if scene is None or uploader is None:
        raise SourceUnavailable("Owner-scoped dashboard providers not ready")

    render_source, upload_source = await asyncio.gather(
        _fetch(scene, owner_username, limit),
        _fetch(uploader, owner_username, limit),
    )
    media = render_source.get("videos")
    if not isinstance(media, Mapping):
        raise InvalidSnapshot("Missing verified media summary")
    video_total = _natural(media, "total")
    render_jobs = _records(render_source.get("jobs"), owner_username, limit, kind="render")
    upload_jobs = _records(upload_source.get("jobs"), owner_username, limit, kind="upload")
    videos = _records(media.get("items"), owner_username, limit, kind="video")
    if len(videos) > video_total:
        raise InvalidSnapshot("Media rows exceed total")
    render_totals = _totals(render_source.get("totals"))
    upload_totals = _totals(upload_source.get("totals"))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "renders": {**render_totals, "queue": _queue(render_source.get("queue")),
                    "jobs": render_jobs},
        "videos": {"total": video_total, "items": videos},
        "uploads": {**upload_totals, "queue": _queue(upload_source.get("queue")),
                    "jobs": upload_jobs},
        "gpu": _gpu(render_source.get("gpu")),
        "recent_activity": _activity(render_jobs, upload_jobs, videos, limit),
    }
