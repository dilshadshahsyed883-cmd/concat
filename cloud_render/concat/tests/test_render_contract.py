"""EXE ↔ Salad worker render contract; legacy jobs remain retryable."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

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
            ("wrong-version", lambda c: c.update(render_contract={"id": RENDER_CONTRACT_ID, "version": 2})),
            ("boolean-version", lambda c: c.update(render_contract={"id": RENDER_CONTRACT_ID, "version": True})),
            ("invalid-type", lambda c: c.update(render_contract="invalid")),
        ):
            with self.subTest(name=name):
                altered = self.root / (name + ".zip")
                self.rewrite_control(source, altered, change)
                with self.assertRaisesRegex(TransferError, "contract does not match"):
                    restore(altered, self.root / (name + "-out"))

    def test_old_v1_zip_without_contract_is_still_recoverable(self):
        source = self.root / "project.zip"
        pack(self.project, source)
        old = self.root / "old.zip"
        self.rewrite_control(source, old, lambda c: c.pop("render_contract"))
        restore(old, self.root / "legacy-out")
        self.assertTrue((self.root / "legacy-out/concat.json").is_file())

    def test_embedded_and_standalone_windows_helpers_have_identical_contract(self):
        root = Path(__file__).resolve().parents[3]
        standalone = (root / "concat_gpu/concat_gpu_client.ps1").read_text(encoding="utf-8")
        embedded = (root / "upstream/src/crates/concat/src/concat_gpu_client.ps1").read_text(encoding="utf-8")
        self.assertEqual(standalone, embedded)
        self.assertIn('render_contract=[ordered]@{id="concat-cuda-hybrid-v1";version=1}', standalone)
        self.assertIn('$capabilities.render_contract.id -ne "concat-cuda-hybrid-v1"', standalone)


if __name__ == "__main__":
    unittest.main()
