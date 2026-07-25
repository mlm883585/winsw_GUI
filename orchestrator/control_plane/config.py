from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orchestrator.common.security import (
    parse_networks,
    validate_cluster_token,
    validate_non_placeholder_secret,
    validate_password_hash,
)


class ControlPlaneConfig(BaseModel):
    """Control Plane process configuration loaded from an external JSON file."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    listen_host: str = Field(default="0.0.0.0", min_length=1, max_length=255)
    listen_port: int = Field(default=8766, ge=1, le=65535)
    database_path: Path = Path("data/control-plane.sqlite3")
    cluster_token: str
    agent_source_cidrs: list[str] = Field(min_length=1)
    offline_after_seconds: Literal[45] = 45
    agent_request_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    agent_request_attempts: int = Field(default=3, ge=1, le=5)
    admin_username: str = Field(default="admin", min_length=1, max_length=64)
    admin_password_hash: str = Field(min_length=1, max_length=512)
    session_secret: str
    session_cookie_name: Literal["recovery_admin_session"] = "recovery_admin_session"
    session_max_age_seconds: int = Field(default=28_800, ge=300, le=86_400)

    @field_validator("cluster_token")
    @classmethod
    def cluster_token_is_random_enough(cls, value: str) -> str:
        return validate_cluster_token(value)

    @field_validator("listen_host")
    @classmethod
    def listen_host_is_ip_literal(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("listen_host must not contain surrounding whitespace")
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("listen_host must be an IP literal") from exc
        if address.is_multicast:
            raise ValueError("listen_host must not be multicast")
        return value

    @field_validator("session_secret")
    @classmethod
    def session_secret_is_long_enough(cls, value: str) -> str:
        if len(value.encode("utf-8")) < 32:
            raise ValueError("session_secret must contain at least 32 bytes")
        return validate_non_placeholder_secret(value, "session_secret")

    @field_validator("agent_source_cidrs")
    @classmethod
    def source_cidrs_are_valid(cls, value: list[str]) -> list[str]:
        networks = parse_networks(value)
        if any(network.prefixlen == 0 for network in networks):
            raise ValueError("agent_source_cidrs must not allow an entire IP address family")
        return value

    @field_validator("admin_password_hash")
    @classmethod
    def password_hash_is_supported(cls, value: str) -> str:
        return validate_password_hash(value)

    @field_validator("admin_username")
    @classmethod
    def username_has_no_surrounding_whitespace(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("admin_username must not be blank or padded with whitespace")
        return value


def load_control_plane_config(path: str | Path | None = None) -> ControlPlaneConfig:
    """Load strict JSON configuration; an environment variable may select the file."""

    selected = Path(path or os.environ.get("RECOVERY_CP_CONFIG", "control-plane.json")).expanduser().resolve()
    try:
        with selected.open("r", encoding="utf-8-sig") as stream:
            raw: Any = json.load(stream)
    except OSError as exc:
        raise RuntimeError("cannot read Control Plane configuration") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Control Plane configuration is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Control Plane configuration root must be an object")
    try:
        config = ControlPlaneConfig.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in exc.errors()})
        raise RuntimeError(
            "Control Plane configuration contains invalid fields: " + ", ".join(fields)
        ) from None
    if not config.database_path.is_absolute():
        config.database_path = (selected.parent / config.database_path).resolve()
    else:
        config.database_path = config.database_path.expanduser().resolve()
    return config
