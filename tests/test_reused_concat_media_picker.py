"""Actual SQLite/filesystem/worker/API regressions in disposable owner storage.

The child process selects isolated Concat configuration before any runtime import.
All remote uploader methods are mocks that fail if cleanup attempts an upload.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

if os.getenv("CONCAT_MEDIA_TEST_CHILD") != "1":
    class IsolatedMediaTests(unittest.TestCase):
        def test_media_regressions(self):
            with tempfile.TemporaryDirectory() as temp:
                env = os.environ.copy()
                for key in ("YOUTUBE_UPLOAD_MANAGER_DATA_DIR", "LUM_GOOGLE_OAUTH_ROOT", "LUM_AUTO_ENGINE_V6_ROOT"):
                    env.pop(key, None)
                env.update(CONCAT_MEDIA_TEST_CHILD="1", LUM_ENGINE_INSTANCE="concat",
                           LUM_DATA_DIR=str(Path(temp) / "concat-media-tests"),
                           LUM_BASE_PATH="/concat-uploader", LUM_SUITE_AUTH="1",
                           CONCAT_WEB_IN_PROCESS="1", LUM_SESSION_SECRET="test-only-session-secret-at-least-thirty-two", LUM_COOKIE_SECURE="1",
                           PYTHONPATH=str(ROOT / "reused/uploader/part01"))
                result = subprocess.run([sys.executable, __file__, "-v"], env=env,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stderr)
else:
    import hashlib
    import sqlite3
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock, patch
    from lum_engine import concat_source as source, worker as worker_module
    from lum_engine.config import DATA_ROOT, STAGING_ROOT
    from lum_engine.project_store import ProjectStore
    from lum_engine.worker import VPSUploadWorker

    BYTES = b"\0\0\0\x14ftypisom" + b"video" * 100
    SHA = hashlib.sha256(BYTES).hexdigest()
    ORIGIN = "https://concat.scenebuilder.online"
    LINK = ORIGIN + "/api/cloud-render/media/video-one/file"

    class MediaTests(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory(dir=DATA_ROOT)
            self.root = Path(self.temp.name)
            self.cloud = self.root / "cloud"
            self.media = self.cloud / "owners" / hashlib.sha256(b"alice").hexdigest() / "media"
            self.media.mkdir(parents=True)
            self.file = self.media / "video-one.mp4"
            self.file.write_bytes(BYTES)
            self.media_db = self.media.parent / "cloud_render.sqlite3"
            with sqlite3.connect(self.media_db) as conn:
                conn.execute("CREATE TABLE files(id TEXT PRIMARY KEY,name TEXT,size INTEGER,sha256 TEXT,state TEXT,drive_path TEXT)")
                conn.execute("INSERT INTO files VALUES(?,?,?,?,?,?)", ("video-one", "<Name>.mp4", len(BYTES), SHA, "ready", "drive:precious"))
            STAGING_ROOT.mkdir(parents=True, exist_ok=True)
            self.stage = STAGING_ROOT / (self.root.name + ".mp4")
            self.stage.write_bytes(BYTES)
            self.store = ProjectStore(self.root / "projects.sqlite3")
            self.store.set_setting("delete_staged_after_verified", "0")
            self.worker = VPSUploadWorker.__new__(VPSUploadWorker)
            self.worker.store = self.store
            self.worker.db = Mock()
            self.worker.auth = Mock()
            self.worker.uploader = Mock()
            self.worker.uploader.process_queue.side_effect = AssertionError("cleanup attempted upload")
            self.worker.shutdown_event = threading.Event()
            self.env = patch.dict(os.environ, {"CONCAT_CLOUD_RENDER_ROOT": str(self.cloud)})
            self.env.start()

        def tearDown(self):
            self.env.stop()
            self.stage.unlink(missing_ok=True)
            self.temp.cleanup()

        def data(self, **overrides):
            return dict(dict(owner_username="alice", channel_db_id=1, title="Video",
                             source_mode="local_file", source_origin="concat_link",
                             staging_path=str(self.stage), source_fingerprint=SHA,
                             concat_job_id="video-one", source_file_name="source.mp4"), **overrides)

        def add(self, **overrides):
            return self.store.create_project(self.data(**overrides))

        def available(self, owner="alice"):
            ids, sha = self.store.concat_reserved_sources(owner)
            return source.list_available_concat_media(owner, cloud_root=self.cloud,
                        origin=ORIGIN, reserved_ids=ids, reserved_sha=sha)

        def verified(self, pid):
            self.store.update(pid, remote_verified_at="2026-11-22T00:00:00+00:00",
                              youtube_video_id="youtube123", youtube_url="https://youtu.be/youtube123")
            return self.store.get(pid)

        def due(self, pid):
            self.store.update(pid, concat_cleanup_retry_at="")

        def cleanup(self, pid):
            self.worker._process(pid)

        def test_ready_only_owner_mp4_and_reserved_fingerprint(self):
            self.assertEqual([x["id"] for x in self.available()], ["video-one"])
            with self.assertRaises(source.ConcatSourceError):
                self.available("bob")
            for state in ("receiving", "queued", "uploading", "verified", "delivered", "failed"):
                with sqlite3.connect(self.media_db) as con:
                    con.execute("UPDATE files SET state=?", (state,))
                self.assertEqual(self.available(), [])
            with sqlite3.connect(self.media_db) as con:
                con.execute("UPDATE files SET state='ready'")
            self.add(concat_job_id="legacy-other-id")
            self.assertEqual(self.available(), [])

        def test_missing_symlink_invalid_header_and_malformed_size_hidden(self):
            self.file.write_bytes(b"bad!" + BYTES[4:].replace(b"ftyp", b"oops", 1))
            self.assertEqual(self.available(), [])
            self.file.unlink()
            self.assertEqual(self.available(), [])
            self.file.symlink_to(self.stage)
            self.assertEqual(self.available(), [])
            self.file.unlink()
            self.file.write_bytes(BYTES)
            with sqlite3.connect(self.media_db) as con:
                con.execute("UPDATE files SET size='broken'")
            self.assertEqual(self.available(), [])

        def test_all_queue_states_remote_ambiguity_and_history_filter(self):
            pid = self.add()
            for state in ("QUEUED", "WAITING_SLOT", "SCHEDULED", "SOURCE_READY", "DOWNLOADING",
                          "UPLOAD_QUEUED", "UPLOADING", "VERIFYING_REMOTE", "CLEANING", "PAUSED", "COMPLETED"):
                self.store.update(pid, status=state)
                self.assertEqual(self.available(), [], state)
            self.store.archive_terminal(pid, "alice")
            self.assertEqual(self.available(), [])
            for state in ("FAILED", "CANCELLED"):
                self.store.update(pid, status=state)
                self.assertEqual(len(self.available()), 1)
                for field, value in (("core_video_id", 17), ("youtube_video_id", "remote"), ("remote_verified_at", "receipt"), ("concat_upload_started", 1)):
                    self.store.update(pid, **{field: value})
                    self.assertEqual(self.available(), [])
                    self.store.update(pid, **{field: 0 if field == "concat_upload_started" else None})

        def test_atomic_cross_channel_concurrent_admission_and_case_owner(self):
            barrier = threading.Barrier(8)
            def submit(channel):
                barrier.wait()
                try:
                    return self.add(channel_db_id=channel, owner_username="Alice" if channel % 2 else "alice")
                except ValueError:
                    return None
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(submit, range(1, 9)))
            self.assertEqual(sum(x is not None for x in results), 1)
            self.assertIsInstance(self.add(owner_username="bob", channel_db_id=9), int)

        def test_safe_retry_same_channel_and_old_retry_rejected(self):
            old = self.add()
            self.store.update(old, status="FAILED")
            new = self.add()
            with self.assertRaisesRegex(ValueError, "already"):
                self.store.retry(old)
            self.store.update(new, status="CANCELLED")
            self.assertTrue(self.store.retry(old))
            self.assertEqual(self.store.get(old)["status"], "QUEUED")

        def test_core_linked_failed_and_remote_cancelled_never_readmitted(self):
            pid = self.add()
            for state in ("FAILED", "CANCELLED"):
                self.store.update(pid, status=state, core_video_id=7)
                with self.assertRaisesRegex(ValueError, "already"):
                    self.add(channel_db_id=2)

        def test_verified_deletes_only_exact_original_preserves_receipt_history_and_drive(self):
            pid = self.add()
            self.verified(pid)
            self.cleanup(pid)
            self.assertFalse(self.file.exists())
            self.assertTrue(self.stage.exists(), "canonical cleanup ignores staged-delete setting")
            row = self.store.get(pid)
            self.assertEqual(row["status"], "COMPLETED")
            self.assertEqual(row["youtube_video_id"], "youtube123")
            self.assertTrue(row["cleanup_verified_at"])
            self.assertEqual(row["concat_cleanup_pending"], 0)
            self.assertTrue(self.store.events(owner="alice"))
            self.cleanup(pid)  # idempotent
            self.worker.uploader.process_queue.assert_not_called()
            self.worker.db.assert_not_called()

        def test_unverified_failed_or_remote_id_alone_never_delete(self):
            pid = self.add()
            for fields in ({"status": "FAILED"}, {"status": "VERIFYING_REMOTE", "youtube_video_id": "remote"}):
                self.store.update(pid, **fields)
                with self.assertRaisesRegex(RuntimeError, "independent YouTube"):
                    self.worker._finish_verified(pid, self.store.get(pid), self.stage)
                self.assertTrue(self.file.exists())
            self.worker._retry_concat_cleanup()
            self.assertTrue(self.file.exists())

        def test_cleanup_failure_restart_and_backoff_never_reupload(self):
            pid = self.add()
            self.verified(pid)
            with patch.object(worker_module, "delete_verified_concat_media", side_effect=OSError("disk denied")):
                with self.assertRaisesRegex(RuntimeError, "needs retry"):
                    self.cleanup(pid)
            self.assertTrue(self.file.exists())
            self.assertEqual(self.store.pending_concat_cleanup(), [])
            self.store = ProjectStore(self.store.path)
            self.worker.store = self.store
            self.due(pid)
            self.worker._retry_concat_cleanup()
            self.assertFalse(self.file.exists())
            self.assertEqual(self.store.get(pid)["status"], "COMPLETED")
            self.worker.uploader.process_queue.assert_not_called()

        def test_staged_cleanup_failure_also_gets_cleanup_only_retry(self):
            pid = self.add()
            self.verified(pid)
            self.store.set_setting("delete_staged_after_verified", "1")
            with patch.object(worker_module, "cleanup_project_staging", side_effect=OSError("staged failure")):
                with self.assertRaisesRegex(RuntimeError, "retry cleanup only"):
                    self.cleanup(pid)
            self.assertTrue(self.file.exists())
            self.due(pid)
            self.worker._retry_concat_cleanup()
            self.assertFalse(self.file.exists())
            self.assertFalse(self.stage.exists())

        def test_deferred_cleanup_eventually_runs_and_two_verified_jobs_do_not_deadlock(self):
            pid = self.add()
            # Existing historical duplicates must migrate without dropping history.
            with self.store.connect() as con:
                con.execute("DROP TRIGGER tr_concat_unique_upload_source")
            other = self.add(channel_db_id=2)
            self.store.init_db()
            self.verified(pid)
            self.cleanup(pid)
            self.assertTrue(self.file.exists())
            self.assertTrue(self.store.get(pid)["concat_cleanup_pending"])
            self.assertIsNone(self.store.get(pid)["cleanup_verified_at"])
            self.verified(other)
            self.cleanup(other)
            self.assertFalse(self.file.exists())
            self.due(pid)
            self.worker._retry_concat_cleanup()
            self.assertEqual(self.store.get(pid)["concat_cleanup_pending"], 0)

        def test_drive_busy_and_changed_checksum_preserved_until_retry(self):
            pid = self.add()
            self.verified(pid)
            for state in ("queued", "uploading", "verified", "receiving"):
                with sqlite3.connect(self.media_db) as con:
                    con.execute("UPDATE files SET state=?", (state,))
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    self.cleanup(pid)
                self.assertTrue(self.file.exists())
            with sqlite3.connect(self.media_db) as con:
                con.execute("UPDATE files SET state='ready'")
            self.file.write_bytes(BYTES[:-1] + b"x")
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                self.cleanup(pid)
            self.assertTrue(self.file.exists())
            self.file.write_bytes(BYTES)
            self.due(pid)
            self.worker._retry_concat_cleanup()
            self.assertFalse(self.file.exists())

        def test_cleanup_rejects_symlink_traversal_wrong_owner_and_checksum(self):
            for job, owner, sha in (("../video-one", "alice", SHA), ("video-one", "bob", SHA), ("video-one", "alice", "f"*64)):
                with self.assertRaises(source.ConcatSourceError):
                    source.delete_verified_concat_media(job, owner, sha, cloud_root=self.cloud)
                self.assertTrue(self.file.exists())
            self.file.unlink()
            self.file.symlink_to(self.stage)
            with self.assertRaisesRegex(source.ConcatSourceError, "symlink"):
                source.delete_verified_concat_media("video-one", "alice", SHA, cloud_root=self.cloud)
            self.assertTrue(self.stage.exists())

        def test_missing_file_after_unlink_crash_reconciles_row(self):
            pid = self.add()
            self.verified(pid)
            self.file.unlink()
            self.cleanup(pid)
            with sqlite3.connect(self.media_db) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

        def test_api_and_manual_submission_real_owner_channels_csrf_and_staging(self):
            import re
            from fastapi.testclient import TestClient
            from lum_engine import standalone_app as web
            from app.auth import Principal
            from app.main import create_app
            from app.modules.uploader import gateway
            cid = web.studio_tenants.create_channel("alice", "UC" + "a" * 22, "Alice")
            cid2 = web.studio_tenants.create_channel("alice", "UC" + "b" * 22, "Alice second")
            web.studio_tenants.create_group("alice", "Group A", [cid, cid2])
            application = create_app()
            application.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=True)
            with patch.dict(os.environ, {"CONCAT_PUBLIC_ORIGIN": ORIGIN, "CONCAT_UPLOADER_ENABLED": "1"}), \
                    patch.object(web, "store", self.store), \
                    patch.object(gateway, "_import_reused_app", return_value=web.app):
                client = TestClient(application, base_url=ORIGIN)
                page = client.get("/concat-uploader/app?tab=add_video")
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn('data-available-media-url="/concat-uploader/api/concat/available-media"', page.text)
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
                endpoint = "/concat-uploader/api/concat/available-media"
                response = client.get(endpoint, headers={"X-Auth-User": "bob"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["items"][0]["id"], "video-one")
                self.assertIn("no-store", response.headers["cache-control"])
                form = dict(csrf_token=csrf, title="Manual", channel_db_id=cid,
                            group_name="Group A", source_mode="concat_link", concat_video_link=LINK)
                response = client.post("/concat-uploader/actions/add-video", data={**form, "csrf_token": "bad"}, follow_redirects=False)
                self.assertEqual(response.status_code, 403)
                response = client.post("/concat-uploader/actions/add-video", data=form, follow_redirects=False)
                self.assertEqual(response.status_code, 303, response.text)
                self.assertIn("message=", response.headers["location"])
                self.assertEqual(client.get(endpoint).json()["items"], [])
                staged = list(STAGING_ROOT.glob("concat_*.mp4"))
                form["channel_db_id"] = cid2
                duplicate = client.post("/concat-uploader/actions/add-video", data=form, follow_redirects=False)
                self.assertIn("already", duplicate.headers["location"])
                self.assertEqual(list(STAGING_ROOT.glob("concat_*.mp4")), staged)
                application.state.identity_resolver = lambda _: Principal("bob", is_active=True, concat_granted=True)
                # Storage is deliberately missing for Bob; never return Alice's list.
                self.assertEqual(client.get(endpoint).status_code, 503)
                application.state.identity_resolver = None
                self.assertEqual(client.get(endpoint, headers={"X-Auth-User": "alice"}).status_code, 401)
                for file in staged:
                    file.unlink()
                client.close()

        def test_worker_upload_api_verification_cleanup_and_failure_gating(self):
            pid = self.add()
            self.worker.stop_current_event = threading.Event()
            self.worker.db.video_id_for_staged_path.return_value = 0
            self.worker.db.add_video.return_value = 21
            core = dict(status="QUEUED", youtube_channel_id="channel-a", channel_token_file_path="token")
            self.worker.db.get_video.side_effect = lambda _: dict(core)
            def uploaded(**kwargs):
                self.assertEqual(kwargs["only_video_id"], 21)
                core.update(status="UPLOADED", youtube_video_id="remote")
            self.worker.uploader.process_queue.side_effect = uploaded
            lookup = self.worker.auth.build_youtube_service.return_value.videos.return_value.list.return_value.execute
            lookup.return_value = {"items": [{"id": "remote", "snippet": {"channelId": "channel-a"}, "status": {"uploadStatus": "failed"}}]}
            with patch.object(worker_module, "StudioTenants"):
                with self.assertRaisesRegex(RuntimeError, "successful"):
                    self.worker._process(pid)
                self.assertTrue(self.file.exists())
                self.assertFalse(self.store.get(pid)["remote_verified_at"])
                lookup.return_value["items"][0]["status"]["uploadStatus"] = "processed"
                self.worker._process(pid)
            self.assertFalse(self.file.exists())
            self.assertEqual(self.store.get(pid)["status"], "COMPLETED")
            self.assertTrue(self.store.get(pid)["remote_verified_at"])
            self.worker.uploader.process_queue.assert_called_once()
            self.worker._process(pid)
            self.worker.uploader.process_queue.assert_called_once()

        def test_upload_intent_reserves_failed_job_even_without_core_linkage(self):
            pid = self.add()
            self.store.update(pid, concat_upload_started=1, status="FAILED")
            self.assertEqual(self.available(), [])
            with self.assertRaisesRegex(ValueError, "already"):
                self.add(channel_db_id=2)

        def test_cleanup_retry_does_not_mutate_core_queue(self):
            from lum_engine import standalone_app as web
            pid = self.add()
            self.verified(pid)
            self.store.update(pid, status="FAILED", core_video_id=19)
            with patch.object(web, "store", self.store), patch.object(web, "db") as db:
                web._copyright_retry_owned_project(pid, self.store.get(pid))
                db.queue_failed_for_retry.assert_not_called()
                db.get_video.assert_not_called()
            self.cleanup(pid)
            self.assertFalse(self.file.exists())

        def test_independent_youtube_api_requires_success_and_channel_match(self):
            from lum_engine.remote_verify import verify_youtube_video
            db = Mock()
            db.get_video.return_value = dict(youtube_video_id="remote", youtube_channel_id="expected",
                                            channel_token_file_path="owner-token")
            auth = Mock()
            lookup = auth.build_youtube_service.return_value.videos.return_value.list.return_value.execute
            for state in ("failed", "rejected", "deleted", "", "uploading"):
                lookup.return_value = {"items": [{"id": "remote", "snippet": {"channelId": "expected"}, "status": {"uploadStatus": state}}]}
                with self.assertRaises(RuntimeError):
                    verify_youtube_video(auth, db, 1, strict=True)
            for state in ("uploaded", "processed"):
                lookup.return_value["items"][0]["status"]["uploadStatus"] = state
                self.assertTrue(verify_youtube_video(auth, db, 1, strict=True)["verified"])
            lookup.return_value["items"][0]["snippet"]["channelId"] = "other"
            with self.assertRaises(RuntimeError):
                verify_youtube_video(auth, db, 1, strict=True)

        def test_legacy_fingerprint_reservation_and_repeatable_schema_upgrade(self):
            pid = self.add()
            self.verified(pid)
            with self.store.connect() as con:
                con.execute("DROP TRIGGER tr_concat_unique_upload_source")
                con.execute("DROP TRIGGER tr_concat_retry_source")
                con.execute("UPDATE projects SET concat_job_id='' WHERE id=?", (pid,))
                con.execute("ALTER TABLE projects DROP COLUMN concat_job_id")
                con.execute("ALTER TABLE projects DROP COLUMN concat_cleanup_pending")
                con.execute("ALTER TABLE projects DROP COLUMN concat_cleanup_retry_at")
            for _ in range(2):
                self.store = ProjectStore(self.store.path)
            self.worker.store = self.store
            self.assertEqual(self.available(), [])
            with self.assertRaisesRegex(ValueError, "already"):
                self.add(channel_db_id=2)
            self.cleanup(pid)
            self.assertTrue(self.file.exists(), "legacy identity must never be guessed")
            self.assertEqual(self.store.get(pid)["youtube_video_id"], "youtube123")

if __name__ == "__main__":
    unittest.main()
