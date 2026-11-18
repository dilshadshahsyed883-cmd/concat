"""Concat-specific queue and identity regressions without network/publishing.

One disposable subprocess imports the reused store with a dedicated Concat
data root so no Copyright, Song or normal LUM runtime can be modified.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPLOADER = ROOT / "reused/uploader/part01"
CORE = UPLOADER / "desktop_core/modules"


class ConcatOwnerQueueContracts(unittest.TestCase):
    def test_durable_manual_start_dedup_and_owner_isolation(self):
        script = r'''
import hashlib
import sqlite3
from pathlib import Path
from lum_engine.config import DATA_ROOT, STAGING_ROOT
from lum_engine.project_store import ProjectStore
from lum_engine.studio_tenants import StudioTenants

assert DATA_ROOT.name == "concat-only"
assert STAGING_ROOT.is_dir() or not STAGING_ROOT.exists()
STAGING_ROOT.mkdir(parents=True, exist_ok=True)
stage = STAGING_ROOT / "concat-unit.mp4"
stage.write_bytes(b"\x00\x00\x00\x14ftyp" + b"video" * 20)
fingerprint = hashlib.sha256(stage.read_bytes()).hexdigest()
store = ProjectStore(DATA_ROOT / "projects.sqlite3")

def project(owner, channel):
    return {
        "owner_username": owner,
        "ui_mode": "heavy",
        "source_mode": "local_file",
        "source_origin": "concat_link",
        "staging_path": str(stage),
        "source_file_name": "Concat.mp4",
        "source_file_size": stage.stat().st_size,
        "source_fingerprint": fingerprint,
        "title": "Test title",
        "description": "Test description",
        "privacy_status": "private",
        "channel_db_id": channel,
        "group_name": "group",
    }

a = store.create_project(project("owner1", 11))
b = store.create_project(project("owner2", 22))
assert a != b
assert store.owner_queue_paused("owner1") is True
assert store.owner_queue_paused("owner2") is True
assert store.claim_next_eligible() is None, "Queued work must never auto-start"
assert [x["id"] for x in store.list(owner="owner1")] == [a]
assert [x["id"] for x in store.list(owner="owner2")] == [b]
try:
    store.create_project(project("owner1", 11))
except ValueError as exc:
    assert "already" in str(exc).lower()
else:
    raise AssertionError("Duplicate queued source admitted")
restarted = ProjectStore(DATA_ROOT / "projects.sqlite3")
assert restarted.owner_queue_paused("owner1") is True
assert restarted.owner_queue_paused("owner2") is True
restarted.set_owner_queue_paused("owner1", False)
claimed = restarted.claim_next_eligible()
assert claimed["id"] == a, claimed
assert restarted.claim_next_eligible() is None, "Owner2 is still paused"
restarted.recover_wrapper_states()
assert restarted.owner_queue_paused("owner2") is True
assert restarted.get(a)["status"] == "QUEUED"

scheduled = project("owner1", 33)
scheduled["privacy_status"] = "public"
scheduled["publish_at"] = "2099-01-01T20:00:00Z"
scheduled_id = restarted.create_project(scheduled)
assert restarted.get(scheduled_id)["publish_at"] == scheduled["publish_at"]
try:
    restarted.edit_waiting_metadata(
        scheduled_id, "owner1", title="Updated title", description="Safe",
        tags="", category_id="22", privacy_status="private")
except ValueError as exc:
    assert "Public" in str(exc), exc
else:
    raise AssertionError("Scheduled public release privacy downgrade was admitted")
assert restarted.get(scheduled_id)["privacy_status"] == "public"

class DB:
    def __init__(self, path):
        self.path = path
    def connect(self):
        return sqlite3.connect(self.path)

tenants = StudioTenants(DB(DATA_ROOT / "tenant.sqlite3"))
tenants.bind("channel", 101, "owner1")
tenants.bind("channel", 202, "owner2")
tenants.require("channel", 101, {"username": "owner1", "is_admin": 0})
assert tenants.allowed_ids("channel", {"username": "owner1", "is_admin": 1}) == {101}
for target in (101, 202):
    owner = "owner2" if target == 101 else "owner1"
    for admin in (0, 1):
        try:
            tenants.require("channel", target, {"username": owner, "is_admin": admin})
        except PermissionError:
            pass
        else:
            raise AssertionError("Cross-user channel passed owner scope")
print("PASS Concat owner queue, persisted pause, dedup, admin parity")
'''
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "concat-only"
            env = os.environ.copy()
            # Other imported upstream modules may set these process-global defaults.
            # An isolated subprocess must not inherit their previous data roots.
            # Keep the production Concat storage guard intact; test its clean
            # canonical data-root configuration instead.
            for stale in (
                "YOUTUBE_UPLOAD_MANAGER_DATA_DIR",
                "LUM_GOOGLE_OAUTH_ROOT",
                "LUM_AUTO_ENGINE_V6_ROOT",
            ):
                env.pop(stale, None)
            env.update({
                "LUM_ENGINE_INSTANCE": "concat",
                "LUM_DATA_DIR": str(path),
                "LUM_BASE_PATH": "/concat-studio/concat-uploader",
                "LUM_SUITE_AUTH": "1",
                "LUM_SESSION_SECRET": "isolated-unit-test-only",
                "PYTHONPATH": str(UPLOADER),
            })
            done = subprocess.run([sys.executable, "-c", script], env=env,
                                  cwd=str(ROOT), capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stdout + "\n" + done.stderr)
            self.assertIn("PASS Concat", done.stdout)

    def test_concat_rejects_shared_core_and_oauth_storage(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            common = {
                "LUM_ENGINE_INSTANCE": "concat",
                "LUM_DATA_DIR": str(base / "concat-only"),
                "LUM_BASE_PATH": "/concat-studio/concat-uploader",
                "LUM_SUITE_AUTH": "1",
                "PYTHONPATH": str(UPLOADER),
            }
            for name, value in (
                ("YOUTUBE_UPLOAD_MANAGER_DATA_DIR", str(base / "copyright/core")),
                ("LUM_GOOGLE_OAUTH_ROOT", str(base / "copyright/secure")),
                ("LUM_AUTO_ENGINE_V6_ROOT", str(base / "copyright/auto_engine_v6")),
            ):
                with self.subTest(name=name):
                    env = os.environ.copy()
                    for stale in ("YOUTUBE_UPLOAD_MANAGER_DATA_DIR",
                                  "LUM_GOOGLE_OAUTH_ROOT", "LUM_AUTO_ENGINE_V6_ROOT"):
                        env.pop(stale, None)
                    env.update(common)
                    env[name] = value
                    run = subprocess.run(
                        [sys.executable, "-c", "import lum_engine.config"],
                        env=env, cwd=ROOT, capture_output=True, text=True, timeout=15,
                    )
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn("Concat", run.stderr)

            for folder in ("lum-copyright", "lum1"):
                with self.subTest(folder=folder):
                    env = os.environ.copy()
                    env.update(common, LUM_DATA_DIR=str(base / folder))
                    for stale in ("YOUTUBE_UPLOAD_MANAGER_DATA_DIR",
                                  "LUM_GOOGLE_OAUTH_ROOT", "LUM_AUTO_ENGINE_V6_ROOT"):
                        env.pop(stale, None)
                    run = subprocess.run(
                        [sys.executable, "-c", "import lum_engine.config"],
                        env=env, cwd=ROOT, capture_output=True, text=True, timeout=15,
                    )
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn("Concat", run.stderr)

    def test_selected_core_video_only_and_channels_reuse(self):
        from ast import parse
        standalone = (UPLOADER / "lum_engine/standalone_app.py").read_text()
        core_db = (CORE / "db/queue_repository.py").read_text()
        core_worker = (CORE / "youtube_uploader.py").read_text()
        engine = (UPLOADER / "lum_engine/worker.py").read_text()
        for name, source in {
            "standalone": standalone, "db": core_db, "engine": core_worker,
            "worker": engine,
        }.items():
            with self.subTest(name=name):
                parse(source)
        self.assertIn('if os.getenv("LUM_ENGINE_INSTANCE") != "concat":', standalone)
        self.assertIn('"concat"', standalone)
        self.assertIn('only_video_id', core_db)
        self.assertIn('only_video_id', core_worker)
        self.assertIn('only_video_id', engine)
        self.assertIn("video_id_for_staged_path", engine)
        self.assertIn("REVIEW_REQUIRED", engine)
        self.assertIn('studio_tenants.create_channel', standalone)
        self.assertIn('studio_tenants.create_group', standalone)


if __name__ == "__main__":
    unittest.main()
