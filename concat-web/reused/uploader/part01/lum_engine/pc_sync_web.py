from __future__ import annotations

from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from typing import Any, Dict, Iterable, List, Tuple

from desktop_core.modules.google_sheet_sync import extract_sheet_id
from desktop_core.modules.path_utils import CONFIG_DIR, TOKEN_DIR, USER_DATA_ROOT
from desktop_core.modules.token_utils import is_verified_channel_id, normalize_gmail
from .channels_web import install_client_json, install_token_json, rescan_channel_tokens
from .oauth_web import save_client_json
from .sheets_bridge import sync_sheet_source_by_id

SYNC_ROOT = USER_DATA_ROOT / "pc_sync"
SNAPSHOTS = SYNC_ROOT / "snapshots"
MAX_FILES = 5000
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_SINGLE = 512 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_rel(name: str) -> Path:
    value = str(name or "").replace("\\", "/").lstrip("/")
    parts = []
    for part in PurePosixPath(value).parts:
        if part in {"", ".", "/"}:
            continue
        if part == "..":
            raise ValueError("Unsafe parent path in upload.")
        clean = re.sub(r"[^A-Za-z0-9 @._()+,'&+-]+", "_", part)[:180]
        if clean:
            parts.append(clean)
    if not parts:
        raise ValueError("Uploaded file has no usable name.")
    return Path(*parts)


def _secure_tree(root: Path) -> None:
    try:
        os.chmod(root, 0o700)
    except Exception:
        pass
    for p in root.rglob("*"):
        try:
            os.chmod(p, 0o700 if p.is_dir() else 0o600)
        except Exception:
            pass


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    try: os.chmod(path, 0o600)
    except Exception: pass


