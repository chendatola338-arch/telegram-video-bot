import os
import shutil
import sys
import threading
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

def start_bot_thread():
    try:
        print("[App] Starting Telegram Bot in background thread...", flush=True)
        run_bot()
    except Exception as e:
        print(f"[App Error] Telegram Bot failed: {e}", flush=True)

if __name__ == "__main__":
    # 1. Start Telegram Bot in background daemon thread
    bot_thread = threading.Thread(target=start_bot_thread, daemon=True)
    bot_thread.start()

    # 2. Start FastAPI / Uvicorn server on main thread for Koyeb HTTP health checks and WebApp
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"[App] Starting HTTP server on {host}:{port} for Koyeb health checks & Mini App...", flush=True)
    uvicorn.run(web_app, host=host, port=port, log_level="info")
