@echo off
rem ============================================================
rem  Portable Python entry point (usable even when no Python is installed)
rem
rem  Layered strategy, cheapest first:
rem    1. .venv already works            -> nothing to do
rem    2. any system python / py launcher -> python_bootstrap.py --auto (local zip first)
rem    3. no Python at all               -> get_python.ps1 downloads a portable one
rem
rem  ASCII-only control flow on purpose: cmd.exe mis-parses non-ASCII batch files.
rem  Note: no "if <cond> cmd && cmd" one-liners here -- that idiom is ambiguous
rem  and was exactly the bug that made Uninstall.bat silently skip its work.
rem ============================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0.."

echo ============================================================
echo   便携 Python 运行时 · 自动准备
echo ============================================================

rem --- 1) 已有可用运行时 ---
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sqlcipher3" >nul 2>&1
    if not errorlevel 1 (
        echo [OK] 已有可用的便携运行时，无需处理。
        exit /b 0
    )
)

rem --- 2) 借系统 Python 跑装配器（它会优先用随包 zip，不联网）---
python -c "import sys" >nul 2>&1
if not errorlevel 1 goto use_python

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 goto use_py

goto use_powershell

:use_python
echo [*] 使用系统 python 执行装配 ...
python "tools\python_bootstrap.py" --auto %*
if not errorlevel 1 goto done
echo [注意] 系统 python 装配失败，尝试用 PowerShell 直接获取。
goto use_powershell

:use_py
echo [*] 使用 py 启动器执行装配 ...
py -3 "tools\python_bootstrap.py" --auto %*
if not errorlevel 1 goto done
echo [注意] py 启动器装配失败，尝试用 PowerShell 直接获取。

:use_powershell
echo [*] 本机没有可用的 Python，改用 PowerShell 获取便携版（约 11 MB）...
set "PS_EXTRA="
if /i "%~1"=="--offline" set "PS_EXTRA=-Offline"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\get_python.ps1" -Root "%CD%" %PS_EXTRA%
if errorlevel 1 goto ps_fail

:done
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sqlcipher3" >nul 2>&1
    if not errorlevel 1 (
        echo [OK] 便携运行时已就绪。
        exit /b 0
    )
)
echo [失败] 运行时仍未就绪，请查看 work\python-bootstrap.log
exit /b 1

:ps_fail
echo.
echo [X] 未能自动获取 Python 运行时。
echo     常见原因：本机断网、公司代理拦截 python.org / 镜像站。
echo.
echo     离线解决办法（推荐）：
echo       1) 在一台能上网的电脑上，解压本程序后执行：
echo            python tools\python_bootstrap.py --fetch --pack
echo       2) 把生成的 dist\python-runtime-*.zip 解压合并到本程序目录，
echo          使 vendor\python\ 下出现 python-3.x.y-embed-amd64.zip
echo       3) 回到本机重新双击本脚本（此时全程不需要联网）
echo.
echo     也可用内网源：本脚本加 --base-url http://内网地址/python
echo.
pause
exit /b 1
