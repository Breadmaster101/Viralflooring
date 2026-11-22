# Typed config from config.toml; secrets come from the environment or .env

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.toml"
ENV_FILE = ROOT / ".env"

REASONING_LEVELS = ("auto", "none", "minimal", "low", "medium", "high")
MEMORY_SCOPES = ("global", "per_server")
PERSONA_SCOPES = MEMORY_SCOPES
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    # Raised when config.toml is missing, malformed, or has a bad value
    pass


# ---------------------------------------------------------------------------
#  Backend registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Backend:
    name: str
    env_key: str
    base_url: str | None  # None => must be supplied in the backend's own table
    default_model: str
    # Models to try, in order, while the chosen one is rate limited. Only worth
    # setting where the provider counts quota per model rather than per project.
    fallbacks: tuple[str, ...] = ()


BACKENDS: dict[str, Backend] = {
    "local": Backend("local", "LOCAL_API_KEY", None, "default"),
    "gemini": Backend(
        "gemini", "GEMINI_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-3.1-flash-lite",
        fallbacks=(
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemma-4-26b-a4b-it",
        ),
    ),
    "mistral": Backend(
        "mistral", "MISTRAL_API_KEY",
        "https://api.mistral.ai/v1", "mistral-medium-3-5",
    ),
}


# ---------------------------------------------------------------------------
#  Config sections
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiscordConfig:
    token: str
    ignore_bots: bool = True
    allow_dms: bool = True
    max_message_length: int = 2000
    member_intent: bool = False
    # Fast chats: wait for this much quiet (capped at the max) so a burst of
    # messages is answered in one turn. 0 disables the wait.
    batch_window_seconds: float = 2.0
    batch_max_wait_seconds: float = 8.0
    # Server ID -> the only channel IDs the bot takes part in there. Servers
    # not listed are unrestricted.
    channel_whitelist: dict[int, frozenset[int]] = field(default_factory=dict)

    def allows(self, guild_id: int | None, channel_id: int) -> bool:
        # DMs are governed by allow_dms, not the whitelist
        if guild_id is None:
            return True
        allowed = self.channel_whitelist.get(guild_id)
        return allowed is None or channel_id in allowed


@dataclass(frozen=True)
class LLMConfig:
    backend: str
    model: str
    base_url: str
    api_key: str
    max_output_tokens: int = 1000
    timeout_seconds: float = 30.0
    cooldown_seconds: float = 1.0
    max_retries: int = 3
    reasoning_effort: str = "auto"
    input_tokens_per_minute: int = 0
    fallback_models: tuple[str, ...] = ()
    fallback_cooldown_seconds: float = 60.0

    @property
    def models(self) -> tuple[str, ...]:
        # The model to call, then the ones to try while it is rate limited
        return (self.model, *self.fallback_models)


@dataclass(frozen=True)
class PersonaConfig:
    # The half that never changes, and the half the bot writes. See persona.py.
    fixed_file: str = "prompt/fixed.txt"
    persona_file: str = "prompt/persona.txt"
    self_edit_enabled: bool = False
    scope: str = "global"
    max_lines_per_section: int = 20
    max_line_length: int = 600
    backup_count: int = 20


@dataclass(frozen=True)
class HistoryConfig:
    db_file: str = "history.db"
    token_budget: int = 6000
    min_messages: int = 12
    max_messages: int = 50


@dataclass(frozen=True)
class UserMemoryConfig:
    # Scope comes from [memory], not repeated here
    enabled: bool = False
    max_users: int = 100
    max_facts_per_user: int = 10


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = False
    scope: str = "global"
    max_memories: int = 10
    users: UserMemoryConfig = field(default_factory=UserMemoryConfig)


@dataclass(frozen=True)
class ToolsConfig:
    enabled: frozenset[str] = frozenset()
    max_iterations: int = 4
    max_replies_per_turn: int = 2
    max_members_listed: int = 50
    max_channels_listed: int = 40


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: str = "bot.log"
    max_bytes: int = 5_000_000
    backup_count: int = 2


@dataclass(frozen=True)
class Config:
    discord: DiscordConfig
    llm: LLMConfig
    persona: PersonaConfig
    history: HistoryConfig
    memory: MemoryConfig
    tools: ToolsConfig
    logging: LoggingConfig
    root: Path = ROOT

    def path(self, relative: str) -> Path:
        # Resolve a config-supplied path relative to the project directory
        p = Path(relative).expanduser()
        return p if p.is_absolute() else self.root / p


# ---------------------------------------------------------------------------
#  .env loading
# ---------------------------------------------------------------------------

def load_dotenv(path: Path = ENV_FILE) -> None:
    # Populate os.environ from a .env file, without overriding real env vars
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
#  Typed getters: every failure names the exact key that is wrong
# ---------------------------------------------------------------------------

