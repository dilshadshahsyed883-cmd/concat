"""CUDA color, spatial, and .cube LUT filters for normalized RGBA frames."""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from functools import cache
from math import isfinite
from pathlib import Path
from typing import Iterable

from .contract import cuda, rgba


@dataclass(frozen=True)
class FilterSettings:
    """Brightness offset, exposure stops, and a symmetric box radius in pixels."""

    brightness: float = 0.0
    contrast: float = 1.0
    exposure: float = 0.0
    saturation: float = 1.0
    temperature: float = 0.0
    blur_radius: int = 0
    sharpen: float = 0.0

    def __post_init__(self):
        ranges = {
            "brightness": (-1, 1), "contrast": (0, 4), "exposure": (-8, 8),
            "saturation": (0, 4), "temperature": (-1, 1), "sharpen": (0, 4),
        }
        for name, (minimum, maximum) in ranges.items():
            value = getattr(self, name)
            if type(value) not in (float, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
        if type(self.blur_radius) is not int or not 0 <= self.blur_radius <= 16:
            raise ValueError("blur_radius must be an integer in [0, 16]")


@dataclass(frozen=True)
class CubeLut:
    size: int
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    entries: bytes

    @classmethod
    def load(cls, path: str | Path) -> CubeLut:
        with Path(path).open(encoding="utf-8-sig") as lines:
            return cls.parse(lines)

    @classmethod
    def parse(cls, lines: Iterable[str]) -> CubeLut:
        """Read a 3D .cube with red as the fastest-changing table axis."""
        size = None
        domains = {}
        values = array("f")
        for number, line in enumerate(lines, 1):
            fields = line.split("#", 1)[0].split()
            if not fields:
                continue
            key, *arguments = fields
            if key == "TITLE" and not values:
                continue
            try:
                if key == "LUT_3D_SIZE" and size is None and len(arguments) == 1:
                    size = int(arguments[0])
                    if not 2 <= size <= 65:
                        raise ValueError("LUT_3D_SIZE must be in [2, 65]")
                elif key in ("DOMAIN_MIN", "DOMAIN_MAX") and key not in domains and not values:
                    domains[key] = _triple(arguments)
                elif size is not None and len(values) < 3 * size ** 3:
                    values.extend(_triple(fields))
                else:
                    raise ValueError("Unexpected or unsupported LUT entry")
            except ValueError as error:
                raise ValueError(f".cube line {number}: {error}") from error
        if size is None or len(values) != 3 * size ** 3:
            raise ValueError(".cube needs exactly LUT_3D_SIZE cubed RGB rows")
        low = domains.get("DOMAIN_MIN", (0.0, 0.0, 0.0))
        high = domains.get("DOMAIN_MAX", (1.0, 1.0, 1.0))
        if any(maximum - minimum < 1e-6 for minimum, maximum in zip(low, high)):
            raise ValueError("Each DOMAIN_MAX must exceed DOMAIN_MIN by at least 1e-6")
        return cls(size, low, high, values.tobytes())

    def upload(self) -> CudaLut:
        """Upload the small static lookup table once per rendering device."""
        import numpy

        cp = cuda()
        table = cp.asarray(numpy.frombuffer(self.entries, dtype=numpy.float32))
        return CudaLut(self.size, self.domain_min, self.domain_max, table)


def _triple(fields):
    if len(fields) != 3:
        raise ValueError("Expected three RGB values")
    values = tuple(map(float, fields))
    if any(not isfinite(value) or abs(value) > 65504 for value in values):
        raise ValueError("RGB values must be finite and within ±65504")
    return values


@dataclass(frozen=True)
class CudaLut:
    size: int
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    table: object

    def __post_init__(self):
        cp = cuda()
        if (type(self.size) is not int or not 2 <= self.size <= 65
                or not isinstance(self.table, cp.ndarray)
                or self.table.shape != (3 * self.size ** 3,)
                or self.table.dtype != cp.float32 or not self.table.flags.c_contiguous):
            raise ValueError("LUT must contain a contiguous device table of RGB triples")
        low = _triple(self.domain_min)
        high = _triple(self.domain_max)
        if any(maximum - minimum < 1e-6 for minimum, maximum in zip(low, high)):
            raise ValueError("Invalid LUT color domain")


_CUDA_FILTERS = r"""
extern "C" __global__ void horizontal(
    const float* image, float* scratch, int width, int height, int radius
) {
    const int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= width * height) return;
    const int y = index / width;
    const int x = index % width;
    float sum[4] = {0, 0, 0, 0};
    for (int offset = -radius; offset <= radius; ++offset) {
        const int column = max(0, min(width - 1, x + offset));
        const float* pixel = image + 4 * (y * width + column);
        const float alpha = pixel[3];
        for (int c = 0; c < 3; ++c) sum[c] += pixel[c] * alpha;
        sum[3] += alpha;
    }
    for (int c = 0; c < 4; ++c) scratch[4 * index + c] = sum[c];
}

__device__ float lerp(float a, float b, float t) {
    return a + (b - a) * t;
}

__device__ float lut_value(
    const float* lut, int size, int r, int g, int b, int c
) {
    return lut[3 * (r + size * (g + size * b)) + c];
}

extern "C" __global__ void apply_filters(
    const float* image, const float* scratch, float* out, const float* lut,
    int width, int height, int radius, float sharpen,
    float brightness, float contrast, float gain, float saturation,
    float temperature, int size,
    float low_r, float low_g, float low_b, float scale_r, float scale_g, float scale_b
) {
    const int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= width * height) return;
    const float* pixel = image + 4 * index;
    float rgb[3] = {pixel[0], pixel[1], pixel[2]};
    if (radius > 0) {
        const int y = index / width;
        const int x = index % width;
        float sum[4] = {0, 0, 0, 0};
        for (int offset = -radius; offset <= radius; ++offset) {
            const int row = max(0, min(height - 1, y + offset));
            const float* sample = scratch + 4 * (row * width + x);
            for (int c = 0; c < 4; ++c) sum[c] += sample[c];
        }
        if (sum[3] > 0.0f) {
            for (int c = 0; c < 3; ++c) {
                const float blurred = sum[c] / sum[3];
                rgb[c] = sharpen > 0.0f ? rgb[c] + sharpen * (rgb[c] - blurred) : blurred;
            }
        }
    }
    for (int c = 0; c < 3; ++c) {
        rgb[c] = (rgb[c] * gain - 0.5f) * contrast + 0.5f + brightness;
    }
    const float luminance = 0.2126f * rgb[0] + 0.7152f * rgb[1] + 0.0722f * rgb[2];
    for (int c = 0; c < 3; ++c) rgb[c] = lerp(luminance, rgb[c], saturation);
    rgb[0] *= 1.0f + 0.25f * temperature;
    rgb[2] *= 1.0f - 0.25f * temperature;

    if (size > 0) {
        const float low[3] = {low_r, low_g, low_b};
        const float scale[3] = {scale_r, scale_g, scale_b};
        int base[3], next[3];
        float fraction[3];
        for (int c = 0; c < 3; ++c) {
            const float entry = fminf(size - 1.0f, fmaxf(0.0f, (rgb[c] - low[c]) * scale[c]));
            base[c] = (int)entry;
            next[c] = min(size - 1, base[c] + 1);
            fraction[c] = entry - base[c];
        }
        for (int c = 0; c < 3; ++c) {
            const float front_top = lerp(lut_value(lut, size, base[0], base[1], base[2], c),
                lut_value(lut, size, next[0], base[1], base[2], c), fraction[0]);
            const float front_bottom = lerp(lut_value(lut, size, base[0], next[1], base[2], c),
                lut_value(lut, size, next[0], next[1], base[2], c), fraction[0]);
            const float back_top = lerp(lut_value(lut, size, base[0], base[1], next[2], c),
                lut_value(lut, size, next[0], base[1], next[2], c), fraction[0]);
            const float back_bottom = lerp(lut_value(lut, size, base[0], next[1], next[2], c),
                lut_value(lut, size, next[0], next[1], next[2], c), fraction[0]);
            rgb[c] = lerp(lerp(front_top, front_bottom, fraction[1]),
                          lerp(back_top, back_bottom, fraction[1]), fraction[2]);
        }
    }
    for (int c = 0; c < 3; ++c) out[4 * index + c] = fminf(1.0f, fmaxf(0.0f, rgb[c]));
    out[4 * index + 3] = pixel[3];
}
"""


@cache
def _kernels():
    module = cuda().RawModule(code=_CUDA_FILTERS)
    return module.get_function("horizontal"), module.get_function("apply_filters")


def apply(pixels, settings: FilterSettings = FilterSettings(), lut: CudaLut | None = None):
    """Apply spatial filtering, then color adjustments and optional LUT.

    Sharpen uses unsharp masking with the blur radius (defaults to 1 pixel
    when sharpening). Premultiplied neighborhood samples prevent transparent
    texels from bleeding color, while output coverage remains unchanged.
    """
    pixels = rgba(pixels)
    cp = cuda()
    if lut is not None and lut.table.device.id != pixels.device.id:
        raise ValueError("LUT is on a different CUDA device than the surface")
    if settings == FilterSettings() and lut is None:
        return pixels

    height, width = pixels.shape[:2]
    if height * width > 2 ** 29 - 1:
        raise ValueError("Filter surface exceeds CUDA kernel indexing limit")
    image = cp.ascontiguousarray(pixels)
    out = cp.empty_like(image)
    blocks = ((height * width + 255) // 256,)
    threads = (256,)
    horizontal, apply_filters = _kernels()

    radius = settings.blur_radius or int(settings.sharpen > 0)
    dimensions = tuple(map(cp.int32, (width, height, radius)))
    scratch = image
    if radius:
        scratch = cp.empty_like(image)
        horizontal(blocks, threads, (image, scratch, *dimensions))

    size = lut.size if lut is not None else 0
    low = lut.domain_min if lut is not None else (0.0, 0.0, 0.0)
    scale = ((size - 1) / (maximum - minimum)
             for minimum, maximum in zip(low, lut.domain_max)) if lut is not None else (0.0,) * 3
    parameters = (settings.sharpen, settings.brightness, settings.contrast,
                  2 ** settings.exposure, settings.saturation, settings.temperature)
    table = lut.table if lut is not None else image
    apply_filters(blocks, threads, (image, scratch, out, table, *dimensions,
                                    *map(cp.float32, parameters), cp.int32(size),
                                    *map(cp.float32, (*low, *scale))))
    return out
