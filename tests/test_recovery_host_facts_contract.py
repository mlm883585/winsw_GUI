from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "get_recovery_host_facts.ps1"


def _windows_powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        pytest.skip("Windows PowerShell is required for Host Facts contract tests")
    return executable


def _run_powershell(command: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
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


def _decode_json(output: bytes) -> dict:
    return json.loads(output.decode("utf-8-sig"))


def test_host_facts_source_is_explicit_local_read_only_and_secret_free() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    folded = source.casefold()

    assert "WindowsServiceName" in source
    assert "CandidatePort" in source
    assert '$MyInvocation.InvocationName -eq "."' in source
    assert 'side_effects = "NONE"' in source
    assert "remote_hosts_scanned = 0" in source
    assert "Win32_OperatingSystem" in source
    assert "Win32_Service" in source
    assert "Get-NetTCPConnection" in source
    assert "GetAllNetworkInterfaces" in source
    assert "-ErrorAction Stop" in source
    assert "SilentlyContinue" not in source
    assert "PassThru" not in source
    assert "Alias(" not in source

    forbidden = (
        "Invoke-Command",
        "New-PSSession",
        "Enter-PSSession",
        "New-CimSession",
        "-ComputerName",
        "-CimSession",
        "Get-WmiObject",
        "Test-NetConnection",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "Start-Service",
        "Stop-Service",
        "Restart-Service",
        "Set-Service",
        "New-Service",
        "Remove-Service",
        "sc.exe",
        "New-NetFirewallRule",
        "Set-NetFirewallRule",
        "Remove-NetFirewallRule",
        "ImagePath",
        "PathName",
        "StartName",
        "ObjectName",
        "$env:",
        "cluster_token",
        "session_secret",
        "password",
    )
    for forbidden_text in forbidden:
        assert forbidden_text.casefold() not in folded


def test_host_facts_requires_an_explicit_service_name_list() -> None:
    result = subprocess.run(
        [
            _windows_powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
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
    assert not result.stderr
    report = _decode_json(result.stdout)
    assert report == {
        "schema_version": 1,
        "component": "recovery-host-facts",
        "outcome": "FAIL",
        "side_effects": "NONE",
        "remote_hosts_scanned": 0,
        "hostname": None,
        "windows": {"version": None, "architecture": None},
        "active_unicast_ips": [],
        "services": [],
        "candidate_ports": [],
    }


def test_host_facts_dot_source_substitutes_prove_output_whitelist_and_scope() -> None:
    script = str(SCRIPT).replace("'", "''")
    command = rf"""
& {{
    . '{script}'
    function Get-RecoveryHostNameFact {{ return 'HOST-A' }}
    function Get-RecoveryHostWindowsFact {{
        return [pscustomobject][ordered]@{{
            version = '10.0.20348'
            architecture = '64-bit'
        }}
    }}
    function Get-RecoveryHostActiveUnicastIpFact {{
        return @('10.20.30.40', 'fd00::10')
    }}
    function Get-RecoveryHostServiceFact {{
        param([string]$Name)
        return [pscustomobject][ordered]@{{
            Name = $Name
            DisplayName = "Display $Name"
            StartMode = 'Manual'
            State = 'Stopped'
        }}
    }}
    function Get-RecoveryHostCandidatePortFact {{
        param([int]$Port)
        return [pscustomobject][ordered]@{{
            port = $Port
            occupied = ($Port -eq 3306)
            listening = ($Port -eq 3306)
            listen_addresses = $(if ($Port -eq 3306) {{ @('0.0.0.0') }} else {{ @() }})
        }}
    }}
    Invoke-RecoveryHostFacts `
        -RequestedServiceNames @('MySQL80', 'Nacos') `
        -RequestedCandidatePorts @(3306, 8848) |
        ConvertTo-Json -Depth 6
}}
"""
    result = _run_powershell(command)

    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        "utf-8-sig", errors="replace"
    )
    assert not result.stderr
    report = _decode_json(result.stdout)
    assert set(report) == {
        "schema_version",
        "component",
        "outcome",
        "side_effects",
        "remote_hosts_scanned",
        "hostname",
        "windows",
        "active_unicast_ips",
        "services",
        "candidate_ports",
    }
    assert report["schema_version"] == 1
    assert report["component"] == "recovery-host-facts"
    assert report["outcome"] == "PASS"
    assert report["side_effects"] == "NONE"
    assert report["remote_hosts_scanned"] == 0
    assert report["hostname"] == "HOST-A"
    assert report["windows"] == {
        "version": "10.0.20348",
        "architecture": "64-bit",
    }
    assert report["active_unicast_ips"] == ["10.20.30.40", "fd00::10"]
    assert report["services"] == [
        {
            "Name": "MySQL80",
            "DisplayName": "Display MySQL80",
            "StartMode": "Manual",
            "State": "Stopped",
        },
        {
            "Name": "Nacos",
            "DisplayName": "Display Nacos",
            "StartMode": "Manual",
            "State": "Stopped",
        },
    ]
    assert report["candidate_ports"] == [
        {
            "port": 3306,
            "occupied": True,
            "listening": True,
            "listen_addresses": ["0.0.0.0"],
        },
        {
            "port": 8848,
            "occupied": False,
            "listening": False,
            "listen_addresses": [],
        },
    ]
    serialized = result.stdout.decode("utf-8-sig")
    for forbidden_output in (
        "ImagePath",
        "PathName",
        "StartName",
        "ObjectName",
        "token",
        "password",
        "process_id",
    ):
        assert forbidden_output.casefold() not in serialized.casefold()


@pytest.mark.parametrize(
    ("service_expression", "port_expression"),
    [
        ("@()", "@()"),
        ("@('Svc', 'svc')", "@()"),
        ("@(' Svc')", "@()"),
        ("@('Svc')", "@(3306, 3306)"),
        ("@('Svc')", "@(0)"),
    ],
)
def test_invalid_scope_fails_before_any_collector_is_called(
    service_expression: str, port_expression: str
) -> None:
    script = str(SCRIPT).replace("'", "''")
    command = rf"""
& {{
    . '{script}'
    function Get-RecoveryHostNameFact {{ throw 'collector-called' }}
    function Get-RecoveryHostWindowsFact {{ throw 'collector-called' }}
    function Get-RecoveryHostActiveUnicastIpFact {{ throw 'collector-called' }}
    function Get-RecoveryHostServiceFact {{ throw 'collector-called' }}
    function Get-RecoveryHostCandidatePortFact {{ throw 'collector-called' }}
    Invoke-RecoveryHostFacts `
        -RequestedServiceNames {service_expression} `
        -RequestedCandidatePorts {port_expression} |
        ConvertTo-Json -Depth 6
}}
"""
    result = _run_powershell(command)

    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        "utf-8-sig", errors="replace"
    )
    assert not result.stderr
    report = _decode_json(result.stdout)
    assert report["outcome"] == "FAIL"
    assert report["hostname"] is None
    assert report["services"] == []
    assert report["candidate_ports"] == []
    assert b"collector-called" not in result.stdout


@pytest.mark.parametrize(
    "canary",
    ["NOT-A-PORT-CANARY", "1.5", "2147483648", "True"],
)
def test_invalid_candidate_port_cli_is_redacted_json_and_exit_two(
    canary: str,
) -> None:
    result = subprocess.run(
        [
            _windows_powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-WindowsServiceName",
            "Svc",
            "-CandidatePort",
            canary,
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=False,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == b""
    report = _decode_json(result.stdout)
    assert report["outcome"] == "FAIL"
    assert report["hostname"] is None
    assert report["services"] == []
    assert report["candidate_ports"] == []
    combined = (result.stdout + result.stderr).decode("utf-8-sig", errors="replace")
    assert canary not in combined
    assert str(SCRIPT) not in combined


def test_inventory_compatible_address_filter_rejects_non_routable_inputs() -> None:
    script = str(SCRIPT).replace("'", "''")
    command = rf"""
& {{
    . '{script}'
    $cases = [ordered]@{{}}
    foreach ($value in @(
        '127.0.0.1', '0.0.0.0', '169.254.10.20', '224.0.0.1',
        '::1', '::', 'fe80::10', 'ff02::1', '::ffff:192.0.2.1',
        '10.20.30.40', 'fd00::10'
    )) {{
        $cases[$value] = Test-RecoveryInventoryCompatibleIpAddress `
            -Address ([Net.IPAddress]::Parse($value))
    }}
    [pscustomobject]$cases | ConvertTo-Json -Depth 3
}}
"""
    result = _run_powershell(command)

    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        "utf-8-sig", errors="replace"
    )
    report = _decode_json(result.stdout)
    assert report == {
        "127.0.0.1": False,
        "0.0.0.0": False,
        "169.254.10.20": False,
        "224.0.0.1": False,
        "::1": False,
        "::": False,
        "fe80::10": False,
        "ff02::1": False,
        "::ffff:192.0.2.1": False,
        "10.20.30.40": True,
        "fd00::10": True,
    }


def test_collector_failures_and_incomplete_facts_fail_closed() -> None:
    script = str(SCRIPT).replace("'", "''")
    command = rf"""
& {{
    . '{script}'
    function Get-RecoveryHostNameFact {{ return '' }}
    function Get-RecoveryHostWindowsFact {{
        return [pscustomobject]@{{ version = '10.0.20348'; architecture = '32-bit' }}
    }}
    function Get-RecoveryHostActiveUnicastIpFact {{ return @() }}
    function Get-RecoveryHostServiceFact {{
        param([string]$Name)
        return [pscustomobject]@{{
            Name = $Name
            DisplayName = $null
            StartMode = $null
            State = 'Running'
        }}
    }}
    function Get-RecoveryHostCandidatePortFact {{ throw 'provider unavailable' }}
    Invoke-RecoveryHostFacts `
        -RequestedServiceNames @('Svc') `
        -RequestedCandidatePorts @('3306') |
        ConvertTo-Json -Depth 6
}}
"""
    result = _run_powershell(command)

    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        "utf-8-sig", errors="replace"
    )
    assert result.stderr == b""
    report = _decode_json(result.stdout)
    assert report["outcome"] == "FAIL"
    assert report["hostname"] is None
    assert report["windows"] == {"version": None, "architecture": None}
    assert report["active_unicast_ips"] == []
    assert report["services"] == [
        {
            "Name": "Svc",
            "DisplayName": None,
            "StartMode": None,
            "State": "UNKNOWN",
        }
    ]
    assert report["candidate_ports"] == [
        {
            "port": 3306,
            "occupied": None,
            "listening": None,
            "listen_addresses": [],
        }
    ]
    assert "provider unavailable" not in result.stdout.decode(
        "utf-8-sig", errors="replace"
    )


def test_collector_objects_cannot_escape_the_output_whitelist() -> None:
    script = str(SCRIPT).replace("'", "''")
    canary = "LEAK-CANARY-password-ImagePath"
    command = rf"""
& {{
    . '{script}'
    $secretObject = [pscustomobject]@{{ password = '{canary}'; ImagePath = 'hidden' }}
    function Get-RecoveryHostNameFact {{ return $secretObject }}
    function Get-RecoveryHostWindowsFact {{
        return [pscustomobject]@{{ version = $secretObject; architecture = $secretObject }}
    }}
    function Get-RecoveryHostActiveUnicastIpFact {{ return @($secretObject) }}
    function Get-RecoveryHostServiceFact {{
        param([string]$Name)
        return [pscustomobject]@{{
            Name = $secretObject
            DisplayName = $secretObject
            StartMode = $secretObject
            State = $secretObject
        }}
    }}
    function Get-RecoveryHostCandidatePortFact {{
        param([int]$Port)
        return [pscustomobject]@{{
            port = $Port
            occupied = $true
            listening = $true
            listen_addresses = @($secretObject)
        }}
    }}
    Invoke-RecoveryHostFacts `
        -RequestedServiceNames @('Svc') `
        -RequestedCandidatePorts @('3306') |
        ConvertTo-Json -Depth 6
}}
"""
    result = _run_powershell(command)

    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        "utf-8-sig", errors="replace"
    )
    assert result.stderr == b""
    serialized = result.stdout.decode("utf-8-sig", errors="replace")
    assert canary not in serialized
    assert "ImagePath" not in serialized
    report = json.loads(serialized)
    assert report["outcome"] == "FAIL"
    assert report["hostname"] is None
    assert report["windows"] == {"version": None, "architecture": None}
    assert report["active_unicast_ips"] == []
    assert report["services"] == [
        {
            "Name": "Svc",
            "DisplayName": None,
            "StartMode": None,
            "State": "UNKNOWN",
        }
    ]
    assert report["candidate_ports"] == [
        {
            "port": 3306,
            "occupied": None,
            "listening": None,
            "listen_addresses": [],
        }
    ]


def test_script_parses_in_windows_powershell_51() -> None:
    script = str(SCRIPT).replace("'", "''")
    command = (
        "$errors = $null; "
        f"$tokens = $null; [void][Management.Automation.Language.Parser]::ParseFile('{script}', "
        "[ref]$tokens, [ref]$errors); "
        "[pscustomobject]@{version=$PSVersionTable.PSVersion.ToString(); "
        "errors=@($errors | ForEach-Object { $_.Message })} | ConvertTo-Json -Depth 3"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stderr.decode("utf-8-sig", errors="replace")
    report = _decode_json(result.stdout)
    assert report["version"].startswith("5.1.")
    assert report["errors"] == []
