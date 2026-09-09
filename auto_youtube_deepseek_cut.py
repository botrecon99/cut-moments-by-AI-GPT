# -*- coding: utf-8 -*-
"""
AUTO YOUTUBE -> TRANSCRIPT -> DEEPSEEK ROUND ROBIN -> MP3 CUT -> MERGE

Luồng:
1) Mở YouTube bằng Selenium với profile local của project.
2) Mở video YouTube.
3) Lấy transcript có timestamp từ YouTube theo nhiều tầng fallback.
   - Direct get_panel bằng session Chrome hiện tại.
   - Nếu có nút Show transcript: bắt response get_panel thật từ Network.
   - Nếu video KHÔNG có nút Show transcript/get_panel: lấy captionTracks động rồi fetch /api/timedtext?fmt=json3.
   - KHÔNG hard-code cookie, signature, pot, expire hay cURL; mọi token lấy mới từ chính video/session hiện tại.
4) Tạo 2 file RIÊNG: *_PROMPT.txt và *_TRANSCRIPT.txt; tuyệt đối không ghép text.
5) Mở Chrome DeepSeek thật và attach Selenium; chỉ hỏi login/xác minh khi phát hiện auth/challenge thật; PROMPT được COPY vào Windows clipboard và Ctrl+V đúng MỘT LẦN vào composer; tuyệt đối không chèn bằng JS/CDP/chunk. Sau đó code verify cấu trúc + thứ tự timestamp mẫu. TRANSCRIPT được đính kèm RIÊNG bằng uploader thật. Mỗi bước phải verify thành công mới được Send.
6) Parse NONE hoặc các mốc HH:MM:SS --> ... / END.
7) Sau khi DeepSeek trả mốc hợp lệ, job được đẩy sang MEDIA WORKER nền để tải AUDIO-ONLY/MP3.
8) Main thread chuyển ngay sang video kế tiếp; media worker cắt/ghép và chỉ khi MP3 cuối OK mới ghi DONE.

LƯU Ý:
- Hai profile nằm trong ./chrome_profiles/youtube và ./chrome_profiles/deepseek.
- Không cần copy cookie YouTube vào code. Cookie trong cURL sẽ hết hạn.
- DeepSeek giữ Chrome/profile sống khi có thể; không dừng tay nếu session còn hợp lệ.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import sys
import time
import traceback
from pathlib import Path
from queue import Queue, Empty
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

APP_DIR = Path(__file__).resolve().parent
WORKSPACE_BRIDGE_FILE = APP_DIR / "workspace_bridge.json"

def _load_workspace_bridge():
    """
    DeepSeek code/project nằm riêng, nhưng DATA có thể trỏ thẳng về project ChatGPT cũ.

    Nhờ vậy không copy hàng GB:
      - list.txt
      - doneLink.txt
      - failedLink.jsonl
      - channels/
      - transcripts / ai_results / done MP3
      - downloads/raw_audio dở
      - YouTube Chrome profile

    Nếu chưa cấu hình bridge thì DATA_DIR = APP_DIR như project độc lập bình thường.
    """
    data_root = APP_DIR
    reuse_youtube_profile = True

    if WORKSPACE_BRIDGE_FILE.exists():
        try:
            cfg = json.loads(
                WORKSPACE_BRIDGE_FILE.read_text(encoding="utf-8-sig")
            )
            raw = str(cfg.get("data_root") or "").strip()
            reuse_youtube_profile = bool(cfg.get("reuse_youtube_profile", True))
            if raw:
                p = Path(raw).expanduser()
                if not p.is_absolute():
                    p = (APP_DIR / p).resolve()
                if p.exists() and p.is_dir():
                    data_root = p.resolve()
                else:
                    print(f"⚠️ workspace_bridge data_root không tồn tại: {p}")
        except Exception as exc:
            print(f"⚠️ Không đọc được workspace_bridge.json: {exc}")

    return data_root, reuse_youtube_profile

DATA_DIR, REUSE_OLD_YOUTUBE_PROFILE = _load_workspace_bridge()

# BASE_DIR giữ tên cũ để phần code legacy tiếp tục dùng DATA workspace.
BASE_DIR = DATA_DIR

# Prompt là tài nguyên của project DeepSeek mới.
PROMPT_FILE = APP_DIR / "prompt.txt"

# Các state/data bên dưới tiếp tục dùng workspace cũ nếu bridge đang bật.
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
AI_RESULT_DIR = DATA_DIR / "ai_results"
AI_INPUT_DIR = DATA_DIR / "ai_inputs"
CHANNELS_DIR = DATA_DIR / "channels"
CHANNEL_INDEX_FILE = DATA_DIR / "channel_index.json"

# Cookie/session tạm của DeepSeek project để riêng, không dùng file runtime cũ.
APP_RUNTIME_DIR = APP_DIR / "runtime"
LIVE_COOKIE_FILE = APP_RUNTIME_DIR / "youtube_live_cookies.txt"

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

# DeepSeek profiles thuộc PROJECT MỚI.
PROFILE_ROOT = APP_DIR / "chrome_profiles"
DEEPSEEK_USER_DATA_DIR = PROFILE_ROOT / "deepseek"

# YouTube có thể reuse đúng profile của workspace cũ để khỏi login/cookie lại.
if REUSE_OLD_YOUTUBE_PROFILE:
    YOUTUBE_USER_DATA_DIR = DATA_DIR / "chrome_profiles" / "youtube"
else:
    YOUTUBE_USER_DATA_DIR = PROFILE_ROOT / "youtube"

# DeepSeek được mở bằng Chrome THẬT trước, chưa có Selenium điều khiển.
# Selenium attach vào Chrome thật đang chạy; chỉ cần thao tác tay nếu auth/challenge thật sự xuất hiện.
DEEPSEEK_DEBUG_HOST = "127.0.0.1"
DEEPSEEK_DEBUG_PORT = 9322

YOUTUBE_HOME = "https://www.youtube.com/"
DEEPSEEK_HOME = "https://chat.deepseek.com/"

# ============================================================
# DEEPSEEK DIRECT MODE - KHÔNG DÙNG PROJECT
# ============================================================
# True = mọi video tạo chat thường trực tiếp tại https://chat.deepseek.com/
# Không cần legacy URL, không cần tạo Project.
DEEPSEEK_DIRECT_MODE = True

# Confirmed from user's screenshots.
DEEPSEEK_UI_SCREENSHOT_CALIBRATED = True

# BẬT DEEPTHINK CHO MỖI LẦN PHÂN TÍCH.
# Mỗi NEW CHAT / mỗi attempt sẽ click DeepThink đúng 1 lần trước khi dán prompt.
DEEPSEEK_FORCE_DEEPTHINK_EACH_ANALYSIS = True


# Legacy compatibility mode: mỗi lần chạy chọn 1 Project/người.
# Mỗi video/attempt sẽ quay về đúng URL Project này để tạo NEW CHAT bên trong Project,
# tuyệt đối không tạo chat ngoài trang Home.
DEEPSEEK_PROJECTS_FILE = APP_DIR / "deepseek_projects.json"  # legacy config, kept for compatibility
DEEPSEEK_ACCOUNTS_FILE = APP_DIR / "deepseek_accounts.json"
DEEPSEEK_ACCOUNT_PROFILE_ROOT = PROFILE_ROOT / "deepseek_accounts"
LAST_DEEPSEEK_ACCOUNT_FILE = APP_RUNTIME_DIR / "last_deepseek_account.txt"
LAST_DEEPSEEK_PROJECT_FILE = APP_RUNTIME_DIR / "last_deepseek_project.txt"
DEEPSEEK_DEBUG_ACCOUNT_FILE = APP_RUNTIME_DIR / "deepseek_debug_account.txt"
ACTIVE_DEEPSEEK_ACCOUNT_KEY = ""
ACTIVE_DEEPSEEK_ACCOUNT_NAME = ""

WAIT_PAGE = 35
WAIT_TRANSCRIPT = 20
WAIT_DEEPSEEK_READY = 20
WAIT_DEEPSEEK_RESPONSE = 240

# Tự đóng hẳn Chrome DeepSeek và mở lại sau mỗi N video để giải phóng RAM.
# 0 = tắt. Giá trị này chỉ còn dùng khi chạy SINGLE PROFILE.
DEEPSEEK_RESTART_EVERY = 0

# MULTI-PROFILE ROUND ROBIN:
# True  = VIDEO 1 -> profile 1, VIDEO 2 -> profile 2, ... rồi quay vòng.
# Mỗi profile dùng Project đầu tiên đang enabled trong deepseek_accounts.json.
DEEPSEEK_ROUND_ROBIN = True
# Đóng HẲN Chrome DeepSeek sau mỗi video để chỉ có 1 profile DeepSeek chạy tại một thời điểm.
DEEPSEEK_ROUND_ROBIN_CLOSE_EACH_VIDEO = False

# ============================================================
# PIPELINE CUỐN CHIẾU AI -> MEDIA
# ============================================================
# 1 worker media là cố ý: story_cutter_core dùng chung DOWNLOAD_DIR/raw_audio.
# Một worker giúp không đụng file tạm, không tranh quá nhiều mạng/CPU với Chrome.
MEDIA_WORKER_ENABLED = True
MEDIA_QUEUE_MAX = 2          # AI được chạy trước tối đa ~2 video
MEDIA_WORKER_COUNT = 1       # giữ = 1 với kiến trúc DOWNLOAD_DIR hiện tại
MEDIA_COOKIE_DIR = APP_RUNTIME_DIR / "media_cookies"


# DeepSeek sidebar/project list can occasionally return HTTP 429 on
# /backend-api/conversations while the current model answer still succeeds.
# IMPORTANT: pipeline NEVER calls that endpoint itself just to "check" it, because
# another GET would increase request pressure. We only PASSIVELY observe requests
# the DeepSeek page already made, then dismiss the matching "Too many requests" popup.
DEEPSEEK_CONVERSATIONS_API_FRAGMENT = ""
DEEPSEEK_API429_POPUP_WAIT = 2.0
DEEPSEEK_RESTART_CLOSE_WAIT = 20

# True = sau khi mỗi video xử lý xong, mở chat mới cho video tiếp theo.
NEW_CHAT_EACH_VIDEO = True

# True = cố bấm nút Copy của DeepSeek. Nếu selector UI đổi, vẫn fallback lấy text từ DOM.
CLICK_DEEPSEEK_COPY = True

# Nếu list.txt trống, chương trình sẽ hỏi link trực tiếp trong console.

TRANSCRIPT_PLACEHOLDER = "[DÁN TOÀN BỘ TRANSCRIPT CÓ TIMESTAMP VÀO ĐÂY]"

# ======================== FINAL / SCALE CONFIG ========================
# list.txt có thể chứa hàng chục nghìn link. File này KHÔNG bị rewrite sau mỗi video.
# Link hoàn thành chỉ được append vào doneLink.txt để resume nhanh/an toàn.
GLOBAL_DONE_FILE = DATA_DIR / "doneLink.txt"
GLOBAL_FAILED_FILE = DATA_DIR / "failedLink.jsonl"
GLOBAL_NO_STORY_FILE = DATA_DIR / "no_story.txt"
GLOBAL_LOG_DIR = DATA_DIR / "logs"
RUNTIME_DIR = APP_RUNTIME_DIR
DEEPSEEK_DEBUG_PORT_FILE = RUNTIME_DIR / "deepseek_debug_port.txt"
DEEPSEEK_ROUND_ROBIN_CURSOR_FILE = RUNTIME_DIR / "deepseek_round_robin_cursor.txt"

# Folder kênh đúng format user yêu cầu:
#   CHANNEL NAME _ CHANNEL ID
# Nếu channel đổi tên, pipeline ưu tiên reuse folder cũ có cùng CHANNEL ID để không tách dữ liệu.
RENAME_CHANNEL_FOLDER_WHEN_NAME_CHANGES = False

# Nếu AI result hợp lệ đã tồn tại từ lần chạy trước (ví dụ lần trước download 403),
# pipeline reuse kết quả để KHÔNG gửi DeepSeek lại.
REUSE_VALID_AI_RESULT = True

# Nếu output DeepSeek sai format / bịa timestamp, tự yêu cầu sửa format tối đa số lần này.
AI_REPAIR_ATTEMPTS = 2

# FAST REPAIR: nếu DeepSeek chỉ lệch timestamp vài giây so với mốc thật,
# sửa LOCAL bằng mốc transcript gần nhất, KHÔNG bắt DeepSeek phân tích lại.
AUTO_LOCAL_TIMESTAMP_REPAIR = True
AUTO_LOCAL_TIMESTAMP_MAX_DELTA = 12.0

# DeepSeek: prompt native Ctrl+V một lần; transcript chỉ upload FILE thật.
DEEPSEEK_UPLOAD_WAIT = 30
DEEPSEEK_UPLOAD_RETRIES = 2
DEEPSEEK_PASTE_FALLBACK_WAIT = 20  # legacy helper only
DEEPSEEK_SMART_CHAT_ATTEMPTS = 2
PROMPT_NATIVE_PASTE_WAIT = 8
PROMPT_MIN_WORD_RATIO = 0.95
# Trên UI DeepSeek hiện tại, paste dài có thể tự biến thành “pasted text” attachment.
# Với prompt dài hơn ngưỡng này, dùng CDP Input.insertText MỘT LẦN (không chunk) để giữ inline.
PROMPT_INLINE_NATIVE_MAX_CHARS = 4200

# Compatibility constants for legacy pasted-text helper definitions that remain in
# this consolidated FINAL file. The active SMART pipeline below does not depend
# on them for its main upload path, but Python evaluates default arguments when
# defining functions, so these names MUST exist at import/startup time.
DEEPSEEK_PASTE_RETRIES = 2
PASTE_ATTACHMENT_WAIT = DEEPSEEK_PASTE_FALLBACK_WAIT

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
# DEEPSEEK LEGACY COMPAT CONFIG
# ============================================================

DEFAULT_DEEPSEEK_PROJECTS = []  # DIRECT MODE: legacy only, không dùng.

# Nếu deepseek_accounts.json đã tồn tại thì code vẫn dùng các account/profile trong đó
# nhưng BỎ QUA hoàn toàn field "projects".
# Nếu chưa có file config, tạo 1 profile mặc định để chạy trực tiếp.
DEFAULT_DEEPSEEK_ACCOUNTS = [
    {
        "key": "default",
        "name": "default",
        "profile_dir": "chrome_profiles/deepseek_accounts/default",
        "enabled": True,
        "projects": [],
    }
]



def _safe_account_key(value):
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return value or "account"


def _normalize_deepseek_project(project):
    if not isinstance(project, dict):
        return None
    key = str(project.get("key") or project.get("name") or "").strip()
    name = str(project.get("name") or key).strip()
    url = str(project.get("url") or "").strip().rstrip("/")
    enabled = bool(project.get("enabled", True))
    if not key or not url:
        return None
    if not re.match(r"^https://deepseek\.com/g/g-p-[^/]+/project$", url, flags=re.I):
        return None
    project_id_match = re.search(r"/(g-p-[^/]+)/project$", url, flags=re.I)
    project_id = project_id_match.group(1) if project_id_match else ""
    return {"key": key, "name": name or key, "url": url, "project_id": project_id, "enabled": enabled}


def _normalize_deepseek_account(account):
    """
    DeepSeek profile path is CANONICAL and always project-local:

        <APP_DIR>/chrome_profiles/deepseek_accounts/<account_key>

    We intentionally ignore stale/absolute profile_dir values from old configs.
    This guarantees LOGIN helper and main pipeline open EXACTLY the same Chrome profile.
    """
    if not isinstance(account, dict):
        return None

    key = _safe_account_key(account.get("key") or account.get("name"))
    name = str(account.get("name") or key).strip() or key
    enabled = bool(account.get("enabled", True))

    canonical_profile = (
        APP_DIR / "chrome_profiles" / "deepseek_accounts" / key
    ).resolve()

    configured_raw = str(account.get("profile_dir") or "").strip()
    if configured_raw:
        try:
            configured = Path(configured_raw)
            if not configured.is_absolute():
                configured = (APP_DIR / configured).resolve()
            else:
                configured = configured.resolve()

            if configured != canonical_profile:
                print(
                    f"⚠️ DeepSeek profile_dir config cũ của '{key}' bị bỏ qua:\n"
                    f"   config: {configured}\n"
                    f"   dùng:   {canonical_profile}"
                )
        except Exception:
            pass

    canonical_profile.mkdir(parents=True, exist_ok=True)

    # DeepSeek Direct: projects are legacy-only.
    return {
        "key": key,
        "name": name,
        "enabled": enabled,
        "profile_dir": str(canonical_profile),
        "projects": [],
    }

def ensure_deepseek_accounts_file():
    if DEEPSEEK_ACCOUNTS_FILE.exists():
        return
    payload = {
        "accounts": DEFAULT_DEEPSEEK_ACCOUNTS,
        "note": "DIRECT MODE: mỗi account dùng một Chrome profile riêng; field projects không bắt buộc và bị bỏ qua.",
    }
    DEEPSEEK_ACCOUNTS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_deepseek_accounts():
    ensure_deepseek_accounts_file()
    try:
        payload = json.loads(DEEPSEEK_ACCOUNTS_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"❌ Không đọc được {DEEPSEEK_ACCOUNTS_FILE.name}: {exc}")
        return []
    raw_accounts = payload.get("accounts", []) if isinstance(payload, dict) else payload
    accounts = []
    seen = set()
    for item in raw_accounts if isinstance(raw_accounts, list) else []:
        account = _normalize_deepseek_account(item)
        if not account or not account["enabled"]:
            continue
        if account["key"].lower() in seen:
            continue
        seen.add(account["key"].lower())
        accounts.append(account)
    return accounts


def set_active_deepseek_account(account):
    global DEEPSEEK_USER_DATA_DIR, ACTIVE_DEEPSEEK_ACCOUNT_KEY, ACTIVE_DEEPSEEK_ACCOUNT_NAME
    if not account:
        return None
    DEEPSEEK_USER_DATA_DIR = Path(account["profile_dir"]).resolve()
    DEEPSEEK_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_DEEPSEEK_ACCOUNT_KEY = str(account.get("key") or "")
    ACTIVE_DEEPSEEK_ACCOUNT_NAME = str(account.get("name") or ACTIVE_DEEPSEEK_ACCOUNT_KEY)
    return DEEPSEEK_USER_DATA_DIR


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


def choose_deepseek_project():
    """Chọn account/profile trước rồi chọn Project thuộc account đó."""
    accounts = load_deepseek_accounts()
    if not accounts:
        print("❌ Không có DeepSeek account/profile hợp lệ trong deepseek_accounts.json")
        print("👉 Chạy LOGIN_DEEPSEEK_ACCOUNTS.bat để tạo/login profile trước.")
        return None
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    last_account = ""
    last_project = ""
    try:
        last_account = LAST_DEEPSEEK_ACCOUNT_FILE.read_text(encoding="utf-8").strip().lower()
    except Exception:
        pass
    try:
        last_project = LAST_DEEPSEEK_PROJECT_FILE.read_text(encoding="utf-8").strip().lower()
    except Exception:
        pass
    default_account_index = next((i for i,a in enumerate(accounts) if a["key"].lower() == last_account), 0)
    account = _choose_from_list(
        accounts,
        "CHỌN DEEPSEEK ACCOUNT / CHROME PROFILE",
        default_account_index,
        lambda a: f"{a['name']}  [{a['key']}]  | projects={len(a['projects'])}",
    )
    if not account:
        return None
    set_active_deepseek_account(account)
    try:
        LAST_DEEPSEEK_ACCOUNT_FILE.write_text(account["key"], encoding="utf-8")
    except Exception:
        pass
    projects = account.get("projects") or []
    if not projects:
        print(f"❌ Account '{account['name']}' chưa có legacy URL.")
        print("👉 Chạy LOGIN_DEEPSEEK_ACCOUNTS.bat -> thêm Project cho account.")
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
    project.update({"account_key": account["key"], "account_name": account["name"], "profile_dir": str(DEEPSEEK_USER_DATA_DIR)})
    try:
        LAST_DEEPSEEK_PROJECT_FILE.write_text(f"{account['key']}|{project['key']}", encoding="utf-8")
    except Exception:
        pass
    print(f"✅ DeepSeek account: {account['name']} [{account['key']}]")
    print(f"📁 Chrome profile: {DEEPSEEK_USER_DATA_DIR}")
    print(f"✅ DeepSeek Project: {project['name']}")
    print(f"📂 {project['url']}")
    return project



def build_deepseek_rotation_targets():
    """
    DIRECT MODE:
    - Xoay tất cả account/profile enabled trong deepseek_accounts.json.
    - KHÔNG cần Project.
    - Field projects nếu còn trong JSON chỉ là legacy và bị bỏ qua.
    """
    accounts = load_deepseek_accounts()
    targets = []

    for account in accounts:
        direct_target = {
            "key": "direct",
            "name": "DeepSeek Direct",
            "url": DEEPSEEK_HOME,
            "project_id": "",
            "enabled": True,
            "direct": True,
            "account_key": account["key"],
            "account_name": account["name"],
            "profile_dir": account["profile_dir"],
        }
        targets.append({
            "account": account,
            "project": direct_target,  # compatibility name; đây KHÔNG phải Project thật
        })

    return targets


def load_deepseek_round_robin_cursor(target_count):
    """Nhớ lượt profile giữa các lần restart chương trình."""
    if target_count <= 0:
        return 0
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        value = int(DEEPSEEK_ROUND_ROBIN_CURSOR_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        value = 0
    return max(0, value) % target_count


def save_deepseek_round_robin_cursor(next_index):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        DEEPSEEK_ROUND_ROBIN_CURSOR_FILE.write_text(str(max(0, int(next_index))), encoding="utf-8")
    except Exception:
        pass


def print_deepseek_rotation_plan(targets, start_cursor=0):
    print("\n" + "=" * 72)
    print("DEEPSEEK DIRECT MULTI-PROFILE ROUND ROBIN - KHÔNG PROJECT")
    print("=" * 72)
    for idx, target in enumerate(targets, start=1):
        account = target["account"]
        start_mark = "  < lượt đầu" if (idx - 1) == (start_cursor % len(targets)) else ""
        print(
            f"{idx}) {account['name']} [{account['key']}] -> "
            f"DeepSeek Direct{start_mark}"
        )
        print(f"   📁 {account['profile_dir']}\\{PROFILE_DIRECTORY}")
    print(f"🔄 Tổng profile trong vòng xoay: {len(targets)}")
    print("🆕 Mỗi video = 1 chat thường mới tại chat.deepseek.com.")
    print("🔐 LOGIN helper và main code dùng CHÍNH XÁC cùng profile path.")
    print("⚡ Không tạo/không dùng Project; Chrome profile được giữ để nhanh.")

def deepseek_project_url_matches(current_url, project):
    """
    Compatibility matcher.
    DeepSeek DIRECT MODE: chỉ cần đang ở chat.deepseek.com.
    Conversation cũ /a/chat/s/... được chặn riêng.
    """
    current = str(current_url or "").lower()
    if (project or {}).get("direct"):
        return current.startswith("https://chat.deepseek.com")

    expected = str((project or {}).get("url") or "").rstrip("/").lower()
    return bool(expected and current.rstrip("/").startswith(expected))


def deepseek_is_existing_conversation_url(url):
    """DeepSeek conversation hiện dùng dạng /a/chat/s/<conversation-id>."""
    value = str(url or "").lower()
    return "/a/chat/s/" in value



def open_deepseek_project_new_chat(driver, project, timeout=None):
    """
    Mở DeepSeek Home để tạo chat mới.
    Composer trên DeepSeek Home chính là điểm bắt đầu một chat mới trong Project.
    Không click lịch sử chat cũ, không reuse conversation cũ.
    """
    timeout = timeout or WAIT_DEEPSEEK_READY
    if not project:
        project = {
            "key": "direct",
            "name": "DeepSeek Direct",
            "url": DEEPSEEK_HOME,
            "project_id": "",
            "direct": True,
        }

    is_direct = bool(project.get("direct") or DEEPSEEK_DIRECT_MODE)
    target = DEEPSEEK_HOME
    if not target:
        raise RuntimeError("DeepSeek target URL rỗng")

    if is_direct:
        print("🆕 NEW CHAT TRỰC TIẾP: chat.deepseek.com (Direct)")
    else:
        print(f"📁 NEW CHAT trong Project: {project.get('name') or project.get('key')}")

    # FAST + ĐÚNG:
    # Chỉ được bỏ navigation nếu đang ở PROJECT ROOT.
    # Nếu URL có /c/... thì đó là conversation cũ -> BẮT BUỘC quay về Project root
    # để video mới không dồn transcript vào chat trước.
    current_before = safe_current_url(driver)
    in_old_conversation = deepseek_is_existing_conversation_url(current_before)

    already_ready = False
    try:
        already_ready = (
            not in_old_conversation
            and deepseek_project_url_matches(current_before, project)
            and find_deepseek_composer(driver) is not None
        )
    except Exception:
        already_ready = False

    if already_ready:
        print("⚡ Đang ở DeepSeek Home + composer sạch -> dùng luôn." if is_direct else "⚡ Đang ở Project ROOT + composer sạch -> dùng luôn.")
    else:
        clicked_new_chat = False

        if in_old_conversation:
            print("🆕 Đang ở chat cũ (/a/chat/s/...) -> click New chat.")
            try:
                candidates = driver.find_elements(
                    By.XPATH,
                    "//*[self::button or @role='button'][contains(normalize-space(.), 'New chat')]"
                )
                for el in candidates:
                    try:
                        if el.is_displayed() and el.is_enabled():
                            el.click()
                            clicked_new_chat = True
                            print("✅ Đã click New chat.")
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        if clicked_new_chat:
            try:
                wait_deepseek_composer(driver, timeout=min(timeout, 12))
                sleep(0.2)
            except Exception:
                clicked_new_chat = False

        if not clicked_new_chat:
            try:
                driver.get(target)
            except Exception as exc:
                raise RuntimeError(f"Không mở được DeepSeek Home: {exc}") from exc

        try:
            WebDriverWait(driver, min(timeout, 15)).until(
                lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
            )
        except Exception:
            pass

    # Passive check: nếu request sidebar /backend-api/conversations vừa bị HTTP 429,
    # KHÔNG gửi thêm API request để check; chỉ quan sát request browser đã tạo rồi
    # tự nhấn Got it nếu popup xuất hiện.
    install_deepseek_429_network_observer(driver)
    handle_deepseek_conversations_api_429(driver, quiet=False)

    composer = wait_deepseek_composer(driver, timeout=timeout)
    handle_deepseek_conversations_api_429(driver, quiet=True)

    # Chốt NEW CHAT: không được phép còn ở /c/... của chat cũ.
    current = safe_current_url(driver)
    if deepseek_is_existing_conversation_url(current):
        print("🔄 DeepSeek vẫn giữ chat cũ -> ép mở lại DeepSeek Home 1 lần..." if is_direct else "🔄 DeepSeek vẫn giữ conversation cũ -> ép mở lại Project root 1 lần...")
        driver.get(target)
        composer = wait_deepseek_composer(driver, timeout=min(timeout, 20))
        current = safe_current_url(driver)

    if deepseek_is_existing_conversation_url(current):
        raise RuntimeError(
            "Không tạo được chat mới: URL vẫn đang ở conversation cũ (/a/chat/s/...)."
        )

    # DIRECT MODE chỉ yêu cầu vẫn ở deepseek.com và không phải chat cũ /c/...
    if is_direct:
        if not str(current or "").lower().startswith("https://chat.deepseek.com"):
            raise RuntimeError(f"DeepSeek bị redirect khỏi chat.deepseek.com. URL hiện tại: {current}")
    else:
        # Nếu bị redirect ra Home/login hoặc Project khác thì không được gửi nhầm.
        if not deepseek_project_url_matches(current, project):
            project_id = project.get("project_id") or ""
            dom_has_project = False
            if project_id:
                try:
                    dom_has_project = bool(driver.find_elements(By.CSS_SELECTOR, f'a[href*="{project_id}"]'))
                except Exception:
                    dom_has_project = False
            if not dom_has_project:
                raise RuntimeError(
                    f"DeepSeek không ở đúng DeepSeek Home '{project.get('name')}'. URL hiện tại: {current}"
                )

    # Xóa draft mà DeepSeek có thể restore ở Project page.
    # Không coi draft cũ là lỗi login. Tự xóa nhiều lớp trước.
    residual = _loose_compare_text(get_deepseek_composer_text(driver, composer))
    if residual:
        print(f"🧹 DeepSeek restore draft cũ ({len(residual)} chars) -> đang tự xóa...")

    for clear_try in range(1, 5):
        composer = find_deepseek_composer(driver) or composer
        clear_deepseek_composer(composer)
        sleep(0.35)
        composer = find_deepseek_composer(driver) or composer
        residual = _loose_compare_text(get_deepseek_composer_text(driver, composer))
        if not residual:
            if clear_try > 1:
                print(f"✅ Đã tự xóa draft ở lượt {clear_try}/4.")
            break
        print(f"   ⚠️ Draft vẫn còn {len(residual)} chars sau clear {clear_try}/4.")

    # Một số phiên DeepSeek restore draft muộn sau navigation.
    # Refresh Project đúng 1 lần rồi clear lại, thay vì bắt user login vô lý.
    if residual:
        print("🔄 Draft bị restore lại -> refresh DeepSeek 1 lần rồi tự xóa tiếp...")
        try:
            driver.get(target)
            WebDriverWait(driver, min(timeout, 45)).until(lambda d: find_deepseek_composer(d))
            sleep(0.8)
            composer = find_deepseek_composer(driver)
            for clear_try in range(1, 4):
                clear_deepseek_composer(composer)
                sleep(0.4)
                composer = find_deepseek_composer(driver) or composer
                residual = _loose_compare_text(get_deepseek_composer_text(driver, composer))
                if not residual:
                    print("✅ Draft đã được xóa sau refresh.")
                    break
        except Exception:
            pass

    if residual:
        raise RuntimeError(
            f"Composer còn draft cũ ({len(residual)} chars) sau AUTO-CLEAR; "
            "đây không phải lỗi login."
        )

    print("✅ DeepSeek Direct + composer sạch. Sẵn sàng tạo chat mới." if is_direct else "✅ Đúng Project + composer sạch. Sẵn sàng tạo chat mới.")
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



def _count_nonempty_lines(path):
    try:
        return sum(
            1 for line in Path(path).read_text(
                encoding="utf-8-sig", errors="replace"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except Exception:
        return 0


def print_resume_bridge_report():
    """Tóm tắt state mà DeepSeek sẽ nối tiếp từ workspace cũ."""
    bridge_on = DATA_DIR.resolve() != APP_DIR.resolve()

    print("\n" + "=" * 72)
    print("♻️ RESUME / WORKSPACE BRIDGE")
    print("=" * 72)
    print(f"📦 DeepSeek app: {APP_DIR}")
    print(f"🗂️ Data workspace: {DATA_DIR}")
    print(f"🔗 Bridge project cũ: {'BẬT' if bridge_on else 'TẮT (standalone)'}")
    print(f"📋 list.txt: {_count_nonempty_lines(DATA_DIR / 'list.txt'):,} dòng")
    print(f"✅ doneLink.txt: {_count_nonempty_lines(GLOBAL_DONE_FILE):,} dòng")

    try:
        unresolved = load_unresolved_failed_set(load_global_done_set())
        print(f"❌ FAIL chưa DONE: {len(unresolved):,}")
    except Exception:
        print("❌ FAIL chưa DONE: ?")

    cuts = 0
    finals = 0
    transcripts = 0
    try:
        if CHANNELS_DIR.exists():
            cuts = sum(1 for _ in CHANNELS_DIR.rglob("*_CUTS.txt"))
            finals = sum(1 for _ in CHANNELS_DIR.rglob("done/*.mp3"))
            transcripts = sum(1 for _ in CHANNELS_DIR.rglob("*_TRANSCRIPT.txt"))
    except Exception:
        pass

    print(f"🧠 AI CUT cache cũ: {cuts:,}")
    print(f"🎵 MP3 final đã có: {finals:,}")
    print(f"📝 Transcript cache: {transcripts:,}")

    try:
        raw = cutter.find_raw_video()
        source_file = cutter.SOURCE_URL_FILE
        raw_url = (
            source_file.read_text(encoding="utf-8").strip()
            if source_file.exists() else ""
        )
        if raw:
            print(f"⏯️ Audio dở có thể resume: {raw.name}")
            if raw_url:
                print(f"   URL: {raw_url}")
        else:
            print("⏯️ Audio dở: không có")
    except Exception:
        pass

    if bridge_on:
        print("⚠️ Không chạy pipeline ChatGPT cũ và DeepSeek mới CÙNG LÚC.")
    print("=" * 72)


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

    Mục tiêu: transcript/cookie đã xong thì phải đi tiếp ngay tới DeepSeek,
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

PIPELINE_FILE_LOCK = threading.RLock()

def append_channel_done_link(workspace, video_url):
    path = workspace["done_links"]
    with PIPELINE_FILE_LOCK:
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
    with PIPELINE_FILE_LOCK:
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
        with PIPELINE_FILE_LOCK:
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



def deepseek_debug_url(path="/json/version"):
    return f"http://{DEEPSEEK_DEBUG_HOST}:{DEEPSEEK_DEBUG_PORT}{path}"


def _debug_port_has_deepseek(port):
    try:
        with urlopen(f"http://{DEEPSEEK_DEBUG_HOST}:{port}/json/list", timeout=1.2) as response:
            items = json.loads(response.read().decode("utf-8", "replace"))
        for item in items if isinstance(items, list) else []:
            url = str(item.get("url") or "")
            if "chat.deepseek.com" in url or "deepseek.com/sign_in" in url or "deepseek.com/sign-in" in url:
                return True
    except Exception:
        pass
    return False


def _choose_deepseek_debug_port():
    """Reuse port chỉ khi marker xác nhận đúng account/profile hiện tại."""
    global DEEPSEEK_DEBUG_PORT
    saved_account = ""
    try:
        saved_account = DEEPSEEK_DEBUG_ACCOUNT_FILE.read_text(encoding="utf-8").strip().lower()
    except Exception:
        pass
    active_account = str(ACTIVE_DEEPSEEK_ACCOUNT_KEY or "").strip().lower()
    try:
        if DEEPSEEK_DEBUG_PORT_FILE.exists():
            saved = int(DEEPSEEK_DEBUG_PORT_FILE.read_text(encoding="utf-8").strip())
            if 1024 <= saved <= 65535 and saved_account and saved_account == active_account and _debug_port_has_deepseek(saved):
                DEEPSEEK_DEBUG_PORT = saved
                return saved, True
    except Exception:
        pass
    for port in range(9322, 9351):
        try:
            with urlopen(f"http://{DEEPSEEK_DEBUG_HOST}:{port}/json/version", timeout=0.35):
                continue
        except Exception:
            DEEPSEEK_DEBUG_PORT = port
            return port, False
    return DEEPSEEK_DEBUG_PORT, False


def deepseek_debug_port_ready(timeout=1.0):
    try:
        with urlopen(deepseek_debug_url(), timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False



def launch_deepseek_manual_chrome(start_url=None):
    """
    Mở Chrome DeepSeek THẬT bằng subprocess + remote debugging.
    Mở Chrome thật trước rồi Selenium attach vào session thật; không dừng tay nếu session còn hợp lệ.
    """
    DEEPSEEK_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    port, reusable = _choose_deepseek_debug_port()
    if reusable and deepseek_debug_port_ready():
        print(f"✅ Reuse đúng Chrome DeepSeek của project ở cổng debug {port}.")
        return None

    if not Path(CHROME_BINARY).exists():
        print(f"❌ Không tìm thấy Chrome: {CHROME_BINARY}")
        return False

    start_url = str(start_url or DEEPSEEK_HOME).strip() or DEEPSEEK_HOME
    command = [
        CHROME_BINARY,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={DEEPSEEK_USER_DATA_DIR}",
        f"--profile-directory={PROFILE_DIRECTORY}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        start_url,
    ]

    print("\n🚀 Mở Chrome DeepSeek THẬT (chưa attach Selenium)...")
    if ACTIVE_DEEPSEEK_ACCOUNT_NAME:
        print(f"👤 DeepSeek account/profile: {ACTIVE_DEEPSEEK_ACCOUNT_NAME} [{ACTIVE_DEEPSEEK_ACCOUNT_KEY}]")
    print(f"📁 DeepSeek profile: {DEEPSEEK_USER_DATA_DIR}\\{PROFILE_DIRECTORY}")
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
        print(f"❌ Không mở được Chrome DeepSeek: {exc}")
        return False

    deadline = time.time() + 25
    while time.time() < deadline:
        if deepseek_debug_port_ready():
            try:
                DEEPSEEK_DEBUG_PORT_FILE.write_text(str(port), encoding="utf-8")
                DEEPSEEK_DEBUG_ACCOUNT_FILE.write_text(str(ACTIVE_DEEPSEEK_ACCOUNT_KEY or ""), encoding="utf-8")
            except OSError:
                pass
            print("✅ Chrome DeepSeek đã mở và remote debugging sẵn sàng.")
            return process
        sleep(0.4)

    print(f"❌ Chrome đã mở nhưng không thấy cổng remote debugging {port}.")
    print("Hãy đóng Chrome DeepSeek của project rồi chạy lại.")
    return False


def deepseek_really_needs_manual_auth(driver):
    """
    Chỉ True khi DeepSeek thật sự ở login/challenge.
    Không coi generic timeout/UI lag là logout.
    """
    if not driver_alive(driver):
        return False

    url = safe_current_url(driver).lower()
    if any(x in url for x in ("/sign_in", "/signin", "/sign-in", "/login")):
        return True

    try:
        if find_deepseek_composer(driver):
            return False
    except Exception:
        pass

    selectors = [
        ".ds-sign-in-form__main",
        ".ds-sign-in-form-wrapper",
        ".ds-auth-form-wrapper",
        ".ds-sign-up-form__main",
        "input[autocomplete='current-password']",
        "input[type='password']",
    ]
    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed():
                    return True
        except Exception:
            pass

    try:
        body = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    except Exception:
        body = ""

    markers = (
        "verify you are human",
        "checking your browser",
        "cloudflare",
        "just a moment",
        "log in",
        "sign in",
    )
    return any(m in body for m in markers) and find_deepseek_composer(driver) is None

def auto_open_project_with_retries(driver, project, attempts=4, timeout=None):
    """
    Tự vào DeepSeek/composer nhiều lần.
    - Timeout/load chậm/draft/UI chưa render: tự retry/refresh.
    - Chỉ báo MANUAL_AUTH khi thật sự thấy login/challenge.
    """
    timeout = timeout or WAIT_DEEPSEEK_READY
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            open_deepseek_project_new_chat(driver, project, timeout=timeout)
            return True, False, None
        except Exception as exc:
            last_exc = exc
            err = str(exc).strip() or type(exc).__name__
            print(f"   ⚠️ DeepSeek/composer chưa sẵn sàng {attempt}/{attempts}: {err}")

            if deepseek_really_needs_manual_auth(driver):
                print("   🔐 Phát hiện login/challenge thật sự.")
                return False, True, exc

            # Generic timeout / UI load chậm: không gọi là logout.
            try:
                target = str((project or {}).get("url") or DEEPSEEK_HOME).strip() or DEEPSEEK_HOME
                if attempt == 1:
                    sleep(0.50)
                elif attempt == 2:
                    print("   🔄 Reload lại đúng DeepSeek Home...")
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


def wait_manual_deepseek_login_before_attach():
    """
    DỪNG CỨNG trước khi Selenium attach.
    Người dùng phải tự hoàn tất login / Cloudflare và nhìn thấy ô chat rồi mới ENTER.
    """
    print("\n" + "=" * 72)
    print(" DEEPSEEK - ĐĂNG NHẬP / XÁC MINH BẰNG TAY")
    print("=" * 72)
    print("Chrome DeepSeek hiện đang chạy như Chrome bình thường.")
    print("Ở thời điểm này Selenium CHƯA điều khiển tab DeepSeek.")
    print()
    print("1) Tự đăng nhập DeepSeek NGAY TRONG CỬA SỔ CHROME NÀY.")
    print("2) Đây phải là ĐÚNG profile mà pipeline đang điều khiển:")
    print(f"   {DEEPSEEK_USER_DATA_DIR}\\{PROFILE_DIRECTORY}")
    print("3) Nếu có 'Verify you are human' / Cloudflare thì tự xác minh.")
    print("4) Khi URL không còn /sign_in và đã thấy ô 'Message DeepSeek', quay lại console.")
    print("5) Nhấn ENTER để code attach Selenium và chạy tiếp.")
    print()

    while True:
        command = input("👉 Đã vào được DeepSeek và thấy ô nhập chưa? ENTER = tiếp tục, QUIT = dừng: ").strip().upper()
        if command in {"QUIT", "EXIT", "THOAT"}:
            raise cutter.UserQuit()
        if deepseek_debug_port_ready():
            return True
        print("⚠️ Chưa thấy cổng debug của Chrome DeepSeek. Hãy kiểm tra Chrome vẫn đang mở.")


def attach_deepseek_driver():
    """Attach Selenium vào Chrome DeepSeek thật đang chạy qua remote debugging."""
    options = webdriver.ChromeOptions()

    # QUAN TRỌNG: chỉ rõ Chrome binary cả khi ATTACH.
    # Một số máy Chrome có ở Program Files nhưng không nằm trong PATH (`where chrome` không thấy).
    # Nếu thiếu dòng này Selenium Manager có thể tưởng máy chưa có Chrome và báo cài Chrome/browser.
    options.binary_location = CHROME_BINARY

    options.add_experimental_option(
        "debuggerAddress",
        f"{DEEPSEEK_DEBUG_HOST}:{DEEPSEEK_DEBUG_PORT}",
    )

    print("🔗 Đang attach Selenium vào Chrome DeepSeek đã login...")
    print(f"🌐 Chrome binary: {CHROME_BINARY}")

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:
        print(f"❌ Attach Chrome DeepSeek thất bại: {exc}")
        return None

    # Attach thành công là đủ. Composer có thể render chậm hoặc trang đang ở Home/Project
    # chưa load xong; caller sẽ tự navigate + retry. Không được biến generic timeout thành
    # "cần login thủ công".
    try:
        wait_deepseek_composer(driver, timeout=12)
        print("✅ Attach thành công. Đã thấy composer DeepSeek.")
    except Exception:
        print("✅ Attach Selenium thành công; composer chưa render ngay -> sẽ tự vào Project/retry.")

    install_deepseek_429_network_observer(driver)
    handle_deepseek_conversations_api_429(driver, quiet=True)
    return driver



def driver_alive(driver):
    if driver is None:
        return False
    try:
        driver.execute_script("return 1;")
        return True
    except Exception:
        return False


def ensure_deepseek_ready_interactive(driver):
    """
    Nếu session DeepSeek logout / Cloudflare xuất hiện giữa batch, dừng an toàn để user xử lý.
    Không tự bypass challenge.
    """
    if not driver_alive(driver):
        return False
    try:
        wait_deepseek_composer(driver, timeout=8)
        return True
    except Exception:
        print("\n⚠️ DeepSeek không còn thấy ô nhập (có thể logout/rate page/Cloudflare).")
        print("Hãy xử lý trực tiếp trong cửa sổ Chrome DeepSeek.")
        while True:
            raw = input("👉 Khi thấy lại ô nhập DeepSeek, nhấn ENTER; gõ QUIT để dừng: ").strip().upper()
            if raw in {"QUIT", "EXIT", "THOAT"}:
                raise cutter.UserQuit()
            try:
                wait_deepseek_composer(driver, timeout=10)
                print("✅ DeepSeek đã sẵn sàng lại.")
                return True
            except Exception:
                print("⚠️ Vẫn chưa thấy ô nhập DeepSeek.")




def _wait_deepseek_debug_port_closed(timeout=None):
    """Đợi Chrome DeepSeek thật sự đóng để lần mở sau tạo process sạch/RAM sạch."""
    timeout = DEEPSEEK_RESTART_CLOSE_WAIT if timeout is None else timeout
    deadline = time.time() + max(1, float(timeout))
    while time.time() < deadline:
        if not deepseek_debug_port_ready(timeout=0.35):
            return True
        sleep(0.35)
    return not deepseek_debug_port_ready(timeout=0.35)


def close_deepseek_browser_hard(driver=None, process=None):
    """
    Đóng HẲN Chrome DeepSeek đang dùng remote-debugging để giải phóng RAM.
    Không xóa chrome_profiles nên login/cookie vẫn được giữ.
    """
    print("🧹 Đang đóng hẳn Chrome DeepSeek để giải phóng RAM...")

    # Ưu tiên CDP Browser.close vì driver đang attach vào Chrome thật.
    if driver is not None:
        try:
            driver.execute_cdp_cmd("Browser.close", {})
        except Exception:
            try:
                driver.quit()
            except Exception:
                pass

    if _wait_deepseek_debug_port_closed(timeout=8):
        try:
            DEEPSEEK_DEBUG_PORT_FILE.unlink(missing_ok=True)
            DEEPSEEK_DEBUG_ACCOUNT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        print("✅ Chrome DeepSeek đã đóng sạch.")
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

    if _wait_deepseek_debug_port_closed(timeout=6):
        try:
            DEEPSEEK_DEBUG_PORT_FILE.unlink(missing_ok=True)
            DEEPSEEK_DEBUG_ACCOUNT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        print("✅ Chrome DeepSeek đã đóng sạch sau fallback.")
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

    closed = _wait_deepseek_debug_port_closed(timeout=6)
    if closed:
        try:
            DEEPSEEK_DEBUG_PORT_FILE.unlink(missing_ok=True)
            DEEPSEEK_DEBUG_ACCOUNT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    print("✅ Chrome DeepSeek đã đóng sạch." if closed else "⚠️ Chưa xác nhận Chrome DeepSeek đã đóng hoàn toàn.")
    return closed



def open_deepseek_rotation_target(account, project, old_driver=None, old_process=None):
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
        and str(ACTIVE_DEEPSEEK_ACCOUNT_KEY or "").strip() == target_key
    )

    # FAST PATH:
    # Nếu video kế tiếp vẫn dùng cùng account/profile thì GIỮ NGUYÊN Chrome thật.
    # Chỉ đưa tab về đúng DeepSeek Home + composer sạch, không kill/open/attach lại.
    if same_profile:
        set_active_deepseek_account(account)
        project = dict(project or {})
        project.update({
            "account_key": account.get("key", ""),
            "account_name": account.get("name", account.get("key", "")),
            "profile_dir": str(DEEPSEEK_USER_DATA_DIR),
        })
        try:
            current_url = safe_current_url(old_driver)
            composer = find_deepseek_composer(old_driver)

            if (
                composer
                and deepseek_project_url_matches(current_url, project)
                and not deepseek_is_existing_conversation_url(current_url)
            ):
                # Chỉ reuse trực tiếp nếu đang ở Project ROOT, tuyệt đối không reuse /c/... chat cũ.
                residual = _loose_compare_text(get_deepseek_composer_text(old_driver, composer))
                if residual:
                    print(f"🧹 Same profile: xóa draft còn lại ({len(residual)} chars)...")
                    clear_deepseek_composer(composer)
                    sleep(0.18)

                composer = find_deepseek_composer(old_driver)
                residual = _loose_compare_text(get_deepseek_composer_text(old_driver, composer)) if composer else "x"
                if composer and not residual:
                    print("⚡ FAST DEEPSEEK: giữ Chrome/profile, đang ở DeepSeek Home.")
                    return old_driver, old_process, project

            if deepseek_is_existing_conversation_url(current_url):
                print("🆕 FAST DEEPSEEK: giữ Chrome nhưng KHÔNG reuse chat cũ.")

            # Không đúng DeepSeek Home hoặc composer chưa có: navigation/retry nhẹ, vẫn không restart.
            ok, manual_auth, exc = auto_open_project_with_retries(
                old_driver,
                project,
                attempts=3,
                timeout=min(WAIT_DEEPSEEK_READY, 15),
            )
            if ok:
                print("⚡ FAST DEEPSEEK: reuse Chrome hiện tại, không restart.")
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
        close_deepseek_browser_hard(old_driver, old_process)
        sleep(0.7)

    set_active_deepseek_account(account)
    project = dict(project or {})
    project.update({
        "account_key": account.get("key", ""),
        "account_name": account.get("name", account.get("key", "")),
        "profile_dir": str(DEEPSEEK_USER_DATA_DIR),
    })

    print("\n" + "=" * 72)
    print("🔄 CHUYỂN DEEPSEEK PROFILE")
    print(f"👤 Account/profile: {account.get('name')} [{account.get('key')}]")
    print(f"📁 Chrome data: {DEEPSEEK_USER_DATA_DIR}")
    print("🌐 Mode: DeepSeek Direct" if project.get("direct") else f"📂 Project: {project.get('name')}")
    print("=" * 72)

    start_url = project.get("url") or DEEPSEEK_HOME

    # Tối đa 2 vòng browser: vòng đầu bình thường, vòng 2 là restart recovery.
    for browser_try in range(1, 3):
        new_process = launch_deepseek_manual_chrome(start_url)
        if new_process is False:
            raise RuntimeError(f"Không mở được Chrome DeepSeek profile {account.get('key')}")

        new_driver = attach_deepseek_driver()
        if new_driver:
            ok, manual_auth, exc = auto_open_project_with_retries(
                new_driver, project, attempts=4, timeout=WAIT_DEEPSEEK_READY
            )
            if ok:
                print("✅ Profile đã login; DeepSeek Direct sẵn sàng.")
                return new_driver, new_process, project

            if manual_auth:
                print(f"⚠️ Profile '{account.get('name')}' thật sự cần login/xác minh.")
                wait_manual_deepseek_login_before_attach()
                # Browser vẫn đang mở; attach có thể đang tồn tại. Dùng driver hiện tại trước.
                if not driver_alive(new_driver):
                    new_driver = attach_deepseek_driver()
                ok2, _, exc2 = auto_open_project_with_retries(
                    new_driver, project, attempts=3, timeout=WAIT_DEEPSEEK_READY
                )
                if ok2:
                    print("✅ Login/xác minh xong; profile đã sẵn sàng.")
                    return new_driver, new_process, project
                raise RuntimeError(
                    f"Đã login nhưng DeepSeek Direct vẫn chưa sẵn sàng: "
                    f"{str(exc2).strip() or type(exc2).__name__}"
                )

            err = str(exc).strip() if exc else ""
            print(
                f"⚠️ Không phải lỗi login; DeepSeek UI chưa sẵn sàng sau auto retry"
                f"{(': ' + err) if err else ''}"
            )

            # Recovery browser tự động đúng 1 lần.
            if browser_try == 1:
                print("♻️ Tự restart Chrome DeepSeek 1 lần rồi thử lại, KHÔNG cần ENTER.")
                close_deepseek_browser_hard(new_driver, new_process)
                sleep(1.0)
                continue

            raise RuntimeError(
                f"DeepSeek composer không sẵn sàng sau auto recovery; "
                "không phát hiện login/Cloudflare."
            )

        # Attach thất bại thật sự: restart 1 vòng, không hỏi tay ngay.
        if browser_try == 1:
            print("♻️ Attach chưa được -> tự restart Chrome 1 lần.")
            close_deepseek_browser_hard(None, new_process)
            sleep(1.0)
            continue

        raise RuntimeError(f"Không attach được DeepSeek profile {account.get('key')}")

    raise RuntimeError(f"Không mở được DeepSeek profile {account.get('key')}")


def restart_deepseek_browser(deepseek_driver, deepseek_process, deepseek_project):
    """
    Restart định kỳ Chrome DeepSeek.
    Generic timeout/UI load chậm được tự retry; chỉ hỏi tay nếu thấy login/challenge thật.
    """
    project_name = (deepseek_project or {}).get("name") or (deepseek_project or {}).get("key") or "Project"
    start_url = (deepseek_project or {}).get("url") or DEEPSEEK_HOME

    print("\n" + "=" * 72)
    print(f"♻️ AUTO RESTART DEEPSEEK - đã xử lý {DEEPSEEK_RESTART_EVERY} video")
    print(f"📁 Giữ nguyên Project: {project_name}")
    print("=" * 72)

    close_deepseek_browser_hard(deepseek_driver, deepseek_process)
    sleep(1.0)

    for browser_try in range(1, 3):
        new_process = launch_deepseek_manual_chrome(start_url)
        if new_process is False:
            raise RuntimeError("Không mở lại được Chrome DeepSeek sau periodic restart")

        new_driver = attach_deepseek_driver()
        if new_driver:
            ok, manual_auth, exc = auto_open_project_with_retries(
                new_driver, deepseek_project, attempts=4, timeout=WAIT_DEEPSEEK_READY
            )
            if ok:
                print("✅ Restart DeepSeek xong, đã quay lại đúng DeepSeek Home.")
                return new_driver, new_process

            if manual_auth:
                print("⚠️ Phát hiện login/challenge thật sự; mới cần thao tác tay.")
                wait_manual_deepseek_login_before_attach()
                if not driver_alive(new_driver):
                    new_driver = attach_deepseek_driver()
                ok2, _, exc2 = auto_open_project_with_retries(
                    new_driver, deepseek_project, attempts=3, timeout=WAIT_DEEPSEEK_READY
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
                close_deepseek_browser_hard(new_driver, new_process)
                sleep(1.0)
                continue

            raise RuntimeError(
                "DeepSeek/composer không sẵn sàng sau auto recovery; "
                "không phát hiện login/Cloudflare."
            )

        if browser_try == 1:
            print("♻️ Attach chưa được -> tự restart thêm 1 lần.")
            close_deepseek_browser_hard(None, new_process)
            sleep(1.0)
            continue

        raise RuntimeError("Không attach được DeepSeek sau periodic restart")

    raise RuntimeError("Restart DeepSeek thất bại")


def prepare_browsers(deepseek_project=None):
    """Khởi tạo YouTube + Chrome DeepSeek thật và attach TỰ ĐỘNG."""
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)

    youtube_driver = create_youtube_driver()
    if not youtube_driver:
        return None, None, None

    deepseek_start_url = (deepseek_project or {}).get("url") or DEEPSEEK_HOME
    deepseek_process = launch_deepseek_manual_chrome(deepseek_start_url)
    if deepseek_process is False:
        try:
            youtube_driver.quit()
        except Exception:
            pass
        return None, None, None

    # Không dừng ENTER mặc định nữa.
    deepseek_driver = attach_deepseek_driver()
    if not deepseek_driver:
        try:
            youtube_driver.quit()
        except Exception:
            pass
        return None, None, None

    if deepseek_project:
        ok, manual_auth, _ = auto_open_project_with_retries(
            deepseek_driver, deepseek_project, attempts=4, timeout=WAIT_DEEPSEEK_READY
        )
        if not ok and manual_auth:
            print("⚠️ Chỉ vì phát hiện login/challenge thật sự nên mới cần thao tác tay.")
            wait_manual_deepseek_login_before_attach()
            ok, _, _ = auto_open_project_with_retries(
                deepseek_driver, deepseek_project, attempts=3, timeout=WAIT_DEEPSEEK_READY
            )
        if not ok:
            try:
                youtube_driver.quit()
            except Exception:
                pass
            return None, None, None

    return youtube_driver, deepseek_driver, deepseek_process


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
# DEEPSEEK AUTOMATION
# ============================================================

def find_deepseek_composer(driver):
    """
    DeepSeek UI confirmed from user's screenshots.

    Strong signal:
      textarea placeholder = "Message DeepSeek"
    """
    selectors = [
        "textarea[placeholder='Message DeepSeek']",
        "textarea[placeholder*='Message DeepSeek' i]",
        "textarea#chat-input",
        "#chat-input",
        "textarea",
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']",
    ]

    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except Exception:
                    continue
        except Exception:
            continue
    return None

def wait_deepseek_composer(driver, timeout=WAIT_DEEPSEEK_READY):
    return WebDriverWait(driver, timeout).until(lambda d: find_deepseek_composer(d))


def set_clipboard_text_windows(text):
    """Dùng PowerShell Set-Clipboard qua file tạm để không vỡ khi prompt rất dài."""
    clip_file = APP_DIR / "_clipboard_prompt.txt"
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


def clear_deepseek_composer(composer):
    """
    Xóa draft DeepSeek thật sự, kể cả khi Project tự restore draft cũ.

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


