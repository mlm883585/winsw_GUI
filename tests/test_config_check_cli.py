from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest

from orchestrator.agent.config import AllowlistedService
from orchestrator.agent.store import AgentStore
from orchestrator.common.security import hash_password, verify_password
from orchestrator.control_plane.config import ControlPlaneConfig, load_control_plane_config


REPOSITORY = Path(__file__).resolve().parents[1]
TEST_CLUSTER_TOKEN = "test-cluster-token-with-at-least-32-bytes-0001"
TEST_SESSION_SECRET = "test-session-secret-with-at-least-32-bytes-0002"


def _valid_example(example_name: str) -> dict[str, object]:
    raw = json.loads((REPOSITORY / "examples" / example_name).read_text(encoding="utf-8"))
    raw["cluster_token"] = TEST_CLUSTER_TOKEN
    if example_name == "control-plane.example.json":
        raw["session_secret"] = TEST_SESSION_SECRET
        raw["admin_password_hash"] = hash_password(
            "test-password", salt=b"0123456789abcdef"
        )
    return raw


def _run_check(module: str, config_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            module,
            "--config",
            str(config_path),
            "--check-config",
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _run_prepare(
    inventory_path: Path, output_dir: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "orchestrator.control_plane",
            "--prepare-deployment",
            str(inventory_path),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    ("module", "example_name", "expected"),
    [
        (
            "orchestrator.agent",
            "agent.example.json",
            {"component": "agent", "config_valid": True, "service_count": 2},
        ),
        (
            "orchestrator.control_plane",
            "control-plane.example.json",
            {"component": "control-plane", "config_valid": True},
        ),
    ],
)
def test_check_config_accepts_valid_config_without_echoing_secrets(
    tmp_path: Path,
    module: str,
    example_name: str,
    expected: dict[str, object],
) -> None:
    raw = _valid_example(example_name)
    raw["database_path"] = str(tmp_path / "must-not-be-created.sqlite3")
    config_path = tmp_path / example_name
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    result = _run_check(module, config_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
    assert result.stderr == ""
    assert not Path(raw["database_path"]).exists()
    combined = result.stdout + result.stderr
    for key in ("cluster_token", "session_secret", "admin_password_hash"):
        secret = raw.get(key)
        if secret:
            assert secret not in combined


@pytest.mark.parametrize(
    ("module", "example_name", "secret_keys"),
    [
        ("orchestrator.agent", "agent.example.json", ("cluster_token",)),
        (
            "orchestrator.control_plane",
            "control-plane.example.json",
            ("cluster_token", "session_secret", "admin_password_hash"),
        ),
    ],
)
def test_check_config_failure_is_nonzero_and_redacted(
    tmp_path: Path,
    module: str,
    example_name: str,
    secret_keys: tuple[str, ...],
) -> None:
    raw = _valid_example(example_name)
    secrets = {key: f"LEAK-CANARY-{key}-VALUE" for key in secret_keys}
    raw.update(secrets)
    raw["unknown_field"] = "force strict validation failure"
    config_path = tmp_path / example_name
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    result = _run_check(module, config_path)

    assert result.returncode == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload == {
        "component": "agent" if module.endswith(".agent") else "control-plane",
        "config_valid": False,
        "error": "configuration validation failed",
    }
    combined = result.stdout + result.stderr
    for secret in secrets.values():
        assert secret not in combined


@pytest.mark.parametrize(
    "module",
    ("orchestrator.agent", "orchestrator.control_plane"),
)
def test_check_config_missing_file_is_redacted(module: str, tmp_path: Path) -> None:
    missing_path = tmp_path / "MISSING-WITHOUT-SECRET.json"

    result = _run_check(module, missing_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["config_valid"] is False
    assert str(missing_path) not in result.stderr


@pytest.mark.parametrize(
    ("module", "example_name"),
    [
        ("orchestrator.agent", "agent.example.json"),
        ("orchestrator.control_plane", "control-plane.example.json"),
    ],
)
def test_check_config_accepts_windows_utf8_bom(
    module: str, example_name: str, tmp_path: Path
) -> None:
    source = json.dumps(_valid_example(example_name))
    config_path = tmp_path / example_name
    config_path.write_text(source, encoding="utf-8-sig")

    result = _run_check(module, config_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["config_valid"] is True


@pytest.mark.parametrize(
    ("module", "example_name"),
    [
        ("orchestrator.agent", "agent.example.json"),
        ("orchestrator.control_plane", "control-plane.example.json"),
    ],
)
def test_unedited_examples_fail_authoritative_config_check(
    module: str, example_name: str
) -> None:
    result = _run_check(module, REPOSITORY / "examples" / example_name)
    assert result.returncode == 2
    assert json.loads(result.stderr)["config_valid"] is False


def test_control_plane_config_rejects_wide_source_and_redacts_normal_start(
    tmp_path: Path,
) -> None:
    raw = _valid_example("control-plane.example.json")
    raw["agent_source_cidrs"] = ["0.0.0.0/0"]
    canary = "CANARY-SESSION-SECRET-THAT-MUST-NOT-LEAK-0001"
    raw["session_secret"] = canary
    config_path = tmp_path / "control-plane.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RuntimeError) as raised:
        load_control_plane_config(config_path)
    assert canary not in str(raised.value)

    result = subprocess.run(
        [sys.executable, "-m", "orchestrator.control_plane", "--config", str(config_path)],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert canary not in result.stdout + result.stderr


def test_control_plane_frozen_secret_generator_contract(monkeypatch, capsys) -> None:
    from orchestrator.control_plane import __main__ as cli

    answers = iter(["operator-password", "operator-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))
    monkeypatch.setattr(sys, "argv", ["winsw-recovery-control-plane", "--generate-secrets"])

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["cluster_token"].encode("utf-8")) >= 32
    assert len(payload["session_secret"].encode("utf-8")) >= 32
    assert verify_password("operator-password", payload["admin_password_hash"])
    generated = _valid_example("control-plane.example.json")
    generated["admin_password_hash"] = payload["admin_password_hash"]
    assert ControlPlaneConfig.model_validate(generated).admin_password_hash == payload[
        "admin_password_hash"
    ]


@pytest.mark.parametrize(
    "invalid_hash",
    [
        "pbkdf2_sha256$garbage",
        "sha256$310000$MDEyMzQ1Njc4OWFiY2RlZg==$" + "AA==",
        "pbkdf2_sha256$99999$MDEyMzQ1Njc4OWFiY2RlZg==$" + "AA==",
        "pbkdf2_sha256$1000001$MDEyMzQ1Njc4OWFiY2RlZg==$" + "AA==",
        "pbkdf2_sha256$310000$not+urlsafe*$" + "AA==",
        "pbkdf2_sha256$310000$"
        + urlsafe_b64encode(b"short").decode()
        + "$"
        + urlsafe_b64encode(b"x" * 32).decode(),
        "pbkdf2_sha256$310000$"
        + urlsafe_b64encode(b"0123456789abcdef").decode()
        + "$"
        + urlsafe_b64encode(b"x" * 31).decode(),
    ],
)
def test_control_plane_config_rejects_malformed_password_hash(invalid_hash: str) -> None:
    raw = _valid_example("control-plane.example.json")
    raw["admin_password_hash"] = invalid_hash
    with pytest.raises(Exception):
        ControlPlaneConfig.model_validate(raw)


@pytest.mark.parametrize("listen_host", ["", " ", "control-plane.local", "224.0.0.1", "ff02::1"])
def test_control_plane_config_rejects_invalid_listen_host(listen_host: str) -> None:
    raw = _valid_example("control-plane.example.json")
    raw["listen_host"] = listen_host
    with pytest.raises(Exception):
        ControlPlaneConfig.model_validate(raw)


def test_control_plane_config_accepts_unspecified_and_loopback_listeners() -> None:
    raw = _valid_example("control-plane.example.json")
    for listen_host in ("0.0.0.0", "::", "127.0.0.1", "::1"):
        raw["listen_host"] = listen_host
        assert ControlPlaneConfig.model_validate(raw).listen_host == listen_host


def test_control_plane_cli_redacts_malformed_password_hash(tmp_path: Path) -> None:
    canary = "CANARY-ADMIN-HASH-MUST-NOT-LEAK"
    raw = _valid_example("control-plane.example.json")
    raw["admin_password_hash"] = f"pbkdf2_sha256$310000${canary}$invalid"
    config_path = tmp_path / "control-plane.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    result = _run_check("orchestrator.control_plane", config_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "component": "control-plane",
        "config_valid": False,
        "error": "configuration validation failed",
    }
    assert canary not in result.stderr


def test_control_plane_config_freezes_public_lease_and_cookie_contract() -> None:
    raw = _valid_example("control-plane.example.json")
    assert ControlPlaneConfig.model_validate(raw).offline_after_seconds == 45
    with pytest.raises(Exception):
        ControlPlaneConfig.model_validate(dict(raw, offline_after_seconds=60))
    with pytest.raises(Exception):
        ControlPlaneConfig.model_validate(dict(raw, session_cookie_name="other"))


def test_agent_normal_start_reports_service_mapping_change_without_traceback(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "agent.sqlite3"
    store = AgentStore(database_path)
    store.initialize()
    store.bind_service_mappings(
        [AllowlistedService(local_service_id="mysql", windows_service_name="MySQL80")]
    )

    raw = _valid_example("agent.example.json")
    raw["database_path"] = str(database_path)
    raw["services"][0]["windows_service_name"] = "CANARY-REBOUND-SERVICE"
    config_path = tmp_path / "agent.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "orchestrator.agent", "--config", str(config_path)],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "component": "agent",
        "started": False,
        "error_code": "SERVICE_MAPPING_CHANGED",
        "error": "service mapping changed",
    }
    assert "Traceback" not in result.stderr
    assert "CANARY-REBOUND-SERVICE" not in result.stderr
    assert str(config_path) not in result.stderr
    assert raw["cluster_token"] not in result.stderr


def test_prepare_deployment_renders_fail_closed_drafts_then_accepts_local_secrets(
    tmp_path: Path,
) -> None:
    inventory_path = tmp_path / "deployment-inventory.json"
    inventory_path.write_bytes(
        (REPOSITORY / "examples" / "deployment-inventory.example.json").read_bytes()
    )
    output_dir = tmp_path / "prepared"

    result = _run_prepare(inventory_path, output_dir)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload == {
        "component": "recovery-deployment-preparer",
        "inventory_valid": True,
        "config_ready": False,
        "agent_count": 3,
        "service_count": 5,
        "recovery_group_count": 1,
        "manifest_sha256": payload["manifest_sha256"],
    }
    assert len(payload["manifest_sha256"]) == 64
    assert set(payload["manifest_sha256"]) <= set("0123456789abcdef")

    cp_path = output_dir / "control-plane" / "control-plane.json"
    agent_paths = sorted((output_dir / "agents").glob("*/agent.json"))
    blueprint_path = output_dir / "recovery-blueprint.json"
    manifest_path = output_dir / "deployment-manifest.json"
    assert cp_path.is_file()
    assert len(agent_paths) == 3
    assert blueprint_path.is_file()
    assert manifest_path.is_file()
    assert not list(output_dir.rglob("*.sqlite3"))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["config_ready"] is False
    assert manifest["inventory_sha256"] == hashlib.sha256(
        inventory_path.read_bytes()
    ).hexdigest()
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == payload[
        "manifest_sha256"
    ]
    for entry in manifest["files"]:
        relative = entry["path"]
        assert "\\" not in relative
        rendered_path = output_dir / Path(relative)
        assert rendered_path.is_file()
        assert hashlib.sha256(rendered_path.read_bytes()).hexdigest() == entry["sha256"]

    sentinel = "REQUIRED-GENERATE-ON-TARGET"
    all_drafts = [cp_path, *agent_paths]
    for draft in all_drafts:
        assert sentinel in draft.read_text(encoding="utf-8")
        module = (
            "orchestrator.control_plane"
            if draft == cp_path
            else "orchestrator.agent"
        )
        rejected = _run_check(module, draft)
        assert rejected.returncode == 2
        assert json.loads(rejected.stderr)["config_valid"] is False

    shared_token = TEST_CLUSTER_TOKEN
    cp = json.loads(cp_path.read_text(encoding="utf-8"))
    cp["cluster_token"] = shared_token
    cp["session_secret"] = TEST_SESSION_SECRET
    cp["admin_password_hash"] = hash_password(
        "test-password", salt=b"0123456789abcdef"
    )
    cp_path.write_text(json.dumps(cp), encoding="utf-8")
    assert _run_check("orchestrator.control_plane", cp_path).returncode == 0

    for agent_path in agent_paths:
        agent = json.loads(agent_path.read_text(encoding="utf-8"))
        agent["cluster_token"] = shared_token
        agent_path.write_text(json.dumps(agent), encoding="utf-8")
        assert _run_check("orchestrator.agent", agent_path).returncode == 0


def test_prepare_deployment_does_not_import_control_plane_web_runtime(
    tmp_path: Path,
) -> None:
    inventory_path = tmp_path / "deployment-inventory.json"
    inventory_path.write_bytes(
        (REPOSITORY / "examples" / "deployment-inventory.example.json").read_bytes()
    )
    output_dir = tmp_path / "prepared"
    guarded_runner = """
import importlib.abc
import runpy
import sys

class RejectWebRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"uvicorn", "orchestrator.control_plane.app"}:
            raise RuntimeError("web runtime imported during offline preparation")
        return None

sys.meta_path.insert(0, RejectWebRuntime())
sys.argv = ["orchestrator.control_plane", *sys.argv[1:]]
runpy.run_module("orchestrator.control_plane.__main__", run_name="__main__")
"""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            guarded_runner,
            "--prepare-deployment",
            str(inventory_path),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["inventory_valid"] is True
    assert output_dir.is_dir()


def test_prepare_deployment_failure_is_redacted_and_leaves_no_output(
    tmp_path: Path,
) -> None:
    inventory = json.loads(
        (REPOSITORY / "examples" / "deployment-inventory.example.json").read_text(
            encoding="utf-8"
        )
    )
    canary = "CANARY-INVENTORY-CONTENT-MUST-NOT-LEAK"
    inventory["unknown_secret_like_field"] = canary
    inventory_path = tmp_path / "invalid-inventory.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    output_dir = tmp_path / "must-not-exist"

    result = _run_prepare(inventory_path, output_dir)

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "component": "recovery-deployment-preparer",
        "inventory_valid": False,
        "error": "deployment inventory validation failed",
    }
    assert canary not in result.stderr
    assert str(inventory_path) not in result.stderr
    assert not output_dir.exists()


def test_prepare_deployment_rejects_existing_output_without_modifying_it(
    tmp_path: Path,
) -> None:
    inventory_path = REPOSITORY / "examples" / "deployment-inventory.example.json"
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    marker = output_dir / "operator-owned.txt"
    marker.write_text("preserve", encoding="utf-8")

    result = _run_prepare(inventory_path, output_dir)

    assert result.returncode == 2
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert list(output_dir.iterdir()) == [marker]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--prepare-deployment", "inventory.json"],
        ["--prepare-deployment", "inventory.json", "--check-config", "--output-dir", "out"],
        ["--generate-secrets", "--output-dir", "out"],
        ["--config", "control-plane.json", "--output-dir", "out"],
    ],
)
def test_prepare_deployment_cli_modes_are_explicitly_separated(
    arguments: list[str],
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "orchestrator.control_plane", *arguments],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
