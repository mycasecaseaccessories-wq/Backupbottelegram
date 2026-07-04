"""
db.py
-----
Tiny SQLite layer for the backup SaaS.

Tables
~~~~~~
users      - one row per Telegram user that uses the control bot
targets    - chats/users to be monitored by the userbot
logs       - every received / deleted message captured by the userbot
payments   - manual screenshot-based payments awaiting admin approval
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterable, Optional

from config import DB_PATH

_LOCK = threading.Lock()


@contextmanager
def get_conn():
    """Yield a sqlite3 connection with row access by name."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with _LOCK:
            yield conn
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Create all tables on first run."""
    with get_conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER UNIQUE NOT NULL,
                username        TEXT,
                plan            TEXT DEFAULT 'free',
                backup_enabled  INTEGER DEFAULT 0,
                created_at      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS targets (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                target_username  TEXT,
                target_id        INTEGER,
                added_at         INTEGER NOT NULL,
                backfilled_at    INTEGER,
                backup_channel_id INTEGER,
                UNIQUE (user_id, target_username),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER,
                target_id       INTEGER,
                target_username TEXT,
                chat_id         INTEGER,
                message_id      INTEGER,
                message_text    TEXT,
                media_type      TEXT DEFAULT 'text',
                action          TEXT NOT NULL,
                timestamp       INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                screenshot_path  TEXT NOT NULL,
                amount           REAL DEFAULT 0,
                status           TEXT DEFAULT 'pending',
                created_at       INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            """
        )
        # Lightweight migrations for older databases.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(targets)")]
        if "backfilled_at" not in cols:
            c.execute("ALTER TABLE targets ADD COLUMN backfilled_at INTEGER")

        log_cols = [r["name"] for r in c.execute("PRAGMA table_info(logs)")]
        if "media_type" not in log_cols:
            c.execute("ALTER TABLE logs ADD COLUMN media_type TEXT DEFAULT 'text'")

        tgt_cols = [r["name"] for r in c.execute("PRAGMA table_info(targets)")]
        if "backup_channel_id" not in tgt_cols:
            c.execute("ALTER TABLE targets ADD COLUMN backup_channel_id INTEGER")

        usr_cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
        if "session_name" not in usr_cols:
            c.execute("ALTER TABLE users ADD COLUMN session_name TEXT")
        if "backup_channel_id" not in usr_cols:
            c.execute("ALTER TABLE users ADD COLUMN backup_channel_id INTEGER")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def upsert_user(telegram_id: int, username: Optional[str] = None) -> dict:
    now = int(time.time())
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO users (telegram_id, username, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
            """,
            (telegram_id, username, now),
        )
        row = c.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row)


def set_user_channel(telegram_id: int, channel_id: Optional[int]) -> None:
    """Set (or clear) the default backup channel for a user."""
    with get_conn() as c:
        c.execute(
            "UPDATE users SET backup_channel_id = ? WHERE telegram_id = ?",
            (channel_id, telegram_id),
        )


def get_user_channel(user_db_id: int) -> Optional[int]:
    """Return the user's custom default backup channel, or None."""
    with get_conn() as c:
        row = c.execute(
            "SELECT backup_channel_id FROM users WHERE id = ?", (user_db_id,)
        ).fetchone()
        return row["backup_channel_id"] if row else None


def set_session_name(telegram_id: int, session_name: str) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE users SET session_name = ? WHERE telegram_id = ?",
            (session_name, telegram_id),
        )


