from __future__ import annotations
import hashlib
import hmac
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import USERS_DB, ensure_dirs

_USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")
_ITERATIONS = 320_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    password = str(password or "")
    if len(password) < 8:
        raise ValueError("Password must contain at least 8 characters")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = str(encoded).split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


class UserStore:
    def __init__(self, path: str | Path = USERS_DB):
        ensure_dirs()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE COLLATE NOCASE NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    ui_mode TEXT NOT NULL DEFAULT 'simple',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lum_users_active ON users(is_active,username)")

    def count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def count_active_admins(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND is_active=1").fetchone()[0])

    def create(self, username: str, password: str, *, is_admin: bool = False, ui_mode: str = "simple") -> int:
        username = str(username or "").strip()
        if not _USERNAME.fullmatch(username):
            raise ValueError("Username must be 3-64 characters using letters, numbers, dot, dash, underscore or @")
        mode = str(ui_mode or "simple").lower().strip()
        if mode not in {"simple", "heavy"}:
            raise ValueError("Invalid UI mode")
        if is_admin:
            mode = "heavy"
        stamp = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO users(username,password_hash,is_active,is_admin,ui_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (username, hash_password(password), 1, 1 if is_admin else 0, mode, stamp, stamp),
            )
            return int(cur.lastrowid)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (str(username or "").strip(),)).fetchone()
            if not row or not int(row["is_active"] or 0) or not verify_password(password, str(row["password_hash"])):
                return None
            conn.execute("UPDATE users SET last_login_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), int(row["id"])))
            return dict(row)

    def get(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
            return dict(row) if row else None

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (str(username or "").strip(),)).fetchone()
            return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id,username,is_active,is_admin,ui_mode,created_at,updated_at,last_login_at FROM users ORDER BY username COLLATE NOCASE").fetchall()
            return [dict(r) for r in rows]

    def update(self, user_id: int, *, ui_mode: str, is_active: bool, is_admin: bool) -> None:
        current = self.get(user_id)
        if not current:
            raise ValueError("User not found")
        mode = str(ui_mode or "simple").lower().strip()
        if mode not in {"simple", "heavy"}:
            raise ValueError("Invalid UI mode")
        if is_admin:
            mode = "heavy"
        if int(current.get("is_admin") or 0) and int(current.get("is_active") or 0):
            if (not is_admin or not is_active) and self.count_active_admins() <= 1:
                raise ValueError("At least one active admin must remain")
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET ui_mode=?,is_active=?,is_admin=?,updated_at=? WHERE id=?",
                (mode, 1 if is_active else 0, 1 if is_admin else 0, now_iso(), int(user_id)),
            )

    def set_password(self, user_id: int, password: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (hash_password(password), now_iso(), int(user_id)))
