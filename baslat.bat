@echo off
chcp 65001 > nul
title Multi-Agent Yazilim Gelistirme Sistemi

echo.
echo  ============================================
echo    Multi-Agent Yazilim Gelistirme Sistemi
echo    Koordinator + Pipeline v2.1
echo  ============================================
echo.

:: agent_system klasorune gec
cd /d "%~dp0agent_system"

:: Python var mi?
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [HATA] Python bulunamadi. Python 3.10+ yukleyin.
    pause & exit /b 1
)

:: Sanal ortam var mi?
if not exist ".venv\Scripts\activate.bat" (
    echo [KURULUM] Sanal ortam olusturuluyor...
    python -m venv .venv
    if %errorlevel% neq 0 (echo [HATA] Sanal ortam olusturulamadi. & pause & exit /b 1)
)

call .venv\Scripts\activate.bat

:: Bagimliliklar yuklu mu?
if not exist ".venv\.deps_installed" (
    echo [KURULUM] Bagimliliklar yukleniyor...
    pip install -q -r requirements.txt
    if %errorlevel% neq 0 (echo [HATA] Kurulum basarisiz. & pause & exit /b 1)
    echo. > .venv\.deps_installed
    echo [KURULUM] Tamamlandi.
    echo.
)

:: Codebase Memory binary kontrolu (Windows icin)
set CB_MEM_DIR=%~dp0third_party\codebase_memory
if not exist "%CB_MEM_DIR%\codebase-memory-mcp.exe" (
    echo [KURULUM] codebase-memory-mcp Windows binary'si hazirlaniyor...
    if exist "%CB_MEM_DIR%\install.ps1" (
        powershell -ExecutionPolicy Bypass -File "%CB_MEM_DIR%\install.ps1" -Dir "%CB_MEM_DIR%" >nul 2>&1
    )
)

:: Ollama calisiyor mu? (uyari ver, durdurmaz)
ollama list >nul 2>&1
if %errorlevel% neq 0 (
    echo [UYARI] Ollama calismiyor gorunuyor. Baslatmayi deneyin: ollama serve
    echo [UYARI] LM Studio kullaniyor musunuz? .env dosyasinda SERVER=lm_studio ayarlayin.
    echo.
)

:: Komut satiri argumani varsa manage_agents'a ilet
if "%1"=="agents" (
    python manage_agents.py %2 %3 %4 %5
    goto :eof
)

if "%1"=="manage" (
    python manage_agents.py %2 %3 %4 %5
    goto :eof
)

:: Ana chat arayuzu
echo [BASLATILIYOR] chat.py
echo.
python chat.py

echo.
echo [BITTI] Cikis kodu: %errorlevel%
pause
