from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from orchestrator.common.models import SERVICE_SLUG_PATTERN
from orchestrator.common.security import parse_networks, validate_cluster_token


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class AllowlistedService(StrictConfigModel):
    local_service_id: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    windows_service_name: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("local_service_id", "windows_service_name")
    @classmethod
    def no_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("value must not contain surrounding whitespace")
        return value


class AgentConfig(StrictConfigModel):
    control_plane_url: str = Field(min_length=1, max_length=2048)
    listen_host: str = Field(default="0.0.0.0", min_length=1, max_length=255)
    listen_port: int = Field(default=8765, ge=1, le=65535)
    advertised_endpoint: str = Field(min_length=1, max_length=256)
    cluster_token: str = Field(min_length=32, max_length=4096)
    control_plane_source_cidrs: list[str] = Field(min_length=1)
    database_path: Path = Path("data/agent.sqlite3")
    heartbeat_interval_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    heartbeat_jitter_ratio: float = Field(default=0.2, ge=0.0, le=0.5)
    heartbeat_backoff_initial_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    heartbeat_backoff_max_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    action_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    action_poll_interval_seconds: float = Field(default=0.5, ge=0.05, le=10.0)
    services: list[AllowlistedService] = Field(min_length=1, max_length=1024)

    @field_validator("cluster_token")
    @classmethod
    def token_has_enough_entropy_bytes(cls, value: str) -> str:
        return validate_cluster_token(value)

    @field_validator("control_plane_source_cidrs")
    @classmethod
    def cidrs_are_valid(cls, values: list[str]) -> list[str]:
        networks = parse_networks(values)
        if any(network.prefixlen != network.max_prefixlen for network in networks):
            raise ValueError("control_plane_source_cidrs must contain only /32 or /128 host prefixes")
        return values

    @field_validator("control_plane_url")
    @classmethod
    def valid_control_plane_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("control_plane_url must be an http base URL without credentials/query/fragment")
        if parsed.path not in ("", "/"):
            raise ValueError("control_plane_url must not contain a path")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("control_plane_url contains an invalid port") from exc
        return value.rstrip("/")

    @field_validator("advertised_endpoint")
    @classmethod
    def valid_advertised_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
        ):
            raise ValueError("advertised_endpoint must be http://IP-literal:port")
        try:
            address = ipaddress.ip_address(parsed.hostname)
            port = parsed.port
        except (ValueError, TypeError) as exc:
            raise ValueError("advertised_endpoint must use an IP literal and valid port") from exc
        if port is None:
            raise ValueError("advertised_endpoint must include a port")
        if address.is_unspecified or address.is_multicast:
            raise ValueError("advertised_endpoint must use a unicast host address")
        return value.rstrip("/")

    @model_validator(mode="after")
    def unique_services_and_valid_backoff(self) -> "AgentConfig":
        local_ids = [item.local_service_id for item in self.services]
        if len(local_ids) != len(set(local_ids)):
            raise ValueError("local_service_id values must be unique")
        windows_names = [item.windows_service_name.casefold() for item in self.services]
        if len(windows_names) != len(set(windows_names)):
            raise ValueError("windows_service_name values must be unique (case-insensitive)")
        if self.heartbeat_backoff_initial_seconds > self.heartbeat_backoff_max_seconds:
            raise ValueError("heartbeat initial backoff must not exceed maximum backoff")

        advertised = urlsplit(self.advertised_endpoint)
        advertised_ip = ipaddress.ip_address(advertised.hostname)
        if advertised.port != self.listen_port:
            raise ValueError("advertised_endpoint port must equal listen_port")

        try:
            listen_ip = ipaddress.ip_address(self.listen_host)
        except ValueError:
            listen_ip = None
        if listen_ip is not None and listen_ip.is_multicast:
            raise ValueError("listen_host must not be multicast")
        if listen_ip is not None and not listen_ip.is_unspecified and listen_ip != advertised_ip:
            raise ValueError("a specific listen_host IP must equal advertised_endpoint host")

        control_plane_host = urlsplit(self.control_plane_url).hostname
        try:
            control_plane_ip = ipaddress.ip_address(control_plane_host)
        except ValueError:
            control_plane_ip = None
        source_networks = parse_networks(self.control_plane_source_cidrs)
        if control_plane_ip is not None and not any(
            control_plane_ip.version == network.version and control_plane_ip in network
            for network in source_networks
        ):
            raise ValueError(
                "an IP-literal control_plane_url must be included in control_plane_source_cidrs"
            )

        any_loopback = (
            advertised_ip.is_loopback
            or (listen_ip is not None and listen_ip.is_loopback)
            or (control_plane_ip is not None and control_plane_ip.is_loopback)
            or any(network.network_address.is_loopback for network in source_networks)
        )
        if any_loopback and not (
            advertised_ip.is_loopback
            and listen_ip is not None
            and listen_ip.is_loopback
            and control_plane_ip is not None
            and control_plane_ip.is_loopback
            and all(network.network_address.is_loopback for network in source_networks)
        ):
            raise ValueError(
                "loopback networking is allowed only for a complete loopback configuration"
            )
        return self

    @property
    def service_map(self) -> dict[str, AllowlistedService]:
        return {item.local_service_id: item for item in self.services}


def load_agent_config(path: str | Path) -> AgentConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise RuntimeError(f"cannot read Agent configuration: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Agent configuration is not valid JSON: {config_path}") from exc
    try:
        config = AgentConfig.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in exc.errors()})
        # Do not include Pydantic's input_value representation: it may contain cluster_token.
        raise RuntimeError(
            "Agent configuration contains invalid fields: " + ", ".join(fields)
        ) from None
    if not config.database_path.is_absolute():
        config.database_path = (config_path.parent / config.database_path).resolve()
    return config
