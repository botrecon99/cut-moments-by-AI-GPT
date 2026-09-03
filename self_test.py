# -*- coding: utf-8 -*-
from pathlib import Path
import shutil
import subprocess
import sys

BASE = Path(__file__).resolve().parent

print('=' * 68)
print('SELF TEST - AUTO YOUTUBE CHATGPT PIPELINE')
print('=' * 68)

checks = []

def check(name, ok, detail=''):
    checks.append(bool(ok))
    print(('✅' if ok else '❌'), name, detail)

check('prompt.txt', (BASE / 'prompt.txt').exists() and (BASE / 'prompt.txt').stat().st_size > 100)
check('auto_youtube_chatgpt_cut.py', (BASE / 'auto_youtube_chatgpt_cut.py').exists())
check('story_cutter_core.py', (BASE / 'story_cutter_core.py').exists())

for prog in ('ffmpeg', 'ffprobe', 'yt-dlp'):
    local = BASE / (prog + '.exe')
    found = str(local) if local.exists() else shutil.which(prog) or shutil.which(prog + '.exe')
    check(prog, bool(found), found or 'KHÔNG TÌM THẤY')

node = shutil.which('node') or shutil.which('node.exe')
deno = shutil.which('deno') or shutil.which('deno.exe')
check('Node hoặc Deno (khuyến nghị)', bool(node or deno), node or deno or 'không có - yt-dlp vẫn sẽ thử')

chrome_candidates = [
    Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
    Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
]
chrome = next((p for p in chrome_candidates if p.exists()), None)
check('Google Chrome', bool(chrome), str(chrome or 'KHÔNG TÌM THẤY'))

for name in ('youtube', 'chatgpt'):
    root = BASE / 'chrome_profiles' / name
    check(f'profile dir {name}', root.exists(), str(root))

# Syntax test bằng đúng Python hiện tại.
try:
    subprocess.run([
        sys.executable, '-m', 'py_compile',
        str(BASE / 'auto_youtube_chatgpt_cut.py'),
        str(BASE / 'story_cutter_core.py')
    ], check=True)
    check('Python syntax', True)
except Exception as exc:
    check('Python syntax', False, str(exc))

print('-' * 68)
if all(checks[:5]) and checks[-1]:
    print('SELF TEST CƠ BẢN: OK')
else:
    print('SELF TEST: CÒN MỤC CẦN SỬA Ở TRÊN')
