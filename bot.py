# imports
import discord
import openai
import asyncio
import logging
import aiosqlite
import os
import time
import re
import tiktoken
import json

# logging config
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

# load config
try:
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except FileNotFoundError:
    logging.critical("config.json not found! Please create it.")
    exit()

DISCORD_TOKEN = config.get("DISCORD_TOKEN", "your discord bot token")

# backend selection ('local' or 'gemini')
LLM_BACKEND = config.get("LLM_BACKEND", "gemini")

# local backend settings
LLAMA_BASE_URL = config.get("LLAMA_BASE_URL", "http://127.0.0.1:8080/v1")
LLAMA_API_KEY  = config.get("LLAMA_API_KEY", "sk-no-key-required")
LLAMA_MODEL_ID = config.get("LLAMA_MODEL_ID", "default")

# gemini backend settings
GEMINI_API_KEY  = config.get("GEMINI_API_KEY", "your gemini api key")
GEMINI_MODEL_ID = config.get("GEMINI_MODEL_ID", "gemini-3.1-flash-lite-preview")

# shared settings
MAX_TOKEN_LIMIT       = config.get("MAX_TOKEN_LIMIT", 1000)
DB_FILE               = config.get("DB_FILE", "history.db")
MAX_DISCORD_MSG_LEN   = config.get("MAX_DISCORD_MSG_LEN", 2000)
API_COOLDOWN_SECONDS  = config.get("API_COOLDOWN_SECONDS", 1.0)

# resolve config
if LLM_BACKEND == "gemini":
    _API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
    _API_KEY      = GEMINI_API_KEY
    _MODEL_ID     = GEMINI_MODEL_ID
elif LLM_BACKEND == "local":
    _API_BASE_URL = LLAMA_BASE_URL
    _API_KEY      = LLAMA_API_KEY
    _MODEL_ID     = LLAMA_MODEL_ID
else:
    raise ValueError(f"Unknown LLM_BACKEND value: '{LLM_BACKEND}'. Must be 'local' or 'gemini'.")


# api lock class
class LlamaAPILock:
    def __init__(self, cooldown):
        self.lock = asyncio.Lock()
        self.last_call_time = 0
        self.cooldown = cooldown

