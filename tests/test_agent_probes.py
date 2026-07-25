from __future__ import annotations

import asyncio
import builtins
import ipaddress
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orchestrator.agent.config import AllowlistedService
from orchestrator.agent.probes import ProbeExecutor, SystemLocalAddressProvider
from orchestrator.agent.scm import SERVICE_DEMAND_START, SERVICE_RUNNING, SCMServiceStatus
from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.models import HttpProbeRequest, ScmProbeRequest, TcpProbeRequest


class StaticAddresses:
    def get_addresses(self):
        return {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}


class ActiveSCM:
    def query(self, _name: str) -> SCMServiceStatus:
        return SCMServiceStatus(SERVICE_RUNNING, start_type=SERVICE_DEMAND_START)

    def start(self, _name: str) -> None:
        raise AssertionError("probe must not start a service")

    def stop(self, _name: str) -> None:
        raise AssertionError("probe must not stop a service")


def executor() -> ProbeExecutor:
    service = AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")
    return ProbeExecutor(
        backend=ActiveSCM(),
        services={service.local_service_id: service},
        address_provider=StaticAddresses(),
    )


def test_scm_probe_only_accepts_allowlisted_services() -> None:
    async def scenario() -> None:
        result = await executor().execute(ScmProbeRequest(kind="scm", local_service_id="mysql"))
        assert result.passed is True
        assert result.code == "SCM_ACTIVE"
        with pytest.raises(ApiError) as raised:
            await executor().execute(ScmProbeRequest(kind="scm", local_service_id="redis"))
        assert raised.value.code is ErrorCode.SERVICE_NOT_ALLOWLISTED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "host",
    ["example.com", "192.0.2.10", "::ffff:127.0.0.1", "fe80::1%3"],
)
def test_tcp_probe_rejects_dns_remote_and_address_bypasses(host: str) -> None:
    async def scenario() -> None:
        with pytest.raises(ApiError) as raised:
            await executor().execute(TcpProbeRequest(kind="tcp", host=host, port=80))
        assert raised.value.code is ErrorCode.PROBE_TARGET_DENIED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/health",
        "http://user:password@127.0.0.1/health",
        "http://example.com/health",
        "http://[::ffff:127.0.0.1]/health",
        "http://127.0.0.1/health#fragment",
        "http://127.0.0.1:0/health",
        "http://127.0.0.1:/health",
    ],
)
def test_http_probe_rejects_nonlocal_and_ambiguous_urls(url: str) -> None:
    async def scenario() -> None:
        with pytest.raises(ApiError) as raised:
            await executor().execute(HttpProbeRequest(kind="http", url=url, expected_status=200))
        assert raised.value.code is ErrorCode.PROBE_TARGET_DENIED

    asyncio.run(scenario())


