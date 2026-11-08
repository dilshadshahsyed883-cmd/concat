"""GPU/desktop compatibility: reject unsupported edits before any media upload."""
import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from cloud_render.concat.gpu.worker import Handler, RenderWorker
from cloud_render.concat.hybrid.captions import CaptionRenderer, _glyph_texture, _tile_region
from cloud_render.concat.hybrid.contract import FrameRate
from cloud_render.concat.hybrid.project import NativeColorEffect, load_project, validate_gpu_manifest


def minimal_project():
    return {
        "version": 1,
        "activeTimelineId": "timeline",
        "media": [{"id": "photo", "kind": "image", "path": "photo.jpg"}],
        "timelines": [{
            "id": "timeline",
            "video": {"width": 1920, "height": 1080, "rateNum": 30, "rateDen": 1},
            "tracks": [{"id": "video", "visible": True}],
            "clips": [{"id": "photo1", "kind": "image", "trackId": "video",
                       "mediaId": "photo", "start": 0.0, "duration": 1.0}],
        }],
    }


def native_caption(document, **style):
    timeline = document["timelines"][0]
    timeline["tracks"].append({"id": "caption", "visible": True, "muted": False})
    caption = {
        "id": "caption1", "kind": "text", "trackId": "caption", "mediaId": "",
        "name": "Hello", "start": 2 / 30, "duration": 4 / 30,
        "sourceStart": 0, "speed": 1, "volume": 1, "opacity": 1,
        "fadeIn": 0, "fadeOut": 0, "offsetX": -.1, "offsetY": .35,
        "rotation": 0, "scale": 1, "preservePitch": True, "filters": [], "videoEffects": [],
        # Every field written by concat_project::model::TextStyle's serializer.
        "text": {
            "content": "Hello world", "fontFamily": "Hanken Grotesk", "fontSize": .04,
            "fontWeight": 600, "italic": False, "color": "#ffffff", "align": "center",
            "opacity": 1, "strokeWidth": 0, "strokeColor": "#000000", "shadow": True,
            "background": "", "backgroundRadius": .0135, "backgroundPaddingX": .0315,
            "backgroundPaddingY": .018, "lineHeight": 1.2, "tracking": 0,
            "maxWidth": 0, "maxHeight": 0, **style,
        },
    }
    timeline["clips"].append(caption)
    return caption


def load_fixture(document):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "concat.json").write_text(json.dumps(document))
        (root / "photo.jpg").write_bytes(b"dummy jpeg")
        return load_project(root, lambda _: None)


