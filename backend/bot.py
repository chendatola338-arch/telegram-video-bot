import os
import re
import gc
import json
import uuid
import subprocess
import sys

# Add backend directory to sys.path before sibling imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import telebot
from telebot import types
from dotenv import load_dotenv
import shutil
import html
import time
import requests
from database import init_db, get_user_status, activate_user
from downloader import get_video_info, download_video_to_temp, DownloadCancelledException, TEMP_DIR

import threading

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL", "http://localhost:8000")

# Lazy Telethon settings (imported only when sending files >50MB to keep startup RAM ultra-low)
API_ID = 6
API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"
SESSION_DIR = "/app/data" if os.path.exists("/app/data") else os.path.dirname(os.path.abspath(__file__))
telethon_upload_lock = threading.Lock()

def safe_print(message):
    """Print message safely to console with immediate flush, avoiding UnicodeEncodeError on Windows"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        try:
            # Encode to console codepage or fall back to ASCII
            print(str(message).encode('ascii', errors='ignore').decode('ascii'), flush=True)
        except Exception:
            pass

if not TOKEN:
    safe_print("[Error] TELEGRAM_BOT_TOKEN is not set in the .env file.")
    exit(1)

bot = telebot.TeleBot(TOKEN, num_threads=4)
_BOT_USERNAME = None

def get_bot_username():
    global _BOT_USERNAME
    if not _BOT_USERNAME:
        _BOT_USERNAME = bot.get_me().username
    return _BOT_USERNAME

def build_video_caption(title, platform):
    return (
        f"🎬 <b>\u1785\u17c6\u178e\u1784\u17cb\u1787\u17be\u1784 (ចុចដើម្បី Copy)៖</b>\n"
        f"<code>{html.escape(title)}</code>\n"
        f"📍 មកពី៖ {html.escape(platform)}\n\n"
        f"🤖 @{get_bot_username()}"
    )

def cleanup_stale_temp_dirs():
    """Remove any leftover job directories in TEMP_DIR from previous interrupted runs."""
    try:
        for entry in os.listdir(TEMP_DIR):
            if entry.startswith("job_"):
                shutil.rmtree(os.path.join(TEMP_DIR, entry), ignore_errors=True)
    except Exception:
        pass

# Initialize subscription database
init_db()

PAYMENT_API_TOKEN = os.getenv("PAYMENT_API_TOKEN")
PAYMENT_AMOUNT = float(os.getenv("PAYMENT_AMOUNT", "1.99"))
PAYMENT_DURATION_DAYS = int(os.getenv("PAYMENT_DURATION_DAYS", "30"))

# Active payments polling tracker (chat_id)
active_payments_polling = set()

# Increase connection and read/write timeouts to prevent timeouts during large video uploads
telebot.apihelper.CONNECT_TIMEOUT = 90
telebot.apihelper.READ_TIMEOUT = 300

# URL matching regex
URL_REGEX = r'(https?://[^\s]+)'

# In-memory dictionaries to track state of active downloads for cancellation and rate limiting
active_downloads = {}  # chat_id -> True/False (True = active, False = cancelled)
download_semaphore = threading.Semaphore(1)  # Strict concurrency limiter to keep RAM and disk within container limits
last_update_time = {}  # chat_id -> float (timestamp)
last_percent = {}      # chat_id -> float

def get_welcome_message():
    msg = (
        "👋 <b>សួស្តី! ខ្ញុំជា Bot ទាញយកវីដេអូ (Video Downloader Bot)</b>\n\n"
        "ខ្ញុំអាចទាញយកវីដេអូពីកម្មវិធីជាច្រើនដូចជា៖\n"
        "👉 <b>Kuaishou, Rednote (Xiaohongshu), Facebook, YouTube, TikTok, DramaBox</b>\n\n"
        "📥 <b>របៀបប្រើប្រាស់៖</b>\n"
        "គ្រាន់តែផ្ញើ <b>Link (តំណភ្ជាប់)</b> វីដេអូមកកាន់ខ្ញុំ ខ្ញុំនឹងទាញយកវីដេអូនោះផ្ញើជូនអ្នកភ្លាមៗ!"
    )
    if MINI_APP_URL.startswith("https://"):
        msg += "\n\n👇 ឬចុចប៊ូតុងខាងក្រោមដើម្បីបើកកម្មវិធី <b>Mini App</b> ៖"
    return msg

def get_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    if MINI_APP_URL.startswith("https://"):
        web_app = types.WebAppInfo(url=MINI_APP_URL)
        btn_mini_app = types.InlineKeyboardButton(text="📱 បើក Mini App", web_app=web_app)
        markup.add(btn_mini_app)
    btn_help = types.InlineKeyboardButton(text="ℹ️ របៀបប្រើប្រាស់", callback_data="help_info")
    markup.add(btn_help)
    return markup

def render_progress_bar(percent):
    """Draw a graphical text-based progress bar"""
    filled_length = int(round(10 * percent / 100))
    bar = "█" * filled_length + "░" * (10 - filled_length)
    return bar

TRANSLATION_MAP = {
    "快手": "Kuaishou Video",
    "TikTok": "TikTok Video",
    "抖音": "Douyin Video",
    "小红书": "Xiaohongshu Video",
    "Instagram": "Instagram Video"
}

def translate_to_khmer(title):
    """Translate Chinese/English titles to Khmer using Google Translate API under deep-translator"""
    if not title or title.strip() == "Video":
        return title
    if title.strip() in TRANSLATION_MAP:
        return TRANSLATION_MAP[title.strip()]
    try:
        from deep_translator import GoogleTranslator
        translated = GoogleTranslator(source='auto', target='km').translate(title)
        if translated:
            return translated
    except Exception as e:
        safe_print(f"Translation failed for '{title}': {e}")
    return title

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    status, expiry = get_user_status(chat_id)
    
    if status != 'active':
        prompt_payment(chat_id, message.from_user)
        return
        
    status_text = f"\U0001f7e2 \u1782\u178e\u1793\u17b8\u179f\u1780\u1798\u17d2\u1798 (Active) - \u1795\u17bb\u178f\u1780\u17c6\u178e\u178f\u17cb\u1793\u17c5\u17d6 {expiry}" if expiry else "\U0001f7e2 \u1782\u178e\u1793\u17b8\u179f\u1780\u1798\u17d2\u1798 (Active) - \u1794\u17d2\u179a\u17be\u1794\u17d2\u179a\u17b6\u1780\u17cb\u17a5\u178f\u178a\u17c2\u1793\u1780\u17c6\u178e\u178f\u17cb (Lifetime)"
    msg = get_welcome_message() + f"\n\n\U0001f4ca <b>\u179f\u17d2\u1790\u17b6\u1793\u1797\u17b6\u1796\u1782\u178e\u1793\u17b8\u17d6</b> {status_text}"
    
    bot.send_message(
        chat_id,
        msg,
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = "\u2139\ufe0f <b>\u179a\u1794\u17c0\u1794\u1794\u17d2\u179a\u17be\u1794\u17d2\u179a\u17b6\u1780\u17cb Bot \u1791\u17b6\u1789\u1799\u1780\u179c\u17b8\u178a\u17c1\u17a2\u17bc\u17d6</b>\n\n1\ufe0f\u20e3 \u1785\u17bc\u179b\u1791\u17c5\u1780\u17b6\u1793\u17cb\u1780\u1798\u17d2\u1798\u179c\u17b7\u1792\u17b8\u178e\u17b6\u1798\u17bd\u1799 (YouTube, TikTok, XHS...) \u179a\u17bd\\u1785\u1785\u1798\u17d2\\u179b\\u1784\u178f\u17c6\\u178e\\u1797\\u1787\\u17b6\\u1794\\u17cb (Copy Link) \u1793\\u17c3\u179c\\u17b8\\u178a\\u17c1\\u17a2\\u17bc\n2\ufe0f\u20e3 \u1795\\u17d2\\u1789\\u17be\u178f\\u17c6\\u178e\\u1797\\u1787\\u17b6\\u1794\\u17cb (Link) \u1793\\u17c4\\u17c7\\u1785\\u17bc\\u179b\\u1798\\u1780\\u1780\\u17d2\\u1793\\u17bb\\u1784 Chat \u1793\\u17c1\\u17c7\n3\ufe0f\u20e3 \u179a\\u1784\\u17cb\\u1785\\u17b6\\u17c6\\u1798\\u17bd\\u1799\\u1797\\u17d2\\u179b\\u17c2\\u178f \u1781\\u17d2\\u1789\\u17bb\\u17c6\\u1793\\u17b9\\u1784\\u179a\\u17c0\\u1794\\u1785\\u17c6 \u1793\\u17b7\\u1784\\u1795\\u17d2\\u1789\\u17be\\u179c\\u17b8\\u178a\\u17c1\\u17a2\\u17bc\\u1796\\u17c1\\u1789\\u1787\\u17bc\\u1793\\u17a2\\u17d2\\u1793\\u1780\\u178a\\u17c4\\u1799\\u179f\\u17d2\\u179c\\u17d0\\u1799\\u1794\\u17d2\\u179a\\u179c\\u178f\\u17d2\\u178f"
    bot.send_message(message.chat.id, help_text, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "help_info")
def callback_help(call):
    help_text = (
        "ℹ️ <b>របៀបប្រើប្រាស់ Bot ទាញយកវីដេអូ៖</b>\n\n"
        "1️⃣ ចូលទៅកាន់កម្មវិធីណាមួយ (YouTube, TikTok, XHS...) រួចចម្លងតំណភ្ជាប់ (Copy Link) នៃវីដេអូ\n"
        "2️⃣ ផ្ញើតំណភ្ជាប់ (Link) នោះចូលមកក្នុង Chat នេះ\n"
        "3️⃣ រង់ចាំមួយភ្លែត ខ្ញុំនឹងរៀបចំ និងផ្ញើវីដេអូពេញជូនអ្នកដោយស្វ័យប្រវត្ត"
    )
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, help_text, parse_mode="HTML")

def delayed_delete(chat_id, status_msg_id, original_msg_id=None, delay=4.0):
    def delete():
        time.sleep(delay)
        try:
            bot.delete_message(chat_id, status_msg_id)
        except Exception:
            pass
        if original_msg_id:
            try:
                bot.delete_message(chat_id, original_msg_id)
            except Exception:
                pass
    import threading
    threading.Thread(target=delete, daemon=True).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_download:"))
def callback_cancel(call):
    chat_id = int(call.data.split(":")[1])
    active_downloads[chat_id] = False  # Set flag to stop download loop
    bot.answer_callback_query(call.id, text="🛑 កំពុងបោះបង់ការទាញយក...")
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text="🛑 <b>ការទាញយកត្រូវបានបោះបង់! (Download Cancelled)</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        safe_print(f"Error handling cancel edit: {e}")
        
    # Start delayed deletion of the messages after 4 seconds
    original_msg_id = None
    if call.message.reply_to_message:
        original_msg_id = call.message.reply_to_message.message_id
    delayed_delete(chat_id, call.message.message_id, original_msg_id, delay=4.0)

def update_upload_progress(chat_id, status_msg, platform, title, bytes_sent, total_bytes, state):
    """Throttle Telegram upload progress edits and abort cleanly if cancelled."""
    if active_downloads.get(chat_id) is False:
        raise DownloadCancelledException()
    if not total_bytes:
        return
    percent = min(100.0, (bytes_sent / total_bytes) * 100)
    now = time.time()
    if (percent - state["percent"] >= 10) or (now - state["time"] >= 1.5):
        state["percent"] = percent
        state["time"] = now
        bar = render_progress_bar(percent)
        progress_text = (
            f"📤 ស្វែងរកឃើញវីដេអូពី <b>{html.escape(platform)}</b>!\n"
            f"🎬 ចំណងជើង៖ <i>{html.escape(title)}</i>\n\n"
            f"⚙️ កំពុងបញ្ជូនវីដេអូទៅ Telegram... <b>{percent:.1f}%</b>\n"
            f"<code>{bar}</code>"
        )
        try:
            cancel_markup = types.InlineKeyboardMarkup()
            cancel_markup.add(types.InlineKeyboardButton(text="❌ បោះបង់ (Cancel)", callback_data=f"cancel_download:{chat_id}"))
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=progress_text,
                parse_mode="HTML",
                reply_markup=cancel_markup
            )
        except Exception:
            pass

class ChunkedMultipartStream:
    """File-like stream emitting multipart/form-data in small chunks with exact Content-Length."""

    def __init__(self, fields, file_field, filepath, thumb_path=None, progress_cb=None):
        self.boundary = f"----TgBotStream{uuid.uuid4().hex}"
        self.progress_cb = progress_cb
        self.segments = []

        for k, v in fields.items():
            if v is None:
                continue
            hdr = (
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n"
            ).encode("utf-8")
            self.segments.append(("bytes", hdr))

        if thumb_path and os.path.exists(thumb_path):
            thumb_sz = os.path.getsize(thumb_path)
            thumb_hdr = (
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="thumbnail"; filename="thumb.jpg"\r\n'
                f"Content-Type: image/jpeg\r\n\r\n"
            ).encode("utf-8")
            self.segments.append(("bytes", thumb_hdr))
            self.segments.append(("file", thumb_path, thumb_sz, False))
            self.segments.append(("bytes", b"\r\n"))

        self.file_size = os.path.getsize(filepath)
        filename = os.path.basename(filepath)
        file_hdr = (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode("utf-8")
        self.segments.append(("bytes", file_hdr))
        self.segments.append(("file", filepath, self.file_size, True))
        self.segments.append(("bytes", f"\r\n--{self.boundary}--\r\n".encode("utf-8")))

        self.total_length = sum(len(s[1]) if s[0] == "bytes" else s[2] for s in self.segments)
        self._seg_idx = 0
        self._byte_offset = 0
        self._open_file = None
        self._sent_video = 0

    @property
    def content_type(self):
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self):
        return self.total_length

    def read(self, size=-1):
        want = None if (size is None or size < 0) else size
        if want == 0:
            return b""
        out = bytearray()
        while (want is None or len(out) < want) and self._seg_idx < len(self.segments):
            seg = self.segments[self._seg_idx]
            remaining = 65536 if want is None else (want - len(out))
            if seg[0] == "bytes":
                data = seg[1]
                chunk = data[self._byte_offset:self._byte_offset + remaining]
                out.extend(chunk)
                self._byte_offset += len(chunk)
                if self._byte_offset >= len(data):
                    self._seg_idx += 1
                    self._byte_offset = 0
            else:
                _, path, _, track = seg
                if self._open_file is None:
                    self._open_file = open(path, "rb")
                chunk = self._open_file.read(remaining)
                if chunk:
                    out.extend(chunk)
                    if track and self.progress_cb:
                        self._sent_video += len(chunk)
                        self.progress_cb(self._sent_video, self.file_size)
                else:
                    self._open_file.close()
                    self._open_file = None
                    self._seg_idx += 1
        return bytes(out)

    def close(self):
        if self._open_file is not None:
            try:
                self._open_file.close()
            except OSError:
                pass
            self._open_file = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

def stream_send_video_bot_api(chat_id, video_path, caption, status_msg, platform, title, duration=0, width=0, height=0, thumb_path=None):
    """Stream video <=50MB to Telegram Bot API in small chunks without loading file into memory."""
    state = {"percent": 0.0, "time": 0.0}
    fields = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": "true",
    }
    if duration:
        fields["duration"] = str(int(duration))
    if width:
        fields["width"] = str(int(width))
    if height:
        fields["height"] = str(int(height))

    with ChunkedMultipartStream(
        fields,
        "video",
        video_path,
        thumb_path=thumb_path,
        progress_cb=lambda s, t: update_upload_progress(chat_id, status_msg, platform, title, s, t, state),
    ) as stream:
        res = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendVideo",
            data=stream,
            headers={
                "Content-Type": stream.content_type,
                "Content-Length": str(len(stream)),
            },
            timeout=(30, 300),
        )
    payload = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
    if res.status_code != 200 or not payload.get("ok"):
        raise Exception(payload.get("description") or f"Telegram API HTTP {res.status_code}")

def get_video_metadata(video_path):
    """Extract duration, width, height using ffprobe without loading video into memory."""
    duration, width, height = 0, 0, 0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            video_path,
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if res.returncode == 0:
            data = json.loads(res.stdout.decode("utf-8", errors="ignore") or "{}")
            streams = data.get("streams") or []
            if streams:
                s = streams[0]
                width = int(s.get("width") or 0)
                height = int(s.get("height") or 0)
                if s.get("duration"):
                    duration = int(float(s["duration"]))
            if not duration:
                fmt = data.get("format") or {}
                if fmt.get("duration"):
                    duration = int(float(fmt["duration"]))
    except Exception as e:
        safe_print(f"ffprobe failed: {e}")
    return duration, width, height

def create_video_thumbnail(video_path, output_dir):
    """Extract a single frame JPEG thumbnail using ffmpeg."""
    thumb_path = os.path.join(output_dir, "thumb.jpg")
    try:
        for ts in ("00:00:01", "00:00:00"):
            cmd = [
                "ffmpeg", "-y", "-ss", ts, "-i", video_path,
                "-frames:v", "1", "-vf", "scale='min(320,iw)':-2",
                "-q:v", "5", thumb_path,
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if res.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                return thumb_path
    except Exception as e:
        safe_print(f"ffmpeg thumbnail generation failed: {e}")
    return None

def send_large_video_via_telethon(chat_id, video_path, caption, status_msg, platform, title, username=None, duration=0, width=0, height=0, thumb_path=None):
    """Lazy-load Telethon only for >50MB videos and stream from disk in 256KB chunks."""
    import asyncio
    from telethon import TelegramClient
    from telethon.tl.types import DocumentAttributeVideo

    bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg.message_id,
        text="📤 កំពុងបញ្ជូនវីដេអូទំហំធំទៅកាន់ Telegram... 🚀"
    )

    state = {"percent": 0.0, "time": 0.0}

    async def upload_task():
        client = TelegramClient(os.path.join(SESSION_DIR, 'bot_session'), API_ID, API_HASH)
        try:
            await client.start(bot_token=TOKEN)
            target = chat_id
            try:
                target = await client.get_input_entity(chat_id)
            except Exception:
                if username:
                    target = await client.get_input_entity(username)

            attrs = None
            if duration > 0 or (width > 0 and height > 0):
                attrs = [DocumentAttributeVideo(
                    duration=duration,
                    w=width or 1,
                    h=height or 1,
                    supports_streaming=True,
                )]

            await client.send_file(
                target,
                video_path,
                caption=caption,
                parse_mode="html",
                supports_streaming=True,
                attributes=attrs,
                thumb=thumb_path,
                part_size_kb=256,
                progress_callback=lambda s, t: update_upload_progress(chat_id, status_msg, platform, title, s, t, state),
            )
        finally:
            await client.disconnect()

    with telethon_upload_lock:
        asyncio.run(asyncio.wait_for(upload_task(), timeout=900))

def poll_payment_status(chat_id, md5, qr_msg_id, username, first_name):
    poll_url = f"https://www.payment-system.dev/api/v1/?type=check_md5&api_token={PAYMENT_API_TOKEN}&md5={md5}"
    start_time = time.time()
    timeout = 300  # 5 minutes
    
    while time.time() - start_time < timeout:
        time.sleep(5)
        try:
            res = requests.get(poll_url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                status = data.get("status")
                
                if status == "success":
                    # Payment successful!
                    # Activate user in database for 30 days
                    activate_user(chat_id, username, first_name, duration_days=PAYMENT_DURATION_DAYS)
                    
                    # Send success message
                    msg_text = (
                        "\u1f389 <b>\u1780\\u17b6\\u179a\\u1791\\u17bc\\u1791\\u17b6\\u178f\\u17cb\\u1791\\u1791\\u17bd\\u179b\\u1794\\u17b6\\u1793\\u1787\\u17c4\\u1782\\u1787\\u17d0\\u1799!</b>\n\n"
                        f"\u1782\\u178e\\u1793\\u17b8\\u179a\\u1794\\u179f\\u17cb\\u17a2\\u17d2\\u1793\\u1780\\u178f\\u17d2\\u179a\\u17bc\\u179c\\u1794\\u17b6\\u1793\\u1794\\u17be\\u1780\\u179f\\u17b7\\u1791\\u17d2\\u1792\\u17b7\\u1794\\u17d2\\u179a\\u17be\\u1794\\u17d2\\u179a\\u17b6\\u179f\\u17cb\\u179a\\u1799\\u17c8\\u1796\\u17c1\\u179b <b>{PAYMENT_DURATION_DAYS} \u1790\\u17d2\\u1784\\u17c3</b> \u179a\\u17bd\\u1785\\u179a\\u17b6\\u179b\\u17cb\\u17a0\\u17be\\u1799\\u1794\\u17b6\\u1791\\u17d4\n"
                        "\u179f\\u17bc\\u1798\\u1795\\u17d2\\u1789\\u17be Link \\u179c\\u17b8\\u178a\\u17c1\\u17a2\\u17bc\\u1798\\u17d2\\u178f\\u1784\\u1791\\u17c0\\u178f\\u178a\\u17be\\u1798\\u17d2\\u1794\\u17b8\\u1794\\u1784\\u1799\\u1780!"
                    )
                    bot.send_message(chat_id, msg_text, parse_mode="HTML")
                    
                    # Delete QR message
                    try:
                        bot.delete_message(chat_id, qr_msg_id)
                    except Exception:
                        pass
                    return
                    
                elif status == "expired" or status == "failed":
                    # Expired or failed
                    msg_text = (
                        "\u274c <b>QR Code \\u179f\\u1798\\u17d2\\u179a\\u17b6\\u179b\\u1795\\u17bb\\u178f\\u1780\\u17c6\\u178e\\u178f\\u17cb (\\u17ac\\u1794\\u179a\\u17b6\\u1787\\u17d0\\u1799)</b>\n\n"
                        "\u179f\\u17bc\\u1798\\u1795\\u17d2\\u1789\\u17be Link \\u179c\\u17b8\\u178a\\u17c1\\u17a2\\u17bc\\u1798\\u17d2\\u178f\\u1784\\u1791\\u17c0\\u178f\\u178a\\u17be\\u1798\\u17d2\\u1794\\u17b8\\u1794\\u1784\\u17d2\\u1780\\u17be\\u178f QR \\u1790\\u17d2\\u1798\\u17b8\\u1794\\u17b6\\u1791\\u17d4"
                    )
                    bot.send_message(chat_id, msg_text, parse_mode="HTML")
                    try:
                        bot.delete_message(chat_id, qr_msg_id)
                    except Exception:
                        pass
                    return
        except Exception as e:
            safe_print(f"Polling error for chat_id {chat_id}: {e}")
            
    # Timeout reached
    try:
        msg_text = (
            "\u274c <b>\\u17a2\\u179f\\u17cb\\u179a\\u1799\\u17c8\\u1796\\u17c1\\u179b\\u179a\\u1784\\u17cb\\u1785\\u17b6\\u17c6\\u1794\\u1784\\u17cb\\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb (Timeout)</b>\n\n"
            "\u179f\\u17bc\\u1798\\u1795\\u17d2\\u1789\\u17be Link \\u1798\\u17d2\\u178f\\u1784\\u1791\\u17c0\\u178f\\u178a\\u17be\\u1798\\u17d2\\u1794\\u17b8\\u1794\\u1784\\u17d2\\u1780\\u17be\\u178f QR Code \\u1790\\u17d2\\u1798\\u17b8\\u17d4"
        )
        bot.send_message(chat_id, msg_text, parse_mode="HTML")
        bot.delete_message(chat_id, qr_msg_id)
    except Exception:
        pass

def prompt_payment(chat_id, user_obj, reply_to_message=None):
    # Check if already polling
    if chat_id in active_payments_polling:
        msg_text = (
            "\u26a0\ufe0f \u179b\u17c4\\u1780\\u17a2\\u17d2\\u1793\\u1780\\u1798\\u17b6\\u1793\\u1780\\u17bc\\u178a\\u1791\\u17bc\\u1791\\u17b6\\u178f\\u17cb\\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb\\u178a\\u17c2\\u179b\\u1780\\u17c6\\u1796\\u17bb\\u1784\\u179a\\u1784\\u17cb\\u1785\\u17b6\\u17c6\\u179a\\u17bd\\u1785\\u17a0\\u17be\\u1799\\u17d4\n\n"
            "\u179f\\u17bc\\u1798\\u1792\\u17d2\\u179c\\u17be\\u1780\\u17b6\\u179a\\u179f\\u17d2\\u1780\\u17c1\\u1793\\u1794\\u1784\\u17cb\\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb\\u178f\\u17b6\\u1798 QR Code \\u1781\\u17b6\\u1784\\u179b\\u17be \\u17ac\\u179a\\u1784\\u17cb\\u1785\\u17b6\\u17c6\\u179c\\u17b6\\u17a0\\u17bd\\u179f\\u1780\\u17c6\\u178e\\u178f\\u17cb (\\u17e5 \\u1793\\u17b6\\u1791\\u17b8)!"
        )
        if reply_to_message:
            bot.reply_to(reply_to_message, msg_text)
        else:
            bot.send_message(chat_id, msg_text)
        return
        
    loading_text = "\u23f3 \u1780\u17c6\u1796\u17bb\u1784\u1794\u1784\u17d2\u1780\u17be\u178f KHQR Code \u179f\u1798\u17d2\u179a\u17b6\u179b\\u17cb\u1794\\u1784\\u17cb\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb... \U0001f504"
    if reply_to_message:
        status_msg = bot.reply_to(reply_to_message, loading_text)
    else:
        status_msg = bot.send_message(chat_id, loading_text)
        
    try:
        gen_url = f"https://www.payment-system.dev/api/v1/?type=generate_qr&api_token={PAYMENT_API_TOKEN}&amount={PAYMENT_AMOUNT}"
        res = requests.get(gen_url, timeout=10)
        if res.status_code == 201:
            data = res.json()
            qr_info = data.get("data", [{}])[0]
            link_qr_code = qr_info.get("link_qr_code")
            md5 = qr_info.get("md5")
            
            if link_qr_code and md5:
                bot.delete_message(chat_id, status_msg.message_id)
                
                caption = (
                    "\u26a0\ufe0f <b>\u1782\\u178e\\u1793\\u17b8\\u179a\\u1794\\u179f\\u17cb\\u17a2\\u17d2\\u1793\\u1780\\u1798\\u17b7\\u1793\\u1791\\u17b6\\u1793\\u17cb\\u1794\\u17b6\\u1793\\u1794\\u1784\\u17cb\\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb\\u1791\\u17c1 (\\u17ac\\u17a0\\u17bd\\u179f\\u1780\\u17c6\\u178e\\u178f\\u17cb)</b>\n\n"
                    f"\U0001f4b5 \u1785\\u17c6\\u1793\\u17bd\\u1793\\u1791\\u17b9\\u1780\\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb\\u17d6 <b>${PAYMENT_AMOUNT:.2f} USD / \u17e3\u17e0\\u1790\\u17d2\\u1784\\u17c3</b>\n\n"
                    "\U0001f449 \u179f\\u17bc\\u1798\\u179f\\u17d2\\u1780\\u17c1\\u1793 KHQR \\u1781\\u17b6\\u1784\\u1780\\u17d2\\u179a\\u17c4\\u1798\\u178a\\u17be\\u1798\\u17d2\\u1794\\u17b8\\u1794\\u1784\\u17cb\\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb\\u1792\\u17d2\\u179c\\u17be\\u1780\\u17b6\\u179a\\u1794\\u17be\\u1780\\u178a\\u17c6\\u178e\\u17be\\u179a\\u1780\\u17b6\\u179a Bot \\u179f\\u17d2\\u179c\\u17d0\\u1799\\u1794\\u17d2\\u179a\\u179c\\u178f\\u17d2\\u178f\\u1797\\u17d2\\u179b\\u17b6\\u1798\\u17d7\\u17d6\n\n"
                    "\u23f3 <i>QR Code \\u1793\\u17c1\\u17c7\\u1798\\u17b6\\u1793\\u179f\\u17bb\\u1796\\u179b\\u1797\\u17b6\\u1796\\u179a\\u1799\\u17c8\\u1796\\u17c1\\u179b \\u17e5\\u1793\\u17b6\\u1791\\u17b8\\u17d4 \\u1794\\u17d2\\u179a\\u1796\\u17d0\\u1793\\u17d2\\u1792\\u1780\\u17c6\\u1796\\u17bb\\u1784\\u179a\\u1784\\u17cb\\u1785\\u17b6\\u17c6\\u1780\\u17b6\\u179a\\u1794\\u1784\\u17cb\\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb\\u1796\\u17b8\\u179b\\u17c4\\u1780\\u17a2\\u17d2\\u1793\\u1780...</i>"
                )
                qr_msg = bot.send_photo(
                    chat_id,
                    photo=link_qr_code,
                    caption=caption,
                    parse_mode="HTML"
                )
                
                active_payments_polling.add(chat_id)
                username = user_obj.username or "Unknown"
                first_name = user_obj.first_name or "Unknown"
                
                def run_polling():
                    try:
                        poll_payment_status(chat_id, md5, qr_msg.message_id, username, first_name)
                    finally:
                        active_payments_polling.discard(chat_id)
                        
                threading.Thread(target=run_polling, daemon=True).start()
                return
                
        raise Exception(f"API status {res.status_code}")
    except Exception as e:
        safe_print(f"Error generating payment QR: {e}")
        err_text = "\u274c <b>\u1798\\u17b6\\u179b\\u1794\\u1789\\u17d2\\u17a0\\u17b6\\u1794\\u1785\\u17d2\\u1785\\u17c1\\u1780\\u1791\\u17c1\\u179f\\u1780\\u17d2\\u1793\\u17bb\\u1784\\u1780\\u17b6\\u179a\\u1794\\u1784\\u17d2\\u1780\\u17be\\u178f QR Code \\u179f\\u1798\\u17d2\\u179a\\u17b6\\u179b\\u17cb\\u1794\\u1784\\u17cb\\u1794\\u17d2\\u179a\\u17b6\\u1780\\u17cb\\u17d4</b>\n\n\u179f\\u17bc\\u1798\\u179f\\u17b6\\u1780\\u179b\\u17d2\\u1794\\u1784\\u1798\\u17d2\\u178f\\u1784\\u1791\\u17c0\\u178f\\u1793\\u17c5\\u1796\\u17c1\\u179b\\u1780\\u17d2\\u179a\\u17c4\\u1799\\u1794\\u17b6\\u1791\\u17d4"
        bot.send_message(chat_id, err_text, parse_mode="HTML")

def check_for_url(message):
    text = message.text or message.caption
    if not text:
        return False
    return bool(re.search(URL_REGEX, text))

@bot.message_handler(func=check_for_url, content_types=['text', 'photo', 'video', 'document'])
def handle_link(message):
    text = message.text or message.caption
    urls = re.findall(URL_REGEX, text)
    if not urls:
        return
        
    url = urls[0]
    chat_id = message.chat.id
    
    # 1. CHECK SUBSCRIPTION STATUS
    user_status, expiry = get_user_status(chat_id)
    if user_status != 'active':
        prompt_payment(chat_id, message.from_user, reply_to_message=message)
        return
        
    # If active, proceed to download
    outcome = "PENDING"
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    log_file_path = os.path.join(log_dir, "bot_activity.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Received from {chat_id}: {url}\n")
    except Exception as le:
        safe_print(f"Failed to write log: {le}")
    
    # Initialize download tracking variables
    active_downloads[chat_id] = True
    last_update_time[chat_id] = 0
    last_percent[chat_id] = 0
    
    # Create Cancel keyboard markup
    cancel_markup = types.InlineKeyboardMarkup()
    btn_cancel = types.InlineKeyboardButton(text="❌ បោះបង់ (Cancel)", callback_data=f"cancel_download:{chat_id}")
    cancel_markup.add(btn_cancel)
    
    safe_print(f"Processing link from {chat_id}: {url}")
    # Send initial status message with cancel button
    try:
        status_msg = bot.reply_to(
            message,
            "🔍 កំពុងពិនិត្យ និងស្វែងរកវីដេអូ... 🔄",
            reply_markup=cancel_markup
        )
    except Exception:
        status_msg = bot.send_message(
            chat_id,
            "🔍 កំពុងពិនិត្យ និងស្វែងរកវីដេអូ... 🔄",
            reply_markup=cancel_markup
        )
    
    download_semaphore.acquire()
    local_file = None
    job_dir = os.path.join(TEMP_DIR, f"job_{chat_id}_{uuid.uuid4().hex[:8]}")
    try:
        os.makedirs(job_dir, exist_ok=True)
        # Extract video info to check if readable
        info = get_video_info(url)
        original_title = info.get('title', 'Video')
        title = translate_to_khmer(original_title)
        if title and len(title) > 750:
            title = title[:750] + "..."
        platform = info.get('platform', 'Unknown')
        
        # Check size in metadata first before starting download (1GB limit)
        filesize = info.get('filesize')
        if filesize and filesize > 1024 * 1024 * 1024:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"⚠️ <b>វីដេអូមានទំហំធំពេក (ទំហំ {(filesize / (1024*1024*1024)):.1f}GB)</b>\n\nBot មិនអាចទាញយកវីដេអូដែលមានទំហំលើសពី 1GB បានទេ ដើម្បីរក្សាស្ថិរភាពប្រព័ន្ធ។",
                parse_mode="HTML"
            )
            outcome = "LIMIT (File over 1GB)"
            return
            
        # Check if it is a slideshow (album of photos)
        if info.get('is_slideshow') and info.get('images'):
            images = info['images']
            audio_url = info.get('audio_url')
            
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"📸 ស្វែងរកឃើញរូបថតចំនួន <b>{len(images)}</b> សន្លឹកពី TikTok!\n🎬 ចំណងជើង៖ <i>{html.escape(title)}</i>\n\n⚙️ កំពុងទាញយក និងរៀបចំបញ្ជូន... ⏳",
                parse_mode="HTML",
                reply_markup=cancel_markup
            )
            
            # Download images to temp files
            temp_images = []
            audio_path = None
            compiled_video_path = None
            
            try:
                import requests
                # 1. Download images
                for idx, img_url in enumerate(images):
                    if active_downloads.get(chat_id) is False:
                        raise DownloadCancelledException()
                    res = requests.get(img_url, timeout=15)
                    res.raise_for_status()
                    img_path = os.path.join(TEMP_DIR, f"slide_{chat_id}_{idx}.jpg")
                    with open(img_path, 'wb') as f:
                        f.write(res.content)
                    temp_images.append(img_path)
                
                # 2. Download audio if available
                if audio_url:
                    if active_downloads.get(chat_id) is False:
                        raise DownloadCancelledException()
                    res_aud = requests.get(audio_url, timeout=20)
                    if res_aud.status_code == 200:
                        audio_path = os.path.join(TEMP_DIR, f"audio_{chat_id}.mp3")
                        with open(audio_path, 'wb') as f:
                            f.write(res_aud.content)
                
                if active_downloads.get(chat_id) is False:
                    raise DownloadCancelledException()
                
                # 3. Compile slideshow video using FFmpeg if audio is downloaded and slides exist
                if temp_images and audio_path:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text="⚙️ កំពុងបំប្លែងរូបថតទៅជាវីដេអូជាមួយតន្ត្រី... 🎞️",
                        parse_mode="HTML",
                        reply_markup=cancel_markup
                    )
                    
                    compiled_video_path = os.path.join(TEMP_DIR, f"compiled_{chat_id}.mp4")
                    num_slides = len(temp_images)
                    slide_duration = 3.0 # seconds per slide
                    
                    cmd = ["ffmpeg", "-y"]
                    for img in temp_images:
                        cmd.extend(["-loop", "1", "-t", str(slide_duration), "-i", img])
                    cmd.extend(["-i", audio_path])
                    
                    if num_slides >= 2:
                        filter_inputs = "".join(f"[{i}:v]" for i in range(num_slides))
                        filter_complex = f"{filter_inputs}concat=n={num_slides}:v=1:a=0[v]"
                        cmd.extend([
                            "-filter_complex", filter_complex,
                            "-map", "[v]",
                            "-map", f"{num_slides}:a"
                        ])
                    else:
                        cmd.extend([
                            "-map", "0:v",
                            "-map", "1:a"
                        ])
                        
                    cmd.extend([
                        "-c:v", "libx264",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "aac",
                        "-shortest",
                        compiled_video_path
                    ])
                    
                    import subprocess
                    res_run = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    
                    if res_run.returncode == 0 and os.path.exists(compiled_video_path):
                        # Successfully compiled! Send the compiled video to the user
                        bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_msg.message_id,
                            text="📤 កំពុងបញ្ជូនវីដេអូស្លាយទៅកាន់ Telegram... 🚀"
                        )
                        
                        stream_send_video_bot_api(
                            chat_id,
                            compiled_video_path,
                            build_video_caption(title, platform),
                            status_msg,
                            platform,
                            title,
                        )
                            
                        # Delete status and user link messages
                        bot.delete_message(chat_id, status_msg.message_id)
                        try:
                            bot.delete_message(chat_id, message.message_id)
                        except Exception:
                            pass
                        outcome = "SUCCESS (Slideshow Video)"
                        return
                
                # Fallback to sending as photo album if compilation fails or no audio is present
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    text="📤 កំពុងបញ្ជូនរូបថតទាំងអស់ទៅកាន់ Telegram... 🚀"
                )
                media_groups = [temp_images[i:i + 10] for i in range(0, len(temp_images), 10)]
                for group in media_groups:
                    media_list = []
                    for idx, path in enumerate(group):
                        with open(path, 'rb') as f:
                            caption = build_video_caption(title, platform) if idx == 0 and group == media_groups[0] else None
                            media_list.append(types.InputMediaPhoto(f.read(), caption=caption, parse_mode="HTML"))
                    bot.send_media_group(chat_id, media_list, timeout=120)
                
                bot.delete_message(chat_id, status_msg.message_id)
                try:
                    bot.delete_message(chat_id, message.message_id)
                except Exception:
                    pass
                outcome = "SUCCESS (Slideshow Photos)"
                return
                    
            finally:
                # Cleanup all temp files
                for path in temp_images:
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                if audio_path and os.path.exists(audio_path):
                    try:
                        os.remove(audio_path)
                    except Exception:
                        pass
                if compiled_video_path and os.path.exists(compiled_video_path):
                    try:
                        os.remove(compiled_video_path)
                    except Exception:
                        pass
            return
            
        # Check cancellation before proceeding
        if active_downloads.get(chat_id) is False:
            raise DownloadCancelledException()
            
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"📥 ស្វែងរកឃើញវីដេអូពី <b>{html.escape(platform)}</b>!\n🎬 ចំណងជើង៖ <i>{html.escape(title)}</i>\n\n⚙️ កំពុងទាញយកវីដេអូ... ⏳",
            parse_mode="HTML",
            reply_markup=cancel_markup
        )
        
        # Define progress hook function
        def progress_callback(downloaded, total):
            # Check size limit
            if total > 1024 * 1024 * 1024:
                active_downloads[chat_id] = False
                raise Exception("Video size exceeds 1GB limit.")
            # Check cancellation first
            if active_downloads.get(chat_id) is False:
                raise DownloadCancelledException()
                
            percent = (downloaded / total) * 100
            now = time.time()
            
            # Rate limit Telegram API requests: only edit message if percentage increase is >= 15% or 1.5 seconds have elapsed
            if (percent - last_percent.get(chat_id, 0) >= 15) or (now - last_update_time.get(chat_id, 0) >= 1.5):
                last_percent[chat_id] = percent
                last_update_time[chat_id] = now
                
                bar = render_progress_bar(percent)
                progress_text = (
                    f"📥 ស្វែងរកឃើញវីដេអូពី <b>{html.escape(platform)}</b>!\n"
                    f"🎬 ចំណងជើង៖ <i>{html.escape(title)}</i>\n\n"
                    f"⚙️ កំពុងទាញយកវីដេអូ... <b>{percent:.1f}%</b>\n"
                    f"<code>{bar}</code>"
                )
                try:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text=progress_text,
                        parse_mode="HTML",
                        reply_markup=cancel_markup
                    )
                except Exception as e:
                    # Ignore API errors like 'message is not modified'
                    pass
        
        # Stream video directly to isolated per-job temp directory (never loaded into memory)
        local_file = download_video_to_temp(
            url,
            progress_callback=progress_callback,
            cancel_check=lambda: active_downloads.get(chat_id) is False,
            output_dir=job_dir,
            extracted_info=info,
        )

        # Verify if user cancelled during the final moments of download
        if active_downloads.get(chat_id) is False:
            raise DownloadCancelledException()

        # Check file size (Telegram bot limit is 50MB)
        file_size_mb = os.path.getsize(local_file) / (1024 * 1024)
        duration, width, height = get_video_metadata(local_file)
        if not duration:
            duration = int(info.get('duration') or 0)
        if not width:
            width = int(info.get('width') or 0)
        if not height:
            height = int(info.get('height') or 0)
        thumb_path = create_video_thumbnail(local_file, job_dir)
        if not thumb_path and info.get('thumbnail'):
            try:
                th_res = requests.get(info['thumbnail'], headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
                if th_res.status_code == 200 and len(th_res.content) > 100:
                    th_file = os.path.join(job_dir, "thumb.jpg")
                    with open(th_file, 'wb') as tf:
                        tf.write(th_res.content)
                    thumb_path = th_file
            except Exception:
                pass
        video_caption = build_video_caption(title, platform)
        sender_username = getattr(message.from_user, "username", None)

        if file_size_mb > 1024: # 1GB limit
            direct_dl_url = info.get('video_url') or url
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"⚠️ <b>វីដេអូធំពេក (ទំហំ {file_size_mb:.1f}MB)</b>\n\nBot មិនអាចផ្ញើឯកសារលើសពី 1GB បានទេ ដើម្បីរក្សាស្ថិរភាពប្រព័ន្ធ។\n\n👇 សូមចុចលើតំណភ្ជាប់ខាងក្រោមដើម្បីទាញយកផ្ទាល់៖\n<a href=\"{html.escape(direct_dl_url)}\">🔗 ចុចទីនេះដើម្បីទាញយកវីដេអូ</a>",
                parse_mode="HTML"
            )
            outcome = "LIMIT (Direct Link Sent)"
            return

        if file_size_mb > 50:
            # Files >50MB require MTProto (Telethon) in 256KB chunks
            send_large_video_via_telethon(
                chat_id,
                local_file,
                video_caption,
                status_msg,
                platform,
                title,
                username=sender_username,
                duration=duration,
                width=width,
                height=height,
                thumb_path=thumb_path,
            )
            outcome = "SUCCESS (Telethon Video)"
        else:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text="📤 កំពុងបញ្ជូនវីដេអូទៅកាន់ Telegram... 🚀"
            )
            stream_send_video_bot_api(
                chat_id,
                local_file,
                video_caption,
                status_msg,
                platform,
                title,
                duration=duration,
                width=width,
                height=height,
                thumb_path=thumb_path,
            )
            outcome = "SUCCESS (Streamed Video)"

        # Delete status message
        bot.delete_message(chat_id, status_msg.message_id)

        # Delete the original user's message containing the link
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception as e:
            safe_print(f"Failed to delete user link message: {e}")
            
    except DownloadCancelledException:
        # Download was aborted by user. Clean up local files.
        safe_print(f"Download cancelled for chat_id {chat_id}")
        # The status message is already updated to "Cancelled" in callback_cancel.
        outcome = "CANCELLED"
        
    except Exception as e:
        safe_print(f"Error handling link {url}: {e}")
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"❌ <b>មានបញ្ហាក្នុងការទាញយក៖</b>\n\n<code>{html.escape(str(e)[:150])}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        outcome = f"ERROR: {str(e)}"
    finally:
        # Write final outcome to log
        try:
            with open(log_file_path, "a", encoding="utf-8") as lf:
                lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Outcome for {url}: {outcome}\n")
        except Exception:
            pass
            
        # Immediately delete temp video, thumbnail, and any partial chunks
        if local_file and os.path.exists(local_file):
            try:
                os.remove(local_file)
            except Exception as e:
                safe_print(f"Error deleting temp file {local_file}: {e}")
        shutil.rmtree(job_dir, ignore_errors=True)
        # Clear states and reclaim memory promptly
        active_downloads.pop(chat_id, None)
        last_update_time.pop(chat_id, None)
        last_percent.pop(chat_id, None)
        gc.collect()
        try:
            download_semaphore.release()
        except Exception:
            pass

# Catch-all handler for non-link/non-command messages to keep the chat clean
@bot.message_handler(func=lambda message: True, content_types=['text', 'audio', 'document', 'photo', 'sticker', 'video', 'video_note', 'voice', 'location', 'contact', 'new_chat_members', 'left_chat_member', 'new_chat_title', 'new_chat_photo', 'delete_chat_photo', 'group_chat_created', 'supergroup_chat_created', 'channel_chat_created', 'migrate_to_chat_id', 'migrate_from_chat_id', 'pinned_message'])
def handle_other_messages(message):
    # Skip if it is a bot command (like /start or /help)
    if message.text and message.text.startswith('/'):
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        safe_print(f"Failed to delete non-link message: {e}")

def run_bot():
    cleanup_stale_temp_dirs()
    safe_print(f"Bot is starting... (@{get_bot_username()})")
    while True:
        try:
            bot.infinity_polling(timeout=30, skip_pending=False, long_polling_timeout=30)
        except Exception as e:
            safe_print(f"Infinity polling exception: {e}. Retrying in 5 seconds...")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
