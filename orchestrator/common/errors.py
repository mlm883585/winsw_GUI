from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    SOURCE_IP_DENIED = "SOURCE_IP_DENIED"
    STALE_AGENT_INSTANCE = "STALE_AGENT_INSTANCE"
    ENDPOINT_SOURCE_MISMATCH = "ENDPOINT_SOURCE_MISMATCH"
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_INVALID = "IDEMPOTENCY_KEY_INVALID"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    AGENT_OFFLINE = "AGENT_OFFLINE"
    SERVICE_NOT_ALLOWLISTED = "SERVICE_NOT_ALLOWLISTED"
    SERVICE_MAPPING_CHANGED = "SERVICE_MAPPING_CHANGED"
    SERVICE_NOT_INSTALLED = "SERVICE_NOT_INSTALLED"
    SERVICE_STATE_UNKNOWN = "SERVICE_STATE_UNKNOWN"
    SERVICE_ACTION_CONFLICT = "SERVICE_ACTION_CONFLICT"
    SERVICE_IN_ACTIVE_RUN = "SERVICE_IN_ACTIVE_RUN"
    SCM_ACTION_FAILED = "SCM_ACTION_FAILED"
    SCM_ACTION_TIMEOUT = "SCM_ACTION_TIMEOUT"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    AGENT_PROTOCOL_MISMATCH = "AGENT_PROTOCOL_MISMATCH"
    PROBE_TARGET_DENIED = "PROBE_TARGET_DENIED"
    PROBE_UNSUPPORTED = "PROBE_UNSUPPORTED"
    PROBE_FAILED = "PROBE_FAILED"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    GROUP_NOT_READY = "GROUP_NOT_READY"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    detail: Any | None
    request_id: UUID


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        detail: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        self.headers = headers or {}


def request_id(request: Request) -> UUID:
    value = getattr(request.state, "request_id", None)
    if isinstance(value, UUID):
        return value
    generated = uuid4()
    request.state.request_id = generated
    return generated


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = uuid4()
        response = await call_next(request)
        response.headers["X-Request-Id"] = str(request.state.request_id)
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        body = ErrorResponse(
            code=exc.code,
            message=exc.message,
            detail=exc.detail,
            request_id=request_id(request),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json"),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = {
            "errors": [
                {
                    "location": list(item.get("loc", ())),
                    "message": item.get("msg", "invalid value"),
                }
                for item in exc.errors()
            ]
        }
        body = ErrorResponse(
            code=ErrorCode.VALIDATION_ERROR,
            message="One or more request fields are invalid",
            detail=detail,
            request_id=request_id(request),
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            code = ErrorCode.ROUTE_NOT_FOUND
            message = "Route was not found"
        elif exc.status_code == 405:
            code = ErrorCode.METHOD_NOT_ALLOWED
            message = "HTTP method is not allowed for this route"
        else:
            code = ErrorCode.VALIDATION_ERROR
            message = "Request could not be processed"
        body = ErrorResponse(
            code=code,
            message=message,
            detail=None,
            request_id=request_id(request),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json"),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        del exc
        body = ErrorResponse(
            code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error",
            detail=None,
            request_id=request_id(request),
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))
