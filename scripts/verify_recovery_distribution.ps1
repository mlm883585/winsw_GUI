[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$DistributionDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ManifestName = "SHA256SUMS.txt"
$ExitCode = [ordered]@{
    PASS = 0
    INPUT_INVALID = 2
    INTEGRITY_FAILED = 3
}

function Test-IsReparsePoint {
    param([Parameter(Mandatory)][IO.FileSystemInfo]$Item)

    return (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$LiteralPath)

    # Use the .NET implementation directly. Get-FileHash is module-backed and
    # can fail to autoload in Windows PowerShell 5.1 when PSModulePath also
    # contains newer PowerShell module directories.
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

function Assert-SafeDistributionRoot {
    param([Parameter(Mandatory)][IO.DirectoryInfo]$RootItem)

    $rootPath = [IO.Path]::GetFullPath($RootItem.FullName)
    $volumeRoot = [IO.Path]::GetPathRoot($rootPath)
    if ($rootPath.TrimEnd([char[]]"\/").Equals(
        $volumeRoot.TrimEnd([char[]]"\/"),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "DistributionDirectory must be a dedicated non-root directory."
    }

    # Reject a reparse point at the requested root or in any ancestor. Otherwise
    # a seemingly local verification target could resolve outside the selected tree.
    $current = $RootItem
    while ($null -ne $current) {
        if (Test-IsReparsePoint -Item $current) {
            throw "DistributionDirectory and its ancestors must not be reparse points."
        }
        $current = $current.Parent
    }
}

function Get-DistributionFiles {
    param([Parameter(Mandatory)][IO.DirectoryInfo]$RootItem)

    $files = [System.Collections.Generic.List[IO.FileInfo]]::new()
    $pending = [System.Collections.Generic.Queue[IO.DirectoryInfo]]::new()
    $pending.Enqueue($RootItem)

    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)) {
            if (Test-IsReparsePoint -Item $child) {
                throw "Distribution tree contains a reparse point: $($child.FullName)"
            }
            if ($child -is [IO.DirectoryInfo]) {
                $pending.Enqueue($child)
            } elseif ($child -is [IO.FileInfo]) {
                $files.Add($child)
            } else {
                throw "Distribution tree contains an unsupported filesystem item: $($child.FullName)"
            }
        }
    }
    return @($files)
}

function Resolve-SafeManifestEntry {
    param(
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][string]$RootPath,
        [Parameter(Mandatory)][string]$RootPrefix
    )

    if ([string]::IsNullOrWhiteSpace($RelativePath)) {
        throw "Manifest path must not be empty."
    }
    if ($RelativePath.Contains("\")) {
        throw "Manifest paths must use forward slashes."
    }
    if ($RelativePath.StartsWith("/", [StringComparison]::Ordinal) -or
        [IO.Path]::IsPathRooted($RelativePath)) {
        throw "Manifest path must be relative to DistributionDirectory: $RelativePath"
    }

    $segments = @($RelativePath.Split([char]"/"))
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -in @(".", "..")) {
            throw "Manifest path contains an empty or traversal segment: $RelativePath"
        }
        if ($segment.EndsWith(".", [StringComparison]::Ordinal) -or
            $segment.EndsWith(" ", [StringComparison]::Ordinal) -or
            $segment.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
            $segment -match "^(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$") {
            throw "Manifest path contains a Windows-unsafe segment: $RelativePath"
        }
    }

    $nativeRelative = $RelativePath.Replace("/", [IO.Path]::DirectorySeparatorChar)
    $fullPath = [IO.Path]::GetFullPath((Join-Path $RootPath $nativeRelative))
    if (-not $fullPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Manifest path escapes DistributionDirectory: $RelativePath"
    }
    return [pscustomobject][ordered]@{
        relative_path = $RelativePath
        full_path = $fullPath
    }
}

