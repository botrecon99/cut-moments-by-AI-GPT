# -*- coding: utf-8 -*-
"""
AUTO YOUTUBE -> TRANSCRIPT -> CHATGPT ROUND ROBIN -> MP3 CUT -> MERGE

Luồng:
1) Mở YouTube bằng Selenium với profile local của project.
2) Mở video YouTube.
3) Lấy transcript có timestamp từ YouTube theo nhiều tầng fallback.
   - Direct get_panel bằng session Chrome hiện tại.
   - Nếu có nút Show transcript: bắt response get_panel thật từ Network.
   - Nếu video KHÔNG có nút Show transcript/get_panel: lấy captionTracks động rồi fetch /api/timedtext?fmt=json3.
   - KHÔNG hard-code cookie, signature, pot, expire hay cURL; mọi token lấy mới từ chính video/session hiện tại.
4) Tạo 2 file RIÊNG: *_PROMPT.txt và *_TRANSCRIPT.txt; tuyệt đối không ghép text.
5) Mở Chrome ChatGPT thật và attach Selenium; chỉ hỏi login/xác minh khi phát hiện auth/challenge thật; PROMPT được COPY vào Windows clipboard và Ctrl+V đúng MỘT LẦN vào composer; tuyệt đối không chèn bằng JS/CDP/chunk. Sau đó code verify cấu trúc + thứ tự timestamp mẫu. TRANSCRIPT được đính kèm RIÊNG bằng uploader thật. Mỗi bước phải verify thành công mới được Send.
6) Parse NONE hoặc các mốc HH:MM:SS --> ... / END.
7) Sau khi có mốc hợp lệ mới tải AUDIO-ONLY và chuyển trực tiếp sang MP3 bằng story_cutter_core.py.
8) Cắt bỏ các đoạn trên MP3 và ghép lại, chuyển vào channels/<kênh>/done/.

LƯU Ý:
- Hai profile nằm trong ./chrome_profiles/youtube và ./chrome_profiles/chatgpt.
- Không cần copy cookie YouTube vào code. Cookie trong cURL sẽ hết hạn.
- ChatGPT giữ Chrome/profile sống khi có thể; không dừng tay nếu session còn hợp lệ.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait


import story_cutter_core as cutter


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
PROMPT_FILE = BASE_DIR / "prompt.txt"
TRANSCRIPT_DIR = BASE_DIR / "transcripts"
AI_RESULT_DIR = BASE_DIR / "ai_results"
AI_INPUT_DIR = BASE_DIR / "ai_inputs"
CHANNELS_DIR = BASE_DIR / "channels"
CHANNEL_INDEX_FILE = BASE_DIR / "channel_index.json"
LIVE_COOKIE_FILE = BASE_DIR / "youtube_live_cookies.txt"

def _detect_chrome_binary():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return shutil.which("chrome.exe") or shutil.which("chrome") or candidates[0]


CHROME_BINARY = _detect_chrome_binary()
PROFILE_DIRECTORY = "Default"

# Hai profile đều nằm NGAY trong thư mục code để dễ quản lý / di chuyển.
PROFILE_ROOT = BASE_DIR / "chrome_profiles"
YOUTUBE_USER_DATA_DIR = PROFILE_ROOT / "youtube"
CHATGPT_USER_DATA_DIR = PROFILE_ROOT / "chatgpt"

# ChatGPT được mở bằng Chrome THẬT trước, chưa có Selenium điều khiển.
# Selenium attach vào Chrome thật đang chạy; chỉ cần thao tác tay nếu auth/challenge thật sự xuất hiện.
CHATGPT_DEBUG_HOST = "127.0.0.1"
CHATGPT_DEBUG_PORT = 9222

YOUTUBE_HOME = "https://www.youtube.com/"
CHATGPT_HOME = "https://chatgpt.com/"

# ChatGPT Project mode: mỗi lần chạy chọn 1 Project/người.
# Mỗi video/attempt sẽ quay về đúng URL Project này để tạo NEW CHAT bên trong Project,
# tuyệt đối không tạo chat ngoài trang Home.
CHATGPT_PROJECTS_FILE = BASE_DIR / "chatgpt_projects.json"  # legacy config, kept for compatibility
CHATGPT_ACCOUNTS_FILE = BASE_DIR / "chatgpt_accounts.json"
CHATGPT_ACCOUNT_PROFILE_ROOT = PROFILE_ROOT / "chatgpt_accounts"
LAST_CHATGPT_ACCOUNT_FILE = BASE_DIR / "runtime" / "last_chatgpt_account.txt"
LAST_CHATGPT_PROJECT_FILE = BASE_DIR / "runtime" / "last_chatgpt_project.txt"
CHATGPT_DEBUG_ACCOUNT_FILE = BASE_DIR / "runtime" / "chatgpt_debug_account.txt"
ACTIVE_CHATGPT_ACCOUNT_KEY = ""
ACTIVE_CHATGPT_ACCOUNT_NAME = ""

WAIT_PAGE = 35
WAIT_TRANSCRIPT = 20
WAIT_CHATGPT_READY = 20
WAIT_CHATGPT_RESPONSE = 240

# Tự đóng hẳn Chrome ChatGPT và mở lại sau mỗi N video để giải phóng RAM.
# 0 = tắt. Giá trị này chỉ còn dùng khi chạy SINGLE PROFILE.
CHATGPT_RESTART_EVERY = 0

# MULTI-PROFILE ROUND ROBIN:
# True  = VIDEO 1 -> profile 1, VIDEO 2 -> profile 2, ... rồi quay vòng.
# Mỗi profile dùng Project đầu tiên đang enabled trong chatgpt_accounts.json.
CHATGPT_ROUND_ROBIN = True
# Đóng HẲN Chrome ChatGPT sau mỗi video để chỉ có 1 profile ChatGPT chạy tại một thời điểm.
CHATGPT_ROUND_ROBIN_CLOSE_EACH_VIDEO = False

# ChatGPT sidebar/project list can occasionally return HTTP 429 on
# /backend-api/conversations while the current model answer still succeeds.
# IMPORTANT: pipeline NEVER calls that endpoint itself just to "check" it, because
# another GET would increase request pressure. We only PASSIVELY observe requests
# the ChatGPT page already made, then dismiss the matching "Too many requests" popup.
CHATGPT_CONVERSATIONS_API_FRAGMENT = "/backend-api/conversations"
CHATGPT_API429_POPUP_WAIT = 2.0
CHATGPT_RESTART_CLOSE_WAIT = 20

# True = sau khi mỗi video xử lý xong, mở chat mới cho video tiếp theo.
NEW_CHAT_EACH_VIDEO = True

# True = cố bấm nút Copy của ChatGPT. Nếu selector UI đổi, vẫn fallback lấy text từ DOM.
CLICK_CHATGPT_COPY = True

# Nếu list.txt trống, chương trình sẽ hỏi link trực tiếp trong console.

TRANSCRIPT_PLACEHOLDER = "[DÁN TOÀN BỘ TRANSCRIPT CÓ TIMESTAMP VÀO ĐÂY]"

# ======================== FINAL / SCALE CONFIG ========================
# list.txt có thể chứa hàng chục nghìn link. File này KHÔNG bị rewrite sau mỗi video.
# Link hoàn thành chỉ được append vào doneLink.txt để resume nhanh/an toàn.
GLOBAL_DONE_FILE = BASE_DIR / "doneLink.txt"
GLOBAL_FAILED_FILE = BASE_DIR / "failedLink.jsonl"
GLOBAL_NO_STORY_FILE = BASE_DIR / "no_story.txt"
GLOBAL_LOG_DIR = BASE_DIR / "logs"
RUNTIME_DIR = BASE_DIR / "runtime"
CHATGPT_DEBUG_PORT_FILE = RUNTIME_DIR / "chatgpt_debug_port.txt"
CHATGPT_ROUND_ROBIN_CURSOR_FILE = RUNTIME_DIR / "chatgpt_round_robin_cursor.txt"

# Folder kênh đúng format user yêu cầu:
#   CHANNEL NAME _ CHANNEL ID
# Nếu channel đổi tên, pipeline ưu tiên reuse folder cũ có cùng CHANNEL ID để không tách dữ liệu.
RENAME_CHANNEL_FOLDER_WHEN_NAME_CHANGES = False

# Nếu AI result hợp lệ đã tồn tại từ lần chạy trước (ví dụ lần trước download 403),
# pipeline reuse kết quả để KHÔNG gửi ChatGPT lại.
REUSE_VALID_AI_RESULT = True

# Nếu output ChatGPT sai format / bịa timestamp, tự yêu cầu sửa format tối đa số lần này.
AI_REPAIR_ATTEMPTS = 2

# ChatGPT: prompt native Ctrl+V một lần; transcript chỉ upload FILE thật.
CHATGPT_UPLOAD_WAIT = 30
CHATGPT_UPLOAD_RETRIES = 2
CHATGPT_PASTE_FALLBACK_WAIT = 20  # legacy helper only
CHATGPT_SMART_CHAT_ATTEMPTS = 2
PROMPT_NATIVE_PASTE_WAIT = 8
PROMPT_MIN_WORD_RATIO = 0.95
# Trên UI ChatGPT hiện tại, paste dài có thể tự biến thành “pasted text” attachment.
# Với prompt dài hơn ngưỡng này, dùng CDP Input.insertText MỘT LẦN (không chunk) để giữ inline.
PROMPT_INLINE_NATIVE_MAX_CHARS = 4200

# Compatibility constants for legacy pasted-text helper definitions that remain in
# this consolidated FINAL file. The active SMART pipeline below does not depend
# on them for its main upload path, but Python evaluates default arguments when
# defining functions, so these names MUST exist at import/startup time.
CHATGPT_PASTE_RETRIES = 2
PASTE_ATTACHMENT_WAIT = CHATGPT_PASTE_FALLBACK_WAIT

# Dung lượng trống tối thiểu trước download audio-only.
MIN_FREE_GB = 1.0

# Cookie live chứa session đăng nhập nên xóa ngay sau khi download xong/thất bại.
DELETE_LIVE_COOKIE_AFTER_DOWNLOAD = True

# Nếu get_panel/Show transcript thất bại, thử captionTracks -> /api/timedtext JSON3.
ENABLE_TRANSCRIPT_TIMEDTEXT_FALLBACK = True
TIMEDTEXT_FETCH_TIMEOUT = 15

# Fallback cuối: đọc transcript đang render trong DOM (nếu panel có thể mở).
ENABLE_TRANSCRIPT_DOM_FALLBACK = True

# Không bao giờ coi AI hợp lệ nếu dùng timestamp không có trong transcript.
STRICT_AI_TIMESTAMP_VALIDATION = True


# ============================================================
# CHATGPT PROJECT / FOLDER MODE
# ============================================================

DEFAULT_CHATGPT_PROJECTS = [
    {"key": "chien", "name": "chien", "url": "https://chatgpt.com/g/g-p-69c239bcb7488191bf506226cce32c3a-chien/project", "enabled": True},
    {"key": "nhat", "name": "nhat", "url": "https://chatgpt.com/g/g-p-69c491ecae7081918913e18f59578764-nhat/project", "enabled": True},
    {"key": "nem", "name": "nem", "url": "https://chatgpt.com/g/g-p-69d37c9c7b28819184b98c10f77012ef-nem/project", "enabled": True},
    {"key": "trung", "name": "trung", "url": "https://chatgpt.com/g/g-p-6a8584bc058c8191acda20d6c8f20abb-trung/project", "enabled": True},
    {"key": "loi", "name": "loi", "url": "https://chatgpt.com/g/g-p-6a6073c858c48191ba69022d7a6e6e45-loi/project", "enabled": True},
    {"key": "sang", "name": "sang", "url": "https://chatgpt.com/g/g-p-69d8af27d9808191a7f6aa14e8bbf76e-sang/project", "enabled": True},
]

# Mặc định tạo 6 Chrome profile độc lập. Có thể thêm bao nhiêu account/profile tùy ý
# bằng LOGIN_CHATGPT_ACCOUNTS.bat. Mỗi account có thể có nhiều Project.
DEFAULT_CHATGPT_ACCOUNTS = [
    {
        "key": item["key"],
        "name": item["name"],
        "profile_dir": f"chrome_profiles/chatgpt_accounts/{item['key']}",
        "enabled": True,
        "projects": [dict(item)],
    }
    for item in DEFAULT_CHATGPT_PROJECTS
]


def _safe_account_key(value):
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return value or "account"


def _normalize_chatgpt_project(project):
    if not isinstance(project, dict):
        return None
    key = str(project.get("key") or project.get("name") or "").strip()
    name = str(project.get("name") or key).strip()
    url = str(project.get("url") or "").strip().rstrip("/")
    enabled = bool(project.get("enabled", True))
    if not key or not url:
        return None
    if not re.match(r"^https://chatgpt\.com/g/g-p-[^/]+/project$", url, flags=re.I):
        return None
    project_id_match = re.search(r"/(g-p-[^/]+)/project$", url, flags=re.I)
    project_id = project_id_match.group(1) if project_id_match else ""
    return {"key": key, "name": name or key, "url": url, "project_id": project_id, "enabled": enabled}


def _normalize_chatgpt_account(account):
    if not isinstance(account, dict):
        return None
    key = _safe_account_key(account.get("key") or account.get("name"))
    name = str(account.get("name") or key).strip() or key
    enabled = bool(account.get("enabled", True))
    profile_raw = str(account.get("profile_dir") or "").strip()
    if not profile_raw:
        profile_raw = f"chrome_profiles/chatgpt_accounts/{key}"
    profile_path = Path(profile_raw)
    if not profile_path.is_absolute():
        profile_path = (BASE_DIR / profile_path).resolve()
    projects = []
    seen = set()
    raw_projects = account.get("projects", [])
    for item in raw_projects if isinstance(raw_projects, list) else []:
        project = _normalize_chatgpt_project(item)
        if not project or not project["enabled"]:
            continue
        if project["key"].lower() in seen:
            continue
        seen.add(project["key"].lower())
        projects.append(project)
    return {"key": key, "name": name, "enabled": enabled, "profile_dir": str(profile_path), "projects": projects}


def ensure_chatgpt_accounts_file():
    if CHATGPT_ACCOUNTS_FILE.exists():
        return
    payload = {
        "accounts": DEFAULT_CHATGPT_ACCOUNTS,
        "note": "Mỗi account dùng một Chrome profile riêng. Login bằng LOGIN_CHATGPT_ACCOUNTS.bat.",
    }
    CHATGPT_ACCOUNTS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_chatgpt_accounts():
    ensure_chatgpt_accounts_file()
    try:
        payload = json.loads(CHATGPT_ACCOUNTS_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"❌ Không đọc được {CHATGPT_ACCOUNTS_FILE.name}: {exc}")
        return []
    raw_accounts = payload.get("accounts", []) if isinstance(payload, dict) else payload
    accounts = []
    seen = set()
    for item in raw_accounts if isinstance(raw_accounts, list) else []:
        account = _normalize_chatgpt_account(item)
        if not account or not account["enabled"]:
            continue
        if account["key"].lower() in seen:
            continue
        seen.add(account["key"].lower())
        accounts.append(account)
    return accounts


def set_active_chatgpt_account(account):
    global CHATGPT_USER_DATA_DIR, ACTIVE_CHATGPT_ACCOUNT_KEY, ACTIVE_CHATGPT_ACCOUNT_NAME
    if not account:
        return None
    CHATGPT_USER_DATA_DIR = Path(account["profile_dir"]).resolve()
    CHATGPT_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_CHATGPT_ACCOUNT_KEY = str(account.get("key") or "")
    ACTIVE_CHATGPT_ACCOUNT_NAME = str(account.get("name") or ACTIVE_CHATGPT_ACCOUNT_KEY)
    return CHATGPT_USER_DATA_DIR


def _choose_from_list(items, title, default_index=0, label_func=None):
    if not items:
        return None
    default_index = max(0, min(int(default_index), len(items) - 1))
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    for idx, item in enumerate(items, start=1):
        label = label_func(item) if label_func else str(item)
        suffix = "  < mặc định" if idx - 1 == default_index else ""
        print(f"{idx}) {label}{suffix}")
    while True:
        raw = input(f"👉 Chọn 1-{len(items)}; ENTER = {default_index + 1}: ").strip()
        if not raw:
            return items[default_index]
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        low = raw.lower()
        for item in items:
            if low in {str(item.get("key", "")).lower(), str(item.get("name", "")).lower()}:
                return item
        print("⚠️ Lựa chọn không hợp lệ.")


def choose_chatgpt_project():
    """Chọn account/profile trước rồi chọn Project thuộc account đó."""
    accounts = load_chatgpt_accounts()
    if not accounts:
        print("❌ Không có ChatGPT account/profile hợp lệ trong chatgpt_accounts.json")
        print("👉 Chạy LOGIN_CHATGPT_ACCOUNTS.bat để tạo/login profile trước.")
        return None
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    last_account = ""
    last_project = ""
    try:
        last_account = LAST_CHATGPT_ACCOUNT_FILE.read_text(encoding="utf-8").strip().lower()
    except Exception:
        pass
    try:
        last_project = LAST_CHATGPT_PROJECT_FILE.read_text(encoding="utf-8").strip().lower()
    except Exception:
        pass
    default_account_index = next((i for i,a in enumerate(accounts) if a["key"].lower() == last_account), 0)
    account = _choose_from_list(
        accounts,
        "CHỌN CHATGPT ACCOUNT / CHROME PROFILE",
        default_account_index,
        lambda a: f"{a['name']}  [{a['key']}]  | projects={len(a['projects'])}",
    )
    if not account:
        return None
    set_active_chatgpt_account(account)
    try:
        LAST_CHATGPT_ACCOUNT_FILE.write_text(account["key"], encoding="utf-8")
    except Exception:
        pass
    projects = account.get("projects") or []
    if not projects:
        print(f"❌ Account '{account['name']}' chưa có Project URL.")
        print("👉 Chạy LOGIN_CHATGPT_ACCOUNTS.bat -> thêm Project cho account.")
        return None
    default_project_index = next((i for i,p in enumerate(projects) if f"{account['key']}|{p['key']}".lower() == last_project), 0)
    if len(projects) == 1:
        project = projects[0]
    else:
        project = _choose_from_list(
            projects,
            f"CHỌN PROJECT CHO ACCOUNT: {account['name']}",
            default_project_index,
            lambda p: f"{p['name']}  [{p['key']}]",
        )
    if not project:
        return None
    project = dict(project)
    project.update({"account_key": account["key"], "account_name": account["name"], "profile_dir": str(CHATGPT_USER_DATA_DIR)})
    try:
        LAST_CHATGPT_PROJECT_FILE.write_text(f"{account['key']}|{project['key']}", encoding="utf-8")
    except Exception:
        pass
    print(f"✅ ChatGPT account: {account['name']} [{account['key']}]")
    print(f"📁 Chrome profile: {CHATGPT_USER_DATA_DIR}")
    print(f"✅ ChatGPT Project: {project['name']}")
    print(f"📂 {project['url']}")
    return project



def build_chatgpt_rotation_targets():
    """
    Tạo danh sách xoay profile theo thứ tự trong chatgpt_accounts.json.

    Mỗi account/profile dùng Project enabled đầu tiên của chính account đó.
    Nếu account chưa có Project thì bỏ khỏi vòng xoay và in cảnh báo.
    """
    accounts = load_chatgpt_accounts()
    targets = []
    for account in accounts:
        projects = account.get("projects") or []
        if not projects:
            print(f"⚠️ Bỏ profile '{account.get('name') or account.get('key')}': chưa có Project URL.")
            continue
        project = dict(projects[0])
        project.update({
            "account_key": account["key"],
            "account_name": account["name"],
            "profile_dir": account["profile_dir"],
        })
        targets.append({"account": account, "project": project})
    return targets


def load_chatgpt_round_robin_cursor(target_count):
    """Nhớ lượt profile giữa các lần restart chương trình."""
    if target_count <= 0:
        return 0
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        value = int(CHATGPT_ROUND_ROBIN_CURSOR_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        value = 0
    return max(0, value) % target_count


def save_chatgpt_round_robin_cursor(next_index):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CHATGPT_ROUND_ROBIN_CURSOR_FILE.write_text(str(max(0, int(next_index))), encoding="utf-8")
    except Exception:
        pass


def print_chatgpt_rotation_plan(targets, start_cursor=0):
    print("\n" + "=" * 72)
    print("CHATGPT MULTI-PROFILE ROUND ROBIN")
    print("=" * 72)
    for idx, target in enumerate(targets, start=1):
        account = target["account"]
        project = target["project"]
        start_mark = "  < lượt đầu" if (idx - 1) == (start_cursor % len(targets)) else ""
        print(
            f"{idx}) {account['name']} [{account['key']}] -> "
            f"Project: {project['name']}{start_mark}"
        )
    print(f"🔄 Tổng profile trong vòng xoay: {len(targets)}")
    print("⚡ Chrome ChatGPT được giữ sống nếu video kế tiếp dùng cùng profile; chỉ đổi/restart khi thật sự cần.")


def chatgpt_project_url_matches(current_url, project):
    current = str(current_url or "")
    project_id = str((project or {}).get("project_id") or "")
    if project_id and project_id.lower() in current.lower():
        return True
    expected = str((project or {}).get("url") or "").rstrip("/")
    return bool(expected and current.rstrip("/").startswith(expected))


def open_chatgpt_project_new_chat(driver, project, timeout=None):
    """
    Mở TRANG PROJECT thay vì ChatGPT Home.
    Composer trên trang Project chính là điểm bắt đầu một chat mới trong Project.
    Không click lịch sử chat cũ, không reuse conversation cũ.
    """
    timeout = timeout or WAIT_CHATGPT_READY
    if not project:
        raise RuntimeError("Chưa chọn ChatGPT Project")
    target = str(project.get("url") or "").strip()
    if not target:
        raise RuntimeError("ChatGPT Project URL rỗng")

    print(f"📁 NEW CHAT trong Project: {project.get('name') or project.get('key')}")

    # FAST PATH: nếu đang ở đúng Project và composer đã hiện thì không driver.get() lại.
    already_ready = False
    try:
        already_ready = (
            chatgpt_project_url_matches(safe_current_url(driver), project)
            and find_chatgpt_composer(driver) is not None
        )
    except Exception:
        already_ready = False

    if already_ready:
        print("⚡ Project đã mở sẵn + có composer -> bỏ qua reload.")
    else:
        try:
            driver.get(target)
        except Exception as exc:
            raise RuntimeError(f"Không mở được Project URL: {exc}") from exc

        try:
            WebDriverWait(driver, min(timeout, 15)).until(
                lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
            )
        except Exception:
            pass

    # Passive check: nếu request sidebar /backend-api/conversations vừa bị HTTP 429,
    # KHÔNG gửi thêm API request để check; chỉ quan sát request browser đã tạo rồi
    # tự nhấn Got it nếu popup xuất hiện.
    install_chatgpt_429_network_observer(driver)
    handle_chatgpt_conversations_api_429(driver, quiet=False)

    composer = wait_chatgpt_composer(driver, timeout=timeout)
    handle_chatgpt_conversations_api_429(driver, quiet=True)

    # Nếu bị redirect ra Home/login hoặc Project khác thì không được gửi nhầm.
    current = safe_current_url(driver)
    if not chatgpt_project_url_matches(current, project):
        # Có UI versions giữ project id trong DOM dù URL được rewrite. Kiểm tra link Project hiện diện.
        project_id = project.get("project_id") or ""
        dom_has_project = False
        if project_id:
            try:
                dom_has_project = bool(driver.find_elements(By.CSS_SELECTOR, f'a[href*="{project_id}"]'))
            except Exception:
                dom_has_project = False
        if not dom_has_project:
            raise RuntimeError(
                f"ChatGPT không ở đúng Project '{project.get('name')}'. URL hiện tại: {current}"
            )

    # Xóa draft mà ChatGPT có thể restore ở Project page.
    # Không coi draft cũ là lỗi login. Tự xóa nhiều lớp trước.
    residual = _loose_compare_text(get_chatgpt_composer_text(driver, composer))
    if residual:
        print(f"🧹 Project restore draft cũ ({len(residual)} chars) -> đang tự xóa...")

    for clear_try in range(1, 5):
        composer = find_chatgpt_composer(driver) or composer
        clear_chatgpt_composer(composer)
        sleep(0.35)
        composer = find_chatgpt_composer(driver) or composer
        residual = _loose_compare_text(get_chatgpt_composer_text(driver, composer))
        if not residual:
            if clear_try > 1:
                print(f"✅ Đã tự xóa draft Project ở lượt {clear_try}/4.")
            break
        print(f"   ⚠️ Draft vẫn còn {len(residual)} chars sau clear {clear_try}/4.")

    # Một số phiên ChatGPT restore draft muộn sau navigation.
    # Refresh Project đúng 1 lần rồi clear lại, thay vì bắt user login vô lý.
    if residual:
        print("🔄 Draft bị restore lại -> refresh Project 1 lần rồi tự xóa tiếp...")
        try:
            driver.get(target)
            WebDriverWait(driver, min(timeout, 45)).until(lambda d: find_chatgpt_composer(d))
            sleep(0.8)
            composer = find_chatgpt_composer(driver)
            for clear_try in range(1, 4):
                clear_chatgpt_composer(composer)
                sleep(0.4)
                composer = find_chatgpt_composer(driver) or composer
                residual = _loose_compare_text(get_chatgpt_composer_text(driver, composer))
                if not residual:
                    print("✅ Draft Project đã được xóa sau refresh.")
                    break
        except Exception:
            pass

    if residual:
        raise RuntimeError(
            f"Composer Project còn draft cũ ({len(residual)} chars) sau AUTO-CLEAR; "
            "đây không phải lỗi login."
        )

    print("✅ Đúng Project + composer sạch. Sẵn sàng tạo chat mới.")
    return composer


# ============================================================
# BASIC UTIL
# ============================================================

def sleep(seconds):
    time.sleep(seconds)


def configure_console():
    if os.name == "nt":
        try:
            os.system("chcp 65001 > nul")
        except OSError:
            pass


def safe_current_url(driver):
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def normalize_youtube_url(url):
    url = (url or "").strip()
    if not url:
        return None

    if "youtu.be/" in url:
        try:
            video_id = urlparse(url).path.strip("/").split("/")[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
        except Exception:
            pass

    if "youtube.com" in url:
        try:
            parsed = urlparse(url)
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [None])[0]
                if video_id:
                    return f"https://www.youtube.com/watch?v={video_id}"
        except Exception:
            pass

    return url


def video_id_from_url(url):
    try:
        parsed = urlparse(url)
        if "youtu.be" in parsed.netloc:
            return parsed.path.strip("/").split("/")[0]
        if "youtube.com" in parsed.netloc:
            return parse_qs(parsed.query).get("v", [""])[0]
    except Exception:
        pass
    return ""


def safe_name(text, fallback="video"):
    text = re.sub(r'[\\/:*?"<>|]', "", str(text or "")).strip()
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return (text or fallback)[:160]


def ensure_dirs():
    # Các thư mục cũ vẫn được tạo để tương thích, nhưng dữ liệu mới sẽ vào channels/<kênh>/...
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    AI_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    AI_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    GLOBAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)



# ============================================================
# CHANNEL WORKSPACE - QUY MÔ LỚN
# ============================================================


def fetch_video_metadata(video_url, js_arguments, youtube_driver=None, cookie_file=None):
    """
    Lấy title/channel/channel_id nhưng TUYỆT ĐỐI không để yt-dlp metadata treo pipeline.

    Thứ tự mới (nhanh hơn):
      1) Đọc metadata trực tiếp từ tab YouTube đang mở bằng Selenium/JS.
      2) Chỉ khi còn thiếu mới gọi yt-dlp public, có TIMEOUT cứng.
      3) Nếu vẫn thiếu mới gọi yt-dlp + cookie LIVE, cũng có TIMEOUT cứng.

    Mục tiêu: transcript/cookie đã xong thì phải đi tiếp ngay tới ChatGPT,
    không đứng im hàng phút chỉ vì --dump-single-json bị treo.
    """

    METADATA_YTDLP_TIMEOUT = 12

    def merge_into(base, extra):
        if not extra:
            return base
        if not base:
            base = {}
        for key, value in extra.items():
            if (not base.get(key)) and value:
                base[key] = value
        return base

    def from_browser():
        if youtube_driver is None:
            return None
        try:
            page = youtube_driver.execute_script(
                r"""
                const pr = window.ytInitialPlayerResponse || {};
                const vd = pr.videoDetails || {};
                const mf = (pr.microformat && pr.microformat.playerMicroformatRenderer) || {};

                const metaTitle =
                    document.querySelector('meta[name="title"]')?.content ||
                    document.querySelector('meta[property="og:title"]')?.content ||
                    '';

                const ownerName =
                    document.querySelector('ytd-watch-metadata ytd-channel-name a')?.textContent?.trim() ||
                    document.querySelector('#owner #channel-name a')?.textContent?.trim() ||
                    '';

                let channelId = vd.channelId || mf.externalChannelId || '';

                if (!channelId) {
                    const ownerHref =
                        document.querySelector('ytd-watch-metadata ytd-channel-name a')?.href ||
                        document.querySelector('#owner #channel-name a')?.href ||
                        '';
                    const m = String(ownerHref).match(/\/channel\/(UC[\w-]+)/);
                    if (m) channelId = m[1];
                }

                return {
                    title: vd.title || mf.title?.simpleText || metaTitle || document.title || '',
                    channel: vd.author || mf.ownerChannelName || ownerName || '',
                    channel_id: channelId || '',
                };
                """
            ) or {}

            page_title = str(page.get("title") or "").replace(" - YouTube", "").strip()
            page_channel = str(page.get("channel") or "").strip()
            page_channel_id = str(page.get("channel_id") or "").strip()

            if not (page_title or page_channel or page_channel_id):
                return None

            return {
                "title": page_title or "video",
                "channel": page_channel,
                "channel_id": page_channel_id,
                "uploader": page_channel,
                "uploader_id": page_channel_id,
                "webpage_url": video_url,
            }
        except Exception as exc:
            print(f"⚠️ Metadata từ tab YouTube chưa lấy được: {type(exc).__name__}")
            return None

    def run_ytdlp(cookie=None, label="public"):
        command = [
            *cutter.YTDLP_CMD,
            "--ignore-config",
            "--no-plugin-dirs",
            *js_arguments,
            "--remote-components", "ejs:github",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
        ]

        if cookie:
            cp = Path(cookie)
            if cp.exists() and cp.stat().st_size > 0:
                command.extend(["--cookies", str(cp)])

        command.append(video_url)

        try:
            print(f"   🔎 yt-dlp metadata ({label}) | timeout={METADATA_YTDLP_TIMEOUT}s...")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=METADATA_YTDLP_TIMEOUT,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                return {
                    "title": data.get("title") or "video",
                    "channel": data.get("channel") or data.get("uploader") or "",
                    "channel_id": data.get("channel_id") or data.get("uploader_id") or "",
                    "uploader": data.get("uploader") or "",
                    "uploader_id": data.get("uploader_id") or "",
                    "webpage_url": data.get("webpage_url") or video_url,
                }

            stderr = (result.stderr or "").strip().replace("\n", " ")
            if stderr:
                print(f"   ⚠️ yt-dlp metadata {label} lỗi: {stderr[:220]}")
        except subprocess.TimeoutExpired:
            print(
                f"   ⏱️ yt-dlp metadata ({label}) quá {METADATA_YTDLP_TIMEOUT}s -> "
                "BỎ QUA, không cho treo pipeline."
            )
        except Exception as exc:
            print(f"   ⚠️ yt-dlp metadata ({label}) exception: {type(exc).__name__}: {exc}")
        return None

    # 1) ƯU TIÊN TAB YOUTUBE HIỆN TẠI - thường gần như tức thì.
    data = from_browser()
    if data:
        print(
            "   ✅ Metadata từ Chrome YouTube: "
            f"title={'CÓ' if data.get('title') else 'KHÔNG'} | "
            f"channel={'CÓ' if data.get('channel') else 'KHÔNG'} | "
            f"channel_id={'CÓ' if data.get('channel_id') else 'KHÔNG'}"
        )

    # Nếu browser đã có đủ 3 trường cần thiết thì KHÔNG gọi yt-dlp metadata nữa.
    if data and data.get("title") and data.get("channel") and data.get("channel_id"):
        return data

    # 2) PUBLIC yt-dlp chỉ là fallback, có timeout.
    public_data = run_ytdlp(label="public")
    data = merge_into(data, public_data)

    if data and data.get("title") and data.get("channel") and data.get("channel_id"):
        return data

    # 3) Cookie LIVE là fallback cuối, cũng có timeout.
    if cookie_file:
        auth_data = run_ytdlp(cookie=cookie_file, label="LIVE cookie")
        data = merge_into(data, auth_data)

    if not data:
        data = {}

    vid = video_id_from_url(video_url) or "UNKNOWN"
    return {
        "title": data.get("title") or "video",
        "channel": data.get("channel") or data.get("uploader") or "UNKNOWN_CHANNEL",
        "channel_id": data.get("channel_id") or data.get("uploader_id") or f"UNKNOWN_{vid}",
        "uploader": data.get("uploader") or "",
        "uploader_id": data.get("uploader_id") or "",
        "webpage_url": data.get("webpage_url") or video_url,
    }

def _load_channel_index():
    if not CHANNEL_INDEX_FILE.exists():
        return {}
    try:
        data = json.loads(CHANNEL_INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_channel_index(index):
    tmp = CHANNEL_INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CHANNEL_INDEX_FILE)



def resolve_channel_workspace(metadata):
    """
    Folder luôn có dạng:
        CHANNEL NAME _ CHANNEL ID

    Với cùng channel_id:
    - tìm/reuse folder cũ kết thúc bằng " _ CHANNEL_ID";
    - không tạo folder mới chỉ vì channel đổi tên;
    - có thể bật RENAME_CHANNEL_FOLDER_WHEN_NAME_CHANGES nếu muốn rename folder cũ.
    """
    channel_title = metadata.get("channel") or metadata.get("uploader") or "UNKNOWN_CHANNEL"
    channel_id = metadata.get("channel_id") or metadata.get("uploader_id") or "UNKNOWN_CHANNEL_ID"

    clean_title = safe_name(channel_title, "UNKNOWN_CHANNEL")
    clean_id = safe_name(channel_id, "UNKNOWN_CHANNEL_ID")
    desired_name = safe_name(f"{clean_title} _ {clean_id}", f"UNKNOWN_CHANNEL _ {clean_id}")

    index = _load_channel_index()
    folder_name = index.get(channel_id)

    # Index cũ có thể chưa tồn tại hoặc user đã di chuyển project.
    if not folder_name or not (CHANNELS_DIR / folder_name).exists():
        suffix = f" _ {clean_id}".lower()
        candidates = [
            item.name
            for item in CHANNELS_DIR.iterdir()
            if item.is_dir() and item.name.lower().endswith(suffix)
        ] if CHANNELS_DIR.exists() else []

        if candidates:
            folder_name = sorted(candidates)[0]
        else:
            folder_name = desired_name

    old_root = CHANNELS_DIR / folder_name

    # Migration từ bản code cũ: nếu folder chưa có " _ CHANNEL_ID" thì đổi sang format mới.
    has_required_suffix = folder_name.lower().endswith(f" _ {clean_id}".lower())
    if (
        not has_required_suffix
        and old_root.exists()
        and not (CHANNELS_DIR / desired_name).exists()
    ):
        try:
            old_root.rename(CHANNELS_DIR / desired_name)
            folder_name = desired_name
            old_root = CHANNELS_DIR / folder_name
            print(f"📦 Đã migrate folder kênh sang format mới: {folder_name}")
        except OSError:
            pass

    # Channel đổi tên: mặc định giữ folder đã có cùng ID để tránh move dữ liệu lớn.
    if (
        RENAME_CHANNEL_FOLDER_WHEN_NAME_CHANGES
        and folder_name != desired_name
        and old_root.exists()
        and not (CHANNELS_DIR / desired_name).exists()
    ):
        try:
            old_root.rename(CHANNELS_DIR / desired_name)
            folder_name = desired_name
        except OSError:
            pass

    index[channel_id] = folder_name
    _save_channel_index(index)

    root = CHANNELS_DIR / folder_name
    workspace = {
        "root": root,
        "done": root / "done",
        "transcripts": root / "transcripts",
        "ai_inputs": root / "ai_inputs",
        "ai_results": root / "ai_results",
        "logs": root / "logs",
        "done_links": root / "done_links.txt",
        "channel_title": channel_title,
        "channel_id": channel_id,
        "folder_name": folder_name,
    }

    for key in ("done", "transcripts", "ai_inputs", "ai_results", "logs"):
        workspace[key].mkdir(parents=True, exist_ok=True)

    info_path = root / "channel_info.json"
    info = {
        "channel": channel_title,
        "channel_id": channel_id,
        "folder": folder_name,
        "last_seen_channel_name": channel_title,
    }
    try:
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    return workspace

def append_channel_done_link(workspace, video_url):
    path = workspace["done_links"]
    existing = set()
    if path.exists():
        existing = {ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()}
    if video_url not in existing:
        with open(path, "a", encoding="utf-8") as f:
            f.write(video_url + "\n")


def load_global_done_set():
    done = set()
    if GLOBAL_DONE_FILE.exists():
        try:
            for line in GLOBAL_DONE_FILE.read_text(encoding="utf-8-sig").splitlines():
                url = normalize_youtube_url(line.strip())
                if url:
                    done.add(url)
        except OSError:
            pass
    return done


def append_global_done_link(video_url):
    """Append-only: không rewrite list.txt, phù hợp hàng chục nghìn link."""
    with open(GLOBAL_DONE_FILE, "a", encoding="utf-8", newline="\n") as f:
        f.write(video_url + "\n")



def load_unresolved_failed_set(done_set=None):
    """
    Trả về các URL từng FAIL nhưng CHƯA DONE.

    failedLink.jsonl là log lịch sử append-only nên một link có thể vừa nằm trong
    failedLink.jsonl vừa nằm trong doneLink.txt sau khi retry thành công.
    Vì vậy phải lấy: FAILED - DONE.

    Parser cố chịu được cả JSONL chuẩn lẫn vài dòng log cũ bị lỗi format.
    """
    done_set = set(done_set or load_global_done_set())
    failed = set()

    if not GLOBAL_FAILED_FILE.exists():
        return failed

    try:
        with open(GLOBAL_FAILED_FILE, "r", encoding="utf-8-sig", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue

                candidates = []

                # JSONL chuẩn.
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        value = str(obj.get("url") or "").strip()
                        if value:
                            candidates.append(value)
                except Exception:
                    pass

                # Fallback cho log cũ/malformed.
                if not candidates:
                    candidates.extend(
                        re.findall(
                            r'https?://(?:www\.)?(?:youtube\.com/watch\?v=[A-Za-z0-9_-]{6,}|youtu\.be/[A-Za-z0-9_-]{6,})[^\s"\'<>]*',
                            line,
                            flags=re.I,
                        )
                    )

                for candidate in candidates:
                    url = normalize_youtube_url(candidate)
                    if url and url not in done_set:
                        failed.add(url)
    except OSError:
        pass

    return failed


def append_failure(video_url, stage, error, channel_id="", title=""):
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": video_url,
        "video_id": video_id_from_url(video_url),
        "channel_id": channel_id,
        "title": title,
        "stage": stage,
        "error": str(error)[:4000],
    }
    try:
        with open(GLOBAL_FAILED_FILE, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def append_no_story(video_url, workspace=None, title=""):
    """AI xác định cắt 00:00:00 --> END: coi là đã phân loại xong, không download MP3."""
    line = video_url + "\n"
    try:
        with open(GLOBAL_NO_STORY_FILE, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
    except OSError:
        pass

    if workspace:
        try:
            path = workspace["root"] / "no_story_links.txt"
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(video_url + "\n")
        except OSError:
            pass


def ai_requests_full_cut(parsed):
    """Nhận trường hợp rõ ràng 00:00:00 --> END trước khi tốn thời gian download."""
    if not parsed or parsed.get("keep_all"):
        return False
    for start_sec, end_sec in parsed.get("cut_ranges", []):
        if start_sec <= 0.0 and end_sec == float("inf"):
            return True
    return False


def write_video_log(workspace, video_id, message):
    try:
        path = workspace["logs"] / f"{safe_name(video_id, 'video')}.log"
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def find_existing_final(workspace, video_id):
    if not video_id:
        return None
    try:
        matches = list(workspace["done"].glob(f"* [{video_id}].mp3"))
        for path in matches:
            if path.is_file() and path.stat().st_size > 0:
                return path
    except Exception:
        pass
    return None


def check_free_space(path, minimum_gb=MIN_FREE_GB):
    try:
        usage = shutil.disk_usage(Path(path).resolve())
        free_gb = usage.free / (1024 ** 3)
        if free_gb < minimum_gb:
            print(f"❌ Ổ đĩa chỉ còn {free_gb:.2f} GB; yêu cầu tối thiểu {minimum_gb:.2f} GB.")
            return False
        print(f"💽 Dung lượng trống: {free_gb:.2f} GB")
        return True
    except Exception:
        return True

# ============================================================
# CHROME / SELENIUM
# ============================================================

def create_youtube_driver():
    """YouTube vẫn dùng Selenium riêng với profile local của chính project."""
    YOUTUBE_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    options = webdriver.ChromeOptions()
    options.binary_location = CHROME_BINARY
    options.add_argument(f"--user-data-dir={YOUTUBE_USER_DATA_DIR}")
    options.add_argument(f"--profile-directory={PROFILE_DIRECTORY}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--start-maximized")
    options.add_argument("--log-level=3")
    # EAGER: Selenium không chờ toàn bộ ảnh/quảng cáo/resource phụ tải xong.
    # DOM interactive là đủ cho transcript/player bootstrap.
    options.page_load_strategy = "eager"

    # Cần performance log để fallback bắt response get_panel thật của YouTube.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    print("🚀 Mở Chrome YouTube bằng Selenium...")
    print(f"🌐 Chrome binary: {CHROME_BINARY}")
    print(f"📁 YouTube profile: {YOUTUBE_USER_DATA_DIR}\\{PROFILE_DIRECTORY}")

    try:
        # Selenium 4 có Selenium Manager, không cần webdriver-manager.
        driver = webdriver.Chrome(options=options)
        try:
            driver.set_page_load_timeout(WAIT_PAGE)
        except Exception:
            pass
        try:
            driver.set_script_timeout(20)
        except Exception:
            pass

        try:
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

        driver.get(YOUTUBE_HOME)
        print("✅ Chrome YouTube đã mở.")
        return driver

    except WebDriverException as exc:
        print("\n❌ Không mở được Chrome YouTube Selenium.")
        print("Hãy đóng Chrome đang dùng profile youtube trong thư mục chrome_profiles rồi chạy lại.")
        print(exc)
        return None



def chatgpt_debug_url(path="/json/version"):
    return f"http://{CHATGPT_DEBUG_HOST}:{CHATGPT_DEBUG_PORT}{path}"


def _debug_port_has_chatgpt(port):
    try:
        with urlopen(f"http://{CHATGPT_DEBUG_HOST}:{port}/json/list", timeout=1.2) as response:
            items = json.loads(response.read().decode("utf-8", "replace"))
        for item in items if isinstance(items, list) else []:
            url = str(item.get("url") or "")
            if "chatgpt.com" in url or "auth.openai.com" in url:
                return True
    except Exception:
        pass
    return False


def _choose_chatgpt_debug_port():
    """Reuse port chỉ khi marker xác nhận đúng account/profile hiện tại."""
    global CHATGPT_DEBUG_PORT
    saved_account = ""
    try:
        saved_account = CHATGPT_DEBUG_ACCOUNT_FILE.read_text(encoding="utf-8").strip().lower()
    except Exception:
        pass
    active_account = str(ACTIVE_CHATGPT_ACCOUNT_KEY or "").strip().lower()
    try:
        if CHATGPT_DEBUG_PORT_FILE.exists():
            saved = int(CHATGPT_DEBUG_PORT_FILE.read_text(encoding="utf-8").strip())
            if 1024 <= saved <= 65535 and saved_account and saved_account == active_account and _debug_port_has_chatgpt(saved):
                CHATGPT_DEBUG_PORT = saved
                return saved, True
    except Exception:
        pass
    for port in range(9222, 9251):
        try:
            with urlopen(f"http://{CHATGPT_DEBUG_HOST}:{port}/json/version", timeout=0.35):
                continue
        except Exception:
            CHATGPT_DEBUG_PORT = port
            return port, False
    return CHATGPT_DEBUG_PORT, False


def chatgpt_debug_port_ready(timeout=1.0):
    try:
        with urlopen(chatgpt_debug_url(), timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False



def launch_chatgpt_manual_chrome(start_url=None):
    """
    Mở Chrome ChatGPT THẬT bằng subprocess + remote debugging.
    Mở Chrome thật trước rồi Selenium attach vào session thật; không dừng tay nếu session còn hợp lệ.
    """
    CHATGPT_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    port, reusable = _choose_chatgpt_debug_port()
    if reusable and chatgpt_debug_port_ready():
        print(f"✅ Reuse đúng Chrome ChatGPT của project ở cổng debug {port}.")
        return None

    if not Path(CHROME_BINARY).exists():
        print(f"❌ Không tìm thấy Chrome: {CHROME_BINARY}")
        return False

    start_url = str(start_url or CHATGPT_HOME).strip() or CHATGPT_HOME
    command = [
        CHROME_BINARY,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={CHATGPT_USER_DATA_DIR}",
        f"--profile-directory={PROFILE_DIRECTORY}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        start_url,
    ]

    print("\n🚀 Mở Chrome ChatGPT THẬT (chưa attach Selenium)...")
    if ACTIVE_CHATGPT_ACCOUNT_NAME:
        print(f"👤 ChatGPT account/profile: {ACTIVE_CHATGPT_ACCOUNT_NAME} [{ACTIVE_CHATGPT_ACCOUNT_KEY}]")
    print(f"📁 ChatGPT profile: {CHATGPT_USER_DATA_DIR}\\{PROFILE_DIRECTORY}")
    print(f"🔌 Debug port: {port}")

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        print(f"❌ Không mở được Chrome ChatGPT: {exc}")
        return False

    deadline = time.time() + 25
    while time.time() < deadline:
        if chatgpt_debug_port_ready():
            try:
                CHATGPT_DEBUG_PORT_FILE.write_text(str(port), encoding="utf-8")
                CHATGPT_DEBUG_ACCOUNT_FILE.write_text(str(ACTIVE_CHATGPT_ACCOUNT_KEY or ""), encoding="utf-8")
            except OSError:
                pass
            print("✅ Chrome ChatGPT đã mở và remote debugging sẵn sàng.")
            return process
        sleep(0.4)

    print(f"❌ Chrome đã mở nhưng không thấy cổng remote debugging {port}.")
    print("Hãy đóng Chrome ChatGPT của project rồi chạy lại.")
    return False


def chatgpt_really_needs_manual_auth(driver):
    """
    Chỉ trả True khi có dấu hiệu RÕ RÀNG là login/challenge.
    Generic Selenium Timeout / Project load chậm KHÔNG được coi là logout.
    """
    if not driver_alive(driver):
        return False

    url = safe_current_url(driver).lower()
    if "auth.openai.com" in url:
        return True
    if any(x in url for x in ("/auth/login", "/login", "/signup")):
        return True

    try:
        body = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    except Exception:
        body = ""

    hard_markers = (
        "verify you are human",
        "checking your browser",
        "cloudflare",
        "just a moment",
        "log in to chatgpt",
        "login to chatgpt",
        "sign in to chatgpt",
    )
    if any(marker in body for marker in hard_markers):
        return True

    # Nút login/sign up trên trang ChatGPT nhưng KHÔNG có composer.
    try:
        if find_chatgpt_composer(driver):
            return False
    except Exception:
        pass

    try:
        buttons = driver.find_elements(By.CSS_SELECTOR, "button, a")
        for el in buttons[:120]:
            try:
                txt = " ".join([
                    el.text or "",
                    el.get_attribute("aria-label") or "",
                    el.get_attribute("href") or "",
                ]).strip().lower()
                if txt in {"log in", "login", "sign in"} or "/auth/login" in txt:
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def auto_open_project_with_retries(driver, project, attempts=4, timeout=None):
    """
    Tự vào Project/composer nhiều lần.
    - Timeout/load chậm/draft/UI chưa render: tự retry/refresh.
    - Chỉ báo MANUAL_AUTH khi thật sự thấy login/challenge.
    """
    timeout = timeout or WAIT_CHATGPT_READY
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            open_chatgpt_project_new_chat(driver, project, timeout=timeout)
            return True, False, None
        except Exception as exc:
            last_exc = exc
            err = str(exc).strip() or type(exc).__name__
            print(f"   ⚠️ Project/composer chưa sẵn sàng {attempt}/{attempts}: {err}")

            if chatgpt_really_needs_manual_auth(driver):
                print("   🔐 Phát hiện login/challenge thật sự.")
                return False, True, exc

            # Generic timeout / UI load chậm: không gọi là logout.
            try:
                target = str((project or {}).get("url") or CHATGPT_HOME).strip() or CHATGPT_HOME
                if attempt == 1:
                    sleep(0.50)
                elif attempt == 2:
                    print("   🔄 Reload lại đúng Project...")
                    driver.get(target)
                    sleep(2.0)
                elif attempt == 3:
                    print("   🔄 Refresh Project rồi thử lần cuối...")
                    try:
                        driver.refresh()
                    except Exception:
                        driver.get(target)
                    sleep(2.5)
            except Exception:
                sleep(1.5)

    return False, False, last_exc


def wait_manual_chatgpt_login_before_attach():
    """
    DỪNG CỨNG trước khi Selenium attach.
    Người dùng phải tự hoàn tất login / Cloudflare và nhìn thấy ô chat rồi mới ENTER.
    """
    print("\n" + "=" * 72)
    print(" CHATGPT - ĐĂNG NHẬP / XÁC MINH BẰNG TAY")
    print("=" * 72)
    print("Chrome ChatGPT hiện đang chạy như Chrome bình thường.")
    print("Ở thời điểm này Selenium CHƯA điều khiển tab ChatGPT.")
    print()
    print("1) Tự đăng nhập ChatGPT.")
    print("2) Nếu có 'Verify you are human' / Cloudflare thì tự xác minh.")
    print("3) Chỉ khi đã vào chat và NHÌN THẤY ô nhập tin nhắn mới quay lại console.")
    print("4) Nhấn ENTER để code attach Selenium và chạy tiếp.")
    print()

    while True:
        command = input("👉 Đã vào được ChatGPT và thấy ô nhập chưa? ENTER = tiếp tục, QUIT = dừng: ").strip().upper()
        if command in {"QUIT", "EXIT", "THOAT"}:
            raise cutter.UserQuit()
        if chatgpt_debug_port_ready():
            return True
        print("⚠️ Chưa thấy cổng debug của Chrome ChatGPT. Hãy kiểm tra Chrome vẫn đang mở.")


def attach_chatgpt_driver():
    """Attach Selenium vào Chrome ChatGPT thật đang chạy qua remote debugging."""
    options = webdriver.ChromeOptions()

    # QUAN TRỌNG: chỉ rõ Chrome binary cả khi ATTACH.
    # Một số máy Chrome có ở Program Files nhưng không nằm trong PATH (`where chrome` không thấy).
    # Nếu thiếu dòng này Selenium Manager có thể tưởng máy chưa có Chrome và báo cài Chrome/browser.
    options.binary_location = CHROME_BINARY

    options.add_experimental_option(
        "debuggerAddress",
        f"{CHATGPT_DEBUG_HOST}:{CHATGPT_DEBUG_PORT}",
    )

    print("🔗 Đang attach Selenium vào Chrome ChatGPT đã login...")
    print(f"🌐 Chrome binary: {CHROME_BINARY}")

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:
        print(f"❌ Attach Chrome ChatGPT thất bại: {exc}")
        return None

    # Attach thành công là đủ. Composer có thể render chậm hoặc trang đang ở Home/Project
    # chưa load xong; caller sẽ tự navigate + retry. Không được biến generic timeout thành
    # "cần login thủ công".
    try:
        wait_chatgpt_composer(driver, timeout=12)
        print("✅ Attach thành công. Đã thấy composer ChatGPT.")
    except Exception:
        print("✅ Attach Selenium thành công; composer chưa render ngay -> sẽ tự vào Project/retry.")

    install_chatgpt_429_network_observer(driver)
    handle_chatgpt_conversations_api_429(driver, quiet=True)
    return driver



def driver_alive(driver):
    if driver is None:
        return False
    try:
        driver.execute_script("return 1;")
        return True
    except Exception:
        return False


def ensure_chatgpt_ready_interactive(driver):
    """
    Nếu session ChatGPT logout / Cloudflare xuất hiện giữa batch, dừng an toàn để user xử lý.
    Không tự bypass challenge.
    """
    if not driver_alive(driver):
        return False
    try:
        wait_chatgpt_composer(driver, timeout=8)
        return True
    except Exception:
        print("\n⚠️ ChatGPT không còn thấy ô nhập (có thể logout/rate page/Cloudflare).")
        print("Hãy xử lý trực tiếp trong cửa sổ Chrome ChatGPT.")
        while True:
            raw = input("👉 Khi thấy lại ô nhập ChatGPT, nhấn ENTER; gõ QUIT để dừng: ").strip().upper()
            if raw in {"QUIT", "EXIT", "THOAT"}:
                raise cutter.UserQuit()
            try:
                wait_chatgpt_composer(driver, timeout=10)
                print("✅ ChatGPT đã sẵn sàng lại.")
                return True
            except Exception:
                print("⚠️ Vẫn chưa thấy ô nhập ChatGPT.")




def _wait_chatgpt_debug_port_closed(timeout=None):
    """Đợi Chrome ChatGPT thật sự đóng để lần mở sau tạo process sạch/RAM sạch."""
    timeout = CHATGPT_RESTART_CLOSE_WAIT if timeout is None else timeout
    deadline = time.time() + max(1, float(timeout))
    while time.time() < deadline:
        if not chatgpt_debug_port_ready(timeout=0.35):
            return True
        sleep(0.35)
    return not chatgpt_debug_port_ready(timeout=0.35)


def close_chatgpt_browser_hard(driver=None, process=None):
    """
    Đóng HẲN Chrome ChatGPT đang dùng remote-debugging để giải phóng RAM.
    Không xóa chrome_profiles nên login/cookie vẫn được giữ.
    """
    print("🧹 Đang đóng hẳn Chrome ChatGPT để giải phóng RAM...")

    # Ưu tiên CDP Browser.close vì driver đang attach vào Chrome thật.
    if driver is not None:
        try:
            driver.execute_cdp_cmd("Browser.close", {})
        except Exception:
            try:
                driver.quit()
            except Exception:
                pass

    if _wait_chatgpt_debug_port_closed(timeout=8):
        try:
            CHATGPT_DEBUG_PORT_FILE.unlink(missing_ok=True)
            CHATGPT_DEBUG_ACCOUNT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        print("✅ Chrome ChatGPT đã đóng sạch.")
        return True

    # Fallback: nếu process do chính code mở còn sống thì terminate/kill.
    if process not in (None, False):
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
        except Exception:
            pass

    if _wait_chatgpt_debug_port_closed(timeout=6):
        try:
            CHATGPT_DEBUG_PORT_FILE.unlink(missing_ok=True)
            CHATGPT_DEBUG_ACCOUNT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        print("✅ Chrome ChatGPT đã đóng sạch sau fallback.")
        return True

    # Windows fallback cuối: taskkill đúng PID process mà code đã launch.
    if os.name == "nt" and process not in (None, False):
        try:
            pid = int(getattr(process, "pid", 0) or 0)
            if pid > 0:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        except Exception:
            pass

    closed = _wait_chatgpt_debug_port_closed(timeout=6)
    if closed:
        try:
            CHATGPT_DEBUG_PORT_FILE.unlink(missing_ok=True)
            CHATGPT_DEBUG_ACCOUNT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    print("✅ Chrome ChatGPT đã đóng sạch." if closed else "⚠️ Chưa xác nhận Chrome ChatGPT đã đóng hoàn toàn.")
    return closed



def open_chatgpt_rotation_target(account, project, old_driver=None, old_process=None):
    """
    Chuyển sang đúng Chrome profile + Project cho MỘT lượt round-robin.

    Bản AUTO:
    - Mở Chrome thật -> attach Selenium ngay.
    - Generic timeout / Project load chậm: tự retry, KHÔNG hỏi ENTER.
    - Đóng/mở Chrome lại 1 lần nếu cần.
    - Chỉ dừng login thủ công khi phát hiện RÕ auth.openai.com/login/challenge.
    """

    target_key = str((account or {}).get("key") or "").strip()
    same_profile = (
        old_driver is not None
        and driver_alive(old_driver)
        and target_key
        and str(ACTIVE_CHATGPT_ACCOUNT_KEY or "").strip() == target_key
    )

    # FAST PATH:
    # Nếu video kế tiếp vẫn dùng cùng account/profile thì GIỮ NGUYÊN Chrome thật.
    # Chỉ đưa tab về đúng Project + composer sạch, không kill/open/attach lại.
    if same_profile:
        set_active_chatgpt_account(account)
        project = dict(project or {})
        project.update({
            "account_key": account.get("key", ""),
            "account_name": account.get("name", account.get("key", "")),
            "profile_dir": str(CHATGPT_USER_DATA_DIR),
        })
        try:
            current_url = safe_current_url(old_driver)
            composer = find_chatgpt_composer(old_driver)

            if composer and chatgpt_project_url_matches(current_url, project):
                # Đã đúng Project rồi: clear draft và đi luôn, không navigate lại.
                residual = _loose_compare_text(get_chatgpt_composer_text(old_driver, composer))
                if residual:
                    print(f"🧹 Same profile: xóa draft còn lại ({len(residual)} chars)...")
                    clear_chatgpt_composer(composer)
                    sleep(0.18)

                composer = find_chatgpt_composer(old_driver)
                residual = _loose_compare_text(get_chatgpt_composer_text(old_driver, composer)) if composer else "x"
                if composer and not residual:
                    print("⚡ FAST CHATGPT: giữ nguyên Chrome/profile + Project hiện tại.")
                    return old_driver, old_process, project

            # Không đúng Project hoặc composer chưa có: navigation/retry nhẹ, vẫn không restart.
            ok, manual_auth, exc = auto_open_project_with_retries(
                old_driver,
                project,
                attempts=3,
                timeout=min(WAIT_CHATGPT_READY, 15),
            )
            if ok:
                print("⚡ FAST CHATGPT: reuse Chrome hiện tại, không restart.")
                return old_driver, old_process, project
            if manual_auth:
                print("🔐 Same profile phát hiện auth/challenge thật; mới chuyển sang flow login.")
            else:
                print(
                    "⚠️ Reuse Chrome chưa được; mới fallback sang restart profile."
                    + (f" ({str(exc).strip()})" if exc else "")
                )
        except Exception as exc:
            print(f"⚠️ FAST reuse lỗi tạm thời -> fallback restart: {type(exc).__name__}: {exc}")

    if old_driver is not None or old_process not in (None, False):
        close_chatgpt_browser_hard(old_driver, old_process)
        sleep(0.7)

    set_active_chatgpt_account(account)
    project = dict(project or {})
    project.update({
        "account_key": account.get("key", ""),
        "account_name": account.get("name", account.get("key", "")),
        "profile_dir": str(CHATGPT_USER_DATA_DIR),
    })

    print("\n" + "=" * 72)
    print("🔄 CHUYỂN CHATGPT PROFILE")
    print(f"👤 Account/profile: {account.get('name')} [{account.get('key')}]")
    print(f"📁 Chrome data: {CHATGPT_USER_DATA_DIR}")
    print(f"📂 Project: {project.get('name')}")
    print("=" * 72)

    start_url = project.get("url") or CHATGPT_HOME

    # Tối đa 2 vòng browser: vòng đầu bình thường, vòng 2 là restart recovery.
    for browser_try in range(1, 3):
        new_process = launch_chatgpt_manual_chrome(start_url)
        if new_process is False:
            raise RuntimeError(f"Không mở được Chrome ChatGPT profile {account.get('key')}")

        new_driver = attach_chatgpt_driver()
        if new_driver:
            ok, manual_auth, exc = auto_open_project_with_retries(
                new_driver, project, attempts=4, timeout=WAIT_CHATGPT_READY
            )
            if ok:
                print("✅ Profile đã login; vào đúng Project tự động.")
                return new_driver, new_process, project

            if manual_auth:
                print(f"⚠️ Profile '{account.get('name')}' thật sự cần login/xác minh.")
                wait_manual_chatgpt_login_before_attach()
                # Browser vẫn đang mở; attach có thể đang tồn tại. Dùng driver hiện tại trước.
                if not driver_alive(new_driver):
                    new_driver = attach_chatgpt_driver()
                ok2, _, exc2 = auto_open_project_with_retries(
                    new_driver, project, attempts=3, timeout=WAIT_CHATGPT_READY
                )
                if ok2:
                    print("✅ Login/xác minh xong; profile đã sẵn sàng.")
                    return new_driver, new_process, project
                raise RuntimeError(
                    f"Đã login nhưng vẫn không vào được Project {project.get('name')}: "
                    f"{str(exc2).strip() or type(exc2).__name__}"
                )

            err = str(exc).strip() if exc else ""
            print(
                f"⚠️ Không phải lỗi login; Project/UI chưa sẵn sàng sau auto retry"
                f"{(': ' + err) if err else ''}"
            )

            # Recovery browser tự động đúng 1 lần.
            if browser_try == 1:
                print("♻️ Tự restart Chrome ChatGPT 1 lần rồi thử lại, KHÔNG cần ENTER.")
                close_chatgpt_browser_hard(new_driver, new_process)
                sleep(1.0)
                continue

            raise RuntimeError(
                f"ChatGPT Project/composer không sẵn sàng sau auto recovery; "
                "không phát hiện login/Cloudflare."
            )

        # Attach thất bại thật sự: restart 1 vòng, không hỏi tay ngay.
        if browser_try == 1:
            print("♻️ Attach chưa được -> tự restart Chrome 1 lần.")
            close_chatgpt_browser_hard(None, new_process)
            sleep(1.0)
            continue

        raise RuntimeError(f"Không attach được ChatGPT profile {account.get('key')}")

    raise RuntimeError(f"Không mở được ChatGPT profile {account.get('key')}")


def restart_chatgpt_browser(chatgpt_driver, chatgpt_process, chatgpt_project):
    """
    Restart định kỳ Chrome ChatGPT.
    Generic timeout/UI load chậm được tự retry; chỉ hỏi tay nếu thấy login/challenge thật.
    """
    project_name = (chatgpt_project or {}).get("name") or (chatgpt_project or {}).get("key") or "Project"
    start_url = (chatgpt_project or {}).get("url") or CHATGPT_HOME

    print("\n" + "=" * 72)
    print(f"♻️ AUTO RESTART CHATGPT - đã xử lý {CHATGPT_RESTART_EVERY} video")
    print(f"📁 Giữ nguyên Project: {project_name}")
    print("=" * 72)

    close_chatgpt_browser_hard(chatgpt_driver, chatgpt_process)
    sleep(1.0)

    for browser_try in range(1, 3):
        new_process = launch_chatgpt_manual_chrome(start_url)
        if new_process is False:
            raise RuntimeError("Không mở lại được Chrome ChatGPT sau periodic restart")

        new_driver = attach_chatgpt_driver()
        if new_driver:
            ok, manual_auth, exc = auto_open_project_with_retries(
                new_driver, chatgpt_project, attempts=4, timeout=WAIT_CHATGPT_READY
            )
            if ok:
                print("✅ Restart ChatGPT xong, đã quay lại đúng Project.")
                return new_driver, new_process

            if manual_auth:
                print("⚠️ Phát hiện login/challenge thật sự; mới cần thao tác tay.")
                wait_manual_chatgpt_login_before_attach()
                if not driver_alive(new_driver):
                    new_driver = attach_chatgpt_driver()
                ok2, _, exc2 = auto_open_project_with_retries(
                    new_driver, chatgpt_project, attempts=3, timeout=WAIT_CHATGPT_READY
                )
                if ok2:
                    print("✅ Login/xác minh xong, tiếp tục batch.")
                    return new_driver, new_process
                raise RuntimeError(
                    f"Login xong nhưng Project vẫn chưa sẵn sàng: "
                    f"{str(exc2).strip() or type(exc2).__name__}"
                )

            if browser_try == 1:
                print("♻️ Project/UI load lỗi tạm thời -> tự restart thêm 1 lần, không cần ENTER.")
                close_chatgpt_browser_hard(new_driver, new_process)
                sleep(1.0)
                continue

            raise RuntimeError(
                "Project/composer không sẵn sàng sau auto recovery; "
                "không phát hiện login/Cloudflare."
            )

        if browser_try == 1:
            print("♻️ Attach chưa được -> tự restart thêm 1 lần.")
            close_chatgpt_browser_hard(None, new_process)
            sleep(1.0)
            continue

        raise RuntimeError("Không attach được ChatGPT sau periodic restart")

    raise RuntimeError("Restart ChatGPT thất bại")


def prepare_browsers(chatgpt_project=None):
    """Khởi tạo YouTube + Chrome ChatGPT thật và attach TỰ ĐỘNG."""
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)

    youtube_driver = create_youtube_driver()
    if not youtube_driver:
        return None, None, None

    chatgpt_start_url = (chatgpt_project or {}).get("url") or CHATGPT_HOME
    chatgpt_process = launch_chatgpt_manual_chrome(chatgpt_start_url)
    if chatgpt_process is False:
        try:
            youtube_driver.quit()
        except Exception:
            pass
        return None, None, None

    # Không dừng ENTER mặc định nữa.
    chatgpt_driver = attach_chatgpt_driver()
    if not chatgpt_driver:
        try:
            youtube_driver.quit()
        except Exception:
            pass
        return None, None, None

    if chatgpt_project:
        ok, manual_auth, _ = auto_open_project_with_retries(
            chatgpt_driver, chatgpt_project, attempts=4, timeout=WAIT_CHATGPT_READY
        )
        if not ok and manual_auth:
            print("⚠️ Chỉ vì phát hiện login/challenge thật sự nên mới cần thao tác tay.")
            wait_manual_chatgpt_login_before_attach()
            ok, _, _ = auto_open_project_with_retries(
                chatgpt_driver, chatgpt_project, attempts=3, timeout=WAIT_CHATGPT_READY
            )
        if not ok:
            try:
                youtube_driver.quit()
            except Exception:
                pass
            return None, None, None

    return youtube_driver, chatgpt_driver, chatgpt_process


# ============================================================
# YOUTUBE TRANSCRIPT - DIRECT get_panel
# ============================================================

def clear_performance_logs(driver):
    try:
        driver.get_log("performance")
    except Exception:
        pass


def wait_youtube_ready(driver):
    WebDriverWait(driver, WAIT_PAGE).until(
        lambda d: "youtube.com/watch" in safe_current_url(d)
    )
    WebDriverWait(driver, WAIT_PAGE).until(
        lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
    )



def wait_youtube_player_bootstrap(driver, timeout=4.0):
    """
    Chờ player/bootstrap theo kiểu adaptive:
    - mạng nhanh: thường thoát sau 0.1-0.4s;
    - mạng lag: chờ tối đa vài giây;
    - không fixed sleep 2s cho mọi video.
    """
    deadline = time.time() + max(0.5, float(timeout))
    while time.time() < deadline:
        try:
            ready = driver.execute_script(
                """
                return !!(
                    window.ytInitialPlayerResponse ||
                    document.getElementById('movie_player') ||
                    document.querySelector('video.html5-main-video')
                );
                """
            )
            if ready:
                return True
        except Exception:
            pass
        sleep(0.10)
    return False


def fetch_get_panel_direct(driver):
    """
    Mô phỏng đúng ý cURL get_panel nhưng dùng context + cookie/session động
    ngay trong tab YouTube, nên không hard-code cookie/header dễ hết hạn.
    """
    script = r"""
        const done = arguments[arguments.length - 1];

        function findPanel(obj, seen) {
            if (!obj || typeof obj !== 'object') return null;
            if (!seen) seen = new Set();
            if (seen.has(obj)) return null;
            seen.add(obj);

            try {
                if (obj.panelId === 'PAmodern_transcript_view' && typeof obj.params === 'string') {
                    return {panelId: obj.panelId, params: obj.params};
                }
            } catch (e) {}

            try {
                for (const key of Object.keys(obj)) {
                    const value = obj[key];
                    const found = findPanel(value, seen);
                    if (found) return found;
                }
            } catch (e) {}
            return null;
        }

        (async () => {
            try {
                const roots = [
                    window.ytInitialData,
                    window.ytInitialPlayerResponse,
                    window.ytcfg && window.ytcfg.data_
                ];

                let endpoint = null;
                for (const root of roots) {
                    endpoint = findPanel(root);
                    if (endpoint) break;
                }

                if (!endpoint) {
                    done(JSON.stringify({ok:false, error:'NO_PANEL_PARAMS'}));
                    return;
                }

                let context = null;
                try {
                    if (window.ytcfg && typeof window.ytcfg.get === 'function') {
                        context = window.ytcfg.get('INNERTUBE_CONTEXT');
                    }
                } catch (e) {}

                if (!context) {
                    try {
                        context = window.ytcfg.data_.INNERTUBE_CONTEXT;
                    } catch (e) {}
                }

                if (!context) {
                    done(JSON.stringify({ok:false, error:'NO_INNERTUBE_CONTEXT'}));
                    return;
                }

                const payload = {
                    context: context,
                    panelId: endpoint.panelId,
                    params: endpoint.params
                };

                const response = await fetch('/youtubei/v1/get_panel?prettyPrint=false', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'accept': '*/*',
                        'content-type': 'application/json'
                    },
                    body: JSON.stringify(payload)
                });

                const text = await response.text();
                done(JSON.stringify({
                    ok: response.ok,
                    status: response.status,
                    text: text,
                    panelId: endpoint.panelId,
                    params: endpoint.params
                }));
            } catch (e) {
                done(JSON.stringify({ok:false, error:String(e)}));
            }
        })();
    """

    try:
        driver.set_script_timeout(15)
        raw = driver.execute_async_script(script)
        result = json.loads(raw)
    except Exception as exc:
        return None, f"direct JS lỗi: {exc}"

    if not result.get("ok"):
        return None, f"direct get_panel lỗi: {result.get('status')} {result.get('error')}"

    text = result.get("text") or ""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"get_panel trả về không phải JSON: {exc}"


def click_first_matching(driver, selectors=None, texts=None):
    selectors = selectors or []
    texts = [t.lower() for t in (texts or [])]

    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if el.is_displayed() and el.is_enabled():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        sleep(0.15)
                        try:
                            el.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", el)
                        return True
                except Exception:
                    continue
        except Exception:
            pass

    if texts:
        # Tìm button theo text/aria-label/title.
        for tag in ("button", "tp-yt-paper-button", "yt-button-shape button", "a"):
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, tag)
            except Exception:
                continue

            for el in elements:
                try:
                    if not el.is_displayed() or not el.is_enabled():
                        continue
                    haystack = " ".join([
                        el.text or "",
                        el.get_attribute("aria-label") or "",
                        el.get_attribute("title") or "",
                    ]).lower()
                    if any(t in haystack for t in texts):
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        sleep(0.15)
                        try:
                            el.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", el)
                        return True
                except Exception:
                    continue

    return False


def try_open_transcript_panel(driver):
    # 1) Nếu nút Show transcript đã hiện thì click luôn.
    transcript_selectors = [
        "ytd-video-description-transcript-section-renderer button",
        "button[aria-label*='transcript' i]",
        "button[aria-label*='bản chép lời' i]",
    ]
    transcript_texts = [
        "show transcript",
        "transcript",
        "hiện bản chép lời",
        "bản chép lời",
    ]

    if click_first_matching(driver, transcript_selectors, transcript_texts):
        return True

    # 2) Mở rộng description bằng nút more / ...more.
    click_first_matching(
        driver,
        selectors=[
            "tp-yt-paper-button#expand",
            "ytd-text-inline-expander #expand",
            "#description-inline-expander #expand",
        ],
        texts=["...more", "more", "thêm"],
    )
    sleep(0.40)

    # 3) Tìm lại transcript.
    return click_first_matching(driver, transcript_selectors, transcript_texts)


def capture_get_panel_from_network(driver):
    """Fallback: click transcript rồi lấy chính response get_panel từ Chrome DevTools."""
    clear_performance_logs(driver)

    if not try_open_transcript_panel(driver):
        return None, "không tìm thấy nút Show transcript"

    deadline = time.time() + WAIT_TRANSCRIPT
    seen_request_ids = set()

    while time.time() < deadline:
        try:
            logs = driver.get_log("performance")
        except Exception:
            logs = []

        for entry in logs:
            try:
                message = json.loads(entry["message"])["message"]
            except Exception:
                continue

            if message.get("method") != "Network.responseReceived":
                continue

            params = message.get("params", {})
            response = params.get("response", {})
            url = response.get("url", "")

            if "/youtubei/v1/get_panel" not in url:
                continue

            request_id = params.get("requestId")
            if not request_id or request_id in seen_request_ids:
                continue
            seen_request_ids.add(request_id)

            try:
                body_info = driver.execute_cdp_cmd(
                    "Network.getResponseBody",
                    {"requestId": request_id},
                )
                body = body_info.get("body", "")
                if body:
                    return json.loads(body), None
            except Exception as exc:
                last_error = str(exc)
                continue

        sleep(0.20)

    return None, "không bắt được response get_panel trong network log"


def extract_transcript_segments(panel_json):
    segments = []

    def walk(node):
        if isinstance(node, dict):
            vm = node.get("transcriptSegmentViewModel")
            if isinstance(vm, dict):
                timestamp = vm.get("timestamp") or ""
                text = vm.get("simpleText") or ""

                if not text:
                    runs = vm.get("runs")
                    if isinstance(runs, list):
                        text = "".join(
                            str(item.get("text", ""))
                            for item in runs
                            if isinstance(item, dict)
                        )

                if timestamp and text:
                    segments.append((str(timestamp).strip(), str(text).strip()))

            for value in node.values():
                walk(value)

        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(panel_json)

    # Dedupe giữ nguyên thứ tự.
    cleaned = []
    seen = set()
    for ts, text in segments:
        key = (ts, text)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append((ts, text))

    return cleaned


def youtube_timestamp_to_hms(value):
    """Chỉ chuẩn hóa mốc YouTube có sẵn, không tạo mốc mới."""
    value = str(value).strip()
    parts = value.split(":")
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        return value

    if len(nums) == 2:
        mm, ss = nums
        return f"00:{mm:02d}:{ss:02d}"
    if len(nums) == 3:
        hh, mm, ss = nums
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return value


def format_transcript(segments):
    return "\n".join(
        f"{youtube_timestamp_to_hms(ts)} {text}"
        for ts, text in segments
    )


def _hms_from_milliseconds(value):
    """Đổi tStartMs của timedtext thành HH:MM:SS bằng cách floor về giây."""
    try:
        total = max(0, int(float(value) // 1000))
    except (TypeError, ValueError):
        return None
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _caption_track_display_name(track):
    if not isinstance(track, dict):
        return ""
    name = track.get("name")
    if isinstance(name, str):
        return name.strip()
    if isinstance(name, dict):
        simple = name.get("simpleText")
        if simple:
            return str(simple).strip()
        runs = name.get("runs")
        if isinstance(runs, list):
            return "".join(
                str(item.get("text", ""))
                for item in runs
                if isinstance(item, dict)
            ).strip()
    return str(track.get("label") or "").strip()


def collect_caption_tracks_from_player(driver):
    """
    Lấy captionTracks trực tiếp từ player response của CHÍNH tab video hiện tại.

    Không hard-code timedtext URL/cookie/signature/pot. baseUrl trả về từ YouTube
    đã chứa các tham số động cần thiết cho video/session hiện tại.
    """
    script = r"""
    function parseMaybeJson(value) {
      if (typeof value !== 'string') return value;
      const s = value.trim();
      if (!s || (s[0] !== '{' && s[0] !== '[')) return value;
      try { return JSON.parse(s); } catch (e) { return value; }
    }

    function trackName(nameObj) {
      if (!nameObj) return '';
      if (typeof nameObj === 'string') return nameObj;
      if (nameObj.simpleText) return String(nameObj.simpleText);
      if (Array.isArray(nameObj.runs)) {
        return nameObj.runs.map(x => (x && x.text) ? String(x.text) : '').join('');
      }
      return '';
    }

    const out = [];
    const seenUrl = new Set();
    const seenObj = new WeakSet();

    function addTracks(tracks) {
      if (!Array.isArray(tracks)) return;
      for (const t of tracks) {
        if (!t || typeof t !== 'object') continue;
        const baseUrl = String(t.baseUrl || '');
        if (!baseUrl || seenUrl.has(baseUrl)) continue;
        seenUrl.add(baseUrl);
        out.push({
          baseUrl: baseUrl,
          languageCode: String(t.languageCode || ''),
          kind: String(t.kind || ''),
          vssId: String(t.vssId || ''),
          name: trackName(t.name),
          isTranslatable: !!t.isTranslatable
        });
      }
    }

    function walk(obj, depth) {
      if (!obj || typeof obj !== 'object' || depth > 10) return;
      if (seenObj.has(obj)) return;
      seenObj.add(obj);

      try {
        if (Array.isArray(obj.captionTracks)) addTracks(obj.captionTracks);
      } catch (e) {}

      if (out.length) return;

      try {
        for (const key of Object.keys(obj)) {
          if (out.length) return;
          const value = parseMaybeJson(obj[key]);
          if (value && typeof value === 'object') walk(value, depth + 1);
        }
      } catch (e) {}
    }

    const roots = [];
    try { roots.push(window.ytInitialPlayerResponse); } catch (e) {}
    try { roots.push(window.ytplayer && window.ytplayer.config && window.ytplayer.config.args && window.ytplayer.config.args.raw_player_response); } catch (e) {}
    try { roots.push(window.ytcfg && window.ytcfg.get && window.ytcfg.get('PLAYER_RESPONSE')); } catch (e) {}
    try { roots.push(window.ytcfg && window.ytcfg.data_ && window.ytcfg.data_.PLAYER_RESPONSE); } catch (e) {}
    try { roots.push(window.ytInitialData); } catch (e) {}

    for (let root of roots) {
      root = parseMaybeJson(root);
      walk(root, 0);
      if (out.length) break;
    }

    return out;
    """

    try:
        tracks = driver.execute_script(script) or []
    except Exception as exc:
        return [], f"không đọc được captionTracks từ player: {exc}"

    cleaned = []
    seen = set()
    for item in tracks:
        if not isinstance(item, dict):
            continue
        base_url = str(item.get("baseUrl") or "").strip()
        if not base_url or base_url in seen:
            continue
        seen.add(base_url)
        cleaned.append(item)

    if not cleaned:
        return [], "player response không có captionTracks/baseUrl"
    return cleaned, None


def choose_best_caption_track(tracks):
    """Ưu tiên English manual, rồi English ASR, sau đó track gốc đầu tiên."""
    if not tracks:
        return None

    def score(track):
        lang = str(track.get("languageCode") or "").lower().strip()
        kind = str(track.get("kind") or "").lower().strip()
        name = _caption_track_display_name(track).lower()

        points = 0
        if lang == "en":
            points += 1000
        elif lang.startswith("en-") or lang.startswith("en_"):
            points += 950
        elif "english" in name:
            points += 900
        else:
            points += 100

        # Manual subtitle thường sạch hơn ASR; nhưng ASR vẫn là fallback hợp lệ.
        if kind != "asr":
            points += 40
        else:
            points += 20

        if track.get("baseUrl"):
            points += 5
        return points

    return max(tracks, key=score)


def fetch_timedtext_json3(driver, base_url):
    """Fetch timedtext bằng chính Chrome/session YouTube hiện tại."""
    script = r"""
    const baseUrl = arguments[0];
    const done = arguments[arguments.length - 1];

    (async () => {
      try {
        const u = new URL(baseUrl, window.location.origin);
        u.searchParams.set('fmt', 'json3');

        const response = await fetch(u.toString(), {
          method: 'GET',
          credentials: 'include',
          cache: 'no-store',
          headers: { 'accept': '*/*' }
        });

        const text = await response.text();
        done({
          ok: response.ok,
          status: response.status,
          statusText: response.statusText || '',
          text: text,
          length: text.length,
          contentType: response.headers.get('content-type') || '',
          url: u.toString()
        });
      } catch (e) {
        done({ok:false, status:0, error:String(e), text:''});
      }
    })();
    """

    try:
        driver.set_script_timeout(TIMEDTEXT_FETCH_TIMEOUT)
        result = driver.execute_async_script(script, str(base_url)) or {}
    except Exception as exc:
        return None, f"timedtext fetch JS lỗi: {exc}"

    if not isinstance(result, dict):
        return None, "timedtext fetch trả về dữ liệu không hợp lệ"

    if not result.get("ok"):
        return None, (
            f"timedtext HTTP {result.get('status')}"
            + (f" | {result.get('error')}" if result.get("error") else "")
        )

    raw = result.get("text") or ""
    if not raw.strip():
        return None, (
            "timedtext endpoint ĐÃ trả response nhưng body rỗng"
            f" | HTTP={result.get('status')}"
            f" | content-type={result.get('contentType') or '?'}"
            f" | bytes={result.get('length', 0)}"
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"timedtext không phải JSON3 hợp lệ: {exc}"

    if not isinstance(payload, dict):
        return None, "timedtext JSON3 root không phải object"
    return payload, None




def _decode_cdp_body(body_info):
    if not isinstance(body_info, dict):
        return ""
    body = body_info.get("body") or ""
    if not body:
        return ""
    if body_info.get("base64Encoded"):
        try:
            return base64.b64decode(body).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return str(body)


def _parse_json3_text(raw):
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return payload
    return None


def trigger_youtube_caption_request(driver):
    """
    ÉP YouTube player tạo request /api/timedtext THẬT bằng nút CC.

    Quan trọng:
    - Nếu CC đang TẮT (aria-pressed=false): click 1 lần để BẬT.
    - Nếu CC đang BẬT (aria-pressed=true): request timedtext có thể đã chạy TRƯỚC
      lúc ta clear performance log. Vì vậy phải toggle TẮT -> BẬT lại để ép
      phát sinh request timedtext mới.
    - Kết thúc luôn để CC ở trạng thái BẬT.
    """

    selectors = [
        "button.ytp-subtitles-button.ytp-button",
        ".ytp-subtitles-button",
        "button[aria-keyshortcuts='c']",
        "button[aria-label*='Phụ đề' i]",
        "button[aria-label*='subtitles' i]",
        "button[aria-label*='captions' i]",
    ]

    def find_cc_button():
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue

            for el in elements:
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except Exception:
                    continue
        return None

    def click_button(el):
        # Native click trước, JS click fallback.
        try:
            driver.execute_script(
                """
                try {
                    const p = document.querySelector('.html5-video-player');
                    if (p) {
                        p.dispatchEvent(new MouseEvent('mousemove', {
                            bubbles: true,
                            clientX: 300,
                            clientY: 300
                        }));
                    }
                } catch(e) {}
                """
            )
        except Exception:
            pass

        try:
            el.click()
            return True
        except Exception:
            pass

        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False

    button = find_cc_button()
    if button is not None:
        try:
            pressed = (button.get_attribute("aria-pressed") or "").strip().lower()
        except Exception:
            pressed = ""

        try:
            label = (button.get_attribute("aria-label") or "").strip()
        except Exception:
            label = ""

        print(
            f"   🎛️ Nút CC tìm thấy"
            f" | aria-pressed={pressed or '?'}"
            f"{' | ' + label if label else ''}"
        )

        if pressed == "true":
            # CC đã bật => phải OFF -> ON để ép request mới.
            print("   🔁 CC đang BẬT -> toggle TẮT rồi BẬT lại để ép timedtext request mới...")

            if not click_button(button):
                print("   ⚠️ Không click được CC để tắt.")
            else:
                sleep(0.45)

            # Re-acquire vì YouTube có thể re-render button.
            button = find_cc_button() or button

            # Xóa log phát sinh khi tắt CC; chỉ muốn bắt request sau lúc bật lại.
            try:
                clear_performance_logs(driver)
            except Exception:
                pass

            if click_button(button):
                sleep(0.35)
                try:
                    now_pressed = (button.get_attribute("aria-pressed") or "").strip().lower()
                except Exception:
                    now_pressed = ""
                print(f"   ✅ Đã click BẬT lại CC | aria-pressed={now_pressed or '?'}")
                return True

            print("   ⚠️ Không click được CC để bật lại.")

        else:
            # CC đang tắt hoặc trạng thái chưa rõ -> bật luôn.
            print("   ▶️ CC đang TẮT/chưa rõ -> click BẬT để trigger timedtext...")
            if click_button(button):
                sleep(0.35)
                try:
                    now_pressed = (button.get_attribute("aria-pressed") or "").strip().lower()
                except Exception:
                    now_pressed = ""
                print(f"   ✅ Đã click CC | aria-pressed={now_pressed or '?'}")
                return True

            print("   ⚠️ Click nút CC thất bại.")

    else:
        print("   ⚠️ Không tìm thấy nút CC thật trên player.")

    # Player API fallback nếu button không dùng được.
    try:
        api_triggered = driver.execute_script(
            r"""
            try {
              const p = document.getElementById('movie_player');
              if (!p) return false;

              try {
                if (typeof p.loadModule === 'function') p.loadModule('captions');
              } catch(e) {}

              try {
                const list = (typeof p.getOption === 'function')
                  ? (p.getOption('captions', 'tracklist') || [])
                  : [];

                if (Array.isArray(list) && list.length && typeof p.setOption === 'function') {
                  // ép unload/reload track để tạo request mới
                  try { p.setOption('captions', 'track', {}); } catch(e) {}
                  try { p.setOption('captions', 'track', list[0]); } catch(e) {}
                  return true;
                }
              } catch(e) {}

              return false;
            } catch(e) {
              return false;
            }
            """
        )

        if api_triggered:
            print("   ✅ Trigger captions bằng player API fallback.")
            return True
    except Exception:
        pass

    return False

def capture_timedtext_json3_from_network(driver, expected_video_id=None, wait_seconds=10):
    """
    Bắt request /api/timedtext THẬT do chính YouTube player tạo ra.

    Vì sao cần:
    captionTracks.baseUrl đôi khi tồn tại nhưng fetch(baseUrl + fmt=json3) nhận HTTP 200
    với body rỗng. Trong khi request thật của player có thể được bổ sung các tham số
    động như POT/variant/client fields. Ta không hard-code chúng; chỉ bắt request thật.
    """
    clear_performance_logs(driver)

    triggered = trigger_youtube_caption_request(driver)
    print(
        "   🎬 Đang bắt request timedtext THẬT từ YouTube player..."
        + (" đã FORCE trigger CC." if triggered else " chưa trigger được CC; vẫn theo dõi network.")
    )

    deadline = time.time() + max(3, int(wait_seconds))
    candidates = []
    seen = set()

    while time.time() < deadline:
        try:
            logs = driver.get_log("performance")
        except Exception:
            logs = []

        for entry in logs:
            try:
                msg = json.loads(entry["message"])["message"]
            except Exception:
                continue

            method = msg.get("method")
            if method != "Network.responseReceived":
                continue

            params = msg.get("params") or {}
            response = params.get("response") or {}
            url = str(response.get("url") or "")
            low = url.lower()

            if "/api/timedtext" not in low and "timedtext?" not in low:
                continue
            if expected_video_id and f"v={expected_video_id.lower()}" not in low:
                # Có thể browser còn request của video cũ.
                continue

            request_id = params.get("requestId")
            if not request_id or request_id in seen:
                continue
            seen.add(request_id)

            status = response.get("status")
            mime = response.get("mimeType") or ""
            candidates.append((url, status, mime))

            try:
                body_info = driver.execute_cdp_cmd(
                    "Network.getResponseBody",
                    {"requestId": request_id},
                )
                raw = _decode_cdp_body(body_info)
            except Exception:
                raw = ""

            if raw.strip():
                payload = _parse_json3_text(raw)
                if payload is not None:
                    print(
                        f"   ✅ Bắt được timedtext network: HTTP={status} | "
                        f"bytes={len(raw)} | mime={mime or '?'}"
                    )
                    return payload, None, url

            # Nếu response body từ CDP chưa lấy được, thử fetch CHÍNH URL request thật.
            if url:
                payload, err = fetch_timedtext_json3(driver, url)
                if payload is not None:
                    print(
                        f"   ✅ Fetch lại URL timedtext thật thành công: "
                        f"HTTP={status} | mime={mime or '?'}"
                    )
                    return payload, None, url

        sleep(0.12)

    if candidates:
        last_url, last_status, last_mime = candidates[-1]
        return None, (
            f"đã thấy {len(candidates)} request timedtext thật nhưng chưa lấy được body "
            f"| HTTP cuối={last_status} | mime={last_mime or '?'}"
        ), last_url

    return None, "không thấy request /api/timedtext thật trong network sau khi trigger captions", None


def extract_timedtext_json3_segments(payload):
    """
    Parse YouTube timedtext fmt=json3.
    Mỗi event có tStartMs + segs[].utf8 trở thành một dòng transcript.
    Event chỉ chứa newline/window-control sẽ bị bỏ.
    """
    if not isinstance(payload, dict):
        return []

    events = payload.get("events")
    if not isinstance(events, list):
        return []

    rows = []
    seen = set()

    for event in events:
        if not isinstance(event, dict):
            continue
        start_ms = event.get("tStartMs")
        if start_ms is None:
            continue

        segs = event.get("segs")
        if not isinstance(segs, list):
            continue

        pieces = []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            value = seg.get("utf8")
            if value is not None:
                pieces.append(str(value))

        if not pieces:
            continue

        # JSON3 dùng event chỉ chứa "\\n" để điều khiển cửa sổ caption.
        text = "".join(pieces)
        text = text.replace("\u200b", "").replace("\ufeff", "")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        hms = _hms_from_milliseconds(start_ms)
        if not hms:
            continue

        key = (hms, text)
        if key in seen:
            continue
        seen.add(key)
        rows.append((hms, text))

    return rows


def fetch_transcript_from_timedtext(driver):
    """
    Fallback cho video không có nút "Show transcript".

    Tầng A:
      captionTracks.baseUrl -> fmt=json3.
    Tầng B:
      nếu A nhận body rỗng/lỗi, trigger CC rồi bắt request /api/timedtext THẬT
      từ Network. Request thật thường có thêm token/params động của session.
    """
    tracks, error = collect_caption_tracks_from_player(driver)
    if not tracks:
        return [], error

    track = choose_best_caption_track(tracks)
    if not track:
        return [], "không chọn được caption track"

    lang = str(track.get("languageCode") or "?")
    kind = str(track.get("kind") or "manual") or "manual"
    name = _caption_track_display_name(track) or "?"
    print(
        f"   🎯 Caption track: lang={lang} | kind={kind} | name={name} "
        f"| tổng tracks={len(tracks)}"
    )

    # A) Base URL từ player response.
    payload, base_error = fetch_timedtext_json3(driver, track.get("baseUrl"))
    if payload is not None:
        segments = extract_timedtext_json3_segments(payload)
        if segments:
            return segments, None
        base_error = "baseUrl JSON3 có response nhưng không parse được event có chữ"

    print(f"   ⚠️ baseUrl timedtext chưa có data: {base_error}")
    print("   🔁 Timedtext tầng B: trigger caption + bắt request API thật từ Network...")

    # B) Network request thật của chính player.
    expected_video_id = None
    try:
        expected_video_id = video_id_from_url(safe_current_url(driver))
    except Exception:
        expected_video_id = None

    payload2, network_error, actual_url = capture_timedtext_json3_from_network(
        driver,
        expected_video_id=expected_video_id,
        wait_seconds=10,
    )
    if payload2 is not None:
        segments = extract_timedtext_json3_segments(payload2)
        if segments:
            print(f"   ✅ Timedtext Network parse được {len(segments)} dòng.")
            return segments, None
        network_error = "request timedtext thật có body nhưng không parse được JSON3 events"

    return [], (
        f"baseUrl: {base_error}; network: {network_error}"
        + (" | đã bắt được URL timedtext thật" if actual_url else "")
    )



def extract_transcript_from_dom(driver):
    """
    Fallback cuối khi get_panel/network log thay đổi.
    Chỉ đọc các timestamp/text đang được YouTube render, không tự bịa timestamp.
    """
    script = r"""
    const out = [];
    const selectors = [
      'ytd-transcript-segment-renderer',
      'transcript-segment-view-model',
      '[class*="transcript-segment"]'
    ];
    const seen = new Set();

    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        let ts = '';
        let tx = '';
        const tsEl =
          el.querySelector('.segment-timestamp') ||
          el.querySelector('[class*="timestamp"]') ||
          el.querySelector('[aria-label*="second"]') ||
          el.querySelector('[aria-label*="phút"]');
        const txEl =
          el.querySelector('.segment-text') ||
          el.querySelector('[class*="segment-text"]') ||
          el.querySelector('[class*="text"]');

        if (tsEl) ts = (tsEl.innerText || tsEl.textContent || '').trim();
        if (txEl) tx = (txEl.innerText || txEl.textContent || '').trim();

        if (!ts || !tx) {
          const raw = (el.innerText || '').trim().split(/\n+/);
          if (!ts && raw.length) {
            const m = raw[0].match(/^\d{1,4}:\d{2}(?::\d{2})?$/);
            if (m) ts = m[0];
          }
          if (!tx && raw.length > 1) tx = raw.slice(1).join(' ').trim();
        }

        if (ts && tx) {
          const key = ts + '\n' + tx;
          if (!seen.has(key)) {
            seen.add(key);
            out.push([ts, tx]);
          }
        }
      }
    }
    return out;
    """
    try:
        rows = driver.execute_script(script) or []
    except Exception:
        return []

    cleaned = []
    for row in rows:
        try:
            ts, tx = str(row[0]).strip(), str(row[1]).strip()
        except Exception:
            continue
        if re.fullmatch(r"\d{1,4}:\d{2}(?::\d{2})?", ts) and tx:
            cleaned.append((ts, tx))
    return cleaned



def get_transcript(driver, video_url):
    print("\n📝 Đang lấy transcript timestamp từ YouTube...")
    driver.get(video_url)
    wait_youtube_ready(driver)
    wait_youtube_player_bootstrap(driver, timeout=4.0)

    error = None
    source = None

    # 1) Direct get_panel.
    panel_json, error = fetch_get_panel_direct(driver)
    if panel_json is not None:
        print("✅ get_panel trực tiếp thành công bằng session Chrome hiện tại.")
        segments = extract_transcript_segments(panel_json)
        source = "get_panel_direct"
    else:
        segments = []
        print(f"⚠️ Direct get_panel chưa được: {error}")

    # 2) Nếu có nút Show transcript, bắt response get_panel thật từ Network.
    if not segments:
        print("🔁 Fallback 1: mở Show transcript và bắt response get_panel thật...")
        panel_json, panel_error = capture_get_panel_from_network(driver)
        if panel_json is not None:
            segments = extract_transcript_segments(panel_json)
            if segments:
                source = "get_panel_network"
        if not segments:
            error = panel_error or error
            if panel_error:
                print(f"⚠️ Fallback 1 chưa được: {panel_error}")

    # 3) QUAN TRỌNG: video có caption nhưng YouTube không hiện nút Show transcript.
    # Lấy captionTracks/baseUrl động từ player rồi fetch /api/timedtext?fmt=json3.
    if not segments and ENABLE_TRANSCRIPT_TIMEDTEXT_FALLBACK:
        print("🔁 Fallback 2: captionTracks -> timedtext JSON3 (không cần nút Show transcript)...")
        segments, timedtext_error = fetch_transcript_from_timedtext(driver)
        if segments:
            source = "timedtext_json3"
            print(f"✅ Timedtext JSON3 thành công: {len(segments)} event có chữ.")
        else:
            error = timedtext_error or error
            print(f"⚠️ Fallback 2 chưa được: {timedtext_error}")

    # 4) Fallback cuối: DOM, chỉ hữu ích nếu transcript panel thực sự render được.
    if not segments and ENABLE_TRANSCRIPT_DOM_FALLBACK:
        print("🔁 Fallback 3: đọc transcript đang render trực tiếp trong DOM...")
        try:
            try_open_transcript_panel(driver)
            sleep(1.5)
        except Exception:
            pass
        segments = extract_transcript_from_dom(driver)
        if segments:
            source = "transcript_dom"

    if not segments:
        print(f"❌ Không lấy được transcript có timestamp. Lỗi cuối: {error}")
        return None

    # Bảo đảm timestamp parse được và thứ tự tăng không bị hỏng nghiêm trọng.
    valid = []
    last_sec = -1
    for ts, speech in segments:
        hms = youtube_timestamp_to_hms(ts)
        sec = cutter.timestamp_to_seconds(hms)
        if sec is None:
            continue
        # YouTube đôi khi lặp cùng timestamp; vẫn giữ text, nhưng loại timestamp chạy ngược.
        if sec < last_sec:
            continue
        last_sec = sec
        valid.append((hms, speech))

    if not valid:
        print("❌ Transcript có dữ liệu nhưng không có timestamp hợp lệ.")
        return None

    transcript = "\n".join(f"{ts} {speech}" for ts, speech in valid)
    print(f"✅ Lấy được {len(valid)} dòng transcript. | source={source}")
    print(f"   Mốc đầu: {valid[0][0]}")
    print(f"   Mốc cuối: {valid[-1][0]}")
    return transcript


def _collect_all_chrome_cookies(driver):
    """Lấy cookie trực tiếp từ Chrome đang chạy, ưu tiên CDP để gồm cả HttpOnly."""
    cookies = []

    try:
        result = driver.execute_cdp_cmd("Network.getAllCookies", {})
        if isinstance(result, dict):
            cookies = result.get("cookies") or []
    except Exception:
        cookies = []

    if not cookies:
        try:
            cookies = driver.get_cookies() or []
        except Exception:
            cookies = []

    return cookies


def _cookie_expiry(cookie):
    value = cookie.get("expires", cookie.get("expiry", 0))
    try:
        value = int(float(value or 0))
    except Exception:
        value = 0
    return max(0, value)


def export_fresh_youtube_cookies(driver, video_url=None, cookie_file=LIVE_COOKIE_FILE):
    """
    Xuất cookie MỚI NGAY TRƯỚC KHI yt-dlp tải.

    Không đọc database Chrome, không dùng cookies.txt cũ. Cookie được lấy thẳng
    từ phiên Selenium YouTube đang login rồi ghi theo chuẩn Netscape mà yt-dlp đọc.
    """
    if video_url and "youtube.com/watch" not in safe_current_url(driver):
        try:
            driver.get(video_url)
            wait_youtube_ready(driver)
            sleep(1)
        except Exception:
            pass

    # Đảm bảo CDP Network đã bật trước khi lấy all cookies.
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass

    cookies = _collect_all_chrome_cookies(driver)
    if not cookies:
        print("❌ Không lấy được cookie nào từ Chrome YouTube.")
        return None

    # Chỉ giữ cookie liên quan YouTube/Google cần cho phiên YouTube.
    accepted_domains = (
        "youtube.com",
        "google.com",
        "googleapis.com",
    )

    selected = []
    seen = set()
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").strip()
        if not domain:
            continue
        bare = domain.lstrip(".").lower()
        if not any(bare == d or bare.endswith("." + d) for d in accepted_domains):
            continue

        name = str(cookie.get("name") or "")
        path = str(cookie.get("path") or "/")
        key = (domain, path, name)
        if not name or key in seen:
            continue
        seen.add(key)
        selected.append(cookie)

    if not selected:
        print("❌ Chrome có cookie nhưng không có cookie YouTube/Google phù hợp.")
        return None

    cookie_file = Path(cookie_file)
    cookie_file.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated automatically from the ACTIVE Selenium YouTube session.",
        "# Do not edit while the program is running.",
    ]

    for cookie in selected:
        domain = str(cookie.get("domain") or "").strip()
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if bool(cookie.get("secure")) else "FALSE"
        expires = str(_cookie_expiry(cookie))
        name = str(cookie.get("name") or "").replace("\t", " ").replace("\r", "").replace("\n", "")
        value = str(cookie.get("value") or "").replace("\t", " ").replace("\r", "").replace("\n", "")
        lines.append("\t".join([domain, include_subdomains, path, secure, expires, name, value]))

    # CRLF giúp cookies.txt tương thích ổn định trên Windows.
    with open(cookie_file, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")

    names = {str(c.get("name") or "") for c in selected}
    auth_names = {
        "LOGIN_INFO", "SAPISID", "APISID", "SID", "HSID", "SSID",
        "__Secure-1PSID", "__Secure-3PSID",
        "__Secure-1PAPISID", "__Secure-3PAPISID",
        "__Secure-1PSIDTS", "__Secure-3PSIDTS",
        "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    }
    auth_found = sorted(names.intersection(auth_names))

    try:
        user_agent = driver.execute_script("return navigator.userAgent || '';" ) or ""
    except Exception:
        user_agent = ""

    print("\n🍪 ĐÃ LẤY COOKIE MỚI TỪ CHROME YOUTUBE")
    print(f"   📄 {cookie_file}")
    print(f"   🍪 Tổng cookie ghi ra: {len(selected)}")
    print(f"   🔐 Cookie phiên đăng nhập tìm thấy: {len(auth_found)}")
    if "LOGIN_INFO" in names:
        print("   ✅ LOGIN_INFO: CÓ")
    else:
        print("   ⚠️ LOGIN_INFO: KHÔNG THẤY (video public vẫn có thể tải)")
    if auth_found:
        print("   ✅ Có cookie xác thực YouTube/Google trong phiên hiện tại.")
    else:
        print("   ⚠️ Không thấy cookie xác thực; có thể profile YouTube chưa login.")

    return {
        "path": cookie_file,
        "user_agent": user_agent,
        "cookie_count": len(selected),
        "auth_cookie_count": len(auth_found),
    }

# ============================================================
# PROMPT
# ============================================================

def load_prompt_template():
    if not PROMPT_FILE.exists():
        print(f"❌ Thiếu {PROMPT_FILE.name}")
        return None
    return PROMPT_FILE.read_text(encoding="utf-8-sig")


def build_prompt_file_text(prompt_template, transcript_filename):
    """
    Tạo NỘI DUNG FILE PROMPT riêng. Tuyệt đối không nhét transcript vào file này.

    Placeholder cũ được thay bằng chỉ dẫn rằng transcript nằm ở file đính kèm riêng.
    """
    replacement = (
        "[TRANSCRIPT KHÔNG NẰM TRONG FILE NÀY. "
        f"HÃY ĐỌC TOÀN BỘ FILE ĐÍNH KÈM: {transcript_filename}]"
    )

    if TRANSCRIPT_PLACEHOLDER in prompt_template:
        text = prompt_template.replace(TRANSCRIPT_PLACEHOLDER, replacement)
    else:
        text = (
            prompt_template.rstrip()
            + "\n\n## TRANSCRIPT CẦN PHÂN TÍCH\n\n"
            + replacement
        )

    return text.rstrip() + "\n"


def prepare_separate_ai_files(video_url, prompt_template, transcript, ai_input_dir=None, transcript_dir=None):
    """
    Tạo đúng 2 file riêng cho mỗi video:
      1) <video_id>_PROMPT.txt
      2) <video_id>_TRANSCRIPT.txt

    Mỗi bước đều verify file thực tế trên đĩa trước khi cho pipeline đi tiếp.
    """
    vid = video_id_from_url(video_url) or "video"
    ai_input_dir = Path(ai_input_dir) if ai_input_dir else AI_INPUT_DIR
    transcript_dir = Path(transcript_dir) if transcript_dir else TRANSCRIPT_DIR
    ai_input_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = ai_input_dir / f"{vid}_PROMPT.txt"
    transcript_path = transcript_dir / f"{vid}_TRANSCRIPT.txt"

    # BƯỚC 1: transcript riêng
    transcript_clean = (transcript or "").strip() + "\n"
    transcript_path.write_text(transcript_clean, encoding="utf-8")

    saved_transcript = transcript_path.read_text(encoding="utf-8")
    transcript_lines = [ln for ln in saved_transcript.splitlines() if ln.strip()]
    ts_lines = [
        ln for ln in transcript_lines
        if re.match(r"^\d{2}:\d{2}:\d{2}\s+", ln.strip())
    ]

    if (
        not transcript_path.exists()
        or transcript_path.stat().st_size <= 0
        or not transcript_lines
        or len(ts_lines) < max(1, int(len(transcript_lines) * 0.90))
    ):
        raise RuntimeError("File transcript lưu ra không hợp lệ.")

    first_ts = ts_lines[0].split(maxsplit=1)[0]
    last_ts = ts_lines[-1].split(maxsplit=1)[0]
    print("✅ BƯỚC 1/5 - TRANSCRIPT FILE OK")
    print(f"   📄 {transcript_path.name}")
    print(f"   📏 {len(transcript_lines)} dòng | {first_ts} → {last_ts}")

    # BƯỚC 2: prompt riêng, KHÔNG chứa transcript
    prompt_text = build_prompt_file_text(prompt_template, transcript_path.name)
    prompt_path.write_text(prompt_text, encoding="utf-8")
    saved_prompt = prompt_path.read_text(encoding="utf-8")

    if (
        not prompt_path.exists()
        or prompt_path.stat().st_size <= 0
        or len(saved_prompt.strip()) < 100
        or TRANSCRIPT_PLACEHOLDER in saved_prompt
    ):
        raise RuntimeError("File prompt riêng lưu ra không hợp lệ.")

    # Chốt an toàn: prompt không được chứa nguyên transcript.
    sample = transcript_lines[0].strip() if transcript_lines else ""
    if sample and sample in saved_prompt:
        raise RuntimeError("Prompt file đang lẫn transcript; dừng để tránh gửi sai.")

    print("✅ BƯỚC 2/5 - PROMPT FILE OK")
    print(f"   📄 {prompt_path.name}")
    print(f"   📏 {len(saved_prompt):,} ký tự | transcript KHÔNG bị trộn vào prompt")

    return prompt_path, transcript_path


# ============================================================
# CHATGPT AUTOMATION
# ============================================================

def find_chatgpt_composer(driver):
    selectors = [
        "#prompt-textarea",
        "textarea[data-testid='prompt-textarea']",
        "div[contenteditable='true'][data-virtualkeyboard='true']",
        "div[contenteditable='true'].ProseMirror",
        "div[contenteditable='true']",
    ]

    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed() and el.is_enabled():
                    return el
        except Exception:
            continue
    return None


def wait_chatgpt_composer(driver, timeout=WAIT_CHATGPT_READY):
    return WebDriverWait(driver, timeout).until(lambda d: find_chatgpt_composer(d))


def set_clipboard_text_windows(text):
    """Dùng PowerShell Set-Clipboard qua file tạm để không vỡ khi prompt rất dài."""
    clip_file = BASE_DIR / "_clipboard_prompt.txt"
    clip_file.write_text(text, encoding="utf-8")

    if os.name != "nt":
        return False

    escaped = str(clip_file).replace("'", "''")
    command = (
        f"Get-Content -LiteralPath '{escaped}' -Raw -Encoding UTF8 | Set-Clipboard"
    )

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def read_clipboard_text_windows():
    if os.name != "nt":
        return None

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout


def clear_chatgpt_composer(composer):
    """
    Xóa draft ChatGPT thật sự, kể cả khi Project tự restore draft cũ.

    Bản cũ chỉ Ctrl+A -> Backspace một lần. Với ProseMirror/React, thao tác đó
    đôi khi chỉ xóa DOM tạm hoặc không cập nhật state nên khi mở Project lại,
    draft 5k ký tự xuất hiện lại.

    Bản này dùng nhiều lớp:
      1) Selenium Ctrl+A/Backspace.
      2) ActionChains Ctrl+A/Backspace.
      3) JS selectAll + execCommand(delete) + InputEvent.
      4) Nếu vẫn còn: ép contenteditable/textarea rỗng + dispatch input/change.
    """
    if composer is None:
        return False

    try:
        driver = composer.parent
    except Exception:
        driver = None

    def _visible_text(el):
        try:
            tag = (el.tag_name or "").lower()
            if tag == "textarea":
                return (el.get_attribute("value") or "").strip()
            return (el.text or el.get_attribute("innerText") or el.get_attribute("textContent") or "").strip()
        except Exception:
            return ""

    # 1) Cách native đơn giản.
    try:
        composer.click()
        composer.send_keys(Keys.CONTROL, "a")
        composer.send_keys(Keys.BACKSPACE)
        sleep(0.15)
        if not _visible_text(composer):
            return True
    except Exception:
        pass

    # 2) ActionChains thường ổn hơn với ProseMirror.
    if driver is not None:
        try:
            ActionChains(driver).move_to_element(composer).click().key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).perform()
            sleep(0.15)
            if not _visible_text(composer):
                return True
        except Exception:
            pass

    # 3) Xóa bằng Selection + execCommand để React nhận thao tác như edit thật.
    if driver is not None:
        try:
            driver.execute_script(
                r"""
                const el = arguments[0];
                try { el.focus(); } catch(e) {}

                const tag = (el.tagName || '').toLowerCase();

                if (tag === 'textarea' || tag === 'input') {
                    try {
                        el.select();
                        document.execCommand('delete');
                    } catch(e) {}
                    try {
                        const setter = Object.getOwnPropertyDescriptor(
                            tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
                            'value'
                        ).set;
                        setter.call(el, '');
                    } catch(e) {
                        el.value = '';
                    }
                    el.dispatchEvent(new InputEvent('input', {
                        bubbles: true,
                        composed: true,
                        inputType: 'deleteContentBackward',
                        data: null
                    }));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return;
                }

                try {
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    document.execCommand('delete', false, null);
                    sel.removeAllRanges();
                } catch(e) {}

                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    composed: true,
                    inputType: 'deleteContentBackward',
                    data: null
                }));
                """,
                composer,
            )
            sleep(0.2)
            if not _visible_text(composer):
                return True
        except Exception:
            pass

    # 4) Lớp cuối: ép DOM rỗng + phát event để state editor đồng bộ.
    if driver is not None:
        try:
            driver.execute_script(
                r"""
                const el = arguments[0];
                const tag = (el.tagName || '').toLowerCase();
                try { el.focus(); } catch(e) {}

                if (tag === 'textarea' || tag === 'input') {
                    try {
                        const setter = Object.getOwnPropertyDescriptor(
                            tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
                            'value'
                        ).set;
                        setter.call(el, '');
                    } catch(e) {
                        el.value = '';
                    }
                } else {
                    el.innerHTML = '<p><br></p>';
                    try {
                        const sel = window.getSelection();
                        sel.removeAllRanges();
                        const range = document.createRange();
                        range.selectNodeContents(el);
                        range.collapse(true);
                        sel.addRange(range);
                    } catch(e) {}
                }

                el.dispatchEvent(new InputEvent('beforeinput', {
                    bubbles: true,
                    composed: true,
                    inputType: 'deleteContentBackward',
                    data: null
                }));
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    composed: true,
                    inputType: 'deleteContentBackward',
                    data: null
                }));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                """,
                composer,
            )
            sleep(0.25)
        except Exception:
            pass

    return not bool(_visible_text(composer))


def get_chatgpt_composer_text(driver, composer=None):
    """
    Đọc text composer hiện tại một cách chống stale/re-render.

    ChatGPT có thể thay node ProseMirror ngay sau Ctrl+V, vì vậy không được chỉ
    đọc element `composer` cũ. Hàm này luôn thử reacquire composer mới và quét
    các editor visible trước khi kết luận rỗng.
    """
    candidates = []

    if composer is not None:
        candidates.append(composer)

    try:
        fresh = find_chatgpt_composer(driver)
        if fresh is not None:
            candidates.append(fresh)
    except Exception:
        pass

    selectors = [
        "#prompt-textarea",
        "textarea[data-testid='prompt-textarea']",
        "div[contenteditable='true'][data-virtualkeyboard='true']",
        "div[contenteditable='true'].ProseMirror",
        "div[contenteditable='true']",
    ]
    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if el.is_displayed():
                        candidates.append(el)
                except Exception:
                    continue
        except Exception:
            continue

    seen = set()
    best = ""
    for el in candidates:
        try:
            key = getattr(el, 'id', None) or str(el)
        except Exception:
            key = str(id(el))
        if key in seen:
            continue
        seen.add(key)

        try:
            tag = (el.tag_name or "").lower()
        except Exception:
            continue

        text = ""
        try:
            if tag in {"textarea", "input"}:
                text = el.get_attribute("value") or ""
            else:
                text = driver.execute_script(
                    "return arguments[0].innerText || arguments[0].textContent || '';",
                    el,
                ) or ""
        except Exception:
            try:
                text = el.text or ""
            except Exception:
                text = ""

        if len(text) > len(best):
            best = text

    return best

def normalize_compare_text(text):
    """Chuẩn hóa nhẹ để so nội dung composer với prompt gốc."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def insert_prompt_with_cdp(driver, composer, text):
    """
    Nhập text bằng Chrome DevTools Input.insertText.

    KHÔNG phát sinh sự kiện clipboard/paste nên ChatGPT không biến transcript dài
    thành thẻ "pasted text / Show in text field".
    """
    composer.click()

    # Chunk vừa phải để ổn định với prompt/transcript dài.
    chunk_size = 1800
    total = len(text)

    for offset in range(0, total, chunk_size):
        chunk = text[offset:offset + chunk_size]
        driver.execute_cdp_cmd("Input.insertText", {"text": chunk})
        # Cho ProseMirror/React kịp nhận input giữa các chunk.
        sleep(0.025)

    sleep(0.5)
    return True


