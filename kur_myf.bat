@echo off
chcp 65001 > nul
title MYF / MAYF CLI Kurulumu

echo.
echo  ======================================================
echo    MYF / MAYF CLI Komut Kurulumu
echo  ======================================================
echo.

set "WIN_APPS=%LOCALAPPDATA%\Microsoft\WindowsApps"
set "MYF_CMD=%WIN_APPS%\myf.cmd"
set "MAYF_CMD=%WIN_APPS%\mayf.cmd"

set "VENV_PY=c:\Users\seyfo\Desktop\Projects\CLI_Project\agent_system\.venv\Scripts\python.exe"
set "CHAT_PY=c:\Users\seyfo\Desktop\Projects\CLI_Project\agent_system\chat.py"

echo @echo off > "%MYF_CMD%"
echo "%VENV_PY%" "%CHAT_PY%" --workdir "%%CD%%" %%* >> "%MYF_CMD%"

echo @echo off > "%MAYF_CMD%"
echo "%VENV_PY%" "%CHAT_PY%" --workdir "%%CD%%" %%* >> "%MAYF_CMD%"

echo  [OK] 'myf'  komutu yuklendi: %MYF_CMD%
echo  [OK] 'mayf' komutu yuklendi: %MAYF_CMD%
echo.
echo  Artik Windows CMD veya PowerShell'de HANGI KLASORDE olursaniz olun:
echo    myf
echo  veya
echo    mayf
echo  yazarak sistemi aninda o klasorde baslatabilirsiniz!
echo.
pause