def _extract_zip_safe(zip_path: Path, out: Path) -> None:
    total = 0
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            count += 1
            if count > MAX_FILES:
                raise ValueError(f"ZIP contains more than {MAX_FILES} files.")
            if info.file_size > MAX_SINGLE:
                raise ValueError(f"ZIP member too large: {info.filename}")
            total += int(info.file_size)
            if total > MAX_TOTAL:
                raise ValueError("ZIP uncompressed content exceeds safety limit.")
            rel = _safe_rel(info.filename)
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def save_snapshot_uploads(db: Any, files: List[Tuple[str, bytes]], source_label: str = "") -> Dict[str, Any]:
    if not files:
        raise ValueError("Select the PC data folder or a ZIP first.")
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    sid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    snap = SNAPSHOTS / sid
    original = snap / "original"
    original.mkdir(parents=True)
    total = 0
    manifest = []
    if len(files) > MAX_FILES:
        raise ValueError(f"Upload contains more than {MAX_FILES} files.")
    for name, payload in files:
        if len(payload) > MAX_SINGLE:
            raise ValueError(f"File too large: {name}")
        total += len(payload)
        if total > MAX_TOTAL:
            raise ValueError("Uploaded snapshot exceeds safety limit.")
        rel = _safe_rel(name)
        dest = original / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        manifest.append({"path": str(rel).replace("\\", "/"), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    # If user supplied one ZIP, extract into immutable extracted/ view.
    extracted = snap / "extracted"
    if len(files) == 1 and Path(files[0][0]).suffix.lower() == ".zip":
        extracted.mkdir()
        _extract_zip_safe(original / _safe_rel(files[0][0]), extracted)
        scan_root = extracted
    else:
        scan_root = original
    _write_json(snap/"manifest.json", {
        "snapshot_id": sid, "source_label": str(source_label or "").strip()[:120],
        "created_at": _now(), "file_count": len(files), "total_bytes": total,
        "files": manifest,
    })
    _secure_tree(snap)
    preview = build_preview(db, scan_root, sid)
    _write_json(snap/"preview.json", preview)
    return preview


def _find_db(scan_root: Path) -> Path | None:
    candidates = [p for p in scan_root.rglob("uploader.db") if p.is_file()]
    if not candidates:
        return None
    def score(p: Path):
        rel = str(p.relative_to(scan_root)).replace("\\","/").lower()
        return (2 if rel.endswith("data/uploader.db") else 1, p.stat().st_size)
    return sorted(candidates, key=score, reverse=True)[0]


def _read_table(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return []
    return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"').fetchall()]


def _source_db_data(db_path: Path | None) -> Dict[str, Any]:
    if not db_path:
        return {"channels": [], "sheet_sources": [], "groups": [], "group_members": [], "tables": {}}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            try: tables[name] = int(con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            except Exception: tables[name] = -1
        return {
            "channels": _read_table(con, "channels"),
            "sheet_sources": _read_table(con, "sheet_sources"),
            "groups": _read_table(con, "channel_groups"),
            "group_members": _read_table(con, "channel_group_members"),
            "tables": tables,
        }
    finally:
        con.close()


def _classify_files(scan_root: Path) -> Dict[str, Any]:
    configs = []
    tokens = []
    ignored = []
    for p in scan_root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(scan_root)).replace("\\", "/")
        low = rel.lower()
        if p.suffix.lower() == ".json" and ("/config/" in f"/{low}" or low.startswith("config/")):
            configs.append({"rel": rel, "path": p})
        elif p.suffix.lower() == ".json" and ("/tokens/" in f"/{low}" or low.startswith("tokens/") or low.startswith("data/tokens/")):
            tokens.append({"rel": rel, "path": p})
        elif p.name.lower() in {"application.lock", "ui_session.json", "portable_state.json", "token_backup_state.json", "drive_backup_state.json"}:
            ignored.append(rel)
    return {"configs": configs, "tokens": tokens, "ignored": ignored}


def _norm_url(value: Any) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _target_snapshot(db: Any) -> Dict[str, Any]:
    with db.connect() as con:
        target_channels = [dict(r) for r in con.execute("SELECT * FROM channels ORDER BY id").fetchall()]
    sheets = db.list_sheet_sources()
    return {"channels": target_channels, "sheets": sheets}


def _sheet_id_safe(value: Any) -> str:
    try: return extract_sheet_id(str(value or "").strip())
    except Exception: return ""


def _match_source_channel(src: Dict[str, Any], source_sheet_name: str, target: Dict[str, Any], target_sheet_by_name: Dict[str,int]) -> Tuple[str, int | None]:
    cid = str(src.get("channel_id") or "").strip()
    if is_verified_channel_id(cid):
        matches = [r for r in target["channels"] if str(r.get("channel_id") or "").strip().lower() == cid.lower()]
        if len(matches) == 1:
            return "UC_ID", int(matches[0]["id"])
        if len(matches) > 1:
            return "CONFLICT", None
    row_key = str(src.get("sheet_row_key") or "").strip().lower()
    tgt_sid = target_sheet_by_name.get(source_sheet_name.lower())
    if row_key and tgt_sid:
        matches = [r for r in target["channels"] if int(r.get("sheet_source_id") or 0)==tgt_sid and str(r.get("sheet_row_key") or "").strip().lower()==row_key]
        if len(matches)==1:
            return "SHEET_ROW", int(matches[0]["id"])
    url = _norm_url(src.get("channel_url"))
    if url:
        matches = [r for r in target["channels"] if _norm_url(r.get("channel_url")) == url]
        if len(matches)==1:
            return "CHANNEL_URL", int(matches[0]["id"])
        if len(matches)>1:
            return "CONFLICT", None
    email = normalize_gmail(str(src.get("google_account_email") or ""))
    name = str(src.get("channel_name") or "").strip().lower()
    if email and name:
        matches = [r for r in target["channels"] if normalize_gmail(str(r.get("google_account_email") or ""))==email and str(r.get("channel_name") or "").strip().lower()==name]
        if len(matches)==1:
            return "GMAIL_NAME", int(matches[0]["id"])
        if len(matches)>1:
            return "CONFLICT", None
    return "NEW", None


def build_preview(db: Any, scan_root: Path, snapshot_id: str) -> Dict[str, Any]:
    db_path = _find_db(scan_root)
    source = _source_db_data(db_path)
    files = _classify_files(scan_root)
    current_channels = [
        r for r in source["channels"]
        if r.get("sheet_source_id") is not None
        and int(r.get("sheet_present") if r.get("sheet_present") is not None else 1) == 1
    ]
    historical = [r for r in source["channels"] if r not in current_channels]

    target = _target_snapshot(db)
    target_sheets_by_sheetid = {}
    for sh in target["sheets"]:
        sid = _sheet_id_safe(sh.get("sheet_url_or_id"))
        if sid:
            target_sheets_by_sheetid[sid] = int(sh["id"])

    source_sheet_by_id = {int(x.get("id") or 0): x for x in source["sheet_sources"]}
    comparison = {
        "new": 0, "exact_match": 0, "update_available": 0, "conflict": 0,
        "pc_has_missing_data": 0, "token_recoverable": 0, "client_recoverable": 0,
    }
    preview_rows = []
    for src in current_channels:
        src_sheet = source_sheet_by_id.get(int(src.get("sheet_source_id") or 0), {})
        source_sheet_id = _sheet_id_safe(src_sheet.get("sheet_url_or_id"))
        target_sheet_id = target_sheets_by_sheetid.get(source_sheet_id)
        cid = str(src.get("channel_id") or "").strip()
        match_reason = "NEW"
        target_row = None

        if is_verified_channel_id(cid):
            matches = [r for r in target["channels"] if str(r.get("channel_id") or "").strip().lower() == cid.lower()]
            if len(matches) == 1:
                target_row = matches[0]; match_reason = "UC_ID"
            elif len(matches) > 1:
                match_reason = "CONFLICT"

        if target_row is None and match_reason != "CONFLICT" and target_sheet_id:
            key = str(src.get("sheet_row_key") or "").strip().lower()
            if key:
                matches = [
                    r for r in target["channels"]
                    if int(r.get("sheet_source_id") or 0) == target_sheet_id
                    and str(r.get("sheet_row_key") or "").strip().lower() == key
                ]
                if len(matches) == 1:
                    target_row = matches[0]; match_reason = "SHEET_ROW"
                elif len(matches) > 1:
                    match_reason = "CONFLICT"

        if target_row is None and match_reason != "CONFLICT":
            url = _norm_url(src.get("channel_url"))
            if url:
                matches = [r for r in target["channels"] if _norm_url(r.get("channel_url")) == url]
                if len(matches) == 1:
                    target_row = matches[0]; match_reason = "CHANNEL_URL"
                elif len(matches) > 1:
                    match_reason = "CONFLICT"

        if target_row is None and match_reason != "CONFLICT":
            email = normalize_gmail(str(src.get("google_account_email") or ""))
            name = str(src.get("channel_name") or "").strip().lower()
            if email and name:
                matches = [
                    r for r in target["channels"]
                    if normalize_gmail(str(r.get("google_account_email") or "")) == email
                    and str(r.get("channel_name") or "").strip().lower() == name
                ]
                if len(matches) == 1:
                    target_row = matches[0]; match_reason = "GMAIL_NAME"
                elif len(matches) > 1:
                    match_reason = "CONFLICT"

        if match_reason == "CONFLICT":
            state = "CONFLICT"; comparison["conflict"] += 1
        elif target_row is None:
            state = "NEW"; comparison["new"] += 1
        else:
            compared_fields = [
                ("channel_name","channel_name"),("google_account_email","google_account_email"),
                ("channel_url","channel_url"),("browser_profile_name","browser_profile_name"),
                ("proxy_scheme","proxy_scheme"),("proxy_host","proxy_host"),
                ("proxy_port","proxy_port"),("proxy_username","proxy_username"),
                ("sheet_notes","sheet_notes"),
            ]
            changed = any(
                str(src.get(a) or "").strip() != str(target_row.get(b) or "").strip()
                for a,b in compared_fields
            )
            if changed:
                state = "UPDATE AVAILABLE"; comparison["update_available"] += 1
            else:
                state = "EXACT MATCH"; comparison["exact_match"] += 1

        preview_rows.append({
            "pc_id": int(src.get("id") or 0),
            "channel": str(src.get("channel_name") or ""),
            "gmail": str(src.get("google_account_email") or ""),
            "channel_id": cid,
            "state": state,
            "match": match_reason,
            "target_id": int(target_row.get("id") or 0) if target_row else None,
        })

    # Credential recovery counts are conservative and never inspect secret contents in UI.
    target_channels = target["channels"]
    by_uc = {
        str(r.get("channel_id") or "").strip().lower(): r for r in target_channels
        if is_verified_channel_id(str(r.get("channel_id") or ""))
    }
    gmail_counts = {}
    for r in target_channels:
        email = normalize_gmail(str(r.get("google_account_email") or ""))
        if email: gmail_counts[email] = gmail_counts.get(email,0)+1

    for item in files["tokens"]:
        stem = item["path"].stem
        if is_verified_channel_id(stem) and stem.lower() in by_uc:
            comparison["token_recoverable"] += 1
        else:
            email = normalize_gmail(stem)
            if email and gmail_counts.get(email,0) == 1:
                comparison["token_recoverable"] += 1

    target_config_names = {p.name.lower() for p in CONFIG_DIR.glob("*.json") if p.is_file()}
    for item in files["configs"]:
        if item["path"].name.lower() not in target_config_names:
            comparison["client_recoverable"] += 1

    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "created_at": _now(),
        "pc_db_found": bool(db_path),
        "pc_db_relative": str(db_path.relative_to(scan_root)).replace("\\","/") if db_path else "",
        "pc_channels_current": len(current_channels),
        "pc_channels_archived_only": len(historical),
        "pc_sheet_sources": len(source["sheet_sources"]),
        "pc_groups": len(source["groups"]),
        "pc_config_jsons": len(files["configs"]),
        "pc_token_jsons": len(files["tokens"]),
        "runtime_files_ignored": files["ignored"],
        "tables": source["tables"],
        "comparison": comparison,
        "channel_preview": preview_rows[:200],
        "message": "Snapshot uploaded, compared with the VPS and previewed. No live VPS data has been changed.",
    }


def list_sync_history(limit: int = 20) -> List[Dict[str, Any]]:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    rows = []
    for snap in sorted([p for p in SNAPSHOTS.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)[:limit]:
        manifest = {}
        preview = {}
        result = {}
        try: manifest = json.loads((snap/"manifest.json").read_text())
        except Exception: pass
        try: preview = json.loads((snap/"preview.json").read_text())
        except Exception: pass
        try: result = json.loads((snap/"result.json").read_text())
        except Exception: pass
        rows.append({
            "snapshot_id": snap.name,
            "created_at": manifest.get("created_at") or preview.get("created_at") or "",
            "source_label": manifest.get("source_label") or "",
            "channels": preview.get("pc_channels_current", 0),
            "tokens": preview.get("pc_token_jsons", 0),
            "configs": preview.get("pc_config_jsons", 0),
            "state": "COMPLETED" if result.get("ok") else ("FAILED" if result else "PREVIEWED"),
            "result": result.get("summary") or "",
        })
    return rows


def _scan_root(snap: Path) -> Path:
    return snap/"extracted" if (snap/"extracted").is_dir() else snap/"original"


def _dpapi_unusable(value: Any) -> bool:
    return str(value or "").startswith("dpapi:")


def apply_snapshot(db: Any, snapshot_id: str) -> Dict[str, Any]:
    sid = re.sub(r"[^A-Za-z0-9TtZz_-]", "", str(snapshot_id or ""))
    snap = SNAPSHOTS / sid
    if not snap.is_dir():
        raise ValueError("PC Sync snapshot not found.")
    if (snap/"result.json").exists():
        existing = json.loads((snap/"result.json").read_text())
        existing["already_applied"] = True
        return existing
    lock = snap/"apply.lock"
    try:
        fd = os.open(lock, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        raise ValueError("This PC Sync snapshot is already being applied.")

    result: Dict[str, Any] = {
        "ok": False, "snapshot_id": sid, "started_at": _now(),
        "sheets_added": 0, "sheets_matched": 0, "channels_synced": 0,
        "historical_archived": 0, "configs_installed": 0, "configs_kept": 0,
        "tokens_installed": 0, "tokens_kept": 0, "token_conflicts": 0,
        "windows_dpapi_secrets_skipped": 0, "sheet_resync_ok": 0, "sheet_resync_failed": 0,
        "errors": [],
    }
    try:
        scan_root = _scan_root(snap)
        source = _source_db_data(_find_db(scan_root))
        classified = _classify_files(scan_root)

        # 1. Permanent OAuth client JSONs. Existing valid VPS files win.
        for item in classified["configs"]:
            payload = item["path"].read_bytes()
            install = install_client_json(item["path"].name, payload, replace_valid=False)
            if install.get("status") == "INSTALLED":
                result["configs_installed"] += 1
                try: save_client_json(item["path"].name, payload)
                except Exception as exc: result["errors"].append(f"Web OAuth client mirror: {item['path'].name}: {exc}")
            elif install.get("status") == "KEPT":
                result["configs_kept"] += 1
            elif install.get("status") == "INVALID":
                result["errors"].append(f"Invalid OAuth client {item['path'].name}: {install.get('reason')}")

        # 2. Sheet sources, matched by real spreadsheet ID. FIRST_TAB is enforced.
        target_sheets = db.list_sheet_sources()
        target_by_sheetid = {_sheet_id_safe(x.get("sheet_url_or_id")): x for x in target_sheets if _sheet_id_safe(x.get("sheet_url_or_id"))}
        src_to_tgt: Dict[int, int] = {}
        for ss in source["sheet_sources"]:
            source_id = int(ss.get("id") or 0)
            sheet_id = _sheet_id_safe(ss.get("sheet_url_or_id"))
            if not sheet_id:
                result["errors"].append(f"Skipped sheet source #{source_id}: invalid/unsupported Sheet URL.")
                continue
            existing = target_by_sheetid.get(sheet_id)
            if existing:
                src_to_tgt[source_id] = int(existing["id"])
                result["sheets_matched"] += 1
            else:
                new_id = int(db.save_sheet_source(
                    source_id=None,
                    sheet_name=str(ss.get("sheet_name") or f"Imported Sheet {source_id}"),
                    sheet_url_or_id=str(ss.get("sheet_url_or_id") or ""),
                    gid="FIRST_TAB",
                    json_folder_path=str(TOKEN_DIR),
                    enabled=bool(int(ss.get("enabled") or 0)),
                ))
                src_to_tgt[source_id] = new_id
                result["sheets_added"] += 1
                target_by_sheetid[sheet_id] = db.get_sheet_source(new_id)

        # 3. Current Sheet inventory rows only. Old runtime/session rows remain archived in snapshot.
        current_source_rows = []
        for ch in source["channels"]:
            if ch.get("sheet_source_id") is None or int(ch.get("sheet_present") if ch.get("sheet_present") is not None else 1) != 1:
                result["historical_archived"] += 1
                continue
            current_source_rows.append(ch)

        with db.connect() as con:
            target_all = [dict(r) for r in con.execute("SELECT * FROM channels").fetchall()]
        target_by_sheetrow = {(int(r.get("sheet_source_id") or 0), str(r.get("sheet_row_key") or "").strip().lower()): r for r in target_all}

        source_sheet_names = {int(s.get("id") or 0): str(s.get("sheet_name") or "") for s in source["sheet_sources"]}
        source_group_names = {int(g.get("id") or 0): str(g.get("group_name") or "") for g in source["groups"]}
        source_groups_by_channel: Dict[int,List[str]] = {}
        for gm in source["group_members"]:
            gname = source_group_names.get(int(gm.get("group_id") or 0))
            if gname:
                source_groups_by_channel.setdefault(int(gm.get("channel_db_id") or 0), []).append(gname)

        imported_target_ids: List[int] = []
        for ch in current_source_rows:
            src_sheet = int(ch.get("sheet_source_id") or 0)
            tgt_sheet = src_to_tgt.get(src_sheet)
            if not tgt_sheet:
                result["errors"].append(f"Skipped channel {ch.get('channel_name')}: source Sheet was not imported.")
                continue
            row_key = str(ch.get("sheet_row_key") or ch.get("channel_id") or ch.get("google_account_email") or "").strip().lower()
            existing = target_by_sheetrow.get((tgt_sheet, row_key))
            def transferable(secret):
                nonlocal result
                if _dpapi_unusable(secret):
                    result["windows_dpapi_secrets_skipped"] += 1
                    return ""
                return str(secret or "")
            payload = {
                "channel_id": str(ch.get("channel_id") or "").strip(),
                "channel_name": str(ch.get("channel_name") or "").strip(),
                "gmail": str(ch.get("google_account_email") or "").strip(),
                "token_file_path": str(existing.get("token_file_path") or "") if existing else "",
                "channel_url": str(ch.get("channel_url") or "").strip(),
                "browser_profile_name": str(ch.get("browser_profile_name") or "").strip(),
                "proxy_scheme": str(ch.get("proxy_scheme") or "").strip(),
                "proxy_host": str(ch.get("proxy_host") or "").strip(),
                "proxy_port": str(ch.get("proxy_port") or "").strip(),
                "proxy_username": str(ch.get("proxy_username") or "").strip(),
                "proxy_password": (str(existing.get("proxy_password") or "") if existing and _dpapi_unusable(ch.get("proxy_password")) else transferable(ch.get("proxy_password"))),
                "sheet_row_key": row_key,
                "sheet_sr": str(ch.get("sheet_sr") or "").strip(),
                "sheet_ip_raw": str(ch.get("sheet_ip_raw") or "").strip(),
                "sheet_password": (str(existing.get("sheet_password") or "") if existing and _dpapi_unusable(ch.get("sheet_password")) else transferable(ch.get("sheet_password"))),
                "sheet_backup_code": (str(existing.get("sheet_backup_code") or "") if existing and _dpapi_unusable(ch.get("sheet_backup_code")) else transferable(ch.get("sheet_backup_code"))),
                "sheet_backup_key": (str(existing.get("sheet_backup_key") or "") if existing and _dpapi_unusable(ch.get("sheet_backup_key")) else transferable(ch.get("sheet_backup_key"))),
                "sheet_status": str(ch.get("sheet_status") or "").strip(),
                "sheet_notes": str(ch.get("sheet_notes") or "").strip(),
            }
            try:
                tid = int(db.upsert_sheet_channel(tgt_sheet, payload))
                imported_target_ids.append(tid)
                result["channels_synced"] += 1
                for gname in source_groups_by_channel.get(int(ch.get("id") or 0), []):
                    try:
                        gid = int(db.ensure_channel_group(gname, "Created/updated from Google Sheet sync"))
                        db.add_channels_to_group(gid, [tid])
                    except Exception as exc:
                        result["errors"].append(f"Group {gname}: {exc}")
            except Exception as exc:
                result["errors"].append(f"Channel {ch.get('channel_name') or ch.get('id')}: {exc}")

        # 4. Permanent authorized tokens. Existing valid VPS tokens win.
        for item in classified["tokens"]:
            install = install_token_json(db, item["path"].name, item["path"].read_bytes(), replace_valid=False)
            state = install.get("status")
            if state == "INSTALLED": result["tokens_installed"] += 1
            elif state == "KEPT": result["tokens_kept"] += 1
            elif state in {"CONFLICT", "UNMATCHED"}:
                result["token_conflicts"] += 1
                result["errors"].append(f"Token {item['path'].name}: {install.get('reason')}")
            elif state == "INVALID":
                result["errors"].append(f"Invalid token {item['path'].name}: {install.get('reason')}")

        # 5. Reconcile token safety. ACTIVE is never allowed without a valid token.
        token_result = rescan_channel_tokens(db, imported_target_ids or None)
        result["token_reconcile"] = token_result

        # 6. Current Google Sheet is final inventory/group authority when reachable.
        for target_sid in sorted(set(src_to_tgt.values())):
            target_source = db.get_sheet_source(target_sid)
            if not target_source or not int(target_source.get("enabled") or 0):
                continue
            sync = sync_sheet_source_by_id(db, target_sid)
            if sync.get("ok"):
                result["sheet_resync_ok"] += 1
            else:
                result["sheet_resync_failed"] += 1
                result["errors"].append(f"Sheet resync {target_source.get('sheet_name')}: {sync.get('error') or sync.get('status')}")

        result["ok"] = True
        result["completed_at"] = _now()
        result["summary"] = (
            f"Channels {result['channels_synced']} · Sheets +{result['sheets_added']}/{result['sheets_matched']} matched · "
            f"OAuth configs +{result['configs_installed']} · Tokens +{result['tokens_installed']} · "
            f"Conflicts {result['token_conflicts']} · Sheet resync {result['sheet_resync_ok']} OK/{result['sheet_resync_failed']} failed"
        )
        _write_json(snap/"result.json", result)
        try: db.add_log(None, "INFO", f"PC Data Sync {sid}: {result['summary']}", None)
        except Exception: pass
        return result
    except Exception as exc:
        result["completed_at"] = _now()
        result["errors"].append(str(exc))
        result["summary"] = f"FAILED: {exc}"
        _write_json(snap/"result.json", result)
        raise
    finally:
        try: lock.unlink(missing_ok=True)
        except Exception: pass
