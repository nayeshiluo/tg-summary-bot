"""SQLite 消息存储：只保存总结所需的文本与回复链。"""
import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from config import DB_PATH, MESSAGE_RETENTION_DAYS

logger = logging.getLogger("summary-bot.storage")
_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER,
    user_name TEXT,
    text TEXT NOT NULL,
    reply_to_msg_id INTEGER,
    created_at REAL NOT NULL,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, message_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with _WRITE_LOCK, connect() as conn:
        conn.executescript(SCHEMA)


def save_message(
    chat_id: int,
    message_id: int,
    user_id: Optional[int],
    user_name: str,
    text: str,
    reply_to_msg_id: Optional[int] = None,
) -> None:
    if not text or not text.strip():
        return
    payload = (
        int(chat_id), int(message_id), int(user_id or 0), (user_name or "群友")[:120],
        text.strip(), int(reply_to_msg_id) if reply_to_msg_id else None, time.time(),
    )
    try:
        with _WRITE_LOCK, connect() as conn:
            conn.execute(
                """INSERT INTO messages
                   (chat_id, message_id, user_id, user_name, text, reply_to_msg_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id, message_id) DO UPDATE SET
                     text=excluded.text,
                     user_name=excluded.user_name,
                     reply_to_msg_id=COALESCE(excluded.reply_to_msg_id, messages.reply_to_msg_id)""",
                payload,
            )
    except Exception as exc:
        logger.error("写入消息失败 chat=%s msg=%s: %s", chat_id, message_id, exc)


def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    return [
        {
            "message_id": row[0],
            "user_id": row[1],
            "user_name": row[2],
            "text": row[3],
            "reply_to_msg_id": row[4],
            "created_at": row[5],
        }
        for row in rows
    ]


def get_recent_messages(chat_id: int, limit: int = 80) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 10000))
    try:
        with connect() as conn:
            rows = conn.execute(
                """SELECT message_id, user_id, user_name, text, reply_to_msg_id, created_at
                   FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?""",
                (int(chat_id), limit),
            ).fetchall()
        rows.reverse()
        return _rows_to_dicts(rows)
    except Exception as exc:
        logger.error("读取最近消息失败 chat=%s: %s", chat_id, exc)
        return []


def get_messages_by_time(
    chat_id: int,
    seconds: float,
    max_limit: int = 8000,
    *,
    end_timestamp: Optional[float] = None,
) -> List[Dict[str, Any]]:
    end = time.time() if end_timestamp is None else float(end_timestamp)
    cutoff = end - max(1.0, float(seconds))
    max_limit = max(1, min(int(max_limit), 20000))
    try:
        with connect() as conn:
            rows = conn.execute(
                """SELECT message_id, user_id, user_name, text, reply_to_msg_id, created_at
                   FROM messages
                   WHERE chat_id = ? AND created_at >= ? AND created_at <= ?
                   ORDER BY id DESC LIMIT ?""",
                (int(chat_id), cutoff, end, max_limit),
            ).fetchall()
        rows.reverse()
        return _rows_to_dicts(rows)
    except Exception as exc:
        logger.error("按时间读取消息失败 chat=%s: %s", chat_id, exc)
        return []


def count_messages(chat_id: int) -> int:
    try:
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (int(chat_id),)).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def prune_old_messages(days: Optional[int] = None) -> int:
    days = MESSAGE_RETENTION_DAYS if days is None else int(days)
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    try:
        with _WRITE_LOCK, connect() as conn:
            cursor = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            return cursor.rowcount or 0
    except Exception as exc:
        logger.error("清理历史消息失败：%s", exc)
        return 0


init_db()
