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

import atexit
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

# #123. How many past messages one `session_search` returns, and how much of
# each. UP HERE WITH THE OTHER TOOL BUDGETS rather than beside the index, because
# `TOOLS` is built a few lines below and reads the first of them: a constant
# defined further down is a NameError at import, which is how this was found.
SEARCH_HITS = 8
SEARCH_SNIPPET = 400

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
# SAID ON EVERY KEYLESS ANSWER, not only on the empty one -- and that gap was
# the defect. `NO_GENERAL_INDEX` below is the honest sentence, but it only ever
# reached the model when the federation found NOTHING. It almost always finds
# something: measured 2026-08-22, `was kostet ein rtx 5090` came back with
# github issue #5090 at rank one, because the number matched an issue NUMBER.
# A non-empty list with the warning suppressed reads as a web search that
# worked, so the model answered from noise -- and then went around the tool with
# `fetch_url` against Bing and DuckDuckGo, which block it. One live turn paid
# 18 rounds, 27 tool calls, 3 of them failed, and 2m52s of waiting for that.
#
# FIRST, NOT LAST, for the reason the registry notes below already carry: a line
# about what the whole list is worth, printed under the list, is read after the
# list has already been believed.
KEYLESS_SCOPE = (
    "note: this index covers code, packages and reference -- NOT the open web. "
    "A hit here may be a keyword match rather than an answer; weigh it before "
    "using it. For a general index set CROW_TAVILY_KEY (free, no credit card, "
    "https://tavily.com) or CROW_SEARXNG_URL to your own instance."
)

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


def reasoning_groups_for(model: str | None) -> tuple[tuple[str, ...], ...]:
    """Which of this model's levels render the SAME prompt. Measured, from the manifest.

    THE LEVEL LIST AND THIS ARE NOT THE SAME LENGTH, and that is the whole point.
    `reasoning_levels_for` says what may be SENT; this says what is DISTINCT. Measured
    2026-08-21 (#117) through /apply-template: on Qwen `off` and `high` render byte-identically,
    on 0731 `off`, `low` AND `high` do -- there only `max` changes anything, so two of the three
    levels that table offers move no byte.

    `off` IS A MEMBER OF A GROUP, not a fourth state beside them. It means "send no
    reasoning_effort at all", and every template has a default for the absent key: xhigh on Qwen,
    none on 0731. So `off` always lands ON one of the real steps -- the group naming it is the
    default group.

    EMPTY MEANS UNMEASURED, not "no duplicates". A model whose entry carries no
    reasoning_groups gets the old behaviour: one row per level, nothing marked default, and every
    change warned about as if it re-rendered. Guessing a grouping here would be the one failure
    this field exists to prevent -- a window claiming two steps are the same on the strength of
    nobody having looked.
    """
    manifest = _manifest()
    key = model_key_for(model, manifest)
    entry = (((manifest.get("models") or {}).get("entries") or {}).get(key) or {}) if key else {}
    groups = entry.get("reasoning_groups")
    if not isinstance(groups, list):
        return ()
    out = [tuple(str(x) for x in g) for g in groups if isinstance(g, list) and g]
    return tuple(out)


def reasoning_group_of(level: str | None,
                       groups: tuple[tuple[str, ...], ...]) -> tuple[str, ...] | None:
    """The group `level` belongs to, or None when nothing claims it.

    `None` and `"off"` are the same question: both mean the key is not sent.
    """
    wanted = level or "off"
    for group in groups:
        if wanted in group:
            return group
    return None


def reasoning_row_name(group: tuple[str, ...]) -> str:
    """What one group is CALLED: its first member that is not `off`.

    `off` never names a row. It is the absence of a setting, and a menu that offers it beside the
    step it is identical to is the defect #117 was cut for -- on Qwen the chip read `reasoning off`
    while the model reasoned at xhigh, the dearest setting there is.
    """
    for name in group:
        if name != "off":
            return name
    return "off"


def reasoning_change_rerenders(current: str | None, wanted: str | None,
                               groups: tuple[tuple[str, ...], ...]) -> bool:
    """Whether moving from `current` to `wanted` actually changes the prompt.

    THE WARNING HAS TO BE ABLE TO STAY SILENT. REASONING_COST_NOTE says the next turn pays a full
    prefill, which is true across groups and FALSE within one: `off` -> `high` on Qwen moves no
    byte, so a client that warned there would be charging for nothing. Unmeasured -> warn, because
    an unpaid warning costs a sentence and an unwarned prefill costs minutes.
    """
    if not groups:
        return True
    here, there = reasoning_group_of(current, groups), reasoning_group_of(wanted, groups)
    if here is None or there is None:
        return True
    return here != there

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
        "Look something up beyond this machine -- a library version, a flag "
        "added recently, anything past your training. WHAT THIS REACHES DEPENDS "
        "ON THE INSTALLATION AND NOT ON THE QUESTION: with a general index "
        "configured it is the open web; with none it is an index of code, "
        "packages and reference, which settles a version or a flag and answers "
        "nothing about the world. The result says which on its first line -- "
        "read that line before you build on a hit, because without a general "
        "index a match can be a keyword collision rather than an answer. Read "
        "the snippets first: they usually settle it. Answer from what you read and "
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
    # #120. THE DESCRIPTION IS THE CURATION POLICY -- it is the only place the
    # model is told what belongs in a bounded store and what does not. Left to
    # itself it saves the conversation; the limit then fills with one afternoon.
    #
    # THERE IS NO PLACE PARAMETER, and that is the design rather than a missing
    # feature: project notes go to the project the chat stands in, because the
    # position decides and not the model. A choosable location is a button that
    # can be pressed wrong, and a misfiled note is only noticed much later, in
    # another project, where it is missing or in the way.
    _fn("memory",
        "Remember something across sessions, or correct what you remember. "
        "Save what you would want to know at the START of the next session and "
        "cannot cheaply look up again: how this project is laid out, its "
        "conventions and commands, a tool quirk you had to work around, a "
        "correction the user made, work you finished. Do NOT save what a single "
        "read would answer, raw output, or anything that is only true for this "
        "one turn. Entries are short and dense -- pack related facts into one "
        "line rather than adding a line each. The store is small and never "
        "trimmed for you: when it is nearly full, use 'replace' to merge two "
        "entries into one shorter entry before you add.",
        {"action": dict(_STR, description="add, replace or remove."),
         "target": dict(_STR, description="'memory' for facts about this project "
                                          "and machine, 'user' for who the user is "
                                          "and how they want to be worked with."),
         "content": dict(_STR, description="The new entry, for add and replace."),
         "old_text": dict(_STR, description="For replace and remove: a SHORT "
                                            "substring that occurs in exactly one "
                                            "entry. Not the whole entry.")},
        ["action"]),
    # #123. IT IS DECLARED ON EVERY MACHINE, INCLUDING THOSE WITHOUT FTS5, and
    # that is the opposite of what the ticket first said. Dropping it from the
    # schema where SQLite lacks FTS5 would make `json.dumps(TOOLS)` -- and
    # therefore `prefix_fingerprint`, and therefore every saved cache -- depend
    # on how somebody's Python was compiled. A session file would then stop
    # matching itself after a Python upgrade. So the tool is always there and
    # answers "unavailable, nothing was searched" where it cannot work.
    # #124. THE DESCRIPTION SAYS WHEN TO WRITE ONE, because left to itself a
    # model saves a summary of the turn it just had. A skill is worth writing
    # only if the NEXT session would otherwise repeat the work.
    _fn("skill",
        "Your own procedures, kept between sessions. The prompt lists the ones "
        "you have by name and description only -- call this with action=read to "
        "get the steps of one before you follow it. Call action=save when a "
        "conversation has worked out a repeatable WAY of doing something that "
        "would otherwise be rediscovered: the order of steps, the flags that "
        "worked, the check that catches the usual mistake. Do NOT save facts "
        "about this project -- those are `memory` -- and do not save a summary "
        "of what just happened. Rewrite an existing skill with save rather than "
        "adding a second one beside it.",
        {"action": dict(_STR, description="read, save or remove."),
         "name": dict(_STR, description="Lower-case letters, digits and hyphens."),
         "description": dict(_STR, description="One line: WHEN this applies. It is "
                                               "all the prompt carries about the "
                                               "skill, so it decides whether the "
                                               "skill is ever chosen."),
         "body": dict(_STR, description="For save: the steps, in full.")},
        ["action"]),
    _fn("session_search",
        "Search everything said in earlier conversations, including ones from "
        "months ago. Costs no context until you call it, so use it instead of "
        "guessing when the user refers to something you cannot see -- 'the thing "
        "we decided last week', a number, a path, a name. Returns the real "
        "messages, not a summary.",
        {"query": dict(_STR, description="Words to look for. All of them must "
                                         "appear; two or three specific ones beat "
                                         "a sentence."),
         "limit": {"type": "integer",
                   "description": f"How many messages, default {SEARCH_HITS}."}},
        ["query"]),
]

# THE TWELVE AS SHIPPED, AND THE FLOOR EVERY LATER ENTRY SITS ON. `TOOLS` grows
# from `mcp.json` at import (see MCP_FILE), and it grows IN PLACE because
# `crow.py` binds the value rather than the name. Keeping the built-ins under a
# second name is what lets that rebuild be idempotent: drop everything above the
# floor, then add. A count would do the same job and say nothing about why.
BUILTIN_TOOLS = tuple(TOOLS)


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
    # LAST, and that is a statement about the README and not about the server: this list is the
    # order the printed line is checked in, so a flag appended here is a flag appended there.
    ("spec_type", "--spec-type"),
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
SESSION_TOOLS_CLEARED_KEY = "tools_cleared"

# #121. THE PINNED MEMORY HEAD, and it is the same kind of fact as the two
# above: something about THIS CHAT rather than about this run. Absent means
# "never pinned" -- every chat file on disk today is in that state, and it must
# not read as "pinned to an empty memory", which is a claim nobody made.
#
# WHY IT IS WRITTEN AT ALL, given that the system message is already in
# `messages`: the fingerprint has to be computed BEFORE the payload is read.
# `load_session` needs the composed system prompt to decide whether the saved KV
# still fits, and it cannot get it from the messages it has not opened yet.
SESSION_MEMORY_KEY = "memory"

# #122. WHEN THE REVIEW RUNS, and it is not after every turn -- that was the
# first build and robin stopped it on 2026-08-21: "es soll ja auch nicht jede
# neue Zeile ins MEMORY, sondern nur was wichtig ist pro Unterhaltung".
#
# THREE TIMES PER WINDOW, at a fifth, a half and three quarters of the context.
# Each share fires at most once, so a conversation gets three reviews and never
# a fourth: one early, while the exchange is still small enough that a decision
# in it is easy to see; one when there is real material; and one more before the
# rollover at 0.9 takes the whole thing away.
#
# 0.20 IS THE SAFETY MARK (robin, 2026-08-21: "dann sind wir safe"). Plenty of
# conversations here never reach half a 200k window -- they are answered and
# closed -- and under two marks alone every one of those would end without
# anything having been written down at all.
#
# HERMES COUNTS TURNS INSTEAD -- every 10 user prompts, `_turns_since_memory`
# against `_memory_nudge_interval`. That does not transfer. A turn here can cost
# 20k tokens (`MAX_TOOL_BYTES` is ~4,000 tokens and `MAX_TOOL_ROUNDS` is 24), so
# ten prompts say nothing about how much conversation exists; measured on a live
# chat the same evening, fourteen rounds stood at 25.2k. The share measures the
# material, and it bounds the cost at two reviews per window rather than at
# however many prompts somebody types.
MEMORY_REVIEW_AT = (0.20, 0.50, 0.75)

# The highest share this chat has already been reviewed at. Absent is 0.0 here
# rather than a third state: "never reviewed" and "reviewed at 0%" are the same
# fact, unlike the pin, where "never" and "empty" are two different claims.
SESSION_REVIEWED_KEY = "reviewed"


def session_reviewed(path: str | None = None) -> float:
    """How far this chat has already been reviewed. 0.0 when it never was."""
    path = path or SESSION_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh).get(SESSION_REVIEWED_KEY)
    except Exception:                       # noqa: BLE001 - no file, no marks
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0


def review_due(context_tokens: int, n_ctx: int, reviewed: float) -> "float | None":
    """The share this turn crossed and has not been reviewed at, or None.

    ONE FIRING PER TURN, EVEN WHEN A TURN CROSSES BOTH. A single round can add
    tens of thousands of tokens, so a chat can go from 0.4 to 0.8 in one answer;
    reviewing twice back to back would ask the same question of the same
    conversation and pay for it twice. The higher mark is taken, because a
    review at 0.75 sees everything the one at 0.50 would have seen.

    n_ctx OF ZERO MEANS "THE SERVER WOULD NOT SAY", NOT "NO ROOM LEFT" -- the
    same reading `should_roll` gives it, and for the same reason: a division by
    an unknown window would fire on the first turn of every session where /props
    did not answer.
    """
    if n_ctx <= 0 or context_tokens <= 0:
        return None
    share = context_tokens / n_ctx
    crossed = [t for t in MEMORY_REVIEW_AT if share >= t and t > reviewed]
    return max(crossed) if crossed else None

# Said BEFORE the level is changed, the way #115 announces the lost context
# before the switch. The value lands in `chat_template_kwargs`, so it is
# rendered into the HEAD of the prompt -- byte 0 moves and the whole cached
# prefix stops matching. That is not a detail at 200k.
REASONING_COST_NOTE = "the level changes the head of every prompt -- the next turn pays a full prefill"

# #121. THE SAME BILL FOR THE SAME REASON, said the same way round: before the
# change, not after it. Binding a different folder to an open chat swaps that
# chat's project memory, and the memory sits in the head.
MEMORY_COST_NOTE = "the project memory changed -- the next turn pays a full prefill"

# #124. The same bill again, and said the same way round: before the change.
SKILL_COST_NOTE = "the skill list changed -- the next turn pays a full prefill"


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


def session_memory(path: str | None = None) -> str | None:
    """The memory head this chat was pinned to, or None if it never pinned one.

    THE ABSENT KEY IS A STATE, not a missing value -- see `SESSION_MEMORY_KEY`.
    A caller that reads None must decide what to pin; a caller that reads "" has
    been told the answer and must not go looking for a better one.
    """
    path = path or SESSION_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh).get(SESSION_MEMORY_KEY)
    except Exception:                       # noqa: BLE001 - no file, no pin
        return None
    return value if isinstance(value, str) else None


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
    groups = reasoning_groups_for(model)
    known = ", ".join(levels)
    wanted = (argument or "").strip().lower()

    def cost(now: str | None, then: str | None) -> str:
        # SILENT WITHIN A GROUP (#117). The note promises a full prefill, and that is a lie for a
        # change that renders the same bytes -- `off` -> `high` on Qwen moves nothing. An
        # unmeasured model still warns; see reasoning_change_rerenders for why that way round.
        return "\n" + REASONING_COST_NOTE if reasoning_change_rerenders(now, then, groups) else ""

    if not wanted:
        now = current or "not set -- nothing is sent, and the model uses its own default"
        return ("reasoning: %s\nlevels: %s, or off" % (now, known), current, False)

    if wanted == "off":
        if current is None:
            return ("reasoning is already unset.", None, False)
        return ("reasoning unset -- nothing is sent.%s" % cost(current, None), None, True)

    if wanted not in levels:
        # Named, and NOT sent. An invalid level reaching the server arrives as a
        # template exception AFTER the prefill has been paid for.
        return ("no level %r for %s. There is: %s, or off"
                % (wanted, model or "this model", known), current, False)

    if wanted == current:
        return ("reasoning is already %s." % wanted, current, False)
    return ("reasoning: %s%s" % (wanted, cost(current, wanted)), wanted, True)


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
                 reasoning: str | None = None,
                 tools_cleared: int = 0) -> str | None:
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
                   # #121: ABSENT WHEN NEVER PINNED, for the reason the key's
                   # own comment gives. `conversation.memory` is None only for a
                   # chat that never pinned; "" is a pin, and it is written,
                   # because "this chat deliberately carries no memory" is a
                   # different fact from "nobody ever decided".
                   **({SESSION_MEMORY_KEY: conversation.memory}
                      if conversation.memory is not None else {}),
                   # #122: written only once it has happened, so a file from
                   # before this build reads as 0.0 -- which is true of it.
                   **({SESSION_REVIEWED_KEY: conversation.reviewed}
                      if conversation.reviewed else {}),
                   **({SESSION_REASONING_KEY: reasoning} if reasoning else {}),
                   # #131: HOW MANY TOOL ROWS THE USER HAS ALREADY DISMISSED.
                   # The conversation itself is untouched -- the model still has
                   # every call it made -- this is a VIEW fact: a reopened chat
                   # replays its tool rows, so without a watermark a list
                   # somebody emptied comes back at the next start. Absent means
                   # nothing was ever cleared, which is true of every file
                   # written before this build.
                   **({SESSION_TOOLS_CLEARED_KEY: tools_cleared}
                      if tools_cleared else {})},
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


def session_tools_cleared(path: str | None = None) -> int:
    """How many tool rows this chat has already had dismissed.

    A COUNT AND NOT A FLAG, so that clearing means "everything up to here" and a
    call made afterwards still shows. robin, 2026-08-22: what was deleted stays
    deleted, unless something new happened.
    """
    try:
        with open(path or SESSION_FILE, encoding="utf-8") as fh:
            value = json.load(fh).get(SESSION_TOOLS_CLEARED_KEY)
    except (OSError, ValueError, AttributeError):
        return 0
    return value if isinstance(value, int) and value > 0 else 0


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

    def __init__(self, system: str | None = None, memory: str | None = None) -> None:
        self._base_system = system
        # #121. None means "never pinned", "" means "pinned to nothing" -- the
        # same three-way shape #101 gave the working directory, and for the same
        # reason: every chat file written before this build lacks the key, and
        # reading that as "pinned to nothing" would be a claim nobody made.
        self._pinned: str | None = None
        # #122. How far this chat has been reviewed, so the two marks fire once
        # each. It rides on the conversation because that is what a chat IS to
        # every caller here, and because `reset()` then clears it with the rest.
        self._reviewed = 0.0
        self._system = system
        self._messages: list[dict[str, str]] = []
        if system:
            self._messages.append({"role": "system", "content": system})
        if memory is not None:
            self.pin_memory(memory)

    @property
    def has_system(self) -> bool:
        return bool(self._system)

    @property
    def system(self) -> str | None:
        return self._system

    @property
    def memory(self) -> str | None:
        """The pinned memory head, or None when this chat never pinned one."""
        return self._pinned

    @property
    def reviewed(self) -> float:
        """The highest share the background review has already run at."""
        return self._reviewed

    def mark_reviewed(self, share: float) -> None:
        """Record a review. Never moves backwards, so adopting a saved mark and
        recording a fresh one are the same call and cannot undo each other."""
        self._reviewed = max(self._reviewed, float(share or 0.0))

    def pin_memory(self, block: str | None) -> None:
        """Fix the memory head for the LIFE OF THIS CHAT. Once, before the first request.

        THIS IS THE WHOLE OF #121 IN ONE METHOD. llama-server reuses a prompt by
        matching a common token prefix -- `n_past =
        slot.prompt.tokens.get_common_prefix(input_tokens)` -- and the system
        prompt is where that prefix begins. `prefix_fingerprint` hashes it, and
        Crow keeps its KV on disk. So a memory head that were re-read from the
        files at every start would go stale against every saved cache the moment
        anything was saved: measured 2026-08-10, a cache that does not match is
        `cached 0/21004` and 469.51 s to the first token.

        Pinning also turns the guarantee into something a test can hold. "The
        head does not change mid-session" is a claim about TIME and cannot be
        checked; "the head is what this file says" is a claim about a FILE.

        RAISES ON THE SECOND CALL rather than quietly re-pinning, which is the
        same refusal `restore()` makes and for the same reason: after the first
        request a prefix exists, and moving it is not an update, it is a bill.
        """
        if self._pinned is not None:
            raise RuntimeError("memory is pinned for the life of a chat, not per turn")
        self._write_head(block or "")

    def repin_memory(self, block: str | None) -> bool:
        """Move the head of a RUNNING chat. Returns True when it actually moved.

        THE ONE EVENT THAT JUSTIFIES THIS is a person binding a different working
        directory to an open chat. They have just said which project this
        conversation belongs to, and answering "you get that project's memory in
        the next chat" would be a rule nobody asked for -- robin, 2026-08-21:
        a user moves a project chat into its project, and that move is when the
        memory should follow.

        IT COSTS A FULL PREFILL, the same bill `REASONING_COST_NOTE` announces
        for the thinking level and for exactly the same mechanism: the head moves,
        so `get_common_prefix` ends at byte 0 and the whole conversation is read
        again. The caller says so BEFORE calling, never after -- and the return
        value is there so that a bind which changes nothing says nothing.

        THIS IS NOT THE EDIT THE CLASS DOCSTRING REFUSES. What moves is the
        system message, which is not a turn: `reset()` has always replaced it
        wholesale. No turn is edited, removed or reordered here.
        """
        if self._pinned is not None and (block or "") == self._pinned:
            return False
        self._write_head(block or "")
        return True

    def _write_head(self, block: str) -> None:
        """The one place the head is composed and put into message 0.

        IT REPLACES A HEAD, IT NEVER INVENTS ONE. A conversation restored from a
        payload that carries no system message has no head, and giving it one
        here would insert a message into somebody else's history -- the exact
        edit this class refuses everywhere else. `__init__` and `reset()` follow
        the same rule (`if system:` on an EMPTY message list), so all three
        agree about when message 0 exists.
        """
        self._pinned = block
        self._system = system_with_memory(self._base_system, self._pinned)
        if self._messages:
            if self._messages[0].get("role") == "system":
                self._messages[0] = {"role": "system", "content": self._system}
        elif self._system:
            self._messages.append({"role": "system", "content": self._system})

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
        # #121. A PINNED CONVERSATION OWNS ITS HEAD, so the restored payload's
        # system message is brought into line with it. Not tidiness: the head is
        # what the next request will actually send and what the next save will
        # fingerprint, and a payload written under a different one would leave
        # the file describing a prompt the request does not carry.
        #
        # ONLY WHEN A PIN EXISTS. Without one this is the behaviour every release
        # up to here had -- the saved head is the head -- and changing that for
        # unpinned chats would rewrite the first message of every session on disk
        # in a commit that is supposed to add a key.
        if self._pinned is not None and self._system:
            if self._messages and self._messages[0].get("role") == "system":
                self._messages[0] = {"role": "system", "content": self._system}
            else:
                self._messages.insert(0, {"role": "system", "content": self._system})

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
        # #121. THE PIN GOES WITH THE CONVERSATION, because `reset` is not a
        # cleanup -- it is the start of a NEW chat, and a new chat has not been
        # pinned yet. Keeping the old head here would hand the next conversation
        # the memory of the project the last one happened to stand in, which is
        # the same mistake `reset()` stopped making with the working directory.
        # The caller pins again once the new chat's boundary is known.
        #
        # A conversation that never pinned is unaffected: `_system` is
        # `_base_system` for it, so this is the line every release up to here had.
        self._pinned = None
        self._reviewed = 0.0
        self._system = self._base_system
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
        # Only when asked for. Sending nothing keeps the prompt byte-identical to a client that
        # predates the switch, which is what the prompt cache wants.
        #
        # THIS COMMENT USED TO SAY 0731's TEMPLATE READS AN ABSENT KEY AS "low". It does not, and
        # #117 measured it: manifests/0731-chat-template.jinja sets the absent key to `none`
        # (lines 12-13) and names reasoning_effort in exactly ONE condition, line 64,
        # `thinking and reasoning_effort == 'max'`. There is no low branch and no high branch, so
        # off, low and high all render sha256 fb2ba7d332bc there and only max differs. What an
        # absent key means is per model and belongs in the manifest -- see reasoning_groups_for.
        #
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


