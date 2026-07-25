from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import orchestrator.deployment.inventory as inventory_module
from orchestrator.deployment import (
    DEPLOYMENT_SECRET_SENTINEL,
    DeploymentInventoryError,
    DeploymentRenderError,
    load_deployment_inventory,
    prepare_deployment,
)


REPOSITORY = Path(__file__).resolve().parents[1]
EXAMPLE = REPOSITORY / "examples" / "deployment-inventory.example.json"
SANITIZED_INVENTORY_ERROR = "deployment inventory validation failed"


def _example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _write_inventory(
    tmp_path: Path,
    document: object,
    *,
    name: str = "deployment-inventory.json",
    bom: bool = False,
) -> Path:
    payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    path = tmp_path / name
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + payload)
    return path


def _assert_invalid(tmp_path: Path, document: object) -> None:
    path = _write_inventory(tmp_path, document)
    with pytest.raises(DeploymentInventoryError) as raised:
        load_deployment_inventory(path)
    assert str(raised.value) == SANITIZED_INVENTORY_ERROR
    assert raised.value.__cause__ is None


def _service(document: dict[str, Any], service_id: str) -> dict[str, Any]:
    for agent in document["agents"]:
        for service in agent["services"]:
            if service["service_id"] == service_id:
                return service
    raise AssertionError(f"missing fixture service: {service_id}")


def _agent(document: dict[str, Any], node_id: str) -> dict[str, Any]:
    for agent in document["agents"]:
        if agent["node_id"] == node_id:
            return agent
    raise AssertionError(f"missing fixture Agent: {node_id}")


def _canonical_json_bytes(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _rendered_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def test_example_loads_as_strict_three_agent_five_service_inventory() -> None:
    inventory = load_deployment_inventory(EXAMPLE)

    assert inventory.schema_version == 1
    assert inventory.deployment_name == "recovery-lab"
    assert inventory.control_plane.node_id == "control-plane"
    assert [agent.node_id for agent in inventory.agents] == [
        "node-db",
        "node-app",
        "node-web",
    ]
    assert sum(len(agent.services) for agent in inventory.agents) == 5
    assert inventory.acceptance_roles.model_dump() == {
        "mysql": "mysql",
        "redis": "redis",
        "nacos": "nacos",
        "java": "java",
        "nginx": "nginx",
    }


def test_utf8_bom_inventory_is_accepted(tmp_path: Path) -> None:
    path = _write_inventory(tmp_path, _example(), bom=True)

    assert load_deployment_inventory(path).deployment_name == "recovery-lab"


@pytest.mark.parametrize(
    "raw",
    [
        b"{broken-json",
        b"[]",
        b"null",
        b'"not-an-object"',
        b'{"schema_version": NaN}',
        b"\xff\xfe\x00\x00",
    ],
    ids=["malformed", "array", "null", "string", "nan", "invalid-utf8"],
)
def test_invalid_json_roots_and_encodings_fail_with_one_sanitized_error(
    tmp_path: Path,
    raw: bytes,
) -> None:
    path = tmp_path / "LEAK-CANARY-INVENTORY.json"
    path.write_bytes(raw)

    with pytest.raises(DeploymentInventoryError) as raised:
        load_deployment_inventory(path)

    assert str(raised.value) == SANITIZED_INVENTORY_ERROR
    assert "CANARY" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("unknown_root_field",), "LEAK-CANARY"),
        (("control_plane", "unknown_node_field"), True),
        (("agents", 0, "unknown_agent_field"), 1),
        (("agents", 0, "services", 0, "unknown_service_field"), "x"),
        (("agents", 0, "services", 0, "readiness", "unknown_probe_field"), "x"),
        (("recovery_groups", 0, "unknown_group_field"), "x"),
        (("recovery_groups", 0, "dependencies", 0, "unknown_edge_field"), "x"),
        (("acceptance_roles", "unknown_role"), "x"),
        (("schema_version",), "1"),
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("control_plane", "listen_port"), "8766"),
        (("agents", 0, "listen_port"), True),
        (("recovery_groups", 0, "node_settle_window_seconds"), "120"),
        (("recovery_groups", 0, "max_parallel_services"), False),
        (("agents", 0, "services", 0, "readiness", "port"), "3306"),
        (("agents", 0, "services", 0, "readiness", "interval_seconds"), "3"),
    ],
    ids=[
        "unknown-root",
        "unknown-control-plane",
        "unknown-agent",
        "unknown-service",
        "unknown-readiness",
        "unknown-group",
        "unknown-dependency",
        "unknown-role",
        "string-schema-version",
        "bool-schema-version",
        "float-schema-version",
        "string-control-plane-port",
        "bool-agent-port",
        "string-settle-window",
        "bool-max-parallel",
        "string-probe-port",
        "string-probe-interval",
    ],
)
def test_all_inventory_layers_reject_unknown_fields_and_numeric_coercion(
    tmp_path: Path,
    field_path: tuple[str | int, ...],
    value: object,
) -> None:
    document = _example()
    cursor: Any = document
    for segment in field_path[:-1]:
        cursor = cursor[segment]
    cursor[field_path[-1]] = value

    _assert_invalid(tmp_path, document)


