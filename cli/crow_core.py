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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
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

# ---- #96, web research ----------------------------------------------------
# THE BUDGET HERE IS ROUND TRIPS, NOT REQUESTS. A fetched page is clipped to
# MAX_TOOL_BYTES, which the comment above prices at ~4,000 tokens and ~two
# minutes of prefill. MAX_TOOL_ROUNDS is 24 for the whole turn. Fetching five
# results one after another is ten minutes and a fifth of the turn spent before
# an answer begins -- so the search result has to carry snippets worth reading on
# their own, and the number of pages one question may open has to be bounded
# rather than left to the model's enthusiasm.
WEB_TIMEOUT = 20
WEB_MAX_DOWNLOAD = 2_000_000   # bytes taken off the socket before extraction
MAX_FETCHES = 4                # pages one question may open; see the note above
SEARCH_RESULTS = 6

# WHERE THE SEARCH RUNS, AND WHY IT IS NOT A SELF-HOSTED SERVICE BY DEFAULT.
#
# There is no free, keyless, reliable, general web search endpoint. Every route
# costs exactly one of four things: money, an account, a service you run, or
# scraping an engine that does not offer an API. Measured 2026-08-14, and the
# first draft of this file got it wrong by choosing the third:
#
# * self-hosted SearXNG is free and keyless, and it is NOT SHIPPABLE as the
#   default. Crow installs with one line of PowerShell onto plain Windows. A
#   default that requires the user to install Docker or WSL and keep a second
#   service running is a capability for whoever built it, not for whoever
#   installed it.
# * public SearXNG instances do not fill the gap: six were probed on
#   2026-08-14 (searx.be, search.inetol.net, priv.au, searxng.site,
#   search.bus-hit.me, baresearch.org) and not one answered `format=json` --
#   200 with HTML, 429, or 403. Their own docs say as much.
# * scraping duckduckgo is against its terms and returns 202/403 as the normal
#   case, which in a 24-round loop is a tool that fails at random.
#
# What is left is the cheapest real cost: THE USER BRINGS A KEY. Tavily's free
# tier is 1,000 searches a month and takes no credit card, so nothing is paid
# for and nothing has to run. The key is the whole setup, and if it is missing
# the tool says so in the one sentence that fixes it.
#
# SearXNG stays as the second provider -- unlimited and account-free for anyone
# who already runs one. Setting CROW_SEARXNG_URL picks it. Its JSON is off until
# turned on: searxng ships `formats: [html]`
# (docs/admin/settings/settings_search.rst), so `format=json` answers with a
# page until `json` joins that list, which is the common first failure and gets
# its own sentence rather than a JSONDecodeError.
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_KEY = os.environ.get("CROW_TAVILY_KEY", "")
SEARXNG_URL = os.environ.get("CROW_SEARXNG_URL", "")

# Per keyless source. Shorter than WEB_TIMEOUT because several run at once and
# the slowest one sets the pace: a source that has not answered in 8 s has cost
# more than its results are worth.
KEYLESS_TIMEOUT = 8

# WHAT AN EMPTY KEYLESS SEARCH SAYS, and it is a hint rather than an error: the
# federation covers repositories, issues, answered questions, packages and
# encyclopaedia entries, which is most of what a coding assistant needs and not
# the open web. A question outside that is a reason to offer the upgrade, not to
# report a fault.
NO_GENERAL_INDEX = (
    "\n[these sources cover code, packages and reference, not the open web. "
    "For a general index set CROW_TAVILY_KEY (free, no credit card, "
    "https://tavily.com) or CROW_SEARXNG_URL to your own instance.]"
)

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


# WHERE THE SHIPPED MANIFEST SITS, SEEN FROM THIS FILE, and it is one path
# rather than two. The repo has cli/crow_core.py beside manifests/; an install
# has <install>\cli beside <install>\manifests. So the same `..` answers in both
# places and there is no "am I installed?" branch to get wrong -- that branch is
# the one that would be right on this machine and wrong on everybody else's.
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, "manifests", "operating-point.json")


def _manifest() -> dict:
    """The shipped manifest, or {} if it is not there.

    NOT AN ERROR WHEN MISSING, and that is deliberate rather than lax. Every
    installation from 0.0.1 to 0.5.1 has no manifest beside its cli/, and a
    client that refused to start without one would break every one of them on
    upgrade. What a missing file costs is named at the call sites: the model
    keeps the three defaults below and sends no top_k, which is exactly what
    this client did before #112.
    """
    try:
        with open(MANIFEST_PATH, encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def model_key_for(model: str | None, manifest: dict | None = None) -> str | None:
    """Which key in models.entries names the model the server has open.

    DERIVED, NOT LISTED. The pairing is `model_display_name` applied to the
    entry's own path -- the same function the header line already runs on what
    /props reports -- so a table with a new model in it needs no second list
    here, and a list here could not go stale against it. A hand-kept map would
    be green about exactly the model nobody remembered to add.

    Returns None when nothing matches, which is the honest answer for a server
    pointed at a GGUF this repo has never measured.
    """
    if not model:
        return None
    entries = (((manifest if manifest is not None else _manifest())
                .get("models") or {}).get("entries") or {})
    for key, entry in entries.items():
        if model_display_name((entry or {}).get("path") or "") == model:
            return key
    return None


def sampling_for(model: str | None) -> dict:
    """The sampling values for one model: the shared block, then its own.

    THE OVERRIDE CARRIES ONLY WHAT DIFFERS, and the reason is the checker rather
    than taste. tools/check_operating_point.py COUNTS the places that write a
    sampling default and allows exactly one, in this file. A per-model table in
    here spelling `"min_p": 0.0` beside `MIN_P = 0.01` would be a second write
    site for min_p and would go red -- correctly, because that is the shape two
    clients drift in. So the numbers that differ per model live in the manifest,
    where they are data, and this file keeps one literal each as the floor.

    THE FLOOR IS WHAT THIS CLIENT ALWAYS SENT. With no manifest, no match, or a
    model with no override, the answer is the same three values and no top_k --
    so the change cannot alter what an existing installation puts on the wire.
    """
    out = {"temperature": TEMPERATURE, "top_p": TOP_P, "min_p": MIN_P}
    manifest = _manifest()
    blocks = [manifest.get("sampling") or {}]
    key = model_key_for(model, manifest)
    if key:
        entry = ((manifest.get("models") or {}).get("entries") or {}).get(key) or {}
        blocks.append(entry.get("sampling") or {})
    for block in blocks:
        for name, value in block.items():
            # FILTERED AGAINST A FIXED LIST, and not merely against a leading
            # underscore. The result of this function is splatted into
            # `run_turn(**sampling)` by the terminal, so a manifest that grew a
            # new numeric field would become an unexpected keyword argument and
            # take down the turn. A field this build does not know is data it
            # has no wire for, which is not the same as a mistake.
            if name in SAMPLING_FIELDS and isinstance(value, (int, float)):
                out[name] = value
    return out


def resolve_sampling(model: str | None, overrides: dict | None = None) -> dict:
    """The model's sampling, with anything the user typed on top.

    `overrides` carries ONLY what was actually given -- see `_Explicit` in
    cli/crow.py. A dict of every flag with its default would put the terminal's
    idea of min_p back on top of the model's and undo the whole stage.
    """
    out = sampling_for(model)
    for name, value in (overrides or {}).items():
        if name in SAMPLING_FIELDS and value is not None:
            out[name] = value
    return out


def reasoning_problem(model: str | None, level: str | None) -> str | None:
    """Why this level cannot be used on this model, or None if it can.

    A STRING RATHER THAN A RAISE OR A BOOL: the caller has to print it, and the
    two things worth saying -- which level was asked for and which ones exist --
    are only knowable here. A bool would send the reader to the manifest to find
    out what they should have typed.
    """
    if level is None:
        return None
    levels = reasoning_levels_for(model)
    if level in levels:
        return None
    return ("--reasoning-effort %s is not one of %s for %s"
            % (level, ", ".join(levels), model or "this model"))


def reasoning_levels_for(model: str | None) -> tuple[str, ...]:
    """What --reasoning-effort may be for this model, in the manifest's order.

    WHY THIS IS NOT AN argparse `choices` LIST. The levels differ per model and
    the model is not known until /props has answered, which happens long after
    the command line is parsed. So the parser takes the union of everything any
    model allows and the refusal happens once the model IS known -- naming the
    model and its levels, rather than rejecting a word that is perfectly valid
    for the server the user is about to point at.

    Measured 2026-08-20 (#108): against unsloth's template `max` RAISES, and
    `high`, `xhigh` and the unset case render byte-identically. One of the three
    levels this client offered before #112 was fatal on the second model.
    """
    manifest = _manifest()
    key = model_key_for(model, manifest)
    entry = (((manifest.get("models") or {}).get("entries") or {}).get(key) or {}) if key else {}
    levels = entry.get("reasoning_levels")
    if isinstance(levels, list) and levels:
        return tuple(str(x) for x in levels)
    return REASONING_LEVELS


# The union, for the parser. Not a claim that every level works on every model:
# that is what reasoning_levels_for answers, once there is a model to ask about.
REASONING_LEVELS = ("low", "medium", "high", "max")

# What a request may carry from the manifest, and nothing else. The list is here
# rather than derived from the manifest because it is a statement about THIS
# build's wire format: these four are the fields `stream_reply` knows how to
# send, and a manifest is data, not a licence to widen the request body.
SAMPLING_FIELDS = ("temperature", "top_p", "min_p", "top_k")


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
    # #96. THE WORDING IS THE INSTRUCTION -- this is the only place the model is
    # told that finding a link is not the job. A description that merely says
    # "search the web" produces a turn that hands the user three URLs, which is
    # the failure this ticket names.
    #
    # AND THE EXCEPTION IS PART OF THE RULE. "Never give a link" is the wrong
    # rule: someone who asks for the URL, the docs page or the source wants
    # exactly that, and a model forbidden to answer them is broken in the
    # opposite direction. What is banned is substituting links FOR an answer.
    _fn("web_search",
        "Search the web when the answer is not on this machine -- a library "
        "version, a flag added recently, anything past your training. Read the "
        "snippets first: they usually settle it. Answer from what you read and "
        "name the sources you used. A list of links is not an answer, unless the "
        "user asked for the link itself. Weigh what you found: a registry or an "
        "official page settles a fact, one forum post or one pull request in "
        "somebody else's project does not -- say plainly that something is "
        "unconfirmed rather than presenting it as a specification. If the thing "
        "you were asked about does not appear in any source, say it was not "
        f"found. Fetch at most {MAX_FETCHES} pages per question -- each one "
        "costs about two minutes.",
        {"query": dict(_STR, description="What to search for."),
         "count": {"type": "integer", "description":
                   f"How many results, default {SEARCH_RESULTS}, at most 10."}},
        ["query"]),
    _fn("fetch_url",
        "Fetch one http(s) page and return its readable text, markup removed. "
        "Use it on a result from web_search that the snippet did not settle, or "
        "on a URL the user gave you.",
        {"url": dict(_STR, description="An http or https URL.")}, ["url"]),
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
# ITS OWN LINE, AND THAT IS THE WHOLE POINT (#113). A model switch used to be
# reported with the line above, which says the cache could not be reused and
# names the wrong reason for it: the reader goes looking for a server without
# --slot-save-path, and the actual cause is that the session belongs to another
# network. Two causes with one sentence between them is one cause nobody can act
# on.
RESUME_MODEL_NOTE = "messages only -- this session was saved under another model"


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


# ---------------------------------------------------------------------------
# Starting the server (#114)
# ---------------------------------------------------------------------------
#
# WHAT THIS IS NOT BUILT FROM: tools/measure-slot-restart.ps1, the only
# kill-and-restart in the tree. It is stale -- `--moe-stream-cache 64s`, no
# --chat-template-file, no --moe-stream-l2 -- and it claims to be the operating
# point anyway. 64 slots is the state at which 390 MiB move to host memory
# without anything printing a word, so a boot built from it would ship the
# configuration #87 replaced and look healthy doing it.
#
# The line is BUILT FROM THE MANIFEST instead, which is the same source
# README.md and install.ps1 are checked against. A boot that assembled its own
# flags would be a fourth copy of the operating point, and this file exists
# because three copies already drifted once.

# <install> is the parent of cli/. Same step in the repo and in an install, for
# the same reason MANIFEST_PATH takes it.
INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(MANIFEST_PATH)))