def projects() -> list[str]:
    """The roots a person promoted to projects, in the order they were added.

    #119. A THIRD FACT, A THIRD KEY, for the reason the comment above `recent`
    and `active` states: two facts on one key held only until they disagreed.

    NOT `recent`, AND THAT IS THE WHOLE REASON THIS KEY EXISTS. `recent` is the
    picker's memory -- "last chosen, by anybody", capped at eight so the menu
    stays a menu. A project is not a memory of a click; it is a place the user
    said they work in, and the ninth folder they ever open must not push it out
    of the rail.

    OLDEST FIRST, unlike `recent`. That list is ordered by when it was touched
    because a picker wants the last thing; this one is a set of standing places,
    and a rail that reorders itself under the mouse is a rail nobody can aim at.

    THE SAME FILTER `known_roots` APPLIES, and for the same reason: a directory
    whose `root.json` is gone is not a root any more, and drawing it as a project
    would offer a boundary that silently does not exist.
    """
    paths = _roots_doc().get("projects")
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
    return out


def is_project(root: str | None) -> bool:
    """Is this exact directory one of the projects?

    EXACT, NOT AN ANCESTOR WALK, and that is a decision rather than a shortcut.
    `find_root` deliberately takes the NEAREST marker and not the highest, so a
    sub-directory that declares itself is its own root; treating it as part of
    the project above it would contradict the rule the rest of the file is built
    on. A chat bound to `Crow/cli` belongs to `Crow/cli`, and if that is not a
    project, the chat has no project.
    """
    if not root:
        return False
    key = os.path.normcase(_resolve(root))
    return any(os.path.normcase(p) == key for p in projects())


def add_project(root: str) -> bool:
    """Declare `root` a root if it is not one yet, and list it as a project.

    RETURNS FALSE WHEN THE MARKER COULD NOT BE WRITTEN, and nothing is listed in
    that case. A project whose directory refused the marker would be drawn in
    the rail, filtered straight back out by `projects`, and look like a click
    that did nothing -- which is exactly the state that is hardest to report.

    `write_root_mode` IS THE ONLY THING THAT CREATES A ROOT (2026-08-14), so it
    is called here rather than the file being written by hand: a second writer
    is a second answer to "what is a root", and the first one is the security
    boundary.

    IDEMPOTENT. Adding a project twice is a click somebody repeated, not an
    error, and it must not put the same folder in the rail twice.
    """
    root = _resolve(root)
    if not os.path.isfile(root_file(root)):
        if not write_root_mode(root, DEFAULT_MODE):
            return False
    doc = _roots_doc()
    listed = doc.get("projects")
    listed = [p for p in listed if isinstance(p, str)] if isinstance(listed, list) else []
    key = os.path.normcase(root)
    if not any(os.path.normcase(p) == key for p in listed):
        listed.append(root)
    doc["projects"] = listed
    _write_doc(doc)
    return True


def drop_project(root: str) -> None:
    """Take `root` off the project list. The directory and its chats are untouched.

    THE MARKER STAYS. Removing `.crow/root.json` would un-root a directory that
    chats are still bound to, and a boundary that disappears because a list was
    tidied is the failure mode the whole root mechanism exists to prevent. This
    removes a row from the rail and nothing else.
    """
    doc = _roots_doc()
    listed = doc.get("projects")
    if not isinstance(listed, list):
        return
    key = os.path.normcase(_resolve(root))
    doc["projects"] = [p for p in listed
                       if not (isinstance(p, str) and os.path.normcase(p) == key)]
    _write_doc(doc)


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


# ---------------------------------------------------------------- #120 -----
# PERSISTENT MEMORY. Two stores, and the working directory decides which one a
# chat gets:
#
#   <root>\.crow\MEMORY.md          what was learned about THIS project
#   %LOCALAPPDATA%\Crow\USER.md     who the user is and how they work
#
# A THIRD, GLOBAL `MEMORY.md` WAS DESIGNED AND DROPPED, and the reason is
# written here so that it is not designed a second time. It was meant for chats
# with no working directory -- but a chat that never chose binds the template
# from `roots.json` (#101), so that state is nearly unreachable, and a second
# notes store for the remainder would be a second place for the same kind of
# fact. What is left is the one honest case: "no folder", chosen on purpose.
# Such a chat gets NO project memory and is told so. It does not get a
# substitute somewhere nobody picked.
#
# THE LIMITS ARE ANCHORED ON `MAX_TOOL_BYTES`, NOT ON THE CONTEXT WINDOW. The
# comment up there prices 16,000 bytes at ~4,000 tokens, so four characters buy
# one token. 4,000 characters is a QUARTER of a single tool read, and that ratio
# is the whole point: memory has to stay cheaper than letting the model read the
# file. A store that outgrows it is not a store that needs a bigger limit, it is
# a file that should be reached with `read_file`.
#
# Coupling the limit to `-c` was considered and refused. A MEMORY.md written at
# 200k would be over the limit at 40k without anyone changing it, and
# `check_operating_point` compares the operating point as raw text -- it may not
# become a function.
MEMORY_FILE = "MEMORY.md"
MEMORY_CHARS = 4_000
USER_CHARS = 1_500

# Beside `roots.json` and `settings.json`, for the reason `ROOTS_FILE` already
# gives: a session is one conversation, and who the user is outlives every one
# of them.
USER_PATH = os.path.join(os.path.dirname(SESSION_DIR), "USER.md")

# What separates two entries. A section sign alone on its line: it does not
# occur inside a path, a command or a sentence the model writes, and someone
# opening the file by hand sees the boundary without needing a legend.
MEMORY_SEP = "§"

# Above this share the header tells the model to consolidate before it adds.
# ADVICE, NOT A SECOND GATE -- the gate is the limit itself, below.
MEMORY_FULL_AT = 0.8

MEMORY_TARGETS = ("memory", "user")


# ---------------------------------------------------------------- #124 -----
# SKILLS. A procedure the model worked out once, written down so the next
# session starts from it instead of from scratch. Memory is what is TRUE;
# a skill is what to DO.
#
#   %LOCALAPPDATA%\Crow\skills\<name>\SKILL.md
#
# GLOBAL, NOT PER PROJECT (robin, 2026-08-21). A procedure that only works in
# one directory is not a procedure, it is a note -- and notes already have a
# home one section up.
#
# A DIRECTORY PER SKILL, NOT A FILE, which costs nothing today and is the
# difference between adding a script to a skill later and changing the format
# later. Both of the clients this is modelled on landed on the same shape
# independently.
#
# ONLY THE NAME AND THE DESCRIPTION GO INTO THE PROMPT. The body is read when
# the skill applies, through the ordinary `read_file` budget. This is the exact
# INVERSE of memory, and the two inversions have one cause: memory is small
# enough to always carry and useless if it has to be fetched; a skill is too
# big to always carry and perfectly useful fetched. That is also why `skill`
# HAS a `read` action while `memory` deliberately has none.
SKILL_FILE = "SKILL.md"
SKILLS_DIR = os.path.join(os.path.dirname(SESSION_DIR), "skills")

# The whole listing, however many skills exist. AN EIGHTH OF ONE TOOL READ, and
# a cap on the LIST rather than on each entry, because the failure this bounds
# is twenty skills of legal length, not one long one. The twelfth skill costs
# nothing extra -- it simply does not fit, and the head says so instead of
# growing.
SKILL_HEAD_CHARS = 2_000
SKILL_DESC_CHARS = 200
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")


# ONE SKILL SHIPS WITH THE CLIENT, and it is the one about writing skills.
#
# WHY IT IS SEEDED AND NOT HARD-WIRED. It could have been a constant the head
# always carried, and that would have been the wrong shape twice over: it could
# not be switched off in the sheet like every other skill, and it could not be
# edited by the person whose procedures it is meant to describe. Seeded, it is
# an ordinary file -- same directory, same frontmatter, same switch.
#
# WHY THE MODEL NEEDS IT AT ALL. Without it the only guidance is the sentence in
# the tool description, which is enough to make the model save SOMETHING and not
# enough to make it save something usable: the failure is a skill whose
# description says "helps with the project", which can never be chosen, and a
# body that recounts one conversation instead of naming the steps.
BUILTIN_SKILLS = (
    ("skill-creator",
     "When a conversation has worked out a repeatable procedure and you are "
     "about to save it: read this first for what a good skill looks like.",
     """A skill is a procedure you keep. Memory is what is TRUE about a project;
a skill is what to DO. If it has no steps, it is memory.

## Save one only when all three hold

1. It WORKED here, in this conversation. Not a plan, not a guess, not something
   you know in general -- something you watched succeed.
2. The next session would otherwise redo the work: find the flags again, hit the
   same trap again, rebuild the same order of steps.
3. It is not already covered. If a skill nearly covers it, use `save` on THAT
   name to sharpen it. Two skills for one job means neither gets chosen.

Saying nothing is the normal outcome. A wrong skill costs every future session.

## The description decides whether the skill is ever used

It is the ONLY thing the prompt carries about you. The body is invisible until
somebody reads it, so a description that does not say WHEN to reach for the
skill guarantees it is never reached for.

  bad:  "Helps with measurements."          -- when? nobody can tell
  bad:  "A guide to the measurement setup."  -- describes itself, not its moment
  good: "When a measurement needs more than one run: write the script BEFORE the
         series, never after."

Name the situation, in the words that situation actually arrives in. Two hundred
characters, one line, no line breaks.

## The body

Write it for yourself in three months, having forgotten everything.

- Numbered steps, in order, each one an action.
- Commands and flags VERBATIM. A remembered flag is a wrong flag.
- Say what each step is supposed to produce, so a failure is visible at the step
  that caused it rather than at the end.
- One line on the trap: the mistake that was actually made here, and what it
  looked like. This is the most valuable thing in the file.
- No summary of the conversation. No "we then decided". Steps only.

## Naming

The name IS the directory: lower case, digits, hyphens, 3 to 50 characters. Name
the JOB, not the topic -- `run-a-measurement-series` beats `measurements`,
because the first one tells you when you need it.

## Keeping them

Rewrite with `save` under the same name; it replaces the file and keeps the
on/off switch as it was. `remove` when the procedure has stopped being true --
a skill that describes a workflow nobody uses any more is worse than no skill,
because it will still be chosen."""),
)


def seed_skills() -> int:
    """Write the shipped skills, ONCE, the first time this machine has any.

    THE DIRECTORY'S ABSENCE IS THE "NEVER BEEN HERE" STATE, which is why the
    check is on the directory and not on the files in it. Seeding per missing
    FILE would mean a skill the user deleted came back at the next start, and a
    deletion that undoes itself is not a deletion. Deleting the whole directory
    does bring them back -- that is a documented reset, not an accident.
    """
    if os.path.isdir(SKILLS_DIR):
        return 0
    for name, description, body in BUILTIN_SKILLS:
        write_skill(name, description, body)
    return len(BUILTIN_SKILLS)


def skill_dir(name: str) -> str:
    return os.path.join(SKILLS_DIR, name)


def skill_path(name: str) -> str:
    return os.path.join(skill_dir(name), SKILL_FILE)


def parse_skill(text: str) -> "tuple[dict, str]":
    """(frontmatter, body) out of a SKILL.md. Both halves survive a broken file.

    HAND-WRITTEN FILES ARE THE NORMAL CASE, not an edge one: the whole point of
    plain Markdown with a fence is that a person can open it. So a file with no
    fence at all is a body with no metadata rather than an error, and a key
    without a colon is skipped rather than fatal.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    head = {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return head, "\n".join(lines[i + 1:]).strip()
        key, sep, value = lines[i].partition(":")
        if sep:
            head[key.strip().lower()] = value.strip()
    # An unterminated fence is a file somebody is in the middle of writing.
    return head, ""


def read_skill(name: str) -> "dict | None":
    """One skill as {name, description, enabled, body}, or None if it is not there.

    THE NAME COMES FROM THE DIRECTORY, not from the frontmatter, and that is the
    one place the two could disagree. A directory is unique by construction and a
    key is not, so the directory wins -- a `name:` that says something else is
    ignored rather than obeyed.
    """
    try:
        with open(skill_path(name), encoding="utf-8") as fh:
            head, body = parse_skill(fh.read())
    except Exception:                       # noqa: BLE001 - absent is None
        return None
    return {"name": name,
            "description": (head.get("description") or "")[:SKILL_DESC_CHARS],
            # ABSENT MEANS ON. A skill somebody wrote by hand, with no `enabled`
            # line, is a skill they want -- reading that as "off" would hide it
            # and give them nothing to click, because the row would not be drawn.
            "enabled": (head.get("enabled") or "true").strip().lower() != "false",
            "body": body}


def skills() -> "list[dict]":
    """Every skill on disk, by name. Enabled and disabled alike -- the settings
    sheet has to draw the ones that are off, or they cannot be switched on."""
    seed_skills()
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return []
    out = []
    for name in names:
        skill = read_skill(name)
        if skill is not None:
            out.append(skill)
    return out


def write_skill(name: str, description: str, body: str,
                enabled: bool = True) -> None:
    """Write one SKILL.md whole. Creates the directory."""
    os.makedirs(skill_dir(name), exist_ok=True)
    text = ("---\nname: %s\ndescription: %s\nenabled: %s\n---\n\n%s\n"
            % (name, description.strip()[:SKILL_DESC_CHARS], "true" if enabled else "false",
               body.strip()))
    with open(skill_path(name), "w", encoding="utf-8") as fh:
        fh.write(text)


def set_skill_enabled(name: str, enabled: bool) -> bool:
    """Flip one skill on or off, keeping everything else in the file.

    IT REWRITES THE FILE RATHER THAN A SECOND LIST, which is the whole reason
    `enabled` lives in the frontmatter: the switch in the settings sheet and a
    person editing the file by hand are the same act, and there is no third
    place that can disagree with either.
    """
    skill = read_skill(name)
    if skill is None or skill["enabled"] == enabled:
        return False
    write_skill(name, skill["description"], skill["body"], enabled)
    return True


def skill_block() -> str:
    """What the prompt says about skills: the enabled ones, name and description.

    THE BODY IS NOT IN HERE and that is the design. What the model needs in
    every prompt is enough to know a skill EXISTS and when it applies; what it
    needs at that moment is the body, and one `skill` call fetches it.

    THE LIST IS CUT AT THE LIMIT AND SAYS SO. A silent truncation would leave
    the model certain it had seen every skill, which is worse than knowing that
    three are out of view -- it would stop looking.
    """
    rows, used, dropped = [], 0, 0
    for skill in skills():
        if not skill["enabled"]:
            continue
        line = "  %s -- %s" % (skill["name"], skill["description"] or "(no description)")
        if used + len(line) + 1 > SKILL_HEAD_CHARS:
            dropped += 1
            continue
        rows.append(line)
        used += len(line) + 1
    if not rows and not dropped:
        return ""
    head = ("SKILLS (call `skill` with action=read and the name to get the steps)\n"
            + "\n".join(rows))
    if dropped:
        head += "\n  [%d more did not fit -- ask for a name you remember]" % dropped
    return head


def tool_skill(action: str, name: "str | None" = None,
               description: "str | None" = None, body: "str | None" = None) -> str:
    """Read, write or drop one skill. Returns JSON, like `memory` beside it.

    `save` IS CREATE AND REPLACE IN ONE ACTION, unlike `memory`'s three. A skill
    is one document with a name, so "write this down under that name" is the
    whole operation; splitting it would make the model ask whether the skill
    already exists before it could write one.
    """
    if action == "read":
        skill = read_skill(name or "")
        if skill is None:
            return json.dumps({"success": False,
                               "error": "no skill named %r. There is: %s"
                                        % (name, ", ".join(s["name"] for s in skills())
                                           or "(none)")})
        return json.dumps({"success": True, "name": skill["name"],
                           "description": skill["description"], "body": skill["body"]})

    if action == "save":
        if not name or not SKILL_NAME.match(name):
            return json.dumps({"success": False,
                               "error": "name must be lower-case letters, digits and "
                                        "hyphens, 3 to 50 characters -- got %r" % name})
        if not description or not description.strip():
            return json.dumps({"success": False,
                               "error": "a skill without a description can never be "
                                        "chosen: the description is all the prompt "
                                        "carries about it."})
        if not body or not body.strip():
            return json.dumps({"success": False, "error": "save needs a body"})
        # THE DESCRIPTION IS SCANNED, THE BODY IS NOT, and the split is the same
        # rule the memory scan follows: what lands in the system prompt is
        # checked, what is fetched on demand is a tool result like any other.
        # Scanning bodies was considered and dropped -- Hermes ships that
        # scanner OFF because real procedures legitimately touch `~/.ssh/` and
        # name API keys, and a filter that eats those gets routed around.
        why = memory_threat(description)
        if why:
            return json.dumps({"success": False, "error": "refused: %s" % why})
        existed = read_skill(name) is not None
        keep = read_skill(name)["enabled"] if existed else True
        write_skill(name, description, body, keep)
        return json.dumps({"success": True, "action": "replaced" if existed else "created",
                           "name": name})

    if action == "remove":
        if read_skill(name or "") is None:
            return json.dumps({"success": False, "error": "no skill named %r" % name})
        shutil.rmtree(skill_dir(name), ignore_errors=True)
        return json.dumps({"success": True, "action": "remove", "name": name})

    return json.dumps({"success": False,
                       "error": "unknown action %r -- use read, save or remove" % action})


def memory_path(root: "str | None" = None) -> "str | None":
    """Where this chat's PROJECT memory lives, or None when it has no folder.

    None is an answer, not a failure: it is the "no folder" state of #101
    arriving here, and every caller has to have a sentence for it rather than a
    fallback path. A substitute location would be a boundary nobody drew.
    """
    here = get_root() if root is None else root
    if not here:
        return None
    return os.path.join(here, ROOT_MARKER, MEMORY_FILE)


def _store(target: str, root: "str | None" = None) -> "tuple[str | None, int, str]":
    """(path, limit, label) for one target. Path is None only for a rootless chat."""
    if target == "user":
        return USER_PATH, USER_CHARS, "USER PROFILE"
    here = get_root() if root is None else root
    label = "MEMORY -- %s" % os.path.basename(here.rstrip("\\/")) if here else "MEMORY"
    return memory_path(root), MEMORY_CHARS, label


def read_store(path: "str | None") -> "list[str]":
    """The entries in one store file. A missing file is an empty store.

    UNREADABLE IS ALSO EMPTY, and that is deliberate rather than lazy: this runs
    on the path that builds the system prompt, and a store that cannot be parsed
    must not take the start of a session down with it. The write path is where a
    problem is reported, because that is where someone is waiting for an answer.
    """
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except Exception:                       # noqa: BLE001 - no file, no entries
        return []
    out = []
    for chunk in raw.split("\n%s\n" % MEMORY_SEP):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def write_store(path: str, entries: "list[str]") -> None:
    """Replace a store with these entries. Creates `.crow` if it is missing."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    body = ("\n%s\n" % MEMORY_SEP).join(entries)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body + ("\n" if body else ""))


def store_chars(entries: "list[str]") -> int:
    """What a set of entries costs against its limit: the file as it is written.

    COUNTING THE SEPARATORS IS THE HONEST VERSION. The limit exists to bound the
    prompt, and the separators are rendered into the prompt too. Counting only
    the entries would let ten short ones cost more than the limit allows while
    the arithmetic said they fit.
    """
    if not entries:
        return 0
    return len(("\n%s\n" % MEMORY_SEP).join(entries))


