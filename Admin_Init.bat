@echo off
rem ============================================================
rem  Implementer one-time setup. ASCII-only on purpose:
rem  cmd.exe mis-parses Chinese in UTF-8 batch files.
rem  All Chinese guidance is printed by tools\admin_init.py.
rem ============================================================
chcp 65001 >nul
title Chat To-Do Assistant - one-time setup (for maintainers)
cd /d "%~dp0"

rem --- needs admin: debugger attach + modifying program folder ---
net session >nul 2>&1
if errorlevel 1 goto elevate

echo ============================================================
echo   Chat To-Do Assistant - one-time setup
echo   Takes about 3 minutes. Only one manual step: QQ login.
echo ============================================================
echo.

rem --- 1) find a Python launcher (must be the SYSTEM python, not .venv) ---
set "LAUNCH="
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "LAUNCH=python"
if defined LAUNCH goto have_py
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "LAUNCH=py -3"
if defined LAUNCH goto have_py
goto portable

:have_py
echo [1/3] Preparing runtime (offline wheels, no download needed) ...
echo ------------------------------------------------------------
%LAUNCH% tools\bootstrap_deps.py
if errorlevel 1 goto dep_fail
echo ------------------------------------------------------------
echo.

:run_wizard
echo [2/3] Starting the setup wizard ...
echo ------------------------------------------------------------
".venv\Scripts\python.exe" tools\admin_init.py %*
set RC=%ERRORLEVEL%
echo ------------------------------------------------------------
echo.
if "%RC%"=="0" goto all_ok
echo   Setup did NOT fully succeed (exit code %RC%).
echo   Check the items marked with X above, then run this file again.
echo   Finished steps are skipped automatically.
echo.
pause
exit /b %RC%

:all_ok
echo   Setup complete. Hand the "2-user daily package" folder to the user.
echo.
echo Starting Web Console for multi-platform configuration ...
start Web_Console.bat
exit /b 0

:elevate
echo Requesting administrator rights, please click "Yes" ...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:portable
echo [*] No Python found. Provisioning a portable runtime (no install needed) ...
call "%~dp0tools\get_python.bat"
if not exist "%~dp0.venv\Scripts\python.exe" goto no_python
echo [1/3] Portable runtime ready (dependencies installed offline).
echo.
goto run_wizard

:no_python
echo.
echo   [X] No usable Python found.
echo       Install Python 3.13 (x64) and tick "Add Python to PATH", then retry.
echo       https://www.python.org/downloads/
echo.
pause
exit /b 1

:dep_fail
echo.
echo   [X] Runtime preparation failed.
echo       Usually Network is blocked AND vendor\wheels is missing.
echo       Re-extract the full package (it must contain vendor\wheels).
echo.
pause
exit /b 1
