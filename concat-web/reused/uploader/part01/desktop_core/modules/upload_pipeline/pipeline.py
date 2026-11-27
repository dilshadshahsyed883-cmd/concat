from __future__ import annotations

import gc
from typing import Any

from googleapiclient.http import MediaFileUpload

from ..preflight import validate_job
from .models import ProgressCallback, StopCheck, UploadContext, UploadJob, UploadResult
from .stages import (
    FinalizeStage,
    MediaUploadStage,
    PlaylistStage,
    PostUploadActionStage,
    ThumbnailStage,
)


class UploadPipeline:
    """Typed, checkpointed upload pipeline.

    Media identity is persisted before optional stages. Each optional stage has a
    terminal checkpoint, allowing a failed/restarted job to continue without
    repeating completed work. Playlist membership is checked before insertion so
    a crash after insertion cannot create duplicate playlist items.
    """

    def __init__(
        self,
        *,
        db: Any,
        worker_id: str,
        build_service: Any,
        wait_for_connection: Any,
        is_transient_http_error: Any,
        is_network_error: Any,
        format_http_error: Any,
        media_upload_factory: Any = MediaFileUpload,
        export_uploaded_links: Any = None,
    ):
        self.db = db
        self.worker_id = str(worker_id)
        self.build_service = build_service
        self.wait_for_connection = wait_for_connection
        self.is_transient_http_error = is_transient_http_error
        self.is_network_error = is_network_error
        self.format_http_error = format_http_error
        self.media_upload_factory = media_upload_factory
        self.export_uploaded_links = export_uploaded_links
        self.media_stage = MediaUploadStage()
        self.thumbnail_stage = ThumbnailStage()
        self.playlist_stage = PlaylistStage()
        self.post_action_stage = PostUploadActionStage()
        self.finalize_stage = FinalizeStage()

    @staticmethod
    def _preflight_errors(job: UploadJob) -> list[str]:
        issues = validate_job(dict(job.raw))
        if not job.has_uploaded_media:
            return [issue.message for issue in issues if issue.severity == "ERROR"]

        # Once YouTube has returned an ID, local media/metadata errors must not
        # force a second media upload. Only prerequisites for unfinished remote
        # stages remain blocking.
        thumbnail_needs_api = bool(
            job.thumbnail_path
            and job.thumbnail_path.exists()
            and not job.thumbnail_stage_state.terminal
        )
        playlist_needs_api = bool(
            job.playlist_id and not job.playlist_stage_state.terminal
        )
        needs_remote_api = thumbnail_needs_api or playlist_needs_api
        ignored_after_media = {
            "VIDEO_MISSING",
            "VIDEO_EMPTY",
            "VIDEO_UNREADABLE",
            "TITLE_EMPTY",
            "TITLE_TOO_LONG",
            "DESCRIPTION_TOO_LONG",
            "THUMBNAIL_MISSING",
        }
        if not needs_remote_api:
            ignored_after_media.update({"TOKEN_INVALID", "CHANNEL_INACTIVE"})
        return [
            issue.message
            for issue in issues
            if issue.severity == "ERROR" and issue.code not in ignored_after_media
        ]

    def run(
        self,
        video_id: int,
        *,
        progress_callback: ProgressCallback | None = None,
        stop_check: StopCheck | None = None,
    ) -> UploadResult:
        record = self.db.get_video(int(video_id))
        if not record:
            raise RuntimeError(f"Video queue item not found: {video_id}")
        job = UploadJob.from_record(record)
        errors = self._preflight_errors(job)
        if errors:
            raise RuntimeError("Preflight failed: " + " | ".join(errors))

        ctx = UploadContext(
            job=job,
            db=self.db,
            worker_id=self.worker_id,
            build_service=self.build_service,
            wait_for_connection=self.wait_for_connection,
            is_transient_http_error=self.is_transient_http_error,
            is_network_error=self.is_network_error,
            format_http_error=self.format_http_error,
            media_upload_factory=self.media_upload_factory,
            export_uploaded_links=self.export_uploaded_links,
            progress_callback=progress_callback,
            stop_check=stop_check,
            youtube_video_id=job.youtube_video_id,
            youtube_url=job.youtube_url,
            warnings=list(job.pipeline_warnings),
        )
        try:
            ctx.checkpoint("PREPARE")
            self.media_stage.run(ctx)
            self.thumbnail_stage.run(ctx)
            self.playlist_stage.run(ctx)
            # Release upload handles before moving/deleting the local file.
            ctx.request_handle = None
            ctx.media_handle = None
            gc.collect()
            self.post_action_stage.run(ctx)
            return self.finalize_stage.run(ctx)
        finally:
            ctx.request_handle = None
            ctx.media_handle = None
            ctx.youtube = None
            gc.collect()
