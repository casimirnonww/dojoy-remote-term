# DOJOY remote-term: keep this PC's reverse tunnel to the gateway up.
# Started at boot by the "DOJOY remote-term tunnel" scheduled task (as SYSTEM). The gateway's
# web terminal reaches this PC's sshd through 127.0.0.1:<Port> on the gateway; the tunnel
# account there can only listen on that one port. ASCII only (Windows PowerShell 5.1).
param(
    [Parameter(Mandatory = $true)][string]$Gateway,
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][string]$KeyFile,
    [Parameter(Mandatory = $true)][string]$KnownHosts,
    [string]$Log = (Join-Path $env:ProgramData 'remote-term\tunnel.log')
)
$ssh = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'

function Write-Log([string]$text) {
    if ((Test-Path -LiteralPath $Log) -and (Get-Item -LiteralPath $Log).Length -gt 1MB) {
        Move-Item -LiteralPath $Log -Destination ($Log + '.old') -Force
    }
    Add-Content -LiteralPath $Log -Value ((Get-Date -Format s) + ' ' + $text) -Encoding ascii
}

while ($true) {
    Write-Log "connecting to tunnel@$Gateway, listening on the gateway's 127.0.0.1:$Port"
    $arguments = @(
        '-N', '-T',
        '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', "UserKnownHostsFile=$KnownHosts",
        '-o', 'HostKeyAlias=remote-term-gateway',
        '-o', 'IdentitiesOnly=yes',
        '-i', $KeyFile,
        '-R', "127.0.0.1:${Port}:127.0.0.1:22",
        "tunnel@$Gateway"
    )
    $process = Start-Process -FilePath $ssh -ArgumentList $arguments -NoNewWindow -Wait -PassThru `
        -RedirectStandardError ($Log + '.ssh')
    if (Test-Path -LiteralPath ($Log + '.ssh')) {
        $detail = (Get-Content -LiteralPath ($Log + '.ssh') -Raw)
        if ($detail) { Write-Log ('ssh: ' + $detail.Trim()) }
    }
    Write-Log ('ssh exited with code ' + $process.ExitCode + '; retrying in 10 seconds')
    Start-Sleep -Seconds 10
}
