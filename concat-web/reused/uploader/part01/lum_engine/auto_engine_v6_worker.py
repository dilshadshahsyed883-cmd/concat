from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from desktop_core.modules.database import Database
from desktop_core.modules.path_utils import DB_PATH as PC_DB_PATH
from desktop_core.modules.youtube_uploader import YouTubeUploader
from desktop_core.modules.upload_pipeline import UploadStopped
from desktop_core.modules.upload_status import UploadStatus

from .auto_engine_v6 import (STAGING_ROOT, V6Store, drive_delete_source, drive_download, drive_move_uploaded, drive_move_uploaded_month, clean, download_channel_thumbnail_for_job, set_channel_job_post_state, emit_channel_sheet_writeback, SHORT_FOLDER_PREFIX, shortstudio_source_path, shortstudio_file_id)


STOP=False


def _sig(_s,_f):
    global STOP
    STOP=True


for s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(s,_sig)


# LUM_AUTO_PROXY_COOLDOWN_V1
PROXY_WAIT_PREFIX="AUTO_PROXY_WAIT|"
PROXY_RETRY_SECONDS=(120,300,600)


def _proxy_group_key(raw: object) -> str:
    value=clean(raw).strip()
    if not value:
        return ""
    # Decodo style: user-splmc54bis-ip-x.x.x.x -> splmc54bis.
    low=value.lower()
    if low.startswith("user-"):
        value=value[5:]
    for marker in ("-ip-","-country-","-city-","-state-","-session-","-sessid-"):
        idx=value.lower().find(marker)
        if idx>0:
            value=value[:idx]
            break
    value=re.sub(r"[^A-Za-z0-9_.@+-]+","_",value).strip("_| ")
    return value[:120]


def _proxy_marker(key:str,attempt:int,retry_at:datetime) -> str:
    return f"{PROXY_WAIT_PREFIX}{key}|{max(1,int(attempt))}|{retry_at.astimezone(timezone.utc).isoformat(timespec='seconds')}"


def _parse_proxy_marker(text: object):
    raw=clean(text)
    if not raw.startswith(PROXY_WAIT_PREFIX):
        return None
    try:
        _prefix,key,attempt,retry_at=raw.split("|",3)
        when=datetime.fromisoformat(retry_at.replace("Z","+00:00"))
        if when.tzinfo is None:
            when=when.replace(tzinfo=timezone.utc)
        return key,max(1,int(attempt)),when.astimezone(timezone.utc)
    except Exception:
        return None


def _proxy_delay(attempt:int) -> int:
    idx=min(max(1,int(attempt))-1,len(PROXY_RETRY_SECONDS)-1)
    return int(PROXY_RETRY_SECONDS[idx])


def _sql_update_video_ids(ids, *, status:str, error_message:str, progress_zero:bool=False) -> None:
    ids=[int(x) for x in ids if int(x)>0]
    if not ids:
        return
    con=sqlite3.connect(str(PC_DB_PATH),timeout=30)
    try:
        marks=','.join('?' for _ in ids)
        progress=', progress=0' if progress_zero else ''
        con.execute(
            f"UPDATE videos SET status=?, error_message=?{progress}, claim_worker_id=NULL, claimed_at=NULL, lease_expires_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id IN ({marks})",
            [status,error_message,*ids],
        )
        con.commit()
    finally:
        con.close()


def _auto_jobs(db:Database):
    try:
        return list(db.list_auto_engine_jobs(limit=5000))
    except Exception:
        return []


def park_proxy_group(db:Database,store:V6Store,key:str,attempt:int,retry_at:datetime) -> int:
    marker=_proxy_marker(key,attempt,retry_at)
    ids=[]
    for row in _auto_jobs(db):
        status=clean(row.get("status")).upper()
        if status not in {"QUEUED","WAITING_SLOT","PAUSED","WAITING_NETWORK"}:
            continue
        row_key=_proxy_group_key(row.get("proxy_username"))
        if not row_key:
            row_key=clean(row.get("proxy_host"))+":"+clean(row.get("proxy_port"))
        if row_key != key:
            continue
        # Do not hijack unrelated manually-paused jobs.
        if status=="PAUSED" and not clean(row.get("error_message")).startswith(PROXY_WAIT_PREFIX):
            continue
        ids.append(int(row["id"]))
    _sql_update_video_ids(ids,status=UploadStatus.WAITING_NETWORK.value,error_message=marker)
    for vid in ids:
        try: store.set_job_state(vid,"WAITING_PROXY",error=f"Proxy group {key} cooling down; automatic retry scheduled.")
        except Exception: pass
        try: db.add_log(vid,"WARNING",f"Proxy group {key} deferred; other proxy groups continue. Automatic retry scheduled.",None)
        except Exception: pass
    return len(ids)


