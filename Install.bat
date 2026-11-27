@echo off
rem ASCII-only on purpose (cmd.exe mis-parses Chinese in UTF-8 bat files).
chcp 65001 >nul
title Chat To-Do Assistant - install runtime
cd /d "%~dp0"
echo ============================================================
echo   Chat To-Do Assistant - install runtime
echo   (Usually not needed: Admin_Init.bat does this for you)
echo ============================================================
echo.

set "LAUNCH_EXE="
set "LAUNCH_ARGS="

for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%D\python.exe" (
        "%%D\python.exe" -c "import venv" >nul 2>&1
        if not errorlevel 1 set "LAUNCH_EXE=%%D\python.exe"
    )
)
if not defined LAUNCH_EXE (
    for %%V in (313 312 311 310) do (
        if not defined LAUNCH_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "LAUNCH_EXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        if not defined LAUNCH_EXE if exist "C:\Python%%V\python.exe" set "LAUNCH_EXE=C:\Python%%V\python.exe"
        if not defined LAUNCH_EXE if exist "D:\Python%%V\python.exe" set "LAUNCH_EXE=D:\Python%%V\python.exe"
    )
)
if not defined LAUNCH_EXE (
    python -c "import sys, venv" >nul 2>&1
    if not errorlevel 1 set "LAUNCH_EXE=python"
)
if not defined LAUNCH_EXE (
    py -3 -c "import sys, venv" >nul 2>&1
    if not errorlevel 1 (
        set "LAUNCH_EXE=py"
        set "LAUNCH_ARGS=-3"
    )
)

if not defined LAUNCH_EXE goto portable

:have_py
echo [1/2] Preparing runtime (offline wheels) ...
"%LAUNCH_EXE%" %LAUNCH_ARGS% "%~dp0tools\bootstrap_deps.py"
if errorlevel 1 goto dep_fail

echo [2/2] Registering auto-start ...
set "RUN_PY="
if exist "%~dp0.venv\Scripts\python.exe" set "RUN_PY=%~dp0.venv\Scripts\python.exe"
if not defined RUN_PY goto dep_fail
"%RUN_PY%" "%~dp0tools\setup_autostart.py"
if errorlevel 1 goto autostart_fail
echo.
echo ============================================================
echo   Done. Double-click Start.bat to run once.
echo ============================================================
pause
exit /b 0

:portable
echo   [!] No Python 3.10~3.13 found. Provisioning a portable runtime ...
call "%~dp0tools\get_python.bat"
if not exist "%~dp0.venv\Scripts\python.exe" goto no_python
echo [2/2] Registering auto-start ...
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\setup_autostart.py"
if errorlevel 1 goto autostart_fail
echo.
echo ============================================================
echo   Done (portable runtime). Double-click Start.bat to run once.
echo ============================================================
pause
exit /b 0

:no_python
echo   [X] No usable Python 3.10~3.13 (x64 with venv) found.
echo       Please install Python 3.13 (Windows x64) and tick "Add Python to PATH":
echo       https://www.python.org/downloads/
pause
exit /b 1

:dep_fail
echo   [X] Runtime preparation failed. Ensure 64-bit Python 3.10~3.13 (with venv)
echo       and vendor\wheels are present, then retry.
pause
exit /b 1

:autostart_fail
echo   [X] Auto-start registration failed. The runtime is installed, but startup is NOT enabled.
pause
exit /b 1