def get_deepseek_composer_text(driver, composer=None):
    """
    Đọc text composer hiện tại một cách chống stale/re-render.

    DeepSeek có thể thay node ProseMirror ngay sau Ctrl+V, vì vậy không được chỉ
    đọc element `composer` cũ. Hàm này luôn thử reacquire composer mới và quét
    các editor visible trước khi kết luận rỗng.
    """
    candidates = []

    if composer is not None:
        candidates.append(composer)

    try:
        fresh = find_deepseek_composer(driver)
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

    KHÔNG phát sinh sự kiện clipboard/paste nên DeepSeek không biến transcript dài
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
    actual = get_deepseek_composer_text(driver, composer)
    exp_n = normalize_compare_text(expected_text)
    act_n = normalize_compare_text(actual)

    # DOM DeepSeek có thể thêm vài ký tự/node nên chỉ cần không bị hụt đáng kể.
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
        f"🧪 Kiểm tra ô DeepSeek: {len(act_n):,}/{len(exp_n):,} ký tự "
        f"| mốc cuối={last_ts or 'N/A'}:{'OK' if last_ts_ok else 'CHƯA THẤY'} "
        f"| lời cuối={'OK' if tail_words_ok else 'CHƯA THẤY'}"
    )

    # Timestamp cuối + độ dài là hai tín hiệu chính. Lời cuối là lớp kiểm tra bổ sung.
    return length_ok and last_ts_ok and tail_words_ok


