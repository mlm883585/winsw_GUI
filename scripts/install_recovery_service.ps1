[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Agent", "ControlPlane")]
    [string]$Role,

    [Parameter(Mandatory)]
    [string]$PackageDirectory,

    [Parameter(Mandatory)]
    [string]$ConfigPath,

    [Parameter(Mandatory)]
    [string]$DataDirectory,

    [string]$WinSWPath,
    [string]$WinSWDownloadUrl,
    [string]$WinSWLockPath,

    [ValidatePattern("^[0-9A-Fa-f]{64}$")]
    [string]$WinSWSha256
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string]$Purpose,
        [string[]]$Arguments = @()
    )

    # Never echo native stdout/stderr: validation or wrapper output can contain
    # local paths or configuration details. The stable purpose and exit code are
    # sufficient for the aggregate installation error.
    & $FilePath @Arguments 2>&1 | Out-Null
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Purpose failed (exit_code=$exitCode)."
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$LiteralPath)

    $sha256 = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::Open(
            $LiteralPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        return [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
        $sha256.Dispose()
    }
}

function Assert-WinSWIntegrity {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][string]$ExpectedSha256,
        [Nullable[long]]$ExpectedSizeBytes = $null,
        [string]$ExpectedAuthenticodeStatus,
        [Parameter(Mandatory)][string]$Purpose
    )

    $item = Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
    if ($item -isnot [IO.FileInfo] -or (Test-IsReparsePoint -Item $item)) {
        throw "$Purpose must remain a regular non-reparse file."
    }

    $actualHash = Get-Sha256Hex -LiteralPath $LiteralPath
    if (-not $actualHash.Equals($ExpectedSha256, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Purpose SHA-256 mismatch."
    }

    $expectedSizeBytes = $ExpectedSizeBytes
    if ($null -ne $expectedSizeBytes) {
        $actualSizeBytes = (Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop).Length
        if ($actualSizeBytes -ne $expectedSizeBytes) {
            throw "$Purpose size differs from the lock."
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($ExpectedAuthenticodeStatus)) {
        $actualAuthenticodeStatus = (
            Get-AuthenticodeSignature -LiteralPath $LiteralPath
        ).Status.ToString()
        if (-not $actualAuthenticodeStatus.Equals(
            $ExpectedAuthenticodeStatus,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "$Purpose Authenticode status differs from the lock."
        }
    }
}

function Test-IsReparsePoint {
    param([Parameter(Mandatory)][IO.FileSystemInfo]$Item)

    return (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Get-SafeTreeItems {
    param([Parameter(Mandatory)][string]$RootPath)

    $root = Get-Item -LiteralPath $RootPath -Force -ErrorAction Stop
    if ($root -isnot [IO.DirectoryInfo]) {
        throw "A managed tree root must be a directory."
    }
    if (Test-IsReparsePoint -Item $root) {
        throw "Managed trees must not contain reparse points."
    }

    $items = [System.Collections.Generic.List[IO.FileSystemInfo]]::new()
    $pending = [System.Collections.Generic.Queue[IO.DirectoryInfo]]::new()
    $items.Add($root)
    $pending.Enqueue($root)
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)) {
            if (Test-IsReparsePoint -Item $child) {
                throw "Managed trees must not contain reparse points."
            }
            if ($child -isnot [IO.FileInfo] -and $child -isnot [IO.DirectoryInfo]) {
                throw "Managed trees must contain only regular files and directories."
            }
            $items.Add($child)
            if ($child -is [IO.DirectoryInfo]) {
                $pending.Enqueue($child)
            }
        }
    }
    return @($items)
}

function Get-RecoveryServiceExact {
    param([Parameter(Mandatory)][string]$ServiceId)

    return Get-Service -Name $ServiceId -ErrorAction SilentlyContinue
}

function Get-RecoveryServiceImagePath {
    param([Parameter(Mandatory)][string]$ServiceId)

    $serviceKey = "Registry::HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\$ServiceId"
    $property = Get-ItemProperty -LiteralPath $serviceKey -Name ImagePath -ErrorAction Stop
    return [string]$property.ImagePath
}

function Get-ImagePathExecutable {
    param([Parameter(Mandatory)][string]$ImagePath)

    $expanded = [Environment]::ExpandEnvironmentVariables($ImagePath).Trim()
    $match = [regex]::Match($expanded, '^(?:"([^"]+)"|(.+?\.exe))(?:\s|$)', 'IgnoreCase')
    if (-not $match.Success) {
        return $null
    }
    if ($match.Groups[1].Success) {
        return $match.Groups[1].Value
    }
    return $match.Groups[2].Value
}

function Test-RecoveryServiceOwnedByRun {
    param(
        [Parameter(Mandatory)][string]$ServiceId,
        [Parameter(Mandatory)][string]$ExpectedWinSWPath
    )

    try {
        $imagePath = Get-RecoveryServiceImagePath -ServiceId $ServiceId
        $imageExecutable = Get-ImagePathExecutable -ImagePath $imagePath
        if (-not $imageExecutable) {
            return $false
        }
        $actual = [IO.Path]::GetFullPath($imageExecutable)
        $expected = [IO.Path]::GetFullPath($ExpectedWinSWPath)
        return $actual.Equals($expected, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Assert-RecoveryInstallTargetsAbsent {
    param(
        [Parameter(Mandatory)][string]$ServiceId,
        [Parameter(Mandatory)][string]$DataPath,
        [Parameter(Mandatory)][string]$InstalledPackage,
        [Parameter(Mandatory)][string]$ServiceDirectory
    )

    if ($null -ne (Get-RecoveryServiceExact -ServiceId $ServiceId)) {
        throw "$ServiceId already exists. The MVP installer is install-only and will not overwrite or upgrade it."
    }
    foreach ($managedPath in @($InstalledPackage, $ServiceDirectory)) {
        if (Test-Path -LiteralPath $managedPath) {
            throw "Managed install residue exists. Refusing to overwrite or delete it."
        }
    }
    $stagingPrefix = ".install-$ServiceId-"
    $stagingResidue = @(
        Get-ChildItem -LiteralPath $DataPath -Force -ErrorAction Stop |
            Where-Object { $_.Name.StartsWith($stagingPrefix, [StringComparison]::OrdinalIgnoreCase) }
    )
    if ($stagingResidue.Count -gt 0) {
        throw "Managed install staging residue exists. Refusing to overwrite or delete it."
    }
}

function Get-RecoveryAclSnapshots {
    param([Parameter(Mandatory)][string]$DataPath)

    $sections = [Security.AccessControl.AccessControlSections]::Owner -bor `
        [Security.AccessControl.AccessControlSections]::Access
    $snapshots = [System.Collections.Generic.Dictionary[string,object]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($item in @(Get-SafeTreeItems -RootPath $DataPath)) {
        $acl = Get-Acl -LiteralPath $item.FullName -ErrorAction Stop
        $snapshots.Add($item.FullName, [pscustomobject][ordered]@{
            path = $item.FullName
            is_directory = ($item -is [IO.DirectoryInfo])
            sddl = $acl.GetSecurityDescriptorSddlForm($sections)
        })
    }
    return ,$snapshots
}

function New-RecoveryInstallJournal {
    param(
        [Parameter(Mandatory)][string]$ServiceId,
        [Parameter(Mandatory)][string]$DataPath,
        [Parameter(Mandatory)][string]$InstalledPackage,
        [Parameter(Mandatory)][string]$ServiceDirectory,
        [Parameter(Mandatory)][string]$TransactionRoot,
        [Parameter(Mandatory)][object]$AclSnapshots
    )

    return [pscustomobject][ordered]@{
        schema_version = 1
        transaction_id = [Guid]::NewGuid().ToString("D")
        current_phase = "PREPARED"
        service_id = $ServiceId
        data_path = $DataPath
        installed_package = $InstalledPackage
        service_directory = $ServiceDirectory
        transaction_root = $TransactionRoot
        winsw_path = Join-Path $ServiceDirectory "$ServiceId.exe"
        xml_path = Join-Path $ServiceDirectory "$ServiceId.xml"
        download_path = $null
        download_owned_by_run = $false
        transaction_root_created = $false
        package_copy_started = $false
        service_files_staged = $false
        acl_apply_started = $false
        acl_snapshots = $AclSnapshots
        acl_restore_paths = [System.Collections.Generic.List[string]]::new()
        package_published = $false
        service_published = $false
        service_install_attempted = $false
        service_registered_by_run = $false
        service_start_attempted = $false
        rollback_stop_attempted = $false
        rollback_uninstall_attempted = $false
        completed = $false
    }
}

function Set-RecoveryDirectoryAcl {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][object]$Journal
    )

    $systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $administratorsSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    $targets = @(Get-SafeTreeItems -RootPath $Path)
    $transactionRoot = [IO.Path]::GetFullPath($Journal.transaction_root)
    $transactionPrefix = $transactionRoot.TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
    foreach ($target in $targets) {
        $isRunOwnedStaging = (
            $target.FullName.Equals($transactionRoot, [StringComparison]::OrdinalIgnoreCase) -or
            $target.FullName.StartsWith($transactionPrefix, [StringComparison]::OrdinalIgnoreCase)
        )
        if (-not $Journal.acl_snapshots.ContainsKey($target.FullName) -and
            -not $isRunOwnedStaging) {
            throw "DataDirectory changed after ACL snapshot; refusing to modify an unjournaled path."
        }
    }

    foreach ($target in $targets) {
        if ($Journal.acl_snapshots.ContainsKey($target.FullName) -and
            -not $Journal.acl_restore_paths.Contains($target.FullName)) {
            # Add before Set-Acl. Even if Set-Acl reports failure after touching
            # the object, rollback will restore its original owner and DACL.
            $Journal.acl_restore_paths.Add($target.FullName)
        }

        if ($target -is [IO.DirectoryInfo]) {
            $security = [Security.AccessControl.DirectorySecurity]::new()
            $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
                [Security.AccessControl.InheritanceFlags]::ObjectInherit
        } else {
            $security = [Security.AccessControl.FileSecurity]::new()
            $inheritance = [Security.AccessControl.InheritanceFlags]::None
        }
        $security.SetAccessRuleProtection($true, $false)
        $security.SetOwner($administratorsSid)
        foreach ($sid in @($systemSid, $administratorsSid)) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
            [void]$security.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $target.FullName -AclObject $security -ErrorAction Stop
    }
}

function Add-RollbackIssue {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Issues,
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$Detail
    )

    $Issues.Add([pscustomobject][ordered]@{ code = $Code; detail = $Detail })
}

function Restore-RecoveryAcl {
    param(
        [Parameter(Mandatory)][object]$Journal,
        [Parameter(Mandatory)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Issues
    )

    if (-not $Journal.acl_apply_started) {
        return
    }
    $sections = [Security.AccessControl.AccessControlSections]::Owner -bor `
        [Security.AccessControl.AccessControlSections]::Access
    $paths = @($Journal.acl_restore_paths | Sort-Object { $_.Length } -Descending)
    foreach ($path in $paths) {
        try {
            if (-not $Journal.acl_snapshots.ContainsKey($path) -or
                -not (Test-Path -LiteralPath $path)) {
                throw "Pre-existing ACL target is unavailable."
            }
            $snapshot = $Journal.acl_snapshots[$path]
            $security = if ($snapshot.is_directory) {
                [Security.AccessControl.DirectorySecurity]::new()
            } else {
                [Security.AccessControl.FileSecurity]::new()
            }
            $security.SetSecurityDescriptorSddlForm($snapshot.sddl, $sections)
            Set-Acl -LiteralPath $snapshot.path -AclObject $security -ErrorAction Stop
        } catch {
            Add-RollbackIssue -Issues $Issues -Code "RB_ACL_RESTORE_FAILED" `
                -Detail "Owner/DACL restoration failed for a pre-existing path."
        }
    }
}

function Remove-RunOwnedTree {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$DataPath,
        [Parameter(Mandatory)][string]$IssueCode,
        [Parameter(Mandatory)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Issues
    )

    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        $dataPrefix = [IO.Path]::GetFullPath($DataPath).TrimEnd([char[]]"\/") + `
            [IO.Path]::DirectorySeparatorChar
        if (-not $fullPath.StartsWith($dataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Run-owned cleanup path escaped DataDirectory."
        }
        if (-not (Test-Path -LiteralPath $fullPath)) {
            return
        }
        $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
        if ($item -isnot [IO.DirectoryInfo] -or (Test-IsReparsePoint -Item $item)) {
            throw "Run-owned cleanup target changed type or became a reparse point."
        }
        [void](Get-SafeTreeItems -RootPath $fullPath)
        Remove-Item -LiteralPath $fullPath -Recurse -Force -ErrorAction Stop
    } catch {
        Add-RollbackIssue -Issues $Issues -Code $IssueCode `
            -Detail "A run-owned managed directory could not be removed safely."
    }
}

function Remove-RunOwnedDownload {
    param(
        [Parameter(Mandatory)][object]$Journal,
        [Parameter(Mandatory)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Issues
    )

    if (-not $Journal.download_owned_by_run -or -not $Journal.download_path) {
        return
    }
    try {
        $fullPath = [IO.Path]::GetFullPath($Journal.download_path)
        $tempPath = [IO.Path]::GetFullPath($env:TEMP).TrimEnd([char[]]"\/") + `
            [IO.Path]::DirectorySeparatorChar
        if (-not $fullPath.StartsWith($tempPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Downloaded cleanup path escaped the local temporary directory."
        }
        if (Test-Path -LiteralPath $fullPath) {
            $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
            if ($item -isnot [IO.FileInfo] -or (Test-IsReparsePoint -Item $item)) {
                throw "Downloaded cleanup target changed type or became a reparse point."
            }
            Remove-Item -LiteralPath $fullPath -Force -ErrorAction Stop
        }
        $Journal.download_owned_by_run = $false
    } catch {
        Add-RollbackIssue -Issues $Issues -Code "RB_DOWNLOAD_DELETE_FAILED" `
            -Detail "The run-owned downloaded WinSW temporary file could not be removed safely."
    }
}

function Invoke-RecoveryInstallRollback {
    param([Parameter(Mandatory)][object]$Journal)

    $issues = [System.Collections.Generic.List[object]]::new()
    $serviceAbsent = $false
    $service = $null
    try {
        $service = Get-RecoveryServiceExact -ServiceId $Journal.service_id
    } catch {
        Add-RollbackIssue -Issues $issues -Code "RB_SERVICE_QUERY_FAILED" `
            -Detail "The Recovery service could not be queried during rollback."
    }

    if ($null -ne $service) {
        if (-not $Journal.service_install_attempted) {
            Add-RollbackIssue -Issues $issues -Code "RB_SERVICE_OWNERSHIP_UNPROVEN" `
                -Detail "A same-name service appeared before this run attempted registration."
        } elseif (-not (Test-RecoveryServiceOwnedByRun `
            -ServiceId $Journal.service_id `
            -ExpectedWinSWPath $Journal.winsw_path)) {
            Add-RollbackIssue -Issues $issues -Code "RB_SERVICE_OWNERSHIP_UNPROVEN" `
                -Detail "The same-name service ImagePath does not prove ownership by this run."
        } else {
            $Journal.service_registered_by_run = $true
            if ([string]$service.Status -ne "Stopped") {
                $Journal.rollback_stop_attempted = $true
                try {
                    Invoke-NativeChecked -FilePath $Journal.winsw_path `
                        -Purpose "WinSW rollback stop" -Arguments @("stop", $Journal.xml_path)
                } catch {
                    Add-RollbackIssue -Issues $issues -Code "RB_STOP_FAILED" `
                        -Detail "Best-effort stop failed; uninstall was still attempted."
                }
            }

            if (Test-RecoveryServiceOwnedByRun `
                -ServiceId $Journal.service_id `
                -ExpectedWinSWPath $Journal.winsw_path) {
                $Journal.rollback_uninstall_attempted = $true
                try {
                    Invoke-NativeChecked -FilePath $Journal.winsw_path `
                        -Purpose "WinSW rollback uninstall" -Arguments @("uninstall", $Journal.xml_path)
                } catch {
                    Add-RollbackIssue -Issues $issues -Code "RB_UNINSTALL_FAILED" `
                        -Detail "The run-owned Recovery service uninstall command failed."
                }
            } else {
                Add-RollbackIssue -Issues $issues -Code "RB_SERVICE_OWNERSHIP_LOST" `
                    -Detail "Service ImagePath ownership changed before rollback uninstall."
            }
        }
    }

    try {
        $remainingService = Get-RecoveryServiceExact -ServiceId $Journal.service_id
        $serviceAbsent = ($null -eq $remainingService)
        if (-not $serviceAbsent) {
            Add-RollbackIssue -Issues $issues -Code "RB_SERVICE_STILL_PRESENT" `
                -Detail "The Recovery service is still registered; wrapper dependencies were preserved."
        }
    } catch {
        Add-RollbackIssue -Issues $issues -Code "RB_SERVICE_RECHECK_FAILED" `
            -Detail "The Recovery service absence could not be confirmed after rollback."
    }

    if ($serviceAbsent) {
        if ($Journal.service_published) {
            Remove-RunOwnedTree -Path $Journal.service_directory -DataPath $Journal.data_path `
                -IssueCode "RB_SERVICE_FILES_DELETE_FAILED" -Issues $issues
        }
        if ($Journal.package_published) {
            Remove-RunOwnedTree -Path $Journal.installed_package -DataPath $Journal.data_path `
                -IssueCode "RB_PACKAGE_DELETE_FAILED" -Issues $issues
        }
    }
    if ($Journal.transaction_root_created) {
        Remove-RunOwnedTree -Path $Journal.transaction_root -DataPath $Journal.data_path `
            -IssueCode "RB_STAGING_DELETE_FAILED" -Issues $issues
    }

    Restore-RecoveryAcl -Journal $Journal -Issues $issues
    Remove-RunOwnedDownload -Journal $Journal -Issues $issues

    $managedPathsAbsent = $false
    try {
        $managedPathsAbsent = (
            -not (Test-Path -LiteralPath $Journal.installed_package) -and
            -not (Test-Path -LiteralPath $Journal.service_directory) -and
            -not (Test-Path -LiteralPath $Journal.transaction_root)
        )
    } catch {
        Add-RollbackIssue -Issues $issues -Code "RB_PATH_RECHECK_FAILED" `
            -Detail "Managed path absence could not be confirmed after rollback."
    }
    $unsafeIssuePrefixes = @(
        "RB_SERVICE_QUERY_", "RB_SERVICE_OWNERSHIP_", "RB_SERVICE_STILL_",
        "RB_SERVICE_RECHECK_", "RB_SERVICE_FILES_", "RB_PACKAGE_", "RB_STAGING_",
        "RB_ACL_", "RB_DOWNLOAD_", "RB_PATH_RECHECK_", "RB_INTERNAL_"
    )
    $hasUnsafeIssue = $false
    foreach ($issue in $issues) {
        foreach ($prefix in $unsafeIssuePrefixes) {
            if ($issue.code.StartsWith($prefix, [StringComparison]::Ordinal)) {
                $hasUnsafeIssue = $true
                break
            }
        }
        if ($hasUnsafeIssue) { break }
    }
    return [pscustomobject][ordered]@{
        issues = @($issues)
        service_absent = $serviceAbsent
        managed_paths_absent = $managedPathsAbsent
        retry_safe = ($serviceAbsent -and $managedPathsAbsent -and -not $hasUnsafeIssue)
    }
}

function ConvertTo-StableErrorText {
    param([Parameter(Mandatory)][string]$Message)

    $singleLine = [regex]::Replace($Message, "[\r\n\t]+", " ").Trim()
    if ($singleLine.Length -gt 512) {
        return $singleLine.Substring(0, 512)
    }
    return $singleLine
}

function New-RecoveryInstallAggregateError {
    param(
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Primary,
        [Parameter(Mandatory)][object]$Rollback
    )

    $codes = @($Rollback.issues | ForEach-Object { $_.code } | Select-Object -Unique)
    $issueText = if ($codes.Count -eq 0) { "NONE" } else { $codes -join "," }
    $retryText = ([string][bool]$Rollback.retry_safe).ToLowerInvariant()
    $action = if ($Rollback.retry_safe) {
        "Correct the primary failure and rerun the same install command."
    } else {
        "Inspect the exact Recovery service ImagePath and managed package/service/staging paths; reconcile only proven installer-owned residue before retrying."
    }
    return "INSTALL_FAILED phase=$Phase primary=$(ConvertTo-StableErrorText -Message $Primary) " +
        "rollback_issues=[$issueText] retry_safe=$retryText action=$action"
}

function Invoke-RecoveryInstallGuarded {
    param(
        [Parameter(Mandatory)][object]$Journal,
        [Parameter(Mandatory)][scriptblock]$Action
    )

    try {
        & $Action | Out-Null
    } catch {
        $primary = $_.Exception.Message
        $phase = $Journal.current_phase
        try {
            $rollback = Invoke-RecoveryInstallRollback -Journal $Journal
        } catch {
            $fallbackIssues = @([pscustomobject][ordered]@{
                code = "RB_INTERNAL_FAILURE"
                detail = "Rollback raised an unexpected internal failure."
            })
            $rollback = [pscustomobject][ordered]@{
                issues = $fallbackIssues
                service_absent = $false
                managed_paths_absent = $false
                retry_safe = $false
            }
        }
        $message = New-RecoveryInstallAggregateError `
            -Phase $phase -Primary $primary -Rollback $rollback
        throw [InvalidOperationException]::new($message)
    }
}

function Invoke-RecoveryInstallCommit {
    param(
        [Parameter(Mandatory)][object]$Journal,
        [Parameter(Mandatory)][string]$PackageSource,
        [Parameter(Mandatory)][string]$WinSWSource,
        [Parameter(Mandatory)][string]$ExecutableName,
        [Parameter(Mandatory)][string]$DisplayName,
        [Parameter(Mandatory)][string]$InstallRole,
        [Parameter(Mandatory)][string]$Config,
        [Parameter(Mandatory)][string]$Data,
        [Parameter(Mandatory)][string]$ExpectedWinSWSha256,
        [Nullable[long]]$ExpectedWinSWSizeBytes = $null,
        [string]$ExpectedWinSWAuthenticodeStatus
    )

    $stagedPackage = Join-Path $Journal.transaction_root "package"
    $stagedService = Join-Path $Journal.transaction_root "service"
    $stagedWinSW = Join-Path $stagedService "$($Journal.service_id).exe"
    $stagedXml = Join-Path $stagedService "$($Journal.service_id).xml"

    $Journal.current_phase = "STAGING"
    if (Test-Path -LiteralPath $Journal.transaction_root) {
        throw "Transaction staging path already exists."
    }
    New-Item -ItemType Directory -Path $Journal.transaction_root -ErrorAction Stop | Out-Null
    $Journal.transaction_root_created = $true
    $Journal.package_copy_started = $true
    Copy-Item -LiteralPath $PackageSource -Destination $stagedPackage -Recurse -ErrorAction Stop
    $stagedExecutable = Join-Path $stagedPackage $ExecutableName
    if (-not (Test-Path -LiteralPath $stagedExecutable -PathType Leaf)) {
        throw "Staged package copy did not produce the required executable."
    }

    New-Item -ItemType Directory -Path $stagedService -ErrorAction Stop | Out-Null
    Copy-Item -LiteralPath $WinSWSource -Destination $stagedWinSW -ErrorAction Stop
    $Journal.current_phase = "STAGED_WINSW_VERIFY"
    Assert-WinSWIntegrity -LiteralPath $stagedWinSW `
        -ExpectedSha256 $ExpectedWinSWSha256 `
        -ExpectedSizeBytes $ExpectedWinSWSizeBytes `
        -ExpectedAuthenticodeStatus $ExpectedWinSWAuthenticodeStatus `
        -Purpose "Staged WinSW wrapper"
    $Journal.current_phase = "STAGING"
    $escapedExecutable = [Security.SecurityElement]::Escape((Join-Path $Journal.installed_package $ExecutableName))
    $escapedConfig = [Security.SecurityElement]::Escape($Config)
    $escapedData = [Security.SecurityElement]::Escape($Data)
    $serviceXml = @"
<service>
  <id>$($Journal.service_id)</id>
  <name>$DisplayName</name>
  <description>Windows multi-node cold-start recovery MVP ($InstallRole)</description>
  <executable>$escapedExecutable</executable>
  <arguments>--config &quot;$escapedConfig&quot;</arguments>
  <workingdirectory>$escapedData</workingdirectory>
  <startmode>Automatic</startmode>
  <stoptimeout>30 sec</stoptimeout>
  <logpath>$escapedData\logs</logpath>
  <log mode="roll" />
</service>
"@
    [IO.File]::WriteAllText($stagedXml, $serviceXml, [Text.UTF8Encoding]::new($false))
    $Journal.service_files_staged = $true

    $Journal.current_phase = "ACL_APPLY"
    $Journal.acl_apply_started = $true
    Set-RecoveryDirectoryAcl -Path $Data -Journal $Journal

    $Journal.current_phase = "PUBLISH_PACKAGE"
    if (Test-Path -LiteralPath $Journal.installed_package) {
        throw "Managed package path appeared during installation."
    }
    [IO.Directory]::Move($stagedPackage, $Journal.installed_package)
    $Journal.package_published = $true

    $Journal.current_phase = "PUBLISH_SERVICE"
    if (Test-Path -LiteralPath $Journal.service_directory) {
        throw "Managed service path appeared during installation."
    }
    [IO.Directory]::Move($stagedService, $Journal.service_directory)
    $Journal.service_published = $true
    Remove-Item -LiteralPath $Journal.transaction_root -Force -ErrorAction Stop
    $Journal.transaction_root_created = $false
    $Journal.current_phase = "PUBLISHED_WINSW_VERIFY"
    Assert-WinSWIntegrity -LiteralPath $Journal.winsw_path `
        -ExpectedSha256 $ExpectedWinSWSha256 `
        -ExpectedSizeBytes $ExpectedWinSWSizeBytes `
        -ExpectedAuthenticodeStatus $ExpectedWinSWAuthenticodeStatus `
        -Purpose "Published WinSW wrapper"

    $Journal.current_phase = "SCM_INSTALL"
    $Journal.service_install_attempted = $true
    Invoke-NativeChecked -FilePath $Journal.winsw_path `
        -Purpose "WinSW install" -Arguments @("install", $Journal.xml_path)
    $registered = Get-RecoveryServiceExact -ServiceId $Journal.service_id
    if ($null -eq $registered) {
        throw "WinSW install returned success but the Recovery service is absent."
    }
    if (-not (Test-RecoveryServiceOwnedByRun `
        -ServiceId $Journal.service_id -ExpectedWinSWPath $Journal.winsw_path)) {
        throw "WinSW install returned success but service ImagePath ownership is unproven."
    }
    $Journal.service_registered_by_run = $true

    $Journal.current_phase = "SCM_START"
    $Journal.service_start_attempted = $true
    Invoke-NativeChecked -FilePath $Journal.winsw_path `
        -Purpose "WinSW start" -Arguments @("start", $Journal.xml_path)
}

function Invoke-RecoveryServiceInstaller {
    param(
        [Parameter(Mandatory)][string]$InstallRole,
        [Parameter(Mandatory)][string]$SourcePackageDirectory,
        [Parameter(Mandatory)][string]$SourceConfigPath,
        [Parameter(Mandatory)][string]$TargetDataDirectory,
        [string]$SourceWinSWPath,
        [string]$SourceWinSWDownloadUrl,
        [string]$SourceWinSWLockPath,
        [string]$SourceWinSWSha256
    )

    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell session."
    }

    $package = (Resolve-Path -LiteralPath $SourcePackageDirectory -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $package -PathType Container)) {
        throw "PackageDirectory must identify a directory."
    }
    $config = (Resolve-Path -LiteralPath $SourceConfigPath -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
        throw "ConfigPath must identify a JSON file."
    }
    $dataCandidate = [IO.Path]::GetFullPath($TargetDataDirectory)
    if (-not (Test-Path -LiteralPath $dataCandidate -PathType Container)) {
        throw "DataDirectory must already exist and contain ConfigPath."
    }
    $data = (Resolve-Path -LiteralPath $dataCandidate -ErrorAction Stop).Path
    $dataRoot = [IO.Path]::GetPathRoot($data)
    if ($data.TrimEnd([char[]]"\/").Equals(
        $dataRoot.TrimEnd([char[]]"\/"),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "DataDirectory must be a dedicated non-root directory."
    }
    [void](Get-SafeTreeItems -RootPath $data)
    [void](Get-SafeTreeItems -RootPath $package)

    $dataPrefix = $data.TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
    if (-not $config.StartsWith($dataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "ConfigPath must be located inside DataDirectory so its secrets receive the restricted ACL."
    }
    if ($package.StartsWith($dataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "PackageDirectory must be an external build source; it is copied into DataDirectory."
    }

    try {
        $configObject = Get-Content -LiteralPath $config -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        throw "ConfigPath must contain valid JSON before service installation."
    }
    $databaseProperty = $configObject.PSObject.Properties["database_path"]
    $databaseSetting = if ($databaseProperty -and $databaseProperty.Value) {
        [string]$databaseProperty.Value
    } elseif ($InstallRole -eq "Agent") {
        "data/agent.sqlite3"
    } else {
        "data/control-plane.sqlite3"
    }
    $databasePath = if ([IO.Path]::IsPathRooted($databaseSetting)) {
        [IO.Path]::GetFullPath($databaseSetting)
    } else {
        [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $config) $databaseSetting))
    }
    if (-not $databasePath.StartsWith($dataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "database_path must resolve inside DataDirectory so SQLite is protected by the service ACL."
    }

    if ($InstallRole -eq "Agent") {
        $serviceId = "winsw-recovery-agent"
        $displayName = "WinSW Recovery Agent"
        $executableName = "winsw-recovery-agent.exe"
    } else {
        $serviceId = "winsw-recovery-control-plane"
        $displayName = "WinSW Recovery Control Plane"
        $executableName = "winsw-recovery-control-plane.exe"
    }

    $installedPackage = Join-Path $data "package"
    $serviceDirectory = Join-Path $data "service"
    $stagingPrefix = ".install-$serviceId-"
    Assert-RecoveryInstallTargetsAbsent -ServiceId $serviceId -DataPath $data `
        -InstalledPackage $installedPackage -ServiceDirectory $serviceDirectory

    $sourceExecutable = Join-Path $package $executableName
    if (-not (Test-Path -LiteralPath $sourceExecutable -PathType Leaf)) {
        throw "Package executable is missing for the selected role."
    }
    Invoke-NativeChecked -FilePath $sourceExecutable `
        -Purpose "$InstallRole authoritative configuration validation" `
        -Arguments @("--config", $config, "--check-config")

    $effectiveDownloadUrl = $SourceWinSWDownloadUrl
    $effectiveSha256 = $SourceWinSWSha256
    $lockedVersion = $null
    $expectedSizeBytes = $null
    $expectedAuthenticodeStatus = $null
    if ($SourceWinSWLockPath) {
        if ($SourceWinSWDownloadUrl -or $SourceWinSWSha256) {
            throw "WinSWLockPath cannot be combined with WinSWDownloadUrl or WinSWSha256."
        }
        $lock = (Resolve-Path -LiteralPath $SourceWinSWLockPath -ErrorAction Stop).Path
        try {
            $lockObject = Get-Content -LiteralPath $lock -Raw -ErrorAction Stop | ConvertFrom-Json
        } catch {
            throw "WinSWLockPath must contain valid JSON."
        }
        $requiredLockProperties = @(
            "schema_version", "component", "version", "architecture", "asset_name",
            "download_url", "sha256", "size_bytes", "authenticode_status"
        )
        foreach ($property in $requiredLockProperties) {
            if (-not $lockObject.PSObject.Properties[$property]) {
                throw "WinSW lock is missing required property: $property"
            }
        }
        if ($lockObject.schema_version -ne 1) { throw "Unsupported WinSW lock schema_version." }
        if ($lockObject.component -ne "WinSW") { throw "WinSW lock component must be WinSW." }
        if ([string]$lockObject.version -notmatch "^\d+\.\d+\.\d+$") {
            throw "WinSW lock version must be a three-part semantic version."
        }
        if ($lockObject.architecture -ne "x64") { throw "WinSW lock architecture must be x64." }
        if ($lockObject.asset_name -ne "WinSW-x64.exe") {
            throw "WinSW lock asset_name must be WinSW-x64.exe."
        }
        if ([string]$lockObject.sha256 -notmatch "^[0-9A-Fa-f]{64}$") {
            throw "WinSW lock sha256 must contain 64 hexadecimal characters."
        }
        if ([long]$lockObject.size_bytes -le 0) { throw "WinSW lock size_bytes must be positive." }
        $effectiveDownloadUrl = [string]$lockObject.download_url
        $effectiveSha256 = [string]$lockObject.sha256
        $lockedVersion = [string]$lockObject.version
        $expectedSizeBytes = [long]$lockObject.size_bytes
        $expectedAuthenticodeStatus = [string]$lockObject.authenticode_status
    } elseif ($SourceWinSWPath -and $SourceWinSWDownloadUrl) {
        throw "Provide either WinSWPath or WinSWDownloadUrl, not both."
    }
    if (-not $effectiveSha256) {
        throw "Provide WinSWSha256 or WinSWLockPath."
    }

    $aclSnapshots = Get-RecoveryAclSnapshots -DataPath $data
    $transactionRoot = Join-Path $data (
        "$stagingPrefix{0}" -f ([Guid]::NewGuid().ToString("N"))
    )
    $journal = New-RecoveryInstallJournal -ServiceId $serviceId -DataPath $data `
        -InstalledPackage $installedPackage -ServiceDirectory $serviceDirectory `
        -TransactionRoot $transactionRoot -AclSnapshots $aclSnapshots

    Invoke-RecoveryInstallGuarded -Journal $journal -Action {
        $journal.current_phase = "WINSW_ACQUIRE"
        if ($SourceWinSWPath) {
            $winswSource = (Resolve-Path -LiteralPath $SourceWinSWPath -ErrorAction Stop).Path
            $winswItem = Get-Item -LiteralPath $winswSource -Force -ErrorAction Stop
            if ($winswItem -isnot [IO.FileInfo] -or (Test-IsReparsePoint -Item $winswItem)) {
                throw "WinSWPath must identify a regular non-reparse file."
            }
        } elseif ($effectiveDownloadUrl) {
            $uri = [Uri]$effectiveDownloadUrl
            if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne "https" -or
                $uri.UserInfo -or $uri.AbsoluteUri -match "/latest/") {
                throw "WinSWDownloadUrl must be an explicit HTTPS version URL without /latest/."
            }
            $winswSource = Join-Path $env:TEMP (
                "winsw-recovery-install-{0}.exe" -f ([Guid]::NewGuid().ToString("N"))
            )
            if (Test-Path -LiteralPath $winswSource) {
                throw "Unique WinSW temporary path unexpectedly exists."
            }
            $tempStream = [IO.File]::Open(
                $winswSource,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            $journal.download_path = $winswSource
            $journal.download_owned_by_run = $true
            $tempStream.Dispose()
            try {
                Invoke-WebRequest -UseBasicParsing -Uri $uri.AbsoluteUri -OutFile $winswSource
            } catch {
                throw "Fixed-version WinSW download failed."
            }
        } else {
            throw "Provide WinSWPath or an explicit fixed-version WinSWDownloadUrl."
        }

        $journal.current_phase = "WINSW_VERIFY"
        Assert-WinSWIntegrity -LiteralPath $winswSource `
            -ExpectedSha256 $effectiveSha256 `
            -ExpectedSizeBytes $expectedSizeBytes `
            -ExpectedAuthenticodeStatus $expectedAuthenticodeStatus `
            -Purpose "WinSW"

        Invoke-RecoveryInstallCommit -Journal $journal -PackageSource $package `
            -WinSWSource $winswSource -ExecutableName $executableName `
            -DisplayName $displayName -InstallRole $InstallRole `
            -Config $config -Data $data `
            -ExpectedWinSWSha256 $effectiveSha256 `
            -ExpectedWinSWSizeBytes $expectedSizeBytes `
            -ExpectedWinSWAuthenticodeStatus $expectedAuthenticodeStatus

        $journal.current_phase = "TEMP_CLEANUP"
        $tempIssues = [System.Collections.Generic.List[object]]::new()
        Remove-RunOwnedDownload -Journal $journal -Issues $tempIssues
        if ($tempIssues.Count -gt 0) {
            throw "Downloaded WinSW temporary cleanup failed."
        }
        $journal.current_phase = "COMPLETE"
        $journal.completed = $true
    }

    $versionMessage = if ($lockedVersion) { " WinSW $lockedVersion lock was enforced." } else { "" }
    Write-Host "$displayName installed as Automatic under LocalSystem.$versionMessage No business service was changed."
}

if ($MyInvocation.InvocationName -ne ".") {
    Invoke-RecoveryServiceInstaller `
        -InstallRole $Role `
        -SourcePackageDirectory $PackageDirectory `
        -SourceConfigPath $ConfigPath `
        -TargetDataDirectory $DataDirectory `
        -SourceWinSWPath $WinSWPath `
        -SourceWinSWDownloadUrl $WinSWDownloadUrl `
        -SourceWinSWLockPath $WinSWLockPath `
        -SourceWinSWSha256 $WinSWSha256
}
