"""Concat Outputs: manual Media Upload to Drive with no direct rclone access."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.modules.concat.engine.cloud_render import CloudRender
from app.modules.concat.engine.drive_socket import DriveSocket
from app.modules.concat.tenant import owner_key


class OutputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.owner = owner_key("alice")
        self.manager = CloudRender(Path(self.temp.name), self.owner)
        self.mp4 = b"\x00\x00\x00\x18ftypisom" + b"verified Concat output" * 5
        self.sha = hashlib.sha256(self.mp4).hexdigest()
        self.md5 = hashlib.md5(self.mp4, usedforsecurity=False).hexdigest()

    def tearDown(self):
        self.manager.pool.shutdown(wait=True)
        self.temp.cleanup()

    def ready(self, job="job1"):
        result = self.manager.receive(
            job, 0, self.mp4, name="s2.mp4", size=len(self.mp4), sha=self.sha,
        )
        self.assertEqual(result["state"], "ready")
        return job

    def test_one_click_uses_canonical_bridge_and_deletes_only_verified_local(self):
        job = self.ready()
        remote = "gdrive:Concat Outputs/" + self.owner + "/job1_s2.mp4"
        with patch.object(DriveSocket, "upload_mp4", return_value=remote) as upload:
            self.assertEqual(self.manager.queue(job)["state"], "queued")
            self.manager.pool.shutdown(wait=True)
        upload.assert_called_once()
        args, kwargs = upload.call_args
        self.assertEqual(args, (self.manager.local(job),))
        self.assertEqual(kwargs, {
            "job": job, "name": "s2.mp4", "size": len(self.mp4),
            "sha": self.sha, "md5": self.md5,
        })
        with self.manager.connection() as con:
            row = con.execute(
                "SELECT state,drive_path,error FROM files WHERE id=?", (job,)
            ).fetchone()
        self.assertEqual(row["state"], "delivered")
        self.assertEqual(row["drive_path"], remote)
        self.assertEqual(row["error"], "")
        self.assertFalse(self.manager.local(job).exists())

    def test_failed_bridge_upload_preserves_604mb_equivalent_vps_original(self):
        job = self.ready()
        with patch.object(DriveSocket, "upload_mp4",
                          side_effect=RuntimeError("Drive output upload unavailable; VPS original retained")):
            self.manager.queue(job)
            self.manager.pool.shutdown(wait=True)
        with self.manager.connection() as con:
            row = con.execute("SELECT state,error FROM files WHERE id=?", (job,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertIn("original retained", row["error"])
        self.assertEqual(self.manager.local(job).read_bytes(), self.mp4)

    def test_bob_cannot_queue_alices_private_mp4(self):
        job = self.ready()
        with tempfile.TemporaryDirectory() as temp:
            bob = CloudRender(Path(temp), owner_key("bob"))
            try:
                with self.assertRaises(HTTPException) as ctx:
                    bob.queue(job)
                self.assertEqual(ctx.exception.status_code, 404)
                self.assertEqual(self.manager.local(job).read_bytes(), self.mp4)
            finally:
                bob.pool.shutdown(wait=True)

    def test_socket_client_streams_fixed_owner_output_without_direct_rclone(self):
        job = self.ready()
        path = self.manager.local(job)
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps({
            "ok": True, "remote": "gdrive:Concat Outputs/" + self.owner + "/job1_s2.mp4",
            "size": len(self.mp4), "md5": self.md5,
        }).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("app.modules.concat.engine.drive_socket.UnixHTTP",
                   return_value=connection) as http:
            remote = DriveSocket(self.owner).upload_mp4(
                path, job=job, name="s2.mp4", size=len(self.mp4),
                sha=self.sha, md5=self.md5,
            )
        self.assertTrue(remote.startswith("gdrive:Concat Outputs/"))
        http.assert_called_once()
        self.assertEqual(connection.send.call_args.args[0], self.mp4)
        self.assertIn(
            ("X-Owner", self.owner),
            [call.args for call in connection.putheader.call_args_list],
        )
        self.assertIn(
            ("X-Content-MD5", self.md5),
            [call.args for call in connection.putheader.call_args_list],
        )

    def test_fake_bridge_success_with_wrong_remote_is_rejected(self):
        job = self.ready()
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps({
            "ok": True, "remote": "gdrive:Some Other Folder/" + self.owner + "/job1_s2.mp4",
            "size": len(self.mp4), "md5": self.md5,
        }).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("app.modules.concat.engine.drive_socket.UnixHTTP",
                   return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                DriveSocket(self.owner).upload_mp4(
                    self.manager.local(job), job=job, name="s2.mp4",
                    size=len(self.mp4), sha=self.sha, md5=self.md5,
                )
        self.assertEqual(self.manager.local(job).read_bytes(), self.mp4)


if __name__ == "__main__":
    unittest.main()
