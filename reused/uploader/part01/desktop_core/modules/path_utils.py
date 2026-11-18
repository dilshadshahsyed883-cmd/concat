from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List

APP_ROOT = Path(__file__).resolve().parents[1]
APP_FOLDER_NAME = APP_ROOT.name

# Mutable user data must live outside the extracted application folder. This
# keeps channels, sheet rows, tokens and settings safe when a new ZIP is
# extracted over/replaces the old application folder.
def _default_user_data_root() -> Path:
    override = str(os.environ.get("YOUTUBE_UPLOAD_MANAGER_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "LocalYouTubeUploadManager"
    return Path.home() / ".local_youtube_upload_manager"


USER_DATA_ROOT = _default_user_data_root()
LEGACY_CONFIG_DIR = APP_ROOT / "config"
LEGACY_DATA_DIR = APP_ROOT / "data"
CONFIG_DIR = USER_DATA_ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "app_settings.json"
DATA_DIR = USER_DATA_ROOT / "data"
TOKEN_DIR = DATA_DIR / "tokens"
DB_PATH = DATA_DIR / "uploader.db"
SESSION_PATH = DATA_DIR / "ui_session.json"
PORTABLE_STATE_PATH = DATA_DIR / "portable_state.json"
PRESETS_DIR = APP_ROOT / "metadata_presets"
INTERNAL_TOP_LEVEL_DIRS = {"data", "config", "metadata_presets", "examples"}


def _copy_missing_tree(source: Path, destination: Path) -> None:
    if not source.exists() or source.resolve() == destination.resolve():
        return
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def migrate_legacy_user_data() -> None:
    """Copy old in-app mutable data to the permanent user-data directory.

    Existing permanent files always win, so upgrading never overwrites current
    data with empty/default files bundled in a later ZIP.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _copy_missing_tree(LEGACY_CONFIG_DIR, CONFIG_DIR)
    _copy_missing_tree(LEGACY_DATA_DIR, DATA_DIR)


def ensure_app_dirs() -> None:
    migrate_legacy_user_data()
    for folder in (CONFIG_DIR, DATA_DIR, TOKEN_DIR, PRESETS_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def _clean(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("'")


def _slash(value: str) -> str:
    return value.replace("\\", "/")


def _norm(value: str) -> str:
    value = _slash(_clean(value))
    while "//" in value and not value.startswith("//"):
        value = value.replace("//", "/")
    return value.rstrip("/")


def _split_parts(value: str) -> List[str]:
    return [p for p in _slash(value).split("/") if p and p != "."]


def _join_app_suffix(suffix: str) -> str:
    parts = _split_parts(suffix)
    if parts and parts[0] == "data":
        return str(DATA_DIR.joinpath(*parts[1:]))
    if parts and parts[0] == "config":
        return str(CONFIG_DIR.joinpath(*parts[1:]))
    return str(APP_ROOT.joinpath(*parts)) if parts else str(APP_ROOT)


def infer_old_app_root_from_path(value: Any) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    norm = _norm(raw)
    for top in INTERNAL_TOP_LEVEL_DIRS:
        marker = f"/{APP_FOLDER_NAME}/{top}/"
        idx = norm.lower().find(marker.lower())
        if idx >= 0:
            return norm[: idx + len(f"/{APP_FOLDER_NAME}")]
        prefix = f"{APP_FOLDER_NAME}/{top}/"
        if norm.lower().startswith(prefix.lower()):
            return APP_FOLDER_NAME
    return None


def load_portable_state() -> Dict[str, Any]:
    try:
        if PORTABLE_STATE_PATH.exists():
            data = json.loads(PORTABLE_STATE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_portable_state(extra: Dict[str, Any] | None = None) -> None:
    try:
        ensure_app_dirs()
        state = {
            "app_root": str(APP_ROOT),
            "app_folder_name": APP_FOLDER_NAME,
            "user_data_root": str(USER_DATA_ROOT),
        }
        if extra:
            state.update(extra)
        tmp = PORTABLE_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PORTABLE_STATE_PATH)
    except Exception:
        pass


def repair_path_value(value: Any, old_roots: Iterable[str] | None = None) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    if raw.startswith("app://"):
        return _join_app_suffix(raw[len("app://") :].lstrip("/\\"))
    norm_raw = _norm(raw)
    for current_root in (APP_ROOT, USER_DATA_ROOT):
        norm_current = _norm(str(current_root))
        if norm_raw.lower() == norm_current.lower() or norm_raw.lower().startswith(norm_current.lower() + "/"):
            return raw
    roots: List[str] = []
    for root in old_roots or []:
        root_norm = _norm(str(root))
        if root_norm and root_norm.lower() not in {r.lower() for r in roots}:
            roots.append(root_norm)
    inferred = infer_old_app_root_from_path(raw)
    if inferred and inferred.lower() not in {r.lower() for r in roots}:
        roots.append(inferred)
    for root in roots:
        root_norm = _norm(root)
        if not root_norm:
            continue
        if norm_raw.lower() == root_norm.lower():
            return str(APP_ROOT)
        if norm_raw.lower().startswith(root_norm.lower() + "/"):
            suffix = norm_raw[len(root_norm) :].lstrip("/")
            return _join_app_suffix(suffix)
    parts = _split_parts(raw)
    if parts and parts[0] in INTERNAL_TOP_LEVEL_DIRS:
        return _join_app_suffix(raw)
    return raw


def resolve_stored_path(value: Any, old_roots: Iterable[str] | None = None) -> Path:
    return Path(repair_path_value(value, old_roots=old_roots))


def path_exists(value: Any, old_roots: Iterable[str] | None = None) -> bool:
    raw = _clean(value)
    if not raw:
        return False
    try:
        return resolve_stored_path(raw, old_roots=old_roots).exists()
    except Exception:
        return False


def filename_only(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    parts = _split_parts(raw)
    return parts[-1] if parts else Path(raw).name


def repair_object_paths(obj: Any, old_roots: Iterable[str] | None = None) -> Any:
    path_key_fragments = ("path", "folder", "dir", "directory", "client_secret", "checked_video_paths")
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for key, value in obj.items():
            key_lower = str(key).lower()
            if any(fragment in key_lower for fragment in path_key_fragments):
                if isinstance(value, str):
                    out[key] = repair_path_value(value, old_roots=old_roots)
                elif isinstance(value, list):
                    out[key] = [repair_path_value(item, old_roots=old_roots) if isinstance(item, str) else repair_object_paths(item, old_roots=old_roots) for item in value]
                else:
                    out[key] = repair_object_paths(value, old_roots=old_roots)
            else:
                out[key] = repair_object_paths(value, old_roots=old_roots)
        return out
    if isinstance(obj, list):
        return [repair_object_paths(item, old_roots=old_roots) for item in obj]
    return obj
