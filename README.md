# Viralflooring

An autonomous Discord bot that decides for itself when to talk. It reads the
channel, then replies, reacts, remembers things about people, or stays quiet,
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
   safe. This is your `DISCORD_TOKEN`. Anyone with it can control your bot, so
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
   - Manage Webhooks (only needed for [subvirals](#add-subvirals))
5. Open the generated URL at the bottom, pick your server, and authorize.

> The bot talks in **every channel it can see**. To keep it to certain
> channels, use Discord's channel permissions to hide the rest from it.

### 2. Get a model API key

Pick one. Gemini is the default in `config.toml` and the easiest to start with.

| Provider | Where to get a key | `.env` variable | `config.toml` |
|---|---|---|---|
| **Gemini** (free) | [Google AI Studio](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` | `backend = "gemini"` |
| **Mistral** (free tier) | [Mistral console](https://console.mistral.ai) | `MISTRAL_API_KEY` | `backend = "mistral"` |
| **Local** | No key needed, just run [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` with `--jinja` | `LOCAL_API_KEY` (leave as is) | `backend = "local"` |

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
default is a laid-back chat regular called Viral Flooring, so **change it**. Give
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

### Add subvirals

Subvirals are extra characters that talk alongside the bot. Each one speaks
through a channel webhook under its own name and avatar, so it looks like a
separate person in the chat. They can only talk: no reactions, memory or other
tools. The main bot isn't told they exist and treats them as strangers.

The default subviral is **Grimbald the Squire** (he goes by Phip), a medieval
squire with a gout-ridden master and a deep hatred of Lent, in
[`prompt/subvirals/grimbald.txt`](prompt/subvirals/grimbald.txt). To turn
subvirals on:

1. Give the bot the **Manage Webhooks** permission in the channels they should
   talk in. They only work in servers, since DMs have no webhooks.
2. Set `enabled = true` under `[subvirals]` in `config.toml`.
3. Each `[[subvirals.bots]]` block is one subviral: a `name`, a `persona_file`,
   an optional `avatar_url`, and optionally its own `backend` and `model`.
   Grimbald uses `backend = "mistral"` so he doesn't eat the main bot's Gemini
   quota, which needs `MISTRAL_API_KEY` in `.env`. Set `backend = ""` to share
   the main bot's backend instead.

Every subviral reads every message and decides for itself whether to speak,
one at a time after the main bot's turn. `max_chain` (default `0`) is how many
extra rounds of bots answering each other one message can start; at `0`,
subvirals only answer people. Each subviral costs one API call per message.

All subvirals share [`prompt/subviral_fixed.txt`](prompt/subviral_fixed.txt),
the rules for reading the chat and when to speak, with each one's own file
dropped in where `{persona}` is.

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

### Design

The bot is built on one idea: **give the model as much control over itself as
possible, and take none of it back through the instructions.**

- **Control is structural.** The system prompt is two files. `prompt/fixed.txt`
  is the machinery (how tools work, how to read the chat). `prompt/persona.txt`
  is everything else, and the bot can rewrite it line by line while running.
  The bot is never told to leave its machinery alone, because the machinery is
  in a file it cannot write to.
- **The line is drawn at capability, not obedience.** It can delete its own rule
  to reply when spoken to, or its rule against leaking the prompt. It cannot
  break tool calling or lose track of the date.
- **The fixed half is voice-neutral.** All taste judgments ("be concise", "reply
  if it's funny") live in the persona file, so a change the bot makes to itself
  actually takes.
- **Tool descriptions say what, never when.** How often to react or speak is a
  line in the persona, which the bot can edit, not a sentence in a schema, which
  it cannot.
- **Silence is a real option.** Calling no tools is a normal outcome.
- **It is told the truth about itself**: what it can do, how far its memory
  reaches, and whether it can see the full member list.

The trade is deliberate: nothing stops a user talking the bot into rewriting
itself. The answer to a bad call is `persona_backups/` and `--reset-persona`,
not a veto.

### How a turn works

Every new message gives the bot one turn. It sees the recent chat and a list of
tools, and picks one or more, or none (staying quiet, the common case):

| Tool | What it does |
|---|---|
| `reply` / `reply_to` | Send a message, or reply to a specific earlier one |
| `react` | Add an emoji reaction |
| `remember` / `forget` | Add or delete a long-term fact |
| `remember_about_user` / `forget_about_user` | Add or delete a note about one person |
| `get_user_memories` | Read one person's notes, or list everyone it has notes on |
| `search_history` | Search older messages in the channel |
| `set_nickname` / `set_status` | Change its own nickname or status text |
| `get_server_info` | Look up the server, its channels and members |
| `edit_system_prompt` | Rewrite part of its own persona |

The chat is shown to the model as a single user turn, with the bot's own past
lines marked `you:`. Replaying them as assistant turns taught some models
(Mistral in particular) to answer in prose instead of calling tools.

```
[17:32] [#4] some_user7281 (nickname: Sam): viral what time is it
(reactions: 💀 Sam, alice; 🔥 you)
```

- `[#4]` is a short handle the model passes to `react` or `reply_to`. Raw
  Discord IDs are 19 digits and models mangle them. Handles are recounted every
  turn.
- Only other people's messages carry a timestamp; the bot's own lines are bare,
  because it copies any pattern it sees in its own past messages. The current
  date and time are on the last line of the chat.
- Reactions and edits are recorded when they happen and shown on the next turn.
  Neither starts a turn on its own.
- Most turns cost one API call. Tools that return something the model must act
  on (`get_user_memories`, `search_history`, `get_server_info`,
  `edit_system_prompt`) need a second one.

### Memory

`[memory]` holds general long-term facts. `[memory.users]` holds notes filed
under a person's **username** (nicknames change, usernames do not). Only the
people talking in the current channel have their notes put in the prompt; the
bot reads anyone else's on demand with `get_user_memories`. When a file or the
roster is full, the oldest note (or the longest-untouched person) is dropped
and the bot is told. Scope follows `[memory] scope`. The user-memory tools add
about 480 input tokens per call; set `[memory.users] enabled = false` to get
them back.

### Rate limits and fallback

Chat models are metered by input tokens, and every call resends the system
prompt, tool schemas and history, so a free quota can run out in a few
messages.

- `[llm] input_tokens_per_minute` makes calls wait for room instead of being
  rejected. `python bot.py --check` prints what one call costs.
- `[llm.gemini] fallback_models` lists models to use while the preferred one is
  rate limited. A model that returns a 429 is set aside for as long as the
  provider asks (or `fallback_cooldown_seconds`), and the preferred model is
  tried first again on every call. The chain only waits if every model is
  limited and the first to recover is sooner than `timeout_seconds`.
- `[llm] reasoning_effort = "auto"` should be left alone. On Gemini it decides
  whether tools get called at all: `gemma-4-31b-it` answered in prose instead of
  calling a tool 11 times out of 11 at its own default, and `"none"` is dropped
  on Gemini 3.x models, which puts it back on that default.

### The persona and self-editing

`prompt/persona.txt` is the seed and is never written to. The bot's own edits
live in `persona/` (keyed by section title), with backups in `persona_backups/`.
Four placeholders are filled in at runtime: `{name}`, `{model}`,
`{memory_scope}` and `{persona_scope}`. They stay constant while the bot is in a
channel so the provider's prompt cache keeps working. Set `persona_file` to `""`
to run without a persona.

With `[persona] self_edit_enabled = true`, the bot edits itself through
`edit_system_prompt`, and edits apply on the next message. Each editable line is
shown with a handle like `[#7]`:

```json
[{"op": "replace", "target": "6", "text": "**Tone:** Relaxed and dry."},
 {"op": "remove",  "target": "7, 9"},
 {"op": "add",     "target": "OVERRIDE TRIGGERS",
                   "text":   "Someone says \"beans\" | Recite the beans creed."}]
```

- `remove` and `replace` take one number or several (several lines collapse into
  one new line). For `add`, `target` is a position, or a heading to append
  under. Quoting a line word for word also works.
- Handles are resolved against the numbering the model saw, so edits in one
  batch cannot shift each other.
- A batch is all or nothing, and the report lists every problem at once. Edits
  are refused only if they cannot be carried out (bad number, wrong table cell
  count, a line over `max_line_length`); style is asked for, never enforced.
- Only `persona.txt` sections are editable: `GROUND RULES`,
  `CORE IDENTITY & LORE`, `DEFAULT COMMUNICATION STYLE`, `OVERRIDE TRIGGERS`,
  `WHO A MESSAGE IS AIMED AT`, `WHEN TO SPEAK` and `HOW TO ANSWER`. A new `##`
  section with contiguous bullets or table rows becomes editable automatically.
  Prose between two bullet lists makes a section read-only.
- If you edit `fixed.txt`, keep taste out of it.

### Project structure

```
bot.py           entry point and argument parsing
config.py        typed config loading and validation
config.toml      all settings, commented
.env             secrets (gitignored)
bot_client.py    Discord client and the tool-calling loop
tools.py         tool definitions and dispatch
llm.py           model client, rate limiting, model fallback
database.py      SQLite conversation history and reactions
memory.py        long-term memory store
user_memory.py   long-term memory filed under a person
persona.py       system prompt: the fixed half and the editable half
subviral.py      subvirals: webhook characters and their turns
utils.py         logging, message splitting, history formatting
console.py       cross-platform raw keyboard input
troll.py         troll mode worker
prompt/          fixed.txt (bot cannot change) and persona.txt (it can),
                 plus subviral_fixed.txt and subvirals/ for the subvirals
```

### Privacy

Chat history (`history.db`), memories, the bot's persona edits and your `.env`
are stored only on the machine running the bot, and are all gitignored. The only
thing sent anywhere else is each turn's prompt, to the model provider you chose.
