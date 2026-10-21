@echo off
rem Thin wrapper: powershell -File deploy.ps1 1.1.0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
