@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist workspace_bridge.json (
  del /q workspace_bridge.json
  echo Da tat bridge. DeepSeek se dung data trong thu muc project moi.
) else (
  echo Chua co workspace_bridge.json.
)
pause
