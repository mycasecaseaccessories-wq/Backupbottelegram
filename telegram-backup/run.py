"""
run.py
------
Single entry point for Replit: starts the Flask API, the control bot
and the Telethon userbot together.

    python telegram-backup/run.py
"""

import asyncio
import logging
import os
import socket
import sys
import threading

import db
from api import run_api
from bot import run_bot
from config import FLASK_PORT, missing_secrets
from main import run_userbot

log = logging.getLogger("run")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
)

# ── Single-instance guard ────────────────────────────────────────────────────
# If another copy is already listening on port 5000 we are the duplicate;
# exit cleanly so only one process ever runs.
def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _thread(target, name):
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


def main() -> None:
    # Guard: exit immediately if another instance already owns our port
    if _port_in_use(FLASK_PORT):
        log.warning("Port %s already in use — another instance is running. Exiting.", FLASK_PORT)
        sys.exit(0)

    db.init_db()

    missing = missing_secrets()
    if missing:
        log.warning(
            "Missing secrets: %s.  Components needing them will idle "
            "until configured in Replit Secrets.",
            ", ".join(missing),
        )

    # Flask API and the synchronous control bot live in their own threads.
    _thread(run_api, "flask-api")
    _thread(run_bot, "control-bot")

    # Telethon owns the main thread because asyncio likes it that way.
    try:
        asyncio.run(run_userbot())
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
