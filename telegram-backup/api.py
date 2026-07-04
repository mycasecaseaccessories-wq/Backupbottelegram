"""
api.py
------
Tiny Flask backend that exposes HTTP control endpoints:

    GET  /                 -> the browser sign-in UI
    POST /login/start      body: {"phone": "+95..."}     -> sends Telegram code
    POST /login/code       body: {"code": "12345"}       -> verifies the code
    POST /login/password   body: {"password": "..."}     -> 2FA step
    GET  /login/status                                    -> {ready, signed_in}
    POST /backup/on        body: {"telegram_id": 123}
    POST /backup/off       body: {"telegram_id": 123}
    POST /targets/add      body: {"telegram_id": 123, "target": "@user"}
    GET  /logs?telegram_id=123&limit=50
    GET  /export?telegram_id=123    -> downloads a ZIP
    GET  /healthz
"""

import asyncio
import logging
import os
from pathlib import Path

from flask import Blueprint, Flask, jsonify, redirect, request, send_file
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import db
import main as userbot_module  # for get_login_state()
import session_manager as sm
from config import API_HASH, API_ID, FLASK_HOST, FLASK_PORT, SESSION_DIR
from export import build_export_zip

# ---------------------------------------------------------------------------
# Per-user pending login states (keyed by session_name, e.g. "user_123456")
# Each value: {"client": TelegramClient, "phone": str, "phone_code_hash": str}
# ---------------------------------------------------------------------------
_PENDING_LOGINS: dict = {}

STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger("api")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
)

app = Flask(__name__)

# All login-UI routes also live under /telegram-login so the Replit
# preview pane can proxy directly to them.
login_bp = Blueprint("login_bp", __name__, url_prefix="/telegram-login")


def _require_user(telegram_id):
    """Look up (or create) the user, return its DB row."""
    if not telegram_id:
        return None
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return None
    return db.upsert_user(telegram_id)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.get("/")
def index():
    """Serve login page at root so the preview pane works directly on port 5000."""
    page = STATIC_DIR / "index.html"
    if page.exists():
        return send_file(page)
    return redirect("/telegram-login/", 302)


@app.get("/chat")
def chat_page():
    """Serve chat UI at root /chat (for direct :5000 access)."""
    page = STATIC_DIR / "chat.html"
    if page.exists():
        return send_file(page)
    return "Chat page not found", 404


# ---------------------------------------------------------------------------
# Browser-based Telethon sign-in — multi-session helpers
# ---------------------------------------------------------------------------
def _get_loop():
    """Return the shared asyncio event loop (set by main.py via session_manager)."""
    loop = sm.get_manager_loop()
    if not loop:
        # Fall back to the legacy login-state loop while main.py is starting.
        state = userbot_module.get_login_state()
        loop = state.get("loop")
    if not loop:
        raise RuntimeError("Server is still starting up — please wait a few seconds.")
    return loop


def _run(coro, timeout: int = 30):
    """Schedule *coro* on the shared event loop and block until done."""
    loop = _get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def _session_name_for_uid(uid) -> str:
    """Map a bot-user telegram_id (or None) to a session name."""
    if uid:
        try:
            return f"user_{int(uid)}"
        except (TypeError, ValueError):
            pass
    return "userbot"


def _get_pending_login(session_name: str) -> dict:
    """Return (creating if needed) the pending login state for a session."""
    if session_name not in _PENDING_LOGINS:
        session_path = os.path.join(SESSION_DIR, session_name)
        _PENDING_LOGINS[session_name] = {
            "client": TelegramClient(session_path, API_ID, API_HASH),
            "phone": None,
            "phone_code_hash": None,
        }
    return _PENDING_LOGINS[session_name]


def _login_status_data(uid=None):
    session_name = _session_name_for_uid(uid)

    # Check a running session first (fast path).
    running = sm.all_sessions()
    for sess in running.values():
        if sess.session_name == session_name:
            return {"ready": True, "signed_in": True,
                    "username": getattr(getattr(sess, "me", None), "username", None)}

    # If we still have the legacy login client, check it.
    if session_name == "userbot":
        state = userbot_module.get_login_state()
        if state.get("client"):
            async def _check():
                return await state["client"].is_user_authorized()
            try:
                ok = _run(_check(), timeout=10)
                return {"ready": True, "signed_in": ok}
            except Exception:
                pass

    # Check whether a pending-login client is already authorised.
    if session_name in _PENDING_LOGINS:
        client = _PENDING_LOGINS[session_name]["client"]

        async def _chk():
            if not client.is_connected():
                await client.connect()
            return await client.is_user_authorized()

        try:
            if _run(_chk(), timeout=10):
                return {"ready": True, "signed_in": True}
        except Exception:
            pass

    return {"ready": True, "signed_in": False}


