import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cloud_render.concat.hybrid.codec import EncodedPacket, Nvdec, Nvenc, nv12_from_rgba
from cloud_render.concat.hybrid.contract import FrameRate, cuda


class CodecValidationTests(unittest.TestCase):
    def test_invalid_encoding_configuration_fails_before_gpu_initialization(self):
        invalid = [
            ((255, 144, FrameRate(30)), {}),
            ((256, 144, FrameRate(30000, 1001)), {}),
            ((256, 144, FrameRate(30)), {"codec": "av1"}),
            ((256, 144, FrameRate(30)), {"pixel_format": "RGB"}),
            ((256, 144, FrameRate(30)), {"pixel_format": "P010"}),
            ((256, 144, FrameRate(30)), {"bitrate": True}),
        ]
        for args, kwargs in invalid:
            with self.subTest(args=args, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Nvenc(*args, **kwargs)

    def test_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "local seekable"):
                Nvdec(Path(temp) / "missing.mp4")

    def test_hdr_metadata_and_bit_depth_are_rejected_by_sdr_decoder(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as source:
            for properties in ({"pix_fmt": "yuv420p10le"},
                               {"pix_fmt": "yuv420p", "color_transfer": "smpte2084"}):
                metadata = json.dumps({"streams": [properties]})
                with self.subTest(properties=properties):
                    with patch("cloud_render.concat.hybrid.codec.subprocess.run") as probe:
                        probe.return_value.stdout = metadata
                        with self.assertRaisesRegex(ValueError, "8-bit 4:2:0 SDR"):
                            Nvdec(source.name)


class CodecCudaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cp = cuda()
            if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
                raise RuntimeError("GPU codec test requires ffmpeg and ffprobe")
        except RuntimeError as error:
            if os.environ.get("CONCAT_REQUIRE_CUDA") == "1":
                raise
            raise unittest.SkipTest(str(error)) from error

    def test_device_frame_conversion_black_white_and_transparent(self):
        cp = self.cp
        pixels = cp.ones((4, 4, 4), dtype=cp.float32)
        pixels[:2, :2, :3] = 0
        pixels[2:, :2, 3] = 0
        nv12 = nv12_from_rgba(pixels)
        self.assertEqual(nv12.shape, (6, 4))
        self.assertEqual(nv12.dtype, cp.uint8)
        self.assertTrue(bool(cp.all(nv12[:4, :2] == 16)))
        self.assertTrue(bool(cp.all(nv12[:4, 2:] == 235)))
        self.assertTrue(bool(cp.all(nv12[4:] == 128)))

    def test_nvenc_and_nvdec_round_trip_with_visible_frame_change(self):
        cp = self.cp
        width, height, frame_count = 256, 144, 30
        encoder = Nvenc(width, height, FrameRate(30), bitrate=2_000_000)
        frame = cp.zeros((height, width, 4), dtype=cp.float32)
        frame[..., 3] = 1
        frame[..., 0] = 1
        surface = nv12_from_rgba(frame)
        with self.assertRaisesRegex(ValueError, "contiguous timeline"):
            encoder.encode(surface, 1)
        with self.assertRaisesRegex(TypeError, "GPU-resident"):
            encoder.encode(b"", 0)

        with tempfile.TemporaryDirectory() as temp:
            elementary, mp4 = Path(temp) / "video.h264", Path(temp) / "video.mp4"
            packets: list[EncodedPacket] = []
            with elementary.open("wb") as stream:
                for index in range(frame_count):
                    if index == 15:
                        frame[..., 0] = 0
                        frame[..., 1] = 1
                        surface = nv12_from_rgba(frame)
                    produced = encoder.encode(surface, index)
                    stream.writelines(packet.data for packet in produced)
                    packets.extend(produced)
                remaining = encoder.finish()
                stream.writelines(packet.data for packet in remaining)
                packets.extend(remaining)
            self.assertEqual(encoder.finish(), ())
            with self.assertRaisesRegex(RuntimeError, "already flushed"):
                encoder.encode(surface, frame_count)
            self.assertEqual([packet.frame_index for packet in packets], list(range(frame_count)))

            subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-f", "h264", "-framerate", "30",
                 "-i", str(elementary), "-c:v", "copy", str(mp4)],
                check=True, timeout=60,
            )
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(mp4)],
                capture_output=True, text=True, check=True, timeout=60,
            )
            info = json.loads(probe.stdout)["streams"][0]
            self.assertEqual((info["codec_name"], info["width"], info["height"]),
                             ("h264", width, height))
            self.assertEqual(int(info["nb_read_frames"]), frame_count)
            self.assertAlmostEqual(float(info["duration"]), 1.0, places=2)

            decoder = Nvdec(mp4)
            self.assertEqual(len(decoder), frame_count)
            red = decoder.decode(3)
            green = decoder.decode(21)
            self.assertLess(red.source_pts, green.source_pts)
            self.assertIsInstance(red.pixels, cp.ndarray)
            self.assertEqual(red.pixels.shape, (height, width, 4))
            self.assertGreater(float(red.pixels[64, 64, 0]), 0.75)
            self.assertLess(float(red.pixels[64, 64, 1]), 0.25)
            self.assertGreater(float(green.pixels[64, 64, 1]), 0.75)
            self.assertLess(float(green.pixels[64, 64, 0]), 0.25)


if __name__ == "__main__":
    unittest.main()
