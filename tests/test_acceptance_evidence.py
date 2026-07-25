from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.acceptance.evidence import (
    AcceptanceEvidence,
    load_evidence,
    validate_evidence,
)
from scripts.validate_recovery_evidence import main


def _passing_payload() -> dict:
    base = datetime(2026, 7, 20, tzinfo=timezone.utc)
    agents = [uuid4() for _ in range(3)]
    services = {name: uuid4() for name in ("mysql", "redis", "nacos", "java", "nginx")}
    service_agents = {
        "mysql": agents[0],
        "redis": agents[0],
        "nacos": agents[1],
        "java": agents[2],
        "nginx": agents[2],
    }
    group_id = uuid4()
    inventory = []
    for name, service_id in services.items():
        inventory.append(
            {
                "local_service_id": name,
                "windows_service_name": f"MVP-{name}",
                "display_name": name.title(),
                "installation_state": "INSTALLED",
                "runtime_state": "ACTIVE",
                "startup_state": "AUTOSTART_DISABLED",
                "last_observed_at": base.isoformat(),
                "managed_service_id": str(service_id),
                "agent_id": str(service_agents[name]),
                "agent_online_status": "ONLINE",
                "mirrored_at": base.isoformat(),
            }
        )
    dependencies = [
        ("nacos", "mysql"),
        ("nacos", "redis"),
        ("java", "nacos"),
        ("nginx", "java"),
    ]
    levels = {"mysql": 0, "redis": 0, "nacos": 1, "java": 2, "nginx": 3}
    offsets = {"mysql": 130, "redis": 130, "nacos": 150, "java": 170, "nginx": 190}
    runs = []
    rounds = []
    actions = []
    for number in range(1, 11):
        window_start = base + timedelta(days=number)
        window_finish = window_start + timedelta(minutes=10)
        created = window_start + timedelta(seconds=125)
        run_id = uuid4()
        epoch = hashlib.sha256(f"round-{number}".encode()).hexdigest()
        members = [
            {
                "managed_service_id": str(services[name]),
                "agent_id": str(service_agents[name]),
                "local_service_id": name,
                "windows_service_name": f"MVP-{name}",
                "display_name": name.title(),
            }
            for name in services
        ]
        steps = []
        for name, service_id in services.items():
            started = window_start + timedelta(seconds=offsets[name])
            finished = started + timedelta(seconds=10)
            step_id = uuid4()
            operation_id = uuid4()
            key = uuid4()
            steps.append(
                {
                    "step_id": str(step_id),
                    "managed_service_id": str(service_id),
                    "agent_id": str(service_agents[name]),
                    "local_service_id": name,
                    "status": "READY",
                    "topology_level": levels[name],
                    "dispatch_idempotency_key": str(key),
                    "operation_id": str(operation_id),
                    "probe_attempts": [
                        {
                            "attempt": 1,
                            "started_at": (started + timedelta(seconds=3)).isoformat(),
                            "finished_at": (started + timedelta(seconds=4)).isoformat(),
                            "result": {
                                "passed": True,
                                "observed_at": (started + timedelta(seconds=4)).isoformat(),
                                "latency_ms": 3,
                                "code": "READY",
                                "message": "readiness passed",
                            },
                        }
                    ],
                    "warnings": [],
                    "root_cause_step_id": None,
                    "dependency_chain": [],
                    "message": "ready",
                    "created_at": created.isoformat(),
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "updated_at": finished.isoformat(),
                }
            )
            actions.append(
                {
                    "kind": "operation",
                    "cold_round_number": number,
                    "managed_service_id": str(service_id),
                    "run_id": str(run_id),
                    "step_id": str(step_id),
                    "observed_at": (started + timedelta(seconds=2)).isoformat(),
                    "operation": {
                        "operation_id": str(operation_id),
                        "agent_id": str(service_agents[name]),
                        "local_service_id": name,
                        "windows_service_name": f"MVP-{name}",
                        "action": "start",
                        "status": "SUCCEEDED",
                        "idempotency_key": str(key),
                        "request_fingerprint": hashlib.sha256(
                            f"{number}-{name}".encode()
                        ).hexdigest(),
                        "created_at": started.isoformat(),
                        "updated_at": (started + timedelta(seconds=2)).isoformat(),
                        "started_at": started.isoformat(),
                        "finished_at": (started + timedelta(seconds=2)).isoformat(),
                        "error_code": None,
                        "message": "started",
                    },
                }
            )
        runs.append(
            {
                "run_id": str(run_id),
                "group_id": str(group_id),
                "trigger": "AUTO",
                "epoch": epoch,
                "retry_of_run_id": None,
                "status": "SUCCEEDED",
                "reason": None,
                "members_snapshot": members,
                "dependencies_snapshot": [
                    {
                        "managed_service_id": str(services[dependent]),
                        "prerequisite_managed_service_id": str(services[prerequisite]),
                    }
                    for dependent, prerequisite in dependencies
                ],
                "probes_snapshot": [],
                "steps": steps,
                "failure_code": None,
                "failure_message": None,
                "created_at": created.isoformat(),
                "started_at": (created + timedelta(seconds=1)).isoformat(),
                "finished_at": (window_start + timedelta(seconds=210)).isoformat(),
                "updated_at": (window_start + timedelta(seconds=210)).isoformat(),
            }
        )
        rounds.append(
            {
                "round_number": number,
                "group_id": str(group_id),
                "epoch": epoch,
                "run_id": str(run_id),
                "window_started_at": window_start.isoformat(),
                "window_finished_at": window_finish.isoformat(),
                "boot_order": [
                    str(agents[(number + offset) % len(agents)]) for offset in range(3)
                ],
                "notes": "randomized cold start",
            }
        )
    scenario_ids = {name: uuid4() for name in (
        "control_plane_last",
        "agent_process_restart",
        "single_node_reboot",
        "missing_node",
        "start_failure",
        "probe_failure",
        "agent_disconnect",
        "control_plane_restart",
        "unknown_service_rejection",
    )}

    def add_success_scenario_run(
        source_round_number: int,
        trigger: str,
        exercise_id,
        window_start: datetime,
        *,
        probe_only_names: set[str] | None = None,
    ) -> str:
        source = runs[source_round_number - 1]
        probe_only_names = probe_only_names or set()
        run_id = uuid4()
        epoch = hashlib.sha256(f"scenario-{exercise_id}".encode()).hexdigest()
        created = window_start + timedelta(seconds=125)
        steps = []
        for name, service_id in services.items():
            started = window_start + timedelta(seconds=offsets[name])
            finished = started + timedelta(seconds=10)
            step_id = uuid4()
            operation_id = None if name in probe_only_names else uuid4()
            key = None if operation_id is None else uuid4()
            steps.append(
                {
                    "step_id": str(step_id),
                    "managed_service_id": str(service_id),
                    "agent_id": str(service_agents[name]),
                    "local_service_id": name,
                    "status": "READY",
                    "topology_level": levels[name],
                    "dispatch_idempotency_key": str(key) if key else None,
                    "operation_id": str(operation_id) if operation_id else None,
                    "probe_attempts": [
                        {
                            "attempt": 1,
                            "started_at": (started + timedelta(seconds=3)).isoformat(),
                            "finished_at": (started + timedelta(seconds=4)).isoformat(),
                            "result": {
                                "passed": True,
                                "observed_at": (started + timedelta(seconds=4)).isoformat(),
                                "latency_ms": 3,
                                "code": "READY",
                                "message": "readiness passed",
                            },
                        }
                    ],
                    "warnings": [],
                    "root_cause_step_id": None,
                    "dependency_chain": [],
                    "message": "ready",
                    "created_at": created.isoformat(),
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "updated_at": finished.isoformat(),
                }
            )
            if operation_id is not None:
                actions.append(
                    {
                        "kind": "operation",
                        "scenario_exercise_id": str(exercise_id),
                        "managed_service_id": str(service_id),
                        "run_id": str(run_id),
                        "step_id": str(step_id),
                        "observed_at": (started + timedelta(seconds=2)).isoformat(),
                        "operation": {
                            "operation_id": str(operation_id),
                            "agent_id": str(service_agents[name]),
                            "local_service_id": name,
                            "windows_service_name": f"MVP-{name}",
                            "action": "start",
                            "status": "SUCCEEDED",
                            "idempotency_key": str(key),
                            "request_fingerprint": hashlib.sha256(
                                f"scenario-{exercise_id}-{name}".encode()
                            ).hexdigest(),
                            "created_at": started.isoformat(),
                            "updated_at": (started + timedelta(seconds=2)).isoformat(),
                            "started_at": started.isoformat(),
                            "finished_at": (started + timedelta(seconds=2)).isoformat(),
                            "error_code": None,
                            "message": "started",
                        },
                    }
                )
        runs.append(
            {
                "run_id": str(run_id),
                "group_id": source["group_id"],
                "trigger": trigger,
                "epoch": epoch,
                "retry_of_run_id": None,
                "status": "SUCCEEDED",
                "reason": f"scenario {exercise_id}",
                "members_snapshot": copy.deepcopy(source["members_snapshot"]),
                "dependencies_snapshot": copy.deepcopy(source["dependencies_snapshot"]),
                "probes_snapshot": [],
                "steps": steps,
                "failure_code": None,
                "failure_message": None,
                "created_at": created.isoformat(),
                "started_at": (created + timedelta(seconds=1)).isoformat(),
                "finished_at": (window_start + timedelta(seconds=210)).isoformat(),
                "updated_at": (window_start + timedelta(seconds=210)).isoformat(),
            }
        )
        return str(run_id)

    def add_fault_run(
        source_round_number: int,
        mode: str,
        exercise_id,
        window_start: datetime,
    ) -> tuple[str, str]:
        source = runs[source_round_number - 1]
        created = window_start + timedelta(seconds=300)
        finished = window_start + timedelta(seconds=390)
        run_id = uuid4()
        step_ids = {name: uuid4() for name in services}
        root_name = "nacos"
        root_id = step_ids[root_name]
        steps = []
        for name, service_id in services.items():
            if name in {"mysql", "redis"}:
                status = "READY"
            elif name == root_name:
                status = "UNKNOWN" if mode == "disconnect" else "FAILED"
            else:
                status = "BLOCKED"
            started = created + timedelta(seconds=10 if name in {"mysql", "redis"} else 30)
            step_finished = started + timedelta(seconds=5)
            operation_id = None if status == "BLOCKED" else uuid4()
            key = None if operation_id is None else uuid4()
            if status == "READY":
                probe_attempts = [
                    {
                        "attempt": 1,
                        "started_at": (started + timedelta(seconds=2)).isoformat(),
                        "finished_at": (started + timedelta(seconds=3)).isoformat(),
                        "result": {
                            "passed": True,
                            "observed_at": (started + timedelta(seconds=3)).isoformat(),
                            "latency_ms": 2,
                            "code": "READY",
                            "message": "readiness passed",
                        },
                    }
                ]
            elif name == root_name and mode == "probe":
                probe_attempts = [
                    {
                        "attempt": 1,
                        "started_at": (started + timedelta(seconds=2)).isoformat(),
                        "finished_at": (started + timedelta(seconds=3)).isoformat(),
                        "result": {
                            "passed": False,
                            "observed_at": (started + timedelta(seconds=3)).isoformat(),
                            "latency_ms": 2000,
                            "code": "PROBE_FAILED",
                            "message": "injected readiness failure",
                        },
                    }
                ]
            else:
                probe_attempts = []
            steps.append(
                {
                    "step_id": str(step_ids[name]),
                    "managed_service_id": str(service_id),
                    "agent_id": str(service_agents[name]),
                    "local_service_id": name,
                    "status": status,
                    "topology_level": levels[name],
                    "dispatch_idempotency_key": str(key) if key else None,
                    "operation_id": str(operation_id) if operation_id else None,
                    "probe_attempts": probe_attempts,
                    "warnings": [],
                    "root_cause_step_id": str(root_id) if status == "BLOCKED" else None,
                    "dependency_chain": (
                        [str(root_id), str(step_ids["java"])]
                        if status == "BLOCKED" and name == "nginx"
                        else [str(root_id)]
                        if status == "BLOCKED"
                        else []
                    ),
                    "message": f"injected {mode}",
                    "created_at": created.isoformat(),
                    "started_at": None if status == "BLOCKED" else started.isoformat(),
                    "finished_at": step_finished.isoformat(),
                    "updated_at": step_finished.isoformat(),
                }
            )
            if operation_id is not None:
                if name == root_name and mode == "start":
                    operation_status = "FAILED"
                    error_code = "SCM_ACTION_FAILED"
                elif name == root_name and mode == "disconnect":
                    operation_status = "UNKNOWN"
                    error_code = None
                else:
                    operation_status = "SUCCEEDED"
                    error_code = None
                actions.append(
                    {
                        "kind": "operation",
                        "scenario_exercise_id": str(exercise_id),
                        "managed_service_id": str(service_id),
                        "run_id": str(run_id),
                        "step_id": str(step_ids[name]),
                        "observed_at": (started + timedelta(seconds=1)).isoformat(),
                        "operation": {
                            "operation_id": str(operation_id),
                            "agent_id": str(service_agents[name]),
                            "local_service_id": name,
                            "windows_service_name": f"MVP-{name}",
                            "action": "start",
                            "status": operation_status,
                            "idempotency_key": str(key),
                            "request_fingerprint": hashlib.sha256(
                                f"fault-{mode}-{name}".encode()
                            ).hexdigest(),
                            "created_at": started.isoformat(),
                            "updated_at": (started + timedelta(seconds=1)).isoformat(),
                            "started_at": started.isoformat(),
                            "finished_at": (started + timedelta(seconds=1)).isoformat(),
                            "error_code": error_code,
                            "message": f"injected {mode}",
                        },
                    }
                )
        run_status = "UNKNOWN" if mode == "disconnect" else "FAILED"
        runs.append(
            {
                "run_id": str(run_id),
                "group_id": source["group_id"],
                "trigger": "MANUAL",
                "epoch": source["epoch"],
                "retry_of_run_id": None,
                "status": run_status,
                "reason": f"fault injection: {mode}",
                "members_snapshot": copy.deepcopy(source["members_snapshot"]),
                "dependencies_snapshot": copy.deepcopy(source["dependencies_snapshot"]),
                "probes_snapshot": [],
                "steps": steps,
                "failure_code": None,
                "failure_message": f"injected {mode}",
                "created_at": created.isoformat(),
                "started_at": (created + timedelta(seconds=1)).isoformat(),
                "finished_at": finished.isoformat(),
                "updated_at": finished.isoformat(),
            }
        )
        return str(run_id), str(services[root_name])

    cp_last_window = base + timedelta(days=20)
    agent_restart_window = base + timedelta(days=21)
    single_reboot_window = base + timedelta(days=22)
    missing_node_window = base + timedelta(days=23)
    start_window = base + timedelta(days=24)
    probe_window = base + timedelta(days=25)
    disconnect_window = base + timedelta(days=26)
    cp_restart_window = base + timedelta(days=27)
    unknown_rejection_window = base + timedelta(days=28)
    cp_last_run_id = add_success_scenario_run(
        1,
        "AUTO",
        scenario_ids["control_plane_last"],
        cp_last_window,
    )
    single_reboot_run_id = add_success_scenario_run(
        3,
        "AUTO",
        scenario_ids["single_node_reboot"],
        single_reboot_window,
        probe_only_names={"nginx"},
    )
    cp_restart_run_id = add_success_scenario_run(
        5,
        "MANUAL",
        scenario_ids["control_plane_restart"],
        cp_restart_window,
    )
    start_failure_run_id, start_failure_service_id = add_fault_run(
        6, "start", scenario_ids["start_failure"], start_window
    )
    probe_failure_run_id, probe_failure_service_id = add_fault_run(
        7, "probe", scenario_ids["probe_failure"], probe_window
    )
    disconnect_run_id, disconnect_service_id = add_fault_run(
        8, "disconnect", scenario_ids["agent_disconnect"], disconnect_window
    )

    unknown_request_id = uuid4()
    actions.append(
        {
            "kind": "rejected_request",
            "scenario_exercise_id": str(scenario_ids["unknown_service_rejection"]),
            "agent_id": str(agents[0]),
            "local_service_id": "not-allowlisted",
            "action": "start",
            "http_status": 404,
            "observed_at": (unknown_rejection_window + timedelta(seconds=20)).isoformat(),
            "response": {
                "code": "SERVICE_NOT_ALLOWLISTED",
                "message": "Service is not in the Agent allowlist",
                "detail": None,
                "request_id": str(unknown_request_id),
            },
        }
    )

    def proof(name: str) -> dict:
        return {
            "artifact_ref": f"evidence/{name}.json",
            "sha256": hashlib.sha256(name.encode()).hexdigest(),
            "reviewed_by": "acceptance-reviewer",
            "reviewed_at": (base + timedelta(days=30)).isoformat(),
            "summary": f"reviewed {name} event evidence",
        }

    cp_restart_run = next(item for item in runs if item["run_id"] == cp_restart_run_id)
    cp_restart_step = next(
        item for item in cp_restart_run["steps"] if item["local_service_id"] == "mysql"
    )
    single_reboot_run = next(item for item in runs if item["run_id"] == single_reboot_run_id)
    scenario_exercises = [
        {
            "kind": "CONTROL_PLANE_LAST",
            "exercise_id": str(scenario_ids["control_plane_last"]),
            "window_started_at": cp_last_window.isoformat(),
            "window_finished_at": (cp_last_window + timedelta(minutes=10)).isoformat(),
            "run_id": cp_last_run_id,
            "agent_starts": [
                {
                    "agent_id": str(agent),
                    "started_at": (cp_last_window + timedelta(seconds=index + 1)).isoformat(),
                }
                for index, agent in enumerate(agents)
            ],
            "control_plane_started_at": (cp_last_window + timedelta(seconds=4)).isoformat(),
            "all_agents_registered_at": (cp_last_window + timedelta(seconds=5)).isoformat(),
            "settle_window_seconds": 120,
            "proof": proof("control-plane-last"),
        },
        {
            "kind": "AGENT_PROCESS_RESTART",
            "exercise_id": str(scenario_ids["agent_process_restart"]),
            "window_started_at": agent_restart_window.isoformat(),
            "window_finished_at": (agent_restart_window + timedelta(minutes=10)).isoformat(),
            "agent_id": str(agents[0]),
            "boot_id_before": str(uuid4()),
            "boot_id_after": None,
            "agent_instance_id_before": str(uuid4()),
            "agent_instance_id_after": str(uuid4()),
            "epoch_before": rounds[1]["epoch"],
            "epoch_after": rounds[1]["epoch"],
            "auto_run_ids_created": [],
            "proof": proof("agent-process-restart"),
        },
        {
            "kind": "SINGLE_NODE_REBOOT",
            "exercise_id": str(scenario_ids["single_node_reboot"]),
            "window_started_at": single_reboot_window.isoformat(),
            "window_finished_at": (single_reboot_window + timedelta(minutes=10)).isoformat(),
            "agent_id": str(agents[0]),
            "boot_id_before": str(uuid4()),
            "boot_id_after": str(uuid4()),
            "epoch_before": rounds[9]["epoch"],
            "epoch_after": single_reboot_run["epoch"],
            "run_id": single_reboot_run_id,
            "already_active_service_ids": [str(services["nginx"])],
            "proof": proof("single-node-reboot"),
        },
        {
            "kind": "MISSING_NODE",
            "exercise_id": str(scenario_ids["missing_node"]),
            "window_started_at": missing_node_window.isoformat(),
            "window_finished_at": (missing_node_window + timedelta(minutes=10)).isoformat(),
            "group_id": str(group_id),
            "required_agent_ids": [str(item) for item in agents],
            "missing_agent_ids": [str(agents[1])],
            "observed_group_state": "WAITING_FOR_NODES",
            "operation_ids_during_absence": [],
            "auto_run_ids_during_absence": [],
            "proof": proof("missing-node"),
        },
        {
            "kind": "START_FAILURE",
            "exercise_id": str(scenario_ids["start_failure"]),
            "window_started_at": start_window.isoformat(),
            "window_finished_at": (start_window + timedelta(minutes=10)).isoformat(),
            "run_id": start_failure_run_id,
            "failed_service_id": start_failure_service_id,
        },
        {
            "kind": "PROBE_FAILURE",
            "exercise_id": str(scenario_ids["probe_failure"]),
            "window_started_at": probe_window.isoformat(),
            "window_finished_at": (probe_window + timedelta(minutes=10)).isoformat(),
            "run_id": probe_failure_run_id,
            "failed_service_id": probe_failure_service_id,
        },
        {
            "kind": "AGENT_DISCONNECT",
            "exercise_id": str(scenario_ids["agent_disconnect"]),
            "window_started_at": disconnect_window.isoformat(),
            "window_finished_at": (disconnect_window + timedelta(minutes=10)).isoformat(),
            "run_id": disconnect_run_id,
            "unknown_service_id": disconnect_service_id,
            "proof": proof("agent-disconnect"),
        },
        {
            "kind": "CONTROL_PLANE_RESTART",
            "exercise_id": str(scenario_ids["control_plane_restart"]),
            "window_started_at": cp_restart_window.isoformat(),
            "window_finished_at": (cp_restart_window + timedelta(minutes=10)).isoformat(),
            "run_id_before": cp_restart_run_id,
            "run_id_after": cp_restart_run_id,
            "step_id": cp_restart_step["step_id"],
            "operation_id_before": cp_restart_step["operation_id"],
            "operation_id_after": cp_restart_step["operation_id"],
            "proof": proof("control-plane-restart"),
        },
        {
            "kind": "UNKNOWN_SERVICE_REJECTION",
            "exercise_id": str(scenario_ids["unknown_service_rejection"]),
            "window_started_at": unknown_rejection_window.isoformat(),
            "window_finished_at": (unknown_rejection_window + timedelta(minutes=10)).isoformat(),
            "request_id": str(unknown_request_id),
        },
    ]
    restart_boot_id = scenario_exercises[1]["boot_id_before"]
    scenario_exercises[1]["boot_id_after"] = restart_boot_id
    return {
        "schema_version": "recovery-mvp-evidence-v1",
        "campaign": {
            "campaign_id": str(uuid4()),
            "name": "three-node ten-round acceptance",
            "environment": "lab-windows",
            "required_rounds": 10,
            "service_roles": {name: str(value) for name, value in services.items()},
        },
        "completeness_attestation": {
            "operator": "acceptance-operator",
            "exported_at": (base + timedelta(days=30)).isoformat(),
            "all_runs_in_declared_windows": True,
            "all_action_attempts_in_declared_windows": True,
            "source_description": "CP Runs and Agent actions exported for every cold-round and scenario window",
        },
        "inventory": {"items": inventory},
        "rounds": rounds,
        "scenario_exercises": scenario_exercises,
        "runs": runs,
        "actions": actions,
    }


