"""Offline tests for authenticated Concat HTTP bridge and retained project data."""
import hashlib
import http.client
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from cloud_render.concat.project_bundle import pack

from cloud_render.concat.gpu.worker import Handler, RenderWorker, SaladDualStackHTTPServer, WorkerError, concat_stage1_plan

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 80
TOKEN = "a" * 48


def bundle():
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("bundle.json", json.dumps({"version": 1,
                "project_file": "concat.json", "references": []}))
            archive.writestr("project/concat.json", json.dumps({"media": []}))
        return buffer.getvalue()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rendered = []
        self.delivered = []
        self.worker = RenderWorker(Path(self.temp.name),
            token=TOKEN, renderer=self.render, deliver=self.deliver)
        # Offline runner has no requirement to have the NVIDIA-capable FFmpeg
        # runtime installed; exercise HTTP routing with readiness mocked.
        self.worker.ready = lambda: True
        self.data = bundle()
        self.sha = hashlib.sha256(self.data).hexdigest()

    def render(self, project, output):
        self.rendered.append(project)
        self.assertTrue((project / "concat.json").is_file())
        output.write_bytes(MP4)

    def deliver(self, job, output):
        self.delivered.append(job)

    def tearDown(self):
        self.worker.pool.shutdown(wait=True)
        self.temp.cleanup()

    def wait(self):
        self.worker.pool.shutdown(wait=True)

    def add(self, job="abc"):
        return self.worker.accept(job, io.BytesIO(self.data), len(self.data), self.sha)

    def test_queued_project_restores_and_renders_local_mp4(self):
        self.add()
        self.wait()
        entry = self.worker.status("abc")
        self.assertEqual(entry["state"], "rendered")
        self.assertEqual(entry["output_sha256"], hashlib.sha256(MP4).hexdigest())
        self.assertEqual(len(self.rendered), 1)
        self.assertTrue((self.worker.inputs / "abc/project.zip").exists())
        self.assertTrue((self.worker.outputs / "abc.mp4").is_file())

    def test_refuse_wrong_checksum_duplicate_and_bad_ids(self):
        with self.assertRaises(WorkerError):
            self.worker.accept("../oops", io.BytesIO(self.data), len(self.data), self.sha)
        with self.assertRaises(WorkerError):
            self.worker.accept("nope", io.BytesIO(self.data), len(self.data), "0" * 64)
        self.assertFalse((self.worker.inputs / "nope").exists())
        self.add("abc")
        with self.assertRaises(WorkerError) as raised:
            self.add("abc")
        self.assertEqual(raised.exception.status, 409)

    def test_refuse_truncated_upload_and_keep_queue_clean(self):
        with self.assertRaises(WorkerError):
            self.worker.accept("half", io.BytesIO(self.data[:5]), len(self.data), self.sha)
        self.assertFalse((self.worker.inputs / "half").exists())

    def test_delivery_ack_cleanup_is_per_job(self):
        self.worker.receiver = "https://app.scenebuilder.online"
        self.add("one")
        self.wait()
        self.assertEqual(self.worker.status("one")["state"], "delivered")
        self.assertEqual(self.delivered, ["one"])
        self.assertFalse((self.worker.outputs / "one.mp4").exists())
        self.assertFalse((self.worker.inputs / "one").exists())

    def test_delivery_failure_keeps_input_and_output(self):
        self.worker.receiver = "https://app.scenebuilder.online"
        def fail(job, output):
            raise RuntimeError("receiver unavailable")
        self.worker.deliver = fail
        self.add("one")
        self.wait()
        self.assertEqual(self.worker.status("one")["state"], "delivery_failed")
        self.assertTrue((self.worker.inputs / "one/project.zip").exists())
        self.assertTrue((self.worker.outputs / "one.mp4").exists())

    def test_interrupted_render_requires_explicit_recovery(self):
        self.worker._save("old", {"job": "old", "state": "running"})
        fresh = RenderWorker(Path(self.temp.name), token=TOKEN,
                             renderer=self.render)
        self.assertEqual(fresh.status("old")["state"], "failed")
        fresh.pool.shutdown(wait=True)

    def test_retired_native_mode_fails_before_creating_job(self):
        with self.assertRaisesRegex(WorkerError, "Native Vulkan engine retired"):
            self.worker.accept("native1", io.BytesIO(self.data), len(self.data),
                               self.sha, render_mode="native")
        self.assertFalse((self.worker.inputs / "native1").exists())
        self.assertFalse(self.worker._path("native1").exists())
        self.assertEqual(self.rendered, [])

    def test_persisted_old_native_job_never_renders_and_keeps_source(self):
        source = self.worker.inputs / "oldnative"
        source.mkdir()
        (source / "project.zip").write_bytes(self.data)
        self.worker._save("oldnative", {"job": "oldnative", "state": "queued",
                                       "render_mode": "native", "error": ""})
        self.worker._process("oldnative")
        result = self.worker.status("oldnative")
        self.assertEqual(result["state"], "failed")
        self.assertIn("retired", result["error"])
        self.assertTrue((source / "project.zip").is_file())
        self.assertEqual(self.rendered, [])

    def test_worker_health_reports_cuda_only(self):
        server = SaladDualStackHTTPServer(("::", 0), Handler)
        server.worker = self.worker
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/health/ready")
            response = conn.getresponse()
            body = json.loads(response.read())
            self.assertEqual(body["engine"], "cuda-nvenc-stage1")
            self.assertTrue(body["cuda_stage1"])
            self.assertFalse(body["native_vulkan"])
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_auth_health_and_ingest(self):
        server = SaladDualStackHTTPServer(("::", 0), Handler)
        server.worker = self.worker
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            # IPv6 must accept Salad Gateway ingress, while IPv4 stays usable.
            conn6 = http.client.HTTPConnection("::1", server.server_address[1], timeout=10)
            try:
                conn6.request("GET", "/health/ready")
                response6 = conn6.getresponse()
                self.assertEqual(response6.status, 200)
                response6.read()
            finally:
                conn6.close()
            conn.request("GET", "/health/ready")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.request("GET", "/api/jobs/abc")
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            # 401 intentionally closes the unauthorized socket to avoid body desync.
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/api/jobs/job-1", body=self.data, headers={
                "Authorization": "Bearer " + TOKEN,
                "Content-Type": "application/zip",
                "X-Content-SHA256": self.sha})
            response = conn.getresponse()
            self.assertEqual(response.status, 202)
            response.read()
            self.wait()
            conn.request("GET", "/api/jobs/job-1",
                         headers={"Authorization": "Bearer " + TOKEN})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["state"], "rendered")
            conn.request("GET", "/api/jobs/job-1/output",
                         headers={"Authorization": "Bearer " + TOKEN})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), MP4)
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ConcatCUDAIntegrationTests(unittest.TestCase):
    """Editor-native v0.2.8 JSON -> GPU contract; no new PC editor/schema."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "desktop"
        self.src.mkdir()
        self.video = self.src / "clip.mp4"
        self.voice = self.src / "voice.wav"
        self.video.write_bytes(b"video-contents")
        self.voice.write_bytes(b"voice-contents")
        self.doc = {
            "concat": "0.2.8", "version": 1, "activeTimelineId": "TL1",
            "media": [
                {"id": "m1", "path": str(self.video), "kind": "video",
                 "hasAudio": False},
                {"id": "m2", "path": str(self.voice), "kind": "audio"}],
            "timelines": [{
                "id": "TL1", "video": {"width": 1920, "height": 1080,
                                     "rateNum": 30, "rateDen": 1},
                "tracks": [{"id": "T1", "visible": True},
                           {"id": "T2", "muted": False}],
                "clips": [
                    {"id": "c1", "trackId": "T1", "mediaId": "m1",
                     "kind": "video", "start": 0.0, "duration": 5.0,
                     "sourceStart": 0.0, "speed": 1.0},
                    {"id": "c2", "trackId": "T2", "mediaId": "m2",
                     "kind": "audio", "start": 0.0, "duration": 5.0}
                ]}]
        }
        self.save()

    def save(self):
        (self.src / "concat.json").write_text(json.dumps(self.doc), encoding="utf-8")

    def test_native_concat_json_maps_to_cuda_without_schema_change(self):
        plan = concat_stage1_plan(self.src)
        saved = json.loads(plan.read_text())
        self.assertEqual(saved["clips"], [{"path": "clip.mp4", "kind": "video",
                                          "duration": 5.0, "source_start": 0.0}])
        self.assertEqual(saved["voice"], "voice.wav")
        self.assertEqual(saved["fps"], 30)
        self.assertTrue((self.src / "concat.json").exists())

    def test_rejects_unsupported_effect_instead_of_cpu_fallback(self):
        self.doc["timelines"][0]["clips"][0]["transitionIn"] = {"id": "wipe"}
        self.save()
        with self.assertRaisesRegex(ValueError, "transitionIn"):
            concat_stage1_plan(self.src)

    def test_rejects_overlap_instead_of_silent_dropped_clip(self):
        second = dict(self.doc["timelines"][0]["clips"][0])
        second.update({"id": "c3", "start": 3.0, "duration": 2.0})
        self.doc["timelines"][0]["clips"].append(second)
        self.save()
        with self.assertRaisesRegex(ValueError, "gaps/overlaps"):
            concat_stage1_plan(self.src)

    def test_rejects_embedded_video_audio_that_would_be_dropped(self):
        self.doc["media"][0]["hasAudio"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "mute embedded"):
            concat_stage1_plan(self.src)
        self.doc["timelines"][0]["clips"][0]["muted"] = True
        self.save()
        self.assertTrue(concat_stage1_plan(self.src).exists())

    def test_rejects_media_outside_restored_project(self):
        self.doc["media"][0]["path"] = str(self.root / "external.mp4")
        (self.root / "external.mp4").write_bytes(b"something")
        self.save()
        with self.assertRaisesRegex(ValueError, "escapes"):
            concat_stage1_plan(self.src)

    def test_resumable_cuda_mode_calls_same_worker_and_delivers(self):
        z = self.root / "native.zip"
        pack(self.src, z)
        raw = z.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        delivered = []
        worker = RenderWorker(self.root / "worker",
                              token=TOKEN, renderer=lambda *_: self.fail("Render unexpectedly called"),
                              deliver=lambda job, out: delivered.append(job))
        worker.receiver = "https://app.scenebuilder.online"
        gpu_calls = []
        def mock_cuda(project, output):
            gpu_calls.append(project)
            self.assertTrue((project / "concat.json").exists())
            self.assertEqual(json.loads(concat_stage1_plan(project).read_text())["voice"],
                             "media/" + next(x.name for x in (project / "media").iterdir()
                             if x.read_bytes() == b"voice-contents"))
            output.write_bytes(MP4)
        worker.renderer = mock_cuda
        try:
            first = worker.spool.begin("cuda1", len(raw), sha, "cuda-stage1")
            self.assertEqual(first["render_mode"], "cuda-stage1")
            worker.spool.put_chunk("cuda1", 0, io.BytesIO(raw), len(raw), sha)
            worker.spool.complete("cuda1")
            worker.pool.shutdown(wait=True)
            self.assertEqual(worker.status("cuda1")["state"], "delivered")
            self.assertEqual(delivered, ["cuda1"])
            self.assertEqual(len(gpu_calls), 1)
            self.assertFalse((worker.outputs / "cuda1.mp4").exists())
        finally:
            worker.pool.shutdown(wait=True)

    def test_resumable_mode_cannot_change_during_retry(self):
        worker = RenderWorker(self.root / "worker2",
                              token=TOKEN, renderer=lambda *_: None)
        try:
            data = b"valid-zip-future"
            checksum = hashlib.sha256(data).hexdigest()
            worker.spool.begin("a",len(data),checksum,"cuda-stage1")
            with self.assertRaisesRegex(ValueError, "Native Vulkan engine retired"):
                worker.spool.begin("a",len(data),checksum,"native")
            self.assertTrue((worker.spool.uploads / "a" / "manifest.json").is_file())
        finally:
            worker.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
