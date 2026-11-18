from __future__ import annotations

import sqlite3
from typing import Any, Dict, TYPE_CHECKING

from ..secret_store import reveal_secret

if TYPE_CHECKING:
    from ..database import Database


class BaseRepository:
    """Shared repository plumbing plus transitional cross-repository routing."""

    def __init__(self, database: "Database"):
        self._database = database

    def connect(self) -> sqlite3.Connection:
        return self._database.connect()

    @staticmethod
    def _decode_sensitive_record(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        for key in ("proxy_password", "sheet_password", "sheet_backup_code", "sheet_backup_key"):
            if key in data:
                data[key] = reveal_secret(data.get(key))
        return data
