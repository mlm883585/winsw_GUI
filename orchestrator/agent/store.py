from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from orchestrator.agent.config import AllowlistedService
from orchestrator.agent.identity import AgentIdentity
from orchestrator.common.db import SQLiteDatabase
from orchestrator.common.enums import JournalState, OperationStatus, ServiceAction
from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.models import Operation
from orchestrator.common.time import parse_rfc3339, to_rfc3339, utc_now


class ServiceMappingChangedError(RuntimeError):
    """The durable local service identity no longer matches the allowlist."""


def _migration_v1(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE agent_identity (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            agent_id TEXT NOT NULL,
            boot_marker TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            instance_generation INTEGER NOT NULL CHECK(instance_generation >= 1),
            current_instance_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE operations (
            operation_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            local_service_id TEXT NOT NULL,
            windows_service_name TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('start', 'stop', 'restart')),
            status TEXT NOT NULL CHECK(status IN
                ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'REJECTED', 'UNKNOWN')),
            idempotency_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,
            journal_state TEXT NOT NULL CHECK(journal_state IN
                ('PREPARED', 'DISPATCHING', 'COMPLETED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error_code TEXT,
            message TEXT
        )
        """,
        """
        CREATE UNIQUE INDEX one_active_operation_per_service
            ON operations(local_service_id)
            WHERE status IN ('PENDING', 'RUNNING')
        """,
        """
        CREATE INDEX operations_journal_scan
            ON operations(journal_state, status, created_at)
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _migration_v2(connection: sqlite3.Connection) -> None:
    """Persist the immutable meaning of every local service identifier.

    Existing v1 databases may already contain Operations.  Those rows are the
    only durable evidence of the mapping that was accepted before this table
    existed, so use them to seed the binding and fail the migration if one
    local identifier has historically named different Windows services.
    """

    connection.execute(
        """
        CREATE TABLE service_bindings (
            local_service_id TEXT PRIMARY KEY,
            windows_service_name TEXT NOT NULL,
            windows_service_name_casefold TEXT NOT NULL UNIQUE,
            bound_at TEXT NOT NULL
        )
        """
    )
    rows = connection.execute(
        """
        SELECT local_service_id, windows_service_name, created_at
        FROM operations
        ORDER BY created_at, operation_id
        """
    ).fetchall()
    bindings: dict[str, tuple[str, str, str]] = {}
    claimed_windows_names: dict[str, str] = {}
    for row in rows:
        local_service_id = str(row["local_service_id"])
        windows_service_name = str(row["windows_service_name"])
        folded_name = windows_service_name.casefold()
        existing = bindings.get(local_service_id)
        if existing is not None and existing[1] != folded_name:
            raise ServiceMappingChangedError(
                f"historical service mapping changed for local_service_id {local_service_id!r}"
            )
        existing_local_id = claimed_windows_names.get(folded_name)
        if existing_local_id is not None and existing_local_id != local_service_id:
            raise ServiceMappingChangedError(
                "one Windows service was historically bound to multiple local service identifiers"
            )
        if existing is None:
            bindings[local_service_id] = (
                windows_service_name,
                folded_name,
                str(row["created_at"]),
            )
            claimed_windows_names[folded_name] = local_service_id

    connection.executemany(
        """
        INSERT INTO service_bindings(
            local_service_id, windows_service_name,
            windows_service_name_casefold, bound_at
        ) VALUES(?, ?, ?, ?)
        """,
        [
            (local_service_id, windows_service_name, folded_name, bound_at)
            for local_service_id, (windows_service_name, folded_name, bound_at) in bindings.items()
        ],
    )


@dataclass(frozen=True, slots=True)
class ReservedOperation:
    operation: Operation
    created: bool


class AgentStore:
    def __init__(self, path: str | Path) -> None:
        self.database = SQLiteDatabase(path)

    def initialize(self) -> None:
        self.database.initialize("recovery_agent", [_migration_v1, _migration_v2])

    def bind_service_mappings(self, services: list[AllowlistedService]) -> None:
        """Bind local ids once and reject silent rebinding on later starts."""

        now = to_rfc3339(utc_now())
        with self.database.transaction() as connection:
            for service in services:
                folded_name = service.windows_service_name.casefold()
                row = connection.execute(
                    """
                    SELECT windows_service_name_casefold
                    FROM service_bindings
                    WHERE local_service_id=?
                    """,
                    (service.local_service_id,),
                ).fetchone()
                if row is not None:
                    if str(row["windows_service_name_casefold"]) != folded_name:
                        raise ServiceMappingChangedError(
                            "service mapping changed for local_service_id "
                            f"{service.local_service_id!r}; use a new local_service_id"
                        )
                    continue
                claimed = connection.execute(
                    """
                    SELECT local_service_id
                    FROM service_bindings
                    WHERE windows_service_name_casefold=?
                    """,
                    (folded_name,),
                ).fetchone()
                if claimed is not None:
                    raise ServiceMappingChangedError(
                        "Windows service is already bound to local_service_id "
                        f"{str(claimed['local_service_id'])!r}"
                    )
                connection.execute(
                    """
                    INSERT INTO service_bindings(
                        local_service_id, windows_service_name,
                        windows_service_name_casefold, bound_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        service.local_service_id,
                        service.windows_service_name,
                        folded_name,
                        now,
                    ),
                )

    def initialize_identity(self, boot_marker: str) -> AgentIdentity:
        now = to_rfc3339(utc_now())
        uncommitted_instance = str(UUID(int=0))
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM agent_identity WHERE singleton=1").fetchone()
            if row is None:
                agent_id = uuid4()
                boot_id = uuid4()
                generation = 1
                connection.execute(
                    """
                    INSERT INTO agent_identity(
                        singleton, agent_id, boot_marker, boot_id,
                        instance_generation, current_instance_id, updated_at
                    ) VALUES(1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(agent_id),
                        boot_marker,
                        str(boot_id),
                        generation,
                        uncommitted_instance,
                        now,
                    ),
                )
            else:
                agent_id = UUID(row["agent_id"])
                boot_id = UUID(row["boot_id"]) if row["boot_marker"] == boot_marker else uuid4()
                generation = int(row["instance_generation"]) + 1
                connection.execute(
                    """
                    UPDATE agent_identity
                    SET boot_marker=?, boot_id=?, instance_generation=?,
                        current_instance_id=?, updated_at=?
                    WHERE singleton=1
                    """,
                    (boot_marker, str(boot_id), generation, uncommitted_instance, now),
                )
            # The generation row is changed first inside BEGIN IMMEDIATE.  The
            # sentinel is never visible outside this uncommitted transaction;
            # failure to generate/persist the UUID rolls the increment back.
            instance_id = uuid4()
            connection.execute(
                """
                UPDATE agent_identity
                SET current_instance_id=?, updated_at=?
                WHERE singleton=1 AND instance_generation=?
                """,
                (str(instance_id), now, generation),
            )
        return AgentIdentity(agent_id, boot_id, instance_id, generation)

    def reserve_operation(
        self,
        *,
        agent_id: UUID,
        service: AllowlistedService,
        action: ServiceAction,
        idempotency_key: UUID,
        request_fingerprint: str,
        initial_status: OperationStatus = OperationStatus.PENDING,
        error_code: ErrorCode | None = None,
        message: str | None = None,
    ) -> ReservedOperation:
        now = utc_now()
        now_text = to_rfc3339(now)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM operations WHERE idempotency_key=?", (str(idempotency_key),)
            ).fetchone()
            if existing is not None:
                operation = self._row_to_operation(existing)
                if operation.request_fingerprint != request_fingerprint:
                    raise ApiError(
                        409,
                        ErrorCode.IDEMPOTENCY_KEY_REUSED,
                        "Idempotency-Key belongs to a different request",
                    )
                return ReservedOperation(operation, False)

            active = connection.execute(
                """
                SELECT operation_id FROM operations
                WHERE local_service_id=? AND status IN ('PENDING', 'RUNNING')
                LIMIT 1
                """,
                (service.local_service_id,),
            ).fetchone()
            if active is not None:
                initial_status = OperationStatus.REJECTED
                error_code = ErrorCode.SERVICE_ACTION_CONFLICT
                message = "Another action is active for this service"

            terminal = initial_status.terminal
            operation_id = uuid4()
            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, agent_id, local_service_id, windows_service_name,
                    action, status, idempotency_key, request_fingerprint, journal_state,
                    created_at, updated_at, started_at, finished_at, error_code, message
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    str(operation_id),
                    str(agent_id),
                    service.local_service_id,
                    service.windows_service_name,
                    action.value,
                    initial_status.value,
                    str(idempotency_key),
                    request_fingerprint,
                    JournalState.COMPLETED.value if terminal else JournalState.PREPARED.value,
                    now_text,
                    now_text,
                    now_text if terminal else None,
                    error_code.value if error_code else None,
                    message,
                ),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
        assert row is not None
        return ReservedOperation(self._row_to_operation(row), True)

    def find_idempotent_operation(
        self,
        idempotency_key: UUID,
        request_fingerprint: str,
    ) -> Operation | None:
        """Resolve a replay before consulting mutable SCM state.

        The idempotency contract is global to this Agent.  Once a request has
        been durably accepted, later service-state changes must not turn the
        same retry into a different response.
        """

        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
        if row is None:
            return None
        operation = self._row_to_operation(row)
        if operation.request_fingerprint != request_fingerprint:
            raise ApiError(
                409,
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                "Idempotency-Key belongs to a different request",
            )
        return operation

    def resolve_replay_or_reject_active(
        self,
        *,
        agent_id: UUID,
        service: AllowlistedService,
        action: ServiceAction,
        idempotency_key: UUID,
        request_fingerprint: str,
    ) -> ReservedOperation | None:
        """Resolve a retry or durably reject while another action is active.

        This short admission transaction prevents a new HTTP request from
        waiting behind a potentially long native SCM call.  Returning ``None``
        means the caller may acquire the per-service lock and observe SCM state.
        """

        now = utc_now()
        now_text = to_rfc3339(now)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM operations WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
            if existing is not None:
                operation = self._row_to_operation(existing)
                if operation.request_fingerprint != request_fingerprint:
                    raise ApiError(
                        409,
                        ErrorCode.IDEMPOTENCY_KEY_REUSED,
                        "Idempotency-Key belongs to a different request",
                    )
                return ReservedOperation(operation, False)

            active = connection.execute(
                """
                SELECT operation_id FROM operations
                WHERE local_service_id=? AND status IN ('PENDING', 'RUNNING')
                LIMIT 1
                """,
                (service.local_service_id,),
            ).fetchone()
            if active is None:
                return None

            operation_id = uuid4()
            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, agent_id, local_service_id, windows_service_name,
                    action, status, idempotency_key, request_fingerprint, journal_state,
                    created_at, updated_at, started_at, finished_at, error_code, message
                ) VALUES(?, ?, ?, ?, ?, 'REJECTED', ?, ?, 'COMPLETED', ?, ?, NULL, ?, ?, ?)
                """,
                (
                    str(operation_id),
                    str(agent_id),
                    service.local_service_id,
                    service.windows_service_name,
                    action.value,
                    str(idempotency_key),
                    request_fingerprint,
                    now_text,
                    now_text,
                    now_text,
                    ErrorCode.SERVICE_ACTION_CONFLICT.value,
                    "Another action is active for this service",
                ),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
        assert row is not None
        return ReservedOperation(self._row_to_operation(row), True)

    def get_operation(self, operation_id: UUID) -> Operation | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
        return self._row_to_operation(row) if row is not None else None

    def list_pending_operation_ids(self) -> list[UUID]:
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT operation_id FROM operations
                WHERE status='PENDING' AND journal_state='PREPARED'
                ORDER BY created_at, operation_id
                """
            ).fetchall()
        return [UUID(row["operation_id"]) for row in rows]

    def list_dispatching_operations(self) -> list[Operation]:
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operations
                WHERE status='RUNNING' AND journal_state='DISPATCHING'
                ORDER BY created_at, operation_id
                """
            ).fetchall()
        return [self._row_to_operation(row) for row in rows]

    def claim_operation(self, operation_id: UUID) -> Operation | None:
        now = to_rfc3339(utc_now())
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE operations
                SET status='RUNNING', journal_state='DISPATCHING',
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE operation_id=? AND status='PENDING' AND journal_state='PREPARED'
                """,
                (now, now, str(operation_id)),
            ).rowcount
            if changed != 1:
                return None
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
        assert row is not None
        return self._row_to_operation(row)

    def finish_operation(
        self,
        operation_id: UUID,
        status: OperationStatus,
        *,
        error_code: ErrorCode | None = None,
        message: str | None = None,
    ) -> Operation:
        if not status.terminal:
            raise ValueError("finish_operation requires a terminal status")
        now = to_rfc3339(utc_now())
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE operations
                SET status=?, journal_state='COMPLETED', updated_at=?, finished_at=?,
                    error_code=?, message=?
                WHERE operation_id=? AND status IN ('PENDING', 'RUNNING')
                """,
                (
                    status.value,
                    now,
                    now,
                    error_code.value if error_code else None,
                    message,
                    str(operation_id),
                ),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if changed == 0 and not OperationStatus(row["status"]).terminal:
                raise RuntimeError("operation could not be completed")
        return self._row_to_operation(row)

    @staticmethod
    def _row_to_operation(row: sqlite3.Row) -> Operation:
        return Operation(
            operation_id=UUID(row["operation_id"]),
            agent_id=UUID(row["agent_id"]),
            local_service_id=row["local_service_id"],
            windows_service_name=row["windows_service_name"],
            action=ServiceAction(row["action"]),
            status=OperationStatus(row["status"]),
            idempotency_key=UUID(row["idempotency_key"]),
            request_fingerprint=row["request_fingerprint"],
            created_at=parse_rfc3339(row["created_at"]),
            updated_at=parse_rfc3339(row["updated_at"]),
            started_at=parse_rfc3339(row["started_at"]),
            finished_at=parse_rfc3339(row["finished_at"]),
            error_code=ErrorCode(row["error_code"]) if row["error_code"] else None,
            message=row["message"],
        )
