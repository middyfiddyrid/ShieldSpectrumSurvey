@echo off
title Shield TSCM — New laptop desktop shortcuts
setlocal EnableExtensions

rem Run this once after Dropbox has synced (or after a manual copy).

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "DESKTOP=%USERPROFILE%\Desktop"

echo ============================================================
echo   SHIELD TSCM — setup desktop shortcuts
echo ============================================================
echo.
echo   Windows user:  %USERNAME%
echo   Profile path:  %USERPROFILE%
echo   App folder:    %APP_DIR%
echo   Desktop:       %DESKTOP%
echo.

where py >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found. Install Python 3 and check "Add to PATH", then re-run.
  pause
  exit /b 1
)

echo Installing Python dependencies...
py -m pip install -r "%APP_DIR%\requirements.txt"
if errorlevel 1 (
  echo WARNING: pip install had errors — fix before running a live sweep.
)

call :mkshortcut "START Shield Sweep.bat" "%APP_DIR%\app.py"
call :mkshortcut "START Shield Sweep (DEMO).bat" "%APP_DIR%\app.py --simulate"

echo.
echo Done. Shortcuts are on your Desktop.
echo Each launch checks Dropbox for updates first.
echo On this PC you are signed in as: %USERNAME%
echo.
pause
exit /b 0

:mkshortcut
set "NAME=%~1"
set "ARGS=%~2"
set "SHORT=%DESKTOP%\%NAME%"
(
  echo @echo off
  echo title Shield Sweep App
  echo setlocal EnableExtensions
  echo set "LOCAL_APP=%USERPROFILE%\Desktop\Shield_TSCM_Business\spectrum_survey"
  echo call "%%USERPROFILE%%\Dropbox\Shield_TSCM\sync_from_dropbox.bat"
  echo cd /d "%%LOCAL_APP%%"
  echo if not exist "app.py" ^(
  echo   echo ERROR: App not found. Wait for Dropbox sync or run SETUP_NEW_LAPTOP.bat.
  echo   pause
  echo   exit /b 1
  echo ^)
  echo echo.
  echo echo ============================================================
  echo echo    SHIELD SWEEP APP
  echo echo ============================================================
  echo echo.
  echo echo    Browser opens at http://127.0.0.1:8765
  echo echo    KEEP THIS WINDOW OPEN during the sweep.
  echo echo.
  echo echo ============================================================
  echo echo.
  echo py %ARGS%
  echo echo.
  echo pause
) > "%SHORT%"
echo Created: %SHORT%
exit /b 0