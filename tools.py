# The actions the model can take, exposed as OpenAI-style function tools.
# Every tool returns a short string fed back to the model, including on failure.
# Action tools (reply, react, ...) end the turn without another round trip;
# read tools (needs_followup) return information and the turn continues.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import discord

from database import search_history as db_search_history
from memory import MemoryStore
from persona import Edit, PersonaStore
from user_memory import UserMemoryStore
from utils import safe_typing, split_display_name, split_message, strip_transcript_stamp

log = logging.getLogger(__name__)

# Need [memory] enabled
MEMORY_TOOLS = {"remember", "forget"}
# Also need [memory.users] enabled
USER_MEMORY_TOOLS = {"remember_about_user", "forget_about_user", "get_user_memories"}
# Need self-editing enabled
PERSONA_TOOLS = {"edit_system_prompt"}


# ---------------------------------------------------------------------------
#  Turn context
# ---------------------------------------------------------------------------

@dataclass
class TurnContext:
    # Everything a tool needs for the current message, plus running turn state

    bot: discord.Client
    channel: discord.abc.Messageable
    guild: discord.Guild | None
    db: Any
    memory: MemoryStore | None
    user_memory: UserMemoryStore | None
    persona: PersonaStore | None
    max_message_length: int
    max_replies: int
    max_members_listed: int = 50
    max_channels_listed: int = 40
    # [discord] channel_whitelist for this server; None means every channel
    allowed_channels: frozenset[int] | None = None
    # [discord] typing_* settings; max 0 means no typing pause before a reply
    typing_min_seconds: float = 0.0
    typing_seconds_per_char: float = 0.0
    typing_max_seconds: float = 0.0
    # Short handles ("1", "2", ...) shown in the prompt -> real Discord message IDs.
    message_refs: dict[str, int] = field(default_factory=dict)
    # Every name the people in this conversation go by, folded -> username.
    chat_users: dict[str, str] = field(default_factory=dict)
    # Oldest message visible in the prompt; search_history looks before it
    oldest_visible_ts: float | None = None
    # Called with (text, sent_message) after each successful reply.
    on_reply: Callable[[str, discord.Message], Awaitable[None]] | None = None

    replies_sent: int = 0
    acted: bool = False

    @property
    def channel_name(self) -> str:
        return getattr(self.channel, "name", "DM")

    @property
    def guild_id(self) -> int | None:
        return self.guild.id if self.guild else None

    def resolve(self, ref: str) -> discord.PartialMessage | None:
        # Prompt handle like "3" or "#3" -> PartialMessage (no extra API call)
        key = str(ref).strip().lstrip("#").strip()
        message_id = self.message_refs.get(key)
        if message_id is None:
            return None
        return self.channel.get_partial_message(message_id)

    def resolve_user(self, name: str) -> str:
        # Map a nickname to its username; names from outside the chat pass through
        username, _ = split_display_name(str(name or ""))
        cleaned = " ".join(username.split()).strip().lstrip("@").strip()
        return self.chat_users.get(cleaned.casefold(), cleaned)


