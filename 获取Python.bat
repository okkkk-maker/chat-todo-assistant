@echo off
rem ============================================================
rem  Portable Python · one-click provisioner (double-clickable)
rem
rem  Puts a working runtime into .venv\ so that the rest of the program can run
rem  on a machine with NO Python installed and/or NO internet:
rem    - prefers the bundled vendor\python\*.zip  (zero network)
rem    - otherwise downloads the official embeddable build (4 mirrors)
rem    - then installs the wheels offline (no pip needed)
rem
rem  Idempotent: if .venv already works, it just reports and exits 0.
rem ============================================================
chcp 65001 >nul 2>&1
title 待办总结助手 · 准备便携 Python 运行环境

echo =========================================================================
echo   待办总结助手 · 便携 Python 运行环境
echo =========================================================================
echo   本程序自带运行时不依赖系统安装的 Python：
echo     · 优先使用随包的 vendor\python\python-3.x.y-embed-amd64.zip（完全离线）
echo     · 没有随包资产时才联网下载（官方源 + 国内镜像，共 4 个源）
echo     · 依赖用 wheel 解压安装，不需要 pip ^& 不需要联网
echo.
echo   可用参数：
echo     --offline              只允许用随包资产，禁止联网
echo     --version 3.13.9       指定精确版本
echo     --base-url URL         使用内网源（http/https/file/UNC 均可）
echo     --force                已有运行时也重建
echo =========================================================================
echo.

call "%~dp0tools\get_python.bat" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo =========================================================================
    echo   完成。现在可以双击「启动桌面App.bat」或「Start.bat」了。
    echo =========================================================================
) else (
    echo =========================================================================
    echo   未完成（退出码 %RC%）。请按上面的提示处理，或查看：
    echo     work\python-bootstrap.log
    echo =========================================================================
)
echo.
pause
exit /b %RC%
