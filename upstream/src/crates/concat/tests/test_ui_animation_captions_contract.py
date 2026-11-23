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
    '"common.effects"', '"Adjustment"',
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
        config = (ROOT / "ui/workspace/config-pane.slint").read_text()
        layout = (ROOT / "ui/workspace/media-layout.slint").read_text()
        self.assertIn("MediaTargetControls { }", config)
        for typ in ("images", "videos"):
            self.assertIn("checked: Keyframes.animation-" + typ + ";", layout)
            self.assertNotIn("checked: Keyframes.animation-" + typ + ";", MEDIA)
        for knob in ("strength", "duration", "speed", "intensity"):
            self.assertIn("value <=> Keyframes.physics-" + knob + ";", config)
        self.assertIn("keys.on_apply_physics_bulk(", LIB)
        self.assertIn("requested_picture(clip, images, videos)", BULK)
        self.assertIn("if selected_only && selected.is_empty()", BULK)

    def test_left_layout_subtabs_and_consolidated_motion_presets(self):
        layout = (ROOT / "ui/workspace/media-layout.slint").read_text()
        self.assertIn('["Transform", "Mirror / Flip", "Canvas", "Canvas Presets", "Content Zoom / Crop"]', MEDIA)
        self.assertNotIn('root.animation-page', MEDIA)
        for name in ("Push In", "Pan Right", "Fade In"):
            self.assertIn('"' + name + '"', MEDIA)
        for field in ("Scale", "Position X", "Position Y", "Rotation", "Opacity"):
            self.assertIn('label: "' + field + '"', layout)
        self.assertIn("Backdrop.apply-bulk(", layout)
        self.assertIn("keys.on_apply_adjustment(", LIB)
        self.assertIn("backdrop.on_apply_bulk(", LIB)

    def test_animation_and_caption_views_have_no_nested_scrollers(self):
        pages = MEDIA.split("pages := PaneScroll {", 1)[1]
        config = (ROOT / "ui/workspace/config-pane.slint").read_text()
        self.assertNotIn("if root.tab == 10 && GpuCompatibility.enabled: PaneScroll {", pages)
        self.assertIn("if root.tab == 10 && GpuCompatibility.enabled: VerticalLayout {", pages)
        self.assertIn("if root.tab == 1 && GpuCompatibility.enabled && root.text-category == 1: CaptionPresetPreviewPanel {", pages)
        self.assertIn("CanvasPresetLibrary {", pages)
        self.assertNotIn("MediaLayoutPanel {", pages)
        self.assertIn("MediaLayoutPanel {", config)
        self.assertIn("CanvasMotionPanel", config)
        self.assertIn("CanvasPresetPanel {", config)
        self.assertIn("AnimationApplyBar { }", config)
        self.assertIn("library-only: true;", pages)
        self.assertIn("inspector-only: true;", config)

    def test_explicit_transform_canvas_scopes_and_selection_fallback(self):
        layout = (ROOT / "ui/workspace/media-layout.slint").read_text()
        actions = (ROOT / "ui/workspace/animation-actions.slint").read_text()
        self.assertIn("root.apply(true);", layout)
        self.assertIn("root.apply(false);", layout)
        self.assertIn('options: ["Framing…", "Fit", "Fill"];', layout)
        self.assertIn('root.apply(true,0)', actions)
        self.assertIn('root.apply(false,0)', actions)
        self.assertIn('Keyframes.animation-scope == 1', actions)
        self.assertIn('Select eligible timeline media first.', BULK)

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
        actions = (ROOT / "ui/workspace/animation-actions.slint").read_text()
        presets = (ROOT / "ui/workspace/canvas-presets.slint").read_text()
        # Animation None exposes explicit clear wording while normal presets
        # retain the original Apply Selected / Apply All actions.
        for label in (
            'label: Keyframes.animation-selected == -1 ? "Clear Selected Animation" : "Apply Selected"',
            'label: Keyframes.animation-selected == -1 ? "Clear All Eligible Animations" : "Apply All"',
        ):
            self.assertIn(label, actions)
        for label in ('label: "Apply Selected"', 'label: "Apply All"'):
            self.assertIn(label, presets)
        self.assertIn("Keyframes.clear-generated-motion(", actions)
        self.assertIn("Keyframes.animation-videos,selected,Keyframes.canvas-motion", actions)
        self.assertIn("CanvasPresets.apply(CanvasPresets.profile, true", presets)
        self.assertIn('label: "Apply Favorites Randomly"', presets)

    def test_none_clear_selection_never_implicitly_edits_other_media(self):
        actions = (ROOT / "ui/workspace/animation-actions.slint").read_text()
        config = (ROOT / "ui/workspace/config-pane.slint").read_text()
        model = (ROOT / "ui/timeline/model.slint").read_text()
        project = (ROOT.parent / "concat-project/src/motion_preset.rs").read_text()
        properties = (ROOT.parent / "concat-project/src/commands/properties.rs").read_text()

        self.assertIn('Keyframes.inspect(-1);', MEDIA)
        self.assertIn('Keyframes.animation-selected == -1', actions)
        self.assertIn('Keyframes.clear-generated-motion(', actions)
        self.assertIn('callback clear-generated-motion(', model)
        self.assertIn('keys.on_clear_generated_motion(', LIB)
        self.assertIn('clear_generated_motion_bulk(', BULK)
        self.assertIn('generated_ownership(clip).is_some()', BULK)
        self.assertIn('pub fn clear_generated(', project)
        self.assertIn('Command::ClearGeneratedMotion', properties)

        self.assertIn('LibraryFocus.selected-id = "__none_transition__";', MEDIA)
        self.assertIn('LibraryFocus.selected-id = "__none_effect__";', MEDIA)
        self.assertIn('label: "Remove Selected Transition"', config)
        self.assertIn('root.transition-remove();', config)
        self.assertIn('label: "Remove " + entry.name;', config)
        self.assertIn('root.chain-remove(false, index);', config)
        self.assertIn('LibraryFocus.selected-id != "__none_effect__"', config)
        # No None selection may be forwarded through the normal Apply IDs.
        self.assertNotIn('root.apply-transition("__none_transition__")', MEDIA)
        self.assertNotIn('root.apply-effect("__none_effect__")', MEDIA)

    def test_generate_captions_reuses_existing_sheet(self):
        self.assertIn('label: "Generate Captions";', MEDIA)
        self.assertIn("root.generate-captions();", MEDIA)
        self.assertIn("generate-captions => { Editor.captions(); }", SEAT)
        self.assertIn("CaptionsDialog {", APP)
        self.assertIn("captions-import-srt", APP)
        self.assertIn("captions-export-srt", APP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
