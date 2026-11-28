# SQLite storage for per-channel conversation history.

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    author_id   INTEGER,
    role        TEXT    NOT NULL,
    name        TEXT,
    content     TEXT    NOT NULL,
    tokens      INTEGER NOT NULL DEFAULT 0,
    edited      INTEGER NOT NULL DEFAULT 0,
    timestamp   REAL    NOT NULL,
    -- Which subviral sent it; NULL for people and for Viral itself
    speaker     TEXT
);
CREATE INDEX IF NOT EXISTS idx_channel_time ON history (channel_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS reactions (
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    emoji       TEXT    NOT NULL,
    name        TEXT,
    author_id   INTEGER NOT NULL,
    timestamp   REAL    NOT NULL,
    -- One row per person per emoji per message
    PRIMARY KEY (message_id, emoji, author_id)
);
CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions (message_id);
"""

# Columns added after the original schema, applied on startup
ADDED_COLUMNS = {
    "message_id": "INTEGER",
    "author_id": "INTEGER",
    "edited": "INTEGER NOT NULL DEFAULT 0",
    "speaker": "TEXT",
}


async def init_db(db_file: str | Any) -> aiosqlite.Connection:
    # Open the database, create the schema, and apply pending migrations
    db = await aiosqlite.connect(db_file)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.executescript(SCHEMA)

    async with db.execute("PRAGMA table_info(history)") as cursor:
        existing = {row["name"] for row in await cursor.fetchall()}
    for column, decl in ADDED_COLUMNS.items():
        if column not in existing:
            log.info("Migrating history table: adding column %s", column)
            await db.execute(f"ALTER TABLE history ADD COLUMN {column} {decl}")

    await db.commit()
    return db


async def add_message(
    db: aiosqlite.Connection,
    channel_id: int,
    role: str,
    content: str,
    count_tokens: Callable[[str], int],
    *,
    name: str | None = None,
    message_id: int | None = None,
    author_id: int | None = None,
    speaker: str | None = None,
) -> None:
    # Insert one message into the history table
    try:
        await db.execute(
            "INSERT INTO history "
            "(channel_id, message_id, author_id, role, name, content, tokens, timestamp, "
            "speaker) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, message_id, author_id, role, name, content,
             count_tokens(content), time.time(), speaker),
        )
        await db.commit()
    except Exception:
        log.exception("Failed to insert message into the database")


async def update_message(
    db: aiosqlite.Connection,
    message_id: int,
    content: str,
    count_tokens: Callable[[str], int],
) -> bool:
    # Replace text after a Discord edit, keeping the timestamp. Returns whether a row changed.
    try:
        cursor = await db.execute(
            "UPDATE history SET content = ?, tokens = ?, edited = 1 WHERE message_id = ?",
            (content, count_tokens(content), message_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    except Exception:
        log.exception("Failed to update an edited message")
        return False


async def add_reaction(
    db: aiosqlite.Connection,
    channel_id: int,
    message_id: int,
    emoji: str,
    *,
    name: str | None = None,
    author_id: int,
) -> bool:
    # Record a reaction only if the message is in history. Returns whether it was recorded.
    try:
        async with db.execute(
            "SELECT 1 FROM history WHERE message_id = ? LIMIT 1", (message_id,)
        ) as cursor:
            if await cursor.fetchone() is None:
                return False

        await db.execute(
            "INSERT OR IGNORE INTO reactions "
            "(channel_id, message_id, emoji, name, author_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, message_id, emoji, name, author_id, time.time()),
        )
        await db.commit()
        return True
    except Exception:
        log.exception("Failed to record a reaction")
        return False


async def remove_reaction(
    db: aiosqlite.Connection, message_id: int, emoji: str, author_id: int
) -> None:
    try:
        await db.execute(
            "DELETE FROM reactions WHERE message_id = ? AND emoji = ? AND author_id = ?",
            (message_id, emoji, author_id),
        )
        await db.commit()
    except Exception:
        log.exception("Failed to remove a reaction")


async def get_reactions(
    db: aiosqlite.Connection, message_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    # Reactions for the given messages, oldest first, grouped by message
    ids = [i for i in message_ids if i]
    if not ids:
        return {}

    placeholders = ",".join("?" * len(ids))
    try:
        async with db.execute(
            f"SELECT message_id, emoji, name FROM reactions "
            f"WHERE message_id IN ({placeholders}) ORDER BY timestamp ASC",
            ids,
        ) as cursor:
            rows = await cursor.fetchall()
    except Exception:
        log.exception("Failed to fetch reactions")
        return {}

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["message_id"], []).append(dict(row))
    return grouped


async def get_context(
    db: aiosqlite.Connection,
    channel_id: int,
    *,
    token_budget: int,
    min_messages: int,
    max_messages: int,
) -> list[dict[str, Any]]:
    # Recent messages, oldest first, within token_budget (history only) but never fewer than min_messages
    budget = max(int(token_budget), 512)
    try:
        query = """
            WITH recent AS (
                SELECT message_id, author_id, role, name, content, tokens, edited, timestamp, speaker
                FROM history
                WHERE channel_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ),
            running AS (
                SELECT *, SUM(tokens) OVER (ORDER BY timestamp DESC) AS cumulative
                FROM recent
            )
            SELECT message_id, author_id, role, name, content, edited, timestamp, speaker
            FROM running
            WHERE cumulative <= ?
            ORDER BY timestamp ASC
        """
        async with db.execute(query, (channel_id, max_messages, budget)) as cursor:
            rows = await cursor.fetchall()

        if len(rows) < min_messages:
            fallback = """
                SELECT message_id, author_id, role, name, content, edited, timestamp, speaker FROM (
                    SELECT message_id, author_id, role, name, content, edited, timestamp, speaker
                    FROM history
                    WHERE channel_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                ) ORDER BY timestamp ASC
            """
            async with db.execute(fallback, (channel_id, min_messages)) as cursor:
                rows = await cursor.fetchall()

        return [dict(row) for row in rows]
    except Exception:
        log.exception("Failed to fetch context from the database")
        return []


async def search_history(
    db: aiosqlite.Connection,
    channel_id: int,
    query: str,
    *,
    limit: int = 10,
    before_timestamp: float | None = None,
) -> list[dict[str, Any]]:
    # Substring search, newest first; before_timestamp skips what the model already sees
    term = query.strip()
    if not term:
        return []

    # Escape % and _ so they are not wildcards
    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = """
        SELECT message_id, role, name, content, timestamp
        FROM history
        WHERE channel_id = ? AND content LIKE ? ESCAPE '\\'
    """
    params: list[Any] = [channel_id, pattern]
    if before_timestamp is not None:
        sql += " AND timestamp < ?"
        params.append(before_timestamp)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(max(1, min(limit, 25)))

    try:
        async with db.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]
    except Exception:
        log.exception("History search failed")
        return []
