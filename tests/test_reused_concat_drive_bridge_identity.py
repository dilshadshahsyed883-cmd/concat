"""Issue #16: Concat must not assume it can read VPS_1's private rclone HOME."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app.modules.concat.engine import cloud_render as bridge


class BridgeIdentityTests(unittest.TestCase):
    def test_explicit_canonical_config_is_respected_without_worker_home_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "rclone.conf"
            config.write_text("[gdrive]\ntype = drive\n")
            with patch.dict(os.environ, {
                    "RCLONE_CONFIG": str(config),
                    "HOME": "/isolation/concat",
                    "XDG_CONFIG_HOME": "/isolation/concat/config"}):
                env = bridge.scene_rclone_env()
            self.assertEqual(env["RCLONE_CONFIG"], str(config))
            self.assertEqual(env["HOME"], "/isolation/concat")
            self.assertEqual(env["XDG_CONFIG_HOME"], "/isolation/concat/config")

    def test_inaccessible_explicit_config_fails_closed_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "inaccessible.conf")
            with patch.dict(os.environ, {"RCLONE_CONFIG": path}):
                with patch.object(bridge.subprocess, "run") as run:
                    with self.assertRaises(bridge.RcloneFailure) as caught:
                        bridge.CloudRender.execute(["rclone", "lsjson", "gdrive:"])
                    run.assert_not_called()
            self.assertIn("configuration inaccessible", str(caught.exception))
            self.assertNotIn(path, str(caught.exception))

    def test_unconfigured_identity_does_not_assume_scene_private_home_access(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "worker-home"
            home.mkdir()
            with patch.object(bridge, "SCENE_WORKER_HOME", home):
                with patch.dict(os.environ, {"RCLONE_CONFIG": ""}):
                    with self.assertRaises(bridge.RcloneFailure) as caught:
                        bridge.scene_rclone_env()
                    self.assertIn("configuration inaccessible", str(caught.exception))
                    path = home / ".config" / "rclone"
                    path.mkdir(parents=True)
                    (path / "rclone.conf").write_text("[gdrive]\ntype = drive\n")
                    env = bridge.scene_rclone_env()
            self.assertEqual(env["HOME"], str(home))
            self.assertEqual(env["XDG_CONFIG_HOME"], str(home / ".config"))
            self.assertNotIn("RCLONE_CONFIG", env) if not env.get("RCLONE_CONFIG") else self.fail("Unexpected configured remote")

    def test_folder_read_permission_is_not_destination_write_permission(self):
        failure = bridge.classify_rclone_error("lsjson", 1,
            "googleapi: Error 403: insufficientPermissions access_token_secret_123")
        self.assertIn("source item permission denied", str(failure))
        self.assertNotIn("destination permission denied", str(failure))
        self.assertNotIn("access_token_secret_123", str(failure))

    def test_copyto_remains_destination_permission_class(self):
        failure = bridge.classify_rclone_error("copyto", 1,
            "googleapi: Error 403: insufficientPermissions")
        self.assertIn("destination permission denied", str(failure))

    def test_local_config_permission_is_not_mistaken_for_drive_acl(self):
        failure = bridge.classify_rclone_error("lsjson", 1,
            "failed to read config file /private/secret/rclone.conf: permission denied")
        self.assertIn("local Drive Bridge configuration access denied", str(failure))
        self.assertNotIn("/private/", str(failure))

    def test_rclone_failure_is_sanitized_at_execute_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "rclone.conf"
            config.write_text("[gdrive]\ntype = drive\n")
            result = subprocess.CompletedProcess(["rclone"], 1, "",
                "googleapi: Error 403: insufficientPermissions token_secret_abcdef")
            with patch.dict(os.environ, {"RCLONE_CONFIG": str(config)}):
                with patch.object(bridge.subprocess, "run", return_value=result) as run:
                    with self.assertRaises(bridge.RcloneFailure) as caught:
                        bridge.CloudRender.execute(["rclone", "lsjson", "gdrive:"], 60)
            self.assertEqual(run.call_args.kwargs["env"]["RCLONE_CONFIG"], str(config))
            self.assertIn("source item permission denied", str(caught.exception))
            self.assertNotIn("token_secret_abcdef", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