def _login_start_data(phone: str, uid=None):
    session_name = _session_name_for_uid(uid)
    state = _get_pending_login(session_name)
    client = state["client"]

    async def _send():
        if not client.is_connected():
            await client.connect()
        result = await client.send_code_request(phone)
        state["phone"] = phone
        state["phone_code_hash"] = result.phone_code_hash

    _run(_send(), timeout=30)
    return {"ok": True, "next": "code", "session": session_name}


def _login_code_data(code: str, uid=None):
    session_name = _session_name_for_uid(uid)
    state = _get_pending_login(session_name)
    client = state["client"]

    async def _signin():
        try:
            await client.sign_in(
                phone=state.get("phone"),
                code=code,
                phone_code_hash=state.get("phone_code_hash"),
            )
            me = await client.get_me()
            return {"need_password": False,
                    "username": getattr(me, "username", None),
                    "id": me.id}
        except SessionPasswordNeededError:
            return {"need_password": True}

    result = _run(_signin(), timeout=30)

    if not result.get("need_password"):
        _on_login_success(result["id"], result.get("username"), session_name)

    return result


def _login_password_data(password: str, uid=None):
    session_name = _session_name_for_uid(uid)
    state = _get_pending_login(session_name)
    client = state["client"]

    async def _signin_pw():
        await client.sign_in(password=password)
        me = await client.get_me()
        return {"username": getattr(me, "username", None), "id": me.id}

    result = _run(_signin_pw(), timeout=30)
    _on_login_success(result["id"], result.get("username"), session_name)
    return result


def _on_login_success(telegram_id: int, username, session_name: str):
    """Called after a successful sign-in: register in DB and start the session."""
    user = db.upsert_user(telegram_id, username)
    db.set_session_name(telegram_id, session_name)

    # Disconnect the pending login client; session_manager will reconnect cleanly.
    pending = _PENDING_LOGINS.pop(session_name, None)
    if pending:
        async def _disc():
            try:
                await pending["client"].disconnect()
            except Exception:
                pass
        try:
            _run(_disc(), timeout=5)
        except Exception:
            pass

    # Start the session in the background.
    async def _start():
        await sm.manager.start_user(user["id"], telegram_id, session_name)
    try:
        _run(_start(), timeout=30)
    except Exception as exc:
        log.error("Failed to start session for %s: %s", session_name, exc)


# ---------------------------------------------------------------------------
# Shared route handler helpers (uid-aware)
# ---------------------------------------------------------------------------
def _h_login_status():
    uid = request.args.get("uid") or (request.get_json(silent=True) or {}).get("uid")
    return jsonify(**_login_status_data(uid=uid))


def _h_login_start():
    payload = request.get_json(silent=True) or request.form
    phone = (payload.get("phone") or "").strip()
    uid = payload.get("uid")
    if not phone:
        return jsonify(error="phone required"), 400
    try:
        return jsonify(**_login_start_data(phone, uid=uid))
    except Exception as exc:
        log.exception("login_start failed")
        return jsonify(error=str(exc)), 500


def _h_login_code():
    payload = request.get_json(silent=True) or request.form
    code = (payload.get("code") or "").strip()
    uid = payload.get("uid")
    if not code:
        return jsonify(error="code required"), 400
    try:
        return jsonify(ok=True, **_login_code_data(code, uid=uid))
    except Exception as exc:
        log.exception("login_code failed")
        return jsonify(error=str(exc)), 500


def _h_login_password():
    payload = request.get_json(silent=True) or request.form
    password = payload.get("password") or ""
    uid = payload.get("uid")
    if not password:
        return jsonify(error="password required"), 400
    try:
        return jsonify(ok=True, **_login_password_data(password, uid=uid))
    except Exception as exc:
        log.exception("login_password failed")
        return jsonify(error=str(exc)), 500


# --- Root-path routes (for direct :5000 access) ---
@app.get("/login/status")
def login_status():
    return _h_login_status()


@app.post("/login/start")
def login_start():
    return _h_login_start()


@app.post("/login/code")
def login_code():
    return _h_login_code()


@app.post("/login/password")
def login_password():
    return _h_login_password()


# --- /telegram-login/* Blueprint (same logic, for preview-pane proxy) ---
@login_bp.get("/")
@login_bp.get("")
def bp_index():
    page = STATIC_DIR / "index.html"
    if page.exists():
        return send_file(page)
    return "Login page not found", 404


@login_bp.get("/login/status")
def bp_login_status():
    return _h_login_status()


@login_bp.post("/login/start")
def bp_login_start():
    return _h_login_start()


@login_bp.post("/login/code")
def bp_login_code():
    return _h_login_code()


