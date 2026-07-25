[CmdletBinding()]
param(
    [string]$Role,
    [string]$ConfigPath,
    [string]$DataDirectory,
    [string]$RuntimeMode,
    [string]$Stage = "PreInstall",
    [string]$Python = "python",
    [string]$PackageDirectory,
    [string[]]$BusinessServiceName = @(),
    [switch]$PassThru
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Stable process exit codes. Parameter validation is deliberately performed in
# the script so expected operator errors still produce the structured report.
$ExitCode = [ordered]@{
    PASS = 0
    INPUT_INVALID = 2
    HOST_UNSUPPORTED = 3
    RUNTIME_INVALID = 4
    CONFIG_INVALID = 5
    SERVICE_INVALID = 6
    PORT_IN_USE = 7
    ACL_INVALID = 8
    UNEXPECTED_ERROR = 10
}

$checks = [System.Collections.Generic.List[object]]::new()
$script:resolvedBusinessServices = @()

function Add-PreflightCheck {
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][string]$Category,
        [Parameter(Mandatory)][string]$Status,
        [Parameter(Mandatory)][string]$Message,
        [int]$FailureCode = 0,
        [object]$Evidence = $null
    )

    $checks.Add([pscustomobject][ordered]@{
        id = $Id
        category = $Category
        status = $Status
        failure_code = if ($Status -eq "FAIL") { $FailureCode } else { $null }
        message = $Message
        evidence = $Evidence
    })
}

function Test-PathContainedBy {
    param(
        [Parameter(Mandatory)][string]$Candidate,
        [Parameter(Mandatory)][string]$Directory
    )

    $candidatePath = [IO.Path]::GetFullPath($Candidate)
    $directoryPath = [IO.Path]::GetFullPath($Directory)
    $root = [IO.Path]::GetPathRoot($directoryPath)
    if ($directoryPath.TrimEnd([char[]]"\/").Equals(
        $root.TrimEnd([char[]]"\/"),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        return $false
    }
    $prefix = $directoryPath.TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
    return $candidatePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-SidValue {
    param([Parameter(Mandatory)][Security.Principal.IdentityReference]$Identity)

    try {
        return $Identity.Translate([Security.Principal.SecurityIdentifier]).Value
    } catch {
        return $Identity.Value
    }
}

function Get-AclAssessment {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [switch]$RequireProtected,
        [switch]$AllowSystemOwner
    )

    $requiredSids = @("S-1-5-18", "S-1-5-32-544")
    $item = Get-Item -LiteralPath $LiteralPath -Force
    $isDirectory = $item -is [IO.DirectoryInfo]
    $acl = Get-Acl -LiteralPath $LiteralPath
    $ownerReference = [Security.Principal.NTAccount]::new($acl.Owner)
    $ownerSid = ConvertTo-SidValue -Identity $ownerReference
    $unexpectedRules = [System.Collections.Generic.List[object]]::new()
    $fullControlSids = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $inheritableFullControlSids = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )

    foreach ($rule in $acl.Access) {
        $sid = ConvertTo-SidValue -Identity $rule.IdentityReference
        $isExpectedAllow = (
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $requiredSids -contains $sid
        )
        $hasFullControl = (
            ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
            [Security.AccessControl.FileSystemRights]::FullControl
        )
        $requiredInheritance = (
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        )
        $inheritanceIsSafe = if ($isDirectory) {
            ($rule.InheritanceFlags -band $requiredInheritance) -eq $requiredInheritance -and
            $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None
        } else {
            $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None
        }
        if (-not $isExpectedAllow -or -not $hasFullControl -or -not $inheritanceIsSafe) {
            $unexpectedRules.Add([pscustomobject]@{
                sid = $sid
                type = $rule.AccessControlType.ToString()
                rights = $rule.FileSystemRights.ToString()
                inheritance = $rule.InheritanceFlags.ToString()
                propagation = $rule.PropagationFlags.ToString()
                inherited = [bool]$rule.IsInherited
            })
            continue
        }
        if ($hasFullControl) {
            [void]$fullControlSids.Add($sid)
            if (-not $isDirectory -or
                ($rule.InheritanceFlags -band $requiredInheritance) -eq $requiredInheritance) {
                [void]$inheritableFullControlSids.Add($sid)
            }
        }
    }

    $missingFullControl = @($requiredSids | Where-Object { -not $fullControlSids.Contains($_) })
    $missingInheritableFullControl = if ($isDirectory) {
        @($requiredSids | Where-Object { -not $inheritableFullControlSids.Contains($_) })
    } else {
        @()
    }
    $missingInheritableFullControl = @($missingInheritableFullControl)
    $allowedOwnerSids = if ($AllowSystemOwner) {
        @("S-1-5-32-544", "S-1-5-18")
    } else {
        @("S-1-5-32-544")
    }
    $protectionValid = -not $RequireProtected -or $acl.AreAccessRulesProtected
    $valid = (
        $protectionValid -and
        $allowedOwnerSids -contains $ownerSid -and
        $unexpectedRules.Count -eq 0 -and
        $missingFullControl.Count -eq 0 -and
        $missingInheritableFullControl.Count -eq 0
    )
    return [pscustomobject][ordered]@{
        path = [IO.Path]::GetFullPath($LiteralPath)
        path_type = if ($isDirectory) { "DIRECTORY" } else { "FILE" }
        policy = if ($RequireProtected) { "PROTECTED_ROOT" } else { "SECURE_DESCENDANT" }
        valid = $valid
        inheritance_protected = [bool]$acl.AreAccessRulesProtected
        owner_sid = $ownerSid
        allowed_owner_sids = @($allowedOwnerSids)
        missing_full_control_sids = @($missingFullControl)
        missing_inheritable_full_control_sids = @($missingInheritableFullControl)
        unexpected_rules = @($unexpectedRules)
    }
}

