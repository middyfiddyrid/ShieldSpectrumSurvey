@echo off
title Shield Sweep
cd /d "%~dp0"
echo ============================================================
echo    SHIELD SWEEP  --  LIVE  (plug the Pluto in first)
echo.
echo    A web browser opens at  http://127.0.0.1:8765
echo    Keep THIS window open during the sweep.
echo    Close this window to stop it.
echo ============================================================
echo.
where py >nul 2>&1
if %errorlevel%==0 goto RUNPY
where python >nul 2>&1
if %errorlevel%==0 goto RUNPYTHON
echo  *** Python is not installed on this computer yet. ***
echo  Open the python folder, run python-3.14.5-amd64.exe,
echo  tick "Add python.exe to PATH", then try again.
echo.
goto END
:RUNPY
py app.py
goto END
:RUNPYTHON
python app.py
goto END
:END
echo.
pause
