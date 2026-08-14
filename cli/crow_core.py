#!/usr/bin/env python3
"""Everything Crow does that never touches a terminal.

Split out of cli/crow.py, unchanged, so that a second client can call the same
code instead of carrying a second copy of it. Nothing in here writes to stdout,
reads a keystroke, or emits an escape sequence -- the two exceptions are named
where they sit (install_font's `verbose` arm, which no caller in this repository
sets, and the ANSI colour constants, which are STRINGS the caller may or may not
print).

TWO BLOCKS CAME OVER A SEAM RATHER THAN AS THEY STOOD, and they are the only
two. `stream_reply` had thirteen terminal lines -- two signature parameters and
eleven statements -- so it could not simply move. They are `ReplyEvents` now,
four named events the caller decides what to do with, and cli/crow.py's
`TerminalEvents` still carries those eleven statements verbatim. The rest of
the function moved line for line.

The tool loop out of `repl()` is the second: twelve terminal lines, eleven
named events on `TurnEvents`, and cli/crow.py's `TerminalTurnEvents` carrying
the twelve prints unchanged. `run_turn` is the rest of that loop, moved line
for line -- including `_SEEN.clear()`, which was the one line of it that could
have been left behind without anything failing today.

WHY THE MODULE IS CALLED crow_core AND NOT core. `python <abs>\\cli\\crow.py`
puts the script's own directory on sys.path[0], so a sibling module resolves by
its bare name. A file called cli/queue.py or cli/json.py would shadow the
standard library for EVERY client that starts from this directory -- and
_post_stream imports `queue` and `json` from it. A prefix costs nothing and
removes the whole class.

WHY IT LIVES IN cli/. tools/pack-release.ps1:310 is the only place that copies a
repository directory into the package as a whole:

    Copy-Item -LiteralPath (Join-Path $repo 'cli') ... -Recurse

A new top-level directory would never be staged, and the package would be
missing this file with nothing saying so -- the failure would first appear on a
user's machine, at import time. Here it costs no change to the shipping path.

THE VERSION IS NOT HERE, and that is a hard rule rather than a preference. It
lives in cli/crow.py and only there. install.ps1:399-403 reads the installed
version out of the shipped cli\\crow.py with ^VERSION\\s*=\\s*"([^"]+)"; with the
literal it reads 0.2.0, with an import it reads nothing, Get-InstalledVersion
returns $null, and Resolve-InstallAction (install.ps1:428-431) answers 'unknown'
and refuses with the advice to pass -Force -- which a run through
`irm ... | iex` cannot do. Every installed base would become un-updatable
through the documented one-liner. See CLIENT_VERSION below for the way round it.

Standard library only, same as the client.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

# The client's version, handed over by whoever owns the literal.
#
# NOT DEFINED HERE -- see the module docstring: `VERSION = "0.2.0"` may only
# appear in cli/crow.py, because install.ps1 greps that file for it. So the
# owner assigns this on import (`crow_core.CLIENT_VERSION = VERSION`) and the
# three places that need a version read it from here.
#
# The empty default is the safe one rather than a placeholder: parse_version("")
# is None, is_newer() is False whenever either side does not parse, and
# update_notice therefore says nothing at all. A client that forgot to hand its
# version over stays quiet instead of announcing an update to everybody.
CLIENT_VERSION = ""


DEFAULT_BASE_URL = "http://127.0.0.1:8081/v1"
DEFAULT_MODEL = "crow"


# Without a system prompt the model picks its own language -- measured: "yo"
# came back in Chinese. Kept to one short line on purpose: it sits at the head
# of every context, so it is paid for in prefill exactly once and then cached,
# but only while it stays byte-identical.
# NO WORKING DIRECTORY IN HERE, deliberately. It would make the system prompt
# differ between two starts in different folders, and a system prompt is byte 0
# of the prefix: a session saved in one directory would then be worthless when
# resumed from another. `list_dir` with no argument answers the same question
# and costs one round only when the model actually needs it.
DEFAULT_SYSTEM = (
    "You are Crow, a local coding assistant. You have tools to read, write, "
    "search and run commands -- look instead of guessing paths. "
    "Always reply in the same language the user wrote in."
)


# Sent with every request, and the reason is the prompt cache rather than the
# tool. The model's chat template keeps a previous turn's thoughts only while
# `tools` is non-empty; with an empty array it drops them and a replayed
# `reasoning_content` renders to nothing. Measured 2026-08-08 via the server's
# own /apply-template: without tools both variants come out at 132 characters,
# with tools at 1197 against 1215 -- and those 18 characters ARE the thoughts.
# 100 KB was the first value and it was chosen against context growth alone.
# Prefill is the cost that matters here: at ~38 tok/s a 100 KB file is ~25,000
# tokens and eleven minutes before the model has read a word of it. 16 KB is
# roughly 4,000 tokens, under two minutes, and a file larger than that is meant
# to be reached through search_text plus a line range.
MAX_TOOL_BYTES = 16_000
MAX_TOOL_ROUNDS = 24
MAX_HITS = 200
COMMAND_TIMEOUT = 120

# Archive the conversation and start a fresh one at this share of the window.
#
# The server's limit is a wall, not a slope: a request that arrives already at
# or past n_ctx is refused outright with ERROR_TYPE_EXCEED_CONTEXT_SIZE, and
# the turn is lost with it. Rolling over before that is the difference between
# an archive on disk and a message the user has to retype.
#
# 0.9 leaves ~20k of a 200k window. That is head-room for ONE more turn, not a
# guarantee: a single tool round has been measured adding 5,253 tokens, and the
# loop runs up to MAX_TOOL_ROUNDS of them. Which is why the check also runs
# inside the loop and not only between turns.
ROLLOVER_AT = 0.9


# THE SAMPLING TRIPLE, AND IT IS WRITTEN HERE OR NOWHERE.
#
# manifests/operating-point.json declares the values and
# tools/check_operating_point.py COUNTS them rather than comparing them, for the
# reason it states at length: two clients that both hard-write 0.95 agree with
# the manifest and with each other right up to the day one of them is edited. So
# the rule is "exactly once, across every client source, and that once in the
# core", and the day a SECOND surface landed is the day the rule stopped being
# theoretical. Everything below reads these three names; nothing writes the
# numbers again.
#
# The reasons moved with the values, out of cli/crow.py's parser:
#
# 1.0 is what DeepSeek-V4-Flash-0731 specifies: the model card runs its agentic
# benchmarks at temperature 1.0 / top_p 0.95, and its generation_config.json says
# temperature 1.0 too. (0.6 was the PREVIEW family's value; it shipped in every
# release up to 0.0.6.) 0.0 stays dangerous either way: greedy is where reasoning
# models loop -- measured 2026-08-07 on a three.js task, the model repeated
# "Actually, let me..." inside its reasoning block and never reached the answer.
# Measurement runs that need byte-identical output pass --temperature 0
# explicitly; the interactive default has to be able to finish a turn.
TEMPERATURE = 1.0

# The card and the generation_config disagree here: the card's agentic runs use
# 0.95, generation_config.json says 1.0. Crow is an agent, so the agentic figure
# wins -- but the disagreement is real and belongs next to the number rather than
# in anyone's memory.
TOP_P = 0.95

# unsloth's docs recommend 0.01 for this model; DeepSeek's own card is silent;
# llama.cpp's server default is 0.05. Sent explicitly, because not sending it
# means inheriting a third value nobody chose.
MIN_P = 0.01


# The calls are executed -- see run_tool and the loop in repl().
#
# WHY LIST_DIR, FIND_FILES AND SEARCH_TEXT ARE NOT OPTIONAL. With read_file
# alone the model has to guess paths, and it does: asked about llama.cpp it
# tried "llama.cpp/server.cpp", which does not exist here. Every guess costs a
# full round at ~10 tok/s. Tools that let it look are cheaper than tools that
# let it read.
def _fn(name, description, properties, required):
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": {"type": "object", "properties": properties,
                                        "required": required}}}


_STR = {"type": "string"}

TOOLS = [
    _fn("read_file",
        "Read a UTF-8 text file. Give start_line and end_line for a range -- everything "
        "read costs prefill time, so read the part you need, not the whole file. "
        "search_text returns line numbers for exactly this.",
        {"path": dict(_STR, description="Path to the file."),
         "start_line": {"type": "integer", "description": "First line, 1-based."},
         "end_line": {"type": "integer", "description": "Last line, inclusive."}}, ["path"]),
    _fn("write_file",
        "Write a file, creating directories as needed. An existing file must have been "
        "read first in this session; otherwise the call is refused.",
        {"path": dict(_STR, description="Path to write."),
         "content": dict(_STR, description="Full new contents.")}, ["path", "content"]),
    _fn("edit_file",
        "Replace one exact occurrence of 'old' with 'new'. The file must have been read "
        "first. Fails if 'old' is absent or appears more than once.",
        {"path": dict(_STR, description="File to edit."),
         "old": dict(_STR, description="Exact text to replace, unique in the file."),
         "new": dict(_STR, description="Replacement text.")}, ["path", "old", "new"]),
    _fn("list_dir", "List the entries of a directory.",
        {"path": dict(_STR, description="Directory, default the working directory.")}, []),
    _fn("find_files", "Find files by name pattern, recursively.",
        {"root": dict(_STR, description="Where to start."),
         "pattern": dict(_STR, description="Glob on the filename, e.g. *.cpp")}, ["pattern"]),
    _fn("search_text", "Search file contents by regular expression, recursively.",
        {"root": dict(_STR, description="Where to start."),
         "pattern": dict(_STR, description="Regular expression."),
         "glob": dict(_STR, description="Only files matching this glob, e.g. *.py")}, ["pattern"]),
    _fn("run_command",
        f"Run a shell command locally and return its exit code and output. "
        f"Killed after {COMMAND_TIMEOUT}s.",
        {"command": dict(_STR, description="The command line."),
         "cwd": dict(_STR, description="Working directory.")}, ["command"]),
]


# ANSI only, and only when stdout is a terminal: a redirected transcript has
# to stay free of escape sequences, or every later grep over it is wrong.
# No 256-colour or truecolour codes - the eight basic ones survive every
# Windows console and every theme, bright-on-dark as well as dark-on-light.
_TTY = bool(getattr(sys.stdout, "isatty", lambda: False)())


def _c(code: str) -> str:
    return code if _TTY else ""


DIM = _c("\033[2m")
RESET_DIM = _c("\033[22m")
RESET = _c("\033[0m")
BOLD = _c("\033[1m")
CYAN = _c("\033[36m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
MAGENTA = _c("\033[35m")
BLUE = _c("\033[34m")
RED = _c("\033[31m")

# The two colours the product is recognised by. Truecolour, unlike the palette
# above: these are brand values, and mapping them onto one of the eight basic
# slots would hand them to the user's theme, where "blue" is whatever they set.
# Terminals without truecolour ignore the sequence and fall back to their own
# foreground - readable either way, just not ours.
CROW_BG = "#0b0e17"                     # window background, set via OSC 11
CROW_ACCENT = _c("\033[38;2;126;176;248m")   # #7eb0f8, the blue of the wordmark
CROW_TEXT = _c("\033[38;2;255;255;255m")     # what the model says stays white
BANNER_BEVEL = _c("\033[38;2;44;91;172m")    # #2c5bac, the wordmark's shaded edge

# THE SAME THREE VALUES IN THE SPELLING A WINDOW CAN USE, and they are here for
# the reason manifests/shared-core.json gives for CROW_BG: a brand value written
# twice is a brand value that gets corrected once. A terminal takes them as
# escape sequences and Tk takes "#rrggbb"; neither can read the other's form,
# and the escapes above are additionally _c()-gated, so under a window every one
# of them is the empty string. CROW_BG needs no twin -- it is already the hex,
# because the terminal sets it through OSC 11 rather than SGR.
CROW_ACCENT_HEX = "#7eb0f8"
CROW_TEXT_HEX = "#ffffff"
BANNER_BEVEL_HEX = "#2c5bac"


# THE THREE SENTENCES A SURFACE MAY NOT SPELL ITSELF. Each one is the only place
# the user learns something the screen does not otherwise show: that a budget cut
# the turn short, that an abort left the context untouched, that a resume brought
# the messages but not the cache. A second surface writing its own wording is a
# second product -- so the wording lives here and both surfaces print this name.
# manifests/shared-core.json holds all three under `wordings`; checking is what
# `tools/check_shared_core.py` does, and it counts the literal, not the intent.
CUT_OFF_NOTE = "CUT OFF at the token budget"
ABORT_NOTE = "[interrupted -- turn discarded, context unchanged]"
RESUME_COLD_NOTE = "messages only -- the first turn pays a prefill"


# Keywords worth colouring, kept to the three languages this assistant writes
# most. A language it does not know renders as plain text rather than wrongly
# highlighted - a false colour is worse than none, because it reads as meaning.
_KEYWORDS = {
    "python": frozenset("""
        def class return if elif else for while in not and or is None True False
        import from as with try except finally raise lambda yield global nonlocal
        pass break continue assert del await async self
    """.split()),
    "javascript": frozenset("""
        function return if else for while in of not const let var new class extends
        import export from default async await try catch finally throw typeof
        instanceof null undefined true false this super yield delete void
    """.split()),
    "json": frozenset(["true", "false", "null"]),
}
_KEYWORDS["py"] = _KEYWORDS["python"]
_KEYWORDS["js"] = _KEYWORDS["javascript"]
_KEYWORDS["ts"] = _KEYWORDS["javascript"]
_KEYWORDS["typescript"] = _KEYWORDS["javascript"]

# One pass, alternatives ordered so that the longest wins: a string containing
# a keyword must stay a string. Comments come first for the same reason.
_TOKENS = re.compile(
    r"(?P<comment>#[^\n]*|//[^\n]*)"
    r"|(?P<string>\"\"\".*?\"\"\"|'''.*?'''|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
    r"|(?P<number>\b\d+\.?\d*\b)"
    r"|(?P<word>\b[A-Za-z_]\w*\b)",
    re.DOTALL,
)


_EXT = {"python": "py", "py": "py", "javascript": "js", "js": "js", "ts": "ts",
        "typescript": "ts", "json": "json", "html": "html", "css": "css",
        "bash": "sh", "sh": "sh", "powershell": "ps1", "sql": "sql"}


# The repository, spelled once. Three hosts quote the same slug -- raw content
# for the installer, the API for the release check, and the page a human opens
# from the header. Three literals are three chances for one of them to go stale
# after a rename, and the one that breaks is the one nobody runs in a test.
REPO = "nibor1896/Crow"
REPO_URL = f"https://github.com/{REPO}"


# The command that updates an installation. It is the SAME line that installs
# one: install.ps1 reads the version out of the cli\crow.py it finds in the
# target and updates when its own is newer. Until 2026-08-08 that line refused a
# non-empty target outright, so there was no route from one version to the next
# short of deleting the directory by hand.
UPDATE_COMMAND = f"irm https://raw.githubusercontent.com/{REPO}/main/install.ps1 | iex"

RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"


class CrowError(RuntimeError):
    """Raised when the endpoint cannot be reached or answers with an error."""


# Where a session is kept between runs. The messages live here; the KV state
# lives wherever the server's --slot-save-path points, because only the server
# can write it.
SESSION_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                           "Crow", "session")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")
SLOT_FILE = "crow-session.bin"


# THE SESSION FILE'S FORMAT VERSION, AND IT IS NOT THE CLIENT'S.
#
# `"version": CLIENT_VERSION` has been written into every session file since
# 0.2.0 and read by NOBODY: every read on the load path is
# saved.get("messages"|"kv"|"prefix"|"kv_tokens"|"context_tokens"). A value that
# nothing compares is not a gate -- and turning THAT field into one would be
# worse than having no gate at all. It holds "0.2.0", so the first build that
# expected a format number there would refuse every session file already on
# disk and take the history of every existing installation with it. That is the
# damage the removal path was built against, arriving through the one door the
# removal path is right never to touch: %LOCALAPPDATA%\Crow\session is user
# data, not a packaged file, and no MANIFEST reaches it.
#
# So the gate reads a key of its own. Every file written before this build lacks
# that key, which is exactly the case the gate has to wave through: those files
# are not a foreign format, they are THIS one, written by a build that stamped
# nothing.
#
# A STRING RATHER THAN A NUMBER, and that is not a matter of taste: `1 == True`
# in Python, so an integer stamp would accept `{"format_version": true}` as this
# build's own work.
SESSION_FORMAT_KEY = "format_version"
SESSION_FORMAT = "1"


class SessionFormatError(CrowError):
    """A session file this build must neither read nor overwrite.

    A CLASS RATHER THAN A None RETURN. `None` out of load_session already means
    "there is nothing here to resume", and the difference between that and
    "your history is right there and this build will not touch it" is the whole
    of what the user has to be told. A silent None would send somebody looking
    for a lost session; this sends them to the file that still holds it.
    """

    def __init__(self, path: str, problem: str) -> None:
        super().__init__(f"{path}: {problem} -- the file was left untouched")
        self.path = path
        self.problem = problem


def session_format_problem(saved: object) -> str | None:
    """None when this build may read AND rewrite `saved`, else why it may not.

    THREE CLASSES, AND THE MIDDLE ONE IS THE POINT OF THE WHOLE GATE:

      * the stamp this build writes -- ours, read it;
      * NO stamp at all -- every session file written before the gate existed,
        which is every one on every installation out there today. Accepted, and
        stamped by the next save. Refusing these would take the history off
        every existing user on the day they update, and a version gate that does
        that is a worse defect than the drift it prevents;
      * any other stamp -- a format this build does not know. Refused, and the
        file is left exactly as it was.

    A file that is not a JSON object has no stamp to read and gets None here:
    the reader below fails on it and returns "no session", which is what it did
    before this gate existed. Unreadable is not a foreign format, and refusing
    to overwrite garbage would strand a user with a corrupt file forever.
    """
    if not isinstance(saved, dict):
        return None
    stamp = saved.get(SESSION_FORMAT_KEY)
    if stamp is None or stamp == SESSION_FORMAT:
        return None
    return f"session format {stamp!r}, and this build reads {SESSION_FORMAT!r}"


def session_file_problem(path: str) -> str | None:
    """The same rule, asked of a file on disk. None when there is nothing wrong.

    Used by save_session BEFORE it writes either half. A gate that only sat on
    the read path would refuse to READ a stranger's file and then overwrite it
    on the way out, which is the same data loss with an extra step in front of
    it.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
    except Exception:
        return None
    return session_format_problem(saved)


