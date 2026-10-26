# What the bot remembers about specific people, filed by Discord username.
# People talking in the channel are put in the prompt; the rest are one tool call away.
# Stored as JSON per scope, written via temp file and atomic replace.

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

from config import MemoryConfig
from utils import split_display_name

log = logging.getLogger(__name__)

# Rejects a sentence passed in the username field
MAX_USERNAME_LENGTH = 64


def _key(username: str) -> str:
    # Username folded for comparison; used as the JSON key
    return " ".join(username.split()).strip().lstrip("@").strip().casefold()


def _clean_name(raw: Any) -> tuple[str, str]:
    # Returns (username, problem); strips a "(nickname: ...)" suffix
    name, _ = split_display_name(str(raw or ""))
    name = " ".join(name.split()).strip().lstrip("@").strip()
    if not name:
        return "", "No username was given, so there is nobody to file that under."
    if len(name) > MAX_USERNAME_LENGTH:
        return "", (
            f"{name[:24]!r}... is {len(name)} characters, which is not a username. "
            f"Pass just the name, and put what you want to remember in the fact."
        )
    return name, ""


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


class UserMemoryStore:
    # Per-person notes, read from disk on each call so hand edits stay live

    def __init__(self, cfg: MemoryConfig, directory: Path):
        self.cfg = cfg
        self.users = cfg.users
        self.directory = directory

    # ------------------------------------------------------------------
    #  Files
    # ------------------------------------------------------------------

    def _file(self, guild_id: int | None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.cfg.scope == "per_server" and guild_id is not None:
            return self.directory / f"users_{guild_id}.json"
        return self.directory / "users_global.json"

    def load(self, guild_id: int | None = None) -> dict[str, dict[str, Any]]:
        # Everyone on file; malformed entries are dropped, not raised on
        path = self._file(guild_id)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Failed to load per-person memories from %s", path)
            return {}

        stored = data.get("users", {}) if isinstance(data, dict) else {}
        if not isinstance(stored, dict):
            return {}

        records: dict[str, dict[str, Any]] = {}
        for key, record in stored.items():
            if not isinstance(record, dict):
                continue
            raw_facts = record.get("facts", [])
            facts = (
                [f.strip() for f in raw_facts if isinstance(f, str) and f.strip()]
                if isinstance(raw_facts, list) else []
            )
            # Empty records prune themselves
            if not facts:
                continue
            name = record.get("name")
            name = name.strip() if isinstance(name, str) and name.strip() else str(key)
            updated = record.get("updated")
            records[_key(str(key))] = {
                "name": name,
                "facts": facts,
                "updated": float(updated) if isinstance(updated, (int, float)) else 0.0,
            }
        return records

    def save(self, records: dict[str, dict[str, Any]],
             guild_id: int | None = None) -> None:
        path = self._file(guild_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps({"users": records}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            log.exception("Failed to save per-person memories to %s", path)
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    #  Editing
    # ------------------------------------------------------------------

    def remember(self, username: str, fact: str,
                 guild_id: int | None = None) -> tuple[bool, str]:
        # File one fact under one person. Returns (changed, outcome)
        name, problem = _clean_name(username)
        if problem:
            return False, problem

        fact = " ".join(str(fact or "").split()).strip()
        if not fact:
            return False, f"Nothing was saved about {name}: the fact was empty."

        records = self.load(guild_id)
        key = _key(name)
        record = records.get(key)

        evicted = ""
        if record is None:
            # New person: evict whoever changed least recently if full
            while records and len(records) >= self.users.max_users:
                oldest = min(records, key=lambda k: records[k]["updated"])
                evicted = (
                    f" Your list of people was full at {self.users.max_users}, so "
                    f"{records[oldest]['name']} was dropped to make room."
                )
                log.info("[UserMemory] Dropped %r to make room for %r",
                         records[oldest]["name"], name)
                del records[oldest]
            record = {"name": name, "facts": [], "updated": 0.0}
            records[key] = record
        else:
            # Keep the spelling current
            record["name"] = name

        if any(_same(existing, fact) for existing in record["facts"]):
            return False, f"You already remember that about {name}, so nothing changed."

        record["facts"].append(fact)
        limit = self.users.max_facts_per_user
        dropped = ""
        if len(record["facts"]) > limit:
            removed = record["facts"][: len(record["facts"]) - limit]
            record["facts"] = record["facts"][-limit:]
            dropped = (
                f" What you had about them was full, so the oldest went: {removed[0]!r}."
            )
        record["updated"] = time.time()

        self.save(records, guild_id)
        return True, (
            f"Saved under {name}. You now remember {len(record['facts'])}/{limit} "
            f"things about them, and have notes on {len(records)}/"
            f"{self.users.max_users} people.{dropped}{evicted}"
        )

    def forget(self, username: str, fact: str,
               guild_id: int | None = None) -> tuple[bool, str]:
        # Remove one fact from one person, by its exact (case-insensitive) text
        name, problem = _clean_name(username)
        if problem:
            return False, problem

        records = self.load(guild_id)
        key = _key(name)
        record = records.get(key)
        if record is None:
            return False, (
                f"You have nothing saved about {name}, so there was nothing to "
                f"remove. Call get_user_memories with no username to see who you "
                f"do have notes on."
            )

        target = str(fact or "")
        kept = [f for f in record["facts"] if not _same(f, target)]
        if len(kept) == len(record["facts"]):
            return False, (
                f"Nothing you remember about {record['name']} matches that text, so "
                f"nothing was removed. Call get_user_memories for the exact wording."
            )

        if kept:
            record["facts"] = kept
            record["updated"] = time.time()
            self.save(records, guild_id)
            return True, (
                f"Forgotten. You still remember {len(kept)} thing(s) about "
                f"{record['name']}."
            )

        # Last fact: remove them from the roster
        del records[key]
        self.save(records, guild_id)
        return True, (
            f"Forgotten. That was the last thing you had about {record['name']}, so "
            f"they are no longer on your list of people."
        )

    # ------------------------------------------------------------------
    #  Reading
    # ------------------------------------------------------------------

    def facts_for(self, username: str, guild_id: int | None = None) -> list[str]:
        record = self.load(guild_id).get(_key(str(username or "")))
        return list(record["facts"]) if record else []

    def format_user(self, username: str, guild_id: int | None = None) -> str:
        # One person's notes, as get_user_memories hands them back
        name, problem = _clean_name(username)
        if problem:
            return problem

        record = self.load(guild_id).get(_key(name))
        if record is None:
            return (
                f"You have nothing saved about {name}. Call get_user_memories with "
                f"no username to see who you do have notes on."
            )
        header = (
            f"What you remember about {record['name']} "
            f"({len(record['facts'])}/{self.users.max_facts_per_user}):"
        )
        return header + "\n" + "\n".join(f"- {f}" for f in record["facts"])

    def format_roster(self, guild_id: int | None = None) -> str:
        # Everyone on file and how much is under each, without the facts
        records = self.load(guild_id)
        if not records:
            return (
                "You have no notes on anyone yet. remember_about_user is how one "
                "gets started."
            )
        listed = ", ".join(
            f"{record['name']} ({len(record['facts'])})" for record in records.values()
        )
        return (
            f"You have notes on {len(records)}/{self.users.max_users} people, and the "
            f"number beside each is how many things you remember about them: {listed}. "
            f"Call get_user_memories with one of these names to read what you have."
        )

    def format_for_prompt(self, present: Iterable[str] = (),
                          guild_id: int | None = None) -> str:
        # Prompt block: people present in full, everyone else as a name
        records = self.load(guild_id)
        header = (
            f"What you remember about specific people, filed under their username, "
            f"notes on {len(records)} of {self.users.max_users} people, up to "
            f"{self.users.max_facts_per_user} things about each:"
        )

        here: list[tuple[str, str]] = []
        for raw in present:
            name = " ".join(str(raw or "").split()).strip()
            key = _key(name)
            if key and all(key != seen for seen, _ in here):
                here.append((key, name))

        blocks: list[str] = []
        unknown: list[str] = []
        for key, name in here:
            record = records.get(key)
            if record is None:
                unknown.append(name)
            else:
                blocks.append(f"{name}:\n" + "\n".join(f"- {f}" for f in record["facts"]))

        present_keys = {key for key, _ in here}
        elsewhere = [
            record["name"] for key, record in records.items() if key not in present_keys
        ]

        parts: list[str] = []
        if blocks:
            parts.append("\n\n".join(blocks))
        if unknown:
            parts.append(f"Nothing saved yet about: {', '.join(unknown)}.")
        if elsewhere:
            parts.append(
                f"You also have notes on people who are not talking here: "
                f"{', '.join(elsewhere)}. Call get_user_memories with one of those "
                f"names to read them."
            )
        if not parts:
            return f"{header}\n(nobody yet)"
        return header + "\n\n" + "\n\n".join(parts)
