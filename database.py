"""
database.py — SQLite database operations for conversation history.
"""

import time
import logging
import aiosqlite


async def init_db(db_file):
    """Connect to the SQLite database and create tables if needed."""
    try:
        db = await aiosqlite.connect(db_file)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute('''CREATE TABLE IF NOT EXISTS history 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          channel_id INTEGER, 
                          role TEXT, 
                          name TEXT,
                          content TEXT, 
                          tokens INTEGER,
                          timestamp REAL)''')
        await db.execute('''CREATE INDEX IF NOT EXISTS idx_channel_time ON history (channel_id, timestamp DESC)''')
        await db.commit()
        return db
    except Exception as e:
        logging.critical(f"Database initialization failed: {e}")
        exit()


async def add_message(db, channel_id, role, content, count_tokens_fn, name=None):
    """Insert a message into the history table."""
    tokens = count_tokens_fn(content)
    try:
        await db.execute(
            "INSERT INTO history (channel_id, role, name, content, tokens, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, role, name, content, tokens, time.time())
        )
        await db.commit()
    except Exception as e:
        logging.error(f"Failed to insert message into DB: {e}")


async def get_context(db, channel_id, max_token_limit, bot_persona, count_tokens_fn):
    """Retrieve recent conversation context that fits within the token budget."""
    system_tokens = count_tokens_fn(bot_persona) if bot_persona else 0
    available_tokens = max_token_limit - system_tokens
    try:
        query = """
            WITH OrderedHistory AS (
                SELECT role, name, content, tokens, timestamp
                FROM history 
                WHERE channel_id = ? 
                ORDER BY timestamp DESC 
                LIMIT 50
            ),
            RunningTotal AS (
                SELECT role, name, content, timestamp,
                       SUM(tokens) OVER (ORDER BY timestamp DESC) as cumulative_tokens
                FROM OrderedHistory
            )
            SELECT role, name, content FROM RunningTotal 
            WHERE cumulative_tokens <= ?
            ORDER BY timestamp ASC
        """
        async with db.execute(query, (channel_id, available_tokens)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logging.error(f"Failed to fetch context from DB: {e}")
        return []
