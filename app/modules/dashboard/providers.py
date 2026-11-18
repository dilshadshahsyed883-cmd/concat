"""Production dashboard adapters: read existing owner-scoped stores, never global counters.

Cloud Render's *tenant database path* is the owner predicate. The uploader's
persistent SQLite store requires an owner predicate on every SQL query.
These adapters do not create jobs, mutate the queue, or touch credentials.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import os
import re
import sqlite3

from app.modules.concat.tenant import owner_key
from app.modules.dashboard.services import SourceUnavailable

_DIGEST = re.compile(r"[a-fA-F0-9]{64}\Z")
_RENDER_STATES = {
    "Queued": "queued", "Waiting for Worker": "queued",
    "Downloading": "downloading", "Uploading to GPU": "uploading to gpu",
    "Rendering": "rendering", "Delivering": "downloading output",
    "Completed": "completed", "Failed": "failed", "Cancelled": "cancelled",
}
_UPLOAD_STATES = {
    "QUEUED": "queued", "WAITING_SLOT": "queued", "SCHEDULED": "scheduled",
    "DOWNLOADING": "processing", "SOURCE_READY": "processing",
    "UPLOAD_QUEUED": "queued", "UPLOADING": "uploading",
    "VERIFYING_REMOTE": "processing", "CLEANING": "processing",
    "COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "cancelled",
    "PAUSED": "pending", "WAITING_NETWORK": "waiting network",
    "WAITING_PROXY": "waiting network", "REVIEW_REQUIRED": "failed",
}


def _validate_owner(username: str) -> None:
    from app.modules.admin.security import _valid_username
    if not isinstance(username, str) or not _valid_username(username) or username.casefold() == "__default__":
        raise SourceUnavailable("Verified canonical owner is required")


def _database(path: Path):
    if path.is_symlink() or not path.is_file():
        raise SourceUnavailable("Owner database is unavailable")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _utc(value):
    if not value:
        return None
    raw = str(value).strip()
    if len(raw) == 19 and raw[10] == " ":
        return raw.replace(" ", "T") + "Z"  # SQLite CURRENT_TIMESTAMP is UTC
    return raw


def _totals(counts: dict[str, int], *, active: set[str], queued: set[str]):
    return {
        "total": sum(counts.values()),
        "completed": counts.get("completed", 0),
        "queued": sum(counts.get(x, 0) for x in queued),
        "active": sum(counts.get(x, 0) for x in active),
        "failed": counts.get("failed", 0),
    }


class ConcatDashboardProvider:
    def __init__(self, app):
        self.app = app

    def dashboard_snapshot(self, *, owner_username: str, limit: int) -> dict:
        _validate_owner(owner_username)
        if not 1 <= limit <= 25:
            raise ValueError("Invalid dashboard page size")
        root = Path(getattr(self.app.state, "concat_data_root", None)
                    or os.environ.get("CONCAT_WEB_DATA_ROOT", "/var/lib/concat-web"))
        home = root / "owners" / owner_key(owner_username)
        if not root.is_absolute() or any(p.is_symlink() for p in (root, root / "owners", home)):
            raise SourceUnavailable("Unsafe Concat owner directory")
        path = home / "cloud_render.sqlite3"
        totals = _totals({}, active=set(), queued=set())
        jobs = []
        videos = []
        queue_state = "stopped"
        gpu_status = "unknown"
        if path.exists():
            try:
                with closing(_database(path)) as con:
                    rows = con.execute("SELECT state, COUNT(*) n FROM production_jobs GROUP BY state").fetchall()
                    counts = {}
                    for row in rows:
                        state = _RENDER_STATES.get(row["state"])
                        if state is None:
                            raise SourceUnavailable("Unknown production state")
                        counts[state] = counts.get(state, 0) + int(row["n"])
                    totals = _totals(counts, active={"downloading", "uploading to gpu", "rendering", "downloading output"},
                                     queued={"queued"})
                    jobs = [
                        {"id": str(r["id"]), "owner_username": owner_username,
                         "name": r["name"], "status": _RENDER_STATES[r["state"]], "updated_at": None}
                        for r in con.execute(
                            "SELECT id,name,state FROM production_jobs ORDER BY id DESC LIMIT ?", (limit,)
                        ).fetchall()
                    ]
                    saved_queue = con.execute("SELECT val FROM config WHERE key='production_queue'").fetchone()
                    queue_state = saved_queue[0] if saved_queue and saved_queue[0] in {"running", "paused", "stopped"} else "stopped"
                    configured = con.execute("SELECT key,val FROM config WHERE key IN ('endpoint','enabled')").fetchall()
                    flags = {r["key"]: r["val"] for r in configured}
                    gpu_status = "configured (not probed)" if flags.get("endpoint") else "not configured"
                    # State=ready is written only after the real MP4 has passed
                    # ingestion SHA-256 or verified Production delivery checks.
                    for r in con.execute(
                        "SELECT id,name,size,sha256,created_at FROM files WHERE state='ready' "
                        "ORDER BY created_at DESC,id DESC"
                    ):
                        if not _DIGEST.fullmatch(str(r["sha256"])) or int(r["size"]) <= 0:
                            continue
                        local = home / "media" / (str(r["id"]) + ".mp4")
                        if local.is_symlink() or not local.is_file() or local.stat().st_size != int(r["size"]):
                            continue
                        videos.append({
                            "id": r["id"], "owner_username": owner_username,
                            "name": r["name"], "state": "ready",
                            "sha256": r["sha256"], "updated_at": _utc(r["created_at"]),
                        })
            except (sqlite3.Error, OSError, KeyError, TypeError, ValueError) as exc:
                raise SourceUnavailable("Concat owner store cannot be read") from exc
        return {
            "owner_username": owner_username, "totals": totals,
            "queue": {"state": queue_state, "queued": totals["queued"], "active": totals["active"]},
            "jobs": jobs, "videos": {"total": len(videos), "items": videos[:limit]},
            # A configured endpoint is NOT proof the Salad worker is online.
            "gpu": {"available": None, "status": gpu_status, "checked_at": None},
        }


class UploaderDashboardProvider:
    def __init__(self, app):
        self.app = app

    def dashboard_snapshot(self, *, owner_username: str, limit: int) -> dict:
        _validate_owner(owner_username)
        if not 1 <= limit <= 25:
            raise ValueError("Invalid dashboard page size")
        if os.environ.get("CONCAT_UPLOADER_ENABLED") != "1":
            raise SourceUnavailable("Owner uploader is not enabled")
        root = Path(getattr(self.app.state, "uploader_data_root", None)
                    or os.environ.get("LUM_DATA_DIR", ""))
        if not root.is_absolute() or root.is_symlink():
            raise SourceUnavailable("Uploader private directory is not configured")
        try:
            with closing(_database(root / "projects.sqlite3")) as con:
                # Ownership is mandatory in BOTH aggregates and row selection;
                # no admin or legacy ownerless fallback.
                clause = "owner_username=? COLLATE NOCASE AND archived_at IS NULL AND owner_username!='__default__'"
                grouped = con.execute(
                    "SELECT status,COUNT(*) n FROM projects WHERE " + clause + " GROUP BY status",
                    (owner_username,)
                ).fetchall()
                counts = {}
                for r in grouped:
                    state = _UPLOAD_STATES.get(r["status"])
                    if state is None:
                        raise SourceUnavailable("Unknown uploader queue state")
                    counts[state] = counts.get(state, 0) + int(r["n"])
                totals = _totals(
                    counts, active={"uploading", "processing"},
                    queued={"queued", "scheduled", "pending"},
                )
                jobs = [
                    {"id": str(r["id"]), "owner_username": owner_username,
                     "title": r["title"], "status": _UPLOAD_STATES[r["status"]],
                     "updated_at": _utc(r["updated_at"])}
                    for r in con.execute(
                        "SELECT id,title,status,updated_at FROM projects WHERE " + clause +
                        " ORDER BY id DESC LIMIT ?", (owner_username, limit)
                    ).fetchall()
                ]
                paused = con.execute(
                    "SELECT paused FROM copyright_owner_queues WHERE owner_username=? COLLATE NOCASE",
                    (owner_username,)
                ).fetchone()
                enabled = con.execute("SELECT value FROM settings WHERE key='engine_enabled'").fetchone()
                running = bool(paused and not int(paused["paused"]) and enabled and enabled["value"] == "1")
        except (sqlite3.Error, OSError, KeyError, TypeError, ValueError) as exc:
            raise SourceUnavailable("Uploader owner store cannot be read") from exc
        return {
            "owner_username": owner_username, "totals": totals,
            "queue": {"state": "running" if running else "stopped",
                      "queued": totals["queued"], "active": totals["active"]},
            "jobs": jobs,
        }
