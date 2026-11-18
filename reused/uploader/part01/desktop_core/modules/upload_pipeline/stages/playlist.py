from __future__ import annotations

from typing import Any

from googleapiclient.errors import HttpError

from ...upload_status import UploadStatus
from ..models import StageState, UploadContext
from .common import pause_pipeline


class PlaylistStage:
    name = "PLAYLIST"

    def _contains_video(self, ctx: UploadContext, youtube: Any) -> bool:
        result = youtube.playlistItems().list(
            part="id",
            playlistId=ctx.job.playlist_id,
            videoId=ctx.youtube_video_id,
            maxResults=1,
        ).execute()
        return bool((result or {}).get("items"))

    def run(self, ctx: UploadContext) -> None:
        if ctx.job.playlist_stage_state.terminal:
            return
        if not ctx.job.playlist_id:
            ctx.checkpoint(self.name, playlist_state=StageState.SKIPPED)
            return

        ctx.checkpoint(self.name, playlist_state=StageState.RUNNING)
        ctx.report(98, "Checking playlist membership")
        if ctx.stop_requested():
            pause_pipeline(
                ctx,
                98,
                "Stopped by user before playlist update.",
                "Paused before playlist update",
            )
        youtube = ctx.ensure_service()
        while True:
            if ctx.stop_requested():
                pause_pipeline(
                    ctx,
                    98,
                    "Stopped by user before playlist update.",
                    "Paused before playlist update",
                )
            try:
                if self._contains_video(ctx, youtube):
                    ctx.checkpoint(self.name, playlist_state=StageState.SUCCEEDED)
                    ctx.report(99, "Video already exists in playlist; duplicate insert skipped")
                    return
                break
            except HttpError as exc:
                if ctx.is_transient_http_error(exc):
                    ctx.wait_for_connection(
                        ctx.job.id,
                        ctx.job.proxy.as_dict(),
                        98,
                        ctx.format_http_error(exc),
                        ctx.stop_check,
                        ctx.progress_callback,
                    )
                    ctx.db.update_video_status(
                        ctx.job.id,
                        status=UploadStatus.UPLOADING,
                        progress=98,
                        error_message="",
                    )
                    continue
                warning = (
                    "Playlist membership could not be verified; insert skipped to avoid duplicates: "
                    f"{ctx.format_http_error(exc)}"
                )
            except Exception as exc:
                if ctx.is_network_error(exc):
                    ctx.wait_for_connection(
                        ctx.job.id,
                        ctx.job.proxy.as_dict(),
                        98,
                        str(exc),
                        ctx.stop_check,
                        ctx.progress_callback,
                    )
                    ctx.db.update_video_status(
                        ctx.job.id,
                        status=UploadStatus.UPLOADING,
                        progress=98,
                        error_message="",
                    )
                    continue
                warning = (
                    "Playlist membership could not be verified; insert skipped to avoid duplicates: "
                    f"{exc}"
                )
            ctx.db.add_log(ctx.job.id, "WARNING", warning, 99)
            ctx.checkpoint(
                self.name,
                playlist_state=StageState.WARNING,
                warning=warning,
            )
            if ctx.progress_callback:
                ctx.progress_callback(99, warning)
            return

        ctx.report(98, "Adding video to playlist")
        while True:
            if ctx.stop_requested():
                pause_pipeline(
                    ctx,
                    98,
                    "Stopped by user before playlist add.",
                    "Paused before playlist add",
                )
            try:
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": ctx.job.playlist_id,
                            "resourceId": {
                                "kind": "youtube#video",
                                "videoId": ctx.youtube_video_id,
                            },
                        }
                    },
                ).execute()
                ctx.checkpoint(self.name, playlist_state=StageState.SUCCEEDED)
                ctx.report(99, "Added to playlist")
                return
            except HttpError as exc:
                if ctx.is_transient_http_error(exc):
                    ctx.wait_for_connection(
                        ctx.job.id,
                        ctx.job.proxy.as_dict(),
                        98,
                        ctx.format_http_error(exc),
                        ctx.stop_check,
                        ctx.progress_callback,
                    )
                    ctx.db.update_video_status(
                        ctx.job.id,
                        status=UploadStatus.UPLOADING,
                        progress=98,
                        error_message="",
                    )
                    continue
                warning = f"Playlist add failed: {ctx.format_http_error(exc)}"
            except Exception as exc:
                if ctx.is_network_error(exc):
                    ctx.wait_for_connection(
                        ctx.job.id,
                        ctx.job.proxy.as_dict(),
                        98,
                        str(exc),
                        ctx.stop_check,
                        ctx.progress_callback,
                    )
                    ctx.db.update_video_status(
                        ctx.job.id,
                        status=UploadStatus.UPLOADING,
                        progress=98,
                        error_message="",
                    )
                    continue
                warning = f"Playlist add failed: {exc}"
            ctx.db.add_log(ctx.job.id, "WARNING", warning, 99)
            ctx.checkpoint(
                self.name,
                playlist_state=StageState.WARNING,
                warning=warning,
            )
            if ctx.progress_callback:
                ctx.progress_callback(99, warning)
            return
