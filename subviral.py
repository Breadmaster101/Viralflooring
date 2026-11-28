# Subvirals: extra characters that speak through a channel webhook under their
# own name and avatar, so in the chat they look like separate people. They can
# only talk. Viral is never told they exist: their messages are stored like
# anyone else's, so it sees them as strangers, and each subviral sees Viral as
# just another person in the chat.

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from config import Config, SubviralConfig
from database import add_message, get_context, get_reactions
from llm import LLMClient
from utils import apply_easter_eggs, build_history, clock_line, split_message, strip_transcript_stamp

if TYPE_CHECKING:
    from bot_client import ViralBot

log = logging.getLogger(__name__)

# Where a subviral's persona_file is dropped into [subvirals] fixed_file
PERSONA_PLACEHOLDER = "{persona}"
# The webhook created (or reused) in each channel
WEBHOOK_NAME = "Viralflooring subvirals"
# After a channel refuses webhooks, leave it alone this long before trying again
WEBHOOK_RETRY_SECONDS = 600.0

NO_MENTIONS = discord.AllowedMentions.none()

REPLY_TOOL = {
    "type": "function",
    "function": {
        "name": "reply",
        "description": (
            "Send a message to the channel. This is the only way you speak. "
            "Calling no tools at all is how you stay quiet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What you want to say."},
            },
            "required": ["message"],
        },
    },
}


def render_prompt(fixed: str, persona: str, name: str) -> str:
    # The shared fixed prompt with this subviral's own file dropped in
    if PERSONA_PLACEHOLDER in fixed:
        prompt = fixed.replace(PERSONA_PLACEHOLDER, persona.rstrip())
    else:
        prompt = f"{fixed.rstrip()}\n\n{persona}"
    return prompt.replace("{name}", name).rstrip()


@dataclass
class Subviral:
    cfg: SubviralConfig
    system_prompt: str
    llm: LLMClient

    @property
    def name(self) -> str:
        return self.cfg.name