# ---------------------------------------------------------------------------
#  Tool definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    # True when the model needs to see the result and keep going.
    needs_followup: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _params(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    # No "additionalProperties": Gemini does not accept it
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


# --- reply -----------------------------------------------------------------

def typing_delay(ctx: TurnContext, text: str) -> float:
    # Seconds of "typing..." before sending: longer replies take a little longer
    if ctx.typing_max_seconds <= 0:
        return 0.0
    return min(
        ctx.typing_max_seconds,
        ctx.typing_min_seconds + ctx.typing_seconds_per_char * len(text),
    )


async def _send(ctx: TurnContext, text: str,
                reference: discord.PartialMessage | None = None) -> str:
    if ctx.replies_sent >= ctx.max_replies:
        return (
            f"Not sent: you have already sent {ctx.replies_sent} message(s) this turn, "
            f"which is the limit. Say everything in one message next time."
        )

    text = text.strip()
    stripped = strip_transcript_stamp(text)
    if stripped != text:
        log.warning(
            "[#%s] Model prefixed its message with a timestamp; removing it: %r",
            ctx.channel_name, text[: len(text) - len(stripped)],
        )
        text = stripped.strip()
    if not text:
        return "Not sent: the message was empty."

    chunks = split_message(text, ctx.max_message_length)
    sent: discord.Message | None = None
    delay = typing_delay(ctx, text)
    # Typing shown for a moment first, so the reply does not land the instant
    # the API answers; sending inside the block ends the indicator cleanly
    async with safe_typing(ctx.channel) if delay > 0 else contextlib.nullcontext():
        if delay > 0:
            await asyncio.sleep(delay)
        for index, chunk in enumerate(chunks):
            # Only the first chunk carries the reply reference
            kwargs: dict[str, Any] = {}
            if reference is not None and index == 0:
                kwargs["reference"] = reference
                # Do not ping on a reply
                kwargs["mention_author"] = False
            sent = await ctx.channel.send(chunk, **kwargs)

    ctx.replies_sent += 1
    ctx.acted = True
    log.info("[#%s] Sent: %s", ctx.channel_name, text)

    if ctx.on_reply and sent is not None:
        await ctx.on_reply(text, sent)

    remaining = ctx.max_replies - ctx.replies_sent
    return f"Message sent. You may send {remaining} more message(s) this turn."


async def tool_reply(ctx: TurnContext, message: str = "") -> str:
    return await _send(ctx, message)


async def tool_reply_to(ctx: TurnContext, message_ref: str = "", message: str = "") -> str:
    target = ctx.resolve(message_ref)
    if target is None:
        return (
            f"No message with reference {message_ref!r} is in view. "
            f"Valid references are: {_known_refs(ctx)}."
        )
    try:
        return await _send(ctx, message, reference=target)
    except discord.NotFound:
        return "That message was deleted, so it could not be replied to. Use reply instead."


# --- react -----------------------------------------------------------------

# Custom emoji: <:name:id> and <a:name:id>
CUSTOM_EMOJI = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>$")
SHORTCODE = re.compile(r"^:[a-z0-9_+-]+:$", re.IGNORECASE)


async def tool_react(ctx: TurnContext, message_ref: str = "", emoji: str = "") -> str:
    target = ctx.resolve(message_ref)
    if target is None:
        return (
            f"No message with reference {message_ref!r} is in view. "
            f"Valid references are: {_known_refs(ctx)}."
        )

    emoji = emoji.strip()
    if not emoji:
        return "No reaction added: the emoji was empty."
    if SHORTCODE.match(emoji):
        return (
            f"{emoji} is a shortcode, not an emoji. Send the actual character "
            f"instead, for example 💀 rather than :skull:."
        )
    if emoji.isascii() and not CUSTOM_EMOJI.match(emoji):
        return (
            f"{emoji!r} is not an emoji. Send a single Unicode emoji character, "
            f"or a custom emoji in the form <:name:id>."
        )

    try:
        await target.add_reaction(emoji)
    except discord.NotFound:
        return "That message was deleted, so it could not be reacted to."
    except discord.Forbidden:
        return "You do not have permission to add reactions in this channel."
    except discord.HTTPException as e:
        # 10014: Unknown Emoji
        if e.code == 10014:
            return (
                f"Discord rejected {emoji!r} as an unknown emoji. Try a plain, "
                f"widely supported one such as 💀, 😭, or 🔥."
            )
        return f"The reaction failed: {e.text or e}"

    ctx.acted = True
    log.info("[#%s] Reacted %s to message %s", ctx.channel_name, emoji, message_ref)
    return f"Reacted with {emoji}."


def _known_refs(ctx: TurnContext) -> str:
    return ", ".join(f"#{k}" for k in ctx.message_refs) or "(none)"


# --- memory ----------------------------------------------------------------

async def tool_remember(ctx: TurnContext, fact: str = "") -> str:
    if ctx.memory is None:
        return "The memory system is turned off."
    _, outcome = ctx.memory.remember(fact, ctx.guild_id)
    log.info("[Memory] remember(%r) -> %s", fact, outcome)
    ctx.acted = True
    return outcome


async def tool_forget(ctx: TurnContext, fact: str = "") -> str:
    if ctx.memory is None:
        return "The memory system is turned off."
    _, outcome = ctx.memory.forget(fact, ctx.guild_id)
    log.info("[Memory] forget(%r) -> %s", fact, outcome)
    ctx.acted = True
    return outcome


# --- memory, filed under one person ----------------------------------------

async def tool_remember_about_user(ctx: TurnContext, username: str = "",
                                   fact: str = "") -> str:
    if ctx.user_memory is None:
        return "Memory filed under a person is turned off."
    who = ctx.resolve_user(username)
    _, outcome = ctx.user_memory.remember(who, fact, ctx.guild_id)
    log.info("[UserMemory] remember(%r, %r) -> %s", who, fact, outcome)
    ctx.acted = True
    return outcome


async def tool_forget_about_user(ctx: TurnContext, username: str = "",
                                 fact: str = "") -> str:
    if ctx.user_memory is None:
        return "Memory filed under a person is turned off."
    who = ctx.resolve_user(username)
    _, outcome = ctx.user_memory.forget(who, fact, ctx.guild_id)
    log.info("[UserMemory] forget(%r, %r) -> %s", who, fact, outcome)
    ctx.acted = True
    return outcome


async def tool_get_user_memories(ctx: TurnContext, username: str = "") -> str:
    if ctx.user_memory is None:
        return "Memory filed under a person is turned off."
    if not str(username or "").strip():
        return ctx.user_memory.format_roster(ctx.guild_id)
    return ctx.user_memory.format_user(ctx.resolve_user(username), ctx.guild_id)


# --- history search --------------------------------------------------------

async def tool_search_history(ctx: TurnContext, query: str = "", limit: int = 5) -> str:
    try:
        limit = max(1, min(int(limit), 15))
    except (TypeError, ValueError):
        limit = 5

    rows = await db_search_history(
        ctx.db, ctx.channel.id, query,
        limit=limit, before_timestamp=ctx.oldest_visible_ts,
    )
    if not rows:
        return f"No older messages in this channel mention {query!r}."

    lines = []
    for row in reversed(rows):  # oldest first reads more naturally
        # Plain date; the model reads this, Discord does not render it
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["timestamp"]))
        who = row["name"] or ("you" if row["role"] == "assistant" else "someone")
        lines.append(f"- [{when}] {who}: {row['content']}")
    return f"Older messages mentioning {query!r}:\n" + "\n".join(lines)


