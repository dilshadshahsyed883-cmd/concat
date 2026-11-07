from fractions import Fraction
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from cloud_render.concat.hybrid.contract import FrameRate, cuda
from cloud_render.concat.hybrid.hdr import (
    Hdr10Metadata, _mux_hdr10, hdr10_from_linear_bt2020, render_hdr10,
    tag_hdr10_access_unit,
)


def mastering(**updates):
    return Hdr10Metadata(**({
        "red": (.680, .320), "green": (.265, .690), "blue": (.150, .060),
        "white": (.3127, .3290), "minimum_nits": .0001, "maximum_nits": 1000,
        "max_cll": 950, "max_fall": 400,
    } | updates))


def frame_report(output):
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals",
               "%+#1", "-show_frames", "-show_streams", "-of", "json", str(output)]
    return json.loads(subprocess.check_output(command, text=True, timeout=30))


def require_hdr_metadata(test, frame):
    side_data = {item["side_data_type"]: item for item in frame["side_data_list"]}
    display = side_data["Mastering display metadata"]
    test.assertEqual(display["red_x"], "34000/50000")
    test.assertEqual(display["min_luminance"], "1/10000")
    test.assertEqual(display["max_luminance"], "10000000/10000")
    lights = side_data["Content light level metadata"]
    test.assertEqual((lights["max_content"], lights["max_average"]), (950, 400))


