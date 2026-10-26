# Persistent long-term facts, stored as a JSON list per scope.
# Writes go through a temp file and atomic replace.

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from config import MemoryConfig

log = logging.getLogger(__name__)


class MemoryStore:
    def __init__(self, cfg: MemoryConfig, directory: Path):
        self.cfg = cfg
        self.directory = directory

    # ------------------------------------------------------------------

    def _file(self, guild_id: int | None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.cfg.scope == "per_server" and guild_id is not None:
            return self.directory / f"memory_{guild_id}.json"
        return self.directory / "memory_global.json"

    def load(self, guild_id: int | None = None) -> list[str]:
        path = self._file(guild_id)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Failed to load memories from %s", path)
            return []
        memories = data.get("memories", []) if isinstance(data, dict) else []
        return [m for m in memories if isinstance(m, str) and m.strip()]

    def save(self, memories: list[str], guild_id: int | None = None) -> None:
        path = self._file(guild_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps({"memories": memories}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            log.exception("Failed to save memories to %s", path)
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------

    def remember(self, fact: str, guild_id: int | None = None) -> tuple[bool, str]:
        # Add one fact. Returns (changed, human-readable outcome)
        fact = fact.strip()
        if not fact:
            return False, "Nothing was saved: the fact was empty."

        memories = self.load(guild_id)
        if any(m.strip().lower() == fact.lower() for m in memories):
            return False, "Already remembered — nothing changed."

        memories.append(fact)
        dropped = ""
        if len(memories) > self.cfg.max_memories:
            removed = memories[: len(memories) - self.cfg.max_memories]
            memories = memories[-self.cfg.max_memories :]
            dropped = f" Memory was full, so the oldest was dropped: {removed[0]!r}."

        self.save(memories, guild_id)
        return True, f"Saved. You now remember {len(memories)}/{self.cfg.max_memories} facts.{dropped}"

    def forget(self, fact: str, guild_id: int | None = None) -> tuple[bool, str]:
        # Remove one fact by exact (case-insensitive) text
        target = fact.strip().lower()
        memories = self.load(guild_id)
        kept = [m for m in memories if m.strip().lower() != target]

        if len(kept) == len(memories):
            return False, (
                "No memory matched that text, so nothing was removed. Everything "
                "you remember is listed in your system prompt; a fact has to be "
                "quoted from there word for word to be forgotten."
            )

        self.save(kept, guild_id)
        return True, f"Forgotten. {len(kept)} facts remain."

    # ------------------------------------------------------------------

    def format_for_prompt(self, guild_id: int | None = None) -> str:
        # States the limit so the bot knows saving may drop the oldest
        memories = self.load(guild_id)
        limit = self.cfg.max_memories
        header = (
            f"Things you have chosen to remember — you hold {len(memories)} of "
            f"{limit}, and once full, saving another drops the oldest:"
        )
        if not memories:
            return f"{header}\n(nothing yet)"
        return header + "\n" + "\n".join(f"- {m}" for m in memories)
