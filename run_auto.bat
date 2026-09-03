@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AUTO YOUTUBE - CHATGPT - MAIN CONTENT CUT

echo ============================================================
echo  AUTO YOUTUBE - CHATGPT - MAIN CONTENT CUT FINAL
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [CHUA SETUP] Hay chay setup_once.bat mot lan truoc.
    echo.
    pause
    exit /b 1
)

set "PATH=%~dp0.venv\Scripts;%PATH%"
".venv\Scripts\python.exe" auto_youtube_chatgpt_cut.py

echo.
pause
