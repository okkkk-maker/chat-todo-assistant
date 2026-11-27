@echo off
rem ============================================================
rem  WeChat 4.x Native Key Extractor (ASCII-only)
rem ============================================================
chcp 65001 >nul
title WeChat 4.x Key Extractor
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator privileges ...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "PY="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0python_runtime\python.exe" set "PY=%~dp0python_runtime\python.exe"

if not defined PY (
    for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)
if not defined PY (
    for /d %%D in ("C:\Python3*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)
if not defined PY (
    for /d %%D in ("D:\Python3*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)
if not defined PY (
    for /d %%D in ("E:\Python3*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)
if not defined PY (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY set "PY=python"

echo Starting WeChat 4.x Native Key Extractor ...
echo ------------------------------------------------------------
if exist "%~dp0..	ools\wxkey.py" (
    "%PY%" "%~dp0..	ools\wxkey.py" acquire
) else if exist "%~dp0tools\wxkey.py" (
    "%PY%" "%~dp0tools\wxkey.py" acquire
) else (
    echo [!] Cannot locate tools\wxkey.py
)
echo ------------------------------------------------------------
echo.
pause
