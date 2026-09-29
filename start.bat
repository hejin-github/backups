@echo off
chcp 65001 >nul
cd /d "%~dp0"
title StockBot 启动器

echo ============================================
echo   策略选股服务启动器  (http://127.0.0.1:8081)
echo ============================================
echo.

REM ---------- 1. 停掉占用 8081 的旧服务 ----------
set "OLDPID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":8081 " ^| findstr /C:"LISTENING"') do set "OLDPID=%%p"
if defined OLDPID (
    echo [1/3] 发现旧服务 PID=%OLDPID%，正在停止 ...
    taskkill /F /PID %OLDPID% >nul 2>&1
    ping -n 3 127.0.0.1 >nul
) else (
    echo [1/3] 未发现运行中的旧服务。
)

REM ---------- 2. 以「独立进程」启动服务 ----------
REM 用 PowerShell Start-Process：服务进程脱离本启动器的进程树，
REM 关闭本窗口 / 按任意键 / 本窗口被中断，都不会再杀掉服务（旧版 start 会被连带终止）。
REM -u 关闭输出缓冲，StockBot 窗口可实时看到启动信息与日志。
echo [2/3] 正在启动服务（独立窗口，脱离本启动器）...
powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/k','title StockBot 服务窗口&python -X utf8 -u app.py' -WorkingDirectory '%CD%' -WindowStyle Minimized"

REM ---------- 3. 等端口就绪后再开浏览器（最多 40 秒）----------
echo [3/3] 等待服务就绪 ...
set "READY=0"
for /l %%i in (1,1,40) do (
    netstat -ano | findstr /C:":8081 " | findstr /C:"LISTENING" >nul
    if not errorlevel 1 (
        set "READY=1"
        goto :ready
    )
    ping -n 2 127.0.0.1 >nul
)

:ready
if "%READY%"=="1" (
    echo       服务已就绪，打开浏览器。
    start http://127.0.0.1:8081
) else (
    echo       [警告] 40 秒内服务未监听 8081，请打开任务栏的「StockBot 服务窗口」查看报错。
)

echo.
echo ============================================
echo  服务运行在独立的「StockBot 服务窗口」中：
echo    - 关闭本启动器窗口 / 按任意键：不会影响服务
echo    - 查看运行日志：点击任务栏最小化的 StockBot 窗口
echo    - 停止服务：关闭 StockBot 窗口，或运行 stop.bat
echo ============================================
echo.
echo 本启动器 10 秒后自动关闭（服务继续运行）...
ping -n 11 127.0.0.1 >nul
