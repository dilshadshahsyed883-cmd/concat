from __future__ import annotations

import os
import shutil
import socket
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

import httplib2
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp, Request as Httplib2Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .database import Database
from .path_utils import TOKEN_DIR, ensure_app_dirs, repair_path_value
from .token_utils import (
    VALID_TOKEN,
    atomic_write_text,
    classify_google_json,
    clear_runtime_invalid_grant,
    is_client_secret_file,
    is_verified_channel_id,
    mark_runtime_invalid_grant,
    safe_channel_id_token_filename,
    token_file_lock,
    token_status_label,
)
from .secret_store import secure_file_for_current_user

ensure_app_dirs()

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def allow_localhost_http_oauth() -> None:
    """Allow OAuthlib to accept the local loopback HTTP redirect used by desktop apps.

    Google OAuth allows localhost loopback redirects for installed/desktop apps, but
    OAuthlib rejects any non-HTTPS redirect unless this development-safe flag is set.
    This app only uses it for localhost manual OAuth capture.
    """
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)[:120]


def build_channel_url(channel_id: str | None) -> str:
    channel_id = (channel_id or "").strip()
    return f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""


def normalize_proxy_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = config or {}
    host = str(config.get("proxy_host") or "").strip()
    port = str(config.get("proxy_port") or "").strip()
    if not host or not port:
        return {}
    return {
        "proxy_scheme": str(config.get("proxy_scheme") or "http").strip().lower() or "http",
        "proxy_host": host,
        "proxy_port": int(port),
        "proxy_username": str(config.get("proxy_username") or "").strip(),
        "proxy_password": str(config.get("proxy_password") or ""),
    }


