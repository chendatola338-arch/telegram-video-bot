import sqlite3
import os
import datetime

DB_PATH = "/app/data/users.db" if os.path.exists("/app/data") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

# Admin IDs that bypass the payment requirement
ADMIN_IDS = [1043240083, 523679883]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            status TEXT DEFAULT 'unpaid', -- 'unpaid', 'active'
            expiry_date TIMESTAMP,        -- NULL if lifetime, datetime string if temporary
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_user_status(telegram_id):
    # Temporarily allow free access to everyone (bypass payment check)
    return 'active', None
    
    # Original payment code (uncomment to enable payment requirement):
    # # Admins have lifetime free access
    # if telegram_id in ADMIN_IDS:
    #     return 'active', None
    #     
    # conn = sqlite3.connect(DB_PATH)
    # cursor = conn.cursor()
    # cursor.execute("SELECT status, expiry_date FROM users WHERE telegram_id = ?", (telegram_id,))
    # row = cursor.fetchone()
    # conn.close()
    # if not row:
    #     return 'unpaid', None
    #     
    # status, expiry_str = row
    # if status == 'active' and expiry_str:
    #     # Check if expired
    #     try:
    #         expiry = datetime.datetime.strptime(expiry_str, '%Y-%m-%d %H:%M:%S')
    #         if datetime.datetime.now() > expiry:
    #             # Set user back to unpaid since subscription has expired
    #             set_user_unpaid(telegram_id)
    #             return 'unpaid', None
    #     except Exception:
    #         # Fallback in case of parse errors
    #         pass
    #         
    # return status, expiry_str

def set_user_unpaid(telegram_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = 'unpaid', expiry_date = NULL WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def activate_user(telegram_id, username, first_name, duration_days=30):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    expiry = None
    if duration_days:
        expiry = (datetime.datetime.now() + datetime.timedelta(days=duration_days)).strftime('%Y-%m-%d %H:%M:%S')
        
    cursor.execute("""
        INSERT INTO users (telegram_id, username, first_name, status, expiry_date)
        VALUES (?, ?, ?, 'active', ?)
        ON CONFLICT(telegram_id) DO UPDATE SET 
            status = 'active',
            expiry_date = ?,
            username = ?,
            first_name = ?
    """, (telegram_id, username, first_name, expiry, expiry, username, first_name))
    conn.commit()
    conn.close()
