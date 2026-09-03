@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Hay chay setup_once.bat truoc.
  pause
  exit /b 1
)
set "PATH=%~dp0.venv\Scripts;%PATH%"
".venv\Scripts\python.exe" self_test.py
pause
