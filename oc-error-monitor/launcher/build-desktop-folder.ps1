# Rebuild the desktop launcher folder from the repository, so the shortcuts on
# the desktop always match the versions that actually exist.
$ErrorActionPreference = 'Continue'
$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)  # ...\oc-error-monitor
$Cfg = Join-Path $env:USERPROFILE '.config\opencode'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Folder = Join-Path $Desktop 'oc-error-monitor 启动器'

New-Item -ItemType Directory -Force $Folder | Out-Null

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
if (-not $builds.Count) { Write-Host '[!!] no build found'; exit 1 }

$sh = New-Object -ComObject WScript.Shell

function New-Link([string]$name, [string]$target, [string]$args, [string]$desc, [int]$icon) {
    $p = Join-Path $Folder $name
    $l = $sh.CreateShortcut($p)
    $l.TargetPath = $target
    if ($args) { $l.Arguments = $args }
    $l.WorkingDirectory = Split-Path -Parent $target
    $l.Description = $desc
    $l.IconLocation = "$env:WINDIR\System32\shell32.dll,$icon"
    $l.Save()
    Write-Host "  + $name"
}

# One launcher per version. The name carries the version so the desktop says
# what it starts.
foreach ($b in $builds) {
    $isProd = $b.Version -notmatch '-'
    $role = if ($isProd) { '生产' } else { '测试' }
    New-Link ("启动 {0} {1}.lnk" -f $role, $b.Version) `
        (Join-Path $Repo 'launcher\launch.bat') `
        ("`"$($b.Version)`"") `
        ("启动 oc-error-monitor $role版本 $($b.Version)") 44
}

# A stop shortcut per version too, because "stop" is what people reach for when
# something misbehaves.
$stopPs = Join-Path $Repo 'launcher\stop.ps1'
$stopBat = Join-Path $Repo 'launcher\stop.bat'
if (Test-Path $stopBat) {
    foreach ($b in $builds) {
        $isProd = $b.Version -notmatch '-'
        $role = if ($isProd) { '生产' } else { '测试' }
        New-Link ("停止 {0} {1}.lnk" -f $role, $b.Version) `
            $stopBat `
            ("`"$($b.Version)`"") `
            ("停止 oc-error-monitor $role版本 $($b.Version)") 28
    }
}

# A status window that lists everything.
New-Link '查看所有版本状态.lnk' (Join-Path $Repo 'launcher\launch.bat') 'status' '列出所有版本运行状态' 109

# The production one, named plainly, at the top of the list.
$prod = $builds | Where-Object { $_.Version -notmatch '-' } | Select-Object -First 1
if ($prod) {
    New-Link ("生产版本 {0}.lnk" -f $prod.Version) `
        (Join-Path $Repo 'launcher\launch.bat') `
        ("`"$($prod.Version)`"") `
        ("启动生产版本 $($prod.Version)") 44
}

Write-Host ''
Write-Host "desktop folder: $Folder"
Get-ChildItem $Folder | ForEach-Object { "  " + $_.Name }