def insert_prompt_with_js(driver, composer, text):
    """Fallback không dùng clipboard nếu CDP Input.insertText không hoạt động."""
    try:
        composer.click()
        chunk_size = 1200
        for offset in range(0, len(text), chunk_size):
            chunk = text[offset:offset + chunk_size]
            ok = driver.execute_script(
                """
                const el = arguments[0];
                const txt = arguments[1];
                el.focus();
                try {
                    return document.execCommand('insertText', false, txt);
                } catch (e) {
                    return false;
                }
                """,
                composer,
                chunk,
            )
            if ok is False:
                return False
            sleep(0.03)
        sleep(0.5)
        return True
    except Exception:
        return False


def _loose_compare_text(text):
    """Chuẩn hóa mạnh để kiểm tra nội dung ProseMirror mà không phụ thuộc dấu câu/xuống dòng."""
    text = (text or "").replace("’", "'").replace("“", '"').replace("”", '"')
    text = text.lower()
    text = re.sub(r"[^0-9a-zA-ZÀ-ỹ:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def verify_prompt_in_composer(driver, composer, expected_text):
    """
    Kiểm tra prompt + transcript đã vào đủ trước khi Send.

    Không còn so nguyên văn 350 ký tự cuối vì ProseMirror có thể tự đổi xuống dòng,
    dấu nháy và khoảng trắng làm báo lỗi giả. Thay vào đó:
      1) kiểm tra lượng text hợp lý;
      2) lấy timestamp CUỐI CÙNG thực sự xuất hiện ở đầu dòng trong full prompt;
      3) kiểm tra timestamp cuối đó có trong composer;
      4) kiểm tra vài từ cuối của chính dòng transcript cuối theo kiểu loose compare.
    """
    actual = get_chatgpt_composer_text(driver, composer)
    exp_n = normalize_compare_text(expected_text)
    act_n = normalize_compare_text(actual)

    # DOM ChatGPT có thể thêm vài ký tự/node nên chỉ cần không bị hụt đáng kể.
    length_ok = len(act_n) >= int(len(exp_n) * 0.90)

    # Timestamp transcript sạch có dạng HH:MM:SS ở đầu dòng. Lấy mốc cuối cùng.
    timestamp_matches = re.findall(
        r"(?m)^\s*(\d{2}:\d{2}:\d{2})\s+.+$",
        expected_text or "",
    )
    last_ts = timestamp_matches[-1] if timestamp_matches else ""
    last_ts_ok = bool(last_ts) and last_ts in act_n

    # Lấy dòng cuối tương ứng với last_ts và so vài từ cuối, bỏ qua dấu câu/xuống dòng.
    last_line = ""
    if last_ts:
        for line in reversed((expected_text or "").splitlines()):
            if line.strip().startswith(last_ts + " "):
                last_line = line.strip()[len(last_ts):].strip()
                break

    loose_actual = _loose_compare_text(act_n)
    loose_last_line = _loose_compare_text(last_line)
    tail_words_ok = True
    tail_phrase = ""

    if loose_last_line:
        words = loose_last_line.split()
        # Dùng tối đa 10 từ cuối; với dòng ngắn thì dùng toàn bộ.
        tail_phrase = " ".join(words[-10:])
        tail_words_ok = bool(tail_phrase) and tail_phrase in loose_actual

    print(
        f"🧪 Kiểm tra ô ChatGPT: {len(act_n):,}/{len(exp_n):,} ký tự "
        f"| mốc cuối={last_ts or 'N/A'}:{'OK' if last_ts_ok else 'CHƯA THẤY'} "
        f"| lời cuối={'OK' if tail_words_ok else 'CHƯA THẤY'}"
    )

    # Timestamp cuối + độ dài là hai tín hiệu chính. Lời cuối là lớp kiểm tra bổ sung.
    return length_ok and last_ts_ok and tail_words_ok


def paste_prompt_into_chatgpt(driver, composer, text):
    """
    Tên hàm giữ nguyên để phần còn lại của pipeline không phải đổi.

    Bản mới TUYỆT ĐỐI KHÔNG Ctrl+V prompt dài, vì ChatGPT có thể biến paste
    thành file/thẻ "Show in text field". Thay vào đó nhập trực tiếp vào composer.
    """
    clear_chatgpt_composer(composer)

    print("⌨️ Đang nhập trực tiếp prompt + transcript vào ô ChatGPT (không dùng clipboard)...")

    try:
        insert_prompt_with_cdp(driver, composer, text)
    except Exception as exc:
        print(f"⚠️ CDP Input.insertText lỗi: {exc}")
        clear_chatgpt_composer(composer)
        if not insert_prompt_with_js(driver, composer, text):
            print("❌ Không nhập được prompt bằng CDP hoặc JS fallback.")
            return False

    if verify_prompt_in_composer(driver, composer, text):
        print("✅ Toàn bộ prompt + transcript đã nằm trong ô ChatGPT.")
        return True

    # Thử lại một lần bằng JS nếu lần CDP đầu bị hụt text.
    print("⚠️ Nội dung trong ô chat chưa đủ. Đang nhập lại bằng fallback...")
    clear_chatgpt_composer(composer)

    if not insert_prompt_with_js(driver, composer, text):
        return False

    if verify_prompt_in_composer(driver, composer, text):
        print("✅ Toàn bộ prompt + transcript đã nằm trong ô ChatGPT sau fallback.")
        return True

    print("❌ Kiểm tra thất bại: transcript chưa vào đầy đủ nên KHÔNG bấm Send.")
    return False

def find_send_button(driver):
    """Tìm đúng nút Send hiện tại của ChatGPT, ưu tiên selector user cung cấp."""
    selectors = [
        "button#composer-submit-button[data-testid='send-button'][aria-disabled='false']",
        "button#composer-submit-button[data-testid='send-button']",
        "button#composer-submit-button[aria-label='Send prompt']",
        "button[data-testid='send-button'][aria-disabled='false']",
        "button[data-testid='send-button']",
        "button[aria-label='Send prompt']",
        "button[aria-label='Send']",
        "button[aria-label='Gửi lời nhắc']",
        "button[aria-label='Gửi']",
    ]

    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    aria_disabled = (el.get_attribute("aria-disabled") or "").lower()
                    disabled_attr = el.get_attribute("disabled")
                    if (
                        el.is_displayed()
                        and el.is_enabled()
                        and aria_disabled != "true"
                        and disabled_attr is None
                    ):
                        return el
                except Exception:
                    continue
        except Exception:
            pass
    return None


def chatgpt_submission_started(driver, before_text=""):
    """Xác nhận prompt đã thực sự được submit, không chỉ click giả."""
    # Khi ChatGPT bắt đầu trả lời thường xuất hiện nút Stop.
    stop_selectors = [
        "button[data-testid='stop-button']",
        "button[aria-label='Stop generating']",
        "button[aria-label='Stop streaming']",
        "button[aria-label='Dừng tạo']",
    ]
    for selector in stop_selectors:
        try:
            if any(el.is_displayed() for el in driver.find_elements(By.CSS_SELECTOR, selector)):
                return True
        except Exception:
            pass

    # Sau khi gửi, composer thường bị clear.
    try:
        composer = find_chatgpt_composer(driver)
        if composer:
            now_text = normalize_compare_text(
                get_chatgpt_composer_text(driver, composer)
            )
            old_text = normalize_compare_text(before_text)
            if old_text and len(now_text) <= max(8, int(len(old_text) * 0.05)):
                return True
    except Exception:
        pass

    return False


def wait_chatgpt_submission(driver, before_text, timeout=8):
    end_time = time.time() + timeout
    while time.time() < end_time:
        if chatgpt_submission_started(driver, before_text):
            return True
        sleep(0.20)
    return False


def click_send(driver):
    """
    Gửi prompt theo nhiều lớp fallback:
    1) click đúng #composer-submit-button
    2) JavaScript click selector chính xác
    3) ENTER trong composer

    Sau mỗi cách đều kiểm tra prompt đã thực sự được submit.
    """
    composer = find_chatgpt_composer(driver)
    before_text = ""
    if composer:
        before_text = get_chatgpt_composer_text(driver, composer)

    print("📤 Đang bấm Send prompt...")

    # --------------------------------------------------------
    # CÁCH 1: Selenium click đúng button hiện tại
    # --------------------------------------------------------
    try:
        button = WebDriverWait(driver, 30).until(lambda d: find_send_button(d))
        print(
            "✅ Đã tìm thấy nút Send: "
            f"id={button.get_attribute('id')} | "
            f"testid={button.get_attribute('data-testid')} | "
            f"aria-disabled={button.get_attribute('aria-disabled')}"
        )

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();",
                button,
            )
        except Exception:
            pass

        try:
            button.click()
            if wait_chatgpt_submission(driver, before_text):
                print("🚀 Đã gửi prompt bằng click nút Send.")
                return True
        except Exception as exc:
            print(f"⚠️ button.click() chưa được: {exc}")

        # ----------------------------------------------------
        # CÁCH 2: JS click chính element + query selector mới
        # ----------------------------------------------------
        try:
            driver.execute_script("arguments[0].click();", button)
            if wait_chatgpt_submission(driver, before_text):
                print("🚀 Đã gửi prompt bằng JS click.")
                return True
        except Exception as exc:
            print(f"⚠️ JS click element chưa được: {exc}")

    except Exception as exc:
        print(f"⚠️ Chưa bắt được nút Send bằng Selenium: {exc}")

    # Query selector trực tiếp, tránh element stale.
    try:
        clicked = driver.execute_script(
            """
            const selectors = [
              "button#composer-submit-button[data-testid='send-button'][aria-disabled='false']",
              "button#composer-submit-button[data-testid='send-button']",
              "button[data-testid='send-button'][aria-disabled='false']",
              "button[data-testid='send-button']"
            ];
            for (const sel of selectors) {
              const b = document.querySelector(sel);
              if (b && b.getAttribute('aria-disabled') !== 'true' && !b.disabled) {
                b.focus();
                b.click();
                return sel;
              }
            }
            return null;
            """
        )
        if clicked:
            print(f"🖱️ Đã JS click trực tiếp selector: {clicked}")
            if wait_chatgpt_submission(driver, before_text):
                print("🚀 ChatGPT đã nhận prompt.")
                return True
    except Exception as exc:
        print(f"⚠️ JS querySelector click lỗi: {exc}")

    # --------------------------------------------------------
    # CÁCH 3: ENTER fallback
    # --------------------------------------------------------
    composer = find_chatgpt_composer(driver)
    if composer:
        try:
            print("↩️ Nút Send chưa chạy, thử nhấn ENTER trong ô ChatGPT...")
            composer.click()
            composer.send_keys(Keys.ENTER)
            if wait_chatgpt_submission(driver, before_text):
                print("🚀 Đã gửi prompt bằng ENTER.")
                return True
        except Exception as exc:
            print(f"⚠️ ENTER fallback lỗi: {exc}")

    print("❌ Đã thử click + JS click + ENTER nhưng ChatGPT vẫn chưa nhận prompt.")
    return False

