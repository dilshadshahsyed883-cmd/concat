"""Unix intake failures must never fall back to Concat's private rclone."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.modules.concat.engine.drive_socket import DriveSocket
from app.modules.concat.engine.cloud_render import CloudRender
from app.modules.concat.tenant import owner_key

ROW = dict(drive_id="id-1", name="bundle.zip", resource_keys="", drive_size=3,
           drive_md5="", folder_id="parent", drive_path="bundle.zip")


class Response(io.BytesIO):
    status = 200
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}
    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class SocketTests(unittest.TestCase):
    def reply(self, response):
        connection = Mock()
        connection.getresponse.return_value = response
        return patch("app.modules.concat.engine.drive_socket.UnixHTTP", return_value=connection), connection

    def test_production_uses_verified_owner_socket_and_not_direct_rclone(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = CloudRender(Path(temp), owner_key("alice"))
            try:
                self.assertIsInstance(manager.production.drive, DriveSocket)
                self.assertEqual(manager.production.drive.owner, owner_key("alice"))
                context, conn = self.reply(Response(json.dumps({"files": [[
                    "id-1", "bundle.zip", "", 3, "", "parent", "bundle.zip"]]}).encode()))
                with context, patch.object(manager, "execute") as execute:
                    self.assertEqual(manager.production.scan("https://drive.google.com/drive/folders/parent")["added"], 1)
                    execute.assert_not_called()
                payload = json.loads(conn.request.call_args.args[2])
                self.assertEqual(payload["owner"], owner_key("alice"))
            finally:
                manager.pool.shutdown()

    def test_source_denial_creates_no_queue_rows_and_sanitizes_error(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = CloudRender(Path(temp), owner_key("alice"))
            response = Response(b'{"error":"source_denied", "raw":"secret"}')
            response.status = 502
            context, _ = self.reply(response)
            try:
                with context:
                    with self.assertRaises(Exception) as error:
                        manager.production.scan("https://drive.google.com/drive/folders/denied")
                self.assertIn("source item permission denied", str(error.exception))
                self.assertNotIn("secret", str(error.exception))
                with manager.connection() as con:
                    self.assertEqual(con.execute("SELECT count(*) FROM production_jobs").fetchone()[0], 0)
            finally:
                manager.pool.shutdown()

    def test_missing_socket_fails_closed(self):
        with patch("app.modules.concat.engine.cloud_render.subprocess.run") as run:
            client = DriveSocket("a" * 64, "/nonexistent-bridge/intake.sock")
            self.assertFalse(client.available())
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                client.discover("https://drive.google.com/drive/folders/parent")
            run.assert_not_called()

    def test_download_exact_bytes_and_rejects_corrupt_or_truncated_stream(self):
        for data, sha, expected in ((b"zip", hashlib.sha256(b"zip").hexdigest(), True),
                                    (b"zi", hashlib.sha256(b"zip").hexdigest(), False),
                                    (b"zip", "0" * 64, False)):
            with self.subTest(data=data, sha=sha), tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "job.part"
                context, _ = self.reply(Response(data, {"Content-Length": "3",
                    "X-Content-SHA256": sha, "X-Zip-Name": '"bundle.zip"'}))
                with context:
                    if expected:
                        self.assertEqual(DriveSocket("a" * 64).request("download", {"row": ROW}, target), "bundle.zip")
                        self.assertEqual(target.read_bytes(), b"zip")
                    else:
                        with self.assertRaises(RuntimeError):
                            DriveSocket("a" * 64).request("download", {"row": ROW}, target)
                        self.assertFalse(target.exists())

    def test_destination_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            victim = Path(temp) / "victim"
            victim.write_bytes(b"keep")
            target = Path(temp) / "job.part"
            target.symlink_to(victim)
            context, _ = self.reply(Response(b"zip", {"Content-Length": "3",
                "X-Content-SHA256": hashlib.sha256(b"zip").hexdigest(), "X-Zip-Name": '"bundle.zip"'}))
            with context, self.assertRaises(RuntimeError):
                DriveSocket("a" * 64).request("download", {"row": ROW}, target)
            self.assertEqual(victim.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