# manifest key -> the flag it becomes. ORDER IS THE PRINTED ORDER, so a command
# line a human reads in a log looks like the one in README.md.
#
# `True` means a bare flag; a number or a string means flag plus value. The two
# special cases are marked where they are handled, not here: slot_save_path
# carries an <install> placeholder, and chat_template_file is `true` in the
# manifest while the server needs a PATH -- the manifest says "this model needs
# one", and where the file lives is the package's business.
SERVER_FLAGS = (
    ("port", "--port"),
    ("ctx", "-c"),
    ("cache_type_k", "-ctk"),
    ("cache_type_v", "-ctv"),
    ("ngl", "-ngl"),
    ("parallel", "-np"),
    ("jinja", "--jinja"),
    ("slot_save_path", "--slot-save-path"),
    ("chat_template_file", "--chat-template-file"),
    ("moe_stream", "--moe-stream"),
    ("moe_stream_cache", "--moe-stream-cache"),
    ("moe_stream_io_threads", "--moe-stream-io-threads"),
    ("moe_stream_direct", "--moe-stream-direct"),
    ("moe_stream_l2", "--moe-stream-l2"),
)

# TWO PLACES, FOR THE SAME REASON model_candidates HAS THREE: the package
# renames this file on the way in -- tools/pack-release.ps1 copies
# manifests\0731-chat-template.jinja to templates\ -- so an install and a
# checkout spell it differently. The packaged name is tried first, because a
# package that ships its own template must not be overtaken by the repo's.
CHAT_TEMPLATES = (os.path.join("templates", "0731-chat-template.jinja"),
                  os.path.join("manifests", "0731-chat-template.jinja"))


class ServerBootError(CrowError):
    """A start that was asked for and did not happen.

    ITS OWN CLASS, AND THAT IS THE NEGATIVE PROOF OF #114. `CrowError` on this
    path is answered with "start llama-server first, then retry" -- which is the
    right sentence for a client that found nothing listening, and exactly the
    wrong one for a client that TRIED to start something and failed. A boot
    failure that reads like the normal cold state is not plannable: the user
    retries the same command forever.
    """


def model_candidates(key: str, manifest: dict | None = None,
                     install: str | None = None) -> list[str]:
    """Every path this build would accept as the GGUF for `key`, in order.

    THREE PLACES BECAUSE THERE ARE TWO LAYOUTS AND THEY DISAGREE. The manifest's
    `models._root` is the measurement machine's tree, where the path is
    `0731-gguf/UD-IQ2_XXS/...`; an install keeps its models under
    <install>\\models and README.md spells that one `models\\UD-IQ2_XXS\\...`.
    The same manifest entry cannot be right for both, so the basename is tried
    under the install root as well.

    The list is RETURNED rather than reduced to the first hit, because the
    failure message has to name what was tried -- a boot that says only "not
    found" cannot tell a wrong table from a missing download.
    """
    manifest = manifest if manifest is not None else _manifest()
    models = manifest.get("models") or {}
    entry = (models.get("entries") or {}).get(key) or {}
    rel = (entry.get("path") or "").replace("/", os.sep)
    if not rel:
        return []
    base = os.path.basename(rel)
    out = []
    for root in ((models.get("_root") or "").replace("/", os.sep),
                 os.path.join(install or INSTALL_ROOT, "models")):
        if not root:
            continue
        for tail in (rel, base):
            path = os.path.normpath(os.path.join(root, tail))
            if path not in out:
                out.append(path)
    return out


def bootable_models() -> tuple[str, ...]:
    """The keys this build can start a server for.

    WHAT MAKES `--model` SAFE TO REUSE. That flag has always been the label in
    the request body -- `crow` by default, and llama.cpp ignores it because it
    serves whatever it loaded. #114 gives it a second meaning, and the two do
    not collide as long as the second only fires for a word that IS a key here:
    `crow`, and every label anyone has ever passed, is not one, so an existing
    command line still starts nothing and still says "start llama-server first".
    """
    return tuple(k for k in (_manifest().get("servers") or {})
                 if not k.startswith("_"))


def model_label(key: str) -> str:
    """The model's own name for a manifest key -- `Qwen3.8-27B`, not the key.

    THE KEY IS THE TABLE'S WORD, NOT THE MODEL'S. `operating-point` says which
    row of the manifest this is and nothing at all about what would load; a
    person choosing between two models is choosing between DeepSeek and Qwen.
    robin, 2026-08-21, at the first picker that listed keys.

    DERIVED FROM THE ENTRY'S OWN PATH through `model_display_name` -- the same
    function /props answers are run through, which is what makes the label and
    the header line agree by construction instead of by a second list. Falls
    back to the key, because a row whose path this build cannot read is still a
    row the user may want to pick.
    """
    paths = model_candidates(key)
    return (model_display_name(paths[0]) if paths else "") or key


def server_port(key: str) -> int | None:
    """Which port this model's line listens on, or None if it does not say.

    Public because the caller has to point the CLIENT at it. A boot that came up
    on 8082 while the session talked to 8081 would end in "start llama-server
    first" about a server that had just started -- the most confusing shape a
    success can take.
    """
    line = (_manifest().get("servers") or {}).get(key) or {}
    try:
        return int(line.get("port")) or None
    except (TypeError, ValueError):
        return None


def server_binary(install: str | None = None) -> tuple[str | None, list[str]]:
    """The llama-server this build would run, and everywhere it looked."""
    packaged = os.path.join(install or INSTALL_ROOT, "bin", "llama-server.exe")
    tried = [packaged]
    if os.path.isfile(packaged):
        return packaged, tried
    # PATH second, never first: a package that ships its own binary must not be
    # overtaken by whatever happens to be on a developer's PATH -- that is how a
    # measurement ends up describing a build nobody shipped.
    found = shutil.which("llama-server") or shutil.which("llama-server.exe")
    tried.append("PATH")
    return found, tried


def server_command(key: str, manifest: dict | None = None,
                   install: str | None = None) -> list[str]:
    """The argv for one model key, built from the manifest and nothing else.

    Raises ServerBootError naming what it looked for -- an unknown key, a GGUF
    that is not on disk, or no server binary. Each of those is a different fix
    and a single "could not start" would hide which.
    """
    manifest = manifest if manifest is not None else _manifest()
    install = install or INSTALL_ROOT
    servers = manifest.get("servers") or {}
    line = servers.get(key)
    if not isinstance(line, dict):
        known = ", ".join(sorted(k for k in servers if not k.startswith("_"))) or "none"
        raise ServerBootError("no server line for model %r. The manifest has: %s"
                              % (key, known))

    tried = model_candidates(key, manifest, install)
    gguf = next((p for p in tried if os.path.isfile(p)), None)
    if gguf is None:
        raise ServerBootError("model %r is not on disk. Tried: %s"
                              % (key, ", ".join(tried) or "nothing -- the table has no path"))

    binary, looked = server_binary(install)
    if binary is None:
        raise ServerBootError("no llama-server to run. Tried: %s" % ", ".join(looked))

    argv = [binary, "-m", gguf]
    for name, flag in SERVER_FLAGS:
        if name not in line:
            continue
        value = line[name]
        if value is True:
            argv.append(flag)
            if name == "chat_template_file":
                # The manifest says THAT one is needed, not where it lives.
                tried = [os.path.join(install, t) for t in CHAT_TEMPLATES]
                found = next((t for t in tried if os.path.isfile(t)), None)
                if found is None:
                    raise ServerBootError(
                        "%r needs a chat template and none is on disk. Tried: %s"
                        % (key, ", ".join(tried)))
                argv.append(found)
        elif value is False or value is None:
            continue
        elif name == "slot_save_path":
            argv += [flag, str(value).replace("<install>", install).replace("/", os.sep)]
        else:
            argv += [flag, str(value)]
    return argv


_PROCESS_QUERY = ("Get-CimInstance Win32_Process -Filter \"Name like 'llama-server%'\""
                  " | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")


def running_servers(query: Callable[[], str] | None = None) -> list[tuple[str, str]]:
    """Every llama-server this machine is running, as (pid, command line).

    THE PORT IS NOT ENOUGH, and finding that out cost a live boot. Asking
    /props at the address we are about to use answers "is MY server up"; it
    says nothing about a server on another port. Start the second model while
    the first is still up and both fit in the arithmetic but not on the card --
    the driver moves the overflow into host memory and prints nothing, so the
    only symptom is that requests got slower.

    An unreadable process list comes back EMPTY rather than raising: this runs
    on the path that starts a server, and refusing to boot because a query
    failed would trade a rare risk for a certain one. The caller says what it
    could not check.
    """
    if query is None:
        argv = (["powershell", "-NoProfile", "-NonInteractive", "-Command", _PROCESS_QUERY]
                if sys.platform == "win32" else ["ps", "-eo", "pid=,args="])

        def query():
            # stdin=DEVNULL, AND IT IS NOT TIDINESS. Without it the child
            # inherits this process's stdin, and `powershell -Command` READS it:
            # measured 2026-08-21, a picker that asked which model to start got
            # EOF instead of the answer, because listing the processes had
            # already swallowed it. Anything that asks a question after calling
            # this would have the same hole.
            done = subprocess.run(argv, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL,
                                  encoding="utf-8", errors="replace", timeout=60)
            return done.stdout if done.returncode == 0 else ""
    try:
        text = query()
    except Exception:
        return []
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or "llama-server" not in line:
            continue
        pid, _, rest = line.partition("\t")
        if not rest:
            pid, _, rest = line.partition(" ")
        out.append((pid.strip(), rest.strip()))
    return out


_DASH_M = re.compile(r'-m\s+("([^"]+)"|(\S+))')


def served_model(command_line: str) -> str:
    """The GGUF a running server was started with, read off its own -m."""
    hit = _DASH_M.search(command_line or "")
    return (hit.group(2) or hit.group(3)) if hit else ""


_DASH_PORT = re.compile(r"--port\s+(\d+)")


def running_base_url(default: str) -> str:
    """The address of the server that IS running, or `default`.

    WHY A CLIENT MAY NOT ASSUME 8081. That is 0731's port and it was the only
    one until a second model arrived on 8082. A window started while Qwen is up
    then knocks on an empty port and says "no endpoint" -- about a server the
    user can see running. robin, 2026-08-21: "NEIN ICH HAENGE NICHTS AN."

    ASKED IN THIS ORDER ON PURPOSE. If something answers where the caller was
    already pointed, that is the answer: an explicit --base-url is a decision
    and must not be overridden by a process list. Only when nothing answers
    there is the running server's own command line read for its --port.
    """
    if server_model_path(default) is not None:
        return default
    for _pid, line in running_servers():
        hit = _DASH_PORT.search(line)
        if not hit:
            continue
        found = "http://127.0.0.1:%s/v1" % hit.group(1)
        if server_model_path(found) is not None:
            return found
    return default