def get_users_with_sessions() -> list:
    """Return all users that have an assigned session_name."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE session_name IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def set_backup_enabled(telegram_id: int, enabled: bool) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE users SET backup_enabled = ? WHERE telegram_id = ?",
            (1 if enabled else 0, telegram_id),
        )


def get_user(telegram_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def get_active_users() -> list:
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE backup_enabled = 1"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
def add_target(user_id: int, target_username: str) -> bool:
    now = int(time.time())
    target_username = target_username.lstrip("@").lower()
    with get_conn() as c:
        try:
            c.execute(
                """INSERT INTO targets (user_id, target_username, added_at)
                       VALUES (?, ?, ?)""",
                (user_id, target_username, now),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def list_targets(user_id: Optional[int] = None) -> list:
    with get_conn() as c:
        if user_id is None:
            rows = c.execute("SELECT * FROM targets").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM targets WHERE user_id = ?", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def all_target_usernames() -> set:
    with get_conn() as c:
        rows = c.execute("SELECT target_username FROM targets").fetchall()
        return {r["target_username"] for r in rows if r["target_username"]}


def update_target_id(target_username: str, target_id: int) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE targets SET target_id = ? WHERE target_username = ?",
            (target_id, target_username.lstrip("@").lower()),
        )


def pending_backfill_targets() -> list:
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM targets WHERE backfilled_at IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_backfilled(target_id_pk: int) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE targets SET backfilled_at = ? WHERE id = ?",
            (int(time.time()), target_id_pk),
        )


def reset_backfill_for(user_id: int, target_username: str) -> bool:
    target_username = target_username.lstrip("@").lower()
    with get_conn() as c:
        cur = c.execute(
            "UPDATE targets SET backfilled_at = NULL "
            "WHERE user_id = ? AND target_username = ?",
            (user_id, target_username),
        )
        return cur.rowcount > 0


def reset_backfill_all_for_user(user_id: int) -> int:
    with get_conn() as c:
        cur = c.execute(
            "UPDATE targets SET backfilled_at = NULL WHERE user_id = ?",
            (user_id,),
        )
        return cur.rowcount


def set_target_channel(
    user_id: int, target_username: str, channel_id: Optional[int]
) -> bool:
    """Set a custom backup channel for one target. None = use default."""
    target_username = target_username.lstrip("@").lower()
    with get_conn() as c:
        cur = c.execute(
            "UPDATE targets SET backup_channel_id = ? "
            "WHERE user_id = ? AND target_username = ?",
            (channel_id, user_id, target_username),
        )
        return cur.rowcount > 0


def get_target_channel(target_username: str) -> Optional[int]:
    """Return the custom channel_id for a target username, or None."""
    target_username = target_username.lstrip("@").lower()
    with get_conn() as c:
        row = c.execute(
            "SELECT backup_channel_id FROM targets "
            "WHERE target_username = ? LIMIT 1",
            (target_username,),
        ).fetchone()
        return row["backup_channel_id"] if row else None


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
def add_log(
    *,
    user_id: Optional[int],
    target_id: Optional[int],
    target_username: Optional[str],
    chat_id: Optional[int],
    message_id: Optional[int],
    message_text: Optional[str],
    media_type: str = "text",
    action: str,
) -> None:
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO logs (user_id, target_id, target_username,
                              chat_id, message_id, message_text,
                              media_type, action, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                target_id,
                target_username,
                chat_id,
                message_id,
                message_text,
                media_type,
                action,
                int(time.time()),
            ),
        )


def get_logs(user_id: Optional[int] = None, limit: int = 50) -> list:
    with get_conn() as c:
        if user_id is None:
            rows = c.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM logs WHERE user_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_logs_for_date(date_str: str) -> list:
    """Return all logs for a given date (YYYY-MM-DD) in UTC."""
    import calendar
    import time as _time
    struct = _time.strptime(date_str, "%Y-%m-%d")
    day_start = int(calendar.timegm(struct))
    day_end = day_start + 86400
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM logs WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY id ASC",
            (day_start, day_end),
        ).fetchall()
        return [dict(r) for r in rows]


def count_logs_for_target(user_id: int, target_username: str) -> dict:
    target_username = target_username.lstrip("@").lower()

    def _count(c, action: str) -> int:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM logs "
            "WHERE target_username = ? AND action = ?",
            (target_username, action),
        ).fetchone()
        return row["n"] if row else 0

    with get_conn() as c:
        received = _count(c, "received")
        sent = _count(c, "sent")
        deleted = _count(c, "deleted")
        last = c.execute(
            "SELECT timestamp FROM logs "
            "WHERE target_username = ? ORDER BY id DESC LIMIT 1",
            (target_username,),
        ).fetchone()
        target = c.execute(
            "SELECT * FROM targets WHERE target_username = ? AND user_id = ?",
            (target_username, user_id),
        ).fetchone()
        return {
            "received": received,
            "sent": sent,
            "total": received + sent,
            "deleted": deleted,
            "last_event_ts": last["timestamp"] if last else None,
            "target": dict(target) if target else None,
        }


def get_chat_contacts(user_id: Optional[int] = None) -> list:
    with get_conn() as c:
        base_where = "target_username IS NOT NULL AND action != 'deleted'"
        params: list = []
        if user_id is not None:
            base_where += " AND user_id = ?"
            params.append(user_id)

        rows = c.execute(
            f"""
            SELECT
                target_username,
                COUNT(*) AS total,
                SUM(CASE WHEN action='received' THEN 1 ELSE 0 END) AS received,
                SUM(CASE WHEN action='sent'     THEN 1 ELSE 0 END) AS sent,
                MAX(timestamp) AS last_ts,
                (SELECT message_text FROM logs inner_l
                 WHERE inner_l.target_username = logs.target_username
                   AND inner_l.action != 'deleted'
                 ORDER BY inner_l.id DESC LIMIT 1) AS last_message,
                (SELECT action FROM logs inner_a
                 WHERE inner_a.target_username = logs.target_username
                   AND inner_a.action != 'deleted'
                 ORDER BY inner_a.id DESC LIMIT 1) AS last_action
            FROM logs
            WHERE {base_where}
            GROUP BY target_username
            ORDER BY last_ts DESC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_chat_messages(
    target_username: str,
    limit: int = 60,
    before_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> list:
    target_username = target_username.lstrip("@").lower()
    where_parts = ["target_username = ?"]
    params: list = [target_username]
    if user_id is not None:
        where_parts.append("user_id = ?")
        params.append(user_id)
    if before_id is not None:
        where_parts.append("id < ?")
        params.append(before_id)
    params.append(limit)
    where = " AND ".join(where_parts)
    with get_conn() as c:
        rows = c.execute(
            f"SELECT * FROM logs WHERE {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def is_message_logged(user_id: int, chat_id: int, message_id: int) -> bool:
    """Return True if this message was already recorded (sent or received) for this user."""
    with get_conn() as c:
        row = c.execute(
            "SELECT 1 FROM logs WHERE user_id=? AND chat_id=? AND message_id=? "
            "AND action IN ('received','sent') LIMIT 1",
            (user_id, chat_id, message_id),
        ).fetchone()
        return row is not None


def delete_logs_for_target(user_id: int, target_username: str) -> int:
    """Delete all log entries for a target (used before re-backfilling a cleared channel)."""
    target_username = target_username.lstrip("@").lower()
    with get_conn() as c:
        cur = c.execute(
            "DELETE FROM logs WHERE user_id=? AND target_username=?",
            (user_id, target_username),
        )
        return cur.rowcount


def find_log_by_message(chat_id: int, message_ids: Iterable[int]) -> list:
    ids = list(message_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as c:
        rows = c.execute(
            f"SELECT * FROM logs WHERE chat_id = ? AND message_id IN "
            f"({placeholders}) AND action = 'received'",
            (chat_id, *ids),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
def add_payment(user_id: int, screenshot_path: str, amount: float = 0) -> int:
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO payments (user_id, screenshot_path, amount,
                                     created_at)
                   VALUES (?, ?, ?, ?)""",
            (user_id, screenshot_path, amount, int(time.time())),
        )
        return cur.lastrowid


def list_payments(status: Optional[str] = None) -> list:
    with get_conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM payments WHERE status = ? ORDER BY id DESC",
                (status,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM payments ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def set_payment_status(payment_id: int, status: str) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE payments SET status = ? WHERE id = ?",
            (status, payment_id),
        )
        if status == "approved":
            row = c.execute(
                "SELECT user_id FROM payments WHERE id = ?", (payment_id,)
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE users SET plan = 'pro' WHERE id = ?",
                    (row["user_id"],),
                )
