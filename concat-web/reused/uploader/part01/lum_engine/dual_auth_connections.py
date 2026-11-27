from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict


BUILD_MARKER = "LUM_DUAL_AUTH_CONNECTIONS_V2"
GOOGLE_CALLBACK_URI = "https://app.scenebuilder.online/LUM/oauth/google/callback"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = os.environ.get("FRESH_LUM_TOKEN_URI", "https://oauth2.googleapis.com/token")
CERT_URI = "https://www.googleapis.com/oauth2/v1/certs"
STATE_TTL_MINUTES = 10
METHOD_LEGACY = "legacy"
METHOD_WEB = "web_oauth"
METHODS = frozenset({METHOD_LEGACY, METHOD_WEB})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).astimezone(timezone.utc).isoformat()


def _secure_root() -> Path:
    return Path(os.environ.get("LUM_GOOGLE_OAUTH_ROOT", "/var/lib/local-upload-manager/secure")).expanduser()


def _safe_user_value(user: Any, name: str) -> str:
    if user is None:
        return ""
    value = getattr(user, name, None)
    if value is None and isinstance(user, dict):
        value = user.get(name)
    return str(value or "")


def _fallback_token_status(path_value: str) -> str:
    path = Path(str(path_value or "").strip())
    if not str(path_value or "").strip() or not path.is_file():
        return "TOKEN_MISSING"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "INVALID_TOKEN"
    if not isinstance(data, dict) or not str(data.get("refresh_token") or "").strip():
        return "INVALID_TOKEN"
    return "OK"


def _token_status(path_value: str) -> str:
    try:
        from desktop_core.modules.token_utils import token_status_label

        return str(token_status_label(Path(str(path_value or "").strip())) or "INVALID_TOKEN").upper()
    except Exception:
        return _fallback_token_status(path_value)


