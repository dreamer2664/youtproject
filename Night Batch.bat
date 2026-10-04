@echo off
REM ===========================================================================
REM  Night Batch.bat - the night shift. Double-click, or let Task Scheduler
REM  run it at night: clip the sheet queue -> generate -> park on Telegram ->
REM  report to Telegram. Re-runnable: same-day re-runs skip finished steps.
REM  Nothing in here can post - uploads stay manual, by design.
REM
REM  Schedule it once (paste in a terminal, adjust the time):
REM    schtasks /Create /TN "youtproject night batch" /TR "\"%~f0\"" /SC DAILY /ST 01:30 /F
REM  Then in Task Scheduler tick "Wake the computer to run this task".
REM
REM  Tune it by editing the NIGHTBATCH line below, e.g.
REM    main.py nightbatch --clips 3 --count 1 --seconds 45 --image-provider stock
REM ===========================================================================
setlocal
cd /d "%~dp0"
if not exist "work\nightbatch" mkdir "work\nightbatch"

set "PY="
if exist "venv\Scripts\python.exe"  set "PY=venv\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (where py     >nul 2>&1 && set "PY=py -3")
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
  echo.
  echo   No Python found. Install Python 3.11+ ^(python.org, "Add to PATH"^),
  echo   then run:  pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

%PY% -u main.py nightbatch %* >> "work\nightbatch\console.log" 2>&1
exit /b %errorlevel%
