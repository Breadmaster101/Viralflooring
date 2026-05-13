"""
utils.py — Shared utilities: typing indicator, message splitting,
           history sanitization, and troll-mode console handler.
"""

import sys
import logging
import asyncio
import contextlib
import discord

from config import MAX_DISCORD_MSG_LEN


# ---------------------------------------------------------------------------
#  Troll-mode console handler (redraws the input prompt after log output)
# ---------------------------------------------------------------------------

input_buffer = ""
input_lock = __import__("threading").Lock()


class TrollConsoleHandler(logging.StreamHandler):
    """Custom log handler that preserves the troll-mode input prompt."""

    def emit(self, record):
        try:
            msg = self.format(record)
            with input_lock:
                sys.stdout.write('\r' + ' ' * 100 + '\r')
                sys.stdout.write(msg + '\n')
                sys.stdout.write("> " + input_buffer)
                sys.stdout.flush()
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
#  Safe typing indicator
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def safe_typing(channel):
    """
    Discord returns an infinite 429 (40062 Service resource is being rate limited)
    on the /typing endpoint if the channel has slowmode enabled or if multiple
    typing indicators are triggered concurrently.
    This safely checks for slowmode and ignores HTTP rate limit exceptions by enforcing a timeout.
    """
    if getattr(channel, 'slowmode_delay', 0) == 0:
        typing_ctx = channel.typing()
        try:
            # wait_for prevents hanging if discord.py infinitely retries a 429
            await asyncio.wait_for(typing_ctx.__aenter__(), timeout=1.5)
        except (discord.errors.HTTPException, asyncio.TimeoutError) as e:
            logging.warning(f"Typing indicator failed or timed out, proceeding without it: {e}")
            yield
            return

        try:
            yield
        finally:
            await typing_ctx.__aexit__(None, None, None)
    else:
        yield


# ---------------------------------------------------------------------------
#  Message splitting
# ---------------------------------------------------------------------------

def split_message(text, limit=MAX_DISCORD_MSG_LEN):
    """Split a long message into chunks that fit within Discord's character limit."""
    chunks = []
    while len(text) > limit:
        split_index = text.rfind('\n', 0, limit)
        if split_index == -1:
            split_index = text.rfind(' ', 0, limit)
        if split_index == -1:
            split_index = limit
        chunks.append(text[:split_index])
        text = text[split_index:].strip()
    if text:
        chunks.append(text)
    return chunks


# ---------------------------------------------------------------------------
#  History sanitization
# ---------------------------------------------------------------------------

def get_sanitized_history(history_list):
    """
    Prepare raw DB history for the LLM:
    - Prefix user messages with the speaker's name
    - Strip leading assistant messages (LLM can't start with its own output)
    - Merge consecutive same-role messages
    """
    if not history_list:
        return []

    sanitized = []
    for msg in history_list:
        role = msg['role']
        content = msg['content']
        name = msg.get('name')
        if role == 'user' and name:
            content = f"{name} said: {content}"
        sanitized.append({'role': role, 'content': content})

    # Strip leading assistant messages
    while sanitized and sanitized[0]['role'] == 'assistant':
        sanitized.pop(0)

    # Merge consecutive same-role messages
    merged = []
    for entry in sanitized:
        if merged and merged[-1]['role'] == entry['role']:
            merged[-1]['content'] += f"\n{entry['content']}"
        else:
            merged.append(entry)
    # Strip trailing assistant messages (API requires last message to be user)
    while merged and merged[-1]['role'] == 'assistant':
        merged.pop()

    return merged
