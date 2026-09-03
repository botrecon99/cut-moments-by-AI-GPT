import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path


# ============================================================
# CẤU HÌNH
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DOWNLOAD_DIR = BASE_DIR / "downloads"
WORK_DIR = DOWNLOAD_DIR / "_cut_work"
DONE_DIR = BASE_DIR / "done"

LIST_FILE = BASE_DIR / "list.txt"
DONE_LINK_FILE = BASE_DIR / "doneLink.txt"

TEMP_VIDEO_NAME = "raw_audio"
SOURCE_URL_FILE = DOWNLOAD_DIR / "raw_audio_source.txt"
SOURCE_TITLE_FILE = DOWNLOAD_DIR / "raw_audio_title.txt"

AUTO_UPDATE_YTDLP = True

# False: nếu MP3 thô đúng link hiện tại thì dùng lại.
# True: luôn xóa MP3 thô và tải lại.
FORCE_REDOWNLOAD = False

# True: xóa MP3 thô sau khi xuất file thành công.
# False: giữ MP3 thô cho tới khi code chuyển sang link tiếp theo.
DELETE_RAW_AFTER_DONE = True

# Audio-only: không dùng GPU/NVENC.
AUDIO_BITRATE = "128k"
YTDLP_AUDIO_QUALITY = "128K"

# Download/cut stability.
# CHỈ tải audio-only. Không tải video stream để tiết kiệm băng thông/thời gian.
DOWNLOAD_FORMAT = "ba[ext=m4a]/ba"
DOWNLOAD_RETRIES = 12
FRAGMENT_RETRIES = 12

# Bỏ qua phần cần giữ ngắn hơn giá trị này.
MIN_KEEP_SECONDS = 0.20

# Sau post-process yt-dlp, file chuẩn là MP3. Các extension phụ chỉ để dọn/recover file dang dở.
VIDEO_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".webm",
    ".opus",
    ".ogg",
    ".aac",
    ".wav"
)


# ============================================================
# KHỞI TẠO
# ============================================================

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
DONE_DIR.mkdir(parents=True, exist_ok=True)


class UserQuit(Exception):
    """Người dùng yêu cầu dừng chương trình."""


def configure_console():
    if os.name == "nt":
        try:
            os.system("chcp 65001 > nul")
        except OSError:
            pass


# ============================================================
# TÌM CHƯƠNG TRÌNH
# ============================================================

def find_program(program_name):
    local_program = BASE_DIR / program_name

    if local_program.exists():
        return str(local_program)

    return shutil.which(program_name)


# IMPORTANT: Trên Windows, file .venv\Scripts\yt-dlp.exe do pip tạo có thể
# chứa đường dẫn TUYỆT ĐỐI tới python.exe tại thời điểm cài. Nếu project/.venv
# bị copy hoặc đổi folder, launcher đó sẽ chết với lỗi:
#   Fatal error in launcher: Unable to create process ... The system cannot find the file specified
# Vì vậy ưu tiên gọi yt-dlp bằng CHÍNH Python đang chạy pipeline:
#   <current python.exe> -m yt_dlp
# Cách này cũng an toàn hơn với đường dẫn Unicode như "audio chúa".
try:
    import yt_dlp as _yt_dlp_module  # noqa: F401
    YTDLP_MODULE_AVAILABLE = True
except Exception:
    YTDLP_MODULE_AVAILABLE = False

YTDLP_EXE = find_program("yt-dlp.exe") or find_program("yt-dlp")
YTDLP_CMD = [sys.executable, "-m", "yt_dlp"] if YTDLP_MODULE_AVAILABLE else ([YTDLP_EXE] if YTDLP_EXE else [])
# Giữ tên YTDLP để tương thích code cũ/debug, nhưng code mới phải dùng YTDLP_CMD.
YTDLP = YTDLP_EXE or (sys.executable if YTDLP_MODULE_AVAILABLE else None)
FFMPEG = find_program("ffmpeg.exe") or find_program("ffmpeg")
FFPROBE = find_program("ffprobe.exe") or find_program("ffprobe")


def check_required_programs():
    missing = []

    if not YTDLP_CMD:
        missing.append("yt-dlp (module hoặc exe)")

    if not FFMPEG:
        missing.append("ffmpeg")

    if not FFPROBE:
        missing.append("ffprobe")

    if missing:
        print("\n❌ THIẾU CÁC CHƯƠNG TRÌNH SAU:")

        for item in missing:
            print(f"   - {item}")

        print("\nHãy đặt file .exe cùng thư mục code hoặc thêm vào PATH.")
        return False

    return True


# ============================================================
# QUẢN LÝ list.txt VÀ doneLink.txt
# ============================================================

