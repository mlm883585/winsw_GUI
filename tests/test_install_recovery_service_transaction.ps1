[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$RepositoryRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installer = Join-Path $RepositoryRoot "scripts\install_recovery_service.ps1"
. $installer -Role Agent -PackageDirectory unused -ConfigPath unused -DataDirectory unused

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw "ASSERTION_FAILED: $Message"
    }
}

function Assert-TextContains {
    param([string]$Text, [string]$Expected, [string]$Message)
    if ($Text.IndexOf($Expected, [StringComparison]::Ordinal) -lt 0) {
        throw "ASSERTION_FAILED: $Message expected='$Expected' actual='$Text'"
    }
}

$script:Scenario = "success"
$script:ServicePresent = $false
$script:ServiceOwned = $false
$script:ServiceStatus = "Stopped"
$script:NativeCalls = [System.Collections.Generic.List[string]]::new()
$script:AclApplied = $false
$script:AclRestored = $false

# These functions are the OS boundary. The transaction, hashing, size checks,
# and filesystem ownership logic remain real; no SCM, registry, ACL,
# Authenticode-provider, or business-service call escapes this test process.
function Get-RecoveryServiceExact {
    param([string]$ServiceId)
    if ($script:ServicePresent) {
        return [pscustomobject]@{ Name = $ServiceId; Status = $script:ServiceStatus }
    }
    return $null
}

function Test-RecoveryServiceOwnedByRun {
    param([string]$ServiceId, [string]$ExpectedWinSWPath)
    return ($script:ServicePresent -and $script:ServiceOwned)
}

function Get-AuthenticodeSignature {
    param([string]$LiteralPath)
    return [pscustomobject]@{ Status = "NotSigned" }
}

function Set-RecoveryDirectoryAcl {
    param([string]$Path, [object]$Journal)
    $script:AclApplied = $true
    if ($script:Scenario -eq "published_wrapper_tamper") {
        $stagedWrapper = Join-Path (
            Join-Path $Journal.transaction_root "service"
        ) "$($Journal.service_id).exe"
        [IO.File]::WriteAllText($stagedWrapper, "tampered-after-staged-verification")
    }
}

function Restore-RecoveryAcl {
    param(
        [object]$Journal,
        [AllowEmptyCollection()][System.Collections.Generic.List[object]]$Issues
    )
    if ($Journal.acl_apply_started) {
        $script:AclRestored = $true
    }
}

function Invoke-NativeChecked {
    param(
        [string]$FilePath,
        [string]$Purpose,
        [string[]]$Arguments = @()
    )

    $script:NativeCalls.Add($Purpose)
    switch ($Purpose) {
        "WinSW install" {
            $script:ServicePresent = $true
            $script:ServiceOwned = ($script:Scenario -ne "install_fail_unowned_service")
            $script:ServiceStatus = "Stopped"
            if ($script:Scenario -in @("install_fail", "install_fail_unowned_service")) {
                throw "simulated install failure"
            }
        }
        "WinSW start" {
            if ($script:Scenario.StartsWith("start_fail", [StringComparison]::Ordinal)) {
                $script:ServiceStatus = "Running"
                throw "simulated start failure"
            }
            $script:ServiceStatus = "Running"
        }
        "WinSW rollback stop" {
            if ($script:Scenario -eq "start_fail_stop_rollback_fail") {
                throw "simulated rollback stop failure"
            }
            $script:ServiceStatus = "Stopped"
        }
        "WinSW rollback uninstall" {
            if ($script:Scenario -eq "start_fail_uninstall_still_present") {
                throw "simulated rollback uninstall failure"
            }
            $script:ServicePresent = $false
            $script:ServiceOwned = $false
            $script:ServiceStatus = "Stopped"
        }
        default {
            throw "Unexpected native purpose in transaction test: $Purpose"
        }
    }
}

function Copy-Item {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][string]$Destination,
        [switch]$Recurse
    )

    Microsoft.PowerShell.Management\Copy-Item @PSBoundParameters
    if ($script:Scenario -eq "staged_wrapper_tamper" -and
        [IO.Path]::GetFileName($LiteralPath) -eq "WinSW-x64.exe") {
        [IO.File]::WriteAllText($Destination, "tampered-immediately-after-copy")
    }
}

