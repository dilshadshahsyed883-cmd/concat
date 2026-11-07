"""CUDA HDR10 output from absolute linear BT.2020 frames."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import cache
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile

from .contract import FrameRate, cuda, rgba

_P010_CUDA = r"""
__device__ float unit(float value) {
    return fminf(fmaxf(value, 0.0f), 1.0f);
}

// ST 2084 inverse EOTF, normalized to 10,000 cd/m² (BT.2100).
__device__ float pq(float linear) {
    const float p = powf(unit(linear), 2610.0f / 16384.0f);
    return powf((3424.0f / 4096.0f + (2413.0f / 128.0f) * p) /
                (1.0f + (2392.0f / 128.0f) * p), 2523.0f / 32.0f);
}

__device__ float lerp(float a, float b, float t) {
    return a + (b - a) * t;
}

__device__ float premultiply(const float* rgba, int channel) {
    return unit(rgba[channel]) * unit(rgba[3]);
}

__device__ float scale_channel(const float* rgba, int width, int height,
                               float x, float y, int channel) {
    x = fminf(fmaxf(x, 0.0f), width - 1.0f);
    y = fminf(fmaxf(y, 0.0f), height - 1.0f);
    const int x0 = (int)x, y0 = (int)y;
    const int x1 = x0 < width - 1 ? x0 + 1 : x0;
    const int y1 = y0 < height - 1 ? y0 + 1 : y0;
    const float a = lerp(premultiply(rgba + (y0 * width + x0) * 4, channel),
                         premultiply(rgba + (y0 * width + x1) * 4, channel), x - x0);
    const float b = lerp(premultiply(rgba + (y1 * width + x0) * 4, channel),
                         premultiply(rgba + (y1 * width + x1) * 4, channel), x - x0);
    return lerp(a, b, y - y0);
}

__device__ unsigned short p010(float code, float low, float high) {
    const unsigned int value = __float2uint_rn(fminf(fmaxf(code, low), high));
    return (unsigned short)(value << 6);
}