def create_list_file():
    with open(LIST_FILE, "w", encoding="utf-8") as file:
        file.write("# Dán các link YouTube vào bên dưới, mỗi dòng một link\n")
        file.write("# https://www.youtube.com/watch?v=xxxxxxxxxxx\n")


def read_url_file(path):
    if not path.exists():
        return []

    urls = []
    seen = set()

    with open(path, "r", encoding="utf-8-sig") as file:
        for line in file:
            url = line.strip()

            if not url or url.startswith("#"):
                continue

            if url not in seen:
                seen.add(url)
                urls.append(url)

    return urls


def remove_url_from_list(video_url):
    if not LIST_FILE.exists():
        return

    original_lines = LIST_FILE.read_text(encoding="utf-8-sig").splitlines()
    remaining_lines = [
        line
        for line in original_lines
        if line.strip() != video_url
    ]

    temporary_file = LIST_FILE.with_suffix(".txt.tmp")
    content = "\n".join(remaining_lines).rstrip()

    if content:
        content += "\n"

    temporary_file.write_text(content, encoding="utf-8")
    os.replace(temporary_file, LIST_FILE)


def append_done_link(video_url):
    existing = set(read_url_file(DONE_LINK_FILE))

    if video_url in existing:
        return

    with open(DONE_LINK_FILE, "a", encoding="utf-8") as file:
        file.write(video_url + "\n")


def mark_link_done(video_url):
    """Chỉ gọi sau khi video đã xuất thành công."""

    append_done_link(video_url)
    remove_url_from_list(video_url)


def read_pending_urls():
    if not LIST_FILE.exists():
        create_list_file()
        print("\n📝 Đã tạo list.txt.")
        print("Hãy dán các link vào list.txt rồi chạy lại code.")

        if os.name == "nt":
            subprocess.run(["notepad.exe", str(LIST_FILE)])

        return []

    all_urls = read_url_file(LIST_FILE)

    if not all_urls:
        print("\n❌ list.txt không có link nào.")
        return []

    done_urls = set(read_url_file(DONE_LINK_FILE))
    already_done = [url for url in all_urls if url in done_urls]

    # Dọn các link đã có trong doneLink.txt khỏi list.txt.
    for url in already_done:
        remove_url_from_list(url)

    pending_urls = [url for url in all_urls if url not in done_urls]

    if already_done:
        print(
            f"\n⏭️ Đã bỏ {len(already_done)} link đã có trong doneLink.txt "
            "khỏi list.txt."
        )

    return pending_urls


# ============================================================
# JAVASCRIPT RUNTIME
# ============================================================

def get_javascript_arguments():
    deno = find_program("deno.exe") or find_program("deno")

    if deno:
        print("✅ JavaScript runtime: Deno")
        return [
            "--no-js-runtimes",
            "--js-runtimes",
            f"deno:{deno}"
        ]

    node = find_program("node.exe") or find_program("node")

    if node:
        print("✅ JavaScript runtime: Node.js")
        return [
            "--no-js-runtimes",
            "--js-runtimes",
            f"node:{node}"
        ]

    print("\n⚠️ Không tìm thấy Deno hoặc Node.js.")
    print("Pipeline vẫn thử yt-dlp không có JS runtime; nếu YouTube lỗi extraction hãy cài Node.js hoặc Deno.")
    return []


# ============================================================
# CẬP NHẬT YT-DLP
# ============================================================

def update_ytdlp():
    if not AUTO_UPDATE_YTDLP:
        return

    print("\n🔄 Đang kiểm tra cập nhật yt-dlp...")

    # Không gọi trực tiếp yt-dlp.exe khi đang dùng pip/.venv vì launcher có thể
    # chứa shebang tuyệt đối cũ sau khi project bị copy/move. Chỉ kiểm tra version.
    try:
        result = subprocess.run(
            [*YTDLP_CMD, "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        version = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0:
            print(f"✅ yt-dlp sẵn sàng: {version or 'OK'} | mode=python -m yt_dlp")
        else:
            print("⚠️ Không đọc được version yt-dlp; pipeline vẫn tiếp tục.")
    except Exception as exc:
        print(f"⚠️ Không kiểm tra được yt-dlp: {exc}")


# ============================================================
# QUẢN LÝ VIDEO THÔ
# ============================================================

def find_raw_video():
    for extension in VIDEO_EXTENSIONS:
        video_path = DOWNLOAD_DIR / f"{TEMP_VIDEO_NAME}{extension}"

        if video_path.exists() and video_path.stat().st_size > 0:
            return video_path

    return None


def delete_old_raw_files():
    for item in DOWNLOAD_DIR.glob(f"{TEMP_VIDEO_NAME}.*"):
        try:
            if item.is_file():
                item.unlink()
        except OSError:
            pass

    SOURCE_URL_FILE.unlink(missing_ok=True)
    SOURCE_TITLE_FILE.unlink(missing_ok=True)


def can_reuse_existing_video(video_url):
    if FORCE_REDOWNLOAD:
        return False

    raw_video = find_raw_video()

    if not raw_video or not SOURCE_URL_FILE.exists():
        return False

    try:
        old_url = SOURCE_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return False

    return old_url == video_url


# ============================================================
# TÊN FILE VÀ TIÊU ĐỀ VIDEO
# ============================================================

def clean_filename(title):
    title = str(title).strip()
    title = re.sub(r'[\\/:*?"<>|]', "", title)
    title = re.sub(r"\s+", " ", title)
    title = title.strip(" .-_")

    if not title:
        title = "video"

    reserved_names = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10))
    }

    if title.upper() in reserved_names:
        title = f"_{title}"

    return title[:180]


