#!/usr/bin/env python3
"""Caption Issue #71 source integration checks (no EXE/image build required)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent / "concat-project" / "src"
STUDIO = (ROOT / "src/studio.rs").read_text(encoding="utf-8")
PREVIEW = (ROOT / "src/studio/caption_preview.rs").read_text(encoding="utf-8")
UI = (ROOT / "ui/workspace/caption-preset-preview.slint").read_text(encoding="utf-8")
PRESETS = (ROOT / "src/presets.rs").read_text(encoding="utf-8")
STAGING = (PROJECT / "caption_preview.rs").read_text(encoding="utf-8")
EDITING = (PROJECT / "commands/editing.rs").read_text(encoding="utf-8")
COMMANDS = (PROJECT / "commands/mod.rs").read_text(encoding="utf-8")


class CaptionPresetRegression(unittest.TestCase):
    def test_all_can_apply_with_zero_caption_segments(self):
        self.assertIn("ui.set_can_apply_all(true)", PREVIEW)
        self.assertIn("Command::SetCaptionPresetSettings", STUDIO)
        self.assertIn('settings: serde_json::json!({', STUDIO)
        self.assertIn("sourceClipId", STUDIO)
        self.assertIn('generateOnGpu', STUDIO)
        self.assertIn("SetCaptionPresetSettings { settings: serde_json::Value }", COMMANDS)

    def test_no_synthetic_words_are_saved_as_project_captions(self):
        body = STUDIO.split("pub fn apply_caption_preset", 1)[1].split("fn add_title", 1)[0]
        self.assertNotIn("AddTextClip", body)
        self.assertNotIn("SetClipTransform", body)
        self.assertIn('fields.remove("content")', body)
        self.assertIn('fields.remove("karaokeWords")', body)
        self.assertIn("Command::Batch { commands }", body)
        self.assertIn('style.contains_key("content")', EDITING)
        self.assertIn('style.contains_key("karaokeWords")', EDITING)

    def test_bottom_center_and_manual_position_preservation(self):
        self.assertIn('if file.id.starts_with("cuda-caption.") { Some(0.35) }', PRESETS)
        self.assertNotIn('clip.offset_y = -0.35;', STAGING)
        self.assertIn('clip.offset_y = offset_y.unwrap_or(0.35);', STAGING)
        self.assertIn('"anchor": "bottom-center"', STUDIO)
        self.assertIn('"safeMargin": 0.08', STUDIO)

    def test_apply_all_uses_existing_worker_schema(self):
        body = STUDIO.split("pub fn apply_caption_preset", 1)[1].split("fn add_title", 1)[0]
        for token in ('"model": "small"', '"wordTimestamps": true', '"renderAfter": true',
                      '"classic"', '"purple-word"', '"yellow-bar"',
                      "SetGpuCaptionRequest { request: None }"):
            self.assertIn(token, body)
        self.assertIn('Some("purple-word" | "yellow-bar" | "classic")', EDITING)

    def test_imported_srt_lane_is_eligible_only_when_explicitly_selected(self):
        body = STUDIO.split("pub fn apply_caption_preset", 1)[1].split("fn add_title", 1)[0]
        self.assertIn("selected_caption_lane", body)
        self.assertIn("selected_caption_lane.as_deref() == Some(clip.track_id.as_str())", body)
        self.assertIn('!clip.extra.contains_key("canvasOwner")', body)
        self.assertIn('clip.extra.contains_key("gpuCaptionSource")', body)

    def test_stage_and_applied_state_are_separate(self):
        for token in ("on_style_font", "on_style_color", "applied_caption_id",
                      "caption_effective_style", "ui.set_applied",
                      "ui.set_previewing", "style_override"):
            self.assertIn(token, PREVIEW)
        for token in ("CaptionPresetPreview.applied", "CaptionPresetPreview.previewing",
                      "Apply to All Captions", "Apply to Selected Captions",
                      "Select an existing caption clip",
                      "CaptionPresetPreview.stage(card.id)",
                      "CaptionPresetPreview.cancel()"):
            self.assertIn(token, UI)


if __name__ == "__main__":
    unittest.main(verbosity=2)