function Get-DataTreeItems {
    param([Parameter(Mandatory)][string]$Root)

    $result = [System.Collections.Generic.List[object]]::new()
    $pending = [System.Collections.Generic.Queue[IO.DirectoryInfo]]::new()
    $rootItem = Get-Item -LiteralPath $Root -Force
    $result.Add($rootItem)
    $pending.Enqueue($rootItem)
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($child in Get-ChildItem -LiteralPath $directory.FullName -Force) {
            $result.Add($child)
            $isReparsePoint = (
                $child.Attributes -band [IO.FileAttributes]::ReparsePoint
            ) -ne 0
            if ($child -is [IO.DirectoryInfo] -and -not $isReparsePoint) {
                $pending.Enqueue($child)
            }
        }
    }
    return @($result)
}

function Get-ServiceObservation {
    param(
        [Parameter(Mandatory)][string]$Name,
        [switch]$IncludeDeploymentIdentity
    )

    if (
        [string]::IsNullOrWhiteSpace($Name) -or
        $Name.Length -gt 256 -or
        $Name -ne $Name.Trim() -or
        $Name.IndexOfAny([char[]]"\/") -ge 0
    ) {
        throw "Windows service name is invalid."
    }

    $registryPath = "Registry::HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\$Name"
    if (-not (Test-Path -LiteralPath $registryPath -PathType Container)) {
        return $null
    }

    $properties = Get-ItemProperty -LiteralPath $registryPath
    $startValue = [int]$properties.Start
    $startMode = switch ($startValue) {
        0 { "Boot" }
        1 { "System" }
        2 { "Automatic" }
        3 { "Manual" }
        4 { "Disabled" }
        default { "Unknown" }
    }
    try {
        Add-Type -AssemblyName System.ServiceProcess.ServiceController
    } catch {
        Add-Type -AssemblyName System.ServiceProcess
    }
    $service = [System.ServiceProcess.ServiceController]::new($Name, ".")
    try {
        $scmStatus = $service.Status.ToString()
        $displayName = $service.DisplayName
    } finally {
        $service.Dispose()
    }
    $runtimeState = switch ($scmStatus) {
        "Running" { "ACTIVE" }
        "Stopped" { "INACTIVE" }
        "StartPending" { "STARTING" }
        "StopPending" { "STOPPING" }
        default { "UNKNOWN" }
    }
    return [pscustomobject][ordered]@{
        service_name = $Name
        display_name = $displayName
        start_mode = $startMode
        start_value = $startValue
        scm_status = $scmStatus
        runtime_state = $runtimeState
        service_account = if ($IncludeDeploymentIdentity -and
            $properties.PSObject.Properties["ObjectName"]) {
            [string]$properties.ObjectName
        } else {
            $null
        }
        image_path = if ($IncludeDeploymentIdentity -and
            $properties.PSObject.Properties["ImagePath"]) {
            [string]$properties.ImagePath
        } else {
            $null
        }
    }
}

function Get-LocalPortObservation {
    param(
        [Parameter(Mandatory)][string]$ListenHost,
        [Parameter(Mandatory)][int]$ListenPort
    )

    $connections = @(
        Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue
    )
    $listeners = @($connections | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort)" })
    $owningProcessIds = @(
        $connections | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique
    )
    return [pscustomobject][ordered]@{
        listen_host = $ListenHost
        listen_port = $ListenPort
        occupied = $listeners.Count -gt 0
        local_listeners = $listeners
        owning_process_ids = $owningProcessIds
    }
}