class Subvirals:
    def __init__(self, bot: ViralBot, cfg: Config):
        self.bot = bot
        self.cfg = cfg
        fixed = cfg.path(cfg.subvirals.fixed_file).read_text(encoding="utf-8")

        # One client per distinct backend setup, so subvirals on the same
        # backend share its quota. No llm of their own means Viral's client.
        self._clients: dict[Any, LLMClient] = {}
        self.members: list[Subviral] = []
        for sv in cfg.subvirals.bots:
            persona = cfg.path(sv.persona_file).read_text(encoding="utf-8")
            if sv.llm is None:
                client = bot.llm
            else:
                client = self._clients.get(sv.llm)
                if client is None:
                    client = self._clients[sv.llm] = LLMClient(sv.llm)
            self.members.append(Subviral(sv, render_prompt(fixed, persona, sv.name), client))

        # Keyed by the channel that owns the webhook (a thread's parent)
        self._webhooks: dict[int, discord.Webhook] = {}
        self.webhook_ids: set[int] = set()
        self._webhook_lock = asyncio.Lock()
        self._refused: dict[int, float] = {}

        log.info(
            "Subvirals: %s (max_chain %d)",
            ", ".join(
                f"{s.name} ({s.llm.cfg.backend} {s.llm.cfg.model}"
                f"{', shares Viral quota' if s.llm is bot.llm else ''})"
                for s in self.members
            ),
            cfg.subvirals.max_chain,
        )

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    # ------------------------------------------------------------------
    #  Webhooks
    # ------------------------------------------------------------------

    @staticmethod
    def _owner(channel: Any) -> Any:
        return channel.parent if isinstance(channel, discord.Thread) else channel

    def can_speak_in(self, channel: Any) -> bool:
        # Webhooks only exist in server channels, and need Manage Webhooks
        guild = getattr(channel, "guild", None)
        owner = self._owner(channel)
        if guild is None or owner is None or not hasattr(owner, "webhooks"):
            return False
        if self._refused.get(owner.id, 0.0) > time.monotonic():
            return False
        me = guild.me
        if me is None or not owner.permissions_for(me).manage_webhooks:
            self._refuse(owner, "the bot lacks the Manage Webhooks permission there")
            return False
        return True

    def _refuse(self, owner: Any, why: str) -> None:
        if self._refused.get(owner.id, 0.0) <= time.monotonic():
            log.warning(
                "[#%s] Subvirals cannot speak here: %s. Trying again in %.0f minutes.",
                getattr(owner, "name", "?"), why, WEBHOOK_RETRY_SECONDS / 60,
            )
        self._refused[owner.id] = time.monotonic() + WEBHOOK_RETRY_SECONDS

    async def _webhook_for(self, channel: Any) -> discord.Webhook:
        owner = self._owner(channel)
        async with self._webhook_lock:
            hook = self._webhooks.get(owner.id)
            if hook is None:
                # Reuse the one from a previous run rather than piling up new ones
                hook = next(
                    (h for h in await owner.webhooks()
                     if h.user and h.user.id == self.bot.user.id and h.name == WEBHOOK_NAME),
                    None,
                ) or await owner.create_webhook(name=WEBHOOK_NAME)
                self._webhooks[owner.id] = hook
                self.webhook_ids.add(hook.id)
            return hook

    async def _send(self, sv: Subviral, channel: Any, text: str) -> None:
        hook = await self._webhook_for(channel)
        extra: dict[str, Any] = {}
        if isinstance(channel, discord.Thread):
            extra["thread"] = channel
        if sv.cfg.avatar_url:
            extra["avatar_url"] = sv.cfg.avatar_url

        # A pause scaled to the length, like Viral's typing delay. A webhook has
        # no typing indicator of its own: one here would show Viral typing.
        d = self.cfg.discord
        if d.typing_max_seconds > 0:
            await asyncio.sleep(min(
                d.typing_max_seconds, d.typing_min_seconds + d.typing_seconds_per_char * len(text),
            ))

        sent = None
        for chunk in split_message(text, self.cfg.discord.max_message_length):
            sent = await hook.send(
                chunk, username=sv.name, allowed_mentions=NO_MENTIONS, wait=True, **extra,
            )
        # Stored now, not when Discord echoes it back, so the next speaker sees
        # it. As a "user" row, which is what makes Viral see a stranger.
        if sent is not None:
            await add_message(
                self.bot.db, channel.id, "user", text, self.bot.count_tokens,
                name=sv.name, message_id=sent.id, author_id=sent.author.id,
                speaker=sv.name,
            )

    # ------------------------------------------------------------------
    #  Turns
    # ------------------------------------------------------------------

    async def round(self, channel: Any) -> bool:
        # Every subviral gets one turn, in a random order, each seeing what the
        # ones before it said. Returns whether any of them spoke.
        if not self.can_speak_in(channel):
            return False
        spoke = False
        for sv in random.sample(self.members, len(self.members)):
            if await self.turn(sv, channel):
                spoke = True
            if not self.can_speak_in(channel):
                break
        return spoke

    def _as_seen_by(self, sv: Subviral, rows: list[dict[str, Any]],
                    reactions: dict[int, list[dict[str, Any]]], viral_name: str):
        # Rewrites the shared history from this subviral's side: its own
        # messages become "you:", and Viral's become a person's
        seen: list[dict[str, Any]] = []
        for row in rows:
            row = dict(row)
            if row.get("speaker") == sv.name:
                row["role"] = "assistant"
            elif row["role"] == "assistant":
                row["role"], row["name"] = "user", viral_name
            seen.append(row)
        # Viral's own reactions are stored as "you"
        renamed = {
            mid: [dict(r, name=viral_name) if r.get("name") == "you" else r for r in rs]
            for mid, rs in reactions.items()
        }
        return seen, renamed

    async def turn(self, sv: Subviral, channel: Any) -> bool:
        label = f"{sv.name} #{getattr(channel, 'name', '?')}"

        # Wait out the rate limit before reading the chat, as Viral does
        delay = sv.llm.ready_in(sv.llm.last_estimate)
        if delay > 0:
            log.info("[%s] Waiting %.1fs for API quota.", label, delay)
            await asyncio.sleep(delay)

        rows = await get_context(
            self.bot.db, channel.id,
            token_budget=self.cfg.history.token_budget,
            min_messages=self.cfg.history.min_messages,
            max_messages=self.cfg.history.max_messages,
        )
        # Nothing new since it last spoke
        if not rows or rows[-1].get("speaker") == sv.name:
            return False

        guild = channel.guild
        viral_name = self.bot._author_name(guild.me or self.bot.user)
        reactions = await get_reactions(self.bot.db, [r.get("message_id") for r in rows])
        seen, reactions = self._as_seen_by(sv, rows, reactions, viral_name)
        history, _, _ = build_history(seen, reactions)
        if not history:
            return False

        # The clock goes after the history to keep the prompt prefix cacheable
        payload: list[dict[str, Any]] = [
            {"role": "system", "content": sv.system_prompt}, *history,
        ]
        payload[-1]["content"] += "\n" + clock_line()
        last_message = next(
            (r["content"] for r in reversed(seen) if r["role"] == "user"), "",
        )

        try:
            reply = await sv.llm.complete(payload, [REPLY_TOOL], label=label)
            # One message per turn: the first reply call, or prose if the model
            # answered without calling the tool
            text = next(
                (str(call.arguments.get("message", ""))
                 for call in reply.tool_calls if call.name == "reply"),
                "" if reply.tool_calls else reply.content,
            )
            text = strip_transcript_stamp(text.strip()).strip()
            if not text:
                log.info("[%s] Chose to stay quiet.", label)
                return False
            text = apply_easter_eggs(text, last_message)
            await self._send(sv, channel, text)
            log.info("[%s] Sent: %s", label, text)
            return True
        except asyncio.CancelledError:
            raise
        except discord.Forbidden:
            self._refuse(self._owner(channel), "Discord refused the webhook (403)")
        except Exception:
            log.exception("[%s] Turn failed", label)
        return False
