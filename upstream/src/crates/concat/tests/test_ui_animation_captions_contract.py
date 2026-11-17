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
    '"common.media"', '"common.text"',
    '"mediaPane.transitions"', '"Animations"', '"mediaPane.filters"',
    '"common.effects"', '"mediaPane.templates"',
)


class DesktopUiContractTests(unittest.TestCase):
    def test_exact_seven_editor_tabs_with_home_only_builder(self):
        start = MEDIA.index("                        LibraryTab {")
        end = MEDIA.index("// The end buttons", start)
        strip = MEDIA[start:end]
        indices = [strip.index(tab) for tab in TABS]
        self.assertEqual(indices, sorted(indices))
        self.assertNotIn('label: I18n.t("common.audio");', strip)
        self.assertNotIn('label: "C Builder";', strip)
        self.assertNotIn('CBuilderPane', MEDIA)
        home=(ROOT/'ui/workspace/c-builder-home.slint').read_text(encoding="utf-8")
        self.assertIn('CBuilderPane',home)
        self.assertIn('open-c-builder',APP)
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
            self.assertIn("value <=> Keyframes.physics-" + knob + ";", MEDIA)
            self.assertIn("in-out property <float> physics-" + knob + ": 1;", KEYFRAMES)
        self.assertIn("callback apply-physics-bulk(", KEYFRAMES)
        self.assertIn("keys.on_apply_physics_bulk(", LIB)
        self.assertIn("pub fn apply_physics_bulk(", BULK)
        self.assertIn("requested_picture(clip, images, videos)", BULK)
        self.assertIn("if selected_only && selected.is_empty()", BULK)
        self.assertIn("Command::Batch { commands }", BULK)

    def test_left_layout_subtabs_and_consolidated_motion_presets(self):
        layout = (ROOT / "ui/workspace/media-layout.slint").read_text(encoding="utf-8")
        self.assertIn('["Animations", "Transform", "Canvas"]', MEDIA)
        for name in ("Push In", "Pan Right", "Fade In"):
            self.assertIn('"' + name + '"', MEDIA)
        self.assertIn('if root.animation-page == 1: MediaLayoutPanel { canvas: false; }', MEDIA)
        self.assertIn('if root.animation-page == 2:', MEDIA)
        for field in ("Scale", "Position X", "Position Y", "Rotation"):
            self.assertIn('label: "' + field + '"', layout)
        self.assertIn("Backdrop.apply-bulk(", layout)
        self.assertIn("root.images, root.videos", layout)
        self.assertIn("root.scope == 1", layout)
        self.assertIn("keys.on_apply_bulk_transform(", LIB)
        self.assertIn("backdrop.on_apply_bulk(", LIB)

    def test_animation_and_caption_views_have_no_nested_scrollers(self):
        # The outer pages viewport already owns vertical scrolling.
        pages = MEDIA.split("pages := PaneScroll {", 1)[1]
        self.assertNotIn("if root.tab == 10 && GpuCompatibility.enabled: PaneScroll {", pages)
        self.assertNotIn("if root.tab == 1 && GpuCompatibility.enabled && root.text-category == 1: PaneScroll {", pages)
        self.assertIn("if root.tab == 10 && GpuCompatibility.enabled: VerticalLayout {", pages)
        self.assertIn("if root.tab == 1 && GpuCompatibility.enabled && root.text-category == 1: CaptionPresetPreviewPanel {", pages)
        for page in ("root.animation-page == 0", "root.animation-page == 1", "root.animation-page == 2"):
            self.assertIn(page, pages)
        for panel in ("CanvasMotionPanel", "CanvasPresetPanel", "MediaLayoutPanel { canvas: false; }", "MediaLayoutPanel { canvas: true; }"):
            self.assertIn(panel, pages)

    def test_explicit_transform_canvas_scopes_and_selection_fallback(self):
        layout = (ROOT / "ui/workspace/media-layout.slint").read_text(encoding="utf-8")
        for item in ("root.selected.count == 0", "root.selected.count > 1",
                     "root.apply(3, true);", "root.apply(3, false);",
                     "root.videos, true);", "root.videos, false);",
                     'label: "Fit";', 'label: "Fill";'):
            self.assertIn(item, layout)

    def test_caption_gallery_preview_staged_separately_from_apply(self):
        gallery = (ROOT / "ui/workspace/caption-preset-preview.slint").read_text(encoding="utf-8")
        for item in ("CaptionPresetPreview.cards.length == 0", "min-card: 144px",
                     "CaptionPresetPreview.stage(card.id);",
                     "CaptionPresetPreview.apply(false);", "CaptionPresetPreview.apply(true);",
                     "CaptionPresetPreview.cancel();", "CaptionPresetPreview.demo-toggle();"):
            self.assertIn(item, gallery)
        preview = (ROOT / "ui/workspace/preview-pane.slint").read_text(encoding="utf-8")
        self.assertIn("CaptionPreviewBadge", preview)

    def test_canvas_preset_all_actions_are_not_selection_limited(self):
        motion = (ROOT / "ui/workspace/canvas-motion.slint").read_text(encoding="utf-8")
        presets = (ROOT / "ui/workspace/canvas-presets.slint").read_text(encoding="utf-8")
        self.assertIn('label: "Apply Selected"', motion)
        self.assertIn('label: "Apply All"', motion)
        self.assertIn("root.images, root.videos, false, root.settings", motion)
        self.assertIn('label: "Apply Selected"', presets)
        self.assertIn('label: "Apply All"', presets)
        self.assertIn("CanvasPresets.apply(CanvasPresets.profile, true", presets)

    def test_generate_captions_reuses_existing_sheet(self):
        self.assertIn('label: "Generate Captions";', MEDIA)
        self.assertIn("root.generate-captions();", MEDIA)
        self.assertIn("generate-captions => { Editor.captions(); }", SEAT)
        self.assertIn("CaptionsDialog {", APP)
        self.assertIn("captions-import-srt", APP)
        self.assertIn("captions-export-srt", APP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
