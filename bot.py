# Entry point. Flags: --troll-mode, --check, --reset-persona, --config PATH

from __future__ import annotations

import argparse
import logging
import sys

import discord

from config import Config, ConfigError, load_config
from utils import setup_logging

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Viralflooring Discord bot")
    parser.add_argument(
        "--troll-mode", action="store_true",
        help="Disable the model and send hand-typed messages from the console.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Validate config.toml and the required secrets, then exit.",
    )
    parser.add_argument(
        "--reset-persona", action="store_true",
        help=(
            "Discard everything the bot has written into its own system prompt, "
            "restoring the sections as authored. The discarded version is backed "
            "up to persona_backups/ first."
        ),
    )
    parser.add_argument(
        "--config", default=None, metavar="PATH",
        help="Path to config.toml (defaults to the one beside this script).",
    )
    return parser.parse_args()


def _read_prompt(cfg: Config, which: str):
    # Returns (path, text, problem); problem is empty when the file was read
    filename = getattr(cfg.persona, which)
    if not filename:
        return None, "", f"not configured: [persona] {which} is empty"

    path = cfg.path(filename)
    try:
        return path, path.read_text(encoding="utf-8"), ""
    except OSError as e:
        return path, "", f"could not be read ({e.strerror or e})"


def check_config(cfg: Config) -> int:
    # Reports the effective state, not just what config.toml says
    from persona import PERSONA_PLACEHOLDER, PersonaStore
    from tools import ALL_TOOLS, select_tools

    print(f"Config OK. Backend: {cfg.llm.backend}, model: {cfg.llm.model}")
    if cfg.llm.fallback_models:
        print(
            f"Falls back to: {' -> '.join(cfg.llm.fallback_models)} "
            f"(a rate-limited model is set aside for as long as the provider asks, "
            f"or {cfg.llm.fallback_cooldown_seconds:.0f}s if it does not say)"
        )

    whitelist = cfg.discord.channel_whitelist
    if whitelist:
        print(f"Channel whitelist: {len(whitelist)} server(s) limited, every other server unrestricted")
        for guild_id, channels in whitelist.items():
            listed = ", ".join(str(c) for c in sorted(channels)) or "none (silent there)"
            print(f"  server {guild_id}: {listed}")
    else:
        print("Channel whitelist: off (every channel in every server)")

    fixed_path, fixed_text, fixed_problem = _read_prompt(cfg, "fixed_file")
    persona_path, persona_text, persona_problem = _read_prompt(cfg, "persona_file")
    # Parsing only; constructing the store touches no files.
    editable = PersonaStore(
        cfg.persona, fixed_text, persona_text,
        directory=cfg.root / "persona", backup_directory=cfg.root / "persona_backups",
    ).editable if persona_text else {}
    self_editing = cfg.persona.self_edit_enabled and bool(editable)

    # select_tools decides what the model is offered; keep only its warnings
    logging.getLogger("tools").setLevel(logging.WARNING)
    offered = select_tools(
        cfg.tools.enabled, cfg.memory.enabled, persona_editable=self_editing,
        user_memory_enabled=cfg.memory.users.enabled,
    )
    withheld = sorted((cfg.tools.enabled & ALL_TOOLS.keys()) - offered.keys())

    print(f"Tools enabled: {', '.join(offered) or '(none)'}")
    if withheld:
        print(f"Tools withheld: {', '.join(withheld)} (the system each belongs to is off)")

    print(f"Memory: {f'on (scope: {cfg.memory.scope})' if cfg.memory.enabled else 'off'}")
    if cfg.memory.enabled and cfg.memory.users.enabled:
        print(
            f"Memory filed under a person: on (up to "
            f"{cfg.memory.users.max_facts_per_user} things about each of "
            f"{cfg.memory.users.max_users} people; only the people talking in a "
            f"channel are put in the prompt)"
        )
    else:
        print("Memory filed under a person: off")

    for label, path, problem in (
        ("Prompt (fixed)", fixed_path, fixed_problem),
        ("Prompt (persona)", persona_path, persona_problem),
    ):
        if not problem:
            print(f"{label}: {path.name}")
        else:
            print(f"{label}: {f'{path}: ' if path else ''}{problem}")

    # Prompt, tool schemas and history are all resent on every call
    from llm import estimate_tokens

    fixed_cost = estimate_tokens(
        [(fixed_text or "").replace(PERSONA_PLACEHOLDER, persona_text),
         [t.schema() for t in offered.values()]]
    )
    worst = fixed_cost + cfg.history.token_budget
    budget = cfg.llm.input_tokens_per_minute
    models = len(cfg.llm.models)
    per_minute = (
        f", so about {max(1, budget // worst)}-{max(1, budget // fixed_cost)} calls/min "
        f"within [llm] input_tokens_per_minute of {budget}"
        + (f", per model, across {models} of them" if models > 1 else "")
        if budget else " ([llm] input_tokens_per_minute is off)"
    )
    print(
        f"Input tokens: ~{fixed_cost} per call before history, ~{worst} with a full "
        f"history{per_minute}"
    )
    # Caching behaviour differs per backend, so the note is backend-specific
    cache_note = {
        "mistral": (
            "which that backend caches on a shared prefix, so it is charged "
            "once and read back after that"
        ),
        "gemini": (
            "which that backend may or may not cache. Implicit caching is "
            "best-effort above a documented 4096-token minimum, so a prefix "
            "over the line still is not guaranteed a hit; explicit caches "
            "cannot be created on the free tier at all, which reports a "
            "cached-content storage quota of 0; gemma models support neither "
            "and 404 on createCachedContent; and the OpenAI-compatible endpoint "
            "this bot calls reports no cache figures, so none of it is visible "
            "from here. Measured on this key: no hits at this prefix size"
        ),
    }.get(cfg.llm.backend, "which may or may not be cached, depending on the backend")
    print(
        f"  of which ~{fixed_cost} is a stable prefix (the system prompt, then "
        f"the tool schemas), byte-identical from call to call: {cache_note}. "
        f"The chat sits after it, the clock with it, and those change every call"
    )
    if "{date}" in f"{fixed_text or ''}{persona_text or ''}":
        print(
            f"  warning: {{date}} is in the prompt. A clock there moves every "
            f"second, so no two calls share a prefix and all ~{fixed_cost} of "
            f"the above are charged in full every time. The bot is given the "
            f"time on the last line of the chat whether or not {{date}} is used"
        )

    if not cfg.persona.self_edit_enabled:
        print("Persona self-editing: off")
    elif not editable:
        print(
            "Persona self-editing: off: the persona file has no editable sections. "
            "It needs at least one '## TITLE' whose body is a bullet list or a table"
        )
    else:
        print(f"Persona self-editing: on (scope: {cfg.persona.scope})")
    return _check_subvirals(cfg)


