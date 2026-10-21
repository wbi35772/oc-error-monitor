@echo off
rem Install a per-minute watchdog for the retry proxy, plus a hidden launcher.
rem The proxy itself is idempotent (health check + single-instance guard), so the
rem watchdog only has to call the same start script. Uninstall: uninstall-watchdog.bat
setlocal
set "TASK=OpenCode Retry Proxy"
set "RUNDIR=%USERPROFILE%\.config\opencode\oc-error-monitor-v1.0.0"

if not exist "%RUNDIR%" mkdir "%RUNDIR%" 2>nul
copy /y "%~dp0retry_proxy.py" "%RUNDIR%\retry_proxy.py" >nul 2>&1
copy /y "%~dp0supervisor.py" "%RUNDIR%\supervisor.py" >nul 2>&1
copy /y "%~dp0start-retry-proxy.bat" "%RUNDIR%\start-retry-proxy.bat" >nul 2>&1
copy /y "%~dp0VERSION" "%RUNDIR%\VERSION" >nul 2>&1
copy /y "%~dp0restart-retry-proxy.ps1" "%RUNDIR%\restart-retry-proxy.ps1" >nul 2>&1
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
