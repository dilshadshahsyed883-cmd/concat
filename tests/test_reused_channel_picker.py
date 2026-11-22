"""Issue #24: Concat Uploader group-to-channel selector regression tests."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PICKER = ROOT / "reused/uploader/part01/static/copyright_video_picker.js"
NAV = ROOT / "reused/uploader/part01/static/local_upload_manager.js"
BASE = ROOT / "reused/uploader/part01/templates/base.html"
APP = ROOT / "reused/uploader/part01/templates/app.html"


class ChannelPickerRegression(unittest.TestCase):
    def test_dynamic_dom_and_channel_filtering(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for browser-script regression")
        result = subprocess.run(
            [node, str(ROOT / "tests/js/channel_picker.test.js")],
            text=True, capture_output=True, timeout=20, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("PASS: Issue #24", result.stdout)

    def test_shell_replacement_notifies_picker(self):
        navigation = NAV.read_text(encoding="utf-8")
        script = PICKER.read_text(encoding="utf-8")
        self.assertIn("document.dispatchEvent(new Event('lum:shell-replaced'))", navigation)
        self.assertLess(
            navigation.index("currentShell.replaceWith(nextShell)"),
            navigation.index("document.dispatchEvent(new Event('lum:shell-replaced'))"),
        )
        self.assertIn('document.addEventListener("lum:shell-replaced", install)', script)

    def test_asset_versions_invalidate_browser_cache(self):
        self.assertIn("copyright_video_picker.js?v=20261119-group-restore-v1",
                      BASE.read_text(encoding="utf-8"))
        self.assertIn("local_upload_manager.js?v=20261119-shell-rebind-v1",
                      APP.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
