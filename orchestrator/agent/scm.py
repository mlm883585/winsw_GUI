from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from orchestrator.agent.config import AllowlistedService
from orchestrator.common.enums import InstallationState, RuntimeState, StartupState
from orchestrator.common.models import ObservedService
from orchestrator.common.time import utc_now


SERVICE_STOPPED = 1
SERVICE_START_PENDING = 2
SERVICE_STOP_PENDING = 3
SERVICE_RUNNING = 4
SERVICE_CONTINUE_PENDING = 5
SERVICE_PAUSE_PENDING = 6
SERVICE_PAUSED = 7

SERVICE_AUTO_START = 2
SERVICE_DEMAND_START = 3
SERVICE_DISABLED = 4


class SCMError(RuntimeError):
    pass


class SCMServiceNotFound(SCMError):
    pass


class SCMAccessDenied(SCMError):
    pass


@dataclass(frozen=True, slots=True)
class SCMServiceStatus:
    current_state: int
    win32_exit_code: int = 0
    service_exit_code: int = 0
    start_type: int | None = None
    delayed_auto_start: bool = False
    display_name: str | None = None


class SCMBackend(Protocol):
    def query(self, windows_service_name: str) -> SCMServiceStatus: ...

    def start(self, windows_service_name: str) -> None: ...

    def stop(self, windows_service_name: str) -> None: ...


def runtime_state(status: SCMServiceStatus) -> RuntimeState:
    if status.current_state == SERVICE_RUNNING:
        return RuntimeState.ACTIVE
    if status.current_state == SERVICE_STOPPED:
        if status.win32_exit_code != 0 or status.service_exit_code != 0:
            return RuntimeState.FAILED
        return RuntimeState.INACTIVE
    if status.current_state == SERVICE_START_PENDING:
        return RuntimeState.STARTING
    if status.current_state == SERVICE_STOP_PENDING:
        return RuntimeState.STOPPING
    return RuntimeState.UNKNOWN


def startup_state(status: SCMServiceStatus) -> StartupState:
    if status.start_type == SERVICE_AUTO_START:
        return StartupState.AUTOSTART_ENABLED
    if status.start_type == SERVICE_DEMAND_START:
        return StartupState.AUTOSTART_DISABLED
    if status.start_type == SERVICE_DISABLED:
        return StartupState.START_BLOCKED
    return StartupState.UNKNOWN


def _observed_from_status(
    service: AllowlistedService,
    status: SCMServiceStatus,
    observed_at: datetime,
) -> ObservedService:
    return ObservedService(
        local_service_id=service.local_service_id,
        windows_service_name=service.windows_service_name,
        display_name=(
            service.display_name or status.display_name or service.windows_service_name
        ),
        installation_state=InstallationState.INSTALLED,
        runtime_state=runtime_state(status),
        startup_state=startup_state(status),
        last_observed_at=observed_at,
    )


def observe_service_strict(
    service: AllowlistedService,
    backend: SCMBackend,
) -> ObservedService:
    """Observe for a durable action, preserving deterministic SCM failures."""

    return _observed_from_status(
        service,
        backend.query(service.windows_service_name),
        utc_now(),
    )


def observe_service(service: AllowlistedService, backend: SCMBackend) -> ObservedService:
    observed_at = utc_now()
    display_name = service.display_name or service.windows_service_name
    try:
        status = backend.query(service.windows_service_name)
    except SCMServiceNotFound:
        return ObservedService(
            local_service_id=service.local_service_id,
            windows_service_name=service.windows_service_name,
            display_name=display_name,
            installation_state=InstallationState.NOT_INSTALLED,
            runtime_state=RuntimeState.UNKNOWN,
            startup_state=StartupState.UNKNOWN,
            last_observed_at=observed_at,
        )
    except (SCMAccessDenied, SCMError, OSError):
        return ObservedService(
            local_service_id=service.local_service_id,
            windows_service_name=service.windows_service_name,
            display_name=display_name,
            installation_state=InstallationState.UNKNOWN,
            runtime_state=RuntimeState.UNKNOWN,
            startup_state=StartupState.UNKNOWN,
            last_observed_at=observed_at,
        )
    return _observed_from_status(service, status, observed_at)


