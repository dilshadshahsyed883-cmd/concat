from __future__ import annotations

def thumbnail_required_for_mode() -> bool:
    """Song shares the Short Uploader Auto Engine flow but requires thumbnails.

    Only the original Short Uploader omits thumbnails; the isolated Song
    Uploader enforces a matched thumbnail for each channel Video ID.
    """
    return os.environ.get("LUM_ENGINE_INSTANCE", "engine1").strip().lower() not in {"shorts", "shortstudio"}


import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
import zlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from desktop_core.modules.google_sheet_sync import (
    extract_gid,
    fetch_sheet_csv_gid,
    fetch_sheet_csv_tab,
)
from desktop_core.modules.token_utils import (
    VALID_TOKEN,
    classify_google_json,
    is_verified_channel_id,
)
from desktop_core.modules.upload_identity import DuplicateUploadError
from .service_status import auto_engine_service_units


DATA_ROOT = Path(os.environ.get("LUM_AUTO_ENGINE_V6_ROOT", "/var/lib/local-upload-manager/auto_engine_v6"))
DB_PATH = DATA_ROOT / "auto_engine_v6.sqlite3"
THUMB_ROOT = DATA_ROOT / "thumbnails"
TRASH_ROOT = DATA_ROOT / "thumbnail_trash"
STAGING_ROOT = DATA_ROOT / "staging"
REPORT_ROOT = DATA_ROOT / "reports"
SHORT_STUDIO_ROOT = Path(os.environ.get("SHORT_STUDIO_VPS_FOLDER_ROOT", "/var/lib/fresh-website/short-maker/vps_short_folders"))
SHORT_FOLDER_PREFIX = "shortstudio-local:"
SHORT_FOLDER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}\Z")


def shortstudio_folder_path(name: str) -> Path:
    if not SHORT_FOLDER_PATTERN.fullmatch(name or ""):
        raise ValueError("Choose a valid Short Maker VPS folder.")
    if SHORT_STUDIO_ROOT.is_symlink():
        raise ValueError("Shared Short Studio root cannot be a symlink.")
    path = SHORT_STUDIO_ROOT / name
    if path.is_symlink():
        raise ValueError("VPS folder symlinks are prohibited.")
    return path


def shortstudio_list_folders(owner: str = "") -> List[str]:
    if not SHORT_STUDIO_ROOT.is_dir() or SHORT_STUDIO_ROOT.is_symlink():
        return []
    folders=[]
    for p in SHORT_STUDIO_ROOT.iterdir():
        if not SHORT_FOLDER_PATTERN.fullmatch(p.name) or not p.is_dir() or p.is_symlink():
            continue
        if owner:
            marker=p/".studio_owner"
            if marker.is_symlink() or not marker.is_file():
                continue  # Legacy unassigned folder is admin-only.
            try:
                if marker.read_text(encoding="utf-8").strip().casefold()!=owner.casefold():
                    continue
            except OSError:
                continue
        folders.append(p.name)
    return sorted(folders,key=str.casefold)


def shortstudio_source_path(folder_id: str, filename: str) -> Path:
    if not folder_id.startswith(SHORT_FOLDER_PREFIX):
        raise ValueError("Not a Short Studio local source.")
    folder = shortstudio_folder_path(folder_id[len(SHORT_FOLDER_PREFIX):])
    if Path(filename).name != filename or not filename.lower().endswith(".mp4"):
        raise ValueError("Invalid Short Studio filename.")
    path = folder / filename
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Short Maker local MP4 is missing or unsafe.")
    return path


def shortstudio_file_id(folder_id: str, path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Unsafe Short Studio source.")
    stat = path.stat()
    fingerprint = f"{folder_id}|{path.name}|{stat.st_size}|{stat.st_mtime_ns}"
    return "shortstudio:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:40]



VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
THUMB_EXTS = {".jpg", ".jpeg", ".png"}
ACTIVE_PC_JOB_STATUSES = {
    "QUEUED","SCHEDULED","WAITING_SLOT","CLAIMED","UPLOADING",
    "WAITING_NETWORK","THUMBNAIL_UPLOADING","PAUSED",
}
TERMINAL_PC_JOB_STATUSES = {"UPLOADED","PUBLISHED","FAILED","CANCELLED"}
SCRIPT_KEY_RE = re.compile(r"[^a-z0-9._-]+")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return str(value or "").strip()


def safe_slug(value: str, fallback: str = "profile") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", clean(value)).strip("-._").lower()
    return (text[:80] or fallback)


def normalize_filename(value: Any) -> str:
    return clean(value).replace("\\","/").rsplit("/",1)[-1].casefold()


def script_key_from_filename(value: Any) -> str:
    """Permanent content/script identity used by VPS duplicate protection.

    The PC app's latest hard rule is Channel ID + exact normalized filename.
    VPS adds a second conservative key using the filename stem so the same
    script/permanent-ID cannot bypass protection by changing only extension.
    """
    name = clean(value).replace("\\","/").rsplit("/",1)[-1]
    stem = Path(name).stem.casefold()
    stem = SCRIPT_KEY_RE.sub("-", stem).strip("-._")
    return stem or normalize_filename(name)


def extract_drive_folder_id(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    # Raw IDs and common Drive folder URL forms.
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text):
        return text
    raise ValueError("Google Drive folder URL/ID is invalid.")


def _image_dimensions(path: Path) -> tuple[int,int]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33:
        # Validate the PNG chunk framing/CRC through IEND so a truncated/corrupt
        # file cannot enter the permanent rotation library just from its header.
        pos=8; width=height=0; saw_iend=False
        while pos+12 <= len(data):
            length=int.from_bytes(data[pos:pos+4],"big")
            ctype=data[pos+4:pos+8]
            end=pos+12+length
            if length < 0 or end > len(data):
                raise ValueError("Corrupt PNG chunk length.")
            chunk=data[pos+8:pos+8+length]
            crc_expected=int.from_bytes(data[pos+8+length:pos+12+length],"big")
            if (zlib.crc32(ctype+chunk)&0xffffffff) != crc_expected:
                raise ValueError("Corrupt PNG checksum.")
            if ctype==b"IHDR":
                if length!=13: raise ValueError("Invalid PNG IHDR.")
                width,height=struct.unpack(">II",chunk[:8])
            if ctype==b"IEND":
                saw_iend=True; break
            pos=end
        if saw_iend and width>0 and height>0:
            return width,height
        raise ValueError("Truncated PNG image.")
    if data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
        i=2
        while i+9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i+1]
            i += 2
            if marker in {0xD8,0xD9}:
                continue
            if i+2 > len(data):
                break
            seglen = int.from_bytes(data[i:i+2],"big")
            if seglen < 2 or i+seglen > len(data):
                break
            if marker in {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF} and seglen >= 7:
                h=int.from_bytes(data[i+3:i+5],"big")
                w=int.from_bytes(data[i+5:i+7],"big")
                if w>0 and h>0: return w,h
            i += seglen
    raise ValueError("Unsupported or corrupt thumbnail image.")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rclone_config() -> Path:
    candidates=[
        Path(os.environ.get("LUM_RCLONE_CONFIG","")),
        Path("/home/ubuntu/.config/masterctl/rclone.conf"),
        Path("/home/ubuntu/apps/auto_caption_studio/data/config/rclone.conf"),
        Path("/home/ubuntu/apps/auto_caption_studio/data/config/rclone.conf"),
    ]
    for p in candidates:
        if str(p) and p.is_file():
            return p
    raise FileNotFoundError("rclone configuration was not found.")


def rclone_remote() -> str:
    return clean(os.environ.get("LUM_RCLONE_REMOTE","gdrive")) or "gdrive"


