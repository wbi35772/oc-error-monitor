# Shared launcher: start one build, then print a status panel the operator can
# read in a console window. Everything is resolved from the repository, so the
# same script works for any version directory.
param(
    [Parameter(Position = 0)][string]$Version = '',
    [switch]$NoPause
)

$ErrorActionPreference = 'Continue'
$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)  # ...\oc-error-monitor
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
            Uptime  = if ($h) { "{0}s" -f [int]$h.uptime_s } else { '-' }
            Dashboard = if ($h) { "http://127.0.0.1:$port/__ui" } else { '-' }
        }
    }
    Write-Host ''
    Write-Host ('  version        state     port   pid    uptime   dashboard')
    Write-Host ('  ' + ('-' * 74))
    foreach ($r in $rows) {
        Write-Host ('  {0,-14} {1,-9} {2,-6} {3,-6} {4,-8} {5}' -f `
            $r.Version, $r.State, $r.Port, $r.Pid, $r.Uptime, $r.Dashboard)
    }
    Write-Host ''
}

$builds = @(Get-Builds)
if (-not $builds.Count) { Write-Host '[!!] no build under oc-error-monitor/'; exit 1 }

# 'status' is an action, not a version name: the desktop shortcut that only
# wants to look at the state must not start anything.
if ($Version -and $Version.ToLower() -in @('status', 'list', 'show')) {
    Show-Panel
    if (-not $NoPause) {
        Write-Host 'press any key to close this window...'
        $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    }
    exit 0
}

$targets = @()
if ($Version) {
    $targets = @($builds | Where-Object { $_.Version -like "*$Version*" })
    if (-not $targets.Count) {
        Write-Host "[!!] no build matching '$Version'; have: $(($builds | ForEach-Object { $_.Version }) -join ', ')"
        exit 1
    }
} else {
    $targets = $builds
}

foreach ($b in $targets) {
    Write-Host ''
    Write-Host ("=== starting {0} ===" -f $b.Version) -ForegroundColor Cyan
    Write-Host ("    source : {0}" -f $b.Dir)
    Write-Host ("    runtime: {0}" -f $b.RunDir)
    & cmd /c "`"$($b.Bat)`" --force" 2>&1 | ForEach-Object { Write-Host "    $_" }
}

Start-Sleep -Seconds 2
Show-Panel

if (-not $NoPause) {
    Write-Host 'press any key to close this window...'
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
}
