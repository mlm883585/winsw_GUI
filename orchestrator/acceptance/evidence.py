from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, UUID4, ValidationError, field_validator, model_validator

from orchestrator.common.enums import (
    OperationStatus,
    RecoveryRunStatus,
    RecoveryStepStatus,
    RunTrigger,
    ServiceAction,
)
from orchestrator.common.errors import ErrorCode, ErrorResponse
from orchestrator.common.graph import DependencyCycleError, reachable_descendants, topological_levels
from orchestrator.common.models import (
    ManagedServiceCollection,
    Operation,
    RecoveryRun,
    SHA256_PATTERN,
    StrictModel,
)


class EvidenceInputError(ValueError):
    """The evidence file is not valid JSON or does not match the evidence model."""


class ScenarioKind(str, Enum):
    CONTROL_PLANE_LAST = "CONTROL_PLANE_LAST"
    AGENT_PROCESS_RESTART = "AGENT_PROCESS_RESTART"
    SINGLE_NODE_REBOOT = "SINGLE_NODE_REBOOT"
    MISSING_NODE = "MISSING_NODE"
    START_FAILURE = "START_FAILURE"
    PROBE_FAILURE = "PROBE_FAILURE"
    AGENT_DISCONNECT = "AGENT_DISCONNECT"
    CONTROL_PLANE_RESTART = "CONTROL_PLANE_RESTART"
    UNKNOWN_SERVICE_REJECTION = "UNKNOWN_SERVICE_REJECTION"


REQUIRED_SCENARIOS = frozenset(ScenarioKind)


class ServiceRoles(StrictModel):
    mysql: UUID4
    redis: UUID4
    nacos: UUID4
    java: UUID4
    nginx: UUID4

    @model_validator(mode="after")
    def roles_are_unique(self) -> "ServiceRoles":
        values = list(self.model_dump().values())
        if len(values) != len(set(values)):
            raise ValueError("each acceptance role must reference a different service")
        return self


class EvidenceCampaign(StrictModel):
    campaign_id: UUID4
    name: str = Field(min_length=1, max_length=160)
    environment: str = Field(min_length=1, max_length=160)
    required_rounds: int = Field(default=10, ge=10, le=100)
    service_roles: ServiceRoles


class ExportCompletenessAttestation(StrictModel):
    operator: str = Field(min_length=1, max_length=120)
    exported_at: datetime
    all_runs_in_declared_windows: bool
    all_action_attempts_in_declared_windows: bool
    source_description: str = Field(min_length=1, max_length=1000)


class AcceptanceRound(StrictModel):
    round_number: int = Field(ge=1)
    group_id: UUID4
    epoch: str = Field(pattern=SHA256_PATTERN)
    run_id: UUID4
    window_started_at: datetime
    window_finished_at: datetime
    boot_order: list[UUID4] = Field(min_length=3)
    notes: str = Field(default="", max_length=2000)

    @field_validator("boot_order")
    @classmethod
    def boot_order_is_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("boot_order must not repeat an Agent")
        return value

    @model_validator(mode="after")
    def window_is_ordered(self) -> "AcceptanceRound":
        if _is_aware(self.window_started_at) != _is_aware(self.window_finished_at):
            raise ValueError("round window timestamps must use the same timezone awareness")
        if self.window_finished_at <= self.window_started_at:
            raise ValueError("window_finished_at must be after window_started_at")
        return self


class ManualProof(StrictModel):
    artifact_ref: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=SHA256_PATTERN)
    reviewed_by: str = Field(min_length=1, max_length=120)
    reviewed_at: datetime
    summary: str = Field(min_length=1, max_length=1000)


class AgentStartObservation(StrictModel):
    agent_id: UUID4
    started_at: datetime


class ScenarioExerciseBase(StrictModel):
    exercise_id: UUID4
    window_started_at: datetime
    window_finished_at: datetime

    @model_validator(mode="after")
    def window_is_ordered(self) -> "ScenarioExerciseBase":
        if _is_aware(self.window_started_at) != _is_aware(self.window_finished_at):
            raise ValueError("scenario window timestamps must use the same timezone awareness")
        if self.window_finished_at <= self.window_started_at:
            raise ValueError("window_finished_at must be after window_started_at")
        return self


class ControlPlaneLastExercise(ScenarioExerciseBase):
    kind: Literal["CONTROL_PLANE_LAST"]
    run_id: UUID4
    agent_starts: list[AgentStartObservation] = Field(min_length=3)
    control_plane_started_at: datetime
    all_agents_registered_at: datetime
    settle_window_seconds: int = Field(ge=120, le=3600)
    proof: ManualProof | None = None


class AgentProcessRestartExercise(ScenarioExerciseBase):
    kind: Literal["AGENT_PROCESS_RESTART"]
    agent_id: UUID4
    boot_id_before: UUID4
    boot_id_after: UUID4
    agent_instance_id_before: UUID4
    agent_instance_id_after: UUID4
    epoch_before: str = Field(pattern=SHA256_PATTERN)
    epoch_after: str = Field(pattern=SHA256_PATTERN)
    auto_run_ids_created: list[UUID4]
    proof: ManualProof | None = None


class SingleNodeRebootExercise(ScenarioExerciseBase):
    kind: Literal["SINGLE_NODE_REBOOT"]
    agent_id: UUID4
    boot_id_before: UUID4
    boot_id_after: UUID4
    epoch_before: str = Field(pattern=SHA256_PATTERN)
    epoch_after: str = Field(pattern=SHA256_PATTERN)
    run_id: UUID4
    already_active_service_ids: list[UUID4] = Field(min_length=1)
    proof: ManualProof | None = None


class MissingNodeExercise(ScenarioExerciseBase):
    kind: Literal["MISSING_NODE"]
    group_id: UUID4
    required_agent_ids: list[UUID4] = Field(min_length=3)
    missing_agent_ids: list[UUID4] = Field(min_length=1)
    observed_group_state: Literal["WAITING_FOR_NODES"]
    operation_ids_during_absence: list[UUID4]
    auto_run_ids_during_absence: list[UUID4]
    proof: ManualProof | None = None


class StartFailureExercise(ScenarioExerciseBase):
    kind: Literal["START_FAILURE"]
    run_id: UUID4
    failed_service_id: UUID4


class ProbeFailureExercise(ScenarioExerciseBase):
    kind: Literal["PROBE_FAILURE"]
    run_id: UUID4
    failed_service_id: UUID4


class AgentDisconnectExercise(ScenarioExerciseBase):
    kind: Literal["AGENT_DISCONNECT"]
    run_id: UUID4
    unknown_service_id: UUID4
    proof: ManualProof | None = None


class ControlPlaneRestartExercise(ScenarioExerciseBase):
    kind: Literal["CONTROL_PLANE_RESTART"]
    run_id_before: UUID4
    run_id_after: UUID4
    step_id: UUID4
    operation_id_before: UUID4
    operation_id_after: UUID4
    proof: ManualProof | None = None


