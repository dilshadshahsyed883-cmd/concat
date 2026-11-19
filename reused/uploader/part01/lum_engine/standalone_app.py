from __future__ import annotations
import os
import secrets
import shutil
import hashlib
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .studio_tenants import StudioTenants
from .auth_store import UserStore
from .config import (
    API_VERSION, BASE_PATH, BUILD, COOKIE_SECURE, DATA_ROOT, REPORTS_ROOT, SESSION_SECRET, STAGING_ROOT,
    SUITE_AUTH_ENABLED, SUITE_LOGIN_URL, THUMBNAIL_ROOT, ensure_dirs,
)
from .oauth_web import (complete_oauth, list_clients, list_owner_clients,
    owner_client_for_oauth, save_client_json, save_owner_client_json,
    save_shared_client_json, start_oauth)
from .project_store import ProjectStore
from .copyright_handoff import available_videos, available_folders, owner_root, owner_folder, safe_relative_folder
from .concat_source import stage_concat_link
from .concat_schedule import concat_upload_at
from .concat_sheet import preview_sheet_videos
from .concat_source import parse_concat_link, ConcatSourceError
from .copyright_ui import schedule_pkt
from .copyright_vault import owner_vault_inventory, import_client as import_owner_client, import_token as import_owner_token
from .four_modes import registry as four_mode_registry, validate_mode as validate_auto_mode
from .mode_gateway import forward as forward_engine_request, MODE_PORTS as ENGINE_PORTS
from .reports import generate_health_report, list_reports, report_path
from .worker import VPSUploadWorker
from desktop_core.modules.auth_manager import AuthManager
from desktop_core.modules.database import Database
from desktop_core.modules.token_utils import classify_google_json, token_status_label

APP_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = APP_ROOT / "templates"
STATIC_ROOT = APP_ROOT / "static"


def route(path: str) -> str:
    path = str(path or "/")
    if not path.startswith("/"):
        path = "/" + path
    if not BASE_PATH:
        return path
    if path == "/":
        return BASE_PATH + "/"
    return BASE_PATH + path


ensure_dirs()
if not SESSION_SECRET:
    raise RuntimeError("LUM_SESSION_SECRET is required")

store = ProjectStore()
users = UserStore()
db = Database()
studio_tenants = StudioTenants(db)
auth_manager = AuthManager(db)
worker = VPSUploadWorker(store, db)

# LUM_PC_ENGINE_WEB_BRIDGE_V4_IMPORT
from .sheets_bridge import (
    save_sheet_source as web_save_sheet_source,
    delete_sheet_source as web_delete_sheet_source,
    sync_sheet_source_by_id as web_sync_sheet_source_by_id,
    sync_all_sheet_sources as web_sync_all_sheet_sources,
    fetch_sheet_csv as tenant_fetch_sheet_csv,
    rows_to_sheet_rows as tenant_rows_to_sheet_rows,
    TOKEN_DIR as tenant_token_dir,
)

# LUM_CHANNELS_PC_SYNC_V5_IMPORTS
from .channels_web import (
    channels_page_context,
    delete_manual_selected,
    install_client_json,
    install_token_json,
    rescan_channel_tokens,
    set_status_bulk,
    test_connections,
)
from .pc_sync_web import apply_snapshot, list_sync_history, save_snapshot_uploads

# LUM_FULL_AUTO_ENGINE_V6_IMPORTS
from pathlib import Path as _V6Path
from fastapi.responses import FileResponse as V6FileResponse, JSONResponse as V6JSONResponse
from desktop_core.modules.token_utils import VALID_TOKEN as V6_VALID_TOKEN, classify_google_json as v6_classify_google_json
from .auto_engine_v6 import (
    V6Store, category_metadata as v6_category_metadata, generate_queue as v6_generate_queue,
    mark_schedule_skipped_today as v6_mark_schedule_skipped_today, planner as v6_planner,
    preflight_profile as v6_preflight_profile, profile_context as v6_profile_context,
    queue_preflight as v6_queue_preflight, save_profile_from_form as v6_save_profile_from_form,
    target_channels as v6_target_channels, parse_schedule_times as v6_parse_schedule_times,
    sync_channel_profile as v6_sync_channel_profile, resume_channel_profile as v6_resume_channel_profile,
    shortstudio_list_folders as v6_shortstudio_list_folders,
)

IS_COPYRIGHT = os.getenv("LUM_ENGINE_INSTANCE") == "copyright"
OWNER_SCOPED = IS_COPYRIGHT or os.getenv("LUM_ENGINE_INSTANCE") == "concat"
app = FastAPI(title="Concat Uploader" if os.getenv("LUM_ENGINE_INSTANCE") == "concat" else "Copyright Uploader", version=BUILD, docs_url=None, redoc_url=None)
v6_store = None if os.getenv("LUM_ENGINE_INSTANCE") == "concat" else V6Store()
app.mount(route("/static"), StaticFiles(directory=str(STATIC_ROOT)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_ROOT))
templates.env.globals["base_path"] = BASE_PATH
templates.env.filters["schedule_pkt"] = schedule_pkt

COPYRIGHT_MEDIA_ROOT = Path(os.environ.get("COPYRIGHT_MEDIA_ROOT", str(DATA_ROOT / "copyright_handoff")))
COPYRIGHT_MAX_VIDEO = 12 * 1024 * 1024 * 1024
COPYRIGHT_MIN_FREE = 5 * 1024 * 1024 * 1024

def copyright_owner_root(owner: str) -> Path:
    return owner_root(owner)


def copyright_folder_files(owner: str, folder: str = '') -> list[str]:
    return available_videos(owner, store.claimed_folder_files(owner,folder),folder=folder)


async def copyright_stage_file(video: UploadFile, ext: str) -> tuple[str, int, str]:
    if ext not in {".mp4", ".mkv", ".mov"}:
        raise ValueError("Video file must be MP4, MKV or MOV")
    if shutil.disk_usage(STAGING_ROOT).free < COPYRIGHT_MIN_FREE:
        raise ValueError("Not enough free space to stage video safely")
    target = STAGING_ROOT / ("copyright_" + uuid.uuid4().hex + ext)
    sha = hashlib.sha256()
    total = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > COPYRIGHT_MAX_VIDEO or shutil.disk_usage(STAGING_ROOT).free < COPYRIGHT_MIN_FREE:
                    raise ValueError("Video too large or operational disk reserve reached")
                sha.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if total == 0:
            raise ValueError("Video file is empty")
        return str(target), total, sha.hexdigest()
    except BaseException:
        target.unlink(missing_ok=True)
        raise

async def copyright_stage_owner_file(owner: str, filename: str, folder: str = '') -> tuple[str, int, str]:
    if not filename or Path(filename).name != filename or filename.startswith("."):
        raise ValueError("Invalid folder filename")
    directory = owner_folder(owner, folder)
    source = directory / filename
    if directory.is_symlink() or source.is_symlink() or not source.is_file() or source.resolve().parent != directory.resolve():
        raise ValueError("Selected video is not in your Copyright Uploader folder")
    if source.suffix.lower() not in {".mp4", ".mkv", ".mov"}:
        raise ValueError("Unsupported video format")
    from starlette.datastructures import UploadFile as _UploadFile
    with source.open("rb") as input_file:
        wrapped = _UploadFile(file=input_file, filename=source.name)
        return await copyright_stage_file(wrapped, source.suffix.lower())



def redirect(path: str, *, message: str = "", error: str = "") -> RedirectResponse:
    path = route(path)
    query = {}
    if message:
        query["message"] = message
    if error:
        query["error"] = error
    if not query:
        return RedirectResponse(path, status_code=303)
    sep = "&" if "?" in path else "?"
    return RedirectResponse(path + sep + urlencode(query), status_code=303)


def csrf(request: Request) -> str:
    token = str(request.session.get("csrf") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def require_csrf(request: Request, supplied: str) -> None:
    expected = str(request.session.get("csrf") or "")
    if not expected or not secrets.compare_digest(expected, str(supplied or "")):
        raise HTTPException(403, "Invalid CSRF token")


def current_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "lum_user", None)
    if not user:
        raise HTTPException(401, "Login required")
    return user


def require_admin(user: dict[str, Any]) -> None:
    if not int(user.get("is_admin") or 0):
        raise HTTPException(403, "Administrator access required")


def user_mode(user: dict[str, Any]) -> str:
    # Retain legacy persisted user modes without modifying accounts or user data.
    return "heavy"


def require_heavy(user: dict[str, Any]) -> None:
    """Compatibility hook: only the full workspace remains."""
    return None

def all_channels(user: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM channels ORDER BY id DESC").fetchall()]
    if user is not None:
        allowed=studio_tenants.allowed_ids("channel",user)
        if allowed is not None:
            rows=[row for row in rows if int(row["id"]) in allowed]
    for row in rows:
        token = str(row.get("token_file_path") or "")
        row["token_status"] = token_status_label(classify_google_json(Path(token))) if token else "Missing"
        row["proxy_password"] = "********" if str(row.get("proxy_password") or "") else ""
    return rows


def project_view(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    core_id = int(item.get("core_video_id") or 0)
    if core_id:
        core = db.get_video(core_id) or {}
        if str(item.get("status")) in {"UPLOADING", "UPLOAD_QUEUED", "PAUSED", "FAILED"}:
            item["core_status"] = core.get("status")
            item["progress"] = int(core.get("progress") or item.get("progress") or 0)
            # LUM_WAITING_PROXY_DISPLAY_V1
            if str(item.get("status") or "").upper()=="WAITING_NETWORK" and str(item.get("error_message") or "").startswith("AUTO_PROXY_WAIT|"):
                item["status"]="WAITING_PROXY"
            if core.get("error_message"):
                item["error_message"] = core.get("error_message")
            item["youtube_video_id"] = core.get("youtube_video_id") or item.get("youtube_video_id")
            item["youtube_url"] = core.get("youtube_url") or item.get("youtube_url")
            if str(core.get("recovery_state") or "") == "REVIEW_REQUIRED":
                item["status"] = "REVIEW_REQUIRED"
    return item


def get_projects(user: dict[str, Any], limit: int = 500) -> list[dict[str, Any]]:
    owner = (str(user["username"]) if OWNER_SCOPED
             else (None if int(user.get("is_admin") or 0) else str(user["username"])))
    return [project_view(p) for p in store.list(owner=owner, limit=limit)]


def get_logs(user: dict[str, Any], limit: int = 300) -> list[dict[str, Any]]:
    owner = (str(user["username"]) if OWNER_SCOPED
             else (None if int(user.get("is_admin") or 0) else str(user["username"])))
    return store.events(owner=owner, limit=limit)


def stats_payload(user: dict[str, Any] | None = None) -> dict[str, Any]:
    channels = all_channels(user)
    owner = (str(user.get("username") or "") if user is not None and
             (OWNER_SCOPED or not int(user.get("is_admin") or 0))
             else None)
    counts=store.counts(owner=owner)
    return {
        "counts": counts,
        "owner_queue_paused": store.owner_queue_paused(str(user["username"])) if user is not None and OWNER_SCOPED else False,
        "owner_scheduled_paused": store.owner_scheduled_paused(str(user["username"])) if user is not None and OWNER_SCOPED else False,
        "channels": len(channels),
        "authenticated_channels": sum(1 for c in channels if str(c.get("token_status") or "").lower().startswith("valid")),
        "active_project_id": (worker.active_project_id if user is None or
            (int(user.get("is_admin") or 0) and not OWNER_SCOPED)
            else None),
        "engine_enabled": store.setting("engine_enabled", "1") == "1",
        "pause_after_current": store.setting("pause_after_current", "0") == "1",
        "settings": ({"sheets_sync_enabled": "1", "auto_engine_enabled": "1",
                       "pause_after_current": store.setting("pause_after_current", "0"),
                       "engine_enabled": store.setting("engine_enabled", "1")}
                     if OWNER_SCOPED and user is not None
                     else (store.settings() if user is None or int(user.get("is_admin") or 0) else {})),
    }


@app.middleware("http")
async def standalone_auth(request: Request, call_next):
    path = request.url.path
    if os.getenv("LUM_ENGINE_INSTANCE") == "concat" and (
        path.startswith(route("/actions/copyright/profile")) or
        path.startswith(route("/api/copyright/")) or
        path.startswith(route("/actions/pc-sync/"))
    ):
        return JSONResponse({"detail": "Not available in Concat Uploader"}, status_code=404)
    if OWNER_SCOPED and (
        path.startswith(route("/actions/auto-v6/")) or
        path.startswith(route("/api/auto-v6")) or
        path.startswith(route("/modes/")) or
        path.startswith(route("/actions/pc-sync/"))
    ):
        return JSONResponse({"detail": "Automatic uploader engine is disabled for Copyright Studio"}, status_code=404)
    if OWNER_SCOPED and (
        path.startswith(route("/reports/")) or path == route("/actions/report-health")
    ):
        return JSONResponse({"detail": "Global health reports are unavailable in owner-scoped Copyright Studio"},status_code=404)
    if OWNER_SCOPED and request.method not in {"GET", "HEAD"} and path in {
        route("/actions/google-oauth/configure"), route("/actions/channels/json-upload"),
        route("/actions/pc-sync/upload"), route("/actions/pc-sync/apply"),
        route("/actions/pause-after-current"), route("/actions/resume"), route("/actions/stop-current"),
    }:
        return JSONResponse({"detail": "Copyright queues are owner-scoped; global controls are disabled"}, status_code=404)
    if path.startswith(route("/static/")) or path == route("/health"):
        return await call_next(request)

    if SUITE_AUTH_ENABLED:
        username = str(request.headers.get("X-Auth-User") or "").strip()
        app_access = str(request.headers.get("X-App-Access") or "").strip().lower()
        is_admin = str(request.headers.get("X-Is-Admin") or "0").strip().lower() in {"1", "true", "yes", "on"}
        if not username:
            return RedirectResponse(SUITE_LOGIN_URL, status_code=303)
        if not is_admin and app_access not in ({"concat", "concat-studio", "all"} if os.getenv("LUM_ENGINE_INSTANCE") == "concat" else {"lum", "all"}):
            if os.getenv("LUM_ENGINE_INSTANCE") == "concat":
                return JSONResponse({"detail": "Concat Studio access required"}, status_code=403)
            raise HTTPException(403, "Local Upload Manager access required")
        mapped = users.get_by_username(username)
        user = {
            "id": int((mapped or {}).get("id") or 0),
            "username": username,
            "is_active": 1,
            "is_admin": 1 if is_admin else 0,
            "ui_mode": "heavy",
            "effective_ui_mode": "heavy",
            "mode_access": "heavy",
            "suite_authenticated": True,
            "app_access": app_access,
        }
        request.session.pop("lum_admin_mode", None)
        request.state.lum_user = user
        # TENANT_SAFE_WRITES: no unscoped global tokens, worker or queue changes.
        if not is_admin and request.method not in {"GET","HEAD"}:
            allowed_exact={route("/logout"),route("/actions/add-video"),route("/actions/oauth-client"),
                route("/actions/oauth-start"),route("/actions/oauth-complete"),
                route("/actions/google-oauth/connect"),
                route("/actions/studio-tenant/channel-create"),
                route("/actions/studio-tenant/group-create"),
                route("/actions/auth-connections/test"),
                route("/actions/auth-connections/activate"),
                route("/actions/sheets/save"),route("/actions/sheets/delete"),
                route("/actions/sheets/sync"),route("/actions/sheets/sync-all"),
                route("/actions/copyright/queue/pause"),route("/actions/copyright/queue/start"),
                route("/actions/copyright/credentials/upload"),
                route("/actions/copyright/queue/bulk"),route("/actions/copyright/queue/remove"),
                route("/actions/channels/status"),route("/actions/channels/delete-manual"),
                route("/actions/channels/rescan-tokens"),route("/actions/channels/test-connections"),
                route("/actions/copyright/profile"),route("/actions/copyright/profile/update"),
                route("/actions/copyright/profile/delete")}
            allowed_prefixes=(route("/actions/retry/"),route("/actions/authorize-fresh/"),
                route("/actions/cancel/"),route("/actions/channel-network/"),
                route("/actions/copyright/metadata/"))
            if path not in allowed_exact and not path.startswith(allowed_prefixes):
                return JSONResponse({"detail":"Global uploader operation is administrator-only"},status_code=403)
        if (not is_admin and os.getenv("LUM_ENGINE_INSTANCE") in
                {"song","shortstudio","copyright"} and
                (path.startswith(route("/reports/")) or
                 path.startswith(route("/actions/auto-v6/thumbnail/")) or
                 path==route("/actions/auto-v6/preview-all") or
                 path==route("/actions/auto-v6/upload-records.csv") or
                 path==route("/api/auto-modes"))):
            return JSONResponse({"detail":"Owner-scoped resource access required"},status_code=403)
        return await call_next(request)

    if users.count() == 0:
        if path != route("/setup"):
            return RedirectResponse(route("/setup"), status_code=303)
        return await call_next(request)
    if path == route("/setup"):
        return RedirectResponse(route("/login"), status_code=303)
    if path == route("/login"):
        return await call_next(request)
    user_id = int(request.session.get("user_id") or 0)
    user = users.get(user_id) if user_id else None
    if not user or not int(user.get("is_active") or 0):
        request.session.clear()
        return RedirectResponse(route("/login"), status_code=303)
    user = dict(user)
    user["effective_ui_mode"] = "heavy"
    user["mode_access"] = "heavy"
    request.session.pop("lum_admin_mode", None)
    request.state.lum_user = user
    return await call_next(request)


# Session middleware must wrap the authentication middleware so request.session
# is available inside standalone_auth on every protected request.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="lum_session" if os.getenv("LUM_ENGINE_INSTANCE", "engine1") == "engine1" else "lum_session_" + os.getenv("LUM_ENGINE_INSTANCE", "engine1"),
    same_site="lax",
    https_only=COOKIE_SECURE,
    max_age=60 * 60 * 24 * 14,
    path=BASE_PATH or "/",
)


@app.on_event("startup")
def startup() -> None:
    worker.start()
    try:
        generate_health_report(store, db, worker)
    except Exception:
        pass


@app.on_event("shutdown")
def shutdown() -> None:
    worker.stop()


@app.get(route("/health"))
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "app": "Local Upload Manager",
        "engine_instance": os.getenv("LUM_ENGINE_INSTANCE", "engine1"),
        "standalone": not SUITE_AUTH_ENABLED,
        "suite_auth": SUITE_AUTH_ENABLED,
        "build": BUILD,
        "api_version": API_VERSION,
        "worker_alive": bool(worker.thread and worker.thread.is_alive()),
        "active_project_id": worker.active_project_id,
        "serial_concurrency": 1,
        "remote_verification": store.setting("remote_verification_enabled", "1") == "1",
        "verified_cleanup": store.setting("delete_staged_after_verified", "1") == "1",
        "drive_source_delete": False,
        "existing_web_integration": SUITE_AUTH_ENABLED,
        "base_path": BASE_PATH or "/",
        "public_path": route("/"),
    }


