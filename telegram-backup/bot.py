"""
bot.py
------
Control bot built with python-telegram-bot 13.15 (synchronous API).

Provides /start with an inline-button menu:

    Start Backup   Stop Backup
    Add Chat       Logs
    Export         Upgrade
"""

import logging
import os
import threading
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    Filters,
    MessageHandler,
    Updater,
)

import asyncio

import db
import session_manager as sm
from config import ADMIN_CHAT_ID, BOT_TOKEN, BACKUP_CHANNEL_ID, UPLOAD_DIR, missing_secrets
from export import build_export_zip

log = logging.getLogger("controlbot")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
)

# Conversation states
ASK_TARGET, ASK_PAYMENT_SCREENSHOT = range(2)


def _md(text) -> str:
    """Escape legacy-Markdown special chars so usernames with '_' etc. don't
    break Telegram's entity parser."""
    s = str(text)
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------
def main_menu() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("Start Backup", callback_data="backup_on"),
            InlineKeyboardButton("Stop Backup", callback_data="backup_off"),
        ],
        [
            InlineKeyboardButton("Add Chat", callback_data="add_chat"),
            InlineKeyboardButton("Logs", callback_data="logs"),
        ],
        [
            InlineKeyboardButton("Export", callback_data="export"),
            InlineKeyboardButton("Upgrade", callback_data="upgrade"),
        ],
    ]
    return InlineKeyboardMarkup(kb)


def start(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)

    domain = os.getenv("REPLIT_DEV_DOMAIN", "")
    if domain:
        login_url = f"https://{domain}/telegram-login/?uid={user.id}"
        login_line = f"\n🔗 *Sign in with your account:*\n{login_url}\n"
    else:
        login_line = ""

    # Check if this user already has a session linked.
    already_linked = bool(record.get("session_name"))
    if already_linked:
        status_msg = (
            f"✅ Your account is already signed in "
            f"(session: `{record['session_name']}`).\n"
        )
    else:
        status_msg = "⚠️ You haven't signed in with your Telegram account yet.\n"

    update.message.reply_text(
        f"Hi {_md(user.first_name)}! 👋\n\n"
        f"{status_msg}"
        f"{login_line}\n"
        "Pick an option below:",
        reply_markup=main_menu(),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Inline-button callbacks
# ---------------------------------------------------------------------------
def on_button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user = query.from_user
    record = db.upsert_user(user.id, user.username)
    action = query.data

    if action == "backup_on":
        db.set_backup_enabled(user.id, True)
        query.edit_message_text(
            "Backup is now ON.\nNew messages from your targets "
            "will be forwarded to the backup channel.",
            reply_markup=main_menu(),
        )

    elif action == "backup_off":
        db.set_backup_enabled(user.id, False)
        query.edit_message_text(
            "Backup paused. Press Start Backup to resume.",
            reply_markup=main_menu(),
        )

    elif action == "add_chat":
        query.edit_message_text(
            "Send me the @username of the chat / user "
            "you want to back up.\n\nType /cancel to abort."
        )
        return ASK_TARGET

    elif action == "logs":
        rows = db.get_logs(user_id=record["id"], limit=15)
        if not rows:
            text = "No log entries yet."
        else:
            lines = []
            for r in rows:
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(r["timestamp"])
                )
                tag = "DEL" if r["action"] == "deleted" else "MSG"
                preview = (r["message_text"] or "")[:60].replace("\n", " ")
                lines.append(
                    f"[{ts}] {tag} @{r['target_username']}: {preview}"
                )
            text = "Last 15 events:\n\n" + "\n".join(lines)
        query.edit_message_text(text, reply_markup=main_menu())

    elif action == "export":
        path = build_export_zip(user_id=record["id"])
        query.edit_message_text("Building your export...")
        with open(path, "rb") as fh:
            context.bot.send_document(
                chat_id=user.id,
                document=fh,
                filename=os.path.basename(path),
                caption="Here is your logs export.",
            )
        context.bot.send_message(
            chat_id=user.id,
            text="Anything else?",
            reply_markup=main_menu(),
        )

    elif action == "upgrade":
        query.edit_message_text(
            "*Upgrade to Pro*\n\n"
            "Please send the payment to the configured wallet "
            "and reply with a *photo* of the payment screenshot.\n\n"
            "Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return ASK_PAYMENT_SCREENSHOT

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Conversation: add target
# ---------------------------------------------------------------------------
def handle_target_text(update: Update, context: CallbackContext):
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)
    target = update.message.text.strip()
    if not target:
        update.message.reply_text("Empty username, try again or /cancel.")
        return ASK_TARGET

    added = db.add_target(record["id"], target)
    if added:
        update.message.reply_text(
            f"Added @{_md(target.lstrip('@'))} to your backup targets.\n\n"
            "I'll start backing up *all old messages* with this chat in "
            "the background, and every new message from now on will also "
            "be forwarded to the backup channel.",
            reply_markup=main_menu(),
            parse_mode="Markdown",
        )
    else:
        update.message.reply_text(
            f"@{target.lstrip('@')} is already in your targets.",
            reply_markup=main_menu(),
        )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Conversation: payment screenshot
