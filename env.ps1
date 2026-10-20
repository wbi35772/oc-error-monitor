# Local toolchains for this workspace (no machine-wide changes).
# Usage:  . .\env.ps1        (dot-source it in the current PowerShell session)

$root = $PSScriptRoot

$env:GOROOT = Join-Path $root '.tools\go'
$env:CARGO_HOME = Join-Path $root '.tools\cargo'
$env:RUSTUP_HOME = Join-Path $root '.tools\rustup'

$paths = @(
    (Join-Path $root '.tools\go\bin'),
    (Join-Path $root '.tools\cargo\bin')
)
$env:PATH = ($paths -join ';') + ';' + $env:PATH

# China-friendly module mirrors
$env:GOPROXY = 'https://goproxy.cn,direct'
$env:GOTOOLCHAIN = 'local'
$env:RUSTUP_DIST_SERVER = 'https://mirrors.tuna.tsinghua.edu.cn/rustup'
$env:RUSTUP_UPDATE_ROOT = 'https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup'

Write-Host "Go:    $(& (Join-Path $root '.tools\go\bin\go.exe') version 2>&1)"
Write-Host "Cargo: $(& (Join-Path $root '.tools\cargo\bin\cargo.exe') --version 2>&1)"
