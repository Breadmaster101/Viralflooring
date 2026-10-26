# The system prompt, assembled from two files:
#   prompt/fixed.txt    never changes; a template where {persona} marks the insertion point
#   prompt/persona.txt  the persona; each "## TITLE" section with a bullet list or
#                       table is editable by the bot while running
# Neither file is written to. Bot edits live as JSON in persona/, seeded from
# persona.txt, with a backup in persona_backups/ before every write
# (`python bot.py --reset-persona` restores the authored text).
#
# Edits address lines by [#n] handle, numbered fresh from the top on each render.
# A batch is resolved against the numbering the model was shown and applied
# all-or-nothing. Editing by line keeps an edit within the output token budget.

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import PersonaConfig

log = logging.getLogger(__name__)

# Where persona.txt goes in fixed.txt
PERSONA_PLACEHOLDER = "{persona}"

# Sections are named, not numbered (a leading number is still tolerated)
_HEADER = re.compile(r"^##\s+(?:\d+\.\s*)?(.+?)\s*$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_TABLE_ROW = re.compile(r"^\|(.*)\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\|[\s:|-]+\|\s*$")

# A line's handle in the prompt; a target may be written "7", "#7" or "[#7]"
_REF_MARKER = re.compile(r"^\[#\s*\d+\s*\]\s*")
_REF = re.compile(r"^\[?\s*#?\s*(\d+)\s*\]?$")


# ---------------------------------------------------------------------------
#  Parsing prompt/persona.txt
# ---------------------------------------------------------------------------

@dataclass
class Section:
    # One "## TITLE" block. Sections with no editable lines stay in `raw`; the
    # rest split into scaffolding (intro/outro) and the editable `lines`.

    title: str
    raw: list[str]
    key: str | None = None
    style: str = "bullet"        # "bullet" or "table"
    intro: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    outro: list[str] = field(default_factory=list)
    columns: int = 1             # cells per row, for table sections


def _cells(inner: str) -> list[str]:
    return [c.strip() for c in inner.split("|")]


def _norm(title: str) -> str:
    # Section title, whitespace- and case-folded; used as the JSON key
    return " ".join(title.upper().split())


def _split_editable(block: list[str], key: str) -> Section | None:
    # Find the contiguous run of editable lines (bullets win over table rows), or None
    title = _HEADER.match(block[0]).group(1)

    indices = [i for i, line in enumerate(block) if _BULLET.match(line)]
    style, columns = "bullet", 1

    if not indices:
        rows = [
            i for i, line in enumerate(block)
            if _TABLE_ROW.match(line) and not _TABLE_SEPARATOR.match(line)
        ]
        # The header row and separator stay fixed
        if len(rows) < 2:
            return None
        style = "table"
        columns = len(_cells(_TABLE_ROW.match(block[rows[0]]).group(1)))
        indices = rows[1:]

    first, last = indices[0], indices[-1]
    if indices != list(range(first, last + 1)):
        log.warning(
            "Section %r has editable lines that are not contiguous; leaving it fixed.",
            title,
        )
        return None

    contents = []
    for line in block[first : last + 1]:
        if style == "bullet":
            contents.append(_BULLET.match(line).group(1).strip())
        else:
            contents.append(" | ".join(_cells(_TABLE_ROW.match(line).group(1))))

    return Section(
        title=title, raw=block, key=key, style=style, columns=columns,
        intro=block[:first], lines=contents, outro=block[last + 1 :],
    )


def parse_sections(text: str) -> list[Section]:
    # Every section with editable lines is the bot's to edit
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if _HEADER.match(line) or not blocks:
            blocks.append([])
        blocks[-1].append(line)

    sections: list[Section] = []
    for block in blocks:
        header = _HEADER.match(block[0]) if block else None
        if header is None:
            # Before the first heading: kept, never edited
            sections.append(Section(title="(preamble)", raw=block))
            continue

        title = header.group(1)
        section = _split_editable(block, _norm(title))
        if section is None:
            log.info("Section %r has no editable lines; it will be left as written.", title)
        sections.append(section or Section(title=title, raw=block))
    return sections


def _render_line(content: str, section: Section, ref: int | None = None) -> str:
    # On a table row the handle goes inside the first cell
    marker = f"[#{ref}] " if ref is not None else ""
    if section.style == "table":
        cells = _cells(content)
        cells[0] = marker + cells[0]
        return "| " + " | ".join(cells) + " |"
    return f"- {marker}{content}"


def parse_refs(target: str) -> list[int] | None:
    # None means "not handles", so the caller falls back to matching text
    parts = [p for p in re.split(r"[,;\s]+", target.strip()) if p]
    refs = []
    for part in parts:
        found = _REF.match(part)
        if not found:
            return None
        refs.append(int(found.group(1)))
    return refs or None


# ---------------------------------------------------------------------------
#  Normalising what the model sends
# ---------------------------------------------------------------------------

def normalise(text: str, section: Section) -> tuple[str | None, str]:
    # Turn a model-supplied line into stored form, or explain what is wrong.
    # Bullet and pipe markers are accepted and stripped.
    # One line in, one line out
    text = " ".join(text.split())
    if not text:
        return None, "Nothing was changed: the line was empty."

    bullet = _BULLET.match(text)
    if bullet:
        text = bullet.group(1).strip()
    # Drop a copied handle
    text = _REF_MARKER.sub("", text).strip()

    if section.style == "table":
        text = text.strip("|").strip()
        cells = _cells(text)
        cells[0] = _REF_MARKER.sub("", cells[0]).strip()
        if len(cells) != section.columns:
            example = (
                " | ".join(_cells(section.lines[0])) if section.lines
                else "when it happens | what you do"
            )
            return None, (
                f"that line has {len(cells)} part(s), but the rows it would join "
                f"have {section.columns}. Write it as {_short(example)}."
            )
        text = " | ".join(cells)

    if not text:
        return None, "Nothing was changed: the line was empty."
    return text, ""


def _match_key(content: str) -> str:
    # Comparison form for finding a line by text; strips markers like normalise
    text = " ".join(content.split()).strip()
    bullet = _BULLET.match(text)
    if bullet:
        text = bullet.group(1).strip()
    text = _REF_MARKER.sub("", text).strip()
    text = text.strip("|").strip()
    # Normalise spacing around interior pipes
    cells = _cells(text)
    cells[0] = _REF_MARKER.sub("", cells[0]).strip()
    return " | ".join(cells).lower()


def _short(text: str, limit: int = 60) -> str:
    # Quote a line briefly, so a batch of outcomes stays cheap to read back
    text = " ".join(text.split())
    return f'"{text}"' if len(text) <= limit else f'"{text[:limit].rstrip()}…"'


# ---------------------------------------------------------------------------
#  Edits
# ---------------------------------------------------------------------------

def _field(value: Any) -> str | None:
    # One field of an edit as text; lists are joined, so [3, 5] works like "3, 5"
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list) and value and all(
        isinstance(item, (str, int, float)) and not isinstance(item, bool)
        for item in value
    ):
        return ", ".join(str(item) for item in value)
    return None


