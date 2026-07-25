from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from orchestrator.common.enums import RecoveryRunStatus, RunTrigger
from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.models import ProbeResult
from orchestrator.common.security import hash_password
from orchestrator.control_plane.app import create_app
from orchestrator.control_plane.config import ControlPlaneConfig
from orchestrator.control_plane.recovery import RecoveryEngine
from orchestrator.control_plane.store import ControlPlaneStore


BASE = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
TOKEN = "test-cluster-token-with-at-least-32-bytes-0001"


class Clock:
    def __init__(self) -> None:
        self.wall = BASE
        self.ticks = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.ticks += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


class ProbeOnlyAgentClient:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def start_action(self, *args, **kwargs):  # pragma: no cover - ACTIVE fixture
        raise AssertionError("ACTIVE service must not be started")

    async def get_operation(self, *args, **kwargs):  # pragma: no cover - ACTIVE fixture
        raise AssertionError("no Operation should exist for an ACTIVE service")

    async def probe(self, endpoint, request):
        del endpoint, request
        return ProbeResult(
            passed=True,
            observed_at=self.clock.now(),
            latency_ms=1,
            code="SCM_RUNNING",
            message="service is running",
        )


def settings(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        listen_host="127.0.0.1",
        database_path=tmp_path / "cp.sqlite3",
        cluster_token=TOKEN,
        agent_source_cidrs=["10.20.0.0/24"],
        admin_username="admin",
        admin_password_hash=hash_password("secret-password"),
        session_secret="test-session-secret-with-at-least-32-bytes-0002",
    )


def agent_report(
    agent_id: UUID,
    boot_id: UUID,
    instance_id: UUID,
    *,
    generation: int = 1,
    sequence: int = 1,
    runtime_state: str = "INACTIVE",
) -> dict[str, object]:
    return {
        "agent_id": str(agent_id),
        "boot_id": str(boot_id),
        "agent_instance_id": str(instance_id),
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
                "runtime_state": runtime_state,
                "startup_state": "AUTOSTART_DISABLED",
                "last_observed_at": BASE.isoformat(),
            }
        ],
    }


def store_with_group(tmp_path: Path, clock: Clock, *, runtime_state: str = "INACTIVE"):
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=clock.now)
    agent_id, boot_id, instance_id = uuid4(), uuid4(), uuid4()
    store.register_agent(
        agent_report(
            agent_id,
            boot_id,
            instance_id,
            runtime_state=runtime_state,
        ),
        "10.20.0.11",
        now=clock.now(),
    )
    managed_service_id = store.list_services(now=clock.now())[0]["managed_service_id"]
    group = store.create_group({"name": "core", "node_settle_window_seconds": 1})
    store.replace_members(group["group_id"], [managed_service_id])
    return store, group, agent_id, boot_id, instance_id


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


