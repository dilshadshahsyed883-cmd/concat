"""Single-replica 1/2 render slots; no VRAM policy or GPU prerequisites."""
import hashlib
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from cloud_render.concat.gpu.worker import RenderWorker, Handler, SaladDualStackHTTPServer, WorkerError

TOKEN = "c" * 48
MP4 = b"\x00\x00\x00\x18ftypisom" + b"x" * 64


def bundle():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("bundle.json", json.dumps({"version": 1, "project_file": "concat.json", "references": []}))
        z.writestr("project/concat.json", json.dumps({"media": []}))
    return out.getvalue()


class RenderSlotsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.started = []
        self.active = 0
        self.max_active = 0
        self.data = bundle()
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.worker = RenderWorker(Path(self.temp.name), token=TOKEN, renderer=self.render)

    def tearDown(self):
        self.release.set()
        self.worker.pool.shutdown(wait=True)
        self.temp.cleanup()

    def render(self, project, output):
        with self.lock:
            self.active += 1
            self.started.append(project)
            self.max_active = max(self.max_active, self.active)
        try:
            if not self.release.wait(timeout=5):
                raise RuntimeError("Timed out waiting for test renderer release")
            output.write_bytes(MP4)
        finally:
            with self.lock:
                self.active -= 1

    def add(self, job):
        self.worker.accept(job, io.BytesIO(self.data), len(self.data), self.sha)

    def wait_started(self, count):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            with self.lock:
                if len(self.started) >= count:
                    return
            time.sleep(0.01)
        self.fail(f"Expected {count} renders to start")

    def test_default_one_then_live_switch_to_two_persists(self):
        self.add("one")
        self.add("two")
        self.wait_started(1)
        time.sleep(0.12)
        self.assertEqual(self.max_active, 1)
        self.assertEqual(self.worker.status("two")["state"], "queued")
        self.assertEqual(self.worker.set_concurrency(2)["concurrency"], 2)
        self.wait_started(2)
        self.assertEqual(self.max_active, 2)
        self.release.set()
        self.worker.pool.shutdown(wait=True)
        self.assertEqual(self.worker.status("one")["state"], "rendered")
        self.assertEqual(self.worker.status("two")["state"], "rendered")
        reopened = RenderWorker(Path(self.temp.name), token=TOKEN, renderer=self.render)
        self.assertEqual(reopened.concurrency_status()["concurrency"], 2)
        reopened.pool.shutdown(wait=True)

    def test_decrease_keeps_both_active_and_holds_next(self):
        self.worker.set_concurrency(2)
        self.add("one")
        self.add("two")
        self.wait_started(2)
        self.worker.set_concurrency(1)
        self.add("three")
        time.sleep(0.12)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.worker.concurrency_status()["active_renders"], 2)
        self.release.set()
        self.worker.pool.shutdown(wait=True)
        self.assertEqual(len(self.started), 3)
        self.assertTrue(all(self.worker.status(x)["state"] == "rendered" for x in ("one", "two", "three")))

    def test_invalid_values_are_rejected(self):
        for v in (0, 3, True, "2", None, 1.5):
            with self.subTest(value=v), self.assertRaises(WorkerError):
                self.worker.set_concurrency(v)

    def test_http_authorized_concurrency_update(self):
        server = SaladDualStackHTTPServer(("::", 0), Handler)
        server.worker = self.worker
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            request = json.dumps({"concurrency": 2})
            conn.request("POST", "/api/config/concurrency", request,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            auth = {"Authorization": "Bearer " + TOKEN}
            conn.request("POST", "/api/config/concurrency", request,
                         headers={**auth, "Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["concurrency"], 2)
            conn.request("GET", "/api/config/concurrency", headers=auth)
            response = conn.getresponse()
            self.assertEqual(json.loads(response.read())["concurrency"], 2)
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
