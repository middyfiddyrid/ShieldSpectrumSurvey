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
