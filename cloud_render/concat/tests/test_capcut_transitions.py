"""Catalog-to-worker compatibility for the twenty procedural GPU cuts."""
import unittest
from unittest.mock import patch
from pathlib import Path
import json

from cloud_render.concat.hybrid.capcut_transitions import (
    CAPCUT_PRESETS, CAPCUT_IDS, CAPCUT_KINDS, render_capcut_transition,
)
from cloud_render.concat.hybrid.contract import FrameSpan, cuda
from cloud_render.concat.hybrid.project import validate_gpu_manifest, _native_cut
from cloud_render.concat.hybrid.transitions import Transition, render_transition
from cloud_render.concat.tests.test_gpu_compat_validation import minimal_project


class CatalogTests(unittest.TestCase):
    def test_twenty_unique_stable_gpu_ids(self):
        self.assertEqual(len(CAPCUT_PRESETS), 20)
        self.assertEqual(len(CAPCUT_IDS), 20)
        self.assertEqual(len(CAPCUT_KINDS), 20)
        self.assertEqual(set(CAPCUT_IDS.values()), set(CAPCUT_KINDS))
        profile = json.loads((Path(__file__).parents[1] / "hybrid" /
                              "gpu_ui_capabilities.json").read_text())
        self.assertEqual(set(CAPCUT_IDS).difference(profile["transitions"]), set())

    def test_all_presets_are_valid_frames_with_safe_endpoints(self):
        for slug, _ in CAPCUT_PRESETS:
            with self.subTest(slug=slug):
                transition = Transition(slug, FrameSpan(10, 15))
                self.assertEqual(transition.progress(10), 0)
                self.assertEqual(transition.progress(14), 1)
                self.assertEqual(transition.progress(16), 1)

    def test_manifest_preflight_accepts_every_known_transition_and_rejects_unknown(self):
        for effect_id in (*CAPCUT_IDS, "concat.unknown-procedural"):
            document = minimal_project()
            timeline = document["timelines"][0]
            timeline["clips"][0]["duration"] = 2
            timeline["clips"].append({
                "id": "photo2", "kind": "image", "trackId": "video",
                "mediaId": "photo", "start": 2.0, "duration": 2.0,
                "transitionIn": {"id": effect_id, "duration": .5},
            })
            with self.subTest(effect_id=effect_id):
                if effect_id in CAPCUT_IDS:
                    self.assertEqual(validate_gpu_manifest(document)["id"], "timeline")
                else:
                    with self.assertRaisesRegex(ValueError, "unknown-procedural"):
                        validate_gpu_manifest(document)

    def test_no_unsupported_extra_serialized_fields(self):
        document = minimal_project()
        document["timelines"][0]["clips"][0]["transitionIn"] = {
            "id": next(iter(CAPCUT_IDS)), "duration": .5, "engine": "cpu"
        }
        with self.assertRaisesRegex(ValueError, "Unsupported GPU transition"):
            validate_gpu_manifest(document)

    def test_validates_invalid_surface_free_arguments_before_cuda(self):
        for bad in (-.01, 1.01, float("nan"), float("inf"), True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    render_capcut_transition(None, None, "ink-drying", bad)
                with self.assertRaises(ValueError):
                    render_capcut_transition(None, None, "ink-drying", .5, bad)

    def test_native_cut_preserves_timing_and_adjacency(self):
        from types import SimpleNamespace
        outgoing = SimpleNamespace(id="a", kind="image", span=FrameSpan(0, 60),
                                   source_start=0, speed=1)
        incoming = SimpleNamespace(id="b", kind="image", span=FrameSpan(60, 120),
                                   source_start=0, speed=1)
        cut = _native_cut(0, outgoing, incoming, "concat.ink-drying", .5, None)
        self.assertEqual(cut.transition.kind, "ink-drying")
        self.assertEqual(cut.transition.span.end-cut.transition.span.start, 15)
        self.assertEqual(cut.incoming.id, "b")


class GpuImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cp = cuda()
        except RuntimeError as error:
            raise unittest.SkipTest(str(error)) from error

    def test_all_twenty_cuda_kernel_modes_and_immutable_sources(self):
        cp = self.cp
        a = cp.zeros((31, 39, 4), dtype=cp.float32)
        b = cp.zeros_like(a)
        a[..., 0] = 1
        b[..., 2] = 1
        a[..., 3] = b[..., 3] = 1
        for kind in CAPCUT_KINDS:
            with self.subTest(kind=kind):
                cut = Transition(kind, FrameSpan(10, 21))
                self.assertIs(render_transition(a, b, cut, 10), a)
                self.assertIs(render_transition(a, b, cut, 20), b)
                mid = render_transition(a, b, cut, 15)
                self.assertEqual(mid.shape, a.shape)
                self.assertTrue(bool(cp.isfinite(mid).all()))
                self.assertTrue(bool(((mid >= 0) & (mid <= 1)).all()))
                self.assertTrue(bool((a[..., 0] == 1).all()))
                self.assertTrue(bool((b[..., 2] == 1).all()))

    def test_selected_styles_have_distinct_visible_patterns(self):
        cp = self.cp
        a = cp.zeros((32, 32, 4), dtype=cp.float32)
        b = cp.zeros_like(a)
        a[..., :3] = cp.asarray([1, 0, 0])
        b[..., :3] = cp.asarray([0, 0, 1])
        a[..., 3] = b[..., 3] = 1
        results = {}
        for kind in ("ink-drying", "torn-paper", "matrix-scan",
                     "warp-portal", "cyber-beehive", "pulse-wipe"):
            results[kind] = render_transition(
                a, b, Transition(kind, FrameSpan(0, 11)), 5
            ).get()
        import numpy as np
        self.assertFalse(np.allclose(results["ink-drying"], results["torn-paper"]))
        self.assertFalse(np.allclose(results["matrix-scan"], results["cyber-beehive"]))
        self.assertFalse(np.allclose(results["pulse-wipe"], results["torn-paper"]))


if __name__ == "__main__":
    unittest.main()
