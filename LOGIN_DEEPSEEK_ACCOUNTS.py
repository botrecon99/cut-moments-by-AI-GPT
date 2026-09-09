# -*- coding: utf-8 -*-
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG = APP_DIR / "deepseek_accounts.json"

def chrome_binary():
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return shutil.which("chrome.exe") or shutil.which("chrome")

def safe_key(v):
    v = re.sub(r"[^A-Za-z0-9_-]+", "_", str(v or "").strip()).strip("_")
    return v or "account"

def load_accounts():
    if not CONFIG.exists():
        payload = {
            "accounts": [
                {"key": "deepseek1", "name": "deepseek1", "enabled": True},
                {"key": "deepseek2", "name": "deepseek2", "enabled": True},
            ]
        }
        CONFIG.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    payload = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    raw = payload.get("accounts", []) if isinstance(payload, dict) else payload
    out = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        key = safe_key(item.get("key") or item.get("name"))
        name = str(item.get("name") or key)
        profile = (APP_DIR / "chrome_profiles" / "deepseek_accounts" / key).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        out.append((key, name, profile))
    return out

chrome = chrome_binary()
if not chrome:
    raise SystemExit("❌ Không tìm thấy Google Chrome.")

accounts = load_accounts()
if not accounts:
    raise SystemExit("❌ Không có DeepSeek account enabled.")

print("=" * 72)
print("LOGIN DEEPSEEK - DÙNG ĐÚNG PROFILE CỦA MAIN PIPELINE")
print("=" * 72)
for i, (key, name, profile) in enumerate(accounts, 1):
    print(f"{i}) {name} [{key}]")
    print(f"   📁 {profile}\\Default")

raw = input(f"\nChọn profile 1-{len(accounts)}: ").strip()
try:
    idx = int(raw) - 1
except Exception:
    idx = 0
idx = max(0, min(idx, len(accounts)-1))

key, name, profile = accounts[idx]

print()
print(f"🚀 Mở DeepSeek profile: {name} [{key}]")
print(f"📁 EXACT PATH: {profile}\\Default")
print("👉 Login NGAY TRONG cửa sổ Chrome này.")
print("👉 Khi đã vào chat và thấy 'Message DeepSeek', hãy ĐÓNG cửa sổ Chrome này.")
print("👉 Sau đó mới chạy auto_youtube_deepseek_cut.py.")

subprocess.Popen([
    chrome,
    f"--user-data-dir={profile}",
    "--profile-directory=Default",
    "--no-first-run",
    "--no-default-browser-check",
    "--start-maximized",
    "https://chat.deepseek.com/",
])

input("\nNhấn ENTER để đóng cửa sổ console này (Chrome vẫn mở): ")
