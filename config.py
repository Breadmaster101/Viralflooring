"""
config.py — Loads configuration from config.jsonc (or config.json fallback),
resolves LLM backend settings, and exports all constants used by the bot.
"""

import json
import re
import logging
import sys
import os

# ---------------------------------------------------------------------------
#  JSONC loader (strips // comments before parsing)
# ---------------------------------------------------------------------------

def _load_jsonc(path):
    """Load a JSONC file (JSON with // line comments)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    # Remove single-line // comments (but not inside strings)
    stripped = re.sub(r'(?<!["\':])//.*', '', raw)
    return json.loads(stripped)


def _load_config():
    """Try config.jsonc first, then fall back to config.json."""
    for filename in ("config.jsonc", "config.json"):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.isfile(path):
            try:
                data = _load_jsonc(path) if filename.endswith(".jsonc") else json.load(open(path, encoding="utf-8"))
                logging.info(f"Loaded configuration from {filename}")
                return data
            except Exception as e:
                logging.critical(f"Failed to parse {filename}: {e}")
                sys.exit(1)
    logging.critical("No config file found! Create config.jsonc (or config.json).")
    sys.exit(1)


config = _load_config()

# ---------------------------------------------------------------------------
#  Discord
# ---------------------------------------------------------------------------

DISCORD_TOKEN = config.get("DISCORD_TOKEN", "your-discord-bot-token")

# ---------------------------------------------------------------------------
#  Main LLM backend
# ---------------------------------------------------------------------------

LLM_BACKEND = config.get("LLM_BACKEND", "cerebras")

# Local
LLAMA_BASE_URL = config.get("LLAMA_BASE_URL", "http://127.0.0.1:8080/v1")
LLAMA_API_KEY  = config.get("LLAMA_API_KEY", "sk-no-key-required")
LLAMA_MODEL_ID = config.get("LLAMA_MODEL_ID", "default")

# Gemini
GEMINI_API_KEY  = config.get("GEMINI_API_KEY", "your-gemini-api-key")
GEMINI_MODEL_ID = config.get("GEMINI_MODEL_ID", "gemini-3.1-flash-lite-preview")

# OpenRouter
OPENROUTER_API_KEY  = config.get("OPENROUTER_API_KEY", "your-openrouter-api-key")
OPENROUTER_MODEL_ID = config.get("OPENROUTER_MODEL_ID", "google/gemma-3-27b-it:free")

# Cerebras
CEREBRAS_API_KEY  = config.get("CEREBRAS_API_KEY", "your-cerebras-api-key")
CEREBRAS_MODEL_ID = config.get("CEREBRAS_MODEL_ID", "qwen-3-235b-2507")

# Mistral
MISTRAL_API_KEY  = config.get("MISTRAL_API_KEY", "your-mistral-api-key")
MISTRAL_MODEL_ID = config.get("MISTRAL_MODEL_ID", "mistral-large-latest")

# ---------------------------------------------------------------------------
#  Thinking / reasoning control
# ---------------------------------------------------------------------------

DISABLE_GEMINI_THINKING       = config.get("DISABLE_GEMINI_THINKING", False)

# ---------------------------------------------------------------------------
#  General settings
# ---------------------------------------------------------------------------

MAX_TOKEN_LIMIT      = config.get("MAX_TOKEN_LIMIT", 1000)
DB_FILE              = config.get("DB_FILE", "history.db")
MAX_DISCORD_MSG_LEN  = config.get("MAX_DISCORD_MSG_LEN", 2000)
API_COOLDOWN_SECONDS = config.get("API_COOLDOWN_SECONDS", 1.0)
API_TIMEOUT_SECONDS  = config.get("API_TIMEOUT_SECONDS", 60.0)

# ---------------------------------------------------------------------------
#  Memory system
# ---------------------------------------------------------------------------

DISABLE_MEMORY_SYSTEM = config.get("DISABLE_MEMORY_SYSTEM", False)
MEMORY_SCOPE   = config.get("MEMORY_SCOPE", "global")    # "global" or "per_server"
MAX_MEMORIES   = config.get("MAX_MEMORIES", 40)

# ---------------------------------------------------------------------------
#  Resolve backend → (base_url, api_key, model_id)
# ---------------------------------------------------------------------------

_BACKEND_MAP = {
    "local":      lambda bu, ak, mi: (bu, ak, mi),
    "gemini":     lambda bu, ak, mi: ("https://generativelanguage.googleapis.com/v1beta/openai/", ak, mi),
    "openrouter": lambda bu, ak, mi: ("https://openrouter.ai/api/v1", ak, mi),
    "cerebras":   lambda bu, ak, mi: ("https://api.cerebras.ai/v1", ak, mi),
    "mistral":    lambda bu, ak, mi: ("https://api.mistral.ai/v1", ak, mi),
}


def _resolve(backend, local_url, local_key, local_model,
             gemini_key, gemini_model,
             openrouter_key, openrouter_model,
             cerebras_key, cerebras_model,
             mistral_key, mistral_model):
    if backend == "local":
        return local_url, local_key, local_model
    elif backend == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta/openai/", gemini_key, gemini_model
    elif backend == "openrouter":
        return "https://openrouter.ai/api/v1", openrouter_key, openrouter_model
    elif backend == "cerebras":
        return "https://api.cerebras.ai/v1", cerebras_key, cerebras_model
    elif backend == "mistral":
        return "https://api.mistral.ai/v1", mistral_key, mistral_model
    else:
        raise ValueError(
            f"Unknown LLM backend: '{backend}'. Must be 'local', 'gemini', 'openrouter', 'cerebras', or 'mistral'."
        )


# Main backend
API_BASE_URL, API_KEY, MODEL_ID = _resolve(
    LLM_BACKEND,
    LLAMA_BASE_URL, LLAMA_API_KEY, LLAMA_MODEL_ID,
    GEMINI_API_KEY, GEMINI_MODEL_ID,
    OPENROUTER_API_KEY, OPENROUTER_MODEL_ID,
    CEREBRAS_API_KEY, CEREBRAS_MODEL_ID,
    MISTRAL_API_KEY, MISTRAL_MODEL_ID,
)

