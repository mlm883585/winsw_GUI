from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Header, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from orchestrator import __version__
from orchestrator.common.enums import RecoveryRunStatus, RunTrigger, ServiceAction
from orchestrator.common.errors import ApiError, ErrorCode, install_error_handlers
from orchestrator.common.models import (
    AgentCollection,
    AgentReport,
    CanonicalUUID4Input,
    EmptyActionRequest,
    HeartbeatAck,
    HealthResponse,
    ManagedServiceCollection,
    ManualRunRequest,
    ProxiedOperation,
    ReadinessProbe,
    ReadinessWrite,
    RecoveryDependenciesWrite,
    RecoveryGroup,
    RecoveryGroupCollection,
    RecoveryGroupCreate,
    RecoveryGroupPatch,
    RecoveryMembersWrite,
    RecoveryRun,
    RecoveryRunCollection,
)
from orchestrator.common.security import (
    canonical_request_fingerprint,
    parse_networks,
    parse_uuid4,
    require_bearer_token,
    require_source_ip,
)

from .agent_client import AgentClient, AgentClientError
from .auth import (
    AdminAuth,
    AdminCredentials,
    csrf_matches,
    require_admin,
    require_admin_write,
)
from .config import ControlPlaneConfig, load_control_plane_config
from .recovery import RecoveryEngine, RecoveryEngineError
from .store import ControlPlaneStore


PACKAGE_DIR = Path(__file__).resolve().parent


def _as_model(model_type, value):
    return model_type.model_validate(value)


def _translate_client_error(exc: AgentClientError) -> ApiError:
    try:
        code = ErrorCode(exc.code)
    except ValueError:
        code = ErrorCode.AGENT_OFFLINE
    status_code = exc.status_code if exc.status_code in {404, 409, 422} else 409
    return ApiError(status_code, code, exc.message, exc.detail)


