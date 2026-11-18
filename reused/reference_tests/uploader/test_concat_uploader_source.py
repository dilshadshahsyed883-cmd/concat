"""Focused Concat Uploader security and scheduling tests (stdlib only)."""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "apps/copyright-uploader/part01/lum_engine"


def module(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


SOURCE = module("concat_source")
SCHEDULE = module("concat_schedule")
import sys
sys.path.insert(0, str(ENGINE.parent))
from lum_engine.concat_sheet import preview_sheet_videos

VIDEO_ID = "abcde12345"
LINK = f"https://app.scenebuilder.online/api/cloud-render/media/{VIDEO_ID}/file"
BYTES = b"\x00\x00\x00\x14ftypisom" + (b"verified-frame" * 100)


class ConcatUploaderSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cloud = root / "cloud-render"
        self.media = self.cloud / "media"
        self.media.mkdir(parents=True)
        self.stage = root / "isolated-staging"
        self.sha = hashlib.sha256(BYTES).hexdigest()
        (self.media / (VIDEO_ID + ".mp4")).write_bytes(BYTES)
        self.db = self.cloud / "cloud_render.sqlite3"
        with sqlite3.connect(self.db) as con:
            con.execute("""CREATE TABLE files (
                id TEXT, name TEXT, size INTEGER, sha256 TEXT,
                state TEXT, owner_username TEXT)""")
            con.execute("INSERT INTO files VALUES(?,?,?,?,?,?)", (
                VIDEO_ID, "completed.mp4", len(BYTES), self.sha, "ready", "owner1"))

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, link=LINK, owner="owner1"):
        return SOURCE.stage_concat_link(link, owner, self.stage, cloud_root=self.cloud)

    def test_verified_owned_video_staged_with_checksum(self):
        staged, filename, count, digest = self.submit()
        self.assertEqual(filename, "completed.mp4")
        self.assertEqual(count, len(BYTES))
        self.assertEqual(digest, self.sha)
        self.assertEqual(Path(staged).read_bytes(), BYTES)

    def test_private_copy_does_not_change_when_cloud_original_is_rewritten(self):
        staged, _, _, _ = self.submit()
        stage_file = Path(staged)
        original = self.media / (VIDEO_ID + ".mp4")
        original.write_bytes(BYTES.replace(b"verified-frame", b"changed-frame"))
        self.assertEqual(stage_file.read_bytes(), BYTES)
        self.assertNotEqual(original.read_bytes(), BYTES)
        self.assertEqual(stage_file.stat().st_nlink, 1)
        self.assertEqual(original.stat().st_nlink, 1)

    def test_other_owner_denied_even_if_admin_like_username(self):
        for username in ("owner2", "admin"):
            with self.subTest(username=username):
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    self.submit(owner=username)

    def test_legacy_media_without_owner_column_denied(self):
        with sqlite3.connect(self.db) as con:
            con.execute("DROP TABLE files")
            con.execute("CREATE TABLE files (id TEXT, name TEXT, size INTEGER, sha256 TEXT, state TEXT)")
            con.execute("INSERT INTO files VALUES(?,?,?,?,?)",
                        (VIDEO_ID, "video.mp4", len(BYTES), self.sha, "ready"))
        with self.assertRaisesRegex(ValueError, "isolation"):
            self.submit()

    def test_malformed_or_external_urls_never_requested(self):
        rejected = [
            "http://app.scenebuilder.online/api/cloud-render/media/abcde12345/file",
            "https://evil.example/api/cloud-render/media/abcde12345/file",
            "https://app.scenebuilder.online.evil.example/api/cloud-render/media/abcde12345/file",
            "https://app.scenebuilder.online/api/cloud-render/media/../file",
            LINK + "?download=1",
            LINK + "#foo",
            LINK.replace("/media/", "/media/%61"),
            "file:///etc/passwd",
            "/var/lib/cloud-render/media/a.mp4",
        ]
        for link in rejected:
            with self.subTest(link=link), self.assertRaises(ValueError):
                self.submit(link)

    def test_unverified_state_denied(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE files SET state='uploading'")
        with self.assertRaisesRegex(ValueError, "verified"):
            self.submit()

    def test_checksum_mismatch_denied_and_no_staging_leak(self):
        (self.media / (VIDEO_ID + ".mp4")).write_bytes(BYTES + b"tamper")
        with self.assertRaises(ValueError):
            self.submit()
        self.assertEqual(list(self.stage.glob("*.mp4")), [])

    def test_symlink_media_denied(self):
        source = self.media / (VIDEO_ID + ".mp4")
        source.unlink()
        source.symlink_to(self.db)
        with self.assertRaises(ValueError):
            self.submit()

    def test_missing_cloud_root_denied(self):
        with self.assertRaises(ValueError):
            SOURCE.stage_concat_link(LINK, "owner1", self.stage, cloud_root=self.cloud / "missing")


class ConcatSheetLinkTests(unittest.TestCase):
    def test_optional_sheet_mapping_does_not_queue_or_fetch_video(self):
        rows = [
            {"concat_video_link": LINK, "title": "Video A", "description": "About A"},
            {"concat_video_link": "https://evil.example/video.mp4", "title": "Bad"},
            {"video_link": LINK.replace(VIDEO_ID, "otherjob"), "video_title": "Video B"},
        ]
        preview = preview_sheet_videos(rows)
        self.assertEqual(len(preview), 2)
        self.assertEqual(preview[0]["title"], "Video A")
        self.assertEqual(preview[1]["title"], "Video B")
        self.assertNotIn("staging_path", preview[0])


class ConcatSchedulingTests(unittest.TestCase):
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    def test_blank_means_no_schedule(self):
        self.assertEqual(SCHEDULE.concat_publish_at("", "Asia/Karachi", "private", now=self.now), "")

    def test_pk_timezone_to_utc(self):
        result = SCHEDULE.concat_publish_at("2026-11-19T20:00", "Asia/Karachi", "public", now=self.now)
        self.assertEqual(result, "2026-11-19T15:00:00Z")

    def test_unlisted_or_private_schedule_rejected(self):
        for value in ("unlisted", "private"):
            with self.assertRaises(ValueError):
                SCHEDULE.concat_publish_at("2026-11-19T20:00", "UTC", value, now=self.now)

    def test_past_and_immediate_schedule_rejected(self):
        with self.assertRaises(ValueError):
            SCHEDULE.concat_publish_at("2026-11-18T12:04", "UTC", "public", now=self.now)

    def test_invalid_timezone_denied(self):
        with self.assertRaises(ValueError):
            SCHEDULE.concat_publish_at("2026-11-19T20:00", "../secret", "public", now=self.now)


if __name__ == "__main__":
    unittest.main()
