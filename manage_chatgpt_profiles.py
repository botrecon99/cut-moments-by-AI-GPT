# -*- coding: utf-8 -*-
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "chatgpt_accounts.json"
PROFILE_ROOT = BASE / "chrome_profiles" / "chatgpt_accounts"
CHATGPT_HOME = "https://chatgpt.com/"
PROFILE_DIRECTORY = "Default"

DEFAULT_ACCOUNTS = [
   ]


def detect_chrome():
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return shutil.which("chrome.exe") or shutil.which("chrome")


def safe_key(text):
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text or "").strip()).strip("_")
    return text or "account"


def valid_project_url(url):
    return bool(re.match(r"^https://chatgpt\.com/g/g-p-[^/]+/project/?$", str(url or "").strip(), flags=re.I))


def default_payload():
    accounts = []
    for key, name, url in DEFAULT_ACCOUNTS:
        accounts.append({
            "key": key,
            "name": name,
            "profile_dir": f"chrome_profiles/chatgpt_accounts/{key}",
            "enabled": True,
            "projects": [{"key": key, "name": name, "url": url, "enabled": True}],
        })
    return {
        "accounts": accounts,
        "note": "Mỗi account dùng 1 Chrome profile riêng. Login thủ công, không lưu password trong code.",
    }


def ensure_config():
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(default_payload(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_config():
    ensure_config()
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"❌ Không đọc được {CONFIG.name}: {exc}")
        return default_payload()
    if not isinstance(data, dict):
        data = {"accounts": []}
    data.setdefault("accounts", [])
    return data


def save_config(data):
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)


def account_profile_path(account):
    raw = str(account.get("profile_dir") or "").strip()
    if not raw:
        raw = f"chrome_profiles/chatgpt_accounts/{safe_key(account.get('key') or account.get('name'))}"
        account["profile_dir"] = raw
    p = Path(raw)
    if not p.is_absolute():
        p = (BASE / p).resolve()
    return p


def enabled_accounts(data):
    return [a for a in data.get("accounts", []) if isinstance(a, dict) and a.get("enabled", True)]


def choose_account(data, allow_all=False):
    accounts = enabled_accounts(data)
    if not accounts:
        print("❌ Chưa có account nào. Hãy chọn 'Thêm account/profile mới'.")
        return None
    print("\nDANH SÁCH CHATGPT PROFILE")
    print("-" * 72)
    for i, a in enumerate(accounts, 1):
        p = account_profile_path(a)
        projects = [x for x in a.get("projects", []) if isinstance(x, dict) and x.get("enabled", True)]
        print(f"{i}) {a.get('name') or a.get('key')} [{a.get('key')}] | projects={len(projects)}")
        print(f"   {p}")
    if allow_all:
        print("A) TẤT CẢ profile lần lượt")
    while True:
        raw = input("👉 Chọn profile: ").strip()
        if allow_all and raw.lower() == "a":
            return "ALL"
        if raw.isdigit() and 1 <= int(raw) <= len(accounts):
            return accounts[int(raw)-1]
        low = raw.lower()
        for a in accounts:
            if low in {str(a.get('key','')).lower(), str(a.get('name','')).lower()}:
                return a
        print("⚠️ Không hợp lệ.")


def chrome_running_for_profile(profile_dir):
    # Không cố quét process hệ thống phức tạp; file SingletonLock thường tồn tại khi Chrome đang dùng profile.
    return (profile_dir / "SingletonLock").exists() or (profile_dir / "SingletonCookie").exists()


def launch_profile(account, start_url=None):
    chrome = detect_chrome()
    if not chrome:
        print("❌ Không tìm thấy Google Chrome.")
        return False
    profile_dir = account_profile_path(account)
    profile_dir.mkdir(parents=True, exist_ok=True)
    start_url = str(start_url or CHATGPT_HOME).strip() or CHATGPT_HOME
    command = [
        chrome,
        f"--user-data-dir={profile_dir}",
        f"--profile-directory={PROFILE_DIRECTORY}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        start_url,
    ]
    print("\n" + "=" * 72)
    print(f"👤 PROFILE: {account.get('name') or account.get('key')} [{account.get('key')}]")
    print(f"📁 {profile_dir}")
    print("=" * 72)
    print("Chrome sẽ mở bằng PROFILE RIÊNG này.")
    print("1) Login đúng tài khoản ChatGPT của profile này.")
    print("2) Nếu có Verify/Cloudflare thì tự xác minh.")
    print("3) Kiểm tra đã vào được ChatGPT/Project.")
    print("4) ĐÓNG HẲN cửa sổ Chrome profile này để Chrome lưu session.")
    print("5) Quay lại console và nhấn ENTER.")
    try:
        subprocess.Popen(command)
    except Exception as exc:
        print(f"❌ Không mở được Chrome: {exc}")
        return False
    input("👉 Sau khi login xong + đã đóng Chrome, nhấn ENTER: ")
    time.sleep(0.5)
    if chrome_running_for_profile(profile_dir):
        print("⚠️ Có vẻ Chrome profile này vẫn còn mở. Nên đóng hẳn trước khi mở profile khác.")
    else:
        print("✅ Profile đã được đóng; session/login được giữ trong folder riêng.")
    return True