@app.get(route("/setup"), response_class=HTMLResponse)
def setup_page(request: Request, error: str = ""):
    if SUITE_AUTH_ENABLED:
        return RedirectResponse(route("/app"), status_code=303)
    return templates.TemplateResponse("setup.html", {"request": request, "error": error, "csrf": csrf(request)})


@app.post(route("/setup"))
def setup_action(request: Request, csrf_token: str = Form(...), username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    if users.count() != 0:
        return redirect("/login")
    require_csrf(request, csrf_token)
    if password != confirm_password:
        return redirect("/setup", error="Passwords do not match")
    try:
        user_id = users.create(username, password, is_admin=True, ui_mode="heavy")
    except Exception as exc:
        return redirect("/setup", error=str(exc))
    request.session.clear()
    request.session["user_id"] = user_id
    request.session["csrf"] = secrets.token_urlsafe(32)
    request.session.pop("lum_admin_mode", None)
    return redirect("/app", message="Administrator account created · Full workspace ready")


@app.get(route("/login"), response_class=HTMLResponse)
def login_page(request: Request, error: str = "", message: str = ""):
    if SUITE_AUTH_ENABLED:
        return RedirectResponse(SUITE_LOGIN_URL, status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "message": message, "csrf": csrf(request)})


@app.post(route("/login"))
def login_action(request: Request, csrf_token: str = Form(...), username: str = Form(...), password: str = Form(...)):
    require_csrf(request, csrf_token)
    user = users.authenticate(username, password)
    if not user:
        return redirect("/login", error="Invalid username/password or account disabled")
    request.session.clear()
    request.session["user_id"] = int(user["id"])
    request.session["csrf"] = secrets.token_urlsafe(32)
    if int(user.get("is_admin") or 0):
        request.session.pop("lum_admin_mode", None)
    return redirect("/app")


@app.post(route("/logout"))
def logout_action(request: Request, csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)
    request.session.clear()
    if SUITE_AUTH_ENABLED:
        return RedirectResponse("/logout", status_code=303)
    return redirect("/login", message="Signed out")


@app.get(route("/"))
def root() -> RedirectResponse:
    return RedirectResponse(route("/app"), status_code=303)


@app.get(route("/app"), response_class=HTMLResponse)
def app_page(request: Request, tab: str = "", message: str = "", error: str = "", embedded: int = 0, source_id: int = 0, auto_profile_id: int = 0, new_profile: int = 0, auto_mode: str = "engine1", profile_id: int = 0, preview_concat: int = 0, concat_video_link: str = "", concat_title: str = ""):
    user = current_user(request)
    if SUITE_AUTH_ENABLED and embedded:
        request.session["lum_embedded"] = "1"
    embedded_view = True
    mode = "heavy"
    allowed = {"dashboard", "channels", "sheets", "auto_engine", "add_video", "bulk_import", "queue", "logs", "auto_stream"}
    if os.getenv("LUM_ENGINE_INSTANCE", "engine1") == "copyright":
        allowed = {"dashboard", "channels", "sheets", "add_video", "queue", "logs"}
    elif os.getenv("LUM_ENGINE_INSTANCE", "engine1") == "concat":
        allowed = {"channels", "sheets", "add_video", "queue"}
    default_tab = "sheets" if os.getenv("LUM_ENGINE_INSTANCE") == "concat" else "add_video"
    selected = tab if tab in allowed else default_tab
    channels = all_channels(user)
    projects = get_projects(user)
    logs = get_logs(user) if selected == "logs" else []
    sheet_sources = db.list_sheet_sources() if selected == "sheets" else []
    selected_sheet_source = db.get_sheet_source(int(source_id)) if selected == "sheets" and int(source_id or 0) else None
    permitted_sheets=studio_tenants.allowed_ids("sheet",user)
    if permitted_sheets is not None:
        sheet_sources=[item for item in sheet_sources if int(item["id"]) in permitted_sheets]
        if selected_sheet_source and int(selected_sheet_source["id"]) not in permitted_sheets:
            selected_sheet_source=None
    permitted_channels=studio_tenants.allowed_ids("channel",user)
    web_clients = (list_owner_clients(str(user["username"])) if OWNER_SCOPED
                   else (list_clients() if permitted_channels is None else []))
    channel_context = channels_page_context(
        db, web_clients, permitted_channels, permitted_sheets,
        inventory_channels=channels if os.getenv("LUM_ENGINE_INSTANCE") in
            {"song", "shortstudio", "copyright", "concat"} else None,
        prefer_stored_token_paths=OWNER_SCOPED,
    ) if (selected == "channels" or (selected == "add_video" and
             OWNER_SCOPED)) else {
        "rows": [], "summary": {}, "groups": [], "sheets": [], "oauth_inventory": {}
    }
    if permitted_channels is not None:
        permitted_groups=studio_tenants.allowed_ids("group",user)
        channel_context["rows"]=[item for item in channel_context["rows"]
                                 if int(item.get("id") or 0) in permitted_channels]
        channel_context["groups"]=[item for item in channel_context["groups"]
                                   if int(item.get("id") or 0) in permitted_groups]
        channel_context["sheets"]=[item for item in channel_context["sheets"]
                                   if int(item.get("id") or 0) in permitted_sheets]
        if not OWNER_SCOPED:
            channel_context["summary"]={"total":len(channel_context["rows"])}
            web_clients=[]  # Other instances expose the shared client only to global admins
        channel_context["oauth_inventory"]=(owner_vault_inventory(db,studio_tenants,str(user["username"]))
            if OWNER_SCOPED else {})
    # PC Sync is a global administrator operation; its history may contain
    # private channel identifiers and source paths from other accounts.
    pc_sync_history = (list_sync_history() if selected == "channels" and
                       int(user.get("is_admin") or 0) and not OWNER_SCOPED else [])
    try:
        selected_auto_mode = validate_auto_mode(auto_mode)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown uploader mode")
    engine_instance = os.getenv("LUM_ENGINE_INSTANCE", "engine1")
    if engine_instance in {"engine2", "shorts", "song", "shortstudio", "copyright", "concat"}:
        # Independent uploaders cannot access Auto Stream.
        if selected == "auto_stream":
            raise HTTPException(404, "Auto Stream is not an uploader engine")
        selected_auto_mode = "engine1"
    elif selected_auto_mode == "short_stream":
        # Historical four-mode bookmarks lead to the separate Stream tab.
        return RedirectResponse(route("/app") + "?tab=auto_stream", status_code=303)
    elif selected == "auto_engine" and selected_auto_mode in {"engine2", "shorts"}:
        # One native full-width document per engine. Engine 2 and Short Uploader
        # remain independent services/data stores, reached via the authenticated
        # same-origin LUM mode gateway. Never embed one document inside another.
        return RedirectResponse(
            f"/local-upload-manager/modes/{selected_auto_mode}/app"
            "?embedded=1&tab=auto_engine",
            status_code=303,
        )
    # Only Engine 1 owns the cross-mode registry. Its root defaults to lum1,
    # while each isolated process points LUM_DATA_DIR at lum2/lum3. Calling
    # four_mode_registry() from those processes aliases the Engine 1 root to
    # their own root and raises "Uploader modes must have separate data roots".
    # Isolated apps render solely from their own V6/PC database instead.
    mode_registry = (
        four_mode_registry()
        if selected == "auto_engine" and engine_instance == "engine1"
        else {}
    )
    auto_v6 = v6_profile_context(db, v6_store, int(auto_profile_id or 0),
                                     owner="" if int(user.get("is_admin") or 0) else str(user.get("username") or "")) if selected == "auto_engine" and selected_auto_mode == "engine1" else {}
    if selected == "auto_engine" and auto_v6:
        auto_v6["short_folders"] = v6_shortstudio_list_folders(
            owner="" if int(user.get("is_admin") or 0) else str(user.get("username") or "")
        ) if engine_instance == "shortstudio" else []
        # LUM_SCHEDULER_CONTROL_LIVE_V1: runtime-only presentation state.
        # This does not mutate scheduler_armed or profile schedules.
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        _engine = auto_v6.get("engine") or {}
        _armed = bool(int(_engine.get("scheduler_armed") or 0))
        _profiles = auto_v6.get("profiles") or []
        _ready = []
        _next = []
        _now = datetime.now(ZoneInfo("Asia/Karachi"))
        for _pr in _profiles:
            try:
                if int(_pr.get("enabled") or 0) != 1 or str(_pr.get("work_mode") or "").strip().lower() != "auto":
                    continue
                _channels = v6_target_channels(db, _pr)
                if not _channels:
                    continue
                if _pr.get("channel_db_id"):
                    # Channel Video-ID profiles are interval-synced. Upload timing belongs to each Sheet row.
                    _ready.append(_pr)
                    _raw_next = str(_pr.get("next_sync_at") or "").strip()
                    try:
                        _candidate = datetime.fromisoformat(_raw_next.replace("Z", "+00:00")) if _raw_next else _now
                        if _candidate.tzinfo is None:
                            _candidate = _candidate.replace(tzinfo=ZoneInfo(str(_pr.get("timezone_name") or "Asia/Karachi")))
                        _next.append(_candidate.astimezone(ZoneInfo("Asia/Karachi")))
                    except Exception:
                        _next.append(_now)
                    continue
                if int(_pr.get("schedule_enabled") or 0) != 1:
                    continue
                _times = v6_parse_schedule_times(_pr.get("schedule_times"))[:max(1, int(_pr.get("daily_upload_count") or 1))]
                if not _times:
                    continue
                _ready.append(_pr)
                for _alarm in _times:
                    _hh, _mm = [int(_x) for _x in _alarm.split(":", 1)]
                    _candidate = _now.replace(hour=_hh, minute=_mm, second=0, microsecond=0)
                    if _candidate <= _now:
                        _candidate += timedelta(days=1)
                    _next.append(_candidate)
            except Exception:
                continue
        _queue = auto_v6.get("queue") or []
        _active_states = {"processing", "uploading", "running", "active", "claimed", "in_progress"}
        _worker_processing = any(str((_row.get("status") if hasattr(_row, "get") else "") or "").strip().lower() in _active_states for _row in _queue)
        _last_error = str(_engine.get("last_error") or "").strip()
        _worker_service = str(auto_v6.get("worker_service") or "").lower()
        _scheduler_service = str(auto_v6.get("scheduler_service") or "").lower()
        _worker_status = "Offline" if _worker_service != "active" else ("Error" if _last_error else ("Processing" if _worker_processing else "Idle"))
        _scheduler_status = "Offline" if _scheduler_service != "active" else ("Error" if _last_error else ("Waiting" if _armed else "Paused"))
        auto_v6["scheduler_ui"] = {
            "worker_status": _worker_status,
            "scheduler_status": _scheduler_status,
            "ready_profiles": len(_ready),
            "next_run": (min(_next).strftime("%a %d %b · %H:%M PKT") if _armed and _next else "No eligible schedule"),
        }
    if selected == "auto_engine" and selected_auto_mode == "engine1" and int(new_profile or 0):
        auto_v6 = dict(auto_v6 or {})
        auto_v6["selected"] = None
        auto_v6["source"] = {}
        auto_v6["videos"] = []
        auto_v6["thumbnails"] = []
        auto_v6["eligible_channels"] = 0
    if selected_sheet_source and not any(int(x.get("id") or 0) == int(selected_sheet_source.get("id") or 0) for x in sheet_sources):
        selected_sheet_source = None
    # Owner-scoped optional Concat link preview from the SAME first-tab Sheets
    # CSV engine. Preview never creates queue rows or contacts arbitrary URLs.
    concat_sheet_videos = []
    concat_sheet_error = ""
    if (engine_instance == "concat" and selected == "sheets"
            and selected_sheet_source and int(preview_concat or 0) == 1):
        try:
            studio_tenants.require("sheet", int(selected_sheet_source["id"]), user)
            raw = tenant_fetch_sheet_csv(selected_sheet_source["sheet_url_or_id"], None)
            concat_sheet_videos = preview_sheet_videos(
                raw, origin=os.getenv("CONCAT_PUBLIC_ORIGIN", "https://concat.scenebuilder.online"))
        except Exception:
            concat_sheet_error = "Sheet preview is unavailable; check access and retry Sync"
    concat_source_prefill = ""
    if engine_instance == "concat" and selected == "add_video" and concat_video_link:
        try:
            parse_concat_link(concat_video_link,
                              origin=os.getenv("CONCAT_PUBLIC_ORIGIN", "https://concat.scenebuilder.online"))
            concat_source_prefill = concat_video_link
        except ConcatSourceError:
            pass
    return templates.TemplateResponse("app.html", {
        "request": request,
        "user": user,
        "mode": mode,
        "tab": selected,
        "message": message,
        "error": error,
        "health": health(),
        "stats": stats_payload(user),
        "channels": channels,
        "clients": web_clients,
        "projects": projects,
        "logs": logs,
        "sheet_sources": sheet_sources,
        "concat_sheet_videos": concat_sheet_videos,
        "concat_sheet_error": concat_sheet_error,
        "concat_source_prefill": concat_source_prefill,
        "concat_title_prefill": str(concat_title or "")[:100],
        "selected_sheet_source": selected_sheet_source,
        "channel_rows": channel_context["rows"],
        "channel_summary": channel_context["summary"],
        "channel_groups": channel_context["groups"],
        "channel_sheets": channel_context["sheets"],
        "oauth_inventory": channel_context["oauth_inventory"],
        "pc_sync_history": pc_sync_history,
        "auto_v6": auto_v6,
        "auto_mode": selected_auto_mode,
        "engine_instance": engine_instance,
        "mode_registry": mode_registry,
        "reports": list_reports() if int(user.get("is_admin") or 0) else [],
        "csrf": csrf(request),
        "oauth_url": str(request.session.get("lum_oauth_url") or ""),
        "oauth_session_id": str(request.session.get("lum_oauth_session_id") or ""),
        "build": BUILD,
        "suite_auth": SUITE_AUTH_ENABLED,
        "embedded_view": embedded_view,
        "copyright_profiles": store.copyright_profiles(str(user["username"])),
        "selected_copyright_profile_id": (int(profile_id) if profile_id > 0 and store.copyright_profile(str(user["username"]),int(profile_id)) else 0),
        "copyright_files": copyright_folder_files(str(user["username"])),
    })