class HdrMetadataTests(unittest.TestCase):
    def test_rejects_invalid_geometry_before_loading_cuda(self):
        for width, height in [(1919, 1080), (3840, 2161), (True, 2160), (3840, 0)]:
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                hdr10_from_linear_bt2020(None, width=width, height=height)

    def test_validates_authored_luminance_and_chromaticities(self):
        for fields in [{"maximum_nits": float("nan")}, {"minimum_nits": 1100},
                       {"max_fall": 951}, {"white": (.9, .9)}, {"max_cll": True}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                mastering(**fields)

    def test_hdr_payload_ordering_and_units(self):
        sei = mastering().prefix_sei()
        self.assertEqual(sei[:6], b"\x00\x00\x00\x01\x4e\x01")
        # Reverse the HEVC emulation-prevention bytes before checking SEI values.
        payload = sei[6:].replace(b"\x00\x00\x03", b"\x00\x00")
        self.assertEqual(payload[:2], bytes((137, 24)))
        self.assertEqual(struct.unpack(">8H2I", payload[2:26]),
                         (13250, 34500, 7500, 3000, 34000, 16000, 15635, 16450,
                          10000000, 1))
        self.assertEqual(payload[26:], bytes((144, 4)) + struct.pack(">2H", 950, 400) + b"\x80")

    def test_tags_irap_after_parameter_sets_and_keeps_inter_frames(self):
        vps = b"\x00\x00\x00\x01\x40\x01header"
        idr = b"\x00\x00\x01\x26\x01intra"
        trail = b"\x00\x00\x00\x01\x02\x01inter"
        metadata = mastering()
        self.assertEqual(tag_hdr10_access_unit(vps + idr, metadata),
                         vps + metadata.prefix_sei() + idr)
        self.assertIs(tag_hdr10_access_unit(trail, metadata), trail)
        for truncated in (b"invalid", b"\x00\x00\x01\x26"):
            with self.assertRaises(ValueError):
                tag_hdr10_access_unit(truncated, metadata)


class HdrMuxTests(unittest.TestCase):
    def test_stream_copy_preserves_hdr10_metadata_and_rejects_eight_bit(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg and FFprobe required")
        encoders = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"], text=True)
        if "libx265" not in encoders:
            self.skipTest("libx265 needed to generate the independent bitstream fixture")
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for rate, frame_count, pixel_format, profile in (
                    (FrameRate(30000, 1001), 3, "yuv420p10le", "main10"),
                    (FrameRate(29), 30, "yuv420p10le", "main10"),
                    (FrameRate(30), 3, "yuv420p", "main")):
                source = directory / f"{profile}-{rate.numerator}.hevc"
                output = source.with_suffix(".mp4")
                fps = f"{rate.numerator}/{rate.denominator}"
                # The CPU encoder creates only a test fixture for the stream-copy boundary.
                subprocess.run([
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    f"color=white:s=64x64:r={fps}", "-frames:v", str(frame_count),
                    "-pix_fmt", pixel_format,
                    "-c:v", "libx265", "-profile:v", profile, "-x265-params",
                    "log-level=error:pools=none:frame-threads=1:keyint=30:bframes=0",
                    "-f", "hevc", str(source),
                ], check=True, timeout=30)
                source.write_bytes(tag_hdr10_access_unit(source.read_bytes(), mastering()))
                if profile == "main":
                    with self.assertRaisesRegex(RuntimeError, "Main 10"):
                        _mux_hdr10(source, output, rate, width=64, height=64, frame_count=frame_count)
                    self.assertFalse(output.exists())
                else:
                    _mux_hdr10(source, output, rate, width=64, height=64, frame_count=frame_count)
                    report = frame_report(output)
                    stream = report["streams"][0]
                    self.assertEqual(stream["r_frame_rate"], fps)
                    self.assertEqual(Fraction(stream["time_base"]) * stream["duration_ts"],
                                     rate.seconds(frame_count))
                    require_hdr_metadata(self, report["frames"][0])
                    with self.assertRaisesRegex(ValueError, "new .mp4"):
                        _mux_hdr10(source, output, rate, width=64, height=64, frame_count=frame_count)


class CudaHdrTests(unittest.TestCase):
    def setUp(self):
        try:
            self.cp = cuda()
        except RuntimeError as error:
            if os.environ.get("CONCAT_REQUIRE_CUDA") == "1":
                raise
            self.skipTest(str(error))

    def test_4k_pq_and_limited_p010_samples(self):
        cp = self.cp
        source = cp.array([[[1, 0, 0, 1], [.1, .1, .1, 1]],
                           [[1, 1, 1, 0], [1, 1, 1, 1]]], dtype=cp.float32)
        p010 = hdr10_from_linear_bt2020(source)
        self.assertEqual(p010.shape, (3240, 3840))
        self.assertEqual(p010.dtype, cp.uint16)
        self.assertEqual(p010.device.id, source.device.id)
        self.assertEqual(int(p010[0, 0]), 294 << 6)
        self.assertEqual(int(p010[0, -1]), 723 << 6)
        self.assertEqual(int(p010[2159, 0]), 64 << 6)
        self.assertEqual(int(p010[2159, -1]), 940 << 6)
        self.assertEqual([int(value) >> 6 for value in p010[2160, :2]], [387, 960])
        self.assertEqual([int(value) >> 6 for value in p010[-1, -2:]], [512, 512])
        self.assertFalse(bool(cp.any(p010 & 63)))

    def test_render_4k_hardware_hevc_mp4(self):
        if importlib.util.find_spec("cloud_render.concat.hybrid.codec") is None:
            self.skipTest("End-to-end NVENC test requires the CUDA core branch")
        cp = self.cp
        source = cp.array([[[.01, .01, .01, 1], [.09, .09, .09, 1]],
                           [[.01, .01, .01, 1], [.09, .09, .09, 1]]], dtype=cp.float32)
        with tempfile.TemporaryDirectory() as directory:
            output = render_hdr10((source for _ in range(4)), Path(directory) / "hdr.mp4",
                                  FrameRate(30), mastering())
            report = frame_report(output)
            self.assertEqual(report["streams"][0]["width"], 3840)
            self.assertEqual(report["streams"][0]["height"], 2160)
            self.assertEqual(report["streams"][0]["nb_frames"], "4")
            require_hdr_metadata(self, report["frames"][0])


if __name__ == "__main__":
    unittest.main()
