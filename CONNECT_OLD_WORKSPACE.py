# -*- coding: utf-8 -*-
import json
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
OUT = APP_DIR / "workspace_bridge.json"

def clean_input(raw):
    raw = (raw or "").strip().strip('"').strip("'")
    return raw

def count_lines(path):
    try:
        return sum(
            1 for x in path.read_text(
                encoding="utf-8-sig", errors="replace"
            ).splitlines()
            if x.strip() and not x.lstrip().startswith("#")
        )
    except Exception:
        return 0

def urls_from_failed(path):
    found = set()
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            url = str(obj.get("url") or "").strip() if isinstance(obj, dict) else ""
            if url:
                found.add(url)
                continue
        except Exception:
            pass
        for url in re.findall(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=[A-Za-z0-9_-]+|youtu\.be/[A-Za-z0-9_-]+)[^\s\"'<>]*", line):
            found.add(url)
    return found

def urls_from_text(path):
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out

raw = " ".join(sys.argv[1:]).strip()
if not raw:
    print("=" * 72)
    print("KẾT NỐI PROJECT DEEPSEEK VỚI DATA PROJECT CŨ")
    print("=" * 72)
    print("Dán đường dẫn THƯ MỤC project cũ, ví dụ:")
    print(r"E:\audio chúa\story_pipeline_bundle\God Miracles Today 1111\cut-moments-by-AI-GPT")
    raw = input("\nOLD PROJECT FOLDER: ")

old = Path(clean_input(raw)).expanduser()
if not old.is_absolute():
    old = old.resolve()

if not old.exists() or not old.is_dir():
    raise SystemExit(f"❌ Không tồn tại folder: {old}")

signals = [
    old / "channels",
    old / "list.txt",
    old / "doneLink.txt",
    old / "failedLink.jsonl",
    old / "chrome_profiles" / "youtube",
]
if not any(p.exists() for p in signals):
    print("⚠️ Folder này chưa thấy các state phổ biến của pipeline cũ.")
    answer = input("Vẫn kết nối? Y/N: ").strip().upper()
    if answer != "Y":
        raise SystemExit("Đã hủy.")

payload = {
    "mode": "reuse_in_place",
    "data_root": str(old.resolve()),
    "reuse_youtube_profile": True,
}
OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

done = urls_from_text(old / "doneLink.txt")
failed = urls_from_failed(old / "failedLink.jsonl")
unresolved = failed - done

cuts = 0
finals = 0
transcripts = 0
channels = old / "channels"
if channels.exists():
    cuts = sum(1 for _ in channels.rglob("*_CUTS.txt"))
    finals = sum(1 for _ in channels.rglob("done/*.mp3"))
    transcripts = sum(1 for _ in channels.rglob("*_TRANSCRIPT.txt"))

raws = []
downloads = old / "downloads"
if downloads.exists():
    raws = [p for p in downloads.glob("raw_audio.*") if p.is_file() and p.suffix.lower() not in {".txt"}]

print("\n✅ ĐÃ NỐI WORKSPACE CŨ - KHÔNG COPY DATA")
print(f"🗂️ Data root: {old}")
print(f"📋 list.txt: {count_lines(old / 'list.txt'):,}")
print(f"✅ doneLink: {len(done):,}")
print(f"❌ failed history: {len(failed):,}")
print(f"🔁 failed chưa DONE: {len(unresolved):,}")
print(f"🧠 *_CUTS.txt: {cuts:,}")
print(f"🎵 final MP3: {finals:,}")
print(f"📝 transcript cache: {transcripts:,}")
print(f"⏯️ raw_audio dở: {len(raws):,}")
print(f"📄 Bridge config: {OUT}")
print()
print("DeepSeek project sẽ dùng DATA cũ trực tiếp.")
print("KHÔNG chạy code ChatGPT cũ và code DeepSeek mới cùng lúc.")