def _table(data: dict[str, Any], name: str, label: str | None = None) -> dict[str, Any]:
    # One section of the file. `label` names it in errors when it is nested
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(
            f"[{label or name}] must be a section, got {type(value).__name__}"
        )
    return value


def _get(table: dict[str, Any], section: str, key: str, default: Any, kind: type,
         choices: tuple[str, ...] | None = None, positive: bool = True) -> Any:
    # Read one key with a type check and a precise error message
    where = f"[{section}] {key}"
    if key not in table:
        return default
    value = table[key]

    # bool is a subclass of int, so guard both directions explicitly.
    if kind is not bool and isinstance(value, bool):
        raise ConfigError(f"{where} must be {kind.__name__}, got a boolean")
    if kind is float and isinstance(value, int):
        value = float(value)
    if not isinstance(value, kind):
        raise ConfigError(f"{where} must be {kind.__name__}, got {value!r}")

    if choices is not None and value not in choices:
        raise ConfigError(f"{where} must be one of: {', '.join(choices)}, got {value!r}")
    if positive and kind in (int, float) and value <= 0:
        raise ConfigError(f"{where} must be greater than 0, got {value!r}")
    return value


def _discord_id(value: Any) -> int | None:
    # A Discord ID as written in TOML: an integer, or digits in a string
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _channel_whitelist(discord_t: dict[str, Any]) -> dict[int, frozenset[int]]:
    table = _table(discord_t, "channel_whitelist", "discord.channel_whitelist")
    whitelist: dict[int, frozenset[int]] = {}
    for key, channels in table.items():
        guild_id = _discord_id(key)
        if guild_id is None:
            raise ConfigError(
                f"[discord.channel_whitelist] keys must be server IDs, got {key!r}"
            )
        if not isinstance(channels, list):
            channels = [channels]
        ids = [_discord_id(c) for c in channels]
        if any(i is None for i in ids):
            raise ConfigError(
                f"[discord.channel_whitelist] {key} must be a list of channel IDs, "
                f"e.g. [987654321098765432], got {channels!r}"
            )
        whitelist[guild_id] = frozenset(ids)
    return whitelist


def _require_secret(env_key: str, what: str) -> str:
    value = os.environ.get(env_key, "").strip()
    if not value:
        raise ConfigError(
            f"{what} is missing. Set {env_key} in your environment or in .env "
            f"(copy .env.example to .env to get started)."
        )
    return value


