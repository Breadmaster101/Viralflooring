"""
memory.py — Persistent memory system for Viral.
Loads/saves a simple JSON list of memory strings.
Supports global or per-guild scoping via config.
"""

import json
import logging
import os

from config import MAX_MEMORIES, MEMORY_SCOPE

MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memories")


def _memory_file(guild_id=None):
    """Return the path to the appropriate memory file."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    if MEMORY_SCOPE == "per_server" and guild_id is not None:
        return os.path.join(MEMORY_DIR, f"memory_{guild_id}.json")
    return os.path.join(MEMORY_DIR, "memory_global.json")


def load_memories(guild_id=None) -> list[str]:
    """Load the current memory list from disk."""
    path = _memory_file(guild_id)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("memories", [])
    except Exception as e:
        logging.error(f"Failed to load memories from {path}: {e}")
        return []


def save_memories(memories: list[str], guild_id=None):
    """Persist the memory list to disk."""
    path = _memory_file(guild_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"memories": memories}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Failed to save memories to {path}: {e}")


def apply_memory_updates(adds: list[str], removes: list[str], guild_id=None) -> list[str]:
    """Add/remove memories and save. Returns the updated list."""
    memories = load_memories(guild_id)

    # Remove first (case-insensitive matching)
    for r in removes:
        r_lower = r.strip().lower()
        memories = [m for m in memories if m.strip().lower() != r_lower]

    # Then add (skip duplicates)
    existing_lower = {m.strip().lower() for m in memories}
    for a in adds:
        a = a.strip()
        if a and a.lower() not in existing_lower:
            memories.append(a)
            existing_lower.add(a.lower())

    # Enforce cap — keep newest
    if len(memories) > MAX_MEMORIES:
        memories = memories[-MAX_MEMORIES:]

    save_memories(memories, guild_id)
    return memories


def format_memories_for_prompt(guild_id=None) -> str:
    """Format memories as a block to inject into the system prompt."""
    memories = load_memories(guild_id)
    header = "Your personal memories (things you've chosen to remember):"
    if not memories:
        return f"{header}\n(none yet)"
    bullets = "\n".join(f"- {m}" for m in memories)
    return f"{header}\n{bullets}"
