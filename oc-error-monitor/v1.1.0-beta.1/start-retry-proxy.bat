@echo off
rem Start (or keep) the OpenCode free-tier retry proxy and its supervisor.
rem Idempotent: safe to call every minute from a scheduled task.
rem
rem Chain: this script -> supervisor.py -> retry_proxy.py
rem The supervisor respawns the proxy within ~1s if it crashes, so the minute
rem task is only the last-resort net (it also revives the supervisor itself).
rem
rem A healthy port is NOT a reason to skip the supervisor: a zero-downtime
rem handover leaves the new proxy unmanaged, and supervisor.py has its own lock
rem so calling it again is free.
setlocal
set "PORT=8899"
set "RUNDIR=%USERPROFILE%\.config\opencode\oc-error-monitor-v1.1.0-beta.1"

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

rem Already serving? A raw TCP connect is used on purpose: it never consults a
rem system proxy.
powershell -NoProfile -Command "try{$c=New-Object System.Net.Sockets.TcpClient; $c.Connect('127.0.0.1',%PORT%); $c.Close(); exit 0}catch{exit 1}"
if errorlevel 1 goto start

echo  [ok] retry proxy already up: http://127.0.0.1:%PORT%/__ui
rem Make sure somebody is watching it. The supervisor's lock makes this a no-op
rem when one is already alive.
start "" cmd /c ""%PYW%" "%RUNDIR%\supervisor.py" > "%RUNDIR%\supervisor.out.log" 2>&1"
exit /b 0

:start
echo  [..] starting retry proxy supervisor on 127.0.0.1:%PORT%
rem The child must not keep the caller's stdout pipe open, or a parent that
rem pipes this script's output waits forever for the pipe to close.
start "" cmd /c ""%PYW%" "%RUNDIR%\supervisor.py" > "%RUNDIR%\supervisor.out.log" 2>&1"

rem Wait for the port, up to ~15s.
set /a TRIES=0
:wait
set /a TRIES+=1
powershell -NoProfile -Command "try{$c=New-Object System.Net.Sockets.TcpClient; $c.Connect('127.0.0.1',%PORT%); $c.Close(); exit 0}catch{exit 1}"
if not errorlevel 1 (
    echo  [ok] retry proxy is up: http://127.0.0.1:%PORT%/__ui
    exit /b 0
)
if %TRIES% GEQ 15 (
    echo  [!!] retry proxy did not come up - see "%RUNDIR%\supervisor.log"
    exit /b 1
)
powershell -NoProfile -Command "Start-Sleep -Milliseconds 800" >nul 2>&1
goto wait
