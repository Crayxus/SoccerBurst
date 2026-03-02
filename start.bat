@echo off
chcp 65001 >nul
title SoccerBurst - 足球盘口扫描器
color 0A

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   ⚽  SoccerBurst 盘口扫描器         ║
echo  ║   启动中，请稍候...                  ║
echo  ╚══════════════════════════════════════╝
echo.

cd /d D:\SoccerBurst

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 启动 Flask 服务
echo [信息] 正在启动服务...
echo [信息] 启动后请在浏览器访问: http://localhost:5000
echo [信息] 按 Ctrl+C 可停止服务
echo.

start "" http://localhost:5000
python app.py

pause
