ASYNC MEDIA PIPELINE - CUỐN CHIẾU GPT + DOWNLOAD/CUT

Mục tiêu:
GPT xong video N thì không chờ yt-dlp/ffmpeg nữa.
Ngay lập tức chuyển sang transcript/GPT video N+1.

Kiến trúc:
MAIN/AI THREAD
  video N: transcript -> GPT -> validate CUT -> snapshot cookie -> enqueue
  video N+1: transcript -> GPT -> ...

MEDIA WORKER (1 thread)
  video N: download audio-only -> MP3 -> FFmpeg cut/merge -> verify -> DONE

Tại cùng thời điểm có thể:
  GPT/video N+1
  download/cut video N

An toàn:
- MEDIA_WORKER_COUNT = 1 vì story_cutter_core dùng chung DOWNLOAD_DIR/raw_audio.
- Queue max = 2 để AI không chạy quá xa download.
- Cookie được snapshot thành file RIÊNG cho từng media job.
- Media worker KHÔNG sử dụng Selenium driver.
- doneLink chỉ được ghi sau khi MP3 cuối verify duration > 0.
- Full-cut 00:00:00 --> END vẫn DONE ngay, không download.
- Existing final vẫn recovery DONE ngay.
- Link fail phase mới vẫn bị hoãn về cuối batch.
- Trước phase RETRY, code đợi media của toàn bộ link mới xử lý xong để biết fail thật.
- Retry fail nữa thì để lần chạy sau.

Tốc độ/mạng:
- 1 media worker để tránh download/ffmpeg tranh quá mạnh với Chrome.
- concurrent fragments giảm 3 -> 2 để cân bằng mạng khi chạy song song.
- queue max 2 tạo backpressure: nếu media quá chậm, AI chỉ đi trước tối đa khoảng 2 job.

Chỉ chép đè auto_youtube_chatgpt_cut.py.