# --- presence --------------------------------------------------------------

async def tool_set_nickname(ctx: TurnContext, nickname: str = "") -> str:
    if ctx.guild is None:
        return "Nicknames only exist in servers, and this is a direct message."
    if ctx.guild.me is None:
        return "Your own member record is not loaded for this server; try again later."

    nickname = nickname.strip()
    if len(nickname) > 32:
        return f"That nickname is {len(nickname)} characters; Discord allows at most 32."

    try:
        await ctx.guild.me.edit(nick=nickname or None)
    except discord.Forbidden:
        return "You do not have the Change Nickname permission in this server."
    except discord.HTTPException as e:
        return f"Changing the nickname failed: {e.text or e}"

    ctx.acted = True
    log.info("[%s] Nickname changed to %r", ctx.guild.name, nickname)
    return f"Nickname changed to {nickname!r}." if nickname else "Nickname cleared."


async def tool_set_status(ctx: TurnContext, status: str = "") -> str:
    status = status.strip()
    if len(status) > 128:
        return f"That status is {len(status)} characters; Discord allows at most 128."

    try:
        activity = discord.CustomActivity(name=status) if status else None
        await ctx.bot.change_presence(activity=activity)
    except discord.HTTPException as e:
        return f"Changing the status failed: {e.text or e}"

    ctx.acted = True
    log.info("Status changed to %r", status)
    return f"Status set to {status!r}." if status else "Status cleared."


# --- where it is -----------------------------------------------------------

