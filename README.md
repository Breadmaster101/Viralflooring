# Viralflooring

An autonomous Discord bot that uses either local models (via llama.cpp), the Gemini API, OpenRouter, Cerebras, or Mistral to converse with users. It features a unique "social intelligence" triage system that allows it to decide when to interject in conversations organically, rather than relying solely on explicit mentions. It also features a persistent memory system to remember long-term facts about users.

## Features
- **Autonomous Interjections**: Uses an LLM-based triage system to evaluate chat history and determine if it should speak.
- **Multiple Backends**: Supports local inference (llama.cpp), Google Gemini, OpenRouter (access to 200+ models), Cerebras, and Mistral.
- **Persistent Memory System**: Automatically remembers and forgets facts about users over time, storing them persistently to build a long-term understanding of the community.
- **Emoji Reactions**: The bot can organically react to messages with emojis.
- **Context Memory**: Maintains a continuous conversation history per channel using an SQLite database.
- **Persona Support**: Loads a custom persona from `systemprompt.txt` (a default example is included).

## Project Structure
```
bot.py           — Entry point (run this)
config.py        — Config loader and constant exports
config.jsonc     — Configuration file with comments explaining every setting
bot_client.py    — Main Discord bot class (event handlers, triage, generation)
llm.py           — LLM API interaction (rate limiting, client creation, API calls)
memory.py        — Persistent memory system for storing long-term facts
database.py      — SQLite database operations for conversation history
utils.py         — Shared utilities (typing indicator, message splitting, etc.)
systemprompt.txt — Bot persona / system prompt
```

## Requirements
- Python 3.8+
- `discord.py`
- `openai`
- `aiosqlite`
- `tiktoken`

## Setup
1. Clone the repository.
2. Install dependencies: `pip install discord.py openai aiosqlite tiktoken`
3. Configure the bot by editing `config.jsonc`:
   - Set `DISCORD_TOKEN` to your Discord bot token.
   - Choose your `LLM_BACKEND` (`"local"`, `"gemini"`, `"openrouter"`, `"cerebras"`, or `"mistral"`).
   - Add the respective API key for your chosen backend (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`).
   - Configure memory settings like `DISABLE_MEMORY_SYSTEM` and `MEMORY_SCOPE`.
4. Customize the included `systemprompt.txt` file to define the bot's specific persona. (An example "chill and laid-back" persona is provided by default). You can delete this file if you don't want to use a system prompt.
5. Run the bot: `python bot.py`
6. Activate troll mode with `python bot.py --troll-mode`.