@app.api_route(route("/modes/{engine_mode}/{rest:path}"), methods=["GET", "HEAD", "POST"])
async def independent_engine_web(engine_mode: str, rest: str, request: Request):
    # Only the original primary app proxies independent engine interfaces.
    if os.getenv("LUM_ENGINE_INSTANCE", "engine1") != "engine1":
        raise HTTPException(404, "Engine proxy unavailable")
    user = current_user(request)
    require_heavy(user)
    if engine_mode not in ENGINE_PORTS:
        raise HTTPException(404, "Unknown independent engine")
    return await forward_engine_request(engine_mode, request, user)


@app.get(route("/api/auto-modes"))
def four_mode_status(request: Request):
    user = current_user(request)
    require_heavy(user)
    return JSONResponse(four_mode_registry())


@app.get(route("/users"), response_class=HTMLResponse)
def users_page(request: Request, message: str = "", error: str = ""):
    if SUITE_AUTH_ENABLED:
        return RedirectResponse("/user-management", status_code=303)
    user = current_user(request)
    require_admin(user)
    return templates.TemplateResponse("users.html", {
        "request": request, "user": user, "users": users.list(), "csrf": csrf(request),
        "message": message, "error": error, "build": BUILD,
    })


@app.post(route("/users/create"))
def users_create(request: Request, csrf_token: str = Form(...), username: str = Form(...), password: str = Form(...), access_level: str = Form("heavy")):
    user = current_user(request); require_admin(user); require_csrf(request, csrf_token)
    try:
        access = str(access_level or "heavy").lower().strip()
        if access not in {"heavy", "administrator"}:
            raise ValueError("Invalid access level")
        is_admin = access == "administrator"
        ui_mode = "heavy"
        users.create(username, password, is_admin=is_admin, ui_mode=ui_mode)
        label = "Administrator" if is_admin else "Full workspace"
        return redirect("/users", message=f"User {username} created · {label}")
    except Exception as exc:
        return redirect("/users", error=str(exc))


@app.post(route("/users/{user_id}/update"))
def users_update(user_id: int, request: Request, csrf_token: str = Form(...), access_level: str = Form("heavy"), is_active: str = Form("")):
    user = current_user(request); require_admin(user); require_csrf(request, csrf_token)
    try:
        access = str(access_level or "heavy").lower().strip()
        if access not in {"heavy", "administrator"}:
            raise ValueError("Invalid access level")
        is_admin = access == "administrator"
        ui_mode = "heavy"
        users.update(user_id, ui_mode=ui_mode, is_active=str(is_active).lower() in {"1", "true", "on", "yes"}, is_admin=is_admin)
        return redirect("/users", message="User access updated")
    except Exception as exc:
        return redirect("/users", error=str(exc))


@app.post(route("/mode"))
def switch_mode(request: Request, csrf_token: str = Form(...), mode: str = Form("heavy")):
    # Cached pre-upgrade forms redirect to the full workspace without switching modes.
    user = current_user(request); require_admin(user); require_csrf(request, csrf_token)
    request.session.pop("lum_admin_mode", None)
    return redirect("/app?tab=dashboard")


@app.post(route("/users/{user_id}/password"))
def users_password(user_id: int, request: Request, csrf_token: str = Form(...), password: str = Form(...)):
    user = current_user(request); require_admin(user); require_csrf(request, csrf_token)
    try:
        users.set_password(user_id, password)
        return redirect("/users", message="Password changed")
    except Exception as exc:
        return redirect("/users", error=str(exc))


@app.get(route("/reports/{name}"))
def download_report(name: str, request: Request):
    current_user(request)
    try:
        path = report_path(name)
    except Exception:
        raise HTTPException(404, "Report not found")
    return FileResponse(str(path), media_type="text/plain", filename=path.name)


@app.post(route("/actions/report-health"))
def action_report_health(request: Request, csrf_token: str = Form(...)):
    current_user(request); require_csrf(request, csrf_token)
    try:
        path = generate_health_report(store, db, worker)
        return redirect("/app", message=f"Health report created: {path.name}")
    except Exception as exc:
        return redirect("/app", error=f"Health report failed: {exc}")


@app.get(route("/api/copyright/folder-videos"))
def copyright_folder_videos(request: Request):
    user = current_user(request)
    if not OWNER_SCOPED:
        raise HTTPException(404, "Not found")
    folder = str(request.query_params.get("folder") or "")
    try:
        folder = safe_relative_folder(folder)
        folders = available_folders(str(user["username"]))
        if folder not in folders:
            raise ValueError("Select one of your own VPS folders")
        videos = copyright_folder_files(str(user["username"]),folder)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"ok": True, "folders": folders, "folder": folder,
                         "files": videos, "count": len(videos)},
                        headers={"Cache-Control": "no-store, private"})


@app.post(route("/actions/copyright/profile"))
async def copyright_add_profile(
    request: Request, csrf_token: str = Form(...), profile_name: str = Form(...),
    group_name: str = Form(""), metadata_source: str = Form("manual"),
):
    user = current_user(request)
    require_csrf(request, csrf_token)
    if os.environ.get("LUM_ENGINE_INSTANCE") != "copyright":
        raise HTTPException(404, "Not found")
    try:
        store.add_copyright_profile(str(user["username"]), profile_name, group_name, metadata_source)
        return redirect("/app?tab=profiles", message="Upload profile saved")
    except ValueError as exc:
        return redirect("/app?tab=profiles", error=str(exc))


@app.post(route("/actions/copyright/profile/update"))
def copyright_update_profile(
    request: Request, csrf_token: str = Form(...), profile_id: int = Form(...),
    profile_name: str = Form(...), group_name: str = Form(""),
    metadata_source: str = Form("manual"),
):
    user = current_user(request); require_csrf(request, csrf_token)
    if not OWNER_SCOPED:
        raise HTTPException(404,"Not found")
    try:
        if not store.update_copyright_profile(str(user["username"]),profile_id,profile_name,group_name,metadata_source):
            raise PermissionError("Profile does not belong to this account")
        return redirect("/app?tab=profiles",message="Your profile updated")
    except (ValueError, PermissionError) as exc:
        return redirect("/app?tab=profiles",error=str(exc))


@app.post(route("/actions/copyright/profile/delete"))
def copyright_delete_profile(request: Request,csrf_token: str = Form(...), profile_id: int = Form(...)):
    user = current_user(request); require_csrf(request,csrf_token)
    if not OWNER_SCOPED:
        raise HTTPException(404,"Not found")
    if not store.delete_copyright_profile(str(user["username"]),profile_id):
        return redirect("/app?tab=profiles",error="Profile does not belong to this account")
    return redirect("/app?tab=profiles",message="Saved profile deleted; queued videos and upload history preserved")