async def tool_get_server_info(ctx: TurnContext) -> str:
    # Describe the server and channel the bot is currently in
    channel = f"#{ctx.channel_name}" if ctx.guild else "a direct message"
    topic = getattr(ctx.channel, "topic", None)

    if ctx.guild is None:
        return (
            f"You are in {channel}, not a server, so there is no member or channel "
            f"list. Direct messages are one-to-one."
        )

    guild = ctx.guild
    lines = [f"Server: {guild.name}", f"You are talking in: #{ctx.channel_name}"]
    if topic:
        lines.append(f"This channel's topic: {topic}")

    # Only channels the bot can see
    visible = [
        c for c in guild.text_channels
        if guild.me is None or c.permissions_for(guild.me).view_channel
    ]
    label = "Channels"
    if ctx.allowed_channels is not None:
        # The rest are ignored entirely, so only list where it can take part
        visible = [c for c in visible if c.id in ctx.allowed_channels]
        label = "Channels you take part in (you cannot see or speak in the others)"
    if visible:
        names = ", ".join(f"#{c.name}" for c in visible[:ctx.max_channels_listed])
        more = len(visible) - ctx.max_channels_listed
        lines.append(
            f"{label} ({len(visible)}): {names}" + (f", and {more} more" if more > 0 else "")
        )

    total = guild.member_count or len(guild.members)
    # Without the members intent, guild.members is only a partial cache
    complete = getattr(ctx.bot, "intents", None) is not None and ctx.bot.intents.members
    people = [m for m in guild.members if not m.bot]
    bots = sum(1 for m in guild.members if m.bot)

    named = _member_names(people, ctx.max_members_listed)
    shown = min(len(people), ctx.max_members_listed)
    # Say how many names are missing so a partial list is not read as complete
    if complete:
        # Only the people over the display cap are left out
        unknown = len(people) - shown
    else:
        # Only the total is reliable
        unknown = max(0, total - shown)

    if not complete:
        lines.append(
            f"Members: {total} in total, and you cannot see who they are, only the "
            f"{shown} you have come across recently. You do not know the other "
            f"{unknown}" + (f". The ones you know: {named}." if people else ".")
        )
    elif unknown:
        lines.append(
            f"Members: {total} in total ({bots} of them bots). That is too many to "
            f"list, so here are {shown} of them, and you do not know the other "
            f"{unknown}: {named}."
        )
    else:
        lines.append(
            f"Members ({total}, {bots} of them bots): {named}. That is all of them."
        )

    return "\n".join(lines)


def _member_names(members: list[Any], limit: int) -> str:
    # Name members the way the chat does, so the model can match them up
    shown = []
    for member in members[:limit]:
        display = getattr(member, "display_name", member.name)
        shown.append(
            f"{member.name} (nickname: {display})" if display != member.name else member.name
        )
    return ", ".join(shown)


# --- self-editing the system prompt ----------------------------------------

def _coerce_edits(raw: Any) -> list[Edit]:
    # Accepts a bare object or a JSON string, which smaller models send
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [edit for edit in (Edit.from_dict(item) for item in raw) if edit is not None]


async def tool_edit_system_prompt(ctx: TurnContext, edits: Any = None) -> str:
    if ctx.persona is None:
        return "You cannot change your own system prompt."

    parsed = _coerce_edits(edits)
    if not parsed:
        return (
            "No edits were understood. Pass a list, each entry with an \"op\" of "
            "add, remove or replace, a \"target\" naming the line number(s), and "
            "for add and replace the new \"text\"."
        )

    _, outcome = ctx.persona.apply_edits(parsed, ctx.guild_id)
    ctx.acted = True
    return outcome


# ---------------------------------------------------------------------------
#  Registry
# ---------------------------------------------------------------------------

