from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from orchestrator.common.errors import ApiError
from orchestrator.common.graph import DependencyCycleError, topological_levels
from orchestrator.common.models import (
    MAX_DEPENDENCY_ITEMS,
    MAX_SERVICE_ITEMS,
    SERVICE_SLUG_PATTERN,
    HttpReadinessWrite,
    ReadinessWrite,
    ScmReadinessWrite,
    TcpReadinessWrite,
)
from orchestrator.common.probe_targets import (
    validate_http_probe_url,
    validate_local_probe_host,
)


DEPLOYMENT_SECRET_SENTINEL = "REQUIRED-GENERATE-ON-TARGET"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:\\")
_WINDOWS_INVALID_COMPONENT_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


class DeploymentInventoryError(RuntimeError):
    """A deliberately sanitized Inventory load or validation failure."""


class DeploymentRenderError(RuntimeError):
    """A deployment tree could not be published atomically."""


class ProbeTargetValidator(Protocol):
    """Injectable boundary around the Agent's shared pure target validators."""

    def validate_host(self, host: str, local_addresses: Iterable[str]) -> str: ...

    def validate_http_url(
        self,
        url: str,
        local_addresses: Iterable[str],
    ) -> tuple[str, int, str]: ...


@dataclass(frozen=True, slots=True)
class _SharedProbeTargetValidator:
    validate_host: Callable[[str, Iterable[str]], str] = validate_local_probe_host
    validate_http_url: Callable[
        [str, Iterable[str]], tuple[str, int, str]
    ] = validate_http_probe_url


DEFAULT_PROBE_TARGET_VALIDATOR: ProbeTargetValidator = _SharedProbeTargetValidator()


class _StrictInventoryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
    )


def _no_surrounding_whitespace(value: str) -> str:
    if value != value.strip():
        raise ValueError("value must not contain surrounding whitespace")
    return value


def _canonical_host_address(value: str) -> str:
    if "%" in value:
        raise ValueError("address must not contain a zone identifier")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("address must be a canonical IP literal") from exc
    if str(address) != value:
        raise ValueError("address must use its canonical IP literal form")
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
        or (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
        )
    ):
        raise ValueError("address must be a non-local unicast IP literal")
    return value


def _windows_data_directory(value: str) -> str:
    if not value or any(ord(character) < 32 for character in value):
        raise ValueError("data_directory must not be empty or contain control characters")
    normalized = value.replace("/", "\\")
    lowered = normalized.casefold()
    if (
        normalized.startswith("\\\\")
        or lowered.startswith("\\??\\")
        or lowered.startswith("\\\\?\\")
        or lowered.startswith("\\\\.\\")
        or not _WINDOWS_DRIVE_ABSOLUTE_RE.match(normalized)
    ):
        raise ValueError("data_directory must be a local absolute Windows drive path")
    remainder = normalized[3:]
    if not remainder:
        raise ValueError("data_directory must not be a drive root")
    parts = remainder.split("\\")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("data_directory must not contain empty or traversal components")
    for part in parts:
        if any(character in _WINDOWS_INVALID_COMPONENT_CHARACTERS for character in part):
            raise ValueError("data_directory contains an invalid Windows path character")
        if part.endswith((" ", ".")):
            raise ValueError("data_directory components must not end with space or dot")
        device_stem = part.split(".", 1)[0].casefold()
        if device_stem in _WINDOWS_RESERVED_DEVICE_NAMES:
            raise ValueError("data_directory contains a reserved Windows device name")
    return normalized[0].upper() + normalized[1:]


class _NodeInventory(_StrictInventoryModel):
    node_id: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=255)
    windows_version: str = Field(min_length=1, max_length=160)
    architecture: str = Field(min_length=1, max_length=64)
    address: str
    listen_port: int = Field(strict=True, ge=1, le=65535)
    data_directory: str

    @field_validator("hostname")
    @classmethod
    def hostname_is_not_padded(cls, value: str) -> str:
        return _no_surrounding_whitespace(value)

    @field_validator("architecture")
    @classmethod
    def architecture_is_64_bit(cls, value: str) -> str:
        if "64" not in value:
            raise ValueError("architecture must describe a 64-bit host")
        return value

    @field_validator("address")
    @classmethod
    def address_is_canonical_unicast(cls, value: str) -> str:
        return _canonical_host_address(value)

    @field_validator("data_directory")
    @classmethod
    def data_directory_is_local_absolute(cls, value: str) -> str:
        return _windows_data_directory(value)


class ControlPlaneInventory(_NodeInventory):
    listen_port: int = Field(default=8766, strict=True, ge=1, le=65535)


