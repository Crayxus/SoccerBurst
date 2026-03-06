@echo off
chcp 65001 >nul
title SoccerBurst - 本地模式

echo ========================================
echo   SoccerBurst - 足球盘口扫描器
echo ========================================
echo.

set RENDER_URL=https://soccerburst.onrender.com
set MODE=local
set PUSH_SECRET=soccerburst2026

echo [本地UI]  http://localhost:5000
echo [云端]    %RENDER_URL%
echo.

REM 在新窗口启动本地 Web 服务器（用于 FETCH 按钮 + 本地查看）
echo 启动本地 Web 服务器...
start "SoccerBurst Web" cmd /k "cd /d %~dp0 && set MODE=local && set RENDER_URL=%RENDER_URL% && set PUSH_SECRET=%PUSH_SECRET% && python app.py"

REM 等待服务器启动
timeout /t 3 /nobreak >nul

REM 自动打开浏览器
start http://localhost:5000

REM 启动扫描推送器（当前窗口）
echo 启动扫描推送器（每5分钟扫描并推送到云端）...
echo.
python push_to_cloud.py

pause
