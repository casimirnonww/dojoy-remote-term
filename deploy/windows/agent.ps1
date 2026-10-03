# DOJOY remote-term agent for Windows: push this machine's metrics to the gateway.
# Windows PowerShell 5.1, standard cmdlets only. Keep this file ASCII: 5.1 reads BOM-less
# scripts in the system code page.
#
#   agent.ps1 -Url https://<gateway>/api/report -TokenFile <file>          # every 30 s, forever
#   agent.ps1 -Url ... -TokenFile <file> -Once                             # one report, exit 1 on failure
#   agent.ps1 -Url https://<gateway>/api/enroll -TokenFile <file> -Enroll `
#             -HostKeyFiles <ssh_host_*_key.pub> [-TunnelKeyFile <tunnel_key.pub>]
#
# Only HTTPS, except to this machine itself (tests). Redirects are never followed, so the
# token cannot be bounced to another host.
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$TokenFile,
    [switch]$Once,
    [int]$Interval = 30,
    [switch]$Enroll,
    [string[]]$HostKeyFiles = @(),
    [string]$TunnelKeyFile = ''
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

function Assert-SafeUrl([string]$value) {
    $uri = [Uri]$value
    if ($uri.Scheme -eq 'https') { return }
    if ($uri.Scheme -eq 'http' -and @('127.0.0.1', 'localhost', '[::1]') -contains $uri.Host) { return }
    throw "refusing to send the token to $value (https only)"
}

function Get-Number($value) {
    if ($null -eq $value) { return $null }
    return [double]$value
}

function Get-Metrics {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $cpus = @(Get-CimInstance -ClassName Win32_Processor)
    $drive = $env:SystemDrive
    $disk = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$drive'"

    # CIM class names are not translated on non-English Windows (Get-Counter paths are).
    $cpuPercent = $null
    $total = Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'"
    if ($null -ne $total -and $null -ne $total.PercentProcessorTime) {
        $cpuPercent = [Math]::Min(100.0, [double]$total.PercentProcessorTime)
    } else {
        $load = ($cpus | Measure-Object -Property LoadPercentage -Average).Average
        if ($null -ne $load) { $cpuPercent = [Math]::Min(100.0, [double]$load) }
    }

    $received = 0.0
    $sent = 0.0
    foreach ($nic in @(Get-CimInstance -ClassName Win32_PerfFormattedData_Tcpip_NetworkInterface)) {
        $received += [double]$nic.BytesReceivedPersec
        $sent += [double]$nic.BytesSentPersec
    }

    $memoryTotal = [long]$os.TotalVisibleMemorySize * 1024
    $memoryUsed = $memoryTotal - [long]$os.FreePhysicalMemory * 1024
    $diskTotal = [long]$disk.Size
    $diskUsed = $diskTotal - [long]$disk.FreeSpace
    $cores = 0
    foreach ($cpu in $cpus) { $cores += [int]$cpu.NumberOfLogicalProcessors }
    $arch = switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { 'x86_64' }
        'ARM64' { 'arm64' }
        default { [string]$env:PROCESSOR_ARCHITECTURE }
    }

    return [ordered]@{
        hostname = [string]$env:COMPUTERNAME
        os = ('{0} {1}' -f $os.Caption, $os.Version).Trim()
        arch = $arch
        cpu_model = ([string]$cpus[0].Name).Trim()
        cpu_cores = [int][Math]::Max(1, $cores)
        cpu_percent = $cpuPercent
        memory_total_bytes = [long]$memoryTotal
        memory_used_bytes = [long][Math]::Max([long]0, [Math]::Min([long]$memoryUsed, [long]$memoryTotal))
        disk_total_bytes = [long]$diskTotal
        disk_used_bytes = [long][Math]::Max([long]0, [Math]::Min([long]$diskUsed, [long]$diskTotal))
        uptime_seconds = [Math]::Round(((Get-Date) - $os.LastBootUpTime).TotalSeconds, 1)
        load_1 = $null
        network_interface = 'all'
        network_rx_bytes_per_second = [Math]::Round($received, 1)
        network_tx_bytes_per_second = [Math]::Round($sent, 1)
    }
}

function Send-Json([string]$target, [string]$token, $document) {
    $json = ConvertTo-Json -InputObject $document -Depth 5 -Compress
    $body = [Text.Encoding]::UTF8.GetBytes($json)
    $request = [Net.HttpWebRequest]::Create($target)
    $request.Method = 'POST'
    $request.ContentType = 'application/json'
    $request.AllowAutoRedirect = $false
    $request.Timeout = 15000
    $request.Headers['Authorization'] = 'Bearer ' + $token
    $request.ContentLength = $body.Length
    $stream = $request.GetRequestStream()
    $stream.Write($body, 0, $body.Length)
    $stream.Close()
    try {
        $response = $request.GetResponse()
    } catch [Net.WebException] {
        if ($null -eq $_.Exception.Response) { throw }
        $response = $_.Exception.Response
    }
    $code = [int]$response.StatusCode
    $response.Close()
    return $code
}

function Read-PublicKey([string]$path) {
    $line = (Get-Content -LiteralPath $path -TotalCount 1).Trim()
    $parts = $line -split '\s+'
    if ($parts.Count -lt 2) { throw "not a public key: $path" }
    return ($parts[0] + ' ' + $parts[1])
}

Assert-SafeUrl $Url
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

if ($Enroll) {
    try {
        $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
        $document = [ordered]@{ host_keys = @($HostKeyFiles | ForEach-Object { Read-PublicKey $_ }) }
        if ($TunnelKeyFile) { $document['tunnel_key'] = Read-PublicKey $TunnelKeyFile }
        $code = Send-Json $Url $token $document
        if ($code -lt 200 -or $code -ge 300) { throw "gateway answered HTTP $code" }
        Write-Output 'enrolled'
        exit 0
    } catch {
        Write-Warning ('enrollment failed: ' + $_.Exception.Message)
        exit 1
    }
}

while ($true) {
    try {
        $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
        $started = Get-Date
        $metrics = Get-Metrics
        $report = [ordered]@{
            metrics = $metrics
            probe_ms = [int][Math]::Min([double]60000, ((Get-Date) - $started).TotalMilliseconds)
        }
        $code = Send-Json $Url $token $report
        if ($code -lt 200 -or $code -ge 300) { throw "gateway answered HTTP $code" }
        if ($Once) {
            Write-Output 'report accepted'
            exit 0
        }
    } catch {
        Write-Warning ((Get-Date -Format s) + ' report failed: ' + $_.Exception.Message)
        if ($Once) { exit 1 }
    }
    Start-Sleep -Seconds $Interval
}
