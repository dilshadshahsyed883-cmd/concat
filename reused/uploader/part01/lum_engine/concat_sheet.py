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


def metadata_sheet_id(value: str) -> str:
    """Accept only a Google Sheets URL/ID; never store an outbound arbitrary URL."""
    import re
    from urllib.parse import urlsplit
    raw = str(value or "").strip()
    if len(raw) > 2048:
        raise ValueError("Paste a valid Google Sheet URL or spreadsheet ID")
    if re.fullmatch(r"[A-Za-z0-9_-]{20,200}", raw):
        return raw
    url = urlsplit(raw)
    match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]{20,200})(?:/(?:edit|view|preview))?/?", url.path)
    if (url.scheme != "https" or url.netloc != "docs.google.com"
            or url.username or url.password or not match):
        raise ValueError("Paste a valid Google Sheet URL or spreadsheet ID")
    return match.group(1)


def metadata_for_video(rows, video_name: str) -> dict:
    """Exact filename match (case-insensitive, optional .mp4), never fuzzy/row-order."""
    import re

    def key(value):
        return re.sub(r"\.mp4$", "", str(value or "").strip(), flags=re.I).casefold()

    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean = {re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_"): str(v or "").strip()
                 for k, v in row.items()}
        if key(clean.get("video_name")) != key(video_name) or not key(video_name):
            continue
        matches.append(clean)
    if not matches:
        raise ValueError("No matching video name found in the metadata sheet")
    if len(matches) != 1:
        raise ValueError("Duplicate video names found in the metadata sheet; keep one matching row")
    row = matches[0]
    if not {"video_name", "title", "description"} <= row.keys():
        raise ValueError("Metadata sheet needs video name, title and description columns")
    if not row["title"] or len(row["title"]) > 100 or len(row["description"]) > 5000:
        raise ValueError("Title must be 1–100 characters and description at most 5000 characters")
    return {"title": row["title"], "description": row["description"]}
