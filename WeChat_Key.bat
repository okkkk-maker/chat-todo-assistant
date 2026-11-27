@echo off
rem ============================================================
rem  WeChat 4.x DB key helper
rem    - 4.0.x : memory scan (raw key cached as x'<key><salt>')
rem    - 4.1+  : WCDB Config.Cipher object (XOR-masked blob, per-DB key)
rem  Both are covered by option 1. Option 2 is a fallback that hooks
rem  the moment WeChat opens its database.
rem  ASCII-only on purpose: cmd.exe mangles non-ASCII in .bat
rem ============================================================
chcp 65001 >nul
title WeChat Key
cd /d "%~dp0"

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY set "PY=python"

:menu
echo.
echo  ============================================================
echo   WeChat DB key
echo  ============================================================
echo   1) Auto grab            - 4.0.x / 4.1+  (recommended)
echo   2) Startup capture      - fallback if option 1 misses
echo   3) Paste 64-hex key
echo   4) Environment check
echo   0) Exit
echo.
set "CH="
set /p "CH=Choose [1]: "
if not defined CH set "CH=1"
if "%CH%"=="1" goto auto
if "%CH%"=="2" goto hook
if "%CH%"=="3" goto paste
if "%CH%"=="4" goto status
if "%CH%"=="0" goto end
goto menu

:auto
echo.
echo  Make sure WeChat is running and logged in (window title = your nick).
echo  Reading keys only; nothing is written to WeChat.
echo.
"%PY%" "%~dp0tools\wxkey.py" acquire
echo.
echo  If it failed: check that Weixin.exe is running and you are admin.
pause
goto menu

:hook
echo.
echo  Fallback: attach at the instant WeChat opens its database.
echo  Only needed when option 1 cannot read the Config.Cipher object.
echo.
echo  Steps:
echo    1. After pressing a key, COMPLETELY quit WeChat (tray icon too).
echo    2. Open WeChat from the desktop and log in within 3 minutes.
echo.
pause
echo.
echo  Waiting for WeChat to restart...
"%PY%" "%~dp0tools\wxhook.py" capture --timeout 180
echo.
echo  If it says NO_KEY: quit WeChat completely, then run option 2 again.
pause
goto menu

:paste
echo.
set "KEY="
set /p "KEY=Paste the 64-hex key: "
if not defined KEY goto menu
"%PY%" "%~dp0tools\wxkey.py" paste "%KEY%"
pause
goto menu

:status
echo.
"%PY%" "%~dp0tools\wxhook.py" status
echo.
"%PY%" "%~dp0tools\wxkey.py" status
pause
goto menu

:end
exit /b 0