def _report(payload: dict):
    return validate_evidence(AcceptanceEvidence.model_validate(payload))


def test_passing_ten_round_evidence() -> None:
    report = _report(_passing_payload())

    assert report.verdict == "PASS"
    assert report.metrics.observed_rounds == 10
    assert report.metrics.observed_agents == 3
    assert report.metrics.observed_runs == 16
    assert report.metrics.operation_records == 73
    assert report.metrics.scenario_exercises == 9
    assert report.metrics.manual_proof_items == 6
    assert len(report.manual_proof_records) == 6
    assert report.issues == []


def test_scenario_labels_without_structured_exercises_cannot_pass() -> None:
    payload = _passing_payload()
    payload["scenario_exercises"] = []

    codes = {item.code for item in _report(payload).issues}

    assert "SCENARIO_COVERAGE_INCOMPLETE" in codes
    assert "SCENARIO_EXERCISE_MISSING" in codes
    assert "UNASSIGNED_RUN" in codes


def test_manual_only_scenario_requires_reviewed_proof() -> None:
    payload = _passing_payload()
    cp_last = next(
        item for item in payload["scenario_exercises"] if item["kind"] == "CONTROL_PLANE_LAST"
    )
    cp_last["proof"] = None

    codes = {item.code for item in _report(payload).issues}

    assert "MANUAL_PROOF_REQUIRED" in codes


