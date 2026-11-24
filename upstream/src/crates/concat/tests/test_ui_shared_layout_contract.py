#!/usr/bin/env python3
"""Regression contracts for #93 shared editor library and inspector routing.

Source-level checks only: native Slint/Rust and Windows visual tests are separate.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui/workspace"
SRC = ROOT / "src"


def source(path):
    return path.read_text(encoding="utf-8")


class SharedLayoutContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = source(UI / "media-pane.slint")
        cls.inspector = source(UI / "config-pane.slint")
        cls.canvas = source(UI / "canvas-presets.slint")
        cls.particle = source(UI / "particle-effects.slint")
        cls.caption = source(UI / "caption-preset-preview.slint")
        cls.caption_rs = source(SRC / "studio/caption_preview.rs")

    def test_all_library_inspector_routes_have_explicit_context(self):
        for page in range(1, 10):
            self.assertIn("LibraryFocus.page == " + str(page), self.inspector)
        self.assertIn("LibraryFocus.page = -1;", self.inspector)
        self.assertIn("changed project-folder =>", self.inspector)
        self.assertIn("changed selected =>", self.inspector)
        self.assertIn("root.show-library();", self.media)

    def test_cards_inspect_without_changing_apply_semantics(self):
        self.assertIn("callback inspected();", self.media)
        self.assertIn("callback favourited(on: bool);", self.media)
        self.assertIn("root.inspected();", self.media)
        self.assertIn("Library.favourite(entry.id, on);", self.media)
        self.assertIn("auditions: false;", self.media)
        self.assertIn("root.apply-effect(id);", self.media)
        self.assertIn("applied(id, label) => { root.apply-transition(id); }", self.media)
        self.assertIn("payload-kind: \"filter\";", self.media)
        self.assertIn("auditioned(id) => { root.audition-filter(id); }", self.media)

    def test_scrollable_settings_with_fixed_right_apply_footers(self):
        for marker in ("CanvasPresetApplyBar", "ParticleApplyBar",
                       "CaptionPresetApplyBar"):
            self.assertIn(marker, self.inspector)
        self.assertIn("vertical-stretch: 1;", self.inspector)
        self.assertIn("if LibraryFocus.page == 8 && CanvasPresets.selected != 0: CanvasPresetApplyBar", self.inspector)
        self.assertIn("if LibraryFocus.page == 9: ParticleApplyBar", self.inspector)
        self.assertIn("if LibraryFocus.page == 6 && GpuCompatibility.enabled && CaptionPresetPreview.chosen != \"\": CaptionPresetApplyBar", self.inspector)
        self.assertIn('label: "Apply Selected"', self.canvas)
        self.assertIn('label: "Apply All"', self.canvas)
        for label in ("Apply Selected", "Apply All", "Random All",
                      "Random Selected", "Favorites Randomly"):
            self.assertIn('label: "' + label + '"', self.particle)
        self.assertIn("if root.tab == 6: VerticalLayout {", self.media)
        self.assertNotIn("if root.tab == 6: PaneScroll {", self.media)

    def test_caption_favourites_do_not_stage_or_apply(self):
        self.assertIn("callback toggle-favourite(id: string);", self.caption)
        self.assertIn("callback filter-favourites(on: bool);", self.caption)
        self.assertIn("CaptionPresetPreview.toggle-favourite(card.id);", self.caption)
        self.assertIn("CaptionPresetPreview.filter-favourites(", self.caption)
        self.assertIn("CaptionPresetPreview.stage(card.id);", self.caption)
        self.assertIn("ui.on_toggle_favourite(|id|", self.caption_rs)
        self.assertIn("studio.library_favourite(id.as_str(), !starred);", self.caption_rs)
        self.assertIn("ui.on_filter_favourites(|only|", self.caption_rs)
        self.assertIn("ui.set_favourites(", self.caption_rs)
        self.assertIn("ui.set_cards(", self.caption_rs)
        self.assertIn("caption_preview.card_model", self.caption_rs)
        self.assertIn("self.prefs.favourites", self.caption_rs)

    def test_favourites_and_no_results_for_all_real_catalogues(self):
        for key in ("No starred transitions", "No starred video effects",
                    "No starred audio effects", "No starred filters",
                    "No starred LUTs"):
            self.assertIn(key, self.media)
        self.assertIn("No starred Canvas presets", self.canvas)
        self.assertIn("No starred particle presets", self.particle)
        self.assertIn("No starred caption presets", self.caption)
        self.assertNotIn('label: I18n.t("mediaPane.templates");', self.media)

    def test_canvas_presets_retain_legacy_ids_and_distinct_single_media_families(self):
        for name in ("Sports Frame", "Orange Frame", "Adaptive Blur (Blurred Fill)",
                     "Gallery Mat", "Floating Card", "Photo Print"):
            self.assertIn(name, self.canvas)
        for retired in ("Cinema Frame", "Editorial Panel"):
            self.assertNotIn(retired, self.canvas)
        model = source(ROOT.parent / "concat-project/src/canvas_preset.rs")
        self.assertIn("pub const ACTIVE_PRESETS: [u32; 13] = [1, 2, 3, 4, 5, 6, 9, 12, 13, 14, 15, 16, 18];", model)
        self.assertIn("7 => { // Cinema Frame", model)
        self.assertIn("8 => { // Editorial Panel", model)
        self.assertIn('property <[string]> names:', self.canvas)
        self.assertIn('property <[int]> ids: [1, 2, 3, 4, 5, 6, 9, 12, 13, 14, 15, 16, 18];', self.canvas)
        self.assertIn('CanvasPresets.toggle-favourite(root.ids[index]);', self.canvas)
        for name in ("Broadcast Edge", "Focus Island", "Diagonal Cut", "Projector Slide",
                     "Browser Window", "Taped Memory", "Broken Grid"):
            self.assertIn(name, self.canvas)
        self.assertIn('CanvasPresets.edit-number("borderWidth", v);', self.canvas)
        self.assertIn('CanvasPresets.edit-colour("borderColor", c);', self.canvas)
        self.assertIn('CanvasPresets.edit-number("rotation", v);', self.canvas)
        self.assertIn('minimum: 0; maximum: 0.1;', self.canvas)
        self.assertIn('CanvasPresets.save-default(CanvasPresets.profile);', self.canvas)
        self.assertIn('CanvasPresets.reset-default();', self.canvas)
        self.assertIn('if root.advanced-open: Rectangle', self.canvas)
        for excluded in ("Split Screen", "Picture-in-Picture",
                         "Three-Panel Story", "Collage Grid"):
            self.assertNotIn(excluded, self.canvas)
        self.assertIn('["Transform", "Mirror / Flip", "Canvas", "Canvas Presets", "Content Zoom / Crop"]', self.media)
        self.assertIn('root.adjustment-page == 3: CanvasPresetLibrary', self.media)
        self.assertIn('if LibraryFocus.page == 8 && CanvasPresets.selected != 0: CanvasPresetApplyBar', self.inspector)

    def test_canvas_none_inspects_and_clear_has_its_own_footer(self):
        none = self.canvas.split('label: CanvasPresets.selected == 0', 1)[1].split('if root.favourites-only', 1)[0]
        self.assertIn('CanvasPresets.selected = 0;', none)
        self.assertNotIn('CanvasPresets.clear(', none)
        self.assertNotIn('CanvasPresets.apply(', none)
        self.assertIn('if LibraryFocus.page == 8 && CanvasPresets.selected == 0: CanvasPresetClearBar', self.inspector)
        self.assertIn('if LibraryFocus.page == 8 && CanvasPresets.selected != 0: CanvasPresetApplyBar', self.inspector)
        self.assertIn('CanvasPresets.clear(true, root.images, root.videos);', self.canvas)
        self.assertIn('CanvasPresets.clear(false, root.images, root.videos);', self.canvas)

    def test_stable_canvas_and_particle_target_scopes(self):
        self.assertIn("CanvasPresets.apply(CanvasPresets.profile, false, root.images, root.videos);", self.canvas)
        self.assertIn("CanvasPresets.apply(CanvasPresets.profile, true, root.images, root.videos);", self.canvas)
        for mode, selected in ((0, "true"), (0, "false"),
                               (1, "true"), (1, "false")):
            call = "ParticleBulk.apply(root.preset, {}, {}, root.images, root.videos);".format(mode, selected)
            self.assertIn(call, self.particle)
        self.assertIn("ParticleBulk.apply(root.preset, 2, Keyframes.animation-scope == 1", self.particle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
