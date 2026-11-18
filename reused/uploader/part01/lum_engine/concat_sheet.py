"""Optional Concat video metadata from existing first-tab Sheet CSV rows.

No new Sheet engine, no queue mutation and no publishing on sync/preview.
"""
from __future__ import annotations

from .concat_source import ConcatSourceError, parse_concat_link


def preview_sheet_videos(rows, *, origin="https://concat.scenebuilder.online", limit=250):
    result = []
    for number, row in enumerate(rows, start=2):
        if not isinstance(row, dict):
            continue
        link = next((str(row.get(key) or "").strip()
                     for key in ("concat_video_link", "concat_link", "video_link")
                     if str(row.get(key) or "").strip()), "")
        if not link:
            continue
        try:
            parse_concat_link(link, origin=origin)
        except ConcatSourceError:
            continue
        result.append({
            "row": number,
            "video_link": link,
            "title": str(row.get("title") or row.get("video_title") or "").strip()[:100],
            "description": str(row.get("description") or "").strip()[:5000],
            "visibility": str(row.get("visibility") or "private").strip().lower(),
        })
        if len(result) >= limit:
            break
    return result