def _check_subvirals(cfg: Config) -> int:
    from llm import estimate_tokens
    from subviral import REPLY_TOOL, render_prompt

    sub = cfg.subvirals
    if not sub.enabled:
        print("Subvirals: off")
        return 0

    try:
        fixed = cfg.path(sub.fixed_file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"Subvirals: {cfg.path(sub.fixed_file)} could not be read ({e.strerror or e})")
        return 1
    chain = (
        "they only answer people" if sub.max_chain == 0
        else f"up to {sub.max_chain} extra round(s) of bots answering each other"
    )
    print(f"Subvirals: on, {len(sub.bots)} of them, {chain} (needs Manage Webhooks)")

    status = 0
    for bot in sub.bots:
        llm = (
            f"{bot.llm.backend} {bot.llm.model}" if bot.llm
            else f"Viral's backend and quota ({cfg.llm.backend} {cfg.llm.model})"
        )
        try:
            persona = cfg.path(bot.persona_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"  {bot.name}: {cfg.path(bot.persona_file)} could not be read "
                  f"({e.strerror or e})")
            status = 1
            continue
        cost = estimate_tokens([render_prompt(fixed, persona, bot.name), [REPLY_TOOL]])
        print(f"  {bot.name}: {llm}, ~{cost} input tokens per call before history")
    return status


def reset_persona(cfg: Config) -> int:
    # Works even with self-editing turned off
    from persona import PersonaStore

    for which in ("fixed_file", "persona_file"):
        path, _, problem = _read_prompt(cfg, which)
        if problem:
            print(f"Cannot reset: [persona] {which} ({path}) {problem}.", file=sys.stderr)
            return 2

    _, fixed_text, _ = _read_prompt(cfg, "fixed_file")
    persona_path, persona_text, _ = _read_prompt(cfg, "persona_file")

    store = PersonaStore(
        cfg.persona, fixed_text, persona_text,
        directory=cfg.root / "persona",
        backup_directory=cfg.root / "persona_backups",
    )
    removed = store.reset()
    if not removed:
        print("The bot has not changed its own system prompt; nothing to reset.")
        return 0

    for item in removed:
        print(f"Reset {item.name} (backed up to persona_backups/).")
    print(f"{persona_path.name} is now in force again.")
    return 0


def main() -> int:
    args = parse_args()

    try:
        from pathlib import Path
        from config import CONFIG_FILE
        cfg = load_config(Path(args.config) if args.config else CONFIG_FILE)
    except ConfigError as e:
        # Logging is not configured yet, so write straight to stderr.
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    setup_logging(cfg.logging, cfg.path(cfg.logging.file) if cfg.logging.file else None)

    if args.reset_persona:
        return reset_persona(cfg)

    if args.check:
        return check_config(cfg)

    from bot_client import ViralBot

    bot = ViralBot(cfg, troll_mode=args.troll_mode)
    try:
        # log_handler=None keeps discord.py from replacing the logging setup above.
        bot.run(cfg.discord.token, log_handler=None)
    except discord.LoginFailure:
        log.critical("Discord rejected the token. Check DISCORD_TOKEN in your .env.")
        return 1
    except discord.PrivilegedIntentsRequired:
        needed = "Message Content"
        if cfg.discord.member_intent:
            needed += " and Server Members ([discord] member_intent is true)"
        log.critical(
            "This bot is missing a privileged intent it asked for: %s. Turn it on at "
            "https://discord.com/developers/applications under Bot > Privileged "
            "Gateway Intents.", needed,
        )
        return 1
    except KeyboardInterrupt:
        log.info("Shutting down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
