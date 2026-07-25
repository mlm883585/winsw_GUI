from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from orchestrator.common.security import hash_password
from orchestrator.control_plane.app import create_app
from orchestrator.control_plane.config import ControlPlaneConfig


RUN_ID = "10000000-0000-4000-8000-000000000001"
GROUP_ID = "10000000-0000-4000-8000-000000000002"
AGENT_ID = "10000000-0000-4000-8000-000000000003"
MYSQL_SERVICE_ID = "20000000-0000-4000-8000-000000000001"
REDIS_SERVICE_ID = "20000000-0000-4000-8000-000000000002"
NACOS_SERVICE_ID = "20000000-0000-4000-8000-000000000003"
NGINX_SERVICE_ID = "20000000-0000-4000-8000-000000000004"
FAILED_STEP_ID = "30000000-0000-4000-8000-000000000001"
UNKNOWN_STEP_ID = "30000000-0000-4000-8000-000000000002"
BLOCKED_STEP_ID = "30000000-0000-4000-8000-000000000003"
READY_STEP_ID = "30000000-0000-4000-8000-000000000004"
TOKEN = "test-cluster-token-with-at-least-32-bytes-0001"


class RunDetailStore:
    def __init__(self, run: dict) -> None:
        self.run = deepcopy(run)

    def get_run(self, run_id: UUID | str) -> dict | None:
        return deepcopy(self.run) if str(run_id) == self.run["run_id"] else None


def settings(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        listen_host="127.0.0.1",
        database_path=tmp_path / "unused.sqlite3",
        cluster_token=TOKEN,
        agent_source_cidrs=["10.20.0.0/24"],
        admin_username="admin",
        admin_password_hash=hash_password("secret-password"),
        session_secret="test-session-secret-with-at-least-32-bytes-0002",
    )


def member(service_id: str, local_id: str, display_name: str) -> dict:
    return {
        "managed_service_id": service_id,
        "agent_id": AGENT_ID,
        "local_service_id": local_id,
        "windows_service_name": f"WinSvc-{local_id}",
        "display_name": display_name,
    }


def step(
    *,
    step_id: str,
    service_id: str,
    local_id: str,
    status: str,
    level: int,
    message: str,
    created_at: str,
    started_at: str | None,
    finished_at: str | None,
    updated_at: str,
    root_cause_step_id: str | None = None,
    dependency_chain: list[str] | None = None,
    probe_attempts: list[dict] | None = None,
) -> dict:
    return {
        "step_id": step_id,
        "managed_service_id": service_id,
        "agent_id": AGENT_ID,
        "local_service_id": local_id,
        "status": status,
        "topology_level": level,
        "dispatch_idempotency_key": None,
        "operation_id": None,
        "probe_attempts": probe_attempts or [],
        "warnings": [],
        "root_cause_step_id": root_cause_step_id,
        "dependency_chain": dependency_chain or [],
        "message": message,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "updated_at": updated_at,
    }