def prefix_fingerprint(system: str | None) -> str:
    """What the saved KV state is only valid for.

    The chat template renders the tool declarations and the system prompt at the
    HEAD of the prompt. Change either and byte 0 differs, so a restored KV cache
    matches nothing and the server re-reads the whole conversation -- measured
    2026-08-09, adding two parameters to read_file turned a resumed 73k session
    into a full re-prefill.

    Cheaper to detect than to suffer: if this does not match, the messages are
    still restored and only the KV is dropped.
    """
    import hashlib

    material = json.dumps(TOOLS, sort_keys=True) + "\x00" + (system or "")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def write_transcript(conversation: "Conversation", path: str) -> int:
    """The archive as plain text, for whoever has to read it back.

    THE JSON BESIDE THIS CANNOT BE READ BY THE MODEL THAT IS POINTED AT IT.
    json.dump writes one line; a rollover archive measured 104,618 bytes on that
    single line, and read_file caps at MAX_TOOL_BYTES. So the model sees the
    first 15 % of it, cut mid-field into invalid JSON, from the OLDEST end --
    the part that helps least when the question is "where was I".

    This file has lines, so a range works, and it leaves `reasoning_content`
    out: that is the bulk of the bytes and none of the recall.

    Returns the line count, which goes into the note so the reader can jump to
    the end rather than start at the beginning.
    """
    out = []
    for message in conversation.payload():
        role = message.get("role", "?")
        body = (message.get("content") or "").strip()
        out.append(f"## {role}")
        if body:
            out.append(body)
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            out.append(f"[tool call] {fn.get('name')}({fn.get('arguments')})")
        out.append("")
    text = "\n".join(out)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text.count("\n") + 1


