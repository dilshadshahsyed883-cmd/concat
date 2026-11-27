from __future__ import annotations

import csv
import io
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Tuple

from .token_utils import find_token_for_gmail_status, VALID_TOKEN, CLIENT_SECRET, BAD_JSON, INVALID_TOKEN


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class SheetRow:
    key: str
    sr: str
    ip: str
    gmail: str
    channel_name: str
    channel_id: str
    channel_link: str
    browser_profile: str
    proxy_scheme: str
    proxy_host: str
    proxy_port: str
    proxy_username: str
    proxy_password: str
    group: str
    status: str
    sheet_status: str
    password: str
    backup_code: str
    backup_key: str
    notes: str
    token_file_path: str
    token_exists: bool


def extract_sheet_id(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Sheet URL/ID is required.")
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", raw):
        return raw
    raise ValueError("Invalid Google Sheet URL/ID. Paste full sheet URL or spreadsheet ID.")


def extract_gid(value: str, fallback: str = "0") -> str:
    raw = (value or "").strip()
    if not raw:
        return str(fallback)
    parsed = urllib.parse.urlparse(raw)
    query = urllib.parse.parse_qs(parsed.query)
    if "gid" in query and query["gid"]:
        return query["gid"][0]
    frag = urllib.parse.parse_qs(parsed.fragment)
    if "gid" in frag and frag["gid"]:
        return frag["gid"][0]
    match = re.search(r"gid=([0-9]+)", raw)
    return match.group(1) if match else str(fallback)


def csv_export_url(sheet_url_or_id: str, gid: str | None = None) -> str:
    """Return the CSV export URL.

    In this app, an empty/None/FIRST_TAB gid means: always read the first
    worksheet tab. This deliberately ignores any #gid=... from a pasted URL.
    """
    sheet_id = extract_sheet_id(sheet_url_or_id)
    if gid and str(gid).strip().upper() not in {"FIRST_TAB", "FIRST", "AUTO"}:
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={urllib.parse.quote(str(gid))}"
    # gviz without sheet/gid reads the first worksheet tab for public sheets.
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"


def auto_sheet_source_name(sheet_url_or_id: str, timeout: int = 10) -> str:
    """Best-effort human label for a sheet source.

    Tries to read the Google Sheets HTML title. If that is not accessible,
    falls back to a short Spreadsheet ID label.
    """
    sheet_id = extract_sheet_id(sheet_url_or_id)
    fallback = f"Google Sheet {sheet_id[:8]}"
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LocalYouTubeUploadManager/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(200000).decode("utf-8", errors="replace")
        title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if title_match:
            title = re.sub(r"\s+", " ", title_match.group(1)).strip()
            title = re.sub(r" - Google Sheets$", "", title, flags=re.I).strip()
            low_title = title.lower()
            if title and "sign in" not in low_title and "google accounts" not in low_title and low_title != "google sheets":
                return title
    except Exception:
        pass
    return fallback


def _simple_group_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


_GROUP_ALIASES = {
    "marati": "Marathi",
    "mrathi": "Marathi",
    "marathi": "Marathi",
    "hindhi": "Hindi",
    "hindi": "Hindi",
    "gujrati": "Gujarati",
    "gujarati": "Gujarati",
    "bangla": "Bengali",
    "bengali": "Bengali",
    "punjabi": "Punjabi",
    "panjabi": "Punjabi",
    "tamil": "Tamil",
    "telugu": "Telugu",
    "kannada": "Kannada",
    "malyalam": "Malayalam",
    "malayalam": "Malayalam",
    "urdu": "Urdu",
    "english": "English",
}


def smart_group_name(raw_group: str, existing_group_names: Iterable[str] = ()) -> Tuple[str, str]:
    """Normalize a sheet group value and return (group_name, reason).

    Handles case, extra spaces, known language misspellings, and fuzzy matching
    against existing app groups.
    """
    raw = re.sub(r"\s+", " ", (raw_group or "").strip())
    if not raw:
        return "", ""
    key = _simple_group_key(raw)

    normalized_existing = []
    for name in existing_group_names or []:
        clean_name = re.sub(r"\s+", " ", str(name or "").strip())
        if clean_name:
            normalized_existing.append((clean_name, _simple_group_key(clean_name)))

    # Exact case/space-insensitive match first.
    for name, existing_key in normalized_existing:
        if key == existing_key:
            return name, "case/space match" if raw != name else "exact"

    # Known aliases/misspellings. Prefer existing app group if it already exists.
    if key in _GROUP_ALIASES:
        alias = _GROUP_ALIASES[key]
        alias_key = _simple_group_key(alias)
        for name, existing_key in normalized_existing:
            if alias_key == existing_key:
                return name, f"alias {raw} → {name}"
        return alias, f"alias {raw} → {alias}"

    # Fuzzy match against existing groups. Require same first letter to avoid risky
    # matches like Hindi/Sindhi.
    best_name = ""
    best_score = 0.0
    for name, existing_key in normalized_existing:
        if not key or not existing_key or key[0] != existing_key[0]:
            continue
        score = SequenceMatcher(None, key, existing_key).ratio()
        if score > best_score:
            best_score = score
            best_name = name
    if best_name and best_score >= 0.78:
        return best_name, f"fuzzy {raw} → {best_name} ({best_score:.2f})"

    # New group: stable clean display name.
    return raw.title(), "new group" if raw != raw.title() else "new group"


def fetch_sheet_csv_tab(sheet_url_or_id: str, tab_name: str, timeout: int = 30) -> List[Dict[str, str]]:
    """Read a public Google Sheet worksheet by visible tab name as CSV.

    Google occasionally returns an empty GViz result for a valid named tab.
    Retry through the standard CSV export endpoint before treating the tab as
    empty. This keeps the original tab-name workflow and does not require users
    to replace saved profiles with GIDs.
    """
    sheet_id = extract_sheet_id(sheet_url_or_id)
    tab = (tab_name or "").strip()
    encoded_tab = urllib.parse.quote(tab)
    urls = []
    if tab:
        # IMPORTANT: named Auto Engine metadata tabs must never fall back to
        # the generic /export endpoint. Google can ignore the ``sheet=``
        # parameter on that endpoint and return the first worksheet instead,
        # which can silently attach another profile's title/metadata.
        # GViz honours the visible worksheet name, so a failed/empty named-tab
        # lookup is safer to block than to upload with cross-profile metadata.
        urls.append(
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={encoded_tab}"
        )
    else:
        urls.extend([
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv",
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv",
        ])

    last_error: Exception | None = None
    saw_csv_response = False
    for url in urls:
        req = urllib.request.Request(url, headers={"User-Agent": "LocalYouTubeUploadManager/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except Exception as exc:
            last_error = exc
            continue

        text = raw.decode("utf-8-sig", errors="replace")
        if "<html" in text.lower()[:500] or "<!doctype" in text.lower()[:500]:
            last_error = RuntimeError("Google returned an HTML page instead of CSV.")
            continue
        saw_csv_response = True
        reader = csv.DictReader(io.StringIO(text))
        rows: List[Dict[str, str]] = []
        for row in reader:
            if not row:
                continue
            clean = {normalize_header(k): (v or "").strip() for k, v in row.items() if k is not None}
            if any(clean.values()):
                rows.append(clean)
        if rows:
            return rows

    if saw_csv_response:
        if tab:
            raise RuntimeError(
                f'Metadata tab "{tab}" returned no filled rows. Queue creation is blocked instead of falling back to another worksheet.'
            )
        return []
    raise RuntimeError(
        "Could not read metadata sheet/tab. Make sure the sheet is shared or published and the tab name is correct. "
        f"Details: {last_error or 'No CSV response received.'}"
    )



def fetch_sheet_csv_gid(sheet_url_or_id: str, gid: str, timeout: int = 30) -> List[Dict[str, str]]:
    """Read exactly one public Google Sheet worksheet by immutable numeric GID.

    This function never falls back to another worksheet. It is intended for
    Auto Engine profile metadata, where cross-profile metadata is unsafe.
    """
    sheet_id = extract_sheet_id(sheet_url_or_id)
    gid_value = str(gid or "").strip()
    if not re.fullmatch(r"[0-9]+", gid_value):
        raise ValueError("A valid numeric metadata Sheet GID is required. Paste the exact Google Sheet tab URL and save the profile.")
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={urllib.parse.quote(gid_value)}"
    req = urllib.request.Request(url, headers={"User-Agent": "LocalYouTubeUploadManager/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as exc:
        raise RuntimeError(
            f"Could not read metadata worksheet GID {gid_value}. Make sure the sheet is shared/published and the saved GID still exists. Details: {exc}"
        ) from exc
    text = raw.decode("utf-8-sig", errors="replace")
    if "<html" in text.lower()[:500] or "<!doctype" in text.lower()[:500]:
        raise RuntimeError("Google returned an HTML page instead of CSV for the saved metadata GID. Share/publish the sheet and try again.")
    reader = csv.DictReader(io.StringIO(text))
    rows: List[Dict[str, str]] = []
    for row in reader:
        if not row:
            continue
        clean = {normalize_header(k): (v or "").strip() for k, v in row.items() if k is not None}
        if any(clean.values()):
            rows.append(clean)
    if not rows:
        raise RuntimeError(f"Metadata worksheet GID {gid_value} returned no filled rows. Queue creation is blocked.")
    return rows

def fetch_sheet_csv(sheet_url_or_id: str, gid: str | None = None, timeout: int = 30) -> List[Dict[str, str]]:
    url = csv_export_url(sheet_url_or_id, None)
    req = urllib.request.Request(url, headers={"User-Agent": "LocalYouTubeUploadManager/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as exc:
        raise RuntimeError(
            "Could not read Google Sheet. Make sure the sheet is shared/published so CSV export can be accessed, then try again. "
            f"Details: {exc}"
        ) from exc
    text = raw.decode("utf-8-sig", errors="replace")
    # Google login/error pages are usually HTML, not CSV.
    if "<html" in text.lower()[:500] or "<!doctype" in text.lower()[:500]:
        raise RuntimeError("Google returned an HTML page instead of CSV. Share/publish the sheet or use a publicly accessible sheet link.")
    reader = csv.DictReader(io.StringIO(text))
    rows: List[Dict[str, str]] = []
    for row in reader:
        if not row:
            continue
        clean = {normalize_header(k): (v or "").strip() for k, v in row.items() if k is not None}
        if any(clean.values()):
            rows.append(clean)
    return rows


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def pick(row: Dict[str, str], *names: str) -> str:
    for name in names:
        key = normalize_header(name)
        if row.get(key):
            return row[key].strip()
    return ""


def parse_channel_id(channel_link: str, gmail: str = "") -> str:
    raw = (channel_link or "").strip()
    match = re.search(r"(UC[a-zA-Z0-9_-]{20,})", raw)
    if match:
        return match.group(1)
    # Stable synthetic ID for sheet-managed channels without a UC ID. Prefer
    # the channel link/handle so two Brand Channels under one Gmail remain
    # distinct even before OAuth upgrades them to real UC IDs.
    key = (raw or gmail).strip().lower()
    safe = re.sub(r"[^a-z0-9._@-]+", "_", key)[:120]
    return f"sheet:{safe}" if safe else ""


def parse_proxy_line(raw: str) -> Dict[str, str]:
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        return {"proxy_scheme": "", "proxy_host": "", "proxy_port": "", "proxy_username": "", "proxy_password": ""}
    scheme = "http"
    rest = value
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
        scheme = scheme.lower().strip() or "http"
    if scheme not in {"http", "https"}:
        scheme = "http"
    username = ""
    password = ""
    if "@" in rest:
        login_part, host_part = rest.rsplit("@", 1)
        if ":" in login_part:
            username, password = login_part.split(":", 1)
        else:
            username = login_part
    else:
        host_part = rest
    parts = host_part.split(":")
    if len(parts) >= 4:
        host, port, username = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
    elif len(parts) >= 2:
        host, port = parts[0], parts[1]
    else:
        host, port = host_part, ""
    return {
        "proxy_scheme": scheme,
        "proxy_host": host.strip(),
        "proxy_port": port.strip(),
        "proxy_username": username.strip(),
        "proxy_password": password.strip(),
    }


def normalize_status(value: str, token_status: str) -> str:
    status = (value or "").strip().upper()
    if status in {"INACTIVE", "DISABLED", "REMOVE", "REMOVED", "OFF", "NO"}:
        return "INACTIVE"
    if token_status == VALID_TOKEN:
        return "ACTIVE"
    if token_status in {CLIENT_SECRET, BAD_JSON, INVALID_TOKEN}:
        return "INVALID_TOKEN"
    return "TOKEN_MISSING"


def rows_to_sheet_rows(raw_rows: Iterable[Dict[str, str]], json_folder: str) -> List[SheetRow]:
    source_rows = list(raw_rows)
    gmail_counts: Dict[str, int] = {}
    for source_row in source_rows:
        source_gmail = pick(source_row, "gmail", "gmail_s", "email", "google_account_email", "account", "account_email").lower()
        if source_gmail:
            gmail_counts[source_gmail] = gmail_counts.get(source_gmail, 0) + 1
    parsed: List[SheetRow] = []
    seen: set[str] = set()
    seen_real_channel_ids: Dict[str, int] = {}
    for row_number, row in enumerate(source_rows, start=2):
        gmail = pick(row, "gmail", "gmail_s", "email", "google_account_email", "account", "account_email").lower()
        channel_name = pick(row, "channel_name", "channel_name_s", "name", "youtube_channel", "channel") or gmail
        sr = pick(row, "sr", "serial", "serial_no", "serial_number")
        ip_raw = pick(row, "ip", "proxy", "upload_proxy", "proxy_line")
        channel_link = pick(row, "channel_link", "channel_link_s", "channel_url", "link", "url")
        explicit_channel_id = pick(row, "channel_id", "youtube_channel_id", "uc_id")
        channel_id = parse_channel_id(explicit_channel_id, gmail) if explicit_channel_id else parse_channel_id(channel_link, "")
        if channel_id.startswith("UC"):
            channel_key = channel_id.casefold()
            previous_row = seen_real_channel_ids.get(channel_key)
            if previous_row is not None:
                raise ValueError(
                    f"Duplicate YouTube Channel ID {channel_id} appears in Sheet rows "
                    f"{previous_row} and {row_number}. Keep one row per real channel before syncing."
                )
            seen_real_channel_ids[channel_key] = row_number

        # Sheet inventory identity must stay independent from OAuth. Prefer the
        # fixed Sr value, then a channel link, then Gmail + channel name. This
        # keeps one visible app row per sheet row even when several Brand
        # Channels share the same Gmail and OAuth later replaces the channel ID.
        if sr:
            identity_seed = f"sr:{sr.strip().lower()}"
        elif channel_link:
            identity_seed = f"link:{channel_link.strip().lower()}"
        else:
            composite = "|".join(part for part in [gmail, channel_name.strip().lower()] if part)
            identity_seed = f"account:{composite}" if composite else f"row:{row_number}"
        key = identity_seed
        if key in seen:
            raise ValueError(
                f"Duplicate Sheet row identity {identity_seed!r} at row {row_number}. "
                "Use a unique Sr or real UC Channel ID for every channel row."
            )
        seen.add(key)

        # Until OAuth verifies the Brand Channel, use a stable synthetic ID that
        # is tied to the sheet row identity rather than Gmail alone.
        if not channel_id:
            safe_key = re.sub(r"[^a-z0-9._@|-]+", "_", key)[:180]
            channel_id = f"sheet:{safe_key}"
        token_file_path, token_status = find_token_for_gmail_status(
            json_folder, gmail, channel_id, allow_legacy=gmail_counts.get(gmail, 0) <= 1
        )
        token_exists = token_status == VALID_TOKEN
        proxy = parse_proxy_line(ip_raw)
        sheet_status = pick(row, "status", "active")
        status = normalize_status(sheet_status, token_status)
        parsed.append(SheetRow(
            key=key,
            sr=sr,
            ip=ip_raw,
            gmail=gmail,
            channel_name=channel_name,
            channel_id=channel_id,
            channel_link=channel_link,
            browser_profile=pick(row, "browser_profile", "profile", "ix_profile", "browser_profile_name", "browser"),
            proxy_scheme=proxy["proxy_scheme"],
            proxy_host=proxy["proxy_host"],
            proxy_port=proxy["proxy_port"],
            proxy_username=proxy["proxy_username"],
            proxy_password=proxy["proxy_password"],
            group=pick(row, "group", "category", "niche"),
            status=status,
            sheet_status=sheet_status,
            password=pick(row, "password", "password_s"),
            backup_code=pick(row, "backup_code", "backup_code_s"),
            backup_key=pick(row, "back_up_key", "back_up_key_s", "backup_key"),
            notes=pick(row, "notes", "note"),
            token_file_path=token_file_path,
            token_exists=token_exists,
        ))
    return parsed
