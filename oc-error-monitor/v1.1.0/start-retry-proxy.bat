@echo off
rem Start (or keep) this build's retry proxy and its supervisor.
rem Idempotent: safe to call every minute from a scheduled task.
rem
rem Chain: this script -> supervisor.py -> retry_proxy.py
rem The supervisor respawns the proxy within ~1s if it crashes, so the minute
rem task is only the last-resort net (it also revives the supervisor itself).
rem
rem Two builds can run side by side (production on 8788, a dev build on 8899),
rem so "the port answers" is not the same as "this build is up": the check
rem below compares the version the port reports with this directory's VERSION.
setlocal
set "PORT=8788"
for /f "usebackq delims=" %%v in ("%~dp0VERSION") do set "VER=%%v"
set "RUNDIR=%USERPROFILE%\.config\opencode\oc-error-monitor-v%VER%"

if not exist "%RUNDIR%" mkdir "%RUNDIR%" 2>nul
rem --force is the explicit start: it clears a user stop. The minute task
rem calls this script without arguments and therefore respects the marker.
if /i "%~1"=="--force" (
    if exist "%RUNDIR%\stopped.by-user" del /q "%RUNDIR%\stopped.by-user" >nul 2>&1
)
if exist "%RUNDIR%\stopped.by-user" (
    echo  [stop] user stopped this instance; run with --force to restart
    exit /b 0
)
rem The peer lookup needs the repository root, which the runtime copy cannot
rem infer from its own location.
if exist "%~dp0..\..\oc-error-monitor" for %%I in ("%~dp0..\..") do > "%RUNDIR%\repo-path.txt" echo %%~fI
copy /y "%~dp0retry_proxy.py" "%RUNDIR%\retry_proxy.py" >nul 2>&1
copy /y "%~dp0supervisor.py" "%RUNDIR%\supervisor.py" >nul 2>&1
copy /y "%~dp0ui.html" "%RUNDIR%\ui.html" >nul 2>&1
copy /y "%~dp0VERSION" "%RUNDIR%\VERSION" >nul 2>&1
copy /y "%~dp0restart-retry-proxy.ps1" "%RUNDIR%\restart-retry-proxy.ps1" >nul 2>&1
if not exist "%RUNDIR%\supervisor.py" (
    echo  [!!] cannot stage files into "%RUNDIR%"
    exit /b 1
)

set "PYW="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not defined PYW for %%P in (pythonw.exe) do set "PYW=%%~$PATH:P"
if not defined PYW (
    echo  [!!] pythonw.exe not found
    exit /b 1
)

rem Already serving *this* build? The HTTP probe ignores a system proxy and the
rem version compare keeps this build from mistaking production for itself.
call :is_this_build_up
if not errorlevel 1 (
    echo  [ok] this build is already up: http://127.0.0.1:%PORT%/__ui
    rem Make sure somebody is watching it. The supervisor's lock makes this a
    rem no-op when one is already alive.
    start "" cmd /c ""%PYW%" "%RUNDIR%\supervisor.py" > "%RUNDIR%\supervisor.out.log" 2>&1"
    exit /b 0
)

echo  [..] starting retry proxy supervisor (build %VER%)
rem The child must not keep the caller's stdout pipe open, or a parent that
rem pipes this script's output waits forever for the pipe to close.
start "" cmd /c ""%PYW%" "%RUNDIR%\supervisor.py" > "%RUNDIR%\supervisor.out.log" 2>&1"

rem Wait for this build's health, up to ~20s. The supervisor may land on any
rem port of its list, so the port is read from active-port.txt, not assumed.
set /a TRIES=0
:wait
set /a TRIES+=1
call :is_this_build_up
if not errorlevel 1 (
    echo  [ok] %VER% is up on port %PORT%
    exit /b 0
)
if %TRIES% GEQ 20 (
    echo  [!!] %VER% did not come up - see "%RUNDIR%\supervisor.log"
    exit /b 1
)
powershell -NoProfile -Command "Start-Sleep -Milliseconds 800" >nul 2>&1
goto wait

:is_this_build_up
set "PNOW="
if exist "%RUNDIR%\active-port.txt" for /f "usebackq delims=" %%p in ("%RUNDIR%\active-port.txt") do set "PNOW=%%p"
if defined PNOW goto :probe_now
set "PNOW=%PORT%"
:probe_now
powershell -NoProfile -Command "try{$h=Invoke-RestMethod 'http://127.0.0.1:%PNOW%/__health' -TimeoutSec 2; if($h.version -eq '%VER%'){exit 0}}catch{}; exit 1"
if errorlevel 1 exit /b 1
set "PORT=%PNOW%"
exit /b 0