class DualAuthConnections:
    """Side-by-side legacy and Web OAuth credentials for each LUM channel.

    The existing channels.token_file_path remains the compatibility projection
    consumed by the unchanged upload engine. The durable connection records are
    the authentication source of truth. New video rows snapshot the active
    credential so a later selection change cannot redirect an already queued job.
    """

    def __init__(self, db: Any):
        self.db = db
        self.root = _secure_root()
        self.config_path = self.root / "google_oauth_web_client.json"
        self.state_db_path = self.root / "google_oauth_web_state.sqlite3"
        self.web_token_dir = self.root / "tokens" / "web_oauth"
        self._ensure_secure_store()
        self._ensure_main_schema()
        self.ensure_channel_connections()

    def _ensure_secure_store(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.web_token_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.root, self.web_token_dir.parent, self.web_token_dir):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        with sqlite3.connect(self.state_db_path, timeout=20) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_oauth (
                    state TEXT PRIMARY KEY,
                    channel_db_id INTEGER NOT NULL,
                    requested_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_pending_oauth_expires ON pending_oauth(expires_at)")
        try:
            os.chmod(self.state_db_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}

    def _ensure_main_schema(self) -> None:
        with self.db.connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_auth_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_db_id INTEGER NOT NULL,
                    method TEXT NOT NULL CHECK(method IN ('legacy','web_oauth')),
                    credential_path TEXT NOT NULL DEFAULT '',
                    verified_channel_id TEXT NOT NULL DEFAULT '',
                    verified_channel_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'TOKEN_MISSING',
                    last_tested_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(channel_db_id, method),
                    FOREIGN KEY(channel_db_id) REFERENCES channels(id) ON DELETE CASCADE
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_auth_preferences (
                    channel_db_id INTEGER PRIMARY KEY,
                    active_connection_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(channel_db_id) REFERENCES channels(id) ON DELETE CASCADE,
                    FOREIGN KEY(active_connection_id) REFERENCES channel_auth_connections(id)
                )
                """
            )
            video_columns = self._columns(con, "videos")
            for name, definition in {
                "auth_connection_id": "INTEGER",
                "auth_method": "TEXT",
                "auth_token_file_path": "TEXT",
            }.items():
                if name not in video_columns:
                    con.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")
            con.execute(
                """
                CREATE TRIGGER IF NOT EXISTS lum_dual_auth_snapshot_after_video_insert
                AFTER INSERT ON videos
                WHEN NEW.auth_connection_id IS NULL
                BEGIN
                    UPDATE videos
                    SET auth_connection_id=(
                            SELECT ac.id FROM channel_auth_preferences ap
                            JOIN channel_auth_connections ac ON ac.id=ap.active_connection_id
                            WHERE ap.channel_db_id=NEW.channel_db_id AND ac.channel_db_id=NEW.channel_db_id
                        ),
                        auth_method=(
                            SELECT ac.method FROM channel_auth_preferences ap
                            JOIN channel_auth_connections ac ON ac.id=ap.active_connection_id
                            WHERE ap.channel_db_id=NEW.channel_db_id AND ac.channel_db_id=NEW.channel_db_id
                        ),
                        auth_token_file_path=(
                            SELECT ac.credential_path FROM channel_auth_preferences ap
                            JOIN channel_auth_connections ac ON ac.id=ap.active_connection_id
                            WHERE ap.channel_db_id=NEW.channel_db_id AND ac.channel_db_id=NEW.channel_db_id
                        )
                    WHERE id=NEW.id;
                END
                """
            )
            con.execute(
                "INSERT INTO app_meta(key,value) VALUES('lum_dual_auth_schema','2') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

    @staticmethod
    def _validate_client_id(value: str) -> str:
        client_id = str(value or "").strip()
        if not client_id.endswith(".apps.googleusercontent.com"):
            raise ValueError("Google Web OAuth Client ID must end with .apps.googleusercontent.com")
        return client_id

    @staticmethod
    def _validate_client_secret(value: str) -> str:
        secret = str(value or "").strip()
        if len(secret) < 8:
            raise ValueError("Google Web OAuth Client Secret is required.")
        return secret

    def save_client(self, client_id: str, client_secret: str) -> Dict[str, Any]:
        client_id = self._validate_client_id(client_id)
        client_secret = self._validate_client_secret(client_secret)
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": GOOGLE_CALLBACK_URI,
            "updated_at": _iso(),
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".google_oauth_web_client.", suffix=".json", dir=str(self.root))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.config_path)
            os.chmod(self.config_path, 0o600)
        finally:
            tmp.unlink(missing_ok=True)
        return self.status()

    def load_client(self) -> Dict[str, str]:
        if not self.config_path.is_file():
            raise RuntimeError("Google Web OAuth app is not configured in LUM yet.")
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("Saved Google Web OAuth client configuration is unreadable.") from exc
        return {
            "client_id": self._validate_client_id(str(data.get("client_id") or "")),
            "client_secret": self._validate_client_secret(str(data.get("client_secret") or "")),
        }

    def status(self) -> Dict[str, Any]:
        configured = False
        hint = ""
        updated_at = ""
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.is_file() else {}
            client_id = self._validate_client_id(str(data.get("client_id") or ""))
            self._validate_client_secret(str(data.get("client_secret") or ""))
            configured = True
            hint = client_id[:12] + "…" + client_id[-28:]
            updated_at = str(data.get("updated_at") or "")
        except Exception:
            pass
        return {
            "ok": True,
            "configured": configured,
            "client_id_hint": hint,
            "redirect_uri": GOOGLE_CALLBACK_URI,
            "updated_at": updated_at,
            "secret_exposed": False,
            "build": BUILD_MARKER,
        }

    def _client_config(self) -> Dict[str, Any]:
        client = self.load_client()
        return {
            "web": {
                "client_id": client["client_id"],
                "project_id": "lum-production",
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "auth_provider_x509_cert_url": CERT_URI,
                "client_secret": client["client_secret"],
                "redirect_uris": [GOOGLE_CALLBACK_URI],
            }
        }

    def _channel(self, channel_db_id: int) -> Dict[str, Any]:
        channel = self.db.get_channel(int(channel_db_id))
        if not channel:
            raise ValueError("Selected LUM channel no longer exists.")
        return dict(channel)

    @staticmethod
    def _channel_proxy(channel: Dict[str, Any]) -> Dict[str, Any]:
        host = str(channel.get("proxy_host") or "").strip()
        port = str(channel.get("proxy_port") or "").strip()
        if bool(host) != bool(port):
            raise RuntimeError("Channel proxy is incomplete. LUM refused a direct-network fallback.")
        return {
            "proxy_scheme": str(channel.get("proxy_scheme") or "http"),
            "proxy_host": host,
            "proxy_port": port,
            "proxy_username": str(channel.get("proxy_username") or ""),
            "proxy_password": str(channel.get("proxy_password") or ""),
        }

    def _active_method(self, con: sqlite3.Connection, channel_db_id: int) -> str:
        row = con.execute(
            """
            SELECT ac.method FROM channel_auth_preferences ap
            JOIN channel_auth_connections ac ON ac.id=ap.active_connection_id
            WHERE ap.channel_db_id=? AND ac.channel_db_id=?
            """,
            (int(channel_db_id), int(channel_db_id)),
        ).fetchone()
        return str(row[0]) if row else ""

    def ensure_channel_connections(self) -> None:
        channels = [dict(row) for row in self.db.list_channels()]
        with self.db.connect() as con:
            for channel in channels:
                channel_id = int(channel["id"])
                active_method = self._active_method(con, channel_id)
                legacy_path = str(channel.get("token_file_path") or "").strip()
                existing = con.execute(
                    "SELECT id,credential_path FROM channel_auth_connections WHERE channel_db_id=? AND method=?",
                    (channel_id, METHOD_LEGACY),
                ).fetchone()
                if existing is None:
                    con.execute(
                        """
                        INSERT INTO channel_auth_connections(
                            channel_db_id,method,credential_path,verified_channel_id,verified_channel_name,status
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            channel_id,
                            METHOD_LEGACY,
                            legacy_path,
                            str(channel.get("channel_id") or ""),
                            str(channel.get("channel_name") or ""),
                            _token_status(legacy_path),
                        ),
                    )
                elif active_method in {"", METHOD_LEGACY} and legacy_path != str(existing["credential_path"] or ""):
                    con.execute(
                        """
                        UPDATE channel_auth_connections SET credential_path=?,status=?,updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (legacy_path, _token_status(legacy_path), int(existing["id"])),
                    )
                preference = con.execute(
                    "SELECT active_connection_id FROM channel_auth_preferences WHERE channel_db_id=?",
                    (channel_id,),
                ).fetchone()
                if preference is None:
                    legacy = con.execute(
                        "SELECT id FROM channel_auth_connections WHERE channel_db_id=? AND method=?",
                        (channel_id, METHOD_LEGACY),
                    ).fetchone()
                    con.execute(
                        "INSERT INTO channel_auth_preferences(channel_db_id,active_connection_id) VALUES(?,?)",
                        (channel_id, int(legacy["id"])),
                    )

    def inventory(self) -> Dict[str, Any]:
        self.ensure_channel_connections()
        with self.db.connect() as con:
            rows = con.execute(
                """
                SELECT ac.*, CASE WHEN ap.active_connection_id=ac.id THEN 1 ELSE 0 END AS is_active
                FROM channel_auth_connections ac
                LEFT JOIN channel_auth_preferences ap ON ap.channel_db_id=ac.channel_db_id
                ORDER BY ac.channel_db_id, CASE ac.method WHEN 'legacy' THEN 0 ELSE 1 END
                """
            ).fetchall()
        safe = []
        for row in rows:
            item = dict(row)
            item["credential_present"] = bool(str(item.pop("credential_path", "") or "").strip())
            item["last_error"] = str(item.get("last_error") or "")[:240]
            safe.append(item)
        return {"ok": True, "connections": safe, "methods": [METHOD_LEGACY, METHOD_WEB]}

    def _prune_states(self, con: sqlite3.Connection) -> None:
        con.execute("DELETE FROM pending_oauth WHERE expires_at < ?", (_iso(),))

    def _save_state(self, state: str, channel_db_id: int, requested_by: str) -> None:
        now = _utcnow()
        with sqlite3.connect(self.state_db_path, timeout=20) as con:
            self._prune_states(con)
            con.execute(
                "INSERT INTO pending_oauth(state,channel_db_id,requested_by,created_at,expires_at) VALUES(?,?,?,?,?)",
                (state, int(channel_db_id), requested_by, _iso(now), _iso(now + timedelta(minutes=STATE_TTL_MINUTES))),
            )

    def _consume_state(self, state: str, requested_by: str) -> Dict[str, Any]:
        raw = str(state or "").strip()
        if not raw:
            raise RuntimeError("Google OAuth state is missing.")
        with sqlite3.connect(self.state_db_path, timeout=20) as con:
            con.row_factory = sqlite3.Row
            self._prune_states(con)
            row = con.execute("SELECT * FROM pending_oauth WHERE state=?", (raw,)).fetchone()
            if row is None:
                raise RuntimeError("Google OAuth session expired or was already used. Start Connect Google again.")
            saved_user = str(row["requested_by"] or "")
            if saved_user and requested_by and saved_user != requested_by:
                raise RuntimeError("This Google OAuth response belongs to a different LUM session.")
            con.execute("DELETE FROM pending_oauth WHERE state=?", (raw,))
            return dict(row)

    def begin(self, channel_db_id: int, user: Any = None) -> str:
        self.load_client()
        channel = self._channel(channel_db_id)
        requested_by = (
            _safe_user_value(user, "id")
            or _safe_user_value(user, "username")
            or _safe_user_value(user, "email")
        )
        from google_auth_oauthlib.flow import Flow
        from desktop_core.modules.auth_manager import SCOPES

        flow = Flow.from_client_config(self._client_config(), scopes=SCOPES)
        flow.redirect_uri = GOOGLE_CALLBACK_URI
        kwargs: Dict[str, Any] = {
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        }
        email = str(channel.get("google_account_email") or "").strip()
        if email:
            kwargs["login_hint"] = email
        authorization_url, state = flow.authorization_url(**kwargs)
        if not state:
            raise RuntimeError("Google OAuth library did not create a state value.")
        self._save_state(str(state), int(channel_db_id), requested_by)
        return authorization_url

    @staticmethod
    def _choose_verified_channel(items: list[Dict[str, Any]], expected: str) -> Dict[str, Any]:
        real_expected = expected if expected.startswith("UC") else ""
        if real_expected:
            match = next((item for item in items if str(item.get("id") or "") == real_expected), None)
            if match is None:
                returned = ", ".join(str(item.get("id") or "unknown") for item in items[:10])
                raise RuntimeError(f"Wrong Brand Channel authorized. Expected {real_expected}; Google returned {returned}.")
            return match
        if len(items) != 1:
            returned = ", ".join(str(item.get("id") or "unknown") for item in items[:10])
            raise RuntimeError(
                "Google returned multiple YouTube channels, so LUM refused to guess. "
                f"Put the exact UC Channel ID in the Sheet row and reconnect. Returned: {returned}"
            )
        return items[0]

    def _save_web_token(self, credentials: Any, channel_id: str) -> Path:
        if not getattr(credentials, "refresh_token", None):
            raise RuntimeError(
                "Google did not return an offline refresh token. Revoke the old grant for this OAuth client, "
                "confirm the OAuth app is in production, then authorize again."
            )
        from desktop_core.modules.token_utils import (
            VALID_TOKEN,
            atomic_write_text,
            classify_google_json,
            clear_runtime_invalid_grant,
            safe_channel_id_token_filename,
            token_file_lock,
        )
        from desktop_core.modules.secret_store import secure_file_for_current_user

        token_path = self.web_token_dir / safe_channel_id_token_filename(channel_id)
        with token_file_lock(token_path):
            if token_path.exists() and classify_google_json(token_path) != VALID_TOKEN:
                quarantine = self.web_token_dir / "quarantine"
                quarantine.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.move(str(token_path), str(quarantine / f"{token_path.stem}_{stamp}{token_path.suffix}"))
            atomic_write_text(token_path, credentials.to_json())
            secure_file_for_current_user(token_path)
            clear_runtime_invalid_grant(token_path)
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
        return token_path

    def _upsert_connection(
        self,
        channel_db_id: int,
        method: str,
        credential_path: str,
        verified_channel_id: str,
        verified_channel_name: str,
        status: str = "OK",
        error: str = "",
    ) -> int:
        if method not in METHODS:
            raise ValueError("Unknown authentication method.")
        with self.db.connect() as con:
            con.execute(
                """
                INSERT INTO channel_auth_connections(
                    channel_db_id,method,credential_path,verified_channel_id,verified_channel_name,
                    status,last_tested_at,last_error,updated_at
                ) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP,?,CURRENT_TIMESTAMP)
                ON CONFLICT(channel_db_id,method) DO UPDATE SET
                    credential_path=excluded.credential_path,
                    verified_channel_id=excluded.verified_channel_id,
                    verified_channel_name=excluded.verified_channel_name,
                    status=excluded.status,
                    last_tested_at=CURRENT_TIMESTAMP,
                    last_error=excluded.last_error,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    int(channel_db_id), method, str(credential_path), str(verified_channel_id),
                    str(verified_channel_name), str(status), str(error)[:1000],
                ),
            )
            row = con.execute(
                "SELECT id FROM channel_auth_connections WHERE channel_db_id=? AND method=?",
                (int(channel_db_id), method),
            ).fetchone()
            return int(row["id"])

    def complete(self, *, state: str, query_string: str, user: Any = None) -> Dict[str, Any]:
        requested_by = (
            _safe_user_value(user, "id")
            or _safe_user_value(user, "username")
            or _safe_user_value(user, "email")
        )
        pending = self._consume_state(state, requested_by)
        channel_db_id = int(pending["channel_db_id"])
        channel = self._channel(channel_db_id)

        from google_auth_oauthlib.flow import Flow
        from desktop_core.modules.auth_manager import AuthManager, SCOPES, build_channel_url, proxy_to_requests_dict

        flow = Flow.from_client_config(self._client_config(), scopes=SCOPES, state=state)
        flow.redirect_uri = GOOGLE_CALLBACK_URI
        authorization_response = GOOGLE_CALLBACK_URI + ("?" + query_string if query_string else "")
        proxy_config = self._channel_proxy(channel)
        proxies = proxy_to_requests_dict(proxy_config)
        if proxies:
            flow.fetch_token(authorization_response=authorization_response, proxies=proxies)
        else:
            flow.fetch_token(authorization_response=authorization_response)

        manager = AuthManager(self.db)
        youtube = manager.build_youtube_service_from_credentials(flow.credentials, proxy_config=proxy_config)
        items = list((youtube.channels().list(part="snippet", mine=True).execute()).get("items") or [])
        if not items:
            raise RuntimeError("Google authorization returned no YouTube channel.")
        verified = self._choose_verified_channel(items, str(channel.get("channel_id") or "").strip())
        channel_id = str(verified.get("id") or "").strip()
        channel_name = str((verified.get("snippet") or {}).get("title") or channel_id)
        token_path = self._save_web_token(flow.credentials, channel_id)

        # Preserve the currently projected credential while the core channel identity is enriched.
        current_path = str(channel.get("token_file_path") or "").strip()
        self.db.update_channel_after_oauth(
            channel_db_id,
            channel_id=channel_id,
            channel_name=channel_name,
            channel_url=build_channel_url(channel_id),
            token_file_path=current_path,
            google_account_email=str(channel.get("google_account_email") or "") or None,
            browser_profile_name=str(channel.get("browser_profile_name") or ""),
            **self._channel_proxy(channel),
        )
        web_id = self._upsert_connection(
            channel_db_id, METHOD_WEB, str(token_path), channel_id, channel_name, status="OK"
        )

        # Do not silently replace a working legacy choice. Web becomes active only
        # when the legacy credential is unusable; otherwise the user activates it.
        with self.db.connect() as con:
            legacy = con.execute(
                "SELECT status FROM channel_auth_connections WHERE channel_db_id=? AND method=?",
                (channel_db_id, METHOD_LEGACY),
            ).fetchone()
        activated = not legacy or str(legacy["status"] or "") != "OK"
        if activated:
            self.select_active(channel_db_id, METHOD_WEB, verify=False)
        return {
            "ok": True,
            "channel_db_id": channel_db_id,
            "connection_id": web_id,
            "method": METHOD_WEB,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "activated": activated,
        }

    def _connection(self, channel_db_id: int, method: str) -> Dict[str, Any]:
        if method not in METHODS:
            raise ValueError("Unknown authentication method.")
        self.ensure_channel_connections()
        with self.db.connect() as con:
            row = con.execute(
                "SELECT * FROM channel_auth_connections WHERE channel_db_id=? AND method=?",
                (int(channel_db_id), method),
            ).fetchone()
        if not row:
            raise RuntimeError(f"{method} is not connected for this channel.")
        return dict(row)

    def test_connection(self, channel_db_id: int, method: str) -> Dict[str, Any]:
        connection = self._connection(channel_db_id, method)
        channel = self._channel(channel_db_id)
        path = str(connection.get("credential_path") or "").strip()
        if _token_status(path) != "OK":
            error = f"{method} credential is missing or invalid."
            with self.db.connect() as con:
                con.execute(
                    "UPDATE channel_auth_connections SET status='INVALID_TOKEN',last_error=?,last_tested_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (error, int(connection["id"])),
                )
            raise RuntimeError(error)

        try:
            from desktop_core.modules.auth_manager import AuthManager

            manager = AuthManager(self.db)
            youtube = manager.build_youtube_service(path, proxy_config=self._channel_proxy(channel))
            items = list((youtube.channels().list(part="snippet", mine=True).execute()).get("items") or [])
            if not items:
                raise RuntimeError("Saved Google credential returned no YouTube channel.")
            verified = self._choose_verified_channel(items, str(channel.get("channel_id") or "").strip())
        except Exception as exc:
            current_status = _token_status(path)
            failed_status = "INVALID_TOKEN" if current_status != "OK" else "CONNECTION_ERROR"
            with self.db.connect() as con:
                con.execute(
                    """
                    UPDATE channel_auth_connections SET status=?,last_error=?,last_tested_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (failed_status, str(exc)[:1000], int(connection["id"])),
                )
            raise
        channel_id = str(verified.get("id") or "")
        channel_name = str((verified.get("snippet") or {}).get("title") or channel_id)
        with self.db.connect() as con:
            con.execute(
                """
                UPDATE channel_auth_connections SET status='OK',verified_channel_id=?,verified_channel_name=?,
                    last_error='',last_tested_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (channel_id, channel_name, int(connection["id"])),
            )
        return {
            "ok": True,
            "channel_db_id": int(channel_db_id),
            "connection_id": int(connection["id"]),
            "method": method,
            "channel_id": channel_id,
            "channel_name": channel_name,
        }

    def select_active(self, channel_db_id: int, method: str, *, verify: bool = True) -> Dict[str, Any]:
        result = self.test_connection(channel_db_id, method) if verify else None
        connection = self._connection(channel_db_id, method)
        if _token_status(str(connection.get("credential_path") or "")) != "OK":
            raise RuntimeError(f"Cannot activate {method}: its credential is not usable.")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                INSERT INTO channel_auth_preferences(channel_db_id,active_connection_id,updated_at)
                VALUES(?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(channel_db_id) DO UPDATE SET
                    active_connection_id=excluded.active_connection_id,updated_at=CURRENT_TIMESTAMP
                """,
                (int(channel_db_id), int(connection["id"])),
            )
            con.execute(
                "UPDATE channels SET token_file_path=?,last_used_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(connection["credential_path"]), int(channel_db_id)),
            )
            con.commit()
        return result or {
            "ok": True,
            "channel_db_id": int(channel_db_id),
            "connection_id": int(connection["id"]),
            "method": method,
        }
