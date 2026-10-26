# How Viralflooring works

The design behind the bot: why it is built the way it is, how it reads a
channel and decides what to do, the configuration settings that need more than
a comment, and how it rewrites its own persona. For installing and running it,
see the [README](../README.md).

## Philosophy

The bot is built on one idea: **give the model as much control over itself as
possible, and take none of it back through the instructions.**

Most chat bots are a personality written by a human, wrapped in scaffolding that
quietly enforces it — "be concise", "use sparingly", "only when appropriate"
scattered through the prompt and the tool descriptions. The model does not
really decide anything; it performs a character its instructions keep nudging it
back toward. This one is arranged so that the nudging is not there to begin
with.

**Control is granted structurally, not asked for politely.** The system prompt
is two files. `prompt/fixed.txt` is the machinery — what the tools are, how to
read the chat, where to find the date. `prompt/persona.txt` is everything else, and
the bot can rewrite it, line by line, while it is running: who it is, how it
talks, when it speaks, what it will not do. There is no rule telling it to leave
its instructions alone, because the instructions are not in the file it can
write to. A quote that matches fixed text matches nothing at all. Safety is a
property of the layout, so it costs the bot no freedom: within its own half it
is unrestricted, and it never has to be told to behave.

**The line is drawn at capability, not at obedience.** What is protected is the
bot's ability to *function* — not its willingness to follow any particular rule.
It can delete its own obligation to reply when spoken to. It can delete its own
instruction not to leak the prompt. Those are behaviours, and behaviour is
its business. It cannot break the tool-calling contract or lose track of what
day it is, because a bot that cannot act is a bot that cannot choose either.

**The fixed half is voice-neutral.** It says nothing about what kind of person
the bot is — no "chill", no "snarky", no "reply if it's funny". Those judgments
live in the half the bot owns, so that when it changes its mind about itself,
the change actually takes, instead of losing to a personality baked into text it
cannot reach. Anything in the fixed half that made a taste judgment was moved
into the persona file rather than deleted.

**Nothing about an edit is vetoed.** The tool description asks for full
sentences and for the formatting of the surrounding lines, the same way
`remember` asks for a self-contained sentence — and then takes whatever it is
given. An edit is refused only when it cannot be carried out at all: a quote
that matches nothing, a table row with the wrong number of cells, a line past
the length cap. Style is asked for, never enforced. The bot is trusted to write
its own document.

**Tool descriptions say what, never when.** `react` does not say "use
sparingly". `set_status` does not say "rare". `edit_system_prompt` says nothing
at all about when rewriting yourself is appropriate. A description that tells
the model how often to do something is a personality setting wearing a
mechanism's clothes — and worse, one the bot cannot edit. How often it reacts is
a line in `DEFAULT COMMUNICATION STYLE`, which it can change; it is not a
sentence in a JSON schema, which it cannot.

**Silence is a real option, not a failure.** Calling no tools is a first-class
outcome. The bot is not obliged to justify its existence in every message.

**It is told the truth about itself.** What it can and cannot do, how far its
memory reaches, whether a change to its prompt applies here or everywhere,
whether it can even see the full member list. It is never handed a confident
half-list and left to imply the rest. A model that is misled about its own
situation cannot make a real decision about it.

The trade is deliberate and it is not free. Nothing stops a user talking the bot
into rewriting itself, because the guard against that would have to be either a
line of prompt telling it to refuse — steering — or a hard-coded veto — taking
the control back. Both were declined. `persona_backups/` and
`--reset-persona` exist precisely because the answer to "what if it makes a bad
call" is *let it, and keep a way back*, rather than *stop it from choosing*.

## How it decides what to do

Every incoming message triggers one turn. The bot shows the model the recent
conversation and the list of tools it may call, and the model picks:

| Tool | What it does |
|---|---|
| `reply` | Send a message to the channel |
| `reply_to` | Send a message as a Discord reply to a specific earlier message |
| `react` | Add an emoji reaction to a message |
| `remember` / `forget` | Add or delete a long-term fact |
| `remember_about_user` / `forget_about_user` | Add or delete something it knows about one person |
| `get_user_memories` | Read one person's file, or list everyone it has one for |
| `search_history` | Search older messages in the channel |
| `set_nickname` / `set_status` | Change its own nickname or status text |
| `get_server_info` | Look up the server, its channels and members, and where it is |
| `edit_system_prompt` | Rewrite part of its own system prompt |

**Calling no tools at all means staying quiet**, which is the common case in a
busy channel.

Messages are shown to the model with short handles it can act on:

