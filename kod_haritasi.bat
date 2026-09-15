@echo off
setlocal
title Codebase Memory Visualizer
echo ======================================================
echo   Codebase Memory - Gorsel Kod Haritasi
echo ======================================================
echo.

set "SCRIPT_DIR=%~dp0"
set "BINARY_PATH=%SCRIPT_DIR%third_party\codebase_memory\codebase-memory-mcp.exe"

:: 1. Port 9749 zaten acik mi?
netstat -ano | findstr ":9749" >nul 2>&1
if %errorlevel% equ 0 (
    echo [BILGI] Sunucu zaten arka planda calisiyor!
    echo [ACILIYOR] Tarayicida aciliyor: http://localhost:9749 ...
    start "" "http://localhost:9749"
    ping 127.0.0.1 -n 2 >nul
    exit /b 0
)

:: 2. Binary yoksa indir
if not exist "%BINARY_PATH%" (
    echo [KURULUM] codebase-memory-mcp Windows binarysi indiriliyor...
    if exist "%SCRIPT_DIR%third_party\codebase_memory\install.ps1" (
        powershell -ExecutionPolicy Bypass -File "%SCRIPT_DIR%third_party\codebase_memory\install.ps1" -Dir "%SCRIPT_DIR%third_party\codebase_memory"
    )
)

echo [1/2] Web Arayuzu Tarayicida Aciliyor (http://localhost:9749)...
start "" "http://localhost:9749"

echo [2/2] Sunucu Baslatiliyor (Kapatmak icin bu pencereyi kapatin)...
echo.
"%BINARY_PATH%"
pause
