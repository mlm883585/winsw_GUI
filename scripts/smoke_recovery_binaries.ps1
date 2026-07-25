[CmdletBinding()]
param(
    [string]$DistributionDirectory = "dist-recovery",
    [string]$ObserveWindowsService = "EventLog",
    [ValidatePattern("^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    [string]$LocalServiceId = "smoke-observed-service",
    [ValidateRange(1024, 65534)]
    [int]$AgentPort = 18765,
    [ValidateRange(1024, 65534)]
    [int]$ControlPlanePort = 18766
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-PortAvailable {
    param([Parameter(Mandatory)][int]$Port)

    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
    } catch {
        throw "Loopback port $Port is already in use."
    } finally {
        $listener.Stop()
    }
}

function Wait-Json {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [hashtable]$Headers = @{}
    )

    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        try {
            return Invoke-RestMethod -Uri $Uri -Headers $Headers -TimeoutSec 2
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    throw "Timed out waiting for $Uri"
}

function Remove-RecoverySmokeState {
    param(
        [Parameter(Mandatory)][string]$SmokePath,
        [Parameter(Mandatory)][string]$TempRoot
    )

    if (-not (Test-Path -LiteralPath $SmokePath)) {
        return
    }
    $item = Get-Item -LiteralPath $SmokePath -Force
    if (
        -not $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Refusing to clean an unsafe smoke path."
    }
    $resolvedRoot = [IO.Path]::GetFullPath($TempRoot).TrimEnd([char[]]"\/")
    $resolvedPath = [IO.Path]::GetFullPath($item.FullName).TrimEnd([char[]]"\/")
    $parent = [IO.Directory]::GetParent($resolvedPath)
    if (
        $null -eq $parent -or
        -not $parent.FullName.Equals(
            $resolvedRoot,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [IO.Path]::GetFileName($resolvedPath).StartsWith(
            "winsw-recovery-smoke-",
            [StringComparison]::Ordinal
        )
    ) {
        throw "Refusing to clean a smoke path outside the managed temp scope."
    }
    Remove-Item -LiteralPath $resolvedPath -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $resolvedPath) {
        throw "Smoke state cleanup failed."
    }
}

if ($MyInvocation.InvocationName -eq ".") {
    return
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distribution = [IO.Path]::GetFullPath((Join-Path $repo $DistributionDirectory))
$agentExecutable = Join-Path $distribution "winsw-recovery-agent\winsw-recovery-agent.exe"
$controlPlaneExecutable = Join-Path $distribution "winsw-recovery-control-plane\winsw-recovery-control-plane.exe"
foreach ($executable in @($agentExecutable, $controlPlaneExecutable)) {
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "Frozen executable was not found: $executable"
    }
}

$observed = Get-Service -Name $ObserveWindowsService -ErrorAction Stop
if ($AgentPort -eq $ControlPlanePort) {
    throw "AgentPort and ControlPlanePort must be different."
}
Assert-PortAvailable -Port $AgentPort
Assert-PortAvailable -Port $ControlPlanePort

$smokeRoot = Join-Path $env:TEMP ("winsw-recovery-smoke-{0}" -f [Guid]::NewGuid())
$resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
$resolvedSmoke = [IO.Path]::GetFullPath($smokeRoot)
if (-not $resolvedSmoke.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create smoke state outside the system temporary directory."
}
New-Item -ItemType Directory -Path $resolvedSmoke | Out-Null

$token = "smoke-cluster-token-0123456789abcdef"
$sessionSecret = "smoke-session-secret-0123456789abcdef"
$passwordHash = "pbkdf2_sha256`$310000`$MDEyMzQ1Njc4OWFiY2RlZg==`$zRq57uku21zVBHW-8m1a1vsZXsstyN42UVsSbfc02uA="
$controlPlaneConfig = Join-Path $resolvedSmoke "control-plane.json"
$agentConfig = Join-Path $resolvedSmoke "agent.json"
$cpOut = Join-Path $resolvedSmoke "control-plane.out.log"
$cpErr = Join-Path $resolvedSmoke "control-plane.err.log"
$agentOut = Join-Path $resolvedSmoke "agent.out.log"
$agentErr = Join-Path $resolvedSmoke "agent.err.log"

[ordered]@{
    listen_host = "127.0.0.1"
    listen_port = $ControlPlanePort
    database_path = "control-plane.sqlite3"
    cluster_token = $token
    agent_source_cidrs = @("127.0.0.1/32")
    offline_after_seconds = 45
    agent_request_timeout_seconds = 3
    agent_request_attempts = 1
    admin_username = "admin"
    admin_password_hash = $passwordHash
    session_secret = $sessionSecret
    session_cookie_name = "recovery_admin_session"
    session_max_age_seconds = 300
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $controlPlaneConfig -Encoding utf8

[ordered]@{
    control_plane_url = "http://127.0.0.1:$ControlPlanePort"
    listen_host = "127.0.0.1"
    listen_port = $AgentPort
    advertised_endpoint = "http://127.0.0.1:$AgentPort"
    cluster_token = $token
    control_plane_source_cidrs = @("127.0.0.1/32")
    database_path = "agent.sqlite3"
    heartbeat_interval_seconds = 1
    heartbeat_jitter_ratio = 0
    heartbeat_backoff_initial_seconds = 0.2
    heartbeat_backoff_max_seconds = 1
    action_timeout_seconds = 60
    action_poll_interval_seconds = 0.5
    services = @(
        [ordered]@{
            local_service_id = $LocalServiceId
            windows_service_name = $observed.Name
            display_name = "$($observed.DisplayName) (read-only smoke)"
        }
    )
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $agentConfig -Encoding utf8

$controlPlane = $null
$agent = $null
try {
    $controlPlane = Start-Process `
        -FilePath $controlPlaneExecutable `
        -ArgumentList @("--config", ('"{0}"' -f $controlPlaneConfig)) `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $cpOut `
        -RedirectStandardError $cpErr
    $cpHealth = Wait-Json -Uri "http://127.0.0.1:$ControlPlanePort/healthz"

    $agent = Start-Process `
        -FilePath $agentExecutable `
        -ArgumentList @("--config", ('"{0}"' -f $agentConfig)) `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $agentOut `
        -RedirectStandardError $agentErr
    $agentHealth = Wait-Json -Uri "http://127.0.0.1:$AgentPort/healthz"
    $headers = @{Authorization = "Bearer $token"}
    $agentInfo = Wait-Json -Uri "http://127.0.0.1:$AgentPort/api/v1/agent" -Headers $headers
    $agentServices = Wait-Json -Uri "http://127.0.0.1:$AgentPort/api/v1/services" -Headers $headers
    if ($agentServices.items.Count -ne 1) {
        throw "Agent did not return exactly one read-only smoke service."
    }

    $session = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
    Invoke-WebRequest `
        -Uri "http://127.0.0.1:$ControlPlanePort/login" `
        -Method Post `
        -Body @{username = "admin"; password = "smoke-password"} `
        -WebSession $session `
        -UseBasicParsing `
        -TimeoutSec 5 | Out-Null

    $cpAgents = $null
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        $cpAgents = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$ControlPlanePort/api/v1/agents" `
            -WebSession $session `
            -TimeoutSec 2
        if ($cpAgents.items.Count -eq 1) { break }
        Start-Sleep -Milliseconds 200
    }
    if ($null -eq $cpAgents -or $cpAgents.items.Count -ne 1) {
        throw "Agent heartbeat did not reach the Control Plane."
    }
    $cpServices = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$ControlPlanePort/api/v1/services" `
        -WebSession $session `
        -TimeoutSec 2
    if ($cpServices.items.Count -ne 1) {
        throw "Control Plane did not mirror the observed Windows service."
    }
    $groups = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$ControlPlanePort/api/v1/recovery-groups" `
        -WebSession $session `
        -TimeoutSec 2
    if ($groups.items.Count -ne 0) {
        throw "Read-only smoke state unexpectedly contains a Recovery Group."
    }
    $dashboard = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$ControlPlanePort/" `
        -WebSession $session `
        -UseBasicParsing `
        -TimeoutSec 2
    if ($dashboard.Content -notmatch [Regex]::Escape($observed.DisplayName)) {
        throw "Dashboard did not render the mirrored Windows service."
    }

    [pscustomobject]@{
        ControlPlaneHealth = $cpHealth.status
        AgentHealth = $agentHealth.status
        SecurityMode = $agentInfo.security_mode
        AgentCount = $cpAgents.items.Count
        LocalServiceId = $cpServices.items[0].local_service_id
        InstallationState = $cpServices.items[0].installation_state
        RuntimeState = $cpServices.items[0].runtime_state
        StartupState = $cpServices.items[0].startup_state
        RecoveryGroupCount = $groups.items.Count
        DashboardStatus = $dashboard.StatusCode
        SideEffects = "NONE (no action or Run endpoint is called)"
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    foreach ($log in @($cpErr, $agentErr)) {
        if (Test-Path -LiteralPath $log) {
            Write-Host "--- $log (tail) ---"
            Get-Content -LiteralPath $log -Tail 30
        }
    }
    throw
} finally {
    if ($agent -and -not $agent.HasExited) {
        Stop-Process -Id $agent.Id -Force -ErrorAction SilentlyContinue
        [void]$agent.WaitForExit(5000)
    }
    if ($controlPlane -and -not $controlPlane.HasExited) {
        Stop-Process -Id $controlPlane.Id -Force -ErrorAction SilentlyContinue
        [void]$controlPlane.WaitForExit(5000)
    }
    if (Test-Path -LiteralPath $resolvedSmoke) {
        Remove-RecoverySmokeState `
            -SmokePath $resolvedSmoke `
            -TempRoot $env:TEMP
    }
}