function Get-ListenerOwnershipAssessment {
    param(
        [Parameter(Mandatory)][int]$WrapperProcessId,
        [Parameter(Mandatory)][int[]]$ListenerProcessIds,
        [Parameter(Mandatory)][string]$ExpectedExecutablePath
    )

    $expected = [IO.Path]::GetFullPath($ExpectedExecutablePath)
    $assessments = [System.Collections.Generic.List[object]]::new()
    foreach ($listenerProcessId in $ListenerProcessIds) {
        $seen = [System.Collections.Generic.HashSet[int]]::new()
        $chain = [System.Collections.Generic.List[int]]::new()
        $currentProcessId = [int]$listenerProcessId
        $listenerExecutableMatches = $false
        $ancestryReachesWrapper = $false
        $processQueryFailed = $false
        for ($depth = 0; $depth -lt 8 -and $currentProcessId -gt 0; $depth++) {
            if (-not $seen.Add($currentProcessId)) { break }
            $chain.Add($currentProcessId)
            try {
                $process = Get-CimInstance `
                    -ClassName Win32_Process `
                    -Filter "ProcessId = $currentProcessId" `
                    -ErrorAction Stop
            } catch {
                $processQueryFailed = $true
                break
            }
            if ($null -eq $process) { break }
            if ($depth -eq 0 -and -not [string]::IsNullOrWhiteSpace($process.ExecutablePath)) {
                try {
                    $listenerExecutable = [IO.Path]::GetFullPath([string]$process.ExecutablePath)
                    $listenerExecutableMatches = $listenerExecutable.Equals(
                        $expected,
                        [StringComparison]::OrdinalIgnoreCase
                    )
                } catch {
                    $listenerExecutableMatches = $false
                }
            }
            if ([int]$process.ParentProcessId -eq $WrapperProcessId) {
                $ancestryReachesWrapper = $true
                break
            }
            $currentProcessId = [int]$process.ParentProcessId
        }
        $assessments.Add([pscustomobject][ordered]@{
            valid = ($listenerExecutableMatches -and $ancestryReachesWrapper)
            listener_process_id = [int]$listenerProcessId
            wrapper_process_id = $WrapperProcessId
            listener_executable_matches = $listenerExecutableMatches
            ancestry_reaches_wrapper = $ancestryReachesWrapper
            process_query_failed = $processQueryFailed
            process_chain = @($chain)
        })
    }

    $invalidListenerProcessIds = @(
        $assessments |
            Where-Object { -not $_.valid } |
            ForEach-Object { [int]$_.listener_process_id }
    )
    $validListenerCount = @($assessments | Where-Object { $_.valid }).Count
    return [pscustomobject][ordered]@{
        valid = ($assessments.Count -gt 0 -and $invalidListenerProcessIds.Count -eq 0)
        listener_count = $assessments.Count
        valid_listener_count = $validListenerCount
        invalid_listener_process_ids = $invalidListenerProcessIds
        wrapper_process_id = $WrapperProcessId
        listeners = @($assessments)
    }
}

function Invoke-RecoveryHostPreflight {
    $roleValid = $Role -in @("Agent", "ControlPlane")
    $runtimeValid = $RuntimeMode -in @("Python", "Frozen")
    $stageValid = $Stage -in @("PreInstall", "PostInstall")
    $missing = [System.Collections.Generic.List[string]]::new()
    if ([string]::IsNullOrWhiteSpace($Role)) { $missing.Add("Role") }
    if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $missing.Add("ConfigPath") }
    if ([string]::IsNullOrWhiteSpace($DataDirectory)) { $missing.Add("DataDirectory") }
    if ([string]::IsNullOrWhiteSpace($RuntimeMode)) { $missing.Add("RuntimeMode") }
    if ($RuntimeMode -eq "Frozen" -and [string]::IsNullOrWhiteSpace($PackageDirectory)) {
        $missing.Add("PackageDirectory")
    }

    if ($missing.Count -gt 0 -or -not $roleValid -or -not $runtimeValid -or -not $stageValid) {
        Add-PreflightCheck `
            -Id "INPUT_PARAMETERS" `
            -Category "INPUT" `
            -Status "FAIL" `
            -FailureCode $ExitCode.INPUT_INVALID `
            -Message "Required parameters or fixed enum values are invalid." `
            -Evidence ([pscustomobject]@{
                missing = @($missing)
                accepted_roles = @("Agent", "ControlPlane")
                accepted_runtime_modes = @("Python", "Frozen")
                accepted_stages = @("PreInstall", "PostInstall")
            })
        return
    }
    if ($Role -eq "ControlPlane" -and $BusinessServiceName.Count -gt 0) {
        Add-PreflightCheck `
            -Id "INPUT_SERVICE_SCOPE" `
            -Category "INPUT" `
            -Status "FAIL" `
            -FailureCode $ExitCode.INPUT_INVALID `
            -Message "Business services may only be checked on an Agent host." `
            -Evidence ([pscustomobject]@{ requested_count = $BusinessServiceName.Count })
        return
    }
    if ($Stage -eq "PostInstall" -and $RuntimeMode -ne "Frozen") {
        Add-PreflightCheck `
            -Id "INPUT_POSTINSTALL_RUNTIME" `
            -Category "INPUT" `
            -Status "FAIL" `
            -FailureCode $ExitCode.INPUT_INVALID `
            -Message "PostInstall must inspect the installed frozen onedir package." `
            -Evidence ([pscustomobject]@{
                stage = $Stage
                runtime_mode = $RuntimeMode
                required_runtime_mode = "Frozen"
            })
        return
    }
    Add-PreflightCheck `
        -Id "INPUT_PARAMETERS" `
        -Category "INPUT" `
        -Status "PASS" `
        -Message "Input scope is explicit and local to this host." `
        -Evidence ([pscustomobject]@{
            role = $Role
            runtime_mode = $RuntimeMode
            stage = $Stage
        })

    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem
        $isServer = [int]$os.ProductType -in @(2, 3)
        Add-PreflightCheck `
            -Id "HOST_WINDOWS_SERVER" `
            -Category "HOST" `
            -Status $(if ($isServer) { "PASS" } else { "FAIL" }) `
            -FailureCode $ExitCode.HOST_UNSUPPORTED `
            -Message $(if ($isServer) { "Host is Windows Server." } else { "Host is not Windows Server." }) `
            -Evidence ([pscustomobject]@{
                caption = $os.Caption
                version = $os.Version
                product_type = [int]$os.ProductType
                os_architecture = $os.OSArchitecture
            })
    } catch {
        Add-PreflightCheck `
            -Id "HOST_WINDOWS_SERVER" `
            -Category "HOST" `
            -Status "FAIL" `
            -FailureCode $ExitCode.HOST_UNSUPPORTED `
            -Message "Unable to identify the local Windows operating system." `
            -Evidence ([pscustomobject]@{ exception_type = $_.Exception.GetType().FullName })
    }

    $is64Bit = [Environment]::Is64BitOperatingSystem
    Add-PreflightCheck `
        -Id "HOST_64_BIT" `
        -Category "HOST" `
        -Status $(if ($is64Bit) { "PASS" } else { "FAIL" }) `
        -FailureCode $ExitCode.HOST_UNSUPPORTED `
        -Message $(if ($is64Bit) { "Host OS is 64-bit." } else { "Host OS must be 64-bit." }) `
        -Evidence ([pscustomobject]@{ is_64_bit = $is64Bit })

    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdministrator = ([Security.Principal.WindowsPrincipal]$currentIdentity).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator
        )
    Add-PreflightCheck `
        -Id "HOST_ELEVATED_READ" `
        -Category "HOST" `
        -Status $(if ($isAdministrator) { "PASS" } else { "FAIL" }) `
        -FailureCode $ExitCode.HOST_UNSUPPORTED `
        -Message $(if ($isAdministrator) {
            "Elevated token is available for a complete local audit."
        } else {
            "Run the read-only preflight from an elevated PowerShell session."
        }) `
        -Evidence ([pscustomobject]@{ administrator = $isAdministrator })

    $runtimeCommand = $null
    $runtimePrefixArguments = @()
    $package = $null
    if ($RuntimeMode -eq "Python") {
        try {
            $pythonCommand = Get-Command -Name $Python -CommandType Application | Select-Object -First 1
            $versionOutput = (& $pythonCommand.Source --version 2>&1 | Out-String).Trim()
            $pythonExit = $LASTEXITCODE
            $versionMatch = [regex]::Match($versionOutput, "Python\s+(\d+\.\d+\.\d+)")
            $pythonOk = $pythonExit -eq 0 -and $versionMatch.Success -and
                $versionMatch.Groups[1].Value.StartsWith("3.13.")
            if ($pythonOk) {
                $runtimeCommand = $pythonCommand.Source
                $runtimePrefixArguments = @(
                    "-B",
                    "-m",
                    $(if ($Role -eq "Agent") {
                        "orchestrator.agent"
                    } else {
                        "orchestrator.control_plane"
                    })
                )
            }
            Add-PreflightCheck `
                -Id "RUNTIME_PYTHON_313" `
                -Category "RUNTIME" `
                -Status $(if ($pythonOk) { "PASS" } else { "FAIL" }) `
                -FailureCode $ExitCode.RUNTIME_INVALID `
                -Message $(if ($pythonOk) {
                    "Python 3.13 runtime is available."
                } else {
                    "Python runtime must report version 3.13.x."
                }) `
                -Evidence ([pscustomobject]@{
                    executable = $pythonCommand.Source
                    version = if ($versionMatch.Success) { $versionMatch.Groups[1].Value } else { $null }
                    process_exit_code = $pythonExit
                })
        } catch {
            Add-PreflightCheck `
                -Id "RUNTIME_PYTHON_313" `
                -Category "RUNTIME" `
                -Status "FAIL" `
                -FailureCode $ExitCode.RUNTIME_INVALID `
                -Message "Python runtime could not be located or queried." `
                -Evidence ([pscustomobject]@{ exception_type = $_.Exception.GetType().FullName })
        }
    } else {
        try {
            $package = (Resolve-Path -LiteralPath $PackageDirectory).Path
            $executableName = if ($Role -eq "Agent") {
                "winsw-recovery-agent.exe"
            } else {
                "winsw-recovery-control-plane.exe"
            }
            $executable = Join-Path $package $executableName
            $internal = Join-Path $package "_internal"
            if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
                throw "Frozen executable is missing."
            }
            if (-not (Test-Path -LiteralPath $internal -PathType Container)) {
                throw "Frozen package _internal directory is missing."
            }
            $helpOutput = (& $executable --help 2>&1 | Out-String)
            $frozenExit = $LASTEXITCODE
            $frozenOk = (
                $frozenExit -eq 0 -and
                $helpOutput -match "--config" -and
                $helpOutput -match "--check-config"
            )
            if ($frozenOk) {
                $runtimeCommand = $executable
            }
            Add-PreflightCheck `
                -Id "RUNTIME_FROZEN_PACKAGE" `
                -Category "RUNTIME" `
                -Status $(if ($frozenOk) { "PASS" } else { "FAIL" }) `
                -FailureCode $ExitCode.RUNTIME_INVALID `
                -Message $(if ($frozenOk) {
                    "Frozen onedir package starts its command-line parser."
                } else {
                    "Frozen onedir package did not pass its command-line smoke check."
                }) `
                -Evidence ([pscustomobject]@{
                    package_directory = $package
                    executable = $executable
                    executable_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $executable).Hash.ToLowerInvariant()
                    process_exit_code = $frozenExit
                })
        } catch {
            Add-PreflightCheck `
                -Id "RUNTIME_FROZEN_PACKAGE" `
                -Category "RUNTIME" `
                -Status "FAIL" `
                -FailureCode $ExitCode.RUNTIME_INVALID `
                -Message "Frozen onedir package is missing, incomplete, or not runnable." `
                -Evidence ([pscustomobject]@{ exception_type = $_.Exception.GetType().FullName })
        }
    }

    $configObject = $null
    $config = $null
    $data = $null
    $database = $null
    $configFailureRecorded = $false
    try {
        $config = (Resolve-Path -LiteralPath $ConfigPath).Path
        if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
            throw "Configuration path is not a file."
        }
        $data = (Resolve-Path -LiteralPath $DataDirectory).Path
        if (-not (Test-Path -LiteralPath $data -PathType Container)) {
            throw "Data directory does not exist."
        }
        if (-not (Test-PathContainedBy -Candidate $config -Directory $data)) {
            throw "Configuration must be inside the dedicated non-root data directory."
        }
        if ($Stage -eq "PostInstall") {
            $expectedInstalledPackage = [IO.Path]::GetFullPath((Join-Path $data "package"))
            $installedPackageMatches = (
                $null -ne $package -and
                $package.Equals(
                    $expectedInstalledPackage,
                    [StringComparison]::OrdinalIgnoreCase
                )
            )
            Add-PreflightCheck `
                -Id "RUNTIME_INSTALLED_PACKAGE" `
                -Category "RUNTIME" `
                -Status $(if ($installedPackageMatches) { "PASS" } else { "FAIL" }) `
                -FailureCode $ExitCode.RUNTIME_INVALID `
                -Message $(if ($installedPackageMatches) {
                    "PostInstall is inspecting the package copied into DataDirectory."
                } else {
                    "PostInstall PackageDirectory must equal DataDirectory\\package."
                }) `
                -Evidence ([pscustomobject]@{
                    package_directory = $package
                    expected_package_directory = $expectedInstalledPackage
                })
        }
        $configObject = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
        if ($null -eq $configObject -or
            $configObject -isnot [System.Management.Automation.PSCustomObject]) {
            throw "Configuration root must be an object."
        }
        $requiredFields = if ($Role -eq "Agent") {
            @(
                "control_plane_url", "advertised_endpoint", "cluster_token",
                "control_plane_source_cidrs", "services"
            )
        } else {
            @(
                "cluster_token", "agent_source_cidrs", "admin_password_hash",
                "session_secret"
            )
        }
        $missingConfigFields = @(
            $requiredFields | Where-Object { $null -eq $configObject.PSObject.Properties[$_] }
        )
        if ($missingConfigFields.Count -gt 0) {
            throw "Configuration does not match the selected role."
        }
        if ($null -eq $runtimeCommand) {
            Add-PreflightCheck `
                -Id "CONFIG_AUTHORITATIVE_SCHEMA" `
                -Category "CONFIG" `
                -Status "SKIP" `
                -Message "Authoritative configuration validation requires a valid selected runtime." `
                -Evidence ([pscustomobject]@{ reason = "runtime validation failed" })
        } else {
            $configCheckArguments = @($runtimePrefixArguments) + @(
                "--config", $config, "--check-config"
            )
            $previousErrorActionPreference = $ErrorActionPreference
            try {
                # Native stderr is expected for a rejected config. Capture it
                # without turning that documented exit path into a PowerShell
                # terminating error; raw output is never copied into the report.
                $ErrorActionPreference = "Continue"
                $configCheckOutput = (
                    & $runtimeCommand @configCheckArguments 2>&1 | Out-String
                ).Trim()
                $configCheckExit = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }
            $configCheckResult = $null
            if ($configCheckExit -eq 0) {
                try {
                    $configCheckResult = $configCheckOutput | ConvertFrom-Json
                } catch {
                    $configCheckResult = $null
                }
            }
            $expectedComponent = if ($Role -eq "Agent") { "agent" } else { "control-plane" }
            $configSchemaOk = (
                $configCheckExit -eq 0 -and
                $null -ne $configCheckResult -and
                $configCheckResult.config_valid -eq $true -and
                $configCheckResult.component -eq $expectedComponent
            )
            Add-PreflightCheck `
                -Id "CONFIG_AUTHORITATIVE_SCHEMA" `
                -Category "CONFIG" `
                -Status $(if ($configSchemaOk) { "PASS" } else { "FAIL" }) `
                -FailureCode $ExitCode.CONFIG_INVALID `
                -Message $(if ($configSchemaOk) {
                    "Configuration passed the role's authoritative strict schema loader."
                } else {
                    "Configuration failed the role's authoritative strict schema loader."
                }) `
                -Evidence ([pscustomobject]@{
                    component = $expectedComponent
                    process_exit_code = $configCheckExit
                    service_count = if ($configSchemaOk -and $Role -eq "Agent") {
                        [int]$configCheckResult.service_count
                    } else {
                        $null
                    }
                    sensitive_values_emitted = $false
                })
            if (-not $configSchemaOk) {
                $configFailureRecorded = $true
                $configObject = $null
                throw "Authoritative configuration validation failed."
            }
        }
        $databaseProperty = $configObject.PSObject.Properties["database_path"]
        $databaseSetting = if ($databaseProperty -and $databaseProperty.Value) {
            [string]$databaseProperty.Value
        } elseif ($Role -eq "Agent") {
            "data/agent.sqlite3"
        } else {
            "data/control-plane.sqlite3"
        }
        $database = if ([IO.Path]::IsPathRooted($databaseSetting)) {
            [IO.Path]::GetFullPath($databaseSetting)
        } else {
            [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $config) $databaseSetting))
        }
        if (-not (Test-PathContainedBy -Candidate $database -Directory $data)) {
            throw "database_path must resolve inside the data directory."
        }
        if ((Test-Path -LiteralPath $database) -and
            -not (Test-Path -LiteralPath $database -PathType Leaf)) {
            throw "database_path exists but is not a file."
        }

        $listenHostProperty = $configObject.PSObject.Properties["listen_host"]
        $listenHost = if ($listenHostProperty -and $listenHostProperty.Value) {
            [string]$listenHostProperty.Value
        } else {
            "0.0.0.0"
        }
        $listenPortProperty = $configObject.PSObject.Properties["listen_port"]
        $listenPort = if ($listenPortProperty -and $listenPortProperty.Value) {
            [int]$listenPortProperty.Value
        } elseif ($Role -eq "Agent") {
            8765
        } else {
            8766
        }
        if ($listenPort -lt 1 -or $listenPort -gt 65535) {
            throw "listen_port must be between 1 and 65535."
        }
        Add-PreflightCheck `
            -Id "CONFIG_LAYOUT" `
            -Category "CONFIG" `
            -Status "PASS" `
            -Message "Configuration and database paths stay inside the dedicated data directory." `
            -Evidence ([pscustomobject]@{
                config_path = $config
                data_directory = $data
                database_path = $database
                listen_host = $listenHost
                listen_port = $listenPort
            })

        $port = Get-LocalPortObservation -ListenHost $listenHost -ListenPort $listenPort
        $roleServiceName = $null
        $roleService = $null
        $roleProcessId = 0
        $roleOwnsPort = $false
        $expectedRoleImagePath = $null
        $roleImageMatches = $false
        $roleAccountMatches = $false
        $roleXmlMatches = $false
        $roleXmlPath = $null
        $listenerOwnership = $null
        $expectedApplicationExecutable = $null
        if ($Stage -eq "PostInstall") {
            $roleServiceName = if ($Role -eq "Agent") {
                "winsw-recovery-agent"
            } else {
                "winsw-recovery-control-plane"
            }
            $applicationExecutableName = if ($Role -eq "Agent") {
                "winsw-recovery-agent.exe"
            } else {
                "winsw-recovery-control-plane.exe"
            }
            $expectedApplicationExecutable = [IO.Path]::GetFullPath((
                Join-Path (Join-Path $data "package") $applicationExecutableName
            ))
            $roleService = Get-ServiceObservation `
                -Name $roleServiceName `
                -IncludeDeploymentIdentity
            $expectedRoleImagePath = [IO.Path]::GetFullPath((
                Join-Path (Join-Path $data "service") "$roleServiceName.exe"
            ))
            if ($null -ne $roleService) {
                $roleAccountMatches = $roleService.service_account -in @(
                    "LocalSystem", "NT AUTHORITY\SYSTEM"
                )
                if (-not [string]::IsNullOrWhiteSpace($roleService.image_path)) {
                    $actualImagePath = [Environment]::ExpandEnvironmentVariables(
                        $roleService.image_path
                    ).Trim().Trim([char]'"')
                    $roleImageMatches = $actualImagePath.Equals(
                        $expectedRoleImagePath,
                        [StringComparison]::OrdinalIgnoreCase
                    )
                }
                $roleCim = Get-CimInstance `
                    -ClassName Win32_Service `
                    -Filter "Name = '$roleServiceName'"
                if ($null -ne $roleCim) {
                    $roleProcessId = [int]$roleCim.ProcessId
                }
                if ($roleProcessId -gt 0 -and $port.owning_process_ids.Count -gt 0) {
                    $listenerOwnership = Get-ListenerOwnershipAssessment `
                        -WrapperProcessId $roleProcessId `
                        -ListenerProcessIds @($port.owning_process_ids) `
                        -ExpectedExecutablePath $expectedApplicationExecutable
                    $roleOwnsPort = [bool]$listenerOwnership.valid
                }
            }
            $roleXmlPath = Join-Path (Join-Path $data "service") "$roleServiceName.xml"
            if (Test-Path -LiteralPath $roleXmlPath -PathType Leaf) {
                try {
                    $xmlSettings = [Xml.XmlReaderSettings]::new()
                    $xmlSettings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
                    $xmlSettings.XmlResolver = $null
                    $xmlReader = [Xml.XmlReader]::Create($roleXmlPath, $xmlSettings)
                    try {
                        $roleXml = [Xml.XmlDocument]::new()
                        $roleXml.XmlResolver = $null
                        $roleXml.Load($xmlReader)
                    } finally {
                        $xmlReader.Dispose()
                    }
                    $xmlApplicationExecutable = [IO.Path]::GetFullPath(
                        [Environment]::ExpandEnvironmentVariables(
                            [string]$roleXml.service.executable
                        )
                    )
                    $xmlWorkingDirectory = [IO.Path]::GetFullPath(
                        [Environment]::ExpandEnvironmentVariables(
                            [string]$roleXml.service.workingdirectory
                        )
                    )
                    $expectedArguments = '--config "{0}"' -f $config
                    $roleXmlMatches = (
                        $xmlApplicationExecutable.Equals(
                            $expectedApplicationExecutable,
                            [StringComparison]::OrdinalIgnoreCase
                        ) -and
                        $xmlWorkingDirectory.Equals(
                            $data,
                            [StringComparison]::OrdinalIgnoreCase
                        ) -and
                        [string]$roleXml.service.arguments -eq $expectedArguments -and
                        [string]$roleXml.service.startmode -eq "Automatic"
                    )
                } catch {
                    $roleXmlMatches = $false
                }
            }
        }
        $portOk = if ($Stage -eq "PreInstall") {
            -not $port.occupied
        } else {
            $port.occupied -and $roleOwnsPort
        }
        Add-PreflightCheck `
            -Id $(if ($Role -eq "Agent") { "PORT_AGENT_LISTEN" } else { "PORT_CONTROL_PLANE_LISTEN" }) `
            -Category "PORT" `
            -Status $(if ($portOk) { "PASS" } else { "FAIL" }) `
            -FailureCode $ExitCode.PORT_IN_USE `
            -Message $(if ($Stage -eq "PreInstall" -and $portOk) {
                "Configured local listen port is free before installation."
            } elseif ($Stage -eq "PreInstall") {
                "Configured local listen port is already occupied before installation."
            } elseif ($portOk) {
                "Every configured-port listener PID belongs to the installed Recovery role process tree."
            } else {
                "Configured local listen port is absent or at least one listener PID is outside the installed Recovery role process tree."
            }) `
            -Evidence ([pscustomobject]@{
                stage = $Stage
                expected = if ($Stage -eq "PreInstall") { "FREE" } else { "OCCUPIED" }
                expected_process_id = if ($Stage -eq "PostInstall") { $roleProcessId } else { $null }
                ownership = $listenerOwnership
                observation = $port
            })

        if ($Stage -eq "PostInstall") {
            $roleServiceOk = (
                $null -ne $roleService -and
                $roleService.start_mode -eq "Automatic" -and
                $roleService.scm_status -eq "Running" -and
                $roleProcessId -gt 0 -and
                $roleAccountMatches -and
                $roleImageMatches -and
                $roleXmlMatches
            )
            Add-PreflightCheck `
                -Id "DEPLOYMENT_ROLE_SERVICE" `
                -Category "DEPLOYMENT" `
                -Status $(if ($roleServiceOk) { "PASS" } else { "FAIL" }) `
                -FailureCode $ExitCode.SERVICE_INVALID `
                -Message $(if ($roleServiceOk) {
                    "Recovery role service uses the expected LocalSystem/ImagePath boundary and is Automatic + Running."
                } else {
                    "Recovery role service identity, ImagePath/XML binding, startup type, or runtime state is invalid."
                }) `
                -Evidence ([pscustomobject]@{
                    service_name = $roleServiceName
                    installed = $null -ne $roleService
                    observation = $roleService
                    process_id = $roleProcessId
                    expected_image_path = $expectedRoleImagePath
                    image_path_matches = $roleImageMatches
                    service_account_matches = $roleAccountMatches
                    winsw_xml_path = $roleXmlPath
                    winsw_xml_matches = $roleXmlMatches
                })
        }

        if ($Role -eq "Agent") {
            $cpUrlProperty = $configObject.PSObject.Properties["control_plane_url"]
            Add-PreflightCheck `
                -Id "REMOTE_CONTROL_PLANE_NOT_SCANNED" `
                -Category "SCOPE" `
                -Status "SKIP" `
                -Message "Remote Control Plane reachability is intentionally outside local preflight scope." `
                -Evidence ([pscustomobject]@{
                    configured = [bool]($cpUrlProperty -and $cpUrlProperty.Value)
                    reason = "local-only policy"
                })
        }
    } catch {
        if (-not $configFailureRecorded) {
            Add-PreflightCheck `
                -Id "CONFIG_LAYOUT" `
                -Category "CONFIG" `
                -Status "FAIL" `
                -FailureCode $ExitCode.CONFIG_INVALID `
                -Message "Configuration JSON, data directory, database path, or listen port is invalid." `
                -Evidence ([pscustomobject]@{ exception_type = $_.Exception.GetType().FullName })
        }
    }

    if ($Role -eq "Agent" -and $null -ne $configObject) {
        try {
            $configuredServices = @(
                $configObject.services |
                    ForEach-Object { [string]$_.windows_service_name }
            )
            $requestedServices = if ($BusinessServiceName.Count -gt 0) {
                @($BusinessServiceName)
            } else {
                @($configuredServices)
            }
            $requestedServices = @($requestedServices)
            $script:resolvedBusinessServices = @($requestedServices)
            if ($requestedServices.Count -eq 0) {
                throw "At least one explicitly configured business service is required."
            }
            $unique = @($requestedServices | Sort-Object -Unique)
            if ($unique.Count -ne $requestedServices.Count) {
                throw "Duplicate business service names are not allowed."
            }
            foreach ($serviceName in $requestedServices) {
                if ($configuredServices -notcontains $serviceName) {
                    Add-PreflightCheck `
                        -Id "SERVICE_ALLOWLIST_$serviceName" `
                        -Category "SERVICE" `
                        -Status "FAIL" `
                        -FailureCode $ExitCode.SERVICE_INVALID `
                        -Message "Requested business service is not present in the Agent allowlist." `
                        -Evidence ([pscustomobject]@{ service_name = $serviceName })
                    continue
                }
                $observation = Get-ServiceObservation -Name $serviceName
                if ($null -eq $observation) {
                    Add-PreflightCheck `
                        -Id "SERVICE_EXISTS_$serviceName" `
                        -Category "SERVICE" `
                        -Status "FAIL" `
                        -FailureCode $ExitCode.SERVICE_INVALID `
                        -Message "Specified Windows service is not installed." `
                        -Evidence ([pscustomobject]@{ service_name = $serviceName })
                    continue
                }
                Add-PreflightCheck `
                    -Id "SERVICE_EXISTS_$serviceName" `
                    -Category "SERVICE" `
                    -Status "PASS" `
                    -Message "Specified Windows service exists; current state was observed." `
                    -Evidence $observation
                Add-PreflightCheck `
                    -Id "SERVICE_MANUAL_$serviceName" `
                    -Category "SERVICE" `
                    -Status $(if ($observation.start_mode -eq "Manual") { "PASS" } else { "FAIL" }) `
                    -FailureCode $ExitCode.SERVICE_INVALID `
                    -Message $(if ($observation.start_mode -eq "Manual") {
                        "Business service startup type is Manual."
                    } else {
                        "Business service startup type must be Manual before recovery is enabled."
                    }) `
                    -Evidence ([pscustomobject]@{
                        service_name = $serviceName
                        start_mode = $observation.start_mode
                        scm_status = $observation.scm_status
                        runtime_state = $observation.runtime_state
                    })
            }
        } catch {
            Add-PreflightCheck `
                -Id "SERVICE_SCOPE" `
                -Category "SERVICE" `
                -Status "FAIL" `
                -FailureCode $ExitCode.SERVICE_INVALID `
                -Message "Configured business service scope is empty, duplicate, or invalid." `
                -Evidence ([pscustomobject]@{
                    exception_type = $_.Exception.GetType().FullName
                    exception_message = $_.Exception.Message
                })
        }
    }

    if ($null -ne $config -and $null -ne $data) {
        if ($Stage -eq "PreInstall") {
            $aclTargets = @($data, $config)
            if ($database -and (Test-Path -LiteralPath $database -PathType Leaf)) {
                $aclTargets += $database
            }
            foreach ($target in $aclTargets) {
                try {
                    $assessment = Get-AclAssessment `
                        -LiteralPath $target `
                        -RequireProtected
                    $aclStatus = if ($assessment.valid) { "PASS" } else { "WARN" }
                    Add-PreflightCheck `
                        -Id "ACL_$([IO.Path]::GetFileName($target))" `
                        -Category "ACL" `
                        -Status $aclStatus `
                        -Message $(if ($assessment.valid) {
                            "ACL already matches the protected post-install boundary."
                        } else {
                            "ACL differs from the post-install boundary; the installer must replace it before service start."
                        }) `
                        -Evidence $assessment
                } catch {
                    Add-PreflightCheck `
                        -Id "ACL_$([IO.Path]::GetFileName($target))" `
                        -Category "ACL" `
                        -Status "FAIL" `
                        -FailureCode $ExitCode.ACL_INVALID `
                        -Message "ACL could not be read or evaluated." `
                        -Evidence ([pscustomobject]@{
                            path = [IO.Path]::GetFullPath($target)
                            exception_type = $_.Exception.GetType().FullName
                        })
                }
            }
        } else {
            try {
                $invalidAclItems = [System.Collections.Generic.List[object]]::new()
                $treeItems = @(Get-DataTreeItems -Root $data)
                foreach ($item in $treeItems) {
                    $isReparsePoint = (
                        $item.Attributes -band [IO.FileAttributes]::ReparsePoint
                    ) -ne 0
                    if ($isReparsePoint) {
                        $invalidAclItems.Add([pscustomobject]@{
                            path = $item.FullName
                            reason = "REPARSE_POINT_NOT_ALLOWED"
                        })
                        continue
                    }
                    $isDataRoot = $item.FullName.Equals(
                        $data,
                        [StringComparison]::OrdinalIgnoreCase
                    )
                    $assessment = if ($isDataRoot) {
                        Get-AclAssessment -LiteralPath $item.FullName -RequireProtected
                    } else {
                        Get-AclAssessment -LiteralPath $item.FullName -AllowSystemOwner
                    }
                    if (-not $assessment.valid) {
                        $invalidAclItems.Add($assessment)
                    }
                }
                $treeAclOk = $invalidAclItems.Count -eq 0
                Add-PreflightCheck `
                    -Id "ACL_DATA_TREE" `
                    -Category "ACL" `
                    -Status $(if ($treeAclOk) { "PASS" } else { "FAIL" }) `
                    -FailureCode $ExitCode.ACL_INVALID `
                    -Message $(if ($treeAclOk) {
                        "Entire data tree inherits or explicitly applies the fixed two-SID ACL boundary."
                    } else {
                        "Data tree contains an ACL violation or reparse point."
                    }) `
                    -Evidence ([pscustomobject]@{
                        target_count = $treeItems.Count
                        invalid_count = $invalidAclItems.Count
                        violations_truncated = $invalidAclItems.Count -gt 50
                        violations = @($invalidAclItems | Select-Object -First 50)
                        descendant_owner_policy = "Administrators or LocalSystem"
                        descendant_inheritance_policy = "secure inheritance or protected explicit DACL"
                    })
            } catch {
                Add-PreflightCheck `
                    -Id "ACL_DATA_TREE" `
                    -Category "ACL" `
                    -Status "FAIL" `
                    -FailureCode $ExitCode.ACL_INVALID `
                    -Message "Post-install data tree ACL could not be enumerated or evaluated." `
                    -Evidence ([pscustomobject]@{
                        path = $data
                        exception_type = $_.Exception.GetType().FullName
                    })
            }
        }
        if ($database -and -not (Test-Path -LiteralPath $database)) {
            Add-PreflightCheck `
                -Id $(if ($Stage -eq "PreInstall") {
                    "ACL_DATABASE_NOT_CREATED"
                } else {
                    "ACL_DATABASE_MISSING"
                }) `
                -Category "ACL" `
                -Status $(if ($Stage -eq "PreInstall") { "SKIP" } else { "FAIL" }) `
                -FailureCode $ExitCode.ACL_INVALID `
                -Message $(if ($Stage -eq "PreInstall") {
                    "SQLite file does not exist yet; its parent data directory ACL was evaluated."
                } else {
                    "SQLite file must exist after the role service has started."
                }) `
                -Evidence ([pscustomobject]@{ database_path = $database })
        }
    }
}