def test_store_keyset_pagination_filters_and_cursor_errors(tmp_path: Path) -> None:
    clock = Clock()
    store, group, *_ = store_with_group(tmp_path, clock)
    run_ids: list[str] = []
    for index in range(7):
        run = store.create_run_from_group(
            group["group_id"],
            RunTrigger.MANUAL,
            reason=f"drill-{index}",
            now=BASE,
        )
        run_ids.append(run["run_id"])
        store.finish_run(run["run_id"], RecoveryRunStatus.SUCCEEDED, now=BASE)

    pending = store.create_run_from_group(
        group["group_id"], RunTrigger.MANUAL, reason="pending", now=BASE
    )
    run_ids.append(pending["run_id"])

    discovered: list[str] = []
    cursor = None
    while True:
        page = store.list_runs(limit=3, cursor=cursor)
        discovered.extend(item["run_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert discovered == sorted(run_ids, reverse=True)
    assert len(discovered) == len(set(discovered)) == 8

    succeeded = store.list_runs(
        group_id=group["group_id"],
        trigger=RunTrigger.MANUAL,
        status=RecoveryRunStatus.SUCCEEDED,
        limit=100,
    )
    assert len(succeeded["items"]) == 7
    assert {item["group_id"] for item in succeeded["items"]} == {group["group_id"]}
    assert {item["trigger"] for item in succeeded["items"]} == {"MANUAL"}
    assert {item["status"] for item in succeeded["items"]} == {"SUCCEEDED"}

    first = store.list_runs(trigger=RunTrigger.MANUAL, limit=2)
    assert first["next_cursor"] is not None
    with pytest.raises(ApiError) as mismatched:
        store.list_runs(
            trigger=RunTrigger.AUTO,
            limit=2,
            cursor=first["next_cursor"],
        )
    assert mismatched.value.status_code == 422
    assert mismatched.value.code is ErrorCode.VALIDATION_ERROR
    assert mismatched.value.message == "RecoveryRun cursor is invalid"
    assert mismatched.value.detail is None

    with pytest.raises(ApiError) as malformed:
        store.list_runs(cursor="%%%")
    assert malformed.value.status_code == 422
    assert malformed.value.code is ErrorCode.VALIDATION_ERROR
    assert malformed.value.message == "RecoveryRun cursor is invalid"
    assert malformed.value.detail is None


def test_run_list_api_auth_filters_errors_and_dashboard(tmp_path: Path) -> None:
    clock = Clock()
    store, group, *_ = store_with_group(tmp_path, clock)
    created: list[dict[str, object]] = []
    for index in range(2):
        run = store.create_run_from_group(
            group["group_id"], RunTrigger.MANUAL, reason=f"manual-{index}", now=BASE
        )
        store.finish_run(run["run_id"], RecoveryRunStatus.SUCCEEDED, now=BASE)
        created.append(run)

    app = create_app(
        settings(tmp_path),
        store=store,
        agent_client=ProbeOnlyAgentClient(clock),
        start_background=False,
    )
    with TestClient(app) as client:
        unauthenticated = client.get("/api/v1/recovery-runs")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["code"] == "AUTH_REQUIRED"
        cluster_token = client.get(
            "/api/v1/recovery-runs",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert cluster_token.status_code == 401
        assert cluster_token.json()["code"] == "AUTH_REQUIRED"

        login(client)
        filtered = client.get(
            "/api/v1/recovery-runs",
            params={
                "group_id": group["group_id"],
                "trigger": "MANUAL",
                "status": "SUCCEEDED",
                "limit": 1,
            },
        )
        assert filtered.status_code == 200
        body = filtered.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["group_id"] == group["group_id"]
        assert body["next_cursor"] is not None

        malformed = client.get(
            "/api/v1/recovery-runs", params={"cursor": "%%%"}
        )
        assert malformed.status_code == 422
        assert malformed.json()["code"] == "VALIDATION_ERROR"
        assert malformed.json()["message"] == "RecoveryRun cursor is invalid"
        assert malformed.json()["detail"] is None
        assert set(malformed.json()) == {"code", "message", "detail", "request_id"}

        wrong_filter = client.get(
            "/api/v1/recovery-runs",
            params={"cursor": body["next_cursor"], "trigger": "AUTO", "limit": 1},
        )
        assert wrong_filter.status_code == 422
        assert wrong_filter.json()["message"] == "RecoveryRun cursor is invalid"
        assert wrong_filter.json()["detail"] is None

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        for run in created:
            assert f'/runs/{run["run_id"]}' in dashboard.text


def test_scheduler_auto_run_is_discoverable_without_known_run_id(tmp_path: Path) -> None:
    clock = Clock()
    store, group, agent_id, *_ = store_with_group(
        tmp_path, clock, runtime_state="ACTIVE"
    )
    store.arm_group(group["group_id"], now=clock.now())

    clock.advance(1)
    store.register_agent(
        agent_report(
            agent_id,
            uuid4(),
            uuid4(),
            generation=2,
            runtime_state="ACTIVE",
        ),
        "10.20.0.11",
        now=clock.now(),
    )
    client = ProbeOnlyAgentClient(clock)
    engine = RecoveryEngine(
        store,
        client,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    async def schedule() -> list[dict[str, object]]:
        assert await engine.scan_auto_groups() == []
        clock.advance(1)
        created = await engine.scan_auto_groups()
        await engine.wait_for_idle()
        return created

    scheduled = asyncio.run(schedule())
    assert len(scheduled) == 1

    # Discovery starts from filters only: no run_id from the scheduler result is
    # passed to the list operation.
    page = store.list_runs(
        trigger=RunTrigger.AUTO,
        status=RecoveryRunStatus.SUCCEEDED,
    )
    assert len(page["items"]) == 1
    discovered_run_id = page["items"][0]["run_id"]
    assert discovered_run_id == scheduled[0]["run_id"]
    assert page["next_cursor"] is None

    app = create_app(
        settings(tmp_path),
        store=store,
        agent_client=client,
        start_background=False,
    )
    with TestClient(app) as web:
        login(web)
        api_page = web.get(
            "/api/v1/recovery-runs", params={"trigger": "AUTO"}
        )
        assert api_page.status_code == 200
        assert api_page.json()["items"][0]["run_id"] == discovered_run_id
        dashboard = web.get("/")
        assert f"/runs/{discovered_run_id}" in dashboard.text
        assert "AUTO" in dashboard.text
