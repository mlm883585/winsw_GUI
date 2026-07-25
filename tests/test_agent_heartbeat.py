from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from uuid import uuid4

from orchestrator.agent.heartbeat import HeartbeatReporter
from orchestrator.agent.identity import AgentIdentity
from orchestrator.common.models import ObservedService


def observed_service() -> ObservedService:
    return ObservedService(
        local_service_id="mysql",
        windows_service_name="MySQL80",
        display_name="MySQL 8",
        installation_state="INSTALLED",
        runtime_state="INACTIVE",
        startup_state="AUTOSTART_DISABLED",
        last_observed_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )


class RecordingIngress:
    def __init__(self) -> None:
        self.registered = []
        self.heartbeats = []
        self.enough = asyncio.Event()

    async def register(self, report) -> bool:
        self.registered.append(report)
        return True

    async def heartbeat(self, report) -> bool:
        self.heartbeats.append(report)
        self.enough.set()
        return True


def test_reporter_registers_then_heartbeats_with_monotonic_sequence() -> None:
    async def scenario() -> None:
        ingress = RecordingIngress()
        identity = AgentIdentity(uuid4(), uuid4(), uuid4(), 9)

        async def observe():
            return [observed_service()]

        reporter = HeartbeatReporter(
            ingress=ingress,
            identity=identity,
            version="0.1.0",
            endpoint="http://127.0.0.1:8765",
            hostname="APP-SRV-01",
            observe_services=observe,
            interval_seconds=0.01,
            jitter_ratio=0,
            backoff_initial_seconds=0.01,
            backoff_max_seconds=0.02,
            random_source=random.Random(1),
        )
        await reporter.start()
        await asyncio.wait_for(ingress.enough.wait(), timeout=1)
        await reporter.stop()

        reports = ingress.registered + ingress.heartbeats
        assert [item.sequence for item in reports] == list(range(1, len(reports) + 1))
        assert len(ingress.registered) == 1
        assert reports[0].agent_id == identity.agent_id
        assert reports[0].boot_id == identity.boot_id
        assert reports[0].agent_instance_id == identity.agent_instance_id
        assert reports[0].instance_generation == 9

    asyncio.run(scenario())


def test_failed_delivery_uses_registration_again_instead_of_heartbeat() -> None:
    class FlakyIngress:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.enough = asyncio.Event()

        async def register(self, _report) -> bool:
            self.calls.append("register")
            if len(self.calls) >= 3:
                self.enough.set()
            return len(self.calls) > 1

        async def heartbeat(self, _report) -> bool:
            self.calls.append("heartbeat")
            self.enough.set()
            return True

    async def scenario() -> None:
        ingress = FlakyIngress()

        async def observe():
            return [observed_service()]

        reporter = HeartbeatReporter(
            ingress=ingress,
            identity=AgentIdentity(uuid4(), uuid4(), uuid4(), 1),
            version="0.1.0",
            endpoint="http://127.0.0.1:8765",
            hostname="APP-SRV-01",
            observe_services=observe,
            interval_seconds=0.01,
            jitter_ratio=0,
            backoff_initial_seconds=0.01,
            backoff_max_seconds=0.02,
        )
        await reporter.start()
        await asyncio.wait_for(ingress.enough.wait(), timeout=1)
        await reporter.stop()
        assert ingress.calls[:3] == ["register", "register", "heartbeat"]

    asyncio.run(scenario())


def test_unexpected_observe_and_ingress_errors_are_redacted_and_recovered(
    caplog,
) -> None:
    canary = "CANARY-HEARTBEAT-SECRET-MUST-NOT-LEAK"

    class RecoveringIngress:
        def __init__(self) -> None:
            self.calls = 0
            self.reports = []
            self.recovered = asyncio.Event()

        async def register(self, report) -> bool:
            self.calls += 1
            self.reports.append(report)
            if self.calls == 1:
                raise RuntimeError(canary)
            self.recovered.set()
            return True

        async def heartbeat(self, _report) -> bool:
            raise AssertionError("registration must recover before heartbeat")

    async def scenario() -> None:
        ingress = RecoveringIngress()
        observe_calls = 0

        async def observe():
            nonlocal observe_calls
            observe_calls += 1
            if observe_calls == 1:
                raise RuntimeError(canary)
            return [observed_service()]

        reporter = HeartbeatReporter(
            ingress=ingress,
            identity=AgentIdentity(uuid4(), uuid4(), uuid4(), 1),
            version="0.1.0",
            endpoint="http://127.0.0.1:8765",
            hostname="APP-SRV-01",
            observe_services=observe,
            interval_seconds=0.01,
            jitter_ratio=0,
            backoff_initial_seconds=0.001,
            backoff_max_seconds=0.002,
        )
        await reporter.start()
        await asyncio.wait_for(ingress.recovered.wait(), timeout=1)
        assert reporter._task is not None and not reporter._task.done()
        await reporter.stop()

        assert observe_calls >= 3
        assert ingress.calls == 2
        assert [item.sequence for item in ingress.reports] == [1, 2]

    with caplog.at_level(logging.WARNING, logger="orchestrator.agent.heartbeat"):
        asyncio.run(scenario())

    assert [record.message for record in caplog.records] == [
        "AGENT_HEARTBEAT_OBSERVE_FAILED",
        "AGENT_HEARTBEAT_INGRESS_FAILED",
    ]
    assert canary not in caplog.text
