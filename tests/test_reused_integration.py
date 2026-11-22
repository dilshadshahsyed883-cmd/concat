"""Offline Concat Web integration: receiver -> checked MP4 -> persistent queue -> dashboard.

No Salad request, Google OAuth call or real YouTube publish is made by these tests.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.auth import Principal
from app.main import create_app
from app.modules.concat.tenant import get_registry, owner_key

ROOT = Path(__file__).resolve().parents[1]
UPLOADER = ROOT / "reused" / "uploader" / "part01"
STAGE_SOURCE = UPLOADER / "lum_engine" / "concat_source.py"
ORIGIN = "https://concat.scenebuilder.online"
VIDEO_ID = "render-alice"
MP4 = b"\x00\x00\x00\x10ftypisomtest-data"


def source_module():
    spec = importlib.util.spec_from_file_location("verified_concat_web_source", STAGE_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FinalIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cloud = base / "cloud"
        self.uploads = base / "uploader"
        env = {
            "CONCAT_UPLOADER_ENABLED": "1",
            "CONCAT_WEB_IN_PROCESS": "1",
            "CONCAT_CLOUD_RENDER_ROOT": str(self.cloud),
            "CONCAT_PUBLIC_ORIGIN": ORIGIN,
            "LUM_DATA_DIR": str(self.uploads),
            "LUM_SESSION_SECRET": "unit-test-secret-is-never-used-in-prod-1234",
            "LUM_ENGINE_INSTANCE": "concat",
            "LUM_BASE_PATH": "/concat-uploader",
            "LUM_SUITE_AUTH": "1",
            "LUM_COOKIE_SECURE": "1",
        }
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.app = create_app()
        self.app.state.concat_data_root = str(self.cloud)
        self.owner = "alice"
        self.granted = True
        self.admin = False
        self.app.state.identity_resolver = lambda request: (
            Principal(self.owner, is_admin=self.admin, is_active=True,
                      concat_granted=self.granted) if self.owner else None
        )
        self.client = TestClient(self.app, base_url=ORIGIN)

    def tearDown(self):
        get_registry(self.app).close()
        self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def ingest(self):
        manager = get_registry(self.app).for_principal(Principal("alice"))
        with manager.connection() as con:
            secret = manager.get(con, "receiver_token")
            con.execute("""INSERT INTO production_jobs
                (remote_id,drive_id,name,resource_keys,drive_size,
                 drive_md5,folder_id,drive_path,state)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (VIDEO_ID, "drive-render-a", "render.zip", "", len(MP4),
                 "", "", "", "Completed"))
        digest = hashlib.sha256(MP4).hexdigest()
        reply = self.client.post(
            "/api/cloud-render/ingest/" + VIDEO_ID + "/chunk",
            params={"index": 0, "name": "render.mp4", "size": len(MP4), "sha256": digest},
            content=MP4, headers={"X-Cloud-Render-Token": secret},
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["state"], "ready")
        return digest

    def queue_verified_video(self, digest):
        source = source_module()
        stage = self.uploads / "staging"
        link = ORIGIN + "/api/cloud-render/media/" + VIDEO_ID + "/file"
        local, name, length, sha = source.stage_concat_link(
            link, "alice", stage, cloud_root=self.cloud, origin=ORIGIN)
        self.assertEqual((name, length, sha), ("render.mp4", len(MP4), digest))
        self.assertEqual(Path(local).read_bytes(), MP4)
        with self.assertRaises(source.ConcatSourceError):
            source.stage_concat_link(link, "bob", stage, cloud_root=self.cloud, origin=ORIGIN)
        with self.assertRaises(source.ConcatSourceError):
            source.stage_concat_link(link.replace(ORIGIN, "https://evil.example"),
                                     "alice", stage, cloud_root=self.cloud, origin=ORIGIN)
        # Use the copied real persistent queue repository, not a dashboard mock.
        script = """
import json, os
from lum_engine.project_store import ProjectStore
store = ProjectStore()
data = dict(owner_username="alice", source_mode="local_file",
            staging_path=os.environ["TEST_STAGE"], source_origin="concat_link", concat_job_id="legacy-fixture",
            source_fingerprint=os.environ["TEST_SHA"], channel_db_id=1,
            title="Verified render", description="Test publish description",
            privacy_status="private", source_file_name="render.mp4",
            source_file_size=int(os.environ["TEST_SIZE"]))
job = store.create_project(data)
try:
    store.create_project(data)
except ValueError:
    duplicate_blocked = True
else:
    duplicate_blocked = False
print(json.dumps(dict(job=job, duplicate_blocked=duplicate_blocked,
                      paused=store.owner_queue_paused("alice"),
                      states=store.counts(owner="alice"))))
"""
        env = dict(os.environ)
        env.update({"TEST_STAGE": local, "TEST_SHA": sha, "TEST_SIZE": str(length),
                    "PYTHONPATH": str(UPLOADER)})
        for key in ("YOUTUBE_UPLOAD_MANAGER_DATA_DIR", "LUM_GOOGLE_OAUTH_ROOT", "LUM_AUTO_ENGINE_V6_ROOT"):
            env.pop(key, None)
        done = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env,
            check=True, text=True, capture_output=True, timeout=25,
        )
        data = json.loads(done.stdout.strip())
        self.assertTrue(data["duplicate_blocked"])
        self.assertTrue(data["paused"])
        self.assertEqual(data["states"]["TOTAL"], 1)
        return data["job"]

    def test_worker_receipt_verified_source_and_persistent_owner_queue(self):
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 503)
        digest = self.ingest()
        queued_id = self.queue_verified_video(digest)
        owner = self.client.get("/api/v1/dashboard/overview")
        self.assertEqual(owner.status_code, 200, owner.text)
        result = owner.json()
        self.assertEqual(result["renders"]["completed"], 1)
        self.assertEqual(result["videos"]["total"], 1)
        self.assertEqual(result["videos"]["items"][0]["status"], "ready")
        self.assertEqual(result["uploads"]["queued"], 1)
        self.assertEqual(result["uploads"]["jobs"][0]["id"], str(queued_id))
        self.assertEqual(result["uploads"]["queue"]["state"], "stopped")
        self.assertIsNone(result["gpu"]["available"])
        # No reinitialization of queues: another read uses the same persistent DB.
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").json()["uploads"]["total"], 1)
        for other, is_admin in (("bob", False), ("admin", True)):
            self.owner, self.admin = other, is_admin
            reply = self.client.get("/api/v1/dashboard/overview")
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(reply.json()["renders"]["total"], 0)
            self.assertEqual(reply.json()["videos"]["total"], 0)
            self.assertEqual(reply.json()["uploads"]["total"], 0)
            self.assertEqual(self.client.get(
                "/api/cloud-render/media/" + VIDEO_ID + "/file").status_code, 404)
        self.owner, self.admin = "alice", False

    def test_local_admin_login_to_verified_mp4_and_uploader_queue(self):
        """Use actual standalone login rather than a test identity injection.

        Salad delivery is simulated by a checksum-verified receiver chunk. The
        uploader uses the real copied staging and persistent queue repository.
        """
        from app.modules.admin.security import LOGIN_CSRF_COOKIE, password_hash

        self.app.state.identity_resolver = None
        with patch.dict(os.environ, {
            "CONCAT_AUTH_MODE": "local-admin",
            "CONCAT_ADMIN_USERNAME": "alice",
            "CONCAT_ADMIN_PASSWORD_HASH": password_hash(
                "Long-Standalone-Admin-Test-Password"),
            "CONCAT_ADMIN_SESSION_SECRET": "independent-test-session-key-over-thirty-two",
            "CONCAT_WEB_SECURITY_DB": str(Path(self.tmp.name) / "admin.sqlite3"),
            "CONCAT_WEB_DATA_ROOT": str(self.cloud),
        }):
            self.assertEqual(self.client.get("/dashboard").status_code, 401)
            page = self.client.get("/auth/login")
            self.assertEqual(page.status_code, 200)
            import re
            match = re.search(r'name="csrf" value="([^"]+)"', page.text)
            self.assertIsNotNone(match)
            self.assertTrue(self.client.cookies.get(LOGIN_CSRF_COOKIE))
            response = self.client.post(
                "/auth/login",
                data={"username": "alice", "password": "Long-Standalone-Admin-Test-Password",
                      "csrf": match.group(1)},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303, response.text)
            for path in ("/dashboard", "/concat/production", "/concat/media",
                         "/concat/gpu-config", "/admin"):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)
            digest = self.ingest()
            job_id = self.queue_verified_video(digest)
            result = self.client.get("/api/v1/dashboard/overview")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["renders"]["completed"], 1)
            self.assertEqual(result.json()["videos"]["total"], 1)
            self.assertEqual(result.json()["uploads"]["queued"], 1)
            self.assertEqual(result.json()["uploads"]["jobs"][0]["id"], str(job_id))
            csrf = self.client.get("/api/v1/auth/csrf").json()["csrf_token"]
            out = self.client.post("/auth/logout", headers={
                "X-CSRF-Token": csrf, "Origin": ORIGIN
            }, follow_redirects=False)
            self.assertEqual(out.status_code, 303)
            self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 401)

    def test_standalone_admin_uploader_ui_in_clean_process(self):
        """Fresh server process proves copied uploader UI uses isolated state.

        Other tests import the reused core with independent fixtures at module
        discovery time. A real deployed Concat process starts with no such
        imports, so exercise the guarded lazy load in its own Python process.
        """
        from app.modules.admin.security import password_hash
        script = r'''
import os, re
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from app.modules.uploader.gateway import backend_for

app = create_app()
with TestClient(app, base_url="https://concat.scenebuilder.online") as client:
    page = client.get("/auth/login")
    assert page.status_code == 200
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post("/auth/login",
        data={"username": "alice", "password": "Long-Standalone-Admin-Test-Password",
              "csrf": token},
        headers={"Origin": "https://concat.scenebuilder.online"},
        follow_redirects=False)
    assert response.status_code == 303, response.text
    response = client.get("/concat-uploader/queue")
    assert response.status_code == 200, response.text[:500]
    from desktop_core.modules.path_utils import USER_DATA_ROOT
    from lum_engine.config import DATA_ROOT
    assert USER_DATA_ROOT == Path(os.environ["LUM_DATA_DIR"]) / "core"
    assert DATA_ROOT == Path(os.environ["LUM_DATA_DIR"])
    assert Path(os.environ["LUM_DATA_DIR"]).joinpath("projects.sqlite3").is_file()
    assert client.get("/api/v1/session").json()["username"] == "alice"
print("STANDALONE_UPLOADER_UI=PASS_PRIVATE_ROOT")
'''
        env = dict(os.environ)
        env.update({
            "CONCAT_AUTH_MODE": "local-admin",
            "CONCAT_ADMIN_USERNAME": "alice",
            "CONCAT_ADMIN_PASSWORD_HASH": password_hash(
                "Long-Standalone-Admin-Test-Password"),
            "CONCAT_ADMIN_SESSION_SECRET": "local-admin-private-test-session-secret",
            "CONCAT_WEB_SECURITY_DB": str(Path(self.tmp.name) / "single-admin.sqlite3"),
            "CONCAT_WEB_DATA_ROOT": str(self.cloud),
            "CONCAT_WEB_PUBLIC_ORIGIN": ORIGIN,
            "LUM_DATA_DIR": str(self.uploads),
            "LUM_ENGINE_INSTANCE": "concat",
            "LUM_BASE_PATH": "/concat-uploader",
            "LUM_SUITE_AUTH": "1",
            "LUM_COOKIE_SECURE": "1",
            "CONCAT_WEB_IN_PROCESS": "1",
            "CONCAT_UPLOADER_ENABLED": "1",
            "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(self.uploads / "core"),
            "LUM_GOOGLE_OAUTH_ROOT": str(self.uploads / "secure"),
            "LUM_AUTO_ENGINE_V6_ROOT": str(self.uploads / "auto_engine_v6"),
        })
        done = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                              env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(done.returncode, 0, done.stdout + "\\n" + done.stderr[-3200:])
        self.assertIn("STANDALONE_UPLOADER_UI=PASS_PRIVATE_ROOT", done.stdout)

    def test_legacy_quarantine_revocation_csrf_and_cross_owner_rows(self):
        self.ingest()
        source = source_module()
        (self.cloud / "cloud_render.sqlite3").write_bytes(b"unattributed legacy data")
        # The Concat Web source is *never* permitted to fall back to root DB.
        self.owner = "bob"
        link = ORIGIN + "/api/cloud-render/media/" + VIDEO_ID + "/file"
        with self.assertRaises(source.ConcatSourceError):
            source.stage_concat_link(link, "bob", self.uploads / "staging",
                                     cloud_root=self.cloud, origin=ORIGIN)
        self.owner = "alice"
        self.assertEqual(self.client.post(
            "/api/cloud-render/config", json={"auto_upload": False}).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/cloud-render/config", json={"auto_upload": False},
            headers={"Origin": "https://evil.example"}).status_code, 403)
        self.owner = None
        self.assertEqual(self.client.get(
            "/api/cloud-render/media", headers={"X-Auth-User": "alice",
                                                "X-Is-Admin": "1"}).status_code, 401)
        self.owner = "__default__"
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 401)
        self.assertEqual((self.cloud / "cloud_render.sqlite3").read_bytes(),
                         b"unattributed legacy data")


if __name__ == "__main__":
    unittest.main()
