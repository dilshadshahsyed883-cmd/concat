"""Optional cross-mode upload ownership ledger.

Engine 1, Engine 2 and Shorts have independent legacy SQLite stores.  This
module does not start a worker or alter any existing upload queue.  Claims
stay disabled until a host-side receipt reconciliation is completed.
Short Stream is an RTMP service, not an ordinary video upload worker.
"""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import quote

UPLOAD_MODES = frozenset({"engine1", "engine2", "shorts"})
BLOCKING_STATES = frozenset({"RESERVED", "COMPLETED", "AMBIGUOUS"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
  channel_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  script_key TEXT NOT NULL,
  mode TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('RESERVED','COMPLETED','AMBIGUOUS','FAILED_SAFE')),
  youtube_video_id TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (channel_id, filename),
  UNIQUE (channel_id, script_key),
  UNIQUE (claim_id)
);
"""


class AdmissionBlocked(RuntimeError):
    """A scheduling or duplicate-safety guard denied this operation."""


def _location() -> Path:
    configured = os.environ.get("LUM_MODE_COORDINATOR_DB", "").strip()
    if configured:
        return Path(configured)
    primary = Path(os.environ.get(
        "LUM_DATA_DIR", "/var/lib/fresh-website/local-upload-manager/lum1"
    ))
    return primary.parent / "shared-upload-ledger.sqlite3"


def _mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in UPLOAD_MODES:
        raise ValueError("Unknown upload mode (Short Stream is a separate app)")
    return mode


def _key(channel_id: str, filename: str) -> tuple[str, str, str]:
    channel = str(channel_id or "").strip()
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip().casefold()
    if not channel or not name or channel in {".", ".."} or name in {".", ".."}:
        raise ValueError("A real channel ID and video filename are required")
    if len(channel) > 256 or len(name) > 512 or "\x00" in channel or "\x00" in name:
        raise ValueError("Invalid upload identity")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    script_key = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-._") or name
    return channel, name, script_key


def _connect(*, write: bool) -> sqlite3.Connection:
    path = _location()
    if not write:
        if not path.is_file():
            raise FileNotFoundError("Shared ledger has not been provisioned")
        uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.execute("PRAGMA query_only=ON")
        return conn
    if not path.parent.is_dir():
        raise AdmissionBlocked("Host-side shared ledger directory is not provisioned")
    return sqlite3.connect(str(path), timeout=30, isolation_level=None)


def status() -> dict:
    """Sanitized read-only health, including missing schema and disabled state."""
    result = {
        "armed": os.environ.get("LUM_MULTI_MODE_CLAIMS_ARMED") == "1",
        "history_reconciled": False,
        "ledger_present": False,
        "claim_count": 0,
        "blocking_count": 0,
        "ready": False,
    }
    try:
        with _connect(write=False) as conn:
            existing = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"metadata", "claims"}.issubset(existing):
                return result
            result["ledger_present"] = True
            item = conn.execute(
                "SELECT value FROM metadata WHERE key='history_reconciled'"
            ).fetchone()
            result["history_reconciled"] = bool(item and item[0] == "1")
            result["claim_count"] = int(
                conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            )
            result["blocking_count"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM claims WHERE state IN "
                    "('RESERVED','COMPLETED','AMBIGUOUS')"
                ).fetchone()[0]
            )
    except (sqlite3.Error, OSError):
        return result
    result["ready"] = result["armed"] and result["history_reconciled"]
    return result


def _writable() -> sqlite3.Connection:
    if os.environ.get("LUM_MULTI_MODE_CLAIMS_ARMED") != "1":
        raise AdmissionBlocked("Cross-mode publishing remains explicitly disabled")
    conn = _connect(write=True)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def reconcile_receipts(receipts: list[tuple[str, str, str]], *, verified_modes: set[str]) -> int:
    """One-shot historical import after external read-only verification of ALL modes.

    Receipts are (mode, real YouTube channel ID, filename), produced by a
    separate trusted host-side inventory.  Conflicting history is retained as
    COMPLETED, so the same channel/filename can never be claimed again.
    No upload is triggered.  Re-running cannot erase existing claims.
    """
    if verified_modes != UPLOAD_MODES:
        raise AdmissionBlocked("Every upload mode must be inventoried first")
    normalized = [(_mode(mode), *_key(channel, filename)) for mode, channel, filename in receipts]
    with _writable() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT value FROM metadata WHERE key='history_reconciled'"
            ).fetchone()
            if existing and existing[0] == "1":
                raise AdmissionBlocked("Historical reconciliation is already sealed")
            for mode, channel, filename, script_key in normalized:
                row = conn.execute(
                    "SELECT state FROM claims WHERE channel_id=? AND (filename=? OR script_key=?)",
                    (channel, filename, script_key),
                ).fetchone()
                if row:
                    if row[0] != "COMPLETED":
                        raise AdmissionBlocked("A pending ledger claim needs resolution")
                    continue
                conn.execute(
                    "INSERT INTO claims(channel_id,filename,script_key,mode,claim_id,state) "
                    "VALUES(?,?,?,?,?, 'COMPLETED')",
                    (channel, filename, script_key, mode, uuid.uuid4().hex),
                )
            conn.execute(
                "INSERT INTO metadata(key,value) VALUES('history_reconciled','1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return len(normalized)


def reserve(mode: str, channel_id: str, filename: str) -> str:
    """Atomically claim the same channel+filename across all three upload modes."""
    mode = _mode(mode)
    channel, name, script_key = _key(channel_id, filename)
    with _writable() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            sealed = conn.execute(
                "SELECT value FROM metadata WHERE key='history_reconciled'"
            ).fetchone()
            if not sealed or sealed[0] != "1":
                raise AdmissionBlocked("Historical receipts must be reconciled first")
            row = conn.execute(
                "SELECT state FROM claims WHERE channel_id=? AND (filename=? OR script_key=?)",
                (channel, name, script_key),
            ).fetchone()
            if row and row[0] in BLOCKING_STATES:
                raise AdmissionBlocked("Channel/video is already owned or published")
            claim_id = uuid.uuid4().hex
            if row:
                conn.execute(
                    "UPDATE claims SET mode=?, claim_id=?, filename=?, script_key=?, state='RESERVED', "
                    "youtube_video_id='', updated_at=CURRENT_TIMESTAMP "
                    "WHERE channel_id=? AND (filename=? OR script_key=?) AND state='FAILED_SAFE'",
                    (mode, claim_id, name, script_key, channel, name, script_key),
                )
            else:
                conn.execute(
                    "INSERT INTO claims(channel_id,filename,script_key,mode,claim_id,state) "
                    "VALUES(?,?,?,?,?, 'RESERVED')",
                    (channel, name, script_key, mode, claim_id),
                )
            conn.commit()
            return claim_id
        except BaseException:
            conn.rollback()
            raise


def settle(claim_id: str, *, mode: str, outcome: str,
           youtube_video_id: str = "", remote_started: bool = False) -> None:
    """Only the current owner can settle; uncertain remotes remain blocked."""
    mode = _mode(mode)
    if outcome not in {"COMPLETED", "AMBIGUOUS", "FAILED_SAFE"}:
        raise ValueError("Unknown claim outcome")
    if outcome == "COMPLETED" and not str(youtube_video_id).strip():
        raise ValueError("Completed publication requires a real YouTube video ID")
    if outcome == "FAILED_SAFE" and remote_started:
        raise AdmissionBlocked("Remote activity requires AMBIGUOUS reconciliation")
    with _writable() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = conn.execute(
                "UPDATE claims SET state=?, youtube_video_id=?, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE claim_id=? AND mode=? AND state='RESERVED'",
                (outcome, str(youtube_video_id).strip(), claim_id, mode),
            )
            if result.rowcount != 1:
                raise AdmissionBlocked("Claim is missing, settled, or owned by another mode")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
