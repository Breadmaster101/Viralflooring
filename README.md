# Viralflooring

An autonomous Discord bot that uses either local models (via llama.cpp) or the Gemini API to converse with users. It features a unique "social intelligence" triage system that allows it to decide when to interject in conversations organically, rather than relying solely on explicit mentions.

## Features
- **Autonomous Interjections**: Uses an LLM-based triage system to evaluate chat history and determine if it should speak.
- **Dual Backends**: Supports both local inference (llama.cpp) and cloud API (Google Gemini).
- **Context Memory**: Maintains a continuous conversation history per channel using an SQLite database.
- **Persona Support**: Loads a custom persona from `systemprompt.txt` (a default example is included).

## Requirements
- Python 3.8+
- `discord.py`
- `openai`
- `aiosqlite`
- `tiktoken`

## Setup
1. Clone the repository.
2. Install dependencies: `pip install discord.py openai aiosqlite tiktoken`
3. Configure the bot by editing the config section in `bot.py`:
   - Set `DISCORD_TOKEN` to your Discord bot token.
   - Choose your `LLM_BACKEND` (`"local"` or `"gemini"`).
   - If using Gemini, set your `GEMINI_API_KEY`.
4. Customize the included `systemprompt.txt` file to define the bot's specific persona. (An example "chill and laid-back" persona is provided by default). You can delete this file if you don't want to use a system prompt.
5. Run the bot: `python bot.py`