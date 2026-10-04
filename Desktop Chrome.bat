@echo off
REM ===========================================================================
REM  Desktop Chrome.bat - start a Chromium browser for the desktop agent.
REM
REM  ANY Chromium-based browser works: Opera GX, Edge, Chrome, Brave,
REM  Vivaldi, plain Chromium. This file finds the first one installed and
REM  opens it with a debug port + its own profile
REM  (%USERPROFILE%\youtproject-desktop), so your everyday browser window
REM  is NOT touched.
REM
REM  Want a specific browser? Pass its path:
REM      "Desktop Chrome.bat" "C:\path\to\browser.exe"
REM
REM  Then log into your YouTube channels in that window ONCE. After that the
REM  AI can drive it (click, read, screenshot) while you watch, and:
REM    python main.py desktop status            - what the agent sees
REM    python main.py desktop go "your goal"    - hand it a goal
REM    python main.py order "get a link from the database, get 6 clips and
REM                           post them in 6 channels, and generate 2 videos"
REM
REM  Keep this window open while the agent works. Uploads stay blocked until
REM  you allow them (/desk uploads on, or desktop.uploads: on in config.yaml).
REM ===========================================================================
setlocal
set "BROWSER="
if not "%~1"=="" set "BROWSER=%~1"

REM Prefer the real .exe over launcher.exe where both exist - launchers
REM sometimes drop the flags. Opera GX first because it is common here.
if not defined BROWSER for %%P in (
  "%LocalAppData%\Programs\Opera GX\opera.exe"
  "%LocalAppData%\Programs\Opera GX\launcher.exe"
  "%ProgramFiles%\Opera GX\opera.exe"
  "%ProgramFiles%\Opera GX\launcher.exe"
  "%LocalAppData%\Programs\Opera\opera.exe"
  "%LocalAppData%\Programs\Opera\launcher.exe"
  "%ProgramFiles%\Opera\launcher.exe"
  "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"
  "%LocalAppData%\Programs\BraveSoftware\Brave-Browser\Application\brave.exe"
  "%LocalAppData%\Vivaldi\Application\vivaldi.exe"
  "%ProgramFiles%\Vivaldi\Application\vivaldi.exe"
) do if not defined BROWSER if exist "%%~P" set "BROWSER=%%~P"

if not defined BROWSER (
  echo   No Chromium browser found in the usual places.
  echo   Opera GX, Edge, Chrome, Brave and Vivaldi all work.
  echo   Install one of them, or pass the path:
  echo       "Desktop Chrome.bat" "C:\path\to\browser.exe"
  pause
  exit /b 1
)
echo   Using: %BROWSER%
echo   Profile: %USERPROFILE%\youtproject-desktop  (your normal window is untouched)
start "" "%BROWSER%" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\youtproject-desktop" "https://studio.youtube.com"
echo   Browser is up. The agent can connect now (python main.py desktop status).
echo   If it cannot connect, this browser may refuse the debug port - run
echo   Desktop Chrome.bat with Edge instead, e.g.
echo       "Desktop Chrome.bat" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
endlocal