def retry_proxy_waits(db:Database,store:V6Store) -> None:
    now=datetime.now(timezone.utc)
    groups={}
    for row in _auto_jobs(db):
        if clean(row.get("status")).upper()!="WAITING_NETWORK":
            continue
        parsed=_parse_proxy_marker(row.get("error_message"))
        if not parsed:
            continue
        key,attempt,retry_at=parsed
        groups.setdefault(key,[]).append((row,attempt,retry_at))
    for key,entries in groups.items():
        retry_at=min(x[2] for x in entries)
        if retry_at>now:
            continue
        representative=entries[0][0]
        proxy_config=AutoEngineYouTubeUploader._proxy_config_from_video(representative)
        # Do not blame a proxy if the VPS itself has lost internet access.
        if not AutoEngineYouTubeUploader.internet_available(None,timeout=6,max_urls=1):
            continue
        ids=[int(x[0]["id"]) for x in entries]
        if AutoEngineYouTubeUploader.internet_available(proxy_config,timeout=8,max_urls=1):
            _sql_update_video_ids(ids,status=UploadStatus.QUEUED.value,error_message="",progress_zero=True)
            for vid in ids:
                try: store.set_job_state(vid,"QUEUED",error="")
                except Exception: pass
                try: db.add_log(vid,"INFO",f"Proxy group {key} restored; job returned to active queue.",0)
                except Exception: pass
            continue
        attempt=max(x[1] for x in entries)+1
        next_at=now+timedelta(seconds=_proxy_delay(attempt))
        marker=_proxy_marker(key,attempt,next_at)
        _sql_update_video_ids(ids,status=UploadStatus.WAITING_NETWORK.value,error_message=marker)
        for vid in ids:
            try: store.set_job_state(vid,"WAITING_PROXY",error=f"Proxy group {key} still unavailable; automatic retry rescheduled.")
            except Exception: pass


class AutoEngineYouTubeUploader(YouTubeUploader):
    def _wait_for_connection(self,video_id,proxy_config,progress,reason,stop_check,progress_callback):
        cfg=proxy_config or {}
        host=clean(cfg.get("proxy_host")); port=clean(cfg.get("proxy_port"))
        key=_proxy_group_key(cfg.get("proxy_username")) or (f"{host}:{port}" if host and port else "")
        if key and host and port and int(progress or 0)<=0:
            try: row=self.db.get_video(int(video_id)) or {}
            except Exception: row={}
            auto_job=bool(row.get("auto_engine_profile_id"))
            remote_id=clean(row.get("youtube_video_id"))
            pipeline=clean(row.get("pipeline_stage")).upper()
            media=clean(row.get("media_stage_state")).upper()
            safe_boundary=(not remote_id and pipeline not in {"MEDIA","REMOTE_CREATED","COMPLETE"} and media not in {"RUNNING","COMPLETE"})
            # Only defer a proxy-specific failure when general VPS internet works.
            if auto_job and safe_boundary and self.internet_available(None,timeout=6,max_urls=1):
                attempt=1
                retry_at=datetime.now(timezone.utc)+timedelta(seconds=_proxy_delay(attempt))
                marker=_proxy_marker(key,attempt,retry_at)
                self.db.update_video_status(int(video_id),status=UploadStatus.WAITING_NETWORK,progress=int(progress or 0),error_message=marker)
                self.db.add_log(int(video_id),"WARNING",f"Proxy group {key} unavailable; job deferred so other proxy groups can continue.",int(progress or 0))
                raise UploadStopped(marker)
        return super()._wait_for_connection(video_id,proxy_config,progress,reason,stop_check,progress_callback)