class PyWin32SCMBackend:
    """Narrow pywin32 adapter; no arbitrary service enumeration or command execution."""

    def __init__(self) -> None:
        try:
            import pywintypes  # type: ignore[import-not-found]
            import win32service  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("pywin32 is required to access Windows Service Control Manager") from exc
        self._pywintypes = pywintypes
        self._service = win32service

    def _translate(self, exc: Exception, service_name: str) -> SCMError:
        winerror = getattr(exc, "winerror", None)
        if winerror == 1060:
            return SCMServiceNotFound(f"Windows service is not installed: {service_name}")
        if winerror == 5:
            return SCMAccessDenied(f"access denied while opening Windows service: {service_name}")
        return SCMError(f"Windows SCM request failed for allowlisted service: {service_name}")

    def _open(self, service_name: str, access: int):
        scm = None
        try:
            scm = self._service.OpenSCManager(
                None,
                None,
                self._service.SC_MANAGER_CONNECT,
            )
            handle = self._service.OpenService(scm, service_name, access)
            return scm, handle
        except self._pywintypes.error as exc:
            if scm is not None:
                self._service.CloseServiceHandle(scm)
            raise self._translate(exc, service_name) from exc

    def query(self, windows_service_name: str) -> SCMServiceStatus:
        scm = handle = None
        try:
            scm, handle = self._open(
                windows_service_name,
                self._service.SERVICE_QUERY_STATUS | self._service.SERVICE_QUERY_CONFIG,
            )
            state = self._service.QueryServiceStatusEx(handle)
            config = self._service.QueryServiceConfig(handle)
            delayed = False
            try:
                delayed_value = self._service.QueryServiceConfig2(
                    handle,
                    self._service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
                )
                delayed = bool(
                    delayed_value.get("DelayedAutostart", False)
                    if isinstance(delayed_value, dict)
                    else delayed_value
                )
            except self._pywintypes.error:
                delayed = False
            return SCMServiceStatus(
                current_state=int(state["CurrentState"]),
                win32_exit_code=int(state.get("Win32ExitCode", 0)),
                service_exit_code=int(state.get("ServiceSpecificExitCode", 0)),
                start_type=int(config[1]),
                delayed_auto_start=delayed,
                display_name=str(config[8]) if config[8] else None,
            )
        except SCMError:
            raise
        except self._pywintypes.error as exc:
            raise self._translate(exc, windows_service_name) from exc
        finally:
            if handle is not None:
                self._service.CloseServiceHandle(handle)
            if scm is not None:
                self._service.CloseServiceHandle(scm)

    def start(self, windows_service_name: str) -> None:
        scm = handle = None
        try:
            scm, handle = self._open(windows_service_name, self._service.SERVICE_START)
            self._service.StartService(handle, None)
        except SCMError:
            raise
        except self._pywintypes.error as exc:
            raise self._translate(exc, windows_service_name) from exc
        finally:
            if handle is not None:
                self._service.CloseServiceHandle(handle)
            if scm is not None:
                self._service.CloseServiceHandle(scm)

    def stop(self, windows_service_name: str) -> None:
        scm = handle = None
        try:
            scm, handle = self._open(windows_service_name, self._service.SERVICE_STOP)
            self._service.ControlService(handle, self._service.SERVICE_CONTROL_STOP)
        except SCMError:
            raise
        except self._pywintypes.error as exc:
            raise self._translate(exc, windows_service_name) from exc
        finally:
            if handle is not None:
                self._service.CloseServiceHandle(handle)
            if scm is not None:
                self._service.CloseServiceHandle(scm)
