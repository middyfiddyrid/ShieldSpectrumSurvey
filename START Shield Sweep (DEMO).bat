@echo off
title Shield Sweep (DEMO)
cd /d "%~dp0"
echo ============================================================
echo    SHIELD SWEEP  --  DEMO  (simulated, no radio needed)
echo.
echo    A web browser opens at  http://127.0.0.1:8765
echo    Keep THIS window open while you use the app.
echo    Close this window to stop it.
echo ============================================================
echo.
rem --- pull newest version from GitHub; NEVER block the demo ---
rem GIT_TERMINAL_PROMPT=0 so a credential prompt can never hang the launcher.
set GIT_TERMINAL_PROMPT=0
where git >nul 2>&1
if %errorlevel% neq 0 goto NOGIT
echo  Checking GitHub for updates...
git pull --ff-only >nul 2>&1
if %errorlevel%==0 goto UPDATED
echo  Could not update - offline, or local edits in the way.
echo  Running the version already on this computer.
goto AFTERUPDATE
:UPDATED
echo  Up to date with GitHub.
goto AFTERUPDATE
:NOGIT
echo  Git not installed - running the version already on this computer.
:AFTERUPDATE
echo.
where py >nul 2>&1
if %errorlevel%==0 goto RUNPY
where python >nul 2>&1
if %errorlevel%==0 goto RUNPYTHON
echo  *** Python is not installed on this computer yet. ***
echo.
echo    1) Open the  python  folder in this kit
echo    2) Run  python-3.14.5-amd64.exe
echo    3) TICK "Add python.exe to PATH", then Install
echo    4) Double-click this file again
echo.
goto END
:RUNPY
py app.py --simulate
goto END
:RUNPYTHON
python app.py --simulate
goto END
:END
echo.
pause
