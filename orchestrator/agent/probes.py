from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Mapping
from typing import Protocol

from orchestrator.agent.config import AllowlistedService
from orchestrator.agent.scm import SCMBackend, observe_service
from orchestrator.common.enums import InstallationState, RuntimeState
from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.models import (
    HttpProbeRequest,
    ProbeRequest,
    ProbeResult,
    ScmProbeRequest,
    TcpProbeRequest,
)
from orchestrator.common.probe_targets import (
    validate_http_probe_url,
    validate_local_probe_host,
)
from orchestrator.common.time import utc_now


MAX_HTTP_BODY_BYTES = 64 * 1024
MAX_HTTP_HEADER_BYTES = 64 * 1024


class _HttpProtocolError(RuntimeError):
    pass


class _HttpResponseTooLarge(RuntimeError):
    pass


class LocalAddressProvider(Protocol):
    def get_addresses(self) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]: ...


class SystemLocalAddressProvider:
    def get_addresses(self) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = {
            ipaddress.ip_address("127.0.0.1"),
            ipaddress.ip_address("::1"),
        }
        try:
            import psutil  # type: ignore[import-not-found]
        except ImportError as exc:
            # DNS results for the hostname do not prove that an address is
            # currently bound to a local interface.  Fail closed instead.
            raise RuntimeError("psutil is required for local probe target validation") from exc
        for rows in psutil.net_if_addrs().values():
            for row in rows:
                if row.family not in {socket.AF_INET, socket.AF_INET6}:
                    continue
                value = row.address.split("%", 1)[0]
                try:
                    addresses.add(ipaddress.ip_address(value))
                except ValueError:
                    pass
        return addresses


