"""Metadata-only sheet configuration and sync through the real authenticated gateway."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

if os.getenv("CONCAT_METADATA_TEST_CHILD") != "1":
    class IsolatedMetadataTests(unittest.TestCase):
        def test_metadata_regressions(self):
            with tempfile.TemporaryDirectory() as root:
                env = os.environ.copy()
                for key in ("YOUTUBE_UPLOAD_MANAGER_DATA_DIR", "LUM_GOOGLE_OAUTH_ROOT", "LUM_AUTO_ENGINE_V6_ROOT"):
                    env.pop(key, None)
                env.update(CONCAT_METADATA_TEST_CHILD="1", LUM_ENGINE_INSTANCE="concat",
                           LUM_DATA_DIR=str(Path(root) / "concat-metadata-tests"),
                           LUM_BASE_PATH="/concat-uploader", LUM_SUITE_AUTH="1",
                           CONCAT_WEB_IN_PROCESS="1", LUM_COOKIE_SECURE="1",
                           LUM_SESSION_SECRET="metadata-test-session-secret-over-thirty-two",
                           PYTHONPATH=str(ROOT / "reused/uploader/part01"))
                result = subprocess.run([sys.executable, __file__, "-v"], env=env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stderr)
else:
    import hashlib
    import re
    import sqlite3
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from app.auth import Principal
    from app.main import create_app
    from app.modules.uploader import gateway
    from lum_engine import standalone_app as web
    from lum_engine.concat_sheet import metadata_sheet_id, metadata_for_video
    from lum_engine.config import DATA_ROOT
    from lum_engine.project_store import ProjectStore

    SHEET = "a" * 30
    SECOND_SHEET = "b" * 30
    ORIGIN = "https://concat.scenebuilder.online"
    LINK = ORIGIN + "/api/cloud-render/media/selected-video/file"
    SYNC = "/concat-uploader/api/concat/metadata"
    SAVE = "/concat-uploader/actions/concat/metadata-sheet"

    class MetadataTests(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory(dir=DATA_ROOT)
            self.root = Path(self.temp.name)
            self.cloud = self.root / "cloud"
            self.media = self.cloud / "owners" / hashlib.sha256(b"alice").hexdigest() / "media"
            self.media.mkdir(parents=True)
            self.content = b"\0\0\0\x14ftypisom" + b"video" * 100
            self.sha = hashlib.sha256(self.content).hexdigest()
            (self.media / "selected-video.mp4").write_bytes(self.content)
            self.db_path = self.media.parent / "cloud_render.sqlite3"
            with sqlite3.connect(self.db_path) as con:
                con.execute("CREATE TABLE files(id TEXT PRIMARY KEY,name TEXT,size INTEGER,sha256 TEXT,state TEXT)")
                con.execute("INSERT INTO files VALUES(?,?,?,?,?)", ("selected-video", "u1.mp4", len(self.content), self.sha, "ready"))
            self.store = ProjectStore(self.root / "queue.sqlite3")
            self.env = patch.dict(os.environ, {"CONCAT_PUBLIC_ORIGIN": ORIGIN, "CONCAT_CLOUD_RENDER_ROOT": str(self.cloud), "CONCAT_UPLOADER_ENABLED": "1"})
            self.env.start()
            self.app = create_app()
            self.owner = "alice"
            self.app.state.identity_resolver = lambda _: Principal(self.owner, is_active=True, concat_granted=True)
            self.patches = [patch.object(web, "store", self.store), patch.object(gateway, "_import_reused_app", return_value=web.app)]
            for p in self.patches:
                p.start()
            self.client = TestClient(self.app, base_url=ORIGIN)
            page = self.client.get("/concat-uploader/app?tab=sheets")
            self.assertEqual(page.status_code, 200, page.text)
            self.csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]

        def tearDown(self):
            self.client.close()
            for p in reversed(self.patches):
                p.stop()
            self.env.stop()
            self.temp.cleanup()

        def save(self, sheet=SHEET, **extra):
            return self.client.post(SAVE, data={"csrf_token": self.csrf, "sheet_url": sheet, **extra})

        def sync(self, link=LINK, **extra):
            return self.client.post(SYNC, data={"csrf_token": self.csrf, "concat_video_link": link, **extra})

        def test_google_only_urls_and_exact_name_matching(self):
            for value in (SHEET, f"https://docs.google.com/spreadsheets/d/{SHEET}/edit?usp=sharing#gid=55"):
                self.assertEqual(metadata_sheet_id(value), SHEET)
            for value in ("file:///etc/passwd", "https://evil.example/spreadsheets/d/" + SHEET,
                          "http://docs.google.com/spreadsheets/d/" + SHEET, "https://docs.google.com.evil.test/spreadsheets/d/" + SHEET):
                with self.assertRaises(ValueError):
                    metadata_sheet_id(value)
            rows = [{"Video Name": " U1.MP4 ", "Title": "Title", "Description": "<b>plain text</b>", "schedule": "2099"}]
            self.assertEqual(metadata_for_video(rows, "u1"), {"title": "Title", "description": "<b>plain text</b>"})
            for name in ("u10", "u1-other", "../u1.mp4"):
                with self.assertRaises(ValueError):
                    metadata_for_video(rows, name)

        def test_one_sheet_replacement_persists_and_owner_isolation(self):
            self.assertEqual(self.save().status_code, 200)
            self.assertEqual(self.save(SECOND_SHEET).status_code, 200)
            restarted = ProjectStore(self.store.path)
            self.assertEqual(restarted.concat_metadata_sheet("alice"), SECOND_SHEET)
            self.assertEqual(restarted.concat_metadata_sheet("ALICE"), SECOND_SHEET)
            self.assertEqual(restarted.concat_metadata_sheet("bob"), "")
            self.owner = "bob"
            self.assertEqual(self.save().status_code, 200)
            self.assertEqual(restarted.concat_metadata_sheet("alice"), SECOND_SHEET)
            self.assertEqual(restarted.concat_metadata_sheet("bob"), SHEET)
            with restarted.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM concat_metadata_sheets").fetchone()[0], 2)

        def test_button_placement_and_no_automatic_sheet_fetch(self):
            with patch.object(web, "tenant_fetch_sheet_csv") as fetch:
                sheets = self.client.get("/concat-uploader/app?tab=sheets").text
                upload = self.client.get("/concat-uploader/app?tab=add_video").text
                self.assertIn("Add Metadata Sheet", sheets)
                self.assertIn("data-concat-metadata-sheet-form", sheets)
                self.assertNotIn("data-concat-metadata-sync", sheets)
                self.assertIn("data-concat-metadata-sync", upload)
                self.assertNotIn("data-concat-metadata-sheet-form", upload)
                self.save()
                fetch.assert_not_called()
                self.assertIn("Edit Metadata Sheet", self.client.get("/concat-uploader/app?tab=sheets").text)

        def test_success_returns_only_title_description_and_never_changes_queue_channels(self):
            self.save()
            rows = [{"video_name": "u1", "title": "New title", "description": "Line 1\nLine 2", "category_id": "99", "schedule_time": "2099-01-01", "channel": "other"}]
            channels = web.db.list_sheet_sources()
            with patch.object(web, "tenant_fetch_sheet_csv", return_value=rows) as fetch, \
                 patch.object(web.store, "create_project", side_effect=AssertionError("sync must not queue")), \
                 patch.object(web.studio_tenants, "sync_owned_sheet_rows", side_effect=AssertionError("metadata must not change channels")):
                result = self.sync()
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json(), {"ok": True, "video_link": LINK, "title": "New title", "description": "Line 1\nLine 2"})
                fetch.assert_called_once_with(SHEET, None)
                self.assertIn("no-store", result.headers["cache-control"])
            self.assertEqual(self.store.list(), [])
            self.assertEqual(web.db.list_sheet_sources(), channels)

        def test_missing_sheet_missing_match_duplicates_and_invalid_lengths(self):
            self.assertEqual(self.sync().status_code, 409)
            self.save()
            for rows in ([], [{"video_name": "u2", "title": "Other", "description": "Other"}],
                         [{"video_name": "u1", "title": "One", "description": "D"}, {"video_name": "U1.mp4", "title": "Two", "description": "D"}],
                         [{"video_name": "u1", "title": "", "description": "D"}],
                         [{"video_name": "u1", "title": "T"*101, "description": "D"}],
                         [{"video_name": "u1", "title": "Title"}],
                         [{"video_name": "u1", "title": "Title", "description": "D"*5001}]):
                with patch.object(web, "tenant_fetch_sheet_csv", return_value=rows):
                    self.assertEqual(self.sync().status_code, 400)
            with patch.object(web, "tenant_fetch_sheet_csv", return_value=[{"video_name": "u1", "title": "T"*100, "description": "D"*5000}]):
                self.assertEqual(self.sync().status_code, 200)

        def test_csrf_untrusted_headers_anonymous_and_non_concat_rejected(self):
            self.assertEqual(self.save(csrf_token="invalid").status_code, 403)
            self.save()
            self.assertEqual(self.sync(csrf_token="invalid").status_code, 403)
            self.app.state.identity_resolver = None
            self.assertEqual(self.client.post(SAVE, data={"csrf_token": self.csrf, "sheet_url": SHEET}, headers={"X-Auth-User": "alice"}).status_code, 401)
            self.app.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=False)
            self.assertEqual(self.sync().status_code, 403)
            # Direct child identity is trusted only behind the authenticated outer gateway.
            with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "copyright"}):
                direct = TestClient(web.app, base_url=ORIGIN, headers={"X-Auth-User": "alice", "X-App-Access": "all"})
                with patch.object(web, "require_csrf"):
                    self.assertEqual(direct.post(SYNC, data={"csrf_token": "test", "concat_video_link": LINK}).status_code, 404)
                direct.close()

        def test_other_owners_missing_busy_and_queued_media_not_fetched(self):
            self.save()
            with patch.object(web, "tenant_fetch_sheet_csv") as fetch:
                self.owner = "bob"
                self.save()
                self.assertEqual(self.sync().status_code, 400)
                self.owner = "alice"
                for state in ("queued", "uploading", "receiving", "delivered"):
                    with sqlite3.connect(self.db_path) as con:
                        con.execute("UPDATE files SET state=?", (state,))
                    self.assertEqual(self.sync().status_code, 400)
                with sqlite3.connect(self.db_path) as con:
                    con.execute("UPDATE files SET state='ready'")
                with self.store.connect() as con:
                    # An existing upload receipt reserves this source.
                    con.execute("INSERT INTO projects(owner_username,drive_url,title,channel_db_id,status,created_at,updated_at,source_origin,source_fingerprint,concat_job_id) VALUES('alice','','old',1,'COMPLETED','','','concat_link',?,'selected-video')", (self.sha,))
                self.assertEqual(self.sync().status_code, 400)
                fetch.assert_not_called()

        def test_fetch_error_and_changed_configuration_do_not_return_stale_metadata(self):
            self.save()
            with patch.object(web, "tenant_fetch_sheet_csv", side_effect=OSError("private-path-secret")):
                response = self.sync()
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("private-path-secret", response.text)
            def changed(*args):
                self.store.save_concat_metadata_sheet("alice", SECOND_SHEET)
                return [{"video_name": "u1", "title": "old sheet", "description": "D"}]
            with patch.object(web, "tenant_fetch_sheet_csv", side_effect=changed):
                self.assertEqual(self.sync().status_code, 409)

if __name__ == "__main__":
    unittest.main()
