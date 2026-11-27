"""Fail-closed, read-only overview of the four distinct Auto Uploader modes.

Do not open a different mode's uploader database with the active engine's ORM:
that would create tables, copy settings, and risk scheduling the wrong uploads.
No token values, stream keys, private paths, or row contents leave this module.
"""
from __future__ import annotations

import os
import sqlite3
import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

MODE_LABELS = {
    "engine1": "Engine 1",
    "engine2": "Engine 2",
    "shorts": "Short Uploader",
    "short_stream": "Short Stream",
}
UPLOAD_MODES = ("engine1", "engine2", "shorts")
TABLES = {
    "profiles": ("core", "auto_engine_profiles"),
    "channels": ("core", "channels"),
    "queue": ("core", "videos"),
    "schedules": ("core", "auto_engine_runs"),
    "upload_records": ("v6", "upload_records"),
}


def validate_mode(value: str) -> str:
    mode = (value or "engine1").strip().lower()
    if mode not in MODE_LABELS:
        raise ValueError("Unknown uploader mode")
    return mode


def _roots() -> dict[str, Path]:
    primary = Path(os.environ.get("LUM_DATA_DIR", "/var/lib/fresh-website/local-upload-manager/lum1"))
    parent = primary.parent
    raw = {
        "engine1": primary,
        "engine2": Path(os.environ.get("LUM_MODE_ENGINE2_ROOT", str(parent / "lum2"))),
        "shorts": Path(os.environ.get("LUM_MODE_SHORTS_ROOT", str(parent / "lum3"))),
        "short_stream": Path(os.environ.get("LUM_MODE_SHORT_STREAM_ROOT", "/var/lib/fresh-website/short-stream")),
    }
    # A separate mode MUST NOT silently reuse another mode's live database.
    canonical = {name: path.resolve(strict=False) for name, path in raw.items()}
    for first in UPLOAD_MODES:
        for second in UPLOAD_MODES:
            if first != second and canonical[first] == canonical[second]:
                raise RuntimeError("Uploader modes must have separate data roots")
    return canonical


def _read_counts(path: Path, names: tuple[str, ...]) -> dict[str, int] | None:
    if not path.is_file():
        return None
    # SQLite URI mode=ro never creates or upgrades a database, even with WAL.
    con = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=1)
    try:
        con.execute("PRAGMA query_only=ON")
        existing = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            name: int(con.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0])
            for name in names if name in existing
        }
    finally:
        con.close()


def _receipt_keys(path: Path) -> set[tuple[str, str]]:
    if not path.is_file():
        return set()
    con = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=1)
    try:
        con.execute("PRAGMA query_only=ON")
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='upload_records'").fetchone() is None:
            return set()
        out: set[tuple[str, str]] = set()
        for row in con.execute("SELECT channel_id, normalized_filename, script_key FROM upload_records"):
            channel = str(row[0] or "").strip()
            for name in row[1:]:
                key = str(name or "").strip().casefold()
                if channel and key:
                    out.add((channel, key))
        return out
    finally:
        con.close()


def registry() -> dict:
    roots = _roots()
    result = {}
    histories: dict[str, set[tuple[str, str]]] = {}
    for mode, label in MODE_LABELS.items():
        root = roots[mode]
        if mode == "short_stream":
            # The independent Short Stream app owns its own FFmpeg sessions.
            # Never return sessions.json: it can contain private stream keys.
            state = root / "sessions.json"
            sessions = None
            if state.is_file():
                try:
                    payload = json.loads(state.read_text(encoding="utf-8"))
                    sessions = len(payload) if isinstance(payload, (list, dict)) else None
                except (ValueError, OSError):
                    sessions = None
            result[mode] = {"label": label, "status": "separate_app",
                            "write_enabled": False, "sessions": sessions,
                            "message": "Separate Short Stream app; livestream state is not an Auto Uploader queue."}
            continue

        paths = {
            "core": root / "core" / "data" / "uploader.db",
            "v6": root / "auto_engine_v6" / "auto_engine_v6.sqlite3",
        }
        stats = {name: 0 for name in TABLES}
        observed = False
        try:
            for source in ("core", "v6"):
                requested = tuple(name for name, spec in TABLES.items() if spec[0] == source)
                counts = _read_counts(paths[source], tuple(TABLES[name][1] for name in requested))
                if counts is not None:
                    observed = True
                    for name in requested:
                        stats[name] = counts.get(TABLES[name][1], 0)
            histories[mode] = _receipt_keys(paths["v6"])
            status = "data_present" if observed else "not_provisioned"
        except (sqlite3.Error, OSError) as exc:
            status = "unavailable"
            # Fail closed: no DB mutation, no token/file/path details in the UI.
            histories[mode] = set()
        result[mode] = {
            "label": label,
            "status": status,
            "write_enabled": mode == "engine1" and observed,
            "stats": stats,
            "message": ("Production runtime managed by Engine 1" if mode == "engine1" else
                        "Isolated state inventory only. Uploads and schedules stay disabled until verified cutover."),
        }

    # A possible collision is reported, never silently discarded or uploaded.
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for mode, keys in histories.items():
        for key in keys:
            owners[key].add(mode)
    duplicate_keys = sum(1 for modes in owners.values() if len(modes) > 1)
    from .mode_coordinator import status as coordinator_status
    coordinator = coordinator_status()
    from .mode_runtime import probe as probe_mode_runtime
    for mode in MODE_LABELS:
        result[mode]["runtime"] = probe_mode_runtime(mode)
    return {"selected_modes": result, "cross_mode_record_collisions": duplicate_keys,
            "cross_mode_uploads_enabled": False,
            "coordinator": coordinator,
            "note": "Shared claim ledger is advisory until all legacy workers are explicitly wired and their histories reconciled. No cross-mode writer is enabled."}
