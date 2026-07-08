"""
session_manager.py
------------------
Manages multiple concurrent Telethon user sessions.
Each authenticated user gets their own TelegramClient instance that
runs independently, monitoring only that user's targets.

Flow:
    1. Bot user sends /start → gets personalised login URL with ?uid=<telegram_id>
    2. User opens URL → logs in with their own phone number
    3. Session saved as  sessions/user_<telegram_id>.session
    4. SessionManager starts a TelegramClient for that session
    5. That client handles only that user's targets
"""

import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from telethon import TelegramClient, events
from telethon.errors import ChannelInvalidError, ChannelPrivateError, FloodWaitError

import db
from config import (API_ID, API_HASH, BACKUP_CHANNEL_ID, SESSION_DIR,
                    BOT_TOKEN, ADMIN_CHAT_ID)

log = logging.getLogger("session_mgr")


def _bot_api(method: str, payload: dict) -> Optional[dict]:
    """Call the Telegram Bot API synchronously (stdlib only)."""
    if not BOT_TOKEN:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Bot API %s failed: %s", method, exc)
        return None

# ─── Global registry ─────────────────────────────────────────────────────────
# Keyed by the DB user.id  (integer primary key, NOT telegram_id)
_SESSIONS: Dict[int, "UserSession"] = {}
_MANAGER_LOOP: Optional[asyncio.AbstractEventLoop] = None


def get_manager_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _MANAGER_LOOP


def set_manager_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MANAGER_LOOP
    _MANAGER_LOOP = loop


def get_session(user_db_id: int) -> Optional["UserSession"]:
    return _SESSIONS.get(user_db_id)


def get_session_by_telegram_id(telegram_id: int) -> Optional["UserSession"]:
    for s in _SESSIONS.values():
        if s.telegram_id == telegram_id:
            return s
    return None


def all_sessions() -> Dict[int, "UserSession"]:
    return dict(_SESSIONS)


# ─── Per-user helpers (imported by UserSession to avoid circular deps) ────────
def _detect_media_type(message) -> str:
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "video_note", None):
        return "video_note"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "animation", None):
        return "animation"
    if getattr(message, "document", None):
        return "document"
    if getattr(message, "contact", None):
        return "contact"
    if getattr(message, "location", None) or getattr(message, "venue", None):
        return "location"
    if getattr(message, "poll", None):
        return "poll"
    media = getattr(message, "media", None)
    if media:
        name = type(media).__name__.lower()
        doc = getattr(media, "document", None)
        if doc:
            mime = getattr(doc, "mime_type", "") or ""
            if mime == "image/webp":
                return "sticker"
            if "video" in mime:
                return "video"
            if "audio" in mime:
                return "audio"
        if "photo" in name:
            return "photo"
        return name.replace("messagemedia", "") or "media"
    if getattr(message, "message", None):
        return "text"
    return "unknown"


def _message_label(message) -> str:
    media_type = _detect_media_type(message)
    label_map = {
        "photo": "📷 [Photo]", "video": "🎬 [Video]",
        "video_note": "📹 [Video Note]", "sticker": "🎉 [Sticker]",
        "voice": "🎤 [Voice]", "audio": "🎵 [Audio]",
        "animation": "🎞 [GIF/Animation]", "document": "📄 [Document]",
        "contact": "👤 [Contact]", "location": "📍 [Location]",
        "poll": "📊 [Poll]", "game": "🎮 [Game]",
    }
    if media_type == "text":
        return getattr(message, "message", "") or ""
    base = label_map.get(media_type, f"[{media_type}]")
    caption = getattr(message, "message", None)
    if caption:
        base += f" {caption}"
    return base