# ---------------------------------------------------------------------------
def handle_payment_photo(update: Update, context: CallbackContext):
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)

    photo = update.message.photo[-1]  # largest size
    file = photo.get_file()
    filename = f"payment_{user.id}_{int(time.time())}.jpg"
    path = os.path.join(UPLOAD_DIR, filename)
    file.download(custom_path=path)

    payment_id = db.add_payment(record["id"], path)

    update.message.reply_text(
        "Got it! Your payment is queued for review. "
        "An admin will approve it shortly.",
        reply_markup=main_menu(),
    )

    # Notify the admin (if configured).
    if ADMIN_CHAT_ID:
        try:
            with open(path, "rb") as fh:
                context.bot.send_photo(
                    chat_id=ADMIN_CHAT_ID,
                    photo=fh,
                    caption=(
                        f"New payment #{payment_id} from "
                        f"@{user.username or user.id}.\n\n"
                        f"Approve: /approve {payment_id}\n"
                        f"Reject:  /reject {payment_id}"
                    ),
                )
        except Exception as exc:
            log.warning("Could not notify admin: %s", exc)
    return ConversationHandler.END


def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("Cancelled.", reply_markup=main_menu())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
def _admin_only(update: Update) -> bool:
    return ADMIN_CHAT_ID and update.effective_user.id == ADMIN_CHAT_ID


def cmd_approve(update: Update, context: CallbackContext):
    if not _admin_only(update):
        return
    try:
        payment_id = int(context.args[0])
    except (IndexError, ValueError):
        update.message.reply_text("Usage: /approve <payment_id>")
        return
    db.set_payment_status(payment_id, "approved")
    update.message.reply_text(f"Payment {payment_id} approved.")


def cmd_reject(update: Update, context: CallbackContext):
    if not _admin_only(update):
        return
    try:
        payment_id = int(context.args[0])
    except (IndexError, ValueError):
        update.message.reply_text("Usage: /reject <payment_id>")
        return
    db.set_payment_status(payment_id, "rejected")
    update.message.reply_text(f"Payment {payment_id} rejected.")


def cmd_history(update: Update, context: CallbackContext):
    """Show how many messages have been backed up for a target."""
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)

    if not context.args:
        update.message.reply_text("Usage: /history <username>")
        return

    target = context.args[0].lstrip("@").lower()
    stats = db.count_logs_for_target(record["id"], target)

    if not stats["target"] and stats["received"] == 0 and stats["deleted"] == 0:
        update.message.reply_text(
            f"No backup data for @{target} yet. "
            "Add it via the Add Chat button first."
        )
        return

    backfill_state = "not started"
    if stats["target"]:
        backfill_state = (
            "done" if stats["target"].get("backfilled_at") else "in progress"
        )

    last = stats["last_event_ts"]
    last_str = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "-"
    )

    update.message.reply_text(
        f"*Backup history for @{_md(target)}*\n\n"
        f"Total backed up    : {stats['total']}\n"
        f"  • Received (သူပို့): {stats['received']}\n"
        f"  • Sent (ငါပို့)    : {stats['sent']}\n"
        f"Deleted captured   : {stats['deleted']}\n"
        f"Old-msg backfill   : {backfill_state}\n"
        f"Last activity      : {last_str}",
        parse_mode="Markdown",
    )