def get_assistant_turns(driver):
    selectors = [
        "article[data-turn='assistant']",
        "[data-message-author-role='assistant']",
    ]
    for selector in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            if els:
                return els
        except Exception:
            pass
    return []


def get_turn_text(element):
    try:
        return (element.text or "").strip()
    except Exception:
        return ""




def install_chatgpt_429_network_observer(driver):
    """
    Cài observer THỤ ĐỘNG trong trang ChatGPT để ghi nhận response 429 từ
    /backend-api/conversations mà KHÔNG tự gửi thêm request nào.

    - Hook fetch + XMLHttpRequest cho request phát sinh SAU khi observer được cài.
    - Request đã xảy ra trong lúc driver.get(...) được kiểm tra thêm qua
      PerformanceResourceTiming.responseStatus ở hàm scan bên dưới.
    - State nằm trong window và tự reset khi điều hướng sang document mới.
    """
    if not driver_alive(driver):
        return False
    script = r"""
        try {
          if (window.__pipeline429ObserverInstalled) return true;
          window.__pipeline429ObserverInstalled = true;
          window.__pipeline429Events = window.__pipeline429Events || [];
          window.__pipeline429Seen = window.__pipeline429Seen || {};

          const TARGET = '/backend-api/conversations';
          const record = (url, status, source) => {
            try {
              url = String(url || '');
              status = Number(status || 0);
              if (status !== 429 || !url.includes(TARGET)) return;
              const key = source + '|' + url + '|' + Date.now();
              window.__pipeline429Events.push({
                url, status, source, ts: Date.now(), key
              });
              if (window.__pipeline429Events.length > 50) {
                window.__pipeline429Events = window.__pipeline429Events.slice(-50);
              }
            } catch (e) {}
          };

          if (typeof window.fetch === 'function' && !window.fetch.__pipeline429Wrapped) {
            const originalFetch = window.fetch;
            const wrappedFetch = async function(...args) {
              const response = await originalFetch.apply(this, args);
              try {
                const req = args && args.length ? args[0] : '';
                const url = (typeof req === 'string') ? req : (req && req.url) || response.url || '';
                record(url, response.status, 'fetch');
              } catch (e) {}
              return response;
            };
            try { wrappedFetch.__pipeline429Wrapped = true; } catch (e) {}
            window.fetch = wrappedFetch;
          }

          if (window.XMLHttpRequest && !XMLHttpRequest.prototype.__pipeline429Wrapped) {
            const origOpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
              try { this.__pipeline429Url = String(url || ''); } catch (e) {}
              try {
                this.addEventListener('loadend', function() {
                  try { record(this.__pipeline429Url || this.responseURL || '', this.status, 'xhr'); } catch (e) {}
                }, {once: true});
              } catch (e) {}
              return origOpen.call(this, method, url, ...rest);
            };
            try { XMLHttpRequest.prototype.__pipeline429Wrapped = true; } catch (e) {}
          }
          return true;
        } catch (e) {
          return false;
        }
    """
    try:
        return bool(driver.execute_script(script))
    except Exception:
        return False