def proxy_to_requests_dict(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    proxy = normalize_proxy_config(config)
    if not proxy:
        return None
    scheme = proxy["proxy_scheme"] if proxy["proxy_scheme"] in {"http", "https"} else "http"
    user = proxy.get("proxy_username") or ""
    password = proxy.get("proxy_password") or ""
    auth = f"{user}:{password}@" if user or password else ""
    url = f"{scheme}://{auth}{proxy['proxy_host']}:{proxy['proxy_port']}"
    return {"http": url, "https": url}


def _disable_resumable_upload_308_redirect(http: httplib2.Http) -> httplib2.Http:
    """Let google-api-python-client handle HTTP 308 resumable-upload responses.

    YouTube/Google resumable uploads use HTTP 308 as a progress response.
    Some httplib2 versions treat 308 as a normal redirect and raise
    "Redirected but the response is missing a Location: header." This removes
    308 from httplib2's redirect list for this Http instance only.
    """
    try:
        redirect_codes = set(getattr(http, "redirect_codes", set()))
        if 308 in redirect_codes:
            redirect_codes.remove(308)
            http.redirect_codes = frozenset(redirect_codes)
    except Exception:
        pass
    return http


def build_proxied_http(config: Optional[Dict[str, Any]]) -> httplib2.Http:
    proxy = normalize_proxy_config(config)
    if not proxy:
        return _disable_resumable_upload_308_redirect(httplib2.Http(timeout=120))
    proxy_info = httplib2.ProxyInfo(
        proxy_type=httplib2.socks.PROXY_TYPE_HTTP,
        proxy_host=proxy["proxy_host"],
        proxy_port=int(proxy["proxy_port"]),
        proxy_user=proxy.get("proxy_username") or None,
        proxy_pass=proxy.get("proxy_password") or None,
    )
    return _disable_resumable_upload_308_redirect(httplib2.Http(timeout=120, proxy_info=proxy_info))


class AuthManager:
    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database()

    def add_channel_from_oauth(
        self,
        client_secret_path: str,
        *,
        profile_name: str = "",
        proxy_config: Optional[Dict[str, Any]] = None,
        open_browser: bool = True,
        target_channel_db_id: Optional[int] = None,
        target_email: str | None = None,
        token_folder_path: str | None = None,
    ) -> Dict[str, Any]:
        client_secret = Path(repair_path_value(client_secret_path))
        if not client_secret.exists():
            raise FileNotFoundError(f"Client secret file not found: {client_secret}")
        if not is_client_secret_file(client_secret):
            raise ValueError(f"Selected file is not a valid client_secret.json file: {client_secret}")

        allow_localhost_http_oauth()
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        credentials = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
            open_browser=open_browser,
        )
        return self._save_authorized_channel(
            credentials,
            profile_name=profile_name,
            proxy_config=proxy_config,
            target_channel_db_id=target_channel_db_id,
            target_email=target_email,
            token_folder_path=token_folder_path,
        )

    def add_channel_from_manual_oauth(
        self,
        client_secret_path: str,
        *,
        profile_name: str = "",
        proxy_config: Optional[Dict[str, Any]] = None,
        auth_url_callback: Optional[Callable[[str], None]] = None,
        timeout_seconds: int = 600,
        target_channel_db_id: Optional[int] = None,
        target_email: str | None = None,
        token_folder_path: str | None = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        manual_callback_getter: Optional[Callable[[], str | None]] = None,
    ) -> Dict[str, Any]:
        """Generate an OAuth URL, let the user open it in any browser/profile, then capture localhost redirect.

        When target_channel_db_id is supplied, the selected imported/sheet channel
        row is updated in-place. This avoids duplicate channels and saves the token
        using only the verified YouTube Channel ID in the fixed token directory.
        """
        client_secret = Path(repair_path_value(client_secret_path))
        if not client_secret.exists():
            raise FileNotFoundError(f"Client secret file not found: {client_secret}")
        if not is_client_secret_file(client_secret):
            raise ValueError(f"Selected file is not a valid client_secret.json file: {client_secret}")

        allow_localhost_http_oauth()
        port = self._find_free_port()
        redirect_uri = f"http://localhost:{port}/"
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES, redirect_uri=redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        if auth_url_callback:
            auth_url_callback(auth_url)

        result: Dict[str, Any] = {}

        class OAuthCallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # type: ignore[override]
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                if "error" in params:
                    result["error"] = params.get("error", ["OAuth error"])[0]
                    message = "OAuth failed. You can close this tab and return to the app."
                elif "code" in params:
                    result["authorization_response"] = redirect_uri.rstrip("/") + self.path
                    message = "Channel permission received. You can close this tab and return to the app."
                else:
                    result["error"] = "OAuth response did not include an authorization code."
                    message = "OAuth response was not valid. Return to the app and try again."
                body = f"<html><body><h2>{message}</h2></body></html>".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

        server = HTTPServer(("localhost", port), OAuthCallbackHandler)
        # Keep the local callback server responsive to Cancel OAuth. A single
        # long handle_request() timeout makes the UI wait or tempts us to force
        # terminate the thread, which can freeze Windows. Short polling exits
        # cleanly within about one second.
        server.timeout = 1
        deadline = time.monotonic() + max(5, int(timeout_seconds))
        try:
            while not result and time.monotonic() < deadline:
                if cancel_check and cancel_check():
                    raise RuntimeError("OAuth cancelled by user.")

                # Fallback for browser profiles where the localhost callback page
                # cannot connect. The user may paste the complete redirected URL
                # into the app; it is consumed by this same live OAuth flow, so
                # state and PKCE verification remain intact.
                if manual_callback_getter:
                    pasted_url = (manual_callback_getter() or "").strip()
                    if pasted_url:
                        parsed_pasted = urlparse(pasted_url)
                        pasted_params = parse_qs(parsed_pasted.query)
                        if pasted_params.get("error"):
                            result["error"] = pasted_params.get("error", ["OAuth error"])[0]
                        elif not pasted_params.get("code"):
                            result["error"] = "The pasted callback URL does not contain a Google authorization code."
                        elif parsed_pasted.hostname not in {"localhost", "127.0.0.1", "::1"}:
                            result["error"] = "Paste the complete localhost callback URL from the browser address bar."
                        else:
                            result["authorization_response"] = pasted_url
                        break

                server.handle_request()
        finally:
            server.server_close()

        if cancel_check and cancel_check():
            raise RuntimeError("OAuth cancelled by user.")
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        authorization_response = result.get("authorization_response")
        if not authorization_response:
            raise TimeoutError("OAuth timed out. Copy the URL into the correct browser profile and allow access within the timeout.")

        allow_localhost_http_oauth()
        proxies = proxy_to_requests_dict(proxy_config)
        if proxies:
            flow.fetch_token(authorization_response=authorization_response, proxies=proxies)
        else:
            flow.fetch_token(authorization_response=authorization_response)
        credentials = flow.credentials
        return self._save_authorized_channel(
            credentials,
            profile_name=profile_name,
            proxy_config=proxy_config,
            target_channel_db_id=target_channel_db_id,
            target_email=target_email,
            token_folder_path=token_folder_path,
        )

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("localhost", 0))
            return int(s.getsockname()[1])

    def _save_authorized_channel(
        self,
        credentials: Credentials,
        *,
        profile_name: str = "",
        proxy_config: Optional[Dict[str, Any]] = None,
        target_channel_db_id: Optional[int] = None,
        target_email: str | None = None,
        token_folder_path: str | None = None,
    ) -> Dict[str, Any]:
        youtube = self.build_youtube_service_from_credentials(credentials, proxy_config=proxy_config)
        response = youtube.channels().list(part="snippet", mine=True).execute()
        items = response.get("items", [])
        if not items:
            raise RuntimeError("No YouTube channel found for the authorized Google account.")

        target = self.db.get_channel(int(target_channel_db_id)) if target_channel_db_id else None
        expected_channel_id = str((target or {}).get("channel_id") or "").strip()
        real_expected_id = expected_channel_id if expected_channel_id and not expected_channel_id.startswith("sheet:") else ""

        # Never silently attach a token to the wrong Brand Channel. If Google
        # returns more than one channel, use the exact UC ID from the selected
        # sheet row. Without an exact ID, fail safely instead of choosing item 0.
        if real_expected_id:
            channel = next((item for item in items if str(item.get("id") or "") == real_expected_id), None)
            if channel is None:
                returned = ", ".join(
                    f"{item.get('snippet', {}).get('title', item.get('id', 'Unknown'))} ({item.get('id', 'no-id')})"
                    for item in items[:10]
                )
                raise RuntimeError(
                    "Wrong YouTube channel authorized. "
                    f"Selected row expects {real_expected_id}, but Google returned: {returned}. "
                    "Open the OAuth URL in the correct Brand Channel identity and try again."
                )
        elif len(items) > 1:
            returned = ", ".join(
                f"{item.get('snippet', {}).get('title', item.get('id', 'Unknown'))} ({item.get('id', 'no-id')})"
                for item in items[:10]
            )
            raise RuntimeError(
                "Google returned multiple YouTube channels for this account, so the app refused to guess. "
                f"Add the correct UC Channel ID or /channel/UC... link to the selected sheet row, then reconnect. Returned: {returned}"
            )
        else:
            channel = items[0]

        channel_id = str(channel["id"])
        channel_name = channel["snippet"].get("title", channel_id)
        channel_url = build_channel_url(channel_id)

        gmail = (target_email or "").strip().lower()
        if not is_verified_channel_id(channel_id):
            raise RuntimeError(f"Google returned an invalid YouTube Channel ID: {channel_id}")

        # Token storage is intentionally fixed and channel-centric. The caller
        # cannot redirect tokens to arbitrary folders, and Gmail is never used
        # as the token identity because one Gmail may own many Brand Channels.
        token_folder = TOKEN_DIR
        token_folder.mkdir(parents=True, exist_ok=True)
        token_path = token_folder / safe_channel_id_token_filename(channel_id)

        # Preserve any unrelated/corrupt JSON that occupies the canonical name.
        # It is quarantined rather than overwritten; a valid existing token is
        # refreshed atomically in place.
        if not credentials.refresh_token:
            raise RuntimeError(
                "Google did not return an offline refresh token. Revoke the old "
                "grant for this OAuth client, confirm the OAuth app is In production, "
                "then authorize the channel again."
            )

        with token_file_lock(token_path):
            if token_path.exists() and classify_google_json(token_path) != VALID_TOKEN:
                quarantine = token_folder / "quarantine"
                quarantine.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                moved = quarantine / f"{token_path.stem}_{stamp}{token_path.suffix}"
                counter = 1
                while moved.exists():
                    moved = quarantine / f"{token_path.stem}_{stamp}_{counter}{token_path.suffix}"
                    counter += 1
                shutil.move(str(token_path), str(moved))

            atomic_write_text(token_path, credentials.to_json())
            secure_file_for_current_user(token_path)
            clear_runtime_invalid_grant(token_path)

        proxy = normalize_proxy_config(proxy_config)
        if target_channel_db_id:
            channel_db_id = self.db.update_channel_after_oauth(
                int(target_channel_db_id),
                channel_id=channel_id,
                channel_name=channel_name,
                channel_url=channel_url,
                token_file_path=str(token_path),
                google_account_email=gmail or None,
                browser_profile_name=profile_name,
                proxy_scheme=proxy.get("proxy_scheme", ""),
                proxy_host=proxy.get("proxy_host", ""),
                proxy_port=str(proxy.get("proxy_port", "")) if proxy else "",
                proxy_username=proxy.get("proxy_username", ""),
                proxy_password=proxy.get("proxy_password", ""),
            )
        else:
            channel_db_id = self.db.add_or_update_channel(
                channel_id=channel_id,
                channel_name=channel_name,
                google_account_email=gmail or None,
                token_file_path=str(token_path),
                status="ACTIVE",
                channel_url=channel_url,
                browser_profile_name=profile_name,
                proxy_scheme=proxy.get("proxy_scheme", ""),
                proxy_host=proxy.get("proxy_host", ""),
                proxy_port=str(proxy.get("proxy_port", "")) if proxy else "",
                proxy_username=proxy.get("proxy_username", ""),
                proxy_password=proxy.get("proxy_password", ""),
            )

        return {
            "id": channel_db_id,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "channel_url": channel_url,
            "google_account_email": gmail or None,
            "token_file_path": str(token_path),
            "target_channel_db_id": target_channel_db_id,
        }

    def load_credentials(self, token_file_path: str, proxy_config: Optional[Dict[str, Any]] = None) -> Credentials:
        token_path = Path(repair_path_value(token_file_path))
        with token_file_lock(token_path):
            if not token_path.exists():
                raise FileNotFoundError(f"Token file not found: {token_path}")

            label = token_status_label(token_path)
            if label != "OK":
                raise RuntimeError(f"Saved channel token is {label}. Reconnect this channel with OAuth.")

            # Reload inside the lock. Another worker or app instance may have
            # refreshed the same channel immediately before this one acquired it.
            credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if not credentials.refresh_token:
                raise RuntimeError(
                    "Saved channel token has no offline refresh token. Reconnect this channel with OAuth."
                )

            # Access-token expiry is normal. Refresh only when google-auth says it
            # is needed; a valid token must not trigger a needless refresh request
            # every time a service or connection test is created.
            if not credentials.valid:
                old_refresh_token = credentials.refresh_token
                refresh_http = build_proxied_http(proxy_config)
                try:
                    credentials.refresh(Httplib2Request(refresh_http))
                except Exception as exc:
                    error_lower = str(exc).lower()
                    if "invalid_grant" in error_lower or "invalid_scope" in error_lower:
                        mark_runtime_invalid_grant(token_path)
                        self._mark_channels_invalid_for_token_path(token_path)
                        if "invalid_scope" in error_lower:
                            raise RuntimeError(
                                "OAuth token scope is invalid for YouTube uploads. Reconnect this channel with OAuth."
                            ) from exc
                        raise RuntimeError(
                            "OAuth token has expired or been revoked. Reconnect this channel with OAuth."
                        ) from exc
                    raise
                if not credentials.refresh_token:
                    credentials.refresh_token = old_refresh_token
                atomic_write_text(token_path, credentials.to_json())
                secure_file_for_current_user(token_path)
                clear_runtime_invalid_grant(token_path)

            if not credentials.valid:
                raise RuntimeError("Saved channel token is invalid. Reconnect this channel.")
            return credentials

    def _mark_channels_invalid_for_token_path(self, token_path: Path) -> None:
        """Persist INVALID_TOKEN for every channel row that uses this exact token file."""
        try:
            target = token_path.resolve(strict=False)
        except Exception:
            target = token_path
        try:
            channels = self.db.list_channels(active_only=False)
        except Exception:
            return
        for channel in channels:
            raw_path = str(channel.get("token_file_path") or "").strip()
            if not raw_path:
                continue
            try:
                candidate = Path(repair_path_value(raw_path)).resolve(strict=False)
            except Exception:
                candidate = Path(repair_path_value(raw_path))
            if candidate == target:
                try:
                    self.db.set_channel_status(int(channel["id"]), "INVALID_TOKEN")
                except Exception:
                    pass

    def build_youtube_service(self, token_file_path: str, proxy_config: Optional[Dict[str, Any]] = None):
        credentials = self.load_credentials(token_file_path, proxy_config=proxy_config)
        return self.build_youtube_service_from_credentials(credentials, proxy_config=proxy_config)

    @staticmethod
    def build_youtube_service_from_credentials(credentials: Credentials, proxy_config: Optional[Dict[str, Any]] = None):
        http = build_proxied_http(proxy_config)
        authed_http = AuthorizedHttp(credentials, http=http)
        return build("youtube", "v3", http=authed_http, cache_discovery=False)
