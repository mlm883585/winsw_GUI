from __future__ import annotations

import asyncio
import hashlib
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx

from orchestrator.common.enums import (
    AgentOnlineStatus,
    GroupState,
    InstallationState,
    OperationStatus,
    RecoveryRunStatus,
    RecoveryStepStatus,
    RuntimeState,
    StartupState,
)
from orchestrator.common.errors import ErrorCode
from orchestrator.common.graph import DependencyCycleError
from orchestrator.common.models import ProbeResult
from orchestrator.common.security import canonical_request_fingerprint
from orchestrator.control_plane.agent_client import AgentClient as HttpAgentClient
from orchestrator.control_plane.recovery import (
    RecoveryEngine,
    canonical_epoch,
    kahn_layers,
)


class FakeClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
        self.ticks = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.ticks

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.ticks += seconds
        self.wall += timedelta(seconds=seconds)


def member(service_id: UUID, agent_id: UUID, local_id: str) -> dict[str, Any]:
    return {
        "managed_service_id": str(service_id),
        "agent_id": str(agent_id),
        "local_service_id": local_id,
        "windows_service_name": f"svc-{local_id}",
        "display_name": local_id.title(),
    }


def dependency(dependent: UUID, prerequisite: UUID) -> dict[str, str]:
    return {
        "managed_service_id": str(dependent),
        "prerequisite_managed_service_id": str(prerequisite),
    }


def explicit_scm_probe(service_id: UUID, *, deadline: float = 5) -> dict[str, Any]:
    return {
        "probe_id": str(uuid4()),
        "managed_service_id": str(service_id),
        "definition": {
            "kind": "scm",
            "timeout_seconds": 1,
            "interval_seconds": 1,
            "deadline_seconds": deadline,
        },
    }


def wire_operation(
    item: dict[str, Any], operation_id: UUID, dispatch_key: UUID, now: datetime
) -> dict[str, Any]:
    local_service_id = str(item["local_service_id"])
    return {
        "operation_id": str(operation_id),
        "agent_id": str(item["agent_id"]),
        "local_service_id": local_service_id,
        "windows_service_name": str(item["windows_service_name"]),
        "action": "start",
        "status": "SUCCEEDED",
        "idempotency_key": str(dispatch_key),
        "request_fingerprint": canonical_request_fingerprint(
            "POST",
            f"/api/v1/services/{local_service_id}/actions/start",
            {},
        ),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "started_at": now.isoformat(),
        "finished_at": now.isoformat(),
        "error_code": None,
        "message": None,
    }


