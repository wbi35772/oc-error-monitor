# Start, stop or switch between the oc-error-monitor builds.
#
#   switch-instance.bat status            what is up, on which port
#   switch-instance.bat start beta        start a build (clears a user stop)
#   switch-instance.bat stop beta         stop a build for real, watchdog included
#   switch-instance.bat switch beta       start beta, wait until healthy, stop the rest
#
# This exists because a stopped instance cannot serve its own dashboard: there
# has to be one entry point that works while nothing is running.
param(
    [Parameter(Position = 0)][string]$Action = 'status',
    [Parameter(Position = 1)][string]$Target = ''
)
$ErrorActionPreference = 'Continue'

$Base = Split-Path -Parent $PSScriptRoot
$Cfg = Join-Path $env:USERPROFILE '.config\opencode'

function Get-Builds {
    Get-ChildItem $Base -Directory -ErrorAction SilentlyContinue |
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

function Resolve-Build([string]$name) {
    $all = @(Get-Builds)
    if (-not $name) { return $all }
    # Aliases keep the everyday commands short: live is the production build,
    # beta is whatever carries the beta suffix right now.
    $alias = @{ live = '1.0.0'; prod = '1.0.0'; beta = 'beta'; test = 'beta' }
    $needle = $name
    if ($alias.ContainsKey($name.ToLower())) { $needle = $alias[$name.ToLower()] }
    $hit = @($all | Where-Object {
        $_.Version -eq $needle -or
        $_.Version -like ('*' + $needle + '*') -or
        (Split-Path $_.Dir -Leaf) -like ('*' + $needle + '*')
    })
    if (-not $hit.Count) {
        throw "no build matching '$name'; have: $((@($all | ForEach-Object { $_.Version })) -join ', ')"
    }
    return $hit
}

function Get-Port($build) {
    $f = Join-Path $build.RunDir 'active-port.txt'
    if (Test-Path $f) {
        $p = (Get-Content $f -Raw -ErrorAction SilentlyContinue)
        if ($p -and $p.Trim() -match '^\d+$') { return [int]$p.Trim() }
    }
    return $null
}

function Probe($build) {
    $p = Get-Port $build
    if (-not $p) { return $null }
    try {
        $h = Invoke-RestMethod ("http://127.0.0.1:$p/__health") -TimeoutSec 2
        if ($h.ok) {
            return [pscustomobject]@{
                Port = $p; Version = $h.version; Pid = $h.pid
                Uptime = [math]::Round($h.uptime_s, 0); Paused = $h.paused
            }
        }
    } catch { }
    return $null
}

function Start-Build($build) {
    $marker = Join-Path $build.RunDir 'stopped.by-user'
    if (Test-Path $marker) { Remove-Item $marker -Force -ErrorAction SilentlyContinue }
    # Do not wait: the script hands off to pythonw and the caller polls health.
    Start-Process -FilePath $build.Bat -ArgumentList '--force' -WindowStyle Hidden
}

function Stop-Build($build) {
    $h = Probe $build
    if ($h) {
        try {
            Invoke-RestMethod ("http://127.0.0.1:$($h.Port)/__control") -Method Post `
                -ContentType 'application/json' -Body '{"action":"stop"}' -TimeoutSec 5 | Out-Null
        } catch { }
    }
    Start-Sleep -Milliseconds 1200
    # Fallback for a build that predates the stop action: kill by runtime dir.
    $pat = [regex]::Escape($build.RunDir)
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $pat } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $marker = Join-Path $build.RunDir 'stopped.by-user'
    if (-not (Test-Path $marker)) {
        New-Item -ItemType Directory -Force $build.RunDir | Out-Null
        $stamp = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
        Set-Content -Path $marker -Value ('{"at":"' + $stamp + '","by":"switch-instance"}') -Encoding utf8
    }
}

function Show-Status {
    $rows = @()
    foreach ($b in (Get-Builds)) {
        $h = Probe $b
        $rows += [pscustomobject]@{
            Version = $b.Version
            State   = if ($h) { 'UP' } else { 'stopped' }
            Port    = if ($h) { $h.Port } else { '-' }
            Pid     = if ($h) { $h.Pid } else { '-' }
            Uptime  = if ($h) { "$($h.Uptime)s" } else { '-' }
            Marker  = if (Test-Path (Join-Path $b.RunDir 'stopped.by-user')) { 'user-stop' } else { '' }
            RunDir  = $b.RunDir
        }
    }
    $rows | Format-Table -AutoSize
}

try {
    switch ($Action.ToLower()) {
        'status' {
            Show-Status
        }
        'start' {
            $b = @(Resolve-Build $Target)
            foreach ($x in $b) {
                Start-Build $x
                $h = $null
                for ($i = 0; $i -lt 40; $i++) {
                    Start-Sleep -Milliseconds 500
                    $h = Probe $x
                    if ($h) { break }
                }
                if ($h) { Write-Host "[ok] $($x.Version) up on $($h.Port) pid $($h.Pid)" }
                else { Write-Host "[!!] $($x.Version) did not answer"; exit 1 }
            }
        }
        'stop' {
            foreach ($x in @(Resolve-Build $Target)) {
                Stop-Build $x
                Write-Host "[ok] $($x.Version) stopped (marker written, watchdog will not revive it)"
            }
        }
        'switch' {
            if (-not $Target) { throw 'switch needs a target build, e.g. switch-instance.bat switch beta' }
            $b = @(Resolve-Build $Target)[0]
            Write-Host "starting $($b.Version)"
            Start-Build $b
            $h = $null
            for ($i = 0; $i -lt 50; $i++) {
                Start-Sleep -Milliseconds 500
                $h = Probe $b
                if ($h) { break }
            }
            if (-not $h) {
                Write-Host "[!!] $($b.Version) did not come up; nothing was stopped"
                exit 1
            }
            Write-Host "[ok] $($b.Version) healthy on $($h.Port)"
            foreach ($x in @(Get-Builds | Where-Object { $_.Version -ne $b.Version })) {
                Stop-Build $x
                Write-Host "[ok] $($x.Version) stopped"
            }
        }
        default {
            Write-Host 'usage: switch-instance.bat status|start|stop|switch [version]'
            Write-Host '       versions: live (1.0.0) / beta (1.1.0-beta.1)'
            exit 2
        }
    }
} catch {
    Write-Host "[!!] $($_.Exception.Message)"
    exit 1
}