def recent_paths(conversation: "Conversation", limit: int = 4) -> list[str]:
    """The files and directories the archived conversation had reached.

    A pointer tells the model where the past IS. It does not tell it where the
    past had got TO, and that is the part it needs first. Measured 2026-08-10 on
    a live rollover: the model guessed two directories that do not exist and
    scanned a whole user profile before it read the archive at all.

    Newest last, deduplicated, because the last few are the ones in play.
    """
    seen: list[str] = []
    for message in conversation.payload():
        for call in message.get("tool_calls") or []:
            raw = (call.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(args, dict):
                continue
            for key in ("path", "root"):
                value = args.get(key)
                if isinstance(value, str) and value:
                    if value in seen:
                        seen.remove(value)
                    seen.append(value)
    return seen[-limit:]


def save_session(conversation: "Conversation", base_url: str, context_tokens: int,
                 path: str | None = None, with_kv: bool = True,
                 pretty: bool = False) -> str | None:
    """Write the session so the next start does not pay for it again.

    TWO HALVES, AND NEITHER IS ENOUGH ALONE. The server holds the KV cache; the
    client holds the messages that produced it. Restoring only the KV would have
    the client send an empty history against a full cache -- the prefix would not
    match and the whole thing would be re-read anyway. Restoring only the
    messages costs a full prefill. Both, or nothing.

    ON EXIT, NOT PER TURN, on robin's call 2026-08-08: a save is ~17 MiB plus
    ~6.9 KiB per token -- about 1.3 GiB at a full 200k window -- and this is the
    one place Crow writes to the SSD rather than reading it. Per turn that
    accumulates; once per session it does not.

    WITH_KV=FALSE IS FOR ARCHIVES, AND IT IS NOT AN OPTIMISATION. SLOT_FILE is
    a fixed name on the server: a second save would write the archive's cache
    over the live one, so the session the user is still in would resume against
    a stranger's prefix. On top of that a save is the ~1.3 GiB figure above, to
    the same SSD the experts are being streamed from. An archive is resumable
    from its messages; it pays a prefill and nothing else is at risk.

    THE STAMP GOES IN HERE AND THE GATE SITS IN FRONT OF IT. A file whose
    format this build does not know is not written over: SessionFormatError is
    raised and nothing on disk or on the server changes. That is the second half
    of the promise -- a gate that only guards the read path refuses to read a
    stranger's file and then flattens it on the way out.

    Returns a one-line report, or None when there was nothing worth saving.
    Raises SessionFormatError rather than overwriting a session file this build
    cannot read.
    """
    if len(conversation) <= (1 if conversation.has_system else 0):
        return None

    path = path or SESSION_FILE
    # BEFORE THE FIRST WRITE OF EITHER HALF, and the server's slot save below is
    # the half that is easy to forget: SLOT_FILE is one fixed name, so a check
    # placed after it would already have overwritten the cache belonging to the
    # very file it is about to refuse.
    problem = session_file_problem(path)
    if problem is not None:
        raise SessionFormatError(path, problem)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    saved_kv = False
    kv_tokens = 0
    if with_kv:
        try:
            reply = post_json(f"{base_url.rstrip('/').removesuffix('/v1')}/slots/0?action=save",
                              {"filename": SLOT_FILE}, timeout=600.0)
            saved_kv = True
            # The server says how many tokens it wrote. Kept so the restore has
            # something to check itself against -- see load_session.
            kv_tokens = int((reply or {}).get("n_saved") or 0)
        except Exception:
            # A server without --slot-save-path refuses this. The messages are still
            # worth keeping; the next start just pays a prefill for them.
            pass

    with open(path, "w", encoding="utf-8") as fh:
        # SESSION_FORMAT_KEY is ADDED, and `version` keeps the meaning it has
        # had since 0.2.0 -- the client that wrote the file. Renaming or
        # re-purposing an existing key would break the way back: an older build
        # reads these five keys and ignores everything else, so an extra key
        # costs it nothing and a changed one would cost it the session.
        json.dump({SESSION_FORMAT_KEY: SESSION_FORMAT,
                   "version": CLIENT_VERSION, "kv": saved_kv, "kv_tokens": kv_tokens,
                   "context_tokens": context_tokens,
                   "prefix": prefix_fingerprint(conversation.system),
                   "messages": conversation.payload()},
                  fh, indent=1 if pretty else None)

    if saved_kv:
        return f"session saved -- {len(conversation)} messages and the server's cache"
    # Two different reasons for the same shape, and saying the wrong one sends
    # the reader to the wrong fix: an archive skipped the cache on purpose, a
    # live session only ever skips it because the server refused.
    why = ("archive: messages only, so the live cache is left alone" if not with_kv else
           "messages only: the server runs without --slot-save-path, so the next"
           " start pays a prefill")
    return f"session saved -- {len(conversation)} messages ({why})"


def load_session(base_url: str, system: str | None = None,
                 path: str | None = None) -> tuple[list[dict], int, bool] | None:
    """The other half. Returns (messages, context_tokens, kv_restored) or None.

    The KV restore is attempted first and its success is carried out, because a
    caller that believes the cache is warm when it is not will report a prefill
    as a surprise rather than as the expected cost.

    A path names an archive written by a rollover. Those carry `kv: false` by
    construction, so this resumes their messages and pays a prefill for them --
    which is the honest price of picking up a conversation that was put down.

    THE FORMAT GATE SITS BEFORE EVERY WRITE ON THIS PATH, and this function has
    one: when a promised cache turns out to be gone the file is rewritten with
    the claim withdrawn (`if not kv:` below). A gate placed after that has
    already changed the stranger's file before refusing it, which is the
    "checked after the write instead of before it" shape. Raises
    SessionFormatError for a format this build does not know.
    """
    path = path or SESSION_FILE
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        # COMPUTED HERE, RAISED BELOW. This `except Exception` swallows
        # everything, so a refusal raised inside it would come back out as
        # "there is no session" -- the invisible answer the gate exists to
        # replace.
        problem = session_format_problem(saved)
        messages = saved.get("messages") or []
    except Exception:
        return None
    if problem is not None:
        raise SessionFormatError(path, problem)
    if not messages:
        return None

    # The KV is only restored when the head of the prompt is byte-identical to
    # what produced it. Restoring it against changed tools would not fail -- it
    # would succeed and then re-read everything, which costs minutes and looks
    # like the server misbehaving.
    kv = False
    if saved.get("kv") and saved.get("prefix") == prefix_fingerprint(system):
        try:
            reply = post_json(f"{base_url.rstrip('/').removesuffix('/v1')}/slots/0?action=restore",
                              {"filename": SLOT_FILE}, timeout=600.0)
            # A 200 SAYS THE FILE WAS READ, NOT THAT THE CACHE IS THE ONE WE
            # SAVED. The server reports how many tokens went back into the slot;
            # if that is not the number that came out, the slot holds something
            # else and the promise of a warm cache is already false here.
            # Measured 2026-08-10: "resumed: 36 messages, cache warm" was
            # followed by `cached 0/21004` and 469.51 s to the first token.
            #
            # SILENCE IS NOT A CONTRADICTION. An endpoint that does not report
            # n_restored has told us nothing, and treating that as a failure
            # would refuse a perfectly good cache on every server but this one.
            # The claim is only withdrawn when the server states a number and
            # the number disagrees.
            restored = (reply or {}).get("n_restored")
            expected = int(saved.get("kv_tokens") or 0)
            if restored is None:
                kv = True
            else:
                kv = int(restored) > 0 and (expected == 0 or int(restored) == expected)
        except Exception:
            kv = False

        # Outside the except on purpose. The claim is now known to be false, so it is withdrawn --
        # and a restore that came back with the WRONG token count is just as false as one that
        # raised, while raising no exception at all. Until 2026-08-10 only the raising half was
        # withdrawn, which is how a file kept promising a cache the server did not have.
        #
        # Without this the same request goes out on every start and the server prints the same two
        # red lines every time -- measured 2026-08-09 after the server was pointed at a different
        # --slot-save-path than the one the state was written to.
        #
        # The messages are NOT dropped: they are still worth a prefill. Only the promise of a
        # warm cache goes.
        #
        # A failure to rewrite is swallowed on purpose. This runs on the path that resumes a
        # session; refusing to start because a cache hint could not be corrected would turn a
        # cosmetic defect into a broken client.
        if not kv:
            try:
                saved["kv"] = False
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(saved, fh)
            except Exception:
                pass
    return messages, int(saved.get("context_tokens") or 0), kv


def should_roll(context_tokens: int, n_ctx: int, at: float = ROLLOVER_AT) -> bool:
    """Whether the window is full enough to archive and start fresh.

    n_ctx OF ZERO MEANS "THE SERVER WOULD NOT SAY", NOT "NO ROOM LEFT".
    fetch_n_ctx returns 0 on any failure, and `context_tokens >= 0 * at` is
    true on every turn including the first -- without this guard the client
    would archive and reset continuously, on exactly the path where something
    is already wrong. A threshold of zero is how the feature is switched off,
    and it has to mean off rather than always.
    """
    if n_ctx <= 0 or at <= 0:
        return False
    return context_tokens >= n_ctx * at


def rollover_path(stamp: str | None = None) -> str:
    """Where an archive goes. Named by time, because there will be several."""
    return os.path.join(SESSION_DIR, f"rollover-{stamp or time.strftime('%Y%m%d-%H%M%S')}.json")


def resume_path(name: str) -> str:
    """Resolve --resume: a bare name is looked for among the archives."""
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        return name
    return os.path.join(SESSION_DIR, name)


# What the fresh conversation opens with. It names the archive rather than
# summarising it: a summary is a guess about what mattered, and the model has
# read_file. A pointer it can follow beats a précis it cannot check.
#
# THREE THINGS THE FIRST VERSION GOT WRONG, all measured on a live rollover on
# 2026-08-10. It pointed at the JSON, which is one 104,618-byte line that
# read_file can only ever show the first 15 % of. It gave no line count, so
# there was no way to ask for the END, which is the part that matters. And it
# said nothing about where the work had got to, so the model guessed two
# directories that do not exist and scanned a user profile before it read
# anything at all.
# What the turn is told when its tool budget runs out. A user turn rather than a
# system one: the system prompt is byte 0 of the prefix, and editing it mid-turn
# would re-read the whole conversation to say one sentence.
BUDGET_SPENT = (
    "[The tool budget for this turn is spent -- no further calls will be run. "
    "Answer now, and report ONLY what is actually present in this conversation as "
    "a tool result. If you ran nothing, say you ran nothing -- do not describe "
    "what a file probably contains. Then name what you did not get to, and what "
    "the next step would be. Do not ask for another tool.]"
)
# The middle two sentences were added after the first live run, on 2026-08-10.
# Asked to summarise with an empty tool budget, the model reported having read a
# line it had never read and described the contents of one that is blank. Asking
# for "what you found" invites a model with nothing to find to invent something;
# naming the failure is cheaper than hoping.
#
# Whether it works is UNMEASURED, and no test here can settle it: this is a
# prompt, and only a live run against a real model shows whether it holds. What
# the suite checks is that it reaches the conversation at all.


ROLLOVER_NOTE = (
    "[The conversation up to this point reached {tokens} tokens and was archived.\n"
    "Transcript: {transcript} -- {lines} lines, oldest first, so read the END of it "
    "for where things stood.\n"
    "{where}"
    "Full record, for `crow --resume`: {path}\n"
    "This conversation starts here.]"
)


def roll_over(conversation: "Conversation", base_url: str, context_tokens: int,
              carry: str | None = None, path: str | None = None) -> str | None:
    """Archive the conversation, empty it, and open the fresh one.

    Append-only is not broken here and this is the reason it is allowed: nothing
    is edited or removed from a prefix that is still in use. The old context is
    written out whole and then dropped whole, and what follows is a new prefix
    that has never been sent. An edit would leave the server's cache matching a
    conversation that no longer exists; a reset leaves it matching nothing,
    which the next request pays for once and honestly.

    `carry` is the turn the user had just typed. Without it a rollover in the
    middle of a turn would archive the question along with everything else and
    leave the model answering a note about a file.

    Returns the archive path, or None when there was nothing worth archiving.
    """
    path = path or rollover_path()
    if save_session(conversation, base_url, context_tokens, path=path,
                    with_kv=False, pretty=True) is None:
        return None

    # Both before the reset: afterwards there is nothing left to read them from.
    transcript = path[:-5] + ".md" if path.endswith(".json") else path + ".md"
    lines = write_transcript(conversation, transcript)
    where = recent_paths(conversation)

    conversation.reset()
    note = ROLLOVER_NOTE.format(
        tokens=context_tokens, path=path, transcript=transcript, lines=lines,
        where=f"Last worked on: {', '.join(where)}\n" if where else "")
    # ONE message, not two. Consecutive turns of the same role are merged or
    # rejected depending on the chat template, and neither is a thing to find
    # out at 180k tokens.
    conversation.append("user", f"{note}\n\n{carry}" if carry else note)
    return path


def post_json(url: str, body: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def parse_version(text: str) -> tuple[int, ...] | None:
    """"0.0.5" -> (0, 0, 5). None when it is not a plain dotted number.

    None rather than a zero tuple on purpose: an unparseable string read as
    (0,0,0) would compare as older than everything and announce an update on
    every start.
    """
    parts = (text or "").strip().lstrip("vV").split(".")
    if not parts or len(parts) > 4:
        return None
    out = []
    for part in parts:
        if not part.isdigit():
            return None
        out.append(int(part))
    return tuple(out)


def is_newer(candidate: str, current: str) -> bool:
    """Is `candidate` a strictly higher version than `current`?

    False whenever either side does not parse. A version check that cannot read
    one of its two inputs has nothing to say, and saying it anyway would put a
    permanent "update available" line in front of a user who is already current.
    """
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def fetch_latest_version(timeout: float = 4.0) -> str | None:
    """The newest published release tag, or None if that cannot be learnt.

    Every failure is None: no network, GitHub down, rate limit, a shape we do
    not recognise. This runs on every start, so it is never allowed to print an
    error or raise -- a broken update check must not stand between the user and
    their prompt.
    """
    try:
        req = urllib.request.Request(RELEASES_API, headers={
            "User-Agent": f"crow/{CLIENT_VERSION}",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tag = (json.loads(resp.read().decode("utf-8")) or {}).get("tag_name")
        return tag.lstrip("vV") if isinstance(tag, str) and tag.strip() else None
    except Exception:
        return None


def start_update_check(enabled: bool) -> "queue.Queue | None":
    """Ask GitHub in the background. Returns the queue the answer will arrive in.

    Started BEFORE the banner is drawn and read after it, so the network call
    overlaps the work the CLI has to do anyway (banner, font, /health). The
    thread is a daemon and nothing ever joins it: if the answer is late, the
    line is skipped rather than the start delayed.
    """
    import queue

    if not enabled:
        return None
    answers: "queue.Queue" = queue.Queue(maxsize=1)

    def _ask() -> None:
        try:
            answers.put(fetch_latest_version(), block=False)
        except Exception:
            pass

    threading.Thread(target=_ask, daemon=True).start()
    return answers


def update_notice(answers: "queue.Queue | None", wait: float = 1.5) -> str | None:
    """The line to print, or None when there is nothing to say.

    `wait` is the entire budget the check may cost a start. It is spent only
    when the request is still in flight after the banner and the font; on a
    machine with no network it is spent once and never blocks a turn.
    """
    if answers is None:
        return None
    try:
        latest = answers.get(timeout=wait)
    except Exception:
        return None
    if not latest or not is_newer(latest, CLIENT_VERSION):
        return None
    return (f"{BOLD}crow {latest} is out{RESET} {DIM}(you have {CLIENT_VERSION}){RESET}\n"
            f"  {UPDATE_COMMAND}")


# Ctrl+C, the belt-and-braces version.
#
# Relying on KeyboardInterrupt to arrive where it is caught did not hold up in
# practice: on Windows the signal is delivered by a separate thread that only
# sets a flag, and the main thread acts on it at the next bytecode boundary -
# which is fine in a tight loop and useless when it sits in a C-level call.
# Two earlier attempts (reader thread, then polling instead of a timed get)
# each fixed one such call and left the next one.
#
# So the handler also sets an Event, and every loop that can run long checks
# it. That works no matter which C call the interrupt landed in, because the
# loop comes back around either way.
INTERRUPT = threading.Event()


def _on_sigint(signum, frame) -> None:
    INTERRUPT.set()
    raise KeyboardInterrupt


def install_interrupt_handler() -> None:
    """Idempotent; a no-op where signals are not available (threads, IDEs)."""
    try:
        import signal
        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:
        pass


class Conversation:
    """The message list. Append-only by construction -- see module docstring.

    There is deliberately no method to edit or remove a message. The only
    way to shrink the context is `reset()`, which drops the whole thing and
    is understood to cost a full re-prefill.

    AN ASSISTANT TURN CARRIES ITS REASONING. The model's template renders a
    kept turn as `<think>...</think>`; omitting the field leaves an EMPTY
    think block, so the prefix diverges where the thoughts began and the whole
    tail behind it is re-read. Measured 2026-08-08 at the operating point with
    --jinja: over ten-turn sessions the omission costs the size of the
    PREVIOUS turn's output on every single turn -- 55.0 s against 33.3 s of
    total prefill on short answers, and 242.3 s against 1.6 s on one turn that
    had generated 2046 tokens. It does not accumulate; it repeats.
    """

    def __init__(self, system: str | None = None) -> None:
        self._system = system
        self._messages: list[dict[str, str]] = []
        if system:
            self._messages.append({"role": "system", "content": system})

    @property
    def has_system(self) -> bool:
        return bool(self._system)

    @property
    def system(self) -> str | None:
        return self._system

    def restore(self, messages: list[dict]) -> None:
        """Adopt a saved history wholesale, at construction time only.

        This is not an exception to append-only: it happens before the first
        request of a session, so no prefix exists yet to break. Calling it
        mid-session would be exactly the edit this class refuses to allow.
        """
        # A fresh Conversation already holds the system prompt, so "empty" is one
        # message, not zero. Checking for zero rejected every real resume.
        if len(self._messages) > (1 if self._system else 0):
            raise RuntimeError("restore() is for a fresh conversation, not a running one")
        self._messages = [dict(m) for m in messages]

    def append(self, role: str, content: str, reasoning: str | None = None,
               tool_calls: list[dict] | None = None,
               tool_call_id: str | None = None) -> None:
        message = {"role": role, "content": content}
        # Absent rather than empty: a turn that produced no reasoning has to
        # serialise exactly as it did before this field existed.
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in tool_calls
            ]
        if tool_call_id:
            message["tool_call_id"] = tool_call_id
        self._messages.append(message)

    def reset(self) -> None:
        self._messages = []
        if self._system:
            self._messages.append({"role": "system", "content": self._system})

    def payload(self) -> list[dict[str, str]]:
        # A copy, so a caller cannot mutate history through the returned list.
        return [dict(m) for m in self._messages]

    def __len__(self) -> int:
        return len(self._messages)


def _post_stream(url: str, body: dict, api_key: str, timeout: float):
    """Yield decoded SSE data lines from an OpenAI-compatible endpoint.

    THE READ RUNS IN A THREAD SO Ctrl+C WORKS. Measured 2026-08-07: with the
    read in the main thread, Ctrl+C did not interrupt a running turn on
    Windows at all. `for raw in resp` blocks inside a C-level socket read,
    and CPython can only deliver KeyboardInterrupt once the interpreter is
    back in control - which, on a stream that keeps delivering bytes, is not
    a moment the user can wait for. A 15-minute turn was therefore
    unstoppable except by killing the window.

    The reader is a daemon thread and the main thread only ever waits on a
    queue with a short timeout, so a signal lands between two waits. The
    thread is left behind on interrupt rather than joined: it is blocked on
    the very read we could not interrupt, and the process exits regardless
    because it is a daemon.
    """
    import queue

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise CrowError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CrowError(f"cannot reach {url}: {exc.reason}") from exc

    lines: "queue.Queue" = queue.Queue(maxsize=256)
    _EOF = object()

    def _reader():
        try:
            for raw in resp:
                lines.put(raw)
        except Exception as exc:  # noqa: BLE001 - handed to the main thread below
            lines.put(exc)
        finally:
            lines.put(_EOF)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        while True:
            if INTERRUPT.is_set():
                return
            try:
                item = lines.get_nowait()
            except queue.Empty:
                # POLL AND SLEEP, do not block on the queue. Measured
                # 2026-08-07: `lines.get(timeout=0.2)` did not fix Ctrl+C
                # either. On Windows a timed queue.get() waits inside a lock
                # acquire, and CPython cannot deliver SIGINT while the main
                # thread sits in that wait - the same class of problem as the
                # socket read it replaced. time.sleep() IS interruptible, so
                # the signal lands here.
                time.sleep(0.05)
                continue
            if INTERRUPT.is_set():
                return
            if item is _EOF:
                return
            if isinstance(item, Exception):
                raise CrowError(f"stream broke: {item}") from item
            line = item.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                return
            yield payload
    finally:
        # Closing the response makes the blocked read fail, which ends the
        # reader thread instead of leaving it attached to a live socket.
        #
        # ON WINDOWS IT DOES NOT WAKE A READ THAT IS ALREADY BLOCKED, and that
        # is measured rather than suspected. #90's failure point P1 asks for "a
        # read timeout as a second abort path"; the first attempt at one was a
        # `settimeout` right here, in front of the close, and
        # tools/measure_gui_stream.py took it apart. Against a loopback server
        # that goes quiet for 10 s, with the reader blocked in recv
        # [measured 2026-08-13]:
        #
        #     settimeout(1.0)          the read woke after 9.7 s
        #     shutdown(SHUT_RDWR)      the read woke after 9.7 s
        #     the socket's own close   the read woke after 9.7 s
        #
        # All three woke when the SERVER hung up, none of them because of what
        # this thread did. And `resp.close()` below blocks with them: it takes
        # the BufferedReader's lock, which the blocked reader holds, so the abort
        # returns at the socket's ORIGINAL timeout -- 1.96 s against a 2 s
        # timeout, 11.95 s against a 30 s one.
        #
        # So the second abort path cannot be armed here. It is the socket's read
        # timeout as it stood when the read STARTED, which means the caller's
        # `timeout` and nothing else -- see READ_TIMEOUT_S in cli/crow_gui.py for
        # the value a window may use and why the mockup's 20 s is not it.
        try:
            resp.close()
        except Exception:
            pass


class ReplyEvents:
    """What `stream_reply` reports while a turn streams. Seven events, no screen.

    The names say what HAPPENED, not what a terminal should do about it -- that
    is the whole point of the seam. Every method does nothing here, so a caller
    that wants only the returned text (a test, a probe, a batch run) passes
    nothing and gets silence.

    THE FIRST FOUR are the THIRTEEN terminal lines the old `stream_reply` in
    cli/crow.py carried -- two signature parameters (`out=sys.stdout`,
    `prefix: str = ""`) and eleven statements -- regrouped by the moment they
    fired at. cli/crow.py's `TerminalEvents` holds those eleven statements now,
    unchanged, and its `stream_reply` still takes the two parameters:

      reply_started   -- before the first byte is asked for. The CLI builds its
                         renderer here and starts the bird.
      answer_started  -- the first CONTENT delta, i.e. the model stopped
                         thinking and began writing. Fires at most once per
                         turn, and never at all for a turn that only calls
                         tools. The CLI switches the bird's label, lets one
                         frame of it show, stops it and writes its prompt.
      answer_text     -- one content delta, in the order it arrived. Reasoning
                         does NOT come through here and never did -- merged
                         into the answer it would be indistinguishable from it.
                         It has its own three below.
      reply_finished  -- from the core's `finally`, so it also runs when the
                         turn raises or is interrupted. The CLI closes its
                         renderer and stops the bird here, and that pairing is
                         why the branch has to stay one piece: half of it means
                         an open spill file, a half-drawn code block and a bird
                         that keeps flapping over the traceback.

    THE LAST THREE ARE THE THOUGHT BLOCKS, and until E10 there were none: the
    reasoning was kept, counted and shown to nobody, so the CLI could not
    display what a window was about to. They are reported by `ReasoningBlocks`
    below, which is where the decision about WHERE a block begins and ends is
    made -- these only carry it out:

      reasoning_started  -- a block opened. `index` counts from 1 within the
                           turn, so a surface can say WHICH one this is; the
                           second one is the case a "think first, then answer"
                           implementation gets wrong.
      reasoning_text     -- one reasoning delta, verbatim and in order.
      reasoning_finished -- the block closed, because content arrived or the
                           stream ended. A surface that ignores all three
                           prints exactly what it printed before E10, which is
                           what the CLI does without --show-reasoning.
    """

    def reply_started(self) -> None:
        """The turn begins; nothing has arrived yet."""

    def answer_started(self) -> None:
        """The first content delta arrived. Thinking is over."""

    def answer_text(self, piece: str) -> None:
        """One content delta, verbatim and in order."""

    def reply_finished(self) -> None:
        """The stream ended -- normally, by error, or by interrupt."""

    def reasoning_started(self, index: int) -> None:
        """A thought block opened. `index` counts from 1 within this turn."""

    def reasoning_text(self, piece: str) -> None:
        """One reasoning delta, verbatim and in order."""

    def reasoning_finished(self) -> None:
        """The open thought block ended. The answer resumes, or the turn does."""


class ReasoningBlocks:
    """WHERE A THOUGHT BEGINS, WHERE IT ENDS, AND WHERE IT BEGINS AGAIN.

    THE MODEL CAN RE-ENTER REASONING MID-ANSWER (#90, failure point P3).
    `reasoning_content` and `content` are two keys of the SAME delta object and
    either can follow either: think, answer, think again, answer again, inside
    one turn. Hard-coding "think first, then answer" puts answer text inside a
    collapsed reasoning block, where nobody reads it. `blocks` is a LIST for
    that reason and for no other, and the counter-probe in
    cli/test_crow_core.py drives a hard-coded version against the same fixture
    to show what it costs.

    IT LIVES IN THE CORE BECAUSE BOTH SURFACES NEED IT. The terminal shows and
    hides a block, the window folds and unfolds one -- two presentations of one
    decision. Written in cli/crow_gui.py it would be the second truth this
    whole split exists to prevent; written in cli/crow.py the window would have
    to reach into a terminal to find out what a thought is.

    IT ALSO COUNTS, and that is not a convenience. `format_timings` prints
    `thinking NN%` from `_reasoning_chars`/`_content_chars`. Counted beside the
    state machine rather than inside it, the share and the blocks on screen
    would be two statements about the same turn, and the one that goes stale is
    the one nobody reads twice.

    Fed in ARRIVAL ORDER, one call per delta. `finish()` closes a block the
    stream ended inside of -- the normal shape of a turn that only calls tools:
    it thinks, it emits a tool call, and no content ever arrives.
    """

    def __init__(self, events: "ReplyEvents | None" = None) -> None:
        self._events = events or ReplyEvents()
        # CLOSED blocks, in order. The open one is not in here until it closes:
        # a block that is still being written is not a block yet, and a length
        # taken mid-stream would count it twice.
        self.blocks: list[str] = []
        self.open = False
        self.reasoning_chars = 0
        self.content_chars = 0
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        """Every thought of this turn, in arrival order, joined by nothing.

        This is what goes back to the server on the next turn, so it has to be
        the concatenation the stream sent and not a rendering of it: the blocks
        are a view for a reader, the string is the prefix. Measured 2026-08-08:
        dropping the field costs the size of the previous turn's output on
        every later turn, 242.3 s of prefill against 1.6 s on a turn that had
        generated 2046 tokens.
        """
        return "".join(self.blocks) + "".join(self._parts)

    def reasoning_delta(self, piece: str) -> None:
        """One `reasoning_content` delta. Opens a block if none is open."""
        if not piece:
            return
        if not self.open:
            self.open = True
            self._parts = []
            # +1 because the open block is not in `blocks` yet: the FIRST block
            # is index 1, and the one that reopens after an answer is 2.
            self._events.reasoning_started(len(self.blocks) + 1)
        self._parts.append(piece)
        self.reasoning_chars += len(piece)
        self._events.reasoning_text(piece)

    def content_delta(self, piece: str) -> None:
        """One `content` delta. Closes the open block -- this is the seam.

        The answer starts HERE, not at the end of the turn, and that single
        line is the difference between two blocks with an answer between them
        and one block with the answer swallowed.
        """
        if not piece:
            return
        self.finish()
        self.content_chars += len(piece)

    def finish(self) -> None:
        """Close an open block. Idempotent, and called from a `finally`."""
        if not self.open:
            return
        self.blocks.append("".join(self._parts))
        self._parts = []
        self.open = False
        self._events.reasoning_finished()


class FenceEvents:
    """What `CodeFences` reports as an answer streams past it. Four events.

    Same seam as `ReplyEvents`: the names say what the text IS, not what a
    surface should do with it. Every method does nothing here, so a caller that
    only wants the finished blocks passes nothing and reads `blocks`.
    """

    def prose(self, piece: str) -> None:
        """Answer text outside any fence, verbatim and in order."""

    def code_started(self, language: str) -> None:
        """A fence opened. `language` is what stood after the backticks, or ""."""

    def code_text(self, line: str) -> None:
        """One line inside the block, without its newline."""

    def code_finished(self, closed: bool) -> None:
        """The block ended. `closed` is False when the ANSWER ended inside it."""


class CodeFences:
    """WHAT COUNTS AS A FENCE, AND WHAT AN UNCLOSED BLOCK IS.

    #90's capability 6 -- "code blocks rendered as such, with a copy button".
    The BUTTON is a window's business; where a block begins and ends is not, and
    a second opinion about it is how one client frames something the other reads
    as prose.

    THREE DECISIONS, and each one is a case the plan names:

      * A FENCE IS A WHOLE LINE. `sent` below tracks how much of the current line
        has already flowed as prose, and a line that has flowed cannot become a
        fence any more. Without it, "call it with ```json" opens a code block in
        the middle of a sentence -- the plan's "prose with three backticks must
        produce NO block".
      * WHAT FOLLOWS THE BACKTICKS HAS TO BE A BARE WORD. "```python" opens one,
        "``` and then" does not: an opening fence carries a language tag or
        nothing at all, and everything else on that line means it was a sentence.
      * AN UNCLOSED BLOCK IS STILL A BLOCK. `finish()` reports `closed=False` and
        the block keeps its text, because an answer that was cut off mid-code is
        exactly when the reader wants to copy what there is. Dropping it would
        lose the part the user was waiting for.

    CODE IS HELD TO ITS LINE, PROSE IS NOT. Prose flows as it arrives, so the
    answer appears while it is generated; a code line is held until its newline,
    because a fence cannot be recognised from half a line. That is one line of
    latency inside a block and none outside -- the same trade cli/crow.py's
    Renderer makes, for the same reason.

    ONLY THE WINDOW IS WIRED TO IT TODAY, and manifests/shared-core.json says so
    with a reason rather than leaving it to be noticed: the terminal's Renderer
    carries its own fence handling, moving it onto this is a rewrite of the
    thing that draws every CLI answer, and that is not this stage.
    """

    # A bare language tag: letters, digits and the punctuation that appears in
    # real tags (c++, c#, objective-c, asp.net). A space in here means the line
    # was a sentence.
    LANGUAGE = re.compile(r"^[A-Za-z0-9+#._-]*$")

    def __init__(self, events: "FenceEvents | None" = None) -> None:
        self._events = events or FenceEvents()
        # CLOSED blocks and their tags, in order. The open one is not in here
        # until it ends, for the reason `ReasoningBlocks` gives: a block that is
        # still being written is not a block yet.
        self.blocks: list[str] = []
        self.languages: list[str] = []
        self.open = False
        self.language = ""
        self._parts: list[str] = []
        self._line = ""
        self._sent = 0
        self._deciding = True

    def feed(self, piece: str) -> None:
        """One content delta, in arrival order.

        WHOLE RUNS, NOT CHARACTERS, and that is a measured constraint rather
        than tidiness. The first version reported one `prose` event per
        character; a window batches its queue per tick and then hands the batch
        here, so a per-character sink undid the batching one layer down and
        turned a 512-delta tick into three thousand widget inserts. Only the
        two or three characters at the START of a line are looked at singly,
        because that is the whole span in which a line could still become a
        fence.
        """
        while piece:
            if self.open or not self._deciding:
                # Inside a block, or on a line already known to be prose: take
                # everything up to the next newline in one go.
                head, sep, piece = piece.partition("\n")
                if head:
                    self._line += head
                    if not self.open:
                        self._events.prose(head)
                        self._sent = len(self._line)
                if sep:
                    self._end_line()
                continue
            ch, piece = piece[0], piece[1:]
            if ch == "\n":
                self._end_line()
                continue
            self._line += ch
            held = self._line.lstrip()
            if not held or "```".startswith(held[:3]):
                # It could still become a fence, so it waits. At most three
                # characters of latency, and only at the start of a line.
                continue
            self._deciding = False
            self._events.prose(self._line[self._sent:])
            self._sent = len(self._line)

    def finish(self) -> None:
        """The answer ended. Closes a block the stream stopped inside of."""
        if self._line or self._sent:
            self._end_line(final=True)
        if self.open:
            self._close(closed=False)

    def is_fence(self, line: str) -> bool:
        """Whether this whole line is an opening or closing fence."""
        stripped = line.strip()
        if not stripped.startswith("```"):
            return False
        return bool(self.LANGUAGE.match(stripped[3:].strip()))

    def _end_line(self, final: bool = False) -> None:
        line, sent = self._line, self._sent
        self._line, self._sent, self._deciding = "", 0, True
        if sent == 0 and self.is_fence(line):
            if self.open:
                self._close(closed=True)
            else:
                self.open = True
                self._parts = []
                self.language = line.strip()[3:].strip()
                self._events.code_started(self.language)
            return
        if self.open:
            self._parts.append(line)
            self._events.code_text(line)
            return
        # `final` is the answer stopping without a trailing newline. Everything
        # before it kept its newline, so adding one here would put a line break
        # into the transcript that the model never sent.
        self._events.prose(line[sent:] + ("" if final else "\n"))

    def _close(self, closed: bool) -> None:
        self.blocks.append("\n".join(self._parts))
        self.languages.append(self.language)
        self._parts = []
        self.open = False
        self.language = ""
        self._events.code_finished(closed)


def stream_reply(
    conversation: Conversation,
    *,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    top_p: float = TOP_P,
    min_p: float = MIN_P,
    reasoning_effort: str | None = None,
    timeout: float,
    events: "ReplyEvents | None" = None,
) -> tuple[str, str, dict]:
    """Stream one assistant turn. Returns (text, reasoning, timings).

    The reply is appended to the conversation by the caller, not here -- a
    turn that was interrupted must not silently become part of the prefix.

    THE SERVER SENDS TWO STREAMS, NOT ONE, and until 2026-08-07 this read
    only one of them. `server_chat_msg_diff_to_json_oaicompat` puts thoughts
    in `delta["reasoning_content"]` and the answer in `delta["content"]` --
    two keys of the same object. Measured over the 30 stored answers of that
    day's reference run: 30 of 30 carried reasoning, and 88.2 % of every
    generated character sat in it. Reading `content` alone therefore threw
    away most of what the model produced and left the user watching a bird.

    BOTH ARE RETURNED, and until 2026-08-08 this docstring claimed the
    opposite: that the template does not replay a previous turn's thoughts, so
    feeding them back would break the cache. Measured that day, it is the
    other way round -- dropping the field is what breaks the prefix. See
    `Conversation` for the numbers. `text` still carries content alone; the
    reasoning travels as its own field and is never merged into the answer.

    `tools` is what makes the replay take effect at all: this model's template
    keeps a past turn's thoughts only when the request carries tools.

    THE SPLIT IS A STATE MACHINE, NOT AN ORDER. `ReasoningBlocks` above owns
    where a thought block begins, ends and BEGINS AGAIN; this loop only feeds it
    the two kinds of delta in arrival order. Both surfaces read the blocks from
    there -- the terminal to show or hide them, the window to fold or unfold
    them -- and neither is allowed its own idea of what a thought is.

    NOTHING IN HERE WRITES TO A SCREEN. Until this seam existed the function
    took `out` and `prefix`, built a Renderer and a Raven and printed as it
    read; those thirteen lines are four calls on `events` now, and cli/crow.py
    hands over an events object that does character for character what the
    thirteen lines did. `events=None` means silence, which is what a probe or
    a test wants.

    THE TRANSPORT IS NOT A PARAMETER, deliberately. `_post_stream` is looked up
    as a module global at call time, and cli/crow.py's module class writes a
    rebind of `crow._post_stream` through to this module (see `_FROM_CORE`
    there) -- so a test double set on either name reaches this loop. A second
    way in would be a second truth about where the bytes come from.
    """
    if events is None:
        events = ReplyEvents()

    body = {
        "model": model,
        "messages": conversation.payload(),
        "tools": TOOLS,
        "temperature": temperature,
        # Sent explicitly rather than trusted to the server default: 0731's card
        # runs its agentic benchmarks at top_p 0.95, its generation_config.json
        # says 1.0, and llama.cpp's own default is a third value. Whichever is
        # right, a measurement must know which one it got.
        "top_p": top_p,
        # Same reason: unsloth recommends 0.01 for this model, llama.cpp defaults
        # to 0.05, and not sending the field means inheriting a value nobody chose.
        "min_p": min_p,
        "stream": True,
        # OpenAI's opt-in for a usage block on the final chunk. Without it a
        # streamed response carries no token counts at all, and the context bar
        # is left guessing from `prompt_n`, which counts something else entirely
        # -- see `repl`. Endpoints that do not know the field ignore it.
        "stream_options": {"include_usage": True},
        # llama.cpp extension: makes the server attach its own timing block
        # to the final chunk. Ignored by endpoints that do not know it.
        "timings_per_token": True,
    }
    if reasoning_effort is not None:
        # Only when asked for. 0731's template reads the key and treats an absent
        # one as "low"; sending nothing keeps the prompt byte-identical to a
        # client that predates the switch, which is what the prompt cache wants.
        # The value lands in the TEMPLATE, not the sampler -- whether it took
        # effect is visible only in the rendered prompt, which is why E11's
        # counter-probe compares /apply-template output and not this body.
        body["chat_template_kwargs"] = {"reasoning_effort": reasoning_effort}

    text_parts: list[str] = []
    # THE THOUGHT BLOCKS, and this used to be a flat list plus an `in_reasoning`
    # flag that nothing ever set. The flag was the whole state machine, unwired:
    # it could not have told a second block from the first, which is exactly the
    # failure E10 was cut for.
    thoughts = ReasoningBlocks(events)
    tool_calls: dict[int, dict] = {}
    timings: dict = {}
    context_tokens: int | None = None
    cached_tokens: int | None = None
    started = time.monotonic()
    first_token_at: float | None = None
    first_content_at: float | None = None
    finish_reason: str | None = None

    events.reply_started()

    def _mark_first_token(now: float) -> None:
        """Record when the first token of ANY kind arrived.

        It does not stop the bird: reasoning is no longer printed, so the
        animation has to survive the whole thinking phase and only give way
        once real content starts.
        """
        nonlocal first_token_at
        if first_token_at is None:
            first_token_at = now

    try:
        for payload in _post_stream(f"{base_url}/chat/completions", body, api_key, timeout):
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if isinstance(chunk.get("timings"), dict):
                timings = chunk["timings"]

            # The absolute size of the conversation, straight from the server's
            # tokeniser. It arrives on the last chunk only, and only one chunk
            # carries it, so it is read wherever it turns up rather than assumed
            # to be on the same object as the timings.
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                if isinstance(total, int):
                    context_tokens = total
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                if isinstance(cached, int):
                    cached_tokens = cached

            for choice in chunk.get("choices") or []:
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}

                thought = delta.get("reasoning_content")
                if thought:
                    # Kept, counted, and SHOWN ONLY IF ASKED. The reasoning is
                    # 60-90 % of every answer this model gives; printed in full
                    # by default it buries the code, which is why the bird
                    # carries the state instead and why --show-reasoning is a
                    # flag rather than the behaviour. Kept rather than merely
                    # counted because the next turn sends it back.
                    thoughts.reasoning_delta(thought)
                    _mark_first_token(time.monotonic())

                # Arguments arrive in fragments across chunks and have to be
                # concatenated per index before the JSON is parseable. `id` and
                # `name` usually come on the first fragment only, so neither may
                # be overwritten with the empty string that follows.
                for call in delta.get("tool_calls") or []:
                    idx = call.get("index", 0)
                    slot = tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if call.get("id"):
                        slot["id"] = call["id"]
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                    _mark_first_token(time.monotonic())

                piece = delta.get("content")
                if piece:
                    now = time.monotonic()
                    _mark_first_token(now)
                    # BEFORE `answer_started`, deliberately: an open thought
                    # block has to be closed before the surface is told the
                    # answer begins, or the terminal writes its prompt inside
                    # the block and the window folds the first line of the
                    # answer away with the thought.
                    thoughts.content_delta(piece)
                    if first_content_at is None:
                        first_content_at = now
                        events.answer_started()
                    text_parts.append(piece)
                    events.answer_text(piece)
    finally:
        # Before `reply_finished`, for the same reason and one more: a turn that
        # ends INSIDE a thought -- a tool call, an interrupt, a dead socket --
        # still has to close its block, or the surface is left holding one open
        # forever. `finish()` is idempotent, so the normal path pays nothing.
        thoughts.finish()
        events.reply_finished()

    elapsed = time.monotonic() - started
    # ttft is the FIRST token of any kind. Before 2026-08-07 it was the first
    # content token, so it silently included the whole reasoning decode and
    # read as a prefill several times larger than the one the server reported.
    if first_token_at is not None:
        timings.setdefault("_client_ttft_s", round(first_token_at - started, 2))
    if first_content_at is not None:
        timings.setdefault("_client_answer_s", round(first_content_at - started, 2))
    timings.setdefault("_client_total_s", round(elapsed, 2))
    reasoning = thoughts.text
    if reasoning:
        # FROM THE STATE MACHINE, NOT BESIDE IT. `format_timings` divides these
        # two into `thinking NN%`; counted here with a second `len()` they would
        # be a second opinion about the same turn, and the day the automaton
        # decides a delta belongs to a block the percentage would not hear of
        # it. The third number is what --show-reasoning actually draws.
        timings.setdefault("_reasoning_chars", thoughts.reasoning_chars)
        timings.setdefault("_content_chars", thoughts.content_chars)
        timings.setdefault("_reasoning_blocks", len(thoughts.blocks))
    if finish_reason:
        timings.setdefault("_finish_reason", finish_reason)
    if context_tokens is not None:
        timings.setdefault("_context_tokens", context_tokens)
    if cached_tokens is not None:
        timings.setdefault("_cached_tokens", cached_tokens)
    if tool_calls:
        timings["_tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return "".join(text_parts), reasoning, timings


def format_clock(seconds: float) -> str:
    """A duration a person can read at a glance. 4531.29 -> 1h15m31s."""
    # The boundary is tested against the UNROUNDED value. Rounding first turned 59.9 into 60 and
    # printed it as "1m00s" -- a minute that had not passed yet.
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


class TurnCost:
    """What one USER turn cost, across however many tool rounds it took.

    WHY THIS EXISTS (#70). The per-round timing line was right while the tool loop was being built
    -- it is what showed the prefix holding, `cached` climbing 3,624/9,048 -> 43,643/43,686. For
    using the thing it is noise: one question on 2026-08-09 produced 12 of those lines and a
    24-round turn on 2026-08-10 produced 24, with the answer somewhere in between and the rollover
    and budget notices scrolling past among them. The lines that matter were getting rarer among
    the lines that do not.

    THE TOTAL IS WALL CLOCK, NOT THE SUM OF THE ROUNDS, and that is the whole point of measuring it
    here rather than adding up `_client_total_s`. The rounds only count time the model was
    generating; the tools run between them and the user waits through those too. A turn that spent
    24 s in `find_files` waited 24 s, and a total that omits them describes a turn nobody had.
    Both parts are printed, so the gap between them stays visible instead of being smoothed away.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.rounds = 0
        self.decoded = 0
        self.prefilled = 0
        self.model_s = 0.0
        # DECODE AND PREFILL TIME SEPARATELY, and not `model_s`, because tok/s is a decode figure.
        # The first version divided tokens by the whole round and printed 1.49 tok/s for a turn the
        # server had just measured at 14.77 and 16.46 -- 252 tokens against 169 s, of which 150 s
        # were prefill. Caught by robin on the first live run, 2026-08-11.
        self.decode_s = 0.0
        self.prefill_s = 0.0
        self.tool_s = 0.0
        self.tool_calls = 0
        self.tool_errors = 0
        # The LAST round's cache figures, not a sum: `cached` is a statement about the prefix as it
        # stands now, and adding those up would produce a number that means nothing.
        self.cached: int | None = None
        self.cached_of: int | None = None
        self.finish: str | None = None

    def add_round(self, timings: dict) -> None:
        self.rounds += 1
        for key, attr in (("predicted_n", "decoded"), ("prompt_n", "prefilled")):
            value = timings.get(key)
            if value is not None:
                setattr(self, attr, getattr(self, attr) + int(value))
        total = timings.get("_client_total_s")
        if total is not None:
            self.model_s += float(total)
        # `*_ms` is what llama.cpp reports for each phase. The fallback derives the same seconds
        # from the rate when only that is present, so a server that sends one and not the other
        # still produces a rate rather than none.
        for ms_key, n_key, rate_key, attr in (
                ("predicted_ms", "predicted_n", "predicted_per_second", "decode_s"),
                ("prompt_ms", "prompt_n", "prompt_per_second", "prefill_s")):
            ms = timings.get(ms_key)
            if ms is not None:
                setattr(self, attr, getattr(self, attr) + float(ms) / 1000.0)
                continue
            n, rate = timings.get(n_key), timings.get(rate_key)
            if n is not None and rate:
                setattr(self, attr, getattr(self, attr) + float(n) / float(rate))
        cached = timings.get("_cached_tokens")
        prompt_n = timings.get("prompt_n")
        if cached is not None and prompt_n is not None:
            self.cached, self.cached_of = int(cached), int(cached) + int(prompt_n)
        self.finish = timings.get("_finish_reason")

    def add_tool(self, seconds: float, failed: bool) -> None:
        self.tool_s += seconds
        self.tool_calls += 1
        self.tool_errors += int(failed)

    def line(self) -> str:
        waited = time.monotonic() - self.started
        bits = [f"{self.rounds} round" + ("s" if self.rounds != 1 else "")]
        if self.decoded:
            rate = self.decoded / self.decode_s if self.decode_s > 0 else None
            bits.append(f"{self.decoded:,} tok" + (f" @ {rate:.2f} tok/s" if rate else ""))
        if self.prefilled:
            rate = self.prefilled / self.prefill_s if self.prefill_s > 0 else None
            bits.append(f"prefill {self.prefilled:,}" + (f" @ {rate:.2f} tok/s" if rate else ""))
        if self.cached is not None:
            bits.append(f"cached {self.cached:,}/{self.cached_of:,}")
        if self.tool_calls:
            failed = f", {self.tool_errors} failed" if self.tool_errors else ""
            bits.append(f"{self.tool_calls} tool call"
                        + ("s" if self.tool_calls != 1 else "") + failed)
        split = f" (model {format_clock(self.model_s)}, tools {format_clock(self.tool_s)})" \
            if self.tool_s >= 0.5 else ""
        bits.append(f"waited {format_clock(waited)}{split}")
        if self.finish == "length":
            bits.append(CUT_OFF_NOTE)
        return " | ".join(bits)


# Read-before-write, and it BLOCKS rather than warns. #10 measured hermes-agent
# resolving this to last-write-wins in two independent code paths: file_state.py
# returns a warning string and file_tools.py performs the write anyway. A model
# that overwrites a file it never read destroys work it cannot see, and at this
# decode rate nobody is watching closely enough to catch it.
#
# ITS LIFETIME IS ONE USER TURN (E6), and that is a decided scope rather than an
# inherited one. Until E6 there was no clear() and no del anywhere, so "read in
# this session" in fact meant "read in this PROCESS" -- true only for as long as
# a process IS a session, which stops being true the moment a second surface can
# close one session and open another without exiting.
#
# THE THRESHOLD WAS WRITTEN DOWN BEFORE THE COUNT, so the number could decide
# rather than confirm: null cases of a write landing on a file last read in an
# EARLIER turn makes the turn scope free; one or more, and the scope is the
# session. MEASURED 2026-08-12 over every rollover archive and session.json on
# this machine -- 3 distinct files (one archive counted once, not twice: the
# session-backup copy is byte-identical), 25 user turns, 31 read_file calls,
# 4 write_file/edit_file calls -- RESULT 0. Every one of the four writes was
# preceded by a read of the same file INSIDE ITS OWN TURN. So the turn scope
# costs nothing that has ever actually happened here, and it is the narrower of
# the two on a rule whose failure mode is losing someone's work.
#
# What the user pays for it is one extra refusal where there was none, and the
# way out is the same one the rule already asks for on a file it has never seen:
# read it again, then write. It is named in the refusal itself, which is why
# both messages below say "in this turn" rather than just "first".
_READ: set[str] = set()


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _clip(text: str, limit: int = MAX_TOOL_BYTES) -> str:
    """EVERY tool result goes through here. No exceptions, and that is the point.

    Each one was capped separately at first -- read_file by bytes, the searches
    by hit count -- and a hit count is not a size: `search_text` for "LRU" over a
    source tree returned 200 hits and ~20,000 tokens, which is eight minutes of
    prefill at 38 tok/s. Same defect as the 100 KB read cap, different tool.

    One ceiling, measured in what it actually costs: bytes that become prefill.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[cut at {limit} bytes -- narrow the query and ask again]"


def tool_read_file(path: str, start_line: int | None = None, end_line: int | None = None,
                   **_) -> str:
    """Read a file, or a range of its lines.

    THE RANGE IS THE POINT, NOT A CONVENIENCE. Everything read has to be
    prefilled, and prefill runs at ~38 tok/s here: a 200 KB source file is
    ~50,000 tokens and costs over twenty minutes before the model has had a
    single thought about it. Measured 2026-08-09, one such call took 654 s.
    `search_text` already returns line numbers, so reading 60 lines around a hit
    turns that into seconds.
    """
    if start_line is not None or end_line is not None:
        lo = max(1, int(start_line or 1))
        hi = int(end_line) if end_line is not None else lo + 200
        if hi < lo:
            return f"error: end_line {hi} is before start_line {lo}"
        try:
            out, total = [], 0
            with open(path, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    total = n
                    if lo <= n <= hi:
                        out.append(f"{n}: {line.rstrip()}")
                    elif n > hi:
                        total = None  # not counted to the end; do not claim a length
                        break
        except FileNotFoundError:
            return f"error: no such file: {path}"
        except OSError as exc:
            return f"error: could not read {path}: {exc}"
        if not out:
            return f"error: {path} has no lines in {lo}-{hi}" + (
                f" (the file has {total})" if total else "")
        _READ.add(_key(path))
        return _clip("\n".join(out))

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = fh.read(MAX_TOOL_BYTES + 1)
    except FileNotFoundError:
        # Near-misses only, not the whole directory. Forty names came back on
        # every failed attempt and were prefilled every time -- the model then
        # tried the same wrong path again, so the "help" paid for the loop.
        # A wrong extension is the common case (server-context.c for .cpp), so
        # what is offered is the same stem, and at most three of them.
        parent = os.path.dirname(os.path.abspath(path)) or "."
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        try:
            near = [n for n in sorted(os.listdir(parent))
                    if os.path.splitext(n)[0].lower() == stem][:3]
        except OSError:
            near = []
        if near:
            return f"error: no such file: {path}\ndid you mean: {', '.join(near)}"
        return f"error: no such file: {path} (use find_files or list_dir to locate it)"
    except IsADirectoryError:
        return f"error: {path} is a directory -- use list_dir"
    except PermissionError:
        return f"error: permission denied: {path}"
    except OSError as exc:
        return f"error: could not read {path}: {exc}"
    _READ.add(_key(path))
    return _clip(data)


def tool_write_file(path: str, content: str = "", **_) -> str:
    if os.path.exists(path) and _key(path) not in _READ:
        return (f"error: refusing to overwrite {path} without reading it first in "
                f"this turn. Call read_file on it, then write.")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    except OSError as exc:
        return f"error: could not write {path}: {exc}"
    _READ.add(_key(path))
    return f"wrote {len(content)} bytes to {path}"


def tool_edit_file(path: str, old: str = "", new: str = "", **_) -> str:
    """Exact-match replacement, and it refuses an ambiguous one.

    A patch format would be more expressive and needs fuzzy matching to survive
    a model that mis-remembers whitespace. Exact match plus a uniqueness check
    fails loudly instead of guessing, which is the behaviour worth having first.
    """
    if _key(path) not in _READ:
        return f"error: read {path} before editing it, in this turn"
    if not old:
        return "error: edit_file needs 'old' -- to create a file use write_file"
    try:
        with open(path, encoding="utf-8") as fh:
            data = fh.read()
    except OSError as exc:
        return f"error: could not read {path}: {exc}"
    hits = data.count(old)
    if hits == 0:
        return f"error: 'old' does not appear in {path}"
    if hits > 1:
        return f"error: 'old' appears {hits} times in {path} -- include more context to make it unique"
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data.replace(old, new, 1))
    except OSError as exc:
        return f"error: could not write {path}: {exc}"
    return f"replaced 1 occurrence in {path}"


def tool_list_dir(path: str = ".", **_) -> str:
    try:
        entries = sorted(os.listdir(path))
    except FileNotFoundError:
        return f"error: no such directory: {path}"
    except NotADirectoryError:
        return f"error: {path} is a file -- use read_file"
    except OSError as exc:
        return f"error: could not list {path}: {exc}"
    lines = []
    for name in entries[:MAX_HITS]:
        full = os.path.join(path, name)
        if os.path.isdir(full):
            lines.append(f"{name}/")
        else:
            try:
                lines.append(f"{name}  ({os.path.getsize(full)} bytes)")
            except OSError:
                lines.append(name)
    if len(entries) > MAX_HITS:
        lines.append(f"[{len(entries) - MAX_HITS} more entries]")
    return _clip("\n".join(lines) or "(empty)")


def tool_find_files(root: str = ".", pattern: str = "*", **_) -> str:
    import fnmatch

    hits, size = [], 0
    for base, dirs, files in os.walk(root):
        # Directories nobody means when they say "find the source file", and
        # walking them turns a search into minutes.
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist"}]
        for name in files:
            if fnmatch.fnmatch(name, pattern):
                hit = os.path.join(base, name)
                hits.append(hit)
                size += len(hit) + 1
                # Both ceilings, because either one alone lets the other through:
                # few hits can still be long paths, many short ones still add up.
                if len(hits) >= MAX_HITS or size >= MAX_TOOL_BYTES:
                    return "\n".join(hits) + "\n[stopped -- narrow the pattern or the root]"
    return "\n".join(hits) or f"no file matching {pattern} under {root}"


def tool_search_text(root: str = ".", pattern: str = "", glob: str = "*", **_) -> str:
    import fnmatch
    import re as _re

    if not pattern:
        return "error: search_text needs a 'pattern'"
    try:
        rx = _re.compile(pattern)
    except _re.error as exc:
        return f"error: bad regular expression: {exc}"
    hits, size = [], 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist"}]
        for name in files:
            if not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(base, name)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if rx.search(line):
                            hit = f"{full}:{n}: {line.rstrip()[:200]}"
                            hits.append(hit)
                            size += len(hit) + 1
                            # The one that cost eight minutes: 200 hits of a
                            # common word are ~20,000 tokens of prefill. A hit
                            # count does not bound a size.
                            if len(hits) >= MAX_HITS or size >= MAX_TOOL_BYTES:
                                return ("\n".join(hits)
                                        + "\n[stopped -- narrow the pattern, or pass a glob]")
            except OSError:
                continue
    return "\n".join(hits) or f"no match for {pattern}"


def tool_run_command(command: str = "", cwd: str | None = None, **_) -> str:
    """Local execution only, with a timeout and a capped result.

    Seven of hermes-agent's eight execution backends are out of scope here --
    the model is local and 96 GB, so a remote sandbox cannot reach it. A command
    that hangs would otherwise hold the turn until the socket timeout, which is
    30 minutes.
    """
    import subprocess

    if not command:
        return "error: run_command needs a 'command'"
    # The child does not inherit anything that looks like a secret. It is a
    # blocklist, so it is not airtight -- it stops the accident, not an attacker.
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))}
    try:
        done = subprocess.run(command, shell=True, cwd=cwd, env=env, timeout=COMMAND_TIMEOUT,
                              capture_output=True, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        return f"error: command exceeded {COMMAND_TIMEOUT}s and was killed: {command}"
    except OSError as exc:
        return f"error: could not run: {exc}"
    out = (done.stdout or "") + (("\n[stderr]\n" + done.stderr) if done.stderr else "")
    return _clip(f"[exit {done.returncode}]\n{out}".rstrip())


TOOL_IMPL = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_dir": tool_list_dir,
    "find_files": tool_find_files,
    "search_text": tool_search_text,
    "run_command": tool_run_command,
}


