@echo off
chcp 65001 >nul
title 小肥鱼直播间 · 安装依赖
cd /d "%~dp0"

echo ================================================
echo   安装 Python 依赖（运行直播系统所需，共 6 个包）
echo ================================================
echo.
python --version >nul 2>&1
if errorlevel 1 (
  echo [错误] 未检测到 Python，请先安装 Python 3.12 并勾选 Add to PATH
  pause
  exit /b 1
)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo 完成。接下来双击 启动.bat 即可开播。
pause