# ---------------------------------------------------------------------------
#  Loader
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_FILE) -> Config:
    load_dotenv(ENV_FILE)

    if not path.is_file():
        raise ConfigError(f"No config file at {path}. See the README for setup.")
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} is not valid TOML: {e}") from e

    # --- discord ---
    discord_t = _table(data, "discord")
    discord_cfg = DiscordConfig(
        token=_require_secret("DISCORD_TOKEN", "The Discord bot token"),
        ignore_bots=_get(discord_t, "discord", "ignore_bots", True, bool),
        allow_dms=_get(discord_t, "discord", "allow_dms", True, bool),
        max_message_length=_get(discord_t, "discord", "max_message_length", 2000, int),
        member_intent=_get(discord_t, "discord", "member_intent", False, bool),
        batch_window_seconds=_get(
            discord_t, "discord", "batch_window_seconds", 2.0, float, positive=False),
        batch_max_wait_seconds=_get(
            discord_t, "discord", "batch_max_wait_seconds", 8.0, float, positive=False),
        channel_whitelist=_channel_whitelist(discord_t),
    )

    # --- llm ---
    llm_t = _table(data, "llm")
    backend_name = _get(llm_t, "llm", "backend", "gemini", str, tuple(BACKENDS))
    backend = BACKENDS[backend_name]
    backend_t = _table(llm_t, backend_name)
    section = f"llm.{backend_name}"

    base_url = _get(backend_t, section, "base_url", backend.base_url, str)
    if not base_url:
        raise ConfigError(f"[{section}] base_url is required for the '{backend_name}' backend.")

    model = _get(backend_t, section, "model", backend.default_model, str).strip()
    if not model:
        raise ConfigError(f"[{section}] model cannot be empty.")

    # Drop duplicates, and the primary, from the fallbacks
    raw_fallbacks = backend_t.get("fallback_models", list(backend.fallbacks))
    if not isinstance(raw_fallbacks, list) or not all(
        isinstance(m, str) and m.strip() for m in raw_fallbacks
    ):
        raise ConfigError(
            f"[{section}] fallback_models must be a list of model names, "
            'e.g. ["gemini-3.5-flash-lite", "gemma-4-26b-a4b-it"], use [] for none.'
        )
    fallbacks: list[str] = []
    for name in raw_fallbacks:
        name = name.strip()
        if name != model and name not in fallbacks:
            fallbacks.append(name)

    llm_cfg = LLMConfig(
        backend=backend_name,
        model=model,
        base_url=base_url,
        api_key=_require_secret(backend.env_key, f"The API key for the '{backend_name}' backend"),
        max_output_tokens=_get(llm_t, "llm", "max_output_tokens", 1000, int),
        timeout_seconds=_get(llm_t, "llm", "timeout_seconds", 30.0, float),
        cooldown_seconds=_get(llm_t, "llm", "cooldown_seconds", 1.0, float, positive=False),
        max_retries=_get(llm_t, "llm", "max_retries", 3, int, positive=False),
        reasoning_effort=_get(llm_t, "llm", "reasoning_effort", "auto", str, REASONING_LEVELS),
        input_tokens_per_minute=_get(
            llm_t, "llm", "input_tokens_per_minute", 0, int, positive=False),
        fallback_models=tuple(fallbacks),
        fallback_cooldown_seconds=_get(
            llm_t, "llm", "fallback_cooldown_seconds", 60.0, float),
    )

    # --- persona ---
    persona_t = _table(data, "persona")
    if "system_prompt_file" in persona_t:
        raise ConfigError(
            "[persona] system_prompt_file has been replaced by two keys, because the "
            "prompt is now two files: fixed_file (which the bot can never change) and "
            "persona_file (which it can). Set those instead; the defaults are "
            '"prompt/fixed.txt" and "prompt/persona.txt".'
        )
    persona_cfg = PersonaConfig(
        fixed_file=persona_t.get("fixed_file", "prompt/fixed.txt"),
        persona_file=persona_t.get("persona_file", "prompt/persona.txt"),
        self_edit_enabled=_get(persona_t, "persona", "self_edit_enabled", False, bool),
        scope=_get(persona_t, "persona", "scope", "global", str, PERSONA_SCOPES),
        max_lines_per_section=_get(persona_t, "persona", "max_lines_per_section", 20, int),
        max_line_length=_get(persona_t, "persona", "max_line_length", 600, int),
        backup_count=_get(persona_t, "persona", "backup_count", 20, int, positive=False),
    )
    if persona_cfg.self_edit_enabled and not persona_cfg.persona_file:
        raise ConfigError(
            "[persona] self_edit_enabled is true but persona_file is empty. "
            "The bot can only edit sections of a persona it actually has."
        )

    # --- history ---
    history_t = _table(data, "history")
    history_cfg = HistoryConfig(
        db_file=_get(history_t, "history", "db_file", "history.db", str),
        token_budget=_get(history_t, "history", "token_budget", 6000, int),
        min_messages=_get(history_t, "history", "min_messages", 12, int),
        max_messages=_get(history_t, "history", "max_messages", 50, int),
    )
    if history_cfg.min_messages > history_cfg.max_messages:
        raise ConfigError(
            "[history] min_messages cannot exceed max_messages "
            f"({history_cfg.min_messages} > {history_cfg.max_messages})."
        )

    # --- memory ---
    memory_t = _table(data, "memory")
    users_t = _table(memory_t, "users", "memory.users")
    memory_cfg = MemoryConfig(
        enabled=_get(memory_t, "memory", "enabled", False, bool),
        scope=_get(memory_t, "memory", "scope", "global", str, MEMORY_SCOPES),
        max_memories=_get(memory_t, "memory", "max_memories", 10, int),
        users=UserMemoryConfig(
            enabled=_get(users_t, "memory.users", "enabled", False, bool),
            max_users=_get(users_t, "memory.users", "max_users", 100, int),
            max_facts_per_user=_get(
                users_t, "memory.users", "max_facts_per_user", 10, int),
        ),
    )

    # --- tools ---
    tools_t = _table(data, "tools")
    enabled_tools = tools_t.get("enabled", [])
    if not isinstance(enabled_tools, list) or not all(isinstance(t, str) for t in enabled_tools):
        raise ConfigError('[tools] enabled must be a list of tool names, e.g. ["reply", "react"]')
    tools_cfg = ToolsConfig(
        enabled=frozenset(enabled_tools),
        max_iterations=_get(tools_t, "tools", "max_iterations", 4, int),
        max_replies_per_turn=_get(tools_t, "tools", "max_replies_per_turn", 2, int),
        max_members_listed=_get(tools_t, "tools", "max_members_listed", 50, int),
        max_channels_listed=_get(tools_t, "tools", "max_channels_listed", 40, int),
    )

    # --- logging ---
    log_t = _table(data, "logging")
    log_cfg = LoggingConfig(
        level=_get(log_t, "logging", "level", "INFO", str, LOG_LEVELS),
        file=log_t.get("file", "bot.log"),
        max_bytes=_get(log_t, "logging", "max_bytes", 5_000_000, int),
        backup_count=_get(log_t, "logging", "backup_count", 2, int, positive=False),
    )

    return Config(
        discord=discord_cfg,
        llm=llm_cfg,
        persona=persona_cfg,
        history=history_cfg,
        memory=memory_cfg,
        tools=tools_cfg,
        logging=log_cfg,
    )
