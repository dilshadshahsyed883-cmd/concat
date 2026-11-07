"""Frame-indexed transforms on CUDA-resident straight-alpha RGBA surfaces."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, fields
from functools import cache
from math import cos, radians, sin

from .contract import cuda, rgba


@dataclass(frozen=True)
class Transform:
    """Translation in pixels, pan in viewport fractions, clockwise rotation."""

    x: float = 0.0
    y: float = 0.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    zoom: float = 1.0
    scale: float = 1.0
    rotation_degrees: float = 0.0
    opacity: float = 1.0

    def __post_init__(self) -> None:
        for name in ("x", "y", "pan_x", "pan_y", "rotation_degrees"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not -1e9 <= value <= 1e9:
                raise ValueError(f"{name} must be a finite number within ±1e9")
        for name in ("zoom", "scale"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 1e-4 <= value <= 1e4:
                raise ValueError(f"{name} must be in [0.0001, 10000]")
        if type(self.opacity) not in (int, float) or not 0 <= self.opacity <= 1:
            raise ValueError("Opacity must be in [0, 1]")


_EASING = {
    "linear": lambda t: t,
    "ease-in": lambda t: t * t,
    "ease-out": lambda t: t * (2 - t),
    "ease-in-out": lambda t: t * t * (3 - 2 * t),
    "hold": lambda t: 0.0,
}
_TRANSFORM_FIELDS = tuple(field.name for field in fields(Transform))


@dataclass(frozen=True)
class Keyframe:
    frame: int
    transform: Transform
    easing: str = "linear"

    def __post_init__(self) -> None:
        if type(self.frame) is not int or self.frame < 0:
            raise ValueError("Keyframe position must be a nonnegative integer")
        if not isinstance(self.transform, Transform):
            raise TypeError("Keyframe requires a Transform")
        if type(self.easing) is not str or self.easing not in _EASING:
            raise ValueError("Unknown keyframe easing")


@dataclass(frozen=True)
class Animation:
    """Ordered keyframes. Each keyframe's easing controls the following segment."""

    keyframes: tuple[Keyframe, ...]

    def __post_init__(self) -> None:
        keyframes = tuple(self.keyframes)
        if not keyframes or not all(isinstance(key, Keyframe) for key in keyframes):
            raise ValueError("Animation requires at least one Keyframe")
        if any(a.frame >= b.frame for a, b in zip(keyframes, keyframes[1:])):
            raise ValueError("Keyframes must be in strictly increasing frame order")
        object.__setattr__(self, "keyframes", keyframes)

    def sample(self, frame_index: int) -> Transform:
        if type(frame_index) is not int or frame_index < 0:
            raise ValueError("Frame index must be a nonnegative integer")
        right = bisect_right(self.keyframes, frame_index, key=lambda key: key.frame)
        if right == 0:
            return self.keyframes[0].transform
        left = self.keyframes[right - 1]
        if right == len(self.keyframes) or frame_index == left.frame:
            return left.transform
        next_key = self.keyframes[right]
        fraction = (frame_index - left.frame) / (next_key.frame - left.frame)
        weight = _EASING[left.easing](fraction)
        values = {
            name: getattr(left.transform, name) * (1 - weight)
            + getattr(next_key.transform, name) * weight
            for name in _TRANSFORM_FIELDS
        }
        return Transform(**values)


_KERNEL_SOURCE = r"""
extern "C" __global__ void transform_rgba(
    const float4* src, float4* dst, int width, int height,
    float offset_x, float offset_y, float inv_cos, float inv_sin, float opacity
) {
    const long long i = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (long long)width * height) return;
    const float cx = (width - 1) * 0.5f;
    const float cy = (height - 1) * 0.5f;
    const float x = i % width - cx - offset_x;
    const float y = i / width - cy - offset_y;
    const float sx = cx + x * inv_cos + y * inv_sin;
    const float sy = cy - x * inv_sin + y * inv_cos;

    float4 sum = make_float4(0, 0, 0, 0);
    if (sx > -1 && sy > -1 && sx < width && sy < height && opacity > 0) {
        const int ix = (int)floorf(sx), iy = (int)floorf(sy);
        const float fx = sx - ix, fy = sy - iy;
        for (int dy = 0; dy < 2; ++dy) {
            for (int dx = 0; dx < 2; ++dx) {
                const int px = ix + dx, py = iy + dy;
                if (px < 0 || py < 0 || px >= width || py >= height) continue;
                const float4 p = src[(long long)py * width + px];
                const float alpha = p.w * (dx ? fx : 1 - fx) * (dy ? fy : 1 - fy);
                sum.x += p.x * alpha;
                sum.y += p.y * alpha;
                sum.z += p.z * alpha;
                sum.w += alpha;
            }
        }
    }
    if (sum.w > 0) {
        sum.x /= sum.w;
        sum.y /= sum.w;
        sum.z /= sum.w;
        sum.w *= opacity;
    }
    dst[i] = sum;
}
"""


@cache
def _kernel():
    return cuda().RawKernel(_KERNEL_SOURCE, "transform_rgba")


def apply_transform(pixels, transform: Transform):
    """Inverse-map a layer in one CUDA pass with transparent edges.

    An identity transform may return the input surface. The source is never
    modified. Bilinear samples are combined with alpha before unpremultiplying.
    """
    source = rgba(pixels)
    if not isinstance(transform, Transform):
        raise TypeError("Expected Transform")
    if transform == Transform():
        return source

    cp = cuda()
    height, width, _ = source.shape
    inverse_scale = 1 / (transform.zoom * transform.scale)
    angle = radians(transform.rotation_degrees % 360)
    output = cp.empty(source.shape, dtype=cp.float32)
    block_size = 256
    _kernel()(
        ((height * width + block_size - 1) // block_size,), (block_size,),
        (
            cp.ascontiguousarray(source), output, cp.int32(width), cp.int32(height),
            cp.float32(transform.x + transform.pan_x * width),
            cp.float32(transform.y + transform.pan_y * height),
            cp.float32(cos(angle) * inverse_scale),
            cp.float32(sin(angle) * inverse_scale),
            cp.float32(transform.opacity),
        ),
    )
    return output


def render_animation(pixels, animation: Animation, frame_index: int):
    """Sample the timeline transform and apply it to the current source frame."""
    return apply_transform(pixels, animation.sample(frame_index))
