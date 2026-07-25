from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from orchestrator.common.enums import OperationStatus, ServiceAction
from orchestrator.common.models import Operation
from orchestrator.common.security import hash_password
from orchestrator.common.time import utc_now
from orchestrator.control_plane.app import create_app
from orchestrator.control_plane.config import ControlPlaneConfig
from orchestrator.control_plane.recovery import RecoveryEngineError
from orchestrator.control_plane.store import ControlPlaneStore


TOKEN = "test-cluster-token-with-at-least-32-bytes-0001"


class FakeAgentClient:
    def __init__(self, agent_id: UUID) -> None:
        self.agent_id = agent_id
        self.dispatch_count = 0
        self.operations: dict[UUID, Operation] = {}

    async def dispatch_action(self, endpoint, local_service_id, action, idempotency_key):
        self.dispatch_count += 1
        now = utc_now()
        operation = Operation(
            operation_id=uuid4(),
            agent_id=self.agent_id,
            local_service_id=local_service_id,
            windows_service_name="MySQL80",
            action=ServiceAction(action),
            status=OperationStatus.PENDING,
            idempotency_key=idempotency_key,
            request_fingerprint="a" * 64,
            created_at=now,
            updated_at=now,
            started_at=None,
            finished_at=None,
            error_code=None,
            message=None,
        )
        self.operations[operation.operation_id] = operation
        return operation

    async def get_operation(self, endpoint, operation_id):
        return self.operations[UUID(str(operation_id))]

    async def probe(self, endpoint, request):  # pragma: no cover - engine path in other tests
        raise AssertionError("probe was not expected")


class RejectingRecordingEngine:
    def __init__(self) -> None:
        self.manual_reasons: list[str | None] = []
        self.retry_reasons: list[str | None] = []

    async def create_manual_run(self, group_id, reason):
        self.manual_reasons.append(reason)
        raise RecoveryEngineError("GROUP_NOT_READY", "test group is not ready")

    async def retry_run(self, run_id, reason):
        self.retry_reasons.append(reason)
        raise RecoveryEngineError("RUN_NOT_FOUND", "test run was not found")

    def launch_run(self, run_id):  # pragma: no cover - rejecting methods never return
        raise AssertionError("a rejected test run must not launch")

    async def wait_for_idle(self) -> None:
        return None


def config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        listen_host="127.0.0.1",
        database_path=tmp_path / "cp.db",
        cluster_token=TOKEN,
        agent_source_cidrs=["10.20.0.0/24"],
        admin_username="admin",
        admin_password_hash=hash_password("secret-password"),
        session_secret="test-session-secret-with-at-least-32-bytes-0002",
    )


def report(sequence: int = 1, generation: int = 1) -> dict:
    return {
        "agent_id": "b8d82b70-5a8a-4c07-8f44-31ef85a6aa01",
        "boot_id": "dba11591-4a51-49bb-8cf0-7e3c2e85e517",
        "agent_instance_id": "a82a5615-dcf8-4f8a-a266-dfe935f83b27",
        "instance_generation": generation,
        "sequence": sequence,
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
                "last_observed_at": "2026-07-16T08:00:00Z",
            }
        ],
    }


