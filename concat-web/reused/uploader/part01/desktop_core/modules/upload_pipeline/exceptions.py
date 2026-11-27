from __future__ import annotations


class UploadStopped(RuntimeError):
    """Raised when the operator pauses or stops a running upload pipeline."""


class LeaseLostError(RuntimeError):
    """Raised when a worker can no longer prove ownership of a queue job."""
