"""Agent 4: read-only, temporary-fixture security tests for the pre-isolation baseline.

These are NOT evidence that Concat Studio multiuser access is safe. They protect
the existing admin-only gate and the reusable Copyright tenant boundary while
the production owner-scoping implementation is pending. No VPS, real tokens,
external Drive, Salad, or YouTube services are contacted.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps" / "scene-builder" / "part01"
COPYRIGHT = ROOT / "apps" / "copyright-uploader" / "part01" / "lum_engine"
sys.path.insert(0, str(SCENE))
from cloud_render import register_cloud_render

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 81


class CloudRenderInterimGateTests(unittest.TestCase):
    """Do not remove admin gates until real two-owner authorization passes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = FastAPI()

        def require_login(request: Request):
            if not request.session.get("authenticated"):
                raise HTTPException(401, "Login required")

        def require_admin(request: Request):
            if not request.session.get("is_admin"):
                raise HTTPException(403, "Administrator access required")

        self.manager = register_cloud_render(
            app, Jinja2Templates(directory=str(SCENE / "templates")),
            self.tmp.name, require_admin, require_login,
        )
        self.addCleanup(self.manager.pool.shutdown, wait=True)

        @app.get("/_audit/session/{user}")
        def set_session(request: Request, user: str):
            request.session.clear()
            if user in ("alice", "bob", "admin"):
                request.session.update({
                    "authenticated": True, "username": user,
                    "is_admin": user == "admin",
                })
            return {"user": user}

        app.add_middleware(SessionMiddleware, secret_key="temporary-audit-fixture")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

        for job in ("alice-media", "bob-media", "legacy-media"):
            self.manager.receive(job, 0, MP4, name=job + ".mp4",
                                 size=len(MP4),
                                 sha=hashlib.sha256(MP4).hexdigest())

    def test_two_non_admin_sessions_cannot_guess_render_media_or_config(self):
        urls = [
            "/cloud-render/",
            "/api/cloud-render/production",
            "/api/cloud-render/media",
            "/api/cloud-render/config",
            "/api/cloud-render/media/alice-media/file",
            "/api/cloud-render/media/bob-media/file",
            "/api/cloud-render/media/legacy-media/file?download=1",
        ]
        for username in ("alice", "bob"):
            self.client.get("/_audit/session/" + username)
            for path in urls:
                with self.subTest(username=username, path=path):
                    response = self.client.get(path, headers={
                        "X-Auth-User": "admin", "X-Is-Admin": "1",
                    })
                    self.assertEqual(response.status_code, 403)

    def test_current_admin_sees_global_media_this_is_not_multiuser_pass(self):
        self.client.get("/_audit/session/admin")
        response = self.client.get("/api/cloud-render/media")
        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.json()["items"]}
        self.assertEqual(ids, {"alice-media", "bob-media", "legacy-media"})
        # An admin can see every row TODAY: release blocker, not desired behavior.

    def test_direct_ingest_requires_receiver_token_without_browser_session(self):
        self.client.get("/_audit/session/guest")
        response = self.client.post(
            "/api/cloud-render/ingest/bob-media/chunk",
            params={"index": 0, "name": "bob.mp4", "size": len(MP4),
                    "sha256": hashlib.sha256(MP4).hexdigest()},
            content=b"ignored",
            headers={"X-Auth-User": "admin"},
        )
        self.assertEqual(response.status_code, 401)

    def test_browser_writes_require_admin_and_csrf(self):
        for user in ("alice", "bob", "admin"):
            self.client.get("/_audit/session/" + user)
            for path in ("/api/cloud-render/production/jobs/999/cancel",
                         "/api/cloud-render/production/queue/start",
                         "/api/cloud-render/media/bob-media/rename"):
                with self.subTest(user=user, path=path):
                    response = self.client.post(path, json={"name": "ignored.mp4"})
                    self.assertEqual(response.status_code, 403)
        # All requests above lacked CSRF and cannot change any queue or file.

    def test_source_guard_cannot_be_dropped_without_backend_owner_fields(self):
        cloud = (SCENE / "cloud_render.py").read_text(encoding="utf-8")
        production = (SCENE / "cloud_render_production.py").read_text(encoding="utf-8")
        app = (SCENE / "app.py").read_text(encoding="utf-8")
        if "owner_username" not in cloud or "owner_username" not in production:
            self.assertIn('require_admin(req)', cloud)
            self.assertIn('request.url.path.startswith("/api/cloud-render/")', app)
            self.assertIn('not account.get("is_admin")', app)


class CopyrightOwnerIsolationReferenceTests(unittest.TestCase):
    """Protect the strict Copyright behavior that Concat must reuse explicitly."""

    def test_two_users_admin_legacy_and_immutable_channel_sheet_bindings(self):
        spec = importlib.util.spec_from_file_location(
            "concat_audit_tenants", COPYRIGHT / "studio_tenants.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            class Repo:
                def connect(self):
                    return sqlite3.connect(str(Path(tmp) / "tenant.sqlite3"))

            tenants = mod.StudioTenants(Repo())
            with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "copyright"}):
                for kind in ("channel", "sheet", "group", "profile"):
                    tenants.bind(kind, 11, "Alice")
                    tenants.bind(kind, 22, "Bob")
                alice = {"username": "alice", "is_admin": 0}
                bob = {"username": "bob", "is_admin": 0}
                admin = {"username": "admin", "is_admin": 1}

                for kind in ("channel", "sheet", "group", "profile"):
                    self.assertEqual(tenants.allowed_ids(kind, alice), {11})
                    self.assertEqual(tenants.allowed_ids(kind, bob), {22})
                    self.assertEqual(tenants.allowed_ids(kind, admin), set())
                    tenants.require(kind, 11, alice)
                    tenants.require(kind, 22, bob)
                    for actor, forbidden_id in ((alice, 22), (bob, 11),
                                                (admin, 11), (admin, 22),
                                                (admin, 99)):
                        with self.subTest(kind=kind, actor=actor["username"],
                                          forbidden=forbidden_id):
                            with self.assertRaises(PermissionError):
                                tenants.require(kind, forbidden_id, actor)
                    self.assertEqual(tenants.owner(kind, 99), "")
                    with self.assertRaises(PermissionError):
                        tenants.bind(kind, 11, "bob")

                for actor in (alice, bob, admin):
                    with self.assertRaises(PermissionError):
                        tenants.require("channel", 99, actor)
                with self.assertRaises(PermissionError):
                    tenants.allowed_ids("sheet", {"username": ""})


if __name__ == "__main__":
    unittest.main()