def _run(cmd: Sequence[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc=subprocess.run(list(cmd),capture_output=True,text=True,timeout=timeout)
    if check and proc.returncode != 0:
        msg=(proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(msg[:1000])
    return proc


def drive_lsjson(folder_id: str) -> List[Dict[str,Any]]:
    cfg=rclone_config()
    cmd=[
        "rclone","--config",str(cfg),"lsjson",f"{rclone_remote()}:",
        "--drive-root-folder-id",folder_id,
        "--max-depth","1",
    ]
    out=_run(cmd,timeout=90).stdout
    rows=json.loads(out or "[]")
    return [dict(x) for x in rows if isinstance(x,dict)]


def drive_download(folder_id: str, filename: str, target: Path, *, lease_renew=None) -> None:
    target.parent.mkdir(parents=True,exist_ok=True)
    part=target.with_suffix(target.suffix+".part")
    part.unlink(missing_ok=True)
    cmd=[
        "rclone","--config",str(rclone_config()),"copyto",
        f"{rclone_remote()}:{filename}",str(part),
        "--drive-root-folder-id",folder_id,
        "--retries","3","--low-level-retries","10",
    ]
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    started=time.monotonic()
    while proc.poll() is None:
        if lease_renew:
            try: lease_renew()
            except Exception: pass
        if time.monotonic()-started > 6*60*60:
            proc.kill()
            raise TimeoutError("Drive download exceeded 6 hours.")
        time.sleep(20)
    stdout,stderr=proc.communicate()
    if proc.returncode != 0:
        part.unlink(missing_ok=True)
        raise RuntimeError((stderr or stdout or "Drive download failed")[:1000])
    if not part.exists() or part.stat().st_size <= 0:
        part.unlink(missing_ok=True)
        raise RuntimeError("Drive download produced an empty file.")
    os.replace(part,target)


def drive_ensure_uploaded(folder_id: str) -> None:
    _run([
        "rclone","--config",str(rclone_config()),"mkdir",
        f"{rclone_remote()}:Uploaded","--drive-root-folder-id",folder_id
    ],timeout=60)


def _drive_item_id(row: Dict[str,Any]) -> str:
    return clean(row.get("ID") or row.get("Id") or row.get("id"))


def _drive_item_name(row: Dict[str,Any]) -> str:
    return clean(row.get("Name") or row.get("Path"))


def _drive_uploaded_folder_id(folder_id: str) -> str:
    for row in drive_lsjson(folder_id):
        if bool(row.get("IsDir")) and _drive_item_name(row).casefold()=="uploaded":
            return _drive_item_id(row)
    return ""


def drive_move_uploaded(folder_id: str, filename: str, drive_file_id: str = "") -> str:
    """Idempotently move a verified source into Uploaded/.

    Drive file ID is the preferred identity. If a crash happens after Drive move
    but before our database checkpoint, the next reconciliation sees the same
    file ID inside Uploaded and returns success instead of attempting a second move.
    """
    drive_ensure_uploaded(folder_id)
    root_rows=drive_lsjson(folder_id)
    uploaded_id=_drive_uploaded_folder_id(folder_id)
    uploaded_rows=drive_lsjson(uploaded_id) if uploaded_id else []
    wanted_id=clean(drive_file_id)

    if wanted_id and not wanted_id.startswith("name:"):
        for row in uploaded_rows:
            if not bool(row.get("IsDir")) and _drive_item_id(row)==wanted_id:
                return f"Uploaded/{_drive_item_name(row)}"

    source_row=None
    if wanted_id and not wanted_id.startswith("name:"):
        source_row=next((r for r in root_rows if not bool(r.get("IsDir")) and _drive_item_id(r)==wanted_id),None)
    if source_row is None:
        source_row=next((r for r in root_rows if not bool(r.get("IsDir")) and _drive_item_name(r)==filename),None)

    if source_row is None:
        # Provider did not expose a stable ID. Exact filename in Uploaded is the
        # safest idempotent fallback; never manufacture a new upload/move.
        exact=next((r for r in uploaded_rows if not bool(r.get("IsDir")) and _drive_item_name(r)==filename),None)
        if exact:
            return f"Uploaded/{filename}"
        raise FileNotFoundError(f"Drive source is missing from both source and Uploaded/: {filename}")

    current_name=_drive_item_name(source_row) or filename
    target_name=current_name
    used={_drive_item_name(r).casefold():r for r in uploaded_rows if not bool(r.get("IsDir"))}
    conflict=used.get(target_name.casefold())
    if conflict:
        p=Path(target_name)
        suffix=safe_slug(wanted_id[-8:] or str(int(time.time())),"dup")
        target_name=f"{p.stem}__{suffix}{p.suffix}"

    _run([
        "rclone","--config",str(rclone_config()),"moveto",
        f"{rclone_remote()}:{current_name}",
        f"{rclone_remote()}:Uploaded/{target_name}",
        "--drive-root-folder-id",folder_id,
        "--retries","3","--low-level-retries","10",
    ],timeout=300)
    return f"Uploaded/{target_name}"


def drive_delete_source(folder_id: str, filename: str, drive_file_id: str = "") -> None:
    rows=drive_lsjson(folder_id)
    wanted_id=clean(drive_file_id)
    source=None
    if wanted_id and not wanted_id.startswith("name:"):
        source=next((r for r in rows if not bool(r.get("IsDir")) and _drive_item_id(r)==wanted_id),None)
    if source is None:
        source=next((r for r in rows if not bool(r.get("IsDir")) and _drive_item_name(r)==filename),None)
    if source is None:
        return  # idempotent: an earlier delete may already have succeeded
    _run([
        "rclone","--config",str(rclone_config()),"deletefile",
        f"{rclone_remote()}:{_drive_item_name(source)}",
        "--drive-root-folder-id",folder_id,
    ],timeout=120)


class V6Store:
    def __init__(self, path: Path = DB_PATH):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self._lock=threading.RLock()
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        con=sqlite3.connect(str(self.path),timeout=30)
        con.row_factory=sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def init_schema(self) -> None:
        DATA_ROOT.mkdir(parents=True,exist_ok=True)
        THUMB_ROOT.mkdir(parents=True,exist_ok=True)
        TRASH_ROOT.mkdir(parents=True,exist_ok=True)
        STAGING_ROOT.mkdir(parents=True,exist_ok=True)
        REPORT_ROOT.mkdir(parents=True,exist_ok=True)
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS profile_sources(
                profile_id INTEGER PRIMARY KEY,
                drive_video_folder_id TEXT NOT NULL DEFAULT '',
                drive_video_folder_url TEXT NOT NULL DEFAULT '',
                drive_thumbnail_folder_id TEXT NOT NULL DEFAULT '',
                drive_thumbnail_folder_url TEXT NOT NULL DEFAULT '',
                thumbnail_source_mode TEXT NOT NULL DEFAULT 'pc_drive',
                local_songmaker_enabled INTEGER NOT NULL DEFAULT 0,
                short_folder_name TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS drive_videos(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                drive_file_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                normalized_filename TEXT NOT NULL,
                script_key TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                modified_at TEXT,
                source_folder_id TEXT NOT NULL,
                present INTEGER NOT NULL DEFAULT 1,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(profile_id, drive_file_id)
            );
            CREATE INDEX IF NOT EXISTS idx_v6_drive_video_profile_present
                ON drive_videos(profile_id,present,normalized_filename);

            CREATE TABLE IF NOT EXISTS thumbnails(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                file_size INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                drive_file_id TEXT,
                source_removed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                rotation_order INTEGER NOT NULL DEFAULT 0,
                times_used INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(profile_id, sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_v6_thumbs_profile_status
                ON thumbnails(profile_id,status,rotation_order,id);

            CREATE TABLE IF NOT EXISTS job_sources(
                video_id INTEGER PRIMARY KEY,
                profile_id INTEGER NOT NULL,
                drive_video_id INTEGER NOT NULL,
                drive_file_id TEXT NOT NULL,
                source_folder_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                normalized_filename TEXT NOT NULL,
                script_key TEXT NOT NULL,
                thumbnail_asset_id INTEGER,
                staging_path TEXT NOT NULL,
                desired_after_success TEXT NOT NULL DEFAULT 'move_uploaded',
                state TEXT NOT NULL DEFAULT 'QUEUED',
                remote_verified INTEGER NOT NULL DEFAULT 0,
                drive_destination TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_v6_job_source_state
                ON job_sources(state,profile_id,drive_file_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_v6_active_profile_drive
                ON job_sources(profile_id,drive_file_id)
                WHERE state NOT IN ('CANCELLED','FAILED_SAFE');

            CREATE TABLE IF NOT EXISTS upload_records(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER,
                profile_id INTEGER NOT NULL,
                profile_name TEXT,
                channel_db_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                channel_name TEXT,
                drive_file_id TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                normalized_filename TEXT NOT NULL,
                script_key TEXT NOT NULL,
                thumbnail_asset_id INTEGER,
                thumbnail_filename TEXT,
                title TEXT,
                youtube_video_id TEXT NOT NULL,
                youtube_url TEXT NOT NULL,
                privacy_status TEXT,
                uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                drive_destination TEXT,
                final_state TEXT NOT NULL DEFAULT 'COMPLETE',
                UNIQUE(channel_id, drive_file_id),
                UNIQUE(channel_id, script_key),
                UNIQUE(channel_id, normalized_filename),
                UNIQUE(youtube_video_id)
            );
            CREATE INDEX IF NOT EXISTS idx_v6_upload_records_date
                ON upload_records(uploaded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_v6_upload_records_channel
                ON upload_records(channel_id,uploaded_at DESC);

            CREATE TABLE IF NOT EXISTS script_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                normalized_filename TEXT NOT NULL,
                script_key TEXT NOT NULL,
                uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                youtube_video_id TEXT,
                source TEXT NOT NULL DEFAULT 'PC_FILENAME_HISTORY',
                UNIQUE(channel_id, normalized_filename),
                UNIQUE(channel_id, script_key)
            );
            CREATE INDEX IF NOT EXISTS idx_v6_script_history_channel
                ON script_history(channel_id,uploaded_at DESC);

            CREATE TABLE IF NOT EXISTS missed_alarms(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                run_key TEXT NOT NULL UNIQUE,
                alarm_time TEXT NOT NULL,
                alarm_date TEXT NOT NULL,
                delay_minutes INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS engine_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                running INTEGER NOT NULL DEFAULT 0,
                pause_after_current INTEGER NOT NULL DEFAULT 0,
                scheduler_armed INTEGER NOT NULL DEFAULT 0,
                concurrency INTEGER NOT NULL DEFAULT 1,
                last_worker_heartbeat TEXT,
                last_scheduler_heartbeat TEXT,
                last_error TEXT,
                manual_profile_ids TEXT DEFAULT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)
            # Additive Short Studio source migration. Existing Drive sources and
            # job reservations are preserved without rewriting historic rows.
            source_columns = {str(r[1]) for r in con.execute("PRAGMA table_info(profile_sources)").fetchall()}
            if "short_folder_name" not in source_columns:
                con.execute("ALTER TABLE profile_sources ADD COLUMN short_folder_name TEXT NOT NULL DEFAULT ''")
            # V6.1.1 migration ordering: add legacy columns before INSERT references them.
            cols={str(r[1]) for r in con.execute("PRAGMA table_info(engine_state)").fetchall()}
            if "scheduler_armed" not in cols:
                con.execute("ALTER TABLE engine_state ADD COLUMN scheduler_armed INTEGER NOT NULL DEFAULT 0")
            if "concurrency" not in cols:
                con.execute("ALTER TABLE engine_state ADD COLUMN concurrency INTEGER NOT NULL DEFAULT 1")
            if "manual_profile_ids" not in cols:
                con.execute("ALTER TABLE engine_state ADD COLUMN manual_profile_ids TEXT DEFAULT NULL")
            con.execute("INSERT OR IGNORE INTO engine_state(singleton,running,pause_after_current,scheduler_armed,concurrency) VALUES(1,0,0,0,1)")

    def source(self, profile_id:int) -> Dict[str,Any]:
        with self.connect() as con:
            row=con.execute("SELECT * FROM profile_sources WHERE profile_id=?",(int(profile_id),)).fetchone()
            return dict(row) if row else {
                "profile_id":int(profile_id),"drive_video_folder_id":"","drive_video_folder_url":"",
                "drive_thumbnail_folder_id":"","drive_thumbnail_folder_url":"","thumbnail_source_mode":"pc_drive","short_folder_name":""
            }

    def group_settings(self, profile_id:int) -> Dict[str,Any]:
        with self.connect() as con:
            row=con.execute("SELECT settings_json FROM song_group_settings WHERE profile_id=?", (int(profile_id),)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_group_settings(self, profile_id:int, settings:Dict[str,Any]) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO song_group_settings(profile_id,settings_json) VALUES(?,?) "
                        "ON CONFLICT(profile_id) DO UPDATE SET settings_json=excluded.settings_json",
                        (int(profile_id),json.dumps(settings)))

    def save_source(self, profile_id:int, *, video_folder:str="", thumb_folder:str="", thumb_mode:str="pc_drive", short_folder:str="", songmaker_enabled:Optional[bool]=None) -> None:
        if short_folder:
            folder = shortstudio_folder_path(short_folder)
            if not folder.is_dir():
                raise ValueError("Short Maker VPS folder does not exist.")
        vf=extract_drive_folder_id(video_folder) if clean(video_folder) else ""
        tf=extract_drive_folder_id(thumb_folder) if clean(thumb_folder) else ""
        mode=thumb_mode if thumb_mode in {"pc","drive","pc_drive"} else "pc_drive"
        with self.connect() as con:
            con.execute("""
            INSERT INTO profile_sources(profile_id,drive_video_folder_id,drive_video_folder_url,
                drive_thumbnail_folder_id,drive_thumbnail_folder_url,thumbnail_source_mode,short_folder_name,local_songmaker_enabled,updated_at)
            VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(profile_id) DO UPDATE SET
                drive_video_folder_id=excluded.drive_video_folder_id,
                drive_video_folder_url=excluded.drive_video_folder_url,
                drive_thumbnail_folder_id=excluded.drive_thumbnail_folder_id,
                drive_thumbnail_folder_url=excluded.drive_thumbnail_folder_url,
                thumbnail_source_mode=excluded.thumbnail_source_mode,
                local_songmaker_enabled=CASE WHEN ? IS NULL THEN profile_sources.local_songmaker_enabled ELSE excluded.local_songmaker_enabled END,
                short_folder_name=excluded.short_folder_name,
                updated_at=CURRENT_TIMESTAMP
            """,(int(profile_id),vf,clean(video_folder),tf,clean(thumb_folder),mode,short_folder,
                 int(bool(songmaker_enabled)), None if songmaker_enabled is None else int(bool(songmaker_enabled))))

    def set_engine(self, running:Optional[bool]=None, pause_after_current:Optional[bool]=None, error:str|None=None) -> None:
        parts=["updated_at=CURRENT_TIMESTAMP"]; params=[]
        if running is not None: parts.append("running=?"); params.append(1 if running else 0)
        if pause_after_current is not None: parts.append("pause_after_current=?"); params.append(1 if pause_after_current else 0)
        if error is not None: parts.append("last_error=?"); params.append(clean(error)[:1000])
        with self.connect() as con:
            con.execute(f"UPDATE engine_state SET {', '.join(parts)} WHERE singleton=1",params)

    def engine(self) -> Dict[str,Any]:
        with self.connect() as con:
            return dict(con.execute("SELECT * FROM engine_state WHERE singleton=1").fetchone())

    def set_manual_profile_ids(self, profile_ids: Optional[List[int]]) -> None:
        """None allows all enabled profiles; an empty list allows none.
        Short Studio uses this to keep a one-profile Start from draining another profile's queue.
        """
        value=None if profile_ids is None else ",".join(str(int(pid)) for pid in sorted(set(profile_ids)) if int(pid)>0)
        with self.connect() as con:
            con.execute("UPDATE engine_state SET manual_profile_ids=?,updated_at=CURRENT_TIMESTAMP WHERE singleton=1",(value,))

    def set_scheduler_armed(self, armed: bool) -> None:
        with self.connect() as con:
            con.execute("UPDATE engine_state SET scheduler_armed=?,updated_at=CURRENT_TIMESTAMP WHERE singleton=1",(1 if armed else 0,))

    def set_concurrency(self, value: int) -> int:
        concurrency=max(1,min(5,int(value or 1)))
        with self.connect() as con:
            con.execute("UPDATE engine_state SET concurrency=?,updated_at=CURRENT_TIMESTAMP WHERE singleton=1",(concurrency,))
        return concurrency

    def heartbeat(self, kind:str, error:str="") -> None:
        col="last_worker_heartbeat" if kind=="worker" else "last_scheduler_heartbeat"
        with self.connect() as con:
            con.execute(f"UPDATE engine_state SET {col}=?, last_error=?, updated_at=CURRENT_TIMESTAMP WHERE singleton=1",
                        (utcnow(),clean(error)[:1000]))

    def scan_drive_videos(self, profile_id:int) -> Dict[str,Any]:
        src=self.source(profile_id)
        folder_id=clean(src.get("drive_video_folder_id"))
        if os.environ.get("LUM_ENGINE_INSTANCE") == "shortstudio" and clean(src.get("short_folder_name")):
            return self.scan_shortstudio_local(profile_id, clean(src["short_folder_name"]))
        if self.source(profile_id).get("local_songmaker_enabled"):
            settings=self.group_settings(profile_id)
            if settings.get("video_source_mode")=="songfolder":
                from .songmaker_local import sync_folder
                sync_folder(self,profile_id,settings)
            return self.scan_songmaker_local(profile_id, thumbnail=False)
        if not folder_id:
            raise ValueError("Google Drive Video Folder is required.")
        rows=drive_lsjson(folder_id)
        files=[]
        names={}
        for r in rows:
            if bool(r.get("IsDir")):
                continue
            name=clean(r.get("Name") or r.get("Path"))
            if Path(name).suffix.lower() not in VIDEO_EXTS:
                continue
            names[name.casefold()]=names.get(name.casefold(),0)+1
            files.append(r)
        duplicates=[n for n,c in names.items() if c>1]
        if duplicates:
            raise ValueError("Drive source contains duplicate filenames. Rename them first: "+", ".join(duplicates[:8]))
        seen=[]
        with self.connect() as con:
            con.execute("UPDATE drive_videos SET present=0 WHERE profile_id=? AND source_folder_id<>'songmaker-local'",(int(profile_id),))
            for r in files:
                name=clean(r.get("Name") or r.get("Path"))
                fid=clean(r.get("ID") or r.get("Id") or r.get("id"))
                if not fid:
                    # Stable fallback only when provider omits ID.
                    fid="name:"+hashlib.sha256((folder_id+"|"+name).encode()).hexdigest()[:32]
                size=int(r.get("Size") or 0)
                mod=clean(r.get("ModTime"))
                normalized=normalize_filename(name)
                skey=script_key_from_filename(name)
                con.execute("""
                INSERT INTO drive_videos(profile_id,drive_file_id,filename,normalized_filename,script_key,
                    size_bytes,modified_at,source_folder_id,present,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,1,CURRENT_TIMESTAMP)
                ON CONFLICT(profile_id,drive_file_id) DO UPDATE SET
                    filename=excluded.filename,normalized_filename=excluded.normalized_filename,
                    script_key=excluded.script_key,size_bytes=excluded.size_bytes,
                    modified_at=excluded.modified_at,source_folder_id=excluded.source_folder_id,
                    present=1,last_seen_at=CURRENT_TIMESTAMP
                """,(int(profile_id),fid,name,normalized,skey,size,mod,folder_id))
                seen.append(fid)
        return {"videos":len(files),"folder_id":folder_id,"duplicates":0}

    def scan_shortstudio_local(self, profile_id: int, folder_name: str) -> Dict[str,Any]:
        """Index only regular MP4s within a selected Short Maker folder."""
        folder = shortstudio_folder_path(folder_name)
        if not folder.is_dir():
            raise ValueError("Short Maker VPS folder is unavailable.")
        source_id = SHORT_FOLDER_PREFIX + folder_name
        files = sorted((p for p in folder.iterdir()
                        if p.suffix.lower() == ".mp4" and p.is_file()
                        and not p.is_symlink() and p.stat().st_size > 0),
                       key=lambda p: p.name.casefold())
        if len({p.name.casefold() for p in files}) != len(files):
            raise ValueError("VPS folder contains duplicate case-insensitive MP4 names.")
        with self.connect() as con:
            con.execute("UPDATE drive_videos SET present=0 WHERE profile_id=?", (int(profile_id),))
            for path in files:
                stat = path.stat()
                fid = shortstudio_file_id(source_id, path)
                con.execute("""
                INSERT INTO drive_videos(profile_id,drive_file_id,filename,normalized_filename,script_key,
                    size_bytes,modified_at,source_folder_id,present,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,1,CURRENT_TIMESTAMP)
                ON CONFLICT(profile_id,drive_file_id) DO UPDATE SET
                    filename=excluded.filename,normalized_filename=excluded.normalized_filename,
                    script_key=excluded.script_key,size_bytes=excluded.size_bytes,
                    modified_at=excluded.modified_at,source_folder_id=excluded.source_folder_id,
                    present=1,last_seen_at=CURRENT_TIMESTAMP
                """, (int(profile_id),fid,path.name,normalize_filename(path.name),
                      script_key_from_filename(path.name),stat.st_size,
                      str(stat.st_mtime_ns),source_id))
        return {"videos":len(files),"folder_id":source_id,"duplicates":0}


    def scan_songmaker_local(self, profile_id:int, *, thumbnail:bool=False) -> Dict[str,Any]:
        """Recheck local media availability without ever contacting Google Drive."""
        table="drive_thumbnails" if thumbnail else "drive_videos"
        total=0
        with self.connect() as con:
            rows=con.execute("SELECT id,drive_file_id,filename FROM "+table+
                             " WHERE profile_id=? AND source_folder_id='songmaker-local'",
                             (int(profile_id),)).fetchall()
            for row in rows:
                folder=STAGING_ROOT/f"profile_{int(profile_id):04d}"/("drive_"+safe_slug(row["drive_file_id"],"file"))
                path=(folder/"thumbnail"/row["filename"]) if thumbnail else (folder/row["filename"])
                valid=path.is_file() and not path.is_symlink() and path.stat().st_size>0
                con.execute("UPDATE "+table+" SET present=? WHERE id=?", (int(valid),int(row["id"])))
                total+=int(valid)
        return {"thumbnails" if thumbnail else "videos":total,"folder_id":"songmaker-local"}

    def drive_videos(self, profile_id:int, present_only:bool=True) -> List[Dict[str,Any]]:
        q="SELECT * FROM drive_videos WHERE profile_id=?"
        params=[int(profile_id)]
        if present_only: q+=" AND present=1"
        source=self.source(profile_id)
        if os.environ.get("LUM_ENGINE_INSTANCE") == "shortstudio" and clean(source.get("short_folder_name")):
            q+=" AND source_folder_id=?"
            params.append(SHORT_FOLDER_PREFIX+clean(source["short_folder_name"]))
        elif source.get("local_songmaker_enabled"):
            q+=" AND source_folder_id='songmaker-local'"
        else:
            q+=" AND source_folder_id<>'songmaker-local' AND source_folder_id NOT LIKE 'shortstudio-local:%'"
        settings=self.group_settings(profile_id)
        if settings.get("video_source_mode")=="songfolder":
            q+=" AND drive_file_id IN (SELECT drive_file_id FROM song_folder_imports WHERE profile_id=? AND folder_id=? AND owner=?)"
            params.extend([int(profile_id),settings["song_folder_id"],settings["song_folder_owner"]])
        q+=" ORDER BY normalized_filename,id"
        with self.connect() as con:
            return [dict(r) for r in con.execute(q,params).fetchall()]

    def profile_thumb_dir(self, profile_id:int, profile_name:str="") -> Path:
        d=THUMB_ROOT/f"profile_{int(profile_id):04d}_{safe_slug(profile_name or str(profile_id))}"
        d.mkdir(parents=True,exist_ok=True)
        return d

    def add_thumbnail_bytes(self, profile_id:int, profile_name:str, filename:str, data:bytes, source_type:str="PC", drive_file_id:str="") -> Dict[str,Any]:
        if len(data)<=0: raise ValueError("Thumbnail is empty.")
        if len(data)>2*1024*1024: raise ValueError("Thumbnail exceeds 2 MB.")
        ext=Path(filename).suffix.lower()
        if ext not in THUMB_EXTS: raise ValueError("Thumbnail must be JPG/JPEG/PNG.")
        tmp=Path(tempfile.mkstemp(prefix="lumthumb_",suffix=ext)[1])
        try:
            tmp.write_bytes(data)
            w,h=_image_dimensions(tmp)
            if w<120 or h<68:
                raise ValueError("Thumbnail dimensions are too small.")
            digest=_sha256_bytes(data)
            with self.connect() as con:
                existing=con.execute("SELECT * FROM thumbnails WHERE profile_id=? AND sha256=?",(int(profile_id),digest)).fetchone()
                if existing:
                    return {"status":"DUPLICATE","asset":dict(existing)}
                order=int(con.execute("SELECT COALESCE(MAX(rotation_order),0)+1 FROM thumbnails WHERE profile_id=?",(int(profile_id),)).fetchone()[0])
                d=self.profile_thumb_dir(profile_id,profile_name)
                safe=safe_slug(Path(filename).stem,"thumb")
                stored=d/f"{order:04d}_{digest[:10]}_{safe}{ext}"
                stored.write_bytes(data)
                cur=con.execute("""
                INSERT INTO thumbnails(profile_id,original_filename,stored_path,sha256,width,height,file_size,
                    source_type,drive_file_id,status,rotation_order)
                VALUES(?,?,?,?,?,?,?,?,?,'ACTIVE',?)
                """,(int(profile_id),clean(filename),str(stored),digest,w,h,len(data),source_type.upper(),clean(drive_file_id),order))
                aid=int(cur.lastrowid)
                return {"status":"INSTALLED","asset_id":aid,"width":w,"height":h,"path":str(stored)}
        finally:
            tmp.unlink(missing_ok=True)

    def sync_drive_thumbnails(self, profile_id:int, profile_name:str) -> Dict[str,Any]:
        src=self.source(profile_id)
        folder_id=clean(src.get("drive_thumbnail_folder_id"))
        if not folder_id:
            return {"seen":0,"installed":0,"duplicates":0,"removed_marked":0,"skipped":0}
        rows=drive_lsjson(folder_id)
        current_ids=set()
        installed=duplicates=skipped=0
        for r in rows:
            if bool(r.get("IsDir")): continue
            name=clean(r.get("Name") or r.get("Path"))
            if Path(name).suffix.lower() not in THUMB_EXTS: continue
            fid=clean(r.get("ID") or r.get("Id") or r.get("id"))
            if not fid: continue
            current_ids.add(fid)
            with self.connect() as con:
                existing=con.execute("SELECT id FROM thumbnails WHERE profile_id=? AND drive_file_id=?",(int(profile_id),fid)).fetchone()
            if existing:
                continue
            tmp=Path(tempfile.mkstemp(prefix="lumdrivethumb_",suffix=Path(name).suffix)[1])
            try:
                drive_download(folder_id,name,tmp)
                result=self.add_thumbnail_bytes(profile_id,profile_name,name,tmp.read_bytes(),"DRIVE",fid)
                if result["status"]=="INSTALLED": installed+=1
                else: duplicates+=1
            except Exception:
                skipped+=1
            finally:
                tmp.unlink(missing_ok=True)
        removed=0
        with self.connect() as con:
            rows2=con.execute("SELECT id,drive_file_id FROM thumbnails WHERE profile_id=? AND source_type='DRIVE' AND COALESCE(drive_file_id,'')<>''",(int(profile_id),)).fetchall()
            for row in rows2:
                flag=0 if clean(row["drive_file_id"]) in current_ids else 1
                con.execute("UPDATE thumbnails SET source_removed=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(flag,int(row["id"])))
                removed += flag
        return {"seen":len(current_ids),"installed":installed,"duplicates":duplicates,"removed_marked":removed,"skipped":skipped}

    def thumbs(self, profile_id:int, include_trashed:bool=False) -> List[Dict[str,Any]]:
        q="SELECT * FROM thumbnails WHERE profile_id=?"
        if not include_trashed: q+=" AND status<>'TRASHED'"
        q+=" ORDER BY rotation_order,id"
        with self.connect() as con:
            return [dict(r) for r in con.execute(q,(int(profile_id),)).fetchall()]

    def thumb(self, asset_id:int) -> Optional[Dict[str,Any]]:
        with self.connect() as con:
            r=con.execute("SELECT * FROM thumbnails WHERE id=?",(int(asset_id),)).fetchone()
            return dict(r) if r else None

    def set_thumb_status(self, ids:Iterable[int], status:str) -> int:
        wanted=status.upper()
        if wanted not in {"ACTIVE","DISABLED"}: raise ValueError("Invalid thumbnail status.")
        vals=sorted({int(x) for x in ids if int(x)>0})
        if not vals: return 0
        marks=",".join("?" for _ in vals)
        with self.connect() as con:
            cur=con.execute(f"UPDATE thumbnails SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id IN ({marks}) AND status<>'TRASHED'",[wanted]+vals)
            return int(cur.rowcount)

    def move_thumbs(self, ids:Iterable[int], target_profile_id:int, target_name:str, pc_db:Any|None=None) -> Tuple[int,List[int],List[int]]:
        vals=sorted({int(x) for x in ids if int(x)>0})
        moved=0; blocked=[]; duplicates=[]
        for aid in vals:
            asset=self.thumb(aid)
            if not asset or asset["status"]=="TRASHED": continue
            if int(asset.get("profile_id") or 0)==int(target_profile_id):
                continue
            if pc_db is not None:
                with self.connect() as con:
                    linked=[int(r["video_id"]) for r in con.execute("SELECT video_id FROM job_sources WHERE thumbnail_asset_id=?",(aid,)).fetchall()]
                if any((pc_db.get_video(vid) or {}).get("status") in ACTIVE_PC_JOB_STATUSES for vid in linked):
                    blocked.append(aid); continue
            with self.connect() as con:
                dup=con.execute("SELECT id FROM thumbnails WHERE profile_id=? AND sha256=? AND status<>'TRASHED' LIMIT 1",(int(target_profile_id),clean(asset.get("sha256")))).fetchone()
                if dup:
                    duplicates.append(aid); continue
                order=int(con.execute("SELECT COALESCE(MAX(rotation_order),0)+1 FROM thumbnails WHERE profile_id=?",(int(target_profile_id),)).fetchone()[0])
            src=Path(asset["stored_path"])
            d=self.profile_thumb_dir(target_profile_id,target_name)
            target=d/src.name
            if target.exists(): target=d/f"{src.stem}_{aid}{src.suffix}"
            if not src.exists():
                blocked.append(aid); continue
            shutil.move(str(src),str(target))
            try:
                with self.connect() as con:
                    con.execute("UPDATE thumbnails SET profile_id=?,stored_path=?,rotation_order=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (int(target_profile_id),str(target),order,aid))
            except Exception:
                shutil.move(str(target),str(src))
                raise
            moved+=1
        return moved,blocked,duplicates

    def trash_thumbs(self, ids:Iterable[int], pc_db:Any) -> Tuple[int,List[int]]:
        vals=sorted({int(x) for x in ids if int(x)>0})
        blocked=[]; moved=0
        active_states=tuple(ACTIVE_PC_JOB_STATUSES)
        for aid in vals:
            with self.connect() as con:
                linked=[int(r["video_id"]) for r in con.execute("SELECT video_id FROM job_sources WHERE thumbnail_asset_id=?",(aid,)).fetchall()]
            if linked:
                blocked_job=False
                for vid in linked:
                    row=pc_db.get_video(vid)
                    if row and clean(row.get("status")).upper() in ACTIVE_PC_JOB_STATUSES:
                        blocked_job=True; break
                if blocked_job:
                    blocked.append(aid); continue
            asset=self.thumb(aid)
            if not asset or asset["status"]=="TRASHED": continue
            src=Path(asset["stored_path"])
            d=TRASH_ROOT/f"profile_{int(asset['profile_id']):04d}"
            d.mkdir(parents=True,exist_ok=True)
            target=d/f"{aid}_{src.name}"
            if src.exists(): shutil.move(str(src),str(target))
            with self.connect() as con:
                con.execute("UPDATE thumbnails SET status='TRASHED',stored_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(str(target),aid))
            moved+=1
        return moved,blocked

    def restore_thumb(self, asset_id:int, profile_name:str="") -> bool:
        asset=self.thumb(asset_id)
        if not asset or asset["status"]!="TRASHED": return False
        src=Path(asset["stored_path"])
        d=self.profile_thumb_dir(int(asset["profile_id"]),profile_name)
        target=d/src.name.split("_",1)[-1]
        if src.exists(): shutil.move(str(src),str(target))
        with self.connect() as con:
            con.execute("UPDATE thumbnails SET status='DISABLED',stored_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(str(target),int(asset_id)))
        return True

    def purge_trash(self, days:int=7) -> int:
        cutoff=(datetime.now(timezone.utc)-timedelta(days=max(1,int(days)))).isoformat()
        removed=0
        with self.connect() as con:
            rows=con.execute("SELECT id,stored_path FROM thumbnails WHERE status='TRASHED' AND updated_at<?",(cutoff,)).fetchall()
            for row in rows:
                Path(row["stored_path"]).unlink(missing_ok=True)
                con.execute("DELETE FROM thumbnails WHERE id=?",(int(row["id"]),))
                removed+=1
        return removed

    def empty_trash(self) -> int:
        removed=0
        with self.connect() as con:
            rows=con.execute("SELECT id,stored_path FROM thumbnails WHERE status='TRASHED'").fetchall()
            for row in rows:
                Path(row["stored_path"]).unlink(missing_ok=True)
                con.execute("DELETE FROM thumbnails WHERE id=?",(int(row["id"]),))
                removed+=1
        return removed

    def import_pc_filename_history(self, pc_db:Any) -> int:
        """Mirror the PC app's authoritative successful-filename ledger into V6.

        This does not replace the PC ledger. It adds the filename-stem/script key so
        an older successful script cannot bypass protection by changing extension.
        """
        imported=0
        try:
            with pc_db.connect() as src:
                rows=src.execute("""
                    SELECT channel_id,original_filename,normalized_filename,uploaded_at,youtube_video_id
                    FROM successful_filename_uploads
                    ORDER BY id
                """).fetchall()
        except Exception:
            return 0
        with self.connect() as con:
            for row in rows:
                original=clean(row["original_filename"])
                normalized=clean(row["normalized_filename"]) or normalize_filename(original)
                skey=script_key_from_filename(original or normalized)
                try:
                    con.execute("""
                        INSERT INTO script_history(channel_id,original_filename,normalized_filename,script_key,uploaded_at,youtube_video_id,source)
                        VALUES(?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP),?,'PC_FILENAME_HISTORY')
                        ON CONFLICT(channel_id,normalized_filename) DO UPDATE SET
                            youtube_video_id=COALESCE(excluded.youtube_video_id,script_history.youtube_video_id),
                            uploaded_at=COALESCE(excluded.uploaded_at,script_history.uploaded_at)
                    """,(clean(row["channel_id"]),original,normalized,skey,clean(row["uploaded_at"]),clean(row["youtube_video_id"]) or None))
                    imported+=1
                except sqlite3.IntegrityError:
                    # A stricter existing script-key record already protects this content.
                    pass
        return imported

    def is_duplicate(self, channel_id:str, drive_file_id:str, script_key:str, normalized_filename:str) -> Tuple[bool,str]:
        with self.connect() as con:
            row=con.execute("""
            SELECT id FROM script_history
            WHERE channel_id=? AND (script_key=? OR normalized_filename=?)
            LIMIT 1
            """,(channel_id,script_key,normalized_filename)).fetchone()
            if row: return True,"PC/V6 permanent script history"
            row=con.execute("""
            SELECT id FROM upload_records
            WHERE channel_id=? AND (drive_file_id=? OR script_key=? OR normalized_filename=?)
            LIMIT 1
            """,(channel_id,drive_file_id,script_key,normalized_filename)).fetchone()
            if row: return True,"V6 permanent upload record"
            active=con.execute("""
            SELECT video_id FROM job_sources
            WHERE state NOT IN ('CANCELLED','FAILED_SAFE')
              AND (drive_file_id=? OR script_key=? OR normalized_filename=?)
            LIMIT 1
            """,(drive_file_id,script_key,normalized_filename)).fetchone()
            if active: return True,"V6 source already reserved by another job"
        return False,""

    def reserve_job_source(self, *, video_id:int, profile_id:int, drive_video:Dict[str,Any], thumb_id:int|None, staging_path:Path, after_success:str) -> None:
        with self.connect() as con:
            con.execute("""
            INSERT INTO job_sources(video_id,profile_id,drive_video_id,drive_file_id,source_folder_id,
                filename,normalized_filename,script_key,thumbnail_asset_id,staging_path,
                desired_after_success,state)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'QUEUED')
            """,(int(video_id),int(profile_id),int(drive_video["id"]),clean(drive_video["drive_file_id"]),
                 clean(drive_video["source_folder_id"]),clean(drive_video["filename"]),
                 clean(drive_video["normalized_filename"]),clean(drive_video["script_key"]),
                 int(thumb_id) if thumb_id else None,str(staging_path),after_success))

    def job_source(self, video_id:int) -> Optional[Dict[str,Any]]:
        with self.connect() as con:
            r=con.execute("SELECT * FROM job_sources WHERE video_id=?",(int(video_id),)).fetchone()
            return dict(r) if r else None

    def set_job_state(self, video_id:int, state:str, *, error:str="", destination:str|None=None, remote_verified:Optional[bool]=None) -> None:
        parts=["state=?","last_error=?","updated_at=CURRENT_TIMESTAMP"]; params=[state,clean(error)[:1000]]
        if destination is not None: parts.append("drive_destination=?"); params.append(clean(destination))
        if remote_verified is not None: parts.append("remote_verified=?"); params.append(1 if remote_verified else 0)
        params.append(int(video_id))
        with self.connect() as con:
            con.execute(f"UPDATE job_sources SET {', '.join(parts)} WHERE video_id=?",params)

    def record_upload(self, pc_db:Any, video_id:int, *, destination:str="") -> Dict[str,Any]:
        job=pc_db.get_video(int(video_id))
        src=self.job_source(video_id)
        if not job or not src: raise ValueError("Completed job/source mapping is missing.")
        channel_id=clean(job.get("youtube_channel_id") or job.get("channel_id"))
        yid=clean(job.get("youtube_video_id"))
        yurl=clean(job.get("youtube_url"))
        if not is_verified_channel_id(channel_id) or not yid or not yurl:
            raise ValueError("Upload record requires verified channel and YouTube video ID/URL.")
        thumb=self.thumb(int(src["thumbnail_asset_id"])) if src.get("thumbnail_asset_id") else None
        with self.connect() as con:
            try:
                con.execute("""
                INSERT INTO upload_records(video_id,profile_id,profile_name,channel_db_id,channel_id,
                    channel_name,drive_file_id,original_filename,normalized_filename,script_key,
                    thumbnail_asset_id,thumbnail_filename,title,youtube_video_id,youtube_url,
                    privacy_status,drive_destination,final_state)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'REMOTE_VERIFIED')
                """,(int(video_id),int(src["profile_id"]),clean(job.get("auto_engine_profile_name")),
                    int(job.get("channel_db_id") or 0),channel_id,clean(job.get("channel_name")),
                    clean(src["drive_file_id"]),clean(src["filename"]),clean(src["normalized_filename"]),
                    clean(src["script_key"]),int(src["thumbnail_asset_id"]) if src.get("thumbnail_asset_id") else None,
                    clean(thumb.get("original_filename")) if thumb else "",clean(job.get("title")),
                    yid,yurl,clean(job.get("privacy_status")),clean(destination)))
            except sqlite3.IntegrityError as exc:
                existing=con.execute("""
                    SELECT * FROM upload_records
                    WHERE channel_id=? AND (drive_file_id=? OR script_key=? OR normalized_filename=? OR youtube_video_id=?)
                    ORDER BY id LIMIT 1
                """,(channel_id,clean(src["drive_file_id"]),clean(src["script_key"]),clean(src["normalized_filename"]),yid)).fetchone()
                if not existing or clean(existing["youtube_video_id"])!=yid:
                    raise RuntimeError(
                        "Permanent upload-history conflict after remote YouTube creation. "
                        "Source was retained and must be reviewed before cleanup/move."
                    ) from exc
            # Keep a second compact permanent script ledger. This deliberately survives
            # queue cleanup and independently blocks extension-only renames forever.
            con.execute("""
                INSERT OR IGNORE INTO script_history(
                    channel_id,original_filename,normalized_filename,script_key,uploaded_at,youtube_video_id,source
                ) VALUES(?,?,?,?,CURRENT_TIMESTAMP,?,'V6_UPLOAD')
            """,(channel_id,clean(src["filename"]),clean(src["normalized_filename"]),clean(src["script_key"]),yid))
        self.export_records_csv()
        return {"channel_id":channel_id,"youtube_video_id":yid,"youtube_url":yurl}

    def records(self, limit:int=1000) -> List[Dict[str,Any]]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM upload_records ORDER BY uploaded_at DESC,id DESC LIMIT ?",(int(limit),)).fetchall()]

    def history_records(self, limit:int=1000) -> List[Dict[str,Any]]:
        # LUM_UPLOAD_RECORDS_DEDUPE_V1
        # upload_records is the canonical V6 upload receipt. script_history is a second,
        # permanent duplicate-protection ledger. A successful V6 upload intentionally
        # writes to BOTH tables; never show/count that same YouTube upload twice.
        records=self.records(limit)
        with self.connect() as con:
            legacy=[dict(r) for r in con.execute("""
                SELECT sh.uploaded_at,'' AS profile_name,'' AS channel_name,sh.channel_id,sh.original_filename,sh.script_key,
                       '' AS drive_file_id,'' AS title,
                       CASE WHEN COALESCE(sh.youtube_video_id,'')<>'' THEN 'https://www.youtube.com/watch?v='||sh.youtube_video_id ELSE '' END AS youtube_url,
                       COALESCE(sh.youtube_video_id,'') AS youtube_video_id,'' AS thumbnail_filename,'' AS privacy_status,
                       CASE WHEN sh.source='V6_UPLOAD' THEN 'V6 HISTORY' ELSE 'PC HISTORY' END AS drive_destination,
                       'HISTORICAL' AS final_state,sh.source
                FROM script_history AS sh
                WHERE NOT (
                    COALESCE(sh.youtube_video_id,'')<>'' AND EXISTS(
                        SELECT 1 FROM upload_records AS ur
                        WHERE ur.channel_id=sh.channel_id AND ur.youtube_video_id=sh.youtube_video_id
                    )
                )
                ORDER BY sh.uploaded_at DESC,sh.id DESC LIMIT ?
            """,(int(limit),)).fetchall()]
        all_rows=records+legacy
        all_rows.sort(key=lambda r: clean(r.get("uploaded_at")),reverse=True)
        return all_rows[:int(limit)]

    def export_records_csv(self) -> Path:
        path=REPORT_ROOT/"auto_engine_upload_records.csv"
        rows=self.history_records(100000)
        headers=["uploaded_at","profile_name","channel_name","channel_id","original_filename","script_key",
                 "drive_file_id","title","youtube_url","youtube_video_id","thumbnail_filename","privacy_status",
                 "drive_destination","final_state"]
        tmp=path.with_suffix(".csv.tmp")
        with tmp.open("w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=headers); w.writeheader()
            for r in reversed(rows): w.writerow({h:r.get(h,"") for h in headers})
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
        return path

    def add_missed(self, profile_id:int, run_key:str, alarm:str, delay:int) -> None:
        with self.connect() as con:
            con.execute("""
            INSERT OR IGNORE INTO missed_alarms(profile_id,run_key,alarm_time,alarm_date,delay_minutes,state)
            VALUES(?,?,?,?,?,'REVIEW_REQUIRED')
            """,(int(profile_id),run_key,alarm,run_key.split(" ",1)[0],int(delay)))

    def missed(self) -> List[Dict[str,Any]]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM missed_alarms WHERE state='REVIEW_REQUIRED' ORDER BY created_at").fetchall()]

    def resolve_missed(self, item_id:int, state:str) -> None:
        with self.connect() as con:
            con.execute("UPDATE missed_alarms SET state=?,resolved_at=CURRENT_TIMESTAMP WHERE id=?",(state,int(item_id)))


def parse_schedule_times(value: Any) -> List[str]:
    out=[]; seen=set()
    for raw in clean(value).replace(";",",").split(","):
        raw=raw.strip()
        if not raw: continue
        m=re.fullmatch(r"(\d{1,2}):(\d{2})",raw)
        if not m: raise ValueError(f"Invalid alarm time: {raw}. Use HH:MM.")
        h=int(m.group(1)); minute=int(m.group(2))
        if not 0<=h<=23 or not 0<=minute<=59: raise ValueError(f"Invalid alarm time: {raw}.")
        val=f"{h:02d}:{minute:02d}"
        if val not in seen: seen.add(val); out.append(val)
    return sorted(out or ["09:00"])


def save_profile_from_form(pc_db:Any, store:V6Store, form:Dict[str,Any], profile_id:int|None=None) -> int:
    mode=clean(form.get("work_mode")) or "manual_review"
    schedule_enabled=str(form.get("schedule_enabled") or "").lower() in {"1","true","on","yes"}
    data={
        "category_name":clean(form.get("category_name")),
        # Keep PC schema fields meaningful but no longer use local video folders.
        "video_folder":"",
        "thumbnail_folder":str(store.profile_thumb_dir(profile_id or 0, clean(form.get("category_name")))) if profile_id else "",
        "metadata_sheet_url_or_id":clean(form.get("metadata_sheet_url_or_id")),
        "metadata_sheet_tab":clean(form.get("metadata_sheet_tab")),
        "metadata_sheet_gid":clean(form.get("metadata_sheet_gid")) or extract_gid(clean(form.get("metadata_sheet_url_or_id")),fallback=""),
        "channel_group_id":clean(form.get("channel_group_id")),
        "daily_upload_count":max(1,min(100,int(form.get("daily_upload_count") or 1))),
        "schedule_times":",".join(parse_schedule_times(form.get("schedule_times"))),
        "schedule_clock_type":clean(form.get("schedule_clock_type")) or "12h",
        "upload_scope":clean(form.get("upload_scope")) or "per_channel",
        "work_mode":mode,
        "after_upload_action":clean(form.get("after_upload_action")) or "move_uploaded",
        "auto_select_video":True,
        "thumbnail_rotation":str(form.get("thumbnail_rotation") or "").lower() in {"1","true","on","yes"},
        "enabled":str(form.get("enabled") or "").lower() in {"1","true","on","yes"},
        "schedule_enabled":schedule_enabled if mode=="auto" else False,
        "missed_schedule_policy":clean(form.get("missed_schedule_policy")) or "run_missed_today",
        "notes":clean(form.get("notes")),
    }
    if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio" and mode=="auto" and schedule_enabled:
        count=len(parse_schedule_times(form.get("schedule_times")))
        if count != data["daily_upload_count"]:
            raise ValueError(f"Choose {data['daily_upload_count']} distinct PKT schedule times, one per daily video; currently {count}.")
    pid=int(pc_db.save_auto_engine_profile(data,profile_id))
    # Update thumbnail folder now that permanent profile ID exists.
    d=store.profile_thumb_dir(pid,data["category_name"])
    with pc_db.connect() as con:
        con.execute("UPDATE auto_engine_profiles SET thumbnail_folder=?,video_folder='' WHERE id=?",(str(d),pid))
    store.save_source(pid,
        video_folder=clean(form.get("drive_video_folder")),
        thumb_folder=clean(form.get("drive_thumbnail_folder")),
        thumb_mode=clean(form.get("thumbnail_source_mode")) or "pc_drive",
        **({"short_folder":clean(form.get("short_vps_folder"))} if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio" else {}),
    )
    return pid


def target_channels(pc_db:Any, profile:Dict[str,Any]) -> List[Dict[str,Any]]:
    gid=profile.get("channel_group_id")
    rows=pc_db.list_channel_group_members(int(gid),active_only=True) if gid else pc_db.list_channels(active_only=True)
    return [r for r in rows if is_verified_channel_id(clean(r.get("channel_id"))) and classify_google_json(clean(r.get("token_file_path"))) == VALID_TOKEN]


def category_metadata(profile:Dict[str,Any]) -> Tuple[Dict[str,Any],str]:
    settings=profile.get("_short_group_metadata") or {}
    if settings.get("metadata_source")=="manual":
        return {"title":settings.get("manual_title"), "description":settings.get("manual_description"),
                "category_id":settings.get("manual_category_id") or "22", "privacy_status":"private"}, "Manual website metadata"
    sheet=clean(profile.get("metadata_sheet_url_or_id")); tab=clean(profile.get("metadata_sheet_tab"))
    gid=clean(profile.get("metadata_sheet_gid")) or (extract_gid(sheet,fallback="") if sheet else "")
    if not sheet: raise ValueError("Metadata Sheet URL / ID is required.")
    if not tab: raise ValueError("Metadata Category Tab name is required.")
    try:
        rows=fetch_sheet_csv_gid(sheet,gid,timeout=18) if gid else fetch_sheet_csv_tab(sheet,tab,timeout=18)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    if not rows: raise ValueError(f'The metadata tab "{tab}" has no filled metadata row.')
    first=dict(rows[0])
    if not any(k in first for k in ("title","video_title")):
        raise ValueError("Metadata tab is missing required title/video_title column.")
    title=clean(first.get("title") or first.get("video_title"))
    if not title: raise ValueError("The first metadata row has an empty title.")
    privacy=clean(first.get("visibility") or first.get("privacy_status") or first.get("privacy") or "private").lower()
    if privacy not in {"private","public","unlisted"}: raise ValueError("Visibility must be private, public, or unlisted.")
    first["privacy_status"]=privacy
    first["category_id"]=clean(first.get("youtube_category") or first.get("category_id") or first.get("category") or "22") or "22"
    note=f'Metadata source: tab "{tab}" | GID {gid}, first filled row.' if gid else f'Metadata source: tab "{tab}" legacy tab-name routing.'
    if len(rows)>1: note+=f" {len(rows)} rows found; only first row is intentionally used."
    return first,note


def apply_vars(text:str, filename:str, index:int, profile:Dict[str,Any], channel:Dict[str,Any]) -> str:
    p=Path(filename)
    rep={
        "{number}":str(index+1),"{video_name}":p.name,"{video_stem}":p.stem,
        "{category}":clean(profile.get("category_name") or profile.get("metadata_sheet_tab")),
        "{channel_name}":clean(channel.get("channel_name") or channel.get("name")),
    }
    val=str(text or "")
    for k,v in rep.items(): val=val.replace(k,v)
    return val


def metadata_for_job(meta:Dict[str,Any], filename:str, index:int, profile:Dict[str,Any], channel:Dict[str,Any], preview:bool=False) -> Dict[str,Any]:
    title=apply_vars(clean(meta.get("title") or meta.get("video_title")),filename,index,profile,channel).strip()
    if not title and not preview: raise ValueError("Resolved Auto Engine title is empty.")
    if len(title)>100 and not preview: raise ValueError(f"Resolved title is {len(title)} characters for {filename}.")
    desc=apply_vars(clean(meta.get("description") or meta.get("desc")),filename,index,profile,channel)
    tags=apply_vars(clean(meta.get("tags") or meta.get("keywords")),filename,index,profile,channel)
    privacy=clean(meta.get("privacy_status") or "private").lower()
    kids=clean(meta.get("made_for_kids") or meta.get("kids")).lower() in {"1","true","yes","y","kids","made"}
    return {
        "title":title,"description":desc,"tags":tags,"category_id":clean(meta.get("category_id") or "22") or "22",
        "privacy_status":privacy if privacy in {"private","public","unlisted"} else "private",
        "playlist_id":clean(meta.get("playlist_id") or meta.get("playlist")),
        "made_for_kids":1 if kids else 0,
    }


def planner(pc_db:Any, store:V6Store, profile:Dict[str,Any], *, max_jobs_override:int|None=None, preview_only:bool=True) -> Dict[str,Any]:
    store.import_pc_filename_history(pc_db)
    pid=int(profile.get("id") or 0)
    if not pid: raise ValueError("Saved Auto Engine profile is required.")
    source=store.source(pid)
    if not clean(source.get("drive_video_folder_id")) and not source.get('local_songmaker_enabled') and not (os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio" and clean(source.get("short_folder_name"))): raise ValueError("Google Drive Video Folder is required.")
    # Always plan against the current Drive inventory. This covers Preview, manual Generate,
    # Start All, scheduler alarms and missed-alarm runs because they all pass through planner().
    try:
        store.scan_drive_videos(pid)
    except Exception as exc:
        raise ValueError(f"Drive video source sync failed: {exc}") from exc
    song_group=os.environ.get("LUM_ENGINE_INSTANCE")=="song"
    settings=store.group_settings(pid) if song_group else {}
    matched_thumbs=[]
    if song_group:
        store.scan_drive_thumbnails(pid)
        matched_thumbs=store.drive_thumbnails(pid)
    thumb_mode=clean(source.get("thumbnail_source_mode") or "pc_drive")
    thumb_folder=clean(source.get("drive_thumbnail_folder_id"))
    if not song_group and thumbnail_required_for_mode() and thumb_mode in {"drive","pc_drive"} and thumb_folder:
        try:
            store.sync_drive_thumbnails(pid,clean(profile.get("category_name")))
        except Exception as exc:
            # Drive-only profiles cannot proceed with a stale/unavailable Drive thumbnail source.
            # PC+Drive profiles may safely continue with the already-installed permanent VPS library.
            if thumb_mode=="drive":
                raise ValueError(f"Drive thumbnail source sync failed: {exc}") from exc
    videos=store.drive_videos(pid,True)
    if not videos: raise ValueError("No indexed Drive videos. Run Sync & Scan Sources.")
    channels=target_channels(pc_db,profile)
    if not channels: raise ValueError("No eligible ACTIVE channel with verified UC ID and valid token.")
    thumbs=[t for t in store.thumbs(pid) if t["status"]=="ACTIVE"]
    blocked=[]
    if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio":
        profile={**profile, "_short_group_metadata":short_group_metadata(store, pid)}
    if not song_group and thumbnail_required_for_mode() and not thumbs: blocked.append("No ACTIVE thumbnail is available.")
    if song_group and settings.get("metadata_source")=="manual":
        meta={"title":settings.get("manual_title"),"description":settings.get("manual_description"),
              "category_id":settings.get("manual_category_id") or "10","privacy_status":"private"}
        note="Manual metadata saved on website. Group schedule applies."
    else:
        meta,note=category_metadata(profile)
    daily=max(1,int(profile.get("daily_upload_count") or 1))
    scope=clean(profile.get("upload_scope") or "per_channel")
    target=daily*len(channels) if scope=="per_channel" else daily
    if max_jobs_override is not None: target=max(1,int(max_jobs_override))
    target=min(target,len(videos))
    cursor=pc_db.get_auto_engine_thumbnail_cursor(pid)
    quota=daily if scope=="per_channel" else target
    if scope=="per_channel" and max_jobs_override is not None:
        quota=max(1,(target+len(channels)-1)//len(channels))
    assignments=[]
    if scope=="per_channel":
        for slot in range(daily):
            for ch in channels:
                for vi,v in enumerate(videos):
                    assignments.append((slot,ch,vi,v))
    else:
        for vi,v in enumerate(videos):
            assignments.append((0,channels[vi%len(channels)],vi,v))
    selected=[]; per_channel={}; claimed=set(); duplicate_count=0
    for slot,ch,vi,v in assignments:
        if len(selected)>=target: break
        cid=int(ch.get("id") or 0)
        if per_channel.get(cid,0)>=quota: continue
        fid=clean(v["drive_file_id"])
        if not fid or fid in claimed: continue
        channel_uc=clean(ch.get("channel_id"))
        # Preserve PC latest hard duplicate rule first.
        if pc_db.is_channel_filename_duplicate(cid,v["filename"]):
            duplicate_count+=1; continue
        dup,reason=store.is_duplicate(channel_uc,fid,clean(v["script_key"]),clean(v["normalized_filename"]))
        if dup:
            duplicate_count+=1; continue
        m=metadata_for_job(meta,v["filename"],vi,profile,ch,preview=preview_only)
        thumb=(thumbs[(cursor+vi)%len(thumbs)] if bool(profile.get("thumbnail_rotation",1)) else thumbs[0]) if thumbs and thumbnail_required_for_mode() else None
        matched={}
        if song_group:
            key=Path(clean(v["filename"])).stem
            candidates=[t for t in matched_thumbs if (clean(t.get("drive_file_id"))==fid
                        if source.get("local_songmaker_enabled") else _channel_norm_key(key) in _channel_norm_key(t.get("filename")))]
            if len(candidates)!=1:
                blocked.append(f"{v['filename']}: expected one original matching thumbnail.")
                continue
            matched=candidates[0]
            thumb_path=STAGING_ROOT/f"profile_{pid:04d}"/f"drive_{safe_slug(fid,'file')}"/"thumbnail"/clean(matched["filename"])
            thumb={"id":None,"stored_path":str(thumb_path),"original_filename":matched["filename"]}
        selected.append({
            "slot":slot,"channel_db_id":cid,"channel_id":channel_uc,"channel_name":clean(ch.get("channel_name")),
            "drive_video_id":int(v["id"]),"drive_file_id":fid,"filename":clean(v["filename"]),
            "normalized_filename":clean(v["normalized_filename"]),"script_key":clean(v["script_key"]),
            "size_bytes":int(v.get("size_bytes") or 0),"source_folder_id":clean(v["source_folder_id"]),
            "thumbnail_asset_id":int(thumb["id"]) if thumb and thumb.get("id") else None,
            "matched_thumbnail":matched,
            "thumbnail_path":clean(thumb["stored_path"]) if thumb else "",
            "thumbnail_filename":clean(thumb["original_filename"]) if thumb else "",
            **m,
        })
        per_channel[cid]=per_channel.get(cid,0)+1; claimed.add(fid)
    if len(selected)<target:
        blocked.append(f"Only {len(selected)} of {target} requested jobs are currently eligible.")
    if any(len(x["title"])>100 or not x["title"] for x in selected):
        blocked.append("One or more resolved titles are invalid.")
    if not preview_only and blocked:
        raise ValueError("Queue blocked: "+" | ".join(blocked))
    return {
        "jobs":selected,"target_jobs":target,"duplicates_blocked":duplicate_count,
        "blocked":blocked,"ready":not blocked and len(selected)>0,"metadata_note":note,
        "eligible_channels":len(channels),"available_videos":len(videos),"active_thumbnails":len(matched_thumbs) if song_group else len(thumbs),
    }


def generate_queue(pc_db:Any, store:V6Store, profile:Dict[str,Any], *, run_key:str|None=None, max_jobs_override:int|None=None) -> Dict[str,Any]:
    pid=int(profile["id"])
    plan=planner(pc_db,store,profile,max_jobs_override=max_jobs_override,preview_only=False)
    claimed_run=False
    if run_key:
        claimed_run=pc_db.claim_auto_engine_run(pid,run_key)
        if not claimed_run:
            return {"created":0,"duplicates_blocked":0,"already_claimed":True}
    created=[]; duplicates=0
    try:
        for job in plan["jobs"]:
            staging_dir=STAGING_ROOT/f"profile_{pid:04d}"/f"drive_{safe_slug(job['drive_file_id'],'file')}"
            staging_path=staging_dir/job["filename"]
            payload={
                "local_video_path":str(staging_path),
                "thumbnail_path":job["thumbnail_path"],
                "title":job["title"],"description":job["description"],"tags":job["tags"],
                "category_id":job["category_id"],"privacy_status":job["privacy_status"],
                "publish_at":None,"app_upload_at":None,"made_for_kids":job["made_for_kids"],
                "playlist_id":job["playlist_id"],"channel_db_id":job["channel_db_id"],
                "group_name":clean(profile.get("group_name") or profile.get("category_name") or "Auto Engine"),
                "status":"QUEUED","auto_engine_profile_id":pid,
                "auto_engine_profile_name":clean(profile.get("category_name") or "Auto Engine"),
                # VPS Drive lifecycle is handled after remote verification.
                "auto_engine_after_upload_action":"keep",
            }
            try:
                vid=int(pc_db.add_video(payload))
            except DuplicateUploadError:
                duplicates+=1; continue
            store.reserve_job_source(
                video_id=vid,profile_id=pid,
                drive_video={
                    "id":job["drive_video_id"],"drive_file_id":job["drive_file_id"],
                    "source_folder_id":job["source_folder_id"],"filename":job["filename"],
                    "normalized_filename":job["normalized_filename"],"script_key":job["script_key"],
                },
                thumb_id=job["thumbnail_asset_id"],staging_path=staging_path,
                after_success=clean(profile.get("after_upload_action") or "move_uploaded"),
            )
            if job.get("matched_thumbnail"):
                t=job["matched_thumbnail"]
                try:
                    with store.connect() as con:
                        con.execute("""INSERT INTO channel_job_meta(
                            video_id,profile_id,metadata_video_key,metadata_row_number,
                            thumbnail_drive_file_id,thumbnail_source_folder_id,thumbnail_filename,strict_thumbnail,
                            snapshot_title,snapshot_description,snapshot_tags,snapshot_privacy,post_upload_state)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (vid,pid,job["drive_file_id"],0,t["drive_file_id"],t["source_folder_id"],t["filename"],1,
                             job["title"],job["description"],job["tags"],job["privacy_status"],"QUEUED"))
                except Exception:
                    pc_db.delete_auto_engine_jobs([vid])
                    raise
            created.append(vid)
        if created and bool(profile.get("thumbnail_rotation",1)):
            pc_db.advance_auto_engine_thumbnail_cursor(pid,len(created))
        if run_key: pc_db.record_auto_engine_run(pid,run_key,len(created))
        return {"created":len(created),"video_ids":created,"duplicates_blocked":duplicates+plan["duplicates_blocked"],"already_claimed":False}
    except Exception:
        if run_key and claimed_run:
            pc_db.record_auto_engine_run(pid,run_key,len(created))
        raise


def mark_schedule_skipped_today(pc_db:Any, profile:Dict[str,Any]) -> int:
    pid=int(profile["id"]); today=datetime.now(ZoneInfo("Asia/Karachi")).date().isoformat()
    times=parse_schedule_times(profile.get("schedule_times"))[:max(1,int(profile.get("daily_upload_count") or 1))]
    marked=0
    for idx,alarm in enumerate(times,1):
        key=f"{today} slot-{idx} {alarm}"
        if pc_db.auto_engine_run_exists(pid,key): continue
        pc_db.record_auto_engine_run(pid,key,0); marked+=1
    return marked


def preflight_profile(pc_db:Any,store:V6Store,profile:Dict[str,Any]) -> Dict[str,Any]:
    result={"errors":[],"warnings":[]}
    try: scan=store.scan_drive_videos(int(profile["id"])); result["video_scan"]=scan
    except Exception as exc: result["errors"].append(f"Drive video source: {exc}")
    try:
        src=store.source(int(profile["id"]))
        if os.environ.get("LUM_ENGINE_INSTANCE")!="song" and clean(src.get("drive_thumbnail_folder_id")) and clean(src.get("thumbnail_source_mode")) in {"drive","pc_drive"}:
            result["thumbnail_sync"]=store.sync_drive_thumbnails(int(profile["id"]),clean(profile.get("category_name")))
    except Exception as exc: result["warnings"].append(f"Drive thumbnail sync: {exc}")
    try:
        plan=planner(pc_db,store,profile,preview_only=True)
        result["plan"]=plan
        result["errors"].extend(plan["blocked"])
    except Exception as exc: result["errors"].append(str(exc))
    try:
        free=shutil.disk_usage(STAGING_ROOT).free
        result["free_bytes"]=free
        max_size=max([int(v.get("size_bytes") or 0) for v in store.drive_videos(int(profile["id"]))] or [0])
        if max_size and free < max_size + 1024*1024*1024:
            result["errors"].append("Insufficient VPS free space for largest indexed video plus 1 GB safety margin.")
        elif free < 2*1024*1024*1024:
            result["warnings"].append("Less than 2 GB free in Auto Engine staging filesystem.")
    except Exception as exc: result["warnings"].append(f"Disk check failed: {exc}")
    result["ok"]=not result["errors"]
    return result


def queue_preflight(pc_db:Any,store:V6Store) -> Dict[str,Any]:
    issues=[]; checked=0
    statuses={"QUEUED","WAITING_SLOT","SCHEDULED","PAUSED","FAILED"}
    for raw in pc_db.list_auto_engine_jobs(limit=5000):
        if clean(raw.get("status")).upper() not in statuses:
            continue
        checked+=1; vid=int(raw.get("id") or 0); src=store.job_source(vid)
        def add(severity,code,message): issues.append({"video_id":vid,"severity":severity,"code":code,"message":message})
        if not src:
            add("ERROR","V6_SOURCE_MISSING","Drive source mapping is missing."); continue
        if not clean(src.get("drive_file_id")):
            add("ERROR","DRIVE_ID_MISSING","Drive File ID/reference is missing.")
        if len(clean(raw.get("title")))==0 or len(clean(raw.get("title")))>100:
            add("ERROR","TITLE_INVALID","Resolved title is empty or longer than 100 characters.")
        if len(str(raw.get("description") or ""))>5000:
            add("ERROR","DESCRIPTION_TOO_LONG","Description exceeds 5000 characters.")
        if len(str(raw.get("tags") or "").encode("utf-8"))>500:
            add("WARNING","TAGS_LARGE","Combined tags are unusually large and may be rejected by YouTube.")
        if clean(raw.get("channel_status")).upper()!="ACTIVE":
            add("ERROR","CHANNEL_INACTIVE",f"Channel status is {clean(raw.get('channel_status')) or 'UNKNOWN'}.")
        if not is_verified_channel_id(clean(raw.get("youtube_channel_id") or raw.get("channel_id"))):
            add("ERROR","CHANNEL_UNVERIFIED","Channel has no verified UC YouTube Channel ID.")
        if classify_google_json(clean(raw.get("token_file_path")))!=VALID_TOKEN:
            add("ERROR","TOKEN_INVALID","Authorized token is missing or invalid.")
        thumb=store.thumb(int(src.get("thumbnail_asset_id") or 0)) if src.get("thumbnail_asset_id") else None
        if not thumb:
            add("ERROR","THUMBNAIL_MISSING","Assigned thumbnail asset is missing.")
        else:
            path=Path(clean(thumb.get("stored_path")))
            if not path.is_file(): add("ERROR","THUMBNAIL_FILE_MISSING","Assigned thumbnail file is missing on VPS.")
            elif path.stat().st_size>2*1024*1024: add("ERROR","THUMBNAIL_TOO_LARGE","Assigned thumbnail exceeds 2 MB.")
        with store.connect() as con:
            drive_row=con.execute("SELECT present,size_bytes FROM drive_videos WHERE id=?",(int(src.get("drive_video_id") or 0),)).fetchone()
        if not drive_row or int(drive_row["present"] or 0)!=1:
            add("WARNING","DRIVE_SOURCE_NOT_INDEXED","Drive video is not present in the latest source index; re-run Sync & Scan Sources.")
    errors=sum(1 for x in issues if x["severity"]=="ERROR")
    warnings=sum(1 for x in issues if x["severity"]=="WARNING")
    return {"ok":errors==0,"checked":checked,"errors":errors,"warnings":warnings,"issues":issues[:200]}


def record_stats(store:V6Store) -> Dict[str,int]:
    now=datetime.now(timezone.utc)
    rows=store.history_records(100000)
    parsed=[]
    for r in rows:
        try: dt=datetime.fromisoformat(clean(r.get("uploaded_at")).replace("Z","+00:00")); dt=dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception: continue
        parsed.append(dt.astimezone(timezone.utc))
    today=now.date(); yesterday=today-timedelta(days=1)
    return {
        "total":len(rows),
        "today":sum(1 for d in parsed if d.date()==today),
        "yesterday":sum(1 for d in parsed if d.date()==yesterday),
        "days7":sum(1 for d in parsed if d>=now-timedelta(days=7)),
        "days30":sum(1 for d in parsed if d>=now-timedelta(days=30)),
        "days90":sum(1 for d in parsed if d>=now-timedelta(days=90)),
        "days365":sum(1 for d in parsed if d>=now-timedelta(days=365)),
    }


def service_state(name:str) -> str:
    try:
        proc=subprocess.run(["systemctl","is-active",name],capture_output=True,text=True,timeout=3)
        return clean(proc.stdout) or ("inactive" if proc.returncode else "active")
    except Exception:
        return "unknown"


def profile_context(pc_db:Any,store:V6Store,profile_id:int=0) -> Dict[str,Any]:
    store.import_pc_filename_history(pc_db)
    raw_profiles=pc_db.list_auto_engine_profiles()
    profiles=[]
    for item in raw_profiles:
        pr=dict(item)
        pid=int(pr.get("id") or 0)
        try:
            with store.connect() as con:
                pr["v6_video_count"]=int(con.execute("SELECT COUNT(*) FROM drive_videos WHERE profile_id=? AND present=1",(pid,)).fetchone()[0])
                pr["v6_thumb_count"]=int(con.execute("SELECT COUNT(*) FROM thumbnails WHERE profile_id=? AND status='ACTIVE'",(pid,)).fetchone()[0])
        except Exception:
            pr["v6_video_count"]=0; pr["v6_thumb_count"]=0
        profiles.append(pr)
    selected=pc_db.get_auto_engine_profile(int(profile_id)) if profile_id else (profiles[0] if profiles else None)
    if selected:
        selected_id=int(selected.get("id") or 0)
        selected_match=next((dict(x) for x in profiles if int(x.get("id") or 0)==selected_id),None)
        if selected_match:
            selected_match.update(dict(selected))
            selected=selected_match
        source=store.source(int(selected["id"]))
        all_thumbs=store.thumbs(int(selected["id"]),include_trashed=True)
        thumbs=[t for t in all_thumbs if t.get("status")!="TRASHED"]
        trash=[t for t in all_thumbs if t.get("status")=="TRASHED"]
        videos=store.drive_videos(int(selected["id"]))
        try: eligible=len(target_channels(pc_db,selected))
        except Exception: eligible=0
    else:
        source={}; thumbs=[]; trash=[]; videos=[]; eligible=0
    queue=[]
    for job in pc_db.list_auto_engine_jobs(limit=500):
        row=dict(job)
        src=store.job_source(int(row.get("id") or 0))
        if src:
            row["v6_state"]=clean(src.get("state"))
            row["drive_file_id"]=clean(src.get("drive_file_id"))
            row["source_filename"]=clean(src.get("filename"))
            row["script_key"]=clean(src.get("script_key"))
            row["after_success"]=clean(src.get("desired_after_success") or row.get("after_upload_action") or "move_uploaded")
            if src.get("thumbnail_asset_id"):
                thumb=store.thumb(int(src["thumbnail_asset_id"]))
                row["thumbnail_filename"]=clean((thumb or {}).get("original_filename"))
        queue.append(row)
    unit_names = auto_engine_service_units()
    return {
        "profiles":profiles,"selected":selected,"source":source,"thumbnails":thumbs,"thumbnail_trash":trash,
        "videos":videos,"eligible_channels":eligible,"groups":pc_db.list_channel_groups(),
        "queue":queue,"records":store.history_records(500),"record_stats":record_stats(store),
        "missed":store.missed(),"engine":store.engine(),
        "worker_service":service_state(unit_names["worker"]),
        "scheduler_service":service_state(unit_names["scheduler"]),
    }
# LUM_CHANNEL_ID_AUTO_ENGINE_V1
# Channel-based, per-row metadata sync layered on top of the proven V6 upload engine.

_CHANNEL_LEGACY_INIT_SCHEMA = V6Store.init_schema
_CHANNEL_LEGACY_PROFILE_CONTEXT = profile_context
_CHANNEL_LEGACY_PREFLIGHT = preflight_profile
_CHANNEL_LEGACY_QUEUE_PREFLIGHT = queue_preflight
_CHANNEL_LEGACY_PLANNER = planner
_CHANNEL_LEGACY_GENERATE = generate_queue
_CHANNEL_LEGACY_TARGET_CHANNELS = target_channels
_CHANNEL_LEGACY_SAVE_PROFILE = save_profile_from_form


def _truthy(value: Any) -> bool:
    return clean(value).casefold() in {"1", "true", "yes", "y", "on", "enabled", "checked"}


def _channel_norm_key(value: Any) -> str:
    return clean(value).casefold()


def _channel_parse_date(value: Any) -> Optional[datetime.date]:
    text = clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        raise ValueError(f"Invalid Upload Date: {text}. Use YYYY-MM-DD when possible.")


def _channel_parse_time(value: Any) -> Optional[datetime.time]:
    text = clean(value)
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(text.upper(), fmt).time().replace(second=0, microsecond=0)
        except Exception:
            pass
    raise ValueError(f"Invalid Upload Time: {text}. Use HH:MM or h:mm AM/PM.")


def _channel_schedule_at(date_value: Any, time_value: Any, timezone_name: str) -> str:
    d = _channel_parse_date(date_value)
    t = _channel_parse_time(time_value)
    if not d or not t:
        return ""
    try:
        tz = ZoneInfo(clean(timezone_name) or "Asia/Karachi")
    except Exception:
        tz = ZoneInfo("Asia/Karachi")
    return datetime.combine(d, t, tzinfo=tz).replace(second=0, microsecond=0).isoformat()


def _channel_row_value(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = clean(row.get(key))
        if value:
            return value
    return ""


def _channel_fetch_rows(profile: Dict[str, Any], videos: Optional[List[Dict[str, Any]]]=None,
                        binding: Optional[Dict[str, Any]]=None) -> List[Dict[str, str]]:
    settings=binding or {}
    if clean(settings.get("metadata_source"))=="manual":
        title=clean(settings.get("manual_title"))
        if not title:
            raise ValueError("Manual metadata title must be saved before syncing.")
        result=[]
        for video in (videos or []):
            key=Path(clean(video.get("filename"))).stem
            if not key:
                continue
            result.append({
                "video_id":key, "title":title,
                "description":clean(settings.get("manual_description")),
                "category_id":clean(settings.get("manual_category_id")) or "10",
                "upload_date":clean(settings.get("manual_upload_date")),
                "upload_time":clean(settings.get("manual_upload_time")),
                "privacy_status":"private", "upload_enabled":"YES", "done":"YES",
            })
        return result
    sheet = clean(profile.get("metadata_sheet_url_or_id"))
    tab = clean(profile.get("metadata_sheet_tab"))
    gid = clean(profile.get("metadata_sheet_gid")) or (extract_gid(sheet, fallback="") if sheet else "")
    if not sheet:
        raise ValueError("Metadata Google Sheet URL / ID is required.")
    try:
        if gid and gid.isdigit():
            rows = fetch_sheet_csv_gid(sheet, gid, timeout=18)
        elif tab:
            rows = fetch_sheet_csv_tab(sheet, tab, timeout=18)
        else:
            raise ValueError("Metadata sub-sheet tab or GID is required.")
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    if not rows:
        raise ValueError("Metadata sub-sheet has no filled rows.")
    return [dict(r) for r in rows]


def _channel_init_schema(self: V6Store) -> None:
    _CHANNEL_LEGACY_INIT_SCHEMA(self)
    with self.connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS song_folder_imports(
            profile_id INTEGER NOT NULL,drive_file_id TEXT NOT NULL,owner TEXT NOT NULL,
            folder_id TEXT NOT NULL,song_job_id TEXT NOT NULL,consumer TEXT NOT NULL,
            PRIMARY KEY(profile_id,drive_file_id)
        );
        CREATE TABLE IF NOT EXISTS song_group_settings(
            profile_id INTEGER PRIMARY KEY, settings_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS channel_profile_bindings(
            profile_id INTEGER PRIMARY KEY,
            channel_db_id INTEGER NOT NULL UNIQUE,
            timezone_name TEXT NOT NULL DEFAULT 'Asia/Karachi',
            sync_interval_minutes INTEGER NOT NULL DEFAULT 15,
            paused INTEGER NOT NULL DEFAULT 0,
            pause_reason TEXT NOT NULL DEFAULT '',
            last_sync_at TEXT,
            next_sync_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_channel_profile_sync
            ON channel_profile_bindings(paused,next_sync_at,profile_id);

        CREATE TABLE IF NOT EXISTS channel_metadata_rows(
            profile_id INTEGER NOT NULL,
            row_number INTEGER NOT NULL,
            video_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            seo_keywords TEXT NOT NULL DEFAULT '',
            upload_date TEXT NOT NULL DEFAULT '',
            upload_time TEXT NOT NULL DEFAULT '',
            privacy_status TEXT NOT NULL DEFAULT 'private',
            upload_enabled INTEGER NOT NULL DEFAULT 0,
            source_status TEXT NOT NULL DEFAULT '',
            source_youtube_url TEXT NOT NULL DEFAULT '',
            schedule_at TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'SEEN',
            error_message TEXT NOT NULL DEFAULT '',
            video_filename TEXT NOT NULL DEFAULT '',
            thumbnail_filename TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(profile_id,row_number)
        );
        CREATE INDEX IF NOT EXISTS idx_channel_metadata_video_key
            ON channel_metadata_rows(profile_id,video_key,row_number);
        CREATE INDEX IF NOT EXISTS idx_channel_metadata_state
            ON channel_metadata_rows(profile_id,state,row_number);

        CREATE TABLE IF NOT EXISTS drive_thumbnails(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            drive_file_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            normalized_filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            modified_at TEXT,
            source_folder_id TEXT NOT NULL,
            present INTEGER NOT NULL DEFAULT 1,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(profile_id,drive_file_id)
        );
        CREATE INDEX IF NOT EXISTS idx_channel_drive_thumbs
            ON drive_thumbnails(profile_id,present,normalized_filename);

        CREATE TABLE IF NOT EXISTS channel_job_meta(
            video_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL,
            metadata_video_key TEXT NOT NULL,
            metadata_row_number INTEGER NOT NULL,
            thumbnail_drive_file_id TEXT NOT NULL,
            thumbnail_source_folder_id TEXT NOT NULL,
            thumbnail_filename TEXT NOT NULL,
            strict_thumbnail INTEGER NOT NULL DEFAULT 1,
            auto_retry_count INTEGER NOT NULL DEFAULT 0,
            retry_not_before TEXT NOT NULL DEFAULT '',
            post_upload_state TEXT NOT NULL DEFAULT '',
            archive_video_destination TEXT NOT NULL DEFAULT '',
            archive_thumbnail_destination TEXT NOT NULL DEFAULT '',
            snapshot_title TEXT NOT NULL DEFAULT '',
            snapshot_description TEXT NOT NULL DEFAULT '',
            snapshot_tags TEXT NOT NULL DEFAULT '',
            snapshot_privacy TEXT NOT NULL DEFAULT 'private',
            snapshot_schedule_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(profile_id,metadata_video_key)
        );
        CREATE INDEX IF NOT EXISTS idx_channel_job_retry
            ON channel_job_meta(post_upload_state,retry_not_before,video_id);
        """)
        meta_cols={str(r[1]) for r in con.execute("PRAGMA table_info(channel_metadata_rows)").fetchall()}
        if "source_done" not in meta_cols:
            con.execute("ALTER TABLE channel_metadata_rows ADD COLUMN source_done TEXT NOT NULL DEFAULT ''")
        job_cols={str(r[1]) for r in con.execute("PRAGMA table_info(channel_job_meta)").fetchall()}
        if "metadata_sheet_url" not in job_cols:
            con.execute("ALTER TABLE channel_job_meta ADD COLUMN metadata_sheet_url TEXT NOT NULL DEFAULT ''")
        if "metadata_sheet_gid" not in job_cols:
            con.execute("ALTER TABLE channel_job_meta ADD COLUMN metadata_sheet_gid TEXT NOT NULL DEFAULT ''")
        source_cols={str(r[1]) for r in con.execute("PRAGMA table_info(profile_sources)").fetchall()}
        if "local_songmaker_enabled" not in source_cols:
            con.execute("ALTER TABLE profile_sources ADD COLUMN local_songmaker_enabled INTEGER NOT NULL DEFAULT 0")
        # Additive migration: all existing profiles continue using Google Sheets.
        binding_cols={str(r[1]) for r in con.execute("PRAGMA table_info(channel_profile_bindings)").fetchall()}
        additions={
            "metadata_source":"TEXT NOT NULL DEFAULT 'sheet'",
            "manual_title":"TEXT NOT NULL DEFAULT ''",
            "manual_description":"TEXT NOT NULL DEFAULT ''",
            "manual_category_id":"TEXT NOT NULL DEFAULT '10'",
            "manual_upload_date":"TEXT NOT NULL DEFAULT ''",
            "manual_upload_time":"TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in additions.items():
            if column not in binding_cols:
                con.execute(f"ALTER TABLE channel_profile_bindings ADD COLUMN {column} {definition}")


def _channel_binding(self: V6Store, profile_id: int) -> Dict[str, Any]:
    with self.connect() as con:
        row = con.execute("SELECT * FROM channel_profile_bindings WHERE profile_id=?", (int(profile_id),)).fetchone()
        return dict(row) if row else {}


def _channel_binding_for_channel(self: V6Store, channel_db_id: int) -> Dict[str, Any]:
    with self.connect() as con:
        row = con.execute("SELECT * FROM channel_profile_bindings WHERE channel_db_id=?", (int(channel_db_id),)).fetchone()
        return dict(row) if row else {}


def _channel_save_binding(self: V6Store, profile_id: int, channel_db_id: int, *, timezone_name: str="Asia/Karachi", sync_interval_minutes: int=15,
                          metadata_source: str="sheet", manual_title: str="", manual_description: str="",
                          manual_category_id: str="10", manual_upload_date: str="", manual_upload_time: str="") -> None:
    interval = max(1, min(1440, int(sync_interval_minutes or 15)))
    tz_name = clean(timezone_name) or "Asia/Karachi"
    try:
        ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(f"Invalid channel timezone: {tz_name}") from exc
    with self.connect() as con:
        other = con.execute("SELECT profile_id FROM channel_profile_bindings WHERE channel_db_id=? AND profile_id<>?", (int(channel_db_id), int(profile_id))).fetchone()
        if other:
            raise ValueError(f"This channel is already assigned to Auto Engine profile #{int(other['profile_id'])}.")
        con.execute("""
        INSERT INTO channel_profile_bindings(profile_id,channel_db_id,timezone_name,sync_interval_minutes,paused,pause_reason,next_sync_at,updated_at)
        VALUES(?,?,?,?,0,'','',CURRENT_TIMESTAMP)
        ON CONFLICT(profile_id) DO UPDATE SET
            channel_db_id=excluded.channel_db_id,
            timezone_name=excluded.timezone_name,
            sync_interval_minutes=excluded.sync_interval_minutes,
            updated_at=CURRENT_TIMESTAMP
        """, (int(profile_id), int(channel_db_id), tz_name, interval))
        con.execute("""UPDATE channel_profile_bindings SET metadata_source=?,manual_title=?,manual_description=?,
                       manual_category_id=?,manual_upload_date=?,manual_upload_time=?,updated_at=CURRENT_TIMESTAMP
                       WHERE profile_id=?""",
                    (metadata_source,manual_title,manual_description,manual_category_id,
                     manual_upload_date,manual_upload_time,int(profile_id)))


def _channel_set_pause(self: V6Store, profile_id: int, paused: bool, reason: str="") -> None:
    with self.connect() as con:
        con.execute("UPDATE channel_profile_bindings SET paused=?,pause_reason=?,updated_at=CURRENT_TIMESTAMP WHERE profile_id=?", (1 if paused else 0, clean(reason)[:1000], int(profile_id)))


def _channel_sync_due(self: V6Store, profile_id: int) -> bool:
    b = self.channel_binding(profile_id)
    if not b or int(b.get("paused") or 0):
        return False
    next_at = clean(b.get("next_sync_at"))
    if not next_at:
        return True
    try:
        dt = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return True


def _channel_mark_synced(self: V6Store, profile_id: int) -> None:
    b = self.channel_binding(profile_id)
    interval = max(1, int(b.get("sync_interval_minutes") or 15))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    nxt = now + timedelta(minutes=interval)
    with self.connect() as con:
        con.execute("UPDATE channel_profile_bindings SET last_sync_at=?,next_sync_at=?,updated_at=CURRENT_TIMESTAMP WHERE profile_id=?", (now.isoformat(), nxt.isoformat(), int(profile_id)))


def _channel_scan_thumbnails(self: V6Store, profile_id: int) -> Dict[str, Any]:
    src = self.source(profile_id)
    folder_id = clean(src.get("drive_thumbnail_folder_id"))
    if src.get("local_songmaker_enabled"):
        return self.scan_songmaker_local(profile_id, thumbnail=True)
    if not folder_id:
        raise ValueError("Google Drive Thumbnail Folder is required.")
    rows = drive_lsjson(folder_id)
    files=[]
    for r in rows:
        if bool(r.get("IsDir")):
            continue
        name=clean(r.get("Name") or r.get("Path"))
        if Path(name).suffix.lower() not in THUMB_EXTS:
            continue
        files.append(r)
    with self.connect() as con:
        con.execute("UPDATE drive_thumbnails SET present=0 WHERE profile_id=? AND source_folder_id<>'songmaker-local'", (int(profile_id),))
        for r in files:
            name=clean(r.get("Name") or r.get("Path"))
            fid=clean(r.get("ID") or r.get("Id") or r.get("id"))
            if not fid:
                fid="name:"+hashlib.sha256((folder_id+"|"+name).encode()).hexdigest()[:32]
            con.execute("""
            INSERT INTO drive_thumbnails(profile_id,drive_file_id,filename,normalized_filename,size_bytes,modified_at,source_folder_id,present,last_seen_at)
            VALUES(?,?,?,?,?,?,?,1,CURRENT_TIMESTAMP)
            ON CONFLICT(profile_id,drive_file_id) DO UPDATE SET
                filename=excluded.filename,normalized_filename=excluded.normalized_filename,
                size_bytes=excluded.size_bytes,modified_at=excluded.modified_at,
                source_folder_id=excluded.source_folder_id,present=1,last_seen_at=CURRENT_TIMESTAMP
            """, (int(profile_id),fid,name,normalize_filename(name),int(r.get("Size") or 0),clean(r.get("ModTime")),folder_id))
    return {"thumbnails":len(files),"folder_id":folder_id}


def _channel_drive_thumbnails(self: V6Store, profile_id: int) -> List[Dict[str, Any]]:
    local=bool(self.source(profile_id).get("local_songmaker_enabled"))
    clause="source_folder_id='songmaker-local'" if local else "source_folder_id<>'songmaker-local'"
    params=[int(profile_id)];settings=self.group_settings(profile_id)
    if settings.get("video_source_mode")=="songfolder":
        clause+=" AND drive_file_id IN (SELECT drive_file_id FROM song_folder_imports WHERE profile_id=? AND folder_id=? AND owner=?)"
        params.extend([int(profile_id),settings["song_folder_id"],settings["song_folder_owner"]])
    with self.connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM drive_thumbnails WHERE profile_id=? AND present=1 AND "+clause+" ORDER BY normalized_filename,id",params).fetchall()]


def _channel_metadata_rows(self: V6Store, profile_id: int) -> List[Dict[str, Any]]:
    with self.connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM channel_metadata_rows WHERE profile_id=? ORDER BY row_number", (int(profile_id),)).fetchall()]


def _channel_job_meta(self: V6Store, video_id: int) -> Dict[str, Any]:
    with self.connect() as con:
        row=con.execute("SELECT * FROM channel_job_meta WHERE video_id=?", (int(video_id),)).fetchone()
        return dict(row) if row else {}


def _channel_is_profile(self: V6Store, profile_id: int) -> bool:
    return bool(self.channel_binding(profile_id))


V6Store.init_schema = _channel_init_schema
V6Store.channel_binding = _channel_binding
V6Store.channel_binding_for_channel = _channel_binding_for_channel
V6Store.save_channel_binding = _channel_save_binding
V6Store.set_channel_pause = _channel_set_pause
V6Store.channel_sync_due = _channel_sync_due
V6Store.mark_channel_synced = _channel_mark_synced
V6Store.scan_drive_thumbnails = _channel_scan_thumbnails
V6Store.drive_thumbnails = _channel_drive_thumbnails
V6Store.channel_metadata_rows = _channel_metadata_rows
V6Store.channel_job_meta = _channel_job_meta
V6Store.is_channel_profile = _channel_is_profile


def _channel_get_channel(pc_db: Any, channel_db_id: int) -> Optional[Dict[str, Any]]:
    try:
        rows=pc_db.list_channels(active_only=False)
    except TypeError:
        rows=pc_db.list_channels()
    for row in rows:
        if int(row.get("id") or 0)==int(channel_db_id):
            return dict(row)
    return None


def _song_source_settings(form: Dict[str,Any]) -> Dict[str,Any]:
    video=clean(form.get("video_source_mode")) or "drive"
    meta=clean(form.get("metadata_source")) or "sheet"
    if video not in {"drive","songmaker","songfolder"} or meta not in {"sheet","manual"}:
        raise ValueError("Choose a valid video and metadata source.")
    settings={key:clean(form.get(key)) for key in
              ("manual_title","manual_description","manual_category_id")}
    settings.update(video_source_mode=video,metadata_source=meta)
    if video=="songfolder":
        from .songmaker_local import folder_selection
        settings.update(folder_selection(clean(form.get("_song_owner")),clean(form.get("song_folder_id"))))
    settings["manual_category_id"]=settings["manual_category_id"] or "10"
    if meta=="manual":
        if not settings["manual_title"] or len(settings["manual_title"])>100:
            raise ValueError("Manual title is required and must be at most 100 characters.")
        if len(settings["manual_description"])>5000:
            raise ValueError("Manual description cannot exceed 5000 characters.")
        if settings["manual_category_id"] not in {"1","2","10","15","17","19","20","22","23","24","25","26","27","28","29"}:
            raise ValueError("Choose a valid YouTube category.")
    return settings


def short_group_metadata(store: V6Store, profile_id: int, settings: Dict[str, Any]|None=None) -> Dict[str, Any]:
    """Additive profile settings; channel bindings and historical jobs stay intact."""
    with store.connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS short_group_metadata (profile_id INTEGER PRIMARY KEY, settings_json TEXT NOT NULL)")
        if settings is not None:
            con.execute("INSERT INTO short_group_metadata VALUES (?,?) ON CONFLICT(profile_id) DO UPDATE SET settings_json=excluded.settings_json",
                        (int(profile_id), json.dumps(settings)))
        row=con.execute("SELECT settings_json FROM short_group_metadata WHERE profile_id=?", (int(profile_id),)).fetchone()
        return json.loads(row[0]) if row else {"metadata_source":"sheet"}


def save_short_group_profile(pc_db: Any, store: V6Store, form: Dict[str, Any], profile_id: int|None=None) -> int:
    original=pc_db.get_auto_engine_profile(int(profile_id)) if profile_id else None
    if profile_id and not original:
        raise ValueError("Profile no longer exists.")
    if clean(form.get("channel_db_id")) or clean(form.get("profile_kind")) not in {"", "group"}:
        raise ValueError("New Short Uploader profiles require a channel group.")
    group=clean(form.get("channel_group_id"))
    if not group.isdigit() or not any(int(g["id"])==int(group) for g in pc_db.list_channel_groups()):
        raise ValueError("Select a valid channel group.")
    if not pc_db.list_channel_group_members(int(group)):
        raise ValueError("Selected group has no channels.")
    if not clean(form.get("category_name")):
        raise ValueError("Enter group profile name.")
    saved=short_group_metadata(store, int(profile_id)) if profile_id else {}
    settings=dict(saved)
    for key in ("metadata_source", "manual_title", "manual_description", "manual_category_id"):
        if key in form:
            settings[key]=clean(form[key])
    settings.setdefault("metadata_source", "sheet")
    settings.setdefault("manual_category_id", "22")
    source=settings["metadata_source"]
    if source not in {"sheet", "manual"}:
        raise ValueError("Select Google Sheets or Manual Metadata.")
    if source=="manual":
        if not settings.get("manual_title") or len(settings["manual_title"])>100:
            raise ValueError("Manual title is required and must be at most 100 characters.")
        if len(settings.get("manual_description", ""))>5000:
            raise ValueError("Manual description cannot exceed 5000 characters.")
        if settings["manual_category_id"] not in {"1","2","10","15","17","19","20","22","23","24","25","26","27","28","29"}:
            raise ValueError("Choose a valid YouTube category.")
    fields=dict(form)
    for key in ("metadata_sheet_url_or_id", "metadata_sheet_tab", "metadata_sheet_gid"):
        if key not in fields:
            fields[key]=(original or {}).get(key, "")
    if source=="sheet" and (not clean(fields.get("metadata_sheet_url_or_id")) or not clean(fields.get("metadata_sheet_tab"))):
        raise ValueError("Google Sheet URL and metadata tab are required.")
    folder=clean(form.get("short_vps_folder"))
    if folder:
        if not shortstudio_folder_path(folder).is_dir():
            raise ValueError("Short Maker VPS folder does not exist.")
    elif not clean(form.get("drive_video_folder")):
        raise ValueError("Choose a Short Maker VPS folder or Google Drive folder.")
    # Explicit profile opt-in never changes the global scheduler arm state.
    fields["schedule_enabled"]="1" if clean(form.get("schedule_enabled")).lower() in {"1","true","on","yes"} else "0"
    pid=_CHANNEL_LEGACY_SAVE_PROFILE(pc_db, store, fields, profile_id)
    short_group_metadata(store, pid, settings)
    return pid


def save_profile_from_form(pc_db: Any, store: V6Store, form: Dict[str, Any], profile_id: int|None=None) -> int:
    if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio":
        binding=store.channel_binding(int(profile_id)) if profile_id else {}
        if not binding:
            return save_short_group_profile(pc_db, store, form, profile_id)
        if clean(form.get("channel_db_id"))!=str(binding["channel_db_id"]):
            raise ValueError("Existing channel profile must keep its channel; create a new group profile.")
    raw_channel=clean(form.get("channel_db_id"))
    if profile_id and os.environ.get("LUM_ENGINE_INSTANCE")=="song":
        saved_owner=store.group_settings(int(profile_id)).get("song_folder_owner")
        if saved_owner and saved_owner!=clean(form.get("_song_owner")):
            raise ValueError("This folder profile belongs to another account.")
    # Restore group-based profiles for Song Uploader without affecting per-channel
    # Video-ID bindings or the other independent uploader instances.
    if os.environ.get("LUM_ENGINE_INSTANCE") == "song":
        kind=clean(form.get("profile_kind")).lower()
        if kind not in {"", "channel", "group"}:
            raise ValueError("Invalid profile type.")
        original=pc_db.get_auto_engine_profile(int(profile_id)) if profile_id else None
        if profile_id and not original:
            raise ValueError("Profile no longer exists.")
        bound=bool(store.channel_binding(int(profile_id))) if profile_id else False
        if kind == "group" or (not kind and not raw_channel):
            if bound or raw_channel:
                raise ValueError("Channel profile cannot be converted to group; create another profile.")
            group_id=clean(form.get("channel_group_id"))
            if not group_id and not (original and not original.get("channel_group_id")):
                raise ValueError("Select a channel group.")
            if group_id:
                if not group_id.isdigit() or int(group_id)<=0:
                    raise ValueError("Invalid channel group.")
                if not any(int(g.get("id") or 0)==int(group_id) for g in pc_db.list_channel_groups()):
                    raise ValueError("Selected channel group no longer exists.")
                if not pc_db.list_channel_group_members(int(group_id)):
                    raise ValueError("Selected group has no channels.")
            settings=_song_source_settings(form)
            if not clean(form.get("category_name")):
                raise ValueError("Enter group profile name.")
            if settings["metadata_source"]=="sheet" and not clean(form.get("metadata_sheet_tab")):
                raise ValueError("Group profile needs a metadata Sheet tab.")
            if settings["metadata_source"]=="sheet" and not clean(form.get("metadata_sheet_url_or_id")):
                raise ValueError("Metadata Sheet URL / ID is required.")
            if settings["video_source_mode"]=="drive" and (not clean(form.get("drive_video_folder")) or not clean(form.get("drive_thumbnail_folder"))):
                raise ValueError("Select video and thumbnail Drive folders.")
            pid=_CHANNEL_LEGACY_SAVE_PROFILE(pc_db, store, form, profile_id)
            store.save_source(pid,video_folder=clean(form.get("drive_video_folder")),
                              thumb_folder=clean(form.get("drive_thumbnail_folder")),thumb_mode="drive",
                              songmaker_enabled=settings["video_source_mode"] in {"songmaker","songfolder"})
            store.save_group_settings(pid,settings)
            return pid
        if not profile_id:
            raise ValueError("New Song Uploader profiles require a channel group.")
        if kind == "channel" and not raw_channel:
            raise ValueError("Select a channel for this profile.")
        if profile_id and not bound:
            raise ValueError("Group profile cannot be converted to channel; create another profile.")
    if not raw_channel:
        return _CHANNEL_LEGACY_SAVE_PROFILE(pc_db, store, form, profile_id)
    if not raw_channel.isdigit() or int(raw_channel)<=0:
        raise ValueError("Select one channel for this Auto Engine profile.")
    channel_db_id=int(raw_channel)
    channel=_channel_get_channel(pc_db, channel_db_id)
    if not channel:
        raise ValueError("Selected channel was not found in Channels.")
    existing=store.channel_binding_for_channel(channel_db_id)
    if existing and int(existing.get("profile_id") or 0)!=(int(profile_id or 0)):
        raise ValueError(f"Channel is already assigned to Auto Engine profile #{int(existing['profile_id'])}.")
    if profile_id:
        current_binding=store.channel_binding(int(profile_id))
        if current_binding and int(current_binding.get("channel_db_id") or 0)!=channel_db_id:
            try: existing_jobs=int(pc_db.count_auto_engine_jobs(int(profile_id)))
            except Exception: existing_jobs=0
            if existing_jobs:
                raise ValueError("Channel cannot be changed after this profile has queue/history jobs. Create a new channel profile instead.")

    profile_name=clean(form.get("category_name")) or clean(channel.get("channel_name")) or f"Channel {channel_db_id}"
    is_song=os.environ.get("LUM_ENGINE_INSTANCE")=="song"
    video_source=clean(form.get("video_source_mode")) or "drive"
    if is_song and video_source not in {"drive","songmaker","songfolder"}:
        raise ValueError("Select Drive folders or Song Maker videos.")
    folder_settings={}
    if is_song and video_source=="songfolder":
        from .songmaker_local import folder_selection
        folder_settings=folder_selection(clean(form.get("_song_owner")),clean(form.get("song_folder_id")))
        folder_settings["video_source_mode"]="songfolder"
    metadata_source=(clean(form.get("metadata_source")) or "sheet") if is_song else "sheet"
    if metadata_source not in {"sheet","manual"}:
        raise ValueError("Select Google Sheet or Manual Website metadata.")
    manual_title=clean(form.get("manual_title"))
    manual_description=clean(form.get("manual_description"))
    manual_category_id=clean(form.get("manual_category_id")) or "10"
    upload_date=clean(form.get("manual_upload_date"))
    upload_time=clean(form.get("manual_upload_time"))
    if metadata_source=="manual":
        if not manual_title or len(manual_title)>100:
            raise ValueError("Manual title is required and must be at most 100 characters.")
        if len(manual_description)>5000:
            raise ValueError("Manual description cannot exceed 5000 characters.")
        if manual_category_id not in {"1","2","10","15","17","19","20","22","23","24","25","26","27","28","29"}:
            raise ValueError("Choose a valid YouTube category.")
        if bool(upload_date)!=bool(upload_time):
            raise ValueError("Set both manual upload date and time, or leave both blank.")
        if upload_date:
            _channel_schedule_at(upload_date,upload_time,clean(form.get("timezone_name")) or "Asia/Karachi")
    data={
        "category_name":profile_name,
        "video_folder":"",
        "thumbnail_folder":"",
        "metadata_sheet_url_or_id":clean(form.get("metadata_sheet_url_or_id")),
        "metadata_sheet_tab":clean(form.get("metadata_sheet_tab")),
        "metadata_sheet_gid":clean(form.get("metadata_sheet_gid")) or extract_gid(clean(form.get("metadata_sheet_url_or_id")), fallback=""),
        "channel_group_id":None,
        "daily_upload_count":1,
        "schedule_times":"09:00",
        "schedule_clock_type":"24h",
        "upload_scope":"total_category",
        "work_mode":"auto",
        "after_upload_action":"move_uploaded",
        "auto_select_video":True,
        "thumbnail_rotation":False,
        "enabled":_truthy(form.get("enabled")) if form.get("enabled") is not None else True,
        "schedule_enabled":True,
        "missed_schedule_policy":"run_missed_today",
        "notes":clean(form.get("notes")),
    }
    created_new=not bool(profile_id)
    pid=int(pc_db.save_auto_engine_profile(data,profile_id))
    try:
        store.save_source(pid,
            video_folder=clean(form.get("drive_video_folder")),
            thumb_folder=clean(form.get("drive_thumbnail_folder")),
            thumb_mode="drive",
            short_folder=clean(form.get("short_vps_folder")) if os.environ.get("LUM_ENGINE_INSTANCE") == "shortstudio" else "",
            songmaker_enabled=(video_source in {"songmaker","songfolder"}) if is_song else None,
        )
        store.save_channel_binding(
            pid,channel_db_id,
            timezone_name=clean(form.get("timezone_name")) or "Asia/Karachi",
            sync_interval_minutes=max(1,int(form.get("sync_interval_minutes") or 15)),
            metadata_source=metadata_source,manual_title=manual_title if metadata_source=="manual" else "",
            manual_description=manual_description if metadata_source=="manual" else "",
            manual_category_id=manual_category_id,manual_upload_date=upload_date if metadata_source=="manual" else "",
            manual_upload_time=upload_time if metadata_source=="manual" else "",
        )
        if is_song: store.save_group_settings(pid,folder_settings)
    except Exception:
        if created_new:
            try: pc_db.delete_auto_engine_profile(pid)
            except Exception: pass
        raise
    return pid


def target_channels(pc_db: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    pid=int(profile.get("id") or 0)
    # Global default V6Store uses the production DB and only reads this additive binding table.
    try:
        binding=V6Store().channel_binding(pid)
    except Exception:
        binding={}
    if not binding:
        return _CHANNEL_LEGACY_TARGET_CHANNELS(pc_db, profile)
    channel=_channel_get_channel(pc_db,int(binding.get("channel_db_id") or 0))
    if not channel:
        return []
    if clean(channel.get("status")).upper()!="ACTIVE":
        return []
    if not is_verified_channel_id(clean(channel.get("channel_id"))):
        return []
    if classify_google_json(clean(channel.get("token_file_path"))) != VALID_TOKEN:
        return []
    return [channel]


def _channel_pause_pending_jobs(pc_db: Any, profile_id: int, reason: str) -> None:
    try:
        with pc_db.connect() as con:
            con.execute("""
                UPDATE videos SET status='PAUSED',error_message=?,claim_worker_id=NULL,claimed_at=NULL,lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE auto_engine_profile_id=? AND status IN ('QUEUED','SCHEDULED','WAITING_SLOT')
            """,(clean(reason)[:1000],int(profile_id)))
    except Exception:
        pass


def _channel_validate_rows(pc_db: Any, store: V6Store, profile: Dict[str, Any]) -> Dict[str, Any]:
    pid=int(profile.get("id") or 0)
    binding=store.channel_binding(pid)
    if not binding:
        raise ValueError("Channel binding is missing for this Auto Engine profile.")
    channel=_channel_get_channel(pc_db,int(binding.get("channel_db_id") or 0))
    if not channel:
        raise ValueError("Assigned channel no longer exists in Channels.")
    if clean(channel.get("status")).upper()!="ACTIVE":
        reason="Channel profile is offline/inactive. Manual Resume is required after it returns."
        store.set_channel_pause(pid,True,reason); _channel_pause_pending_jobs(pc_db,pid,reason)
        raise ValueError("Assigned channel is inactive/offline; this channel profile is paused.")
    if not is_verified_channel_id(clean(channel.get("channel_id"))):
        reason="Channel ID is not verified."
        store.set_channel_pause(pid,True,reason); _channel_pause_pending_jobs(pc_db,pid,reason)
        raise ValueError("Assigned channel has no verified YouTube UC ID; this channel profile is paused.")
    if classify_google_json(clean(channel.get("token_file_path"))) != VALID_TOKEN:
        reason="Channel token is unavailable/invalid."
        store.set_channel_pause(pid,True,reason); _channel_pause_pending_jobs(pc_db,pid,reason)
        raise ValueError("Assigned channel token is unavailable/invalid; this channel profile is paused.")
    if int(binding.get("paused") or 0):
        raise ValueError(f"Channel profile is paused: {clean(binding.get('pause_reason')) or 'manual resume required'}")

    store.scan_drive_videos(pid)
    if thumbnail_required_for_mode():
        store.scan_drive_thumbnails(pid)
    videos=store.drive_videos(pid,True)
    thumbs=store.drive_thumbnails(pid) if thumbnail_required_for_mode() else []
    rows=_channel_fetch_rows(profile,videos,binding)
    timezone_name=clean(binding.get("timezone_name")) or "Asia/Karachi"
    seen_keys=set()
    output=[]
    with store.connect() as con:
        con.execute("DELETE FROM channel_metadata_rows WHERE profile_id=?", (pid,))
        for row_no,row in enumerate(rows,2):
            key=_channel_row_value(row,"video_id","id","videoid")
            title=_channel_row_value(row,"title","video_title")
            desc=_channel_row_value(row,"description","desc")
            if clean(binding.get("metadata_source"))=="manual":
                video_name=next((clean(v.get("filename")) for v in videos
                                 if Path(clean(v.get("filename"))).stem.casefold()==key.casefold()), key+".mp4")
                title=apply_vars(title,video_name,row_no-2,profile,channel)
                desc=apply_vars(desc,video_name,row_no-2,profile,channel)
            tags=_channel_row_value(row,"tags")
            seo=""
            done_raw=_channel_row_value(row,"done")
            source_done=_truthy(done_raw)
            date_text=_channel_row_value(row,"upload_date","date")
            time_text=_channel_row_value(row,"upload_time","time")
            privacy=_channel_row_value(row,"privacy","privacy_status","visibility").lower() or "private"
            enabled_raw=_channel_row_value(row,"upload_enabled","enabled")
            upload_enabled=_truthy(enabled_raw)
            source_status=_channel_row_value(row,"status")
            source_url=_channel_row_value(row,"youtube_url","url")
            state="READY"; error=""; schedule_at=""; vname=""; tname=""
            if not key:
                state="ERROR"; error="Missing Video ID."
            elif _channel_norm_key(key) in seen_keys:
                state="IGNORED_DUPLICATE_SHEET_ID"; error="Duplicate Video ID in Sheet; first row is authoritative."
            else:
                seen_keys.add(_channel_norm_key(key))
                if source_status.casefold() in {"uploaded","published","completed"} and source_url:
                    state="UPLOADED"
                elif not upload_enabled:
                    state="DISABLED"
                elif not source_done:
                    state="WAITING_AUTOSCRIPT"
                elif not title or len(title)>100:
                    state="ERROR"; error="Title is empty or exceeds 100 characters."
                elif len(desc)>5000:
                    state="ERROR"; error="Description exceeds 5000 characters."
                elif privacy not in {"private","public","unlisted"}:
                    state="ERROR"; error="Privacy must be private, public, or unlisted."
                else:
                    try:
                        schedule_at=_channel_schedule_at(date_text,time_text,timezone_name)
                    except Exception as exc:
                        state="ERROR"; error=str(exc)
                    if state=="READY" and not schedule_at:
                        state="UNSCHEDULED"
                    token=_channel_norm_key(key)
                    vm=[v for v in videos if token and token in clean(v.get("filename")).casefold()]
                    tm=[t for t in thumbs if token and token in clean(t.get("filename")).casefold()]
                    if state in {"READY","UNSCHEDULED"}:
                        if len(vm)==0:
                            state="ERROR"; error="Matching video is missing."
                        elif len(vm)>1:
                            state="ERROR"; error=f"Multiple videos match Video ID ({len(vm)} found)."
                        else:
                            vname=clean(vm[0].get("filename"))
                        if thumbnail_required_for_mode():
                            if len(tm)==0:
                                state="ERROR"; error=(error+" " if error else "")+"Matching thumbnail is missing."
                            elif len(tm)>1:
                                state="ERROR"; error=(error+" " if error else "")+f"Multiple thumbnails match Video ID ({len(tm)} found)."
                            else:
                                tname=clean(tm[0].get("filename"))
                                thumb_bytes=int(tm[0].get("size_bytes") or 0)
                                if thumb_bytes > 2 * 1024 * 1024:
                                    state="ERROR"; error=(error+" " if error else "")+"Matching thumbnail exceeds 2 MB."
                                elif thumb_bytes <= 0:
                                    state="ERROR"; error=(error+" " if error else "")+"Matching thumbnail is empty or its size is unavailable."
            con.execute("""
            INSERT INTO channel_metadata_rows(profile_id,row_number,video_key,title,description,tags,seo_keywords,source_done,
                upload_date,upload_time,privacy_status,upload_enabled,source_status,source_youtube_url,
                schedule_at,state,error_message,video_filename,thumbnail_filename,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """,(pid,row_no,key,title,desc,tags,seo,"YES" if source_done else "",date_text,time_text,privacy,1 if upload_enabled else 0,
                  source_status,source_url,schedule_at,state,error,vname,tname))
            output.append({"row_number":row_no,"video_key":key,"title":title,"description":desc,"tags":tags,
                           "category_id":_channel_row_value(row,"category_id","youtube_category") or "22",
                           "seo_keywords":"","source_done":source_done,"privacy_status":privacy,"upload_enabled":upload_enabled,
                           "schedule_at":schedule_at,"state":state,"error_message":error,
                           "video_filename":vname,"thumbnail_filename":tname})
    return {"profile_id":pid,"channel":channel,"binding":binding,"rows":output,"videos":videos,"thumbnails":thumbs}


SHEET_WRITEBACK_PATH = Path(os.environ.get("LUM_SHEET_WRITEBACK_PATH", "/var/lib/automation-shared/lum_sheet_writebacks.jsonl"))
SHEET_WRITEBACK_LOCK = threading.Lock()


def emit_channel_sheet_writeback(store: V6Store, video_id: int, status: str, youtube_url: str="", error: str="") -> bool:
    meta=store.channel_job_meta(int(video_id))
    if not meta:
        return False
    sheet_url=clean(meta.get("metadata_sheet_url"))
    sheet_gid=clean(meta.get("metadata_sheet_gid"))
    video_key=clean(meta.get("metadata_video_key"))
    row_number=int(meta.get("metadata_row_number") or 0)
    if not sheet_url or not sheet_gid or not video_key or row_number < 2:
        return False
    payload={
        "event_id":str(uuid.uuid4()),
        "sheet_url":sheet_url,
        "sheet_gid":sheet_gid,
        "row_number":row_number,
        "video_id":video_key,
        "status":clean(status),
        "youtube_url":clean(youtube_url),
        "error":clean(error)[:4000],
        "created_at":utcnow(),
    }
    path=SHEET_WRITEBACK_PATH
    path.parent.mkdir(parents=True,exist_ok=True)
    line=json.dumps(payload,ensure_ascii=False,separators=(",",":"))+"\n"
    with SHEET_WRITEBACK_LOCK:
        with path.open("a",encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        try: os.chmod(path,0o644)
        except Exception: pass
    return True


def _channel_find_asset(rows: List[Dict[str,Any]], key: str, filename_field: str="filename") -> Dict[str,Any]:
    token=_channel_norm_key(key)
    matches=[r for r in rows if token and token in clean(r.get(filename_field)).casefold()]
    return dict(matches[0]) if len(matches)==1 else {}


def _channel_queue_valid_rows(pc_db: Any, store: V6Store, profile: Dict[str,Any], sync_result: Dict[str,Any], *, only_video_key: str="") -> Dict[str,Any]:
    pid=int(profile["id"])
    channel=dict(sync_result["channel"])
    videos=list(sync_result["videos"]); thumbs=list(sync_result["thumbnails"])
    created=[]; blocked=[]; now_utc=datetime.now(timezone.utc)
    ordered_rows=sorted(sync_result["rows"],key=lambda r:(clean(r.get("schedule_at")) or "9999",_channel_norm_key(r.get("video_key"))))
    for row in ordered_rows:
        key=clean(row.get("video_key"))
        if only_video_key and _channel_norm_key(key)!=_channel_norm_key(only_video_key):
            continue
        if clean(row.get("state"))!="READY":
            continue
        with store.connect() as con:
            existing=con.execute("SELECT video_id FROM channel_job_meta WHERE profile_id=? AND lower(metadata_video_key)=lower(?)",(pid,key)).fetchone()
        if existing:
            continue
        v=_channel_find_asset(videos,key); t=_channel_find_asset(thumbs,key)
        if not v or (thumbnail_required_for_mode() and not t) or not clean(row.get("title")):
            blocked.append(key); continue
        staging_dir=STAGING_ROOT/f"profile_{pid:04d}"/f"drive_{safe_slug(clean(v.get('drive_file_id')),'file')}"
        staging_path=staging_dir/clean(v.get("filename"))
        thumb_dir=staging_dir/"thumbnail"
        thumb_path=thumb_dir/clean(t.get("filename")) if thumbnail_required_for_mode() else ""
        schedule_at=clean(row.get("schedule_at"))
        try:
            due=datetime.fromisoformat(schedule_at.replace("Z","+00:00"))
            if due.tzinfo is None: due=due.replace(tzinfo=timezone.utc)
            initial_status="SCHEDULED" if due.astimezone(timezone.utc)>now_utc else "QUEUED"
        except Exception:
            continue
        tags=clean(row.get("tags"))
        payload={
            "local_video_path":str(staging_path),"thumbnail_path":str(thumb_path),
            "title":clean(row.get("title")),"description":clean(row.get("description")),"tags":tags,
            "category_id":clean(row.get("category_id")) or "22","privacy_status":clean(row.get("privacy_status")) or "private",
            "publish_at":None,"app_upload_at":schedule_at if initial_status=="SCHEDULED" else None,
            "made_for_kids":0,"playlist_id":"","channel_db_id":int(channel.get("id") or 0),
            "group_name":"Auto Engine","status":initial_status,"auto_engine_profile_id":pid,
            "auto_engine_profile_name":clean(channel.get("channel_name")) or clean(profile.get("category_name")) or "Auto Engine",
            "auto_engine_after_upload_action":"keep",
        }
        # Manual metadata never writes back to a historical Sheet, even if the
        # profile retains that Sheet connection for switching back later.
        use_manual_metadata=clean(store.channel_binding(pid).get("metadata_source"))=="manual"
        metadata_sheet_url="" if use_manual_metadata else clean(profile.get("metadata_sheet_url_or_id"))
        metadata_sheet_gid="" if use_manual_metadata else (clean(profile.get("metadata_sheet_gid")) or
                             (extract_gid(metadata_sheet_url,fallback="") if metadata_sheet_url else ""))
        try:
            vid=int(pc_db.add_video(payload))
        except DuplicateUploadError:
            continue
        try:
            store.reserve_job_source(
                video_id=vid,profile_id=pid,
                drive_video={"id":int(v["id"]),"drive_file_id":clean(v["drive_file_id"]),
                    "source_folder_id":clean(v["source_folder_id"]),"filename":clean(v["filename"]),
                    "normalized_filename":clean(v["normalized_filename"]),"script_key":clean(v["script_key"])},
                thumb_id=None,staging_path=staging_path,after_success="move_uploaded",
            )
            with store.connect() as con:
                con.execute("""
                INSERT INTO channel_job_meta(video_id,profile_id,metadata_video_key,metadata_row_number,
                    thumbnail_drive_file_id,thumbnail_source_folder_id,thumbnail_filename,strict_thumbnail,
                    metadata_sheet_url,metadata_sheet_gid,
                    snapshot_title,snapshot_description,snapshot_tags,snapshot_privacy,snapshot_schedule_at,post_upload_state)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED')
                """,(vid,pid,key,int(row.get("row_number") or 0),clean(t.get("drive_file_id")),clean(t.get("source_folder_id")),
                      clean(t.get("filename")),int(thumbnail_required_for_mode()),metadata_sheet_url,metadata_sheet_gid,clean(row.get("title")),clean(row.get("description")),tags,
                      clean(row.get("privacy_status")) or "private",schedule_at))
                con.execute("UPDATE channel_metadata_rows SET state=?,error_message='' WHERE profile_id=? AND row_number=?",(initial_status,pid,int(row.get("row_number") or 0)))
            emit_channel_sheet_writeback(store,vid,initial_status,error="")
            created.append(vid)
        except Exception:
            try: pc_db.delete_auto_engine_jobs([vid])
            except Exception: pass
            raise
    return {"created":len(created),"video_ids":created,"blocked":blocked}


def sync_channel_profile(pc_db: Any, store: V6Store, profile: Dict[str,Any], *, queue_valid: bool=True, only_video_key: str="") -> Dict[str,Any]:
    result=_channel_validate_rows(pc_db,store,profile)
    queued={"created":0,"video_ids":[],"blocked":[]}
    if queue_valid:
        queued=_channel_queue_valid_rows(pc_db,store,profile,result,only_video_key=only_video_key)
        if queued["created"]:
            store.set_engine(running=True,pause_after_current=False)
    store.mark_channel_synced(int(profile["id"]))
    states={}
    for r in result["rows"]: states[r["state"]]=states.get(r["state"],0)+1
    return {"ok":True,"profile_id":int(profile["id"]),"created":queued["created"],"video_ids":queued["video_ids"],"states":states,
            "rows":result["rows"],"channel_name":clean(result["channel"].get("channel_name")),"channel_id":clean(result["channel"].get("channel_id"))}


def planner(pc_db: Any, store: V6Store, profile: Dict[str,Any], *, max_jobs_override:int|None=None, preview_only:bool=True) -> Dict[str,Any]:
    if not store.is_channel_profile(int(profile.get("id") or 0)):
        return _CHANNEL_LEGACY_PLANNER(pc_db,store,profile,max_jobs_override=max_jobs_override,preview_only=preview_only)
    sync=_channel_validate_rows(pc_db,store,profile)
    jobs=[r for r in sync["rows"] if r["state"]=="READY"]
    if max_jobs_override is not None: jobs=jobs[:max(1,int(max_jobs_override))]
    blocked=[f"Row {r['row_number']} · {r['video_key'] or 'no ID'}: {r['error_message']}" for r in sync["rows"] if r["state"]=="ERROR"]
    return {"jobs":jobs,"target_jobs":len(jobs),"duplicates_blocked":0,"blocked":blocked,"ready":bool(jobs) and not blocked,
            "metadata_note":"Per-video row metadata; Video ID is the matching key.","eligible_channels":1,
            "available_videos":len(sync["videos"]),"active_thumbnails":len(sync["thumbnails"]),
            "unscheduled":sum(1 for r in sync["rows"] if r["state"]=="UNSCHEDULED")}


def generate_queue(pc_db: Any, store: V6Store, profile: Dict[str,Any], *, run_key:str|None=None, max_jobs_override:int|None=None) -> Dict[str,Any]:
    if not store.is_channel_profile(int(profile.get("id") or 0)):
        return _CHANNEL_LEGACY_GENERATE(pc_db,store,profile,run_key=run_key,max_jobs_override=max_jobs_override)
    result=sync_channel_profile(pc_db,store,profile,queue_valid=True)
    return {"created":int(result.get("created") or 0),"video_ids":result.get("video_ids") or [],"duplicates_blocked":0,"already_claimed":False}


def preflight_profile(pc_db: Any, store: V6Store, profile: Dict[str,Any]) -> Dict[str,Any]:
    if not store.is_channel_profile(int(profile.get("id") or 0)):
        return _CHANNEL_LEGACY_PREFLIGHT(pc_db,store,profile)
    try:
        sync=sync_channel_profile(pc_db,store,profile,queue_valid=False)
        errors=[f"Row {r['row_number']} · {r['video_key'] or 'no ID'}: {r['error_message']}" for r in sync["rows"] if r["state"]=="ERROR"]
        warnings=[f"Row {r['row_number']} · {r['video_key']}: unscheduled" for r in sync["rows"] if r["state"]=="UNSCHEDULED"]
        warnings += [f"Row {r['row_number']} · {r['video_key']}: waiting for Auto Script Done=YES" for r in sync["rows"] if r["state"]=="WAITING_AUTOSCRIPT"]
        return {"ok":not errors,"errors":errors,"warnings":warnings,"sync":sync}
    except Exception as exc:
        return {"ok":False,"errors":[str(exc)],"warnings":[]}


def queue_preflight(pc_db: Any, store: V6Store) -> Dict[str,Any]:
    base=_CHANNEL_LEGACY_QUEUE_PREFLIGHT(pc_db,store)
    # New channel jobs are already blocked before queue if title/video/thumbnail are absent.
    return base


def profile_context(pc_db: Any, store: V6Store, profile_id:int=0, *, owner:str="") -> Dict[str,Any]:
    ctx=_CHANNEL_LEGACY_PROFILE_CONTEXT(pc_db,store,profile_id)
    try:
        all_channels=pc_db.list_channels(active_only=False)
    except TypeError:
        all_channels=pc_db.list_channels()
    ctx["all_channels"]=[dict(x) for x in all_channels]
    profiles=[]
    for raw in ctx.get("profiles") or []:
        pr=dict(raw); pid=int(pr.get("id") or 0); b=store.channel_binding(pid)
        if b:
            ch=_channel_get_channel(pc_db,int(b.get("channel_db_id") or 0)) or {}
            pr["v6_thumb_count"]=len(store.drive_thumbnails(pid))
            pr.update({"channel_db_id":int(b.get("channel_db_id") or 0),"bound_channel_name":clean(ch.get("channel_name")),
                       "bound_channel_id":clean(ch.get("channel_id")),"timezone_name":clean(b.get("timezone_name")) or "Asia/Karachi",
                       "sync_interval_minutes":int(b.get("sync_interval_minutes") or 15),"channel_paused":int(b.get("paused") or 0),
                       "channel_pause_reason":clean(b.get("pause_reason")),"last_sync_at":clean(b.get("last_sync_at")),"next_sync_at":clean(b.get("next_sync_at")),
                       "metadata_source":clean(b.get("metadata_source")) or "sheet",
                       "manual_title":clean(b.get("manual_title")),"manual_description":clean(b.get("manual_description")),
                       "manual_category_id":clean(b.get("manual_category_id")) or "10",
                       "manual_upload_date":clean(b.get("manual_upload_date")),
                       "manual_upload_time":clean(b.get("manual_upload_time"))})
        elif os.environ.get("LUM_ENGINE_INSTANCE")=="song":
            pr.update(store.group_settings(pid))


        if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio" and not b:
            pr.update(short_group_metadata(store, pid))
        settings=store.group_settings(pid) if os.environ.get("LUM_ENGINE_INSTANCE")=="song" else {}
        if settings.get("song_folder_id"):
            pr.update({k:settings[k] for k in ("song_folder_id","song_folder_name","video_source_mode")})
        profiles.append(pr)
    # STUDIO_USER_ISOLATION_V1: strict ACL. Legacy/unassigned resources
    # stay visible to admins only; do not infer ownership from song folder IDs.
    if owner:
        from .studio_tenants import StudioTenants
        acl=StudioTenants(pc_db)
        viewer={"username":owner,"is_admin":0}
        allowed_profiles=acl.allowed_ids("profile",viewer)
        allowed_channels=acl.allowed_ids("channel",viewer)
        allowed_groups=acl.allowed_ids("group",viewer)
        hidden_ids={int(pr["id"]) for pr in profiles
                    if int(pr["id"]) not in allowed_profiles}
        ctx["all_channels"]=[ch for ch in ctx.get("all_channels",[])
                             if int(ch.get("id") or 0) in allowed_channels]
        ctx["groups"]=[g for g in ctx.get("groups",[])
                       if int(g.get("id") or 0) in allowed_groups]
    else:
        hidden_ids=set()
    profiles=[pr for pr in profiles if int(pr["id"]) not in hidden_ids]
    ctx["profiles"]=profiles
    if ctx.get("selected") and int(ctx["selected"]["id"]) in hidden_ids:
        ctx.update(selected=None,source={},videos=[],thumbnails=[],thumbnail_trash=[],eligible_channels=0)
    # If the global default belongs to someone else, load only this user's
    # first owned profile rather than showing a blank/stale editor.
    if owner and not ctx.get("selected") and profiles:
        own_first=profiles[0]
        own_id=int(own_first["id"])
        thumbnails=store.thumbs(own_id,include_trashed=True)
        ctx.update(selected=own_first,source=store.source(own_id),
                   thumbnails=[row for row in thumbnails if row.get("status")!="TRASHED"],
                   thumbnail_trash=[row for row in thumbnails if row.get("status")=="TRASHED"],
                   videos=store.drive_videos(own_id),
                   eligible_channels=len(target_channels(pc_db,own_first)))
    for key in ("queue","records"):
        ctx[key]=[row for row in ctx.get(key,[])
                  if int(row.get("auto_engine_profile_id") or row.get("profile_id") or 0)
                  not in hidden_ids and (not owner or
                     int(row.get("auto_engine_profile_id") or row.get("profile_id") or 0)
                     in allowed_profiles)]
    if owner:
        # Dashboard aggregate counters can contain cross-tenant history.
        stats=ctx.get("record_stats") or {}
        ctx["record_stats"]={key:0 for key in stats}
        ctx["missed"]=[]
    selected=ctx.get("selected")
    if selected:
        sid=int(selected.get("id") or 0); match=next((x for x in profiles if int(x.get("id") or 0)==sid),None)
        if match: ctx["selected"]=match; selected=match
        b=store.channel_binding(sid)
        ctx["binding"]=b
        ctx["metadata_rows"]=store.channel_metadata_rows(sid) if b else []
        ctx["drive_thumbnails_index"]=store.drive_thumbnails(sid) if b else []
        if b:
            ctx["eligible_channels"]=1 if target_channels(pc_db,selected) else 0
    else:
        ctx["binding"]={}; ctx["metadata_rows"]=[]; ctx["drive_thumbnails_index"]=[]
    # Decorate queue with immutable Video ID snapshot metadata.
    for q in ctx.get("queue") or []:
        meta=store.channel_job_meta(int(q.get("id") or 0))
        if meta:
            q["metadata_video_key"]=clean(meta.get("metadata_video_key")); q["channel_job"]=True
            q["thumbnail_filename"]=clean(meta.get("thumbnail_filename"))
            q["post_upload_state"]=clean(meta.get("post_upload_state"))
    return ctx


def channel_profile_scheduler_tick(pc_db: Any, store: V6Store, profile: Dict[str,Any]) -> int:
    pid=int(profile.get("id") or 0)
    if not store.is_channel_profile(pid):
        return -1
    if int(profile.get("enabled") or 0)!=1:
        return 0
    binding=store.channel_binding(pid)
    if int(binding.get("paused") or 0):
        return 0
    if not store.channel_sync_due(pid):
        return 0
    try:
        result=sync_channel_profile(pc_db,store,profile,queue_valid=True)
        return int(result.get("created") or 0)
    except Exception as exc:
        pc_db.add_log(None,"ERROR",f"Auto Engine channel sync {profile.get('category_name')}: {exc}",None)
        return 0


def resume_channel_profile(store: V6Store, profile_id: int) -> None:
    store.set_channel_pause(int(profile_id),False,"")
    with store.connect() as con:
        con.execute("UPDATE channel_profile_bindings SET next_sync_at='',updated_at=CURRENT_TIMESTAMP WHERE profile_id=?",(int(profile_id),))


def download_channel_thumbnail_for_job(store: V6Store, video_id: int) -> str:
    meta=store.channel_job_meta(video_id)
    if not meta:
        return ""
    source=store.job_source(video_id)
    if not source:
        raise RuntimeError("Auto Engine source mapping is missing.")
    target=Path(clean(source.get("staging_path"))).parent/"thumbnail"/clean(meta.get("thumbnail_filename"))
    folder=clean(meta.get("thumbnail_source_folder_id")); name=clean(meta.get("thumbnail_filename"))
    if not folder or not name:
        raise RuntimeError("Matched thumbnail Drive reference is missing.")
    if Path(name).suffix.lower() not in THUMB_EXTS:
        raise RuntimeError("Matched thumbnail must be JPG/JPEG/PNG.")
    try:
        if not target.is_file():
            if folder=="songmaker-local":
                raise RuntimeError("Local Song Maker thumbnail missing; Drive fallback is forbidden")
            drive_download(folder,name,target)
        if not target.is_file() or not 0 < target.stat().st_size <= 2 * 1024 * 1024:
            raise RuntimeError("Matched thumbnail is missing, empty or exceeds 2 MB.")
        width,height=_image_dimensions(target)
        if width < 120 or height < 68:
            raise RuntimeError("Matched thumbnail dimensions are too small.")
    except Exception as exc:
        # Never allow a corrupt or oversized cached thumbnail into YouTube's
        # upload stage, even when an earlier Drive download appeared successful.
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Matched thumbnail validation failed: {exc}") from exc
    return str(target)


def set_channel_job_post_state(store: V6Store, video_id: int, state: str, *, retry_count: Optional[int]=None, retry_not_before: str="", video_dest: str="", thumb_dest: str="") -> None:
    parts=["post_upload_state=?","updated_at=CURRENT_TIMESTAMP"]; params=[clean(state)]
    if retry_count is not None: parts.append("auto_retry_count=?"); params.append(int(retry_count))
    if retry_not_before!="": parts.append("retry_not_before=?"); params.append(clean(retry_not_before))
    if video_dest!="": parts.append("archive_video_destination=?"); params.append(clean(video_dest))
    if thumb_dest!="": parts.append("archive_thumbnail_destination=?"); params.append(clean(thumb_dest))
    params.append(int(video_id))
    with store.connect() as con:
        con.execute(f"UPDATE channel_job_meta SET {', '.join(parts)} WHERE video_id=?",params)


# LUM_CHANNEL_ID_AUTO_ENGINE_V1_ARCHIVE
def _drive_lsjson_path(folder_id: str, relative_path: str) -> List[Dict[str,Any]]:
    cfg=rclone_config()
    path=clean(relative_path).strip("/")
    remote=f"{rclone_remote()}:{path}" if path else f"{rclone_remote()}:"
    cmd=["rclone","--config",str(cfg),"lsjson",remote,"--drive-root-folder-id",folder_id,"--max-depth","1"]
    out=_run(cmd,timeout=90).stdout
    rows=json.loads(out or "[]")
    return [dict(x) for x in rows if isinstance(x,dict)]


def drive_move_uploaded_month(folder_id: str, filename: str, drive_file_id: str="", year_month: str="") -> str:
    month=clean(year_month) or datetime.now(timezone.utc).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}",month):
        raise ValueError("Archive month must be YYYY-MM.")
    rel=f"Uploaded/{month}"
    _run(["rclone","--config",str(rclone_config()),"mkdir",f"{rclone_remote()}:{rel}","--drive-root-folder-id",folder_id],timeout=60)
    root_rows=drive_lsjson(folder_id)
    dest_rows=_drive_lsjson_path(folder_id,rel)
    wanted_id=clean(drive_file_id)
    if wanted_id and not wanted_id.startswith("name:"):
        found=next((r for r in dest_rows if not bool(r.get("IsDir")) and _drive_item_id(r)==wanted_id),None)
        if found:
            return f"{rel}/{_drive_item_name(found)}"
    source=None
    if wanted_id and not wanted_id.startswith("name:"):
        source=next((r for r in root_rows if not bool(r.get("IsDir")) and _drive_item_id(r)==wanted_id),None)
    if source is None:
        source=next((r for r in root_rows if not bool(r.get("IsDir")) and _drive_item_name(r)==filename),None)
    if source is None:
        exact=next((r for r in dest_rows if not bool(r.get("IsDir")) and _drive_item_name(r)==filename),None)
        if exact:
            return f"{rel}/{filename}"
        raise FileNotFoundError(f"Drive source is missing from source and {rel}/: {filename}")
    current_name=_drive_item_name(source) or filename
    target_name=current_name
    used={_drive_item_name(r).casefold():r for r in dest_rows if not bool(r.get("IsDir"))}
    if target_name.casefold() in used:
        p=Path(target_name); suffix=safe_slug(wanted_id[-8:] or str(int(time.time())),"dup")
        target_name=f"{p.stem}__{suffix}{p.suffix}"
    _run(["rclone","--config",str(rclone_config()),"moveto",f"{rclone_remote()}:{current_name}",f"{rclone_remote()}:{rel}/{target_name}","--drive-root-folder-id",folder_id,"--retries","3","--low-level-retries","10"],timeout=300)
    return f"{rel}/{target_name}"

# LUM_CHANNEL_ID_AUTO_ENGINE_V1_PREFLIGHT_OVERRIDE
def queue_preflight(pc_db: Any, store: V6Store) -> Dict[str,Any]:
    base=_CHANNEL_LEGACY_QUEUE_PREFLIGHT(pc_db,store)
    filtered=[]
    for issue in base.get("issues") or []:
        # Shorts never require a thumbnail, including legacy V6 queue jobs.
        if not thumbnail_required_for_mode() and clean(issue.get("code")) in {"THUMBNAIL_MISSING","THUMBNAIL_FILE_MISSING"}:
            continue
        vid=int(issue.get("video_id") or 0)
        meta=store.channel_job_meta(vid)
        if thumbnail_required_for_mode() and meta and clean(issue.get("code")) in {"THUMBNAIL_MISSING","THUMBNAIL_FILE_MISSING"}:
            # Engine 2 keeps matched thumbnails in Drive until claim. Suppress
            # only this expected local-file warning if the *same* eligible
            # thumbnail remains indexed in the right channel folder.
            with store.connect() as con:
                row=con.execute(
                    "SELECT present,filename,source_folder_id,size_bytes FROM drive_thumbnails WHERE profile_id=? AND drive_file_id=?",
                    (int(meta.get("profile_id") or 0),clean(meta.get("thumbnail_drive_file_id"))),
                ).fetchone()
            if (row and int(row["present"] or 0)==1
                    and clean(row["filename"])==clean(meta.get("thumbnail_filename"))
                    and clean(row["source_folder_id"])==clean(meta.get("thumbnail_source_folder_id"))
                    and Path(clean(row["filename"])).suffix.lower() in THUMB_EXTS
                    and 0 < int(row["size_bytes"] or 0) <= 2 * 1024 * 1024):
                continue
        filtered.append(issue)
    errors=sum(1 for x in filtered if clean(x.get("severity")).upper()=="ERROR")
    warnings=sum(1 for x in filtered if clean(x.get("severity")).upper()=="WARNING")
    return {**base,"ok":errors==0,"errors":errors,"warnings":warnings,"issues":filtered}

