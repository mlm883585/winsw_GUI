from __future__ import annotations

import asyncio
import threading
import time
from uuid import UUID, uuid4

import pytest

from orchestrator.agent.config import AllowlistedService
from orchestrator.agent.operations import OperationManager
from orchestrator.agent.scm import (
    SERVICE_DEMAND_START,
    SERVICE_PAUSED,
    SERVICE_RUNNING,
    SERVICE_START_PENDING,
    SERVICE_STOPPED,
    SERVICE_STOP_PENDING,
    SCMAccessDenied,
    SCMError,
    SCMServiceStatus,
    SCMServiceNotFound,
)
from orchestrator.agent.store import AgentStore, _migration_v1
from orchestrator.common.enums import OperationStatus, ServiceAction
from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.models import Operation


class FakeSCM:
    def __init__(self, state: int = SERVICE_STOPPED) -> None:
        self.state = state
        self.calls: list[str] = []

    def query(self, _name: str) -> SCMServiceStatus:
        return SCMServiceStatus(self.state, start_type=SERVICE_DEMAND_START, display_name="MySQL 8")

    def start(self, _name: str) -> None:
        self.calls.append("start")
        self.state = SERVICE_RUNNING

    def stop(self, _name: str) -> None:
        self.calls.append("stop")
        self.state = SERVICE_STOPPED


class ActionMatrixSCM(FakeSCM):
    """Expose every frozen observation category without timing-based sleeps."""

    def __init__(self, observed_case: str, action: ServiceAction) -> None:
        super().__init__()
        self.observed_case = observed_case
        self.action = action
        self.query_count = 0

    def query(self, name: str) -> SCMServiceStatus:
        self.query_count += 1
        if self.observed_case == "NOT_INSTALLED":
            raise SCMServiceNotFound(name)
        if self.observed_case == "INSTALLATION_UNKNOWN":
            raise SCMAccessDenied("query denied")
        if self.observed_case == "ACTIVE":
            state, exit_code = SERVICE_RUNNING, 0
        elif self.observed_case == "INACTIVE":
            state, exit_code = SERVICE_STOPPED, 0
        elif self.observed_case == "FAILED":
            state, exit_code = SERVICE_STOPPED, 1
        elif self.observed_case == "STARTING":
            state = (
                SERVICE_RUNNING
                if self.action is ServiceAction.START and self.query_count >= 3
                else SERVICE_START_PENDING
            )
            exit_code = 0
        elif self.observed_case == "STOPPING":
            state = (
                SERVICE_STOPPED
                if self.action is ServiceAction.STOP and self.query_count >= 3
                else SERVICE_STOP_PENDING
            )
            exit_code = 0
        else:
            assert self.observed_case == "RUNTIME_UNKNOWN"
            state, exit_code = SERVICE_PAUSED, 0
        if self.state == SERVICE_RUNNING:
            state, exit_code = SERVICE_RUNNING, 0
        return SCMServiceStatus(
            state,
            win32_exit_code=exit_code,
            start_type=SERVICE_DEMAND_START,
        )

    def stop(self, name: str) -> None:
        super().stop(name)
        # Once a real stop side effect has returned, observations must converge
        # to INACTIVE rather than the test's original ACTIVE category.
        self.observed_case = "INACTIVE"


class DeterministicFailureSCM(FakeSCM):
    """Fail one exact SCM call after admission has durably created an Operation."""

    def __init__(self, state: int, failure: str) -> None:
        super().__init__(state)
        self.failure = failure
        self.query_count = 0

    def query(self, name: str) -> SCMServiceStatus:
        self.query_count += 1
        if self.failure == "query" and self.query_count == 2:
            raise SCMError("deterministic query failure")
        return super().query(name)

    def start(self, name: str) -> None:
        self.calls.append("start")
        if self.failure == "start":
            raise SCMError("deterministic start failure")
        self.state = SERVICE_RUNNING

    def stop(self, name: str) -> None:
        self.calls.append("stop")
        if self.failure == "stop":
            raise SCMError("deterministic stop failure")
        self.state = SERVICE_STOPPED


class CountingSCM(FakeSCM):
    def __init__(self, state: int = SERVICE_STOPPED) -> None:
        super().__init__(state)
        self.query_count = 0
        self.fail_queries = False

    def query(self, name: str) -> SCMServiceStatus:
        self.query_count += 1
        if self.fail_queries:
            raise RuntimeError("SCM must not be consulted for an idempotent replay")
        return super().query(name)


class NotifyingStore(AgentStore):
    """Expose durable completion to concurrency tests without polling sleeps."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.operation_finished = threading.Event()

    def finish_operation(
        self,
        operation_id: UUID,
        status: OperationStatus,
        *,
        error_code: ErrorCode | None = None,
        message: str | None = None,
    ) -> Operation:
        operation = super().finish_operation(
            operation_id,
            status,
            error_code=error_code,
            message=message,
        )
        self.operation_finished.set()
        return operation


class GatedObservationSCM(FakeSCM):
    """Hold the first observation open and record all observation intervals."""

    def __init__(self, state: int = SERVICE_STOPPED) -> None:
        super().__init__(state)
        self.first_query_started = threading.Event()
        self.release_first_query = threading.Event()
        self._guard = threading.Lock()
        self.query_count = 0
        self.active_queries = 0
        self.max_active_queries = 0
        self.intervals: list[tuple[str, str, int]] = []

    def query(self, name: str) -> SCMServiceStatus:
        with self._guard:
            self.query_count += 1
            query_number = self.query_count
            self.active_queries += 1
            self.max_active_queries = max(
                self.max_active_queries,
                self.active_queries,
            )
            self.intervals.append(("enter", name, query_number))
            if query_number == 1:
                self.first_query_started.set()
        try:
            if query_number == 1 and not self.release_first_query.wait(timeout=3):
                raise TimeoutError("test did not release the first SCM observation")
            return super().query(name)
        finally:
            with self._guard:
                self.intervals.append(("exit", name, query_number))
                self.active_queries -= 1


class SignalingAsyncLock:
    """Signal when a concurrent caller has reached the same service lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.attempts = 0
        self.second_attempted = asyncio.Event()

    async def __aenter__(self) -> "SignalingAsyncLock":
        self.attempts += 1
        if self.attempts == 2:
            self.second_attempted.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, *_args) -> None:
        self._lock.release()


