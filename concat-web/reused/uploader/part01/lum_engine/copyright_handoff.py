"""Owner-scoped VPS handoff. Never follow links or delete unverified media."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from .config import DATA_ROOT

ROOT = Path(os.environ.get("COPYRIGHT_MEDIA_ROOT", str(DATA_ROOT / "copyright_handoff")))
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov"})


def owner_root(owner: str) -> Path:
    name = str(owner or "").strip().lower()
    if not name:
        raise ValueError("Login is required for Copyright Uploader folder")
    return ROOT / hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]


def safe_relative_folder(folder: str = "") -> str:
    raw = str(folder or "").strip().replace("\\", "/")
    if not raw:
        return ""
    pieces = raw.split("/")
    if (len(pieces) > 5 or
            any(not p or p in (".", "..") or p.startswith(".") or
                "\x00" in p or len(p) > 120 for p in pieces)):
        raise ValueError("Invalid VPS folder selection")
    return "/".join(pieces)


def owner_folder(owner: str, folder: str = "") -> Path:
    if ROOT.is_symlink():
        raise ValueError("Unsafe shared handoff root")
    target = owner_root(owner)
    if target.is_symlink():
        raise ValueError("Unsafe account handoff root")
    for segment in safe_relative_folder(folder).split("/") if folder else []:
        target = target / segment
        if target.is_symlink():
            raise ValueError("Unsafe account handoff folder")
    return target


def available_folders(owner: str) -> list[str]:
    root = owner_folder(owner)
    if not root.is_dir():
        return [""]
    found = [""]
    # Directory discovery is owner-local and read-only. Ignore symlinks.
    stack = [("", root, 0)]
    while stack and len(found) < 250:
        rel, current, depth = stack.pop()
        if depth >= 5:
            continue
        for child in current.iterdir():
            if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
                continue
            path = (rel + "/" if rel else "") + child.name
            try:
                safe_relative_folder(path)
            except ValueError:
                continue
            found.append(path)
            stack.append((path, child, depth + 1))
    return [""] + sorted(set(found[1:]), key=str.casefold)


def valid_name(filename: str) -> bool:
    value = str(filename or "")
    return (bool(value) and value == Path(value).name and not value.startswith(".")
            and Path(value).suffix.lower() in VIDEO_EXTENSIONS)


def available_videos(owner: str, claimed: set[str] | None = None, folder: str = "") -> list[str]:
    directory = owner_folder(owner, folder)
    if not directory.is_dir():
        return []
    blocked = {name.casefold() for name in (claimed or set())}
    result = []
    for path in directory.iterdir():
        if (valid_name(path.name) and path.name.casefold() not in blocked
                and not path.is_symlink() and path.is_file()
                and path.stat().st_size > 0):
            result.append(path.name)
    return sorted(result, key=str.casefold)[:500]


def _verified_unlink(source: Path, expected_sha256: str, parent: Path) -> bool:
    if (not isinstance(expected_sha256, str) or
            not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)):
        raise ValueError("Missing verified file SHA256")
    if source.is_symlink() or parent.is_symlink():
        raise RuntimeError("Refusing linked VPS media")
    if not source.exists():
        return False
    if not source.is_file() or source.resolve().parent != parent.resolve():
        raise RuntimeError("VPS media escaped its protected folder")
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.stat(source, follow_symlinks=False)
        if (before.st_ino != after.st_ino or before.st_dev != after.st_dev
                or before.st_size != after.st_size or digest.hexdigest() != expected_sha256.lower()):
            raise RuntimeError("VPS media changed since queue admission; cleanup refused")
        source.unlink()
        if source.exists() or source.is_symlink():
            raise RuntimeError("VPS source still exists after verified cleanup")
        return True
    finally:
        os.close(fd)


def delete_verified_source(owner: str, filename: str, expected_sha256: str,
                           folder: str = "") -> bool:
    """Only a verified remote upload caller may delete this owner handoff."""
    if not valid_name(filename):
        raise ValueError("Invalid verified handoff file")
    directory = owner_folder(owner, folder)
    return _verified_unlink(directory / filename, expected_sha256, directory)


def delete_verified_merger_output(owner: str, filename: str, expected_sha256: str) -> bool:
    """Remove a matching Video Merger retained copy, if this is its completed job.

    Cross-app cleanup requires an independent owner+job+checksum DB match.
    Legacy/non-Merger inputs are left untouched. No shared Drive files are opened.
    """
    match = re.fullmatch(r"([0-9a-f]{12})__(.+)", str(filename or ""))
    if not match:
        return False
    job_id, original_name = match.groups()
    # Runtime config mounts COPYRIGHT_MEDIA_ROOT under the live Video Merger state root.
    # Never guess /opt/video-merger, which differs from the guarded VPS deployment.
    base = ROOT.parent
    database = base / "data" / "video_merger.db"
    if not database.is_file() or database.is_symlink():
        raise RuntimeError("Video Merger job inventory unavailable; retained output preserved")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id,owner_username,filename,status,local_path FROM jobs WHERE id=?",
            (job_id,)).fetchone()
    if row is None or str(row["owner_username"]).casefold() != str(owner).strip().casefold():
        raise RuntimeError("Video Merger retained copy ownership could not be verified")
    if row["status"] != "complete" or row["filename"] != original_name:
        raise RuntimeError("Video Merger output is not verified and complete")
    parent = base / "verified_outputs" / job_id
    expected = parent / original_name
    if (parent.is_symlink() or expected.is_symlink() or
            str(expected) != str(row["local_path"]) or
            expected.name != original_name or not valid_name(original_name)):
        raise RuntimeError("Video Merger retained output path is not trusted")
    return _verified_unlink(expected, expected_sha256, parent)
