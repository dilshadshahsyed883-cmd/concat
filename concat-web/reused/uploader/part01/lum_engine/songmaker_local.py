"""Owner-scoped, loopback-only Song Maker -> isolated Song Uploader source import.

No Google Drive APIs, path parameters, public URLs or YouTube publishing here.
The existing Song Uploader profile/sheet matching and scheduler remain authoritative.
"""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil

from .auto_engine_v6 import (
    STAGING_ROOT, V6Store, _image_dimensions, normalize_filename,
    safe_slug, script_key_from_filename,
)

HOST = "127.0.0.1"
PORT = 8150
LOCAL_SOURCE = "songmaker-local"
MAX_VIDEO_BYTES = 1100 * 1024 * 1024
MAX_THUMB_BYTES = 2 * 1024 * 1024
JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def _get(owner: str, path: str, target: Path | None = None, maximum: int = 1048576):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=180)
    temp = target.with_suffix(target.suffix + ".part") if target else None
    try:
        conn.request("GET", path, headers={"X-Auth-User": owner})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("Song Maker output is unavailable to this user")
        length = response.getheader("Content-Length")
        if length and int(length) > maximum:
            raise ValueError("Song Maker output exceeds size limit")
        if temp is None:
            data = response.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError("Song Maker catalog exceeds size limit")
            return json.loads(data.decode("utf-8"))
        if temp.is_symlink() or target.is_symlink():
            raise ValueError("Unsafe local Song Maker import path")
        count = 0
        with temp.open("wb") as stream:
            os.chmod(temp, 0o600)
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > maximum:
                    raise ValueError("Song Maker media exceeds size limit")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if count <= 0 or (length and count != int(length)):
            raise ValueError("Song Maker media response is missing or truncated")
        os.replace(temp, target)
        return count
    finally:
        if temp:
            temp.unlink(missing_ok=True)
        conn.close()