def create_app(
    config: ControlPlaneConfig | None = None,
    *,
    store: ControlPlaneStore | None = None,
    agent_client: AgentClient | None = None,
    recovery_engine: RecoveryEngine | None = None,
    start_background: bool = True,
) -> FastAPI:
    settings = config or load_control_plane_config()
    state_store = store or ControlPlaneStore(
        settings.database_path,
        offline_after_seconds=settings.offline_after_seconds,
    )
    client = agent_client or AgentClient(
        settings.cluster_token,
        timeout_seconds=settings.agent_request_timeout_seconds,
        attempts=settings.agent_request_attempts,
    )
    engine = recovery_engine or RecoveryEngine(state_store, client)
    stop_event = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = state_store
        app.state.agent_client = client
        app.state.recovery = engine
        scheduler: asyncio.Task[None] | None = None
        if start_background:
            await engine.resume_unfinished_runs()
            scheduler = asyncio.create_task(
                engine.scheduler_loop(stop_event), name="recovery-auto-scheduler"
            )
        try:
            yield
        finally:
            stop_event.set()
            if scheduler is not None:
                scheduler.cancel()
                await asyncio.gather(scheduler, return_exceptions=True)
            await engine.wait_for_idle()
            if agent_client is None:
                await client.close()

    app = FastAPI(
        title="WinSW Recovery Control Plane",
        version=__version__,
        description="LAB_HTTP / production_ready=false",
        lifespan=lifespan,
    )
    app.state.config = settings
    app.state.store = state_store
    app.state.agent_client = client
    app.state.recovery = engine
    app.state.admin_auth = AdminAuth(
        AdminCredentials(settings.admin_username, settings.admin_password_hash)
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=settings.session_cookie_name,
        max_age=settings.session_max_age_seconds,
        same_site="strict",
        https_only=False,
    )
    install_error_handlers(app)
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    source_networks = parse_networks(settings.agent_source_cidrs)

    def ingress_peer(request: Request) -> str:
        require_bearer_token(request, settings.cluster_token)
        return str(require_source_ip(request, source_networks))

    async def call_store(method: str, *args: Any, **kwargs: Any) -> Any:
        function = getattr(state_store, method)
        return await asyncio.to_thread(function, *args, **kwargs)

    @app.get("/healthz", response_model=HealthResponse, tags=["Health"])
    async def healthz() -> HealthResponse:
        return HealthResponse(component="control-plane", version=__version__)

    @app.post("/api/v1/agents/register", response_model=HeartbeatAck, tags=["Agent ingress"])
    async def register_agent(request: Request, report: AgentReport) -> HeartbeatAck:
        peer = ingress_peer(request)
        return _as_model(HeartbeatAck, await call_store("register_agent", report, peer))

    @app.post(
        "/api/v1/agents/{agent_id}/heartbeat",
        response_model=HeartbeatAck,
        tags=["Agent ingress"],
    )
    async def heartbeat(
        request: Request, agent_id: CanonicalUUID4Input, report: AgentReport
    ) -> HeartbeatAck:
        peer = ingress_peer(request)
        return _as_model(
            HeartbeatAck,
            await call_store("ingest_heartbeat", agent_id, report, peer),
        )

    @app.get("/api/v1/agents", response_model=AgentCollection, tags=["Inventory"])
    async def list_agents(_: str = Depends(require_admin)) -> AgentCollection:
        return AgentCollection(items=await call_store("list_agents"))

    @app.get("/api/v1/services", response_model=ManagedServiceCollection, tags=["Inventory"])
    async def list_services(_: str = Depends(require_admin)) -> ManagedServiceCollection:
        return ManagedServiceCollection(items=await call_store("list_services"))

    @app.post(
        "/api/v1/services/{managed_service_id}/actions/{action}",
        response_model=ProxiedOperation,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Service actions"],
    )
    async def proxy_action(
        response: Response,
        managed_service_id: CanonicalUUID4Input,
        action: ServiceAction,
        request: Request,
        body: EmptyActionRequest = EmptyActionRequest(),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        _: str = Depends(require_admin_write),
    ) -> ProxiedOperation:
        raw_body = await request.body()
        if raw_body.strip() and await request.json() is None:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "Action body must be an empty JSON object when provided",
            )
        del body
        key = parse_uuid4(idempotency_key)
        path = f"/api/v1/services/{managed_service_id}/actions/{action.value}"
        fingerprint = canonical_request_fingerprint("POST", path, {})
        dispatch = await call_store(
            "prepare_proxy_action", managed_service_id, action.value, key, fingerprint
        )
        if dispatch.get("operation_id"):
            existing = await call_store("get_proxy_operation", dispatch["operation_id"])
            if existing is not None:
                response.headers["Location"] = f"/api/v1/operations/{dispatch['operation_id']}"
                return _as_model(ProxiedOperation, existing)
        try:
            operation = await client.dispatch_action(
                dispatch["endpoint"], dispatch["local_service_id"], action, key
            )
        except AgentClientError as exc:
            raise _translate_client_error(exc) from exc
        saved = await call_store(
            "save_proxy_operation", managed_service_id, dispatch["endpoint"], operation
        )
        response.headers["Location"] = f"/api/v1/operations/{operation.operation_id}"
        return _as_model(ProxiedOperation, saved)

    @app.get(
        "/api/v1/operations/{operation_id}",
        response_model=ProxiedOperation,
        tags=["Service actions"],
    )
    async def get_proxy_operation(
        operation_id: CanonicalUUID4Input, _: str = Depends(require_admin)
    ) -> ProxiedOperation:
        route = await call_store("get_proxy_operation_route", operation_id)
        if route is None:
            raise ApiError(404, ErrorCode.OPERATION_NOT_FOUND, "Operation was not found")
        try:
            operation = await client.get_operation(route["endpoint"], operation_id)
        except AgentClientError as exc:
            raise _translate_client_error(exc) from exc
        saved = await call_store(
            "save_proxy_operation", route["managed_service_id"], route["endpoint"], operation
        )
        return _as_model(ProxiedOperation, saved)

    @app.get(
        "/api/v1/recovery-groups",
        response_model=RecoveryGroupCollection,
        tags=["Recovery groups"],
    )
    async def list_groups(_: str = Depends(require_admin)) -> RecoveryGroupCollection:
        return RecoveryGroupCollection(items=await call_store("list_groups"))

    @app.post(
        "/api/v1/recovery-groups",
        response_model=RecoveryGroup,
        status_code=status.HTTP_201_CREATED,
        tags=["Recovery groups"],
    )
    async def create_group(
        value: RecoveryGroupCreate,
        response: Response,
        _: str = Depends(require_admin_write),
    ) -> RecoveryGroup:
        group = _as_model(RecoveryGroup, await call_store("create_group", value))
        response.headers["Location"] = f"/api/v1/recovery-groups/{group.group_id}"
        return group

    async def require_group(group_id: CanonicalUUID4Input) -> RecoveryGroup:
        group = await call_store("get_group", group_id)
        if group is None:
            raise ApiError(404, ErrorCode.GROUP_NOT_READY, "Recovery group was not found")
        return _as_model(RecoveryGroup, group)

    @app.get(
        "/api/v1/recovery-groups/{group_id}",
        response_model=RecoveryGroup,
        tags=["Recovery groups"],
    )
    async def get_group(
        group_id: CanonicalUUID4Input, _: str = Depends(require_admin)
    ) -> RecoveryGroup:
        return await require_group(group_id)

    @app.patch(
        "/api/v1/recovery-groups/{group_id}",
        response_model=RecoveryGroup,
        tags=["Recovery groups"],
    )
    async def update_group(
        group_id: CanonicalUUID4Input,
        value: RecoveryGroupPatch,
        _: str = Depends(require_admin_write),
    ) -> RecoveryGroup:
        return _as_model(RecoveryGroup, await call_store("update_group", group_id, value))

    @app.delete(
        "/api/v1/recovery-groups/{group_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["Recovery groups"],
    )
    async def delete_group(
        group_id: CanonicalUUID4Input, _: str = Depends(require_admin_write)
    ) -> Response:
        await call_store("delete_group", group_id)
        return Response(status_code=204)

    @app.put(
        "/api/v1/recovery-groups/{group_id}/members",
        response_model=RecoveryGroup,
        tags=["Recovery groups"],
    )
    async def replace_members(
        group_id: CanonicalUUID4Input,
        value: RecoveryMembersWrite,
        _: str = Depends(require_admin_write),
    ) -> RecoveryGroup:
        result = await call_store("replace_members", group_id, value.managed_service_ids)
        return _as_model(RecoveryGroup, result)

    @app.put(
        "/api/v1/recovery-groups/{group_id}/dependencies",
        response_model=RecoveryGroup,
        tags=["Recovery groups"],
    )
    async def replace_dependencies(
        group_id: CanonicalUUID4Input,
        value: RecoveryDependenciesWrite,
        _: str = Depends(require_admin_write),
    ) -> RecoveryGroup:
        result = await call_store("replace_dependencies", group_id, value.dependencies)
        return _as_model(RecoveryGroup, result)

    @app.get(
        "/api/v1/recovery-groups/{group_id}/services/{managed_service_id}/probe",
        response_model=ReadinessProbe,
        tags=["Recovery groups"],
    )
    async def get_probe(
        group_id: CanonicalUUID4Input,
        managed_service_id: CanonicalUUID4Input,
        _: str = Depends(require_admin),
    ) -> ReadinessProbe:
        result = await call_store("get_probe", group_id, managed_service_id)
        if result is None:
            raise ApiError(404, ErrorCode.GROUP_NOT_READY, "Readiness probe was not found")
        return _as_model(ReadinessProbe, result)

    @app.put(
        "/api/v1/recovery-groups/{group_id}/services/{managed_service_id}/probe",
        response_model=ReadinessProbe,
        tags=["Recovery groups"],
    )
    async def put_probe(
        group_id: CanonicalUUID4Input,
        managed_service_id: CanonicalUUID4Input,
        definition: ReadinessWrite,
        _: str = Depends(require_admin_write),
    ) -> ReadinessProbe:
        return _as_model(
            ReadinessProbe,
            await call_store("put_probe", group_id, managed_service_id, definition),
        )

    @app.delete(
        "/api/v1/recovery-groups/{group_id}/services/{managed_service_id}/probe",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["Recovery groups"],
    )
    async def delete_probe(
        group_id: CanonicalUUID4Input,
        managed_service_id: CanonicalUUID4Input,
        _: str = Depends(require_admin_write),
    ) -> Response:
        await call_store("delete_probe", group_id, managed_service_id)
        return Response(status_code=204)

    @app.post(
        "/api/v1/recovery-groups/{group_id}/arm",
        response_model=RecoveryGroup,
        tags=["Recovery groups"],
    )
    async def arm_group(
        group_id: CanonicalUUID4Input, _: str = Depends(require_admin_write)
    ) -> RecoveryGroup:
        return _as_model(RecoveryGroup, await call_store("arm_group", group_id))

    @app.post(
        "/api/v1/recovery-groups/{group_id}/disarm",
        response_model=RecoveryGroup,
        tags=["Recovery groups"],
    )
    async def disarm_group(
        group_id: CanonicalUUID4Input, _: str = Depends(require_admin_write)
    ) -> RecoveryGroup:
        return _as_model(RecoveryGroup, await call_store("disarm_group", group_id))

    @app.post(
        "/api/v1/recovery-groups/{group_id}/runs",
        response_model=RecoveryRun,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Recovery runs"],
    )
    async def create_manual_run(
        group_id: CanonicalUUID4Input,
        response: Response,
        value: ManualRunRequest | None = None,
        _: str = Depends(require_admin_write),
    ) -> RecoveryRun:
        try:
            run = await engine.create_manual_run(group_id, None if value is None else value.reason)
        except RecoveryEngineError as exc:
            code = ErrorCode(exc.code) if exc.code in ErrorCode._value2member_map_ else ErrorCode.GROUP_NOT_READY
            raise ApiError(409, code, str(exc), exc.detail) from exc
        engine.launch_run(run["run_id"])
        response.headers["Location"] = f"/api/v1/recovery-runs/{run['run_id']}"
        return _as_model(RecoveryRun, run)

    @app.get(
        "/api/v1/recovery-runs",
        response_model=RecoveryRunCollection,
        tags=["Recovery runs"],
    )
    async def list_runs(
        group_id: CanonicalUUID4Input | None = Query(default=None),
        trigger: RunTrigger | None = Query(default=None),
        run_status: RecoveryRunStatus | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=2048),
        _: str = Depends(require_admin),
    ) -> RecoveryRunCollection:
        page = await call_store(
            "list_runs",
            group_id=group_id,
            trigger=trigger,
            status=run_status,
            limit=limit,
            cursor=cursor,
        )
        return _as_model(RecoveryRunCollection, page)

    @app.get(
        "/api/v1/recovery-runs/{run_id}",
        response_model=RecoveryRun,
        tags=["Recovery runs"],
    )
    async def get_run(
        run_id: CanonicalUUID4Input, _: str = Depends(require_admin)
    ) -> RecoveryRun:
        run = await call_store("get_run", run_id)
        if run is None:
            raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "RecoveryRun was not found")
        return _as_model(RecoveryRun, run)

    @app.post(
        "/api/v1/recovery-runs/{run_id}/retry",
        response_model=RecoveryRun,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Recovery runs"],
    )
    async def retry_run(
        run_id: CanonicalUUID4Input,
        response: Response,
        value: ManualRunRequest | None = None,
        _: str = Depends(require_admin_write),
    ) -> RecoveryRun:
        try:
            run = await engine.retry_run(run_id, None if value is None else value.reason)
        except RecoveryEngineError as exc:
            code = ErrorCode(exc.code) if exc.code in ErrorCode._value2member_map_ else ErrorCode.GROUP_NOT_READY
            status_code = 404 if code == ErrorCode.RUN_NOT_FOUND else 409
            raise ApiError(status_code, code, str(exc), exc.detail) from exc
        engine.launch_run(run["run_id"])
        response.headers["Location"] = f"/api/v1/recovery-runs/{run['run_id']}"
        return _as_model(RecoveryRun, run)

    # Minimal server-rendered UI. The management API remains the sole mutation path.
    def page_context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "session_admin": request.session.get("admin"),
            "csrf_token": request.session.get("csrf"),
            **values,
        }

    def require_page_login(request: Request) -> RedirectResponse | None:
        return None if request.session.get("admin") else RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request) -> HTMLResponse:
        if request.session.get("admin"):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", page_context(request, error=None))

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_submit(
        request: Request,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> HTMLResponse:
        auth: AdminAuth = app.state.admin_auth
        if not auth.authenticate(username, password):
            return templates.TemplateResponse(
                request,
                "login.html",
                page_context(request, error="用户名或密码错误"),
                status_code=401,
            )
        auth.login(request)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request, csrf_token: Annotated[str, Form()]) -> RedirectResponse:
        expected = request.session.get("csrf")
        if not csrf_matches(expected, csrf_token):
            raise ApiError(403, ErrorCode.AUTH_INVALID, "CSRF token is missing or invalid")
        app.state.admin_auth.logout(request)
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard_page(request: Request) -> HTMLResponse:
        redirect = require_page_login(request)
        if redirect:
            return redirect
        agents, services, recent_runs = await asyncio.gather(
            call_store("list_agents"),
            call_store("list_services"),
            call_store("list_runs", limit=10),
        )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            page_context(
                request,
                agents=agents,
                services=services,
                recent_runs=recent_runs["items"],
            ),
        )

    @app.get("/groups", response_class=HTMLResponse, include_in_schema=False)
    async def groups_page(
        request: Request, group: CanonicalUUID4Input | None = None
    ) -> HTMLResponse:
        redirect = require_page_login(request)
        if redirect:
            return redirect
        groups, services = await asyncio.gather(
            call_store("list_groups"), call_store("list_services")
        )
        selected = next((item for item in groups if str(item["group_id"]) == str(group)), None)
        member_ids = [] if selected is None else [item["managed_service_id"] for item in selected["members"]]
        dependencies = [] if selected is None else selected["dependencies"]
        return templates.TemplateResponse(
            request,
            "groups.html",
            page_context(
                request,
                groups=groups,
                services=services,
                selected_group=selected,
                selected_member_ids=member_ids,
                dependencies_json=json.dumps(dependencies, ensure_ascii=False, indent=2),
            ),
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
    async def run_page(
        request: Request, run_id: CanonicalUUID4Input
    ) -> HTMLResponse:
        redirect = require_page_login(request)
        if redirect:
            return redirect
        run = await call_store("get_run", run_id)
        if run is None:
            raise ApiError(404, ErrorCode.RUN_NOT_FOUND, "RecoveryRun was not found")
        return templates.TemplateResponse(
            request, "run_detail.html", page_context(request, run=run)
        )

    return app