def test_failure_scenario_must_reference_a_matching_failure_run() -> None:
    payload = _passing_payload()
    start_failure = next(
        item for item in payload["scenario_exercises"] if item["kind"] == "START_FAILURE"
    )
    start_failure["run_id"] = payload["rounds"][0]["run_id"]
    start_failure["failed_service_id"] = payload["campaign"]["service_roles"]["nacos"]

    codes = {item.code for item in _report(payload).issues}

    assert "START_FAILURE_NOT_PROVEN" in codes


def test_duplicate_epoch_and_run_are_rejected() -> None:
    payload = _passing_payload()
    payload["rounds"][1]["epoch"] = payload["rounds"][0]["epoch"]
    payload["runs"].append(copy.deepcopy(payload["runs"][0]))

    codes = {item.code for item in _report(payload).issues}

    assert "EPOCH_NOT_UNIQUE" in codes
    assert "RUN_ID_NOT_UNIQUE" in codes
    assert "AUTO_RUN_NOT_UNIQUE" in codes


def test_downstream_activity_before_upstream_ready_is_rejected() -> None:
    payload = _passing_payload()
    first_run = payload["runs"][0]
    nacos = next(item for item in first_run["steps"] if item["local_service_id"] == "nacos")
    early = datetime.fromisoformat(
        next(item for item in first_run["steps"] if item["local_service_id"] == "mysql")[
            "started_at"
        ]
    )
    nacos["started_at"] = early.isoformat()

    codes = {item.code for item in _report(payload).issues}

    assert "DEPENDENCY_ORDER_VIOLATION" in codes


