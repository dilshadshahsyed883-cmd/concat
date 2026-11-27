from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import os
import re
import shutil
import tempfile
from typing import Any, Dict, Iterable, List, Tuple

from desktop_core.modules.oauth_registry import list_oauth_client_files
from desktop_core.modules.path_utils import CONFIG_DIR, TOKEN_DIR
from desktop_core.modules.secret_store import secure_file_for_current_user
from desktop_core.modules.token_utils import (
    BAD_JSON, CLIENT_SECRET, INVALID_TOKEN, MISSING_TOKEN, VALID_TOKEN,
    classify_google_json, find_token_for_gmail_status, is_client_secret_file,
    is_verified_channel_id, normalize_gmail, promote_token_to_channel_id,
    safe_channel_id_token_filename, safe_email_token_filename,
)


ALLOWED_STATUSES = {"ACTIVE", "INACTIVE", "DISABLED", "TOKEN_MISSING", "INVALID_TOKEN"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _mask(value: Any) -> str:
    return "••••••••" if _clean(value) else ""


def _basename(value: Any) -> str:
    raw = _clean(value).replace("\\", "/")
    return raw.rsplit("/", 1)[-1] if raw else ""


def _proxy_display(ch: Dict[str, Any]) -> str:
    host = _clean(ch.get("proxy_host"))
    port = _clean(ch.get("proxy_port"))
    return f"{host}:{port}" if host and port else (host or "Direct")


def _client_keys(gmail: str) -> set[str]:
    email = normalize_gmail(gmail)
    if not email:
        return set()
    safe = safe_email_token_filename(email).removesuffix(".json").lower()
    return {email, safe, email.replace("@", "_").replace(".", "_")}


def config_status_for_gmail(gmail: str, web_clients: Iterable[Dict[str, Any]] | None = None) -> Tuple[str, str]:
    if web_clients:
        return "OK", "OAuth client is available for this account"
    shared = list_oauth_client_files(CONFIG_DIR)
    if shared:
        return "OK", f"Shared OAuth client: config/{shared[0].name}"
    return "MISSING", "Shared OAuth client is not configured. Existing valid channel tokens can still upload; new/reconnect OAuth is blocked."


def token_status_for_channel(gmail: str, channel_id: str, same_gmail_count: int) -> Tuple[str, str]:
    if not is_verified_channel_id(channel_id):
        return "WAITING FOR OAUTH", "A verified UC... YouTube Channel ID is required before a canonical token can be assigned."
    path, status = find_token_for_gmail_status(
        str(TOKEN_DIR), gmail, channel_id, allow_legacy=same_gmail_count <= 1
    )
    p = Path(path) if path else None
    canonical = f"{channel_id}.json"
    if status == VALID_TOKEN:
        if p and p.name.lower() != canonical.lower():
            return "LEGACY", f"Valid legacy token {p.name}; canonical target is {canonical}"
        return "OK", f"Valid permanent token data/tokens/{p.name if p else canonical}"
    if status == MISSING_TOKEN:
        return "MISSING", f"Expected permanent data/tokens/{canonical}"
    if status == CLIENT_SECRET:
        return "INVALID", "A client-secret JSON is incorrectly stored in the token folder."
    if status == BAD_JSON:
        return "INVALID", f"Bad token JSON: {p.name if p else canonical}"
    return "INVALID", f"Invalid/expired/unusable token: {p.name if p else canonical}"


def stored_token_status_for_channel(channel_id: str, stored_path: str) -> Tuple[str, str]:
    """Copyright only: resolve the channel's DB-linked token, never a global filename.

    The owner claim is checked by the caller before the channel row is visible.
    Imported tokens live in an owner-private directory, not global TOKEN_DIR.
    """
    if not is_verified_channel_id(channel_id):
        return "WAITING FOR OAUTH", "Verified YouTube UC Channel ID is required."
    if not _clean(stored_path):
        return "MISSING", "No authorized token is linked to this account's channel."
    state = classify_google_json(stored_path)
    if state == VALID_TOKEN:
        return "OK", "Valid authorized token linked to this account's channel."
    if state == MISSING_TOKEN:
        return "MISSING", "The linked authorized token file was not found."
    if state == CLIENT_SECRET:
        return "INVALID", "Linked file is an OAuth client JSON, not a channel token."
    if state == BAD_JSON:
        return "INVALID", "Linked channel token JSON could not be parsed."
    return "INVALID", "Linked channel token is invalid, expired or unusable."


def channels_page_context(db: Any, web_clients: Iterable[Dict[str, Any]] | None = None,
                          allowed_channel_ids=None, allowed_sheet_ids=None,
                          inventory_channels=None, *, prefer_stored_token_paths=False) -> Dict[str, Any]:
    channels = (list(inventory_channels) if inventory_channels is not None
                else db.list_channels(active_only=False))
    # A tenant may create a brand-new manual channel before attaching a Sheet.
    # Do not let the PC-Sheets inventory filter hide that owned channel, or
    # load credentials for another tenant while building the channel manager.
    if allowed_channel_ids is not None:
        allowed={int(i) for i in allowed_channel_ids}
        channels=[row for row in channels if int(row["id"]) in allowed]
        present={int(row["id"]) for row in channels}
        for cid in sorted(allowed-present):
            row=db.get_channel(cid)
            if row:
                channels.append(row)
    groups_map = db.channel_group_names_map()
    sheets = db.list_sheet_sources()
    if allowed_sheet_ids is not None:
        sheets=[row for row in sheets if int(row["id"]) in allowed_sheet_ids]
    sheet_names = {int(s["id"]): _clean(s.get("sheet_name")) or f"Sheet #{s['id']}" for s in sheets}
    gmail_counts: Dict[str, int] = {}
    for ch in channels:
        email = normalize_gmail(_clean(ch.get("google_account_email")))
        gmail_counts[email] = gmail_counts.get(email, 0) + 1

    rows = []
    summary = {
        "total": len(channels), "active": 0, "inactive": 0, "disabled": 0,
        "token_missing": 0, "token_invalid": 0, "token_ok": 0,
        "config_missing": 0, "config_invalid": 0,
    }
    for ch in channels:
        gmail = normalize_gmail(_clean(ch.get("google_account_email")))
        channel_id = _clean(ch.get("channel_id"))
        config_status, config_detail = config_status_for_gmail(gmail, web_clients)
        token_status, token_detail = (
            stored_token_status_for_channel(channel_id, _clean(ch.get("token_file_path")))
            if prefer_stored_token_paths else
            token_status_for_channel(gmail, channel_id, gmail_counts.get(gmail, 1))
        )
        status = _clean(ch.get("status")).upper() or "INACTIVE"
        if status == "ACTIVE": summary["active"] += 1
        elif status == "DISABLED": summary["disabled"] += 1
        else: summary["inactive"] += 1
        if token_status in {"MISSING", "WAITING FOR OAUTH"}: summary["token_missing"] += 1
        elif token_status == "INVALID": summary["token_invalid"] += 1
        elif token_status in {"OK", "LEGACY"}: summary["token_ok"] += 1
        if config_status == "MISSING": summary["config_missing"] += 1
        elif config_status == "INVALID": summary["config_invalid"] += 1

        sheet_id = int(ch.get("sheet_source_id") or 0)
        row = {
            "id": int(ch.get("id") or 0),
            "status": status,
            "channel_name": _clean(ch.get("channel_name")),
            "gmail": gmail,
            "groups": _clean(groups_map.get(int(ch.get("id") or 0))),
            "config_status": config_status,
            "config_detail": config_detail,
            "token_status": token_status,
            "token_detail": token_detail,
            "browser_profile": _clean(ch.get("browser_profile_name")),
            "channel_id": channel_id,
            "channel_id_display": channel_id if is_verified_channel_id(channel_id) else "Pending OAuth",
            "channel_url": _clean(ch.get("channel_url")),
            "proxy": _proxy_display(ch),
            "proxy_scheme": _clean(ch.get("proxy_scheme")) or "http",
            "proxy_host": _clean(ch.get("proxy_host")),
            "proxy_port": _clean(ch.get("proxy_port")),
            "proxy_username": _clean(ch.get("proxy_username")),
            "proxy_password_masked": _mask(ch.get("proxy_password")),
            "token_file": _basename(ch.get("token_file_path")),
            "sheet_name": sheet_names.get(sheet_id, "Manual / local"),
            "sheet_source_id": sheet_id,
            "sheet_notes": _clean(ch.get("sheet_notes")),
            "last_used": _clean(ch.get("last_used_at")),
            "created": _clean(ch.get("created_at")),
        }
        row["search_blob"] = " ".join([
            str(row["id"]), row["channel_name"], row["gmail"], row["groups"],
            row["config_status"], row["config_detail"], row["token_status"], row["token_detail"],
            row["browser_profile"], row["channel_id"], row["channel_url"], row["proxy"],
            row["proxy_username"], row["sheet_name"], row["sheet_notes"], row["status"],
        ]).lower()
        rows.append(row)

    inventory = oauth_file_inventory()
    return {
        "rows": rows,
        "summary": summary,
        "groups": [dict(g) for g in db.list_channel_groups()],
        "sheets": [dict(s) for s in sheets],
        "oauth_inventory": inventory,
    }


def oauth_file_inventory() -> Dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    configs = []
    for p in sorted(CONFIG_DIR.glob("*.json"), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        configs.append({
            "name": p.name,
            "valid": bool(is_client_secret_file(p)),
            "size": p.stat().st_size,
        })
    tokens = []
    for p in sorted(TOKEN_DIR.glob("*.json"), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        cls = classify_google_json(p)
        tokens.append({
            "name": p.name,
            "status": "OK" if cls == VALID_TOKEN else ("CLIENT SECRET" if cls == CLIENT_SECRET else "INVALID"),
            "size": p.stat().st_size,
        })
    return {
        "config_total": len(configs),
        "config_valid": sum(1 for x in configs if x["valid"]),
        "config_invalid": sum(1 for x in configs if not x["valid"]),
        "token_total": len(tokens),
        "token_valid": sum(1 for x in tokens if x["status"] == "OK"),
        "token_invalid": sum(1 for x in tokens if x["status"] != "OK"),
        "configs": configs[:100],
        "tokens": tokens[:100],
    }


def set_status_bulk(db: Any, channel_ids: Iterable[int], status: str, *, prefer_stored_token_paths=False) -> int:
    state = _clean(status).upper()
    if state not in {"ACTIVE", "INACTIVE", "DISABLED"}:
        raise ValueError("Unsupported channel status.")
    ids = sorted({int(x) for x in channel_ids if int(x) > 0})
    if not ids:
        raise ValueError("Select one or more channels first.")
    if state == "ACTIVE":
        allowed = []
        for cid in ids:
            ch = db.get_channel(cid)
            if not ch:
                continue
            gmail = normalize_gmail(_clean(ch.get("google_account_email")))
            channel_id = _clean(ch.get("channel_id"))
            same = sum(
                1 for row in db.list_channels(active_only=False)
                if normalize_gmail(_clean(row.get("google_account_email"))) == gmail
            )
            token_status, _ = (
                stored_token_status_for_channel(channel_id, _clean(ch.get("token_file_path")))
                if prefer_stored_token_paths else token_status_for_channel(gmail, channel_id, same)
            )
            if token_status in {"OK", "LEGACY"}:
                allowed.append(cid)
        if not allowed:
            raise ValueError("None of the selected channels has a valid authorized token. Activation was blocked.")
        return int(db.set_channel_status_bulk(allowed, "ACTIVE"))
    return int(db.set_channel_status_bulk(ids, state))


def delete_manual_selected(db: Any, channel_ids: Iterable[int]) -> Dict[str, int]:
    ids = sorted({int(x) for x in channel_ids if int(x) > 0})
    if not ids:
        raise ValueError("Select one or more channels first.")
    return db.delete_manual_channels(ids)


def rescan_channel_tokens(db: Any, channel_ids: Iterable[int] | None = None,
                          *, prefer_stored_token_paths=False) -> Dict[str, Any]:
    if prefer_stored_token_paths:
        # Copyright tokens are private. Never "repair" them from global TOKEN_DIR,
        # which would overwrite an owner's valid DB pointer or expose foreign files.
        # A rescan only checks explicitly authorized IDs; it cannot claim data.
        selected = sorted({int(i) for i in (channel_ids or []) if int(i) > 0})
        if not selected:
            raise ValueError("Select owned channels for a private token scan")
        result = {"checked": 0, "updated": 0, "valid": 0,
                  "missing": 0, "invalid": 0, "blocked_activation": 0}
        for channel_db_id in selected:
            channel = db.get_channel(channel_db_id)
            if channel is None:
                continue
            status, _ = stored_token_status_for_channel(
                _clean(channel.get("channel_id")), _clean(channel.get("token_file_path"))
            )
            result["checked"] += 1
            if status == "OK":
                result["valid"] += 1
            elif status == "INVALID":
                result["invalid"] += 1
            else:
                result["missing"] += 1
        return result
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    channels = db.list_channels(active_only=False)
    wanted = set(int(x) for x in (channel_ids or []) if int(x) > 0)
    gmail_counts: Dict[str, int] = {}
    for row in channels:
        email = normalize_gmail(_clean(row.get("google_account_email")))
        gmail_counts[email] = gmail_counts.get(email, 0) + 1
    result = {"checked": 0, "updated": 0, "missing": 0, "invalid": 0, "blocked_activation": 0}
    for ch in channels:
        cid = int(ch.get("id") or 0)
        if wanted and cid not in wanted:
            continue
        gmail = normalize_gmail(_clean(ch.get("google_account_email")))
        channel_id = _clean(ch.get("channel_id"))
        current = _clean(ch.get("status")).upper()
        path, state = find_token_for_gmail_status(
            str(TOKEN_DIR), gmail, channel_id,
            allow_legacy=gmail_counts.get(gmail, 1) <= 1,
        )
        result["checked"] += 1
        if state == VALID_TOKEN and is_verified_channel_id(channel_id):
            canonical = promote_token_to_channel_id(path, channel_id)
            effective = current if current in {"INACTIVE", "DISABLED"} else "ACTIVE"
            db.update_channel_token_file(cid, str(canonical), effective)
            result["updated"] += 1
        elif state in {CLIENT_SECRET, BAD_JSON, INVALID_TOKEN}:
            db.update_channel_token_file(cid, path, "INVALID_TOKEN")
            result["invalid"] += 1
        else:
            db.update_channel_token_file(cid, path, "TOKEN_MISSING")
            result["missing"] += 1
    return result


def test_connections(db: Any, channel_ids: Iterable[int]) -> Dict[str, Any]:
    # Lazy import keeps ordinary Channels page rendering lightweight.
    from desktop_core.modules.youtube_uploader import YouTubeUploader
    ids = sorted({int(x) for x in channel_ids if int(x) > 0})
    if not ids:
        raise ValueError("Select one or more channels first.")
    channels = [db.get_channel(cid) for cid in ids]
    channels = [c for c in channels if c]
    def one(ch: Dict[str, Any]) -> Dict[str, Any]:
        proxy = {
            "proxy_scheme": ch.get("proxy_scheme"),
            "proxy_host": ch.get("proxy_host"),
            "proxy_port": ch.get("proxy_port"),
            "proxy_username": ch.get("proxy_username"),
            "proxy_password": ch.get("proxy_password"),
        }
        ok = YouTubeUploader.internet_available(proxy, timeout=3, max_urls=1)
        return {"id": int(ch["id"]), "channel": _clean(ch.get("channel_name")) or _clean(ch.get("channel_id")), "ok": bool(ok), "proxy": bool(_clean(ch.get("proxy_host")))}
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, min(20, len(channels)))) as ex:
        futures = [ex.submit(one, c) for c in channels]
        for f in as_completed(futures):
            try:
                rows.append(f.result())
            except Exception as exc:
                rows.append({"id": 0, "channel": "Unknown", "ok": False, "error": str(exc)})
    passed = sum(1 for r in rows if r.get("ok"))
    failed = len(rows) - passed
    for r in rows:
        try:
            db.add_log(None, "INFO" if r.get("ok") else "WARNING",
                       f"Connection test {'passed' if r.get('ok') else 'failed'}: {r.get('channel')} ({'proxy' if r.get('proxy') else 'direct'})", None)
        except Exception:
            pass
    return {"passed": passed, "failed": failed, "rows": rows}


def _safe_upload_name(filename: str) -> str:
    name = Path(str(filename or "").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9@._+-]+", "_", name)[:220]
    if not name.lower().endswith(".json"):
        name += ".json"
    return name


def install_client_json(filename: str, payload: bytes, *, replace_valid: bool = False) -> Dict[str, Any]:
    if len(payload) > 2 * 1024 * 1024:
        return {"name": filename, "status": "INVALID", "reason": "File exceeds 2 MB."}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp.write(payload)
        temp_path = Path(tmp.name)
    try:
        if not is_client_secret_file(temp_path):
            return {"name": filename, "status": "INVALID", "reason": "Not a Google OAuth client-secret JSON."}
        name = _safe_upload_name(filename)
        dest = CONFIG_DIR / name
        if dest.exists() and is_client_secret_file(dest) and not replace_valid:
            return {"name": name, "status": "KEPT", "reason": "Existing valid VPS OAuth client kept."}
        if dest.exists():
            q = CONFIG_DIR / "quarantine"
            q.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, q / f"{dest.stem}.replaced{dest.suffix}")
        tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        tmp_dest.write_bytes(payload)
        secure_file_for_current_user(tmp_dest)
        os.replace(tmp_dest, dest)
        secure_file_for_current_user(dest)
        return {"name": name, "status": "INSTALLED", "path": str(dest)}
    finally:
        temp_path.unlink(missing_ok=True)