# THE SCAN IS NARROW ON PURPOSE. Every entry is rendered into the system prompt,
# so a memory entry is the one place where text the model READ can become text
# the model OBEYS -- a file carrying "ignore previous instructions", saved as a
# note, is an injection with a delay fuse.
#
# But a filter that refuses ordinary notes is a filter that gets routed around,
# and a coding assistant's memory is full of what a broad one would eat: `curl`,
# `http://localhost`, "run as administrator", key paths, shell lines. So these
# are the narrow, high-signal patterns only -- instruction override, forged
# prompt boundaries, private key material -- plus the invisible-character check,
# which has no legitimate use in a note at all.
_MEMORY_THREATS = (
    (re.compile(r"\bignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
                r"(?:instructions?|prompts?|rules?)", re.I), "instruction override"),
    (re.compile(r"\bdisregard\s+(?:all\s+)?(?:the\s+)?"
                r"(?:previous|prior|above|earlier)\b", re.I), "instruction override"),
    (re.compile(r"</?\s*(?:system|instructions?)\s*>", re.I), "forged prompt boundary"),
    (re.compile(r"^\s*\[\s*system\s*\]", re.I | re.M), "forged prompt boundary"),
    (re.compile(r"-----BEGIN\s+(?:[A-Z0-9 ]+\s+)?PRIVATE KEY-----"), "private key material"),
    (re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+AAAA"), "ssh key material"),
)


def memory_threat(text: str) -> "str | None":
    """Why this entry may not be stored, or None when it may.

    Cf is Unicode's "format" category -- zero-width joiners, bidi overrides, the
    soft hyphen. None of them can be SEEN in a rendered prompt and all of them
    can change what it says, which is the whole trick. A newline is not Cf, so
    multi-line entries are untouched by this.
    """
    import unicodedata

    for pattern, why in _MEMORY_THREATS:
        if pattern.search(text):
            return why
    hidden = sorted({"U+%04X" % ord(ch) for ch in text
                     if unicodedata.category(ch) == "Cf"})
    if hidden:
        return "invisible characters (%s)" % ", ".join(hidden)
    return None


def render_store(label: str, entries: "list[str]", limit: int) -> str:
    """One block of the injected head: rule, header with usage, then the entries.

    THE USAGE IS IN THE HEADER BECAUSE THE MODEL NEEDS IT. It is the only way it
    can know, before writing, whether it has to consolidate first -- and a write
    that discovers the limit by failing has already cost a round.
    """
    used = store_chars(entries)
    pct = int(round(100.0 * used / limit)) if limit else 0
    rule = "=" * 46
    head = "%s\n%s [%d%% -- %s/%s chars]\n%s" % (
        rule, label, pct, "{:,}".format(used), "{:,}".format(limit), rule)
    body = ("\n%s\n" % MEMORY_SEP).join(entries)
    return head + ("\n" + body if body else "")


def memory_block(root: "str | None" = None) -> str:
    """The whole memory head for one chat, or "" when there is nothing to say.

    ORDER IS FIXED AND NOT SORTABLE -- profile, then project. This lands at byte
    0 of the prompt, where llama-server matches a common token prefix; two
    orders would be two caches for one set of facts.

    NOTHING REMEMBERED MEANS NOTHING IN THE HEAD -- not a pair of empty frames
    announcing two stores with nothing in them. Two reasons, and the second is
    the one that decided it:

      * An empty block reads as "nothing was learned here", which is a different
        claim from "there is nothing here", and it is the more dangerous of the
        two because it looks answered.
      * Until something is actually remembered, this feature must cost the
        prompt NOTHING. A head that appears on a fresh installation would change
        byte 0 for every existing chat on every machine, in exchange for two
        headers saying 0%.

    A ROOTLESS CHAT GETS NO SECOND BLOCK EITHER, but it does get the line saying
    why -- once there is anything else in the head at all. Silence about the
    project would otherwise be indistinguishable from an empty project.
    """
    blocks = []
    user = read_store(USER_PATH)
    if user:
        blocks.append(render_store("USER PROFILE", user, USER_CHARS))
    path = memory_path(root)
    entries = read_store(path)
    if entries:
        blocks.append(render_store(_store("memory", root)[2], entries, MEMORY_CHARS))
    elif path is None and blocks:
        blocks.append("(no working directory bound -- this chat has no project memory)")
    return "\n\n".join(blocks)


def prompt_head(root: "str | None" = None) -> str:
    """Everything this chat carries above the conversation: memory, then skills.

    ONE FUNCTION, BECAUSE ONE STRING IS PINNED. The chat file holds a single
    head; if two callers composed memory and skills in two orders, two chats
    would carry two byte-different heads for one set of facts and neither could
    reuse the other's cache.

    ORDER IS FIXED: what is TRUE before what to DO. Not a preference -- it is
    part of the prefix, and a sortable head is two caches.
    """
    parts = [p for p in (memory_block(root), skill_block()) if p]
    return "\n\n".join(parts)


def system_with_memory(system: "str | None", block: "str | None") -> "str | None":
    """The system prompt this chat is actually sent, memory included.

    ONE FUNCTION, BECAUSE THE JOIN IS PART OF THE CACHE KEY. `prefix_fingerprint`
    hashes the system prompt; two surfaces joining these two strings with
    different whitespace would produce two fingerprints for one chat, and the
    second one to run would drop a perfectly good KV cache.
    """
    if not block:
        return system
    return ((system or "") + "\n\n" + block).strip()


def tool_memory(action: str, target: str = "memory",
                content: "str | None" = None, old_text: "str | None" = None) -> str:
    """Add, replace or remove one memory entry. Returns JSON.

    THERE IS NO `read` ACTION, and that is not an omission. The entries are
    already rendered into the system prompt the model is reading; a read action
    would be a second source for the same text and would spend a round telling
    the model what it can already see.

    JSON RATHER THAN A SENTENCE, unlike every other tool here, because the
    failure that matters carries structure: over the limit the model needs the
    current entries and both numbers in order to consolidate IN THE SAME TURN,
    and a prose line would make it guess at what is in there.

    NOTHING IS EVER DROPPED TO MAKE ROOM. A store that silently evicts on
    overflow evicts the wrong entry eventually and nobody learns when. The write
    fails, says by how much, and hands back what is in the way.
    """
    if target not in MEMORY_TARGETS:
        return json.dumps({"success": False,
                           "error": "unknown target %r -- use %s"
                                    % (target, " or ".join(MEMORY_TARGETS))})
    path, limit, label = _store(target)
    if path is None:
        # The one honest rootless case, answered rather than redirected.
        return json.dumps({"success": False,
                           "error": "this chat has no working directory, so it has no "
                                    "project memory. Use target 'user' for facts about "
                                    "the user, or bind a folder first."})
    entries = read_store(path)

    if action == "add":
        if not content or not content.strip():
            return json.dumps({"success": False, "error": "add needs content"})
        content = content.strip()
        why = memory_threat(content)
        if why:
            return json.dumps({"success": False,
                               "error": "refused: %s" % why})
        if content in entries:
            # Success, not failure: the wanted state is already the state.
            return json.dumps({"success": True, "note": "no duplicate added",
                               "usage": _usage(entries, limit)})
        room = store_chars(entries + [content])
        if room > limit:
            return _too_big(entries, limit, room - store_chars(entries),
                            "Adding costs")
        write_store(path, entries + [content])
        return json.dumps({"success": True, "action": "add", "target": target,
                           "usage": _usage(entries + [content], limit)})

    if action in ("replace", "remove"):
        if not old_text or not old_text.strip():
            return json.dumps({"success": False,
                               "error": "%s needs old_text -- a short substring "
                                        "unique to one entry" % action})
        hits = [i for i, e in enumerate(entries) if old_text in e]
        if not hits:
            return json.dumps({"success": False,
                               "error": "no entry contains %r" % old_text,
                               "current_entries": entries})
        if len(hits) > 1:
            # A pick by order would be a coin toss dressed as a result.
            return json.dumps({"success": False,
                               "error": "%r matches %d entries -- give a longer, "
                                        "unique substring" % (old_text, len(hits)),
                               "matches": [entries[i] for i in hits]})
        i = hits[0]
        if action == "remove":
            write_store(path, entries[:i] + entries[i + 1:])
            return json.dumps({"success": True, "action": "remove", "target": target,
                               "usage": _usage(entries[:i] + entries[i + 1:], limit)})
        if not content or not content.strip():
            return json.dumps({"success": False, "error": "replace needs content"})
        content = content.strip()
        why = memory_threat(content)
        if why:
            return json.dumps({"success": False, "error": "refused: %s" % why})
        after = entries[:i] + [content] + entries[i + 1:]
        # REPLACE IS BOUND BY THE LIMIT TOO. Swapping a short entry for a long
        # one is an addition wearing another name, and letting it through here
        # would be the one door around the gate.
        if store_chars(after) > limit:
            return _too_big(entries, limit, store_chars(after) - store_chars(entries),
                            "Replacing grows the store by")
        write_store(path, after)
        return json.dumps({"success": True, "action": "replace", "target": target,
                           "usage": _usage(after, limit)})

    return json.dumps({"success": False,
                       "error": "unknown action %r -- use add, replace or remove" % action})


def _usage(entries: "list[str]", limit: int) -> str:
    return "%s/%s" % ("{:,}".format(store_chars(entries)), "{:,}".format(limit))


def _too_big(entries: "list[str]", limit: int, cost: int, verb: str) -> str:
    """The refusal, with everything needed to fix it without another round.

    `cost` IS THE GROWTH OF THE STORE, not the length of the entry, and the two
    differ: a replace swaps one entry for another, and an add pays for the
    separator as well. Reporting the entry length would hand the model a number
    that does not add up against the usage beside it.
    """
    return json.dumps({
        "success": False,
        "error": "Memory at %s chars. %s %d chars, which would exceed the limit. "
                 "Consolidate now: use 'replace' to merge overlapping entries into "
                 "shorter ones, or 'remove' stale ones (see current_entries), then "
                 "retry -- all in this turn."
                 % (_usage(entries, limit), verb, cost),
        "current_entries": entries,
        "usage": _usage(entries, limit)})


# ---------------------------------------------------------------- #123 -----
# SEARCHING WHAT WAS SAID BEFORE. Memory is small on purpose; this is the other
# half -- everything ever said, searchable, and free.
#
# `index.db` IS AN INDEX AND NOT A SECOND STORE, and that sentence is the whole
# design. The truth stays the chat JSON; the database is built from it and is
# disposable. Delete it and it comes back. An index is not a second place for a
# fact, because it knows nothing the file does not say -- whereas a COPY of the
# messages, held as a source, would be exactly the appointment at which the two
# disagree and nobody can say which one is the conversation.
#
# Two consequences are load-bearing rather than nice: the file's mtime decides
# whether its rows are stale, and a row whose file has gone is dropped instead
# of being answered from.
ARCHIVE_DIR = "archiv"
INDEX_PATH = os.path.join(os.path.dirname(SESSION_DIR), "index.db")


def fts5_available() -> bool:
    """Whether this Python's SQLite was built with FTS5.

    ASKED, NOT ASSUMED. Measured 2026-08-21 on Python 3.13.3 / SQLite 3.49.1 --
    the version `install.ps1` measures against -- where it is present. That says
    nothing about a stranger's machine: FTS5 is a compile-time option, and the
    honest answer on a build without it is to drop the tool and SAY so, rather
    than to fail at the first search with a message about SQL.
    """
    import sqlite3
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        finally:
            con.close()
        return True
    except Exception:                       # noqa: BLE001 - absence is the answer
        return False


def index_sources(session_file: str | None = None) -> "list[str]":
    """Every chat file the index covers: the live one and the archive.

    ONE LIST, so that "searchable" and "in the rail" cannot drift apart. The
    window draws its rail out of the same two places.
    """
    live = session_file or SESSION_FILE
    out = [live] if os.path.exists(live) else []
    folder = os.path.join(os.path.dirname(live) or ".", ARCHIVE_DIR)
    try:
        for name in sorted(os.listdir(folder)):
            if name.startswith("chat-") and name.endswith(".json"):
                out.append(os.path.join(folder, name))
    except OSError:
        pass
    return out


def _index_connect(db_path: str | None = None):
    import sqlite3
    path = db_path or INDEX_PATH
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5("
                "body, path UNINDEXED, title UNINDEXED, role UNINDEXED, pos UNINDEXED)")
    return con


def sync_index(db_path: str | None = None,
               session_file: str | None = None) -> "tuple[int, int]":
    """Bring the index level with the files. Returns (files indexed, rows written).

    MTIME IS THE WHOLE FRESHNESS RULE. A file whose timestamp has not moved is
    not re-read; one that has moved has ALL its rows dropped and rewritten,
    because a conversation is append-only on disk but an archive can be replaced
    wholesale, and a partial update would leave the tail of a previous version
    answering searches.

    A FILE THAT IS GONE LOSES ITS ROWS. The alternative is an index that answers
    with text nobody can open any more, which is worse than not answering.
    """
    con = _index_connect(db_path)
    try:
        live = index_sources(session_file)
        seen = {}
        for path in live:
            try:
                seen[path] = os.path.getmtime(path)
            except OSError:
                pass
        known = dict(con.execute("SELECT path, mtime FROM files").fetchall())
        for gone in set(known) - set(seen):
            con.execute("DELETE FROM messages WHERE path = ?", (gone,))
            con.execute("DELETE FROM files WHERE path = ?", (gone,))
        files = rows = 0
        for path, mtime in seen.items():
            if known.get(path) == mtime:
                continue
            con.execute("DELETE FROM messages WHERE path = ?", (path,))
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:               # noqa: BLE001 - unreadable is not indexed
                con.execute("DELETE FROM files WHERE path = ?", (path,))
                continue
            title = (data.get("crow_title") or os.path.basename(path)).strip()
            for i, message in enumerate(data.get("messages") or []):
                body = (message.get("content") or "").strip()
                role = message.get("role") or ""
                # The system message is the HEAD, not something anybody said.
                # Indexing it would answer every search with the same prompt.
                if not body or role == "system":
                    continue
                con.execute("INSERT INTO messages (body, path, title, role, pos) "
                            "VALUES (?, ?, ?, ?, ?)", (body, path, title, role, i))
                rows += 1
            con.execute("INSERT OR REPLACE INTO files (path, mtime) VALUES (?, ?)",
                        (path, mtime))
            files += 1
        con.commit()
        return files, rows
    finally:
        con.close()


def _match_expression(query: str) -> str:
    """A user's words as an FTS5 MATCH expression, with its syntax defused.

    EVERY TOKEN IS QUOTED. FTS5's query language treats `-`, `*`, `:`, `(` and
    `"` as operators, so a perfectly ordinary question -- "what did we say about
    --slot-save-path?" -- is a syntax error rather than a search. Quoting each
    token makes it a phrase, and several phrases side by side are an implicit
    AND, which is what someone typing three words means.
    """
    tokens = [t for t in re.split(r"\s+", query.strip()) if t]
    return " ".join('"%s"' % t.replace('"', '""') for t in tokens)


def search_sessions(query: str, limit: int = SEARCH_HITS,
                    db_path: str | None = None,
                    session_file: str | None = None) -> "list[dict]":
    """Messages matching `query`, newest file first. Actual text, never a summary."""
    if not fts5_available():
        return []
    sync_index(db_path, session_file)
    expression = _match_expression(query)
    if not expression:
        return []
    con = _index_connect(db_path)
    try:
        rows = con.execute(
            "SELECT title, role, body, path FROM messages "
            "WHERE messages MATCH ? ORDER BY rank LIMIT ?",
            (expression, int(limit))).fetchall()
    except Exception:                       # noqa: BLE001 - a bad query is no hits
        return []
    finally:
        con.close()
    return [{"chat": t, "role": r, "text": b, "path": p} for t, r, b, p in rows]


def tool_session_search(query: str, limit: int | None = None) -> str:
    """Search every past conversation. Returns the messages, not a summary.

    NO SUMMARISATION AND NO TRUNCATION OF THE ANSWER SET, because the moment a
    search starts summarising it is a second model standing between the question
    and the transcript -- and the transcript is the only thing here that cannot
    be wrong. Individual messages are clipped, which is a length decision; what
    is returned is what was said.
    """
    if not fts5_available():
        return ("error: session_search is unavailable -- this Python's SQLite was "
                "built without FTS5. Nothing was searched.")
    hits = search_sessions(query, limit or SEARCH_HITS)
    if not hits:
        return "no past message matches %r." % query
    out = ["%d match(es) for %r:" % (len(hits), query)]
    for hit in hits:
        text = hit["text"]
        if len(text) > SEARCH_SNIPPET:
            text = text[:SEARCH_SNIPPET] + " [...]"
        out.append("\n-- %s (%s) --\n%s" % (hit["chat"], hit["role"], text))
    return "\n".join(out)


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
    # THE SCOPE COMES BEFORE EVERYTHING, including the registry notes: it is the
    # sentence that decides how much any of the rest is worth. Only on the
    # keyless path -- with a real index configured the results ARE a web search
    # and the warning would be a lie in the other direction.
    if not (SEARXNG_URL or TAVILY_KEY):
        lines.append(KEYLESS_SCOPE)
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
    # `lines` now carries the scope note on every keyless run, so "nothing was
    # found" can no longer be read off its emptiness. The results themselves are
    # what that question is about.
    if not results and not answers:
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
    "memory": tool_memory,
    "skill": tool_skill,
    "session_search": tool_session_search,
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
    # #120. `memory` IS NOT `writing`, and the difference is whose file it is.
    # The `writing` class exists because `write_file` and `edit_file` reach the
    # user's work -- source, notes, anything a wrong path could destroy. This one
    # reaches exactly two files that belong to the client itself, both bounded,
    # both plain text, both readable with an editor. Classing it as `writing`
    # would make `manual` stop and ask before every saved note, which IS the
    # write-approval gate -- and that gate was offered on 2026-08-21 and declined.
    # Naming the class `memory` keeps the classification true without any level
    # acting on it, the way `network` already does.
    "memory": "memory",
    # #123. `reading`, and it is the plainest case in the table: it opens files
    # this client wrote, on this machine, and returns what they say. No level
    # asks before a read, which is the rule that was already argued out above.
    # #124. `memory`'s class, not `writing`: it reaches two directories this
    # client owns, never the user's work. The reasoning is written out above.
    "skill": "memory",
    "session_search": "reading",
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


# ---------------------------------------------------------------- E2 ------
# MCP SERVERS. THE SCHEMA LIES ON DISK, AND `TOOLS` IS BUILT FROM IT.
#
# THE SENTENCE THE WHOLE STAGE HANGS ON: `TOOLS` may not move because a foreign
# server is slow. `prefix_fingerprint` hashes `json.dumps(TOOLS, sort_keys=True)`,
# llama-server reuses a prompt by common token prefix, and the KV state Crow
# keeps on disk was 212,742,060 bytes on 2026-08-21. A cache that does not fit
# is `cached 0/21004` and 469.51 s to the first token -- measured 2026-08-10.
#
# Hermes asks every server for `tools/list` at every start. For a cloud model
# that pays for the head in every turn anyway that is free. Here it would mean:
# an `npx` server that comes up slower than the client turns EVERY saved session
# into a full re-prefill, and nobody could see why. So:
#
#   schema            asked ONCE, when the server is added, then written here
#   `TOOLS` at start  read from this file, never from a server
#   connecting        only when a tool is actually CALLED (E3)
#   server is down    the CALL fails and the model says so. `TOOLS` is untouched
#
# Same construction `session_search` already has: the tool stays declared even
# where FTS5 is missing, so that the list cannot depend on how somebody's Python
# was compiled. A session file has to keep matching itself.
#
# WHAT IS DELIBERATELY NOT HERE: `notifications/tools/list_changed` is not acted
# on. A tool list that changes mid-chat moves byte 0. A server announcing new
# tools is a note for the user, not something that carries itself out.
#
# THE FILE, beside `roots.json` and `settings.json` because a server binds the
# machine and not one conversation:
#
#   %LOCALAPPDATA%\Crow\mcp.json
#   {"servers": {"github": {
#       "command": "npx", "args": [...], "env": {...},   stdio
#       "url": "...", "headers": {...},                  http (E5)
#       "enabled": false,                                skip it entirely
#       "timeout": 60, "connect_timeout": 15,
#       "tools": {"include": [...], "exclude": [...]},
#       "schema":  {"tools": [ ...what the server answered... ]},
#       "classes": {"create_issue": "writing"}           what the USER decided
#   }}}
#
# TWO KEYS FOR TWO AUTHORS, and that split is the point. `schema` is what the
# server said and the specification calls it untrusted; `classes` is what a
# person confirmed. A server that reports `readOnlyHint: true` tomorrow changes
# nothing here -- the same construction as the pinned memory head.
MCP_FILE = os.path.join(os.path.dirname(SESSION_DIR), "mcp.json")

# The three the checklist offers: reads / writes / executes. They are names
# `TOOL_CLASS` already means something by, so an MCP tool hangs in the level
# system Crow has rather than beside it. `network` and `memory` are NOT
# offered -- both describe something about Crow itself, and neither is a
# statement anybody can make about a foreign process.
MCP_TOOL_CLASSES = ("reading", "writing", "executing")

# Same bill as MEMORY_COST_NOTE and said the same way round: before the change,
# never after. The second half is the one nobody expects -- a tool list that
# grew is a different byte 0 for conversations that were saved months ago.
MCP_COST_NOTE = ("the tool list changed -- the next turn pays a full prefill, "
                 "and so does the first turn of every saved session")

# The tag block. Every one of these renders as nothing at all and every one of
# them is fully visible to the model, which is the whole trick: a description
# fetched from a stranger's server is text that lands in the HEAD of the prompt.
# Crow's memory scan already refuses the neighbouring class (Unicode Cf) for the
# same reason -- but Cf does not cover this block, because most of it is
# unassigned, so a check built on `unicodedata.category` would let the middle of
# it through.
_TAG_FIRST, _TAG_LAST = 0xE0000, 0xE007F
# CODEPOINTS AND NOT LITERALS, and that is not style. `check_gui_prereqs`
# parses string literals and asks whether the shipped face can DRAW what is
# in them -- because a braille cell written as an escape still lands on
# somebody's screen. These two are never drawn: they are compared with `==`
# and nothing else. Written as literals they were 4 of that checker's 6 red
# lines, and a checker carrying false red is a checker that stops being read.
_TAG_BASE = chr(0x1F3F4)       # waving black flag: the one legitimate opener
_TAG_TERM = chr(0xE007F)       # cancel tag: what closes a real emoji sequence


def strip_tag_characters(text: str) -> str:
    """Drop invisible tag characters, and keep the one sequence that is not one.

    A REGIONAL FLAG IS A TAG SEQUENCE. U+1F3F4 followed by tag letters and
    U+E007F is how the Scottish, Welsh and English flags are written, so a plain
    range delete would mangle ordinary text -- and a filter that mangles ordinary
    text is a filter somebody switches off. The exception is therefore the
    SEQUENCE, not the block: tag characters that do not follow U+1F3F4 are not a
    flag and they go.
    """
    if not any(_TAG_FIRST <= ord(ch) <= _TAG_LAST for ch in text):
        return text
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == _TAG_BASE:
            j = i + 1
            while j < n and 0xE0020 <= ord(text[j]) <= 0xE007E:
                j += 1
            if j > i + 1 and j < n and text[j] == _TAG_TERM:
                out.append(text[i:j + 1])
                i = j + 1
                continue
        if not _TAG_FIRST <= ord(ch) <= _TAG_LAST:
            out.append(ch)
        i += 1
    return "".join(out)


def _mcp_clean(value):
    """Strip tag characters everywhere in a stored schema, not only the surface.

    A parameter's own `description` is prompt-head text exactly like the tool's,
    and it is one level deeper than a filter aimed at the obvious field looks.
    """
    if isinstance(value, str):
        return strip_tag_characters(value)
    if isinstance(value, list):
        return [_mcp_clean(v) for v in value]
    if isinstance(value, dict):
        return {(strip_tag_characters(k) if isinstance(k, str) else k): _mcp_clean(v)
                for k, v in value.items()}
    return value


# WHAT A FAILING SERVER ECHOES BACK, AND WHAT MAY NOT TRAVEL WITH IT.
#
# An MCP error is written by the server and lands in three places at once: the
# prompt, the chat on screen, and the session file on disk. Servers routinely
# quote the request that failed -- "invalid token: Bearer ghp_..." -- and that
# sentence is then permanent. This repository has already paid for that lesson
# once, on 2026-08-22, when a configuration block reached a chat log and the key
# in it had to be rotated.
#
# ERRORS ONLY, NEVER A SUCCESSFUL RESULT. A tool that legitimately returns
# documentation about API keys, or a file containing the word `password=`, is
# doing its job; mangling that would break real answers to protect nothing. The
# leak path is the failure path, and that is where this runs.
_MCP_SECRETS = (
    # Named tokens, by their own published prefixes.
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})"),
    re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"),
    # A JWT is three base64 segments and always starts with the same header.
    re.compile(r"\b(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,})"),
    # An HTTP credential, however the scheme is spelled.
    re.compile(r"(?i)\b((?:bearer|basic|dpop)\s+[A-Za-z0-9._~+/\-]{12,}=*)"),
    # `key=value` shapes. The NAME is what marks it, so the value may be anything.
    re.compile(r"(?i)\b((?:api[_\-]?key|access[_\-]?token|refresh[_\-]?token|"
               r"client[_\-]?secret|token|secret|password|passwd|credential)"
               r"\s*[=:]\s*\"?[A-Za-z0-9._~+/\-]{8,}\"?)"),
)


def _mcp_redact(text: str) -> str:
    """Credential-shaped runs replaced by `[REDACTED]`."""
    out = str(text)
    for pattern in _MCP_SECRETS:
        out = pattern.sub("[REDACTED]", out)
    return out


def _mcp_slug(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", strip_tag_characters(str(text)).lower()).strip("_")


def mcp_tool_name(server: str, tool: str) -> str:
    """`mcp_<server>_<tool>`, the scheme Hermes uses and users already know.

    The prefix is what keeps a foreign tool from ever colliding with one of the
    twelve, and the server part is what keeps two servers offering `search` from
    colliding with each other.
    """
    return "mcp_%s_%s" % (_mcp_slug(server), _mcp_slug(tool))


# `${VAR}` IN A BLOCK, RESOLVED WHEN THE SERVER IS USED AND NEVER STORED.
# A token written into `mcp.json` sits in a file two surfaces draw and a person
# edits; a token named `${GITHUB_TOKEN}` sits in the environment, and what the
# sheet shows is the placeholder. Crow already keeps `CROW_TAVILY_KEY` this way
# -- this is the same rule for a foreign server's credentials.
_MCP_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _mcp_expand(value) -> str:
    """`${VAR}` from the environment. An unresolved one is left ALONE here and
    caught in `start`, where it can be named -- silently sending the literal
    `${GITHUB_TOKEN}` as a bearer token is a 401 nobody can explain."""
    return _MCP_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), str(value))


def _mcp_missing(block: dict) -> list:
    """Which `${VAR}` in a block names nothing in the environment.

    ONLY THE SIX KEYS THAT REACH A PROCESS OR A REQUEST. `schema` is the
    server's own words and may legitimately contain a `${...}` in a description;
    treating that as a missing variable would make a server unusable over its
    own documentation.
    """
    found = []

    def walk(value):
        if isinstance(value, str):
            found.extend(n for n in _MCP_VAR.findall(value) if n not in os.environ)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    for key in ("command", "args", "cwd", "env", "url", "headers"):
        walk(block.get(key))
    return sorted(set(found))


def _mcp_pattern(text: str) -> bool:
    return any(ch in str(text) for ch in "*?[")


