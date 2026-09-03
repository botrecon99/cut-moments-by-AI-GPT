@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo  RETRY FAILED - YOUTUBE / CHATGPT
echo ============================================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" "retry_failed_runner.py"
set "ERR=%ERRORLEVEL%"

echo.
echo ============================================================
if "%ERR%"=="0" (
    echo  RETRY FAILED DA HOAN TAT
) else (
    echo  RETRY FAILED KET THUC VOI MA LOI: %ERR%
)
echo ============================================================
pause
exit /b %ERR%
