"""Transition inspector navigation and native-document/actual ZIP regressions."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

from cloud_render.concat.hybrid.project import load_project, validate_gpu_manifest
from cloud_render.concat.project_bundle import pack, restore

ROOT = Path(__file__).resolve().parents[3]
DESKTOP = ROOT / "upstream/src/crates/concat"


class TransitionInspectorUiTests(unittest.TestCase):
    def test_duration_action_is_adjacent_responsive_and_wired_to_entered_value(self):
        text = (DESKTOP / "ui/inspector/clip-inspector.slint").read_text()
        panel = text.split("export component TransitionPanel", 1)[1].split("/// Audio:", 1)[0]
        self.assertLess(panel.index('text: I18n.t("common.duration")'),
                        panel.index('label: "Apply Duration to All"'))
        self.assertLess(panel.index('label: "Apply Duration to All"'), panel.index('label: "GPU Intensity"'))
        duration_row = panel.split("HorizontalLayout {", 2)[2].split('text: "All cuts: "', 1)[0]
        self.assertIn("NumberField {", duration_row)
        self.assertIn('label: "Apply Duration to All"', duration_row)
        self.assertIn("width: 92px;", duration_row)
        self.assertIn("step: 0.1;", duration_row)
        self.assertIn("precision: 1;", duration_row)
        self.assertIn("wrap: word-wrap;", panel)
        self.assertIn("root.bulk-duration = v; root.duration-set(v);", panel)
        self.assertIn("root.duration-all(root.bulk-duration)", panel)
        self.assertIn("root.duration-clip != root.transition.id", panel)
        # A short selected cut may clamp individually. Its actual duration must
        # not replace the typed bulk value used by the other cuts.
        self.assertIn('text: "All cuts: "', panel)
        for file, expected in (
            ("ui/workspace/config-pane.slint", "root.transition-duration-all(seconds)"),
            ("ui/workspace/seat.slint", "Editor.transition-duration-all(seconds)"),
            ("ui/editor.slint", "callback transition-duration-all(seconds: float)"),
            ("src/lib.rs", "state.apply_transition_duration_to_all(seconds as f64)"),
        ):
            self.assertIn(expected, (DESKTOP / file).read_text())

    def test_navigation_order_preserves_effects_templates_identity_and_home_only_builder(self):
        text = (DESKTOP / "ui/workspace/media-pane.slint").read_text()
        strip = text[text.index("chips := HorizontalLayout"):text.index("// The end buttons")]
        labels = re.findall(r'label: (?:I18n.t\("([^"]+)"\)|"([^"]+)");', strip)
        self.assertEqual([left or right for left, right in labels][:7], [
            "common.media", "common.text", "mediaPane.transitions", "Animations",
            "mediaPane.filters", "common.effects", "mediaPane.templates"])
        for label, index in (("common.effects",4),("mediaPane.templates",6)):
            self.assertRegex(strip, rf'label: I18n.t\("{re.escape(label)}"\);\s*current: root.tab == {index};\s*clicked => \{{ root.tab = {index}; \}}')
        self.assertNotIn("C Builder", strip)
        self.assertNotIn("CBuilderPane", text)
        self.assertIn("CBuilderPane", (DESKTOP / "ui/workspace/c-builder-home.slint").read_text())
        # Existing page bodies still use the original numeric identities.
        self.assertIn("if root.tab == 6", text)
        self.assertIn("if root.tab == 4", text)


class TransitionDurationZipTests(unittest.TestCase):
    def round_trip(self, document):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root / "desktop"
            desktop.mkdir()
            # These tests exercise manifest/planner/transport semantics. Image
            # decoding and video rendering are covered by their separate suites.
            for item in document["media"]:
                name = Path(item["path"]).name
                (desktop / name).write_bytes(b"intact media fixture")
                item["path"] = str(desktop / name)
            original = deepcopy(document)
            (desktop / "concat.json").write_text(json.dumps(document))
            validate_gpu_manifest(document)
            pack(desktop, root / "gpu.zip")
            with zipfile.ZipFile(root / "gpu.zip") as archive:
                packaged = json.loads(archive.read("project/concat.json"))
            self.assertEqual(packaged, original)
            restored = root / "worker"
            restore(root / "gpu.zip", restored)
            loaded = json.loads((restored / "concat.json").read_text())
            self.assertEqual(loaded["timelines"], original["timelines"])
            gpu = load_project(restored, lambda _: None)
            self.assertEqual(len(gpu.native_cuts), 3)
            self.assertEqual([cut.transition.span.end-cut.transition.span.start for cut in gpu.native_cuts], [18,18,18])
            self.assertEqual([cut.transition.intensity for cut in gpu.native_cuts], [.5,.6,.4])
            self.assertEqual(dict(gpu.native_cuts[2].transition.params), {"roughness":.6,"seed":42})
            self.assertEqual(len(gpu.sounds), 1)
            voice = gpu.sounds[0]
            self.assertEqual((voice.start,voice.seconds,voice.source_start),(0,8.,.25))
            self.assertEqual(gpu.frames,240)
            return loaded

    def test_actual_zip_keeps_per_cut_durations_types_intensity_and_independent_narration(self):
        document = {"version":1,"activeTimelineId":"main","media":[
            {"id":"photo","kind":"image","path":"photo.jpg"},
            {"id":"voice","kind":"audio","path":"voice.wav","duration":10000.}],
            "timelines":[{"id":"main","video":{"width":1920,"height":1080,"rateNum":30,"rateDen":1},
                          "tracks":[{"id":"video"},{"id":"audio"}],"clips":[]} ]}
        clips=document["timelines"][0]["clips"]
        clips.append({"id":"voice","kind":"audio","trackId":"audio","mediaId":"voice","start":0.,"duration":8.,"sourceStart":.25})
        for index in range(4):
            clip={"id":f"c{index}","kind":"image","trackId":"video","mediaId":"photo","start":index*2.,"duration":2.}
            if index:
                clip["transitionIn"]={"id":["concat.fade-black","concat.clock-wipe","concat.paper-rip-vertical"][index-1],"duration":.6,"intensity":[.5,.6,.4][index-1]}
                if index==3:clip["transitionIn"]["params"]={"roughness":.6,"seed":42}
            clips.append(clip)
        self.round_trip(document)

    def test_worker_reads_real_rust_duration_edit_after_save_reopen_and_zip(self):
        directory=os.environ.get("CONCAT_TRANSITION_FIXTURE_DIR")
        if not directory:
            self.skipTest("Rust inspector fixture is generated by planned source CI")
        path=Path(directory)/"bulk-transition-duration.json"
        self.assertTrue(path.is_file(),"headless Rust planner fixture must exist before worker tests")
        self.round_trip(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
