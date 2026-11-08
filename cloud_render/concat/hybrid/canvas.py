"""Native Concat Canvas backgrounds rendered entirely on CUDA.

The native Rust renderer already uses per-clip/timeline Background {none,blur,color}.
This module interprets the same persisted values without changing source media.
Only the *untransformed* image copy is enlarged and blurred; the sharp clip is
keyframed, rotated and positioned separately above it.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from math import isfinite

from .contract import cuda, rgba


@dataclass(frozen=True)
class CanvasBackground:
    kind: str
    amount: float = .5
    color: tuple[float, float, float] = (0., 0., 0.)


def parse_background(spec):
    """Validate the native JSON union, without silently dropping a style."""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError("Unsupported GPU Canvas background: expected an object")
    kind = spec.get("kind", "none")
    if kind == "none":
        if set(spec) - {"kind"}:
            raise ValueError("Unsupported GPU Canvas none parameters")
        return None
    if kind == "blur":
        if set(spec) - {"kind", "amount"}:
            raise ValueError("Unsupported GPU Canvas blur parameters")
        amount = spec.get("amount", .5)
        if type(amount) not in (int, float) or not isfinite(amount) or not 0 <= amount <= 1:
            raise ValueError("GPU Canvas blur strength must be between 0 and 100%")
        return CanvasBackground("blur", float(amount))
    if kind == "color":
        if set(spec) - {"kind", "color"}:
            raise ValueError("Unsupported GPU Canvas color parameters")
        color = spec.get("color")
        if not isinstance(color, str) or len(color) not in (7, 9) or not color.startswith("#"):
            raise ValueError("GPU Canvas solid color must be #rrggbb")
        try:
            raw = bytes.fromhex(color[1:])
        except ValueError as exc:
            raise ValueError("GPU Canvas solid color must be #rrggbb") from exc
        return CanvasBackground("color", color=tuple(value/255 for value in raw[:3]))
    raise ValueError(f"Unsupported GPU Canvas background kind: {kind}")


def fitted_rect(source_width, source_height, canvas_width, canvas_height):
    """Match Concat's contain-fit geometry before constructing the cover copy."""
    if any(type(value) is not int or value < 1 for value in
           (source_width, source_height, canvas_width, canvas_height)):
        raise ValueError("Invalid Canvas source dimensions")
    scale = min(canvas_width / source_width, canvas_height / source_height)
    width = max(1, round(source_width * scale))
    height = max(1, round(source_height * scale))
    return ((canvas_width - width)//2, (canvas_height - height)//2, width, height)


_COVER_KERNEL = r"""
extern "C" __global__ void canvas_cover(
    const float4* src, float4* out,
    int width, int height, int left, int top, int src_width, int src_height
) {
    long long i = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (long long)width * height) return;
    float zoom = fmaxf((float)width / src_width, (float)height / src_height);
    float x = left + (src_width - 1) * 0.5f
        + ((int)(i % width) - (width - 1) * 0.5f) / zoom;
    float y = top + (src_height - 1) * 0.5f
        + ((int)(i / width) - (height - 1) * 0.5f) / zoom;
    x = fminf(fmaxf(x, (float)left), (float)(left + src_width - 1));
    y = fminf(fmaxf(y, (float)top), (float)(top + src_height - 1));
    int x0 = (int)floorf(x), y0 = (int)floorf(y);
    int x1 = min(x0 + 1, left + src_width - 1);
    int y1 = min(y0 + 1, top + src_height - 1);
    float fx = x - x0, fy = y - y0;
    float4 a = src[(long long)y0 * width + x0];
    float4 b = src[(long long)y0 * width + x1];
    float4 c = src[(long long)y1 * width + x0];
    float4 d = src[(long long)y1 * width + x1];
    float4 blended;
    blended.x = (a.x*(1-fx) + b.x*fx)*(1-fy) + (c.x*(1-fx) + d.x*fx)*fy;
    blended.y = (a.y*(1-fx) + b.y*fx)*(1-fy) + (c.y*(1-fx) + d.y*fx)*fy;
    blended.z = (a.z*(1-fx) + b.z*fx)*(1-fy) + (c.z*(1-fx) + d.z*fx)*fy;
    blended.w = 1.0f;
    out[i] = blended;
}
"""


@cache
def _cover():
    return cuda().RawKernel(_COVER_KERNEL, "canvas_cover")


def make_background(pixels, source_size, background: CanvasBackground):
    """New full-frame device texture; never edit or blur the foreground pixels."""
    cp = cuda()
    frame = rgba(pixels)
    height, width = frame.shape[:2]
    if background.kind == "color":
        output = cp.empty((height, width, 4), dtype=cp.float32)
        for channel, value in enumerate((*background.color, 1.)):
            output[:, :, channel].fill(value)
        return output
    if background.kind != "blur":
        raise ValueError("Unsupported GPU Canvas background mode")
    left, top, fitted_width, fitted_height = fitted_rect(
        source_size[0], source_size[1], width, height)
    source = cp.ascontiguousarray(frame)
    output = cp.empty((height, width, 4), dtype=cp.float32)
    _cover()(((width * height + 255) // 256,), (256,),
             (source, output, cp.int32(width), cp.int32(height),
              cp.int32(left), cp.int32(top),
              cp.int32(fitted_width), cp.int32(fitted_height)))
    # Native Canvas maps 100% to a Gaussian sigma of 1/12 of the
    # shorter frame edge. Downsample only for wider Gaussian kernels,
    # keeping all spatial processing CUDA resident at large strengths.
    sigma = background.amount * min(width, height) / 12.
    if sigma < .15:
        return output
    factor = 1 if sigma <= 2 else 2 if sigma <= 5 else 4 if sigma <= 12 else 8
    from cupyx.scipy.ndimage import gaussian_filter, zoom
    smaller = output[::factor, ::factor].copy()
    filtered = gaussian_filter(smaller, sigma=(sigma / factor, sigma / factor, 0),
                               mode="nearest")
    if factor == 1:
        return filtered
    enlarged = zoom(filtered, (height / smaller.shape[0], width / smaller.shape[1], 1),
                    order=1, prefilter=False)
    return cp.ascontiguousarray(enlarged[:height, :width])