# ---------------------------------------------------------------- #88 ------
# RELEASE LEVELS. The classes come from the tools themselves, not from taste:
# reading is safe at every level, writing touches the disk, executing starts a
# shell. #88's table, one row per class.
#
# READING NEVER ASKS, at any level, and that is a decision rather than an
# oversight: "a level that asks before list_dir is a level nobody keeps switched
# on, and a protection everyone turns off protects nothing".
TOOL_CLASS = {
    "read_file": "reading",
    "list_dir": "reading",
    "find_files": "reading",
    "search_text": "reading",
    "write_file": "writing",
    "edit_file": "writing",
    "run_command": "executing",
}

# Which classes stop and ask, per level. A class not named here runs.
MODE_ASKS = {
    "manual": ("writing", "executing"),
    "allowedit": ("executing",),
    "auto": (),
}
MODES = tuple(MODE_ASKS)

# AUTO IS THE DEFAULT BECAUSE IT IS WHAT THIS CLIENT ALREADY DID. Every release
# up to here ran all seven tools unasked; making `manual` the default would
# change the behaviour of every existing session in a commit that is supposed to
# add a choice. The level is one word away in either surface, and both show
# which one is live.
DEFAULT_MODE = "auto"

# What a declined call comes back as. IT IS A TOOL RESULT, NOT AN ABORT --
# #88 point 1, and the same invariant `run_turn` already keeps three times over:
# an assistant turn whose tool_calls have no `tool` message behind them is a
# broken prefix for every later turn of the session. The model can read this
# line and try something else; it cannot read a turn that ended.
DECLINED = "error: declined by the user"


