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


@cache
def _source_over():
    return cuda().ElementwiseKernel(
        "float32 sr, float32 sg, float32 sb, float32 sa, "
        "float32 dr, float32 dg, float32 db, float32 da, float32 opacity",
        "float32 red, float32 green, float32 blue, float32 alpha",
        """
        const float source = sa * opacity;
        const float remaining = da * (1.0f - source);
        const float coverage = source + remaining;
        red = coverage > 0.0f ? (sr * source + dr * remaining) / coverage : 0.0f;
        green = coverage > 0.0f ? (sg * source + dg * remaining) / coverage : 0.0f;
        blue = coverage > 0.0f ? (sb * source + db * remaining) / coverage : 0.0f;
        alpha = coverage;
        """,
        "concat_rgba_source_over",
    )


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
        source_channels = tuple(source[..., channel] for channel in range(4))
        dest_channels = tuple(dest[..., channel] for channel in range(4))
        _source_over()(*source_channels, *dest_channels, cp.float32(layer.opacity),
                       *dest_channels)
    return result
