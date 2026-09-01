@echo off
chcp 936 >nul
title 停止 A股云图服务
set FOUND=

rem ====== 方式1（主）：按进程名 + 命令行精确匹配 server.py ======
rem 兼容 Win10/Win11（不用 wmic，Win11 已移除）；只杀本服务，不误杀其他进程
for /f "usebackq delims=" %%p in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'server\.py' } | ForEach-Object { $_.ProcessId }"`) do (
    echo [A股云图] 停止服务进程 PID=%%p
    taskkill /F /PID %%p >nul 2>&1
    set FOUND=1
)

rem ====== 方式2（兜底）：8080 端口仍被监听时的提示 ======
rem 若端口被占用但进程不是本服务（如其他程序占用），提示但不自动杀，防误杀
netstat -ano | findstr ":8080" | findstr "LISTENING" >nul
if not errorlevel 1 (
    for /f "usebackq tokens=5" %%p in (`netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"`) do (
        echo [A股云图] 提示：8080 端口仍被 PID=%%p 监听（若非本服务进程，请手动确认）
        set FOUND=1
    )
)

if defined FOUND (
    echo [A股云图] 已停止运行中的服务
) else (
    echo [A股云图] 未发现运行中的服务
)
pause
