"""Verified-owner adapters for the original VPS_1 Cloud Render implementation.

UI and API compatibility are kept while the separate Concat Web service stores
each owner's data in independent databases and directories.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.auth import Principal, current_principal
from app.contracts.navigation import CONCAT, UPLOADER
from .engine.cloud_render import CHUNK_MAX
from .tenant import get_registry

_BASE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_BASE / "templates"))
_CSRF = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")


@asynccontextmanager
async def _lifespan(app):
    registry = get_registry(app)
    registry.start_existing()
    try:
        yield
    finally:
        registry.close()


router = APIRouter(lifespan=_lifespan)


def _manager(request: Request, principal: Principal):
    # Verified current_principal is the *only* ownership source.
    return get_registry(request.app).for_principal(principal)


def _csrf(request: Request):
    actual = request.headers.get("X-Cloud-Render-CSRF", "")
    expected = request.cookies.get("concat_csrf", "")
    if not _CSRF.fullmatch(expected or "") or not hmac.compare_digest(actual, expected or ""):
        raise HTTPException(403, "Concat Studio CSRF token required")
    origin = request.app.state.settings.origin
    for header in ("origin", "referer"):
        supplied = request.headers.get(header)
        if supplied:
            parsed = urlsplit(supplied)
            test = parsed.scheme + "://" + parsed.netloc
            if test != origin:
                raise HTTPException(403, "Cross-origin request denied")


def _page(request: Request, principal: Principal, index: int):
    token = request.cookies.get("concat_csrf", "")
    if not _CSRF.fullmatch(token or ""):
        token = secrets.token_urlsafe(32)
    reply = _TEMPLATES.TemplateResponse(
        request=request, name="cloud_render.html",
        context={"principal": principal, "page": CONCAT[index],
                 "concat_pages": CONCAT, "uploader_pages": UPLOADER,
                 "csrf_token": token, "initial_tab": ("production", "media", "gpu")[index]},
        headers={"Cache-Control": "private,no-store", "X-Content-Type-Options": "nosniff"},
    )
    reply.set_cookie("concat_csrf", token, secure=True, httponly=True, samesite="strict", path="/")
    return reply


@router.get("/concat")
@router.get("/concat/")
@router.get("/cloud-render/")
async def index(_: Principal = Depends(current_principal)):
    return RedirectResponse("/concat/production", status_code=303)


@router.get("/concat/production")
async def production_page(request: Request, principal: Principal = Depends(current_principal)):
    return _page(request, principal, 0)


@router.get("/concat/media")
async def media_page(request: Request, principal: Principal = Depends(current_principal)):
    return _page(request, principal, 1)


@router.get("/concat/gpu-config")
async def gpu_page(request: Request, principal: Principal = Depends(current_principal)):
    return _page(request, principal, 2)


@router.get("/concat/assets/cloud_render.js")
async def script(_: Principal = Depends(current_principal)):
    return FileResponse(_BASE / "assets" / "cloud_render.js", media_type="application/javascript",
                        headers={"Cache-Control": "private,no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/concat/assets/cloud_render.css")
async def styles(_: Principal = Depends(current_principal)):
    return FileResponse(_BASE / "assets" / "cloud_render.css", media_type="text/css",
                        headers={"Cache-Control": "private,no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/v1/concat/gpu-config")
@router.get("/api/cloud-render/config")
async def read_config(request: Request, principal: Principal = Depends(current_principal)):
    return _manager(request, principal).config()


@router.post("/api/cloud-render/config")
async def save_config(request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return _manager(request, principal).save(await request.json())


@router.post("/api/cloud-render/token/rotate")
async def rotate_token(request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return _manager(request, principal).rotate_token()


@router.post("/api/cloud-render/gpu/test")
async def test_worker(request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return await run_in_threadpool(_manager(request, principal).probe)


@router.post("/api/cloud-render/gpu/connect")
async def connect_worker(request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return await run_in_threadpool(_manager(request, principal).probe, True)


@router.post("/api/cloud-render/gpu/disconnect")
async def disconnect_worker(request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return _manager(request, principal).disconnect()


@router.get("/api/v1/concat/production")
@router.get("/api/cloud-render/production")
async def read_production(request: Request, principal: Principal = Depends(current_principal)):
    return _manager(request, principal).production.snapshot()


@router.post("/api/cloud-render/production/scan")
async def scan_production(request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Drive link body must be an object")
    return await run_in_threadpool(_manager(request, principal).production.scan, body.get("link"))


@router.post("/api/cloud-render/production/queue/{action}")
async def control_queue(action: str, request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return _manager(request, principal).production.control(action)


@router.post("/api/cloud-render/production/jobs/{job}/{action}")
async def update_job(job: int, action: str, request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return _manager(request, principal).production.action(job, action)


@router.get("/api/v1/concat/media")
@router.get("/api/cloud-render/media")
async def read_media(request: Request, principal: Principal = Depends(current_principal)):
    return _manager(request, principal).listing()


@router.post("/api/cloud-render/media/{job}/rename")
async def rename(job: str, request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Rename body must be an object")
    return _manager(request, principal).rename(job, body.get("name", ""))


@router.post("/api/cloud-render/media/{job}/drive")
async def upload_to_drive(job: str, request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return _manager(request, principal).queue(job)


@router.delete("/api/cloud-render/media/{job}")
async def delete_media(job: str, request: Request, principal: Principal = Depends(current_principal)):
    _csrf(request)
    return _manager(request, principal).delete(job)


@router.get("/api/cloud-render/media/{job}/file")
async def media_file(job: str, request: Request, principal: Principal = Depends(current_principal)):
    owner = _manager(request, principal)
    local = owner.local(job)
    with owner.connection() as con:
        row = con.execute("SELECT name,state FROM files WHERE id=?", (job,)).fetchone()
    if not row or row["state"] not in ("ready", "failed", "queued") or not local.is_file() or local.is_symlink():
        raise HTTPException(404, "Local MP4 unavailable")
    return FileResponse(
        local, media_type="video/mp4", filename=row["name"],
        content_disposition_type=("attachment" if request.query_params.get("download") == "1" else "inline"),
        headers={"Cache-Control": "private,no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/api/cloud-render/ingest/{job}/chunk")
async def ingest(job: str, request: Request, index: int, name: str, size: int, sha256: str):
    # The Salad worker uses a rotating per-owner token; not a browser cookie.
    manager = get_registry(request.app).by_receiver_token(
        request.headers.get("X-Cloud-Render-Token", ""))
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > CHUNK_MAX:
            raise HTTPException(413, "Chunk too large")
        data.extend(chunk)
    return await run_in_threadpool(
        manager.receive, job, index, bytes(data), name=name, size=size, sha=sha256)