def test_inventory_requires_at_least_three_agents(tmp_path: Path) -> None:
    document = _example()
    document["agents"] = document["agents"][:2]

    _assert_invalid(tmp_path, document)


@pytest.mark.parametrize(
    "field_path",
    [
        ("control_plane", "architecture"),
        ("agents", 0, "architecture"),
    ],
    ids=["control-plane", "agent"],
)
def test_inventory_rejects_non_64_bit_nodes(
    tmp_path: Path,
    field_path: tuple[str | int, ...],
) -> None:
    document = _example()
    cursor: Any = document
    for segment in field_path[:-1]:
        cursor = cursor[segment]
    cursor[field_path[-1]] = "x86"

    _assert_invalid(tmp_path, document)


def test_inventory_rejects_more_than_1024_services_in_total(tmp_path: Path) -> None:
    document = _example()
    template = document["agents"][0]["services"][0]
    for index in range(1020):
        service = copy.deepcopy(template)
        service["service_id"] = f"bulk-{index:04d}"
        service["windows_service_name"] = f"BulkService{index:04d}"
        service["display_name"] = f"Bulk Service {index:04d}"
        document["agents"][0]["services"].append(service)

    assert sum(len(agent["services"]) for agent in document["agents"]) == 1025
    _assert_invalid(tmp_path, document)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate-node-id",
        "duplicate-hostname-case-insensitive",
        "duplicate-primary-address",
        "control-plane-address-on-agent-interface",
        "agent-interface-address-on-another-agent",
        "control-plane-data-directory-on-agent",
        "duplicate-global-service-id",
        "duplicate-windows-service-name-case-insensitive",
        "duplicate-group-id",
    ],
)
def test_node_service_and_group_identity_must_be_unambiguous(
    tmp_path: Path,
    mutation: str,
) -> None:
    document = _example()
    agents = document["agents"]
    if mutation == "duplicate-node-id":
        agents[1]["node_id"] = agents[0]["node_id"]
    elif mutation == "duplicate-hostname-case-insensitive":
        agents[1]["hostname"] = agents[0]["hostname"].lower()
    elif mutation == "duplicate-primary-address":
        agents[1]["address"] = agents[0]["address"]
        agents[1]["active_unicast_ips"] = [agents[0]["address"]]
    elif mutation == "control-plane-address-on-agent-interface":
        agents[0]["active_unicast_ips"].append(document["control_plane"]["address"])
    elif mutation == "agent-interface-address-on-another-agent":
        agents[1]["active_unicast_ips"].append(agents[0]["address"])
    elif mutation == "control-plane-data-directory-on-agent":
        agents[0]["data_directory"] = document["control_plane"]["data_directory"]
    elif mutation == "duplicate-global-service-id":
        duplicate = copy.deepcopy(agents[0]["services"][0])
        duplicate["windows_service_name"] = "MySQL80-copy"
        agents[1]["services"].append(duplicate)
    elif mutation == "duplicate-windows-service-name-case-insensitive":
        agents[0]["services"][1]["windows_service_name"] = "mysql80"
    elif mutation == "duplicate-group-id":
        document["recovery_groups"].append(
            copy.deepcopy(document["recovery_groups"][0])
        )
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(mutation)

    _assert_invalid(tmp_path, document)