def read_saved_title():
    if not SOURCE_TITLE_FILE.exists():
        return None

    try:
        title = SOURCE_TITLE_FILE.read_text(encoding="utf-8").strip()
        return title or None
    except OSError:
        return None


def fetch_video_title(video_url, js_arguments, use_cookies=False):
    command = [
        *YTDLP_CMD,
        "--ignore-config",
        "--no-plugin-dirs",
        *js_arguments,
        "--remote-components",
        "ejs:github",
        "--no-playlist",
        "--force-ipv4",
        "--quiet",
        "--no-warnings",
        "--skip-download",
        "--print",
        "%(title)s"
    ]

    cookie_file = BASE_DIR / "cookies.txt"

    if use_cookies and cookie_file.exists():
        command.extend(["--cookies", str(cookie_file)])

    command.append(video_url)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return None

    lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]

    if not lines:
        return None

    return clean_filename(lines[-1])


def get_video_title(video_url, js_arguments):
    if can_reuse_existing_video(video_url):
        saved_title = read_saved_title()
        if saved_title:
            return saved_title

    # Không dùng cookies.txt tĩnh cũ ở bước lấy title. Cookie LIVE chỉ được
    # tạo từ Selenium ngay trước lúc download để tránh cookie bị rotate.
    title = fetch_video_title(video_url, js_arguments, use_cookies=False)
    return title or "video"


# ============================================================
# TẢI VIDEO
# ============================================================


def build_download_command(
    video_url,
    js_arguments,
    cookie_file=None,
    user_agent=None,
    extractor_args=None,
    force_ipv4=True,
):
    """Tạo lệnh yt-dlp. Không dùng cookies.txt tĩnh; cookie được truyền từ session LIVE."""
    output_template = str(DOWNLOAD_DIR / f"{TEMP_VIDEO_NAME}.%(ext)s")

    command = [
        *YTDLP_CMD,
        "--ignore-config",
        "--no-plugin-dirs",
        *js_arguments,
        "--remote-components", "ejs:github",
        "--no-playlist",
        "--geo-bypass",
        "--windows-filenames",
        "--force-overwrites",
        "--no-part",
        "--retries", str(DOWNLOAD_RETRIES),
        "--fragment-retries", str(FRAGMENT_RETRIES),
        "--retry-sleep", "2",
    ]

    if force_ipv4:
        command.append("--force-ipv4")

    if cookie_file:
        cookie_path = Path(cookie_file)
        if cookie_path.exists() and cookie_path.stat().st_size > 0:
            command.extend(["--cookies", str(cookie_path)])

    if user_agent:
        command.extend(["--user-agent", str(user_agent)])

    if extractor_args:
        command.extend(["--extractor-args", str(extractor_args)])

    command.extend([
        "-f", DOWNLOAD_FORMAT,
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", YTDLP_AUDIO_QUALITY,
        "-o", output_template,
        video_url,
    ])
    return command

def run_download(
    video_url,
    js_arguments,
    cookie_file=None,
    user_agent=None,
    extractor_args=None,
    force_ipv4=True,
    label="yt-dlp",
):
    command = build_download_command(
        video_url,
        js_arguments,
        cookie_file=cookie_file,
        user_agent=user_agent,
        extractor_args=extractor_args,
        force_ipv4=force_ipv4,
    )

    print(f"\n⬇️ {label}")
    if cookie_file:
        print(f"   🍪 Cookie LIVE: {cookie_file}")
    else:
        print("   🍪 Cookie: KHÔNG DÙNG")

    if extractor_args:
        print(f"   🧩 Extractor args: {extractor_args}")
    print(f"   🌐 IPv4 bắt buộc: {'CÓ' if force_ipv4 else 'KHÔNG'}")

    result = subprocess.run(command)

    if result.returncode != 0:
        return None

    return find_raw_video()