def remote_verify(uploader:YouTubeUploader, row:dict, youtube_video_id:str) -> bool:
    token=clean(row.get("token_file_path"))
    proxy=uploader._proxy_config_from_video(row)
    youtube=uploader.auth.build_youtube_service(token,proxy_config=proxy)
    response=youtube.videos().list(part="id,status",id=youtube_video_id,maxResults=1).execute()
    items=response.get("items") or []
    return any(clean(item.get("id"))==youtube_video_id for item in items)


def reconcile_completed(db:Database, store:V6Store, video_id:int) -> None:
    row=db.get_video(video_id) or {}
    source=store.job_source(video_id)
    if not source:
        return
    status=clean(row.get("status")).upper()
    yid=clean(row.get("youtube_video_id"))
    if status not in {"UPLOADED","PUBLISHED"} or not yid:
        return
    channel_meta=store.channel_job_meta(video_id)
    if channel_meta and int(channel_meta.get("strict_thumbnail") or 0):
        if clean(row.get("thumbnail_stage_state")).upper() != "SUCCEEDED":
            # Never archive video or thumbnail until the required thumbnail is
            # published, including direct recovery/retry paths.
            store.set_job_state(video_id,"POST_UPLOAD_REVIEW",
                                error="Required thumbnail not yet published; sources retained.")
            return
    uploader=YouTubeUploader(db=db,worker_id=f"v6-reconcile-{os.getpid()}")
    if not source.get("remote_verified"):
        if not remote_verify(uploader,row,yid):
            store.set_job_state(video_id,"REMOTE_VERIFY_PENDING",error="YouTube remote verification did not return the uploaded ID.")
            return

    # Commit durable upload identity/history BEFORE local cleanup or Drive movement.
    # record_upload is idempotent for the same verified YouTube video ID; a true
    # history conflict raises and intentionally leaves all source evidence intact.
    store.record_upload(db,video_id,destination=clean(source.get("drive_destination")))
    store.set_job_state(video_id,"REMOTE_VERIFIED",remote_verified=True,error="")
    verified_url=clean(row.get("youtube_url")) or f"https://www.youtube.com/watch?v={yid}"
    emit_channel_sheet_writeback(store,video_id,"UPLOADED",youtube_url=verified_url,error="")

    if clean(source.get("source_folder_id"))=="songmaker-local":
        try:
            from .songmaker_local import finish_folder_source
            finish_folder_source(store,source,yid)
        except Exception as exc:
            store.set_job_state(video_id,"FOLDER_CLEANUP_PENDING",error=str(exc),remote_verified=True)
            with store.connect() as con:
                con.execute("UPDATE upload_records SET final_state='FOLDER_CLEANUP_PENDING' WHERE video_id=?",(int(video_id),))
            return
    staging=Path(clean(source.get("staging_path")))
    try:
        staging.unlink(missing_ok=True)
        if channel_meta:
            thumb_local=staging.parent/"thumbnail"/clean(channel_meta.get("thumbnail_filename"))
            thumb_local.unlink(missing_ok=True)
            try: thumb_local.parent.rmdir()
            except Exception: pass
        # Remove empty parent folders only.
        for parent in [staging.parent, staging.parent.parent]:
            try: parent.rmdir()
            except Exception: pass
    except Exception as exc:
        store.set_job_state(video_id,"LOCAL_CLEANUP_PENDING",error=str(exc))
        emit_channel_sheet_writeback(store,video_id,"UPLOADED",youtube_url=verified_url,error=f"Local cleanup pending: {exc}")
        with store.connect() as con:
            con.execute("UPDATE upload_records SET final_state='LOCAL_CLEANUP_PENDING' WHERE video_id=?",(int(video_id),))
        return

    action=clean(source.get("desired_after_success") or "move_uploaded")
    try:
        destination=""
        if clean(source.get("source_folder_id")).startswith(SHORT_FOLDER_PREFIX):
            # Short Maker -> Short Uploader: delete only the exact source that
            # was queued, after YouTube ID verification and durable history.
            folder_id=clean(source["source_folder_id"])
            filename=clean(source["filename"])
            with store.connect() as con:
                dependents=con.execute("""
                    SELECT video_id FROM job_sources
                    WHERE video_id<>? AND source_folder_id=? AND filename=?
                      AND state NOT IN ('COMPLETE','CANCELLED','FAILED_SAFE','DRIVE_MOVE_PENDING')
                """,(int(video_id),folder_id,filename)).fetchall()
            if dependents:
                raise RuntimeError("Other queued/failed channel jobs still need this VPS video; local cleanup deferred.")
            try:
                original=shortstudio_source_path(folder_id,filename)
            except FileNotFoundError:
                original=None
            if original is not None:
                if shortstudio_file_id(folder_id,original)!=clean(source["drive_file_id"]):
                    raise RuntimeError("Source MP4 changed after queueing; refusing to delete replacement.")
                original.unlink()
            destination="SHORT_STUDIO_VPS_VERIFIED_DELETED"
            with store.connect() as con:
                con.execute("UPDATE drive_videos SET present=0 WHERE source_folder_id=? AND filename=?",
                            (folder_id,filename))
            if channel_meta:
                set_channel_job_post_state(store,video_id,"COMPLETE")
        elif clean(source.get("source_folder_id"))=="songmaker-local":
            # The verified source is a local copy. Never issue Drive moves or deletes.
            destination="SONG_MAKER_LOCAL_ORIGINAL_RETAINED"
            set_channel_job_post_state(store,video_id,"COMPLETE")
            with store.connect() as con:
                con.execute("UPDATE drive_videos SET present=0 WHERE profile_id=? AND drive_file_id=?",
                            (int(source["profile_id"]),clean(source["drive_file_id"])))
                con.execute("UPDATE drive_thumbnails SET present=0 WHERE profile_id=? AND drive_file_id=?",
                            (int(source["profile_id"]),clean(source["drive_file_id"])))
        elif channel_meta and action!="keep":
            month=datetime.now(timezone.utc).strftime("%Y-%m")
            video_destination=drive_move_uploaded_month(clean(source["source_folder_id"]),clean(source["filename"]),clean(source["drive_file_id"]),month)
            thumb_destination=""
            if clean(channel_meta.get("thumbnail_drive_file_id")):
                thumb_destination=drive_move_uploaded_month(clean(channel_meta["thumbnail_source_folder_id"]),clean(channel_meta["thumbnail_filename"]),clean(channel_meta["thumbnail_drive_file_id"]),month)
            destination=f"video={video_destination}" + (f"; thumbnail={thumb_destination}" if thumb_destination else "")
            set_channel_job_post_state(store,video_id,"COMPLETE",video_dest=video_destination,thumb_dest=thumb_destination)
        elif action=="move_uploaded":
            destination=drive_move_uploaded(clean(source["source_folder_id"]),clean(source["filename"]),clean(source["drive_file_id"]))
        elif action=="delete":
            drive_delete_source(clean(source["source_folder_id"]),clean(source["filename"]),clean(source.get("drive_file_id")))
            destination="DELETED_FROM_DRIVE"
        elif action=="keep":
            destination="SOURCE_KEPT"
            if channel_meta: set_channel_job_post_state(store,video_id,"COMPLETE")
        else:
            raise ValueError(f"Unknown after-success action: {action}")
        store.set_job_state(video_id,"COMPLETE",destination=destination,remote_verified=True)
        # Update durable record with final Drive destination if it already exists.
        with store.connect() as con:
            con.execute("UPDATE upload_records SET drive_destination=?,final_state='COMPLETE' WHERE video_id=?",(destination,int(video_id)))
        store.export_records_csv()
    except Exception as exc:
        store.set_job_state(video_id,"DRIVE_MOVE_PENDING",error=str(exc),remote_verified=True)
        emit_channel_sheet_writeback(store,video_id,"UPLOADED",youtube_url=verified_url,error=f"Drive post-action pending: {exc}")
        with store.connect() as con:
            con.execute("UPDATE upload_records SET final_state='DRIVE_MOVE_PENDING' WHERE video_id=?",(int(video_id),))
        store.export_records_csv()