def scan_chatgpt_conversations_429(driver):
    """
    Trả về các event 429 MỚI của /backend-api/conversations.

    Nguồn:
      1) fetch/XHR observer (nếu đã cài),
      2) PerformanceResourceTiming.responseStatus để bắt request xảy ra trong
         lúc trang Project đang load trước khi observer kịp cài.

    Không phát sinh network request mới.
    """
    if not driver_alive(driver):
        return []

    script = r"""
        try {
          const TARGET = '/backend-api/conversations';
          window.__pipeline429Events = window.__pipeline429Events || [];
          window.__pipeline429Seen = window.__pipeline429Seen || {};
          const out = [];

          // 1) Drain observer events.
          const queued = Array.isArray(window.__pipeline429Events)
            ? window.__pipeline429Events.splice(0, window.__pipeline429Events.length)
            : [];
          for (const ev of queued) {
            const url = String((ev && ev.url) || '');
            const status = Number((ev && ev.status) || 0);
            if (status !== 429 || !url.includes(TARGET)) continue;
            const key = 'hook|' + String((ev && ev.source) || '') + '|' + url + '|' + String((ev && ev.ts) || '');
            if (window.__pipeline429Seen[key]) continue;
            window.__pipeline429Seen[key] = true;
            out.push({url, status, source: String((ev && ev.source) || 'hook')});
          }

          // 2) Resource Timing for requests that happened during page load.
          try {
            const resources = performance.getEntriesByType('resource') || [];
            for (const e of resources) {
              const url = String(e.name || '');
              if (!url.includes(TARGET)) continue;
              const status = Number(e.responseStatus || 0);
              if (status !== 429) continue;
              const key = 'resource|' + url + '|' + String(Math.round(Number(e.startTime || 0) * 1000));
              if (window.__pipeline429Seen[key]) continue;
              window.__pipeline429Seen[key] = true;
              out.push({url, status, source: 'resource_timing'});
            }
          } catch (e) {}

          return out;
        } catch (e) {
          return [];
        }
    """
    try:
        events = driver.execute_script(script) or []
        return events if isinstance(events, list) else []
    except Exception:
        return []


