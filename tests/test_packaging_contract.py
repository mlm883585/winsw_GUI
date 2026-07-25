from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from orchestrator.agent.config import AgentConfig, load_agent_config
from orchestrator.common.security import hash_password
from orchestrator.control_plane.config import ControlPlaneConfig, load_control_plane_config


REPOSITORY = Path(__file__).resolve().parents[1]


def _windows_powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        pytest.skip("Windows PowerShell is required for the packaging contract test")
    return executable


def _write_fake_python(path: Path, invocation_marker: Path) -> None:
    escaped_marker = str(invocation_marker).replace("'", "''")
    path.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"[System.IO.File]::WriteAllText('{escaped_marker}', 'python-invoked')\n"
        "Write-Output '3.13.0'\n"
        "exit 0\n",
        encoding="utf-8",
    )


def _write_fake_build_python(path: Path) -> None:
    path.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$pythonArguments = @($args)\n"
        "if ($pythonArguments.Count -ge 1 -and $pythonArguments[0] -eq '-c') {\n"
        "    Write-Output '3.13.0'\n"
        "    exit 0\n"
        "}\n"
        "if ($pythonArguments.Count -ge 2 -and "
        "$pythonArguments[0] -eq '-m' -and $pythonArguments[1] -eq 'pip') {\n"
        "    exit 0\n"
        "}\n"
        "if ($pythonArguments.Count -ge 2 -and "
        "$pythonArguments[0] -eq '-m' -and $pythonArguments[1] -eq 'PyInstaller') {\n"
        "    $nameIndex = [Array]::IndexOf($pythonArguments, '--name')\n"
        "    $distIndex = [Array]::IndexOf($pythonArguments, '--distpath')\n"
        "    if ($nameIndex -lt 0 -or $distIndex -lt 0) { exit 8 }\n"
        "    $name = $pythonArguments[$nameIndex + 1]\n"
        "    $distPath = $pythonArguments[$distIndex + 1]\n"
        "    $packagePath = Join-Path $distPath $name\n"
        "    New-Item -ItemType Directory -Force -Path $packagePath | Out-Null\n"
        "    [IO.File]::WriteAllBytes(\n"
        "        (Join-Path $packagePath \"$name.exe\"),\n"
        "        [Text.Encoding]::ASCII.GetBytes(\"fake-$name\")\n"
        "    )\n"
        "    exit 0\n"
        "}\n"
        "Write-Error 'Unexpected fake Python invocation.'\n"
        "exit 9\n",
        encoding="utf-8",
    )


