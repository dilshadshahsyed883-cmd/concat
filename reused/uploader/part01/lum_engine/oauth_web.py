from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow

from .config import OAUTH_CLIENT_ROOT, OAUTH_SESSION_ROOT, ensure_dirs
from desktop_core.modules.auth_manager import AuthManager, SCOPES, allow_localhost_http_oauth, proxy_to_requests_dict
from desktop_core.modules.token_utils import is_client_secret_file
from desktop_core.modules.oauth_registry import get_shared_oauth_client, shared_oauth_client_path
from desktop_core.modules.secret_store import secure_file_for_current_user

REDIRECT_URI = "http://localhost:8765/"


def safe_label(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.@-]+", "_", str(value or "").strip())[:100].strip("._")
    return text or "oauth_client"


def save_client_json(filename: str, payload: bytes) -> dict:
    ensure_dirs()
    if len(payload) > 1024 * 1024:
        raise ValueError("OAuth JSON file is unexpectedly large")
    label = safe_label(Path(filename or "client_secret.json").stem)
    target = OAUTH_CLIENT_ROOT / f"{label}.json"
    counter = 2
    while target.exists() and target.read_bytes() != payload:
        target = OAUTH_CLIENT_ROOT / f"{label}_{counter}.json"
        counter += 1
    target.write_bytes(payload)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    if not is_client_secret_file(target):
        target.unlink(missing_ok=True)
        raise ValueError("Uploaded file is not a valid Google OAuth client JSON")
    return {"label": target.stem, "filename": target.name}


def save_shared_client_json(payload: bytes) -> dict:
    """Validate and atomically replace the single shared Desktop OAuth client.

    Existing channel token files are intentionally not read, rewritten, renamed,
    or deleted here.
    """
    ensure_dirs()
    if len(payload) > 1024 * 1024:
        raise ValueError("OAuth JSON file is unexpectedly large")
    target = shared_oauth_client_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(payload)
    try:
        if not is_client_secret_file(temporary):
            raise ValueError("Uploaded file is not a valid Google OAuth client JSON")
        os.replace(temporary, target)
        secure_file_for_current_user(target)
    finally:
        temporary.unlink(missing_ok=True)
    if not is_client_secret_file(target):
        raise RuntimeError("Shared OAuth client could not be installed correctly")
    return {"label": "shared_oauth_client", "filename": target.name}



def _owner_client_path(username: str) -> Path:
    """One private Desktop OAuth client per authenticated Studio account."""
    owner = str(username or "").strip().casefold()
    if not re.fullmatch(r"[a-z0-9_.@+-]{1,160}", owner):
        raise ValueError("Verified account name is required")
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]
    return OAUTH_CLIENT_ROOT / "owners" / digest / "client_secret.json"


def save_owner_client_json(username: str, payload: bytes) -> dict:
    """Replace only this owner's OAuth JSON, never the shared client or tokens."""
    ensure_dirs()
    if not payload or len(payload) > 1024 * 1024:
        raise ValueError("Invalid OAuth client JSON size")
    target = _owner_client_path(username)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    temporary = target.with_name("client_" + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_bytes(payload)
        temporary.chmod(0o600)
        if not is_client_secret_file(temporary):
            raise ValueError("Uploaded file is not a valid Google OAuth client JSON")
        os.replace(temporary, target)
        secure_file_for_current_user(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"label": "my_oauth_client", "filename": "My OAuth client"}


def list_owner_clients(username: str) -> list[dict]:
    target = _owner_client_path(username)
    if target.is_file() and is_client_secret_file(target):
        return [{"label": "my_oauth_client", "filename": "My OAuth client"}]
    return ([{"label": "shared_oauth_client", "filename": "Available shared client"}]
            if get_shared_oauth_client() else [])


def owner_client_for_oauth(username: str) -> Path:
    target = _owner_client_path(username)
    if target.is_file():
        if not is_client_secret_file(target):
            raise ValueError("Your OAuth client is invalid. Replace it before reconnecting.")
        return target
    return _client_path("shared_oauth_client")


def list_clients() -> list[dict]:
    ensure_dirs()
    path = get_shared_oauth_client()
    if not path:
        return []
    return [{"label": "shared_oauth_client", "filename": path.name}]


def _client_path(label: str = "") -> Path:
    path = get_shared_oauth_client()
    if not path:
        raise FileNotFoundError("Shared OAuth client is not configured. Set the Shared OAuth Client first.")
    return path


def start_oauth(label: str = "", *, proxy_config: dict | None = None, profile_name: str = "", target_channel_db_id: int | None = None, target_email: str = "", client_path: Path | None = None, owner_username: str = "") -> dict:
    client = client_path if client_path is not None else _client_path(label)
    allow_localhost_http_oauth()
    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    session_id = secrets.token_urlsafe(24)
    session_path = OAUTH_SESSION_ROOT / f"{session_id}.json"
    session_path.write_text(json.dumps({
        "client": str(client), "state": state, "redirect_uri": REDIRECT_URI,
        "proxy": proxy_config or {}, "profile_name": profile_name,
        "target_channel_db_id": target_channel_db_id, "target_email": target_email,
        "owner_username": str(owner_username or "").casefold(),
    }, ensure_ascii=False), encoding="utf-8")
    session_path.chmod(0o600)
    return {"session_id": session_id, "authorization_url": auth_url, "redirect_uri": REDIRECT_URI}


def complete_oauth(db, session_id: str, callback_url: str, *, owner_username: str = "") -> dict:
    session_path = OAUTH_SESSION_ROOT / f"{safe_label(session_id)}.json"
    if not session_path.is_file():
        raise ValueError("OAuth session was not found or already completed")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    expected_owner = str(session.get("owner_username") or "").casefold()
    if expected_owner and expected_owner != str(owner_username or "").strip().casefold():
        raise PermissionError("OAuth session belongs to another account")
    parsed = urlparse(str(callback_url or "").strip())
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Paste the complete localhost callback URL from the browser address bar")
    params = parse_qs(parsed.query)
    if params.get("error"):
        raise RuntimeError(f"Google OAuth error: {params['error'][0]}")
    if not params.get("code"):
        raise ValueError("Callback URL does not contain an authorization code")
    if params.get("state", [""])[0] != session.get("state"):
        raise ValueError("OAuth state mismatch; restart authentication")
    allow_localhost_http_oauth()
    flow = InstalledAppFlow.from_client_secrets_file(
        session["client"], SCOPES, state=session["state"], redirect_uri=session["redirect_uri"]
    )
    proxies = proxy_to_requests_dict(session.get("proxy"))
    if proxies:
        flow.fetch_token(authorization_response=callback_url, proxies=proxies)
    else:
        flow.fetch_token(authorization_response=callback_url)
    result = AuthManager(db)._save_authorized_channel(
        flow.credentials,
        profile_name=str(session.get("profile_name") or ""),
        proxy_config=session.get("proxy") or {},
        target_channel_db_id=session.get("target_channel_db_id") or None,
        target_email=str(session.get("target_email") or "") or None,
    )
    session_path.unlink(missing_ok=True)
    return result
