from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
from base64 import b64decode, urlsafe_b64encode
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from fastapi import Request

from .errors import ApiError, ErrorCode


_PLACEHOLDER_SECRET_PREFIXES = ("change-me", "replace", "required", "generate")
PBKDF2_SHA256_MIN_ITERATIONS = 100_000
PBKDF2_SHA256_MAX_ITERATIONS = 1_000_000
PBKDF2_SHA256_MIN_SALT_BYTES = 16
PBKDF2_SHA256_MAX_SALT_BYTES = 64


def validate_non_placeholder_secret(value: str, field_name: str) -> str:
    normalized = value.strip().casefold()
    if not normalized or normalized.startswith(_PLACEHOLDER_SECRET_PREFIXES):
        raise ValueError(f"{field_name} must be generated locally and must not be a placeholder")
    return value


def validate_cluster_token(token: str) -> str:
    if len(token.encode("utf-8")) < 32:
        raise ValueError("cluster_token must contain at least 32 bytes")
    return validate_non_placeholder_secret(token, "cluster_token")


def _decode_canonical_urlsafe_base64(value: str, field_name: str) -> bytes:
    try:
        encoded = value.encode("ascii")
        decoded = b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be canonical URL-safe base64") from exc
    if urlsafe_b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{field_name} must be canonical URL-safe base64")
    return decoded


def parse_password_hash(encoded: str) -> tuple[int, bytes, bytes]:
    """Parse the only supported administrator password hash representation."""

    parts = encoded.split("$")
    if len(parts) != 4:
        raise ValueError("admin_password_hash must contain exactly four fields")
    algorithm, iteration_text, salt_text, digest_text = parts
    if algorithm != "pbkdf2_sha256":
        raise ValueError("admin_password_hash must use pbkdf2_sha256")
    if (
        not iteration_text.isascii()
        or not iteration_text.isdecimal()
        or iteration_text != str(int(iteration_text))
    ):
        raise ValueError("admin_password_hash iterations must be a canonical integer")
    iterations = int(iteration_text)
    if not PBKDF2_SHA256_MIN_ITERATIONS <= iterations <= PBKDF2_SHA256_MAX_ITERATIONS:
        raise ValueError("admin_password_hash iterations are outside the supported range")
    salt = _decode_canonical_urlsafe_base64(salt_text, "admin_password_hash salt")
    if not PBKDF2_SHA256_MIN_SALT_BYTES <= len(salt) <= PBKDF2_SHA256_MAX_SALT_BYTES:
        raise ValueError("admin_password_hash salt length is outside the supported range")
    digest = _decode_canonical_urlsafe_base64(digest_text, "admin_password_hash digest")
    if len(digest) != hashlib.sha256().digest_size:
        raise ValueError("admin_password_hash digest must contain 32 bytes")
    return iterations, salt, digest


def validate_password_hash(encoded: str) -> str:
    parse_password_hash(encoded)
    return encoded


def require_bearer_token(request: Request, expected_token: str) -> None:
    header = request.headers.get("Authorization")
    if not header:
        raise ApiError(401, ErrorCode.AUTH_REQUIRED, "Bearer token is required")
    scheme, separator, supplied = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied:
        raise ApiError(401, ErrorCode.AUTH_INVALID, "Bearer token is invalid")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected_token.encode("utf-8")):
        raise ApiError(401, ErrorCode.AUTH_INVALID, "Bearer token is invalid")


def parse_networks(values: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for value in values:
        networks.append(ipaddress.ip_network(value, strict=False))
    if not networks:
        raise ValueError("at least one source CIDR is required")
    return tuple(networks)


def peer_ip(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if request.client is None:
        raise ApiError(403, ErrorCode.SOURCE_IP_DENIED, "Source address is unavailable")
    try:
        return ipaddress.ip_address(request.client.host)
    except ValueError as exc:
        raise ApiError(403, ErrorCode.SOURCE_IP_DENIED, "Source address is invalid") from exc


def require_source_ip(
    request: Request,
    networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = peer_ip(request)
    if not any(address.version == network.version and address in network for network in networks):
        raise ApiError(403, ErrorCode.SOURCE_IP_DENIED, "Source address is not allowed")
    return address


def parse_uuid4(value: str | None, *, required: bool = True) -> UUID:
    if value is None or value == "":
        if required:
            raise ApiError(
                422,
                ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
                "Idempotency-Key header is required",
            )
        raise ValueError("UUID value is empty")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ApiError(422, ErrorCode.IDEMPOTENCY_KEY_INVALID, "Idempotency-Key must be UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise ApiError(422, ErrorCode.IDEMPOTENCY_KEY_INVALID, "Idempotency-Key must be UUIDv4")
    return parsed


def canonical_request_fingerprint(method: str, path: str, body: Any | None = None) -> str:
    encoded_body = json.dumps(
        {} if body is None else body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw = f"{method.upper()}\n{path}\n{encoded_body}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def hash_password(password: str, *, iterations: int = 310_000, salt: bytes | None = None) -> str:
    salt = secrets.token_bytes(16) if salt is None else salt
    if not PBKDF2_SHA256_MIN_ITERATIONS <= iterations <= PBKDF2_SHA256_MAX_ITERATIONS:
        raise ValueError("PBKDF2 iterations are outside the supported range")
    if not PBKDF2_SHA256_MIN_SALT_BYTES <= len(salt) <= PBKDF2_SHA256_MAX_SALT_BYTES:
        raise ValueError("PBKDF2 salt length is outside the supported range")
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_encoded = urlsafe_b64encode(salt).decode()
    digest_encoded = urlsafe_b64encode(derived).decode()
    return f"pbkdf2_sha256${iterations}${salt_encoded}${digest_encoded}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        iterations, salt, expected = parse_password_hash(encoded)
    except (ValueError, TypeError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def csrf_token() -> str:
    return secrets.token_urlsafe(32)
