# Start, stop or switch between the oc-error-monitor builds.
#
# Development only. The product builds do not switch each other: one build
# serves one instance, and start/stop lives in launcher/. This script exists so
# a build can be brought up and another taken down in one step while testing.
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
    # Aliases keep the everyday commands short. 'live' is resolved at runtime,
    # not pinned to a version number: whatever answers on 8788 is production.
    $alias = @{ beta = 'beta'; test = 'beta'; dev = 'dev' }
    if ($name.ToLower() -in @('live', 'prod')) {
        $lb = Get-LiveBuild
        if ($lb) { return @($all | Where-Object { $_.Version -eq $lb.Version }) }
        return @($all | Where-Object { $_.Version -notmatch '-' } |
            Sort-Object { Get-VKey $_.Version } | Select-Object -Last 1)
    }
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

function Get-VKey([string]$v) {
    # Same ordering the proxy uses: numeric core first, prerelease loses to the
    # release with the same core.
    $m = [regex]::Match($v, '^(\d+)\.(\d+)\.(\d+)')
    if (-not $m.Success) { return 0 }
    $n = [int]$m.Groups[1].Value * 1000000 + [int]$m.Groups[2].Value * 1000 + [int]$m.Groups[3].Value
    if ($v -match '-') { $n -= 1 }
    return $n
}

function Get-LiveBuild {
    try { $h = Invoke-RestMethod 'http://127.0.0.1:8788/__health' -TimeoutSec 2 } catch { return $null }
    if (-not $h) { return $null }
    foreach ($b in (Get-Builds)) { if ($b.Version -eq $h.version) { return $b } }
    return [pscustomobject]@{ Version = $h.version; RunDir = $null; Bat = $null }
}