$report = $null
try {
    if ([string]::IsNullOrWhiteSpace($DistributionDirectory)) {
        throw "DistributionDirectory is required."
    }
    $candidate = [IO.Path]::GetFullPath($DistributionDirectory)
    if ($candidate.StartsWith("\\", [StringComparison]::Ordinal)) {
        throw "DistributionDirectory must use local storage; UNC and device paths are not allowed."
    }
    $candidateVolumeRoot = [IO.Path]::GetPathRoot($candidate)
    $candidateDrive = [IO.DriveInfo]::new($candidateVolumeRoot)
    if ($candidateDrive.DriveType -eq [IO.DriveType]::Network) {
        throw "DistributionDirectory must use local storage; mapped network drives are not allowed."
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "DistributionDirectory must identify an existing directory."
    }
    $rootItem = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
    if ($rootItem -isnot [IO.DirectoryInfo]) {
        throw "DistributionDirectory must identify a directory."
    }
    Assert-SafeDistributionRoot -RootItem $rootItem

    $rootPath = [IO.Path]::GetFullPath($rootItem.FullName)
    $rootPrefix = $rootPath.TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
    $manifestPath = Join-Path $rootPath $ManifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "$ManifestName is missing from DistributionDirectory."
    }

    # Enumerate without following reparse points before trusting or hashing any
    # child path. The manifest itself is deliberately part of this safety scan.
    $treeFiles = @(Get-DistributionFiles -RootItem $rootItem)
    $manifestItem = Get-Item -LiteralPath $manifestPath -Force -ErrorAction Stop
    if (Test-IsReparsePoint -Item $manifestItem) {
        throw "$ManifestName must not be a reparse point."
    }

    $utf8Strict = [Text.UTF8Encoding]::new($false, $true)
    $manifestLines = [IO.File]::ReadAllLines($manifestPath, $utf8Strict)
    if ($manifestLines.Count -eq 0) {
        throw "$ManifestName must contain at least one file entry."
    }

    $expected = [System.Collections.Generic.Dictionary[string,string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    for ($index = 0; $index -lt $manifestLines.Count; $index++) {
        $lineNumber = $index + 1
        $line = $manifestLines[$index]
        $match = [regex]::Match($line, "^([0-9A-Fa-f]{64})  (.+)$")
        if (-not $match.Success) {
            throw "$ManifestName line $lineNumber must use '<sha256><two spaces><relative/path>'."
        }
        $hash = $match.Groups[1].Value.ToLowerInvariant()
        $entry = Resolve-SafeManifestEntry -RelativePath $match.Groups[2].Value -RootPath $rootPath -RootPrefix $rootPrefix
        if ($entry.relative_path.Equals($ManifestName, [StringComparison]::OrdinalIgnoreCase)) {
            throw "$ManifestName must not contain an entry for itself."
        }
        if ($expected.ContainsKey($entry.relative_path)) {
            throw "$ManifestName contains a duplicate path: $($entry.relative_path)"
        }
        $expected.Add($entry.relative_path, $hash)
    }

    $actual = [System.Collections.Generic.Dictionary[string,string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($file in $treeFiles) {
        if ($file.FullName.Equals($manifestPath, [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        if (-not $file.FullName.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Distribution file escaped DistributionDirectory: $($file.FullName)"
        }
        $relative = $file.FullName.Substring($rootPrefix.Length).Replace("\", "/")
        if ($actual.ContainsKey($relative)) {
            throw "Distribution contains a case-insensitive duplicate path: $relative"
        }
        $actual.Add($relative, $file.FullName)
    }

    $missing = @(
        $expected.Keys |
            Where-Object { -not $actual.ContainsKey($_) } |
            Sort-Object
    )
    $extra = @(
        $actual.Keys |
            Where-Object { -not $expected.ContainsKey($_) } |
            Sort-Object
    )
    $hashMismatches = [System.Collections.Generic.List[object]]::new()
    foreach ($relative in @($expected.Keys | Sort-Object)) {
        if (-not $actual.ContainsKey($relative)) {
            continue
        }
        $actualHash = Get-Sha256Hex -LiteralPath $actual[$relative]
        if (-not $actualHash.Equals($expected[$relative], [StringComparison]::OrdinalIgnoreCase)) {
            $hashMismatches.Add([pscustomobject][ordered]@{
                path = $relative
                expected_sha256 = $expected[$relative]
                actual_sha256 = $actualHash
            })
        }
    }

    $integrityOk = (
        $missing.Count -eq 0 -and
        $extra.Count -eq 0 -and
        $hashMismatches.Count -eq 0
    )
    $report = [pscustomobject][ordered]@{
        schema_version = 1
        component = "recovery-distribution-verifier"
        outcome = if ($integrityOk) { "PASS" } else { "FAIL" }
        exit_code = if ($integrityOk) { $ExitCode.PASS } else { $ExitCode.INTEGRITY_FAILED }
        side_effects = "NONE"
        distribution_directory = $rootPath
        manifest_path = $manifestPath
        manifest_self_verified = $false
        manifest_trust = "OUT_OF_BAND_REQUIRED"
        expected_file_count = $expected.Count
        actual_file_count = $actual.Count
        missing_files = @($missing)
        extra_files = @($extra)
        hash_mismatches = @($hashMismatches)
        errors = @()
    }
} catch {
    $report = [pscustomobject][ordered]@{
        schema_version = 1
        component = "recovery-distribution-verifier"
        outcome = "FAIL"
        exit_code = $ExitCode.INPUT_INVALID
        side_effects = "NONE"
        distribution_directory = $DistributionDirectory
        manifest_path = $null
        manifest_self_verified = $false
        manifest_trust = "OUT_OF_BAND_REQUIRED"
        expected_file_count = $null
        actual_file_count = $null
        missing_files = @()
        extra_files = @()
        hash_mismatches = @()
        errors = @($_.Exception.Message)
    }
}

$report | ConvertTo-Json -Depth 8
exit ([int]$report.exit_code)