def needs_approval(name: str, mode: str) -> bool:
    """Does this tool stop and ask at this level?

    An unknown tool is treated as `executing`: the strictest class, because a
    tool this table has not heard of is one nobody has classified yet, and
    guessing "safe" for it is the one guess with a cost.
    """
    return TOOL_CLASS.get(name, "executing") in MODE_ASKS.get(mode, ())


# STANDING APPROVALS, per session and never written to disk. #88 point 3:
# "without a memory, manual is unusable -- a 24-round turn asks 24 times, and
# everyone switches to auto within two minutes". Cleared by new_session().
_ALLOWED: set[tuple[str, str]] = set()


def approval_scope(name: str, arguments: str) -> tuple[str, str] | None:
    """What one "always" covers -- and, more importantly, what it does NOT.

    A DIRECTORY FOR WRITES, A PROGRAM FOR COMMANDS. "Yes, and from now on"
    against `write_file C:/x/a.py` releases writes under `C:/x`, not writes
    everywhere; against `run_command git status` it releases `git`, not every
    command. The narrower the key, the less an "always" can widen into `auto`
    by accident -- which is the failure #88 asks for a test against.

    None means this call cannot be remembered at all: unparseable arguments, a
    missing path, an empty command. Then every occurrence asks again, which is
    the safe direction for a case nobody has thought about.
    """
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(args, dict):
        return None

    if name in ("write_file", "edit_file"):
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        return ("writing", os.path.dirname(os.path.abspath(path)).lower())

    if name == "run_command":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        # The program, not the line: `git status` and `git log` share a key,
        # `git` and `rm` do not.
        return ("executing", command.split()[0].lower())

    return None


