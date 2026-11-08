"""Native editor contract for safe Compound Clip rename/delete workflows."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class CompoundEditorUiTests(unittest.TestCase):
    def test_rename_modal_wired_through_native_application(self):
        ui = (ROOT / "upstream/src/crates/concat/ui/app.slint").read_text()
        lib = (ROOT / "upstream/src/crates/concat/src/lib.rs").read_text()
        studio = (ROOT / "upstream/src/crates/concat/src/studio.rs").read_text()
        actions = (ROOT / "upstream/src/crates/concat/src/studio/compound.rs").read_text()
        for prop in ("compound-rename-open", "compound-rename-name"):
            self.assertIn(prop, ui)
        for callback in ("compound-rename-edited", "compound-rename-apply",
                         "compound-rename-dismiss"):
            self.assertIn(callback, ui)
        for callback in ("on_compound_rename_edited", "on_compound_rename_apply",
                         "on_compound_rename_dismiss"):
            self.assertIn(callback, lib)
        self.assertIn('"compound-rename" => self.prompt_compound_rename(id)', studio)
        self.assertIn("Command::RenameCompound", actions)

    def test_member_view_and_exit_are_wired(self):
        editor = (ROOT / "upstream/src/crates/concat/ui/editor.slint").read_text()
        pane = (ROOT / "upstream/src/crates/concat/ui/workspace/timeline-pane.slint").read_text()
        seat = (ROOT / "upstream/src/crates/concat/ui/workspace/seat.slint").read_text()
        lib = (ROOT / "upstream/src/crates/concat/src/lib.rs").read_text()
        studio = (ROOT / "upstream/src/crates/concat/src/studio.rs").read_text()
        compound = (ROOT / "upstream/src/crates/concat/src/studio/compound.rs").read_text()
        self.assertIn("callback compound-edit-exit()", editor)
        self.assertIn("Back to parent", pane)
        self.assertIn("Editor.compound-edit-exit()", seat)
        self.assertIn("on_compound_edit_exit", lib)
        self.assertIn("visible_compound_clips()", studio)
        self.assertIn("compound_view_clips(", compound)
        self.assertIn('"compound-open" => self.open_compound_from(id)', studio)

    def test_delete_group_requires_confirmation_and_keeps_source(self):
        studio = (ROOT / "upstream/src/crates/concat/src/studio.rs").read_text()
        actions = (ROOT / "upstream/src/crates/concat/src/studio/compound.rs").read_text()
        self.assertIn('"compound-delete" => self.ask_delete_compound(id)', studio)
        self.assertIn("Confirm::RemoveCompound", studio)
        self.assertIn("self.confirm = Some(Confirm::RemoveCompound", actions)
        self.assertIn("Command::UngroupCompound { compound_id }", studio)
        self.assertIn("Original video, audio, clips, effects, timing and files", studio)


if __name__ == "__main__":
    unittest.main()
