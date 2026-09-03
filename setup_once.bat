@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SETUP AUTO YOUTUBE CHATGPT PIPELINE

echo ============================================================
echo  SETUP RIENG .venv - KHONG DUNG CHUNG ANACONDA
echo ============================================================
echo.

if exist ".venv\Scripts\python.exe" goto install

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
    echo [LOI] Khong tao duoc .venv
    pause
    exit /b 1
)

:install
set "PATH=%~dp0.venv\Scripts;%PATH%"
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo ============================================================
echo  SETUP XONG
echo ============================================================
echo Selenium va yt-dlp nam rieng trong .venv cua project.
echo Khong ha/nang version thu vien trong Anaconda cua ban.
echo.
pause
