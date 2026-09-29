@echo off
chcp 65001 >nul
cd /d "%~dp0"
title StockBot 停止器

echo 正在停止 8081 服务 ...
set "OLDPID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":8081 " ^| findstr /C:"LISTENING"') do set "OLDPID=%%p"
if defined OLDPID (
    taskkill /F /PID %OLDPID%
    echo 已停止服务 PID=%OLDPID%
) else (
    echo 未发现运行中的服务（8081 未在监听）。
)
echo.
ping -n 6 127.0.0.1 >nul
