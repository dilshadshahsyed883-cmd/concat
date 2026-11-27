from __future__ import annotations

from typing import Any

from ...upload_status import UploadStatus
from ..exceptions import UploadStopped
from ..models import UploadContext


def pause_pipeline(ctx: UploadContext, progress: int, message: str, log_message: str) -> None:
    ctx.db.update_video_status(
        ctx.job.id,
        status=UploadStatus.PAUSED,
        progress=progress,
        error_message=message,
    )
    ctx.db.add_log(ctx.job.id, "WARNING", log_message, progress)
    raise UploadStopped(message)


def save_resumable_state(ctx: UploadContext, request: Any, file_stat: Any) -> None:
    uri = str(getattr(request, "resumable_uri", None) or "").strip()
    if not uri:
        return
    ctx.db.save_resumable_state(
        ctx.job.id,
        uri,
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(getattr(request, "resumable_progress", 0) or 0),
    )
