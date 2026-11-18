"""One shared, tested response template; agents replace their route bodies only."""
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from app.auth import Principal
from app.contracts.navigation import Page, CONCAT, UPLOADER

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

def render_page(request: Request, principal: Principal, page: Page) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="page.html",
        context={"principal": principal, "page": page, "concat_pages": CONCAT, "uploader_pages": UPLOADER},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
