@echo off
rem Start (or keep) the OpenCode free-tier retry proxy and its supervisor.
rem Idempotent: safe to call every minute from a scheduled task.
rem
rem Chain: this script -> supervisor.py -> retry_proxy.py
rem The supervisor respawns the proxy within ~1s if it crashes, so the minute
rem task is only the last-resort net (it also revives the supervisor itself).
setlocal
set "PORT=8787"
set "RUNDIR=%USERPROFILE%\.config\opencode\resilience"

if not exist "%RUNDIR%" mkdir "%RUNDIR%" 2>nul
copy /y "%~dp0retry_proxy.py" "%RUNDIR%\retry_proxy.py" >nul 2>&1
copy /y "%~dp0supervisor.py" "%RUNDIR%\supervisor.py" >nul 2>&1
if not exist "%RUNDIR%\supervisor.py" (
    echo  [!!] cannot stage files into "%RUNDIR%"
    exit /b 1
)

rem Already serving? Then there is nothing to do. A raw TCP connect is used on
rem purpose: it never consults a system proxy.
powershell -NoProfile -Command "try{$c=New-Object System.Net.Sockets.TcpClient; $c.Connect('127.0.0.1',%PORT%); $c.Close(); exit 0}catch{exit 1}"
if not errorlevel 1 (
    echo  [ok] retry proxy already up: http://127.0.0.1:%PORT%/__stats
    exit /b 0
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

echo  [..] starting retry proxy supervisor on 127.0.0.1:%PORT%
rem The child must not keep the caller's stdout pipe open, or a parent that
rem pipes this script's output waits forever for the pipe to close.
start "" "%PYW%" "%RUNDIR%\supervisor.py" > "%RUNDIR%\supervisor.out.log" 2>&1

rem Wait for the port, up to ~15s.
set /a TRIES=0
:wait
set /a TRIES+=1
powershell -NoProfile -Command "try{$c=New-Object System.Net.Sockets.TcpClient; $c.Connect('127.0.0.1',%PORT%); $c.Close(); exit 0}catch{exit 1}"
if not errorlevel 1 (
    echo  [ok] retry proxy is up: http://127.0.0.1:%PORT%/__stats
    exit /b 0
)
if %TRIES% GEQ 15 (
    echo  [!!] retry proxy did not come up - see "%RUNDIR%\supervisor.log"
    exit /b 1
)
powershell -NoProfile -Command "Start-Sleep -Milliseconds 800" >nul 2>&1
goto wait
