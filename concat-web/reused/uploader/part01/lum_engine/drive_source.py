from __future__ import annotations
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import gdown

from .config import STAGING_ROOT

_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def extract_drive_file_id(url: str) -> str:
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Google Drive link must start with http:// or https://")
    host = (parsed.hostname or "").lower()
    if host not in {"drive.google.com", "docs.google.com"}:
        raise ValueError("Only Google Drive file links are supported in the current source mode")
    parts = [p for p in parsed.path.split("/") if p]
    if "d" in parts:
        idx = parts.index("d")
        if idx + 1 < len(parts) and _DRIVE_ID.fullmatch(parts[idx + 1]):
            return parts[idx + 1]
    query_id = parse_qs(parsed.query).get("id", [""])[0]
    if _DRIVE_ID.fullmatch(query_id or ""):
        return query_id
    raise ValueError("Could not extract a Google Drive file ID from the link")


def stage_drive_file(project_id: int, drive_url: str) -> Path:
    file_id = extract_drive_file_id(drive_url)
    job_dir = STAGING_ROOT / str(int(project_id))
    job_dir.mkdir(parents=True, exist_ok=True)
    # Remove only unfinished temporary files inside this project's isolated folder.
    for path in job_dir.glob("*.part"):
        try:
            path.unlink()
        except OSError:
            pass
    result = gdown.download(id=file_id, output=str(job_dir) + "/", quiet=True, use_cookies=False)
    if not result:
        raise RuntimeError("Google Drive download failed. Confirm the file link is accessible to the VPS.")
    path = Path(result)
    if not path.is_absolute():
        path = job_dir / path
    path = path.resolve()
    if job_dir.resolve() not in path.parents:
        raise RuntimeError("Drive downloader returned a path outside the project staging directory")
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("Drive download did not produce a valid file")
    return path


def cleanup_project_staging(project_id: int, keep: set[Path] | None = None) -> None:
    job_dir = STAGING_ROOT / str(int(project_id))
    if not job_dir.exists():
        return
    keep_resolved = {p.resolve() for p in (keep or set()) if p.exists()}
    for child in list(job_dir.iterdir()):
        try:
            if child.resolve() in keep_resolved:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except FileNotFoundError:
            pass
    try:
        if not any(job_dir.iterdir()):
            job_dir.rmdir()
    except OSError:
        pass
