import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cloud_render.concat.hybrid.contract import FrameSpan, cuda
from cloud_render.concat.hybrid.project import NativeCut
from cloud_render.concat.hybrid.render import native_transition_frame
from cloud_render.concat.hybrid.transitions import Transition, render_transition


class TransitionTimingTests(unittest.TestCase):
    def test_overlap_has_exact_outgoing_and_incoming_endpoints(self):
        transition = Transition("dissolve", FrameSpan(10, 15))
        self.assertEqual([transition.progress(i) for i in range(9, 16)],
                         [0, 0, 0.25, 0.5, 0.75, 1, 1])

    def test_single_frame_overlap_is_a_cut(self):
        transition = Transition("wipe", FrameSpan(7, 8), "up")
        self.assertEqual([transition.progress(i) for i in range(6, 9)], [0, 1, 1])

    def test_packaged_fade_progress_matches_half_open_native_span(self):
        for kind in ("native-fade-black", "native-fade-white"):
            with self.subTest(kind=kind):
                transition = Transition(kind, FrameSpan(10, 14))
                self.assertEqual([transition.progress(i) for i in range(9, 16)],
                                 [0, 0, .25, .5, .75, 1, 1])

    def test_native_stage_composites_both_stacks_and_holds_the_outgoing_source(self):
        outgoing = SimpleNamespace(span=FrameSpan(0, 10))
        incoming = SimpleNamespace(span=FrameSpan(10, 20))
        cut = NativeCut(0, Transition("native-fade-white", FrameSpan(8, 14)),
                        outgoing, incoming, True)
        with (
            patch("cloud_render.concat.hybrid.render.picture", side_effect=["old", "new"]) as picture,
            patch("cloud_render.concat.hybrid.render.composite", side_effect=["from", "to"]) as composite,
            patch("cloud_render.concat.hybrid.render.render_transition") as combine,
        ):
            result = native_transition_frame("sources", ["lower"], cut, 11)
        self.assertIs(result, combine.return_value)
        self.assertEqual(picture.call_args_list[0].args, ("sources", outgoing, 9))
        self.assertEqual(picture.call_args_list[0].kwargs, {"tail": True, "placement_index": 10})
        self.assertEqual(picture.call_args_list[1].args, ("sources", incoming, 11))
        self.assertEqual(picture.call_args_list[1].kwargs, {"force_opacity": True})
        old_layers, new_layers = [call.args[0] for call in composite.call_args_list]
        self.assertEqual(old_layers[0], "lower")
        self.assertEqual(old_layers[1].texture, "old")
        self.assertEqual(new_layers[:2], old_layers)
        self.assertEqual(new_layers[2].texture, "new")
        combine.assert_called_once_with("from", "to", cut.transition, 11)

    def test_invalid_parameters_fail_before_cuda(self):
        with self.assertRaises(ValueError):
            Transition("crossfade?", FrameSpan(0, 2))
        with self.assertRaises(ValueError):
            Transition("slide", FrameSpan(0, 2), "diagonal")
        with self.assertRaises(TypeError):
            Transition("fade", (0, 2))
        for invalid in (-1, 1.5, True):
            with self.subTest(frame=invalid), self.assertRaises(ValueError):
                Transition("zoom", FrameSpan(0, 3)).progress(invalid)


class TransitionGpuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cp = cuda()
        except RuntimeError as error:
            raise unittest.SkipTest(str(error)) from error

    def setUp(self):
        cp = self.cp
        self.a = cp.zeros((4, 4, 4), dtype=cp.float32)
        self.b = cp.zeros_like(self.a)
        self.a[:, :, 0] = 1
        self.b[:, :, 2] = 1
        self.a[:, :, 3] = self.b[:, :, 3] = 1

    def render_middle(self, kind, direction="left"):
        return render_transition(self.a, self.b, Transition(kind, FrameSpan(7, 12),
                                                             direction), 9)

    def test_dissolve_handles_transparent_incoming_and_preserves_endpoints(self):
        cp = self.cp
        self.b[:, :, 3] = 0
        transition = Transition("dissolve", FrameSpan(7, 12))
        self.assertIs(render_transition(self.a, self.b, transition, 7), self.a)
        self.assertIs(render_transition(self.a, self.b, transition, 11), self.b)
        cp.testing.assert_allclose(self.render_middle("dissolve")[0, 0],
                                   cp.asarray([1, 0, 0, 0.5]), atol=1e-6)

    def test_fade_middle_is_black(self):
        cp = self.cp
        cp.testing.assert_allclose(self.render_middle("fade")[0, 0],
                                   cp.asarray([0, 0, 0, 1]), atol=1e-6)

    def test_packaged_fades_match_native_display_mix_and_preserve_stage_alpha(self):
        cp = self.cp
        self.a[:] = cp.asarray([.25, .5, .75, .25])
        self.b[:] = cp.asarray([.5, .25, .75, .75])
        for kind, targets in (
            ("native-fade-black", ([.125, .25, .375, .25], [0, 0, 0, .75],
                                   [.25, .125, .375, .75])),
            ("native-fade-white", ([.625, .75, .875, .25], [1, 1, 1, .75],
                                   [.75, .625, .875, .75])),
        ):
            transition = Transition(kind, FrameSpan(8, 12))
            for frame, target in zip(range(9, 12), targets):
                with self.subTest(kind=kind, frame=frame):
                    actual = render_transition(self.a, self.b, transition, frame)
                    cp.testing.assert_allclose(actual[0, 0], cp.asarray(target), atol=1e-6)

    def test_wipe_and_slide_enter_from_the_correct_edge(self):
        for kind in ("wipe", "slide"):
            for direction, outgoing_pos, incoming_pos in (
                ("left", (0, 0), (0, 3)), ("right", (0, 3), (0, 0)),
                ("up", (0, 0), (3, 0)), ("down", (3, 0), (0, 0)),
            ):
                with self.subTest(kind=kind, direction=direction):
                    frame = self.render_middle(kind, direction)
                    self.assertEqual(frame[outgoing_pos].get().tolist(), [1, 0, 0, 1])
                    self.assertEqual(frame[incoming_pos].get().tolist(), [0, 0, 1, 1])
        self.a[0, 2] = self.cp.asarray([0, 1, 0, 1])
        self.assertEqual(self.render_middle("slide")[0, 0].get().tolist(), [0, 1, 0, 1])

    def test_zoom_resamples_before_blending(self):
        cp = self.cp
        self.a[:] = 0
        self.b[:] = 0
        self.a[0, 0] = cp.asarray([1, 0, 0, 1])
        dissolve = self.render_middle("dissolve")
        zoom = self.render_middle("zoom")
        self.assertEqual(float(dissolve[0, 0, 3]), 0.5)
        self.assertGreater(float(zoom[0, 0, 3]), 0)
        self.assertLess(float(zoom[0, 0, 3]), 0.5)
