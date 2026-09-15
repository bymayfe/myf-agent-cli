@echo off
setlocal
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
title Multi-Agent Log ve Denetim Goruntuleyici
echo ======================================================
echo   Multi-Agent Canli Log ve Denetim Goruntuleyici
echo ======================================================
echo.

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%agent_system"

if exist ".venv\Scripts\python.exe" (
    if exist "storage\log_viewer.py" (
        ".venv\Scripts\python.exe" storage\log_viewer.py %*
    ) else (
        ".venv\Scripts\python.exe" log_viewer.py %*
    )
) else (
    if exist "storage\log_viewer.py" (
        python storage\log_viewer.py %*
    ) else (
        python log_viewer.py %*
    )
)

pause
