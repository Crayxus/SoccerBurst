@echo off
chcp 65001 >nul
title SoccerBurst - 本地推送模式

echo ========================================
echo   SoccerBurst - 足球盘口扫描器
echo ========================================
echo.

REM 检查是否设置了 RENDER_URL
if "%RENDER_URL%"=="" (
    echo [提示] 未设置 RENDER_URL 环境变量
    echo [提示] 将使用默认地址: https://soccerburst.onrender.com
    echo.
    set RENDER_URL=https://soccerburst.onrender.com
)

echo [模式] 本地扫描 + 推送到云端
echo [云端] %RENDER_URL%
echo.
echo 正在启动...
echo.

python push_to_cloud.py

pause