def _write_distribution_manifest(distribution: Path, relative_paths: list[str]) -> None:
    lines = []
    for relative_path in sorted(relative_paths):
        payload = distribution / Path(relative_path)
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        lines.append(f"{digest}  {Path(relative_path).as_posix()}")
    (distribution / "SHA256SUMS.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _run_distribution_verifier(distribution: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run(
        [
            _windows_powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPOSITORY / "scripts" / "verify_recovery_distribution.ps1"),
            "-DistributionDirectory",
            str(distribution),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.stdout.strip(), result.stderr
    return result, json.loads(result.stdout)


def test_example_configs_are_strict_and_loadable(tmp_path: Path) -> None:
    agent_source = REPOSITORY / "examples" / "agent.example.json"
    agent_raw = json.loads(agent_source.read_text(encoding="utf-8"))
    agent_raw["cluster_token"] = "test-cluster-token-with-at-least-32-bytes-0001"
    agent_raw["database_path"] = str(tmp_path / "agent.sqlite3")
    agent_path = tmp_path / "agent.json"
    agent_path.write_text(json.dumps(agent_raw), encoding="utf-8")

    cp_source = REPOSITORY / "examples" / "control-plane.example.json"
    cp_raw = json.loads(cp_source.read_text(encoding="utf-8"))
    cp_raw["cluster_token"] = "test-cluster-token-with-at-least-32-bytes-0001"
    cp_raw["session_secret"] = "test-session-secret-with-at-least-32-bytes-0002"
    cp_raw["admin_password_hash"] = hash_password(
        "test-password", salt=b"0123456789abcdef"
    )
    cp_raw["database_path"] = str(tmp_path / "control-plane.sqlite3")
    cp_path = tmp_path / "control-plane.json"
    cp_path.write_text(json.dumps(cp_raw), encoding="utf-8")

    assert isinstance(load_agent_config(agent_path), AgentConfig)
    assert isinstance(load_control_plane_config(cp_path), ControlPlaneConfig)


def test_module_entry_points_can_show_help() -> None:
    for module in ("orchestrator.agent", "orchestrator.control_plane"):
        result = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "--config" in result.stdout
        assert "--check-config" in result.stdout
        if module == "orchestrator.control_plane":
            assert "--generate-secrets" in result.stdout


def test_build_script_is_onedir_and_supplies_package_search_path() -> None:
    script = (REPOSITORY / "scripts" / "build_recovery_mvp.ps1").read_text(encoding="utf-8")
    assert script.count('"--onedir"') == 3
    assert "--onefile" not in script
    assert script.count('"--paths", $repo') == 3
    assert 'PyInstallerVersion = "6.16.0"' in script
    assert "Join-Path $repo 'orchestrator\\control_plane\\templates'" in script
    assert "Join-Path $repo 'orchestrator\\control_plane\\static'" in script
    assert 'Join-Path $output "deployment"' in script
    assert 'scripts\\test_recovery_host_preflight.ps1' in script
    assert 'deployment\\winsw-x64-v2.12.0.lock.json' in script
    assert 'docs\\recovery-mvp-operations.md' in script
    assert 'scripts\\validate_recovery_evidence.py' in script
    assert 'scripts\\verify_recovery_distribution.ps1' in script
    assert 'examples\\recovery-evidence.template.json' in script
    assert 'docs\\contracts\\recovery-mvp-evidence-v1.md' in script
    assert 'scripts\\get_recovery_host_facts.ps1' in script
    assert 'examples\\deployment-inventory.example.json' in script
    assert 'docs\\contracts\\recovery-deployment-inventory-v1.md' in script


def test_build_distribution_includes_inventory_assets_with_source_hashes(
    tmp_path: Path,
) -> None:
    # The real build contract requires Get-FileHash (Windows PowerShell 5.1+
    # or PowerShell 7). This test host also exposes a legacy powershell.exe
    # without that cmdlet, so prefer pwsh when it is available.
    powershell = shutil.which("pwsh") or _windows_powershell()
    script = REPOSITORY / "scripts" / "build_recovery_mvp.ps1"
    output_name = f"dist-packaging-contract-{uuid.uuid4().hex[:12]}"
    distribution = REPOSITORY / output_name
    fake_python = tmp_path / "fake-build-python.ps1"
    _write_fake_build_python(fake_python)
    expected_sources = {
        "scripts/get_recovery_host_facts.ps1": (
            REPOSITORY / "scripts" / "get_recovery_host_facts.ps1"
        ),
        "examples/deployment-inventory.example.json": (
            REPOSITORY / "examples" / "deployment-inventory.example.json"
        ),
        "docs/contracts/recovery-deployment-inventory-v1.md": (
            REPOSITORY
            / "docs"
            / "contracts"
            / "recovery-deployment-inventory-v1.md"
        ),
    }

    try:
        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Python",
                str(fake_python),
                "-OutputDirectory",
                output_name,
            ],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        manifest_lines = (distribution / "SHA256SUMS.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        manifest = {
            relative_path: digest
            for digest, relative_path in (
                line.split("  ", maxsplit=1) for line in manifest_lines
            )
        }

        for relative_path, source in expected_sources.items():
            packaged = distribution / Path(relative_path)
            assert packaged.is_file(), relative_path
            source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            packaged_digest = hashlib.sha256(packaged.read_bytes()).hexdigest()
            assert manifest[relative_path] == source_digest == packaged_digest
    finally:
        shutil.rmtree(distribution, ignore_errors=True)


