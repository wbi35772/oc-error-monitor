@echo off
rem Start one build of oc-error-monitor and print a status panel.
rem Usage: launch.bat [version]
rem With no argument every build is started.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
