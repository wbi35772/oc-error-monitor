@echo off
rem Start the gateway windowless. Safe to call every minute: a second copy
rem probes the saved port, finds a live oc-gateway and exits 0, so the
rem watchdog never accumulates instances on fresh ports.
setlocal
set "RUNDIR=%~dp0"
set "PYW="
for /f "delims=" %%p in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%p"
if defined PYW (
    start "" "%PYW%" "%RUNDIR%gateway.py"
    exit /b 0
)
rem No pythonw on PATH: python in a hidden window is the fallback.
start "" /min python "%RUNDIR%gateway.py"
exit /b 0