def cmd_backfill(update: Update, context: CallbackContext):
    """Re-trigger old-message backfill for one target (or all targets)."""
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)

    if context.args:
        target = context.args[0].lstrip("@").lower()
        ok = db.reset_backfill_for(record["id"], target)
        if not ok:
            update.message.reply_text(
                f"@{target} ကို သင့် target list ထဲ မတွေ့ပါ။ "
                "Add Chat နှိပ်ပြီး အရင် ထည့်ပါ။"
            )
            return
        update.message.reply_text(
            f"OK — @{target} ရဲ့ ဟောင်းနေတဲ့ chat history (စာ၊ ပုံ၊ video၊ "
            "file) အကုန်လုံးကို backup channel ထဲ ပြန်ပို့ပါမယ်။\n\n"
            "နောက် ၃၀ စက္ကန့်အတွင်း backup စပါမယ်။ Message များရင် "
            "မိနစ်နဲ့ချီ ကြာနိုင်ပါတယ်။"
        )
    else:
        n = db.reset_backfill_all_for_user(record["id"])
        if n == 0:
            update.message.reply_text(
                "သင့်မှာ target မရှိသေးပါ။ Add Chat နှိပ်ပြီး chat ထည့်ပါ။"
            )
            return
        update.message.reply_text(
            f"OK — သင့် target {n} ခုလုံးရဲ့ ဟောင်းနေတဲ့ chat history "
            "(စာ၊ ပုံ၊ video၊ file) အကုန်လုံးကို backup channel ထဲ "
            "ပြန်ပို့ပါမယ်။"
        )


def cmd_payments(update: Update, context: CallbackContext):
    if not _admin_only(update):
        return
    rows = db.list_payments(status="pending")
    if not rows:
        update.message.reply_text("No pending payments.")
        return
    lines = [f"#{r['id']}  user={r['user_id']}  {r['screenshot_path']}"
             for r in rows]
    update.message.reply_text("\n".join(lines))


def cmd_setmychannel(update: Update, context: CallbackContext):
    """Set (or clear) the user's own default backup channel.

    Usage:
        /setmychannel -1001234567890
        /setmychannel reset
    """
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)

    if not context.args:
        current = record.get("backup_channel_id")
        if current:
            cur_str = f"`{current}`"
        else:
            cur_str = "global default (BACKUP\\_CHANNEL\\_ID)"
        update.message.reply_text(
            "📢 *သင့် default backup channel*\n\n"
            f"လက်ရှိ: {cur_str}\n\n"
            "Channel ပြောင်းဖို့:\n"
            "`/setmychannel -1001234567890`\n\n"
            "Default ပြန်သုံးဖို့:\n"
            "`/setmychannel reset`\n\n"
            "🔑 Channel ID ရဖို့: channel ထဲ bot ကို forward လုပ်ပြီး "
            "@userinfobot မှ ရယူနိုင်သည်။",
            parse_mode="Markdown",
        )
        return

    raw = context.args[0].strip()

    if raw.lower() == "reset":
        db.set_user_channel(user.id, None)
        update.message.reply_text(
            "✅ Default channel ကို global default သို့ ပြန်သတ်မှတ်ပြီးပါပြီ။"
        )
        return

    try:
        channel_id = int(raw)
    except ValueError:
        update.message.reply_text(
            "Channel ID မှားနေသည်။ -100 ဖြင့်စတင်သော နံပါတ်ဖြစ်ရမည်။\n"
            "Example: /setmychannel -1001234567890"
        )
        return

    db.set_user_channel(user.id, channel_id)
    update.message.reply_text(
        f"✅ သင့် backup channel ကို `{channel_id}` သို့ သတ်မှတ်ပြီးပါပြီ။\n\n"
        "ယခုမှစ၍ target တစ်ခုချင်းစီ custom channel မထားမချင်း "
        "ဤ channel သို့ forward လုပ်မည်ဖြစ်သည်။\n\n"
        "မှတ်ချက်: သင့် Telegram account ကို ထို channel ၏ member/admin အဖြစ် "
        "ထည့်ထားရပါမည်။",
        parse_mode="Markdown",
    )