def paste_prompt_into_deepseek(driver, composer, text):
    """
    Tên hàm giữ nguyên để phần còn lại của pipeline không phải đổi.

    Bản mới TUYỆT ĐỐI KHÔNG Ctrl+V prompt dài, vì DeepSeek có thể biến paste
    thành file/thẻ "Show in text field". Thay vào đó nhập trực tiếp vào composer.
    """
    clear_deepseek_composer(composer)

    print("⌨️ Đang nhập trực tiếp prompt + transcript vào ô DeepSeek (không dùng clipboard)...")

    try:
        insert_prompt_with_cdp(driver, composer, text)
    except Exception as exc:
        print(f"⚠️ CDP Input.insertText lỗi: {exc}")
        clear_deepseek_composer(composer)
        if not insert_prompt_with_js(driver, composer, text):
            print("❌ Không nhập được prompt bằng CDP hoặc JS fallback.")
            return False

    if verify_prompt_in_composer(driver, composer, text):
        print("✅ Toàn bộ prompt + transcript đã nằm trong ô DeepSeek.")
        return True

    # Thử lại một lần bằng JS nếu lần CDP đầu bị hụt text.
    print("⚠️ Nội dung trong ô chat chưa đủ. Đang nhập lại bằng fallback...")
    clear_deepseek_composer(composer)

    if not insert_prompt_with_js(driver, composer, text):
        return False

    if verify_prompt_in_composer(driver, composer, text):
        print("✅ Toàn bộ prompt + transcript đã nằm trong ô DeepSeek sau fallback.")
        return True

    print("❌ Kiểm tra thất bại: transcript chưa vào đầy đủ nên KHÔNG bấm Send.")
    return False



