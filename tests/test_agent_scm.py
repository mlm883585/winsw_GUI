from __future__ import annotations

import pytest

from orchestrator.agent.config import AllowlistedService
from orchestrator.agent.scm import (
    SERVICE_AUTO_START,
    SERVICE_CONTINUE_PENDING,
    SERVICE_DEMAND_START,
    SERVICE_DISABLED,
    SERVICE_PAUSED,
    SERVICE_PAUSE_PENDING,
    SERVICE_RUNNING,
    SERVICE_START_PENDING,
    SERVICE_STOPPED,
    SERVICE_STOP_PENDING,
    SCMServiceNotFound,
    SCMServiceStatus,
    observe_service,
    runtime_state,
    startup_state,
)
from orchestrator.common.enums import InstallationState, RuntimeState, StartupState


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (SCMServiceStatus(SERVICE_RUNNING), RuntimeState.ACTIVE),
        (SCMServiceStatus(SERVICE_STOPPED), RuntimeState.INACTIVE),
        (SCMServiceStatus(SERVICE_STOPPED, win32_exit_code=1), RuntimeState.FAILED),
        (SCMServiceStatus(SERVICE_STOPPED, service_exit_code=2), RuntimeState.FAILED),
        (SCMServiceStatus(SERVICE_START_PENDING), RuntimeState.STARTING),
        (SCMServiceStatus(SERVICE_STOP_PENDING), RuntimeState.STOPPING),
        (SCMServiceStatus(SERVICE_PAUSED), RuntimeState.UNKNOWN),
        (SCMServiceStatus(SERVICE_PAUSE_PENDING), RuntimeState.UNKNOWN),
        (SCMServiceStatus(SERVICE_CONTINUE_PENDING), RuntimeState.UNKNOWN),
        (SCMServiceStatus(999), RuntimeState.UNKNOWN),
    ],
)
def test_runtime_state_mapping(status: SCMServiceStatus, expected: RuntimeState) -> None:
    assert runtime_state(status) is expected


@pytest.mark.parametrize(
    ("start_type", "expected"),
    [
        (SERVICE_AUTO_START, StartupState.AUTOSTART_ENABLED),
        (SERVICE_DEMAND_START, StartupState.AUTOSTART_DISABLED),
        (SERVICE_DISABLED, StartupState.START_BLOCKED),
        (None, StartupState.UNKNOWN),
        (999, StartupState.UNKNOWN),
    ],
)
def test_startup_state_mapping(start_type: int | None, expected: StartupState) -> None:
    assert startup_state(SCMServiceStatus(SERVICE_RUNNING, start_type=start_type)) is expected


def test_missing_service_is_not_confused_with_unknown_installation() -> None:
    class MissingBackend:
        def query(self, _name: str):
            raise SCMServiceNotFound("missing")

    observed = observe_service(
        AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80"),
        MissingBackend(),
    )
    assert observed.installation_state is InstallationState.NOT_INSTALLED
    assert observed.runtime_state is RuntimeState.UNKNOWN