def test_build_script_uses_powershell_51_safe_output_boundaries() -> None:
    script = (REPOSITORY / "scripts" / "build_recovery_mvp.ps1").read_text(encoding="utf-8")

    # System.IO.Path.GetRelativePath is unavailable in Windows PowerShell 5.1.
    assert "GetRelativePath" not in script
    assert ".FullName.Substring($outputPrefix.Length)" in script

    # OutputDirectory is a single repository-local dist-* leaf, never a path.
    assert '[ValidatePattern("^dist-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]' in script
    assert "[System.IO.Path]::IsPathRooted($OutputDirectory)" in script
    assert "$outputParent.Equals($repo, [StringComparison]::OrdinalIgnoreCase)" in script

    # Existing output roots, managed paths, cleanup targets, and final contents
    # are all checked before any recursive removal or manifest generation.
    assert script.count("[IO.FileAttributes]::ReparsePoint") >= 4
    assert "OutputDirectory must not be a reparse point." in script
    assert "Managed build path must not be a reparse point" in script
    assert "Distribution must not contain reparse points." in script


@pytest.mark.parametrize("unsafe_output", [".", "..", "ABSOLUTE"])
def test_build_script_rejects_unsafe_output_before_invoking_python(
    tmp_path: Path, unsafe_output: str
) -> None:
    powershell = _windows_powershell()
    script = REPOSITORY / "scripts" / "build_recovery_mvp.ps1"
    script_text = script.read_text(encoding="utf-8")

    # Do not execute a destructive regression: first prove parameter binding has
    # the restrictive leaf-name guard that makes the dynamic check safe.
    assert '[ValidatePattern("^dist-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]' in script_text

    invocation_marker = tmp_path / "python-invocation.txt"
    invocation_marker.write_text("untouched", encoding="utf-8")
    fake_python = tmp_path / "fake-python.ps1"
    _write_fake_python(fake_python, invocation_marker)
    output_argument = (
        str((tmp_path / "dist-absolute").resolve())
        if unsafe_output == "ABSOLUTE"
        else unsafe_output
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
            str(script),
            "-Python",
            str(fake_python),
            "-OutputDirectory",
            output_argument,
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert invocation_marker.read_text(encoding="utf-8") == "untouched"


def test_build_script_rejects_reparse_output_before_invoking_python(tmp_path: Path) -> None:
    powershell = _windows_powershell()
    script = REPOSITORY / "scripts" / "build_recovery_mvp.ps1"
    output_name = f"dist-reparse-contract-{uuid.uuid4().hex[:12]}"
    output_link = REPOSITORY / output_name
    invocation_marker = tmp_path / "python-invocation.txt"
    invocation_marker.write_text("untouched", encoding="utf-8")
    fake_python = tmp_path / "fake-python.ps1"
    _write_fake_python(fake_python, invocation_marker)

    try:
        try:
            os.symlink(tmp_path, output_link, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlink creation is unavailable: {exc}")

        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Python",
                str(fake_python),
                "-OutputDirectory",
                output_name,
            ],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert result.returncode != 0
        assert "reparse point" in (result.stdout + result.stderr).casefold()
        assert invocation_marker.read_text(encoding="utf-8") == "untouched"
    finally:
        if output_link.is_symlink():
            output_link.unlink()


def test_install_script_only_registers_mvp_services_with_fixed_account_boundary() -> None:
    script = (REPOSITORY / "scripts" / "install_recovery_service.ps1").read_text(encoding="utf-8")
    assert 'ValidateSet("Agent", "ControlPlane")' in script
    assert '<startmode>Automatic</startmode>' in script
    assert "ServiceAccount" not in script
    assert 'SecurityIdentifier]::new("S-1-5-18")' in script
    assert 'SecurityIdentifier]::new("S-1-5-32-544")' in script
    assert "SetAccessRuleProtection($true, $false)" in script
    assert 'Join-Path $data "package"' in script
    assert "database_path must resolve inside DataDirectory" in script
    assert '@("--config", $config, "--check-config")' in script
    assert '-Purpose "WinSW install" -Arguments @("install", $Journal.xml_path)' in script
    assert '-Purpose "WinSW start" -Arguments @("start", $Journal.xml_path)' in script
    assert "sc.exe" not in script.casefold()
    assert "Set-Service" not in script
    assert "installer is install-only" in script
    assert "/latest/" in script
    assert 'current_phase = "PREPARED"' in script
    assert '$Journal.current_phase = "STAGING"' in script
    assert '$Journal.current_phase = "ACL_APPLY"' in script
    assert '$Journal.current_phase = "PUBLISH_PACKAGE"' in script
    assert '$Journal.current_phase = "PUBLISH_SERVICE"' in script
    assert '$Journal.current_phase = "STAGED_WINSW_VERIFY"' in script
    assert '$Journal.current_phase = "PUBLISHED_WINSW_VERIFY"' in script
    assert '$Journal.current_phase = "SCM_INSTALL"' in script
    assert '$Journal.current_phase = "SCM_START"' in script
    assert "GetSecurityDescriptorSddlForm($sections)" in script
    assert "SetSecurityDescriptorSddlForm($snapshot.sddl, $sections)" in script
    assert "DataDirectory changed after ACL snapshot" in script
    assert "RB_SERVICE_OWNERSHIP_UNPROVEN" in script
    assert "RB_STOP_FAILED" in script
    assert "RB_UNINSTALL_FAILED" in script
    assert "INSTALL_FAILED phase=" in script
    assert script.count("Assert-WinSWIntegrity -LiteralPath") >= 3


def test_install_transaction_rolls_back_owned_state_and_is_retryable() -> None:
    result = subprocess.run(
        [
            _windows_powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPOSITORY / "tests" / "test_install_recovery_service_transaction.ps1"),
            "-RepositoryRoot",
            str(REPOSITORY),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["outcome"] == "PASS"
    assert report["side_effects"] == "TEST_TEMP_ONLY"
    assert set(report["scenarios"]) == {
        "staged_wrapper_tamper_rejected",
        "published_wrapper_tamper_rejected",
        "install_failure_retry",
        "start_failure_retry",
        "rollback_command_failure_aggregated",
        "uninstall_failure_preserves_dependencies",
        "unowned_service_never_removed",
        "managed_residue_fail_closed",
    }


def test_distribution_verifier_contract_is_local_read_only_and_fail_closed() -> None:
    script = (REPOSITORY / "scripts" / "verify_recovery_distribution.ps1").read_text(
        encoding="utf-8"
    )
    assert 'side_effects = "NONE"' in script
    assert "manifest_self_verified = $false" in script
    assert 'manifest_trust = "OUT_OF_BAND_REQUIRED"' in script
    assert "[Security.Cryptography.SHA256]::Create()" in script
    assert "[IO.FileAttributes]::ReparsePoint" in script
    assert "dedicated non-root directory" in script
    assert "UNC and device paths are not allowed" in script
    assert "[IO.DriveType]::Network" in script
    assert "must not contain an entry for itself" in script

    forbidden_commands = (
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "Get-Service",
        "Start-Service",
        "Stop-Service",
        "Set-Service",
        "Win32_Service",
        "sc.exe",
        "Remove-Item",
        "Set-Content",
        "Add-Content",
    )
    folded = script.casefold()
    for command in forbidden_commands:
        assert command.casefold() not in folded


def test_distribution_verifier_accepts_exact_set_without_modifying_it(tmp_path: Path) -> None:
    distribution = tmp_path / "dist-recovery"
    (distribution / "nested").mkdir(parents=True)
    (distribution / "agent.exe").write_bytes(b"agent-binary")
    (distribution / "nested" / "config.json").write_bytes(b'{"version":1}')
    _write_distribution_manifest(distribution, ["agent.exe", "nested/config.json"])
    before = {
        path.relative_to(distribution).as_posix(): path.read_bytes()
        for path in distribution.rglob("*")
        if path.is_file()
    }

    result, report = _run_distribution_verifier(distribution)

    after = {
        path.relative_to(distribution).as_posix(): path.read_bytes()
        for path in distribution.rglob("*")
        if path.is_file()
    }
    assert result.returncode == 0, result.stderr
    assert report["outcome"] == "PASS"
    assert report["exit_code"] == 0
    assert report["side_effects"] == "NONE"
    assert report["manifest_self_verified"] is False
    assert report["manifest_trust"] == "OUT_OF_BAND_REQUIRED"
    assert report["expected_file_count"] == 2
    assert report["actual_file_count"] == 2
    assert before == after


@pytest.mark.parametrize(
    ("mutation", "report_field"),
    [
        ("missing", "missing_files"),
        ("extra", "extra_files"),
        ("hash", "hash_mismatches"),
    ],
)
def test_distribution_verifier_rejects_set_and_hash_mismatches(
    tmp_path: Path, mutation: str, report_field: str
) -> None:
    distribution = tmp_path / "dist-recovery"
    distribution.mkdir()
    payload = distribution / "payload.bin"
    payload.write_bytes(b"expected")
    _write_distribution_manifest(distribution, ["payload.bin"])

    if mutation == "missing":
        payload.unlink()
    elif mutation == "extra":
        (distribution / "unexpected.bin").write_bytes(b"unexpected")
    else:
        payload.write_bytes(b"tampered")

    result, report = _run_distribution_verifier(distribution)

    assert result.returncode == 3, result.stderr
    assert report["outcome"] == "FAIL"
    assert report["exit_code"] == 3
    assert report[report_field]


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.bin",
        "nested\\payload.bin",
        "/absolute.bin",
        "SHA256SUMS.txt",
    ],
)
def test_distribution_verifier_rejects_unsafe_manifest_paths(
    tmp_path: Path, unsafe_path: str
) -> None:
    distribution = tmp_path / "dist-recovery"
    distribution.mkdir()
    (distribution / "payload.bin").write_bytes(b"payload")
    (distribution / "SHA256SUMS.txt").write_text(
        f"{'0' * 64}  {unsafe_path}\n",
        encoding="utf-8",
    )

    result, report = _run_distribution_verifier(distribution)

    assert result.returncode == 2, result.stderr
    assert report["outcome"] == "FAIL"
    assert report["exit_code"] == 2
    assert report["errors"]


