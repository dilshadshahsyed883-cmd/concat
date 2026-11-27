from __future__ import annotations

from googleapiclient.errors import HttpError

from ...upload_status import UploadStatus
from ..models import StageState, UploadContext
from .common import pause_pipeline, save_resumable_state


class MediaUploadStage:
    name = "MEDIA"

    def run(self, ctx: UploadContext) -> None:
        ctx.checkpoint(self.name, media_state=StageState.RUNNING)
        if ctx.job.youtube_video_id:
            ctx.youtube_video_id = ctx.job.youtube_video_id
            ctx.youtube_url = ctx.job.youtube_url or (
                f"https://www.youtube.com/watch?v={ctx.youtube_video_id}"
            )
            ctx.db.update_video_status(
                ctx.job.id,
                status=UploadStatus.UPLOADING,
                progress=92,
                youtube_video_id=ctx.youtube_video_id,
                youtube_url=ctx.youtube_url,
                error_message="",
            )
            ctx.checkpoint(self.name, media_state=StageState.SUCCEEDED)
            ctx.report(
                92,
                f"Video already uploaded; continuing remaining steps: {ctx.youtube_url}",
            )
            return

        video_path = ctx.job.local_video_path
        if not video_path.exists() or not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        ctx.db.update_video_status(
            ctx.job.id,
            status=UploadStatus.UPLOADING,
            progress=1,
            error_message="",
        )
        ctx.report(1, "Starting video upload")
        if ctx.stop_requested():
            pause_pipeline(
                ctx,
                1,
                "Stopped by user before upload started.",
                "Paused before video upload started",
            )
        youtube = ctx.ensure_service()
        body = {
            "snippet": {
                "title": ctx.job.title,
                "description": ctx.job.description,
                "tags": list(ctx.job.tags),
                "categoryId": ctx.job.category_id,
            },
            "status": {
                "privacyStatus": ctx.job.privacy_status,
                "selfDeclaredMadeForKids": ctx.job.made_for_kids,
            },
        }
        # YouTube requires private + publishAt for scheduled PUBLIC releases.
        # Concat only: preserve the legacy uploader behavior of other studios.
        import os
        if os.getenv("LUM_ENGINE_INSTANCE") == "concat":
            publish_at = str(ctx.job.raw.get("publish_at") or "").strip()
            if publish_at:
                if body["status"]["privacyStatus"] != "private":
                    raise ValueError("Scheduled upload must be private until publication")
                body["status"]["publishAt"] = publish_at
        ctx.media_handle = ctx.media_upload_factory(
            str(video_path),
            chunksize=8 * 1024 * 1024,
            resumable=True,
            mimetype="video/*",
        )
        ctx.request_handle = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=ctx.media_handle,
        )
        request = ctx.request_handle
        file_stat = video_path.stat()
        resumable_matches = bool(
            ctx.job.resumable_uri
            and ctx.job.resumable_file_size == int(file_stat.st_size)
            and ctx.job.resumable_file_mtime_ns == int(file_stat.st_mtime_ns)
        )
        if resumable_matches:
            request.resumable_uri = ctx.job.resumable_uri
            # Google API client probes the saved session when this flag is set.
            request._in_error_state = True
            ctx.report(max(1, ctx.job.progress), "Resuming saved YouTube upload session")
        elif ctx.job.resumable_uri:
            ctx.db.clear_resumable_state(ctx.job.id)

        response = None
        last_progress = max(1, min(90, ctx.job.progress or 1))
        while response is None:
            if ctx.stop_requested():
                save_resumable_state(ctx, request, file_stat)
                pause_pipeline(
                    ctx,
                    last_progress,
                    "Stopped by user. Resume Paused will continue the saved YouTube upload session when available.",
                    "Upload paused by user; resumable session saved",
                )
            try:
                status, response = request.next_chunk()
                save_resumable_state(ctx, request, file_stat)
                if status:
                    last_progress = max(1, min(90, int(status.progress() * 90)))
                    ctx.report(last_progress, f"Uploading video: {last_progress}%")
            except HttpError as exc:
                save_resumable_state(ctx, request, file_stat)
                status_code = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
                if resumable_matches and status_code in {404, 410}:
                    ctx.db.clear_resumable_state(ctx.job.id)
                    raise RuntimeError(
                        "Saved YouTube upload session expired. Retry the job to start a fresh session."
                    ) from exc
                if ctx.is_transient_http_error(exc):
                    ctx.wait_for_connection(
                        ctx.job.id,
                        ctx.job.proxy.as_dict(),
                        last_progress,
                        ctx.format_http_error(exc),
                        ctx.stop_check,
                        ctx.progress_callback,
                    )
                    ctx.db.update_video_status(
                        ctx.job.id,
                        status=UploadStatus.UPLOADING,
                        progress=last_progress,
                        error_message="",
                    )
                    continue
                raise RuntimeError(ctx.format_http_error(exc)) from exc
            except Exception as exc:
                save_resumable_state(ctx, request, file_stat)
                if ctx.is_network_error(exc):
                    ctx.wait_for_connection(
                        ctx.job.id,
                        ctx.job.proxy.as_dict(),
                        last_progress,
                        str(exc),
                        ctx.stop_check,
                        ctx.progress_callback,
                    )
                    ctx.db.update_video_status(
                        ctx.job.id,
                        status=UploadStatus.UPLOADING,
                        progress=last_progress,
                        error_message="",
                    )
                    continue
                raise

        youtube_video_id = str((response or {}).get("id") or "").strip()
        if not youtube_video_id:
            raise RuntimeError(
                f"Upload finished but no YouTube video ID was returned: {response}"
            )
        ctx.youtube_video_id = youtube_video_id
        ctx.youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        # Persist the remote identity before any optional stage. A retry can then
        # continue without uploading the media twice.
        ctx.db.clear_resumable_state(ctx.job.id)
        ctx.db.update_video_status(
            ctx.job.id,
            youtube_video_id=ctx.youtube_video_id,
            youtube_url=ctx.youtube_url,
            progress=92,
        )
        ctx.checkpoint(self.name, media_state=StageState.SUCCEEDED)
        ctx.report(92, f"Video uploaded: {ctx.youtube_url}")
