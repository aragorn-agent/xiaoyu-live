@echo off
chcp 65001 >nul
title 小肥鱼直播间
cd /d "%~dp0"

echo ================================================
echo   小肥鱼直播间 · 一键启动
echo ================================================
echo.
echo [1/2] 启动主服务（舞台 http://127.0.0.1:8188 ，中控台 /admin）
rem 优先用项目自带虚拟环境，其次回退 PATH 里的 python
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
start "xiaoyu-server" /min cmd /c "%PY% server.py > logs\server.log 2>&1"
timeout /t 3 /nobreak >nul

echo [2/2] 打开直播舞台窗口（自动播声，供直播伴侣采集）
rem 自动播放策略：免点击出声。也可以手动用浏览器打开 http://127.0.0.1:8188/
set CHROME=
for %%p in (
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do if exist %%p set CHROME=%%p
if defined CHROME (
  start "" %CHROME% --autoplay-policy=no-user-gesture-required --app=http://127.0.0.1:8188/
) else (
  start "" http://127.0.0.1:8188/
  echo   [提示] 未找到 Chrome，请手动点一下页面解锁声音
)

echo.
echo 完成。中控台: http://127.0.0.1:8188/admin
echo 抖音直播伴侣里添加【浏览器图层/窗口采集】选这个舞台窗口即可。
echo 关闭：直接关掉舞台窗口，再关 "xiaoyu-server" 最小化窗口。
pause
