from __future__ import annotations
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import BUILD, DATA_ROOT, REPORTS_ROOT, ensure_dirs


def _safe_name(name: str) -> str:
    value = Path(str(name or "")).name
    if not value.lower().endswith(".txt"):
        raise ValueError("Only .txt reports are available")
    return value


def list_reports(limit: int = 100) -> list[dict]:
    ensure_dirs()
    rows = []
    for path in REPORTS_ROOT.glob("*.txt"):
        try:
            stat = path.stat()
            rows.append({"name": path.name, "size": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
        except OSError:
            continue
    rows.sort(key=lambda x: x["modified_at"], reverse=True)
    return rows[:max(1, min(int(limit), 500))]


def report_path(name: str) -> Path:
    safe = _safe_name(name)
    path = (REPORTS_ROOT / safe).resolve()
    if REPORTS_ROOT.resolve() not in path.parents or not path.is_file():
        raise FileNotFoundError(safe)
    return path


def generate_health_report(store, db, worker) -> Path:
    ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = REPORTS_ROOT / f"LUM_HEALTH_{stamp}.txt"
    counts = store.counts()
    try:
        channels = db.connect().execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    except Exception:
        channels = "ERROR"
    total, used, free = shutil.disk_usage(DATA_ROOT)
    lines = [
        "LOCAL UPLOAD MANAGER HEALTH REPORT",
        f"UTC: {datetime.now(timezone.utc).isoformat()}",
        f"Build: {BUILD}",
        f"Python: {sys.version.split()[0]}",
        f"PID: {os.getpid()}",
        f"Worker alive: {bool(worker.thread and worker.thread.is_alive())}",
        f"Active project: {worker.active_project_id or 'none'}",
        "Serial concurrency: 1",
        f"Remote verification: {store.setting('remote_verification_enabled','1')}",
        f"Verified cleanup: {store.setting('delete_staged_after_verified','1')}",
        "Drive source delete: 0",
        f"Channels: {channels}",
        f"Queue counts: {counts}",
        f"Data root: {DATA_ROOT}",
        f"Disk total bytes: {total}",
        f"Disk used bytes: {used}",
        f"Disk free bytes: {free}",
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
