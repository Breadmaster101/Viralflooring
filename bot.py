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

# logging config
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

DISCORD_TOKEN = "insert discord token here"
LLAMA_BASE_URL = "http://127.0.0.1:8080/v1" 
LLAMA_API_KEY = "sk-no-key-required"
LLAMA_MODEL_ID = "default"

MAX_TOKEN_LIMIT = 1000        
DB_FILE = "history.db"        
MAX_DISCORD_MSG_LEN = 2000    
API_COOLDOWN_SECONDS = 1.0    
NAME_TRIGGERS = ["viral", "grok", "ai", "llm"]

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
        
        try:
            with open("systemprompt.txt", "r", encoding="utf-8") as f:
                self.bot_persona = f.read()
        except FileNotFoundError:
            logging.info("systemprompt.txt not found. Proceeding without a system prompt.")
            self.bot_persona = None 

        try:
            self.openai_client = openai.AsyncOpenAI(
                base_url=LLAMA_BASE_URL, 
                api_key=LLAMA_API_KEY
            )
        except Exception as e:
            logging.critical(f"Error configuring OpenAI client: {e}")
            exit()

        try:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            logging.critical(f"Failed to load tokenizer: {e}")
            exit()

    # db and close logic
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

    # add message to database
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

    # retrieve context
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

    # clean and merge context
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

    # token helper
    def count_tokens(self, text):
        if not text: return 0
        return len(self.tokenizer.encode(str(text)))

    # message splitter
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

    # llama api call
    async def call_llama_api(self, conversation_messages):
        async with self.api_monitor.lock:
            elapsed = time.time() - self.api_monitor.last_call_time
            if elapsed < self.api_monitor.cooldown:
                await asyncio.sleep(self.api_monitor.cooldown - elapsed)

            try:
                completion = await self.openai_client.chat.completions.create(
                    model=LLAMA_MODEL_ID,
                    messages=conversation_messages,
                    max_tokens=1000,
                )
                self.api_monitor.last_call_time = time.time()

                return completion.choices[0].message.content.strip()
            except Exception as e:
                self.api_monitor.last_call_time = time.time()
                raise e

    # event handlers
    async def on_ready(self):
        logging.info(f'Logged in as {self.user} targeting {LLAMA_MODEL_ID}')

    async def on_message(self, message):
        mentioned = self.user.mentioned_in(message)
        msg_lower = message.content.lower()
        name_triggered = any(re.search(rf'\b{re.escape(trigger)}\b', msg_lower) for trigger in NAME_TRIGGERS)

        if message.author == self.user and not mentioned:
            return

        if not mentioned and not name_triggered:
            return

        channel_id = message.channel.id
        async with message.channel.typing():
            try:
                user_content_clean = message.clean_content
                if mentioned:
                    user_content_clean = user_content_clean.replace(f"@{self.user.name}", "")
                user_content_clean = user_content_clean.strip()

                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    ref_author = ref.author.display_name
                    ref_content = ref.clean_content[:200]
                    user_content_clean = f'[replying to {ref_author}: "{ref_content}"]\n{user_content_clean}'

                safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', message.author.display_name)[:64]
                
                await self.add_message_to_db(channel_id, 'user', user_content_clean, name=safe_name)
                
                raw_history = await self.get_context_from_db(channel_id)
                sanitized_history = self.get_sanitized_history(raw_history)
                
                payload = []
                if self.bot_persona:
                    payload.append({'role': 'system', 'content': self.bot_persona})
                payload.extend(sanitized_history)

                response_text = await self.call_llama_api(payload)
                response_text = response_text.replace('\u200b', '')

                user_message_lower = message.clean_content.lower()
                if any(k in user_message_lower for k in ['woke', 'liberal']):
                    response_text = response_text.rstrip() + " I hit the jack pot! $                  1"

                await self.add_message_to_db(channel_id, 'assistant', response_text)

                for chunk in self.split_message(response_text):
                    await message.reply(chunk)
                    await asyncio.sleep(0.5)

            except Exception as e:
                logging.error(f"Error: {ascii(str(e))}")
                await message.reply("I'm overstimmed, give me a minute.")

# main execution
if __name__ == "__main__":
    bot = LlamaDiscordBot()
    bot.run(DISCORD_TOKEN)