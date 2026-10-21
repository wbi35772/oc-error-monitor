@echo off
rem Start one build of oc-error-monitor and print a status panel.
rem Usage: launch.bat [version|status]
rem
rem The version is handed over through a file, not through the environment or
rem the command line: powershell.exe -File does not bind switch parameters,
rem and both the environment and %~1 can carry a trailing space that stops
rem the version from matching a directory name.
setlocal
set "HAND=%TEMP%\ocm-launch-%RANDOM%.txt"
> "%HAND%" echo %~1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" "%HAND%"
set "RC=%errorlevel%"
del /q "%HAND%" >nul 2>&1
exit /b %RC%