@login_bp.post("/login/password")
def bp_login_password():
    return _h_login_password()


# ---------------------------------------------------------------------------
# Chat viewer endpoints  (root + blueprint mirror)
# ---------------------------------------------------------------------------
def _chat_contacts_data():
    return db.get_chat_contacts()


def _chat_messages_data(target, before_id, limit):
    return db.get_chat_messages(
        target_username=target,
        limit=min(int(limit), 200),
        before_id=int(before_id) if before_id else None,
    )


@app.get("/chat/contacts")
def chat_contacts():
    return jsonify(contacts=_chat_contacts_data())


@app.get("/chat/messages")
def chat_messages():
    target = (request.args.get("target") or "").strip()
    if not target:
        return jsonify(error="target required"), 400
    msgs = _chat_messages_data(
        target, request.args.get("before_id"), request.args.get("limit", 60)
    )
    return jsonify(messages=msgs)


@login_bp.get("/chat")
def bp_chat():
    page = STATIC_DIR / "chat.html"
    if page.exists():
        return send_file(page)
    return "Chat page not found", 404


@login_bp.get("/chat/contacts")
def bp_chat_contacts():
    return jsonify(contacts=_chat_contacts_data())


@login_bp.get("/chat/messages")
def bp_chat_messages():
    target = (request.args.get("target") or "").strip()
    if not target:
        return jsonify(error="target required"), 400
    msgs = _chat_messages_data(
        target, request.args.get("before_id"), request.args.get("limit", 60)
    )
    return jsonify(messages=msgs)


# ---------------------------------------------------------------------------
# Logout / switch account
# ---------------------------------------------------------------------------
@app.post("/logout")
@login_bp.post("/logout")
def logout():
    """Log out the current Telethon session so a new account can log in."""
    async def _logout(client, _state):
        try:
            await client.log_out()
        except Exception:
            pass

    try:
        _run_on_userbot_loop(_logout, timeout=15)
    except Exception:
        pass

    # Delete the session file so next startup is a fresh login.
    from config import SESSION_DIR
    import glob as _glob
    for f in _glob.glob(os.path.join(SESSION_DIR, "userbot.session*")):
        try:
            os.remove(f)
        except Exception:
            pass

    return jsonify(ok=True, message="Logged out. Restart the server to log in with a new account.")


# ---------------------------------------------------------------------------
# Backup toggle
# ---------------------------------------------------------------------------
@app.post("/backup/on")
def backup_on():
    payload = request.get_json(silent=True) or request.form
    user = _require_user(payload.get("telegram_id"))
    if not user:
        return jsonify(error="telegram_id required"), 400
    db.set_backup_enabled(user["telegram_id"], True)
    return jsonify(ok=True, backup_enabled=True)


@app.post("/backup/off")
def backup_off():
    payload = request.get_json(silent=True) or request.form
    user = _require_user(payload.get("telegram_id"))
    if not user:
        return jsonify(error="telegram_id required"), 400
    db.set_backup_enabled(user["telegram_id"], False)
    return jsonify(ok=True, backup_enabled=False)


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
@app.post("/targets/add")
def targets_add():
    payload = request.get_json(silent=True) or request.form
    user = _require_user(payload.get("telegram_id"))
    if not user:
        return jsonify(error="telegram_id required"), 400
    target = (payload.get("target") or "").strip()
    if not target:
        return jsonify(error="target required"), 400
    added = db.add_target(user["id"], target)
    return jsonify(ok=True, added=added, target=target.lstrip("@").lower())


@app.get("/targets")
def targets_list():
    telegram_id = request.args.get("telegram_id")
    user = _require_user(telegram_id) if telegram_id else None
    rows = db.list_targets(user_id=user["id"] if user else None)
    return jsonify(targets=rows)


# ---------------------------------------------------------------------------
# Logs / export
# ---------------------------------------------------------------------------
@app.get("/logs")
def logs():
    telegram_id = request.args.get("telegram_id")
    limit = int(request.args.get("limit", 50))
    user = _require_user(telegram_id) if telegram_id else None
    rows = db.get_logs(user_id=user["id"] if user else None, limit=limit)
    return jsonify(logs=rows)


@app.get("/export")
def export():
    telegram_id = request.args.get("telegram_id")
    user = _require_user(telegram_id) if telegram_id else None
    path = build_export_zip(user_id=user["id"] if user else None)
    return send_file(
        path,
        as_attachment=True,
        download_name=os.path.basename(path),
        mimetype="application/zip",
    )


# Register blueprint AFTER all routes are defined
app.register_blueprint(login_bp)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def run_api() -> None:
    db.init_db()
    log.info("Flask API listening on %s:%s", FLASK_HOST, FLASK_PORT)
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    run_api()
