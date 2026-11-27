"""Owner-private Copyright Uploader PC JSON import.

Never writes the legacy global config or shared TOKEN_DIR; owner claims are
checked again inside the SQLite transaction that changes the channel pointer.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
from pathlib import Path

from desktop_core.modules.path_utils import TOKEN_DIR
from desktop_core.modules.secret_store import secure_file_for_current_user
from desktop_core.modules.token_utils import (
    VALID_TOKEN, classify_google_json, is_verified_channel_id,
    normalize_gmail, safe_channel_id_token_filename,
)
from .oauth_web import _owner_client_path, list_owner_clients, save_owner_client_json
from .studio_tenants import StudioTenants

MAX_TOKEN_BYTES = 4 * 1024 * 1024
MAX_CLIENT_BYTES = 1024 * 1024


def _token_root(username: str) -> Path:
    owner = StudioTenants.username(username)
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]
    return TOKEN_DIR / "owners" / digest


def owner_vault_inventory(db, tenants, username: str) -> dict:
    owner = StudioTenants.username(username)
    allowed = tenants.allowed_ids("channel", {"username": owner, "is_admin": 0}) or set()
    # db.list_channels() intentionally lists only Sheet-present rows; a PC
    # token may be for a user-owned manually registered channel as well.
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id,channel_id,channel_name,token_file_path FROM channels ORDER BY id"
        ).fetchall() if int(r["id"]) in allowed]
    tokens = []
    for row in rows:
        token_path = str(row.get("token_file_path") or "").strip()
        valid = bool(token_path) and classify_google_json(token_path) == VALID_TOKEN
        tokens.append({
            "name": str(row.get("channel_id") or "Unnamed UC channel") + ".json",
            "status": "OK" if valid else "MISSING / INVALID",
            "channel_name": str(row.get("channel_name") or ""),
        })
    private = _owner_client_path(owner)
    own_client = private.is_file()
    clients = list_owner_clients(owner)
    return {
        "config_total": 1 if own_client else 0,
        "config_valid": 1 if own_client and clients and clients[0]["label"] == "my_oauth_client" else 0,
        "config_invalid": 1 if own_client and not (clients and clients[0]["label"] == "my_oauth_client") else 0,
        "token_total": len(tokens),
        "token_valid": sum(t["status"] == "OK" for t in tokens),
        "token_invalid": sum(t["status"] != "OK" for t in tokens),
        "configs": ([{"name": "My OAuth client (private)", "valid": True}]
                    if own_client and clients and clients[0]["label"] == "my_oauth_client" else []),
        "tokens": tokens[:200],
        "shared_fallback": bool(clients and clients[0]["label"] == "shared_oauth_client"),
    }


def import_client(username: str, payload: bytes, *, replace_valid: bool = False) -> dict:
    owner = StudioTenants.username(username)
    existing = _owner_client_path(owner)
    if existing.is_file() and not replace_valid:
        from desktop_core.modules.token_utils import is_client_secret_file
        if is_client_secret_file(existing):
            return {"status": "KEPT", "reason": "Your existing valid OAuth client was kept"}
    save_owner_client_json(owner, payload)
    return {"status": "INSTALLED"}


def _match_owned_channel(db, tenants, owner: str, filename: str) -> dict | None:
    name = Path(str(filename or "").replace("\\", "/")).name
    if not name.lower().endswith(".json"):
        raise ValueError("Select a .json file named ChannelID.json or unique Gmail.json")
    stem = name[:-5]
    with db.connect() as con:
        all_rows = [dict(r) for r in con.execute(
            "SELECT id,channel_id,channel_name,google_account_email,token_file_path,status FROM channels"
        ).fetchall()]
    if is_verified_channel_id(stem):
        matched = [r for r in all_rows if str(r.get("channel_id") or "").casefold() == stem.casefold()]
    else:
        gmail = normalize_gmail(stem)
        if not gmail or "@" not in gmail:
            raise ValueError("Use the exact UC ChannelID.json or a unique Gmail.json filename")
        matched = [r for r in all_rows
                   if normalize_gmail(str(r.get("google_account_email") or "")) == gmail]
        if len(matched) > 1:
            raise ValueError("Gmail maps to multiple channels; use exact UC ChannelID.json")
    if len(matched) != 1:
        return None
    channel = matched[0]
    # No cross-owner lookups exposed in responses; foreign is an unmatched file.
    if int(channel["id"]) not in (tenants.allowed_ids("channel", {"username": owner, "is_admin": 0}) or set()):
        return None
    if not is_verified_channel_id(str(channel.get("channel_id") or "")):
        return None
    return channel


def import_token(db, tenants, username: str, filename: str, payload: bytes,
                 *, replace_valid: bool = False) -> dict:
    owner = StudioTenants.username(username)
    if not payload or len(payload) > MAX_TOKEN_BYTES:
        return {"status": "INVALID", "reason": "Missing JSON or file exceeds 4 MB"}
    channel = _match_owned_channel(db, tenants, owner, filename)
    if not channel:
        return {"status": "UNMATCHED", "reason": "No unique owned channel matches this filename"}
    cid = int(channel["id"])
    tenants.require("channel", cid, {"username": owner, "is_admin": 0})
    root = _token_root(owner)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    dest = root / safe_channel_id_token_filename(str(channel["channel_id"]))
    temporary = root / (".upload-" + secrets.token_hex(12) + ".json")
    try:
        temporary.write_bytes(payload)
        temporary.chmod(0o600)
        if classify_google_json(temporary) != VALID_TOKEN:
            return {"status": "INVALID", "reason": "Not a reusable authorized-channel token JSON"}
        with db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            actual = con.execute("""SELECT c.id,c.status,c.token_file_path
                FROM channels c INNER JOIN studio_entity_owners own
                ON own.entity_kind='channel' AND own.entity_id=c.id
                WHERE c.id=? AND own.owner_username=? COLLATE NOCASE""",
                (cid, owner)).fetchone()
            if not actual:
                return {"status": "UNMATCHED", "reason": "Channel no longer belongs to this account"}
            old_path = str(actual["token_file_path"] or "").strip()
            if not replace_valid and old_path and classify_google_json(old_path) == VALID_TOKEN:
                return {"status": "KEPT", "reason": "Existing valid channel token kept"}
            if not replace_valid and dest.is_file() and classify_google_json(dest) == VALID_TOKEN:
                status = str(actual["status"] or "").upper()
                effective = status if status in {"INACTIVE", "DISABLED"} else "ACTIVE"
                con.execute("UPDATE channels SET token_file_path=?,status=?,last_used_at=CURRENT_TIMESTAMP WHERE id=?",
                            (str(dest), effective, cid))
                return {"status": "KEPT", "reason": "Existing private token restored"}
            if dest.exists():
                backup = root / (dest.stem + ".replaced-" + secrets.token_hex(8) + ".json")
                shutil.copy2(dest, backup)
                secure_file_for_current_user(backup)
            os.replace(temporary, dest)
            secure_file_for_current_user(dest)
            status = str(actual["status"] or "").upper()
            effective = status if status in {"INACTIVE", "DISABLED"} else "ACTIVE"
            con.execute("UPDATE channels SET token_file_path=?,status=?,last_used_at=CURRENT_TIMESTAMP WHERE id=?",
                        (str(dest), effective, cid))
        return {"status": "INSTALLED", "channel_id": str(channel["channel_id"])}
    finally:
        temporary.unlink(missing_ok=True)
