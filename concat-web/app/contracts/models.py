"""Data shapes for future authenticated /api/v1 modules.

IMPORTANT: owner_username comes only from Principal, never caller input.
These models are contracts, not a substitute for owner-enforced persistence.
"""
from typing import Literal, TypedDict

JobStatus = Literal["pending", "downloading", "queued", "rendering", "completed", "failed", "cancelled"]
UploadStatus = Literal["pending", "queued", "uploading", "completed", "failed", "cancelled"]

class PageInfo(TypedDict):
    limit: int
    cursor: str | None

class RenderRecord(TypedDict):
    id: str
    owner_username: str
    status: JobStatus
    media_id: str | None

class VerifiedMedia(TypedDict):
    id: str
    owner_username: str
    sha256: str
    bytes: int
    state: str
    drive_path: str | None

class UploadRecord(TypedDict):
    id: str
    owner_username: str
    media_id: str
    channel_id: str
    status: UploadStatus

class DashboardStats(TypedDict):
    renders_total: int
    renders_completed: int
    uploads_total: int
    uploads_completed: int
