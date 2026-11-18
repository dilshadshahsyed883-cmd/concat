"""Verified-identity ASGI bridge to the copied Concat-only uploader.

This is deliberately NOT an HTTP reverse proxy. The reused app is invoked in
process only after the independent Concat Web identity resolver approves the
request. Client-supplied auth headers are removed and replaced with values
derived from the authenticated Principal. Request bodies (including videos and
OAuth JSON) stream unchanged; no network fetch or temporary identity bypass.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
from pathlib import Path
import sys
from urllib.parse import urlencode, parse_qsl, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import Response

from app.auth import Principal

LOG = logging.getLogger(__name__)
BASE = "/concat-uploader"
REUSED_ROOT = Path(__file__).resolve().parents[3] / "reused" / "uploader" / "part01"

# Make old unsafe/admin-only LUM endpoints unreachable even for Concat Web
# admins. Admin is a separate administration surface; uploads remain owner-only.
GET_EXACT = {"app"}
POST_EXACT = {
    "actions/add-video",
    "actions/oauth-client", "actions/oauth-start", "actions/oauth-complete",
    "actions/studio-tenant/channel-create", "actions/studio-tenant/group-create",
    "actions/sheets/save", "actions/sheets/delete",
    "actions/sheets/sync", "actions/sheets/sync-all",
    "actions/channels/status", "actions/channels/delete-manual",
    "actions/channels/rescan-tokens", "actions/channels/test-connections",
    "actions/copyright/credentials/upload",
    "actions/copyright/queue/pause", "actions/copyright/queue/start",
    "actions/copyright/queue/bulk", "actions/copyright/queue/remove",
}
POST_ID_PREFIXES = {
    "actions/retry/", "actions/cancel/", "actions/authorize-fresh/",
    "actions/channel-network/", "actions/copyright/metadata/",
}
FORGED_IDENTITY_HEADERS = {
    b"x-auth-user", b"x-app-access", b"x-is-admin",
    b"x-remote-user", b"x-forwarded-user", b"x-authenticated-user",
    b"x-scene-user", b"x-user", b"x-admin",
}
SAFE_QUERY_KEYS = {
    "source_id", "preview_concat", "concat_video_link", "concat_title",
    "message", "error", "embedded",
}
PAGE_TABS = {
    "sheets-sync": "sheets", "channels": "channels",
    "upload-engine": "add_video", "queue": "queue",
}


def _allowed(path: str, method: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or "%" in path:
        return False
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return False
    if method in {"GET", "HEAD"}:
        return (path in GET_EXACT or
                (path.startswith("static/") and len(path) > len("static/")))
    if method != "POST":
        return False
    if path in POST_EXACT:
        return True
    return any(path.startswith(prefix) and path[len(prefix):].isdigit() and
               int(path[len(prefix):]) > 0 for prefix in POST_ID_PREFIXES)


def _configure_from_private_environment() -> None:
    if os.environ.get("CONCAT_UPLOADER_ENABLED") != "1":
        raise HTTPException(503, "Concat Uploader is not configured")
    data = os.environ.get("LUM_DATA_DIR", "")
    secret = os.environ.get("LUM_SESSION_SECRET", "")
    cloud_root = os.environ.get("CONCAT_CLOUD_RENDER_ROOT", "")
    media_origin = os.environ.get("CONCAT_PUBLIC_ORIGIN", "")
    parts = urlsplit(media_origin)
    if (not data or not Path(data).is_absolute() or Path(data).is_symlink()
            or not cloud_root or not Path(cloud_root).is_absolute()
            or Path(cloud_root).is_symlink()
            or Path(data).resolve() == Path(cloud_root).resolve()
            or len(secret) < 32
            or parts.scheme != "https" or not parts.hostname or
            parts.path not in {"", "/"} or parts.query or parts.fragment or
            parts.username or parts.password):
        raise HTTPException(503, "Concat Uploader private configuration is incomplete")
    for key, required in (
        ("LUM_ENGINE_INSTANCE", "concat"),
        ("LUM_BASE_PATH", BASE),
        ("LUM_SUITE_AUTH", "1"),
        ("LUM_COOKIE_SECURE", "1"),
        ("CONCAT_WEB_IN_PROCESS", "1"),
    ):
        existing = os.environ.get(key)
        if existing is not None and existing != required:
            raise HTTPException(503, "Conflicting Concat Uploader isolation configuration")
        os.environ[key] = required


def _import_reused_app():
    if str(REUSED_ROOT) not in sys.path:
        # The source is copied into this repository. There are no runtime
        # imports from VPS_1 and no external service trusting auth headers.
        sys.path.insert(0, str(REUSED_ROOT))
    return importlib.import_module("lum_engine.standalone_app").app


async def backend_for(request: Request):
    state = request.app.state
    current = getattr(state, "concat_uploader_backend", None)
    if current is not None:
        return current
    lock = getattr(state, "concat_uploader_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        state.concat_uploader_lock = lock
    async with lock:
        current = getattr(state, "concat_uploader_backend", None)
        if current is not None:
            return current
        _configure_from_private_environment()
        try:
            current = _import_reused_app()
            await current.router.startup()
        except (ImportError, RuntimeError, OSError) as exc:
            LOG.exception("Concat Uploader backend initialization failed")
            raise HTTPException(503, "Concat Uploader dependencies or storage unavailable") from exc
        state.concat_uploader_backend = current
        return current


async def shutdown_backend(app) -> None:
    current = getattr(app.state, "concat_uploader_backend", None)
    if current is not None:
        app.state.concat_uploader_backend = None
        await current.router.shutdown()


class UploaderResponse(Response):
    """Streams the child ASGI response, after server-side principal validation."""

    def __init__(self, backend, principal: Principal, path: str, query: bytes):
        super().__init__(content=b"", status_code=200)
        self.backend = backend
        self.principal = principal
        self.path = path
        self.query = query

    async def __call__(self, scope, receive, send):
        trusted = tuple(
            (key, value) for key, value in scope.get("headers", ())
            if key.lower() not in FORGED_IDENTITY_HEADERS
            and not key.lower().startswith(b"x-forwarded-")
        )
        # Never trust the browser's X-Auth-User/X-App-Access/X-Is-Admin.
        trusted += (
            (b"x-auth-user", self.principal.username.encode("utf-8")),
            (b"x-app-access", b"concat-studio"),
            (b"x-is-admin", b"1" if self.principal.is_admin else b"0"),
        )
        child_scope = dict(scope)
        child_scope.update(
            path=self.path,
            raw_path=self.path.encode("ascii"),
            query_string=self.query,
            headers=trusted,
            root_path="",
        )
        await self.backend(child_scope, receive, send)


async def page_response(request: Request, principal: Principal, page: str) -> Response:
    if page not in PAGE_TABS:
        raise HTTPException(404, "Unknown uploader page")
    backend = await backend_for(request)
    args = [("tab", PAGE_TABS[page])]
    args.extend((key, value) for key, value in
                parse_qsl(request.url.query, keep_blank_values=False)
                if key in SAFE_QUERY_KEYS)
    return UploaderResponse(backend, principal, BASE + "/app",
                            urlencode(args).encode("utf-8"))


async def resource_response(request: Request, principal: Principal, path: str) -> Response:
    if not _allowed(path, request.method):
        raise HTTPException(404, "Not available in Concat Uploader")
    if path == "app":
        tab = request.query_params.get("tab", "sheets")
        if tab not in PAGE_TABS.values():
            raise HTTPException(404, "Unknown uploader tab")
    backend = await backend_for(request)
    return UploaderResponse(backend, principal, BASE + "/" + path,
                            request.scope.get("query_string", b""))
