"""Issue #65 source contracts; actual slider interaction needs Windows acceptance."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
SRC = ROOT / "src"


class CBuilderLinkedUiContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = (UI / "workspace/c-builder-home.slint").read_text(encoding="utf-8")
        cls.pane = (UI / "workspace/c-builder.slint").read_text(encoding="utf-8")
        cls.selection = (UI / "workspace/c-builder-selection.slint").read_text(encoding="utf-8")
        cls.editor = (UI / "editor.slint").read_text(encoding="utf-8")
        cls.studio = (SRC / "studio.rs").read_text(encoding="utf-8")
        cls.lib = (SRC / "lib.rs").read_text(encoding="utf-8")
        cls.builder_home = (SRC / "studio/c_builder_home.rs").read_text(encoding="utf-8")

    def test_c_builder_remains_home_only_and_keeps_output_actions(self):
        self.assertIn("CBuilderPane {", self.home)
        self.assertNotIn("CBuilderPane {", self.editor)
        for phrase in ("Home", "Output Folder", "Open in Editor", "Retry used-media"):
            self.assertIn(phrase, self.home)
        for phrase in ("used-pending", "cb-project-name", "cb-name-preview", "cb-generate-project"):
            self.assertIn(phrase, self.home)

    def test_linked_rails_are_atomically_persisted_for_both_media_types(self):
        self.assertIn("rail: true", self.selection)
        self.assertIn("Each enabled keyword shares one 100% budget", self.selection)
        self.assertIn("root.changed(false, keyword, selected, weight)", self.selection)
        self.assertIn("root.changed(true, keyword, selected, weight)", self.selection)
        self.assertIn("root.equalize(false)", self.selection)
        self.assertIn("root.equalize(true)", self.selection)
        self.assertIn("selection.change_keyword(video,keyword,selected", self.studio)
        self.assertIn("selection.normalize()", self.studio)
        self.assertIn("editor.on_cb_equalize_keywords", self.lib)

    def test_compact_tiles_separate_toggle_from_percentage_popup(self):
        tile = self.selection.split("component KeywordTile", 1)[1].split("component KeywordGrid", 1)[0]
        body = tile.split("body-touch := TouchArea", 1)[1].split("chip := Rectangle", 1)[0]
        chip = tile.split("percent-touch := TouchArea", 1)[1].split("share-popup := PopupWindow", 1)[0]
        self.assertIn("root.changed(!root.entry.selected", body)
        self.assertNotIn("share-popup.show()", body)
        self.assertIn("share-popup.show()", chip)
        self.assertNotIn("root.changed(", chip)
        self.assertIn("Theme.accent-soft", tile)
        self.assertIn("root.entry.available", tile)
        self.assertIn("if !root.entry.selected { share-popup.close(); }", tile)
        self.assertIn("changed busy", tile)
        self.assertIn('label: "Done"', tile)
        self.assertIn("close-on-click-outside", tile)
        self.assertIn("root.width >= 720px ? 3 : root.width >= 480px ? 2 : 1", self.selection)
        self.assertIn("for entry[index] in root.entries: KeywordTile", self.selection)
        self.assertIn("KeywordGrid { entries: root.images", self.selection)
        self.assertIn("KeywordGrid { entries: root.videos", self.selection)
        self.assertNotIn("component KeywordRow", self.selection)

    def test_overall_ratio_and_unchecked_rails(self):
        self.assertIn("Overall Media Ratio", self.selection)
        self.assertIn("enabled: !root.busy && root.entry.selected", self.selection)
        self.assertIn("Editor.cb-overall-video-ratio-changed(value)", self.home)
        self.assertIn("editor.on_cb_overall_video_ratio_changed", self.lib)
        self.assertEqual(self.lib.count("profile.overall_video_percent=Some("), 2)

    def test_compact_planning_form_is_wired_to_backend(self):
        for label in (
            "Media Library", "Timeline Builder", "Media Selection", "Preview Plan", "Reroll",
            "Generate Native Concat Timeline", "Apply Planning Settings",
            "Opening video %", "Ending video %", "Image min", "Image max",
            "Video min", "Video max", "Max video run", "Max image run",
            "Repeat cooldown", "Random seed",
        ):
            self.assertIn(label, self.pane)
        for label in ("Planning profile","Profile name","Advanced / Edit JSON","Pin Preview Shot","Clear Pins","Delete Saved",'label: "Store"','label: "Duplicate"','label: "Rename"','label: "Import"','label: "Export"'):
            self.assertNotIn(label,self.pane)
        self.assertIn('Param { label: "Opening video %"',self.pane)
        self.assertIn('Param { label: "Ending video %"',self.pane)
        self.assertIn("save_planning_settings",self.lib)
        self.assertIn("load_planning_settings",self.studio)
        self.assertIn("simplify_planning",self.lib)
        for label in ("Select Audio","Use Audio Name","root.name-preview","root.status","root.narration-duration"):
            self.assertIn(label,self.pane)
        for label in ('label:"Media Library"','label:"Timeline Builder"','label:"Media Selection"'):
            self.assertLess(self.home.index(label),self.home.index("        PaneScroll {"))
        self.assertIn("page <=> root.page",self.home)
        self.assertNotIn('label: "Timeline Builder"',self.pane)
        self.assertIn("callback tune-profile(", self.pane)
        self.assertIn("Editor.cb-tune-profile(", self.home)
        self.assertIn("callback cb-tune-profile(", self.editor)
        self.assertIn("editor.on_cb_tune_profile(", self.lib)
        self.assertIn("profile.validate()", self.lib)

    def test_dedicated_intro_library_parity(self):
        self.assertIn('Intro Clips Library', self.pane)
        self.assertIn('Editor.cb-import-intro-folder()', self.home)
        self.assertIn('KeywordGrid {entries:root.intro-videos', self.selection)
        self.assertIn('Equalize Intro Videos', self.selection)
        self.assertIn('c_builder::plan_profile_with_intro', self.lib)
        self.assertIn('finish_libraries', self.lib)
        self.assertIn('c_builder-intro-cache.json', self.lib)
        self.assertIn('profile.intro_selection.get_or_insert_with', self.studio)

    def test_video_only_intro_is_wired(self):
        self.assertIn('Param { label: "Video-only intro (0 = off)"', self.pane)
        self.assertIn('Math.floor(root.narration-duration / 3)', self.pane)
        self.assertIn('root.tune-closing, root.tune-intro,', self.pane)
        self.assertIn('plan-intro: Editor.cb-plan-intro', self.home)
        self.assertIn('cb-plan-intro:0', self.editor)
        self.assertIn('set_cb_plan_intro', self.studio)
        self.assertIn('profile.video_intro_seconds=intro.round()', self.lib)

    def test_keyword_reset_only_after_verified_archive(self):
        finished = self.builder_home.split("pub fn builder_archive_finished", 1)[1].split("pub fn builder_retry_used", 1)[0]
        success, failure = finished.split("match finish.result", 1)[1].split("Err(error)=>", 1)
        self.assertIn("reset_keyword_selections_after_build()", success)
        self.assertNotIn("reset_keyword_selections_after_build()", failure)
        self.assertIn("remembered_keyword_weight(video,keyword)", self.studio)
        self.assertIn("forget_keyword_weight(video, keyword)", self.studio)
        self.assertIn('" saved"', self.selection)
        self.assertIn("enabled: !root.busy && root.entry.selected", self.selection)

    def test_generation_does_not_publish_detailed_plan_preview(self):
        preview = self.lib.split("editor.on_cb_plan_profile(", 1)[1].split("editor.on_cb_generate_project(", 1)[0]
        generate = self.lib.split("editor.on_cb_generate_project(", 1)[1].split("// ── the bin", 1)[0]
        self.assertIn("studio.c_builder.preview=summary", preview)
        self.assertIn("studio.c_builder.preview_shots=shots", preview)
        self.assertLess(generate.index("studio.c_builder.preview.clear()"), generate.index("host::spawn("))
        self.assertLess(generate.index("studio.c_builder.preview_shots.clear()"), generate.index("host::spawn("))
        self.assertNotIn("studio.c_builder.preview=", generate)
        self.assertIn("studio.builder_archive_finished(pending,finish)", generate)

    def test_legacy_and_generation_paths_use_canonical_shares(self):
        self.assertIn("if let Some(selection)=profile.media_selection.as_mut(){selection.normalize();}", self.lib)
        self.assertIn("profile.media_selection.get_or_insert_with(Default::default).normalize();", self.lib)
        self.assertIn("studio.builder_archive_finished(pending,finish)", self.lib)
        self.assertIn("studio.c_builder.effective_name()", self.lib)
        self.assertIn("studio.c_builder.profile_json = encoded.clone()", self.lib)


if __name__ == "__main__":
    unittest.main()
