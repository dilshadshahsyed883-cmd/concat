#!/usr/bin/env python3
"""Issue #66: Home/Slint project management source and callback regressions.

Actual filesystem behavior is tested by the lightweight Rust source-check
harness, which compiles the production Rust modules directly.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOME = (ROOT / "concat/ui/start.slint").read_text(encoding="utf-8")
CONFIRM = (ROOT / "concat/ui/dialogs/confirm.slint").read_text(encoding="utf-8")
PANE = (ROOT / "concat/src/panes/start.rs").read_text(encoding="utf-8")
STUDIO = (ROOT / "concat/src/studio.rs").read_text(encoding="utf-8")
BACKEND = (ROOT / "concat-host/src/project_delete.rs").read_text(encoding="utf-8")
HOST_PROJECTS = (ROOT / "concat-host/src/projects.rs").read_text(encoding="utf-8")


class HomeProjectManagement(unittest.TestCase):
    def test_home_only_c_builder_launcher_without_removed_gpu_queue(self):
        wide = HOME.split("component WideHome inherits", 1)[1].split(
            "component NarrowHome inherits", 1
        )[0]
        narrow = HOME.split("component NarrowHome inherits", 1)[1].split(
            "export component StartScreen inherits", 1
        )[0]
        for page in (wide, narrow):
            self.assertIn('label: "C Builder";', page)
            self.assertNotIn('I18n.t("cloudGpu.gpuQueue")', page)
            self.assertIn("root.open-c-builder()", page)
            self.assertNotIn('BarAction { glyph:Glyph.slot;', page)
        self.assertNotIn("component CBuilderPane inherits", HOME)

    def test_separate_visible_delete_with_no_card_open_callback(self):
        card = HOME.split("component PosterCard inherits Pressable {", 1)[1].split(
            "component ProjectsGrid inherits Rectangle {", 1
        )[0]
        self.assertIn('label: "Delete";', card)
        self.assertIn("glyph: Glyph.trash;", card)
        self.assertIn("root.delete-requested();", card)
        self.assertIn("clicked => { root.opened(); }", card)
        self.assertIn("root.forgotten();", card)
        delete = card.split('// This is a child hit target', 1)[1]
        self.assertNotIn("root.opened()", delete)
        narrow = HOME.split("component ProjectRow inherits Pressable {", 1)[1].split(
            "component SearchField inherits", 1
        )[0]
        self.assertIn("callback delete-requested();", narrow)
        self.assertIn("root.delete-requested();", narrow)
        self.assertIn("root.forgotten();", narrow)
        self.assertIn("delete-recent(path) => { root.delete-recent(path); }", HOME)

    def test_card_metadata_has_own_rows_and_delete_space(self):
        card = HOME.split("component PosterCard inherits Pressable {", 1)[1].split(
            "component ProjectsGrid inherits Rectangle {", 1
        )[0]
        self.assertIn('text: root.project.missing ? root.project.path : root.project.detail;', card)
        self.assertIn('text: root.project.missing ? I18n.t("start.folderNotFound") : root.project.when;', card)
        self.assertIn("root.width - 2 * root.inset - 103px", card)
        self.assertIn("root.width - 94px", card)

    def test_typed_name_and_resolved_path_required_before_recycle(self):
        self.assertIn("required-name: string,", CONFIRM)
        self.assertIn("root.typed-name == root.data.required-name", CONFIRM)
        self.assertIn("enabled: root.authorized;", CONFIRM)
        self.assertIn("if (root.authorized) { root.confirmed(); }", CONFIRM)
        self.assertIn("resolved_path: target.path.to_string_lossy()", PANE)
        self.assertIn('title: "Delete Project from disk"', STUDIO)
        self.assertIn('required_name: name.as_str().into()', STUDIO)
        self.assertIn('concat_host::project_delete::recycle(', STUDIO)

    def test_delete_vs_remove_is_not_an_alias(self):
        self.assertIn("projects::forget(&studio.host.dirs.config, &path)", PANE)
        self.assertIn("concat_host::project_delete::recycle(", STUDIO)
        self.assertNotIn("concat_host::projects::forget(&self.host.dirs.config, &path)", STUDIO)
        self.assertIn("pub fn forget(", HOST_PROJECTS)
        self.assertIn("pub fn recycle(", BACKEND)
        self.assertNotIn("remove_dir_all(", BACKEND)
        self.assertIn("RecycleOption]::SendToRecycleBin", BACKEND)
        self.assertIn("Project moved to Recycle Bin, but Recent metadata", BACKEND)

    def test_no_independent_editor_c_builder_tab_is_added(self):
        app = (ROOT / "concat/ui/app.slint").read_text(encoding="utf-8")
        self.assertIn("if root.on-start && !root.c-builder-home: StartScreen", app)


if __name__ == "__main__":
    unittest.main()