def login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "secret-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    match = re.search(r'name="csrf-token" content="([^"]+)"', dashboard.text)
    assert match
    return match.group(1)


def test_ingress_fencing_and_admin_boundary(tmp_path: Path) -> None:
    settings = config(tmp_path)
    fake = FakeAgentClient(UUID(report()["agent_id"]))
    app = create_app(settings, agent_client=fake, start_background=False)
    with TestClient(app, client=("10.20.0.11", 50000)) as client:
        assert client.get("/healthz").status_code == 200
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.post("/api/v1/agents/register", json=report()).json()["code"] == "AUTH_REQUIRED"
        assert (
            client.post(
                "/api/v1/agents/register",
                headers={"Authorization": "Bearer wrong-token-with-at-least-32-bytes"},
                json=report(),
            ).json()["code"]
            == "AUTH_INVALID"
        )
        accepted = client.post("/api/v1/agents/register", headers=headers, json=report())
        assert accepted.status_code == 200
        duplicate = client.post("/api/v1/agents/register", headers=headers, json=report())
        assert duplicate.json()["ignored"] is True

        # Cluster token is not an administrator credential.
        assert client.get("/api/v1/services", headers=headers).status_code == 401
        csrf = login(client)
        assert client.get("/api/v1/services").status_code == 200
        assert client.post("/api/v1/recovery-groups", json={"name": "x"}).status_code == 403
        created = client.post(
            "/api/v1/recovery-groups",
            headers={"X-CSRF-Token": csrf},
            json={"name": "cold-start"},
        )
        assert created.status_code == 201


def test_proxy_action_is_idempotent_at_control_plane(tmp_path: Path) -> None:
    settings = config(tmp_path)
    agent_id = UUID(report()["agent_id"])
    fake = FakeAgentClient(agent_id)
    ticks = {"now": 0.0}
    store = ControlPlaneStore(
        settings.database_path, monotonic=lambda: ticks["now"]
    )
    app = create_app(
        settings, store=store, agent_client=fake, start_background=False
    )
    with TestClient(app, client=("10.20.0.11", 50000)) as client:
        client.post(
            "/api/v1/agents/register",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=report(),
        )
        csrf = login(client)
        service_id = client.get("/api/v1/services").json()["items"][0]["managed_service_id"]
        key = str(uuid4())
        headers = {"X-CSRF-Token": csrf, "Idempotency-Key": key}
        first = client.post(f"/api/v1/services/{service_id}/actions/start", headers=headers)
        second = client.post(f"/api/v1/services/{service_id}/actions/start", headers=headers)
        assert first.status_code == second.status_code == 202
        assert first.json()["operation_id"] == second.json()["operation_id"]
        assert fake.dispatch_count == 1

        # Once prepared, the same retry must remain resolvable even if the
        # mutable Agent lease expires before the client receives its response.
        with app.state.store.db.transaction() as connection:
            connection.execute(
                "UPDATE agents SET last_received_at='2000-01-01T00:00:00.000000Z'"
            )
        replay_after_offline = client.post(
            f"/api/v1/services/{service_id}/actions/start", headers=headers
        )
        assert replay_after_offline.status_code == 202
        assert replay_after_offline.json()["operation_id"] == first.json()["operation_id"]
        assert fake.dispatch_count == 1
        assert client.get("/api/v1/services").json()["items"][0]["agent_online_status"] == "ONLINE"

        ticks["now"] = 45.0
        fresh_headers = {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())}
        assert (
            client.post(
                f"/api/v1/services/{service_id}/actions/start",
                headers=fresh_headers,
            ).json()["code"]
            == "AGENT_OFFLINE"
        )


def test_proxy_action_rejects_non_empty_body_before_dispatch(tmp_path: Path) -> None:
    settings = config(tmp_path)
    agent_id = UUID(report()["agent_id"])
    fake = FakeAgentClient(agent_id)
    app = create_app(settings, agent_client=fake, start_background=False)
    with TestClient(app, client=("10.20.0.11", 50000)) as client:
        client.post(
            "/api/v1/agents/register",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=report(),
        )
        csrf = login(client)
        service_id = client.get("/api/v1/services").json()["items"][0]["managed_service_id"]
        response = client.post(
            f"/api/v1/services/{service_id}/actions/start",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": str(uuid4()),
            },
            json={"PowerShell": "Get-Service"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert fake.dispatch_count == 0

        explicit_null = client.post(
            f"/api/v1/services/{service_id}/actions/start",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": str(uuid4()),
                "Content-Type": "application/json",
            },
            content="null",
        )
        assert explicit_null.status_code == 422
        assert explicit_null.json()["code"] == "VALIDATION_ERROR"
        assert fake.dispatch_count == 0


def test_public_uuid_inputs_require_canonical_hyphenated_uuid4(tmp_path: Path) -> None:
    settings = config(tmp_path)
    fake = FakeAgentClient(UUID(report()["agent_id"]))
    app = create_app(settings, agent_client=fake, start_background=False)
    canonical = "72961a6e-35cd-46c8-b405-56d73dbf3619"
    invalid_values = (
        canonical.replace("-", ""),
        "{" + canonical + "}",
        "urn:uuid:" + canonical,
    )
    with TestClient(app, client=("10.20.0.11", 50000)) as client:
        csrf = login(client)
        for value in invalid_values:
            requests = (
                client.get(f"/api/v1/operations/{value}"),
                client.get(f"/api/v1/recovery-groups/{value}"),
                client.get(f"/api/v1/recovery-runs/{value}"),
                client.get("/api/v1/recovery-runs", params={"group_id": value}),
                client.post(
                    f"/api/v1/services/{value}/actions/start",
                    headers={
                        "X-CSRF-Token": csrf,
                        "Idempotency-Key": str(uuid4()),
                    },
                ),
            )
            for response in requests:
                assert response.status_code == 422
                assert response.json()["code"] == "VALIDATION_ERROR"
        assert fake.dispatch_count == 0

        created = client.post(
            "/api/v1/recovery-groups",
            headers={"X-CSRF-Token": csrf},
            json={"name": "uuid-case"},
        )
        assert created.status_code == 201
        uppercase = created.json()["group_id"].upper()
        assert client.get(f"/api/v1/recovery-groups/{uppercase}").status_code == 200


def test_group_timing_fields_reject_coerced_numbers_without_persisting(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    app = create_app(
        settings,
        agent_client=FakeAgentClient(UUID(report()["agent_id"])),
        start_background=False,
    )
    invalid_create_values = (
        {"node_settle_window_seconds": "120"},
        {"node_settle_window_seconds": True},
        {"node_settle_window_seconds": 120.0},
        {"max_parallel_services": "4"},
        {"max_parallel_services": True},
        {"max_parallel_services": 4.0},
    )
    with TestClient(app) as client:
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        for index, invalid in enumerate(invalid_create_values):
            response = client.post(
                "/api/v1/recovery-groups",
                headers=headers,
                json={"name": f"invalid-{index}", **invalid},
            )
            assert response.status_code == 422
            assert response.json()["code"] == "VALIDATION_ERROR"
        assert client.get("/api/v1/recovery-groups").json()["items"] == []

        created = client.post(
            "/api/v1/recovery-groups",
            headers=headers,
            json={
                "name": "strict-group",
                "node_settle_window_seconds": 120,
                "max_parallel_services": 4,
            },
        )
        assert created.status_code == 201
        group_id = created.json()["group_id"]
        for invalid in invalid_create_values:
            field, value = next(iter(invalid.items()))
            response = client.patch(
                f"/api/v1/recovery-groups/{group_id}",
                headers=headers,
                json={field: value},
            )
            assert response.status_code == 422
            assert response.json()["code"] == "VALIDATION_ERROR"
        unchanged = client.get(f"/api/v1/recovery-groups/{group_id}").json()
        assert unchanged["node_settle_window_seconds"] == 120
        assert unchanged["max_parallel_services"] == 4


def test_group_capacity_returns_contract_error_without_partial_write(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("orchestrator.control_plane.store.MAX_SERVICE_ITEMS", 1)
    settings = config(tmp_path)
    store = ControlPlaneStore(settings.database_path)
    app = create_app(
        settings,
        store=store,
        agent_client=FakeAgentClient(UUID(report()["agent_id"])),
        start_background=False,
    )
    with TestClient(app) as client:
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        first = client.post(
            "/api/v1/recovery-groups",
            headers=headers,
            json={"name": "only-slot"},
        )
        assert first.status_code == 201

        exceeded = client.post(
            "/api/v1/recovery-groups",
            headers=headers,
            json={"name": "one-too-many"},
        )
        assert exceeded.status_code == 422
        assert exceeded.json()["code"] == "VALIDATION_ERROR"
        assert exceeded.json()["message"] == "Recovery group capacity was reached"
        items = client.get("/api/v1/recovery-groups").json()["items"]
        assert [item["name"] for item in items] == ["only-slot"]


def test_manual_run_reason_boundaries_reject_before_engine_side_effects(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    engine = RejectingRecordingEngine()
    app = create_app(
        settings,
        agent_client=FakeAgentClient(UUID(report()["agent_id"])),
        recovery_engine=engine,
        start_background=False,
    )
    group_id = str(uuid4())
    run_id = str(uuid4())
    with TestClient(app) as client:
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        endpoints = (
            (f"/api/v1/recovery-groups/{group_id}/runs", engine.manual_reasons),
            (f"/api/v1/recovery-runs/{run_id}/retry", engine.retry_reasons),
        )
        for endpoint, calls in endpoints:
            omitted = client.post(endpoint, headers=headers)
            assert omitted.status_code in {404, 409}
            assert calls[-1] is None

            for body, expected in (
                ({}, None),
                ({"reason": None}, None),
                ({"reason": "x"}, "x"),
                ({"reason": "x" * 512}, "x" * 512),
            ):
                accepted = client.post(endpoint, headers=headers, json=body)
                assert accepted.status_code in {404, 409}
                assert calls[-1] == expected

            accepted_count = len(calls)
            for body in (
                {"reason": ""},
                {"reason": "x" * 513},
                {"unknown": "value"},
            ):
                rejected = client.post(endpoint, headers=headers, json=body)
                assert rejected.status_code == 422
                assert rejected.json()["code"] == "VALIDATION_ERROR"
                assert len(calls) == accepted_count


def test_probe_write_rejects_coerced_numeric_types_without_persisting(tmp_path: Path) -> None:
    settings = config(tmp_path)
    fake = FakeAgentClient(UUID(report()["agent_id"]))
    app = create_app(settings, agent_client=fake, start_background=False)
    with TestClient(app, client=("10.20.0.11", 50000)) as client:
        client.post(
            "/api/v1/agents/register",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=report(),
        )
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        service_id = client.get("/api/v1/services").json()["items"][0][
            "managed_service_id"
        ]
        group = client.post(
            "/api/v1/recovery-groups",
            headers=headers,
            json={"name": "strict-probe-types"},
        ).json()
        group_id = group["group_id"]
        members = client.put(
            f"/api/v1/recovery-groups/{group_id}/members",
            headers=headers,
            json={"managed_service_ids": [service_id]},
        )
        assert members.status_code == 200
        probe_path = (
            f"/api/v1/recovery-groups/{group_id}/services/{service_id}/probe"
        )

        invalid_definitions = (
            {"kind": "scm", "timeout_seconds": "2"},
            {"kind": "scm", "timeout_seconds": True},
            {"kind": "scm", "interval_seconds": "3"},
            {"kind": "scm", "interval_seconds": 3.0},
            {"kind": "scm", "deadline_seconds": "60"},
            {"kind": "scm", "deadline_seconds": 60.0},
            {"kind": "tcp", "host": "localhost", "port": "3306"},
            {"kind": "tcp", "host": "localhost", "port": 3306.0},
            {"kind": "tcp", "host": "localhost", "port": True},
            {
                "kind": "http",
                "url": "http://localhost/ready",
                "expected_status": "200",
            },
            {
                "kind": "http",
                "url": "http://localhost/ready",
                "expected_status": 200.0,
            },
        )
        for definition in invalid_definitions:
            rejected = client.put(probe_path, headers=headers, json=definition)
            assert rejected.status_code == 422
            assert rejected.json()["code"] == "VALIDATION_ERROR"
            assert client.get(probe_path).status_code == 404

        accepted = client.put(
            probe_path,
            headers=headers,
            json={
                "kind": "scm",
                "timeout_seconds": 0.5,
                "interval_seconds": 1,
                "deadline_seconds": 1,
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["definition"] == {
            "kind": "scm",
            "timeout_seconds": 0.5,
            "interval_seconds": 1,
            "deadline_seconds": 1,
        }


def test_ingress_rejects_endpoint_not_matching_socket_peer(tmp_path: Path) -> None:
    settings = config(tmp_path)
    app = create_app(
        settings,
        agent_client=FakeAgentClient(UUID(report()["agent_id"])),
        start_background=False,
    )
    bad = report()
    bad["endpoint"] = "http://10.20.0.99:8765"
    with TestClient(app, client=("10.20.0.11", 50000)) as client:
        response = client.post(
            "/api/v1/agents/register",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=bad,
        )
        assert response.status_code == 409
        assert response.json()["code"] == "ENDPOINT_SOURCE_MISMATCH"


def test_logout_requires_the_matching_session_csrf_token(tmp_path: Path) -> None:
    settings = config(tmp_path)
    app = create_app(
        settings,
        agent_client=FakeAgentClient(UUID(report()["agent_id"])),
        start_background=False,
    )
    with TestClient(app) as client:
        unauthenticated = client.post("/logout", data={"csrf_token": "not-a-token"})
        assert unauthenticated.status_code == 403
        assert unauthenticated.json()["code"] == "AUTH_INVALID"

        csrf = login(client)
        rejected = client.post("/logout", data={"csrf_token": csrf + "x"})
        assert rejected.status_code == 403
        assert client.get("/").status_code == 200

        accepted = client.post(
            "/logout", data={"csrf_token": csrf}, follow_redirects=False
        )
        assert accepted.status_code == 303
        assert accepted.headers["location"] == "/login"
        assert client.get("/", follow_redirects=False).status_code == 303
