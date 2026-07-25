from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from orchestrator.agent.config import AgentConfig, load_agent_config


BASE = {
    "control_plane_url": "http://127.0.0.1:9000",
    "listen_host": "127.0.0.1",
    "listen_port": 8765,
    "advertised_endpoint": "http://127.0.0.1:8765",
    "cluster_token": "0123456789abcdef0123456789abcdef",
    "control_plane_source_cidrs": ["127.0.0.1/32"],
    "database_path": "data/agent.sqlite3",
    "services": [
        {
            "local_service_id": "mysql",
            "windows_service_name": "MySQL80",
            "display_name": "MySQL 8",
        }
    ],
}


def test_json_config_is_strict_and_resolves_database_relative_to_config(tmp_path) -> None:
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(BASE), encoding="utf-8")
    config = load_agent_config(path)
    assert config.database_path == (tmp_path / "data" / "agent.sqlite3").resolve()
    assert list(config.service_map) == ["mysql"]

    invalid = dict(BASE, unexpected=True)
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected"):
        load_agent_config(path)


def test_config_rejects_duplicate_service_mapping_and_non_ip_endpoint() -> None:
    duplicate = dict(
        BASE,
        services=[
            BASE["services"][0],
            {"local_service_id": "mysql-copy", "windows_service_name": "mysql80"},
        ],
    )
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(duplicate)

    invalid_endpoint = dict(BASE, advertised_endpoint="http://agent.example.test:8765")
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(invalid_endpoint)

    invalid_slug = dict(
        BASE,
        services=[dict(BASE["services"][0], local_service_id="1mysql")],
    )
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(invalid_slug)

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(dict(BASE, control_plane_source_cidrs=["0.0.0.0/0"]))


def test_config_accepts_consistent_lan_and_complete_loopback_networking() -> None:
    loopback = AgentConfig.model_validate(BASE)
    assert loopback.listen_host == "127.0.0.1"

    lan = AgentConfig.model_validate(
        dict(
            BASE,
            control_plane_url="http://10.20.0.10:8766",
            listen_host="0.0.0.0",
            advertised_endpoint="http://10.20.0.11:8765",
            control_plane_source_cidrs=["10.20.0.10/32"],
        )
    )
    assert lan.advertised_endpoint == "http://10.20.0.11:8765"


@pytest.mark.parametrize(
    "overrides",
    [
        {"advertised_endpoint": "http://127.0.0.1:9999"},
        {
            "control_plane_url": "http://10.20.0.10:8766",
            "listen_host": "0.0.0.0",
            "advertised_endpoint": "http://0.0.0.0:8765",
            "control_plane_source_cidrs": ["10.20.0.10/32"],
        },
        {
            "control_plane_url": "http://10.20.0.10:8766",
            "listen_host": "0.0.0.0",
            "advertised_endpoint": "http://224.0.0.1:8765",
            "control_plane_source_cidrs": ["10.20.0.10/32"],
        },
        {
            "control_plane_url": "http://10.20.0.10:8766",
            "listen_host": "10.20.0.12",
            "advertised_endpoint": "http://10.20.0.11:8765",
            "control_plane_source_cidrs": ["10.20.0.10/32"],
        },
        {
            "control_plane_url": "http://10.20.0.10:8766",
            "listen_host": "0.0.0.0",
            "advertised_endpoint": "http://10.20.0.11:8765",
            "control_plane_source_cidrs": ["10.20.0.99/32"],
        },
        {"listen_host": "0.0.0.0"},
        {
            "control_plane_url": "http://10.20.0.10:8766",
            "listen_host": "127.0.0.1",
            "advertised_endpoint": "http://127.0.0.1:8765",
            "control_plane_source_cidrs": ["10.20.0.10/32"],
        },
    ],
)
def test_config_rejects_inconsistent_or_unsafe_networking(overrides) -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(dict(BASE, **overrides))


def test_loader_does_not_echo_invalid_cluster_token(tmp_path) -> None:
    secret = "SECRET-DO-NOT-ECHO"
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(dict(BASE, cluster_token=secret)), encoding="utf-8")
    with pytest.raises(RuntimeError) as raised:
        load_agent_config(path)
    assert secret not in str(raised.value)
