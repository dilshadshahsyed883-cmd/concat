"""Guarded Cloud Render web-only release tests (never access VPS)."""
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from production import cloud_render_drive_web_release as lane


class CloudRenderDriveReleaseTests(unittest.TestCase):
    def test_idle_failed_and_ready_media_are_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            db = pathlib.Path(d) / "cloud_render.sqlite3"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE files(state TEXT)")
                con.executemany("INSERT INTO files(state) VALUES(?)",
                                [("failed",), ("ready",), ("delivered",)])
            self.assertEqual(lane.check_idle(db), {"failed": 1, "ready": 1, "delivered": 1})
            with sqlite3.connect(db) as con:
                con.execute("INSERT INTO files(state) VALUES ('uploading')")
            with self.assertRaisesRegex(RuntimeError, "active incoming/upload"):
                lane.check_idle(db)

    def test_queued_and_receiving_block_web_restart(self):
        for state in ("queued", "receiving", "verifying"):
            with self.subTest(state=state):
                with tempfile.TemporaryDirectory() as d:
                    db = pathlib.Path(d) / "cloud_render.sqlite3"
                    with sqlite3.connect(db) as con:
                        con.execute("CREATE TABLE files(state TEXT)")
                        con.execute("INSERT INTO files(state) VALUES(?)", (state,))
                    with self.assertRaisesRegex(RuntimeError, "active incoming/upload"):
                        lane.check_idle(db)

    def test_active_production_project_blocks_scoped_release(self):
        with tempfile.TemporaryDirectory() as d:
            db = pathlib.Path(d) / "cloud_render.sqlite3"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE files(state TEXT)")
                con.execute("CREATE TABLE production_jobs(state TEXT)")
                con.execute("INSERT INTO production_jobs VALUES('Queued')")
            self.assertEqual(lane.check_idle(db), {})
            with sqlite3.connect(db) as con:
                con.execute("UPDATE production_jobs SET state='Rendering'")
            with self.assertRaisesRegex(RuntimeError, "Production has an active project"):
                lane.check_idle(db)

    def test_missing_or_corrupt_db_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            db = pathlib.Path(d) / "cloud_render.sqlite3"
            with self.assertRaisesRegex(RuntimeError, "database missing"):
                lane.check_idle(db)
            db.write_bytes(b"not sqlite")
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                lane.check_idle(db)

    def test_rclone_preflight_is_read_only_and_worker_scoped(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d)
            (home / ".config").mkdir()
            with patch.object(lane, "WORKER_HOME", home):
                with patch.object(lane.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="gdrive:\n", stderr="")) as spy:
                    lane.verify_rclone_context()
                argv = spy.call_args.args[0]
                self.assertEqual(argv[:5], ["runuser", "-u", "vps1scene", "--", "env"])
                self.assertIn("listremotes", argv)
                self.assertNotIn("copyto", argv)
                self.assertIn("HOME=" + str(home), argv)
                self.assertIn("XDG_CONFIG_HOME=" + str(home / ".config"), argv)
                with patch.object(lane.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="otherdrive:\n", stderr="")):
                    with self.assertRaisesRegex(RuntimeError, "no gdrive remote"):
                        lane.verify_rclone_context()

    def test_production_health_proof_checks_anonymous_redirect_without_following_it(self):
        requests = {
            ("/cloud-render/", "session=fixture", True): (200, b'Cloud Render data-tab="production" aria-selected="true" id="cr-concurrency"'),
            ("/api/cloud-render/production", "session=fixture", True): (200, b'{"items":[]}'),
            ("/static/cloud_render.js", "", True): (200, b'function loadProduction() {} document.getElementById("cr-concurrency");'),
            ("/api/cloud-render/config", "session=fixture", True): (200, b'{"concurrency":1}'),
            ("/api/cloud-render/production", "", False): (303, b""),
            ("/api/cloud-render/media", "session=fixture", True): (200, b'{"items":[]}'),
        }
        def request(path, cookie="", *, follow_redirects=True):
            status, body = requests[path, cookie, follow_redirects]
            return status, {}, body
        with patch("production.route_uploader_scene_https.scene_cookie", return_value="session=fixture"), \
             patch("production.scene_candidate_release._request", side_effect=request):
            lane.verify_ui(production=True)

    def test_exclusive_web_only_dispatch_and_rollback_markers(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        runtime = (root / "production/runtime.py").read_text()
        release = (root / "production/cloud_render_drive_web_release.py").read_text()
        workflow = (root / ".github/workflows/vps1-experiment-management-release.yml").read_text()
        self.assertIn("[vps1-cloud-render-drive-web-only]", runtime)
        self.assertIn("[vps1-cloud-render-scene-only]", runtime)
        self.assertIn("args.apps != ['scene-builder']", runtime)
        self.assertIn("install, rollback", runtime)
        self.assertIn("shutil.copytree(old, target", release)
        self.assertIn('RELATIVE = "part01/cloud_render.py"', release)
        self.assertIn("check_idle()", release)
        self.assertIn("verify_rclone_context()", release)
        self.assertIn("worker_pid() == pid", release)
        self.assertIn("atomic_link(CURRENT, old)", release)
        self.assertIn("AUTO_SCRIPT=OFFLINE_UNCHANGED", workflow)


if __name__ == "__main__":
    unittest.main()