class UnknownServiceRejectionExercise(ScenarioExerciseBase):
    kind: Literal["UNKNOWN_SERVICE_REJECTION"]
    request_id: UUID4


ScenarioExercise = Annotated[
    ControlPlaneLastExercise
    | AgentProcessRestartExercise
    | SingleNodeRebootExercise
    | MissingNodeExercise
    | StartFailureExercise
    | ProbeFailureExercise
    | AgentDisconnectExercise
    | ControlPlaneRestartExercise
    | UnknownServiceRejectionExercise,
    Field(discriminator="kind"),
]


class ActionOwnership(StrictModel):
    cold_round_number: int | None = Field(default=None, ge=1)
    scenario_exercise_id: UUID4 | None = None

    @model_validator(mode="after")
    def belongs_to_exactly_one_owner(self) -> "ActionOwnership":
        if (self.cold_round_number is None) == (self.scenario_exercise_id is None):
            raise ValueError(
                "exactly one of cold_round_number or scenario_exercise_id is required"
            )
        return self


class OperationActionEvidence(ActionOwnership):
    kind: Literal["operation"]
    managed_service_id: UUID4
    run_id: UUID4
    step_id: UUID4
    observed_at: datetime
    operation: Operation


class RejectedActionEvidence(ActionOwnership):
    kind: Literal["rejected_request"]
    agent_id: UUID4
    local_service_id: str = Field(min_length=1, max_length=64)
    action: ServiceAction
    http_status: int = Field(ge=400, le=599)
    observed_at: datetime
    response: ErrorResponse


ActionEvidence = Annotated[
    OperationActionEvidence | RejectedActionEvidence,
    Field(discriminator="kind"),
]


class AcceptanceEvidence(StrictModel):
    schema_version: Literal["recovery-mvp-evidence-v1"]
    campaign: EvidenceCampaign
    completeness_attestation: ExportCompletenessAttestation
    inventory: ManagedServiceCollection
    rounds: list[AcceptanceRound]
    scenario_exercises: list[ScenarioExercise]
    runs: list[RecoveryRun]
    actions: list[ActionEvidence]


class EvidenceIssue(StrictModel):
    severity: Literal["ERROR", "WARNING"]
    code: str
    message: str
    round_number: int | None = None
    scenario_exercise_id: UUID | None = None
    run_id: UUID | None = None
    managed_service_id: UUID | None = None


class EvidenceMetrics(StrictModel):
    required_rounds: int
    observed_rounds: int
    observed_runs: int
    observed_agents: int
    observed_services: int
    operation_records: int
    rejected_request_records: int
    scenario_exercises: int
    manual_proof_items: int
    derived_checks: int


class ManualProofRecord(StrictModel):
    scenario: ScenarioKind
    exercise_id: UUID4
    artifact_ref: str
    sha256: str
    reviewed_by: str
    reviewed_at: datetime
    summary: str


class EvidenceReport(StrictModel):
    report_version: Literal["recovery-mvp-evidence-report-v1"] = (
        "recovery-mvp-evidence-report-v1"
    )
    verdict: Literal["PASS", "FAIL"]
    campaign_id: UUID4
    checked_at: datetime
    metrics: EvidenceMetrics
    manual_proof_records: list[ManualProofRecord]
    issues: list[EvidenceIssue]


def load_evidence(path: str | Path) -> AcceptanceEvidence:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise EvidenceInputError(f"cannot read evidence file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceInputError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    try:
        return AcceptanceEvidence.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in exc.errors()[:10]
        )
        suffix = "" if len(exc.errors()) <= 10 else f"; and {len(exc.errors()) - 10} more"
        raise EvidenceInputError(f"evidence schema validation failed: {problems}{suffix}") from exc


