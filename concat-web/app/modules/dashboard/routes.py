"""Authenticated Dashboard UI and read-only live metrics endpoint."""
from pathlib import Path
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.auth import Principal, current_principal
from app.contracts.navigation import DASHBOARD, CONCAT, UPLOADER
from .services import InvalidSnapshot, SourceUnavailable, get_overview
from .system_metrics import system_dashboard_snapshot

router = APIRouter()
_root = Path(__file__).resolve().parent
_templates = Jinja2Templates(directory=str(_root / "templates"))
_logger = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


@router.get("/dashboard")
async def dashboard(request: Request, principal: Principal = Depends(current_principal)):
    return _templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={"principal": principal, "page": DASHBOARD[0],
                 "concat_pages": CONCAT, "uploader_pages": UPLOADER},
        headers=_NO_STORE,
    )



@router.get("/api/v1/dashboard/system")
async def system_dashboard(_principal: Principal = Depends(current_principal)):
    """Protected host resource snapshot. Never writes or calls upstream Scene APIs."""
    try:
        payload = await run_in_threadpool(system_dashboard_snapshot)
    except Exception:
        _logger.exception("Read-only VPS system monitoring failed")
        raise HTTPException(status_code=503, detail="System monitoring unavailable", headers=_NO_STORE) from None
    return JSONResponse(payload, headers=_NO_STORE)


@router.get("/api/v1/dashboard/overview")
async def overview(request: Request,
                   limit: int = Query(default=8, ge=1, le=25),
                   principal: Principal = Depends(current_principal)):
    try:
        data = await get_overview(request.app, principal.username, limit=limit)
    except SourceUnavailable:
        raise HTTPException(status_code=503, detail="Live dashboard sources unavailable", headers=_NO_STORE)
    except InvalidSnapshot:
        _logger.error("Dashboard producer returned invalid or cross-owner data")
        raise HTTPException(status_code=502, detail="Live dashboard data failed validation", headers=_NO_STORE)
    return JSONResponse(data, headers=_NO_STORE)


@router.get("/dashboard/assets/dashboard.css", include_in_schema=False)
async def dashboard_css():
    return FileResponse(_root / "static" / "dashboard.css", media_type="text/css",
                        headers={"X-Content-Type-Options": "nosniff"})


@router.get("/dashboard/assets/dashboard.js", include_in_schema=False)
async def dashboard_js():
    return FileResponse(_root / "static" / "dashboard.js",
                        media_type="application/javascript",
                        headers={"X-Content-Type-Options": "nosniff"})
