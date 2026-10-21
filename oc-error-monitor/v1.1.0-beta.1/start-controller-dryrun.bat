@echo off
setlocal
cd /d "%~dp0"
python controller.py --follow
pause