class ServiceInventory(_StrictInventoryModel):
    service_id: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    windows_service_name: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    startup_mode: Literal["Manual"]
    readiness: ReadinessWrite

    @field_validator("windows_service_name")
    @classmethod
    def windows_service_name_is_not_padded(cls, value: str) -> str:
        return _no_surrounding_whitespace(value)


class AgentInventory(_NodeInventory):
    active_unicast_ips: list[str] = Field(min_length=1, max_length=64)
    listen_port: int = Field(default=8765, strict=True, ge=1, le=65535)
    services: list[ServiceInventory] = Field(min_length=1, max_length=MAX_SERVICE_ITEMS)

    @field_validator("active_unicast_ips")
    @classmethod
    def active_addresses_are_canonical_and_unique(cls, values: list[str]) -> list[str]:
        validated = [_canonical_host_address(value) for value in values]
        if len(validated) != len(set(validated)):
            raise ValueError("active_unicast_ips must be unique")
        return validated

    @model_validator(mode="after")
    def address_and_services_are_consistent(self) -> AgentInventory:
        if self.address not in self.active_unicast_ips:
            raise ValueError("active_unicast_ips must contain the Agent address")
        local_ids = [service.service_id for service in self.services]
        if len(local_ids) != len(set(local_ids)):
            raise ValueError("service_id values must be unique within an Agent")
        windows_names = [service.windows_service_name.casefold() for service in self.services]
        if len(windows_names) != len(set(windows_names)):
            raise ValueError(
                "windows_service_name values must be unique within an Agent (case-insensitive)"
            )
        return self


class DependencyInventory(_StrictInventoryModel):
    dependent_service_id: str = Field(
        pattern=SERVICE_SLUG_PATTERN,
        min_length=1,
        max_length=64,
    )
    prerequisite_service_id: str = Field(
        pattern=SERVICE_SLUG_PATTERN,
        min_length=1,
        max_length=64,
    )

    @model_validator(mode="after")
    def not_self_referential(self) -> DependencyInventory:
        if self.dependent_service_id == self.prerequisite_service_id:
            raise ValueError("a service cannot depend on itself")
        return self


class RecoveryGroupInventory(_StrictInventoryModel):
    group_id: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    node_settle_window_seconds: int = Field(default=120, strict=True, ge=1, le=3600)
    max_parallel_services: int = Field(default=4, strict=True, ge=1, le=4)
    service_ids: list[str] = Field(min_length=1, max_length=MAX_SERVICE_ITEMS)
    dependencies: list[DependencyInventory] = Field(max_length=MAX_DEPENDENCY_ITEMS)

    @field_validator("service_ids")
    @classmethod
    def service_ids_are_slugs_and_unique(cls, values: list[str]) -> list[str]:
        adapter = re.compile(SERVICE_SLUG_PATTERN)
        if any(not 1 <= len(value) <= 64 or adapter.fullmatch(value) is None for value in values):
            raise ValueError("service_ids must contain only valid service slugs")
        if len(values) != len(set(values)):
            raise ValueError("service_ids must be unique")
        return values

    @model_validator(mode="after")
    def dependencies_are_unique_and_acyclic(self) -> RecoveryGroupInventory:
        members = set(self.service_ids)
        edges = [
            (dependency.dependent_service_id, dependency.prerequisite_service_id)
            for dependency in self.dependencies
        ]
        if len(edges) != len(set(edges)):
            raise ValueError("dependency edges must be unique")
        if any(dependent not in members or prerequisite not in members for dependent, prerequisite in edges):
            raise ValueError("dependency edges must reference services in the Group")
        try:
            topological_levels(members, edges)
        except (DependencyCycleError, ValueError) as exc:
            raise ValueError("Group dependencies must form an acyclic graph") from exc
        return self


class AcceptanceRoles(_StrictInventoryModel):
    mysql: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    redis: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    nacos: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    java: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)
    nginx: str = Field(pattern=SERVICE_SLUG_PATTERN, min_length=1, max_length=64)

    @model_validator(mode="after")
    def role_services_are_unique(self) -> AcceptanceRoles:
        values = list(self.model_dump().values())
        if len(values) != len(set(values)):
            raise ValueError("acceptance role services must be distinct")
        return self