def cmd_logout(update: Update, context: CallbackContext):
    """Log out the current userbot session so a new account can be used."""
    import glob as _glob
    from config import SESSION_DIR

    update.message.reply_text(
        "⏳ Userbot session ကို logout လုပ်နေပါသည်..."
    )
    for f in _glob.glob(os.path.join(SESSION_DIR, "userbot.session*")):
        try:
            os.remove(f)
        except Exception:
            pass

    update.message.reply_text(
        "✅ Logout ပြီးပါပြီ။\n\n"
        "အသစ် account နဲ့ login ဝင်ဖို့:\n"
        "1. App ကို preview မှာ ဖွင့်ပါ\n"
        "2. Phone number ထည့်ပြီး sign in ဝင်ပါ\n\n"
        "_(Server restart လုပ်ရပါမည်)_",
        parse_mode="Markdown",
    )


def cmd_setchannel(update: Update, context: CallbackContext):
    """Set a custom backup channel for a specific target.

    Usage:
        /setchannel @pudin121 -1001234567890
        /setchannel @pudin121 reset   <- go back to default channel
    """
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)

    if len(context.args) < 2:
        update.message.reply_text(
            "အသုံးပြုနည်း:\n"
            "/setchannel @username -100xxxxxxxxxx\n"
            "/setchannel @username reset  (default channel ပြန်သုံး)\n\n"
            "Example: /setchannel @pudin121 -1001234567890"
        )
        return

    target = context.args[0].lstrip("@").lower()
    raw_channel = context.args[1].strip()

    if raw_channel.lower() == "reset":
        ok = db.set_target_channel(record["id"], target, None)
        if ok:
            update.message.reply_text(
                f"@{target} ကို default backup channel သို့ ပြန်သတ်မှတ်ပြီးပါပြီ။"
            )
        else:
            update.message.reply_text(
                f"@{target} ကို target list ထဲ မတွေ့ပါ။ Add Chat မှ ဦးစွာ ထည့်ပါ။"
            )
        return

    try:
        channel_id = int(raw_channel)
    except ValueError:
        update.message.reply_text(
            "Channel ID မှားနေသည်။ -100 ဖြင့်စတင်သော နံပါတ်ဖြစ်ရမည်။\n"
            "Example: -1001234567890"
        )
        return

    ok = db.set_target_channel(record["id"], target, channel_id)
    if ok:
        update.message.reply_text(
            f"✅ @{_md(target)} ၏ message များကို channel `{channel_id}` သို့ ပို့မည်။\n\n"
            f"မှတ်ချက်: သင့် Telegram account ကို ထို channel ၏ member/admin အဖြစ် ထည့်ထားရပါမည်။",
            parse_mode="Markdown",
        )
    else:
        update.message.reply_text(
            f"@{target} ကို target list ထဲ မတွေ့ပါ။ Add Chat မှ ဦးစွာ ထည့်ပါ။"
        )


