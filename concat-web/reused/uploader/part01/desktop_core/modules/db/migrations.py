from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .connection import SQLiteConnectionFactory
from ..secret_store import protect_secret

ApplyMigration = Callable[[sqlite3.Connection], None]
VerifyMigration = Callable[[sqlite3.Connection], bool]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: ApplyMigration
    verify: VerifyMigration
    destructive: bool = False


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_columns(conn: sqlite3.Connection, table: str, columns: Iterable[str]) -> bool:
    return set(columns).issubset(_table_columns(conn, table))


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone())


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE NOT NULL,
            channel_name TEXT NOT NULL,
            google_account_email TEXT,
            token_file_path TEXT NOT NULL,
            channel_url TEXT,
            browser_profile_name TEXT,
            proxy_scheme TEXT,
            proxy_host TEXT,
            proxy_port TEXT,
            proxy_username TEXT,
            proxy_password TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT UNIQUE NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_group_members (
            group_id INTEGER NOT NULL,
            channel_db_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(group_id, channel_db_id),
            FOREIGN KEY(group_id) REFERENCES channel_groups(id) ON DELETE CASCADE,
            FOREIGN KEY(channel_db_id) REFERENCES channels(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_video_path TEXT NOT NULL,
            thumbnail_path TEXT,
            title TEXT NOT NULL,
            description TEXT,
            tags TEXT,
            category_id TEXT NOT NULL DEFAULT '22',
            privacy_status TEXT NOT NULL DEFAULT 'private',
            publish_at TEXT,
            made_for_kids INTEGER NOT NULL DEFAULT 0,
            playlist_id TEXT,
            channel_db_id INTEGER NOT NULL,
            group_name TEXT,
            youtube_video_id TEXT,
            youtube_url TEXT,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            progress INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(channel_db_id) REFERENCES channels(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER,
            level TEXT NOT NULL DEFAULT 'INFO',
            message TEXT NOT NULL,
            progress INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sheet_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_name TEXT NOT NULL,
            sheet_url_or_id TEXT NOT NULL,
            gid TEXT NOT NULL DEFAULT '0',
            json_folder_path TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_sync_at TEXT,
            last_sync_status TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def _verify_1(conn: sqlite3.Connection) -> bool:
    required = {
        "channels": {"id", "channel_id", "token_file_path", "status"},
        "videos": {"id", "local_video_path", "channel_db_id", "status", "progress"},
        "upload_logs": {"id", "video_id", "message", "created_at"},
        "sheet_sources": {"id", "sheet_url_or_id", "json_folder_path"},
        "channel_groups": {"id", "group_name"},
        "channel_group_members": {"group_id", "channel_db_id"},
        "app_meta": {"key", "value"},
    }
    return all(_has_columns(conn, table, columns) for table, columns in required.items())


def _migration_2(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auto_engine_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT UNIQUE NOT NULL,
            video_folder TEXT,
            thumbnail_folder TEXT,
            metadata_sheet_url_or_id TEXT,
            metadata_sheet_tab TEXT,
            channel_group_id INTEGER,
            daily_upload_count INTEGER NOT NULL DEFAULT 1,
            schedule_times TEXT NOT NULL DEFAULT '09:00',
            schedule_clock_type TEXT NOT NULL DEFAULT '12h',
            upload_scope TEXT NOT NULL DEFAULT 'per_channel',
            work_mode TEXT NOT NULL DEFAULT 'manual_review',
            after_upload_action TEXT NOT NULL DEFAULT 'move_uploaded',
            auto_select_video INTEGER NOT NULL DEFAULT 1,
            thumbnail_rotation INTEGER NOT NULL DEFAULT 1,
            thumbnail_cursor INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            schedule_enabled INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(channel_group_id) REFERENCES channel_groups(id) ON DELETE SET NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auto_engine_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            run_key TEXT NOT NULL,
            jobs_created INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(profile_id, run_key),
            FOREIGN KEY(profile_id) REFERENCES auto_engine_profiles(id) ON DELETE CASCADE
        )
    """)
    for column, definition in {
        "group_name": "TEXT",
        "app_upload_at": "TEXT",
        "auto_engine_profile_id": "INTEGER",
        "auto_engine_profile_name": "TEXT",
        "auto_engine_after_upload_action": "TEXT",
    }.items():
        _ensure_column(conn, "videos", column, definition)
    for column, definition in {
        "schedule_clock_type": "TEXT NOT NULL DEFAULT '12h'",
        "thumbnail_cursor": "INTEGER NOT NULL DEFAULT 0",
        "schedule_enabled": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        _ensure_column(conn, "auto_engine_profiles", column, definition)


def _verify_2(conn: sqlite3.Connection) -> bool:
    return (
        _has_columns(conn, "auto_engine_profiles", {
            "id", "category_name", "schedule_times", "schedule_enabled", "thumbnail_cursor"
        })
        and _has_columns(conn, "auto_engine_runs", {"id", "profile_id", "run_key"})
        and _has_columns(conn, "videos", {
            "app_upload_at", "auto_engine_profile_id", "auto_engine_after_upload_action"
        })
    )


def _migration_3(conn: sqlite3.Connection) -> None:
    for column, definition in {
        "claim_worker_id": "TEXT",
        "claimed_at": "TEXT",
        "lease_expires_at": "TEXT",
        "resumable_uri": "TEXT",
        "resumable_file_size": "INTEGER",
        "resumable_file_mtime_ns": "INTEGER",
        "upload_bytes_confirmed": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _ensure_column(conn, "videos", column, definition)


def _verify_3(conn: sqlite3.Connection) -> bool:
    return _has_columns(conn, "videos", {
        "claim_worker_id", "claimed_at", "lease_expires_at", "resumable_uri",
        "resumable_file_size", "resumable_file_mtime_ns", "upload_bytes_confirmed",
    })


def _migration_4(conn: sqlite3.Connection) -> None:
    for column, definition in {
        "sheet_source_id": "INTEGER",
        "sheet_row_key": "TEXT",
        "sheet_notes": "TEXT",
        "sheet_last_seen_at": "TEXT",
        "channel_url": "TEXT",
        "browser_profile_name": "TEXT",
        "proxy_scheme": "TEXT",
        "proxy_host": "TEXT",
        "proxy_port": "TEXT",
        "proxy_username": "TEXT",
        "proxy_password": "TEXT",
    }.items():
        _ensure_column(conn, "channels", column, definition)
    _ensure_column(
        conn, "auto_engine_profiles", "missed_schedule_policy",
        "TEXT NOT NULL DEFAULT 'run_missed_today'",
    )
    conn.execute("""
        UPDATE videos
        SET app_upload_at=publish_at, publish_at=NULL
        WHERE (app_upload_at IS NULL OR app_upload_at='')
          AND publish_at IS NOT NULL AND publish_at!=''
          AND status IN ('SCHEDULED','QUEUED','PAUSED')
    """)


def _verify_4(conn: sqlite3.Connection) -> bool:
    return (
        _has_columns(conn, "channels", {
            "sheet_source_id", "sheet_row_key", "channel_url", "proxy_password"
        })
        and _has_columns(conn, "auto_engine_profiles", {"missed_schedule_policy"})
    )


def _migration_5(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_status_scope ON videos(status, auto_engine_profile_id, app_upload_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_claim_lease ON videos(claim_worker_id, lease_expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_channel_state ON videos(channel_db_id, status, lease_expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_logs_video_created ON upload_logs(video_id, created_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_sheet_identity ON channels(sheet_source_id, sheet_row_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auto_runs_profile_key ON auto_engine_runs(profile_id, run_key)")


def _verify_5(conn: sqlite3.Connection) -> bool:
    return all(_index_exists(conn, name) for name in {
        "idx_videos_status_scope",
        "idx_videos_claim_lease",
        "idx_videos_channel_state",
        "idx_upload_logs_video_created",
        "idx_channels_sheet_identity",
        "idx_auto_runs_profile_key",
    })


def _migration_6(conn: sqlite3.Connection) -> None:
    for column, definition in {
        "pipeline_stage": "TEXT NOT NULL DEFAULT 'PENDING'",
        "media_stage_state": "TEXT NOT NULL DEFAULT 'PENDING'",
        "thumbnail_stage_state": "TEXT NOT NULL DEFAULT 'PENDING'",
        "playlist_stage_state": "TEXT NOT NULL DEFAULT 'PENDING'",
        "post_action_stage_state": "TEXT NOT NULL DEFAULT 'PENDING'",
        "pipeline_warnings_json": "TEXT NOT NULL DEFAULT '[]'",
        "media_sha256": "TEXT",
        "upload_fingerprint": "TEXT",
        "recovery_state": "TEXT NOT NULL DEFAULT 'NONE'",
    }.items():
        _ensure_column(conn, "videos", column, definition)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_fingerprint_cache (
            normalized_path TEXT PRIMARY KEY,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_db_id INTEGER NOT NULL,
            upload_fingerprint TEXT NOT NULL,
            media_sha256 TEXT NOT NULL,
            video_id INTEGER,
            state TEXT NOT NULL DEFAULT 'RESERVED',
            youtube_video_id TEXT,
            youtube_url TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel_db_id, upload_fingerprint)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_ledger_state ON upload_ledger(state, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_recovery_state ON videos(recovery_state, status, id)")

    # Existing jobs that already have a YouTube ID must resume after the media
    # stage rather than uploading the source file again.
    conn.execute(
        """
        UPDATE videos
        SET media_stage_state='SUCCEEDED', pipeline_stage='MEDIA'
        WHERE COALESCE(youtube_video_id, '')<>''
          AND media_stage_state='PENDING'
        """
    )
    # Completed legacy rows are terminal. Optional legacy outcomes cannot be
    # reconstructed precisely, so configured optional steps are marked WARNING
    # rather than pretending that they definitely succeeded.
    conn.execute(
        """
        UPDATE videos
        SET pipeline_stage='COMPLETE',
            media_stage_state='SUCCEEDED',
            thumbnail_stage_state=CASE
                WHEN COALESCE(thumbnail_path, '')='' THEN 'SKIPPED' ELSE 'WARNING' END,
            playlist_stage_state=CASE
                WHEN COALESCE(playlist_id, '')='' THEN 'SKIPPED' ELSE 'WARNING' END,
            post_action_stage_state=CASE
                WHEN COALESCE(auto_engine_after_upload_action, '') IN ('', 'keep')
                THEN 'SKIPPED' ELSE 'WARNING' END
        WHERE status IN ('UPLOADED', 'PUBLISHED')
        """
    )


def _verify_6(conn: sqlite3.Connection) -> bool:
    return (
        _has_columns(conn, "videos", {
            "pipeline_stage",
            "media_stage_state",
            "thumbnail_stage_state",
            "playlist_stage_state",
            "post_action_stage_state",
            "pipeline_warnings_json",
            "media_sha256",
            "upload_fingerprint",
            "recovery_state",
        })
        and _has_columns(conn, "media_fingerprint_cache", {
            "normalized_path", "file_size", "file_mtime_ns", "sha256"
        })
        and _has_columns(conn, "upload_ledger", {
            "channel_db_id", "upload_fingerprint", "media_sha256", "state"
        })
        and _index_exists(conn, "idx_upload_ledger_state")
        and _index_exists(conn, "idx_videos_recovery_state")
    )


def _migration_7(conn: sqlite3.Connection) -> None:
    # Sheet inventory is authoritative for row presence, while operational
    # status remains app-owned. Historical rows stay in SQLite for token reuse.
    for column, definition in {
        "sheet_present": "INTEGER NOT NULL DEFAULT 1",
        "sheet_sr": "TEXT",
        "sheet_ip_raw": "TEXT",
        "sheet_password": "TEXT",
        "sheet_backup_code": "TEXT",
        "sheet_backup_key": "TEXT",
        "sheet_status": "TEXT",
    }.items():
        _ensure_column(conn, "channels", column, definition)
    conn.execute("UPDATE channels SET sheet_present=1 WHERE sheet_present IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_sheet_present ON channels(sheet_source_id, sheet_present)")


def _verify_7(conn: sqlite3.Connection) -> bool:
    return (
        _has_columns(conn, "channels", {
            "sheet_present", "sheet_sr", "sheet_ip_raw", "sheet_password",
            "sheet_backup_code", "sheet_backup_key", "sheet_status",
        })
        and _index_exists(conn, "idx_channels_sheet_present")
    )


def _migration_8(conn: sqlite3.Connection) -> None:
    """Permanent per-channel exact-filename upload protection."""
    _ensure_column(conn, "videos", "original_filename", "TEXT")
    _ensure_column(conn, "videos", "normalized_filename", "TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS successful_filename_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            normalized_filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            youtube_video_id TEXT,
            UNIQUE(channel_id, normalized_filename)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filename_history_channel ON successful_filename_uploads(channel_id, uploaded_at)")

    # Populate basename fields without using folder paths as identity.
    rows = conn.execute("SELECT id, local_video_path, original_filename, normalized_filename FROM videos").fetchall()
    for row in rows:
        original = str(row["original_filename"] or "").strip()
        if not original:
            original = str(row["local_video_path"] or "").replace('\\', '/').rsplit('/', 1)[-1].strip()
        normalized = original.strip().casefold()
        conn.execute("UPDATE videos SET original_filename=?, normalized_filename=? WHERE id=?", (original, normalized, int(row["id"])))

    # Migrate only reliable completed records already present in this database.
    completed = conn.execute("""
        SELECT c.channel_id, v.original_filename, v.normalized_filename,
               v.updated_at, v.youtube_video_id
        FROM videos v JOIN channels c ON c.id=v.channel_db_id
        WHERE v.status IN ('UPLOADED','PUBLISHED')
          AND COALESCE(v.normalized_filename, '')<>''
    """).fetchall()
    for row in completed:
        conn.execute("""
            INSERT OR IGNORE INTO successful_filename_uploads(
                channel_id, original_filename, normalized_filename, uploaded_at, youtube_video_id
            ) VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?)
        """, (row["channel_id"], row["original_filename"], row["normalized_filename"], row["updated_at"], row["youtube_video_id"]))

    # Resolve pre-existing simultaneous duplicates deterministically before adding the unique index.
    active = conn.execute("""
        SELECT channel_db_id, normalized_filename, GROUP_CONCAT(id) AS ids, COUNT(*) AS n
        FROM videos
        WHERE status IN ('QUEUED','SCHEDULED','WAITING_SLOT','CLAIMED','UPLOADING','WAITING_NETWORK','THUMBNAIL_UPLOADING','PAUSED')
          AND COALESCE(normalized_filename, '')<>''
        GROUP BY channel_db_id, normalized_filename HAVING COUNT(*)>1
    """).fetchall()
    for group in active:
        ids = sorted(int(x) for x in str(group["ids"]).split(',') if x)
        for duplicate_id in ids[1:]:
            reason = "Skipped: filename already queued for this channel."
            conn.execute("UPDATE videos SET status='FAILED', error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (reason, duplicate_id))
            conn.execute("INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, 'WARNING', ?, 0)", (duplicate_id, reason))

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_channel_filename
        ON videos(channel_db_id, normalized_filename)
        WHERE status IN ('QUEUED','SCHEDULED','WAITING_SLOT','CLAIMED','UPLOADING','WAITING_NETWORK','THUMBNAIL_UPLOADING','PAUSED')
          AND normalized_filename IS NOT NULL AND normalized_filename<>''
    """)


def _verify_8(conn: sqlite3.Connection) -> bool:
    return (
        _has_columns(conn, "videos", {"original_filename", "normalized_filename"})
        and _has_columns(conn, "successful_filename_uploads", {
            "channel_id", "original_filename", "normalized_filename",
            "uploaded_at", "youtube_video_id"
        })
        and _index_exists(conn, "uq_active_channel_filename")
    )



def _migration_9(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "auto_engine_profiles", "metadata_sheet_gid", "TEXT")


def _verify_9(conn: sqlite3.Connection) -> bool:
    return _has_columns(conn, "auto_engine_profiles", {"metadata_sheet_gid"})


MIGRATIONS = (
    Migration(1, "base channels, queue, logs, and sheets schema", _migration_1, _verify_1),
    Migration(2, "auto engine profiles and portable paths", _migration_2, _verify_2),
    Migration(3, "atomic queue leases and resumable upload sessions", _migration_3, _verify_3),
    Migration(4, "channel-scoped tokens and protected proxy credentials", _migration_4, _verify_4),
    Migration(5, "repository separation and operational indexes", _migration_5, _verify_5),
    Migration(6, "checkpointed idempotent upload pipeline", _migration_6, _verify_6),
    Migration(7, "sheet inventory presence and persistent inventory fields", _migration_7, _verify_7),
    Migration(8, "per-channel permanent exact-filename upload history", _migration_8, _verify_8),
    Migration(9, "auto engine metadata worksheet gid routing", _migration_9, _verify_9),
)


class MigrationRunner:
    """Transactional, verifiable, versioned SQLite schema migrations."""

    def __init__(self, factory: SQLiteConnectionFactory):
        self.factory = factory

    def run(self) -> int:
        with self.factory.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

        latest = 0
        for migration in MIGRATIONS:
            self._apply_one(migration)
            latest = migration.version
        self._harden_proxy_secrets()
        with self.factory.connect() as conn:
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(latest),),
            )
        return latest

    def _apply_one(self, migration: Migration) -> None:
        with self.factory.connect() as conn:
            recorded = bool(conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (migration.version,)
            ).fetchone())
            if recorded and migration.verify(conn):
                return

        if migration.destructive:
            self._backup_before_destructive_migration(migration)

        conn = self.factory.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            migration.apply(conn)
            if not migration.verify(conn):
                raise RuntimeError(
                    f"Migration {migration.version} verification failed: {migration.name}"
                )
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(version) DO UPDATE SET name=excluded.name, applied_at=CURRENT_TIMESTAMP",
                (migration.version, migration.name),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _backup_before_destructive_migration(self, migration: Migration) -> None:
        source = self.factory.db_path
        if not source.exists():
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = source.with_name(f"{source.stem}.pre_migration_v{migration.version}_{stamp}{source.suffix}")
        source_conn = sqlite3.connect(source)
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()

    def _harden_proxy_secrets(self) -> None:
        with self.factory.connect() as conn:
            if not _table_exists(conn, "channels") or "proxy_password" not in _table_columns(conn, "channels"):
                return
            rows = conn.execute(
                "SELECT id, proxy_password FROM channels WHERE COALESCE(proxy_password, '')<>''"
            ).fetchall()
            for row in rows:
                protected = protect_secret(row["proxy_password"])
                if protected != str(row["proxy_password"] or ""):
                    conn.execute(
                        "UPDATE channels SET proxy_password=? WHERE id=?",
                        (protected, int(row["id"])),
                    )
