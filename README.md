# Viralflooring

An autonomous Discord bot that decides for itself when to talk. It reads the
channel, then replies, reacts, remembers things about people, or stays quiet —
all through tool calls. It can even rewrite its own personality while it runs.

Works with **Google Gemini / Gemma** (free tier), **Mistral** (free tier), or a
**local model** through llama.cpp or any OpenAI-compatible server. Light enough
to run on a Raspberry Pi.

- [Quick start](#quick-start)
- [Make it yours](#make-it-yours)
- [Commands](#commands)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)

---

## Quick start

You need **Python 3.11 or newer**, a Discord account, and an API key for one
model provider. Setup takes about 10 minutes.

### 1. Create the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**. Give it a name.
2. Open the **Bot** tab. Click **Reset Token** and copy the token somewhere
   safe — this is your `DISCORD_TOKEN`. Anyone with it can control your bot, so
   never share or commit it.
3. Still on the **Bot** tab, under **Privileged Gateway Intents**, turn on
   **Message Content Intent** and save. The bot will not start without it.
4. Open **OAuth2 → URL Generator**. Tick the `bot` scope, then these
   permissions:
   - View Channels
   - Send Messages
   - Read Message History
   - Add Reactions
   - Change Nickname
5. Open the generated URL at the bottom, pick your server, and authorize.

> The bot talks in **every channel it can see**. To keep it to certain
> channels, use Discord's channel permissions to hide the rest from it.

### 2. Get a model API key

Pick one. Gemini is the default in `config.toml` and the easiest to start with.

| Provider | Where to get a key | `.env` variable | `config.toml` |
|---|---|---|---|
| **Gemini** (free) | [Google AI Studio](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` | `backend = "gemini"` |
| **Mistral** (free tier) | [Mistral console](https://console.mistral.ai) | `MISTRAL_API_KEY` | `backend = "mistral"` |
| **Local** | No key needed — run [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` with `--jinja` | `LOCAL_API_KEY` (leave as is) | `backend = "local"` |

### 3. Install and run

Pick your operating system.

<details>
<summary><b>Linux (including Raspberry Pi)</b></summary>

1. Install Python and git. On Debian, Ubuntu or Raspberry Pi OS:

   ```bash
   sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
   ```

   Check the version is 3.11 or newer with `python3 --version`.
   (Raspberry Pi OS Bookworm and Ubuntu 24.04 already are.)

2. Download the bot and go into its folder:

   ```bash
   git clone https://github.com/Breadmaster101/Viralflooring.git
   cd Viralflooring
   ```

3. Create a virtual environment and install the dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Create your secrets file and fill in your Discord token and API key:

   ```bash
   cp .env.example .env
   nano .env
   ```

   In nano, save with `Ctrl+O`, `Enter`, then quit with `Ctrl+X`.

5. If you are not using Gemini, open `config.toml` and change `backend` under
   `[llm]` (see the table in step 2).

6. Check everything is set up, then start the bot:

   ```bash
   python bot.py --check
   python bot.py
   ```

Next time, start it with `cd Viralflooring`, `source .venv/bin/activate`,
`python bot.py`. Stop it with `Ctrl+C`.

</details>

<details>
<summary><b>Windows</b></summary>

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/).
   On the first installer screen, tick **Add python.exe to PATH**.
   Or, from PowerShell:

   ```powershell
   winget install Python.Python.3.12
   ```

2. Download the bot. Either install [Git for Windows](https://git-scm.com/download/win)
   and run the commands below in PowerShell, or click **Code → Download ZIP** on
   the GitHub page, unzip it, and open PowerShell in that folder.

   ```powershell
   git clone https://github.com/Breadmaster101/Viralflooring.git
   cd Viralflooring
   ```

3. Create a virtual environment and install the dependencies:

   ```powershell
   py -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

4. Create your secrets file and fill in your Discord token and API key:

   ```powershell
   copy .env.example .env
   notepad .env
   ```

   Save and close Notepad when you are done.

5. If you are not using Gemini, open `config.toml` (`notepad config.toml`) and
   change `backend` under `[llm]` (see the table in step 2).

6. Check everything is set up, then start the bot:

   ```powershell
   .venv\Scripts\python bot.py --check
   .venv\Scripts\python bot.py
   ```

Next time, open PowerShell in the folder and run `.venv\Scripts\python bot.py`.
Stop it with `Ctrl+C`.

</details>

<details>
<summary><b>macOS</b></summary>

1. Install Python 3.11 or newer. With [Homebrew](https://brew.sh):

   ```bash
   brew install python@3.12 git
   ```

   Or use the installer from [python.org](https://www.python.org/downloads/macos/).
   If you do, open **Applications → Python 3.x** afterwards and double-click
   **Install Certificates.command**, or the bot will fail to connect with an SSL
   error.

2. Download the bot and go into its folder:

   ```bash
   git clone https://github.com/Breadmaster101/Viralflooring.git
   cd Viralflooring
   ```

3. Create a virtual environment and install the dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Create your secrets file and fill in your Discord token and API key:

   ```bash
   cp .env.example .env
   open -e .env
   ```

   This opens it in TextEdit. Save and close when you are done.

5. If you are not using Gemini, open `config.toml` (`open -e config.toml`) and
   change `backend` under `[llm]` (see the table in step 2).

6. Check everything is set up, then start the bot:

   ```bash
   python bot.py --check
   python bot.py
   ```

Next time, start it with `cd Viralflooring`, `source .venv/bin/activate`,
`python bot.py`. Stop it with `Ctrl+C`.

</details>

Once it's running, say hi to it by name in your server. It answers whenever it's
mentioned, replied to, or called by name, and joins other conversations when it
has something to add.

---

## Make it yours

### Change the persona

The bot's personality lives in [`prompt/persona.txt`](prompt/persona.txt). The
default is a laid-back chat regular called Viral Flooring — **change it**. Give
it a new name, a voice, interests, and rules about when to speak.

- Each `## HEADING` is a section. Keep the headings, and write the rules
  underneath as bullet points or table rows.
- You can add your own sections, such as `## PET PEEVES`, with no code changes.
- `{model}` and `{name}` are filled in automatically with the model it's
  running on and its Discord name.
- Leave [`prompt/fixed.txt`](prompt/fixed.txt) alone. It's the machinery that
  makes tool calling work, not the personality.

**The bot can rewrite its own persona while it runs**, separately for each
server. Its edits are saved in `persona/`. Once it has made one, editing
`persona.txt` won't change that server until you reset it:

```bash
python bot.py --reset-persona
```

To stop it rewriting itself, set `self_edit_enabled = false` under `[persona]`
in `config.toml`. Be aware that anyone in the server can try to talk it into an
edit.

### Pick a model

Under `[llm]` in `config.toml`, set `backend`, then the `model` in the matching
`[llm.gemini]`, `[llm.mistral]` or `[llm.local]` section. For a local server,
also set `base_url` (default `http://127.0.0.1:8080/v1`).

If you switch to **Mistral**, raise `input_tokens_per_minute` to `120000`. The
default is tuned for Gemini's tighter free tier.

### Turn features on or off

Everything is in [`config.toml`](config.toml), and every setting has a comment
explaining it. The most useful ones:

| Setting | What it does |
|---|---|
| `[tools] enabled` | The list of things the bot may do. Remove a name to take that ability away, such as `set_nickname` or `edit_system_prompt`. |
| `[memory] enabled` | Long-term memory of facts and people. Off means it keeps no notes and only knows what's in recent chat. |
| `[memory] scope` / `[persona] scope` | `"per_server"` keeps each server separate, `"global"` shares one memory or persona everywhere. |
| `[discord] allow_dms` | Whether it answers direct messages. |
| `[discord] member_intent` | Lets it see the full member list. Also turn on **Server Members Intent** in the Developer Portal, or the bot won't start. |
| `[logging] level` | Set to `"DEBUG"` to see everything it's thinking about in `bot.log`. |

---

## Commands

Run these from the bot's folder with the virtual environment active (on Windows,
use `.venv\Scripts\python` in place of `python`).

| Command | What it does |
|---|---|
| `python bot.py` | Start the bot. |
| `python bot.py --check` | Check the config and keys, list the tools it will use, and show how many tokens each call costs. Starts nothing. |
| `python bot.py --reset-persona` | Throw away the bot's own persona edits and go back to `prompt/persona.txt`. A backup goes in `persona_backups/`. |
| `python bot.py --troll-mode` | Turn the AI off and type the bot's messages yourself. The typing indicator follows your keystrokes. |
| `python bot.py --config PATH` | Use a different config file. |

---

## Troubleshooting

**"Discord rejected the token"**
`DISCORD_TOKEN` in `.env` is wrong or was reset. Copy a fresh one from the
**Bot** tab, with no quotes or spaces around it.

**"Missing a privileged intent"**
Turn on **Message Content Intent** under **Bot → Privileged Gateway Intents**.
If you set `member_intent = true`, turn on **Server Members Intent** too.

**The bot is online but never replies**
Make sure it can view and send messages in that channel. Then check `bot.log`
for errors, or set `[logging] level = "DEBUG"` to see each decision it makes.
It won't answer every message on purpose, but it should always answer when you
use its name.

**"... is missing. Set GEMINI_API_KEY ..."**
The key for the backend in `config.toml` isn't in `.env`. Make sure the
variable matches the backend (see [step 2](#2-get-a-model-api-key)).

**`ModuleNotFoundError: No module named 'tomllib'`**
Your Python is older than 3.11. Install a newer one and recreate `.venv`.

**`CERTIFICATE_VERIFY_FAILED` on macOS**
Run **Install Certificates.command** from your Python folder in Applications.

**Rate-limit (429) errors or slow replies**
Free tiers allow only a few calls a minute. Run `python bot.py --check` to see
the cost per call. Lower `[history] token_budget` or remove unused tools to
make each call cheaper. On Gemini, the bot switches to the
`fallback_models` automatically while one model is limited.

**PowerShell says running scripts is disabled**
Use `.venv\Scripts\python ...` as shown above instead of activating the
environment.

---

## How it works

Every new message gives the bot one turn. It sees the recent chat and a list of
tools, and picks: `reply`, `react`, `remember`, `search_history`,
`edit_system_prompt`, and so on — or nothing at all, which is the most common
choice in a busy channel.

The system prompt is split in two. `prompt/fixed.txt` is the machinery the bot
can't touch. `prompt/persona.txt` is everything about who it is, and the bot can
rewrite it. Because the machinery is out of reach, no edit can break the bot,
and `--reset-persona` always gets you back.

The full write-up — design philosophy, how the chat is shown to the model, the
memory system, rate limiting and model fallback, and how self-editing works — is
in [docs/DESIGN.md](docs/DESIGN.md).

Chat history (`history.db`), memories, the bot's persona edits and your `.env`
are stored only on the machine running the bot, and are all gitignored. The only
thing sent anywhere else is each turn's prompt, to the model provider you chose.