class ParallelObservationSCM(FakeSCM):
    """Require two different service observations to overlap."""

    def __init__(self) -> None:
        super().__init__()
        self._barrier = threading.Barrier(2)
        self._guard = threading.Lock()
        self.active_queries = 0
        self.max_active_queries = 0

    def query(self, name: str) -> SCMServiceStatus:
        with self._guard:
            self.active_queries += 1
            self.max_active_queries = max(
                self.max_active_queries,
                self.active_queries,
            )
        try:
            self._barrier.wait(timeout=3)
            return super().query(name)
        finally:
            with self._guard:
                self.active_queries -= 1


def make_manager(tmp_path, backend: FakeSCM) -> tuple[AgentStore, OperationManager, AllowlistedService]:
    store = AgentStore(tmp_path / "agent.sqlite3")
    store.initialize()
    service = AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")
    manager = OperationManager(
        store=store,
        backend=backend,
        services={service.local_service_id: service},
        action_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    return store, manager, service


async def wait_for_terminal(store: AgentStore, operation_id: UUID) -> Operation:
    for _ in range(300):
        operation = store.get_operation(operation_id)
        assert operation is not None
        if operation.status.terminal:
            return operation
        await asyncio.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not reach a terminal state")


_ACTION_MATRIX_EXPECTATIONS = {
    "ACTIVE": {
        ServiceAction.START: (OperationStatus.SUCCEEDED, None, []),
        ServiceAction.STOP: (OperationStatus.SUCCEEDED, None, ["stop"]),
        ServiceAction.RESTART: (OperationStatus.SUCCEEDED, None, ["stop", "start"]),
    },
    "INACTIVE": {
        ServiceAction.START: (OperationStatus.SUCCEEDED, None, ["start"]),
        ServiceAction.STOP: (OperationStatus.SUCCEEDED, None, []),
        ServiceAction.RESTART: (
            OperationStatus.REJECTED,
            ErrorCode.SERVICE_ACTION_CONFLICT,
            [],
        ),
    },
    "FAILED": {
        ServiceAction.START: (OperationStatus.SUCCEEDED, None, ["start"]),
        ServiceAction.STOP: (OperationStatus.SUCCEEDED, None, []),
        ServiceAction.RESTART: (
            OperationStatus.REJECTED,
            ErrorCode.SERVICE_ACTION_CONFLICT,
            [],
        ),
    },
    "STARTING": {
        ServiceAction.START: (OperationStatus.SUCCEEDED, None, []),
        ServiceAction.STOP: (
            OperationStatus.REJECTED,
            ErrorCode.SERVICE_ACTION_CONFLICT,
            [],
        ),
        ServiceAction.RESTART: (
            OperationStatus.REJECTED,
            ErrorCode.SERVICE_ACTION_CONFLICT,
            [],
        ),
    },
    "STOPPING": {
        ServiceAction.START: (
            OperationStatus.REJECTED,
            ErrorCode.SERVICE_ACTION_CONFLICT,
            [],
        ),
        ServiceAction.STOP: (OperationStatus.SUCCEEDED, None, []),
        ServiceAction.RESTART: (
            OperationStatus.REJECTED,
            ErrorCode.SERVICE_ACTION_CONFLICT,
            [],
        ),
    },
    "RUNTIME_UNKNOWN": {
        action: (OperationStatus.REJECTED, ErrorCode.SERVICE_STATE_UNKNOWN, [])
        for action in ServiceAction
    },
    "NOT_INSTALLED": {
        action: (OperationStatus.REJECTED, ErrorCode.SERVICE_NOT_INSTALLED, [])
        for action in ServiceAction
    },
    "INSTALLATION_UNKNOWN": {
        action: (OperationStatus.REJECTED, ErrorCode.SERVICE_STATE_UNKNOWN, [])
        for action in ServiceAction
    },
}


@pytest.mark.parametrize(
    "observed_case",
    tuple(_ACTION_MATRIX_EXPECTATIONS),
    ids=str.lower,
)
@pytest.mark.parametrize("action", tuple(ServiceAction), ids=lambda action: action.value)
def test_frozen_action_matrix_is_exhaustive(
    tmp_path,
    observed_case: str,
    action: ServiceAction,
) -> None:
    async def scenario() -> None:
        backend = ActionMatrixSCM(observed_case, action)
        store, manager, service = make_manager(tmp_path, backend)
        await manager.start()
        try:
            accepted = await manager.create_operation(
                agent_id=uuid4(),
                service=service,
                action=action,
                idempotency_key=uuid4(),
                request_fingerprint="a" * 64,
            )
            finished = await wait_for_terminal(store, accepted.operation_id)
            expected_status, expected_error, expected_calls = _ACTION_MATRIX_EXPECTATIONS[
                observed_case
            ][action]
            assert finished.status is expected_status
            assert finished.error_code is expected_error
            assert backend.calls == expected_calls
            with store.database.read() as connection:
                journal_state = connection.execute(
                    "SELECT journal_state FROM operations WHERE operation_id=?",
                    (str(finished.operation_id),),
                ).fetchone()[0]
            assert journal_state == "COMPLETED"
        finally:
            await manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("action", "initial_state", "failure", "expected_calls"),
    [
        pytest.param(ServiceAction.START, SERVICE_STOPPED, "query", [], id="query"),
        pytest.param(ServiceAction.START, SERVICE_STOPPED, "start", ["start"], id="start"),
        pytest.param(ServiceAction.STOP, SERVICE_RUNNING, "stop", ["stop"], id="stop"),
        pytest.param(
            ServiceAction.RESTART,
            SERVICE_RUNNING,
            "stop",
            ["stop"],
            id="restart-stop",
        ),
        pytest.param(
            ServiceAction.RESTART,
            SERVICE_RUNNING,
            "start",
            ["stop", "start"],
            id="restart-start",
        ),
    ],
)
def test_deterministic_scm_failures_have_stable_failed_result(
    tmp_path,
    action: ServiceAction,
    initial_state: int,
    failure: str,
    expected_calls: list[str],
) -> None:
    async def scenario() -> None:
        backend = DeterministicFailureSCM(initial_state, failure)
        store, manager, service = make_manager(tmp_path, backend)
        await manager.start()
        try:
            accepted = await manager.create_operation(
                agent_id=uuid4(),
                service=service,
                action=action,
                idempotency_key=uuid4(),
                request_fingerprint="b" * 64,
            )
            finished = await wait_for_terminal(store, accepted.operation_id)
            assert finished.status is OperationStatus.FAILED
            assert finished.error_code is ErrorCode.SCM_ACTION_FAILED
            assert backend.calls == expected_calls
        finally:
            await manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("action", "initial_state", "expected_calls"),
    [
        pytest.param(ServiceAction.START, SERVICE_STOPPED, ["start"], id="start"),
        pytest.param(ServiceAction.STOP, SERVICE_RUNNING, ["stop"], id="stop"),
        pytest.param(
            ServiceAction.RESTART,
            SERVICE_RUNNING,
            ["stop", "start"],
            id="restart",
        ),
    ],
)
def test_journal_brackets_every_scm_side_effect(
    tmp_path,
    action: ServiceAction,
    initial_state: int,
    expected_calls: list[str],
) -> None:
    async def scenario() -> None:
        store = AgentStore(tmp_path / "journal-barrier.sqlite3")
        store.initialize()

        class JournalBarrierSCM(FakeSCM):
            def __init__(self) -> None:
                super().__init__(initial_state)
                self.operation_id: UUID | None = None
                self.operation_ready = threading.Event()
                self.entered = [threading.Event() for _ in expected_calls]
                self.release = [threading.Event() for _ in expected_calls]
                self.snapshots: list[tuple[str, str, str]] = []

            def _before_side_effect(self, call: str) -> None:
                index = len(self.snapshots)
                assert self.operation_ready.wait(timeout=3)
                assert self.operation_id is not None
                with store.database.read() as connection:
                    row = connection.execute(
                        """
                        SELECT status, journal_state FROM operations
                        WHERE operation_id=?
                        """,
                        (str(self.operation_id),),
                    ).fetchone()
                assert row is not None
                self.snapshots.append((call, row["status"], row["journal_state"]))
                self.entered[index].set()
                assert self.release[index].wait(timeout=3)

            def start(self, name: str) -> None:
                self._before_side_effect("start")
                super().start(name)

            def stop(self, name: str) -> None:
                self._before_side_effect("stop")
                super().stop(name)

        backend = JournalBarrierSCM()
        service = AllowlistedService(
            local_service_id="mysql",
            windows_service_name="MySQL80",
        )
        manager = OperationManager(
            store=store,
            backend=backend,
            services={service.local_service_id: service},
            action_timeout_seconds=2,
            poll_interval_seconds=0.01,
        )
        await manager.start()
        try:
            accepted = await manager.create_operation(
                agent_id=uuid4(),
                service=service,
                action=action,
                idempotency_key=uuid4(),
                request_fingerprint="c" * 64,
            )
            backend.operation_id = accepted.operation_id
            backend.operation_ready.set()

            for index, expected_call in enumerate(expected_calls):
                assert await asyncio.to_thread(backend.entered[index].wait, 3)
                with store.database.read() as connection:
                    row = connection.execute(
                        """
                        SELECT status, journal_state FROM operations
                        WHERE operation_id=?
                        """,
                        (str(accepted.operation_id),),
                    ).fetchone()
                assert row is not None
                assert (row["status"], row["journal_state"]) == (
                    "RUNNING",
                    "DISPATCHING",
                )
                assert backend.snapshots[index] == (
                    expected_call,
                    "RUNNING",
                    "DISPATCHING",
                )
                backend.release[index].set()

            finished = await wait_for_terminal(store, accepted.operation_id)
            assert finished.status is OperationStatus.SUCCEEDED
            assert backend.calls == expected_calls
            with store.database.read() as connection:
                row = connection.execute(
                    """
                    SELECT status, journal_state FROM operations
                    WHERE operation_id=?
                    """,
                    (str(accepted.operation_id),),
                ).fetchone()
            assert row is not None
            assert (row["status"], row["journal_state"]) == (
                "SUCCEEDED",
                "COMPLETED",
            )
        finally:
            for release in backend.release:
                release.set()
            await manager.stop()

    asyncio.run(scenario())


