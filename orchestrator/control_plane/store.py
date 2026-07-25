from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from orchestrator.common.db import SQLiteDatabase
from orchestrator.common.enums import (
    AgentOnlineStatus,
    GroupState,
    InstallationState,
    RecoveryRunStatus,
    RecoveryStepStatus,
    RuntimeState,
    RunTrigger,
    StartupState,
)
from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.graph import DependencyCycleError, topological_levels
from orchestrator.common.models import (
    MAX_REASON_ITEMS,
    MAX_SERVICE_ITEMS,
    AgentReport,
    PreconditionIssue,
    ReadinessWrite,
)
from orchestrator.common.time import parse_rfc3339, to_rfc3339, utc_now

from .leases import MonotonicLeaseRegistry


_ACTIVE_RUN_STATUSES = (RecoveryRunStatus.PENDING.value, RecoveryRunStatus.RUNNING.value)
_TERMINAL_RUN_STATUSES = {
    RecoveryRunStatus.SUCCEEDED.value,
    RecoveryRunStatus.FAILED.value,
    RecoveryRunStatus.UNKNOWN.value,
}
_RUN_CURSOR_KEYS = {
    "v",
    "created_at",
    "run_id",
    "group_id",
    "trigger",
    "status",
}


def _invalid_run_cursor() -> ApiError:
    return ApiError(
        422,
        ErrorCode.VALIDATION_ERROR,
        "RecoveryRun cursor is invalid",
    )


def _encode_run_cursor(
    *,
    created_at: str,
    run_id: str,
    group_id: str | None,
    trigger: str | None,
    status: str | None,
) -> str:
    payload = {
        "v": 1,
        "created_at": created_at,
        "run_id": run_id,
        "group_id": group_id,
        "trigger": trigger,
        "status": status,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_run_cursor(
    cursor: str,
    *,
    group_id: str | None,
    trigger: str | None,
    status: str | None,
) -> tuple[str, str]:
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            raise ValueError
        padded = cursor + ("=" * (-len(cursor) % 4))
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != _RUN_CURSOR_KEYS:
            raise ValueError
        if type(payload["v"]) is not int or payload["v"] != 1:
            raise ValueError
        created_at = payload["created_at"]
        run_id = payload["run_id"]
        if not isinstance(created_at, str) or not isinstance(run_id, str):
            raise ValueError
        parsed_at = parse_rfc3339(created_at)
        parsed_run_id = UUID(run_id)
        if (
            parsed_at is None
            or to_rfc3339(parsed_at) != created_at
            or parsed_run_id.version != 4
            or str(parsed_run_id) != run_id
            or payload["group_id"] != group_id
            or payload["trigger"] != trigger
            or payload["status"] != status
            or _encode_run_cursor(
                created_at=created_at,
                run_id=run_id,
                group_id=group_id,
                trigger=trigger,
                status=status,
            )
            != cursor
        ):
            raise ValueError
        return created_at, run_id
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        OverflowError,
        RecursionError,
    ) as exc:
        raise _invalid_run_cursor() from exc


