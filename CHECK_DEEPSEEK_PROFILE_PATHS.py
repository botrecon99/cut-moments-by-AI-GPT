# -*- coding: utf-8 -*-
from pathlib import Path
import json, re

APP = Path(__file__).resolve().parent
cfg = APP / "deepseek_accounts.json"
payload = json.loads(cfg.read_text(encoding="utf-8-sig"))
accounts = payload.get("accounts", [])

print("="*72)
print("DEEPSEEK PROFILE PATHS - MAIN + LOGIN MUST MATCH THESE")
print("="*72)
for a in accounts:
    key = re.sub(r"[^A-Za-z0-9_-]+", "_", str(a.get("key") or a.get("name") or "account")).strip("_")
    p = (APP / "chrome_profiles" / "deepseek_accounts" / key).resolve()
    print(f"{key}:")
    print(f"  {p}\\Default")