def cmd_listtargets(update: Update, context: CallbackContext):
    """List all targets with their assigned channels."""
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)
    targets = db.list_targets(user_id=record["id"])
    if not targets:
        update.message.reply_text("Target မရှိသေးပါ။ Add Chat မှ ထည့်ပါ။")
        return
    lines = []
    for t in targets:
        ch = t.get("backup_channel_id")
        ch_str = f"`{ch}`" if ch else "default channel"
        lines.append(f"• @{_md(t['target_username'])} → {ch_str}")
    update.message.reply_text(
        "*သင့် backup targets:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


def _run_async(coro) -> str:
    """Run an async coroutine on the session manager's event loop (blocking)."""
    loop = sm.get_manager_loop()
    if not loop or not loop.is_running():
        return "❌ Userbot session မ run ဘူးသေးပါ။ Bot login ဦးဆုံး ဝင်ပါ။"
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=180)
    except Exception as exc:
        return f"❌ Error: {exc}"


def _get_user_session(telegram_id: int):
    """Return the running UserSession for a telegram_id, or None."""
    return sm.get_session_by_telegram_id(telegram_id)


def cmd_clearchannel(update: Update, context: CallbackContext):
    """Delete all messages from a target's backup channel and re-backfill fresh.

    Usage: /clearchannel @username
    """
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)

    if not context.args:
        update.message.reply_text(
            "📋 *Channel ရှင်းပြီး ပြန်ပို့ပုံ:*\n\n"
            "```\n/clearchannel @username\n```\n\n"
            "ဤ command သည်:\n"
            "1. ထို target ၏ backup channel ထဲ ရှိသမျှ message ဖျက်မည်\n"
            "2. Database log များ ဖျက်မည်\n"
            "3. ဟောင်းနေတဲ့ message အကုန် ပြန်ပို့မည်\n\n"
            "⚠️ Userbot သည် ထို channel ၏ admin ဖြစ်ရမည်။",
            parse_mode="Markdown",
        )
        return

    target = context.args[0].lstrip("@").lower()
    session = _get_user_session(user.id)
    if not session:
        update.message.reply_text(
            "❌ Userbot session မတွေ့ပါ။ /start မှ login ဝင်ပါ။"
        )
        return

    update.message.reply_text(
        f"⏳ @{target} ၏ backup channel ကို ရှင်းနေပါသည်...\n"
        "Channel message အများကြီးရှိလျှင် မိနစ်အနည်းငယ် ကြာနိုင်သည်။"
    )
    result = _run_async(session.clear_channel_and_reset(target))
    update.message.reply_text(result, parse_mode="Markdown")


def cmd_sendzip(update: Update, context: CallbackContext):
    """Send a full export zip to a Telegram channel.

    Usage:
        /sendzip                    <- send to your default backup channel
        /sendzip -1001234567890     <- send to specified channel
        /sendzip me                 <- send directly to this chat (DM)
    """
    user = update.effective_user
    record = db.upsert_user(user.id, user.username)

    # Determine destination
    if context.args and context.args[0].lower() == "me":
        # Send directly to the user in DM via bot
        update.message.reply_text("📦 Export zip ဆောက်နေပါသည်...")
        try:
            zip_path = build_export_zip(user_id=record["id"])
            with open(zip_path, "rb") as fh:
                context.bot.send_document(
                    chat_id=user.id,
                    document=fh,
                    filename=os.path.basename(zip_path),
                    caption="📦 Full Backup Export",
                )
        except Exception as exc:
            update.message.reply_text(f"❌ Error: {exc}")
        return

    # Send to Telegram channel via userbot
    if context.args:
        try:
            channel_id = int(context.args[0])
        except ValueError:
            update.message.reply_text(
                "Channel ID မှားနေသည်။\n"
                "Example: /sendzip -1001234567890\n"
                "Bot ထဲ တိုက်ရိုက်ပို့ဖို့: /sendzip me"
            )
            return
    else:
        # Use the user's default backup channel
        channel_id = db.get_user_channel(record["id"]) or BACKUP_CHANNEL_ID

    session = _get_user_session(user.id)
    if not session:
        update.message.reply_text(
            "❌ Userbot session မတွေ့ပါ။\n"
            "Bot ထဲ တိုက်ရိုက်ပို့ဖို့: /sendzip me"
        )
        return

    update.message.reply_text(f"📦 Backup zip ဆောက်ပြီး channel `{channel_id}` သို့ ပို့နေပါသည်...",
                               parse_mode="Markdown")
    result = _run_async(session.send_export_to_channel(channel_id))
    update.message.reply_text(result, parse_mode="Markdown")


