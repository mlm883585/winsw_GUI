from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from jsonschema import Draft202012Validator, RefResolver
from openapi_spec_validator import validate_spec
from pydantic import TypeAdapter, ValidationError

from orchestrator.common.enums import (
    GroupState,
    InstallationState,
    OperationStatus,
    RecoveryRunStatus,
    RecoveryStepStatus,
    RuntimeState,
    StartupState,
)
from orchestrator.common.errors import ErrorCode
from orchestrator.common.models import PreconditionIssue, ReadinessWrite, RecoveryGroup
from orchestrator.common.security import canonical_request_fingerprint


ROOT = Path(__file__).resolve().parents[1]
AGENT_SPEC = ROOT / "docs" / "api" / "recovery-agent-openapi.yaml"
CP_SPEC = ROOT / "docs" / "api" / "recovery-control-plane-openapi.yaml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_openapi_documents_are_semantically_valid() -> None:
    for document in (load(AGENT_SPEC), load(CP_SPEC)):
        validate_spec(document)


def test_public_paths_are_frozen() -> None:
    assert set(load(AGENT_SPEC)["paths"]) == {
        "/healthz",
        "/api/v1/agent",
        "/api/v1/services",
        "/api/v1/services/{local_service_id}/actions/{action}",
        "/api/v1/operations/{operation_id}",
        "/api/v1/probe",
    }
    cp_paths = set(load(CP_SPEC)["paths"])
    assert "/api/v1/services/{managed_service_id}/actions/{action}" in cp_paths
    assert "/api/v1/agents/register" in cp_paths
    assert "/api/v1/recovery-runs" in cp_paths
    assert "/api/v1/recovery-runs/{run_id}" in cp_paths


def test_recovery_run_collection_contract_is_stable_and_admin_only() -> None:
    document = load(CP_SPEC)
    operation = document["paths"]["/api/v1/recovery-runs"]["get"]
    assert operation["security"] == [{"adminSession": []}]
    assert [item["$ref"].rsplit("/", 1)[-1] for item in operation["parameters"]] == [
        "RunGroupFilter",
        "RunTriggerFilter",
        "RunStatusFilter",
        "RunLimit",
        "RunCursor",
    ]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RecoveryRunCollection"
    }
    assert "422" in operation["responses"]

    parameters = document["components"]["parameters"]
    assert parameters["RunLimit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 50,
    }
    collection = document["components"]["schemas"]["RecoveryRunCollection"]
    assert collection["required"] == ["items", "next_cursor"]
    assert collection["properties"]["items"]["maxItems"] == 100


def test_recovery_group_schema_matches_public_model_and_blocked_reason_contract() -> None:
    schemas = load(CP_SPEC)["components"]["schemas"]
    group = schemas["RecoveryGroup"]
    issue = schemas["PreconditionIssue"]

    assert group["additionalProperties"] is False
    assert group["required"] == list(RecoveryGroup.model_fields)
    assert group["properties"]["description"] == {
        "type": "string",
        "minLength": 0,
        "maxLength": 1024,
    }
    for write_schema_name in ("RecoveryGroupCreate", "RecoveryGroupPatch"):
        assert schemas[write_schema_name]["properties"]["description"] == {
            "type": "string",
            "minLength": 0,
            "maxLength": 1024,
            **({"default": ""} if write_schema_name == "RecoveryGroupCreate" else {}),
        }
    assert group["properties"]["blocked_reasons"] == {
        "type": "array",
        "maxItems": 100,
        "items": {"$ref": "#/components/schemas/PreconditionIssue"},
    }

    assert issue["additionalProperties"] is False
    assert issue["required"] == ["code", "message"]
    assert set(issue["properties"]) == set(PreconditionIssue.model_fields)
    assert issue["properties"]["code"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": r"^[^\u0000]*$",
        "description": "Unicode scalar values only; U+0000 is forbidden.",
    }
    assert issue["properties"]["message"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 512,
        "pattern": r"^[^\u0000]*$",
        "description": "Unicode scalar values only; U+0000 is forbidden.",
    }
    nullable_uuid = {
        "oneOf": [
            {"$ref": "#/components/schemas/UuidV4"},
            {"type": "null"},
        ]
    }
    assert issue["properties"]["managed_service_id"] == nullable_uuid
    assert issue["properties"]["agent_id"] == nullable_uuid
    assert issue["properties"]["managed_service_ids"] == {
        "type": "array",
        "maxItems": 100,
        "uniqueItems": True,
        "items": {"$ref": "#/components/schemas/UuidV4"},
    }


