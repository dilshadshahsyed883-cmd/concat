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
from app.modules.concat.engine.cloud_render_production import bundle_manifest, JobError, MAX_MANIFEST


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


class ManifestSizeTests(unittest.TestCase):
    def test_manifest_above_old_limit(self):
        self.assertEqual(MAX_MANIFEST, 128 * 1024 ** 2)
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "large.zip"
            body = b'{"media":[]}'
            body += b" " * (65 * 1024 ** 2 - len(body))
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("bundle.json", json.dumps({
                    "version": 1, "project_file": "concat.json", "references": []}))
                archive.writestr("project/concat.json", body)
            self.assertEqual(len(bundle_manifest(archive_path)), len(body))

    def test_manifest_above_new_limit_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "oversized.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("bundle.json", json.dumps({
                    "version": 1, "project_file": "concat.json", "references": []}))
                with archive.open("project/concat.json", "w") as member:
                    for _ in range(128):
                        member.write(b" " * (1024 ** 2))
                    member.write(b" ")
            with self.assertRaisesRegex(JobError, "128 MiB"):
                bundle_manifest(archive_path)


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

    def temporary_failure(self, row, destination):
        from app.modules.concat.engine.drive_socket import RetryableDriveError
        self.download_calls += 1
        destination.write_bytes(b"partial")
        raise RetryableDriveError("connection interrupted")

    def test_retry_waits_forty_seconds_then_recovers_and_cleans_partial(self):
        good_download = self.queue.drive.download
        self.queue.drive.download = self.temporary_failure
        with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=100):
            self.queue.tick_intake()
        self.assertEqual(self.state()["state"], "Retrying Drive")
        self.assertEqual(self.state()["intake_retries"], 1)
        self.assertEqual(self.state()["intake_retry_at"], 140)
        self.assertEqual(self.queue.snapshot()["active"], 0)
        self.queue.drive.download = good_download
        with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=139):
            self.assertEqual(self.queue.tick_intake(), 1)
        self.assertEqual(self.download_calls, 1)
        with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=140):
            self.queue.tick_intake()
        self.assertEqual(self.download_calls, 2)
        self.assertEqual(self.state()["state"], "Ready for GPU")
        self.assertEqual(self.state()["intake_retry_at"], 0)
        self.assertEqual(self.queue.archive(1).read_bytes(), self.fixture.read_bytes())

    def test_exactly_five_retries_then_manual_retry_resets_budget(self):
        self.queue.drive.download = self.temporary_failure
        for attempt in range(6):
            with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=100 + 40 * attempt):
                self.queue.tick_intake()
            self.assertEqual(self.state()["state"], "Failed" if attempt == 5 else "Retrying Drive")
        self.assertEqual(self.download_calls, 6)
        self.assertEqual(self.state()["intake_retries"], 5)
        self.queue.tick_intake()
        self.assertEqual(self.download_calls, 6)
        self.queue.action(1, "retry")
        self.assertEqual(self.state()["intake_retries"], 0)
        self.assertEqual(self.state()["intake_retry_at"], 0)
        self.assertEqual(self.state()["state"], "Queued")

    def test_existing_database_migration_preserves_jobs(self):
        from app.modules.concat.engine.cloud_render_production import ProductionQueue
        with self.media.connection() as con:
            con.execute("ALTER TABLE production_jobs DROP COLUMN intake_retries")
            con.execute("ALTER TABLE production_jobs DROP COLUMN intake_retry_at")
        restored = ProductionQueue(self.media, self.queue.worker_factory)
        self.assertEqual(self.state()["intake_retries"], 0)
        self.assertEqual(self.state()["intake_retry_at"], 0)
        self.assertEqual(restored.snapshot()["items"][0]["name"], "source.zip")
        ProductionQueue(self.media, self.queue.worker_factory)
        self.assertEqual(self.state()["state"], "Queued")

    def test_restart_preserves_retry_deadline_and_budget(self):
        from app.modules.concat.engine.cloud_render_production import ProductionQueue
        self.queue.drive.download = self.temporary_failure
        with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=100):
            self.queue.tick_intake()
        restored = ProductionQueue(self.media, self.queue.worker_factory)
        restored.drive.download = self.temporary_failure
        with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=139):
            restored.tick_intake()
        self.assertEqual(self.download_calls, 1)
        self.assertEqual(self.state()["intake_retries"], 1)
        with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=140):
            restored.tick_intake()
        self.assertEqual(self.download_calls, 2)
        self.assertEqual(self.state()["intake_retries"], 2)

    def test_retry_wait_does_not_block_other_job_or_cancel(self):
        self.queue.drive.download = self.temporary_failure
        with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=100):
            self.queue.tick_intake()
        self.add_job("second-drive")
        self.queue.drive.download = lambda row, destination: (destination.write_bytes(
            self.fixture.read_bytes()) and "source.zip")
        with patch("app.modules.concat.engine.cloud_render_production.time.time", return_value=101):
            self.queue.tick_intake()
        self.assertEqual(self.state(2)["state"], "Ready for GPU")
        self.queue.action(1, "cancel")
        self.queue.tick_intake()
        self.assertEqual(self.state()["state"], "Cancelled")
        self.assertEqual(self.download_calls, 1)

    def test_permanent_drive_error_does_not_retry(self):
        self.queue.drive.download = lambda *args: (_ for _ in ()).throw(RuntimeError("permission denied"))
        self.queue.tick_intake()
        self.assertEqual(self.state()["state"], "Failed")
        self.assertEqual(self.state()["intake_retries"], 0)

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

    def check_pipeline_prefetch(self, concurrency):
        self.queue.pipeline_enabled = True
        self.worker.configure_concurrency = lambda value: {"concurrency": value, "pipeline_version": 1}
        self.queue.control("start")
        with self.media.connection() as con:
            self.media.put(con, "render_concurrency", str(concurrency))
        # Seed a delivery and the requested number of actual renders.
        for i in range(2, concurrency + 4):
            self.add_job("pipeline-" + str(i))
        digest = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        with self.media.connection() as con:
            con.execute("UPDATE production_jobs SET state='Delivering',digest=?,endpoint=?,submitted_at=1 WHERE id=1",
                        (digest, self.worker.endpoint))
            for i in range(2, concurrency + 2):
                con.execute("UPDATE production_jobs SET state='Rendering',digest=?,endpoint=?,submitted_at=1 WHERE id=?",
                            (digest, self.worker.endpoint, i))
        for i in (concurrency + 2, concurrency + 3):
            self.queue.archive(i).write_bytes(self.fixture.read_bytes())
            self.queue.update(i, state="Ready for GPU", digest=digest)
        def request(method, path, **kwargs):
            remote = path.rsplit("/", 1)[-1]
            with self.media.connection() as con:
                row = con.execute("SELECT state FROM production_jobs WHERE remote_id=?", (remote,)).fetchone()
            states = {"Rendering": "running", "Delivering": "delivering", "GPU Ready": "queued"}
            return {"state": states[row["state"]], "sha256": digest, "render_mode": "cuda-hybrid-v1"}
        self.worker.request = request
        self.queue.tick()
        self.assertEqual(len(self.worker.uploads), 1)
        snap = self.queue.snapshot()
        self.assertEqual(snap["rendering"], concurrency)
        self.assertEqual(snap["gpu_ready"], 1)
        self.assertEqual(snap["delivering"], 1)
        self.assertEqual(snap["active"], concurrency + 2)
        # A second preloaded ZIP would exceed the bounded lookahead.
        for _ in range(4):
            self.queue.tick()
        self.assertEqual(len(self.worker.uploads), 1)
        # Once a render releases its slot and the prefetched job starts,
        # the next ZIP can upload even while the older MP4 is delivering.
        self.queue.update(2, state="Completed")
        self.queue.update(concurrency + 2, state="Rendering")
        self.queue.tick()
        self.assertEqual(len(self.worker.uploads), 2)

    def test_pipeline_prefetch_while_serial_render_and_delivery_active(self):
        self.check_pipeline_prefetch(1)

    def test_pipeline_prefetch_while_parallel_renders_and_delivery_active(self):
        self.check_pipeline_prefetch(2)

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