def download_video(
    video_url,
    js_arguments,
    original_title,
    cookie_file=None,
    user_agent=None,
):
    """
    Nhiều fallback download. Mỗi lượt xóa raw của lượt trước để tránh file hỏng bị reuse.

    Lượt:
      1) LIVE cookie + yt-dlp default
      2) LIVE cookie + web,web_embedded
      3) LIVE cookie + không ép IPv4
      4) public default
      5) public web_embedded
    """
    if can_reuse_existing_video(video_url):
        raw_video = find_raw_video()
        if raw_video and get_video_duration(raw_video):
            print("\n✅ Audio này đã được tải trước đó và đọc được duration.")
            print(f"📁 Dùng lại: {raw_video.name}")
            return raw_video

    attempts = []
    live_cookie = None
    if cookie_file:
        candidate = Path(cookie_file)
        if candidate.exists() and candidate.stat().st_size > 0:
            live_cookie = candidate

    if live_cookie:
        attempts.extend([
            {
                "cookie_file": live_cookie,
                "user_agent": user_agent,
                "extractor_args": None,
                "force_ipv4": True,
                "label": "LƯỢT 1/5 - COOKIE LIVE + yt-dlp mặc định...",
            },
            {
                "cookie_file": live_cookie,
                "user_agent": user_agent,
                "extractor_args": "youtube:player_client=web,web_embedded",
                "force_ipv4": True,
                "label": "LƯỢT 2/5 - COOKIE LIVE + web,web_embedded...",
            },
            {
                "cookie_file": live_cookie,
                "user_agent": user_agent,
                "extractor_args": None,
                "force_ipv4": False,
                "label": "LƯỢT 3/5 - COOKIE LIVE + không ép IPv4...",
            },
        ])

    attempts.extend([
        {
            "cookie_file": None,
            "user_agent": user_agent,
            "extractor_args": None,
            "force_ipv4": True,
            "label": "PUBLIC + yt-dlp mặc định...",
        },
        {
            "cookie_file": None,
            "user_agent": user_agent,
            "extractor_args": "youtube:player_client=web_embedded",
            "force_ipv4": False,
            "label": "PUBLIC + web_embedded + không ép IPv4...",
        },
    ])

    raw_video = None
    for attempt_number, params in enumerate(attempts, start=1):
        delete_old_raw_files()
        params["label"] = f"LƯỢT {attempt_number}/{len(attempts)} - {params['label']}"
        raw_video = run_download(
            video_url,
            js_arguments,
            cookie_file=params["cookie_file"],
            user_agent=params["user_agent"],
            extractor_args=params["extractor_args"],
            force_ipv4=params["force_ipv4"],
            label=params["label"],
        )

        # Không chỉ nhìn exit code: ffprobe phải đọc được duration.
        if raw_video:
            duration = get_video_duration(raw_video)
            if duration and duration > 0:
                break
            print("⚠️ File tải ra không đọc được duration; xóa và thử lượt khác.")
            delete_old_raw_files()
            raw_video = None

        print(f"⚠️ Lượt tải {attempt_number} thất bại; thử phương án tiếp theo...")

    if not raw_video:
        print("\n❌ Không tải được audio gốc sau tất cả phương án.")
        if live_cookie:
            print("Cookie LIVE đã được dùng. Nếu vẫn HTTP 403, có thể YouTube đổi player/PO Token/media authorization.")
        return None

    try:
        SOURCE_URL_FILE.write_text(video_url, encoding="utf-8")
        SOURCE_TITLE_FILE.write_text(original_title, encoding="utf-8")
    except OSError:
        pass

    print("\n✅ Tải MP3 thành công:")
    print(f"📁 {raw_video}")
    return raw_video

def timestamp_to_seconds(timestamp):
    parts = timestamp.strip().split(":")

    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None

    if len(numbers) == 2:
        minutes, seconds = numbers

        if seconds >= 60:
            return None

        return minutes * 60 + seconds

    if len(numbers) == 3:
        hours, minutes, seconds = numbers

        if minutes >= 60 or seconds >= 60:
            return None

        return hours * 3600 + minutes * 60 + seconds

    return None


def seconds_to_timestamp(total_seconds):
    total_seconds = max(0, int(round(total_seconds)))

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# ============================================================
# DÁN TIMESTAMP TRỰC TIẾP TRONG CONSOLE
# ============================================================

def normalize_command_text(text):
    text = text.replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return text.upper().strip()


