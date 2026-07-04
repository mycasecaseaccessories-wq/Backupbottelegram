"""
config.py
---------
Central configuration loaded from environment variables.

Required secrets (set via Replit Secrets):
    API_ID              -> Telegram API ID  (https://my.telegram.org)
    API_HASH            -> Telegram API hash
    BOT_TOKEN           -> Control bot token (from @BotFather)
    BACKUP_CHANNEL_ID   -> Numeric chat id of the private backup channel
                          (the userbot account MUST be a member of this channel)
    ADMIN_CHAT_ID       -> Telegram numeric id of the admin (you)

Optional:
    FLASK_HOST  (default 0.0.0.0)
    FLASK_PORT  (default 5000)
    DB_PATH     (default ./backup.db)
"""

import os

def _safe_int(name: str) -> int:
    """Read an int env var without crashing on bad input."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


# --- Telegram credentials -------------------------------------------------
API_ID = _safe_int("API_ID")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Numeric channel id (e.g. -1001234567890).  Userbot must be a member.
BACKUP_CHANNEL_ID = _safe_int("BACKUP_CHANNEL_ID")

# Admin user id used for payment approvals and admin-only actions.
ADMIN_CHAT_ID = _safe_int("ADMIN_CHAT_ID")

# --- Server ---------------------------------------------------------------
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", os.getenv("PORT", "5000")))

# --- Storage --------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "backup.db"))
SESSION_DIR = os.path.join(BASE_DIR, "sessions")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

# Make sure storage folders exist
for _d in (SESSION_DIR, UPLOAD_DIR, EXPORT_DIR):
    os.makedirs(_d, exist_ok=True)


def missing_secrets() -> list:
    """Return names of any required secrets that are not configured."""
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not BACKUP_CHANNEL_ID:
        missing.append("BACKUP_CHANNEL_ID")
    if not ADMIN_CHAT_ID:
        missing.append("ADMIN_CHAT_ID")
    return missing
