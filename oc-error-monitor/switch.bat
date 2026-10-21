@echo off
rem Stable entry point for the desktop shortcut: hand the action to the newest
rem build under oc-error-monitor\ so the shortcut survives version churn.
setlocal
set "BASE=%~dp0"
set "NEWEST="
for /f "delims=" %%D in ('dir /b /ad /o-n "%BASE%v*" 2^>nul') do if not defined NEWEST set "NEWEST=%BASE%%%D"
if not defined NEWEST (
    echo  [!!] no version directory under "%BASE%"
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%NEWEST%\switch-instance.ps1" %*
