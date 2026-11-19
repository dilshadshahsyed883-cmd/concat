"""Scene Builder-style UX parity contracts for standalone Concat Web.

The native Scene shell is a design reference, NOT an authentication or CSS
runtime dependency. Pages still require the independent Concat session.
"""
from pathlib import Path
import re
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
                # Prevent stale per-module sidebars from passing the
                # three-nav-group count without actually removing sublinks.
                sidebar = content.split('<nav id="sidebar"', 1)[1].split('</nav>', 1)[0]
                self.assertNotIn('nav-subtitle', sidebar)
                self.assertNotIn('nav-link', sidebar)
                self.assertIn('id="nav-logout"', content)
                self.assertIn('id="nav-logout-feedback"', content)
                self.assertIn('href="/admin"', content)
                self.assertIn('three-tab-1', content)
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

    def test_only_three_primary_links_and_studio_modules_inside_content(self):
        app = create_app()
        app.state.identity_resolver = lambda _: Principal(
            "alice", is_admin=True, is_active=True, concat_granted=True
        )
        expected = ("/dashboard", "/concat/production", "/admin")
        with TestClient(app) as client:
            for path in (
                "/dashboard", "/concat/production", "/concat/media",
                "/concat/gpu-config", "/concat-uploader/sheets-sync",
                "/concat-uploader/channels", "/concat-uploader/upload-engine",
                "/concat-uploader/queue", "/admin",
            ):
                with self.subTest(path=path):
                    res = client.get(path)
                    self.assertEqual(res.status_code, 200, res.text[:250])
                    sidebar = res.text.split('<nav id="sidebar"', 1)[1].split('</nav>', 1)[0]
                    tabs = re.findall(r'<a class="nav-top[^"]*"[^>]*href="([^"]+)"', sidebar)
                    self.assertEqual(tuple(tabs), expected)
                    self.assertEqual(sidebar.count('class="nav-group"'), 3)
                    self.assertNotIn('class="nav-link"', sidebar)
                    self.assertNotIn('class="nav-subtitle"', sidebar)
                    self.assertNotIn('href="/concat/media"', sidebar)
                    self.assertNotIn('href="/concat-uploader/channels"', sidebar)
                    if path.startswith(("/concat/", "/concat-uploader/")):
                        self.assertIn('href="/concat/production" aria-current="page">Concat Studio', sidebar)

    def test_studio_has_four_modules_and_live_uploader_keeps_one_internal_tab_rail(self):
        app = create_app()
        app.state.identity_resolver = lambda _: Principal(
            "alice", is_admin=True, is_active=True, concat_granted=True
        )
        with TestClient(app) as client:
            for slug, tab in (
                ("production", "production"), ("media", "media"),
                ("gpu-config", "gpu"),
            ):
                with self.subTest(slug=slug):
                    page = client.get("/concat/" + slug).text
                    self.assertIn('class="cr-tabs studio-tabs"', page)
                    self.assertEqual(page.count('data-tab="'), 3)
                    self.assertIn('data-initial-tab="' + tab + '"', page)
                    self.assertIn('href="/concat-uploader/sheets-sync">Uploader</a>', page)

            for slug, child in (
                ("sheets-sync", "sheets"), ("channels", "channels"),
                ("upload-engine", "add_video"), ("queue", "queue"),
            ):
                with self.subTest(slug=slug):
                    page = client.get("/concat-uploader/" + slug).text
                    self.assertIn('class="studio-tabs"', page)
                    for href in ("/concat/production", "/concat/media",
                                 "/concat/gpu-config", "/concat-uploader/sheets-sync"):
                        self.assertIn('href="' + href + '"', page)
                    # Without CONCAT_UPLOADER_ENABLED, the fail-closed fallback
                    # renders the nested links itself instead of a live iframe.
                    self.assertIn('class="uploader-tabs"', page)
                    self.assertIn('href="/concat-uploader/' + slug +
                                  '" class="active" aria-current="page"', page)
                    self.assertNotIn('class="uploader-frame"', page)

        # When enabled, the real Uploader template embeds the working app.
        # Its one native tab rail remains inside the iframe (not duplicated).
        live = (ROOT / "app/modules/uploader/templates/workspace.html").read_text()
        self.assertIn('src="/concat-uploader/app?tab={{ tab | urlencode }}"', live)
        self.assertNotIn('class="uploader-tabs"', live)

        copied = (ROOT / "reused/uploader/part01/templates/app.html").read_text()
        self.assertIn("engine_instance == 'concat'", copied)
        for title in ("Sheets Sync", "Channels", "Upload Engine", "Queue"):
            self.assertIn(">" + title + "</a>", copied)

    def test_navigation_keeps_auth_denials(self):
        app = create_app()
        with TestClient(app) as client:
            for path in ("/dashboard", "/concat/production", "/concat/media",
                         "/concat/gpu-config", "/concat-uploader/channels", "/admin"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path, headers={"X-Auth-User": "admin"}).status_code, 401)

    def test_login_design_changes_do_not_remove_security_nonce(self):
        page = (ROOT / "app/modules/admin/templates/login.html").read_text()
        self.assertIn('name="csrf" value="{{ csrf }}"', page)
        self.assertIn('action="/auth/login" method="post"', page)
        self.assertIn('autocomplete="current-password"', page)
        self.assertIn("color-scheme:light", page)
        self.assertNotIn("CONCAT_ADMIN_PASSWORD_HASH", page)


if __name__ == "__main__":
    unittest.main()