# main bot class
class LlamaDiscordBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)

        self.api_monitor = LlamaAPILock(cooldown=API_COOLDOWN_SECONDS)
        self.db = None
        
        # autonomous settings
        self.AUTONOMOUS_THRESHOLD = 7 # score (1-10) to trigger speaking

        try:
            with open("systemprompt.txt", "r", encoding="utf-8") as f:
                self.bot_persona = f.read()
        except FileNotFoundError:
            logging.info("systemprompt.txt not found. Proceeding without a system prompt.")
            self.bot_persona = None 

        try:
            self.openai_client = openai.AsyncOpenAI(
                base_url=_API_BASE_URL,
                api_key=_API_KEY,
            )
        except Exception as e:
            logging.critical(f"Error configuring OpenAI client: {e}")
            exit()

        try:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            logging.critical(f"Failed to load tokenizer: {e}")
            exit()

        logging.info(f"LLM backend: {LLM_BACKEND.upper()} | Model: {_MODEL_ID}")

    async def setup_hook(self):
        await self.init_db()

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()

    async def init_db(self):
        try:
            self.db = await aiosqlite.connect(DB_FILE)
            self.db.row_factory = aiosqlite.Row
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA synchronous=NORMAL")
            await self.db.execute('''CREATE TABLE IF NOT EXISTS history 
                             (id INTEGER PRIMARY KEY AUTOINCREMENT,
                              channel_id INTEGER, 
                              role TEXT, 
                              name TEXT,
                              content TEXT, 
                              tokens INTEGER,
                              timestamp REAL)''')
            await self.db.execute('''CREATE INDEX IF NOT EXISTS idx_channel_time ON history (channel_id, timestamp DESC)''')
            await self.db.commit()
        except Exception as e:
            logging.critical(f"Database initialization failed: {e}")
            exit()

    async def add_message_to_db(self, channel_id, role, content, name=None):
        tokens = self.count_tokens(content)
        try:
            await self.db.execute(
                "INSERT INTO history (channel_id, role, name, content, tokens, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, role, name, content, tokens, time.time())
            )
            await self.db.commit()
        except Exception as e:
            logging.error(f"Failed to insert message into DB: {e}")

    async def get_context_from_db(self, channel_id):
        system_tokens = self.count_tokens(self.bot_persona) if self.bot_persona else 0
        available_tokens = MAX_TOKEN_LIMIT - system_tokens
        try:
            query = """
                WITH OrderedHistory AS (
                    SELECT role, name, content, tokens, timestamp
                    FROM history 
                    WHERE channel_id = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 50
                ),
                RunningTotal AS (
                    SELECT role, name, content, timestamp,
                           SUM(tokens) OVER (ORDER BY timestamp DESC) as cumulative_tokens
                    FROM OrderedHistory
                )
                SELECT role, name, content FROM RunningTotal 
                WHERE cumulative_tokens <= ?
                ORDER BY timestamp ASC
            """
            async with self.db.execute(query, (channel_id, available_tokens)) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logging.error(f"Failed to fetch context from DB: {e}")
            return []

    def get_sanitized_history(self, history_list):
        if not history_list: return []
        sanitized = []
        for msg in history_list:
            role = msg['role']
            content = msg['content']
            name = msg.get('name')
            if role == 'user' and name:
                content = f"{name} said: {content}"
            sanitized.append({'role': role, 'content': content})
        
        while sanitized and sanitized[0]['role'] == 'assistant':
            sanitized.pop(0)
            
        merged = []
        for entry in sanitized:
            if merged and merged[-1]['role'] == entry['role']:
                merged[-1]['content'] += f"\n{entry['content']}"
            else:
                merged.append(entry)
        return merged

    def count_tokens(self, text):
        if not text: return 0
        return len(self.tokenizer.encode(str(text)))

    def split_message(self, text, limit=MAX_DISCORD_MSG_LEN):
        chunks = []
        while len(text) > limit:
            split_index = text.rfind('\n', 0, limit)
            if split_index == -1: split_index = text.rfind(' ', 0, limit)
            if split_index == -1: split_index = limit
            chunks.append(text[:split_index])
            text = text[split_index:].strip() 
        if text: chunks.append(text)
        return chunks

    async def call_llama_api(self, conversation_messages):
        async with self.api_monitor.lock:
            elapsed = time.time() - self.api_monitor.last_call_time
            if elapsed < self.api_monitor.cooldown:
                await asyncio.sleep(self.api_monitor.cooldown - elapsed)
            try:
                completion = await self.openai_client.chat.completions.create(
                    model=_MODEL_ID,
                    messages=conversation_messages,
                    max_tokens=1000,
                )
                self.api_monitor.last_call_time = time.time()
                return completion.choices[0].message.content.strip()
            except Exception as e:
                self.api_monitor.last_call_time = time.time()
                raise e

    # decision logic

    async def evaluate_and_respond(self, channel_id, channel):
        raw_history = await self.get_context_from_db(channel_id)
        sanitized_history = self.get_sanitized_history(raw_history)
        if not sanitized_history: return

        # social intelligence triage prompt
        triage_system = (
            "You are the social reasoning engine for an AI bot. Your goal is to decide if the bot should speak. "
            "Examine the chat history. Provide a 'score' from 1 to 10 on how appropriate it is for you to reply. "
            "Considerations:\n"
            "- If users are asking you questions or talking about topics you like: Score High (8-10).\n"
            "- If there is a joke you can complete or a factual error you can fix: Score Medium (6-8).\n"
            "- If the conversation is moving very fast between humans and you aren't needed: Score Low (1-4).\n"
            "- If users have recently told you to be quiet or 'shut up', or are ignoring you: Score Very Low (1).\n"
            "Output ONLY valid JSON: {\"score\": <int>, \"reasoning\": \"<string>\"}"
        )
        if self.bot_persona:
            triage_system += f"\nYour Persona Context:\n{self.bot_persona}"

        payload = [{'role': 'system', 'content': triage_system}]
        payload.extend(sanitized_history)

        try:
            triage_response = await self.call_llama_api(payload)
            triage_response = re.sub(r'<(thought|think)>.*?</\1>\s*', '', triage_response, flags=re.DOTALL)
            json_match = re.search(r'\{.*\}', triage_response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                score = int(data.get('score', 0))
                reasoning = data.get('reasoning', '')
                
                logging.info(f"[Triage - #{channel.name}] Score: {score}/{self.AUTONOMOUS_THRESHOLD} | Reason: {reasoning}")
                
                if score >= self.AUTONOMOUS_THRESHOLD:
                    await self.trigger_generation(channel_id, channel, explicit=False, reasoning=reasoning)
        except Exception as e:
            logging.error(f"Triage evaluation failed: {e}")

    async def trigger_generation(self, channel_id, target, explicit=False, reasoning=None):
        channel = target.channel if explicit else target
        async with channel.typing():
            try:
                raw_history = await self.get_context_from_db(channel_id)
                sanitized_history = self.get_sanitized_history(raw_history)
                
                system_content = self.bot_persona if self.bot_persona else "You are a helpful AI assistant."
                if not explicit and reasoning:
                    system_content += f"\n\nInternal Note: You decided to interject because: '{reasoning}'."
                
                payload = [{'role': 'system', 'content': system_content}]
                payload.extend(sanitized_history)

                response_text = await self.call_llama_api(payload)
                response_text = response_text.replace('\u200b', '')
                
                # strip out thought blocks
                response_text = re.sub(r'<(thought|think)>.*?</\1>\s*', '', response_text, flags=re.DOTALL)

                # easter egg check
                user_message_lower = target.clean_content.lower() if explicit else (sanitized_history[-1]['content'].lower() if sanitized_history else "")
                if any(k in user_message_lower for k in ['woke', 'liberal']):
                    response_text = response_text.rstrip() + " I hit the jack pot! $                  1"

                await self.add_message_to_db(channel_id, 'assistant', response_text)

                for chunk in self.split_message(response_text):
                    if explicit:
                        await target.reply(chunk)
                    else:
                        await channel.send(chunk)
                    await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"Generation Error: {ascii(str(e))}")

    # event handlers

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

        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', message.author.display_name)[:64]
        await self.add_message_to_db(channel_id, 'user', user_content_clean, name=safe_name)

        if mentioned:
            # explicit mention trigger
            await self.trigger_generation(channel_id, message, explicit=True)
        else:
            # evaluate individual messages
            asyncio.create_task(self.evaluate_and_respond(channel_id, message.channel))

# main execution
if __name__ == "__main__":
    bot = LlamaDiscordBot()
    bot.run(DISCORD_TOKEN)