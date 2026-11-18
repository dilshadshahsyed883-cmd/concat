from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

from .path_utils import TOKEN_DIR, repair_path_value
from .secret_store import secure_file_for_current_user

VALID_TOKEN = "VALID_TOKEN"
MISSING_TOKEN = "MISSING_TOKEN"
INVALID_TOKEN = "INVALID_TOKEN"
CLIENT_SECRET = ''
BAD_JSON = "BAD_JSON"

RESERVED_JSON_NAMES = {
    "app_settings.json",
    "portable_state.json",
    "ui_session.json",
    "client_secret.json",
    "credentials.json",
    "token_backup_settings.json",
}

_TOKEN_LOCKS_GUARD = threading.Lock()
_TOKEN_LOCKS: dict[str, threading.RLock] = {}


def _safe_token_path(path_value: str | Path) -> Path | None:
    """Return a usable token *file* path, or None for blank/directory-like input.

    ``Path("")`` becomes ``Path(".")`` on Windows/Python. Calling
    ``with_name`` on that value raises ``WindowsPath('.') has an empty name``.
    Token paths come from sheets/database rows and may legitimately be blank, so
    all read/status helpers must treat such input as a missing token instead of
    crashing the whole sync.
    """
    raw = repair_path_value(str(path_value or "")).strip()
    if not raw or raw in {".", "./", ".\\"}:
        return None
    path = Path(raw)
    if not path.name:
        return None
    try:
        if path.exists() and path.is_dir():
            return None
    except OSError:
        return None
    return path


def _resolved_lock_key(path_value: str | Path) -> str:
    path = _safe_token_path(path_value)
    if path is None:
        return "<missing-token-path>"
    try:
        return str(path.resolve(strict=False)).lower()
    except Exception:
        return str(path.absolute()).lower()


@contextmanager
def token_file_lock(path_value: str | Path) -> Iterator[None]:
    """Serialize token reads/refreshes/writes across threads and app instances."""
    path = _safe_token_path(path_value)
    if path is None:
        raise ValueError("Token file path is empty or points to a directory.")
    key = _resolved_lock_key(path)
    with _TOKEN_LOCKS_GUARD:
        thread_lock = _TOKEN_LOCKS.setdefault(key, threading.RLock())

    with thread_lock:
        lock_path = path.with_name(path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
        acquired = False
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            deadline = time.monotonic() + 180
            while not acquired:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for token lock: {path.name}"
                        )
                    time.sleep(0.1)
            try:
                yield
            finally:
                lock_file.seek(0)
                if acquired and os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                elif acquired:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def atomic_write_text(path_value: str | Path, content: str) -> Path:
    """Replace a text file atomically without exposing a partially written token."""
    path = _safe_token_path(path_value)
    if path is None:
        raise ValueError("Token file path is empty or points to a directory.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass
    return path


def _invalid_grant_marker_path(path_value: str | Path) -> Path | None:
    path = _safe_token_path(path_value)
    if path is None:
        return None
    return path.with_name(path.name + ".invalid_grant")


def _token_file_fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def has_runtime_invalid_grant(path_value: str | Path) -> bool:
    """Return True only when the current token file was rejected with invalid_grant.

    The marker stores a SHA-256 fingerprint of the rejected token file. Replacing
    the token file manually therefore does not leave a stale revoked status behind.
    """
    path = _safe_token_path(path_value)
    if path is None or not path.exists():
        return False
    marker = _invalid_grant_marker_path(path)
    if marker is None or not marker.exists():
        return False
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except Exception:
        return False
    current = _token_file_fingerprint(path)
    return bool(recorded and current and recorded == current)


def mark_runtime_invalid_grant(path_value: str | Path) -> None:
    """Persist that the current token file was rejected as expired/revoked."""
    path = _safe_token_path(path_value)
    if path is None:
        return
    fingerprint = _token_file_fingerprint(path)
    if not fingerprint:
        return
    marker = _invalid_grant_marker_path(path)
    try:
        marker.write_text(fingerprint, encoding="utf-8")
    except Exception:
        # The upload error itself must still surface even if the marker cannot be written.
        pass


def clear_runtime_invalid_grant(path_value: str | Path) -> None:
    """Clear the runtime expired/revoked marker after successful OAuth/token save."""
    marker = _invalid_grant_marker_path(path_value)
    if marker is None:
        return
    try:
        marker.unlink(missing_ok=True)
    except Exception:
        pass


def normalize_gmail(value: str) -> str:
    return (value or "").strip().lower()


def safe_email_token_filename(email: str) -> str:
    email = normalize_gmail(email)
    if not email:
        return ""
    return "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in email)[:180] + ".json"


def is_verified_channel_id(channel_id: str) -> bool:
    value = str(channel_id or "").strip()
    return value.startswith("UC") and len(value) >= 10 and not value.lower().startswith("sheet:")


def safe_channel_id_token_filename(channel_id: str) -> str:
    """Canonical token filename: the verified YouTube channel ID only."""
    value = str(channel_id or "").strip()
    if not value:
        return ""
    clean = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)[:160]
    return f"{clean}.json" if clean else ""