function New-TestFixture {
    $root = Join-Path $env:TEMP (
        "winsw-recovery-installer-test-{0}" -f ([Guid]::NewGuid().ToString("N"))
    )
    $data = Join-Path $root "data"
    $source = Join-Path $root "source"
    $dbDirectory = Join-Path $data "data"
    $logDirectory = Join-Path $data "logs"
    [void](New-Item -ItemType Directory -Path $data, $source, $dbDirectory, $logDirectory)
    [IO.File]::WriteAllText((Join-Path $data "agent.json"), '{"token":"preserve-me"}')
    [IO.File]::WriteAllText((Join-Path $data "notes.txt"), "operator-owned")
    [IO.File]::WriteAllText((Join-Path $dbDirectory "agent.sqlite3"), "sqlite-existing")
    [IO.File]::WriteAllText((Join-Path $logDirectory "agent.log"), "log-existing")
    [IO.File]::WriteAllText((Join-Path $source "winsw-recovery-agent.exe"), "agent-package")
    $winswSource = Join-Path $root "WinSW-x64.exe"
    [IO.File]::WriteAllText($winswSource, "winsw-wrapper")
    return [pscustomobject][ordered]@{
        root = $root
        data = $data
        source = $source
        winsw_source = $winswSource
        config = Join-Path $data "agent.json"
        database = Join-Path $dbDirectory "agent.sqlite3"
        log = Join-Path $logDirectory "agent.log"
        notes = Join-Path $data "notes.txt"
        installed_package = Join-Path $data "package"
        service_directory = Join-Path $data "service"
    }
}

function Get-PreservedContent {
    param([object]$Fixture)
    return @{
        config = [IO.File]::ReadAllText($Fixture.config)
        database = [IO.File]::ReadAllText($Fixture.database)
        log = [IO.File]::ReadAllText($Fixture.log)
        notes = [IO.File]::ReadAllText($Fixture.notes)
    }
}

function Assert-PreservedContent {
    param([object]$Fixture, [hashtable]$Before)
    Assert-True ([IO.File]::ReadAllText($Fixture.config) -eq $Before.config) "config changed or was deleted"
    Assert-True ([IO.File]::ReadAllText($Fixture.database) -eq $Before.database) "SQLite changed or was deleted"
    Assert-True ([IO.File]::ReadAllText($Fixture.log) -eq $Before.log) "log changed or was deleted"
    Assert-True ([IO.File]::ReadAllText($Fixture.notes) -eq $Before.notes) "unmanaged data changed or was deleted"
}

function New-TestJournal {
    param([object]$Fixture)
    $snapshots = [System.Collections.Generic.Dictionary[string,object]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $transactionRoot = Join-Path $Fixture.data (
        ".install-winsw-recovery-agent-{0}" -f ([Guid]::NewGuid().ToString("N"))
    )
    return New-RecoveryInstallJournal -ServiceId "winsw-recovery-agent" `
        -DataPath $Fixture.data -InstalledPackage $Fixture.installed_package `
        -ServiceDirectory $Fixture.service_directory -TransactionRoot $transactionRoot `
        -AclSnapshots $snapshots
}

function Invoke-TestInstall {
    param([object]$Fixture)
    $journal = New-TestJournal -Fixture $Fixture
    $expectedHash = Get-Sha256Hex -LiteralPath $Fixture.winsw_source
    $expectedSize = (Get-Item -LiteralPath $Fixture.winsw_source -Force).Length
    $expectedAuthenticode = (
        Get-AuthenticodeSignature -LiteralPath $Fixture.winsw_source
    ).Status.ToString()
    Invoke-RecoveryInstallGuarded -Journal $journal -Action {
        Invoke-RecoveryInstallCommit -Journal $journal `
            -PackageSource $Fixture.source -WinSWSource $Fixture.winsw_source `
            -ExecutableName "winsw-recovery-agent.exe" `
            -DisplayName "WinSW Recovery Agent" -InstallRole "Agent" `
            -Config $Fixture.config -Data $Fixture.data `
            -ExpectedWinSWSha256 $expectedHash `
            -ExpectedWinSWSizeBytes $expectedSize `
            -ExpectedWinSWAuthenticodeStatus $expectedAuthenticode
    }
    return $journal
}

