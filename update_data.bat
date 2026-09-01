@echo off
title A股云图 数据更新
cd /d D:\autoagent_file\a-share-heatmap

rem ========== 探测 Python（与 start.bat 同一套逻辑） ==========
set "PY="
set "PYARGS="
if exist "D:\Program Files\AutoClaw\resources\python\python.exe" (
    set "PY=D:\Program Files\AutoClaw\resources\python\python.exe"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%i in ('where python') do (
            echo %%i | findstr /i "WindowsApps" >nul
            if errorlevel 1 if not defined PY set "PY=%%i"
        )
    )
    if not defined PY (
        where py >nul 2>nul
        if not errorlevel 1 (
            set "PY=py"
            set "PYARGS=-3"
        )
    )
)
if not defined PY (
    echo [A股云图] 未找到可用的 Python，请安装 Python 3 后重试。
    pause
    exit /b 1
)

echo.
echo ============================================
echo   A股云图 数据更新（data.json + global.json）
echo   全量抓取约 3-5 分钟，请勿关闭本窗口
echo ============================================
echo.

"%PY%" %PYARGS% fetch_data.py

echo.
if errorlevel 1 (
    echo [A股云图] 更新失败，请检查网络后重试。
) else (
    echo [A股云图] 更新完成！data.json 与 global.json 已刷新。
    echo [A股云图] 刷新浏览器页面即可看到最新数据（Ctrl+F5 强制刷新）。
)
echo.
pause
