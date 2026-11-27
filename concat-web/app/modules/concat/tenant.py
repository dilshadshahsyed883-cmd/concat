"""Owner-scoped Cloud Render runtime. Never import or adopt ownerless Scene records."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import threading
from pathlib import Path

from fastapi import HTTPException
from .engine.cloud_render import CloudRender

_OWNER = re.compile(r"[a-f0-9]{64}\Z")
_GATE = threading.RLock()


def owner_key(username: str) -> str:
    # Only call with the verified Principal.username, never with client input.
    return hashlib.sha256(username.encode("utf-8")).hexdigest()


class TenantRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().absolute()
        self.owners = self.root / "owners"
        self.lock = threading.RLock()
        self.managers: dict[str, CloudRender] = {}

    def _ensure_root(self):
        if self.root.is_symlink() or self.owners.is_symlink():
            raise RuntimeError("Concat owner data root cannot be a symlink")
        self.owners.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owners.chmod(0o700)

    def for_key(self, key: str) -> CloudRender:
        if not _OWNER.fullmatch(key):
            raise ValueError("Invalid owner key")
        with self.lock:
            if key not in self.managers:
                self._ensure_root()
                home = self.owners / key
                if home.is_symlink():
                    raise RuntimeError("Concat owner directory cannot be a symlink")
                manager = CloudRender(home, tenant_key=key)
                manager.production.start()
                self.managers[key] = manager
            return self.managers[key]

    def for_principal(self, principal) -> CloudRender:
        return self.for_key(owner_key(principal.username))

    def _known_keys(self):
        if self.owners.is_symlink():
            raise RuntimeError("Concat owner directory cannot be a symlink")
        if not self.owners.is_dir():
            return []
        return [p.name for p in self.owners.iterdir()
                if _OWNER.fullmatch(p.name) and p.is_dir() and not p.is_symlink()
                and (p / "cloud_render.sqlite3").is_file()
                and not (p / "cloud_render.sqlite3").is_symlink()]

    def start_existing(self):
        # Recover owner-specific persistent queues after a restart.
        for key in self._known_keys():
            self.for_key(key)

    def by_receiver_token(self, token: str) -> CloudRender:
        # Callbacks are not browser sessions. The owner's rotating, 288-bit
        # receiver secret is the sole capability; never infer an owner from a job id.
        if not isinstance(token, str) or len(token) > 256 or not token:
            raise HTTPException(401, "Invalid receiver token")
        matches = []
        for key in self._known_keys():
            db = self.owners / key / "cloud_render.sqlite3"
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    row = con.execute("SELECT val FROM config WHERE key='receiver_token'").fetchone()
                finally:
                    con.close()
                if row and hmac.compare_digest(token, row[0]):
                    matches.append(key)
            except sqlite3.Error:
                continue
        if len(matches) != 1:
            raise HTTPException(401, "Invalid receiver token")
        manager = self.for_key(matches[0])
        manager.token_required(token)
        return manager

    def close(self):
        with self.lock:
            managers = list(self.managers.values())
            self.managers.clear()
        for manager in managers:
            manager.production.close()
            manager.pool.shutdown(wait=False, cancel_futures=False)


def get_registry(app) -> TenantRegistry:
    with _GATE:
        existing = getattr(app.state, "concat_registry", None)
        if existing is None:
            root = getattr(app.state, "concat_data_root", None) or os.environ.get(
                "CONCAT_WEB_DATA_ROOT", "/var/lib/concat-web")
            existing = TenantRegistry(root)
            app.state.concat_registry = existing
        return existing
