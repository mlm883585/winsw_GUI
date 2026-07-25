from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Path as PathParameter, Request, Response

from orchestrator import __version__
from orchestrator.agent.config import AgentConfig, AllowlistedService, load_agent_config
from orchestrator.agent.heartbeat import (
    ControlPlaneIngress,
    HeartbeatReporter,
    HttpControlPlaneIngress,
)
from orchestrator.agent.identity import AgentIdentity, BootMarkerProvider, WmiBootMarkerProvider
from orchestrator.agent.operations import OperationManager
from orchestrator.agent.probes import LocalAddressProvider, ProbeExecutor
from orchestrator.agent.scm import SCMBackend, PyWin32SCMBackend, observe_service
from orchestrator.agent.store import AgentStore
from orchestrator.common.enums import (
    InstallationState,
    RuntimeState,
    ServiceAction,
    StartupState,
)
from orchestrator.common.errors import ApiError, ErrorCode, install_error_handlers
from orchestrator.common.models import (
    AgentCapabilities,
    AgentInfo,
    CanonicalUUID4Input,
    EmptyActionRequest,
    HealthResponse,
    ObservedService,
    ObservedServiceCollection,
    Operation,
    ProbeRequest,
    ProbeResult,
    SERVICE_SLUG_PATTERN,
)
from orchestrator.common.security import (
    canonical_request_fingerprint,
    parse_networks,
    parse_uuid4,
    require_bearer_token,
    require_source_ip,
)
from orchestrator.common.time import utc_now


@dataclass(slots=True)
class AgentRuntime:
    config: AgentConfig
    identity: AgentIdentity
    hostname: str
    services: dict[str, AllowlistedService]
    backend: SCMBackend
    store: AgentStore
    operations: OperationManager
    probes: ProbeExecutor
    heartbeat: HeartbeatReporter

    async def observe_services(self) -> list[ObservedService]:
        async def observe_with_deadline(item: AllowlistedService) -> ObservedService:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(observe_service, item, self.backend),
                    timeout=self.config.action_timeout_seconds,
                )
            except TimeoutError:
                return ObservedService(
                    local_service_id=item.local_service_id,
                    windows_service_name=item.windows_service_name,
                    display_name=item.display_name or item.windows_service_name,
                    installation_state=InstallationState.UNKNOWN,
                    runtime_state=RuntimeState.UNKNOWN,
                    startup_state=StartupState.UNKNOWN,
                    last_observed_at=utc_now(),
                )

        return list(
            await asyncio.gather(
                *(observe_with_deadline(item) for item in self.services.values())
            )
        )


def build_runtime(
    config: AgentConfig,
    *,
    backend: SCMBackend | None = None,
    boot_provider: BootMarkerProvider | None = None,
    address_provider: LocalAddressProvider | None = None,
    ingress: ControlPlaneIngress | None = None,
) -> AgentRuntime:
    store = AgentStore(config.database_path)
    store.initialize()
    # A local service id is a durable identity used by the Control Plane.  Bind
    # it before WMI/SCM access and before any operation recovery can occur.
    store.bind_service_mappings(config.services)
    marker = (boot_provider or WmiBootMarkerProvider()).get_boot_marker()
    identity = store.initialize_identity(marker)
    scm_backend = backend or PyWin32SCMBackend()
    services = config.service_map
    operations = OperationManager(
        store=store,
        backend=scm_backend,
        services=services,
        action_timeout_seconds=config.action_timeout_seconds,
        poll_interval_seconds=config.action_poll_interval_seconds,
    )
    probes = ProbeExecutor(
        backend=scm_backend,
        services=services,
        address_provider=address_provider,
    )
    hostname = socket.gethostname()

    runtime_placeholder: AgentRuntime | None = None

    async def observe_all() -> list[ObservedService]:
        assert runtime_placeholder is not None
        return await runtime_placeholder.observe_services()

    heartbeat = HeartbeatReporter(
        ingress=ingress
        or HttpControlPlaneIngress(config.control_plane_url, config.cluster_token),
        identity=identity,
        version=__version__,
        endpoint=config.advertised_endpoint,
        hostname=hostname,
        observe_services=observe_all,
        interval_seconds=config.heartbeat_interval_seconds,
        jitter_ratio=config.heartbeat_jitter_ratio,
        backoff_initial_seconds=config.heartbeat_backoff_initial_seconds,
        backoff_max_seconds=config.heartbeat_backoff_max_seconds,
    )
    runtime_placeholder = AgentRuntime(
        config=config,
        identity=identity,
        hostname=hostname,
        services=services,
        backend=scm_backend,
        store=store,
        operations=operations,
        probes=probes,
        heartbeat=heartbeat,
    )
    return runtime_placeholder


