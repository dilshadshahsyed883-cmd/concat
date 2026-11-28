"""Keep pointer cancellation and the exact plan/commit boundary wired together.
Numerical placement/serialization/undo assertions execute in concat-project::drag_edit.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3] / 'upstream/src/crates/concat'

class TimelineDragUiContracts(unittest.TestCase):
    def test_exact_preview_and_commit_share_native_request(self):
        studio = (ROOT/'src/studio.rs').read_text()
        drag = studio.split('    pub fn clip_dragged(',1)[1].split('    pub fn clip_released(',1)[0]
        self.assertIn('concat_project::drag_edit::plan(self.project(),&request)',drag)
        self.assertIn('self.echo=None;',drag)
        self.assertNotIn('resolve_moves(',drag)
        release=studio.split('    pub fn clip_released(',1)[1].split('    pub fn clip_set(',1)[0]
        self.assertIn('Command::DragClips {request}',release)
        self.assertNotIn('Command::ScopedMoveClips',release)

    def test_four_direct_timeline_controls(self):
        tray=(ROOT/'ui/timeline/tray.slint').read_text()
        for action in ['close-all-gaps','spacer','group-clips','ungroup-clips']:
            self.assertIn('Editor.shortcut("'+action+'")',tray)
        studio=(ROOT/'src/studio.rs').read_text()
        self.assertIn('Command::CloseAllTrackGaps',studio)
        self.assertIn('Command::SetClipGroup',studio)
        self.assertIn('duplicate:self.drag_copy&&!self.spacer_mode',studio)

    def test_pointer_cancel_is_forwarded_and_never_committed(self):
        lanes=(ROOT/'ui/timeline/lanes.slint').read_text()
        self.assertIn('PointerEventKind.cancel {root.cancelled();}',lanes)
        self.assertIn('clip-cancelled',lanes)
        for relative in ['ui/editor.slint','ui/workspace/timeline-pane.slint','ui/workspace/seat.slint']:
            self.assertIn('clip-cancelled',(ROOT/relative).read_text())
        lib=(ROOT/'src/lib.rs').read_text()
        self.assertIn('on_clip_cancelled',lib)
        self.assertIn('state.clip_cancelled();',lib)
        self.assertIn('chord == "escape"',lib)

    def test_feedback_and_independent_modes_stay_visible(self):
        lanes=(ROOT/'ui/timeline/lanes.slint').read_text()
        tray=(ROOT/'ui/timeline/tray.slint').read_text()
        for name in ['valid','label','duration','start','row']:
            self.assertIn('TimelineDrag.feedback.'+name,lanes)
        self.assertIn('MouseCursor.not-allowed',lanes)
        self.assertIn('press-track-scroll',lanes)
        self.assertIn('TimelineDrag.ripple',tray)
        self.assertIn('TimelineDrag.duplicate',tray)
        self.assertIn('root.snap',tray)
        self.assertIn('root.magnetic',tray)

if __name__ == '__main__':
    unittest.main()