def explanatory_run() -> dict:
    failed_attempt = {
        "attempt": 1,
        "started_at": "2026-07-16T08:00:02.100Z",
        "finished_at": "2026-07-16T08:00:02.200Z",
        "result": {
            "passed": False,
            "observed_at": "2026-07-16T08:00:02.150Z",
            "latency_ms": 100,
            "code": "TCP_REFUSED",
            "message": "<script>probe()</script>",
        },
    }
    ready_attempt = {
        "attempt": 1,
        "started_at": "2026-07-16T08:00:05.100Z",
        "finished_at": "2026-07-16T08:00:05.120Z",
        "result": {
            "passed": True,
            "observed_at": "2026-07-16T08:00:05.110Z",
            "latency_ms": 20,
            "code": "HTTP_200",
            "message": "ready",
        },
    }
    return {
        "run_id": RUN_ID,
        "group_id": GROUP_ID,
        "trigger": "AUTO",
        "epoch": "a" * 64,
        "retry_of_run_id": None,
        "status": "UNKNOWN",
        "reason": "<script>reason()</script>",
        "members_snapshot": [
            member(MYSQL_SERVICE_ID, "mysql", "MySQL Primary"),
            member(REDIS_SERVICE_ID, "redis", "Redis Cache"),
            member(NACOS_SERVICE_ID, "nacos", "Nacos Registry"),
            member(NGINX_SERVICE_ID, "nginx", "Nginx <script>name()</script>"),
        ],
        "dependencies_snapshot": [
            {
                "managed_service_id": NACOS_SERVICE_ID,
                "prerequisite_managed_service_id": MYSQL_SERVICE_ID,
            },
            {
                "managed_service_id": NGINX_SERVICE_ID,
                "prerequisite_managed_service_id": NACOS_SERVICE_ID,
            },
        ],
        "probes_snapshot": [
            {
                "probe_id": "40000000-0000-4000-8000-000000000001",
                "group_id": GROUP_ID,
                "managed_service_id": MYSQL_SERVICE_ID,
                "definition": {
                    "kind": "tcp",
                    "host": "127.0.0.1",
                    "port": 3306,
                    "timeout_seconds": 2.0,
                    "interval_seconds": 3,
                    "deadline_seconds": 60,
                },
                "created_at": "2026-07-16T07:00:00.000Z",
                "updated_at": "2026-07-16T07:30:00.000Z",
            },
            {
                "probe_id": "40000000-0000-4000-8000-000000000002",
                "group_id": GROUP_ID,
                "managed_service_id": NGINX_SERVICE_ID,
                "definition": {
                    "kind": "http",
                    "url": "http://127.0.0.1/health?mode=<script>snapshot()</script>",
                    "expected_status": 200,
                    "body_contains": "ready",
                    "timeout_seconds": 2.0,
                    "interval_seconds": 3,
                    "deadline_seconds": 60,
                },
                "created_at": "2026-07-16T07:00:00.000Z",
                "updated_at": "2026-07-16T07:30:00.000Z",
            },
        ],
        "steps": [
            step(
                step_id=FAILED_STEP_ID,
                service_id=MYSQL_SERVICE_ID,
                local_id="mysql",
                status="FAILED",
                level=0,
                message="<img src=x onerror=alert(1)>",
                created_at="2026-07-16T08:00:01.000Z",
                started_at="2026-07-16T08:00:02.000Z",
                finished_at="2026-07-16T08:00:03.000Z",
                updated_at="2026-07-16T08:00:03.010Z",
                probe_attempts=[failed_attempt],
            ),
            step(
                step_id=UNKNOWN_STEP_ID,
                service_id=REDIS_SERVICE_ID,
                local_id="redis",
                status="UNKNOWN",
                level=0,
                message="Operation result could not be confirmed",
                created_at="2026-07-16T08:00:01.100Z",
                started_at="2026-07-16T08:00:02.100Z",
                finished_at=None,
                updated_at="2026-07-16T08:00:04.000Z",
            ),
            step(
                step_id=BLOCKED_STEP_ID,
                service_id=NACOS_SERVICE_ID,
                local_id="nacos",
                status="BLOCKED",
                level=1,
                message="blocked by prerequisite",
                created_at="2026-07-16T08:00:01.200Z",
                started_at=None,
                finished_at="2026-07-16T08:00:04.100Z",
                updated_at="2026-07-16T08:00:04.100Z",
                root_cause_step_id=FAILED_STEP_ID,
                dependency_chain=[FAILED_STEP_ID, UNKNOWN_STEP_ID],
            ),
            step(
                step_id=READY_STEP_ID,
                service_id=NGINX_SERVICE_ID,
                local_id="nginx",
                status="READY",
                level=2,
                message="ready",
                created_at="2026-07-16T08:00:01.300Z",
                started_at="2026-07-16T08:00:05.000Z",
                finished_at="2026-07-16T08:00:05.200Z",
                updated_at="2026-07-16T08:00:05.200Z",
                probe_attempts=[ready_attempt],
            ),
        ],
        "failure_code": "PROBE_FAILED",
        "failure_message": "<b>run failed</b>",
        "created_at": "2026-07-16T08:00:00.000Z",
        "started_at": "2026-07-16T08:00:01.000Z",
        "finished_at": None,
        "updated_at": "2026-07-16T08:00:05.500Z",
    }


