from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from orchestrator.common.security import hash_password
from orchestrator.control_plane.app import create_app
from orchestrator.control_plane.config import ControlPlaneConfig
from orchestrator.control_plane.store import ControlPlaneStore


class UnusedAgentClient:
    pass


def _definitions_from_page(html: str) -> dict[str, dict]:
    match = re.search(
        r'<script id="probe-definitions" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    probes = json.loads(match.group(1))
    return {item["managed_service_id"]: item["definition"] for item in probes}


def test_group_page_binds_saved_readiness_to_each_service_and_exposes_fallback_delete(
    tmp_path: Path,
) -> None:
    settings = ControlPlaneConfig(
        listen_host="127.0.0.1",
        database_path=tmp_path / "cp.sqlite3",
        cluster_token="readiness-ui-cluster-token-with-32-bytes-001",
        agent_source_cidrs=["127.0.0.1/32"],
        admin_username="admin",
        admin_password_hash=hash_password("secret-password"),
        session_secret="readiness-ui-session-secret-with-32-bytes-002",
    )
    store = ControlPlaneStore(settings.database_path)
    agent_id = str(uuid4())
    store.register_agent(
        {
            "agent_id": agent_id,
            "boot_id": str(uuid4()),
            "agent_instance_id": str(uuid4()),
            "instance_generation": 1,
            "sequence": 1,
            "version": "0.1.0",
            "endpoint": "http://127.0.0.1:8765",
            "hostname": "readiness-host",
            "services": [
                {
                    "local_service_id": "mysql",
                    "windows_service_name": "MySQL80",
                    "display_name": "MySQL 8",
                    "installation_state": "INSTALLED",
                    "runtime_state": "INACTIVE",
                    "startup_state": "AUTOSTART_DISABLED",
                    "last_observed_at": "2026-07-17T00:00:00Z",
                },
                {
                    "local_service_id": "nacos",
                    "windows_service_name": "Nacos",
                    "display_name": "Nacos",
                    "installation_state": "INSTALLED",
                    "runtime_state": "INACTIVE",
                    "startup_state": "AUTOSTART_DISABLED",
                    "last_observed_at": "2026-07-17T00:00:00Z",
                },
            ],
        },
        "127.0.0.1",
    )
    services = {item["local_service_id"]: item for item in store.list_services()}
    mysql_id = services["mysql"]["managed_service_id"]
    nacos_id = services["nacos"]["managed_service_id"]
    group = store.create_group({"name": "cold-start"})
    store.replace_members(group["group_id"], [mysql_id, nacos_id])
    store.replace_dependencies(
        group["group_id"],
        [
            {
                "managed_service_id": nacos_id,
                "prerequisite_managed_service_id": mysql_id,
            }
        ],
    )
    store.put_probe(
        group["group_id"],
        mysql_id,
        {
            "kind": "tcp",
            "host": "127.0.0.1",
            "port": 3306,
            "timeout_seconds": 2.0,
            "interval_seconds": 3,
            "deadline_seconds": 60,
        },
    )
    store.put_probe(
        group["group_id"],
        nacos_id,
        {
            "kind": "http",
            "url": "http://127.0.0.1:8848/health",
            "expected_status": 200,
            "body_contains": "</script><script>probe()</script>",
            "timeout_seconds": 2.0,
            "interval_seconds": 3,
            "deadline_seconds": 60,
        },
    )

    app = create_app(
        settings,
        store=store,
        agent_client=UnusedAgentClient(),  # type: ignore[arg-type]
        start_background=False,
    )
    with TestClient(app) as client:
        login = client.post(
            "/login",
            data={"username": "admin", "password": "secret-password"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        response = client.get(f"/groups?group={group['group_id']}")
        assert response.status_code == 200
        html = response.text

        definitions = _definitions_from_page(html)
        assert definitions[mysql_id]["kind"] == "tcp"
        assert definitions[mysql_id]["port"] == 3306
        assert definitions[nacos_id]["kind"] == "http"
        assert definitions[nacos_id]["body_contains"] == "</script><script>probe()</script>"
        assert "</script><script>probe()" not in html
        assert 'id="probe-service"' in html
        assert f'value="{mysql_id}"' in html
        assert f'value="{nacos_id}"' in html
        assert html.count(mysql_id) >= 3
        assert html.count(nacos_id) >= 3
        assert 'id="dependency-dependent"' in html
        assert 'id="dependency-prerequisite"' in html
        assert 'id="add-dependency-button"' in html
        assert 'id="dependency-definitions"' in html
        assert 'id="dependency-list"' in html
        dependency_match = re.search(
            r'<script id="dependency-definitions" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        assert dependency_match is not None
        assert json.loads(dependency_match.group(1)) == [
            {
                "managed_service_id": nacos_id,
                "prerequisite_managed_service_id": mysql_id,
            }
        ]
        assert "未保存显式探针；运行时使用 SCM fallback" in html
        assert 'id="delete-probe-button"' in html
        assert "删除已保存探针 / 恢复 SCM fallback" in html

        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html)
        assert csrf is not None
        deleted = client.delete(
            f"/api/v1/recovery-groups/{group['group_id']}/services/{mysql_id}/probe",
            headers={"X-CSRF-Token": csrf.group(1)},
        )
        assert deleted.status_code == 204
        refreshed = client.get(f"/groups?group={group['group_id']}")
        refreshed_definitions = _definitions_from_page(refreshed.text)
        assert mysql_id not in refreshed_definitions
        assert refreshed_definitions[nacos_id]["kind"] == "http"

    javascript = (
        Path(__file__).resolve().parents[1]
        / "orchestrator"
        / "control_plane"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    assert 'serviceSelect.addEventListener("change", renderProbe)' in javascript
    assert "definitionField.dataset.managedServiceId = serviceId" in javascript
    assert "definitionField.dataset.managedServiceId !== serviceId" in javascript
    assert 'method:"PUT"' in javascript
    assert 'method:"DELETE"' in javascript
    assert 'document.getElementById("add-dependency-button")' in javascript
    assert 'document.getElementById("dependency-list")' in javascript
    assert 'JSON.stringify({dependencies})' in javascript
    assert "dependency.managed_service_id" in javascript
    assert "dependency.prerequisite_managed_service_id" in javascript
    assert javascript.count("window.location.reload()") >= 2
