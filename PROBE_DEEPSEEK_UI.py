# -*- coding: utf-8 -*-
"""
PROBE DeepSeek UI selectors.
Không lấy password/token/cookie.
Chỉ dump URL + outerHTML rút gọn của control UI để hoàn thiện Selenium selectors.

Cách dùng:
1) python PROBE_DEEPSEEK_UI.py
2) Chrome DeepSeek mở ra.
3) Login thủ công nếu cần.
4) Vào trang chat chính, nhấn ENTER trong console.
5) Script tạo deepseek_ui_probe.json.
6) Gửi file JSON đó cho ChatGPT để chốt selector.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

from selenium import webdriver
from selenium.webdriver.common.by import By

BASE = Path(__file__).resolve().parent
CHROME = (
    next((p for p in [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")),
    ] if p.exists()), None)
    or shutil.which("chrome.exe")
    or shutil.which("chrome")
)
PROFILE = BASE / "chrome_profiles" / "deepseek_probe"
PORT = 9399
URL = "https://chat.deepseek.com/"

def port_ready():
    try:
        with urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False

def clean_outer(el, limit=3000):
    try:
        html = el.get_attribute("outerHTML") or ""
    except Exception:
        return ""
    # Avoid dumping huge inline data.
    return html[:limit]

def attrs(el):
    out = {}
    for key in ("id","class","role","aria-label","aria-disabled","title","placeholder","type","accept","data-testid"):
        try:
            v = el.get_attribute(key)
            if v:
                out[key] = v
        except Exception:
            pass
    return out

if not CHROME:
    raise SystemExit("Không tìm thấy Chrome.")

PROFILE.mkdir(parents=True, exist_ok=True)
cmd = [
    str(CHROME),
    f"--remote-debugging-port={PORT}",
    f"--user-data-dir={PROFILE}",
    "--profile-directory=Default",
    "--no-first-run",
    "--no-default-browser-check",
    "--start-maximized",
    URL,
]
proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

deadline = time.time() + 25
while time.time() < deadline and not port_ready():
    time.sleep(0.3)
if not port_ready():
    raise SystemExit("Không thấy remote debugging port.")

print("=" * 72)
print("Chrome DeepSeek đã mở.")
print("Login thủ công nếu cần.")
print("Vào đúng màn hình chat có ô Message DeepSeek.")
input("Khi đã thấy ô chat, nhấn ENTER ở đây để capture UI: ")

opts = webdriver.ChromeOptions()
opts.binary_location = str(CHROME)
opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{PORT}")
driver = webdriver.Chrome(options=opts)

report = {
    "url": driver.current_url,
    "title": driver.title,
    "composer_candidates": [],
    "file_inputs": [],
    "button_candidates": [],
    "assistant_markdown": [],
}

for selector in [
    "textarea[placeholder='Message DeepSeek']",
    "textarea#chat-input",
    "#chat-input",
    "textarea",
    "div[contenteditable='true']",
]:
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, selector):
            if el.is_displayed():
                report["composer_candidates"].append({
                    "selector": selector,
                    "attrs": attrs(el),
                    "outerHTML": clean_outer(el),
                })
    except Exception:
        pass

try:
    for el in driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
        report["file_inputs"].append({
            "attrs": attrs(el),
            "outerHTML": clean_outer(el),
        })
except Exception:
    pass

# Only UI metadata, no cookie/localStorage/auth data.
try:
    candidates = driver.find_elements(By.CSS_SELECTOR, "button, div[role='button']")
    for el in candidates:
        try:
            if not el.is_displayed():
                continue
            txt = (el.text or "").strip()
            a = attrs(el)
            html = clean_outer(el, 1800)
            if (
                txt
                or a.get("aria-label")
                or a.get("title")
                or "ds-icon" in html
            ):
                report["button_candidates"].append({
                    "text": txt[:300],
                    "attrs": a,
                    "outerHTML": html,
                })
        except Exception:
            pass
except Exception:
    pass

try:
    for el in driver.find_elements(By.CSS_SELECTOR, ".ds-markdown"):
        if el.is_displayed():
            report["assistant_markdown"].append({
                "text_preview": (el.text or "")[:500],
                "outerHTML": clean_outer(el, 2200),
            })
except Exception:
    pass

out = BASE / "deepseek_ui_probe.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"✅ Đã tạo: {out}")
print("Gửi file deepseek_ui_probe.json cho ChatGPT.")
