@echo off
title Shield Sweep
cd /d "%~dp0"
echo ============================================================
echo    SHIELD SWEEP  --  LIVE  (plug the radios in first)
echo.
echo    A web browser opens at  http://127.0.0.1:8765
echo    Keep THIS window open during the sweep.
echo    Close this window to stop it.
echo    If the app crashes (radio USB fault) it RESTARTS ITSELF here.
echo ============================================================
echo.
set PYCMD=
where py >nul 2>&1
if %errorlevel%==0 set PYCMD=py
if not defined PYCMD (
  where python >nul 2>&1
  if %errorlevel%==0 set PYCMD=python
)
if not defined PYCMD (
  echo  *** Python is not installed on this computer yet. ***
  echo  Open the python folder, run python-3.14.5-amd64.exe,
  echo  tick "Add python.exe to PATH", then try again.
  echo.
  goto END
)
if not exist logs mkdir logs

:RUN
%PYCMD% app.py
set EXITCODE=%errorlevel%
echo [%date% %time%] app exited with code %EXITCODE% >> logs\launcher.log
rem 0 = clean shutdown; -1073741510 = Ctrl+C (0xC000013A) — operator meant to stop
if "%EXITCODE%"=="0" goto END
if "%EXITCODE%"=="-1073741510" goto END
echo.
echo  ************************************************************
echo  ***  Shield Sweep stopped unexpectedly (code %EXITCODE%).
echo  ***  Usually a radio USB fault - the radios may need a replug.
echo  ***  RESTARTING in 5 seconds... press Ctrl+C to stop instead.
echo  ***  Details: logs\app.log and logs\crash.log
echo  ************************************************************
echo.
timeout /t 5 /nobreak >nul
goto RUN

:END
echo.
pause
