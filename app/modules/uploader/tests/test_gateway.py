"""Focused no-network checks of the verified Concat Uploader ASGI gateway."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth import Principal
from app.main import create_app
from app.modules.uploader import gateway


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "CONCAT_UPLOADER_ENABLED": "1",
            "LUM_DATA_DIR": str(self.tmp / "uploader-data"),
            "LUM_SESSION_SECRET": "test-secret-only-for-unit-tests-1234567890",
            "CONCAT_CLOUD_RENDER_ROOT": str(self.tmp / "cloud"),
            "CONCAT_PUBLIC_ORIGIN": "https://app.scenebuilder.online",
            "LUM_ENGINE_INSTANCE": "concat",
            "LUM_BASE_PATH": "/concat-uploader",
            "LUM_SUITE_AUTH": "1",
            "LUM_COOKIE_SECURE": "1",
            "CONCAT_WEB_IN_PROCESS": "1",
        })
        self.env.start()
        self.fake = FastAPI()

        @self.fake.api_route("/concat-uploader/app", methods=["GET"])
        async def app_page(request: Request):
            return {"path": request.url.path,
                    "query": dict(request.query_params),
                    "principal": request.headers.get("x-auth-user"),
                    "grant": request.headers.get("x-app-access"),
                    "admin": request.headers.get("x-is-admin")}

        @self.fake.post("/concat-uploader/actions/add-video")
        async def add_video(request: Request):
            return {"principal": request.headers.get("x-auth-user"),
                    "payload": (await request.body()).decode("utf-8")}

        @self.fake.get("/concat-uploader/static/test.css")
        async def static(request: Request):
            return {"principal": request.headers.get("x-auth-user")}

        self.importer = patch.object(gateway, "_import_reused_app", return_value=self.fake)
        self.importer.start()
        self.app = create_app()
        self.app.state.identity_resolver = lambda _: Principal(
            "alice", is_active=True, concat_granted=True)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.importer.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_four_pages_delegate_to_actual_backend_tabs(self):
        for path, expected_tab in (
            ("sheets-sync", "sheets"), ("channels", "channels"),
            ("upload-engine", "add_video"), ("queue", "queue"),
        ):
            with self.subTest(path=path):
                response = self.client.get("/concat-uploader/" + path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('class="uploader-frame"', response.text)
                self.assertIn('/concat-uploader/app?tab=' + expected_tab, response.text)
                self.assertEqual(response.text.count('class="nav-group"'), 3)
                result = self.client.get("/concat-uploader/app?tab=" + expected_tab).json()
                self.assertEqual(result["query"]["tab"], expected_tab)
                self.assertEqual(result["path"], "/concat-uploader/app")
                self.assertEqual(result["principal"], "alice")
                self.assertEqual(result["grant"], "concat-studio")
        self.assertEqual(self.client.get("/concat-uploader/").status_code, 200)

    def test_untrusted_headers_never_set_identity_or_admin(self):
        response = self.client.get("/concat-uploader/app?tab=queue", headers={
            "X-Auth-User": "other-owner", "X-Is-Admin": "1",
            "X-App-Access": "all", "X-Forwarded-User": "evil",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["principal"], "alice")
        self.assertEqual(response.json()["admin"], "0")
        self.assertEqual(self.client.post("/concat-uploader/actions/add-video",
                                           data="trusted-body",
                                           headers={"X-Auth-User": "bob"}).json(),
                         {"principal": "alice", "payload": "trusted-body"})

    def test_fail_closed_revocation_and_no_legacy_admin_endpoints(self):
        self.app.state.identity_resolver = None
        response = self.client.get("/concat-uploader/app",
                                   headers={"X-Auth-User": "alice"})
        self.assertEqual(response.status_code, 401)
        self.app.state.identity_resolver = lambda _: Principal(
            "alice", is_active=True, concat_granted=False)
        self.assertEqual(self.client.get("/concat-uploader/queue").status_code, 403)
        self.app.state.identity_resolver = lambda _: Principal(
            "alice", is_active=True, concat_granted=True, is_admin=True)
        for path in ("users", "login", "setup", "actions/settings",
                     "actions/auto-v6/engine", "actions/google-oauth/configure",
                     "actions/studio-tenant/grant"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post("/concat-uploader/" + path).status_code, 404)
        self.assertEqual(self.client.get("/concat-uploader/static/test.css").json()["principal"],
                         "alice")

    def test_backend_requires_private_persistent_configuration(self):
        self.app.state.concat_uploader_backend = None
        os.environ.pop("LUM_SESSION_SECRET")
        response = self.client.get("/concat-uploader/queue")
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.tmp / "uploader-data").exists())

    def test_child_owns_no_other_service_and_restricts_methods(self):
        self.assertFalse(gateway._allowed("actions/copyright/profile", "POST"))
        self.assertFalse(gateway._allowed("actions/copyright/metadata/-2", "POST"))
        self.assertFalse(gateway._allowed("users/1/update", "POST"))
        self.assertFalse(gateway._allowed("actions/google-oauth/connect", "POST"))
        self.assertFalse(gateway._allowed("oauth/google/callback", "GET"))
        self.assertFalse(gateway._allowed("static/../setup", "GET"))
        self.assertFalse(gateway._allowed("static/%2e%2e/setup", "GET"))
        self.assertFalse(gateway._allowed("api/copyright/folder-videos", "GET"))
        self.assertTrue(gateway._allowed("actions/sheets/sync-all", "POST"))
        self.assertTrue(gateway._allowed("actions/copyright/queue/start", "POST"))


if __name__ == "__main__":
    unittest.main()