# ─── UserSession ──────────────────────────────────────────────────────────────
class UserSession:
    """One TelegramClient for one authenticated user account."""

    def __init__(self, user_db_id: int, telegram_id: int, session_name: str):
        self.user_db_id = user_db_id
        self.telegram_id = telegram_id
        self.session_name = session_name
        self.session_path = os.path.join(SESSION_DIR, session_name)
        self.client: Optional[TelegramClient] = None
        self._channel_cache: Dict[int, object] = {}
        self.me = None

    # ------------------------------------------------------------------
    def _build_client(self) -> TelegramClient:
        return TelegramClient(self.session_path, API_ID, API_HASH)

    def _my_targets(self) -> set:
        targets = db.list_targets(user_id=self.user_db_id)
        return {t["target_username"] for t in targets if t.get("target_username")}

    async def _dest_channel(self, target_username: str) -> int:
        # Priority: per-target channel > user default channel > global default
        per_target = db.get_target_channel(target_username)
        if per_target:
            cid = per_target
        else:
            user_ch = db.get_user_channel(self.user_db_id)
            cid = user_ch if user_ch else BACKUP_CHANNEL_ID
        if cid not in self._channel_cache:
            try:
                self._channel_cache[cid] = await self.client.get_entity(cid)
                log.info("[%s] Channel resolved: %s", self.session_name,
                         getattr(self._channel_cache[cid], "title", cid))
            except Exception as exc:
                log.error("[%s] Cannot resolve channel %s: %s",
                          self.session_name, cid, exc)
        return cid

    # ------------------------------------------------------------------
    async def start(self) -> bool:
        self.client = self._build_client()
        await self.client.connect()

        if not await self.client.is_user_authorized():
            log.warning("[%s] Not authorized yet.", self.session_name)
            await self.client.disconnect()
            return False

        self.me = await self.client.get_me()
        log.info("[%s] Signed in as @%s (id=%s)",
                 self.session_name,
                 getattr(self.me, "username", "?"),
                 getattr(self.me, "id", "?"))

        # Pre-cache default backup channel
        try:
            ent = await self.client.get_entity(BACKUP_CHANNEL_ID)
            self._channel_cache[BACKUP_CHANNEL_ID] = ent
            log.info("[%s] Default backup channel: %s",
                     self.session_name, getattr(ent, "title", BACKUP_CHANNEL_ID))
        except Exception as exc:
            log.error("[%s] Cannot resolve default backup channel: %s",
                      self.session_name, exc)

        # Auto-enable backup so new messages are forwarded immediately
        db.set_backup_enabled(self.telegram_id, True)

        self._attach_handlers()
        asyncio.create_task(self._backfill_loop())
        asyncio.create_task(self._daily_backup_loop())
        _SESSIONS[self.user_db_id] = self
        return True

    async def run(self) -> None:
        ok = await self.start()
        if ok:
            await self.client.run_until_disconnected()
        _SESSIONS.pop(self.user_db_id, None)

    async def stop(self) -> None:
        _SESSIONS.pop(self.user_db_id, None)
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        log.info("[%s] Session stopped.", self.session_name)

    # ------------------------------------------------------------------
    def _attach_handlers(self) -> None:
        client = self.client
        session = self

        @client.on(events.NewMessage())
        async def on_new_message(event):
            # Respect backup_enabled flag
            user_row = db.get_user(session.telegram_id)
            if not user_row or not user_row.get("backup_enabled"):
                return

            if event.out:
                try:
                    chat = await event.get_chat()
                    partner = getattr(chat, "username", None)
                    if partner:
                        partner = partner.lower()
                    partner_id = getattr(chat, "id", 0) or 0
                except Exception:
                    return
            else:
                try:
                    sender = await event.get_sender()
                    partner = getattr(sender, "username", None)
                    if partner:
                        partner = partner.lower()
                    partner_id = event.sender_id
                except Exception:
                    return

            if not partner or partner not in session._my_targets():
                return

            # Dedup: skip if this message was already forwarded
            if db.is_message_logged(session.user_db_id, event.chat_id, event.message.id):
                return

            media_type = _detect_media_type(event.message)
            text = _message_label(event.message)
            direction = "sent" if event.out else "received"

            db.add_log(
                user_id=session.user_db_id,
                target_id=partner_id,
                target_username=partner,
                chat_id=event.chat_id,
                message_id=event.message.id,
                message_text=text,
                media_type=media_type,
                action=direction,
            )
            db.update_target_id(partner, partner_id)

            dest = await session._dest_channel(partner)
            try:
                await client.forward_messages(dest, event.message)
            except (ChannelInvalidError, ChannelPrivateError):
                log.error("[%s] Cannot forward to channel=%s", session.session_name, dest)
            except Exception as exc:
                log.exception("[%s] Forward failed: %s", session.session_name, exc)

        @client.on(events.MessageDeleted())
        async def on_deleted(event):
            chat_id = event.chat_id
            targets = db.list_targets(user_id=session.user_db_id)
            if chat_id is None:
                for t in targets:
                    if t.get("target_id"):
                        for m in db.find_log_by_message(t["target_id"], event.deleted_ids):
                            db.add_log(
                                user_id=m["user_id"], target_id=m["target_id"],
                                target_username=m["target_username"],
                                chat_id=m["chat_id"], message_id=m["message_id"],
                                message_text=m["message_text"],
                                media_type=m.get("media_type", "text"),
                                action="deleted",
                            )
                return
            for m in db.find_log_by_message(chat_id, event.deleted_ids):
                db.add_log(
                    user_id=m["user_id"], target_id=m["target_id"],
                    target_username=m["target_username"],
                    chat_id=m["chat_id"], message_id=m["message_id"],
                    message_text=m["message_text"],
                    media_type=m.get("media_type", "text"),
                    action="deleted",
                )

    # ------------------------------------------------------------------
    async def _backfill_loop(self) -> None:
        in_progress: set = set()
        while True:
            try:
                pending = [t for t in db.pending_backfill_targets()
                           if t["user_id"] == self.user_db_id
                           and t["id"] not in in_progress]
                for t in pending:
                    in_progress.add(t["id"])

                    async def _run(target=t):
                        try:
                            await self._backfill_one(target)
                        finally:
                            in_progress.discard(target["id"])

                    asyncio.create_task(_run())
            except Exception as exc:
                log.exception("[%s] Backfill loop error: %s", self.session_name, exc)
            await asyncio.sleep(30)

    async def _backfill_one(self, target: dict) -> None:
        username = target["target_username"]
        log.info("[%s] Backfilling @%s ...", self.session_name, username)
        try:
            entity = await self.client.get_entity(username)
        except Exception as exc:
            log.error("[%s] Cannot resolve @%s: %s", self.session_name, username, exc)
            db.mark_backfilled(target["id"])
            return

        db.update_target_id(username, getattr(entity, "id", 0))
        dest = await self._dest_channel(username)
        count = 0
        skipped = 0
        async for message in self.client.iter_messages(entity, reverse=True):
            if not message:
                continue

            # Dedup: skip messages already logged/forwarded
            if db.is_message_logged(self.user_db_id, message.chat_id, message.id):
                skipped += 1
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
            try:
                await self.client.forward_messages(dest, message)
            except FloodWaitError as fw:
                log.warning("[%s] Flood wait %ss for @%s",
                            self.session_name, fw.seconds, username)
                await self._flood_wait_countdown(
                    username, fw.seconds + 1, target["user_id"])
                try:
                    await self.client.forward_messages(dest, message)
                except Exception as exc:
                    log.error("[%s] Retry after flood wait failed for msg %s: %s",
                              self.session_name, message.id, exc)
            except (ChannelInvalidError, ChannelPrivateError):
                log.error("[%s] Cannot forward to channel=%s", self.session_name, dest)
                break
            except Exception as exc:
                log.warning("[%s] Forward failed msg %s: %s",
                            self.session_name, message.id, exc)
            count += 1
            if count % 20 == 0:
                await asyncio.sleep(1)
                # Abort if the user removed this target mid-backfill.
                if not db.target_exists(target["user_id"], username):
                    log.info("[%s] Backfill aborted @%s (target removed).",
                             self.session_name, username)
                    return
        db.mark_backfilled(target["id"])
        log.info("[%s] Backfill done @%s (%s new, %s skipped).",
                 self.session_name, username, count, skipped)

    # ------------------------------------------------------------------
    async def _flood_wait_countdown(self, username: str, seconds: int,
                                    user_db_id: int) -> None:
        """Wait out a Telegram flood wait while showing a live countdown
        in the control bot (message edited once per minute)."""
        chat_id = None
        user = db.get_user_by_id(user_db_id)
        if user and user.get("telegram_id"):
            chat_id = user["telegram_id"]
        elif ADMIN_CHAT_ID:
            chat_id = ADMIN_CHAT_ID

        total = max(int(seconds), 1)
        message_id = None
        if chat_id:
            mins = (total + 59) // 60
            resp = await asyncio.to_thread(_bot_api, "sendMessage", {
                "chat_id": chat_id,
                "text": (f"⏳ Telegram flood limit ကြောင့် @{username} backup "
                         f"ကို ခဏရပ်ထားပါတယ်။\nကျန်ချိန် — {mins} မိနစ်"),
            })
            if resp and resp.get("ok"):
                message_id = resp["result"]["message_id"]

        remaining = total
        while remaining > 0:
            step = min(60, remaining)
            await asyncio.sleep(step)
            remaining -= step
            if message_id and remaining > 0:
                mins = (remaining + 59) // 60
                await asyncio.to_thread(_bot_api, "editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": (f"⏳ @{username} backup — "
                             f"ကျန်ချိန် {mins} မိနစ်..."),
                })

        if message_id:
            await asyncio.to_thread(_bot_api, "editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f"▶️ @{username} backup ပြန်စတင်နေပါပြီ။",
            })

    # ------------------------------------------------------------------
    async def clear_channel_and_reset(self, target_username: str) -> str:
        """Delete all messages from the target's backup channel, then reset backfill."""
        username = target_username.lstrip("@").lower()

        # Verify target belongs to this user
        targets = db.list_targets(user_id=self.user_db_id)
        if not any(t["target_username"] == username for t in targets):
            return f"❌ @{username} သည် target list ထဲ မရှိပါ။"

        dest = await self._dest_channel(username)

        # Collect all message IDs from the channel in batches
        deleted_count = 0
        batch = []
        try:
            async for msg in self.client.iter_messages(dest, limit=None):
                batch.append(msg.id)
                if len(batch) >= 100:
                    try:
                        await self.client.delete_messages(dest, batch)
                        deleted_count += len(batch)
                    except Exception as exc:
                        log.warning("[%s] Delete batch failed: %s", self.session_name, exc)
                    batch = []
                    await asyncio.sleep(0.3)
            if batch:
                try:
                    await self.client.delete_messages(dest, batch)
                    deleted_count += len(batch)
                except Exception as exc:
                    log.warning("[%s] Delete batch failed: %s", self.session_name, exc)
        except Exception as exc:
            return (f"⚠️ Channel message ဖျက်ရာတွင် error: {exc}\n\n"
                    "Bot/userbot သည် ထို channel ၏ admin ဖြစ်ရမည်။")

        # Wipe logs and reset backfill so everything re-forwards fresh
        db.delete_logs_for_target(self.user_db_id, username)
        db.reset_backfill_for(self.user_db_id, username)

        log.info("[%s] Cleared %d msgs from channel %s for @%s; backfill reset.",
                 self.session_name, deleted_count, dest, username)
        return (f"✅ Channel မှ message {deleted_count} ခု ဖျက်ပြီးပါပြီ။\n\n"
                f"@{username} ၏ backup ကို အစကနေ ပြန်မပို့မီ ၃၀ စက္ကန့်ခန့် ကြာပါမည်။")

    async def send_export_to_channel(self, channel_id: int) -> str:
        """Build a full export zip and send it to the given channel."""
        from export import build_export_zip
        zip_path = build_export_zip(user_id=self.user_db_id)
        import time as _time
        caption = (f"📦 Full Backup Export\n"
                   f"Session: {self.session_name}\n"
                   f"Generated: {_time.strftime('%Y-%m-%d %H:%M UTC')}")
        with open(zip_path, "rb") as fh:
            await self.client.send_file(
                channel_id, fh, caption=caption, force_document=True
            )
        return f"✅ Backup zip ကို channel `{channel_id}` သို့ ပို့ပြီးပါပြီ။"

    # ------------------------------------------------------------------
    async def _daily_backup_loop(self) -> None:
        from export import build_daily_export_zip
        log.info("[%s] Daily backup scheduler started (00:00 UTC).", self.session_name)
        while True:
            now = datetime.now(timezone.utc)
            next_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            await asyncio.sleep((next_midnight - now).total_seconds())
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            log.info("[%s] Sending daily backup for %s ...", self.session_name, yesterday)
            try:
                zip_path = build_daily_export_zip(yesterday)
                if zip_path is None:
                    await self.client.send_message(
                        BACKUP_CHANNEL_ID,
                        f"📋 **Daily Backup — {yesterday}**\n\nNo messages recorded.",
                    )
                else:
                    caption = (f"📦 **Daily Backup — {yesterday}**\n"
                               f"Session: {self.session_name}")
                    with open(zip_path, "rb") as fh:
                        await self.client.send_file(
                            BACKUP_CHANNEL_ID, fh, caption=caption,
                            force_document=True)
            except Exception as exc:
                log.exception("[%s] Daily backup failed: %s", self.session_name, exc)


