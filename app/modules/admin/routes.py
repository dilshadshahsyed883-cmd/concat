"""Concat Web administration and verified Scene SSO endpoints.

Scene remains the authority for user accounts and roles; this app controls
only Concat grants, app configuration and its own revocable sessions.
"""
from pathlib import Path
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, StrictBool, conint

from app.auth import Principal, admin_principal, current_principal
from app.modules.admin.security import (
    COOKIE, STATE_COOKIE, _safe_return_path, get_security, require_csrf,
    SecurityUnavailable,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


@router.get("/auth/scene/start")
async def scene_login(request: Request, next: str = "/dashboard"):
    service = get_security(request)
    state = secrets.token_urlsafe(32)
    # The upstream issuer MUST require a real active Scene session and use
    # its own pinned callback allowlist, never arbitrary redirect_uri values.
    params = urlencode({
        "client_id": "concat-web",
        "state": state,
        "redirect_uri": service.web_origin + "/auth/scene/callback",
    })
    response = RedirectResponse(
        service.scene_origin + "/concat-sso/authorize?" + params,
        status_code=303, headers=PRIVATE,
    )
    response.set_cookie(STATE_COOKIE, state, max_age=180, secure=True,
                        httponly=True, samesite="lax", path="/")
    return response


@router.get("/auth/scene/callback")
async def scene_callback(request: Request, ticket: str = "", state: str = ""):
    service = get_security(request)
    cookie_state = request.cookies.get(STATE_COOKIE, "")
    if not cookie_state or len(cookie_state) < 32 or len(state) > 200 or not secrets.compare_digest(cookie_state, state):
        raise HTTPException(401, "Invalid Scene SSO state")
    try:
        username, sid_hash, jti_hash = service.validate_ticket(ticket, state)
        token, _, max_age = service.create_session(username, sid_hash, jti_hash)
    except SecurityUnavailable as exc:
        raise HTTPException(503, "Scene SSO session verification unavailable") from exc
    service.revoke_cookie(request)  # rotate any prior Concat browser session
    response = RedirectResponse("/dashboard", status_code=303, headers=PRIVATE)
    response.delete_cookie(STATE_COOKIE, path="/", secure=True, httponly=True)
    response.set_cookie(COOKIE, token, max_age=max_age, secure=True,
                        httponly=True, samesite="lax", path="/")
    return response


@router.get("/api/v1/auth/csrf")
async def csrf_token(request: Request, _: Principal = Depends(current_principal)):
    csrf = get_security(request).csrf_token(request)
    if not csrf:
        raise HTTPException(401, "Verified browser session required")
    return JSONResponse({"csrf_token": csrf}, headers=PRIVATE)


@router.post("/auth/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request, _: Principal = Depends(current_principal)):
    get_security(request).revoke_cookie(request)
    response = RedirectResponse("/auth/scene/start", status_code=303, headers=PRIVATE)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True)
    return response


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, principal: Principal = Depends(admin_principal)):
    return templates.TemplateResponse(
        request=request, name="admin.html",
        context={"principal": principal}, headers=PRIVATE,
    )


@router.get("/api/v1/admin/users")
async def users(request: Request, _: Principal = Depends(admin_principal)):
    return JSONResponse({"users": get_security(request).users()}, headers=PRIVATE)


class GrantBody(BaseModel):
    concat_granted: StrictBool

    model_config = {"extra": "forbid"}


@router.post("/api/v1/admin/users/{username}/access", dependencies=[Depends(require_csrf)])
async def user_access(username: str, body: GrantBody, request: Request,
                      principal: Principal = Depends(admin_principal)):
    service = get_security(request)
    service.grant(principal.username, username, body.concat_granted)
    return JSONResponse({"ok": True}, headers=PRIVATE)


@router.post("/api/v1/admin/users/{username}/revoke-sessions", dependencies=[Depends(require_csrf)])
async def revoke_user_sessions(username: str, request: Request,
                               principal: Principal = Depends(admin_principal)):
    count = get_security(request).revoke_user(principal.username, username)
    return JSONResponse({"ok": True, "revoked": count}, headers=PRIVATE)


@router.get("/api/v1/admin/config")
async def config(request: Request, _: Principal = Depends(admin_principal)):
    service = get_security(request)
    return JSONResponse({"config": service._config(), "scene_origin": service.scene_origin,
                         "scene_identity": "read-only", "secrets_exposed": False}, headers=PRIVATE)


class ConfigBody(BaseModel):
    maintenance_mode: StrictBool | None = None
    session_max_minutes: conint(strict=True, ge=15, le=480) | None = None

    model_config = {"extra": "forbid"}


@router.post("/api/v1/admin/config", dependencies=[Depends(require_csrf)])
async def save_config(body: ConfigBody, request: Request, principal: Principal = Depends(admin_principal)):
    values = body.model_dump(exclude_unset=True)
    return JSONResponse({"config": get_security(request).update_config(principal.username, values)}, headers=PRIVATE)


@router.get("/api/v1/admin/audit")
async def audit(request: Request, _: Principal = Depends(admin_principal)):
    return JSONResponse({"events": get_security(request).audit()}, headers=PRIVATE)