def _legacy_channel_token_filename(email: str, channel_id: str) -> str:
    email_part = safe_email_token_filename(email).removesuffix(".json") if normalize_gmail(email) else "account"
    channel_part = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(channel_id or "").strip())[:120]
    return f"{email_part}__{channel_part}.json" if channel_part else f"{email_part}.json"


def _legacy_authorized_token_filename(email: str, channel_id: str) -> str:
    base = _legacy_channel_token_filename(email, channel_id).removesuffix(".json")
    return f"{base}__authorized_token.json"


def safe_channel_token_filename(email: str, channel_id: str) -> str:
    """Backward-compatible API returning the new Channel-ID-only filename.

    ``email`` is deliberately ignored when a verified channel ID is available.
    This prevents token collisions and keeps Brand Channel identity stable even
    when Gmail ownership or channel names change.
    """
    canonical = safe_channel_id_token_filename(channel_id)
    return canonical or safe_email_token_filename(email)


def safe_authorized_token_filename(email: str, channel_id: str) -> str:
    """Collision-safe alternative for a Channel-ID token path."""
    base = safe_channel_token_filename(email, channel_id).removesuffix(".json")
    return f"{base}__authorized_token.json" if base else "authorized_token.json"


def is_reserved_json_filename(path_value: str | Path) -> bool:
    path = _safe_token_path(path_value)
    return bool(path and path.name.strip().lower() in RESERVED_JSON_NAMES)


def exact_gmail_json_path(json_folder: str, gmail: str, channel_id: str = "") -> Path:
    folder = Path((json_folder or "").strip()).expanduser()
    return folder / (safe_channel_token_filename(gmail, channel_id) if channel_id else safe_email_token_filename(gmail))


def load_json_file(path_value: str | Path) -> Dict[str, Any] | None:
    try:
        path = _safe_token_path(path_value)
        if path is None:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def classify_google_json(path_value: str | Path) -> str:
    path = _safe_token_path(path_value)
    if path is None or not path.exists() or not path.is_file():
        return MISSING_TOKEN
    if has_runtime_invalid_grant(path):
        return INVALID_TOKEN
    data = load_json_file(path)
    if not data:
        return BAD_JSON

    if isinstance(data.get("installed"), dict) or isinstance(data.get("web"), dict):
        return CLIENT_SECRET

    has_core = bool(data.get("client_id") and data.get("client_secret") and data.get("token_uri"))
    # An access token alone expires quickly and cannot support unattended use.
    # Only an offline refresh token is a reusable authorized-channel token.
    has_token_material = bool(data.get("refresh_token"))
    if has_core and has_token_material:
        return VALID_TOKEN

    return INVALID_TOKEN


def is_valid_authorized_token(path_value: str | Path) -> bool:
    return classify_google_json(path_value) == VALID_TOKEN


def is_client_secret_file(path_value: str | Path) -> bool:
    path = _safe_token_path(path_value)
    if path is None or not path.exists() or not path.is_file():
        return False
    data = load_json_file(path)
    return bool(data and (isinstance(data.get("installed"), dict) or isinstance(data.get("web"), dict)))


def token_status_label(path_value: str | Path) -> str:
    if has_runtime_invalid_grant(path_value):
        return "EXPIRED / REVOKED / SCOPE ERROR"
    status = classify_google_json(path_value)
    if status == VALID_TOKEN:
        return "OK"
    if status == CLIENT_SECRET:
        return "INVALID: client secret in token folder"
    if status == BAD_JSON:
        return "INVALID: bad JSON"
    if status == INVALID_TOKEN:
        return "INVALID"
    return "MISSING"


def token_status_short(path_value: str | Path) -> str:
    status = classify_google_json(path_value)
    if status == VALID_TOKEN:
        return "OK"
    if status in {CLIENT_SECRET, BAD_JSON, INVALID_TOKEN}:
        return "INVALID"
    return "MISSING"


