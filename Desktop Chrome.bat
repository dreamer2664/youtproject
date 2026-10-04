@echo off
REM ===========================================================================
REM  Desktop Chrome.bat - start Chrome the way the desktop agent needs it.
REM
REM  Opens Chrome with a debug port and a dedicated profile
REM  (%USERPROFILE%\youtproject-desktop). Log into your channels in that
REM  window ONCE; after that the AI can drive it (click, read, screenshot)
REM  while you watch.
REM
REM  Then, from the project folder:
REM    python main.py desktop status            - what the agent sees
REM    python main.py desktop go "your goal"    - hand it a goal
REM    python main.py order "get a link from the database, get 6 clips and
REM                           post them in 6 channels, and generate 2 videos"
REM
REM  Leave this window open while the agent works. Closing Chrome ends the
REM  session but loses nothing - reopen it and carry on. Uploads stay blocked
REM  until you allow them (/desk uploads on, or desktop.uploads: on in
REM  config.yaml).
REM ===========================================================================
setlocal
set "CHROME="
for %%P in (
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do if not defined CHROME if exist "%%~P" set "CHROME=%%~P"
if not defined CHROME (
  echo   Chrome was not found in the usual places.
  echo   Install Google Chrome, or edit this file with the right path.
  pause
  exit /b 1
)
echo   Starting Chrome for the desktop agent ...
echo   Profile: %USERPROFILE%\youtproject-desktop
start "" %CHROME% --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\youtproject-desktop" "https://studio.youtube.com"
echo   Chrome is up. The agent can connect now (python main.py desktop status).
endlocal
