"""
bot_client.py — The main Discord bot class.

Imports configuration, database, LLM, and utility modules to keep this file
focused on Discord event handling and bot logic.
"""

import discord
import asyncio
import logging
import time
import re
import json
import sys
import tiktoken

from config import (
    LLM_BACKEND,
    MODEL_ID,
    MAX_TOKEN_LIMIT, DB_FILE,
    API_COOLDOWN_SECONDS, API_TIMEOUT_SECONDS,
    MAX_MEMORIES, DISABLE_MEMORY_SYSTEM,
)
from memory import format_memories_for_prompt, apply_memory_updates
from database import init_db, add_message, get_context
from llm import APIRateLimiter, create_clients, call_llm_api
from utils import (
    safe_typing, split_message, get_sanitized_history,
    TrollConsoleHandler, input_buffer, input_lock,
)


class LlamaDiscordBot(discord.Client):
    def __init__(self, troll_mode=False):
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)

        self.api_monitor = APIRateLimiter(cooldown=API_COOLDOWN_SECONDS)
        self.db = None
        self.troll_mode = troll_mode
        self.last_troll_channel = None

        # autonomous settings
        self.AUTONOMOUS_THRESHOLD = 7  # score (1-10) to trigger speaking

        try:
            with open("systemprompt.txt", "r", encoding="utf-8") as f:
                self.bot_persona = f.read()
        except FileNotFoundError:
            logging.info("systemprompt.txt not found. Proceeding without a system prompt.")
            self.bot_persona = None

        # LLM clients
        self.openai_client = create_clients()

        # Tokenizer
        try:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            logging.critical(f"Failed to load tokenizer: {e}")
            exit()

        self.channel_locks = {}

        logging.info(f"LLM backend: {LLM_BACKEND.upper()} | Model: {MODEL_ID}")

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    async def setup_hook(self):
        self.db = await init_db(DB_FILE)
        if self.troll_mode:
            asyncio.create_task(self.troll_input_worker())

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    MEMORY_INSTRUCTIONS = (
        "\n\nMEMORY SYSTEM: You have a persistent long term memory Your current memories are shown below. "
        "To update your memory, you may OPTIONALLY end your message with a memory block on a new line:\n"
        "[MEMORY:{\"add\":[\"thing to remember\"],\"remove\":[\"thing to forget\"]}]\n"
        "- 'add': short bullet points to save (brief, lowercase, factual)\n"
        "- 'remove': existing memories to delete (must match exactly)\n"
        "You can use this memory system whenever you learn something new about a user "
        "(like their real name, preferences, opinions, running jokes, or anything correlated to their username), "
        "you can save it. It helps you act more human.\n"
        "Add context and mention specifics in your memories so that you know what they mean in the future and when to use them.\n"
        "Write your memories in full descriptive sentences to make it easier to contextualize when reading the memory in the future.\n"
        "Don't just randomly save current bits of conversation such as \"user is mad\", only save things that would be actually useful in the **future**, not now.\n"
        "Only save things that you would like to have in your long-term memory. Things that you would like to remember tomorrow or in weeks to come.\n"
        "The memory block is invisible to users — they won't see it.\n"
        f"Memory limit: {MAX_MEMORIES} items. If full, remove old ones to make room.\n"
    )

    def count_tokens(self, text):
        if not text:
            return 0
        return len(self.tokenizer.encode(str(text)))

    def _get_guild_id(self, channel):
        """Extract the guild ID from a channel, or None for DMs."""
        return getattr(getattr(channel, 'guild', None), 'id', None)

    def _process_memory_block(self, response_text, guild_id=None):
        """Extract [MEMORY:{...}] from the response, apply updates, return cleaned text."""
        if DISABLE_MEMORY_SYSTEM:
            return response_text
        memory_match = re.search(r'\[MEMORY:(\{.*?\})\]', response_text, re.DOTALL)
        if memory_match:
            try:
                mem_data = json.loads(memory_match.group(1))
                adds = mem_data.get('add', [])
                removes = mem_data.get('remove', [])
                if adds or removes:
                    updated = apply_memory_updates(adds, removes, guild_id)
                    logging.info(f"[Memory] Added: {adds}, Removed: {removes} | Total: {len(updated)}")
            except json.JSONDecodeError as e:
                logging.warning(f"[Memory] Failed to parse memory JSON: {e}")
            # Strip the memory block from the visible response
            response_text = response_text[:memory_match.start()].rstrip() + response_text[memory_match.end():].lstrip()
            response_text = response_text.strip()
        return response_text

    # ------------------------------------------------------------------
    #  Decision logic
    # ------------------------------------------------------------------

    def _get_channel_lock(self, channel_id):
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()
        return self.channel_locks[channel_id]

    async def locked_evaluate_and_respond(self, channel_id, channel, trigger_message=None,
                                           user_content=None, user_name=None):
        async with self._get_channel_lock(channel_id):
            if user_content is not None:
                await add_message(self.db, channel_id, 'user', user_content, self.count_tokens, name=user_name)
            await self.evaluate_and_respond(channel_id, channel, trigger_message=trigger_message)

    async def locked_trigger_generation(self, channel_id, target, explicit=False, trigger_message=None,
                                        user_content=None, user_name=None):
        async with self._get_channel_lock(channel_id):
            if user_content is not None:
                await add_message(self.db, channel_id, 'user', user_content, self.count_tokens, name=user_name)
            await self.trigger_generation(channel_id, target, explicit=explicit, trigger_message=trigger_message)

    async def evaluate_and_respond(self, channel_id, channel, trigger_message=None):
        """Use the triage LLM to decide whether to interject.

        When the score is high enough, the triage model also returns the
        response text directly, eliminating a second API call.
        """
        raw_history = await get_context(
            self.db, channel_id, MAX_TOKEN_LIMIT,
            self.bot_persona, self.count_tokens,
        )
        sanitized_history = get_sanitized_history(raw_history)
        if not sanitized_history:
            return

        # Build persona context once (shared between triage system prompt and inline persona)
        persona_formatted = None
        if self.bot_persona:
            persona_formatted = self.bot_persona.replace(
                '{date}', time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime())
            )

        # social intelligence triage prompt — now also asks for a response
        triage_system = (
            "You are the social reasoning engine for an AI bot. Your goal is to decide if the bot should speak. "
            "Examine the chat history. Provide a 'score' from 1 to 10 on how appropriate it is for you to reply. "
            "Considerations:\n"
            "- If users are asking you questions or talking about topics you like. Also if you are participating in a conversation or arguing with someone: Score High (8-10).\n"
            "- If there is a joke you can complete or a factual error you can fix. Also when something baffles you so much you want to react: Score Medium (6-8).\n"
            "- If the conversation is moving very fast between humans and you aren't needed: Score Low (1-4).\n"
            "- If users have recently told you to be quiet or 'shut up', or are ignoring you: Score Very Low (1).\n\n"
            f"THRESHOLD: If your score is >= {self.AUTONOMOUS_THRESHOLD}, you MUST also include a 'response' field "
            "with the message you would send as the bot. Write the response fully in-character using your persona. "
            "If your score is below the threshold, omit the 'response' field.\n\n"
            "You may also optionally include a 'react' field with a SINGLE unicode emoji to react to the last "
            "message with (e.g. \"💀\", \"😂\", \"🔥\"). Only react if it feels natural, not every time. "
        )
        
        if not DISABLE_MEMORY_SYSTEM:
            triage_system += (
                "MEMORY SYSTEM: You have a persistent memory. You MUST use this actively! Whenever you learn "
                "something new about a user (their real name, preferences, opinions, running jokes, etc), "
                "include an optional 'memory' field with {\"add\":[\"things to remember\"],\"remove\":[\"things to forget\"]}. "
                "Keep memory items short, lowercase, and factual. Save anything that feels like a persistent detail. "
                f"Memory limit: {MAX_MEMORIES} items. If full, remove old ones to make room. "
                "You can use memory even if your score is low (to remember something without responding).\n\n"
                "Output ONLY valid JSON: {\"score\": <int>, \"reasoning\": \"<string>\", \"response\": \"<string or omit>\", "
                "\"react\": \"<emoji or omit>\", \"memory\": {\"add\": [...], \"remove\": [...]} or omit}"
            )
        else:
            triage_system += (
                "Output ONLY valid JSON: {\"score\": <int>, \"reasoning\": \"<string>\", \"response\": \"<string or omit>\", "
                "\"react\": \"<emoji or omit>\"}"
            )

        if persona_formatted:
            triage_system += f"\nYour Persona Context:\n{persona_formatted}"

        # Inject current memories into context
        guild_id = self._get_guild_id(channel)
        if not DISABLE_MEMORY_SYSTEM:
            triage_system += "\n\n" + format_memories_for_prompt(guild_id)

        payload = [{'role': 'system', 'content': triage_system}]
        payload.extend(sanitized_history)

        try:
            triage_response = await call_llm_api(
                self.openai_client, self.api_monitor,
                payload, self.count_tokens, label="Triage",
            )
            triage_response = re.sub(r'<(thought|think)>.*?</\1>\s*', '', triage_response, flags=re.DOTALL)
            json_match = re.search(r'\{.*\}', triage_response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                score = int(data.get('score', 0))
                reasoning = data.get('reasoning', '')
                inline_response = data.get('response', '')
                react_emoji = data.get('react', '')

                logging.info(f"[Triage - #{channel.name}] Score: {score}/{self.AUTONOMOUS_THRESHOLD} | Reason: {reasoning}")

                # Process memory updates from triage JSON
                if not DISABLE_MEMORY_SYSTEM:
                    memory_data = data.get('memory', {})
                    if memory_data:
                        mem_adds = memory_data.get('add', [])
                        mem_removes = memory_data.get('remove', [])
                        if mem_adds or mem_removes:
                            updated = apply_memory_updates(mem_adds, mem_removes, guild_id)
                            logging.info(f"[Memory] Added: {mem_adds}, Removed: {mem_removes} | Total: {len(updated)}")

                # Apply emoji reaction if provided
                if react_emoji and trigger_message:
                    try:
                        await trigger_message.add_reaction(react_emoji.strip())
                        logging.info(f"[Triage - #{channel.name}] Reacted with: {react_emoji}")
                    except Exception as react_err:
                        logging.warning(f"Failed to add reaction '{react_emoji}': {react_err}")

                if score >= self.AUTONOMOUS_THRESHOLD:
                    if inline_response and inline_response.strip():
                        # Use the response from triage directly — no second API call needed
                        logging.info(f"[Triage - #{channel.name}] Using inline response (saved 1 API call)")
                        await self._send_triage_response(channel_id, channel, inline_response.strip(), reasoning)
                    else:
                        # Fallback: triage didn't include a response, do a separate generation call
                        await self.trigger_generation(channel_id, channel, explicit=False, reasoning=reasoning)
        except asyncio.TimeoutError:
            logging.error(f"Triage evaluation failed: API request timed out after {API_TIMEOUT_SECONDS} seconds.")
        except Exception as e:
            logging.error(f"Triage evaluation failed: {e}", exc_info=True)

    async def _send_triage_response(self, channel_id, channel, response_text, reasoning):
        """Send a response that was generated inline by the triage model."""
        async with safe_typing(channel):
            try:
                response_text = response_text.replace('\u200b', '')
                response_text = re.sub(r'<(thought|think)>.*?</\1>\s*', '', response_text, flags=re.DOTALL)

                channel_name = getattr(channel, 'name', 'DM')
                logging.info(f"[LLM Response (triage-inline) - #{channel_name}] {response_text}")

                await add_message(self.db, channel_id, 'assistant', response_text, self.count_tokens)

                for chunk in split_message(response_text):
                    await channel.send(chunk)
                    await asyncio.sleep(0.5)

            except Exception as e:
                logging.error(f"Triage inline send error: {ascii(str(e))}", exc_info=True)
                try:
                    await channel.send(f"An error occurred: {str(e)}")
                except Exception:
                    pass

    async def trigger_generation(self, channel_id, target, explicit=False, reasoning=None, trigger_message=None):
        """Generate and send an LLM response."""
        channel = target.channel if explicit else target
        async with safe_typing(channel):
            try:
                raw_history = await get_context(
                    self.db, channel_id, MAX_TOKEN_LIMIT,
                    self.bot_persona, self.count_tokens,
                )
                sanitized_history = get_sanitized_history(raw_history)

                if self.bot_persona:
                    system_content = self.bot_persona.replace(
                        '{date}', time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime())
                    )
                else:
                    system_content = "You are a helpful AI assistant."

                if not explicit and reasoning:
                    system_content += f"\n\nInternal Note: You decided to interject because: '{reasoning}'."

                # Allow the LLM to react with an emoji
                system_content += (
                    "\n\nYou may optionally react to the last message with an emoji. "
                    "If you want to react, start your response with exactly [REACT:emoji] "
                    "(e.g. [REACT:💀]) on the first line, then your message on the next line. "
                    "Only react if it feels natural. Most of the time, just respond normally."
                )

                # Inject memory system
                guild_id = self._get_guild_id(channel)
                if not DISABLE_MEMORY_SYSTEM:
                    system_content += self.MEMORY_INSTRUCTIONS
                    system_content += "\n" + format_memories_for_prompt(guild_id)

                payload = [{'role': 'system', 'content': system_content}]
                payload.extend(sanitized_history)

                response_text = await call_llm_api(
                    self.openai_client, self.api_monitor,
                    payload, self.count_tokens,
                )
                response_text = response_text.replace('\u200b', '')

                # strip out thought blocks
                response_text = re.sub(r'<(thought|think)>.*?</\1>\s*', '', response_text, flags=re.DOTALL)

                # Process memory updates before sending
                response_text = self._process_memory_block(response_text, guild_id)

                # extract and apply emoji reaction if present
                react_match = re.match(r'^\[REACT:(.+?)\]\s*\n?', response_text)
                if react_match and (trigger_message or (explicit and target)):
                    react_emoji = react_match.group(1).strip()
                    react_target = trigger_message if trigger_message else target
                    try:
                        await react_target.add_reaction(react_emoji)
                        logging.info(f"[Generation] Reacted with: {react_emoji}")
                    except Exception as react_err:
                        logging.warning(f"Failed to add reaction '{react_emoji}': {react_err}")
                    response_text = response_text[react_match.end():].strip()

                # easter egg check
                user_message_lower = target.clean_content.lower() if explicit else (
                    sanitized_history[-1]['content'].lower() if sanitized_history else ""
                )
                if any(k in user_message_lower for k in ['woke', 'robot', 'liberal']):
                    response_text = response_text.rstrip() + " I hit the jack pot! $                  1"

                channel_name = getattr(channel, 'name', 'DM')
                logging.info(f"[LLM Response - #{channel_name}] {response_text}")

                await add_message(self.db, channel_id, 'assistant', response_text, self.count_tokens)

                for chunk in split_message(response_text):
                    if explicit:
                        await target.reply(chunk)
                    else:
                        await channel.send(chunk)
                    await asyncio.sleep(0.5)

            except asyncio.TimeoutError:
                logging.error(f"Generation Error: API request timed out after {API_TIMEOUT_SECONDS} seconds.")
                error_msg = f"Error: The LLM took more than {API_TIMEOUT_SECONDS} seconds to respond (timed out)."
                try:
                    if explicit:
                        await target.reply(error_msg)
                    else:
                        await channel.send(error_msg)
                except Exception:
                    pass
            except Exception as e:
                logging.error(f"Generation Error: {ascii(str(e))}", exc_info=True)
                error_msg = f"An error occurred: {str(e)}"
                try:
                    if explicit:
                        await target.reply(error_msg)
                    else:
                        await channel.send(error_msg)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    #  Troll mode
    # ------------------------------------------------------------------

    async def _manage_typing(self):
        if not self.last_troll_channel:
            return
        async with safe_typing(self.last_troll_channel):
            while time.time() - getattr(self, 'last_keystroke_time', 0) < 5:
                await asyncio.sleep(1)

    async def troll_input_worker(self):
        import utils  # access mutable module-level input_buffer

        loop = asyncio.get_running_loop()
        print("\n--- Troll mode active. Type your response at any time and press Enter to send it. ---")

        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                root_logger.removeHandler(handler)

        troll_handler = TrollConsoleHandler()
        troll_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        root_logger.addHandler(troll_handler)

        def read_input_sync():
            import msvcrt

            with utils.input_lock:
                sys.stdout.write("> " + utils.input_buffer)
                sys.stdout.flush()

            while True:
                char = msvcrt.getwch()

                if char in ('\x00', '\xe0'):
                    msvcrt.getwch()
                    continue

                if char == '\r' or char == '\n':
                    with utils.input_lock:
                        final_input = utils.input_buffer
                        utils.input_buffer = ""
                        sys.stdout.write('\n')
                        sys.stdout.flush()
                    return final_input
                elif char == '\b':
                    with utils.input_lock:
                        if len(utils.input_buffer) > 0:
                            utils.input_buffer = utils.input_buffer[:-1]
                            sys.stdout.write('\b \b')
                            sys.stdout.flush()
                elif char == '\x03':  # Ctrl+C
                    raise KeyboardInterrupt
                else:
                    with utils.input_lock:
                        utils.input_buffer += char
                        sys.stdout.write(char)
                        sys.stdout.flush()

                    loop.call_soon_threadsafe(trigger_typing)

        def trigger_typing():
            if self.last_troll_channel:
                self.last_keystroke_time = time.time()
                if not hasattr(self, 'typing_task') or self.typing_task is None or self.typing_task.done():
                    self.typing_task = loop.create_task(self._manage_typing())

        while True:
            try:
                response_text = await loop.run_in_executor(None, read_input_sync)
                response_text = response_text.strip()
                if response_text:
                    if self.last_troll_channel:
                        async with safe_typing(self.last_troll_channel):
                            await add_message(
                                self.db, self.last_troll_channel.id,
                                'assistant', response_text, self.count_tokens,
                            )
                            for chunk in split_message(response_text):
                                await self.last_troll_channel.send(chunk)
                                await asyncio.sleep(0.5)
                    else:
                        logging.warning("No active channel to send message to.")
            except (EOFError, KeyboardInterrupt):
                break
            except Exception as e:
                logging.error(f"Input worker error: {e}")

    # ------------------------------------------------------------------
    #  Event handlers
    # ------------------------------------------------------------------

    async def on_ready(self):
        logging.info(f'Logged in as {self.user}. Autonomous mode active (LLM-determined interjections).')

    async def on_message(self, message):
        if message.author == self.user:
            return

        channel_id = message.channel.id
        mentioned = self.user.mentioned_in(message)

        user_content_clean = message.clean_content
        if mentioned:
            user_content_clean = user_content_clean.replace(f"@{self.user.name}", "").strip()

        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            user_content_clean = f'[replying to {ref.author.display_name}: "{ref.clean_content[:200]}"]\n{user_content_clean}'

        display_name = message.author.display_name
        username = message.author.name
        full_name = f"{display_name} (nickname: {username})" if display_name != username else display_name

        channel_name = getattr(message.channel, 'name', 'DM')
        logging.info(f"[User Message - #{channel_name}] {full_name}: {user_content_clean}")

        if self.troll_mode:
            # In troll mode, write immediately (no lock contention)
            await add_message(self.db, channel_id, 'user', user_content_clean, self.count_tokens, name=full_name)
            self.last_troll_channel = message.channel
        elif mentioned:
            # explicit mention — user message is written inside the lock to prevent ordering races
            asyncio.create_task(self.locked_trigger_generation(
                channel_id, message, explicit=True, trigger_message=message,
                user_content=user_content_clean, user_name=full_name,
            ))
        else:
            # autonomous evaluation — user message is written inside the lock to prevent ordering races
            asyncio.create_task(self.locked_evaluate_and_respond(
                channel_id, message.channel, trigger_message=message,
                user_content=user_content_clean, user_name=full_name,
            ))