def test_same_idempotency_key_converges_and_different_fingerprint_conflicts(tmp_path) -> None:
    async def scenario() -> None:
        store, manager, service = make_manager(tmp_path, FakeSCM())
        agent_id = uuid4()
        key = uuid4()
        results = await asyncio.gather(
            *(
                manager.create_operation(
                    agent_id=agent_id,
                    service=service,
                    action=ServiceAction.START,
                    idempotency_key=key,
                    request_fingerprint="a" * 64,
                )
                for _ in range(8)
            )
        )
        assert len({item.operation_id for item in results}) == 1
        with pytest.raises(ApiError) as raised:
            await manager.create_operation(
                agent_id=agent_id,
                service=service,
                action=ServiceAction.START,
                idempotency_key=key,
                request_fingerprint="b" * 64,
            )
        assert raised.value.code is ErrorCode.IDEMPOTENCY_KEY_REUSED
        assert len(store.list_pending_operation_ids()) == 1

    asyncio.run(scenario())


def test_concurrent_identical_retries_create_and_dispatch_exactly_once(tmp_path) -> None:
    async def scenario() -> None:
        backend = GatedObservationSCM()
        store = NotifyingStore(tmp_path / "same-key.sqlite3")
        store.initialize()
        service = AllowlistedService(
            local_service_id="mysql",
            windows_service_name="MySQL80",
        )
        manager = OperationManager(
            store=store,
            backend=backend,
            services={service.local_service_id: service},
            action_timeout_seconds=1,
            poll_interval_seconds=0.01,
        )
        await manager.start()
        signaling_lock = SignalingAsyncLock()
        manager._service_locks[service.local_service_id] = signaling_lock
        request_gate = asyncio.Event()
        key = uuid4()

        async def submit() -> Operation:
            await request_gate.wait()
            return await manager.create_operation(
                agent_id=agent_id,
                service=service,
                action=ServiceAction.START,
                idempotency_key=key,
                request_fingerprint="a" * 64,
            )

        agent_id = uuid4()
        tasks = [asyncio.create_task(submit()) for _ in range(2)]
        try:
            request_gate.set()
            assert await asyncio.to_thread(backend.first_query_started.wait, 3)
            await asyncio.wait_for(signaling_lock.second_attempted.wait(), timeout=3)
            assert backend.query_count == 1
            assert backend.calls == []
            backend.release_first_query.set()

            results = await asyncio.gather(*tasks)
            assert len({item.operation_id for item in results}) == 1
            assert await asyncio.to_thread(store.operation_finished.wait, 3)
            assert backend.calls == ["start"]
            with store.database.read() as connection:
                operation_count = connection.execute(
                    "SELECT COUNT(*) FROM operations"
                ).fetchone()[0]
            assert operation_count == 1
        finally:
            backend.release_first_query.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await manager.stop()

    asyncio.run(scenario())


