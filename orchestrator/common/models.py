from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    UUID4,
    field_validator,
    model_validator,
)

from .enums import (
    AgentOnlineStatus,
    GroupState,
    InstallationState,
    OperationStatus,
    RecoveryRunStatus,
    RecoveryStepStatus,
    RunTrigger,
    RuntimeState,
    ServiceAction,
    StartupState,
)
from .errors import ErrorCode


SERVICE_SLUG_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_SERVICE_ITEMS = 1024
MAX_DEPENDENCY_ITEMS = 16384
MAX_REASON_ITEMS = 100


def _parse_canonical_uuid4_input(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("UUIDv4 input must be a canonical string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("UUIDv4 input must be a canonical string") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise ValueError("UUIDv4 input must use canonical hyphenated form")
    return parsed


CanonicalUUID4Input = Annotated[UUID, BeforeValidator(_parse_canonical_uuid4_input)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EmptyActionRequest(StrictModel):
    """An omitted action body or a strict empty JSON object."""


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    component: str
    version: str


class ObservedService(StrictModel):
    local_service_id: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    windows_service_name: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    installation_state: InstallationState
    runtime_state: RuntimeState
    startup_state: StartupState
    last_observed_at: datetime


class ObservedServiceCollection(StrictModel):
    items: list[ObservedService] = Field(max_length=MAX_SERVICE_ITEMS)


class AgentCapabilities(StrictModel):
    service_actions: list[ServiceAction] = Field(min_length=3, max_length=3)
    probe_kinds: list[Literal["scm", "tcp", "http"]] = Field(
        min_length=3,
        max_length=3,
    )
    arbitrary_command: Literal[False] = False

    @field_validator("service_actions", "probe_kinds")
    @classmethod
    def capabilities_are_unique(cls, value: list[object]) -> list[object]:
        if len(value) != len(set(value)):
            raise ValueError("capability values must be unique")
        return value


class AgentInfo(StrictModel):
    agent_id: UUID4
    boot_id: UUID4
    agent_instance_id: UUID4
    instance_generation: int = Field(ge=1)
    hostname: str
    endpoint: str
    version: str
    protocol_version: Literal["recovery-mvp-v1"] = "recovery-mvp-v1"
    security_mode: Literal["LAB_HTTP"] = "LAB_HTTP"
    production_ready: Literal[False] = False
    capabilities: AgentCapabilities


class AgentReport(StrictModel):
    agent_id: UUID4
    boot_id: UUID4
    agent_instance_id: UUID4
    instance_generation: int = Field(ge=1)
    sequence: int = Field(ge=1)
    version: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=256)
    hostname: str = Field(min_length=1, max_length=255)
    services: list[ObservedService] = Field(min_length=1, max_length=MAX_SERVICE_ITEMS)


class HeartbeatAck(StrictModel):
    accepted: bool
    ignored: bool
    reason: str
    received_at: datetime
    lease_updated_at: datetime | None


class AgentSummary(StrictModel):
    agent_id: UUID4
    boot_id: UUID4
    agent_instance_id: UUID4
    instance_generation: int
    hostname: str
    endpoint: str
    version: str
    online_status: AgentOnlineStatus
    last_sequence: int
    last_received_at: datetime
    offline_after_seconds: Literal[45]


class AgentCollection(StrictModel):
    items: list[AgentSummary] = Field(max_length=MAX_SERVICE_ITEMS)


class Operation(StrictModel):
    operation_id: UUID4
    agent_id: UUID4
    local_service_id: str = Field(pattern=SERVICE_SLUG_PATTERN)
    windows_service_name: str
    action: ServiceAction
    status: OperationStatus
    idempotency_key: UUID4
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: ErrorCode | None
    message: str | None


class ManagedService(ObservedService):
    managed_service_id: UUID4
    agent_id: UUID4
    agent_online_status: AgentOnlineStatus
    mirrored_at: datetime


class ManagedServiceCollection(StrictModel):
    items: list[ManagedService] = Field(max_length=MAX_SERVICE_ITEMS)


class ProxiedOperation(Operation):
    managed_service_id: UUID4


class ScmProbeRequest(StrictModel):
    kind: Literal["scm"]
    local_service_id: str = Field(pattern=SERVICE_SLUG_PATTERN)
    timeout_seconds: float = Field(default=2.0, strict=True, ge=0.1, le=10)


class TcpProbeRequest(StrictModel):
    kind: Literal["tcp"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(strict=True, ge=1, le=65535)
    timeout_seconds: float = Field(default=2.0, strict=True, ge=0.1, le=10)


class HttpProbeRequest(StrictModel):
    kind: Literal["http"]
    url: str = Field(min_length=1, max_length=2048)
    expected_status: int = Field(strict=True, ge=100, le=599)
    body_contains: str | None = Field(default=None, min_length=1, max_length=256)
    timeout_seconds: float = Field(default=2.0, strict=True, ge=0.1, le=10)


ProbeRequest = Annotated[
    ScmProbeRequest | TcpProbeRequest | HttpProbeRequest,
    Field(discriminator="kind"),
]


class ProbeResult(StrictModel):
    passed: bool
    observed_at: datetime
    latency_ms: int = Field(ge=0)
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=512)


class RecoveryGroupCreate(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    node_settle_window_seconds: int = Field(default=120, strict=True, ge=1, le=3600)
    max_parallel_services: int = Field(default=4, strict=True, ge=1, le=4)


class RecoveryGroupPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    node_settle_window_seconds: int | None = Field(
        default=None, strict=True, ge=1, le=3600
    )
    max_parallel_services: int | None = Field(default=None, strict=True, ge=1, le=4)

    @field_validator(
        "name",
        "description",
        "node_settle_window_seconds",
        "max_parallel_services",
        mode="before",
    )
    @classmethod
    def explicit_null_is_not_a_patch_value(cls, value: object) -> object:
        if value is None:
            raise ValueError("patch fields cannot be null; omit the field instead")
        return value

    @model_validator(mode="after")
    def not_empty(self) -> "RecoveryGroupPatch":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class RecoveryMembersWrite(StrictModel):
    managed_service_ids: list[UUID4] = Field(
        min_length=1,
        max_length=MAX_SERVICE_ITEMS,
    )

    @field_validator("managed_service_ids")
    @classmethod
    def unique_services(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("managed_service_ids must be unique")
        return value


class RecoveryDependency(StrictModel):
    managed_service_id: UUID4
    prerequisite_managed_service_id: UUID4

    @model_validator(mode="after")
    def reject_self_reference(self) -> "RecoveryDependency":
        if self.managed_service_id == self.prerequisite_managed_service_id:
            raise ValueError("a service cannot depend on itself")
        return self


class RecoveryDependenciesWrite(StrictModel):
    dependencies: list[RecoveryDependency] = Field(max_length=MAX_DEPENDENCY_ITEMS)

    @field_validator("dependencies")
    @classmethod
    def unique_edges(cls, value: list[RecoveryDependency]) -> list[RecoveryDependency]:
        edges = {(item.managed_service_id, item.prerequisite_managed_service_id) for item in value}
        if len(value) != len(edges):
            raise ValueError("dependencies must be unique")
        return value


class ScmReadinessWrite(StrictModel):
    kind: Literal["scm"]
    timeout_seconds: float = Field(default=2, strict=True, ge=0.1, le=10)
    interval_seconds: int = Field(default=3, strict=True, ge=1, le=30)
    deadline_seconds: int = Field(default=60, strict=True, ge=1, le=300)

    @model_validator(mode="after")
    def deadline_not_shorter(self) -> "ScmReadinessWrite":
        if self.deadline_seconds < self.timeout_seconds:
            raise ValueError("deadline_seconds must be >= timeout_seconds")
        return self


class TcpReadinessWrite(StrictModel):
    kind: Literal["tcp"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(strict=True, ge=1, le=65535)
    timeout_seconds: float = Field(default=2, strict=True, ge=0.1, le=10)
    interval_seconds: int = Field(default=3, strict=True, ge=1, le=30)
    deadline_seconds: int = Field(default=60, strict=True, ge=1, le=300)

    @model_validator(mode="after")
    def deadline_not_shorter(self) -> "TcpReadinessWrite":
        if self.deadline_seconds < self.timeout_seconds:
            raise ValueError("deadline_seconds must be >= timeout_seconds")
        return self


class HttpReadinessWrite(StrictModel):
    kind: Literal["http"]
    url: str = Field(min_length=1, max_length=2048)
    expected_status: int = Field(strict=True, ge=100, le=599)
    body_contains: str | None = Field(default=None, min_length=1, max_length=256)
    timeout_seconds: float = Field(default=2, strict=True, ge=0.1, le=10)
    interval_seconds: int = Field(default=3, strict=True, ge=1, le=30)
    deadline_seconds: int = Field(default=60, strict=True, ge=1, le=300)

    @model_validator(mode="after")
    def deadline_not_shorter(self) -> "HttpReadinessWrite":
        if self.deadline_seconds < self.timeout_seconds:
            raise ValueError("deadline_seconds must be >= timeout_seconds")
        return self


ReadinessWrite = Annotated[
    ScmReadinessWrite | TcpReadinessWrite | HttpReadinessWrite,
    Field(discriminator="kind"),
]


class RecoveryMember(StrictModel):
    managed_service_id: UUID4
    agent_id: UUID4
    local_service_id: str = Field(
        pattern=SERVICE_SLUG_PATTERN,
        min_length=1,
        max_length=64,
    )
    windows_service_name: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)


class ReadinessProbe(StrictModel):
    probe_id: UUID4
    group_id: UUID4
    managed_service_id: UUID4
    definition: ReadinessWrite
    created_at: datetime
    updated_at: datetime


class PreconditionIssue(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=512)
    managed_service_id: UUID4 | None = None
    agent_id: UUID4 | None = None
    managed_service_ids: list[UUID4] = Field(
        default_factory=list,
        max_length=MAX_REASON_ITEMS,
    )

    @field_validator("code", "message")
    @classmethod
    def text_is_safe_unicode_scalar(cls, value: str) -> str:
        if "\x00" in value or any(
            0xD800 <= ord(character) <= 0xDFFF for character in value
        ):
            raise ValueError("must contain Unicode scalar values and must not contain U+0000")
        return value

    @field_validator("managed_service_ids")
    @classmethod
    def managed_services_are_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("managed_service_ids must be unique")
        return value


class RecoveryGroup(StrictModel):
    group_id: UUID4
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(max_length=1024)
    state: GroupState
    node_settle_window_seconds: int = Field(ge=1, le=3600)
    max_parallel_services: int = Field(ge=1, le=4)
    baseline_epoch: str | None = Field(pattern=SHA256_PATTERN)
    candidate_epoch: str | None = Field(pattern=SHA256_PATTERN)
    candidate_stable_since: datetime | None
    settle_deadline_at: datetime | None
    last_scheduled_epoch: str | None = Field(pattern=SHA256_PATTERN)
    missing_agent_ids: list[UUID4] = Field(max_length=MAX_SERVICE_ITEMS)
    blocked_reasons: list[PreconditionIssue] = Field(
        default_factory=list,
        max_length=MAX_REASON_ITEMS,
    )
    members: list[RecoveryMember] = Field(max_length=MAX_SERVICE_ITEMS)
    dependencies: list[RecoveryDependency] = Field(max_length=MAX_DEPENDENCY_ITEMS)
    probes: list[ReadinessProbe] = Field(max_length=MAX_SERVICE_ITEMS)
    created_at: datetime
    updated_at: datetime

    @field_validator("missing_agent_ids")
    @classmethod
    def missing_agents_are_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("missing_agent_ids must be unique")
        return value

    @model_validator(mode="after")
    def blocked_reasons_match_state(self) -> "RecoveryGroup":
        if self.state is GroupState.BLOCKED_PRECONDITION:
            if not self.blocked_reasons:
                raise ValueError("BLOCKED_PRECONDITION requires at least one blocked reason")
        elif self.blocked_reasons:
            raise ValueError("blocked_reasons must be empty unless the group is blocked")
        return self


class RecoveryGroupCollection(StrictModel):
    items: list[RecoveryGroup] = Field(max_length=MAX_SERVICE_ITEMS)


class ManualRunRequest(StrictModel):
    reason: str | None = Field(default=None, min_length=1, max_length=512)


class ProbeAttempt(StrictModel):
    attempt: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime
    result: ProbeResult


class RecoveryStep(StrictModel):
    step_id: UUID4
    managed_service_id: UUID4
    agent_id: UUID4
    local_service_id: str
    status: RecoveryStepStatus
    topology_level: int = Field(ge=0)
    dispatch_idempotency_key: UUID4 | None
    operation_id: UUID4 | None
    probe_attempts: list[ProbeAttempt] = Field(max_length=MAX_SERVICE_ITEMS)
    warnings: list[str] = Field(max_length=MAX_REASON_ITEMS)
    root_cause_step_id: UUID4 | None
    dependency_chain: list[UUID4] = Field(max_length=MAX_SERVICE_ITEMS)
    message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime

    @field_validator("warnings")
    @classmethod
    def warnings_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("warnings must be unique")
        return value


class RecoveryRun(StrictModel):
    run_id: UUID4
    group_id: UUID4
    trigger: RunTrigger
    epoch: str | None = Field(pattern=SHA256_PATTERN)
    retry_of_run_id: UUID4 | None
    status: RecoveryRunStatus
    reason: str | None = Field(min_length=1, max_length=512)
    members_snapshot: list[RecoveryMember] = Field(max_length=MAX_SERVICE_ITEMS)
    dependencies_snapshot: list[RecoveryDependency] = Field(
        max_length=MAX_DEPENDENCY_ITEMS
    )
    probes_snapshot: list[ReadinessProbe] = Field(max_length=MAX_SERVICE_ITEMS)
    steps: list[RecoveryStep] = Field(max_length=MAX_SERVICE_ITEMS)
    failure_code: ErrorCode | None
    failure_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class RecoveryRunCollection(StrictModel):
    items: list[RecoveryRun] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=2048)
