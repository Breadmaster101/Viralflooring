# The Discord client. Each incoming message runs one "turn": the model sees the
# recent history and its tools, and decides whether to reply, react, or stay quiet.

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import discord

import tools as tool_module
from config import Config
from database import (
    add_message, add_reaction, get_context, get_reactions, init_db, remove_reaction,
    update_message,
)
from llm import LLMClient
from memory import MemoryStore
from persona import PERSONA_PLACEHOLDER, PersonaStore
from tools import TurnContext, select_tools
from user_memory import UserMemoryStore
from utils import (
    apply_easter_eggs, build_history, chat_participants, clock_line, safe_typing,
    split_message,
)

log = logging.getLogger(__name__)

# Used when no persona file is configured; deliberately characterless
DEFAULT_PERSONA = "You are a participant in a Discord chat."


@dataclass
class ChannelQueue:
    # Messages in one channel that no turn has answered yet
    pending: bool = False
    mentioned: bool = False  # any of them @mentioned the bot
    count: int = 0
    first_at: float = 0.0  # monotonic time the oldest unanswered one arrived
    last_at: float = 0.0   # and the newest
    worker: asyncio.Task | None = None


class ViralBot(discord.Client):
    def __init__(self, cfg: Config, troll_mode: bool = False):
        intents = discord.Intents.default()
        intents.message_content = True
        # Privileged: off by default, since asking without enabling it in the portal stops startup
        intents.members = cfg.discord.member_intent

        super().__init__(
            intents=intents,
            # Stop a model typing "@everyone" from pinging the server
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )

        self.cfg = cfg
        self.troll_mode = troll_mode
        self.db: Any = None
        self.llm = LLMClient(cfg.llm)
        self.memory = MemoryStore(cfg.memory, cfg.root / "memories") if cfg.memory.enabled else None
        # Per-person memory, only on when memory itself is
        self.user_memory = (
            UserMemoryStore(cfg.memory, cfg.root / "memories")
            if cfg.memory.enabled and cfg.memory.users.enabled else None
        )

        self.persona = self._load_persona()
        self.persona_store = self._build_persona_store()
        self.tools = select_tools(
            cfg.tools.enabled, cfg.memory.enabled,
            persona_editable=self.persona_store is not None,
            user_memory_enabled=cfg.memory.users.enabled,
        )
        self.tool_schemas = [tool.schema() for tool in self.tools.values()]

        self.tokenizer = self._load_tokenizer()

        self._queues: dict[int, ChannelQueue] = {}
        self._turns: set[asyncio.Task] = set()
        self.last_troll_channel: discord.abc.Messageable | None = None

        log.info(
            "Backend %s | model %s%s | tools: %s",
            cfg.llm.backend, cfg.llm.model,
            f" (falling back to {' then '.join(cfg.llm.fallback_models)})"
            if cfg.llm.fallback_models else "",
            ", ".join(self.tools) or "(none)",
        )

    # ------------------------------------------------------------------
    #  Startup
    # ------------------------------------------------------------------

    def _read(self, filename: str) -> str:
        if not filename:
            return ""
        path = self.cfg.path(filename)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            log.warning("%s could not be read.", path)
            return ""

    def _load_persona(self) -> str:
        # The prompt as authored; a missing fixed file falls back to a generic persona
        fixed = self._read(self.cfg.persona.fixed_file)
        persona = self._read(self.cfg.persona.persona_file)
        if not fixed:
            log.warning(
                "%s is missing or empty; running with a generic persona.",
                self.cfg.persona.fixed_file,
            )
            return DEFAULT_PERSONA
        # Matches PersonaStore.render() byte for byte
        if PERSONA_PLACEHOLDER in fixed:
            return fixed.replace(PERSONA_PLACEHOLDER, persona.rstrip()).rstrip()
        return f"{fixed.rstrip()}\n\n{persona}".rstrip()

    def _build_persona_store(self) -> PersonaStore | None:
        # None when self-editing is off or the persona file has no editable sections
        if not self.cfg.persona.self_edit_enabled:
            return None

        fixed = self._read(self.cfg.persona.fixed_file)
        persona = self._read(self.cfg.persona.persona_file)
        if not fixed:
            log.warning("Self-editing is off: %s could not be read.",
                        self.cfg.persona.fixed_file)
            return None

        store = PersonaStore(
            self.cfg.persona, fixed, persona,
            directory=self.cfg.root / "persona",
            backup_directory=self.cfg.root / "persona_backups",
        )
        if not store.editable:
            log.warning(
                "[persona] self_edit_enabled is true, but %s has no editable "
                "sections: it needs at least one '## TITLE' section whose body "
                "is a bullet list or a table. Self-editing is off.",
                self.cfg.persona.persona_file,
            )
            return None

        log.info("Persona self-editing on (scope: %s).", self.cfg.persona.scope)
        return store

    def _load_tokenizer(self):
        try:
            import tiktoken
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            # Not fatal: a rough estimate is enough for the history window
            log.warning("tiktoken unavailable; estimating token counts instead.", exc_info=True)
            return None

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        if self.tokenizer is None:
            return max(1, len(str(text)) // 4)
        return len(self.tokenizer.encode(str(text)))

    async def setup_hook(self) -> None:
        self.db = await init_db(self.cfg.path(self.cfg.history.db_file))
        if self.troll_mode:
            from troll import troll_input_worker
            self.loop.create_task(troll_input_worker(self))

    async def close(self) -> None:
        # Let in-flight turns unwind before closing the database
        pending = list(self._turns)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.db is not None:
            await self.db.close()
        await self.llm.aclose()
        await super().close()

    async def on_ready(self) -> None:
        mode = "troll mode" if self.troll_mode else "autonomous mode"
        log.info("Logged in as %s (%s).", self.user, mode)

    # ------------------------------------------------------------------
    #  Prompt assembly
    # ------------------------------------------------------------------

    def _bot_name(self, guild: discord.Guild | None) -> str:
        username = self.user.name
        if guild and guild.me and guild.me.display_name != username:
            return f"{username} (nickname in this server: {guild.me.display_name})"
        return username

    def _memory_scope(self, guild: discord.Guild | None) -> str:
        # States how far memory reaches, since the bot gets asked and would guess
        if self.memory is None:
            return "off: you remember nothing once a conversation scrolls out of view"
        if self.cfg.memory.scope != "per_server":
            return "shared: the same memories everywhere you are, servers and DMs alike"
        if guild is None:
            return (
                "kept per server, but direct messages share one memory between them; "
                "what you remember here is not what you remember in any server"
            )
        return (
            f"kept per server: these are your memories of {guild.name} only, and you "
            f"have separate ones elsewhere"
        )

    def _persona_scope(self, guild: discord.Guild | None) -> str:
        # Describe how far a change to its own system prompt would reach
        if self.persona_store is None:
            return "fixed: you cannot change it"
        if self.cfg.persona.scope != "per_server":
            return "yours to change, and a change applies everywhere you are"
        if guild is None:
            return (
                "yours to change; direct messages share one version, separate from "
                "the one each server has"
            )
        return (
            f"yours to change, but only here: this version is {guild.name}'s, and "
            f"other servers keep their own"
        )

    def _system_prompt(self, guild: discord.Guild | None,
                       present: list[str] | None = None,
                       model: str | None = None) -> str:
        guild_id = guild.id if guild else None
        # The model is only settled in llm.complete, which renders this again with the one it picked
        model = model or self.llm.current_model
        # Rebuilt every turn from the store so edits apply immediately
        base = (
            self.persona_store.render(guild_id)
            if self.persona_store is not None else self.persona
        )
        # Keep substitutions stable between turns so the prompt prefix stays cacheable.
        # {model} is constant within a model's cache (see llm.complete's system_for).
        prompt = (
            base
            .replace("{name}", self._bot_name(guild))
            # The model actually called, which may be a fallback
            .replace("{model}", model)
            .replace("{memory_scope}", self._memory_scope(guild))
            .replace("{persona_scope}", self._persona_scope(guild))
            # Breaks prompt caching; the shipped prompt gets the time from utils.clock_line
            .replace("{date}", time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()))
        )

        if self.memory:
            prompt += "\n\n" + self.memory.format_for_prompt(guild_id)
        # Only people talking here are written out in full
        if self.user_memory:
            prompt += "\n\n" + self.user_memory.format_for_prompt(
                present or [], guild_id
            )
        return prompt

    # ------------------------------------------------------------------
    #  Turn execution
    # ------------------------------------------------------------------

    async def run_turn(self, channel: discord.abc.Messageable,
                       show_typing: bool = False) -> None:
        # Show the model the conversation and let it decide what to do
        channel_name = getattr(channel, "name", "DM")
        guild = getattr(channel, "guild", None)

        rows = await get_context(
            self.db, channel.id,
            token_budget=self.cfg.history.token_budget,
            min_messages=self.cfg.history.min_messages,
            max_messages=self.cfg.history.max_messages,
        )
        reactions = await get_reactions(
            self.db, [row.get("message_id") for row in rows]
        )
        history, refs, oldest_ts = build_history(rows, reactions)
        if not history:
            log.warning("[#%s] No usable history (%d rows), skipping turn.", channel_name, len(rows))
            return

        # Every name in the chat for the tools, and one entry per person for the prompt
        participants = chat_participants(rows)
        present = list(dict.fromkeys(participants.values()))

        # The clock goes after the history to keep the prompt prefix cacheable
        payload: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(guild, present)},
            *history,
        ]
        payload[-1]["content"] += "\n" + clock_line()

        # From the rows, since the rendered history is one block for the whole channel
        last_user_message = next(
            (r["content"] for r in reversed(rows) if r["role"] == "user"), ""
        )

        ctx = TurnContext(
            bot=self,
            channel=channel,
            guild=guild,
            db=self.db,
            memory=self.memory,
            user_memory=self.user_memory,
            persona=self.persona_store,
            max_message_length=self.cfg.discord.max_message_length,
            max_replies=self.cfg.tools.max_replies_per_turn,
            max_members_listed=self.cfg.tools.max_members_listed,
            max_channels_listed=self.cfg.tools.max_channels_listed,
            allowed_channels=(
                self.cfg.discord.channel_whitelist.get(guild.id) if guild else None
            ),
            typing_min_seconds=self.cfg.discord.typing_min_seconds,
            typing_seconds_per_char=self.cfg.discord.typing_seconds_per_char,
            typing_max_seconds=self.cfg.discord.typing_max_seconds,
            message_refs=refs,
            chat_users=participants,
            oldest_visible_ts=oldest_ts,
            on_reply=self._record_reply,
        )

        # Rebuilt per attempt so the prompt names the model actually serving the call
        def system_for(model: str) -> str:
            return self._system_prompt(guild, present, model)

        try:
            if show_typing:
                async with safe_typing(channel):
                    await self._tool_loop(ctx, payload, last_user_message,
                                          channel_name, system_for)
            else:
                await self._tool_loop(ctx, payload, last_user_message,
                                      channel_name, system_for)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[#%s] Turn failed", channel_name)

    async def _tool_loop(self, ctx: TurnContext, payload: list[dict[str, Any]],
                         last_user_message: str, channel_name: str,
                         system_for: Callable[[str], str] | None = None) -> None:
        # Run model round trips until it stops asking for information
        for iteration in range(1, self.cfg.tools.max_iterations + 1):
            reply = await self.llm.complete(
                payload, self.tool_schemas, label=f"#{channel_name} {iteration}",
                system_for=system_for,
            )

            if not reply.tool_calls:
                if reply.content and not ctx.acted:
                    # Some models answer in prose instead of calling reply
                    log.warning("[#%s] Model replied without a tool call; sending anyway.", channel_name)
                    text = apply_easter_eggs(reply.content, last_user_message)
                    await tool_module.tool_reply(ctx, text)
                elif not ctx.acted:
                    log.info("[#%s] Model chose to stay quiet.", channel_name)
                return

            # Logged so repeated round trips can be explained
            log.info(
                "[#%s] Model called: %s", channel_name,
                ", ".join(call.name for call in reply.tool_calls),
            )
            payload.append(reply.as_assistant_message())
            wants_more = False

            for call in reply.tool_calls:
                tool = self.tools.get(call.name)
                if tool is None:
                    result = (
                        f"There is no tool called {call.name!r}. "
                        f"Available tools: {', '.join(self.tools)}."
                    )
                    log.warning("[#%s] Model called unknown tool %r", channel_name, call.name)
                else:
                    arguments = dict(call.arguments)
                    if tool.name in ("reply", "reply_to") and "message" in arguments:
                        arguments["message"] = apply_easter_eggs(
                            str(arguments["message"]), last_user_message
                        )
                    result = await tool_module.execute(tool, ctx, arguments)
                    wants_more = wants_more or tool.needs_followup

                payload.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": result,
                })

            # Only read tools justify another API call
            if not wants_more:
                return

        log.warning(
            "[#%s] Hit the %d-iteration limit ([tools] max_iterations)%s.",
            channel_name, self.cfg.tools.max_iterations,
            "" if ctx.acted else " without the model ever saying anything",
        )

    async def _record_reply(self, text: str, sent: discord.Message) -> None:
        await add_message(
            self.db, sent.channel.id, "assistant", text, self.count_tokens,
            message_id=sent.id, author_id=self.user.id,
        )

    # ------------------------------------------------------------------
    #  Message intake
    # ------------------------------------------------------------------

    def _queue_turn(self, channel: discord.abc.Messageable, mentioned: bool) -> None:
        # Note that a channel has something new and make sure a worker will
        # answer it. The message is already stored, so however many arrive
        # before the worker gets to them, one turn covers them all.
        queue = self._queues.setdefault(channel.id, ChannelQueue())
        now = time.monotonic()
        if not queue.pending:
            queue.first_at = now
        queue.pending = True
        queue.last_at = now
        queue.count += 1
        queue.mentioned = queue.mentioned or mentioned
        if queue.worker is None or queue.worker.done():
            queue.worker = asyncio.create_task(self._channel_worker(channel, queue))
            # Keep a reference so the task is not garbage collected
            self._turns.add(queue.worker)
            queue.worker.add_done_callback(self._turns.discard)

    async def _channel_worker(self, channel: discord.abc.Messageable,
                              queue: ChannelQueue) -> None:
        # Takes turns in one channel until nothing new has come in. Only one
        # runs per channel, so turns there never overlap.
        channel_name = getattr(channel, "name", "DM")
        while queue.pending:
            await self._wait_for_quiet(queue)
            await self._wait_for_quota(channel_name)
            # Cleared just before reading the chat: anything that arrives from
            # here on is not in this turn, so it sets pending again for the next
            if queue.count > 1:
                log.info("[#%s] Answering %d new messages in one turn.",
                         channel_name, queue.count)
            show_typing = queue.mentioned
            queue.pending, queue.mentioned, queue.count = False, False, 0
            await self.run_turn(channel, show_typing=show_typing)

    async def _wait_for_quiet(self, queue: ChannelQueue) -> None:
        # Until the chat has paused for batch_window_seconds, or the first
        # waiting message has been held for batch_max_wait_seconds
        window = self.cfg.discord.batch_window_seconds
        cap = self.cfg.discord.batch_max_wait_seconds
        while True:
            wait = min(queue.last_at + window, queue.first_at + cap) - time.monotonic()
            if wait <= 0:
                return
            await asyncio.sleep(wait)

    async def _wait_for_quota(self, channel_name: str) -> None:
        # Wait out the rate limit before reading the chat rather than after, so
        # whatever is said in the meantime makes it into this call
        delay = self.llm.ready_in(self.llm.last_estimate)
        if delay > 0:
            log.info(
                "[#%s] Waiting %.1fs for API quota before reading the chat.",
                channel_name, delay,
            )
            await asyncio.sleep(delay)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.author.id == self.user.id:
            return
        if self.cfg.discord.ignore_bots and message.author.bot:
            return
        if message.guild is None and not self.cfg.discord.allow_dms:
            return
        if not self._channel_allowed(
            message.guild.id if message.guild else None, message.channel.id, message.channel,
        ):
            return

        content = self._describe(message)
        author = self._author_name(message.author)
        channel_name = getattr(message.channel, "name", "DM")
        log.info("[#%s] %s: %s", channel_name, author, content)

        # Stored straight away, so a turn already waiting sees it
        await add_message(
            self.db, message.channel.id, "user", content, self.count_tokens,
            name=author, message_id=message.id, author_id=message.author.id,
        )
        if self.troll_mode:
            self.last_troll_channel = message.channel
            return

        self._queue_turn(message.channel, self.user.mentioned_in(message))

    # ------------------------------------------------------------------
    #  Edits and reactions
    # ------------------------------------------------------------------

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        # Updates the stored message but takes no turn; the raw event carries the full Message
        if self.db is None or self.user is None:
            return

        message = getattr(payload, "message", None)
        if message is None:
            return

        content = self._describe(message)
        if not await update_message(self.db, payload.message_id, content, self.count_tokens):
            return  # Not a message the bot has

        log.info("[#%s] Edited: %s", self._channel_name(payload.channel_id), content)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # Records the reaction but takes no turn. Raw event, since on_reaction_add
        # only fires for cached messages (none after a restart).
        if self.db is None or self.user is None:
            return
        if not self._channel_allowed(payload.guild_id, payload.channel_id):
            return

        reactor = payload.member
        if reactor is not None and reactor.bot and reactor.id != self.user.id:
            if self.cfg.discord.ignore_bots:
                return

        # Keep its own reactions so it knows what it has reacted to
        if payload.user_id == self.user.id:
            name = "you"
        elif reactor is not None:
            name = self._author_name(reactor)
        else:
            user = self.get_user(payload.user_id)
            name = self._author_name(user) if user else "someone"

        recorded = await add_reaction(
            self.db, payload.channel_id, payload.message_id, str(payload.emoji),
            name=name, author_id=payload.user_id,
        )
        if recorded:
            log.info(
                "[#%s] %s reacted %s", self._channel_name(payload.channel_id),
                name, payload.emoji,
            )

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if self.db is None:
            return
        await remove_reaction(self.db, payload.message_id, str(payload.emoji), payload.user_id)

    def _channel_allowed(self, guild_id: int | None, channel_id: int,
                         channel: Any = None) -> bool:
        # [discord] channel_whitelist; a thread counts as the channel it is in
        if channel is None:
            channel = self.get_channel(channel_id)
        if isinstance(channel, discord.Thread) and channel.parent_id:
            channel_id = channel.parent_id
        return self.cfg.discord.allows(guild_id, channel_id)

    def _channel_name(self, channel_id: int) -> str:
        channel = self.get_channel(channel_id)
        return getattr(channel, "name", "DM")

    @staticmethod
    def _author_name(author: discord.abc.User) -> str:
        display = author.display_name
        return f"{author.name} (nickname: {display})" if display != author.name else display

    def _describe(self, message: discord.Message) -> str:
        # Bot mentions are left in (as @display_name) so the prompt can treat them as aimed at it
        content = message.clean_content.strip()

        reference = message.reference.resolved if message.reference else None
        if isinstance(reference, discord.Message):
            quoted = reference.clean_content[:200]
            content = (
                f'[replying to {reference.author.display_name}: "{quoted}"]\n{content}'
            )

        if message.attachments:
            names = ", ".join(a.filename for a in message.attachments)
            content = f"{content}\n[attached: {names}]".strip()

        return content or "(empty message)"

    # ------------------------------------------------------------------
    #  Troll mode support
    # ------------------------------------------------------------------

    async def send_as_bot(self, channel: discord.abc.Messageable, text: str) -> None:
        # Send a hand-written message and record it as the bot's own
        sent = None
        async with safe_typing(channel):
            for chunk in split_message(text, self.cfg.discord.max_message_length):
                sent = await channel.send(chunk)
        if sent is not None:
            await self._record_reply(text, sent)