# ─── SessionManager ───────────────────────────────────────────────────────────
class SessionManager:
    """Starts and tracks all user sessions."""

    def __init__(self):
        self._tasks: Dict[int, asyncio.Task] = {}

    async def start_all(self) -> None:
        """Load all users who have a session file and start their sessions."""
        users = db.get_users_with_sessions()
        log.info("SessionManager: starting %d session(s).", len(users))
        for user in users:
            await self.start_user(user["id"], user["telegram_id"], user["session_name"])

    async def start_user(self, user_db_id: int, telegram_id: int,
                         session_name: str) -> bool:
        """Start (or restart) the session for one user."""
        session_path = os.path.join(SESSION_DIR, session_name + ".session")
        if not os.path.exists(session_path):
            log.warning("SessionManager: session file missing for %s", session_name)
            return False

        if user_db_id in _SESSIONS:
            log.info("SessionManager: session %s already running.", session_name)
            return True

        sess = UserSession(user_db_id, telegram_id, session_name)

        async def _run():
            try:
                await sess.run()
            except Exception as exc:
                log.exception("Session %s crashed: %s", session_name, exc)
            _SESSIONS.pop(user_db_id, None)
            self._tasks.pop(user_db_id, None)

        task = asyncio.create_task(_run())
        self._tasks[user_db_id] = task
        return True

    async def stop_user(self, user_db_id: int) -> None:
        sess = _SESSIONS.get(user_db_id)
        if sess:
            await sess.stop()
        task = self._tasks.pop(user_db_id, None)
        if task and not task.done():
            task.cancel()


# Singleton
manager = SessionManager()