def find_deepthink_button(driver):
    """
    EXACT selector based on user's real DeepSeek HTML:

      <div tabindex="0"
           aria-pressed="false|true"
           class="... ds-toggle-button ds-toggle-button--m">
          ...
          <span>DeepThink</span>
      </div>

    The control is a DIV, not a button and not role=button.
    """
    selectors = [
        "div.ds-toggle-button[aria-pressed]",
        "div.ds-toggle-button.ds-toggle-button--m[aria-pressed]",
    ]

    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if not el.is_displayed():
                        continue

                    # Confirm this toggle is specifically DeepThink, not another toggle.
                    txt = (el.text or "").strip().lower()
                    if "deepthink" in txt:
                        return el

                    # Fallback: inspect child span text.
                    spans = el.find_elements(By.CSS_SELECTOR, "span")
                    for span in spans:
                        if "deepthink" in (span.text or "").strip().lower():
                            return el
                except Exception:
                    continue
        except Exception:
            continue

    # Strong XPath fallback: find the DeepThink span, then parent toggle div.
    xpaths = [
        "//span[normalize-space()='DeepThink']/ancestor::div[contains(@class,'ds-toggle-button')][1]",
        "//*[normalize-space()='DeepThink']/ancestor::div[contains(@class,'ds-toggle-button')][1]",
    ]
    for xpath in xpaths:
        try:
            for el in driver.find_elements(By.XPATH, xpath):
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
        except Exception:
            continue

    return None


def _deepthink_state_hint(driver, button):
    """
    Confirmed from user's REAL DeepSeek HTML:

    OFF:
      aria-pressed="false"
      class includes:
        ds-toggle-button ds-toggle-button--m

    ON:
      aria-pressed="true"
      class additionally includes:
        ds-toggle-button--selected

    aria-pressed is primary truth.
    ds-toggle-button--selected is secondary cross-check.
    """
    if button is None:
        return "missing"

    try:
        pressed = (button.get_attribute("aria-pressed") or "").strip().lower()
        cls = (button.get_attribute("class") or "").strip().lower()
        selected = "ds-toggle-button--selected" in cls

        if pressed == "true":
            # This is the confirmed ON state from the user's HTML.
            return "on"

        if pressed == "false":
            return "off"

        # Fallback only if DeepSeek temporarily omits aria-pressed during React rerender.
        if selected:
            return "on"
    except Exception:
        pass

    return "unknown"


def _deepthink_debug_state(button):
    if button is None:
        return "missing"
    try:
        pressed = button.get_attribute("aria-pressed") or ""
        cls = button.get_attribute("class") or ""
        selected = "ds-toggle-button--selected" in cls
        return (
            f"aria-pressed={pressed!r} | "
            f"selected-class={'YES' if selected else 'NO'}"
        )
    except Exception:
        return "unreadable"

def enable_deepthink_for_analysis(driver):
    """
    Always ensure DeepThink is ON before analysis.

    Exact behavior:
      - find div.ds-toggle-button containing DeepThink
      - aria-pressed=true  -> already ON, do nothing
      - aria-pressed=false -> click once, then WAIT until true
      - if state never becomes true -> fail-safe, do not Send
    """
    if not DEEPSEEK_FORCE_DEEPTHINK_EACH_ANALYSIS:
        return True

    try:
        button = WebDriverWait(driver, 12).until(
            lambda d: find_deepthink_button(d)
        )
    except Exception:
        button = find_deepthink_button(driver)

    if button is None:
        print("❌ Không tìm thấy DeepThink toggle div.ds-toggle-button.")
        print("⛔ KHÔNG gửi analysis bằng Instant.")
        return False

    before = _deepthink_state_hint(driver, button)

    if before == "on":
        print(
            "🧠 DeepThink: đã BẬT sẵn | "
            + _deepthink_debug_state(button)
        )
        return True

    if before != "off":
        print(f"⚠️ DeepThink state không rõ: {before}. Thử đọc lại aria-pressed...")
        sleep(0.25)
        button = find_deepthink_button(driver)
        before = _deepthink_state_hint(driver, button)

    if before == "on":
        print("🧠 DeepThink: đã BẬT sẵn.")
        return True

    if before != "off":
        print("❌ Không xác định được aria-pressed của DeepThink. KHÔNG Send.")
        return False

    print("🧠 DeepThink: aria-pressed=false -> click BẬT...")

    # Native click first.
    clicked = False
    try:
        button.click()
        clicked = True
    except Exception as exc:
        print(f"⚠️ Native click DeepThink chưa được: {exc}")

    # JS click fallback if needed.
    if not clicked:
        try:
            driver.execute_script("arguments[0].click();", button)
            clicked = True
        except Exception as exc:
            print(f"❌ JS click DeepThink cũng lỗi: {exc}")
            return False

    # React may replace the node after click. Always reacquire.
    deadline = time.time() + 5.0
    last_state = "unknown"

    while time.time() < deadline:
        sleep(0.12)
        fresh = find_deepthink_button(driver)
        if fresh is None:
            continue

        last_state = _deepthink_state_hint(driver, fresh)

        if last_state == "on":
            fresh = find_deepthink_button(driver)
            print(
                "✅ DeepThink: ON | "
                + _deepthink_debug_state(fresh)
            )
            return True

    print(
        f"❌ DeepThink không chuyển sang aria-pressed=true sau click "
        f"(state cuối={last_state})."
    )
    print("⛔ KHÔNG gửi analysis bằng Instant.")
    return False


def _deepseek_composer_rect(driver):
    composer = find_deepseek_composer(driver)
    if composer is None:
        return None
    try:
        return driver.execute_script(
            """
            const r = arguments[0].getBoundingClientRect();
            return {left:r.left, top:r.top, right:r.right, bottom:r.bottom,
                    width:r.width, height:r.height};
            """,
            composer,
        )
    except Exception:
        return None


def _deepseek_near_composer_buttons(driver):
    """
    Screenshot layout:
      [DeepThink] [Search] ......... [paperclip] [send]

    Some DeepSeek builds use icon-only controls without useful aria-label.
    """
    rect = _deepseek_composer_rect(driver)
    if not rect:
        return []

    try:
        candidates = driver.find_elements(
            By.CSS_SELECTOR,
            "button, [role='button'], .ds-icon-button, .ds-button"
        )
    except Exception:
        return []

    items = []
    for el in candidates:
        try:
            if not el.is_displayed() or not el.is_enabled():
                continue
            if (el.get_attribute("aria-disabled") or "").lower() == "true":
                continue

            r = driver.execute_script(
                """
                const x = arguments[0].getBoundingClientRect();
                return {left:x.left, top:x.top, right:x.right, bottom:x.bottom,
                        width:x.width, height:x.height};
                """,
                el,
            )
            if not r or r["width"] <= 0 or r["height"] <= 0:
                continue

            cx = (r["left"] + r["right"]) / 2
            cy = (r["top"] + r["bottom"]) / 2

            if not (rect["left"] - 25 <= cx <= rect["right"] + 25):
                continue
            if not (rect["bottom"] - 100 <= cy <= rect["bottom"] + 30):
                continue

            label = " ".join([
                el.text or "",
                el.get_attribute("aria-label") or "",
                el.get_attribute("title") or "",
                el.get_attribute("data-tooltip") or "",
            ]).strip().lower()

            items.append((cx, cy, label, el))
        except Exception:
            continue

    items.sort(key=lambda x: (x[0], x[1]))
    return items


def _deepseek_find_send_by_geometry(driver):
    items = _deepseek_near_composer_buttons(driver)
    usable = []
    for cx, cy, label, el in items:
        if any(k in label for k in ("deepthink", "search", "upload", "attach", "file")):
            continue
        usable.append((cx, cy, label, el))

    if not usable:
        return None
    return max(usable, key=lambda x: x[0])[3]


def _deepseek_find_attach_by_geometry(driver):
    items = _deepseek_near_composer_buttons(driver)
    if not items:
        return None

    for cx, cy, label, el in reversed(items):
        if any(k in label for k in ("attach", "upload", "paperclip", "file")):
            return el

    usable = []
    for cx, cy, label, el in items:
        if any(k in label for k in ("deepthink", "search")):
            continue
        usable.append((cx, cy, label, el))

    if len(usable) >= 2:
        usable.sort(key=lambda x: x[0])
        return usable[-2][3]

    return None


def find_send_button(driver):
    """
    DeepSeek Send:
      semantic selectors -> icon fallback -> geometry fallback.
    Screenshot confirms Send is the rightmost control at composer bottom-right.
    """
    selectors = [
        "button[aria-label*='Send' i]:not([disabled])",
        "[role='button'][aria-label*='Send' i]",
        "button[title*='Send' i]:not([disabled])",
        ".ds-button.ds-button--circle",
    ]

    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if not el.is_displayed() or not el.is_enabled():
                        continue
                    if (el.get_attribute("aria-disabled") or "").lower() == "true":
                        continue

                    label = " ".join([
                        el.text or "",
                        el.get_attribute("aria-label") or "",
                        el.get_attribute("title") or "",
                    ]).lower()

                    if any(x in label for x in ("upload", "attach", "file", "search", "deepthink")):
                        continue
                    return el
                except Exception:
                    continue
        except Exception:
            continue

    return _deepseek_find_send_by_geometry(driver)

def deepseek_submission_started(driver, before_text=""):
    """Xác nhận prompt đã thực sự được submit, không chỉ click giả."""
    # Khi DeepSeek bắt đầu trả lời thường xuất hiện nút Stop.
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
        composer = find_deepseek_composer(driver)
        if composer:
            now_text = normalize_compare_text(
                get_deepseek_composer_text(driver, composer)
            )
            old_text = normalize_compare_text(before_text)
            if old_text and len(now_text) <= max(8, int(len(old_text) * 0.05)):
                return True
    except Exception:
        pass

    return False


def wait_deepseek_submission(driver, before_text, timeout=8):
    end_time = time.time() + timeout
    while time.time() < end_time:
        if deepseek_submission_started(driver, before_text):
            return True
        sleep(0.20)
    return False