def parse_cut_ranges_from_lines(lines):
    # Hỗ trợ cả:
    # 00:00:00 --> 00:04:11
    # 01:34:29 --> END
    # END nghĩa là cắt từ mốc bắt đầu đến hết video.
    time_token = r"\d{1,4}:\d{2}(?::\d{2})?"
    end_token = rf"(?:{time_token}|END)"

    range_pattern = re.compile(
        rf"({time_token})\s*(?:-->|-|–|—|TO)\s*({end_token})",
        re.IGNORECASE
    )

    cut_ranges = []
    invalid_lines = []
    no_cut_requested = False

    for original_line in lines:
        line = original_line.strip()

        if not line or line.startswith("```"):
            continue

        normalized = normalize_command_text(line)

        if (
            "KHONG CO DOAN CAN CAT" in normalized
            or normalized in {"NONE", "NO CUT", "KEEP ALL", "GIU NGUYEN"}
        ):
            no_cut_requested = True
            continue

        match = range_pattern.search(line)

        if not match:
            # Bỏ qua các tiêu đề thường gặp thay vì coi là lỗi nghiêm trọng.
            if any(word in normalized for word in (
                "DOAN CAN CAT",
                "TONG KET",
                "INTRO",
                "OUTRO",
                "QUANG CAO"
            )):
                continue

            if line.startswith("#"):
                continue

            invalid_lines.append(line)
            continue

        start_text, end_text = match.groups()
        start_seconds = timestamp_to_seconds(start_text)

        # Dùng vô cực làm dấu hiệu "đến hết video".
        # normalize_cut_ranges() sẽ tự giới hạn về đúng thời lượng video.
        if normalize_command_text(end_text) == "END":
            end_seconds = float("inf")
        else:
            end_seconds = timestamp_to_seconds(end_text)

        if (
            start_seconds is None
            or end_seconds is None
            or end_seconds <= start_seconds
        ):
            invalid_lines.append(line)
            continue

        cut_ranges.append((float(start_seconds), float(end_seconds)))

    if no_cut_requested and cut_ranges:
        no_cut_requested = False

    return cut_ranges, no_cut_requested, invalid_lines


def read_cut_ranges_from_console(video_number, total_videos, title):
    while True:
        print("\n=====================================================")
        print(f"📝 VIDEO {video_number}/{total_videos}: {title}")
        print("Dán các khoảng CẦN CẮT BỎ trực tiếp vào cửa sổ này.")
        print("Mỗi dòng ví dụ: 00:00:00 --> 00:01:44")
        print("Có thể dùng: 01:34:29 --> END để cắt đến hết video.")
        print("")
        print("Sau khi dán xong, gõ DONE rồi nhấn Enter.")
        print("Gõ NONE nếu video không có đoạn nào cần cắt.")
        print("Gõ SKIP để bỏ qua link này.")
        print("Gõ QUIT để dừng chương trình.")
        print("=====================================================")

        pasted_lines = []

        while True:
            try:
                line = input()
            except EOFError:
                line = "DONE"

            command = normalize_command_text(line)

            if command in {"DONE", "XONG"}:
                break

            if command in {"SKIP", "BO QUA", "BOQUA"}:
                return "skip", [], False

            if command in {"QUIT", "EXIT", "THOAT"}:
                return "quit", [], False

            if command in {"NONE", "NO CUT", "KEEP ALL", "GIU NGUYEN"}:
                return "process", [], True

            # Dòng trống trong nội dung dán được bỏ qua; chỉ DONE mới kết thúc.
            if not line.strip():
                continue

            pasted_lines.append(line)

        cut_ranges, keep_all, invalid_lines = parse_cut_ranges_from_lines(
            pasted_lines
        )

        if invalid_lines:
            print("\n⚠️ Một số dòng không được nhận diện và đã bị bỏ qua:")
            for bad_line in invalid_lines[:10]:
                print(f"   - {bad_line}")

            if len(invalid_lines) > 10:
                print(f"   ... còn {len(invalid_lines) - 10} dòng khác")

        if cut_ranges or keep_all:
            return "process", cut_ranges, keep_all

        print("\n❌ Không tìm thấy khoảng thời gian hợp lệ.")
        print("Hãy dán lại, hoặc gõ NONE / SKIP / QUIT.")


# ============================================================
# CHUẨN HÓA KHOẢNG CẮT VÀ TÍNH PHẦN GIỮ
# ============================================================

def normalize_cut_ranges(cut_ranges, video_duration):
    valid_ranges = []

    for start_seconds, end_seconds in cut_ranges:
        if start_seconds >= video_duration:
            continue

        start_seconds = max(0.0, start_seconds)
        end_seconds = min(float(video_duration), end_seconds)

        if end_seconds > start_seconds:
            valid_ranges.append((start_seconds, end_seconds))

    valid_ranges.sort(key=lambda item: item[0])
    merged = []

    for start_seconds, end_seconds in valid_ranges:
        if not merged or start_seconds > merged[-1][1] + 0.05:
            merged.append([start_seconds, end_seconds])
        else:
            merged[-1][1] = max(merged[-1][1], end_seconds)

    return [(start, end) for start, end in merged]


