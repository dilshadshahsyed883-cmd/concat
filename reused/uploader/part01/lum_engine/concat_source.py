"""Owner-verified Cloud Render MP4 handoff for the Concat Uploader.

The browser URL is solely an identifier. Never request it over HTTP or use a
user-supplied filesystem path. Legacy ownerless media is deliberately denied.
This independent application uses owner-keyed Cloud Render directories. Legacy
global Scene media cannot be adopted.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlsplit, unquote

LINK_RE = re.compile(r"^/api/cloud-render/media/([a-zA-Z0-9_-]{1,128})/file$")
SHA_RE = re.compile(r"^[a-fA-F0-9]{64}$")
MAX_MP4 = 12 * 1024 ** 3


class ConcatSourceError(ValueError):
    """Safe admission failure, never a reason to fall back to remote download."""


def _trusted_origin(origin: str):
    trusted = urlsplit(origin)
    if (trusted.scheme != "https" or not trusted.netloc
            or trusted.username or trusted.password or trusted.query or trusted.fragment
            or trusted.path not in {"", "/"}):
        raise ConcatSourceError("Invalid Concat website origin")
    return trusted


def parse_concat_link(link: str, *, origin: str = "https://concat.scenebuilder.online") -> str:
    raw = str(link or "").strip()
    if not raw or len(raw) > 2048 or any(ord(c) < 32 for c in raw) or "\\" in raw:
        raise ConcatSourceError("A valid Cloud Render Copy Video Link is required")
    url = urlsplit(raw)
    trusted = _trusted_origin(origin)
    if (url.scheme != "https" or url.netloc.lower() != trusted.netloc.lower()
            or url.username or url.password or url.fragment or url.query):
        raise ConcatSourceError("Only the current website's Cloud Render video links are accepted")
    if "%" in url.path or unquote(url.path) != url.path:
        raise ConcatSourceError("Encoded Cloud Render paths are not accepted")
    match = LINK_RE.fullmatch(url.path)
    if not match:
        raise ConcatSourceError("Link must point to a completed Cloud Render MP4")
    return match.group(1)


def _owned_verified_file(con: sqlite3.Connection, job: str) -> dict:
    """Use only verified media in a server-selected, owner-specific database."""
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(files)")}
    if not {"id", "name", "size", "sha256", "state"} <= columns:
        raise ConcatSourceError("Cloud Render owner schema is unavailable")
    row = con.execute(
        "SELECT id,name,size,sha256,state FROM files WHERE id=?", (job,)
    ).fetchone()
    if not row:
        raise ConcatSourceError("Video is unavailable in this account")
    info = dict(row)
    if (info["state"] != "ready" or not 0 < int(info["size"]) <= MAX_MP4
            or not SHA_RE.fullmatch(str(info["sha256"]))):
        raise ConcatSourceError("Cloud Render video is not a verified ready MP4")
    return info


def stage_concat_link(link: str, owner: str, staging_root: Path, *,
                      cloud_root: Path, origin: str = "https://concat.scenebuilder.online") -> tuple[str, str, int, str]:
    """Return a *private copy* (path, media_name, byte_count, SHA-256).

    Never hardlink Cloud Render media into a YouTube worker. A hardlink shares
    a mutable inode: even after a checksum passes, renderer/retention changes
    to the original could silently change uploaded bytes. Copy, fsync, verify,
    and leave the canonical Cloud Render / Drive original untouched.
    """
    job = parse_concat_link(link, origin=origin)
    database, media_root = _verified_owner_storage(owner, cloud_root)
    destination_root = Path(staging_root)
    if not destination_root.is_absolute() or any(p.is_symlink() for p in (destination_root, *destination_root.parents)):
        raise ConcatSourceError("Verified storage configuration is unavailable")
    # Never inspect root/cloud_render.sqlite3 from the ownerless Scene system.
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        info = _owned_verified_file(con, job)
    source = media_root / f"{job}.mp4"
    if (not source.is_file() or source.is_symlink()
            or source.resolve().parent != media_root.resolve()):
        # Never treat a Drive URL/path as permission to fetch arbitrary content.
        raise ConcatSourceError("Local MP4 is unavailable; authorized Drive handoff is required")
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected_size = int(info["size"])
    # Guard precious VPS storage. No staging job may exhaust the filesystem.
    if shutil.disk_usage(destination_root).free < expected_size + 1024 * 1024 * 1024:
        raise ConcatSourceError("Insufficient safe VPS staging capacity for this MP4")
    destination = destination_root / ("concat_" + uuid.uuid4().hex + ".mp4")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, flags)
        with os.fdopen(source_fd, "rb") as inp:
            stat_info = os.fstat(inp.fileno())
            if not stat.S_ISREG(stat_info.st_mode) or stat_info.st_size != expected_size:
                raise ConcatSourceError("Cloud Render media changed before secure handoff")
            if inp.read(8)[4:8] != b"ftyp":
                raise ConcatSourceError("Cloud Render video is not an MP4")
            inp.seek(0)
            fd = os.open(destination,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = inp.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size:
                        raise ConcatSourceError("Cloud Render media size changed")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        if total != expected_size or destination.stat().st_size != expected_size:
            raise ConcatSourceError("Cloud Render media size changed")
        if digest.hexdigest().lower() != str(info["sha256"]).lower():
            raise ConcatSourceError("Cloud Render media checksum changed")
        return str(destination), str(info["name"]), total, digest.hexdigest()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _verified_owner_storage(owner: str, cloud_root: Path) -> tuple[Path, Path]:
    """Resolve only an authenticated owner's Cloud Render storage; never legacy global data."""
    if (not isinstance(owner, str) or not owner or owner != owner.strip()
            or len(owner) > 128 or any(c.isspace() or c in "/\\:" or ord(c) < 32 for c in owner)):
        raise ConcatSourceError("Verified owner identity is required")
    root = Path(cloud_root)
    if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ConcatSourceError("Verified Cloud Render configuration is unavailable")
    owners = root / "owners"
    owner_root = owners / hashlib.sha256(owner.encode("utf-8")).hexdigest()
    database = owner_root / "cloud_render.sqlite3"
    media = owner_root / "media"
    if (owners.is_symlink() or owner_root.is_symlink() or
            not owner_root.is_dir() or not database.is_file() or database.is_symlink()
            or not media.is_dir() or media.is_symlink()):
        raise ConcatSourceError("Cloud Render verified owner storage is unavailable")
    return database, media


