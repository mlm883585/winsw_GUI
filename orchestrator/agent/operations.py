from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from uuid import UUID

from orchestrator.agent.config import AllowlistedService
from orchestrator.agent.scm import (
    SCMAccessDenied,
    SCMBackend,
    SCMError,
    SCMServiceNotFound,
    observe_service,
    observe_service_strict,
)
from orchestrator.agent.store import AgentStore
from orchestrator.common.enums import InstallationState, OperationStatus, RuntimeState, ServiceAction
from orchestrator.common.errors import ErrorCode
from orchestrator.common.models import Operation


class _RejectedState(RuntimeError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OperationManager:
    """Durable operation dispatcher with one persistent active slot per service."""

    def __init__(
        self,
        *,
        store: AgentStore,
        backend: SCMBackend,
        services: Mapping[str, AllowlistedService],
        action_timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self.store = store
        self.backend = backend
        self.services = dict(services)
        self.action_timeout_seconds = action_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._service_locks = {service_id: asyncio.Lock() for service_id in self.services}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._active_tasks: dict[UUID, asyncio.Task[None]] = {}
        # A native SCM side effect cannot be force-cancelled safely once the OS
        # call has begun.  If its public deadline expires, the Operation is
        # completed as FAILED immediately while this per-service quarantine
        # prevents a second action until the late native call actually returns.
        self._native_quarantine: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        if self._worker is not None:
            return
        # Lifespan may be restarted by an ASGI host; primitives must belong to its current loop.
        self._service_locks = {service_id: asyncio.Lock() for service_id in self.services}
        self._wake = asyncio.Event()
        await self.recover_dispatching()
        self._worker = asyncio.create_task(self._worker_loop(), name="agent-operation-worker")

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        tasks = list(self._active_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()

    async def create_operation(
        self,
        *,
        agent_id: UUID,
        service: AllowlistedService,
        action: ServiceAction,
        idempotency_key: UUID,
        request_fingerprint: str,
    ) -> Operation:
        existing = self.store.find_idempotent_operation(
            idempotency_key,
            request_fingerprint,
        )
        if existing is not None:
            return existing

        native_call = self._native_quarantine.get(service.local_service_id)
        if native_call is not None and not native_call.done():
            return self.store.reserve_operation(
                agent_id=agent_id,
                service=service,
                action=action,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                initial_status=OperationStatus.REJECTED,
                error_code=ErrorCode.SERVICE_ACTION_CONFLICT,
                message="A timed-out native SCM call is still settling for this service",
            ).operation

        admission = self.store.resolve_replay_or_reject_active(
            agent_id=agent_id,
            service=service,
            action=action,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if admission is not None:
            return admission.operation

        # Creation observes mutable SCM state, so it participates in the same
        # per-service critical section as dispatch.  Recheck idempotency after
        # acquiring the lock: concurrent retries must not perform a second
        # observation after the first request has durably reserved its key.
        lock = self._service_locks[service.local_service_id]
        async with lock:
            # A request can pass the lock-free fast-path check immediately
            # before an older native SCM call times out and establishes the
            # quarantine while this request is waiting for the service lock.
            # Recheck inside the critical section before any Store admission
            # or SCM observation so no queued request can escape quarantine.
            native_call = self._native_quarantine.get(service.local_service_id)
            if native_call is not None and not native_call.done():
                return self.store.reserve_operation(
                    agent_id=agent_id,
                    service=service,
                    action=action,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    initial_status=OperationStatus.REJECTED,
                    error_code=ErrorCode.SERVICE_ACTION_CONFLICT,
                    message="A timed-out native SCM call is still settling for this service",
                ).operation
            admission = self.store.resolve_replay_or_reject_active(
                agent_id=agent_id,
                service=service,
                action=action,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if admission is not None:
                return admission.operation
            deadline = asyncio.get_running_loop().time() + self.action_timeout_seconds
            try:
                observed = await self._observe_with_deadline(service, deadline)
            except TimeoutError:
                initial_status = OperationStatus.FAILED
                error_code = ErrorCode.SCM_ACTION_TIMEOUT
                message = "Timed out while querying the Windows service state"
            else:
                initial_status, error_code, message = self._initial_outcome(action, observed)
            reserved = self.store.reserve_operation(
                agent_id=agent_id,
                service=service,
                action=action,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                initial_status=initial_status,
                error_code=error_code,
                message=message,
            )
            if reserved.created and reserved.operation.status is OperationStatus.PENDING:
                self._wake.set()
            return reserved.operation

    @staticmethod
    def _initial_outcome(action: ServiceAction, observed) -> tuple[OperationStatus, ErrorCode | None, str | None]:
        if observed.installation_state is InstallationState.NOT_INSTALLED:
            return (
                OperationStatus.REJECTED,
                ErrorCode.SERVICE_NOT_INSTALLED,
                "The allowlisted Windows service is not installed",
            )
        if (
            observed.installation_state is InstallationState.UNKNOWN
            or observed.runtime_state is RuntimeState.UNKNOWN
        ):
            return (
                OperationStatus.REJECTED,
                ErrorCode.SERVICE_STATE_UNKNOWN,
                "The Windows service state cannot be determined safely",
            )

        state = observed.runtime_state
        if action is ServiceAction.START:
            if state is RuntimeState.ACTIVE:
                return OperationStatus.SUCCEEDED, None, "Service is already active"
            if state in {RuntimeState.INACTIVE, RuntimeState.FAILED, RuntimeState.STARTING}:
                return OperationStatus.PENDING, None, None
        elif action is ServiceAction.STOP:
            if state in {RuntimeState.INACTIVE, RuntimeState.FAILED}:
                return OperationStatus.SUCCEEDED, None, "Service is already stopped"
            if state in {RuntimeState.ACTIVE, RuntimeState.STOPPING}:
                return OperationStatus.PENDING, None, None
        elif action is ServiceAction.RESTART and state is RuntimeState.ACTIVE:
            return OperationStatus.PENDING, None, None
        return (
            OperationStatus.REJECTED,
            ErrorCode.SERVICE_ACTION_CONFLICT,
            f"Action {action.value} is not allowed while service state is {state.value}",
        )

    async def recover_dispatching(self) -> None:
        """Converge uncertain in-flight work without replaying an SCM side effect."""
        for operation in self.store.list_dispatching_operations():
            service = self._operation_target(operation)
            if service is None:
                self.store.finish_operation(
                    operation.operation_id,
                    OperationStatus.UNKNOWN,
                    error_code=ErrorCode.SERVICE_MAPPING_CHANGED,
                    message="Operation target mapping changed before Agent recovery",
                )
                continue
            if operation.action is ServiceAction.RESTART:
                self.store.finish_operation(
                    operation.operation_id,
                    OperationStatus.UNKNOWN,
                    message="Operation outcome cannot be proven after Agent restart",
                )
                continue
            native_call = self._native_quarantine.get(service.local_service_id)
            if native_call is not None and not native_call.done():
                self.store.finish_operation(
                    operation.operation_id,
                    OperationStatus.UNKNOWN,
                    error_code=ErrorCode.SCM_ACTION_TIMEOUT,
                    message="A native SCM call is still settling during Agent recovery",
                )
                continue
            deadline = asyncio.get_running_loop().time() + self.action_timeout_seconds
            try:
                observed = await self._observe_with_deadline(service, deadline)
            except TimeoutError:
                self.store.finish_operation(
                    operation.operation_id,
                    OperationStatus.UNKNOWN,
                    error_code=ErrorCode.SCM_ACTION_TIMEOUT,
                    message="Timed out while reconciling the Windows service state",
                )
                continue
            reached = (
                operation.action is ServiceAction.START
                and observed.runtime_state is RuntimeState.ACTIVE
            ) or (
                operation.action is ServiceAction.STOP
                and observed.runtime_state is RuntimeState.INACTIVE
            )
            if operation.action is ServiceAction.STOP and observed.runtime_state is RuntimeState.FAILED:
                self.store.finish_operation(
                    operation.operation_id,
                    OperationStatus.FAILED,
                    error_code=ErrorCode.SCM_ACTION_FAILED,
                    message="Windows service entered FAILED instead of the stop target state",
                )
                continue
            self.store.finish_operation(
                operation.operation_id,
                OperationStatus.SUCCEEDED if reached else OperationStatus.UNKNOWN,
                message=(
                    "Target state confirmed during Agent recovery"
                    if reached
                    else "Operation outcome cannot be proven after Agent restart"
                ),
            )

    async def _worker_loop(self) -> None:
        while True:
            for operation_id in self.store.list_pending_operation_ids():
                if operation_id in self._active_tasks:
                    continue
                task = asyncio.create_task(
                    self._execute(operation_id), name=f"agent-operation-{operation_id}"
                )
                self._active_tasks[operation_id] = task
                task.add_done_callback(lambda _task, oid=operation_id: self._task_done(oid))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.2)
            except TimeoutError:
                pass

    def _task_done(self, operation_id: UUID) -> None:
        task = self._active_tasks.pop(operation_id, None)
        if task is not None and not task.cancelled():
            # Retrieve unexpected exceptions so the event loop does not emit an unhandled-task warning.
            with suppress(Exception):
                task.exception()
        self._wake.set()

    async def _execute(self, operation_id: UUID) -> None:
        operation = self.store.get_operation(operation_id)
        if operation is None:
            return
        service = self._operation_target(operation)
        if service is None:
            self.store.finish_operation(
                operation_id,
                OperationStatus.FAILED,
                error_code=ErrorCode.SERVICE_MAPPING_CHANGED,
                message="Operation target mapping changed before dispatch",
            )
            return
        lock = self._service_locks[service.local_service_id]
        async with lock:
            claimed = self.store.claim_operation(operation_id)
            if claimed is None:
                return
            deadline = asyncio.get_running_loop().time() + self.action_timeout_seconds
            try:
                await self._apply_action(claimed, service, deadline)
                self.store.finish_operation(
                    operation_id,
                    OperationStatus.SUCCEEDED,
                    message="Target service state confirmed",
                )
            except asyncio.CancelledError:
                # Leave RUNNING/DISPATCHING durable state for deterministic startup reconciliation.
                raise
            except TimeoutError:
                self.store.finish_operation(
                    operation_id,
                    OperationStatus.FAILED,
                    error_code=ErrorCode.SCM_ACTION_TIMEOUT,
                    message="Timed out while waiting for the target service state",
                )
            except _RejectedState as exc:
                self.store.finish_operation(
                    operation_id,
                    OperationStatus.REJECTED,
                    error_code=exc.code,
                    message=exc.message,
                )
            except SCMServiceNotFound:
                self.store.finish_operation(
                    operation_id,
                    OperationStatus.FAILED,
                    error_code=ErrorCode.SERVICE_NOT_INSTALLED,
                    message="The allowlisted Windows service is no longer installed",
                )
            except (SCMAccessDenied, SCMError, OSError):
                self.store.finish_operation(
                    operation_id,
                    OperationStatus.FAILED,
                    error_code=ErrorCode.SCM_ACTION_FAILED,
                    message="Windows Service Control Manager rejected the action",
                )
            except Exception:
                self.store.finish_operation(
                    operation_id,
                    OperationStatus.FAILED,
                    error_code=ErrorCode.SCM_ACTION_FAILED,
                    message="Windows service action failed",
                )

    async def _apply_action(
        self,
        operation: Operation,
        service: AllowlistedService,
        deadline: float,
    ) -> None:
        observed = await self._observe_action_with_deadline(service, deadline)
        self._ensure_before_deadline(deadline)
        if observed.installation_state is InstallationState.NOT_INSTALLED:
            raise SCMServiceNotFound(service.windows_service_name)
        if (
            observed.installation_state is InstallationState.UNKNOWN
            or observed.runtime_state is RuntimeState.UNKNOWN
        ):
            raise _RejectedState(
                ErrorCode.SERVICE_STATE_UNKNOWN,
                "The Windows service state cannot be determined safely",
            )

        state = observed.runtime_state
        if operation.action is ServiceAction.START:
            if state is RuntimeState.ACTIVE:
                return
            if state in {RuntimeState.INACTIVE, RuntimeState.FAILED}:
                await self._run_scm_side_effect(
                    self.backend.start,
                    service.windows_service_name,
                    local_service_id=service.local_service_id,
                    deadline=deadline,
                )
                self._ensure_before_deadline(deadline)
            elif state is not RuntimeState.STARTING:
                raise _RejectedState(
                    ErrorCode.SERVICE_ACTION_CONFLICT,
                    f"Action start is not allowed while service state is {state.value}",
                )
            await self._wait_for(service, {RuntimeState.ACTIVE}, deadline)
            return

        if operation.action is ServiceAction.STOP:
            if state in {RuntimeState.INACTIVE, RuntimeState.FAILED}:
                return
            if state is RuntimeState.ACTIVE:
                await self._run_scm_side_effect(
                    self.backend.stop,
                    service.windows_service_name,
                    local_service_id=service.local_service_id,
                    deadline=deadline,
                )
                self._ensure_before_deadline(deadline)
            elif state is not RuntimeState.STOPPING:
                raise _RejectedState(
                    ErrorCode.SERVICE_ACTION_CONFLICT,
                    f"Action stop is not allowed while service state is {state.value}",
                )
            await self._wait_for(
                service,
                {RuntimeState.INACTIVE},
                deadline,
                failure_states={RuntimeState.FAILED},
            )
            return

        if state is not RuntimeState.ACTIVE:
            raise _RejectedState(
                ErrorCode.SERVICE_ACTION_CONFLICT,
                f"Action restart is not allowed while service state is {state.value}",
            )
        await self._run_scm_side_effect(
            self.backend.stop,
            service.windows_service_name,
            local_service_id=service.local_service_id,
            deadline=deadline,
        )
        self._ensure_before_deadline(deadline)
        await self._wait_for(
            service,
            {RuntimeState.INACTIVE},
            deadline,
            failure_states={RuntimeState.FAILED},
        )
        await self._run_scm_side_effect(
            self.backend.start,
            service.windows_service_name,
            local_service_id=service.local_service_id,
            deadline=deadline,
        )
        self._ensure_before_deadline(deadline)
        await self._wait_for(service, {RuntimeState.ACTIVE}, deadline)

    @staticmethod
    def _ensure_before_deadline(deadline: float) -> None:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError

    async def _run_scm_side_effect(
        self,
        function,
        *args,
        local_service_id: str,
        deadline: float,
    ) -> None:
        """Apply a native action with a hard public deadline and quarantine.

        ``asyncio.to_thread`` cannot terminate an already-running Win32 call.
        Shield it so timeout does not pretend cancellation succeeded, publish a
        deterministic timeout terminal state in the caller, and quarantine this
        service until the late call has really returned.
        """

        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._remaining(deadline),
            )
        except TimeoutError:
            self._quarantine_native_call(local_service_id, task)
            raise
        except asyncio.CancelledError:
            self._quarantine_native_call(local_service_id, task)
            raise

    def _quarantine_native_call(
        self,
        local_service_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self._native_quarantine[local_service_id] = task

        def release(completed: asyncio.Task[None]) -> None:
            if self._native_quarantine.get(local_service_id) is completed:
                self._native_quarantine.pop(local_service_id, None)
            if not completed.cancelled():
                with suppress(Exception):
                    completed.exception()

        task.add_done_callback(release)

    async def _observe_with_deadline(
        self,
        service: AllowlistedService,
        deadline: float,
    ):
        return await asyncio.wait_for(
            asyncio.to_thread(observe_service, service, self.backend),
            timeout=self._remaining(deadline),
        )

    async def _observe_action_with_deadline(
        self,
        service: AllowlistedService,
        deadline: float,
    ):
        return await asyncio.wait_for(
            asyncio.to_thread(observe_service_strict, service, self.backend),
            timeout=self._remaining(deadline),
        )

    async def _wait_for(
        self,
        service: AllowlistedService,
        targets: set[RuntimeState],
        deadline: float,
        *,
        failure_states: set[RuntimeState] | None = None,
    ) -> RuntimeState:
        while True:
            self._ensure_before_deadline(deadline)
            observed = await self._observe_action_with_deadline(service, deadline)
            self._ensure_before_deadline(deadline)
            if observed.installation_state is InstallationState.NOT_INSTALLED:
                raise SCMServiceNotFound(service.windows_service_name)
            if observed.runtime_state in targets:
                return observed.runtime_state
            if failure_states and observed.runtime_state in failure_states:
                raise SCMError("Windows service entered a terminal non-target state")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.sleep(min(self.poll_interval_seconds, remaining))

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _operation_target(self, operation: Operation) -> AllowlistedService | None:
        """Resolve only the exact durable target accepted with the Operation."""

        current = self.services.get(operation.local_service_id)
        if current is None:
            return None
        if current.windows_service_name.casefold() != operation.windows_service_name.casefold():
            return None
        # Never use a mutable/reloaded mapping as the actual SCM target.  A
        # casing-only config change is safe on Windows, while the persisted
        # spelling keeps the Operation audit record and side effect identical.
        return current.model_copy(
            update={"windows_service_name": operation.windows_service_name}
        )
