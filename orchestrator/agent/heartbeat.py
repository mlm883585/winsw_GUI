from __future__ import annotations

import asyncio
import logging
import random
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Protocol

from orchestrator.agent.identity import AgentIdentity
from orchestrator.common.models import AgentReport, HeartbeatAck, ObservedService


LOGGER = logging.getLogger(__name__)


class ControlPlaneIngress(Protocol):
    async def register(self, report: AgentReport) -> bool: ...

    async def heartbeat(self, report: AgentReport) -> bool: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpControlPlaneIngress:
    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    async def register(self, report: AgentReport) -> bool:
        return await asyncio.to_thread(
            self._post,
            "/api/v1/agents/register",
            report,
        )

    async def heartbeat(self, report: AgentReport) -> bool:
        return await asyncio.to_thread(
            self._post,
            f"/api/v1/agents/{report.agent_id}/heartbeat",
            report,
        )

    def _post(self, path: str, report: AgentReport) -> bool:
        payload = report.model_dump_json().encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    return False
                raw = response.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    return False
                HeartbeatAck.model_validate_json(raw)
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            return False


class HeartbeatReporter:
    def __init__(
        self,
        *,
        ingress: ControlPlaneIngress,
        identity: AgentIdentity,
        version: str,
        endpoint: str,
        hostname: str,
        observe_services: Callable[[], Awaitable[list[ObservedService]]],
        interval_seconds: float = 10.0,
        jitter_ratio: float = 0.2,
        backoff_initial_seconds: float = 2.0,
        backoff_max_seconds: float = 60.0,
        random_source: random.Random | None = None,
    ) -> None:
        self.ingress = ingress
        self.identity = identity
        self.version = version
        self.endpoint = endpoint
        self.hostname = hostname
        self.observe_services = observe_services
        self.interval_seconds = interval_seconds
        self.jitter_ratio = jitter_ratio
        self.backoff_initial_seconds = backoff_initial_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.random = random_source or random.Random()
        self._sequence = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        # Do not reuse a loop-bound Event if an ASGI server restarts lifespan.
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="agent-heartbeat")

    async def stop(self) -> None:
        task, self._task = self._task, None
        self._stop.set()
        if task is not None:
            await task

    async def _report(self) -> AgentReport:
        services = await self.observe_services()
        self._sequence += 1
        return AgentReport(
            agent_id=self.identity.agent_id,
            boot_id=self.identity.boot_id,
            agent_instance_id=self.identity.agent_instance_id,
            instance_generation=self.identity.instance_generation,
            sequence=self._sequence,
            version=self.version,
            endpoint=self.endpoint,
            hostname=self.hostname,
            services=services,
        )

    async def _run(self) -> None:
        registered = False
        backoff = self.backoff_initial_seconds
        while not self._stop.is_set():
            try:
                report = await self._report()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("AGENT_HEARTBEAT_OBSERVE_FAILED")
                accepted = False
            else:
                try:
                    accepted = (
                        await self.ingress.heartbeat(report)
                        if registered
                        else await self.ingress.register(report)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.warning("AGENT_HEARTBEAT_INGRESS_FAILED")
                    accepted = False
            if accepted:
                registered = True
                backoff = self.backoff_initial_seconds
                spread = self.interval_seconds * self.jitter_ratio
                delay = self.random.uniform(
                    self.interval_seconds - spread,
                    self.interval_seconds + spread,
                )
            else:
                registered = False
                delay = backoff
                backoff = min(self.backoff_max_seconds, backoff * 2)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