def retry_post_actions(db:Database,store:V6Store) -> None:
    with store.connect() as con:
        rows=con.execute("""
        SELECT video_id FROM job_sources
        WHERE state IN ('REMOTE_VERIFY_PENDING','REMOTE_VERIFIED','LOCAL_CLEANUP_PENDING','DRIVE_MOVE_PENDING','FOLDER_CLEANUP_PENDING')
        ORDER BY updated_at,video_id
        LIMIT 25
        """).fetchall()
    for row in rows:
        if STOP: return
        try: reconcile_completed(db,store,int(row["video_id"]))
        except Exception as exc: store.set_job_state(int(row["video_id"]),"DRIVE_MOVE_PENDING",error=str(exc))


def download_for_claim(db:Database,store:V6Store,video_id:int,worker_id:str) -> Path:
    source=store.job_source(video_id)
    if not source:
        raise RuntimeError("V6 Drive source mapping is missing for this Auto Engine job.")
    target=Path(clean(source["staging_path"]))
    if target.is_file() and target.stat().st_size>0:
        return target
    folder_id=clean(source.get("source_folder_id"))
    if folder_id.startswith(SHORT_FOLDER_PREFIX):
        # Snapshot, never move or delete a source before verified publishing.
        original=shortstudio_source_path(folder_id,clean(source["filename"]))
        if shortstudio_file_id(folder_id,original)!=clean(source["drive_file_id"]):
            raise RuntimeError("Short Maker source changed since scan; rerun Sync Channel.")
        target.parent.mkdir(parents=True,exist_ok=True)
        capacity=os.statvfs(str(target.parent))
        if capacity.f_bavail*capacity.f_frsize < original.stat().st_size+1024*1024*1024:
            raise RuntimeError("Not enough disk space for local upload snapshot plus 1 GB reserve.")
        temporary=target.parent/f".{uuid.uuid4().hex}.part"
        try:
            shutil.copyfile(original,temporary)
            if temporary.stat().st_size!=original.stat().st_size:
                raise RuntimeError("Local Short Maker snapshot size mismatch.")
            if shortstudio_file_id(folder_id,original)!=clean(source["drive_file_id"]):
                raise RuntimeError("Source changed during snapshot; keeping source untouched.")
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
        store.set_job_state(video_id,"DOWNLOAD_VERIFIED")
        return target
    if folder_id=="songmaker-local":
        raise RuntimeError("Local Song Maker MP4 missing; Drive fallback is forbidden")
    target.parent.mkdir(parents=True,exist_ok=True)
    free=os.statvfs(str(target.parent))
    free_bytes=free.f_bavail*free.f_frsize
    row=None
    with store.connect() as con:
        row=con.execute("SELECT size_bytes FROM drive_videos WHERE id=?",(int(source["drive_video_id"]),)).fetchone()
    expected=int(row["size_bytes"] or 0) if row else 0
    if expected and free_bytes < expected + 1024*1024*1024:
        raise RuntimeError("Not enough VPS disk space for JIT video download plus 1 GB safety margin.")
    store.set_job_state(video_id,"DOWNLOADING")
    def renew():
        if not db.renew_claim(video_id,worker_id,lease_seconds=900):
            raise RuntimeError("Queue lease lost during Drive download.")
    drive_download(clean(source["source_folder_id"]),clean(source["filename"]),target,lease_renew=renew)
    if expected and target.stat().st_size != expected:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Drive download size mismatch: expected {expected}, got {target.stat().st_size if target.exists() else 0}.")
    store.set_job_state(video_id,"DOWNLOAD_VERIFIED")
    return target