class GpuManifestValidationTests(unittest.TestCase):
    def test_accepts_supported_basic_edit_and_filter(self):
        document = minimal_project()
        document["timelines"][0]["clips"][0]["videoEffects"] = [
            {"id": "concat.box-blur", "params": {"radius": 4}}]
        self.assertEqual(validate_gpu_manifest(document)["id"], "timeline")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "concat.json").write_text(json.dumps(document))
            (root / "photo.jpg").write_bytes(b"dummy jpeg")
            warnings = []
            result = load_project(root, warnings.append)
            self.assertEqual(result.frames, 30)
            self.assertEqual(warnings, [])

    def test_rejects_unsupported_effect_in_preflight_and_renderer(self):
        document = minimal_project()
        document["timelines"][0]["clips"][0]["videoEffects"] = [
            {"id": "concat.chroma-key", "params": {}}]
        with self.assertRaisesRegex(ValueError, r"concat.chroma-key.*photo1"):
            validate_gpu_manifest(document)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "concat.json").write_text(json.dumps(document))
            (root / "photo.jpg").write_bytes(b"dummy jpeg")
            with self.assertRaisesRegex(ValueError, "chroma-key"):
                load_project(root, lambda _: None)

    def test_rejects_unsupported_keyframes_and_effects(self):
        for field, value in (("crop", {"left": 1}),
                             ("speedCurve", [1, 2]),
                             ("keys", [{"property": "volume", "at": 0, "value": 2}]),
                             ("transitionIn", {"id": "unknown"})):
            with self.subTest(field=field):
                document = minimal_project()
                document["timelines"][0]["clips"][0][field] = value
                with self.assertRaisesRegex(ValueError, "Unsupported"):
                    validate_gpu_manifest(document)


    def test_supported_legacy_worker_transitions_still_render(self):
        for transition_id in ("cross-fade", "fade-black", "push", "zoom", "wipe-left", "wipe-right"):
            with self.subTest(transition=transition_id):
                document = minimal_project()
                document["timelines"][0]["clips"].append({
                    "id": "photo2", "kind": "image", "trackId": "video",
                    "mediaId": "photo", "start": 1.0, "duration": 1.0,
                    "transitionIn": {"id": transition_id, "duration": 0.4},
                })
                validate_gpu_manifest(document)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "concat.json").write_text(json.dumps(document))
                    (root / "photo.jpg").write_bytes(b"dummy jpeg")
                    project = load_project(root, lambda _: None)
                    self.assertEqual(project.frames, 60)
                    self.assertTrue(any(clip.transition for lane in project.lanes
                                        for clip in lane))

    def test_audio_pitch_change_is_rejected_before_upload(self):
        document = minimal_project()
        document["media"].append({"id": "voice", "kind": "audio", "path": "voice.mp3"})
        document["timelines"][0]["clips"].append({
            "id": "voice1", "kind": "audio", "trackId": "video",
            "mediaId": "voice", "start": 0.0, "duration": 1.0,
            "preservePitch": False,
        })
        with self.assertRaisesRegex(ValueError, "preserved audio pitch"):
            validate_gpu_manifest(document)

    def test_native_trimmed_jpeg_keeps_timeline_span(self):
        document = minimal_project()
        document["timelines"][0]["clips"][0].update(start=.5, duration=1.5, sourceStart=.75)
        validate_gpu_manifest(document)
        project = load_fixture(document)
        picture = project.lanes[0][0]
        self.assertEqual((picture.span.start, picture.span.end, project.frames), (15, 60, 60))
        self.assertEqual(picture.source_start, .75)
        document["timelines"][0]["clips"][0]["sourceStart"] = -.75
        with self.assertRaisesRegex(ValueError, "source start"):
            validate_gpu_manifest(document)

    def test_denies_non_jpeg_image_before_upload(self):
        document = minimal_project()
        document["media"][0]["path"] = "picture.png"
        with self.assertRaisesRegex(ValueError, "JPEG"):
            validate_gpu_manifest(document)

    def test_native_caption_preserves_font_position_shadow_and_frame_boundaries(self):
        document = minimal_project()
        native_caption(document, opacity=.6)
        validate_gpu_manifest(document)
        cue, style = load_fixture(document).captions[0]
        self.assertTrue(Path(style.font).is_file())
        self.assertEqual(Path(style.font).name, "HankenGrotesk-SemiBold.ttf")
        self.assertEqual(style.anchor, (.4, .85))
        self.assertTrue(style.shadow)
        self.assertEqual(style.opacity, .6)
        renderer = CaptionRenderer([cue], style)
        rate = FrameRate(30, 1)
        self.assertEqual([bool(renderer.active(index, rate)) for index in range(7)],
                         [False, False, True, True, True, True, False])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "caption glyph dependency unavailable")
    def test_caption_glyphs_use_native_anchor_and_shadow(self):
        document = minimal_project()
        native_caption(document, content="First line\n\nSecond line")
        cue, style = load_fixture(document).captions[0]
        tile = _glyph_texture(cue, None, style, 1920, 1080)
        top, left, visible_top, visible_bottom, visible_left, visible_right = _tile_region(
            (1080, 1920, 4), (tile.height, tile.width, 4), style, 0)
        self.assertAlmostEqual(top + tile.height / 2, 1080 * .85, delta=1)
        self.assertAlmostEqual(left + tile.width / 2, 1920 * .4, delta=1)
        self.assertEqual((visible_top, visible_bottom, visible_left, visible_right),
                         (top, top + tile.height, left, left + tile.width))
        colors = tile.getcolors(tile.width * tile.height)
        self.assertTrue(any(alpha and red == green == blue == 0
                            for _, (red, green, blue, alpha) in colors))

    def test_existing_custom_caption_style_is_not_silently_dropped(self):
        for field, value in (("fontFamily", "Unsupported family"), ("fontWeight", 700),
                             ("tracking", .01), ("background", "#111111"), ("align", "left")):
            with self.subTest(field=field):
                document = minimal_project()
                native_caption(document, **{field: value})
                with self.assertRaisesRegex(ValueError, rf"caption styling.*{field}.*caption1"):
                    validate_gpu_manifest(document)
                with self.assertRaisesRegex(ValueError, rf"caption styling.*{field}.*caption1"):
                    load_fixture(document)

    def test_track_order_and_caption_stacking_are_native(self):
        document = minimal_project()
        caption = native_caption(document)
        timeline = document["timelines"][0]
        timeline["tracks"].insert(1, {"id": "overlay", "visible": True})
        timeline["clips"].append({
            "id": "overlay1", "kind": "image", "trackId": "overlay",
            "mediaId": "photo", "start": 0, "duration": 1,
        })
        validate_gpu_manifest(document)
        self.assertEqual([[clip.id for clip in lane] for lane in load_fixture(document).lanes],
                         [["photo1"], ["overlay1"], []])
        caption["trackId"] = "video"
        with self.assertRaisesRegex(ValueError, "caption1.*higher track"):
            validate_gpu_manifest(document)

    def test_rejects_unaligned_transition_in_preflight_and_renderer(self):
        document = minimal_project()
        document["timelines"][0]["clips"].append({
            "id": "photo2", "kind": "image", "trackId": "video", "mediaId": "photo",
            "start": 1.5, "duration": 1.0,
            "transitionIn": {"id": "cross-fade", "duration": 0.5}
        })
        for operation in (validate_gpu_manifest, load_fixture):
            with self.assertRaisesRegex(ValueError, "unaligned.*photo2"):
                operation(document)

    def test_rejects_inexact_packaged_transitions_in_preflight(self):
        capabilities = json.loads((Path(__file__).parents[1] / "hybrid"
                                   / "gpu_ui_capabilities.json").read_text())
        self.assertEqual(capabilities["transitions"], ["concat.fade-black", "concat.fade-white"])
        for native_id in ("concat.dissolve", "concat.push", "concat.zoom-transition"):
            with self.subTest(transition=native_id):
                document = minimal_project()
                document["timelines"][0]["clips"].append({
                    "id": "photo2", "kind": "image", "trackId": "video", "mediaId": "photo",
                    "start": 1, "duration": 1,
                    "transitionIn": {"id": native_id, "duration": .4},
                })
                with self.assertRaisesRegex(ValueError, rf"{native_id}.*photo2"):
                    validate_gpu_manifest(document)

    def test_packaged_fades_plan_centered_still_cut_without_changing_length(self):
        document = minimal_project()
        incoming = {"id": "photo2", "kind": "image", "trackId": "video",
                    "mediaId": "photo", "start": 1, "duration": 1}
        document["timelines"][0]["clips"].append(incoming)
        for ident, kind in (("concat.fade-black", "native-fade-black"),
                            ("concat.fade-white", "native-fade-white")):
            with self.subTest(transition=ident):
                incoming["transitionIn"] = {"id": ident, "duration": .4}
                validate_gpu_manifest(document)
                project = load_fixture(document)
                cut, = project.native_cuts
                self.assertEqual(project.frames, 60)
                self.assertEqual((cut.transition.span.start, cut.transition.span.end), (24, 36))
                self.assertEqual((cut.incoming.id, cut.outgoing.id), ("photo2", "photo1"))
                self.assertEqual(cut.transition.kind, kind)
                self.assertEqual(cut.transition.progress(30), .5)
                self.assertFalse(cut.hold_outgoing)

    def test_packaged_fades_shift_later_when_video_has_no_incoming_handle(self):
        document = minimal_project()
        timeline = document["timelines"][0]
        document["media"].append({"id": "video1", "kind": "video", "path": "photo.jpg",
                                  "duration": 1})
        timeline["clips"][0].update(kind="video", mediaId="video1")
        incoming = {"id": "clip2", "kind": "video", "trackId": "video",
                    "mediaId": "video1", "start": 1, "duration": .7, "sourceStart": 0,
                    "transitionIn": {"id": "concat.fade-white", "duration": .4}}
        timeline["clips"].append(incoming)
        validate_gpu_manifest(document)
        cut, = load_fixture(document).native_cuts
        self.assertEqual((cut.transition.span.start, cut.transition.span.end), (30, 42))
        self.assertTrue(cut.hold_outgoing)
        self.assertEqual(cut.outgoing_frame(31), 29)
        incoming["sourceStart"] = .1
        cut, = load_fixture(document).native_cuts
        self.assertEqual((cut.transition.span.start, cut.transition.span.end), (27, 39))

    def test_packaged_fade_audio_reads_same_source_at_original_cut(self):
        document = minimal_project()
        timeline = document["timelines"][0]
        document["media"].append({"id": "video1", "kind": "video", "path": "photo.jpg",
                                  "duration": 3, "hasAudio": True})
        timeline["clips"][0].update(kind="video", mediaId="video1")
        timeline["clips"].append({
            "id": "clip2", "kind": "video", "trackId": "video", "mediaId": "video1",
            "start": 1, "duration": 1, "sourceStart": .6, "speed": 2,
            "transitionIn": {"id": "concat.fade-white", "duration": .4},
        })
        project = load_fixture(document)
        first, second = project.sounds
        self.assertAlmostEqual(first.seconds, 1.2)
        self.assertAlmostEqual(first.fade_out, .4)
        self.assertEqual(second.start, 24)
        self.assertAlmostEqual(second.fade_in, .4)
        self.assertAlmostEqual(second.source_start + (30 - second.start) * second.speed / 30, .6)
        self.assertEqual(project.frames, 60)

    def test_overlapping_packaged_fades_fail_in_preflight(self):
        document = minimal_project()
        timeline = document["timelines"][0]
        timeline["tracks"].append({"id": "upper"})
        first = timeline["clips"][0]
        timeline["clips"] += [
            {**first, "id": "upper1", "trackId": "upper"},
            {**first, "id": "photo2", "start": 1,
             "transitionIn": {"id": "concat.fade-black", "duration": .4}},
            {**first, "id": "upper2", "trackId": "upper", "start": 1,
             "transitionIn": {"id": "concat.fade-white", "duration": .4}},
        ]
        for operation in (validate_gpu_manifest, load_fixture):
            with self.assertRaisesRegex(ValueError, "upper2.*overlaps another transition"):
                operation(document)

    def test_rejects_inexact_packaged_filters_in_preflight(self):
        capabilities = json.loads((Path(__file__).parents[1] / "hybrid"
                                   / "gpu_ui_capabilities.json").read_text())
        self.assertEqual(capabilities["video_effects"], ["concat.exposure", "concat.contrast"])
        for effect_id in ("concat.saturation", "concat.sharpen"):
            with self.subTest(effect=effect_id):
                document = minimal_project()
                document["timelines"][0]["clips"][0]["videoEffects"] = [{"id": effect_id}]
                with self.assertRaisesRegex(ValueError, rf"{effect_id}.*photo1"):
                    validate_gpu_manifest(document)

    def test_native_exposure_plans_animated_stops(self):
        document = minimal_project()
        clip = document["timelines"][0]["clips"][0]
        clip.update(start=.5, duration=2, videoEffects=[{
            "id": "concat.exposure", "params": {"stops": -1},
            "keys": {"stops": [{"at": 0.25, "value": 0},
                               {"at": 0.75, "value": 2, "ease": [0, 0, 1, 1]}]},
        }])
        validate_gpu_manifest(document)
        exposure, = load_fixture(document).lanes[0][0].filters
        self.assertIsInstance(exposure, NativeColorEffect)
        self.assertAlmostEqual(exposure.at_frame(45), 1, places=5)
        self.assertEqual((exposure.at_frame(15), exposure.at_frame(70)), (0, 2))

    def test_invalid_exposure_rejected_before_upload(self):
        for effect in ({"params": {"stops": 5}}, {"params": {"wrong": 1}},
                       {"keys": {"stops": [{"at": .5, "value": 1, "ease": [0, 1]}]}}):
            with self.subTest(effect=effect):
                document = minimal_project()
                document["timelines"][0]["clips"][0]["videoEffects"] = [
                    {"id": "concat.exposure", **effect}]
                with self.assertRaisesRegex(ValueError, "concat.exposure.*photo1"):
                    validate_gpu_manifest(document)

    def test_native_contrast_uses_catalogue_amount_and_validates_limits(self):
        document = minimal_project()
        clip = document["timelines"][0]["clips"][0]
        clip["videoEffects"] = [{"id": "concat.contrast", "params": {"amount": 42}}]
        validate_gpu_manifest(document)
        contrast, = load_fixture(document).lanes[0][0].filters
        self.assertEqual(contrast.id, "concat.contrast")
        self.assertEqual(contrast.at_frame(0), 42)
        clip["videoEffects"][0]["params"]["amount"] = 101
        with self.assertRaisesRegex(ValueError, "concat.contrast.*photo1"):
            validate_gpu_manifest(document)

    def test_preflight_validates_timing_and_filter_parameters(self):
        for update, error in (({"duration": .001}, "sub-frame duration"),
                              ({"videoEffects": [{"id": "concat.box-blur",
                                                 "params": {"unknown": 12}}]},
                               "unknown effect parameters")):
            with self.subTest(update=update):
                document = minimal_project()
                document["timelines"][0]["clips"][0].update(update)
                with self.assertRaisesRegex(ValueError, error):
                    validate_gpu_manifest(document)


