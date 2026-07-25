[CmdletBinding()]
param(
    [string[]]$WindowsServiceName = @(),
    [string[]]$CandidatePort = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-RecoveryHostNameFact {
    return [Environment]::MachineName
}

function Get-RecoveryHostWindowsFact {
    $operatingSystems = @(
        Get-CimInstance `
            -ClassName Win32_OperatingSystem `
            -Property Version, OSArchitecture `
            -ErrorAction Stop
    )
    if ($operatingSystems.Count -ne 1) {
        throw "The local Windows operating system could not be identified uniquely."
    }

    return [pscustomobject][ordered]@{
        version = [string]$operatingSystems[0].Version
        architecture = [string]$operatingSystems[0].OSArchitecture
    }
}

function Get-RecoveryHostActiveUnicastIpFact {
    $addresses = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($networkInterface in
        [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($networkInterface.OperationalStatus -ne
            [Net.NetworkInformation.OperationalStatus]::Up) {
            continue
        }
        if ($networkInterface.NetworkInterfaceType -in @(
                [Net.NetworkInformation.NetworkInterfaceType]::Loopback
            )) {
            continue
        }

        foreach ($unicastAddress in $networkInterface.GetIPProperties().UnicastAddresses) {
            $address = $unicastAddress.Address
            if (
                $null -eq $address -or
                -not (Test-RecoveryInventoryCompatibleIpAddress -Address $address)
            ) {
                continue
            }
            [void]$addresses.Add($address.ToString())
        }
    }

    return @($addresses | Sort-Object)
}

function Test-RecoveryInventoryCompatibleIpAddress {
    param([Parameter(Mandatory)][Net.IPAddress]$Address)

    if (
        [Net.IPAddress]::IsLoopback($Address) -or
        $Address.Equals([Net.IPAddress]::Any) -or
        $Address.Equals([Net.IPAddress]::IPv6Any)
    ) {
        return $false
    }

    if ($Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) {
        return -not (
            $Address.IsIPv6LinkLocal -or
            $Address.IsIPv6Multicast -or
            $Address.IsIPv4MappedToIPv6
        )
    }

    if ($Address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        return $false
    }
    $bytes = $Address.GetAddressBytes()
    if (
        ($bytes[0] -eq 169 -and $bytes[1] -eq 254) -or
        ($bytes[0] -ge 224 -and $bytes[0] -le 239)
    ) {
        return $false
    }
    return $true
}

function Get-RecoveryHostServiceFact {
    param([Parameter(Mandatory)][string]$Name)

    # WQL quotes are doubled. No remote session or computer name is accepted.
    $escapedName = $Name.Replace("'", "''")
    $services = @(
        Get-CimInstance `
            -ClassName Win32_Service `
            -Filter ("Name = '{0}'" -f $escapedName) `
            -Property Name, DisplayName, StartMode, State `
            -ErrorAction Stop
    )
    if ($services.Count -eq 0) {
        return [pscustomobject][ordered]@{
            Name = $Name
            DisplayName = $null
            StartMode = $null
            State = "NOT_FOUND"
        }
    }
    if ($services.Count -ne 1) {
        throw "The local Windows service could not be identified uniquely."
    }

    return [pscustomobject][ordered]@{
        Name = [string]$services[0].Name
        DisplayName = [string]$services[0].DisplayName
        StartMode = [string]$services[0].StartMode
        State = [string]$services[0].State
    }
}

function Get-RecoveryHostCandidatePortFact {
    param([Parameter(Mandatory)][int]$Port)

    $connections = @(
        Get-NetTCPConnection -ErrorAction Stop |
            Where-Object { $_.LocalPort -eq $Port }
    )
    $listeners = @($connections | Where-Object { $_.State -eq "Listen" })
    $listenAddresses = @(
        $listeners |
            ForEach-Object { [string]$_.LocalAddress } |
            Sort-Object -Unique
    )
    return [pscustomobject][ordered]@{
        port = $Port
        occupied = $connections.Count -gt 0
        listening = $listeners.Count -gt 0
        listen_addresses = @($listenAddresses)
    }
}

function Test-RecoveryHostFactsInput {
    param(
        [string[]]$RequestedServiceNames,
        [object[]]$RequestedCandidatePorts
    )

    if ($null -eq $RequestedServiceNames -or $RequestedServiceNames.Count -eq 0) {
        return $false
    }
    if (
        $RequestedServiceNames.Count -gt 1024 -or
        @($RequestedCandidatePorts).Count -gt 1024
    ) {
        return $false
    }

    $seenServiceNames = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($serviceName in $RequestedServiceNames) {
        if (
            [string]::IsNullOrWhiteSpace($serviceName) -or
            $serviceName.Length -gt 256 -or
            $serviceName -ne $serviceName.Trim() -or
            -not $seenServiceNames.Add($serviceName)
        ) {
            return $false
        }
    }

    $seenPorts = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($port in @($RequestedCandidatePorts)) {
        $normalizedPort = 0
        $parsed = [int]::TryParse(
            [string]$port,
            [Globalization.NumberStyles]::None,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$normalizedPort
        )
        if (
            -not $parsed -or
            $normalizedPort -lt 1 -or
            $normalizedPort -gt 65535 -or
            -not $seenPorts.Add($normalizedPort)
        ) {
            return $false
        }
    }
    return $true
}

function Invoke-RecoveryHostFacts {
    param(
        [string[]]$RequestedServiceNames,
        [object[]]$RequestedCandidatePorts = @()
    )

    $outcome = "PASS"
    $hostname = $null
    $windows = [pscustomobject][ordered]@{
        version = $null
        architecture = $null
    }
    $activeUnicastIps = @()
    $serviceFacts = [System.Collections.Generic.List[object]]::new()
    $candidatePortFacts = [System.Collections.Generic.List[object]]::new()

    if (-not (Test-RecoveryHostFactsInput `
            -RequestedServiceNames $RequestedServiceNames `
            -RequestedCandidatePorts $RequestedCandidatePorts)) {
        $outcome = "FAIL"
    } else {
        try {
            $rawHostname = Get-RecoveryHostNameFact
            if (
                $rawHostname -isnot [string] -or
                [string]::IsNullOrWhiteSpace($rawHostname)
            ) {
                throw "The local hostname could not be confirmed."
            }
            $hostname = [string]$rawHostname
        } catch {
            $outcome = "FAIL"
            $hostname = $null
        }
        try {
            $rawWindows = Get-RecoveryHostWindowsFact
            if (
                $rawWindows.version -isnot [string] -or
                $rawWindows.architecture -isnot [string] -or
                [string]::IsNullOrWhiteSpace($rawWindows.version) -or
                [string]::IsNullOrWhiteSpace($rawWindows.architecture) -or
                [string]$rawWindows.architecture -notmatch "64"
            ) {
                throw "The local Windows version or architecture could not be confirmed."
            }
            $windows = [pscustomobject][ordered]@{
                version = [string]$rawWindows.version
                architecture = [string]$rawWindows.architecture
            }
        } catch {
            $outcome = "FAIL"
        }
        try {
            $normalizedAddresses = [System.Collections.Generic.List[string]]::new()
            $seenAddresses = [System.Collections.Generic.HashSet[string]]::new(
                [StringComparer]::OrdinalIgnoreCase
            )
            foreach ($rawAddress in @(Get-RecoveryHostActiveUnicastIpFact)) {
                if ($rawAddress -isnot [string]) {
                    throw "A local address was not returned as a string."
                }
                [Net.IPAddress]$parsedAddress = $null
                if (
                    -not [Net.IPAddress]::TryParse($rawAddress, [ref]$parsedAddress) -or
                    $parsedAddress.ToString() -cne $rawAddress -or
                    -not (Test-RecoveryInventoryCompatibleIpAddress `
                        -Address $parsedAddress) -or
                    -not $seenAddresses.Add($rawAddress)
                ) {
                    throw "A local address was not Inventory-compatible."
                }
                $normalizedAddresses.Add($rawAddress)
            }
            $activeUnicastIps = @($normalizedAddresses.ToArray() | Sort-Object)
            if ($activeUnicastIps.Count -eq 0) {
                throw "No Inventory-compatible local address could be confirmed."
            }
        } catch {
            $outcome = "FAIL"
            $activeUnicastIps = @()
        }

        foreach ($serviceName in $RequestedServiceNames) {
            try {
                $rawServiceFact = Get-RecoveryHostServiceFact -Name $serviceName
                if ([string]$rawServiceFact.State -ieq "NOT_FOUND") {
                    $serviceFacts.Add([pscustomobject][ordered]@{
                        Name = $serviceName
                        DisplayName = $null
                        StartMode = $null
                        State = "NOT_FOUND"
                    })
                    $outcome = "FAIL"
                    continue
                }
                if (
                    $rawServiceFact.Name -isnot [string] -or
                    $rawServiceFact.DisplayName -isnot [string] -or
                    $rawServiceFact.StartMode -isnot [string] -or
                    $rawServiceFact.State -isnot [string] -or
                    [string]::IsNullOrWhiteSpace($rawServiceFact.Name) -or
                    $rawServiceFact.Name -ine $serviceName -or
                    [string]::IsNullOrWhiteSpace($rawServiceFact.DisplayName) -or
                    $rawServiceFact.StartMode -notin @(
                        "Auto", "Manual", "Disabled", "Boot", "System"
                    ) -or
                    $rawServiceFact.State -notin @(
                        "Stopped", "Start Pending", "Stop Pending", "Running",
                        "Continue Pending", "Pause Pending", "Paused", "Unknown"
                    )
                ) {
                    throw "The local Windows service facts could not be confirmed."
                }
                $serviceFact = [pscustomobject][ordered]@{
                    Name = [string]$rawServiceFact.Name
                    DisplayName = [string]$rawServiceFact.DisplayName
                    StartMode = [string]$rawServiceFact.StartMode
                    State = [string]$rawServiceFact.State
                }
                if (
                    $serviceFact.State -ieq "Unknown"
                ) {
                    $outcome = "FAIL"
                }
                $serviceFacts.Add($serviceFact)
            } catch {
                $outcome = "FAIL"
                $serviceFacts.Add([pscustomobject][ordered]@{
                    Name = $serviceName
                    DisplayName = $null
                    StartMode = $null
                    State = "UNKNOWN"
                })
            }
        }

        foreach ($port in @($RequestedCandidatePorts)) {
            $normalizedPort = 0
            [void][int]::TryParse(
                [string]$port,
                [Globalization.NumberStyles]::None,
                [Globalization.CultureInfo]::InvariantCulture,
                [ref]$normalizedPort
            )
            try {
                $rawCandidatePortFact = Get-RecoveryHostCandidatePortFact `
                    -Port $normalizedPort
                if (
                    $rawCandidatePortFact.port -isnot [int] -or
                    $rawCandidatePortFact.port -ne $normalizedPort -or
                    $rawCandidatePortFact.occupied -isnot [bool] -or
                    $rawCandidatePortFact.listening -isnot [bool]
                ) {
                    throw "The local candidate port facts could not be confirmed."
                }
                $listenAddresses = [System.Collections.Generic.List[string]]::new()
                $seenListenAddresses = [System.Collections.Generic.HashSet[string]]::new(
                    [StringComparer]::OrdinalIgnoreCase
                )
                foreach ($rawListenAddress in @(
                        $rawCandidatePortFact.listen_addresses
                    )) {
                    if ($rawListenAddress -isnot [string]) {
                        throw "A listen address was not returned as a string."
                    }
                    [Net.IPAddress]$parsedListenAddress = $null
                    if (
                        -not [Net.IPAddress]::TryParse(
                            $rawListenAddress,
                            [ref]$parsedListenAddress
                        ) -or
                        $parsedListenAddress.ToString() -cne $rawListenAddress -or
                        -not $seenListenAddresses.Add($rawListenAddress)
                    ) {
                        throw "A listen address could not be confirmed."
                    }
                    $listenAddresses.Add($rawListenAddress)
                }
                if (
                    ($rawCandidatePortFact.listening -and
                        $listenAddresses.Count -eq 0) -or
                    (-not $rawCandidatePortFact.listening -and
                        $listenAddresses.Count -ne 0) -or
                    ($rawCandidatePortFact.listening -and
                        -not $rawCandidatePortFact.occupied)
                ) {
                    throw "The local candidate port facts were contradictory."
                }
                $candidatePortFacts.Add([pscustomobject][ordered]@{
                    port = $normalizedPort
                    occupied = [bool]$rawCandidatePortFact.occupied
                    listening = [bool]$rawCandidatePortFact.listening
                    listen_addresses = [string[]]$listenAddresses.ToArray()
                })
            } catch {
                $outcome = "FAIL"
                $candidatePortFacts.Add([pscustomobject][ordered]@{
                    port = $normalizedPort
                    occupied = $null
                    listening = $null
                    listen_addresses = @()
                })
            }
        }
    }

    return [pscustomobject][ordered]@{
        schema_version = 1
        component = "recovery-host-facts"
        outcome = $outcome
        side_effects = "NONE"
        remote_hosts_scanned = 0
        hostname = $hostname
        windows = $windows
        active_unicast_ips = @($activeUnicastIps)
        services = [object[]]$serviceFacts.ToArray()
        candidate_ports = [object[]]$candidatePortFacts.ToArray()
    }
}

function New-RecoveryHostFactsFailureReport {
    return [pscustomobject][ordered]@{
        schema_version = 1
        component = "recovery-host-facts"
        outcome = "FAIL"
        side_effects = "NONE"
        remote_hosts_scanned = 0
        hostname = $null
        windows = [pscustomobject][ordered]@{
            version = $null
            architecture = $null
        }
        active_unicast_ips = @()
        services = @()
        candidate_ports = @()
    }
}

if ($MyInvocation.InvocationName -eq ".") {
    return
}

try {
    $report = Invoke-RecoveryHostFacts `
        -RequestedServiceNames $WindowsServiceName `
        -RequestedCandidatePorts $CandidatePort
} catch {
    $report = New-RecoveryHostFactsFailureReport
}
$report | ConvertTo-Json -Depth 6
if ($report.outcome -eq "PASS") {
    exit 0
}
exit 2