def _channel_result_handled(db:Database,store:V6Store,video_id:int) -> bool:
    meta=store.channel_job_meta(video_id)
    if not meta:
        return False
    row=db.get_video(video_id) or {}
    status=clean(row.get("status")).upper()
    yid=clean(row.get("youtube_video_id"))
    thumb_state=clean(row.get("thumbnail_stage_state")).upper()
    post_state=clean(meta.get("post_upload_state")).upper()
    retries=int(meta.get("auto_retry_count") or 0)

    if status in {"UPLOADED","PUBLISHED"} and yid:
        if thumb_state=="WARNING":
            if retries < 1:
                if db.schedule_channel_retry(video_id,300,thumbnail_only=True,message="Thumbnail upload failed; one automatic thumbnail-only retry scheduled in 5 minutes."):
                    retry_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).replace(microsecond=0).isoformat()
                    set_channel_job_post_state(store,video_id,"THUMBNAIL_RETRY_SCHEDULED",retry_count=1,retry_not_before=retry_at)
                    emit_channel_sheet_writeback(store,video_id,"UPLOADED",youtube_url=clean(row.get("youtube_url")) or f"https://www.youtube.com/watch?v={yid}",error="Thumbnail retry scheduled.")
                return True
            db.mark_channel_post_upload_failed(video_id,"Thumbnail retry limit reached. Video remains on YouTube; manual Retry Failed will retry only unfinished post-media stages.")
            set_channel_job_post_state(store,video_id,"MANUAL_RETRY_REQUIRED",retry_count=retries)
            emit_channel_sheet_writeback(store,video_id,"UPLOADED",youtube_url=clean(row.get("youtube_url")) or f"https://www.youtube.com/watch?v={yid}",error="Thumbnail retry limit reached; video is already on YouTube.")
            return True
        if thumb_state=="SKIPPED" and int(meta.get("strict_thumbnail") or 0):
            # A completed video is not a completed Engine 2 job until its
            # required thumbnail is uploaded. Never archive/delete its Drive
            # inputs or mark the Sheet complete for a skipped thumbnail.
            message="Required Engine 2 thumbnail was skipped; retry the thumbnail-only stage."
            db.mark_channel_post_upload_failed(video_id,message)
            set_channel_job_post_state(store,video_id,"MANUAL_RETRY_REQUIRED",retry_count=retries)
            emit_channel_sheet_writeback(store,video_id,"UPLOADED",youtube_url=clean(row.get("youtube_url")) or f"https://www.youtube.com/watch?v={yid}",error=message)
            return True
        if thumb_state in {"SUCCEEDED","SKIPPED"}:
            store.set_job_state(video_id,"REMOTE_VERIFYING")
            reconcile_completed(db,store,video_id)
            return True

    if status=="FAILED":
        if post_state in {"MANUAL_RETRY_REQUIRED","VALIDATION_FAILED"}:
            return True
        if retries < 1:
            if db.schedule_channel_retry(video_id,300,thumbnail_only=False,message="Upload failed; one automatic retry scheduled in 5 minutes."):
                retry_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).replace(microsecond=0).isoformat()
                set_channel_job_post_state(store,video_id,"AUTO_RETRY_SCHEDULED",retry_count=1,retry_not_before=retry_at)
                emit_channel_sheet_writeback(store,video_id,"RETRY_SCHEDULED",error=clean(row.get("error_message")) or "Upload retry scheduled.")
            return True
        set_channel_job_post_state(store,video_id,"MANUAL_RETRY_REQUIRED",retry_count=retries)
        emit_channel_sheet_writeback(store,video_id,"FAILED",error=clean(row.get("error_message")) or "Upload failed; manual retry required.")
        return True
    return False


