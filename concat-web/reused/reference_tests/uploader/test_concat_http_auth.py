"""Disposable authenticated Concat Uploader HTTP smoke (no network, uploads, or OAuth)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "apps/copyright-uploader/part01"


class ConcatHTTPContracts(unittest.TestCase):
    def test_four_tabs_owner_channels_sheet_access_and_safe_admission(self):
        script = r'''
import re
from fastapi.testclient import TestClient
import lum_engine.standalone_app as host

prefix = "/concat-studio/concat-uploader"
alice = {"X-Auth-User": "alice", "X-App-Access": "concat-studio", "X-Is-Admin": "0"}
bob = {"X-Auth-User": "bob", "X-App-Access": "concat-studio", "X-Is-Admin": "0"}
admin = {"X-Auth-User": "admin", "X-App-Access": "concat-studio", "X-Is-Admin": "1"}
bad_grant = {"X-Auth-User": "alice", "X-App-Access": "lum", "X-Is-Admin": "0"}

with TestClient(host.app, raise_server_exceptions=False) as client:
    no_session = client.get(prefix + "/app", follow_redirects=False)
    assert no_session.status_code == 303, no_session.status_code
    denied = client.get(prefix + "/app", headers=bad_grant, follow_redirects=False)
    assert denied.status_code == 403, (denied.status_code, denied.text[:400])

    sheet_page = client.get(prefix + "/app?tab=sheets", headers=alice)
    assert sheet_page.status_code == 200, sheet_page.text[:700]
    assert 'data-engine-instance="concat"' in sheet_page.text
    nav = re.search(r'<nav class="lum-tabs"[^>]*>(.*?)</nav>', sheet_page.text, re.S)
    assert nav, "Missing top-level Concat uploader tabs"
    labels = re.findall(r'<a[^>]*>(.*?)</a>', nav.group(1), re.S)
    labels = [re.sub(r'<[^>]*>', '', value).strip() for value in labels]
    assert labels == ["Sheets Sync", "Channels", "Upload Engine", "Queue"], labels

    channels = client.get(prefix + "/app?tab=channels", headers=alice)
    assert channels.status_code == 200, channels.text[:700]
    sections = re.findall(r'data-lum-channel-panel-target="([^"]+)"', channels.text)
    assert sections == ["manager", "accounts"], sections
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', channels.text)
    assert csrf, "Missing authenticated CSRF"
    token = csrf.group(1)

    form = client.get(prefix + "/app?tab=add_video", headers=alice)
    assert form.status_code == 200, form.text[:700]
    assert form.text.count('name="concat_video_link"') == 1
    assert 'name="drive_url"' not in form.text
    assert 'name="video_file"' not in form.text
    assert 'name="privacy_status"' in form.text

    create = client.post(prefix + "/actions/studio-tenant/channel-create", headers=alice,
        data={"csrf_token": token, "channel_id": "UC" + "A"*22,
              "channel_name": "Alice Channel", "google_account_email": "alice@example.org"},
        follow_redirects=False)
    assert create.status_code == 303, (create.status_code, create.text[:400])
    with host.db.connect() as db:
        row = db.execute("SELECT id FROM channels WHERE channel_name='Alice Channel'").fetchone()
    assert row, create.headers.get("location")
    channel_id = int(row[0])

    groups = client.post(prefix + "/actions/studio-tenant/group-create", headers=alice,
        data={"csrf_token": token, "group_name": "My Videos",
              "channel_ids": str(channel_id)}, follow_redirects=False)
    assert groups.status_code == 303, (groups.status_code, groups.text[:300])
    assert "my videos" in str(host.db.channel_group_names_map().get(channel_id, "")).lower(), groups.headers.get("location")

    alice_channels = client.get(prefix + "/app?tab=channels", headers=alice)
    bob_channels = client.get(prefix + "/app?tab=channels", headers=bob)
    admin_channels = client.get(prefix + "/app?tab=channels", headers=admin)
    assert "Alice Channel" in alice_channels.text
    assert "Alice Channel" not in bob_channels.text
    assert "Alice Channel" not in admin_channels.text

    saved = client.post(prefix + "/actions/sheets/save", headers=alice,
        data={"csrf_token": token, "sheet_name": "Alice Sheet",
              "sheet_url_or_id": "https://docs.google.com/spreadsheets/d/" + "A"*32 + "/edit",
              "enabled": "1"}, follow_redirects=False)
    assert saved.status_code == 303, (saved.status_code, saved.text[:300])
    assert "Alice Sheet" in client.get(prefix + "/app?tab=sheets", headers=alice).text
    assert "Alice Sheet" not in client.get(prefix + "/app?tab=sheets", headers=bob).text
    assert "Alice Sheet" not in client.get(prefix + "/app?tab=sheets", headers=admin).text

    # A forged remote URL cannot trigger HTTP download or admit a queue job.
    forged = client.post(prefix + "/actions/add-video", headers=alice,
        data={"csrf_token": token, "title": "Never publish",
              "source_mode": "concat_link", "concat_video_link": "https://evil.example/video.mp4",
              "group_name": "My Videos", "channel_db_id": str(channel_id),
              "privacy_status": "private"}, follow_redirects=False)
    assert forged.status_code == 303, (forged.status_code, forged.text[:300])
    assert not host.store.list(owner="alice"), "Unverified source reached queue"
    assert host.store.setting("engine_enabled", "1") == "0", "Concat worker auto-started"
    assert host.store.setting("sheets_sync_enabled", "0") == "1"
    assert client.get(prefix + "/app?tab=auto_engine", headers=alice).status_code == 200
print("PASS Concat four tabs, two subtabs, auth, owner isolation, safe queue admission")
'''
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update({
                "LUM_ENGINE_INSTANCE": "concat",
                "LUM_BASE_PATH": "/concat-studio/concat-uploader",
                "LUM_DATA_DIR": str(Path(root) / "concat"),
                "LUM_GOOGLE_OAUTH_ROOT": str(Path(root) / "concat/secure"),
                "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(Path(root) / "concat/core"),
                "LUM_SESSION_SECRET": "disposable-test-only-not-production",
                "LUM_SUITE_AUTH": "1",
                "LUM_COOKIE_SECURE": "0",
                "PYTHONPATH": str(APP_DIR),
            })
            done = subprocess.run([sys.executable, "-c", script], env=env,
                                  cwd=APP_DIR, capture_output=True, text=True, timeout=75)
            self.assertEqual(done.returncode, 0, done.stdout + "\n" + done.stderr[-5000:])
            self.assertIn("PASS Concat", done.stdout)


if __name__ == "__main__":
    unittest.main()