def test_failed_root_requires_all_strict_descendants_blocked() -> None:
    payload = _passing_payload()
    first_run = payload["runs"][0]
    mysql = next(item for item in first_run["steps"] if item["local_service_id"] == "mysql")
    mysql["status"] = "FAILED"
    mysql["probe_attempts"] = []
    first_run["status"] = "FAILED"
    action = next(
        item
        for item in payload["actions"]
        if item["kind"] == "operation" and item["operation"]["operation_id"] == mysql["operation_id"]
    )
    action["operation"]["status"] = "FAILED"
    action["operation"]["error_code"] = "SCM_ACTION_FAILED"

    codes = {item.code for item in _report(payload).issues}

    assert "FAILED_DESCENDANT_NOT_BLOCKED" in codes


def test_unknown_accepted_operation_and_missing_step_operation_are_rejected() -> None:
    payload = _passing_payload()
    removed = next(item for item in payload["actions"] if item["kind"] == "operation")
    payload["actions"].remove(removed)
    unknown = copy.deepcopy(payload["actions"][0])
    unknown["operation"]["operation_id"] = str(uuid4())
    unknown["operation"]["agent_id"] = str(uuid4())
    unknown["operation"]["local_service_id"] = "unknown-service"
    payload["actions"].append(unknown)

    codes = {item.code for item in _report(payload).issues}

    assert "OPERATION_EVIDENCE_MISSING" in codes
    assert "OPERATION_TARGET_UNKNOWN" in codes
    assert "UNLINKED_OPERATION" in codes


