@echo off
rem Install a per-minute watchdog for the retry proxy, plus a hidden launcher.
rem The proxy itself is idempotent (health check + single-instance guard), so the
rem watchdog only has to call the same start script. Uninstall: uninstall-watchdog.bat
setlocal
for /f "usebackq delims=" %%v in ("%~dp0VERSION") do set "VER=%%v"
rem One task per version: a shared name let a test build overwrite the
rem production watchdog, which then kept restarting the wrong instance.
set "TASK=OpenCode Retry Proxy v%VER%"
set "RUNDIR=%USERPROFILE%\.config\opencode\oc-error-monitor-v%VER%"

if not exist "%RUNDIR%" mkdir "%RUNDIR%" 2>nul
copy /y "%~dp0retry_proxy.py" "%RUNDIR%\retry_proxy.py" >nul 2>&1
copy /y "%~dp0supervisor.py" "%RUNDIR%\supervisor.py" >nul 2>&1
copy /y "%~dp0start-retry-proxy.bat" "%RUNDIR%\start-retry-proxy.bat" >nul 2>&1
copy /y "%~dp0VERSION" "%RUNDIR%\VERSION" >nul 2>&1
copy /y "%~dp0restart-retry-proxy.ps1" "%RUNDIR%\restart-retry-proxy.ps1" >nul 2>&1
copy /y "%~dp0ui.html" "%RUNDIR%\ui.html" >nul 2>&1
copy /y "%~dp0gateway.py" "%RUNDIR%\gateway.py" >nul 2>&1
copy /y "%~dp0start-gateway.bat" "%RUNDIR%\start-gateway.bat" >nul 2>&1
if not exist "%RUNDIR%\retry_proxy.py" (
    echo  [!!] cannot stage files into "%RUNDIR%"
    exit /b 1
)

rem Windowless launcher: schtasks would otherwise flash a console every minute.
> "%RUNDIR%\run-hidden.vbs" echo CreateObject("WScript.Shell").Run "cmd /c ""%RUNDIR%\start-retry-proxy.bat""", 0, False

schtasks /Create /TN "%TASK%" /TR "wscript.exe //B //Nologo %RUNDIR%\run-hidden.vbs" /SC MINUTE /MO 1 /F >nul 2>&1
if errorlevel 1 (
    echo  [!!] schtasks failed
    exit /b 1
)

rem Second net for the reboot case: the minute task only runs while the
rem machine is up, so a logon trigger brings the proxy back immediately.
rem ONLOGON tasks need elevation, so this usually only works from start-all.bat.
set "TASK_LOGON=%TASK% (logon)"
schtasks /Create /TN "%TASK_LOGON%" /TR "wscript.exe //B //Nologo %RUNDIR%\run-hidden.vbs" /SC ONLOGON /F >nul 2>&1
if errorlevel 1 (
    echo  [note] logon trigger needs admin rights - start-all.bat creates it
) else (
    echo  [ok] logon trigger installed: "%TASK_LOGON%"
)

schtasks /Run /TN "%TASK%" >nul 2>&1
echo  [ok] watchdog installed: "%TASK%" (every 1 minute, hidden)
echo  [ok] run now:   schtasks /Run /TN "%TASK%"
echo  [ok] remove:    schtasks /Delete /TN "%TASK%" /F  ^&  schtasks /Delete /TN "%TASK_LOGON%" /F

rem The gateway is its own process so it keeps answering while the proxy is
rem restarted. Same minute-plus-logon pair; the extra copy steps aside instead
rem of taking a new port, so the per-minute call is free.
set "GTASK=OpenCode Gateway v%VER%"
> "%RUNDIR%\run-gateway-hidden.vbs" echo CreateObject("WScript.Shell").Run "cmd /c ""%RUNDIR%\start-gateway.bat""", 0, False
schtasks /Create /TN "%GTASK%" /TR "wscript.exe //B //Nologo %RUNDIR%\run-gateway-hidden.vbs" /SC MINUTE /MO 1 /F >nul 2>&1
if errorlevel 1 (
    echo  [!!] gateway task failed
    exit /b 1
)
set "GTASK_LOGON=%GTASK% (logon)"
schtasks /Create /TN "%GTASK_LOGON%" /TR "wscript.exe //B //Nologo %RUNDIR%\run-gateway-hidden.vbs" /SC ONLOGON /F >nul 2>&1
if errorlevel 1 (
    echo  [note] gateway logon trigger needs admin rights - start-all.bat creates it
) else (
    echo  [ok] gateway logon trigger installed: "%GTASK_LOGON%"
)
schtasks /Run /TN "%GTASK%" >nul 2>&1
echo  [ok] gateway watchdog: "%GTASK%"
echo  [ok] remove:    schtasks /Delete /TN "%GTASK%" /F  ^&  schtasks /Delete /TN "%GTASK_LOGON%" /F
