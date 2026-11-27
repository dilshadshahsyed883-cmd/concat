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


def upload_settings_for_video(rows, video_name, channels, groups):
    """Return an editable draft and errors, resolving names only in owned inventory."""
    import re
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from .concat_schedule import concat_upload_at
    def clean(row):
        return {re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_"):
                str(v or "").strip() for k, v in row.items()}
    def key(value):
        return re.sub(r"\.mp4$", "", str(value or "").strip(), flags=re.I).casefold()
    matches = [clean(row) for row in rows if isinstance(row, dict)
               and key(clean(row).get("video_name")) == key(video_name)]
    draft = {"title": "", "description": "", "group_name": "", "channel_db_id": "",
             "channel": "", "privacy_status": "public", "app_upload_at": "",
             "timezone_name": "Asia/Karachi"}
    errors = []
    if len(matches) != 1:
        return draft, ["No matching video name in sheet" if not matches else
                       "Duplicate video names in sheet; keep one matching row"]
    row = matches[0]
    draft.update(title=row.get("title", ""), description=row.get("description", ""),
                 group_name=row.get("group", ""), channel=row.get("channel", ""),
                 privacy_status=row.get("visibility", "").lower() or "public",
                 app_upload_at=row.get("upload_start", "").replace(" ", "T", 1),
                 timezone_name=row.get("timezone", "") or "Asia/Karachi")
    try:
        metadata_for_video(rows, video_name)
    except ValueError as exc:
        errors.append(str(exc))
    group = next((g for g in groups if g.casefold() == draft["group_name"].casefold()), None)
    if group is None:
        errors.append("Select an available group")
    else:
        draft["group_name"] = group
    candidates = [c for c in channels if draft["channel"] and
                  draft["channel"].casefold() in {str(c[k]).casefold() for k in
                      ("channel_name", "channel_id", "id")} and group is not None and group.casefold() in {g.casefold() for g in c["groups"]}]
    if len(candidates) == 1:
        draft["channel_db_id"] = str(candidates[0]["id"])
    else:
        errors.append("Channel missing, ambiguous or outside selected group")
    if draft["privacy_status"] not in {"public", "unlisted", "private"}:
        errors.append("Visibility must be public, unlisted or private")
    try:
        ZoneInfo(draft["timezone_name"])
        concat_upload_at(draft["app_upload_at"], draft["timezone_name"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        errors.append(str(exc))
    return draft, errors