@app.post(route("/actions/add-video"))
async def add_video(
    request: Request, csrf_token: str = Form(...), drive_url: str = Form(""), title: str = Form(...),
    description: str = Form(""), tags: str = Form(""), category_id: str = Form("22"), privacy_status: str = Form("private"),
    app_upload_at: str = Form(""), made_for_kids: str = Form(""), playlist_id: str = Form(""), channel_db_id: int = Form(...),
    group_name: str = Form(""), thumbnail: UploadFile | None = File(default=None),
    source_mode: str = Form("drive_link"), video_file: UploadFile | None = File(default=None),
    folder_file: str = Form(""), vps_folder: str = Form(""), profile_id: int = Form(0),
    concat_video_link: str = Form(""), timezone_name: str = Form("Asia/Karachi"),
):
    user = current_user(request); require_csrf(request, csrf_token)
    thumb_path = ""
    owned_staging = ""
    source_original = ""
    fingerprint = ""
    source_size = 0
    try:
        upload_instance = os.getenv("LUM_ENGINE_INSTANCE", "engine1").strip().lower()
        studio_tenants.require("channel",int(channel_db_id),user)
        if upload_instance in {"copyright", "concat"}:
            # Group selection is authoritative server-side, never a client-only filter.
            membership = db.channel_group_names_map()
            memberships = [name.strip().casefold() for name in
                           str(membership.get(int(channel_db_id)) or "").replace(";",",").replace("|",",").split(",")]
            if not str(group_name or "").strip() or str(group_name).strip().casefold() not in memberships:
                raise ValueError("Select a group containing the selected owned channel")
            if upload_instance == "concat" and profile_id:
                raise ValueError("Concat Uploader does not support upload profiles")
            if profile_id:
                profile = store.copyright_profile(str(user["username"]), int(profile_id))
                if not profile:
                    raise ValueError("Upload profile not found for current account")
                group_name = str(profile["group_name"])
            if upload_instance == "concat":
                if (source_mode != "concat_link" or not concat_video_link.strip() or drive_url.strip()
                        or folder_file or vps_folder or (video_file and video_file.filename)):
                    raise ValueError("Use only an authorized Concat Video Link")
                cloud_root = os.getenv("CONCAT_CLOUD_RENDER_ROOT", "")
                if not cloud_root:
                    raise ValueError("Owner-verified Cloud Render handoff is not configured")
                owned_staging, source_original, source_size, fingerprint = stage_concat_link(
                    concat_video_link, str(user["username"]), STAGING_ROOT,
                    cloud_root=Path(cloud_root),
                    origin=os.getenv("CONCAT_PUBLIC_ORIGIN", "https://concat.scenebuilder.online"),
                )
            elif source_mode == "drive_link":
                if not drive_url.strip():
                    raise ValueError("Google Drive URL required")
                fingerprint = hashlib.sha256(("drive:" + drive_url.strip()).encode()).hexdigest()
            elif source_mode == "pc_upload":
                if video_file is None or not video_file.filename:
                    raise ValueError("Select a video file from your PC")
                source_original = Path(video_file.filename).name
                owned_staging, source_size, fingerprint = await copyright_stage_file(
                    video_file, Path(source_original).suffix.lower())
            elif source_mode == "folder":
                vps_folder = safe_relative_folder(vps_folder)
                if vps_folder not in available_folders(str(user["username"])):
                    raise ValueError("Select your own VPS folder")
                if folder_file not in copyright_folder_files(str(user["username"]),vps_folder):
                    raise ValueError("Choose an available video in your selected VPS folder")
                source_original = folder_file
                owned_staging, source_size, fingerprint = await copyright_stage_owner_file(
                    str(user["username"]), folder_file, vps_folder)
            else:
                raise ValueError("Unknown source mode")
        has_thumbnail = bool(thumbnail and thumbnail.filename)
        if upload_instance == "engine2" and not has_thumbnail:
            raise ValueError("Engine 2 requires a JPG/PNG thumbnail before queue admission.")
        if upload_instance == "song" and not has_thumbnail:
            raise ValueError("Song Uploader requires a JPG/PNG thumbnail before queue admission.")
        if upload_instance in {"shorts", "shortstudio"} and has_thumbnail:
            raise ValueError("Short Uploader does not use thumbnails.")
        if thumbnail and thumbnail.filename:
            suffix = Path(thumbnail.filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png"}:
                raise ValueError("Thumbnail must be JPG or PNG")
            payload = await thumbnail.read()
            if not payload:
                raise ValueError("Thumbnail must not be empty")
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError("Thumbnail must be 2 MB or smaller")
            temp = THUMBNAIL_ROOT / f"pending_{os.urandom(8).hex()}{suffix}"
            temp.write_bytes(payload); thumb_path = str(temp)
        app_upload_at_utc = app_upload_at
        if upload_instance == "concat":
            # Concat schedules the START of the VPS upload, not YouTube publishAt.
            app_upload_at_utc = concat_upload_at(app_upload_at, timezone_name)
        elif str(app_upload_at or "").strip():
            from datetime import datetime as _pkt_datetime, timezone as _pkt_utc
            from zoneinfo import ZoneInfo as _PktZoneInfo
            _pkt_value = _pkt_datetime.fromisoformat(str(app_upload_at).strip().replace("Z", "+00:00"))
            if _pkt_value.tzinfo is None:
                _pkt_value = _pkt_value.replace(tzinfo=_PktZoneInfo("Asia/Karachi"))
            app_upload_at_utc = _pkt_value.astimezone(_pkt_utc.utc).replace(microsecond=0).isoformat()
        # Keep publish_at empty for new projects: scheduled starts use app_upload_at.
        # Legacy projects already holding publish_at retain their original behavior.
        publish_at_utc = ""
        project_id = store.create_project({
            "owner_username": str(user["username"]), "ui_mode": user_mode(user),
            "source_mode": "local_file" if owned_staging else "drive_link",
            "drive_url": drive_url, "thumbnail_path": thumb_path,
            "staging_path": owned_staging, "source_file_name": source_original,
            "source_file_size": source_size, "source_fingerprint": fingerprint,
            "source_origin": source_mode, "vps_folder": vps_folder if source_mode == "folder" else "",
            "title": title, "description": description, "tags": tags, "category_id": category_id,
            "privacy_status": privacy_status, "app_upload_at": app_upload_at_utc,
            "publish_at": publish_at_utc,
            "made_for_kids": str(made_for_kids).lower() in {"1", "true", "on", "yes"},
            "playlist_id": playlist_id, "channel_db_id": channel_db_id, "group_name": group_name,
        })
        owned_staging = ""
        if thumb_path:
            final = THUMBNAIL_ROOT / f"{project_id}{Path(thumb_path).suffix.lower()}"
            Path(thumb_path).replace(final)
            store.update(project_id, thumbnail_path=str(final))
        # Copyright uses the independent serial worker. Re-arm it when a new
        # project is accepted; do not override an explicit pause-after-current.
        if upload_instance == "copyright" and store.setting("pause_after_current", "0") != "1":
            store.set_setting("engine_enabled", "1")
        next_tab = "queue"
        queue_message = (f"Project #{project_id} scheduled for automatic YouTube upload start"
                         if upload_instance == "concat" and app_upload_at_utc else
                         f"Project #{project_id} added to VPS queue")
        return redirect(f"/app?tab={next_tab}", message=queue_message)
    except Exception as exc:
        if owned_staging:
            Path(owned_staging).unlink(missing_ok=True)
        if thumb_path:
            Path(thumb_path).unlink(missing_ok=True)
        next_tab = "add_video"
        return redirect(f"/app?tab={next_tab}", error=str(exc))


@app.post(route("/actions/oauth-client"))
async def upload_oauth_client(request: Request, csrf_token: str = Form(...), client_json: UploadFile = File(...)):
    user = current_user(request); require_csrf(request, csrf_token)
    try:
        if OWNER_SCOPED:
            result = save_owner_client_json(str(user["username"]), await client_json.read())
            message = f"Your private OAuth client is ready: {result['filename']}"
        else:
            require_admin(user)
            result = save_shared_client_json(await client_json.read())
            message = f"Shared OAuth client ready: {result['filename']}"
        tab = "authentication" if user_mode(user) == "simple" else "channels"
        return redirect(f"/app?tab={tab}", message=message)
    except Exception as exc:
        tab = "authentication" if user_mode(user) == "simple" else "channels"
        return redirect(f"/app?tab={tab}", error=str(exc))


@app.post(route("/actions/oauth-start"))
def oauth_start_action(
    request: Request,
    csrf_token: str = Form(...),
    target_channel_db_id: int = Form(...),
):
    user = current_user(request); require_csrf(request, csrf_token)
    tab = "authentication" if user_mode(user) == "simple" else "channels"
    try:
        target_id = int(target_channel_db_id or 0)
        if target_id <= 0:
            raise ValueError("Select a channel before starting Google authorization.")
        studio_tenants.require("channel",target_id,user)
        target = db.get_channel(target_id)
        if not target:
            raise ValueError("Selected channel no longer exists. Refresh Accounts & Tokens and select it again.")
        expected_channel_id = str(target.get("channel_id") or "").strip()
        if not (expected_channel_id.startswith("UC") and len(expected_channel_id) >= 20):
            raise ValueError("Selected channel does not have a verified UC Channel ID. Add the exact /channel/UC... identity in Sheets, sync it, then authorize.")

        # Desktop parity: identity, browser/profile and proxy all come from the
        # selected channel row. The web form cannot generalize an OAuth result
        # and attach it to a different channel afterwards.
        target_email = str(target.get("gmail") or target.get("email") or "").strip()
        profile_name = str(target.get("browser_profile_name") or target.get("browser_profile") or "").strip()
        proxy_config = {
            "proxy_scheme": str(target.get("proxy_scheme") or "http").strip(),
            "proxy_host": str(target.get("proxy_host") or "").strip(),
            "proxy_port": str(target.get("proxy_port") or "").strip(),
            "proxy_username": str(target.get("proxy_username") or "").strip(),
            "proxy_password": str(target.get("proxy_password") or ""),
        }
        if not proxy_config["proxy_host"]:
            proxy_config = {}

        result = start_oauth(
            "shared_oauth_client",
            proxy_config=proxy_config,
            profile_name=profile_name,
            target_channel_db_id=target_id,
            target_email=target_email,
            client_path=(owner_client_for_oauth(str(user["username"]))
                         if OWNER_SCOPED else None),
            owner_username=str(user["username"]),
        )
        request.session["lum_oauth_url"] = result["authorization_url"]
        request.session["lum_oauth_session_id"] = result["session_id"]
        request.session["lum_oauth_target_channel_db_id"] = target_id
        request.session["lum_oauth_target_channel_id"] = expected_channel_id
        request.session["lum_oauth_target_channel_name"] = str(target.get("channel_name") or expected_channel_id)
        return redirect(
            f"/app?tab={tab}",
            message=f"Authorization locked to {target.get('channel_name') or 'selected channel'} ({expected_channel_id}).",
        )
    except Exception as exc:
        return redirect(f"/app?tab={tab}", error=str(exc))


@app.post(route("/actions/oauth-complete"))
def oauth_complete_action(request: Request, csrf_token: str = Form(...), callback_url: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token)
    session_id = str(request.session.get("lum_oauth_session_id") or "")
    tab = "authentication" if user_mode(user) == "simple" else "channels"
    if not session_id:
        return redirect(f"/app?tab={tab}", error="Start authentication first")
    try:
        studio_tenants.require("channel",int(request.session.get("lum_oauth_target_channel_db_id") or 0),user)
        result = complete_oauth(db, session_id, callback_url, owner_username=str(user["username"]))
        request.session.pop("lum_oauth_url", None); request.session.pop("lum_oauth_session_id", None)
        request.session.pop("lum_oauth_target_channel_db_id", None); request.session.pop("lum_oauth_target_channel_id", None); request.session.pop("lum_oauth_target_channel_name", None)
        return redirect(f"/app?tab={tab}", message=f"Authenticated: {result['channel_name']} ({result['channel_id']})")
    except Exception as exc:
        return redirect(f"/app?tab={tab}", error=str(exc))


@app.post(route("/actions/channel-network/{channel_id}"))
def channel_network(channel_id: int, request: Request, csrf_token: str = Form(...), profile_name: str = Form(""), proxy_scheme: str = Form("http"), proxy_host: str = Form(""), proxy_port: str = Form(""), proxy_username: str = Form(""), proxy_password: str = Form("")):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        studio_tenants.require("channel",channel_id,user)
        db.update_channel_network_profile(channel_id, browser_profile_name=profile_name, proxy_scheme=proxy_scheme, proxy_host=proxy_host, proxy_port=proxy_port, proxy_username=proxy_username, proxy_password=proxy_password)
        return redirect("/app?tab=channels", message="Channel network profile updated")
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


def _owned_or_admin(user: dict[str, Any], project_id: int) -> dict[str, Any]:
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if (OWNER_SCOPED or not int(user.get("is_admin") or 0)) and str(project.get("owner_username") or "").lower() != str(user.get("username") or "").lower():
        raise HTTPException(403, "Project does not belong to this user")
    return project


@app.post(route("/actions/copyright/queue/pause"))
def copyright_pause_my_queue(request: Request, csrf_token: str = Form(...),
                             scheduled_only: str = Form("")):
    user = current_user(request); require_csrf(request, csrf_token)
    if not OWNER_SCOPED:
        raise HTTPException(404, "Not found")
    if os.getenv("LUM_ENGINE_INSTANCE") == "concat" and scheduled_only == "1":
        store.set_owner_scheduled_paused(str(user["username"]), True)
        return redirect("/app?tab=queue", message="Scheduled uploads paused for your account; active upload may finish.")
    store.set_owner_queue_paused(str(user["username"]), True)
    return redirect("/app?tab=queue",
                    message="Your manual queue paused. Any upload already in progress may finish.")


@app.post(route("/actions/copyright/queue/start"))
def copyright_start_my_queue(request: Request, csrf_token: str = Form(...),
                             scheduled_only: str = Form("")):
    user = current_user(request); require_csrf(request, csrf_token)
    if not OWNER_SCOPED:
        raise HTTPException(404, "Not found")
    if os.getenv("LUM_ENGINE_INSTANCE") == "concat" and scheduled_only == "1":
        store.set_owner_scheduled_paused(str(user["username"]), False)
        return redirect("/app?tab=queue", message="Automatic scheduled uploads resumed for your account.")
    if os.getenv("LUM_ENGINE_INSTANCE") == "concat":
        store.set_setting("engine_enabled", "1")  # Only this dedicated Concat instance.
    if store.setting("engine_enabled", "1") != "1":
        return redirect("/app?tab=queue", error="Shared worker is paused; your queue cannot be started until it is available.")
    store.set_owner_queue_paused(str(user["username"]), False)
    return redirect("/app?tab=queue", message="Your waiting queue is enabled (manual jobs only).")


@app.post(route("/actions/copyright/metadata/{project_id}"))
def copyright_edit_metadata(
    project_id: int, request: Request, csrf_token: str = Form(...),
    title: str = Form(...), description: str = Form(""), tags: str = Form(""),
    category_id: str = Form("22"), privacy_status: str = Form("private"),
):
    user = current_user(request); require_csrf(request, csrf_token)
    if not OWNER_SCOPED:
        raise HTTPException(404, "Not found")
    # Unlike admin cross-tenant inspection, editing is *always* owner-only.
    project = store.get(project_id)
    if not project or str(project.get("owner_username") or "").casefold() != str(user["username"]).casefold():
        raise HTTPException(403, "Project does not belong to this account")
    try:
        if not store.edit_waiting_metadata(project_id, str(user["username"]),
                                           title=title, description=description,
                                           tags=tags, category_id=category_id,
                                           privacy_status=privacy_status):
            raise ValueError("Only waiting jobs may have metadata changed")
        return redirect("/app?tab=queue", message=f"Project #{project_id} metadata updated")
    except ValueError as exc:
        return redirect("/app?tab=queue", error=str(exc))


def _copyright_retry_owned_project(project_id: int, project: dict[str, Any]) -> None:
    if str(project.get("status") or "") not in {"FAILED", "PAUSED"}:
        raise ValueError("Only failed or paused projects can be retried")
    core_id = int(project.get("core_video_id") or 0)
    if core_id:
        core = db.get_video(core_id) or {}
        if str(core.get("recovery_state") or "") == "REVIEW_REQUIRED":
            raise ValueError("Crash review required before a fresh upload can be authorized")
        if str(core.get("status") or "") == "FAILED":
            db.queue_failed_for_retry(exclude_auto_engine=True, video_ids=[core_id])
        elif str(core.get("status") or "") == "PAUSED":
            db.resume_paused(exclude_auto_engine=True, video_ids=[core_id])
    if not store.retry(project_id):
        raise ValueError("Project is not retryable")


@app.post(route("/actions/copyright/queue/bulk"))
async def copyright_bulk_queue(request: Request):
    user = current_user(request)
    if not OWNER_SCOPED:
        raise HTTPException(404, "Not found")
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    selected = form.getlist("project_ids")
    action = str(form.get("action") or "").strip()
    if action not in {"cancel", "retry", "remove"} or not 1 <= len(selected) <= 100:
        return redirect("/app?tab=queue", error="Choose up to 100 jobs and a valid bulk action")
    try:
        ids = sorted({int(value) for value in selected})
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid project IDs")
    if len(ids) != len(selected) or any(pid <= 0 for pid in ids):
        raise HTTPException(400, "Invalid or duplicate project IDs")
    # Validate the *entire* set before touching any row, including for admins.
    owned = [store.get(pid) for pid in ids]
    if any(not p or str(p.get("owner_username") or "").casefold() != str(user["username"]).casefold()
           for p in owned):
        raise HTTPException(403, "Bulk selection contains an unowned project")
    count = 0
    failed = 0
    for pid, project in zip(ids, owned):
        try:
            if action == "remove":
                if store.archive_terminal(pid,str(user["username"])):
                    count += 1
                else:
                    failed += 1
            elif action == "cancel":
                if store.cancel_waiting(pid, str(user["username"])):
                    count += 1
                else:
                    failed += 1
            else:
                _copyright_retry_owned_project(pid, project)
                count += 1
        except (ValueError, PermissionError):
            failed += 1
    return redirect("/app?tab=queue",
                    message=f"Own queue: {action} applied to {count} job(s); {failed} skipped. Files and history retained.")


@app.post(route("/actions/copyright/queue/remove"))
def copyright_remove_terminal_job(request: Request, csrf_token: str = Form(...), project_id: int = Form(...)):
    user = current_user(request)
    require_csrf(request,csrf_token)
    if not OWNER_SCOPED:
        raise HTTPException(404, "Not found")
    _owned_or_admin(user,project_id)
    if store.archive_terminal(project_id,str(user["username"])):
        return redirect("/app?tab=queue",message="Removed from queue; upload history and media preserved.")
    return redirect("/app?tab=queue",error="Only completed or cancelled jobs can be removed.")

@app.post(route("/actions/retry/{project_id}"))
def retry_project(project_id: int, request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); project = _owned_or_admin(user, project_id)
    try:
        _copyright_retry_owned_project(project_id, project)
        tab = "upload" if user_mode(user) == "simple" else "queue"
        return redirect(f"/app?tab={tab}", message=f"Project #{project_id} returned to queue")
    except Exception as exc:
        tab = "upload" if user_mode(user) == "simple" else "queue"
        return redirect(f"/app?tab={tab}", error=str(exc))


@app.post(route("/actions/cancel/{project_id}"))
def cancel_waiting_project(project_id: int, request: Request, csrf_token: str = Form(...)):
    user = current_user(request)
    require_csrf(request, csrf_token)
    if not OWNER_SCOPED:
        raise HTTPException(404, "Not found")
    project = _owned_or_admin(user, project_id)
    if store.cancel_waiting(project_id, str(project["owner_username"])):
        return redirect("/app?tab=queue", message=f"Waiting project #{project_id} cancelled; media preserved")
    return redirect("/app?tab=queue", error="Project is no longer waiting; active or completed uploads cannot be cancelled here")


@app.post(route("/actions/authorize-fresh/{project_id}"))
def authorize_fresh(project_id: int, request: Request, csrf_token: str = Form(...), confirm_text: str = Form("")):
    user = current_user(request); require_csrf(request, csrf_token); project = _owned_or_admin(user, project_id)
    if confirm_text.strip().upper() != "NO REMOTE VIDEO":
        return redirect("/app?tab=queue", error="Type NO REMOTE VIDEO only after checking YouTube Studio")
    try:
        core_id = int(project.get("core_video_id") or 0)
        if not core_id or not db.resolve_recovery_review([core_id]):
            raise ValueError("Project is not awaiting crash review")
        store.update(project_id, status="QUEUED", error_message="User confirmed no remote upload exists; fresh retry authorized.")
        store.event(project_id, "Fresh retry authorized after manual YouTube Studio review", "WARNING")
        return redirect("/app?tab=queue", message=f"Fresh retry authorized for project #{project_id}")
    except Exception as exc:
        return redirect("/app?tab=queue", error=str(exc))


@app.post(route("/actions/pause-after-current"))
def pause_after_current(request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    store.set_setting("pause_after_current", "1")
    return redirect("/app?tab=queue", message="Uploader will pause after the current project")


@app.post(route("/actions/stop-current"))
def stop_current(request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    worker.request_stop_current()
    return redirect("/app?tab=queue", message="Safe stop requested for the current upload")


@app.post(route("/actions/resume"))
def resume(request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    store.set_setting("engine_enabled", "1"); store.set_setting("pause_after_current", "0")
    return redirect("/app?tab=queue", message="Uploader resumed")



# LUM_PC_ENGINE_WEB_BRIDGE_V4_ROUTES
@app.post(route("/actions/sheets/save"))
def sheets_save(
    request: Request,
    csrf_token: str = Form(...),
    source_id: str = Form(""),
    sheet_name: str = Form(""),
    sheet_url_or_id: str = Form(...),
    enabled: str = Form(""),
):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        sid = int(source_id) if str(source_id or "").strip() else None
        if sid:
            studio_tenants.require("sheet",sid,user)
        elif OWNER_SCOPED or not int(user.get("is_admin") or 0):
            existing={str(row.get("sheet_url_or_id") or "").strip()
                      for row in db.list_sheet_sources()}
            if str(sheet_url_or_id or "").strip() in existing:
                raise PermissionError("Sheet already exists in another workspace")
        saved_id = web_save_sheet_source(
            db,
            source_id=sid,
            sheet_name=sheet_name,
            sheet_url_or_id=sheet_url_or_id,
            enabled=str(enabled).lower() in {"1", "true", "on", "yes"},
        )
        if OWNER_SCOPED or not int(user.get("is_admin") or 0):
            studio_tenants.bind("sheet",int(saved_id),user["username"])
        return redirect(
            f"/app?tab=sheets&source_id={saved_id}",
            message="Sheet source saved.",
        )
    except Exception as exc:
        return redirect(
            f"/app?tab=sheets" + (f"&source_id={source_id}" if str(source_id or "").strip().isdigit() else ""),
            error=str(exc),
        )


@app.post(route("/actions/sheets/delete"))
def sheets_delete(
    request: Request,
    csrf_token: str = Form(...),
    source_id: int = Form(...),
):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        studio_tenants.require("sheet",int(source_id),user)
        if OWNER_SCOPED or not int(user.get("is_admin") or 0):
            studio_tenants.delete_owned_sheet(user["username"],int(source_id))
        else:
            web_delete_sheet_source(db,int(source_id))
        return redirect(
            "/app?tab=sheets",
            message="Sheet source removed from current inventory. Saved token/history data was retained.",
        )
    except Exception as exc:
        return redirect(
            f"/app?tab=sheets&source_id={int(source_id)}",
            error=str(exc),
        )


@app.post(route("/actions/sheets/sync"))
def sheets_sync(
    request: Request,
    csrf_token: str = Form(...),
    source_id: int = Form(...),
):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    if store.setting("sheets_sync_enabled", "0") != "1":
        return redirect(
            f"/app?tab=sheets&source_id={int(source_id)}",
            error="Enable Sheets Sync on Dashboard before running synchronization.",
        )
    try:
        if not OWNER_SCOPED and int(user.get("is_admin") or 0):
            result=web_sync_sheet_source_by_id(db,int(source_id))
        else:
            studio_tenants.require("sheet",int(source_id),user)
            source=db.get_sheet_source(int(source_id))
            if not source:
                raise ValueError("Sheet source does not exist")
            raw=tenant_fetch_sheet_csv(source["sheet_url_or_id"],None)
            if not raw:
                raise ValueError("Sheet returned zero rows; no existing records changed")
            prepared=tenant_rows_to_sheet_rows(raw,str(tenant_token_dir))
            result=studio_tenants.sync_owned_sheet_rows(
                user["username"],int(source_id),prepared,
                sync_sheet_groups=OWNER_SCOPED)
        if result.get("ok"):
            return redirect(
                f"/app?tab=sheets&source_id={int(source_id)}",
                message=f"Sheet synced · {int(result.get('rows') or 0)} row(s). "
                        "Owned Sheet groups synchronized; existing OAuth unchanged.",
            )
        return redirect(
            f"/app?tab=sheets&source_id={int(source_id)}",
            error=str(result.get("error") or result.get("status") or "Sheet sync failed."),
        )
    except Exception as exc:
        return redirect(
            f"/app?tab=sheets&source_id={int(source_id)}",error=str(exc))


@app.post(route("/actions/sheets/sync-all"))
def sheets_sync_all(request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    if store.setting("sheets_sync_enabled", "0") != "1":
        return redirect(
            "/app?tab=sheets",
            error="Enable Sheets Sync on Dashboard before running synchronization.",
        )
    try:
        if OWNER_SCOPED:
            allowed = studio_tenants.allowed_ids("sheet", user)
            sources = [src for src in db.list_sheet_sources()
                       if int(src.get("id") or 0) in (allowed or set())
                       and int(src.get("enabled") or 0) == 1]
            count = 0
            failures = []
            rows = 0
            for source in sources:
                try:
                    raw = tenant_fetch_sheet_csv(source["sheet_url_or_id"], None)
                    if not raw:
                        raise ValueError("Sheet returned zero rows")
                    prepared = tenant_rows_to_sheet_rows(raw, str(tenant_token_dir))
                    outcome = studio_tenants.sync_owned_sheet_rows(
                        str(user["username"]), int(source["id"]), prepared,
                        sync_sheet_groups=True)
                    if not outcome.get("ok"):
                        raise ValueError(str(outcome.get("error") or "Sync failed"))
                    count += 1
                    rows += int(outcome.get("rows") or 0)
                except Exception as exc:
                    failures.append(f"{source.get('sheet_name') or source['id']}: {exc}")
            if failures:
                return redirect("/app?tab=sheets",
                    error=f"Updated {count} owned sheet(s); {len(failures)} failed: " + "; ".join(failures[:3]))
            return redirect("/app?tab=sheets",
                message=f"Updated {count} owned sheet(s), {rows} row(s) and Sheet groups; other users unchanged.")
        result = web_sync_all_sheet_sources(db)
    except Exception as exc:
        return redirect("/app?tab=sheets", error=str(exc))
    if result.get("ok"):
        return redirect(
            "/app?tab=sheets",
            message=f"Updated {int(result.get('count') or 0)} enabled sheet(s) · {int(result.get('rows') or 0)} row(s).",
        )
    failures = [
        str(r.get("sheet_name") or r.get("source_id") or "Sheet")
        for r in result.get("results", [])
        if not r.get("ok")
    ]
    return redirect(
        "/app?tab=sheets",
        error=f"Sync completed with {int(result.get('failed') or 0)} failed source(s): " + ", ".join(failures[:5]),
    )



# STUDIO_TENANT_SAFE_ONBOARDING_V1
@app.post(route("/actions/studio-tenant/channel-create"))
def studio_tenant_channel_create(request: Request,csrf_token: str=Form(...),
                                 channel_id: str=Form(...),channel_name: str=Form(...),
                                 google_account_email: str=Form("")):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    if os.getenv("LUM_ENGINE_INSTANCE") not in {"song","shortstudio","copyright"}:
        if os.getenv("LUM_ENGINE_INSTANCE") != "concat":
            raise HTTPException(404,"Not found")
    try:
        cid=studio_tenants.create_channel(user["username"],channel_id,channel_name,
                                          google_account_email)
        return redirect("/app?tab=channels",message=f"YouTube channel #{cid} created for this account; connect Google OAuth to authorize.")
    except Exception as exc:
        return redirect("/app?tab=channels",error=str(exc))


@app.post(route("/actions/studio-tenant/group-create"))
def studio_tenant_group_create(request: Request,csrf_token: str=Form(...),
                               group_name: str=Form(...),channel_ids: list[int]=Form([])):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    if os.getenv("LUM_ENGINE_INSTANCE") not in {"song","shortstudio","copyright"}:
        if os.getenv("LUM_ENGINE_INSTANCE") != "concat":
            raise HTTPException(404,"Not found")
    try:
        gid=studio_tenants.create_group(user["username"],group_name,channel_ids)
        return redirect("/app?tab=channels",message=f"Private channel group #{gid} created for this account.")
    except Exception as exc:
        return redirect("/app?tab=channels",error=str(exc))


# STUDIO_TENANT_ASSIGNMENT_V1
@app.post(route("/actions/studio-tenant/grant"))
def studio_tenant_grant(
    request: Request,
    csrf_token: str = Form(...),
    entity_kind: str = Form(...),
    entity_id: int = Form(...),
    owner_username: str = Form(...),
):
    """Admin explicitly assigns an idle legacy resource without touching media."""
    user=current_user(request); require_admin(user); require_csrf(request,csrf_token)
    if OWNER_SCOPED:
        raise HTTPException(404, "Copyright Uploader uses independent user-owned resources")
    try:
        kind,entity_id=studio_tenants.identity(entity_kind,entity_id)
        owner=studio_tenants.username(owner_username)
        table={"channel":"channels","group":"channel_groups",
               "profile":"auto_engine_profiles","sheet":"sheet_sources"}[kind]
        with db.connect() as con:
            if not con.execute(f"SELECT 1 FROM {table} WHERE id=?",(entity_id,)).fetchone():
                raise ValueError("Resource not found in current Studio instance")
            if kind=="channel":
                pending=con.execute(
                    "SELECT COUNT(*) FROM videos WHERE channel_db_id=? AND"
                    " UPPER(status) NOT IN ('UPLOADED','PUBLISHED','FAILED','CANCELLED')",
                    (entity_id,)).fetchone()[0]
                groups=[int(row[0]) for row in con.execute(
                    "SELECT group_id FROM channel_group_members WHERE channel_db_id=?",
                    (entity_id,)).fetchall()]
                for gid in groups:
                    assigned=studio_tenants.owner("group",gid)
                    if assigned and assigned!=owner:
                        raise PermissionError("Channel belongs to a different user's group")
            elif kind=="group":
                pending=con.execute(
                    "SELECT COUNT(*) FROM videos WHERE auto_engine_profile_id IN"
                    " (SELECT id FROM auto_engine_profiles WHERE channel_group_id=?)"
                    " AND UPPER(status) NOT IN ('UPLOADED','PUBLISHED','FAILED','CANCELLED')",
                    (entity_id,)).fetchone()[0]
                members=[int(row[0]) for row in con.execute(
                    "SELECT channel_db_id FROM channel_group_members WHERE group_id=?",
                    (entity_id,)).fetchall()]
                if any(studio_tenants.owner("channel",cid)!=owner for cid in members):
                    raise PermissionError("Assign all member channels to the same user first")
            elif kind=="profile":
                pending=con.execute(
                    "SELECT COUNT(*) FROM videos WHERE auto_engine_profile_id=? AND"
                    " UPPER(status) NOT IN ('UPLOADED','PUBLISHED','FAILED','CANCELLED')",
                    (entity_id,)).fetchone()[0]
                row=con.execute("SELECT channel_group_id FROM auto_engine_profiles WHERE id=?",
                                (entity_id,)).fetchone()
                if row and row[0] and studio_tenants.owner("group",int(row[0]))!=owner:
                    raise PermissionError("Assign parent channel group to this account first")
            else:
                pending=con.execute(
                    "SELECT COUNT(*) FROM videos WHERE channel_db_id IN"
                    " (SELECT id FROM channels WHERE sheet_source_id=?) AND"
                    " UPPER(status) NOT IN ('UPLOADED','PUBLISHED','FAILED','CANCELLED')",
                    (entity_id,)).fetchone()[0]
                children=[int(row[0]) for row in con.execute(
                    "SELECT id FROM channels WHERE sheet_source_id=?", (entity_id,)).fetchall()]
                if any(studio_tenants.owner("channel",cid)!=owner for cid in children):
                    raise PermissionError("Assign referenced channels to this user first")
        if pending:
            raise PermissionError("Resource has outstanding queue jobs; assignment blocked")
        studio_tenants.assign_by_admin(kind,entity_id,owner,user)
        return redirect("/app?tab=channels",
                        message=f"{kind.title()} #{entity_id} assigned to {owner}.")
    except Exception as exc:
        return redirect("/app?tab=channels",error=str(exc))


# LUM_CHANNELS_PC_SYNC_V5_ROUTES
@app.post(route("/actions/channels/status"))
def channels_status(
    request: Request,
    csrf_token: str = Form(...),
    status: str = Form(...),
    channel_ids: list[int] = Form([]),
):
    user=current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        for channel_id in channel_ids:
            studio_tenants.require("channel",channel_id,user)
        updated=set_status_bulk(db, channel_ids, status,
            prefer_stored_token_paths=OWNER_SCOPED)
        return redirect("/app?tab=channels", message=f"Updated {updated} channel(s) to {str(status).upper()}.")
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


@app.post(route("/actions/channels/delete-manual"))
def channels_delete_manual(
    request: Request,
    csrf_token: str = Form(...),
    channel_ids: list[int] = Form([]),
):
    user=current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        for channel_id in channel_ids:
            studio_tenants.require("channel",channel_id,user)
        result=delete_manual_selected(db, channel_ids)
        return redirect(
            "/app?tab=channels",
            message=(
                f"Deleted manual channels: {int(result.get('deleted') or 0)} · "
                f"Blocked by queue/history: {int(result.get('blocked_with_queue') or 0)} · "
                f"Sheet rows skipped: {int(result.get('not_manual') or 0)}"
            ),
        )
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


@app.post(route("/actions/channels/rescan-tokens"))
def channels_rescan_tokens(
    request: Request,
    csrf_token: str = Form(...),
    scope: str = Form("selected"),
    channel_ids: list[int] = Form([]),
):
    user=current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        if OWNER_SCOPED and str(scope).lower()=="all_missing":
            ids=sorted(studio_tenants.allowed_ids("channel",user) or [])
        else:
            if str(scope).lower()=="all_missing" and not int(user.get("is_admin") or 0):
                raise PermissionError("Global token scan is administrator-only")
            ids=None if str(scope).lower()=="all_missing" else channel_ids
        for channel_id in (ids or []):
            studio_tenants.require("channel",channel_id,user)
        if OWNER_SCOPED and not ids:
            return redirect("/app?tab=channels",message="No owned channels need scanning.")
        copyright_private = OWNER_SCOPED
        result=rescan_channel_tokens(db, ids, prefer_stored_token_paths=copyright_private)
        summary = (
            f"Token scan · checked {result['checked']} · valid {result['valid']} · "
            f"missing {result['missing']} · invalid {result['invalid']}"
            if copyright_private else
            f"Token scan · checked {result['checked']} · updated {result['updated']} · "
            f"missing {result['missing']} · invalid {result['invalid']}"
        )
        return redirect("/app?tab=channels", message=summary)
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


@app.post(route("/actions/channels/test-connections"))
def channels_test_connections(
    request: Request,
    csrf_token: str = Form(...),
    channel_ids: list[int] = Form([]),
):
    user=current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        for channel_id in channel_ids:
            studio_tenants.require("channel",channel_id,user)
        result=test_connections(db, channel_ids)
        return redirect(
            "/app?tab=channels",
            message=f"Connection tests · {result['passed']} passed · {result['failed']} failed.",
        )
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


@app.post(route("/actions/channels/json-upload"))
async def channels_json_upload(
    request: Request,
    csrf_token: str = Form(...),
    secret_kind: str = Form(...),
    replace_existing: str = Form("0"),
    files: list[UploadFile] = File(...),
):
    user=current_user(request); require_admin(user); require_csrf(request, csrf_token); require_heavy(user)
    kind=str(secret_kind or "").strip().lower()
    if kind not in {"client","token"}:
        return redirect("/app?tab=channels", error="Invalid JSON storage type.")
    installed=kept=invalid=conflict=0
    errors=[]
    replace=str(replace_existing).lower() in {"1","true","on","yes"}
    for upload in files[:250]:
        try:
            payload=await upload.read()
            if kind=="client":
                item=install_client_json(upload.filename or "client.json", payload, replace_valid=replace)
                if item.get("status")=="INSTALLED":
                    installed+=1
                    try:
                        save_client_json(upload.filename or item.get("name") or "client.json", payload)
                    except Exception as exc:
                        errors.append(f"Web OAuth mirror {upload.filename}: {exc}")
                elif item.get("status")=="KEPT": kept+=1
                else:
                    invalid+=1; errors.append(f"{upload.filename}: {item.get('reason')}")
            else:
                item=install_token_json(db, upload.filename or "token.json", payload, replace_valid=replace)
                if item.get("status")=="INSTALLED": installed+=1
                elif item.get("status")=="KEPT": kept+=1
                elif item.get("status") in {"CONFLICT","UNMATCHED"}:
                    conflict+=1; errors.append(f"{upload.filename}: {item.get('reason')}")
                else:
                    invalid+=1; errors.append(f"{upload.filename}: {item.get('reason')}")
        except Exception as exc:
            invalid+=1; errors.append(f"{upload.filename}: {exc}")
    if kind=="token":
        try: rescan_channel_tokens(db, None)
        except Exception as exc: errors.append(f"Post-upload token scan: {exc}")
    msg=f"{'OAuth clients' if kind=='client' else 'Channel tokens'} · installed {installed} · kept {kept} · invalid {invalid} · conflicts {conflict}"
    if errors:
        return redirect("/app?tab=channels", error=msg+" · "+"; ".join(errors[:3]))
    return redirect("/app?tab=channels", message=msg)


@app.post(route("/actions/copyright/credentials/upload"))
async def copyright_credentials_upload(
    request: Request, csrf_token: str = Form(...), secret_kind: str = Form(...),
    replace_existing: str = Form("0"), files: list[UploadFile] = File(...),
):
    """PC JSON restoration with identical admin/member controls and private owner mapping."""
    user = current_user(request)
    require_csrf(request, csrf_token)
    if not OWNER_SCOPED:
        raise HTTPException(404, "Not found")
    kind = str(secret_kind or "").strip().lower()
    if kind not in {"client", "token"}:
        return redirect("/app?tab=channels", error="Unknown credential JSON type")
    if not files:
        return redirect("/app?tab=channels", error="Select at least one credential JSON file")
    if kind == "client" and len(files) != 1:
        return redirect("/app?tab=channels", error="Choose exactly one OAuth client JSON")
    installed=kept=invalid=unmatched=0
    failures=[]
    replace = str(replace_existing).lower() in {"1", "on", "true", "yes"}
    size_limit = 1024 * 1024 if kind == "client" else 4 * 1024 * 1024
    for upload in files:
        try:
            name = Path(str(upload.filename or "").replace(chr(92),"/")).name
            if not name.lower().endswith(".json"):
                raise ValueError("Only .json files are accepted")
            content = await upload.read(size_limit + 1)
            if not content or len(content) > size_limit:
                raise ValueError("Empty or oversized JSON file")
            if kind == "client":
                result = import_owner_client(str(user["username"]), content, replace_valid=replace)
            else:
                result = import_owner_token(db,studio_tenants,str(user["username"]),name,content,replace_valid=replace)
            state=str(result.get("status") or "INVALID")
            if state == "INSTALLED": installed+=1
            elif state == "KEPT": kept+=1
            elif state in {"UNMATCHED","CONFLICT"}:
                unmatched+=1
                failures.append(name + ": " + str(result.get("reason") or "Not matched"))
            else:
                invalid+=1
                failures.append(name + ": " + str(result.get("reason") or "Invalid JSON"))
        except Exception as exc:
            invalid+=1
            failures.append(Path(str(upload.filename or "file")).name + ": " + str(exc))
        finally:
            await upload.close()
    summary = (("OAuth client" if kind == "client" else "Channel tokens") +
               f" · installed {installed} · kept {kept} · invalid {invalid} · unmatched {unmatched}")
    if failures:
        return redirect("/app?tab=channels", error=summary + " · " + "; ".join(failures[:3]))
    return redirect("/app?tab=channels", message=summary)


@app.post(route("/actions/pc-sync/upload"))
async def pc_sync_upload(
    request: Request,
    csrf_token: str = Form(...),
    source_label: str = Form(""),
    files: list[UploadFile] = File(...),
):
    user=current_user(request); require_admin(user); require_csrf(request, csrf_token); require_heavy(user)
    payloads=[]
    total=0
    for upload in files[:5000]:
        data=await upload.read()
        total+=len(data)
        if total>2*1024*1024*1024:
            raise HTTPException(413, "PC Sync snapshot exceeds 2 GB safety limit.")
        payloads.append((upload.filename or "upload.bin", data))
    return save_snapshot_uploads(db, payloads, source_label)


@app.post(route("/actions/pc-sync/apply"))
def pc_sync_apply(
    request: Request,
    csrf_token: str = Form(...),
    snapshot_id: str = Form(...),
):
    user=current_user(request); require_admin(user); require_csrf(request, csrf_token); require_heavy(user)
    return apply_snapshot(db, snapshot_id)





# LUM_FULL_AUTO_ENGINE_V6_ROUTES

def _v6_selected_ids(form, key: str = "video_ids"):
    try:
        return sorted({int(x) for x in form.getlist(key) if str(x).strip().isdigit() and int(x)>0})
    except Exception:
        value=form.get(key) if hasattr(form,"get") else None
        return [int(value)] if str(value or "").isdigit() else []


def _v6_profile(profile_id: int,request: Request=None):
    profile=db.get_auto_engine_profile(int(profile_id))
    if not profile:
        raise ValueError("Auto Engine profile not found.")
    if request is None:
        raise ValueError("Verified Studio account is required for profile changes.")
    user=current_user(request)
    studio_tenants.require("profile",int(profile_id),user)
    if not int(user.get("is_admin") or 0):
        # A profile's ACL alone does not authorize new members added to
        # its group later. Revalidate its live target set before scheduling.
        group_id=int(profile.get("channel_group_id") or 0)
        if group_id:
            studio_tenants.require("group",group_id,user)
            with db.connect() as con:
                members=[int(row[0]) for row in con.execute(
                    "SELECT channel_db_id FROM channel_group_members WHERE group_id=?",
                    (group_id,)).fetchall()]
            for cid in members:
                studio_tenants.require("channel",cid,user)
        direct_channel=int(profile.get("channel_db_id") or 0)
        if direct_channel:
            studio_tenants.require("channel",direct_channel,user)
    return profile


@app.post(route("/actions/auto-v6/profile/save"))
async def auto_v6_profile_save(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    try:
        raw=str(form.get("profile_id") or "").strip()
        pid=int(raw) if raw.isdigit() else None
        if pid:
            _v6_profile(pid,request)
        group_id=str(form.get("channel_group_id") or "").strip()
        channel_id=str(form.get("channel_db_id") or "").strip()
        if group_id.isdigit():
            studio_tenants.require("group",int(group_id),user)
        if channel_id.isdigit():
            studio_tenants.require("channel",int(channel_id),user)
        if os.getenv("LUM_ENGINE_INSTANCE")=="shortstudio":
            folder_name=str(form.get("short_vps_folder") or "").strip()
            if folder_name and not int(user.get("is_admin") or 0):
                folder=v6_shortstudio_folder_path(folder_name)
                marker=folder/".studio_owner"
                if (marker.is_symlink() or not marker.is_file() or
                        marker.read_text(encoding="utf-8").strip().casefold() !=
                        str(user["username"]).casefold()):
                    raise PermissionError("Short Maker folder belongs to another user")
        fields=dict(form); fields["_song_owner"]=str(user.get("username") or "")
        saved=v6_save_profile_from_form(db,v6_store,fields,pid)
        if not int(user.get("is_admin") or 0):
            studio_tenants.bind("profile",int(saved),user["username"])
        return redirect(f"/app?tab=auto_engine&auto_profile_id={saved}",message=f"Auto Engine profile #{saved} saved.")
    except Exception as exc:
        return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/profile/toggle"))
def auto_v6_profile_toggle(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),enabled: str=Form("0")):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        _v6_profile(profile_id,request)
        state=str(enabled).lower() in {"1","true","on","yes"}
        db.set_auto_engine_profile_enabled(int(profile_id),state)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message=f"Profile {'enabled' if state else 'disabled'}.")
    except Exception as exc:
        return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/profile/delete"))
def auto_v6_profile_delete(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        nonterminal=[j for j in db.list_auto_engine_jobs(limit=5000) if int(j.get("auto_engine_profile_id") or 0)==int(profile_id) and str(j.get("status") or "").upper() not in {"UPLOADED","PUBLISHED","FAILED","CANCELLED"}]
        if nonterminal: raise ValueError("Profile still has queued/running/review jobs. Finish or safely remove those jobs before deleting the profile.")
        pid=int(profile_id)
        _v6_profile(pid,request)
        is_channel=v6_store.is_channel_profile(pid)
        db.delete_auto_engine_profile(pid)
        with v6_store.connect() as con:
            con.execute("DELETE FROM song_group_settings WHERE profile_id=?",(pid,))
        if is_channel:
            with v6_store.connect() as con:
                con.execute("DELETE FROM channel_metadata_rows WHERE profile_id=?",(pid,))
                con.execute("DELETE FROM drive_thumbnails WHERE profile_id=?",(pid,))
                con.execute("DELETE FROM channel_profile_bindings WHERE profile_id=?",(pid,))
        return redirect("/app?tab=auto_engine",message="Profile removed. Upload history was retained for recovery; the channel is available for a new profile.")
    except Exception as exc:
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",error=str(exc))


@app.post(route("/actions/auto-v6/songmaker/import"))
def import_songmaker_local(request: Request, csrf_token: str=Form(...),
                           profile_id: int=Form(...), job_id: str=Form(...)):
    # Only the isolated Song Uploader process may accept this handoff.
    if os.getenv("LUM_ENGINE_INSTANCE") != "song" or not SUITE_AUTH_ENABLED:
        raise HTTPException(404, "Song Maker local import is available only in Song Uploader")
    user = current_user(request)
    require_csrf(request, csrf_token)
    owner = str(user.get("username") or "")
    if not owner or not secrets.compare_digest(owner, str(request.headers.get("x-auth-user") or "")):
        raise HTTPException(403, "Song Maker and Song Uploader user mismatch")
    try:
        from .songmaker_local import import_job
        result = import_job(v6_store, int(profile_id), owner, job_id)
        return V6JSONResponse({"ok": True, **result})
    except ValueError as exc:
        return V6JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except Exception:
        return V6JSONResponse({"ok": False, "error": "Song Maker local import unavailable; no queue was modified"}, status_code=503)


@app.post(route("/actions/auto-v6/sync-scan"))
def auto_v6_sync_scan(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        profile=_v6_profile(profile_id,request)
        if v6_store.is_channel_profile(int(profile_id)):
            result=v6_sync_channel_profile(db,v6_store,profile,queue_valid=True)
        else:
            result=v6_preflight_profile(db,v6_store,profile)
        return V6JSONResponse(result)
    except Exception as exc:
        return V6JSONResponse({"ok":False,"errors":[str(exc)],"warnings":[]},status_code=400)


@app.post(route("/actions/auto-v6/sync-all"))
def auto_v6_sync_all(request: Request,csrf_token: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    created=0; synced=0; errors=[]
    for profile in db.list_auto_engine_profiles():
        folder_owner=v6_store.group_settings(int(profile.get("id") or 0)).get("song_folder_owner")
        if folder_owner and folder_owner!=str(user.get("username") or ""): continue
        pid=int(profile.get("id") or 0)
        if int(profile.get("enabled") or 0)!=1:
            continue
        try:
            if v6_store.is_channel_profile(pid):
                result=v6_sync_channel_profile(db,v6_store,profile,queue_valid=True)
            elif os.environ.get("LUM_ENGINE_INSTANCE")=="song":
                result=v6_preflight_profile(db,v6_store,profile)
                if not result.get("ok"):
                    raise ValueError("; ".join(result.get("errors") or ["Source validation failed"]))
            else:
                continue
            created+=int(result.get("created") or 0); synced+=1
        except Exception as exc:
            errors.append(f"{profile.get('category_name') or pid}: {exc}")
    if created: v6_store.set_engine(running=True,pause_after_current=False)
    message=f"Synced {synced} channel profile(s); queued {created} new job(s)."
    if errors: message+=f" {len(errors)} profile(s) need attention."
    return redirect("/app?tab=auto_engine",message=message,error="; ".join(errors[:3]) if errors and synced==0 else "")


@app.post(route("/actions/auto-v6/profile/resume"))
def auto_v6_profile_resume(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    pid=int(profile_id)
    _v6_profile(pid,request)
    v6_resume_channel_profile(v6_store,pid)
    with db.connect() as con:
        con.execute("UPDATE videos SET status='QUEUED',error_message='',updated_at=CURRENT_TIMESTAMP WHERE auto_engine_profile_id=? AND status='PAUSED'",(pid,))
    return redirect(f"/app?tab=auto_engine&auto_profile_id={pid}",message="Channel Auto Engine profile resumed. Run Sync Channel to validate current resources.")


@app.post(route("/actions/auto-v6/job/upload-now"))
def auto_v6_job_upload_now(request: Request,csrf_token: str=Form(...),video_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    vid=int(video_id); meta=v6_store.channel_job_meta(vid)
    if meta: _v6_profile(int(meta["profile_id"]),request)
    if not meta:
        return redirect("/app?tab=auto_engine",error="Upload Now is available only for channel Video-ID Auto Engine jobs.")
    with db.connect() as con:
        row=con.execute("SELECT status FROM videos WHERE id=?",(vid,)).fetchone()
        if not row:
            return redirect("/app?tab=auto_engine",error="Queue job was not found.")
        status=str(row["status"] or "").upper()
        if status not in {"SCHEDULED","QUEUED","WAITING_SLOT"}:
            return redirect("/app?tab=auto_engine",error=f"Upload Now is available only for scheduled/pending jobs, not {status}. Use Retry Failed for recovery jobs.")
        con.execute("UPDATE videos SET status='QUEUED',app_upload_at=NULL,created_at='1970-01-01 00:00:00',error_message='',updated_at=CURRENT_TIMESTAMP WHERE id=?",(vid,))
    v6_store.set_engine(running=True,pause_after_current=False)
    return redirect("/app?tab=auto_engine",message=f"Job #{vid} moved to the front of the global queue after the current upload.")


@app.post(route("/actions/auto-v6/job/edit"))
async def auto_v6_job_edit(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    vid=int(str(form.get("video_id") or "0")); meta=v6_store.channel_job_meta(vid)
    if not meta:
        return redirect("/app?tab=auto_engine",error="Queued job edit is available only for channel Video-ID jobs.")
    _v6_profile(int(meta["profile_id"]),request)
    title=str(form.get("title") or "").strip(); description=str(form.get("description") or "")
    if not title: return redirect("/app?tab=auto_engine",error="Title cannot be empty.")
    if len(title)>100: return redirect("/app?tab=auto_engine",error="Title cannot exceed 100 characters.")
    with db.connect() as con:
        row=con.execute("SELECT status FROM videos WHERE id=?",(vid,)).fetchone()
        if not row: return redirect("/app?tab=auto_engine",error="Queue job was not found.")
        status=str(row["status"] or "").upper()
        if status not in {"QUEUED","SCHEDULED","WAITING_SLOT","PAUSED","FAILED"}:
            return redirect("/app?tab=auto_engine",error=f"Job is locked while status is {status}.")
        con.execute("UPDATE videos SET title=?,description=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(title,description,vid))
    return redirect("/app?tab=auto_engine",message=f"Queued job #{vid} updated. Sheet data was not changed.")


@app.post(route("/actions/auto-v6/queue-preflight"))
def auto_v6_queue_preflight(request: Request,csrf_token: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        return V6JSONResponse(v6_queue_preflight(db,v6_store))
    except Exception as exc:
        return V6JSONResponse({"ok":False,"issues":[str(exc)],"warnings":[]},status_code=400)


@app.get(route("/actions/auto-v6/preview"))
def auto_v6_preview(request: Request,profile_id: int):
    current_user(request); require_heavy(current_user(request))
    try:
        result=v6_planner(db,v6_store,_v6_profile(profile_id,request),preview_only=True)
        return V6JSONResponse(result)
    except Exception as exc:
        return V6JSONResponse({"ready":False,"jobs":[],"blocked":[str(exc)]},status_code=400)


@app.post(route("/actions/auto-v6/generate"))
def auto_v6_generate(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),confirmation: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        if str(confirmation).strip()!="GENERATE": raise ValueError('Type GENERATE exactly to create real queue jobs.')
        profile=_v6_profile(profile_id,request)
        result=v6_generate_queue(db,v6_store,profile)
        v6_mark_schedule_skipped_today(db,profile)
        if int(result.get("created") or 0) and str(profile.get("work_mode") or "") == "auto":
            v6_store.set_engine(running=True,pause_after_current=False)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message=f"Generated {int(result.get('created') or 0)} job(s); duplicate blocks {int(result.get('duplicates_blocked') or 0)}.")
    except Exception as exc:
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",error=str(exc))


# LUM_AJAX_GENERATE_STATUS_V1
@app.post(route("/actions/auto-v6/generate-json"))
def auto_v6_generate_json(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),confirmation: str=Form("GENERATE")):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    pid=int(profile_id)
    try:
        if str(confirmation).strip()!="GENERATE":
            raise ValueError("Generate confirmation is invalid.")
        profile=_v6_profile(pid,request)
        result=v6_generate_queue(db,v6_store,profile)
        v6_mark_schedule_skipped_today(db,profile)
        created=int(result.get("created") or 0)
        if created and str(profile.get("work_mode") or "") == "auto":
            v6_store.set_engine(running=True,pause_after_current=False)
        rows=db.list_auto_engine_jobs(limit=5000)
        terminal={"UPLOADED","PUBLISHED","FAILED","CANCELLED"}
        active=[j for j in rows if str(j.get("status") or "").upper() not in terminal]
        profile_active=[j for j in active if int(j.get("auto_engine_profile_id") or 0)==pid]
        videos=len(v6_store.drive_videos(pid,True))
        thumbs=sum(1 for t in v6_store.thumbs(pid) if str(t.get("status") or "").upper()=="ACTIVE")
        if created<=0:
            return V6JSONResponse({
                "ok":False,"profile_id":pid,"created":0,
                "duplicates_blocked":int(result.get("duplicates_blocked") or 0),
                "queue_total":len(active),"profile_queue_total":len(profile_active),
                "available_videos":videos,"active_thumbnails":thumbs,
                "errors":["No new queue jobs were created. The selected items may already be reserved or blocked as duplicates. Run Preview to see eligibility."],
            },status_code=409)
        return V6JSONResponse({
            "ok":True,"profile_id":pid,"created":created,
            "duplicates_blocked":int(result.get("duplicates_blocked") or 0),
            "queue_total":len(active),"profile_queue_total":len(profile_active),
            "available_videos":videos,"active_thumbnails":thumbs,
            "message":f"Generated {created} queue job(s).",
        })
    except Exception as exc:
        return V6JSONResponse({"ok":False,"profile_id":pid,"created":0,"errors":[str(exc)]},status_code=400)


# Song / Short Uploader: restored group-level controls above the profiles list.
# A queue generation is intentionally different from Start All: it does NOT
# start the worker or arm the scheduled uploader.
@app.post(route("/actions/auto-v6/generate-all"))
def auto_v6_generate_all(request: Request, csrf_token: str=Form(...), confirmation: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    if str(confirmation).strip()!="GENERATE ALL":
        return redirect("/app?tab=auto_engine",error="Confirm GENERATE ALL to build the queue.")
    if os.environ.get("LUM_ENGINE_INSTANCE") not in {"song","shortstudio"}:
        return redirect("/app?tab=auto_engine",error="This group action belongs to Song or Short Uploader.")
    created=0; duplicates=0; checked=0; skipped=[]
    for raw in db.list_auto_engine_profiles():
        pid=int(raw.get("id") or 0)
        owner=v6_store.group_settings(pid).get("song_folder_owner")
        if owner and owner!=str(user.get("username") or ""): continue
        if int(raw.get("enabled") or 0)!=1: continue
        checked+=1
        try:
            profile=_v6_profile(pid,request)
            result=v6_generate_queue(db,v6_store,profile)
            count=int(result.get("created") or 0)
            created+=count
            duplicates+=int(result.get("duplicates_blocked") or 0)
            if count:
                v6_mark_schedule_skipped_today(db,profile)
        except Exception as exc:
            skipped.append(f"{raw.get('category_name') or pid}: {exc}")
    message=f"Queue generation: {created} new job(s) from {checked} enabled profile(s); {duplicates} duplicate(s) blocked. Upload engine state unchanged."
    if skipped:
        message+=f" {len(skipped)} profile(s) skipped: "+"; ".join(skipped[:3])
    return redirect("/app?tab=auto_engine",message=message)


@app.get(route("/actions/auto-v6/preview-all"))
def auto_v6_preview_all(request: Request):
    user=current_user(request); require_heavy(user)
    if os.environ.get("LUM_ENGINE_INSTANCE") not in {"song","shortstudio"}:
        return V6JSONResponse({"ok":False,"error":"Group preview unavailable for this engine."},status_code=400)
    summaries=[]; total=0
    for raw in db.list_auto_engine_profiles():
        pid=int(raw.get("id") or 0)
        owner=v6_store.group_settings(pid).get("song_folder_owner")
        if owner and owner!=str(user.get("username") or ""): continue
        if int(raw.get("enabled") or 0)!=1: continue
        item={"profile_id":pid,"name":str(raw.get("category_name") or f"Profile #{pid}"),
              "ready":False,"eligible_jobs":0,"blocked":[],"next":[]}
        try:
            plan=v6_planner(db,v6_store,_v6_profile(pid,request),preview_only=True)
            jobs=plan.get("jobs") or []
            item["ready"]=bool(plan.get("ready"))
            item["eligible_jobs"]=len(jobs)
            item["blocked"]=[str(e) for e in (plan.get("blocked") or [])[:3]]
            item["next"]=[{"video":str(j.get("filename") or j.get("video_key") or ""),
                           "channel":str(j.get("channel_name") or j.get("channel_id") or "")}
                          for j in jobs[:3]]
            total+=len(jobs)
        except Exception as exc:
            item["blocked"]=[str(exc)]
        summaries.append(item)
    return V6JSONResponse({"ok":True,"profiles":summaries,"eligible_jobs":total})


# Profile-level controls are Short Uploader only. The worker respects the
# manual run scope so Start one cannot lease another profile's queued videos.
@app.post(route("/actions/auto-v6/start-profile"))
def auto_v6_start_profile(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    pid=int(profile_id)
    if os.environ.get("LUM_ENGINE_INSTANCE")!="shortstudio":
        return redirect("/app?tab=auto_engine",error="Individual Start is only available in Short Uploader.")
    previously_enabled=None
    try:
        profile=_v6_profile(pid,request)
        if profile.get("channel_db_id"):
            raise ValueError("Use the legacy channel profile controls for this profile.")
        previously_enabled=bool(int(profile.get("enabled") or 0))
        if not previously_enabled:
            db.set_auto_engine_profile_enabled(pid,True)
            profile=_v6_profile(pid,request)
        result=v6_generate_queue(db,v6_store,profile)
        count=int(result.get("created") or 0)
        if count:
            v6_mark_schedule_skipped_today(db,profile)
        current=v6_store.engine().get("manual_profile_ids")
        active={int(value) for value in str(current or "").split(",") if value.strip().isdigit()} if current is not None else set()
        active.add(pid)
        v6_store.set_manual_profile_ids(sorted(active))
        v6_store.set_engine(running=True,pause_after_current=False,error="")
        return redirect(f"/app?tab=auto_engine&auto_profile_id={pid}",message=f"Started profile #{pid} only: {count} new job(s), {int(result.get('duplicates_blocked') or 0)} duplicate(s) blocked. Other queued profiles remain untouched.")
    except Exception as exc:
        if previously_enabled is False:
            try: db.set_auto_engine_profile_enabled(pid,False)
            except Exception: pass
        return redirect(f"/app?tab=auto_engine&auto_profile_id={pid}",error=str(exc))


@app.post(route("/actions/auto-v6/pause-profile"))
def auto_v6_pause_profile(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    pid=int(profile_id)
    if os.environ.get("LUM_ENGINE_INSTANCE")!="shortstudio":
        return redirect("/app?tab=auto_engine",error="Individual Pause is only available in Short Uploader.")
    try:
        profile=_v6_profile(pid,request)
        if profile.get("channel_db_id"):
            raise ValueError("Use the legacy channel profile controls for this profile.")
        db.set_auto_engine_profile_enabled(pid,False)
        current=v6_store.engine().get("manual_profile_ids")
        if current is not None:
            active={int(value) for value in str(current or "").split(",") if value.strip().isdigit()}
            active.discard(pid)
            v6_store.set_manual_profile_ids(sorted(active))
        return redirect(f"/app?tab=auto_engine&auto_profile_id={pid}",message=f"Paused profile #{pid}. Pending jobs are retained; any in-flight upload is allowed to finish.")
    except Exception as exc:
        return redirect(f"/app?tab=auto_engine&auto_profile_id={pid}",error=str(exc))


@app.post(route("/actions/auto-v6/start-all"))
def auto_v6_start_all(request: Request,csrf_token: str=Form(...),confirmation: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    if str(confirmation).strip()!="START ALL":
        return redirect("/app?tab=auto_engine",error='Type START ALL exactly.')
    created=0; skipped=[]
    for profile in db.list_auto_engine_profiles():
        folder_owner=v6_store.group_settings(int(profile.get("id") or 0)).get("song_folder_owner")
        if folder_owner and folder_owner!=str(user.get("username") or ""): continue
        if int(profile.get("enabled") or 0)!=1: continue
        try:
            result=v6_generate_queue(db,v6_store,profile)
            created+=int(result.get("created") or 0)
            v6_mark_schedule_skipped_today(db,profile)
        except Exception as exc:
            skipped.append(f"{profile.get('category_name')}: {exc}")
    v6_store.set_manual_profile_ids(None) if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio" else None
    if created: v6_store.set_engine(running=True,pause_after_current=False)
    text=f"Start All created {created} job(s)."
    if skipped: text+=f" {len(skipped)} profile(s) skipped: "+"; ".join(skipped[:3])
    return redirect("/app?tab=auto_engine",message=text)


@app.post(route("/actions/auto-v6/engine"))
def auto_v6_engine(request: Request,csrf_token: str=Form(...),action: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    act=str(action or "").strip().lower()
    if act in {"start","resume"}:
        if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio": v6_store.set_manual_profile_ids(None)
        v6_store.set_engine(running=True,pause_after_current=False,error="")
    elif act=="pause_after_current": v6_store.set_engine(running=True,pause_after_current=True)
    elif act=="stop": v6_store.set_engine(running=False,pause_after_current=False)
    elif act=="arm_scheduler":
        ready=[]; invalid=[]
        for profile in db.list_auto_engine_profiles():
            folder_owner=v6_store.group_settings(int(profile.get("id") or 0)).get("song_folder_owner")
            if folder_owner and folder_owner!=str(user.get("username") or ""): continue
            try:
                if int(profile.get("enabled") or 0)!=1 or str(profile.get("work_mode") or "").strip().lower()!="auto":
                    continue
                channels=v6_target_channels(db,profile)
                if not channels:
                    invalid.append(f"{profile.get('category_name')}: no eligible channel")
                    continue
                if v6_store.is_channel_profile(int(profile.get("id") or 0)):
                    ready.append(profile)
                    continue
                if int(profile.get("schedule_enabled") or 0)!=1:
                    continue
                times=v6_parse_schedule_times(profile.get("schedule_times"))[:max(1,int(profile.get("daily_upload_count") or 1))]
                if not times:
                    invalid.append(f"{profile.get('category_name')}: no schedule")
                    continue
                ready.append(profile)
            except Exception as exc:
                invalid.append(f"{profile.get('category_name')}: {exc}")
        if not ready:
            detail=(" " + "; ".join(invalid[:3])) if invalid else ""
            db.add_log(None,"WARN","Scheduled Uploads ARM blocked: no enabled automatic profile has both a valid schedule and eligible channels."+detail,None)
            return redirect("/app?tab=auto_engine",error="Cannot arm automation: configure at least one enabled channel Auto Engine profile with an eligible channel, or a valid legacy scheduled profile." )
        if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio": v6_store.set_manual_profile_ids(None)
        v6_store.set_scheduler_armed(True)
        db.add_log(None,"INFO",f"Scheduled Uploads ARMED from web control. Ready profiles: {len(ready)}.",None)
        return redirect("/app?tab=auto_engine",message=f"Auto Sync & Scheduled Uploads ARMED. {len(ready)} profile(s) ready.")
    elif act=="disarm_scheduler":
        v6_store.set_scheduler_armed(False)
        db.add_log(None,"INFO","Scheduled Uploads DISARMED from web control.",None)
        return redirect("/app?tab=auto_engine",message="Auto Sync & Scheduled Uploads DISARMED. Automatic sync/schedule triggers are paused.")
    else: return redirect("/app?tab=auto_engine",error="Unknown Auto Engine control.")
    return redirect("/app?tab=auto_engine",message=f"Auto Engine control applied: {act.replace('_',' ')}.")


@app.post(route("/actions/auto-v6/concurrency"))
def auto_v6_concurrency(
    request: Request,
    csrf_token: str=Form(...),
    concurrency: int=Form(1),
    profile_id: int=Form(0),
):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        saved=v6_store.set_concurrency(int(concurrency))
        suffix=f"&auto_profile_id={int(profile_id)}" if int(profile_id or 0)>0 else ""
        return redirect(f"/app?tab=auto_engine{suffix}",message=f"Concurrent Uploads request saved as {saved}. Effective VPS safety limit remains 1.")
    except Exception as exc:
        return redirect("/app?tab=auto_engine",error=str(exc))


# Short Uploader inline schedule editor: persist exactly one profile and never
# start workers or change the global scheduler as a side-effect of saving.
@app.post(route("/actions/auto-v6/profile-schedule/save"))
def auto_v6_profile_schedule_save(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),
                                 schedule_times: str=Form(""),schedule_enabled: str=Form("0")):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    pid=int(profile_id)
    if os.environ.get("LUM_ENGINE_INSTANCE")!="shortstudio":
        return redirect("/app?tab=auto_engine",error="Inline scheduling is Short Uploader only.")
    try:
        profile=_v6_profile(pid,request)
        if profile.get("channel_db_id"):
            raise ValueError("Use the existing channel Video-ID schedule editor.")
        expected=max(1,min(100,int(profile.get("daily_upload_count") or 1)))
        raw=str(schedule_times or "").strip()
        # Empty or partial input is not a valid replacement for a saved daily schedule.
        if not raw or len([value for value in raw.split(",") if value.strip()])!=expected:
            raise ValueError(f"Choose all {expected} PKT video times before saving.")
        parsed=v6_parse_schedule_times(raw)
        if len(parsed)!=expected:
            raise ValueError(f"Choose {expected} distinct PKT video times; duplicates are not allowed.")
        armed=str(schedule_enabled or "").strip().lower() in {"1","true","on","yes"}
        data=dict(profile)
        data["schedule_times"]=",".join(parsed)
        data["schedule_enabled"]=armed
        # Explicitly checking 'Automatic schedule' enables the automatic work
        # mode for this profile. Otherwise preserve the current work mode.
        if armed: data["work_mode"]="auto"
        db.save_auto_engine_profile(data,pid)
        mode="ON" if armed else "OFF"
        note=" Global Arm Scheduled Uploads is still required." if armed and not int(v6_store.engine().get("scheduler_armed") or 0) else ""
        return redirect(f"/app?tab=auto_engine&auto_profile_id={pid}",message=f"Profile #{pid}: {expected} PKT times saved; automatic schedule {mode}.{note}")
    except Exception as exc:
        return redirect(f"/app?tab=auto_engine&auto_profile_id={pid}",error=str(exc))


@app.post(route("/actions/auto-v6/profile-schedules/save"))
async def auto_v6_profile_schedules_save(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    updated=0
    errors=[]
    current_raw=str(form.get("current_profile_id") or "0")
    current_profile_id=int(current_raw) if current_raw.isdigit() else 0
    for profile in db.list_auto_engine_profiles():
        folder_owner=v6_store.group_settings(int(profile.get("id") or 0)).get("song_folder_owner")
        if folder_owner and folder_owner!=str(user.get("username") or ""): continue
        pid=int(profile.get("id") or 0)
        try:
            raw_enabled=str(form.get(f"schedule_enabled_{pid}") or "0").lower()
            raw_times=str(form.get(f"schedule_times_{pid}") or profile.get("schedule_times") or "09:00")
            parsed_times=v6_parse_schedule_times(raw_times)
            if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio" and not profile.get("channel_db_id") and raw_enabled in {"1","true","on","yes"}:
                expected=max(1,int(profile.get("daily_upload_count") or 1))
                if len(parsed_times)!=expected:
                    raise ValueError(f"Choose {expected} distinct PKT times (currently {len(parsed_times)}).")
            times=",".join(parsed_times)
            data=dict(profile)
            data["schedule_enabled"]=raw_enabled in {"1","true","on","yes"}
            data["schedule_times"]=times
            if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio" and not profile.get("channel_db_id") and data["schedule_enabled"]:
                data["work_mode"]="auto"
            db.save_auto_engine_profile(data,pid)
            updated+=1
        except Exception as exc:
            errors.append(f"{profile.get('category_name') or pid}: {exc}")
    suffix=f"&auto_profile_id={current_profile_id}" if current_profile_id>0 else ""
    if errors:
        return redirect(f"/app?tab=auto_engine{suffix}",error=f"Updated {updated} schedule(s); "+"; ".join(errors[:3]))
    return redirect(f"/app?tab=auto_engine{suffix}",message=f"Profile schedules updated for {updated} profile(s).")


@app.post(route("/actions/auto-v6/retry-failed"))
async def auto_v6_retry_failed(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form)
    for selected_id in ids:
        source=v6_store.job_source(selected_id)
        if source: _v6_profile(int(source["profile_id"]),request)
    candidates=[j for j in db.list_auto_engine_jobs(limit=5000) if str(j.get("status") or "")=="FAILED" and str(j.get("recovery_state") or "")!="REVIEW_REQUIRED"]
    if ids: candidates=[j for j in candidates if int(j.get("id") or 0) in ids]
    safe=[]; blocked=0
    for job in candidates:
        current=db.get_video(int(job["id"])) or {}
        if str(current.get("channel_status") or "")!="ACTIVE": blocked+=1; continue
        if v6_classify_google_json(str(current.get("token_file_path") or ""))!=V6_VALID_TOKEN: blocked+=1; continue
        safe.append(int(job["id"]))
    count=db.queue_failed_for_retry(auto_engine_only=True,video_ids=safe) if safe else 0
    if count: v6_store.set_engine(running=True,pause_after_current=False)
    return redirect("/app?tab=auto_engine",message=f"Retry queued {count}; blocked by channel/token safety {blocked}.")


@app.post(route("/actions/auto-v6/resume-paused"))
def auto_v6_resume_paused(request: Request,csrf_token: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    count=db.resume_paused(auto_engine_only=True)
    if count: v6_store.set_engine(running=True,pause_after_current=False)
    return redirect("/app?tab=auto_engine",message=f"Resumed {count} safe paused job(s).")


@app.post(route("/actions/auto-v6/clear-done"))
def auto_v6_clear_done(request: Request,csrf_token: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    count=db.clear_completed_auto_engine_jobs()
    return redirect("/app?tab=auto_engine",message=f"Cleared {count} completed queue row(s). Permanent upload/script history was retained.")


@app.post(route("/actions/auto-v6/remove-selected"))
async def auto_v6_remove_selected(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form)
    for selected_id in ids:
        source=v6_store.job_source(selected_id)
        if source: _v6_profile(int(source["profile_id"]),request)
    count=db.delete_auto_engine_jobs(ids)
    with v6_store.connect() as con:
        for vid in ids:
            if not db.get_video(vid):
                con.execute("UPDATE job_sources SET state='CANCELLED',updated_at=CURRENT_TIMESTAMP WHERE video_id=?",(vid,))
    return redirect("/app?tab=auto_engine",message=f"Removed {count} safe queue job(s). Active uploads were protected.")


@app.post(route("/actions/auto-v6/recovery-clear"))
async def auto_v6_recovery_clear(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    if str(form.get("confirmation") or "").strip()!="I CHECKED YOUTUBE":
        return redirect("/app?tab=auto_engine",error='Type I CHECKED YOUTUBE exactly after checking YouTube Studio.')
    ids=_v6_selected_ids(form)
    for selected_id in ids:
        source=v6_store.job_source(selected_id)
        if source: _v6_profile(int(source["profile_id"]),request)
    count=db.resolve_recovery_review(ids)
    with v6_store.connect() as con:
        for vid in ids:
            con.execute("UPDATE job_sources SET state='QUEUED',last_error='',updated_at=CURRENT_TIMESTAMP WHERE video_id=?",(vid,))
    return redirect("/app?tab=auto_engine",message=f"Recovery review cleared for {count} job(s).")


@app.post(route("/actions/auto-v6/thumbnails/upload"))
async def auto_v6_thumbnail_upload(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),files: list[UploadFile]=File(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    profile=_v6_profile(profile_id,request); installed=duplicates=invalid=0; errors=[]
    source=v6_store.source(int(profile_id))
    if str(source.get("thumbnail_source_mode") or "pc_drive") == "drive":
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",error="This profile is Drive-only for thumbnails. Change Thumbnail Source Mode to PC or PC + Drive before uploading from PC.")
    for item in files[:500]:
        try:
            data=await item.read()
            result=v6_store.add_thumbnail_bytes(int(profile_id),str(profile.get("category_name") or ""),item.filename or "thumb.jpg",data,"PC")
            if result.get("status")=="INSTALLED": installed+=1
            else: duplicates+=1
        except Exception as exc:
            invalid+=1; errors.append(f"{item.filename}: {exc}")
    message=f"PC thumbnails: installed {installed}, duplicates {duplicates}, invalid {invalid}."
    return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message=message if not errors else message+" "+"; ".join(errors[:2]))


@app.post(route("/actions/auto-v6/thumbnails/sync-drive"))
def auto_v6_thumbnail_sync_drive(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        p=_v6_profile(profile_id,request); source=v6_store.source(int(profile_id))
        if str(source.get("thumbnail_source_mode") or "pc_drive") == "pc":
            raise ValueError("This profile is PC-upload-only for thumbnails. Change Thumbnail Source Mode to Drive or PC + Drive before syncing Drive thumbnails.")
        result=v6_store.sync_drive_thumbnails(int(profile_id),str(p.get("category_name") or ""))
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message=f"Drive thumbnails · installed {result.get('installed',0)} · duplicates {result.get('duplicates',0)} · source removed {result.get('removed_marked',0)}.")
    except Exception as exc:
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",error=str(exc))


@app.post(route("/actions/auto-v6/thumbnails/status"))
async def auto_v6_thumbnail_status(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form,"asset_ids"); status=str(form.get("status") or "").upper()
    try:
        count=v6_store.set_thumb_status(ids,status)
        return redirect("/app?tab=auto_engine",message=f"Updated {count} thumbnail(s) to {status}.")
    except Exception as exc: return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/thumbnails/move"))
async def auto_v6_thumbnail_move(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form,"asset_ids"); target_id=int(form.get("target_profile_id") or 0)
    try:
        target=_v6_profile(target_id,request); moved,blocked,duplicates=v6_store.move_thumbs(ids,target_id,str(target.get("category_name") or ""),db)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={target_id}",message=f"Moved {moved} thumbnail(s) to {target.get('category_name')} · live-job protected {len(blocked)} · duplicates skipped {len(duplicates)}.")
    except Exception as exc: return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/thumbnails/trash"))
async def auto_v6_thumbnail_trash(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form,"asset_ids")
    moved,blocked=v6_store.trash_thumbs(ids,db)
    return redirect("/app?tab=auto_engine",message=f"Moved {moved} thumbnail(s) to 7-day Trash; {len(blocked)} attached to live queue and protected.")


@app.post(route("/actions/auto-v6/thumbnails/restore"))
def auto_v6_thumbnail_restore(request: Request,csrf_token: str=Form(...),asset_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    asset=v6_store.thumb(int(asset_id)); name=""
    if asset:
        p=db.get_auto_engine_profile(int(asset.get("profile_id") or 0)); name=str((p or {}).get("category_name") or "")
    ok=v6_store.restore_thumb(int(asset_id),name)
    return redirect("/app?tab=auto_engine",message="Thumbnail restored as DISABLED; enable it when ready." if ok else "Thumbnail could not be restored.")


@app.post(route("/actions/auto-v6/thumbnails/empty-trash"))
def auto_v6_thumbnail_empty_trash(request: Request,csrf_token: str=Form(...),confirmation: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    if str(confirmation or "").strip() != "EMPTY TRASH":
        return redirect("/app?tab=auto_engine",error='Type EMPTY TRASH exactly to permanently remove trashed thumbnails.')
    count=v6_store.empty_trash()
    return redirect("/app?tab=auto_engine",message=f"Permanently deleted {count} thumbnail(s) from Trash.")


@app.post(route("/actions/auto-v6/thumbnails/reset-rotation"))
def auto_v6_thumbnail_reset_rotation(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    with db.connect() as con: con.execute("UPDATE auto_engine_profiles SET thumbnail_cursor=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",(int(profile_id),))
    return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message="Thumbnail rotation cursor reset to #1.")


@app.get(route("/actions/auto-v6/thumbnail/{asset_id}"))
def auto_v6_thumbnail_file(request: Request,asset_id: int):
    user=current_user(request); require_heavy(user)
    asset=v6_store.thumb(int(asset_id))
    if not asset: raise HTTPException(404,"Thumbnail not found.")
    path=_V6Path(str(asset.get("stored_path") or ""))
    if not path.is_file(): raise HTTPException(404,"Thumbnail file is missing.")
    media={".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".webp":"image/webp"}.get(path.suffix.lower(),"application/octet-stream")
    return V6FileResponse(str(path),media_type=media,headers={"Content-Disposition":f'inline; filename="{path.name}"'})


@app.get(route("/actions/auto-v6/upload-records.csv"))
def auto_v6_upload_records_csv(request: Request):
    user=current_user(request); require_heavy(user)
    v6_store.import_pc_filename_history(db)
    path=v6_store.export_records_csv()
    return V6FileResponse(str(path),filename="auto_engine_upload_records.csv",media_type="text/csv")


@app.post(route("/actions/auto-v6/missed/run"))
def auto_v6_missed_run(request: Request,csrf_token: str=Form(...),missed_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    with v6_store.connect() as con: row=con.execute("SELECT * FROM missed_alarms WHERE id=? AND state='REVIEW_REQUIRED'",(int(missed_id),)).fetchone()
    if not row: return redirect("/app?tab=auto_engine",error="Missed alarm review item not found.")
    item=dict(row); profile=_v6_profile(int(item["profile_id"]),request); channels=v6_target_channels(db,profile)
    max_jobs=len(channels) if str(profile.get("upload_scope") or "per_channel")=="per_channel" else 1
    try:
        result=v6_generate_queue(db,v6_store,profile,run_key=str(item["run_key"]),max_jobs_override=max_jobs)
        v6_store.resolve_missed(int(missed_id),"RUN")
        if int(result.get("created") or 0): v6_store.set_engine(running=True,pause_after_current=False)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile['id'])}",message=f"Missed alarm run now: created {int(result.get('created') or 0)} job(s).")
    except Exception as exc: return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/missed/skip"))
def auto_v6_missed_skip(request: Request,csrf_token: str=Form(...),missed_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    with v6_store.connect() as con: row=con.execute("SELECT * FROM missed_alarms WHERE id=? AND state='REVIEW_REQUIRED'",(int(missed_id),)).fetchone()
    if not row: return redirect("/app?tab=auto_engine",error="Missed alarm review item not found.")
    item=dict(row); db.record_auto_engine_run(int(item["profile_id"]),str(item["run_key"]),0); v6_store.resolve_missed(int(missed_id),"SKIPPED")
    return redirect("/app?tab=auto_engine",message="Missed alarm skipped for today.")


@app.post(route("/actions/settings"))
def save_settings(request: Request, csrf_token: str = Form(...), sheets_sync_enabled: str = Form("0"), auto_engine_enabled: str = Form("0"), bulk_import_enabled: str = Form("0"), delete_staged_after_verified: str = Form("1")):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    if OWNER_SCOPED:
        raise HTTPException(404, "Copyright uploader mode settings are fixed")
    settings = {
        "sheets_sync_enabled": sheets_sync_enabled,
        "auto_engine_enabled": auto_engine_enabled,
        "delete_staged_after_verified": delete_staged_after_verified,
    }
    # Bulk Import is not a Song Uploader mode. Ignore stale or forged form
    # values without changing the other isolated uploader instances.
    if os.getenv("LUM_ENGINE_INSTANCE", "engine1") == "song":
        settings["bulk_import_enabled"] = "0"
    else:
        settings["bulk_import_enabled"] = bulk_import_enabled
    for key, value in settings.items():
        store.set_setting(key, "1" if str(value).lower() in {"1", "true", "on", "yes"} else "0")
    store.set_setting("max_active_uploads", "1")
    store.set_setting("remote_verification_enabled", "1")
    store.set_setting("drive_delete_source_after_verified", "0")
    return redirect("/app?tab=dashboard", message="Settings saved")

# LUM_DUAL_AUTH_CONNECTIONS_V2_ROUTES
from fastapi import HTTPException as _DualAuthHTTPException
from fastapi.responses import JSONResponse as _DualAuthJSONResponse, RedirectResponse as _DualAuthRedirectResponse
from .dual_auth_connections import DualAuthConnections as _DualAuthConnections


# Initialize at application import so schema, queue snapshots, and the legacy
# compatibility projection exist before worker traffic reaches the app.
_dual_auth = _DualAuthConnections(db)


def _dual_auth_is_admin(user):
    if isinstance(user, dict):
        return bool(user.get("is_admin"))
    return bool(getattr(user, "is_admin", False))


@app.get(route("/actions/auth-connections/status"))
def dual_auth_status(request: Request):
    user=current_user(request)
    payload=_dual_auth.status()
    payload.update(_dual_auth.inventory())
    allowed=studio_tenants.allowed_ids("channel",user)
    if allowed is not None:
        payload["connections"]=[
            row for row in payload.get("connections",[])
            if int(row.get("channel_db_id") or 0) in allowed
        ]
        payload["client_id_hint"]=""
        payload["updated_at"]=""
    return _DualAuthJSONResponse(payload)


@app.post(route("/actions/google-oauth/configure"))
async def dual_auth_google_configure(request: Request):
    user = current_user(request)
    if not _dual_auth_is_admin(user):
        raise _DualAuthHTTPException(status_code=403, detail="Administrator access required.")
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    try:
        status = _dual_auth.save_client(
            str(form.get("client_id") or ""),
            str(form.get("client_secret") or ""),
        )
        return redirect("/app?tab=authentication", message=f"Google Web OAuth app saved securely ({status.get('client_id_hint') or 'configured'}).")
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


@app.post(route("/actions/google-oauth/connect"))
async def dual_auth_google_connect(request: Request):
    user = current_user(request)
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    try:
        channel_id=int(str(form.get("channel_db_id") or "0"))
        studio_tenants.require("channel",channel_id,user)
        url = _dual_auth.begin(channel_id, user=user)
        return _DualAuthRedirectResponse(url=url, status_code=303)
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


@app.get(route("/oauth/google/callback"))
def dual_auth_google_callback(request: Request):
    user = current_user(request)
    try:
        google_error = str(request.query_params.get("error") or "").strip()
        if google_error:
            raise RuntimeError(f"Google authorization was not completed: {google_error}")
        result = _dual_auth.complete(
            state=str(request.query_params.get("state") or ""),
            query_string=str(request.url.query or ""),
            user=user,
        )
        label = str(result.get("channel_name") or result.get("channel_id") or "YouTube channel")
        suffix = " Web OAuth is active." if result.get("activated") else " Web OAuth is connected; activate it when ready."
        return redirect("/app?tab=authentication", message=label + suffix)
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


@app.post(route("/actions/auth-connections/test"))
async def dual_auth_test(request: Request):
    user = current_user(request)
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    try:
        channel_id=int(str(form.get("channel_db_id") or "0"))
        studio_tenants.require("channel",channel_id,user)
        result = _dual_auth.test_connection(
            channel_id, str(form.get("method") or ""),
        )
        return redirect("/app?tab=authentication", message=f"{result.get('channel_name') or 'Channel'}: {result.get('method')} connection is working.")
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


@app.post(route("/actions/auth-connections/activate"))
async def dual_auth_activate(request: Request):
    user = current_user(request)
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    try:
        channel_id=int(str(form.get("channel_db_id") or "0"))
        studio_tenants.require("channel",channel_id,user)
        result = _dual_auth.select_active(
            channel_id, str(form.get("method") or ""), verify=True,
        )
        return redirect("/app?tab=authentication", message=f"{result.get('channel_name') or 'Channel'} now uses {result.get('method')} authentication for new jobs.")
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))

