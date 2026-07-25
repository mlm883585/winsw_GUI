from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from urllib.parse import quote, urlsplit

from orchestrator.common.errors import ApiError, ErrorCode


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
LocalAddress = str | IPAddress

_IPV4_LOOPBACK = ipaddress.IPv4Address("127.0.0.1")
_IPV6_LOOPBACK = ipaddress.IPv6Address("::1")
_LOOPBACK_ADDRESSES = frozenset({_IPV4_LOOPBACK, _IPV6_LOOPBACK})


def validate_local_probe_host(
    host: str,
    local_addresses: Iterable[LocalAddress],
) -> str:
    """Return a canonical local probe host or reject the target.

    ``local_addresses`` is supplied by the caller so this function performs no
    interface discovery or other I/O.  Runtime callers must supply a fresh
    snapshot at each security boundary; deployment inventory validation can
    instead supply its declared ``active_unicast_ips`` strings.
    """

    if host == "localhost":
        return str(_IPV4_LOOPBACK)
    if "%" in host:
        raise _target_denied()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise _target_denied() from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise _target_denied()
    if address in _LOOPBACK_ADDRESSES:
        return str(address)

    try:
        allowed_addresses = {_local_address(value) for value in local_addresses}
    except (TypeError, ValueError) as exc:
        # A malformed caller-supplied address set must fail closed and must not
        # accidentally authorize an otherwise valid remote target.
        raise _target_denied() from exc
    if address not in allowed_addresses:
        raise _target_denied()
    return str(address)


def validate_http_probe_url(
    url: str,
    local_addresses: Iterable[LocalAddress],
) -> tuple[str, int, str]:
    """Validate an HTTP probe URL and return host, port, and request target.

    The request target quoting intentionally matches the Agent wire behavior:
    existing percent escapes and RFC path/query delimiters remain intact while
    spaces, Unicode, and other unsafe bytes are UTF-8 percent-encoded.
    """

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise _target_denied() from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _target_denied()
    explicit_port = _explicit_port_text(parsed.netloc)
    if explicit_port == "" or port == 0:
        raise _target_denied()
    if port is None:
        port = 80
    target = validate_local_probe_host(parsed.hostname, local_addresses)
    path = quote(
        parsed.path or "/",
        safe="/%:@!$&'()*+,;=-._~",
    )
    if parsed.query:
        path += "?" + quote(
            parsed.query,
            safe="/%?:@!$&'()*+,;=-._~",
        )
    return target, port, path


def _local_address(value: LocalAddress) -> IPAddress:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return value
    if "%" in value:
        raise ValueError("scoped addresses are not valid local probe targets")
    return ipaddress.ip_address(value)


def _explicit_port_text(netloc: str) -> str | None:
    host_port = netloc.rsplit("@", 1)[-1]
    if host_port.startswith("["):
        closing = host_port.find("]")
        if closing < 0:
            return ""
        suffix = host_port[closing + 1 :]
        if suffix == "":
            return None
        return suffix[1:] if suffix.startswith(":") else ""
    if ":" not in host_port:
        return None
    return host_port.rsplit(":", 1)[1]


def _target_denied() -> ApiError:
    return ApiError(
        422,
        ErrorCode.PROBE_TARGET_DENIED,
        "Probe target must be localhost or an IP address bound to this Agent",
    )
