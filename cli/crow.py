#!/usr/bin/env python3
"""Crow CLI -- an interactive chat against Crow's own OpenAI-compatible endpoint.

Phase 6.1 of #45, pulled ahead of the agent core because nothing else is
operable without an input surface.

Two design constraints come from measurements, not taste:

  * The context is APPEND-ONLY (#45 phase 0.2). Nothing is ever inserted in
    front of, or edited inside, an existing message. The prompt cache only
    survives while the prefix stays byte-identical, and at ~11 tok/s a
    re-prefill of 12k tokens costs minutes rather than milliseconds.
  * Output STREAMS. A non-streaming call leaves the user in front of a blank
    terminal for the whole decode.

Standard library only, on purpose: the CLI must run before anything is
installed.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8081/v1"
DEFAULT_MODEL = "crow"
VERSION = "0.0.6"

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


def set_background(colour: str = CROW_BG) -> None:
    """Ask the terminal for our window background via OSC 11.

    A terminal that does not know the sequence drops it silently, so this
    degrades to "the user's own background" rather than to garbage on screen.
    Every path that leaves the CLI calls reset_background(): a program that
    repaints the window and exits without restoring it has broken the terminal
    for whatever runs next.
    """
    if _TTY:
        sys.stdout.write(f"\033]11;{colour}\007")
        sys.stdout.flush()


def reset_background() -> None:
    """Hand the background back. OSC 111 restores the terminal's own value."""
    if _TTY:
        sys.stdout.write("\033]111\007")
        sys.stdout.flush()

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

# The wordmark, drawn rather than typed: no terminal lets a program pick a
# display face, so a banner that should look like anything has to be built out
# of cells. Block elements, not ASCII - measured 2026-08-07, U+2580-259F is
# 32 of 32 in both the bundled Google Sans Code and Cascadia Mono. The shade
# character carries the bevel; painted in a darker blue it reads as depth.
#
# BANNER_ACCENT is the shade cell, so the caller can colour the two apart.
BANNER_SHADE = "▓"
BANNER = """
    ██████  ███████   ██████  ██    ██
   ██▓▓▓▓██ ██▓▓▓▓██ ██▓▓▓▓██ ██▓   ██
   ██▓    ▓▓███████▓▓██▓   ██▓██▓   ██
   ██▓      ██▓▓██▓▓ ██▓   ██▓██▓ ████
   ██▓   ██ ██▓  ██  ██▓   ██▓████████
    ██████▓▓██▓   ██  ██████▓▓███▓▓███
   {version}
"""


def paint_banner(text: str) -> str:
    """Two blues: the face in the wordmark colour, the bevel a few steps down."""
    if not _TTY:
        return text
    shaded = text.replace(BANNER_SHADE, f"{BANNER_BEVEL}{BANNER_SHADE}{CROW_ACCENT}")
    return f"{CROW_ACCENT}{shaded}{RESET}"

# A quarter block travelling the corners: one cell, four frames, and it reads
# as motion because only one quadrant is ever lit.
#
# NOT braille (⠋⠙⠹⠸), which is the usual choice for terminal spinners: the
# bundled Google Sans Code carries 0 of 256 braille codepoints - measured
# 2026-08-07 from its cmap, against Cascadia Mono's 256 of 256 as a control.
# A braille spinner would fall back to a substitute face mid-animation and the
# cell width would jump with it. Block elements are 32 of 32 in both.
SPINNER_FRAMES = ("▘", "▝", "▗", "▖")   # ▘ ▝ ▗ ▖

