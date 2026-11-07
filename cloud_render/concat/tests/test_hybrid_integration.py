import http.client
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cloud_render.concat.gpu.worker import DirectReceiver, Handler, RenderWorker


class CompatibilityTests(unittest.TestCase):
    def test_capabilities_require_auth_and_report_wire_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            token = "testing-renderer-token-" * 2
            worker = RenderWorker(Path(directory), token=token)
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                server.worker = worker
                runner = threading.Thread(target=server.serve_forever)
                runner.start()
                client = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    client.request("GET", "/api/capabilities")
                    response = client.getresponse()
                    self.assertEqual(response.status, 401)
                    response.read()
                    client.close()
                    client.request("GET", "/api/capabilities",
                                   headers={"Authorization": "Bearer " + token})
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), {
                        "api_version": 1, "bundle_versions": [1],
                        "render_modes": ["cuda-stage1", "cuda-hybrid-v1"],
                    })
                finally:
                    client.close()
                    server.shutdown()
                    runner.join()
                    worker.pool.shutdown()

    def test_ingest_redirect_rejected_before_following(self):
        paths = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()

            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.end_headers()

        with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
            runner = threading.Thread(target=server.serve_forever)
            runner.start()
            try:
                host, port = server.server_address
                request = urllib.request.Request(
                    f"http://{host}:{port}/ingest", data=b"test",
                    headers={"X-Cloud-Render-Token": "test-only"},
                )
                with self.assertRaisesRegex(RuntimeError, "redirected"):
                    urllib.request.build_opener(DirectReceiver()).open(request, timeout=5)
            finally:
                server.shutdown()
                runner.join()
        self.assertEqual(paths, ["/ingest"])
