@echo off
rem Stop one build of oc-error-monitor and show what is left running.
rem Usage: stop.bat [version]
rem
rem The version is handed over through a file, not through the environment or
rem the command line: powershell.exe -File does not bind switch parameters,
rem and both the environment and %~1 can carry a trailing space that stops
rem the version from matching a directory name.
setlocal
set "HAND=%TEMP%\ocm-stop-%RANDOM%.txt"
> "%HAND%" echo %~1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" "%HAND%"
set "RC=%errorlevel%"
del /q "%HAND%" >nul 2>&1
exit /b %RC%
