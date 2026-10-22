# Restart the retry proxy.
#
# Default: stop the old process, start the new one, report the measured gap.
# -Handover: start the new instance *first* (Windows SO_REUSEADDR lets both
# bind 8787), wait until its /__health answers with a new pid, then stop the
# old one. The socket is never closed, so the CLI never sees ConnectionRefused.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File restart-retry-proxy.ps1 [-Handover]
param(
    [int]$Port = 0,
    [int]$TimeoutSec = 20,
    [switch]$Handover
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ver = (Get-Content (Join-Path $here 'VERSION') -Raw -ErrorAction SilentlyContinue)
if ($ver) { $ver = $ver.Trim() } else { $ver = 'unknown' }
$rundir = Join-Path $env:USERPROFILE ('.config\opencode\oc-error-monitor-v' + $ver)
$dst = Join-Path $rundir 'retry_proxy.py'
$ui = Join-Path $rundir 'ui.html'
$pyw = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'

# The proxy moves to another port when the preferred one is taken, so the
# active port is the one that matters.
if ($Port -le 0) {
    $activeFile = Join-Path $rundir 'active-port.txt'
    if (Test-Path $activeFile) {
        $txt = ([System.IO.File]::ReadAllText($activeFile)).Trim()
        if ($txt -match '^\d+$') { $Port = [int]$txt }
    }
    if ($Port -le 0) { $Port = 8788 }
}

if (-not (Test-Path $pyw)) {
    $pyw = (Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter 'pythonw.exe' -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName)
}
if (-not $pyw -or -not (Test-Path $pyw)) { Write-Host '  [!!] pythonw.exe not found'; exit 1 }

New-Item -ItemType Directory -Path $rundir -Force | Out-Null
# When this script runs from the runtime directory itself (the deployed copy),
# source and destination are the same file: copying it onto itself errors.
if ([System.IO.Path]::GetFullPath($here) -ne [System.IO.Path]::GetFullPath($rundir)) {
    Copy-Item (Join-Path $here 'retry_proxy.py') $dst -Force
    if (Test-Path (Join-Path $here 'ui.html')) { Copy-Item (Join-Path $here 'ui.html') $ui -Force }
    # The gateway is started by the dashboard on first use (POST
    # /__gateway/start) and reads its code from this same directory, so it has
    # to travel with the proxy or that button has nothing to run.
    $gw = Join-Path $here 'gateway.py'
    if (Test-Path $gw) { Copy-Item $gw (Join-Path $rundir 'gateway.py') -Force }
    # VERSION travels with the code, so the live instance reports the build it
    # actually came from rather than a value baked in at import time. It is
    # read from this version's own directory: the repository root carries the
    # version of the branch, not of the build being deployed.
    $ver = Join-Path $here 'VERSION'
    if (Test-Path $ver) { Copy-Item $ver (Join-Path $rundir 'VERSION') -Force }
}

# Loopback health check that never consults the system proxy.
function Get-Health {
    param([int]$P)
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$P/__health")
        $req.Proxy = $null
        $req.Timeout = 2000
        $resp = $req.GetResponse()
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $txt = $sr.ReadToEnd(); $sr.Close(); $resp.Close()
        return $txt | ConvertFrom-Json
    } catch { return $null }
}

function Start-Proxy {
    param([string]$Extra = '')
    # The child must bind the port we are serving on, not the default.
    $env:RETRY_PROXY_PORT = "$Port"
    $outLog = Join-Path $rundir 'proxy.out.log'
    $errLog = Join-Path $rundir 'proxy.err.log'
    $argList = if ($Extra) { @("`"$dst`"", $Extra) } else { @("`"$dst`"") }
    # Redirecting the child's output to files keeps it from holding the
    # caller's stdout pipe open, which would make a parent that pipes this
    # script's output wait for the proxy to exit.
    return Start-Process -FilePath $pyw -ArgumentList $argList -WorkingDirectory $rundir `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
}

$ours = @()
$downtimeStart = Get-Date
foreach ($p in (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -like '*retry_proxy*') { $ours += $p }
    else {
        Write-Host "  [!!] port $Port is held by pid $p ($($proc.Name)) - not ours, aborting"
        exit 1
    }
}
$oldPid = if ($ours.Count -ge 1) { $ours[0] } else { 0 }
$before = Get-Health -P $Port

if ($Handover -and $oldPid -gt 0) {
    $newProc = Start-Proxy -Extra "--handover --handover-from $oldPid"
    # The incumbent keeps receiving new connections while both are bound (that
    # is what makes the swap gapless), so /__health still answers with the OLD
    # pid here. Bind evidence is the listener table instead.
    $bound = $false
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($newProc.HasExited) { break }
        $owners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
        if ($owners -contains $newProc.Id) { $bound = $true; break }
        Start-Sleep -Milliseconds 150
    }
    if ($bound) {
        # The new instance asks the old one to step down through a file. An
        # HTTP request would be unreliable here: both instances are bound and
        # Windows hands a new connection to one of them at random.
        $drained = $false
        foreach ($p in $ours) {
            $deadline = (Get-Date).AddSeconds(60)
            while ((Get-Date) -lt $deadline) {
                if (-not (Get-Process -Id $p -ErrorAction SilentlyContinue)) { $drained = $true; break }
                Start-Sleep -Milliseconds 150
            }
            if (Get-Process -Id $p -ErrorAction SilentlyContinue) {
                Write-Host "  [..] old pid $p did not step down in time, stopping it"
                Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
            }
        }
        $ok = $false
        $deadline = (Get-Date).AddSeconds($TimeoutSec)
        while ((Get-Date) -lt $deadline) {
            $h = Get-Health -P $Port
            if ($h -and [int]$h.pid -eq $newProc.Id) { $ok = $true; break }
            Start-Sleep -Milliseconds 100
        }
        if ($ok) {
            Write-Host ("  [ok] handover complete  old pid {0} -> new pid {1}  (drained={2}, downtime 0 ms)" -f $oldPid, $newProc.Id, $drained)
            try { ($h | ConvertTo-Json -Compress) } catch { }
            exit 0
        }
        Write-Host '  [!!] new instance bound but did not answer after the old one exited'
        exit 1
    }
    Write-Host '  [..] handover instance did not bind; keeping the old one, doing a stop+start'
    if (-not $newProc.HasExited) { Stop-Process -Id $newProc.Id -Force -ErrorAction SilentlyContinue }
}
foreach ($p in $ours) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
$downtimeStart = Get-Date

# Wait for the port to be released, then start normally.
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 50
}
$null = Start-Proxy

$up = $false
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    try {
        $t = New-Object System.Net.Sockets.TcpClient
        $t.Connect('127.0.0.1', $Port)
        $t.Close()
        $up = $true
        break
    } catch { Start-Sleep -Milliseconds 150 }
}

if (-not $up) {
    Write-Host "  [!!] retry proxy did not come back on port $Port"
    exit 1
}

$gap = [int]((Get-Date) - $downtimeStart).TotalMilliseconds
Write-Host ("  [ok] retry proxy back on 127.0.0.1:{0}  (downtime {1} ms)" -f $Port, $gap)
try { (Get-Health -P $Port | ConvertTo-Json -Compress) } catch { }
exit 0
