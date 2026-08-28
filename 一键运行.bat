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

".venv\Scripts\python.exe" -B -X utf8 app.py
pause
