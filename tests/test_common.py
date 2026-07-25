from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from orchestrator.common.db import DatabaseVersionError, SQLiteDatabase
from orchestrator.common.graph import DependencyCycleError, reachable_descendants, topological_levels
from orchestrator.common.security import canonical_request_fingerprint, parse_uuid4


def test_topological_levels_and_descendants() -> None:
    nodes = ["mysql", "redis", "nacos", "java", "nginx"]
    edges = [
        ("nacos", "mysql"),
        ("nacos", "redis"),
        ("java", "nacos"),
        ("nginx", "java"),
    ]
    assert topological_levels(nodes, edges) == [
        ["mysql", "redis"],
        ["nacos"],
        ["java"],
        ["nginx"],
    ]
    assert reachable_descendants(["nacos"], edges) == {"java", "nginx"}


def test_cycle_is_rejected() -> None:
    with pytest.raises(DependencyCycleError):
        topological_levels(["a", "b"], [("a", "b"), ("b", "a")])


def test_request_fingerprint_is_canonical() -> None:
    left = canonical_request_fingerprint("post", "/api/v1/services/mysql/actions/start", {"b": 2, "a": 1})
    right = canonical_request_fingerprint("POST", "/api/v1/services/mysql/actions/start", {"a": 1, "b": 2})
    assert left == right
    assert len(left) == 64


def test_uuid4_header_contract() -> None:
    value = "db7df82f-c129-4e7a-bc51-6bcd4433fe74"
    assert parse_uuid4(value) == UUID(value)
    assert parse_uuid4(value.upper()) == UUID(value)
    with pytest.raises(Exception):
        parse_uuid4("8b63b1a0-fca0-11da-8cd6-0800200c9a66")


def test_sqlite_pragmas_and_version_guard(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "state.db")

    def migration(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")

    database.initialize("test", [migration])
    with database.read() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        connection.execute("UPDATE schema_versions SET version=99 WHERE component='test'")
    with pytest.raises(DatabaseVersionError):
        database.initialize("test", [migration])


def test_sqlite_registered_function_is_available_on_every_connection(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "function.db")
    database.register_function("double_value", 1, lambda value: int(value) * 2, deterministic=True)
    with database.read() as connection:
        assert connection.execute("SELECT double_value(3)").fetchone()[0] == 6
    with database.transaction() as connection:
        assert connection.execute("SELECT double_value(4)").fetchone()[0] == 8

    with pytest.raises(ValueError, match="function name and arity"):
        database.register_function("", 1, lambda value: value)


@pytest.mark.parametrize("busy_timeout_ms", [0, -1])
def test_sqlite_rejects_non_positive_busy_timeout(
    tmp_path: Path, busy_timeout_ms: int
) -> None:
    with pytest.raises(ValueError, match="busy_timeout_ms must be a positive integer"):
        SQLiteDatabase(tmp_path / "invalid.db", busy_timeout_ms=busy_timeout_ms)


def test_sqlite_migrations_are_sequential_and_failure_is_atomic(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "migration.db")
    executed: list[int] = []

    def migration_1(connection: sqlite3.Connection) -> None:
        executed.append(1)
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child(parent_id INTEGER NOT NULL REFERENCES parent(id))"
        )

    def failing_migration_2(connection: sqlite3.Connection) -> None:
        executed.append(2)
        connection.execute("CREATE TABLE must_rollback(id INTEGER PRIMARY KEY)")
        raise RuntimeError("injected migration failure")

    database.initialize("test", [migration_1])
    with pytest.raises(RuntimeError, match="injected migration failure"):
        database.initialize("test", [migration_1, failing_migration_2])

    with database.read() as connection:
        version = connection.execute(
            "SELECT version FROM schema_versions WHERE component='test'"
        ).fetchone()[0]
        rolled_back_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='must_rollback'"
        ).fetchone()
    assert version == 1
    assert rolled_back_table is None
    assert executed == [1, 2]

    def migration_2(connection: sqlite3.Connection) -> None:
        executed.append(3)
        connection.execute("CREATE TABLE migrated(id INTEGER PRIMARY KEY)")

    database.initialize("test", [migration_1, migration_2])
    with database.read() as connection:
        assert connection.execute(
            "SELECT version FROM schema_versions WHERE component='test'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrated'"
        ).fetchone() is not None
    assert executed == [1, 2, 3]

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as connection:
            connection.execute("INSERT INTO child(parent_id) VALUES(999)")