def _mcp_listed(name: str, patterns) -> bool:
    """Is this tool named by that list, exactly or by glob?

    AN ENTRY WITHOUT A METACHARACTER IS AN EXACT NAME, never a prefix: `docs`
    excludes the tool called `docs` and not `docs_search`. The globs are what
    make a flat surface manageable at all -- Cloudflare's API server reports
    around 3,300 tools at `?codemode=false`, and excluding product areas one
    endpoint at a time is not a thing anybody finishes.
    """
    import fnmatch

    for pattern in (patterns or ()):
        pattern = str(pattern)
        if _mcp_pattern(pattern):
            if fnmatch.fnmatchcase(name, pattern):
                return True
        elif name == pattern:
            return True
    return False


def mcp_doc(path: str | None = None) -> "tuple[dict, list[str]]":
    """The configuration, and why it is not usable when it is not.

    NO FILE IS THE NORMAL CASE and it is silent -- that is every installation
    until somebody adds a server. A file that cannot be read is NOT silent: it
    is hand-edited JSON, the likeliest state of it is half-written, and the
    failure it must not have is a client that will not start.
    """
    target = path or MCP_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError) as exc:
        return {}, ["mcp.json could not be read (%s: %s) -- no MCP server is "
                    "configured this run" % (exc.__class__.__name__, exc)]
    if not isinstance(doc, dict) or not isinstance(doc.get("servers"), dict):
        return {}, ["mcp.json has no 'servers' object -- no MCP server is "
                    "configured this run"]
    return doc, []


def mcp_catalog(doc: dict | None = None) -> "tuple[list[dict], list[str]]":
    """What the file declares: one entry per tool, plus everything wrong with it.

    DETERMINISTIC ORDER, SERVERS AND TOOLS BOTH SORTED, and that is a cache
    decision rather than tidiness. The order of this list reaches
    `json.dumps(TOOLS)` and therefore the fingerprint, so reading the file in the
    order somebody happened to type it would bill a cold start for moving two
    blocks around in an editor.
    """
    problems: list[str] = []
    if doc is None:
        doc, problems = mcp_doc()
    entries: list[dict] = []
    taken = {t["function"]["name"] for t in BUILTIN_TOOLS}
    for server in sorted(doc.get("servers") or {}):
        block = (doc.get("servers") or {})[server]
        if not isinstance(block, dict):
            problems.append("server %r is not an object and was skipped" % server)
            continue
        if block.get("enabled", True) is False:
            continue
        # A NAME THAT SURVIVES THE FILTER AS NOTHING IS NOT A NAME. `mcp_x_` is a
        # prefix, and a tool offered under one is a tool nobody can talk about.
        if not _mcp_slug(server):
            problems.append("server name %r is nothing the tool list can carry and "
                            "was skipped" % server)
            continue
        schema = block.get("schema") if isinstance(block.get("schema"), dict) else {}
        tools = schema.get("tools")
        if not isinstance(tools, list):
            problems.append("server %r has no stored schema -- nothing is declared "
                            "for it. Add it again to fetch one" % server)
            continue
        wanted = block.get("tools") if isinstance(block.get("tools"), dict) else {}
        include = wanted.get("include") if isinstance(wanted.get("include"), list) else None
        exclude = wanted.get("exclude") if isinstance(wanted.get("exclude"), list) else []
        classes = block.get("classes") if isinstance(block.get("classes"), dict) else {}

        for tool in sorted(tools, key=lambda t: str((t or {}).get("name", ""))):
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                problems.append("server %r stored a tool with no name" % server)
                continue
            raw = tool["name"]
            # INCLUDE IS THE POSITIVE LIST AND IT WINS. An exception list only
            # ever protects what somebody guessed in advance (measured
            # 2026-08-10), so the narrowing list is the one that names what may
            # pass -- and where the two disagree, the naming one decides.
            if include is not None:
                if not _mcp_listed(raw, include):
                    continue
            elif _mcp_listed(raw, exclude):
                continue

            if not _mcp_slug(raw):
                problems.append("server %r offers %r, which is nothing the tool list "
                                "can carry, and it was skipped" % (server, raw))
                continue
            name = mcp_tool_name(server, raw)
            if name in taken:
                problems.append("server %r offers %r, which collides with %s and "
                                "was skipped" % (server, raw, name))
                continue
            taken.add(name)

            klass = classes.get(raw)
            # UNCLASSIFIED IS NOT A PROBLEM, it is the normal state of a
            # freshly added server -- and the strict one. Reporting it would
            # print a red line per tool on every listing, and a report that is
            # always red is a report nobody reads. The listing shows the
            # unconfirmed class in brackets instead, which says the same thing
            # where somebody is already looking.
            if klass is None:
                pass
            elif klass not in MCP_TOOL_CLASSES:
                problems.append("%s is stored as %r, which is not one of %s -- it "
                                "asks at every level until that is corrected"
                                % (name, klass, ", ".join(MCP_TOOL_CLASSES)))
                klass = None

            # THE SCHEMA GOES THROUGH `_fn`, NOT STRAIGHT INTO THE LIST, so every
            # entry of `TOOLS` has the one shape both listings and the request
            # builder already read. What the server described is kept whole
            # inside it -- an `enum` or a nested description is information the
            # model needs -- but every string in it has been through the filter.
            params = tool.get("inputSchema")
            params = params if isinstance(params, dict) else {}
            required = params.get("required")
            entries.append({
                "name": name, "server": server, "tool": raw, "class": klass,
                "declaration": _fn(name,
                                   strip_tag_characters(tool.get("description") or ""),
                                   _mcp_clean(params.get("properties") or {}),
                                   _mcp_clean(required) if isinstance(required, list) else []),
            })
    return entries, problems


def mcp_apply(doc: dict | None = None) -> "list[str]":
    """Rebuild the three registries from the file. Returns what was wrong with it.

    IN PLACE, NEVER REBOUND. `crow.py` does `from crow_core import TOOLS`, which
    binds the VALUE -- the same trap `SESSION_FILE` carries a comment about. A
    fresh list here would leave every surface holding the old one, and both
    halves would work, on different state.

    IDEMPOTENT, because it is called again whenever the file changes: the
    built-ins are the floor and everything above them is dropped first, so
    applying twice adds nothing twice.
    """
    read: list[str] = []
    if doc is None:
        doc, read = mcp_doc()
    entries, problems = mcp_catalog(doc)
    problems = read + problems
    del TOOLS[len(BUILTIN_TOOLS):]
    for registry in (TOOL_IMPL, TOOL_CLASS):
        for name in [n for n in registry if n.startswith("mcp_")]:
            del registry[name]
    for entry in entries:
        TOOLS.append(entry["declaration"])
        # EVERY DECLARED NAME GETS AN IMPLEMENTATION. A name in `TOOLS` and not
        # in `TOOL_IMPL` is a tool the model calls and never reaches.
        TOOL_IMPL[entry["name"]] = _mcp_caller(entry["server"], entry["tool"])
        # AN UNCLASSIFIED TOOL GETS NO ENTRY AT ALL, which is not an omission:
        # `needs_approval` answers `executing` for a name it has not heard of, so
        # the absent entry IS the strict answer. Writing a guessed class here
        # would look like a decision somebody made.
        if entry["class"]:
            TOOL_CLASS[entry["name"]] = entry["class"]
    # E3: a process started from a block that no longer exists is a server
    # running yesterday's command. It goes with the configuration that made it.
    _mcp_retire(doc)
    return problems


def mcp_prompt_cost() -> int:
    """How many characters the configured servers add to the hashed head.

    MEASURED, NOT PREDICTED. What a server costs is per server -- Cloudflare's
    reports around 3,300 tools at `?codemode=false`, Crow's own twelve are about
    6,200 characters -- and it is not a property of Crow. But the schema is in
    hand once it is on disk, so this counts it instead of guessing.
    """
    return (len(json.dumps(TOOLS, sort_keys=True))
            - len(json.dumps(list(BUILTIN_TOOLS), sort_keys=True)))


# ---------------------------------------------------------------- E3 ------
# THE CONNECTION HAPPENS WHEN A TOOL IS CALLED, AND NEVER BEFORE.
#
# E2 is why: `TOOLS` comes off the disk so that a server which is slow, broken or
# uninstalled cannot move byte 0. This half keeps the other end of that promise
# -- such a server has to cost a CALL and nothing else. So no process is started
# at import, none at the start of a turn, and none for a server nobody uses.
#
# ONE PROCESS PER SERVER, KEPT. `npx` takes seconds to come up; paying that per
# call would make round two of a turn slower than the model. The process is
# started on the first call, reused, and ended when the configuration changes
# under it or the client goes away.
#
# THE FRAMING IS ONE JSON OBJECT PER LINE, both ways, with no embedded newline --
# that is the whole of the stdio transport. Anything the server prints to stdout
# that is not a message would corrupt the stream, so a line that does not parse
# is dropped rather than guessed at; stderr is a separate pipe and is DRAINED,
# because a full stderr pipe blocks the server rather than losing its output.
#
# WHAT THE SERVER MAY ASK OF CROW: nothing. `capabilities` is sent empty, so no
# `sampling` and no `elicitation` -- on one slot a foreign process asking for
# inference takes the hardware from the person at the keyboard, decided
# 2026-08-22. The specification makes that safe to do BY NAME rather than by
# silence: a capability the client never declared may not be relied on, so a
# server asking anyway gets a JSON-RPC error naming what is missing. Silence
# would hang it, and a hung server looks exactly like a slow one.
MCP_PROTOCOL_VERSION = "2025-06-18"

# Seconds, overridable per server with `connect_timeout` and `timeout`. Both
# exist for the same reason `COMMAND_TIMEOUT` does: a turn runs at ~10 tok/s and
# a call that never returns would hold it until the socket timeout, 30 minutes.
MCP_CONNECT_TIMEOUT = 20.0
MCP_CALL_TIMEOUT = 60.0

# How many `tools/list` pages are followed before giving up on a server that
# keeps handing out cursors. Only the add path pages; nothing at start does.
MCP_LIST_PAGES = 20

# How much of a failed server's own words are kept. Its stderr is usually the
# only thing that ever explains `npx: not found`, and throwing it away is what
# makes this class of failure unreadable.
MCP_STDERR_LINES = 20

# BOTH TYPES, ON EVERY POST, and the specification writes it as a MUST rather
# than a preference. A server picks its answer shape off this header: context7
# answers `tools/list` as an SSE STREAM, measured 2026-08-22, so a client that
# asks for JSON alone is a client context7 has nothing to say to. THE STREAM IS
# THE NORMAL CASE, not the exception.
MCP_ACCEPT = "application/json, text/event-stream"

# How much of an HTTP failure body is kept. A 401 explains itself in its first
# line and in nobody's second page, and the whole of an error page would push
# every other line out of the deque that has to carry it.
MCP_HTTP_SAID = 400

# WHERE TOKENS LIVE, AND WHY NOT IN `mcp.json`. That file is read by
# `mcp_view`, drawn by two surfaces, pasted into bug reports and edited by hand;
# a refresh token in it is a credential with a rotation nobody performs. This
# one is written by Crow alone, never read into a view, and dropped with the
# server it belongs to.
MCP_TOKEN_FILE = os.path.join(os.path.dirname(SESSION_DIR), "mcp_tokens.json")

# Seconds for the discovery and token calls. They are not a turn -- nothing is
# streaming and nobody is waiting on tokens per second -- so this is short.
MCP_OAUTH_TIMEOUT = 20.0

# How long the browser leg may take. It is a person reading a consent screen,
# which is why it is minutes and not seconds, and why it runs at ADD time and
# never inside a tool call.
MCP_OAUTH_WAIT = 300.0

# Refresh this many seconds before the token would expire. A token that is
# valid when the request is built and stale when it arrives is the failure this
# margin exists for.
MCP_TOKEN_SKEW = 60.0

# Seconds allowed for the `DELETE` that ends a session. It is a courtesy to the
# server, it happens while a window is closing, and a client that waited on it
# would hang on the way out for a message nobody reads.
MCP_DELETE_TIMEOUT = 3.0

# A POSITIVE LIST, WHICH `run_command` IS NOT, and the difference is who runs.
# There the child is a command the user just typed, and a blocklist that "stops
# the accident, not an attacker" is the honest trade. Here it is a foreign
# server the user configured once and forgot, so the question turns around: not
# "what must not travel" -- nobody can guess that -- but "what does a process
# need in order to start at all". Everything else is added per server in `env`.
#
# ON WINDOWS THE SHORT LIST IS ALREADY WRONG. Without SYSTEMROOT neither Python
# nor Node comes up; without PATHEXT a bare `npx` is not found; without TEMP the
# npm cache has nowhere to go. An empty environment is not safety, it is a
# server that never answers.
_MCP_ENV_KEEP = frozenset((
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "HOME", "HOMEDRIVE", "HOMEPATH", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL",
))