ALL_TOOLS: dict[str, Tool] = {
    tool.name: tool for tool in [
        Tool(
            name="reply",
            description=(
                "Send a message to the channel. This is how you speak. Calling no "
                "tools at all is how you stay quiet. Which of those to do is decided "
                "by your system prompt, not by this description."
            ),
            parameters=_params(
                {"message": {"type": "string", "description": "What you want to say, in character."}},
                ["message"],
            ),
            handler=tool_reply,
        ),
        Tool(
            name="reply_to",
            description=(
                "Send a message as a Discord reply attached to a specific earlier "
                "message. Use this only when the conversation has moved on and it "
                "would otherwise be unclear what you are answering. Prefer plain reply."
            ),
            parameters=_params(
                {
                    "message_ref": {
                        "type": "string",
                        "description": "The reference of the message to reply to, e.g. \"3\".",
                    },
                    "message": {"type": "string", "description": "What you want to say, in character."},
                },
                ["message_ref", "message"],
            ),
            handler=tool_reply_to,
        ),
        Tool(
            name="react",
            description=(
                "Add a single emoji reaction to a message. A reaction with no reply "
                "is a complete response on its own. How often you react, and what "
                "you react to, is your own style, see your system prompt."
            ),
            parameters=_params(
                {
                    "message_ref": {
                        "type": "string",
                        "description": "The reference of the message to react to, e.g. \"3\".",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "One Unicode emoji character, such as 💀. Not a :shortcode:.",
                    },
                },
                ["message_ref", "emoji"],
            ),
            handler=tool_react,
        ),
        Tool(
            name="remember",
            description=(
                "Store one long-term fact that is not about one particular person, "
                "something about a place, a group, a running joke, or the world. "
                "Anything about a specific person goes under them with "
                "remember_about_user instead. Be extremely selective: only things "
                "worth recalling weeks from now, never passing chat. Write a full, "
                "self-contained sentence."
            ),
            parameters=_params(
                {"fact": {"type": "string", "description": "The fact to remember, as one full sentence."}},
                ["fact"],
            ),
            handler=tool_remember,
        ),
        Tool(
            name="forget",
            description=(
                "Delete a stored fact, word for word as it appears in your memories. "
                "Use this when a fact is wrong, outdated, or duplicated."
            ),
            parameters=_params(
                {"fact": {"type": "string", "description": "The exact text of the memory to delete."}},
                ["fact"],
            ),
            handler=tool_forget,
        ),
        Tool(
            name="remember_about_user",
            description=(
                "Store one long-term fact about a specific person, filed under them "
                "rather than in your general memory, so what you know about somebody "
                "stays together and comes back to you when they are talking. Be "
                "extremely selective: only things worth recalling weeks from now, "
                "such as a running joke of theirs, a strong opinion, or a real-life "
                "detail they volunteered. Never store passing chat. Write a full, "
                "self-contained sentence."
            ),
            parameters=_params(
                {
                    "username": {
                        "type": "string",
                        "description": (
                            "Whose it is. Use their username, the name before the "
                            "colon in the chat, not their nickname, which changes."
                        ),
                    },
                    "fact": {
                        "type": "string",
                        "description": "What to remember about them, as one full sentence.",
                    },
                },
                ["username", "fact"],
            ),
            handler=tool_remember_about_user,
        ),
        Tool(
            name="forget_about_user",
            description=(
                "Delete one thing you remember about a person, word for word as it "
                "appears under them. Use this when it is wrong, outdated, or "
                "duplicated. Removing the last one takes them off your list of "
                "people entirely."
            ),
            parameters=_params(
                {
                    "username": {
                        "type": "string",
                        "description": "Whose memory to remove it from.",
                    },
                    "fact": {
                        "type": "string",
                        "description": "The exact text of the thing to delete.",
                    },
                },
                ["username", "fact"],
            ),
            handler=tool_forget_about_user,
        ),
        Tool(
            name="get_user_memories",
            description=(
                "Read everything you remember about one person, by username. Called "
                "with no username, it instead lists every person you have notes on "
                "and how many things you hold about each, which is how you find out "
                "who you know."
            ),
            parameters=_params(
                {
                    "username": {
                        "type": "string",
                        "description": (
                            "Whose memories to read. Leave it out to list everyone "
                            "you have notes on instead."
                        ),
                    },
                },
                [],
            ),
            handler=tool_get_user_memories,
            needs_followup=True,
        ),
        Tool(
            name="search_history",
            description=(
                "Search this channel's older messages for a word or phrase, further "
                "back than the conversation you can already see. Use it when someone "
                "refers to something from a while ago."
            ),
            parameters=_params(
                {
                    "query": {"type": "string", "description": "Word or phrase to search for."},
                    "limit": {
                        "type": "integer",
                        "description": "How many matches to return (1-15, default 5).",
                    },
                },
                ["query"],
            ),
            handler=tool_search_history,
            needs_followup=True,
        ),
        Tool(
            name="set_nickname",
            description=(
                "Change your own nickname in this server. Everyone in the server "
                "sees it, and it stays until you change it again."
            ),
            parameters=_params(
                {"nickname": {"type": "string", "description": "The new nickname, 32 characters or fewer."}},
                ["nickname"],
            ),
            handler=tool_set_nickname,
        ),
        Tool(
            name="set_status",
            description=(
                "Change the status text shown under your name. Unlike your nickname "
                "this is not per-server: it is visible in every server you are in."
            ),
            parameters=_params(
                {"status": {"type": "string", "description": "The new status text, or \"\" to clear it."}},
                ["status"],
            ),
            handler=tool_set_status,
        ),
        Tool(
            name="get_server_info",
            description=(
                "Look up where you are: the server's name, its channels, its members, "
                "and which channel you are talking in right now. In a direct message "
                "there is no server, and this says so."
            ),
            parameters=_params({}, []),
            handler=tool_get_server_info,
            needs_followup=True,
        ),
        Tool(
            name="edit_system_prompt",
            description=(
                "Change your own system prompt, the document that defines who you "
                "are. Lines are addressed by the number in front of them. Any number "
                "of lines can be added, removed, and rewritten in one call, anywhere "
                "in the part of the document that is yours; the machinery above that "
                "part carries no numbers and cannot be reached. Either every edit in "
                "the call is applied or none of them is. The changes are kept from "
                "now on."
            ),
            parameters=_params(
                {
                    "edits": {
                        "type": "array",
                        "description": "The changes to make, applied in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["add", "remove", "replace"],
                                    "description": "What to do to the target line.",
                                },
                                "target": {
                                    "type": "string",
                                    "description": (
                                        "Which lines, by the numbers in front of "
                                        "them: \"7\", or \"3, 5, 9\" for several at "
                                        "once. Naming several to replace rewrites "
                                        "them all into the one new line. For add, "
                                        "give the number of the position the new "
                                        "line should take, and whatever is there now "
                                        "moves down; a heading instead of a number "
                                        "puts it at the end of what is under that "
                                        "heading."
                                    ),
                                },
                                "text": {
                                    "type": "string",
                                    "description": (
                                        "For add and replace, the new line. Write it "
                                        "in full sentences with correct spelling, "
                                        "grammar and punctuation, however casually you "
                                        "talk in chat; this is a written document, "
                                        "not a message. Match the formatting of the "
                                        "lines around it: the same **Bold label:** "
                                        "opening where they use one, and the same "
                                        "\"this | that\" split where they are "
                                        "written in two parts."
                                    ),
                                },
                            },
                            "required": ["op", "target"],
                        },
                    },
                },
                ["edits"],
            ),
            handler=tool_edit_system_prompt,
            # Needs a round trip so the model sees whether the edit worked
            needs_followup=True,
        ),
    ]
}


