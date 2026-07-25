from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypeVar, runtime_checkable
from uuid import UUID, uuid4

from orchestrator.common.enums import (
    AgentOnlineStatus,
    GroupState,
    InstallationState,
    OperationStatus,
    RecoveryRunStatus,
    RecoveryStepStatus,
    RunTrigger,
    RuntimeState,
    StartupState,
)
from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.graph import DependencyCycleError
from orchestrator.common.models import (
    HttpProbeRequest,
    ProbeResult,
    ScmProbeRequest,
    TcpProbeRequest,
)
from orchestrator.common.security import canonical_request_fingerprint
from orchestrator.common.time import parse_rfc3339, utc_now

from .agent_client import AgentOperationProtocolError


LOGGER = logging.getLogger(__name__)


Record = dict[str, Any]
T = TypeVar("T")

_AGENT_PROTOCOL_MISMATCH_PREFIX = f"{ErrorCode.AGENT_PROTOCOL_MISMATCH.value}:"


class RecoveryEngineError(RuntimeError):
    """A deterministic orchestration failure suitable for an API error mapping."""

    def __init__(self, code: str, message: str, detail: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@runtime_checkable
class RecoveryStore(Protocol):
    """Persistence boundary used by :class:`RecoveryEngine`.

    Implementations may return dictionaries or Pydantic models. Mutating methods
    are deliberately small so every safety boundary is durable before network I/O.
    """

    def list_groups(self) -> Sequence[Record]: ...

    def get_group(self, group_id: UUID | str) -> Record | None: ...

    def evaluate_group_candidate(
        self, group_id: UUID | str, now: datetime | None = None
    ) -> Record: ...

    def validate_group_preconditions(
        self, group_id: UUID | str, now: datetime | None = None
    ) -> Sequence[Record]: ...

    def create_run_from_group(
        self,
        group_id: UUID | str,
        trigger: str,
        epoch: str | None = None,
        reason: str | None = None,
        retry_of_run_id: UUID | str | None = None,
    ) -> Record | None: ...

    def get_run(self, run_id: UUID | str) -> Record | None: ...

    def list_unfinished_runs(self) -> Sequence[Record]: ...

    def get_service(self, managed_service_id: UUID | str) -> Record | None: ...

    def update_run(self, run_id: UUID | str, status: str, **fields: Any) -> Any: ...

    def update_step(self, step_id: UUID | str, status: str, **fields: Any) -> Any: ...

    def assign_step_dispatch(self, step_id: UUID | str, key: UUID | str) -> UUID | str: ...

    def assign_step_operation(
        self, step_id: UUID | str, operation_id: UUID | str
    ) -> UUID | str: ...

    def record_probe_attempt(self, step_id: UUID | str, **attempt: Any) -> Any: ...

    def release_run_locks(self, run_id: UUID | str) -> Any: ...


@runtime_checkable
class AgentClient(Protocol):
    async def start_action(
        self,
        endpoint: str,
        local_service_id: str,
        action: str,
        idempotency_key: UUID | str,
    ) -> Any: ...

    async def get_operation(self, endpoint: str, operation_id: UUID | str) -> Any: ...

    async def probe(self, endpoint: str, request: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class TopologyBatch:
    """A bounded slice of one logical Kahn topology level."""

    level: int
    services: tuple[str, ...]


def _uuid_text(value: UUID | str) -> str:
    return str(UUID(str(value))).lower()


def _uuid4_text(value: Any) -> str:
    parsed = UUID(str(value))
    if parsed.version != 4:
        raise ValueError("value is not a UUIDv4")
    return str(parsed).lower()


def _start_operation_binding_mismatch(
    operation: Mapping[str, Any],
    step: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    expected_operation_id: Any | None,
) -> str | None:
    """Return the first semantic field that is not bound to this start dispatch.

    Agent responses are untrusted protocol input.  A syntactically valid
    Operation belonging to another Agent, service, action, or idempotent
    request must never authorize readiness probing or release a downstream
    dependency.
    """

    try:
        actual_operation_id = _uuid4_text(operation.get("operation_id"))
    except (TypeError, ValueError, AttributeError):
        return "operation_id"
    if expected_operation_id is not None:
        try:
            if actual_operation_id != _uuid4_text(expected_operation_id):
                return "operation_id"
        except (TypeError, ValueError, AttributeError):
            return "operation_id"

    try:
        if _uuid4_text(operation.get("agent_id")) != _uuid4_text(step.get("agent_id")):
            return "agent_id"
    except (TypeError, ValueError, AttributeError):
        return "agent_id"

    actual_local_id = operation.get("local_service_id")
    expected_local_id = step.get("local_service_id")
    if not isinstance(actual_local_id, str) or actual_local_id != expected_local_id:
        return "local_service_id"

    actual_windows_name = operation.get("windows_service_name")
    expected_windows_name = member.get("windows_service_name")
    if (
        not isinstance(actual_windows_name, str)
        or not isinstance(expected_windows_name, str)
        or actual_windows_name.casefold() != expected_windows_name.casefold()
    ):
        return "windows_service_name"

    if str(_value(operation.get("action", ""))) != "start":
        return "action"

    try:
        if _uuid4_text(operation.get("idempotency_key")) != _uuid4_text(
            step.get("dispatch_idempotency_key")
        ):
            return "idempotency_key"
    except (TypeError, ValueError, AttributeError):
        return "idempotency_key"

    expected_fingerprint = canonical_request_fingerprint(
        "POST",
        f"/api/v1/services/{expected_local_id}/actions/start",
        {},
    )
    if operation.get("request_fingerprint") != expected_fingerprint:
        return "request_fingerprint"
    return None


def _is_agent_protocol_mismatch(step: Mapping[str, Any]) -> bool:
    return str(step.get("message") or "").startswith(_AGENT_PROTOCOL_MISMATCH_PREFIX)


def canonical_epoch(
    group_id: UUID | str,
    agent_boot_ids: Mapping[UUID | str, UUID | str]
    | Iterable[tuple[UUID | str, UUID | str]],
) -> str:
    """Return the exact Recovery MVP v1 boot epoch digest.

    UUIDs are parsed and rendered canonically. Duplicate agent identifiers are
    rejected even if their boot identifiers match, avoiding ambiguous input.
    """

    pairs = agent_boot_ids.items() if isinstance(agent_boot_ids, Mapping) else agent_boot_ids
    normalized: dict[str, str] = {}
    for raw_agent_id, raw_boot_id in pairs:
        agent_id = _uuid_text(raw_agent_id)
        if agent_id in normalized:
            raise ValueError(f"duplicate agent_id in epoch input: {agent_id}")
        normalized[agent_id] = _uuid_text(raw_boot_id)
    if not normalized:
        raise ValueError("at least one agent boot id is required")

    payload = ["recovery-mvp-v1\n", f"group_id={_uuid_text(group_id)}\n"]
    payload.extend(
        f"agent_id={agent_id};boot_id={normalized[agent_id]}\n"
        for agent_id in sorted(normalized)
    )
    return hashlib.sha256("".join(payload).encode("utf-8")).hexdigest()


def _dependency_pair(edge: Any) -> tuple[str, str]:
    if isinstance(edge, Mapping):
        dependent = edge.get("managed_service_id", edge.get("dependent"))
        prerequisite = edge.get(
            "prerequisite_managed_service_id", edge.get("prerequisite")
        )
    elif hasattr(edge, "managed_service_id"):
        dependent = getattr(edge, "managed_service_id")
        prerequisite = getattr(edge, "prerequisite_managed_service_id")
    else:
        dependent, prerequisite = edge
    if dependent is None or prerequisite is None:
        raise ValueError("dependency must contain dependent and prerequisite services")
    return str(dependent), str(prerequisite)


def _logical_kahn_levels(
    nodes: Iterable[UUID | str], dependencies: Iterable[Any]
) -> list[list[str]]:
    node_set = {str(node) for node in nodes}
    indegree = {node: 0 for node in node_set}
    downstream: dict[str, set[str]] = defaultdict(set)

    for raw_edge in dependencies:
        dependent, prerequisite = _dependency_pair(raw_edge)
        if dependent not in node_set or prerequisite not in node_set:
            raise ValueError("dependency references a service outside the run snapshot")
        if dependent == prerequisite:
            raise DependencyCycleError([dependent])
        if dependent not in downstream[prerequisite]:
            downstream[prerequisite].add(dependent)
            indegree[dependent] += 1

    current = sorted(node for node, degree in indegree.items() if degree == 0)
    levels: list[list[str]] = []
    visited = 0
    while current:
        levels.append(current)
        visited += len(current)
        following: list[str] = []
        for node in current:
            for child in sorted(downstream[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    following.append(child)
        current = sorted(following)
    if visited != len(node_set):
        cyclic = sorted(node for node, degree in indegree.items() if degree > 0)
        raise DependencyCycleError(cyclic)
    return levels


def kahn_layers(
    nodes: Iterable[UUID | str],
    dependencies: Iterable[Any],
    *,
    max_parallel: int = 4,
) -> list[TopologyBatch]:
    """Return deterministic Kahn levels split into batches of at most four.

    Batches originating from the same logical level retain the same ``level``.
    The engine itself uses a semaphore as well, so this helper is also useful for
    rendering or testing without weakening dependency ordering.
    """

    if not 1 <= max_parallel <= 4:
        raise ValueError("max_parallel must be between 1 and 4")
    batches: list[TopologyBatch] = []
    for level, services in enumerate(_logical_kahn_levels(nodes, dependencies)):
        for offset in range(0, len(services), max_parallel):
            batches.append(TopologyBatch(level, tuple(services[offset : offset + max_parallel])))
    return batches


def _record(value: Any) -> Record:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"unsupported record type: {type(value)!r}")


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _status(record: Mapping[str, Any], key: str = "status") -> str:
    return str(_value(record.get(key, "")))


def _aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        return parse_rfc3339(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _await_if_needed(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


class RecoveryEngine:
    """Crash-resumable strict-DAG recovery coordinator.

    The database owns durable run/service locks. The in-memory locks below only
    prevent duplicate work in one CP process; correctness does not depend on them.
    """

    def __init__(
        self,
        store: RecoveryStore,
        agent_client: AgentClient,
        *,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        operation_poll_interval_seconds: float = 1.0,
        operation_poll_deadline_seconds: float = 300.0,
    ) -> None:
        self.store = store
        self.agent_client = agent_client
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep
        self.operation_poll_interval_seconds = operation_poll_interval_seconds
        self.operation_poll_deadline_seconds = operation_poll_deadline_seconds
        self._run_guards: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._service_guards: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        # Wall-clock timestamps remain useful durable evidence, but they cannot
        # prove a continuous settle interval when NTP or an administrator moves
        # the clock.  This process-local guard deliberately starts empty after
        # a CP restart, making recovery wait a fresh full window rather than
        # trusting a persisted deadline that may have elapsed while CP was down.
        self._candidate_monotonic_started: dict[str, tuple[str, float]] = {}

    async def _store_call(self, names: str | Sequence[str], *args: Any, **kwargs: Any) -> Any:
        candidates = (names,) if isinstance(names, str) else names
        for name in candidates:
            method = getattr(self.store, name, None)
            if method is not None:
                return await _await_if_needed(method(*args, **kwargs))
        raise AttributeError(f"store does not implement any of: {', '.join(candidates)}")

    async def scan_auto_groups(self) -> list[Record]:
        """Evaluate every armed group and atomically create eligible AUTO runs."""

        groups = await self._store_call("list_groups")
        created: list[Record] = []
        eligible_states = {
            GroupState.ARMED_IDLE.value,
            GroupState.WAITING_FOR_NODES.value,
            GroupState.SETTLING.value,
        }
        known_group_ids = {str(_record(item)["group_id"]) for item in groups}
        for stale_group_id in set(self._candidate_monotonic_started) - known_group_ids:
            self._candidate_monotonic_started.pop(stale_group_id, None)
        for raw_group in groups:
            group = _record(raw_group)
            group_id = str(group["group_id"])
            if str(_value(group.get("state"))) not in eligible_states:
                self._candidate_monotonic_started.pop(group_id, None)
                continue
            evaluator = getattr(self.store, "evaluate_group_candidate", None) or getattr(
                self.store, "evaluate_candidate", None
            )
            try:
                if evaluator is not None:
                    decision = _record(await _await_if_needed(evaluator(group_id, now=self.now())))
                else:
                    decision = await self._evaluate_candidate_fallback(group)
            except (RecoveryEngineError, ApiError):
                # A concurrent administrative change may invalidate this one
                # group; it must not prevent other groups from being evaluated.
                self._candidate_monotonic_started.pop(group_id, None)
                continue

            if not self._monotonic_candidate_ready(group, decision):
                continue
            epoch = decision.get("epoch")
            if not epoch:
                self._candidate_monotonic_started.pop(group_id, None)
                continue
            try:
                run = await self._create_run(
                    group_id,
                    trigger=RunTrigger.AUTO.value,
                    epoch=str(epoch),
                )
            except (RecoveryEngineError, ApiError):
                # Store rechecks preconditions and moves the group into its
                # deterministic blocked state; no Agent call has occurred.
                self._candidate_monotonic_started.pop(group_id, None)
                continue
            if run:
                self._candidate_monotonic_started.pop(group_id, None)
                created.append(run)
                self.launch_run(run["run_id"])
        return created

    def _monotonic_candidate_ready(self, group: Record, decision: Record) -> bool:
        """Require one unchanged candidate for a full process-local window.

        The durable Store may say ``READY`` early after a wall-clock jump, or
        remain ``SETTLING`` after a backward jump.  Both are merely observations
        of the same persisted candidate.  Only monotonic elapsed time authorizes
        AUTO Run creation; the Store still performs the final epoch and
        precondition checks atomically.
        """

        group_id = str(group["group_id"])
        decision_name = str(decision.get("decision", "")).upper()
        epoch_value = decision.get("epoch")
        if decision_name not in {"SETTLING", "READY"} or not epoch_value:
            self._candidate_monotonic_started.pop(group_id, None)
            return False

        epoch = str(epoch_value)
        current_tick = self.monotonic()
        tracked = self._candidate_monotonic_started.get(group_id)
        if tracked is None or tracked[0] != epoch or current_tick < tracked[1]:
            self._candidate_monotonic_started[group_id] = (epoch, current_tick)
            return False

        settle_seconds = float(group.get("node_settle_window_seconds", 120))
        return current_tick - tracked[1] >= settle_seconds

    async def _evaluate_candidate_fallback(self, group: Record) -> Record:
        """Reference implementation for stores without an atomic evaluator.

        Production stores should implement ``evaluate_group_candidate`` in one
        SQLite transaction. This fallback keeps the engine independently testable.
        """

        now = _aware(self.now()) or self.now()
        members = [_record(item) for item in group.get("members", [])]
        required = {str(item["agent_id"]) for item in members}
        agents = {
            str(item["agent_id"]): _record(item)
            for item in await self._store_call("list_agents")
        }
        missing = sorted(
            agent_id
            for agent_id in required
            if agent_id not in agents
            or str(_value(agents[agent_id].get("online_status")))
            != AgentOnlineStatus.ONLINE.value
            or not agents[agent_id].get("boot_id")
        )
        if missing:
            await self._set_group_schedule(
                group["group_id"],
                state=GroupState.WAITING_FOR_NODES.value,
                missing_agent_ids=missing,
                candidate_epoch=None,
                candidate_stable_since=None,
                settle_deadline_at=None,
            )
            return {"decision": "WAITING_FOR_NODES", "missing_agent_ids": missing}

        if not required:
            await self._set_group_schedule(
                group["group_id"],
                state=GroupState.BLOCKED_PRECONDITION.value,
                missing_agent_ids=[],
            )
            return {
                "decision": "BLOCKED",
                "errors": [{"code": "GROUP_NOT_READY", "message": "group has no members"}],
            }

        epoch = canonical_epoch(
            group["group_id"],
            ((agent_id, agents[agent_id]["boot_id"]) for agent_id in required),
        )
        baseline = group.get("baseline_epoch")
        last_scheduled = group.get("last_scheduled_epoch")
        if epoch in {baseline, last_scheduled}:
            await self._set_group_schedule(
                group["group_id"],
                state=GroupState.ARMED_IDLE.value,
                missing_agent_ids=[],
                candidate_epoch=None,
                candidate_stable_since=None,
                settle_deadline_at=None,
            )
            return {"decision": "UNCHANGED", "epoch": epoch}

        candidate = group.get("candidate_epoch")
        stable_since = _aware(group.get("candidate_stable_since"))
        window = int(group.get("node_settle_window_seconds", 120))
        if candidate != epoch or stable_since is None:
            deadline = now + timedelta(seconds=window)
            await self._set_group_schedule(
                group["group_id"],
                state=GroupState.SETTLING.value,
                missing_agent_ids=[],
                candidate_epoch=epoch,
                candidate_stable_since=now,
                settle_deadline_at=deadline,
            )
            return {
                "decision": "SETTLING",
                "epoch": epoch,
                "candidate_stable_since": now,
                "settle_deadline_at": deadline,
            }

        deadline = stable_since + timedelta(seconds=window)
        if now < deadline:
            await self._set_group_schedule(
                group["group_id"],
                state=GroupState.SETTLING.value,
                missing_agent_ids=[],
                candidate_epoch=epoch,
                candidate_stable_since=stable_since,
                settle_deadline_at=deadline,
            )
            return {"decision": "SETTLING", "epoch": epoch, "settle_deadline_at": deadline}

        errors = await self._validate_preconditions(group["group_id"], group)
        if errors:
            await self._set_group_schedule(
                group["group_id"],
                state=GroupState.BLOCKED_PRECONDITION.value,
                missing_agent_ids=[],
                candidate_epoch=epoch,
                candidate_stable_since=stable_since,
                settle_deadline_at=deadline,
            )
            return {"decision": "BLOCKED", "epoch": epoch, "errors": errors}
        return {"decision": "READY", "epoch": epoch}

    async def _set_group_schedule(self, group_id: Any, **fields: Any) -> None:
        method = (
            getattr(self.store, "set_group_schedule", None)
            or getattr(self.store, "update_group_schedule", None)
            or getattr(self.store, "update_group_runtime", None)
        )
        if method is None:
            raise AttributeError(
                "fallback candidate evaluation requires store.set_group_schedule"
            )
        await _await_if_needed(method(group_id, **fields))

    async def _validate_preconditions(
        self, group_id: Any, group: Record | None = None
    ) -> list[Record]:
        validator = getattr(self.store, "validate_group_preconditions", None)
        if validator is not None:
            return [
                _record(item)
                for item in await _await_if_needed(validator(group_id, now=self.now()))
            ]

        group = group or _record(await self._store_call("get_group", group_id))
        errors: list[Record] = []
        members = [_record(item) for item in group.get("members", [])]
        try:
            _logical_kahn_levels(
                (item["managed_service_id"] for item in members),
                group.get("dependencies", []),
            )
        except (ValueError, DependencyCycleError) as exc:
            errors.append({"code": "DEPENDENCY_CYCLE", "message": str(exc)})
        for member in members:
            service = _record(
                await self._store_call("get_service", member["managed_service_id"])
            )
            if str(_value(service.get("agent_online_status"))) != AgentOnlineStatus.ONLINE.value:
                errors.append({"code": "AGENT_OFFLINE", "service": member["managed_service_id"]})
            if str(_value(service.get("installation_state"))) != InstallationState.INSTALLED.value:
                errors.append(
                    {"code": "SERVICE_NOT_INSTALLED", "service": member["managed_service_id"]}
                )
            if str(_value(service.get("startup_state"))) != StartupState.AUTOSTART_DISABLED.value:
                errors.append({"code": "GROUP_NOT_READY", "service": member["managed_service_id"]})
        return errors

    async def _create_run(
        self,
        group_id: Any,
        *,
        trigger: str,
        epoch: str | None = None,
        reason: str | None = None,
        retry_of_run_id: Any | None = None,
    ) -> Record | None:
        method = getattr(self.store, "create_run_from_group", None) or getattr(
            self.store, "create_run", None
        )
        if method is None:
            raise AttributeError("store does not implement run creation")
        try:
            result = await _await_if_needed(
                method(
                    group_id,
                    trigger=trigger,
                    epoch=epoch,
                    reason=reason,
                    retry_of_run_id=retry_of_run_id,
                )
            )
        except RecoveryEngineError:
            raise
        return _record(result) if result else None

    async def create_manual_run(self, group_id: UUID | str, reason: str | None = None) -> Record:
        run = await self._create_run(
            group_id, trigger=RunTrigger.MANUAL.value, reason=reason
        )
        if not run:
            raise RecoveryEngineError("GROUP_NOT_READY", "manual run could not be created")
        self.launch_run(run["run_id"])
        return run

    async def retry_run(self, run_id: UUID | str, reason: str | None = None) -> Record:
        retry_method = getattr(self.store, "retry_run", None)
        if retry_method is not None:
            result = await _await_if_needed(retry_method(run_id, reason=reason, now=self.now()))
            run = _record(result)
        else:
            parent = _record(await self._store_call("get_run", run_id))
            if not parent:
                raise RecoveryEngineError("RUN_NOT_FOUND", "parent run does not exist")
            run = await self._create_run(
                parent["group_id"],
                trigger=RunTrigger.MANUAL.value,
                reason=reason,
                retry_of_run_id=run_id,
            ) or {}
        if not run:
            raise RecoveryEngineError("GROUP_NOT_READY", "retry run could not be created")
        self.launch_run(run["run_id"])
        return run

    def launch_run(self, run_id: UUID | str) -> asyncio.Task[Any]:
        key = str(run_id)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self.execute_run(run_id), name=f"recovery-run-{key}")
        self._tasks[key] = task

        def discard(completed: asyncio.Task[Any]) -> None:
            if not completed.cancelled():
                failure = completed.exception()
                if failure is not None:
                    LOGGER.error(
                        "recovery Run task failed and remains eligible for durable reconciliation",
                        exc_info=(type(failure), failure, failure.__traceback__),
                        extra={"run_id": key},
                    )
            if self._tasks.get(key) is completed:
                self._tasks.pop(key, None)

        task.add_done_callback(discard)
        return task

    async def resume_unfinished_runs(self) -> list[asyncio.Task[Any]]:
        method = getattr(self.store, "list_unfinished_runs", None) or getattr(
            self.store, "list_active_runs", None
        )
        if method is None:
            raise AttributeError("store does not expose unfinished runs")
        runs = await _await_if_needed(method())
        return [self.launch_run(_record(run)["run_id"]) for run in runs]

    async def wait_for_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=False)

    async def scheduler_loop(
        self, stop_event: asyncio.Event, *, interval_seconds: float = 1.0
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        consecutive_failures = 0
        while not stop_event.is_set():
            try:
                # A Run task may have failed after its durable state was
                # updated. Relaunching by run_id is safe because Step dispatch
                # keys and Agent Operations are reconciled rather than replayed.
                await self.resume_unfinished_runs()
                await self.scan_auto_groups()
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                LOGGER.exception(
                    "recovery scheduler iteration failed; retrying from durable state"
                )
                delay = min(
                    60.0,
                    interval_seconds * (2 ** min(consecutive_failures - 1, 6)),
                )
            else:
                consecutive_failures = 0
                delay = interval_seconds
            if stop_event.is_set():
                break
            await self.sleep(delay)

    async def execute_run(self, run_id: UUID | str) -> Record:
        run_key = str(run_id)
        async with self._run_guards[run_key]:
            run = _record(await self._store_call("get_run", run_id))
            if not run:
                raise RecoveryEngineError("RUN_NOT_FOUND", "recovery run does not exist")
            if _status(run) in {
                RecoveryRunStatus.SUCCEEDED.value,
                RecoveryRunStatus.FAILED.value,
                RecoveryRunStatus.UNKNOWN.value,
            }:
                return run

            await self._update_run(run, RecoveryRunStatus.RUNNING.value, started_at=run.get("started_at") or self.now())
            members = [_record(item) for item in run.get("members_snapshot", [])]
            dependencies = list(run.get("dependencies_snapshot", []))
            member_ids = [str(item["managed_service_id"]) for item in members]
            logical_levels = _logical_kahn_levels(member_ids, dependencies)
            steps = {_service_id(step): _record(step) for step in run.get("steps", [])}
            if set(steps) != set(member_ids):
                await self._update_run(
                    run,
                    RecoveryRunStatus.UNKNOWN.value,
                    failure_message="run snapshot and durable steps do not match",
                    finished_at=self.now(),
                )
                await self._release_run(run_id)
                return _record(await self._store_call("get_run", run_id))

            prereqs: dict[str, list[str]] = defaultdict(list)
            for edge in dependencies:
                dependent, prerequisite = _dependency_pair(edge)
                prereqs[dependent].append(prerequisite)
            probes = {
                str(_record(item)["managed_service_id"]): _record(item)
                for item in run.get("probes_snapshot", [])
            }
            member_map = {str(item["managed_service_id"]): item for item in members}
            configured_parallelism = run.get("max_parallel_services")
            if configured_parallelism is None:
                current_group = _record(
                    await self._store_call("get_group", run["group_id"])
                )
                configured_parallelism = current_group.get("max_parallel_services", 4)
            limit = min(4, max(1, int(configured_parallelism)))
            semaphore = asyncio.Semaphore(limit)

            try:
                for services in logical_levels:
                    runnable: list[str] = []
                    for service_id in services:
                        step = steps[service_id]
                        if _status(step) in {
                            RecoveryStepStatus.READY.value,
                            RecoveryStepStatus.FAILED.value,
                            RecoveryStepStatus.BLOCKED.value,
                            RecoveryStepStatus.UNKNOWN.value,
                        }:
                            continue
                        blocking = [
                            steps[parent]
                            for parent in prereqs.get(service_id, [])
                            if _status(steps[parent]) != RecoveryStepStatus.READY.value
                        ]
                        if blocking:
                            cause = _choose_blocking_cause(blocking)
                            root_id, chain = _root_cause(cause)
                            await self._update_step(
                                step,
                                RecoveryStepStatus.BLOCKED.value,
                                root_cause_step_id=root_id,
                                dependency_chain=chain,
                                message=f"blocked by prerequisite step {cause['step_id']}",
                                finished_at=self.now(),
                            )
                        else:
                            if prereqs.get(service_id) and _status(step) == RecoveryStepStatus.PENDING.value:
                                await self._update_step(
                                    step, RecoveryStepStatus.WAITING_DEPENDENCY.value
                                )
                            runnable.append(service_id)

                    async def run_one(service_id: str) -> None:
                        async with semaphore:
                            step = steps[service_id]
                            try:
                                await self._execute_step(
                                    run,
                                    step,
                                    member_map[service_id],
                                    probes.get(service_id),
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                # Unexpected adapter/network failures are not proof
                                # that an Agent side effect did or did not occur.
                                await self._update_step(
                                    step,
                                    RecoveryStepStatus.UNKNOWN.value,
                                    message="step outcome could not be determined",
                                    finished_at=self.now(),
                                )

                    await asyncio.gather(*(run_one(service_id) for service_id in runnable))

                refreshed = _record(await self._store_call("get_run", run_id))
                final = _final_run_status([_record(item) for item in refreshed.get("steps", [])])
                if final is None:
                    final = RecoveryRunStatus.UNKNOWN.value
                failure_message = None
                failure_code = None
                if final == RecoveryRunStatus.UNKNOWN.value:
                    if any(
                        _is_agent_protocol_mismatch(_record(item))
                        for item in refreshed.get("steps", [])
                    ):
                        failure_code = ErrorCode.AGENT_PROTOCOL_MISMATCH.value
                        failure_message = "Agent Operation semantic binding mismatch"
                    else:
                        failure_message = "one or more step outcomes are unknown"
                elif final == RecoveryRunStatus.FAILED.value:
                    failure_message = "one or more steps failed or were blocked"
                await self._update_run(
                    refreshed,
                    final,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    finished_at=self.now(),
                )
                await self._release_run(run_id)
                return _record(await self._store_call("get_run", run_id))
            except asyncio.CancelledError:
                # Durable RUNNING/STARTING/PROBING state is intentionally left
                # intact. The next CP process resumes this exact run.
                raise

    async def _execute_step(
        self, run: Record, step: Record, member: Record, probe_snapshot: Record | None
    ) -> None:
        service_id = str(member["managed_service_id"])
        async with self._service_guards[service_id]:
            status = _status(step)
            if status == RecoveryStepStatus.PROBING.value:
                await self._probe_until_ready(step, member, probe_snapshot)
                return
            if status == RecoveryStepStatus.STARTING.value or step.get("operation_id") or step.get(
                "dispatch_idempotency_key"
            ):
                outcome = await self._resume_or_dispatch_start(step, member)
                if outcome == RecoveryStepStatus.PROBING.value:
                    await self._probe_until_ready(step, member, probe_snapshot)
                return

            service = await self._current_service(member)
            runtime = str(_value(service.get("runtime_state")))
            installation = str(_value(service.get("installation_state")))
            online = str(_value(service.get("agent_online_status", AgentOnlineStatus.ONLINE.value)))
            if online != AgentOnlineStatus.ONLINE.value:
                await self._update_step(
                    step,
                    RecoveryStepStatus.UNKNOWN.value,
                    message="target agent became unavailable",
                    finished_at=self.now(),
                )
                return
            if installation != InstallationState.INSTALLED.value:
                await self._update_step(
                    step,
                    RecoveryStepStatus.FAILED.value,
                    message="service is no longer installed",
                    finished_at=self.now(),
                )
                return
            if runtime == RuntimeState.ACTIVE.value:
                await self._update_step(step, RecoveryStepStatus.PROBING.value)
                await self._probe_until_ready(step, member, probe_snapshot)
                return
            if runtime == RuntimeState.UNKNOWN.value:
                await self._update_step(
                    step,
                    RecoveryStepStatus.UNKNOWN.value,
                    message="service runtime state is unknown",
                    finished_at=self.now(),
                )
                return
            if runtime not in {
                RuntimeState.INACTIVE.value,
                RuntimeState.FAILED.value,
                RuntimeState.STARTING.value,
            }:
                await self._update_step(
                    step,
                    RecoveryStepStatus.FAILED.value,
                    message=f"service cannot be started from {runtime or 'an invalid state'}",
                    finished_at=self.now(),
                )
                return

            await self._update_step(
                step,
                RecoveryStepStatus.STARTING.value,
                started_at=step.get("started_at") or self.now(),
            )
            outcome = await self._resume_or_dispatch_start(step, member)
            if outcome == RecoveryStepStatus.PROBING.value:
                await self._probe_until_ready(step, member, probe_snapshot)

    async def _current_service(self, member: Record) -> Record:
        service = _record(
            await self._store_call("get_service", member["managed_service_id"])
        )
        if not service:
            raise RecoveryEngineError("GROUP_NOT_READY", "managed service disappeared")
        return service

    async def _member_endpoint(self, member: Record) -> str:
        service = await self._current_service(member)
        endpoint = service.get("agent_endpoint") or service.get("endpoint")
        if not endpoint:
            nested = _record(service.get("agent")) if service.get("agent") else {}
            endpoint = nested.get("endpoint")
        if not endpoint:
            get_agent = getattr(self.store, "get_agent", None)
            if get_agent is not None:
                agent = _record(await _await_if_needed(get_agent(member["agent_id"])))
                endpoint = agent.get("endpoint")
        if not endpoint:
            raise RecoveryEngineError("AGENT_OFFLINE", "target Agent endpoint is unavailable")
        return str(endpoint)

    async def _resume_or_dispatch_start(self, step: Record, member: Record) -> str:
        endpoint = await self._member_endpoint(member)
        operation_id = step.get("operation_id")
        if operation_id:
            return await self._reconcile_operation(
                step, member, endpoint, operation_id
            )

        dispatch_key = step.get("dispatch_idempotency_key")
        if not dispatch_key:
            proposed = uuid4()
            dispatch_key = await self._store_call(
                ("assign_step_dispatch", "set_step_dispatch_key"), step["step_id"], proposed
            )
            dispatch_key = dispatch_key or proposed
            step["dispatch_idempotency_key"] = dispatch_key

        started = self.monotonic()
        while True:
            try:
                operation = await self.agent_client.start_action(
                    endpoint,
                    str(member["local_service_id"]),
                    "start",
                    dispatch_key,
                )
                operation_record = _record(operation)
                operation_id = operation_record.get("operation_id")
                mismatch = _start_operation_binding_mismatch(
                    operation_record,
                    step,
                    member,
                    expected_operation_id=None,
                )
                if mismatch is not None:
                    return await self._mark_agent_protocol_mismatch(step, mismatch)
                durable_id = await self._store_call(
                    ("assign_step_operation", "set_step_operation_id"),
                    step["step_id"],
                    operation_id,
                )
                operation_id = durable_id or operation_id
                step["operation_id"] = operation_id
                return await self._reconcile_operation(
                    step,
                    member,
                    endpoint,
                    operation_id,
                    initial=operation_record,
                )
            except asyncio.CancelledError:
                raise
            except AgentOperationProtocolError:
                return await self._mark_agent_protocol_mismatch(step, "response_schema")
            except Exception:
                if self.monotonic() - started >= self.operation_poll_deadline_seconds:
                    await self._update_step(
                        step,
                        RecoveryStepStatus.UNKNOWN.value,
                        message="start dispatch outcome could not be confirmed",
                        finished_at=self.now(),
                    )
                    return RecoveryStepStatus.UNKNOWN.value
                await self.sleep(self.operation_poll_interval_seconds)

    async def _reconcile_operation(
        self,
        step: Record,
        member: Record,
        endpoint: str,
        operation_id: Any,
        *,
        initial: Record | None = None,
    ) -> str:
        started = self.monotonic()
        operation = initial
        while True:
            try:
                if operation is None:
                    operation = _record(
                        await self.agent_client.get_operation(endpoint, operation_id)
                    )
                mismatch = _start_operation_binding_mismatch(
                    operation,
                    step,
                    member,
                    expected_operation_id=operation_id,
                )
                if mismatch is not None:
                    return await self._mark_agent_protocol_mismatch(step, mismatch)
                status = _status(operation)
                if status == OperationStatus.SUCCEEDED.value:
                    await self._update_step(step, RecoveryStepStatus.PROBING.value)
                    return RecoveryStepStatus.PROBING.value
                if status in {OperationStatus.FAILED.value, OperationStatus.REJECTED.value}:
                    await self._update_step(
                        step,
                        RecoveryStepStatus.FAILED.value,
                        message=str(operation.get("message") or f"start Operation {status.lower()}"),
                        finished_at=self.now(),
                    )
                    return RecoveryStepStatus.FAILED.value
                if status == OperationStatus.UNKNOWN.value:
                    await self._update_step(
                        step,
                        RecoveryStepStatus.UNKNOWN.value,
                        message="Agent reported an unknown start outcome",
                        finished_at=self.now(),
                    )
                    return RecoveryStepStatus.UNKNOWN.value
                if status not in {OperationStatus.PENDING.value, OperationStatus.RUNNING.value}:
                    raise ValueError("Agent returned an invalid Operation status")
            except asyncio.CancelledError:
                raise
            except AgentOperationProtocolError:
                return await self._mark_agent_protocol_mismatch(step, "response_schema")
            except Exception:
                operation = None

            if self.monotonic() - started >= self.operation_poll_deadline_seconds:
                await self._update_step(
                    step,
                    RecoveryStepStatus.UNKNOWN.value,
                    message="start Operation could not be reconciled before deadline",
                    finished_at=self.now(),
                )
                return RecoveryStepStatus.UNKNOWN.value
            await self.sleep(self.operation_poll_interval_seconds)
            operation = None

    async def _mark_agent_protocol_mismatch(self, step: Record, field: str) -> str:
        await self._update_step(
            step,
            RecoveryStepStatus.UNKNOWN.value,
            message=(
                f"{_AGENT_PROTOCOL_MISMATCH_PREFIX} Agent Operation field "
                f"{field} did not match the persisted start dispatch"
            ),
            finished_at=self.now(),
        )
        return RecoveryStepStatus.UNKNOWN.value

    async def _probe_until_ready(
        self, step: Record, member: Record, probe_snapshot: Record | None
    ) -> None:
        definition, fallback = _probe_definition(probe_snapshot)
        interval = float(definition.pop("interval_seconds", 3))
        deadline = float(definition.pop("deadline_seconds", 60))
        request = _make_probe_request(definition, str(member["local_service_id"]))
        warnings = list(step.get("warnings") or [])
        if fallback and "READINESS_FALLBACK_SCM" not in warnings:
            warnings.append("READINESS_FALLBACK_SCM")
            await self._update_step(step, RecoveryStepStatus.PROBING.value, warnings=warnings)

        attempts = list(step.get("probe_attempts") or step.get("attempts") or [])
        if attempts:
            latest = _record(attempts[-1])
            latest_result = _record(latest.get("result"))
            first_started = _aware(_record(attempts[0]).get("started_at"))
            latest_finished = _aware(latest.get("finished_at"))
            if latest_result.get("passed") is True:
                # The attempt is committed before the terminal Step update.  A CP
                # crash in that window must not turn a success which completed
                # after the total readiness deadline into READY on restart.
                # Persisted attempt timestamps are the durable evidence available
                # to distinguish an on-time success from a late one.  Missing or
                # backwards timestamps cannot prove an on-time result, so fail
                # closed instead of releasing strict downstream dependencies.
                completed_elapsed = (
                    (latest_finished - first_started).total_seconds()
                    if first_started is not None and latest_finished is not None
                    else -1.0
                )
                if 0.0 <= completed_elapsed <= deadline:
                    await self._update_step(
                        step,
                        RecoveryStepStatus.READY.value,
                        message="readiness probe passed",
                        finished_at=self.now(),
                    )
                else:
                    await self._update_step(
                        step,
                        RecoveryStepStatus.FAILED.value,
                        message="readiness probe completed after its deadline",
                        finished_at=self.now(),
                    )
                return
        else:
            latest_result = {}
            # For a service that needed starting, step.started_at belongs to the
            # STARTING phase.  The readiness deadline starts when PROBING was
            # persisted, represented by updated_at until the first attempt is
            # durably recorded.
            first_started = _aware(step.get("updated_at")) or _aware(step.get("started_at"))
        persisted_elapsed = (
            max(
                0.0,
                ((_aware(self.now()) or self.now()) - first_started).total_seconds(),
            )
            if first_started
            else 0.0
        )
        if persisted_elapsed >= deadline:
            await self._update_step(
                step,
                RecoveryStepStatus.FAILED.value,
                message=str(
                    latest_result.get("message")
                    or "readiness deadline elapsed before probing could resume"
                ),
                finished_at=self.now(),
            )
            return
        started_mono = self.monotonic() - persisted_elapsed
        endpoint = await self._member_endpoint(member)
        attempt_number = len(attempts) + 1

        while True:
            elapsed_before_attempt = self.monotonic() - started_mono
            if attempts and elapsed_before_attempt >= deadline:
                await self._update_step(
                    step,
                    RecoveryStepStatus.FAILED.value,
                    message=str(
                        latest_result.get("message")
                        or "readiness deadline elapsed before the next attempt"
                    ),
                    finished_at=self.now(),
                )
                return
            attempt_started_at = self.now()
            attempt_started_mono = self.monotonic()
            try:
                raw_result = await self.agent_client.probe(endpoint, request)
                result = _record(raw_result)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = ProbeResult(
                    passed=False,
                    observed_at=self.now(),
                    latency_ms=max(0, int((self.monotonic() - attempt_started_mono) * 1000)),
                    code="PROBE_FAILED",
                    message="probe request could not be confirmed",
                ).model_dump(mode="python")
            attempt_finished_at = self.now()
            attempt = {
                "attempt": attempt_number,
                "started_at": attempt_started_at,
                "finished_at": attempt_finished_at,
                "result": result,
            }
            await self._record_probe_attempt(step, attempt)
            durable_attempts = list(step.get("probe_attempts") or step.get("attempts") or [])
            if durable_attempts and _record(durable_attempts[-1]).get("attempt") == attempt_number:
                attempts = durable_attempts
            else:
                attempts.append(attempt)
            step["probe_attempts"] = attempts
            elapsed = self.monotonic() - started_mono
            if bool(result.get("passed")) and elapsed <= deadline:
                await self._update_step(
                    step,
                    RecoveryStepStatus.READY.value,
                    message="readiness probe passed",
                    finished_at=self.now(),
                )
                return
            if elapsed >= deadline:
                await self._update_step(
                    step,
                    RecoveryStepStatus.FAILED.value,
                    message=(
                        "readiness probe completed after its deadline"
                        if bool(result.get("passed"))
                        else str(result.get("message") or "readiness probe deadline exceeded")
                    ),
                    finished_at=self.now(),
                )
                return
            await self.sleep(min(interval, deadline - elapsed))
            attempt_number += 1

    async def _record_probe_attempt(self, step: Record, attempt: Record) -> None:
        method = getattr(self.store, "record_probe_attempt", None) or getattr(
            self.store, "add_probe_attempt", None
        )
        if method is None:
            raise AttributeError("store does not persist probe attempts")
        try:
            await _await_if_needed(method(step["step_id"], **attempt))
        except TypeError:
            await _await_if_needed(method(step["step_id"], attempt))

    async def _update_step(self, step: Record, status: str, **fields: Any) -> None:
        persistent_fields = {
            key: value
            for key, value in fields.items()
            if key in {"message", "warnings", "root_cause_step_id", "dependency_chain"}
        }
        method = getattr(self.store, "update_step")
        if "now" in inspect.signature(method).parameters:
            persistent_fields["now"] = self.now()
        await _await_if_needed(method(step["step_id"], status=status, **persistent_fields))
        step["status"] = status
        step.update(fields)
        step["updated_at"] = self.now()

    async def _update_run(self, run: Record, status: str, **fields: Any) -> None:
        persistent_fields = {
            key: value
            for key, value in fields.items()
            if key in {"failure_code", "failure_message"}
        }
        method = getattr(self.store, "update_run")
        if "now" in inspect.signature(method).parameters:
            persistent_fields["now"] = self.now()
        await _await_if_needed(method(run["run_id"], status=status, **persistent_fields))
        run["status"] = status
        run.update(fields)
        run["updated_at"] = self.now()

    async def _release_run(self, run_id: Any) -> None:
        await self._store_call(("release_run_locks", "release_service_locks"), run_id)


def _service_id(step: Any) -> str:
    return str(_record(step)["managed_service_id"])


def _choose_blocking_cause(steps: Sequence[Record]) -> Record:
    priority = {
        RecoveryStepStatus.UNKNOWN.value: 0,
        RecoveryStepStatus.FAILED.value: 1,
        RecoveryStepStatus.BLOCKED.value: 2,
    }
    return min(steps, key=lambda item: (priority.get(_status(item), 3), str(item["step_id"])))


def _root_cause(step: Record) -> tuple[Any, list[Any]]:
    root = step.get("root_cause_step_id") or step["step_id"]
    chain = list(step.get("dependency_chain") or [])
    if not chain:
        chain = [step["step_id"]]
    elif chain[-1] != step["step_id"]:
        chain.append(step["step_id"])
    return root, chain


def _final_run_status(steps: Sequence[Record]) -> str | None:
    statuses = {_status(step) for step in steps}
    if RecoveryStepStatus.UNKNOWN.value in statuses:
        return RecoveryRunStatus.UNKNOWN.value
    if statuses & {RecoveryStepStatus.FAILED.value, RecoveryStepStatus.BLOCKED.value}:
        return RecoveryRunStatus.FAILED.value
    if statuses and statuses == {RecoveryStepStatus.READY.value}:
        return RecoveryRunStatus.SUCCEEDED.value
    return None


def _probe_definition(snapshot: Record | None) -> tuple[Record, bool]:
    if not snapshot:
        return {
            "kind": "scm",
            "timeout_seconds": 2,
            "interval_seconds": 3,
            "deadline_seconds": 60,
        }, True
    definition = snapshot.get("definition", snapshot)
    return dict(_record(definition)), False


def _make_probe_request(definition: Record, local_service_id: str) -> Any:
    kind = definition.get("kind")
    if kind == "scm":
        return ScmProbeRequest(
            kind="scm",
            local_service_id=local_service_id,
            timeout_seconds=definition.get("timeout_seconds", 2),
        )
    if kind == "tcp":
        return TcpProbeRequest(
            kind="tcp",
            host=definition["host"],
            port=definition["port"],
            timeout_seconds=definition.get("timeout_seconds", 2),
        )
    if kind == "http":
        return HttpProbeRequest(
            kind="http",
            url=definition["url"],
            expected_status=definition.get("expected_status", 200),
            body_contains=definition.get("body_contains"),
            timeout_seconds=definition.get("timeout_seconds", 2),
        )
    raise RecoveryEngineError("PROBE_UNSUPPORTED", f"unsupported readiness probe: {kind!r}")


__all__ = [
    "AgentClient",
    "RecoveryEngine",
    "RecoveryEngineError",
    "RecoveryStore",
    "TopologyBatch",
    "canonical_epoch",
    "kahn_layers",
]