def import_job(store: V6Store, profile_id: int, owner: str, job_id: str, expected_folder:str="") -> dict:
    if not JOB_ID.fullmatch(job_id or ""):
        raise ValueError("Select a valid Song Maker project")
    if not owner or len(owner) > 160 or any(ord(char) < 32 for char in owner):
        raise ValueError("Authenticated Song Maker owner required")
    if int(profile_id) <= 0 or not (store.is_channel_profile(int(profile_id)) or store.group_settings(int(profile_id)).get("video_source_mode") in {"songmaker","songfolder"}):
        raise ValueError("Choose a saved Song Uploader profile")
    saved_owner=store.group_settings(int(profile_id)).get("song_folder_owner")
    if saved_owner and saved_owner!=owner: raise ValueError("Folder profile belongs to another account")
    if not os.environ.get("LUM_SUITE_AUTH", "").lower() in {"1", "true", "yes", "on"}:
        raise ValueError("Song Maker imports require trusted Scene SSO")
    # Retrieve only this authenticated owner's completed, validated outputs.
    catalog = _get(owner, "/song-maker/api/folders/"+expected_folder+"/outputs" if expected_folder else "/song-maker/api/uploader/outputs")
    found = [x for x in catalog.get("outputs", []) if x.get("id") == job_id]
    if len(found) != 1:
        raise ValueError("Song Maker output is not available for this owner")
    item = found[0]
    folder_id=str(item.get("folder_id") or "")
    if expected_folder and folder_id!=expected_folder: raise ValueError("Song Maker folder identity mismatch")
    name = str(item.get("name") or "")
    if (len(name) > 180 or name in {"", ".", ".."} or
            "/" in name or "\\\\" in name or
            not name.lower().endswith(".mp4") or
            any(ord(char) < 32 or char in '< > : " | ? *'.replace(" ", "") for char in name)):
        raise ValueError("Song Maker MP4 filename is unsafe")
    identity = "songmaker:" + job_id
    stage = STAGING_ROOT / f"profile_{int(profile_id):04d}" / ("drive_" + safe_slug(identity, "file"))
    if stage.is_symlink():
        raise ValueError("Unsafe import directory")
    stage.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stage.is_symlink():
        raise ValueError("Unsafe import directory")
    thumbnail_name = Path(name).stem + ".jpg"
    video = stage / name
    thumbnail = stage / "thumbnail" / thumbnail_name
    thumbnail.parent.mkdir(mode=0o700, exist_ok=True)
    with (stage / ".import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with store.connect() as con:
            duplicate = con.execute(
                "SELECT id,present FROM drive_videos WHERE profile_id=? AND drive_file_id=?",
                (int(profile_id),identity)).fetchone()
            other_active = con.execute(
                """SELECT 1 FROM job_sources
                   WHERE profile_id=? AND source_folder_id<>?
                     AND state NOT IN ('COMPLETE','CANCELLED','FAILED_SAFE')
                   LIMIT 1""", (int(profile_id),LOCAL_SOURCE)).fetchone()
            if other_active:
                raise ValueError("Finish existing Drive-source jobs before switching this channel to local Song Maker inputs")
            if duplicate:
                return {"status": "already_imported", "profile_id": int(profile_id),
                        "name": name, "video_id": identity}
        consumer="song-uploader:profile:"+str(int(profile_id))
        if folder_id:
            transfer(owner,folder_id,job_id,'reserve',{'consumer':consumer})
        required = int(item.get("size") or 0)
        if required <= 0 or required > MAX_VIDEO_BYTES:
            raise ValueError("Invalid Song Maker video size")
        usage = shutil.disk_usage(stage)
        if usage.free < required + 5_000_000_000:
            raise ValueError("Insufficient disk above the 5 GB operational reserve")
        try:
            size = _get(owner, f"/song-maker/api/jobs/{job_id}/video", video, MAX_VIDEO_BYTES)
            if size != required:
                raise ValueError("Song Maker MP4 size changed; retry import")
            thumb_size = _get(owner, f"/song-maker/api/uploader/{job_id}/thumbnail",
                              thumbnail, MAX_THUMB_BYTES)
            for path,key in ((video,'video_sha256'),(thumbnail,'thumbnail_sha256')):
                if item.get(key):
                    h=hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
                    if h.hexdigest()!=item[key]: raise ValueError('Original folder media hash mismatch')
            width, height = _image_dimensions(thumbnail)
            if width < 120 or height < 68:
                raise ValueError("Song Maker thumbnail dimensions are too small")
            with store.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("""
                    INSERT INTO drive_videos(profile_id,drive_file_id,filename,normalized_filename,
                                             script_key,size_bytes,source_folder_id,present)
                    VALUES(?,?,?,?,?,?,?,1)
                    """, (int(profile_id),identity,name,normalize_filename(name),
                          script_key_from_filename(name),size,LOCAL_SOURCE))
                con.execute("""
                    INSERT INTO drive_thumbnails(profile_id,drive_file_id,filename,normalized_filename,
                                                 size_bytes,source_folder_id,present)
                    VALUES(?,?,?,?,?,?,1)
                    """, (int(profile_id),identity,thumbnail_name,normalize_filename(thumbnail_name),
                          thumb_size,LOCAL_SOURCE))
                if folder_id:
                    con.execute("INSERT INTO song_folder_imports VALUES(?,?,?,?,?,?) ON CONFLICT(profile_id,drive_file_id) DO NOTHING",
                                (int(profile_id),identity,owner,folder_id,job_id,consumer))
                con.execute("""
                    INSERT INTO profile_sources(profile_id,local_songmaker_enabled)
                    VALUES(?,1)
                    ON CONFLICT(profile_id) DO UPDATE SET
                    local_songmaker_enabled=1,updated_at=CURRENT_TIMESTAMP
                    """, (int(profile_id),))
        except Exception:
            # Never delete media already claimed by an uploader; this is a new import.
            video.unlink(missing_ok=True)
            thumbnail.unlink(missing_ok=True)
            raise
    return {"status": "imported", "profile_id": int(profile_id), "name": name,
            "video_id": identity, "ready_for_sheet_matching": True}


def folder_selection(owner:str,folder_id:str) -> dict:
    if not owner or not re.fullmatch(r'[0-9a-f]{32}',folder_id):
        raise ValueError('Select your Song Maker VPS folder')
    item=_get(owner,'/song-maker/api/folders/'+folder_id)
    if item.get('id')!=folder_id: raise ValueError('Folder is unavailable for this account')
    return {'song_folder_id':folder_id,'song_folder_owner':owner,'song_folder_name':str(item.get('name') or '')}


def sync_folder(store:V6Store,profile_id:int,settings:dict):
    owner=settings['song_folder_owner'];fid=settings['song_folder_id']
    folder_selection(owner,fid)
    catalog=_get(owner,'/song-maker/api/folders/'+fid+'/outputs')
    for item in catalog.get('outputs',[]):
        import_job(store,profile_id,owner,str(item.get('id') or ''),expected_folder=fid)


def transfer(owner:str,fid:str,jid:str,action:str,body:dict):
    if not JOB_ID.fullmatch(fid) or not JOB_ID.fullmatch(jid) or action not in {'reserve','consume'}:
        raise ValueError('Invalid folder transfer identity')
    connection=http.client.HTTPConnection(HOST,PORT,timeout=60)
    try:
        connection.request('POST',f'/internal/song-folders/{fid}/{jid}/{action}',json.dumps(body),
                           {'Content-Type':'application/json','X-Auth-User':owner,'X-Song-Maker':'1'})
        response=connection.getresponse();payload=response.read(32768)
        if response.status!=200: raise ValueError('Canonical folder transfer is unavailable; media retained')
        return json.loads(payload)
    finally: connection.close()


def finish_folder_source(store:V6Store,source:dict,youtube_id:str):
    with store.connect() as con:
        row=con.execute('SELECT * FROM song_folder_imports WHERE profile_id=? AND drive_file_id=?',
                        (int(source['profile_id']),source['drive_file_id'])).fetchone()
    if row:
        transfer(row['owner'],row['folder_id'],row['song_job_id'],'consume',
                 {'consumer':row['consumer'],'youtube_id':youtube_id})
