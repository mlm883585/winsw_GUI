from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator.agent.app import create_app
from orchestrator.agent.config import AgentConfig, AllowlistedService
from orchestrator.agent.scm import SERVICE_DEMAND_START, SERVICE_STOPPED, SCMServiceStatus
from orchestrator.agent.store import AgentStore


TOKEN = "0123456789abcdef0123456789abcdef"


class StaticBoot:
    def get_boot_marker(self) -> str:
        return "134128800000000000"


class FakeSCM:
    def __init__(self) -> None:
        self.state = SERVICE_STOPPED

    def query(self, _name: str) -> SCMServiceStatus:
        return SCMServiceStatus(self.state, start_type=SERVICE_DEMAND_START)

    def start(self, _name: str) -> None:
        self.state = 4

    def stop(self, _name: str) -> None:
        self.state = SERVICE_STOPPED


class AcceptingIngress:
    async def register(self, _report) -> bool:
        return True

    async def heartbeat(self, _report) -> bool:
        return True


def make_app(tmp_path):
    config = AgentConfig(
        control_plane_url="http://127.0.0.1:9000",
        listen_host="127.0.0.1",
        advertised_endpoint="http://127.0.0.1:8765",
        cluster_token=TOKEN,
        control_plane_source_cidrs=["127.0.0.1/32"],
        database_path=tmp_path / "agent.sqlite3",
        heartbeat_interval_seconds=300,
        services=[AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")],
    )
    return create_app(
        config,
        backend=FakeSCM(),
        boot_provider=StaticBoot(),
        ingress=AcceptingIngress(),
    )


def test_health_is_minimal_and_auth_source_fencing_protects_agent_api(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app, client=("192.0.2.10", 50000)) as denied:
        assert denied.get("/healthz").json() == {
            "status": "ok",
            "component": "agent",
            "version": "0.1.0",
        }
        response = denied.get("/api/v1/agent", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 403
        assert response.json()["code"] == "SOURCE_IP_DENIED"

    with TestClient(app, client=("127.0.0.1", 50000)) as allowed:
        assert allowed.get("/api/v1/agent").status_code == 401
        response = allowed.get(
            "/api/v1/agent", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        assert response.json()["security_mode"] == "LAB_HTTP"
        assert response.json()["production_ready"] is False


def test_action_returns_durable_operation_and_location(tmp_path) -> None:
    app = make_app(tmp_path)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Idempotency-Key": "98E48687-D102-4C38-AF9D-E0F1BB813405",
    }
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/v1/services/mysql/actions/start", headers=headers)
        assert response.status_code == 202
        operation_id = response.json()["operation_id"]
        assert response.json()["idempotency_key"] == headers["Idempotency-Key"].lower()
        assert response.headers["Location"] == f"/api/v1/operations/{operation_id}"

        repeated = client.post("/api/v1/services/mysql/actions/start", headers=headers)
        assert repeated.status_code == 202
        assert repeated.json()["operation_id"] == operation_id

        fetched = client.get(
            f"/api/v1/operations/{operation_id.upper()}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert fetched.status_code == 200
        assert fetched.json()["status"] in {"PENDING", "RUNNING", "SUCCEEDED"}

        for noncanonical in (
            operation_id.replace("-", ""),
            "{" + operation_id + "}",
            "urn:uuid:" + operation_id,
        ):
            rejected = client.get(
                f"/api/v1/operations/{noncanonical}",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert rejected.status_code == 422
            assert rejected.json()["code"] == "VALIDATION_ERROR"


def test_runtime_rejects_service_rebinding_before_boot_or_scm(tmp_path) -> None:
    database_path = tmp_path / "agent.sqlite3"
    store = AgentStore(database_path)
    store.initialize()
    store.bind_service_mappings(
        [AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")]
    )

    class MustNotReadBoot:
        called = False

        def get_boot_marker(self) -> str:
            self.called = True
            raise AssertionError("mapping validation must happen before WMI")

    boot = MustNotReadBoot()
    config = AgentConfig(
        control_plane_url="http://127.0.0.1:9000",
        listen_host="127.0.0.1",
        advertised_endpoint="http://127.0.0.1:8765",
        cluster_token=TOKEN,
        control_plane_source_cidrs=["127.0.0.1/32"],
        database_path=database_path,
        services=[
            AllowlistedService(local_service_id="mysql", windows_service_name="Spooler")
        ],
    )

    with pytest.raises(RuntimeError, match="service mapping changed"):
        create_app(
            config,
            backend=FakeSCM(),
            boot_provider=boot,
            ingress=AcceptingIngress(),
        )
    assert boot.called is False


def test_action_rejects_non_empty_body_without_operation(tmp_path) -> None:
    app = make_app(tmp_path)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Idempotency-Key": "de249b73-86f9-47f8-9206-bb740d58f155",
    }
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/api/v1/services/mysql/actions/start",
            headers=headers,
            json={"cmd": "whoami"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

        explicit_null = client.post(
            "/api/v1/services/mysql/actions/start",
            headers={**headers, "Content-Type": "application/json"},
            content="null",
        )
        assert explicit_null.status_code == 422
        assert explicit_null.json()["code"] == "VALIDATION_ERROR"
        with app.state.runtime.store.database.read() as connection:
            assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


def test_probe_unknown_kind_and_non_json_numeric_types_are_rejected(tmp_path) -> None:
    app = make_app(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    invalid_numeric_payloads = [
        {"kind": "tcp", "host": "127.0.0.1", "port": "3306"},
        {"kind": "tcp", "host": "127.0.0.1", "port": True},
        {"kind": "tcp", "host": "127.0.0.1", "port": 3306.0},
        {
            "kind": "http",
            "url": "http://127.0.0.1/health",
            "expected_status": "200",
        },
        {"kind": "scm", "local_service_id": "mysql", "timeout_seconds": "2"},
        {"kind": "scm", "local_service_id": "mysql", "timeout_seconds": True},
    ]

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        unsupported = client.post(
            "/api/v1/probe",
            headers=headers,
            json={"kind": "exec", "command": "whoami"},
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["code"] == "PROBE_UNSUPPORTED"

        invalid_discriminator_type = client.post(
            "/api/v1/probe",
            headers=headers,
            json={"kind": ["tcp"], "host": "127.0.0.1", "port": 3306},
        )
        assert invalid_discriminator_type.status_code == 422
        assert invalid_discriminator_type.json()["code"] == "VALIDATION_ERROR"

        for payload in invalid_numeric_payloads:
            response = client.post("/api/v1/probe", headers=headers, json=payload)
            assert response.status_code == 422
            assert response.json()["code"] == "VALIDATION_ERROR"


def test_framework_errors_use_stable_redacted_error_response(tmp_path) -> None:
    app = make_app(tmp_path)

    @app.get("/_test/unexpected")
    async def unexpected_error() -> None:
        raise RuntimeError("CANARY-SECRET-IN-EXCEPTION")

    with TestClient(
        app,
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    ) as client:
        missing = client.get("/route-that-does-not-exist")
        assert missing.status_code == 404
        assert missing.json()["code"] == "ROUTE_NOT_FOUND"
        assert set(missing.json()) == {"code", "message", "detail", "request_id"}

        wrong_method = client.options(
            "/api/v1/agent",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Origin": "http://browser.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert wrong_method.status_code == 405
        assert wrong_method.json()["code"] == "METHOD_NOT_ALLOWED"
        assert "Access-Control-Allow-Origin" not in wrong_method.headers

        unexpected = client.get("/_test/unexpected")
        assert unexpected.status_code == 500
        assert unexpected.json()["code"] == "INTERNAL_ERROR"
        assert "CANARY-SECRET-IN-EXCEPTION" not in unexpected.text
