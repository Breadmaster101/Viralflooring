# Troll mode: you type the bot's messages by hand. The model is not called;
# the typing indicator follows your keystrokes.

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING

from console import Console
from utils import safe_typing

if TYPE_CHECKING:
    from bot_client import ViralBot

log = logging.getLogger(__name__)

BANNER = (
    "\n--- Troll mode. Type a message and press Enter to send it as the bot. "
    "Ctrl+C or Ctrl+D to quit. ---\n"
)

# How long the typing indicator lingers after the last keystroke.
TYPING_LINGER_SECONDS = 5.0


async def troll_input_worker(bot: "ViralBot") -> None:
    # Read hand-typed lines from the console and send them to Discord
    console = Console()
    console.install_log_handler()

    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | None] = asyncio.Queue()
    state = {"last_keystroke": 0.0, "typing_task": None}

    if not console.interactive:
        log.warning("stdin is not a terminal; troll mode will read whole lines only.")

    # --- typing indicator ------------------------------------------------

    async def keep_typing() -> None:
        channel = bot.last_troll_channel
        if channel is None:
            return
        async with safe_typing(channel):
            while time.monotonic() - state["last_keystroke"] < TYPING_LINGER_SECONDS:
                await asyncio.sleep(1)

    def poke_typing() -> None:
        state["last_keystroke"] = time.monotonic()
        task = state["typing_task"]
        if task is None or task.done():
            state["typing_task"] = loop.create_task(keep_typing())

    def on_keystroke() -> None:
        # Called from the reader thread.
        loop.call_soon_threadsafe(poke_typing)

    # --- reader thread ---------------------------------------------------

    def reader() -> None:
        while True:
            try:
                line = console.read_line(on_keystroke)
            except (EOFError, KeyboardInterrupt):
                loop.call_soon_threadsafe(lines.put_nowait, None)
                return
            except Exception:
                log.exception("Console reader failed")
                loop.call_soon_threadsafe(lines.put_nowait, None)
                return
            loop.call_soon_threadsafe(lines.put_nowait, line)

    # A daemon thread, so a blocked read never keeps the process alive.
    threading.Thread(target=reader, name="troll-input", daemon=True).start()
    print(BANNER)

    # --- send loop -------------------------------------------------------

    while True:
        line = await lines.get()
        if line is None:
            log.info("Troll input closed; shutting down.")
            await bot.close()
            return

        text = line.strip()
        if not text:
            continue

        channel = bot.last_troll_channel
        if channel is None:
            log.warning("No channel has spoken yet, so there is nowhere to send that.")
            continue

        try:
            await bot.send_as_bot(channel, text)
        except Exception:
            log.exception("Failed to send the message")