def _mcp_seconds(value, default: float) -> float:
    """A timeout from the file, or the default where it is absent or nonsense.

    Zero and negative are nonsense rather than "no limit": a configuration that
    says 0 is a typo, and reading it as "wait forever" turns a typo into a
    client that hangs.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return seconds if seconds > 0 else default


class McpServer:
    """One server, and the single conversation running over it.

    TWO TRANSPORTS, ONE CLASS, and the seam is `_send`. A child process on a
    pipe and an endpoint behind a POST have nothing in common below that line
    and nothing DIFFERENT above it: `request`, `_hear` and `_handshake` are the
    same code either way, because what they work on is a queue of JSON-RPC
    messages and neither of them ever asks where the queue is filled from.
    """

    def __init__(self, name: str, block: dict) -> None:
        self.name = name
        self.block = block
        self.proc = None
        self.endpoint: str | None = None
        self.session: str | None = None
        self.protocol: str | None = None
        self.info: dict = {}
        self.asked: dict = {}
        self._id = 0
        self._lines = None
        self._stderr = None
        # WHAT CAME OUT OF stdout AND WAS NOT A MESSAGE. Kept, because it is
        # usually the only thing that explains the failure -- and it was being
        # dropped on the floor until 2026-08-22, when `npx ctx7 setup` timed out
        # after printing an interactive menu that nobody ever saw.
        self._noise = None
        self._reinitialising = False
        self._reauthorising = False
        # ONE CALLER AT A TIME ON ONE PIPE. The background review runs on its own
        # thread and can call a tool; two writers interleaving on one stdin would
        # hand each other's answers back.
        #
        # RE-ENTRANT, AND ONLY HTTP NEEDS THAT. A 404 on a session obliges the
        # client to initialise again, and that second handshake goes out through
        # `request` -- from inside the `request` that is still holding this. A
        # plain Lock would deadlock the thread against itself; other threads are
        # held out exactly as before.
        self._lock = threading.RLock()

    # -- what the child is given -------------------------------------------

    def environment(self) -> dict:
        kept = {k: v for k, v in os.environ.items() if k.upper() in _MCP_ENV_KEEP}
        configured = self.block.get("env")
        if isinstance(configured, dict):
            kept.update({str(k): _mcp_expand(v) for k, v in configured.items()})
        return kept

    def argv(self) -> "list[str] | None":
        command = self.block.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        # RESOLVED HERE, NOT LEFT TO Popen. On Windows `npx` is `npx.CMD`, and
        # CreateProcess does not consult PATHEXT -- `Popen(["npx", ...])` raises
        # WinError 2 while `where npx` finds it on the first try. Measured
        # 2026-08-22 against @modelcontextprotocol/server-filesystem.
        #
        # THIS IS NOT COSMETIC. Nearly every MCP example in the world starts with
        # `npx`, so without this line not one documented server can be started on
        # the platform this client ships for.
        #
        # `shell=True` WOULD ALSO "FIX" IT and is the wrong fix: it would hand a
        # configured string to cmd.exe, where a space or an ampersand in a path
        # stops being an argument and becomes syntax.
        command = _mcp_expand(command)
        found = shutil.which(command)
        args = self.block.get("args")
        return ([found or command]
                + [_mcp_expand(a) for a in (args if isinstance(args, list) else [])])

    def url(self) -> "str | None":
        """The configured endpoint, or None where this server is a command.

        THE FIELD, NOT THE SCHEME. Whether `ftp://` is a transport this client
        opens is a question with its own answer and its own sentence, and
        deciding it here would report a typed-in scheme as "no url configured".
        """
        url = self.block.get("url")
        url = _mcp_expand(url).strip() if isinstance(url, str) else ""
        return url or None

    def _call_seconds(self) -> float:
        return _mcp_seconds(self.block.get("timeout"), MCP_CALL_TIMEOUT)

    def _connect_seconds(self) -> float:
        return _mcp_seconds(self.block.get("connect_timeout"), MCP_CONNECT_TIMEOUT)

    # -- the process --------------------------------------------------------

    def start(self) -> "str | None":
        import collections
        import queue

        # BEFORE ANYTHING IS STARTED OR SENT. A `${VAR}` that names nothing
        # would otherwise travel as its own literal text -- into a command line,
        # or into an `Authorization` header, where it comes back as a 401 with
        # no hint of what went wrong.
        missing = _mcp_missing(self.block)
        if missing:
            return ("the MCP server %r wants %s from the environment, and "
                    "nothing is set" % (self.name, ", ".join(missing)))
        endpoint = self.url()
        if endpoint is not None:
            # ONE BLOCK IS ONE TRANSPORT. Letting one of them quietly win would
            # leave somebody watching a command that never starts, with a file
            # in front of them that plainly says it should.
            if self.block.get("command"):
                return ("the MCP server %r has both 'url' and 'command'. One "
                        "block is one transport -- take one of them out"
                        % self.name)
            return self._open(endpoint)
        argv = self.argv()
        if argv is None:
            return "the MCP server %r has neither 'command' nor 'url'" % self.name
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.environment(),
                cwd=self.block.get("cwd") if isinstance(self.block.get("cwd"), str) else None,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            self.proc = None
            return "the MCP server %r could not be started: %s" % (self.name, exc)
        self._lines = queue.Queue()
        self._stderr = collections.deque(maxlen=MCP_STDERR_LINES)
        self._noise = collections.deque(maxlen=MCP_STDERR_LINES)
        threading.Thread(target=self._pump, daemon=True,
                         name="mcp-%s-out" % self.name).start()
        threading.Thread(target=self._sip, daemon=True,
                         name="mcp-%s-err" % self.name).start()
        return self._handshake()

    def _pump(self) -> None:
        """stdout into a queue, and a `None` at the end of it.

        A THREAD RATHER THAN A NON-BLOCKING READ, because there is no portable
        non-blocking read of a pipe -- and the timeout has to hold on Windows,
        where this client lives.
        """
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        finally:
            self._lines.put(None)

    def _sip(self) -> None:
        """stderr, drained and kept. DRAINED IS THE LOAD-BEARING HALF: a stderr
        pipe nobody reads fills up and blocks the server mid-answer."""
        try:
            for line in self.proc.stderr:
                self._stderr.append(line.rstrip("\r\n"))
        except Exception:
            pass

    # -- the other transport ------------------------------------------------
    #
    # STREAMABLE HTTP, the transport that replaced the old HTTP+SSE pair. One
    # endpoint that takes POST, every message its own request, and an answer
    # that is either a JSON object or a stream of them. Read as raw text from
    # the 2025-06-18 specification and driven against context7 on 2026-08-22,
    # which is where the two assumptions that look obvious both turned out
    # wrong: the answer to `tools/list` is a STREAM, and no session id at all is
    # a valid state rather than a fault.
    #
    # WHAT IS NOT BUILT HERE, and both are MAY in the specification: the
    # standing `GET` stream, because a server's unsolicited notifications are
    # the one class of message this client acts on none of; and `Last-Event-ID`
    # resumption, because there is no stream held open to lose.

    def _open(self, endpoint: str) -> "str | None":
        """The HTTP half of `start`: no child, and the same queue behind it.

        THE QUEUE IS WHAT KEEPS `_hear` TRANSPORT-FREE. Over stdio a thread
        pumps lines off a pipe into it; here a thread pumps events off a
        response body into it. Neither knows what a JSON-RPC id is, and nothing
        above them ever learns which one it is talking through.
        """
        import collections
        import queue

        scheme = urllib.parse.urlparse(endpoint).scheme.lower()
        if scheme not in ("http", "https"):
            return ("the MCP server %r has a 'url' this client will not open: "
                    "%s. http and https only"
                    % (self.name, scheme or "no scheme in it"))
        self.endpoint = endpoint
        self._lines = queue.Queue()
        # THE DEQUE THE stdio ARM FILLS FROM stderr, filled here from failure
        # bodies -- and for the same reason. A 401 explains itself in its body
        # and nowhere else, and `_gone` is already the one place that reads this
        # back out into a sentence somebody sees.
        self._stderr = collections.deque(maxlen=MCP_STDERR_LINES)
        return self._handshake()

    def _said(self, line: str) -> None:
        # THE BODY OF A FAILED REQUEST, and the likeliest place a credential
        # comes back: a gateway that answers 401 with the header it did not
        # like. This deque is read by `_gone` straight into a tool result.
        if self._stderr is not None:
            self._stderr.append(
                _mcp_redact(strip_tag_characters(str(line)))[:MCP_HTTP_SAID])

    def _headers(self) -> dict:
        """What rides on every request, and the order the two sources merge in.

        THREE LAYERS, AND WHICH ONE WINS SAYS WHAT EACH IS FOR. Identity is a
        default, because a server may insist on its own; the block is a token
        and whatever else that server wants; the transport's four are not
        preferences at all -- `Accept`, `Content-Type`, the session and the
        version ARE the transport, and a server handed the wrong ones answers
        nothing at all.

        IDENTIFYING, NOT DISGUISED -- the sentence `web_fetch` already carries.
        urllib signs itself `Python-urllib`, which several networks refuse on
        sight; a client that answered that by dressing as a browser would be
        taking an answer it was not offered. Crow says who it is and lets the
        far end decide.
        """
        sending = {"User-Agent": "Crow/%s (+%s)" % (CLIENT_VERSION or "dev", REPO_URL)}
        # LAYER 1b: a token this client obtained itself, UNDER the block rather
        # than over it. Somebody who typed an `Authorization` into `mcp.json`
        # meant it, and a server that accepts it never issues the 401 that would
        # have started an OAuth flow in the first place.
        token = mcp_token_for(self.name)
        if token.get("access_token"):
            # `Bearer`, WHATEVER THE TOKEN ENDPOINT SPELLED IT. RFC 6750 makes
            # the scheme case-insensitive and RFC 7235 says so for every scheme,
            # but a resource server is free to compare the string it was handed.
            #
            # MEASURED 2026-08-22 against higgsfield, with one token and two
            # requests: its token endpoint answers `"token_type": "bearer"` and
            # its MCP endpoint answers `bearer <token>` with 401 and
            # `Bearer <token>` with 200. Echoing the endpoint's own spelling back
            # at it is what made a completed browser leg look like a refused one.
            #
            # ANYTHING THAT IS NOT bearer GOES THROUGH UNCHANGED -- `DPoP` is a
            # different scheme with different rules, not a different spelling.
            kind = str(token.get("token_type") or "Bearer")
            sending["Authorization"] = "%s %s" % (
                "Bearer" if kind.lower() == "bearer" else kind,
                token["access_token"])
        configured = self.block.get("headers")
        if isinstance(configured, dict):
            sending.update({str(k): _mcp_expand(v) for k, v in configured.items()})
        sending["Content-Type"] = "application/json"
        sending["Accept"] = MCP_ACCEPT
        if self.session:
            sending["Mcp-Session-Id"] = self.session
        # AFTER THE HANDSHAKE, NEVER BEFORE IT. The header names the version the
        # two sides AGREED on, and there is no agreement until `initialize` has
        # come back. Sending one first is a client announcing its own decision
        # as a negotiated one.
        if self.protocol:
            sending["MCP-Protocol-Version"] = self.protocol
        return sending

    def _post(self, message: dict, timeout: float) -> "str | None":
        # BEFORE THE HEADERS ARE BUILT, because a token that is valid when the
        # request is assembled and stale when it lands is exactly the failure
        # `MCP_TOKEN_SKEW` exists for. Costs a clock read when there is nothing
        # to do, which is every call but one.
        due = mcp_token_for(self.name).get("expires_at")
        if due is not None and float(due) - time.time() < MCP_TOKEN_SKEW:
            problem = mcp_refresh_token(self.name)
            if problem:
                return "the MCP server %r needs authorising again: %s" % (self.name, problem)
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(message).encode("utf-8"),
            headers=self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return self._refused(exc, message, timeout)
        except (OSError, ValueError) as exc:
            self._said("%s: %s" % (exc.__class__.__name__, exc))
            return self._gone("could not be reached")
        # THE SESSION IS ASSIGNED ONCE, AT INITIALISATION, and taken only while
        # there is none. A server that puts a different id on a later answer is
        # not renaming the session, it is a proxy answering for somebody else.
        given = resp.headers.get("Mcp-Session-Id")
        if given and not self.session:
            self.session = str(given).strip()
        return self._take(resp)

    def _refused(self, exc, message: dict, timeout: float) -> "str | None":
        """A status that is not 2xx -- and the one of them that is not a failure.

        404 WITH A SESSION IN HAND IS AN EXPIRY, NOT AN ERROR. The specification
        is flat about it: a client that gets 404 for a request carrying a session
        id MUST start a new session with a fresh `initialize`. Handing that back
        as a failed tool call would make every server that recycles sessions look
        broken once an hour, and from the keyboard it would look like the tool
        itself was unreliable.
        """
        try:
            said = exc.read().decode("utf-8", "replace")
        except Exception:
            said = ""
        self._said("HTTP %s %s" % (exc.code, " ".join(said.split())))
        # 401 IS A CREDENTIAL QUESTION, AND A CALL MAY ANSWER ONLY HALF OF IT.
        # A stored refresh token costs a round trip and no human, so it is spent
        # here; a browser leg is not, at any price. The challenge is put where
        # the ADD path can find it, and the sentence names the command that
        # opens the page while somebody is actually sitting there.
        if exc.code == 401 and not self._reauthorising:
            challenge = mcp_www_auth(exc.headers.get("WWW-Authenticate") or "")
            _mcp_saw_401(self.name, challenge)
            if mcp_token_for(self.name).get("refresh_token"):
                self._reauthorising = True
                try:
                    problem = mcp_refresh_token(self.name)
                    if problem:
                        return "%s The refresh failed: %s" % (
                            self._gone("answered HTTP 401"), problem)
                    return self._post(message, timeout)
                finally:
                    self._reauthorising = False
            # THE SERVER'S OWN WORDS SURVIVE THE ADVICE. A sentence that only
            # said "run /mcp auth" would drop the one line separating a wrong
            # token from a missing one, and the body is where that line is.
            return "%s Run: /mcp auth %s" % (self._gone("answered HTTP 401"),
                                             self.name)
        if exc.code == 404 and self.session and not self._reinitialising:
            self.session = None
            self.protocol = None
            # THE FLAG COVERS THE RETRY, NOT ONLY THE HANDSHAKE, and that is the
            # difference between one extra round-trip and a client spinning. A
            # server that answers 404 to everything would otherwise hand the
            # retry its own 404, and the retry would start the whole thing again.
            # Exactly one new session per call: a server that refuses twice is
            # refusing.
            self._reinitialising = True
            try:
                problem = self._handshake()
                if problem:
                    return problem
                return self._post(message, timeout)
            finally:
                self._reinitialising = False
        return self._gone("answered HTTP %s" % exc.code)

    def _take(self, resp) -> "str | None":
        """What came back on a POST: nothing, one object, or a stream of them.

        202 WITH AN EMPTY BODY IS THE NORMAL ANSWER to a notification and to a
        response Crow sends -- there is nothing to wait for and nothing to
        enqueue. Measured against context7 on 2026-08-22:
        `notifications/initialized` comes back 202 with a body of zero bytes.

        THE JSON ARM RE-SERIALISES, and that is what the seam costs. `_hear`
        reads LINES, so a parsed object goes back to a line before it is put in
        the queue -- one `json.dumps` per message, against a `_hear` that would
        otherwise have to know two shapes and stop being transport-free.
        """
        kind = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind == "text/event-stream":
            threading.Thread(target=self._sip_stream, args=(resp,), daemon=True,
                             name="mcp-%s-sse" % self.name).start()
            return None
        try:
            with resp:
                raw = resp.read()
        except (OSError, ValueError) as exc:
            self._said("%s: %s" % (exc.__class__.__name__, exc))
            return self._gone("stopped mid-answer")
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self._said(" ".join(raw.decode("utf-8", "replace").split()))
            return self._gone("answered with something that is not JSON")
        for one in (payload if isinstance(payload, list) else [payload]):
            self._lines.put(json.dumps(one))
        return None

    def _sip_stream(self, resp) -> None:
        """An SSE body, event by event, into the queue stdout goes into.

        A THREAD RATHER THAN A READ IN LINE, and the reason is a server that ASKS
        something mid-answer. Over HTTP its request arrives on this stream while
        Crow's refusal has to leave as a SEPARATE POST -- a client reading the
        stream in the calling thread could not send that refusal until the stream
        ended, and the stream does not end until the server has it. That is a
        deadlock with a timeout on it.

        IT STOPS AT THE RESPONSE. The specification has the server close the
        stream once it has answered; not every server does, and a reader waiting
        for an EOF that never comes is one thread per call that never ends.
        """
        data, answered = [], False
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    continue                # a comment, and a keep-alive is one
                if line:
                    field, _, value = line.partition(":")
                    if field == "data":
                        data.append(value[1:] if value.startswith(" ") else value)
                    continue                # `event:` and `id:` decide nothing here
                if not data:
                    continue
                body, data = "\n".join(data), []
                self._lines.put(body)
                try:
                    message = json.loads(body)
                except ValueError:
                    continue
                if isinstance(message, dict) and (
                        "result" in message or "error" in message):
                    answered = True
                    return
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass
            # A STREAM THAT ENDED WITHOUT AN ANSWER ENDS THE WAIT, and saying so
            # is what stdio's `None` says. It goes in ONLY in that case: after an
            # answer the queue belongs to whoever calls next, and a sentinel left
            # lying in it would fail their call instead of this one.
            if not answered:
                self._lines.put(None)

    def _end_session(self) -> None:
        """`HTTP DELETE` with the session id, and every way it can fail ignored.

        A SERVER MAY REFUSE TO BE TOLD -- the specification lets it answer 405 --
        and a client that treated that as a failure would report an error while
        closing, every time, against a server doing nothing wrong.
        """
        endpoint, session = self.endpoint, self.session
        self.session = None
        if not endpoint or not session:
            return
        try:
            request = urllib.request.Request(
                endpoint, method="DELETE",
                headers={"Mcp-Session-Id": session,
                         "MCP-Protocol-Version": self.protocol
                         or MCP_PROTOCOL_VERSION})
            with urllib.request.urlopen(request, timeout=MCP_DELETE_TIMEOUT) as resp:
                resp.read()
        except Exception:
            pass

    def _heard_noise(self, line: str) -> None:
        if self._noise is not None:
            self._noise.append(
                _mcp_redact(strip_tag_characters(str(line)))[:MCP_HTTP_SAID])

    def _gone(self, why: str) -> str:
        said = "\n".join(line for line in (self._stderr or ()) if line.strip())
        # TWO CHANNELS, NAMED APART. A server writes its errors to stderr and a
        # program that is not a server at all writes its interface to stdout;
        # merging them would tell somebody their MCP server had an error when
        # what actually happened is that it was never an MCP server.
        printed = "\n".join(line for line in (self._noise or ()) if line.strip())
        code = self.proc.poll() if self.proc is not None else None
        return ("the MCP server %r %s.%s%s%s"
                % (self.name, why,
                   " It exited with %s." % code if code is not None else "",
                   " It said:\n%s" % said if said else "",
                   " It printed, which is not a protocol message:\n%s" % printed
                   if printed else ""))

    def close(self) -> None:
        # THE SESSION GOES FIRST, AND IT GOES OVER THE WIRE. There is no process
        # to end on that transport, so an id nobody released is a session the
        # server keeps holding open for a client that will never come back.
        self._end_session()
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()          # EOF on stdin is how a server is asked to go
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            # A SERVER ASLEEP IN ITS OWN HANDSHAKE NEVER READS THE EOF, so the
            # polite ask above expires and it is killed -- and then REAPED, because
            # a killed child that nobody waits for is a zombie on POSIX and an
            # object warning about itself at collection time here.
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        # AND THE READ ENDS TOO. The two pump threads hold the other pipes open;
        # closing stdin alone leaves a file object per server behind, which a
        # long-running window would accumulate one at a time.
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass

    # -- the wire -----------------------------------------------------------

    def _send(self, message: dict, timeout: "float | None" = None) -> "str | None":
        """One message out. THE ONE METHOD THAT KNOWS WHICH TRANSPORT THIS IS.

        Everything above it -- `request`, `_hear`, `_handshake` -- is the same
        code for a child on a pipe and for an endpoint on the other side of the
        world. Everything below it has nothing in common. That is where the seam
        belongs, and it is why the second transport did not need a second class.
        """
        if self.endpoint is not None:
            return self._post(message, timeout or self._call_seconds())
        if self.proc is None or self.proc.stdin is None:
            return self._gone("is not running")
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return self._gone("could not be written to")
        return None

    def _elicits(self) -> bool:
        """Whether this server may ask. On unless the block says otherwise.

        ON BY DEFAULT, and that is not the same call `sampling` got. Sampling
        spends the one slot on a request nobody made; this spends a person's
        attention, and only while that person is sitting there answering a turn
        they started. The gate asks every single time -- there is no standing
        yes, because a form is different every time it appears.
        """
        return self.block.get("elicitation", True) is not False

    def _elicit(self, message: dict) -> None:
        """One `elicitation/create`, from the wire to a person and back.

        BLOCKS THE CALL, deliberately: the tool call is waiting, the server is
        waiting, and the person is the only one who can move it. That is the
        opposite of the OAuth rule, and the difference is who asked for what --
        nobody asked for a browser mid-turn, and this server was asked to do
        something by the model in this very turn.
        """
        params = message.get("params") or {}
        wanted = strip_tag_characters(str(params.get("message") or ""))[:400]
        fields, problem = elicit_fields(params.get("requestedSchema"))
        if problem:
            # A SCHEMA THIS CLIENT CANNOT DRAW IS DECLINED, not ignored and not
            # guessed at. `decline` is the specification's own word for a
            # refusal, so the server learns it was answered rather than dropped.
            self._send({"jsonrpc": "2.0", "id": message.get("id"),
                        "result": {"action": "decline",
                                   "_meta": {"crow/reason": problem}}})
            return
        entry = stage_elicitation(self.name, wanted or "This server is asking.",
                                  fields)
        announce = ELICIT_ANNOUNCE
        if announce is not None:
            try:
                announce(elicit_view())
            except Exception:               # noqa: BLE001 - a surface may be gone
                pass
        if not entry["answered"].wait(ELICIT_TTL):
            # NOT `decline`. The specification separates the two, and a person
            # who never saw the question has not said no to it.
            entry["action"], entry["content"] = "cancel", {}
            with _ASKS_LOCK:
                if entry in _ASKS:
                    _ASKS.remove(entry)
        result = {"action": entry["action"] or "cancel"}
        if result["action"] == "accept":
            result["content"] = entry["content"] or {}
        self._send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})

    def _refuse(self, message: dict) -> None:
        """Answer a request the server made of Crow. BY NAME, NEVER BY SILENCE.

        The specification says a server may not rely on a capability the client
        did not declare and must be told so. A client that simply ignores the
        request leaves the server waiting, and a waiting server is
        indistinguishable from a slow one.
        """
        method = str(message.get("method") or "")
        # OVER HTTP THIS LEAVES AS ITS OWN POST, while the stream that carried
        # the question is still open. That arrangement is what the reader thread
        # exists for.
        self._send({"jsonrpc": "2.0", "id": message.get("id"),
                    "error": {"code": -32601,
                              "message": "Crow declares no %r capability, so %s "
                                         "cannot be served"
                                         % (method.split("/")[0] or method, method)}})

    def _hear(self, wanted, timeout: float) -> "tuple[dict | None, str | None]":
        import queue as _queue

        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None, self._gone("did not answer within %gs" % timeout)
            try:
                line = self._lines.get(timeout=left)
            except _queue.Empty:
                return None, self._gone("did not answer within %gs" % timeout)
            if line is None:
                return None, self._gone("closed the connection")
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                # NOT A MESSAGE, AND NOT NOTHING. A command that is not an MCP
                # server at all -- an installer, a wizard, a CLI printing its
                # usage -- says so here and nowhere else, and dropping it was
                # what made `npx ctx7 setup` fail as a bare timeout.
                self._heard_noise(line)
                continue
            if not isinstance(message, dict):
                self._heard_noise(line)
                continue
            if message.get("id") == wanted and ("result" in message or "error" in message):
                return message, None
            if message.get("method") and message.get("id") is not None:
                if (message.get("method") == "elicitation/create"
                        and self._elicits()):
                    self._elicit(message)
                else:
                    self._refuse(message)
            # everything else is a notification, and this client acts on none of
            # them -- `notifications/tools/list_changed` least of all, because a
            # tool list that changes mid-chat moves byte 0.

    def request(self, method: str, params: dict, timeout: float
                ) -> "tuple[dict | None, str | None]":
        with self._lock:
            self._id += 1
            wanted = self._id
            problem = self._send({"jsonrpc": "2.0", "id": wanted,
                                  "method": method, "params": params}, timeout)
            if problem:
                return None, problem
            return self._hear(wanted, timeout)

    # -- the handshake ------------------------------------------------------

    def _handshake(self) -> "str | None":
        """`initialize`, then `notifications/initialized`, and one retry.

        THE RETRY READS WHAT THE REFUSAL OFFERS. A version error carries the
        list of versions that WOULD have worked, so acting on it is the
        difference between one extra round-trip and a server nobody can use.
        Exactly one retry: a server that refuses twice is refusing.
        """
        timeout = self._connect_seconds()
        wanted = MCP_PROTOCOL_VERSION
        for attempt in (1, 2):
            self.asked = {"protocolVersion": wanted,
                          # DECLARED ONLY WHERE IT IS ALLOWED. A capability
                          # announced and then refused is a server left waiting
                          # on a promise; the specification's whole point is
                          # that this list is the truth.
                          "capabilities": ({"elicitation": {}}
                                           if self._elicits() else {}),
                          "clientInfo": {"name": "Crow",
                                         "version": CLIENT_VERSION or "0"}}
            answer, problem = self.request("initialize", self.asked, timeout)
            if problem:
                return problem
            failure = answer.get("error")
            if not failure:
                result = answer.get("result") or {}
                self.protocol = result.get("protocolVersion") or wanted
                self.info = result.get("serverInfo") or {}
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized",
                            "params": {}}, timeout)
                return None
            data = failure.get("data")
            offered = data.get("supported") if isinstance(data, dict) else None
            if attempt == 1 and isinstance(offered, list) and offered:
                wanted = max(str(v) for v in offered)
                continue
            return ("the MCP server %r refused the handshake: %s"
                    % (self.name, strip_tag_characters(str(failure.get("message")
                                                          or failure))))
        return None


# WHAT A 401 LEFT BEHIND, by server name. Written where the challenge is seen
# and POPPED where it is read, so a stale one from an earlier attempt can never
# start a browser leg nobody asked for.
#
# WHY IT IS NOT RETURNED INSTEAD: the 401 is seen four layers below the add
# path -- `_post` inside `_send` inside `request` inside `_handshake` -- and
# every one of them answers with a problem STRING. Threading a second value up
# through all four to serve one caller would change four signatures and the
# suite that pins them.
_MCP_CHALLENGE: "dict[str, dict]" = {}


def _mcp_saw_401(name: str, challenge: dict) -> None:
    with _MCP_LIVE_LOCK:
        _MCP_CHALLENGE[name] = challenge


def _mcp_take_401(name: str) -> "dict | None":
    with _MCP_LIVE_LOCK:
        return _MCP_CHALLENGE.pop(name, None)


# Live processes, by server name. NOT a cache -- a cache may be dropped and
# rebuilt at will, and each entry here is a child process that has to be ended.
_MCP_LIVE: "dict[str, McpServer]" = {}
_MCP_LIVE_LOCK = threading.Lock()


def mcp_server(name: str, block: dict | None = None
               ) -> "tuple[McpServer | None, str | None]":
    """The running server of that name, started now if this is the first call.

    PASSING `block` IS THE AD-HOC PATH and it is filed under NOTHING. That is
    the add flow: a server not in the file yet, or one already in it whose
    schema is being fetched again. Registering it would replace the process the
    open chat is talking to and orphan the old one -- alive, unreferenced, and
    invisible to `forget_mcp_servers`. The caller closes what it asked for.
    """
    if block is not None:
        server = McpServer(name, block)
        problem = server.start()
        if problem:
            server.close()
            return None, problem
        return server, None
    with _MCP_LIVE_LOCK:
        live = _MCP_LIVE.get(name)
        if live is not None:
            return live, None
        doc, problems = mcp_doc()
        configured = (doc.get("servers") or {}).get(name)
        if not isinstance(configured, dict):
            return None, (problems[0] if problems else
                          "no MCP server named %r is configured" % name)
        server = McpServer(name, configured)
    problem = server.start()
    if problem:
        server.close()
        return None, problem
    # TWO CALLERS CAN MISS THE SAME EMPTY SLOT -- the background review runs on
    # its own thread. Whoever lands second closes its own child rather than
    # writing over the first, which would leak exactly the process nobody holds.
    with _MCP_LIVE_LOCK:
        winner = _MCP_LIVE.setdefault(name, server)
    if winner is not server:
        server.close()
    return winner, None


def forget_mcp_servers() -> None:
    """End every running server. Called at exit and when the file changes.

    A window open for a day would otherwise leave one `npx` per server behind
    it, with nothing in the client knowing they exist.
    """
    with _MCP_LIVE_LOCK:
        running = list(_MCP_LIVE.values())
        _MCP_LIVE.clear()
    for server in running:
        server.close()


def _mcp_launch_key(block) -> tuple:
    """What about a block decides the PROCESS, and nothing else.

    Which tools are exposed and how they are classed are facts about the PROMPT.
    Restarting the child for them would drop a live connection mid-turn over a
    change the server never sees -- and the checklist writes one of those on
    every single tick.
    """
    if not isinstance(block, dict):
        return ()
    env = block.get("env")
    headers = block.get("headers")
    return (block.get("command"),
            tuple(str(a) for a in (block.get("args") or [])),
            tuple(sorted((str(k), str(v)) for k, v in env.items()))
            if isinstance(env, dict) else (),
            block.get("cwd"),
            # THE ENDPOINT AND THE TOKEN DECIDE THE CONNECTION exactly as the
            # command and its environment decide the process. A url edited under
            # a live connection would otherwise leave calls going to yesterday's
            # server, and a rotated key would go on failing against the old one.
            block.get("url"),
            tuple(sorted((str(k), str(v)) for k, v in headers.items()))
            if isinstance(headers, dict) else (),
            block.get("enabled", True) is not False)


def _mcp_retire(doc: dict) -> None:
    """End servers whose configuration no longer matches the one that started them.

    A LIVE PROCESS BELONGS TO THE BLOCK THAT STARTED IT. Keeping one across a
    rewrite would leave yesterday's command running while the file says
    something else -- and nothing on screen would disagree.
    """
    servers = doc.get("servers") or {}
    with _MCP_LIVE_LOCK:
        stale = [name for name, live in _MCP_LIVE.items()
                 if _mcp_launch_key(servers.get(name)) != _mcp_launch_key(live.block)]
        retired = [_MCP_LIVE.pop(name) for name in stale]
    for server in retired:
        server.close()


# ---------------------------------------------------------------- E5b -----
# MCP AUTHORIZATION: OAuth 2.1, and the whole of it hangs on one decision.
#
# THE BROWSER LEG RUNS WHEN A SERVER IS ADDED, NEVER WHEN A TOOL IS CALLED.
# That is the same sentence E2 and E3 are built on, one layer up: adding is when
# the person is at the keyboard and expects to be asked, and a turn is when they
# are not. A client that opened a consent page in round 14 of a 24-round turn
# would stall the turn on a human who walked away -- and it would do it for a
# token that expired quietly an hour ago. So a CALL may refresh silently, and a
# call may fail with a sentence naming what to run. It may not ask.
#
# WHAT IS IMPLEMENTED, all of it required of a client by the specification:
#   * RFC 9728 protected resource metadata, from `WWW-Authenticate` when the 401
#     carries it and from the two well-known paths when it does not;
#   * RFC 8414 authorization server metadata, tried in the documented order,
#     with the issuer compared against the URL it was fetched from;
#   * PKCE with S256, and a REFUSAL where the server does not advertise it --
#     `code_challenge_methods_supported` absent means no PKCE, and no PKCE means
#     an authorization code anybody who sees it can redeem;
#   * RFC 7591 dynamic client registration, with a configured `client_id` as the
#     way out where a server does not offer it;
#   * RFC 8707 `resource`, on the authorization request AND the token request,
#     so the token is bound to this server and cannot be replayed at another;
#   * `state` on the way out and compared on the way back, and `iss` compared
#     where the server returns it.


def _oauth_open(url: str) -> bool:
    """Hand the authorisation URL to the browser.

    THE ONE STEP A SUITE CANNOT PERFORM, and it is a seam for exactly that
    reason: a test replaces the PERSON here and nothing else. Everything below
    -- the loopback listener, the redirect, the code, the exchange -- stays real
    HTTP against a real server.
    """
    import webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _oauth_pkce() -> "tuple[str, str]":
    """A verifier and its S256 challenge. Never `plain`: the specification calls
    S256 mandatory where the client can do it, and this one can."""
    import base64
    import hashlib
    import secrets

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    verifier = b64(secrets.token_bytes(32))
    return verifier, b64(hashlib.sha256(verifier.encode("ascii")).digest())


def _oauth_state() -> str:
    import base64
    import secrets
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def mcp_www_auth(header: str) -> dict:
    """The parameters out of a `WWW-Authenticate` challenge.

    QUOTED VALUES MAY CONTAIN COMMAS -- a `scope="files:read files:write"` is
    one value and a URL is another, and splitting the header on commas first
    cuts both in half. So the pairs are read with a scanner, not a split.
    """
    found = {}
    for key, quoted, bare in re.findall(
            r'([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?:"([^"]*)"|([^\s,]+))',
            str(header or "")):
        found[key.lower()] = quoted if quoted else bare
    return found


def _oauth_loopback(url: str) -> bool:
    """Is this an address a token may travel to without TLS?

    The specification allows exactly two: `https`, or a loopback redirect. A
    plain `http://` anywhere else is a token on the wire in clear text, and it
    is refused rather than warned about.
    """
    parsed = urllib.parse.urlparse(str(url or ""))
    return (parsed.scheme.lower() == "http"
            and (parsed.hostname or "").lower() in ("127.0.0.1", "::1", "localhost"))


def _oauth_safe(url: str) -> bool:
    return urllib.parse.urlparse(str(url or "")).scheme.lower() == "https" or _oauth_loopback(url)


def _oauth_canonical(endpoint: str) -> str:
    """The `resource` value: this server's canonical URI.

    Lowercase scheme and host, no fragment, and the path kept -- a host may
    serve two MCP servers under two paths, and a token bound to the host alone
    would be valid at both.
    """
    parsed = urllib.parse.urlparse(str(endpoint or ""))
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunparse((parsed.scheme.lower(), (parsed.netloc or "").lower(),
                                    path, "", "", ""))


def _oauth_json(url: str, data=None, headers=None
                ) -> "tuple[dict | None, str | None]":
    """One JSON call in the authorisation flow, and every failure as a sentence.

    THE CLIENT NAMES ITSELF HERE TOO. Measured 2026-08-22 on the MCP endpoint
    itself: a network that refuses `Python-urllib` refuses it at every path, and
    a discovery call that came back 403 while the tool call worked would be the
    least readable failure in this file.
    """
    if not _oauth_safe(url):
        return None, ("%s is not https and not loopback -- a token may not "
                      "travel over it" % url)
    sending = {"Accept": "application/json",
               "User-Agent": "Crow/%s (+%s)" % (CLIENT_VERSION or "dev", REPO_URL)}
    body = None
    if data is not None:
        if isinstance(data, dict) and headers is None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            sending["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode("utf-8")
            sending["Content-Type"] = "application/json"
    sending.update(headers or {})
    try:
        request = urllib.request.Request(url, data=body, headers=sending)
        with urllib.request.urlopen(request, timeout=MCP_OAUTH_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            said = " ".join(exc.read().decode("utf-8", "replace").split())
        except Exception:
            said = ""
        return None, _mcp_redact(
            "%s answered HTTP %s%s"
            % (url, exc.code, (": " + said[:MCP_HTTP_SAID]) if said else ""))
    except (OSError, ValueError) as exc:
        return None, "%s could not be reached: %s" % (url, exc)
    try:
        answer = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None, "%s did not answer JSON" % url
    if not isinstance(answer, dict):
        return None, "%s answered JSON that is not an object" % url
    return answer, None


def _oauth_resource_metadata(endpoint: str, challenge: dict
                             ) -> "tuple[dict | None, str | None]":
    """RFC 9728, from the header where the server put it and from the two
    well-known paths where it did not.

    BOTH MECHANISMS, IN THIS ORDER, because the specification requires a client
    to support both: the header when present, then the path-qualified
    well-known, then the root one. A client that only read the header cannot
    reach a server that only serves the file.
    """
    parsed = urllib.parse.urlparse(endpoint)
    root = "%s://%s" % (parsed.scheme, parsed.netloc)
    path = parsed.path.rstrip("/")
    tries = []
    if challenge.get("resource_metadata"):
        tries.append(challenge["resource_metadata"])
    if path:
        tries.append(root + "/.well-known/oauth-protected-resource" + path)
    tries.append(root + "/.well-known/oauth-protected-resource")

    problems = []
    for url in tries:
        found, problem = _oauth_json(url)
        if problem:
            problems.append(problem)
            continue
        servers = found.get("authorization_servers")
        if not isinstance(servers, list) or not servers:
            problems.append("%s names no authorization_servers" % url)
            continue
        return found, None
    return None, ("no protected resource metadata for %s -- %s"
                  % (endpoint, "; ".join(problems[:2])))


def _oauth_server_metadata(issuer: str) -> "tuple[dict | None, str | None]":
    """RFC 8414, tried in the documented order, and the issuer CHECKED.

    THE CHECK IS THE POINT, not paperwork: a document fetched from
    `attacker.example` that names `honest.example` as its issuer would otherwise
    send the user's consent -- and the token that follows it -- to whoever
    answered. The specification says reject, in those words.
    """
    parsed = urllib.parse.urlparse(str(issuer or ""))
    root = "%s://%s" % (parsed.scheme, parsed.netloc)
    path = parsed.path.rstrip("/")
    if path:
        tries = [root + "/.well-known/oauth-authorization-server" + path,
                 root + "/.well-known/openid-configuration" + path,
                 root + path + "/.well-known/openid-configuration"]
    else:
        tries = [root + "/.well-known/oauth-authorization-server",
                 root + "/.well-known/openid-configuration"]

    problems = []
    for url in tries:
        found, problem = _oauth_json(url)
        if problem:
            problems.append(problem)
            continue
        if str(found.get("issuer") or "").rstrip("/") != str(issuer).rstrip("/"):
            return None, ("%s claims issuer %r while it was fetched as %r -- "
                          "refusing it" % (url, found.get("issuer"), issuer))
        # NO PKCE, NO FLOW. There is no other way for a client to learn whether
        # the server supports it, so an absent field is a NO -- and an
        # authorization code without PKCE is one anybody who sees it can redeem.
        methods = found.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            return None, ("%s does not advertise S256 in "
                          "code_challenge_methods_supported -- without PKCE this "
                          "client will not authorise" % issuer)
        for needed in ("authorization_endpoint", "token_endpoint"):
            if not _oauth_safe(found.get(needed)):
                return None, ("%s names no usable %s" % (issuer, needed))
        return found, None
    return None, ("no authorization server metadata for %s -- %s"
                  % (issuer, "; ".join(problems[:2])))


def _oauth_register(meta: dict, redirect: str, client_name: str
                    ) -> "tuple[dict | None, str | None]":
    """RFC 7591. A public client, because Crow ships to desktops.

    `token_endpoint_auth_method: none` IS THE HONEST DECLARATION. A secret in a
    program the user runs is not a secret, and claiming otherwise would have the
    authorization server treat this client as confidential when it cannot be.
    """
    where = meta.get("registration_endpoint")
    if not where:
        return None, ("this authorization server offers no dynamic registration. "
                      "Put a 'client_id' in the server's block in mcp.json")
    return _oauth_json(where, data={
        "client_name": client_name,
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }, headers={"Content-Type": "application/json"})


def _oauth_listen(host: str = "127.0.0.1"):
    """A listener on a port the operating system picks, and the redirect it is.

    THE LISTENER ALWAYS BINDS 127.0.0.1; only the NAME in the redirect URI
    changes. Some authorization servers sit behind a WAF that refuses any
    authorize request whose query string carries a literal `127.0.0.1`, and
    `localhost` is the way past it -- documented by Hermes, read 2026-08-22.
    Binding the name would be a different thing entirely and is not done.

    LOOPBACK ONLY. `0.0.0.0` here would put the authorization code -- the one
    thing between a stranger and this user's account -- on every interface of
    the machine for the length of the flow.

    THE IMPORT IS LOCAL, and that is a start-up decision rather than style:
    `http.server` drags in `mimetypes`, which reads the registry on Windows at
    import time. Every client start would pay for it, and a browser leg happens
    once per server per year.
    """
    import http.server

    class Catch(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_GET(self):
            got = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.server.caught = {k: v[0] for k, v in got.items()}
            self.server.done.set()
            said = ("Crow: authorisation received. Close this tab."
                    if "code" in self.server.caught else
                    "Crow: the authorization server sent no code. Close this tab.")
            body = said.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), Catch)
    server.caught = {}
    server.done = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="crow-oauth").start()
    name = host if host in ("127.0.0.1", "localhost") else "127.0.0.1"
    return server, "http://%s:%d/callback" % (name, server.server_address[1])


def _oauth_exchange(meta: dict, form: dict) -> "tuple[dict | None, str | None]":
    answer, problem = _oauth_json(meta["token_endpoint"], data=form)
    if problem:
        return None, problem
    if not answer.get("access_token"):
        return None, _mcp_redact(
            "the token endpoint returned no access_token: %s"
            % strip_tag_characters(str(answer.get("error") or answer))[:200])
    return answer, None


def mcp_token_doc() -> dict:
    try:
        with open(MCP_TOKEN_FILE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def mcp_token_write(doc: dict) -> "str | None":
    """Write the token store, and read it back in the same call.

    Same contract as `mcp_write`, and here it decides more: a token that was not
    written is a browser leg the user gets to do again, and they would find that
    out on the next call rather than now.
    """
    try:
        os.makedirs(os.path.dirname(MCP_TOKEN_FILE), exist_ok=True)
        with open(MCP_TOKEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        # OWNER ONLY, WHERE THE PLATFORM MEANS IT. On POSIX this is the whole
        # protection; on Windows `chmod` reaches only the read-only bit and the
        # real answer is the ACL on %LOCALAPPDATA%. Doing it anyway costs one
        # call and is right on the platform where it counts.
        try:
            os.chmod(MCP_TOKEN_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return "mcp_tokens.json could not be written: %s" % exc
    if mcp_token_doc().get("servers") != doc.get("servers"):
        return "mcp_tokens.json did not read back as it was written"
    return None


def mcp_token_for(name: str) -> dict:
    record = (mcp_token_doc().get("servers") or {}).get(name)
    return record if isinstance(record, dict) else {}


def mcp_token_drop(name: str) -> None:
    """Forget a server's credentials. Called when the server is removed.

    A TOKEN THAT OUTLIVES ITS SERVER is a grant nobody can see and nobody
    revokes -- the configuration no longer mentions the server, so nothing in
    either surface would ever show it again.
    """
    doc = mcp_token_doc()
    servers = doc.get("servers") or {}
    if name in servers:
        servers.pop(name)
        doc["servers"] = servers
        mcp_token_write(doc)


def _oauth_store(name: str, record: dict, answer: dict) -> "str | None":
    kept = dict(record)
    kept["access_token"] = answer["access_token"]
    kept["token_type"] = answer.get("token_type") or "Bearer"
    # A REFRESH TOKEN IS ROTATED BY SOME SERVERS AND OMITTED BY OTHERS. Keeping
    # the old one when the answer carries none is right; overwriting it with an
    # empty value would turn every future refresh into a browser leg.
    if answer.get("refresh_token"):
        kept["refresh_token"] = answer["refresh_token"]
    try:
        kept["expires_at"] = time.time() + float(answer.get("expires_in"))
    except (TypeError, ValueError):
        kept.pop("expires_at", None)
    doc = mcp_token_doc()
    doc.setdefault("servers", {})[name] = kept
    return mcp_token_write(doc)


def mcp_authorise(name: str, block: dict, challenge: dict | None = None
                  ) -> "str | None":
    """The whole browser flow, start to stored token. None when it worked.

    CALLED FROM THE ADD PATH AND FROM `/mcp auth`, never from a tool call.
    """
    endpoint = str(block.get("url") or "").strip()
    if not endpoint:
        return "%r is not an HTTP server, so there is nothing to authorise" % name
    challenge = challenge or {}
    resource = _oauth_canonical(endpoint)

    prm, problem = _oauth_resource_metadata(endpoint, challenge)
    if problem:
        return problem
    # THE DOCUMENT'S OWN `resource` WINS OVER THE DERIVED ONE, and it is checked
    # against the host before it does. Measured 2026-08-22: GitHub's metadata
    # names `https://api.githubcopilot.com/mcp/` WITH the trailing slash, and a
    # client that sent its own stripped form would be asking for a token bound
    # to a resource the server does not know it by.
    #
    # THE CHECK IS NOT A FORMALITY: this document is fetched from a host that
    # just refused us, and one that named somebody else's resource would have
    # this client ask an authorization server for a token belonging to a service
    # it is not talking to.
    named = prm.get("resource")
    if isinstance(named, str) and named.strip():
        if (urllib.parse.urlparse(named).netloc.lower()
                != urllib.parse.urlparse(endpoint).netloc.lower()):
            return ("the metadata for %s names resource %r, which is a different "
                    "host -- refusing it"
                    % (endpoint, strip_tag_characters(named)[:120]))
        resource = named.strip()

    # EVERY SERVER THE DOCUMENT LISTS, IN ORDER, not just the first one. RFC 9728
    # allows several and says the choice is the client's; taking `[0]` and
    # stopping would refuse a resource whose first entry is retired, misconfigured
    # or simply not the one a desktop client can use.
    issuer, meta, problem = None, None, None
    reasons = []
    for candidate in prm["authorization_servers"]:
        meta, problem = _oauth_server_metadata(str(candidate))
        if not problem:
            issuer = str(candidate)
            break
        reasons.append(problem)
    if issuer is None:
        return "; ".join(reasons[:3])

    listener, redirect = _oauth_listen(str(block.get("redirect_host") or "127.0.0.1"))
    try:
        record = {"issuer": issuer, "resource": resource,
                  "token_endpoint": meta["token_endpoint"]}
        # A PRE-REGISTERED PAIR COMES FROM THE BLOCK, BOTH HALVES. Some servers
        # reject dynamic registration outright -- Google's Drive endpoint answers
        # 400 -- and then the only way in is a client the user created in the
        # provider's console, which may well be a confidential one.
        client_id = block.get("client_id") or mcp_token_for(name).get("client_id")
        if block.get("client_secret"):
            record["client_secret"] = str(block["client_secret"])
        if not client_id:
            # THE NAME IS CONFIGURABLE, and it is not cosmetic: Figma's endpoint
            # allowlists dynamic registration BY `client_name` and 403s anything
            # it does not know. Crow says "Crow" and lets somebody who needs a
            # different name set one, rather than shipping a name that claims to
            # be another client.
            registered, problem = _oauth_register(
                meta, redirect, str(block.get("client_name") or "Crow"))
            if problem:
                # THE WAY OUT IS NAMED WHATEVER WENT WRONG. A registration
                # endpoint that is absent and one that answers 404 leave the
                # user in the same place, and only one of them was saying so.
                return ("%s. Put a 'client_id' in the server's block in mcp.json"
                        % problem.rstrip("."))
            client_id = registered.get("client_id")
            if not client_id:
                return "dynamic registration returned no client_id"
            if registered.get("client_secret"):
                record["client_secret"] = registered["client_secret"]
        record["client_id"] = str(client_id)

        # SCOPE COMES FROM THE SERVER OR NOT AT ALL. Asking for scopes nobody
        # named is a client deciding on the user's behalf how much access to
        # request, and the consent screen is where that decision is shown.
        scope = challenge.get("scope") or prm.get("scopes_supported")
        if isinstance(scope, list):
            scope = " ".join(str(s) for s in scope)
        verifier, code_challenge = _oauth_pkce()
        state = _oauth_state()
        query = {"response_type": "code", "client_id": client_id,
                 "redirect_uri": redirect, "state": state,
                 "code_challenge": code_challenge, "code_challenge_method": "S256",
                 "resource": resource}
        if scope:
            query["scope"] = str(scope)
            record["scope"] = str(scope)
        where = (meta["authorization_endpoint"]
                 + ("&" if "?" in meta["authorization_endpoint"] else "?")
                 + urllib.parse.urlencode(query))
        _oauth_open(where)
        if not listener.done.wait(MCP_OAUTH_WAIT):
            return ("nobody finished the authorisation for %r within %gs. The "
                    "page was: %s" % (name, MCP_OAUTH_WAIT, where))
        caught = listener.caught
        if caught.get("error"):
            return ("the authorization server refused: %s %s"
                    % (strip_tag_characters(str(caught.get("error")))[:80],
                       strip_tag_characters(str(caught.get("error_description") or ""))[:200]))
        # THE STATE IS THE BINDING, AND IT IS THE ONLY ONE THIS CLIENT NEEDS.
        # Without it, anything that can reach the loopback port can feed this
        # client a code of its own.
        if caught.get("state") != state:
            return "the authorisation came back with a state this client did not send"
        # `iss` IS READ AND NOT ENFORCED, decided 2026-08-22 after it refused a
        # working server. RFC 9207 has it guard MIX-UP: a client talking to
        # SEVERAL authorization servers in one flow could otherwise send one
        # server's code to another's token endpoint. This client talks to
        # exactly one -- the token endpoint is taken from the metadata
        # discovered BEFORE the browser opened and held in a local until the
        # exchange, so nothing coming back through the redirect can move it. The
        # code itself is worthless without the verifier held here, and the token
        # is bound to `resource`.
        #
        # WHAT ENFORCING IT COSTS IS EVERY BROKERED LOGIN. Measured on
        # higgsfield: its metadata declares `https://mcp.higgsfield.ai`, its
        # `/oauth2/authorize` hands off to Clerk, and Clerk stamps
        # `iss=https://clerk.higgsfield.ai` on the way back. Auth0, Okta, Clerk
        # and every other identity service sit on a domain of their own -- a
        # client that insisted on one issuer string would work with servers that
        # run their own login and with nobody else's.
        if not caught.get("code"):
            return "the authorisation came back without a code"

        form = {"grant_type": "authorization_code", "code": caught["code"],
                "redirect_uri": redirect, "client_id": client_id,
                "code_verifier": verifier, "resource": resource}
        if record.get("client_secret"):
            form["client_secret"] = record["client_secret"]
        answer, problem = _oauth_exchange(meta, form)
        if problem:
            return problem
        return _oauth_store(name, record, answer)
    finally:
        listener.shutdown()
        listener.server_close()


def mcp_refresh_token(name: str) -> "str | None":
    """One refresh, from the stored record. NO BROWSER, EVER.

    This is the half a tool call is allowed to run: it costs a round trip and no
    human. When it fails, the call fails with a sentence naming `/mcp auth`,
    because the alternative is a consent page opening in the middle of a turn.
    """
    record = mcp_token_for(name)
    if not record.get("refresh_token"):
        return "%r has no refresh token. Run: /mcp auth %s" % (name, name)
    form = {"grant_type": "refresh_token", "refresh_token": record["refresh_token"],
            "client_id": record.get("client_id", ""),
            "resource": record.get("resource", "")}
    if record.get("client_secret"):
        form["client_secret"] = record["client_secret"]
    if record.get("scope"):
        form["scope"] = record["scope"]
    answer, problem = _oauth_exchange({"token_endpoint": record.get("token_endpoint", "")},
                                      form)
    if problem:
        return "%s. Run: /mcp auth %s" % (problem, name)
    return _oauth_store(name, record, answer)


def mcp_authorise_server(name: str) -> "str | None":
    """`/mcp auth <server>` and the sheet's button: authorise a configured one."""
    doc, problems = mcp_doc()
    if problems:
        return problems[0]
    block = (doc.get("servers") or {}).get(name)
    if not isinstance(block, dict):
        return "no MCP server named %r is configured" % name
    problem = mcp_authorise(name, block)
    if problem:
        return problem
    # BOTH HALVES, AND THE ORDER MATTERS. A stored token says one arrived, not
    # that this server accepts it; a successful `tools/list` says the server
    # answered, not that it needed the token at all.
    #
    # THE SECOND HALF EXISTS BECAUSE THE FAILURE IS SILENT. A server that serves
    # `tools/list` WITHOUT auth -- Hermes documents Google Drive as one -- lets
    # an authorisation that never produced a token look like it worked, and the
    # first real tool call is where somebody finds out. Read 2026-08-22.
    if not mcp_token_for(name).get("access_token"):
        return ("%r was authorised and no token arrived. The server may reject "
                "dynamic registration -- put a 'client_id' in its block" % name)
    _, problem = mcp_fetch_tools(name, dict(block))
    return problem


