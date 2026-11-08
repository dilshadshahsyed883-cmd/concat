"""CUDA filter looks: manifest parity, layer stacking and validation."""
from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest

from cloud_render.concat.hybrid.looks import LOOK_CATALOGUE, FILTER_LOOKS
from cloud_render.concat.hybrid.project import _plan_project, validate_gpu_manifest

ROOT = Path(__file__).resolve().parents[3]


def project_with_filter(effect_id: str, *, duration: float = 2.0):
    return {
        "version": 1, "activeTimelineId": "timeline",
        "media": [{"id": "photo", "kind": "image", "path": "photo.jpg"}],
        "timelines": [{
            "id": "timeline",
            "video": {"width": 1920, "height": 1080, "rateNum": 30, "rateDen": 1},
            "tracks": [{"id": "footage", "visible": True}, {"id": "master", "visible": True}],
            "clips": [
                {"id": "photo1", "kind": "image", "trackId": "footage",
                 "mediaId": "photo", "start": 0, "duration": duration},
                {"id": "master1", "kind": "layer", "trackId": "master",
                 "start": 0, "duration": duration, "opacity": 0.65,
                 "videoEffects": [{"id": effect_id}]},
            ],
        }],
    }


class TimelineFilterLookTests(unittest.TestCase):
    def test_complete_names_and_gpu_catalogue_parity(self):
        names = (
            "Crystal Honey", "8K", "Bright Summer", "Red Tide", "Iced Fog",
            "Soft Golden", "Urban Gray", "Sun Glitter", "Aquamarine",
            "Coastal Dusk", "Milky Fog", "City Lights", "Spring Paint",
            "Autumn Pastel", "Payphone Talk", "Pulp Retro Film", "Velvet Halo",
            "Honey Brown", "4K", "Cold Thriller", "HDR Look",
        )
        self.assertEqual(tuple(item["name"] for item in LOOK_CATALOGUE), names)
        self.assertEqual(len(FILTER_LOOKS), len(names))
        capabilities = json.loads((ROOT / "cloud_render/concat/hybrid/gpu_ui_capabilities.json").read_text())
        self.assertEqual(set(capabilities["filters"]), set(FILTER_LOOKS))
        for item in LOOK_CATALOGUE:
            with self.subTest(name=item["name"]):
                folder = ROOT / "upstream/src/crates/concat-effects/packages" / item["id"]
                manifest = tomllib.loads((folder / "effect.toml").read_text())
                self.assertEqual(manifest["effect"]["id"], item["id"])
                self.assertEqual(manifest["effect"]["name"], item["name"])
                self.assertEqual(manifest["effect"]["kind"], "filter")
                shader = (folder / "effect.wgsl").read_text()
                for field in ("brightness", "contrast", "saturation", "temperature",
                              "exposure", "sharpen"):
                    self.assertIn(f"{item[field]:.6f}", shader)

    def test_each_preset_renders_as_a_master_layer(self):
        for effect_id, settings in FILTER_LOOKS.items():
            with self.subTest(id=effect_id):
                document = project_with_filter(effect_id)
                timeline = validate_gpu_manifest(document)
                project = _plan_project(document, timeline, lambda item: Path(item["path"]))
                self.assertEqual(project.frames, 60)
                layer = project.lanes[1][0]
                self.assertEqual(layer.kind, "layer")
                self.assertEqual((layer.span.start, layer.span.end), (0, 60))
                self.assertEqual(layer.filters, (settings,))
                self.assertAlmostEqual(layer.transform(0).opacity, .65)

    def test_multiple_independent_master_layers(self):
        document = project_with_filter("concat.look.4k")
        timeline = document["timelines"][0]
        timeline["tracks"].append({"id": "second", "visible": True})
        timeline["clips"].append({
            "id": "second1", "kind": "layer", "trackId": "second",
            "start": 0, "duration": 2, "opacity": .25,
            "videoEffects": [{"id": "concat.look.cold-thriller"}],
        })
        plan = _plan_project(document, validate_gpu_manifest(document),
                             lambda item: Path(item["path"]))
        self.assertEqual(plan.lanes[1][0].filters, (FILTER_LOOKS["concat.look.4k"],))
        self.assertEqual(plan.lanes[2][0].filters, (FILTER_LOOKS["concat.look.cold-thriller"],))
        self.assertAlmostEqual(plan.lanes[2][0].transform(0).opacity, .25)

    def test_filter_presets_not_allowed_on_individual_media_clips(self):
        document = project_with_filter("concat.look.8k")
        document["timelines"][0]["clips"][0]["videoEffects"] = [
            {"id": "concat.look.crystal-honey"}]
        with self.assertRaisesRegex(ValueError, "Unsupported GPU effect"):
            validate_gpu_manifest(document)

    def test_preset_rejects_custom_effect_params(self):
        for extra in ({"params": {"brightness": .2}},
                      {"params": {"intensity": 50}},
                      {"keys": {"brightness": [{"at": 0, "value": .2}]}}):
            with self.subTest(extra=extra):
                document = project_with_filter("concat.look.4k")
                document["timelines"][0]["clips"][1]["videoEffects"][0].update(extra)
                with self.assertRaisesRegex(ValueError, "timeline look accepts only"):
                    validate_gpu_manifest(document)

    def test_professional_detail_looks_preserve_sdr_and_limit_halos(self):
        # 4K/8K/HDR are perceived-detail and tone looks, never output modes.
        detail = [next(item for item in LOOK_CATALOGUE if item["name"] == name)
                  for name in ("4K", "8K", "HDR Look")]
        for item in detail:
            self.assertLessEqual(item["sharpen"], .65)
            self.assertLessEqual(item["contrast"], 1.08)
            self.assertLessEqual(item["saturation"], 1.045)
            self.assertEqual(item["exposure"], 0)
        self.assertLess(detail[0]["sharpen"], detail[1]["sharpen"])

    def test_detail_and_hdr_look_do_not_misrepresent_export_mode(self):
        for effect_id in ("concat.look.4k", "concat.look.8k", "concat.look.hdr-look"):
            document = project_with_filter(effect_id)
            video = validate_gpu_manifest(document)["video"]
            self.assertEqual((video["width"], video["height"]), (1920, 1080))


if __name__ == "__main__":
    unittest.main()
