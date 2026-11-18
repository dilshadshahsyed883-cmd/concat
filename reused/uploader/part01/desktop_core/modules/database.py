from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import (
    AutoEngineRepository,
    ChannelRepository,
    MigrationRunner,
    PortabilityRepository,
    QueueRepository,
    SQLiteConnectionFactory,
)
from .path_utils import DB_PATH


class Database:
    """Compatibility facade over focused SQLite repositories.

    Existing UI and worker code can keep calling ``db.list_channels()`` or
    ``db.claim_available_jobs()`` while persistence responsibilities remain
    separated and independently testable.
    """

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self._factory = SQLiteConnectionFactory(self.db_path)
        self._migration_runner = MigrationRunner(self._factory)
        self.init_db()

        self.channels = ChannelRepository(self)
        self.queue = QueueRepository(self)
        self.auto_engine = AutoEngineRepository(self)
        self.portability = PortabilityRepository(self)
        self._method_routes = self._build_method_routes()

    def connect(self):
        """Return a consistently configured SQLite connection.

        Kept public for compatibility with diagnostic utilities. New business
        logic should live inside a repository instead of using this directly.
        """

        return self._factory.connect()

    def init_db(self) -> int:
        """Apply and verify all pending schema migrations."""

        return self._migration_runner.run()

    def _build_method_routes(self) -> dict[str, Any]:
        routes: dict[str, Any] = {}
        for repository in (self.channels, self.queue, self.auto_engine, self.portability):
            for name, value in type(repository).__dict__.items():
                if name.startswith("_") or not callable(value):
                    continue
                if name in routes:
                    raise RuntimeError(f"Duplicate database repository method: {name}")
                routes[name] = repository
        return routes

    def __getattr__(self, name: str) -> Any:
        routes = self.__dict__.get("_method_routes", {})
        repository = routes.get(name)
        if repository is not None:
            return getattr(repository, name)
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")
