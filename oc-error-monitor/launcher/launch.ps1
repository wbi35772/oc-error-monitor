# Start one build of oc-error-monitor and print a status panel.
#
# The version arrives as the path of a small handover file, because
# powershell.exe -File does not bind switch parameters and %~1 can carry a
# trailing space that stops the version from matching a directory name.
$ErrorActionPreference = 'Continue'

$Repo = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$Cfg = Join-Path $env:USERPROFILE '.config\opencode'

$Version = ''
if ($args.Count -ge 1 -and $args[0] -and (Test-Path $args[0])) {
    $Version = (Get-Content $args[0] -Raw -ErrorAction SilentlyContinue)
    if ($Version) { $Version = $Version.Trim() }
}

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

function Wait-Key {
    # ReadKey blocks forever when no console is attached (a pipe, a tool
    # call, a scheduled task). Only wait when a real console window exists.
    $code = @"
using System;
using System.Runtime.InteropServices;
public class Win {
  [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
}
"@
    try {
        Add-Type -TypeDefinition $code -ErrorAction SilentlyContinue | Out-Null
        if ([Win]::GetConsoleWindow() -eq [IntPtr]::Zero) { return }
        $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    } catch { }
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
    Write-Host '  version        state     port   pid    uptime   dashboard'
    Write-Host ('  ' + ('-' * 74))
    foreach ($r in $rows) {
        Write-Host ('  {0,-14} {1,-9} {2,-6} {3,-6} {4,-8} {5}' -f `
            $r.Version, $r.State, $r.Port, $r.Pid, $r.Uptime, $r.Dashboard)
    }
    Write-Host ''
}

$builds = @(Get-Builds)
if (-not $builds.Count) { Write-Host "[!!] no build under $Repo"; exit 1 }

# 'status' is an action, not a version name: the desktop shortcut that only
# wants to look at the state must not start anything.
if ($Version -and $Version.ToLower() -in @('status', 'list', 'show')) {
    Show-Panel
    Write-Host 'press any key to close this window...'
    Wait-Key
    exit 0
}

$targets = @()
if ($Version) {
    $targets = @($builds | Where-Object { $_.Version -like "*$Version*" })
} else {
    $targets = @($builds)
}

if ($targets.Count -eq 0) {
    Write-Host "[!!] no build matching '$Version'; have: $(($builds | ForEach-Object { $_.Version }) -join ', ')"
    exit 1
}

function Get-LivePort {
    param($Build)
    $pf = Join-Path $Build.RunDir 'active-port.txt'
    if (-not (Test-Path $pf)) { return $null }
    $p = (Get-Content $pf -Raw -ErrorAction SilentlyContinue)
    if ($p -and $p.Trim() -match '^\d+$') { return [int]$p.Trim() }
    return $null
}

function Test-Health {
    param([int]$Port, [string]$Version)
    if (-not $Port) { return $null }
    try {
        $h = Invoke-RestMethod ("http://127.0.0.1:$Port/__health") -TimeoutSec 2
        if ($h.version -ne $Version) { return $null }
        return $h
    } catch { return $null }
}

foreach ($b in $targets) {
    Write-Host ''
    Write-Host ("=== {0} ===" -f $b.Version) -ForegroundColor Cyan
    Write-Host ("    source : {0}" -f $b.Dir)
    Write-Host ("    runtime: {0}" -f $b.RunDir)

    $h = Test-Health -Port (Get-LivePort $b) -Version $b.Version
    if ($h) {
        Write-Host ("    already running on port {0} (pid {1})" -f (Get-LivePort $b), $h.pid) -ForegroundColor Green
        continue
    }

    # The batch file starts the supervisor and waits for its own health probe.
    # It is launched detached and without a pipe: its detached child inherits
    # stdout, and a pipe here would be held open by that child forever.
    Write-Host '    starting...'
    Start-Process -FilePath 'cmd.exe' `
        -ArgumentList @('/c', "`"$($b.Bat)`" --force") `
        -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null

    $deadline = (Get-Date).AddSeconds(30)
    $up = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        $up = Test-Health -Port (Get-LivePort $b) -Version $b.Version
        if ($up) { break }
    }
    if ($up) {
        Write-Host ("    up on port {0} (pid {1})" -f (Get-LivePort $b), $up.pid) -ForegroundColor Green
    } else {
        Write-Host ("    did not come up; see {0}\supervisor.log" -f $b.RunDir) -ForegroundColor Red
    }
}

Show-Panel
Write-Host 'press any key to close this window...'
Wait-Key