def cmd_help(update: Update, context: CallbackContext):
    """Show a complete command guide."""
    text = (
        "📖 *Telegram Backup Bot — Command Guide*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "🚀 *အစပျိုးခြင်း*\n"
        "/start — Bot ဖွင့်ပြီး login link ရယူပါ\n\n"

        "🎯 *Target စီမံခြင်း*\n"
        "/start → Add Chat — Chat/user ထည့်ပါ\n"
        "/targets — သင့် target list ကြည့်ပါ\n"
        "/history @username — ထို target ၏ backup ကိုင်စီရင်မှု ကြည့်ပါ\n"
        "/backfill @username — ထို target ၏ ဟောင်းသော history ပြန်ပို့ပါ\n"
        "/backfill — Target အကုန်၏ history ပြန်ပို့ပါ\n\n"

        "📢 *Channel စီမံခြင်း*\n"
        "/setmychannel -100xxx — သင့် default backup channel သတ်မှတ်ပါ\n"
        "/setmychannel reset — Global default channel ပြန်သုံးပါ\n"
        "/setchannel @user -100xxx — ထို target အတွက် channel သီးသန့်သတ်မှတ်ပါ\n"
        "/setchannel @user reset — ထို target ကို default channel ပြန်သုံးပါ\n\n"

        "🧹 *Channel ရှင်းပြီး ပြန်ပို့ခြင်း*\n"
        "/clearchannel @username — Channel message အကုန်ဖျက်ပြီး ပြန် backup\n"
        "  ↳ Userbot သည် ထို channel ၏ admin ဖြစ်ရမည်\n\n"

        "📦 *Backup ZIP ပို့ခြင်း*\n"
        "/sendzip me — Bot ထဲ တိုက်ရိုက် zip ပို့ပါ\n"
        "/sendzip -100xxx — သတ်မှတ်သော channel သို့ zip ပို့ပါ\n"
        "/sendzip — Default backup channel သို့ zip ပို့ပါ\n"
        "/daily_backup — ယနေ့ backup zip ရယူပါ\n"
        "/daily_backup 2025-05-10 — သတ်မှတ်ရက်ကို backup zip ရယူပါ\n\n"

        "⚙️ *Backup ထိန်းချုပ်ခြင်း*\n"
        "/start → Start Backup — Message forward စပါ\n"
        "/start → Stop Backup — Message forward ခေတ္တရပ်ပါ\n"
        "/start → Export — Log CSV zip ရယူပါ\n"
        "/start → Logs — နောက်ဆုံး ၁၅ ခုကြည့်ပါ\n\n"

        "🔐 *Session*\n"
        "/logout — ဤ userbot session ကို ဖြုတ်ပါ\n\n"

        "💡 *Channel ID ရဖို့*\n"
        "Channel ထဲ @userinfobot ကို forward လုပ်ပါ\n"
        "သို့မဟုတ် `https://web.telegram.org` ၌ channel ဖွင့်ပြီး URL ထဲက ID ကူးပါ\n"
        "(-100 ဖြင့် ဦးဆောင်သော ကိန်းဖြစ်ရမည်)"
    )
    update.message.reply_text(text, parse_mode="Markdown")


