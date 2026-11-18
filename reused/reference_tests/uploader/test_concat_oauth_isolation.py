"""Offline Concat OAuth client fallback and owner-private channel token mapping.

Only disposable SQLite and synthetic client JSON: no OAuth browser login,
YouTube publication, channel calls or production file modification.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPLOADER = ROOT / "apps/copyright-uploader/part01"


class ConcatOAuthOwnerIsolation(unittest.TestCase):
    def test_shared_client_and_owner_token_boundaries(self):
        script = r'''
import json
from pathlib import Path
from lum_engine.standalone_app import db, studio_tenants
from lum_engine.oauth_web import (
    _owner_client_path, save_owner_client_json, save_shared_client_json,
    owner_client_for_oauth, list_owner_clients,
)
from lum_engine.copyright_vault import _token_root, import_token

cfg=json.dumps({"installed": {
    "client_id": "offline-tests.apps.googleusercontent.com",
    "project_id": "offline-concat",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_secret": "unit-test-only",
    "redirect_uris": ["http://localhost:8765/"]
}}).encode()
a=studio_tenants.create_channel("Alice", "UC"+"a"*22, "Alice Channel", "alice@example.org")
b=studio_tenants.create_channel("Bob", "UC"+"b"*22, "Bob Channel", "bob@example.org")
assert a != b
save_shared_client_json(cfg)
assert list_owner_clients("Alice")[0]["label"] == "shared_oauth_client"
assert list_owner_clients("Bob")[0]["label"] == "shared_oauth_client"
shared = owner_client_for_oauth("Bob")
assert shared.is_file()
save_owner_client_json("Alice", cfg)
assert list_owner_clients("Alice")[0]["label"] == "my_oauth_client"
assert owner_client_for_oauth("Alice") == _owner_client_path("Alice")
assert owner_client_for_oauth("Bob") == shared
assert not _owner_client_path("Bob").exists()
assert _token_root("Alice") != _token_root("Bob")

# Cross-user uploaded token filename is not even eligible for validation.
foreign_token = json.dumps({"client_id": "offline", "refresh_token": "never-use"}).encode()
for owner, foreign in [
    ("Alice", "UC"+"b"*22+".json"),
    ("Bob", "UC"+"a"*22+".json"),
    ("Admin", "UC"+"a"*22+".json"),
]:
    result=import_token(db,studio_tenants,owner,foreign,foreign_token)
    assert result["status"] == "UNMATCHED", result
assert not db.get_channel(a)["token_file_path"]
assert not db.get_channel(b)["token_file_path"]
assert studio_tenants.allowed_ids("channel",{"username":"Admin","is_admin":1}) == set()
print("PASS Concat OAuth shared client and owner-private token mapping")
'''
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "isolated-concat"
            env = os.environ.copy()
            for key in ("YOUTUBE_UPLOAD_MANAGER_DATA_DIR", "LUM_GOOGLE_OAUTH_ROOT",
                        "LUM_AUTO_ENGINE_V6_ROOT"):
                env.pop(key, None)
            env.update({
                "LUM_ENGINE_INSTANCE": "concat",
                "LUM_DATA_DIR": str(data),
                "LUM_BASE_PATH": "/concat-studio/concat-uploader",
                "LUM_SESSION_SECRET": "synthetic-unit-only",
                "LUM_SUITE_AUTH": "1",
                "PYTHONPATH": str(UPLOADER),
            })
            done=subprocess.run([sys.executable,"-c",script], env=env, cwd=UPLOADER,
                                capture_output=True, text=True, timeout=35)
            self.assertEqual(done.returncode, 0, done.stdout + "\n" + done.stderr[-5000:])
            self.assertIn("PASS Concat OAuth",done.stdout)


if __name__ == "__main__":
    unittest.main()