def server_model_path(base_url: str, timeout: float = 3.0) -> str | None:
    """What the server at this address has open, or None if nothing answers.

    The PATH and not the display name, because criterion 2 of #114 is that a
    second server is not started and the message names the path of the one that
    is running -- and "Qwen3.8-27B" does not tell you which of two quantisations
    is on the card.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urllib.request.urlopen(root + "/props", timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8")) or {}
    except Exception:
        return None
    return str(doc.get("model_path") or "") or None


def start_server(key: str, base_url: str, install: str | None = None,
                 wait_s: float = 600.0, log: Callable[[str], None] | None = None) -> str:
    """Bring `key` up and return the path the server reports. Or raise.

    ALREADY RUNNING IS SUCCESS, NOT A CONFLICT. A second llama-server beside the
    first overbooks the card, and the driver moves what does not fit into host
    memory without printing anything -- the failure looks like a slow day. So
    this checks first, says what is up, and starts nothing.

    POLLING IS THE POINT. `check_endpoint` runs once, which is enough for a
    server somebody else started and useless for one this line just spawned:
    the process exists seconds before the model is loaded. And a process that
    exits during loading has to be noticed as an exit rather than waited out --
    llama-server prints an impossible configuration, says `abort`, and on some
    paths keeps running with CPU offload, so neither "the process is alive" nor
    "/health says ok" is on its own a start.

    ON GIVING UP IT SHOWS THE SERVER'S OWN stderr, not a guess. Everything this
    function could say about why a load failed would be an invention; the
    server's last lines are evidence.
    """
    say = log or (lambda _msg: None)
    running = server_model_path(base_url)
    if running is not None:
        say("a server is already up: %s" % running)
        want = model_candidates(key, None, install)
        if want and os.path.basename(running).lower() != os.path.basename(want[0]).lower():
            # Said, not fixed. Stopping somebody else's server to start ours is
            # not a decision this program gets to make on its own.
            say("note: that is not %s -- stop it first if you meant to switch" % key)
        return running

    # NOTHING AT OUR ADDRESS, BUT SOMETHING ON THE CARD. Criterion 2 of #114 is
    # about this and not about the port: a second llama-server beside the first
    # overbooks the VRAM, and the only counter that would report it is the one
    # llama.cpp does not have. Refused rather than reported, because a boot that
    # says "note: something else is running" and starts anyway has told the user
    # about the damage after doing it.
    others = running_servers()
    if others:
        pid, line = others[0]
        raise ServerBootError(
            "a llama-server is already running (pid %s) on %s. Two at once "
            "overbook the card and nothing reports it -- stop that one, or "
            "point --base-url at it."
            % (pid, served_model(line) or "an unreadable command line"))

    argv = server_command(key, None, install)
    # THE SLOT DIRECTORY IS MADE, NOT ASSUMED, and the server is the one that
    # taught this: `--slot-save-path` REFUSES a path that is not an existing
    # directory -- "error while handling argument", exit 1, before a single
    # tensor is read. install.ps1 creates it, so an install is fine; a client
    # run from a checkout, or an install whose session directory was cleaned
    # out, is not. Caught on the first live boot of this function, by its own
    # rule that a failed start shows the SERVER's stderr rather than a guess.
    if "--slot-save-path" in argv:
        try:
            os.makedirs(argv[argv.index("--slot-save-path") + 1], exist_ok=True)
        except OSError:
            # Left to the server to refuse, with its own message. A directory
            # that cannot be made is a fact about the disk, and inventing a
            # sentence for it here would compete with the real one.
            pass
    say("starting %s" % os.path.basename(argv[0]))
    errfile = tempfile.NamedTemporaryFile(prefix="crow-server-", suffix=".log",
                                          delete=False, mode="w", encoding="utf-8")
    errfile.close()
    with open(errfile.name, "w", encoding="utf-8") as sink:
        proc = subprocess.Popen(argv, stdout=sink, stderr=subprocess.STDOUT)

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise ServerBootError("llama-server exited with %s before it was ready.\n%s"
                                  % (code, _tail(errfile.name)))
        path = server_model_path(base_url, timeout=2.0)
        if path is not None:
            say("server ready: %s" % path)
            return path
        time.sleep(1.0)

    proc.kill()
    raise ServerBootError("llama-server did not answer within %.0f s.\n%s"
                          % (wait_s, _tail(errfile.name)))


# The one sentence a model switch costs, written here so both surfaces say it.
# IT IS SAID BEFORE THE SWITCH IS BELIEVED, not after: a user who reads it once
# the window has already emptied has been informed of a loss rather than warned
# about one. What it does NOT promise is an archive -- an open 200k context on a
# live switch is out of scope for #115 and stays out until M5 says what a
# re-prefill of one costs.
MODEL_SWITCH_NOTE = "the context went with the old server -- the next turn pays a full prefill"


def stop_servers(log: Callable[[str], None] | None = None) -> int:
    """Stop every llama-server on this machine. Returns how many were asked.

    BY PID AND NOT BY PORT. A switch has to leave the card empty, and a server
    on some other port holds VRAM just as firmly as the one being replaced --
    that is the same arithmetic #114's criterion 2 refuses a second server for.

    Failures are counted, not raised: the next step is a start that polls, and
    it will say the truth about whether the card came free. A kill that reports
    success is not evidence either.
    """
    say = log or (lambda _msg: None)
    asked = 0
    for pid, line in running_servers():
        say("stopping pid %s (%s)" % (pid, os.path.basename(served_model(line)) or "?"))
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, stdin=subprocess.DEVNULL,
                               timeout=30)
            else:
                os.kill(int(pid), 15)
            asked += 1
        except Exception:
            continue
    return asked


def model_command(argument: str, base_url: str, install: str | None = None,
                  log: Callable[[str], None] | None = None) -> tuple[str, str, bool]:
    """`/model`: report, or switch. Returns (what to say, base_url, switched).

    THE DECISION IS HERE AND THE PLUMBING IS NOT, which is the same split
    `switch_mode` uses. Both surfaces have to name the same models, refuse the
    same typo and print the same sentence about the lost context; what each of
    them does afterwards -- empty a widget or clear a terminal -- is its own.

    `switched` is the caller's instruction to drop the conversation. It is
    returned rather than done here because the core does not own either
    surface's idea of "the chat", and a context that was dropped in one place
    and kept in the other is the half-state this return value exists to avoid.
    """
    say = log or (lambda _msg: None)
    keys = bootable_models()
    running = server_model_path(base_url)
    # THE KEY AND THE MODEL, not one or the other: the key is what has to be
    # typed back, the name is what the reader recognises. Listing only keys is
    # what `operating-point` looked like to somebody who wanted DeepSeek.
    known = (", ".join("%s (%s)" % (k, model_label(k)) for k in keys)
             or "none -- the manifest declares no server lines")

    wanted = (argument or "").strip()
    if not wanted:
        # THE RUNNING ONE IS ASKED FOR, NOT REMEMBERED. A name kept from the
        # last start would still be printed after somebody stopped the server
        # in another window, and the user would read it as "still up".
        now = running or "nothing is answering at %s" % base_url
        return ("model: %s\nkeys: %s" % (now, known), base_url, False)

    if wanted not in keys:
        # Named, and nothing started. A typo that silently booted something
        # would put 17 GB on the card for a word the user did not mean.
        return ("no model %r. The table has: %s" % (wanted, known), base_url, False)

    port = server_port(wanted)
    target = f"http://127.0.0.1:{port}/v1" if port else base_url
    if running and served_model_matches(wanted, running):
        return ("%s is already the one running." % wanted, target, False)

    stop_servers(say)
    try:
        path = start_server(wanted, target, install, log=say)
    except ServerBootError as exc:
        # THE OLD SERVER IS ALREADY GONE, and saying so is the difference
        # between "try again" and "you now have nothing". Reported as the
        # answer rather than raised: this runs inside a slash command, and a
        # traceback out of one closes the session it was typed into.
        return ("%s did not start, and the previous server was already stopped.\n%s"
                % (wanted, exc), target, True)
    return ("%s is up: %s\n%s" % (wanted, path, MODEL_SWITCH_NOTE), target, True)


def served_model_matches(key: str, path: str) -> bool:
    """Is the file at `path` the GGUF that `key` names?

    Basenames, because the same model reached through two roots is the same
    model -- and #114 already accepts three different roots for one entry.
    """
    want = model_candidates(key)
    return any(os.path.basename(c).lower() == os.path.basename(path).lower()
               for c in want)


def _tail(path: str, lines: int = 20) -> str:
    """The last lines of the server's log, or a sentence saying there are none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            kept = fh.read().splitlines()[-lines:]
    except Exception:
        return "(its log could not be read: %s)" % path
    return "\n".join(kept) if kept else "(it printed nothing; log: %s)" % path


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


def prefix_fingerprint(system: str | None, model: str | None = None) -> str:
    """What the saved KV state is only valid for.

    The chat template renders the tool declarations and the system prompt at the
    HEAD of the prompt. Change either and byte 0 differs, so a restored KV cache
    matches nothing and the server re-reads the whole conversation -- measured
    2026-08-09, adding two parameters to read_file turned a resumed 73k session
    into a full re-prefill.

    Cheaper to detect than to suffer: if this does not match, the messages are
    still restored and only the KV is dropped.

    THE MODEL IS PART OF IT, and it is not the same kind of mismatch as the two
    above. Changing the tools or the prompt makes a cache that no longer FITS;
    changing the model makes a cache that belongs to a different network
    entirely, and until #113 the only thing standing between the two was
    llama.cpp's own geometry check. On a live switch that is an edge case. In a
    boot path that chooses a model it is the normal case, because start and
    restore are the same second.

    UNKNOWN IS NOT NEUTRAL, IT IS ITS OWN VALUE. `None` hashes as the empty
    string rather than being left out of the material, so a save that could not
    name the model and a restore that can do not agree by accident -- they
    disagree, the KV is dropped, and the messages survive. Every failure of this
    function therefore lands on "pay a prefill", never on "restore the wrong
    cache".
    """
    import hashlib

    material = (json.dumps(TOOLS, sort_keys=True) + "\x00" + (system or "")
                + "\x00" + (model or ""))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def resume_cold_note(path: str | None = None, model: str | None = None) -> str:
    """Which of the two cold-resume lines the user gets, and why that is a choice.

    The fingerprint above is the GATE; this is the only thing the reader ever
    sees of it. A gate that refuses correctly and then reports the wrong reason
    has spent the refusal and bought nothing.

    READ FROM THE FILE, NOT FROM THE HASH. The fingerprint is one-way on
    purpose, so it can say "these do not match" and never "the model was X".
    `save_session` therefore writes the name beside it, and this reads it back.

    THE FALLBACK IS THE OLD LINE, NOT A THIRD ONE. A file written before #113
    has no `model` key and every session on disk today is one of those; they
    resume cold ONCE, on the old wording, which is the correct sentence for them
    -- nothing about their model is known, so nothing about it is claimed.
    Silence about the name is also why a save that could not reach /props does
    not produce this line: an empty name is not evidence of a switch.
    """
    path = path or SESSION_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            was = (json.load(fh).get("model") or "").strip()
    except Exception:
        # Unreadable is not "switched". The caller is on the resume path and
        # already has its messages; the honest answer here is the general line.
        return RESUME_COLD_NOTE
    if was and was != (model or ""):
        return f"{RESUME_MODEL_NOTE}: {was}, not {model or 'this one'}"
    return RESUME_COLD_NOTE


# #116. Where the chat's reasoning level lives: the SESSION FILE, beside
# `crow_root` and `crow_title`, because it is the same kind of fact -- something
# about this chat rather than about this run. Written by the core and not
# stamped on afterwards the way the window's two keys are: the terminal has a
# `/reasoning` too, and a key only one surface knows how to write is a setting
# that survives in one client and evaporates in the other.
SESSION_REASONING_KEY = "reasoning"

# Said BEFORE the level is changed, the way #115 announces the lost context
# before the switch. The value lands in `chat_template_kwargs`, so it is
# rendered into the HEAD of the prompt -- byte 0 moves and the whole cached
# prefix stops matching. That is not a detail at 200k.
REASONING_COST_NOTE = "the level changes the head of every prompt -- the next turn pays a full prefill"