extern "C" __global__ void hdr10_p010(const float* src, int sw, int sh,
                                     unsigned short* out, int w, int h) {
    const int tile = blockIdx.x * blockDim.x + threadIdx.x;
    if (tile >= (w / 2) * (h / 2)) return;

    const int x0 = (tile % (w / 2)) * 2;
    const int y0 = (tile / (w / 2)) * 2;
    float cb = 0.0f, cr = 0.0f;
    for (int dy = 0; dy < 2; ++dy) {
        for (int dx = 0; dx < 2; ++dx) {
            const int x = x0 + dx, y = y0 + dy;
            const float sx = (x + 0.5f) * sw / w - 0.5f;
            const float sy = (y + 0.5f) * sh / h - 0.5f;
            const float r = pq(scale_channel(src, sw, sh, sx, sy, 0));
            const float g = pq(scale_channel(src, sw, sh, sx, sy, 1));
            const float b = pq(scale_channel(src, sw, sh, sx, sy, 2));
            const float luma = 0.2627f * r + 0.6780f * g + 0.0593f * b;
            out[y * w + x] = p010(64.0f + 876.0f * luma, 64.0f, 940.0f);
            cb += (b - luma) / 1.8814f;
            cr += (r - luma) / 1.4746f;
        }
    }
    const int uv = w * h + (y0 / 2) * w + x0;
    out[uv] = p010(512.0f + 224.0f * cb, 64.0f, 960.0f);
    out[uv + 1] = p010(512.0f + 224.0f * cr, 64.0f, 960.0f);
}
"""

_START_CODE = re.compile(b"\x00\x00(?:\x00)?\x01")
_HDR10_VUI = (
    "hevc_metadata=video_full_range_flag=0:colour_primaries=9:"
    "transfer_characteristics=16:matrix_coefficients=9:chroma_sample_loc_type=1"
)


def _dimensions(width, height):
    if any(type(value) is not int or value < 2 or value > 8192 or value % 2
           for value in (width, height)):
        raise ValueError("P010 needs even output dimensions between 2 and 8192")


@cache
def _kernel():
    return cuda().RawKernel(_P010_CUDA, "hdr10_p010")


def hdr10_from_linear_bt2020(pixels, *, width=3840, height=2160):
    """Return a GPU P010 surface, with packed Y then interleaved UV rows.

    Input is straight-alpha float32 RGBA in linear BT.2020, with RGB=1
    corresponding to 10,000 cd/m². The kernel scales in linear light and
    composites transparency over black before applying PQ. It averages
    chroma at the center of each 2×2 output block. Callers must explicitly
    convert their source into this color space before invoking this path.
    """
    _dimensions(width, height)
    source = rgba(pixels)
    src_height, src_width = source.shape[:2]
    if max(src_width, src_height) > 8192:
        raise ValueError("HDR input dimensions must be at most 8192 pixels")
    cp = cuda()
    source = cp.ascontiguousarray(source)
    result = cp.empty((height * 3 // 2, width), dtype=cp.uint16)
    tiles = (width // 2) * (height // 2)
    _kernel()(((tiles + 127) // 128,), (128,), (
        source, cp.int32(src_width), cp.int32(src_height),
        result, cp.int32(width), cp.int32(height),
    ))
    return result


def _xy(point):
    if (not isinstance(point, tuple) or len(point) != 2
            or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in point)
            or sum(point) > 1):
        raise ValueError("Mastering chromaticity requires CIE xy coordinates")
    return tuple(round(value * 50000) for value in point)


@dataclass(frozen=True)
class Hdr10Metadata:
    """Mastering display measurements and authored HDR content light levels."""

    red: tuple[float, float]
    green: tuple[float, float]
    blue: tuple[float, float]
    white: tuple[float, float]
    minimum_nits: float
    maximum_nits: float
    max_cll: int
    max_fall: int

    def __post_init__(self):
        for point in (self.red, self.green, self.blue, self.white):
            _xy(point)
        luminance = (self.minimum_nits, self.maximum_nits)
        if (any(type(value) not in (int, float) for value in luminance)
                or not 0 <= self.minimum_nits < self.maximum_nits <= 10000
                or round(self.minimum_nits * 10000) >= round(self.maximum_nits * 10000)):
            raise ValueError("Invalid mastering luminance")
        if (type(self.max_cll) is not int or type(self.max_fall) is not int
                or not 1 <= self.max_fall <= self.max_cll <= 10000):
            raise ValueError("Expected measured MaxFALL and MaxCLL in cd/m²")

    def prefix_sei(self) -> bytes:
        """Build an HEVC Annex B prefix SEI carrying ST 2086 and CLLI."""
        # HEVC specifies display primaries in G, B, R order.
        primaries = tuple(value for point in (self.green, self.blue, self.red, self.white)
                          for value in _xy(point))
        mastering = struct.pack(">8H2I", *primaries,
                                round(self.maximum_nits * 10000),
                                round(self.minimum_nits * 10000))
        lights = struct.pack(">2H", self.max_cll, self.max_fall)
        rbsp = bytes((137, len(mastering))) + mastering + bytes((144, len(lights))) + lights + b"\x80"
        return b"\x00\x00\x00\x01\x4e\x01" + _escape_rbsp(rbsp)


def _escape_rbsp(rbsp: bytes) -> bytes:
    escaped = bytearray()
    zeros = 0
    for value in rbsp:
        if zeros == 2 and value <= 3:
            escaped.append(3)
            zeros = 0
        escaped.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return bytes(escaped)


def tag_hdr10_access_unit(data: bytes, metadata: Hdr10Metadata) -> bytes:
    """Add mastering and light-level metadata ahead of each HEVC IRAP."""
    start = _START_CODE.search(data)
    if start is None or start.end() + 2 > len(data) or any(data[:start.start()]):
        raise ValueError("Expected an Annex B HEVC access unit")
    for start in _START_CODE.finditer(data):
        if start.end() + 2 > len(data):
            raise ValueError("Truncated HEVC NAL header")
        nal_type = (data[start.end()] >> 1) & 0x3f
        if 16 <= nal_type <= 23:
            return data[:start.start()] + metadata.prefix_sei() + data[start.start():]
    return data


def _run(command):
    result = subprocess.run(command, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed: {result.stderr[-2000:]}")
    return result.stdout


def _mux_hdr10(hevc, output, rate: FrameRate, *, width, height, frame_count,
              ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """Copy a tagged HEVC elementary stream into MP4 and check its video format."""
    _dimensions(width, height)
    if not isinstance(rate, FrameRate) or type(frame_count) is not int or frame_count < 1:
        raise ValueError("Expected a positive frame count and a FrameRate")
    output = Path(output)
    if output.suffix.lower() != ".mp4" or output.exists() or output.is_symlink():
        raise ValueError("HDR output needs a new .mp4 destination")
    frequency = Fraction(rate.numerator, rate.denominator)
    ticks = frequency.denominator
    # Raw Annex B has no packet timestamps. NVENC emits a low-delay stream in
    # presentation order, so assign exact CFR timestamps before muxing.
    timestamps = (f"setts=pts=N*{ticks}:dts=N*{ticks}:duration={ticks}:"
                  f"time_base=1/{frequency.numerator}")
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".concat-hdr10-") as directory:
        temporary = Path(directory) / "render.mp4"
        _run([ffmpeg, "-nostdin", "-v", "error", "-f", "hevc", "-framerate", str(frequency),
              "-i", str(hevc), "-map", "0:v:0", "-c:v", "copy",
              "-bsf:v", f"{_HDR10_VUI},{timestamps}",
              "-video_track_timescale", str(frequency.numerator), "-color_range", "tv", "-color_primaries", "bt2020",
              "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-chroma_sample_location",
              "center", "-movflags", "+faststart", str(temporary)])
        report = json.loads(_run([ffprobe, "-v", "error", "-select_streams", "v:0",
                                  "-show_streams", "-of", "json", str(temporary)]))
        streams = report["streams"]
        expected = {
            "codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le",
            "width": width, "height": height, "nb_frames": str(frame_count),
            "color_range": "tv", "color_space": "bt2020nc",
            "color_transfer": "smpte2084", "color_primaries": "bt2020",
            "chroma_location": "center", "has_b_frames": 0,
        }
        if len(streams) != 1 or any(streams[0].get(key) != value for key, value in expected.items()):
            raise RuntimeError("HDR MP4 failed HEVC Main 10 / BT.2020 PQ output verification")
        stream = streams[0]
        time_base = Fraction(stream["time_base"])
        actual = int(stream["duration_ts"]) * time_base
        if actual != rate.seconds(frame_count):
            raise RuntimeError("HDR MP4 duration differs from the requested frame rate")
        os.link(temporary, output)
    return output


def render_hdr10(frames, output, rate: FrameRate, metadata: Hdr10Metadata, *,
                 width=3840, height=2160, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """Encode HDR surfaces with the shared NVENC adapter and retain HDR metadata."""
    _dimensions(width, height)
    if not isinstance(rate, FrameRate) or not isinstance(metadata, Hdr10Metadata):
        raise ValueError("HDR rendering requires frame rate and mastering metadata")

    output = Path(output)
    if output.suffix.lower() != ".mp4" or output.exists() or output.is_symlink():
        raise ValueError("HDR output needs a new .mp4 destination")
    from .codec import Nvenc

    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".concat-encode-") as directory:
        stream = Path(directory) / "hdr10.hevc"
        encoder = Nvenc(width, height, rate, codec="hevc", pixel_format="P010")
        frame_count = 0
        has_hdr_sei = False
        with stream.open("wb") as handle:
            def write_packets(packets):
                nonlocal has_hdr_sei
                for packet in packets:
                    tagged = tag_hdr10_access_unit(packet.data, metadata)
                    has_hdr_sei |= tagged != packet.data
                    handle.write(tagged)

            for frame_index, pixels in enumerate(frames):
                surface = hdr10_from_linear_bt2020(pixels, width=width, height=height)
                write_packets(encoder.encode(surface, frame_index))
                frame_count += 1
            write_packets(encoder.finish())
        if not has_hdr_sei:
            raise RuntimeError("NVENC returned no HEVC random-access frame for HDR metadata")
        return _mux_hdr10(stream, output, rate, width=width, height=height,
                         frame_count=frame_count, ffmpeg=ffmpeg, ffprobe=ffprobe)
