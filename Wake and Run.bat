@echo off
REM ===========================================================================
REM  Wake and Run.bat - the overnight worker.
REM
REM  Schedule it ONCE; it then runs the /go you sent from your phone:
REM
REM    schtasks /Create /TN "youtproject overnight" /TR "\"%~f0\"" /SC DAILY /ST 01:00 /F
REM
REM  Then open Task Scheduler -> that task -> tick BOTH:
REM     [x] Wake the computer to run this task      (needs hibernate/sleep,
REM         not a full shutdown - see OPERATIONS.md "The phone pipeline")
REM     [x] Run task as soon as possible after a scheduled start is missed
REM         (covers a fully shut down PC: it runs at the next boot instead)
REM
REM  The run checks Telegram once for a /go sent while you slept, runs the
REM  clip lane + both boardroom sittings, messages the report, and puts the
REM  PC back to hibernate when nobody is at the keyboard.
REM  Nothing in here can post - uploads stay manual, by design.
REM ===========================================================================
setlocal
cd /d "%~dp0"
if not exist "work\nightrun" mkdir "work\nightrun"

set "PY="
if exist "venv\Scripts\python.exe"  set "PY=venv\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (where py     >nul 2>&1 && set "PY=py -3")
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY exit /b 1

%PY% -u main.py wakeup --sleep-after %* >> "work\nightrun\wake.log" 2>&1
exit /b %errorlevel%
