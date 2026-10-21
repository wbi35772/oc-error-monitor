# Deploy one version from the repository to the runtime directory and make it live.
#
#   powershell -File deploy.ps1 1.1.0
#
# The repository holds every version under oc-error-monitor/<version>/; the
# runtime holds exactly one deployed copy per version at
# %USERPROFILE%\.config\opencode\oc-error-monitor-v<version>\.
#
# Two rules this script must never break, because a mistake here takes the
# live proxy down and every OpenCode session with it:
#
#   1. A deployment touches ONE version. It restarts that version's own
#      processes and nothing else. A test build going live must not stop the
#      production proxy.
#   2. The watchdog task is named per version. One shared task name meant
#      that deploying a test build re-pointed the production watchdog at the
#      test build, so production died silently a minute later.
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Version,
    [switch]$NoTask,
    [switch]$NoRestart
)
$ErrorActionPreference = 'Stop'

$Base = $PSScriptRoot
$Repo = Split-Path -Parent $Base
$Cfg = Join-Path $env:USERPROFILE '.config\opencode'
$Task = "OpenCode Retry Proxy v$Version"

$dir = @(Get-ChildItem $Base -Directory | Where-Object {
    $v = Join-Path $_.FullName 'VERSION'
    (Test-Path $v) -and ((Get-Content $v -Raw).Trim() -eq $Version)
})
if (-not $dir.Count) { throw "no build with VERSION=$Version under $Base" }
$dir = $dir[0]

$run = Join-Path $Cfg "oc-error-monitor-v$Version"
New-Item -ItemType Directory -Force $run | Out-Null

$stage = @(
    'retry_proxy.py', 'supervisor.py', 'ui.html', 'port_owner.py', 'wire_retry_proxy.py',
    'start-retry-proxy.bat', 'restart-retry-proxy.ps1',
    'VERSION'
)
$staged = 0
foreach ($f in $stage) {
    $src = Join-Path $dir.FullName $f
    if (Test-Path $src) { Copy-Item $src $run -Force; $staged++ }
}

# A runtime copy older than this one may still carry repo-path.txt from when the
# build looked up its siblings. Nothing reads it any more, and a stale file that
# implies the build reaches into the repository is misleading.
$stale = Join-Path $run 'repo-path.txt'
if (Test-Path $stale) { Remove-Item $stale -Force -ErrorAction SilentlyContinue }

if (-not $NoTask) {
    $bat = Join-Path $run 'start-retry-proxy.bat'
    $vbs = Join-Path $run 'run-hidden.vbs'
    $line = 'CreateObject("WScript.Shell").Run "cmd /c ""' + $bat + '""", 0, False'
    [System.IO.File]::WriteAllText($vbs, $line)
    schtasks /Create /TN $Task /TR "wscript.exe //B //Nologo $vbs" /SC MINUTE /MO 1 /F | Out-Null
    Write-Host "[ok] watchdog -> $run"
}

if (-not $NoRestart) {
    # Restart this version only. The processes are matched by their own
    # runtime directory, so another deployed version keeps serving: stopping
    # the production proxy to put a test build in place is exactly the kind of
    # surprise that breaks every open session.
    $target = [regex]::Escape($run)
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $target -and
            $_.CommandLine -match 'supervisor\.py|retry_proxy\.py'
        } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2

    Start-Process -FilePath (Join-Path $run 'start-retry-proxy.bat') -ArgumentList '--force' -WindowStyle Hidden

    $up = $null
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        $pf = Join-Path $run 'active-port.txt'
        if (-not (Test-Path $pf)) { continue }
        $p = (Get-Content $pf -Raw).Trim()
        if ($p -notmatch '^\d+$') { continue }
        try { $h = Invoke-RestMethod "http://127.0.0.1:$p/__health" -TimeoutSec 2 } catch { continue }
        if ($h.version -eq $Version) { $up = $h; $port = [int]$p; break }
    }
    if (-not $up) { throw "staged $Version but it did not answer with that version" }

    [pscustomobject]@{
        version     = $up.version
        port        = $port
        pid         = $up.pid
        deployed_at = (Get-Date -Format s)
        source      = $dir.FullName
        runtime     = $run
    } | ConvertTo-Json | Set-Content (Join-Path $run 'deployed.json') -Encoding utf8
    Write-Host "[ok] $Version live on $port pid $($up.pid) ($staged files staged)"
} else {
    Write-Host "[ok] $Version staged to $run ($staged files, not started)"
}
