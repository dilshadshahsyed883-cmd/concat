from __future__ import annotations

from ..models import UploadContext, UploadResult


class FinalizeStage:
    name = "FINALIZE"

    def run(self, ctx: UploadContext) -> UploadResult:
        final_status = ctx.job.final_status
        completed = ctx.db.complete_pipeline_job(
            ctx.job.id,
            ctx.worker_id,
            status=final_status,
            youtube_video_id=ctx.youtube_video_id,
            youtube_url=ctx.youtube_url,
        )
        if not completed:
            from .exceptions import LeaseLostError

            raise LeaseLostError(
                f"Queue lease lost for worker {ctx.worker_id}; final completion was not saved."
            )
        if ctx.job.raw.get("auto_engine_profile_id") is not None:
            try:
                from ...upload_reporting import update_auto_engine_completed_uploads_txt

                report_path = update_auto_engine_completed_uploads_txt(ctx.db, ctx.job.id)
                ctx.db.add_log(
                    ctx.job.id,
                    "INFO",
                    f"Auto Engine completed-upload TXT updated: {report_path}",
                    100,
                )
            except Exception as exc:
                warning = f"Auto Engine completed-upload TXT update failed: {exc}"
                ctx.db.add_log(ctx.job.id, "WARNING", warning, 100)
                ctx.db.update_pipeline_checkpoint(ctx.job.id, warning=warning)
                if warning not in ctx.warnings:
                    ctx.warnings.append(warning)
        if ctx.export_uploaded_links is not None:
            try:
                ctx.export_uploaded_links(ctx.db)
                ctx.db.add_log(
                    ctx.job.id,
                    "INFO",
                    "Uploaded links sheet refreshed.",
                    100,
                )
            except Exception as exc:
                warning = f"Uploaded links sheet refresh failed: {exc}"
                ctx.db.add_log(ctx.job.id, "WARNING", warning, 100)
                ctx.db.update_pipeline_checkpoint(ctx.job.id, warning=warning)
                if warning not in ctx.warnings:
                    ctx.warnings.append(warning)
        return UploadResult(
            youtube_video_id=ctx.youtube_video_id,
            youtube_url=ctx.youtube_url,
            status=final_status,
            warnings=tuple(ctx.warnings),
        )
