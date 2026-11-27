from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from .repository_base import BaseRepository
from ..upload_status import UploadStatus, coerce_upload_status, validate_status_transition
from ..upload_identity import DuplicateUploadError


class QueueRepository(BaseRepository):
    BLOCKING_LEDGER_STATES = frozenset({
        "RESERVED", "CLAIMED", "MEDIA_IN_PROGRESS", "RESUMABLE",
        "REMOTE_CREATED", "COMPLETED", "AMBIGUOUS",
    })
    REUSABLE_LEDGER_STATES = frozenset({"FAILED_SAFE", "CANCELLED"})

    @staticmethod
    def normalize_filename(path_or_name: Any) -> tuple[str, str]:
        original = str(path_or_name or "").replace('\\', '/').rsplit('/', 1)[-1].strip()
        return original, original.casefold()

    def _filename_duplicate_message(self, channel_db_id: int, original_filename: str) -> str:
        with self.connect() as conn:
            channel = conn.execute("SELECT channel_id, channel_name FROM channels WHERE id=?", (int(channel_db_id),)).fetchone()
        label = str((channel and (channel["channel_name"] or channel["channel_id"])) or channel_db_id)
        return (f"SKIPPED_DUPLICATE_FILENAME | Channel: {label} | File: {original_filename} | "
                "Reason: filename previously uploaded successfully to this channel")

    def _record_duplicate_block(self, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_meta(key, value) VALUES ('duplicate_uploads_blocked', '1')
                ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1
                """
            )
            conn.execute(
                "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (NULL, 'WARNING', ?, NULL)",
                (str(message),),
            )

    def is_channel_filename_duplicate(self, channel_db_id: int, path_or_name: Any) -> bool:
        """Read-only duplicate check used by Auto Engine preview/planning."""
        _original, normalized = self.normalize_filename(path_or_name)
        if not normalized:
            return True
        with self.connect() as conn:
            channel = conn.execute(
                "SELECT channel_id FROM channels WHERE id=?",
                (int(channel_db_id),),
            ).fetchone()
            if not channel:
                return True
            historic = conn.execute(
                """
                SELECT 1 FROM successful_filename_uploads
                WHERE channel_id=? AND normalized_filename=? LIMIT 1
                """,
                (str(channel["channel_id"]), normalized),
            ).fetchone()
            if historic:
                return True
            active = conn.execute(
                """
                SELECT 1 FROM videos
                WHERE channel_db_id=? AND normalized_filename=?
                  AND status NOT IN ('FAILED', 'CANCELLED')
                LIMIT 1
                """,
                (int(channel_db_id), normalized),
            ).fetchone()
            return bool(active)

    @staticmethod
    def _ledger_state_for_row(row: sqlite3.Row | Dict[str, Any], *, failed: bool = False) -> str:
        if str(row["youtube_video_id"] or "").strip():
            return "REMOTE_CREATED"
        if str(row["resumable_uri"] or "").strip():
            return "RESUMABLE"
        media_state = str(row["media_stage_state"] or "PENDING").upper()
        pipeline_stage = str(row["pipeline_stage"] or "PENDING").upper()
        if media_state == "RUNNING" or pipeline_stage == "MEDIA":
            return "AMBIGUOUS" if failed else "MEDIA_IN_PROGRESS"
        return "FAILED_SAFE" if failed else "RESERVED"

    def _set_ledger_state(
        self,
        conn: sqlite3.Connection,
        video_id: int,
        state: str,
        *,
        youtube_video_id: str | None = None,
        youtube_url: str | None = None,
        last_error: str | None = None,
    ) -> None:
        row = conn.execute(
            "SELECT channel_db_id, upload_fingerprint FROM videos WHERE id=?",
            (int(video_id),),
        ).fetchone()
        if not row or not str(row["upload_fingerprint"] or "").strip():
            return
        assignments = ["video_id=?", "state=?", "updated_at=CURRENT_TIMESTAMP"]
        params: list[Any] = [int(video_id), str(state)]
        if youtube_video_id is not None:
            assignments.append("youtube_video_id=?")
            params.append(str(youtube_video_id))
        if youtube_url is not None:
            assignments.append("youtube_url=?")
            params.append(str(youtube_url))
        if last_error is not None:
            assignments.append("last_error=?")
            params.append(str(last_error))
        params.extend([int(row["channel_db_id"]), str(row["upload_fingerprint"])])
        conn.execute(
            f"""
            UPDATE upload_ledger SET {', '.join(assignments)}
            WHERE channel_db_id=? AND upload_fingerprint=?
            """,
            params,
        )

    def ensure_video_upload_identity(self, video_id: int) -> bool:
        # Kept for compatibility with older callers. Filename-only protection
        # requires no content hashing or upload-fingerprint reservation.
        with self.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM videos WHERE id=?", (int(video_id),)).fetchone())

    def ensure_pending_upload_identities(self, **kwargs: Any) -> int:
        # Filename protection is handled by the database history and active-row unique index.
        # Hashes are not used for duplicate eligibility.
        return 0

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            # Treat naive datetimes as local PC time, matching QDateTimeEdit behavior.
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt

    @classmethod
    def _is_future_datetime(cls, value: Any) -> bool:
        dt = cls._parse_iso_datetime(value)
        if not dt:
            return False
        now = datetime.now(dt.tzinfo)
        return dt > now

    def activate_due_scheduled_jobs(self, auto_engine_only: bool = False, exclude_auto_engine: bool = False) -> int:
        """Move app-side scheduled jobs into the upload queue once their time is due.

        This is intentionally app-side only. The timestamp is never sent to YouTube
        as publishAt; YouTube receives the upload request only when this method marks
        the job QUEUED.
        """
        due_ids: List[int] = []
        with self.connect() as conn:
            query = "SELECT id, app_upload_at FROM videos WHERE status='SCHEDULED' AND app_upload_at IS NOT NULL AND app_upload_at!=''"
            if auto_engine_only:
                query += " AND auto_engine_profile_id IS NOT NULL"
            elif exclude_auto_engine:
                query += " AND auto_engine_profile_id IS NULL"
            query += " ORDER BY app_upload_at ASC, id ASC"
            rows = conn.execute(query).fetchall()
            for row in rows:
                dt = self._parse_iso_datetime(row["app_upload_at"])
                if not dt or dt <= datetime.now(dt.tzinfo):
                    due_ids.append(int(row["id"]))
            if not due_ids:
                return 0
            placeholders = ",".join("?" for _ in due_ids)
            conn.execute(
                f"UPDATE videos SET status='QUEUED', error_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                due_ids,
            )
            conn.executemany(
                "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                [(vid, "INFO", "App schedule time reached; moved to queue", 0) for vid in due_ids],
            )
            return len(due_ids)

    def next_scheduled_upload_at(self, auto_engine_only: bool = False, exclude_auto_engine: bool = False) -> Optional[str]:
        with self.connect() as conn:
            query = "SELECT app_upload_at FROM videos WHERE status='SCHEDULED' AND app_upload_at IS NOT NULL AND app_upload_at!=''"
            if auto_engine_only:
                query += " AND auto_engine_profile_id IS NOT NULL"
            elif exclude_auto_engine:
                query += " AND auto_engine_profile_id IS NULL"
            query += " ORDER BY app_upload_at ASC, id ASC LIMIT 1"
            row = conn.execute(query).fetchone()
            return str(row["app_upload_at"]) if row else None

    def mark_waiting_slot(self, video_ids: List[int]) -> None:
        ids = [int(v) for v in video_ids if v]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE videos SET status='WAITING_SLOT', updated_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders}) AND status='QUEUED'",
                ids,
            )

    def queue_waiting_slot(self, video_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE videos SET status='QUEUED', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='WAITING_SLOT'",
                (int(video_id),),
            )

    def add_video(self, data: Dict[str, Any]) -> int:
        keys = [
            "local_video_path", "thumbnail_path", "title", "description",
            "tags", "category_id", "privacy_status", "publish_at",
            "app_upload_at", "made_for_kids", "playlist_id",
            "channel_db_id", "group_name", "auto_engine_profile_id",
            "auto_engine_profile_name", "auto_engine_after_upload_action",
        ]
        requested_status = str(data.get("status") or "").strip().upper()
        if not requested_status:
            requested_status = (UploadStatus.SCHEDULED.value if self._is_future_datetime(data.get("app_upload_at")) else UploadStatus.QUEUED.value)
        status_enum = coerce_upload_status(requested_status)
        if status_enum not in {UploadStatus.QUEUED, UploadStatus.SCHEDULED, UploadStatus.WAITING_SLOT, UploadStatus.PAUSED, UploadStatus.FAILED}:
            raise ValueError(f"Invalid initial upload status: {status_enum.value}")
        status = status_enum.value
        channel_db_id = int(data.get("channel_db_id") or 0)
        original_filename, normalized_filename = self.normalize_filename(data.get("local_video_path"))
        if not normalized_filename:
            raise ValueError("Video filename is required")
        keys.extend(["status", "original_filename", "normalized_filename", "recovery_state"])
        values = [data.get(k) for k in keys[:-4]] + [status, original_filename, normalized_filename, "NONE"]
        placeholders = ",".join("?" for _ in keys)
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                channel = conn.execute("SELECT channel_id, channel_name FROM channels WHERE id=?", (channel_db_id,)).fetchone()
                if not channel:
                    raise ValueError(f"Channel not found: {channel_db_id}")
                historic = conn.execute(
                    "SELECT 1 FROM successful_filename_uploads WHERE channel_id=? AND normalized_filename=? LIMIT 1",
                    (str(channel["channel_id"]), normalized_filename),
                ).fetchone()
                if historic:
                    message = (f"SKIPPED_DUPLICATE_FILENAME | Channel: {channel['channel_name'] or channel['channel_id']} | "
                               f"File: {original_filename} | Reason: filename previously uploaded successfully to this channel")
                    conn.execute("INSERT INTO upload_logs(video_id, level, message, progress) VALUES (NULL, 'WARNING', ?, 0)", (message,))
                    conn.commit()
                    raise DuplicateUploadError("Skipped: filename already uploaded to this channel.")
                cur = conn.execute(f"INSERT INTO videos({','.join(keys)}) VALUES ({placeholders})", values)
                video_id = int(cur.lastrowid)
                conn.execute("INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                             (video_id, "INFO", "Added to app schedule" if status == "SCHEDULED" else "Added to queue", 0))
                conn.commit()
                return video_id
        except sqlite3.IntegrityError as exc:
            if "videos.channel_db_id, videos.normalized_filename" in str(exc) or "uq_active_channel_filename" in str(exc):
                message = "Skipped: filename already queued for this channel."
                self._record_duplicate_block(message)
                raise DuplicateUploadError(message) from exc
            raise
        except DuplicateUploadError as exc:
            self._record_duplicate_block(str(exc))
            raise

    def update_video_status(
        self,
        video_id: int,
        *,
        status: Optional[UploadStatus | str] = None,
        progress: Optional[int] = None,
        error_message: Optional[str] = None,
        youtube_video_id: Optional[str] = None,
        youtube_url: Optional[str] = None,
        increment_retry: bool = False,
    ) -> None:
        """Update one queue item and its persistent duplicate-prevention ledger."""

        assignments: list[str] = ["updated_at=CURRENT_TIMESTAMP"]
        params: list[Any] = []
        target_status: UploadStatus | None = None
        if status is not None:
            target_status = coerce_upload_status(status)
        if progress is not None:
            assignments.append("progress=?")
            params.append(max(0, min(100, int(progress))))
        if error_message is not None:
            assignments.append("error_message=?")
            params.append(str(error_message))
        if youtube_video_id is not None:
            assignments.append("youtube_video_id=?")
            params.append(str(youtube_video_id))
        if youtube_url is not None:
            assignments.append("youtube_url=?")
            params.append(str(youtube_url))
        if increment_retry:
            assignments.append("retry_count=retry_count+1")

        with self.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id=?", (int(video_id),)).fetchone()
            if not row:
                raise KeyError(f"Video queue item not found: {video_id}")
            if target_status is not None:
                _current, canonical_target = validate_status_transition(row["status"], target_status)
                assignments.append("status=?")
                params.append(canonical_target.value)
            params.append(int(video_id))
            cur = conn.execute(f"UPDATE videos SET {', '.join(assignments)} WHERE id=?", params)
            if not cur.rowcount:
                raise KeyError(f"Video queue item not found: {video_id}")

            updated = conn.execute("SELECT * FROM videos WHERE id=?", (int(video_id),)).fetchone()
            if not updated:
                return
            current_status = str(updated["status"] or "")
            ledger_state: str | None = None
            remote_id = str(updated["youtube_video_id"] or "").strip()
            remote_url = str(updated["youtube_url"] or "").strip()
            if remote_id:
                ledger_state = "REMOTE_CREATED"
            elif current_status == UploadStatus.FAILED.value:
                ledger_state = self._ledger_state_for_row(updated, failed=True)
            elif current_status in {
                UploadStatus.UPLOADING.value,
                UploadStatus.WAITING_NETWORK.value,
                UploadStatus.THUMBNAIL_UPLOADING.value,
            }:
                ledger_state = self._ledger_state_for_row(updated, failed=False)
            elif current_status == UploadStatus.PAUSED.value:
                ledger_state = self._ledger_state_for_row(updated, failed=False)
                if ledger_state == "MEDIA_IN_PROGRESS":
                    ledger_state = "AMBIGUOUS"
            elif current_status in {UploadStatus.QUEUED.value, UploadStatus.WAITING_SLOT.value, UploadStatus.SCHEDULED.value}:
                ledger_state = "RESUMABLE" if str(updated["resumable_uri"] or "").strip() else "RESERVED"

            if ledger_state:
                if ledger_state == "AMBIGUOUS":
                    conn.execute(
                        "UPDATE videos SET recovery_state='REVIEW_REQUIRED' WHERE id=?",
                        (int(video_id),),
                    )
                self._set_ledger_state(
                    conn,
                    int(video_id),
                    ledger_state,
                    youtube_video_id=remote_id if remote_id else None,
                    youtube_url=remote_url if remote_url else None,
                    last_error=str(updated["error_message"] or "") if current_status == UploadStatus.FAILED.value else None,
                )

    def list_videos(self, statuses: Optional[Iterable[str]] = None, auto_engine_only: bool = False, exclude_auto_engine: bool = False) -> List[Dict[str, Any]]:
        query = """
            SELECT
                v.*,
                c.channel_name,
                c.channel_id AS youtube_channel_id,
                COALESCE(NULLIF(v.auth_token_file_path, ''), c.token_file_path) AS channel_token_file_path, /* LUM_DUAL_AUTH_CONNECTIONS_V2_QUEUE */
                c.channel_url AS stored_channel_url,
                c.browser_profile_name,
                c.proxy_scheme,
                c.proxy_host,
                c.proxy_port,
                c.proxy_username,
                c.proxy_password,
                c.status AS channel_status,
                c.sheet_source_id AS sheet_source_id,
                ss.sheet_name AS sheet_source_name,
                COALESCE(NULLIF(c.channel_url, ''), 'https://www.youtube.com/channel/' || c.channel_id) AS channel_url
            FROM videos v
            JOIN channels c ON c.id = v.channel_db_id
            LEFT JOIN sheet_sources ss ON ss.id = c.sheet_source_id
        """
        params: list[Any] = []
        where: list[str] = []
        if statuses:
            st = list(statuses)
            where.append(f"v.status IN ({','.join('?' for _ in st)})")
            params.extend(st)
        if auto_engine_only:
            where.append("v.auto_engine_profile_id IS NOT NULL")
        elif exclude_auto_engine:
            where.append("v.auto_engine_profile_id IS NULL")
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY v.created_at ASC, v.id ASC"
        with self.connect() as conn:
            return [self._decode_sensitive_record(r) for r in conn.execute(query, params).fetchall()]

    def get_video(self, video_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    v.*,
                    c.channel_name,
                    c.channel_id AS youtube_channel_id,
                    COALESCE(NULLIF(v.auth_token_file_path, ''), c.token_file_path) AS token_file_path,
                    c.browser_profile_name,
                    c.proxy_scheme,
                    c.proxy_host,
                    c.proxy_port,
                    c.proxy_username,
                    c.proxy_password,
                    c.status AS channel_status,
                    COALESCE(NULLIF(c.channel_url, ''), 'https://www.youtube.com/channel/' || c.channel_id) AS channel_url
                FROM videos v
                JOIN channels c ON c.id = v.channel_db_id
                WHERE v.id=?
                """,
                (video_id,),
            ).fetchone()
            return self._decode_sensitive_record(row) if row else None

    def video_id_for_staged_path(self, path: str, channel_db_id: int) -> int:
        """Recover a crashed wrapper-to-core handoff, never guess among duplicates."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM videos WHERE local_video_path=? AND channel_db_id=? ORDER BY id LIMIT 2",
                (str(path), int(channel_db_id)),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("Ambiguous staged video; manual reconciliation required")
        return int(rows[0][0]) if rows else 0

    def claim_available_jobs(
        self,
        worker_id: str,
        limit: int,
        *,
        auto_engine_only: bool = False,
        exclude_auto_engine: bool = False,
        recovery_only: bool = False,
        only_video_id: int | None = None,
        lease_seconds: int = 600,
        allowed_auto_engine_profile_ids: Optional[Iterable[int]] = None,
    ) -> List[Dict[str, Any]]:
        """Atomically lease upload jobs to one worker.

        BEGIN IMMEDIATE serializes the small claim transaction across the manual
        queue, Auto Engine, and accidental second processes. One channel can have
        at most one live claim at a time.
        """
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        self.ensure_pending_upload_identities(
            auto_engine_only=auto_engine_only,
            exclude_auto_engine=exclude_auto_engine,
            recovery_only=recovery_only,
        )
        wanted = 1  # LUM_GLOBAL_SINGLE_UPLOAD_V1: repository-level hard limit
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        expiry_text = (now + timedelta(seconds=max(60, int(lease_seconds)))).isoformat(timespec="seconds")
        selected_ids: List[int] = []
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE videos
                SET claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    status=CASE WHEN status='CLAIMED' THEN 'QUEUED' ELSE status END
                WHERE lease_expires_at IS NOT NULL AND lease_expires_at<=?
                """,
                (now_text,),
            )
            active_channel_rows = conn.execute(
                """
                SELECT DISTINCT channel_db_id FROM videos
                WHERE (
                    (claim_worker_id IS NOT NULL AND lease_expires_at>?)
                    OR status IN ('UPLOADING','THUMBNAIL_UPLOADING','WAITING_NETWORK')
                )
                """,
                (now_text,),
            ).fetchall()
            active_channels = {int(row["channel_db_id"] or 0) for row in active_channel_rows}
            # LUM_GLOBAL_SINGLE_UPLOAD_V1: one actual upload globally. WAITING_NETWORK
            # is intentionally excluded so a dead proxy group cannot stall healthy groups.
            active_global = conn.execute(
                """
                SELECT id FROM videos
                WHERE (
                    (claim_worker_id IS NOT NULL AND lease_expires_at>? AND status='CLAIMED')
                    OR status IN ('UPLOADING','THUMBNAIL_UPLOADING')
                )
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if active_global:
                conn.commit()
                return []
            query = "SELECT id, channel_db_id FROM videos WHERE status IN ('QUEUED','WAITING_SLOT') AND (claim_worker_id IS NULL OR lease_expires_at<=?)"
            params: List[Any] = [now_text]
            if only_video_id is not None:
                if int(only_video_id) <= 0:
                    raise ValueError("Valid core video ID required")
                query += " AND id=?"
                params.append(int(only_video_id))
            if auto_engine_only:
                query += " AND auto_engine_profile_id IS NOT NULL"
            elif exclude_auto_engine:
                query += " AND auto_engine_profile_id IS NULL"
            if allowed_auto_engine_profile_ids is not None:
                # Applies only to explicitly scoped Short Studio worker claims.
                # Empty is fail-closed: no disabled or other-profile job can be leased.
                allowed = sorted({int(pid) for pid in allowed_auto_engine_profile_ids if int(pid)>0})
                if allowed:
                    query += " AND auto_engine_profile_id IN (" + ",".join("?" for _ in allowed) + ")"
                    params.extend(allowed)
                else:
                    query += " AND 1=0"
            if recovery_only:
                query += " AND recovery_state='AUTO_RESUME'"
            query += " ORDER BY COALESCE(NULLIF(app_upload_at,''), created_at) ASC, normalized_filename ASC, id ASC"
            for row in conn.execute(query, params).fetchall():
                channel_key = int(row["channel_db_id"] or 0)
                if channel_key in active_channels:
                    continue
                selected_ids.append(int(row["id"]))
                active_channels.add(channel_key)
                if len(selected_ids) >= wanted:
                    break
            for video_id in selected_ids:
                cur = conn.execute(
                    """
                    UPDATE videos
                    SET status='CLAIMED', claim_worker_id=?, claimed_at=?, lease_expires_at=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status IN ('QUEUED','WAITING_SLOT')
                    """,
                    (worker_id, now_text, expiry_text, video_id),
                )
                if cur.rowcount:
                    self._set_ledger_state(conn, video_id, "CLAIMED")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        jobs: List[Dict[str, Any]] = []
        for video_id in selected_ids:
            row = self.get_video(video_id)
            if row and str(row.get("claim_worker_id") or "") == worker_id:
                jobs.append(row)
        return jobs

    def renew_claim(self, video_id: int, worker_id: str, lease_seconds: int = 600) -> bool:
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=max(60, int(lease_seconds)))).isoformat(timespec="seconds")
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE videos SET lease_expires_at=? WHERE id=? AND claim_worker_id=?",
                (expiry, int(video_id), str(worker_id)),
            )
            return bool(cur.rowcount)

    def record_upload_progress(
        self,
        video_id: int,
        worker_id: str,
        progress: int,
        message: str,
        *,
        level: str = "INFO",
        replace_log_prefix: str | None = None,
        lease_seconds: int = 600,
    ) -> bool:
        """Renew the lease, update progress, and write the log atomically.

        Returning ``False`` means the caller no longer owns the job. No partial
        progress or log write is committed in that case.
        """

        worker = str(worker_id or "").strip()
        if not worker:
            raise ValueError("worker_id is required")
        safe_progress = max(0, min(100, int(progress)))
        expiry = (
            datetime.now(timezone.utc) + timedelta(seconds=max(60, int(lease_seconds)))
        ).isoformat(timespec="seconds")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE videos
                SET lease_expires_at=?, progress=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND claim_worker_id=?
                """,
                (expiry, safe_progress, int(video_id), worker),
            )
            if not cur.rowcount:
                conn.rollback()
                return False

            if replace_log_prefix:
                row = conn.execute(
                    """
                    SELECT id FROM upload_logs
                    WHERE video_id=? AND level=? AND message LIKE ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (int(video_id), str(level), f"{replace_log_prefix}%"),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE upload_logs SET message=?, progress=?, created_at=CURRENT_TIMESTAMP WHERE id=?",
                        (str(message), safe_progress, int(row["id"])),
                    )
                else:
                    conn.execute(
                        "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                        (int(video_id), str(level), str(message), safe_progress),
                    )
            else:
                conn.execute(
                    "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                    (int(video_id), str(level), str(message), safe_progress),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


    def update_pipeline_checkpoint(
        self,
        video_id: int,
        *,
        worker_id: str | None = None,
        pipeline_stage: str | None = None,
        media_stage_state: str | None = None,
        thumbnail_stage_state: str | None = None,
        playlist_stage_state: str | None = None,
        post_action_stage_state: str | None = None,
        warning: str | None = None,
    ) -> bool:
        """Persist one pipeline checkpoint atomically and ownership-safely."""

        valid_stage_states = {"PENDING", "RUNNING", "SUCCEEDED", "SKIPPED", "WARNING"}
        state_values = {
            "media_stage_state": media_stage_state,
            "thumbnail_stage_state": thumbnail_stage_state,
            "playlist_stage_state": playlist_stage_state,
            "post_action_stage_state": post_action_stage_state,
        }
        assignments = ["updated_at=CURRENT_TIMESTAMP"]
        params: list[Any] = []
        if pipeline_stage is not None:
            stage = str(pipeline_stage or "PENDING").strip().upper()
            if stage not in {"PENDING", "PREPARE", "MEDIA", "THUMBNAIL", "PLAYLIST", "POST_ACTION", "FINALIZE", "COMPLETE"}:
                raise ValueError(f"Unknown upload pipeline stage: {pipeline_stage!r}")
            assignments.append("pipeline_stage=?")
            params.append(stage)
        for column, value in state_values.items():
            if value is None:
                continue
            state = str(value).strip().upper()
            if state not in valid_stage_states:
                raise ValueError(f"Unknown upload stage state for {column}: {value!r}")
            assignments.append(f"{column}=?")
            params.append(state)

        owner = str(worker_id or "").strip()
        where = "id=?"
        where_params: list[Any] = [int(video_id)]
        if owner:
            where += " AND claim_worker_id=?"
            where_params.append(owner)

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if warning:
                row = conn.execute(
                    f"SELECT pipeline_warnings_json FROM videos WHERE {where}",
                    where_params,
                ).fetchone()
                if not row:
                    conn.rollback()
                    return False
                try:
                    warnings = json.loads(str(row["pipeline_warnings_json"] or "[]"))
                except Exception:
                    warnings = []
                if not isinstance(warnings, list):
                    warnings = []
                warning_text = str(warning).strip()
                if warning_text and warning_text not in warnings:
                    warnings.append(warning_text)
                assignments.append("pipeline_warnings_json=?")
                params.append(json.dumps(warnings, ensure_ascii=False))
            params.extend(where_params)
            cur = conn.execute(
                f"UPDATE videos SET {', '.join(assignments)} WHERE {where}",
                params,
            )
            if not cur.rowcount:
                conn.rollback()
                return False
            if media_stage_state is not None:
                media_state = str(media_stage_state).strip().upper()
                if media_state == "RUNNING":
                    self._set_ledger_state(conn, int(video_id), "MEDIA_IN_PROGRESS")
                elif media_state == "SUCCEEDED":
                    identity = conn.execute(
                        "SELECT youtube_video_id, youtube_url FROM videos WHERE id=?",
                        (int(video_id),),
                    ).fetchone()
                    if identity and str(identity["youtube_video_id"] or "").strip():
                        self._set_ledger_state(
                            conn,
                            int(video_id),
                            "REMOTE_CREATED",
                            youtube_video_id=str(identity["youtube_video_id"] or ""),
                            youtube_url=str(identity["youtube_url"] or ""),
                        )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete_pipeline_job(
        self,
        video_id: int,
        worker_id: str,
        *,
        status: UploadStatus | str,
        youtube_video_id: str,
        youtube_url: str,
    ) -> bool:
        """Finalize and release a claimed job in one ownership-safe transaction."""

        target = coerce_upload_status(status)
        if not str(youtube_video_id or "").strip():
            raise ValueError("youtube_video_id is required to complete an upload pipeline")
        if target not in {UploadStatus.UPLOADED, UploadStatus.PUBLISHED}:
            raise ValueError(f"Invalid pipeline completion status: {target.value}")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, claim_worker_id FROM videos WHERE id=?",
                (int(video_id),),
            ).fetchone()
            if not row or str(row["claim_worker_id"] or "") != str(worker_id):
                conn.rollback()
                return False
            validate_status_transition(str(row["status"]), target)
            conn.execute(
                """
                UPDATE videos
                SET status=?, progress=100, youtube_video_id=?, youtube_url=?,
                    error_message='', pipeline_stage='COMPLETE', recovery_state='NONE',
                    claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND claim_worker_id=?
                """,
                (target.value, str(youtube_video_id), str(youtube_url), int(video_id), str(worker_id)),
            )
            self._set_ledger_state(
                conn,
                int(video_id),
                "COMPLETED",
                youtube_video_id=str(youtube_video_id),
                youtube_url=str(youtube_url),
                last_error="",
            )
            completed_row = conn.execute("""
                SELECT c.channel_id, v.original_filename, v.normalized_filename
                FROM videos v JOIN channels c ON c.id=v.channel_db_id WHERE v.id=?
            """, (int(video_id),)).fetchone()
            if not completed_row or not str(completed_row["normalized_filename"] or "").strip():
                raise RuntimeError("Completed upload is missing normalized filename identity")
            conn.execute("""
                INSERT INTO successful_filename_uploads(
                    channel_id, original_filename, normalized_filename, uploaded_at, youtube_video_id
                ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(channel_id, normalized_filename) DO UPDATE SET
                    youtube_video_id=COALESCE(successful_filename_uploads.youtube_video_id, excluded.youtube_video_id)
            """, (completed_row["channel_id"], completed_row["original_filename"],
                    completed_row["normalized_filename"], str(youtube_video_id)))
            conn.execute(
                "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'INFO', ?, 100)",
                (int(video_id), f"Done: {target.value}"),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_pipeline_checkpoints(self, video_id: int, *, clear_remote_identity: bool = False) -> None:
        """Reset stage checkpoints for an intentional fresh re-run of a queue row."""

        assignments = [
            "pipeline_stage='PENDING'",
            "media_stage_state='PENDING'",
            "thumbnail_stage_state='PENDING'",
            "playlist_stage_state='PENDING'",
            "post_action_stage_state='PENDING'",
            "pipeline_warnings_json='[]'",
            "updated_at=CURRENT_TIMESTAMP",
        ]
        if clear_remote_identity:
            assignments.extend([
                "youtube_video_id=NULL",
                "youtube_url=NULL",
                "resumable_uri=NULL",
                "resumable_file_size=NULL",
                "resumable_file_mtime_ns=NULL",
                "upload_bytes_confirmed=0",
            ])
        with self.connect() as conn:
            conn.execute(
                f"UPDATE videos SET {', '.join(assignments)} WHERE id=?",
                (int(video_id),),
            )

    def fail_claimed_job(self, video_id: int, worker_id: str, message: str) -> bool:
        """Fail a job while preserving duplicate-safety evidence."""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM videos WHERE id=? AND claim_worker_id=?",
                (int(video_id), str(worker_id)),
            ).fetchone()
            if not row:
                return False
            _current, target = validate_status_transition(row["status"], UploadStatus.FAILED)
            ledger_state = self._ledger_state_for_row(row, failed=True)
            recovery_state = "REVIEW_REQUIRED" if ledger_state == "AMBIGUOUS" else "NONE"
            cur = conn.execute(
                """
                UPDATE videos
                SET status=?, progress=0, error_message=?, retry_count=retry_count+1,
                    recovery_state=?, claim_worker_id=NULL, claimed_at=NULL,
                    lease_expires_at=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND claim_worker_id=?
                """,
                (target.value, str(message), recovery_state, int(video_id), str(worker_id)),
            )
            if cur.rowcount:
                self._set_ledger_state(
                    conn,
                    int(video_id),
                    ledger_state,
                    youtube_video_id=str(row["youtube_video_id"] or "") or None,
                    youtube_url=str(row["youtube_url"] or "") or None,
                    last_error=str(message),
                )
                conn.execute(
                    "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'ERROR', ?, 0)",
                    (int(video_id), str(message)),
                )
                if recovery_state == "REVIEW_REQUIRED":
                    conn.execute(
                        "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'WARNING', ?, 0)",
                        (
                            int(video_id),
                            "Duplicate protection quarantined this job: confirm in YouTube Studio that no remote video exists before forcing a fresh retry.",
                        ),
                    )
            return bool(cur.rowcount)

    def release_claim(self, video_id: int, worker_id: str, *, requeue_if_claimed: bool = False) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM videos WHERE id=? AND claim_worker_id=?",
                (int(video_id), str(worker_id)),
            ).fetchone()
            cur = conn.execute(
                """
                UPDATE videos
                SET claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    status=CASE WHEN ?=1 AND status='CLAIMED' THEN 'QUEUED' ELSE status END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND claim_worker_id=?
                """,
                (1 if requeue_if_claimed else 0, int(video_id), str(worker_id)),
            )
            if cur.rowcount and row and str(row["status"] or "") == "CLAIMED":
                self._set_ledger_state(conn, int(video_id), "RESERVED")

    def release_worker_claims(self, worker_id: str, *, requeue_claimed: bool = True) -> int:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, status FROM videos WHERE claim_worker_id=?",
                (str(worker_id),),
            ).fetchall()
            cur = conn.execute(
                """
                UPDATE videos
                SET claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    status=CASE WHEN ?=1 AND status='CLAIMED' THEN 'QUEUED' ELSE status END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE claim_worker_id=?
                """,
                (1 if requeue_claimed else 0, str(worker_id)),
            )
            for row in rows:
                if str(row["status"] or "") == "CLAIMED":
                    self._set_ledger_state(conn, int(row["id"]), "RESERVED")
            return int(cur.rowcount)

    def queued_work_exists(self, *, auto_engine_only: bool = False, exclude_auto_engine: bool = False, recovery_only: bool = False, only_video_id: int | None = None) -> bool:
        query = "SELECT 1 FROM videos WHERE status IN ('QUEUED','WAITING_SLOT','CLAIMED','UPLOADING','WAITING_NETWORK','THUMBNAIL_UPLOADING')"
        if auto_engine_only:
            query += " AND auto_engine_profile_id IS NOT NULL"
        elif exclude_auto_engine:
            query += " AND auto_engine_profile_id IS NULL"
        if recovery_only:
            query += " AND recovery_state='AUTO_RESUME'"
        params = ()
        if only_video_id is not None:
            query += " AND id=?"
            params = (int(only_video_id),)
        query += " LIMIT 1"
        with self.connect() as conn:
            return bool(conn.execute(query, params).fetchone())

    def save_resumable_state(
        self, video_id: int, uri: str, file_size: int, file_mtime_ns: int, confirmed_bytes: int = 0
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE videos SET resumable_uri=?, resumable_file_size=?, resumable_file_mtime_ns=?,
                    upload_bytes_confirmed=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (str(uri or ""), int(file_size), int(file_mtime_ns), max(0, int(confirmed_bytes or 0)), int(video_id)),
            )
            if str(uri or "").strip():
                self._set_ledger_state(conn, int(video_id), "RESUMABLE")

    def clear_resumable_state(self, video_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE videos SET resumable_uri=NULL, resumable_file_size=NULL, resumable_file_mtime_ns=NULL,
                    upload_bytes_confirmed=0, updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (int(video_id),),
            )

    def queue_failed_for_retry(
        self,
        auto_engine_only: bool = False,
        exclude_auto_engine: bool = False,
        video_ids: Optional[Iterable[int]] = None,
    ) -> int:
        where = (
            "status='FAILED' AND recovery_state<>'REVIEW_REQUIRED' "
            "AND EXISTS (SELECT 1 FROM channels c "
            "WHERE c.id=videos.channel_db_id AND c.status='ACTIVE')"
        )
        params: list[Any] = []
        if auto_engine_only:
            where += " AND auto_engine_profile_id IS NOT NULL"
        elif exclude_auto_engine:
            where += " AND auto_engine_profile_id IS NULL"
        if video_ids is not None:
            retry_ids = sorted({int(value) for value in video_ids})
            if not retry_ids:
                return 0
            where += f" AND id IN ({','.join('?' for _ in retry_ids)})"
            params.extend(retry_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, resumable_uri, youtube_video_id, youtube_url
                FROM videos WHERE {where}
                """,
                params,
            ).fetchall()
            cur = conn.execute(
                f"""
                UPDATE videos SET status='QUEUED', claim_worker_id=NULL, claimed_at=NULL,
                    lease_expires_at=NULL, error_message=NULL, progress=0,
                    recovery_state='NONE', updated_at=CURRENT_TIMESTAMP WHERE {where}
                """,
                params,
            )
            for row in rows:
                remote_id = str(row["youtube_video_id"] or "").strip()
                remote_url = str(row["youtube_url"] or "").strip()
                if remote_id:
                    ledger_state = "REMOTE_CREATED"
                elif str(row["resumable_uri"] or "").strip():
                    ledger_state = "RESUMABLE"
                else:
                    ledger_state = "RESERVED"
                self._set_ledger_state(
                    conn,
                    int(row["id"]),
                    ledger_state,
                    youtube_video_id=remote_id or None,
                    youtube_url=remote_url or None,
                    last_error="",
                )
            return int(cur.rowcount)

    def schedule_channel_retry(self, video_id: int, delay_seconds: int = 300, *, thumbnail_only: bool = False, message: str = "Automatic retry scheduled") -> bool:
        """Schedule one safe retry while preserving any remote YouTube identity.

        This is intentionally narrow for channel-based Auto Engine jobs. If a YouTube
        ID already exists, media_stage_state stays SUCCEEDED so the pipeline resumes
        at the unfinished stage instead of uploading media again.
        """
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=max(60, int(delay_seconds)))).isoformat(timespec="seconds")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id=?", (int(video_id),)).fetchone()
            if not row:
                return False
            remote_id = str(row["youtube_video_id"] or "").strip()
            assignments = [
                "status='SCHEDULED'", "app_upload_at=?", "claim_worker_id=NULL", "claimed_at=NULL",
                "lease_expires_at=NULL", "error_message=?", "recovery_state='NONE'",
                "updated_at=CURRENT_TIMESTAMP"
            ]
            params: list[Any] = [retry_at, str(message)]
            if thumbnail_only:
                if not remote_id:
                    raise ValueError("Thumbnail-only retry requires an existing YouTube video ID")
                assignments.extend([
                    "pipeline_stage='THUMBNAIL'", "media_stage_state='SUCCEEDED'",
                    "thumbnail_stage_state='PENDING'", "progress=93"
                ])
            else:
                assignments.append("progress=0")
            params.append(int(video_id))
            conn.execute(f"UPDATE videos SET {', '.join(assignments)} WHERE id=?", params)
            self._set_ledger_state(
                conn, int(video_id), "REMOTE_CREATED" if remote_id else ("RESUMABLE" if str(row["resumable_uri"] or "").strip() else "RESERVED"),
                youtube_video_id=remote_id or None, youtube_url=str(row["youtube_url"] or "").strip() or None, last_error=str(message),
            )
            conn.execute("INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'WARNING', ?, ?)",
                         (int(video_id), str(message), 93 if thumbnail_only else 0))
            return True

    def mark_channel_post_upload_failed(self, video_id: int, message: str) -> bool:
        """Put a post-media channel job in manual-retry state without losing remote identity."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id=?", (int(video_id),)).fetchone()
            if not row:
                return False
            remote_id = str(row["youtube_video_id"] or "").strip()
            conn.execute(
                """
                UPDATE videos SET status='FAILED', claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    error_message=?, recovery_state='NONE', pipeline_stage=CASE WHEN ?<>'' THEN 'THUMBNAIL' ELSE pipeline_stage END,
                    progress=CASE WHEN ?<>'' THEN 93 ELSE 0 END, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (str(message), remote_id, remote_id, int(video_id)),
            )
            self._set_ledger_state(
                conn, int(video_id), "REMOTE_CREATED" if remote_id else self._ledger_state_for_row(row, failed=True),
                youtube_video_id=remote_id or None, youtube_url=str(row["youtube_url"] or "").strip() or None, last_error=str(message),
            )
            conn.execute("INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'ERROR', ?, ?)",
                         (int(video_id), str(message), 93 if remote_id else 0))
            return True

    def recover_interrupted_uploads(self, auto_resume_safe: bool = False) -> int:
        """Recover jobs left active by a process crash.

        With ``auto_resume_safe=False`` the legacy/manual behavior is preserved: all
        interrupted rows become PAUSED. Startup uses ``True`` and re-queues only jobs
        whose persisted evidence proves that continuing cannot create a second media
        upload. Ambiguous media-stage crashes are quarantined for explicit review.
        """

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM videos
                WHERE status IN ('CLAIMED','UPLOADING','THUMBNAIL_UPLOADING','WAITING_NETWORK')
                ORDER BY id
                """
            ).fetchall()
            if not rows:
                conn.execute(
                    """
                    INSERT INTO app_meta(key, value) VALUES ('last_recovery_summary', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (json.dumps({"total": 0, "auto_resumed": 0, "review_required": 0}),),
                )
                return 0

            auto_resumed = 0
            review_required = 0
            for row in rows:
                video_id = int(row["id"])
                remote_id = str(row["youtube_video_id"] or "").strip()
                resumable_uri = str(row["resumable_uri"] or "").strip()
                media_state = str(row["media_stage_state"] or "PENDING").upper()
                pipeline_stage = str(row["pipeline_stage"] or "PENDING").upper()
                safe_before_media = media_state == "PENDING" and pipeline_stage in {"PENDING", "PREPARE"}
                safe_to_resume = bool(remote_id or resumable_uri or safe_before_media)

                if auto_resume_safe and safe_to_resume:
                    auto_resumed += 1
                    message = (
                        "Crash recovery: job safely re-queued for automatic resume. "
                        "Remote identity/resumable state was preserved."
                    )
                    conn.execute(
                        """
                        UPDATE videos SET status='QUEUED', recovery_state='AUTO_RESUME',
                            claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                            error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (message, video_id),
                    )
                    ledger_state = "REMOTE_CREATED" if remote_id else ("RESUMABLE" if resumable_uri else "RESERVED")
                    self._set_ledger_state(
                        conn,
                        video_id,
                        ledger_state,
                        youtube_video_id=remote_id or None,
                        youtube_url=str(row["youtube_url"] or "") or None,
                    )
                    conn.execute(
                        "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'INFO', ?, ?)",
                        (video_id, message, int(row["progress"] or 0)),
                    )
                else:
                    is_ambiguous = bool(auto_resume_safe and not safe_to_resume)
                    if is_ambiguous:
                        review_required += 1
                        message = (
                            "REVIEW REQUIRED: the app stopped inside the media-upload stage before a resumable session or YouTube ID was safely stored. "
                            "Automatic retry is blocked to prevent a possible duplicate. Check YouTube Studio first."
                        )
                        recovery_state = "REVIEW_REQUIRED"
                        ledger_state = "AMBIGUOUS"
                    else:
                        message = (
                            "App closed while this job was in progress. Review it, then click Resume Paused if it was not uploaded."
                        )
                        recovery_state = "NONE"
                        ledger_state = self._ledger_state_for_row(row, failed=False)
                        if ledger_state == "MEDIA_IN_PROGRESS":
                            ledger_state = "AMBIGUOUS"
                    conn.execute(
                        """
                        UPDATE videos SET status='PAUSED', recovery_state=?,
                            claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                            error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (recovery_state, message, video_id),
                    )
                    self._set_ledger_state(
                        conn,
                        video_id,
                        ledger_state,
                        youtube_video_id=remote_id or None,
                        youtube_url=str(row["youtube_url"] or "") or None,
                        last_error=message,
                    )
                    conn.execute(
                        "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'WARNING', ?, ?)",
                        (video_id, message, int(row["progress"] or 0)),
                    )

            summary = {
                "total": len(rows),
                "auto_resumed": auto_resumed,
                "review_required": review_required,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            conn.execute(
                """
                INSERT INTO app_meta(key, value) VALUES ('last_recovery_summary', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (json.dumps(summary),),
            )
            return len(rows)

    def recovery_summary(self) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key='last_recovery_summary'").fetchone()
            try:
                summary = json.loads(str(row["value"] or "{}")) if row else {}
            except Exception:
                summary = {}
            summary["auto_resume_pending"] = int(conn.execute(
                "SELECT COUNT(*) FROM videos WHERE recovery_state='AUTO_RESUME'"
            ).fetchone()[0])
            summary["review_required"] = int(conn.execute(
                "SELECT COUNT(*) FROM videos WHERE recovery_state='REVIEW_REQUIRED'"
            ).fetchone()[0])
            return summary

    def recovery_jobs_exist(self, *, auto_engine_only: bool = False, exclude_auto_engine: bool = False) -> bool:
        query = "SELECT 1 FROM videos WHERE recovery_state='AUTO_RESUME' AND status IN ('QUEUED','WAITING_SLOT')"
        if auto_engine_only:
            query += " AND auto_engine_profile_id IS NOT NULL"
        elif exclude_auto_engine:
            query += " AND auto_engine_profile_id IS NULL"
        query += " LIMIT 1"
        with self.connect() as conn:
            return bool(conn.execute(query).fetchone())

    def pause_active(self) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE videos SET status='PAUSED', updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('UPLOADING','THUMBNAIL_UPLOADING','WAITING_NETWORK')
                """
            )
            return int(cur.rowcount)

    def resume_paused(self, auto_engine_only: bool = False, exclude_auto_engine: bool = False,
                      video_ids: Optional[Iterable[int]] = None) -> int:
        where = "status='PAUSED' AND recovery_state<>'REVIEW_REQUIRED'"
        params: list[Any] = []
        if auto_engine_only:
            where += " AND auto_engine_profile_id IS NOT NULL"
        elif exclude_auto_engine:
            where += " AND auto_engine_profile_id IS NULL"
        if video_ids is not None:
            selected = sorted({int(value) for value in video_ids if int(value)>0})
            if not selected:
                return 0
            where += " AND id IN (" + ",".join("?" for _ in selected) + ")"
            params.extend(selected)
        with self.connect() as conn:
            rows = conn.execute(f"SELECT id, resumable_uri FROM videos WHERE {where}",params).fetchall()
            cur = conn.execute(
                f"""
                UPDATE videos SET status='QUEUED', claim_worker_id=NULL, claimed_at=NULL,
                    lease_expires_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE {where}
                """,params
            )
            for row in rows:
                self._set_ledger_state(
                    conn,
                    int(row["id"]),
                    "RESUMABLE" if str(row["resumable_uri"] or "").strip() else "RESERVED",
                )
            return int(cur.rowcount)

    def resolve_recovery_review(self, video_ids: Iterable[int]) -> int:
        """User-confirmed escape hatch after checking YouTube Studio."""

        ids = sorted({int(value) for value in video_ids if int(value) > 0})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id FROM videos WHERE id IN ({placeholders})
                  AND status='PAUSED' AND recovery_state='REVIEW_REQUIRED'
                """,
                ids,
            ).fetchall()
            resolved = [int(row["id"]) for row in rows]
            if not resolved:
                return 0
            marks = ",".join("?" for _ in resolved)
            conn.execute(
                f"""
                UPDATE videos SET status='QUEUED', recovery_state='NONE', progress=0,
                    error_message='User confirmed no remote upload exists; fresh retry authorized.',
                    claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    resumable_uri=NULL, resumable_file_size=NULL,
                    resumable_file_mtime_ns=NULL, upload_bytes_confirmed=0,
                    media_stage_state='PENDING', pipeline_stage='PENDING',
                    updated_at=CURRENT_TIMESTAMP WHERE id IN ({marks})
                """,
                resolved,
            )
            for video_id in resolved:
                self._set_ledger_state(conn, video_id, "RESERVED", last_error="")
                conn.execute(
                    "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'WARNING', ?, 0)",
                    (video_id, "Recovery review cleared by user after checking YouTube Studio; fresh retry authorized."),
                )
            return len(resolved)

    def delete_auto_engine_jobs(self, video_ids: Iterable[int]) -> int:
        """Remove selected Auto Engine jobs only when they are not actively uploading."""
        ids = [int(v) for v in video_ids if v]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        safe_statuses = ("QUEUED", "SCHEDULED", "WAITING_SLOT", "PAUSED", "FAILED", "UPLOADED", "PUBLISHED")
        status_marks = ",".join("?" for _ in safe_statuses)
        params = ids + list(safe_statuses)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, channel_db_id, upload_fingerprint, youtube_video_id FROM videos
                WHERE id IN ({placeholders})
                  AND auto_engine_profile_id IS NOT NULL
                  AND status IN ({status_marks})
                """,
                params,
            ).fetchall()
            removable = [int(r["id"]) for r in rows]
            if not removable:
                return 0
            marks = ",".join("?" for _ in removable)
            for row in rows:
                fingerprint = str(row["upload_fingerprint"] or "").strip()
                if not fingerprint:
                    continue
                state = "COMPLETED" if str(row["youtube_video_id"] or "").strip() else "CANCELLED"
                conn.execute(
                    """
                    UPDATE upload_ledger SET state=?, video_id=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE channel_db_id=? AND upload_fingerprint=?
                    """,
                    (state, int(row["channel_db_id"]), fingerprint),
                )
            conn.execute(f"DELETE FROM upload_logs WHERE video_id IN ({marks})", removable)
            cur = conn.execute(f"DELETE FROM videos WHERE id IN ({marks})", removable)
            return int(cur.rowcount)

    def clear_completed_auto_engine_jobs(self) -> int:
        """Clear completed Auto Engine rows from the Auto Engine queue table; does not touch media files."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, channel_db_id, upload_fingerprint FROM videos WHERE auto_engine_profile_id IS NOT NULL AND status IN ('UPLOADED','PUBLISHED')"
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            for row in rows:
                fingerprint = str(row["upload_fingerprint"] or "").strip()
                if fingerprint:
                    conn.execute(
                        """
                        UPDATE upload_ledger SET state='COMPLETED', video_id=NULL,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE channel_db_id=? AND upload_fingerprint=?
                        """,
                        (int(row["channel_db_id"]), fingerprint),
                    )
            conn.execute(f"DELETE FROM upload_logs WHERE video_id IN ({placeholders})", ids)
            cur = conn.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", ids)
            return int(cur.rowcount)

    def upload_safety_stats(self) -> Dict[str, Any]:
        with self.connect() as conn:
            ledger_rows = conn.execute(
                "SELECT state, COUNT(*) AS total FROM upload_ledger GROUP BY state"
            ).fetchall()
            blocked = conn.execute(
                "SELECT value FROM app_meta WHERE key='duplicate_uploads_blocked'"
            ).fetchone()
            return {
                "ledger": {str(row["state"]): int(row["total"]) for row in ledger_rows},
                "duplicate_uploads_blocked": int(blocked["value"] or 0) if blocked else 0,
                "review_required": int(conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE recovery_state='REVIEW_REQUIRED'"
                ).fetchone()[0]),
                "auto_resume_pending": int(conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE recovery_state='AUTO_RESUME'"
                ).fetchone()[0]),
                "unprotected_pending": int(conn.execute(
                    """
                    SELECT COUNT(*) FROM videos
                    WHERE status IN ('QUEUED','WAITING_SLOT','SCHEDULED','PAUSED','FAILED')
                      AND (upload_fingerprint IS NULL OR upload_fingerprint='')
                    """
                ).fetchone()[0]),
            }

    def add_log(self, video_id: int | None, level: str, message: str, progress: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                (video_id, level, message, progress),
            )

    def clear_logs(self) -> int:
        """Start a new app session with an empty runtime log stream."""
        with self.connect() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM upload_logs").fetchone()[0])
            conn.execute("DELETE FROM upload_logs")
            return count

    def upsert_progress_log(
        self,
        video_id: int | None,
        level: str,
        message_prefix: str,
        message: str,
        progress: int | None = None,
    ) -> None:
        """Keep a live progress log as one row instead of creating hundreds of rows.

        The Logs tab refreshes from SQLite, so updating the latest matching row makes
        the visible percentage change from digit to digit on the same line.
        """
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM upload_logs
                WHERE video_id IS ? AND level=? AND message LIKE ?
                ORDER BY id DESC LIMIT 1
                """,
                (video_id, level, f"{message_prefix}%"),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE upload_logs SET message=?, progress=?, created_at=CURRENT_TIMESTAMP WHERE id=?",
                    (message, progress, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                    (video_id, level, message, progress),
                )

    def list_logs(self, video_id: int | None = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            if video_id:
                rows = conn.execute(
                    "SELECT * FROM upload_logs WHERE video_id=? ORDER BY id DESC LIMIT ?",
                    (video_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM upload_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