@pytest.mark.parametrize("ownership", ["neither", "both"])
def test_action_requires_exactly_one_explicit_owner(ownership: str) -> None:
    payload = _passing_payload()
    action = next(item for item in payload["actions"] if item["kind"] == "operation")
    action.pop("cold_round_number")
    if ownership == "both":
        action["cold_round_number"] = 1
        action["scenario_exercise_id"] = payload["scenario_exercises"][0]["exercise_id"]

    with pytest.raises(ValueError, match="exactly one of cold_round_number"):
        AcceptanceEvidence.model_validate(payload)


def test_fault_operation_cannot_be_assigned_to_a_successful_cold_round() -> None:
    payload = _passing_payload()
    exercise = next(
        item for item in payload["scenario_exercises"] if item["kind"] == "START_FAILURE"
    )
    action = next(
        item
        for item in payload["actions"]
        if item.get("scenario_exercise_id") == exercise["exercise_id"]
    )
    action.pop("scenario_exercise_id")
    action["cold_round_number"] = 6

    codes = {item.code for item in _report(payload).issues}

    assert "ACTION_RUN_OWNERSHIP_MISMATCH" in codes
    assert "OPERATION_LINK_MISMATCH" in codes


def test_single_node_reboot_run_must_be_exported() -> None:
    payload = _passing_payload()
    exercise = next(
        item
        for item in payload["scenario_exercises"]
        if item["kind"] == "SINGLE_NODE_REBOOT"
    )
    payload["runs"] = [item for item in payload["runs"] if item["run_id"] != exercise["run_id"]]

    issues = _report(payload).issues

    assert any(
        item.code == "SCENARIO_RUN_NOT_EXPORTED"
        and str(item.scenario_exercise_id) == exercise["exercise_id"]
        for item in issues
    )