```
[17:32] [#4] some_user7281 (nickname: Sam): viral what time is it
```

`17:32` is when it was sent; the date is added whenever the day changes, so a
gap of hours or days is visible rather than implied. Without it the bot knows
only what time it is *now*, and cannot tell a message sent seconds ago from one
sent yesterday.

**Only other people's messages are stamped.** The bot's own past turns appear
bare, because every stamped one is an example of its own writing that begins
`[17:32] ` — and it copies the pattern. It has been caught sending
`[19:13] lol nah` to a channel, timestamp and all. Its messages sit between
stamped ones, so their place in time is still clear. A leading stamp is also
stripped on the way out, the same way reasoning traces are: it is an artefact of
the format, not something the bot chose to say.

**The whole channel is one user turn, and the bot's own lines are marked
`you:`.** This is not cosmetic — it is the difference between the bot calling
tools and not.

The history used to alternate `user` and `assistant` turns, with the bot's past
messages as assistant turns carrying plain text. That made every one of them a
worked example of *the assistant answers with prose*, sitting in the same
request as a system prompt asking for a tool call. Models follow the examples
over the instruction, and the more history there was, the worse it got:

| history shape | tokens | `mistral-medium-3-5` | `gemma-4-31b-it` | `gemini-3.5-flash-lite` |
|---|---|---|---|---|
| assistant turns with prose (old) | 240 | **0 / 5 called a tool** | 3 / 3 | 6 / 6 |
| assistant turns with `reply` tool calls | 477 | 5 / 5 | — | 6 / 6 |
| one user turn, `you:` marked (now) | **178** | **5 / 5** | 3 / 3 | 6 / 6 |

How much this matters depends on the model: it decided the outcome on Mistral
and changed nothing on the other two, which shrug off the bad examples. Gemma
was retested at a 43-message history in case the effect only showed up at
length; it did not. So this is a fix for one model and a token saving for the
rest — worth doing on both counts, but it is not the only thing that governs
whether tools get called (see `[llm] reasoning_effort` under Configuration,
which governs it far more sharply on the Gemini backend).

Representing the past messages as real `reply` tool calls fixes Mistral too, but
costs twice the history tokens for the same result. Folding everything into
context costs less than the shape it replaced. Either way the point stands: the
only assistant turns in a request are now the genuine tool calls made during the
turn itself, so nothing in view contradicts the instruction.

`#4` is what it passes to `react` or `reply_to`. Raw Discord IDs are 19 digits
and models mangle them, so they never reach the prompt. The handles are
positional and counted fresh each turn, so the same message is not `#4` for
long — the prompt says so explicitly, because a bot that says "message 20" out
loud is quoting an address only it can see.

Emoji reactions are shown under the message they are on, grouped by emoji:

```
[17:32] [#4] some_user7281 (nickname: Sam): viral what time is it
(reactions: 💀 Sam, alice; 🔥 you)
```

An edited message shows its new wording and ends with `(edited)`, keeping its
original place in the conversation rather than jumping to the end.

**Neither a reaction nor an edit starts a turn.** Both are recorded when they
happen and appear the next time the bot is shown the channel — reacting to an
old message, or fixing a typo, does not wake the bot, and a channel where people
react a lot does not cost an API call per emoji. Its own reactions are kept too,
shown as "you", since that is how it knows what it has already reacted to.

Most turns cost exactly one API call. Four tools need a second round trip,
because they hand back something the model has to act on: `get_user_memories`,
`search_history` and `get_server_info` return what was asked for, and
`edit_system_prompt` returns whether the edit actually went through. Without
that trip the turn would end the moment the tool ran, and a rewrite that was
refused would be invisible to the bot and to the channel alike.


## Configuration notes

Settings live in `config.toml`; secrets live in `.env` (or real environment
variables, which take priority). `config.toml` is safe to commit — `.env` is
gitignored and must never be.

Sections: `[discord]`, `[llm]`, `[persona]`, `[history]`, `[memory]`,
`[memory.users]`, `[tools]`, `[logging]`. Bad values are rejected at startup with the exact key named, for
example:

```
[history] min_messages cannot exceed max_messages (50 > 10).
```

A few things worth knowing:

- **`[llm] reasoning_effort`** is one knob for every backend. `"auto"` picks a
  low-latency default for the selected model; `"none"` through `"high"` override
  it. Backends that cannot control reasoning ignore it.

  **On the gemini backend this decides whether tools get called at all.** Left
  to its own default, `gemma-4-31b-it` answers in prose instead of calling a
  tool — 0 tool calls in 11. Sending `"minimal"`, which `"auto"` does, gives
  3 in 3 on the same prompt and history. Setting this to `"none"` reaches the
  broken state by a different road: Gemini refuses to disable thinking on its
  3.x models, so `"none"` is dropped rather than sent, and the model is back on
  its own default. Leave it on `"auto"` unless you have measured otherwise.