def handle_chatgpt_conversations_api_429(driver, quiet=False):
    """
    Nếu browser vừa gặp HTTP 429 ở /backend-api/conversations thì KHÔNG coi
    câu trả lời model hiện tại là lỗi. Chỉ đóng popup "Too many requests" ->
    "Got it" nếu popup xuất hiện.

    Trả True nếu phát hiện API 429 hoặc đã dismiss popup.
    """
    if not driver_alive(driver):
        return False

    # Cài observer cho các request tiếp theo (không gửi request mới).
    install_chatgpt_429_network_observer(driver)
    events = scan_chatgpt_conversations_429(driver)

    if events and not quiet:
        print(
            "⚠️ Phát hiện ChatGPT API /backend-api/conversations trả HTTP 429. "
            "Không bỏ answer; chỉ tự đóng popup 'Got it'."
        )

    dismissed = dismiss_chatgpt_too_many_requests_popup(driver, quiet=quiet)
    if dismissed:
        return True

    # API 429 có thể tới trước khi dialog render vài trăm ms.
    if events:
        deadline = time.time() + max(0.0, float(CHATGPT_API429_POPUP_WAIT))
        while time.time() < deadline:
            if dismiss_chatgpt_too_many_requests_popup(driver, quiet=quiet):
                return True
            sleep(0.12)
        return True

    return False

def dismiss_chatgpt_too_many_requests_popup(driver, quiet=False):
    """
    Đóng ĐÚNG popup ChatGPT có heading "Too many requests" bằng nút "Got it".

    Popup user cung cấp có dạng:
      div[role="dialog"][data-state="open"]
        h2 -> Too many requests
        button -> Got it

    Chỉ dismiss UI. Không coi popup là lỗi nếu câu trả lời AI vẫn tồn tại.
    """
    if not driver_alive(driver):
        return False

    dismissed = False
    try:
        dialogs = driver.find_elements(By.CSS_SELECTOR, "div[role='dialog'][data-state='open'], div[role='dialog']")
    except Exception:
        dialogs = []

    for dialog in dialogs:
        try:
            if not dialog.is_displayed():
                continue

            heading = ""
            try:
                heads = dialog.find_elements(By.CSS_SELECTOR, "h1, h2, h3, [role='heading']")
                heading = " ".join((h.text or "").strip() for h in heads if (h.text or "").strip())
            except Exception:
                pass

            dialog_text = ""
            try:
                dialog_text = (dialog.text or "").strip()
            except Exception:
                pass

            signature = f"{heading}\n{dialog_text}".lower()
            if "too many requests" not in signature:
                continue

            got_it = None
            try:
                for button in dialog.find_elements(By.CSS_SELECTOR, "button"):
                    text = re.sub(r"\s+", " ", (button.text or "").strip()).lower()
                    if text == "got it":
                        got_it = button
                        break
            except Exception:
                pass

            if got_it is None:
                # Fallback DOM search chỉ bên trong đúng dialog Too many requests.
                try:
                    got_it = driver.execute_script(
                        """
                        const dialogs = [...document.querySelectorAll("div[role='dialog']")];
                        for (const d of dialogs) {
                          const txt = (d.innerText || '').toLowerCase();
                          if (!txt.includes('too many requests')) continue;
                          const buttons = [...d.querySelectorAll('button')];
                          const b = buttons.find(x => (x.innerText || '').trim().toLowerCase() === 'got it');
                          if (b) return b;
                        }
                        return null;
                        """
                    )
                except Exception:
                    got_it = None

            if got_it is None:
                if not quiet:
                    print("⚠️ Thấy popup 'Too many requests' nhưng chưa bắt được nút 'Got it'.")
                continue

            try:
                got_it.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", got_it)
                except Exception:
                    continue

            dismissed = True
            if not quiet:
                print("✅ Đã tự nhấn 'Got it' để đóng popup Too many requests.")

            # Chờ popup đóng thật, nhưng không chặn pipeline lâu.
            end = time.time() + 3.0
            while time.time() < end:
                still_open = False
                try:
                    for d in driver.find_elements(By.CSS_SELECTOR, "div[role='dialog'][data-state='open'], div[role='dialog']"):
                        try:
                            if d.is_displayed() and "too many requests" in ((d.text or "").lower()):
                                still_open = True
                                break
                        except Exception:
                            continue
                except Exception:
                    still_open = False
                if not still_open:
                    break
                sleep(0.15)
        except Exception:
            continue

    return dismissed

