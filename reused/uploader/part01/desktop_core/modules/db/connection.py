from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Type


class ManagedSQLiteConnection(sqlite3.Connection):
    """SQLite connection whose context manager commits/rolls back and closes."""

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class SQLiteConnectionFactory:
    """Create consistently configured, deterministically closed connections."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            check_same_thread=False,
            factory=ManagedSQLiteConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