def article(html: str, step_id: str) -> str:
    match = re.search(
        rf'<article id="step-{re.escape(step_id)}".*?</article>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def test_run_detail_explains_all_terminal_steps_and_real_timestamps(tmp_path: Path) -> None:
    app = create_app(
        settings(tmp_path),
        store=RunDetailStore(explanatory_run()),
        agent_client=object(),
        start_background=False,
    )
    with TestClient(app) as client:
        login = client.post(
            "/login",
            data={"username": "admin", "password": "secret-password"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        response = client.get(f"/runs/{RUN_ID}")

    assert response.status_code == 200
    html = response.text

    # Run timestamps are exact persisted evidence; the missing finished time is
    # not synthesized from updated_at or the current clock.
    for timestamp in (
        "2026-07-16T08:00:00.000Z",
        "2026-07-16T08:00:01.000Z",
        "2026-07-16T08:00:05.500Z",
    ):
        assert f'<time datetime="{timestamp}">{timestamp}</time>' in html
    for label in ("Created", "Started", "Finished", "Updated"):
        assert label in html
    assert html.count("未发生") == 3

    # The Run page renders the immutable execution inputs, including readable
    # dependency edges and an explicit SCM fallback for members without a
    # saved probe at Run creation time.
    assert "冻结执行快照" in html
    assert "冻结依赖" in html
    assert "冻结 Readiness" in html
    assert "Nacos Registry" in html and "MySQL Primary" in html
    assert "Nginx" in html and "Nacos Registry" in html
    assert "TCP" in html and "127.0.0.1:3306" in html
    assert "HTTP" in html
    assert "SCM fallback" in html
    assert "Redis Cache" in html

    failed = article(html, FAILED_STEP_ID)
    unknown = article(html, UNKNOWN_STEP_ID)
    blocked = article(html, BLOCKED_STEP_ID)
    ready = article(html, READY_STEP_ID)
    for fragment, status, service in (
        (failed, "FAILED", "MySQL Primary"),
        (unknown, "UNKNOWN", "Redis Cache"),
        (blocked, "BLOCKED", "Nacos Registry"),
        (ready, "READY", "Nginx"),
    ):
        assert status in fragment
        assert service in fragment
        assert "Step 时间" in fragment

    # FAILED and UNKNOWN identify themselves as the root; BLOCKED resolves its
    # persisted root ID to a readable, clickable service/status reference.
    assert f'href="#step-{FAILED_STEP_ID}"' in failed
    assert "本步骤 · MySQL Primary" in failed
    assert f'href="#step-{UNKNOWN_STEP_ID}"' in unknown
    assert "本步骤 · Redis Cache" in unknown
    assert f'href="#step-{FAILED_STEP_ID}"' in blocked
    assert "MySQL Primary" in blocked
    assert "(mysql)" in blocked
    assert "FAILED" in blocked
    assert 'class="cause-link"' not in ready
    assert "Operation result could not be confirmed" in unknown
    assert "blocked by prerequisite" in blocked
    assert "未发生" in unknown and "未发生" in blocked

    chain = re.search(
        r'<ol class="dependency-chain">(.*?)</ol>', blocked, flags=re.DOTALL
    )
    assert chain is not None
    chain_html = chain.group(1)
    assert chain_html.index(f'href="#step-{FAILED_STEP_ID}"') < chain_html.index(
        f'href="#step-{UNKNOWN_STEP_ID}"'
    )
    assert "MySQL Primary" in chain_html and "Redis Cache" in chain_html
    assert "FAILED" in chain_html and "UNKNOWN" in chain_html

    # Probe evidence exposes request start/end, Agent observation, result code,
    # latency and message rather than only a summarized status.
    for timestamp in (
        "2026-07-16T08:00:02.100Z",
        "2026-07-16T08:00:02.200Z",
        "2026-07-16T08:00:02.150Z",
    ):
        assert timestamp in failed
    assert "TCP_REFUSED" in failed
    assert "100 ms" in failed
    assert "Probe attempts (1)" in failed
    for timestamp in (
        "2026-07-16T08:00:05.100Z",
        "2026-07-16T08:00:05.120Z",
        "2026-07-16T08:00:05.110Z",
    ):
        assert timestamp in ready
    assert "HTTP_200" in ready
    assert "20 ms" in ready

    # Jinja autoescaping applies to persisted run, member, step and probe text.
    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "<b>run failed</b>" not in html
    assert "<script>snapshot()" not in html
    assert "&lt;script&gt;reason()&lt;/script&gt;" in html
    assert "&lt;script&gt;name()&lt;/script&gt;" in html
    assert "&lt;script&gt;probe()&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&lt;b&gt;run failed&lt;/b&gt;" in html
    assert "&lt;script&gt;snapshot()&lt;/script&gt;" in html
