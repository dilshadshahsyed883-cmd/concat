"""Resumable upload wire protocol, digest, replay, and restart regressions."""
import hashlib
import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from cloud_render.concat.gpu.resumable import CHUNK_BYTES, UploadError
from cloud_render.concat.gpu.worker import Handler, RenderWorker, SaladDualStackHTTPServer

TOKEN = "t" * 48
MP4 = b"\x00\x00\x00\x18ftypisom" + b"0" * 80


def sample_zip(media_bytes=0):
    data = b"d" * media_bytes
    member = "media/" + "b" * 32 + ".wav"
    references = [{"index": 0, "member": member,
                   "size": media_bytes, "sha256": hashlib.sha256(data).hexdigest()}] if media_bytes else []
    project = {"media": [{"path": "source.wav"}] if media_bytes else []}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("bundle.json", json.dumps({
            "version": 1, "project_file": "concat.json", "references": references}))
        archive.writestr("project/concat.json", json.dumps(project))
        if media_bytes:
            archive.writestr(member, data, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


class ResumableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.worker = RenderWorker(self.dir, token=TOKEN,
                                   renderer=self.render)
        self.renders = []

    def render(self, project, output):
        self.renders.append(project)
        self.assertTrue((project / "concat.json").is_file())
        output.write_bytes(MP4)

    def tearDown(self):
        self.worker.pool.shutdown(wait=True)
        self.temp.cleanup()

    def test_resume_two_chunks_then_sha_verified_render(self):
        data = sample_zip(CHUNK_BYTES + 137)
        sha = hashlib.sha256(data).hexdigest()
        state = self.worker.spool.begin("test_a", len(data), sha)
        self.assertEqual(state["total_chunks"], 2)
        first = data[:CHUNK_BYTES]
        second = data[CHUNK_BYTES:]
        self.worker.spool.put_chunk("test_a", 0, io.BytesIO(first), len(first), hashlib.sha256(first).hexdigest())
        state = self.worker.spool.begin("test_a", len(data), sha)
        self.assertEqual(state["uploaded"], [0])
        with self.assertRaises(UploadError) as bad:
            self.worker.spool.put_chunk("test_a", 1, io.BytesIO(second), len(second), "0" * 64)
        self.assertEqual(bad.exception.status, 422)
        with self.assertRaises(UploadError) as missing:
            self.worker.spool.complete("test_a")
        self.assertEqual(missing.exception.status, 409)
        self.worker.spool.put_chunk("test_a", 1, io.BytesIO(second), len(second), hashlib.sha256(second).hexdigest())
        record = self.worker.spool.complete("test_a")
        self.assertIn(record["state"], ("queued", "running", "rendered"))
        self.worker.pool.shutdown(wait=True)
        self.assertEqual(self.worker.status("test_a")["state"], "rendered")
        self.assertEqual(len(self.renders), 1)
        self.assertEqual(self.worker.spool.complete("test_a")["state"], "rendered")
        self.assertFalse((self.worker.root / "uploads/test_a").exists())
        self.assertTrue((self.worker.inputs / "test_a/project.zip").exists())
        self.assertTrue((self.worker.outputs / "test_a.mp4").exists())

    def test_upload_survives_process_restart(self):
        data = sample_zip()
        sha = hashlib.sha256(data).hexdigest()
        self.worker.spool.begin("test_b", len(data), sha)
        self.worker.spool.put_chunk("test_b", 0, io.BytesIO(data), len(data), sha)
        resumed = RenderWorker(self.dir, token=TOKEN,
                               renderer=self.render)
        self.assertEqual(resumed.spool.status("test_b")["uploaded"], [0])
        self.assertEqual(resumed.spool.begin("test_b", len(data), sha)["remaining"], 0)
        resumed.pool.shutdown(wait=True)

    def test_replay_same_chunk_and_reject_conflicting_manifest(self):
        data = sample_zip()
        sha = hashlib.sha256(data).hexdigest()
        self.worker.spool.begin("test_c", len(data), sha)
        self.worker.spool.put_chunk("test_c", 0, io.BytesIO(data), len(data), sha)
        self.assertEqual(self.worker.spool.put_chunk("test_c", 0, io.BytesIO(data), len(data), sha)["remaining"], 0)
        with self.assertRaises(UploadError):
            self.worker.spool.begin("test_c", len(data), "f" * 64)
        with self.assertRaises(UploadError):
            self.worker.spool.put_chunk("test_c", 0, io.BytesIO(data), len(data) - 1, sha)

    def test_actual_http_init_chunks_complete_and_auth(self):
        data = sample_zip()
        sha = hashlib.sha256(data).hexdigest()
        server = SaladDualStackHTTPServer(("::", 0), Handler)
        server.worker = self.worker
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "/api/uploads/test_http"
        auth = {"Authorization": "Bearer " + TOKEN}
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
        try:
            conn.request("POST", base, body=json.dumps({"size":len(data), "sha256":sha}),
                         headers={"Content-Type":"application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
            conn.request("POST", base, body=json.dumps({"size":len(data), "sha256":sha}),
                         headers={**auth,"Content-Type":"application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["remaining"], 1)
            conn.request("PUT", base + "/chunks/0", body=data,
                         headers={**auth, "Content-Type":"application/octet-stream","X-Content-SHA256":sha})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["remaining"], 0)
            conn.request("GET", base, headers=auth)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["uploaded"], [0])
            conn.request("POST", base + "/complete", body=b"", headers=auth)
            response = conn.getresponse()
            self.assertEqual(response.status, 202)
            response.read()
            self.worker.pool.shutdown(wait=True)
            conn.request("GET", "/api/jobs/test_http", headers=auth)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["state"], "rendered")
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