def test_run_cannot_belong_to_both_cold_round_and_scenario() -> None:
    payload = _passing_payload()
    exercise = next(
        item
        for item in payload["scenario_exercises"]
        if item["kind"] == "SINGLE_NODE_REBOOT"
    )
    exercise["run_id"] = payload["rounds"][2]["run_id"]

    codes = {item.code for item in _report(payload).issues}

    assert "RUN_OWNERSHIP_AMBIGUOUS" in codes


def test_scenario_run_and_step_must_stay_inside_scenario_window() -> None:
    payload = _passing_payload()
    exercise = next(
        item for item in payload["scenario_exercises"] if item["kind"] == "START_FAILURE"
    )
    run = next(item for item in payload["runs"] if item["run_id"] == exercise["run_id"])
    outside = (
        datetime.fromisoformat(exercise["window_started_at"]) - timedelta(seconds=1)
    ).isoformat()
    run["created_at"] = outside
    run["steps"][0]["updated_at"] = outside
    action = next(
        item
        for item in payload["actions"]
        if item.get("scenario_exercise_id") == exercise["exercise_id"]
    )
    action["operation"]["created_at"] = outside

    codes = {item.code for item in _report(payload).issues}

    assert "RUN_OUTSIDE_OWNER_WINDOW" in codes
    assert "STEP_OUTSIDE_OWNER_WINDOW" in codes
    assert "OPERATION_TIME_INVALID" not in codes