def test_all_control_plane_public_arrays_have_frozen_capacity_limits() -> None:
    schemas = load(CP_SPEC)["components"]["schemas"]
    expected_limits = {
        ("AgentReport", "services"): 1024,
        ("AgentCollection", "items"): 1024,
        ("ManagedServiceCollection", "items"): 1024,
        ("RecoveryMembersWrite", "managed_service_ids"): 1024,
        ("RecoveryDependenciesWrite", "dependencies"): 16384,
        ("PreconditionIssue", "managed_service_ids"): 100,
        ("RecoveryGroup", "missing_agent_ids"): 1024,
        ("RecoveryGroup", "blocked_reasons"): 100,
        ("RecoveryGroup", "members"): 1024,
        ("RecoveryGroup", "dependencies"): 16384,
        ("RecoveryGroup", "probes"): 1024,
        ("RecoveryGroupCollection", "items"): 1024,
        ("RecoveryStep", "probe_attempts"): 1024,
        ("RecoveryStep", "warnings"): 100,
        ("RecoveryStep", "dependency_chain"): 1024,
        ("RecoveryRun", "members_snapshot"): 1024,
        ("RecoveryRun", "dependencies_snapshot"): 16384,
        ("RecoveryRun", "probes_snapshot"): 1024,
        ("RecoveryRun", "steps"): 1024,
        ("RecoveryRunCollection", "items"): 100,
    }

    discovered_arrays: dict[tuple[str, str], dict] = {}
    for schema_name, schema in schemas.items():
        for property_name, value in schema.get("properties", {}).items():
            if value.get("type") == "array":
                discovered_arrays[(schema_name, property_name)] = value

    assert set(discovered_arrays) == set(expected_limits)
    for key, expected_maximum in expected_limits.items():
        assert discovered_arrays[key]["maxItems"] == expected_maximum


def test_readiness_numeric_types_match_the_openapi_contract() -> None:
    schemas = load(CP_SPEC)["components"]["schemas"]
    expected_types = {
        ("ScmReadinessWrite", "timeout_seconds"): "number",
        ("ScmReadinessWrite", "interval_seconds"): "integer",
        ("ScmReadinessWrite", "deadline_seconds"): "integer",
        ("TcpReadinessWrite", "port"): "integer",
        ("TcpReadinessWrite", "timeout_seconds"): "number",
        ("TcpReadinessWrite", "interval_seconds"): "integer",
        ("TcpReadinessWrite", "deadline_seconds"): "integer",
        ("HttpReadinessWrite", "expected_status"): "integer",
        ("HttpReadinessWrite", "timeout_seconds"): "number",
        ("HttpReadinessWrite", "interval_seconds"): "integer",
        ("HttpReadinessWrite", "deadline_seconds"): "integer",
    }
    for (schema_name, field_name), expected_type in expected_types.items():
        assert schemas[schema_name]["properties"][field_name]["type"] == expected_type

    adapter = TypeAdapter(ReadinessWrite)
    valid = (
        {"kind": "scm", "timeout_seconds": 0.5, "interval_seconds": 1, "deadline_seconds": 1},
        {
            "kind": "tcp",
            "host": "localhost",
            "port": 3306,
            "timeout_seconds": 2,
            "interval_seconds": 3,
            "deadline_seconds": 60,
        },
        {
            "kind": "http",
            "url": "http://localhost/ready",
            "expected_status": 200,
            "timeout_seconds": 2,
            "interval_seconds": 3,
            "deadline_seconds": 60,
        },
    )
    for value in valid:
        adapter.validate_python(value)

    invalid = (
        {"kind": "scm", "timeout_seconds": "2"},
        {"kind": "scm", "timeout_seconds": True},
        {"kind": "scm", "interval_seconds": "3"},
        {"kind": "scm", "interval_seconds": 3.0},
        {"kind": "scm", "deadline_seconds": "60"},
        {"kind": "scm", "deadline_seconds": 60.0},
        {"kind": "tcp", "host": "localhost", "port": "3306"},
        {"kind": "tcp", "host": "localhost", "port": 3306.0},
        {"kind": "tcp", "host": "localhost", "port": True},
        {"kind": "http", "url": "http://localhost/ready", "expected_status": "200"},
        {"kind": "http", "url": "http://localhost/ready", "expected_status": 200.0},
    )
    for value in invalid:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


