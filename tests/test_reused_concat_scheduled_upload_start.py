"""Concat upload START scheduling regression tests (no YouTube/network access)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPLOADER = ROOT / "reused/uploader/part01"


class ConcatScheduledUploadStartTests(unittest.TestCase):
    def test_timezone_validation_dst_and_utc(self):
        script = r"""
from datetime import datetime, timezone
from lum_engine.concat_schedule import concat_upload_at

clock = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
assert concat_upload_at("", "Asia/Karachi", now=clock) == ""
assert concat_upload_at("2026-11-19T07:30", "Asia/Karachi", now=clock) == "2026-11-19T02:30:00Z"
assert concat_upload_at("2026-11-19T07:30", "UTC", now=clock) == "2026-11-19T07:30:00Z"
for raw, zone in (
    ("2026-11-19T04:00", "Invalid/Timezone"),
    ("2026-11-19T05:00", "Asia/Karachi"),  # selected time is now
    ("2026-03-29T01:30", "Europe/London"),  # spring DST gap
    ("2026-10-25T01:30", "Europe/London"),  # autumn DST ambiguity
):
    try:
        concat_upload_at(raw, zone, now=clock)
    except ValueError:
        pass
    else:
        raise AssertionError((raw, zone))
print("PASS schedule timezone and DST")
"""
        self._isolated(script)

    def test_automatic_due_jobs_manual_isolation_and_persistent_pause(self):
        script = r"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from lum_engine.config import DATA_ROOT, STAGING_ROOT
from lum_engine.project_store import ProjectStore

STAGING_ROOT.mkdir(parents=True, exist_ok=True)
media = STAGING_ROOT / "source.mp4"
media.write_bytes(b"\x00\x00\x00\x14ftyp" + b"media" * 8)
db_path = DATA_ROOT / "schedule-tests.sqlite3"
store = ProjectStore(db_path)
assert store.setting("engine_enabled") == "0"

def new_job(owner, channel, schedule=""):
    return store.create_project({
        "owner_username": owner, "ui_mode": "simple",
        "source_mode": "local_file", "source_origin": "concat_link",
        "staging_path": str(media), "source_file_name": media.name,
        "source_file_size": media.stat().st_size,
        "source_fingerprint": str(channel), "title": "Scheduled job",
        "privacy_status": "unlisted", "channel_db_id": channel,
        "group_name": "my group", "app_upload_at": schedule,
    })

manual = new_job("owner-one", 1)
future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
scheduled = new_job("owner-one", 2, future)
another = new_job("owner-two", 3, future)
assert store.get(scheduled)["status"] == "SCHEDULED"
assert store.get(scheduled)["publish_at"] == ""  # no YouTube publication timer
assert store.owner_queue_paused("owner-one") is True
assert store.owner_scheduled_paused("owner-one") is False
assert store.claim_next_eligible(schedule_only=True) is None  # not due

past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
store.update(scheduled, app_upload_at=past)
store.update(another, app_upload_at=past)
store.set_owner_scheduled_paused("owner-two", True)
restarted = ProjectStore(db_path)
assert restarted.owner_scheduled_paused("owner-two") is True
assert restarted.owner_queue_paused("owner-one") is True
due = restarted.claim_next_eligible(schedule_only=True)
assert due["id"] == scheduled, due
assert restarted.get(manual)["status"] == "QUEUED"
assert restarted.claim_next_eligible(schedule_only=True) is None
restarted.set_owner_scheduled_paused("owner-two", False)
due2 = restarted.claim_next_eligible(schedule_only=True)
assert due2["id"] == another, due2
assert restarted.owner_queue_paused("owner-two") is True
assert restarted.claim_next_eligible(schedule_only=True) is None
restarted.set_owner_queue_paused("owner-one", False)
claimed_manual = restarted.claim_next_eligible()
assert claimed_manual["id"] == manual, claimed_manual
print("PASS auto due upload, persisted pause, per-owner manual gate")
"""
        self._isolated(script)

    def test_worker_and_endpoint_no_longer_schedule_publication(self):
        engine = (UPLOADER / "lum_engine/worker.py").read_text()
        handler = (UPLOADER / "lum_engine/standalone_app.py").read_text()
        template = (UPLOADER / "templates/app.html").read_text()
        self.assertIn("schedule_only=(concat and not manual_engine_enabled)", engine)
        self.assertIn("concat_upload_at(app_upload_at, timezone_name)", handler)
        self.assertNotIn("concat_publish_at(app_upload_at, timezone_name, privacy_status)", handler)
        self.assertIn('publish_at_utc = ""', handler)
        self.assertIn("Schedule YouTube upload start", template)

    def _isolated(self, script: str) -> None:
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            for stale in ("YOUTUBE_UPLOAD_MANAGER_DATA_DIR", "LUM_GOOGLE_OAUTH_ROOT", "LUM_AUTO_ENGINE_V6_ROOT"):
                env.pop(stale, None)
            env.update({
                "LUM_ENGINE_INSTANCE": "concat",
                "LUM_DATA_DIR": str(Path(root) / "concat-scheduler"),
                "LUM_BASE_PATH": "/concat-studio/concat-uploader",
                "LUM_SUITE_AUTH": "1",
                "LUM_SESSION_SECRET": "test-session-only",
                "PYTHONPATH": str(UPLOADER),
            })
            result = subprocess.run(
                [sys.executable, "-c", script], env=env, cwd=ROOT,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)


if __name__ == "__main__":
    unittest.main()
