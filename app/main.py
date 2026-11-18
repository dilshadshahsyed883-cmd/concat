"""Independent ASGI entrypoint. No source imports from the VPS_1 repository."""
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from app.auth import Principal, current_principal
from app.config import load_settings
from app.modules.dashboard.routes import router as dashboard_router
from app.modules.dashboard.providers import ConcatDashboardProvider, UploaderDashboardProvider
from app.modules.concat.routes import router as concat_router
from app.modules.uploader.routes import router as uploader_router
from app.modules.admin.routes import router as admin_router

def create_app():
    settings = load_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.identity_resolver = None  # Production resolves only revocable Scene SSO; tests may inject fixtures.
    app.state.concat_dashboard_provider = ConcatDashboardProvider(app)
    app.state.uploader_dashboard_provider = UploaderDashboardProvider(app)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(dashboard_router)
    app.include_router(concat_router)
    app.include_router(uploader_router)
    app.include_router(admin_router)

    @app.get("/healthz")
    async def health():
        return {"ok": True, "service": "concat-web"}

    @app.get("/", include_in_schema=False)
    async def home(_: Principal = Depends(current_principal)):
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/api/v1/session")
    async def session(principal: Principal = Depends(current_principal)):
        return {"username": principal.username, "is_admin": principal.is_admin, "concat_granted": True}

    return app

app = create_app()
