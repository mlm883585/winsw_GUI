from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Request

from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.security import csrf_token, verify_password


@dataclass(frozen=True)
class AdminCredentials:
    username: str
    password_hash: str


class AdminAuth:
    def __init__(self, credentials: AdminCredentials) -> None:
        self.credentials = credentials

    def authenticate(self, username: str, password: str) -> bool:
        username_ok = hmac.compare_digest(
            username.encode("utf-8"), self.credentials.username.encode("utf-8")
        )
        password_ok = verify_password(password, self.credentials.password_hash)
        return username_ok and password_ok

    def login(self, request: Request) -> str:
        token = csrf_token()
        request.session.clear()
        request.session.update({"admin": self.credentials.username, "csrf": token})
        return token

    @staticmethod
    def logout(request: Request) -> None:
        request.session.clear()


def csrf_matches(expected: object, supplied: object) -> bool:
    return (
        isinstance(expected, str)
        and isinstance(supplied, str)
        and hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
    )


def require_admin(request: Request) -> str:
    username = request.session.get("admin") if hasattr(request, "session") else None
    if not isinstance(username, str) or not username:
        raise ApiError(401, ErrorCode.AUTH_REQUIRED, "Administrator session is required")
    return username


def require_admin_write(request: Request) -> str:
    username = require_admin(request)
    expected = request.session.get("csrf")
    supplied = request.headers.get("X-CSRF-Token")
    if not csrf_matches(expected, supplied):
        raise ApiError(403, ErrorCode.AUTH_INVALID, "CSRF token is missing or invalid")
    return username
