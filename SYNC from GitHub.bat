@echo off
setlocal
title Shield Sweep - SYNC from GitHub
cd /d "%~dp0"

echo ============================================================
echo    SHIELD SWEEP  -  SYNC FROM GITHUB
echo ============================================================
echo.
echo    Repo:   %CD%
echo.

rem ---- 0. is git even here? -----------------------------------------------
git --version >nul 2>&1
if errorlevel 1 (
  echo    [X] Git is not installed / not on PATH.
  echo        Install: winget install Git.Git   then re-run this.
  goto :END
)

rem ---- 0b. is THIS folder actually the repo? ------------------------------
rem Without this, a copy of the script run from the wrong folder falls through
rem to the fetch and blames GitHub for what is really a wrong-directory problem.
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo    [X] This folder is not a git checkout:
  echo        %CD%
  echo.
  echo        This script must live in - and be run from - the
  echo        spectrum_survey folder that was cloned from GitHub.
  echo        First-time desktop setup:
  echo          git clone https://github.com/middyfiddyrid/ShieldSpectrumSurvey.git
  goto :END
)

rem ---- 1. NEVER clobber local work ----------------------------------------
rem A dirty tree means someone edited the app on THIS machine. Pulling over the
rem top of that loses it silently, so stop and let the operator decide.
for /f %%i in ('git status --porcelain 2^>nul ^| find /c /v ""') do set DIRTY=%%i
if not "%DIRTY%"=="0" (
  echo    [!] This machine has %DIRTY% uncommitted change^(s^).
  echo        NOT pulling - that would overwrite local work.
  echo.
  git status --short
  echo.
  echo        Commit or discard them, then run this again.
  goto :END
)

rem ---- 2. what's on the server? -------------------------------------------
echo    Checking GitHub...
git fetch origin --quiet
if errorlevel 1 (
  echo    [X] Could not reach GitHub ^(offline, or sign-in needed^).
  echo        The app still runs fine on the local copy.
  goto :END
)

for /f %%i in ('git rev-list --count HEAD..@{u} 2^>nul') do set BEHIND=%%i
if "%BEHIND%"=="" set BEHIND=0
if "%BEHIND%"=="0" (
  echo    [OK] Already up to date - nothing new on GitHub.
  goto :END
)

echo.
echo    %BEHIND% new commit^(s^) on GitHub:
echo    ------------------------------------------------------------
git --no-pager log --oneline --no-decorate HEAD..@{u}
echo    ------------------------------------------------------------
echo.

rem ---- 3. fast-forward only ------------------------------------------------
rem --ff-only REFUSES rather than inventing a merge if the two machines have
rem diverged. A refusal you can see beats a silent merge you cannot.
git pull --ff-only
if errorlevel 1 (
  echo.
  echo    [X] Could not fast-forward - this machine and GitHub have diverged.
  echo        Nothing was changed. Ask before forcing anything.
  goto :END
)

echo.
echo    [OK] Updated to the latest version.
echo.
echo    NOTE: if the Shield Sweep app is running, RESTART it to pick up
echo          backend changes, then hard-refresh the browser ^(Ctrl+Shift+R^).

:END
echo.
echo ============================================================
pause
endlocal
