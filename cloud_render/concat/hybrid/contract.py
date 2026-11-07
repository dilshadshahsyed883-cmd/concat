"""Version and timing shared by the native project, worker and hybrid effects."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import cache

BUNDLE_VERSION = 1
RENDER_MODE = "cuda-stage1"
HYBRID_EFFECTS_VERSION = 1


@dataclass(frozen=True)
class FrameRate:
    numerator: int
    denominator: int = 1

    def __post_init__(self):
        if type(self.numerator) is not int or type(self.denominator) is not int:
            raise TypeError("Frame rate needs integer numerator and denominator")
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("Frame rate must be positive")

    def seconds(self, frame_index: int) -> Fraction:
        if type(frame_index) is not int or frame_index < 0:
            raise ValueError("Frame index must be a non-negative integer")
        return Fraction(frame_index * self.denominator, self.numerator)


@dataclass(frozen=True)
class FrameSpan:
    """Half-open range in output timeline frames."""

    start: int
    end: int

    def __post_init__(self):
        if (type(self.start) is not int or type(self.end) is not int
                or self.start < 0 or self.end <= self.start):
            raise ValueError("Expected a nonempty range of non-negative frames")

    def contains(self, frame_index: int) -> bool:
        return self.start <= frame_index < self.end


@cache
def cuda():
    """Load the NVIDIA array backend on demand and require a real device."""
    try:
        import cupy
        if cupy.cuda.runtime.getDeviceCount() > 0:
            return cupy
    except (ImportError, OSError, RuntimeError) as error:
        raise RuntimeError("Hybrid effects require a working CuPy CUDA runtime") from error
    raise RuntimeError("Hybrid effects require an NVIDIA CUDA device")


def rgba(pixels):
    """Validate a CUDA H×W×4 float32 surface with straight alpha.

    RGB values use the caller's common working color encoding. Alpha and
    normalized RGB are in [0, 1]. Frames passed together must share a device.
    No implicit upload, download or software fallback is allowed.
    """
    cp = cuda()
    if not isinstance(pixels, cp.ndarray):
        raise TypeError("Expected a GPU-resident CuPy surface")
    if (pixels.ndim != 3 or pixels.shape[2] != 4 or min(pixels.shape[:2]) < 1
            or pixels.dtype != cp.float32):
        raise ValueError("Expected a nonempty H×W×4 float32 RGBA surface")
    if pixels.device.id != cp.cuda.Device().id:
        raise ValueError("Surface is on a different CUDA device")
    return pixels