def build_keep_ranges(cut_ranges, video_duration):
    keep_ranges = []
    cursor = 0.0

    for cut_start, cut_end in cut_ranges:
        if cut_start - cursor >= MIN_KEEP_SECONDS:
            keep_ranges.append((cursor, cut_start))

        cursor = max(cursor, cut_end)

    if video_duration - cursor >= MIN_KEEP_SECONDS:
        keep_ranges.append((cursor, float(video_duration)))

    return keep_ranges


# ============================================================
# THÔNG TIN VIDEO
# ============================================================

def get_video_duration(video_path):
    command = [
        FFPROBE,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return None

    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


# ============================================================
# RENDER AUDIO CÁC PHẦN CẦN GIỮ
# ============================================================

def clear_work_dir():
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)

    WORK_DIR.mkdir(parents=True, exist_ok=True)


def render_keep_segment(
    raw_video,
    start_seconds,
    end_seconds,
    output_file,
    use_nvenc=False,
):
    """Fallback: render một đoạn audio thành MP3 128k."""
    duration = end_seconds - start_seconds

    command = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{start_seconds:.3f}",
        "-i", str(raw_video),
        "-t", f"{duration:.3f}",
        "-map", "0:a:0",
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", AUDIO_BITRATE,
        "-ar", "48000",
        "-avoid_negative_ts", "make_zero",
        str(output_file),
    ]

    result = subprocess.run(command)

    return (
        result.returncode == 0
        and output_file.exists()
        and output_file.stat().st_size > 0
    )


def render_all_keep_segments(raw_video, keep_ranges):
    """Fallback ổn định nếu one-pass filter thất bại."""
    segment_files = []

    for index, (start_seconds, end_seconds) in enumerate(keep_ranges, start=1):
        segment_file = WORK_DIR / f"keep_{index:04d}.mp3"

        print(
            f"🎧 [{index}/{len(keep_ranges)}] Giữ "
            f"{seconds_to_timestamp(start_seconds)} → "
            f"{seconds_to_timestamp(end_seconds)}"
        )

        success = render_keep_segment(
            raw_video,
            start_seconds,
            end_seconds,
            segment_file,
        )

        if not success:
            print(f"   ❌ Render audio thất bại tại đoạn {index}.")
            return None

        segment_files.append(segment_file)

    return segment_files


def render_all_keep_segments_cpu(raw_video, keep_ranges):
    # Giữ tên hàm để tương thích code cũ; audio không dùng NVENC.
    return render_all_keep_segments(raw_video, keep_ranges)


# ============================================================
# GHÉP AUDIO CÁC PHẦN CÒN LẠI
# ============================================================

def escape_concat_path(path):
    normalized = path.resolve().as_posix()
    return normalized.replace("'", "'\\''")


def merge_segments(segment_files, temporary_output):
    if not segment_files:
        return False

    if len(segment_files) == 1:
        shutil.copy2(segment_files[0], temporary_output)
        return (
            temporary_output.exists()
            and temporary_output.stat().st_size > 0
        )

    concat_file = WORK_DIR / "concat_list.txt"

    with open(concat_file, "w", encoding="utf-8") as file:
        for segment_file in segment_files:
            file.write(f"file '{escape_concat_path(segment_file)}'\n")

    # Các segment đều do cùng encoder/bitrate/sample-rate tạo ra nên có thể concat stream-copy.
    command = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c:a", "copy",
        "-vn",
        str(temporary_output),
    ]

    result = subprocess.run(command)

    return (
        result.returncode == 0
        and temporary_output.exists()
        and temporary_output.stat().st_size > 0
    )


def render_audio_one_pass(raw_audio, keep_ranges, temporary_output):
    """
    Cách chính: decode audio một lần, atrim từng đoạn cần giữ, concat trong filter graph,
    rồi encode MP3 đúng MỘT lần. Nhanh hơn và tránh encode từng đoạn nhiều lần.
    """
    if not keep_ranges:
        return False

    filters = []
    labels = []
    for i, (start_seconds, end_seconds) in enumerate(keep_ranges):
        label = f"a{i}"
        labels.append(f"[{label}]")
        filters.append(
            f"[0:a:0]atrim=start={start_seconds:.3f}:end={end_seconds:.3f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )

    if len(keep_ranges) == 1:
        filter_complex = filters[0]
        out_map = f"[{ 'a0' }]"
    else:
        concat = "".join(labels) + f"concat=n={len(keep_ranges)}:v=0:a=1[outa]"
        filter_complex = ";".join(filters + [concat])
        out_map = "[outa]"

    command = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(raw_audio),
        "-filter_complex", filter_complex,
        "-map", out_map,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", AUDIO_BITRATE,
        "-ar", "48000",
        str(temporary_output),
    ]

    result = subprocess.run(command)
    return (
        result.returncode == 0
        and temporary_output.exists()
        and temporary_output.stat().st_size > 0
    )


