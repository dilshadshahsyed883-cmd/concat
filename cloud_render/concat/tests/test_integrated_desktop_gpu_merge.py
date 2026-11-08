"""Regression contract across the merged Queue, Physics, Transitions, Captions, and SDR Looks.

This is deliberately a source+manifest smoke check: a full Windows UI build
and live Salad GPU render remain separate acceptance tests.
"""
import json
import unittest
from pathlib import Path

from cloud_render.concat.hybrid.project import validate_gpu_manifest
from cloud_render.concat.tests.test_gpu_compat_validation import minimal_project, native_caption

ROOT = Path(__file__).resolve().parents[3]


class MergedConcatContractTests(unittest.TestCase):
    def test_transition_strength_and_word_karaoke_share_1080p_manifest(self):
        project = minimal_project()
        timeline = project["timelines"][0]
        timeline["clips"][0]["duration"] = 2.0
        timeline["clips"].append({
            "id": "next", "kind": "image", "trackId": "video",
            "mediaId": "photo", "start": 2.0, "duration": 2.0,
            "transitionIn": {
                "id": "concat.ink-drying", "duration": 0.5, "intensity": 0.35,
            },
        })
        native_caption(project, content="Hello", karaoke=True)
        self.assertEqual(validate_gpu_manifest(project)["id"], "timeline")
        timeline["video"]["width"] = 3840
        with self.assertRaisesRegex(ValueError, "1920x1080"):
            validate_gpu_manifest(project)

    def test_editor_and_worker_contracts_survive_the_combined_merge(self):
        source = ROOT / "upstream/src/crates"
        queue = (source / "concat/src/cloud_gpu_queue.rs").read_text()
        add = queue.split("fn add(&mut self, project: &str) {", 1)[1].split("fn control(", 1)[0]
        self.assertNotIn("self.data.started = true", add)

        physics = (source / "concat-project/src/physics_motion.rs").read_text()
        self.assertIn("pub struct PhysicsControls", physics)
        self.assertIn("fn strength_and_speed_change_baked_motion_without_project_extensions()", physics)

        ui = (source / "concat/ui/workspace/media-pane.slint").read_text()
        self.assertIn("Apply to Selected Randomly", ui)
        self.assertIn("Apply Favorites to All Randomly", ui)

        captions = (source / "concat/src/panes/captions.rs").read_text()
        self.assertIn("karaoke_words_aligned(&segments, subject.start, subject.speed)", captions)
        self.assertIn("Preserve ASR boundaries", captions)

        looks = json.loads((ROOT / "cloud_render/concat/hybrid/looks_catalogue.json").read_text())
        named = {look["name"]: look for look in looks}
        for label in ("4K", "8K", "HDR Look"):
            self.assertIn(label, named)
            self.assertIn("sharpen", named[label])


if __name__ == "__main__":
    unittest.main()
