"""Issue #68: matching desktop ZIP and Salad JPEG/PNG still-image support."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cloud_render.concat.gpu.worker import RenderWorker
from cloud_render.concat.hybrid.project import load_project, validate_gpu_manifest
from cloud_render.concat.project_bundle import pack, restore, sha256

try:
    from PIL import Image
except ImportError:
    Image = None


def manifest():
    return {
        "version": 1,
        "activeTimelineId": "timeline",
        "media": [
            {"id": "jpeg", "kind": "image", "path": "background.jpg"},
            {"id": "png", "kind": "image", "path": "cutout.png"},
            {"id": "moving", "kind": "video", "path": "clip.mp4"},
        ],
        "timelines": [{
            "id": "timeline",
            "video": {"width": 1920, "height": 1080, "rateNum": 30, "rateDen": 1},
            "tracks": [{"id": "bottom", "visible": True},
                       {"id": "overlay", "visible": True}],
            "clips": [
                {"id": "c100", "kind": "image", "mediaId": "jpeg",
                 "trackId": "bottom", "start": 0, "duration": 2},
                {"id": "c200", "kind": "image", "mediaId": "png",
                 "trackId": "overlay", "start": 0, "duration": 1},
                {"id": "c300", "kind": "video", "mediaId": "moving",
                 "trackId": "overlay", "start": 1, "duration": 1},
            ],
        }],
    }


@unittest.skipUnless(Image is not None, "Pillow is required by the GPU worker")
class StillImageDecodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_rgb_jpeg_and_png_keep_colors(self):
        from cloud_render.concat.hybrid.render import decode_still_rgba
        for name, fmt in (("rgb.jpg", "JPEG"), ("rgb.png", "PNG"),
                          ("uppercase.PNG", "PNG"), ("long.jpeg", "JPEG")):
            with self.subTest(name=name):
                path = self.root / name
                Image.new("RGB", (2, 2), (40, 110, 200)).save(path, format=fmt)
                before = sha256(path)
                decoded = decode_still_rgba(path)
                self.assertEqual(decoded.shape, (2, 2, 4))
                self.assertEqual(decoded.dtype.name, "uint8")
                self.assertEqual(decoded[0, 0, 3], 255)
                self.assertLess(abs(int(decoded[0, 0, 0]) - 40), 4)
                self.assertLess(abs(int(decoded[0, 0, 1]) - 110), 4)
                self.assertEqual(sha256(path), before)

    def test_rgba_transparency_is_straight_not_matted(self):
        from cloud_render.concat.hybrid.render import decode_still_rgba
        path = self.root / "transparent.png"
        image = Image.new("RGBA", (3, 1))
        image.putdata([(255, 0, 0, 0), (0, 0, 255, 128), (10, 20, 30, 255)])
        image.save(path)
        pixels = decode_still_rgba(path)
        self.assertEqual(pixels.tolist(), [[[255, 0, 0, 0],
                                            [0, 0, 255, 128],
                                            [10, 20, 30, 255]]])

    def test_grayscale_palette_and_palette_transparency(self):
        from cloud_render.concat.hybrid.render import decode_still_rgba
        gray = self.root / "gray.png"
        Image.new("L", (1, 1), 87).save(gray)
        self.assertEqual(decode_still_rgba(gray)[0, 0].tolist(), [87, 87, 87, 255])
        palette = Image.new("P", (2, 1))
        palette.putpalette([255, 0, 0, 0, 0, 255] + [0, 0, 0] * 254)
        palette.putdata([0, 1])
        palette.info["transparency"] = 0
        indexed = self.root / "indexed.png"
        palette.save(indexed)
        self.assertEqual(decode_still_rgba(indexed).tolist(),
                         [[[255, 0, 0, 0], [0, 0, 255, 255]]])

    def test_corrupt_and_spoofed_files_fail_with_filename(self):
        from cloud_render.concat.hybrid.render import decode_still_rgba
        bad = self.root / "bad.png"
        bad.write_bytes(b"not a PNG")
        with self.assertRaisesRegex(ValueError, r"bad.png.*invalid PNG"):
            decode_still_rgba(bad)
        disguised = self.root / "actually.png"
        Image.new("RGB", (1, 1)).save(disguised, format="JPEG")
        with self.assertRaisesRegex(ValueError, r"actually.png.*expected PNG"):
            decode_still_rgba(disguised)
        gif = self.root / "animated.gif"
        Image.new("RGB", (1, 1)).save(gif)
        with self.assertRaisesRegex(ValueError, r"animated.gif.*JPEG or PNG"):
            decode_still_rgba(gif)

    def test_gpu_resizing_and_blending_preserve_transparent_edges(self):
        from cloud_render.concat.hybrid.contract import cuda
        from cloud_render.concat.hybrid.compositor import Layer, composite
        from cloud_render.concat.hybrid import render
        try:
            cp = cuda()
        except RuntimeError as exc:
            if os.environ.get("CONCAT_REQUIRE_CUDA") == "1":
                raise
            self.skipTest(str(exc))
        path = self.root / "cutout.png"
        image = Image.new("RGBA", (2, 2))
        # Fully transparent red must not bleed into adjacent blue while resizing.
        image.putdata([(255, 0, 0, 0), (0, 0, 255, 128)] * 2)
        image.save(path)
        with patch.object(render, "WIDTH", 4), patch.object(render, "HEIGHT", 2):
            source = render.Sources(self.root)
            pixels = source.image(path)
            self.assertIs(source.image(path), pixels)
            output = composite([Layer(pixels)], width=4, height=2,
                               background=(1, 1, 1, 1))
        sample = cp.asnumpy(output)
        # On a white matte: the transparent red channel cannot color the edge.
        self.assertLess(float(abs(sample[..., 0] - sample[..., 1]).max()), 0.02)
        self.assertGreater(float(sample[..., 2].min()), 0.98)


@unittest.skipUnless(Image is not None, "Pillow is required by the GPU worker")
class ZipParityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for real mixed-media fixture")
    def test_legacy_zip_restores_and_retries_rgb_rgba_mixed_video_without_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            Image.new("RGB", (2, 2), (60, 70, 80)).save(source / "background.jpg")
            Image.new("RGBA", (2, 2), (10, 20, 30, 125)).save(source / "cutout.png")
            subprocess.run([
                "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=green:s=16x16:r=30:d=1",
                "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(source / "clip.mp4"),
            ], capture_output=True, check=True, timeout=30)
            document = manifest()
            (source / "concat.json").write_text(json.dumps(document), encoding="utf-8")
            originals = {p.name: sha256(p) for p in source.iterdir()}
            validate_gpu_manifest(document)
            archive = root / "failed-job.zip"
            packed = pack(source, archive)
            self.assertEqual(packed["files"], 3)
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                for ext in (".jpg", ".png", ".mp4"):
                    self.assertEqual(sum(name.endswith(ext) for name in names), 1)

            # Re-restoring the same retained ZIP emulates retry after a worker
            # image update; it does not require touching or rebuilding originals.
            for attempt in (1, 2):
                restored = root / f"retry-{attempt}"
                restore(archive, restored)
                restored_doc = json.loads((restored / "concat.json").read_text())
                validate_gpu_manifest(restored_doc)
                plan = load_project(restored, lambda _: None)
                self.assertEqual(plan.frames, 60)
                self.assertEqual([clip.id for lane in plan.lanes for clip in lane],
                                 ["c100", "c200", "c300"])
                expected_by_id = {
                    "jpeg": originals["background.jpg"],
                    "png": originals["cutout.png"],
                    "moving": originals["clip.mp4"],
                }
                for media in restored_doc["media"]:
                    self.assertEqual(sha256(Path(media["path"])),
                                     expected_by_id[media["id"]])
            self.assertEqual({p.name: sha256(p) for p in source.iterdir()}, originals)


    def test_failed_worker_job_retries_unchanged_legacy_png_zip_with_same_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            Image.new("RGBA", (2, 2), (0, 0, 255, 128)).save(source / "cutout.png")
            Image.new("RGB", (2, 2), (0, 255, 0)).save(source / "background.jpg")
            (source / "clip.mp4").write_bytes(b"retained source video")
            (source / "concat.json").write_text(json.dumps(manifest()), encoding="utf-8")
            archive = root / "legacy.zip"
            pack(source, archive)
            raw = archive.read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
            calls = []

            def simulate_renderer(project, output):
                calls.append(project)
                doc = json.loads((project / "concat.json").read_text())
                validate_gpu_manifest(doc)
                png_path = next(Path(m["path"]) for m in doc["media"]
                                if m["id"] == "png")
                self.assertEqual(png_path.read_bytes(), (source / "cutout.png").read_bytes())
                if len(calls) == 1:
                    raise RuntimeError("old renderer rejected PNG")
                output.write_bytes(b"\\x00\\x00\\x00\\x18ftypisom" + b"\\x00" * 20)

            worker = RenderWorker(root / "worker", token="png-retry-test-token" * 3,
                                  renderer=simulate_renderer)
            def until_state(expected):
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if worker.status("oldzip68")["state"] == expected:
                        return
                    time.sleep(.02)
                self.fail(f"Worker never reached {expected}: {worker.status('oldzip68')}")
            try:
                worker.accept("oldzip68", io.BytesIO(raw), len(raw), checksum,
                              render_mode="cuda-hybrid-v1")
                until_state("failed")
                saved_zip = worker.inputs / "oldzip68" / "project.zip"
                self.assertEqual(hashlib.sha256(saved_zip.read_bytes()).hexdigest(), checksum)
                worker.retry("oldzip68")
                until_state("rendered")
                self.assertEqual(len(calls), 2)
                self.assertEqual(hashlib.sha256(saved_zip.read_bytes()).hexdigest(), checksum)
            finally:
                worker.pool.shutdown(wait=True)

    def test_manifest_rejects_unsupported_extension_with_clip_id(self):
        document = manifest()
        for name in ("cutout.png", "CUTOUT.PNG", "cutout.jpg", "cutout.jpeg"):
            document["media"][1]["path"] = name
            validate_gpu_manifest(document)
        document["media"][1]["path"] = "cutout.webp"
        with self.assertRaisesRegex(ValueError, r"c200.*JPEG or PNG"):
            validate_gpu_manifest(document)


if __name__ == "__main__":
    unittest.main()
