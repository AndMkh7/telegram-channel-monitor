"""SQLite database module for multi-user Telegram channel monitor."""

import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "data.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS channels (
            channel_username  TEXT PRIMARY KEY,
            added_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_keywords (
            user_id  INTEGER NOT NULL,
            keyword  TEXT NOT NULL,
            PRIMARY KEY (user_id, keyword),
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bot_state (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            channel   TEXT NOT NULL,
            status    TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()


# ── Users ──────────────────────────────────────────────────────

def register_user(user_id: int, username: str | None, first_name: str | None):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
        (user_id, username, first_name),
    )
    conn.commit()
    conn.close()


def get_all_users() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Channels (admin pool) ─────────────────────────────────────

def get_channels() -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT channel_username FROM channels").fetchall()
    conn.close()
    return [r["channel_username"] for r in rows]


def add_channel(channel: str) -> bool:
    conn = get_conn()
    try:
        conn.execute("INSERT INTO channels (channel_username) VALUES (?)", (channel.lower(),))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_channel(channel: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM channels WHERE channel_username = ?", (channel.lower(),))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return removed


# ── User Keywords ──────────────────────────────────────────────

def get_user_keywords(user_id: int) -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT keyword FROM user_keywords WHERE user_id = ?", (user_id,)
    ).fetchall()
    conn.close()
    return [r["keyword"] for r in rows]


def add_user_keyword(user_id: int, keyword: str) -> bool:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO user_keywords (user_id, keyword) VALUES (?, ?)",
            (user_id, keyword.lower()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_user_keyword(user_id: int, keyword: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM user_keywords WHERE user_id = ? AND keyword = ?",
        (user_id, keyword.lower()),
    )
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return removed


# ── Suggestions ────────────────────────────────────────────────

def add_suggestion(user_id: int, channel: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO suggestions (user_id, channel) VALUES (?, ?)",
        (user_id, channel.lower()),
    )
    conn.commit()
    suggestion_id = cur.lastrowid
    conn.close()
    return suggestion_id


def get_pending_suggestions() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT s.id, s.user_id, s.channel, u.username, u.first_name "
        "FROM suggestions s JOIN users u ON s.user_id = u.user_id "
        "WHERE s.status = 'pending'"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_suggestion_status(suggestion_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE suggestions SET status = ? WHERE id = ?", (status, suggestion_id)
    )
    conn.commit()
    conn.close()


# ── Monitoring helpers ─────────────────────────────────────────

def get_all_users_with_keywords() -> list[dict]:
    """Returns list of {user_id, keywords: [str]} for all users who have keywords."""
    conn = get_conn()
    users = conn.execute("SELECT DISTINCT user_id FROM user_keywords").fetchall()
    result = []
    for u in users:
        uid = u["user_id"]
        kws = conn.execute(
            "SELECT keyword FROM user_keywords WHERE user_id = ?", (uid,)
        ).fetchall()
        result.append({"user_id": uid, "keywords": [r["keyword"] for r in kws]})
    conn.close()
    return result


# ── Bot state (last_check_time etc.) ───────────────────────────

def get_last_check_time() -> datetime | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM bot_state WHERE key = 'last_check_time'"
    ).fetchone()
    conn.close()
    if row:
        return datetime.fromisoformat(row["value"])
    return None


def save_last_check_time(dt: datetime | None = None):
    if dt is None:
        dt = datetime.utcnow()
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('last_check_time', ?)",
        (dt.isoformat(),),
    )
    conn.commit()
    conn.close()


def import_channels_from_config(channels: list[str]):
    """Import channels from old config.json into the database."""
    conn = get_conn()
    for ch in channels:
        conn.execute(
            "INSERT OR IGNORE INTO channels (channel_username) VALUES (?)",
            (ch.lower(),)
        )
    conn.commit()
    conn.close()
