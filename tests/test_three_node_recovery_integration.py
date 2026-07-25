from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.common.enums import (
    OperationStatus,
    RecoveryRunStatus,
    RecoveryStepStatus,
)
from orchestrator.common.models import AgentReport, ObservedService, ProbeResult
from orchestrator.common.security import canonical_request_fingerprint
from orchestrator.control_plane.recovery import RecoveryEngine
from orchestrator.control_plane.store import ControlPlaneStore


class TrackingAgentClient:
    """Controlled Agent transport that exposes orchestration order and concurrency."""

    def __init__(
        self,
        now: datetime,
        services: dict[str, dict[str, Any]],
        *,
        synchronize_root_probes: bool = False,
        failed_starts: set[str] | None = None,
    ) -> None:
        self.now = now
        self.services = services
        self.synchronize_root_probes = synchronize_root_probes
        self.failed_starts = failed_starts or set()
        self.events: list[str] = []
        self.action_calls: list[str] = []
        self.probe_calls: list[str] = []
        self.operations: dict[str, dict[str, Any]] = {}
        self.root_probe_entries: set[str] = set()
        self.root_probe_gate = asyncio.Event()
        self.root_probes_were_concurrent = True

    async def start_action(
        self,
        endpoint: str,
        local_service_id: str,
        action: str,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        del endpoint
        self.action_calls.append(local_service_id)
        self.events.append(f"start:{local_service_id}:enter")
        await asyncio.sleep(0)
        status = (
            OperationStatus.FAILED.value
            if local_service_id in self.failed_starts
            else OperationStatus.SUCCEEDED.value
        )
        service = self.services[local_service_id]
        operation = {
            "operation_id": str(uuid4()),
            "agent_id": service["agent_id"],
            "local_service_id": local_service_id,
            "windows_service_name": service["windows_service_name"],
            "action": action,
            "status": status,
            "idempotency_key": str(idempotency_key),
            "request_fingerprint": canonical_request_fingerprint(
                "POST",
                f"/api/v1/services/{local_service_id}/actions/{action}",
                {},
            ),
            "created_at": self.now,
            "updated_at": self.now,
            "started_at": self.now,
            "finished_at": self.now,
            "error_code": "SCM_ACTION_FAILED" if status == "FAILED" else None,
            "message": "controlled start failure" if status == "FAILED" else None,
        }
        self.operations[operation["operation_id"]] = operation
        self.events.append(f"start:{local_service_id}:{status.lower()}")
        return operation

    async def get_operation(
        self, endpoint: str, operation_id: Any
    ) -> dict[str, Any]:
        del endpoint
        return self.operations[str(operation_id)]

    async def probe(self, endpoint: str, request: Any) -> ProbeResult:
        del endpoint
        local_service_id = str(request.local_service_id)
        self.probe_calls.append(local_service_id)
        self.events.append(f"probe:{local_service_id}:enter")

        if self.synchronize_root_probes and local_service_id in {"mysql", "redis"}:
            self.root_probe_entries.add(local_service_id)
            if self.root_probe_entries == {"mysql", "redis"}:
                self.root_probe_gate.set()
            try:
                await asyncio.wait_for(self.root_probe_gate.wait(), timeout=0.25)
            except TimeoutError:
                # Let the Run finish so the assertion reports a useful failure
                # instead of leaving an integration test deadlocked.
                self.root_probes_were_concurrent = False
                self.root_probe_gate.set()

        self.events.append(f"probe:{local_service_id}:ready")
        return ProbeResult(
            passed=True,
            observed_at=self.now,
            latency_ms=1,
            code="READY",
            message="controlled readiness success",
        )


class ThreeNodeRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)

    def _register_agent(
        self,
        store: ControlPlaneStore,
        *,
        hostname: str,
        peer_ip: str,
        services: list[tuple[str, str, str]],
    ) -> None:
        store.register_agent(
            AgentReport(
                agent_id=uuid4(),
                boot_id=uuid4(),
                agent_instance_id=uuid4(),
                instance_generation=1,
                sequence=1,
                version="integration-test",
                endpoint=f"http://{peer_ip}:8765",
                hostname=hostname,
                services=[
                    ObservedService(
                        local_service_id=local_service_id,
                        windows_service_name=windows_service_name,
                        display_name=local_service_id.title(),
                        installation_state="INSTALLED",
                        runtime_state=runtime_state,
                        startup_state="AUTOSTART_DISABLED",
                        last_observed_at=self.now,
                    )
                    for local_service_id, windows_service_name, runtime_state in services
                ],
            ),
            peer_ip,
            now=self.now,
        )

    def _build_three_node_run(
        self, database_path: Path
    ) -> tuple[ControlPlaneStore, dict[str, Any], dict[str, dict[str, Any]]]:
        store = ControlPlaneStore(database_path, now=lambda: self.now)
        self._register_agent(
            store,
            hostname="node-a",
            peer_ip="10.0.0.11",
            services=[
                ("mysql", "MySQL80", "ACTIVE"),
                ("redis", "Redis", "INACTIVE"),
            ],
        )
        self._register_agent(
            store,
            hostname="node-b",
            peer_ip="10.0.0.12",
            services=[
                ("nacos", "Nacos", "INACTIVE"),
                ("java", "BusinessJava", "INACTIVE"),
            ],
        )
        self._register_agent(
            store,
            hostname="node-c",
            peer_ip="10.0.0.13",
            services=[("nginx", "Nginx", "INACTIVE")],
        )

        services = {item["local_service_id"]: item for item in store.list_services(now=self.now)}
        group = store.create_group(
            {
                "name": "three-node-cold-start",
                "description": "MySQL + Redis -> Nacos -> Java -> Nginx",
                "node_settle_window_seconds": 120,
                "max_parallel_services": 4,
            }
        )
        store.replace_members(
            group["group_id"],
            [item["managed_service_id"] for item in services.values()],
        )
        store.replace_dependencies(
            group["group_id"],
            [
                (
                    services["nacos"]["managed_service_id"],
                    services["mysql"]["managed_service_id"],
                ),
                (
                    services["nacos"]["managed_service_id"],
                    services["redis"]["managed_service_id"],
                ),
                (
                    services["java"]["managed_service_id"],
                    services["nacos"]["managed_service_id"],
                ),
                (
                    services["nginx"]["managed_service_id"],
                    services["java"]["managed_service_id"],
                ),
            ],
        )
        for service in services.values():
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
        store.arm_group(group["group_id"], now=self.now)
        run = store.create_run_from_group(
            group["group_id"], "MANUAL", reason="three-node integration", now=self.now
        )
        return store, run, services

    @staticmethod
    def _step_statuses(run: dict[str, Any]) -> dict[str, str]:
        return {step["local_service_id"]: step["status"] for step in run["steps"]}

    async def test_three_node_dag_waits_for_readiness_and_finishes_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, run, services = self._build_three_node_run(Path(directory) / "cp.sqlite3")
            client = TrackingAgentClient(
                self.now, services, synchronize_root_probes=True
            )
            engine = RecoveryEngine(store, client, now=lambda: self.now)

            result = await engine.execute_run(run["run_id"])

            self.assertEqual(RecoveryRunStatus.SUCCEEDED.value, result["status"])
            self.assertEqual(
                {
                    local_id: RecoveryStepStatus.READY.value
                    for local_id in {"mysql", "redis", "nacos", "java", "nginx"}
                },
                self._step_statuses(result),
            )
            self.assertNotIn("mysql", client.action_calls, "ACTIVE MySQL must not be started")
            self.assertEqual(["redis", "nacos", "java", "nginx"], client.action_calls)
            self.assertTrue(
                client.root_probes_were_concurrent,
                "same-level MySQL and Redis readiness probes must overlap",
            )

            for root in ("mysql", "redis"):
                self.assertLess(
                    client.events.index(f"probe:{root}:ready"),
                    client.events.index("start:nacos:enter"),
                )
            self.assertLess(
                client.events.index("probe:nacos:ready"),
                client.events.index("start:java:enter"),
            )
            self.assertLess(
                client.events.index("probe:java:ready"),
                client.events.index("start:nginx:enter"),
            )

            levels = {
                step["local_service_id"]: step["topology_level"] for step in result["steps"]
            }
            self.assertEqual(
                {"mysql": 0, "redis": 0, "nacos": 1, "java": 2, "nginx": 3},
                levels,
            )

    async def test_failed_root_blocks_every_strict_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, run, services = self._build_three_node_run(Path(directory) / "cp.sqlite3")
            client = TrackingAgentClient(
                self.now, services, failed_starts={"redis"}
            )
            engine = RecoveryEngine(store, client, now=lambda: self.now)

            result = await engine.execute_run(run["run_id"])

            self.assertEqual(RecoveryRunStatus.FAILED.value, result["status"])
            self.assertEqual(
                {
                    "mysql": RecoveryStepStatus.READY.value,
                    "redis": RecoveryStepStatus.FAILED.value,
                    "nacos": RecoveryStepStatus.BLOCKED.value,
                    "java": RecoveryStepStatus.BLOCKED.value,
                    "nginx": RecoveryStepStatus.BLOCKED.value,
                },
                self._step_statuses(result),
            )
            self.assertEqual(["redis"], client.action_calls)
            self.assertNotIn("nacos", client.probe_calls)
            self.assertNotIn("java", client.probe_calls)
            self.assertNotIn("nginx", client.probe_calls)


if __name__ == "__main__":
    unittest.main()