class DeploymentInventory(_StrictInventoryModel):
    schema_version: Literal[1]
    deployment_name: str = Field(
        pattern=SERVICE_SLUG_PATTERN,
        min_length=1,
        max_length=64,
    )
    control_plane: ControlPlaneInventory
    agents: list[AgentInventory] = Field(min_length=3, max_length=MAX_SERVICE_ITEMS)
    recovery_groups: list[RecoveryGroupInventory] = Field(
        min_length=1,
        max_length=MAX_SERVICE_ITEMS,
    )
    acceptance_roles: AcceptanceRoles

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_a_strict_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_inventory_graph(self, info: ValidationInfo) -> DeploymentInventory:
        node_ids = [self.control_plane.node_id, *(agent.node_id for agent in self.agents)]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("node_id values must be globally unique")

        hostnames = [
            self.control_plane.hostname.casefold(),
            *(agent.hostname.casefold() for agent in self.agents),
        ]
        if len(hostnames) != len(set(hostnames)):
            raise ValueError("hostname values must be globally unique (case-insensitive)")

        primary_addresses = [self.control_plane.address, *(agent.address for agent in self.agents)]
        if len(primary_addresses) != len(set(primary_addresses)):
            raise ValueError("node address values must be globally unique")
        agent_interface_addresses = [
            address for agent in self.agents for address in agent.active_unicast_ips
        ]
        if len(agent_interface_addresses) != len(set(agent_interface_addresses)):
            raise ValueError(
                "active_unicast_ips values must be globally unique across Agents"
            )
        if self.control_plane.address in agent_interface_addresses:
            raise ValueError("Control Plane address must not be bound to an Agent")

        cp_data_directory = self.control_plane.data_directory.casefold()
        if any(agent.data_directory.casefold() == cp_data_directory for agent in self.agents):
            raise ValueError("Control Plane data_directory must differ from every Agent")

        services = [service for agent in self.agents for service in agent.services]
        if len(services) > MAX_SERVICE_ITEMS:
            raise ValueError(f"Inventory services must not exceed {MAX_SERVICE_ITEMS}")
        service_ids = [service.service_id for service in services]
        if len(service_ids) != len(set(service_ids)):
            raise ValueError("service_id values must be globally unique")
        service_id_set = set(service_ids)

        group_ids = [group.group_id for group in self.recovery_groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("group_id values must be unique")
        referenced_services: set[str] = set()
        for group in self.recovery_groups:
            group_services = set(group.service_ids)
            if not group_services <= service_id_set:
                raise ValueError("Recovery Group references an unknown service_id")
            referenced_services.update(group_services)
        if referenced_services != service_id_set:
            raise ValueError("every Inventory service must belong to at least one Recovery Group")

        roles = cast(dict[str, str], self.acceptance_roles.model_dump())
        if not set(roles.values()) <= service_id_set:
            raise ValueError("acceptance_roles must reference Inventory services")
        required_edges = {
            (roles["nacos"], roles["mysql"]),
            (roles["nacos"], roles["redis"]),
            (roles["java"], roles["nacos"]),
            (roles["nginx"], roles["java"]),
        }
        role_values = set(roles.values())
        if not any(
            role_values <= set(group.service_ids)
            and required_edges
            <= {
                (edge.dependent_service_id, edge.prerequisite_service_id)
                for edge in group.dependencies
            }
            for group in self.recovery_groups
        ):
            raise ValueError(
                "acceptance role services and required direct dependencies must share a Group"
            )

        context = info.context if isinstance(info.context, Mapping) else {}
        target_validator = cast(
            ProbeTargetValidator,
            context.get("probe_target_validator", DEFAULT_PROBE_TARGET_VALIDATOR),
        )
        for agent in self.agents:
            for service in agent.services:
                readiness = service.readiness
                try:
                    if isinstance(readiness, TcpReadinessWrite):
                        target_validator.validate_host(readiness.host, agent.active_unicast_ips)
                    elif isinstance(readiness, HttpReadinessWrite):
                        target_validator.validate_http_url(
                            readiness.url,
                            agent.active_unicast_ips,
                        )
                    elif not isinstance(readiness, ScmReadinessWrite):  # pragma: no cover
                        raise ValueError("unsupported readiness kind")
                except (ApiError, ValueError) as exc:
                    raise ValueError(
                        f"readiness target for service_id {service.service_id!r} is not local"
                    ) from exc
        return self


@dataclass(frozen=True, slots=True)
class DeploymentRenderResult:
    output_directory: Path
    manifest_sha256: str
    agent_count: int
    service_count: int
    recovery_group_count: int


JsonObject: TypeAlias = dict[str, Any]


def load_deployment_inventory(
    path: str | Path,
    *,
    target_validator: ProbeTargetValidator | None = None,
) -> DeploymentInventory:
    raw = _read_inventory_bytes(path)
    return _parse_inventory(raw, target_validator=target_validator)


def prepare_deployment(
    inventory_path: str | Path,
    output_directory: str | Path,
    *,
    target_validator: ProbeTargetValidator | None = None,
) -> DeploymentRenderResult:
    """Load, validate, fully render, and atomically publish a deployment tree."""

    raw = _read_inventory_bytes(inventory_path)
    inventory = _parse_inventory(raw, target_validator=target_validator)
    return _render_deployment(
        inventory,
        output_directory,
        inventory_sha256=hashlib.sha256(raw).hexdigest(),
        target_validator=target_validator,
    )


def _render_deployment(
    inventory: DeploymentInventory,
    output_directory: str | Path,
    *,
    inventory_sha256: str,
    target_validator: ProbeTargetValidator | None = None,
) -> DeploymentRenderResult:
    """Render to a sibling temporary directory and rename only after success."""

    if not _SHA256_RE.fullmatch(inventory_sha256):
        raise DeploymentRenderError(
            "inventory_sha256 must be a lowercase SHA-256 digest"
        )
    try:
        validated = DeploymentInventory.model_validate(
            inventory.model_dump(mode="python"),
            context={
                "probe_target_validator": target_validator
                or DEFAULT_PROBE_TARGET_VALIDATOR
            },
        )
    except (ValidationError, ValueError) as exc:
        raise DeploymentRenderError("deployment inventory validation failed") from exc

    target = Path(output_directory).expanduser().resolve()
    parent = target.parent
    if target.exists() or target.is_symlink():
        raise DeploymentRenderError("output directory must not already exist")
    if not parent.is_dir():
        raise DeploymentRenderError("output directory parent must already exist")

    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=parent))
    published = False
    try:
        rendered_files = _render_files(validated)
        hashes: list[dict[str, str]] = []
        for relative_path, document in sorted(rendered_files.items()):
            destination = temporary.joinpath(*relative_path.split("/"))
            payload = _write_json(destination, document)
            hashes.append(
                {
                    "path": relative_path,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        manifest: JsonObject = {
            "schema_version": 1,
            "deployment_name": validated.deployment_name,
            "config_ready": False,
            "inventory_sha256": inventory_sha256,
            "files": hashes,
        }
        manifest_payload = _write_json(
            temporary / "deployment-manifest.json",
            manifest,
        )
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()

        # A second existence check narrows the publication race.  os.rename is
        # atomic on the target Windows filesystem and does not merge trees.
        if target.exists() or target.is_symlink():
            raise DeploymentRenderError("output directory appeared during rendering")
        os.rename(temporary, target)
        published = True
        return DeploymentRenderResult(
            output_directory=target,
            manifest_sha256=manifest_sha256,
            agent_count=len(validated.agents),
            service_count=sum(len(agent.services) for agent in validated.agents),
            recovery_group_count=len(validated.recovery_groups),
        )
    except DeploymentRenderError:
        raise
    except Exception as exc:
        raise DeploymentRenderError("deployment rendering failed") from exc
    finally:
        if not published and temporary.exists():
            try:
                shutil.rmtree(temporary)
            except OSError as exc:
                raise DeploymentRenderError(
                    "deployment temporary directory cleanup failed"
                ) from exc
            if temporary.exists():
                raise DeploymentRenderError(
                    "deployment temporary directory cleanup failed"
                )


def _read_inventory_bytes(path: str | Path) -> bytes:
    try:
        return Path(path).expanduser().resolve().read_bytes()
    except OSError:
        raise DeploymentInventoryError("deployment inventory validation failed") from None


def _parse_inventory(
    raw: bytes,
    *,
    target_validator: ProbeTargetValidator | None,
) -> DeploymentInventory:
    try:
        text = raw.decode("utf-8-sig")
        parsed = json.loads(
            text,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
        if not isinstance(parsed, dict):
            raise ValueError("Inventory root is not an object")
        return DeploymentInventory.model_validate(
            parsed,
            context={
                "probe_target_validator": target_validator
                or DEFAULT_PROBE_TARGET_VALIDATOR
            },
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
        raise DeploymentInventoryError("deployment inventory validation failed") from None


def _render_files(inventory: DeploymentInventory) -> dict[str, JsonObject]:
    cp_address = ipaddress.ip_address(inventory.control_plane.address)
    cp_config: JsonObject = {
        "listen_host": inventory.control_plane.address,
        "listen_port": inventory.control_plane.listen_port,
        "database_path": _database_path(
            inventory.control_plane.data_directory,
            "control-plane.sqlite3",
        ),
        "cluster_token": DEPLOYMENT_SECRET_SENTINEL,
        "agent_source_cidrs": sorted(
            (_host_prefix(agent.address) for agent in inventory.agents),
            key=_network_sort_key,
        ),
        "offline_after_seconds": 45,
        "agent_request_timeout_seconds": 10.0,
        "agent_request_attempts": 3,
        "admin_username": "admin",
        "admin_password_hash": DEPLOYMENT_SECRET_SENTINEL,
        "session_secret": DEPLOYMENT_SECRET_SENTINEL,
        "session_cookie_name": "recovery_admin_session",
        "session_max_age_seconds": 28_800,
    }

    files: dict[str, JsonObject] = {
        "control-plane/control-plane.json": cp_config,
    }
    for agent in sorted(inventory.agents, key=lambda item: item.node_id):
        agent_address = ipaddress.ip_address(agent.address)
        files[f"agents/{agent.node_id}/agent.json"] = {
            "control_plane_url": _http_endpoint(
                cp_address,
                inventory.control_plane.listen_port,
            ),
            "listen_host": agent.address,
            "listen_port": agent.listen_port,
            "advertised_endpoint": _http_endpoint(agent_address, agent.listen_port),
            "cluster_token": DEPLOYMENT_SECRET_SENTINEL,
            "control_plane_source_cidrs": [_host_prefix(inventory.control_plane.address)],
            "database_path": _database_path(agent.data_directory, "agent.sqlite3"),
            "heartbeat_interval_seconds": 10.0,
            "heartbeat_jitter_ratio": 0.2,
            "heartbeat_backoff_initial_seconds": 2.0,
            "heartbeat_backoff_max_seconds": 60.0,
            "action_timeout_seconds": 60.0,
            "action_poll_interval_seconds": 0.5,
            "services": [
                {
                    "local_service_id": service.service_id,
                    "windows_service_name": service.windows_service_name,
                    "display_name": service.display_name,
                }
                for service in sorted(agent.services, key=lambda item: item.service_id)
            ],
        }

    files["recovery-blueprint.json"] = _recovery_blueprint(inventory)
    return files


def _recovery_blueprint(inventory: DeploymentInventory) -> JsonObject:
    return {
        "schema_version": 1,
        "deployment_name": inventory.deployment_name,
        "control_plane": inventory.control_plane.model_dump(mode="json"),
        "agents": [
            {
                "node_id": agent.node_id,
                "hostname": agent.hostname,
                "windows_version": agent.windows_version,
                "architecture": agent.architecture,
                "address": agent.address,
                "active_unicast_ips": sorted(
                    agent.active_unicast_ips,
                    key=_address_sort_key,
                ),
                "listen_port": agent.listen_port,
                "data_directory": agent.data_directory,
            }
            for agent in sorted(inventory.agents, key=lambda item: item.node_id)
        ],
        "services": [
            {
                "agent_node_id": agent.node_id,
                "service_id": service.service_id,
                "windows_service_name": service.windows_service_name,
                "display_name": service.display_name,
                "startup_mode": service.startup_mode,
                "readiness": service.readiness.model_dump(mode="json"),
            }
            for agent in sorted(inventory.agents, key=lambda item: item.node_id)
            for service in sorted(agent.services, key=lambda item: item.service_id)
        ],
        "recovery_groups": [
            {
                **group.model_dump(mode="json", exclude={"service_ids", "dependencies"}),
                "service_ids": sorted(group.service_ids),
                "dependencies": sorted(
                    (
                        dependency.model_dump(mode="json")
                        for dependency in group.dependencies
                    ),
                    key=lambda edge: (
                        edge["dependent_service_id"],
                        edge["prerequisite_service_id"],
                    ),
                ),
            }
            for group in sorted(inventory.recovery_groups, key=lambda item: item.group_id)
        ],
        "acceptance_roles": inventory.acceptance_roles.model_dump(mode="json"),
    }


def _write_json(path: Path, document: JsonObject) -> bytes:
    payload = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _database_path(directory: str, filename: str) -> str:
    return directory.rstrip("\\") + "\\" + filename


def _host_prefix(address: str) -> str:
    parsed = ipaddress.ip_address(address)
    return f"{parsed}/{parsed.max_prefixlen}"


def _http_endpoint(address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int) -> str:
    host = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    return f"http://{host}:{port}"


def _address_sort_key(address: str) -> tuple[int, int]:
    parsed = ipaddress.ip_address(address)
    return parsed.version, int(parsed)


def _network_sort_key(network: str) -> tuple[int, int]:
    parsed = ipaddress.ip_network(network, strict=True)
    return parsed.version, int(parsed.network_address)
