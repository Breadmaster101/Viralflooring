# Talking to the model: an OpenAI-compatible client with rate limiting,
# reasoning-effort resolution, and backend-agnostic tool calls.
# Rate limits are handled here (not by the SDK) because Gemini reports the wait
# in the error body. SDK retries only apply to the last model left in the chain;
# before that, a failing model is skipped at once since the next one is free.

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import openai

from config import LLMConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCall:
    # One tool invocation, normalised across backends
    id: str
    name: str
    arguments: dict[str, Any]
    # Provider baggage (e.g. Gemini thought signatures) that must be sent back verbatim
    extra: dict[str, Any] | None = None


@dataclass
class ModelReply:
    # What came back from one model round trip
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_assistant_message(self) -> dict[str, Any]:
        # Rebuilt rather than echoed, since some gateways reject extra response keys
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                _tool_call_json(call) for call in self.tool_calls
            ]
        return message


def _tool_call_json(call: ToolCall) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
    }
    if call.extra:
        entry.update(call.extra)
    return entry


def estimate_tokens(payload: Any) -> int:
    # Rough count at four characters per token; only has to be close
    return max(1, len(json.dumps(payload, default=str)) // 4)


class RateLimiter:
    # Paces calls by interval and by input tokens per rolling minute.
    # Token windows are per model (that is how quotas count); the interval is shared.

    def __init__(self, cooldown: float, tokens_per_minute: int = 0):
        self.cooldown = max(0.0, cooldown)
        self.tokens_per_minute = max(0, tokens_per_minute)
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._spent: dict[str, deque[tuple[float, int]]] = {}

    def _window(self, model: str, now: float) -> tuple[deque[tuple[float, int]], int]:
        window = self._spent.setdefault(model, deque())
        while window and window[0][0] <= now - 60.0:
            window.popleft()
        return window, sum(tokens for _, tokens in window)

    def delay_for(self, model: str, tokens: int) -> float:
        # How long this model must wait for room, without waiting
        if not (self.tokens_per_minute and tokens):
            return 0.0
        now = time.monotonic()
        window, used = self._window(model, now)
        # Empty window: the call exceeds the whole allowance, so let the provider decide
        if not window or used + tokens <= self.tokens_per_minute:
            return 0.0
        return max(0.1, window[0][0] + 60.0 - now)

    async def wait(self, model: str = "", tokens: int = 0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                sleep_for = max(0.0, self.cooldown - (now - self._last_call))
                # Two limits can hold a call up; the log says which
                reason = f"the {self.cooldown:.1f}s gap between calls"
                if not sleep_for:
                    sleep_for = self.delay_for(model, tokens)
                    _, used = self._window(model, now)
                    reason = (
                        f"{used} of {self.tokens_per_minute} input tokens used "
                        f"this minute, and this call needs {tokens}"
                    )
                if not sleep_for:
                    if self.tokens_per_minute and tokens:
                        self._spent.setdefault(model, deque()).append((now, tokens))
                    self._last_call = now
                    return
            # Sleep outside the lock so other channels are not stalled
            log.info(
                "Holding off %.1fs on %s: %s.",
                sleep_for, model or "the model", reason,
            )
            await asyncio.sleep(sleep_for)


class ModelRotation:
    # Picks the model to call. A rate-limited model is set aside for as long as
    # the provider asked; the chain is walked from the top, so the preferred
    # model returns on its own. A model that keeps failing is set aside for
    # longer each time, so it is not retried on every message while it is down.

    MAX_BENCH_SECONDS = 900.0

    def __init__(self, models: Sequence[str], cooldown: float):
        self.models = list(dict.fromkeys(m for m in models if m))
        self.cooldown = max(1.0, cooldown)
        self._until: dict[str, float] = {}
        # Failures in a row per model, cleared by a success
        self._strikes: dict[str, int] = {}

    def usable(self) -> list[str]:
        # The chain in order, minus whatever is still counting down
        now = time.monotonic()
        for model, until in list(self._until.items()):
            if until <= now:
                del self._until[model]
                log.info("%s is off its rate limit; using it again.", model)
        return [model for model in self.models if model not in self._until]

    def preferred(self) -> str:
        # The model a call would go to right now
        return next(iter(self.usable()), self.models[0])

    def bench(self, model: str, seconds: float, label: str = "turn",
              reason: str = "rate limited") -> None:
        strikes = self._strikes.get(model, 0) + 1
        self._strikes[model] = strikes
        if strikes > 1:
            # Exponential backoff from the second failure in a row: the provider's
            # retryDelay only covers its per-minute window, and an overloaded
            # model (500/503) fails again as soon as it is back
            seconds = max(seconds, min(
                self.cooldown * 2 ** (strikes - 2), self.MAX_BENCH_SECONDS))
        # Floor of one second
        seconds = max(1.0, seconds)
        self._until[model] = time.monotonic() + seconds
        remaining = [m for m in self.models if m not in self._until]
        log.warning(
            "[%s] %s is %s; setting it aside for %.0fs and %s.",
            label, model, reason, seconds,
            f"switching to {remaining[0]}" if remaining
            else "every other model is set aside too",
        )

    def soonest(self) -> tuple[str, float]:
        # The model that frees up first, and how long that takes
        model = min(self._until, key=self._until.__getitem__)
        return model, max(0.0, self._until[model] - time.monotonic())

    def clear(self, model: str) -> None:
        self._until.pop(model, None)

    def succeeded(self, model: str) -> None:
        self._strikes.pop(model, None)


# Gemini puts the wait in the error body, not a Retry-After header
_RETRY_SECONDS = re.compile(r"retry in ([\d.]+)\s*s", re.IGNORECASE)
_RETRY_DELAY_FIELD = re.compile(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+(?:\.\d+)?)s?['\"]")


def retry_after(error: Exception, cap: float = 60.0) -> float | None:
    # How long the provider asked us to wait, if it said
    response = getattr(error, "response", None)
    header = getattr(response, "headers", {}) or {}
    raw = header.get("retry-after") or header.get("Retry-After")
    if raw:
        try:
            return min(float(raw), cap)
        except (TypeError, ValueError):
            pass

    text = str(getattr(error, "body", "") or "") + " " + str(error)
    for pattern in (_RETRY_DELAY_FIELD, _RETRY_SECONDS):
        found = pattern.search(text)
        if found:
            return min(float(found.group(1)), cap)
    return None


# ---------------------------------------------------------------------------
#  Reasoning effort
# ---------------------------------------------------------------------------

def _mistral_reasons(name: str) -> bool:
    # Only magistral-* and mistral-small-* accept reasoning_effort; the rest reject it
    return "magistral" in name or "mistral-small" in name


def resolve_reasoning_effort(cfg: LLMConfig, model: str | None = None) -> str | None:
    # None means send nothing. Resolved per model, since fallbacks differ.
    # Gemini 3.x cannot turn thinking off, so "none" is dropped there.
    setting = cfg.reasoning_effort
    name = (model or cfg.model).lower()

    # Non-reasoning Mistral models reject the parameter with a 400, even if set explicitly
    if cfg.backend == "mistral" and not _mistral_reasons(name):
        return None

    if setting != "auto":
        return None if setting == "none" and cfg.backend != "mistral" else setting

    if cfg.backend == "gemini":
        if "pro" in name:
            return "low"
        # Needed, not just a speed tweak: without it gemma-4-31b-it answers in prose instead of calling tools
        return "minimal"
    if cfg.backend == "mistral":
        return "none"
    return None


# ---------------------------------------------------------------------------
#  Tool call normalisation
# ---------------------------------------------------------------------------

_THINK_TAGS = re.compile(r"<(thought|think|reasoning)>.*?</\1>\s*", re.DOTALL)
_CODE_FENCE = re.compile(r"```(?:json|tool_call|tool_code|python)?\s*(.*?)\s*```", re.DOTALL)


def clean_text(content: Any) -> str:
    # Flatten content (string or typed chunks) to text, dropping reasoning traces
    if content is None:
        return ""
    if isinstance(content, str):
        return _THINK_TAGS.sub("", content).strip()
    if isinstance(content, list):
        parts = []
        for chunk in content:
            get = chunk.get if isinstance(chunk, dict) else lambda k, d=None: getattr(chunk, k, d)
            if get("type") == "text":
                parts.append(get("text") or "")
        return _THINK_TAGS.sub("", "".join(parts)).strip()
    return str(content).strip()


def _coerce_arguments(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _as_tool_call(obj: Any) -> ToolCall | None:
    # Recognise the several shapes a stray tool call can arrive in
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        func = obj["function"]
        name, raw_args = func.get("name"), func.get("arguments", {})
        call_id = obj.get("id")
    else:
        name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
        raw_args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
        call_id = obj.get("id")
    if not isinstance(name, str) or not name:
        return None
    args = _coerce_arguments(raw_args)
    if args is None:
        return None
    return ToolCall(id=call_id or f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=args)


def salvage_tool_calls(content: str) -> list[ToolCall]:
    # Some gateways put the tool call in the message body, sometimes fenced
    if not content or "{" not in content:
        return []

    candidates = [m.group(1) for m in _CODE_FENCE.finditer(content)]
    if not candidates:
        start = content.find("{")
        bracket = content.find("[")
        if bracket != -1 and (start == -1 or bracket < start):
            start = bracket
        if start != -1:
            candidates.append(content[start:])

    for candidate in candidates:
        parsed = _parse_first_json(candidate)
        if parsed is None:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("tool_calls"), list):
            parsed = parsed["tool_calls"]
        items = parsed if isinstance(parsed, list) else [parsed]
        calls = [c for c in (_as_tool_call(item) for item in items) if c]
        if calls:
            return calls
    return []


def _parse_first_json(text: str) -> Any | None:
    # Decode the first complete JSON value in `text`, ignoring any trailing prose
    text = text.strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text)
        return value
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
#  Thought signatures
# ---------------------------------------------------------------------------

# Gemini needs thought signatures on tool calls it is sent; Gemma rejects Gemini's.
# So the rule depends on the destination model: strip for Gemma, keep otherwise.

_TOOL_CALL_KEYS = ("id", "type", "function")


def keeps_thought_signatures(backend: str, model: str) -> bool:
    return backend != "gemini" or "gemma" not in model.lower()


def strip_tool_call_extras(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # `messages` with the provider baggage taken off every tool call
    return [
        {**message, "tool_calls": [
            {key: value for key, value in call.items() if key in _TOOL_CALL_KEYS}
            for call in message["tool_calls"]
        ]} if message.get("tool_calls") else message
        for message in messages
    ]


def has_tool_call_extras(messages: list[dict[str, Any]]) -> bool:
    return any(
        set(call) - set(_TOOL_CALL_KEYS)
        for message in messages
        for call in message.get("tool_calls") or ()
    )


def is_signature_complaint(error: Exception) -> bool:
    # Whether a 400 is the provider objecting to the tool calls themselves
    text = (str(getattr(error, "body", "") or "") + " " + str(error)).lower()
    return "thought_signature" in text or "invalid argument" in text


# ---------------------------------------------------------------------------
#  Client
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.limiter = RateLimiter(cfg.cooldown_seconds, cfg.input_tokens_per_minute)
        self.rotation = ModelRotation(cfg.models, cfg.fallback_cooldown_seconds)

        self.client = openai.AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
        )
        # For when another model could take the call instead: SDK retries all hit
        # the same model, costing up to a timeout each before the chain moves on
        self.no_retry_client = self.client.with_options(max_retries=0)
        # Size of the most recent request, used to ask about room for the next
        self.last_estimate = 0

    def ready_in(self, estimated: int) -> float:
        # Seconds until some model in the chain has room for a call this size,
        # so the caller can wait before building the request rather than after
        usable = self.rotation.usable()
        if not usable:
            return self.rotation.soonest()[1]
        return min(self.limiter.delay_for(m, estimated) for m in usable)

    async def aclose(self) -> None:
        await self.client.close()

    @property
    def current_model(self) -> str:
        # The model a call would go to right now, fallbacks included
        return self.rotation.preferred()

    @staticmethod
    def _retarget(model: str, request: dict[str, Any],
                  system_for: Callable[[str], str] | None) -> dict[str, Any]:
        # Copies rather than edits, since the request may go to the next model
        messages = request["messages"]
        if system_for is None or not messages or messages[0].get("role") != "system":
            return request
        rendered = system_for(model)
        if rendered == messages[0].get("content"):
            return request
        return dict(request, messages=[{**messages[0], "content": rendered}, *messages[1:]])

    async def _call(
        self, model: str, request: dict[str, Any], estimated: int, label: str,
        system_for: Callable[[str], str] | None = None, retry: bool = True,
    ):
        request = self._retarget(model, request, system_for)
        keep = keeps_thought_signatures(self.cfg.backend, model)
        try:
            return await self._post(model, request, estimated, label, keep, retry)
        except openai.BadRequestError as e:
            # The name-based rule can guess wrong; retry once with the other treatment
            if not (is_signature_complaint(e) and has_tool_call_extras(request["messages"])):
                raise
            log.warning(
                "[%s] %s rejected the tool calls %s their thought signatures; "
                "retrying %s them.",
                label, model, "with" if keep else "without",
                "without" if keep else "with",
            )
            return await self._post(model, request, estimated, label, not keep, retry)

    async def _post(
        self, model: str, request: dict[str, Any], estimated: int, label: str,
        keep_signatures: bool, retry: bool = True,
    ):
        kwargs = dict(request, model=model)
        if not keep_signatures:
            kwargs["messages"] = strip_tool_call_extras(request["messages"])
        effort = resolve_reasoning_effort(self.cfg, model)
        if effort is not None:
            # Not in the base OpenAI schema
            kwargs["extra_body"] = {"reasoning_effort": effort}

        await self.limiter.wait(model, estimated)
        log.info(
            "[%s] -> %s (%s): %d messages, %d tools, ~%d input tokens",
            label, model, self.cfg.backend,
            len(request["messages"]), len(request.get("tools") or []), estimated,
        )
        started = time.monotonic()
        client = self.client if retry else self.no_retry_client
        completion = await client.chat.completions.create(**kwargs)
        # Timed so slow turns can be attributed
        log.info("[%s] <- %s in %.1fs", label, model, time.monotonic() - started)
        return completion

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        label: str = "turn",
        system_for: Callable[[str], str] | None = None,
    ) -> ModelReply:
        # Runs one completion, walking the model chain: a 429, 5xx or stall benches
        # that model and the next is tried. system_for renders the system prompt (which names
        # the model) for whichever model is called, keeping each prompt cache-stable.
        request: dict[str, Any] = {
            "messages": messages,
            # max_tokens, not max_completion_tokens: not every backend accepts the newer one
            "max_tokens": self.cfg.max_output_tokens,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        estimated = estimate_tokens([messages, tools or []])
        self.last_estimate = estimated

        # Prefer a model with room now over the preferred one without it
        usable = self.rotation.usable()
        ready = [m for m in usable if not self.limiter.delay_for(m, estimated)]
        order = ready or sorted(usable, key=lambda m: self.limiter.delay_for(m, estimated))

        limited: openai.RateLimitError | None = None
        stalled: openai.APIError | None = None
        completion = None
        for i, model in enumerate(order):
            # Only the last model standing gets SDK retries
            last = i == len(order) - 1
            try:
                completion = await self._call(
                    model, request, estimated, label, system_for, retry=last)
                self.rotation.succeeded(model)
                break
            except openai.RateLimitError as e:
                # Bench the model for the time the provider asked for
                limited = e
                self.rotation.bench(model, retry_after(e) or self.rotation.cooldown, label)
            except openai.APIConnectionError as e:
                # A stall (timeouts included): bench for about as long as it cost
                stalled = e
                self.rotation.bench(
                    model, self.cfg.timeout_seconds, label,
                    "not responding" if isinstance(e, openai.APITimeoutError)
                    else "unreachable",
                )
            except openai.APIStatusError as e:
                # 500/503: overloaded on the provider's side, so move on
                if e.status_code < 500:
                    raise
                stalled = e
                self.rotation.bench(
                    model, self.cfg.timeout_seconds, label,
                    f"failing (HTTP {e.status_code})",
                )

        if completion is None and stalled is not None and limited is None:
            # All stalled and no quota to wait out, so retrying would just cost another timeout
            log.error(
                "[%s] No model answered; all %d stalled.", label, len(order),
            )
            raise stalled

        if completion is None:
            # Wait for the first model back, unless that is longer than the call timeout
            model, delay = self.rotation.soonest()
            if delay > self.cfg.timeout_seconds:
                log.error(
                    "[%s] Every model is set aside, and the first one back (%s) "
                    "is %.0fs away, longer than [llm] timeout_seconds.",
                    label, model, delay,
                )
                raise limited or RuntimeError(
                    f"every model is set aside; {model} frees up in {delay:.0f}s"
                )
            log.warning(
                "[%s] Every model is set aside; waiting %.1fs for %s.",
                label, delay, model,
            )
            await asyncio.sleep(delay)
            self.rotation.clear(model)
            completion = await self._call(model, request, estimated, label, system_for)
            self.rotation.succeeded(model)

        if not completion.choices:
            log.warning("[%s] Model returned no choices", label)
            return ModelReply()

        message = completion.choices[0].message
        content = clean_text(message.content)

        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            function = getattr(call, "function", None)
            if function is None:  # a non-function tool type we do not use
                continue
            args = _coerce_arguments(function.arguments)
            if args is None:
                log.warning(
                    "[%s] Tool call %s had unparseable arguments: %r",
                    label, function.name, function.arguments,
                )
                args = {}
            # Non-OpenAI fields travel with the call
            extra = {
                key: value
                for key, value in (getattr(call, "model_extra", None) or {}).items()
                if key not in ("id", "type", "function", "index")
            }
            calls.append(ToolCall(
                id=call.id, name=function.name, arguments=args, extra=extra or None,
            ))

        if not calls and content:
            salvaged = salvage_tool_calls(content)
            if salvaged:
                log.info("[%s] Recovered %d tool call(s) from message content", label, len(salvaged))
                calls, content = salvaged, ""

        return ModelReply(content=content, tool_calls=calls)
