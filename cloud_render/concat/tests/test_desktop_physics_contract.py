"""Static interface contract; numerical physics assertions execute via cargo CI."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class PhysicsControlContractTests(unittest.TestCase):
    def test_parameter_controls_reach_gpu_native_keyframe_sampler(self):
        physics = (ROOT / "upstream/src/crates/concat-project/src/physics_motion.rs").read_text()
        ui = (ROOT / "upstream/src/crates/concat/ui/inspector/clip-inspector.slint").read_text()
        lib = (ROOT / "upstream/src/crates/concat/src/lib.rs").read_text()
        for control in ("strength", "duration", "speed", "intensity"):
            self.assertIn("pub " + control + ": f64", physics)
            self.assertIn("root.physics-" + control, ui)
        self.assertIn("pub fn command_with(", physics)
        self.assertIn("state.apply_physics_motion(index, strength, duration, speed, intensity)", lib)
        self.assertEqual(ui.count("Keyframes.apply-physics-motion("), 6)


if __name__ == "__main__":
    unittest.main()
