from __future__ import annotations
import os
from pathlib import Path

BUILD = "2026.08.21-local-upload-manager-standalone-v2.6-path-lum"
API_VERSION = 5

DATA_ROOT = Path(os.getenv("LUM_DATA_DIR", "/var/lib/local-upload-manager"))
CORE_DATA_ROOT = DATA_ROOT / "core"
PROJECT_DB = DATA_ROOT / "projects.sqlite3"
USERS_DB = DATA_ROOT / "users.sqlite3"
STAGING_ROOT = DATA_ROOT / "staging"
THUMBNAIL_ROOT = DATA_ROOT / "thumbnails"
REPORTS_ROOT = DATA_ROOT / "reports"
SECURE_ROOT = DATA_ROOT / "secure"
OAUTH_CLIENT_ROOT = SECURE_ROOT / "oauth_clients"
OAUTH_SESSION_ROOT = SECURE_ROOT / "oauth_sessions"
WEB_HOST = os.getenv("LUM_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("LUM_PORT", "8015"))
SESSION_SECRET = os.getenv("LUM_SESSION_SECRET", "")
COOKIE_SECURE = os.getenv("LUM_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}
SUITE_AUTH_ENABLED = os.getenv("LUM_SUITE_AUTH", "0").strip().lower() in {"1", "true", "yes", "on"}
SUITE_LOGIN_URL = os.getenv("LUM_SUITE_LOGIN_URL", "/login").strip() or "/login"

_raw_base_path = os.getenv("LUM_BASE_PATH", "/LUM").strip()
if _raw_base_path in {"", "/"}:
    BASE_PATH = ""
else:
    BASE_PATH = "/" + _raw_base_path.strip("/")

# Concat must be a distinct trusted process, never a mode alias sharing LUM
# or Copyright's secrets, SQLite queue, OAuth vault, or session cookies.
if os.getenv("LUM_ENGINE_INSTANCE") == "concat":
    forbidden = {
        Path("/var/lib/local-upload-manager"),
        Path("/var/lib/lum-copyright"),
        Path("/var/lib/copyright-uploader"),
    }
    if (not os.environ.get("LUM_DATA_DIR")
            or not DATA_ROOT.is_absolute()
            or DATA_ROOT.is_symlink()
            or DATA_ROOT.resolve() in forbidden
            or DATA_ROOT.name.casefold() in {"lum-copyright", "lum1", "copyright-uploader"}
            or (BASE_PATH != "/concat-studio/concat-uploader" and
                not (BASE_PATH == "/concat-uploader" and
                     os.getenv("CONCAT_WEB_IN_PROCESS") == "1"))
            or not SUITE_AUTH_ENABLED):
        raise RuntimeError("Concat Uploader requires a dedicated data root, canonical base path and trusted Scene SSO")
    # Fail closed rather than silently allowing an operator to point the shared
    # desktop core at Copyright/Song's channel DB or owner-token vault.
    if (os.environ.get("YOUTUBE_UPLOAD_MANAGER_DATA_DIR")
            and Path(os.environ["YOUTUBE_UPLOAD_MANAGER_DATA_DIR"]).resolve()
                != CORE_DATA_ROOT.resolve()):
        raise RuntimeError("Concat YouTube core data must stay in its isolated instance root")
    if (os.environ.get("LUM_GOOGLE_OAUTH_ROOT")
            and Path(os.environ["LUM_GOOGLE_OAUTH_ROOT"]).resolve()
                != SECURE_ROOT.resolve()):
        raise RuntimeError("Concat OAuth state must stay in its isolated instance root")
    if (os.environ.get("LUM_AUTO_ENGINE_V6_ROOT")
            and Path(os.environ["LUM_AUTO_ENGINE_V6_ROOT"]).resolve()
                != (DATA_ROOT / "auto_engine_v6").resolve()):
        raise RuntimeError("Concat cannot share another Studio's Auto Engine state")
    if any(p.is_symlink() for p in (CORE_DATA_ROOT, SECURE_ROOT, STAGING_ROOT)):
        raise RuntimeError("Concat isolated storage directories must not be symlinks")
    os.environ["YOUTUBE_UPLOAD_MANAGER_DATA_DIR"] = str(CORE_DATA_ROOT)
    os.environ["LUM_GOOGLE_OAUTH_ROOT"] = str(SECURE_ROOT)
    os.environ["LUM_AUTO_ENGINE_V6_ROOT"] = str(DATA_ROOT / "auto_engine_v6")

# Reused desktop core stores all mutable state below this permanent VPS path.
os.environ.setdefault("YOUTUBE_UPLOAD_MANAGER_DATA_DIR", str(CORE_DATA_ROOT))

DEFAULT_SETTINGS = {
    "engine_enabled": "0" if os.getenv("LUM_ENGINE_INSTANCE", "engine1") in {"engine2", "shorts", "song", "shortstudio", "concat"} else "1",
    "pause_after_current": "0",
    "delete_staged_after_verified": "1",
    "remote_verification_enabled": "1",
    "source_mode": "drive_link",
    "max_active_uploads": "1",
    # Copyright UI compatibility flags never arm an automatic uploader.
    "sheets_sync_enabled": "1" if os.getenv("LUM_ENGINE_INSTANCE") in {"copyright", "concat"} else "0",
    "auto_engine_enabled": "1" if os.getenv("LUM_ENGINE_INSTANCE") == "copyright" else "0",
    "bulk_import_enabled": "0",
    "drive_delete_source_after_verified": "0",
}


def ensure_dirs() -> None:
    for folder in (
        DATA_ROOT, CORE_DATA_ROOT, STAGING_ROOT, THUMBNAIL_ROOT, REPORTS_ROOT,
        SECURE_ROOT, OAUTH_CLIENT_ROOT, OAUTH_SESSION_ROOT,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    for folder in (DATA_ROOT, SECURE_ROOT, OAUTH_CLIENT_ROOT, OAUTH_SESSION_ROOT):
        try:
            folder.chmod(0o700)
        except OSError:
            pass
