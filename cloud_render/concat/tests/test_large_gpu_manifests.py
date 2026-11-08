"""Large, malformed and stress fixture coverage for the GPU project JSON contract."""
import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from cloud_render.concat.gpu.worker import Handler, RenderWorker, SaladDualStackHTTPServer
from cloud_render.concat.gpu.resumable import CHUNK_BYTES
from cloud_render.concat.hybrid.project import validate_gpu_manifest
from cloud_render.concat.project_bundle import MAX_PROJECT_MANIFEST_BYTES, TransferError, restore

TOKEN = "m" * 48
MP4 = b"\x00\x00\x00\x18ftypisom" + b"0" * 80


def project_fixture(count=1, *, animations=True, transitions=True, effects=True):
    """Actual sequential clips with keyframes, transitions, and effects; no filler field."""
    clips = []
    for index in range(count):
        clip = {
            "id": f"clip-{index}", "name": "scene_" + str(index) + "_" + "a" * 160,
            "kind": "image", "trackId": "video", "mediaId": "photo",
            "start": index / 10, "duration": .1,
        }
        if animations:
            clip["keys"] = [{"property": "scale", "at": 0, "value": 1.0},
                            {"property": "scale", "at": 1, "value": 1.05}]
        if effects and index % 10 == 0:
            clip["videoEffects"] = [{"id": "concat.box-blur", "params": {"radius": 4}}]
        if transitions and index and index % 40 == 0:
            clip["transitionIn"] = {"id": "cross-fade", "duration": 2 / 30}
        clips.append(clip)
    return {
        "version": 1, "activeTimelineId": "timeline",
        "media": [{"id": "photo", "kind": "image", "path": "photo.jpg"}],
        "timelines": [{
            "id": "timeline",
            "video": {"width": 1920, "height": 1080, "rateNum": 30, "rateDen": 1},
            "tracks": [{"id": "video", "visible": True}],
            "clips": clips,
        }],
    }


def fixture_bytes(count):
    return json.dumps(project_fixture(count), separators=(",", ":")).encode("utf-8")