function Show-Toast([string]$title, [string]$text) {
    # A double click has no console to read, so the outcome has to surface.
    try {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        Add-Type -AssemblyName System.Drawing | Out-Null
        $ni = New-Object System.Windows.Forms.NotifyIcon
        $ni.Icon = [System.Drawing.SystemIcons]::Information
        $ni.Visible = $true
        $ni.ShowBalloonTip(4000, $title, $text, [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Milliseconds 4200
        $ni.Dispose()
    } catch { }
}

function Point-Client-Fallback([int]$port) {
    # Fallback for a build without the point_client action (v1.1.0). Without
    # it, a switch would leave the client pointing at an instance that is
    # being stopped - the one failure mode the client cannot recover from.
    #
    # The rewrite is the same one the proxy does: only the local api
    # overrides change, and the file has to stay valid JSON.
    $cfg = Join-Path $env:USERPROFILE '.config\opencode\opencode.json'
    if (-not (Test-Path $cfg)) { Write-Host "[!!] no client config: $cfg"; return $false }
    try {
        $bytes = [IO.File]::ReadAllBytes($cfg)
        $bom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
        $raw = [Text.Encoding]::UTF8.GetString($bytes)
        if ($bom) { $raw = $raw.TrimStart([char]0xFEFF) }
        $new = [regex]::Replace(
            $raw,
            '("api"\s*:\s*")http://127\.0\.0\.1:\d+/v1(")',
            ('${1}http://127.0.0.1:' + $port + '/v1${2}'))
        if ($new -eq $raw) {
            Write-Host "[ok] client has no local api override; nothing to rewrite"
            return $true
        }
        $null = $new | ConvertFrom-Json    # 不合法就不写
        [IO.File]::WriteAllText($cfg, $new, (New-Object Text.UTF8Encoding($bom)))
        # 状态文件也得跟着更新，否则面板会显示一个过期指向
        $state = Join-Path $env:USERPROFILE '.config\opencode\retry-proxy-target.json'
        $obj = [ordered]@{
            base_url   = "http://127.0.0.1:$port/v1"
            port       = $port
            version    = 'unknown'
            reason     = 'switch-instance fallback'
            updated_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
        }
        [IO.File]::WriteAllText($state, ($obj | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        Write-Host "[ok] client now points at http://127.0.0.1:$port/v1 (fallback rewrite)"
        return $true
    } catch {
        Write-Host "[!!] fallback rewrite failed: $($_.Exception.Message)"
        return $false
    }
}

function Point-Client([int]$port) {
    # The client config holds one api override per model and opencode honours
    # no other form, so the port has to be written into all of them. The
    # instance does that: it knows its own port, and nobody here has to know
    # a port number.
    try {
        $r = Invoke-RestMethod ("http://127.0.0.1:$port/__control") -Method Post `
            -ContentType 'application/json' -Body '{"action":"point_client"}' -TimeoutSec 8
        Write-Host ("[ok] client now points at http://127.0.0.1:{0}/v1 ({1} overrides)" -f $port, $r.overrides)
        return $true
    } catch {
        # A build without the action cannot move the client, so this script
        # does it instead. The alternative is a client pointing at an
        # instance that is being stopped.
        Write-Host "[..] this build has no point_client; the switcher rewrites it"
        return (Point-Client-Fallback $port)
    }
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
            # The target runs on its own port, so the client has to follow it.
            Point-Client $h.Port | Out-Null
            foreach ($x in @(Get-Builds | Where-Object { $_.Version -ne $b.Version })) {
                Stop-Build $x
                Write-Host "[ok] $($x.Version) stopped"
            }
        }
        'toggle' {
            # Double click path: flip production between the build on 8788 and
            # the newest other build (a dev build, normally).
            $live = Get-LiveBuild
            $all = @(Get-Builds)
            $cands = @($all | Where-Object { -not $live -or $_.Version -ne $live.Version })
            if (-not $cands.Count) { throw 'only one build under oc-error-monitor/' }
            $target = $cands | Sort-Object { Get-VKey $_.Version } | Select-Object -Last 1
            $prev = $all | Where-Object { $live -and $_.Version -eq $live.Version } | Select-Object -First 1

            Write-Host ("now: {0} -> target: {1}" -f $(if ($live) { $live.Version } else { 'none' }), $target.Version)
            foreach ($b in $all) { Stop-Build $b }
            Start-Sleep -Milliseconds 600

            # The switch moves production, so the target takes 8788 even if its
            # own default port list starts lower.
            $env:RETRY_PROXY_PORTS = '8788,8899,8900,8901,8902'
            $marker = Join-Path $target.RunDir 'stopped.by-user'
            if (Test-Path $marker) { Remove-Item $marker -Force -ErrorAction SilentlyContinue }
            Start-Process -FilePath $target.Bat -ArgumentList '--force' -WindowStyle Hidden

            $h = $null
            for ($i = 0; $i -lt 50; $i++) {
                Start-Sleep -Milliseconds 500
                try { $h = Invoke-RestMethod 'http://127.0.0.1:8788/__health' -TimeoutSec 2 } catch { $h = $null }
                if ($h -and $h.version -eq $target.Version) { break }
            }
            if ($h -and $h.version -eq $target.Version) {
                Write-Host "[ok] $($target.Version) now on 8788 pid $($h.pid)"
                # The port did not move, so the client usually does not have to.
                # Pointing it anyway keeps the state file honest about which
                # instance is serving.
                Point-Client 8788 | Out-Null
                Show-Toast "生产端已切换到 $($target.Version)" "8788 由 $($target.Version) 接管（pid $($h.pid)）"
            } else {
                Write-Host "[!!] $($target.Version) did not take 8788; rolling back"
                if ($prev) {
                    Remove-Item Env:\RETRY_PROXY_PORTS -ErrorAction SilentlyContinue
                    $m2 = Join-Path $prev.RunDir 'stopped.by-user'
                    if (Test-Path $m2) { Remove-Item $m2 -Force -ErrorAction SilentlyContinue }
                    Start-Process -FilePath $prev.Bat -ArgumentList '--force' -WindowStyle Hidden
                    for ($i = 0; $i -lt 40; $i++) {
                        Start-Sleep -Milliseconds 500
                        try { $h2 = Invoke-RestMethod 'http://127.0.0.1:8788/__health' -TimeoutSec 2 } catch { $h2 = $null }
                        if ($h2) { break }
                    }
                }
                Show-Toast '切换失败，已回滚' '详情：switch-instance.bat status'
                exit 1
            }
        }
        default {
            Write-Host 'usage: switch-instance.bat status|start|stop|switch|toggle [version]'
            Write-Host '       versions: live (1.1.0) / dev (1.2.0-dev)'
            exit 2
        }
    }
} catch {
    Write-Host "[!!] $($_.Exception.Message)"
    exit 1
}