def test_concurrent_fingerprint_conflict_has_one_durable_winner(tmp_path) -> None:
    async def scenario() -> None:
        backend = GatedObservationSCM()
        store = NotifyingStore(tmp_path / "conflicting-key.sqlite3")
        store.initialize()
        service = AllowlistedService(
            local_service_id="mysql",
            windows_service_name="MySQL80",
        )
        manager = OperationManager(
            store=store,
            backend=backend,
            services={service.local_service_id: service},
            action_timeout_seconds=1,
            poll_interval_seconds=0.01,
        )
        await manager.start()
        signaling_lock = SignalingAsyncLock()
        manager._service_locks[service.local_service_id] = signaling_lock
        request_gate = asyncio.Event()
        key = uuid4()
        fingerprints = ("a" * 64, "b" * 64)

        async def submit(fingerprint: str) -> Operation:
            await request_gate.wait()
            return await manager.create_operation(
                agent_id=agent_id,
                service=service,
                action=ServiceAction.START,
                idempotency_key=key,
                request_fingerprint=fingerprint,
            )

        agent_id = uuid4()
        tasks = [
            asyncio.create_task(submit(fingerprint))
            for fingerprint in fingerprints
        ]
        try:
            request_gate.set()
            assert await asyncio.to_thread(backend.first_query_started.wait, 3)
            await asyncio.wait_for(signaling_lock.second_attempted.wait(), timeout=3)
            assert backend.query_count == 1
            backend.release_first_query.set()

            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            accepted = [item for item in outcomes if isinstance(item, Operation)]
            conflicts = [item for item in outcomes if isinstance(item, ApiError)]
            assert len(accepted) == 1
            assert len(conflicts) == 1
            assert conflicts[0].status_code == 409
            assert conflicts[0].code is ErrorCode.IDEMPOTENCY_KEY_REUSED
            assert await asyncio.to_thread(store.operation_finished.wait, 3)
            assert backend.calls == ["start"]

            losing_fingerprint = next(
                item
                for item in fingerprints
                if item != accepted[0].request_fingerprint
            )
            with pytest.raises(ApiError) as repeated:
                await manager.create_operation(
                    agent_id=agent_id,
                    service=service,
                    action=ServiceAction.START,
                    idempotency_key=key,
                    request_fingerprint=losing_fingerprint,
                )
            assert repeated.value.status_code == 409
            assert repeated.value.code is ErrorCode.IDEMPOTENCY_KEY_REUSED
            with store.database.read() as connection:
                operation_count = connection.execute(
                    "SELECT COUNT(*) FROM operations"
                ).fetchone()[0]
            assert operation_count == 1
        finally:
            backend.release_first_query.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await manager.stop()

    asyncio.run(scenario())


def test_different_keys_on_one_service_serialize_observation_intervals(tmp_path) -> None:
    async def scenario() -> None:
        backend = GatedObservationSCM(SERVICE_RUNNING)
        store, manager, service = make_manager(tmp_path, backend)
        signaling_lock = SignalingAsyncLock()
        manager._service_locks[service.local_service_id] = signaling_lock
        request_gate = asyncio.Event()

        async def submit(fingerprint: str) -> Operation:
            await request_gate.wait()
            return await manager.create_operation(
                agent_id=agent_id,
                service=service,
                action=ServiceAction.START,
                idempotency_key=uuid4(),
                request_fingerprint=fingerprint,
            )

        agent_id = uuid4()
        tasks = [
            asyncio.create_task(submit("a" * 64)),
            asyncio.create_task(submit("b" * 64)),
        ]
        try:
            request_gate.set()
            assert await asyncio.to_thread(backend.first_query_started.wait, 3)
            await asyncio.wait_for(signaling_lock.second_attempted.wait(), timeout=3)
            assert backend.query_count == 1
            backend.release_first_query.set()

            results = await asyncio.gather(*tasks)
            assert all(item.status is OperationStatus.SUCCEEDED for item in results)
            assert backend.max_active_queries == 1
            assert [item[0] for item in backend.intervals] == [
                "enter",
                "exit",
                "enter",
                "exit",
            ]
            assert backend.calls == []
        finally:
            backend.release_first_query.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_different_services_observe_in_parallel(tmp_path) -> None:
    async def scenario() -> None:
        backend = ParallelObservationSCM()
        store = AgentStore(tmp_path / "parallel-services.sqlite3")
        store.initialize()
        services = [
            AllowlistedService(
                local_service_id="mysql",
                windows_service_name="MySQL80",
            ),
            AllowlistedService(
                local_service_id="redis",
                windows_service_name="Redis",
            ),
        ]
        manager = OperationManager(
            store=store,
            backend=backend,
            services={service.local_service_id: service for service in services},
        )
        request_gate = asyncio.Event()

        async def submit(service: AllowlistedService) -> Operation:
            await request_gate.wait()
            return await manager.create_operation(
                agent_id=agent_id,
                service=service,
                action=ServiceAction.START,
                idempotency_key=uuid4(),
                request_fingerprint=(
                    "a" * 64 if service.local_service_id == "mysql" else "b" * 64
                ),
            )

        agent_id = uuid4()
        tasks = [asyncio.create_task(submit(service)) for service in services]
        request_gate.set()
        results = await asyncio.gather(*tasks)

        assert {item.local_service_id for item in results} == {"mysql", "redis"}
        assert all(item.status is OperationStatus.PENDING for item in results)
        assert backend.max_active_queries == 2
        assert backend.calls == []

    asyncio.run(scenario())


