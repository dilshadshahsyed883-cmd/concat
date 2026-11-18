from __future__ import annotations

from pathlib import Path
from typing import Any

from desktop_core.modules.google_sheet_sync import extract_gid

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
THUMB_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def clean_schedule_times(value: str) -> str:
    items = []
    seen = set()
    for raw in _text(value).replace(";", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid schedule time: {raw}. Use HH:MM.")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except Exception as exc:
            raise ValueError(f"Invalid schedule time: {raw}. Use HH:MM.") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"Invalid schedule time: {raw}.")
        result = f"{hour:02d}:{minute:02d}"
        if result not in seen:
            seen.add(result)
            items.append(result)
    return ",".join(sorted(items or ["09:00"]))


def save_profile(db: Any, form: dict[str, Any], profile_id: int | None = None) -> int:
    sheet_url = _text(form.get("metadata_sheet_url_or_id"))
    gid = _text(form.get("metadata_sheet_gid")) or (extract_gid(sheet_url, fallback="") if sheet_url else "")
    payload = {
        "category_name": _text(form.get("category_name")),
        "video_folder": _text(form.get("video_folder")),
        "thumbnail_folder": _text(form.get("thumbnail_folder")),
        "metadata_sheet_url_or_id": sheet_url,
        "metadata_sheet_tab": _text(form.get("metadata_sheet_tab")),
        "metadata_sheet_gid": gid,
        "channel_group_id": _text(form.get("channel_group_id")),
        "daily_upload_count": int(form.get("daily_upload_count") or 1),
        "schedule_times": clean_schedule_times(_text(form.get("schedule_times")) or "09:00"),
        "schedule_clock_type": _text(form.get("schedule_clock_type")) or "12h",
        "upload_scope": _text(form.get("upload_scope")) or "per_channel",
        "work_mode": _text(form.get("work_mode")) or "manual_review",
        "after_upload_action": _text(form.get("after_upload_action")) or "move_uploaded",
        "auto_select_video": str(form.get("auto_select_video") or "").lower() in {"1","true","on","yes"},
        "thumbnail_rotation": str(form.get("thumbnail_rotation") or "").lower() in {"1","true","on","yes"},
        "enabled": str(form.get("enabled") or "").lower() in {"1","true","on","yes"},
        "schedule_enabled": str(form.get("schedule_enabled") or "").lower() in {"1","true","on","yes"},
        "missed_schedule_policy": _text(form.get("missed_schedule_policy")) or "run_missed_today",
        "notes": _text(form.get("notes")),
    }
    return int(db.save_auto_engine_profile(payload, profile_id=profile_id))


def scan_profile(profile: dict[str, Any]) -> dict[str, int]:
    def count(folder: Any, suffixes: set[str]) -> int:
        path = Path(_text(folder))
        if not path.is_dir():
            return 0
        try:
            return sum(1 for item in path.iterdir() if item.is_file() and item.suffix.lower() in suffixes)
        except Exception:
            return 0
    return {
        "videos": count(profile.get("video_folder"), VIDEO_SUFFIXES),
        "thumbnails": count(profile.get("thumbnail_folder"), THUMB_SUFFIXES),
    }
