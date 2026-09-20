@echo off
chcp 65001 > nul
title MYF AI - llama.cpp GPU Server (Windows CUDA)

echo.
echo ==========================================================
echo   🚀 MYF AI — llama.cpp GPU Server (Windows CUDA)
echo   🌐 API Endpoint: http://localhost:8080/v1
echo   🛑 Durdurmak icin: llama_server_durdur.bat veya Ctrl+C
echo ==========================================================
echo.

set SCRIPT_DIR=%~dp0
set MODELS_DIR=%SCRIPT_DIR%models

if not exist "%MODELS_DIR%" mkdir "%MODELS_DIR%"

:: llama-server.exe binary konumu arama
set LLAMA_BIN=
if exist "%SCRIPT_DIR%llama.cpp\llama-server.exe" set "LLAMA_BIN=%SCRIPT_DIR%llama.cpp\llama-server.exe"
if not defined LLAMA_BIN if exist "%SCRIPT_DIR%llama-server.exe" set "LLAMA_BIN=%SCRIPT_DIR%llama-server.exe"
if not defined LLAMA_BIN if exist "%SCRIPT_DIR%llama.cpp\build\bin\Release\llama-server.exe" set "LLAMA_BIN=%SCRIPT_DIR%llama.cpp\build\bin\Release\llama-server.exe"
if not defined LLAMA_BIN if exist "%LOCALAPPDATA%\llama.cpp\llama-server.exe" set "LLAMA_BIN=%LOCALAPPDATA%\llama.cpp\llama-server.exe"
if not defined LLAMA_BIN (
    where llama-server.exe >nul 2>&1
    if %errorlevel% equ 0 (
        set "LLAMA_BIN=llama-server.exe"
    )
)

if not defined LLAMA_BIN (
    echo ==========================================================
    echo   ❌ [HATA] llama-server.exe bulunamadi!
    echo ==========================================================
    echo   Lutfen llama.cpp Windows surumunu indirin ve llama-server.exe
    echo   dosyasini su konumlardan birine yerlestirin:
    echo     - %SCRIPT_DIR%llama-server.exe
    echo     - %SCRIPT_DIR%llama.cpp\llama-server.exe
    echo     - Veya PATH ortam degiskenine ekleyin.
    echo ==========================================================
    echo.
    pause
    exit /b 1
)

:: Yerel model arama (Oncelik: Qwen3.8 Flash Next shard 1 -> Qwen2.5-Coder 7B -> diger .gguf)
set LOCAL_MODEL=

if exist "%MODELS_DIR%\Qwen3.8-Flash-Next\Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf" (
    set "LOCAL_MODEL=%MODELS_DIR%\Qwen3.8-Flash-Next\Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf"
    goto :run_model
)

if exist "%MODELS_DIR%\Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf" (
    set "LOCAL_MODEL=%MODELS_DIR%\Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"
    goto :run_model
)

for /r "%MODELS_DIR%" %%f in (*00001-of-*.gguf) do (
    set "LOCAL_MODEL=%%f"
    goto :run_model
)

for /r "%MODELS_DIR%" %%f in (*.gguf) do (
    set "LOCAL_MODEL=%%f"
    goto :run_model
)

:run_model
if defined LOCAL_MODEL (
    echo 📦 Yerel Model Yukleniyor: %LOCAL_MODEL%
    "%LLAMA_BIN%" -m "%LOCAL_MODEL%" --host 0.0.0.0 --port 8080 -c 32768 -t 8 -tb 16 --fit on --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 -b 512 -ub 512 --cont-batching --reasoning-format deepseek --reasoning-preserve
) else (
    echo 🌐 Yerel model bulunamadi, HuggingFace'ten Qwen2.5-Coder-7B indiriliyor...
    "%LLAMA_BIN%" --hf-repo "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF" --hf-file "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf" --host 0.0.0.0 --port 8080 -c 32768 -t 8 -tb 16 --fit on --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 -b 512 -ub 512 --cont-batching --reasoning-format deepseek --reasoning-preserve
)

echo.
echo ==========================================================
echo   Sunucu sonlandi veya durduruldu.
echo ==========================================================
echo.
pause
