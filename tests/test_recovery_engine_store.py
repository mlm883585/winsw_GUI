from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.common.enums import RecoveryRunStatus, RecoveryStepStatus
from orchestrator.common.models import AgentReport, ObservedService, ProbeResult
from orchestrator.control_plane.recovery import RecoveryEngine
from orchestrator.control_plane.store import ControlPlaneStore


class ImmediateAgentClient:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.action_calls: list[tuple[Any, ...]] = []

    async def start_action(self, *args: Any) -> dict[str, Any]:
        self.action_calls.append(args)
        return {"operation_id": str(uuid4()), "status": "SUCCEEDED", "message": None}

    async def get_operation(self, endpoint: str, operation_id: Any) -> dict[str, Any]:
        return {"operation_id": str(operation_id), "status": "SUCCEEDED", "message": None}

    async def probe(self, endpoint: str, request: Any) -> ProbeResult:
        return ProbeResult(
            passed=True,
            observed_at=self.now,
            latency_ms=1,
            code="READY",
            message="ready",
        )


class RecoveryEngineSQLiteIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_store_persists_attempt_and_releases_lock(self) -> None:
        now = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = ControlPlaneStore(
                Path(directory) / "cp.sqlite3", now=lambda: now
            )
            agent_id = uuid4()
            report = AgentReport(
                agent_id=agent_id,
                boot_id=uuid4(),
                agent_instance_id=uuid4(),
                instance_generation=1,
                sequence=1,
                version="test",
                endpoint="http://127.0.0.1:8765",
                hostname="node-a",
                services=[
                    ObservedService(
                        local_service_id="mysql",
                        windows_service_name="MySQL80",
                        display_name="MySQL",
                        installation_state="INSTALLED",
                        runtime_state="ACTIVE",
                        startup_state="AUTOSTART_DISABLED",
                        last_observed_at=now,
                    )
                ],
            )
            store.register_agent(report, "127.0.0.1", now=now)
            service = store.list_services(now=now)[0]
            group = store.create_group(
                {
                    "name": "database",
                    "description": "",
                    "node_settle_window_seconds": 120,
                    "max_parallel_services": 1,
                }
            )
            store.replace_members(group["group_id"], [service["managed_service_id"]])
            store.put_probe(
                group["group_id"],
                service["managed_service_id"],
                {
                    "kind": "scm",
                    "timeout_seconds": 1,
                    "interval_seconds": 1,
                    "deadline_seconds": 5,
                },
            )
            store.arm_group(group["group_id"], now=now)
            run = store.create_run_from_group(
                group["group_id"], "MANUAL", reason="integration", now=now
            )
            client = ImmediateAgentClient(now)
            engine = RecoveryEngine(store, client, now=lambda: now)

            result = await engine.execute_run(run["run_id"])

            self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, result["status"])
            self.assertEqual(RecoveryStepStatus.READY.value, result["steps"][0]["status"])
            self.assertEqual(1, len(result["steps"][0]["probe_attempts"]))
            self.assertIsNone(store.service_lock(service["managed_service_id"]))
            self.assertEqual([], client.action_calls, "ACTIVE service must only be probed")


if __name__ == "__main__":
    unittest.main()
