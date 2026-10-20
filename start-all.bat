@echo off
setlocal
title Start All - Windows Network Monitors
cd /d "%~dp0"

for /f "delims=" %%A in ('powershell -NoProfile -Command "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"') do set "ISADMIN=%%A"
if /i not "%ISADMIN%"=="True" (
    echo Requesting administrator privileges for all monitors...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo  Starting all available monitors in separate windows...
echo.

if exist "wirewatch\start.bat" (
    start "WireWatch" "%~dp0wirewatch\start.bat"
) else (
    echo  [skip] wirewatch\start.bat not found
)

if exist "simple-netmon\start.bat" (
    start "Simple NetMon" "%~dp0simple-netmon\start.bat"
) else (
    echo  [skip] simple-netmon\start.bat not found
)

if exist "openprocmon\start.bat" (
    start "OpenProcMon" "%~dp0openprocmon\start.bat"
) else (
    echo  [skip] openprocmon\start.bat not found
)

set "SI="
if exist "systeminformer\bin\Release64\SystemInformer.exe" set "SI=systeminformer\bin\Release64\SystemInformer.exe"
if not defined SI if exist "systeminformer\bin\ReleaseARM64\SystemInformer.exe" set "SI=systeminformer\bin\ReleaseARM64\SystemInformer.exe"
if not defined SI if exist "systeminformer\bin\Release32\SystemInformer.exe" set "SI=systeminformer\bin\Release32\SystemInformer.exe"
if not defined SI if exist "systeminformer\bin\Debug64\SystemInformer.exe" set "SI=systeminformer\bin\Debug64\SystemInformer.exe"
if not defined SI if exist "systeminformer\dist" for /d %%D in ("systeminformer\dist\*") do if not defined SI if exist "%%~D\amd64\SystemInformer.exe" set "SI=%%~D\amd64\SystemInformer.exe"
if not defined SI if exist "systeminformer\dist" for /d %%D in ("systeminformer\dist\*") do if not defined SI if exist "%%~D\arm64\SystemInformer.exe" set "SI=%%~D\arm64\SystemInformer.exe"
if not defined SI if exist "systeminformer\dist" for /d %%D in ("systeminformer\dist\*") do if not defined SI if exist "%%~D\i386\SystemInformer.exe" set "SI=%%~D\i386\SystemInformer.exe"

if defined SI (
    start "System Informer" "%~dp0systeminformer\start.bat"
) else (
    echo  [skip] System Informer is neither built nor extracted ^(systeminformer\bin\... or systeminformer\dist\...^)
)

echo.
echo  WireWatch      http://127.0.0.1:8080
echo  Simple NetMon  http://127.0.0.1:8888
echo.

rem The OpenCode free models are routed through the P4 retry proxy on 8787.
rem Trigger the scheduled task so the proxy keeps running with the normal user
rem token instead of being started elevated from this script.
schtasks /Query /TN "OpenCode Retry Proxy" >nul 2>&1
if not errorlevel 1 (
    schtasks /Run /TN "OpenCode Retry Proxy" >nul 2>&1
    echo  [ok]   OpenCode retry proxy task triggered ^(127.0.0.1:8787^)
) else (
    echo  [skip] OpenCode Retry Proxy task not installed - run tools\oc-error-monitor\install-watchdog.bat
)

rem This script is elevated, so it can add the logon trigger that a normal
rem user cannot create: the proxy comes back right after a reboot.
if exist "%USERPROFILE%\.config\opencode\resilience\run-hidden.vbs" (
    schtasks /Create /TN "OpenCode Retry Proxy (logon)" /TR "wscript.exe //B //Nologo %USERPROFILE%\.config\opencode\resilience\run-hidden.vbs" /SC ONLOGON /F >nul 2>&1
    if not errorlevel 1 (
        echo  [ok]   OpenCode retry proxy logon trigger installed
    ) else (
        echo  [skip] OpenCode retry proxy logon trigger could not be created
    )
)

echo.
echo  Note: each tool opens its own window. Close a window to stop that tool.
timeout /t 6 >nul
endlocal