def mcp_render(result: dict) -> str:
    """A `tools/call` result as the text the model gets back.

    THE FILTER RUNS ON THIS DIRECTION TOO. A result is prompt text written by a
    stranger exactly as a description is, so the invisible tag characters go
    here as well -- and a check that only cleaned the descriptions would be
    guarding the door a server does not walk through.
    """
    parts = []
    blocks = result.get("content")
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(strip_tag_characters(str(block.get("text") or "")))
            continue
        resource = block.get("resource")
        if kind == "resource" and isinstance(resource, dict) and isinstance(
                resource.get("text"), str):
            parts.append(strip_tag_characters(resource["text"]))
            continue
        # An image or an audio block cannot travel through a tool result -- this
        # client has one channel to the model and it is text. Saying what came
        # back beats returning nothing, which reads as a tool that did nothing.
        parts.append("[the server returned a %s block, which this client cannot "
                     "pass on]" % strip_tag_characters(str(kind or "nameless")))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(_mcp_clean(result["structuredContent"]), indent=1))
    body = "\n".join(part for part in parts if part)
    if not body:
        body = "[the server answered with nothing]"
    # `isError` IS THE SPECIFICATION'S OWN SHAPE and it exists so the model can
    # see the failure and correct itself. It is a result, not a client fault --
    # and it is the half that gets REDACTED, because a failing server quotes the
    # request that failed and a working one quotes nothing.
    return ("error: " + _mcp_redact(body)) if result.get("isError") else body


def mcp_call(server: str, tool: str, arguments: dict) -> str:
    """One `tools/call`, and every way it can fail rendered as a tool result."""
    live, problem = mcp_server(server)
    if problem:
        return _clip("error: " + _mcp_redact(problem))
    timeout = _mcp_seconds(live.block.get("timeout"), MCP_CALL_TIMEOUT)
    answer, problem = live.request("tools/call",
                                   {"name": tool, "arguments": arguments}, timeout)
    if problem:
        # THE SESSION GOES WITH THE FAILURE. A server that timed out may still
        # answer later, and that answer would arrive in the queue in front of the
        # NEXT call's -- one late reply and every result after it is off by one.
        _mcp_drop(server)
        return _clip("error: " + _mcp_redact(problem))
    failure = answer.get("error")
    if failure:
        return _clip("error: the MCP server %r refused the call: [%s] %s"
                     % (server, failure.get("code"),
                        _mcp_redact(strip_tag_characters(
                            str(failure.get("message") or failure)))))
    return _clip(mcp_render(answer.get("result") or {}))


def _mcp_drop(name: str) -> None:
    with _MCP_LIVE_LOCK:
        server = _MCP_LIVE.pop(name, None)
    if server is not None:
        server.close()