def recover_channel_jobs(db:Database,store:V6Store) -> None:
    with store.connect() as con:
        ids=[int(r["video_id"]) for r in con.execute("SELECT video_id FROM channel_job_meta ORDER BY updated_at DESC LIMIT 500").fetchall()]
    for vid in ids:
        if STOP: return
        try: _channel_result_handled(db,store,vid)
        except Exception as exc:
            try: store.set_job_state(vid,"POST_UPLOAD_REVIEW",error=str(exc))
            except Exception: pass


def process_one(db:Database,store:V6Store,item:dict,worker_id:str) -> None:
    vid=int(item["id"])
    channel_meta=store.channel_job_meta(vid)
    try:
        download_for_claim(db,store,vid,worker_id)
        if channel_meta and int(channel_meta.get("strict_thumbnail") or 0):
            try:
                download_channel_thumbnail_for_job(store,vid)
            except Exception as exc:
                if db.fail_claimed_job(vid,worker_id,f"Pre-upload thumbnail validation failed: {exc}"):
                    store.set_job_state(vid,"FAILED",error=str(exc))
                    set_channel_job_post_state(store,vid,"VALIDATION_FAILED",retry_count=1)
                    emit_channel_sheet_writeback(store,vid,"FAILED",error=f"Pre-upload thumbnail validation failed: {exc}")
                return
        uploader=AutoEngineYouTubeUploader(db=db,worker_id=worker_id)
        store.set_job_state(vid,"UPLOADING")
        if channel_meta:
            emit_channel_sheet_writeback(store,vid,"UPLOADING",error="")
        # Reuse the exact PC queue error/retry/token handling.
        uploader._process_one(item, lambda: False, None)
        if channel_meta and _channel_result_handled(db,store,vid):
            return
        row=db.get_video(vid) or {}
        if clean(row.get("status")).upper() in {"UPLOADED","PUBLISHED"} and clean(row.get("youtube_video_id")):
            if channel_meta and int(channel_meta.get("strict_thumbnail") or 0):
                # Thumbnail-required Song jobs must pass publishing first.
                # A pending/unknown stage may not fall through to Drive archiving.
                store.set_job_state(vid,"POST_UPLOAD_REVIEW",
                                    error="Thumbnail publishing status pending; sources retained.")
            else:
                store.set_job_state(vid,"REMOTE_VERIFYING")
                reconcile_completed(db,store,vid)
        elif clean(row.get("status")).upper()=="FAILED":
            store.set_job_state(vid,"FAILED",error=clean(row.get("error_message")))
        elif clean(row.get("status")).upper()=="PAUSED":
            err=clean(row.get("error_message"))
            parsed=_parse_proxy_marker(err)
            if parsed:
                key,attempt,retry_at=parsed
                park_proxy_group(db,store,key,attempt,retry_at)
            else:
                store.set_job_state(vid,"PAUSED",error=err)
    except Exception as exc:
        try:
            if db.fail_claimed_job(vid,worker_id,str(exc)):
                store.set_job_state(vid,"FAILED",error=str(exc))
            else:
                db.release_claim(vid,worker_id,requeue_if_claimed=True)
                store.set_job_state(vid,"FAILED",error=str(exc))
            if channel_meta:
                _channel_result_handled(db,store,vid)
        except Exception:
            pass