def click_send(driver):
    """Send on DeepSeek and confirm composer clears / conversation starts."""
    composer = find_deepseek_composer(driver)
    before = get_deepseek_composer_text(driver, composer) if composer else ""

    button = None
    try:
        button = WebDriverWait(driver, 15).until(lambda d: find_send_button(d))
    except Exception:
        button = find_send_button(driver)

    if button is not None:
        for mode in ("native", "js"):
            try:
                if mode == "native":
                    button.click()
                else:
                    driver.execute_script("arguments[0].click();", button)
                deadline = time.time() + 8
                while time.time() < deadline:
                    current = get_deepseek_composer_text(driver, find_deepseek_composer(driver))
                    if before.strip() and len(current.strip()) < max(5, int(len(before.strip()) * 0.15)):
                        return True
                    if deepseek_is_existing_conversation_url(safe_current_url(driver)):
                        return True
                    sleep(0.15)
            except Exception:
                pass

    # Fallback: Enter in textarea.
    try:
        composer = find_deepseek_composer(driver)
        if composer:
            composer.click()
            composer.send_keys(Keys.ENTER)
            deadline = time.time() + 8
            while time.time() < deadline:
                current = get_deepseek_composer_text(driver, find_deepseek_composer(driver))
                if before.strip() and len(current.strip()) < max(5, int(len(before.strip()) * 0.15)):
                    return True
                if deepseek_is_existing_conversation_url(safe_current_url(driver)):
                    return True
                sleep(0.15)
    except Exception:
        pass

    return False

def get_assistant_turns(driver):
    """
    DeepSeek assistant responses.
    Prefer .ds-markdown; each assistant message contains one main markdown node.
    """
    selectors = [
        "[data-message-author-role='assistant'] .ds-markdown",
        ".ds-markdown.ds-markdown--block",
        ".ds-markdown",
    ]
    for selector in selectors:
        try:
            els = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()]
            if els:
                return els
        except Exception:
            continue
    return []

def get_turn_text(element):
    try:
        return (element.text or "").strip()
    except Exception:
        return ""




def install_deepseek_429_network_observer(driver):
    """DeepSeek port: no ChatGPT conversations-API observer."""
    return True


def scan_deepseek_conversations_429(driver):
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


def handle_deepseek_conversations_api_429(driver, quiet=False):
    """DeepSeek port: no-op compatibility hook."""
    return False


def dismiss_deepseek_too_many_requests_popup(driver, quiet=False):
    """
    Đóng ĐÚNG popup DeepSeek có heading "Too many requests" bằng nút "Got it".

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
    """
    DeepSeek: generation is active if a visible Stop control exists.
    Fallback to text/aria labels and known square/stop icons.
    """
    selectors = [
        "[aria-label*='Stop' i]",
        "[title*='Stop' i]",
        "button[aria-label*='stop' i]",
        "div[role='button'][aria-label*='stop' i]",
    ]
    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed() and el.is_enabled():
                    return True
        except Exception:
            pass

    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "button, div[role='button']"):
            try:
                if not el.is_displayed():
                    continue
                txt = " ".join([
                    el.text or "",
                    el.get_attribute("aria-label") or "",
                    el.get_attribute("title") or "",
                ]).strip().lower()
                if txt in {"stop", "stop generating", "dừng", "dừng tạo"}:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False

def wait_for_deepseek_answer(driver, previous_count):
    """
    Wait DeepSeek assistant answer.
    Exit when a new .ds-markdown exists, generation stopped, text stable >= 1.2s.
    """
    deadline = time.time() + WAIT_DEEPSEEK_RESPONSE
    last_turn = None
    last_text = ""
    stable_since = None

    while time.time() < deadline:
        turns = get_assistant_turns(driver)
        if len(turns) > previous_count:
            last_turn = turns[-1]
            try:
                value = (last_turn.text or last_turn.get_attribute("innerText") or "").strip()
            except Exception:
                value = ""

            if value:
                if value != last_text:
                    last_text = value
                    stable_since = time.time()
                elif (
                    not generation_in_progress(driver)
                    and stable_since is not None
                    and time.time() - stable_since >= 1.2
                ):
                    return last_turn, value

        sleep(0.20)

    if last_turn is not None and last_text:
        return last_turn, last_text
    raise TimeoutException("DeepSeek không trả lời trong thời gian chờ.")

def click_copy_last_answer(driver, last_turn):
    """
    DeepSeek: DOM text is reliable enough for this pipeline.
    Try copy button only as optional enhancement; otherwise return DOM text.
    """
    if last_turn is None:
        return ""

    try:
        dom_text = (last_turn.text or last_turn.get_attribute("innerText") or "").strip()
    except Exception:
        dom_text = ""

    if not CLICK_DEEPSEEK_COPY:
        return dom_text

    try:
        container = last_turn.find_element(
            By.XPATH,
            "./ancestor::*[@data-virtual-list-item-key or @data-message-author-role='assistant'][1]"
        )
    except Exception:
        container = None

    if container is not None:
        try:
            buttons = container.find_elements(By.CSS_SELECTOR, "button, div[role='button']")
            for b in buttons:
                try:
                    label = " ".join([
                        b.text or "",
                        b.get_attribute("aria-label") or "",
                        b.get_attribute("title") or "",
                        b.get_attribute("data-tooltip") or "",
                    ]).strip().lower()
                    if "copy" in label or "复制" in label:
                        b.click()
                        sleep(0.25)
                        clip = read_clipboard_text_windows()
                        if clip and len(clip.strip()) >= max(10, int(len(dom_text) * 0.6)):
                            return clip.strip()
                except Exception:
                    continue
        except Exception:
            pass

    return dom_text

def _deepseek_body_text(driver):
    try:
        return driver.execute_script(
            "return (document.body && document.body.innerText) ? document.body.innerText : '';"
        ) or ""
    except Exception:
        try:
            return driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return ""


def deepseek_has_upload_error(driver):
    try:
        body = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    except Exception:
        return False

    markers = (
        "upload failed",
        "failed to upload",
        "file upload failed",
        "unable to upload",
        "unsupported file",
        "file too large",
        "上传失败",
        "文件上传失败",
    )
    return any(m in body for m in markers)

def pasted_text_attachment_count(driver):
    """
    Đếm pasted-text cards của DeepSeek.
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


def _visible_marker_in_deepseek(driver, marker):
    if not marker:
        return False
    try:
        body = _deepseek_body_text(driver)
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
      3) Ctrl+V vào DeepSeek;
      4) DeepSeek tự biến big-paste thành pasted-text attachment;
      5) chỉ trả True khi xác nhận card mới xuất hiện / marker đã chuyển ra khỏi composer.

    Nếu DeepSeek giữ nguyên text dài trong composer thay vì tạo attachment thì coi là FAIL,
    tuyệt đối không sang bước tiếp theo để tránh trộn PROMPT + TRANSCRIPT.
    """
    file_path = Path(file_path).resolve()
    if not file_path.exists() or file_path.stat().st_size <= 0:
        print(f"❌ {step_label}: file không tồn tại/rỗng: {file_path}")
        return False

    payload, marker, end_marker = build_paste_attachment_payload(file_path, kind)

    for attempt in range(1, DEEPSEEK_PASTE_RETRIES + 1):
        before_count = pasted_text_attachment_count(driver)
        before_body = _deepseek_body_text(driver)

        # Composer phải không có một khối text dài trước khi paste attachment tiếp theo.
        existing_inline = normalize_compare_text(get_deepseek_composer_text(driver, composer))
        if len(existing_inline) > 50:
            print(f"⚠️ {step_label}: composer còn {len(existing_inline):,} ký tự inline; đang dọn trước khi paste.")
            clear_deepseek_composer(composer)
            sleep(0.3)

        current_payload = payload
        # Nếu lần đầu DeepSeek không tự convert prompt ngắn thành attachment,
        # retry bằng padding chỉ gồm xuống dòng SAU END marker. Nội dung quy tắc không đổi.
        if attempt > 1 and len(current_payload) < 14000:
            current_payload = current_payload + ("\n" * (14000 - len(current_payload)))

        print(
            f"📋 {step_label}: COPY/PASTE {file_path.name} "
            f"({len(payload):,} ký tự, lần {attempt}/{DEEPSEEK_PASTE_RETRIES})..."
        )

        if not _paste_clipboard_once(composer, current_payload):
            print(f"⚠️ {step_label}: Ctrl+V thất bại.")
            continue

        deadline = time.time() + timeout
        stable_since = None
        saw_inline_payload = False

        while time.time() < deadline:
            if deepseek_has_upload_error(driver):
                print(f"❌ {step_label}: DeepSeek hiện toast lỗi upload; KHÔNG đi tiếp.")
                return False

            count_now = pasted_text_attachment_count(driver)
            body_now = _deepseek_body_text(driver)
            composer_text = normalize_compare_text(get_deepseek_composer_text(driver, composer))

            count_increased = count_now > before_count
            marker_newly_visible = marker in body_now and marker not in before_body
            marker_left_composer = marker not in composer_text
            inline_large = len(composer_text) >= int(len(normalize_compare_text(payload)) * 0.70)
            saw_inline_payload = saw_inline_payload or inline_large

            # Tín hiệu mạnh nhất: card count tăng.
            # Fallback: marker mới xuất hiện ngoài composer và composer đã được DeepSeek clear.
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
        composer_text = normalize_compare_text(get_deepseek_composer_text(driver, composer))
        if saw_inline_payload or len(composer_text) > 500:
            print(
                f"⚠️ {step_label}: DeepSeek chưa convert thành attachment; "
                f"text vẫn nằm inline ({len(composer_text):,} ký tự)."
            )
            clear_deepseek_composer(composer)
            sleep(0.5)
        else:
            print(f"⚠️ {step_label}: chưa xác nhận được pasted-text card.")

    print(f"❌ {step_label}: thất bại sau {DEEPSEEK_PASTE_RETRIES} lần. Pipeline DỪNG.")
    return False


def two_paste_attachments_still_present(driver, prompt_marker, transcript_marker, min_cards=2):
    """Chốt cuối trước Send: phải còn đủ hai attachment riêng."""
    body = _deepseek_body_text(driver)
    cards = pasted_text_attachment_count(driver)
    markers_ok = prompt_marker in body and transcript_marker in body
    # Nếu UI ẩn marker trong card sau khi render, 2 card rõ ràng vẫn là tín hiệu đủ mạnh.
    return markers_ok or cards >= min_cards


def set_short_instruction(driver, composer, text):
    """Chỉ nhập câu lệnh ngắn; prompt/transcript đều nằm trong 2 file riêng."""
    clear_deepseek_composer(composer)
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

    actual = normalize_compare_text(get_deepseek_composer_text(driver, composer))
    expected = normalize_compare_text(text)
    ok = expected in actual or actual in expected
    print(
        f"🧪 BƯỚC 5/5 - kiểm tra câu lệnh ngắn: "
        f"{'OK' if ok else 'CHƯA ĐÚNG'} ({len(actual)}/{len(expected)} ký tự)"
    )
    return ok


def ask_deepseek(driver, prompt_path, transcript_path):
    """
    Pipeline DeepSeek: 2 PASTED-TEXT ATTACHMENT TÁCH BIỆT, xác nhận từng bước.

    BƯỚC 3: copy/paste PROMPT.txt -> DeepSeek tự tạo pasted-text card -> verify.
    BƯỚC 4: copy/paste TRANSCRIPT.txt -> card RIÊNG -> verify.
    BƯỚC 5: nhập câu lệnh ngắn -> verify -> Send.

    Không dùng input[type=file], nên không còn lỗi `Unable to upload *.txt` của uploader.
    """
    if NEW_CHAT_EACH_VIDEO:
        driver.get(DEEPSEEK_HOME)

    try:
        composer = wait_deepseek_composer(driver)
    except Exception as exc:
        print(f"❌ Không thấy ô nhập DeepSeek: {exc}")
        return None

    previous_count = len(get_assistant_turns(driver))

    print("\n" + "=" * 72)
    print("DEEPSEEK - 2 PASTED-TEXT ATTACHMENTS, XÁC NHẬN TỪNG BƯỚC")
    print("=" * 72)

    # Dọn text inline cũ. New chat nên không có attachment cũ.
    clear_deepseek_composer(composer)
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
    if cards_after_prompt <= base_cards and not _visible_marker_in_deepseek(driver, prompt_marker):
        print("⛔ DỪNG: không xác nhận được PROMPT card sau bước 3.")
        return None

    # 4) TRANSCRIPT pasted attachment riêng.
    if not paste_text_attachment_from_file(
        driver, composer, transcript_path, "TRANSCRIPT", "BƯỚC 4/5 - TRANSCRIPT PASTE"
    ):
        print("⛔ DỪNG: TRANSCRIPT chưa thành attachment riêng, KHÔNG Send.")
        return None

    cards_after_transcript = pasted_text_attachment_count(driver)
    prompt_ok = _visible_marker_in_deepseek(driver, prompt_marker) or cards_after_prompt > base_cards
    transcript_ok = _visible_marker_in_deepseek(driver, transcript_marker) or cards_after_transcript > cards_after_prompt

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
    composer = wait_deepseek_composer(driver, timeout=30)
    if not set_short_instruction(driver, composer, instruction):
        print("⛔ DỪNG: câu lệnh ngắn chưa vào đúng, KHÔNG Send.")
        return None

    # Chốt cuối ngay trước Send.
    if not two_paste_attachments_still_present(
        driver, prompt_marker, transcript_marker, min_cards=base_cards + 2
    ):
        print("⛔ DỪNG: 2 pasted-text attachment không còn đủ ngay trước Send.")
        return None

    if deepseek_has_upload_error(driver):
        print("⛔ DỪNG: UI đang có lỗi upload, KHÔNG Send.")
        return None

    if not click_send(driver):
        print("❌ Không gửi được prompt + 2 pasted-text attachment.")
        return None

    print("⌛ Đã gửi 2 pasted-text attachment. Đang đợi DeepSeek phân tích transcript...")
    last_turn, dom_text = wait_for_deepseek_answer(driver, previous_count)

    copied = click_copy_last_answer(driver, last_turn)
    answer = (copied or dom_text or "").strip()

    print("✅ Đã nhận kết quả DeepSeek.")
    return answer




# ============================================================
# DEEPSEEK SMART INPUT OVERRIDES - PROMPT INLINE + TRANSCRIPT ATTACHMENT
# ============================================================

# NOTE:
# Các hàm bên dưới CỐ Ý override một số helper/ask_deepseek ở phía trên.
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
    actual = get_deepseek_composer_text(driver, composer)
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
        current = get_deepseek_composer_text(driver, composer)
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
    - Prompt dài: KHÔNG paste clipboard vì DeepSeek có thể tự biến thành
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
        print("❌ Clipboard verify thất bại. KHÔNG nhập vào DeepSeek.")
        return False, prompt_text
    print("✅ Clipboard PROMPT = đúng nội dung file.")

    # Reacquire editor mới, không dùng element stale từ trước khi New Chat render xong.
    try:
        composer = wait_deepseek_composer(driver, timeout=15)
    except Exception:
        print("❌ Không tìm thấy composer DeepSeek trước khi nhập prompt.")
        return False, prompt_text

    clear_deepseek_composer(composer)
    sleep(0.35)
    composer = find_deepseek_composer(driver) or composer
    residual = _loose_compare_text(get_deepseek_composer_text(driver, composer))
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
            "Không Ctrl+V để tránh DeepSeek biến prompt thành pasted-text attachment."
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
    composer = find_deepseek_composer(driver) or composer
    _wait_prompt_paste_stable(driver, composer, prompt_text)
    composer = find_deepseek_composer(driver) or composer

    actual_now = get_deepseek_composer_text(driver, composer)
    actual_loose = _loose_compare_text(actual_now)
    after_cards = pasted_text_attachment_count(driver)

    # Diagnostic rõ ràng cho đúng lỗi user vừa gặp: clipboard đúng nhưng composer=0.
    if not actual_loose:
        body_lower = (_deepseek_body_text(driver) or "").lower()
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
                "❌ PROMPT đã bị DeepSeek biến thành PASTED-TEXT ATTACHMENT; "
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
            f"❌ PROMPT bị DeepSeek biến thành attachment ({before_cards} → {after_cards}). "
            "KHÔNG đi tiếp."
        )
        return False, prompt_text

    if not verify_prompt_inline(driver, composer, prompt_text, verbose=True):
        print("❌ PROMPT bị thiếu/lộn cấu trúc. KHÔNG upload transcript, KHÔNG Send.")
        return False, prompt_text

    print(f"✅ BƯỚC 3/5 - PROMPT INLINE OK | mode={mode}")
    return True, prompt_text

def find_deepseek_plus_button(driver):
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


def open_deepseek_plus_menu(driver, timeout=12):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        btn = find_deepseek_plus_button(driver)
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
                print("✅ Đã mở menu + của DeepSeek.")
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
        print("⚠️ Không tìm thấy nút + của DeepSeek.")
    return False


def _upload_menu_text_visible(driver):
    """Chỉ để log/diagnostic; không phụ thuộc text này để upload."""
    try:
        body = _deepseek_body_text(driver).lower()
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
    """
    DeepSeek screenshot shows file-card filename can be truncated visually.
    Do not require the complete filename to be visible.
    """
    filename = str(filename)
    stem = Path(filename).stem
    prefix = stem[: max(8, min(18, len(stem)))]

    composer = find_deepseek_composer(driver)
    composer_text = get_deepseek_composer_text(driver, composer) if composer else ""

    # Full filename in DOM/body is strongest.
    try:
        body = _deepseek_body_text(driver)
        if filename in body and filename not in composer_text:
            return True
    except Exception:
        body = ""

    # Visible card may only show prefix + ellipsis + TXT + size.
    try:
        for el in driver.find_elements(By.XPATH, "//*[normalize-space(text())!='']"):
            try:
                if not el.is_displayed():
                    continue
                txt = (el.text or "").strip()
                if not txt:
                    continue

                # Avoid matching composer text.
                if composer is not None:
                    try:
                        inside = driver.execute_script(
                            "return arguments[0]===arguments[1] || arguments[1].contains(arguments[0]);",
                            el,
                            composer,
                        )
                        if inside:
                            continue
                    except Exception:
                        pass

                low = txt.lower()
                if filename in txt:
                    return True
                if prefix and prefix in txt and (
                    "txt" in low or "kb" in low or "mb" in low
                ):
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Attributes can keep full filename despite ellipsis in visible UI.
    for attr in ("title", "aria-label", "data-tooltip", "data-title"):
        try:
            for el in driver.find_elements(By.XPATH, f"//*[@{attr}]"):
                try:
                    if not el.is_displayed():
                        continue
                    value = el.get_attribute(attr) or ""
                    if filename in value or (prefix and prefix in value):
                        return True
                except Exception:
                    continue
        except Exception:
            pass

    # Last fallback: body contains unique prefix outside composer.
    try:
        if prefix and prefix in body and prefix not in composer_text:
            return True
    except Exception:
        pass

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


def wait_real_file_attachment(driver, filename, timeout=DEEPSEEK_UPLOAD_WAIT):
    deadline = time.time() + timeout
    stable_since = None
    while time.time() < deadline:
        if deepseek_has_upload_error(driver):
            return False, "UPLOAD_ERROR_TOAST"
        visible = attachment_filename_visible(driver, filename)
        if visible:
            stable_since = stable_since or time.time()
            # Đợi thêm để bắt lỗi upload xuất hiện chậm.
            if time.time() - stable_since >= 1.4:
                if deepseek_has_upload_error(driver):
                    return False, "UPLOAD_ERROR_TOAST"
                return True, "FILENAME_VISIBLE_STABLE"
        else:
            stable_since = None
        sleep(0.25)
    return False, "UPLOAD_VERIFY_TIMEOUT"


def _deepseek_click_attach_control(driver):
    """
    Screenshot confirms paperclip is immediately left of Send.
    """
    selectors = [
        "button[aria-label*='Upload' i]",
        "button[aria-label*='Attach' i]",
        "[role='button'][aria-label*='Upload' i]",
        "[role='button'][aria-label*='Attach' i]",
        "button[title*='Upload' i]",
        "button[title*='Attach' i]",
    ]

    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed() and el.is_enabled():
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    sleep(0.25)
                    return True
        except Exception:
            pass

    el = _deepseek_find_attach_by_geometry(driver)
    if el is not None:
        try:
            el.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el)
            except Exception:
                return False
        sleep(0.25)
        return True

    return False

def upload_transcript_via_plus(driver, transcript_path, prompt_text):
    """
    DeepSeek file upload:
      1) send_keys trực tiếp vào input[type=file] nếu đã có trong DOM;
      2) nếu chưa có, click paperclip/attach rồi tìm input lại;
      3) verify filename visible + prompt vẫn còn.
    """
    transcript_path = Path(transcript_path).resolve()
    if not transcript_path.exists() or transcript_path.stat().st_size <= 0:
        return False, "INVALID_FILE"

    print(f"📎 BƯỚC 4/5 - DEEPSEEK TRANSCRIPT UPLOAD: {transcript_path.name}")

    ok, detail = _send_file_to_any_input(driver, transcript_path)
    if not ok:
        _deepseek_click_attach_control(driver)
        ok, detail = _send_file_to_any_input(driver, transcript_path)

    if not ok:
        return False, detail or "NO_FILE_INPUT"

    print(f"📤 Đã đưa file vào DeepSeek input ({detail}). Đang chờ parse/upload...")
    ok, reason = wait_real_file_attachment(driver, transcript_path.name)
    if not ok:
        return False, reason

    composer = find_deepseek_composer(driver)
    if not composer or not verify_prompt_inline(driver, composer, prompt_text, verbose=False):
        return False, "PROMPT_LOST_AFTER_UPLOAD"

    print(f"✅ DEEPSEEK FILE ATTACHMENT OK: {transcript_path.name}")
    return True, "REAL_FILE_UPLOAD"

def paste_transcript_as_attachment(driver, transcript_path, prompt_text, timeout=DEEPSEEK_PASTE_FALLBACK_WAIT):
    """
    Fallback giống thao tác user: Ctrl+V transcript dài để DeepSeek tự chuyển thành pasted-text attachment.
    Quan trọng: KHÔNG xóa prompt inline và chỉ thành công khi card count tăng + prompt vẫn còn nguyên.
    """
    transcript_path = Path(transcript_path).resolve()
    transcript_text = transcript_path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not transcript_text:
        return False, "EMPTY_TRANSCRIPT"

    composer = find_deepseek_composer(driver)
    if not composer:
        return False, "NO_COMPOSER"
    if not verify_prompt_inline(driver, composer, prompt_text, verbose=False):
        return False, "PROMPT_NOT_READY"

    before_cards = pasted_text_attachment_count(driver)
    before_text = normalize_compare_text(get_deepseek_composer_text(driver, composer))
    if not set_clipboard_text_windows(transcript_text):
        return False, "SET_CLIPBOARD_FAILED"

    print(
        f"📋 Fallback TRANSCRIPT PASTE: Ctrl+V {len(transcript_text):,} ký tự; "
        "chờ DeepSeek tự tạo pasted-text attachment..."
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
        current = normalize_compare_text(get_deepseek_composer_text(driver, composer))
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


def ask_deepseek(driver, prompt_path, transcript_path, deepseek_project=None):
    """
    FINAL SAFE DeepSeek pipeline - KHÔNG còn cơ chế có thể làm prompt bị đảo text.

    Mỗi attempt bắt đầu bằng NEW CHAT sạch:
      B3 PROMPT: prompt ngắn Ctrl+V; prompt dài CDP Input.insertText 1 lần -> verify cấu trúc cực chặt.
      B4 TRANSCRIPT: upload FILE thật qua DeepSeek.
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

    methods = ["TXT_UPLOAD", "MD_UPLOAD"][:max(1, DEEPSEEK_SMART_CHAT_ATTEMPTS)]

    for attempt_index, method in enumerate(methods, start=1):
        print("\n" + "=" * 72)
        print(f"DEEPSEEK SAFE - ATTEMPT {attempt_index}/{len(methods)} - {method}")
        print("NEW CHAT -> DEEPTHINK aria-pressed=true -> PROMPT -> TRANSCRIPT FILE -> SEND")
        print("=" * 72)

        # NEW CHAT THƯỜNG trực tiếp tại deepseek.com cho mỗi attempt/video.
        # Không dùng Project; mỗi video luôn tách chat riêng.
        try:
            composer = open_deepseek_project_new_chat(
                driver, deepseek_project, timeout=WAIT_DEEPSEEK_READY
            )
        except Exception as exc:
            print(f"❌ Không mở được New Chat trực tiếp: {exc}")
            continue

        # USER REQUIREMENT:
        # Mỗi lần phân tích phải bật DeepThink TRƯỚC prompt/upload/send.
        # Mỗi attempt là một NEW CHAT nên hàm này được gọi đúng 1 lần/attempt.
        if not enable_deepthink_for_analysis(driver):
            print("⛔ DeepThink chưa bật được. KHÔNG chạy analysis ở Instant.")
            continue

        # Reacquire composer vì click DeepThink có thể làm React re-render.
        composer = find_deepseek_composer(driver) or composer

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
        composer = find_deepseek_composer(driver)
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

        if deepseek_has_upload_error(driver):
            print("⛔ DeepSeek đang báo Unable/Failed to upload. KHÔNG Send.")
            continue

        # Không thêm bất kỳ text nào sau prompt, vì prompt đã tự nói transcript đính kèm riêng.
        # Dọn popup Too many requests nếu nó đang che composer/nút Send; đồng thời
        # passive-detect HTTP 429 từ /backend-api/conversations.
        handle_deepseek_conversations_api_429(driver, quiet=False)
        print("📤 BƯỚC 5/5 - bấm Send...")
        if not click_send(driver):
            print("❌ Không gửi được prompt + transcript file.")
            continue

        # Popup/API 429 có thể bật ngay sau request; đóng popup nhưng vẫn tiếp tục
        # chờ/lấy answer hiện tại. Không tự retry chỉ vì sidebar conversations bị 429.
        handle_deepseek_conversations_api_429(driver, quiet=False)
        print("⌛ Đã Send. Đang đợi DeepSeek phân tích transcript...")
        last_turn, dom_text = wait_for_deepseek_answer(driver, previous_count)
        # Đóng lần cuối trước khi click Copy vì dialog overlay có thể chặn nút.
        handle_deepseek_conversations_api_429(driver, quiet=False)
        copied = click_copy_last_answer(driver, last_turn)
        answer = (copied or dom_text or "").strip()
        if answer:
            print("✅ Đã nhận kết quả DeepSeek.")
            try:
                if md_fallback and md_fallback.exists():
                    md_fallback.unlink(missing_ok=True)
            except Exception:
                pass
            return answer

        print("⚠️ DeepSeek không trả text hợp lệ; attempt này thất bại.")

    try:
        if md_fallback and md_fallback.exists():
            md_fallback.unlink(missing_ok=True)
    except Exception:
        pass

    print("❌ DeepSeek pipeline thất bại an toàn. Link được giữ để retry sau.")
    return None


