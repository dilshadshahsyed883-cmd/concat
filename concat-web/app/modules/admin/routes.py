"""Standalone Concat Web login, revocable sessions and one-admin controls.

No route to Scene authentication, issuer tickets, or user provisioning exists.
"""
from pathlib import Path
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, StrictBool

from app.auth import Principal, admin_principal, current_principal
from app.modules.admin.security import (
    COOKIE, LOGIN_CSRF_COOKIE, get_security, require_csrf,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
           "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    security = get_security(request)  # fail closed until configured
    if security.authenticate(request):
        return RedirectResponse("/dashboard", status_code=303, headers=PRIVATE)
    nonce = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request=request, name="login.html", context={"csrf": nonce}, headers=PRIVATE
    )
    response.set_cookie(LOGIN_CSRF_COOKIE, nonce, path="/", max_age=600,
                        secure=True, httponly=True, samesite="strict")
    return response


@router.post("/auth/login")
async def sign_in(request: Request):
    security = get_security(request)
    if int(request.headers.get("content-length", "0") or 0) > 4096:
        raise HTTPException(413, "Login form too large")
    origin = request.headers.get("origin", "").rstrip("/")
    referer = request.headers.get("referer", "")
    if not origin and referer:
        from urllib.parse import urlsplit
        parsed = urlsplit(referer)
        origin = parsed.scheme + "://" + parsed.netloc
    if not origin or not hmac.compare_digest(origin, security.web_origin):
        raise HTTPException(403, "Login origin invalid")
    form = await request.form()
    nonce = str(form.get("csrf", ""))
    expected = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if (not expected or len(expected) < 32 or len(nonce) > 200 or
        not hmac.compare_digest(expected, nonce)):
        raise HTTPException(403, "Login CSRF invalid")
    security.revoke_cookie(request)  # rotate on every new login
    token, max_age = security.login(str(form.get("username", "")),
                                    str(form.get("password", "")), request)
    response = RedirectResponse("/dashboard", status_code=303, headers=PRIVATE)
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/", secure=True, httponly=True)
    response.set_cookie(COOKIE, token, max_age=max_age, secure=True, httponly=True,
                        samesite="lax", path="/")
    return response


@router.get("/api/v1/auth/csrf")
async def csrf_token(request: Request, _: Principal = Depends(current_principal)):
    csrf = get_security(request).csrf_token(request)
    if not csrf:
        raise HTTPException(401, "Authenticated local session required")
    return JSONResponse({"csrf_token": csrf}, headers=PRIVATE)


@router.post("/auth/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request, _: Principal = Depends(current_principal)):
    get_security(request).revoke_cookie(request)
    response = RedirectResponse("/auth/login", status_code=303, headers=PRIVATE)
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


@router.post("/api/v1/admin/sessions/revoke-all", dependencies=[Depends(require_csrf)])
async def revoke_all(request: Request, _: Principal = Depends(admin_principal)):
    return JSONResponse({"ok": True, "revoked": get_security(request).revoke_all()},
                        headers=PRIVATE)


@router.get("/api/v1/admin/config")
async def config(request: Request, _: Principal = Depends(admin_principal)):
    return JSONResponse({"config": get_security(request)._config(),
                         "identity": "standalone-local-admin",
                         "secrets_exposed": False}, headers=PRIVATE)


class ConfigBody(BaseModel):
    maintenance_mode: StrictBool | None = None
    model_config = {"extra": "forbid"}


@router.post("/api/v1/admin/config", dependencies=[Depends(require_csrf)])
async def save_config(body: ConfigBody, request: Request,
                      principal: Principal = Depends(admin_principal)):
    values = body.model_dump(exclude_unset=True)
    return JSONResponse(
        {"config": get_security(request).update_config(principal.username, values)},
        headers=PRIVATE,
    )


@router.get("/api/v1/admin/audit")
async def audit(request: Request, _: Principal = Depends(admin_principal)):
    return JSONResponse({"events": get_security(request).audit()}, headers=PRIVATE)