def test_http_probe_never_follows_redirects_or_returns_body() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.10/private")
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = HttpProbeRequest(
            kind="http",
            url=f"http://127.0.0.1:{server.server_port}/health",
            expected_status=302,
        )
        result = asyncio.run(executor().execute(request))
        assert result.passed is False
        assert result.code == "HTTP_REDIRECT_DENIED"
        assert "192.0.2.10" not in result.message
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_address_discovery_fails_closed_without_psutil(monkeypatch) -> None:
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("injected missing psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(RuntimeError, match="psutil is required"):
        SystemLocalAddressProvider().get_addresses()


@pytest.mark.parametrize("kind", ["tcp", "http"])
def test_probe_revalidates_address_at_connection_boundary_without_network_call(
    monkeypatch, kind: str
) -> None:
    class AddressRemoved:
        def __init__(self) -> None:
            self.calls = 0

        def get_addresses(self):
            self.calls += 1
            if self.calls == 1:
                return {ipaddress.ip_address("192.0.2.10")}
            return set()

    provider = AddressRemoved()
    service = AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")
    probe = ProbeExecutor(
        backend=ActiveSCM(),
        services={service.local_service_id: service},
        address_provider=provider,
    )
    network_calls: list[str] = []

    def forbidden_socket(*_args, **_kwargs):
        network_calls.append("tcp")
        raise AssertionError("TCP connection must not be attempted")

    async def forbidden_open_connection(*_args, **_kwargs):
        network_calls.append("http")
        raise AssertionError("HTTP connection must not be attempted")

    monkeypatch.setattr("orchestrator.agent.probes.socket.create_connection", forbidden_socket)
    monkeypatch.setattr(
        "orchestrator.agent.probes.asyncio.open_connection",
        forbidden_open_connection,
    )
    request = (
        TcpProbeRequest(kind="tcp", host="192.0.2.10", port=3306)
        if kind == "tcp"
        else HttpProbeRequest(
            kind="http",
            url="http://192.0.2.10:8080/health",
            expected_status=200,
        )
    )

    with pytest.raises(ApiError) as raised:
        asyncio.run(probe.execute(request))
    assert raised.value.code is ErrorCode.PROBE_TARGET_DENIED
    assert provider.calls == 2
    assert network_calls == []


def test_tcp_probe_source_binds_the_validated_local_address(monkeypatch) -> None:
    calls: list[tuple[tuple[str, int], tuple[str, int]]] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def recording_connection(address, *, timeout, source_address):
        assert timeout > 0
        calls.append((address, source_address))
        return Connection()

    monkeypatch.setattr(
        "orchestrator.agent.probes.socket.create_connection", recording_connection
    )
    result = asyncio.run(
        executor().execute(TcpProbeRequest(kind="tcp", host="127.0.0.1", port=3306))
    )
    assert result.passed is True
    assert calls == [(('127.0.0.1', 3306), ('127.0.0.1', 0))]


def test_tcp_probe_timeout_is_a_monotonic_total_deadline(monkeypatch) -> None:
    class LateConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def slow_connection(*_args, **_kwargs):
        time.sleep(0.5)
        return LateConnection()

    monkeypatch.setattr(
        "orchestrator.agent.probes.socket.create_connection", slow_connection
    )

    async def scenario():
        started = time.monotonic()
        result = await executor().execute(
            TcpProbeRequest(
                kind="tcp",
                host="127.0.0.1",
                port=3306,
                timeout_seconds=0.1,
            )
        )
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert result.passed is False
    assert result.code == "TCP_CONNECT_FAILED"
    assert elapsed < 0.3


def test_http_probe_source_binds_and_keeps_fixed_client_policy(monkeypatch) -> None:
    requests: list[tuple[str, str | None, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(
                (
                    self.command,
                    self.headers.get("Accept-Encoding"),
                    self.headers.get("Connection"),
                )
            )
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"UP")

        def log_message(self, _format, *_args):
            return

    real_open_connection = asyncio.open_connection
    connections: list[tuple[str | None, int | None, tuple[str, int] | None]] = []

    async def recording_open_connection(*args, **kwargs):
        connections.append(
            (
                kwargs.get("host"),
                kwargs.get("port"),
                kwargs.get("local_addr"),
            )
        )
        return await real_open_connection(*args, **kwargs)

    monkeypatch.setattr(
        "orchestrator.agent.probes.asyncio.open_connection",
        recording_open_connection,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = asyncio.run(
            executor().execute(
                HttpProbeRequest(
                    kind="http",
                    url=f"http://127.0.0.1:{server.server_port}/health",
                    expected_status=200,
                    body_contains="UP",
                )
            )
        )
        assert result.passed is True
        assert result.code == "HTTP_READY"
        assert connections == [
            ("127.0.0.1", server.server_port, ("127.0.0.1", 0))
        ]
        assert requests == [("GET", "identity", "close")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_probe_supports_a_bounded_chunked_response() -> None:
    class ChunkedHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"2\r\nUP\r\n0\r\n\r\n")

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ChunkedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = asyncio.run(
            executor().execute(
                HttpProbeRequest(
                    kind="http",
                    url=f"http://127.0.0.1:{server.server_port}/health",
                    expected_status=200,
                    body_contains="UP",
                )
            )
        )
        assert result.passed is True
        assert result.code == "HTTP_READY"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_probe_rejects_an_oversized_response_before_reading_the_body() -> None:
    class OversizedHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(64 * 1024 + 1))
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), OversizedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = asyncio.run(
            executor().execute(
                HttpProbeRequest(
                    kind="http",
                    url=f"http://127.0.0.1:{server.server_port}/health",
                    expected_status=200,
                )
            )
        )
        assert result.passed is False
        assert result.code == "HTTP_RESPONSE_TOO_LARGE"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_probe_timeout_is_a_monotonic_total_deadline() -> None:
    class SlowBodyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            try:
                for _ in range(10):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.08)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowBodyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        result = asyncio.run(
            executor().execute(
                HttpProbeRequest(
                    kind="http",
                    url=f"http://127.0.0.1:{server.server_port}/slow",
                    expected_status=200,
                    timeout_seconds=0.1,
                )
            )
        )
        elapsed = time.monotonic() - started
        assert result.passed is False
        assert result.code == "HTTP_REQUEST_FAILED"
        assert elapsed < 0.4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