def mcp_fetch_tools(name: str, block: dict | None = None
                    ) -> "tuple[list | None, str | None]":
    """Ask a server what it offers. THE ONE PLACE `tools/list` IS EVER ASKED.

    Called when a server is ADDED, so that the answer can be written to disk and
    `TOOLS` never has to ask again. Never at start -- that is the sentence the
    whole design hangs on.

    `block` is passed when the server is not in the file yet, which is the add
    path itself: it is started, asked and ended again, and nothing is kept.
    """
    ad_hoc = block is not None
    live, problem = mcp_server(name, block)
    if problem:
        return None, problem
    try:
        timeout = _mcp_seconds(live.block.get("timeout"), MCP_CALL_TIMEOUT)
        found, cursor = [], None
        for _ in range(MCP_LIST_PAGES):
            answer, problem = live.request(
                "tools/list", {"cursor": cursor} if cursor else {}, timeout)
            if problem:
                return None, problem
            failure = answer.get("error")
            if failure:
                return None, ("the MCP server %r would not list its tools: [%s] %s"
                              % (name, failure.get("code"),
                                 strip_tag_characters(str(failure.get("message")
                                                          or failure))))
            result = answer.get("result") or {}
            page = result.get("tools")
            found.extend(t for t in (page if isinstance(page, list) else [])
                         if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return [_mcp_clean(tool) for tool in found], None
    finally:
        if ad_hoc:
            live.close()


def _mcp_caller(server: str, tool: str):
    """What a declared MCP tool runs. `**arguments` because the schema is the
    server's, so `run_tool`'s TypeError arm would blame the model for a shape
    this client never validated."""
    def call(**arguments):
        return mcp_call(server, tool, arguments)
    return call


# ---------------------------------------------------------------- E4 ------
# THE CHECKLIST. THE HINT PROPOSES, A PERSON DISPOSES, THE FILE REMEMBERS WHICH.
#
# The specification is blunt about what an annotation is worth: clients "must
# treat these hints as untrusted unless they come from a trusted server source",
# and they "are not authorization constructs". So a hint may fill a form in. It
# may not answer it.
#
# WHY FOLLOWING AN UNTRUSTED HINT IS STILL SAFE HERE: because it decides
# nothing. It pre-fills a form somebody nods at, and what the nod produces is
# written into Crow's own configuration -- not the server's answer. A server
# that reports `readOnlyHint: true` tomorrow changes the stored classification
# by exactly nothing, the same construction the pinned memory head has.
#
# AND THE DEFAULTS ALREADY POINT THE SAFE WAY. Absent, `readOnlyHint` is false
# and `destructiveHint` is true -- "no statement" reads as "writes, and
# destructively". A server can therefore only lie in ONE direction, towards
# harmless, and that single direction is what the person at the keyboard is
# there to catch. Somebody who clicks through the list without reading it gets
# the strict answer, not the convenient one.
#
# ADDING IS NOT CONFIRMING, and that split is the whole stage. Adding fetches
# the schema once and writes it down, so nothing ever has to ask again -- and
# declares NOT ONE TOOL, because nobody has said yes to one yet. The prompt head
# does not move until somebody ticks something, which is also where the bill for
# moving it belongs.

MCP_USAGE = (
    "  /mcp                               what is configured\n"
    "  /mcp add <command line>            add a server, take what it offers\n"
    "  /mcp add <url> [--header n: v]     the same server, over HTTP\n"
    "  /mcp auth <server>                 authorise it in the browser\n"
    "  /mcp fetch <server>                ask it again\n"
    "  /mcp use <server> <tool> <class>   reading, writing or executing\n"
    "  /mcp drop <server> <tool>          take it out"
)


_MCP_GENERIC = frozenset(("index", "main", "server", "dist", "build", "src",
                          "app", "cli", "bin", "start", "run"))

# The same idea one layer up: labels in a host that say what a thing IS rather
# than whose it is. `mcp.context7.com` is context7's, and the `mcp.` in front of
# it names every MCP server on earth.
_MCP_HOST_GENERIC = frozenset(("mcp", "www", "api", "sse", "http", "https",
                               "remote", "server"))

# Second-level suffixes, where the label before the last one is still the
# registry rather than the house: `something.co.uk` is not `co`.
_MCP_SUFFIX = frozenset(("co", "com", "org", "net", "gov", "edu", "ac"))


def _mcp_is_url(token) -> bool:
    return urllib.parse.urlparse(str(token)).scheme.lower() in ("http", "https")


def _mcp_name_from_host(url: str) -> str:
    """`https://mcp.context7.com/mcp` is context7, and Cloudflare's several are
    not all called cloudflare.

    THE HOUSE IS THE LABEL BEFORE THE SUFFIX -- that is the part of a host that
    says WHO answers. What sits in front of it is either noise (`mcp.`, `www.`,
    `api.`) or the only thing telling two servers of one house apart, and
    `docs.mcp.cloudflare.com` beside `bindings.mcp.cloudflare.com` is exactly
    that case. Taking the first label instead would have named the first of them
    "docs", which says nothing about whose docs.
    """
    host = urllib.parse.urlparse(url).hostname or ""
    labels = [part for part in host.split(".") if part]
    if not labels:
        return "server"
    # AN ADDRESS HAS NO HOUSE IN IT. `127.0.0.1` would otherwise be named after
    # its third octet, which is a name nobody could guess twice.
    if all(part.isdigit() for part in labels):
        return _mcp_slug(host) or "server"
    cut = 2 if len(labels) > 1 else 1
    if len(labels) > cut and _mcp_slug(labels[-cut]) in _MCP_SUFFIX:
        cut += 1
    marks = [part for part in labels[:-cut]
             if _mcp_slug(part) not in _MCP_HOST_GENERIC]
    return _mcp_slug("_".join([labels[-cut]] + marks)) or "server"


def _mcp_headers_from(line: str) -> "tuple[str, dict, list]":
    """`--header Authorization: Bearer x` cut off an add line.

    SPLIT ON THE FLAG, NOT ON WHITESPACE, and that is the whole reason this is
    not `shlex`. A token is `Bearer <something>` -- two words -- and a Windows
    path is full of backslashes that POSIX quoting eats. Cutting the line at
    each flag takes all of the rest as one value and never meets either problem.

    A HEADER THAT DID NOT PARSE COMES BACK AS A COMPLAINT, never as a header
    quietly dropped. The failure it prevents is the confusing one: an
    `Authorization` nobody sent, reported by the server as a plain 401.
    """
    chunks = re.split(r"(?:^|\s)(?:--header|-H)\s+", str(line or ""))
    headers, bad = {}, []
    for chunk in chunks[1:]:
        chunk = chunk.strip().strip('"').strip("'").strip()
        field, sep, value = chunk.partition(":")
        field, value = field.strip(), value.strip()
        # A NEWLINE IN A HEADER IS A SECOND HEADER. Anything below a space ends
        # the field as far as HTTP is concerned, so it may not travel in one.
        if not sep or not field or not value or any(
                ord(ch) < 32 or ord(ch) == 127 for ch in field + value):
            bad.append(chunk)
            continue
        headers[field] = value
    return chunks[0], headers, bad


def mcp_name_from(argv: list) -> str:
    """A server name out of the command line, so nobody has to invent one.

    `npx -y @modelcontextprotocol/server-github` is github. A path ending in
    dist/index.js is the project it sits in, because "index" names nothing --
    that is what `_MCP_GENERIC` is for, and it is the case a basename-only rule
    gets wrong on every Node server there is. A URL is named from its host,
    which is the same idea against a different kind of line.
    """
    parts = [str(a) for a in argv if not str(a).startswith("-")]
    if parts and _mcp_is_url(parts[0]):
        return _mcp_name_from_host(parts[0])
    # THE FIRST TOKEN AFTER THE LAUNCHER, NOT THE LAST. `npx -y
    # @modelcontextprotocol/server-filesystem C:/Users/robin/dev/Crow` ends in
    # the directory the server was pointed AT -- reading backwards named that
    # server "crow", after the folder it happens to serve.
    for token in (parts[1:] or parts):
        raw = str(token).replace("\\", "/").rstrip("/")
        pieces = [p for p in raw.split("/") if p]
        while pieces:
            stem = pieces.pop().split(".")[0]
            for _ in range(3):     # mcp-server-fetch strips twice, not once
                stem = re.sub(r"^(?:@[^/]*|mcp|server)[-_]", "", stem)
                stem = re.sub(r"[-_](?:mcp|server)$", "", stem)
            slug = _mcp_slug(stem)
            if slug and slug not in _MCP_GENERIC:
                return slug
    return _mcp_slug(str(parts[0]).replace("\\", "/").split("/")[-1].split(".")[0]) or "server"


def mcp_add_line(line: str) -> "tuple[str | None, dict | None, str | None]":
    """Add a server from one line, a command or a URL: `(name, view, problem)`.

    THE NAME COMES BACK, and that is not decoration. Without it the caller has
    to guess which of the servers it is now looking at is the one it just added
    -- and "the last one" is wrong the moment the list is sorted: adding
    a second server beside the first confirmed the WRONG one, on screen, on
    2026-08-22.
    """
    rest, headers, bad = _mcp_headers_from(line)
    argv = [a for a in rest.split() if a]
    if not argv:
        return None, None, ("give a command line or a URL, e.g. "
                            "npx -y @modelcontextprotocol/server-github")
    if bad:
        return None, None, ("--header takes 'name: value'; %r is not one"
                            % bad[0][:80])
    name = mcp_name_from(argv)
    if _mcp_is_url(argv[0]):
        # A URL IS ONE TOKEN AND NOTHING FOLLOWS IT. Anything else on the line is
        # somebody typing a stdio line at an HTTP server, and swallowing it would
        # store an endpoint with an argument nothing will ever read.
        if len(argv) > 1:
            return None, None, ("a URL takes no arguments -- %r is one. A token "
                                "goes in --header <name: value>" % argv[1][:80])
        block = {"url": argv[0]}
        if headers:
            block["headers"] = headers
    elif headers:
        return None, None, ("--header belongs to a URL. A command gets its "
                            "secrets through 'env' in mcp.json")
    else:
        block = {"command": argv[0], "args": argv[1:]}
    view, problem = mcp_add_server(name, block)
    return name, view, problem


def mcp_proposed_class(tool: dict) -> str:
    """What the annotation SUGGESTS, with the specification's defaults where it
    is silent. A proposal for a form, never an answer to one.

    `is True` AND `is False`, NOT TRUTHINESS. A hint arrives as JSON from a
    stranger: `"readOnlyHint": "yes"` is a string, and a truthy test would read
    it as the safe-for-the-server answer. Anything that is not exactly the
    boolean falls through to the strict end.
    """
    hints = tool.get("annotations")
    hints = hints if isinstance(hints, dict) else {}
    if hints.get("readOnlyHint") is True:
        return "reading"
    if hints.get("destructiveHint") is False:
        return "writing"
    return "executing"


def _mcp_cost(declarations: list) -> int:
    """Characters these declarations add to `json.dumps(TOOLS)`.

    The list's own separators count: appending one entry to a non-empty list
    costs `", "` as well as the object, which is exactly what `json.dumps` of
    the appended entries alone comes to.
    """
    return len(json.dumps(declarations, sort_keys=True)) if declarations else 0


def mcp_write(doc: dict) -> "str | None":
    """Write the configuration, and READ IT BACK in the same call.

    Persistence is a contract, not a one-way valve. A writer nobody reads back
    is a writer nobody has proved -- three times in one day on 2026-08-21, each
    time with a green suite behind it.
    """
    try:
        os.makedirs(os.path.dirname(MCP_FILE), exist_ok=True)
        with open(MCP_FILE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
    except OSError as exc:
        return "mcp.json could not be written: %s" % exc
    back, problems = mcp_doc()
    if problems:
        return problems[0]
    if back.get("servers") != doc.get("servers"):
        return "mcp.json did not read back as it was written"
    return None


def mcp_add_server(name: str, block: dict) -> "tuple[dict | None, str | None]":
    """Ask a server once what it offers, and write the answer down.

    NOTHING IS DECLARED BY THIS. A new server arrives with an EMPTY positive
    list, so the prompt head does not move and no tool is offered to the model
    until somebody ticks it. An existing server keeps the ticks and the classes
    it already had -- a refresh is not an undo.

    NOTHING IS WRITTEN IF THE FETCH FAILS. Half a block for a server nobody can
    reach is a configuration the next start reports as broken.
    """
    if not _mcp_slug(name):
        return None, "%r is not a name the tool list can carry" % name
    if not isinstance(block, dict):
        return None, "server %r needs a command block" % name
    doc, problems = mcp_doc()
    if problems:
        return None, problems[0]
    _mcp_take_401(name)
    tools, problem = mcp_fetch_tools(name, block)
    if problem:
        # THE ONE PLACE A BROWSER MAY OPEN, and it is this one because it is the
        # only moment the client knows somebody is at the keyboard: they just
        # typed the line. A tool call three rounds into a turn does not know
        # that and must never assume it.
        challenge = _mcp_take_401(name)
        if challenge is None:
            return None, problem
        problem = mcp_authorise(name, block, challenge)
        if problem:
            return None, problem
        tools, problem = mcp_fetch_tools(name, block)
        if problem:
            return None, problem

    known = (doc.get("servers") or {}).get(name)
    known = known if isinstance(known, dict) else {}
    offered = {t.get("name") for t in tools if isinstance(t, dict)}
    kept_tools = known.get("tools") if isinstance(known.get("tools"), dict) else {}
    kept_classes = known.get("classes") if isinstance(known.get("classes"), dict) else {}

    stored = {k: v for k, v in block.items() if k not in ("schema",)}
    stored["schema"] = {"tools": tools}
    # ADDING A SERVER MAKES IT USABLE, and that is robin's call on 2026-08-22
    # against the way I first built it. Every other client works this way -- one
    # command and the tools are there -- and a client that demands twelve ticks
    # before anything works is a client nobody configures twice.
    #
    # `classes` STAYS EMPTY, which is what keeps this safe: `needs_approval`
    # answers `executing` for a name it has not heard of, so `manual` and
    # `allowedit` still stop before every call. The checklist is where somebody
    # UNTICKS and relaxes -- effort buys convenience, never safety.
    #
    # A SERVER THAT IS ALREADY CONFIGURED KEEPS ITS TICKS. A refresh is not an
    # undo, and it may not re-take what somebody took out.
    known_before = isinstance(known.get("tools"), dict)
    # A HAND-WRITTEN GLOB SURVIVES A REFRESH. It matches no name in `offered`,
    # so a filter that only kept known names would quietly delete the one line
    # somebody wrote to keep 3,000 tools out.
    include = ([t for t in (kept_tools.get("include") or [])
                if t in offered or _mcp_pattern(t)]
               if known_before else sorted(offered))
    stored["tools"] = dict(kept_tools, include=sorted(include))
    classes = {t: c for t, c in kept_classes.items()
               if t in offered and c in MCP_TOOL_CLASSES}
    if classes:
        stored["classes"] = classes
    else:
        stored.pop("classes", None)

    doc.setdefault("servers", {})[name] = stored
    problem = mcp_write(doc)
    if problem:
        return None, problem
    mcp_apply(doc)
    return mcp_view(), None


def mcp_confirm(name: str, choices: dict) -> "str | None":
    """What a person ticked, written into Crow's configuration.

    CHECKED WHOLE BEFORE ANYTHING IS WRITTEN. A checklist half applied is worse
    than one refused: the sheet would show one state, the file another, and only
    the file decides what the model is offered.

    A CHOICE WITHOUT A `class` KEY LEAVES THE STORED CLASS ALONE, and that is
    not the same as `"class": None`. Un-ticking a tool must not also throw away
    the decision about what it does, or re-ticking it later would ask again.
    """
    doc, problems = mcp_doc()
    if problems:
        return problems[0]
    block = (doc.get("servers") or {}).get(name)
    if not isinstance(block, dict):
        return "no MCP server named %r is configured" % name
    if not isinstance(choices, dict):
        return "that checklist could not be read"
    schema = block.get("schema") if isinstance(block.get("schema"), dict) else {}
    stored = schema.get("tools")
    offered = {t.get("name") for t in (stored if isinstance(stored, list) else [])
               if isinstance(t, dict)}
    for tool, choice in choices.items():
        if tool not in offered:
            return ("%r is not a tool the server %r offered -- only what its "
                    "stored schema lists can be taken" % (tool, name))
        klass = (choice or {}).get("class")
        if "class" in (choice or {}) and klass is not None and klass not in MCP_TOOL_CLASSES:
            return ("%r is not one of %s" % (klass, ", ".join(MCP_TOOL_CLASSES)))

    kept = block.get("tools") if isinstance(block.get("tools"), dict) else {}
    include = [t for t in (kept.get("include") or [])
               if t in offered or _mcp_pattern(t)]
    classes = dict(block.get("classes") or {}) if isinstance(
        block.get("classes"), dict) else {}
    for tool, choice in choices.items():
        choice = choice or {}
        if choice.get("included"):
            if tool not in include:
                include.append(tool)
        elif tool in include:
            include.remove(tool)
        if "class" in choice:
            if choice.get("class"):
                classes[tool] = choice["class"]
            else:
                classes.pop(tool, None)
    block["tools"] = dict(kept, include=sorted(include))
    if classes:
        block["classes"] = classes
    else:
        block.pop("classes", None)

    problem = mcp_write(doc)
    if problem:
        return problem
    mcp_apply(doc)
    return None


def mcp_refresh_server(name: str) -> "str | None":
    """Ask a configured server for its tools again, keeping what was ticked.

    ONE FUNCTION FOR BOTH SURFACES rather than the same four steps written out
    in each. `/mcp fetch` and the sheet's "ask again" are one operation, and two
    copies of it would agree right up to the day one of them was fixed.
    """
    doc, problems = mcp_doc()
    if problems:
        return problems[0]
    block = (doc.get("servers") or {}).get(name)
    if not isinstance(block, dict):
        return "no MCP server named %r is configured" % name
    _, problem = mcp_add_server(name, block)
    return problem


def mcp_remove_server(name: str) -> "str | None":
    """Take a server out of the configuration, and end it if it is running."""
    doc, problems = mcp_doc()
    if problems:
        return problems[0]
    servers = doc.get("servers") or {}
    if name not in servers:
        return "no MCP server named %r is configured" % name
    servers.pop(name)
    doc["servers"] = servers
    # THE CREDENTIAL GOES WITH THE SERVER. A token left behind is a grant
    # nothing in either surface would ever show again and nobody would revoke.
    mcp_token_drop(name)
    problem = mcp_write(doc)
    if problem:
        return problem
    mcp_apply(doc)
    return None


def mcp_view() -> dict:
    """Everything a surface needs to draw the checklist, in one shape.

    ONE VIEW FOR BOTH CLIENTS. Two ways of describing one configuration diverge,
    and the second one gets worse -- #90's failure in the shape
    `check_shared_core` cannot see, because both sides would still call the core.
    """
    doc, problems = mcp_doc()
    entries, more = mcp_catalog(doc)
    declared = {entry["name"]: entry for entry in entries}
    servers = []
    for name in sorted(doc.get("servers") or {}):
        block = (doc.get("servers") or {})[name]
        if not isinstance(block, dict):
            continue
        schema = block.get("schema") if isinstance(block.get("schema"), dict) else {}
        stored = schema.get("tools")
        classes = block.get("classes") if isinstance(block.get("classes"), dict) else {}
        tools, taken = [], []
        for tool in (stored if isinstance(stored, list) else []):
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            raw = tool["name"]
            qualified = mcp_tool_name(name, raw)
            entry = declared.get(qualified)
            if entry is not None:
                taken.append(entry["declaration"])
            klass = classes.get(raw)
            tools.append({
                "tool": raw,
                "name": qualified,
                "description": strip_tag_characters(str(tool.get("description") or "")),
                "proposed": mcp_proposed_class(tool),
                "class": klass if klass in MCP_TOOL_CLASSES else None,
                "included": entry is not None,
            })
        # `headers` IS NOT IN HERE AND MAY NOT BE. This shape is what both
        # surfaces draw and what any future one would; a Bearer token that
        # reaches a view reaches a screen, a screenshot and a bug report. The
        # url carries no secret and is what somebody needs to see to recognise
        # the server -- that pair is the whole rule.
        servers.append({"name": name,
                        "command": str(block.get("command") or ""),
                        "args": [str(a) for a in (block.get("args") or [])],
                        "url": str(block.get("url") or ""),
                        "enabled": block.get("enabled", True) is not False,
                        "cost": _mcp_cost(taken),
                        "tools": tools})
    return {"file": MCP_FILE, "classes": list(MCP_TOOL_CLASSES),
            "problems": problems + more, "servers": servers}


def mcp_installed(view: dict, name: str) -> str:
    """What `/mcp add` says when it worked. NOT the listing.

    robin, 2026-08-22, having watched it: printing the whole table and the
    command palette after an install is not a confirmation -- the user asked one
    question ("did it install?") and got the same answer `/mcp` gives, which is
    indistinguishable from having changed nothing. What each tool may do is a
    question for later, and it has a place of its own.
    """
    servers = [s for s in (view.get("servers") or []) if s["name"] == name]
    added = servers[0] if servers else None
    if added is None:
        return "nothing was added."
    return ("%s installed: %d tools, %s characters in every prompt.%s"
            "%s lists them; the window keeps them under Settings, MCPs.%s%s"
            % (added["name"], len(added["tools"]), "{:,}".format(added["cost"]),
               "\n", "/mcp", "\n", MCP_COST_NOTE))


def mcp_listing() -> str:
    """`/mcp`, in both clients, out of `mcp_view`.

    NO SENTENCE HERE POINTS AT A CONTROL. Prose about pixels cannot be tested,
    and the one time this repository tried it the prose was wrong about which
    side of the rail a button sits on. A path can be checked; "the panel on the
    left" cannot.
    """
    view = mcp_view()
    if not view["servers"]:
        return ("no MCP server is configured.\nwrite one into %s, then "
                "'/mcp fetch <server>'.\n\n%s" % (view["file"], MCP_USAGE))
    lines = ["MCP servers, from %s" % view["file"]]
    for server in view["servers"]:
        started = (server["url"]
                   or " ".join([server["command"]] + server["args"]).strip())
        lines.append("")
        lines.append("%s   %s%s" % (server["name"], started,
                                    "" if server["enabled"] else "   [switched off]"))
        for tool in server["tools"]:
            # A CLASS IN BRACKETS IS A PROPOSAL, a bare one is a decision. The
            # difference is the whole stage, so it has to survive into the line
            # somebody actually reads.
            klass = tool["class"] or "(%s)" % tool["proposed"]
            # COLLAPSED FIRST, SLICED SECOND. A foreign description is written
            # by a stranger and Cloudflare's are written over several lines with
            # tabs in them -- found in the live run, 2026-08-22. Cutting one
            # straight into a column printed the remainder underneath it, out of
            # line, and a row that is sometimes two rows is not a table.
            first = " ".join(tool["description"].split()).split(". ")[0].rstrip(".")
            lines.append("  %s %-30s %-12s %s"
                         % ("[x]" if tool["included"] else "[ ]",
                            tool["name"], klass, first[:64]))
        lines.append("  %d characters in every prompt" % server["cost"])
    if view["problems"]:
        lines.append("")
        lines.extend("! " + problem for problem in view["problems"])
    lines.append("")
    lines.append(MCP_USAGE)
    return "\n".join(lines)


def mcp_command(argv: list) -> str:
    """`/mcp` and its three forms, answered once for both surfaces."""
    argv = [str(a) for a in (argv or [])]
    if not argv:
        return mcp_listing()
    verb, rest = argv[0].lower(), argv[1:]

    if verb == "add" and rest:
        name, view, problem = mcp_add_line(" ".join(rest))
        return ("error: " + problem) if problem else mcp_installed(view, name)

    if verb == "auth" and len(rest) == 1:
        problem = mcp_authorise_server(rest[0])
        return ("error: " + problem) if problem else mcp_installed(mcp_view(), rest[0])

    if verb == "fetch" and len(rest) == 1:
        problem = mcp_refresh_server(rest[0])
        return ("error: " + problem) if problem else mcp_installed(mcp_view(),
                                                                  rest[0])

    if verb == "use" and len(rest) == 3:
        name, tool, klass = rest
        problem = mcp_confirm(name, {tool: {"included": True, "class": klass}})
        if problem:
            return "error: " + problem
        return "%s is taken, as %s.\n%s" % (mcp_tool_name(name, tool), klass,
                                            MCP_COST_NOTE)

    if verb == "drop" and len(rest) == 2:
        name, tool = rest
        # NO `class` KEY: dropping a tool is not un-deciding what it does.
        problem = mcp_confirm(name, {tool: {"included": False}})
        if problem:
            return "error: " + problem
        return "%s is out of the tool list.\n%s" % (mcp_tool_name(name, tool),
                                                    MCP_COST_NOTE)

    return "that is not a form of /mcp.\n" + MCP_USAGE


# READ AT IMPORT, ONCE, FROM DISK. Not from a server -- see the top of the E2
# section. It sits HERE rather than up there because `mcp_apply` wires
# `_mcp_caller` into `TOOL_IMPL`, and a name is only resolvable once it exists.
MCP_PROBLEMS = mcp_apply()

# Nothing else in this client owns a child process, so nothing else had to do
# this. An interpreter that exits without it leaves the servers running.
atexit.register(forget_mcp_servers)


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
SLASH_COMMANDS = ("/help", "/tools", "/mcp", "/mode", "/model", "/reasoning",
                  "/thoughts", "/reset", "/context", "/exit", "/quit")


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
    # #128: THE STAGED MEMORY WRITES GO WITH THEM, from in here rather than
    # from eight call sites. A write the review proposed belongs to the chat
    # that produced it exactly as much as a standing approval does, so the two
    # have one lifetime and one place that ends it. A second call beside each of
    # the eight would be eight chances to forget one, and the forgotten one
    # would carry a note out of a conversation the user has already dropped.
    forget_pending()
    # AND EVERY WAITING QUESTION, as `cancel`. A server blocked on an
    # answer from a conversation that has ended is a tool call hanging
    # until its own timeout, in a chat nobody is looking at.
    forget_asks()


# #128. THE MEMORY GATE -- what the background review wants to write, held back
# until a person says yes.
#
# WHY THIS IS NOT A `MODE_ASKS` CLASS. The three levels gate calls the MODEL
# makes inside a turn: `run_turn` reaches one, asks the surface through
# `approve`, and the user is sitting there because their answer is what the turn
# is waiting on. The review is the opposite case in every respect -- it runs
# BEHIND the visible end of the turn, `run_turn` does not know it exists, and
# `review_turn`'s own comment says why a question there is useless: "the user is
# not at the keyboard for a background pass, so a write or a shell command here
# would have nobody to refuse it".
#
# So this gate does not ask. It STAGES, and the surface raises a state the user
# meets whenever they next look. A question needs someone in the chair; a state
# waits.
#
# NOTHING IS EVER WRITTEN BY A TIMER, and that is the difference between a gate
# and a delay. An entry nobody approves EXPIRES and is dropped. Expiry has to
# fall on the side of not writing, or the gate is a speed bump.
#
# ON BY DEFAULT (robin, 2026-08-22), against this file's own precedent, and the
# reason the precedent loses here. `DEFAULT_MODE` is `auto` because every
# release before it ran the tools unasked and a commit that adds a CHOICE must
# not change what an existing session does -- a good rule, and it was the first
# answer here too. It does not hold for this one: a gate that ships off is not a
# gate, it is a setting nobody finds. What it guards is the one writer in this
# client that runs with nobody at the keyboard and writes into the head of every
# later session, and the whole reason it exists is that "the review writes
# unasked" was the behaviour worth changing. `--no-memory-approval` is one word
# away for anyone who wants the old shape back.
MEMORY_APPROVAL_DEFAULT = True

# How long a staged entry waits. Hermes uses 300 s for elicitation and the
# reasoning transfers: long enough that an answer noticed on the way back from
# the kettle still counts, short enough that a note nobody looked at does not
# surface days later out of a conversation that has since ended.
#
# CHECKED WHEN SOMEBODY LOOKS, NOT BY A THREAD. A timer would be a second thing
# in this client that can write to memory, and the entire point of the gate is
# that there is exactly one.
PENDING_TTL = 300.0

# Per session, never written to disk -- the same shape and the same reason as
# `_ALLOWED` above. A staged write that survived a restart would be a decision
# the user made in a conversation they have already forgotten.
_PENDING: "list[dict]" = []
_PENDING_SEQ = 0


def _pending_summary(name: str, arguments: str) -> str:
    """One line naming what WOULD be written, in the note's words not the API's.

    THE CONTENT, NOT THE CALL. `memory(action=add, target=memory)` is a
    keystroke; the sentence that would stand in the head of every later session
    is a decision. Same reasoning that put the arguments into the terminal's
    approval prompt (#88 point 2) -- a prompt that does not show what it
    releases is not a question.
    """
    try:
        args = json.loads(arguments or "{}")
    except (json.JSONDecodeError, AttributeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    action = str(args.get("action") or "?")
    where = str(args.get("target") or args.get("name") or name)
    body = args.get("content") or args.get("old_text") or args.get("description") or ""
    body = " ".join(str(body).split())
    if len(body) > 160:
        body = body[:157] + "..."
    return "%s %s: %s" % (action, where, body) if body else "%s %s" % (action, where)


def stage_memory(name: str, arguments: str) -> dict:
    """Hold one write until a person answers. Returns the staged entry."""
    global _PENDING_SEQ
    _PENDING_SEQ += 1
    try:
        args = json.loads(arguments or "{}")
        action = str((args or {}).get("action") or "add")
    except (json.JSONDecodeError, AttributeError):
        action = "add"
    entry = {"id": _PENDING_SEQ, "name": name, "arguments": arguments,
             # #128. THE ACTION IS CARRIED SEPARATELY from the summary, because
             # the window counts with it: `add` is a line gained, `remove` a
             # line lost, `replace` is both at once. A surface that had to parse
             # that back out of the summary string would be reading its own
             # prose, and the prose is for people.
             "action": action,
             "summary": _pending_summary(name, arguments),
             "staged": time.monotonic()}
    _PENDING.append(entry)
    return entry


def pending_memory() -> "list[dict]":
    """What is still waiting.

    EXPIRED ENTRIES ARE DROPPED HERE, on the read, and therefore never run. Any
    caller that is about to show the state or act on it passes through this
    function first, so there is no path on which an expired write survives long
    enough to be approved.
    """
    now = time.monotonic()
    _PENDING[:] = [e for e in _PENDING if now - e["staged"] < PENDING_TTL]
    return list(_PENDING)


def pending_view() -> "list[dict]":
    """What the surfaces draw: the action and the text, nothing else.

    ONE SHAPE FOR BOTH SURFACES and neither of them reaches into the entry. The
    window counts lines gained and lost off `action`; the terminal prints
    `text`. `arguments`, `id` and the staged clock are this module's business.
    """
    return [{"action": e["action"], "text": e["summary"]}
            for e in pending_memory()]


def approve_pending(ident: "int | None" = None) -> "list[str]":
    """Run what was staged. `None` means all of it. Returns what was saved.

    THE SAME `run_tool` THE REVIEW WOULD HAVE CALLED. An approved write and an
    ungated write are one code path, so the character cap, the duplicate check
    and the injection scan all still answer -- the gate adds a question, it does
    not add a second way to write.
    """
    ready = [e for e in pending_memory() if ident is None or e["id"] == ident]
    saved = []
    for entry in ready:
        _PENDING.remove(entry)
        try:
            result = json.loads(run_tool(entry["name"], entry["arguments"]))
        except Exception:                   # noqa: BLE001 - failure is silence, as in review_turn
            continue
        if result.get("success") and result.get("action"):
            saved.append("%s %s" % (result["action"],
                                    result.get("target") or result.get("name")
                                    or entry["name"]))
    return saved


def decline_pending(ident: "int | None" = None) -> int:
    """Drop what was staged, writing nothing. `None` means all of it."""
    doomed = [e for e in pending_memory() if ident is None or e["id"] == ident]
    for entry in doomed:
        _PENDING.remove(entry)
    return len(doomed)


def forget_pending() -> None:
    """Drop every staged write. Called where `forget_approvals` is called, and
    for the same reason: `/reset` and the window's new-chat button are where a
    session actually ends."""
    _PENDING.clear()


# ---------------------------------------------------------------- E6 ------
# ELICITATION: THE SERVER ASKS, A PERSON ANSWERS, AND THE SERVER NEVER DRAWS.
#
# This was refused outright until 2026-08-22, and the reason was real: it is the
# only place in the protocol where a foreign server puts words in front of a
# human who then acts on them. What changed is that the risk turned out to be
# SEPARABLE, and Hermes had already separated it -- form mode through the
# client's own approval surface, URL mode declined as unsupported.
#
# THE SPLIT IS THE WHOLE DESIGN:
#
#   * what the server sends is a SCHEMA, not a rendering. Crow draws the fields
#     from `requestedSchema` itself, so there is no markup, no link and no
#     button that came off the wire;
#   * the subset is deliberately tiny -- a flat object of primitives. Anything
#     nested, an array, a `$ref`, a URL mode, or a schema with no properties at
#     all is DECLINED with a reason. That refusal is what covers every future
#     mode nobody has read yet;
#   * every string that reaches a screen goes through the tag filter first, the
#     same one tool descriptions get;
#   * the answer is validated against the schema the person was shown, so a
#     server cannot get back a field it never asked for.
#
# AND IT BLOCKS, on purpose. The tool call that triggered it is waiting, the
# server is waiting, and the person is the only one who can move it. That is the
# opposite of the OAuth rule one file above -- and the difference is consent:
# nobody asked for a browser mid-turn, and this server was asked to do something
# by the model in this very turn.

# The five shapes a field may have. A `format` is read and then ignored: it is a
# hint for a nicer input, never a reason to refuse a schema.
ELICIT_TYPES = ("string", "number", "integer", "boolean")

# How many fields a form may carry. A server that wants more than this is not
# asking a question, it is handing over a configuration screen -- and a screen
# nobody reads is a screen everybody confirms.
ELICIT_FIELDS = 12

# Seconds a question stands. Hermes uses 300 and the reasoning transfers: long
# enough that somebody coming back to the machine still answers it, short enough
# that a tool call does not hang for an afternoon. A timeout is `cancel`, which
# the specification defines as dismissed without a choice -- and it is NOT
# `decline`, because the person never said no.
ELICIT_TTL = 300.0

_ASKS: "list[dict]" = []
_ASKS_SEQ = 0
_ASKS_LOCK = threading.Lock()

# What a surface installs so it can draw the question the moment it is asked.
# ONE PLUG PER SURFACE AND NO SECOND GATE: the core still owns the staging, the
# waiting, the validation and the answer. This only says "somebody is asking
# now", because a terminal has to prompt in line and a window has to push.
ELICIT_ANNOUNCE = None


def elicit_fields(schema) -> "tuple[list, str | None]":
    """The restricted subset out of `requestedSchema`, or why it is refused.

    THE REFUSAL IS THE SECURITY BOUNDARY, not the parsing. Everything this
    function does not understand is declined by name -- which is how a mode that
    does not exist yet, a nested object that would need a layout, and a `$ref`
    that would need fetching all end up in the same safe place.
    """
    if not isinstance(schema, dict):
        return [], "the server sent no requestedSchema"
    if schema.get("type") != "object":
        return [], ("this client answers a flat object of primitives, and that "
                    "schema is %r" % strip_tag_characters(str(schema.get("type")))[:40])
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return [], "that schema asks for nothing"
    if len(properties) > ELICIT_FIELDS:
        return [], ("that schema asks for %d values, and this client draws at "
                    "most %d" % (len(properties), ELICIT_FIELDS))
    required = schema.get("required")
    required = [str(r) for r in required] if isinstance(required, list) else []

    fields = []
    for name in properties:
        spec = properties[name]
        if not isinstance(spec, dict):
            return [], "the field %r has no description" % strip_tag_characters(str(name))[:40]
        kind = spec.get("type")
        if kind not in ELICIT_TYPES:
            return [], ("the field %r is a %r, and this client answers only %s"
                        % (strip_tag_characters(str(name))[:40],
                           strip_tag_characters(str(kind))[:40],
                           ", ".join(ELICIT_TYPES)))
        # AN ENUM IS A LIST OF STRINGS AND NOTHING ELSE. A number enum would
        # render fine and come back as text, and the mismatch would be the
        # server's problem to discover at the worst moment.
        choices = spec.get("enum")
        if choices is not None:
            if (kind != "string" or not isinstance(choices, list) or not choices
                    or not all(isinstance(c, str) for c in choices)):
                return [], ("the field %r offers choices this client cannot draw"
                            % strip_tag_characters(str(name))[:40])
            choices = [strip_tag_characters(c)[:80] for c in choices][:ELICIT_FIELDS * 2]
        fields.append({
            "name": str(name),
            "type": kind,
            # EVERY STRING THROUGH THE FILTER. A title is prompt-head text
            # written by a stranger exactly as a tool description is, except
            # this one is read by a person rather than a model.
            "title": strip_tag_characters(str(spec.get("title") or name))[:80],
            "description": strip_tag_characters(str(spec.get("description") or ""))[:200],
            "enum": choices,
            "required": str(name) in required,
        })
    return fields, None


def stage_elicitation(server: str, message: str, fields: list) -> dict:
    """Hold one question until a person answers it."""
    global _ASKS_SEQ
    with _ASKS_LOCK:
        _ASKS_SEQ += 1
        entry = {"id": _ASKS_SEQ, "server": server, "message": message,
                 "fields": fields, "staged": time.monotonic(),
                 "action": None, "content": None,
                 "answered": threading.Event()}
        _ASKS.append(entry)
    return entry


def pending_asks() -> "list[dict]":
    """What is still being asked. Expired questions are dropped on the read, the
    same construction `pending_memory` uses and for the same reason."""
    now = time.monotonic()
    with _ASKS_LOCK:
        _ASKS[:] = [e for e in _ASKS if now - e["staged"] < ELICIT_TTL]
        return list(_ASKS)


def elicit_view() -> "list[dict]":
    """What the surfaces draw. ONE SHAPE FOR BOTH, and neither reaches into the
    entry: the event and the clock are this module's business."""
    return [{"id": e["id"], "server": e["server"], "message": e["message"],
             "fields": e["fields"]} for e in pending_asks()]


def _elicit_value(field: dict, given):
    """One answer, coerced to the type the person was shown, or `None`.

    STRICT, BECAUSE THE SERVER TRUSTS THIS. It declared a boolean; handing it
    the string "false" -- which is true in most languages that will read it --
    is worse than handing it nothing.
    """
    kind = field["type"]
    if kind == "boolean":
        if isinstance(given, bool):
            return given
        if isinstance(given, str) and given.strip().lower() in ("true", "false"):
            return given.strip().lower() == "true"
        return None
    if kind in ("number", "integer"):
        try:
            number = float(str(given).strip())
        except (TypeError, ValueError):
            return None
        if kind == "integer":
            return int(number) if number == int(number) else None
        return number
    text = "" if given is None else str(given)
    if field.get("enum") is not None and text not in field["enum"]:
        return None
    return text


def answer_elicitation(ident: int, action: str, content=None) -> "str | None":
    """What a person decided, checked against the schema they were shown.

    THE SERVER GETS BACK ONLY WHAT IT ASKED FOR. A surface that passed extra
    keys through would let whatever filled that form reach a foreign process,
    and the form is the one thing on screen a stranger wrote the labels for.
    """
    if action not in ("accept", "decline", "cancel"):
        return "%r is not accept, decline or cancel" % strip_tag_characters(str(action))[:40]
    entry = next((e for e in pending_asks() if e["id"] == ident), None)
    if entry is None:
        return "that question is no longer waiting"

    kept = {}
    if action == "accept":
        given = content if isinstance(content, dict) else {}
        for field in entry["fields"]:
            raw = given.get(field["name"])
            # ABSENT AND WRONG ARE NOT THE SAME ANSWER. An optional field left
            # empty is a decision and travels as nothing; a field somebody
            # filled in with something the schema does not allow is a mistake,
            # and dropping it silently would send the server a form that looks
            # like it was answered.
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                if field["required"]:
                    return "%s is needed" % field["title"]
                continue
            value = _elicit_value(field, raw)
            if value is None:
                if field.get("enum") is not None:
                    return "%s must be one of: %s" % (field["title"],
                                                      ", ".join(field["enum"]))
                return "%s is not a %s" % (field["title"], field["type"])
            kept[field["name"]] = value

    with _ASKS_LOCK:
        if entry in _ASKS:
            _ASKS.remove(entry)
    entry["action"] = action
    entry["content"] = kept
    entry["answered"].set()
    return None


def forget_asks() -> None:
    """Release every waiting question as `cancel`. Called where the session
    ends, because a server blocked on an answer nobody will ever give is a tool
    call that hangs until its own timeout."""
    with _ASKS_LOCK:
        waiting, _ASKS[:] = list(_ASKS), []
    for entry in waiting:
        entry["action"] = "cancel"
        entry["content"] = {}
        entry["answered"].set()


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
# #120 PUTS `memory` HERE, and it is the same reason `run_command` is: the
# result is not a function of the arguments. `add` the same entry twice must be
# able to answer "no duplicate" the second time, and `remove` then `add` of one
# entry are two identical calls with two different correct answers. Answering
# the second from the first would turn a correction into a silent no-op.
NEVER_CACHED = frozenset({"run_command", "memory", "skill"})
READ_GATED = frozenset({"write_file", "edit_file"})


def _cache_key(name: str, arguments: str) -> tuple | None:
    """What this call's result depends on, or None when it depends on too much.

    None means "do not cache", NOT "cache miss": the caller must not write the
    result back either, or the next identical call is answered from a key that
    was never a promise about anything.
    """
    # EVERY MCP TOOL IS `run_command`'S CASE, and it is not in the set above
    # because the set cannot hold it: the names arrive from `mcp.json` at import,
    # `NEVER_CACHED` is a frozenset, and rebinding it would leave `crow.py`
    # holding the old one -- it imports the VALUE. A prefix test also holds for a
    # server added after this line was written, which a list would not.
    #
    # The reason is the one `run_command` carries: the result is not a function
    # of the arguments. A foreign process asked the same thing twice may answer
    # twice on purpose -- creating one issue and then a second one is two calls --
    # and answering the second from the first is a write the model is told
    # happened while nothing did.
    if name in NEVER_CACHED or name.startswith("mcp_"):
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


# ---------------------------------------------------------------- #122 -----
# THE BACKGROUND REVIEW. Twice per window -- see `MEMORY_REVIEW_AT` -- the model
# is asked to read the conversation so far and decide what a future session
# would need from it. Curation is the hard half of a bounded memory: a model
# that only saves when it happens to think of it mid-answer saves almost
# nothing, and one asked after every turn saves the turn.
#
# IT RIDES THE PREFIX IT WAS JUST GIVEN. llama-server reuses a prompt by common
# token prefix, so replaying the finished conversation plus one short question
# is a cache hit up to the question -- and the NEXT user turn matches that same
# conversation again, because the review sits behind it. Cost is therefore one
# short prefill plus whatever the review decides to write, not a second pass.
#
# WHICH IS A DERIVATION FROM `server-context.cpp`, NOT A MEASUREMENT. On a
# single slot (`-np 1`) the review holds the server while it runs, and nobody has
# yet timed how long that is or whether a fast typist waits behind it. The
# measurement is owed before this ships and is written down in #122.
#
# THE TOOL LIST IS SENT WHOLE, NOT NARROWED TO `memory`. Narrowing it looks
# obviously right and would throw away the entire saving above: `tools` is
# rendered into the HEAD of the prompt, so a shorter list is a different byte 0
# and the whole conversation would be re-read. The restriction is stated in the
# question instead, and enforced here by ignoring every call that is not
# `memory`.
MEMORY_REVIEW_PROMPT = (
    "[system] Read the WHOLE conversation above, not just the last exchange. It "
    "has already been shown to the user; nothing you write now reaches them. "
    "Decide what a future session would need to know at its very first turn.\n"
    "SAVE, at most two or three entries: a decision and the reason for it; a "
    "convention, command or path that turned out to be the right one; a "
    "correction the user made; a constraint they stated; a trap that cost time "
    "and how it was avoided.\n"
    "DO NOT SAVE: the question or the answer; anything one read of a file would "
    "tell you again; a restatement of code; anything true only inside this "
    "conversation; progress, plans or what you are about to do next.\n"
    "Prefer ONE dense entry over three thin ones -- merge related facts into a "
    "single line. If an existing entry already covers it, use `replace` to "
    "sharpen that one instead of adding beside it. The store is small and is "
    "never trimmed for you.\n"
    "SEPARATELY, AND USUALLY NOT: if this conversation worked out a repeatable "
    "WAY of doing something -- an order of steps, the flags that worked, the "
    "check that catches the usual mistake -- save it with the `skill` tool "
    "instead. A skill is what to DO; memory is what is TRUE. Do not save a "
    "summary of this conversation as a skill, and do not save a procedure you "
    "have not actually seen work here.\n"
    "Saying nothing is the normal outcome and the right one whenever you are "
    "unsure: an entry you regret costs every future session, an entry you "
    "skipped costs nothing. If nothing qualifies, call nothing and reply with "
    "the single word NOTHING. Call no tool other than `memory`."
)


def review_turn(conversation: "Conversation", *, base_url: str, model: str,
                api_key: str, temperature: float, top_p: float, min_p: float,
                top_k: int | None = None, reasoning_effort: str | None = None,
                timeout: float = 180.0, gate: bool = False,
                events: "TurnEvents | None" = None) -> "list[str]":
    """Ask once whether this turn left anything worth keeping. Returns what was saved.

    IT IS CALLED AFTER THE TURN IS OVER ON SCREEN, never inside it. It sat
    inside `run_turn` for one afternoon and robin found it live on 2026-08-21:
    the answer was complete, the cost line never came and the composer still
    said `Stop`, because the turn does not end until this returns -- and at
    `high` it thinks about a 20k conversation first. What a person waits for is
    the answer; the review is something that happens afterwards.

    `events` FIRES PER SAVED ENTRY, IN THE LOOP, not once at the end -- robin,
    same evening. The return value still carries everything, for the caller that
    wants the total rather than the moments.

    NOTHING IT DOES REACHES THE CONVERSATION. The question and the answer are
    built into a throwaway list and dropped; appending them would put the review
    into the history, which would move the head of every later turn and cost the
    prefix this whole design protects.

    IT NEVER RAISES. A review that takes a finished turn down with it would turn
    a working answer into an error after the user has already read it. Every
    failure here is silence.
    """
    if len(conversation) < 2:
        return []
    messages = conversation.payload() + [{"role": "user",
                                          "content": MEMORY_REVIEW_PROMPT}]
    body = {"model": model, "messages": messages, "tools": TOOLS, "stream": False,
            "temperature": temperature, "top_p": top_p, "min_p": min_p}
    if top_k is not None:
        body["top_k"] = top_k
    if reasoning_effort:
        body["chat_template_kwargs"] = {"reasoning_effort": reasoning_effort}
    url = f"{base_url.rstrip('/')}/chat/completions"
    try:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            answer = json.loads(resp.read().decode("utf-8") or "{}")
        calls = (answer.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
    except Exception:                       # noqa: BLE001 - see the docstring
        return []
    saved, staged = [], []
    for call in calls:
        function = call.get("function") or {}
        # THE TWO THIS PASS MAY CALL, and nothing else. Anything further is a
        # model that did not read the question, and it is dropped rather than
        # run: the user is not at the keyboard for a background pass, so a write
        # or a shell command here would have nobody to refuse it.
        if function.get("name") not in ("memory", "skill"):
            continue
        # #128. STAGED, NOT WRITTEN. The gate cannot ask here -- see
        # MEMORY_APPROVAL_DEFAULT for why a question in a background pass is a
        # question put to an empty chair. What it can do is leave the write
        # where the user will meet it.
        if gate:
            staged.append(stage_memory(function["name"],
                                       function.get("arguments") or "{}"))
            continue
        try:
            result = json.loads(run_tool(function["name"],
                                         function.get("arguments") or "{}"))
        except Exception:                   # noqa: BLE001
            continue
        if result.get("success") and result.get("action"):
            one = "%s %s" % (result["action"],
                             result.get("target") or result.get("name")
                             or function["name"])
            saved.append(one)
            # The moment it is on disk, not the moment the pass is done.
            if events is not None:
                events.memory_saved([one])
    # ONE REPORT FOR THE WHOLE PASS, unlike `memory_saved` which fires per entry
    # at the moment of writing. Nothing has happened yet here: what the surface
    # raises is a state ("something is waiting"), and a state does not need to
    # be told twice.
    if staged and events is not None:
        events.memory_pending([e["summary"] for e in staged])
    return saved


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

    def memory_pending(self, what: "list[str]") -> None:
        """#128. The review wants to write and the gate held it. Nothing is on
        disk yet.

        A STATE, NOT AN EVENT, and the surfaces draw it that way: `memory_saved`
        reports something that happened and is over, so the window's line glows
        once and settles. This one reports something that is still true and will
        stay true until the user answers or it expires -- so it breathes, the
        same grammar the recording microphone already uses.

        AND IT HAS TO BE ACTIONABLE. A state nobody can act on is furniture with
        a colour (robin, on the microphone, and it holds here): whatever the
        surface raises has to be the thing you press to see the entries.
        """

    def memory_saved(self, what: "list[str]") -> None:
        """#122. The background review saved something, and this is how anyone knows.

        NOT OPTIONAL AND NOT SWITCHABLE. There is no write-approval gate here --
        robin declined it on 2026-08-21 -- so this line is the ONLY moment a
        person finds out that something entered the head of their next session.
        A silent learner is a system nobody can correct.

        FIRED ONLY WHEN SOMETHING WAS WRITTEN. The review runs after every turn
        and saves after almost none of them; a line saying "nothing to remember"
        after each answer would be noise that teaches the reader to skip the one
        line that matters.
        """


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