class _Validator:
    def __init__(self, evidence: AcceptanceEvidence) -> None:
        self.evidence = evidence
        self.issues: list[EvidenceIssue] = []
        self.derived_checks = 0
        self.rounds = {item.round_number: item for item in evidence.rounds}
        self.exercises = {
            str(item.exercise_id): item for item in evidence.scenario_exercises
        }
        self.runs = {str(item.run_id): item for item in evidence.runs}
        self.inventory = {
            str(item.managed_service_id): item for item in evidence.inventory.items
        }
        self.action_by_operation: dict[str, list[OperationActionEvidence]] = defaultdict(list)
        for action in evidence.actions:
            if isinstance(action, OperationActionEvidence):
                self.action_by_operation[str(action.operation.operation_id)].append(action)

    def error(
        self,
        code: str,
        message: str,
        *,
        round_number: int | None = None,
        scenario_exercise_id: UUID | None = None,
        run_id: UUID | None = None,
        managed_service_id: UUID | None = None,
    ) -> None:
        self.issues.append(
            EvidenceIssue(
                severity="ERROR",
                code=code,
                message=message,
                round_number=round_number,
                scenario_exercise_id=scenario_exercise_id,
                run_id=run_id,
                managed_service_id=managed_service_id,
            )
        )

    def check(self, condition: bool, code: str, message: str, **context: Any) -> None:
        self.derived_checks += 1
        if not condition:
            self.error(code, message, **context)

    def validate(self) -> EvidenceReport:
        self._validate_attestation()
        self._validate_campaign_shape()
        self._validate_round_run_cardinality()
        self._validate_actions()
        self._validate_scenario_exercises()
        for run_id, owners in self._run_owners().items():
            run = self.runs.get(run_id)
            if run is not None and len(owners) == 1:
                self._validate_run(owners[0], run)
        errors = [item for item in self.issues if item.severity == "ERROR"]
        operations = sum(
            isinstance(item, OperationActionEvidence) for item in self.evidence.actions
        )
        rejections = sum(
            isinstance(item, RejectedActionEvidence) for item in self.evidence.actions
        )
        agents = {str(item.agent_id) for item in self.evidence.inventory.items}
        manual_proofs = _manual_proof_records(self.evidence.scenario_exercises)
        return EvidenceReport(
            verdict="FAIL" if errors else "PASS",
            campaign_id=self.evidence.campaign.campaign_id,
            checked_at=datetime.now(timezone.utc),
            metrics=EvidenceMetrics(
                required_rounds=self.evidence.campaign.required_rounds,
                observed_rounds=len(self.evidence.rounds),
                observed_runs=len(self.evidence.runs),
                observed_agents=len(agents),
                observed_services=len(self.evidence.inventory.items),
                operation_records=operations,
                rejected_request_records=rejections,
                scenario_exercises=len(self.evidence.scenario_exercises),
                manual_proof_items=len(manual_proofs),
                derived_checks=self.derived_checks,
            ),
            manual_proof_records=manual_proofs,
            issues=self.issues,
        )

    def _validate_attestation(self) -> None:
        item = self.evidence.completeness_attestation
        self.check(
            item.all_runs_in_declared_windows,
            "RUN_EXPORT_INCOMPLETE",
            "operator did not attest that all Runs in every cold-round and scenario window were exported",
        )
        self.check(
            item.all_action_attempts_in_declared_windows,
            "ACTION_EXPORT_INCOMPLETE",
            "operator did not attest that all action attempts in every cold-round and scenario window were exported",
        )
        self.check(
            _is_aware(item.exported_at),
            "NAIVE_TIMESTAMP",
            "completeness_attestation.exported_at must include a UTC offset",
        )
        latest_required_time = [
            *(round_item.window_finished_at for round_item in self.evidence.rounds),
            *(exercise.window_finished_at for exercise in self.evidence.scenario_exercises),
            *(
                proof.reviewed_at
                for exercise in self.evidence.scenario_exercises
                if (proof := getattr(exercise, "proof", None)) is not None
            ),
        ]
        self.check(
            _is_aware(item.exported_at)
            and all(_is_aware(value) for value in latest_required_time)
            and all(item.exported_at >= value for value in latest_required_time),
            "EXPORT_TIME_INVALID",
            "exported_at must not predate any declared evidence window or manual review",
        )

    def _validate_campaign_shape(self) -> None:
        campaign = self.evidence.campaign
        round_numbers = [item.round_number for item in self.evidence.rounds]
        self.check(
            len(self.evidence.rounds) >= campaign.required_rounds,
            "CAMPAIGN_MIN_ROUNDS",
            f"expected at least {campaign.required_rounds} rounds, found {len(self.evidence.rounds)}",
        )
        expected_numbers = list(range(1, len(round_numbers) + 1))
        self.check(
            sorted(round_numbers) == expected_numbers and len(set(round_numbers)) == len(round_numbers),
            "ROUND_NUMBER_SEQUENCE",
            "round_number values must be unique and contiguous starting at 1",
        )
        epochs = [item.epoch for item in self.evidence.rounds]
        self.check(
            len(epochs) == len(set(epochs)),
            "EPOCH_NOT_UNIQUE",
            "each cold-start round must record a distinct epoch",
        )
        run_ids = [str(item.run_id) for item in self.evidence.runs]
        self.check(
            len(run_ids) == len(set(run_ids)),
            "RUN_ID_NOT_UNIQUE",
            "runs contains a duplicate run_id",
        )
        run_keys = [
            (str(item.group_id), item.epoch)
            for item in self.evidence.runs
            if item.trigger == RunTrigger.AUTO
        ]
        self.check(
            len(run_keys) == len(set(run_keys)),
            "AUTO_RUN_NOT_UNIQUE",
            "more than one AUTO run exists for the same (group_id, epoch)",
        )
        agents = {str(item.agent_id) for item in self.evidence.inventory.items}
        self.check(
            len(agents) >= 3,
            "MIN_AGENT_COUNT",
            f"three-machine acceptance requires at least 3 Agents, found {len(agents)}",
        )
        inventory_ids = list(self.inventory)
        self.check(
            len(inventory_ids) == len(set(inventory_ids))
            and len(self.inventory) == len(self.evidence.inventory.items),
            "INVENTORY_ID_NOT_UNIQUE",
            "inventory contains a duplicate managed_service_id",
        )
        inventory_pairs = [
            (str(item.agent_id), item.local_service_id)
            for item in self.evidence.inventory.items
        ]
        self.check(
            len(inventory_pairs) == len(set(inventory_pairs)),
            "INVENTORY_MAPPING_NOT_UNIQUE",
            "inventory contains a duplicate (agent_id, local_service_id) mapping",
        )
        roles = {str(value) for value in campaign.service_roles.model_dump().values()}
        self.check(
            roles <= set(self.inventory),
            "ROLE_SERVICE_NOT_IN_INVENTORY",
            "one or more required service roles are absent from inventory",
        )
        scenario_kinds = {ScenarioKind(item.kind) for item in self.evidence.scenario_exercises}
        missing = sorted(tag.value for tag in REQUIRED_SCENARIOS - scenario_kinds)
        self.check(
            not missing,
            "SCENARIO_COVERAGE_INCOMPLETE",
            "required structured scenario exercises were not recorded: " + ", ".join(missing),
        )
        orders = {tuple(str(agent) for agent in item.boot_order) for item in self.evidence.rounds}
        self.check(
            len(orders) >= min(3, len(self.evidence.rounds)),
            "BOOT_ORDER_VARIATION_INSUFFICIENT",
            "cold-start campaign must contain at least three distinct recorded boot orders",
        )
        for round_item in self.evidence.rounds:
            self.check(
                _is_aware(round_item.window_started_at)
                and _is_aware(round_item.window_finished_at),
                "NAIVE_TIMESTAMP",
                "round windows must include a UTC offset",
                round_number=round_item.round_number,
            )

    def _validate_round_run_cardinality(self) -> None:
        auto_by_key: dict[tuple[str, str | None], list[RecoveryRun]] = defaultdict(list)
        for run in self.evidence.runs:
            if run.trigger == RunTrigger.AUTO:
                auto_by_key[(str(run.group_id), run.epoch)].append(run)
        for round_item in self.evidence.rounds:
            matching = auto_by_key[(str(round_item.group_id), round_item.epoch)]
            self.check(
                len(matching) == 1,
                "AUTO_RUN_CARDINALITY",
                "round must have exactly one exported AUTO run for its (group_id, epoch)",
                round_number=round_item.round_number,
                run_id=round_item.run_id,
            )
            self.check(
                len(matching) == 1 and matching[0].run_id == round_item.run_id,
                "ROUND_RUN_MISMATCH",
                "round.run_id does not identify its unique AUTO run",
                round_number=round_item.round_number,
                run_id=round_item.run_id,
            )
            run = self.runs.get(str(round_item.run_id))
            if run is not None:
                self.check(
                    _within(round_item, run.created_at)
                    and run.finished_at is not None
                    and _within(round_item, run.finished_at),
                    "RUN_OUTSIDE_ROUND_WINDOW",
                    "run timestamps are outside the declared round window",
                    round_number=round_item.round_number,
                    run_id=run.run_id,
                )
                self.check(
                    run.status == RecoveryRunStatus.SUCCEEDED
                    and all(step.status == RecoveryStepStatus.READY for step in run.steps),
                    "COLD_ROUND_NOT_SUCCESSFUL",
                    "each of the ten cold-start AUTO rounds must finish with every step READY",
                    round_number=round_item.round_number,
                    run_id=run.run_id,
                )
            boot_agents = {str(item) for item in round_item.boot_order}
            if run is not None:
                member_agents = {str(item.agent_id) for item in run.members_snapshot}
                self.check(
                    member_agents == boot_agents,
                    "BOOT_ORDER_AGENT_MISMATCH",
                    "boot_order must contain exactly the Agents required by the run",
                    round_number=round_item.round_number,
                    run_id=run.run_id,
                )
        run_owners = self._run_owners()
        for exercise in self.evidence.scenario_exercises:
            for run_id in _scenario_run_ids(exercise):
                self.check(
                    str(run_id) in self.runs,
                    "SCENARIO_RUN_NOT_EXPORTED",
                    "every Run referenced by a scenario exercise must be present in runs[]",
                    scenario_exercise_id=exercise.exercise_id,
                    run_id=run_id,
                )
        for run in self.evidence.runs:
            owners = run_owners.get(str(run.run_id), [])
            self.check(
                bool(owners),
                "UNASSIGNED_RUN",
                "every exported Run must be assigned to a cold-start round or scenario exercise",
                run_id=run.run_id,
            )
            self.check(
                len(owners) <= 1,
                "RUN_OWNERSHIP_AMBIGUOUS",
                "a Run must not be assigned to both a cold-start round and a scenario, or to multiple scenarios",
                run_id=run.run_id,
            )

    def _run_owners(self) -> dict[str, list[AcceptanceRound | ScenarioExercise]]:
        owners: dict[str, list[AcceptanceRound | ScenarioExercise]] = defaultdict(list)
        for round_item in self.evidence.rounds:
            owners[str(round_item.run_id)].append(round_item)
        for exercise in self.evidence.scenario_exercises:
            for run_id in _scenario_run_ids(exercise):
                owners[str(run_id)].append(exercise)
        return owners

    def _validate_actions(self) -> None:
        operation_ids: list[str] = []
        unknown_rejection_seen = False
        inventory_pairs = {
            (str(item.agent_id), item.local_service_id): str(item.managed_service_id)
            for item in self.evidence.inventory.items
        }
        for action in self.evidence.actions:
            owner = self._action_owner(action)
            context = _owner_context_from_action(action)
            self.check(
                owner is not None,
                "ACTION_OWNER_NOT_FOUND",
                "action evidence references an owner that is not in the bundle",
                **context,
            )
            if isinstance(action, OperationActionEvidence):
                operation = action.operation
                operation_ids.append(str(operation.operation_id))
                run = self.runs.get(str(action.run_id))
                linked_step = None
                if run is not None:
                    linked_step = next(
                        (item for item in run.steps if item.step_id == action.step_id), None
                    )
                self.check(
                    run is not None
                    and linked_step is not None
                    and linked_step.operation_id == operation.operation_id,
                    "UNLINKED_OPERATION",
                    "accepted Operation is not the durable operation_id of its referenced step",
                    **context,
                    run_id=action.run_id,
                    managed_service_id=action.managed_service_id,
                )
                self.check(
                    owner is not None
                    and str(action.run_id) in {
                        str(item) for item in _owner_run_ids(owner)
                    },
                    "ACTION_RUN_OWNERSHIP_MISMATCH",
                    "Operation action must reference a Run assigned to the same cold round or scenario",
                    **context,
                    run_id=action.run_id,
                    managed_service_id=action.managed_service_id,
                )
                mapping = inventory_pairs.get(
                    (str(operation.agent_id), operation.local_service_id)
                )
                self.check(
                    mapping == str(action.managed_service_id),
                    "OPERATION_TARGET_UNKNOWN",
                    "an accepted Operation does not map to the exported allowlist inventory",
                    **context,
                    run_id=action.run_id,
                    managed_service_id=action.managed_service_id,
                )
                self.check(
                    operation.status.terminal,
                    "OPERATION_NOT_TERMINAL",
                    "acceptance evidence must contain the terminal Operation result",
                    **context,
                    run_id=action.run_id,
                    managed_service_id=action.managed_service_id,
                )
                if owner is not None:
                    self.check(
                        _within(owner, action.observed_at),
                        "ACTION_OUTSIDE_OWNER_WINDOW",
                        "Operation collection timestamp is outside its cold-round or scenario window",
                        **context,
                        run_id=action.run_id,
                        managed_service_id=action.managed_service_id,
                    )
                self.check(
                    _operation_times_are_valid(operation),
                    "OPERATION_TIME_INVALID",
                    "remote Agent Operation timestamps must be offset-aware, terminal-finished and monotonic in the Agent clock domain",
                    **context,
                    run_id=action.run_id,
                    managed_service_id=action.managed_service_id,
                )
            else:
                mapping = inventory_pairs.get((str(action.agent_id), action.local_service_id))
                if mapping is None:
                    unknown_rejection_seen = True
                    self.check(
                        action.http_status == 404
                        and action.response.code == ErrorCode.SERVICE_NOT_ALLOWLISTED,
                        "UNKNOWN_SERVICE_NOT_REJECTED",
                        "an unknown service request must be rejected with 404 SERVICE_NOT_ALLOWLISTED",
                        **context,
                    )
                if owner is not None:
                    self.check(
                        _within(owner, action.observed_at),
                        "ACTION_OUTSIDE_OWNER_WINDOW",
                        "rejected request timestamp is outside its cold-round or scenario window",
                        **context,
                    )
        self.check(
            len(operation_ids) == len(set(operation_ids)),
            "OPERATION_ID_NOT_UNIQUE",
            "actions contains a duplicate Operation record; idempotent responses must be deduplicated",
        )
        self.check(
            unknown_rejection_seen,
            "UNKNOWN_SERVICE_NEGATIVE_TEST_MISSING",
            "campaign does not include a rejected unknown-service request",
        )

    def _action_owner(
        self, action: ActionEvidence
    ) -> AcceptanceRound | ScenarioExercise | None:
        if action.cold_round_number is not None:
            return self.rounds.get(action.cold_round_number)
        if action.scenario_exercise_id is not None:
            return self.exercises.get(str(action.scenario_exercise_id))
        return None

    def _validate_scenario_exercises(self) -> None:
        exercise_ids = [str(item.exercise_id) for item in self.evidence.scenario_exercises]
        self.check(
            len(exercise_ids) == len(set(exercise_ids)),
            "SCENARIO_EXERCISE_ID_NOT_UNIQUE",
            "scenario_exercises contains a duplicate exercise_id",
        )
        counts = Counter(item.kind for item in self.evidence.scenario_exercises)
        for kind in ScenarioKind:
            self.check(
                counts[kind.value] >= 1,
                "SCENARIO_EXERCISE_MISSING",
                f"scenario {kind.value} requires a separate structured exercise",
            )
        for exercise in self.evidence.scenario_exercises:
            context = {"scenario_exercise_id": exercise.exercise_id}
            self.check(
                _is_aware(exercise.window_started_at)
                and _is_aware(exercise.window_finished_at),
                "NAIVE_TIMESTAMP",
                f"scenario {exercise.kind} window must include a UTC offset",
                **context,
            )
            proof = getattr(exercise, "proof", None)
            if isinstance(
                exercise,
                (
                    ControlPlaneLastExercise,
                    AgentProcessRestartExercise,
                    SingleNodeRebootExercise,
                    MissingNodeExercise,
                    AgentDisconnectExercise,
                    ControlPlaneRestartExercise,
                ),
            ):
                self.check(
                    proof is not None,
                    "MANUAL_PROOF_REQUIRED",
                    f"scenario {exercise.kind} cannot be derived from Run JSON alone; attach reviewed proof metadata",
                    **context,
                )
                if proof is not None:
                    self.check(
                        _is_aware(proof.reviewed_at),
                        "NAIVE_TIMESTAMP",
                        "manual proof reviewed_at must include a UTC offset",
                        **context,
                    )
                    self.check(
                        _is_aware(proof.reviewed_at)
                        and _is_aware(exercise.window_finished_at)
                        and proof.reviewed_at >= exercise.window_finished_at,
                        "MANUAL_PROOF_TIME_INVALID",
                        "manual proof review must not predate the scenario window completion",
                        **context,
                    )
            if isinstance(exercise, ControlPlaneLastExercise):
                self._validate_control_plane_last(exercise)
            elif isinstance(exercise, AgentProcessRestartExercise):
                known_agents = {item.agent_id for item in self.evidence.inventory.items}
                self.check(
                    exercise.agent_id in known_agents
                    and exercise.boot_id_before == exercise.boot_id_after
                    and exercise.agent_instance_id_before != exercise.agent_instance_id_after
                    and exercise.epoch_before == exercise.epoch_after
                    and not exercise.auto_run_ids_created,
                    "AGENT_RESTART_FENCING_NOT_PROVEN",
                    "Agent process restart must keep boot/epoch, change instance, and create zero AUTO Runs",
                    **context,
                )
            elif isinstance(exercise, SingleNodeRebootExercise):
                self._validate_single_node_reboot(exercise)
            elif isinstance(exercise, MissingNodeExercise):
                known_agents = {item.agent_id for item in self.evidence.inventory.items}
                required_agents = set(exercise.required_agent_ids)
                self.check(
                    not exercise.operation_ids_during_absence
                    and not exercise.auto_run_ids_during_absence,
                    "MISSING_NODE_PARTIAL_EXECUTION",
                    "WAITING_FOR_NODES interval must contain zero Operations and zero AUTO Runs",
                    **context,
                )
                self.check(
                    len(required_agents) == len(exercise.required_agent_ids),
                    "MISSING_NODE_REQUIRED_AGENT_DUPLICATE",
                    "required_agent_ids must not contain duplicates",
                    **context,
                )
                self.check(
                    required_agents <= known_agents
                    and set(exercise.missing_agent_ids) <= required_agents,
                    "MISSING_NODE_TARGET_INVALID",
                    "required/missing Agent IDs must identify known required Agents in the recovery group",
                    **context,
                )
            elif isinstance(exercise, StartFailureExercise):
                self._validate_start_failure(exercise)
            elif isinstance(exercise, ProbeFailureExercise):
                self._validate_probe_failure(exercise)
            elif isinstance(exercise, AgentDisconnectExercise):
                self._validate_agent_disconnect(exercise)
            elif isinstance(exercise, ControlPlaneRestartExercise):
                self._validate_control_plane_restart(exercise)
            elif isinstance(exercise, UnknownServiceRejectionExercise):
                self._validate_unknown_service_rejection(exercise)

    def _validate_control_plane_last(self, exercise: ControlPlaneLastExercise) -> None:
        run = self.runs.get(str(exercise.run_id))
        context = {
            "scenario_exercise_id": exercise.exercise_id,
            "run_id": exercise.run_id,
        }
        starts = {str(item.agent_id): item.started_at for item in exercise.agent_starts}
        required_agents = (
            {str(item.agent_id) for item in run.members_snapshot} if run is not None else set()
        )
        timestamps = [
            exercise.control_plane_started_at,
            exercise.all_agents_registered_at,
            *(item.started_at for item in exercise.agent_starts),
        ]
        self.check(
            run is not None
            and run.trigger == RunTrigger.AUTO
            and run.status == RecoveryRunStatus.SUCCEEDED
            and all(step.status == RecoveryStepStatus.READY for step in run.steps)
            and len(starts) == len(exercise.agent_starts)
            and required_agents <= set(starts)
            and _is_aware(run.created_at)
            and all(_is_aware(item) for item in timestamps)
            and all(_within(exercise, item) for item in timestamps)
            and all(item < exercise.control_plane_started_at for item in starts.values())
            and exercise.all_agents_registered_at >= exercise.control_plane_started_at
            and run.created_at
            >= exercise.all_agents_registered_at
            + timedelta(seconds=exercise.settle_window_seconds),
            "CONTROL_PLANE_LAST_NOT_PROVEN",
            "CP-last evidence must cover every required Agent and a full settle window before Run creation",
            **context,
        )

    def _validate_single_node_reboot(self, exercise: SingleNodeRebootExercise) -> None:
        run = self.runs.get(str(exercise.run_id))
        context = {
            "scenario_exercise_id": exercise.exercise_id,
            "run_id": exercise.run_id,
        }
        steps = (
            {str(item.managed_service_id): item for item in run.steps} if run is not None else {}
        )
        members = (
            {str(item.managed_service_id): item for item in run.members_snapshot}
            if run is not None
            else {}
        )
        active_services_valid = True
        for service_id in exercise.already_active_service_ids:
            step = steps.get(str(service_id))
            member = members.get(str(service_id))
            if (
                step is None
                or member is None
                or member.agent_id == exercise.agent_id
                or step.operation_id is not None
                or step.status != RecoveryStepStatus.READY
                or not step.probe_attempts
                or not step.probe_attempts[-1].result.passed
            ):
                active_services_valid = False
        self.check(
            run is not None
            and exercise.boot_id_before != exercise.boot_id_after
            and exercise.epoch_before != exercise.epoch_after
            and run.trigger == RunTrigger.AUTO
            and run.status == RecoveryRunStatus.SUCCEEDED
            and all(item.status == RecoveryStepStatus.READY for item in run.steps)
            and run.epoch == exercise.epoch_after
            and any(item.agent_id == exercise.agent_id for item in run.members_snapshot)
            and active_services_valid,
            "SINGLE_NODE_REBOOT_NOT_PROVEN",
            "single-node reboot must change boot/epoch and leave already-active services probe-only",
            **context,
        )

    def _validate_start_failure(self, exercise: StartFailureExercise) -> None:
        run, step = self._scenario_run_step(exercise.run_id, exercise.failed_service_id)
        context = {
            "scenario_exercise_id": exercise.exercise_id,
            "run_id": exercise.run_id,
            "managed_service_id": exercise.failed_service_id,
        }
        matches = (
            self.action_by_operation.get(str(step.operation_id), [])
            if step is not None and step.operation_id is not None
            else []
        )
        self.check(
            run is not None
            and run.trigger == RunTrigger.MANUAL
            and run.status == RecoveryRunStatus.FAILED
            and step is not None
            and step.status == RecoveryStepStatus.FAILED
            and len(matches) == 1
            and matches[0].operation.status
            in {OperationStatus.FAILED, OperationStatus.REJECTED},
            "START_FAILURE_NOT_PROVEN",
            "start-failure exercise needs a FAILED/REJECTED Operation, FAILED step and FAILED MANUAL Run",
            **context,
        )

    def _validate_probe_failure(self, exercise: ProbeFailureExercise) -> None:
        run, step = self._scenario_run_step(exercise.run_id, exercise.failed_service_id)
        context = {
            "scenario_exercise_id": exercise.exercise_id,
            "run_id": exercise.run_id,
            "managed_service_id": exercise.failed_service_id,
        }
        operation_ok = True
        if step is not None and step.operation_id is not None:
            matches = self.action_by_operation.get(str(step.operation_id), [])
            operation_ok = len(matches) == 1 and matches[0].operation.status == OperationStatus.SUCCEEDED
        self.check(
            run is not None
            and run.trigger == RunTrigger.MANUAL
            and run.status == RecoveryRunStatus.FAILED
            and step is not None
            and step.status == RecoveryStepStatus.FAILED
            and bool(step.probe_attempts)
            and all(not item.result.passed for item in step.probe_attempts)
            and operation_ok,
            "PROBE_FAILURE_NOT_PROVEN",
            "probe-failure exercise needs failed attempts after a successful/no-op start and a FAILED Run",
            **context,
        )

    def _validate_agent_disconnect(self, exercise: AgentDisconnectExercise) -> None:
        run, step = self._scenario_run_step(exercise.run_id, exercise.unknown_service_id)
        context = {
            "scenario_exercise_id": exercise.exercise_id,
            "run_id": exercise.run_id,
            "managed_service_id": exercise.unknown_service_id,
        }
        operation_unknown = True
        if step is not None and step.operation_id is not None:
            matches = self.action_by_operation.get(str(step.operation_id), [])
            operation_unknown = len(matches) == 1 and matches[0].operation.status == OperationStatus.UNKNOWN
        self.check(
            run is not None
            and run.trigger == RunTrigger.MANUAL
            and run.status == RecoveryRunStatus.UNKNOWN
            and step is not None
            and step.status == RecoveryStepStatus.UNKNOWN
            and operation_unknown,
            "AGENT_DISCONNECT_NOT_PROVEN",
            "disconnect exercise needs an UNKNOWN step/Run and, when known, an UNKNOWN Operation",
            **context,
        )

    def _validate_control_plane_restart(self, exercise: ControlPlaneRestartExercise) -> None:
        run = self.runs.get(str(exercise.run_id_before))
        step = (
            next((item for item in run.steps if item.step_id == exercise.step_id), None)
            if run is not None
            else None
        )
        self.check(
            run is not None
            and exercise.run_id_before == exercise.run_id_after
            and exercise.operation_id_before == exercise.operation_id_after
            and step is not None
            and step.operation_id == exercise.operation_id_after
            and run.status.terminal,
            "CONTROL_PLANE_RESUME_NOT_PROVEN",
            "CP restart must resume the same Run and same durable Operation to a terminal result",
            scenario_exercise_id=exercise.exercise_id,
            run_id=exercise.run_id_before,
        )

    def _validate_unknown_service_rejection(
        self, exercise: UnknownServiceRejectionExercise
    ) -> None:
        inventory_pairs = {
            (str(item.agent_id), item.local_service_id) for item in self.evidence.inventory.items
        }
        matches = [
            item
            for item in self.evidence.actions
            if isinstance(item, RejectedActionEvidence)
            and item.response.request_id == exercise.request_id
        ]
        valid = (
            len(matches) == 1
            and matches[0].scenario_exercise_id == exercise.exercise_id
            and matches[0].cold_round_number is None
            and (str(matches[0].agent_id), matches[0].local_service_id) not in inventory_pairs
            and matches[0].http_status == 404
            and matches[0].response.code == ErrorCode.SERVICE_NOT_ALLOWLISTED
        )
        self.check(
            valid,
            "UNKNOWN_SERVICE_REJECTION_NOT_PROVEN",
            "unknown-service exercise must reference one exported 404 SERVICE_NOT_ALLOWLISTED response",
            scenario_exercise_id=exercise.exercise_id,
        )

    def _scenario_run_step(self, run_id: UUID, service_id: UUID) -> tuple[Any, Any]:
        run = self.runs.get(str(run_id))
        step = (
            next(
                (item for item in run.steps if item.managed_service_id == service_id),
                None,
            )
            if run is not None
            else None
        )
        return run, step

    def _validate_run(
        self, owner: AcceptanceRound | ScenarioExercise, run: RecoveryRun
    ) -> None:
        context = {**_owner_context(owner), "run_id": run.run_id}
        self.check(
            run.status.terminal,
            "RUN_NOT_TERMINAL",
            "acceptance run is not in a terminal state",
            **context,
        )
        run_times = [
            run.created_at,
            run.updated_at,
            *([run.started_at] if run.started_at is not None else []),
            *([run.finished_at] if run.finished_at is not None else []),
        ]
        self.check(
            run.finished_at is not None
            and all(_is_aware(item) and _within(owner, item) for item in run_times),
            "RUN_OUTSIDE_OWNER_WINDOW",
            "Run timestamps must be offset-aware and remain inside its assigned cold-round or scenario window",
            **context,
        )
        self.check(
            _run_times_are_valid(run),
            "RUN_TIME_INVALID",
            "Run timestamps must be monotonic: created <= started <= finished <= updated",
            **context,
        )
        member_by_id = {str(item.managed_service_id): item for item in run.members_snapshot}
        step_by_service = {str(item.managed_service_id): item for item in run.steps}
        step_by_id = {str(item.step_id): item for item in run.steps}
        self.check(
            len(member_by_id) == len(run.members_snapshot),
            "RUN_MEMBER_NOT_UNIQUE",
            "members_snapshot contains a duplicate managed_service_id",
            **context,
        )
        self.check(
            len(step_by_service) == len(run.steps) and len(step_by_id) == len(run.steps),
            "RUN_STEP_NOT_UNIQUE",
            "steps contains a duplicate step_id or managed_service_id",
            **context,
        )
        self.check(
            set(member_by_id) == set(step_by_service),
            "RUN_STEP_MEMBER_MISMATCH",
            "run steps do not exactly cover the member snapshot",
            **context,
        )
        for service_id, member in member_by_id.items():
            inventory = self.inventory.get(service_id)
            self.check(
                inventory is not None
                and inventory.agent_id == member.agent_id
                and inventory.local_service_id == member.local_service_id,
                "RUN_MEMBER_INVENTORY_MISMATCH",
                "run member does not match the exported inventory mapping",
                managed_service_id=member.managed_service_id,
                **context,
            )
        dependencies = [
            (str(item.managed_service_id), str(item.prerequisite_managed_service_id))
            for item in run.dependencies_snapshot
        ]
        self.check(
            len(dependencies) == len(set(dependencies)),
            "RUN_DEPENDENCY_NOT_UNIQUE",
            "dependencies_snapshot must not contain duplicate dependency edges",
            **context,
        )
        try:
            levels = topological_levels(member_by_id, dependencies)
        except (ValueError, DependencyCycleError) as exc:
            self.error("RUN_GRAPH_INVALID", str(exc), **context)
            return
        expected_level = {
            service_id: level_number
            for level_number, level in enumerate(levels)
            for service_id in level
        }
        for service_id, step in step_by_service.items():
            self.check(
                step.topology_level == expected_level.get(service_id),
                "TOPOLOGY_LEVEL_INVALID",
                "step topology_level does not match Kahn topological levels",
                managed_service_id=step.managed_service_id,
                **context,
            )
        roles = {
            key: str(value)
            for key, value in self.evidence.campaign.service_roles.model_dump().items()
        }
        required_edges = {
            (roles["nacos"], roles["mysql"]),
            (roles["nacos"], roles["redis"]),
            (roles["java"], roles["nacos"]),
            (roles["nginx"], roles["java"]),
        }
        self.check(
            set(roles.values()) <= set(member_by_id) and required_edges <= set(dependencies),
            "REQUIRED_ROLE_CHAIN_MISSING",
            "run does not contain the required MySQL+Redis -> Nacos -> Java -> Nginx chain",
            **context,
        )
        for dependent_id, prerequisite_id in dependencies:
            dependent = step_by_service.get(dependent_id)
            prerequisite = step_by_service.get(prerequisite_id)
            if dependent is None or prerequisite is None:
                continue
            if dependent.status != RecoveryStepStatus.BLOCKED:
                self.check(
                    prerequisite.status == RecoveryStepStatus.READY,
                    "DEPENDENCY_NOT_READY",
                    "a non-BLOCKED downstream step has a prerequisite that is not READY",
                    managed_service_id=dependent.managed_service_id,
                    **context,
                )
            activity_times = _step_activity_times(dependent)
            if activity_times:
                self.check(
                    prerequisite.status == RecoveryStepStatus.READY
                    and prerequisite.finished_at is not None
                    and _is_aware(prerequisite.finished_at)
                    and all(_is_aware(item) for item in activity_times)
                    and all(item >= prerequisite.finished_at for item in activity_times),
                    "DEPENDENCY_ORDER_VIOLATION",
                    "downstream STARTING/PROBING evidence precedes prerequisite READY",
                    managed_service_id=dependent.managed_service_id,
                    **context,
                )
        failed_roots = [
            service_id
            for service_id, step in step_by_service.items()
            if step.status in {RecoveryStepStatus.FAILED, RecoveryStepStatus.UNKNOWN}
        ]
        descendants = reachable_descendants(failed_roots, dependencies)
        for service_id in descendants:
            step = step_by_service[service_id]
            self.check(
                step.status == RecoveryStepStatus.BLOCKED,
                "FAILED_DESCENDANT_NOT_BLOCKED",
                "every strict downstream of FAILED/UNKNOWN must be BLOCKED",
                managed_service_id=step.managed_service_id,
                **context,
            )
        for step in run.steps:
            step_times = [
                step.created_at,
                step.updated_at,
                *([step.started_at] if step.started_at is not None else []),
                *([step.finished_at] if step.finished_at is not None else []),
                *(
                    value
                    for attempt in step.probe_attempts
                    for value in (
                        attempt.started_at,
                        attempt.finished_at,
                        attempt.result.observed_at,
                    )
                ),
            ]
            self.check(
                all(_is_aware(item) and _within(owner, item) for item in step_times),
                "STEP_OUTSIDE_OWNER_WINDOW",
                "Step and probe timestamps must be offset-aware and remain inside the Run owner window",
                managed_service_id=step.managed_service_id,
                **context,
            )
            self.check(
                _step_times_are_valid(run, step),
                "STEP_TIME_INVALID",
                "Step timestamps must be monotonic and remain bounded by the Run timestamps",
                managed_service_id=step.managed_service_id,
                **context,
            )
            self.check(
                _probe_attempt_times_are_valid(step),
                "PROBE_ATTEMPT_TIME_INVALID",
                "probe attempts must be sequential, monotonic, non-overlapping and bounded by the Step",
                managed_service_id=step.managed_service_id,
                **context,
            )
            if step.status == RecoveryStepStatus.READY:
                attempts = sorted(step.probe_attempts, key=lambda item: item.attempt)
                self.check(
                    bool(attempts) and attempts[-1].result.passed,
                    "READY_WITHOUT_PASSED_PROBE",
                    "READY requires a final successful readiness attempt",
                    managed_service_id=step.managed_service_id,
                    **context,
                )
            if step.status == RecoveryStepStatus.BLOCKED:
                self._validate_blocked_root(step, step_by_id, dependencies, context)
            if step.operation_id is not None:
                matches = self.action_by_operation.get(str(step.operation_id), [])
                self.check(
                    len(matches) == 1,
                    "OPERATION_EVIDENCE_MISSING",
                    "step operation_id must have exactly one exported terminal Operation",
                    managed_service_id=step.managed_service_id,
                    **context,
                )
                if len(matches) == 1:
                    self._validate_operation_link(step, matches[0], owner, run)
        statuses = {item.status for item in run.steps}
        expected_status: RecoveryRunStatus | None
        if RecoveryStepStatus.UNKNOWN in statuses:
            expected_status = RecoveryRunStatus.UNKNOWN
        elif statuses & {RecoveryStepStatus.FAILED, RecoveryStepStatus.BLOCKED}:
            expected_status = RecoveryRunStatus.FAILED
        elif statuses and statuses == {RecoveryStepStatus.READY}:
            expected_status = RecoveryRunStatus.SUCCEEDED
        else:
            expected_status = None
        self.check(
            run.status == expected_status,
            "RUN_STATUS_INCONSISTENT",
            "run terminal status does not match its step statuses",
            **context,
        )

    def _validate_blocked_root(
        self,
        step: Any,
        step_by_id: dict[str, Any],
        dependencies: list[tuple[str, str]],
        context: dict[str, Any],
    ) -> None:
        root = step_by_id.get(str(step.root_cause_step_id))
        self.check(
            root is not None
            and root.status in {RecoveryStepStatus.FAILED, RecoveryStepStatus.UNKNOWN},
            "BLOCKED_ROOT_INVALID",
            "BLOCKED step must identify a FAILED/UNKNOWN root cause step",
            managed_service_id=step.managed_service_id,
            **context,
        )
        if root is None:
            return
        descendants = reachable_descendants(
            [str(root.managed_service_id)], dependencies
        )
        self.check(
            str(step.managed_service_id) in descendants,
            "BLOCKED_ROOT_NOT_ANCESTOR",
            "BLOCKED root cause is not an upstream dependency",
            managed_service_id=step.managed_service_id,
            **context,
        )
        chain = [str(item) for item in step.dependency_chain]
        chain_steps = [step_by_id.get(item) for item in chain]
        dependency_edges = set(dependencies)
        chain_is_valid = (
            bool(chain)
            and len(chain) == len(set(chain))
            and chain[0] == str(root.step_id)
            and all(item is not None for item in chain_steps)
            and str(step.step_id) not in chain
        )
        if chain_is_valid:
            path = [*chain_steps, step]
            chain_is_valid = all(
                (
                    str(dependent.managed_service_id),
                    str(prerequisite.managed_service_id),
                )
                in dependency_edges
                for prerequisite, dependent in zip(path, path[1:])
            )
        self.check(
            chain_is_valid,
            "BLOCKED_DEPENDENCY_CHAIN_INVALID",
            "dependency_chain must be a continuous strict path from root cause through the BLOCKED step's direct prerequisite",
            managed_service_id=step.managed_service_id,
            **context,
        )

    def _validate_operation_link(
        self,
        step: Any,
        action: OperationActionEvidence,
        owner: AcceptanceRound | ScenarioExercise,
        run: RecoveryRun,
    ) -> None:
        operation = action.operation
        valid = (
            _action_belongs_to_owner(action, owner)
            and action.run_id == run.run_id
            and action.step_id == step.step_id
            and action.managed_service_id == step.managed_service_id
            and operation.agent_id == step.agent_id
            and operation.local_service_id == step.local_service_id
            and operation.action == ServiceAction.START
            and step.dispatch_idempotency_key == operation.idempotency_key
        )
        self.check(
            valid,
            "OPERATION_LINK_MISMATCH",
            "Operation does not match its owner, run, step, service or dispatch key",
            **_owner_context(owner),
            run_id=run.run_id,
            managed_service_id=step.managed_service_id,
        )
        if operation.status in {OperationStatus.FAILED, OperationStatus.REJECTED}:
            compatible = step.status == RecoveryStepStatus.FAILED
        elif operation.status == OperationStatus.UNKNOWN:
            compatible = step.status == RecoveryStepStatus.UNKNOWN
        else:
            compatible = step.status in {
                RecoveryStepStatus.READY,
                RecoveryStepStatus.FAILED,
                RecoveryStepStatus.UNKNOWN,
            }
        self.check(
            compatible,
            "OPERATION_STEP_STATUS_INCONSISTENT",
            "terminal Operation status is inconsistent with the step result",
            **_owner_context(owner),
            run_id=run.run_id,
            managed_service_id=step.managed_service_id,
        )


