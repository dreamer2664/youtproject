@echo off
REM ===========================================================================
REM  Start Panel.bat - double-click me.
REM
REM  Opens THIS project's panel as an app-style window in Edge or Chrome:
REM  no tabs, no address bar, no terminal. If the panel is already running,
REM  this just re-opens its window.
REM
REM  All the logic lives in  panel.py --app-window , which:
REM    * reuses a running panel, identifying it by probing /api/state - a
REM      bare "the port answers" check once opened an OLDER project's window
REM      that also listens on 8765, so there is no bare port check anymore;
REM    * and opens the port it ACTUALLY bound (8765, else the next free one),
REM      so the window can never point at something else on a shared port.
REM
REM  Stop it: Task Manager -> end "pythonw.exe", or just reboot.
REM  If it ever fails, a dialog explains why; to watch it live:
REM      venv\Scripts\python.exe panel.py --app-window
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PY="
if exist "venv\Scripts\pythonw.exe"  set "PY=venv\Scripts\pythonw.exe"
if not defined PY if exist ".venv\Scripts\pythonw.exe" set "PY=.venv\Scripts\pythonw.exe"
if not defined PY if exist "venv\Scripts\python.exe"  set "PY=venv\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (where py      >nul 2>&1 && set "PY=py -3")
if not defined PY (where pythonw >nul 2>&1 && set "PY=pythonw")
if not defined PY (where python  >nul 2>&1 && set "PY=python")
if not defined PY (
  echo.
  echo   No Python found. Install Python 3.11+ ^(python.org, "Add to PATH"^),
  echo   then run:  pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

start "" /min %PY% panel.py --app-window
endlocal
