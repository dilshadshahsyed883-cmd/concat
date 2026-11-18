"""Cloud Render V1 upload safety and admin-scope regression tests."""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/scene-builder/part01"))
from cloud_render import CloudRender, filename, endpoint_url, GPU
from fastapi import HTTPException

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 81


class CloudRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mgr = CloudRender(Path(self.temp.name))
        self.sha = hashlib.sha256(MP4).hexdigest()
        self.md5 = hashlib.md5(MP4, usedforsecurity=False).hexdigest()

    def tearDown(self):
        self.mgr.pool.shutdown(wait=True)
        self.temp.cleanup()

    def receive(self, job="job-A"):
        return self.mgr.receive(job, 0, MP4, name="Concat test.mp4", size=len(MP4), sha=self.sha)

    def test_image_defaults_and_auto_upload_off(self):
        config=self.mgr.config()
        self.assertEqual(config["gpu"],"RTX 3060 Ti")
        self.assertEqual(config["vram_gb"],8)
        self.assertEqual(config["port"],8080)
        self.assertEqual(config["health_path"],"/health/ready")
        self.assertEqual(config["recipe"],"cloud_render/concat/gpu/Dockerfile")
        self.assertIn("Rust concat-cli",config["engine"])
        self.assertFalse(config["image_ready"])
        self.assertFalse(config["auto_upload"])
        self.assertEqual(self.receive()["state"],"ready")
        self.assertTrue(self.mgr.local("job-A").exists())

    def test_concurrency_change_preserves_connected_worker(self):
        self.mgr.save({"endpoint": "https://fixture.salad.cloud"})
        with self.mgr.connection() as con:
            self.mgr.put(con, "enabled", "1")
        with patch.object(self.mgr.production, "worker_factory") as factory:
            factory.return_value.timeout = 15
            result = self.mgr.save({
                "endpoint": "https://fixture.salad.cloud",
                "concurrency": 2
            })
        self.assertTrue(result["enabled"])
        self.assertEqual(result["concurrency"], 2)
        self.assertEqual(result["concurrency_sync"], "applied")
        factory.return_value.configure_concurrency.assert_called_once_with(2)
        self.mgr.save({"endpoint": "https://different.salad.cloud"})
        self.assertFalse(self.mgr.config()["enabled"])

    def test_receiver_token_rotates_and_denies_invalid(self):
        with self.assertRaises(HTTPException): self.mgr.token_required("")
        token = self.mgr.rotate_token()["receiver_token"]
        self.mgr.token_required(token)
        new_token = self.mgr.rotate_token()["receiver_token"]
        self.assertNotEqual(token,new_token)
        with self.assertRaises(HTTPException): self.mgr.token_required(token)

    def test_chunk_resume_is_idempotent_and_metadata_immutable(self):
        part = self.mgr.receive("job-Z",0,MP4[:20],name="Concat test.mp4",size=len(MP4),sha=self.sha)
        self.assertEqual(part["next_chunk"],1)
        self.assertEqual(self.mgr.receive("job-Z",0,MP4[:20],name="Concat test.mp4",size=len(MP4),sha=self.sha)["next_chunk"],1)
        with self.assertRaises(HTTPException): self.mgr.receive("job-Z",2,MP4[20:],name="Concat test.mp4",size=len(MP4),sha=self.sha)
        done = self.mgr.receive("job-Z",1,MP4[20:],name="Concat test.mp4",size=len(MP4),sha=self.sha)
        self.assertEqual(done["state"],"ready")

    def test_reject_traversal_and_bad_mp4(self):
        with self.assertRaises(HTTPException): self.mgr.local("../escape")
        with self.assertRaises(HTTPException): filename("bad.exe")
        with self.assertRaises(HTTPException): endpoint_url("http://127.0.0.1:8080")
        with self.assertRaises(HTTPException): endpoint_url("https://salad.cloud.evil.com")
        data = b"a" * len(MP4)
        with self.assertRaises(HTTPException):
            self.mgr.receive("bad-mp4",0,data,name="a.mp4",size=len(data),sha=hashlib.sha256(data).hexdigest())

    def test_manual_drive_upload_requires_remote_size_and_md5(self):
        self.receive()
        self.assertTrue(self.mgr.local("job-A").exists())
        with patch.object(self.mgr.pool,"submit"):
            self.mgr.queue("job-A")
        stat={"Size":len(MP4),"Hashes":{"MD5":self.md5}}
        with patch("cloud_render.shutil.which",return_value="/usr/bin/rclone"):
            with patch.object(self.mgr,"remote_stat",side_effect=[None,stat]):
                with patch.object(self.mgr,"execute",side_effect=lambda cmd, timeout=1200: "gdrive:\n" if cmd[1] == "listremotes" else ""):
                    self.mgr._upload("job-A")
        self.assertFalse(self.mgr.local("job-A").exists())
        self.assertEqual(self.mgr.listing()["items"][0]["state"],"delivered")

    def test_failed_remote_verification_preserves_original(self):
        self.receive()
        with patch.object(self.mgr.pool,"submit"):
            self.mgr.queue("job-A")
        stat={"Size":len(MP4),"Hashes":{"MD5":"0"*32}}
        with patch("cloud_render.shutil.which",return_value="/usr/bin/rclone"):
            with patch.object(self.mgr,"remote_stat",side_effect=[None,stat]):
                with patch.object(self.mgr,"execute",side_effect=lambda cmd, timeout=1200: "gdrive:\n" if cmd[1] == "listremotes" else ""):
                    self.mgr._upload("job-A")
        self.assertTrue(self.mgr.local("job-A").is_file())
        self.assertEqual(self.mgr.listing()["items"][0]["state"],"failed")

    def test_active_upload_cannot_be_renamed_or_deleted(self):
        self.receive()
        with patch.object(self.mgr.pool,"submit"):
            self.mgr.queue("job-A")
        with self.assertRaises(HTTPException): self.mgr.rename("job-A","other.mp4")
        with self.assertRaises(HTTPException): self.mgr.delete("job-A")
        self.assertTrue(self.mgr.local("job-A").exists())

    def test_auto_upload_only_new_jobs(self):
        self.receive("old")
        self.mgr.save({"auto_upload":True})
        with patch.object(self.mgr,"queue") as q:
            self.receive("new")
        q.assert_called_once_with("new")
        self.assertEqual({item["id"] for item in self.mgr.listing()["items"]}, {"old","new"})

    def test_candidate_policy_is_scoped(self):
        from precutover_policy import mutation_allowed
        self.assertTrue(mutation_allowed("POST","/api/cloud-render/media/a-123/drive",False))
        self.assertTrue(mutation_allowed("POST","/api/cloud-render/ingest/a-123/chunk",False))
        self.assertFalse(mutation_allowed("POST","/api/cloud-render/ingest/a-123/bypass",False))
        self.assertFalse(mutation_allowed("DELETE","/api/cloud-render/config",False))

    def test_scene_admin_gate_and_tabs_are_server_registered(self):
        app = (ROOT/"apps/scene-builder/part01/app.py").read_text()
        shell = (ROOT/"apps/scene-builder/part01/static/vps1_native_site_shell.js").read_text()
        html = (ROOT/"apps/scene-builder/part01/templates/cloud_render.html").read_text()
        self.assertIn('request.url.path.rstrip("/") == "/cloud-render"',app)
        self.assertIn('not account.get("is_admin")',app)
        self.assertIn('register_cloud_render(app, templates, DATA_DIR, require_admin, require_login)',app)
        self.assertIn('["Concat", "/concat-studio/concat/", "concat-render"]',shell)
        self.assertIn('["Concat Uploader", "/concat-studio/concat-uploader/", "concat-uploader"]',shell)
        self.assertEqual(html.count('data-tab="media"'),1)
        self.assertEqual(html.count('data-tab="gpu"'),1)
        self.assertIn('Auto Upload New Jobs',html)


if __name__ == "__main__": unittest.main()
