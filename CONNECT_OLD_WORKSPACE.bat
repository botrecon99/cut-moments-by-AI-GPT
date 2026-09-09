@echo off
chcp 65001 >nul
cd /d "%~dp0"
python CONNECT_OLD_WORKSPACE.py
echo.
pause
