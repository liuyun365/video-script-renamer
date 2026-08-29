@echo off
chcp 65001 >nul
title 视频按剧本顺序重命名工具
set "HF_ENDPOINT=https://hf-mirror.com"
set "NO_PROXY=*"
set "PYTHONDONTWRITEBYTECODE=1"
cd /d "%~dp0"

echo ============================================
echo   视频按剧本顺序重命名工具
echo   启动后将自动打开浏览器操作界面
echo   关闭本窗口即退出程序
echo ============================================
echo.

rem 结束占用端口的旧实例，避免新实例启动失败、浏览器仍连到旧页面
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":17888" ^| findstr /C:"LISTENING"') do (
  echo 检测到旧实例 PID %%a，正在结束...
  taskkill /F /PID %%a >nul 2>&1
)

".venv\Scripts\python.exe" -B -X utf8 app.py
pause
