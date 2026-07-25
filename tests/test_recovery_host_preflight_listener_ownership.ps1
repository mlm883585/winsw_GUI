[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$RepositoryRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$preflight = Join-Path $RepositoryRoot "scripts\test_recovery_host_preflight.ps1"
. $preflight

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw "ASSERTION_FAILED: $Message"
    }
}

$expectedExecutable = Join-Path $env:TEMP "winsw-recovery-agent.exe"
$processes = @{
    2000 = [pscustomobject]@{
        ProcessId = 2000
        ParentProcessId = 1000
        ExecutablePath = $expectedExecutable
    }
    3000 = [pscustomobject]@{
        ProcessId = 3000
        ParentProcessId = 4000
        # Even an executable-path match is insufficient outside the wrapper tree.
        ExecutablePath = $expectedExecutable
    }
    4000 = [pscustomobject]@{
        ProcessId = 4000
        ParentProcessId = 0
        ExecutablePath = (Join-Path $env:SystemRoot "System32\cmd.exe")
    }
}

function Get-CimInstance {
    [CmdletBinding()]
    param([string]$ClassName, [string]$Filter)
    if ($ClassName -ne "Win32_Process" -or $Filter -notmatch "ProcessId = (\d+)") {
        throw "Unexpected CIM query in listener ownership test."
    }
    $processId = [int]$Matches[1]
    if ($processes.ContainsKey($processId)) {
        return $processes[$processId]
    }
    return $null
}

$validOnly = Get-ListenerOwnershipAssessment `
    -WrapperProcessId 1000 `
    -ListenerProcessIds @(2000) `
    -ExpectedExecutablePath $expectedExecutable
Assert-True $validOnly.valid "a sole valid listener was rejected"
Assert-True ($validOnly.listener_count -eq 1) "valid listener count was not reported"
Assert-True ($validOnly.valid_listener_count -eq 1) "valid listener was not counted"
Assert-True ($validOnly.invalid_listener_process_ids.Count -eq 0) `
    "valid listener was reported as invalid"

$mixed = Get-ListenerOwnershipAssessment `
    -WrapperProcessId 1000 `
    -ListenerProcessIds @(2000, 3000) `
    -ExpectedExecutablePath $expectedExecutable
Assert-True (-not $mixed.valid) `
    "mixed role and impostor listeners incorrectly passed ownership"
Assert-True ($mixed.listener_count -eq 2) "mixed listener total was not reported"
Assert-True ($mixed.valid_listener_count -eq 1) "mixed valid listener count was incorrect"
Assert-True ($mixed.invalid_listener_process_ids.Count -eq 1) `
    "impostor listener PID was not isolated"
Assert-True ($mixed.invalid_listener_process_ids[0] -eq 3000) `
    "wrong listener PID was reported as the impostor"
Assert-True ($mixed.listeners.Count -eq 2) "per-listener evidence was incomplete"
Assert-True (-not $mixed.listeners[1].ancestry_reaches_wrapper) `
    "impostor listener incorrectly reached the wrapper process tree"

[pscustomobject][ordered]@{
    outcome = "PASS"
    side_effects = "NONE"
    scenarios = @(
        "sole_valid_listener_passes",
        "mixed_valid_and_impostor_listener_fails"
    )
} | ConvertTo-Json -Depth 5
