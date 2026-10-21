# Stop one build of oc-error-monitor and show what is left running.
# Usage: stop.ps1 [version]
# With no argument every build is stopped.
param(
    [Parameter(Position = 0)][string]$Version = '',
    [switch]$NoPause
)

$ErrorActionPreference = 'Continue'
$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Cfg = Join-Path $env:USERPROFILE '.config\opencode'

function Get-Builds {
    Get-ChildItem $Repo -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'start-retry-proxy.bat') } |
        ForEach-Object {
            $ver = (Get-Content (Join-Path $_.FullName 'VERSION') -Raw -ErrorAction SilentlyContinue)
            if ($ver) { $ver = $ver.Trim() } else { $ver = '?' }
            [pscustomobject]@{
                Version = $ver
                Dir     = $_.FullName
                Bat     = (Join-Path $_.FullName 'start-retry-proxy.bat')
                RunDir  = (Join-Path $Cfg ('oc-error-monitor-v' + $ver))
            }
        }
}

function Show-Panel {
    $rows = @()
    foreach ($b in (Get-Builds)) {
        $port = $null
        $pf = Join-Path $b.RunDir 'active-port.txt'
        if (Test-Path $pf) {
            $p = (Get-Content $pf -Raw -ErrorAction SilentlyContinue)
            if ($p -and $p.Trim() -match '^\d+$') { $port = [int]$p.Trim() }
        }
        $h = $null
        if ($port) {
            try { $h = Invoke-RestMethod ("http://127.0.0.1:$port/__health") -TimeoutSec 2 } catch { $h = $null }
        }
        $marker = Test-Path (Join-Path $b.RunDir 'stopped.by-user')
        $rows += [pscustomobject]@{
            Version = $b.Version
            State   = if ($h) { 'RUN' } elseif ($marker) { 'stopped' } else { 'down' }
            Port    = if ($port) { $port } else { '-' }
            Pid     = if ($h) { $h.pid } else { '-' }
        }
    }
    Write-Host ''
    Write-Host '  version        state     port   pid'
    Write-Host ('  ' + ('-' * 40))
    foreach ($r in $rows) { Write-Host ('  {0,-14} {1,-9} {2,-6} {3}' -f $r.Version, $r.State, $r.Port, $r.Pid) }
    Write-Host ''
}

$builds = @(Get-Builds)
if (-not $builds.Count) { Write-Host '[!!] no build under oc-error-monitor/'; exit 1 }

$targets = if ($Version) {
    @($builds | Where-Object { $_.Version -like "*$Version*" })
} else { $builds }

if (-not $targets.Count) {
    Write-Host "[!!] no build matching '$Version'"
    exit 1
}

foreach ($b in $targets) {
    Write-Host ("=== stopping {0} ===" -f $b.Version) -ForegroundColor Yellow
    # The proxy writes the marker itself, so the minute watchdog leaves it
    # alone afterwards. Killing the supervisor first stops it from respawning.
    $pat = [regex]::Escape($b.RunDir)
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $pat -and $_.CommandLine -match 'supervisor\.py' } |
        ForEach-Object { Write-Host "    supervisor pid=$($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    $port = $null
    $pf = Join-Path $b.RunDir 'active-port.txt'
    if (Test-Path $pf) {
        $p = (Get-Content $pf -Raw -ErrorAction SilentlyContinue)
        if ($p -and $p.Trim() -match '^\d+$') { $port = [int]$p.Trim() }
    }
    if ($port) {
        try {
            Invoke-RestMethod ("http://127.0.0.1:$port/__control") -Method Post `
                -ContentType 'application/json' -Body '{"action":"stop"}' -TimeoutSec 5 | Out-Null
            Write-Host "    proxy asked to stop on port $port"
        } catch { Write-Host "    proxy on port $port did not answer" }
    }
    Start-Sleep -Milliseconds 1200
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $pat } |
        ForEach-Object { Write-Host "    kill pid=$($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $marker = Join-Path $b.RunDir 'stopped.by-user'
    if (-not (Test-Path $marker)) {
        New-Item -ItemType Directory -Force $b.RunDir | Out-Null
        Set-Content -Path $marker -Value ('{"at":"' + (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') + '","by":"launcher"}') -Encoding utf8
    }
    Write-Host "    stopped"
}

Start-Sleep -Seconds 1
Show-Panel
if (-not $NoPause) {
    Write-Host 'press any key to close this window...'
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
}