@pytest.mark.parametrize(
    "data_directory",
    [
        r"ProgramData\WinSW-Recovery\Agent",
        "C:\\",
        r"\\server\share\Agent",
        r"\\?\C:\ProgramData\Agent",
        r"\\.\C:\ProgramData\Agent",
        r"\??\C:\ProgramData\Agent",
        r"C:\ProgramData\..\Agent",
        r"C:\ProgramData\.\Agent",
        r"C:\ProgramData\\Agent",
        "C:\\ProgramData\\Bad\x00Name",
        r"C:\ProgramData\Bad<Name",
        r"C:\ProgramData\Bad>Name",
        'C:\\ProgramData\\Bad"Name',
        r"C:\ProgramData\Bad|Name",
        r"C:\ProgramData\Bad?Name",
        r"C:\ProgramData\Bad*Name",
        r"C:\ProgramData\Bad:Name",
        r"C:\ProgramData\CON\Agent",
        r"C:\ProgramData\aux.txt\Agent",
        r"C:\ProgramData\COM1\Agent",
        r"C:\ProgramData\lpt9.log\Agent",
        "C:\\ProgramData\\Trailing.\\Agent",
        "C:\\ProgramData\\Trailing \\Agent",
    ],
    ids=[
        "relative",
        "drive-root",
        "unc",
        "win32-device-prefix",
        "dos-device-prefix",
        "nt-device-prefix",
        "parent-traversal",
        "dot-component",
        "empty-component",
        "control-character",
        "less-than",
        "greater-than",
        "quote",
        "pipe",
        "question-mark",
        "asterisk",
        "colon-in-component",
        "reserved-con",
        "reserved-aux-with-extension",
        "reserved-com1",
        "reserved-lpt9-with-extension",
        "trailing-dot",
        "trailing-space",
    ],
)
def test_windows_data_directories_reject_unusable_or_escaping_paths(
    tmp_path: Path,
    data_directory: str,
) -> None:
    document = _example()
    document["agents"][0]["data_directory"] = data_directory

    _assert_invalid(tmp_path, document)


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("tcp", "10.20.0.99"),
        ("tcp", "database.internal"),
        ("tcp", "::ffff:10.20.0.11"),
        ("tcp", "fe80::1%7"),
        ("http", "http://10.20.0.99:8080/health"),
        ("http", "http://service.internal:8080/health"),
        ("http", "https://10.20.0.11:8080/health"),
        ("http", "http://user:password@10.20.0.11:8080/health"),
        ("http", "http://10.20.0.11:8080/health#fragment"),
        ("http", "http://10.20.0.11:0/health"),
        ("http", "http://10.20.0.11:/health"),
        ("http", "http://[::ffff:10.20.0.11]:8080/health"),
    ],
)
def test_readiness_targets_cannot_escape_the_owning_agent(
    tmp_path: Path,
    kind: str,
    target: str,
) -> None:
    document = _example()
    readiness = _service(document, "mysql")["readiness"]
    if kind == "tcp":
        readiness["host"] = target
    else:
        readiness.clear()
        readiness.update(
            {
                "kind": "http",
                "url": target,
                "expected_status": 200,
                "timeout_seconds": 2.0,
                "interval_seconds": 3,
                "deadline_seconds": 60,
            }
        )

    _assert_invalid(tmp_path, document)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "10.20.0.11"])
