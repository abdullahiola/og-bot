"""SQLite-backed tracking for users, groups, and searches."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".data")
_DB_PATH = os.path.join(_DB_DIR, "stats.sqlite")

_db: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is not None:
        return _db
    Path(_DB_DIR).mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(_DB_PATH, check_same_thread=False)
    _db.execute("PRAGMA journal_mode = WAL")
    _db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            last_seen_at INTEGER NOT NULL,
            first_seen_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_id      INTEGER PRIMARY KEY,
            title        TEXT,
            joined_at    INTEGER NOT NULL,
            left_at      INTEGER
        );
        CREATE TABLE IF NOT EXISTS search_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            chat_id      INTEGER NOT NULL,
            query        TEXT NOT NULL,
            mode         TEXT,
            ts           INTEGER NOT NULL
        );
    """)
    _db.commit()
    return _db


# ── User tracking ─────────────────────────────────────────────────────

def track_user(user_id: int, username: str | None, first_name: str | None) -> None:
    """Upsert a user entry on every interaction."""
    now = int(time.time())
    db = _get_db()
    db.execute(
        """INSERT INTO users (user_id, username, first_name, last_seen_at, first_seen_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               username   = excluded.username,
               first_name = excluded.first_name,
               last_seen_at = excluded.last_seen_at""",
        (user_id, username, first_name, now, now),
    )
    db.commit()


# ── Group tracking ────────────────────────────────────────────────────

def track_group_join(chat_id: int, title: str | None) -> None:
    """Record the bot joining a group."""
    now = int(time.time())
    db = _get_db()
    db.execute(
        """INSERT INTO groups (chat_id, title, joined_at, left_at)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT(chat_id) DO UPDATE SET
               title    = excluded.title,
               joined_at = excluded.joined_at,
               left_at  = NULL""",
        (chat_id, title, now),
    )
    db.commit()


def track_group_left(chat_id: int) -> None:
    """Record the bot leaving/being removed from a group."""
    now = int(time.time())
    db = _get_db()
    db.execute(
        "UPDATE groups SET left_at = ? WHERE chat_id = ?",
        (now, chat_id),
    )
    db.commit()


def track_group_activity(chat_id: int, title: str | None) -> None:
    """Ensure any group the bot receives commands in is tracked."""
    now = int(time.time())
    db = _get_db()
    db.execute(
        """INSERT INTO groups (chat_id, title, joined_at, left_at)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT(chat_id) DO UPDATE SET
               title = COALESCE(excluded.title, groups.title),
               left_at = NULL""",
        (chat_id, title, now),
    )
    db.commit()


# ── Search logging ────────────────────────────────────────────────────

def log_search(user_id: int, chat_id: int, query: str, mode: str | None) -> None:
    """Record a search for analytics."""
    now = int(time.time())
    db = _get_db()
    db.execute(
        "INSERT INTO search_log (user_id, chat_id, query, mode, ts) VALUES (?, ?, ?, ?, ?)",
        (user_id, chat_id, query, mode, now),
    )
    db.commit()


# ── Stats queries ─────────────────────────────────────────────────────

def get_stats() -> dict:
    """Return aggregate stats."""
    db = _get_db()

    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active_groups = db.execute("SELECT COUNT(*) FROM groups WHERE left_at IS NULL").fetchone()[0]
    total_searches = db.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]

    # Users active in the last 24 hours
    day_ago = int(time.time()) - 86400
    active_24h = db.execute(
        "SELECT COUNT(*) FROM users WHERE last_seen_at >= ?", (day_ago,)
    ).fetchone()[0]

    # Searches in last 24h
    searches_24h = db.execute(
        "SELECT COUNT(*) FROM search_log WHERE ts >= ?", (day_ago,)
    ).fetchone()[0]

    return {
        "total_users": total_users,
        "active_24h": active_24h,
        "active_groups": active_groups,
        "total_searches": total_searches,
        "searches_24h": searches_24h,
    }


def format_subscriber_count(n: int) -> str:
    """Format a number like '1.2K' or '345' — for footer display."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
