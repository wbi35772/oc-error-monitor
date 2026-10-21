@echo off
setlocal
cd /d "%~dp0"
python monitor.py --follow
pause
