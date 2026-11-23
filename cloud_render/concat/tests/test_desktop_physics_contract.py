"""Static interface contract; numerical physics assertions execute via cargo CI."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class PhysicsControlContractTests(unittest.TestCase):
    def test_parameter_controls_reach_gpu_native_keyframe_sampler(self):
        physics = (ROOT / "upstream/src/crates/concat-project/src/physics_motion.rs").read_text()
        ui = (ROOT / "upstream/src/crates/concat/ui/inspector/clip-inspector.slint").read_text()
        lib = (ROOT / "upstream/src/crates/concat/src/lib.rs").read_text()
        inspector_ui = (ROOT / "upstream/src/crates/concat/ui/workspace/config-pane.slint").read_text()
        timeline_ui = (ROOT / "upstream/src/crates/concat/ui/timeline/model.slint").read_text()
        for control in ("strength", "duration", "speed", "intensity"):
            self.assertIn("pub " + control + ": f64", physics)
            # #95 keeps the gallery on the left; typed settings live in Details.
            self.assertIn("Keyframes.physics-" + control, inspector_ui)
            self.assertNotIn("Keyframes.physics-" + control, ui)
            self.assertIn("in-out property <float> physics-" + control, timeline_ui)
        self.assertIn("pub fn command_with(", physics)
        self.assertIn("state.apply_physics_motion(index, strength, duration, speed, intensity, auto_fit)", lib)
        self.assertIn("auto-fit: bool", timeline_ui)
        self.assertIn("Keyframes.canvas-motion.auto-fit", inspector_ui)
        actions = (ROOT / "upstream/src/crates/concat/ui/workspace/animation-actions.slint").read_text()
        self.assertIn("Keyframes.apply-physics-bulk(",actions)
        for mode in ("root.apply(true,0)","root.apply(false,0)","root.apply(Keyframes.animation-scope == 1,2)","root.apply(Keyframes.animation-scope == 1,3)"):
            self.assertIn(mode,actions)
        self.assertIn("Keyframes.physics-intensity", inspector_ui)

    def test_saved_inspector_fields_do_not_read_a_moved_local(self):
        model = (ROOT / "upstream/src/crates/concat/ui/timeline/model.slint").read_text()
        restore = model.split("public function restore-current()", 1)[1].split(
            "public function inspect(", 1)[0]
        self.assertIn("animation-saved=Keyframes.load-animation(animation-selected);", restore)
        self.assertNotIn("let settings=", restore)
        for field in ("strength", "duration", "speed", "intensity", "canvas"):
            self.assertIn(f"animation-saved.{field}", restore)


if __name__ == "__main__":
    unittest.main()
