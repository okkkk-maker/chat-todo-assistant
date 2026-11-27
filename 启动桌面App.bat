@echo off
chcp 65001 >nul 2>&1
title 待办总结助手 · 桌面应用
cd /d "%~dp0"

rem ============================================================
rem  Unified interpreter resolution (ASCII-only logic for cmd.exe).
rem  - Supports paths with spaces (PY_EXE quoted, PY_ARGS unquoted).
rem  - Scans PATH, py launcher, standard install dirs, WorkBuddy.
rem  - Fails fast if sqlcipher3 cannot be prepared (no silent fallback).
rem ============================================================
set "PY_EXE=%~dp0.venv\Scripts\python.exe"
set "PY_ARGS="
if exist "%PY_EXE%" goto check_venv

call :find_python
if not defined PY_EXE goto no_python

echo [*] First run: preparing .venv runtime offline (for QQ/WeChat) ...
"%PY_EXE%" %PY_ARGS% "%~dp0tools\bootstrap_deps.py"
if errorlevel 1 goto bootstrap_fail

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
    set "PY_ARGS="
    goto check_venv
)
goto bootstrap_fail

:check_venv
"%PY_EXE%" %PY_ARGS% -c "import sqlcipher3" >nul 2>&1
if not errorlevel 1 goto run

echo [*] Detected incomplete runtime. Repairing .venv offline ...
call :find_python
if not defined PY_EXE goto bootstrap_fail
"%PY_EXE%" %PY_ARGS% "%~dp0tools\bootstrap_deps.py" --force
if errorlevel 1 goto bootstrap_fail
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
    set "PY_ARGS="
)
"%PY_EXE%" %PY_ARGS% -c "import sqlcipher3" >nul 2>&1
if errorlevel 1 goto bootstrap_fail

:run
"%PY_EXE%" %PY_ARGS% "%~dp0app.py" %*
if errorlevel 1 (
    echo.
    echo [X] Startup failed. See the messages above.
    pause
    exit /b 1
)
exit /b 0

:find_python
set "PY_EXE="
set "PY_ARGS="
for %%P in (python.exe) do (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys, venv" >nul 2>&1
        if not errorlevel 1 (
            set "PY_EXE=python"
            exit /b 0
        )
    )
)
py -3 -c "import sys, venv" >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
    exit /b 0
)
for %%V in (313 312 311 310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        exit /b 0
    )
    if exist "C:\Python%%V\python.exe" (
        set "PY_EXE=C:\Python%%V\python.exe"
        exit /b 0
    )
    if exist "D:\Python%%V\python.exe" (
        set "PY_EXE=D:\Python%%V\python.exe"
        exit /b 0
    )
)
for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%D\python.exe" (
        "%%D\python.exe" -c "import venv" >nul 2>&1
        if not errorlevel 1 (
            set "PY_EXE=%%D\python.exe"
            exit /b 0
        )
    )
)
exit /b 1

:bootstrap_fail
echo.
echo [X] Runtime preparation failed (sqlcipher3 / .venv unavailable).
echo     Please install Python 3.11 ~ 3.13 (Windows 64-bit) with:
echo       - "Add Python to PATH" checked
echo       - Standard installer (NOT embeddable zip, must include venv/pip)
echo     Download: https://www.python.org/downloads/
echo.
pause
exit /b 1

:no_python
echo.
echo [X] No usable Python 3.10~3.13 (x64 with venv) found.
echo     Please install Python 3.13 (Windows 64-bit) and tick "Add Python to PATH",
echo     or run Install.bat first.
echo     Download: https://www.python.org/downloads/
echo.
pause
exit /b 1