def test_idempotency_survives_store_and_manager_reconstruction(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "rebuilt-store.sqlite3"
        service = AllowlistedService(
            local_service_id="mysql",
            windows_service_name="MySQL80",
        )
        first_store = AgentStore(database_path)
        first_store.initialize()
        first_backend = CountingSCM()
        first_manager = OperationManager(
            store=first_store,
            backend=first_backend,
            services={service.local_service_id: service},
        )
        key = uuid4()
        fingerprint = "a" * 64
        original = await first_manager.create_operation(
            agent_id=uuid4(),
            service=service,
            action=ServiceAction.START,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
        assert first_backend.query_count == 1

        rebuilt_store = AgentStore(database_path)
        rebuilt_store.initialize()
        rebuilt_backend = CountingSCM()
        rebuilt_backend.fail_queries = True
        rebuilt_manager = OperationManager(
            store=rebuilt_store,
            backend=rebuilt_backend,
            services={service.local_service_id: service},
        )
        replay = await rebuilt_manager.create_operation(
            agent_id=original.agent_id,
            service=service,
            action=ServiceAction.START,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )

        assert replay.operation_id == original.operation_id
        assert replay.status is original.status
        assert rebuilt_backend.query_count == 0

    asyncio.run(scenario())


def test_idempotent_replay_is_resolved_before_mutable_scm_state(tmp_path) -> None:
    async def scenario() -> None:
        backend = CountingSCM()
        _, manager, service = make_manager(tmp_path, backend)
        key = uuid4()
        first = await manager.create_operation(
            agent_id=uuid4(),
            service=service,
            action=ServiceAction.START,
            idempotency_key=key,
            request_fingerprint="a" * 64,
        )
        backend.fail_queries = True
        replay = await manager.create_operation(
            agent_id=first.agent_id,
            service=service,
            action=ServiceAction.START,
            idempotency_key=key,
            request_fingerprint="a" * 64,
        )
        assert replay.operation_id == first.operation_id
        assert backend.query_count == 1

    asyncio.run(scenario())


def test_second_action_on_same_service_is_persisted_as_rejected(tmp_path) -> None:
    async def scenario() -> None:
        _, manager, service = make_manager(tmp_path, FakeSCM())
        first = await manager.create_operation(
            agent_id=uuid4(),
            service=service,
            action=ServiceAction.START,
            idempotency_key=uuid4(),
            request_fingerprint="a" * 64,
        )
        second = await manager.create_operation(
            agent_id=first.agent_id,
            service=service,
            action=ServiceAction.START,
            idempotency_key=uuid4(),
            request_fingerprint="b" * 64,
        )
        assert first.status is OperationStatus.PENDING
        assert second.status is OperationStatus.REJECTED
        assert second.error_code is ErrorCode.SERVICE_ACTION_CONFLICT

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("action", "initial_state", "expected_calls"),
    [
        pytest.param(ServiceAction.START, SERVICE_STOPPED, ["start"], id="start"),
        pytest.param(ServiceAction.STOP, SERVICE_RUNNING, ["stop"], id="stop"),
        pytest.param(
            ServiceAction.RESTART,
            SERVICE_RUNNING,
            ["stop", "start"],
            id="restart",
        ),
    ],
)
def test_prepared_recovery_executes_each_side_effect_exactly_once(
    tmp_path,
    action: ServiceAction,
    initial_state: int,
    expected_calls: list[str],
) -> None:
    async def scenario() -> None:
        backend = FakeSCM(initial_state)
        store, manager, service = make_manager(tmp_path, backend)
        pending = store.reserve_operation(
            agent_id=uuid4(),
            service=service,
            action=action,
            idempotency_key=uuid4(),
            request_fingerprint="a" * 64,
        ).operation
        await manager.start()
        try:
            current = await wait_for_terminal(store, pending.operation_id)
            assert current.status is OperationStatus.SUCCEEDED
            assert backend.calls == expected_calls
            with store.database.read() as connection:
                journal_state = connection.execute(
                    "SELECT journal_state FROM operations WHERE operation_id=?",
                    (str(pending.operation_id),),
                ).fetchone()[0]
            assert journal_state == "COMPLETED"
        finally:
            await manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("action", "state", "expected"),
    [
        (ServiceAction.START, SERVICE_RUNNING, OperationStatus.SUCCEEDED),
        (ServiceAction.STOP, SERVICE_STOPPED, OperationStatus.SUCCEEDED),
        (ServiceAction.RESTART, SERVICE_RUNNING, OperationStatus.UNKNOWN),
    ],
)
def test_dispatching_recovery_never_replays_side_effect(
    tmp_path, action: ServiceAction, state: int, expected: OperationStatus
) -> None:
    async def scenario() -> None:
        backend = FakeSCM(state)
        store, manager, service = make_manager(tmp_path, backend)
        reserved = store.reserve_operation(
            agent_id=uuid4(),
            service=service,
            action=action,
            idempotency_key=uuid4(),
            request_fingerprint="a" * 64,
        ).operation
        claimed = store.claim_operation(reserved.operation_id)
        assert claimed is not None
        await manager.recover_dispatching()
        recovered = store.get_operation(reserved.operation_id)
        assert recovered is not None
        assert recovered.status is expected
        assert backend.calls == []
        with store.database.read() as connection:
            journal_state = connection.execute(
                "SELECT journal_state FROM operations WHERE operation_id=?",
                (str(reserved.operation_id),),
            ).fetchone()[0]
        assert journal_state == "COMPLETED"

    asyncio.run(scenario())


