@echo off
chcp 65001 > nul
title MYF AI - llama.cpp Sunucusunu Kapat

echo.
echo ==========================================================
echo   🛑 llama-server ve Ollama Modelleri Kapatiliyor...
echo ==========================================================
echo.

taskkill /F /IM llama-server.exe 2>nul
taskkill /F /IM llama.exe 2>nul

ollama ps >nul 2>&1
if %errorlevel% equ 0 (
    for /f "skip=1 tokens=1" %%m in ('ollama ps') do (
        echo ✓ Ollama modeli bellekten atiliyor: %%m
        ollama stop %%m >nul 2>&1
    )
)

echo.
echo ==========================================================
echo   ✅ Tum yerel yapay zeka surecleri sonlandirildi!
echo   ❄️ RAM ve GPU VRAM bellegi bosaltildi.
echo ==========================================================
echo.
pause