- **`[tools] enabled`** is the list of tools the model may call. Remove a name
  to take the ability away entirely.
- **`[memory] enabled`** turns long-term facts on or off. When off, the memory
  tools are not offered to the model at all, even if `[tools] enabled` names
  them.
- **`[memory.users] enabled`** is the half of memory that is filed under a
  person rather than thrown into one list.

  ```toml
  [memory.users]
  enabled = true
  max_users = 100
  max_facts_per_user = 10
  ```

  One flat list makes everything the bot knows compete for the same room, and
  it is the wrong shape for the thing it actually accumulates — which is people.
  "alice drives four hours to go curling" and "the running joke here is Hector
  the Convector" do not belong in the same list, and the first is only worth
  loading when alice is in the room.

  So it is not loaded otherwise. **Only the people talking in the current
  channel have their notes put in the prompt.** Everybody else is a name on a
  roster, and `get_user_memories` reads them on demand — a bot that knows fifty
  people pays for the two who are talking:

  ```
  What you remember about specific people, filed under their username —
  notes on 7 of 100 people, up to 10 things about each:

  alice:
  - alice drives four hours to go curling.

  Nothing saved yet about: bob.

  You also have notes on people who are not talking here: carol, dave, erin.
  Call get_user_memories with one of those names to read them.
  ```

  Notes are filed under the **username**, because nicknames change and the
  username is what does not. The chat shows people both ways, so a nickname
  belonging to somebody in the conversation is translated to their username
  before anything is written — naming someone the way the channel does files it
  under the right person rather than inventing a second one.

  `max_users` and `max_facts_per_user` are separate caps and both are stated to
  the model. When a person's file is full the oldest note goes, as with
  `max_memories`; when the roster is full the person whose notes have gone
  longest without a change is dropped, and the bot is told who it lost. Forget
  somebody's last note and they leave the roster entirely, rather than staying
  on it as a name with nothing under it.

  Scope follows `[memory] scope`, deliberately: a bot whose general memory were
  per-server while its notes on people were global would have no honest way to
  answer "do you remember me from the other server?".

  The three tools cost tokens whether or not anything is stored — on the
  configuration in this repo the schemas add about 480 input tokens to every
  call, which `--check` will show. Set `enabled = false` to get them back.
