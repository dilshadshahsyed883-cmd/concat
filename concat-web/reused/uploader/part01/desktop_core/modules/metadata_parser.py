from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _bool_from_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "made", "kids", "for kids"}


def normalize_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize aliases so both simple txt files and JSON presets work."""
    normalized: Dict[str, Any] = {}
    aliases = {
        "title": "title",
        "video_title": "title",
        "description": "description",
        "desc": "description",
        "tags": "tags",
        "keywords": "tags",
        "category": "category_id",
        "category_id": "category_id",
        "youtube_category": "category_id",
        "playlist": "playlist_id",
        "playlist_id": "playlist_id",
        "privacy": "privacy_status",
        "privacy_status": "privacy_status",
        "publish_at": "app_upload_at",
        "app_upload_at": "app_upload_at",
        "upload_at": "app_upload_at",
        "upload_time": "app_upload_at",
        "schedule": "app_upload_at",
        "schedule_time": "app_upload_at",
        "made_for_kids": "made_for_kids",
        "kids": "made_for_kids",
        "for_kids": "made_for_kids",
        "channel_group": "channel_group",
        "group": "channel_group",
        "channel_group_name": "channel_group",
        "upload_group": "channel_group",
    }

    for key, value in data.items():
        key_norm = str(key).strip().lower().replace(" ", "_").replace("-", "_")
        out_key = aliases.get(key_norm)
        if not out_key:
            continue
        normalized[out_key] = value

    if "tags" in normalized and isinstance(normalized["tags"], list):
        normalized["tags"] = ", ".join(str(t).strip() for t in normalized["tags"] if str(t).strip())

    if "privacy_status" in normalized:
        privacy = str(normalized["privacy_status"]).strip().lower()
        if privacy in {"private", "public", "unlisted"}:
            normalized["privacy_status"] = privacy
        elif privacy == "schedule":
            normalized["privacy_status"] = "private"
        else:
            normalized["privacy_status"] = "private"

    if "made_for_kids" in normalized:
        normalized["made_for_kids"] = _bool_from_value(normalized["made_for_kids"])

    if "channel_group" in normalized:
        normalized["channel_group"] = str(normalized["channel_group"]).strip()

    return normalized


def load_metadata_file(path: str | Path) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))

    text = file_path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return {}

    if file_path.suffix.lower() == ".json":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("Metadata JSON must be an object/dictionary.")
        return normalize_metadata(raw)

    # Simple .txt preset format:
    # title: ...
    # description: first line
    # next description line
    # tags: a,b,c
    # category_id: 22
    # privacy: private/public/unlisted
    data: Dict[str, Any] = {}
    current_key: Optional[str] = None
    description_lines: List[str] = []

    for line in text.splitlines():
        raw = line.rstrip()
        if not raw:
            if current_key == "description":
                description_lines.append("")
            continue

        if ":" in raw:
            key, value = raw.split(":", 1)
            key_norm = key.strip().lower().replace(" ", "_").replace("-", "_")
            mapped = normalize_metadata({key_norm: value.strip()})
            if mapped:
                if current_key == "description" and description_lines:
                    data["description"] = "\n".join(description_lines).strip()
                    description_lines = []
                out_key = next(iter(mapped.keys()))
                data[out_key] = next(iter(mapped.values()))
                current_key = out_key
                if out_key == "description":
                    description_lines = [str(data.get("description", ""))]
                continue

        if current_key == "description":
            description_lines.append(raw)

    if current_key == "description" and description_lines:
        data["description"] = "\n".join(description_lines).strip()

    return normalize_metadata(data)


def save_metadata_file(path: str | Path, data: Dict[str, Any]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_metadata(data)
    file_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")


def metadata_from_folder(folder: str | Path) -> Dict[str, Any]:
    folder_path = Path(folder)
    for name in ("metadata.json", "metadata.txt"):
        candidate = folder_path / name
        if candidate.exists():
            return load_metadata_file(candidate)
    return {}


def find_default_thumbnail(folder: str | Path) -> str | None:
    folder_path = Path(folder)
    for name in ("thumbnail.jpg", "thumbnail.jpeg", "thumbnail.png", "thumbnail.webp"):
        candidate = folder_path / name
        if candidate.exists():
            return str(candidate)
    return None


def find_matching_thumbnail(video_path: str | Path) -> str | None:
    """Find thumbnail with the same base name as the video.

    Example: story_01.mp4 -> story_01.jpg / story_01.png / story_01.webp
    """
    video = Path(video_path)
    folder = video.parent
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = folder / f"{video.stem}{ext}"
        if candidate.exists():
            return str(candidate)
    return None


def scan_media_folder(folder: str | Path) -> List[Dict[str, Any]]:
    folder_path = Path(folder)
    if not folder_path.exists():
        raise FileNotFoundError(str(folder_path))
    if not folder_path.is_dir():
        raise NotADirectoryError(str(folder_path))

    videos = sorted(
        [p for p in folder_path.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda p: p.name.lower(),
    )
    results: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()
    for video in videos:
        key = str(video.resolve()).lower()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        results.append(
            {
                "name": video.name,
                "video_path": str(video),
                "thumbnail_path": find_matching_thumbnail(video),
            }
        )
    return results
