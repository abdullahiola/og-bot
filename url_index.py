"""SQLite-backed URL index for social link lookups — port of url-index.ts."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

from social_url import normalize_for_social_match

_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".data")
_DB_PATH = os.path.join(_DB_DIR, "url-index.sqlite")

_db: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _db
    if _db is not None:
        return _db
    Path(_DB_DIR).mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(_DB_PATH, check_same_thread=False)
    _db.execute("PRAGMA journal_mode = WAL")
    _db.executescript("""
        CREATE TABLE IF NOT EXISTS token_links (
            mint       TEXT    NOT NULL,
            url_norm   TEXT    NOT NULL,
            url_raw    TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 'unknown',
            discovered_at INTEGER NOT NULL,
            UNIQUE(mint, url_norm)
        );
        CREATE INDEX IF NOT EXISTS idx_token_links_url ON token_links(url_norm);
        CREATE TABLE IF NOT EXISTS poll_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    _db.commit()
    return _db


def upsert_token_links(mint: str, urls: list[str], source: str) -> None:
    now = int(time.time() * 1000)
    db = get_db()
    for raw in urls:
        norm = normalize_for_social_match(raw)
        if not norm or len(norm) < 3:
            continue
        # Bare host (e.g. x.com) matches every x.com/... URL in search — skip.
        if "/" not in norm:
            continue
        try:
            db.execute(
                "INSERT OR IGNORE INTO token_links (mint, url_norm, url_raw, source, discovered_at) VALUES (?, ?, ?, ?, ?)",
                (mint, norm, raw, source, now),
            )
        except sqlite3.Error:
            continue
    db.commit()


def _escape_sql_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_by_url(target_norms: list[str]) -> list[str]:
    """Find mints whose stored URLs path-prefix-match any normalized target."""
    if not target_norms:
        return []
    db = get_db()
    mints: set[str] = set()

    for t in target_norms:
        if not t or len(t) < 3:
            continue
        pat = f"{_escape_sql_like(t)}/%"
        cursor = db.execute(
            """SELECT DISTINCT mint FROM token_links
               WHERE url_norm = ?
                  OR url_norm LIKE ? ESCAPE '\\'
                  OR (? LIKE url_norm || '/%' AND url_norm LIKE '%/%')""",
            (t, pat, t),
        )
        for row in cursor:
            mints.add(row[0])

    return list(mints)


def count_indexed_tokens() -> int:
    row = get_db().execute("SELECT COUNT(DISTINCT mint) FROM token_links").fetchone()
    return row[0] if row else 0


def get_poll_state(key: str) -> str | None:
    row = get_db().execute("SELECT value FROM poll_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_poll_state(key: str, value: str) -> None:
    db = get_db()
    db.execute("INSERT OR REPLACE INTO poll_state (key, value) VALUES (?, ?)", (key, value))
    db.commit()
