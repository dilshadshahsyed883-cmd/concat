from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DEFAULT_SETTINGS, PROJECT_DB, STAGING_ROOT, ensure_dirs

ACTIVE_STATES = {"DOWNLOADING", "SOURCE_READY", "UPLOAD_QUEUED", "UPLOADING", "VERIFYING_REMOTE", "CLEANING"}
TERMINAL_STATES = {"COMPLETED", "CANCELLED"}
WAITING_STATES = {"QUEUED", "WAITING_SLOT", "SCHEDULED"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class ProjectStore:
    def __init__(self, path: str | Path = PROJECT_DB):
        ensure_dirs()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_username TEXT NOT NULL,
                    ui_mode TEXT NOT NULL DEFAULT 'simple',
                    source_mode TEXT NOT NULL DEFAULT 'drive_link',
                    drive_url TEXT NOT NULL,
                    staging_path TEXT,
                    thumbnail_path TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    category_id TEXT NOT NULL DEFAULT '22',
                    privacy_status TEXT NOT NULL DEFAULT 'private',
                    app_upload_at TEXT,
                    made_for_kids INTEGER NOT NULL DEFAULT 0,
                    playlist_id TEXT NOT NULL DEFAULT '',
                    channel_db_id INTEGER NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'QUEUED',
                    progress INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    core_video_id INTEGER,
                    youtube_video_id TEXT,
                    youtube_url TEXT,
                    remote_verified_at TEXT,
                    cleanup_verified_at TEXT,
                    source_file_name TEXT,
                    source_file_size INTEGER,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    source_origin TEXT NOT NULL DEFAULT '',
                    vps_folder TEXT NOT NULL DEFAULT '',
                    archived_at TEXT
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
            if "source_fingerprint" not in columns:
                conn.execute("ALTER TABLE projects ADD COLUMN source_fingerprint TEXT NOT NULL DEFAULT ''")
            if "publish_at" not in columns:
                conn.execute("ALTER TABLE projects ADD COLUMN publish_at TEXT NOT NULL DEFAULT ''")
            if "source_origin" not in columns:
                conn.execute("ALTER TABLE projects ADD COLUMN source_origin TEXT NOT NULL DEFAULT ''")
            if "vps_folder" not in columns:
                conn.execute("ALTER TABLE projects ADD COLUMN vps_folder TEXT NOT NULL DEFAULT ''")
            if "archived_at" not in columns:
                conn.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT")
            conn.execute("DROP INDEX IF EXISTS uq_copyright_owner_folder_claim")
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_copyright_owner_folder_claim
                ON projects(owner_username,vps_folder,source_file_name)
                WHERE source_origin='folder' AND status!='CANCELLED'
            """)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_copyright_source_channel ON projects(owner_username,channel_db_id,source_fingerprint) WHERE source_fingerprint != '' AND status != 'CANCELLED'")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS copyright_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_username TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    metadata_source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_username, profile_name)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lum_projects_status ON projects(status,id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lum_projects_owner ON projects(owner_username,id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_lum_project_core_video ON projects(core_video_id) WHERE core_video_id IS NOT NULL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER,
                    level TEXT NOT NULL DEFAULT 'INFO',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lum_events_project ON project_events(project_id,id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS copyright_owner_queues (
                owner_username TEXT PRIMARY KEY COLLATE NOCASE,
                paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
                scheduled_paused INTEGER NOT NULL DEFAULT 0 CHECK(scheduled_paused IN (0,1))
            )""")
            owner_columns = {row[1] for row in conn.execute("PRAGMA table_info(copyright_owner_queues)")}
            if "scheduled_paused" not in owner_columns:
                conn.execute("ALTER TABLE copyright_owner_queues ADD COLUMN scheduled_paused INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
            # Upgrade legacy OFF flags without starting Copyright scheduling.
            if os.getenv("LUM_ENGINE_INSTANCE") == "copyright":
                for key in ("sheets_sync_enabled", "auto_engine_enabled"):
                    conn.execute("UPDATE settings SET value='1' WHERE key=? AND value!='1'", (key,))

    def setting(self, key: str, default: str = "") -> str:
        if os.getenv("LUM_ENGINE_INSTANCE") == "copyright" and key in {"sheets_sync_enabled", "auto_engine_enabled"}:
            return "1"
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        if os.getenv("LUM_ENGINE_INSTANCE") == "copyright" and key in {"sheets_sync_enabled", "auto_engine_enabled"}:
            value = "1"
        with self.connect() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(key), str(value)))

    def settings(self) -> dict[str, str]:
        with self.connect() as conn:
            result = {str(r["key"]): str(r["value"]) for r in conn.execute("SELECT key,value FROM settings ORDER BY key")}
        if os.getenv("LUM_ENGINE_INSTANCE") == "copyright":
            result.update(sheets_sync_enabled="1", auto_engine_enabled="1")
        return result

    def owner_queue_paused(self, owner: str) -> bool:
        if not str(owner or "").strip():
            raise ValueError("Owner is required")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT paused FROM copyright_owner_queues WHERE owner_username=?",
                (owner.strip(),),
            ).fetchone()
        if row is None and os.getenv("LUM_ENGINE_INSTANCE") == "concat":
            return True  # No Concat queue is ever armed before the owner clicks Start.
        return bool(row and int(row["paused"]))

    def set_owner_queue_paused(self, owner: str, paused: bool) -> None:
        if not str(owner or "").strip():
            raise ValueError("Owner is required")
        with self.connect() as conn:
            conn.execute("""INSERT INTO copyright_owner_queues(owner_username,paused)
                VALUES(?,?) ON CONFLICT(owner_username)
                DO UPDATE SET paused=excluded.paused""",
                (owner.strip(), int(bool(paused))))

    def owner_scheduled_paused(self, owner: str) -> bool:
        if not str(owner or "").strip():
            raise ValueError("Owner is required")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT scheduled_paused FROM copyright_owner_queues WHERE owner_username=?",
                (owner.strip(),),
            ).fetchone()
        return bool(row and int(row["scheduled_paused"]))

    def set_owner_scheduled_paused(self, owner: str, paused: bool) -> None:
        if not str(owner or "").strip():
            raise ValueError("Owner is required")
        with self.connect() as conn:
            conn.execute("""INSERT INTO copyright_owner_queues(owner_username,paused,scheduled_paused)
                VALUES(?,1,?) ON CONFLICT(owner_username)
                DO UPDATE SET scheduled_paused=excluded.scheduled_paused""",
                (owner.strip(), int(bool(paused))))

    def edit_waiting_metadata(self, project_id: int, owner: str, *,
                              title: str, description: str, tags: str,
                              category_id: str, privacy_status: str) -> bool:
        """Edit only a still-unclaimed project owned by this account."""
        title, description, tags = title.strip(), str(description), str(tags)
        category_id = str(category_id).strip()
        privacy_status = str(privacy_status).strip().lower()
        if not title or len(title) > 100 or len(description) > 5000:
            raise ValueError("Title (1–100) and description (up to 5000) required")
        if len(tags) > 5000 or not category_id.isdigit() or not 1 <= int(category_id) <= 99:
            raise ValueError("Invalid tags or YouTube category")
        if privacy_status not in {"private", "unlisted", "public"}:
            raise ValueError("Invalid visibility")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if os.getenv("LUM_ENGINE_INSTANCE") == "concat":
                current = conn.execute(
                    "SELECT publish_at FROM projects WHERE id=? AND owner_username=? COLLATE NOCASE",
                    (int(project_id), owner),
                ).fetchone()
                if current and str(current["publish_at"] or "").strip() and privacy_status != "public":
                    raise ValueError("Scheduled Concat YouTube releases must remain Public; cancel and recreate to change the schedule")
            cursor = conn.execute("""
                UPDATE projects SET title=?,description=?,tags=?,category_id=?,
                    privacy_status=?,updated_at=?
                WHERE id=? AND owner_username=? COLLATE NOCASE
                  AND status IN ('QUEUED','WAITING_SLOT','SCHEDULED')
            """, (title, description, tags, category_id, privacy_status,
                  now_iso(), int(project_id), owner))
            changed = cursor.rowcount == 1
        if changed:
            self.event(project_id, "Waiting project metadata updated by owner")
        return changed

    def create_project(self, data: dict[str, Any]) -> int:
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("Title is required")
        if len(title) > 100:
            raise ValueError("Title must be 100 characters or fewer")
        description = str(data.get("description") or "")
        if len(description) > 5000:
            raise ValueError("Description must be 5000 characters or fewer")
        privacy = str(data.get("privacy_status") or "private").lower().strip()
        if privacy not in {"private", "public", "unlisted"}:
            raise ValueError("Invalid privacy status")
        drive_url = str(data.get("drive_url") or "").strip()
        source_mode = str(data.get("source_mode") or "drive_link")
        staging_path = str(data.get("staging_path") or "").strip()
        if source_mode == "local_file":
            path = Path(staging_path)
            if not staging_path or path.is_symlink() or not path.is_file() or path.stat().st_size <= 0 or STAGING_ROOT.resolve() not in path.resolve().parents:
                raise ValueError("Local video must be verified in isolated staging")
        elif source_mode == "drive_link":
            if not drive_url:
                raise ValueError("Google Drive video link is required")
        else:
            raise ValueError("Unknown video source mode")
        app_upload_at = str(data.get("app_upload_at") or "").strip() or None
        scheduled = parse_time(app_upload_at)
        status = "SCHEDULED" if scheduled and scheduled > datetime.now(timezone.utc) else "QUEUED"
        stamp = now_iso()
        values = (
            str(data.get("owner_username") or "").strip(), str(data.get("ui_mode") or "simple"),
            str(data.get("source_mode") or "drive_link"), drive_url, str(data.get("thumbnail_path") or "").strip() or None,
            title, description, str(data.get("tags") or ""), str(data.get("category_id") or "22"), privacy,
            app_upload_at, 1 if data.get("made_for_kids") else 0, str(data.get("playlist_id") or "").strip(),
            int(data.get("channel_db_id") or 0), str(data.get("group_name") or "").strip(), status, stamp, stamp,
            staging_path or None, str(data.get("source_file_name") or ""), int(data.get("source_file_size") or 0),
            str(data.get("source_fingerprint") or ""),
            str(data.get("source_origin") or ""),
            str(data.get("vps_folder") or ""),
            str(data.get("publish_at") or ""),
        )
        if not values[0]:
            raise ValueError("Project owner is required")
        if values[13] <= 0:
            raise ValueError("Channel is required")
        if values[-3] not in {"folder", "pc_upload", "drive_link", "concat_link", ""}:
            raise ValueError("Invalid source origin")
        if values[-3] == "folder" and not str(data.get("source_file_name") or ""):
            raise ValueError("Folder video name is required")
        with self.connect() as conn:
            try:
                cur = conn.execute("""
                INSERT INTO projects(owner_username,ui_mode,source_mode,drive_url,thumbnail_path,title,description,tags,
                    category_id,privacy_status,app_upload_at,made_for_kids,playlist_id,channel_db_id,group_name,status,created_at,updated_at,
                    staging_path,source_file_name,source_file_size,source_fingerprint,source_origin,vps_folder,publish_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, values)
            except sqlite3.IntegrityError as exc:
                raise ValueError("This video was already queued or uploaded to the selected channel") from exc
            project_id = int(cur.lastrowid)
            # Keep the manual queue paused on Concat admission. A timed job
            # uses an independent scheduled pause gate and starts when due.
            if os.getenv("LUM_ENGINE_INSTANCE") == "concat":
                conn.execute("""INSERT INTO copyright_owner_queues(owner_username,paused)
                    VALUES(?,1) ON CONFLICT(owner_username)
                    DO UPDATE SET paused=1""", (values[0],))
            conn.execute("INSERT INTO project_events(project_id,level,message,created_at) VALUES(?,?,?,?)", (project_id,"INFO","Project accepted into VPS queue",stamp))
            return project_id

    def get(self, project_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (int(project_id),)).fetchone()
        return dict(row) if row else None

    def list(self, owner: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        query = "SELECT * FROM projects WHERE archived_at IS NULL"
        params: list[Any] = []
        if owner:
            query += " AND owner_username=? COLLATE NOCASE"
            params.append(owner)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(5000, int(limit))))
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def update(self, project_id: int, **fields: Any) -> None:
        allowed = {
            "status","progress","error_message","staging_path","thumbnail_path","core_video_id","youtube_video_id","youtube_url",
            "remote_verified_at","cleanup_verified_at","source_file_name","source_file_size","retry_count","app_upload_at"
        }
        safe = {k:v for k,v in fields.items() if k in allowed}
        if not safe:
            return
        safe["updated_at"] = now_iso()
        sql = ",".join(f"{k}=?" for k in safe)
        with self.connect() as conn:
            conn.execute(f"UPDATE projects SET {sql} WHERE id=?", [*safe.values(), int(project_id)])

    def event(self, project_id: int | None, message: str, level: str = "INFO") -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO project_events(project_id,level,message,created_at) VALUES(?,?,?,?)", (project_id,level,str(message),now_iso()))

    def events(self, owner: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        query = """
            SELECT e.*, p.owner_username, p.title FROM project_events e
            LEFT JOIN projects p ON p.id=e.project_id
        """
        params: list[Any] = []
        if owner:
            query += " WHERE p.owner_username=? COLLATE NOCASE"
            params.append(owner)
        query += " ORDER BY e.id DESC LIMIT ?"
        params.append(max(1, min(5000, int(limit))))
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def recover_wrapper_states(self) -> int:
        with self.connect() as conn:
            rows = conn.execute("SELECT id,status FROM projects WHERE status IN ('DOWNLOADING','SOURCE_READY','UPLOAD_QUEUED','UPLOADING','VERIFYING_REMOTE','CLEANING')").fetchall()
            for row in rows:
                conn.execute("UPDATE projects SET status='QUEUED', error_message='Recovered after service restart; state will be reconciled safely.', updated_at=? WHERE id=?", (now_iso(), int(row["id"])))
            return len(rows)

    def _owner_eligibility_clause(self) -> str:
        if os.getenv("LUM_ENGINE_INSTANCE") == "concat":
            return """AND NOT EXISTS (SELECT 1 FROM copyright_owner_queues q
                WHERE q.owner_username=p.owner_username AND (
                    (COALESCE(p.app_upload_at,'')='' AND q.paused=1) OR
                    (COALESCE(p.app_upload_at,'')!='' AND q.scheduled_paused=1)))"""
        return """AND NOT EXISTS (SELECT 1 FROM copyright_owner_queues q
            WHERE q.owner_username=p.owner_username AND q.paused=1)"""

    def next_eligible(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects p WHERE status IN ('QUEUED','WAITING_SLOT','SCHEDULED','PAUSED') "
                + self._owner_eligibility_clause() + " ORDER BY id ASC"
            ).fetchall()
        for raw in rows:
            row = dict(raw)
            if row["status"] == "PAUSED":
                continue
            when = parse_time(row.get("app_upload_at"))
            if when and when > now:
                if row["status"] != "SCHEDULED":
                    self.update(int(row["id"]), status="SCHEDULED")
                continue
            return row
        return None

    def claim_next_eligible(self, *, schedule_only: bool = False) -> dict[str, Any] | None:
        """Atomically claim due work; a paused manual Concat queue still allows
        due timed jobs unless that owner paused scheduled uploads separately.
        """
        now = datetime.now(timezone.utc)
        concat = os.getenv("LUM_ENGINE_INSTANCE") == "concat"
        schedule_gate = " AND COALESCE(p.app_upload_at,'')!=''" if concat and schedule_only else ""
        order = ("ORDER BY CASE WHEN COALESCE(p.app_upload_at,'')!='' THEN 0 ELSE 1 END, id ASC"
                 if concat else "ORDER BY id ASC")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM projects p WHERE status IN ('QUEUED','WAITING_SLOT','SCHEDULED') "
                + self._owner_eligibility_clause() + schedule_gate + " " + order
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                when = parse_time(row.get("app_upload_at"))
                if when and when > now:
                    if row["status"] != "SCHEDULED":
                        conn.execute("UPDATE projects SET status='SCHEDULED',updated_at=? WHERE id=?",
                                     (now_iso(), int(row["id"])))
                    continue
                conn.execute("UPDATE projects SET status='SOURCE_READY',updated_at=? WHERE id=?",
                             (now_iso(), int(row["id"])))
                row["status"] = "SOURCE_READY"
                return row
        return None

    def cancel_waiting(self, project_id: int, owner: str) -> bool:
        """Cancel only unclaimed waiting work; keep media and upload history intact."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_username,status FROM projects WHERE id=?", (int(project_id),)
            ).fetchone()
            if (not row or str(row["owner_username"]).casefold() != str(owner).casefold()
                    or str(row["status"]) not in WAITING_STATES):
                return False
            conn.execute(
                "UPDATE projects SET status='CANCELLED',updated_at=? WHERE id=?",
                (now_iso(), int(project_id)),
            )
        self.event(project_id, "Waiting project cancelled; source media retained", "WARNING")
        return True

    def mark_other_waiting(self, active_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE projects SET status='WAITING_SLOT',updated_at=? WHERE id<>? AND status='QUEUED'", (now_iso(), int(active_id)))

    def release_waiting(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE projects SET status='QUEUED',updated_at=? WHERE status='WAITING_SLOT'", (now_iso(),))

    def retry(self, project_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT status,retry_count FROM projects WHERE id=?", (int(project_id),)).fetchone()
            if not row or str(row["status"]) not in {"FAILED","PAUSED"}:
                return False
            conn.execute("UPDATE projects SET status='QUEUED',error_message='',retry_count=?,updated_at=? WHERE id=?", (int(row["retry_count"] or 0)+1, now_iso(), int(project_id)))
        self.event(project_id, "Project returned to queue for retry", "WARNING")
        return True

    def archive_terminal(self, project_id: int, owner: str) -> bool:
        """Remove completed/cancelled work from the visible queue without losing history.

        Never delete projects, event records, source media, or YouTube receipts.
        An active or retryable failure cannot be archived by this method.
        """
        if not str(owner or "").strip():
            raise ValueError("Owner is required")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("""
                UPDATE projects SET archived_at=?,updated_at=?
                WHERE id=? AND owner_username=? COLLATE NOCASE
                  AND archived_at IS NULL AND status IN ('COMPLETED','CANCELLED')
            """, (now_iso(), now_iso(), int(project_id), owner)).rowcount
        if changed:
            self.event(project_id, "Project removed from queue by owner; upload history retained")
        return changed == 1

    def counts(self, owner: str | None = None) -> dict[str, int]:
        with self.connect() as conn:
            if owner:
                rows = conn.execute("SELECT status,COUNT(*) AS n FROM projects "
                                    "WHERE archived_at IS NULL AND owner_username=? COLLATE NOCASE GROUP BY status",
                                    (owner,)).fetchall()
            else:
                rows = conn.execute("SELECT status,COUNT(*) AS n FROM projects WHERE archived_at IS NULL GROUP BY status").fetchall()
        out = {str(r["status"]): int(r["n"]) for r in rows}
        out["TOTAL"] = sum(out.values())
        out["ACTIVE"] = sum(out.get(s, 0) for s in ACTIVE_STATES)
        out["WAITING"] = sum(out.get(s, 0) for s in WAITING_STATES)
        return out

    def claimed_folder_files(self, owner: str, folder: str = '') -> set[str]:
        """Include queued, scheduled, uploading, failed and completed jobs.

        Failed media remains reserved for retry. Cancelled jobs release their
        claim; successful uploads delete their physical handoff copy.
        """
        with self.connect() as conn:
            return {str(row[0]) for row in conn.execute(
                """SELECT source_file_name FROM projects
                   WHERE owner_username=? COLLATE NOCASE
                     AND vps_folder=? AND source_origin='folder' AND status!='CANCELLED'
                     AND source_file_name IS NOT NULL""",
                (owner,folder))}

    def copyright_profiles(self, owner: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM copyright_profiles WHERE owner_username=? COLLATE NOCASE ORDER BY id DESC",
                (owner,))]

    def add_copyright_profile(self, owner: str, name: str, group: str, metadata_source: str) -> int:
        if not name.strip() or len(name) > 100 or len(group) > 120:
            raise ValueError("Valid profile name and group are required")
        if metadata_source not in {"manual", "sheets"}:
            raise ValueError("Invalid metadata source")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO copyright_profiles(owner_username,profile_name,group_name,metadata_source,created_at) VALUES(?,?,?,?,?)",
                    (owner, name.strip(), group.strip(), metadata_source, now_iso()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Profile name already exists") from exc
            return int(cur.lastrowid)

    def update_copyright_profile(self, owner: str, profile_id: int, name: str,
                                 group: str, metadata_source: str) -> bool:
        if not name.strip() or len(name) > 100 or len(group) > 120:
            raise ValueError("Valid profile name and group are required")
        if metadata_source not in {"manual", "sheets"}:
            raise ValueError("Unsupported metadata source")
        with self.connect() as conn:
            try:
                changed = conn.execute("""
                    UPDATE copyright_profiles
                    SET profile_name=?,group_name=?,metadata_source=?
                    WHERE id=? AND owner_username=? COLLATE NOCASE
                """, (name.strip(), group.strip(), metadata_source,int(profile_id),owner)).rowcount
            except sqlite3.IntegrityError as exc:
                raise ValueError("Profile name already exists") from exc
        return changed == 1

    def delete_copyright_profile(self, owner: str, profile_id: int) -> bool:
        """Only remove saved configuration; queued/uploaded projects remain intact."""
        with self.connect() as conn:
            changed=conn.execute(
                "DELETE FROM copyright_profiles WHERE id=? AND owner_username=? COLLATE NOCASE",
                (int(profile_id),owner)).rowcount
        return changed == 1

    def copyright_profile(self, owner: str, profile_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM copyright_profiles WHERE id=? AND owner_username=? COLLATE NOCASE",
                (profile_id, owner)).fetchone()
            return dict(row) if row else None