def session_reasoning(path: str | None = None) -> str | None:
    """The level this chat was left on, or None if it never chose one.

    THE ABSENT KEY IS A STATE AND NOT A MISSING VALUE, the same three-way shape
    #101 gave the working directory: absent means nobody ever chose, and it has
    to stay reachable or a chat that merely displayed a slider once would own a
    level from then on.
    """
    path = path or SESSION_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh).get(SESSION_REASONING_KEY)
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def reasoning_for_chat(model: str | None,
                       path: str | None = None) -> tuple[str | None, str | None]:
    """What to send for this chat, and what to say about it: (level, line).

    THE THIRD STATE IS THE ONE THAT NEEDED WRITING DOWN. A level bound under one
    model is not necessarily a level the next one accepts -- `max` is fine for
    0731 and RAISES against unsloth's template (#108) -- so a stored value that
    is not in this model's list is treated as never chosen. It is SAID, because
    the alternative is a chat that silently stopped doing what its own setting
    says it does.

    NOT WRITTEN BACK. The invalid value stays in the file untouched: the user may
    switch back to the model it was valid for, and rewriting it here would erase
    a choice on the strength of a server that happens to be up right now.
    """
    level = session_reasoning(path)
    if level is None:
        return None, None
    levels = reasoning_levels_for(model)
    if level in levels:
        return level, None
    return None, ("this chat is set to %s, which %s does not take (%s) -- "
                  "sending nothing until it is set again"
                  % (level, model or "this model", ", ".join(levels)))


def reasoning_command(argument: str, model: str | None,
                      current: str | None) -> tuple[str, str | None, bool]:
    """`/reasoning`: report, or bind. Returns (what to say, level, changed).

    THE DECISION IS HERE AND THE PLUMBING IS NOT, the same split `/mode` and
    `/model` use: both surfaces have to offer the same levels, refuse the same
    typo and say the same thing about the prefill a change costs.

    `off` IS OFFERED ON PURPOSE. Without it the third state is reachable only by
    editing the file: once a level is bound there would be no way back to
    "send nothing", which is the state every existing chat is in and the only
    one whose prompt is byte-identical to a client without this feature.
    """
    levels = reasoning_levels_for(model)
    known = ", ".join(levels)
    wanted = (argument or "").strip().lower()

    if not wanted:
        now = current or "not set -- nothing is sent, and the model uses its own default"
        return ("reasoning: %s\nlevels: %s, or off" % (now, known), current, False)

    if wanted == "off":
        if current is None:
            return ("reasoning is already unset.", None, False)
        return ("reasoning unset -- nothing is sent.\n%s" % REASONING_COST_NOTE, None, True)

    if wanted not in levels:
        # Named, and NOT sent. An invalid level reaching the server arrives as a
        # template exception AFTER the prefill has been paid for.
        return ("no level %r for %s. There is: %s, or off"
                % (wanted, model or "this model", known), current, False)

    if wanted == current:
        return ("reasoning is already %s." % wanted, current, False)
    return ("reasoning: %s\n%s" % (wanted, REASONING_COST_NOTE), wanted, True)


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


def forget_session(path: str | None = None) -> bool:
    """Remove the persisted session. True if a file was actually there.

    WHY THIS IS NOT save_session's JOB. Its first line refuses to write a
    conversation with nothing in it, and for the case it was written for --
    a client that started and was closed without a word -- that is right: an
    empty file is worse than none. But the guard cannot tell "nothing was ever
    said" from "the user just emptied it on purpose", and `/reset` is the second
    one. So the emptying reaches the disk from here instead.

    MEASURED 2026-08-14, and it had been true since `/reset` existed: robin
    dropped the context in the window and closed it. `save_session` saw one
    message (the system prompt), returned None, wrote nothing -- and
    `session.json` still held the three messages the last turn had put there,
    timestamped before the reset. The next start restored the conversation he
    had just dropped. Both surfaces, because the guard is in the core.

    NOT THE SERVER'S SLOT. `SLOT_FILE` is a fixed name and the next save writes
    over it; a restore only ever happens through the file removed here, so a
    stale cache on the server is unreachable rather than dangerous.
    """
    path = path or SESSION_FILE
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # A file that will not go is not worth losing the reset over -- the next
        # save overwrites it anyway, and this is the way out of a turn.
        return False


def save_session(conversation: "Conversation", base_url: str, context_tokens: int,
                 path: str | None = None, with_kv: bool = True,
                 pretty: bool = False, model: str | None = None,
                 reasoning: str | None = None) -> str | None:
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
        # `model` IS ADDED FOR THE READER, NOT FOR THE GATE. The gate is the
        # fingerprint, which already covers the name; this key exists so the
        # refusal can SAY which model, and it is written even when empty so the
        # shape of the file does not depend on whether /props answered.
        json.dump({SESSION_FORMAT_KEY: SESSION_FORMAT,
                   "version": CLIENT_VERSION, "kv": saved_kv, "kv_tokens": kv_tokens,
                   "context_tokens": context_tokens, "model": model or "",
                   "prefix": prefix_fingerprint(conversation.system, model),
                   "messages": conversation.payload(),
                   # #116: ABSENT WHEN NEVER CHOSEN, and that is the first of
                   # the three states rather than a tidy-up. A chat with no
                   # level sends no `reasoning_effort` at all, which keeps its
                   # prompt byte-identical to one written by a client that
                   # predates the switch -- so the cache of every session on
                   # disk survives this change. Writing "" or a default here
                   # would bind every existing chat to a level nobody picked.
                   **({SESSION_REASONING_KEY: reasoning} if reasoning else {})},
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
                 path: str | None = None,
                 model: str | None = None) -> tuple[list[dict], int, bool] | None:
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
    if saved.get("kv") and saved.get("prefix") == prefix_fingerprint(system, model):
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
    top_k: int | None = None,
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
    if top_k is not None:
        # ABSENT BY DEFAULT, AND THAT IS NOT AN OVERSIGHT (#112). 0731 must keep
        # sending no top_k: it is the model under measurement, and adding a
        # sampler field to its requests would make every figure taken before
        # today incomparable with every figure taken after. The second model
        # names 20 in the manifest, so only its requests carry the key.
        #
        # WORTH SAYING BECAUSE THE TICKET SAID OTHERWISE: on Qwen this is belt
        # and braces, not a repair. /props already reports 20 -- the GGUF
        # carries general.sampling.top_k = 20 and llama.cpp applies it. Sending
        # it stops the value depending on metadata nobody re-reads.
        body["top_k"] = top_k
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
        # SEPARATE FROM tool_errors, and #95 is why. `DECLINED` begins with
        # "error: " ON PURPOSE -- that prefix is what makes the model treat a
        # refusal as something to work around rather than as an abort, and #88's
        # cases pin it. The counter used the same prefix to decide what to call a
        # malfunction, so a user's own decision arrived in the cost line as
        # `1 failed`. Measured 2026-08-14 in the run that closed #55:
        # `12 tool calls, 1 failed`, where the one "failure" was the
        # read-before-write rule holding.
        self.tool_declined = 0
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

    def add_tool(self, seconds: float, failed: bool, declined: bool = False) -> None:
        self.tool_s += seconds
        self.tool_calls += 1
        self.tool_errors += int(failed)
        self.tool_declined += int(declined)

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
            # NAMED RATHER THAN HIDDEN. The alternative to `1 declined` was to
            # count a refusal as nothing at all -- but a call the user stopped is
            # part of why the turn went the way it did, and a turn that drops it
            # reads as one where nothing happened.
            declined = f", {self.tool_declined} declined" if self.tool_declined else ""
            bits.append(f"{self.tool_calls} tool call"
                        + ("s" if self.tool_calls != 1 else "") + failed + declined)
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


# #92. THE BOUNDARY REFUSES WITHOUT ASKING, and that is the whole reason it is a
# different mechanism from the release levels (#88) rather than a stricter
# setting of them. A level protects an ATTENTIVE user -- it hands them a path and
# a question, and is only as good as their reading of it at round 14 of 24. A
# boundary protects an INATTENTIVE one, at `auto`, on the turn nobody watched.
# `auto` is the default, so without this there is nothing there at all.
#
# THE ROOT IS THE NEAREST ANCESTOR HOLDING `.crow/root.json` -- the rule
# `.claude` follows, one level deeper. The start directory was the obvious answer
# and the wrong one for a window: `crow_gui.py` is launched from a shortcut, so
# its cwd is wherever Windows felt like.
#
# WHY NOT `.crow/` ITSELF, which is what this was built as first: `.crow/` is a
# BY-PRODUCT. `SPILL_DIR` creates it wherever crow happens to run, and the window
# archives chats into `.crow/archiv/`. MEASURED 2026-08-14 on this machine:
# `C:\Users\robin\.crow` exists, dated 2026-08-08, holding 10+ spill files from a
# session that was started from the home directory once. Treating `.crow/` as the
# marker would have made the ENTIRE HOME DIRECTORY a root -- every project, every
# download, the whole profile inside the boundary -- and the case that caught it
# (`find_root` on a directory with no root above it) is the only reason it did
# not ship that way.
#
# A marker that appears by itself cannot testify to an intention. `root.json` is
# written only when someone picks a directory, and it carries what that pick
# means: the release level remembered for this root (robin's decision, #92).
#
# WRITES ONLY, and both halves of that are robin's decision of 2026-08-14
# recorded on #92. `read_file` stays unbounded, because a read boundary makes the
# model blind to its own installation -- a real use -- and a read destroys
# nothing. `run_command` is NOT covered either: a `cwd` inside the root says
# nothing about what the command does, `cd /d C:\ && del ...` being one shell
# line, so a path check there would read as protection nobody has. It stands on
# #88's `executing` class instead. The cost of that is named rather than hidden:
# at `auto`, `run_command` is unbounded.
ROOT_MARKER = ".crow"
ROOT_FILE = "root.json"

# None means no boundary -- what a surface that never calls `set_root` gets.
_ROOT: "str | None" = None


def _resolve(path: str) -> str:
    """Absolute and symlink-free, on a path that does not exist yet.

    `abspath` normalises `..` TEXTUALLY, so `root/junction/../..` resolves to
    `root` on paper while landing somewhere else on disk -- one junction and the
    boundary is decoration. `realpath` walks component by component, resolving
    each link, and stops at the first component that does not exist. That last
    part is what makes it usable here: a write names a file that is usually not
    there yet, above directories that are.

    MEASURED 2026-08-14 on this machine, Python 3.13.3: `os.path.ALLOW_MISSING`
    is in the 3.13 documentation and DOES NOT EXIST here -- it landed in a later
    patch release. The missing-path case is therefore carried by the default
    `strict=False`, not by the constant the docs point at.
    """
    return os.path.realpath(path)


def _inside(root: str, path: str) -> bool:
    """Is `path` at or below `root`? Two traps, both measured 2026-08-14.

    A BARE `startswith` IS WRONG AND QUIETLY SO: `"C:\\root2\\x"` starts with
    `"C:\\root"`, so a sibling whose name merely begins with the root's passes a
    check that looks careful. The separator has to be part of the comparison,
    which is what the `rstrip` + `sep` below is for.

    `commonpath` and `relpath` are the obvious tools and are not used, because
    across drive letters they RAISE `ValueError: Paths don't have the same
    drive` instead of answering "no". The answer there is no -- a different
    drive is outside any root -- and an exception that escapes does not refuse
    the write, it ends the turn.
    """
    here = os.path.normcase(_resolve(root)).rstrip(os.sep)
    there = os.path.normcase(_resolve(path))
    return there == here or there.startswith(here + os.sep)


def root_file(root: str) -> str:
    return os.path.join(root, ROOT_MARKER, ROOT_FILE)