def move_final_to_done(temporary_output, original_title, output_dir=None, video_id=None):
    """Chuyển MP3 cuối vào thư mục đích; gắn VIDEO_ID để không ghi đè title trùng."""
    target_dir = Path(output_dir) if output_dir else DONE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    base_title = clean_filename(original_title)
    if video_id:
        final_name = f"{base_title} [{clean_filename(video_id)}].mp3"
    else:
        final_name = f"{base_title}.mp3"

    final_path = target_dir / final_name

    if final_path.exists():
        final_path.unlink()

    shutil.move(str(temporary_output), str(final_path))
    return final_path


# ============================================================
# XỬ LÝ MỘT AUDIO
# ============================================================


def process_video(raw_video, original_title, cut_ranges, keep_all=False, output_dir=None, video_id=None):
    """Tên hàm giữ nguyên để auto_youtube_chatgpt_cut.py tương thích; thực tế xử lý MP3."""
    video_duration = get_video_duration(raw_video)
    if video_duration is None or video_duration <= 0:
        print("\n❌ Không đọc được thời lượng audio.")
        return None

    if keep_all:
        normalized_cuts = []
        keep_ranges = [(0.0, float(video_duration))]
    else:
        normalized_cuts = normalize_cut_ranges(cut_ranges, video_duration)
        if not normalized_cuts:
            print("\n❌ Không có khoảng cắt hợp lệ nằm trong thời lượng audio.")
            return None
        keep_ranges = build_keep_ranges(normalized_cuts, video_duration)

    if not keep_ranges:
        print("\n❌ Các khoảng cắt đã bao phủ toàn bộ audio.")
        return None

    print("\n=====================================================")
    print(f"🎧 Audio gốc: {original_title}")
    print(f"🕒 Thời lượng: {seconds_to_timestamp(video_duration)}")

    if keep_all:
        print("✂️ NONE: giữ toàn bộ MP3, copy trực tiếp không encode lại.")
    else:
        print(f"✂️ Số khoảng cần bỏ: {len(normalized_cuts)}")
        print(f"✅ Số phần audio sẽ giữ: {len(keep_ranges)}")
    print("=====================================================\n")

    if normalized_cuts:
        print("CÁC KHOẢNG SẼ CẮT BỎ:")
        for start_seconds, end_seconds in normalized_cuts:
            print(
                f"   ❌ {seconds_to_timestamp(start_seconds)} --> "
                f"{seconds_to_timestamp(end_seconds)}"
            )
        print("")

    clear_work_dir()
    temporary_output = WORK_DIR / "final_merged.mp3"

    if keep_all:
        # yt-dlp đã post-process thành MP3; copy byte-for-byte là nhanh nhất.
        try:
            shutil.copy2(raw_video, temporary_output)
        except OSError as error:
            print(f"❌ Copy MP3 thất bại: {error}")
            return None
    else:
        print("🎚️ Đang cắt/ghép audio one-pass và encode MP3 128k...")
        success = render_audio_one_pass(raw_video, keep_ranges, temporary_output)

        if not success:
            print("⚠️ One-pass audio thất bại; fallback render từng đoạn rồi concat...")
            temporary_output.unlink(missing_ok=True)
            segment_files = render_all_keep_segments(raw_video, keep_ranges)
            if not segment_files:
                print("❌ Không tạo được các đoạn audio cần giữ.")
                return None
            if not merge_segments(segment_files, temporary_output):
                print("❌ Ghép audio thất bại.")
                return None

    temp_duration = get_video_duration(temporary_output)
    if temp_duration is None or temp_duration <= 0:
        print("❌ File MP3 output tạm không hợp lệ.")
        return None

    try:
        final_path = move_final_to_done(
            temporary_output,
            original_title,
            output_dir=output_dir,
            video_id=video_id,
        )
    except OSError as error:
        print(f"\n❌ Không chuyển được MP3 vào thư mục done: {error}")
        return None

    final_duration = get_video_duration(final_path)
    if final_duration is None or final_duration <= 0:
        print("❌ MP3 cuối không đọc được duration.")
        return None

    # Sanity duration.
    if keep_all and abs(final_duration - video_duration) > 3.0:
        print(
            f"⚠️ Cảnh báo duration NONE lệch {abs(final_duration-video_duration):.2f}s "
            "so với audio gốc."
        )
    if not keep_all and final_duration > video_duration + 2.0:
        print("❌ Duration MP3 cuối dài hơn audio gốc bất thường.")
        return None

    print("\n=====================================================")
    print("✅ ĐÃ CẮT/GHÉP MP3 THÀNH CÔNG")
    print(f"📄 Tên file: {final_path.name}")
    print(f"📁 Đã chuyển vào: {final_path.parent}")
    print(f"🕒 Thời lượng mới: {seconds_to_timestamp(final_duration)}")
    print("=====================================================")

    clear_work_dir()
    return final_path