def test_remote_operation_times_must_be_aware_terminal_and_monotonic() -> None:
    payload = _passing_payload()
    action = next(item for item in payload["actions"] if item["kind"] == "operation")
    finished = datetime.fromisoformat(action["operation"]["finished_at"])
    action["operation"]["created_at"] = (finished + timedelta(seconds=1)).isoformat()

    codes = {item.code for item in _report(payload).issues}

    assert "OPERATION_TIME_INVALID" in codes


def test_export_must_not_predate_declared_evidence_windows() -> None:
    payload = _passing_payload()
    payload["completeness_attestation"]["exported_at"] = payload["rounds"][0][
        "window_started_at"
    ]

    codes = {item.code for item in _report(payload).issues}

    assert "EXPORT_TIME_INVALID" in codes


def test_run_times_must_be_monotonic() -> None:
    payload = _passing_payload()
    run = payload["runs"][0]
    created = datetime.fromisoformat(run["created_at"])
    run["started_at"] = (created - timedelta(seconds=1)).isoformat()

    codes = {item.code for item in _report(payload).issues}

    assert "RUN_TIME_INVALID" in codes


def test_step_times_must_be_monotonic_and_bounded_by_run() -> None:
    payload = _passing_payload()
    run = payload["runs"][0]
    step = run["steps"][0]
    created = datetime.fromisoformat(step["created_at"])
    step["updated_at"] = (created - timedelta(seconds=1)).isoformat()

    codes = {item.code for item in _report(payload).issues}

    assert "STEP_TIME_INVALID" in codes


