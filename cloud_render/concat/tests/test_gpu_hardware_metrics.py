"""Read-only hardware telemetry: no GPU dependency in offline tests."""
import subprocess
import unittest
from unittest.mock import patch

from cloud_render.concat.gpu.worker import RenderWorker


class HardwareMetricsTest(unittest.TestCase):
    @patch("cloud_render.concat.gpu.worker.subprocess.run")
    def test_supported_and_missing_values(self, run):
        run.return_value.stdout = "NVIDIA RTX 3090, 75, 8000, 24576, 60, N/A\n"
        result = RenderWorker.hardware_metrics()
        self.assertEqual(result["gpu_model"], "NVIDIA RTX 3090")
        self.assertEqual(result["gpu_utilization_percent"], 75)
        self.assertEqual(result["vram_used_mb"], 8000)
        self.assertNotIn("encoder_utilization_percent", result)
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    @patch("cloud_render.concat.gpu.worker.subprocess.run")
    def test_timeout_is_unavailable(self, run):
        run.side_effect = subprocess.TimeoutExpired("nvidia-smi", 3)
        self.assertEqual(RenderWorker.hardware_metrics(), {})

    @patch("cloud_render.concat.gpu.worker.subprocess.run")
    def test_multiple_devices_are_not_misattributed(self, run):
        run.return_value.stdout = "GPU A, 75, 8000, 24576, 60, 5\nGPU B, 20, 2, 3, 30, 1\n"
        self.assertEqual(RenderWorker.hardware_metrics(), {})
