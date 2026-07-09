"""
main.py
-------
Telethon userbot.

Responsibilities
    * Sign in with the user's API_ID / API_HASH (interactive on first run).
    * Listen to messages from any monitored target user.
    * Forward each captured message (text, photo, video, sticker, audio,
      voice, document, animation, etc.) to the private backup channel.
    * Detect deletions and mark them in the database.
    * Every midnight UTC: auto-export the day's messages and send the
      ZIP file to the backup channel.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
)

import db
from config import (
    API_ID,
    API_HASH,
    BACKUP_CHANNEL_ID,
    SESSION_DIR,
    missing_secrets,
)

# Cache of resolved channel entities  {channel_id: entity}
_CHANNEL_CACHE: dict = {}

BACKFILL_POLL_INTERVAL = 30

log = logging.getLogger("userbot")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
)

_LOGIN_STATE: dict = {
    "client": None,
    "loop": None,
    "phone": None,
    "phone_code_hash": None,
    "session_name": "userbot",
}


def get_login_state() -> dict:
    return _LOGIN_STATE


# ---------------------------------------------------------------------------
# Media type detection
# ---------------------------------------------------------------------------
def _detect_media_type(message) -> str:
    """Return a human-readable media type string for a Telethon message."""
    msg = message
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "video_note", None):
        return "video_note"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "animation", None):
        return "animation"
    if getattr(msg, "document", None):
        return "document"
    if getattr(msg, "contact", None):
        return "contact"
    if getattr(msg, "location", None) or getattr(msg, "venue", None):
        return "location"
    if getattr(msg, "poll", None):
        return "poll"
    if getattr(msg, "game", None):
        return "game"
    # Telethon wraps media in message.media; check raw media attr too
    media = getattr(msg, "media", None)
    if media:
        media_name = type(media).__name__.lower()
        if "photo" in media_name:
            return "photo"
        if "document" in media_name:
            # Check MIME for sticker / animation / audio / video
            doc = getattr(media, "document", None)
            if doc:
                mime = getattr(doc, "mime_type", "") or ""
                if mime == "image/webp":
                    return "sticker"
                if "video" in mime:
                    return "video"
                if "audio" in mime:
                    return "audio"
                if "gif" in mime or "animation" in mime:
                    return "animation"
            return "document"
        return media_name.replace("messagemedia", "") or "media"
    if getattr(msg, "message", None):
        return "text"
    return "unknown"


def _message_label(message) -> str:
    """Return the text to store in message_text for non-text messages."""
    media_type = _detect_media_type(message)
    if media_type == "text":
        return message.message or ""
    label_map = {
        "photo":      "📷 [Photo]",
        "video":      "🎬 [Video]",
        "video_note": "📹 [Video Note]",
        "sticker":    "🎉 [Sticker]",
        "voice":      "🎤 [Voice]",
        "audio":      "🎵 [Audio]",
        "animation":  "🎞 [GIF/Animation]",
        "document":   "📄 [Document]",
        "contact":    "👤 [Contact]",
        "location":   "📍 [Location]",
        "poll":       "📊 [Poll]",
        "game":       "🎮 [Game]",
    }
    base = label_map.get(media_type, f"[{media_type}]")
    # Append caption if present
    caption = getattr(message, "message", None)
    if caption:
        base += f" {caption}"
    return base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _sender_username(event) -> str | None:
    try:
        sender = await event.get_sender()
        if sender and getattr(sender, "username", None):
            return sender.username.lower()
    except Exception:
        pass
    return None


async def _chat_partner_username(event) -> str | None:
    try:
        chat = await event.get_chat()
        if chat and getattr(chat, "username", None):
            return chat.username.lower()
    except Exception:
        pass
    return None


async def _chat_partner_id(event) -> int:
    try:
        chat = await event.get_chat()
        return getattr(chat, "id", 0) or 0
    except Exception:
        return 0


def _is_monitored(username: str | None) -> bool:
    if not username:
        return False
    return username.lower() in db.all_target_usernames()


async def _get_dest_channel(client: TelegramClient, target_username: str) -> int:
    """Return the destination channel id for a target (custom or default)."""
    custom = db.get_target_channel(target_username)
    channel_id = custom if custom else BACKUP_CHANNEL_ID
    if channel_id not in _CHANNEL_CACHE:
        try:
            entity = await client.get_entity(channel_id)
            _CHANNEL_CACHE[channel_id] = entity
            log.info(
                "Resolved channel %s → %s",
                channel_id,
                getattr(entity, "title", channel_id),
            )
        except Exception as exc:
            log.error("Cannot resolve channel %s: %s", channel_id, exc)
    return channel_id


# ---------------------------------------------------------------------------
# Client + handlers
# ---------------------------------------------------------------------------
def build_client() -> TelegramClient:
    session_path = os.path.join(SESSION_DIR, "userbot")
    return TelegramClient(session_path, API_ID, API_HASH)


async def run_userbot() -> None:
    db.init_db()

    missing = missing_secrets()
    if missing:
        log.warning(
            "Userbot disabled - missing secrets: %s", ", ".join(missing)
        )
        while True:
            await asyncio.sleep(3600)

    import session_manager as sm

    loop = asyncio.get_running_loop()
    sm.set_manager_loop(loop)

    # ------------------------------------------------------------------
    # Legacy "userbot" session: used by the first / admin account.
    # Keep a temporary client alive so api.py can drive the login flow.
    # ------------------------------------------------------------------
    client = build_client()
    _LOGIN_STATE["client"] = client
    _LOGIN_STATE["loop"] = loop
    _LOGIN_STATE["session_name"] = "userbot"

    await client.connect()

    if not await client.is_user_authorized():
        login_url = f"https://{os.getenv('REPLIT_DEV_DOMAIN', 'your-repl.replit.dev')}"
        log.warning("=" * 60)
        log.warning("Userbot NOT signed in. Open the app to log in:")
        log.warning("  %s", login_url)
        log.warning("=" * 60)
        # Start every OTHER user's session while waiting for the legacy
        # login. Skip "userbot" itself - its session file is held open by
        # the temporary login client above (two clients on one SQLite
        # session file cause "database is locked" corruption).
        for u in db.get_users_with_sessions():
            if u["session_name"] != "userbot":
                await sm.manager.start_user(
                    u["id"], u["telegram_id"], u["session_name"]
                )
        log.info("Waiting for legacy userbot login; other sessions running.")
        while not await client.is_user_authorized():
            await asyncio.sleep(3)

    me = await client.get_me()
    log.info("Userbot legacy session signed in as @%s (id=%s)", me.username, me.id)

    # Register the admin in DB with session_name="userbot" if not already set.
    user = db.upsert_user(me.id, me.username)
    if not user.get("session_name"):
        db.set_session_name(me.id, "userbot")

    # Disconnect the temporary login client; session_manager will reconnect it.
    await client.disconnect()
    _LOGIN_STATE["client"] = None

    # ------------------------------------------------------------------
    # Start ALL sessions (admin "userbot" + any additional user sessions).
    # ------------------------------------------------------------------
    await sm.manager.start_all()

    log.info("Session manager running. All sessions started.")

    # Keep the event loop alive.
    while True:
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Daily auto backup: send yesterday's log ZIP to the backup channel
# ---------------------------------------------------------------------------
async def send_daily_backup(client: TelegramClient, date_str: str) -> None:
    """Export all messages for *date_str* (YYYY-MM-DD) and post to channel."""
    from export import build_daily_export_zip

    log.info("Daily backup: generating export for %s ...", date_str)
    try:
        zip_path = build_daily_export_zip(date_str)
        if zip_path is None:
            log.info("Daily backup: no messages on %s, skipping.", date_str)
            try:
                await client.send_message(
                    BACKUP_CHANNEL_ID,
                    f"📋 **Daily Backup — {date_str}**\n\nNo messages recorded today.",
                )
            except Exception:
                pass
            return

        caption = (
            f"📦 **Daily Backup — {date_str}**\n"
            "ဒီနေ့ backup ပြီးပါပြီ။ "
            "ZIP ထဲမှာ logs.csv နဲ့ README.txt ပါဝင်ပါတယ်။"
        )
        with open(zip_path, "rb") as fh:
            await client.send_file(
                BACKUP_CHANNEL_ID,
                fh,
                caption=caption,
                force_document=True,
            )
        log.info("Daily backup for %s sent to channel.", date_str)
    except Exception as exc:
        log.exception("Daily backup failed for %s: %s", date_str, exc)


async def daily_backup_loop(client: TelegramClient) -> None:
    """Wait until midnight UTC every day, then send the daily backup."""
    log.info("Daily backup scheduler started (fires at 00:00 UTC each day).")
    while True:
        now = datetime.now(timezone.utc)
        # Next midnight UTC
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wait_seconds = (next_midnight - now).total_seconds()
        log.info(
            "Daily backup: next run in %.0f s (at %s UTC).",
            wait_seconds,
            next_midnight.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await asyncio.sleep(wait_seconds)

        # Send yesterday's backup
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        await send_daily_backup(client, yesterday)


# ---------------------------------------------------------------------------
# Backfill: copy a target's existing chat history to the backup channel
# ---------------------------------------------------------------------------
async def backfill_target(client: TelegramClient, target: dict) -> None:
    username = target["target_username"]
    log.info("Backfilling old messages for @%s ...", username)

    try:
        entity = await client.get_entity(username)
    except Exception as exc:
        log.error("Cannot resolve @%s: %s - skipping.", username, exc)
        db.mark_backfilled(target["id"])
        return

    db.update_target_id(username, getattr(entity, "id", 0))

    count = 0
    async for message in client.iter_messages(entity, reverse=True):
        if not message:
            continue

        media_type = _detect_media_type(message)
        text = _message_label(message)
        direction = "sent" if getattr(message, "out", False) else "received"
        db.add_log(
            user_id=target["user_id"],
            target_id=getattr(entity, "id", 0),
            target_username=username,
            chat_id=message.chat_id,
            message_id=message.id,
            message_text=text,
            media_type=media_type,
            action=direction,
        )

        dest = await _get_dest_channel(client, username)
        try:
            await client.forward_messages(dest, message)
        except FloodWaitError as fw:
            log.warning("Flood wait %ss while backfilling @%s ...",
                        fw.seconds, username)
            await asyncio.sleep(fw.seconds + 1)
        except (ChannelInvalidError, ChannelPrivateError):
            log.error(
                "Cannot forward to channel=%s - join the channel "
                "with the userbot account first.",
                dest,
            )
            return
        except Exception as exc:
            log.warning("Forward failed for msg %s: %s", message.id, exc)

        count += 1
        if count % 20 == 0:
            await asyncio.sleep(1)

    db.mark_backfilled(target["id"])
    log.info("Backfill complete for @%s (%s messages).", username, count)


async def backfill_loop(client: TelegramClient) -> None:
    """Periodically backfill any newly added targets — run all in parallel."""
    # Track which target PKs are currently being backfilled so we don't
    # launch duplicate tasks on the next poll tick.
    _in_progress: set[int] = set()

    while True:
        try:
            pending = db.pending_backfill_targets()
            new_targets = [t for t in pending if t["id"] not in _in_progress]
            for target in new_targets:
                _in_progress.add(target["id"])

                async def _run(t=target):
                    try:
                        await backfill_target(client, t)
                    finally:
                        _in_progress.discard(t["id"])

                asyncio.create_task(_run())
        except Exception as exc:
            log.exception("Backfill loop error: %s", exc)
        await asyncio.sleep(BACKFILL_POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_userbot())