def test_distribution_verifier_rejects_filesystem_root() -> None:
    root = Path(REPOSITORY.anchor)
    result, report = _run_distribution_verifier(root)

    assert result.returncode == 2, result.stderr
    assert report["exit_code"] == 2
    assert "non-root" in " ".join(report["errors"]).casefold()


def test_distribution_verifier_rejects_unc_before_filesystem_access() -> None:
    result, report = _run_distribution_verifier(
        Path(r"\\recovery-verifier.invalid\untrusted\distribution")
    )

    assert result.returncode == 2, result.stderr
    assert report["exit_code"] == 2
    assert "local storage" in " ".join(report["errors"]).casefold()


def test_distribution_verifier_rejects_reparse_points(tmp_path: Path) -> None:
    distribution = tmp_path / "dist-recovery"
    distribution.mkdir()
    payload = distribution / "payload.bin"
    payload.write_bytes(b"payload")
    _write_distribution_manifest(distribution, ["payload.bin"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"outside")

    try:
        os.symlink(outside, distribution / "linked", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    result, report = _run_distribution_verifier(distribution)

    assert result.returncode == 2, result.stderr
    assert report["exit_code"] == 2
    assert "reparse point" in " ".join(report["errors"]).casefold()


def test_frozen_binary_smoke_script_is_loopback_and_read_only() -> None:
    script = (REPOSITORY / "scripts" / "smoke_recovery_binaries.ps1").read_text(
        encoding="utf-8"
    )
    cookie_line = next(
        line for line in script.splitlines() if "session_cookie_name =" in line
    )
    smoke_cookie_name = cookie_line.split("=", 1)[1].strip().strip('"')
    validated_config = ControlPlaneConfig.model_validate(
        {
            "listen_host": "127.0.0.1",
            "listen_port": 18766,
            "database_path": "control-plane.sqlite3",
            "cluster_token": "smoke-cluster-token-0123456789abcdef",
            "agent_source_cidrs": ["127.0.0.1/32"],
            "offline_after_seconds": 45,
            "agent_request_timeout_seconds": 3,
            "agent_request_attempts": 1,
            "admin_username": "admin",
            "admin_password_hash": hash_password(
                "smoke-password", salt=b"0123456789abcdef"
            ),
            "session_secret": "smoke-session-secret-0123456789abcdef",
            "session_cookie_name": smoke_cookie_name,
            "session_max_age_seconds": 300,
        }
    )

    assert validated_config.session_cookie_name == smoke_cookie_name
    assert 'ObserveWindowsService = "EventLog"' in script
    assert 'listen_host = "127.0.0.1"' in script
    assert "-WindowStyle Hidden" in script
    assert "/api/v1/agents" in script
    assert "/api/v1/services" in script
    assert "/actions/" not in script
    assert "Start-Service" not in script
    assert "Stop-Service" not in script
    assert 'SideEffects = "NONE' in script
    assert script.count("-UseBasicParsing") == 2
    assert 'if ($MyInvocation.InvocationName -eq ".")' in script
    assert "function Remove-RecoverySmokeState" in script
    assert "$failed" not in script
    finally_block = script.split("} finally {", 1)[1]
    assert "Remove-RecoverySmokeState" in finally_block


def test_smoke_state_cleanup_removes_only_managed_temp_child(tmp_path: Path) -> None:
    temp_root = tmp_path / "temp"
    managed = temp_root / "winsw-recovery-smoke-contract"
    outside = tmp_path / "operator-owned"
    managed.mkdir(parents=True)
    outside.mkdir()
    (managed / "agent.json").write_text("secret", encoding="utf-8")
    (outside / "preserve.txt").write_text("preserve", encoding="utf-8")
    script = str(REPOSITORY / "scripts" / "smoke_recovery_binaries.ps1").replace(
        "'", "''"
    )
    managed_ps = str(managed).replace("'", "''")
    outside_ps = str(outside).replace("'", "''")
    temp_ps = str(temp_root).replace("'", "''")
    command = rf"""
& {{
    . '{script}'
    Remove-RecoverySmokeState -SmokePath '{managed_ps}' -TempRoot '{temp_ps}'
    $outsideRejected = $false
    try {{
        Remove-RecoverySmokeState -SmokePath '{outside_ps}' -TempRoot '{temp_ps}'
    }} catch {{
        $outsideRejected = $true
    }}
    [pscustomobject]@{{
        managed_removed = -not (Test-Path -LiteralPath '{managed_ps}')
        outside_rejected = $outsideRejected
        outside_preserved = Test-Path -LiteralPath '{outside_ps}\preserve.txt'
    }} | ConvertTo-Json -Compress
}}
"""

    result = subprocess.run(
        [
            _windows_powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=False,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        "utf-8-sig", errors="replace"
    )
    assert result.stderr == b""
    assert json.loads(result.stdout.decode("utf-8-sig")) == {
        "managed_removed": True,
        "outside_rejected": True,
        "outside_preserved": True,
    }
