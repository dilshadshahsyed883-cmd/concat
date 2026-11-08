"""Opt-in, GPU-synchronized sampling for the CUDA hybrid renderer.

A measured frame is deliberately synchronized at stage boundaries. Diagnostics
are therefore NOT a throughput benchmark; leave CONCAT_CUDA_PROFILE unset for
before/after FPS comparisons on an identical project and GPU.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import os
from time import perf_counter


class StageProfiler:
    def __init__(self, enabled: bool = False, *, synchronize=None, sample_every: int = 120):
        if type(sample_every) is not int or sample_every < 1:
            raise ValueError("sample_every must be a positive integer")
        self.enabled = enabled
        self.synchronize = synchronize
        self.sample_every = sample_every
        self._samples = defaultdict(list)
        self._counters = defaultdict(int)

    @classmethod
    def from_environment(cls, *, synchronize=None):
        return cls(enabled=os.environ.get("CONCAT_CUDA_PROFILE") == "1",
                   synchronize=synchronize)

    def count(self, name: str):
        if self.enabled:
            self._counters[name] += 1

    @contextmanager
    def stage(self, name: str, frame_index: int | None = None):
        # Ignore frame zero: GPU module initialization/JIT heavily skews timings.
        if (not self.enabled or
                (frame_index is not None and
                 (frame_index < 1 or (frame_index - 1) % self.sample_every != 0))):
            yield
            return
        # CUDA kernels enqueue asynchronously; a wall clock without the sync
        # would measure only launch overhead, not device execution.
        gpu_stage = frame_index is not None and self.synchronize is not None
        if gpu_stage:
            self.synchronize()
        started = perf_counter()
        try:
            yield
        finally:
            if gpu_stage:
                self.synchronize()
            self._samples[name].append((perf_counter() - started) * 1000)

    def summary(self) -> dict:
        if not self.enabled:
            return {}
        return {
            "mode": "diagnostic-only; synchronized samples alter throughput",
            "sample_every_frames": self.sample_every,
            "stage_ms": {
                name: {"samples": len(values),
                       "mean": round(sum(values) / len(values), 3),
                       "max": round(max(values), 3)}
                for name, values in sorted(self._samples.items()) if values
            },
            "counters": dict(sorted(self._counters.items())),
        }