def main() -> None:
    db=Database(PC_DB_PATH)
    store=V6Store()
    worker_id=f"lum-auto-v6-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    # Reconcile safe PC crash evidence on service start. This is the PC behavior;
    # ambiguous media-stage crashes remain REVIEW_REQUIRED.
    try:
        db.recover_interrupted_uploads(auto_resume_safe=True)
    except Exception:
        pass
    while not STOP:
        try:
            store.heartbeat("worker")
            recover_channel_jobs(db,store)
            retry_post_actions(db,store)
            retry_proxy_waits(db,store)
            state=store.engine()
            if not int(state.get("running") or 0):
                time.sleep(2); continue
            if int(state.get("pause_after_current") or 0):
                store.set_engine(running=False,pause_after_current=False)
                time.sleep(2); continue

            db.activate_due_scheduled_jobs(auto_engine_only=True)
            claim_scope={}
            if os.environ.get("LUM_ENGINE_INSTANCE")=="shortstudio":
                # A paused profile cannot be claimed even when it has older queued jobs.
                # Manual Start scopes the worker to just the requested profile(s).
                enabled={int(p["id"]) for p in db.list_auto_engine_profiles() if int(p.get("enabled") or 0)==1}
                manual=state.get("manual_profile_ids")
                if manual is not None:
                    selected={int(x) for x in str(manual).split(",") if x.strip().isdigit()}
                    enabled.intersection_update(selected)
                claim_scope["allowed_auto_engine_profile_ids"]=sorted(enabled)
            claimed=db.claim_available_jobs(worker_id,1,auto_engine_only=True,lease_seconds=900,**claim_scope)
            if not claimed:
                time.sleep(2); continue
            process_one(db,store,claimed[0],worker_id)
            if int(store.engine().get("pause_after_current") or 0):
                store.set_engine(running=False,pause_after_current=False)
        except Exception as exc:
            store.heartbeat("worker",str(exc))
            time.sleep(3)
    try: db.release_worker_claims(worker_id,requeue_claimed=True)
    except Exception: pass


if __name__=="__main__":
    main()
