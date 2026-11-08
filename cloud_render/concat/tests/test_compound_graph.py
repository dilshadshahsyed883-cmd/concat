"""Compound graph safety and neutral render equivalence for both CUDA paths."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from cloud_render.concat.hybrid.compounds import validate_compounds
from cloud_render.concat.hybrid.project import load_project, validate_gpu_manifest
from cloud_render.concat.gpu.worker import concat_stage1_plan


def fixture():
    return {
        "version": 1, "activeTimelineId": "TL1",
        "media": [{"id": "photo", "kind": "image", "path": "photo.jpg"}],
        "timelines": [{
            "id": "TL1",
            "video": {"width": 1920, "height": 1080,
                      "rateNum": 30, "rateDen": 1},
            "tracks": [{"id": "T1", "visible": True, "muted": False}],
            "clips": [
                {"id": "c1", "kind": "image", "trackId": "T1",
                 "mediaId": "photo", "start": 0.0, "duration": 1.0},
                {"id": "c2", "kind": "image", "trackId": "T1",
                 "mediaId": "photo", "start": 1.0, "duration": 1.0},
            ],
        }],
    }


def grouped(document):
    document = copy.deepcopy(document)
    document["timelines"][0]["compounds"] = [
        {"id": "cmp1", "name": "First", "members": [
            {"kind": "clip", "id": "c1"}]},
        {"id": "cmp2", "name": "Scene", "members": [
            {"kind": "compound", "id": "cmp1"},
            {"kind": "clip", "id": "c2"}]},
    ]
    return document


class CompoundGraphTests(unittest.TestCase):
    def test_valid_nested_membership_has_identical_cuda_plan(self):
        baseline = fixture()
        nested = grouped(baseline)
        validate_gpu_manifest(baseline)
        validate_gpu_manifest(nested)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "photo.jpg").write_bytes(b"image for planning only")
            def plan(doc):
                (root / "concat.json").write_text(json.dumps(doc), encoding="utf-8")
                return load_project(root, lambda _: None)
            original = plan(baseline)
            compound = plan(nested)
            self.assertEqual(original, compound)

    def test_rejects_parent_effects_instead_of_silently_ignoring(self):
        doc = grouped(fixture())
        doc["timelines"][0]["compounds"][0]["videoEffects"] = [{"id": "concat.blur"}]
        with self.assertRaisesRegex(ValueError, "Unsupported compound-level"):
            validate_gpu_manifest(doc)
        with self.assertRaisesRegex(ValueError, "Unsupported compound-level"):
            validate_compounds(doc["timelines"][0])

    def test_rejects_cycles_duplicates_dangling_and_bad_nodes(self):
        cases = []
        doc = grouped(fixture())
        doc["timelines"][0]["compounds"][0]["members"] = [
            {"kind": "compound", "id": "cmp2"}]
        cases.append(doc)  # cycle
        doc = grouped(fixture())
        doc["timelines"][0]["compounds"][1]["members"].append(
            {"kind": "clip", "id": "c1"})
        cases.append(doc)  # duplicate clip ownership
        doc = grouped(fixture())
        doc["timelines"][0]["compounds"][0]["members"] = [
            {"kind": "clip", "id": "missing"}]
        cases.append(doc)  # dangling source
        doc = grouped(fixture())
        doc["timelines"][0]["compounds"][0]["members"] = [
            {"kind": "compound", "id": "cmp1"}]
        cases.append(doc)  # self-cycle
        for doc in cases:
            with self.subTest(groups=doc["timelines"][0]["compounds"]):
                with self.assertRaises(ValueError):
                    validate_gpu_manifest(doc)

    def test_stage1_planner_accepts_structural_compounds_without_audio_duplication(self):
        doc = fixture()
        clip = doc["timelines"][0]["clips"]
        clip.pop()  # One image, one narration as Stage1 requires
        doc["media"].append({"id": "voice", "kind": "audio",
                             "path": "voice.mp3", "hasAudio": False})
        clip.append({"id": "voice1", "kind": "audio", "trackId": "T1",
                     "mediaId": "voice", "start": 0.0, "duration": 1.0})
        doc["timelines"][0]["compounds"] = [{
            "id": "cmp8", "name": "Scene", "members": [
                {"kind": "clip", "id": "c1"}, {"kind": "clip", "id": "voice1"}]}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for filename in ("photo.jpg", "voice.mp3"):
                (root / filename).write_bytes(b"data")
            for item in doc["media"]:
                item["path"] = str(root / item["path"])
            (root / "concat.json").write_text(json.dumps(doc), encoding="utf-8")
            output = concat_stage1_plan(root)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["clips"]), 1)
            self.assertEqual(manifest["voice"], "voice.mp3")
