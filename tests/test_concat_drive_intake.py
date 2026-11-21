"""Offline regressions for independent Concat Drive intake and GPU dispatch.

No real Drive, Salad API, SSH or production services are contacted.
"""
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.modules.concat.engine.cloud_render import CloudRender
from app.modules.concat.engine.cloud_render_production import bundle_manifest, JobError


class FakeWorker:
    endpoint = "https://test-renderer.salad.cloud"
    timeout = 60

    def __init__(self):
        self.uploads = []
        self.concurrency = []

    def configure_concurrency(self, value):
        self.concurrency.append(value)

    def upload(self, remote_id, path, size, digest, manifest, checkpoint, uploading, submit):
        checkpoint()
        uploading()
        self.uploads.append((remote_id, size, digest, manifest))
        submit()


def bundle(target, *, bad_media=False):
    payload = b"offline-fixture-media"
    digest = hashlib.sha256(b"different" if bad_media else payload).hexdigest()
    ref = {"member": "media/" + "a" * 32 + ".wav", "index": 0,
           "sha256": digest, "size": len(payload)}
    control = {"version": 1, "project_file": "concat.json", "references": [ref]}
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("bundle.json", json.dumps(control))
        archive.writestr("project/concat.json", json.dumps({"media": [{"path": "voice.wav"}]}))
        archive.writestr(ref["member"], payload)
    return target


class IndependentDriveIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = CloudRender(self.root / "owner", tenant_key="a" * 64)
        self.queue = self.media.production
        self.fixture = bundle(self.root / "source.zip")
        self.download_calls = 0

        def mock_download(row, destination):
            self.download_calls += 1
            destination.write_bytes(self.fixture.read_bytes())
            return "source.zip"

        self.queue.drive.download = mock_download
        self.worker = FakeWorker()
        self.queue.worker_factory = lambda pinned: self.worker
        self.add_job()

    def tearDown(self):
        self.media.pool.shutdown(wait=True, cancel_futures=True)
        self.tmp.cleanup()

    def add_job(self, drive_id="drive-test"):
        with self.media.connection() as con:
            con.execute("""INSERT INTO production_jobs
                (remote_id,drive_id,name,resource_keys,drive_size,drive_md5,folder_id,drive_path)
                VALUES (?,?,?,?,?,?,?,?)""",
                ("production-" + drive_id, drive_id, "source.zip", "",
                 self.fixture.stat().st_size, hashlib.md5(self.fixture.read_bytes()).hexdigest(),
                 "drive-folder", "source.zip"))
            return con.execute("SELECT last_insert_rowid()").fetchone()[0]

    def state(self, job=1):
        with self.media.connection() as con:
            return con.execute("SELECT * FROM production_jobs WHERE id=?", (job,)).fetchone()

    def test_offline_intake_ready_without_worker(self):
        self.queue.worker_factory = lambda pinned: self.fail("GPU worker used during Drive intake")
        self.assertEqual(self.queue.tick_intake(), 0)
        self.assertEqual(self.state()["state"], "Ready for GPU")
        self.assertEqual(self.download_calls, 1)
        self.assertTrue(self.queue.archive(1).is_file())
        self.assertEqual(self.queue.snapshot()["active"], 0)
        self.assertEqual(self.queue.snapshot()["ready_for_gpu"], 1)

    def test_ready_job_dispatches_after_worker_available(self):
        self.queue.tick_intake()
        self.queue.control("start")
        self.queue.tick()
        self.assertEqual(len(self.worker.uploads), 1)
        self.assertEqual(self.download_calls, 1)
        self.assertEqual(self.state()["state"], "Rendering")
        self.assertEqual(self.queue.snapshot()["active"], 1)

    def test_worker_unavailable_keeps_zip_and_no_redownload(self):
        self.queue.tick_intake()
        self.queue.control("start")
        from app.modules.concat.engine.cloud_render_production import WorkerWait
        self.queue.worker_factory = lambda pinned: (_ for _ in ()).throw(WorkerWait("offline"))
        self.queue.tick()
        self.assertEqual(self.state()["state"], "Ready for GPU")
        self.assertEqual(self.queue.snapshot()["active"], 0)
        self.assertTrue(self.queue.archive(1).is_file())
        self.assertEqual(self.download_calls, 1)
        self.queue.worker_factory = lambda pinned: self.worker
        self.queue.tick()
        self.assertEqual(len(self.worker.uploads), 1)
        self.assertEqual(self.download_calls, 1)

    def test_pause_blocks_new_upload_but_not_drive_intake(self):
        self.queue.control("start")
        self.queue.control("pause")
        self.queue.tick_intake()
        self.assertEqual(self.state()["state"], "Ready for GPU")
        self.queue.tick()
        self.assertEqual(self.worker.uploads, [])
        self.queue.control("resume")
        self.queue.tick()
        self.assertEqual(len(self.worker.uploads), 1)

    def test_restart_rechecks_cached_zip_without_downloading(self):
        self.queue.tick_intake()
        self.queue.update(1, state="Verifying ZIP")
        self.queue.tick_intake()
        self.assertEqual(self.download_calls, 1)
        self.assertEqual(self.state()["state"], "Ready for GPU")

    def test_corrupt_media_zip_rejected_before_upload(self):
        bundle(self.fixture, bad_media=True)
        # Scan metadata must describe the corrupt bundle so this regression
        # reaches media SHA-256 validation, rather than the Drive MD5 guard.
        with self.media.connection() as con:
            con.execute("UPDATE production_jobs SET drive_size=?,drive_md5=? WHERE id=1",
                        (self.fixture.stat().st_size,
                         hashlib.md5(self.fixture.read_bytes()).hexdigest()))
        self.queue.tick_intake()
        self.assertEqual(self.state()["state"], "Failed")
        self.assertIn("checksum", self.state()["error"].lower())
        self.assertEqual(self.worker.uploads, [])

    def test_drive_content_changed_after_scan_rejected_before_upload(self):
        bundle(self.fixture, bad_media=True)
        self.queue.tick_intake()
        self.assertEqual(self.state()["state"], "Failed")
        self.assertIn("content changed after the scan", self.state()["error"].lower())
        self.assertFalse(self.queue.archive(1).exists())
        self.assertEqual(self.worker.uploads, [])

    def test_intake_not_counted_against_two_gpu_slots(self):
        with self.media.connection() as con:
            self.media.put(con, "render_concurrency", 2)
        self.assertEqual(self.queue.snapshot()["active"], 0)
        self.queue.update(1, state="Downloading")
        self.assertEqual(self.queue.snapshot()["intake_active"], 1)
        self.assertEqual(self.queue.snapshot()["active"], 0)
        self.queue.tick_intake()
        self.assertEqual(self.queue.snapshot()["ready_for_gpu"], 1)

    def test_scan_arms_dispatch_but_preserves_explicit_pause(self):
        self.queue.drive.discover = lambda link: [
            ("other-drive", "another.zip", "", self.fixture.stat().st_size, "",
             "folder", "another.zip")]
        self.assertEqual(self.queue.scan("unused")["added"], 1)
        self.assertEqual(self.queue.snapshot()["state"], "running")
        self.queue.control("pause")
        self.queue.drive.discover = lambda link: [
            ("new-drive", "third.zip", "", self.fixture.stat().st_size, "", "folder", "third.zip")]
        self.queue.scan("unused")
        self.assertEqual(self.queue.snapshot()["state"], "paused")


if __name__ == "__main__":
    unittest.main()