def remembered(name: str, arguments: str) -> bool:
    """Has this session already said "always" for something covering this call?"""
    scope = approval_scope(name, arguments)
    return scope is not None and scope in _ALLOWED


def remember(name: str, arguments: str) -> tuple[str, str] | None:
    """Record an "always" for this call's scope. Returns what was recorded."""
    scope = approval_scope(name, arguments)
    if scope is not None:
        _ALLOWED.add(scope)
    return scope


def forget_approvals() -> None:
    """Drop every standing approval. Called when the user drops the chat.

    NOT FROM Conversation.reset(), and the difference is the point. reset() also
    runs inside roll_over(): the context is archived and started again while the
    user carries on with the same work. Clearing there would ask again for the
    directory they released four rounds ago, mid-turn, for a reason invisible
    from where they sit. `/reset` and the window's new-chat button are the
    places a session actually ends, and they are what call this.
    """
    _ALLOWED.clear()


# What has already been asked this turn, and what came back. Cleared per user
# turn, not per round.
_SEEN: dict[tuple[str, str], str] = {}


def run_tool_cached(name: str, arguments: str) -> tuple[str, bool]:
    """Run a tool, unless this exact call was already made. Returns (result, repeated).

    THE LOOP THIS PREVENTS, observed 2026-08-09: the model asked for
    `server-context.c` -- a file that does not exist, the real one ends in .cpp --
    got an error, and asked for the same path again. Eight times, twice within a
    single round. Each attempt cost a prefill of the error text, so the run spent
    minutes going nowhere and would have hit the round limit rather than an answer.

    Re-running would produce the identical failure, so the second call is answered
    from the first and told plainly that it is a repeat. That turns a loop into a
    fact the model has to react to.
    """
    key = (name, arguments)
    if key in _SEEN:
        return (f"[you already called {name} with these exact arguments this turn. "
                f"The result was, and still is:]\n{_SEEN[key]}"), True
    out = run_tool(name, arguments)
    _SEEN[key] = out
    return out, False