def test_dispatching_stop_rejects_failed_as_the_wrong_target_state(tmp_path) -> None:
    class FailedExitSCM(FakeSCM):
        def query(self, _name: str) -> SCMServiceStatus:
            return SCMServiceStatus(
                SERVICE_STOPPED,
                win32_exit_code=1,
                start_type=SERVICE_DEMAND_START,
            )

    async def scenario() -> None:
        backend = FailedExitSCM()
        store, manager, service = make_manager(tmp_path, backend)
        reserved = store.reserve_operation(
            agent_id=uuid4(),
            service=service,
            action=ServiceAction.STOP,
            idempotency_key=uuid4(),
            request_fingerprint="a" * 64,
        ).operation
        assert store.claim_operation(reserved.operation_id) is not None

        await manager.recover_dispatching()

        recovered = store.get_operation(reserved.operation_id)
        assert recovered is not None
        assert recovered.status is OperationStatus.FAILED
        assert recovered.error_code is ErrorCode.SCM_ACTION_FAILED
        assert backend.calls == []

    asyncio.run(scenario())


def test_service_mapping_binding_is_case_insensitive_and_immutable(tmp_path) -> None:
    store = AgentStore(tmp_path / "bindings.sqlite3")
    store.initialize()
    store.bind_service_mappings(
        [AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")]
    )
    store.bind_service_mappings(
        [AllowlistedService(local_service_id="mysql", windows_service_name="mysql80")]
    )

    with pytest.raises(RuntimeError, match="service mapping changed"):
        store.bind_service_mappings(
            [AllowlistedService(local_service_id="mysql", windows_service_name="Spooler")]
        )

    with store.database.read() as connection:
        version = connection.execute(
            "SELECT version FROM schema_versions WHERE component='recovery_agent'"
        ).fetchone()[0]
        binding = connection.execute(
            "SELECT * FROM service_bindings WHERE local_service_id='mysql'"
        ).fetchone()
    assert version == 2
    assert binding["windows_service_name"] == "MySQL80"
    assert binding["windows_service_name_casefold"] == "mysql80"


def test_v2_migration_uses_historical_operation_as_service_binding(tmp_path) -> None:
    store = AgentStore(tmp_path / "historical.sqlite3")
    store.database.initialize("recovery_agent", [_migration_v1])
    service = AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")
    store.reserve_operation(
        agent_id=uuid4(),
        service=service,
        action=ServiceAction.START,
        idempotency_key=uuid4(),
        request_fingerprint="a" * 64,
        initial_status=OperationStatus.SUCCEEDED,
    )

    store.initialize()

    with pytest.raises(RuntimeError, match="service mapping changed"):
        store.bind_service_mappings(
            [AllowlistedService(local_service_id="mysql", windows_service_name="Spooler")]
        )


def test_v2_migration_rejects_conflicting_historical_mappings_atomically(tmp_path) -> None:
    store = AgentStore(tmp_path / "conflicting-history.sqlite3")
    store.database.initialize("recovery_agent", [_migration_v1])
    for index, windows_service_name in enumerate(("MySQL80", "Spooler")):
        store.reserve_operation(
            agent_id=uuid4(),
            service=AllowlistedService(
                local_service_id="mysql",
                windows_service_name=windows_service_name,
            ),
            action=ServiceAction.START,
            idempotency_key=uuid4(),
            request_fingerprint=f"{index + 1}" * 64,
            initial_status=OperationStatus.SUCCEEDED,
        )

    with pytest.raises(RuntimeError, match="historical service mapping changed"):
        store.initialize()

    with store.database.read() as connection:
        version = connection.execute(
            "SELECT version FROM schema_versions WHERE component='recovery_agent'"
        ).fetchone()[0]
        binding_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_bindings'"
        ).fetchone()
    assert version == 1
    assert binding_table is None


def test_prepared_operation_fails_without_touching_a_rebound_target(tmp_path) -> None:
    class RecordingSCM(FakeSCM):
        def query(self, name: str) -> SCMServiceStatus:
            self.calls.append(f"query:{name}")
            return super().query(name)

    async def scenario() -> None:
        backend = RecordingSCM()
        store = AgentStore(tmp_path / "prepared-rebind.sqlite3")
        store.initialize()
        old_service = AllowlistedService(
            local_service_id="mysql", windows_service_name="MySQL80"
        )
        operation = store.reserve_operation(
            agent_id=uuid4(),
            service=old_service,
            action=ServiceAction.START,
            idempotency_key=uuid4(),
            request_fingerprint="a" * 64,
        ).operation
        rebound = AllowlistedService(
            local_service_id="mysql", windows_service_name="Spooler"
        )
        manager = OperationManager(
            store=store,
            backend=backend,
            services={"mysql": rebound},
            action_timeout_seconds=1,
            poll_interval_seconds=0.01,
        )

        await manager.start()
        try:
            for _ in range(100):
                current = store.get_operation(operation.operation_id)
                assert current is not None
                if current.status.terminal:
                    break
                await asyncio.sleep(0.01)
            assert current.status is OperationStatus.FAILED
            assert current.error_code is ErrorCode.SERVICE_MAPPING_CHANGED
            assert backend.calls == []
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_dispatching_operation_becomes_unknown_without_querying_rebound_target(
    tmp_path,
) -> None:
    class RecordingSCM(FakeSCM):
        def query(self, name: str) -> SCMServiceStatus:
            self.calls.append(f"query:{name}")
            return super().query(name)

    async def scenario() -> None:
        backend = RecordingSCM()
        store = AgentStore(tmp_path / "dispatching-rebind.sqlite3")
        store.initialize()
        old_service = AllowlistedService(
            local_service_id="mysql", windows_service_name="MySQL80"
        )
        operation = store.reserve_operation(
            agent_id=uuid4(),
            service=old_service,
            action=ServiceAction.START,
            idempotency_key=uuid4(),
            request_fingerprint="a" * 64,
        ).operation
        assert store.claim_operation(operation.operation_id) is not None
        rebound = AllowlistedService(
            local_service_id="mysql", windows_service_name="Spooler"
        )
        manager = OperationManager(
            store=store,
            backend=backend,
            services={"mysql": rebound},
        )

        await manager.recover_dispatching()

        recovered = store.get_operation(operation.operation_id)
        assert recovered is not None
        assert recovered.status is OperationStatus.UNKNOWN
        assert recovered.error_code is ErrorCode.SERVICE_MAPPING_CHANGED
        assert backend.calls == []

    asyncio.run(scenario())