def test_tcp_readiness_accepts_only_explicit_contract_local_targets(
    tmp_path: Path,
    host: str,
) -> None:
    document = _example()
    _service(document, "mysql")["readiness"]["host"] = host
    path = _write_inventory(tmp_path, document)

    assert load_deployment_inventory(path).deployment_name == "recovery-lab"


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-group-service",
        "unknown-edge-service",
        "duplicate-group-service",
        "duplicate-edge",
        "self-edge",
        "cycle",
        "unassigned-inventory-service",
    ],
)
def test_recovery_groups_reject_ambiguous_or_invalid_dependency_graphs(
    tmp_path: Path,
    mutation: str,
) -> None:
    document = _example()
    group = document["recovery_groups"][0]
    if mutation == "unknown-group-service":
        group["service_ids"].append("ghost")
    elif mutation == "unknown-edge-service":
        group["dependencies"].append(
            {
                "dependent_service_id": "ghost",
                "prerequisite_service_id": "mysql",
            }
        )
    elif mutation == "duplicate-group-service":
        group["service_ids"].append("mysql")
    elif mutation == "duplicate-edge":
        group["dependencies"].append(copy.deepcopy(group["dependencies"][0]))
    elif mutation == "self-edge":
        group["dependencies"].append(
            {
                "dependent_service_id": "mysql",
                "prerequisite_service_id": "mysql",
            }
        )
    elif mutation == "cycle":
        group["dependencies"].append(
            {
                "dependent_service_id": "mysql",
                "prerequisite_service_id": "nginx",
            }
        )
    elif mutation == "unassigned-inventory-service":
        group["service_ids"].remove("nginx")
        group["dependencies"] = [
            edge
            for edge in group["dependencies"]
            if "nginx" not in edge.values()
        ]
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(mutation)

    _assert_invalid(tmp_path, document)


@pytest.mark.parametrize(
    ("dependent", "prerequisite"),
    [
        ("nacos", "mysql"),
        ("nacos", "redis"),
        ("java", "nacos"),
        ("nginx", "java"),
    ],
)
def test_all_four_acceptance_edges_are_required_as_direct_dependencies(
    tmp_path: Path,
    dependent: str,
    prerequisite: str,
) -> None:
    document = _example()
    group = document["recovery_groups"][0]
    group["dependencies"] = [
        edge
        for edge in group["dependencies"]
        if not (
            edge["dependent_service_id"] == dependent
            and edge["prerequisite_service_id"] == prerequisite
        )
    ]

    _assert_invalid(tmp_path, document)


def test_transitive_path_cannot_replace_required_direct_acceptance_edge(
    tmp_path: Path,
) -> None:
    document = _example()
    group = document["recovery_groups"][0]
    group["dependencies"] = [
        edge
        for edge in group["dependencies"]
        if not (
            edge["dependent_service_id"] == "nacos"
            and edge["prerequisite_service_id"] == "mysql"
        )
    ]
    group["dependencies"].append(
        {"dependent_service_id": "java", "prerequisite_service_id": "mysql"}
    )

    _assert_invalid(tmp_path, document)


def test_acceptance_roles_must_be_distinct_known_services(tmp_path: Path) -> None:
    duplicate = _example()
    duplicate["acceptance_roles"]["redis"] = "mysql"
    _assert_invalid(tmp_path, duplicate)

    unknown = _example()
    unknown["acceptance_roles"]["redis"] = "ghost"
    _assert_invalid(tmp_path, unknown)


def test_acceptance_roles_and_direct_chain_must_share_one_group(tmp_path: Path) -> None:
    document = _example()
    original = document["recovery_groups"][0]
    document["recovery_groups"] = [
        {
            **copy.deepcopy(original),
            "group_id": "data-tier",
            "service_ids": ["mysql", "redis", "nacos"],
            "dependencies": copy.deepcopy(original["dependencies"][:2]),
        },
        {
            **copy.deepcopy(original),
            "group_id": "application-tier",
            "service_ids": ["java", "nginx"],
            "dependencies": [copy.deepcopy(original["dependencies"][3])],
        },
    ]

    _assert_invalid(tmp_path, document)


