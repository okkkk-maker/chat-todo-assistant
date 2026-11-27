@echo off
rem ============================================================
rem  WeChat Mock Pipeline Test (ASCII-only)
rem ============================================================
chcp 65001 >nul
title WeChat Mock Pipeline Test
cd /d "%~dp0"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
  set "PY=python"
)

"%PY%" "%~dp0..\tools\mock_wechat_test.py"
pause