def run_tool(name: str, arguments: str) -> str:
    """Execute one tool call and return what the model gets back.

    EVERY FAILURE IS A RESULT, NOT AN EXCEPTION. A tool that raises kills the
    turn and costs the whole prefix; a tool that returns "no such file" lets the
    model correct itself in the next round. At ~10 tok/s a lost turn is minutes,
    so the difference is not cosmetic.
    """
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return f"error: arguments were not valid JSON: {arguments[:200]}"
    if not isinstance(args, dict):
        return f"error: arguments must be a JSON object, got {type(args).__name__}"

    impl = TOOL_IMPL.get(name)
    if impl is None:
        return f"error: no tool named {name!r}. Available: {', '.join(sorted(TOOL_IMPL))}"
    try:
        return impl(**args)
    except TypeError as exc:
        return f"error: wrong arguments for {name}: {exc}"
    except Exception as exc:  # a tool must never take the turn down with it
        return f"error: {name} failed: {exc!r}"


class TurnEvents:
    """What `run_turn` reports while ONE USER TURN runs. Twelve prints, named.

    Same seam as `ReplyEvents` one level up: the names say what HAPPENED, not
    what a terminal should do about it. Every method does nothing here, so a
    caller that passes nothing gets a turn that runs in silence -- which is
    what a probe, a test or a batch run wants.

    THESE ARE THE TWELVE TERMINAL LINES THE LOOP CARRIED while it sat in
    cli/crow.py's `repl()` (:2501, 2508, 2516, 2536, 2547, 2561, 2573, 2587,
    2593, 2595, 2611, 2617 of the file before the split). They are eleven
    methods, because "interrupted" fires from two places with the same text:

      turn_failed          :2501  the endpoint said no
      turn_interrupted     :2508  Ctrl+C raised out of the stream
                           :2516  ... or set the flag and returned quietly
      round_finished       :2536  one round is in the conversation
      cache_promise_broken :2547  the start line said "cache warm" and lied
      budget_spent         :2561  the tool budget is gone; one forced round left
      tool_started         :2573  before the call runs, not after
      tool_finished        :2587  it came back
      tool_failed          :2593  ... and the result began with "error: "
      tools_finished       :2595  every call of this round is appended
      rollover_refused     :2611  the window filled twice inside one turn
      rolled_over          :2617  the window filled once and was archived

    `tools_reported` is the one that had no line to move: it belongs to the
    operating mode this stage added, where calls are reported and not run.

    `reply_events` is not a report at all -- it is the sink for the stream
    INSIDE each round, handed down so the two seams stay one decision. None
    means silence there too.
    """

    def reply_events(self) -> "ReplyEvents | None":
        """The sink for this round's stream. None is silence."""
        return None

    def turn_failed(self, message: str) -> None:
        """The endpoint could not be reached, or answered with an error."""

    def turn_interrupted(self) -> None:
        """Ctrl+C. The partial turn was discarded; the context is unchanged."""

    def round_finished(self, timings: dict) -> None:
        """One round is in the conversation. Fires on EVERY round, including
        the forced one, because the line it replaced did too."""

    def cache_promise_broken(self) -> None:
        """The start line claimed a warm cache and the first round disagreed."""

    def budget_spent(self, budget: int) -> None:
        """The tool budget is gone. One forced round follows."""

    def tool_started(self, name: str, arguments: str) -> None:
        """Before the call runs, so a slow call is not silence."""

    def tool_finished(self, name: str, seconds: float, repeated: bool) -> None:
        """The call came back. `repeated` means it was answered from `_SEEN`."""

    def tool_failed(self, name: str, result: str) -> None:
        """The result began with "error: ". The whole result is handed over,
        not its first line: how much of it fits on a screen is the screen's
        decision."""

    def tools_finished(self) -> None:
        """Every call of this round has run and been appended."""

    def tools_reported(self, calls: list[dict]) -> None:
        """`execute_tools=False`: these calls were NOT run and never will be."""

    def rolled_over(self, tokens: int, path: str) -> None:
        """The window filled mid-turn and the conversation was archived."""

    def rollover_refused(self) -> None:
        """Twice in one turn. The question itself does not fit."""


class TurnResult:
    """What one user turn left behind, for the caller that owns the session.

    THE STATE THE LOOP MUTATES IS RETURNED, NOT HIDDEN. `context_tokens`,
    `promised_warm` and `rolled` were local variables of `repl()` that the loop
    wrote through; a second surface has to be able to carry the same three
    across turns or it will resume against numbers that stopped being true.

    `cost` is the DATA behind the one line printed per turn -- its fields and
    their order live in `TurnCost.line`, over here, so that two surfaces cannot
    assemble the same six numbers into two different sentences. The caller
    decides the colour and nothing else.
    """

    def __init__(self, *, cost: TurnCost, context_tokens: int, promised_warm: bool,
                 rolled: bool, stopped: bool, reported: list[dict]) -> None:
        self.cost = cost
        self.context_tokens = context_tokens
        self.promised_warm = promised_warm
        self.rolled = rolled
        # True when the turn ended on an error, an interrupt or a refused second
        # rollover. Those have already said what happened, so the caller's cost
        # line is not printed under them -- it would read like a finished turn.
        self.stopped = stopped
        # Only ever non-empty with execute_tools=False: the calls that were
        # reported instead of run.
        self.reported = reported


def run_turn(
    conversation: Conversation,
    *,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    # NO DEFAULTS FOR THE SAMPLING VALUES, and that is a rule rather than an
    # oversight: manifests/operating-point.json wants each of them written
    # exactly once, in this file, and `stream_reply` below is where that one
    # copy sits. A convenience default here would be a second literal to bump
    # and the one that goes stale is the one no measurement reads.
    top_p: float,
    min_p: float,
    reasoning_effort: str | None = None,
    timeout: float,
    carry: str | None = None,
    context_tokens: int = 0,
    n_ctx: int = 0,
    rollover_at: float = ROLLOVER_AT,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    promised_warm: bool = False,
    rolled: bool = False,
    execute_tools: bool = True,
    # #88. `mode` picks which classes stop and ask; `approve` is how they ask.
    # A surface that passes neither keeps the behaviour every release up to
    # 0.3.1 had: everything runs. A surface that passes a mode but no `approve`
    # gets refusals rather than silent execution -- the safe half of a
    # half-wired client.
    mode: str = DEFAULT_MODE,
    approve: "Callable[[str, str], str] | None" = None,
    events: "TurnEvents | None" = None,
) -> TurnResult:
    """Run one USER turn to its end: however many tool rounds it takes.

    THIS IS THE TOOL LOOP OUT OF `repl()`, moved rather than rewritten. It was
    the one place in this client that did five jobs at once -- read a key, run a
    turn, run tools, archive a full window and price the result -- and the only
    one a second surface could not call. Nothing about the four loop rules
    changed on the way over; what changed is that they are now written once.

    `carry` is the line the user typed, needed only by a mid-turn rollover: the
    archive takes everything up to here and the fresh conversation opens with
    the question, so the model is not left answering a note about a file.

    EXECUTE_TOOLS=FALSE IS AN OPERATING MODE, NOT A DRY RUN. A client that sends
    `tools` in the body gets tool calls back whether it can run them or not --
    and it must send them, because this model's template drops a previous turn's
    thoughts when the array is empty (measured 2026-08-08 over /apply-template:
    132 characters against 132 without tools, 1197 against 1215 with them). The
    rule about what happens to the prefix then is the same rule the budget uses,
    and it lives here rather than in the surface that discovered it: the
    assistant turn is appended WITHOUT its `tool_calls`, so the next turn
    against the same prefix is still valid. The calls are handed to the caller
    through `tools_reported` and `TurnResult.reported`.

    Returns a `TurnResult`. Raises nothing that the loop can name: a `CrowError`
    from the endpoint and a `KeyboardInterrupt` from the user both end the turn
    through `stopped`.
    """
    if events is None:
        events = TurnEvents()

    # THE TOOL LOOP. Everything is appended, never inserted: the assistant
    # turn with its calls, then one `tool` message per call, then the next
    # request. The prefix only grows, so the cache holds across rounds --
    # and this template keeps every reasoning block while tools are present,
    # which is why the loop is affordable at all.
    #
    # CLEARED HERE, NOT BY THE CALLER, and that line moved with the loop on
    # purpose. `_SEEN` is what stops the model asking for the same missing file
    # eight times in one turn; left behind in `repl()` it would go on being
    # cleared for the CLI and never for anybody else, and every later turn of a
    # second surface would be answered out of a stale result, forever.
    #
    # E6 ANSWERED THE LIFETIME QUESTION THIS LINE LEFT OPEN, and the answer put
    # the second name beside it: ONE USER TURN, for both. The measurement behind
    # that choice is written out where `_READ` is declared.
    #
    # THE TWO CLEARS ARE ONE STATEMENT PAIR AND MUST STAY ONE. Split them and
    # the half-state is a live configuration rather than a mistake somebody has
    # to make: `_READ` emptied without `_SEEN` refuses the write correctly while
    # still handing back a tool result from the turn before, and `_SEEN` emptied
    # without `_READ` lets a stale permission outlive the results that earned
    # it. Neither is a state anyone would choose, and neither announces itself.
    _READ.clear()
    _SEEN.clear()
    stopped = False
    cost = TurnCost()
    budget = max_tool_rounds
    reported: list[dict] = []
    # One iteration past the budget, for the forced answer. It is not a tool
    # round -- its calls are discarded -- so it does not quietly hand out a
    # round more than was asked for.
    forced = False
    for round_no in range(budget + 2):
        try:
            reply, reasoning, timings = stream_reply(
                conversation,
                base_url=base_url,
                model=model,
                api_key=api_key,
                temperature=temperature,
                top_p=top_p,
                min_p=min_p,
                reasoning_effort=reasoning_effort,
                timeout=timeout,
                events=events.reply_events(),
            )
        except CrowError as exc:
            events.turn_failed(str(exc))
            stopped = True
            break
        except KeyboardInterrupt:
            # The partial turn is discarded rather than appended: a truncated
            # assistant message would poison the prefix for every later turn.
            INTERRUPT.clear()
            events.turn_interrupted()
            stopped = True
            break

        # The generator returns quietly on an interrupt rather than raising,
        # so the flag is what tells a stopped turn from a finished one.
        if INTERRUPT.is_set():
            INTERRUPT.clear()
            events.turn_interrupted()
            stopped = True
            break

        calls = timings.get("_tool_calls") or []
        # CALLS THAT WILL NEVER RUN ARE NOT APPENDED, and that is not
        # tidiness. An assistant turn whose tool_calls have no `tool` message
        # behind them is a broken prefix for every later turn of the session.
        #
        # Three rounds are in that position, and missing the first one is a
        # mistake the probe caught: the round that SPENDS the budget, whose
        # calls are refused, the forced round after it, whose calls are
        # discarded, and -- since this loop grew an operating mode -- every
        # round of a turn that reports its calls instead of running them.
        # Keeping the text and losing the calls is the only shape that stays
        # valid.
        unanswerable = not execute_tools or forced or (bool(calls) and round_no >= budget)
        conversation.append("assistant", reply, reasoning,
                            tool_calls=None if unanswerable else calls)
        context_tokens = next_context_tokens(context_tokens, timings)
        cost.add_round(timings)
        events.round_finished(timings)

        # THE PROMISE IS SETTLED HERE, BECAUSE HERE IS WHERE IT IS PAID.
        # Everything before this is the server saying it read a file. Only
        # `cached` says whether the prefix it holds is the one being sent,
        # and it is the number the user is charged against: on 2026-08-10 a
        # start that said "cache warm" was followed by `cached 0/21004` and
        # 469.51 s to the first token, with nothing in between admitting it.
        if promised_warm:
            promised_warm = False
            if timings.get("_cached_tokens") == 0:
                events.cache_promise_broken()

        # REPORTED AND NOT RUN. One round only: there is no tool result to feed
        # back, so a second round would ask the same question against the same
        # prefix and get the same answer.
        if not execute_tools:
            if calls:
                reported = list(calls)
                events.tools_reported(reported)
            break

        if forced or not calls:
            break

        # THE BUDGET BUYS TOOL ROUNDS, NOT THE TURN. Until 2026-08-10 this
        # was a bare `break`, and a turn that ran out ended on a bracket:
        # driven live with --max-tool-rounds 0 the model produced 102 tokens,
        # `thinking 100%`, and the user was shown nothing at all -- the reply
        # was a tool request that would never run. One more round, with the
        # tools still declared so the prompt cache holds (#60), spends what
        # is left on saying where things stood.
        if round_no >= budget:
            events.budget_spent(budget)
            conversation.append("user", BUDGET_SPENT)
            forced = True
            continue
        for call in calls:
            # REPORTED BEFORE THE CALL RUNS, and that is the fix rather than a detail (#70).
            # Until here the order on screen was: run the tool, then say what was run. A slow
            # call left the terminal silent for its whole duration with nothing naming what it
            # was waiting on -- and the previous round's six figures as the last thing visible.
            events.tool_started(call["name"], call["arguments"])
            started = time.monotonic()

            # #88: THE LEVEL DECIDES, THE SURFACE ASKS. This loop knows which
            # class a tool is in and whether the level releases it; it does not
            # know how to put a question on a screen, and a core that did would
            # have one implementation per surface. `approve` is that seam --
            # None means nobody can be asked, which is the same answer as "no"
            # for anything the level holds back.
            declined = False
            if (needs_approval(call["name"], mode)
                    and not remembered(call["name"], call["arguments"])):
                answer = "no"
                if approve is not None:
                    answer = approve(call["name"], call["arguments"]) or "no"
                if answer == "always":
                    remember(call["name"], call["arguments"])
                elif answer != "yes":
                    declined = True

            if declined:
                # A REFUSAL IS A RESULT. Same shape as a failed call: the text
                # goes back as the tool message, the round continues, and the
                # prefix stays valid for every later turn. #88 point 1.
                result, repeated = DECLINED, False
            else:
                result, repeated = run_tool_cached(call["name"], call["arguments"])
            took = time.monotonic() - started
            failed = result.startswith("error: ")
            cost.add_tool(took, failed)
            events.tool_finished(call["name"], took, repeated)
            # A FAILED CALL STAYS ON SCREEN even once the model has recovered from it (#70).
            # It is not the user's problem to solve, but it is the reason the turn took longer
            # than it looks like it should have, and a turn that hides its retries reads as
            # slower for no reason.
            if failed:
                events.tool_failed(call["name"], result)
            conversation.append("tool", result, tool_call_id=call["id"])
        events.tools_finished()

        # THE CHECK BELONGS HERE TOO, NOT ONLY BETWEEN TURNS. One tool round
        # has been measured adding 5,253 tokens, and up to MAX_TOOL_ROUNDS of
        # them run without the user typing anything. A turn that starts under
        # the threshold can still walk into the server's wall inside itself,
        # and the wall costs the whole turn.
        #
        # At the end of a round, never in the middle of one: the assistant
        # message and its tool results are both in by now, so what gets
        # archived is a conversation and not half of one.
        if should_roll(context_tokens, n_ctx, rollover_at):
            if rolled:
                # Twice in one turn means the question itself does not fit.
                # Rolling again would archive the note and ask the same
                # thing again, forever.
                events.rollover_refused()
                stopped = True
                break
            archived = roll_over(conversation, base_url, context_tokens, carry=carry)
            if archived:
                events.rolled_over(context_tokens, archived)
                context_tokens = 0
                rolled = True

    return TurnResult(cost=cost, context_tokens=context_tokens,
                      promised_warm=promised_warm, rolled=rolled,
                      stopped=stopped, reported=reported)


