"""
bot.py — Entry point for the Viralflooring Discord bot.

Usage:
    python bot.py              # Normal mode
    python bot.py --troll-mode # Troll mode (manual responses via console)
"""

import sys
import logging

# logging config
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

from config import DISCORD_TOKEN
from bot_client import LlamaDiscordBot

if __name__ == "__main__":
    troll_mode = "--troll-mode" in sys.argv
    bot = LlamaDiscordBot(troll_mode=troll_mode)
    bot.run(DISCORD_TOKEN)