def generation_in_progress(driver):
    selectors = [
        "button[data-testid='stop-button']",
        "button[aria-label*='Stop' i]",
        "button[aria-label*='Dừng' i]",
    ]
    for selector in selectors:
        try:
            if any(el.is_displayed() for el in driver.find_elements(By.CSS_SELECTOR, selector)):
                return True
        except Exception:
            pass
    return False


def wait_for_chatgpt_answer(driver, previous_count):
    deadline = time.time() + WAIT_CHATGPT_RESPONSE
    last_text = ""
    stable_since = None
    last_turn = None

    while time.time() < deadline:
        # Nếu /backend-api/conversations trả 429 hoặc popup Too many requests xuất hiện
        # nhưng AI vẫn đang/đã trả lời, chỉ nhấn Got it; KHÔNG bỏ answer hiện tại.
        handle_chatgpt_conversations_api_429(driver, quiet=True)
        turns = get_assistant_turns(driver)

        if len(turns) > previous_count:
            last_turn = turns[-1]
            text = get_turn_text(last_turn)

            if text:
                if text == last_text:
                    if stable_since is None:
                        stable_since = time.time()
                else:
                    last_text = text
                    stable_since = time.time()

                # Kết thúc khi không còn nút Stop và text đã ổn định >= 2 giây.
                if (
                    not generation_in_progress(driver)
                    and stable_since is not None
                    and time.time() - stable_since >= 2.0
                ):
                    return last_turn, text

        sleep(0.25)

    if last_turn is not None and last_text:
        print("⚠️ Hết thời gian đợi trạng thái hoàn tất, dùng text cuối đang có.")
        return last_turn, last_text

    raise TimeoutException("ChatGPT không trả về câu trả lời trong thời gian chờ")



def click_copy_last_answer(driver, last_turn):
    """
    DOM text là nguồn chính. Copy chỉ là lớp phụ.
    Chỉ dùng clipboard nếu sau click nó thật sự chứa output mới có DONE.
    """
    if not CLICK_CHATGPT_COPY:
        return None

    # Clear clipboard trước để không vô tình đọc response cũ.
    if os.name == "nt":
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ''"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    selectors = [
        "button[data-testid='copy-turn-action-button']",
        "button[aria-label='Copy']",
        "button[aria-label='Copy response']",
        "button[aria-label='Sao chép']",
        "button[aria-label='Sao chép câu trả lời']",
    ]

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", last_turn)
        ActionChains(driver).move_to_element(last_turn).pause(0.4).perform()
    except Exception:
        pass

    buttons = []
    for selector in selectors:
        try:
            buttons.extend(last_turn.find_elements(By.CSS_SELECTOR, selector))
        except Exception:
            pass

    # SVG fallback user từng cung cấp.
    try:
        for use in last_turn.find_elements(By.XPATH, ".//*[name()='use' and contains(@href, '#7fccfb')]"):
            try:
                buttons.append(use.find_element(By.XPATH, "./ancestor::button[1]"))
            except Exception:
                pass
    except Exception:
        pass

    seen = set()
    for button in buttons:
        try:
            key = button.id
            if key in seen:
                continue
            seen.add(key)
            if not button.is_displayed() or not button.is_enabled():
                continue
            try:
                button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", button)
            sleep(0.4)
            copied = (read_clipboard_text_windows() or "").strip()
            if copied and re.search(r"(?im)^DONE\s*$", copied):
                print("✅ Copy response thành công.")
                return copied
        except Exception:
            continue

    print("⚠️ Copy không xác nhận được; dùng text DOM của câu trả lời.")
    return None

def _chatgpt_body_text(driver):
    try:
        return driver.execute_script(
            "return (document.body && document.body.innerText) ? document.body.innerText : '';"
        ) or ""
    except Exception:
        try:
            return driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return ""


def chatgpt_has_upload_error(driver):
    """Bắt toast/lỗi upload nếu UI có báo. Với chế độ paste-text bình thường sẽ không xuất hiện."""
    body = _chatgpt_body_text(driver).lower()
    needles = (
        "unable to upload",
        "failed to upload",
        "upload failed",
        "không thể tải lên",
        "tải tệp không thành công",
        "unsupported file",
        "file is too large",
        "tệp quá lớn",
    )
    return any(x in body for x in needles)


def pasted_text_attachment_count(driver):
    """
    Đếm pasted-text cards của ChatGPT.
    UI hiện tại thường có nút/text `Show in text field` trong mỗi card.
    Có fallback nhiều ngôn ngữ để tránh phụ thuộc đúng một selector.
    """
    try:
        return int(driver.execute_script(
            r"""
            const needles = [
              'show in text field',
              'show in text box',
              'hiển thị trong trường văn bản',
              'hiện trong trường văn bản',
              'pasted text'
            ];
            const els = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            let count = 0;
            for (const el of els) {
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') continue;
              const t = ((el.innerText || el.textContent || '') + ' ' +
                         (el.getAttribute('aria-label') || '')).trim().toLowerCase();
              if (needles.some(n => t.includes(n))) count++;
            }
            return count;
            """
        ) or 0)
    except Exception:
        return 0


def _visible_marker_in_chatgpt(driver, marker):
    if not marker:
        return False
    try:
        body = _chatgpt_body_text(driver)
        return marker in body
    except Exception:
        return False


def build_paste_attachment_payload(file_path, kind):
    """
    Giữ PROMPT và TRANSCRIPT là hai payload độc lập.
    Header marker nằm ở dòng đầu để card preview dễ xác nhận đúng attachment.
    File gốc trên đĩa KHÔNG bị sửa.
    """
    path = Path(file_path)
    content = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    kind = kind.upper().strip()
    marker = f"[[{kind}_FILE:{path.name}]]"
    end_marker = f"[[END_{kind}_FILE]]"
    payload = f"{marker}\n{content}\n{end_marker}"
    return payload, marker, end_marker


def _paste_clipboard_once(composer, payload):
    if not set_clipboard_text_windows(payload):
        return False
    try:
        composer.click()
        composer.send_keys(Keys.CONTROL, "v")
        return True
    except Exception:
        try:
            ActionChains(composer.parent).key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform()
            return True
        except Exception:
            return False


def paste_text_attachment_from_file(driver, composer, file_path, kind, step_label, timeout=PASTE_ATTACHMENT_WAIT):
    """
    KHÔNG dùng input[type=file].

    Cách làm giống thao tác tay của user:
      1) đọc file;
      2) copy TOÀN BỘ text vào clipboard;
      3) Ctrl+V vào ChatGPT;
      4) ChatGPT tự biến big-paste thành pasted-text attachment;
      5) chỉ trả True khi xác nhận card mới xuất hiện / marker đã chuyển ra khỏi composer.

    Nếu ChatGPT giữ nguyên text dài trong composer thay vì tạo attachment thì coi là FAIL,
    tuyệt đối không sang bước tiếp theo để tránh trộn PROMPT + TRANSCRIPT.
    """
    file_path = Path(file_path).resolve()
    if not file_path.exists() or file_path.stat().st_size <= 0:
        print(f"❌ {step_label}: file không tồn tại/rỗng: {file_path}")
        return False

    payload, marker, end_marker = build_paste_attachment_payload(file_path, kind)

    for attempt in range(1, CHATGPT_PASTE_RETRIES + 1):
        before_count = pasted_text_attachment_count(driver)
        before_body = _chatgpt_body_text(driver)

        # Composer phải không có một khối text dài trước khi paste attachment tiếp theo.
        existing_inline = normalize_compare_text(get_chatgpt_composer_text(driver, composer))
        if len(existing_inline) > 50:
            print(f"⚠️ {step_label}: composer còn {len(existing_inline):,} ký tự inline; đang dọn trước khi paste.")
            clear_chatgpt_composer(composer)
            sleep(0.3)

        current_payload = payload
        # Nếu lần đầu ChatGPT không tự convert prompt ngắn thành attachment,
        # retry bằng padding chỉ gồm xuống dòng SAU END marker. Nội dung quy tắc không đổi.
        if attempt > 1 and len(current_payload) < 14000:
            current_payload = current_payload + ("\n" * (14000 - len(current_payload)))

        print(
            f"📋 {step_label}: COPY/PASTE {file_path.name} "
            f"({len(payload):,} ký tự, lần {attempt}/{CHATGPT_PASTE_RETRIES})..."
        )

        if not _paste_clipboard_once(composer, current_payload):
            print(f"⚠️ {step_label}: Ctrl+V thất bại.")
            continue

        deadline = time.time() + timeout
        stable_since = None
        saw_inline_payload = False

        while time.time() < deadline:
            if chatgpt_has_upload_error(driver):
                print(f"❌ {step_label}: ChatGPT hiện toast lỗi upload; KHÔNG đi tiếp.")
                return False

            count_now = pasted_text_attachment_count(driver)
            body_now = _chatgpt_body_text(driver)
            composer_text = normalize_compare_text(get_chatgpt_composer_text(driver, composer))

            count_increased = count_now > before_count
            marker_newly_visible = marker in body_now and marker not in before_body
            marker_left_composer = marker not in composer_text
            inline_large = len(composer_text) >= int(len(normalize_compare_text(payload)) * 0.70)
            saw_inline_payload = saw_inline_payload or inline_large

            # Tín hiệu mạnh nhất: card count tăng.
            # Fallback: marker mới xuất hiện ngoài composer và composer đã được ChatGPT clear.
            attachment_confirmed = count_increased or (
                marker_newly_visible and marker_left_composer and len(composer_text) < 300
            )

            if attachment_confirmed:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= 0.8:
                    print(
                        f"✅ {step_label}: pasted-text attachment OK "
                        f"| cards {before_count} → {count_now} | marker=OK"
                    )
                    return True
            else:
                stable_since = None

            sleep(0.25)

        # Nếu hết timeout mà text vẫn nằm inline => không được coi là thành công.
        composer_text = normalize_compare_text(get_chatgpt_composer_text(driver, composer))
        if saw_inline_payload or len(composer_text) > 500:
            print(
                f"⚠️ {step_label}: ChatGPT chưa convert thành attachment; "
                f"text vẫn nằm inline ({len(composer_text):,} ký tự)."
            )
            clear_chatgpt_composer(composer)
            sleep(0.5)
        else:
            print(f"⚠️ {step_label}: chưa xác nhận được pasted-text card.")

    print(f"❌ {step_label}: thất bại sau {CHATGPT_PASTE_RETRIES} lần. Pipeline DỪNG.")
    return False


def two_paste_attachments_still_present(driver, prompt_marker, transcript_marker, min_cards=2):
    """Chốt cuối trước Send: phải còn đủ hai attachment riêng."""
    body = _chatgpt_body_text(driver)
    cards = pasted_text_attachment_count(driver)
    markers_ok = prompt_marker in body and transcript_marker in body
    # Nếu UI ẩn marker trong card sau khi render, 2 card rõ ràng vẫn là tín hiệu đủ mạnh.
    return markers_ok or cards >= min_cards


def set_short_instruction(driver, composer, text):
    """Chỉ nhập câu lệnh ngắn; prompt/transcript đều nằm trong 2 file riêng."""
    clear_chatgpt_composer(composer)
    try:
        composer.click()
        driver.execute_cdp_cmd("Input.insertText", {"text": text})
        sleep(0.3)
    except Exception:
        try:
            composer.send_keys(text)
            sleep(0.3)
        except Exception as exc:
            print(f"❌ Không nhập được câu lệnh ngắn: {exc}")
            return False

    actual = normalize_compare_text(get_chatgpt_composer_text(driver, composer))
    expected = normalize_compare_text(text)
    ok = expected in actual or actual in expected
    print(
        f"🧪 BƯỚC 5/5 - kiểm tra câu lệnh ngắn: "
        f"{'OK' if ok else 'CHƯA ĐÚNG'} ({len(actual)}/{len(expected)} ký tự)"
    )
    return ok


def ask_chatgpt(driver, prompt_path, transcript_path):
    """
    Pipeline ChatGPT: 2 PASTED-TEXT ATTACHMENT TÁCH BIỆT, xác nhận từng bước.

    BƯỚC 3: copy/paste PROMPT.txt -> ChatGPT tự tạo pasted-text card -> verify.
    BƯỚC 4: copy/paste TRANSCRIPT.txt -> card RIÊNG -> verify.
    BƯỚC 5: nhập câu lệnh ngắn -> verify -> Send.

    Không dùng input[type=file], nên không còn lỗi `Unable to upload *.txt` của uploader.
    """
    if NEW_CHAT_EACH_VIDEO:
        driver.get(CHATGPT_HOME)

    try:
        composer = wait_chatgpt_composer(driver)
    except Exception as exc:
        print(f"❌ Không thấy ô nhập ChatGPT: {exc}")
        return None

    previous_count = len(get_assistant_turns(driver))

    print("\n" + "=" * 72)
    print("CHATGPT - 2 PASTED-TEXT ATTACHMENTS, XÁC NHẬN TỪNG BƯỚC")
    print("=" * 72)

    # Dọn text inline cũ. New chat nên không có attachment cũ.
    clear_chatgpt_composer(composer)
    base_cards = pasted_text_attachment_count(driver)

    prompt_payload, prompt_marker, _ = build_paste_attachment_payload(prompt_path, "PROMPT")
    transcript_payload, transcript_marker, _ = build_paste_attachment_payload(transcript_path, "TRANSCRIPT")

    # 3) PROMPT pasted attachment riêng.
    if not paste_text_attachment_from_file(
        driver, composer, prompt_path, "PROMPT", "BƯỚC 3/5 - PROMPT PASTE"
    ):
        print("⛔ DỪNG: PROMPT chưa thành attachment riêng, KHÔNG paste transcript.")
        return None

    # Phải tăng ít nhất một card trước khi sang transcript.
    cards_after_prompt = pasted_text_attachment_count(driver)
    if cards_after_prompt <= base_cards and not _visible_marker_in_chatgpt(driver, prompt_marker):
        print("⛔ DỪNG: không xác nhận được PROMPT card sau bước 3.")
        return None

    # 4) TRANSCRIPT pasted attachment riêng.
    if not paste_text_attachment_from_file(
        driver, composer, transcript_path, "TRANSCRIPT", "BƯỚC 4/5 - TRANSCRIPT PASTE"
    ):
        print("⛔ DỪNG: TRANSCRIPT chưa thành attachment riêng, KHÔNG Send.")
        return None

    cards_after_transcript = pasted_text_attachment_count(driver)
    prompt_ok = _visible_marker_in_chatgpt(driver, prompt_marker) or cards_after_prompt > base_cards
    transcript_ok = _visible_marker_in_chatgpt(driver, transcript_marker) or cards_after_transcript > cards_after_prompt

    print(
        "🔒 CHỐT 2 PASTE ATTACHMENT: "
        f"PROMPT={'OK' if prompt_ok else 'MẤT'} | "
        f"TRANSCRIPT={'OK' if transcript_ok else 'MẤT'} | "
        f"cards={cards_after_transcript}"
    )
    if not (prompt_ok and transcript_ok):
        print("⛔ DỪNG: chưa xác nhận đủ 2 pasted-text attachment, KHÔNG Send.")
        return None

    instruction = (
        "Bạn đang nhận 2 pasted-text attachment RIÊNG. "
        f"Attachment có marker {prompt_marker} là PROMPT/RULES bắt buộc; "
        f"attachment có marker {transcript_marker} là TRANSCRIPT cần phân tích. "
        "Hãy đọc TOÀN BỘ cả hai attachment, làm đúng PROMPT và chỉ trả kết quả theo định dạng trong PROMPT."
    )

    # Composer sau 2 big-paste attachments phải rỗng/nhỏ; mới nhập câu lệnh ngắn.
    composer = wait_chatgpt_composer(driver, timeout=30)
    if not set_short_instruction(driver, composer, instruction):
        print("⛔ DỪNG: câu lệnh ngắn chưa vào đúng, KHÔNG Send.")
        return None

    # Chốt cuối ngay trước Send.
    if not two_paste_attachments_still_present(
        driver, prompt_marker, transcript_marker, min_cards=base_cards + 2
    ):
        print("⛔ DỪNG: 2 pasted-text attachment không còn đủ ngay trước Send.")
        return None

    if chatgpt_has_upload_error(driver):
        print("⛔ DỪNG: UI đang có lỗi upload, KHÔNG Send.")
        return None

    if not click_send(driver):
        print("❌ Không gửi được prompt + 2 pasted-text attachment.")
        return None

    print("⌛ Đã gửi 2 pasted-text attachment. Đang đợi ChatGPT phân tích transcript...")
    last_turn, dom_text = wait_for_chatgpt_answer(driver, previous_count)

    copied = click_copy_last_answer(driver, last_turn)
    answer = (copied or dom_text or "").strip()

    print("✅ Đã nhận kết quả ChatGPT.")
    return answer




# ============================================================
# CHATGPT SMART INPUT OVERRIDES - PROMPT INLINE + TRANSCRIPT ATTACHMENT
# ============================================================

# NOTE:
# Các hàm bên dưới CỐ Ý override một số helper/ask_chatgpt ở phía trên.
# Lý do: bản final dùng chiến lược ổn định hơn:
#   - PROMPT.txt vẫn là file riêng trên đĩa, nhưng nội dung prompt được nhập thẳng vào composer.
#   - TRANSCRIPT.txt là attachment riêng.
#   - Ưu tiên upload thật qua nút + và input[type=file].
#   - Transcript chỉ được upload bằng uploader thật; không Ctrl+V transcript vào composer.
#   - Không bao giờ gửi nếu chưa verify prompt + transcript attachment.


def build_prompt_file_text(prompt_template, transcript_filename=None):
    """Prompt riêng KHÔNG chứa transcript và KHÔNG nhắc tên file cụ thể để tránh verify attachment false-positive."""
    replacement = (
        "[TRANSCRIPT KHÔNG NẰM TRONG PROMPT NÀY. "
        "HÃY ĐỌC TOÀN BỘ TRANSCRIPT ĐƯỢC ĐÍNH KÈM RIÊNG TRONG CÙNG TIN NHẮN.]"
    )
    if TRANSCRIPT_PLACEHOLDER in prompt_template:
        text = prompt_template.replace(TRANSCRIPT_PLACEHOLDER, replacement)
    else:
        text = (
            prompt_template.rstrip()
            + "\n\n## TRANSCRIPT CẦN PHÂN TÍCH\n\n"
            + replacement
        )
    return text.rstrip() + "\n"


def _ordered_find(haystack, needles):
    """Trả True khi mọi needle xuất hiện theo đúng thứ tự trong haystack."""
    pos = 0
    for needle in needles:
        needle = (needle or "").strip()
        if not needle:
            continue
        found = haystack.find(needle, pos)
        if found < 0:
            return False
        pos = found + len(needle)
    return True


def _prompt_headings(expected_text):
    """Lấy toàn bộ heading ## từ prompt gốc theo thứ tự."""
    return [
        _loose_compare_text(m.group(1))
        for m in re.finditer(r"(?m)^\s*##\s+(.+?)\s*$", expected_text or "")
        if _loose_compare_text(m.group(1))
    ]


