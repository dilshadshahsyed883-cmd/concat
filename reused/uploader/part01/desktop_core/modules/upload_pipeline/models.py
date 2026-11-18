from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, MutableSequence

from ..path_utils import resolve_stored_path
from ..upload_status import UploadStatus

ProgressCallback = Callable[[int, str], None]
StopCheck = Callable[[], bool]


class StageState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SKIPPED = "SKIPPED"
    WARNING = "WARNING"

    @classmethod
    def coerce(cls, value: Any) -> "StageState":
        text = str(value or cls.PENDING.value).strip().upper()
        try:
            return cls(text)
        except ValueError:
            return cls.PENDING

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.SKIPPED, self.WARNING}


@dataclass(frozen=True)
class ProxyConfig:
    scheme: str = "http"
    host: str = ""
    port: str = ""
    username: str = ""
    password: str = ""

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ProxyConfig":
        scheme = str(record.get("proxy_scheme") or "http").strip().lower()
        if scheme not in {"http", "https"}:
            scheme = "http"
        return cls(
            scheme=scheme,
            host=str(record.get("proxy_host") or "").strip(),
            port=str(record.get("proxy_port") or "").strip(),
            username=str(record.get("proxy_username") or "").strip(),
            password=str(record.get("proxy_password") or ""),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "proxy_scheme": self.scheme,
            "proxy_host": self.host,
            "proxy_port": self.port,
            "proxy_username": self.username,
            "proxy_password": self.password,
        }


@dataclass(frozen=True)
class UploadJob:
    id: int
    local_video_path: Path
    thumbnail_path: Path | None
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str
    privacy_status: str
    made_for_kids: bool
    playlist_id: str
    token_file_path: str
    proxy: ProxyConfig
    youtube_video_id: str
    youtube_url: str
    progress: int
    resumable_uri: str
    resumable_file_size: int
    resumable_file_mtime_ns: int
    auto_engine_after_upload_action: str
    media_stage_state: StageState
    thumbnail_stage_state: StageState
    playlist_stage_state: StageState
    post_action_stage_state: StageState
    pipeline_warnings: tuple[str, ...]
    raw: Mapping[str, Any] = field(repr=False)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "UploadJob":
        privacy = str(record.get("privacy_status") or "private").strip().lower()
        if privacy not in {"private", "public", "unlisted"}:
            privacy = "private"
        thumbnail_raw = str(record.get("thumbnail_path") or "").strip()
        tags = tuple(
            part.strip() for part in str(record.get("tags") or "").split(",") if part.strip()
        )
        try:
            raw_warnings = json.loads(str(record.get("pipeline_warnings_json") or "[]"))
        except Exception:
            raw_warnings = []
        if not isinstance(raw_warnings, list):
            raw_warnings = []
        pipeline_warnings = tuple(
            str(item).strip() for item in raw_warnings if str(item).strip()
        )
        return cls(
            id=int(record.get("id") or 0),
            local_video_path=resolve_stored_path(record.get("local_video_path")),
            thumbnail_path=resolve_stored_path(thumbnail_raw) if thumbnail_raw else None,
            title=str(record.get("title") or ""),
            description=str(record.get("description") or ""),
            tags=tags,
            category_id=str(record.get("category_id") or "22"),
            privacy_status=privacy,
            made_for_kids=bool(record.get("made_for_kids")),
            playlist_id=str(record.get("playlist_id") or "").strip(),
            token_file_path=str(record.get("token_file_path") or ""),
            proxy=ProxyConfig.from_record(record),
            youtube_video_id=str(record.get("youtube_video_id") or "").strip(),
            youtube_url=str(record.get("youtube_url") or "").strip(),
            progress=max(0, min(100, int(record.get("progress") or 0))),
            resumable_uri=str(record.get("resumable_uri") or "").strip(),
            resumable_file_size=int(record.get("resumable_file_size") or -1),
            resumable_file_mtime_ns=int(record.get("resumable_file_mtime_ns") or -1),
            auto_engine_after_upload_action=str(
                record.get("auto_engine_after_upload_action") or ""
            ).strip().lower(),
            media_stage_state=StageState.coerce(record.get("media_stage_state")),
            thumbnail_stage_state=StageState.coerce(record.get("thumbnail_stage_state")),
            playlist_stage_state=StageState.coerce(record.get("playlist_stage_state")),
            post_action_stage_state=StageState.coerce(record.get("post_action_stage_state")),
            pipeline_warnings=pipeline_warnings,
            raw=record,
        )

    @property
    def final_status(self) -> UploadStatus:
        return UploadStatus.PUBLISHED if self.privacy_status == "public" else UploadStatus.UPLOADED

    @property
    def has_uploaded_media(self) -> bool:
        return bool(self.youtube_video_id)