def find_root(start: str | None = None) -> str | None:
    """The nearest ancestor of `start` holding `.crow/root.json`, or None.

    Walking UP rather than trusting the cwd is what lets a window opened three
    directories deep mean the same project as a terminal opened at its top.
    NEAREST, not highest: a sub-project that declares itself is its own root, and
    the case for that is `test_find_root_takes_the_nearest_marker_not_the_highest`.
    """
    here = _resolve(start or os.getcwd())
    while True:
        if os.path.isfile(root_file(here)):
            return here
        parent = os.path.dirname(here)
        if parent == here:                      # drive root, and no marker above
            return None
        here = parent


def read_root_mode(root: str) -> str | None:
    """The release level remembered for this root, or None if it names none.

    A root whose file is unreadable or malformed answers None rather than
    raising: the boundary is the security mechanism here, the remembered level is
    a convenience, and a broken convenience must not take the boundary down with
    it. The caller then falls back to DEFAULT_MODE, which is what a root without
    a level has always meant.
    """
    try:
        with open(root_file(root), encoding="utf-8") as fh:
            mode = json.load(fh).get("mode")
    except (OSError, ValueError, AttributeError):
        return None
    return mode if mode in MODES else None


def write_root_mode(root: str, mode: str) -> bool:
    """Declare `root` a root, and remember `mode` for it. Returns success.

    THIS IS THE ONLY THING THAT CREATES A ROOT. Nothing infers one, and that is
    the whole correction of 2026-08-14: `.crow/` appears on its own wherever crow
    runs, so a directory becomes a root when a human picks it, never because a
    spill file landed there once.
    """
    try:
        os.makedirs(os.path.join(root, ROOT_MARKER), exist_ok=True)
        with open(root_file(root), "w", encoding="utf-8") as fh:
            json.dump({"mode": mode if mode in MODES else DEFAULT_MODE}, fh, indent=1)
    except OSError:
        return False
    return True


def set_root(root: str | None) -> None:
    """Bind the boundary, or remove it with None."""
    global _ROOT
    _ROOT = _resolve(root) if root else None


def get_root() -> str | None:
    return _ROOT


# The roots picked before, so a window can offer them instead of asking for a
# path. Beside the session rather than inside it: a session is one conversation,
# the list of places a user works in outlives every one of them.
ROOTS_FILE = os.path.join(os.path.dirname(SESSION_DIR), "roots.json")


# TWO FACTS, TWO KEYS, and the split is the whole point (#92, 2026-08-15).
#
#   "recent"  the picker's menu. Every root ever chosen, newest first, written
#             by `remember_root` -- from the window's picker AND from the
#             terminal's `--root`.
#   "active"  what the WINDOW should bind at its next start. Written only where
#             a person chose: the picker, or "no folder".
#
# THEY WERE ALMOST ONE KEY. The obvious design is to read `recent[0]` as "last
# active" and save a field. It does not survive contact with `remember_root`:
# that list is "last PICKED, by anybody", so `crow --root D:\x` in a terminal
# would silently move where the WINDOW opens tomorrow. Two surfaces on one
# global head pointer, and neither of them wrong until they disagreed.
#
# ABSENT, NULL AND A PATH ARE THREE STATES, not two. No key at all means nobody
# has ever chosen; `null` means somebody chose "no folder" and that has to
# survive a restart too; a path means bind it. Collapsing the first two would
# make an explicit "no folder" evaporate on the next start.
#
# The comment that stood here claimed the active directory was "the open chat's
# business, not this file's". Nothing in the code ever read a root from a chat
# file -- the sentence described an intention, and the window started unbounded
# every time because of it.
#
# `{"recent": [...]}`; a bare list is the older shape and still read.
def _roots_doc() -> dict:
    try:
        with open(ROOTS_FILE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    if isinstance(doc, list):
        return {"recent": doc}
    return doc if isinstance(doc, dict) else {}


def _write_doc(doc: dict) -> None:
    try:
        os.makedirs(os.path.dirname(ROOTS_FILE), exist_ok=True)
        with open(ROOTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
    except OSError:
        pass


def _write_roots(recent: list[str]) -> None:
    # READ-MODIFY-WRITE, not a fresh dict. The old body wrote `{"recent": ...}`
    # whole, so the first `remember_root` after a pick would have deleted the
    # `active` key beside it -- and the restore would have failed exactly once
    # per session, on the run after the one that set it.
    doc = _roots_doc()
    doc["recent"] = recent
    _write_doc(doc)


def set_active_root(root: str | None) -> None:
    """Remember what the window should bind next time. `None` means "no folder".

    WRITTEN ONLY WHERE A PERSON CHOSE. Not from `adopt_root`'s `--root` branch:
    a terminal invocation states where THAT run works, and letting it move the
    window's next start is the coupling `active` exists to avoid.
    """
    doc = _roots_doc()
    doc["active"] = _resolve(root) if root else None
    _write_doc(doc)


def restore_root() -> "tuple[str | None, str | None]":
    """What the window should bind at start: `(root, problem)`.

    `(path, None)`  bind it.
    `(None, None)`  nobody ever chose, or somebody chose "no folder". Silence is
                    the right answer to both -- a line on every start is a line
                    nobody reads.
    `(None, text)`  a remembered root is gone. That one is SAID, because without
                    a root nothing bounds what Crow picks for itself: the session
                    changes its operating mode, and a silent change of operating
                    mode is the thing a user finds out about afterwards.
    """
    doc = _roots_doc()
    if "active" not in doc:
        return None, None                       # first run
    path = doc["active"]
    if path is None:
        return None, None                       # chosen, and chosen to be none
    if not isinstance(path, str) or not os.path.isfile(root_file(path)):
        return None, (f"the last working directory is gone: {path}\n"
                      f"running without one -- Crow's own writes are unbounded")
    return path, None


def known_roots(limit: int = 8) -> list[str]:
    """Roots picked before, newest first, and only ones that STILL declare themselves.

    The filter is not tidiness. A directory whose `root.json` is gone is not a
    root any more -- offering it would hand the user a boundary that silently
    does not exist, which is worse than no entry at all.
    """
    paths = _roots_doc().get("recent")
    if not isinstance(paths, list):
        return []
    out: list[str] = []
    seen = set()
    for path in paths:
        if not isinstance(path, str) or not os.path.isfile(root_file(path)):
            continue
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out[:limit]


def remember_root(root: str, limit: int = 8) -> None:
    """Put `root` at the head of the recent list -- the menu's memory, nothing more.

    It is NOT the active pick. That lives in the chat file, so a chat that names
    no directory has none however many the list holds.
    """
    root = _resolve(root)
    rest = [p for p in known_roots(limit) if os.path.normcase(p) != os.path.normcase(root)]
    _write_roots([root] + rest[:limit - 1])


def adopt_root(stated: str | None,
               mode: str | None,
               walk_up: bool = True) -> "tuple[str | None, str, str | None]":
    """Bind the working directory for a run and resolve the level with it.

    Returns `(root, mode, problem)`; `problem` is a message for the user and
    means nothing was bound.

    IN THE CORE BECAUSE BOTH SURFACES DECIDE IT, and `check_shared_core` cannot
    see a rule that exists twice -- both would call the same tools and neither
    would be wrong until they disagreed. The terminal calls this with `--root`,
    the window with the folder the picker returned.

    STATED CREATES, FOUND ONLY ADOPTS. `--root` (and the picker) write
    `root.json` and thereby declare a root; walking up never does. That
    asymmetry is the correction of 2026-08-14: `.crow/` is a by-product of
    running crow anywhere, so a boundary inferred from the disk would have made
    the home directory a root on this very machine.
    """
    problem: str | None = None
    if stated:
        if not os.path.isdir(stated):
            return None, mode or DEFAULT_MODE, f"no such directory: {stated}"
        # READ BEFORE WRITE, and a case caught it the other way round: writing
        # first stamps `mode or DEFAULT_MODE` over what the directory remembers,
        # so re-opening a root released at `manual` silently handed it back at
        # `auto` -- the memory destroyed by the act of consulting it.
        if mode is None:
            mode = read_root_mode(stated)
        write_root_mode(stated, mode or DEFAULT_MODE)
        set_root(stated)
        remember_root(stated)
    else:
        # THE LAST PICK IS THE FALLBACK, and it is what makes the choice survive
        # a restart at all. MEASURED 2026-08-14: the root was riding in
        # session.json, and `save_session` refuses to write a conversation with
        # nothing in it -- so picking a folder and closing the window without
        # saying a word threw the pick away. That is the same guard `/reset` died
        # on, in a new coat.
        #
        # A DIRECTORY IS A SETTING, NOT SOMETHING SAID. It has no business
        # hanging off a conversation, and `roots.json` is already written the
        # moment someone picks -- before any turn exists to save. `find_root`
        # still wins: standing INSIDE a declared project means that project,
        # whatever was picked last somewhere else.
        #
        # The pick is GLOBAL (robin, 2026-08-14) and survives a restart, including
        # an explicit "no folder". Where you stand still wins over both.
        #
        # THE TWO SURFACES DIVIDE HERE, AND THEY DIVIDE ON EXPECTATION rather
        # than on mechanism. A terminal user expects Crow to work where they just
        # put it, so `find_root()` -- the cwd they typed in -- wins and nothing is
        # restored. A window user expects the project to open where they left it,
        # and its cwd came from a shortcut, so the cwd means nothing and the
        # remembered choice is everything. Until 2026-08-15 the window branch was
        # a bare `None`: the fifteen lines above described a restore that no line
        # under them performed, and the folder had to be picked again after every
        # single start.
        if walk_up:
            set_root(find_root())
        else:
            restored, problem = restore_root()
            set_root(restored)
    here = get_root()
    if mode is None:
        mode = (read_root_mode(here) if here else None) or DEFAULT_MODE
    return here, mode, problem


# WHAT THE BOUNDARY REFUSED THIS TURN. It is a REPORT, not a second boundary,
# and #98 is the turn it exists to make visible: `write_file` was refused, and
# the model reached the same path with `run_command` one call later -- unprompted,
# politely, and it said so out loud. Helpfulness is the bypass.
#
# TURN-LEVEL, NOT PATH-LEVEL, and that is the honest version. Matching a refused
# path against a shell line is string analysis against a shell, and that loses:
# `cmd /c`, `%USERPROFILE%`, `>>`, a UNC path, a variable set one command
# earlier. So the rule is STATED rather than guessed -- a shell command that runs
# in a turn where the boundary already said no is marked. It over-reports an
# unrelated command in the same turn; it cannot under-report the #98 sequence,
# and a marker that is wrong in the safe direction is the only kind worth having.
#
# Lifetime is `_READ`'s and `_SEEN`'s: ONE USER TURN, cleared in `run_turn`.
_REFUSED: set[str] = set()


# WHAT THE USER NAMED. The only thing that releases a path outside the root.
#
#   Crow itself outside the root ................ NO
#   Crow because the user asked for it .......... YES
#
# and both regardless of mode -- the level decides who is ASKED, never what the
# user is allowed to order (robin, 2026-08-15).
#
# WHY THE PATH ALONE COULD NEVER DECIDE THIS. `_outside_root` saw a path and
# nothing else, so it answered identically in two situations that are not alike:
# the model inventing a location while doing something else, and the user typing
# a location into the prompt. #98's founding turn was the SECOND kind --
# `Erstell mir bitte die Datei "C:\Users\robin\Desktop\x.txt"` -- so the client
# refused an explicit instruction and then reported the model for carrying it
# out anyway. An assistant that argues with the address its user typed is not
# careful, it is broken, and the ticket had recorded that as a security finding.
#
# REBUILT EACH TURN FROM THE WHOLE CONVERSATION, not from the newest message:
# "and put the log next to it" two turns later is the same mandate, and a rule
# that forgot the address would start refusing in the middle of a task the user
# had already given.
_MANDATED: set[str] = set()

# A location in prose. Drive letters and UNC shares only: something without a
# separator is a word, not a path. "leg das auf den Desktop" names nothing this
# can resolve, and guessing a directory out of a noun is how a release rule
# begins releasing places nobody named. That limit is real and it is the price
# of not guessing -- naming the path releases it.
_PATH_IN_TEXT = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|]*")


def mandated_paths(conversation: "Conversation") -> set[str]:
    """Every location the USER spelled out in this conversation, resolved.

    Only `user` messages are read. What the MODEL wrote is not a mandate -- it is
    the thing being bounded, and a rule that let the assistant widen its own
    permission by mentioning a path would be no rule at all.
    """
    found: set[str] = set()
    for message in conversation.payload():
        if message.get("role") != "user":
            continue
        for hit in _PATH_IN_TEXT.findall(message.get("content") or ""):
            hit = hit.rstrip(".,;:!?\"')")
            if hit:
                found.add(_resolve(hit))
    return found


def _outside_root(path: str) -> str | None:
    """The refusal for a path out of bounds, or None when it is allowed.

    THE REFUSAL NAMES THE ROOT (#92 done-criterion): a user who cannot see what
    the boundary thought it was is left guessing whether the tool or their
    directory is wrong. It is returned as a tool RESULT, never raised -- the same
    invariant #88's decline keeps, since an assistant turn whose `tool_calls`
    have no `tool` message behind them is a broken prefix for every later turn.

    IT DOES NOT NAME THE WAY AROUND ITSELF (#98). The honest sentence -- that
    this holds for `write_file` and `edit_file` and that `run_command` is not
    bounded by it -- belongs in the README and on the user's screen, both of
    which the user reads and the model does not. Putting it here would hand the
    model the map to a door it already finds on its own. What goes to the model
    is the instruction, and the instruction is labelled as what it is: an
    instruction, not a mechanism.

    IT REFUSES A CHOICE, NOT A LOCATION. Everything the user named is released
    through `_MANDATED` above, so what is left to refuse is exactly the case
    worth refusing: a path the ASSISTANT picked, that nobody asked for. The
    refusal says so in those words, because "outside the working directory" was
    true of both cases and told the reader which one they were in.
    """
    if _ROOT is None or _inside(_ROOT, path):
        return None
    # THE USER'S OWN ADDRESS IS NOT A TRESPASS. A named file releases itself; a
    # named directory releases what is under it, because "put it in D:\export"
    # is an instruction about a place, and the file name is the assistant's job.
    if any(_inside(named, path) for named in _MANDATED):
        return None
    resolved = _resolve(path)
    _REFUSED.add(resolved)
    return (f"error: refusing to write outside the working directory.\n"
            f"  root: {_ROOT}\n"
            f"  path: {resolved}\n"
            f"Nobody asked for this location -- it is neither in the working "
            f"directory nor named anywhere in this conversation by the user. Do not "
            f"reach it by other means either. Write inside the root, or ask for the "
            f"path you need and let the user name it.")


def escaped_the_working_area(name: str) -> bool:
    """Is this call a shell command in a turn the boundary already refused?

    The whole predicate, and it is deliberately this small: the working-area
    guarantee covers `write_file` and `edit_file`, `run_command` is outside it,
    and the pairing of the two inside one turn is the only thing anybody can
    state without analysing a shell string.
    """
    return name == "run_command" and bool(_REFUSED)


# IN THE CORE BECAUSE BOTH SURFACES DRAW THIS LINE (#99). It sat in
# cli/crow.py, and cli/crow_gui.py reached for `crow_core.format_tool_args`
# behind a `hasattr` guard that has been False since the split -- so the
# window has always shown raw JSON where the terminal shows this. An
# expression written to make two surfaces agree is what kept them apart.
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
    # THE BOUNDARY GOES FIRST, ahead of read-before-write, and the order is not
    # cosmetic: reads are NOT bounded, so a path outside the root would answer
    # "read it first", the model would read it successfully, and only the second
    # write would be refused -- two rounds at ~18 tok/s to deliver one refusal
    # that was knowable without any state at all.
    outside = _outside_root(path)
    if outside:
        return outside
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
    outside = _outside_root(path)                   # #92, and before the read rule
    if outside:
        return outside
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


# ---------------------------------------------------------------- #96 ------
# WEB RESEARCH: THE MODEL SEARCHES AND READS, THE USER GETS AN ANSWER.
#
# A turn that ends in a list of links has not done this job -- it has handed the
# reading back to the person a local assistant exists to do it for. The unit is
# the chain, and it runs inside one turn: search -> read the snippets -> fetch
# what they did not settle -> keep working. `tool_web_search` is the entry point
# and `tool_fetch_url` serves it.

# Tags whose text is never the page. `script` and `style` would otherwise
# dominate a modern page's character count outright; the rest is furniture that
# repeats on every page of a site, and it would fill the 16 KB with navigation
# instead of with the answer.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg",
                        "nav", "header", "footer", "aside", "form", "iframe"})

