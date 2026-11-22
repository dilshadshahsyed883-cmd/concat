"""EXE ↔ Salad worker render contract; legacy jobs remain retryable."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cloud_render.concat.gpu.worker import RenderWorker
from cloud_render.concat.hybrid.contract import RENDER_CONTRACT_ID, RENDER_CONTRACT_VERSION
from cloud_render.concat.project_bundle import TransferError, pack, restore


class RenderContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "source"
        self.project.mkdir()
        (self.project / "concat.json").write_text(
            json.dumps({"version": 1, "media": []}), encoding="utf-8")

    def rewrite_control(self, source, destination, edit):
        with zipfile.ZipFile(source) as reader:
            control = json.loads(reader.read("bundle.json"))
            edit(control)
            project = reader.read("project/concat.json")
        with zipfile.ZipFile(destination, "w") as output:
            output.writestr("bundle.json", json.dumps(control))
            output.writestr("project/concat.json", project)

    def test_python_package_declares_contract_and_worker_restores_it(self):
        archive = self.root / "project.zip"
        pack(self.project, archive)
        with zipfile.ZipFile(archive) as file:
            data = json.loads(file.read("bundle.json"))
        self.assertEqual(data["render_contract"], {
            "id": RENDER_CONTRACT_ID, "version": RENDER_CONTRACT_VERSION})
        output = self.root / "restored"
        restore(archive, output)
        self.assertTrue((output / "concat.json").is_file())

    def test_mismatched_or_malformed_render_contract_is_rejected_by_worker_restore(self):
        source = self.root / "source.zip"
        pack(self.project, source)
        for name, change in (
            ("wrong-id", lambda c: c.update(render_contract={"id": "wrong", "version": 1})),
            ("wrong-version", lambda c: c.update(render_contract={"id": RENDER_CONTRACT_ID, "version": RENDER_CONTRACT_VERSION + 1})),
            ("boolean-version", lambda c: c.update(render_contract={"id": RENDER_CONTRACT_ID, "version": True})),
            ("string-version", lambda c: c.update(render_contract={"id": RENDER_CONTRACT_ID, "version": "1"})),
            ("float-version", lambda c: c.update(render_contract={"id": RENDER_CONTRACT_ID, "version": 1.0})),
            ("case-changed-id", lambda c: c.update(render_contract={"id": RENDER_CONTRACT_ID.upper(), "version": 1})),
            ("missing-version", lambda c: c.update(render_contract={"id": RENDER_CONTRACT_ID})),
            ("null-contract", lambda c: c.update(render_contract=None)),
            ("array-contract", lambda c: c.update(render_contract=[{"id": RENDER_CONTRACT_ID, "version": 1}])),
            ("invalid-type", lambda c: c.update(render_contract="invalid")),
        ):
            with self.subTest(name=name):
                altered = self.root / (name + ".zip")
                self.rewrite_control(source, altered, change)
                with self.assertRaisesRegex(TransferError, "contract does not match"):
                    restore(altered, self.root / (name + "-out"))
                self.assertFalse((self.root / (name + "-out")).exists())

    def test_old_v1_zip_without_contract_is_still_recoverable(self):
        source = self.root / "project.zip"
        pack(self.project, source)
        old = self.root / "old.zip"
        self.rewrite_control(source, old, lambda c: c.pop("render_contract"))
        restore(old, self.root / "legacy-out")
        self.assertTrue((self.root / "legacy-out/concat.json").is_file())

    def test_stored_contract_v1_zip_remains_readable_by_v2_worker(self):
        source = self.root / "current.zip"
        pack(self.project, source)
        old = self.root / "stored-v1.zip"
        self.rewrite_control(source, old, lambda c:c.update(render_contract={"id":RENDER_CONTRACT_ID,"version":1}))
        restore(old,self.root / "stored-v1-out")
        self.assertTrue((self.root / "stored-v1-out/concat.json").is_file())

    def test_embedded_and_standalone_windows_helpers_have_identical_contract(self):
        root = Path(__file__).resolve().parents[3]
        standalone = (root / "concat_gpu/concat_gpu_client.ps1").read_text(encoding="utf-8")
        embedded = (root / "upstream/src/crates/concat/src/concat_gpu_client.ps1").read_text(encoding="utf-8")
        self.assertEqual(standalone, embedded)
        self.assertEqual((root / "concat_gpu/concat_gpu_client.ps1").read_bytes(),
                         (root / "upstream/src/crates/concat/src/concat_gpu_client.ps1").read_bytes())
        self.assertIn('render_contract=[ordered]@{id="concat-cuda-hybrid-v1";version=2}', standalone)
        self.assertIn('$capabilities.render_contract.id -cne "concat-cuda-hybrid-v1"', standalone)

    def test_rejected_contract_keeps_source_and_does_not_block_following_job(self):
        source = self.root / "new.zip"
        pack(self.project, source)
        bad = self.root / "mismatch.zip"
        self.rewrite_control(source, bad, lambda c: c.update(
            render_contract={"id": RENDER_CONTRACT_ID, "version": 999}))
        worker = RenderWorker(self.root / "worker", token="fixture-token-" * 4,
                              renderer=lambda project, output: output.write_bytes(b"\0\0\0\x18ftypisom"))
        self.addCleanup(worker.pool.shutdown, wait=True)
        # Run real restore/state transitions synchronously, with only MP4 generation mocked.
        with patch.object(worker.pool, "submit", side_effect=lambda fn, *args: fn(*args)):
            data = bad.read_bytes()
            worker.accept("mismatch", io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest(),
                          render_mode="cuda-hybrid-v1")
            status = worker.status("mismatch")
            self.assertEqual(status["state"], "failed")
            self.assertIn("contract does not match", status["error"])
            self.assertNotIn("output_sha256", status)
            self.assertEqual((worker.inputs / "mismatch/project.zip").read_bytes(), data)
            data = source.read_bytes()
            worker.accept("following", io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest(),
                          render_mode="cuda-hybrid-v1")
            self.assertEqual(worker.status("following")["state"], "rendered")

    def test_legacy_zip_can_retry_after_worker_restart_without_repacking(self):
        source = self.root / "new.zip"
        pack(self.project, source)
        legacy = self.root / "legacy.zip"
        self.rewrite_control(source, legacy, lambda c: c.pop("render_contract"))
        data = legacy.read_bytes()
        def unavailable(project, output):
            raise RuntimeError("Unsupported feature fixture; source retained")
        worker = RenderWorker(self.root / "legacy-worker", token="fixture-token-" * 4,
                              renderer=unavailable)
        with patch.object(worker.pool, "submit", side_effect=lambda fn, *args: fn(*args)):
            worker.accept("legacy", io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest(),
                          render_mode="cuda-hybrid-v1")
        worker.pool.shutdown(wait=True)
        self.assertEqual(worker.status("legacy")["state"], "failed")
        fresh = RenderWorker(self.root / "legacy-worker", token="fixture-token-" * 4,
                             renderer=lambda project, output: output.write_bytes(b"\0\0\0\x18ftypisom"))
        self.addCleanup(fresh.pool.shutdown, wait=True)
        with patch.object(fresh.pool, "submit", side_effect=lambda fn, *args: fn(*args)):
            self.assertEqual(fresh.retry("legacy")["state"], "rendered")
        self.assertEqual((fresh.inputs / "legacy/project.zip").read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