def select_tools(enabled: frozenset[str], memory_enabled: bool,
                 persona_editable: bool = False,
                 user_memory_enabled: bool = False) -> dict[str, Tool]:
    # Resolve the configured tool names into a registry for this run
    unknown = enabled - ALL_TOOLS.keys()
    if unknown:
        log.warning(
            "Ignoring unknown tool name(s) in [tools] enabled: %s. Known tools: %s",
            ", ".join(sorted(unknown)), ", ".join(sorted(ALL_TOOLS)),
        )

    selected = {}
    for name in sorted(enabled & ALL_TOOLS.keys()):
        if name in MEMORY_TOOLS and not memory_enabled:
            log.info("Tool %r is disabled because [memory] enabled is false.", name)
            continue
        if name in USER_MEMORY_TOOLS and not (memory_enabled and user_memory_enabled):
            log.info(
                "Tool %r is disabled because [memory] enabled or [memory.users] "
                "enabled is false.", name,
            )
            continue
        if name in PERSONA_TOOLS and not persona_editable:
            log.info(
                "Tool %r is disabled because [persona] self_edit_enabled is false, "
                "or the system prompt has no editable parts.", name,
            )
            continue
        selected[name] = ALL_TOOLS[name]

    if "reply" not in selected:
        log.warning("The 'reply' tool is not enabled, so the bot will never speak.")
    return selected


async def execute(tool: Tool, ctx: TurnContext, arguments: dict[str, Any]) -> str:
    # Run one tool, converting any failure into a message the model can read
    try:
        return await tool.handler(ctx, **arguments)
    except TypeError as e:
        log.warning("Bad arguments for %s(%r): %s", tool.name, arguments, e)
        expected = ", ".join(tool.parameters["properties"]) or "no arguments"
        return f"Those arguments are not valid for {tool.name}. It takes: {expected}."
    except discord.Forbidden:
        return f"You do not have permission to do that ({tool.name})."
    except discord.HTTPException as e:
        log.warning("Discord rejected %s: %s", tool.name, e)
        return f"Discord rejected that: {e.text or e}"
    except Exception:
        log.exception("Tool %s raised", tool.name)
        return f"{tool.name} failed unexpectedly. Try something else or say nothing."
