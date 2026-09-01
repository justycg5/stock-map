@echo off
title A股热力图服务
cd /d D:\autoagent_file\a-share-heatmap

rem ========== 选择 Python（优先 pythonw.exe，无窗口后台运行） ==========
rem 0) 项目独立 Python（.python\，官方完整版，本项目优先使用）
set "PYW="
set "PY="
if exist "%~dp0.python\pythonw.exe" (
    set "PYW=%~dp0.python\pythonw.exe"
) else if exist "D:\Program Files\AutoClaw\resources\python\pythonw.exe" (
    set "PYW=D:\Program Files\AutoClaw\resources\python\pythonw.exe"
) else if exist "D:\Program Files\AutoClaw\resources\python\python.exe" (
    set "PY=D:\Program Files\AutoClaw\resources\python\python.exe"
) else (
    rem 2) PATH 中的 python（跳过 WindowsApps 商店版）
    where python >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%i in ('where python') do (
            echo %%i | findstr /i "WindowsApps" >nul
            if errorlevel 1 if not defined PY set "PY=%%i"
        )
    )
    if not defined PY (
        rem 3) py launcher
        where py >nul 2>nul
        if not errorlevel 1 set "PY=py"
    )
)
if not defined PYW if not defined PY (
    echo [服务] 未找到 Python 3，请先安装 Python 3
    pause
    exit /b 1
)

rem ========== 选择应用模式浏览器（方案A：--app 独立窗口，无地址栏） ==========
rem Edge 优先（用户默认浏览器），Chrome 次之，兜底系统默认浏览器普通打开
set "APPBROWSER="
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    set "APPBROWSER=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
) else if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
    set "APPBROWSER=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
) else if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "APPBROWSER=C:\Program Files\Google\Chrome\Application\chrome.exe"
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "APPBROWSER=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)

rem ========== 已在运行则只打开应用窗口 ==========
netstat -ano | findstr ":8080 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [服务] 8080 已在运行，打开应用窗口...
    call :open_app
    timeout /t 2 >nul
    exit /b 0
)

rem ========== 后台启动（无窗口，日志写入 log.log），就绪后打开应用窗口 ==========
echo [服务] 后台启动中：http://127.0.0.1:8080/  （日志：同级目录 log.log）
if defined PYW (
    start "" "%PYW%" server.py 8080
) else (
    rem 无 pythonw 时降级：start /b 后台运行 + cmd 重定向日志
    start "" /b "%PY%" server.py 8080 >> log.log 2>&1
)
timeout /t 2 >nul
call :open_app
exit /b 0

rem ========== 打开应用窗口（标签必须在文件末尾，顺序执行到此） ==========
:open_app
if defined APPBROWSER (
    start "" "%APPBROWSER%" --app=http://127.0.0.1:8080/
) else (
    start "" http://127.0.0.1:8080/
)
exit /b 0
