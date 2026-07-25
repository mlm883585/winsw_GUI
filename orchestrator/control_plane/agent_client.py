from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel

from orchestrator.common.enums import ServiceAction
from orchestrator.common.errors import ErrorCode
from orchestrator.common.models import Operation, ProbeRequest, ProbeResult
from orchestrator.common.security import validate_cluster_token


class AgentClientError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


class AgentOperationProtocolError(AgentClientError):
    """A successful Agent HTTP response that is not a valid Operation.

    The exception deliberately carries no response body or Pydantic validation
    details: Agent payloads are untrusted and may contain secrets.  Recovery
    orchestration uses this type to distinguish a deterministic protocol breach
    from a transport or Agent business error.
    """

    def __init__(self) -> None:
        super().__init__(
            409,
            ErrorCode.AGENT_PROTOCOL_MISMATCH.value,
            "Agent returned an invalid Operation response",
            {"reason": "operation_schema_mismatch"},
        )


def _validated_operation(response: httpx.Response) -> Operation:
    try:
        return Operation.model_validate(response.json())
    except (TypeError, ValueError):
        # Do not attach the parser/validation exception: its text can include
        # arbitrary fields and values from the untrusted Agent response.
        pass
    # Raise outside the exception handler so even ``__context__`` cannot retain
    # an untrusted response value embedded in a parser/ValidationError message.
    raise AgentOperationProtocolError()


def _agent_base_url(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("",)
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
            or parsed.port is None
        ):
            raise ValueError
        ipaddress.ip_address(parsed.hostname)
    except (ValueError, TypeError) as exc:
        raise ValueError("Agent endpoint must be a canonical HTTP IP-literal endpoint") from exc
    return endpoint


class AgentClient:
    """Constrained client for the frozen Recovery Agent API.

    An action POST may be retried after a transport failure, but every retry uses
    the original Idempotency-Key.  No generic URL or arbitrary request method is
    exposed to callers.
    """

    def __init__(
        self,
        cluster_token: str,
        *,
        timeout_seconds: float = 10.0,
        attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cluster_token = validate_cluster_token(cluster_token)
        if attempts < 1:
            raise ValueError("attempts must be positive")
        self.attempts = attempts
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AgentClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cluster_token}"}

    async def dispatch_action(
        self,
        endpoint: str,
        local_service_id: str,
        action: ServiceAction | str,
        idempotency_key: UUID | str,
    ) -> Operation:
        action_value = action.value if isinstance(action, ServiceAction) else str(action)
        if action_value not in {item.value for item in ServiceAction}:
            raise ValueError("Unsupported service action")
        base = _agent_base_url(endpoint)
        url = f"{base}/api/v1/services/{local_service_id}/actions/{action_value}"
        response = await self._request_with_idempotent_retry(
            "POST",
            url,
            headers={**self._headers, "Idempotency-Key": str(idempotency_key)},
        )
        if response.status_code != 202:
            self._raise_response(response)
        return _validated_operation(response)

    async def start_action(
        self,
        endpoint: str,
        local_service_id: str,
        action: ServiceAction | str,
        idempotency_key: UUID | str,
    ) -> Operation:
        return await self.dispatch_action(endpoint, local_service_id, action, idempotency_key)

    async def get_operation(self, endpoint: str, operation_id: UUID | str) -> Operation:
        base = _agent_base_url(endpoint)
        response = await self._request(
            "GET", f"{base}/api/v1/operations/{operation_id}", headers=self._headers
        )
        if response.status_code != 200:
            self._raise_response(response)
        return _validated_operation(response)

    async def probe(
        self,
        endpoint: str,
        request: BaseModel | Mapping[str, Any],
    ) -> ProbeResult:
        base = _agent_base_url(endpoint)
        payload = request.model_dump(mode="json") if isinstance(request, BaseModel) else dict(request)
        # Validation here catches accidental expansion of the CP probe surface
        # before a network request is made.
        from pydantic import TypeAdapter

        validated = TypeAdapter(ProbeRequest).validate_python(payload)
        response = await self._request(
            "POST",
            f"{base}/api/v1/probe",
            headers=self._headers,
            json=validated.model_dump(mode="json"),
        )
        if response.status_code != 200:
            self._raise_response(response)
        return ProbeResult.model_validate(response.json())

    async def _request_with_idempotent_retry(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        last_error: httpx.TransportError | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == self.attempts:
                    break
            else:
                if response.status_code not in {502, 503, 504} or attempt == self.attempts:
                    return response
            await asyncio.sleep(min(0.1 * (2 ** (attempt - 1)), 1.0))
        raise AgentClientError(
            409,
            "AGENT_OFFLINE",
            "Agent request could not be completed",
            {"reason": "transport_error"},
        ) from last_error

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise AgentClientError(
                409,
                "AGENT_OFFLINE",
                "Agent request could not be completed",
                {"reason": "transport_error"},
            ) from exc

    @staticmethod
    def _raise_response(response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or "code" not in body or "message" not in body:
            raise AgentClientError(
                409,
                "AGENT_OFFLINE",
                "Agent returned an invalid protocol response",
                {"reason": "protocol_error", "http_status": response.status_code},
            )
        code = str(body["code"])
        message = str(body["message"])
        detail = body.get("detail")
        raise AgentClientError(response.status_code, code, message, detail)