function Invoke-ExpectedFailure {
    param([object]$Fixture)
    try {
        [void](Invoke-TestInstall -Fixture $Fixture)
    } catch {
        return $_.Exception.Message
    }
    throw "ASSERTION_FAILED: expected installation failure"
}

function Reset-TestState {
    param([string]$Scenario)
    $script:Scenario = $Scenario
    $script:ServicePresent = $false
    $script:ServiceOwned = $false
    $script:ServiceStatus = "Stopped"
    $script:NativeCalls.Clear()
    $script:AclApplied = $false
    $script:AclRestored = $false
}

function Assert-RollbackCleanAndRetry {
    param(
        [object]$Fixture,
        [hashtable]$Before,
        [bool]$ExpectAclRestore = $true
    )
    Assert-True (-not $script:ServicePresent) "Recovery service remained after complete rollback"
    Assert-True (-not (Test-Path -LiteralPath $Fixture.installed_package)) "run-owned package remained"
    Assert-True (-not (Test-Path -LiteralPath $Fixture.service_directory)) "run-owned service files remained"
    Assert-PreservedContent -Fixture $Fixture -Before $Before
    Assert-True ($script:AclRestored -eq $ExpectAclRestore) `
        "ACL rollback occurrence did not match the reached installation phase"

    Assert-RecoveryInstallTargetsAbsent -ServiceId "winsw-recovery-agent" `
        -DataPath $Fixture.data -InstalledPackage $Fixture.installed_package `
        -ServiceDirectory $Fixture.service_directory
    Reset-TestState -Scenario "success"
    [void](Invoke-TestInstall -Fixture $Fixture)
    Assert-True $script:ServicePresent "second installation attempt did not register the service"
    Assert-True (Test-Path -LiteralPath $Fixture.installed_package -PathType Container) `
        "second installation attempt did not publish package"
    Assert-True (Test-Path -LiteralPath $Fixture.service_directory -PathType Container) `
        "second installation attempt did not publish service files"
    Assert-PreservedContent -Fixture $Fixture -Before $Before
}

$results = [System.Collections.Generic.List[string]]::new()

# A source that changes while it is copied must fail staged verification. No
# ACL or SCM side effect has happened, and the run-owned staging tree is removed.
$fixture = New-TestFixture
try {
    $before = Get-PreservedContent -Fixture $fixture
    Reset-TestState -Scenario "staged_wrapper_tamper"
    $message = Invoke-ExpectedFailure -Fixture $fixture
    Assert-TextContains $message "phase=STAGED_WINSW_VERIFY" `
        "staged wrapper integrity failure phase was lost"
    Assert-TextContains $message "Staged WinSW wrapper SHA-256 mismatch" `
        "staged wrapper tamper was not rejected"
    Assert-TextContains $message "retry_safe=true" `
        "clean staged verification rollback was not retry-safe"
    Assert-True ($script:NativeCalls.Count -eq 0) `
        "staged wrapper tamper reached a native SCM command"
    Assert-RollbackCleanAndRetry -Fixture $fixture -Before $before `
        -ExpectAclRestore $false
    $results.Add("staged_wrapper_tamper_rejected")
} finally {
    Remove-Item -LiteralPath $fixture.root -Recurse -Force -ErrorAction SilentlyContinue
}

# A wrapper changed after staged verification but before final publication is
# caught by the final-path verification before WinSW can register the service.
$fixture = New-TestFixture
try {
    $before = Get-PreservedContent -Fixture $fixture
    Reset-TestState -Scenario "published_wrapper_tamper"
    $message = Invoke-ExpectedFailure -Fixture $fixture
    Assert-TextContains $message "phase=PUBLISHED_WINSW_VERIFY" `
        "published wrapper integrity failure phase was lost"
    Assert-TextContains $message "Published WinSW wrapper SHA-256 mismatch" `
        "published wrapper tamper was not rejected"
    Assert-TextContains $message "retry_safe=true" `
        "clean published verification rollback was not retry-safe"
    Assert-True ($script:NativeCalls.Count -eq 0) `
        "published wrapper tamper reached a native SCM command"
    Assert-RollbackCleanAndRetry -Fixture $fixture -Before $before
    $results.Add("published_wrapper_tamper_rejected")
} finally {
    Remove-Item -LiteralPath $fixture.root -Recurse -Force -ErrorAction SilentlyContinue
}