def create_app(
    config: AgentConfig | str | Path,
    *,
    backend: SCMBackend | None = None,
    boot_provider: BootMarkerProvider | None = None,
    address_provider: LocalAddressProvider | None = None,
    ingress: ControlPlaneIngress | None = None,
) -> FastAPI:
    parsed_config = load_agent_config(config) if isinstance(config, (str, Path)) else config
    runtime = build_runtime(
        parsed_config,
        backend=backend,
        boot_provider=boot_provider,
        address_provider=address_provider,
        ingress=ingress,
    )
    networks = parse_networks(parsed_config.control_plane_source_cidrs)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.operations.start()
        await runtime.heartbeat.start()
        try:
            yield
        finally:
            await runtime.heartbeat.stop()
            await runtime.operations.stop()

    app = FastAPI(
        title="Windows Recovery MVP Agent API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    install_error_handlers(app)

    async def authorize(request: Request) -> None:
        require_bearer_token(request, parsed_config.cluster_token)
        require_source_ip(request, networks)

    async def require_supported_probe_kind(request: Request) -> None:
        """Preserve the frozen PROBE_UNSUPPORTED error before union validation.

        FastAPI otherwise converts an unknown discriminator into the generic
        VALIDATION_ERROR response.  Malformed JSON and non-object bodies remain
        normal request-validation failures.
        """

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict) or "kind" not in payload:
            return
        kind = payload["kind"]
        if not isinstance(kind, str):
            return
        if kind not in {"scm", "tcp", "http"}:
            raise ApiError(
                422,
                ErrorCode.PROBE_UNSUPPORTED,
                "Probe kind is not supported",
            )

    @app.get("/healthz", response_model=HealthResponse, tags=["Health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", component="agent", version=__version__)

    @app.get(
        "/api/v1/agent",
        response_model=AgentInfo,
        dependencies=[Depends(authorize)],
        tags=["Agent"],
    )
    async def agent_info() -> AgentInfo:
        return AgentInfo(
            agent_id=runtime.identity.agent_id,
            boot_id=runtime.identity.boot_id,
            agent_instance_id=runtime.identity.agent_instance_id,
            instance_generation=runtime.identity.instance_generation,
            hostname=runtime.hostname,
            endpoint=parsed_config.advertised_endpoint,
            version=__version__,
            capabilities=AgentCapabilities(
                service_actions=list(ServiceAction),
                probe_kinds=["scm", "tcp", "http"],
                arbitrary_command=False,
            ),
        )

    @app.get(
        "/api/v1/services",
        response_model=ObservedServiceCollection,
        dependencies=[Depends(authorize)],
        tags=["Services"],
    )
    async def list_services() -> ObservedServiceCollection:
        return ObservedServiceCollection(items=await runtime.observe_services())

    @app.post(
        "/api/v1/services/{local_service_id}/actions/{action}",
        response_model=Operation,
        status_code=202,
        dependencies=[Depends(authorize)],
        tags=["Operations"],
    )
    async def create_service_operation(
        local_service_id: Annotated[
            str,
            PathParameter(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64),
        ],
        action: ServiceAction,
        response: Response,
        request: Request,
        body: EmptyActionRequest = EmptyActionRequest(),
        idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Operation:
        raw_body = await request.body()
        if raw_body.strip() and await request.json() is None:
            # An explicit JSON null is not an omitted body and does not satisfy
            # the strict EmptyActionRequest object in the public contract.
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "Action body must be an empty JSON object when provided",
            )
        del body
        service = runtime.services.get(local_service_id)
        if service is None:
            raise ApiError(
                404,
                ErrorCode.SERVICE_NOT_ALLOWLISTED,
                "Service is not in the Agent allowlist",
            )
        idempotency_key = parse_uuid4(idempotency_key_header)
        canonical_path = f"/api/v1/services/{local_service_id}/actions/{action.value}"
        fingerprint = canonical_request_fingerprint("POST", canonical_path, {})
        operation = await runtime.operations.create_operation(
            agent_id=runtime.identity.agent_id,
            service=service,
            action=action,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        response.headers["Location"] = f"/api/v1/operations/{operation.operation_id}"
        return operation

    @app.get(
        "/api/v1/operations/{operation_id}",
        response_model=Operation,
        dependencies=[Depends(authorize)],
        tags=["Operations"],
    )
    async def get_operation(operation_id: CanonicalUUID4Input) -> Operation:
        operation = runtime.store.get_operation(operation_id)
        if operation is None:
            raise ApiError(404, ErrorCode.OPERATION_NOT_FOUND, "Operation was not found")
        return operation

    @app.post(
        "/api/v1/probe",
        response_model=ProbeResult,
        dependencies=[Depends(authorize), Depends(require_supported_probe_kind)],
        tags=["Probes"],
    )
    async def execute_probe(probe: ProbeRequest) -> ProbeResult:
        return await runtime.probes.execute(probe)

    return app
