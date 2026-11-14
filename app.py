import os
import shutil
import sys
import threading
import time
from dotenv import load_dotenv

os.environ["PYTHONUNBUFFERED"] = "1"
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(line_buffering=True)

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# Clear pip cache if present to save disk
pip_cache = os.path.expanduser("~/.cache/pip")
if os.path.isdir(pip_cache):
    shutil.rmtree(pip_cache, ignore_errors=True)

load_dotenv(os.path.join(ROOT_DIR, ".env"))
sys.path.insert(0, os.path.join(ROOT_DIR, "backend"))

from backend.bot import run_bot
import uvicorn
from backend.main import app as web_app

_bot_thread = None
_bot_lock = threading.Lock()

def bot_worker_loop():
    time.sleep(2)
    while True:
        try:
            print("[App] Starting Telegram Bot in background loop...", flush=True)
            run_bot()
        except Exception as e:
            print(f"[App Error] Telegram Bot loop exception: {e}. Restarting in 5s...", flush=True)
        time.sleep(5)

def ensure_bot_running():
    global _bot_thread
    with _bot_lock:
        if _bot_thread is None or not _bot_thread.is_alive():
            print("[App] Launching/Reviving Telegram Bot background thread...", flush=True)
            _bot_thread = threading.Thread(target=bot_worker_loop, name="TelegramBotWorker", daemon=True)
            _bot_thread.start()
            return False
        return True

if __name__ == "__main__":
    # 1. Start Telegram Bot in background daemon thread
    ensure_bot_running()

    # 2. Start FastAPI / Uvicorn server on main thread for health checks and WebApp
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"[App] Starting HTTP server on {host}:{port} for health checks & Mini App...", flush=True)
    uvicorn.run(web_app, host=host, port=port, log_level="info")