def list_available_concat_media(owner: str, *, cloud_root: Path, origin: str,
                                reserved_ids: set[str], reserved_sha: set[str]) -> list[dict]:
    """Ready, physically present, owner-verified MP4s not claimed by an upload."""
    database, media = _verified_owner_storage(owner, cloud_root)
    _trusted_origin(origin)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id,name,size,sha256 FROM files WHERE state='ready' ORDER BY rowid DESC"
        ).fetchall()
    result = []
    for row in rows:
        job, digest = str(row["id"]), str(row["sha256"]).lower()
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job) or
                job in reserved_ids or digest in reserved_sha or
                not SHA_RE.fullmatch(digest)):
            continue
        source = media / f"{job}.mp4"
        try:
            size = int(row["size"])
            if not 0 < size <= MAX_MP4 or source.is_symlink():
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(source, flags), "rb") as video:
                info = os.fstat(video.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size != size
                        or video.read(8)[4:8] != b"ftyp"):
                    continue
        except (OSError, TypeError, ValueError):
            # A deleted or malformed row must not break the whole picker.
            continue
        result.append({
            "id": job, "name": str(row["name"]), "size": size,
            "link": origin.rstrip("/") + f"/api/cloud-render/media/{job}/file",
        })
        if len(result) >= 500:
            break
    return result


def delete_verified_concat_media(job: str, owner: str, fingerprint: str, *,
                                 cloud_root: Path) -> bool:
    """Delete only the exact owned MP4 following independently verified YouTube success.

    SQLite's write lock prevents a concurrent Cloud Render Drive transition.
    A missing file/row is an idempotent success after a prior interrupted cleanup.
    A changed file is *never* deleted.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(job or "")):
        raise ConcatSourceError("Invalid Concat media ID for cleanup")
    if not SHA_RE.fullmatch(str(fingerprint or "")):
        raise ConcatSourceError("Verified Concat fingerprint is required for cleanup")
    database, media = _verified_owner_storage(owner, cloud_root)
    source = media / f"{job}.mp4"
    with sqlite3.connect(database, timeout=30) as con:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT state,size,sha256 FROM files WHERE id=?", (job,)).fetchone()
        if row is None:
            if source.exists() or source.is_symlink():
                raise ConcatSourceError("Concat media metadata missing; original retained for review")
            return False
        if str(row["sha256"]).lower() != fingerprint.lower():
            raise ConcatSourceError("Concat media changed; refusing cleanup")
        if row["state"] not in {"ready", "failed", "delivered"}:
            raise ConcatSourceError("Concat media is busy; cleanup must retry")
        if source.is_symlink():
            raise ConcatSourceError("Unsafe symlink at Concat media cleanup path")
        if source.exists():
            if not source.is_file() or source.resolve().parent != media.resolve():
                raise ConcatSourceError("Unsafe Concat media cleanup path")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            digest = hashlib.sha256()
            with os.fdopen(os.open(source, flags), "rb") as video:
                before = os.fstat(video.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != int(row["size"]):
                    raise ConcatSourceError("Concat media size changed; refusing cleanup")
                for chunk in iter(lambda: video.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
                current = source.stat(follow_symlinks=False)
                if (current.st_ino != before.st_ino or current.st_dev != before.st_dev
                        or current.st_size != before.st_size or current.st_mtime_ns != before.st_mtime_ns):
                    raise ConcatSourceError("Concat media changed during cleanup")
            if digest.hexdigest() != fingerprint.lower():
                raise ConcatSourceError("Concat media checksum changed; refusing cleanup")
            source.unlink()
            deleted = True
        else:
            deleted = False
        con.execute("DELETE FROM files WHERE id=?", (job,))
    return deleted