# BANNER_ACCENT is the shade cell, so the caller can colour the two apart.


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

    Returns a one-line report, or None when there was nothing worth saving.
    """
    if len(conversation) <= (1 if conversation.has_system else 0):
        return None

    path = path or SESSION_FILE
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
        json.dump({"version": VERSION, "kv": saved_kv, "kv_tokens": kv_tokens,
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
    """
    path = path or SESSION_FILE
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        messages = saved.get("messages") or []
    except Exception:
        return None
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
            "User-Agent": f"crow/{VERSION}",
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
    if not latest or not is_newer(latest, VERSION):
        return None
    return (f"{BOLD}crow {latest} is out{RESET} {DIM}(you have {VERSION}){RESET}\n"
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


def highlight(line: str, language: str) -> str:
    """Colour one line of source. Unknown languages come back untouched.

    Line-based on purpose: the reply arrives token by token, and a highlighter
    that needed the whole block would have to buffer the answer to the end -
    which is exactly the blank terminal the streaming design exists to avoid.
    The cost is that a string spanning several lines loses its colour after
    the first; that is a display detail, not a correctness one.
    """
    keywords = _KEYWORDS.get(language.lower())
    if not keywords or not _TTY:
        return line

    def paint(m: "re.Match") -> str:
        kind = m.lastgroup
        text = m.group()
        if kind == "comment":
            return DIM + text + RESET_DIM
        if kind == "string":
            return GREEN + text + RESET
        if kind == "number":
            return CYAN + text + RESET
        if kind == "word" and text in keywords:
            return MAGENTA + text + RESET
        return text

    return _TOKENS.sub(paint, line)


SPILL_DIR = ".crow"
SPILL_AFTER = 18        # lines of a block shown before the rest goes to file

_EXT = {"python": "py", "py": "py", "javascript": "js", "js": "js", "ts": "ts",
        "typescript": "ts", "json": "json", "html": "html", "css": "css",
        "bash": "sh", "sh": "sh", "powershell": "ps1", "sql": "sql"}


class Renderer:
    """Prints a streamed reply, setting fenced code apart from prose.

    STREAMING IS THE CONSTRAINT. Prose is written through character by
    character so the answer appears as it is produced. Only inside a fence is
    output held back to the end of a line, because a highlighter cannot colour
    half a token. A fence therefore costs at most one line of latency.

    NOTHING IS PRINTED IN FRONT OF A CODE LINE. An earlier version drew a
    border and prefixed every line with "| ". It looked tidy and made the code
    unusable: selecting a block in the terminal copies that prefix with it, so
    every paste had to be cleaned by hand. A rule above and below carries the
    same information and stays outside the selection.

    LONG BLOCKS ARE NOT POURED INTO THE SCROLLBACK. Past SPILL_AFTER lines the
    rest goes to a file and the reader is told where. A 300-line answer that
    pushes the whole conversation off screen is not output, it is noise - and
    the file is what the user wanted anyway.
    """

    WIDTH = 76

    def __init__(self, out=None, spill_dir: str | None = None) -> None:
        self._out = out or sys.stdout
        self._buf = ""
        self.in_code = False
        self.language = ""
        self._code_lines = 0
        self._spill_dir = SPILL_DIR if spill_dir is None else spill_dir
        self._spill_path: str | None = None
        self._spill_file = None
        self._pending: list[str] = []
        self.blocks = 0

    def feed(self, text: str) -> None:
        for ch in text:
            if ch == "\n":
                self._line(self._buf)
                self._buf = ""
            else:
                self._buf += ch
                if not self.in_code:
                    # Prose flows immediately; only a fence has to be
                    # recognised before it is echoed, so hold back a line
                    # that still could become one.
                    if not "```".startswith(self._buf.lstrip()[:3]) or self._buf.strip() == "":
                        self._out.write(ch)
                        self._out.flush()
                        self._buf = ""

    def close(self) -> None:
        """Flush the tail and shut an unterminated fence, so a cut-off answer
        cannot leave the block half-open."""
        if self._buf:
            self._line(self._buf)
            self._buf = ""
        if self.in_code:
            self._end_block()

    def _rule(self, text: str) -> None:
        self._out.write(DIM + text + RESET + "\n")
        self._out.flush()

    def _open_spill(self) -> None:
        """Open the file and hand it the lines seen so far.

        Called only once a block passes SPILL_AFTER, not when it opens: a block
        is not known to be long until it is, and writing a file for every
        `pip install psutil` litters the working directory with one-line
        scripts. Nothing is written for short blocks.

        Failure is not fatal - a read-only directory must cost the file, not
        the answer.

        THE NAME IS TAKEN, NOT ASSUMED. `self.blocks` counts within one Renderer
        and a Renderer is built per turn, so it restarts at 1 every turn: turn 2
        wrote block-001 over turn 1's. The number now steps until the name is
        free, which also survives a session started in a directory that already
        holds blocks from an earlier one.
        """
        try:
            os.makedirs(self._spill_dir, exist_ok=True)
            ext = _EXT.get(self.language.lower(), "txt")
            n = self.blocks
            while os.path.exists(os.path.join(self._spill_dir, f"block-{n:03d}.{ext}")):
                n += 1
            self._spill_path = os.path.join(self._spill_dir, f"block-{n:03d}.{ext}")
            self._spill_file = open(self._spill_path, "w", encoding="utf-8")
            for held in self._pending:
                self._spill_file.write(held + "\n")
        except Exception:
            self._spill_path = None
            self._spill_file = None
        finally:
            self._pending = []

    def _end_block(self) -> None:
        note = ""
        # The live counter was written with \r and no newline; close its line
        # before the rule, or the rule lands on top of it.
        if _TTY and self._code_lines > SPILL_AFTER:
            self._out.write("\r\033[2K")
        if self._spill_file is not None:
            try:
                self._spill_file.close()
            except Exception:
                pass
            self._spill_file = None
        if self._spill_path:
            hidden = self._code_lines - SPILL_AFTER
            note = f"  {hidden} more lines -> {self._spill_path}"
        self._rule("-" * self.WIDTH + note)
        self.in_code = False
        self.language = ""
        self._code_lines = 0
        self._spill_path = None
        self._pending = []

    def _line(self, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith("```"):
            if self.in_code:
                self._end_block()
            else:
                self.language = stripped[3:].strip()
                self.blocks += 1
                self._code_lines = 0
                self._pending = []
                self.in_code = True
                label = f" {self.language}" if self.language else " code"
                self._rule("-" * 3 + label + " " + "-" * max(0, self.WIDTH - 5 - len(label)))
            return

        if not self.in_code:
            self._out.write(line + "\n")
            self._out.flush()
            return

        self._code_lines += 1
        # Every code line ends up in the file, whether shown or not, so the
        # saved block is the WHOLE block and not the visible part of it. Until
        # the block is long enough to warrant a file, the lines are held.
        if self._spill_file is not None:
            try:
                self._spill_file.write(line + "\n")
            except Exception:
                pass
        elif self._code_lines == SPILL_AFTER + 1:
            self._open_spill()
            if self._spill_file is not None:
                try:
                    self._spill_file.write(line + "\n")
                except Exception:
                    pass
        else:
            self._pending.append(line)
        if self._code_lines <= SPILL_AFTER:
            self._out.write(highlight(line, self.language) + "\n")
            self._out.flush()
            return

        # Past the cut the block still streams, and it can stream for minutes.
        # Printing nothing at all here looks exactly like a model that stopped
        # mid-block - which is how this first got reported. So the count is
        # rewritten in place: one line, no scrollback, but visibly alive.
        if _TTY:
            self._out.write(f"\r\033[2K{DIM}... writing, {self._code_lines} lines{RESET}")
            self._out.flush()
        elif self._code_lines == SPILL_AFTER + 1:
            self._out.write(DIM + "..." + RESET + "\n")
            self._out.flush()


class Raven:
    """One line: a turning quarter block, a word, and the seconds waited.

    It earns its place: a cold prefill of a long context takes minutes here,
    and a blank terminal is indistinguishable from a hung process. The label
    carries the state - `thinking` while the model reasons, `writing` once it
    starts on the answer - and the whole line erases itself at the end, so it
    never mixes into the reply text.

    One line, not three: it sits where the prompt sits and does not push the
    conversation up the screen on every turn.

    Silent when stdout is not a terminal (pipes, CI, transcript capture).
    """

    HEIGHT = 1

    def __init__(self, stream=None, interval: float = 0.12, label: str = "thinking") -> None:
        self._stream = stream or sys.stdout
        self._interval = interval
        self._label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._drawn = False
        self._enabled = bool(getattr(self._stream, "isatty", lambda: False)()) \
            and os.environ.get("CROW_NO_RAVEN") != "1"

    def set_label(self, label: str) -> None:
        """Change what the bird says while it keeps flapping.

        The label is the only progress signal during a turn: the reasoning is
        no longer printed in full, so without it a 5-minute think and a hung
        process look identical. Assignment is atomic in CPython, so the draw
        thread needs no lock for this.
        """
        self._label = label

    def __enter__(self) -> "Raven":
        if self._enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def _run(self) -> None:
        started = time.monotonic()
        for frame in itertools.cycle(SPINNER_FRAMES):
            if self._stop.is_set():
                return
            waited = time.monotonic() - started
            line = (f"{CROW_ACCENT}{frame}{RESET} {DIM}{self._label} "
                    f"{waited:.1f}s{RESET}")
            with _DRAW_LOCK:
                if self._stop.is_set():
                    return
                if self._drawn:
                    self._stream.write(f"\033[{self.HEIGHT}A")
                self._stream.write(f"\033[2K{line}\n")
                self._stream.flush()
                self._drawn = True
            self._stop.wait(self._interval)

    def stop(self) -> None:
        """Stop the animation and wipe every line it drew.

        The join is short on purpose: this runs from the interrupt path too,
        and a long wait there would swallow the Ctrl+C the user just pressed.
        The thread is a daemon, so an unjoined one cannot hold the process.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.3)
            self._thread = None
        with _DRAW_LOCK:
            if self._drawn:
                self._stream.write(f"\033[{self.HEIGHT}A")
                for _ in range(self.HEIGHT):
                    self._stream.write("\033[2K\n")
                self._stream.write(f"\033[{self.HEIGHT}A")
                self._stream.flush()
                self._drawn = False


_DRAW_LOCK = threading.Lock()


def enable_ansi() -> None:
    """Turn on virtual-terminal processing on the Windows console.

    Windows Terminal handles ANSI already; conhost does not until asked.
    Failure is not fatal -- the raven simply stays off.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
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
        try:
            resp.close()
        except Exception:
            pass


def stream_reply(
    conversation: Conversation,
    *,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    top_p: float = 0.95,
    reasoning_effort: str | None = None,
    timeout: float,
    out=sys.stdout,
    prefix: str = "",
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
    """
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
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    timings: dict = {}
    context_tokens: int | None = None
    cached_tokens: int | None = None
    started = time.monotonic()
    first_token_at: float | None = None
    first_content_at: float | None = None
    in_reasoning = False
    finish_reason: str | None = None

    renderer = Renderer(out=out)
    raven = Raven(stream=out, label="thinking")
    raven.__enter__()

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
                    # Kept, not printed. The reasoning is 60-90 % of every
                    # answer this model gives; printed in full it buries the
                    # code. The bird carries the state instead. Kept rather
                    # than merely counted because the next turn sends it back.
                    reasoning_parts.append(thought)
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
                    if first_content_at is None:
                        first_content_at = now
                        # One frame of the new state before the bird goes, so
                        # the switch from thinking to writing is visible.
                        raven.set_label("writing")
                        time.sleep(raven._interval)
                        raven.stop()
                        if prefix:
                            out.write(prefix)
                    text_parts.append(piece)
                    renderer.feed(piece)
    finally:
        renderer.close()
        raven.stop()

    elapsed = time.monotonic() - started
    # ttft is the FIRST token of any kind. Before 2026-08-07 it was the first
    # content token, so it silently included the whole reasoning decode and
    # read as a prefill several times larger than the one the server reported.
    if first_token_at is not None:
        timings.setdefault("_client_ttft_s", round(first_token_at - started, 2))
    if first_content_at is not None:
        timings.setdefault("_client_answer_s", round(first_content_at - started, 2))
    timings.setdefault("_client_total_s", round(elapsed, 2))
    reasoning = "".join(reasoning_parts)
    if reasoning:
        timings.setdefault("_reasoning_chars", len(reasoning))
        timings.setdefault("_content_chars", sum(len(p) for p in text_parts))
    if finish_reason:
        timings.setdefault("_finish_reason", finish_reason)
    if context_tokens is not None:
        timings.setdefault("_context_tokens", context_tokens)
    if cached_tokens is not None:
        timings.setdefault("_cached_tokens", cached_tokens)
    if tool_calls:
        timings["_tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return "".join(text_parts), reasoning, timings


def format_tool_args(arguments: str | None, width: int = 78) -> str:
    """What a tool call is about, in one line.

    The first version printed the raw JSON cut at 80 characters, which lands mid-string often
    enough to be the normal case: `read_file({"path":"C:\\...\\manifest-runs.ps1","start_line":1,"`.
    That is not a shortened argument list, it is a broken one - the reader cannot tell whether the
    call itself was malformed.

    So the values are shown and the syntax is dropped. Paths are cut from the FRONT, because the
    file name is what identifies the call and the drive letter never does.
    """
    raw = arguments or ""
    try:
        parsed = json.loads(raw)
    except Exception:
        # Not JSON, or not yet complete. Shortening is still better than a hard cut, and the
        # ellipsis says which one happened.
        return raw[:width] + ("..." if len(raw) > width else "")
    if not isinstance(parsed, dict):
        return str(parsed)[:width]

    parts = []
    for key, value in parsed.items():
        if isinstance(value, str):
            # Long text arguments (write_file content, a search pattern) are summarised by length
            # rather than shown: the line is a label, not a transcript.
            if len(value) > 42:
                if "\\" in value or "/" in value:
                    shown = "..." + value[-39:]
                else:
                    shown = f"<{len(value)} chars>"
            else:
                shown = value
        else:
            shown = str(value)
        parts.append(f"{key}={shown}")

    line = ", ".join(parts)
    return line[:width] + ("..." if len(line) > width else "")


def format_timings(timings: dict) -> str:
    """One line of measured cost. Absent numbers are omitted, never invented."""
    bits: list[str] = []
    decoded = timings.get("predicted_n")
    rate = timings.get("predicted_per_second")
    if decoded is not None and rate is not None:
        bits.append(f"{decoded} tok @ {rate:.2f} tok/s")
    elif decoded is not None:
        bits.append(f"{decoded} tok")

    prompt_n = timings.get("prompt_n")
    prompt_rate = timings.get("prompt_per_second")
    if prompt_n is not None and prompt_rate is not None:
        bits.append(f"prefill {prompt_n} @ {prompt_rate:.2f} tok/s")

    # How much of the prompt the server did NOT have to read again. It is the
    # cache working or not working, per turn, from the server rather than
    # inferred -- and this project spent a day finding out the difference.
    cached = timings.get("_cached_tokens")
    if cached is not None and prompt_n is not None:
        bits.append(f"cached {cached}/{cached + int(prompt_n)}")

    ttft = timings.get("_client_ttft_s")
    if ttft is not None:
        bits.append(f"ttft {ttft}s")
    # Separate from ttft on purpose: the gap between them IS the thinking
    # time, and conflating the two is the defect this line was added for.
    answer = timings.get("_client_answer_s")
    if answer is not None and answer != ttft:
        bits.append(f"answer {answer}s")
    rc = timings.get("_reasoning_chars")
    cc = timings.get("_content_chars")
    if rc is not None and cc is not None and (rc + cc) > 0:
        bits.append(f"thinking {100.0 * rc / (rc + cc):.0f}%")
    total = timings.get("_client_total_s")
    if total is not None:
        bits.append(f"total {total}s")
    # Named, not swallowed: a turn cut off at the budget looks exactly like a
    # finished one once the text stops scrolling, and the user needs to know
    # which of the two happened before trusting the answer.
    if timings.get("_finish_reason") == "length":
        bits.append("CUT OFF at the token budget -- raise --max-tokens")
    return " | ".join(bits)


# Read-before-write, and it BLOCKS rather than warns. #10 measured hermes-agent
# resolving this to last-write-wins in two independent code paths: file_state.py
# returns a warning string and file_tools.py performs the write anyway. A model
# that overwrites a file it never read destroys work it cannot see, and at this
# decode rate nobody is watching closely enough to catch it.
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
        return (f"error: refusing to overwrite {path} without reading it first. "
                f"Call read_file on it, then write.")
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
        return f"error: read {path} before editing it"
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


HELP = """commands:
  /help          this list
  /tools         the tools the model can call
  /reset         drop the context (costs a full re-prefill)
  /context       message count in the current context
  /exit, /quit   leave
"""


def _first_sentence(text: str) -> str:
    """The opening sentence of a tool description, for the one-line listing.

    The full descriptions are written for the model and carry the reasoning it
    needs -- when to prefer a range over a whole file, why a write is refused.
    That is the right length in a request and the wrong length in a list.
    """
    head = text.split(". ")[0].strip()
    if not head:
        return ""
    return head if head.endswith(".") else head + "."


def format_tools(tools: list | None = None) -> str:
    """The /tools listing, derived from TOOLS instead of written beside it.

    A hand-maintained list drifts from the schema the model is actually sent,
    and the drift surfaces as a user calling for a tool that was renamed two
    versions ago. Everything below is read out of the same structure that goes
    into the request, so the two cannot disagree.

    Required arguments are bare, optional ones in brackets -- the same shape a
    usage line has everywhere else.
    """
    tools = TOOLS if tools is None else tools
    rows = []
    for entry in tools:
        fn = entry["function"]
        params = fn.get("parameters", {})
        required = list(params.get("required", []))
        optional = [p for p in params.get("properties", {}) if p not in required]
        signature = " ".join(required + [f"[{p}]" for p in optional])
        rows.append((fn["name"], signature, _first_sentence(fn.get("description", ""))))

    if not rows:
        return "no tools are registered.\n"

    name_w = max(len(name) for name, _, _ in rows)
    sig_w = max(len(sig) for _, sig, _ in rows)
    plural = "tool" if len(rows) == 1 else "tools"
    lines = [f"{len(rows)} {plural}, called by the model itself -- "
             f"there is nothing here to switch on:", ""]
    for name, signature, summary in rows:
        lines.append(f"  {BOLD}{name.ljust(name_w)}{RESET} "
                     f"{DIM}{signature.ljust(sig_w)}{RESET}  {summary}")
    lines.append("")
    return "\n".join(lines)


def format_prompt(context_tokens: int, n_ctx: int = 0) -> str:
    """The input prompt, carrying how full the context is.

    On an append-only context the number only ever grows, and it is what the
    next prefill will cost. With the server's n_ctx known it also shows how
    close the wall is - running into it mid-session costs the whole
    conversation, and a bare token count does not warn anybody.

    ASCII bar, deliberately: block-drawing characters are exactly what the
    cp1252 fallback mangles.
    """
    if context_tokens <= 0:
        return f"{BOLD}{CROW_ACCENT}you>{RESET} "

    if context_tokens < 1000:
        size = str(context_tokens)
    else:
        size = f"{context_tokens / 1000:.1f}k"

    if n_ctx > 0:
        share = min(1.0, context_tokens / n_ctx)
        filled = int(round(share * 10))
        # Colour is the warning, not the number: green while there is room,
        # yellow past half, red past 85 % where a reset is imminent.
        colour = GREEN if share < 0.5 else (YELLOW if share < 0.85 else RED)
        bar = colour + "#" * filled + DIM + "-" * (10 - filled) + RESET
        limit = f"{n_ctx / 1000:.0f}k"
        return f"{DIM}[{RESET}{bar}{DIM}]{RESET} {size}/{limit} {DIM}|{RESET} {BOLD}{CROW_ACCENT}you>{RESET} "

    return f"{size} {DIM}|{RESET} {BOLD}{CROW_ACCENT}you>{RESET} "


# READING A LINE ONE KEY AT A TIME.
#
# input() cannot colour a line while it is being typed: the console stays in
# cooked mode, the terminal does the echo, and nothing reaches this process
# until Enter. Turning that around is the entire cost of the feature -- the
# echo, backspace, Ctrl+C and Ctrl+D all become this file's job.
#
# It is a fallback, not a requirement. Piped input, a dumb terminal, a platform
# without msvcrt or termios: every one of those drops back to input(), where
# the colours are off anyway because _TTY is false.
@contextlib.contextmanager
def _raw_keys():
    """Yield a reader for single keystrokes, or None when raw mode is not available.

    The reader returns "" for a key that carries no character -- arrows and
    function keys. Swallowing their second half here keeps the caller from
    seeing a stray 'H' when someone presses Up.
    """
    if not _TTY:
        yield None
        return

    try:
        import msvcrt
    except ImportError:
        pass
    else:
        def read_windows() -> str:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()
                return ""
            return ch

        yield read_windows
        return

    try:
        import select
        import termios
        import tty
    except ImportError:
        yield None
        return

    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:
        yield None
        return

    def read_posix() -> str:
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # Drain the rest of the sequence without blocking: a bare Esc has
            # nothing after it, and read(2) would hang waiting for a key.
            while select.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.read(1)
            return ""
        return ch

    try:
        tty.setraw(fd)
        yield read_posix
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_coloured(prompt: str, getch, out=None) -> str:
    """Echo what is typed, in yellow once the line starts with '/'.

    The colour is emitted at the transition -- before the '/' is echoed, not
    after -- so the slash itself is yellow and nothing already on screen has to
    be repainted. That is deliberate: repainting means rewriting the line, and
    a line long enough to wrap cannot be rewritten with a carriage return.

    Raises KeyboardInterrupt on Ctrl+C and EOFError on Ctrl+D at an empty line,
    because in raw mode the console no longer raises them for us and the caller
    above already knows what to do with both.
    """
    out = sys.stdout if out is None else out
    out.write(prompt)
    out.flush()
    buffer = ""
    yellow = False
    try:
        while True:
            ch = getch()
            if ch == "":
                continue
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x04":
                if not buffer:
                    raise EOFError
                continue
            if ch in ("\x08", "\x7f"):
                if buffer:
                    buffer = buffer[:-1]
                    out.write("\b \b")
                    if yellow and not buffer.startswith("/"):
                        out.write(CROW_TEXT)
                        yellow = False
                out.flush()
                continue
            if ch < " ":
                continue

            if (buffer + ch).startswith("/") != yellow:
                yellow = not yellow
                out.write(YELLOW if yellow else CROW_TEXT)
            buffer += ch
            out.write(ch)
            out.flush()
    finally:
        out.write(RESET)
        out.flush()
    out.write("\r\n")
    out.flush()
    return buffer


def read_line(prompt: str) -> str:
    """input(), except that a slash command turns yellow as it is typed."""
    with _raw_keys() as getch:
        if getch is None:
            return input(prompt)
        return read_coloured(prompt, getch)


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


def repl(args: argparse.Namespace) -> int:
    enable_ansi()
    install_interrupt_handler()
    # First thing, before anything is drawn: the request then runs while the
    # banner, the font check and /health do their work, and is usually answered
    # by the time anyone looks at the queue.
    updates = start_update_check(getattr(args, "update_check", True))
    if getattr(args, "background", True):
        set_background()
    print(paint_banner(BANNER.format(version=f"v{VERSION}")))

    # Before the endpoint check, not after: the font has nothing to do with the
    # server. Behind the check it would never install on a machine where the
    # user starts the CLI before llama-server, which is the normal order.
    if getattr(args, "font", True):
        ensure_font()

    # Above the endpoint check, because an out-of-date client that also cannot
    # reach its server should say both things -- and the version line is the one
    # that might explain the other.
    notice = update_notice(updates)
    if notice:
        print(notice)
        print("")

    try:
        status = check_endpoint(args.base_url)
    except CrowError as exc:
        print(f"crow: {exc}", file=sys.stderr)
        print("crow: start llama-server first, then retry.", file=sys.stderr)
        reset_background()
        return 2

    n_ctx = fetch_n_ctx(args.base_url)
    room = f", {n_ctx / 1000:.0f}k context" if n_ctx else ""
    print(f"{BOLD}{args.model}{RESET} at {args.base_url} "
          f"{DIM}(health: {status}{room}){RESET}")
    print(f"{DIM}/help for commands, /tools for what the model can call, "
          f"/exit to leave.{RESET}")
    # Its own line rather than appended to the one above: together they run past
    # 80 columns, and a wrapped header is the first thing a new user sees.
    print(f"{DIM}{REPO_URL}{RESET}")
    print("")

    conversation = Conversation(args.system)
    # Tokens the server actually charged for last turn, not an estimate: it
    # reports prompt_n (everything it re-read) and predicted_n (what it added).
    # Their sum is what the NEXT turn starts from. Counting characters here
    # instead would drift from the tokeniser and quietly mislead.
    context_tokens = 0
    # Set when the start line CLAIMED a warm cache. The claim is only settled by
    # the first turn, and it is cleared there whichever way it goes.
    promised_warm = False

    # Before the first turn, so no prefix exists yet to break.
    wanted = getattr(args, "resume", None)
    if getattr(args, "session", True) or wanted:
        source = resume_path(wanted) if wanted else None
        restored = load_session(args.base_url, args.system, path=source)
        if restored:
            messages, context_tokens, kv = restored
            conversation.restore(messages)
            promised_warm = kv
            how = ("cache warm" if kv else
                   "messages only -- the first turn pays a prefill")
            where = f" from {os.path.basename(source)}" if source else ""
            print(f"{DIM}resumed{where}: {len(messages)} messages, {how}{RESET}\n")
        elif wanted:
            # Named explicitly and not there: silence would look like an empty
            # archive rather than a wrong name, and the user would go looking in
            # the wrong place.
            print(f"crow: no session at {source}", file=sys.stderr)
            reset_background()
            return 2

    def leave() -> int:
        if getattr(args, "session", True):
            note = save_session(conversation, args.base_url, context_tokens)
            if note:
                print(f"{DIM}{note}{RESET}")
        return 0

    while True:
        try:
            line = read_line(format_prompt(context_tokens, n_ctx)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return leave()

        if not line:
            continue
        if line in ("/exit", "/quit"):
            return leave()
        if line == "/help":
            print(HELP)
            continue
        if line == "/tools":
            print(format_tools())
            continue
        if line == "/reset":
            conversation.reset()
            context_tokens = 0
            print("context dropped -- the next turn pays a full prefill.\n")
            continue
        if line == "/context":
            # The rollover point is shown here or nowhere: it is the number that
            # decides when the conversation ends, and a user who cannot see it
            # cannot plan around it.
            enabled = n_ctx > 0 and args.rollover_at > 0
            room = f", rolls over at {int(n_ctx * args.rollover_at)}" if enabled else ""
            print(f"{len(conversation)} messages, {context_tokens} tokens{room}\n")
            continue
        # BEFORE the turn is appended, not after: the archive is then a complete
        # conversation, and the question the user just typed opens the new one
        # instead of being the last thing in a file nobody reads.
        rolled = False
        if should_roll(context_tokens, n_ctx, args.rollover_at):
            archived = roll_over(conversation, args.base_url, context_tokens, carry=line)
            if archived:
                # Printed while context_tokens still HOLDS the number. Zeroing
                # first and interpolating after is how this reads "archived at
                # 0 tokens" forever.
                print(f"{DIM}context rolled over at {context_tokens} tokens -- archived to "
                      f"{archived}{RESET}\n")
                context_tokens = 0
                rolled = True
        if not rolled:
            conversation.append("user", line)
        print()
        # Cleared per turn: an interrupt from the PREVIOUS turn must not kill
        # the next one before it starts.
        INTERRUPT.clear()

        # THE TOOL LOOP. Everything is appended, never inserted: the assistant
        # turn with its calls, then one `tool` message per call, then the next
        # request. The prefix only grows, so the cache holds across rounds --
        # and this template keeps every reasoning block while tools are present,
        # which is why the loop is affordable at all.
        _SEEN.clear()
        stopped = False
        budget = args.max_tool_rounds
        # One iteration past the budget, for the forced answer. It is not a tool
        # round -- its calls are discarded -- so it does not quietly hand out a
        # round more than was asked for.
        forced = False
        for round_no in range(budget + 2):
            try:
                reply, reasoning, timings = stream_reply(
                    conversation,
                    base_url=args.base_url,
                    model=args.model,
                    api_key=args.api_key,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    reasoning_effort=args.reasoning_effort,
                    timeout=args.timeout,
                    prefix=f"{BOLD}{CROW_TEXT}crow>{RESET} ",
                )
            except CrowError as exc:
                print(f"\ncrow: {exc}\n", file=sys.stderr)
                stopped = True
                break
            except KeyboardInterrupt:
                # The partial turn is discarded rather than appended: a truncated
                # assistant message would poison the prefix for every later turn.
                INTERRUPT.clear()
                print("\n[interrupted -- turn discarded, context unchanged]\n")
                stopped = True
                break

            # The generator returns quietly on an interrupt rather than raising,
            # so the flag is what tells a stopped turn from a finished one.
            if INTERRUPT.is_set():
                INTERRUPT.clear()
                print("\n[interrupted -- turn discarded, context unchanged]\n")
                stopped = True
                break

            calls = timings.get("_tool_calls") or []
            # CALLS THAT WILL NEVER RUN ARE NOT APPENDED, and that is not
            # tidiness. An assistant turn whose tool_calls have no `tool` message
            # behind them is a broken prefix for every later turn of the session.
            #
            # Two rounds are in that position, and missing the first one is a
            # mistake the probe caught: the round that SPENDS the budget, whose
            # calls are refused, and the forced round after it, whose calls are
            # discarded. Keeping the text and losing the calls is the only shape
            # that stays valid.
            unanswerable = forced or (bool(calls) and round_no >= budget)
            conversation.append("assistant", reply, reasoning,
                                tool_calls=None if unanswerable else calls)
            context_tokens = next_context_tokens(context_tokens, timings)
            line_out = format_timings(timings)
            print(f"\n\n[{line_out}]\n" if line_out else "\n")

            # THE PROMISE IS SETTLED HERE, BECAUSE HERE IS WHERE IT IS PAID.
            # Everything before this is the server saying it read a file. Only
            # `cached` says whether the prefix it holds is the one being sent,
            # and it is the number the user is charged against: on 2026-08-10 a
            # start that said "cache warm" was followed by `cached 0/21004` and
            # 469.51 s to the first token, with nothing in between admitting it.
            if promised_warm:
                promised_warm = False
                if timings.get("_cached_tokens") == 0:
                    print(f"{DIM}[the restored cache did not hold -- that prefill was the whole "
                          f"conversation, not a resume]{RESET}\n")

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
                print(f"{DIM}[tool budget spent after {budget} rounds -- answering from what it "
                      f"has; --max-tool-rounds raises it]{RESET}\n")
                conversation.append("user", BUDGET_SPENT)
                forced = True
                continue
            for call in calls:
                arg_note = format_tool_args(call["arguments"])
                result, repeated = run_tool_cached(call["name"], call["arguments"])
                mark = " (repeat)" if repeated else ""
                print(f"{DIM}  ⚒ {call['name']}({arg_note}){mark}{RESET}")
                if result.startswith("error: "):
                    print(f"{DIM}    {result.splitlines()[0]}{RESET}")
                conversation.append("tool", result, tool_call_id=call["id"])
            print("")

            # THE CHECK BELONGS HERE TOO, NOT ONLY BETWEEN TURNS. One tool round
            # has been measured adding 5,253 tokens, and up to MAX_TOOL_ROUNDS of
            # them run without the user typing anything. A turn that starts under
            # the threshold can still walk into the server's wall inside itself,
            # and the wall costs the whole turn.
            #
            # At the end of a round, never in the middle of one: the assistant
            # message and its tool results are both in by now, so what gets
            # archived is a conversation and not half of one.
            if should_roll(context_tokens, n_ctx, args.rollover_at):
                if rolled:
                    # Twice in one turn means the question itself does not fit.
                    # Rolling again would archive the note and ask the same
                    # thing again, forever.
                    print(f"{DIM}[the window filled again inside this turn -- stopping here."
                          f" Ask for a narrower slice, or /reset]{RESET}\n")
                    stopped = True
                    break
                archived = roll_over(conversation, args.base_url, context_tokens, carry=line)
                if archived:
                    print(f"{DIM}context rolled over at {context_tokens} tokens mid-turn -- "
                          f"archived to {archived}{RESET}\n")
                    context_tokens = 0
                    rolled = True

        if stopped:
            continue


FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# The name a terminal has to be given, NOT the typographic family in the file.
# GoogleSansCode[MONO,wght].ttf is a variable font; Windows resolves it into
# named instances and registers those. Measured 2026-08-07 after installing:
# the families on offer are "Google Sans Code Monospace", "... Proportional",
# "... Medium Monospa" and so on - "Google Sans Code" is not among them, and
# asking for it gets the "font not found" dialog. Name ID 1 of the file says
# "Google Sans Code", which is what made the first attempt wrong.
FONT_FAMILY = "Google Sans Code Monospace"

# Values we wrote ourselves in earlier versions and may correct without asking.
# Anything else in the user's settings is their choice and stays.
_OUR_OLD_FACES = frozenset({"Google Sans Code"})
_OUR_OLD_BACKGROUNDS = frozenset({"#0b0e17"})

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


def _terminal_settings_paths() -> list[str]:
    """Where Windows Terminal keeps settings.json - store build, then unpackaged."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return []
    found = []
    pkgs = os.path.join(local, "Packages")
    if os.path.isdir(pkgs):
        for name in os.listdir(pkgs):
            if name.startswith("Microsoft.WindowsTerminal"):
                p = os.path.join(pkgs, name, "LocalState", "settings.json")
                if os.path.isfile(p):
                    found.append(p)
    p = os.path.join(local, "Microsoft", "Windows Terminal", "settings.json")
    if os.path.isfile(p):
        found.append(p)
    return found


def _strip_jsonc(text: str) -> str:
    """Windows Terminal ships settings.json WITH comments; json.loads chokes on
    them. Stripping is done outside string literals only - a // inside a path
    like "C:\\\\x" or a URL must survive."""
    out, i, n = [], 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def set_terminal_look() -> str | None:
    """Set our face and background as the defaults in Windows Terminal.

    Returns the settings path if anything was written, else None.

    Yes, this edits the user's settings.json. It is the only way either setting
    reaches the screen: installing a font merely makes it choosable, and OSC 11
    - the escape sequence that asks a terminal to repaint its background - is
    not honoured here (measured 2026-08-07: sent at startup, the window stayed
    black). A .bak of the original is written before anything changes.

    Only profiles.defaults.font.face and profiles.defaults.background are
    touched. A value that is already ours is left alone; any OTHER value is a
    decision the user made on purpose and is never overwritten.
    """
    for path in _terminal_settings_paths():
        try:
            raw = open(path, encoding="utf-8-sig").read()
            data = json.loads(_strip_jsonc(raw))
            profiles = data.setdefault("profiles", {})
            if isinstance(profiles, list):        # very old schema
                continue
            defaults = profiles.setdefault("defaults", {})
            changed = False

            font = defaults.get("font")
            if not isinstance(font, dict):
                font = {}
            face = font.get("face")
            if face != FONT_FAMILY and (not face or face in _OUR_OLD_FACES):
                font["face"] = FONT_FAMILY
                defaults["font"] = font
                changed = True

            bg = defaults.get("background")
            if bg != CROW_BG and (not bg or bg.lower() in _OUR_OLD_BACKGROUNDS):
                defaults["background"] = CROW_BG
                changed = True

            if not changed:
                return None
            with open(path + ".bak", "w", encoding="utf-8") as f:
                f.write(raw)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            return path
        except Exception:
            continue
    return None


def ensure_font() -> None:
    """First start: install the font AND select it, then say what happened.
    Silent on every later start.

    Never raises. A missing font must not keep the CLI from starting - it is a
    typeface, not a dependency.
    """
    try:
        if os.name != "nt" or not font_files():
            return
        # Two separate questions, checked apart. Tying the profile write to the
        # install meant that a version which registered the WRONG face name
        # could never correct it: the files were already there, so the whole
        # block was skipped and the terminal kept asking for a font that does
        # not exist. Both steps are cheap and idempotent.
        if not font_installed() and install_font() == 0:
            print(f"{DIM}installed {FONT_FAMILY} (SIL OFL 1.1, cli/fonts/OFL.txt){RESET}")
        written = set_terminal_look()
        if written:
            print(f"{DIM}set {FONT_FAMILY} and the background as Windows Terminal defaults "
                  f"(backup: {os.path.basename(written)}.bak) - restart the terminal{RESET}")
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crow",
        description="Interactive chat against Crow's OpenAI-compatible endpoint.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"endpoint base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL,
                        help=f"model name sent to the endpoint (default: {DEFAULT_MODEL})")
    parser.add_argument("--api-key", default="local-no-provider",
                        help="placeholder key; the local server does not check it")
    parser.add_argument("--system", default=DEFAULT_SYSTEM,
                        help="system prompt; stays byte-identical for the whole session")
    parser.add_argument("--no-system", dest="system", action="store_const", const=None,
                        help="send no system prompt at all (the model then picks its own language)")
    # 1.0 is what DeepSeek-V4-Flash-0731 specifies: the model card runs its
    # agentic benchmarks at temperature 1.0 / top_p 0.95, and its
    # generation_config.json says temperature 1.0 too. (0.6 was the PREVIEW
    # family's value; it shipped in every release up to 0.0.6.)
    # 0.0 stays dangerous either way: greedy is where reasoning models loop --
    # measured 2026-08-07 on a three.js task, the model repeated "Actually,
    # let me..." inside its reasoning block and never reached the answer.
    # Measurement runs that need byte-identical output pass --temperature 0
    # explicitly; the interactive default has to be able to finish a turn.
    parser.add_argument("--temperature", type=float, default=1.0)
    # The card and the generation_config disagree here: the card's agentic runs
    # use 0.95, generation_config.json says 1.0. Crow is an agent, so the
    # agentic figure wins -- but the disagreement is real and belongs next to
    # the number rather than in anyone's memory.
    parser.add_argument("--top-p", dest="top_p", type=float, default=0.95)
    # Lands in the chat template, not the sampler. None sends nothing and the
    # template falls back to "low" on its own; the flag exists so E12 can
    # measure what the levels actually cost.
    parser.add_argument("--reasoning-effort", dest="reasoning_effort",
                        choices=("low", "high", "max"), default=None)
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="socket timeout in seconds (default: 1800)")
    parser.add_argument("--no-font", dest="font", action="store_false",
                        help=f"do not install the bundled {FONT_FAMILY} on first start")
    parser.add_argument("--no-background", dest="background", action="store_false",
                        help="leave the terminal background alone")
    parser.add_argument("--version", action="version", version=f"crow {VERSION}",
                        help="print the version and exit")
    parser.add_argument("--no-update-check", dest="update_check", action="store_false",
                        help="do not ask GitHub whether a newer release exists")
    parser.add_argument("--no-session", dest="session", action="store_false",
                        help="do not resume the last session, and do not save this one")
    # --resume, not --session <file>: --no-session already owns dest="session"
    # as a flag, and one name that is both a switch and a path is how a parser
    # starts lying about what it accepts.
    parser.add_argument("--resume", metavar="FILE",
                        help="resume this session file instead of the last one; a bare name is"
                             f" looked for in {SESSION_DIR}")
    parser.add_argument("--rollover-at", dest="rollover_at", type=float, default=ROLLOVER_AT,
                        metavar="SHARE",
                        help="archive the conversation and start a fresh one at this share of"
                             f" the window, 0 to switch it off (default: {ROLLOVER_AT})")
    # Raising this raises what ONE turn can add to the window: 24 rounds were
    # measured on 2026-08-10 growing a turn by 28,900 tokens, which is more than
    # the 20,000 that --rollover-at 0.9 leaves between the threshold and the
    # wall. The two settings are one setting with two names.
    parser.add_argument("--max-tool-rounds", dest="max_tool_rounds", type=int,
                        default=MAX_TOOL_ROUNDS, metavar="N",
                        help="how many tool rounds one turn may take before it stops"
                             f" (default: {MAX_TOOL_ROUNDS}, 0 answers without running any)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # finally, not a plain call after repl(): an unhandled exception or a
    # Ctrl+C that escapes the loop must not leave the user's terminal painted
    # in our background for whatever they run next.
    try:
        return repl(args)
    finally:
        reset_background()


if __name__ == "__main__":
    sys.exit(main())


