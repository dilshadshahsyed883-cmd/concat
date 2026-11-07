"""Offline regression checks for persisted Concat GPU performance metrics.

No NVIDIA device, Salad connection, or desktop EXE build is required.
"""
import hashlib
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from cloud_render.concat.gpu.worker import RenderWorker

TOKEN = "test-renderer-token-" * 3
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 80


def project_zip():
    with io.BytesIO() as stream:
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("bundle.json", json.dumps({
                "version": 1, "project_file": "concat.json", "references": []
            }))
            archive.writestr("project/concat.json", json.dumps({"media": []}))
        return stream.getvalue()


class PerformanceMetricsTests(unittest.TestCase):
    def test_calculates_720_frames_in_12_seconds(self):
        metrics = RenderWorker._performance_metrics(
            {"frames": 720, "seconds": 24.0}, 12.0
        )
        self.assertEqual(metrics, {
            "render_time_seconds": 12.0,
            "rendered_frames": 720,
            "video_duration_seconds": 24.0,
            "average_fps": 60.0,
            "speed_multiplier": 2.0,
        })

    def test_missing_or_invalid_renderer_report_is_backward_compatible(self):
        self.assertEqual(
            RenderWorker._performance_metrics(None, 9.5),
            {"render_time_seconds": 9.5},
        )
        self.assertEqual(
            RenderWorker._performance_metrics({"frames": 0, "seconds": 24.0}, 9.5),
            {"render_time_seconds": 9.5},
        )

    def test_metrics_survive_verified_delivery_and_worker_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            delivered = []

            def render(project, output):
                self.assertTrue((project / "concat.json").is_file())
                time.sleep(0.02)
                output.write_bytes(MP4)
                return {"frames": 720, "seconds": 24.0, "encoder": "nvenc-h264"}

            def deliver(job, output):
                delivered.append(job)
                self.assertTrue(output.is_file())

            worker = RenderWorker(
                root, token=TOKEN, receiver="https://example.com",
                renderer=render, deliver=deliver,
            )
            data = project_zip()
            try:
                worker.accept("metrics1", io.BytesIO(data), len(data),
                              hashlib.sha256(data).hexdigest(), "cuda-hybrid-v1")
                worker.pool.shutdown(wait=True)
                status = worker.status("metrics1")
                self.assertEqual(status["state"], "delivered")
                self.assertEqual(status["rendered_frames"], 720)
                self.assertEqual(status["video_duration_seconds"], 24.0)
                self.assertGreater(status["render_time_seconds"], 0)
                self.assertGreater(status["average_fps"], 0)
                self.assertGreater(status["speed_multiplier"], 0)
                self.assertEqual(delivered, ["metrics1"])
                self.assertFalse((root / "outputs/metrics1.mp4").exists())
                self.assertFalse((root / "jobs/metrics1").exists())
                # Terminal API reads the same persisted JSON as the desktop.
                fresh = RenderWorker(root, token=TOKEN)
                try:
                    self.assertEqual(fresh.status("metrics1"), status)
                finally:
                    fresh.pool.shutdown(wait=True)
            finally:
                worker.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
