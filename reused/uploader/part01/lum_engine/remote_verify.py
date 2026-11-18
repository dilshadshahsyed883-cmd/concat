from __future__ import annotations
from typing import Any


def verify_youtube_video(auth_manager: Any, db: Any, core_video_id: int) -> dict:
    job = db.get_video(int(core_video_id))
    if not job:
        raise RuntimeError("Core upload row is missing")
    youtube_id = str(job.get("youtube_video_id") or "").strip()
    if not youtube_id:
        raise RuntimeError("YouTube did not return a video ID; remote verification cannot run")
    token_path = str(job.get("channel_token_file_path") or job.get("token_file_path") or "").strip()
    if not token_path:
        raise RuntimeError("Channel token path is missing")
    proxy = {
        "proxy_scheme": job.get("proxy_scheme") or "http",
        "proxy_host": job.get("proxy_host") or "",
        "proxy_port": job.get("proxy_port") or "",
        "proxy_username": job.get("proxy_username") or "",
        "proxy_password": job.get("proxy_password") or "",
    }
    youtube = auth_manager.build_youtube_service(token_path, proxy_config=proxy)
    response = youtube.videos().list(part="id,snippet,status", id=youtube_id).execute()
    items = response.get("items") or []
    if not items:
        raise RuntimeError(f"YouTube verification could not find video {youtube_id}")
    item = items[0]
    returned_id = str(item.get("id") or "")
    if returned_id != youtube_id:
        raise RuntimeError("YouTube verification returned the wrong video ID")
    expected_channel = str(job.get("youtube_channel_id") or job.get("channel_id") or "").strip()
    returned_channel = str((item.get("snippet") or {}).get("channelId") or "").strip()
    if expected_channel and returned_channel and expected_channel != returned_channel:
        raise RuntimeError("YouTube video exists but belongs to a different channel")
    return {
        "verified": True,
        "video_id": youtube_id,
        "youtube_url": str(job.get("youtube_url") or f"https://www.youtube.com/watch?v={youtube_id}"),
        "channel_id": returned_channel or expected_channel,
        "privacy_status": str((item.get("status") or {}).get("privacyStatus") or ""),
    }
