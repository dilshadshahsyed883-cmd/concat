"""Mocked YouTube insertion contract: Concat scheduled public release stays private until publishAt."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPLOADER = ROOT / "reused/uploader/part01"


class ScheduledYouTubePayload(unittest.TestCase):
    def test_selected_release_contract_is_concat_only(self):
        script = r'''
import os
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory

# The target stage uses only the HttpError exception type in this all-success
# mocked run. Never open YouTube credentials or send a network request.
api = types.ModuleType("googleapiclient")
errors = types.ModuleType("googleapiclient.errors")
errors.HttpError = type("HttpError", (Exception,), {})
http = types.ModuleType("googleapiclient.http")
http.MediaFileUpload = lambda *args, **kwargs: object()
api.errors = errors
api.http = http
sys.modules["googleapiclient"] = api
sys.modules["googleapiclient.errors"] = errors
sys.modules["googleapiclient.http"] = http

from desktop_core.modules.upload_pipeline.stages.media import MediaUploadStage

class FakeRequest:
    resumable_uri = None
    def next_chunk(self):
        return None, {"id": "safe_test_video_id"}

class FakeService:
    def __init__(self):
        self.body = None
    def videos(self):
        return self
    def insert(self, *, part, body, media_body):
        assert part == "snippet,status"
        self.body = body
        return FakeRequest()

class FakeDB:
    def update_video_status(self, *args, **kwargs):
        pass
    def clear_resumable_state(self, *args):
        pass

class FakeJob:
    id = 10
    youtube_video_id = ""
    youtube_url = ""
    title = "Video title"
    description = "Description"
    tags = ("a", "b")
    category_id = "22"
    made_for_kids = False
    resumable_uri = ""
    resumable_file_size = 0
    resumable_file_mtime_ns = 0
    progress = 0
    privacy_status = "private"
    raw = {"publish_at": "2026-11-22T15:00:00Z"}

class Context:
    def __init__(self, video):
        self.job = FakeJob()
        self.job.local_video_path = video
        self.db = FakeDB()
        self.service = FakeService()
        self.media_handle = None
        self.request_handle = None
    def checkpoint(self, *args, **kwargs):
        pass
    def report(self, *args):
        pass
    def stop_requested(self):
        return False
    def ensure_service(self):
        return self.service
    def media_upload_factory(self, *args, **kwargs):
        return object()

with TemporaryDirectory() as root:
    path = Path(root) / "safe.mp4"
    path.write_bytes(b"\x00\x00\x00\x14ftypisom")
    os.environ["LUM_ENGINE_INSTANCE"] = "concat"
    ctx = Context(path)
    MediaUploadStage().run(ctx)
    assert ctx.service.body["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "publishAt": "2026-11-22T15:00:00Z",
    }
    assert ctx.youtube_video_id == "safe_test_video_id"

    # Forged/mistaken public scheduled submission must not be uploaded.
    invalid = Context(path)
    invalid.job.privacy_status = "public"
    try:
        MediaUploadStage().run(invalid)
    except ValueError as exc:
        assert "private" in str(exc)
    else:
        raise AssertionError("Scheduled public insertion bypassed private gate")
    assert invalid.service.body is None

    # Copyright's normal uploader must not inherit Concat's publishAt extension.
    os.environ["LUM_ENGINE_INSTANCE"] = "copyright"
    original = Context(path)
    MediaUploadStage().run(original)
    assert "publishAt" not in original.service.body["status"]
print("PASS mocked YouTube schedule payload and Copyright parity")
'''
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update({
                "PYTHONPATH": str(UPLOADER),
                "LUM_DATA_DIR": str(Path(root) / "isolated-source-test"),
            })
            done = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stdout + "\n" + done.stderr)
            self.assertIn("PASS mocked YouTube", done.stdout)


if __name__ == "__main__":
    unittest.main()