def test_blocked_reason_state_invariants_are_enforced_by_public_model() -> None:
    payload = {
        "group_id": uuid4(),
        "name": "cold-start",
        "description": "",
        "state": GroupState.DISARMED,
        "node_settle_window_seconds": 120,
        "max_parallel_services": 4,
        "baseline_epoch": None,
        "candidate_epoch": None,
        "candidate_stable_since": None,
        "settle_deadline_at": None,
        "last_scheduled_epoch": None,
        "missing_agent_ids": [],
        "blocked_reasons": [],
        "members": [],
        "dependencies": [],
        "probes": [],
        "created_at": "2026-07-17T08:00:00Z",
        "updated_at": "2026-07-17T08:00:00Z",
    }
    issue = {"code": "STARTUP_NOT_MANUAL", "message": "service must be manual"}

    for state in GroupState:
        if state is GroupState.BLOCKED_PRECONDITION:
            continue
        assert RecoveryGroup.model_validate({**payload, "state": state}).blocked_reasons == []
        with pytest.raises(ValidationError):
            RecoveryGroup.model_validate(
                {**payload, "state": state, "blocked_reasons": [issue]}
            )

    blocked = {**payload, "state": GroupState.BLOCKED_PRECONDITION, "blocked_reasons": [issue]}
    assert len(RecoveryGroup.model_validate(blocked).blocked_reasons) == 1
    assert len(
        RecoveryGroup.model_validate({**blocked, "blocked_reasons": [issue] * 100}).blocked_reasons
    ) == 100

    with pytest.raises(ValidationError):
        RecoveryGroup.model_validate({**blocked, "blocked_reasons": []})
    with pytest.raises(ValidationError):
        RecoveryGroup.model_validate({**blocked, "blocked_reasons": [issue] * 101})
    with pytest.raises(ValidationError):
        PreconditionIssue.model_validate({**issue, "unexpected": True})
    duplicate_id = uuid4()
    with pytest.raises(ValidationError):
        PreconditionIssue.model_validate(
            {**issue, "managed_service_ids": [duplicate_id, duplicate_id]}
        )
    for unsafe_text in ("\x00", "prefix\x00suffix", "\ud800"):
        with pytest.raises(ValidationError):
            PreconditionIssue.model_validate({**issue, "message": unsafe_text})


def test_common_enums_match_openapi() -> None:
    agent = load(AGENT_SPEC)["components"]["schemas"]
    cp = load(CP_SPEC)["components"]["schemas"]
    pairs = [
        (agent["InstallationState"]["enum"], InstallationState),
        (agent["RuntimeState"]["enum"], RuntimeState),
        (agent["StartupState"]["enum"], StartupState),
        (agent["OperationStatus"]["enum"], OperationStatus),
        (cp["GroupState"]["enum"], GroupState),
        (cp["RunStatus"]["enum"], RecoveryRunStatus),
        (cp["StepStatus"]["enum"], RecoveryStepStatus),
        (cp["ErrorCode"]["enum"], ErrorCode),
    ]
    for declared, implementation in pairs:
        assert declared == [item.value for item in implementation]


def test_all_media_examples_validate_against_their_schema() -> None:
    for path in (AGENT_SPEC, CP_SPEC):
        document = load(path)
        resolver = RefResolver.from_schema(document)
        checked = 0

        def walk(node: object) -> None:
            nonlocal checked
            if isinstance(node, dict):
                schema = node.get("schema")
                values: list[object] = []
                if isinstance(schema, dict) and "example" in node:
                    values.append(node["example"])
                if isinstance(schema, dict) and isinstance(node.get("examples"), dict):
                    for example in node["examples"].values():
                        values.append(example.get("value") if isinstance(example, dict) else example)
                if isinstance(schema, dict):
                    validator = Draft202012Validator(schema, resolver=resolver)
                    for value in values:
                        validator.validate(value)
                        checked += 1
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(document)
        assert checked > 0


