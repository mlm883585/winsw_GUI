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


def test_group_page_explains_persisted_blocked_preconditions_and_escapes_html(
    tmp_path: Path,
) -> None:
    settings = ControlPlaneConfig(
        listen_host="127.0.0.1",
        database_path=tmp_path / "cp.sqlite3",
        cluster_token="group-ui-cluster-token-with-32-bytes-0001",
        agent_source_cidrs=["127.0.0.1/32"],
        admin_username="admin",
        admin_password_hash=hash_password("secret-password"),
        session_secret="group-ui-session-secret-with-32-bytes-0002",
    )
    store = ControlPlaneStore(settings.database_path)
    group = store.create_group({"name": "cold-start", "description": "database & cache"})
    service_id = str(uuid4())
    reasons = [
        {
            "code": "STARTUP_NOT_MANUAL",
            "message": "<script>alert('unsafe')</script>",
            "managed_service_id": service_id,
        }
    ]
    with store.db.transaction() as connection:
        connection.execute(
            """
            UPDATE recovery_groups
            SET state='BLOCKED_PRECONDITION', blocked_reasons_json=?
            WHERE group_id=?
            """,
            (json.dumps(reasons), group["group_id"]),
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
        html = client.get(f"/groups?group={group['group_id']}").text

    assert 'id="blocked-reasons-title"' in html
    assert "STARTUP_NOT_MANUAL" in html
    assert service_id in html
    assert "必须先 disarm" in html
    assert "<script>" not in html
    assert re.search(r"&lt;script&gt;alert\(.+unsafe.+&lt;/script&gt;", html)

    create_form = re.search(r'<form id="create-group-form".*?</form>', html, re.DOTALL)
    edit_form = re.search(r'<form id="edit-group-form".*?</form>', html, re.DOTALL)
    assert create_form is not None
    assert edit_form is not None
    assert re.search(r'name="name"[^>]*maxlength="128"', create_form.group())
    assert re.search(r'name="description"[^>]*maxlength="1024"', create_form.group())
    assert re.search(r'name="name"[^>]*maxlength="128"', edit_form.group())
    assert re.search(r'name="description"[^>]*maxlength="1024"', edit_form.group())
    assert 'value="cold-start"' in edit_form.group()
    assert 'value="database &amp; cache"' in edit_form.group()

    javascript = (
        Path(__file__).resolve().parents[1]
        / "orchestrator"
        / "control_plane"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    assert 'getElementById("edit-group-form")' in javascript
    assert 'method: "PATCH"' in javascript
    assert "event.currentTarget.dataset.groupId" in javascript
