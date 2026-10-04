@echo off
REM ===========================================================================
REM  Start Bot.bat - the Telegram bot, listening for /go.
REM
REM  Double-click once (or make it start with Windows: press Win+R, type
REM    shell:startup  and drop a shortcut to this file there).
REM  It runs hidden in the background; close it via Task Manager
REM  (pythonw.exe) or reboot.
REM
REM  While this is running, /go on your phone starts the whole night shift
REM  right away. If the PC is off, the message waits and the RUN happens at
REM  the next wake/boot - see "Wake and Run.bat".
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PY="
if exist "venv\Scripts\pythonw.exe"  set "PY=venv\Scripts\pythonw.exe"
if not defined PY if exist ".venv\Scripts\pythonw.exe" set "PY=.venv\Scripts\pythonw.exe"
if not defined PY (where pythonw >nul 2>&1 && set "PY=pythonw")
if not defined PY (where python  >nul 2>&1 && set "PY=python")
if not defined PY (
  echo   No Python found - install Python 3.11+ from python.org first.
  pause
  exit /b 1
)

start "" /min %PY% main.py bot
endlocal
