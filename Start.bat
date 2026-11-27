@echo off
rem ============================================================
rem  Chat To-Do Assistant - Start Console
rem ============================================================
chcp 65001 >nul 2>&1
title 待办自动汇总助手 · 控制中心
cd /d "%~dp0"

set "PY_EXE=%~dp0.venv\Scripts\python.exe"
set "PY_ARGS="
if exist "%PY_EXE%" goto check_deps

call :find_python
if defined PY_EXE goto check_deps

rem 一个能用的 Python 都没有：走便携运行时自动准备（先找随包 vendor\python 的 zip，
rem 没有再联网下载）。旧版到此直接报错退出，等于把"没有 Python"的机器劝退。
set "PY_EXE=%~dp0.venv\Scripts\python.exe"
set "PY_ARGS="
echo.
echo [*] 未检测到 Python 3.10~3.13，正在尝试自动准备便携运行环境 ...
call "%~dp0tools\get_python.bat"
if exist "%PY_EXE%" goto check_deps

echo.
echo =========================================================================
echo   [错误] 未检测到可用的 Python 运行环境！
echo.
echo   请先安装 Python 3.10 ~ 3.13（Windows 64位），并在安装界面务必勾选：
echo   [✔] Add Python to PATH (将 Python 添加到环境变量)
echo.
echo   官方下载页面: https://www.python.org/downloads/
echo =========================================================================
echo.
pause
exit /b 1

:check_deps
"%PY_EXE%" %PY_ARGS% -c "import sqlcipher3" >nul 2>&1
if not errorlevel 1 goto launch

echo [*] 检测到当前环境缺少解密依赖（sqlcipher3），正在通过随包 wheels 离线初始化 .venv ...
call :find_python
if not defined PY_EXE goto dep_fail
"%PY_EXE%" %PY_ARGS% "%~dp0tools\bootstrap_deps.py"
if errorlevel 1 goto dep_fail

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
    set "PY_ARGS="
)
"%PY_EXE%" %PY_ARGS% -c "import sqlcipher3" >nul 2>&1
if errorlevel 1 goto dep_fail
echo [OK] 专属运行环境与离线依赖准备完毕！

:launch
if not "%~1"=="" (
    rem 不能把 exit /b %ERRORLEVEL% 写进括号块：%ERRORLEVEL% 在整块解析时就展开了，
    rem 取到的是 run.py 运行**之前**的值，退出码永远是错的。
    "%PY_EXE%" %PY_ARGS% "%~dp0run.py" %*
    goto pass_rc
)

echo =========================================================================
echo   待办自动汇总助手 · 本地控制台
echo   管理后台地址: http://127.0.0.1:8000
echo =========================================================================
echo.
echo [1/2] 正在启动控制台核心服务...
echo [2/2] 浏览器即将自动打开主控界面...
echo.

"%PY_EXE%" %PY_ARGS% "%~dp0server.py"
if errorlevel 1 (
    echo.
    echo [提示] 服务已停止运行。若有报错，请查看上方输出。
    pause
)
pause
exit /b 0

:pass_rc
exit /b %ERRORLEVEL%

:find_python
set "PY_EXE="
set "PY_ARGS="
for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%D\python.exe" (
        "%%D\python.exe" -c "import venv" >nul 2>&1
        if not errorlevel 1 (
            set "PY_EXE=%%D\python.exe"
            exit /b 0
        )
    )
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
python -c "import sys, venv" >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=python"
    exit /b 0
)
py -3 -c "import sys, venv" >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
    exit /b 0
)
exit /b 1

:dep_fail
echo.
echo [X] 离线依赖初始化失败，未能构建可用的 .venv（缺少 sqlcipher3）。
echo     请确认：
echo       1) 已安装 64 位 Python 3.10 ~ 3.13（非精简/嵌入式版本，需包含 venv）
echo       2) 安装包内 vendor\wheels 目录完整
echo.
pause
exit /b 1