def _prompt_checkpoints(expected_text, count=9, words_per_phrase=10):
    """
    Tạo các phrase rải đều từ đầu -> cuối prompt.
    Verify theo thứ tự giúp bắt lỗi ProseMirror chèn đoạn sau vào giữa đoạn trước.
    """
    loose = _loose_compare_text(expected_text)
    words = loose.split()
    if len(words) < words_per_phrase:
        return [loose] if loose else []

    count = max(3, min(count, max(3, len(words) // words_per_phrase)))
    max_start = max(0, len(words) - words_per_phrase)
    starts = []
    for i in range(count):
        frac = i / max(1, count - 1)
        idx = int(round(max_start * frac))
        if idx not in starts:
            starts.append(idx)
    return [" ".join(words[i:i + words_per_phrase]) for i in starts]


def verify_prompt_inline(driver, composer, expected_text, verbose=True):
    """
    VERIFY CHẶT prompt sau native Ctrl+V.

    Không chỉ check đầu/cuối. Bắt buộc:
      1) số từ không hụt đáng kể;
      2) TẤT CẢ heading ## xuất hiện đúng thứ tự;
      3) 9 checkpoint rải đều toàn prompt xuất hiện đúng thứ tự;
      4) chuỗi timestamp ví dụ trong prompt phải giữ ĐÚNG THỨ TỰ, ĐÚNG SỐ LƯỢNG.

    Nếu prompt bị lộn đoạn như trường hợp 00:00:00 --> 00: ... thì fail ngay.
    """
    actual = get_chatgpt_composer_text(driver, composer)
    exp_loose = _loose_compare_text(expected_text)
    act_loose = _loose_compare_text(actual)

    exp_words = exp_loose.split()
    act_words = act_loose.split()
    word_ratio = (len(act_words) / max(1, len(exp_words)))
    words_ok = PROMPT_MIN_WORD_RATIO <= word_ratio <= 1.08

    # Heading/checkpoint đều được sinh ĐỘNG từ chính prompt.txt hiện tại.
    # Không hard-code câu chữ của prompt cũ nữa, nên đổi prompt không làm verifier báo lỗi giả.
    headings = _prompt_headings(expected_text)
    headings_ok = True if not headings else _ordered_find(act_loose, headings)

    checkpoints = _prompt_checkpoints(expected_text)
    checkpoints_ok = True if not checkpoints else _ordered_find(act_loose, checkpoints)

    expected_ts = re.findall(r"\b\d{2,4}:\d{2}:\d{2}\b", expected_text or "")
    actual_ts = re.findall(r"\b\d{2,4}:\d{2}:\d{2}\b", actual or "")
    timestamps_ok = actual_ts == expected_ts

    # So khớp thứ tự TOÀN BỘ token sau khi normalize. Đây là kiểm tra generic:
    # - prompt đổi nội dung vẫn chạy;
    # - bắt được trường hợp ProseMirror làm đảo một khối text;
    # - không phụ thuộc một câu rule cụ thể nào.
    try:
        from difflib import SequenceMatcher
        sequence_ratio = SequenceMatcher(None, exp_words, act_words, autojunk=False).ratio()
    except Exception:
        sequence_ratio = 1.0 if exp_words == act_words else 0.0
    sequence_ok = sequence_ratio >= 0.995

    ok = words_ok and headings_ok and checkpoints_ok and timestamps_ok and sequence_ok

    if verbose:
        print(
            "🧪 VERIFY PROMPT SAU CTRL+V: "
            f"words={len(act_words)}/{len(exp_words)} ({word_ratio:.3f}) "
            f"| headings={'OK' if headings_ok else 'SAI THỨ TỰ/MẤT'} "
            f"| checkpoints={'OK' if checkpoints_ok else 'SAI THỨ TỰ/MẤT'} "
            f"| timestamps={'OK' if timestamps_ok else 'BỊ ĐẢO/MẤT'} "
            f"| sequence={'OK' if sequence_ok else f'SAI ({sequence_ratio:.3f})'}"
        )
        if not timestamps_ok:
            print(f"   Timestamp mong đợi: {expected_ts}")
            print(f"   Timestamp đọc lại : {actual_ts}")
        if not sequence_ok:
            print(f"   Độ giống thứ tự token: {sequence_ratio:.4f} (yêu cầu >= 0.9950)")

    return ok


def _wait_prompt_paste_stable(driver, composer, expected_text, timeout=PROMPT_NATIVE_PASTE_WAIT):
    """Đợi native paste render xong và text ổn định trước khi verify."""
    deadline = time.time() + timeout
    last_len = -1
    stable_since = None
    min_expected = max(100, int(len(_loose_compare_text(expected_text)) * 0.80))

    while time.time() < deadline:
        current = get_chatgpt_composer_text(driver, composer)
        current_len = len(_loose_compare_text(current))
        if current_len >= min_expected:
            if current_len == last_len:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= 0.8:
                    return True
            else:
                stable_since = None
        last_len = current_len
        sleep(0.15)
    return False


def insert_prompt_file_inline(driver, composer, prompt_path):
    """
    BƯỚC 3 - PROMPT SAFE INLINE.

    - Prompt ngắn: native Ctrl+V đúng 1 lần.
    - Prompt dài: KHÔNG paste clipboard vì ChatGPT có thể tự biến thành
      “pasted text attachment”. Thay vào đó dùng CDP Input.insertText MỘT LẦN,
      tuyệt đối không chia chunk.
    - Sau khi nhập luôn reacquire composer mới vì React/ProseMirror có thể thay DOM.
    - Chỉ đi tiếp khi verify cấu trúc/timestamp/token đều đạt.
    """
    prompt_path = Path(prompt_path).resolve()
    if not prompt_path.exists() or prompt_path.stat().st_size <= 0:
        print(f"❌ BƯỚC 3/5 - PROMPT: file không hợp lệ: {prompt_path}")
        return False, ""

    prompt_text = prompt_path.read_text(encoding="utf-8-sig", errors="strict")
    prompt_text = prompt_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(prompt_text) < 100:
        print("❌ BƯỚC 3/5 - PROMPT: prompt quá ngắn.")
        return False, ""

    print(f"📋 BƯỚC 3/5 - PROMPT: chuẩn bị {len(prompt_text):,} ký tự...")

    # Vẫn set + verify clipboard để đảm bảo file đọc đúng và để native paste dùng khi đủ ngắn.
    if not set_clipboard_text_windows(prompt_text):
        print("❌ Không set được Windows clipboard.")
        return False, prompt_text
    clipboard_now = read_clipboard_text_windows()
    if _loose_compare_text(clipboard_now) != _loose_compare_text(prompt_text):
        print("❌ Clipboard verify thất bại. KHÔNG nhập vào ChatGPT.")
        return False, prompt_text
    print("✅ Clipboard PROMPT = đúng nội dung file.")

    # Reacquire editor mới, không dùng element stale từ trước khi New Chat render xong.
    try:
        composer = wait_chatgpt_composer(driver, timeout=15)
    except Exception:
        print("❌ Không tìm thấy composer ChatGPT trước khi nhập prompt.")
        return False, prompt_text

    clear_chatgpt_composer(composer)
    sleep(0.35)
    composer = find_chatgpt_composer(driver) or composer
    residual = _loose_compare_text(get_chatgpt_composer_text(driver, composer))
    if residual:
        print(f"❌ Composer không sạch sau Ctrl+A/Delete ({len(residual)} chars). KHÔNG nhập.")
        return False, prompt_text
    print("🧹 Composer sạch trước khi nhập prompt.")

    before_cards = pasted_text_attachment_count(driver)
    mode = "NATIVE_CTRL_V"

    if len(prompt_text) > PROMPT_INLINE_NATIVE_MAX_CHARS:
        mode = "CDP_ONE_SHOT"
        print(
            f"⚠️ Prompt dài {len(prompt_text):,} ký tự > {PROMPT_INLINE_NATIVE_MAX_CHARS:,}. "
            "Không Ctrl+V để tránh ChatGPT biến prompt thành pasted-text attachment."
        )
        print("⌨️ Đang Input.insertText MỘT LẦN (không chunk, không JS loop)...")
        try:
            composer.click()
            driver.execute_cdp_cmd("Input.insertText", {"text": prompt_text})
        except Exception as exc:
            print(f"❌ CDP one-shot lỗi: {type(exc).__name__}: {exc}")
            return False, prompt_text
    else:
        print("⌨️ Ctrl+V PROMPT đúng 1 lần (native keyboard)...")
        try:
            ActionChains(driver)\
                .click(composer)\
                .key_down(Keys.CONTROL)\
                .send_keys("v")\
                .key_up(Keys.CONTROL)\
                .perform()
        except Exception as exc:
            print(f"❌ Native Ctrl+V lỗi: {type(exc).__name__}: {exc}")
            return False, prompt_text

    # React/ProseMirror có thể thay node sau input. Reacquire trước mọi verify.
    sleep(0.4)
    composer = find_chatgpt_composer(driver) or composer
    _wait_prompt_paste_stable(driver, composer, prompt_text)
    composer = find_chatgpt_composer(driver) or composer

    actual_now = get_chatgpt_composer_text(driver, composer)
    actual_loose = _loose_compare_text(actual_now)
    after_cards = pasted_text_attachment_count(driver)

    # Diagnostic rõ ràng cho đúng lỗi user vừa gặp: clipboard đúng nhưng composer=0.
    if not actual_loose:
        body_lower = (_chatgpt_body_text(driver) or "").lower()
        attachment_hints = (
            after_cards > before_cards
            or "show in text field" in body_lower
            or "show in text box" in body_lower
            or "pasted text" in body_lower
            or "hiển thị trong trường văn bản" in body_lower
            or "hiện trong trường văn bản" in body_lower
        )
        if attachment_hints:
            print(
                "❌ PROMPT đã bị ChatGPT biến thành PASTED-TEXT ATTACHMENT; "
                "composer inline đang rỗng. KHÔNG Send."
            )
        else:
            print(
                "❌ Composer đọc lại = 0 ký tự sau khi nhập. "
                "Có thể UI vừa thay node/focus; KHÔNG Send."
            )
        return False, prompt_text

    if after_cards > before_cards:
        print(
            f"❌ PROMPT bị ChatGPT biến thành attachment ({before_cards} → {after_cards}). "
            "KHÔNG đi tiếp."
        )
        return False, prompt_text

    if not verify_prompt_inline(driver, composer, prompt_text, verbose=True):
        print("❌ PROMPT bị thiếu/lộn cấu trúc. KHÔNG upload transcript, KHÔNG Send.")
        return False, prompt_text

    print(f"✅ BƯỚC 3/5 - PROMPT INLINE OK | mode={mode}")
    return True, prompt_text

def find_chatgpt_plus_button(driver):
    selectors = [
        "button[data-testid='composer-plus-btn']",
        "button#composer-plus-btn",
        "button[aria-label='Thêm tệp và nhiều nội dung khác']",
        "button[aria-label='Add files and more']",
        "button[aria-haspopup='menu'][data-testid*='plus']",
    ]
    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed() and el.is_enabled():
                    return el
        except Exception:
            continue
    return None


def open_chatgpt_plus_menu(driver, timeout=12):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        btn = find_chatgpt_plus_button(driver)
        if not btn:
            sleep(0.25)
            continue
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        except Exception:
            pass
        try:
            btn.click()
        except Exception as exc:
            last_error = exc
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception as exc2:
                last_error = exc2
                sleep(0.3)
                continue

        # HTML user cung cấp có aria-expanded=false -> sau click mong đợi true hoặc menu visible.
        end2 = time.time() + 4
        while time.time() < end2:
            try:
                expanded = (btn.get_attribute("aria-expanded") or "").lower() == "true"
            except Exception:
                expanded = False
            try:
                menus = [m for m in driver.find_elements(By.CSS_SELECTOR, "[role='menu']") if m.is_displayed()]
            except Exception:
                menus = []
            if expanded or menus:
                print("✅ Đã mở menu + của ChatGPT.")
                return True
            sleep(0.15)
        # Có UI phiên bản không set aria-expanded; nếu file input đã xuất hiện cũng coi là menu sẵn sàng.
        try:
            if driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                print("✅ Menu + đã kích hoạt file input.")
                return True
        except Exception:
            pass
        sleep(0.2)

    if last_error:
        print(f"⚠️ Không mở được menu +: {last_error}")
    else:
        print("⚠️ Không tìm thấy nút + của ChatGPT.")
    return False


def _upload_menu_text_visible(driver):
    """Chỉ để log/diagnostic; không phụ thuộc text này để upload."""
    try:
        body = _chatgpt_body_text(driver).lower()
    except Exception:
        return False
    needles = (
        "thêm hình & tệp",
        "tải lên từ máy tính",
        "add photos & files",
        "upload from computer",
        "upload files",
    )
    return any(n in body for n in needles)


def _candidate_file_inputs(driver):
    try:
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
    except Exception:
        return []

    scored = []
    for index, el in enumerate(inputs):
        try:
            disabled = el.get_attribute("disabled") is not None
            if disabled:
                continue
            accept = (el.get_attribute("accept") or "").lower()
            multiple = el.get_attribute("multiple") is not None
            # General file uploader thường accept rỗng hoặc nhiều loại file.
            score = 0
            if not accept:
                score += 5
            if "text" in accept or ".txt" in accept or ".md" in accept:
                score += 8
            if "image" in accept and all(x not in accept for x in ("text", ".txt", ".md", "pdf")):
                score -= 5
            if multiple:
                score += 1
            scored.append((score, index, el, accept))
        except Exception:
            continue
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored


def attachment_filename_visible(driver, filename):
    """Verify tên file ở UI ngoài composer; prompt inline không chứa filename nên tránh false-positive."""
    filename = str(filename)
    # Tín hiệu mạnh: visible element có text đúng bằng filename.
    try:
        xpath = "//*[normalize-space(text())=" + json.dumps(filename) + "]"
        for el in driver.find_elements(By.XPATH, xpath):
            try:
                if not el.is_displayed():
                    continue
                # Không tính text nếu nó nằm trong composer.
                inside_composer = driver.execute_script(
                    'return !!arguments[0].closest("#prompt-textarea, textarea[data-testid=\"prompt-textarea\"], [contenteditable=\"true\"]");',
                    el,
                )
                if not inside_composer:
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Fallback: body có filename nhưng composer không có filename.
    try:
        body = _chatgpt_body_text(driver)
        composer = find_chatgpt_composer(driver)
        composer_text = get_chatgpt_composer_text(driver, composer) if composer else ""
        return filename in body and filename not in composer_text
    except Exception:
        return False


def _send_file_to_any_input(driver, file_path):
    file_path = str(Path(file_path).resolve())
    candidates = _candidate_file_inputs(driver)
    if not candidates:
        return False, "NO_FILE_INPUT"

    errors = []
    for score, index, el, accept in candidates:
        try:
            # File input có thể hidden; send_keys vẫn thường hoạt động. Nếu Chrome/Selenium chặn hidden,
            # tạm làm nó interactable rồi gửi path.
            try:
                driver.execute_script(
                    "arguments[0].removeAttribute('hidden');"
                    "arguments[0].style.display='block';"
                    "arguments[0].style.visibility='visible';"
                    "arguments[0].style.opacity='1';"
                    "arguments[0].style.position='fixed';"
                    "arguments[0].style.left='0';"
                    "arguments[0].style.bottom='0';",
                    el,
                )
            except Exception:
                pass
            el.send_keys(file_path)
            return True, f"input#{index} accept={accept or '*'} score={score}"
        except Exception as exc:
            errors.append(f"#{index}:{type(exc).__name__}")
            continue
    return False, ",".join(errors) or "SEND_KEYS_FAILED"


def wait_real_file_attachment(driver, filename, timeout=CHATGPT_UPLOAD_WAIT):
    deadline = time.time() + timeout
    stable_since = None
    while time.time() < deadline:
        if chatgpt_has_upload_error(driver):
            return False, "UPLOAD_ERROR_TOAST"
        visible = attachment_filename_visible(driver, filename)
        if visible:
            stable_since = stable_since or time.time()
            # Đợi thêm để bắt lỗi upload xuất hiện chậm.
            if time.time() - stable_since >= 1.4:
                if chatgpt_has_upload_error(driver):
                    return False, "UPLOAD_ERROR_TOAST"
                return True, "FILENAME_VISIBLE_STABLE"
        else:
            stable_since = None
        sleep(0.25)
    return False, "UPLOAD_VERIFY_TIMEOUT"


def upload_transcript_via_plus(driver, transcript_path, prompt_text):
    """Upload thật: click + -> tìm input[type=file] -> send_keys -> verify filename + no error toast."""
    transcript_path = Path(transcript_path).resolve()
    print(f"📎 BƯỚC 4/5 - TRANSCRIPT UPLOAD: {transcript_path.name}")

    if not open_chatgpt_plus_menu(driver):
        return False, "PLUS_MENU_FAILED"
    if _upload_menu_text_visible(driver):
        print("✅ Đã thấy mục 'Thêm hình & tệp / Upload from computer'.")

    ok, detail = _send_file_to_any_input(driver, transcript_path)
    if not ok:
        print(f"⚠️ Không gửi được file vào input[type=file]: {detail}")
        return False, detail
    print(f"📤 Đã đưa file vào uploader ({detail}). Đang chờ ChatGPT nhận file...")

    ok, reason = wait_real_file_attachment(driver, transcript_path.name)
    if not ok:
        print(f"⚠️ Upload file chưa được xác nhận: {reason}")
        return False, reason

    # Prompt inline phải còn nguyên sau upload.
    composer = find_chatgpt_composer(driver)
    if not composer or not verify_prompt_inline(driver, composer, prompt_text, verbose=False):
        print("❌ Upload có vẻ xong nhưng PROMPT inline bị mất/đổi; KHÔNG Send.")
        return False, "PROMPT_LOST_AFTER_UPLOAD"

    print(f"✅ BƯỚC 4/5 - TRANSCRIPT FILE ATTACHMENT OK: {transcript_path.name}")
    return True, "REAL_FILE_UPLOAD"


def paste_transcript_as_attachment(driver, transcript_path, prompt_text, timeout=CHATGPT_PASTE_FALLBACK_WAIT):
    """
    Fallback giống thao tác user: Ctrl+V transcript dài để ChatGPT tự chuyển thành pasted-text attachment.
    Quan trọng: KHÔNG xóa prompt inline và chỉ thành công khi card count tăng + prompt vẫn còn nguyên.
    """
    transcript_path = Path(transcript_path).resolve()
    transcript_text = transcript_path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not transcript_text:
        return False, "EMPTY_TRANSCRIPT"

    composer = find_chatgpt_composer(driver)
    if not composer:
        return False, "NO_COMPOSER"
    if not verify_prompt_inline(driver, composer, prompt_text, verbose=False):
        return False, "PROMPT_NOT_READY"

    before_cards = pasted_text_attachment_count(driver)
    before_text = normalize_compare_text(get_chatgpt_composer_text(driver, composer))
    if not set_clipboard_text_windows(transcript_text):
        return False, "SET_CLIPBOARD_FAILED"

    print(
        f"📋 Fallback TRANSCRIPT PASTE: Ctrl+V {len(transcript_text):,} ký tự; "
        "chờ ChatGPT tự tạo pasted-text attachment..."
    )
    try:
        composer.click()
        composer.send_keys(Keys.CONTROL, "v")
    except Exception as exc:
        return False, f"PASTE_FAILED:{type(exc).__name__}"

    deadline = time.time() + timeout
    stable_since = None
    while time.time() < deadline:
        cards = pasted_text_attachment_count(driver)
        current = normalize_compare_text(get_chatgpt_composer_text(driver, composer))
        prompt_ok = verify_prompt_inline(driver, composer, prompt_text, verbose=False)

        if cards > before_cards and prompt_ok:
            stable_since = stable_since or time.time()
            if time.time() - stable_since >= 0.8:
                print(f"✅ TRANSCRIPT pasted-text attachment OK | cards {before_cards} → {cards}")
                return True, "PASTED_TEXT_ATTACHMENT"
        else:
            stable_since = None

        # Nếu transcript bị dán inline thay vì attachment, dừng sớm để không gửi nhầm.
        if len(current) > len(before_text) + max(800, int(len(normalize_compare_text(transcript_text)) * 0.55)):
            print("⚠️ Transcript đang nằm INLINE thay vì attachment; sẽ không Send.")
            return False, "TRANSCRIPT_STAYED_INLINE"
        sleep(0.25)

    return False, "PASTE_ATTACHMENT_TIMEOUT"


def _runtime_markdown_copy(transcript_path):
    """Một số uploader/browser có thể xử lý .md ổn hơn .txt. Tạo fallback tạm trong runtime."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(transcript_path).resolve()
    dst = RUNTIME_DIR / (src.stem + ".md")
    dst.write_text(src.read_text(encoding="utf-8-sig", errors="replace"), encoding="utf-8")
    return dst


def ask_chatgpt(driver, prompt_path, transcript_path, chatgpt_project=None):
    """
    FINAL SAFE ChatGPT pipeline - KHÔNG còn cơ chế có thể làm prompt bị đảo text.

    Mỗi attempt bắt đầu bằng NEW CHAT sạch:
      B3 PROMPT: prompt ngắn Ctrl+V; prompt dài CDP Input.insertText 1 lần -> verify cấu trúc cực chặt.
      B4 TRANSCRIPT: chỉ upload FILE thật qua uploader ChatGPT.
          attempt 1: .txt
          attempt 2: .md tương đương nếu .txt bị UI từ chối.
      B5 Chỉ khi prompt + attachment đều verify OK và không có upload-error mới Send.

    TUYỆT ĐỐI KHÔNG:
      - chia prompt thành chunk / chèn lặp nhiều lần;
      - chia prompt thành chunk;
      - Ctrl+V transcript vào composer;
      - retry bằng cách chèn tiếp vào composer cũ.
    """
    prompt_path = Path(prompt_path).resolve()
    transcript_path = Path(transcript_path).resolve()
    md_fallback = None

    methods = ["TXT_UPLOAD", "MD_UPLOAD"][:max(1, CHATGPT_SMART_CHAT_ATTEMPTS)]

    for attempt_index, method in enumerate(methods, start=1):
        print("\n" + "=" * 72)
        print(f"CHATGPT SAFE - ATTEMPT {attempt_index}/{len(methods)} - {method}")
        print("NEW CHAT -> PROMPT SAFE INLINE -> TRANSCRIPT FILE -> SEND")
        print("=" * 72)

        # NEW CHAT thật BÊN TRONG PROJECT cho mỗi attempt/video.
        # Không bao giờ driver.get(CHATGPT_HOME) ở luồng active để tránh tạo chat ngoài.
        try:
            composer = open_chatgpt_project_new_chat(
                driver, chatgpt_project, timeout=WAIT_CHATGPT_READY
            )
        except Exception as exc:
            print(f"❌ Không mở được New Chat trong đúng Project: {exc}")
            continue

        previous_count = len(get_assistant_turns(driver))

        # B3: NATIVE CTRL+V đúng một lần, verify full structure.
        prompt_ok, prompt_text = insert_prompt_file_inline(driver, composer, prompt_path)
        if not prompt_ok:
            print("⛔ DỪNG ATTEMPT: PROMPT chưa chính xác. Mở new chat ở attempt kế tiếp.")
            continue

        # B4: transcript FILE thật. Không dùng paste transcript.
        attach_ok = False
        attach_reason = ""
        upload_path = transcript_path

        if method == "MD_UPLOAD":
            try:
                md_fallback = md_fallback or _runtime_markdown_copy(transcript_path)
                upload_path = md_fallback
            except Exception as exc:
                print(f"❌ Không tạo được MD fallback: {exc}")
                continue

        attach_ok, attach_reason = upload_transcript_via_plus(
            driver, upload_path, prompt_text
        )
        if not attach_ok:
            print(
                f"❌ BƯỚC 4/5 - TRANSCRIPT FILE chưa thành công ({attach_reason}). "
                "KHÔNG Send."
            )
            continue

        # CHỐT PROMPT LẦN CUỐI sau upload để bắt mọi re-render làm text hỏng.
        composer = find_chatgpt_composer(driver)
        if not composer:
            print("⛔ Mất composer sau upload. KHÔNG Send.")
            continue

        if not verify_prompt_inline(driver, composer, prompt_text, verbose=True):
            print("⛔ PROMPT bị thay đổi/lộn sau upload. KHÔNG Send.")
            continue

        transcript_ready = attachment_filename_visible(driver, upload_path.name)
        print(
            "🔒 CHỐT TRƯỚC SEND: "
            f"PROMPT=CẤU TRÚC OK | TRANSCRIPT={'OK' if transcript_ready else 'MẤT'} "
            f"| file={upload_path.name}"
        )
        if not transcript_ready:
            print("⛔ Không xác nhận được transcript attachment. KHÔNG Send.")
            continue

        if chatgpt_has_upload_error(driver):
            print("⛔ ChatGPT đang báo Unable/Failed to upload. KHÔNG Send.")
            continue

        # Không thêm bất kỳ text nào sau prompt, vì prompt đã tự nói transcript đính kèm riêng.
        # Dọn popup Too many requests nếu nó đang che composer/nút Send; đồng thời
        # passive-detect HTTP 429 từ /backend-api/conversations.
        handle_chatgpt_conversations_api_429(driver, quiet=False)
        print("📤 BƯỚC 5/5 - bấm Send...")
        if not click_send(driver):
            print("❌ Không gửi được prompt + transcript file.")
            continue

        # Popup/API 429 có thể bật ngay sau request; đóng popup nhưng vẫn tiếp tục
        # chờ/lấy answer hiện tại. Không tự retry chỉ vì sidebar conversations bị 429.
        handle_chatgpt_conversations_api_429(driver, quiet=False)
        print("⌛ Đã Send. Đang đợi ChatGPT phân tích transcript...")
        last_turn, dom_text = wait_for_chatgpt_answer(driver, previous_count)
        # Đóng lần cuối trước khi click Copy vì dialog overlay có thể chặn nút.
        handle_chatgpt_conversations_api_429(driver, quiet=False)
        copied = click_copy_last_answer(driver, last_turn)
        answer = (copied or dom_text or "").strip()
        if answer:
            print("✅ Đã nhận kết quả ChatGPT.")
            try:
                if md_fallback and md_fallback.exists():
                    md_fallback.unlink(missing_ok=True)
            except Exception:
                pass
            return answer

        print("⚠️ ChatGPT không trả text hợp lệ; attempt này thất bại.")

    try:
        if md_fallback and md_fallback.exists():
            md_fallback.unlink(missing_ok=True)
    except Exception:
        pass

    print("❌ ChatGPT pipeline thất bại an toàn. Link được giữ để retry sau.")
    return None


# ============================================================
# PARSE CHATGPT CUT RANGES
# ============================================================

def clean_ai_answer(answer):
    answer = (answer or "").replace("```text", "").replace("```", "").strip()

    # Nếu UI copy kèm text action lạ, chỉ giữ tới DONE đầu tiên.
    match = re.search(r"(?im)^DONE\s*$", answer)
    if match:
        answer = answer[:match.end()]

    return answer.strip()



def transcript_timestamp_set(transcript):
    allowed = set()
    for line in (transcript or "").splitlines():
        m = re.match(r"^\s*(\d{2,4}:\d{2}:\d{2})\s+", line)
        if m:
            allowed.add(m.group(1))
    return allowed


def parse_ai_cut_result(answer, transcript=None):
    """
    STRICT:
    - câu trả lời phải kết thúc bằng DONE;
    - NONE chỉ được đứng một mình trước DONE;
    - mọi dòng khác phải chính xác HH:MM:SS --> HH:MM:SS hoặc END;
    - nếu bật STRICT_AI_TIMESTAMP_VALIDATION, mọi timestamp phải có thật trong transcript.
    """
    answer = clean_ai_answer(answer)
    if not answer:
        return None, "AI trả về rỗng"

    raw_lines = [line.strip() for line in answer.splitlines() if line.strip()]
    if not raw_lines or raw_lines[-1].upper() != "DONE":
        return None, "Thiếu DONE ở cuối"

    body = raw_lines[:-1]
    if body == ["NONE"]:
        return {
            "answer": answer,
            "cut_ranges": [],
            "keep_all": True,
            "raw_ranges": [],
        }, None

    if not body:
        return None, "Không có NONE hoặc khoảng cắt"

    pattern = re.compile(
        r"^(\d{2,4}:\d{2}:\d{2})\s*-->\s*(\d{2,4}:\d{2}:\d{2}|END)$",
        re.IGNORECASE,
    )
    allowed = transcript_timestamp_set(transcript) if transcript else set()

    cut_ranges = []
    raw_ranges = []

    for line in body:
        m = pattern.fullmatch(line)
        if not m:
            return None, f"Dòng sai format: {line}"

        start_text = m.group(1)
        end_text = m.group(2).upper()

        start_sec = cutter.timestamp_to_seconds(start_text)
        if start_sec is None:
            return None, f"Timestamp bắt đầu không hợp lệ: {start_text}"

        if STRICT_AI_TIMESTAMP_VALIDATION and allowed and start_text not in allowed:
            return None, f"AI dùng timestamp không tồn tại trong transcript: {start_text}"

        if end_text == "END":
            end_sec = float("inf")
        else:
            end_sec = cutter.timestamp_to_seconds(end_text)
            if end_sec is None:
                return None, f"Timestamp kết thúc không hợp lệ: {end_text}"
            if STRICT_AI_TIMESTAMP_VALIDATION and allowed and end_text not in allowed:
                return None, f"AI dùng timestamp không tồn tại trong transcript: {end_text}"
            if end_sec <= start_sec:
                return None, f"Khoảng cắt ngược/rỗng: {line}"

        cut_ranges.append((float(start_sec), float(end_sec)))
        raw_ranges.append((start_text, end_text))

    return {
        "answer": answer,
        "cut_ranges": cut_ranges,
        "keep_all": False,
        "raw_ranges": raw_ranges,
    }, None


def repair_ai_answer(driver, reason):
    """Yêu cầu ChatGPT sửa CHỈ output, giữ nguyên prompt + transcript file đã có trong conversation."""
    previous_count = len(get_assistant_turns(driver))
    composer = wait_chatgpt_composer(driver, timeout=30)
    message = (
        "Kết quả vừa rồi KHÔNG hợp lệ. "
        f"Lỗi: {reason}. "
        "Hãy đọc lại PROMPT trong tin nhắn trước và TRANSCRIPT file đã đính kèm trong conversation, rồi trả lời LẠI. "
        "Chỉ được dùng timestamp có thật trong transcript. "
        "Chỉ xuất các dòng HH:MM:SS --> HH:MM:SS hoặc HH:MM:SS --> END, "
        "hoặc NONE, rồi dòng cuối DONE. Không giải thích."
    )
    if not set_short_instruction(driver, composer, message):
        return None
    if not click_send(driver):
        return None
    last_turn, dom_text = wait_for_chatgpt_answer(driver, previous_count)
    copied = click_copy_last_answer(driver, last_turn)
    return (copied or dom_text or "").strip()


def read_urls_from_console_or_list():
    print("\n" + "=" * 70)
    print("NGUỒN LINK")
    print("=" * 70)
    raw = input(
        "Dán 1 link YouTube rồi ENTER, hoặc chỉ ENTER để dùng list.txt: "
    ).strip()

    done = load_global_done_set()

    if raw:
        url = normalize_youtube_url(raw)
        if not url:
            return []
        if url in done:
            print("⏭️ Link này đã có trong doneLink.txt.")
            return []
        return [url]

    list_file = BASE_DIR / "list.txt"
    if not list_file.exists():
        list_file.write_text("# Mỗi dòng một link YouTube\n", encoding="utf-8")
        print("📝 Đã tạo list.txt nhưng chưa có link.")
        return []

    urls = []
    seen = set()
    invalid_count = 0

    # Streaming từng dòng; list.txt không bị sửa trong quá trình chạy.
    with open(list_file, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = normalize_youtube_url(line)
            if not url or not video_id_from_url(url):
                invalid_count += 1
                continue
            if url in done or url in seen:
                continue
            seen.add(url)
            urls.append(url)

    print(f"📚 Link pending: {len(urls):,}")
    print(f"✅ Link done đã bỏ qua: {len(done):,}")
    if invalid_count:
        print(f"⚠️ Dòng/link không hợp lệ bỏ qua: {invalid_count:,}")

    return urls

def save_debug_files(video_url, title, transcript, answer=None, transcript_dir=None, ai_result_dir=None):
    vid = video_id_from_url(video_url) or safe_name(title)
    stem = safe_name(f"{vid}_{title}")
    transcript_dir = Path(transcript_dir) if transcript_dir else TRANSCRIPT_DIR
    ai_result_dir = Path(ai_result_dir) if ai_result_dir else AI_RESULT_DIR
    transcript_dir.mkdir(parents=True, exist_ok=True)
    ai_result_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / f"{stem}.txt"
    transcript_path.write_text(transcript, encoding="utf-8")

    answer_path = None
    if answer is not None:
        answer_path = ai_result_dir / f"{stem}.txt"
        answer_path.write_text(answer, encoding="utf-8")

    return transcript_path, answer_path


# ============================================================
# ONE VIDEO
# ============================================================



# ============================================================
# ROBUST YT-DLP AUDIO DOWNLOAD - NO HANG / SABR FALLBACK
# ============================================================

DOWNLOAD_SOCKET_TIMEOUT = 12
DOWNLOAD_ATTEMPT_TIMEOUT = 120  # timeout cứng cho MỖI lượt, tránh đứng vô hạn


def _kill_process_tree(proc):
    """Dừng yt-dlp và toàn bộ process con nếu một lượt bị treo."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _find_downloaded_audio():
    """
    Ưu tiên helper của story_cutter_core để giữ tương thích.
    Nếu helper không có/không thấy file thì tự quét DOWNLOAD_DIR.
    """
    try:
        p = cutter.find_raw_video()
        if p:
            return Path(p)
    except Exception:
        pass

    download_dir = Path(cutter.DOWNLOAD_DIR)
    candidates = []
    for ext in (".mp3", ".m4a", ".webm", ".opus", ".ogg", ".aac", ".wav", ".mp4"):
        candidates.extend(download_dir.glob(f"raw_audio*{ext}"))
        candidates.extend(download_dir.glob(f"raw_video*{ext}"))

    candidates = [p for p in candidates if p.is_file() and p.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _delete_old_download_audio():
    try:
        cutter.delete_old_raw_files()
        return
    except Exception:
        pass

    download_dir = Path(cutter.DOWNLOAD_DIR)
    for pat in ("raw_audio*", "raw_video*"):
        for p in download_dir.glob(pat):
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                pass


def _run_ytdlp_audio_attempt(
    video_url,
    js_arguments,
    *,
    cookie_file=None,
    user_agent=None,
    extractor_args=None,
    force_ipv4=True,
    format_selector="bestaudio",
    label="yt-dlp",
):
    """
    Chạy 1 lượt yt-dlp có:
    - socket timeout ngắn để không kẹt ở Downloading webpage
    - timeout cứng toàn lượt
    - chỉ tải AUDIO; không fallback sang video/combined format
    """
    _delete_old_download_audio()

    download_dir = Path(cutter.DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(download_dir / "raw_audio.%(ext)s")

    cmd = [
        *cutter.YTDLP_CMD,
        "--ignore-config",
        "--no-plugin-dirs",
        *js_arguments,
        "--remote-components", "ejs:github",
        "--no-playlist",
        "--geo-bypass",
        "--windows-filenames",
        "--force-overwrites",
        "--no-part",
        "--socket-timeout", str(DOWNLOAD_SOCKET_TIMEOUT),
        "--extractor-retries", "2",
        "--retries", "2",
        "--fragment-retries", "2",
        "--retry-sleep", "1",
        "--concurrent-fragments", "3",
    ]

    if force_ipv4:
        cmd.append("--force-ipv4")

    if cookie_file:
        cp = Path(cookie_file)
        if cp.exists() and cp.stat().st_size > 0:
            cmd.extend(["--cookies", str(cp)])

    if user_agent:
        cmd.extend(["--user-agent", str(user_agent)])

    if extractor_args:
        cmd.extend(["--extractor-args", str(extractor_args)])

    cmd.extend([
        "-f", format_selector,
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "128K",
        "-o", output_template,
        video_url,
    ])

    print(f"\n⬇️ {label}")
    if cookie_file:
        print(f"   🍪 Cookie LIVE: {cookie_file}")
    else:
        print("   🍪 Cookie: KHÔNG DÙNG")

    if extractor_args:
        print(f"   🧩 Extractor args: {extractor_args}")

    print(f"   🎵 AUDIO ONLY: {format_selector} -> MP3 | KHÔNG tải video MP4")
    print(f"   🌐 IPv4 bắt buộc: {'CÓ' if force_ipv4 else 'KHÔNG'}")
    print(
        f"   ⏱️ Socket timeout={DOWNLOAD_SOCKET_TIMEOUT}s | "
        f"timeout cứng/lượt={DOWNLOAD_ATTEMPT_TIMEOUT}s"
    )

    proc = None
    try:
        # Không capture stdout/stderr: log yt-dlp vẫn hiện realtime trong console.
        proc = subprocess.Popen(cmd, cwd=str(BASE_DIR))
        return_code = proc.wait(timeout=DOWNLOAD_ATTEMPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(
            f"   ⏱️ Lượt này quá {DOWNLOAD_ATTEMPT_TIMEOUT}s -> "
            "TỰ DỪNG yt-dlp và chuyển fallback, không đứng vô hạn."
        )
        _kill_process_tree(proc)
        _delete_old_download_audio()
        return None
    except KeyboardInterrupt:
        _kill_process_tree(proc)
        raise
    except Exception as exc:
        print(f"   ⚠️ Không chạy được yt-dlp: {type(exc).__name__}: {exc}")
        _kill_process_tree(proc)
        _delete_old_download_audio()
        return None

    if return_code != 0:
        _delete_old_download_audio()
        return None

    raw = _find_downloaded_audio()
    if not raw:
        return None

    try:
        duration = cutter.get_video_duration(raw)
    except Exception:
        duration = None

    if not duration or duration <= 0:
        print("   ⚠️ File tải xong nhưng ffprobe không đọc được duration.")
        _delete_old_download_audio()
        return None

    return raw


def robust_download_audio(
    video_url,
    js_arguments,
    original_title,
    cookie_file=None,
    user_agent=None,
):
    """
    Downloader mới:
    - Ưu tiên audio-only.
    - Chỉ tải audio-only. Nếu YouTube không cấp audio-only URL thì lượt đó fail và chuyển client khác.
    - Không để một lượt treo vô hạn.
    - Cookie LIVE và public đều được thử.
    """
    try:
        if cutter.can_reuse_existing_video(video_url):
            raw = _find_downloaded_audio()
            if raw:
                duration = cutter.get_video_duration(raw)
                if duration and duration > 0:
                    print("\n✅ Audio thô đúng link đã tồn tại; dùng lại.")
                    print(f"📁 {raw}")
                    return raw
    except Exception:
        pass

    live_cookie = None
    if cookie_file:
        cp = Path(cookie_file)
        if cp.exists() and cp.stat().st_size > 0:
            live_cookie = cp

    attempts = []

    if live_cookie:
        attempts.extend([
            # Audio-only trước.
            dict(
                cookie_file=live_cookie,
                user_agent=user_agent,
                extractor_args=None,
                force_ipv4=True,
                format_selector="bestaudio[ext=m4a]/bestaudio",
                label="COOKIE LIVE + default + audio/best fallback",
            ),
            # Web variants.
            dict(
                cookie_file=live_cookie,
                user_agent=user_agent,
                extractor_args="youtube:player_client=web,web_embedded",
                force_ipv4=True,
                format_selector="bestaudio",
                label="COOKIE LIVE + web,web_embedded",
            ),
            # Khác client để tránh session web đang dính SABR-only.
            dict(
                cookie_file=live_cookie,
                user_agent=user_agent,
                extractor_args="youtube:player_client=android,web_safari",
                force_ipv4=True,
                format_selector="bestaudio",
                label="COOKIE LIVE + android,web_safari",
            ),
        ])

    # Public fallback đôi khi lấy format bình thường hơn session có cookie.
    attempts.extend([
        dict(
            cookie_file=None,
            user_agent=user_agent,
            extractor_args=None,
            force_ipv4=True,
            format_selector="bestaudio",
            label="PUBLIC + default",
        ),
        dict(
            cookie_file=None,
            user_agent=user_agent,
            extractor_args="youtube:player_client=android,web_safari",
            force_ipv4=False,
            format_selector="bestaudio",
            label="PUBLIC + android,web_safari + no IPv4 force",
        ),
    ])

    total = len(attempts)
    for idx, params in enumerate(attempts, 1):
        params["label"] = f"LƯỢT {idx}/{total} - {params['label']}"
        raw = _run_ytdlp_audio_attempt(
            video_url,
            js_arguments,
            **params,
        )
        if raw:
            # Lưu marker để lần sau biết raw thuộc URL nào.
            try:
                if hasattr(cutter, "SOURCE_URL_FILE"):
                    Path(cutter.SOURCE_URL_FILE).write_text(video_url, encoding="utf-8")
                if hasattr(cutter, "SOURCE_TITLE_FILE"):
                    Path(cutter.SOURCE_TITLE_FILE).write_text(original_title, encoding="utf-8")
            except Exception:
                pass

            print("\n✅ Tải audio thành công:")
            print(f"📁 {raw}")
            return raw

        print(f"⚠️ Lượt tải {idx}/{total} thất bại -> chuyển phương án kế tiếp.")

    print("\n❌ Không tải được audio sau tất cả fallback.")
    print("   Nếu log vẫn báo SABR-only/Only images, đây là giới hạn format của phiên YouTube hiện tại.")
    return None


def process_one_video(
    youtube_driver,
    chatgpt_driver,
    video_url,
    prompt_template,
    js_arguments,
    index,
    total,
    chatgpt_project=None,
):
    video_id = video_id_from_url(video_url)
    title = "video"
    workspace = None
    stage = "start"

    print("\n\n" + "#" * 72)
    print(f"VIDEO {index:,}/{total:,}")
    print(video_url)
    print("#" * 72)

    try:
        # ------------------------------------------------------------
        # STEP 1: TRANSCRIPT. get_transcript tự mở đúng video.
        # ------------------------------------------------------------
        stage = "transcript"
        transcript = get_transcript(youtube_driver, video_url)
        if not transcript:
            append_failure(video_url, stage, "Không có/lấy không được transcript")
            return False

        # Không export cookie ngay ở đây nữa.
        # Browser thường đã có title/channel/channel_id, nên ghi cookie 2 lần/video là phí.
        cookie_path = None
        browser_user_agent = None

        # ------------------------------------------------------------
        # STEP 2: METADATA + CHANNEL FOLDER.
        # ------------------------------------------------------------
        stage = "metadata"
        print("\n⚡ BƯỚC METADATA: ưu tiên đọc trực tiếp từ tab YouTube; không export cookie nếu chưa cần...")
        metadata = fetch_video_metadata(
            video_url,
            js_arguments,
            youtube_driver=youtube_driver,
            cookie_file=None,
        )

        # Chỉ khi metadata thật sự thiếu channel/channel_id mới export cookie sớm để retry.
        channel_id_probe = str(metadata.get("channel_id") or "")
        channel_probe = str(metadata.get("channel") or metadata.get("uploader") or "")
        if (
            not channel_probe
            or not channel_id_probe
            or channel_id_probe.startswith("UNKNOWN_")
            or channel_probe == "UNKNOWN_CHANNEL"
        ):
            print("   🍪 Metadata thiếu channel -> mới export cookie LIVE để retry metadata...")
            stage = "cookies_for_metadata_retry"
            cookie_session = export_fresh_youtube_cookies(
                youtube_driver,
                video_url=video_url,
                cookie_file=LIVE_COOKIE_FILE,
            )
            if cookie_session:
                cookie_path = cookie_session["path"]
                browser_user_agent = cookie_session.get("user_agent")
                metadata = fetch_video_metadata(
                    video_url,
                    js_arguments,
                    youtube_driver=youtube_driver,
                    cookie_file=cookie_path,
                )

        title = metadata["title"]
        workspace = resolve_channel_workspace(metadata)

        print(f"🏷️ {title}")
        print(f"📺 Kênh: {workspace['channel_title']}")
        print(f"🆔 Channel ID: {workspace['channel_id']}")
        print(f"📁 Folder: {workspace['folder_name']}")
        if chatgpt_project:
            print(f"👤 ChatGPT Project: {chatgpt_project.get('name') or chatgpt_project.get('key')}")

        # Nếu file cuối đã tồn tại nhưng doneLink chưa ghi (crash sau move) => tự phục hồi.
        existing_final = find_existing_final(workspace, video_id)
        if existing_final:
            print(f"✅ Đã thấy MP3 hoàn chỉnh từ lần trước: {existing_final.name}")
            append_global_done_link(video_url)
            append_channel_done_link(workspace, video_url)
            return True

        # ------------------------------------------------------------
        # STEP 3: 2 FILE AI RIÊNG.
        # ------------------------------------------------------------
        stage = "prepare_ai_files"
        prompt_path, transcript_path = prepare_separate_ai_files(
            video_url,
            prompt_template,
            transcript,
            ai_input_dir=workspace["ai_inputs"],
            transcript_dir=workspace["transcripts"],
        )

        canonical_result = workspace["ai_results"] / f"{video_id}_CUTS.txt"
        answer = None
        parsed = None

        # ------------------------------------------------------------
        # STEP 4: REUSE AI result nếu lần trước AI xong nhưng download/cut fail.
        # ------------------------------------------------------------
        if REUSE_VALID_AI_RESULT and canonical_result.exists():
            try:
                cached = canonical_result.read_text(encoding="utf-8-sig")
                parsed, reason = parse_ai_cut_result(cached, transcript=transcript)
                if parsed:
                    answer = cached
                    print(f"♻️ Reuse AI result hợp lệ: {canonical_result.name}")
                else:
                    print(f"⚠️ AI cache cũ không hợp lệ ({reason}), sẽ hỏi ChatGPT lại.")
            except Exception:
                parsed = None

        # ------------------------------------------------------------
        # STEP 5: CHATGPT + STRICT VALIDATION + REPAIR.
        # ------------------------------------------------------------
        if parsed is None:
            stage = "chatgpt"
            answer = ask_chatgpt(chatgpt_driver, prompt_path, transcript_path, chatgpt_project=chatgpt_project)
            if not answer:
                append_failure(
                    video_url, stage, "Không nhận được câu trả lời ChatGPT",
                    workspace["channel_id"], title
                )
                return False

            parsed, reason = parse_ai_cut_result(answer, transcript=transcript)

            repair_count = 0
            while parsed is None and repair_count < AI_REPAIR_ATTEMPTS:
                repair_count += 1
                print(f"⚠️ AI output chưa hợp lệ: {reason}")
                print(f"🔧 Yêu cầu ChatGPT sửa output lần {repair_count}/{AI_REPAIR_ATTEMPTS}...")
                repaired = repair_ai_answer(chatgpt_driver, reason)
                if not repaired:
                    break
                answer = repaired
                parsed, reason = parse_ai_cut_result(answer, transcript=transcript)

            if parsed is None:
                bad_path = workspace["ai_results"] / f"{video_id}_INVALID.txt"
                bad_path.write_text(answer or "", encoding="utf-8")
                append_failure(
                    video_url, "ai_validate", reason or "AI output invalid",
                    workspace["channel_id"], title
                )
                print("❌ AI vẫn không hợp lệ sau repair; KHÔNG download/cắt.")
                return False

            canonical_result.write_text(clean_ai_answer(answer) + "\n", encoding="utf-8")
            print(f"💾 AI result chuẩn: {canonical_result}")

        print("\n📋 KẾT QUẢ AI ĐÃ VALIDATE:")
        print("-" * 50)
        print(clean_ai_answer(answer))
        print("-" * 50)

        # ------------------------------------------------------------
        # STEP 5.5: AI yêu cầu cắt TOÀN BỘ => phân loại NO_STORY, KHÔNG download.
        # ------------------------------------------------------------
        if ai_requests_full_cut(parsed):
            print("🚫 AI trả 00:00:00 --> END: toàn bộ audio bị loại theo kết quả AI.")
            print("⏭️ Không tải MP3. Đã ghi no_story.txt và chuyển link tiếp theo.")
            append_no_story(video_url, workspace=workspace, title=title)
            append_global_done_link(video_url)
            append_channel_done_link(workspace, video_url)
            write_video_log(workspace, video_id, "FULL_CUT -> AI requested full cut; download skipped")
            return True

        # ------------------------------------------------------------
        # STEP 6: DISK + DOWNLOAD.
        # ------------------------------------------------------------
        stage = "disk"
        if not check_free_space(cutter.DOWNLOAD_DIR):
            append_failure(video_url, stage, "Không đủ dung lượng trống", workspace["channel_id"], title)
            return False

        # Refresh cookie LẦN NỮA ngay sát download. Cookie lấy trước AI chỉ dùng hỗ trợ metadata.
        stage = "refresh_cookies_before_download"
        fresh_download_session = export_fresh_youtube_cookies(
            youtube_driver,
            video_url=video_url,
            cookie_file=LIVE_COOKIE_FILE,
        )
        if fresh_download_session:
            cookie_path = fresh_download_session["path"]
            browser_user_agent = fresh_download_session.get("user_agent") or browser_user_agent
        else:
            print("⚠️ Không refresh được cookie ngay trước download; yt-dlp vẫn có public fallback.")
            cookie_path = None

        stage = "download_audio_mp3"
        raw_video = robust_download_audio(
            video_url,
            js_arguments,
            title,
            cookie_file=cookie_path,
            user_agent=browser_user_agent,
        )
        if not raw_video:
            append_failure(
                video_url, stage, "yt-dlp không tải được video sau tất cả fallback",
                workspace["channel_id"], title
            )
            return False

        # ------------------------------------------------------------
        # STEP 7: CUT/MERGE.
        # ------------------------------------------------------------
        stage = "cut_merge"
        final_path = cutter.process_video(
            raw_video,
            title,
            parsed["cut_ranges"],
            keep_all=parsed["keep_all"],
            output_dir=workspace["done"],
            video_id=video_id,
        )
        if not final_path:
            append_failure(video_url, stage, "FFmpeg cắt/ghép thất bại", workspace["channel_id"], title)
            return False

        # Sanity: final phải đọc được duration và > 0.
        final_duration = cutter.get_video_duration(final_path)
        if final_duration is None or final_duration <= 0:
            append_failure(video_url, "verify_final_mp3", "MP3 cuối không đọc được duration", workspace["channel_id"], title)
            return False

        # ------------------------------------------------------------
        # STEP 8: MARK DONE APPEND-ONLY.
        # ------------------------------------------------------------
        stage = "mark_done"
        append_global_done_link(video_url)
        append_channel_done_link(workspace, video_url)
        write_video_log(workspace, video_id, f"DONE -> {final_path}")

        cutter.cleanup_after_success(raw_video)
        print(f"✅ HOÀN TẤT MP3: {final_path}")
        return True

    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if workspace:
            write_video_log(workspace, video_id, f"ERROR stage={stage}: {exc}\n{traceback.format_exc()}")
            channel_id = workspace.get("channel_id", "")
        else:
            channel_id = ""
        append_failure(video_url, stage, exc, channel_id, title)
        print(f"❌ Lỗi tại stage={stage}: {exc}")
        return False
    finally:
        if DELETE_LIVE_COOKIE_AFTER_DOWNLOAD:
            try:
                LIVE_COOKIE_FILE.unlink(missing_ok=True)
            except Exception:
                pass

def main():
    configure_console()
    ensure_dirs()

    print("=" * 72)
    print(" AUTO YOUTUBE MAIN-CONTENT CUT PIPELINE - CHATGPT ROUND ROBIN")
    print(" YouTube get_panel -> ChatGPT multi-profile -> yt-dlp -> FFmpeg cut/merge")
    print("=" * 72)
    print(f"📁 Project: {BASE_DIR}")
    print(f"📁 YouTube profile: {YOUTUBE_USER_DATA_DIR}")
    print(f"📁 ChatGPT multi-profile root: {CHATGPT_ACCOUNT_PROFILE_ROOT}")

    yt_cookie_db = YOUTUBE_USER_DATA_DIR / PROFILE_DIRECTORY / "Network" / "Cookies"
    print(f"🔐 YouTube profile data: {'CÓ' if yt_cookie_db.exists() else 'CHƯA CÓ/PROFILE MỚI'}")

    if not cutter.check_required_programs():
        return

    prompt_template = load_prompt_template()
    if not prompt_template:
        return

    urls = read_urls_from_console_or_list()
    if not urls:
        print("❌ Không có link để xử lý.")
        return

    rotation_targets = build_chatgpt_rotation_targets()
    if not rotation_targets:
        print("❌ Không có account/profile nào vừa enabled vừa có Project URL.")
        print("👉 Chạy LOGIN_CHATGPT_ACCOUNTS.bat để login và gán Project trước.")
        return

    rotation_cursor = load_chatgpt_round_robin_cursor(len(rotation_targets))
    print_chatgpt_rotation_plan(rotation_targets, rotation_cursor)

    js_arguments = cutter.get_javascript_arguments()
    if js_arguments is None:
        return

    cutter.update_ytdlp()

    youtube_driver = None
    chatgpt_driver = None
    chatgpt_process = None
    current_project = None

    try:
        # YouTube giữ nguyên một Selenium profile cho cả batch.
        youtube_driver = create_youtube_driver()
        if not youtube_driver:
            return

        # ============================================================
        # SCHEDULER 2 PHASE:
        #   PHASE 1 = chỉ link MỚI/chưa từng fail
        #   PHASE 2 = retry link fail SAU KHI phase 1 chạy hết
        #
        # Một link fail ở phase 1 chỉ được enqueue, KHÔNG retry ngay.
        # Ở phase 2 mỗi link chỉ retry 1 lần trong run hiện tại.
        # Nếu vẫn fail -> để lần chạy sau, không loop vô hạn.
        # ============================================================
        done_now = load_global_done_set()
        historical_failed = load_unresolved_failed_set(done_now)

        fresh_urls = [u for u in urls if u not in historical_failed]
        retry_queue = [u for u in urls if u in historical_failed]
        retry_seen = set(retry_queue)

        print("\n" + "=" * 72)
        print("📋 LỊCH XỬ LÝ LINK - ƯU TIÊN LINK MỚI")
        print("=" * 72)
        print(f"🆕 Link mới/chưa từng fail: {len(fresh_urls):,}")
        print(f"⏳ Link fail cũ hoãn về cuối: {len(retry_queue):,}")
        print("✅ Link fail trong lúc chạy sẽ KHÔNG retry ngay.")
        print("🔁 Chỉ sau khi chạy hết link mới mới bắt đầu RETRY.")
        print("=" * 72)

        success_urls = set()
        unresolved_urls = set()
        fresh_failed_this_run = 0
        retry_attempts = 0

        def run_one_scheduled(video_url, display_index, display_total, phase_name):
            nonlocal youtube_driver
            nonlocal chatgpt_driver
            nonlocal chatgpt_process
            nonlocal current_project
            nonlocal rotation_cursor

            # Chọn profile theo round-robin và nhớ cursor xuyên qua restart chương trình.
            target_index = rotation_cursor % len(rotation_targets)
            target = rotation_targets[target_index]
            account = target["account"]
            project = target["project"]

            print("\n" + "=" * 72)
            print(
                f"🔁 ROUND ROBIN {target_index + 1}/{len(rotation_targets)} | "
                f"{phase_name} {display_index}/{display_total} -> "
                f"{account['name']} [{account['key']}]"
            )
            print(f"📂 Project: {project['name']}")
            print("=" * 72)

            ok = False
            try:
                # Browser YouTube có thể crash sau nhiều giờ/ngày.
                if not driver_alive(youtube_driver):
                    print("♻️ YouTube driver đã mất kết nối, đang mở lại...")
                    try:
                        youtube_driver.quit()
                    except Exception:
                        pass
                    youtube_driver = create_youtube_driver()
                    if not youtube_driver:
                        raise RuntimeError("Không recover được YouTube driver")

                chatgpt_driver, chatgpt_process, current_project = open_chatgpt_rotation_target(
                    account,
                    project,
                    old_driver=chatgpt_driver,
                    old_process=chatgpt_process,
                )

                ensure_chatgpt_ready_interactive(chatgpt_driver)

                ok = process_one_video(
                    youtube_driver,
                    chatgpt_driver,
                    video_url,
                    prompt_template,
                    js_arguments,
                    display_index,
                    display_total,
                    chatgpt_project=current_project,
                )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                ok = False
                print(f"\n❌ Lỗi ngoài dự kiến với profile/video này: {exc}")

            finally:
                # Mỗi ATTEMPT dùng xong một profile thì cursor đi tiếp,
                # bất kể success/fail.
                rotation_cursor = (target_index + 1) % len(rotation_targets)
                save_chatgpt_round_robin_cursor(rotation_cursor)

                if CHATGPT_ROUND_ROBIN_CLOSE_EACH_VIDEO:
                    close_chatgpt_browser_hard(chatgpt_driver, chatgpt_process)
                    chatgpt_driver = None
                    chatgpt_process = None
                    current_project = None

            return ok

        # ------------------------------------------------------------
        # PHASE 1: CHỈ LINK MỚI.
        # ------------------------------------------------------------
        if fresh_urls:
            print("\n" + "#" * 72)
            print("🆕 PHASE 1/2 - CHẠY TOÀN BỘ LINK MỚI TRƯỚC")
            print("#" * 72)

        for index, video_url in enumerate(fresh_urls, start=1):
            ok = run_one_scheduled(
                video_url,
                index,
                len(fresh_urls),
                "LINK MỚI",
            )

            if ok:
                success_urls.add(video_url)
                unresolved_urls.discard(video_url)
            else:
                fresh_failed_this_run += 1
                unresolved_urls.add(video_url)

                # Hoãn về cuối; không retry ngay.
                if video_url not in retry_seen:
                    retry_seen.add(video_url)
                    retry_queue.append(video_url)

                print(
                    "⏭️ Link FAIL -> ĐÃ HOÃN RETRY VỀ CUỐI. "
                    "Tiếp tục link mới kế tiếp."
                )

        # ------------------------------------------------------------
        # PHASE 2: RETRY FAIL SAU KHI HẾT LINK MỚI.
        # historical failed + fresh failures, mỗi URL đúng 1 lượt.
        # ------------------------------------------------------------
        if retry_queue:
            print("\n" + "#" * 72)
            print("🔁 PHASE 2/2 - ĐÃ HẾT LINK MỚI, BẮT ĐẦU RETRY LINK FAIL")
            print(f"📦 Tổng link cần retry: {len(retry_queue):,}")
            print("⚠️ Mỗi link chỉ retry 1 lần trong run này; fail nữa để lần chạy sau.")
            print("#" * 72)

        for retry_index, video_url in enumerate(retry_queue, start=1):
            # Có thể link đã thành công ở phase 1 qua recovery bên trong process,
            # hoặc được mark DONE từ tác vụ khác; bỏ qua nếu giờ đã done.
            if video_url in load_global_done_set():
                print(
                    f"⏭️ RETRY {retry_index}/{len(retry_queue)} đã DONE trước lượt retry -> bỏ qua."
                )
                success_urls.add(video_url)
                unresolved_urls.discard(video_url)
                continue

            retry_attempts += 1
            ok = run_one_scheduled(
                video_url,
                retry_index,
                len(retry_queue),
                "RETRY",
            )

            if ok:
                success_urls.add(video_url)
                unresolved_urls.discard(video_url)
                print("✅ RETRY thành công.")
            else:
                unresolved_urls.add(video_url)
                print(
                    "❌ RETRY vẫn fail -> KHÔNG chạy lại lần nữa trong run này. "
                    "Để dành cho lần chạy sau."
                )

        success = len(success_urls)
        failed = len(unresolved_urls)

        print("\n" + "=" * 72)
        print("KẾT QUẢ")
        print(f"✅ Thành công: {success}")
        print(f"❌ Thất bại còn lại: {failed}")
        print(f"⏳ Fail phát sinh ở phase link mới: {fresh_failed_this_run}")
        print(f"🔁 Số lượt retry cuối batch: {retry_attempts}")
        print(f"🔄 Profile kế tiếp khi chạy lại: {rotation_targets[rotation_cursor]['account']['name']}")
        print(f"📁 Video: {CHANNELS_DIR} / <CHANNEL NAME _ CHANNEL ID> / done")
        print(f"📝 Transcript: {CHANNELS_DIR} / <CHANNEL NAME _ CHANNEL ID> / transcripts")
        print(f"🤖 AI: {CHANNELS_DIR} / <CHANNEL NAME _ CHANNEL ID> / ai_results")
        print("=" * 72)

        if success and os.name == "nt":
            try:
                os.startfile(CHANNELS_DIR)
            except Exception:
                pass

    except cutter.UserQuit:
        print("\n⛔ Đã dừng theo yêu cầu.")

    except KeyboardInterrupt:
        print("\n⛔ Đã dừng bằng Ctrl+C.")

    finally:
        if youtube_driver is not None:
            try:
                youtube_driver.quit()
            except Exception:
                pass

        if chatgpt_driver is not None or chatgpt_process not in (None, False):
            close_chatgpt_browser_hard(chatgpt_driver, chatgpt_process)


if __name__ == "__main__":
    main()