class WorkerPreflightTests(unittest.TestCase):
    def test_authenticated_validation_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            token = "gpu-validation-test-" * 3
            worker = RenderWorker(Path(directory), token=token)
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                server.worker = worker
                runner = threading.Thread(target=server.serve_forever, daemon=True)
                runner.start()
                try:
                    client = http.client.HTTPConnection(*server.server_address, timeout=5)
                    try:
                        document = minimal_project()
                        native_caption(document)
                        payload = json.dumps(document).encode("utf-8")
                        client.request("POST", "/api/validate", body=payload,
                                       headers={"Content-Type": "application/json"})
                        unauthorized = client.getresponse()
                        self.assertEqual(unauthorized.status, 401)
                        unauthorized.read()
                        client.close()
                        headers = {"Content-Type": "application/json",
                                   "Authorization": "Bearer " + token}
                        client.request("POST", "/api/validate", body=payload, headers=headers)
                        accepted = client.getresponse()
                        self.assertEqual(accepted.status, 200)
                        self.assertTrue(json.loads(accepted.read())["ok"])
                        invalid = minimal_project()
                        invalid["timelines"][0]["clips"][0]["videoEffects"] = [
                            {"id": "concat.unsupported-filter"}]
                        client.request("POST", "/api/validate",
                                       body=json.dumps(invalid).encode("utf-8"), headers=headers)
                        rejected = client.getresponse()
                        self.assertEqual(rejected.status, 200)
                        result = json.loads(rejected.read())
                        self.assertFalse(result["ok"])
                        self.assertIn("concat.unsupported-filter", result["errors"][0])
                        self.assertEqual(list((Path(directory) / "jobs").glob("*.zip")), [])
                    finally:
                        client.close()
                finally:
                    server.shutdown()
                    runner.join()
                    worker.pool.shutdown()


if __name__ == "__main__":
    unittest.main()
