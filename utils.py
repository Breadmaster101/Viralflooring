# Shared helpers: logging setup, typing indicator, message splitting,
# and turning stored history rows into a model payload.

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import re
import time
from typing import Any

import discord

from config import LoggingConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------

def setup_logging(cfg: LoggingConfig, log_path) -> None:
    # Configure root logging: rotating file plus console
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if cfg.file:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=getattr(logging, cfg.level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    # httpx logs every request at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
#  Typing indicator
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def safe_typing(channel: discord.abc.Messageable):
    # Shows typing, giving up quietly. /typing can 429 forever, so slowmode
    # channels are skipped and the rest get a short timeout.
    if getattr(channel, "slowmode_delay", 0):
        yield
        return

    typing_ctx = channel.typing()
    try:
        await asyncio.wait_for(typing_ctx.__aenter__(), timeout=1.5)
    except (discord.HTTPException, asyncio.TimeoutError) as e:
        log.warning("Typing indicator unavailable, continuing without it: %s", e)
        yield
        return

    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await typing_ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
#  Message splitting
# ---------------------------------------------------------------------------

# A leading clock stamp as written by _stamp: "[19:13] " or "[2026-09-01 19:13] "
_LEADING_STAMP = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2}\s+)?\d{1,2}:\d{2}\]\s*")


def strip_transcript_stamp(text: str) -> str:
    # Drop a timestamp the model copied from the transcript
    return _LEADING_STAMP.sub("", text, count=1)


# How a person is stored when their nickname differs: "alice (nickname: Sam)"
_STORED_NAME = re.compile(r"^(?P<user>.+?)\s*\(nickname:\s*(?P<nick>.*?)\)\s*$")


def split_display_name(name: str) -> tuple[str, str | None]:
    # Split a stored author name into (username, nickname or None)
    text = str(name or "").strip()
    match = _STORED_NAME.match(text)
    if not match:
        return text, None
    return match.group("user").strip(), match.group("nick").strip() or None


def chat_participants(rows: list[dict[str, Any]]) -> dict[str, str]:
    # Map folded usernames and nicknames to usernames, in order of appearance.
    # Usernames are claimed first, so they win over someone else's nickname.
    people: dict[str, str] = {}
    pairs: list[tuple[str, str | None]] = []

    for row in rows:
        if row.get("role") != "user":
            continue
        username, nickname = split_display_name(row.get("name") or "")
        if username:
            pairs.append((username, nickname))

    for username, _ in pairs:
        people.setdefault(username.casefold(), username)
    for username, nickname in pairs:
        if nickname:
            people.setdefault(nickname.casefold(), username)
    return people


def split_message(text: str, limit: int = 2000) -> list[str]:
    # Split text into chunks within Discord's character limit, on whitespace
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks


# ---------------------------------------------------------------------------
#  History -> model payload
# ---------------------------------------------------------------------------

NO_NEW_MESSAGES = (
    "(no new messages since you last spoke; decide whether to say anything else)"
)


def clock_line(now: float | None = None) -> str:
    # Sits at the end of the chat so it does not break the cacheable prompt prefix
    return time.strftime("(it is now %Y-%m-%d %H:%M:%S %Z)", time.localtime(now))


def format_reactions(reactions: list[dict[str, Any]]) -> str:
    # One line per message, grouped by emoji
    by_emoji: dict[str, list[str]] = {}
    for reaction in reactions:
        by_emoji.setdefault(reaction["emoji"], []).append(reaction.get("name") or "someone")
    parts = [f"{emoji} {', '.join(who)}" for emoji, who in by_emoji.items()]
    return f"(reactions: {'; '.join(parts)})"


def _stamp(timestamp: float, previous: float | None) -> str:
    # Clock time, with the date only when the day changes
    when = time.localtime(timestamp)
    if previous is not None:
        before = time.localtime(previous)
        if (when.tm_year, when.tm_yday) == (before.tm_year, before.tm_yday):
            return time.strftime("%H:%M", when)
    return time.strftime("%Y-%m-%d %H:%M", when)


def build_history(
    rows: list[dict[str, Any]],
    reactions: dict[int, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, int], float | None]:
    # Returns (messages, handle -> message ID, oldest visible timestamp).
    # User messages get a short [#n] handle so the model never sees raw IDs.
    formatted: list[dict[str, str]] = []
    refs: dict[str, int] = {}
    oldest_ts: float | None = None
    previous_ts: float | None = None
    next_handle = 1
    reactions = reactions or {}

    for row in rows:
        role = row["role"]
        content = row["content"]
        timestamp = row.get("timestamp")
        if oldest_ts is None and timestamp:
            oldest_ts = timestamp

        # The bot may have answered the old wording
        if row.get("edited"):
            content = f"{content} (edited)"

        message_id = row.get("message_id")
        if role == "user":
            name = row.get("name") or "someone"
            if message_id:
                handle = str(next_handle)
                next_handle += 1
                refs[handle] = message_id
                content = f"[#{handle}] {name}: {content}"
            else:
                content = f"{name}: {content}"

        # Stamp only other people's messages; stamping the bot's own makes it copy the pattern
        if timestamp and role == "user":
            content = f"[{_stamp(timestamp, previous_ts)}] {content}"
            previous_ts = timestamp

        on_this = reactions.get(message_id) if message_id else None
        if on_this:
            content = f"{content}\n{format_reactions(on_this)}"

        formatted.append(content if role == "user" else f"you: {content}")

    if not formatted:
        return [], refs, oldest_ts

    # Tell the bot when its own message was the last thing said
    if rows and rows[-1]["role"] == "assistant":
        formatted.append(NO_NEW_MESSAGES)

    # One user turn for the whole channel: past bot messages as assistant turns
    # taught the model to answer in prose instead of calling tools
    return [{"role": "user", "content": "\n".join(formatted)}], refs, oldest_ts


# ---------------------------------------------------------------------------
#  Easter eggs
# ---------------------------------------------------------------------------

JACKPOT_TRIGGERS = ("woke", "robot", "liberal")
JACKPOT_SUFFIX = " I hit the jack pot! $                  1"


def apply_easter_eggs(response: str, last_user_message: str) -> str:
    # Long-standing in-joke: append the jackpot line on certain trigger words
    if any(word in last_user_message.lower() for word in JACKPOT_TRIGGERS):
        return response.rstrip() + JACKPOT_SUFFIX
    return response