def validate_evidence(evidence: AcceptanceEvidence) -> EvidenceReport:
    return _Validator(evidence).validate()


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _within(owner: AcceptanceRound | ScenarioExercise, value: datetime) -> bool:
    if not (_is_aware(owner.window_started_at) and _is_aware(owner.window_finished_at)):
        return False
    if not _is_aware(value):
        return False
    return owner.window_started_at <= value <= owner.window_finished_at


def _owner_context(owner: AcceptanceRound | ScenarioExercise) -> dict[str, Any]:
    if isinstance(owner, AcceptanceRound):
        return {"round_number": owner.round_number}
    return {"scenario_exercise_id": owner.exercise_id}


def _owner_context_from_action(action: ActionEvidence) -> dict[str, Any]:
    if action.cold_round_number is not None:
        return {"round_number": action.cold_round_number}
    return {"scenario_exercise_id": action.scenario_exercise_id}


def _action_belongs_to_owner(
    action: ActionEvidence, owner: AcceptanceRound | ScenarioExercise
) -> bool:
    if isinstance(owner, AcceptanceRound):
        return (
            action.cold_round_number == owner.round_number
            and action.scenario_exercise_id is None
        )
    return (
        action.cold_round_number is None
        and action.scenario_exercise_id == owner.exercise_id
    )


