from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from orchestrator.common.errors import ErrorCode
from orchestrator.common.models import Operation
from orchestrator.common.security import canonical_request_fingerprint
from orchestrator.control_plane.agent_client import (
    AgentClient,
    AgentClientError,
    AgentOperationProtocolError,
)
from orchestrator.control_plane.store import ControlPlaneStore


BASE = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


def _report(agent_id) -> dict[str, object]:
    return {
        "agent_id": str(agent_id),
        "boot_id": str(uuid4()),
        "agent_instance_id": str(uuid4()),
        "instance_generation": 1,
        "sequence": 1,
        "version": "0.1.0",
        "endpoint": "http://10.20.0.11:8765",
        "hostname": "APP-SRV-01",
        "services": [
            {
                "local_service_id": "mysql",
                "windows_service_name": "MySQL80",
                "display_name": "MySQL 8",
                "installation_state": "INSTALLED",
                "runtime_state": "INACTIVE",
                "startup_state": "AUTOSTART_DISABLED",
                "last_observed_at": BASE.isoformat(),
            }
        ],
    }


def _prepared_proxy(
    tmp_path: Path,
) -> tuple[ControlPlaneStore, dict[str, object], object, object]:
    agent_id = uuid4()
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=lambda: BASE)
    store.register_agent(_report(agent_id), "10.20.0.11", now=BASE)
    managed_service_id = store.list_services()[0]["managed_service_id"]
    key = uuid4()
    fingerprint = canonical_request_fingerprint(
        "POST", f"/api/v1/services/{managed_service_id}/actions/start", {}
    )
    dispatch = store.prepare_proxy_action(
        managed_service_id, "start", key, fingerprint, now=BASE
    )
    return store, dispatch, agent_id, key


def _bound_operation(
    dispatch: dict[str, object],
    *,
    operation_id=None,
    agent_id=None,
    local_service_id: str | None = None,
    windows_service_name: str | None = None,
    action: str | None = None,
    idempotency_key=None,
    request_fingerprint: str | None = None,
) -> Operation:
    local_id = local_service_id or str(dispatch["local_service_id"])
    action_value = action or str(dispatch["action"])
    return Operation.model_validate(
        {
            "operation_id": str(operation_id or uuid4()),
            "agent_id": str(agent_id or dispatch["agent_id"]),
            "local_service_id": local_id,
            "windows_service_name": windows_service_name
            or str(dispatch["windows_service_name"]),
            "action": action_value,
            "status": "PENDING",
            "idempotency_key": str(idempotency_key or dispatch["idempotency_key"]),
            "request_fingerprint": request_fingerprint
            or canonical_request_fingerprint(
                "POST", f"/api/v1/services/{local_id}/actions/{action_value}", {}
            ),
            "created_at": BASE,
            "updated_at": BASE,
            "started_at": None,
            "finished_at": None,
            "error_code": None,
            "message": None,
        }
    )


def _operation_payload(request: httpx.Request) -> dict[str, object]:
    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc).isoformat()
    return {
        "operation_id": str(uuid4()),
        "agent_id": str(uuid4()),
        "local_service_id": "mysql",
        "windows_service_name": "MySQL80",
        "action": "start",
        "status": "PENDING",
        "idempotency_key": request.headers.get("Idempotency-Key", str(uuid4())),
        "request_fingerprint": "a" * 64,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "error_code": None,
        "message": None,
    }


@pytest.mark.asyncio
async def test_action_transport_retry_reuses_the_original_idempotency_key() -> None:
    requests: list[httpx.Request] = []
    operation_id, agent_id, key = uuid4(), uuid4(), uuid4()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("temporary disconnect", request=request)
        now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc).isoformat()
        return httpx.Response(
            202,
            request=request,
            json={
                "operation_id": str(operation_id),
                "agent_id": str(agent_id),
                "local_service_id": "mysql",
                "windows_service_name": "MySQL80",
                "action": "start",
                "status": "PENDING",
                "idempotency_key": str(key),
                "request_fingerprint": "a" * 64,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "error_code": None,
                "message": None,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as raw_client:
        client = AgentClient("x" * 32, attempts=2, client=raw_client)
        operation = await client.dispatch_action(
            "http://127.0.0.1:8765", "mysql", "start", key
        )

    assert operation.operation_id == operation_id
    assert len(requests) == 2
    assert {request.headers["Idempotency-Key"] for request in requests} == {str(key)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_kind", "defect"),
    [
        ("post", "missing"),
        ("post", "invalid_uuid"),
        ("get", "missing"),
        ("get", "invalid_uuid"),
    ],
)
async def test_malformed_successful_operation_response_uses_sanitized_protocol_error(
    request_kind: str, defect: str
) -> None:
    requests: list[httpx.Request] = []
    operation_id = uuid4()
    key = uuid4()
    canary = "sensitive-agent-payload-canary"

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = _operation_payload(request)
        if defect == "missing":
            payload.pop("operation_id")
        else:
            payload["operation_id"] = canary
        return httpx.Response(
            202 if request.method == "POST" else 200,
            request=request,
            json=payload,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as raw_client:
        client = AgentClient("x" * 32, attempts=1, client=raw_client)
        with pytest.raises(AgentOperationProtocolError) as captured:
            if request_kind == "post":
                await client.dispatch_action(
                    "http://127.0.0.1:8765", "mysql", "start", key
                )
            else:
                await client.get_operation("http://127.0.0.1:8765", operation_id)

    error = captured.value
    assert error.code == ErrorCode.AGENT_PROTOCOL_MISMATCH.value
    assert error.detail == {"reason": "operation_schema_mismatch"}
    assert canary not in str(error)
    assert "validation error" not in str(error).lower()
    assert error.__cause__ is None
    assert error.__context__ is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_agent_http_business_error_is_not_reclassified_as_operation_protocol_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            request=request,
            json={
                "code": "SERVICE_ACTION_CONFLICT",
                "message": "another action is active",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as raw_client:
        client = AgentClient("x" * 32, attempts=1, client=raw_client)
        with pytest.raises(AgentClientError) as captured:
            await client.dispatch_action(
                "http://127.0.0.1:8765", "mysql", "start", uuid4()
            )

    assert type(captured.value) is AgentClientError
    assert captured.value.code == "SERVICE_ACTION_CONFLICT"
