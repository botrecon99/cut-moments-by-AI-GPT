@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LOGIN PROFILE YOUTUBE + CHATGPT

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
  echo Khong tim thay Google Chrome.
  pause
  exit /b 1
)

if not exist "chrome_profiles\youtube" mkdir "chrome_profiles\youtube"
if not exist "chrome_profiles\chatgpt" mkdir "chrome_profiles\chatgpt"

echo ============================================================
echo  1/2 LOGIN YOUTUBE PROFILE
echo ============================================================
echo Chrome se mo profile rieng cua project.
echo Dang nhap YouTube bang tay, sau do DONG cua so Chrome do.
echo Roi quay lai day nhan phim bat ky.
echo.
start "YOUTUBE PROFILE" "%CHROME%" --user-data-dir="%~dp0chrome_profiles\youtube" --profile-directory=Default --no-first-run https://www.youtube.com/
pause

echo.
echo ============================================================
echo  2/2 LOGIN CHATGPT PROFILE
echo ============================================================
echo Dang nhap ChatGPT bang tay, xu ly Verify you are human neu co.
echo Sau do DONG cua so Chrome do.
echo Roi quay lai day nhan phim bat ky.
echo.
start "CHATGPT PROFILE" "%CHROME%" --user-data-dir="%~dp0chrome_profiles\chatgpt" --profile-directory=Default --no-first-run https://chatgpt.com/
pause

echo.
echo XONG. Tu gio chay run_auto.bat.
pause