def bundle_fixture(manifest, media_bytes=0):
    buffer = io.BytesIO()
    member = "media/" + "b" * 32 + ".jpg"
    media = b"x" * media_bytes
    references = ([{"index": 0, "member": member, "size": len(media),
                    "sha256": hashlib.sha256(media).hexdigest()}] if media else [])
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bundle.json", json.dumps({
            "version": 1, "project_file": "concat.json", "references": references}))
        archive.writestr("project/concat.json", manifest)
        if media:
            archive.writestr(member, media, compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue()


class LargeManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.worker = RenderWorker(self.root / "worker", token=TOKEN, renderer=self.render)
        self.renders = []
        self.server = SaladDualStackHTTPServer(("::", 0), Handler)
        self.server.worker = self.worker
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.worker.pool.shutdown(wait=True)
        self.temp.cleanup()

    def render(self, project, output):
        self.renders.append(project)
        output.write_bytes(MP4)

    def request(self, method, path, body=b"", *, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=150)
        h = {"Content-Type": "application/json", **(headers or {})}
        if auth:
            h["Authorization"] = "Bearer " + TOKEN
        conn.request(method, path, body=body, headers=h)
        response = conn.getresponse()
        code, payload = response.status, json.loads(response.read())
        conn.close()
        return code, payload

    def test_small_medium_and_large_preflight(self):
        for label, count in (("small", 1), ("medium", 250), ("large", 8000)):
            with self.subTest(label=label):
                body = fixture_bytes(count)
                if label == "large":
                    self.assertGreater(len(body), 2 * 1024 * 1024)
                self.assertLess(len(body), MAX_PROJECT_MANIFEST_BYTES)
                status, reply = self.request("POST", "/api/validate", body)
                self.assertEqual((status, reply.get("ok")), (200, True), reply)
                self.assertEqual(reply["project_bytes"], len(body))

    def test_authenticated_validation_and_fail_closed_compatibility(self):
        body = fixture_bytes(1)
        code, _ = self.request("POST", "/api/validate", body, auth=False)
        self.assertEqual(code, 401)
        unsupported = project_fixture(1)
        unsupported["timelines"][0]["clips"][0]["videoEffects"] = [
            {"id": "concat.chroma-key", "params": {}}]
        code, reply = self.request("POST", "/api/validate", json.dumps(unsupported).encode())
        self.assertEqual(code, 200)
        self.assertFalse(reply["ok"])
        self.assertIn("chroma-key", " ".join(reply["errors"]))

    def test_malformed_empty_nested_and_oversized_fixtures(self):
        for label, payload in (("invalid-json", b"{no"),
                               ("trailing-json", b"{}{}"),
                               ("empty", b""),
                               ("nonutf8", b"\xff"),
                               ("too-deep", b"[" * 20000 + b"0" + b"]" * 20000)):
            with self.subTest(label=label):
                code, reply = self.request("POST", "/api/validate", payload)
                self.assertEqual(code, 400, reply)
                self.assertFalse(reply["ok"])
        code, reply = self.request("POST", "/api/validate", b"", headers={
            "Content-Length": str(MAX_PROJECT_MANIFEST_BYTES + 1)})
        self.assertEqual(code, 413, reply)
        self.assertIn("64 MiB", reply["error"])

    def test_capabilities_advertise_real_limit(self):
        code, reply = self.request("GET", "/api/capabilities")
        self.assertEqual(code, 200)
        self.assertEqual(reply["max_project_manifest_bytes"], MAX_PROJECT_MANIFEST_BYTES)

    def test_large_bundle_restores_then_resumable_restart(self):
        body = fixture_bytes(8000)
        self.assertGreater(len(body), 2 * 1024 * 1024)
        package = bundle_fixture(body, media_bytes=CHUNK_BYTES + 137)
        self.assertGreater(len(package), CHUNK_BYTES)
        archive = self.root / "large.zip"
        archive.write_bytes(package)
        restored = self.root / "restored"
        restore(archive, restored)
        self.assertEqual((restored / "concat.json.original").read_bytes(), body)
        sha = hashlib.sha256(package).hexdigest()
        session = self.worker.spool.begin("large", len(package), sha, "cuda-hybrid-v1")
        self.assertGreaterEqual(session["total_chunks"], 2)
        first = package[:session["chunk_size"]]
        self.worker.spool.put_chunk("large", 0, io.BytesIO(first), len(first),
                                    hashlib.sha256(first).hexdigest())
        restarted = RenderWorker(self.worker.root, token=TOKEN, renderer=self.render)
        try:
            self.assertEqual(restarted.spool.status("large")["uploaded"], [0])
            for index in range(1, session["total_chunks"]):
                chunk = package[index * session["chunk_size"]:(index + 1) * session["chunk_size"]]
                restarted.spool.put_chunk("large", index, io.BytesIO(chunk), len(chunk),
                                          hashlib.sha256(chunk).hexdigest())
            restarted.spool.complete("large")
            restarted.pool.shutdown(wait=True)
            self.assertEqual(restarted.status("large")["state"], "rendered")
        finally:
            restarted.pool.shutdown(wait=True)

    def test_stress_count_and_large_bundle_member(self):
        document = project_fixture(1)
        document["timelines"][0]["clips"] *= 50001
        with self.assertRaisesRegex(ValueError, "50000 clips"):
            validate_gpu_manifest(document)
        large = fixture_bytes(8000)
        archive = self.root / "large-raw.zip"
        archive.write_bytes(bundle_fixture(large))
        restore(archive, self.root / "accepted")
        self.assertTrue((self.root / "accepted" / "concat.json").exists())

    def test_mirrored_desktop_clients_remain_identical(self):
        repo = Path(__file__).resolve().parents[3]
        a = (repo / "concat_gpu" / "concat_gpu_client.ps1").read_bytes()
        b = (repo / "upstream/src/crates/concat/src/concat_gpu_client.ps1").read_bytes()
        self.assertEqual(a, b)
        self.assertIn(b"max_project_manifest_bytes", a)
        self.assertNotIn(b"2 MiB compatibility limit", a)


if __name__ == "__main__":
    unittest.main()