def cmd_daily_backup(update: Update, context: CallbackContext):
    """Manually trigger a daily backup export for a given date (or today)."""
    import time as _time
    from export import build_daily_export_zip

    if context.args:
        date_str = context.args[0]
        try:
            _time.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            update.message.reply_text(
                "Date format မှားနေသည်။ Example: /daily_backup 2025-05-10"
            )
            return
    else:
        date_str = _time.strftime("%Y-%m-%d")

    update.message.reply_text(
        f"📦 {date_str} ၏ backup ကို export လုပ်နေပါသည်..."
    )

    try:
        zip_path = build_daily_export_zip(date_str)
        if zip_path is None:
            update.message.reply_text(
                f"📋 {date_str} တွင် message မရှိသောကြောင့် backup မရှိပါ။"
            )
            return
        with open(zip_path, "rb") as fh:
            context.bot.send_document(
                chat_id=update.effective_user.id,
                document=fh,
                filename=os.path.basename(zip_path),
                caption=f"📦 Daily Backup — {date_str}",
            )
    except Exception as exc:
        update.message.reply_text(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_bot() -> None:
    db.init_db()

    if "BOT_TOKEN" in missing_secrets():
        log.warning("Control bot disabled - BOT_TOKEN is not set.")
        # Sleep forever to avoid restart loops in the supervisor.
        while True:
            time.sleep(3600)

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Conversation: button -> follow-up
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_button)],
        states={
            ASK_TARGET: [
                MessageHandler(Filters.text & ~Filters.command,
                               handle_target_text),
            ],
            ASK_PAYMENT_SCREENSHOT: [
                MessageHandler(Filters.photo, handle_payment_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(conv)
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("history", cmd_history))
    dp.add_handler(CommandHandler("backfill", cmd_backfill))
    dp.add_handler(CommandHandler("approve", cmd_approve))
    dp.add_handler(CommandHandler("reject", cmd_reject))
    dp.add_handler(CommandHandler("payments", cmd_payments))
    dp.add_handler(CommandHandler("daily_backup", cmd_daily_backup))
    dp.add_handler(CommandHandler("setchannel", cmd_setchannel))
    dp.add_handler(CommandHandler("setmychannel", cmd_setmychannel))
    dp.add_handler(CommandHandler("targets", cmd_listtargets))
    dp.add_handler(CommandHandler("logout", cmd_logout))
    dp.add_handler(CommandHandler("clearchannel", cmd_clearchannel))
    dp.add_handler(CommandHandler("sendzip", cmd_sendzip))

    # Register all commands in the Telegram command menu (BotFather)
    try:
        from telegram import BotCommand
        updater.bot.set_my_commands([
            BotCommand("start",        "Bot ဖွင့်ပြီး login link ရယူပါ"),
            BotCommand("help",         "Command guide အပြည့်အစုံ ကြည့်ပါ"),
            BotCommand("targets",      "Backup target list ကြည့်ပါ"),
            BotCommand("history",      "Target တစ်ခု၏ backup ကိုင်စီရင်မှု"),
            BotCommand("backfill",     "ဟောင်းသော message history ပြန်ပို့ပါ"),
            BotCommand("setmychannel", "Default backup channel သတ်မှတ်ပါ"),
            BotCommand("setchannel",   "Target အတွက် channel သီးသန့် သတ်မှတ်ပါ"),
            BotCommand("clearchannel", "Channel ရှင်းပြီး ပြန် backup လုပ်ပါ"),
            BotCommand("sendzip",      "Backup zip file ပို့ပါ"),
            BotCommand("daily_backup", "ရက်တစ်ရက်၏ backup export ရယူပါ"),
            BotCommand("logout",       "Userbot session ဖြုတ်ပါ"),
        ])
        log.info("Bot commands registered with Telegram.")
    except Exception as exc:
        log.warning("Could not set bot commands: %s", exc)

    log.info("Control bot starting...")
    updater.start_polling(drop_pending_updates=True)
    # We are usually launched from a worker thread, so we cannot call
    # updater.idle() (it installs signal handlers). Block forever instead.
    stop = threading.Event()
    try:
        stop.wait()
    except (KeyboardInterrupt, SystemExit):
        updater.stop()


if __name__ == "__main__":
    run_bot()
