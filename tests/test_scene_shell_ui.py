"""Scene Builder-style UX parity contracts for standalone Concat Web.

The native Scene shell is a design reference, NOT an authentication or CSS
runtime dependency. Pages still require the independent Concat session.
"""
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from app.auth import Principal
from app.main import create_app


ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "app/static/app.css"
APP_JS = ROOT / "app/static/app.js"

TEMPLATES = (
    "app/templates/page.html",
    "app/modules/concat/templates/base.html",
    "app/modules/dashboard/templates/dashboard.html",
    "app/modules/uploader/templates/workspace.html",
    "app/modules/admin/templates/admin.html",
)


class SceneShellUXTests(unittest.TestCase):
    def test_canonical_vps1_visual_tokens_and_readable_workspace(self):
        css = APP_CSS.read_text()
        for token in (
            "color-scheme: light",
            "--sb-sidebar-width: 214px",
            "--sb-sidebar: #101526",
            "--sb-bg: #f8fafc",
            "--sb-card: #ffffff",
            "--sb-accent: #4f46e5",
            ".dashboard-page .content",
            ".visually-hidden",
            "prefers-reduced-motion",
            "max-width: 900px",
        ):
            with self.subTest(token=token):
                self.assertIn(token, css)
        cloud = (ROOT / "app/modules/concat/templates/cloud_render.html").read_text()
        self.assertIn('class="visually-hidden">Concat Studio workspace', cloud)
        self.assertIn('id="cr-health"', cloud)
        self.assertIn('data-tab="gpu"', cloud)
        self.assertIn('id="cr-save-gpu"', cloud)

    def test_all_authenticated_shells_share_brand_nav_and_mobile_drawer(self):
        for path in TEMPLATES:
            with self.subTest(path=path):
                content = (ROOT / path).read_text()
                self.assertEqual(content.count('class="nav-group"'), 3)
                self.assertIn('class="brand" href="/dashboard"', content)
                self.assertIn('id="nav-toggle"', content)
                self.assertIn('id="nav-backdrop"', content)
                self.assertIn('id="sidebar"', content)
                self.assertIn('id="nav-logout"', content)
                self.assertIn('id="nav-logout-feedback"', content)
                self.assertIn('href="/admin"', content)
                self.assertIn('scene-shell-1', content)
                self.assertNotIn("https://app.scenebuilder.online", content)

    def test_sidebar_logout_uses_real_csrf_origin_and_revocable_session(self):
        js = APP_JS.read_text()
        for marker in (
            'getElementById("nav-logout")',
            'getElementById("nav-backdrop")',
            '"/api/v1/auth/csrf"',
            '"/auth/logout"',
            '"X-CSRF-Token": payload.csrf_token',
            '"Origin": window.location.origin',
            'credentials: "same-origin"',
            'event.key === "Escape"',
            'aria-expanded',
            'matchMedia("(max-width: 900px)")',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, js)
        self.assertNotIn("X-Auth-User", js)
        self.assertNotIn("localStorage", js)
        self.assertNotIn("scene_sid", js)

    def test_private_pages_render_across_all_sections_with_active_links(self):
        app = create_app()
        app.state.identity_resolver = lambda _: Principal(
            "alice", is_admin=True, is_active=True, concat_granted=True
        )
        with TestClient(app) as client:
            for path in (
                "/dashboard", "/concat/production", "/concat/media",
                "/concat/gpu-config", "/concat-uploader/sheets-sync",
                "/concat-uploader/channels", "/concat-uploader/upload-engine",
                "/concat-uploader/queue", "/admin",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, response.text[:160])
                    self.assertIn('aria-current="page"', response.text)
                    self.assertIn('id="nav-backdrop"', response.text)
                    self.assertIn('id="nav-logout"', response.text)

    def test_login_design_changes_do_not_remove_security_nonce(self):
        page = (ROOT / "app/modules/admin/templates/login.html").read_text()
        self.assertIn('name="csrf" value="{{ csrf }}"', page)
        self.assertIn('action="/auth/login" method="post"', page)
        self.assertIn('autocomplete="current-password"', page)
        self.assertIn("color-scheme:light", page)
        self.assertNotIn("CONCAT_ADMIN_PASSWORD_HASH", page)


if __name__ == "__main__":
    unittest.main()