# ============================================================
# PARSE DEEPSEEK CUT RANGES
# ============================================================

_AI_RESULT_RANGE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\d+[.)]\s*)?"
    r"(\d{2,4}:\d{2}:\d{2})\s*"
    r"(?:-->|->|→|—>|–>)\s*"
    r"(\d{2,4}:\d{2}:\d{2}|END)"
    r"\s*[.;]?\s*$",
    re.IGNORECASE,
)

_AI_RESULT_NONE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\d+[.)]\s*)?NONE\s*[.;]?\s*$",
    re.IGNORECASE,
)

_AI_RESULT_DONE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\d+[.)]\s*)?DONE\b",
    re.IGNORECASE,
)


def _strip_result_markdown(line):
    """Chuẩn hóa nhẹ 1 dòng output, chỉ phục vụ việc LỌC block kết quả cuối."""
    value = (line or "").strip()
    if not value:
        return ""

    # Remove common Markdown wrappers without touching timestamp digits.
    value = value.replace("`", "")
    value = re.sub(r"^\s*#{1,6}\s*", "", value)
    value = re.sub(r"^\s*>\s*", "", value)
    value = value.strip()

    # Remove bold/italic wrappers at both ends.
    value = re.sub(r"^\*{1,3}", "", value)
    value = re.sub(r"\*{1,3}$", "", value)
    value = value.strip()
    return value


def _canonical_result_line(line):
    """
    Return:
      ("range", "HH:MM:SS --> HH:MM:SS/END")
      ("none", "NONE")
      ("done", "DONE")
      (None, None)
    """
    value = _strip_result_markdown(line)
    if not value:
        return None, None

    m = _AI_RESULT_RANGE_RE.fullmatch(value)
    if m:
        return "range", f"{m.group(1)} --> {m.group(2).upper()}"

    if _AI_RESULT_NONE_RE.fullmatch(value):
        return "none", "NONE"

    # DONE may have DeepSeek footer text AFTER it on the same line.
    # We only accept it as DONE when it starts the line.
    if _AI_RESULT_DONE_RE.match(value):
        return "done", "DONE"

    return None, None


