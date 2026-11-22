"""Focused owner/auth/receiver/Cloud Render integration tests. Offline only."""
import hashlib
import re
import tempfile
import unittest

from fastapi.testclient import TestClient
from unittest.mock import Mock, patch
from pathlib import Path

from app.modules.concat.engine.cloud_render import CloudRender

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
            self.assertIn('id="cr-media-list"', page.text)
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

    def test_auto_worker_settings_validation_isolation_and_retry_csrf(self):
        token = self.auth_csrf()
        page = self.client.get("/concat/gpu-config").text
        self.assertIn('<details class="cr-card" id="cr-connection-setup">', page)
        self.assertIn('id="cr-pull-threshold"', page)
        self.assertIn('id="cr-pull-seconds"', page)
        path = "/api/cloud-render/config"
        saved = self.client.post(path, headers=token, json={"auto_start": True,
            "image_pull_threshold": 12, "image_pull_seconds": 35,
            "salad_organization": "owner-org", "salad_project": "owner-project",
            "salad_container_group": "owner-group"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["image_pull_threshold"], 12)
        self.assertEqual(saved.json()["image_pull_seconds"], 35)
        self.assertTrue(saved.json()["auto_start"])
        for bad in (0, 101, True, 8.5, "8"):
            self.assertEqual(self.client.post(path, headers=token,
                json={"image_pull_threshold": bad}).status_code, 400)
        retry = "/api/cloud-render/gpu/auto-retry"
        self.assertEqual(self.client.post(retry).status_code, 403)
        self.assertEqual(self.client.post(retry, headers=token).status_code, 200)
        self.assertEqual(self.client.post("/api/cloud-render/gpu/worker/start", headers=token).status_code, 409)
        self.client.post(path, headers=token, json={"auto_start": False})
        self.assertEqual(self.client.post("/api/cloud-render/gpu/worker/start").status_code, 403)
        self.assertEqual(self.client.post("/api/cloud-render/gpu/worker/start", headers=token).status_code, 200)
        self.owner = "bob"
        cfg = self.client.get(path).json()
        self.assertFalse(cfg["auto_start"])
        self.assertEqual(cfg["salad_container_group"], "")
        self.assertEqual(cfg["image_pull_threshold"], 8)
        self.owner = None
        self.assertEqual(self.client.post(retry, headers=token).status_code, 401)

    def test_salad_idle_controls_and_persistence(self):
        page = self.client.get("/concat/gpu-config")
        self.assertEqual(page.status_code, 200)
        for ident in ("cr-auto-start", "cr-pull-threshold", "cr-pull-seconds", "cr-save-automation"):
            self.assertIn('id="' + ident + '"', page.text)
        token = self.auth_csrf()
        for bad in (0, 61, True, 1.5, "5"):
            self.assertEqual(self.client.post("/api/cloud-render/config", headers=token,
                json={"idle_timeout_minutes": bad}).status_code, 400)
        self.assertEqual(self.client.post("/api/cloud-render/config", headers=token,
            json={"auto_shutdown": "true"}).status_code, 400)
        saved = self.client.post("/api/cloud-render/config", headers=token,
            json={"auto_shutdown": True, "idle_timeout_minutes": 7})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["auto_shutdown"])
        self.assertEqual(saved.json()["idle_timeout_minutes"], 7)
        self.owner = "bob"
        self.assertFalse(self.client.get("/api/cloud-render/config").json()["auto_shutdown"])
        self.owner = "alice"
        self.assertEqual(self.client.get("/api/cloud-render/config").json()["idle_timeout_minutes"], 7)

    def test_salad_idle_stop_requires_real_idle_and_running_group(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = CloudRender(Path(folder) / "owner", tenant_key="a" * 64)
            worker = Mock()
            worker.timeout = 60
            worker.request.return_value = {"active_renders": 0}
            group = Mock(status_code=200)
            group.json.return_value = {"current_state": {"status": "running"},
                                       "autostart_policy": False}
            stopped = Mock(status_code=202)
            env = {"CONCAT_SALAD_ORGANIZATION": "org-one",
                   "CONCAT_SALAD_PROJECT": "proj-one",
                   "CONCAT_SALAD_CONTAINER_GROUP": "render-one"}
            try:
                with manager.connection() as con:
                    for k, v in {"auto_shutdown":"1", "idle_timeout_minutes":"5",
                                 "enabled":"1", "endpoint":"https://render.salad.cloud",
                                 "worker_key":"example-worker-token",
                                 "salad_key":"example-management-key"}.items():
                        manager.put(con, k, v)
                with patch.dict("os.environ", env), patch.object(
                    manager, "production_worker", return_value=worker
                ), patch("app.modules.concat.engine.salad_idle.requests.get",
                         return_value=group) as get, patch(
                    "app.modules.concat.engine.salad_idle.requests.post",
                    return_value=stopped
                ) as post, patch("app.modules.concat.engine.salad_idle.time.time",
                                  return_value=1000) as clock:
                    manager.salad_idle.check()
                    clock.return_value = 1299
                    manager.salad_idle.check()
                    post.assert_not_called()
                    clock.return_value = 1300
                    manager.salad_idle.check()
                    self.assertEqual(post.call_count, 1)
                    self.assertTrue(post.call_args.args[0].endswith(
                        "/organizations/org-one/projects/proj-one/containers/render-one/stop"))
                    self.assertFalse(post.call_args.kwargs["allow_redirects"])
                    self.assertEqual(post.call_args.kwargs["headers"]["Salad-Api-Key"],
                                     "example-management-key")
                    self.assertEqual(manager.config()["salad_idle_status"], "stop_requested")
                    self.assertEqual(manager.config()["salad_idle_since"], 0)
                    self.assertTrue(get.called)
                    manager.salad_idle.check()
                    self.assertEqual(post.call_count, 1)
            finally:
                manager.production.close()
                manager.pool.shutdown(wait=False, cancel_futures=True)

    def test_salad_idle_never_stops_with_queued_job_or_unknown_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = CloudRender(Path(folder) / "owner", tenant_key="b" * 64)
            env = {"CONCAT_SALAD_ORGANIZATION": "org-one",
                   "CONCAT_SALAD_PROJECT": "proj-one",
                   "CONCAT_SALAD_CONTAINER_GROUP": "render-one"}
            worker = Mock()
            worker.timeout = 30
            worker.request.return_value = {"active_renders": 1}
            try:
                with manager.connection() as con:
                    for k, v in {"auto_shutdown":"1", "idle_timeout_minutes":"1",
                                 "enabled":"1", "endpoint":"https://render.salad.cloud",
                                 "worker_key":"worker-token", "salad_key":"api-key"}.items():
                        manager.put(con, k, v)
                    con.execute("""INSERT INTO production_jobs
                        (remote_id,drive_id,name,resource_keys,drive_size,drive_md5,
                         folder_id,drive_path,state) VALUES(?,?,?,?,?,?,?,?,?)""",
                        ("remote-1", "drive-1", "pending.zip", "", 1, "", "", "", "Queued"))
                with patch.dict("os.environ", env), patch.object(
                    manager, "production_worker", return_value=worker
                ), patch("app.modules.concat.engine.salad_idle.requests.post") as post, patch(
                    "app.modules.concat.engine.salad_idle.requests.get") as get, patch(
                    "app.modules.concat.engine.salad_idle.time.time", return_value=1000
                ) as clock:
                    manager.salad_idle.check()
                    self.assertEqual(manager.config()["salad_idle_status"], "busy")
                    post.assert_not_called()
                    with manager.connection() as con:
                        con.execute("UPDATE production_jobs SET state='Completed'")
                    manager.salad_idle.check()  # new full countdown
                    clock.return_value = 1061
                    manager.salad_idle.check()
                    self.assertEqual(manager.config()["salad_idle_status"], "worker_busy")
                    get.assert_not_called()
                    post.assert_not_called()
            finally:
                manager.production.close()
                manager.pool.shutdown(wait=False, cancel_futures=True)

    def test_salad_idle_fail_closed_without_group_or_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = CloudRender(Path(folder) / "owner", tenant_key="c" * 64)
            try:
                manager.save({"auto_shutdown": True, "idle_timeout_minutes": 5})
                with patch.dict("os.environ", {
                    "CONCAT_SALAD_ORGANIZATION": "", "CONCAT_SALAD_PROJECT": "",
                    "CONCAT_SALAD_CONTAINER_GROUP": ""
                }), patch("app.modules.concat.engine.salad_idle.requests.post") as post:
                    manager.salad_idle.check()
                    post.assert_not_called()
                    self.assertFalse(manager.config()["auto_shutdown_ready"])
                    self.assertEqual(manager.config()["salad_idle_status"], "not_configured")
            finally:
                manager.production.close()
                manager.pool.shutdown(wait=False, cancel_futures=True)

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
