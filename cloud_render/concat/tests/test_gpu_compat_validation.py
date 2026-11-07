"""GPU/desktop compatibility: reject unsupported edits before any media upload."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from cloud_render.concat.gpu.worker import Handler, RenderWorker
from cloud_render.concat.hybrid.project import load_project, validate_gpu_manifest


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


class GpuManifestValidationTests(unittest.TestCase):
    def test_accepts_supported_basic_edit_and_filter(self):
        document = minimal_project()
        document["timelines"][0]["clips"][0]["videoEffects"] = [
            {"id": "concat.contrast", "params": {"amount": 10}}]
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

    def test_denies_non_jpeg_image_before_upload(self):
        document = minimal_project()
        document["media"][0]["path"] = "picture.png"
        with self.assertRaisesRegex(ValueError, "JPEG"):
            validate_gpu_manifest(document)

    def test_existing_custom_caption_style_is_not_silently_dropped(self):
        document = minimal_project()
        document["timelines"][0]["clips"].append({
            "id": "caption1", "kind": "text", "trackId": "video",
            "start": 0, "duration": 1,
            "text": {"content": "Hello", "fontFamily": "Unsupported family"}
        })
        with self.assertRaisesRegex(ValueError, "caption styling"):
            validate_gpu_manifest(document)

    def test_rejects_unaligned_transition_in_renderer(self):
        document = minimal_project()
        document["timelines"][0]["clips"].append({
            "id": "photo2", "kind": "image", "trackId": "video", "mediaId": "photo",
            "start": 1.5, "duration": 1.0,
            "transitionIn": {"id": "cross-fade", "duration": 0.5}
        })
        validate_gpu_manifest(document)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "concat.json").write_text(json.dumps(document))
            (root / "photo.jpg").write_bytes(b"dummy jpeg")
            with self.assertRaisesRegex(ValueError, "unaligned"):
                load_project(root, lambda _: None)


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
                        payload = json.dumps(minimal_project()).encode("utf-8")
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
