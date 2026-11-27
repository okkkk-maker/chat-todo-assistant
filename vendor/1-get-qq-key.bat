@echo off
rem ============================================================
rem  QQ 数据库密钥自动抓取与验证向导 (需管理员权限挂载调试器)
rem  Logic block is ASCII-only: cmd.exe mis-parses Chinese in UTF-8 bat.
rem  Interpreter resolution is identical to every other entry point
rem  (see tools/runtime_env.py) - the old build silently fell back to
rem  a system `python` without sqlcipher3, so the key could be found
rem  but never verified, and schema.json was never frozen.
rem ============================================================
chcp 65001 >nul 2>&1
title 待办自动汇总助手 · QQ密钥自动抓取向导
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator rights to attach the debugger...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "PY_EXE=%~dp0..\.venv\Scripts\python.exe"
if exist "%PY_EXE%" goto run

rem Interpreter and its arguments MUST stay in two variables.
rem Old code stored "py -3" in one variable and then quoted it as "%BOOT_PY%",
rem so cmd looked for an executable literally named `py -3` and always failed --
rem i.e. the whole flow broke on machines that only have the py launcher.
set "BOOT_EXE="
set "BOOT_ARGS="
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "BOOT_EXE=python"
    goto have_boot_py
)
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "BOOT_EXE=py"
    set "BOOT_ARGS=-3"
    goto have_boot_py
)
goto no_python

:have_boot_py
echo [*] Preparing runtime offline (sqlcipher3 is required to verify the key) ...
"%BOOT_EXE%" %BOOT_ARGS% "%~dp0..\tools\bootstrap_deps.py"
if exist "%PY_EXE%" goto run
echo [!] Bootstrap failed; without sqlcipher3 the key cannot be verified.
pause
exit /b 1

:run
"%PY_EXE%" "%~dp0..\tools\qqkey.py" --extract
exit /b %ERRORLEVEL%

:no_python
echo [X] No usable Python found. Install Python 3.13 (x64) and retry.
pause
exit /b 1
