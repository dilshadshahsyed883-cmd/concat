from __future__ import annotations

import gc
import shutil
import time

from ..models import StageState, UploadContext


class PostUploadActionStage:
    name = "POST_ACTION"

    def run(self, ctx: UploadContext) -> None:
        if ctx.job.post_action_stage_state.terminal:
            return
        action = ctx.job.auto_engine_after_upload_action
        if not action or action == "keep":
            ctx.checkpoint(self.name, post_action_state=StageState.SKIPPED)
            return

        ctx.checkpoint(self.name, post_action_state=StageState.RUNNING)
        source = ctx.job.local_video_path
        if not source.exists() or not source.is_file():
            warning = (
                "Post-upload file action could not find the source file. "
                "It may already have completed during a previous interrupted finalization: "
                f"{source}"
            )
            ctx.db.add_log(ctx.job.id, "WARNING", warning, 100)
            ctx.checkpoint(
                self.name,
                post_action_state=StageState.WARNING,
                warning=warning,
            )
            return
        try:
            if action == "delete":
                source.unlink()
                ctx.db.add_log(
                    ctx.job.id,
                    "INFO",
                    "Auto Engine after success: video file deleted.",
                    100,
                )
                ctx.checkpoint(self.name, post_action_state=StageState.SUCCEEDED)
                return
            if action != "move_uploaded":
                warning = f"Unknown post-upload file action skipped: {action}"
                ctx.db.add_log(ctx.job.id, "WARNING", warning, 100)
                ctx.checkpoint(
                    self.name,
                    post_action_state=StageState.WARNING,
                    warning=warning,
                )
                return

            source_size = source.stat().st_size
            target_dir = source.parent / "Uploaded" / time.strftime("%Y-%m-%d")
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            if target.exists():
                stamp = time.strftime("%Y%m%d_%H%M%S")
                target = target_dir / f"{source.stem}_{stamp}{source.suffix}"
            shutil.move(str(source), str(target))
            if source.exists():
                target_ok = target.exists() and target.stat().st_size == source_size
                if target_ok:
                    last_error: Exception | None = None
                    for _attempt in range(5):
                        try:
                            gc.collect()
                            source.unlink()
                            last_error = None
                            break
                        except Exception as cleanup_exc:
                            last_error = cleanup_exc
                            time.sleep(0.7)
                    if source.exists():
                        warning = (
                            f"Moved copy to {target}, but original is still locked/not deleted: "
                            f"{last_error}"
                        )
                        ctx.db.add_log(ctx.job.id, "WARNING", warning, 100)
                        ctx.checkpoint(
                            self.name,
                            post_action_state=StageState.WARNING,
                            warning=warning,
                        )
                        return
                else:
                    warning = (
                        "Post-upload move verification failed; original kept for safety. "
                        f"Target: {target}"
                    )
                    ctx.db.add_log(ctx.job.id, "WARNING", warning, 100)
                    ctx.checkpoint(
                        self.name,
                        post_action_state=StageState.WARNING,
                        warning=warning,
                    )
                    return
            ctx.db.add_log(
                ctx.job.id,
                "INFO",
                f"Auto Engine after success: video moved to {target}",
                100,
            )
            ctx.checkpoint(self.name, post_action_state=StageState.SUCCEEDED)
        except Exception as exc:
            warning = f"Auto Engine after success action failed: {exc}"
            ctx.db.add_log(ctx.job.id, "WARNING", warning, 100)
            ctx.checkpoint(
                self.name,
                post_action_state=StageState.WARNING,
                warning=warning,
            )