def add_account(data):
    print("\nTHÊM ACCOUNT / PROFILE MỚI")
    name = input("Tên hiển thị (vd: acc07): ").strip()
    key = safe_key(input(f"Key folder [{safe_key(name)}]: ").strip() or name)
    existing = {str(a.get("key", "")).lower() for a in data.get("accounts", []) if isinstance(a, dict)}
    if key.lower() in existing:
        print("❌ Key đã tồn tại.")
        return
    account = {
        "key": key,
        "name": name or key,
        "profile_dir": f"chrome_profiles/chatgpt_accounts/{key}",
        "enabled": True,
        "projects": [],
    }
    data.setdefault("accounts", []).append(account)
    save_config(data)
    account_profile_path(account).mkdir(parents=True, exist_ok=True)
    print(f"✅ Đã tạo cấu hình profile: {account_profile_path(account)}")
    if input("Mở Chrome để login account này ngay? [Y/n]: ").strip().lower() not in {"n", "no"}:
        launch_profile(account)


def add_project(data):
    account = choose_account(data)
    if not account:
        return
    print(f"\nTHÊM PROJECT CHO: {account.get('name') or account.get('key')}")
    url = input("Dán URL Project ChatGPT: ").strip().rstrip("/")
    if not valid_project_url(url):
        print("❌ URL không đúng dạng https://chatgpt.com/g/g-p-.../project")
        return
    name = input("Tên Project (vd: prayer01): ").strip()
    key = safe_key(input(f"Key Project [{safe_key(name or 'project')}]: ").strip() or name or "project")
    projects = account.setdefault("projects", [])
    if any(str(p.get("url", "")).rstrip("/").lower() == url.lower() for p in projects if isinstance(p, dict)):
        print("⚠️ Project URL đã có trong account này.")
        return
    projects.append({"key": key, "name": name or key, "url": url, "enabled": True})
    save_config(data)
    print("✅ Đã thêm Project.")


def status(data):
    accounts = enabled_accounts(data)
    print("\nTRẠNG THÁI PROFILE")
    print("=" * 72)
    for i, a in enumerate(accounts, 1):
        p = account_profile_path(a)
        cookie_candidates = [
            p / PROFILE_DIRECTORY / "Network" / "Cookies",
            p / PROFILE_DIRECTORY / "Cookies",
        ]
        has_cookie = any(x.exists() and x.stat().st_size > 0 for x in cookie_candidates)
        projects = [x for x in a.get("projects", []) if isinstance(x, dict) and x.get("enabled", True)]
        print(f"{i:02d}. {a.get('name') or a.get('key')} [{a.get('key')}]")
        print(f"    Profile: {p}")
        print(f"    Chrome data/cookies: {'CÓ' if has_cookie else 'CHƯA THẤY'}")
        print(f"    Projects: {len(projects)}")
        for pr in projects:
            print(f"      - {pr.get('name') or pr.get('key')}: {pr.get('url')}")


def login_one_or_all(data):
    selected = choose_account(data, allow_all=True)
    if selected == "ALL":
        accounts = enabled_accounts(data)
        print(f"\nSẽ mở {len(accounts)} profile LẦN LƯỢT, mỗi lần chỉ 1 profile.")
        for idx, account in enumerate(accounts, 1):
            print(f"\n[{idx}/{len(accounts)}]")
            projects = [p for p in account.get("projects", []) if isinstance(p, dict) and p.get("enabled", True)]
            start = projects[0].get("url") if projects else CHATGPT_HOME
            if not launch_profile(account, start):
                print("⚠️ Profile này chưa hoàn tất; chuyển profile tiếp theo.")
        print("\n✅ Đã đi hết danh sách profile.")
        return
    if selected:
        projects = [p for p in selected.get("projects", []) if isinstance(p, dict) and p.get("enabled", True)]
        start = projects[0].get("url") if projects else CHATGPT_HOME
        launch_profile(selected, start)


def menu():
    if os.name == "nt":
        try:
            os.system("chcp 65001 > nul")
        except Exception:
            pass
    ensure_config()
    while True:
        data = load_config()
        print("\n" + "=" * 72)
        print(" CHATGPT MULTI-PROFILE LOGIN MANAGER")
        print(" Mỗi account = một Chrome user-data-dir riêng, login thủ công 1 lần")
        print("=" * 72)
        print("1) Login / relogin 1 profile hoặc TẤT CẢ profile")
        print("2) Thêm account/profile mới")
        print("3) Thêm Project URL cho một account")
        print("4) Xem trạng thái profile + Project")
        print("5) Mở 1 profile để kiểm tra")
        print("0) Thoát")
        choice = input("👉 Chọn: ").strip()
        if choice == "1":
            login_one_or_all(data)
        elif choice == "2":
            add_account(data)
        elif choice == "3":
            add_project(data)
        elif choice == "4":
            status(data)
        elif choice == "5":
            account = choose_account(data)
            if account:
                projects = [p for p in account.get("projects", []) if isinstance(p, dict) and p.get("enabled", True)]
                start = projects[0].get("url") if projects else CHATGPT_HOME
                launch_profile(account, start)
        elif choice == "0":
            break
        else:
            print("⚠️ Không hợp lệ.")


if __name__ == "__main__":
    menu()
