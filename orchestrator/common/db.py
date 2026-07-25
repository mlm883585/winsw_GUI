from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path


Migration = Callable[[sqlite3.Connection], None]


class DatabaseVersionError(RuntimeError):
    pass


class SQLiteDatabase:
    """Small synchronous SQLite boundary with durable MVP pragmas."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be a positive integer")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._write_lock = threading.RLock()
        self._functions: dict[str, tuple[int, Callable[..., object], bool]] = {}

    def register_function(
        self,
        name: str,
        num_params: int,
        function: Callable[..., object],
        *,
        deterministic: bool = False,
    ) -> None:
        if not name or num_params < 0:
            raise ValueError("SQLite function name and arity must be valid")
        with self._write_lock:
            self._functions[name] = (num_params, function, deterministic)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        for name, (num_params, function, deterministic) in self._functions.items():
            connection.create_function(
                name,
                num_params,
                function,
                deterministic=deterministic,
            )
        return connection

    def initialize(self, component: str, migrations: Sequence[Migration]) -> None:
        with self._write_lock:
            connection = self.connect()
            try:
                self._initialize_connection(connection, component, migrations)
            finally:
                connection.close()

    def _initialize_connection(
        self,
        connection: sqlite3.Connection,
        component: str,
        migrations: Sequence[Migration],
    ) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_versions (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL CHECK(version >= 0)
            )
            """
        )
        row = connection.execute(
            "SELECT version FROM schema_versions WHERE component=?", (component,)
        ).fetchone()
        current = int(row["version"]) if row else 0
        supported = len(migrations)
        if current > supported:
            raise DatabaseVersionError(
                f"{component} database version {current} is newer than supported {supported}"
            )
        for index in range(current, supported):
            try:
                connection.execute("BEGIN IMMEDIATE")
                migrations[index](connection)
                connection.execute(
                    """
                    INSERT INTO schema_versions(component, version) VALUES(?, ?)
                    ON CONFLICT(component) DO UPDATE SET version=excluded.version
                    """,
                    (component, index + 1),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()
