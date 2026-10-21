# Runs the doc tooling against the live free tier without touching the
# running setup.
#
#   - its own OpenAI-compatible endpoint on port 8791 (the production proxy
#     stays on 8788 and is never restarted or rewired)
#   - its own scratch project copy under .\scratch
#   - env var configuration only, so ~/.readmex/config.json is left alone
#   - a before/after snapshot of the live system, so "the running version was
#     not affected" is measured
#
# Usage:  powershell -ExecutionPolicy Bypass -File run_readmex_test.ps1
param(
    [string]$Project = "",
    [string]$Model = "opencode/deepseek-v4.1-flash-free",
    [int]$Port = 8891,
    [switch]$KeepScratch
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $Here "..\..\..\..")).Path
$Py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"

if (-not $Project) { $Project = Join-Path $Here "..\.." }

$Scratch = Join-Path $Here "scratch"
if (Test-Path $Scratch) { Remove-Item $Scratch -Recurse -Force }
New-Item -ItemType Directory -Force $Scratch | Out-Null

# A small, self-contained copy: a doc generator is pointed at a folder, and
# the folder must not be the live one.
Copy-Item (Join-Path $Project "README.md") (Join-Path $Scratch "README.md") -Force
Get-ChildItem $Project -Filter *.py -File | Select-Object -First 3 | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $Scratch $_.Name) -Force
}
if (Test-Path (Join-Path $Project "ui.html")) {
    Copy-Item (Join-Path $Project "ui.html") (Join-Path $Scratch "ui.html") -Force
}
Push-Location $Scratch
$ErrorActionPreference = "Continue"
git init -q 2>&1 | Out-Null
git remote add origin "https://github.com/opencode-network/opencode-resilience.git" 2>&1 | Out-Null
$ErrorActionPreference = "Stop"
Pop-Location
Write-Host "[1/5] scratch project: $Scratch"

# Baseline before the experiment.
& $Py (Join-Path $Here "snapshot_state.py") --out (Join-Path $Here "before.json") | Out-Null
Write-Host "[2/5] live state snapshotted"

# The endpoint for this experiment, on its own port.
$shim = Join-Path $Here "..\..\cli_llm_server.py"
$env:CLI_LLM_CONCURRENCY = "2"
Start-Process $Py -ArgumentList $shim, "--port", $Port -WindowStyle Hidden `
    -RedirectStandardOutput "$env:TEMP\clillm-$Port.log" `
    -RedirectStandardError  "$env:TEMP\clillm-$Port.err"
Start-Sleep -Seconds 2
$health = Invoke-RestMethod "http://127.0.0.1:$Port/__health"
Write-Host "[3/5] cli llm server up on $Port (cli: $($health.cli))"

# readmeX configuration lives in env vars only.
$env:PYTHONPATH = Join-Path $Root "readmeX\src"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:LLM_API_KEY = "public"
$env:LLM_BASE_URL = "http://127.0.0.1:$Port/v1"
$env:LLM_MODEL_NAME = $Model
$env:T2I_API_KEY = "public"
$env:T2I_BASE_URL = "http://127.0.0.1:$Port/v1"
$env:T2I_MODEL_NAME = $Model
$env:EMBEDDING_API_KEY = "public"
$env:EMBEDDING_BASE_URL = "http://127.0.0.1:$Port/v1"
$env:EMBEDDING_MODEL_NAME = $Model
$env:LOCAL_EMBEDDING = "false"
$env:MAX_WORKERS = "2"
$env:GITHUB_USERNAME = "opencode-network"

$answers = @("", "", "", "", "en", "", "", "", "", "", "", "")
Write-Host "[4/5] running readmeX (model: $Model)"
$sw = [Diagnostics.Stopwatch]::StartNew()
($answers -join "`n") | & $Py -m readmex $Scratch *> (Join-Path $Here "readmex.log")
$code = $LASTEXITCODE
$sw.Stop()
Write-Host ("      exit={0} elapsed={1}s" -f $code, [int]$sw.Elapsed.TotalSeconds)

# Snapshot after, and diff against the baseline.
& $Py (Join-Path $Here "snapshot_state.py") --out (Join-Path $Here "after.json") | Out-Null
& $Py (Join-Path $Here "snapshot_state.py") --diff (Join-Path $Here "before.json")
$verdict = $LASTEXITCODE

# Stop the experimental endpoint. The production proxy on 8788 is untouched.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*cli_llm_server.py*$Port*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "[5/5] experimental endpoint stopped, production proxy untouched"

Write-Host ""
Write-Host "artifacts:"
Write-Host "  log      : $(Join-Path $Here 'readmex.log')"
Write-Host "  output   : $(Join-Path $Scratch 'readmex_output')"
Get-ChildItem (Join-Path $Scratch "readmex_output") -Recurse -File -ErrorAction SilentlyContinue |
    ForEach-Object { "    {0}  ({1} bytes)" -f $_.Name, $_.Length }

if (-not $KeepScratch) { }
exit $verdict
