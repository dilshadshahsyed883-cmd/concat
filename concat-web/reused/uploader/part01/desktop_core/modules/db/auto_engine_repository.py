from __future__ import annotations

from typing import Any, Dict, List, Optional

from .repository_base import BaseRepository


class AutoEngineRepository(BaseRepository):
    def list_auto_engine_profiles(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*, g.group_name, COUNT(m.channel_db_id) AS group_channel_count
                FROM auto_engine_profiles p
                LEFT JOIN channel_groups g ON g.id = p.channel_group_id
                LEFT JOIN channel_group_members m ON m.group_id = p.channel_group_id
                GROUP BY p.id
                ORDER BY p.category_name COLLATE NOCASE
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_auto_engine_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, g.group_name, COUNT(m.channel_db_id) AS group_channel_count
                FROM auto_engine_profiles p
                LEFT JOIN channel_groups g ON g.id = p.channel_group_id
                LEFT JOIN channel_group_members m ON m.group_id = p.channel_group_id
                WHERE p.id=?
                GROUP BY p.id
                """,
                (int(profile_id),),
            ).fetchone()
            return dict(row) if row else None

    def save_auto_engine_profile(self, data: Dict[str, Any], profile_id: int | None = None) -> int:
        name = str(data.get("category_name") or "").strip()
        if not name:
            raise ValueError("Category name is required.")

        def text(key: str, default: str = "") -> str:
            return str(data.get(key) if data.get(key) is not None else default).strip()

        def integer(key: str, default: int = 0) -> int:
            try:
                return int(data.get(key))
            except Exception:
                return int(default)

        group_value = data.get("channel_group_id")
        channel_group_id = None
        if group_value not in (None, "", 0, "0"):
            try:
                channel_group_id = int(group_value)
            except Exception:
                channel_group_id = None

        work_mode = text("work_mode", "manual_review")
        if work_mode not in {"manual_review", "auto", "manual"}:
            work_mode = "manual_review"
        upload_scope = text("upload_scope", "per_channel")
        if upload_scope not in {"per_channel", "total_category"}:
            upload_scope = "per_channel"
        after_upload_action = text("after_upload_action", "move_uploaded")
        if after_upload_action not in {"move_uploaded", "keep", "delete"}:
            after_upload_action = "move_uploaded"

        payload = {
            "category_name": name,
            "video_folder": text("video_folder"),
            "thumbnail_folder": text("thumbnail_folder"),
            "metadata_sheet_url_or_id": text("metadata_sheet_url_or_id"),
            "metadata_sheet_tab": text("metadata_sheet_tab"),
            "metadata_sheet_gid": text("metadata_sheet_gid"),
            "channel_group_id": channel_group_id,
            "daily_upload_count": max(1, min(100, integer("daily_upload_count", 1))),
            "schedule_times": text("schedule_times", "09:00") or "09:00",
            "schedule_clock_type": text("schedule_clock_type", "12h") if text("schedule_clock_type", "12h") in {"12h", "24h"} else "12h",
            "upload_scope": upload_scope,
            "work_mode": work_mode,
            "after_upload_action": after_upload_action,
            "auto_select_video": 1 if bool(data.get("auto_select_video", True)) else 0,
            "thumbnail_rotation": 1 if bool(data.get("thumbnail_rotation", True)) else 0,
            "enabled": 1 if bool(data.get("enabled", True)) else 0,
            "schedule_enabled": 1 if work_mode == "auto" and bool(data.get("schedule_enabled", True)) else 0,
            "missed_schedule_policy": text("missed_schedule_policy", "run_missed_today") if text("missed_schedule_policy", "run_missed_today") in {"run_missed_today", "skip_missed", "ask"} else "run_missed_today",
            "notes": text("notes"),
        }
        keys = list(payload.keys())
        with self.connect() as conn:
            if profile_id:
                assignments = ", ".join(f"{k}=?" for k in keys) + ", updated_at=CURRENT_TIMESTAMP"
                conn.execute(
                    f"UPDATE auto_engine_profiles SET {assignments} WHERE id=?",
                    [payload[k] for k in keys] + [int(profile_id)],
                )
                return int(profile_id)
            placeholders = ",".join("?" for _ in keys)
            cur = conn.execute(
                f"INSERT INTO auto_engine_profiles({','.join(keys)}) VALUES ({placeholders})",
                [payload[k] for k in keys],
            )
            profile_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                (None, "INFO", f"Auto Engine profile created: {name}", None),
            )
            return profile_id

    def delete_auto_engine_profile(self, profile_id: int) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT category_name FROM auto_engine_profiles WHERE id=?", (int(profile_id),)).fetchone()
            name = str(row["category_name"]) if row else f"#{profile_id}"
            conn.execute("DELETE FROM auto_engine_profiles WHERE id=?", (int(profile_id),))
            conn.execute(
                "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                (None, "INFO", f"Auto Engine profile deleted: {name}", None),
            )

    def set_auto_engine_profile_enabled(self, profile_id: int, enabled: bool) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT category_name FROM auto_engine_profiles WHERE id=?", (int(profile_id),)).fetchone()
            if not row:
                raise ValueError(f"Auto Engine profile #{profile_id} was not found.")
            enabled_int = 1 if enabled else 0
            conn.execute(
                "UPDATE auto_engine_profiles SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (enabled_int, int(profile_id)),
            )
            state = "enabled" if enabled else "disabled"
            conn.execute(
                "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                (None, "INFO", f"Auto Engine profile {state}: {row['category_name']}", None),
            )

    def list_auto_engine_jobs(self, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._database.queue.list_videos(auto_engine_only=True)
        return rows[-int(limit):] if limit and len(rows) > int(limit) else rows

    def auto_engine_used_video_paths(self, profile_id: int) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT local_video_path FROM videos
                WHERE auto_engine_profile_id=?
                  AND status NOT IN ('FAILED')
                  AND local_video_path IS NOT NULL AND local_video_path!=''
                """,
                (int(profile_id),),
            ).fetchall()
            return {str(r["local_video_path"]).strip().lower() for r in rows if str(r["local_video_path"] or "").strip()}

    def count_auto_engine_jobs(self, profile_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM videos WHERE auto_engine_profile_id=?",
                (int(profile_id),),
            ).fetchone()
            return int(row["n"] if row else 0)

    def get_auto_engine_thumbnail_cursor(self, profile_id: int) -> int:
        """Return the persistent thumbnail rotation cursor for an Auto Engine profile."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT thumbnail_cursor FROM auto_engine_profiles WHERE id=?",
                (int(profile_id),),
            ).fetchone()
            if not row:
                return 0
            try:
                return max(0, int(row["thumbnail_cursor"] or 0))
            except Exception:
                return 0

    def advance_auto_engine_thumbnail_cursor(self, profile_id: int, increment: int) -> None:
        """Advance thumbnail rotation without depending on queue rows.

        This keeps rotation stable after Clear Done, app restart, or queue cleanup.
        """
        step = max(0, int(increment or 0))
        if step <= 0:
            return
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auto_engine_profiles
                SET thumbnail_cursor=COALESCE(thumbnail_cursor, 0) + ?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (step, int(profile_id)),
            )

    def auto_engine_run_exists(self, profile_id: int, run_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM auto_engine_runs WHERE profile_id=? AND run_key=?",
                (int(profile_id), str(run_key)),
            ).fetchone()
            return bool(row)

    def claim_auto_engine_run(self, profile_id: int, run_key: str) -> bool:
        """Atomically reserve one scheduled slot across windows/processes."""
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO auto_engine_runs(profile_id, run_key, jobs_created)
                VALUES (?, ?, -1)
                """,
                (int(profile_id), str(run_key)),
            )
            return int(cur.rowcount or 0) == 1

    def record_auto_engine_run(self, profile_id: int, run_key: str, jobs_created: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO auto_engine_runs(profile_id, run_key, jobs_created)
                VALUES (?, ?, ?)
                ON CONFLICT(profile_id, run_key) DO UPDATE SET
                    jobs_created=excluded.jobs_created
                """,
                (int(profile_id), str(run_key), int(jobs_created)),
            )