def test_prepare_deployment_renders_normalized_fail_closed_tree(tmp_path: Path) -> None:
    inventory_path = _write_inventory(tmp_path, _example())
    output = tmp_path / "prepared"

    result = prepare_deployment(inventory_path, output)

    assert result.output_directory == output.resolve()
    assert result.agent_count == 3
    assert result.service_count == 5
    assert result.recovery_group_count == 1
    assert len(result.manifest_sha256) == 64
    assert set(result.manifest_sha256) <= set("0123456789abcdef")
    assert not list(output.rglob("*.sqlite3"))

    expected_paths = {
        "agents/node-app/agent.json",
        "agents/node-db/agent.json",
        "agents/node-web/agent.json",
        "control-plane/control-plane.json",
        "deployment-manifest.json",
        "recovery-blueprint.json",
    }
    assert set(_rendered_file_bytes(output)) == expected_paths

    cp = json.loads(
        (output / "control-plane" / "control-plane.json").read_text(
            encoding="utf-8"
        )
    )
    assert cp["listen_host"] == "10.20.0.10"
    assert cp["agent_source_cidrs"] == [
        "10.20.0.11/32",
        "10.20.0.12/32",
        "10.20.0.13/32",
    ]
    assert cp["database_path"].endswith("\\control-plane.sqlite3")

    db_agent = json.loads(
        (output / "agents" / "node-db" / "agent.json").read_text(
            encoding="utf-8"
        )
    )
    assert db_agent["control_plane_url"] == "http://10.20.0.10:8766"
    assert db_agent["advertised_endpoint"] == "http://10.20.0.11:8765"
    assert db_agent["control_plane_source_cidrs"] == ["10.20.0.10/32"]
    assert [service["local_service_id"] for service in db_agent["services"]] == [
        "mysql",
        "redis",
    ]

    blueprint = json.loads(
        (output / "recovery-blueprint.json").read_text(encoding="utf-8")
    )
    assert [agent["node_id"] for agent in blueprint["agents"]] == [
        "node-app",
        "node-db",
        "node-web",
    ]
    assert [service["service_id"] for service in blueprint["services"]] == [
        "java",
        "nacos",
        "mysql",
        "redis",
        "nginx",
    ]


def test_every_config_secret_is_the_invalid_sentinel_and_no_usable_secret_exists(
    tmp_path: Path,
) -> None:
    output = tmp_path / "prepared"
    prepare_deployment(EXAMPLE, output)

    cp_path = output / "control-plane" / "control-plane.json"
    cp = json.loads(cp_path.read_text(encoding="utf-8"))
    assert {
        cp["cluster_token"],
        cp["admin_password_hash"],
        cp["session_secret"],
    } == {DEPLOYMENT_SECRET_SENTINEL}

    agent_paths = sorted((output / "agents").glob("*/agent.json"))
    assert len(agent_paths) == 3
    for path in agent_paths:
        assert json.loads(path.read_text(encoding="utf-8"))["cluster_token"] == (
            DEPLOYMENT_SECRET_SENTINEL
        )

    assert DEPLOYMENT_SECRET_SENTINEL not in (
        output / "recovery-blueprint.json"
    ).read_text(encoding="utf-8")
    assert DEPLOYMENT_SECRET_SENTINEL not in (
        output / "deployment-manifest.json"
    ).read_text(encoding="utf-8")


def test_manifest_binds_exact_inventory_and_every_non_manifest_file(
    tmp_path: Path,
) -> None:
    inventory_path = _write_inventory(tmp_path, _example())
    output = tmp_path / "prepared"
    result = prepare_deployment(inventory_path, output)

    manifest_path = output / "deployment-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest == {
        "schema_version": 1,
        "deployment_name": "recovery-lab",
        "config_ready": False,
        "inventory_sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
        "files": manifest["files"],
    }
    assert result.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()

    paths = [entry["path"] for entry in manifest["files"]]
    assert paths == sorted(paths)
    assert "deployment-manifest.json" not in paths
    assert all("\\" not in path for path in paths)
    assert set(paths) == {
        "agents/node-app/agent.json",
        "agents/node-db/agent.json",
        "agents/node-web/agent.json",
        "control-plane/control-plane.json",
        "recovery-blueprint.json",
    }
    for entry in manifest["files"]:
        payload = (output / Path(entry["path"])).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]

    for path in output.rglob("*.json"):
        payload = path.read_bytes()
        assert payload.endswith(b"\n")
        assert payload == _canonical_json_bytes(json.loads(payload))


