@echo off
setlocal
cd /d "%~dp0"
python session_failover.py --follow --apply
pause
