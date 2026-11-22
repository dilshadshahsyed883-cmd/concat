from __future__ import annotations
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import THUMBNAIL_ROOT, STAGING_ROOT, ensure_dirs
from .copyright_handoff import delete_verified_source, delete_verified_merger_output
from .drive_source import cleanup_project_staging, stage_drive_file
from .project_store import ProjectStore, now_iso
from .studio_tenants import StudioTenants
from datetime import timedelta
from datetime import datetime, timezone
import os
from .remote_verify import verify_youtube_video
from desktop_core.modules.auth_manager import AuthManager
from desktop_core.modules.database import Database
from desktop_core.modules.upload_status import UploadStatus
from desktop_core.modules.youtube_uploader import YouTubeUploader


class VPSUploadWorker:
    def __init__(self, store: ProjectStore, db: Database):
        self.store = store
        self.db = db
        self.auth = AuthManager(db)
        self.uploader = YouTubeUploader(db)
        self.thread: threading.Thread | None = None
        self.shutdown_event = threading.Event()
        self.stop_current_event = threading.Event()
        self.active_project_id: int | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        ensure_dirs()
        try:
            recovered_core = int(self.db.recover_interrupted_uploads(auto_resume_safe=True) or 0)
        except Exception as exc:
            self.store.event(None, f"Core crash recovery failed: {exc}", "ERROR")
            recovered_core = 0
        recovered_wrapper = self.store.recover_wrapper_states()
        self.store.event(None, f"Worker startup recovery: wrapper={recovered_wrapper}, core={recovered_core}")
        self.shutdown_event.clear()
        self.thread = threading.Thread(target=self._run, name="lum-vps-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.shutdown_event.set()
        self.stop_current_event.set()
        if self.thread:
            self.thread.join(timeout=15)

    def request_stop_current(self) -> None:
        self.stop_current_event.set()

    def _run(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                manual_engine_enabled = self.store.setting("engine_enabled", "1") == "1"
                concat = os.getenv("LUM_ENGINE_INSTANCE") == "concat"
                if not manual_engine_enabled and not concat:
                    time.sleep(2)
                    continue
                # Scheduled Concat jobs start when due even without Start My Queue;
                # unscheduled jobs remain gated by the manual engine and owner.
                project = self.store.claim_next_eligible(
                    schedule_only=(concat and not manual_engine_enabled))
                if not project:
                    self.store.release_waiting()
                    time.sleep(2)
                    continue
                project_id = int(project["id"])
                self.active_project_id = project_id
                self.store.mark_other_waiting(project_id)
                self.stop_current_event.clear()
                self._process(project_id)
                self.active_project_id = None
                self.store.release_waiting()
                if self.store.setting("pause_after_current", "0") == "1":
                    self.store.set_setting("engine_enabled", "0")
                    self.store.set_setting("pause_after_current", "0")
                    self.store.event(None, "Queue paused after current project completed", "WARNING")
            except Exception as exc:
                pid = self.active_project_id
                if pid:
                    current = self.store.get(pid) or {}
                    if str(current.get("status") or "") not in {"COMPLETED", "CANCELLED", "PAUSED"}:
                        self.store.update(pid, status="FAILED", error_message=str(exc))
                        self.store.event(pid, f"Project failed: {exc}", "ERROR")
                else:
                    self.store.event(None, f"Worker loop error: {exc}", "ERROR")
                self.active_project_id = None
                self.store.release_waiting()
                time.sleep(2)

    def _sync_core_state(self, project_id: int, core: dict[str, Any]) -> None:
        status = str(core.get("status") or "")
        progress = int(core.get("progress") or 0)
        mapped = "UPLOADING"
        if status == UploadStatus.WAITING_NETWORK.value:
            mapped = "UPLOADING"
        elif status == UploadStatus.PAUSED.value:
            mapped = "PAUSED"
        elif status == UploadStatus.FAILED.value:
            mapped = "FAILED"
        elif status in {UploadStatus.UPLOADED.value, UploadStatus.PUBLISHED.value}:
            mapped = "VERIFYING_REMOTE"
        self.store.update(
            project_id,
            status=mapped,
            progress=progress,
            error_message=str(core.get("error_message") or ""),
            youtube_video_id=str(core.get("youtube_video_id") or "") or None,
            youtube_url=str(core.get("youtube_url") or "") or None,
        )

    def _process(self, project_id: int) -> None:
        project = self.store.get(project_id)
        if not project:
            return
        # 1) Stage the Drive source only once. Existing valid files are reused after restart/retry.
        staging = Path(str(project.get("staging_path") or "")) if project.get("staging_path") else None
        # A verified YouTube upload must NEVER be submitted again simply
        # because deletion failed or a worker restarted during cleanup.
        if project.get("remote_verified_at") and project.get("youtube_video_id"):
            self._finish_verified(project_id, project, staging)
            return
        if (not staging or not staging.is_file() or staging.stat().st_size <= 0) and project.get("source_mode") == "local_file":
            raise RuntimeError("Local input missing: keep original video and review before retry")
        if not staging or not staging.is_file() or staging.stat().st_size <= 0:
            self.store.update(project_id, status="DOWNLOADING", progress=0, error_message="")
            self.store.event(project_id, "Downloading Google Drive source to isolated VPS staging")
            staging = stage_drive_file(project_id, str(project["drive_url"]))
            self.store.update(
                project_id,
                status="SOURCE_READY",
                staging_path=str(staging),
                source_file_name=staging.name,
                source_file_size=int(staging.stat().st_size),
                progress=0,
            )
            self.store.event(project_id, f"Drive source staged and verified: {staging.name} ({staging.stat().st_size} bytes)")
        project = self.store.get(project_id) or project

        # 2) One owner-bound wrapper may claim exactly one core video row.
        concat = os.getenv("LUM_ENGINE_INSTANCE") == "concat"
        core_video_id = int(project.get("core_video_id") or 0)
        if concat and project.get("publish_at") and str(project.get("privacy_status") or "").lower() != "public":
            raise RuntimeError("Scheduled Concat release visibility changed; only Public can be scheduled")
        if concat:
            # Re-check at execution time: a channel may have been revoked since admission.
            StudioTenants(self.db).require(
                "channel", int(project["channel_db_id"]),
                {"username": str(project["owner_username"]), "is_admin": 0},
            )
            if core_video_id <= 0:
                # Crash may occur after core INSERT but before wrapper linkage is written.
                # Reuse that exact staged path + channel instead of uploading twice.
                core_video_id = self.db.video_id_for_staged_path(
                    str(staging), int(project["channel_db_id"]))
                if core_video_id:
                    self.store.update(project_id, core_video_id=core_video_id)
                    self.store.event(project_id, f"Recovered existing core upload #{core_video_id}")
        if core_video_id <= 0:
            if concat and project.get("publish_at"):
                value = datetime.fromisoformat(str(project["publish_at"]).replace("Z", "+00:00"))
                if value.tzinfo is None or value.astimezone(timezone.utc) <= datetime.now(timezone.utc) + timedelta(minutes=10):
                    raise RuntimeError("Scheduled release is too close or passed; create a new upload with a future time")

            self.store.update(project_id, status="UPLOAD_QUEUED", progress=0)
            core_video_id = int(self.db.add_video({
                "local_video_path": str(staging),
                "thumbnail_path": str(project.get("thumbnail_path") or ""),
                "title": project["title"],
                "description": project.get("description") or "",
                "tags": project.get("tags") or "",
                "category_id": project.get("category_id") or "22",
                "privacy_status": "private" if project.get("publish_at") else (project.get("privacy_status") or "private"),
                "publish_at": (project.get("publish_at") or None),
                "app_upload_at": None,
                "made_for_kids": bool(project.get("made_for_kids")),
                "playlist_id": project.get("playlist_id") or "",
                "channel_db_id": int(project["channel_db_id"]),
                "group_name": project.get("group_name") or "",
                "auto_engine_profile_id": None,
                "auto_engine_profile_name": None,
                # VPS cleanup is intentionally outside the desktop pipeline. It runs only after remote verification.
                "auto_engine_after_upload_action": "keep",
                "status": "QUEUED",
            }))
            self.store.update(project_id, core_video_id=core_video_id)
            self.store.event(project_id, f"Core upload job #{core_video_id} created")

        core = self.db.get_video(core_video_id) or {}
        if concat and project.get("publish_at") and str(core.get("status") or "") not in {
                UploadStatus.UPLOADED.value, UploadStatus.PUBLISHED.value}:
            scheduled = datetime.fromisoformat(str(project["publish_at"]).replace("Z", "+00:00"))
            if (scheduled.tzinfo is None or
                    scheduled.astimezone(timezone.utc) <= datetime.now(timezone.utc) + timedelta(minutes=10)):
                self.store.update(project_id, status="PAUSED",
                    error_message="Scheduled public release is no longer safely in the future; review and reschedule before retrying")
                self.store.event(project_id, "Unsafe expired publishAt blocked before YouTube upload", "WARNING")
                return
        if concat and str(core.get("recovery_state") or "") == "REVIEW_REQUIRED":
            self.store.update(project_id, status="PAUSED",
                error_message="Remote upload outcome unknown; review YouTube Studio before authorizing retry")
            self.store.event(project_id, "Recovery review required; automatic retry blocked", "WARNING")
            return
        if str(core.get("status") or "") == UploadStatus.FAILED.value:
            self.db.queue_failed_for_retry(exclude_auto_engine=True, video_ids=[core_video_id])
        elif str(core.get("status") or "") == UploadStatus.PAUSED.value and str(core.get("recovery_state") or "") != "REVIEW_REQUIRED":
            if concat:
                self.db.resume_paused(exclude_auto_engine=True, video_ids=[core_video_id])
            else:
                self.db.resume_paused(exclude_auto_engine=True)

        # 3) Proven desktop queue engine. Concat claims this selected ID only.
        self.store.update(project_id, status="UPLOADING")
        self.store.event(project_id, "YouTube upload started with serial concurrency=1")
        if not (concat and str(core.get("status") or "") in {
                UploadStatus.UPLOADED.value, UploadStatus.PUBLISHED.value}):
            self.uploader.process_queue(
                stop_check=lambda: self.shutdown_event.is_set() or self.stop_current_event.is_set(),
                max_concurrent=1,
                exclude_auto_engine=True,
                wait_for_future_schedules=False,
                **({"only_video_id": core_video_id} if concat else {}),
            )
        core = self.db.get_video(core_video_id) or {}
        self._sync_core_state(project_id, core)
        core_status = str(core.get("status") or "")
        if core_status == UploadStatus.PAUSED.value:
            self.store.event(project_id, "Upload paused at a safe checkpoint", "WARNING")
            return
        if core_status == UploadStatus.FAILED.value:
            raise RuntimeError(str(core.get("error_message") or "YouTube upload failed"))
        if core_status not in {UploadStatus.UPLOADED.value, UploadStatus.PUBLISHED.value}:
            raise RuntimeError(f"Unexpected core upload status after queue run: {core_status}")

        # 4) Independent remote lookup must pass before staged media is eligible for deletion.
        self.store.update(project_id, status="VERIFYING_REMOTE", progress=96)
        verified = verify_youtube_video(self.auth, self.db, core_video_id)
        verified_at = now_iso()
        self.store.update(
            project_id,
            remote_verified_at=verified_at,
            youtube_video_id=verified["video_id"],
            youtube_url=verified["youtube_url"],
            progress=98,
        )
        self.store.event(project_id, f"YouTube remote verification passed: {verified['video_id']}")

        project = self.store.get(project_id) or project
        self._finish_verified(project_id, project, staging)

    def _finish_verified(self, project_id: int, project: dict[str, Any], staging: Path | None) -> None:
        """Idempotent verified-only cleanup, never reupload on a cleanup retry."""
        if not project.get("remote_verified_at") or not project.get("youtube_video_id"):
            raise RuntimeError("Cannot delete VPS video before independent YouTube verification")
        self.store.update(project_id, status="CLEANING", progress=99)
        if self.store.setting("delete_staged_after_verified", "1") == "1":
            # Folder sources are separate handoff copies. Never delete the
            # protected Google Drive original or Video Merger master output.
            if project.get("source_origin") == "folder":
                try:
                    deleted = delete_verified_source(
                        str(project["owner_username"]), str(project["source_file_name"]),
                        str(project.get("source_fingerprint") or ""),
                        str(project.get("vps_folder") or ""),
                    )
                    self.store.event(project_id,
                        "Verified Copyright handoff source deleted from VPS"
                        if deleted else "Verified Copyright handoff source already absent")
                    removed_master = delete_verified_merger_output(
                        str(project["owner_username"]), str(project["source_file_name"]),
                        str(project.get("source_fingerprint") or ""),
                    )
                    if removed_master:
                        self.store.event(project_id,
                            "Matching owner-owned Video Merger retained MP4 deleted after YouTube verification")
                except Exception as exc:
                    raise RuntimeError(f"YouTube verified but Copyright folder cleanup needs retry: {exc}") from exc
            try:
                if staging and (staging.exists() or staging.is_symlink()):
                    if staging.is_symlink() or STAGING_ROOT.resolve() not in staging.resolve().parents:
                        raise RuntimeError("Staged source ownership changed")
                    staging.unlink()
                thumb_raw = str(project.get("thumbnail_path") or "").strip()
                if thumb_raw:
                    thumb = Path(thumb_raw)
                    if thumb.exists() and not thumb.is_symlink() and THUMBNAIL_ROOT.resolve() in thumb.resolve().parents:
                        thumb.unlink()
                cleanup_project_staging(project_id)
            except Exception as exc:
                raise RuntimeError(
                    f"YouTube upload verified, but VPS staged cleanup failed; retry cleanup only: {exc}"
                ) from exc
            if staging and (staging.exists() or staging.is_symlink()):
                raise RuntimeError("YouTube verified, but staged video still exists")
        self.store.update(project_id, status="COMPLETED", progress=100,
                          cleanup_verified_at=now_iso(), error_message="")
        self.store.event(project_id, "Project completed: remote verification + VPS cleanup verified")