def test_casing_only_mapping_change_uses_the_persisted_operation_target(tmp_path) -> None:
    class RecordingSCM(FakeSCM):
        def query(self, name: str) -> SCMServiceStatus:
            self.calls.append(f"query:{name}")
            return super().query(name)

        def start(self, name: str) -> None:
            self.calls.append(f"start:{name}")
            self.state = SERVICE_RUNNING

    async def scenario() -> None:
        backend = RecordingSCM()
        store = AgentStore(tmp_path / "case-only.sqlite3")
        store.initialize()
        persisted = AllowlistedService(
            local_service_id="mysql", windows_service_name="MySQL80"
        )
        operation = store.reserve_operation(
            agent_id=uuid4(),
            service=persisted,
            action=ServiceAction.START,
            idempotency_key=uuid4(),
            request_fingerprint="a" * 64,
        ).operation
        current = AllowlistedService(
            local_service_id="mysql", windows_service_name="mysql80"
        )
        manager = OperationManager(
            store=store,
            backend=backend,
            services={"mysql": current},
            action_timeout_seconds=1,
            poll_interval_seconds=0.01,
        )

        await manager.start()
        try:
            for _ in range(100):
                finished = store.get_operation(operation.operation_id)
                assert finished is not None
                if finished.status.terminal:
                    break
                await asyncio.sleep(0.01)
            assert finished.status is OperationStatus.SUCCEEDED
            assert all("mysql80" not in call for call in backend.calls)
            assert any(call == "start:MySQL80" for call in backend.calls)
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_live_stop_fails_when_scm_reports_failed_instead_of_inactive(tmp_path) -> None:
    class FailedAfterStopSCM(FakeSCM):
        def __init__(self) -> None:
            super().__init__(SERVICE_RUNNING)
            self.failed = False

        def query(self, _name: str) -> SCMServiceStatus:
            return SCMServiceStatus(
                self.state,
                win32_exit_code=1 if self.failed else 0,
                start_type=SERVICE_DEMAND_START,
            )

        def stop(self, name: str) -> None:
            self.calls.append("stop")
            self.state = SERVICE_STOPPED
            self.failed = True

    async def scenario() -> None:
        backend = FailedAfterStopSCM()
        store, manager, service = make_manager(tmp_path, backend)
        await manager.start()
        try:
            operation = await manager.create_operation(
                agent_id=uuid4(),
                service=service,
                action=ServiceAction.STOP,
                idempotency_key=uuid4(),
                request_fingerprint="a" * 64,
            )
            for _ in range(100):
                finished = store.get_operation(operation.operation_id)
                assert finished is not None
                if finished.status.terminal:
                    break
                await asyncio.sleep(0.01)
            assert finished.status is OperationStatus.FAILED
            assert finished.error_code is ErrorCode.SCM_ACTION_FAILED
            assert backend.calls == ["stop"]
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_slow_scm_side_effect_times_out_and_quarantines_followup_action(tmp_path) -> None:
    class SlowStartSCM(FakeSCM):
        def start(self, name: str) -> None:
            time.sleep(0.25)
            super().start(name)

    async def scenario() -> None:
        backend = SlowStartSCM()
        store = AgentStore(tmp_path / "slow-agent.sqlite3")
        store.initialize()
        service = AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")
        manager = OperationManager(
            store=store,
            backend=backend,
            services={service.local_service_id: service},
            action_timeout_seconds=0.1,
            poll_interval_seconds=0.01,
        )
        await manager.start()
        try:
            first = await manager.create_operation(
                agent_id=uuid4(),
                service=service,
                action=ServiceAction.START,
                idempotency_key=uuid4(),
                request_fingerprint="a" * 64,
            )
            await asyncio.sleep(0.15)
            timed_out = store.get_operation(first.operation_id)
            assert timed_out is not None
            assert timed_out.status is OperationStatus.FAILED
            assert timed_out.error_code is ErrorCode.SCM_ACTION_TIMEOUT
            assert backend.state == SERVICE_STOPPED

            conflicting = await manager.create_operation(
                agent_id=first.agent_id,
                service=service,
                action=ServiceAction.STOP,
                idempotency_key=uuid4(),
                request_fingerprint="b" * 64,
            )
            assert conflicting.status is OperationStatus.REJECTED
            assert conflicting.error_code is ErrorCode.SERVICE_ACTION_CONFLICT

            await asyncio.sleep(0.2)
            finished = store.get_operation(first.operation_id)
            assert finished is not None
            assert finished.status is OperationStatus.FAILED
            assert finished.error_code is ErrorCode.SCM_ACTION_TIMEOUT
            assert backend.state == SERVICE_RUNNING
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_queued_request_rechecks_quarantine_after_acquiring_service_lock(tmp_path) -> None:
    class BlockingNativeStartSCM(FakeSCM):
        def __init__(self) -> None:
            super().__init__()
            self.native_started = threading.Event()
            self.release_native = threading.Event()

        def start(self, _name: str) -> None:
            self.calls.append("start")
            self.native_started.set()
            if not self.release_native.wait(timeout=3):
                raise RuntimeError("test did not release the native SCM call")
            self.state = SERVICE_RUNNING

    class QuarantineRaceLock:
        """Queue the follow-up before letting the timing-out action acquire."""

        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self.followup_waiting = asyncio.Event()
            self.release_followup = asyncio.Event()

        async def __aenter__(self) -> "QuarantineRaceLock":
            current = asyncio.current_task()
            if current is not None and current.get_name() == "queued-followup":
                self.followup_waiting.set()
                await self.release_followup.wait()
            await self._lock.acquire()
            return self

        async def __aexit__(self, *_args) -> None:
            self._lock.release()

    async def scenario() -> None:
        backend = BlockingNativeStartSCM()
        store = AgentStore(tmp_path / "quarantine-race.sqlite3")
        store.initialize()
        service = AllowlistedService(
            local_service_id="mysql",
            windows_service_name="MySQL80",
        )
        manager = OperationManager(
            store=store,
            backend=backend,
            services={service.local_service_id: service},
            action_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
        )
        race_lock = QuarantineRaceLock()
        manager._service_locks[service.local_service_id] = race_lock
        agent_id = uuid4()

        followup_task = asyncio.create_task(
            manager.create_operation(
                agent_id=agent_id,
                service=service,
                action=ServiceAction.START,
                idempotency_key=uuid4(),
                request_fingerprint="b" * 64,
            ),
            name="queued-followup",
        )
        try:
            # The follow-up has passed the lock-free quarantine/admission
            # checks and is queued before the first action times out.
            await asyncio.wait_for(race_lock.followup_waiting.wait(), timeout=3)
            first = store.reserve_operation(
                agent_id=agent_id,
                service=service,
                action=ServiceAction.START,
                idempotency_key=uuid4(),
                request_fingerprint="a" * 64,
            ).operation
            first_task = asyncio.create_task(
                manager._execute(first.operation_id),
                name="timing-out-action",
            )
            assert await asyncio.to_thread(backend.native_started.wait, 3)
            await asyncio.wait_for(first_task, timeout=3)

            timed_out = store.get_operation(first.operation_id)
            assert timed_out is not None
            assert timed_out.status is OperationStatus.FAILED
            assert timed_out.error_code is ErrorCode.SCM_ACTION_TIMEOUT
            native_call = manager._native_quarantine.get(service.local_service_id)
            assert native_call is not None
            assert not native_call.done()

            race_lock.release_followup.set()
            conflicting = await asyncio.wait_for(followup_task, timeout=3)
            assert conflicting.status is OperationStatus.REJECTED
            assert conflicting.error_code is ErrorCode.SERVICE_ACTION_CONFLICT
            assert backend.calls == ["start"]

            # Once the late native call returns, quarantine is released and a
            # new request is admitted without replaying the start side effect.
            backend.release_native.set()
            await asyncio.wait_for(native_call, timeout=3)
            await asyncio.sleep(0)
            assert service.local_service_id not in manager._native_quarantine
            accepted = await manager.create_operation(
                agent_id=agent_id,
                service=service,
                action=ServiceAction.START,
                idempotency_key=uuid4(),
                request_fingerprint="c" * 64,
            )
            assert accepted.status is OperationStatus.SUCCEEDED
            assert backend.calls == ["start"]
        finally:
            race_lock.release_followup.set()
            backend.release_native.set()
            await asyncio.gather(followup_task, return_exceptions=True)

    asyncio.run(scenario())


