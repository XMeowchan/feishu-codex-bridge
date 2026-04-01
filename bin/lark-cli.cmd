@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 "%SCRIPT_DIR%lark_cli_wrapper.py" %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%SCRIPT_DIR%lark_cli_wrapper.py" %*
    exit /b %ERRORLEVEL%
)

echo Python interpreter not found for lark-cli wrapper. 1>&2
exit /b 127
