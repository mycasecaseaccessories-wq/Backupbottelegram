"""
export.py
---------
Builds ZIP archives of backup logs (CSV + summary).
Used by both the control bot and the Flask API.
"""

import csv
import io
import os
import time
import zipfile
from typing import Optional

import db
from config import EXPORT_DIR


def _rows_to_zip(rows: list, label: str) -> str:
    """Write rows into a ZIP file and return its path."""
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(
        [
            "id",
            "user_id",
            "target_id",
            "target_username",
            "chat_id",
            "message_id",
            "action",
            "media_type",
            "timestamp",
            "datetime",
            "message_text",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r.get("user_id", ""),
                r.get("target_id", ""),
                r.get("target_username", ""),
                r.get("chat_id", ""),
                r.get("message_id", ""),
                r["action"],
                r.get("media_type", "text"),
                r["timestamp"],
                time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(r["timestamp"])
                ),
                r.get("message_text") or "",
            ]
        )

    summary = (
        f"Telegram Backup Export\n"
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Filter: {label}\n"
        f"Total rows: {len(rows)}\n"
    )

    out_path = os.path.join(
        EXPORT_DIR, f"export_{label}_{int(time.time())}.zip"
    )
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("logs.csv", csv_buf.getvalue())
        zf.writestr("README.txt", summary)
    return out_path


def build_export_zip(user_id: Optional[int] = None) -> str:
    """Generate a ZIP file of all logs and return its path."""
    rows = db.get_logs(user_id=user_id, limit=10_000)
    label = f"user{user_id}" if user_id else "all"
    return _rows_to_zip(rows, label)


def build_daily_export_zip(date_str: str) -> Optional[str]:
    """
    Generate a ZIP file for a specific date (YYYY-MM-DD).
    Returns the file path, or None if there are no messages for that day.
    """
    rows = db.get_logs_for_date(date_str)
    if not rows:
        return None
    return _rows_to_zip(rows, f"daily_{date_str}")
