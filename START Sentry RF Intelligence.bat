@echo off
title Sentry RF Intelligence
cd /d "%~dp0"
echo ============================================================
echo    SENTRY RF INTELLIGENCE  --  LIVE B220
echo.
echo    A web browser opens at  http://127.0.0.1:8876
echo    Keep THIS window open during the sweep.
echo    Close this window to stop it.
echo    The original ShieldTSCM application is not modified.
echo.
echo    B220 first CONNECT can take 20-30s. If it fails: unplug USB
echo    for 5 seconds, reconnect it, and launch Sentry again.
echo ============================================================
echo.

rem UHD host library for the B220 / B210-compatible receiver.
if exist "C:\Program Files\UHD\bin\uhd.dll" set "PATH=C:\Program Files\UHD\bin;%PATH%"
if exist "C:\Program Files\PothosSDR\bin\uhd.dll" set "PATH=C:\Program Files\PothosSDR\bin;%PATH%"

set "PYCMD="
if exist "%~dp0runtime\python\python.exe" set "PYCMD=%~dp0runtime\python\python.exe"
if defined PYCMD goto PYTHON_READY
where py >nul 2>&1
if %errorlevel%==0 set "PYCMD=py"
if not defined PYCMD (
  where python >nul 2>&1
  if %errorlevel%==0 set "PYCMD=python"
)
if not defined PYCMD (
  echo  *** Sentry's private Python runtime is missing. ***
  echo  Run "Repair Sentry Runtime.bat" from the installation package,
  echo  then retry. No Windows Python installation is required.
  goto END
)
:PYTHON_READY
if not exist logs mkdir logs
if not exist data mkdir data

:RUN
"%PYCMD%" -B app.py --port 8876
set EXITCODE=%errorlevel%
echo [%date% %time%] app exited with code %EXITCODE% >> logs\launcher.log
if "%EXITCODE%"=="0" goto END
if "%EXITCODE%"=="-1073741510" goto END
echo.
echo  Sentry stopped unexpectedly, usually from a radio USB fault.
echo  Reconnect the B220. Restarting in 5 seconds; Ctrl+C cancels.
timeout /t 5 /nobreak >nul
goto RUN

:END
echo.
pause
