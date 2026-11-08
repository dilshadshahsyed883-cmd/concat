"""Verify the desktop queue's explicit-start safety contract without a GUI build."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
QUEUE = ROOT / "upstream/src/crates/concat/src/cloud_gpu_queue.rs"
MODEL = ROOT / "upstream/src/crates/concat/src/gpu_queue_model.rs"


class ManualQueueContractTests(unittest.TestCase):
    def test_enqueue_does_not_implicitly_start_queue(self):
        code = QUEUE.read_text(encoding="utf-8")
        add = code.split("    fn add(&mut self, project: &str) {", 1)[1].split("    fn control(", 1)[0]
        self.assertIn("self.data.enqueue(Job", add)
        self.assertNotIn("self.data.started = true", add)
        self.assertIn("Click Start Queue", add)

    def test_queue_model_guards_waiting_uploads(self):
        code = MODEL.read_text(encoding="utf-8")
        self.assertIn("pub fn enqueue(&mut self, job: Job)", code)
        self.assertIn("if !self.started", code)
        self.assertIn("fn interrupted_queue_remains_manual_after_new_job()", code)


if __name__ == "__main__":
    unittest.main()