@dataclass(frozen=True)
class UploadResult:
    youtube_video_id: str
    youtube_url: str
    status: UploadStatus
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "youtube_video_id": self.youtube_video_id,
            "youtube_url": self.youtube_url,
            "status": self.status.value,
            "warnings": list(self.warnings),
        }


@dataclass
class UploadContext:
    job: UploadJob
    db: Any
    worker_id: str
    build_service: Callable[[int, str, dict[str, str], StopCheck | None, ProgressCallback | None], Any]
    wait_for_connection: Callable[[int, dict[str, str], int, str, StopCheck | None, ProgressCallback | None], None]
    is_transient_http_error: Callable[[BaseException], bool]
    is_network_error: Callable[[BaseException], bool]
    format_http_error: Callable[[BaseException], str]
    media_upload_factory: Callable[..., Any]
    export_uploaded_links: Callable[[Any], Any] | None
    progress_callback: ProgressCallback | None = None
    stop_check: StopCheck | None = None
    youtube: Any = None
    youtube_video_id: str = ""
    youtube_url: str = ""
    warnings: MutableSequence[str] = field(default_factory=list)
    media_handle: Any = None
    request_handle: Any = None

    def report(self, progress: int, message: str) -> None:
        safe_progress = max(0, min(100, int(progress)))
        prefix = "Uploading video:" if message.startswith("Uploading video:") else None
        try:
            recorded = self.db.record_upload_progress(
                self.job.id,
                self.worker_id,
                safe_progress,
                message,
                replace_log_prefix=prefix,
            )
        except Exception as exc:
            from .exceptions import LeaseLostError

            error = f"Queue progress transaction failed for worker {self.worker_id}: {exc}"
            try:
                self.db.add_log(self.job.id, "ERROR", error, safe_progress)
            except Exception:
                pass
            raise LeaseLostError(error) from exc
        if not recorded:
            from .exceptions import LeaseLostError

            error = (
                f"Queue lease lost for worker {self.worker_id}; "
                "upload stopped before writing progress."
            )
            try:
                self.db.add_log(self.job.id, "ERROR", error, safe_progress)
            except Exception:
                pass
            raise LeaseLostError(error)
        if self.progress_callback:
            self.progress_callback(safe_progress, message)

    def checkpoint(
        self,
        pipeline_stage: str,
        *,
        media_state: StageState | None = None,
        thumbnail_state: StageState | None = None,
        playlist_state: StageState | None = None,
        post_action_state: StageState | None = None,
        warning: str | None = None,
    ) -> None:
        saved = self.db.update_pipeline_checkpoint(
            self.job.id,
            worker_id=self.worker_id,
            pipeline_stage=pipeline_stage,
            media_stage_state=media_state.value if media_state else None,
            thumbnail_stage_state=thumbnail_state.value if thumbnail_state else None,
            playlist_stage_state=playlist_state.value if playlist_state else None,
            post_action_stage_state=post_action_state.value if post_action_state else None,
            warning=warning,
        )
        if not saved:
            from .exceptions import LeaseLostError

            raise LeaseLostError(
                f"Queue lease lost for worker {self.worker_id}; pipeline checkpoint was not saved."
            )
        if warning and warning not in self.warnings:
            self.warnings.append(warning)

    def ensure_service(self) -> Any:
        if self.youtube is None:
            self.youtube = self.build_service(
                self.job.id,
                self.job.token_file_path,
                self.job.proxy.as_dict(),
                self.stop_check,
                self.progress_callback,
            )
        return self.youtube

    def stop_requested(self) -> bool:
        return bool(self.stop_check and self.stop_check())
