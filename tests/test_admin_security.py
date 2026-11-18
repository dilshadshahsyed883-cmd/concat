"""Negative security acceptance tests: two owners and an admin ordinary owner.

Uses temporary SQLite fixtures only. No VPS, Drive, Salad, YouTube or Scene
production account is contacted. The signed ticket fixture represents the
*separately required* authenticated Scene issuer.
"""
import base64
from contextlib import ExitStack
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import Principal, require_owned_resource
from app.main import create_app
from app.modules.admin.security import ensure_owner, owned_records, COOKIE, STATE_COOKIE


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.scene_db = self.temp / "scene.sqlite"
        self.auth_db = self.temp / "concat.sqlite"
        self.secret = "scene-sso-separate-secret-test-only--32-bytes-min"
        self.scene_origin = "https://app.scenebuilder.online"
        self.web_origin = "https://concat.scenebuilder.online"
        with sqlite3.connect(self.scene_db) as db:
            db.executescript("""
                CREATE TABLE users(
                    username TEXT PRIMARY KEY COLLATE NOCASE, secret_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL, is_admin INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE concat_sso_sessions(
                    sid_hash TEXT PRIMARY KEY, username_key TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, revoked_at INTEGER
                );
            """)
            for username, admin in (("alice", 0), ("bob", 0), ("root", 1)):
                db.execute("INSERT INTO users VALUES(?,?,?,?,?)",
                           (username, "never-exposed", 1, admin, "2026-11-18"))
        self.sids = {}
        for username in ("alice", "bob", "root"):
            self.add_scene_sid(username)
        self.stack.enter_context(patch.dict(os.environ, {
            "CONCAT_WEB_PUBLIC_ORIGIN": self.web_origin,
            "CONCAT_SCENE_PUBLIC_ORIGIN": self.scene_origin,
            "CONCAT_SCENE_USERS_DB": str(self.scene_db),
            "CONCAT_WEB_SECURITY_DB": str(self.auth_db),
            "CONCAT_SCENE_SSO_SECRET": self.secret,
            "CONCAT_BOOTSTRAP_ADMINS": "root",
        }))
        self.app = create_app()
        self.client = TestClient(self.app, base_url=self.web_origin, follow_redirects=False)
        self.addCleanup(self.client.close)
        from app.modules.admin.security import SecurityService
        self.service = SecurityService.from_environment(self.app)
        self.app.state.concat_security = self.service
        self.service.grant("root", "alice", True)
        self.service.grant("root", "bob", True)

    def add_scene_sid(self, username):
        sid = secrets.token_urlsafe(40)
        self.sids[username] = sid
        with sqlite3.connect(self.scene_db) as db:
            db.execute("INSERT INTO concat_sso_sessions(sid_hash,username_key,expires_at,revoked_at) VALUES(?,?,?,NULL)",
                       (hashlib.sha256(sid.encode()).hexdigest(), username.casefold(), int(time.time()) + 7200))

    def sign_ticket(self, username="alice", **overrides):
        now = int(time.time())
        payload = dict(v=1, iss=self.scene_origin, aud="concat-web", sub=username,
                       sid=self.sids[username], state=self.client.cookies.get(STATE_COOKIE, ""),
                       jti=secrets.token_urlsafe(36), iat=now, exp=now + 60)
        payload.update(overrides)
        data = b64(json.dumps(payload, separators=(",", ":")).encode())
        signature = hmac.new(self.secret.encode(), ("scene-concat-v1." + data).encode(),
                             hashlib.sha256).digest()
        return data + "." + b64(signature)

    def start_sso(self):
        res = self.client.get("/auth/scene/start", params={"next": "https://attacker.test/"})
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["location"].startswith(
            self.scene_origin + "/concat-sso/authorize?"))
        self.assertIn("redirect_uri=https%3A%2F%2Fconcat.scenebuilder.online%2Fauth%2Fscene%2Fcallback",
                      res.headers["location"])
        self.assertNotIn("attacker.test", res.headers["location"])
        self.assertTrue(self.client.cookies.get(STATE_COOKIE))
        return res

    def login(self, username):
        self.start_sso()
        state = self.client.cookies.get(STATE_COOKIE)
        res = self.client.get("/auth/scene/callback",
                              params={"ticket": self.sign_ticket(username), "state": state})
        self.assertEqual(res.status_code, 303, res.text[:300])
        self.assertEqual(res.headers["location"], "/dashboard")
        self.assertIn("Secure", res.headers["set-cookie"])
        self.assertTrue(self.client.cookies.get(COOKIE))
        return self.client.get("/api/v1/auth/csrf").json()["csrf_token"]

    def post(self, path, token=None, payload=None, origin=None):
        headers = {"origin": origin if origin is not None else self.web_origin}
        if token is not None:
            headers["x-csrf-token"] = token
        return self.client.post(path, json=payload, headers=headers)

    def test_forged_identity_and_legacy_cookie_never_sign_in(self):
        for path in ("/", "/admin", "/api/v1/admin/users", "/api/v1/auth/csrf",
                     "/api/v1/session", "/concat/media", "/concat-uploader/queue"):
            with self.subTest(path=path):
                res = self.client.get(path, headers={
                    "X-Auth-User": "root", "X-Is-Admin": "true",
                    "X-App-Access": "all", "X-Forwarded-User": "root",
                })
                self.assertEqual(res.status_code, 401)
        self.client.cookies.set("session", "fake-scene-session-cookie", domain="concat.scenebuilder.online")
        self.assertEqual(self.client.get("/dashboard").status_code, 401)

    def test_sso_ticket_tampering_expiry_replay_wrong_sid_and_state(self):
        self.start_sso()
        ticket = self.sign_ticket()
        state = self.client.cookies.get(STATE_COOKIE)
        bad = [
            ( ("A" if ticket[0] != "A" else "B") + ticket[1:], state),
            (self.sign_ticket(aud="unrelated"), state),
            (self.sign_ticket(exp=int(time.time()) - 1), state),
            (self.sign_ticket(sid=secrets.token_urlsafe(35)), state),
            (ticket, "forged-state"),
        ]
        for t, s in bad:
            with self.subTest(ticket=t[-10:], state=s):
                self.assertEqual(self.client.get("/auth/scene/callback",
                                 params={"ticket": t, "state": s}).status_code, 401)
        first = self.client.get("/auth/scene/callback", params={"ticket": ticket, "state": state})
        self.assertEqual(first.status_code, 303)
        # Restore the original nonce in the local test browser: the signed JTI
        # must remain rejected even with a matching CSRF state cookie.
        self.client.cookies.set(STATE_COOKIE, state, path="/")
        self.assertEqual(self.client.get("/auth/scene/callback",
                         params={"ticket": ticket, "state": state}).status_code, 401)

    def test_user_controls_and_admin_cannot_read_other_owner(self):
        self.login("alice")
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/session").json()["username"], "alice")
        alice = Principal("alice", is_active=True, concat_granted=True)
        admin = Principal("root", is_admin=True, is_active=True, concat_granted=True)
        rows = [
            {"id": "a", "owner_username": "alice", "app": "concat", "token": "a"},
            {"id": "b", "owner_username": "bob", "app": "concat", "token": "private"},
            {"id": "legacy", "app": "concat", "token": "legacy"},
            {"id": "cross-app", "owner_username": "alice", "app": "concat-uploader"},
        ]
        self.assertEqual([x["id"] for x in owned_records(alice, rows, "concat")], ["a"])
        for principal in (alice, admin):
            for row in rows[1:]:
                with self.subTest(user=principal.username, row=row["id"]):
                    with self.assertRaises(HTTPException) as ctx:
                        require_owned_resource(principal, row, "concat")
                    self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ensure_owner(alice, rows[0], "concat")["id"], "a")

    def test_revoked_scene_session_and_demoted_scene_account(self):
        token = self.login("root")
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/admin/users").status_code, 200)
        with sqlite3.connect(self.scene_db) as db:
            db.execute("UPDATE users SET is_admin=0 WHERE username='root'")
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.get("/dashboard").status_code, 403)
        with sqlite3.connect(self.scene_db) as db:
            db.execute("UPDATE users SET is_admin=1 WHERE username='root'")
            db.execute("UPDATE concat_sso_sessions SET revoked_at=? WHERE username_key='root'",
                       (int(time.time()),))
        self.assertEqual(self.client.get("/dashboard").status_code, 401)
        self.assertEqual(self.post("/api/v1/admin/config",token,{"maintenance_mode":True}).status_code,403)

    def test_cross_user_grants_revoke_live_sessions_and_csrf(self):
        alice_csrf = self.login("alice")
        self.assertEqual(self.post("/api/v1/admin/config", alice_csrf, {"maintenance_mode": True}).status_code, 403)
        self.client.cookies.clear()
        root_csrf = self.login("root")
        path = "/api/v1/admin/users/bob/access"
        self.assertEqual(self.post(path, payload={"concat_granted":False}).status_code,403)
        self.assertEqual(self.post(path, token="bad", payload={"concat_granted":False}).status_code,403)
        self.assertEqual(self.post(path, token=root_csrf, payload={"concat_granted":False},
                                   origin="https://evil.example").status_code,403)
        self.assertEqual(self.post(path, token=root_csrf, payload={"concat_granted":"yes"}).status_code,422)
        self.assertEqual(self.post(path, token=root_csrf, payload={"concat_granted":False}).status_code,200)
        users=self.client.get("/api/v1/admin/users").json()["users"]
        self.assertFalse(next(x for x in users if x["username"]=="bob")["concat_granted"])
        self.assertEqual(self.post("/api/v1/admin/users/unknown/access",
                                   token=root_csrf,payload={"concat_granted":True}).status_code,404)
        self.assertEqual(self.post("/api/v1/admin/users/alice/revoke-sessions",
                                   token=root_csrf).status_code,200)
        self.assertEqual(self.post("/api/v1/admin/config",token=root_csrf,
                                   payload={"maintenance_mode":True,"session_max_minutes":30}).status_code,200)
        self.assertEqual(self.client.get("/api/v1/admin/config").json()["config"]["session_max_minutes"],30)
        self.assertNotIn("CONCAT_SCENE_SSO_SECRET", self.client.get("/api/v1/admin/config").text)
        self.client.cookies.clear()
        self.start_sso()
        ticket=self.sign_ticket("alice")
        result=self.client.get("/auth/scene/callback",
                               params={"ticket":ticket,"state":self.client.cookies.get(STATE_COOKIE)})
        self.assertEqual(result.status_code,401) # maintenance closes ordinary accounts

    def test_individual_concats_sessions_and_logout(self):
        alice_csrf = self.login("alice")
        self.assertEqual(self.client.get("/concat/production").status_code, 200)
        self.assertEqual(self.post("/auth/logout", token="forged").status_code,403)
        self.assertEqual(self.post("/auth/logout", token=alice_csrf).status_code,303)
        self.assertEqual(self.client.get("/dashboard").status_code,401)

    def test_legacy_and_foreign_records_hidden_for_every_resource_family(self):
        principals = [
            Principal("alice", is_active=True, concat_granted=True),
            Principal("root", is_admin=True, is_active=True, concat_granted=True),
        ]
        for category, app in (
            ("render_job", "concat"), ("completed_media", "concat"),
            ("gpu_config", "concat"), ("channel", "concat-uploader"),
            ("oauth_credential", "concat-uploader"), ("upload_queue", "concat-uploader"),
            ("sheet", "concat-uploader"), ("thumbnail", "concat-uploader"),
        ):
            for principal in principals:
                for bad in (
                    {"id": category + "-bob", "owner_username": "bob", "app": app},
                    {"id": category + "-legacy", "app": app},
                    {"id": category + "-default", "owner_username": "__default__", "app": app},
                ):
                    with self.subTest(category=category, caller=principal.username, record=bad["id"]):
                        with self.assertRaises(HTTPException) as caught:
                            ensure_owner(principal, bad, app)
                        self.assertEqual(caught.exception.status_code, 404)

    def test_sessions_store_is_private_and_distinct_from_scene(self):
        from stat import S_IMODE
        self.assertNotEqual(self.auth_db.resolve(), self.scene_db.resolve())
        self.assertEqual(S_IMODE(self.auth_db.stat().st_mode), 0o600)
        self.assertEqual(self.client.get("/api/v1/session").status_code, 401)

    def test_scene_session_table_required_fail_closed(self):
        with sqlite3.connect(self.scene_db) as db:
            db.execute("DROP TABLE concat_sso_sessions")
        self.start_sso()
        response=self.client.get("/auth/scene/callback",
              params={"ticket":self.sign_ticket(), "state":self.client.cookies.get(STATE_COOKIE)})
        self.assertNotEqual(response.status_code,303)
        self.assertEqual(self.client.get("/dashboard").status_code,401)


if __name__ == "__main__":
    unittest.main()