def test_repeated_render_of_same_input_is_byte_for_byte_deterministic(
    tmp_path: Path,
) -> None:
    inventory_path = _write_inventory(tmp_path, _example())
    first = tmp_path / "prepared-a"
    second = tmp_path / "prepared-b"

    first_result = prepare_deployment(inventory_path, first)
    second_result = prepare_deployment(inventory_path, second)

    assert _rendered_file_bytes(first) == _rendered_file_bytes(second)
    assert first_result.manifest_sha256 == second_result.manifest_sha256


def test_logically_identical_reordered_inventory_renders_identical_content(
    tmp_path: Path,
) -> None:
    original = _example()
    reordered = copy.deepcopy(original)
    reordered["agents"].reverse()
    for agent in reordered["agents"]:
        agent["services"].reverse()
        agent["active_unicast_ips"].reverse()
    reordered["recovery_groups"].reverse()
    for group in reordered["recovery_groups"]:
        group["service_ids"].reverse()
        group["dependencies"].reverse()

    original_path = _write_inventory(tmp_path, original, name="a.json")
    reordered_path = _write_inventory(tmp_path, reordered, name="b.json")
    first = tmp_path / "prepared-a"
    second = tmp_path / "prepared-b"

    prepare_deployment(original_path, first)
    prepare_deployment(reordered_path, second)

    first_files = _rendered_file_bytes(first)
    second_files = _rendered_file_bytes(second)
    first_manifest = json.loads(first_files.pop("deployment-manifest.json"))
    second_manifest = json.loads(second_files.pop("deployment-manifest.json"))

    assert first_files == second_files
    assert first_manifest["files"] == second_manifest["files"]
    assert first_manifest["inventory_sha256"] != second_manifest["inventory_sha256"]


def test_existing_output_directory_is_never_merged_or_modified(tmp_path: Path) -> None:
    output = tmp_path / "operator-owned"
    output.mkdir()
    marker = output / "preserve.txt"
    marker.write_text("operator data", encoding="utf-8")

    with pytest.raises(DeploymentRenderError):
        prepare_deployment(EXAMPLE, output)

    assert marker.read_text(encoding="utf-8") == "operator data"
    assert list(output.iterdir()) == [marker]
    assert not list(tmp_path.glob(f".{output.name}.tmp-*"))


def test_render_failure_leaves_no_target_or_temporary_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "must-not-exist"
    original_write = inventory_module._write_json
    writes = 0

    def fail_after_first_write(path: Path, document: dict[str, Any]) -> bytes:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected render interruption")
        return original_write(path, document)

    monkeypatch.setattr(inventory_module, "_write_json", fail_after_first_write)

    with pytest.raises(DeploymentRenderError) as raised:
        prepare_deployment(EXAMPLE, output)

    assert str(raised.value) == "deployment rendering failed"
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.tmp-*"))


def test_temporary_cleanup_failure_is_never_silently_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "must-not-exist"
    original_write = inventory_module._write_json
    writes = 0

    def fail_after_first_write(path: Path, document: dict[str, Any]) -> bytes:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected render interruption")
        return original_write(path, document)

    monkeypatch.setattr(inventory_module, "_write_json", fail_after_first_write)
    monkeypatch.setattr(inventory_module.shutil, "rmtree", lambda _path: None)

    with pytest.raises(DeploymentRenderError) as raised:
        prepare_deployment(EXAMPLE, output)

    assert str(raised.value) == "deployment temporary directory cleanup failed"
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.tmp-*"))
