from __future__ import annotations

from googleapiclient.errors import HttpError

from ...upload_status import UploadStatus
from ..models import StageState, UploadContext
from .common import pause_pipeline


class ThumbnailStage:
    name = "THUMBNAIL"

    def run(self, ctx: UploadContext) -> None:
        if ctx.job.thumbnail_stage_state.terminal:
            return
        thumb = ctx.job.thumbnail_path
        if thumb is None:
            ctx.checkpoint(self.name, thumbnail_state=StageState.SKIPPED)
            return
        if not thumb.exists() or not thumb.is_file():
            warning = f"Thumbnail upload skipped; file not found: {thumb}"
            ctx.db.add_log(ctx.job.id, "WARNING", warning, 97)
            ctx.checkpoint(
                self.name,
                thumbnail_state=StageState.WARNING,
                warning=warning,
            )
            return

        ctx.checkpoint(self.name, thumbnail_state=StageState.RUNNING)
        ctx.db.update_video_status(
            ctx.job.id,
            status=UploadStatus.THUMBNAIL_UPLOADING,
            progress=93,
            error_message="",
        )
        ctx.report(93, "Uploading thumbnail")
        if ctx.stop_requested():
            pause_pipeline(
                ctx,
                93,
                "Stopped by user before thumbnail upload.",
                "Paused before thumbnail upload",
            )
        youtube = ctx.ensure_service()
        while True:
            if ctx.stop_requested():
                pause_pipeline(
                    ctx,
                    93,
                    "Stopped by user before thumbnail upload.",
                    "Paused before thumbnail upload",
                )
            try:
                youtube.thumbnails().set(
                    videoId=ctx.youtube_video_id,
                    media_body=ctx.media_upload_factory(str(thumb)),
                ).execute()
                ctx.checkpoint(self.name, thumbnail_state=StageState.SUCCEEDED)
                ctx.report(97, "Thumbnail uploaded")
                return
            except HttpError as exc:
                if ctx.is_transient_http_error(exc):
                    ctx.wait_for_connection(
                        ctx.job.id,
                        ctx.job.proxy.as_dict(),
                        93,
                        ctx.format_http_error(exc),
                        ctx.stop_check,
                        ctx.progress_callback,
                    )
                    ctx.db.update_video_status(
                        ctx.job.id,
                        status=UploadStatus.THUMBNAIL_UPLOADING,
                        progress=93,
                        error_message="",
                    )
                    continue
                warning = f"Thumbnail upload failed: {ctx.format_http_error(exc)}"
            except Exception as exc:
                if ctx.is_network_error(exc):
                    ctx.wait_for_connection(
                        ctx.job.id,
                        ctx.job.proxy.as_dict(),
                        93,
                        str(exc),
                        ctx.stop_check,
                        ctx.progress_callback,
                    )
                    ctx.db.update_video_status(
                        ctx.job.id,
                        status=UploadStatus.THUMBNAIL_UPLOADING,
                        progress=93,
                        error_message="",
                    )
                    continue
                warning = f"Thumbnail upload failed: {exc}"
            ctx.db.add_log(ctx.job.id, "WARNING", warning, 97)
            ctx.checkpoint(
                self.name,
                thumbnail_state=StageState.WARNING,
                warning=warning,
            )
            if ctx.progress_callback:
                ctx.progress_callback(97, warning)
            return
