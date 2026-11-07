"""CUDA-only Salad diagnostics/auth regression (no legacy Vulkan engine)."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from cloud_render.concat.gpu.worker import Handler, RenderWorker, SaladDualStackHTTPServer


class CUDAWorkerDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_require_auth_and_never_offer_native_vulkan(self):
        with tempfile.TemporaryDirectory() as folder:
            token = "s" * 48
            worker = RenderWorker(Path(folder), token=token)
            worker.ready = lambda: True
            server = SaladDualStackHTTPServer(("::", 0), Handler)
            server.worker = worker
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/api/diagnostics/gpu")
                response = conn.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/api/diagnostics/gpu",
                             headers={"Authorization": "Bearer " + token})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                data = json.loads(response.read())
                self.assertEqual(data["engine"], "cuda-nvenc-stage1")
                self.assertTrue(data["cuda_stage1"])
                self.assertFalse(data["native_vulkan"])
                self.assertNotIn("vulkan_adapters", data)
                self.assertNotIn("RENDER_OUTPUT_TOKEN", str(data))
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                worker.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
