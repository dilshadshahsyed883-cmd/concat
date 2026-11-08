#!/usr/bin/env python3
"""Focused desktop UI contract checks; no native EXE or GPU required."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MEDIA = (ROOT / "ui/workspace/media-pane.slint").read_text(encoding="utf-8")
INSPECTOR = (ROOT / "ui/inspector/clip-inspector.slint").read_text(encoding="utf-8")
KEYFRAMES = (ROOT / "ui/timeline/model.slint").read_text(encoding="utf-8")
SEAT = (ROOT / "ui/workspace/seat.slint").read_text(encoding="utf-8")
LIB = (ROOT / "src/lib.rs").read_text(encoding="utf-8")
BULK = (ROOT / "src/studio/bulk_presets.rs").read_text(encoding="utf-8")
APP = (ROOT / "ui/app.slint").read_text(encoding="utf-8")

PHYSICS = (
    "Spring Zoom", "Pendulum Swing", "Gravity Bounce",
    "Elastic Pop", "Inertia Glide", "Damped Shake",
)
TABS = (
    '"common.media"', '"C Builder"', '"common.text"',
    '"mediaPane.transitions"', '"Animations"', '"mediaPane.filters"',
    '"mediaPane.templates"', '"common.effects"',
)


class DesktopUiContractTests(unittest.TestCase):
    def test_exact_eight_gpu_tabs_without_audio(self):
        start = MEDIA.index("                        LibraryTab {")
        end = MEDIA.index("// The end buttons", start)
        strip = MEDIA[start:end]
        indices = [strip.index(tab) for tab in TABS]
        self.assertEqual(indices, sorted(indices))
        self.assertNotIn('label: I18n.t("common.audio");', strip)
        self.assertIn('label: "Sounds & Voices";', MEDIA)
        self.assertIn('root.tab == 0 && root.media-audio-view', MEDIA)
        self.assertIn("Sounds.mine", MEDIA)
        self.assertIn("Sounds.concat", MEDIA)
        self.assertIn("VoiceList {", MEDIA)

    def test_six_physics_presets_live_only_in_animations(self):
        self.assertIn("if root.tab == 10 && GpuCompatibility.enabled", MEDIA)
        for name in PHYSICS:
            self.assertIn('"' + name + '"', MEDIA)
            self.assertNotIn('label: "' + name + '";', INSPECTOR)
        self.assertIn("Keyframes.toggle-physics-favourite(index);", MEDIA)

    def test_type_scope_and_shared_inspector_controls(self):
        for typ in ("images", "videos"):
            self.assertIn("checked: root.physics-" + typ + ";", MEDIA)
        self.assertIn("root.physics-scope == 1", MEDIA)
        self.assertIn("enabled: root.physics-images || root.physics-videos", MEDIA)
        for knob in ("strength", "duration", "speed", "intensity"):
            self.assertIn("value <=> Keyframes.physics-" + knob + ";", INSPECTOR)
            self.assertIn("in-out property <float> physics-" + knob + ": 1;", KEYFRAMES)
        self.assertIn("callback apply-physics-bulk(", KEYFRAMES)
        self.assertIn("keys.on_apply_physics_bulk(", LIB)
        self.assertIn("pub fn apply_physics_bulk(", BULK)
        self.assertIn("requested_picture(clip, images, videos)", BULK)
        self.assertIn("if selected_only && selected.is_empty()", BULK)
        self.assertIn("Command::Batch { commands }", BULK)

    def test_generate_captions_reuses_existing_sheet(self):
        self.assertIn('label: "Generate Captions";', MEDIA)
        self.assertIn("root.generate-captions();", MEDIA)
        self.assertIn("generate-captions => { Editor.captions(); }", SEAT)
        self.assertIn("CaptionsDialog {", APP)
        self.assertIn("captions-import-srt", APP)
        self.assertIn("captions-export-srt", APP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