# Install command fails after creating the service: ownership is proven,
# uninstall runs, only this transaction's paths disappear, and retry succeeds.
$fixture = New-TestFixture
try {
    $before = Get-PreservedContent -Fixture $fixture
    Reset-TestState -Scenario "install_fail"
    $message = Invoke-ExpectedFailure -Fixture $fixture
    Assert-TextContains $message "phase=SCM_INSTALL" "install failure phase was lost"
    Assert-TextContains $message "primary=simulated install failure" "primary install failure was lost"
    Assert-TextContains $message "retry_safe=true" "complete install rollback was not retry-safe"
    Assert-RollbackCleanAndRetry -Fixture $fixture -Before $before
    $results.Add("install_failure_retry")
} finally {
    Remove-Item -LiteralPath $fixture.root -Recurse -Force -ErrorAction SilentlyContinue
}

# Start failure must stop before uninstall and then be directly retryable.
$fixture = New-TestFixture
try {
    $before = Get-PreservedContent -Fixture $fixture
    Reset-TestState -Scenario "start_fail"
    $message = Invoke-ExpectedFailure -Fixture $fixture
    Assert-TextContains $message "phase=SCM_START" "start failure phase was lost"
    Assert-TextContains $message "primary=simulated start failure" "primary start failure was lost"
    $stopIndex = $script:NativeCalls.IndexOf("WinSW rollback stop")
    $uninstallIndex = $script:NativeCalls.IndexOf("WinSW rollback uninstall")
    Assert-True ($stopIndex -ge 0 -and $uninstallIndex -gt $stopIndex) `
        "rollback did not stop before uninstall"
    Assert-RollbackCleanAndRetry -Fixture $fixture -Before $before
    $results.Add("start_failure_retry")
} finally {
    Remove-Item -LiteralPath $fixture.root -Recurse -Force -ErrorAction SilentlyContinue
}

# A stop rollback command can fail without suppressing uninstall. The aggregate
# retains both failures; confirmed service absence still permits a safe retry.
$fixture = New-TestFixture
try {
    $before = Get-PreservedContent -Fixture $fixture
    Reset-TestState -Scenario "start_fail_stop_rollback_fail"
    $message = Invoke-ExpectedFailure -Fixture $fixture
    Assert-TextContains $message "primary=simulated start failure" "primary failure was replaced"
    Assert-TextContains $message "RB_STOP_FAILED" "rollback stop issue was not aggregated"
    Assert-TextContains $message "retry_safe=true" "successful uninstall did not permit retry"
    Assert-RollbackCleanAndRetry -Fixture $fixture -Before $before
    $results.Add("rollback_command_failure_aggregated")
} finally {
    Remove-Item -LiteralPath $fixture.root -Recurse -Force -ErrorAction SilentlyContinue
}

# If uninstall fails and the service remains, wrapper dependencies must remain
# too. The residue gate blocks an unsafe retry until an operator reconciles the
# exact owned service and paths.
$fixture = New-TestFixture
try {
    $before = Get-PreservedContent -Fixture $fixture
    Reset-TestState -Scenario "start_fail_uninstall_still_present"
    $message = Invoke-ExpectedFailure -Fixture $fixture
    Assert-TextContains $message "RB_UNINSTALL_FAILED" "uninstall rollback issue was not aggregated"
    Assert-TextContains $message "RB_SERVICE_STILL_PRESENT" "remaining service was not reported"
    Assert-TextContains $message "retry_safe=false" "unsafe retry was incorrectly allowed"
    Assert-True $script:ServicePresent "simulated remaining service unexpectedly disappeared"
    Assert-True (Test-Path -LiteralPath $fixture.installed_package) `
        "package was deleted while its service remained"
    Assert-True (Test-Path -LiteralPath $fixture.service_directory) `
        "wrapper files were deleted while their service remained"
    Assert-PreservedContent -Fixture $fixture -Before $before
    try {
        Assert-RecoveryInstallTargetsAbsent -ServiceId "winsw-recovery-agent" `
            -DataPath $fixture.data -InstalledPackage $fixture.installed_package `
            -ServiceDirectory $fixture.service_directory
        throw "ASSERTION_FAILED: residue gate allowed service overwrite"
    } catch {
        Assert-TextContains $_.Exception.Message "install-only" "service residue gate returned wrong reason"
    }
    $results.Add("uninstall_failure_preserves_dependencies")
} finally {
    Remove-Item -LiteralPath $fixture.root -Recurse -Force -ErrorAction SilentlyContinue
}

# A same-name service that cannot be tied to this run's wrapper is never stopped,
# uninstalled, or deprived of files even though the install command failed.
$fixture = New-TestFixture
try {
    $before = Get-PreservedContent -Fixture $fixture
    Reset-TestState -Scenario "install_fail_unowned_service"
    $message = Invoke-ExpectedFailure -Fixture $fixture
    Assert-TextContains $message "RB_SERVICE_OWNERSHIP_UNPROVEN" `
        "unproven service ownership was not reported"
    Assert-TextContains $message "retry_safe=false" "unowned service incorrectly allowed retry"
    Assert-True ($script:NativeCalls.IndexOf("WinSW rollback stop") -lt 0) `
        "unowned same-name service was stopped"
    Assert-True ($script:NativeCalls.IndexOf("WinSW rollback uninstall") -lt 0) `
        "unowned same-name service was uninstalled"
    Assert-True (Test-Path -LiteralPath $fixture.installed_package) `
        "package was deleted while unowned same-name service remained"
    Assert-True (Test-Path -LiteralPath $fixture.service_directory) `
        "wrapper files were deleted while unowned same-name service remained"
    Assert-PreservedContent -Fixture $fixture -Before $before
    $results.Add("unowned_service_never_removed")
} finally {
    Remove-Item -LiteralPath $fixture.root -Recurse -Force -ErrorAction SilentlyContinue
}

