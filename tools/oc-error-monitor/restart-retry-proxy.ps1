# Restart the retry proxy with the smallest possible port gap.
#
# The CLI talks to 127.0.0.1:8787 for every model, so a dead listener shows up
# as "ConnectionRefused". This script stops the old process, starts the new one
# and reports the measured downtime, so the gap is never a surprise.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools/oc-error-monitor/restart-retry-proxy.ps1
param(
    [int]$Port = 8787,
    [int]$TimeoutSec = 20
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$rundir = Join-Path $env:USERPROFILE '.config\opencode\resilience'
$dst = Join-Path $rundir 'retry_proxy.py'
$pyw = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'

if (-not (Test-Path $pyw)) {
    $pyw = (Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter 'pythonw.exe' -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName)
}
if (-not $pyw -or -not (Test-Path $pyw)) { Write-Host '  [!!] pythonw.exe not found'; exit 1 }

New-Item -ItemType Directory -Path $rundir -Force | Out-Null
Copy-Item (Join-Path $here 'retry_proxy.py') $dst -Force

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$downtimeStart = Get-Date
foreach ($p in ($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
    # Only ever stop our own proxy, never some other program on the same port.
    if ($proc -and $proc.CommandLine -like '*retry_proxy*') {
        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "  [!!] port $Port is held by pid $p ($($proc.Name)) - not ours, aborting"
        exit 1
    }
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 50
}

# UseShellExecute=true launches the child fully detached (no inherited pipes),
# exactly like cmd's start. With UseShellExecute=false the child keeps the
# caller's stdout pipe open and any parent that pipes this script's output
# would wait for the proxy to exit.
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $pyw
$psi.Arguments = "`"$dst`""
$psi.UseShellExecute = $true
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.WorkingDirectory = $rundir
[System.Diagnostics.Process]::Start($psi) | Out-Null

$up = $false
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    try {
        $t = New-Object System.Net.Sockets.TcpClient
        $t.Connect('127.0.0.1', $Port)
        $t.Close()
        $up = $true
        break
    } catch {
        Start-Sleep -Milliseconds 150
    }
}

if (-not $up) {
    Write-Host "  [!!] retry proxy did not come back on port $Port"
    exit 1
}

$gap = [int]((Get-Date) - $downtimeStart).TotalMilliseconds
Write-Host ("  [ok] retry proxy back on 127.0.0.1:{0}  (downtime {1} ms)" -f $Port, $gap)
try {
    (Invoke-RestMethod "http://127.0.0.1:$Port/__stats") | ConvertTo-Json -Depth 3 -Compress
} catch { }
exit 0
