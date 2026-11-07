"""Frame-accurate CUDA transitions on equally sized straight-alpha surfaces."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from .contract import FrameSpan, cuda, rgba


_KINDS = {"dissolve": 0, "fade": 1, "wipe": 2, "slide": 3, "zoom": 4}
_DIRECTIONS = {"left": 0, "right": 1, "up": 2, "down": 3}


@dataclass(frozen=True)
class Transition:
    """A half-open overlap. Direction is the incoming frame's travel direction."""

    kind: str
    span: FrameSpan
    direction: str = "left"

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise ValueError("Unknown transition kind")
        if not isinstance(self.span, FrameSpan):
            raise TypeError("Expected a FrameSpan")
        if type(self.direction) is not str or self.direction not in _DIRECTIONS:
            raise ValueError("Unknown transition direction")

    def progress(self, frame_index: int) -> float:
        """First overlap frame is outgoing, last is incoming. One frame is a cut."""
        if type(frame_index) is not int or frame_index < 0:
            raise ValueError("Frame index must be a nonnegative integer")
        if frame_index < self.span.start:
            return 0.0
        last = self.span.end - 1
        if frame_index >= last:
            return 1.0
        return (frame_index - self.span.start) / (last - self.span.start)


_KERNEL_SOURCE = r"""
__device__ __forceinline__ float4 blend_rgba(float4 a, float4 b, float t) {
    const float wa = a.w * (1 - t), wb = b.w * t;
    const float alpha = wa + wb;
    if (alpha <= 0) return make_float4(0, 0, 0, 0);
    return make_float4((a.x * wa + b.x * wb) / alpha,
                       (a.y * wa + b.y * wb) / alpha,
                       (a.z * wa + b.z * wb) / alpha, alpha);
}

__device__ __forceinline__ float4 sample_zoom(
    const float4* src, int x, int y, int width, int height, float zoom
) {
    const float cx = (width - 1) * 0.5f, cy = (height - 1) * 0.5f;
    const float sx = cx + (x - cx) / zoom;
    const float sy = cy + (y - cy) / zoom;
    const int x0 = (int)floorf(sx), y0 = (int)floorf(sy);
    const int x1 = x0 + (x0 < width - 1), y1 = y0 + (y0 < height - 1);
    const long long row0 = (long long)y0 * width, row1 = (long long)y1 * width;
    const float4 top = blend_rgba(src[row0 + x0], src[row0 + x1], sx - x0);
    const float4 bottom = blend_rgba(src[row1 + x0], src[row1 + x1], sx - x0);
    return blend_rgba(top, bottom, sy - y0);
}

extern "C" __global__ void transition_rgba(
    const float4* outgoing, const float4* incoming, float4* output,
    int width, int height, int kind, int direction, float progress
) {
    const long long i = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (long long)width * height) return;
    const int x = i % width, y = i / width;
    const bool horizontal = direction < 2;
    const bool toward_start = (direction & 1) == 0;
    const int axis = horizontal ? x : y;
    const int extent = horizontal ? width : height;
    if (kind == 0) {
        output[i] = blend_rgba(outgoing[i], incoming[i], progress);
    } else if (kind == 1) {
        const float4 black = make_float4(0, 0, 0, 1);
        output[i] = progress < 0.5f ? blend_rgba(outgoing[i], black, progress * 2)
                                    : blend_rgba(black, incoming[i], progress * 2 - 1);
    } else if (kind == 2) {
        const int rank = toward_start ? extent - 1 - axis : axis;
        const float mask = fminf(1.f, fmaxf(0.f, progress * extent - rank));
        output[i] = blend_rgba(outgoing[i], incoming[i], mask);
    } else if (kind == 3) {
        const int shift = (int)floorf(progress * extent + 0.5f);
        int source_axis = axis + (toward_start ? shift : -shift);
        const bool enters = source_axis < 0 || source_axis >= extent;
        if (enters) source_axis += toward_start ? -extent : extent;
        const long long source_index = horizontal
            ? (long long)y * width + source_axis : (long long)source_axis * width + x;
        output[i] = enters ? incoming[source_index] : outgoing[source_index];
    } else {
        const float4 a = sample_zoom(outgoing, x, y, width, height, 1 + progress);
        const float4 b = sample_zoom(incoming, x, y, width, height, 1.5f - 0.5f * progress);
        output[i] = blend_rgba(a, b, progress);
    }
}
"""


@cache
def _kernel():
    return cuda().RawKernel(_KERNEL_SOURCE, "transition_rgba")


def render_transition(outgoing, incoming, transition: Transition, frame_index: int):
    """Render an overlap frame without staging either surface on the host.

    Endpoint frames may return one of the inputs. Both source surfaces remain
    unchanged. Dissolve, wipes and zoom mix colors weighted by alpha.
    """
    source = rgba(outgoing)
    destination = rgba(incoming)
    if source.shape != destination.shape:
        raise ValueError("Transition inputs must have identical dimensions")
    if not isinstance(transition, Transition):
        raise TypeError("Expected Transition")
    progress = transition.progress(frame_index)
    if progress == 0:
        return source
    if progress == 1:
        return destination

    cp = cuda()
    height, width, _ = source.shape
    output = cp.empty(source.shape, dtype=cp.float32)
    block_size = 256
    _kernel()(
        ((height * width + block_size - 1) // block_size,), (block_size,),
        (
            cp.ascontiguousarray(source), cp.ascontiguousarray(destination),
            output, cp.int32(width), cp.int32(height), cp.int32(_KINDS[transition.kind]),
            cp.int32(_DIRECTIONS[transition.direction]), cp.float32(progress),
        ),
    )
    return output
