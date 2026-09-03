# -*- coding: utf-8 -*-
"""
RETRY FAILED RUNNER
- Đọc failedLink.jsonl
- Lấy toàn bộ YouTube URL từng fail
- Bỏ trùng theo video ID
- Bỏ link đã có trong doneLink.txt
- Tạo retry_failed.txt
- Backup list.txt hiện tại
- Tạm thay list.txt bằng danh sách fail cần retry
- Chạy auto_youtube_chatgpt_cut.py
- Luôn restore list.txt ban đầu sau khi code chính thoát

failedLink.jsonl chỉ là log lịch sử và KHÔNG bị xóa.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent

FAILED_FILE = BASE_DIR / "failedLink.jsonl"
DONE_FILE = BASE_DIR / "doneLink.txt"
LIST_FILE = BASE_DIR / "list.txt"
RETRY_FILE = BASE_DIR / "retry_failed.txt"
RUNTIME_DIR = BASE_DIR / "runtime"
BACKUP_DIR = RUNTIME_DIR / "retry_backups"

MAIN_CANDIDATES = [
    "auto_youtube_chatgpt_cut.py",
    "auto_youtube_chatgpt_cut(1).py",
]

YOUTUBE_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s"\'<>\\]+',
    re.IGNORECASE,
)


def configure_console():
    if os.name == "nt":
        try:
            os.system("chcp 65001 > nul")
        except OSError:
            pass


def video_id_from_url(url: str) -> str | None:
    """Lấy video ID từ watch/youtu.be/shorts/live/embed."""
    try:
        parsed = urlparse(url.strip())
        host = (parsed.netloc or "").lower()
        path = parsed.path.strip("/")

        if host.endswith("youtu.be"):
            vid = path.split("/", 1)[0]
            return vid or None

        if "youtube.com" in host:
            if path == "watch":
                vid = parse_qs(parsed.query).get("v", [None])[0]
                return vid or None

            parts = path.split("/")
            if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
                return parts[1] or None
    except Exception:
        pass

    m = re.search(r'(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{6,})', url)
    return m.group(1) if m else None


def clean_url(url: str) -> str:
    return url.strip().rstrip(".,;)]}")


def extract_urls_from_text(text: str) -> list[str]:
    urls = []
    for m in YOUTUBE_URL_RE.finditer(text):
        url = clean_url(m.group(0))
        if video_id_from_url(url):
            urls.append(url)
    return urls


def extract_urls_from_json_value(value) -> list[str]:
    urls = []
    if isinstance(value, str):
        urls.extend(extract_urls_from_text(value))
    elif isinstance(value, dict):
        # Ưu tiên các field URL quen thuộc trước.
        for key in ("url", "video_url", "video", "link"):
            v = value.get(key)
            if isinstance(v, str):
                urls.extend(extract_urls_from_text(v))
        # Sau đó quét toàn bộ dict để chịu được format log khác.
        for v in value.values():
            urls.extend(extract_urls_from_json_value(v))
    elif isinstance(value, list):
        for item in value:
            urls.extend(extract_urls_from_json_value(item))
    return urls


def read_failed_urls(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Không thấy {path.name}: {path}")

    # key = video ID (hoặc URL fallback), value = URL.
    # Nếu cùng link fail nhiều lần thì giữ occurrence MỚI NHẤT.
    latest = {}

    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line_no, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue

            urls = []
            try:
                obj = json.loads(raw)
                urls.extend(extract_urls_from_json_value(obj))
            except Exception:
                # JSONL có dòng lỗi/hỏng vẫn cố lấy URL trực tiếp.
                urls.extend(extract_urls_from_text(raw))

            for url in urls:
                vid = video_id_from_url(url)
                key = vid or url
                if key in latest:
                    latest.pop(key, None)
                latest[key] = url

    return list(latest.values())


def read_done_ids(path: Path) -> set[str]:
    done = set()
    if not path.exists():
        return done

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    urls = extract_urls_from_text(text)

    # doneLink.txt thường mỗi dòng 1 URL; fallback thêm từng dòng.
    if not urls:
        urls = [line.strip() for line in text.splitlines() if line.strip()]

    for url in urls:
        vid = video_id_from_url(url)
        done.add(vid or url.strip())

    return done


def filter_pending(failed_urls: list[str], done_ids: set[str]) -> list[str]:
    pending = []
    seen = set()

    for url in failed_urls:
        vid = video_id_from_url(url)
        key = vid or url

        if key in done_ids:
            continue
        if key in seen:
            continue

        seen.add(key)
        pending.append(url)

    return pending


def find_main_script() -> Path | None:
    for name in MAIN_CANDIDATES:
        p = BASE_DIR / name
        if p.exists():
            return p

    # Fallback: tìm file auto_youtube... nhưng không lấy chính runner này.
    matches = sorted(BASE_DIR.glob("auto_youtube_chatgpt_cut*.py"))
    for p in matches:
        if p.name != Path(__file__).name:
            return p
    return None


def write_retry_file(urls: list[str]):
    RETRY_FILE.write_text(
        "".join(f"{url}\n" for url in urls),
        encoding="utf-8",
    )


def backup_list_file() -> tuple[bool, Path | None]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    existed = LIST_FILE.exists()
    if not existed:
        return False, None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"list_before_retry_{stamp}.txt"
    shutil.copy2(LIST_FILE, backup)
    return True, backup


def restore_list_file(existed: bool, backup: Path | None):
    if existed and backup and backup.exists():
        shutil.copy2(backup, LIST_FILE)
        print(f"✅ Đã restore list.txt gốc từ: {backup.name}")
    else:
        try:
            LIST_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        print("✅ Trước khi retry không có list.txt -> đã trả về trạng thái cũ.")


def print_header():
    print("=" * 72)
    print(" RETRY FAILED - YOUTUBE / CHATGPT PIPELINE")
    print("=" * 72)
    print(f"📁 Project: {BASE_DIR}")
    print(f"❌ Failed log: {FAILED_FILE.name}")
    print(f"✅ Done file: {DONE_FILE.name}")
    print()


def main() -> int:
    configure_console()

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Chỉ tạo retry_failed.txt, không chạy code chính.",
    )
    args = parser.parse_args()

    print_header()

    try:
        failed_urls = read_failed_urls(FAILED_FILE)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 2

    done_ids = read_done_ids(DONE_FILE)
    pending = filter_pending(failed_urls, done_ids)
    write_retry_file(pending)

    print(f"📋 Link unique từng fail: {len(failed_urls):,}")
    print(f"✅ Video đã DONE đang được loại ra: {len(failed_urls) - len(pending):,}")
    print(f"🔁 Link còn phải RETRY: {len(pending):,}")
    print(f"📝 Đã tạo: {RETRY_FILE}")

    if not pending:
        print("\n✅ Không còn link fail nào cần chạy lại.")
        return 0

    if args.generate_only:
        print("\n✅ Chỉ tạo danh sách. Chưa chạy pipeline.")
        return 0

    main_script = find_main_script()
    if not main_script:
        print("\n❌ Không tìm thấy code chính.")
        print("   Hãy đặt RETRY_FAILED.bat + retry_failed_runner.py")
        print("   cùng thư mục với auto_youtube_chatgpt_cut.py")
        return 3

    existed, backup = backup_list_file()

    try:
        # Tạm dùng danh sách retry làm list.txt.
        shutil.copy2(RETRY_FILE, LIST_FILE)

        print("\n" + "=" * 72)
        print(" BẮT ĐẦU CHẠY RIÊNG LINK FAIL")
        print("=" * 72)
        print(f"🐍 Python: {sys.executable}")
        print(f"🚀 Code: {main_script.name}")
        if backup:
            print(f"💾 Backup list gốc: {backup}")
        print()

        result = subprocess.run(
            [sys.executable, str(main_script)],
            cwd=str(BASE_DIR),
        )

        print("\n" + "=" * 72)
        print(f"PIPELINE ĐÃ THOÁT | exit code = {result.returncode}")
        print("=" * 72)
        return int(result.returncode)

    except KeyboardInterrupt:
        print("\n⚠️ Bạn đã dừng RETRY bằng Ctrl+C.")
        return 130

    finally:
        restore_list_file(existed, backup)
        print(f"📝 Danh sách retry vẫn giữ tại: {RETRY_FILE}")
        print("ℹ️ failedLink.jsonl được giữ nguyên làm lịch sử.")
        print("ℹ️ Lần sau chạy RETRY_FAILED.bat, link đã có trong doneLink.txt sẽ tự bị bỏ.")


if __name__ == "__main__":
    raise SystemExit(main())
