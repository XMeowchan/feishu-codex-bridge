@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 "%SCRIPT_DIR%start_bridge.py"
    set "RC=%ERRORLEVEL%"
    goto after_run
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%SCRIPT_DIR%start_bridge.py"
    set "RC=%ERRORLEVEL%"
    goto after_run
)

echo 未找到 py 或 python，请先安装 Python。
set "RC=1"
goto after_run

:after_run
echo.
if not "%RC%"=="0" (
    echo 桥接服务已退出，退出码：%RC%
) else (
    echo 桥接服务已退出。
)
echo.
pause
exit /b %RC%