# Pre-existing final and staging paths are never overwritten or auto-deleted.
# Removing each here represents an explicit operator decision; only then does
# the gate pass.
$fixture = New-TestFixture
try {
    $before = Get-PreservedContent -Fixture $fixture
    Reset-TestState -Scenario "success"
    $residuePaths = @(
        $fixture.installed_package,
        $fixture.service_directory,
        (Join-Path $fixture.data ".install-winsw-recovery-agent-preexisting")
    )
    foreach ($residuePath in $residuePaths) {
        $residue = Join-Path $residuePath "operator.txt"
        [void](New-Item -ItemType Directory -Path $residuePath)
        [IO.File]::WriteAllText($residue, "do-not-delete")
        try {
            Assert-RecoveryInstallTargetsAbsent -ServiceId "winsw-recovery-agent" `
                -DataPath $fixture.data -InstalledPackage $fixture.installed_package `
                -ServiceDirectory $fixture.service_directory
            throw "ASSERTION_FAILED: managed residue gate did not fail"
        } catch {
            Assert-TextContains $_.Exception.Message "Refusing to overwrite or delete" `
                "managed residue gate returned wrong reason"
        }
        Assert-True ([IO.File]::ReadAllText($residue) -eq "do-not-delete") `
            "residue gate deleted or changed an existing managed file"
        Assert-True ($script:NativeCalls.Count -eq 0) "residue gate invoked a native service command"
        Assert-PreservedContent -Fixture $fixture -Before $before
        Remove-Item -LiteralPath $residuePath -Recurse -Force
    }
    Assert-RecoveryInstallTargetsAbsent -ServiceId "winsw-recovery-agent" `
        -DataPath $fixture.data -InstalledPackage $fixture.installed_package `
        -ServiceDirectory $fixture.service_directory
    $results.Add("managed_residue_fail_closed")
} finally {
    Remove-Item -LiteralPath $fixture.root -Recurse -Force -ErrorAction SilentlyContinue
}

[pscustomobject][ordered]@{
    outcome = "PASS"
    side_effects = "TEST_TEMP_ONLY"
    scenarios = @($results)
} | ConvertTo-Json -Depth 4
