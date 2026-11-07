import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from cloud_render.concat.project_bundle import TransferError, pack, restore


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "desktop"
        self.project.mkdir()
        self.media = self.project / "sound.wav"
        self.media.write_bytes(b"audio data")
        self.manifest = self.project / "concat.json"
        self.manifest.write_text(json.dumps({"media": [
            {"id": "m1", "path": str(self.media)}, {"id": "m2", "path": ""}]}))
        self.bundle = self.root / "transfer.zip"

    def test_round_trip_and_source_preserved(self):
        old = self.manifest.read_bytes()
        self.assertEqual(pack(self.project, self.bundle)["files"], 1)
        self.assertEqual(restore(self.bundle, self.root / "worker")["files"], 1)
        output = self.root / "worker"
        new_doc = json.loads((output / "concat.json").read_text())
        self.assertEqual(Path(new_doc["media"][0]["path"]).read_bytes(), b"audio data")
        self.assertEqual(new_doc["media"][1]["path"], "")
        self.assertEqual((output / "concat.json.original").read_bytes(), old)
        self.assertEqual(self.manifest.read_bytes(), old)

    def test_no_overwrite(self):
        pack(self.project, self.bundle)
        with self.assertRaises(TransferError):
            pack(self.project, self.bundle)
        (self.root / "worker").mkdir()
        with self.assertRaises(TransferError):
            restore(self.bundle, self.root / "worker")

    def test_reject_path_traversal_archive(self):
        pack(self.project, self.bundle)
        with zipfile.ZipFile(self.bundle, "a") as file:
            file.writestr("../escape", "bad")
        with self.assertRaisesRegex(TransferError, "unexpected"):
            restore(self.bundle, self.root / "worker")
        self.assertFalse((self.root / "escape").exists())

    def test_hash_check(self):
        pack(self.project, self.bundle)
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(self.bundle) as original, zipfile.ZipFile(tampered, "w") as output:
            for member in original.namelist():
                content = original.read(member)
                output.writestr(member, b"invalid" if member.startswith("media/") else content)
        with self.assertRaisesRegex(TransferError, "checksum"):
            restore(tampered, self.root / "worker")
        self.assertFalse((self.root / "worker").exists())

    def test_missing_media_fails_before_zip(self):
        self.media.unlink()
        with self.assertRaises(FileNotFoundError):
            pack(self.project, self.bundle)
        self.assertFalse(self.bundle.exists())


if __name__ == "__main__":
    unittest.main()
