@echo off
REM ===========================================================================
REM  Start Panel.bat — double-click me.
REM
REM  Starts the panel server (hidden, no terminal window) and opens it as an
REM  app-style window in Edge or Chrome: no tabs, no address bar, no console.
REM  If the panel is already running, this just opens the window again.
REM
REM  Stop it: Task Manager -> end "pythonw.exe", or just reboot.
REM  Change the port in ONE place: the PORT line below + panel.py --port.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%/"

REM ---- already running? then just open the window --------------------------
powershell -NoProfile -Command "try{ $c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',%PORT%); $c.Close(); exit 0 }catch{ exit 1 }" >nul 2>&1
if %errorlevel%==0 goto open

REM ---- find a Python ------------------------------------------------------
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

REM ---- start the server (hidden) ------------------------------------------
start "" /min %PY% panel.py --no-browser

REM ---- wait for the port to answer (max ~20s) -----------------------------
powershell -NoProfile -Command "$d=[DateTime]::Now.AddSeconds(20); while([DateTime]::Now -lt $d){ try{ $c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',%PORT%); $c.Close(); exit 0 }catch{ Start-Sleep -Milliseconds 300 } }; exit 1" >nul 2>&1
if not %errorlevel%==0 (
  echo.
  echo   The panel did not come up in 20 seconds.
  echo   Run this once to see the error:  %PY% panel.py
  echo.
  pause
  exit /b 1
)

:open
REM ---- open an app-style window: Edge, then Chrome, then default browser ---
set "BROWSER="
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"          set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe"       set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe"       set "BROWSER=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if defined BROWSER (
  start "" "%BROWSER%" --app=%URL%
) else (
  start "" %URL%
)
endlocal
