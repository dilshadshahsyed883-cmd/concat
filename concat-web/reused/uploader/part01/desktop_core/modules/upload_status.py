from __future__ import annotations

from enum import Enum
from typing import Final


class UploadStatus(str, Enum):
    """Canonical queue states stored in SQLite.

    Keep status values centralized so workers, database code, preflight, and UI
    cannot silently drift to slightly different spellings.
    """

    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    WAITING_SLOT = "WAITING_SLOT"
    CLAIMED = "CLAIMED"
    UPLOADING = "UPLOADING"
    WAITING_NETWORK = "WAITING_NETWORK"
    THUMBNAIL_UPLOADING = "THUMBNAIL_UPLOADING"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    UPLOADED = "UPLOADED"
    PUBLISHED = "PUBLISHED"


class InvalidUploadStatus(ValueError):
    """Raised when code attempts to use an unknown upload status."""


class InvalidStatusTransition(ValueError):
    """Raised when a queue item is moved through an unsafe state transition."""


ALL_UPLOAD_STATUSES: Final[frozenset[str]] = frozenset(status.value for status in UploadStatus)

# Self transitions are accepted separately. The mapping describes only actual
# state changes allowed by the current queue, scheduler, preflight, and uploader.
ALLOWED_STATUS_TRANSITIONS: Final[dict[UploadStatus, frozenset[UploadStatus]]] = {
    UploadStatus.QUEUED: frozenset({
        UploadStatus.SCHEDULED,
        UploadStatus.WAITING_SLOT,
        UploadStatus.CLAIMED,
        UploadStatus.PAUSED,
        UploadStatus.FAILED,
    }),
    UploadStatus.SCHEDULED: frozenset({
        UploadStatus.QUEUED,
        UploadStatus.PAUSED,
        UploadStatus.FAILED,
    }),
    UploadStatus.WAITING_SLOT: frozenset({
        UploadStatus.QUEUED,
        UploadStatus.CLAIMED,
        UploadStatus.PAUSED,
        UploadStatus.FAILED,
    }),
    UploadStatus.CLAIMED: frozenset({
        UploadStatus.QUEUED,
        UploadStatus.UPLOADING,
        UploadStatus.WAITING_NETWORK,
        UploadStatus.PAUSED,
        UploadStatus.FAILED,
    }),
    UploadStatus.UPLOADING: frozenset({
        UploadStatus.WAITING_NETWORK,
        UploadStatus.THUMBNAIL_UPLOADING,
        UploadStatus.PAUSED,
        UploadStatus.FAILED,
        UploadStatus.UPLOADED,
        UploadStatus.PUBLISHED,
    }),
    UploadStatus.WAITING_NETWORK: frozenset({
        UploadStatus.UPLOADING,
        UploadStatus.THUMBNAIL_UPLOADING,
        UploadStatus.PAUSED,
        UploadStatus.FAILED,
    }),
    UploadStatus.THUMBNAIL_UPLOADING: frozenset({
        UploadStatus.WAITING_NETWORK,
        UploadStatus.PAUSED,
        UploadStatus.FAILED,
        UploadStatus.UPLOADED,
        UploadStatus.PUBLISHED,
    }),
    UploadStatus.PAUSED: frozenset({
        UploadStatus.QUEUED,
        UploadStatus.FAILED,
    }),
    UploadStatus.FAILED: frozenset({UploadStatus.QUEUED}),
    UploadStatus.UPLOADED: frozenset(),
    UploadStatus.PUBLISHED: frozenset(),
}


def coerce_upload_status(value: UploadStatus | str) -> UploadStatus:
    """Return a canonical UploadStatus or raise a descriptive error."""

    if isinstance(value, UploadStatus):
        return value
    text = str(value or "").strip().upper()
    try:
        return UploadStatus(text)
    except ValueError as exc:
        raise InvalidUploadStatus(f"Unknown upload status: {value!r}") from exc


def validate_status_transition(
    current: UploadStatus | str,
    target: UploadStatus | str,
) -> tuple[UploadStatus, UploadStatus]:
    """Validate and return canonical current/target states.

    Repeating the current status is safe and useful when retrying a network or
    thumbnail step while only progress/error text changes.
    """

    current_status = coerce_upload_status(current)
    target_status = coerce_upload_status(target)
    if current_status == target_status:
        return current_status, target_status
    if target_status not in ALLOWED_STATUS_TRANSITIONS[current_status]:
        raise InvalidStatusTransition(
            f"Unsafe upload status transition: {current_status.value} -> {target_status.value}"
        )
    return current_status, target_status
