from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .path_utils import resolve_stored_path
from .token_utils import VALID_TOKEN, classify_google_json
from .upload_status import UploadStatus

ACTIVE_QUEUE_STATUSES = {
    UploadStatus.QUEUED.value,
    UploadStatus.WAITING_SLOT.value,
    UploadStatus.CLAIMED.value,
    UploadStatus.UPLOADING.value,
    UploadStatus.WAITING_NETWORK.value,
    UploadStatus.THUMBNAIL_UPLOADING.value,
    UploadStatus.PAUSED.value,
    UploadStatus.SCHEDULED.value,
}


@dataclass
class PreflightIssue:
    video_id: int | None
    severity: str
    code: str
    message: str


@dataclass
class PreflightReport:
    checked: int = 0
    issues: List[PreflightIssue] = field(default_factory=list)

    @property
    def errors(self) -> List[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "ERROR"]

    @property
    def warnings(self) -> List[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "WARNING"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _readable_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.read(1)
        return True
    except Exception:
        return False


def validate_job(job: Dict[str, Any]) -> List[PreflightIssue]:
    issues: List[PreflightIssue] = []
    video_id = int(job.get("id") or 0) or None
    video_path = resolve_stored_path(job.get("local_video_path"))
    if not video_path.exists() or not video_path.is_file():
        issues.append(PreflightIssue(video_id, "ERROR", "VIDEO_MISSING", f"Video file not found: {video_path}"))
    elif video_path.stat().st_size <= 0:
        issues.append(PreflightIssue(video_id, "ERROR", "VIDEO_EMPTY", f"Video file is empty: {video_path}"))
    elif not _readable_file(video_path):
        issues.append(PreflightIssue(video_id, "ERROR", "VIDEO_UNREADABLE", f"Video file cannot be read: {video_path}"))

    title = str(job.get("title") or "").strip()
    if not title:
        issues.append(PreflightIssue(video_id, "ERROR", "TITLE_EMPTY", "Video title is empty."))
    elif len(title) > 100:
        issues.append(PreflightIssue(video_id, "ERROR", "TITLE_TOO_LONG", f"Title has {len(title)} characters; reduce it to 100 or fewer."))

    description = str(job.get("description") or "")
    if len(description) > 5000:
        issues.append(PreflightIssue(video_id, "ERROR", "DESCRIPTION_TOO_LONG", f"Description has {len(description)} characters; reduce it to 5000 or fewer."))

    tags = str(job.get("tags") or "")
    if len(tags.encode("utf-8")) > 500:
        issues.append(PreflightIssue(video_id, "WARNING", "TAGS_LARGE", "Combined tags are unusually large and may be rejected by YouTube."))

    token_path = resolve_stored_path(job.get("token_file_path") or job.get("channel_token_file_path"))
    token_status = classify_google_json(token_path)
    if token_status != VALID_TOKEN:
        issues.append(PreflightIssue(video_id, "ERROR", "TOKEN_INVALID", f"Authorized token is missing or invalid: {token_path}"))

    expected_channel_id = str(job.get("youtube_channel_id") or job.get("channel_id") or "").strip()
    if not re.fullmatch(r"UC[a-zA-Z0-9_-]{20,}", expected_channel_id):
        issues.append(PreflightIssue(video_id, "ERROR", "CHANNEL_UNVERIFIED", "Channel has no verified UC channel ID. Reconnect it before uploading."))

    channel_status = str(job.get("channel_status") or "ACTIVE").upper()
    if channel_status != "ACTIVE":
        issues.append(PreflightIssue(video_id, "ERROR", "CHANNEL_INACTIVE", f"Channel status is {channel_status}, not ACTIVE."))

    thumb_raw = str(job.get("thumbnail_path") or "").strip()
    # Reject Engine 2 jobs without thumbnails even if bypassing HTML validation.
    # Existing uploaded media is handled by UploadPipeline without re-upload.
    import os
    if os.environ.get("LUM_ENGINE_INSTANCE", "engine1").strip().lower() == "engine2" and not thumb_raw:
        issues.append(PreflightIssue(video_id, "ERROR", "THUMBNAIL_MISSING", "Engine 2 requires a thumbnail before upload."))
    if os.environ.get("LUM_ENGINE_INSTANCE", "engine1").strip().lower() == "song" and not thumb_raw:
        issues.append(PreflightIssue(video_id, "ERROR", "THUMBNAIL_MISSING", "Song Uploader requires a thumbnail before upload."))
    if thumb_raw:
        thumb = resolve_stored_path(thumb_raw)
        if not thumb.exists() or not thumb.is_file() or thumb.stat().st_size <= 0:
            issues.append(PreflightIssue(video_id, "ERROR", "THUMBNAIL_MISSING", f"Thumbnail file missing or empty: {thumb}"))
        elif thumb.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            issues.append(PreflightIssue(video_id, "WARNING", "THUMBNAIL_FORMAT", f"Thumbnail format may not be supported: {thumb.name}"))
        elif thumb.stat().st_size > 2 * 1024 * 1024:
            issues.append(PreflightIssue(video_id, "WARNING", "THUMBNAIL_LARGE", f"Thumbnail is larger than 2 MB: {thumb.name}"))

    if video_path.exists():
        try:
            free = shutil.disk_usage(video_path.parent).free
            if free < 512 * 1024 * 1024:
                issues.append(PreflightIssue(video_id, "WARNING", "LOW_DISK_SPACE", f"Less than 512 MB free near video folder: {video_path.parent}"))
        except Exception:
            pass
    return issues


def run_preflight(jobs: Iterable[Dict[str, Any]]) -> PreflightReport:
    rows = list(jobs)
    report = PreflightReport(checked=len(rows))
    seen: set[tuple[str, int]] = set()
    for job in rows:
        report.issues.extend(validate_job(job))
        key = (str(resolve_stored_path(job.get("local_video_path"))).lower(), int(job.get("channel_db_id") or 0))
        if key in seen:
            report.issues.append(PreflightIssue(int(job.get("id") or 0) or None, "WARNING", "DUPLICATE_JOB", "The same video is queued more than once for the same channel."))
        seen.add(key)
    return report
