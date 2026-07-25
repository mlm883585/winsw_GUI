from __future__ import annotations

import ipaddress

import pytest

from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.probe_targets import (
    validate_http_probe_url,
    validate_local_probe_host,
)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("192.0.2.10", "192.0.2.10"),
        ("2001:db8:0:0::10", "2001:db8::10"),
    ],
)
def test_local_probe_host_accepts_inventory_string_addresses(
    host: str,
    expected: str,
) -> None:
    inventory_addresses = ["192.0.2.10", "2001:db8::10"]

    assert validate_local_probe_host(host, inventory_addresses) == expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("::1", "::1"),
    ],
)
def test_local_probe_host_allows_contract_loopbacks_without_inventory_entries(
    host: str,
    expected: str,
) -> None:
    assert validate_local_probe_host(host, []) == expected


def test_local_probe_host_accepts_ipaddress_objects_from_runtime_provider() -> None:
    addresses = {
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("10.20.30.40"),
    }

    assert validate_local_probe_host("10.20.30.40", addresses) == "10.20.30.40"


@pytest.mark.parametrize(
    "host",
    [
        "LOCALHOST",
        "example.com",
        "192.0.2.11",
        "::ffff:192.0.2.10",
        "fe80::1%7",
    ],
)
def test_local_probe_host_rejects_nonlocal_and_ambiguous_targets(host: str) -> None:
    with pytest.raises(ApiError) as raised:
        validate_local_probe_host(host, ["192.0.2.10"])

    assert raised.value.status_code == 422
    assert raised.value.code is ErrorCode.PROBE_TARGET_DENIED
    assert raised.value.message == (
        "Probe target must be localhost or an IP address bound to this Agent"
    )
    assert raised.value.detail is None


def test_local_probe_host_fails_closed_for_malformed_explicit_address_set() -> None:
    with pytest.raises(ApiError) as raised:
        validate_local_probe_host("192.0.2.10", ["not-an-ip"])

    assert raised.value.status_code == 422
    assert raised.value.code is ErrorCode.PROBE_TARGET_DENIED


def test_http_probe_url_uses_inventory_addresses_and_preserves_wire_quoting() -> None:
    target = validate_http_probe_url(
        "http://192.0.2.10:8080/health check/\u96ea?name=a b&raw=%2F:x",
        ["192.0.2.10"],
    )

    assert target == (
        "192.0.2.10",
        8080,
        "/health%20check/%E9%9B%AA?name=a%20b&raw=%2F:x",
    )


def test_http_probe_url_normalizes_localhost_and_default_port() -> None:
    assert validate_http_probe_url("HTTP://LOCALHOST/ready", []) == (
        "127.0.0.1",
        80,
        "/ready",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://192.0.2.10/health",
        "http://user:password@192.0.2.10/health",
        "http://192.0.2.11/health",
        "http://[::ffff:192.0.2.10]/health",
        "http://192.0.2.10/health#details",
        "http://192.0.2.10:0/health",
        "http://192.0.2.10:/health",
    ],
)
def test_http_probe_url_rejects_contract_violations(url: str) -> None:
    with pytest.raises(ApiError) as raised:
        validate_http_probe_url(url, ["192.0.2.10"])

    assert raised.value.status_code == 422
    assert raised.value.code is ErrorCode.PROBE_TARGET_DENIED