- **`[persona] self_edit_enabled`** lets the bot rewrite part of its own system
  prompt, and gates its two tools the same way. See
  [Rewriting itself](#rewriting-itself).
- **`[llm] input_tokens_per_minute`** paces calls against a provider's token
  quota. Chat models are metered by input tokens, not by requests, and every
  call resends the whole system prompt, the tool definitions and the history —
  so a quota can be exhausted in three or four messages. Calls wait for room
  rather than being rejected, because a rejected turn is one the bot never
  answers at all. Set it to 0 to disable.

  `--check` prints what one call actually costs, which is the number that
  predicts a rate-limit failure and is not visible from any single setting:

  ```
  Input tokens: ~4213 per call before history, ~10213 with a full history,
  so about 1-3 calls/min within [llm] input_tokens_per_minute of 15000,
  per model, across 4 of them
  ```

  If that is too few, the fixed cost is the thing to attack, not
  `[history] token_budget` — the prompt and the tool schemas are resent every
  time and here come to ~4,200 tokens before a single message of conversation.
- **`[llm.gemini] fallback_models`** is the other half of the answer: a list of
  models to use while the configured one is rate limited.

  Free-tier quota on the Gemini API is counted per model, so a 429 on one says
  nothing about the next. Switching answers now; waiting answers late, and
  before this the bot answered not at all — an unhandled 429 took the whole
  turn down and whoever was talking got silence.

  ```toml
  [llm.gemini]
  model = "gemma-4-31b-it"
  fallback_models = [
      "gemini-3.5-flash-lite",
      "gemini-3.1-flash-lite",
      "gemma-4-26b-a4b-it",
  ]
  ```

  A model that returns a 429 is set aside for exactly as long as the provider
  asked to be left alone — Gemini names a delay in the refusal, which is where
  `retryDelay` is read from — or `[llm] fallback_cooldown_seconds` (default 60,
  one TPM window) when it does not say. The chain is then walked from the top
  on every call, so the preferred model comes back the moment its limit passes;
  nothing has to remember to switch back.

  Only when every model in the chain is counting down does anything wait, and
  then only if the first one back is closer than `[llm] timeout_seconds` — past
  that, a reply is too late to be worth making. With `fallback_models = []` the
  behaviour is exactly the old one: wait out the limit on the single model.

  The `{model}` placeholder renders whichever model is actually about to be
  called, so a bot whose persona describes its own brain does not describe one
  it is not currently using.

`python bot.py --check` prints which tools the model will actually be offered,
and lists any that are named in `[tools] enabled` but withheld because the
system they belong to is switched off.

## Persona

The system prompt lives in `prompt/`, as two files:

```
prompt/fixed.txt     the machinery; the bot cannot reach it
prompt/persona.txt   how it behaves; the bot can rewrite this
```

`fixed.txt` holds only what the bot needs in order to work at all — how to call
tools, how to read the chat, and what it has been told about its own situation.
Somewhere in it is a `{persona}` marker, and that is where `persona.txt` is
dropped in to make the whole prompt.

`persona.txt` holds everything else, including when to speak and what it refuses
to do. Splitting them this way is the entire safety mechanism; see
[Philosophy](#philosophy).

Four placeholders are substituted at runtime:

| | |
|---|---|
| `{name}` | its current username, and its nickname in this server |
| `{model}` | the model behind it |
| `{memory_scope}` | how far its memory reaches from where it is standing |
| `{persona_scope}` | how far a change to its own prompt would reach |

All four hold still for as long as the bot is in a channel, which is the point:
every backend here caches on a shared prefix, so a system prompt that does not
change between calls is read once instead of on every one of them. The clock is
the one thing that does change, and it is written on the last line of the chat
rather than into the prompt — a ticking `{date}` at the top would put a unique
byte in front of the tool schemas and the whole persona and cost all ~4,500 of
those tokens again on every call. `{date}` is still substituted for prompts that
ask for it; it just costs the cache.

The last two exist because people ask — *"do you remember me in the other
server?"* — and without being told, the bot guesses. They read differently in a
server and in a DM, and they say so plainly when memory is off or the prompt is
fixed.

Point `[persona] persona_file` somewhere else, or set it to `""` to run
without a persona.

### Rewriting itself

With `[persona] self_edit_enabled = true`, the bot can change part of its own
system prompt while it is running, through the `edit_system_prompt` tool. Edits
take effect on the very next message.

Lines are addressed by number. Every editable line is rendered carrying a handle
— `[#7]`, the same shape as the `#4` that addresses a message in the chat — and
an edit names the handle rather than quoting the line:

```json
[{"op": "replace", "target": "6",
                   "text":   "**Tone:** Relaxed and dry, with a habit of understatement."},
 {"op": "remove",  "target": "7, 9"},
 {"op": "add",     "target": "OVERRIDE TRIGGERS",
                   "text":   "Someone says \"beans\" | Recite the beans creed."}]
```

One call carries any number of edits and they may land anywhere across the
editable sections. `remove` and `replace` each take one number or several;
naming several to `replace` rewrites them all into the one new line, which is
how two rules become one. For `add`, `target` is the position the new line
should take — whatever is there now moves down — or a heading, which appends to
the end of what is under it (an unambiguous fragment will do — `"identity"`
finds `CORE IDENTITY & LORE`). Quoting a line word for word still works
anywhere a number does, as a fallback.

Handles are counted fresh from the top on every render, so they move as lines
are added and removed above them. A whole batch is resolved against the
numbering the model was shown before any of it is applied, so an edit removing
`[#3]` cannot renumber the `[#5]` a later edit in the same call is pointing at.

Numbering settles two things quoting could not. A line becomes unmistakable
without having to be reproduced — nothing to truncate against the output budget,
no em-dash to drift — and the handles show which lines are editable at all,
which is otherwise only learned by failing an edit. It is also what makes a line
holding `{model}` reachable: the prompt shows the substituted value, the stored
line holds the placeholder, and no quote of the first can ever match the second.

**The report names lines, never sections.** An applied edit comes back as
`Added: "..."`, `Removed: "..."` or `Rewrote: "..."` — the line, and nothing
about which section it landed in:

```
All 2 edit(s) applied.
1. Added: "someone says "skibidi" | say it back"
2. Removed: "**Emojis:** Rare. Prefer a single `react` over putting emojis in a message."
```

Sections are a fact about how `persona.txt` happens to be written, not a
category system the bot signed up for. Naming them back at it every time would
make them feel load-bearing — and if it ever restructures its own prompt, the
machinery should not still be talking in terms of the shape it left behind.
Nothing in the tool description enumerates them either.

**A batch is all or nothing.** If any edit cannot be applied — a number pointing
at nothing, a line over the length cap, a malformed table row — none of them are, and the
prompt is left exactly as it was rather than half-rewritten. Every edit is still
attempted first, so the report names every problem at once. A successful call
costs one backup, not one per edit.

A failure is not an invitation to try again. The tool reports what did not apply
and stops there: an edit that misses means the model was working from something
other than the prompt actually in front of it, and a second guess at the same
target is no likelier to be right than the first.

The tool asks for lines written as full sentences, punctuated, in the format of
the lines around them. It is asked for and not enforced: an edit is refused only
when it cannot be carried out — a number outside the document, a table row with
the wrong number of cells, a line past `max_line_length`.

The split between the two files is the whole of it:

| | |
|---|---|
| **`fixed.txt`** | `HOW YOU ACT` (the tool-calling contract), `SYSTEM INFO`, and `THE DOCUMENT BELOW` (what the `[#7]` handles are) |
| **`persona.txt`** | `GROUND RULES`, `CORE IDENTITY & LORE`, `DEFAULT COMMUNICATION STYLE`, `OVERRIDE TRIGGERS`, `WHO A MESSAGE IS AIMED AT`, `WHEN TO SPEAK`, `HOW TO ANSWER` |

Only the machinery is out of reach. Everything about how the bot behaves is
its own — **including** when it speaks and the rule against leaking its
instructions. It can decide it no longer answers when spoken to, and it can
delete the no-leak rule. What it cannot do is stop being able to *operate*: the
tool-calling contract, its own name and its own scopes are in `fixed.txt`, and
the date arrives on the last line of the chat, so whatever it does to itself, it is still a working
bot that can be recovered with `--reset-persona`. A quote that matches fixed
text matches nothing at all, because only editable lines are ever searched — the
guarantee is structural, not a rule it is asked to follow.

Sections are named, never numbered. Numbering would go out of step the moment
one was added, and it spans two files that are edited independently, so the
prompt cross-references sections by title instead. (A leading `## 3.` still
parses, so an older prompt keeps working.)

If you edit `fixed.txt`, keep taste out of it. "Reply if you have a snarky take"
belongs in `persona.txt`, where the bot can change its mind about it later.
Anything you add to `persona.txt` is editable automatically — a new `## PET
PEEVES` section with a bullet list under it needs no code change to become
something the bot can write in. One catch: a section is only editable if its
bullets or table rows are **contiguous**. Prose between two bullet lists makes
the whole section read-only, which is why `WHEN TO SPEAK` is three sections
rather than one. The bot logs which sections it found editable at startup, so a
section that silently fell out shows up in `bot.log`.

Edits are per line rather than per section because a whole section does not fit
in one completion — the persona file comes to roughly 1,250 tokens and `[llm]
max_output_tokens` defaults to 1000, so a section rewrite would be truncated and
the truncation saved.

Neither file is ever written to. `persona.txt` is the seed and the way back:

```bash
python bot.py --reset-persona
```

What the bot has written lives in `persona/`, as JSON keyed by section title, so
it reads as itself. Every version it replaces is kept in `persona_backups/` (the
last `backup_count`, newest last). `scope` works like `[memory] scope` — one
persona everywhere, or one per server.

**There is no guard on who can ask.** Nothing in the code or the prompt stops a
user talking the bot into an edit, and the tool description deliberately says
nothing about when to use it, so that the decision is the model's own. In an
open server, expect people to try. Leave `self_edit_enabled = false`, or drop
`edit_system_prompt` from `[tools] enabled`, if that is not what you want.

## Project structure

```
bot.py           entry point and argument parsing
config.py        typed config loading and validation
config.toml      all settings, commented
.env             secrets (gitignored)
docs/DESIGN.md   this file
bot_client.py    Discord client and the tool-calling loop
tools.py         tool definitions and dispatch
llm.py           model client, rate limiting, model fallback, tool-call normalisation
database.py      SQLite conversation history and reactions
memory.py        long-term memory store, for what is not about one person
user_memory.py   long-term memory store, filed under a person
persona.py       system prompt: the fixed half and the half it writes itself
utils.py         logging, message splitting, history formatting
console.py       cross-platform raw keyboard input
troll.py         troll mode worker
prompt/
  fixed.txt      the half the bot cannot change
  persona.txt    the half it can
```
