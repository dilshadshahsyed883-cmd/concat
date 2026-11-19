"""Standalone Concat admin security tests. Disposable state, no VPS/Scene IO."""
from contextlib import ExitStack
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.auth import Principal, require_owned_resource
from app.main import create_app
from app.modules.admin.security import (
    COOKIE, LOGIN_CSRF_COOKIE, SecurityService, ensure_owner, owned_records,
    password_hash,
)

ORIGIN = "https://concat.scenebuilder.online"
PASSWORD = "Correct-Horse-Staple-Admin-Password-2026"


class LocalAdminSecurityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.db = self.root / "security.sqlite3"
        self.encoded = password_hash(PASSWORD)
        self.environment = {
            "CONCAT_AUTH_MODE": "local-admin",
            "CONCAT_WEB_PUBLIC_ORIGIN": ORIGIN,
            "CONCAT_WEB_SECURITY_DB": str(self.db),
            "CONCAT_WEB_DATA_ROOT": str(self.root),
            "CONCAT_ADMIN_USERNAME": "owner",
            "CONCAT_ADMIN_PASSWORD_HASH": self.encoded,
            "CONCAT_ADMIN_SESSION_SECRET": "local-admin-private-secret-length-over-32-TEST",
            "LUM_SESSION_SECRET": "different-uploader-private-secret-over-32-TEST",
            "CONCAT_UPLOADER_ENABLED": "0",
        }
        for unused in ("CONCAT_SCENE_SSO_SECRET", "CONCAT_SCENE_USERS_DB",
                       "CONCAT_SCENE_PUBLIC_ORIGIN"):
            self.environment[unused] = ""
        self.stack.enter_context(patch.dict(os.environ, self.environment))
        self.app = create_app()
        self.client = TestClient(self.app, base_url=ORIGIN, follow_redirects=False)
        self.addCleanup(self.client.close)

    def _form(self):
        response = self.client.get("/auth/login")
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertIn("Secure", response.headers.get("set-cookie", ""))
        self.assertTrue(self.client.cookies.get(LOGIN_CSRF_COOKIE))
        match = re.search(r'name="csrf" value="([^"]+)"', response.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def _login(self, username="owner", password=PASSWORD, *, csrf=None, origin=ORIGIN):
        if csrf is None:
            csrf = self._form()
        return self.client.post("/auth/login",
                                data={"username": username, "password": password, "csrf": csrf},
                                headers={"Origin": origin}, follow_redirects=False)

    def _signed_in(self):
        response = self._login()
        self.assertEqual(response.status_code, 303, response.text[:300])
        self.assertEqual(response.headers["location"], "/dashboard")
        cookie = response.headers["set-cookie"]
        for required in ("Secure", "HttpOnly", "SameSite=lax"):
            self.assertIn(required.lower(), cookie.lower())
        self.assertTrue(self.client.cookies.get(COOKIE))
        return self.client.get("/api/v1/auth/csrf").json()["csrf_token"]

    def _post(self, path, csrf, data=None, origin=ORIGIN):
        return self.client.post(path, json=data,
                                headers={"X-CSRF-Token": csrf, "Origin": origin})

    def test_fail_closed_before_credentials_and_legacy_scene_endpoints_removed(self):
        with patch.dict(os.environ, {"CONCAT_AUTH_MODE": "", "CONCAT_ADMIN_PASSWORD_HASH": ""}):
            app = create_app()
            with TestClient(app, base_url=ORIGIN) as client:
                self.assertEqual(client.get("/auth/login").status_code, 503)
                self.assertEqual(client.get("/dashboard").status_code, 401)
        for path in ("/auth/scene/start", "/auth/scene/callback"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_forged_headers_and_scene_cookie_cannot_authenticate(self):
        for path in ("/", "/admin", "/api/v1/admin/users", "/api/v1/session",
                     "/dashboard", "/concat/media", "/concat-uploader/queue"):
            with self.subTest(path=path):
                res = self.client.get(path, headers={
                    "X-Auth-User": "owner", "X-Is-Admin": "1",
                    "X-Forwarded-User": "owner", "X-App-Access": "all",
                })
                self.assertEqual(res.status_code, 401)
        self.client.cookies.set("session", "legacy-scene-session")
        self.assertEqual(self.client.get("/dashboard").status_code, 401)

    def test_local_login_dashboard_concat_uploader_and_admin(self):
        csrf = self._signed_in()
        for path in ("/dashboard", "/concat/production", "/concat/media",
                     "/concat/gpu-config", "/concat-uploader/sheets-sync",
                     "/concat-uploader/queue", "/admin", "/api/v1/session"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text[:250])
        user = self.client.get("/api/v1/session").json()
        self.assertEqual(user["username"], "owner")
        self.assertTrue(user["is_admin"])
        users = self.client.get("/api/v1/admin/users").json()["users"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "owner")
        self.assertEqual(self.client.get("/api/v1/admin/config").json()["identity"],
                         "standalone-local-admin")
        self.assertEqual(self._post("/api/v1/admin/config", csrf,
                                    {"session_max_minutes": 60}).status_code, 200)

    def test_failed_login_csrf_cross_origin_and_bruteforce_limit(self):
        self.assertEqual(self._login(password="incorrect").status_code, 401)
        self.assertEqual(self._login(username="another").status_code, 401)
        self.assertEqual(self._login(csrf="forged").status_code, 403)
        self.assertEqual(self._login(origin="https://evil.example").status_code, 403)
        self.assertEqual(self._login().status_code, 303)
        self.client.cookies.clear()
        for _ in range(8):
            self.assertEqual(self._login(password="wrong").status_code, 401)
        self.assertEqual(self._login().status_code, 429)

    def test_csrf_logout_and_server_side_revocation(self):
        csrf = self._signed_in()
        self.assertEqual(self._post("/auth/logout", "forged").status_code, 403)
        self.assertEqual(self._post("/auth/logout", csrf,
                                    origin="https://evil.example").status_code, 403)
        self.assertEqual(self._post("/auth/logout", csrf).status_code, 303)
        self.assertEqual(self.client.get("/dashboard").status_code, 401)
        csrf = self._signed_in()
        self.assertEqual(self._post("/api/v1/admin/sessions/revoke-all", csrf).status_code, 200)
        self.assertEqual(self.client.get("/dashboard").status_code, 401)

    def test_session_survives_restart_and_password_rotation_invalidates_it(self):
        self._signed_in()
        token = self.client.cookies.get(COOKIE)
        newer_app = create_app()
        with TestClient(newer_app, base_url=ORIGIN) as other:
            other.cookies.set(COOKIE, token)
            self.assertEqual(other.get("/dashboard").status_code, 200)
        changed = password_hash("Different-Admin-Password-After-Rotation")
        with patch.dict(os.environ, {"CONCAT_ADMIN_PASSWORD_HASH": changed}):
            rotated = create_app()
            with TestClient(rotated, base_url=ORIGIN) as other:
                other.cookies.set(COOKIE, token)
                self.assertEqual(other.get("/dashboard").status_code, 401)

    def test_owner_protection_applies_to_admin_too(self):
        principal = Principal("owner", is_admin=True, is_active=True, concat_granted=True)
        valid = {"owner_username": "owner", "app": "concat", "id": 1}
        foreign = {"owner_username": "other", "app": "concat", "id": 2}
        legacy = {"app": "concat", "id": 3}
        cross_app = {"owner_username": "owner", "app": "concat-uploader", "id": 4}
        self.assertEqual(ensure_owner(principal, valid, "concat")["id"], 1)
        self.assertEqual(owned_records(principal, [valid, foreign, legacy, cross_app],
                                       "concat"), [valid])
        for record in (foreign, legacy, cross_app):
            with self.assertRaises(HTTPException) as caught:
                require_owned_resource(principal, record, "concat")
            self.assertEqual(caught.exception.status_code, 404)

    def test_private_database_and_config_no_secret_disclosure(self):
        self._signed_in()
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        for marker in ("CONCAT_ADMIN_PASSWORD_HASH", "CONCAT_ADMIN_SESSION_SECRET",
                       self.encoded, "CONCAT_SCENE_SSO_SECRET"):
            self.assertNotIn(marker, self.client.get("/api/v1/admin/config").text)
        with sqlite3.connect(self.db) as con:
            cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
            self.assertIn("credential_tag", cols)
            self.assertNotIn("scene_sid_hash", cols)


if __name__ == "__main__":
    unittest.main()
