"""Static interface contract; numerical physics assertions execute via cargo CI."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class PhysicsControlContractTests(unittest.TestCase):
    def test_parameter_controls_reach_gpu_native_keyframe_sampler(self):
        physics = (ROOT / "upstream/src/crates/concat-project/src/physics_motion.rs").read_text()
        ui = (ROOT / "upstream/src/crates/concat/ui/inspector/clip-inspector.slint").read_text()
        lib = (ROOT / "upstream/src/crates/concat/src/lib.rs").read_text()
        media_ui = (ROOT / "upstream/src/crates/concat/ui/workspace/media-pane.slint").read_text()
        timeline_ui = (ROOT / "upstream/src/crates/concat/ui/timeline/model.slint").read_text()
        for control in ("strength", "duration", "speed", "intensity"):
            self.assertIn("pub " + control + ": f64", physics)
            # Agent 4 moved preset buttons into the Animations tab while
            # keeping all four Inspector knobs backed by shared Keyframes state.
            self.assertIn("Keyframes.physics-" + control, ui)
            self.assertIn("in-out property <float> physics-" + control, timeline_ui)
        self.assertIn("pub fn command_with(", physics)
        self.assertIn("state.apply_physics_motion(index, strength, duration, speed, intensity)", lib)
        self.assertEqual(media_ui.count("Keyframes.apply-physics-bulk("), 4)
        self.assertIn("Keyframes.physics-intensity", media_ui)


if __name__ == "__main__":
    unittest.main()