class ProbeExecutor:
    def __init__(
        self,
        *,
        backend: SCMBackend,
        services: Mapping[str, AllowlistedService],
        address_provider: LocalAddressProvider | None = None,
    ) -> None:
        self.backend = backend
        self.services = dict(services)
        self.address_provider = address_provider or SystemLocalAddressProvider()

    async def execute(self, request: ProbeRequest) -> ProbeResult:
        started_at = utc_now()
        started = time.monotonic()
        deadline = started + request.timeout_seconds
        if isinstance(request, ScmProbeRequest):
            passed, code, message = await self._scm(request)
        elif isinstance(request, TcpProbeRequest):
            # Validate once at request handling and again in the connection
            # worker.  The socket also binds the target as its source address.
            validate_local_probe_host(
                request.host,
                self.address_provider.get_addresses(),
            )
            try:
                passed, code, message = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._tcp,
                        request.host,
                        request.port,
                        deadline,
                    ),
                    timeout=self._remaining(deadline),
                )
            except TimeoutError:
                passed, code, message = (
                    False,
                    "TCP_CONNECT_FAILED",
                    "TCP endpoint did not accept a connection",
                )
        elif isinstance(request, HttpProbeRequest):
            # Parsing/address validation is repeated by _http immediately
            # before the source-bound transport is created.
            validate_http_probe_url(
                request.url,
                self.address_provider.get_addresses(),
            )
            passed, code, message = await self._http(
                request.url,
                request.expected_status,
                request.body_contains,
                deadline,
            )
        else:  # pragma: no cover - discriminated Pydantic union prevents this.
            raise ApiError(422, ErrorCode.PROBE_UNSUPPORTED, "Probe kind is not supported")
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        return ProbeResult(
            passed=passed,
            observed_at=started_at,
            latency_ms=latency_ms,
            code=code,
            message=message,
        )

    async def _scm(self, request: ScmProbeRequest) -> tuple[bool, str, str]:
        service = self.services.get(request.local_service_id)
        if service is None:
            raise ApiError(
                404,
                ErrorCode.SERVICE_NOT_ALLOWLISTED,
                "Service is not in the Agent allowlist",
            )
        try:
            observed = await asyncio.wait_for(
                asyncio.to_thread(observe_service, service, self.backend),
                timeout=request.timeout_seconds,
            )
        except TimeoutError:
            return False, "SCM_PROBE_TIMEOUT", "SCM state query timed out"
        if observed.installation_state is InstallationState.NOT_INSTALLED:
            return False, "SCM_NOT_INSTALLED", "Allowlisted Windows service is not installed"
        if observed.runtime_state is RuntimeState.ACTIVE:
            return True, "SCM_ACTIVE", "Windows service is active"
        return False, "SCM_NOT_ACTIVE", "Windows service is not active"

    def _tcp(self, host: str, port: int, deadline: float) -> tuple[bool, str, str]:
        try:
            target = validate_local_probe_host(
                host,
                self.address_provider.get_addresses(),
            )
            with socket.create_connection(
                (target, port),
                timeout=self._remaining(deadline),
                source_address=(target, 0),
            ):
                self._remaining(deadline)
                return True, "TCP_CONNECTED", "TCP endpoint accepted a connection"
        except (OSError, TimeoutError):
            return False, "TCP_CONNECT_FAILED", "TCP endpoint did not accept a connection"

    async def _http(
        self,
        url: str,
        expected_status: int,
        body_contains: str | None,
        deadline: float,
    ) -> tuple[bool, str, str]:
        target, port, path = validate_http_probe_url(
            url,
            self.address_provider.get_addresses(),
        )
        authority = f"[{target}]" if ":" in target else target
        writer: asyncio.StreamWriter | None = None
        try:
            remaining = self._remaining(deadline)
            async with asyncio.timeout(remaining):
                reader, writer = await asyncio.open_connection(
                    host=target,
                    port=port,
                    local_addr=(target, 0),
                    limit=MAX_HTTP_HEADER_BYTES,
                )
                request = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {authority}:{port}\r\n"
                    "Accept: */*\r\n"
                    "Accept-Encoding: identity\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
                writer.write(request)
                await writer.drain()
                status, headers = await self._read_http_headers(reader)
                if 300 <= status <= 399:
                    return (
                        False,
                        "HTTP_REDIRECT_DENIED",
                        "HTTP probe redirects are not allowed",
                    )
                if status != expected_status:
                    return (
                        False,
                        "HTTP_STATUS_MISMATCH",
                        "HTTP status did not match the expectation",
                    )
                body = await self._read_http_body(reader, status, headers)
                if body_contains is not None and body_contains not in body.decode(
                    "utf-8", errors="replace"
                ):
                    return (
                        False,
                        "HTTP_BODY_MISMATCH",
                        "HTTP response did not contain the expected marker",
                    )
                return (
                    True,
                    "HTTP_READY",
                    "HTTP endpoint satisfied the readiness expectation",
                )
        except _HttpResponseTooLarge:
            return (
                False,
                "HTTP_RESPONSE_TOO_LARGE",
                "HTTP response exceeded the probe limit",
            )
        except (
            TimeoutError,
            OSError,
            UnicodeError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            _HttpProtocolError,
        ):
            return False, "HTTP_REQUEST_FAILED", "HTTP endpoint could not be queried"
        finally:
            if writer is not None:
                # close() is non-blocking.  Waiting for a hostile peer to finish
                # TLS/TCP teardown must not extend the probe's total deadline.
                writer.close()

    @staticmethod
    async def _read_http_headers(
        reader: asyncio.StreamReader,
    ) -> tuple[int, dict[str, list[str]]]:
        total_bytes = 0
        status_line = await reader.readline()
        total_bytes += len(status_line)
        if (
            not status_line.endswith(b"\n")
            or total_bytes > MAX_HTTP_HEADER_BYTES
        ):
            raise _HttpProtocolError("invalid HTTP status line")
        parts = status_line.rstrip(b"\r\n").split(b" ", 2)
        if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
            raise _HttpProtocolError("invalid HTTP status line")
        try:
            status = int(parts[1])
        except ValueError as exc:
            raise _HttpProtocolError("invalid HTTP status") from exc
        if status < 100 or status > 599:
            raise _HttpProtocolError("invalid HTTP status")

        headers: dict[str, list[str]] = {}
        while True:
            line = await reader.readline()
            total_bytes += len(line)
            if total_bytes > MAX_HTTP_HEADER_BYTES or not line.endswith(b"\n"):
                raise _HttpProtocolError("HTTP headers are invalid or too large")
            if line in {b"\r\n", b"\n"}:
                return status, headers
            if line[:1] in {b" ", b"\t"} or b":" not in line:
                raise _HttpProtocolError("obsolete or malformed HTTP header")
            name, value = line.rstrip(b"\r\n").split(b":", 1)
            try:
                normalized_name = name.decode("ascii").strip().lower()
                normalized_value = value.decode("latin-1").strip()
            except UnicodeError as exc:
                raise _HttpProtocolError("HTTP header cannot be decoded") from exc
            if not normalized_name:
                raise _HttpProtocolError("HTTP header name is empty")
            headers.setdefault(normalized_name, []).append(normalized_value)

    @classmethod
    async def _read_http_body(
        cls,
        reader: asyncio.StreamReader,
        status: int,
        headers: dict[str, list[str]],
    ) -> bytes:
        if 100 <= status < 200 or status in {204, 304}:
            return b""

        transfer_values = headers.get("transfer-encoding", [])
        length_values = headers.get("content-length", [])
        if transfer_values and length_values:
            raise _HttpProtocolError("ambiguous HTTP body framing")
        if transfer_values:
            codings = [
                coding.strip().lower()
                for value in transfer_values
                for coding in value.split(",")
                if coding.strip()
            ]
            if codings != ["chunked"]:
                raise _HttpProtocolError("unsupported HTTP transfer encoding")
            return await cls._read_chunked_body(reader)
        if length_values:
            normalized_lengths = {
                value.strip()
                for header_value in length_values
                for value in header_value.split(",")
            }
            if len(normalized_lengths) != 1:
                raise _HttpProtocolError("conflicting Content-Length values")
            length_text = normalized_lengths.pop()
            if not length_text.isdigit():
                raise _HttpProtocolError("invalid Content-Length")
            length = int(length_text)
            if length > MAX_HTTP_BODY_BYTES:
                raise _HttpResponseTooLarge
            return await reader.readexactly(length)

        body = bytearray()
        while True:
            chunk = await reader.read(min(8192, MAX_HTTP_BODY_BYTES + 1 - len(body)))
            if not chunk:
                return bytes(body)
            body.extend(chunk)
            if len(body) > MAX_HTTP_BODY_BYTES:
                raise _HttpResponseTooLarge

    @staticmethod
    async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        trailer_bytes = 0
        while True:
            size_line = await reader.readline()
            if not size_line.endswith(b"\n") or len(size_line) > 1024:
                raise _HttpProtocolError("invalid HTTP chunk size")
            size_text = size_line.rstrip(b"\r\n").split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise _HttpProtocolError("invalid HTTP chunk size") from exc
            if size < 0:
                raise _HttpProtocolError("invalid HTTP chunk size")
            if size == 0:
                while True:
                    trailer = await reader.readline()
                    trailer_bytes += len(trailer)
                    if (
                        not trailer.endswith(b"\n")
                        or trailer_bytes > MAX_HTTP_HEADER_BYTES
                    ):
                        raise _HttpProtocolError("invalid HTTP trailer")
                    if trailer in {b"\r\n", b"\n"}:
                        return bytes(body)
            if len(body) + size > MAX_HTTP_BODY_BYTES:
                raise _HttpResponseTooLarge
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise _HttpProtocolError("invalid HTTP chunk terminator")

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining
