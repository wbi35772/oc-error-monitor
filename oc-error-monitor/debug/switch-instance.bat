@echo off
rem Thin wrapper: the logic lives in switch-instance.ps1 next to it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0switch-instance.ps1" %*