if ($MyInvocation.InvocationName -eq ".") {
    return
}

try {
    Invoke-RecoveryHostPreflight
} catch {
    Add-PreflightCheck `
        -Id "UNEXPECTED_FAILURE" `
        -Category "INTERNAL" `
        -Status "FAIL" `
        -FailureCode $ExitCode.UNEXPECTED_ERROR `
        -Message "Preflight could not complete all local read-only checks." `
        -Evidence ([pscustomobject]@{ exception_type = $_.Exception.GetType().FullName })
}

$failures = @($checks | Where-Object { $_.status -eq "FAIL" })
$warnings = @($checks | Where-Object { $_.status -eq "WARN" })
$overallExitCode = if ($failures.Count -eq 0) {
    $ExitCode.PASS
} else {
    # Checks are emitted in dependency order. The first failure is the primary
    # remediation gate; all additional failures remain visible in the report.
    [int]$failures[0].failure_code
}
$report = [pscustomobject][ordered]@{
    schema_version = 1
    generated_at = [DateTimeOffset]::UtcNow.ToString("o")
    host = [Environment]::MachineName
    role = if ([string]::IsNullOrWhiteSpace($Role)) { $null } else { $Role }
    stage = if ([string]::IsNullOrWhiteSpace($Stage)) { $null } else { $Stage }
    outcome = if ($overallExitCode -eq 0) { "PASS" } else { "FAIL" }
    exit_code = $overallExitCode
    side_effects = "NONE"
    scope = [pscustomobject][ordered]@{
        local_only = $true
        remote_hosts_scanned = 0
        requested_business_services = @($BusinessServiceName)
        resolved_business_services = @($script:resolvedBusinessServices)
    }
    checks = @($checks)
    failures = @($failures | ForEach-Object {
        [pscustomobject]@{
            id = $_.id
            failure_code = $_.failure_code
            message = $_.message
        }
    })
    warnings = @($warnings | ForEach-Object {
        [pscustomobject]@{
            id = $_.id
            message = $_.message
        }
    })
}

if ($PassThru) {
    Write-Output $report
} else {
    $report | ConvertTo-Json -Depth 12
}
exit $overallExitCode