def test_initial_scm_query_timeout_creates_a_durable_failed_operation(tmp_path) -> None:
    class BlockingQuerySCM(FakeSCM):
        def __init__(self) -> None:
            super().__init__()
            self.query_started = threading.Event()
            self.release_query = threading.Event()

        def query(self, name: str) -> SCMServiceStatus:
            self.query_started.set()
            if not self.release_query.wait(timeout=3):
                raise RuntimeError("test did not release the SCM query")
            return super().query(name)

    async def scenario() -> None:
        backend = BlockingQuerySCM()
        store = AgentStore(tmp_path / "query-timeout.sqlite3")
        store.initialize()
        service = AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")
        manager = OperationManager(
            store=store,
            backend=backend,
            services={service.local_service_id: service},
            action_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
        )
        try:
            started = asyncio.get_running_loop().time()
            operation = await manager.create_operation(
                agent_id=uuid4(),
                service=service,
                action=ServiceAction.START,
                idempotency_key=uuid4(),
                request_fingerprint="a" * 64,
            )
            elapsed = asyncio.get_running_loop().time() - started
            assert backend.query_started.is_set()
            assert elapsed < 0.3
            assert operation.status is OperationStatus.FAILED
            assert operation.error_code is ErrorCode.SCM_ACTION_TIMEOUT
            assert store.get_operation(operation.operation_id) == operation
            assert backend.calls == []
        finally:
            backend.release_query.set()

    asyncio.run(scenario())


def test_dispatching_recovery_query_timeout_finishes_unknown_without_replay(tmp_path) -> None:
    class BlockingQuerySCM(FakeSCM):
        def __init__(self) -> None:
            super().__init__()
            self.release_query = threading.Event()

        def query(self, name: str) -> SCMServiceStatus:
            if not self.release_query.wait(timeout=3):
                raise RuntimeError("test did not release the SCM query")
            return super().query(name)

    async def scenario() -> None:
        backend = BlockingQuerySCM()
        store, manager, service = make_manager(tmp_path, backend)
        manager.action_timeout_seconds = 0.05
        operation = store.reserve_operation(
            agent_id=uuid4(),
            service=service,
            action=ServiceAction.START,
            idempotency_key=uuid4(),
            request_fingerprint="a" * 64,
        ).operation
        assert store.claim_operation(operation.operation_id) is not None
        try:
            await manager.recover_dispatching()
            recovered = store.get_operation(operation.operation_id)
            assert recovered is not None
            assert recovered.status is OperationStatus.UNKNOWN
            assert recovered.error_code is ErrorCode.SCM_ACTION_TIMEOUT
            assert backend.calls == []
        finally:
            backend.release_query.set()

    asyncio.run(scenario())


def test_target_observed_only_after_deadline_is_not_reported_success(tmp_path) -> None:
    class SlowFinalQuerySCM(FakeSCM):
        def __init__(self) -> None:
            super().__init__()
            self.query_count = 0

        def query(self, name: str) -> SCMServiceStatus:
            self.query_count += 1
            if self.query_count >= 3:
                time.sleep(0.15)
            return super().query(name)

    async def scenario() -> None:
        backend = SlowFinalQuerySCM()
        store = AgentStore(tmp_path / "slow-query-agent.sqlite3")
        store.initialize()
        service = AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")
        manager = OperationManager(
            store=store,
            backend=backend,
            services={service.local_service_id: service},
            action_timeout_seconds=0.1,
            poll_interval_seconds=0.01,
        )
        await manager.start()
        try:
            operation = await manager.create_operation(
                agent_id=uuid4(),
                service=service,
                action=ServiceAction.START,
                idempotency_key=uuid4(),
                request_fingerprint="a" * 64,
            )
            await asyncio.sleep(0.3)
            finished = store.get_operation(operation.operation_id)
            assert finished is not None
            assert finished.status is OperationStatus.FAILED
            assert finished.error_code is ErrorCode.SCM_ACTION_TIMEOUT
        finally:
            await manager.stop()

    asyncio.run(scenario())
