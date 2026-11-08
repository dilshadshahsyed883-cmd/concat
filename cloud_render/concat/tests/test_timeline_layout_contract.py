"""Guard compact lanes and the true full-width bottom timeline layout."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class TimelineLayoutContract(unittest.TestCase):
    def test_compact_track_defaults_without_disabling_manual_sizes(self):
        source = (ROOT / "upstream/src/crates/concat/src/studio.rs").read_text()
        for expected in ("const LANE_AUTO: f32 = 48.0",
                         "const LANE_SMALL: f32 = 48.0",
                         "const LANE_MEDIUM: f32 = 72.0",
                         "const LANE_LARGE: f32 = 108.0",
                         "TrackSize::Auto => LANE_AUTO"):
            self.assertIn(expected, source)
        self.assertIn("TrackSize::Large => LANE_LARGE", source)

    def test_default_timeline_uses_full_window_width_and_resizes(self):
        source = (ROOT / "upstream/src/crates/concat/src/dock.rs").read_text()
        self.assertIn("ratio: 0.60", source)
        self.assertIn("second: Dock::leaf(PaneKind::Timeline)", source)
        self.assertIn("fn default_timeline_extends_under_inspector_and_resizes()", source)
        self.assertIn("assert!((timeline.width - width).abs() < 0.01)", source)


if __name__ == "__main__":
    unittest.main()
