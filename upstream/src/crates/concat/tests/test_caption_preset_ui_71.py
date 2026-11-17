#!/usr/bin/env python3
"""Caption Issue #71 source integration checks (no EXE/image build required)."""
from pathlib import Path
import tomllib
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

    def test_apply_all_matches_pr76_worker_v1_contract(self):
        body = STUDIO.split("pub fn apply_caption_preset", 1)[1].split("fn add_title", 1)[0]
        for token in ('"model": "small"', '"wordTimestamps": true',
                      '"renderAfter": true', '"preset": preset',
                      '"position": gpu_position', '"style": gpu_style',
                      "fields.retain(|field, _|", '"style": wire_style',
                      "SetGpuCaptionRequest { request: None }"):
            self.assertIn(token, body)
        # Native project retains all user controls, but worker request must
        # exclude fields rejected by PR #76's STYLE_FIELDS validator.
        native_only = ('"italic"', '"align"', '"lineHeight"', '"tracking"')
        gpu_only = body.split("fields.retain(|field, _|", 1)[1].split("));", 1)[0]
        for key in native_only:
            self.assertNotIn(key, gpu_only)
        for key in ('"fontFamily"', '"fontWeight"', '"color"', '"background"',
                    '"backgroundRadius"', '"backgroundPaddingX"',
                    '"backgroundPaddingY"', '"karaokeColor"'):
            self.assertIn(key, gpu_only)
        self.assertIn('"x": (0.5 + position.get("offsetX")', body)
        self.assertIn('"y": (0.5 + position.get("offsetY")', body)

    def test_all_nine_text_caption_presets_have_native_ids_and_no_canvas_bars(self):
        paths = sorted((ROOT / "text-presets").glob("cuda-*/preset.toml"))
        self.assertEqual(len(paths), 9)
        ids = set()
        for path in paths:
            with path.open("rb") as f:
                item = tomllib.load(f)
            self.assertTrue(item["id"].startswith("cuda-caption."))
            self.assertNotIn(item["id"], ids)
            ids.add(item["id"])
            self.assertIn(item["id"], EDITING)
            self.assertLessEqual(len(item["name"]), 70)
        self.assertIn("cuda-caption.preset-2-yellow-bar", ids)
        with (ROOT / "text-presets/cuda-preset-2-yellow-bar/preset.toml").open("rb") as f:
            yellow = tomllib.load(f)
        self.assertEqual(yellow["style"]["background"], "#ffff00")
        self.assertEqual(yellow["style"]["color"], "#000000")
        self.assertEqual(yellow["style"]["fontWeight"], 900)
        self.assertIn('p.id.starts_with("cuda-caption.")', PREVIEW)
        self.assertIn('self.host', PREVIEW)
        self.assertIn('titles', PREVIEW)
        self.assertIn("CaptionPresetPreview.applied", UI)
        self.assertIn("CaptionPresetPreview.chosen", UI)

    def test_imported_gpu_artifacts_preserve_preset_and_word_timing(self):
        artifacts = (PROJECT / "gpu_captions.rs").read_text(encoding="utf-8")
        self.assertIn('"cuda-caption.preset-2-yellow-bar"', artifacts)
        self.assertIn('"cuda-caption.karaoke-highlight"', artifacts)
        self.assertIn("offset_y: Some(0.35)", artifacts)
        self.assertNotIn("offset_y: Some(if purple { 0.28 } else { -0.35 })", artifacts)
        self.assertIn("styled.content = generated.content.clone()", EDITING)
        self.assertIn("styled.karaoke_words = generated.karaoke_words.clone()", EDITING)
        self.assertIn("clip.offset_y = y;", EDITING)

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