# Tags that end a line, or the extracted text runs together into one paragraph
# and the model cannot tell a heading from the middle of a sentence.
_BREAK_TAGS = frozenset({"p", "div", "br", "li", "tr", "section", "article",
                         "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"})


class _TextExtractor(HTMLParser):
    """HTML in, readable text out, with nothing that is not in the standard library.

    A DEPTH COUNTER AND NOT A TAG TEST, because of what this parser is: its own
    docstring says it finds tags and calls handlers, with "no notion of building
    a parse tree, tracking open elements, or checking tag nesting". So a
    `<script>` inside a `<nav>` closes one level, not both, and the counter is
    what keeps the text after it from leaking back in. It is clamped at zero:
    a stray closing tag on a malformed page would otherwise drive it negative and
    turn every later skip into a no-op -- and malformed pages are the normal
    case, which the same docs demonstrate on `<p><a class=link href=#main>tag
    soup</p ></a>`.

    `convert_charrefs` is left at its default of True, which is what turns
    `&amp;` into text before it reaches handle_data -- and, per the docs, also
    what stops handle_data being split into arbitrary chunks.
    """

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data.strip()
        elif data.strip():
            self.parts.append(data)


def _extract_text(html: str) -> tuple[str, str]:
    """(title, text), whitespace collapsed.

    EXTRACTION RUNS BEFORE `_clip`, NEVER AFTER, and that order is the whole
    point of having it. A raw page is mostly head, script and navigation, so
    clipping first keeps the furniture and throws away the paragraph the model
    was sent for: in bytes, the answer is usually below the fold even when it is
    at the top of the screen.
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # A parser that raises on one page must not take the turn with it. The
        # tag-stripped source is a worse answer than a parse and a better one
        # than the exception, which is what would otherwise reach run_turn.
        return "", re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    return parser.title.strip(), re.sub(r"\n\s*\n\s*", "\n\n", text).strip()


def _http_text(url: str, timeout: int = WEB_TIMEOUT, data: bytes | None = None,
               extra_headers: dict | None = None) -> tuple[str, str] | str:
    """One GET. Returns (content_type, body) or an error STRING -- never raises.

    NEVER RAISES IS THE CONTRACT, not a courtesy: an assistant turn whose
    `tool_calls` have no `tool` message behind them is a broken prefix for every
    later turn of the session -- the same invariant `DECLINED` keeps. And on this
    machine "no network" is a normal state rather than a fault, so it has to come
    back as something the model can read and act on.

    `HTTPError` is caught before `URLError` because it is a subclass of it; the
    other way round, an HTTP 404 would report as an unreachable host.
    """
    headers = {
        # Identifying, not disguised. A tool that lies about who it is cannot be
        # rate-limited fairly, and a site that would rather not answer Crow is
        # entitled to that.
        "User-Agent": f"Crow/{CLIENT_VERSION or 'dev'} (+{REPO_URL})",
        "Accept": "text/html,application/xhtml+xml,text/plain,application/json",
    }
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            charset = resp.headers.get_content_charset() or "utf-8"
            # A bounded read, so a multi-gigabyte body cannot hold the turn until
            # the socket gives up. Everything past this is clipped anyway.
            raw = resp.read(WEB_MAX_DOWNLOAD)
    except urllib.error.HTTPError as exc:
        return f"error: {url} answered HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        return (f"error: could not reach {url}: {exc.reason} -- this machine may "
                f"be offline, or the address may be wrong")
    except (TimeoutError, OSError, ValueError) as exc:
        return f"error: {url} did not answer within {timeout}s ({exc})"
    return ctype, raw.decode(charset, errors="replace")


def tool_fetch_url(url: str = "", **_) -> str:
    """One page, as text. It serves the search; it is not the capability alone."""
    if not url:
        return "error: fetch_url needs a 'url'"
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        # urlopen also speaks file: and data:. Left open, this tool would become
        # an unbounded read of the disk that goes AROUND #92's boundary rather
        # than through it -- and reads are not bounded there by design.
        return f"error: fetch_url takes http or https, not {scheme or 'a bare path'}"
    got = _http_text(url)
    if isinstance(got, str):
        return got
    ctype, body = got
    if ctype in ("text/html", "application/xhtml+xml", ""):
        title, text = _extract_text(body)
        if not text:
            return f"error: {url} has no readable text (it may be built by scripts)"
        return _clip((f"{title}\n{url}\n\n" if title else f"{url}\n\n") + text)
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        return _clip(f"{url}\n\n{body}")
    return f"error: {url} is {ctype}, which has no text to read"


def _json_get(url: str, timeout: int = KEYLESS_TIMEOUT):
    """A keyless GET that answers None instead of raising or returning a message.

    The federation below runs several of these at once and merges what came
    back. One source being down, rate-limited or slow is the normal case there,
    not an error worth a sentence -- the sentence belongs to the merge, once,
    when NOTHING came back.
    """
    got = _http_text(url, timeout=timeout)
    if isinstance(got, str):
        return None
    try:
        return json.loads(got[1])
    except json.JSONDecodeError:
        return None


def _src_github(query: str, want: int) -> list[dict]:
    """Repositories and issues. Unauthenticated search is 10 requests a minute,
    which is far above what one question costs."""
    # EVERY FIELD THROUGH .get(). One unexpected item would otherwise raise, and
    # the federation's own guard turns that into "this source returned nothing"
    # -- so a single odd record silently costs the whole source, which is the
    # kind of failure that never shows up as one.
    out = []
    q = urllib.parse.quote(query)
    data = _json_get(f"https://api.github.com/search/repositories?q={q}&per_page={want}")
    for r in (data or {}).get("items", [])[:want]:
        if r.get("html_url"):
            out.append({"title": f"{r.get('full_name') or r['html_url']} "
                                 f"({r.get('stargazers_count', 0)}*)",
                        "url": r["html_url"], "content": r.get("description") or "",
                        "source": "github"})
    data = _json_get(f"https://api.github.com/search/issues?q={q}&per_page={want}")
    for r in (data or {}).get("items", [])[:want]:
        if not r.get("html_url") or r.get("number") is None:
            continue
        body = " ".join((r.get("body") or "").split())[:300]
        out.append({"title": f"#{r['number']} {r.get('title') or ''} "
                             f"[{r.get('state') or '?'}]",
                    "url": r["html_url"], "content": body, "source": "github issue"})
    return out


def _src_stackoverflow(query: str, want: int) -> list[dict]:
    """Answered questions only -- an unanswered one costs the same tokens and
    settles nothing."""
    data = _json_get("https://api.stackexchange.com/2.3/search/advanced?order=desc"
                     f"&sort=relevance&accepted=True&q={urllib.parse.quote(query)}"
                     f"&site=stackoverflow&pagesize={want}")
    return [{"title": r["title"], "url": r["link"],
             "content": f"score {r.get('score', 0)}, {r.get('answer_count', 0)} answers",
             "source": "stackoverflow"}
            for r in (data or {}).get("items", [])[:want]]


def _src_wikipedia(query: str, want: int) -> list[dict]:
    data = _json_get("https://en.wikipedia.org/w/api.php?action=query&list=search"
                     f"&srsearch={urllib.parse.quote(query)}&format=json&srlimit={want}")
    out = []
    for r in ((data or {}).get("query") or {}).get("search", [])[:want]:
        title = r["title"]
        out.append({"title": title,
                    "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
                        title.replace(" ", "_")),
                    "content": re.sub(r"<[^>]+>", "", r.get("snippet") or ""),
                    "source": "wikipedia"})
    return out


# What makes a query a package question. Deliberately narrow: this source ranks
# first, so a false positive costs the top slot, while a false negative costs
# one line the other sources usually cover anyway.
_PACKAGE_QUESTION = re.compile(
    r"\b(version|versions|release|released|latest|current|install|installed|"
    r"package|packages|pypi|pip|crate|crates|cargo|npm|changelog|upgrade)\b",
    re.I)


def _src_packages(query: str, want: int) -> list[dict]:
    """Exact-name lookups, and they answer the question this tool exists for.

    "which version of X is current" is the single most common thing a model
    trained months ago gets wrong, and it is the one question with an
    authoritative one-request answer. PyPI has no search endpoint any more, so
    the query's identifier-looking words are tried as names -- a miss costs one
    404 and prints nothing.

    IT ONLY FIRES WHEN THE QUESTION IS ABOUT A PACKAGE, and that gate was added
    after a live run on 2026-08-14: "llama.cpp moe stream flag" matched a music
    library manager on PyPI and, because this source ranks first, `Moe 2.5.0`
    led the results. A coincidental name match in the top slot is worse than
    noise -- it looks authoritative, and it is the answer the model reads first.
    """
    if not _PACKAGE_QUESTION.search(query):
        return []
    out = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", query)[:3]:
        data = _json_get(f"https://pypi.org/pypi/{urllib.parse.quote(word)}/json")
        info = (data or {}).get("info")
        if info:
            out.append({"title": f"pypi {info['name']} {info['version']}",
                        "url": info.get("project_url") or
                        f"https://pypi.org/project/{info['name']}/",
                        "content": info.get("summary") or "", "source": "pypi"})
        data = _json_get(f"https://crates.io/api/v1/crates/{urllib.parse.quote(word)}")
        crate = (data or {}).get("crate")
        if crate:
            out.append({"title": f"crate {crate['name']} {crate.get('max_stable_version')}",
                        "url": f"https://crates.io/crates/{crate['name']}",
                        "content": crate.get("description") or "", "source": "crates.io"})
    return out


# What makes a query a model question. Same shape and the same reason as the
# package gate: this source is authoritative for "does this model exist", and
# noise for anything else.
_MODEL_QUESTION = re.compile(
    r"\b(model|models|gguf|quant|quantis|weights|checkpoint|huggingface|"
    r"\d{1,3}b|moe|llm|instruct|qwen|llama|mistral|deepseek|gemma|phi)\b", re.I)


def _src_huggingface(query: str, want: int) -> list[dict]:
    """Does this model exist, what is it, and how much traction does it have.

    ADDED BECAUSE OF WHAT THE FIRST LIVE RUN LACKED, 2026-08-14. Asked in the
    window about "Qwen3.8-27B" -- a release that postdates the model's training
    -- the search returned three third-party pull requests and the model wrote a
    specification from them. It turned out to be broadly right, which is the
    point: nothing in those results could have told it so. This registry can.
    `Qwen/Qwen3.8-27B` reports 2 downloads against 8,457 likes, which is the
    signature of a release published hours ago, and `pipeline_tag`
    image-text-to-text confirms the vision half without reading a single page.

    THE COUNTS ARE THE WEIGHT, and they are printed for that reason: an official
    org path with millions of downloads and a 0-download re-upload of the same
    name are the same string and not the same evidence.
    """
    if not _MODEL_QUESTION.search(query):
        return []
    # THE NAME, NOT THE SENTENCE. Measured 2026-08-14: "Qwen3.5-27B model" --
    # a model that exists -- returned nothing, because the word "model" is part
    # of the search string and the registry matches names. The gate words are
    # exactly the ones that must not travel into the query they let through.
    name = " ".join(w for w in query.split() if not _MODEL_QUESTION.fullmatch(w))
    if not name.strip():
        return []
    data = _json_get("https://huggingface.co/api/models?limit=%d&sort=downloads"
                     "&direction=-1&search=%s" % (want, urllib.parse.quote(name)))
    out = []
    for m in (data or [])[:want] if isinstance(data, list) else []:
        mid = m.get("modelId") or m.get("id")
        if not mid:
            continue
        out.append({"title": f"HF {mid}",
                    "url": f"https://huggingface.co/{mid}",
                    "content": f"{m.get('downloads', 0):,} downloads, "
                               f"{m.get('likes', 0)} likes, "
                               f"{m.get('pipeline_tag') or 'no pipeline tag'}",
                    "source": "huggingface"})
    if not out and isinstance(data, list):
        # AN ABSENCE THE MODEL CAN READ. This is the half the first live run was
        # missing: the registry had no entry, the registry said so by returning
        # nothing, and nothing is invisible in a result list -- so the model saw
        # only three third-party pull requests and wrote a specification from
        # them. A silent absence teaches nothing; this line is the evidence.
        return [{"note": f"huggingface: no model matching {query!r} exists"}]
    return out


def _src_ddg_answer(query: str, want: int) -> list[dict]:
    """DuckDuckGo's OFFICIAL instant-answer API -- keyless, documented, and not
    the html endpoint. It answers a minority of queries, and when it does the
    answer costs no fetch at all."""
    data = _json_get("https://api.duckduckgo.com/?format=json&no_html=1&q="
                     + urllib.parse.quote(query))
    text = (data or {}).get("AbstractText") or (data or {}).get("Answer") or ""
    if not text:
        return []
    return [{"answer": text, "url": (data or {}).get("AbstractURL") or "",
             "source": "duckduckgo"}]


# THE KEYLESS FEDERATION, and why it is the default rather than a fallback.
#
# A general web index costs money, an account, a service, or a lie. The last one
# is not a trade-off, it is the mechanism: measured 2026-08-14, ONE URL and one
# second apart, lite.duckduckgo.com answered HTTP 200 with 10 results to a
# browser user-agent and HTTP 202 with none to `Crow/0.3.3 (+github...)`. A
# search that only works while Crow misrepresents what it is would break in
# every installation at once the day that check tightens, and nobody would be
# able to tell the user why.
#
# These six sources are the opposite trade: official, documented, keyless, and
# they answer the questions a CODING assistant actually has -- which version is
# current, does this repo exist, has someone hit this error, what is this thing.
# Measured the same day with an honest user-agent: all six answered HTTP 200,
# github reported 7,356 repositories and 245 issues, wikipedia 10 articles,
# pypi `requests 2.34.2`, stackexchange quota_remaining 298 of 300.
#
# For general web search beyond these, CROW_TAVILY_KEY or CROW_SEARXNG_URL take
# over -- an upgrade the user may choose, not a setup step they must complete.
# THE ORDER IS AUTHORITY, AND IT IS LOAD-BEARING. Measured live on 2026-08-14:
# concatenating the sources put github first unconditionally, so "requests
# library current version" answered with a stranger's library-management project
# while pypi's exact `requests 2.34.2` sat further down. The merge below is
# round-robin in this order, so the first three lines are the three best
# ANSWERS rather than the first source's first three guesses.
KEYLESS_SOURCES = (_src_packages, _src_huggingface, _src_ddg_answer,
                   _src_stackoverflow, _src_github, _src_wikipedia)

# EVERY SNIPPET, NOT JUST THE LONG ONES. The same live run returned 16,056 bytes
# for three results, because one repository description was 15 KB on its own --
# `_clip` then cut the tail, so the model paid full prefill for one project's
# marketing and never saw results two and three. `_clip`'s own lesson, one level
# down: a result count is not a size.
MAX_SNIPPET = 240


def _search_keyless(query: str, want: int) -> dict:
    """Every source at once, because the slowest one would otherwise set the pace.

    Sequentially this is five to eight requests -- at KEYLESS_TIMEOUT each, a
    single dead source would cost more wall clock than the whole search is worth.
    """
    collected: list[list[dict]] = [[] for _ in KEYLESS_SOURCES]

    def run(i, src):
        try:
            collected[i] = src(query, want)
        except Exception:
            collected[i] = []          # one source may never sink the search

    threads = [threading.Thread(target=run, args=(i, s), daemon=True)
               for i, s in enumerate(KEYLESS_SOURCES)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=KEYLESS_TIMEOUT + 2)

    answers = [h["answer"] for g in collected for h in g if "answer" in h]
    notes = [h["note"] for g in collected for h in g if "note" in h]
    queues = [[h for h in g if h.get("url")] for g in collected]
    results, seen = [], set()
    for rank in range(max((len(q) for q in queues), default=0)):
        for queue in queues:                      # round-robin: one each, in turn
            if rank >= len(queue):
                continue
            hit = queue[rank]
            if hit["url"] in seen:
                continue
            seen.add(hit["url"])
            hit["content"] = " ".join((hit.get("content") or "").split())[:MAX_SNIPPET]
            results.append(hit)
    return {"results": results, "answers": answers, "notes": notes}


def _search_tavily(query: str, want: int) -> dict | str:
    """The shipping default: a free key, no card, nothing to install or run.

    `include_answer` is on because it is the cheapest thing in this whole file --
    a finished answer costs no fetch, and a fetch costs two minutes.
    """
    body = json.dumps({"query": query, "max_results": want,
                       "include_answer": True}).encode("utf-8")
    got = _http_text(TAVILY_URL, data=body, extra_headers={
        "Authorization": f"Bearer {TAVILY_KEY}",
        "Content-Type": "application/json",
    })
    if isinstance(got, str):
        # A rejected key is an HTTP 401, and "unauthorized" alone would leave the
        # user guessing which of their env vars is wrong.
        if "401" in got or "403" in got:
            return (f"{got}\nCROW_TAVILY_KEY was refused. Check it at "
                    f"https://tavily.com, or unset it and set CROW_SEARXNG_URL "
                    f"to your own instance.")
        return got
    _, text = got
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return f"error: {TAVILY_URL} did not answer with JSON"


def _search_searxng(query: str) -> dict | str:
    """The second provider: unlimited and account-free, for a machine already running one."""
    url = (SEARXNG_URL.rstrip("/") + "/search?"
           + urllib.parse.urlencode({"q": query, "format": "json"}))
    got = _http_text(url)
    if isinstance(got, str):
        return (f"{got}\n(Crow is set to search through a SearXNG at "
                f"{SEARXNG_URL}. Change CROW_SEARXNG_URL, or unset it and set "
                f"CROW_TAVILY_KEY instead.)")
    _, body = got
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # The instance answered, but with a page. That is its DEFAULT state --
        # settings_search.rst ships `formats: [html]` -- so it gets a sentence
        # that says what to change, not a parser error that says what broke.
        return (f"error: {SEARXNG_URL} did not answer with JSON. Add 'json' under "
                f"search.formats in its settings.yml and restart it -- only 'html' "
                f"is enabled by default.")


def tool_web_search(query: str = "", count: int = SEARCH_RESULTS, **_) -> str:
    """Search, and return results worth using without opening any of them.

    THE SNIPPET IS THE PRODUCT. Every fetch that follows costs about two minutes
    of prefill, so a result list that settles the common question on its own is
    the difference between a capability and a way to spend a turn. searxng's
    `answers` field is the best case of that -- a direct answer, zero fetches --
    which is why it is printed first and not folded in with the results.
    """
    if not query:
        return "error: web_search needs a 'query'"
    try:
        want = max(1, min(int(count or SEARCH_RESULTS), 10))
    except (TypeError, ValueError):
        want = SEARCH_RESULTS

    # NOTHING HAS TO BE CONFIGURED, and that is the whole point: Crow arrives
    # through one line of PowerShell, and a capability that starts with "first
    # create an account" is a capability nobody switches on. The keyless
    # federation is therefore the default; a key or an instance is an upgrade
    # for whoever wants a general index, not a setup step.
    if SEARXNG_URL:
        data = _search_searxng(query)
    elif TAVILY_KEY:
        data = _search_tavily(query, want)
    else:
        data = _search_keyless(query, want)
    if isinstance(data, str):
        return data

    results = data.get("results") or []
    lines = []
    # Both providers can hand back a finished answer -- searxng under `answers`,
    # tavily under `answer`. It is the best case there is here: zero fetches,
    # zero further rounds, and it is printed first rather than folded in with
    # the results, where the model would have to notice it.
    answers = data.get("answers") or ([data["answer"]] if data.get("answer") else [])
    for answer in answers[:2]:
        text = answer.get("answer") if isinstance(answer, dict) else answer
        if text:
            lines.append(f"answer: {text}")
    # Before the results, because a registry saying "this does not exist" outranks
    # every keyword match underneath it -- and underneath is where it would be
    # read last, if at all.
    for note in (data.get("notes") or []):
        lines.append(f"note: {note}")
    for n, hit in enumerate(results[:want], 1):
        snippet = " ".join((hit.get("content") or "").split())
        # The source is named because these are not interchangeable. An accepted
        # stackoverflow answer, an open issue and an encyclopaedia paragraph
        # carry different weight, and a list that hides which is which invites
        # the model to weigh them the same.
        tag = f" [{hit['source']}]" if hit.get("source") else ""
        lines.append(f"{n}. {(hit.get('title') or '').strip()}{tag}\n"
                     f"   {(hit.get('url') or '').strip()}\n   {snippet}")
    if not lines:
        # AN EMPTY RESULT AND A BROKEN BACKEND LOOK IDENTICAL from here, and they
        # are not the same answer: searxng reports the engines that failed
        # separately, so a query that found nothing because every engine was
        # rate-limited says so instead of reading as "the web does not know".
        dead = data.get("unresponsive_engines") or []
        if dead:
            return (f"no result for {query} -- and {len(dead)} engine(s) did not "
                    f"answer: {dead}. This may be the instance, not the query.")
        if not (SEARXNG_URL or TAVILY_KEY):
            return f"no result for {query}" + NO_GENERAL_INDEX
        return f"no result for {query}"
    lines.append(f"\n[the snippets answer most questions; fetch_url at most "
                 f"{MAX_FETCHES} of these, each costs ~2 min]")
    return _clip("\n".join(lines))


TOOL_IMPL = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_dir": tool_list_dir,
    "find_files": tool_find_files,
    "search_text": tool_search_text,
    "run_command": tool_run_command,
    "web_search": tool_web_search,
    "fetch_url": tool_fetch_url,
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
    # #96. A FOURTH CLASS, because neither of the three fits. Fetching destroys
    # nothing, so it is not `writing`; it starts no shell, so it is not
    # `executing`. But it is not `reading` either, and calling it that would be
    # the mistake this entry exists to avoid: a local read stays on the machine,
    # while these two send the query off it and bring a stranger's text back in.
    # Both halves matter -- what leaves, and what arrives.
    "web_search": "network",
    "fetch_url": "network",
}

# Which classes stop and ask, per level. A class not named here runs.
#
# `network` IS ABSENT FROM EVERY LEVEL, DELIBERATELY (robin, #96, 2026-08-14):
# a search happens because a task was given, and giving the task is the release.
# Asking "may I look this up?" of the person who just asked the question is the
# same protection-nobody-keeps-on that the reading rule already names -- it would
# arrive mid-turn, at round 9 of 24, about a step the user implicitly ordered.
# The class still exists above, because the classification is true even when no
# level acts on it, and because a later level that DOES want to gate the network
# needs the name to already mean something.
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


# #94. THE LIST IS SHARED, THE ANSWERS ARE NOT.
#
# Every surface must have an answer for every one of these; what the answer IS
# belongs to the surface. The terminal runs them. The window mostly points --
# four of the seven already have a widget doing the job there, and a window that
# grows a second way to do everything is the divergence #90 exists to prevent,
# in the shape that `check_shared_core.py` cannot see because both sides call
# the same core.
#
# WHAT GOES WRONG WITHOUT IT is not a crash: a command the terminal offers and
# the window has never heard of travels to the model as a question about the
# word, and comes back as an answer about slashes. That is what `/reset`,
# `/context`, `/thoughts`, `/mode`, `/exit` and `/quit` did in the window until
# now -- six of the seven.
#
# `crow.py` keeps the prose of `HELP` and is pinned against this tuple; the
# window reads the tuple directly. Neither owns the other.
SLASH_COMMANDS = ("/help", "/tools", "/mode", "/model", "/reasoning", "/thoughts",
                  "/reset", "/context", "/exit", "/quit")


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
#
# THE KEY IS NOT (name, arguments) FOR EVERY TOOL -- see `_cache_key`. That pair
# is the whole input only where the answer is a function of the arguments, and
# #93 is the run where it was not.
_SEEN: dict[tuple, str] = {}


# #93. The two tools whose result is NOT a function of their arguments alone.
#
# MEASURED 2026-08-14, in the run that closed #55: `write_file` was refused for
# want of a read, `read_file` supplied it, and the identical `write_file` came
# back as a repeat carrying the OLD REFUSAL -- three times, until the model gave
# up and reached for `edit_file` instead. In the same turn a `run_command` after
# an `edit_file` on the same file replayed the output from before the edit, and
# the model escaped by appending `2>&1` to change the key rather than the
# command. 4 of that turn's 12 calls were replays of a state that had moved.
#
# THE FIX IS NOT TO RECOGNISE REFUSAL TEXT. A cache keyed on less than its
# inputs is wrong whatever the text says, so the inputs go into the key:
#
#   run_command           depends on everything a shell can reach -> never cached
#   write_file/edit_file  depend on `_READ` -> the key carries whether this
#                         path has been read in this turn, so a read between two
#                         identical calls IS a different call
#   everything else       is a function of its arguments -> keyed as before
#
# That last line is what keeps the 2026-08-09 loop closed: it happened on
# `read_file` for a path that did not exist, and a path does not start existing
# because it was asked for twice.
NEVER_CACHED = frozenset({"run_command"})
READ_GATED = frozenset({"write_file", "edit_file"})


def _cache_key(name: str, arguments: str) -> tuple | None:
    """What this call's result depends on, or None when it depends on too much.

    None means "do not cache", NOT "cache miss": the caller must not write the
    result back either, or the next identical call is answered from a key that
    was never a promise about anything.
    """
    if name in NEVER_CACHED:
        return None
    if name not in READ_GATED:
        return (name, arguments)
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        args = None
    path = args.get("path") if isinstance(args, dict) else None
    # A call whose path is missing or not a string cannot have been read, and it
    # is about to fail on its arguments -- which IS a function of its arguments,
    # so caching it under `seen=False` is correct rather than a fallback.
    seen = _key(path) in _READ if isinstance(path, str) else False
    return (name, arguments, seen)


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

    THAT SENTENCE IS TRUE ONLY WHERE THE RESULT IS A FUNCTION OF THE ARGUMENTS,
    and for two of the seven tools it is not. `_cache_key` is where that is
    decided and why; #93 carries the turn in which it was measured.
    """
    key = _cache_key(name, arguments)
    if key is not None and key in _SEEN:
        return (f"[you already called {name} with these exact arguments this turn. "
                f"The result was, and still is:]\n{_SEEN[key]}"), True
    out = run_tool(name, arguments)
    if key is not None:
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
    """What `run_turn` reports while ONE USER TURN runs. Thirteen prints, named.

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
    `boundary_escaped` is the second (#98), and for a different reason: it
    reports a sequence across two calls, and no single print ever carried it.

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

    def boundary_escaped(self, name: str, refused: list[str]) -> None:
        """A shell command ran in a turn where the boundary refused a write (#98).

        The thirteenth line, and the only one that had no terminal ancestor: it
        reports a SEQUENCE rather than a call, so there was nothing in `repl()`
        to move. `refused` is what the boundary turned away this turn, so the
        surface can name the path instead of saying that something happened.
        """

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
    # top_k KEEPS ITS DEFAULT WHILE THE THREE ABOVE MAY NOT HAVE ONE, and the
    # difference is what the checker counts. None is not a value: it means "this
    # model declares no top_k", which is 0731's case and the case of every
    # endpoint this client has ever talked to. A number here would be a literal.
    top_k: int | None = None,
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
    # THE THREE CLEARS ARE ONE STATEMENT GROUP AND MUST STAY ONE. Split them and
    # the half-state is a live configuration rather than a mistake somebody has
    # to make: `_READ` emptied without `_SEEN` refuses the write correctly while
    # still handing back a tool result from the turn before, and `_SEEN` emptied
    # without `_READ` lets a stale permission outlive the results that earned
    # it. Neither is a state anyone would choose, and neither announces itself.
    #
    # `_REFUSED` JOINED THEM WITH #98 and fails the same way: left behind, the
    # next turn's first `run_command` is marked as an escape from a refusal that
    # happened before the user typed again. That is a false alarm, and a false
    # alarm on a marker is worse than no marker -- it is the one failure mode
    # that trains the reader to skip the line.
    _READ.clear()
    _SEEN.clear()
    _REFUSED.clear()
    # NOT CLEARED -- REBUILT, and from the conversation rather than this turn's
    # line. The user's addresses accumulate: a path named two turns ago is still
    # the place the user asked for, and forgetting it would start refusing in the
    # middle of the very task that named it. It happens here rather than in the
    # surfaces because a client that forgot to build the list would silently
    # refuse everything its user typed -- the failure #98 already recorded once.
    _MANDATED.clear()
    _MANDATED.update(mandated_paths(conversation))
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
                top_k=top_k,
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
            # TWO NAMES FOR WHAT WAS ONE LINE, and #95 is the reason. `errored`
            # is what the MODEL sees: the "error: " prefix that makes a result
            # recoverable instead of terminal, which `DECLINED` carries on
            # purpose. `failed` is what the COUNTER means: a tool that
            # malfunctioned. A refusal the user made is the first and not the
            # second, and one predicate could not say so.
            errored = result.startswith("error: ")
            failed = errored and not declined
            cost.add_tool(took, failed, declined)
            events.tool_finished(call["name"], took, repeated)
            # #98: THE USER HEARS THIS FROM CROW, NOT FROM THE MODEL'S APOLOGY.
            # In the measured turn the only notice that the working area had been
            # left came from the model itself, after the fact, phrased as a
            # courtesy -- "mein Datei-Werkzeug ist beschraenkt, daher habe ich es
            # ueber die Shell angelegt". A client whose only account of its own
            # limits is the sentence of the thing that just went around them has
            # no account at all.
            #
            # AFTER THE CALL, NOT BEFORE: the marker is a report of what ran, and
            # a declined call ran nothing. `not declined` is therefore the
            # condition, not `not errored` -- a shell command that failed on its
            # own terms still reached the shell, and that is the fact being
            # reported.
            if not declined and escaped_the_working_area(call["name"]):
                events.boundary_escaped(call["name"], sorted(_REFUSED))
            # A FAILED CALL STAYS ON SCREEN even once the model has recovered from it (#70).
            # It is not the user's problem to solve, but it is the reason the turn took longer
            # than it looks like it should have, and a turn that hides its retries reads as
            # slower for no reason.
            #
            # THE SCREEN KEEPS THE WIDER OF THE TWO. A declined call is still
            # printed: that the delay was the user's own choice does not make it
            # less of a reason the turn went the way it did. Only the count
            # separates them, which is all #95 asked for.
            if errored:
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