def token_candidates_for_gmail(json_folder: str, gmail: str, channel_id: str = "", *, allow_legacy: bool = True) -> list[Path]:
    """Return canonical Channel-ID token first, then safe legacy fallbacks."""
    folder = Path((json_folder or str(TOKEN_DIR)).strip()).expanduser()
    email = normalize_gmail(gmail)
    names: list[str] = []

    if is_verified_channel_id(channel_id):
        names.append(safe_channel_id_token_filename(channel_id))
        names.append(safe_authorized_token_filename(email, channel_id))
        # Compatibility with releases that used Gmail__ChannelID.json.
        if email:
            names.append(_legacy_channel_token_filename(email, channel_id))
            names.append(_legacy_authorized_token_filename(email, channel_id))

    if allow_legacy and email:
        names.extend([safe_email_token_filename(email), f"{email}.json"])

    out: list[Path] = []
    seen: set[str] = set()
    for name in names:
        if not name or name.strip().lower() in RESERVED_JSON_NAMES:
            continue
        path = folder / name
        key = str(path).lower()
        if key not in seen:
            out.append(path)
            seen.add(key)
    return out


def find_token_for_gmail_status(
    json_folder: str, gmail: str, channel_id: str = "", *, allow_legacy: bool = True
) -> Tuple[str, str]:
    candidates = token_candidates_for_gmail(json_folder, gmail, channel_id, allow_legacy=allow_legacy)
    if not candidates:
        # A synthetic sheet identity cannot safely predict a token filename.
        if is_verified_channel_id(channel_id):
            return str(Path(json_folder or TOKEN_DIR) / safe_channel_id_token_filename(channel_id)), MISSING_TOKEN
        return "", MISSING_TOKEN

    first_existing_invalid = ""
    first_existing_status = ""
    for candidate in candidates:
        if candidate.exists():
            status = classify_google_json(candidate)
            if status == VALID_TOKEN:
                return str(candidate), VALID_TOKEN
            if not first_existing_invalid:
                first_existing_invalid = str(candidate)
                first_existing_status = status
    if first_existing_invalid:
        return first_existing_invalid, first_existing_status or INVALID_TOKEN
    return str(candidates[0]), MISSING_TOKEN


def find_token_for_gmail(
    json_folder: str, gmail: str, channel_id: str = "", *, allow_legacy: bool = True
) -> Tuple[str, bool]:
    path, status = find_token_for_gmail_status(json_folder, gmail, channel_id, allow_legacy=allow_legacy)
    return path, status == VALID_TOKEN


def promote_token_to_channel_id(path_value: str | Path, channel_id: str, *, token_dir: Path = TOKEN_DIR) -> Path:
    """Copy a valid legacy token to the canonical ``ChannelID.json`` path.

    The source is left intact for rollback safety. The application/database can
    immediately switch to the canonical path without risking data loss.
    """
    source = _safe_token_path(path_value)
    if source is None:
        return Path()
    if classify_google_json(source) != VALID_TOKEN or not is_verified_channel_id(channel_id):
        return source
    token_dir.mkdir(parents=True, exist_ok=True)
    destination = token_dir / safe_channel_id_token_filename(channel_id)
    try:
        if source.resolve() == destination.resolve():
            return destination
    except Exception:
        pass
    with token_file_lock(destination):
        if destination.exists() and classify_google_json(destination) == VALID_TOKEN:
            return destination
        if destination.exists():
            quarantine = token_dir / "quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            moved = quarantine / f"{destination.stem}_{stamp}{destination.suffix}"
            counter = 1
            while moved.exists():
                moved = quarantine / f"{destination.stem}_{stamp}_{counter}{destination.suffix}"
                counter += 1
            shutil.move(str(destination), str(moved))
        atomic_write_text(destination, source.read_text(encoding="utf-8"))
        secure_file_for_current_user(destination)
    return destination


def expected_client_secret_candidates(client_secret_path: str = "", json_folder: str = "") -> list[Path]:
    candidates: list[Path] = []
    for raw in [client_secret_path, str(Path((json_folder or "").strip()).expanduser() / "client_secret.json") if json_folder else ""]:
        raw = (raw or "").strip()
        if raw:
            path = Path(repair_path_value(raw))
            if str(path) not in [str(x) for x in candidates]:
                candidates.append(path)
    return candidates