def health_url(base_url: str) -> str:
    """Derive the /health URL from an OpenAI-style base URL.

    Its own function because the naive `base_url[:-3] + "health"` produced
    `http://127.0.0.1:8081health` -- a non-numeric port, and an error message
    that pointed at http.client rather than at the caller.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root.rstrip("/") + "/health"


def check_endpoint(base_url: str, timeout: float = 5.0) -> str:
    """Return the server's health status, or raise CrowError."""
    health = health_url(base_url)
    try:
        with urllib.request.urlopen(health, timeout=timeout) as resp:
            return (json.loads(resp.read().decode("utf-8")) or {}).get("status", "?")
    except urllib.error.URLError as exc:
        raise CrowError(f"no endpoint at {health}: {exc.reason}") from exc


def fetch_n_ctx(base_url: str, timeout: float = 5.0) -> int:
    """How many tokens this server's slot holds, or 0 if it will not say.

    Asked once at startup rather than assumed: with -np 4 the server splits
    its context across slots, so the number a client can actually use is not
    the one on the command line. Returns 0 on any failure - the prompt then
    shows a bare count instead of a wrong bar, because a progress bar against
    an invented limit is worse than no bar.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urllib.request.urlopen(root + "/props", timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8")) or {}
    except Exception:
        return 0
    settings = doc.get("default_generation_settings") or {}
    for value in (settings.get("n_ctx"), doc.get("n_ctx")):
        try:
            if value and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


# A GGUF path carries two tails that are not the model's name: the shard counter
# and the quantisation tag. Both come off. Anything the patterns do not
# recognise is left standing rather than guessed at -- a name cut short is worse
# than a name with a suffix, because only one of the two is silent about it.
_GGUF_SHARD = re.compile(r"-\d{4,6}-of-\d{4,6}$")
_GGUF_QUANT = re.compile(r"-(?:UD-)?(?:I?Q\d[A-Z0-9_]*|BF16|F16|F32|MXFP4)$", re.IGNORECASE)


def model_display_name(path: str) -> str:
    """`…\\DeepSeek-V4-Flash-0731-UD-IQ3_XXS-00001-of-00004.gguf` → `DeepSeek-V4-Flash-0731`."""
    name = os.path.basename((path or "").replace("\\", "/").rstrip("/"))
    if name.lower().endswith(".gguf"):
        name = name[: -len(".gguf")]
    name = _GGUF_SHARD.sub("", name)
    return _GGUF_QUANT.sub("", name) or name


def fetch_model_name(base_url: str, timeout: float = 5.0) -> str:
    """What the server actually has OPEN, or "" if it will not say.

    Not `--model`. That one is a label in the request body — `crow` by default —
    and a header that printed it would confirm the client's own argument while
    the server ran something else entirely. Step 2 of the README exists because
    that mix-up costs a measurement, and this line is the cheap half of it.

    Its own request rather than a second return value from fetch_n_ctx: both are
    milliseconds against a local socket, and threading a tuple through would
    change a function three tests already pin.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urllib.request.urlopen(root + "/props", timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8")) or {}
    except Exception:
        return ""
    settings = doc.get("default_generation_settings") or {}
    for value in (doc.get("model_path"), doc.get("model"), settings.get("model")):
        if isinstance(value, str) and value.strip():
            return model_display_name(value)
    return ""


def next_context_tokens(current: int, timings: dict) -> int:
    """How full the window is after this turn.

    THE SERVER IS ASKED, NOT ESTIMATED. Until 2026-08-08 this was
    `context_tokens = prompt_n + predicted_n`, and both halves were wrong:

      * it ASSIGNED rather than accumulated, so the bar showed the last turn
        instead of the session -- measured live, it ran 4.7k -> 1.3k -> 792
        BACKWARDS while the conversation grew;
      * `prompt_n` is the count of tokens the server actually PROCESSED, i.e.
        the uncached remainder. On a warm cache it is near zero precisely
        because things went well: 18 for a prompt that was 4,700 tokens long;
      * `predicted_n` counts everything generated, reasoning included.

    `usage.total_tokens` is the whole conversation as the server's own
    tokeniser counted it -- prompt plus completion, absolute, so assignment is
    now the correct operation. Measured on a two-turn conversation:
    prompt_tokens 29 = cached_tokens 11 + prompt_n 18.

    The fallback exists for endpoints that send no usage block. It accumulates,
    which is right only while the prefix holds -- on a break `prompt_n` counts
    old tokens again and the figure runs high. That is the honest failure
    direction: a bar that overstates makes someone reset early, one that
    understates lets them run into the wall.
    """
    total = timings.get("_context_tokens")
    if isinstance(total, int):
        return total
    prompt_n = timings.get("prompt_n")
    predicted_n = timings.get("predicted_n")
    if prompt_n is None or predicted_n is None:
        return current
    return current + int(prompt_n) + int(predicted_n)


FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# The name a terminal has to be given, NOT the typographic family in the file.
# GoogleSansCode[MONO,wght].ttf is a variable font; Windows resolves it into
# named instances and registers those. Measured 2026-08-07 after installing:
# the families on offer are "Google Sans Code Monospace", "... Proportional",
# "... Medium Monospa" and so on - "Google Sans Code" is not among them, and
# asking for it gets the "font not found" dialog. Name ID 1 of the file says
# "Google Sans Code", which is what made the first attempt wrong.
FONT_FAMILY = "Google Sans Code Monospace"


# What the family covers, measured 2026-08-07 by reading the cmap of
# GoogleSansCode[MONO,wght].ttf v7.001 against Cascadia Mono as a control:
# block elements U+2580-259F 32/32 and box drawing U+2500-257F 128/128 are
# complete, BRAILLE U+2800-28FF is 0 of 256 (Cascadia has all 256). Any banner
# built from braille cells would fall back to a substitute face here, the cell
# advance changes with it, and the drawing comes apart. Block art is safe.


def font_files() -> list[str]:
    if not os.path.isdir(FONT_DIR):
        return []
    return sorted(f for f in os.listdir(FONT_DIR) if f.lower().endswith((".ttf", ".otf")))


def font_installed() -> list[str]:
    """Names of our font files already present in the per-user font store."""
    target = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")
    if not os.path.isdir(target):
        return []
    return [f for f in font_files() if os.path.isfile(os.path.join(target, f))]


# THE ONE BLOCK IN HERE THAT CAN PRINT, and it is named rather than hidden: the
# two `if verbose:` lines below are the only print() calls in this file. Nothing
# in this repository passes verbose=True -- ensure_font() in cli/crow.py calls
# install_font() bare, and the suite calls it bare -- so on every path that
# exists today they are unreachable. A second client that wants the two messages
# takes them as a return code, not as stdout.
def install_font(verbose: bool = False) -> int:
    """Copy the bundled faces into the PER-USER font store and register them.

    Runs on first start, not behind a flag. A typeface nobody knows to ask for
    does not get installed, and asking the user to type a setup command for
    something they never requested is friction with no payoff.

    Per-user on purpose: HKLM and %WINDIR%\\Fonts need elevation, and a chat CLI
    has no business prompting for admin. Windows has honoured the per-user store
    since 10 1809, and nothing outside this account is touched.

    What it does NOT do is select the font. No emulator lets a running program
    set its own typeface - Windows Terminal reads it from settings.json, conhost
    from the registry. Installing makes it choosable; choosing stays with the
    user, which is why the one line printed afterwards says how.
    """
    if os.name != "nt":
        if verbose:
            print("font install is Windows-only; the files are in cli/fonts")
        return 2

    files = font_files()
    if not files:
        if verbose:
            print(f"no font files in {FONT_DIR}")
        return 2

    import shutil
    import winreg

    target = os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts")
    os.makedirs(target, exist_ok=True)
    key = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"

    done = 0
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
        for name in files:
            dst = os.path.join(target, name)
            if not os.path.isfile(dst):
                shutil.copyfile(os.path.join(FONT_DIR, name), dst)
                done += 1
            # The per-user store wants the FULL PATH as the value; the machine
            # store takes a bare filename. Writing a bare name here registers a
            # font Windows then cannot find, and it fails silently.
            winreg.SetValueEx(k, f"{FONT_FAMILY} ({name})", 0, winreg.REG_SZ, dst)
    return 0 if done else 1
