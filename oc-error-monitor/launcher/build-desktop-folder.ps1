# Rebuild the desktop launcher folder from the repository.
#
# The folder is regenerated from whatever versions exist, so the names always
# say which version they start, and the shortcuts never point at a version
# that was removed.
param([switch]$Quiet)

$ErrorActionPreference = 'Continue'
$Repo = Split-Path -Parent $PSScriptRoot  # ...\oc-error-monitor
$Cfg = Join-Path $env:USERPROFILE '.config\opencode'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Folder = Join-Path $Desktop 'oc-error-monitor 启动器'
$Launcher = Join-Path $Repo 'launcher'

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

$builds = @(Get-Builds)
if (-not $builds.Count) { Write-Host '[!!] no build under oc-error-monitor/'; exit 1 }

# A fresh folder every time: leftovers from an older layout are the reason the
# desktop looked cluttered.
if (Test-Path $Folder) { Remove-Item $Folder -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force $Folder | Out-Null

$sh = New-Object -ComObject WScript.Shell

function New-Link {
    param(
        [string]$LinkName,
        [string]$Target,
        [string]$TargetArgs,
        [string]$Description,
        [int]$Icon
    )
    $path = Join-Path $Folder $LinkName
    $lnk = $sh.CreateShortcut($path)
    $lnk.TargetPath = $Target
    if ($TargetArgs) { $lnk.Arguments = $TargetArgs }
    $lnk.WorkingDirectory = Split-Path -Parent $Target
    $lnk.Description = $Description
    $lnk.IconLocation = "$env:WINDIR\System32\shell32.dll,$Icon"
    $lnk.Save()
    if (-not $Quiet) { Write-Host "  + $LinkName" }
}

$launchBat = Join-Path $Launcher 'launch.bat'
$stopBat = Join-Path $Launcher 'stop.bat'

# 1) one start and one stop per version, name carries the version
foreach ($b in $builds) {
    $role = if ($b.Version -notmatch '-') { '生产' } else { '测试' }
    New-Link -LinkName ("启动 {0} {1}.lnk" -f $role, $b.Version) `
        -Target $launchBat -TargetArgs ("`"$($b.Version)`"") `
        -Description ("启动 $role 版本 $($b.Version)") -Icon 44
    New-Link -LinkName ("停止 {0} {1}.lnk" -f $role, $b.Version) `
        -Target $stopBat -TargetArgs ("`"$($b.Version)`"") `
        -Description ("停止 $role 版本 $($b.Version)") -Icon 28
}

# 2) the production build gets the plain name too, it is the one used daily
$prod = $builds | Where-Object { $_.Version -notmatch '-' } | Select-Object -First 1
if ($prod) {
    New-Link -LinkName ("生产版本 {0}.lnk" -f $prod.Version) `
        -Target $launchBat -TargetArgs ("`"$($prod.Version)`"") `
        -Description ("启动生产版本 $($prod.Version)") -Icon 44
    New-Link -LinkName ("停止生产版本 {0}.lnk" -f $prod.Version) `
        -Target $stopBat -TargetArgs ("`"$($prod.Version)`"") `
        -Description ("停止生产版本 $($prod.Version)") -Icon 28
}

# 3) a window that only shows state
New-Link -LinkName '查看所有版本状态.lnk' `
    -Target $launchBat -TargetArgs 'status' `
    -Description '列出所有版本的运行状态' -Icon 109

# 4) a shortcut per running dashboard, so the UI is one click away
foreach ($b in $builds) {
    $pf = Join-Path $b.RunDir 'active-port.txt'
    if (-not (Test-Path $pf)) { continue }
    $p = (Get-Content $pf -Raw -ErrorAction SilentlyContinue)
    if (-not $p -or $p.Trim() -notmatch '^\d+$') { continue }
    $port = $p.Trim()
    $role = if ($b.Version -notmatch '-') { '生产' } else { '测试' }
    New-Link -LinkName ("打开仪表盘 {0} {1}.lnk" -f $role, $b.Version) `
        -Target "$env:WINDIR\explorer.exe" -TargetArgs ("http://127.0.0.1:$port/__ui") `
        -Description ("打开 $role 版本 $($b.Version) 的仪表盘") -Icon 21
}

if (-not $Quiet) {
    Write-Host ''
    Write-Host "desktop folder: $Folder"
    Get-ChildItem $Folder | ForEach-Object { "  " + $_.Name }
}
