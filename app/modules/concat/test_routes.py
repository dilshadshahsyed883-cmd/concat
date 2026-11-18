"""Focused owner/auth/receiver/Cloud Render integration tests. Offline only."""
import hashlib
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.auth import Principal
from app.main import create_app
from app.modules.concat.tenant import get_registry


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.state.concat_data_root = self.temp.name
        self.owner = "alice"
        self.granted = True
        self.app.state.identity_resolver = lambda request: (
            Principal(self.owner, is_active=True, concat_granted=self.granted)
            if self.owner is not None else None
        )
        self.client = TestClient(self.app, base_url="https://concat.scenebuilder.online")

    def tearDown(self):
        get_registry(self.app).close()
        self.client.close()
        self.temp.cleanup()

    def auth_csrf(self):
        reply = self.client.get("/concat/production")
        self.assertEqual(reply.status_code, 200, reply.text[:350])
        token = re.search(r'data-csrf="([^"]+)"', reply.text)
        self.assertIsNotNone(token)
        return {"X-Cloud-Render-CSRF": token.group(1)}

    def render_file(self, manager, job="video-alice"):
        mp4 = b"\x00\x00\x00\x10ftypisomtest-data"
        sha = hashlib.sha256(mp4).hexdigest()
        result = manager.receive(job, 0, mp4, name="verified.mp4", size=len(mp4), sha=sha)
        self.assertEqual(result["state"], "ready")
        return mp4, sha

    def test_unverified_and_revoked_access_is_closed(self):
        for path in ("/concat/production", "/concat/media", "/concat/gpu-config",
                     "/api/cloud-render/config", "/api/cloud-render/production",
                     "/api/cloud-render/media", "/concat/assets/cloud_render.js"):
            self.owner = None
            self.assertEqual(self.client.get(path, headers={"X-Auth-User": "admin"}).status_code, 401)
            self.owner = "alice"
            self.granted = False
            self.assertEqual(self.client.get(path).status_code, 403)
            self.granted = True

    def test_real_copied_ui_and_three_tabs(self):
        for tab, slug in (("production","production"), ("media","media"), ("gpu","gpu-config")):
            page = self.client.get("/concat/" + slug)
            self.assertEqual(page.status_code, 200)
            self.assertIn('data-initial-tab="' + tab + '"', page.text)
            self.assertIn("Scan &amp; Add to Queue", page.text)
            self.assertIn("Copy Link", page.text)
            self.assertIn("SaladCloud", page.text)
            self.assertEqual(page.text.count('class="nav-group"'), 3)
        self.assertIn("copyMediaLink", self.client.get("/concat/assets/cloud_render.js").text)
        self.assertEqual(self.client.get("/cloud-render/", follow_redirects=False).status_code, 303)

    def test_csrf_and_cross_origin_mutations(self):
        token = self.auth_csrf()
        self.assertEqual(self.client.post("/api/cloud-render/config", json={"auto_upload": True}).status_code, 403)
        self.assertEqual(self.client.post("/api/cloud-render/config",
            headers={**token, "Origin": "https://evil.example"}, json={"auto_upload": True}).status_code, 403)
        self.assertEqual(self.client.post("/api/cloud-render/config",
            headers=token, json={"auto_upload": True}).status_code, 200)

    def test_owner_specific_gpu_and_media(self):
        token = self.auth_csrf()
        self.assertEqual(self.client.post("/api/cloud-render/config", headers=token,
            json={"endpoint": "https://alice-renderer.salad.cloud", "priority": "low"}).status_code, 200)
        alice = get_registry(self.app).for_principal(Principal("alice"))
        mp4, sha = self.render_file(alice)
        self.assertEqual(self.client.get("/api/cloud-render/media/video-alice/file").content, mp4)
        self.assertEqual(len(self.client.get("/api/cloud-render/media").json()["items"]), 1)
        self.owner = "bob"
        bob_token = self.auth_csrf()
        self.assertEqual(self.client.get("/api/cloud-render/config").json()["endpoint"], "")
        self.assertEqual(self.client.get("/api/cloud-render/media").json()["items"], [])
        self.assertEqual(self.client.get("/api/cloud-render/media/video-alice/file").status_code, 404)
        self.assertEqual(self.client.post("/api/cloud-render/media/video-alice/rename",
            headers=bob_token, json={"name": "stolen.mp4"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/cloud-render/media/video-alice",
            headers=bob_token).status_code, 404)
        self.owner = "alice"
        self.assertEqual(self.client.get("/api/cloud-render/media/video-alice/file").content, mp4)
        self.assertEqual(self.client.get("/api/v1/concat/gpu-config").json()["priority"], "low")

    def test_job_and_queue_isolation(self):
        token = self.auth_csrf()
        alice = get_registry(self.app).for_principal(Principal("alice"))
        with alice.connection() as db:
            db.execute("""INSERT INTO production_jobs
                        (remote_id, drive_id, name, resource_keys, drive_size,
                         drive_md5, folder_id, drive_path)
                        VALUES (?,?,?,?,?,?,?,?)""",
                       ("production-example", "drive-1", "one.zip", "", 2, "", "", ""))
        self.assertEqual(len(self.client.get("/api/cloud-render/production").json()["items"]), 1)
        self.owner = "bob"
        bob_token = self.auth_csrf()
        self.assertEqual(self.client.get("/api/cloud-render/production").json()["items"], [])
        self.assertEqual(self.client.post("/api/cloud-render/production/jobs/1/cancel",
            headers=bob_token).status_code, 404)
        self.owner = "alice"
        self.assertEqual(self.client.post("/api/cloud-render/production/jobs/1/cancel",
            headers=token).status_code, 200)
        self.assertEqual(self.client.get("/api/cloud-render/production").json()["items"][0]["state"], "Cancelled")

    def test_callback_token_is_scoped_and_rotates(self):
        token = self.auth_csrf()
        manager = get_registry(self.app).for_principal(Principal("alice"))
        with manager.connection() as db:
            secret = manager.get(db, "receiver_token")
        mp4 = b"\x00\x00\x00\x10ftypisomtest-data"
        params = {"index": 0, "name": "render.mp4", "size": len(mp4),
                  "sha256": hashlib.sha256(mp4).hexdigest()}
        path = "/api/cloud-render/ingest/render-alice/chunk"
        self.assertEqual(self.client.post(path, params=params, content=mp4,
            headers={"X-Cloud-Render-Token": "bad"}).status_code, 401)
        self.assertEqual(self.client.post(path, params=params, content=mp4,
            headers={"X-Cloud-Render-Token": secret}).status_code, 200)
        self.owner = "bob"
        self.assertEqual(self.client.get("/api/cloud-render/media/render-alice/file").status_code, 404)
        self.owner = "alice"
        new_token = self.client.post("/api/cloud-render/token/rotate", headers=token).json()["receiver_token"]
        self.assertNotEqual(secret, new_token)
        self.assertEqual(self.client.post("/api/cloud-render/ingest/another/chunk",
            params=params, content=mp4, headers={"X-Cloud-Render-Token": secret}).status_code, 401)

    def test_does_not_adopt_unattributed_legacy_rows(self):
        # Independent storage is deliberately not populated from ownerless VPS_1 DBs.
        self.auth_csrf()
        root = get_registry(self.app).root
        (root / "cloud_render.sqlite3").write_bytes(b"legacy ownerless data left untouched")
        self.assertEqual(self.client.get("/api/cloud-render/media").json()["items"], [])
        self.assertEqual((root / "cloud_render.sqlite3").read_bytes(),
                         b"legacy ownerless data left untouched")


if __name__ == "__main__":
    unittest.main()