def _owner_run_ids(owner: AcceptanceRound | ScenarioExercise) -> list[UUID]:
    if isinstance(owner, AcceptanceRound):
        return [owner.run_id]
    return _scenario_run_ids(owner)


def _step_activity_times(step: Any) -> list[datetime]:
    values: list[datetime] = []
    if step.started_at is not None:
        values.append(step.started_at)
    values.extend(item.started_at for item in step.probe_attempts)
    return values


def _operation_times_are_valid(operation: Operation) -> bool:
    timestamps = [
        operation.created_at,
        operation.updated_at,
        *([operation.started_at] if operation.started_at is not None else []),
        *([operation.finished_at] if operation.finished_at is not None else []),
    ]
    if not all(_is_aware(item) for item in timestamps):
        return False
    if operation.finished_at is None:
        return False
    if not (
        operation.created_at <= operation.finished_at <= operation.updated_at
    ):
        return False
    if operation.started_at is not None and not (
        operation.created_at <= operation.started_at <= operation.finished_at
    ):
        return False
    return True


def _run_times_are_valid(run: RecoveryRun) -> bool:
    timestamps = [
        run.created_at,
        run.updated_at,
        *([run.started_at] if run.started_at is not None else []),
        *([run.finished_at] if run.finished_at is not None else []),
    ]
    if not all(_is_aware(item) for item in timestamps) or run.finished_at is None:
        return False
    if not run.created_at <= run.finished_at <= run.updated_at:
        return False
    return run.started_at is None or (
        run.created_at <= run.started_at <= run.finished_at
    )


