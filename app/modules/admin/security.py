"""Verified Scene SSO, local session revocation and strict owner boundaries.

The Scene issuer must implement the upstream contract described in SSO_CONTRACT.md.
No browser identity headers, cookies minted by Scene without verification, or
unattributed historical rows are treated as identities.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import quote, urlsplit
from typing import Any, Mapping

from fastapi import HTTPException, Request

COOKIE = "__Host-concat_session"
STATE_COOKIE = "__Host-concat_sso_state"
SID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
USERNAME_PATTERN = re.compile(r"^[^\s/\\:\x00-\x1f\x7f]{1,128}$")
ALLOWED_APPS = frozenset({"concat", "concat-uploader", "dashboard"})


class SecurityUnavailable(RuntimeError):
    """A missing or untrusted identity source must not enable fallback auth."""


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _b64decode(raw: str) -> bytes:
    if not raw or len(raw) > 12000 or re.search(r"[^A-Za-z0-9_-]", raw):
        raise ValueError("invalid encoding")
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _unique_json(data: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate claim")
            result[key] = value
        return result
    value = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise ValueError("invalid claims")
    return value


def _origin(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    p = urlsplit(raw)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.path or p.query or p.fragment or p.netloc != p.hostname
            and not (p.port == 443 and p.netloc == p.hostname + ":443")):
        raise SecurityUnavailable("HTTPS-only SSO origin required")
    return raw


def _valid_username(value: Any) -> bool:
    return isinstance(value, str) and bool(USERNAME_PATTERN.fullmatch(value)) and value == value.strip()


def _safe_return_path(value: str | None) -> str:
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith("/") or value.startswith("//"):
        return "/dashboard"
    if any(ord(ch) < 32 or ord(ch) == 127 or ch == "\\" for ch in value):
        return "/dashboard"
    from urllib.parse import unquote
    p = urlsplit(value)
    if p.scheme or p.netloc or p.fragment:
        return "/dashboard"
    check = p.path
    for _ in range(3):
        if check.startswith("//") or "\\" in check or any(seg in (".", "..") for seg in check.split("/")):
            return "/dashboard"
        check = unquote(check)
    if not p.path.startswith(("/dashboard", "/concat/", "/concat-uploader/", "/admin")):
        return "/dashboard"
    return value


class SecurityService:
    def __init__(self, scene_database: Path, security_database: Path, sso_secret: str,
                 scene_origin: str, web_origin: str, bootstrap_admins: set[str]):
        if len(sso_secret.encode("utf-8")) < 32:
            raise SecurityUnavailable("A separate 32-byte Scene SSO signing key is required")
        self.scene_database = scene_database
        self.security_database = security_database
        self.secret = sso_secret.encode("utf-8")
        self.scene_origin = _origin(scene_origin)
        self.web_origin = _origin(web_origin)
        self.bootstrap_admins = {x.casefold() for x in bootstrap_admins if _valid_username(x)}
        if not self.scene_database.is_file():
            raise SecurityUnavailable("Scene account database unavailable")
        if self.scene_database == self.security_database:
            raise SecurityUnavailable("Scene and Concat databases must be separate")
        if not self.security_database.parent.is_dir():
            raise SecurityUnavailable("Concat auth storage directory missing")
        # SQLite uses the process umask for newly created files. Pre-create the
        # session store privately; never overwrite Scene or follow a hard link.
        if not self.security_database.exists():
            try:
                fd = os.open(str(self.security_database), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
            except OSError as exc:
                raise SecurityUnavailable("Private Concat auth storage unavailable") from exc
        import stat
        info = self.security_database.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SecurityUnavailable("Invalid Concat auth storage file")
        try:
            os.chmod(self.security_database, 0o600)
        except OSError as exc:
            raise SecurityUnavailable("Cannot secure Concat auth storage file") from exc
        self._initialize()

    @classmethod
    def from_environment(cls, app) -> "SecurityService":
        scene = os.environ.get("CONCAT_SCENE_USERS_DB", "").strip()
        local = os.environ.get("CONCAT_WEB_SECURITY_DB", "").strip()
        secret = os.environ.get("CONCAT_SCENE_SSO_SECRET", "")
        if not scene or not local or not secret:
            raise SecurityUnavailable("Scene SSO configuration missing")
        admins = {s.strip() for s in os.environ.get("CONCAT_BOOTSTRAP_ADMINS", "").split(",") if s.strip()}
        return cls(
            Path(scene).resolve(), Path(local).resolve(), secret,
            os.environ.get("CONCAT_SCENE_PUBLIC_ORIGIN", "https://app.scenebuilder.online"),
            app.state.settings.origin, admins,
        )

    def _local(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.security_database), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _scene(self) -> sqlite3.Connection:
        # Read-only authoritative Scene users and revocable SSO sessions.
        # NEVER run migrations or write to production Scene SQLite here.
        if not self.scene_database.is_file():
            raise SecurityUnavailable("Scene account database unavailable")
        uri = "file:" + quote(str(self.scene_database), safe="/") + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            return db
        except sqlite3.Error as exc:
            raise SecurityUnavailable("Scene account database unavailable") from exc

    def _initialize(self) -> None:
        with self._local() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS grants (
                    username_key TEXT PRIMARY KEY, granted INTEGER NOT NULL CHECK(granted IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, username_key TEXT NOT NULL, scene_sid_hash TEXT NOT NULL,
                    csrf_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(username_key, revoked_at);
                CREATE TABLE IF NOT EXISTS used_tickets (
                    jti_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_key TEXT NOT NULL,
                    action TEXT NOT NULL, target_key TEXT NOT NULL, created_at INTEGER NOT NULL
                );
            """)

    def _account(self, username: str) -> dict | None:
        if not _valid_username(username):
            return None
        try:
            with self._scene() as db:
                row = db.execute(
                    "SELECT username,is_active,is_admin,created_at FROM users WHERE username=? COLLATE NOCASE",
                    (username,),
                ).fetchone()
            return dict(row) if row is not None else None
        except sqlite3.Error as exc:
            raise SecurityUnavailable("Scene account schema unavailable") from exc

    def scene_session_active(self, username: str, sid_hash: str) -> bool:
        try:
            with self._scene() as db:
                row = db.execute(
                    """SELECT username_key,expires_at,revoked_at FROM concat_sso_sessions
                       WHERE sid_hash=?""", (sid_hash,),
                ).fetchone()
            return bool(row and row["username_key"] == username.casefold()
                        and int(row["expires_at"]) > int(time.time())
                        and row["revoked_at"] is None)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise SecurityUnavailable("Scene revocable SSO session table unavailable") from exc

    def _grant(self, username: str, scene_is_admin: bool) -> bool:
        with self._local() as db:
            row = db.execute("SELECT granted FROM grants WHERE username_key=?", (username.casefold(),)).fetchone()
        if row is not None:
            return bool(row["granted"])
        return scene_is_admin and username.casefold() in self.bootstrap_admins

    def _config(self) -> dict[str, Any]:
        value = {"maintenance_mode": False, "session_max_minutes": 480}
        with self._local() as db:
            rows = db.execute("SELECT key,value FROM config").fetchall()
        for row in rows:
            if row["key"] in value:
                try:
                    value[row["key"]] = json.loads(row["value"])
                except ValueError:
                    raise SecurityUnavailable("Invalid stored system configuration")
        if not isinstance(value["maintenance_mode"], bool) or type(value["session_max_minutes"]) is not int or not 15 <= value["session_max_minutes"] <= 480:
            raise SecurityUnavailable("Invalid stored system configuration")
        return value

    def account_principal(self, username: str):
        from app.auth import Principal
        account = self._account(username)
        if not account or not bool(account["is_active"]):
            return None
        admin = bool(account["is_admin"])
        granted = self._grant(account["username"], admin)
        if self._config()["maintenance_mode"] and not admin:
            granted = False
        return Principal(account["username"], is_admin=admin, is_active=True, concat_granted=granted)

    def _raw_session(self, request: Request):
        token = request.cookies.get(COOKIE, "")
        if len(token) < 40 or len(token) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            return None
        with self._local() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",
                (_hash(token), int(time.time())),
            ).fetchone()
        return row

    def authenticate(self, request: Request):
        row = self._raw_session(request)
        if row is None:
            return None
        principal = self.account_principal(row["username_key"])
        if principal is None or not self.scene_session_active(principal.username, row["scene_sid_hash"]):
            # Disable stale Concat sessions without trusting cached cookie roles.
            with self._local() as db:
                db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?",
                           (int(time.time()), row["token_hash"]))
            return None
        return principal

    def csrf_token(self, request: Request) -> str | None:
        """Return a secret-bound CSRF value only for an authenticated session."""
        row = self._raw_session(request)
        token = request.cookies.get(COOKIE, "")
        if row is None or not token:
            return None
        return hmac.new(self.secret, ("csrf." + token).encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_csrf(self, request: Request) -> None:
        row = self._raw_session(request)
        csrf = request.headers.get("x-csrf-token", "")
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        if not row or not csrf or not hmac.compare_digest(_hash(csrf), row["csrf_hash"]):
            raise HTTPException(403, "CSRF validation failed")
        if origin:
            if origin.rstrip("/") != self.web_origin:
                raise HTTPException(403, "Origin validation failed")
        elif referer:
            p = urlsplit(referer)
            if p.scheme + "://" + p.netloc != self.web_origin:
                raise HTTPException(403, "Origin validation failed")
        else:
            # Token alone is not enough if the caller omits browser origin evidence.
            raise HTTPException(403, "Origin validation failed")

    def validate_ticket(self, ticket: str, state: str) -> tuple[str, str, str]:
        if len(ticket) > 12000 or ticket.count(".") != 1:
            raise HTTPException(401, "Invalid Scene SSO ticket")
        encoded, mac = ticket.split(".", 1)
        try:
            expected = hmac.new(self.secret, ("scene-concat-v1." + encoded).encode("ascii"), hashlib.sha256).digest()
            signature = _b64decode(mac)
            payload = _unique_json(_b64decode(encoded))
            if not hmac.compare_digest(expected, signature):
                raise ValueError("bad signature")
            now = int(time.time())
            if (type(payload.get("v")) is not int or payload["v"] != 1 or
                    payload.get("iss") != self.scene_origin or payload.get("aud") != "concat-web" or
                    type(payload.get("iat")) is not int or type(payload.get("exp")) is not int or
                    not now - 90 <= payload["iat"] <= now + 5 or
                    not now < payload["exp"] <= payload["iat"] + 90 or
                    payload.get("state") != state or not hmac.compare_digest(str(payload.get("state", "")), state) or
                    not _valid_username(payload.get("sub")) or
                    not isinstance(payload.get("sid"), str) or not SID_PATTERN.fullmatch(payload["sid"]) or
                    not isinstance(payload.get("jti"), str) or not SID_PATTERN.fullmatch(payload["jti"])):
                raise ValueError("invalid SSO claims")
            account = self._account(payload["sub"])
            if not account or not bool(account["is_active"]):
                raise ValueError("inactive or unknown Scene user")
            username = account["username"]
            sid_hash = _hash(payload["sid"])
            if not self.scene_session_active(username, sid_hash):
                raise ValueError("revoked Scene session")
            if not self._grant(username, bool(account["is_admin"])):
                raise ValueError("no Concat grant")
            if self._config()["maintenance_mode"] and not bool(account["is_admin"]):
                raise ValueError("maintenance")
            return username, sid_hash, _hash(payload["jti"])
        except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
            raise HTTPException(401, "Invalid Scene SSO ticket") from exc

    def create_session(self, username: str, sid_hash: str, jti_hash: str) -> tuple[str, str, int]:
        now = int(time.time())
        max_age = self._config()["session_max_minutes"] * 60
        token = secrets.token_urlsafe(48)
        csrf = hmac.new(self.secret, ("csrf." + token).encode("utf-8"), hashlib.sha256).hexdigest()
        with self._local() as db:
            # Retain replay markers well beyond the maximum ticket lifetime.
            db.execute("DELETE FROM used_tickets WHERE expires_at<?", (now,))
            # Atomic one-time consumption; never reuse tickets even after logout.
            try:
                db.execute("INSERT INTO used_tickets(jti_hash,expires_at) VALUES (?,?)",
                           (jti_hash, now + 600))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(401, "Scene SSO ticket already used") from exc
            db.execute(
                """INSERT INTO sessions
                   (token_hash,username_key,scene_sid_hash,csrf_hash,created_at,expires_at)
                   VALUES (?,?,?,?,?,?)""",
                (_hash(token), username.casefold(), sid_hash, _hash(csrf), now, now + max_age),
            )
            db.execute("INSERT INTO audit(actor_key,action,target_key,created_at) VALUES (?,?,?,?)",
                       (username.casefold(), "login", username.casefold(), now))
        return token, csrf, max_age

    def revoke_cookie(self, request: Request) -> None:
        token = request.cookies.get(COOKIE, "")
        if not token:
            return
        with self._local() as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                       (int(time.time()), _hash(token)))

    def _audit(self, actor: str, action: str, target: str) -> None:
        with self._local() as db:
            db.execute("INSERT INTO audit(actor_key,action,target_key,created_at) VALUES (?,?,?,?)",
                       (actor.casefold(), action, target.casefold(), int(time.time())))

    def users(self) -> list[dict]:
        try:
            with self._scene() as db:
                rows = db.execute(
                    "SELECT username,is_active,is_admin,created_at FROM users ORDER BY username COLLATE NOCASE LIMIT 1000"
                ).fetchall()
        except sqlite3.Error as exc:
            raise SecurityUnavailable("Scene user listing unavailable") from exc
        return [{
            "username": row["username"], "is_active": bool(row["is_active"]),
            "is_admin": bool(row["is_admin"]), "created_at": row["created_at"],
            "concat_granted": self._grant(row["username"], bool(row["is_admin"])),
        } for row in rows]

    def grant(self, actor: str, username: str, granted: bool) -> None:
        account = self._account(username)
        if account is None:
            raise HTTPException(404, "User not found")
        if type(granted) is not bool:
            raise HTTPException(422, "Boolean grant required")
        key = account["username"].casefold()
        with self._local() as db:
            db.execute(
                "INSERT INTO grants(username_key,granted) VALUES (?,?) ON CONFLICT(username_key) DO UPDATE SET granted=excluded.granted",
                (key, int(granted)),
            )
            if not granted:
                db.execute("UPDATE sessions SET revoked_at=? WHERE username_key=? AND revoked_at IS NULL",
                           (int(time.time()), key))
        self._audit(actor, "grant" if granted else "deny", key)

    def revoke_user(self, actor: str, username: str) -> int:
        account = self._account(username)
        if account is None:
            raise HTTPException(404, "User not found")
        with self._local() as db:
            cur = db.execute("UPDATE sessions SET revoked_at=? WHERE username_key=? AND revoked_at IS NULL",
                             (int(time.time()), account["username"].casefold()))
        self._audit(actor, "revoke_sessions", account["username"])
        return cur.rowcount

    def update_config(self, actor: str, config: Mapping[str, Any]) -> dict:
        if not isinstance(config, Mapping) or set(config) - {"maintenance_mode", "session_max_minutes"} or not config:
            raise HTTPException(422, "Unsupported configuration key")
        if "maintenance_mode" in config and type(config["maintenance_mode"]) is not bool:
            raise HTTPException(422, "maintenance_mode must be boolean")
        if "session_max_minutes" in config and (type(config["session_max_minutes"]) is not int or
                                                 not 15 <= config["session_max_minutes"] <= 480):
            raise HTTPException(422, "session_max_minutes must be 15 to 480")
        with self._local() as db:
            for key, value in config.items():
                db.execute("INSERT INTO config(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, json.dumps(value)))
        self._audit(actor, "system_config", ",".join(sorted(config)))
        return self._config()

    def audit(self) -> list[dict]:
        with self._local() as db:
            rows = db.execute(
                "SELECT actor_key,action,target_key,created_at FROM audit ORDER BY id DESC LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows]


def get_security(request: Request) -> SecurityService:
    cached = getattr(request.app.state, "concat_security", None)
    if cached is None:
        try:
            cached = SecurityService.from_environment(request.app)
        except SecurityUnavailable as exc:
            raise HTTPException(503, "Verified Scene SSO not configured") from exc
        request.app.state.concat_security = cached
    return cached


def ensure_owner(principal, record: Mapping[str, Any] | None, app: str):
    """Hide foreign, legacy, ownerless and cross-app resources (admin included)."""
    if (app not in ALLOWED_APPS or not record or not _valid_username(record.get("owner_username"))
            or record.get("app") != app or
            record["owner_username"].casefold() != principal.username.casefold()):
        raise HTTPException(404, "Resource not found")
    return record


def owned_records(principal, rows, app: str) -> list:
    result = []
    for row in rows:
        try:
            ensure_owner(principal, row, app)
        except HTTPException:
            continue
        result.append(row)
    return result


def require_csrf(request: Request) -> None:
    security = get_security(request)
    security.verify_csrf(request)
