from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.common.security import hash_password


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "test_recovery_host_preflight.ps1"


def test_host_preflight_is_explicit_local_and_read_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "BusinessServiceName" in source
    assert '$Stage = "PreInstall"' in source
    assert '@("PreInstall", "PostInstall")' in source
    assert "CurrentControlSet\\Services" in source
    assert "Get-NetTCPConnection" in source
    assert "Get-ListenerOwnershipAssessment" in source
    assert "Win32_Process" in source
    assert "ParentProcessId" in source
    assert "ExecutablePath" in source
    assert "$depth -lt 8" in source
    assert "Get-DataTreeItems" in source
    assert "REPARSE_POINT_NOT_ALLOWED" in source
    assert "ACL_DATA_TREE" in source
    assert 'PostInstall must inspect the installed frozen onedir package.' in source
    assert '"--check-config"' in source
    assert '"orchestrator.agent"' in source
    assert '"orchestrator.control_plane"' in source
    assert '"-B"' in source
    assert "Get-Acl" in source
    assert "AreAccessRulesProtected" in source
    assert '"S-1-5-18"' in source
    assert '"S-1-5-32-544"' in source
    assert 'side_effects = "NONE"' in source
    assert "ConvertTo-Json -Depth" in source
    assert "remote_hosts_scanned = 0" in source

    forbidden = (
        "Start-Service",
        "Stop-Service",
        "Restart-Service",
        "Set-Service",
        "New-Service",
        "Remove-Service",
        "Invoke-Command",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "Test-NetConnection",
        "TcpClient",
        "-ComputerName",
        "sc.exe",
        'Arguments @("install"',
    )
    for command in forbidden:
        assert command.casefold() not in source.casefold()


def test_host_preflight_has_stable_failure_exit_codes() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    expected = {
        "PASS": 0,
        "INPUT_INVALID": 2,
        "HOST_UNSUPPORTED": 3,
        "RUNTIME_INVALID": 4,
        "CONFIG_INVALID": 5,
        "SERVICE_INVALID": 6,
        "PORT_IN_USE": 7,
        "ACL_INVALID": 8,
        "UNEXPECTED_ERROR": 10,
    }
    for name, value in expected.items():
        assert f"{name} = {value}" in source


def test_postinstall_rejects_mixed_role_and_impostor_listener_pids() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this test host")

    harness = REPOSITORY / "tests" / "test_recovery_host_preflight_listener_ownership.ps1"
    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-RepositoryRoot",
            str(REPOSITORY),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["outcome"] == "PASS"
    assert report["side_effects"] == "NONE"
    assert report["scenarios"] == [
        "sole_valid_listener_passes",
        "mixed_valid_and_impostor_listener_fails",
    ]


def test_missing_preflight_arguments_return_machine_readable_failure() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this test host")

    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(SCRIPT),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=False,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    stdout = result.stdout.decode("utf-8-sig")
    stderr = result.stderr.decode("utf-8-sig")
    report = json.loads(stdout)
    assert report["schema_version"] == 1
    assert report["outcome"] == "FAIL"
    assert report["exit_code"] == 2
    assert report["side_effects"] == "NONE"
    assert report["stage"] == "PreInstall"
    assert report["scope"]["local_only"] is True
    assert report["scope"]["remote_hosts_scanned"] == 0
    assert report["scope"]["requested_business_services"] == []
    assert report["scope"]["resolved_business_services"] == []
    assert report["failures"][0]["id"] == "INPUT_PARAMETERS"
    assert report["warnings"] == []
    assert not stderr


def test_preflight_uses_authoritative_config_check_without_secret_output(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("SCM and ACL preflight is Windows-only")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this test host")

    config = json.loads(
        (REPOSITORY / "examples" / "control-plane.example.json").read_text(
            encoding="utf-8"
        )
    )
    injected_token = "preflight-valid-cluster-token-0123456789"
    injected_session_secret = "preflight-valid-session-secret-0123456789"
    config.update(
        {
            "database_path": "control-plane.sqlite3",
            "cluster_token": injected_token,
            "admin_password_hash": hash_password(
                "preflight-test-password", salt=b"0123456789abcdef"
            ),
            "session_secret": injected_session_secret,
        }
    )
    config_path = tmp_path / "control-plane.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-Role",
            "ControlPlane",
            "-Stage",
            "PreInstall",
            "-ConfigPath",
            str(config_path),
            "-DataDirectory",
            str(tmp_path),
            "-RuntimeMode",
            "Python",
            "-Python",
            sys.executable,
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=False,
        timeout=60,
        check=False,
    )

    stdout = result.stdout.decode("utf-8-sig")
    stderr = result.stderr.decode("utf-8-sig")
    report = json.loads(stdout)
    check = next(
        item for item in report["checks"] if item["id"] == "CONFIG_AUTHORITATIVE_SCHEMA"
    )
    assert check["status"] == "PASS"
    assert check["evidence"]["component"] == "control-plane"
    assert check["evidence"]["sensitive_values_emitted"] is False
    assert injected_token not in stdout
    assert injected_session_secret not in stdout
    assert "pbkdf2_sha256$" not in stdout
    assert not stderr


def test_preflight_reports_authoritative_config_failure_without_secret(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("SCM and ACL preflight is Windows-only")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this test host")

    secret = "preflight-secret-must-not-appear-0123456789"
    config_path = tmp_path / "control-plane.json"
    config_path.write_text(
        json.dumps(
            {
                "listen_host": "127.0.0.1",
                "listen_port": 18766,
                "database_path": "control-plane.sqlite3",
                "cluster_token": secret,
                "agent_source_cidrs": ["127.0.0.1/32"],
                "admin_password_hash": "pbkdf2_sha256$invalid-secret-hash",
                "session_secret": secret,
                "unexpected_field": True,
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-Role",
            "ControlPlane",
            "-Stage",
            "PreInstall",
            "-ConfigPath",
            str(config_path),
            "-DataDirectory",
            str(tmp_path),
            "-RuntimeMode",
            "Python",
            "-Python",
            sys.executable,
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=False,
        timeout=60,
        check=False,
    )

    stdout = result.stdout.decode("utf-8-sig")
    stderr = result.stderr.decode("utf-8-sig")
    report = json.loads(stdout)
    check = next(
        item for item in report["checks"] if item["id"] == "CONFIG_AUTHORITATIVE_SCHEMA"
    )
    assert check["status"] == "FAIL"
    assert check["failure_code"] == 5
    assert secret not in stdout
    assert "invalid-secret-hash" not in stdout
    assert not stderr
