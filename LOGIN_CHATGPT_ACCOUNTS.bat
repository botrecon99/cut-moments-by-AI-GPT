@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ================================================================
echo  CHATGPT MULTI-PROFILE LOGIN MANAGER
echo ================================================================

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" manage_chatgpt_profiles.py
) else (
    python manage_chatgpt_profiles.py
)

if errorlevel 1 (
    echo.
    echo [LOI] Khong chay duoc profile manager.
    echo Hay chay setup_once.bat truoc neu chua co Python/.venv.
)

echo.
pause
