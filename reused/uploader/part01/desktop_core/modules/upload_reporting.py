from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from .path_utils import DATA_DIR, ensure_app_dirs
from .upload_status import UploadStatus

UPLOADED_LINKS_CSV_PATH = DATA_DIR / "uploaded_video_links.csv"
COMPLETED_STATUSES = {UploadStatus.UPLOADED.value, UploadStatus.PUBLISHED.value}
AUTO_ENGINE_REPORT_DIR = DATA_DIR / "auto_engine_reports"
AUTO_ENGINE_REPORT_PATH = AUTO_ENGINE_REPORT_DIR / "auto_engine_completed_uploads.txt"


def export_uploaded_links_csv(db: Any, csv_path: Optional[Path] = None) -> Path:
    """Refresh a compact Excel-compatible CSV of successful uploads."""
    ensure_app_dirs()
    out = csv_path or UPLOADED_LINKS_CSV_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        row for row in db.list_videos()
        if str(row.get("status") or "").upper() in COMPLETED_STATUSES
        and str(row.get("youtube_url") or "").strip()
    ]
    rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""))
    headers = [
        "id", "uploaded_at", "status", "channel_name", "channel_id", "title",
        "youtube_url", "youtube_video_id", "privacy_status", "video_file",
        "thumbnail_file", "group_name", "created_at", "scheduled_app_upload_at",
        "retry_count", "error_message",
    ]
    with out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "id": row.get("id"),
                "uploaded_at": row.get("updated_at") or "",
                "status": row.get("status") or "",
                "channel_name": row.get("channel_name") or "",
                "channel_id": row.get("youtube_channel_id") or "",
                "title": row.get("title") or "",
                "youtube_url": row.get("youtube_url") or "",
                "youtube_video_id": row.get("youtube_video_id") or "",
                "privacy_status": row.get("privacy_status") or "",
                "video_file": row.get("local_video_path") or "",
                "thumbnail_file": row.get("thumbnail_path") or "",
                "group_name": row.get("group_name") or "",
                "created_at": row.get("created_at") or "",
                "scheduled_app_upload_at": row.get("app_upload_at") or "",
                "retry_count": row.get("retry_count") or 0,
                "error_message": row.get("error_message") or "",
            })
    return out


def update_auto_engine_completed_uploads_txt(db: Any, video_id: int, report_path: Optional[Path] = None) -> Path:
    """Atomically add one confirmed successful Auto Engine upload to the master TXT report."""
    ensure_app_dirs()
    row = db.get_video(int(video_id))
    if not row:
        raise ValueError(f"Video queue item not found: {video_id}")

    status = str(row.get("status") or "").strip().upper()
    youtube_url = str(row.get("youtube_url") or "").strip()
    youtube_video_id = str(row.get("youtube_video_id") or "").strip()
    auto_engine_profile_id = row.get("auto_engine_profile_id")
    if auto_engine_profile_id is None:
        raise ValueError("TXT report is only for Auto Engine uploads")
    if status not in COMPLETED_STATUSES or not youtube_url or not youtube_video_id:
        raise ValueError("Upload is not a confirmed successful Auto Engine upload")

    out = Path(report_path) if report_path else AUTO_ENGINE_REPORT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = out.read_text(encoding="utf-8") if out.exists() else ""

    # The visible report stays limited to the requested fields. The unique
    # YouTube URL is sufficient to prevent duplicate entries on recovery/retry.
    if f"YouTube URL: {youtube_url}" in existing:
        return out

    profile_name = str(row.get("auto_engine_profile_name") or row.get("group_name") or "Auto Engine").strip()
    channel_name = str(row.get("channel_name") or row.get("youtube_channel_id") or "").strip()
    channel_id = str(row.get("youtube_channel_id") or "").strip()
    filename = str(row.get("original_filename") or Path(str(row.get("local_video_path") or "")).name).strip()
    record = (
        f"Auto Engine Profile: {profile_name}\n"
        f"Channel: {channel_name}\n"
        f"Channel ID: {channel_id}\n"
        f"YouTube URL: {youtube_url}\n"
        f"Uploaded Video Filename: {filename}\n\n"
    )
    updated = existing
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += record

    tmp = out.with_name(out.name + ".tmp")
    backup = out.with_name(out.name + ".bak")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        if out.exists():
            shutil.copy2(out, backup)
        os.replace(tmp, out)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return out
