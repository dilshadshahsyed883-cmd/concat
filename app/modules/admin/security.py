"""Independent, single-owner Concat admin authentication. No Scene SSO or Scene DB.

Never trust forwarded identity headers. Local password verification uses stdlib
scrypt, and bearer sessions are random, hashed, revocable SQLite records.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

COOKIE = "__Host-concat_session"
# Browsers cap persistent cookies; renew on authenticated activity.
SESSION_COOKIE_MAX_AGE = 400 * 86400
LOGIN_CSRF_COOKIE = "__Host-concat_login_csrf"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ALLOWED_APPS = frozenset({"concat", "concat-uploader", "dashboard"})
# Bound resource use and avoid a user-configurable scrypt work factor.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 16384, 8, 1
_UNAUTHENTICATED = "Invalid username or password"


class SecurityUnavailable(RuntimeError):
    """Local identity is missing or unsafe. Never fall back to Scene."""


def _valid_username(value: Any) -> bool:
    return isinstance(value, str) and bool(USERNAME_PATTERN.fullmatch(value)) and value != "__default__"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _https_origin(value: str) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
        parsed.password or parsed.path or parsed.query or parsed.fragment or
        (parsed.port is not None and parsed.port != 443)):
        raise SecurityUnavailable("Local admin requires HTTPS public origin")
    return parsed.scheme + "://" + parsed.netloc


def _parse_password_hash(raw: str) -> tuple[bytes, bytes]:
    try:
        name, n, r, p, salt, digest = raw.split("$")
        if (name != "scrypt" or (int(n), int(r), int(p)) !=
                (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)):
            raise ValueError("Invalid scrypt parameters")
        if len(salt) != 32 or len(digest) != 64:
            raise ValueError("Invalid scrypt sizes")
        return bytes.fromhex(salt), bytes.fromhex(digest)
    except (ValueError, TypeError) as exc:
        raise SecurityUnavailable("Invalid private admin password hash") from exc


def password_hash(password: str) -> str:
    """Used only by private, interactive host setup. Never print the password."""
    if (not isinstance(password, str) or len(password) < 6 or
        len(password) > 1024 or any(ord(ch) < 32 for ch in password)):
        raise ValueError("Admin password must contain at least 6 safe characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt$16384$8$1$" + salt.hex() + "$" + digest.hex()


class SecurityService:
    def __init__(self, database: Path, username: str, encoded_password: str,
                 session_secret: str, web_origin: str):
        if not _valid_username(username):
            raise SecurityUnavailable("Invalid local admin username")
        if len(session_secret.encode("utf-8")) < 32:
            raise SecurityUnavailable("Private admin session secret too short")
        if not database.is_absolute() or database.is_symlink():
            raise SecurityUnavailable("Local security DB must be a real absolute path")
        if not database.parent.is_dir() or database.parent.is_symlink():
            raise SecurityUnavailable("Local security storage is not provisioned")
        self.username = username
        self.database = database
        self.web_origin = _https_origin(web_origin)
        self.secret = session_secret.encode("utf-8")
        self.salt, self.password_digest = _parse_password_hash(encoded_password)
        self.credential_tag = _hash(encoded_password)
        if self.database.exists():
            info = self.database.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise SecurityUnavailable("Local security DB must be a private regular file")
        else:
            try:
                fd = os.open(str(self.database), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            except OSError as exc:
                raise SecurityUnavailable("Unable to initialize private local sessions") from exc
        self._initialize()

    @classmethod
    def from_environment(cls, app) -> "SecurityService":
        if os.environ.get("CONCAT_AUTH_MODE") != "local-admin":
            raise SecurityUnavailable("Explicit local-admin authentication mode required")
        keys = ("CONCAT_WEB_SECURITY_DB", "CONCAT_ADMIN_USERNAME",
                "CONCAT_ADMIN_PASSWORD_HASH", "CONCAT_ADMIN_SESSION_SECRET")
        if any(not os.environ.get(key) for key in keys):
            raise SecurityUnavailable("Private Concat local admin configuration incomplete")
        if os.environ.get("CONCAT_ADMIN_SESSION_SECRET") == os.environ.get("LUM_SESSION_SECRET"):
            raise SecurityUnavailable("Admin and uploader secrets must be distinct")
        return cls(Path(os.environ["CONCAT_WEB_SECURITY_DB"]),
                   os.environ["CONCAT_ADMIN_USERNAME"],
                   os.environ["CONCAT_ADMIN_PASSWORD_HASH"],
                   os.environ["CONCAT_ADMIN_SESSION_SECRET"],
                   app.state.settings.origin)

    def _db(self):
        con = sqlite3.connect(str(self.database), timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def _initialize(self):
        # Incompatible legacy Scene ticket DBs are not silently upgraded.
        with self._db() as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
            if cols and not {"token_hash", "username_key", "csrf_hash", "credential_tag",
                             "created_at", "expires_at", "revoked_at"}.issubset(cols):
                raise SecurityUnavailable("Legacy SSO database cannot be adopted")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username_key TEXT NOT NULL,
                    csrf_hash TEXT NOT NULL,
                    credential_tag TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(username_key, revoked_at);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    bucket TEXT NOT NULL, happened_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempt_time ON login_attempts(happened_at);
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_key TEXT NOT NULL, action TEXT NOT NULL,
                    target_key TEXT NOT NULL, created_at INTEGER NOT NULL
                );
            """)
            # Upgrade only still-active sessions. Never revive expired or revoked tokens.
            con.execute("UPDATE sessions SET expires_at=0 WHERE revoked_at IS NULL AND expires_at>?",
                        (int(time.time()),))
            con.execute("DELETE FROM config WHERE key='session_max_minutes'")

    def _config(self) -> dict[str, Any]:
        config = {"maintenance_mode": False}
        with self._db() as con:
            for row in con.execute("SELECT key,value FROM config"):
                if row["key"] in config:
                    try:
                        config[row["key"]] = json.loads(row["value"])
                    except (TypeError, ValueError) as exc:
                        raise SecurityUnavailable("Corrupt local admin configuration") from exc
        if type(config["maintenance_mode"]) is not bool:
            raise SecurityUnavailable("Corrupt local admin configuration")
        return config

    def _session_row(self, request: Request):
        token = request.cookies.get(COOKIE, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,200}", token):
            return None
        with self._db() as con:
            return con.execute(
                "SELECT * FROM sessions WHERE token_hash=? AND username_key=? "
                "AND credential_tag=? AND revoked_at IS NULL AND (expires_at=0 OR expires_at>?)",
                (_hash(token), self.username.casefold(), self.credential_tag, int(time.time()))
            ).fetchone()

    def authenticate(self, request: Request):
        from app.auth import Principal
        if self._session_row(request) is None:
            return None
        request.state.concat_authenticated_token = request.cookies[COOKIE]
        return Principal(self.username, is_admin=True, is_active=True, concat_granted=True)

    def _audit(self, action: str, target: str):
        with self._db() as con:
            con.execute("INSERT INTO audit(actor_key,action,target_key,created_at) VALUES (?,?,?,?)",
                        (self.username.casefold(), action, target, int(time.time())))

    def login(self, username: str, password: str, request: Request) -> tuple[str, int]:
        # Persisted, bounded throttling even if the process restarts.
        # Bucket deliberately does not trust X-Forwarded-For from the browser.
        remote = request.client.host if request.client else "unknown"
        bucket = _hash(str(remote))[:24]
        now = int(time.time())
        with self._db() as con:
            con.execute("DELETE FROM login_attempts WHERE happened_at<?", (now - 900,))
            attempts = con.execute("SELECT COUNT(*) FROM login_attempts WHERE bucket=?",
                                   (bucket,)).fetchone()[0]
        if attempts >= 8:
            raise HTTPException(429, "Too many login attempts; retry later",
                                headers={"Retry-After": "900"})
        # Equal-time comparison for the only configured administrator.
        expected = hashlib.scrypt(
            (password if isinstance(password, str) and len(password) <= 1024 else "").encode("utf-8"),
            salt=self.salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
        )
        correct_user = isinstance(username, str) and hmac.compare_digest(
            username.casefold(), self.username.casefold()
        )
        if not (correct_user and hmac.compare_digest(expected, self.password_digest)):
            with self._db() as con:
                con.execute("INSERT INTO login_attempts(bucket,happened_at) VALUES (?,?)",
                            (bucket, now))
            raise HTTPException(401, _UNAUTHENTICATED)
        token = secrets.token_urlsafe(48)
        csrf = self._csrf_for(token)
        max_age = SESSION_COOKIE_MAX_AGE
        with self._db() as con:
            con.execute("DELETE FROM login_attempts WHERE bucket=?", (bucket,))
            con.execute("DELETE FROM sessions WHERE expires_at>0 AND expires_at<?", (now - 86400,))
            con.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,NULL)",
                (_hash(token), self.username.casefold(), _hash(csrf), self.credential_tag,
                 now, 0)
            )
        self._audit("login", self.username.casefold())
        return token, max_age

    def _csrf_for(self, token: str) -> str:
        return hmac.new(self.secret, ("csrf." + token).encode(), hashlib.sha256).hexdigest()

    def csrf_token(self, request: Request) -> str | None:
        if self._session_row(request) is None:
            return None
        return self._csrf_for(request.cookies.get(COOKIE, ""))

    def verify_csrf(self, request: Request):
        row = self._session_row(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not row or not hmac.compare_digest(_hash(csrf), row["csrf_hash"]):
            raise HTTPException(403, "CSRF validation failed")
        origin = request.headers.get("origin") or ""
        referer = request.headers.get("referer") or ""
        if origin:
            supplied = origin.rstrip("/")
        elif referer:
            url = urlsplit(referer)
            supplied = url.scheme + "://" + url.netloc
        else:
            raise HTTPException(403, "Origin validation failed")
        if not hmac.compare_digest(supplied, self.web_origin):
            raise HTTPException(403, "Origin validation failed")

    def revoke_cookie(self, request: Request):
        token = request.cookies.get(COOKIE, "")
        if token:
            with self._db() as con:
                con.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                            (int(time.time()), _hash(token)))
            self._audit("logout", self.username.casefold())

    def revoke_all(self):
        with self._db() as con:
            result = con.execute(
                "UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL",
                (int(time.time()),)
            )
            count = result.rowcount
        self._audit("revoke_all_sessions", self.username.casefold())
        return count

    def users(self):
        return [{"username": self.username, "is_active": True, "is_admin": True,
                 "concat_granted": True}]

    def update_config(self, actor: str, config: Mapping[str, Any]):
        if (actor.casefold() != self.username.casefold() or not config or
            set(config) - {"maintenance_mode"}):
            raise HTTPException(422, "Invalid admin configuration")
        if "maintenance_mode" in config and type(config["maintenance_mode"]) is not bool:
            raise HTTPException(422, "Invalid admin configuration")
        with self._db() as con:
            for key, value in config.items():
                con.execute("INSERT INTO config(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))
        self._audit("system_config", ",".join(sorted(config)))
        return self._config()

    def audit(self):
        with self._db() as con:
            return [dict(r) for r in con.execute(
                "SELECT actor_key,action,target_key,created_at FROM audit ORDER BY id DESC LIMIT 100")]


def get_security(request: Request) -> SecurityService:
    service = getattr(request.app.state, "concat_security", None)
    if service is None:
        try:
            service = SecurityService.from_environment(request.app)
        except SecurityUnavailable as exc:
            raise HTTPException(503, "Local admin authentication not configured") from exc
        request.app.state.concat_security = service
    return service


def ensure_owner(principal, record: Mapping[str, Any] | None, app: str):
    if (app not in ALLOWED_APPS or not record or
        not _valid_username(record.get("owner_username")) or
        record.get("app") != app or
        record["owner_username"].casefold() != principal.username.casefold()):
        raise HTTPException(404, "Resource not found")
    return record


def owned_records(principal, rows, app: str):
    result = []
    for row in rows:
        try:
            ensure_owner(principal, row, app)
        except HTTPException:
            continue
        result.append(row)
    return result


def require_csrf(request: Request):
    get_security(request).verify_csrf(request)
