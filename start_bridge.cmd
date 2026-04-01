@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

where psmux >nul 2>nul
if not %ERRORLEVEL% EQU 0 (
    echo psmux not found, please install first:
    echo   winget install psmux
    set "RC=1"
    goto after_run
)

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

echo Could not find py or python. Please install Python first.
set "RC=1"
goto after_run

:after_run
echo.
if not "%RC%"=="0" (
    echo Bridge service exited with code: %RC%
) else (
    echo Bridge service exited.
)
echo.
pause
exit /b %RC%
