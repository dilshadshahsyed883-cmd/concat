"""Ordered, source-over composition on CUDA straight-alpha RGBA surfaces."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from math import isfinite

from .contract import cuda, rgba


@dataclass(frozen=True)
class Layer:
    """A resident texture placed at integer canvas coordinates."""

    texture: object
    x: int = 0
    y: int = 0
    opacity: float = 1.0

    def __post_init__(self):
        if type(self.x) is not int or type(self.y) is not int:
            raise TypeError("Layer offsets must be integer pixels")
        if type(self.opacity) not in (int, float) or not 0 <= self.opacity <= 1:
            raise ValueError("Layer opacity must be in [0, 1]")


def _clip_axis(offset: int, texture_size: int, canvas_size: int):
    start = max(0, offset)
    stop = min(canvas_size, offset + texture_size)
    if start >= stop:
        return None
    return slice(start, stop), slice(start - offset, stop - offset)


def _validate_canvas(width, height, background):
    if type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise ValueError("Canvas dimensions must be positive integers")
    if len(background) != 4 or any(
        type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1
        for value in background
    ):
        raise ValueError("Background must be four normalized RGBA values")


_CUDA_COMPOSITOR = r"""
extern "C" __global__ void source_over(
    const float* source, float* dest,
    long long width, long long height,
    long long src_row, long long src_col, long long src_chan,
    long long dst_row, long long dst_col, float opacity
) {
    const long long index = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= width * height) return;
    const long long y = index / width;
    const long long x = index % width;
    const float* input = source + y * src_row + x * src_col;
    float* output = dest + y * dst_row + x * dst_col;
    const float source_alpha = input[3 * src_chan] * opacity;
    const float remaining_alpha = output[3] * (1.0f - source_alpha);
    const float alpha = source_alpha + remaining_alpha;
    for (int c = 0; c < 3; ++c) {
        output[c] = alpha > 0.0f
            ? (input[c * src_chan] * source_alpha + output[c] * remaining_alpha) / alpha
            : 0.0f;
    }
    output[3] = alpha;
}
"""


@cache
def _source_over():
    return cuda().RawKernel(_CUDA_COMPOSITOR, "source_over")


def composite(layers, *, width: int, height: int, background=(0.0, 0.0, 0.0, 0.0)):
    """Composite bottom-to-top into one new device canvas.

    Textures stay on their original device. Layer rectangles are cropped using
    strided views, so offscreen pixels and temporary full-frame surfaces are
    avoided. All textures use the same working color encoding.
    """
    background = tuple(background)
    _validate_canvas(width, height, background)
    cp = cuda()
    result = cp.empty((height, width, 4), dtype=cp.float32)
    for channel, value in enumerate(background):
        result[..., channel].fill(value)

    for layer in layers:
        texture = rgba(layer.texture)
        horizontal = _clip_axis(layer.x, texture.shape[1], width)
        vertical = _clip_axis(layer.y, texture.shape[0], height)
        if horizontal is None or vertical is None or layer.opacity == 0:
            continue
        dst_x, src_x = horizontal
        dst_y, src_y = vertical
        source = texture[src_y, src_x]
        dest = result[dst_y, dst_x]
        roi_height, roi_width = source.shape[:2]
        strides = (*source.strides, *dest.strides[:2])
        if any(stride % source.itemsize for stride in strides):
            raise ValueError("CUDA texture strides must align to float32 elements")
        strides = tuple(cp.int64(stride // source.itemsize) for stride in strides)
        _source_over()(
            ((roi_width * roi_height + 255) // 256,), (256,),
            (source, dest, cp.int64(roi_width), cp.int64(roi_height),
             *strides, cp.float32(layer.opacity)),
        )
    return result