@dataclass(frozen=True)
class Edit:
    # One change. `target` is a line number (or several), or for an add the
    # position or a section name; `text` is new content (unused by remove).

    op: str
    target: str
    text: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> Edit | None:
        # Accepts common synonyms for the field names
        if not isinstance(raw, dict):
            return None
        get = lambda *names: next(
            (text for text in (_field(raw.get(n)) for n in names) if text is not None),
            "",
        )
        op = get("op", "operation", "action", "type")
        target = get(
            "target", "targets", "line", "lines", "ref", "refs", "number",
            "numbers", "anchor", "after", "section", "old",
        )
        text = get("text", "content", "new", "value", "replacement")
        return cls(op=op, target=target, text=text) if op or target else None


@dataclass
class _Plan:
    # A batch planned in full before writing. Keyed by position in the starting
    # state; counts/texts track what each section will end up holding.

    counts: dict[str, int]
    texts: dict[str, set[str]]
    removed: set[tuple[str, int]] = field(default_factory=set)
    rewritten: dict[tuple[str, int], str] = field(default_factory=dict)
    inserted: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    appended: dict[str, list[str]] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
#  Store
# ---------------------------------------------------------------------------

class PersonaStore:
    # The rendered system prompt and its editable lines, read from disk each turn

    def __init__(self, cfg: PersonaConfig, fixed_text: str, persona_text: str,
                 directory: Path, backup_directory: Path):
        self.cfg = cfg
        self.directory = directory
        self.backup_directory = backup_directory
        self.fixed_text = fixed_text
        self.sections = parse_sections(persona_text)
        self.editable: dict[str, Section] = {
            s.key: s for s in self.sections if s.key is not None
        }
        if PERSONA_PLACEHOLDER not in fixed_text:
            log.warning(
                "The fixed prompt has no %s marker, so the persona sections are "
                "appended to the end of it instead of sitting where they belong.",
                PERSONA_PLACEHOLDER,
            )

    @property
    def can_self_edit(self) -> bool:
        return self.cfg.self_edit_enabled and bool(self.editable)

    # ------------------------------------------------------------------
    #  Files
    # ------------------------------------------------------------------

    def _file(self, guild_id: int | None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.cfg.scope == "per_server" and guild_id is not None:
            return self.directory / f"persona_{guild_id}.json"
        return self.directory / "persona_global.json"

    def _seed(self) -> dict[str, list[str]]:
        # The authored lines from prompt/persona.txt, used until the bot edits
        return {key: list(section.lines) for key, section in self.editable.items()}

    def load(self, guild_id: int | None = None) -> dict[str, list[str]]:
        state = self._seed()
        path = self._file(guild_id)
        if not path.is_file():
            return state

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Fall back to the authored persona
            log.exception(
                "Failed to load persona from %s; using %s instead.",
                path, self.cfg.persona_file,
            )
            return state

        stored = data.get("sections", {}) if isinstance(data, dict) else {}
        for key in self.editable:
            value = stored.get(key)
            if isinstance(value, list):
                state[key] = [v.strip() for v in value if isinstance(v, str) and v.strip()]
        return state

    def _backup(self, path: Path) -> None:
        # Copy the current state aside before it is overwritten or deleted
        if not path.is_file() or self.cfg.backup_count <= 0:
            return
        try:
            self.backup_directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = self.backup_directory / f"{path.stem}_{stamp}.json"
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            # A failed backup must not block the edit
            log.exception("Failed to back up %s", path)
            return

        # Prune per file stem
        existing = sorted(self.backup_directory.glob(f"{path.stem}_*.json"))
        for old in existing[: max(0, len(existing) - self.cfg.backup_count)]:
            old.unlink(missing_ok=True)

    def save(self, state: dict[str, list[str]], guild_id: int | None = None) -> bool:
        path = self._file(guild_id)
        self._backup(path)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps({"sections": state}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            log.exception("Failed to save persona to %s", path)
            tmp.unlink(missing_ok=True)
            return False
        return True

    # ------------------------------------------------------------------
    #  Rendering
    # ------------------------------------------------------------------

    def render(self, guild_id: int | None = None) -> str:
        # Rebuild the whole prompt: the fixed file with the persona dropped in
        state = self.load(guild_id)
        out: list[str] = []
        ref = 0
        for section in self.sections:
            if section.key is None:
                out.extend(section.raw)
                continue
            out.extend(section.intro)
            for line in state.get(section.key, ()):
                ref += 1
                out.append(_render_line(line, section, ref))
            out.extend(section.outro)

        persona = "\n".join(out).rstrip()
        if PERSONA_PLACEHOLDER in self.fixed_text:
            return self.fixed_text.replace(PERSONA_PLACEHOLDER, persona).rstrip()
        return f"{self.fixed_text.rstrip()}\n\n{persona}"

    # ------------------------------------------------------------------
    #  Editing
    # ------------------------------------------------------------------

    def _find(self, state: dict[str, list[str]], target: str) -> tuple[str, int] | None:
        # Fallback for non-handle targets; fixed lines are not in `state`, so never match
        wanted = _match_key(target)
        for key in self.editable:
            for index, line in enumerate(state[key]):
                if _match_key(line) == wanted:
                    return key, index
        return None

    def ref_table(self, state: dict[str, list[str]]) -> list[tuple[str, int]]:
        # Handle N (1-based) -> its line, in the same order as render()
        table: list[tuple[str, int]] = []
        for section in self.sections:
            if section.key is None:
                continue
            table.extend((section.key, i) for i in range(len(state.get(section.key, ()))))
        return table

    def _find_section(self, target: str) -> str | None:
        # An unambiguous fragment of the title is enough; ambiguous ones resolve to None
        wanted = _norm(target)
        if wanted in self.editable:
            return wanted
        partial = [key for key in self.editable if wanted and wanted in key]
        return partial[0] if len(partial) == 1 else None

    def _resolve(self, state: dict[str, list[str]], table: list[tuple[str, int]],
                 target: str) -> tuple[list[tuple[str, int]], str]:
        # Work out which lines a target names. Returns (lines, problem)
        refs = parse_refs(target)
        if refs is None:
            found = self._find(state, target)
            if found is None:
                return [], f"nothing you can change is numbered or reads {_short(target)}."
            return [found], ""

        if not table:
            return [], "there are no numbered lines to change yet."
        for ref in refs:
            if not 1 <= ref <= len(table):
                return [], (
                    f"there is no line [#{ref}]. The lines you can change are "
                    f"[#1] to [#{len(table)}]."
                )
        # Dedupe
        lines: list[tuple[str, int]] = []
        for ref in refs:
            if table[ref - 1] not in lines:
                lines.append(table[ref - 1])
        return lines, ""

    def apply_edits(self, edits: list[Edit],
                    guild_id: int | None = None) -> tuple[int, str]:
        # All or none: the batch is planned against the numbering the model saw,
        # every edit is attempted so all problems are reported, and a success
        # writes the file once.
        if not edits:
            return 0, "No edits were given, so nothing changed."

        state = self.load(guild_id)
        table = self.ref_table(state)
        plan = _Plan(
            counts={key: len(lines) for key, lines in state.items()},
            texts={key: {_match_key(l) for l in lines} for key, lines in state.items()},
        )

        outcomes: list[str] = []
        failed: list[int] = []
        for number, edit in enumerate(edits, start=1):
            ok, note = self._plan_one(state, table, plan, edit)
            outcomes.append(f"{number}. {note}")
            if not ok:
                failed.append(number)

        if failed:
            which = ", ".join(str(n) for n in failed)
            # Logged, since the model may claim it worked anyway
            log.warning(
                "[Persona] nothing changed; edit(s) %s did not apply: %s",
                which, " ".join(outcomes),
            )
            # Not an invitation to retry
            return 0, (
                f"Nothing was changed. Edit(s) {which} could not be applied to your "
                f"system prompt, so the whole call was cancelled rather than leaving "
                f"it half-rewritten:\n" + "\n".join(outcomes)
            )

        if not self.save(self._rebuild(state, plan), guild_id):
            return 0, "Your system prompt could not be written to. Nothing changed."

        # Log only after a successful save
        for entry in plan.log:
            log.info("[Persona] %s", entry)
        return len(edits), (
            f"All {len(edits)} edit(s) applied. The lines are numbered again from "
            f"the top, so the numbers you were working from are now out of date.\n"
            + "\n".join(outcomes)
        )

    def _rebuild(self, state: dict[str, list[str]],
                 plan: _Plan) -> dict[str, list[str]]:
        # Walk the lines as they were and emit them as they will be
        working: dict[str, list[str]] = {}
        for key, lines in state.items():
            out: list[str] = []
            for index, line in enumerate(lines):
                out.extend(plan.inserted.get((key, index), ()))
                if (key, index) in plan.removed:
                    continue
                out.append(plan.rewritten.get((key, index), line))
            out.extend(plan.appended.get(key, ()))
            working[key] = out
        return working

    def _plan_one(self, state: dict[str, list[str]], table: list[tuple[str, int]],
                  plan: _Plan, edit: Edit) -> tuple[bool, str]:
        # Fold one edit into the plan. Returns (planned, explanation)
        op = (edit.op or "").strip().lower()
        target = (edit.target or "").strip()

        if op not in ("add", "remove", "replace"):
            return False, f"{op!r} is not an operation. Use add, remove, or replace."
        if not target:
            return False, f"Failed: {op} needs a target."

        if op == "add":
            return self._plan_add(state, table, plan, target, edit.text)

        lines, problem = self._resolve(state, table, target)
        if problem:
            return False, f"Failed: {problem}"
        for spot in lines:
            if spot in plan.removed or spot in plan.rewritten:
                return False, (
                    f"Failed: {_short(state[spot[0]][spot[1]])} is already being "
                    f"changed by an earlier edit in this call."
                )

        if op == "remove":
            for key, index in lines:
                gone = state[key][index]
                plan.removed.add((key, index))
                plan.counts[key] -= 1
                plan.texts[key].discard(_match_key(gone))
                plan.log.append(f"removed {gone!r}")
            return True, "Removed: " + ", ".join(_short(state[k][i]) for k, i in lines)

        # replace: the first line takes the new text and the rest are removed
        key, index = lines[0]
        content, problem = self._prepare(
            plan, key, edit.text,
            ignoring={_match_key(state[k][i]) for k, i in lines},
        )
        if content is None:
            return False, f"Failed: {problem}"

        for spot in lines[1:]:
            merged = state[spot[0]][spot[1]]
            plan.removed.add(spot)
            plan.counts[spot[0]] -= 1
            plan.texts[spot[0]].discard(_match_key(merged))
            plan.log.append(f"removed {merged!r}")
        plan.rewritten[(key, index)] = content
        plan.texts[key].discard(_match_key(state[key][index]))
        plan.texts[key].add(_match_key(content))
        plan.log.append(f"replaced {state[key][index]!r} with {content!r}")
        return True, f"Rewrote: {_short(content)}"

    def _plan_add(self, state: dict[str, list[str]], table: list[tuple[str, int]],
                  plan: _Plan, target: str, text: str) -> tuple[bool, str]:
        # Plan an insertion: the target says where, not what
        key = None if parse_refs(target) else self._find_section(target)
        anchor: int | None = None

        if key is None:
            lines, problem = self._resolve(state, table, target)
            if problem:
                return False, (
                    f"Failed: {problem} To add a line, give the number of the "
                    f"position you want it to take, or a heading to put it at the "
                    f"end of."
                )
            key, anchor = lines[0]

        if plan.counts[key] >= self.cfg.max_lines_per_section:
            return False, (
                f"Failed: that part of the prompt is full at {plan.counts[key]} "
                f"lines. Remove one there before adding another."
            )

        content, problem = self._prepare(plan, key, text)
        if content is None:
            return False, f"Failed: {problem}"

        if anchor is None:
            plan.appended.setdefault(key, []).append(content)
        else:
            plan.inserted.setdefault((key, anchor), []).append(content)
        plan.counts[key] += 1
        plan.texts[key].add(_match_key(content))
        plan.log.append(f"added {content!r}")
        return True, f"Added: {_short(content)}"

    def _prepare(self, plan: _Plan, key: str, text: str,
                 ignoring: frozenset[str] | set[str] = frozenset()) -> tuple[str | None, str]:
        # Validate shape, length and duplicates; `ignoring` holds lines this edit consumes
        section = self.editable[key]
        content, problem = normalise(text or "", section)
        if content is None:
            return None, problem

        if len(content) > self.cfg.max_line_length:
            return None, (
                f"that line is {len(content)} characters and the limit is "
                f"{self.cfg.max_line_length}; say it more briefly."
            )

        wanted = _match_key(content)
        if wanted in plan.texts[key] - set(ignoring):
            return None, f"it already says {_short(content)}."
        return content, ""


    # ------------------------------------------------------------------
    #  Recovery
    # ------------------------------------------------------------------

    def reset(self) -> list[Path]:
        # Back up and remove every self-written persona so it re-seeds from persona.txt
        removed: list[Path] = []
        if not self.directory.is_dir():
            return removed
        for path in sorted(self.directory.glob("persona_*.json")):
            self._backup(path)
            path.unlink(missing_ok=True)
            removed.append(path)
        return removed