def _channels_by_identity(db: Any) -> tuple[dict[str, Dict[str, Any]], dict[str, list[Dict[str, Any]]]]:
    channels = db.list_channels(active_only=False)
    by_uc = {}
    by_gmail: Dict[str, list[Dict[str, Any]]] = {}
    for ch in channels:
        cid = _clean(ch.get("channel_id"))
        if is_verified_channel_id(cid):
            by_uc[cid.lower()] = ch
        email = normalize_gmail(_clean(ch.get("google_account_email")))
        if email:
            by_gmail.setdefault(email, []).append(ch)
    return by_uc, by_gmail


def install_token_json(db: Any, filename: str, payload: bytes, *, replace_valid: bool = False) -> Dict[str, Any]:
    if len(payload) > 4 * 1024 * 1024:
        return {"name": filename, "status": "INVALID", "reason": "File exceeds 4 MB."}
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp.write(payload)
        temp_path = Path(tmp.name)
    try:
        if classify_google_json(temp_path) != VALID_TOKEN:
            return {"name": filename, "status": "INVALID", "reason": "Not a reusable authorized-channel token JSON."}
        stem = Path(_safe_upload_name(filename)).stem
        by_uc, by_gmail = _channels_by_identity(db)
        matched = None
        if is_verified_channel_id(stem):
            matched = by_uc.get(stem.lower())
        if not matched:
            email = normalize_gmail(stem)
            rows = by_gmail.get(email, [])
            if len(rows) == 1:
                matched = rows[0]
            elif len(rows) > 1:
                return {"name": filename, "status": "CONFLICT", "reason": "Multiple Brand Channels use this Gmail; ChannelID.json is required."}
        if not matched:
            return {"name": filename, "status": "UNMATCHED", "reason": "No unique current channel identity matched this token."}
        channel_id = _clean(matched.get("channel_id"))
        if not is_verified_channel_id(channel_id):
            return {"name": filename, "status": "UNMATCHED", "reason": "Matched row has no verified UC Channel ID yet."}
        dest = TOKEN_DIR / safe_channel_id_token_filename(channel_id)
        if dest.exists() and classify_google_json(dest) == VALID_TOKEN and not replace_valid:
            db.update_channel_token_file(int(matched["id"]), str(dest), _clean(matched.get("status")) or "ACTIVE")
            return {"name": filename, "status": "KEPT", "reason": "Existing valid VPS ChannelID token kept.", "channel_id": channel_id}
        if dest.exists():
            q = TOKEN_DIR / "quarantine"
            q.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, q / f"{dest.stem}.replaced{dest.suffix}")
        tmp_dest = dest.with_suffix(".json.tmp")
        tmp_dest.write_bytes(payload)
        secure_file_for_current_user(tmp_dest)
        os.replace(tmp_dest, dest)
        secure_file_for_current_user(dest)
        current = _clean(matched.get("status")).upper()
        effective = current if current in {"INACTIVE", "DISABLED"} else "ACTIVE"
        db.update_channel_token_file(int(matched["id"]), str(dest), effective)
        return {"name": filename, "status": "INSTALLED", "channel_id": channel_id, "channel": _clean(matched.get("channel_name"))}
    finally:
        temp_path.unlink(missing_ok=True)
