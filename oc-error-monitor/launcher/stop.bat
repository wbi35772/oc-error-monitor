@echo off
rem Stop one build of oc-error-monitor and show what is left running.
rem Usage: stop.bat [version]
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
