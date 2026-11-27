"""Offline Production coverage using the existing Media manager and worker protocol."""
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

SCENE = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
sys.path.insert(0, str(SCENE))
from cloud_render import CloudRender, RcloneFailure
from cloud_render_production import CHUNK, MODE, JobError, Worker, WorkerWait, drive_link

FOLDER = "https://drive.google.com/drive/u/2/folders/folder1?resourcekey=folder-key"
FILE = "https://drive.google.com/file/d/project1/view?resourcekey=project-key"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"verified output fixture"


def project_zip(label):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("bundle.json", json.dumps({
            "version": 1, "project_file": "concat.json", "references": []
        }))
        bundle.writestr("project/concat.json", json.dumps({"media": [], "label": label}))
    return stream.getvalue()


class WorkerFixture(Worker):
    def __init__(self):
        super().__init__("https://fixture.salad.cloud", "token", "gateway", "/health/ready", 15)
        self.online = True
        self.concurrency = 1
        self.chunks = {}
        self.sessions = {}
        self.jobs = {}
        self.output = MP4
        self.valid = True
        self.manifest_limit = 64 * 1024 ** 2
        self.fail_put_once = False
        self.lose_complete_once = False
        self.requests = []

    def request(self, method, path, *, missing=False, stream=False, **kwargs):
        self.requests.append((method, path))
        if not self.online:
            raise WorkerWait("Fixture offline.")
        if path == self.health:
            return {"ok": True}
        if path == "/api/config/concurrency":
            self.concurrency = kwargs["json"]["concurrency"]
            return {"concurrency": self.concurrency}
        if path == "/api/capabilities":
            caps = {"api_version": 1, "validation_version": 1, "bundle_versions": [1],
                    "render_modes": [MODE]}
            if self.manifest_limit is not None:
                caps["max_project_manifest_bytes"] = self.manifest_limit
            return caps
        if path == "/api/validate":
            json.loads(kwargs["data"])
            return {"ok": self.valid, "errors": ["Unsupported effect fixture"]}
        parts = path.split("/")
        job = parts[3]
        if parts[2] == "jobs":
            if stream:
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.iter_content.return_value = [self.output]
                return response
            return self.jobs.get(job)
        if len(parts) == 4:
            self.sessions[job] = kwargs["json"]
            self.chunks.setdefault(job, {})
            return {**kwargs["json"], "chunk_size": CHUNK,
                    "total_chunks": (kwargs["json"]["size"] + CHUNK - 1) // CHUNK,
                    "uploaded": list(self.chunks[job])}
        if parts[4] == "chunks":
            data = kwargs["data"]
            assert hashlib.sha256(data).hexdigest() == kwargs["headers"]["X-Content-SHA256"]
            self.chunks[job][int(parts[5])] = data
            if self.fail_put_once:
                self.fail_put_once = False
                raise WorkerWait("Lost acknowledgement for a verified chunk.")
            return {"ok": True}
        archive = b"".join(data for _, data in sorted(self.chunks[job].items()))
        assert hashlib.sha256(archive).hexdigest() == self.sessions[job]["sha256"]
        self.jobs[job] = {**self.sessions[job], "state": "running"}
        if self.lose_complete_once:
            self.lose_complete_once = False
            raise WorkerWait("Lost acknowledgement for complete.")
        return self.jobs[job]

    def rendered(self, job):
        self.jobs[job].update(state="rendered", output_size=len(self.output),
                              output_sha256=hashlib.sha256(self.output).hexdigest())


class ProductionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worker = WorkerFixture()
        self.files = {"project1": project_zip("one"), "project2": project_zip("two")}
        self.drive_calls = []
        self.reopen()

    def reopen(self):
        self.media = CloudRender(self.temp.name)
        self.addCleanup(self.media.pool.shutdown, wait=True)
        self.queue = self.media.production
        self.queue.worker_factory = lambda _: self.worker
        self.queue.drive.execute = self.execute

    def execute(self, cmd, timeout):
        self.drive_calls.append(cmd)
        if cmd[1] == "lsjson":
            return json.dumps([
                {"ID": key, "Name": "Same name.zip", "Path": "Same name.zip",
                 "Size": len(data), "Hashes": {"MD5": hashlib.md5(data).hexdigest()}}
                for key, data in self.files.items()
            ] + [{"ID": "ignore", "Name": "notes.txt"}])
        self.assertEqual(cmd[:4], ["rclone", "backend", "copyid", "gdrive:"])
        target = Path(cmd[5])
        if cmd[5].endswith("/"):
            target = target / "From another account.zip"
        target.write_bytes(self.files[cmd[4]])
        return ""

    def row(self, index=0):
        with self.media.connection() as con:
            return con.execute("SELECT * FROM production_jobs ORDER BY id").fetchall()[index]

    def state(self, index=0):
        return self.queue.snapshot()["items"][index]["state"]

    def start_file(self):
        self.queue.scan(FILE)
        self.queue.control("start")
        self.queue.tick()
        return self.row()["remote_id"]

    def test_concurrency_two_dispatches_two_jobs_on_one_worker(self):
        self.media.save({"concurrency": 2})
        self.queue.scan(FOLDER)
        self.queue.control("start")
        self.queue.tick()
        self.assertEqual(self.state(0), "Rendering")
        self.assertEqual(self.state(1), "Queued")
        self.queue.tick()
        self.assertEqual(self.state(1), "Rendering")
        self.assertEqual(len(self.worker.jobs), 2)
        self.assertEqual(self.worker.concurrency, 2)
        self.assertEqual(self.queue.snapshot()["active"], 2)
        self.assertEqual(self.queue.snapshot()["concurrency"], 2)

    def test_lower_concurrency_does_not_cancel_active_jobs(self):
        self.media.save({"concurrency": 2})
        self.queue.scan(FOLDER)
        self.queue.control("start")
        self.queue.tick()
        self.queue.tick()
        self.media.save({"concurrency": 1})
        self.assertEqual(self.queue.snapshot()["active"], 2)
        self.assertEqual(self.state(0), "Rendering")
        self.assertEqual(self.state(1), "Rendering")
        # The setting must persist across the web process restart.
        self.reopen()
        self.assertEqual(self.media.config()["concurrency"], 1)
        self.assertEqual(self.queue.snapshot()["active"], 2)
        self.queue.tick()
        self.assertEqual(len(self.worker.jobs), 2)
        self.assertEqual(self.worker.concurrency, 1)

    def test_reject_invalid_concurrency(self):
        for value in (0, 3, True, "2", None, 1.5):
            with self.subTest(value=value), self.assertRaises(Exception):
                self.media.save({"concurrency": value})
        self.assertEqual(self.media.config()["concurrency"], 1)

    def test_links_require_google_origin_and_keep_resource_keys(self):
        self.assertEqual(drive_link(FOLDER), ("folder1", "folder-key", True))
        self.assertEqual(drive_link(FILE), ("project1", "project-key", False))
        self.assertEqual(drive_link("https://drive.google.com/open?id=private"), ("private", "", False))
        for value in ("http://drive.google.com/open?id=x", "https://drive.google.com.evil/open?id=x",
                      "https://drive.google.com@evil/open?id=x", "https://drive.google.com/open?id=a&id=b",
                      "https://drive.google.com/open?id=a&resourcekey=abc%0A"):
            with self.subTest(value=value), self.assertRaises(JobError):
                drive_link(value)

    def test_folder_scans_shared_children_by_id_and_deduplicates_direct_link(self):
        self.assertEqual(self.queue.scan(FOLDER), {"added": 2, "duplicates": 0})
        self.assertIn("--recursive", self.drive_calls[0])
        self.assertIn("folder-key", self.drive_calls[0])
        self.assertEqual(self.queue.scan(FILE), {"added": 0, "duplicates": 1})
        self.queue.control("start")
        self.queue.tick()
        self.assertIn("X-Goog-Drive-Resource-Keys: folder1/folder-key,project1/project-key", self.drive_calls[1])
        self.assertEqual(self.state(), "Rendering")
        self.assertEqual(self.state(1), "Queued")
        self.assertEqual(len(self.worker.jobs), 1)

    def test_private_file_uses_canonical_account_and_public_file_uses_resource_key(self):
        self.queue.scan("https://drive.google.com/file/d/project1/view")
        self.queue.control("start")
        self.queue.tick()
        self.assertNotIn("--header", self.drive_calls[0])
        self.assertEqual(self.row()["name"], "From another account.zip")
        self.assertEqual(self.queue.scan("https://drive.google.com/file/d/project2/view?resourcekey=public-key")["added"], 1)
        self.queue.action(self.row()["id"], "cancel")
        self.worker.rendered(self.row()["remote_id"])
        self.queue.tick()
        self.queue.tick()
        self.assertIn("X-Goog-Drive-Resource-Keys: project2/public-key", self.drive_calls[-1])

    def test_keyed_folder_fallback_verifies_the_scanned_file_hash(self):
        self.queue.scan(FOLDER)
        self.queue.control("start")
        def fallback(cmd, timeout):
            if cmd[1] == "backend":
                raise RcloneFailure("backend", 4, "remote_file_absent")
            self.assertEqual(cmd[1:3], ["copyto", "gdrive:Same name.zip"])
            self.assertIn("folder-key", cmd)
            Path(cmd[3]).write_bytes(self.files["project2"])
        self.queue.drive.execute = fallback
        self.queue.tick()
        self.assertEqual(self.state(), "Failed")
        self.assertFalse(self.worker.jobs)
        self.assertIn("changed after the scan", self.row()["error"])

    def test_invalid_zip_is_retained_and_retry_recovers(self):
        self.files["project1"] = b"not a ZIP"
        self.start_file()
        self.assertEqual(self.state(), "Failed")
        self.assertTrue(self.queue.archive(self.row()["id"]).with_suffix(".part").is_file())
        self.assertFalse(self.worker.jobs)
        self.files["project1"] = project_zip("fixed")
        self.queue.action(self.row()["id"], "retry")
        self.queue.tick()
        self.assertEqual(self.state(), "Rendering")

    def test_compatibility_error_prevents_any_gpu_zip_upload(self):
        self.worker.valid = False
        self.start_file()
        self.assertEqual(self.state(), "Failed")
        self.assertIn("Unsupported effect", self.row()["error"])
        self.assertFalse(self.worker.sessions)

    def test_existing_worker_without_advertised_manifest_limit_can_render(self):
        self.worker.manifest_limit = None
        job = self.start_file()
        self.worker.rendered(job)
        self.queue.tick()
        self.assertEqual(self.state(), "Completed")

    def test_pause_and_resume_survive_restart_and_cancel_queued_job(self):
        self.queue.scan(FOLDER)
        self.queue.control("start")
        self.queue.control("pause")
        self.reopen()
        self.queue.tick()
        self.assertEqual(self.state(), "Queued")
        self.assertEqual(self.queue.snapshot()["state"], "paused")
        first = self.row()["id"]
        self.queue.action(first, "cancel")
        self.assertEqual(self.state(), "Cancelled")
        self.queue.action(first, "delete")
        self.queue.control("resume")
        self.queue.tick()
        self.assertEqual(self.state(), "Rendering")
        self.assertEqual(self.row()["drive_id"], "project2")

    def test_worker_outage_chunk_resume_and_lost_complete_recover_after_restart(self):
        self.worker.online = False
        job = self.start_file()
        self.assertEqual(self.state(), "Waiting for Worker")
        self.worker.online = True
        self.worker.fail_put_once = True
        self.queue.tick()
        self.assertEqual(self.state(), "Waiting for Worker")
        self.worker.lose_complete_once = True
        self.reopen()
        self.queue.tick()
        self.assertEqual(self.state(), "Waiting for Worker")
        self.reopen()
        self.queue.tick()
        self.assertEqual(self.state(), "Rendering")
        self.assertEqual(list(self.worker.jobs), [job])
        self.assertEqual(len(self.drive_calls), 1)
        self.assertEqual(sum(method == "PUT" for method, _ in self.worker.requests), 1)

    def test_verified_pull_is_delivered_to_media_and_cleans_input(self):
        job = self.start_file()
        self.worker.rendered(job)
        self.queue.tick()
        self.assertEqual(self.state(), "Completed")
        self.assertEqual(self.media.local(job).read_bytes(), MP4)
        self.assertEqual(self.media.listing()["items"][0]["name"], "From another account.mp4")
        self.assertFalse(self.queue.archive(self.row()["id"]).exists())

    def test_bad_output_retains_input_and_retry_uses_same_render(self):
        job = self.start_file()
        self.worker.rendered(job)
        self.worker.output = b"incorrect"
        self.queue.tick()
        self.assertEqual(self.state(), "Failed")
        self.assertTrue(self.queue.archive(self.row()["id"]).is_file())
        self.assertEqual(self.media.listing()["items"], [])
        self.worker.output = MP4
        self.queue.action(self.row()["id"], "retry")
        self.queue.tick()
        self.assertEqual(self.state(), "Completed")
        self.assertEqual(list(self.worker.jobs), [job])

    def test_worker_render_failure_retries_with_new_job_id_and_same_zip(self):
        job = self.start_file()
        self.worker.jobs[job].update(state="failed", error="Renderer timed out")
        self.queue.tick()
        self.assertEqual(self.state(), "Failed")
        self.queue.action(self.row()["id"], "retry")
        self.queue.tick()
        self.assertEqual(self.state(), "Rendering")
        self.assertNotEqual(self.row()["remote_id"], job)
        self.assertEqual(len(self.drive_calls), 1)

    def test_cancel_inflight_waits_for_worker_terminal_before_next_dispatch(self):
        self.queue.scan(FOLDER)
        self.queue.control("start")
        self.queue.tick()
        job = self.row()["remote_id"]
        self.queue.action(self.row()["id"], "cancel")
        self.queue.tick()
        self.assertEqual(self.state(), "Rendering")
        self.assertEqual(self.state(1), "Queued")
        self.worker.rendered(job)
        self.queue.tick()
        self.assertEqual(self.state(), "Cancelled")
        self.assertEqual(self.media.listing()["items"], [])
        self.queue.tick()
        self.assertEqual(self.state(1), "Rendering")

    def test_verified_push_is_idempotent_and_survives_media_delete_before_tick(self):
        job = self.start_file()
        sha = hashlib.sha256(MP4).hexdigest()
        self.media.receive(job, 0, MP4[:12], name=job+".mp4", size=len(MP4), sha=sha)
        done = self.media.receive(job, 1, MP4[12:], name=job+".mp4", size=len(MP4), sha=sha)
        self.assertEqual(done["state"], "ready")
        self.media.delete(job)
        self.assertEqual(self.media.receive(job, 0, MP4, name=job+".mp4", size=len(MP4), sha=sha)["state"], "ready")
        self.reopen()
        self.queue.tick()
        self.assertEqual(self.state(), "Completed")
        self.assertEqual(self.media.listing()["items"], [])

    def test_push_recovers_a_crash_after_final_file_rename(self):
        job = self.start_file()
        sha = hashlib.sha256(MP4).hexdigest()
        self.media.receive(job, 0, MP4[:12], name=job+".mp4", size=len(MP4), sha=sha)
        (self.media.staging / (job+".part")).unlink()
        self.media.local(job).write_bytes(MP4)
        self.reopen()
        self.assertEqual(self.media.receive(job, 0, MP4[:12], name=job+".mp4", size=len(MP4), sha=sha)["state"], "ready")
        self.queue.tick()
        self.assertEqual(self.state(), "Completed")

    def test_verified_pull_uses_optional_drive_output_queue(self):
        job = self.start_file()
        self.media.save({"auto_upload": True})
        self.worker.rendered(job)
        self.media.pool.submit = Mock()
        self.queue.tick()
        self.assertEqual(self.state(), "Completed")
        self.assertEqual(self.media.listing()["items"][0]["state"], "queued")
        self.media.pool.submit.assert_called_once_with(self.media._upload, job)