def test_probe_attempt_times_must_be_monotonic_and_bounded_by_step() -> None:
    payload = _passing_payload()
    step = payload["runs"][0]["steps"][0]
    attempt = step["probe_attempts"][0]
    started = datetime.fromisoformat(attempt["started_at"])
    attempt["finished_at"] = (started - timedelta(seconds=1)).isoformat()

    codes = {item.code for item in _report(payload).issues}

    assert "PROBE_ATTEMPT_TIME_INVALID" in codes


def test_blocked_dependency_chain_must_be_a_continuous_strict_path() -> None:
    payload = _passing_payload()
    exercise = next(
        item for item in payload["scenario_exercises"] if item["kind"] == "START_FAILURE"
    )
    run = next(item for item in payload["runs"] if item["run_id"] == exercise["run_id"])
    nginx = next(item for item in run["steps"] if item["local_service_id"] == "nginx")
    nginx["dependency_chain"] = [nginx["root_cause_step_id"]]

    codes = {item.code for item in _report(payload).issues}

    assert "BLOCKED_DEPENDENCY_CHAIN_INVALID" in codes


def test_manual_proof_report_keeps_review_time_and_summary() -> None:
    report = _report(_passing_payload())

    assert all(item.reviewed_at.tzinfo is not None for item in report.manual_proof_records)
    assert all(item.summary.startswith("reviewed ") for item in report.manual_proof_records)


def test_run_dependency_snapshot_rejects_duplicate_edges() -> None:
    payload = _passing_payload()
    run = payload["runs"][0]
    run["dependencies_snapshot"].append(copy.deepcopy(run["dependencies_snapshot"][0]))

    codes = {item.code for item in _report(payload).issues}

    assert "RUN_DEPENDENCY_NOT_UNIQUE" in codes


def test_boot_order_must_match_run_agents_exactly() -> None:
    payload = _passing_payload()
    payload["rounds"][0]["boot_order"].append(str(uuid4()))

    codes = {item.code for item in _report(payload).issues}

    assert "BOOT_ORDER_AGENT_MISMATCH" in codes


def test_manual_review_must_follow_scenario_window() -> None:
    payload = _passing_payload()
    exercise = next(item for item in payload["scenario_exercises"] if item.get("proof"))
    exercise["proof"]["reviewed_at"] = exercise["window_started_at"]

    codes = {item.code for item in _report(payload).issues}

    assert "MANUAL_PROOF_TIME_INVALID" in codes


def test_unknown_service_rejection_must_belong_to_its_scenario() -> None:
    payload = _passing_payload()
    exercise = next(
        item
        for item in payload["scenario_exercises"]
        if item["kind"] == "UNKNOWN_SERVICE_REJECTION"
    )
    action = next(
        item
        for item in payload["actions"]
        if item["kind"] == "rejected_request"
        and item["response"]["request_id"] == exercise["request_id"]
    )
    action.pop("scenario_exercise_id")
    action["cold_round_number"] = 1

    codes = {item.code for item in _report(payload).issues}

    assert "UNKNOWN_SERVICE_REJECTION_NOT_PROVEN" in codes
    assert "ACTION_OUTSIDE_OWNER_WINDOW" in codes


def test_cli_writes_report_and_uses_distinct_exit_codes(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    report_path = tmp_path / "report.json"
    evidence_path.write_text(json.dumps(_passing_payload()), encoding="utf-8")

    assert main([str(evidence_path), "--report", str(report_path)]) == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["verdict"] == "PASS"

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{", encoding="utf-8")
    assert main([str(invalid_path)]) == 2


def test_utf8_bom_evidence_is_accepted_by_loader_and_cli(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence-with-bom.json"
    evidence_path.write_text(
        json.dumps(_passing_payload()),
        encoding="utf-8-sig",
    )

    assert validate_evidence(load_evidence(evidence_path)).verdict == "PASS"
    assert main([str(evidence_path)]) == 0


def test_repository_template_is_parseable_but_cannot_pass() -> None:
    template_path = Path(__file__).parents[1] / "examples" / "recovery-evidence.template.json"
    evidence = AcceptanceEvidence.model_validate_json(template_path.read_text(encoding="utf-8"))

    report = validate_evidence(evidence)

    assert report.verdict == "FAIL"
    assert {item.code for item in report.issues} >= {
        "RUN_EXPORT_INCOMPLETE",
        "ACTION_EXPORT_INCOMPLETE",
        "CAMPAIGN_MIN_ROUNDS",
        "SCENARIO_COVERAGE_INCOMPLETE",
    }