class FakeStore:
    """Small durable-store double; every returned record is the durable object."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.groups: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.services: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.preflight_errors: list[dict[str, Any]] = []
        self.auto_keys: set[tuple[str, str]] = set()
        self.locked_services: dict[str, str] = {}
        self.cancel_after_assign_operation = False

    def list_groups(self) -> list[dict[str, Any]]:
        return list(self.groups.values())

    def get_group(self, group_id: Any) -> dict[str, Any] | None:
        return self.groups.get(str(group_id))

    def list_agents(self) -> list[dict[str, Any]]:
        return list(self.agents.values())

    def set_group_schedule(self, group_id: Any, **fields: Any) -> None:
        self.groups[str(group_id)].update(fields)

    def validate_group_preconditions(
        self, group_id: Any, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        return list(self.preflight_errors)

    def create_run_from_group(
        self,
        group_id: Any,
        trigger: str,
        epoch: str | None = None,
        reason: str | None = None,
        retry_of_run_id: Any | None = None,
    ) -> dict[str, Any] | None:
        group = self.groups[str(group_id)]
        if self.preflight_errors:
            group["state"] = GroupState.BLOCKED_PRECONDITION.value
            return None
        if trigger == "AUTO":
            key = (str(group_id), str(epoch))
            if key in self.auto_keys:
                return None
            self.auto_keys.add(key)
        for item in group["members"]:
            service_id = str(item["managed_service_id"])
            if service_id in self.locked_services:
                return None

        run_id = str(uuid4())
        steps: list[dict[str, Any]] = []
        for item in group["members"]:
            step = {
                "step_id": str(uuid4()),
                "managed_service_id": str(item["managed_service_id"]),
                "agent_id": str(item["agent_id"]),
                "local_service_id": item["local_service_id"],
                "status": RecoveryStepStatus.PENDING.value,
                "topology_level": 0,
                "dispatch_idempotency_key": None,
                "operation_id": None,
                "probe_attempts": [],
                "warnings": [],
                "root_cause_step_id": None,
                "dependency_chain": [],
                "message": None,
                "created_at": self.clock.now(),
                "started_at": None,
                "finished_at": None,
                "updated_at": self.clock.now(),
            }
            steps.append(step)
            self.locked_services[str(item["managed_service_id"])] = run_id
        run = {
            "run_id": run_id,
            "group_id": str(group_id),
            "trigger": trigger,
            "epoch": epoch,
            "retry_of_run_id": str(retry_of_run_id) if retry_of_run_id else None,
            "status": RecoveryRunStatus.PENDING.value,
            "reason": reason,
            "members_snapshot": deepcopy(group["members"]),
            "dependencies_snapshot": deepcopy(group.get("dependencies", [])),
            "probes_snapshot": deepcopy(group.get("probes", [])),
            "max_parallel_services": group.get("max_parallel_services", 4),
            "steps": steps,
            "failure_code": None,
            "failure_message": None,
            "created_at": self.clock.now(),
            "started_at": None,
            "finished_at": None,
            "updated_at": self.clock.now(),
        }
        self.runs[run_id] = run
        group["state"] = GroupState.RUNNING.value
        if trigger == "AUTO":
            group["last_scheduled_epoch"] = epoch
        return run

    def get_run(self, run_id: Any) -> dict[str, Any] | None:
        return self.runs.get(str(run_id))

    def list_unfinished_runs(self) -> list[dict[str, Any]]:
        return [
            run
            for run in self.runs.values()
            if run["status"] not in {"SUCCEEDED", "FAILED", "UNKNOWN"}
        ]

    def get_service(self, managed_service_id: Any) -> dict[str, Any] | None:
        return self.services.get(str(managed_service_id))

    def update_run(self, run_id: Any, status: str, **fields: Any) -> None:
        run = self.runs[str(run_id)]
        run.update(fields, status=status, updated_at=self.clock.now())
        if status in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            self.groups[run["group_id"]]["state"] = GroupState.ARMED_IDLE.value

    def update_step(self, step_id: Any, status: str, **fields: Any) -> None:
        step = self._step(step_id)
        step.update(fields, status=status, updated_at=self.clock.now())

    def assign_step_dispatch(self, step_id: Any, key: Any) -> Any:
        step = self._step(step_id)
        if step["dispatch_idempotency_key"] is None:
            step["dispatch_idempotency_key"] = key
        return step["dispatch_idempotency_key"]

    def assign_step_operation(self, step_id: Any, operation_id: Any) -> Any:
        step = self._step(step_id)
        if step["operation_id"] is None:
            step["operation_id"] = operation_id
        if self.cancel_after_assign_operation:
            self.cancel_after_assign_operation = False
            raise asyncio.CancelledError
        return step["operation_id"]

    def record_probe_attempt(self, step_id: Any, **attempt: Any) -> None:
        self._step(step_id)["probe_attempts"].append(deepcopy(attempt))

    def release_run_locks(self, run_id: Any) -> None:
        for service_id, owner in list(self.locked_services.items()):
            if owner == str(run_id):
                del self.locked_services[service_id]

    def _step(self, step_id: Any) -> dict[str, Any]:
        for run in self.runs.values():
            for step in run["steps"]:
                if str(step["step_id"]) == str(step_id):
                    return step
        raise KeyError(step_id)


class FakeAgentClient:
    def __init__(
        self,
        clock: FakeClock,
        store: FakeStore,
        *,
        shared_dispatches: dict[str, dict[str, Any]] | None = None,
        shared_side_effect_keys: list[str] | None = None,
    ) -> None:
        self.clock = clock
        self.store = store
        self.action_status_by_local_id: dict[str, str] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.probe_results_by_endpoint: dict[str, list[bool]] = {}
        self.action_calls: list[tuple[str, str, str, Any]] = []
        self.probe_calls: list[str] = []
        self.cancel_next_action = False
        self.cancel_after_accept = False
        self.get_operation_calls: list[str] = []
        self.post_operation_overrides_by_local_id: dict[str, dict[str, Any]] = {}
        self.get_operation_overrides_by_id: dict[str, dict[str, Any]] = {}
        self.shared_dispatches = (
            shared_dispatches if shared_dispatches is not None else {}
        )
        self.shared_side_effect_keys = (
            shared_side_effect_keys if shared_side_effect_keys is not None else []
        )

    async def start_action(
        self, endpoint: str, local_service_id: str, action: str, idempotency_key: Any
    ) -> dict[str, Any]:
        self.action_calls.append((endpoint, local_service_id, action, idempotency_key))
        if self.cancel_next_action:
            self.cancel_next_action = False
            raise asyncio.CancelledError
        dispatch_key = str(idempotency_key)
        operation = self.shared_dispatches.get(dispatch_key)
        if operation is None:
            service = next(
                item
                for item in self.store.services.values()
                if item["local_service_id"] == local_service_id
            )
            status = self.action_status_by_local_id.get(
                local_service_id, OperationStatus.SUCCEEDED.value
            )
            now = self.clock.now()
            operation = {
                "operation_id": str(uuid4()),
                "agent_id": service["agent_id"],
                "local_service_id": local_service_id,
                "windows_service_name": service["windows_service_name"],
                "action": action,
                "status": status,
                "idempotency_key": dispatch_key,
                "request_fingerprint": canonical_request_fingerprint(
                    "POST",
                    f"/api/v1/services/{local_service_id}/actions/{action}",
                    {},
                ),
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "finished_at": (
                    now
                    if status
                    in {
                        OperationStatus.SUCCEEDED.value,
                        OperationStatus.FAILED.value,
                        OperationStatus.REJECTED.value,
                        OperationStatus.UNKNOWN.value,
                    }
                    else None
                ),
                "error_code": None,
                "message": None,
            }
            self.shared_dispatches[dispatch_key] = operation
            self.shared_side_effect_keys.append(dispatch_key)
        operation_id = str(operation["operation_id"])
        self.operations[operation_id] = operation
        if self.cancel_after_accept:
            self.cancel_after_accept = False
            raise asyncio.CancelledError
        return {
            **operation,
            **self.post_operation_overrides_by_local_id.get(local_service_id, {}),
        }

    async def get_operation(self, endpoint: str, operation_id: Any) -> dict[str, Any]:
        self.get_operation_calls.append(str(operation_id))
        operation = self.operations[str(operation_id)]
        return {
            **operation,
            **self.get_operation_overrides_by_id.get(str(operation_id), {}),
        }

    async def probe(self, endpoint: str, request: Any) -> ProbeResult:
        self.probe_calls.append(endpoint)
        script = self.probe_results_by_endpoint.setdefault(endpoint, [True])
        passed = script.pop(0) if len(script) > 1 else script[0]
        return ProbeResult(
            passed=passed,
            observed_at=self.clock.now(),
            latency_ms=1,
            code="READY" if passed else "NOT_READY",
            message="ready" if passed else "not ready",
        )


def add_group(
    store: FakeStore,
    members: list[dict[str, Any]],
    *,
    dependencies: list[dict[str, Any]] | None = None,
    probes: list[dict[str, Any]] | None = None,
    baseline_epoch: str | None = None,
    settle_seconds: int = 120,
) -> dict[str, Any]:
    group_id = str(uuid4())
    group = {
        "group_id": group_id,
        "name": "test",
        "state": GroupState.ARMED_IDLE.value,
        "baseline_epoch": baseline_epoch,
        "candidate_epoch": None,
        "candidate_stable_since": None,
        "settle_deadline_at": None,
        "last_scheduled_epoch": None,
        "missing_agent_ids": [],
        "node_settle_window_seconds": settle_seconds,
        "max_parallel_services": 4,
        "members": members,
        "dependencies": dependencies or [],
        "probes": probes or [],
    }
    store.groups[group_id] = group
    return group


def add_service(
    store: FakeStore,
    item: dict[str, Any],
    runtime_state: str,
    *,
    online: str = AgentOnlineStatus.ONLINE.value,
) -> None:
    store.services[str(item["managed_service_id"])] = {
        **item,
        "agent_endpoint": f"http://127.0.0.1/{item['local_service_id']}",
        "installation_state": InstallationState.INSTALLED.value,
        "runtime_state": runtime_state,
        "startup_state": StartupState.AUTOSTART_DISABLED.value,
        "agent_online_status": online,
    }


class RecoveryPureFunctionTests(unittest.TestCase):
    def test_canonical_epoch_matches_contract_and_sorts_agents(self) -> None:
        group_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        first_agent = UUID("11111111-1111-4111-8111-111111111111")
        second_agent = UUID("22222222-2222-4222-8222-222222222222")
        first_boot = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        second_boot = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
        payload = (
            "recovery-mvp-v1\n"
            f"group_id={group_id}\n"
            f"agent_id={first_agent};boot_id={first_boot}\n"
            f"agent_id={second_agent};boot_id={second_boot}\n"
        )
        expected = hashlib.sha256(payload.encode()).hexdigest()
        actual = canonical_epoch(
            str(group_id).upper(),
            [(second_agent, second_boot), (first_agent, first_boot)],
        )
        self.assertEqual(expected, actual)

    def test_kahn_layers_are_deterministic_bounded_and_reject_cycles(self) -> None:
        roots = [f"service-{index}" for index in range(6)]
        dependent = "dependent"
        batches = kahn_layers(
            [*roots, dependent],
            [(dependent, root) for root in roots],
            max_parallel=4,
        )
        self.assertEqual([4, 2, 1], [len(batch.services) for batch in batches])
        self.assertEqual([0, 0, 1], [batch.level for batch in batches])
        with self.assertRaises(DependencyCycleError):
            kahn_layers(["a", "b"], [("a", "b"), ("b", "a")])


class RecoveryEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = FakeStore(self.clock)
        self.client = FakeAgentClient(self.clock, self.store)

    def engine(self, client: FakeAgentClient | None = None) -> RecoveryEngine:
        return RecoveryEngine(
            self.store,
            client or self.client,
            now=self.clock.now,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            operation_poll_interval_seconds=1,
            operation_poll_deadline_seconds=3,
        )

    def isolated_start_scenario(
        self, local_service_id: str = "mysql"
    ) -> tuple[
        FakeClock,
        FakeStore,
        FakeAgentClient,
        RecoveryEngine,
        dict[str, Any],
        dict[str, Any],
    ]:
        clock = FakeClock()
        store = FakeStore(clock)
        client = FakeAgentClient(clock, store)
        item = member(uuid4(), uuid4(), local_service_id)
        group = add_group(store, [item], probes=[explicit_scm_probe(UUID(item["managed_service_id"]))])
        add_service(store, item, RuntimeState.INACTIVE.value)
        run = store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None
        engine = RecoveryEngine(
            store,
            client,
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            operation_poll_interval_seconds=1,
            operation_poll_deadline_seconds=3,
        )
        return clock, store, client, engine, item, run

    async def test_post_operation_binding_mismatches_are_quarantined_before_persistence(
        self,
    ) -> None:
        cases: list[tuple[str, Any]] = [
            ("operation_id", "not-a-uuid"),
            ("agent_id", str(uuid4())),
            ("local_service_id", "other-service"),
            ("windows_service_name", "svc-other-service"),
            ("action", "stop"),
            ("idempotency_key", str(uuid4())),
            ("request_fingerprint", "0" * 64),
        ]
        for field, wrong_value in cases:
            with self.subTest(field=field):
                _, _, client, engine, _, run = self.isolated_start_scenario()
                client.post_operation_overrides_by_local_id["mysql"] = {
                    field: wrong_value
                }

                result = await engine.execute_run(run["run_id"])
                step = result["steps"][0]

                self.assertEqual(RecoveryRunStatus.UNKNOWN.value, result["status"])
                self.assertEqual(
                    ErrorCode.AGENT_PROTOCOL_MISMATCH.value, result["failure_code"]
                )
                self.assertEqual(RecoveryStepStatus.UNKNOWN.value, step["status"])
                self.assertTrue(
                    step["message"].startswith(
                        f"{ErrorCode.AGENT_PROTOCOL_MISMATCH.value}:"
                    )
                )
                self.assertIsNone(step["operation_id"])
                self.assertEqual(1, len(client.action_calls))
                self.assertEqual([], client.get_operation_calls)
                self.assertEqual([], client.probe_calls)

                await engine.execute_run(run["run_id"])
                self.assertEqual(1, len(client.action_calls), "mismatch must not be resent")

    async def test_get_operation_binding_mismatches_are_quarantined_without_redispatch(
        self,
    ) -> None:
        cases: list[tuple[str, Any]] = [
            ("operation_id", str(uuid4())),
            ("agent_id", str(uuid4())),
            ("local_service_id", "other-service"),
            ("windows_service_name", "svc-other-service"),
            ("action", "stop"),
            ("idempotency_key", str(uuid4())),
            ("request_fingerprint", "f" * 64),
        ]
        for field, wrong_value in cases:
            with self.subTest(field=field):
                _, store, client, engine, _, run = self.isolated_start_scenario()
                step = run["steps"][0]
                dispatch_key = uuid4()
                accepted = await client.start_action(
                    "http://127.0.0.1/mysql", "mysql", "start", dispatch_key
                )
                operation_id = str(accepted["operation_id"])
                store.assign_step_dispatch(step["step_id"], dispatch_key)
                store.assign_step_operation(step["step_id"], operation_id)
                store.update_step(step["step_id"], RecoveryStepStatus.STARTING.value)
                client.action_calls.clear()
                client.get_operation_overrides_by_id[operation_id] = {
                    field: wrong_value
                }

                result = await engine.execute_run(run["run_id"])
                recovered_step = result["steps"][0]

                self.assertEqual(RecoveryRunStatus.UNKNOWN.value, result["status"])
                self.assertEqual(
                    ErrorCode.AGENT_PROTOCOL_MISMATCH.value, result["failure_code"]
                )
                self.assertEqual(
                    RecoveryStepStatus.UNKNOWN.value, recovered_step["status"]
                )
                self.assertEqual(operation_id, str(recovered_step["operation_id"]))
                self.assertEqual([], client.action_calls)
                self.assertEqual([operation_id], client.get_operation_calls)
                self.assertEqual([], client.probe_calls)

    async def test_real_agent_client_quarantines_malformed_post_operation_immediately(
        self,
    ) -> None:
        for defect in ("missing", "invalid_uuid"):
            with self.subTest(defect=defect):
                clock, store, _, _, item, run = self.isolated_start_scenario()
                service = store.services[str(item["managed_service_id"])]
                service["agent_endpoint"] = "http://127.0.0.1:8765"
                requests: list[httpx.Request] = []
                canary = "sensitive-agent-payload-canary"

                async def handler(request: httpx.Request) -> httpx.Response:
                    requests.append(request)
                    payload = wire_operation(
                        item,
                        uuid4(),
                        UUID(request.headers["Idempotency-Key"]),
                        clock.now(),
                    )
                    if defect == "missing":
                        payload.pop("operation_id")
                    else:
                        payload["operation_id"] = canary
                    return httpx.Response(202, request=request, json=payload)

                transport = httpx.MockTransport(handler)
                async with httpx.AsyncClient(
                    transport=transport, trust_env=False
                ) as raw_client:
                    client = HttpAgentClient(
                        "x" * 32, attempts=1, client=raw_client
                    )
                    engine = RecoveryEngine(
                        store,
                        client,
                        now=clock.now,
                        monotonic=clock.monotonic,
                        sleep=clock.sleep,
                        operation_poll_interval_seconds=1,
                        operation_poll_deadline_seconds=3,
                    )
                    result = await engine.execute_run(run["run_id"])
                    await engine.execute_run(run["run_id"])

                step = result["steps"][0]
                self.assertEqual(RecoveryRunStatus.UNKNOWN.value, result["status"])
                self.assertEqual(
                    ErrorCode.AGENT_PROTOCOL_MISMATCH.value, result["failure_code"]
                )
                self.assertEqual(RecoveryStepStatus.UNKNOWN.value, step["status"])
                self.assertIsNone(step["operation_id"])
                self.assertNotIn(canary, step["message"])
                self.assertEqual(1, len(requests), "malformed POST must not be retried")
                self.assertEqual("POST", requests[0].method)
                self.assertEqual([], result["steps"][0]["probe_attempts"])

    async def test_real_agent_client_quarantines_malformed_get_operation_immediately(
        self,
    ) -> None:
        for defect in ("missing", "invalid_uuid"):
            with self.subTest(defect=defect):
                clock, store, _, _, item, run = self.isolated_start_scenario()
                service = store.services[str(item["managed_service_id"])]
                service["agent_endpoint"] = "http://127.0.0.1:8765"
                step = run["steps"][0]
                dispatch_key = uuid4()
                operation_id = uuid4()
                store.assign_step_dispatch(step["step_id"], dispatch_key)
                store.assign_step_operation(step["step_id"], operation_id)
                store.update_step(step["step_id"], RecoveryStepStatus.STARTING.value)
                requests: list[httpx.Request] = []
                canary = "sensitive-agent-payload-canary"

                async def handler(request: httpx.Request) -> httpx.Response:
                    requests.append(request)
                    payload = wire_operation(
                        item, operation_id, dispatch_key, clock.now()
                    )
                    if defect == "missing":
                        payload.pop("agent_id")
                    else:
                        payload["agent_id"] = canary
                    return httpx.Response(200, request=request, json=payload)

                transport = httpx.MockTransport(handler)
                async with httpx.AsyncClient(
                    transport=transport, trust_env=False
                ) as raw_client:
                    client = HttpAgentClient(
                        "x" * 32, attempts=1, client=raw_client
                    )
                    engine = RecoveryEngine(
                        store,
                        client,
                        now=clock.now,
                        monotonic=clock.monotonic,
                        sleep=clock.sleep,
                        operation_poll_interval_seconds=1,
                        operation_poll_deadline_seconds=3,
                    )
                    result = await engine.execute_run(run["run_id"])

                recovered_step = result["steps"][0]
                self.assertEqual(RecoveryRunStatus.UNKNOWN.value, result["status"])
                self.assertEqual(
                    ErrorCode.AGENT_PROTOCOL_MISMATCH.value, result["failure_code"]
                )
                self.assertEqual(
                    RecoveryStepStatus.UNKNOWN.value, recovered_step["status"]
                )
                self.assertEqual(str(operation_id), str(recovered_step["operation_id"]))
                self.assertNotIn(canary, recovered_step["message"])
                self.assertEqual(1, len(requests), "malformed GET must not be retried")
                self.assertEqual("GET", requests[0].method)
                self.assertEqual([], recovered_step["probe_attempts"])

    async def test_windows_service_name_binding_is_case_insensitive_for_post_and_get(
        self,
    ) -> None:
        _, _, post_client, post_engine, _, post_run = self.isolated_start_scenario()
        post_client.post_operation_overrides_by_local_id["mysql"] = {
            "windows_service_name": "SVC-MYSQL"
        }
        post_result = await post_engine.execute_run(post_run["run_id"])
        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, post_result["status"])

        _, get_store, get_client, get_engine, _, get_run = self.isolated_start_scenario()
        get_step = get_run["steps"][0]
        dispatch_key = uuid4()
        accepted = await get_client.start_action(
            "http://127.0.0.1/mysql", "mysql", "start", dispatch_key
        )
        operation_id = str(accepted["operation_id"])
        get_store.assign_step_dispatch(get_step["step_id"], dispatch_key)
        get_store.assign_step_operation(get_step["step_id"], operation_id)
        get_store.update_step(get_step["step_id"], RecoveryStepStatus.STARTING.value)
        get_client.action_calls.clear()
        get_client.get_operation_overrides_by_id[operation_id] = {
            "windows_service_name": "SVC-MYSQL"
        }

        get_result = await get_engine.execute_run(get_run["run_id"])
        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, get_result["status"])
        self.assertEqual([], get_client.action_calls)
        self.assertEqual([operation_id], get_client.get_operation_calls)

    async def test_protocol_mismatch_blocks_downstream_but_independent_branch_continues(
        self,
    ) -> None:
        agent = uuid4()
        upstream_id, child_id, independent_id = uuid4(), uuid4(), uuid4()
        upstream = member(upstream_id, agent, "upstream")
        child = member(child_id, agent, "child")
        independent = member(independent_id, agent, "independent")
        group = add_group(
            self.store,
            [upstream, child, independent],
            dependencies=[dependency(child_id, upstream_id)],
            probes=[
                explicit_scm_probe(upstream_id),
                explicit_scm_probe(child_id),
                explicit_scm_probe(independent_id),
            ],
        )
        add_service(self.store, upstream, RuntimeState.INACTIVE.value)
        add_service(self.store, child, RuntimeState.INACTIVE.value)
        add_service(self.store, independent, RuntimeState.ACTIVE.value)
        self.client.post_operation_overrides_by_local_id["upstream"] = {
            "agent_id": str(uuid4())
        }
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None

        result = await self.engine().execute_run(run["run_id"])
        steps = {item["local_service_id"]: item for item in result["steps"]}

        self.assertEqual(RecoveryRunStatus.UNKNOWN.value, result["status"])
        self.assertEqual(
            ErrorCode.AGENT_PROTOCOL_MISMATCH.value, result["failure_code"]
        )
        self.assertEqual(RecoveryStepStatus.UNKNOWN.value, steps["upstream"]["status"])
        self.assertEqual(RecoveryStepStatus.BLOCKED.value, steps["child"]["status"])
        self.assertEqual(RecoveryStepStatus.READY.value, steps["independent"]["status"])
        self.assertEqual(["upstream"], [call[1] for call in self.client.action_calls])
        self.assertNotIn("http://127.0.0.1/upstream", self.client.probe_calls)
        self.assertNotIn("http://127.0.0.1/child", self.client.probe_calls)
        self.assertIn("http://127.0.0.1/independent", self.client.probe_calls)

    async def test_auto_waits_for_every_node_and_resets_settle_on_boot_change(self) -> None:
        agent_a, agent_b = uuid4(), uuid4()
        service_a, service_b = uuid4(), uuid4()
        members = [member(service_a, agent_a, "mysql"), member(service_b, agent_b, "redis")]
        old_a, old_b = uuid4(), uuid4()
        group = add_group(self.store, members, settle_seconds=120)
        group["baseline_epoch"] = canonical_epoch(
            group["group_id"], [(agent_a, old_a), (agent_b, old_b)]
        )
        new_a, new_b = uuid4(), uuid4()
        self.store.agents[str(agent_a)] = {
            "agent_id": str(agent_a),
            "boot_id": str(new_a),
            "online_status": AgentOnlineStatus.ONLINE.value,
        }
        self.store.agents[str(agent_b)] = {
            "agent_id": str(agent_b),
            "boot_id": str(new_b),
            "online_status": AgentOnlineStatus.OFFLINE.value,
        }
        for item in members:
            add_service(self.store, item, RuntimeState.ACTIVE.value)

        engine = self.engine()
        self.assertEqual([], await engine.scan_auto_groups())
        self.assertEqual(GroupState.WAITING_FOR_NODES.value, group["state"])
        self.assertEqual([str(agent_b)], group["missing_agent_ids"])

        self.store.agents[str(agent_b)]["online_status"] = AgentOnlineStatus.ONLINE.value
        self.assertEqual([], await engine.scan_auto_groups())
        first_candidate = group["candidate_epoch"]
        first_stable = group["candidate_stable_since"]
        self.assertEqual(GroupState.SETTLING.value, group["state"])

        self.clock.advance(119)
        self.assertEqual([], await engine.scan_auto_groups())
        self.store.agents[str(agent_b)]["boot_id"] = str(uuid4())
        self.assertEqual([], await engine.scan_auto_groups())
        self.assertNotEqual(first_candidate, group["candidate_epoch"])
        self.assertGreater(group["candidate_stable_since"], first_stable)

        self.clock.advance(120)
        created = await engine.scan_auto_groups()
        self.assertEqual(1, len(created))
        await engine.wait_for_idle()
        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, created[0]["status"])
        self.assertEqual([], await engine.scan_auto_groups())
        self.assertEqual(1, len(self.store.runs))

    async def test_preflight_failure_blocks_auto_without_actions(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "mysql")
        boot_old, boot_new = uuid4(), uuid4()
        group = add_group(self.store, [item], settle_seconds=1)
        group["baseline_epoch"] = canonical_epoch(group["group_id"], [(agent, boot_old)])
        self.store.agents[str(agent)] = {
            "agent_id": str(agent),
            "boot_id": str(boot_new),
            "online_status": AgentOnlineStatus.ONLINE.value,
        }
        add_service(self.store, item, RuntimeState.INACTIVE.value)
        self.store.preflight_errors = [{"code": "GROUP_NOT_READY", "message": "automatic"}]
        engine = self.engine()
        await engine.scan_auto_groups()
        self.clock.advance(1)
        self.assertEqual([], await engine.scan_auto_groups())
        self.assertEqual(GroupState.BLOCKED_PRECONDITION.value, group["state"])
        self.assertEqual([], self.client.action_calls)

    async def test_auto_settle_uses_monotonic_across_wall_clock_jumps(self) -> None:
        agent_id, service_id = uuid4(), uuid4()
        item = member(service_id, agent_id, "mysql")
        old_boot, new_boot = uuid4(), uuid4()
        group = add_group(self.store, [item], settle_seconds=120)
        group["baseline_epoch"] = canonical_epoch(
            group["group_id"], [(agent_id, old_boot)]
        )
        self.store.agents[str(agent_id)] = {
            "agent_id": str(agent_id),
            "boot_id": str(new_boot),
            "online_status": AgentOnlineStatus.ONLINE.value,
        }
        add_service(self.store, item, RuntimeState.ACTIVE.value)
        engine = self.engine()

        self.assertEqual([], await engine.scan_auto_groups())
        # A forward wall jump must not authorize an early AUTO Run.
        self.clock.wall += timedelta(days=1)
        self.assertEqual([], await engine.scan_auto_groups())
        self.clock.ticks += 119
        self.assertEqual([], await engine.scan_auto_groups())

        # A backward wall jump must not extend a completed monotonic window.
        self.clock.wall -= timedelta(days=2)
        self.clock.ticks += 1
        created = await engine.scan_auto_groups()
        self.assertEqual(1, len(created))
        await engine.wait_for_idle()
        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, created[0]["status"])

    async def test_cp_restart_conservatively_restarts_candidate_settle_window(self) -> None:
        agent_id, service_id = uuid4(), uuid4()
        item = member(service_id, agent_id, "mysql")
        old_boot, new_boot = uuid4(), uuid4()
        group = add_group(self.store, [item], settle_seconds=120)
        group["baseline_epoch"] = canonical_epoch(
            group["group_id"], [(agent_id, old_boot)]
        )
        self.store.agents[str(agent_id)] = {
            "agent_id": str(agent_id),
            "boot_id": str(new_boot),
            "online_status": AgentOnlineStatus.ONLINE.value,
        }
        add_service(self.store, item, RuntimeState.ACTIVE.value)

        first_process = self.engine()
        self.assertEqual([], await first_process.scan_auto_groups())
        self.clock.advance(120)

        # A new engine has no process-local proof that CP observed the nodes
        # continuously while it was unavailable, even if the durable deadline
        # has elapsed.
        restarted_process = self.engine()
        self.assertEqual([], await restarted_process.scan_auto_groups())
        self.clock.advance(119)
        self.assertEqual([], await restarted_process.scan_auto_groups())
        self.clock.advance(1)
        created = await restarted_process.scan_auto_groups()
        self.assertEqual(1, len(created))
        await restarted_process.wait_for_idle()

    async def test_scheduler_recovers_after_transient_iteration_failure(self) -> None:
        engine = self.engine()
        stop_event = asyncio.Event()
        calls = 0

        async def flaky_scan() -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient database failure")
            stop_event.set()
            return []

        engine.scan_auto_groups = flaky_scan  # type: ignore[method-assign]
        with self.assertLogs("orchestrator.control_plane.recovery", level="ERROR"):
            await engine.scheduler_loop(stop_event, interval_seconds=1)
        self.assertEqual(2, calls)
        self.assertEqual(1.0, self.clock.ticks)

    async def test_failed_run_task_is_observed_and_can_be_relaunched(self) -> None:
        engine = self.engine()

        async def fail_run(run_id: Any) -> None:
            raise RuntimeError(f"failed {run_id}")

        engine.execute_run = fail_run  # type: ignore[method-assign]
        with self.assertLogs("orchestrator.control_plane.recovery", level="ERROR"):
            first = engine.launch_run("run-a")
            await asyncio.gather(first, return_exceptions=True)
            await asyncio.sleep(0)
        second = engine.launch_run("run-a")
        self.assertIsNot(first, second)
        await asyncio.gather(second, return_exceptions=True)

    async def test_scheduler_rejects_non_positive_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            await self.engine().scheduler_loop(asyncio.Event(), interval_seconds=0)

    async def test_failure_blocks_only_downstream_and_active_branch_still_probes(self) -> None:
        agent = uuid4()
        mysql_id, redis_id, nacos_id = uuid4(), uuid4(), uuid4()
        mysql = member(mysql_id, agent, "mysql")
        redis = member(redis_id, agent, "redis")
        nacos = member(nacos_id, agent, "nacos")
        group = add_group(
            self.store,
            [mysql, redis, nacos],
            dependencies=[dependency(nacos_id, mysql_id)],
            probes=[
                explicit_scm_probe(mysql_id, deadline=2),
                explicit_scm_probe(redis_id),
                explicit_scm_probe(nacos_id),
            ],
        )
        add_service(self.store, mysql, RuntimeState.FAILED.value)
        add_service(self.store, redis, RuntimeState.ACTIVE.value)
        add_service(self.store, nacos, RuntimeState.INACTIVE.value)
        self.client.probe_results_by_endpoint["http://127.0.0.1/mysql"] = [False]
        self.client.probe_results_by_endpoint["http://127.0.0.1/redis"] = [True]
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None

        result = await self.engine().execute_run(run["run_id"])
        statuses = {
            step["local_service_id"]: step["status"] for step in result["steps"]
        }
        self.assertEqual(RecoveryRunStatus.FAILED.value, result["status"])
        self.assertEqual(RecoveryStepStatus.FAILED.value, statuses["mysql"])
        self.assertEqual(RecoveryStepStatus.READY.value, statuses["redis"])
        self.assertEqual(RecoveryStepStatus.BLOCKED.value, statuses["nacos"])
        self.assertEqual(["mysql"], [call[1] for call in self.client.action_calls])
        self.assertNotIn("http://127.0.0.1/nacos", self.client.probe_calls)
        self.assertEqual({}, self.store.locked_services)

    async def test_unknown_has_priority_while_independent_failure_finishes(self) -> None:
        agent = uuid4()
        uncertain_id, failed_id, child_id = uuid4(), uuid4(), uuid4()
        uncertain = member(uncertain_id, agent, "uncertain")
        failed = member(failed_id, agent, "failed-probe")
        child = member(child_id, agent, "child")
        group = add_group(
            self.store,
            [uncertain, failed, child],
            dependencies=[dependency(child_id, uncertain_id)],
            probes=[
                explicit_scm_probe(uncertain_id),
                explicit_scm_probe(failed_id, deadline=1),
                explicit_scm_probe(child_id),
            ],
        )
        add_service(self.store, uncertain, RuntimeState.INACTIVE.value)
        add_service(self.store, failed, RuntimeState.ACTIVE.value)
        add_service(self.store, child, RuntimeState.INACTIVE.value)
        self.client.action_status_by_local_id["uncertain"] = OperationStatus.UNKNOWN.value
        self.client.probe_results_by_endpoint["http://127.0.0.1/failed-probe"] = [False]
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None

        result = await self.engine().execute_run(run["run_id"])
        statuses = {
            step["local_service_id"]: step["status"] for step in result["steps"]
        }
        self.assertEqual(RecoveryRunStatus.UNKNOWN.value, result["status"])
        self.assertEqual(RecoveryStepStatus.UNKNOWN.value, statuses["uncertain"])
        self.assertEqual(RecoveryStepStatus.FAILED.value, statuses["failed-probe"])
        self.assertEqual(RecoveryStepStatus.BLOCKED.value, statuses["child"])

    async def test_cp_restart_reuses_durable_dispatch_key_and_original_run(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "mysql")
        group = add_group(
            self.store, [item], probes=[explicit_scm_probe(service)]
        )
        add_service(self.store, item, RuntimeState.INACTIVE.value)
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None
        first_client = FakeAgentClient(self.clock, self.store)
        first_client.cancel_next_action = True
        first_engine = self.engine(first_client)

        with self.assertRaises(asyncio.CancelledError):
            await first_engine.execute_run(run["run_id"])
        step = run["steps"][0]
        persisted_key = step["dispatch_idempotency_key"]
        self.assertIsNotNone(persisted_key)
        self.assertIsNone(step["operation_id"])
        self.assertEqual(RecoveryStepStatus.STARTING.value, step["status"])

        second_client = FakeAgentClient(self.clock, self.store)
        second_engine = self.engine(second_client)
        tasks = await second_engine.resume_unfinished_runs()
        self.assertEqual(1, len(tasks))
        await second_engine.wait_for_idle()
        result = self.store.get_run(run["run_id"])
        assert result is not None
        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, result["status"])
        self.assertEqual(run["run_id"], result["run_id"])
        self.assertEqual(persisted_key, second_client.action_calls[0][3])
        self.assertEqual(1, len(self.store.runs))

    async def test_cp_restart_recovers_agent_operation_after_response_loss(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "mysql")
        group = add_group(
            self.store, [item], probes=[explicit_scm_probe(service)]
        )
        add_service(self.store, item, RuntimeState.INACTIVE.value)
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None
        dispatches: dict[str, dict[str, Any]] = {}
        side_effect_keys: list[str] = []
        first_client = FakeAgentClient(
            self.clock,
            self.store,
            shared_dispatches=dispatches,
            shared_side_effect_keys=side_effect_keys,
        )
        first_client.cancel_after_accept = True

        with self.assertRaises(asyncio.CancelledError):
            await self.engine(first_client).execute_run(run["run_id"])

        step = run["steps"][0]
        persisted_key = str(step["dispatch_idempotency_key"])
        self.assertIsNone(step["operation_id"])
        self.assertIn(persisted_key, dispatches)
        accepted_operation_id = dispatches[persisted_key]["operation_id"]
        self.assertEqual([persisted_key], side_effect_keys)

        second_client = FakeAgentClient(
            self.clock,
            self.store,
            shared_dispatches=dispatches,
            shared_side_effect_keys=side_effect_keys,
        )
        tasks = await self.engine(second_client).resume_unfinished_runs()
        self.assertEqual(1, len(tasks))
        await asyncio.gather(*tasks)

        result = self.store.get_run(run["run_id"])
        assert result is not None
        recovered_step = result["steps"][0]
        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, result["status"])
        self.assertEqual(accepted_operation_id, recovered_step["operation_id"])
        self.assertEqual(persisted_key, str(second_client.action_calls[0][3]))
        self.assertEqual([persisted_key], side_effect_keys)

    async def test_cp_restart_with_durable_operation_id_only_reconciles(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "mysql")
        group = add_group(
            self.store, [item], probes=[explicit_scm_probe(service)]
        )
        add_service(self.store, item, RuntimeState.INACTIVE.value)
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None
        client = FakeAgentClient(self.clock, self.store)
        self.store.cancel_after_assign_operation = True

        with self.assertRaises(asyncio.CancelledError):
            await self.engine(client).execute_run(run["run_id"])

        step = run["steps"][0]
        durable_operation_id = str(step["operation_id"])
        self.assertIn(durable_operation_id, client.operations)
        client.action_calls.clear()
        client.get_operation_calls.clear()

        tasks = await self.engine(client).resume_unfinished_runs()
        self.assertEqual(1, len(tasks))
        await asyncio.gather(*tasks)

        result = self.store.get_run(run["run_id"])
        assert result is not None
        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, result["status"])
        self.assertEqual([], client.action_calls)
        self.assertEqual([durable_operation_id], client.get_operation_calls)

    async def test_probe_attempt_history_is_persisted_until_ready(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "nacos")
        group = add_group(
            self.store, [item], probes=[explicit_scm_probe(service, deadline=5)]
        )
        add_service(self.store, item, RuntimeState.ACTIVE.value)
        self.client.probe_results_by_endpoint["http://127.0.0.1/nacos"] = [False, False, True]
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None

        result = await self.engine().execute_run(run["run_id"])
        step = result["steps"][0]
        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, result["status"])
        self.assertEqual(RecoveryStepStatus.READY.value, step["status"])
        self.assertEqual([1, 2, 3], [item["attempt"] for item in step["probe_attempts"]])
        self.assertEqual(0, len(self.client.action_calls), "ACTIVE service must not be started again")

    async def test_resume_does_not_extend_an_expired_probe_deadline(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "nacos")
        group = add_group(
            self.store, [item], probes=[explicit_scm_probe(service, deadline=5)]
        )
        add_service(self.store, item, RuntimeState.ACTIVE.value)
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None
        step = run["steps"][0]
        step["status"] = RecoveryStepStatus.PROBING.value
        step["started_at"] = self.clock.now()
        step["probe_attempts"] = [
            {
                "attempt": 1,
                "started_at": self.clock.now(),
                "finished_at": self.clock.now(),
                "result": {
                    "passed": False,
                    "observed_at": self.clock.now(),
                    "latency_ms": 1,
                    "code": "NOT_READY",
                    "message": "not ready",
                },
            }
        ]
        self.clock.advance(6)

        result = await self.engine().execute_run(run["run_id"])

        self.assertEqual(RecoveryRunStatus.FAILED.value, result["status"])
        self.assertEqual(RecoveryStepStatus.FAILED.value, result["steps"][0]["status"])
        self.assertEqual([], self.client.probe_calls)

    async def test_readiness_deadline_starts_after_service_start_completes(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "nacos")
        group = add_group(
            self.store, [item], probes=[explicit_scm_probe(service, deadline=5)]
        )
        add_service(self.store, item, RuntimeState.INACTIVE.value)

        class SlowStartClient(FakeAgentClient):
            async def start_action(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                self.clock.advance(30)
                return await super().start_action(*args, **kwargs)

        client = SlowStartClient(self.clock, self.store)
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None

        result = await self.engine(client).execute_run(run["run_id"])

        self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, result["status"])
        self.assertEqual(RecoveryStepStatus.READY.value, result["steps"][0]["status"])
        self.assertEqual(1, len(client.probe_calls))

    async def test_probe_success_after_total_deadline_is_not_accepted(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "nacos")
        group = add_group(
            self.store, [item], probes=[explicit_scm_probe(service, deadline=5)]
        )
        add_service(self.store, item, RuntimeState.ACTIVE.value)

        class LateProbeClient(FakeAgentClient):
            async def probe(self, endpoint: str, request: Any) -> ProbeResult:
                self.clock.advance(6)
                return await super().probe(endpoint, request)

        client = LateProbeClient(self.clock, self.store)
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None

        result = await self.engine(client).execute_run(run["run_id"])

        self.assertEqual(RecoveryRunStatus.FAILED.value, result["status"])
        self.assertEqual(RecoveryStepStatus.FAILED.value, result["steps"][0]["status"])
        self.assertIn("after its deadline", result["steps"][0]["message"])

    async def test_restart_does_not_accept_a_late_persisted_probe_success(self) -> None:
        agent, service = uuid4(), uuid4()
        item = member(service, agent, "nacos")
        group = add_group(
            self.store, [item], probes=[explicit_scm_probe(service, deadline=5)]
        )
        add_service(self.store, item, RuntimeState.ACTIVE.value)
        run = self.store.create_run_from_group(group["group_id"], "MANUAL")
        assert run is not None
        step = run["steps"][0]

        # Model the durable state after record_probe_attempt committed but before
        # the original process could persist the terminal FAILED Step update.
        attempt_started = self.clock.now()
        step["status"] = RecoveryStepStatus.PROBING.value
        step["started_at"] = attempt_started
        step["updated_at"] = attempt_started
        self.clock.advance(6)
        step["probe_attempts"] = [
            {
                "attempt": 1,
                "started_at": attempt_started,
                "finished_at": self.clock.now(),
                "result": {
                    "passed": True,
                    "observed_at": self.clock.now(),
                    "latency_ms": 6_000,
                    "code": "READY",
                    "message": "late success",
                },
            }
        ]

        result = await self.engine().execute_run(run["run_id"])

        self.assertEqual(RecoveryRunStatus.FAILED.value, result["status"])
        self.assertEqual(RecoveryStepStatus.FAILED.value, result["steps"][0]["status"])
        self.assertIn("after its deadline", result["steps"][0]["message"])
        self.assertEqual([], self.client.probe_calls)


if __name__ == "__main__":
    unittest.main()
