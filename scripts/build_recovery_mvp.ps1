[CmdletBinding()]
param(
    [string]$Python = "python",
    [ValidatePattern("^dist-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    [string]$OutputDirectory = "dist-recovery",
    [string]$PyInstallerVersion = "6.16.0"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-NoReparsePoints {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$Message
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $rootItem = Get-Item -LiteralPath $Path -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw $Message
    }
    $nestedReparsePoint = Get-ChildItem -LiteralPath $Path -Recurse -Force |
        Where-Object {
            ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        } |
        Select-Object -First 1
    if ($null -ne $nestedReparsePoint) {
        throw "$Message Found: $($nestedReparsePoint.FullName)"
    }
}

function Invoke-PythonChecked {
    param(
        [Parameter(Mandatory)]
        [string]$Purpose,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Purpose failed with exit code $LASTEXITCODE"
    }
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    throw "OutputDirectory must be one direct dist-* child of the repository."
}
$output = [System.IO.Path]::GetFullPath((Join-Path $repo $OutputDirectory))
$outputParent = [System.IO.Directory]::GetParent($output).FullName
if (-not $outputParent.Equals($repo, [StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDirectory must be one direct dist-* child of the repository."
}
$outputPrefix = $output.TrimEnd([char[]]"\/") + [System.IO.Path]::DirectorySeparatorChar
$managedNames = @(
    ".build",
    "winsw-recovery-agent",
    "winsw-recovery-control-plane",
    "winsw-recovery-evidence-validator",
    "deployment",
    "scripts",
    "examples",
    "docs",
    "SHA256SUMS.txt"
)
if (Test-Path -LiteralPath $output) {
    $outputItem = Get-Item -LiteralPath $output -Force
    if (-not $outputItem.PSIsContainer) {
        throw "OutputDirectory must be a directory."
    }
    if (($outputItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "OutputDirectory must not be a reparse point."
    }
    foreach ($name in $managedNames) {
        $managedPath = Join-Path $output $name
        if (Test-Path -LiteralPath $managedPath) {
            $managedItem = Get-Item -LiteralPath $managedPath -Force
            if (($managedItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Managed build path must not be a reparse point: $managedPath"
            }
        }
    }
    Assert-NoReparsePoints -Path $output `
        -Message "OutputDirectory must not contain reparse points before the build."
}
$version = & $Python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to query the selected Python interpreter (exit code $LASTEXITCODE)"
}
if (-not $version.StartsWith("3.13.")) {
    throw "Recovery MVP must be built with Python 3.13; found $version"
}

$buildRoot = Join-Path $output ".build"
$specRoot = Join-Path $buildRoot "spec"
New-Item -ItemType Directory -Force -Path $specRoot | Out-Null

Push-Location $repo
try {
    Invoke-PythonChecked -Purpose "Dependency installation" -Arguments @(
        "-m", "pip", "install", "--disable-pip-version-check",
        "-r", "requirements.txt", "PyInstaller==$PyInstallerVersion"
    )

    Invoke-PythonChecked -Purpose "Agent onedir build" -Arguments @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "winsw-recovery-agent",
        "--paths", $repo,
        "--distpath", $output,
        "--workpath", (Join-Path $buildRoot "agent"),
        "--specpath", $specRoot,
        (Join-Path $repo "orchestrator\agent\__main__.py")
    )
    Invoke-PythonChecked -Purpose "Control Plane onedir build" -Arguments @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "winsw-recovery-control-plane",
        "--paths", $repo,
        "--distpath", $output,
        "--workpath", (Join-Path $buildRoot "control-plane"),
        "--specpath", $specRoot,
        "--add-data", "$(Join-Path $repo 'orchestrator\control_plane\templates');orchestrator\control_plane\templates",
        "--add-data", "$(Join-Path $repo 'orchestrator\control_plane\static');orchestrator\control_plane\static",
        (Join-Path $repo "orchestrator\control_plane\__main__.py")
    )
    Invoke-PythonChecked -Purpose "Acceptance evidence validator onedir build" -Arguments @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "winsw-recovery-evidence-validator",
        "--paths", $repo,
        "--distpath", $output,
        "--workpath", (Join-Path $buildRoot "evidence-validator"),
        "--specpath", $specRoot,
        (Join-Path $repo "scripts\validate_recovery_evidence.py")
    )

    $agentExecutable = Join-Path $output "winsw-recovery-agent\winsw-recovery-agent.exe"
    $controlPlaneExecutable = Join-Path $output "winsw-recovery-control-plane\winsw-recovery-control-plane.exe"
    $evidenceValidatorExecutable = Join-Path $output "winsw-recovery-evidence-validator\winsw-recovery-evidence-validator.exe"
    if (-not (Test-Path -LiteralPath $agentExecutable -PathType Leaf)) {
        throw "Agent package executable was not produced: $agentExecutable"
    }
    if (-not (Test-Path -LiteralPath $controlPlaneExecutable -PathType Leaf)) {
        throw "Control Plane package executable was not produced: $controlPlaneExecutable"
    }
    if (-not (Test-Path -LiteralPath $evidenceValidatorExecutable -PathType Leaf)) {
        throw "Evidence validator package executable was not produced: $evidenceValidatorExecutable"
    }

    $deploymentBundle = [System.IO.Path]::GetFullPath((Join-Path $output "deployment"))
    $deploymentScripts = [System.IO.Path]::GetFullPath((Join-Path $output "scripts"))
    $deploymentExamples = [System.IO.Path]::GetFullPath((Join-Path $output "examples"))
    $deploymentDocs = [System.IO.Path]::GetFullPath((Join-Path $output "docs"))
    $deploymentContracts = Join-Path $deploymentDocs "contracts"
    foreach ($bundlePath in @(
        $deploymentBundle,
        $deploymentScripts,
        $deploymentExamples,
        $deploymentDocs
    )) {
        if (-not $bundlePath.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to replace a deployment path outside the output directory: $bundlePath"
        }
        if (Test-Path -LiteralPath $bundlePath) {
            $bundleItem = Get-Item -LiteralPath $bundlePath -Force
            if (($bundleItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to remove a reparse-point deployment path: $bundlePath"
            }
            Remove-Item -LiteralPath $bundlePath -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Force -Path @(
        $deploymentBundle,
        $deploymentScripts,
        $deploymentExamples,
        $deploymentDocs,
        $deploymentContracts
    ) | Out-Null
    Copy-Item -LiteralPath (Join-Path $repo "scripts\install_recovery_service.ps1") `
        -Destination $deploymentScripts -Force
    Copy-Item -LiteralPath (Join-Path $repo "scripts\test_recovery_host_preflight.ps1") `
        -Destination $deploymentScripts -Force
    Copy-Item -LiteralPath (Join-Path $repo "scripts\get_recovery_host_facts.ps1") `
        -Destination $deploymentScripts -Force
    Copy-Item -LiteralPath (Join-Path $repo "scripts\smoke_recovery_binaries.ps1") `
        -Destination $deploymentScripts -Force
    Copy-Item -LiteralPath (Join-Path $repo "scripts\verify_recovery_distribution.ps1") -Destination $deploymentScripts -Force
    Copy-Item -LiteralPath (Join-Path $repo "examples\agent.example.json") `
        -Destination $deploymentExamples -Force
    Copy-Item -LiteralPath (Join-Path $repo "examples\control-plane.example.json") `
        -Destination $deploymentExamples -Force
    Copy-Item -LiteralPath (Join-Path $repo "examples\recovery-evidence.template.json") `
        -Destination $deploymentExamples -Force
    Copy-Item -LiteralPath (Join-Path $repo "examples\deployment-inventory.example.json") `
        -Destination $deploymentExamples -Force
    Copy-Item -LiteralPath (Join-Path $repo "deployment\winsw-x64-v2.12.0.lock.json") `
        -Destination $deploymentBundle -Force
    Copy-Item -LiteralPath (Join-Path $repo "docs\recovery-mvp-operations.md") `
        -Destination $deploymentDocs -Force
    Copy-Item -LiteralPath (Join-Path $repo "docs\contracts\recovery-mvp-evidence-v1.md") `
        -Destination $deploymentContracts -Force
    Copy-Item -LiteralPath (Join-Path $repo "docs\contracts\recovery-deployment-inventory-v1.md") `
        -Destination $deploymentContracts -Force

    $resolvedBuildRoot = [System.IO.Path]::GetFullPath($buildRoot)
    if (-not $resolvedBuildRoot.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a build path outside the output directory: $resolvedBuildRoot"
    }
    $buildRootItem = Get-Item -LiteralPath $resolvedBuildRoot -Force
    if (($buildRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to clean a reparse-point build path: $resolvedBuildRoot"
    }
    Remove-Item -LiteralPath $resolvedBuildRoot -Recurse -Force

    $manifest = Join-Path $output "SHA256SUMS.txt"
    $reparseItems = @(
        Get-ChildItem -LiteralPath $output -Recurse -Force |
            Where-Object {
                ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
            }
    )
    if ($reparseItems.Count -gt 0) {
        throw "Distribution must not contain reparse points."
    }
    $lines = Get-ChildItem -LiteralPath $output -Recurse -File |
        Where-Object { $_.FullName -ne $manifest } |
        Sort-Object FullName |
        ForEach-Object {
            if (-not $_.FullName.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Distribution file escaped the output directory: $($_.FullName)"
            }
            $relative = $_.FullName.Substring($outputPrefix.Length).Replace("\", "/")
            "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant(), $relative
        }
    [System.IO.File]::WriteAllLines($manifest, $lines, [System.Text.UTF8Encoding]::new($false))
    Write-Host "onedir packages written to $output"
}
finally {
    Pop-Location
}