def _step_times_are_valid(run: RecoveryRun, step: Any) -> bool:
    timestamps = [
        step.created_at,
        step.updated_at,
        *([step.started_at] if step.started_at is not None else []),
        *([step.finished_at] if step.finished_at is not None else []),
    ]
    if not all(_is_aware(item) for item in timestamps) or step.finished_at is None:
        return False
    if not (
        run.created_at
        <= step.created_at
        <= step.finished_at
        <= step.updated_at
        <= run.updated_at
    ):
        return False
    return step.started_at is None or (
        step.created_at <= step.started_at <= step.finished_at
    )


def _probe_attempt_times_are_valid(step: Any) -> bool:
    attempts = sorted(step.probe_attempts, key=lambda item: item.attempt)
    if [item.attempt for item in attempts] != list(range(1, len(attempts) + 1)):
        return False
    previous_finished: datetime | None = None
    for attempt in attempts:
        timestamps = [
            attempt.started_at,
            attempt.result.observed_at,
            attempt.finished_at,
        ]
        if not all(_is_aware(item) for item in timestamps):
            return False
        if not (
            step.created_at
            <= attempt.started_at
            <= attempt.result.observed_at
            <= attempt.finished_at
            <= step.updated_at
        ):
            return False
        if step.started_at is not None and attempt.started_at < step.started_at:
            return False
        if step.finished_at is not None and attempt.finished_at > step.finished_at:
            return False
        if previous_finished is not None and attempt.started_at < previous_finished:
            return False
        previous_finished = attempt.finished_at
    return True


def _scenario_run_ids(exercise: ScenarioExercise) -> list[UUID]:
    if isinstance(exercise, ControlPlaneRestartExercise):
        return list(dict.fromkeys([exercise.run_id_before, exercise.run_id_after]))
    run_id = getattr(exercise, "run_id", None)
    return [run_id] if isinstance(run_id, UUID) else []


def _manual_proof_records(exercises: list[ScenarioExercise]) -> list[ManualProofRecord]:
    records: list[ManualProofRecord] = []
    for exercise in exercises:
        proof = getattr(exercise, "proof", None)
        if proof is None:
            continue
        records.append(
            ManualProofRecord(
                scenario=ScenarioKind(exercise.kind),
                exercise_id=exercise.exercise_id,
                artifact_ref=proof.artifact_ref,
                sha256=proof.sha256,
                reviewed_by=proof.reviewed_by,
                reviewed_at=proof.reviewed_at,
                summary=proof.summary,
            )
        )
    return records