def extract_last_final_result_block(answer):
    """
    DeepThink may return:
      reasoning...
      examples...
      analysis...
      FINAL ranges
      DONE
      *This response is AI-generated...*

    We DO NOT take the first DONE anymore.

    Algorithm:
    1) Scan DONE candidates from BOTTOM to TOP.
    2) For each DONE, walk upward and collect the contiguous valid result lines.
    3) First valid candidate found from the bottom is the FINAL result.
    4) Canonicalize it to exact console format.

    This prevents:
    - reasoning text from breaking parse;
    - an earlier quoted NONE/DONE example from becoming the result;
    - DeepSeek footer text after DONE from contaminating the result.
    """
    raw = (answer or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("```text", "\n").replace("```", "\n")
    if not raw.strip():
        return ""

    lines = raw.splitlines()

    done_indices = []
    for i, line in enumerate(lines):
        kind, _ = _canonical_result_line(line)
        if kind == "done":
            done_indices.append(i)

    # LAST DONE wins, but only if the block immediately above it is valid.
    for done_idx in reversed(done_indices):
        collected = []
        saw_none = False
        j = done_idx - 1

        while j >= 0:
            raw_line = lines[j]

            # Blank lines inside final block are harmless.
            if not raw_line.strip():
                j -= 1
                continue

            kind, canonical = _canonical_result_line(raw_line)

            if kind == "range":
                if saw_none:
                    break
                collected.append(canonical)
                j -= 1
                continue

            if kind == "none":
                # NONE must be the ONLY body line before DONE.
                if collected:
                    break
                saw_none = True
                j -= 1
                # Do not absorb any earlier text/range.
                break

            # Any explanation/heading marks the start boundary.
            break

        if saw_none and not collected:
            return "NONE\nDONE"

        if collected:
            collected.reverse()
            return "\n".join(collected + ["DONE"])

    # No valid final block found. Return cleaned original so strict parser
    # rejects it and existing repair logic can ask DeepSeek to fix output.
    return raw.strip()


def clean_ai_answer(answer):
    return extract_last_final_result_block(answer).strip()


def transcript_timestamp_set(transcript):
    allowed = set()
    for line in (transcript or "").splitlines():
        m = re.match(r"^\s*(\d{2,4}:\d{2}:\d{2})\s+", line)
        if m:
            allowed.add(m.group(1))
    return allowed


def transcript_timestamp_rows(transcript):
    """Danh sách mốc transcript theo thứ tự: (timestamp, seconds, full_line)."""
    rows = []
    seen = set()

    for raw_line in (transcript or "").splitlines():
        m = re.match(r"^\s*(\d{2,4}:\d{2}:\d{2})\s+(.*)$", raw_line)
        if not m:
            continue

        ts = m.group(1)
        if ts in seen:
            continue

        sec = cutter.timestamp_to_seconds(ts)
        if sec is None:
            continue

        seen.add(ts)
        rows.append((ts, float(sec), raw_line.strip()))

    rows.sort(key=lambda x: x[1])
    return rows


def _timestamp_neighbor_candidates(rows, target_sec, max_delta):
    """
    Trả về tối đa 2 mốc sát nhất: trước và sau target.
    Chỉ giữ mốc cách <= max_delta giây.
    """
    before = None
    after = None

    for row in rows:
        ts, sec, line = row
        if sec <= target_sec:
            before = row
        if sec >= target_sec:
            after = row
            break

    out = []
    if before and abs(before[1] - target_sec) <= max_delta:
        out.append(before)
    if after and abs(after[1] - target_sec) <= max_delta:
        if not out or after[0] != out[-1][0]:
            out.append(after)

    return out


def _choose_best_repaired_range(
    start_text,
    end_text,
    rows,
    allowed,
    max_delta,
):
    """
    Sửa 1 range bằng mốc thật gần nhất.

    - Nếu mốc đã hợp lệ -> giữ nguyên.
    - Nếu mốc sai -> chỉ xét mốc transcript trước/sau trong max_delta.
    - Chọn combo có tổng độ lệch nhỏ nhất nhưng vẫn start < end.
    - Tie-break bảo thủ:
        start: ưu tiên mốc MUỘN hơn
        end:   ưu tiên mốc SỚM hơn
      để hạn chế cắt lấn vào nội dung chính.
    """
    start_sec = cutter.timestamp_to_seconds(start_text)
    if start_sec is None:
        return None

    if start_text in allowed:
        start_candidates = [(start_text, float(start_sec), "exact")]
    else:
        candidates = _timestamp_neighbor_candidates(rows, float(start_sec), max_delta)
        start_candidates = [(ts, sec, "snap") for ts, sec, _ in candidates]

    if end_text == "END":
        end_candidates = [("END", float("inf"), "exact")]
    else:
        end_sec = cutter.timestamp_to_seconds(end_text)
        if end_sec is None:
            return None

        if end_text in allowed:
            end_candidates = [(end_text, float(end_sec), "exact")]
        else:
            candidates = _timestamp_neighbor_candidates(rows, float(end_sec), max_delta)
            end_candidates = [(ts, sec, "snap") for ts, sec, _ in candidates]

    if not start_candidates or not end_candidates:
        return None

    choices = []
    for s_ts, s_sec, s_mode in start_candidates:
        for e_ts, e_sec, e_mode in end_candidates:
            if e_sec != float("inf") and e_sec <= s_sec:
                continue

            start_delta = abs(s_sec - float(start_sec))
            if end_text == "END":
                end_delta = 0.0
            else:
                original_end_sec = float(cutter.timestamp_to_seconds(end_text))
                end_delta = abs(e_sec - original_end_sec)

            # Small conservative tie-break:
            # later start preferred; earlier end preferred.
            conservative = (-s_sec, e_sec if e_sec != float("inf") else 10**18)
            choices.append((
                start_delta + end_delta,
                conservative,
                s_ts,
                e_ts,
                s_mode,
                e_mode,
                start_delta,
                end_delta,
            ))

    if not choices:
        return None

    choices.sort(key=lambda x: (x[0], x[1]))
    best = choices[0]

    return {
        "start": best[2],
        "end": best[3],
        "start_mode": best[4],
        "end_mode": best[5],
        "start_delta": best[6],
        "end_delta": best[7],
    }


def auto_repair_result_timestamps(answer, transcript):
    """
    FAST LOCAL REPAIR.

    Ví dụ DeepSeek trả:
      00:09:18 --> 00:09:27

    nhưng transcript chỉ có:
      00:09:11 ...
      00:09:27 ...

    Nếu độ lệch nhỏ (mặc định <=12s), code tự thay bằng mốc thật gần nhất,
    KHÔNG gửi prompt repair cho DeepSeek.
    """
    if not AUTO_LOCAL_TIMESTAMP_REPAIR or not transcript:
        return answer, []

    cleaned = clean_ai_answer(answer)
    lines = [x.strip() for x in cleaned.splitlines() if x.strip()]

    if not lines or lines[-1].upper() != "DONE":
        return cleaned, []

    body = lines[:-1]
    if body == ["NONE"]:
        return cleaned, []

    rows = transcript_timestamp_rows(transcript)
    allowed = {ts for ts, _, _ in rows}
    if not rows or not allowed:
        return cleaned, []

    pattern = re.compile(
        r"^(\d{2,4}:\d{2}:\d{2})\s*-->\s*(\d{2,4}:\d{2}:\d{2}|END)$",
        re.IGNORECASE,
    )

    repaired_lines = []
    changes = []

    for line in body:
        m = pattern.fullmatch(line)
        if not m:
            # Format error is NOT a timestamp-only repair case.
            return cleaned, []

        old_start = m.group(1)
        old_end = m.group(2).upper()

        if old_start in allowed and (old_end == "END" or old_end in allowed):
            repaired_lines.append(f"{old_start} --> {old_end}")
            continue

        fixed = _choose_best_repaired_range(
            old_start,
            old_end,
            rows,
            allowed,
            float(AUTO_LOCAL_TIMESTAMP_MAX_DELTA),
        )

        if not fixed:
            # One bad/far timestamp -> leave for micro repair.
            return cleaned, []

        new_start = fixed["start"]
        new_end = fixed["end"]

        repaired_lines.append(f"{new_start} --> {new_end}")

        if new_start != old_start:
            changes.append(
                f"{old_start} -> {new_start} "
                f"(Δ{fixed['start_delta']:.0f}s)"
            )
        if new_end != old_end:
            changes.append(
                f"{old_end} -> {new_end} "
                f"(Δ{fixed['end_delta']:.0f}s)"
            )

    if not changes:
        return cleaned, []

    repaired = "\n".join(repaired_lines + ["DONE"])
    return repaired, changes


def nearby_transcript_context(transcript, target_text, radius=2):
    """
    Chỉ lấy vài mốc thật quanh timestamp sai để MICRO REPAIR,
    thay vì bắt DeepSeek đọc lại toàn bộ transcript.
    """
    target_sec = cutter.timestamp_to_seconds(target_text)
    rows = transcript_timestamp_rows(transcript)
    if target_sec is None or not rows:
        return ""

    # Find insertion/nearest index.
    nearest_i = min(
        range(len(rows)),
        key=lambda i: abs(rows[i][1] - float(target_sec)),
    )
    lo = max(0, nearest_i - int(radius))
    hi = min(len(rows), nearest_i + int(radius) + 1)

    snippets = []
    for ts, sec, line in rows[lo:hi]:
        # Keep context short to make repair fast.
        content = line[len(ts):].strip()
        if len(content) > 180:
            content = content[:177] + "..."
        snippets.append(f"{ts} {content}")

    return "\n".join(snippets)


def parse_ai_cut_result(answer, transcript=None):
    """
    STRICT:
    - câu trả lời phải kết thúc bằng DONE;
    - NONE chỉ được đứng một mình trước DONE;
    - mọi dòng khác phải chính xác HH:MM:SS --> HH:MM:SS hoặc END;
    - nếu bật STRICT_AI_TIMESTAMP_VALIDATION, mọi timestamp phải có thật trong transcript.
    """
    original_answer = (answer or "").strip()
    answer = clean_ai_answer(answer)

    # FAST PATH: repair small timestamp drift locally BEFORE strict validation.
    if transcript and AUTO_LOCAL_TIMESTAMP_REPAIR:
        locally_repaired, local_changes = auto_repair_result_timestamps(
            answer, transcript
        )
        if local_changes:
            print("⚡ FAST TIMESTAMP REPAIR - KHÔNG GỌI DEEPSEEK LẠI:")
            for change in local_changes:
                print(f"   🔧 {change}")
            answer = locally_repaired

    if original_answer and answer and answer != original_answer:
        print(
            f"🧹 LỌC KẾT QUẢ DEEPSEEK: "
            f"{len(original_answer):,} chars verbose -> "
            f"{len(answer):,} chars FINAL block."
        )

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


def repair_ai_answer(driver, reason, current_answer=None, transcript=None):
    """
    MICRO REPAIR:
    - KHÔNG yêu cầu DeepSeek đọc lại toàn bộ prompt/transcript.
    - Nếu lỗi là timestamp không tồn tại:
        chỉ gửi FINAL block hiện tại + vài dòng transcript quanh mốc sai.
    - Mục tiêu chỉ đổi timestamp sai, giữ nguyên toàn bộ range khác.
    """
    previous_count = len(get_assistant_turns(driver))
    composer = wait_deepseek_composer(driver, timeout=30)

    final_block = clean_ai_answer(current_answer or "")
    bad_ts_match = re.search(
        r"timestamp không tồn tại trong transcript:\s*(\d{2,4}:\d{2}:\d{2})",
        reason or "",
        re.IGNORECASE,
    )

    if bad_ts_match and transcript:
        bad_ts = bad_ts_match.group(1)
        nearby = nearby_transcript_context(transcript, bad_ts, radius=2)

        message = (
            "SỬA NHANH OUTPUT, KHÔNG PHÂN TÍCH LẠI VIDEO. "
            "KHÔNG đọc lại toàn bộ transcript. "
            f"Timestamp sai: {bad_ts}. "
            "Chỉ thay timestamp sai bằng MỘT timestamp có thật phù hợp trong "
            "các dòng transcript gần nó dưới đây. "
            "GIỮ NGUYÊN tất cả range khác.\n\n"
            "FINAL BLOCK HIỆN TẠI:\n"
            f"{final_block}\n\n"
            "TRANSCRIPT CỤC BỘ QUANH TIMESTAMP SAI:\n"
            f"{nearby}\n\n"
            "Chỉ trả lại FINAL BLOCK đã sửa. "
            "Không giải thích. Dòng cuối phải là DONE."
        )
        print("⚡ MICRO REPAIR: chỉ gửi timestamp sai + transcript cục bộ.")
    else:
        message = (
            "SỬA NHANH OUTPUT, KHÔNG PHÂN TÍCH LẠI NỘI DUNG. "
            f"Lỗi format/validation: {reason}. "
            "Chỉ sửa FINAL BLOCK dưới đây, giữ nguyên ý nghĩa và các range hợp lệ:\n\n"
            f"{final_block}\n\n"
            "Chỉ trả các dòng HH:MM:SS --> HH:MM:SS, "
            "HH:MM:SS --> END, hoặc NONE; dòng cuối DONE. "
            "Không giải thích."
        )
        print("⚡ MICRO REPAIR: chỉ sửa FINAL BLOCK, không đọc lại transcript.")

    if not set_short_instruction(driver, composer, message):
        return None

    if not click_send(driver):
        return None

    last_turn, dom_text = wait_for_deepseek_answer(driver, previous_count)
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


    # RESUME BRIDGE:
    # failedLink.jsonl là lịch sử append-only. Nếu user từng dọn/sửa list.txt,
    # vẫn đưa các FAIL chưa DONE trở lại danh sách; scheduler sẽ tự xếp chúng
    # về PHASE RETRY ở cuối, không chen vào link mới.
    unresolved_failed = load_unresolved_failed_set(done)
    added_failed = 0
    seen_urls = set(urls)
    for failed_url in sorted(unresolved_failed):
        if failed_url not in seen_urls and failed_url not in done:
            urls.append(failed_url)
            seen_urls.add(failed_url)
            added_failed += 1

    if added_failed:
        print(
            f"♻️ RESUME: thêm lại {added_failed:,} link FAIL chưa DONE "
            "từ failedLink.jsonl vào cuối batch."
        )

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
        "--concurrent-fragments", "2",
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


def snapshot_media_cookie_session(youtube_driver, video_url, video_id):
    """
    Chụp cookie RIÊNG cho job media ngay khi AI vừa xong.

    Main thread dùng Selenium YouTube; media worker KHÔNG chạm Selenium.
    Queue nhỏ (MEDIA_QUEUE_MAX) giúp cookie không nằm chờ quá lâu.
    """
    MEDIA_COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    cookie_path = MEDIA_COOKIE_DIR / f"{safe_name(video_id, 'video')}_{stamp}.txt"

    try:
        session = export_fresh_youtube_cookies(
            youtube_driver,
            video_url=video_url,
            cookie_file=cookie_path,
        )
    except Exception as exc:
        print(f"⚠️ Không snapshot được cookie cho media: {exc}")
        session = None

    if not session:
        try:
            cookie_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {
            "cookie_path": None,
            "user_agent": None,
        }

    return {
        "cookie_path": str(session["path"]),
        "user_agent": session.get("user_agent") or "",
    }


def prepare_video_ai_job(
    youtube_driver,
    deepseek_driver,
    video_url,
    prompt_template,
    js_arguments,
    index,
    total,
    deepseek_project=None,
    is_retry=False,
):
    """
    PHẦN AI/CHROME của một video.

    Return:
      {"status": "done"}       -> đã DONE ngay (existing final / full-cut)
      {"status": "failed"}     -> fail trước media
      {"status": "media_job"}  -> GPT xong; đưa job sang background media worker

    Quan trọng: sau khi trả media_job, GPT/YouTube main thread được chuyển ngay
    sang video kế tiếp. doneLink CHỈ được ghi bởi media worker khi MP3 cuối OK.
    """
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
        # STEP 1: TRANSCRIPT
        # ------------------------------------------------------------
        stage = "transcript"
        transcript = get_transcript(youtube_driver, video_url)
        if not transcript:
            append_failure(video_url, stage, "Không có/lấy không được transcript")
            return {"status": "failed", "url": video_url, "stage": stage}

        cookie_path = None
        browser_user_agent = None

        # ------------------------------------------------------------
        # STEP 2: METADATA + CHANNEL FOLDER
        # ------------------------------------------------------------
        stage = "metadata"
        print("\n⚡ BƯỚC METADATA: ưu tiên đọc trực tiếp từ tab YouTube; không export cookie nếu chưa cần...")
        metadata = fetch_video_metadata(
            video_url,
            js_arguments,
            youtube_driver=youtube_driver,
            cookie_file=None,
        )

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
        if deepseek_project and deepseek_project.get("direct"):
            print("👤 DeepSeek: DIRECT CHAT (không Project)")
        elif deepseek_project:
            print(f"👤 DeepSeek Project: {deepseek_project.get('name') or deepseek_project.get('key')}")

        # Crash recovery: file final đã có nhưng doneLink thiếu.
        existing_final = find_existing_final(workspace, video_id)
        if existing_final:
            print(f"✅ Đã thấy MP3 hoàn chỉnh từ lần trước: {existing_final.name}")
            append_global_done_link(video_url)
            append_channel_done_link(workspace, video_url)
            return {"status": "done", "url": video_url, "final_path": str(existing_final)}

        # ------------------------------------------------------------
        # STEP 3: AI files
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
        # STEP 4: Reuse AI cache
        # ------------------------------------------------------------
        if REUSE_VALID_AI_RESULT and canonical_result.exists():
            try:
                cached = canonical_result.read_text(encoding="utf-8-sig")
                parsed, reason = parse_ai_cut_result(cached, transcript=transcript)
                if parsed:
                    answer = parsed.get("answer") or cached
                    # Nếu cache cũ chỉ lệch timestamp nhỏ, ghi đè bằng bản đã repair local.
                    if answer.strip() != cached.strip():
                        canonical_result.write_text(answer.strip() + "\n", encoding="utf-8")
                        print("⚡ Đã tự sửa timestamp nhỏ trong AI cache cũ.")
                    print(f"♻️ Reuse AI result hợp lệ: {canonical_result.name}")
                else:
                    print(f"⚠️ AI cache cũ không hợp lệ ({reason}), sẽ hỏi DeepSeek lại.")
            except Exception:
                parsed = None

        # ------------------------------------------------------------
        # STEP 5: DEEPSEEK + STRICT VALIDATION
        # ------------------------------------------------------------
        if parsed is None:
            stage = "deepseek"
            answer = ask_deepseek(
                deepseek_driver,
                prompt_path,
                transcript_path,
                deepseek_project=deepseek_project,
            )
            if not answer:
                append_failure(
                    video_url, stage, "Không nhận được câu trả lời DeepSeek",
                    workspace["channel_id"], title
                )
                return {"status": "failed", "url": video_url, "stage": stage}

            parsed, reason = parse_ai_cut_result(answer, transcript=transcript)
            if parsed is not None:
                # parse may have locally repaired/sanitized the final block.
                answer = parsed.get("answer") or answer

            repair_count = 0
            while parsed is None and repair_count < AI_REPAIR_ATTEMPTS:
                repair_count += 1
                print(f"⚠️ AI output chưa hợp lệ: {reason}")
                print(f"🔧 Yêu cầu DeepSeek sửa output lần {repair_count}/{AI_REPAIR_ATTEMPTS}...")
                repaired = repair_ai_answer(deepseek_driver, reason, current_answer=answer, transcript=transcript)
                if not repaired:
                    break
                answer = repaired
                parsed, reason = parse_ai_cut_result(answer, transcript=transcript)
                if parsed is not None:
                    answer = parsed.get("answer") or answer

            if parsed is None:
                bad_path = workspace["ai_results"] / f"{video_id}_INVALID.txt"
                bad_path.write_text(answer or "", encoding="utf-8")
                append_failure(
                    video_url, "ai_validate", reason or "AI output invalid",
                    workspace["channel_id"], title
                )
                print("❌ AI vẫn không hợp lệ sau repair; KHÔNG download/cắt.")
                return {"status": "failed", "url": video_url, "stage": "ai_validate"}

            canonical_result.write_text(clean_ai_answer(answer) + "\n", encoding="utf-8")
            print(f"💾 AI result chuẩn: {canonical_result}")

        print("\n📋 KẾT QUẢ AI ĐÃ VALIDATE:")
        print("-" * 50)
        print(clean_ai_answer(answer))
        print("-" * 50)

        # ------------------------------------------------------------
        # FULL CUT = không cần media
        # ------------------------------------------------------------
        if ai_requests_full_cut(parsed):
            print("🚫 AI trả 00:00:00 --> END: toàn bộ audio bị loại theo kết quả AI.")
            print("⏭️ Không tải MP3. Đã ghi no_story.txt và chuyển link tiếp theo.")
            append_no_story(video_url, workspace=workspace, title=title)
            append_global_done_link(video_url)
            append_channel_done_link(workspace, video_url)
            write_video_log(workspace, video_id, "FULL_CUT -> AI requested full cut; download skipped")
            return {"status": "done", "url": video_url, "full_cut": True}

        # ------------------------------------------------------------
        # GPT HẾT NHIỆM VỤ -> SNAPSHOT COOKIE -> MEDIA QUEUE
        # ------------------------------------------------------------
        stage = "snapshot_media_session"
        media_session = snapshot_media_cookie_session(
            youtube_driver,
            video_url,
            video_id,
        )

        job = {
            "video_url": video_url,
            "video_id": video_id,
            "title": title,
            "workspace": workspace,
            "parsed": parsed,
            "js_arguments": list(js_arguments or []),
            "cookie_path": media_session.get("cookie_path"),
            "user_agent": media_session.get("user_agent"),
            "is_retry": bool(is_retry),
        }

        print("\n🚀 DEEPSEEK ĐÃ XONG VIDEO NÀY.")
        print("📦 Đưa DOWNLOAD MP3 + FFmpeg sang MEDIA WORKER chạy nền.")
        print("➡️ Main thread được chuyển ngay sang transcript/GPT của video kế tiếp.")

        return {
            "status": "media_job",
            "url": video_url,
            "job": job,
        }

    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if workspace:
            write_video_log(
                workspace,
                video_id,
                f"ERROR stage={stage}: {exc}\n{traceback.format_exc()}",
            )
            channel_id = workspace.get("channel_id", "")
        else:
            channel_id = ""

        append_failure(video_url, stage, exc, channel_id, title)
        print(f"❌ Lỗi tại stage={stage}: {exc}")
        return {"status": "failed", "url": video_url, "stage": stage}


def process_media_job(job):
    """
    DOWNLOAD MP3 + CUT/MERGE chạy trong background worker.

    KHÔNG dùng Selenium driver trong thread này.
    Cookie là snapshot riêng của video do main thread tạo trước khi enqueue.
    """
    video_url = job["video_url"]
    video_id = job["video_id"]
    title = job["title"]
    workspace = job["workspace"]
    parsed = job["parsed"]
    js_arguments = job["js_arguments"]
    cookie_path = job.get("cookie_path")
    user_agent = job.get("user_agent")
    is_retry = bool(job.get("is_retry"))
    stage = "media_start"
    raw_video = None

    print("\n" + "▓" * 72)
    print(f"🎧 [MEDIA] BẮT ĐẦU: {video_id} | {title[:70]}")
    print(f"🔁 [MEDIA] retry={'CÓ' if is_retry else 'KHÔNG'}")
    print("▓" * 72)

    try:
        # Có thể final đã được tạo bởi lần chạy khác trước khi job tới worker.
        existing_final = find_existing_final(workspace, video_id)
        if existing_final:
            append_global_done_link(video_url)
            append_channel_done_link(workspace, video_url)
            return {
                "url": video_url,
                "ok": True,
                "is_retry": is_retry,
                "final_path": str(existing_final),
                "stage": "existing_final",
            }

        stage = "disk"
        if not check_free_space(cutter.DOWNLOAD_DIR):
            append_failure(
                video_url, stage, "Không đủ dung lượng trống",
                workspace["channel_id"], title
            )
            return {
                "url": video_url,
                "ok": False,
                "is_retry": is_retry,
                "stage": stage,
            }

        stage = "download_audio_mp3"
        raw_video = robust_download_audio(
            video_url,
            js_arguments,
            title,
            cookie_file=cookie_path,
            user_agent=user_agent,
        )
        if not raw_video:
            append_failure(
                video_url,
                stage,
                "yt-dlp không tải được audio sau tất cả fallback",
                workspace["channel_id"],
                title,
            )
            return {
                "url": video_url,
                "ok": False,
                "is_retry": is_retry,
                "stage": stage,
            }

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
            append_failure(
                video_url,
                stage,
                "FFmpeg cắt/ghép thất bại",
                workspace["channel_id"],
                title,
            )
            return {
                "url": video_url,
                "ok": False,
                "is_retry": is_retry,
                "stage": stage,
            }

        stage = "verify_final_mp3"
        final_duration = cutter.get_video_duration(final_path)
        if final_duration is None or final_duration <= 0:
            append_failure(
                video_url,
                stage,
                "MP3 cuối không đọc được duration",
                workspace["channel_id"],
                title,
            )
            return {
                "url": video_url,
                "ok": False,
                "is_retry": is_retry,
                "stage": stage,
            }

        stage = "mark_done"
        append_global_done_link(video_url)
        append_channel_done_link(workspace, video_url)
        write_video_log(workspace, video_id, f"DONE -> {final_path}")

        cutter.cleanup_after_success(raw_video)
        raw_video = None

        print(f"✅ [MEDIA] HOÀN TẤT MP3: {final_path}")
        return {
            "url": video_url,
            "ok": True,
            "is_retry": is_retry,
            "final_path": str(final_path),
            "stage": stage,
        }

    except Exception as exc:
        write_video_log(
            workspace,
            video_id,
            f"MEDIA ERROR stage={stage}: {exc}\n{traceback.format_exc()}",
        )
        append_failure(
            video_url,
            stage,
            exc,
            workspace.get("channel_id", ""),
            title,
        )
        print(f"❌ [MEDIA] Lỗi stage={stage}: {exc}")
        return {
            "url": video_url,
            "ok": False,
            "is_retry": is_retry,
            "stage": stage,
            "error": str(exc),
        }

    finally:
        # Cookie snapshot là file riêng của job, xóa khi worker xong.
        if cookie_path:
            try:
                Path(cookie_path).unlink(missing_ok=True)
            except Exception:
                pass


class AsyncMediaPipeline:
    """
    Một consumer media chạy nền.

    queue max nhỏ để:
    - AI không chạy quá xa download;
    - cookie snapshot không nằm chờ hàng giờ;
    - RAM/disk không phình;
    - vẫn overlap GPT(video N+1) với download/cut(video N).
    """

    SENTINEL = object()

    def __init__(self, max_queue=MEDIA_QUEUE_MAX):
        self.jobs = Queue(maxsize=max(1, int(max_queue)))
        self.results = Queue()
        self.thread = threading.Thread(
            target=self._worker_loop,
            name="MEDIA-WORKER-1",
            daemon=True,
        )
        self.started = False
        self._stopping = False

    def start(self):
        if self.started:
            return
        self.started = True
        self.thread.start()
        print(
            f"🚄 MEDIA PIPELINE: 1 worker nền | queue tối đa {self.jobs.maxsize} job."
        )

    def _worker_loop(self):
        while True:
            job = self.jobs.get()
            try:
                if job is self.SENTINEL:
                    return
                result = process_media_job(job)
                self.results.put(result)
            except Exception as exc:
                # Không để worker chết làm queue.join() treo vô hạn.
                try:
                    video_url = str(job.get("video_url") or "")
                    result = {
                        "url": video_url,
                        "ok": False,
                        "is_retry": bool(job.get("is_retry")),
                        "stage": "media_worker_crash",
                        "error": str(exc),
                    }
                    self.results.put(result)
                    if video_url:
                        append_failure(video_url, "media_worker_crash", exc)
                except Exception:
                    pass
            finally:
                self.jobs.task_done()

    def submit(self, job):
        if not self.started:
            self.start()

        if self.jobs.full():
            print(
                "⏳ MEDIA QUEUE đã đầy -> AI chờ media nhường 1 slot. "
                "Vẫn giữ tối đa 2 video chạy trước để không nghẽn mạng/RAM."
            )

        # Blocking ở đây là backpressure CÓ CHỦ Ý.
        self.jobs.put(job)
        print(
            f"📥 MEDIA QUEUE: +1 | đang chờ={self.jobs.qsize()} | "
            f"{job.get('video_id')}"
        )

    def drain_results(self):
        items = []
        while True:
            try:
                items.append(self.results.get_nowait())
            except Empty:
                break
        return items

    def wait_all(self):
        if self.started:
            self.jobs.join()
        return self.drain_results()

    def shutdown(self, wait=True):
        if not self.started or self._stopping:
            return
        self._stopping = True

        if wait:
            self.jobs.join()

        try:
            self.jobs.put_nowait(self.SENTINEL)
        except Exception:
            try:
                self.jobs.put(self.SENTINEL, timeout=1)
            except Exception:
                return

        if wait:
            self.thread.join(timeout=10)


def process_one_video(
    youtube_driver,
    deepseek_driver,
    video_url,
    prompt_template,
    js_arguments,
    index,
    total,
    deepseek_project=None,
):
    """
    Wrapper tuần tự giữ tương thích nếu có code ngoài gọi hàm cũ.
    Main pipeline mới KHÔNG dùng wrapper này; main dùng producer + media worker.
    """
    outcome = prepare_video_ai_job(
        youtube_driver,
        deepseek_driver,
        video_url,
        prompt_template,
        js_arguments,
        index,
        total,
        deepseek_project=deepseek_project,
        is_retry=False,
    )
    if outcome.get("status") == "done":
        return True
    if outcome.get("status") != "media_job":
        return False

    result = process_media_job(outcome["job"])
    return bool(result.get("ok"))


def main():
    configure_console()
    ensure_dirs()
    print_resume_bridge_report()

    print("=" * 72)
    print(" AUTO YOUTUBE MAIN-CONTENT CUT PIPELINE - DEEPSEEK DIRECT")
    print(" YouTube get_panel -> DeepSeek Direct multi-profile -> yt-dlp -> FFmpeg cut/merge")
    print("=" * 72)
    print(f"📁 Project: {BASE_DIR}")
    print(f"📁 YouTube profile: {YOUTUBE_USER_DATA_DIR}")
    print(f"📁 DeepSeek multi-profile root: {DEEPSEEK_ACCOUNT_PROFILE_ROOT}")

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

    rotation_targets = build_deepseek_rotation_targets()
    if not rotation_targets:
        print("❌ Không có DeepSeek account/profile enabled trong deepseek_accounts.json.")
        print("👉 Chỉ cần tạo/login Chrome profile; KHÔNG cần Project.")
        return

    rotation_cursor = load_deepseek_round_robin_cursor(len(rotation_targets))
    print_deepseek_rotation_plan(rotation_targets, rotation_cursor)

    js_arguments = cutter.get_javascript_arguments()
    if js_arguments is None:
        return

    cutter.update_ytdlp()

    youtube_driver = None
    deepseek_driver = None
    deepseek_process = None
    current_project = None
    media_pipeline = None

    try:
        # YouTube giữ nguyên một Selenium profile cho cả batch.
        youtube_driver = create_youtube_driver()
        if not youtube_driver:
            return

        # ============================================================
        # SCHEDULER 2 PHASE + ASYNC MEDIA
        #
        # MAIN THREAD:
        #   transcript -> GPT -> enqueue media -> VIDEO KẾ
        #
        # MEDIA THREAD:
        #   download MP3 -> FFmpeg -> DONE
        #
        # Fail vẫn giữ rule:
        #   link mới fail -> KHÔNG retry ngay
        #   chạy hết phase link mới -> mới retry.
        # ============================================================
        done_now = load_global_done_set()
        historical_failed = load_unresolved_failed_set(done_now)

        fresh_urls = [u for u in urls if u not in historical_failed]
        retry_queue = [u for u in urls if u in historical_failed]
        retry_seen = set(retry_queue)

        print("\n" + "=" * 72)
        print("🚄 PIPELINE CUỐN CHIẾU: AI + MEDIA CHẠY SONG SONG")
        print("=" * 72)
        print(f"🆕 Link mới/chưa từng fail: {len(fresh_urls):,}")
        print(f"⏳ Link fail cũ hoãn về cuối: {len(retry_queue):,}")
        print(f"🎧 Media worker: 1 | queue tối đa: {MEDIA_QUEUE_MAX}")
        print("🧠 DeepSeek xong video N -> lập tức chuyển video N+1.")
        print("🎵 Download/cắt video N chạy nền.")
        print("✅ doneLink chỉ ghi khi MP3 cuối đã OK.")
        print("=" * 72)

        success_urls = set()
        unresolved_urls = set()
        pending_media_urls = set()
        fresh_failed_this_run = 0
        retry_attempts = 0

        media_pipeline = AsyncMediaPipeline(MEDIA_QUEUE_MAX)
        media_pipeline.start()

        def handle_media_results(results):
            nonlocal fresh_failed_this_run

            for result in results:
                video_url = result.get("url")
                if not video_url:
                    continue

                pending_media_urls.discard(video_url)

                if result.get("ok"):
                    success_urls.add(video_url)
                    unresolved_urls.discard(video_url)
                    print(
                        f"✅ [MEDIA RESULT] DONE: {video_id_from_url(video_url)}"
                    )
                    continue

                unresolved_urls.add(video_url)
                is_retry = bool(result.get("is_retry"))
                stage = result.get("stage") or "media"

                if not is_retry:
                    if video_url not in retry_seen:
                        retry_seen.add(video_url)
                        retry_queue.append(video_url)
                    print(
                        f"⏭️ [MEDIA RESULT] FAIL stage={stage} -> "
                        "hoãn RETRY về cuối batch."
                    )
                else:
                    print(
                        f"❌ [MEDIA RESULT] RETRY vẫn fail stage={stage} -> "
                        "để lần chạy sau."
                    )

        def drain_media_now():
            handle_media_results(media_pipeline.drain_results())

        def run_one_ai_scheduled(
            video_url,
            display_index,
            display_total,
            phase_name,
            is_retry=False,
        ):
            nonlocal youtube_driver
            nonlocal deepseek_driver
            nonlocal deepseek_process
            nonlocal current_project
            nonlocal rotation_cursor

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
            print("🌐 DeepSeek: DIRECT (không Project)")
            print("=" * 72)

            outcome = {"status": "failed", "url": video_url}

            try:
                if not driver_alive(youtube_driver):
                    print("♻️ YouTube driver đã mất kết nối, đang mở lại...")
                    try:
                        youtube_driver.quit()
                    except Exception:
                        pass
                    youtube_driver = create_youtube_driver()
                    if not youtube_driver:
                        raise RuntimeError("Không recover được YouTube driver")

                deepseek_driver, deepseek_process, current_project = open_deepseek_rotation_target(
                    account,
                    project,
                    old_driver=deepseek_driver,
                    old_process=deepseek_process,
                )

                ensure_deepseek_ready_interactive(deepseek_driver)

                outcome = prepare_video_ai_job(
                    youtube_driver,
                    deepseek_driver,
                    video_url,
                    prompt_template,
                    js_arguments,
                    display_index,
                    display_total,
                    deepseek_project=current_project,
                    is_retry=is_retry,
                )

                if outcome.get("status") == "media_job":
                    pending_media_urls.add(video_url)
                    media_pipeline.submit(outcome["job"])

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                outcome = {
                    "status": "failed",
                    "url": video_url,
                    "stage": "outer_scheduler",
                }
                append_failure(video_url, "outer_scheduler", exc)
                print(f"\n❌ Lỗi ngoài dự kiến với profile/video này: {exc}")

            finally:
                rotation_cursor = (target_index + 1) % len(rotation_targets)
                save_deepseek_round_robin_cursor(rotation_cursor)

                if DEEPSEEK_ROUND_ROBIN_CLOSE_EACH_VIDEO:
                    close_deepseek_browser_hard(deepseek_driver, deepseek_process)
                    deepseek_driver = None
                    deepseek_process = None
                    current_project = None

            # Media của video trước có thể vừa xong trong lúc GPT video này chạy.
            drain_media_now()
            return outcome

        # ------------------------------------------------------------
        # PHASE 1: toàn bộ LINK MỚI.
        # ------------------------------------------------------------
        if fresh_urls:
            print("\n" + "#" * 72)
            print("🆕 PHASE 1/2 - AI chạy link mới; MEDIA chạy cuốn chiếu phía sau")
            print("#" * 72)

        for index, video_url in enumerate(fresh_urls, start=1):
            outcome = run_one_ai_scheduled(
                video_url,
                index,
                len(fresh_urls),
                "LINK MỚI",
                is_retry=False,
            )

            status = outcome.get("status")
            if status == "done":
                success_urls.add(video_url)
                unresolved_urls.discard(video_url)

            elif status == "failed":
                fresh_failed_this_run += 1
                unresolved_urls.add(video_url)

                if video_url not in retry_seen:
                    retry_seen.add(video_url)
                    retry_queue.append(video_url)

                print(
                    "⏭️ AI/TRANSCRIPT FAIL -> hoãn RETRY về cuối. "
                    "Tiếp tục link mới kế tiếp."
                )

            # media_job: KHÔNG đánh dấu done/fail lúc này.
            # Worker sẽ trả result sau.
            drain_media_now()

        # Hết link mới về phía AI chưa có nghĩa media đã xong.
        # Drain hết media fresh trước khi bắt đầu retry để giữ đúng rule của user.
        if pending_media_urls:
            print("\n⏳ AI đã chạy hết LINK MỚI.")
            print(
                f"🎧 Chờ MEDIA xử lý nốt {len(pending_media_urls)} job đang pending "
                "trước khi sang RETRY..."
            )
        handle_media_results(media_pipeline.wait_all())

        # ------------------------------------------------------------
        # PHASE 2: retry các fail cũ + fail fresh.
        # ------------------------------------------------------------
        if retry_queue:
            print("\n" + "#" * 72)
            print("🔁 PHASE 2/2 - LINK MỚI ĐÃ XONG, BẮT ĐẦU RETRY FAIL")
            print(f"📦 Tổng link cần retry: {len(retry_queue):,}")
            print("⚠️ Mỗi link chỉ retry 1 lần trong run hiện tại.")
            print("#" * 72)

        for retry_index, video_url in enumerate(retry_queue, start=1):
            if video_url in load_global_done_set():
                print(
                    f"⏭️ RETRY {retry_index}/{len(retry_queue)} đã DONE -> bỏ qua."
                )
                success_urls.add(video_url)
                unresolved_urls.discard(video_url)
                continue

            retry_attempts += 1

            outcome = run_one_ai_scheduled(
                video_url,
                retry_index,
                len(retry_queue),
                "RETRY",
                is_retry=True,
            )

            status = outcome.get("status")

            if status == "done":
                success_urls.add(video_url)
                unresolved_urls.discard(video_url)

            elif status == "failed":
                unresolved_urls.add(video_url)
                print(
                    "❌ RETRY AI/TRANSCRIPT vẫn fail -> không chạy lại trong run này."
                )

            # media_job sẽ được worker xử lý song song với retry kế tiếp.
            drain_media_now()

        # Drain media của phase retry.
        if pending_media_urls:
            print(
                f"\n⏳ Đã enqueue hết RETRY. Chờ MEDIA nốt "
                f"{len(pending_media_urls)} job..."
            )
        handle_media_results(media_pipeline.wait_all())

        media_pipeline.shutdown(wait=True)

        success = len(success_urls)
        failed = len(unresolved_urls)

        print("\n" + "=" * 72)
        print("KẾT QUẢ")
        print(f"✅ Thành công: {success}")
        print(f"❌ Thất bại còn lại: {failed}")
        print(f"⏳ Fail AI/TRANSCRIPT phát sinh ở phase link mới: {fresh_failed_this_run}")
        print(f"🔁 Số lượt retry cuối batch: {retry_attempts}")
        print("🚄 Chế độ: GPT(video N+1) chạy song song DOWNLOAD/CUT(video N)")
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
        if media_pipeline is not None:
            try:
                # Normal path đã drain xong. Nếu Ctrl+C thì không chờ lâu.
                media_pipeline.shutdown(wait=False)
            except Exception:
                pass

        if youtube_driver is not None:
            try:
                youtube_driver.quit()
            except Exception:
                pass

        if deepseek_driver is not None or deepseek_process not in (None, False):
            close_deepseek_browser_hard(deepseek_driver, deepseek_process)


if __name__ == "__main__":
    main()