def cleanup_after_success(raw_video):
    clear_work_dir()

    if DELETE_RAW_AFTER_DONE:
        try:
            raw_video.unlink(missing_ok=True)
            SOURCE_URL_FILE.unlink(missing_ok=True)
            SOURCE_TITLE_FILE.unlink(missing_ok=True)
            print("🗑️ Đã xóa MP3 thô của link vừa xử lý.")
        except OSError as error:
            print(f"⚠️ Không xóa được MP3 thô: {error}")


# ============================================================
# XỬ LÝ HÀNG LOẠT
# ============================================================

def process_all_links(pending_urls, js_arguments):
    total = len(pending_urls)
    success_count = 0
    failed_count = 0
    skipped_count = 0

    for index, video_url in enumerate(pending_urls, start=1):
        print("\n\n#####################################################")
        print(f"🔄 LINK {index}/{total}")
        print(video_url)
        print("#####################################################")

        original_title = get_video_title(video_url, js_arguments)
        print(f"\n🏷️ Tiêu đề YouTube: {original_title}")

        raw_video = download_video(
            video_url,
            js_arguments,
            original_title
        )

        if not raw_video:
            failed_count += 1
            print("⏭️ Tự chuyển sang link tiếp theo.")
            continue

        status, cut_ranges, keep_all = read_cut_ranges_from_console(
            index,
            total,
            original_title
        )

        if status == "quit":
            raise UserQuit()

        if status == "skip":
            skipped_count += 1
            print("\n⏭️ Đã bỏ qua link này; link vẫn nằm trong list.txt.")
            continue

        final_path = process_video(
            raw_video,
            original_title,
            cut_ranges,
            keep_all=keep_all
        )

        if not final_path:
            failed_count += 1
            print("\n❌ Link chưa hoàn tất nên vẫn được giữ trong list.txt.")
            print("⏭️ Tự chuyển sang link tiếp theo.")
            continue

        try:
            mark_link_done(video_url)
            print("✅ Đã chuyển link từ list.txt sang doneLink.txt.")
        except OSError as error:
            print(f"⚠️ Video đã xuất xong nhưng chưa cập nhật được link: {error}")

        cleanup_after_success(raw_video)
        success_count += 1

        if index < total:
            print("\n➡️ Đang chuyển sang link tiếp theo...")

    return success_count, failed_count, skipped_count


# ============================================================
# CHẠY CHƯƠNG TRÌNH
# ============================================================

def main():
    configure_console()

    print("=====================================================")
    print(" YOUTUBE STORY-ONLY BATCH CUTTER")
    print(" DÁN TIMESTAMP TRỰC TIẾP + XỬ LÝ HÀNG LOẠT")
    print("=====================================================")

    if not check_required_programs():
        return

    pending_urls = read_pending_urls()

    if not pending_urls:
        print("\n✅ Không còn link nào cần xử lý.")
        return

    js_arguments = get_javascript_arguments()

    if not js_arguments:
        return

    update_ytdlp()

    print(f"\n📋 Có {len(pending_urls)} link đang chờ xử lý.")

    success_count, failed_count, skipped_count = process_all_links(
        pending_urls,
        js_arguments
    )

    print("\n\n=====================================================")
    print("📊 KẾT QUẢ TOÀN BỘ DANH SÁCH")
    print(f"✅ Thành công: {success_count}")
    print(f"❌ Thất bại: {failed_count}")
    print(f"⏭️ Bỏ qua: {skipped_count}")
    print(f"📁 Video hoàn chỉnh: {DONE_DIR}")
    print(f"📄 Link hoàn thành: {DONE_LINK_FILE}")
    print("=====================================================")

    if success_count > 0 and os.name == "nt":
        os.startfile(DONE_DIR)


if __name__ == "__main__":
    try:
        main()

    except UserQuit:
        print("\n\n⛔ Đã dừng theo yêu cầu. Link chưa xong vẫn ở list.txt.")

    except KeyboardInterrupt:
        print("\n\n⛔ Bạn đã dừng chương trình bằng Ctrl+C.")

    except Exception as error:
        print("\n❌ Chương trình gặp lỗi:")
        print(error)

    input("\nNhấn Enter để đóng chương trình...")