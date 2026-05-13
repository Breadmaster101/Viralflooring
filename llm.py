"""
llm.py — LLM API interaction: rate-limiting lock and the unified API call function.
"""

import asyncio
import time
import logging
import openai

from config import (
    LLM_BACKEND,
    API_BASE_URL, API_KEY, MODEL_ID,
    API_COOLDOWN_SECONDS, API_TIMEOUT_SECONDS,
    DISABLE_GEMINI_THINKING,
    OPENROUTER_API_KEY,
)


class APIRateLimiter:
    """Async lock with a cooldown period between API calls."""

    def __init__(self, cooldown):
        self.lock = asyncio.Lock()
        self.last_call_time = 0
        self.cooldown = cooldown


def create_openai_client(base_url, api_key, is_openrouter=False):
    """Create an AsyncOpenAI client, with optional OpenRouter headers."""
    kwargs = {
        "base_url": base_url,
        "api_key": api_key,
    }
    if is_openrouter:
        kwargs["default_headers"] = {
            "HTTP-Referer": "https://github.com/Viralflooring",
            "X-Title": "Viralflooring Discord Bot",
        }
    try:
        return openai.AsyncOpenAI(**kwargs)
    except Exception as e:
        logging.critical(f"Error configuring OpenAI client (base_url={base_url}): {e}")
        exit()


def create_clients():
    """Create the main OpenAI client based on resolved config."""
    main_client = create_openai_client(
        API_BASE_URL, API_KEY,
        is_openrouter=(LLM_BACKEND == "openrouter"),
    )
    return main_client


MAX_APP_RETRIES = 3        # Application-level retries (on top of the SDK's built-in retries)
APP_RETRY_BASE_DELAY = 2   # Seconds; doubles each attempt (2s, 4s, 8s)


async def call_llm_api(client, api_monitor, conversation_messages,
                       count_tokens_fn, label="Generation"):
    """
    Send a chat completion request to the configured LLM backend.

    Handles rate-limiting, Gemini thinking-disable, timeouts, and logging.
    Retries transient server errors (5xx) up to MAX_APP_RETRIES times with
    exponential backoff, on top of the openai SDK's own internal retries.
    """
    # Hold the lock only for cooldown enforcement, then release before the API call
    async with api_monitor.lock:
        elapsed = time.time() - api_monitor.last_call_time
        if elapsed < api_monitor.cooldown:
            await asyncio.sleep(api_monitor.cooldown - elapsed)
        api_monitor.last_call_time = time.time()

    model = MODEL_ID
    backend = LLM_BACKEND

    kwargs = {
        "model": model,
        "messages": conversation_messages,
        "max_tokens": 1000,
    }

    # Disable thinking for Gemini models when configured
    should_disable = DISABLE_GEMINI_THINKING
    is_gemini = backend == "gemini"

    if should_disable and is_gemini:
        model_lower = model.lower()
        if "gemma" in model_lower:
            kwargs["extra_body"] = {"reasoning_effort": "minimal"}
        elif "2.5" in model_lower:
            kwargs["extra_body"] = {"reasoning_effort": "low" if "pro" in model_lower else "none"}
        else:
            kwargs["extra_body"] = {"reasoning_effort": "low" if "pro" in model_lower else "minimal"}

    est_tokens = sum(count_tokens_fn(m.get('content', '')) for m in conversation_messages)
    logging.info(
        f"[{label}] Sending API request to {model}... "
        f"(Messages: {len(conversation_messages)}, Est. Tokens: {est_tokens})"
    )

    last_exception = None
    for attempt in range(1 + MAX_APP_RETRIES):
        try:
            completion = await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=API_TIMEOUT_SECONDS,
            )
            content = completion.choices[0].message.content
            return content.strip() if content else ""

        except asyncio.TimeoutError:
            logging.error(
                f"[{label}] API Request Timed Out! "
                f"Model: {model}, Backend: {backend}, Est. Tokens: {est_tokens}"
            )
            raise

        except openai.InternalServerError as e:
            last_exception = e
            if attempt < MAX_APP_RETRIES:
                delay = APP_RETRY_BASE_DELAY * (2 ** attempt)
                logging.warning(
                    f"[{label}] Server error (500) from {model}, "
                    f"app-level retry {attempt + 1}/{MAX_APP_RETRIES} in {delay}s..."
                )
                await asyncio.sleep(delay)
            else:
                logging.error(
                    f"[{label}] API Request Failed after {MAX_APP_RETRIES} app-level retries! "
                    f"Model: {model}, Backend: {backend}, Est. Tokens: {est_tokens}, "
                    f"Error: {type(e).__name__}: {e}",
                    exc_info=True,
                )
                raise

        except Exception as e:
            logging.error(
                f"[{label}] API Request Failed! "
                f"Model: {model}, Backend: {backend}, Est. Tokens: {est_tokens}, "
                f"Error: {type(e).__name__}: {e}",
                exc_info=True,
            )
            raise
