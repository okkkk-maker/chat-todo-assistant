@echo off
rem ============================================================
rem  待办自动汇总助手 · 一键完全卸载与痕迹彻底清理
rem
rem  ASCII-only control flow: cmd.exe mis-parses non-ASCII batch files.
rem  v1.5.3.3 修复：
rem    · 去掉 `if not defined X cmd && set X=..` 这种歧义写法（会覆盖已定位的解释器）
rem    · 真正校验 uninstaller.py 的退出码，失败就如实报错，不再"假成功"
rem    · 后台进程由 uninstaller 按 PID 结束（pythonw 起的实例没有窗口标题可匹配）
rem ============================================================
chcp 65001 >nul 2>&1
title 待办自动汇总助手 · 彻底卸载与痕迹清理
cd /d "%~dp0"

echo =========================================================================
echo   待办自动汇总助手 · 一键彻底卸载向导
echo =========================================================================
echo.
echo   此操作将为您执行深度彻底清理：
echo     1. 结束本程序拉起的后台进程（按 PID 精确匹配，不影响其他 Python 程序）
echo     2. 注销 Windows 开机自启动项 (ChatTodoWatcher / ChatTodoScheduler)
echo     3. 删除桌面与开始菜单上的「待办总结助手」应用图标
echo     4. 还原 QQ 自动更新组件（仅在检测到本程序留下的备份时才会动）
echo     5. 销毁本地明文解密镜像、密钥环、浏览器缓存 (.app_profile)
echo     6. 清理敏感配置文件 (.env) 与临时工作目录
echo.
echo   注意：不会删除程序目录本身，也不会动其他程序的任何文件。
echo =========================================================================
echo.
set "CONFIRM="
set /p CONFIRM="是否确定彻底卸载？(输入 Y 确认，直接回车取消): "
if /i not "%CONFIRM%"=="Y" (
    echo.
    echo [已取消] 未执行卸载操作。
    pause
    exit /b 0
)

echo.
echo [1/3] 正在停止后台服务与采集调度器...
set "PY_EXE="
set "PY_ARGS="
if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if defined PY_EXE goto run_uninstall

python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=python"
    goto run_uninstall
)
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
    goto run_uninstall
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined PY_EXE if exist "%%D\python.exe" set "PY_EXE=%%D\python.exe"
)
if defined PY_EXE goto run_uninstall

echo [!] 未检测到 Python，改用批处理兜底清理流程...
call :fallback_cleanup
goto finished

:run_uninstall
echo [2/3] 正在注销开机项、删除快捷方式、销毁本地镜像与敏感配置...
"%PY_EXE%" %PY_ARGS% "tools\uninstaller.py"
set "UNINSTALL_RC=%ERRORLEVEL%"
if not "%UNINSTALL_RC%"=="0" goto uninstall_failed
goto finished

:uninstall_failed
echo.
echo [X] 卸载未完全成功（退出码 %UNINSTALL_RC%）。
echo     请查看上方列出的未完成项目，处理后再运行一次本脚本。
echo     常见原因：程序仍在运行、文件被占用、缺少管理员权限。
pause
exit /b %UNINSTALL_RC%

:fallback_cleanup
rem 批处理兜底：只删本程序自己创建的固定名字文件/目录
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ChatTodoWatcher.bat" del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ChatTodoWatcher.bat" >nul 2>&1
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ChatTodoScheduler.bat" del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ChatTodoScheduler.bat" >nul 2>&1
if exist "%USERPROFILE%\Desktop\待办总结助手.lnk" del /f /q "%USERPROFILE%\Desktop\待办总结助手.lnk" >nul 2>&1
if exist "%USERPROFILE%\桌面\待办总结助手.lnk" del /f /q "%USERPROFILE%\桌面\待办总结助手.lnk" >nul 2>&1
if exist "%USERPROFILE%\OneDrive\Desktop\待办总结助手.lnk" del /f /q "%USERPROFILE%\OneDrive\Desktop\待办总结助手.lnk" >nul 2>&1
if exist "%USERPROFILE%\OneDrive\桌面\待办总结助手.lnk" del /f /q "%USERPROFILE%\OneDrive\桌面\待办总结助手.lnk" >nul 2>&1
if exist "work\.app_profile" rmdir /s /q "work\.app_profile" >nul 2>&1
if exist "work\wx_mirror" rmdir /s /q "work\wx_mirror" >nul 2>&1
if exist "work\tmp_db" rmdir /s /q "work\tmp_db" >nul 2>&1
if exist "work\tmp_wx" rmdir /s /q "work\tmp_wx" >nul 2>&1
if exist "work\console_port.json" del /f /q "work\console_port.json" >nul 2>&1
if exist "work\.process.lock" del /f /q "work\.process.lock" >nul 2>&1
if exist "config\schema.json" del /f /q "config\schema.json" >nul 2>&1
if exist ".env" del /f /q ".env" >nul 2>&1
if exist ".env.lock" del /f /q ".env.lock" >nul 2>&1
goto :eof

:finished
echo.
echo [3/3] 卸载与清理完毕！
echo.
echo =========================================================================
echo   待办总结助手的外部关联已清理完毕。
echo
echo   【最后一步】关闭本窗口后，把本程序文件夹移入回收站即可彻底删除。
echo =========================================================================
echo.
pause
exit /b 0