def test_agent_operation_examples_use_the_canonical_request_fingerprint() -> None:
    document = load(AGENT_SPEC)
    examples = document["paths"][
        "/api/v1/services/{local_service_id}/actions/{action}"
    ]["post"]["responses"]["202"]["content"]["application/json"]["examples"]

    vectors = (
        (
            "pending",
            "start",
            "10bcff34a8469ba0c193fab8b37b733a88dd0b5e7c381e8f6098b6c1878c0f25",
        ),
        (
            "rejected",
            "restart",
            "e048b9fac9e8c82f4dad5b57c7f98b6ce9388b2cb0c67000c1012ca4d5e4afb1",
        ),
    )
    for example_name, action, expected in vectors:
        operation = examples[example_name]["value"]
        path = f"/api/v1/services/{operation['local_service_id']}/actions/{action}"
        independent = hashlib.sha256(f"POST\n{path}\n{{}}".encode("utf-8")).hexdigest()
        assert operation["action"] == action
        assert independent == expected
        assert canonical_request_fingerprint("POST", path, {}) == expected
        assert operation["request_fingerprint"] == expected


def test_agent_uuid_input_accepts_case_but_response_schema_is_canonical_lowercase() -> None:
    document = load(AGENT_SPEC)
    schemas = document["components"]["schemas"]
    parameters = document["components"]["parameters"]
    lowercase = "db7df82f-c129-4e7a-bc51-6bcd4433fe74"
    uppercase = lowercase.upper()

    assert parameters["IdempotencyKey"]["schema"] == {
        "$ref": "#/components/schemas/UuidV4Input"
    }
    assert parameters["OperationId"]["schema"] == {
        "$ref": "#/components/schemas/UuidV4Input"
    }
    assert not list(Draft202012Validator(schemas["UuidV4Input"]).iter_errors(lowercase))
    assert not list(Draft202012Validator(schemas["UuidV4Input"]).iter_errors(uppercase))
    assert not list(Draft202012Validator(schemas["UuidV4"]).iter_errors(lowercase))
    assert list(Draft202012Validator(schemas["UuidV4"]).iter_errors(uppercase))


def test_control_plane_uuid_inputs_accept_case_but_reject_noncanonical_forms() -> None:
    document = load(CP_SPEC)
    schemas = document["components"]["schemas"]
    parameters = document["components"]["parameters"]
    lowercase = "db7df82f-c129-4e7a-bc51-6bcd4433fe74"
    uppercase = lowercase.upper()
    for name in (
        "AgentId",
        "ManagedServiceId",
        "GroupId",
        "RunId",
        "OperationId",
        "IdempotencyKey",
        "RunGroupFilter",
    ):
        assert parameters[name]["schema"] == {
            "$ref": "#/components/schemas/UuidV4Input"
        }
    validator = Draft202012Validator(schemas["UuidV4Input"])
    assert not list(validator.iter_errors(lowercase))
    assert not list(validator.iter_errors(uppercase))
    for invalid in (
        lowercase.replace("-", ""),
        "{" + lowercase + "}",
        "urn:uuid:" + lowercase,
    ):
        assert list(validator.iter_errors(invalid))
    assert list(Draft202012Validator(schemas["UuidV4"]).iter_errors(uppercase))


def test_manual_run_reason_schema_requires_non_empty_non_null_text() -> None:
    schemas = load(CP_SPEC)["components"]["schemas"]
    manual_reason = schemas["ManualRunRequest"]["properties"]["reason"]
    run_reason = schemas["RecoveryRun"]["properties"]["reason"]
    assert manual_reason["type"] == ["string", "null"]
    assert manual_reason["minLength"] == 1
    assert manual_reason["maxLength"] == 512
    assert run_reason["type"] == ["string", "null"]
    assert run_reason["minLength"] == 1
    assert run_reason["maxLength"] == 512
