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
        self.assertIn("root.changed(false, entry.keyword, selected, weight)", self.selection)
        self.assertIn("root.changed(true, entry.keyword, selected, weight)", self.selection)
        self.assertIn("root.equalize(false)", self.selection)
        self.assertIn("root.equalize(true)", self.selection)
        self.assertIn("selection.change_keyword(video,keyword,selected", self.studio)
        self.assertIn("selection.normalize()", self.studio)
        self.assertIn("editor.on_cb_equalize_keywords", self.lib)

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

    def test_legacy_and_generation_paths_use_canonical_shares(self):
        self.assertIn("if let Some(selection)=profile.media_selection.as_mut(){selection.normalize();}", self.lib)
        self.assertIn("profile.media_selection.get_or_insert_with(Default::default).normalize();", self.lib)
        self.assertIn("studio.builder_archive_finished(pending,finish)", self.lib)
        self.assertIn("studio.c_builder.effective_name()", self.lib)
        self.assertIn("studio.c_builder.profile_json = encoded.clone()", self.lib)


if __name__ == "__main__":
    unittest.main()