def _migration_v1(connection: sqlite3.Connection) -> None:
    script = """
        CREATE TABLE agents (
            agent_id TEXT PRIMARY KEY,
            boot_id TEXT NOT NULL,
            agent_instance_id TEXT NOT NULL,
            instance_generation INTEGER NOT NULL CHECK(instance_generation >= 1),
            hostname TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            peer_ip TEXT NOT NULL,
            version TEXT NOT NULL,
            last_sequence INTEGER NOT NULL CHECK(last_sequence >= 1),
            last_received_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE services (
            managed_service_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
            local_service_id TEXT NOT NULL,
            windows_service_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            installation_state TEXT NOT NULL,
            runtime_state TEXT NOT NULL,
            startup_state TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            mirrored_at TEXT NOT NULL,
            seen_in_last_report INTEGER NOT NULL DEFAULT 1 CHECK(seen_in_last_report IN (0, 1)),
            UNIQUE(agent_id, local_service_id)
        );
        CREATE INDEX idx_services_agent ON services(agent_id);

        CREATE TABLE recovery_groups (
            group_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            node_settle_window_seconds INTEGER NOT NULL CHECK(node_settle_window_seconds BETWEEN 1 AND 3600),
            max_parallel_services INTEGER NOT NULL CHECK(max_parallel_services BETWEEN 1 AND 4),
            baseline_epoch TEXT,
            candidate_epoch TEXT,
            candidate_stable_since TEXT,
            settle_deadline_at TEXT,
            last_scheduled_epoch TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE group_services (
            group_id TEXT NOT NULL REFERENCES recovery_groups(group_id) ON DELETE CASCADE,
            managed_service_id TEXT NOT NULL REFERENCES services(managed_service_id) ON DELETE RESTRICT,
            PRIMARY KEY(group_id, managed_service_id)
        );
        CREATE INDEX idx_group_services_service ON group_services(managed_service_id);

        CREATE TABLE dependencies (
            group_id TEXT NOT NULL,
            managed_service_id TEXT NOT NULL,
            prerequisite_managed_service_id TEXT NOT NULL,
            PRIMARY KEY(group_id, managed_service_id, prerequisite_managed_service_id),
            CHECK(managed_service_id <> prerequisite_managed_service_id),
            FOREIGN KEY(group_id, managed_service_id)
                REFERENCES group_services(group_id, managed_service_id) ON DELETE CASCADE,
            FOREIGN KEY(group_id, prerequisite_managed_service_id)
                REFERENCES group_services(group_id, managed_service_id) ON DELETE CASCADE
        );

        CREATE TABLE probes (
            probe_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            managed_service_id TEXT NOT NULL,
            definition_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(group_id, managed_service_id),
            FOREIGN KEY(group_id, managed_service_id)
                REFERENCES group_services(group_id, managed_service_id) ON DELETE CASCADE
        );

        CREATE TABLE recovery_runs (
            run_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL REFERENCES recovery_groups(group_id) ON DELETE CASCADE,
            trigger TEXT NOT NULL,
            epoch TEXT,
            retry_of_run_id TEXT REFERENCES recovery_runs(run_id) ON DELETE RESTRICT,
            status TEXT NOT NULL,
            reason TEXT,
            members_snapshot_json TEXT NOT NULL,
            dependencies_snapshot_json TEXT NOT NULL,
            probes_snapshot_json TEXT NOT NULL,
            failure_code TEXT,
            failure_message TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX uq_auto_group_epoch
            ON recovery_runs(group_id, epoch)
            WHERE trigger = 'AUTO';
        CREATE INDEX idx_runs_status ON recovery_runs(status, created_at);

        CREATE TABLE recovery_steps (
            step_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES recovery_runs(run_id) ON DELETE CASCADE,
            managed_service_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            local_service_id TEXT NOT NULL,
            status TEXT NOT NULL,
            topology_level INTEGER NOT NULL CHECK(topology_level >= 0),
            dispatch_idempotency_key TEXT,
            operation_id TEXT,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            root_cause_step_id TEXT REFERENCES recovery_steps(step_id) ON DELETE SET NULL,
            dependency_chain_json TEXT NOT NULL DEFAULT '[]',
            message TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, managed_service_id)
        );
        CREATE INDEX idx_steps_run_status ON recovery_steps(run_id, status, topology_level);

        CREATE TABLE probe_attempts (
            step_id TEXT NOT NULL REFERENCES recovery_steps(step_id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL CHECK(attempt >= 1),
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            result_json TEXT NOT NULL,
            PRIMARY KEY(step_id, attempt)
        );

        CREATE TABLE service_run_locks (
            managed_service_id TEXT PRIMARY KEY REFERENCES services(managed_service_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL REFERENCES recovery_runs(run_id) ON DELETE CASCADE,
            acquired_at TEXT NOT NULL
        );
        CREATE INDEX idx_service_locks_run ON service_run_locks(run_id);

        CREATE TABLE proxy_dispatches (
            idempotency_key TEXT PRIMARY KEY,
            request_fingerprint TEXT NOT NULL,
            managed_service_id TEXT NOT NULL REFERENCES services(managed_service_id) ON DELETE RESTRICT,
            agent_id TEXT NOT NULL,
            local_service_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            action TEXT NOT NULL,
            operation_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE proxy_operations (
            operation_id TEXT PRIMARY KEY,
            managed_service_id TEXT NOT NULL REFERENCES services(managed_service_id) ON DELETE RESTRICT,
            agent_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            operation_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    # sqlite3.executescript() performs an implicit COMMIT and would defeat the
    # transaction opened by SQLiteDatabase.initialize().  The schema contains no
    # triggers or semicolons inside literals, so execute each statement directly.
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _migration_v2(connection: sqlite3.Connection) -> None:
    """Enforce persisted enum values and immutable terminal outcomes."""

    domains: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("services", "installation_state", tuple(item.value for item in InstallationState)),
        ("services", "runtime_state", tuple(item.value for item in RuntimeState)),
        ("services", "startup_state", tuple(item.value for item in StartupState)),
        ("recovery_groups", "state", tuple(item.value for item in GroupState)),
        ("recovery_runs", "trigger", tuple(item.value for item in RunTrigger)),
        ("recovery_runs", "status", tuple(item.value for item in RecoveryRunStatus)),
        ("recovery_steps", "status", tuple(item.value for item in RecoveryStepStatus)),
        ("proxy_dispatches", "action", ("start", "stop", "restart")),
    )
    for table, column, allowed in domains:
        placeholders = ",".join("?" for _ in allowed)
        invalid = connection.execute(
            f"SELECT 1 FROM {table} WHERE {column} NOT IN ({placeholders}) LIMIT 1",
            allowed,
        ).fetchone()
        if invalid is not None:
            raise sqlite3.IntegrityError(
                f"cannot migrate: {table}.{column} contains an invalid value"
            )
        rendered = ",".join("'" + value.replace("'", "''") + "'" for value in allowed)
        for operation in ("INSERT", "UPDATE OF " + column):
            suffix = "insert" if operation == "INSERT" else "update"
            connection.execute(
                f"""
                CREATE TRIGGER enforce_{table}_{column}_{suffix}
                BEFORE {operation} ON {table}
                WHEN NEW.{column} NOT IN ({rendered})
                BEGIN
                    SELECT RAISE(ABORT, 'invalid {table}.{column}');
                END
                """
            )

    terminal_runs = ",".join(f"'{value}'" for value in sorted(_TERMINAL_RUN_STATUSES))
    terminal_steps = ",".join(
        f"'{item.value}'" for item in RecoveryStepStatus if item.terminal
    )
    connection.execute(
        f"""
        CREATE TRIGGER enforce_recovery_runs_terminal_immutable
        BEFORE UPDATE OF status ON recovery_runs
        WHEN OLD.status IN ({terminal_runs}) AND NEW.status <> OLD.status
        BEGIN
            SELECT RAISE(ABORT, 'terminal recovery_runs.status is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER enforce_recovery_runs_no_running_to_pending
        BEFORE UPDATE OF status ON recovery_runs
        WHEN OLD.status = 'RUNNING' AND NEW.status = 'PENDING'
        BEGIN
            SELECT RAISE(ABORT, 'recovery_runs.status cannot return to PENDING');
        END
        """
    )
    connection.execute(
        f"""
        CREATE TRIGGER enforce_recovery_steps_terminal_immutable
        BEFORE UPDATE OF status ON recovery_steps
        WHEN OLD.status IN ({terminal_steps}) AND NEW.status <> OLD.status
        BEGIN
            SELECT RAISE(ABORT, 'terminal recovery_steps.status is immutable');
        END
        """
    )


def _migration_v3(connection: sqlite3.Connection) -> None:
    """Persist why an armed group entered BLOCKED_PRECONDITION."""

    connection.execute(
        "ALTER TABLE recovery_groups ADD COLUMN blocked_reasons_json "
        "TEXT NOT NULL DEFAULT '[]'"
    )
    connection.execute(
        """
        UPDATE recovery_groups
        SET blocked_reasons_json =
            '[{"code":"LEGACY_REASON_UNAVAILABLE",'
            || '"message":"Blocked reason was not persisted before schema v3"}]'
        WHERE state = 'BLOCKED_PRECONDITION'
        """
    )
    for operation, suffix in (("INSERT", "insert"), ("UPDATE OF state, blocked_reasons_json", "update")):
        connection.execute(
            f"""
            CREATE TRIGGER enforce_recovery_groups_blocked_reasons_{suffix}
            BEFORE {operation} ON recovery_groups
            WHEN json_valid(NEW.blocked_reasons_json) = 0
              OR json_type(NEW.blocked_reasons_json) <> 'array'
              OR (
                    NEW.state = 'BLOCKED_PRECONDITION'
                    AND json_array_length(NEW.blocked_reasons_json) < 1
                 )
              OR (
                    NEW.state <> 'BLOCKED_PRECONDITION'
                    AND json_array_length(NEW.blocked_reasons_json) <> 0
                 )
            BEGIN
                SELECT RAISE(ABORT, 'recovery_groups.blocked_reasons invariant failed');
            END
            """
        )


def _uuid4_text_invalid(value_sql: str) -> str:
    """Return a SQLite predicate that rejects non-canonical UUIDv4 text."""

    lowered = f"lower({value_sql})"
    return f"""(
        instr({value_sql}, char(0)) > 0
        OR length({value_sql}) <> 36
        OR length(replace({value_sql}, '-', '')) <> 32
        OR substr({value_sql}, 9, 1) <> '-'
        OR substr({value_sql}, 14, 1) <> '-'
        OR substr({value_sql}, 19, 1) <> '-'
        OR substr({value_sql}, 24, 1) <> '-'
        OR {lowered} GLOB '*[^0-9a-f-]*'
        OR substr({lowered}, 15, 1) <> '4'
        OR substr({lowered}, 20, 1) NOT IN ('8', '9', 'a', 'b')
    )"""


def _blocked_reasons_invalid_predicate(json_sql: str, state_sql: str) -> str:
    """Build the strict PreconditionIssue[] invariant used by migration v4."""

    safe_json = f"(CASE WHEN json_valid({json_sql}) THEN {json_sql} ELSE '[]' END)"
    issue_json = "(CASE WHEN issue.type = 'object' THEN issue.value ELSE '{}' END)"

    def optional_uuid(path: str) -> str:
        item_type = f"json_type({issue_json}, '{path}')"
        item_value = f"json_extract({issue_json}, '{path}')"
        return f"""(
            {item_type} IS NOT NULL
            AND {item_type} <> 'null'
            AND ({item_type} <> 'text' OR {_uuid4_text_invalid(item_value)})
        )"""

    managed_ids_type = f"json_type({issue_json}, '$.managed_service_ids')"
    safe_managed_ids = (
        f"(CASE WHEN {managed_ids_type} = 'array' "
        f"THEN json_extract({issue_json}, '$.managed_service_ids') ELSE '[]' END)"
    )

    return f"""
        json_valid({json_sql}) = 0
        OR typeof({json_sql}) <> 'text'
        OR recovery_blocked_reasons_valid({json_sql}, {state_sql}) <> 1
        OR json_type({safe_json}) <> 'array'
        OR json_array_length({safe_json}) > 100
        OR (
            {state_sql} = 'BLOCKED_PRECONDITION'
            AND json_array_length({safe_json}) < 1
        )
        OR (
            {state_sql} <> 'BLOCKED_PRECONDITION'
            AND json_array_length({safe_json}) <> 0
        )
        OR EXISTS (
            SELECT 1
            FROM json_each({safe_json}) AS issue
            WHERE issue.type <> 'object'
               OR json_type({issue_json}, '$.code') IS NOT 'text'
               OR instr(json_extract({issue_json}, '$.code'), char(0)) > 0
               OR length(json_extract({issue_json}, '$.code')) NOT BETWEEN 1 AND 64
               OR json_type({issue_json}, '$.message') IS NOT 'text'
               OR instr(json_extract({issue_json}, '$.message'), char(0)) > 0
               OR length(json_extract({issue_json}, '$.message')) NOT BETWEEN 1 AND 512
               OR EXISTS (
                    SELECT 1 FROM json_each({issue_json}) AS field
                    WHERE field.key NOT IN (
                        'code', 'message', 'managed_service_id', 'agent_id',
                        'managed_service_ids'
                    )
               )
               OR EXISTS (
                    SELECT 1 FROM json_each({issue_json}) AS field
                    GROUP BY field.key HAVING count(*) > 1
               )
               OR {optional_uuid('$.managed_service_id')}
               OR {optional_uuid('$.agent_id')}
               OR (
                    {managed_ids_type} IS NOT NULL
                    AND {managed_ids_type} <> 'array'
               )
               OR json_array_length({safe_managed_ids}) > 100
               OR EXISTS (
                    SELECT 1 FROM json_each({safe_managed_ids}) AS managed_id
                    WHERE managed_id.type <> 'text'
                       OR {_uuid4_text_invalid('managed_id.value')}
               )
               OR EXISTS (
                    SELECT 1 FROM json_each({safe_managed_ids}) AS managed_id
                    GROUP BY lower(managed_id.value) HAVING count(*) > 1
               )
        )
    """


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _validate_blocked_reasons(
    raw: object, state: object
) -> list[PreconditionIssue]:
    if not isinstance(raw, str) or not isinstance(state, str):
        raise ValueError("blocked_reasons storage must be text")
    value = json.loads(raw, object_pairs_hook=_strict_json_object)
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("blocked_reasons must be an array with at most 100 items")
    if state == GroupState.BLOCKED_PRECONDITION.value:
        if not value:
            raise ValueError("blocked group requires at least one reason")
    elif value:
        raise ValueError("non-blocked group must not contain blocked reasons")
    return [PreconditionIssue.model_validate(item) for item in value]


def _sqlite_blocked_reasons_valid(raw: object, state: object) -> int:
    try:
        _validate_blocked_reasons(raw, state)
    except (TypeError, ValueError, UnicodeError):
        return 0
    return 1


def _normalize_blocked_reasons_for_migration(raw: object, state: object) -> str:
    issues = _validate_blocked_reasons(raw, state)
    return json.dumps(
        [issue.model_dump(mode="json", exclude_unset=True) for issue in issues],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _migration_v4(connection: sqlite3.Connection) -> None:
    """Enforce the complete persisted PreconditionIssue[] public contract."""

    rows = connection.execute(
        "SELECT group_id, state, blocked_reasons_json FROM recovery_groups"
    ).fetchall()
    try:
        normalized = [
            (
                _normalize_blocked_reasons_for_migration(
                    row["blocked_reasons_json"], row["state"]
                ),
                str(row["group_id"]),
            )
            for row in rows
        ]
    except (TypeError, ValueError, UnicodeError) as exc:
        raise sqlite3.IntegrityError(
            "cannot migrate: recovery_groups.blocked_reasons_json is invalid"
        ) from exc
    connection.executemany(
        "UPDATE recovery_groups SET blocked_reasons_json=? WHERE group_id=?",
        normalized,
    )

    for suffix in ("insert", "update"):
        connection.execute(
            f"DROP TRIGGER enforce_recovery_groups_blocked_reasons_{suffix}"
        )

    predicate = _blocked_reasons_invalid_predicate(
        "NEW.blocked_reasons_json", "NEW.state"
    )
    for operation, suffix in (
        ("INSERT", "insert"),
        ("UPDATE OF state, blocked_reasons_json", "update"),
    ):
        connection.execute(
            f"""
            CREATE TRIGGER enforce_recovery_groups_blocked_reasons_{suffix}
            BEFORE {operation} ON recovery_groups
            WHEN {predicate}
            BEGIN
                SELECT RAISE(ABORT, 'recovery_groups.blocked_reasons invariant failed');
            END
            """
        )

    # Exercise the new UPDATE trigger against every existing v3 row. A dirty
    # database aborts this migration and SQLiteDatabase rolls back both trigger
    # replacement and the schema version change atomically.
    connection.execute(
        "UPDATE recovery_groups SET blocked_reasons_json=blocked_reasons_json"
    )


def _migration_v5(connection: sqlite3.Connection) -> None:
    """Reject legacy databases that cannot satisfy public collection schemas."""

    checks = (
        ("agents", "SELECT COUNT(*) FROM agents"),
        (
            "active managed services",
            "SELECT COUNT(*) FROM services WHERE seen_in_last_report=1",
        ),
        ("recovery groups", "SELECT COUNT(*) FROM recovery_groups"),
    )
    for label, query in checks:
        count = int(connection.execute(query).fetchone()[0])
        if count > MAX_SERVICE_ITEMS:
            raise sqlite3.IntegrityError(
                f"cannot migrate: {label} exceed public collection capacity "
                f"{MAX_SERVICE_ITEMS}"
            )
    connection.execute(
        """
        CREATE INDEX idx_services_active_agent_local
        ON services(agent_id, local_service_id)
        WHERE seen_in_last_report=1
        """
    )


def _enum_value(enum_type: type, value: Any, *, label: str) -> str:
    raw = value.value if isinstance(value, enum_type) else str(value)
    try:
        return enum_type(raw).value
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {raw}") from exc


def _model_dict(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def _json_dump(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        to_jsonable_python(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bounded_precondition_issues(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(issues) <= MAX_REASON_ITEMS:
        return issues
    ordered = sorted(
        issues,
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("agent_id") or ""),
            str(item.get("managed_service_id") or ""),
            tuple(str(value) for value in item.get("managed_service_ids") or []),
            str(item.get("message") or ""),
        ),
    )
    retained = MAX_REASON_ITEMS - 1
    omitted = len(ordered) - retained
    return [
        *ordered[:retained],
        {
            "code": "PRECONDITION_ISSUES_TRUNCATED",
            "message": f"{omitted} additional precondition issues were omitted",
        },
    ]


def _canonical_endpoint(reported_endpoint: str, peer: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(reported_endpoint)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("",)
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
            or parsed.port is None
        ):
            raise ValueError
        reported_ip = ipaddress.ip_address(parsed.hostname)
        peer_ip = ipaddress.ip_address(peer)
    except (ValueError, TypeError) as exc:
        raise ApiError(
            409,
            ErrorCode.ENDPOINT_SOURCE_MISMATCH,
            "Agent endpoint must be an HTTP IP literal with an explicit port",
        ) from exc
    if reported_ip != peer_ip:
        raise ApiError(
            409,
            ErrorCode.ENDPOINT_SOURCE_MISMATCH,
            "Agent endpoint address does not match the socket peer",
        )
    host = f"[{peer_ip.compressed}]" if peer_ip.version == 6 else peer_ip.compressed
    return f"http://{host}:{parsed.port}", peer_ip.compressed


class ControlPlaneStore:
    """Durable, transaction-oriented Control Plane state boundary."""

    def __init__(
        self,
        database: SQLiteDatabase | str,
        *,
        offline_after_seconds: int = 45,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        self.db.register_function(
            "recovery_blocked_reasons_valid",
            2,
            _sqlite_blocked_reasons_valid,
            deterministic=True,
        )
        self.offline_after_seconds = offline_after_seconds
        self._now = now
        self._leases = MonotonicLeaseRegistry(
            offline_after_seconds, monotonic=monotonic
        )
        self.db.initialize(
            "control-plane",
            [_migration_v1, _migration_v2, _migration_v3, _migration_v4, _migration_v5],
        )

    def _timestamp(self, value: datetime | None = None) -> str:
        return to_rfc3339(value or self._now()) or ""

    def _cutoff(self, now: datetime | None = None) -> str:
        return self._timestamp((now or self._now()) - timedelta(seconds=self.offline_after_seconds))

    # ------------------------------------------------------------------
    # Agent ingress and mirrored inventory

    def register_agent(
        self,
        report: AgentReport | Mapping[str, Any],
        peer_ip: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        parsed = report if isinstance(report, AgentReport) else AgentReport.model_validate(report)
        endpoint, canonical_peer = _canonical_endpoint(parsed.endpoint, peer_ip)
        received_at = self._timestamp(now)
        lease_was_online = self._leases.is_online(parsed.agent_id)
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agents WHERE agent_id=?", (str(parsed.agent_id),)
            ).fetchone()
            if row is None:
                agent_count = int(
                    connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
                )
                if agent_count >= MAX_SERVICE_ITEMS:
                    raise ApiError(
                        422,
                        ErrorCode.VALIDATION_ERROR,
                        "Control Plane Agent capacity was reached",
                    )
            if row is not None:
                generation = int(row["instance_generation"])
                same_instance = row["agent_instance_id"] == str(parsed.agent_instance_id)
                if parsed.instance_generation < generation or (
                    parsed.instance_generation == generation and not same_instance
                ):
                    raise ApiError(
                        409,
                        ErrorCode.STALE_AGENT_INSTANCE,
                        "Agent process instance is stale",
                    )
                if (
                    parsed.instance_generation == generation
                    and same_instance
                    and parsed.sequence <= int(row["last_sequence"])
                ):
                    return self._heartbeat_ack(received_at, ignored=True)
            self._write_accepted_report(
                connection,
                parsed,
                endpoint,
                canonical_peer,
                received_at,
                is_new=row is None,
                lease_was_expired=row is not None and not lease_was_online,
            )
        self._leases.renew(parsed.agent_id)
        return self._heartbeat_ack(received_at, ignored=False)

    def ingest_heartbeat(
        self,
        agent_id: UUID | str,
        report: AgentReport | Mapping[str, Any],
        peer_ip: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        parsed = report if isinstance(report, AgentReport) else AgentReport.model_validate(report)
        if str(parsed.agent_id) != str(agent_id):
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, "Path agent_id does not match body")
        endpoint, canonical_peer = _canonical_endpoint(parsed.endpoint, peer_ip)
        received_at = self._timestamp(now)
        lease_was_expired = not self._leases.is_online(parsed.agent_id)
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agents WHERE agent_id=?", (str(parsed.agent_id),)
            ).fetchone()
            if row is None:
                raise ApiError(404, ErrorCode.AGENT_NOT_FOUND, "Agent is not registered")
            if (
                int(row["instance_generation"]) != parsed.instance_generation
                or row["agent_instance_id"] != str(parsed.agent_instance_id)
            ):
                raise ApiError(409, ErrorCode.STALE_AGENT_INSTANCE, "Agent process instance is stale")
            if parsed.sequence <= int(row["last_sequence"]):
                return self._heartbeat_ack(received_at, ignored=True)
            self._write_accepted_report(
                connection,
                parsed,
                endpoint,
                canonical_peer,
                received_at,
                is_new=False,
                lease_was_expired=lease_was_expired,
            )
        self._leases.renew(parsed.agent_id)
        return self._heartbeat_ack(received_at, ignored=False)

    heartbeat = ingest_heartbeat

    @staticmethod
    def _heartbeat_ack(received_at: str, *, ignored: bool) -> dict[str, Any]:
        return {
            "accepted": not ignored,
            "ignored": ignored,
            "reason": "DUPLICATE_OR_OUT_OF_ORDER" if ignored else "ACCEPTED",
            "received_at": received_at,
            "lease_updated_at": None if ignored else received_at,
        }

    def _write_accepted_report(
        self,
        connection: sqlite3.Connection,
        report: AgentReport,
        endpoint: str,
        peer_ip: str,
        received_at: str,
        *,
        is_new: bool,
        lease_was_expired: bool,
    ) -> None:
        other_active_services = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM services
                WHERE seen_in_last_report=1 AND agent_id<>?
                """,
                (str(report.agent_id),),
            ).fetchone()[0]
        )
        if other_active_services + len(report.services) > MAX_SERVICE_ITEMS:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "Control Plane managed service capacity was reached",
            )
        if is_new:
            connection.execute(
                """
                INSERT INTO agents(
                    agent_id, boot_id, agent_instance_id, instance_generation, hostname,
                    endpoint, peer_ip, version, last_sequence, last_received_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(report.agent_id),
                    str(report.boot_id),
                    str(report.agent_instance_id),
                    report.instance_generation,
                    report.hostname,
                    endpoint,
                    peer_ip,
                    report.version,
                    report.sequence,
                    received_at,
                    received_at,
                    received_at,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE agents SET boot_id=?, agent_instance_id=?, instance_generation=?,
                    hostname=?, endpoint=?, peer_ip=?, version=?, last_sequence=?,
                    last_received_at=?, updated_at=? WHERE agent_id=?
                """,
                (
                    str(report.boot_id),
                    str(report.agent_instance_id),
                    report.instance_generation,
                    report.hostname,
                    endpoint,
                    peer_ip,
                    report.version,
                    report.sequence,
                    received_at,
                    received_at,
                    str(report.agent_id),
                ),
            )
        connection.execute(
            "UPDATE services SET seen_in_last_report=0 WHERE agent_id=?", (str(report.agent_id),)
        )
        for service in report.services:
            connection.execute(
                """
                INSERT INTO services(
                    managed_service_id, agent_id, local_service_id, windows_service_name,
                    display_name, installation_state, runtime_state, startup_state,
                    last_observed_at, mirrored_at, seen_in_last_report
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(agent_id, local_service_id) DO UPDATE SET
                    windows_service_name=excluded.windows_service_name,
                    display_name=excluded.display_name,
                    installation_state=excluded.installation_state,
                    runtime_state=excluded.runtime_state,
                    startup_state=excluded.startup_state,
                    last_observed_at=excluded.last_observed_at,
                    mirrored_at=excluded.mirrored_at,
                    seen_in_last_report=1
                """,
                (
                    str(uuid4()),
                    str(report.agent_id),
                    service.local_service_id,
                    service.windows_service_name,
                    service.display_name,
                    service.installation_state.value,
                    service.runtime_state.value,
                    service.startup_state.value,
                    self._timestamp(service.last_observed_at),
                    received_at,
                ),
            )
        if lease_was_expired:
            # If the CP was down or could not hear this Agent for a full lease,
            # a persisted settle interval cannot be considered continuous.  This
            # reset happens in the same transaction as lease renewal so a restart
            # can never skip the fresh settle window.
            connection.execute(
                """
                UPDATE recovery_groups AS g
                SET state=?, candidate_epoch=NULL, candidate_stable_since=NULL,
                    settle_deadline_at=NULL, blocked_reasons_json='[]', updated_at=?
                WHERE g.state IN (?, ?, ?)
                  AND EXISTS (
                    SELECT 1 FROM group_services gs
                    JOIN services s ON s.managed_service_id=gs.managed_service_id
                    WHERE gs.group_id=g.group_id AND s.agent_id=?
                  )
                """,
                (
                    GroupState.WAITING_FOR_NODES.value,
                    received_at,
                    GroupState.ARMED_IDLE.value,
                    GroupState.WAITING_FOR_NODES.value,
                    GroupState.SETTLING.value,
                    str(report.agent_id),
                ),
            )

    def list_agents(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            rows = connection.execute("SELECT * FROM agents ORDER BY hostname, agent_id").fetchall()
        return [self._agent_dict(row) for row in rows]

    def get_agent(
        self, agent_id: UUID | str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM agents WHERE agent_id=?", (str(agent_id),)
            ).fetchone()
        return None if row is None else self._agent_dict(row)

    def _agent_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "agent_id": row["agent_id"],
            "boot_id": row["boot_id"],
            "agent_instance_id": row["agent_instance_id"],
            "instance_generation": row["instance_generation"],
            "hostname": row["hostname"],
            "endpoint": row["endpoint"],
            "version": row["version"],
            "online_status": (
                AgentOnlineStatus.ONLINE.value
                if self._leases.is_online(row["agent_id"])
                else AgentOnlineStatus.OFFLINE.value
            ),
            "last_sequence": row["last_sequence"],
            "last_received_at": row["last_received_at"],
            "offline_after_seconds": self.offline_after_seconds,
        }

    def list_services(
        self, *, now: datetime | None = None, include_unreported: bool = False
    ) -> list[dict[str, Any]]:
        where = "" if include_unreported else "WHERE s.seen_in_last_report=1"
        with self.db.read() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*, a.last_received_at, a.endpoint, a.hostname
                FROM services s JOIN agents a ON a.agent_id=s.agent_id
                {where}
                ORDER BY a.hostname, s.local_service_id
                """
            ).fetchall()
        internal_fields = {"endpoint", "hostname", "seen_in_last_report"}
        return [
            {key: value for key, value in self._service_dict(row).items() if key not in internal_fields}
            for row in rows
        ]

    def get_service(
        self,
        managed_service_id: UUID | str,
        *,
        now: datetime | None = None,
        include_unreported: bool = True,
    ) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(
                """
                SELECT s.*, a.last_received_at, a.endpoint, a.hostname
                FROM services s JOIN agents a ON a.agent_id=s.agent_id
                WHERE s.managed_service_id=?
                """,
                (str(managed_service_id),),
            ).fetchone()
        if row is None or (not include_unreported and not row["seen_in_last_report"]):
            return None
        return self._service_dict(row)

    def _service_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "managed_service_id": row["managed_service_id"],
            "agent_id": row["agent_id"],
            "agent_online_status": (
                AgentOnlineStatus.ONLINE.value
                if self._leases.is_online(row["agent_id"])
                else AgentOnlineStatus.OFFLINE.value
            ),
            "local_service_id": row["local_service_id"],
            "windows_service_name": row["windows_service_name"],
            "display_name": row["display_name"],
            "installation_state": row["installation_state"],
            "runtime_state": row["runtime_state"],
            "startup_state": row["startup_state"],
            "last_observed_at": row["last_observed_at"],
            "mirrored_at": row["mirrored_at"],
            "endpoint": row["endpoint"],
            "hostname": row["hostname"],
            "seen_in_last_report": bool(row["seen_in_last_report"]),
        }

    # ------------------------------------------------------------------
    # Recovery group configuration

    def create_group(self, value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
        data = _model_dict(value)
        group_id = str(uuid4())
        now = self._timestamp()
        with self.db.transaction() as connection:
            group_count = int(
                connection.execute("SELECT COUNT(*) FROM recovery_groups").fetchone()[0]
            )
            if group_count >= MAX_SERVICE_ITEMS:
                raise ApiError(
                    422,
                    ErrorCode.VALIDATION_ERROR,
                    "Recovery group capacity was reached",
                )
            connection.execute(
                """
                INSERT INTO recovery_groups(
                    group_id, name, description, state, node_settle_window_seconds,
                    max_parallel_services, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    data["name"],
                    data.get("description") or "",
                    GroupState.DISARMED.value,
                    int(data.get("node_settle_window_seconds", 120)),
                    int(data.get("max_parallel_services", 4)),
                    now,
                    now,
                ),
            )
        return self.get_group(group_id)  # type: ignore[return-value]

    create_recovery_group = create_group

    def update_group(
        self, group_id: UUID | str, value: BaseModel | Mapping[str, Any]
    ) -> dict[str, Any]:
        data = _model_dict(value)
        with self.db.transaction() as connection:
            self._require_mutable_group(connection, str(group_id))
            allowed = {
                "name",
                "description",
                "node_settle_window_seconds",
                "max_parallel_services",
            }
            updates = {key: item for key, item in data.items() if key in allowed and item is not None}
            if not updates:
                raise ApiError(422, ErrorCode.VALIDATION_ERROR, "No group fields were supplied")
            columns = ", ".join(f"{key}=?" for key in updates)
            connection.execute(
                f"UPDATE recovery_groups SET {columns}, updated_at=? WHERE group_id=?",
                (*updates.values(), self._timestamp(), str(group_id)),
            )
        return self.get_group(group_id)  # type: ignore[return-value]

    update_recovery_group = update_group

    def delete_group(self, group_id: UUID | str) -> None:
        with self.db.transaction() as connection:
            self._require_mutable_group(connection, str(group_id))
            connection.execute("DELETE FROM recovery_groups WHERE group_id=?", (str(group_id),))

    delete_recovery_group = delete_group

    def replace_members(
        self, group_id: UUID | str, managed_service_ids: Iterable[UUID | str]
    ) -> dict[str, Any]:
        requested = {str(item) for item in managed_service_ids}
        if not requested:
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, "A recovery group needs members")
        with self.db.transaction() as connection:
            self._require_mutable_group(connection, str(group_id))
            existing = {
                row["managed_service_id"]
                for row in connection.execute(
                    "SELECT managed_service_id FROM group_services WHERE group_id=?",
                    (str(group_id),),
                ).fetchall()
            }
            placeholders = ",".join("?" for _ in requested)
            rows = connection.execute(
                f"""
                SELECT managed_service_id, seen_in_last_report
                FROM services
                WHERE managed_service_id IN ({placeholders})
                """,
                tuple(sorted(requested)),
            ).fetchall()
            found = {row["managed_service_id"] for row in rows}
            if found != requested:
                raise ApiError(
                    404,
                    ErrorCode.SERVICE_NOT_ALLOWLISTED,
                    "One or more managed services do not exist",
                    {"missing": sorted(requested - found)},
                )
            unreported_additions = {
                row["managed_service_id"]
                for row in rows
                if not row["seen_in_last_report"] and row["managed_service_id"] not in existing
            }
            if unreported_additions:
                raise ApiError(
                    404,
                    ErrorCode.SERVICE_NOT_ALLOWLISTED,
                    "One or more managed services are not currently allowlisted",
                    {"unreported": sorted(unreported_additions)},
                )
            for item in existing - requested:
                connection.execute(
                    "DELETE FROM group_services WHERE group_id=? AND managed_service_id=?",
                    (str(group_id), item),
                )
            for item in requested - existing:
                connection.execute(
                    "INSERT INTO group_services(group_id, managed_service_id) VALUES(?, ?)",
                    (str(group_id), item),
                )
            connection.execute(
                "UPDATE recovery_groups SET updated_at=? WHERE group_id=?",
                (self._timestamp(), str(group_id)),
            )
        return self.get_group(group_id)  # type: ignore[return-value]

    replace_group_members = replace_members

    def replace_dependencies(
        self,
        group_id: UUID | str,
        dependencies: Iterable[BaseModel | Mapping[str, Any] | tuple[Any, Any]],
    ) -> dict[str, Any]:
        edges: list[tuple[str, str]] = []
        for item in dependencies:
            if isinstance(item, tuple):
                dependent, prerequisite = item
            else:
                data = _model_dict(item)
                dependent = data["managed_service_id"]
                prerequisite = data["prerequisite_managed_service_id"]
            edges.append((str(dependent), str(prerequisite)))
        if len(edges) != len(set(edges)):
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, "Dependency edges must be unique")
        with self.db.transaction() as connection:
            self._require_mutable_group(connection, str(group_id))
            members = self._member_ids(connection, str(group_id))
            try:
                topological_levels(members, edges)
            except DependencyCycleError as exc:
                raise ApiError(
                    409,
                    ErrorCode.DEPENDENCY_CYCLE,
                    "Recovery dependency graph contains a cycle",
                    {"managed_service_ids": [str(item) for item in exc.nodes]},
                ) from exc
            except ValueError as exc:
                raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from exc
            connection.execute("DELETE FROM dependencies WHERE group_id=?", (str(group_id),))
            connection.executemany(
                """
                INSERT INTO dependencies(
                    group_id, managed_service_id, prerequisite_managed_service_id
                ) VALUES(?, ?, ?)
                """,
                [(str(group_id), dependent, prerequisite) for dependent, prerequisite in edges],
            )
            connection.execute(
                "UPDATE recovery_groups SET updated_at=? WHERE group_id=?",
                (self._timestamp(), str(group_id)),
            )
        return self.get_group(group_id)  # type: ignore[return-value]

    replace_group_dependencies = replace_dependencies

    def put_probe(
        self,
        group_id: UUID | str,
        managed_service_id: UUID | str,
        definition: BaseModel | Mapping[str, Any],
    ) -> dict[str, Any]:
        # Pydantic's discriminated union is validated through TypeAdapter lazily to
        # avoid creating a second public model.
        from pydantic import TypeAdapter

        validated = TypeAdapter(ReadinessWrite).validate_python(_model_dict(definition))
        self._validate_readiness_target(validated.model_dump(mode="json"))
        now = self._timestamp()
        with self.db.transaction() as connection:
            self._require_mutable_group(connection, str(group_id))
            if str(managed_service_id) not in self._member_ids(connection, str(group_id)):
                raise ApiError(404, ErrorCode.SERVICE_NOT_ALLOWLISTED, "Service is not a group member")
            row = connection.execute(
                "SELECT probe_id, created_at FROM probes WHERE group_id=? AND managed_service_id=?",
                (str(group_id), str(managed_service_id)),
            ).fetchone()
            probe_id = row["probe_id"] if row else str(uuid4())
            created_at = row["created_at"] if row else now
            connection.execute(
                """
                INSERT INTO probes(
                    probe_id, group_id, managed_service_id, definition_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, managed_service_id) DO UPDATE SET
                    definition_json=excluded.definition_json, updated_at=excluded.updated_at
                """,
                (
                    probe_id,
                    str(group_id),
                    str(managed_service_id),
                    _json_dump(validated),
                    created_at,
                    now,
                ),
            )
            connection.execute(
                "UPDATE recovery_groups SET updated_at=? WHERE group_id=?", (now, str(group_id))
            )
        return self.get_probe(group_id, managed_service_id)  # type: ignore[return-value]

    set_probe = put_probe

    @staticmethod
    def _validate_readiness_target(definition: Mapping[str, Any]) -> None:
        kind = definition["kind"]
        if kind == "scm":
            return
        if kind == "tcp":
            host = str(definition["host"])
        else:
            parsed = urlsplit(str(definition["url"]))
            if (
                parsed.scheme != "http"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.hostname is None
                or parsed.fragment
            ):
                raise ApiError(
                    422,
                    ErrorCode.PROBE_TARGET_DENIED,
                    "HTTP readiness target is not allowed",
                )
            host = parsed.hostname
        if host.lower() == "localhost":
            return
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ApiError(
                422,
                ErrorCode.PROBE_TARGET_DENIED,
                "Readiness target must be localhost or an IP literal",
            ) from exc

    def get_probe(
        self, group_id: UUID | str, managed_service_id: UUID | str
    ) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM probes WHERE group_id=? AND managed_service_id=?",
                (str(group_id), str(managed_service_id)),
            ).fetchone()
        return None if row is None else self._probe_dict(row)

    def delete_probe(self, group_id: UUID | str, managed_service_id: UUID | str) -> None:
        with self.db.transaction() as connection:
            self._require_mutable_group(connection, str(group_id))
            if str(managed_service_id) not in self._member_ids(connection, str(group_id)):
                raise ApiError(404, ErrorCode.SERVICE_NOT_ALLOWLISTED, "Service is not a group member")
            connection.execute(
                "DELETE FROM probes WHERE group_id=? AND managed_service_id=?",
                (str(group_id), str(managed_service_id)),
            )
            connection.execute(
                "UPDATE recovery_groups SET updated_at=? WHERE group_id=?",
                (self._timestamp(), str(group_id)),
            )

    def list_groups(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            ids = [row["group_id"] for row in connection.execute(
                "SELECT group_id FROM recovery_groups ORDER BY name, group_id"
            ).fetchall()]
        return [item for item in (self.get_group(group_id, now=now) for group_id in ids) if item]

    def get_group(
        self, group_id: UUID | str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_groups WHERE group_id=?", (str(group_id),)
            ).fetchone()
            if row is None:
                return None
            members = self._members_snapshot(connection, str(group_id))
            dependencies = self._dependencies_snapshot(connection, str(group_id))
            probes = self._probes_snapshot(connection, str(group_id))
            missing = self._missing_agent_ids(connection, str(group_id))
        return {
            "group_id": row["group_id"],
            "name": row["name"],
            "description": row["description"],
            "state": row["state"],
            "node_settle_window_seconds": row["node_settle_window_seconds"],
            "max_parallel_services": row["max_parallel_services"],
            "baseline_epoch": row["baseline_epoch"],
            "candidate_epoch": row["candidate_epoch"],
            "candidate_stable_since": row["candidate_stable_since"],
            "settle_deadline_at": row["settle_deadline_at"],
            "last_scheduled_epoch": row["last_scheduled_epoch"],
            "blocked_reasons": json.loads(row["blocked_reasons_json"]),
            "missing_agent_ids": missing,
            "members": members,
            "dependencies": dependencies,
            "probes": probes,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _require_group(self, connection: sqlite3.Connection, group_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM recovery_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, ErrorCode.GROUP_NOT_READY, "Recovery group was not found")
        return row

    def _require_mutable_group(self, connection: sqlite3.Connection, group_id: str) -> sqlite3.Row:
        row = self._require_group(connection, group_id)
        active = connection.execute(
            "SELECT 1 FROM recovery_runs WHERE group_id=? AND status IN (?, ?) LIMIT 1",
            (group_id, *_ACTIVE_RUN_STATUSES),
        ).fetchone()
        if row["state"] != GroupState.DISARMED.value or active is not None:
            raise ApiError(
                409,
                ErrorCode.GROUP_NOT_READY,
                "Only a DISARMED group without an active run may be changed",
            )
        return row

    @staticmethod
    def _member_ids(connection: sqlite3.Connection, group_id: str) -> set[str]:
        return {
            row["managed_service_id"]
            for row in connection.execute(
                "SELECT managed_service_id FROM group_services WHERE group_id=?", (group_id,)
            ).fetchall()
        }

    @staticmethod
    def _members_snapshot(connection: sqlite3.Connection, group_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT s.managed_service_id, s.agent_id, s.local_service_id,
                   s.windows_service_name, s.display_name
            FROM group_services gs
            JOIN services s ON s.managed_service_id=gs.managed_service_id
            WHERE gs.group_id=? ORDER BY s.managed_service_id
            """,
            (group_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _dependencies_snapshot(
        connection: sqlite3.Connection, group_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT managed_service_id, prerequisite_managed_service_id
            FROM dependencies WHERE group_id=?
            ORDER BY managed_service_id, prerequisite_managed_service_id
            """,
            (group_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _probe_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "probe_id": row["probe_id"],
            "group_id": row["group_id"],
            "managed_service_id": row["managed_service_id"],
            "definition": json.loads(row["definition_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _probes_snapshot(
        self, connection: sqlite3.Connection, group_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._probe_dict(row)
            for row in connection.execute(
                "SELECT * FROM probes WHERE group_id=? ORDER BY managed_service_id", (group_id,)
            ).fetchall()
        ]

    def _missing_agent_ids(
        self, connection: sqlite3.Connection, group_id: str, cutoff: str | None = None
    ) -> list[str]:
        del cutoff
        rows = connection.execute(
            """
            SELECT DISTINCT a.agent_id
            FROM group_services gs
            JOIN services s ON s.managed_service_id=gs.managed_service_id
            JOIN agents a ON a.agent_id=s.agent_id
            WHERE gs.group_id=?
            ORDER BY a.agent_id
            """,
            (group_id,),
        ).fetchall()
        return [
            row["agent_id"]
            for row in rows
            if not self._leases.is_online(row["agent_id"])
        ]

    # ------------------------------------------------------------------
    # Arm, preflight, epoch and settle state

    def required_agent_view(
        self, group_id: UUID | str, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            self._require_group(connection, str(group_id))
            rows = connection.execute(
                """
                SELECT DISTINCT a.agent_id, a.boot_id, a.agent_instance_id,
                       a.instance_generation, a.endpoint, a.last_received_at
                FROM group_services gs
                JOIN services s ON s.managed_service_id=gs.managed_service_id
                JOIN agents a ON a.agent_id=s.agent_id
                WHERE gs.group_id=? ORDER BY a.agent_id
                """,
                (str(group_id),),
            ).fetchall()
        return [
            {
                **dict(row),
                "online_status": (
                    AgentOnlineStatus.ONLINE.value
                    if self._leases.is_online(row["agent_id"])
                    else AgentOnlineStatus.OFFLINE.value
                ),
            }
            for row in rows
        ]

    def compute_group_epoch(self, group_id: UUID | str) -> str:
        with self.db.read() as connection:
            self._require_group(connection, str(group_id))
            return self._compute_epoch(connection, str(group_id))

    @staticmethod
    def _compute_epoch(connection: sqlite3.Connection, group_id: str) -> str:
        rows = connection.execute(
            """
            SELECT DISTINCT a.agent_id, a.boot_id
            FROM group_services gs
            JOIN services s ON s.managed_service_id=gs.managed_service_id
            JOIN agents a ON a.agent_id=s.agent_id
            WHERE gs.group_id=? ORDER BY a.agent_id
            """,
            (group_id,),
        ).fetchall()
        if not rows:
            raise ApiError(409, ErrorCode.GROUP_NOT_READY, "Recovery group has no required nodes")
        body = "recovery-mvp-v1\n" + f"group_id={group_id.lower()}\n"
        body += "".join(
            f"agent_id={row['agent_id'].lower()};boot_id={row['boot_id'].lower()}\n"
            for row in rows
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def validate_group_preconditions(
        self, group_id: UUID | str, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        cutoff = self._cutoff(now)
        with self.db.read() as connection:
            self._require_group(connection, str(group_id))
            return self._preflight(connection, str(group_id), cutoff)

    preflight_group = validate_group_preconditions

    def _preflight(
        self,
        connection: sqlite3.Connection,
        group_id: str,
        cutoff: str,
        *,
        service_ids: set[str] | None = None,
        dependencies: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        ids = service_ids if service_ids is not None else self._member_ids(connection, group_id)
        if not ids:
            return [{"code": "GROUP_EMPTY", "message": "Recovery group has no services"}]
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""
            SELECT s.*, a.last_received_at
            FROM services s JOIN agents a ON a.agent_id=s.agent_id
            WHERE s.managed_service_id IN ({placeholders})
            """,
            tuple(sorted(ids)),
        ).fetchall()
        issues: list[dict[str, Any]] = []
        found = {row["managed_service_id"] for row in rows}
        for missing in sorted(ids - found):
            issues.append(
                {"code": "SERVICE_MISSING", "managed_service_id": missing, "message": "Service is missing"}
            )
        offline_agents: set[str] = set()
        for row in rows:
            common = {
                "managed_service_id": row["managed_service_id"],
                "agent_id": row["agent_id"],
            }
            if not self._leases.is_online(row["agent_id"]) and row["agent_id"] not in offline_agents:
                offline_agents.add(row["agent_id"])
                issues.append({**common, "code": "AGENT_OFFLINE", "message": "Required agent is offline"})
            if not row["seen_in_last_report"]:
                issues.append(
                    {**common, "code": "SERVICE_NOT_REPORTED", "message": "Service left the Agent allowlist"}
                )
            if row["installation_state"] != "INSTALLED":
                issues.append(
                    {**common, "code": "SERVICE_NOT_INSTALLED", "message": "Service is not installed"}
                )
            if row["startup_state"] != "AUTOSTART_DISABLED":
                issues.append(
                    {**common, "code": "STARTUP_NOT_MANUAL", "message": "Service startup must be manual"}
                )
        graph = dependencies if dependencies is not None else self._dependencies_snapshot(connection, group_id)
        edges = [
            (item["managed_service_id"], item["prerequisite_managed_service_id"])
            for item in graph
        ]
        try:
            topological_levels(ids, edges)
        except DependencyCycleError as exc:
            issues.append(
                {
                    "code": ErrorCode.DEPENDENCY_CYCLE.value,
                    "message": "Dependency graph contains a cycle",
                    "managed_service_ids": [str(item) for item in exc.nodes],
                }
            )
        except ValueError as exc:
            issues.append({"code": "INVALID_GRAPH", "message": str(exc)})
        return _bounded_precondition_issues(issues)

    def arm_group(
        self, group_id: UUID | str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        at = now or self._now()
        timestamp = self._timestamp(at)
        cutoff = self._cutoff(at)
        with self.db.transaction() as connection:
            row = self._require_group(connection, str(group_id))
            if row["state"] != GroupState.DISARMED.value:
                return self.get_group(group_id, now=at)  # type: ignore[return-value]
            issues = self._preflight(connection, str(group_id), cutoff)
            if issues:
                raise ApiError(
                    409,
                    ErrorCode.GROUP_NOT_READY,
                    "Recovery group failed arm preconditions",
                    {"errors": issues},
                )
            epoch = self._compute_epoch(connection, str(group_id))
            connection.execute(
                """
                UPDATE recovery_groups SET state=?, baseline_epoch=?, candidate_epoch=NULL,
                    candidate_stable_since=NULL, settle_deadline_at=NULL,
                    last_scheduled_epoch=NULL, blocked_reasons_json='[]',
                    updated_at=? WHERE group_id=?
                """,
                (GroupState.ARMED_IDLE.value, epoch, timestamp, str(group_id)),
            )
        return self.get_group(group_id, now=at)  # type: ignore[return-value]

    arm = arm_group

    def disarm_group(
        self, group_id: UUID | str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        timestamp = self._timestamp(now)
        with self.db.transaction() as connection:
            self._require_group(connection, str(group_id))
            connection.execute(
                """
                UPDATE recovery_groups SET state=?, baseline_epoch=NULL, candidate_epoch=NULL,
                    candidate_stable_since=NULL, settle_deadline_at=NULL,
                    last_scheduled_epoch=NULL, blocked_reasons_json='[]',
                    updated_at=? WHERE group_id=?
                """,
                (GroupState.DISARMED.value, timestamp, str(group_id)),
            )
        return self.get_group(group_id, now=now)  # type: ignore[return-value]

    disarm = disarm_group

    def evaluate_group_candidate(
        self, group_id: UUID | str, now: datetime | None = None
    ) -> dict[str, Any]:
        at = now or self._now()
        timestamp = self._timestamp(at)
        cutoff = self._cutoff(at)
        with self.db.transaction() as connection:
            row = self._require_group(connection, str(group_id))
            state = row["state"]
            if state in {
                GroupState.DISARMED.value,
                GroupState.BLOCKED_PRECONDITION.value,
                GroupState.RUNNING.value,
            }:
                return self._candidate_result("UNCHANGED", row, [], [], row["candidate_epoch"])
            missing = self._missing_agent_ids(connection, str(group_id), cutoff)
            if missing:
                connection.execute(
                    """
                    UPDATE recovery_groups SET state=?, candidate_epoch=NULL,
                        candidate_stable_since=NULL, settle_deadline_at=NULL,
                        blocked_reasons_json='[]', updated_at=?
                    WHERE group_id=?
                    """,
                    (GroupState.WAITING_FOR_NODES.value, timestamp, str(group_id)),
                )
                fresh = self._require_group(connection, str(group_id))
                return self._candidate_result("WAITING_FOR_NODES", fresh, missing, [], None)
            epoch = self._compute_epoch(connection, str(group_id))
            if epoch == row["baseline_epoch"] or epoch == row["last_scheduled_epoch"]:
                connection.execute(
                    """
                    UPDATE recovery_groups SET state=?, candidate_epoch=NULL,
                        candidate_stable_since=NULL, settle_deadline_at=NULL,
                        blocked_reasons_json='[]', updated_at=?
                    WHERE group_id=?
                    """,
                    (GroupState.ARMED_IDLE.value, timestamp, str(group_id)),
                )
                fresh = self._require_group(connection, str(group_id))
                return self._candidate_result("UNCHANGED", fresh, [], [], epoch)
            if row["candidate_epoch"] != epoch or state == GroupState.WAITING_FOR_NODES.value:
                deadline = at + timedelta(seconds=int(row["node_settle_window_seconds"]))
                connection.execute(
                    """
                    UPDATE recovery_groups SET state=?, candidate_epoch=?,
                        candidate_stable_since=?, settle_deadline_at=?,
                        blocked_reasons_json='[]', updated_at=?
                    WHERE group_id=?
                    """,
                    (
                        GroupState.SETTLING.value,
                        epoch,
                        timestamp,
                        self._timestamp(deadline),
                        timestamp,
                        str(group_id),
                    ),
                )
                fresh = self._require_group(connection, str(group_id))
                return self._candidate_result("SETTLING", fresh, [], [], epoch)
            deadline_at = parse_rfc3339(row["settle_deadline_at"])
            if deadline_at is None or at < deadline_at:
                return self._candidate_result("SETTLING", row, [], [], epoch)
            errors = self._preflight(connection, str(group_id), cutoff)
            if errors:
                connection.execute(
                    """
                    UPDATE recovery_groups SET state=?, blocked_reasons_json=?, updated_at=?
                    WHERE group_id=?
                    """,
                    (
                        GroupState.BLOCKED_PRECONDITION.value,
                        _json_dump(errors),
                        timestamp,
                        str(group_id),
                    ),
                )
                fresh = self._require_group(connection, str(group_id))
                return self._candidate_result("BLOCKED", fresh, [], errors, epoch)
            return self._candidate_result("READY", row, [], [], epoch)

    evaluate_candidate = evaluate_group_candidate

    @staticmethod
    def _candidate_result(
        decision: str,
        row: sqlite3.Row,
        missing: list[str],
        errors: list[dict[str, Any]],
        epoch: str | None,
    ) -> dict[str, Any]:
        return {
            "decision": decision,
            "epoch": epoch,
            "state": row["state"],
            "baseline_epoch": row["baseline_epoch"],
            "candidate_epoch": row["candidate_epoch"],
            "candidate_stable_since": row["candidate_stable_since"],
            "settle_deadline_at": row["settle_deadline_at"],
            "missing_agent_ids": missing,
            "all_online": not missing,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # RecoveryRun snapshots, steps, attempts and durable service locks

    def create_run_from_group(
        self,
        group_id: UUID | str,
        trigger: RunTrigger | str,
        epoch: str | None = None,
        reason: str | None = None,
        retry_of_run_id: UUID | str | None = None,
        snapshots: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        trigger_value = trigger.value if isinstance(trigger, RunTrigger) else str(trigger)
        if trigger_value not in {RunTrigger.AUTO.value, RunTrigger.MANUAL.value}:
            raise ValueError("trigger must be AUTO or MANUAL")
        if trigger_value == RunTrigger.AUTO.value and epoch is not None:
            # A scheduler retry for an already-created epoch is a pure idempotent
            # read, even when live preconditions have since changed.
            with self.db.read() as connection:
                existing = connection.execute(
                    "SELECT run_id FROM recovery_runs WHERE group_id=? AND epoch=? AND trigger='AUTO'",
                    (str(group_id), epoch),
                ).fetchone()
            if existing is not None:
                return self.get_run(existing["run_id"])  # type: ignore[return-value]
        at = now or self._now()
        timestamp = self._timestamp(at)
        cutoff = self._cutoff(at)
        run_id: str | None = None
        existing_run_id: str | None = None
        with self.db.transaction() as connection:
            group = self._require_group(connection, str(group_id))
            if group["state"] == GroupState.BLOCKED_PRECONDITION.value:
                raise ApiError(
                    409,
                    ErrorCode.GROUP_NOT_READY,
                    "BLOCKED_PRECONDITION requires disarm, repair and re-arm",
                )
            if snapshots is None:
                members = self._members_snapshot(connection, str(group_id))
                dependencies = self._dependencies_snapshot(connection, str(group_id))
                probes = self._probes_snapshot(connection, str(group_id))
            else:
                members = [dict(item) for item in snapshots.get("members", [])]
                dependencies = [dict(item) for item in snapshots.get("dependencies", [])]
                probes = [dict(item) for item in snapshots.get("probes", [])]
            service_ids = {str(item["managed_service_id"]) for item in members}
            issues = self._preflight(
                connection,
                str(group_id),
                cutoff,
                service_ids=service_ids,
                dependencies=dependencies,
            )
            if issues:
                if trigger_value == RunTrigger.AUTO.value:
                    connection.execute(
                        """
                        UPDATE recovery_groups
                        SET state=?, blocked_reasons_json=?, updated_at=?
                        WHERE group_id=?
                        """,
                        (
                            GroupState.BLOCKED_PRECONDITION.value,
                            _json_dump(issues),
                            timestamp,
                            str(group_id),
                        ),
                    )
                    # Persist the explicit isolation state while still holding the
                    # same BEGIN IMMEDIATE lock.  No Run, Step or service lock has
                    # been written at this point.
                    connection.commit()
                raise ApiError(
                    409,
                    ErrorCode.GROUP_NOT_READY,
                    "Recovery run preconditions failed",
                    {"errors": issues},
                )
            current_epoch = self._compute_epoch(connection, str(group_id))
            if (
                trigger_value == RunTrigger.AUTO.value
                and epoch is not None
                and epoch != current_epoch
            ):
                # The scheduler's READY decision and Run creation are separate
                # calls.  Fence a second OS reboot that lands in that gap; the
                # next scan will persist the new candidate and restart settling.
                raise ApiError(
                    409,
                    ErrorCode.GROUP_NOT_READY,
                    "AUTO epoch changed before the RecoveryRun was created",
                    {"expected_epoch": epoch, "current_epoch": current_epoch},
                )
            actual_epoch = current_epoch if trigger_value == RunTrigger.AUTO.value else (epoch or current_epoch)
            if trigger_value == RunTrigger.AUTO.value:
                if group["state"] == GroupState.DISARMED.value:
                    raise ApiError(409, ErrorCode.GROUP_NOT_READY, "DISARMED group cannot auto-run")
                existing = connection.execute(
                    "SELECT run_id FROM recovery_runs WHERE group_id=? AND epoch=? AND trigger='AUTO'",
                    (str(group_id), actual_epoch),
                ).fetchone()
                if existing is not None:
                    existing_run_id = existing["run_id"]
                elif group["candidate_epoch"] != actual_epoch:
                    raise ApiError(409, ErrorCode.GROUP_NOT_READY, "AUTO epoch is not the settled candidate")
            if existing_run_id is None:
                edges = [
                    (str(item["managed_service_id"]), str(item["prerequisite_managed_service_id"]))
                    for item in dependencies
                ]
                try:
                    levels = topological_levels(service_ids, edges)
                except DependencyCycleError as exc:
                    raise ApiError(
                        409,
                        ErrorCode.DEPENDENCY_CYCLE,
                        "Recovery dependency graph contains a cycle",
                    ) from exc
                conflicts: list[dict[str, str]] = []
                for service_id in sorted(service_ids):
                    lock = connection.execute(
                        "SELECT run_id FROM service_run_locks WHERE managed_service_id=?", (service_id,)
                    ).fetchone()
                    if lock is not None:
                        conflicts.append(
                            {"managed_service_id": service_id, "run_id": lock["run_id"]}
                        )
                if conflicts:
                    raise ApiError(
                        409,
                        ErrorCode.SERVICE_IN_ACTIVE_RUN,
                        "One or more services are held by an active recovery run",
                        {"conflicts": conflicts},
                    )
                run_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO recovery_runs(
                        run_id, group_id, trigger, epoch, retry_of_run_id, status, reason,
                        members_snapshot_json, dependencies_snapshot_json, probes_snapshot_json,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(group_id),
                        trigger_value,
                        actual_epoch,
                        str(retry_of_run_id) if retry_of_run_id else None,
                        RecoveryRunStatus.PENDING.value,
                        reason,
                        _json_dump(members),
                        _json_dump(dependencies),
                        _json_dump(probes),
                        timestamp,
                        timestamp,
                    ),
                )
                member_by_id = {str(item["managed_service_id"]): item for item in members}
                for level_index, level in enumerate(levels):
                    for service_id in level:
                        member = member_by_id[str(service_id)]
                        connection.execute(
                            """
                            INSERT INTO recovery_steps(
                                step_id, run_id, managed_service_id, agent_id, local_service_id,
                                status, topology_level, warnings_json, dependency_chain_json,
                                created_at, updated_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?)
                            """,
                            (
                                str(uuid4()),
                                run_id,
                                str(service_id),
                                str(member["agent_id"]),
                                member["local_service_id"],
                                RecoveryStepStatus.PENDING.value,
                                level_index,
                                timestamp,
                                timestamp,
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO service_run_locks(managed_service_id, run_id, acquired_at)
                            VALUES(?, ?, ?)
                            """,
                            (str(service_id), run_id, timestamp),
                        )
                if trigger_value == RunTrigger.AUTO.value:
                    connection.execute(
                        """
                        UPDATE recovery_groups SET state=?, last_scheduled_epoch=?, updated_at=?
                        WHERE group_id=?
                        """,
                        (GroupState.RUNNING.value, actual_epoch, timestamp, str(group_id)),
                    )
                else:
                    connection.execute(
                        "UPDATE recovery_groups SET state=?, updated_at=? WHERE group_id=?",
                        (GroupState.RUNNING.value, timestamp, str(group_id)),
                    )
        selected_id = existing_run_id or run_id
        if selected_id is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("run transaction produced no run identifier")
        return self.get_run(selected_id)  # type: ignore[return-value]

    create_run = create_run_from_group

    def retry_run(
        self,
        run_id: UUID | str,
        reason: str | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        parent = self.get_run(run_id)
        if parent is None:
            raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "Recovery run was not found")
        if parent["status"] not in {
            RecoveryRunStatus.FAILED.value,
            RecoveryRunStatus.UNKNOWN.value,
        }:
            raise ApiError(
                409,
                ErrorCode.GROUP_NOT_READY,
                "Only a FAILED or UNKNOWN recovery run may be retried",
            )
        return self.create_run_from_group(
            parent["group_id"],
            RunTrigger.MANUAL,
            epoch=parent["epoch"],
            reason=reason,
            retry_of_run_id=parent["run_id"],
            snapshots={
                "members": parent["members_snapshot"],
                "dependencies": parent["dependencies_snapshot"],
                "probes": parent["probes_snapshot"],
            },
            now=now,
        )

    def get_run(self, run_id: UUID | str) -> dict[str, Any] | None:
        with self.db.read() as connection:
            run = connection.execute(
                "SELECT * FROM recovery_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
            if run is None:
                return None
            return self._run_dict(connection, run)

    def _run_dict(self, connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        steps = connection.execute(
            "SELECT * FROM recovery_steps WHERE run_id=? ORDER BY topology_level, managed_service_id",
            (run["run_id"],),
        ).fetchall()
        step_items = [self._step_dict(connection, row) for row in steps]
        return {
            "run_id": run["run_id"],
            "group_id": run["group_id"],
            "trigger": run["trigger"],
            "epoch": run["epoch"],
            "retry_of_run_id": run["retry_of_run_id"],
            "status": run["status"],
            "reason": run["reason"],
            "members_snapshot": json.loads(run["members_snapshot_json"]),
            "dependencies_snapshot": json.loads(run["dependencies_snapshot_json"]),
            "probes_snapshot": json.loads(run["probes_snapshot_json"]),
            "steps": step_items,
            "failure_code": run["failure_code"],
            "failure_message": run["failure_message"],
            "created_at": run["created_at"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "updated_at": run["updated_at"],
        }

    def _step_dict(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        attempts = connection.execute(
            "SELECT * FROM probe_attempts WHERE step_id=? ORDER BY attempt", (row["step_id"],)
        ).fetchall()
        return {
            "step_id": row["step_id"],
            "managed_service_id": row["managed_service_id"],
            "agent_id": row["agent_id"],
            "local_service_id": row["local_service_id"],
            "status": row["status"],
            "topology_level": row["topology_level"],
            "dispatch_idempotency_key": row["dispatch_idempotency_key"],
            "operation_id": row["operation_id"],
            "probe_attempts": [
                {
                    "attempt": attempt["attempt"],
                    "started_at": attempt["started_at"],
                    "finished_at": attempt["finished_at"],
                    "result": json.loads(attempt["result_json"]),
                }
                for attempt in attempts
            ],
            "warnings": json.loads(row["warnings_json"]),
            "root_cause_step_id": row["root_cause_step_id"],
            "dependency_chain": json.loads(row["dependency_chain_json"]),
            "message": row["message"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
        }

    def list_runs(
        self,
        *,
        group_id: UUID | str | None = None,
        trigger: RunTrigger | str | None = None,
        status: RecoveryRunStatus | str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "RecoveryRun page limit is invalid",
            )

        group_value: str | None = None
        if group_id is not None:
            try:
                parsed_group_id = UUID(str(group_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ApiError(
                    422,
                    ErrorCode.VALIDATION_ERROR,
                    "RecoveryRun group filter is invalid",
                ) from exc
            if parsed_group_id.version != 4:
                raise ApiError(
                    422,
                    ErrorCode.VALIDATION_ERROR,
                    "RecoveryRun group filter is invalid",
                )
            group_value = str(parsed_group_id)

        trigger_value = (
            trigger.value
            if isinstance(trigger, RunTrigger)
            else (str(trigger) if trigger is not None else None)
        )
        if trigger_value not in {None, RunTrigger.AUTO.value, RunTrigger.MANUAL.value}:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "RecoveryRun trigger filter is invalid",
            )

        status_value = (
            status.value
            if isinstance(status, RecoveryRunStatus)
            else (str(status) if status is not None else None)
        )
        valid_statuses = {item.value for item in RecoveryRunStatus}
        if status_value is not None and status_value not in valid_statuses:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "RecoveryRun status filter is invalid",
            )

        boundary = None
        if cursor is not None:
            boundary = _decode_run_cursor(
                cursor,
                group_id=group_value,
                trigger=trigger_value,
                status=status_value,
            )

        clauses: list[str] = []
        parameters: list[Any] = []
        if group_value is not None:
            clauses.append("group_id=?")
            parameters.append(group_value)
        if trigger_value is not None:
            clauses.append("trigger=?")
            parameters.append(trigger_value)
        if status_value is not None:
            clauses.append("status=?")
            parameters.append(status_value)
        if boundary is not None:
            boundary_created_at, boundary_run_id = boundary
            clauses.append("(created_at < ? OR (created_at = ? AND run_id < ?))")
            parameters.extend([boundary_created_at, boundary_created_at, boundary_run_id])

        query = "SELECT * FROM recovery_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        parameters.append(limit + 1)

        with self.db.read() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
            page_rows = rows[:limit]
            items = [self._run_dict(connection, row) for row in page_rows]

        next_cursor = None
        if len(rows) > limit and page_rows:
            last = page_rows[-1]
            next_cursor = _encode_run_cursor(
                created_at=last["created_at"],
                run_id=last["run_id"],
                group_id=group_value,
                trigger=trigger_value,
                status=status_value,
            )
        return {"items": items, "next_cursor": next_cursor}

    def list_resumable_runs(self) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            ids = [
                row["run_id"]
                for row in connection.execute(
                    "SELECT run_id FROM recovery_runs WHERE status IN (?, ?) ORDER BY created_at",
                    _ACTIVE_RUN_STATUSES,
                ).fetchall()
            ]
        return [item for item in (self.get_run(run_id) for run_id in ids) if item]

    list_unfinished_runs = list_resumable_runs
    list_active_runs = list_resumable_runs

    def start_run(self, run_id: UUID | str, *, now: datetime | None = None) -> dict[str, Any]:
        timestamp = self._timestamp(now)
        with self.db.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE recovery_runs SET status=?, started_at=COALESCE(started_at, ?), updated_at=?
                WHERE run_id=? AND status=?
                """,
                (
                    RecoveryRunStatus.RUNNING.value,
                    timestamp,
                    timestamp,
                    str(run_id),
                    RecoveryRunStatus.PENDING.value,
                ),
            ).rowcount
            if not changed and connection.execute(
                "SELECT 1 FROM recovery_runs WHERE run_id=?", (str(run_id),)
            ).fetchone() is None:
                raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "Recovery run was not found")
        return self.get_run(run_id)  # type: ignore[return-value]

    def update_run(
        self,
        run_id: UUID | str,
        status: RecoveryRunStatus | str,
        *,
        failure_code: ErrorCode | str | None = None,
        failure_message: str | None = None,
        started_at: datetime | str | None = None,
        finished_at: datetime | str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        status_value = _enum_value(
            RecoveryRunStatus, status, label="RecoveryRun status"
        )
        if status_value in _TERMINAL_RUN_STATUSES:
            return self.finish_run(
                run_id,
                status_value,
                failure_code=failure_code,
                failure_message=failure_message,
                finished_at=finished_at,
                now=now,
            )
        timestamp = self._timestamp(now)
        explicit_started = (
            started_at if isinstance(started_at, str) else self._timestamp(started_at)
            if started_at is not None
            else timestamp
        )
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT run_id, status FROM recovery_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "Recovery run was not found")
            if row["status"] in _TERMINAL_RUN_STATUSES:
                raise ValueError("terminal RecoveryRun status is immutable")
            if (
                row["status"] == RecoveryRunStatus.RUNNING.value
                and status_value == RecoveryRunStatus.PENDING.value
            ):
                raise ValueError("RUNNING RecoveryRun cannot return to PENDING")
            connection.execute(
                """
                UPDATE recovery_runs SET status=?, started_at=CASE WHEN ?='RUNNING'
                    THEN COALESCE(started_at, ?) ELSE started_at END,
                    failure_code=?, failure_message=?, updated_at=? WHERE run_id=?
                """,
                (
                    status_value,
                    status_value,
                    explicit_started,
                    failure_code.value if hasattr(failure_code, "value") else failure_code,
                    failure_message,
                    timestamp,
                    str(run_id),
                ),
            )
        return self.get_run(run_id)  # type: ignore[return-value]

    def finish_run(
        self,
        run_id: UUID | str,
        status: RecoveryRunStatus | str,
        *,
        failure_code: ErrorCode | str | None = None,
        failure_message: str | None = None,
        finished_at: datetime | str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        status_value = _enum_value(
            RecoveryRunStatus, status, label="RecoveryRun status"
        )
        if status_value not in _TERMINAL_RUN_STATUSES:
            raise ValueError("finish_run requires a terminal status")
        timestamp = self._timestamp(now)
        explicit_finished = (
            finished_at
            if isinstance(finished_at, str)
            else self._timestamp(finished_at)
            if finished_at is not None
            else timestamp
        )
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT group_id, status FROM recovery_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "Recovery run was not found")
            if (
                row["status"] in _TERMINAL_RUN_STATUSES
                and row["status"] != status_value
            ):
                raise ValueError("terminal RecoveryRun status is immutable")
            connection.execute(
                """
                UPDATE recovery_runs SET status=?, failure_code=?, failure_message=?,
                    finished_at=COALESCE(finished_at, ?), updated_at=? WHERE run_id=?
                """,
                (
                    status_value,
                    failure_code.value if hasattr(failure_code, "value") else failure_code,
                    failure_message,
                    explicit_finished,
                    timestamp,
                    str(run_id),
                ),
            )
            connection.execute("DELETE FROM service_run_locks WHERE run_id=?", (str(run_id),))
            group = connection.execute(
                "SELECT state, baseline_epoch FROM recovery_groups WHERE group_id=?",
                (row["group_id"],),
            ).fetchone()
            if group and group["state"] != GroupState.DISARMED.value:
                next_state = (
                    GroupState.ARMED_IDLE.value
                    if group["baseline_epoch"] is not None
                    else GroupState.DISARMED.value
                )
                connection.execute(
                    """
                    UPDATE recovery_groups
                    SET state=?, blocked_reasons_json='[]', updated_at=?
                    WHERE group_id=?
                    """,
                    (next_state, timestamp, row["group_id"]),
                )
        return self.get_run(run_id)  # type: ignore[return-value]

    def release_run_locks(self, run_id: UUID | str) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM service_run_locks WHERE run_id=?", (str(run_id),))

    def service_lock(self, managed_service_id: UUID | str) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM service_run_locks WHERE managed_service_id=?",
                (str(managed_service_id),),
            ).fetchone()
        return None if row is None else dict(row)

    def update_step(
        self,
        step_id: UUID | str,
        status: RecoveryStepStatus | str,
        *,
        message: str | None = None,
        warnings: list[str] | None = None,
        root_cause_step_id: UUID | str | None = None,
        dependency_chain: list[UUID | str] | None = None,
        started_at: datetime | str | None = None,
        finished_at: datetime | str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        status_value = _enum_value(
            RecoveryStepStatus, status, label="RecoveryStep status"
        )
        timestamp = self._timestamp(now)
        explicit_started = (
            started_at
            if isinstance(started_at, str)
            else self._timestamp(started_at)
            if started_at is not None
            else timestamp
        )
        explicit_finished = (
            finished_at
            if isinstance(finished_at, str)
            else self._timestamp(finished_at)
            if finished_at is not None
            else timestamp
        )
        terminal = status_value in {
            RecoveryStepStatus.READY.value,
            RecoveryStepStatus.FAILED.value,
            RecoveryStepStatus.BLOCKED.value,
            RecoveryStepStatus.UNKNOWN.value,
        }
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_steps WHERE step_id=?", (str(step_id),)
            ).fetchone()
            if row is None:
                raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "Recovery step was not found")
            if (
                row["status"]
                in {item.value for item in RecoveryStepStatus if item.terminal}
                and row["status"] != status_value
            ):
                raise ValueError("terminal RecoveryStep status is immutable")
            connection.execute(
                """
                UPDATE recovery_steps SET status=?, message=?, warnings_json=?,
                    root_cause_step_id=?, dependency_chain_json=?,
                    started_at=CASE WHEN ? IN ('STARTING','PROBING')
                        THEN COALESCE(started_at, ?) ELSE started_at END,
                    finished_at=CASE WHEN ? THEN COALESCE(finished_at, ?) ELSE finished_at END,
                    updated_at=? WHERE step_id=?
                """,
                (
                    status_value,
                    message if message is not None else row["message"],
                    _json_dump(warnings if warnings is not None else json.loads(row["warnings_json"])),
                    (
                        str(root_cause_step_id)
                        if root_cause_step_id is not None
                        else row["root_cause_step_id"]
                    ),
                    _json_dump(
                        [str(item) for item in dependency_chain]
                        if dependency_chain is not None
                        else json.loads(row["dependency_chain_json"])
                    ),
                    status_value,
                    explicit_started,
                    1 if terminal else 0,
                    explicit_finished,
                    timestamp,
                    str(step_id),
                ),
            )
            fresh = connection.execute(
                "SELECT * FROM recovery_steps WHERE step_id=?", (str(step_id),)
            ).fetchone()
            return self._step_dict(connection, fresh)

    def assign_step_dispatch(
        self, step_id: UUID | str, idempotency_key: UUID | str
    ) -> str:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT dispatch_idempotency_key FROM recovery_steps WHERE step_id=?",
                (str(step_id),),
            ).fetchone()
            if row is None:
                raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "Recovery step was not found")
            final = row["dispatch_idempotency_key"] or str(idempotency_key)
            if row["dispatch_idempotency_key"] is None:
                connection.execute(
                    "UPDATE recovery_steps SET dispatch_idempotency_key=?, updated_at=? WHERE step_id=?",
                    (final, self._timestamp(), str(step_id)),
                )
            return final

    set_step_dispatch_key = assign_step_dispatch

    def assign_step_operation(self, step_id: UUID | str, operation_id: UUID | str) -> str:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT operation_id FROM recovery_steps WHERE step_id=?", (str(step_id),)
            ).fetchone()
            if row is None:
                raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "Recovery step was not found")
            final = row["operation_id"] or str(operation_id)
            if row["operation_id"] is None:
                connection.execute(
                    "UPDATE recovery_steps SET operation_id=?, updated_at=? WHERE step_id=?",
                    (final, self._timestamp(), str(step_id)),
                )
            return final

    set_step_operation_id = assign_step_operation

    def record_probe_attempt(
        self,
        step_id: UUID | str,
        result: BaseModel | Mapping[str, Any],
        started_at: datetime | str,
        finished_at: datetime | str,
        *,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        started = started_at if isinstance(started_at, str) else self._timestamp(started_at)
        finished = finished_at if isinstance(finished_at, str) else self._timestamp(finished_at)
        payload = _model_dict(result)
        with self.db.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM recovery_steps WHERE step_id=?", (str(step_id),)
            ).fetchone() is None:
                raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "Recovery step was not found")
            final_attempt = attempt
            if final_attempt is None:
                final_attempt = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(attempt), 0) + 1 AS next FROM probe_attempts WHERE step_id=?",
                        (str(step_id),),
                    ).fetchone()["next"]
                )
            connection.execute(
                """
                INSERT INTO probe_attempts(step_id, attempt, started_at, finished_at, result_json)
                VALUES(?, ?, ?, ?, ?)
                """,
                (str(step_id), final_attempt, started, finished, _json_dump(payload)),
            )
            connection.execute(
                "UPDATE recovery_steps SET updated_at=? WHERE step_id=?",
                (self._timestamp(), str(step_id)),
            )
        return {
            "attempt": final_attempt,
            "started_at": started,
            "finished_at": finished,
            "result": payload,
        }

    add_probe_attempt = record_probe_attempt

    # ------------------------------------------------------------------
    # Durable proxy dispatch records

    def prepare_proxy_action(
        self,
        managed_service_id: UUID | str,
        action: str,
        idempotency_key: UUID | str,
        request_fingerprint: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, "Unsupported service action")
        timestamp = self._timestamp(now)
        with self.db.transaction() as connection:
            # Resolve retries before consulting mutable liveness, inventory or
            # run-lock state.  A previously prepared request remains the same
            # request even if the Agent subsequently goes offline.
            existing = connection.execute(
                "SELECT * FROM proxy_dispatches WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise ApiError(
                        409,
                        ErrorCode.IDEMPOTENCY_KEY_REUSED,
                        "Idempotency-Key was already used for another request",
                    )
                return dict(existing)
            row = connection.execute(
                """
                SELECT s.*, a.endpoint, a.last_received_at
                FROM services s JOIN agents a ON a.agent_id=s.agent_id
                WHERE s.managed_service_id=?
                """,
                (str(managed_service_id),),
            ).fetchone()
            if row is None or not row["seen_in_last_report"]:
                raise ApiError(404, ErrorCode.SERVICE_NOT_ALLOWLISTED, "Managed service was not found")
            if not self._leases.is_online(row["agent_id"]):
                raise ApiError(409, ErrorCode.AGENT_OFFLINE, "Target agent is offline")
            lock = connection.execute(
                "SELECT run_id FROM service_run_locks WHERE managed_service_id=?",
                (str(managed_service_id),),
            ).fetchone()
            if lock is not None:
                raise ApiError(
                    409,
                    ErrorCode.SERVICE_IN_ACTIVE_RUN,
                    "Service is held by an active recovery run",
                    {"run_id": lock["run_id"]},
                )
            connection.execute(
                """
                INSERT INTO proxy_dispatches(
                    idempotency_key, request_fingerprint, managed_service_id, agent_id,
                    local_service_id, endpoint, action, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(idempotency_key),
                    request_fingerprint,
                    str(managed_service_id),
                    row["agent_id"],
                    row["local_service_id"],
                    row["endpoint"],
                    action,
                    timestamp,
                    timestamp,
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM proxy_dispatches WHERE idempotency_key=?",
                    (str(idempotency_key),),
                ).fetchone()
            )

    def save_proxy_operation(
        self,
        managed_service_id: UUID | str,
        endpoint: str,
        operation: BaseModel | Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        payload = _model_dict(operation)
        timestamp = self._timestamp(now)
        operation_id = str(payload["operation_id"])
        with self.db.transaction() as connection:
            dispatch = connection.execute(
                "SELECT * FROM proxy_dispatches WHERE idempotency_key=?",
                (str(payload["idempotency_key"]),),
            ).fetchone()
            if dispatch is None:
                raise ApiError(
                    409,
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    "Proxy action was not durably prepared",
                )
            if (
                dispatch["managed_service_id"] != str(managed_service_id)
                or dispatch["agent_id"] != str(payload["agent_id"])
                or dispatch["local_service_id"] != str(payload["local_service_id"])
                or dispatch["action"] != str(payload["action"])
            ):
                raise ApiError(
                    409,
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    "Agent Operation does not match the prepared proxy action",
                )
            existing_operation = connection.execute(
                "SELECT managed_service_id, agent_id FROM proxy_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing_operation is not None and (
                existing_operation["managed_service_id"] != str(managed_service_id)
                or existing_operation["agent_id"] != str(payload["agent_id"])
            ):
                raise ApiError(
                    409,
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    "Operation identifier is already bound to another target",
                )
            connection.execute(
                """
                INSERT INTO proxy_operations(
                    operation_id, managed_service_id, agent_id, endpoint,
                    operation_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    operation_json=excluded.operation_json, endpoint=excluded.endpoint,
                    updated_at=excluded.updated_at
                """,
                (
                    operation_id,
                    str(managed_service_id),
                    str(payload["agent_id"]),
                    endpoint,
                    _json_dump(payload),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE proxy_dispatches SET operation_id=?, updated_at=?
                WHERE idempotency_key=?
                """,
                (operation_id, timestamp, str(payload["idempotency_key"])),
            )
        return {"managed_service_id": str(managed_service_id), **payload}

    complete_proxy_action = save_proxy_operation

    def get_proxy_operation(self, operation_id: UUID | str) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM proxy_operations WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
        if row is None:
            return None
        return {
            "managed_service_id": row["managed_service_id"],
            **json.loads(row["operation_json"]),
        }

    def get_proxy_operation_route(self, operation_id: UUID | str) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(
                """
                SELECT operation_id, managed_service_id, agent_id, endpoint
                FROM proxy_operations WHERE operation_id=?
                """,
                (str(operation_id),),
            ).fetchone()
        return None if row is None else dict(row)
