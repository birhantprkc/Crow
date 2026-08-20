#!/usr/bin/env python3
"""E14: the one run against the SHIPPED operating point. Five probes, one block.

WHY THIS FILE EXISTS. Every [gemessen] mark in #90's plan comes from text
analysis, regex control, AST counting, package inspection or a serverless
checker run. NOT ONE behavioural statement about the shipped operating point
comes from a measurement of its own -- everything close to behaviour is quoted
from the n = 1 measuring head of #90 (19.39 tok/s, ttft 7.15 s, answer 281.52 s).
That is consistent, because server time needs robin's consent; it also means the
whole acceptance side of the epic is unrun. Spread over six stages it would ask
for consent six times, so it stands here: ONE BLOCK, ONE CONSENT, FIVE ANSWERS.

WHAT IT WILL NOT DO. Without --consent nothing is sent. The default prints the
plan, the gate and the price and exits 2 with "nothing was measured" -- a tool
that measured on being run would be a tool that spends someone else's server
time on a typo. --dry-run prints the same and exits 0, because printing the plan
is what it was asked for.

AND IT DOES NOT START A SERVER. The block is run against the point the installer
ships and the machine is already running; a server this tool started would be a
hand-started point, which is the one thing the plan rules out by name. If none is
running, the gate is red and nothing is sent.

THE ORDER IS FIXED: iii -> iv -> i -> ii -> v. Not preference -- a block that
breaks off after the third probe has spent server time and closed no question, so
the two recordings go first: they are the cheapest, and their answers decide
which of E12's two F7 fixtures describes the wire.

THE RETURN PATH IS PART OF THE STAGE, not a precaution. Every probe writes slot 0
and the session file, so `session.json` and `crow-session.bin` are copied out of
the session directory BEFORE the first byte and put back afterwards -- in a
`finally`, so a probe that raises still gives the session back. The slot file is
the server's KV state and can be gigabytes; its size is printed before it is
copied, because that copy is the most expensive thing this tool does that is not
a turn.

THE GATE BEFORE THE FIRST BYTE, and one half of it is the plan's own line: count
the process list. A second llama-server means the block measures the pair rather
than the point, and no counter in the system says so. The other half is stronger
than the plan asks for and costs nothing: the RUNNING process's command line is
held against manifests/operating-point.json with the very code
tools/check_operating_point.py holds README.md and install.ps1 to. A server
started by hand with -np 4 or a different cache passes "one process is running"
and fails here, which is exactly the difference between the shipped point and a
point that looks like it.

WHAT PROBE (i) CAN FAIL ON THAT IS NOT P1, and it belongs at the top because the
two look identical in the number that comes out:

  * `Api.send` RETURNS SILENTLY while a worker is alive (cli/crow_gui.py). So
    the second question -- the one whose answer is the whole proof -- may never
    be sent at all, and the seconds tick by on a question the server never
    heard. Probe (i) records whether the send was refused, because "no answer
    for 60 s" and "no question for 60 s" are the same measurement otherwise.
    (The order in `send` is the good one: refused BEFORE `INTERRUPT.clear()`, so
    a user typing straight after an abort cannot clear the flag out from under
    the reader that has not seen it yet. The Tk build cleared it first.)
  * the aborted turn's `{"k": "idle"}` reaches the page while the NEXT turn is
    streaming -- the send button reads "send" over an answer still arriving.
    Probe (i) watches for it and writes it down. It is not fixed here: E14
    measures the shipped window, it does not change it.

WHAT THIS BLOCK NO LONGER SEES. E12 made the window a webview, so `Window` below
drives `Api` and reads the message queue -- the whole shipped client path, minus
the page's own JavaScript. Two checks the Tk build made have no subject here and
are reported as NOT MEASURED rather than as held: folding a thought block open
again, and typing into the input while a turn runs.

Usage:
  run_server_block.py                     the plan, the gate, and why nothing ran
  run_server_block.py --dry-run           the same, as an answer (0 on a green gate)
  run_server_block.py --consent           the block, in the fixed order
  run_server_block.py --consent --only i  one probe again, for an INVALID run

Exit 0 = every selected probe answered its question (or: a dry run over a green gate).
     1 = at least one question is still open.
     2 = setup error: no consent, a red gate, a missing toolkit. Nothing was sent.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "cli"))
sys.path.insert(0, HERE)

import crow                      # noqa: E402  the VERSION and the CLI's own door
import crow_core                 # noqa: E402
import check_operating_point as point   # noqa: E402


# --------------------------------------------------------------- the block ---

# THE ORDER, AND IT IS THE PLAN'S. iii and iv are recordings and cost one short
# turn each; i and ii cost a long turn, fifty aborts and two CLI turns; v is the
# pass over all seven capabilities. Cheapest first is not tidiness here -- (iii)
# decides which F7 fixture in cli/test_crow_gui.py describes the wire, and (iv)
# produces the prompt_n numbers the later probes are read against.
PROBE_ORDER = ("iii", "iv", "i", "ii", "v")

# One line per probe, and it is the QUESTION rather than the activity: a probe
# whose line describes what it does can be run and still answer nothing.
PROBE_ONELINE = {
    "iii": "timings on every chunk, or only on the last?",
    "iv": "what does a client switch cost at one slot?",
    "i": "does the abort free slot 0, and does the race hold?",
    "ii": "does the session survive the door it is carried through?",
    "v": "do the seven capabilities hold against this server?",
}

QUESTIONS = {
    # ASCII on purpose: this text travels through a piped stdin into
    # cli/crow.py on a Windows console whose code page is not this file's.
    "word": "Antworte mit genau einem Wort: OK",
    "long": "Erklaere den Prefix-Cache in llama.cpp so ausfuehrlich wie moeglich,"
            " mit Beispielen und Randfaellen.",
    "code": "Antworte nur mit einem Python-Codeblock, der genau print(\"hi\")"
            " enthaelt. Kein Text davor oder danach.",
    "prefix": "Nenne einen Grund, warum ein Prefix-Cache bricht.",
}

# F4's band, and both edges are the plan's. The derivation is one number that
# already exists: ttft of a one-word question against a FRESH server is 7.15 s in
# #90's measuring head (n = 1). Under 30 s the abort took; over 60 s the old turn
# is still holding slot 0; between them the run decides nothing and is repeated
# rather than read -- the first draft of the plan said "seconds" against
# "minutes" and left a band in which the case neither passes nor fails.
ABORT_GRIP_S = 30.0
ABORT_STUCK_S = 60.0
TTFT_REFERENCE_S = 7.15

# The race before the first byte. FIFTY IS A FLOOR, NOT A DERIVATION, and the
# plan says so in the same breath: repeat until the race has been hit once, and
# at least this often.
RACE_FLOOR = 50

# F1 in the live pass. A fixture case can fix N chunks and demand k states; a
# live turn cannot, so the floor is derived from the two numbers that bound it:
# the window draws at TICK_MS = 33 (30 ticks a second) and the point decodes at
# about 19 tok/s, so an answer of 20 tokens has room for ~20 states and a window
# that filled once at the end has exactly 1. Five is far under the first and far
# over the second.
STATE_FLOOR = 5

ANSWERED, OPEN, INVALID = "ANSWERED", "OPEN", "INVALID"


class SetupError(RuntimeError):
    """A reason not to send anything. Exit 2, and nothing was spent."""


class Finding:
    """One probe's answer: a number, a verdict, and the run it came out of."""

    def __init__(self, probe: str, question: str) -> None:
        self.probe = probe
        self.question = question
        self.verdict = OPEN
        self.headline = ""
        self.numbers: dict = {}
        self.lines: list[str] = []
        self.run = ""
        self.seconds = 0.0

    def say(self, text: str) -> None:
        self.lines.append(text)

    def answer(self, headline: str, **numbers) -> "Finding":
        self.verdict = ANSWERED
        self.headline = headline
        self.numbers.update(numbers)
        return self

    def open(self, headline: str, **numbers) -> "Finding":
        self.verdict = OPEN
        self.headline = headline
        self.numbers.update(numbers)
        return self

    def invalid(self, headline: str, **numbers) -> "Finding":
        self.verdict = INVALID
        self.headline = headline
        self.numbers.update(numbers)
        return self


# ----------------------------------------------------------------- the gate --

_PS_QUERY = ("Get-CimInstance Win32_Process -Filter \"Name like 'llama-server%'\""
             " | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")


def _run_process_query() -> str:
    """Ask the operating system for every llama-server it is running."""
    if platform.system() == "Windows":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_QUERY]
    else:
        argv = ["ps", "-eo", "pid=,args="]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError("cannot read the process list: %s" % exc) from exc
    if proc.returncode != 0:
        raise SetupError("the process list came back with %d: %s"
                         % (proc.returncode, (proc.stderr or "").strip()[:200]))
    return proc.stdout


def server_processes(query=None) -> list[tuple[str, str]]:
    """Every running llama-server as (pid, command line).

    The query is a parameter so the negative control can hand this function a
    machine with two servers on it without starting one.
    """
    text = (query or _run_process_query)()
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "llama-server" not in line:
            continue
        pid, _, rest = line.partition("\t")
        if not rest:
            pid, _, rest = line.partition(" ")
        out.append((pid.strip(), rest.strip()))
    return out


def process_gate(servers: list[tuple[str, str]]) -> list[str]:
    """The plan's own precondition, and the reason it is the FIRST thing here."""
    if not servers:
        return ["no llama-server is running. There is nothing to measure, and a"
                " server started by this tool would be a hand-started point --"
                " which is what the stage rules out by name."]
    if len(servers) > 1:
        return ["%d llama-server processes are running (pids %s). The block would"
                " measure the PAIR and not the operating point, and no counter in"
                " the system says so."
                % (len(servers), ", ".join(pid for pid, _ in servers))]
    return []


def command_line_gate(command_line: str, want: dict) -> list[str]:
    """The running server's own command line, against the manifest.

    THE SAME CODE THE README AND THE INSTALLER ARE HELD TO. `point.compare`
    anchors on the binary name and reads the flags out of the raw text; handing
    it the LIVE command line is the only check in this repository that says the
    process on this machine is the point the installer ships, rather than a
    process with the same name.
    """
    if not (command_line or "").strip():
        return ["the running server's command line came back empty. Windows only"
                " hands it out for processes of the same user, so this block"
                " cannot tell the shipped point from a hand-started one. Run it"
                " from the session that started llama-server."]
    _got, problems = point.compare("llama-server", command_line, want)
    return ["the running server differs from manifests/operating-point.json: %s" % p
            for p in problems]


def fetch_props(base_url: str, timeout: float = 5.0) -> dict:
    """/props as it stands, raw. `fetch_model_name` strips what this gate needs."""
    import urllib.request

    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    with urllib.request.urlopen(root + "/props", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")) or {}


def props_gate(props: dict, manifest: dict) -> list[str]:
    """What the server has OPEN, against what the release says it ships.

    Two different questions and both are asked: the model NAME (the display name
    the window's chip carries) and the QUANTISATION (the rung #89 moved to). A
    run against DeepSeek-V4-Flash-0731 at UD-IQ3_XXS would pass every other gate
    in this file and produce numbers for a point that is not shipped.
    """
    problems = []
    model = manifest.get("model") or {}
    settings = props.get("default_generation_settings") or {}
    path = ""
    for value in (props.get("model_path"), props.get("model"), settings.get("model")):
        if isinstance(value, str) and value.strip():
            path = value
            break
    if not path:
        return ["/props names no model. The block cannot say what it measured."]

    want_name = crow_core.model_display_name(model.get("first_shard") or "")
    got_name = crow_core.model_display_name(path)
    if want_name and got_name != want_name:
        problems.append("the server has %r open, the manifest ships %r"
                        % (got_name, want_name))
    quant = model.get("quant") or ""
    if quant and quant.lower() not in path.lower():
        problems.append("the open model is not at %s: %s" % (quant, path))

    # THE OPERATING POINT, EXPLICITLY. #111 turned `server` into a map of one
    # line per model key, and this block measures 0731 -- so it names the key it
    # means rather than taking whatever the map happens to yield first.
    want_ctx = (((manifest.get("servers") or {}).get("operating-point")) or {}).get("ctx")
    got_ctx = 0
    for value in (settings.get("n_ctx"), props.get("n_ctx")):
        try:
            if value and int(value) > 0:
                got_ctx = int(value)
                break
        except (TypeError, ValueError):
            continue
    if want_ctx and got_ctx:
        want = int(want_ctx)
        # LLAMA.CPP ROUNDS THE WINDOW UP, and the shipped point is a number it
        # rounds: `-c 200000` comes back from /props as 200192. README.md tells
        # the reader to expect exactly that. An equality check therefore called
        # every correctly started server wrong -- measured 2026-08-13, on the
        # first run of this block against a real server.
        #
        # AND THE FIXTURE COULD NOT SEE IT: shipped_props() answers with the
        # manifest's own number, which is the one value no server returns. That
        # is the shape of a suite measuring itself.
        #
        # WHAT THE GATE IS FOR is a MEASUREMENT server on the same machine --
        # 65536 or 16384, the windows the harness runs -- and a rounding of
        # under half a thousand tokens is not one. Anything below the manifest,
        # or more than one rounding above it, is still red.
        if not want <= got_ctx < want + 512:
            problems.append("n_ctx is %d, the manifest says %d" % (got_ctx, want))
    return problems


# ---------------------------------------------------------- the return path --

class SessionBackup:
    """The copy that is pulled BEFORE the block and put back after it.

    Not a precaution: no probe in here is idempotent -- every one of them writes
    slot 0 and the session file -- so this is the way back, and it is the reason
    the block may be run at all on a machine somebody is using.

    THE SLOT FILE CAN BE GIGABYTES. `save_session` writes the server's whole KV
    state, about 17 MiB plus ~6.9 KiB per token; the size is printed before the
    copy so the operator sees what the return path costs.
    """

    def __init__(self, session_dir: str, into: str) -> None:
        self.dir = session_dir
        self.into = into
        self.names = (os.path.basename(crow_core.SESSION_FILE), crow_core.SLOT_FILE)
        self.taken: dict[str, str | None] = {}

    def take(self, log=None) -> list[str]:
        os.makedirs(self.into, exist_ok=True)
        notes = []
        for name in self.names:
            src = os.path.join(self.dir, name)
            if not os.path.exists(src):
                self.taken[name] = None
                notes.append("%s does not exist yet -- it will be removed again" % name)
                continue
            size = os.path.getsize(src)
            if log:
                log("  copying %s (%s) out of the way" % (name, _bytes(size)))
            dst = os.path.join(self.into, name)
            shutil.copy2(src, dst)
            self.taken[name] = dst
            notes.append("%s copied, %s" % (name, _bytes(size)))
        return notes

    def put_back(self) -> list[str]:
        """Both halves back where they were. Reports what it did, per file."""
        notes = []
        for name in self.names:
            live = os.path.join(self.dir, name)
            kept = self.taken.get(name, "missing")
            if kept == "missing":
                notes.append("%s was never taken -- nothing to put back" % name)
                continue
            if kept is None:
                if os.path.exists(live):
                    os.remove(live)
                    notes.append("%s did not exist before the block; removed" % name)
                else:
                    notes.append("%s did not exist before the block, nor now" % name)
                continue
            shutil.copy2(kept, live)
            same = os.path.getsize(kept) == os.path.getsize(live)
            notes.append("%s restored, %s%s" % (name, _bytes(os.path.getsize(live)),
                                                "" if same else " -- SIZES DIFFER"))
        return notes


def _bytes(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % size


# ------------------------------------------------------------- the verdicts --

def count_timings(payloads: list[str]) -> tuple[int, int]:
    """(the grep count, the parsed count) over one recorded stream.

    BOTH, because the plan says `grep -c '"timings"'` and that counts the FIELD
    while the question is about the BLOCK. A chunk carrying `"timings": null` --
    which is what an endpoint that knows the field and has nothing to say sends --
    is counted by the grep and not by the parse. When the two disagree the report
    says so instead of quietly preferring one of them.
    """
    raw = sum(1 for p in payloads if '"timings"' in p)
    parsed = 0
    for payload in payloads:
        try:
            chunk = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(chunk, dict) and isinstance(chunk.get("timings"), dict):
            parsed += 1
    return raw, parsed


def timings_shape(parsed: int, predicted_n: int | None) -> tuple[str, str]:
    """Which of the two shapes the wire carries. (verdict, why)

    The two shapes are E12's two F7 fixtures: `timings` on the LAST chunk only,
    and `timings` on EVERY chunk. Anything between them is a third shape nobody
    wrote a fixture for, and it is reported as undecided rather than rounded to
    whichever is nearer.
    """
    if not predicted_n:
        return "undecided", "the server sent no predicted_n to hold the count against"
    if parsed <= 1:
        return ("final chunk only",
                "%d chunk carried a timings block against predicted_n %d"
                % (parsed, predicted_n))
    if parsed >= predicted_n:
        return ("every chunk",
                "%d chunks carried a timings block against predicted_n %d"
                % (parsed, predicted_n))
    return ("undecided",
            "%d chunks carried a timings block against predicted_n %d -- neither"
            " of the two shapes E12 wrote a fixture for" % (parsed, predicted_n))


def abort_verdict(seconds: float) -> tuple[str, str]:
    """F4's band, in one place. (verdict, sentence)"""
    if seconds < ABORT_GRIP_S:
        return ANSWERED, ("the next question answered in %.2f s, under the %.0f s"
                          " threshold -- the abort took" % (seconds, ABORT_GRIP_S))
    if seconds > ABORT_STUCK_S:
        return OPEN, ("the next question took %.2f s, over the %.0f s threshold --"
                      " the aborted turn was still holding slot 0"
                      % (seconds, ABORT_STUCK_S))
    return INVALID, ("the next question took %.2f s, between %.0f s and %.0f s --"
                     " the run decides nothing and is repeated"
                     % (seconds, ABORT_GRIP_S, ABORT_STUCK_S))


def race_verdict(runs: int, hits: int, leaked: int) -> tuple[str, str]:
    """The abort-before-the-first-byte repetition. (verdict, sentence)"""
    if runs < RACE_FLOOR:
        return OPEN, ("%d repetitions against a floor of %d -- the plan's minimum"
                      " was not reached" % (runs, RACE_FLOOR))
    if leaked:
        return OPEN, ("%d of %d aborts left a reader alive past the grace -- P1 in"
                      " the shape #90 names it" % (leaked, runs))
    if not hits:
        return INVALID, ("%d repetitions and the race was never hit: every abort"
                         " landed after the first byte, so the case measured"
                         " something else" % runs)
    return ANSWERED, ("%d aborts before the first byte, %d of them hit the race,"
                      " 0 readers left behind" % (runs, hits))


_RESUMED = re.compile(r"resumed(?:\s+from\s+\S+)?:\s*(\d+)\s+messages")


def resumed_count(text: str) -> int | None:
    """The N out of `resumed: N messages`, through the dim colour it is printed in.

    cli/crow.py:1313 wraps that line in ANSI, and a probe that matched the raw
    bytes would find nothing on a coloured terminal and report the round trip as
    broken. None means the line was not there at all, which is the OTHER answer
    and must not be confused with zero.
    """
    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")
    match = _RESUMED.search(plain)
    return int(match.group(1)) if match else None


def prefix_verdict(prompt_ns: list[int], context_after: int) -> tuple[str, str]:
    """What a client switch costs at one slot. (classification, sentence)

    The numbers are A, B (same client), C (a body with its own system prompt) and
    D (back to the first client). B is what a held prefix looks like; D against B
    is the answer the plan asks for and nobody has.
    """
    if len(prompt_ns) != 4 or any(n is None for n in prompt_ns):
        return "undecided", "not all four turns reported a prompt_n"
    a, b, c, d = prompt_ns
    if context_after and d >= 0.5 * context_after:
        return ("re-read",
                "the switch back cost %d prompt tokens against %d on the held"
                " prefix -- the client change re-read the conversation" % (d, b))
    if d <= max(4 * b, b + 64):
        return ("held",
                "the switch back cost %d prompt tokens against %d on the held"
                " prefix -- a client change costs nothing measurable here" % (d, b))
    return ("undecided",
            "A %d, B %d, C %d, D %d -- between the two shapes, and the numbers are"
            " what this probe was for" % (a, b, c, d))


# -------------------------------------------------------------- the recorder --

@contextlib.contextmanager
def recording(sink: list):
    """Tee every SSE payload into `sink` while the real stream runs.

    `_post_stream` is the only door there is (cli/crow_core.py says so where the
    transport is deliberately not a parameter), so a wrapper here records the
    SHIPPED stream rather than a second one built for measuring.
    """
    original = crow_core._post_stream

    def tee(url, body, api_key, timeout):
        for payload in original(url, body, api_key, timeout):
            sink.append(payload)
            yield payload

    crow_core._post_stream = tee
    try:
        yield sink
    finally:
        crow_core._post_stream = original


def as_client(system: str | None, messages: list[dict]) -> crow_core.Conversation:
    """The same history, seen through another client's system prompt.

    `Conversation.restore` refuses a running conversation on purpose, so a client
    switch is a NEW conversation over the same messages -- which is what it is on
    the wire too: the head changes, the tail does not.
    """
    conversation = crow_core.Conversation(system)
    body = [dict(m) for m in messages if m.get("role") != "system"]
    head = [{"role": "system", "content": system}] if system else []
    conversation.restore(head + body)
    return conversation


# ---------------------------------------------------------------- the window --

class Window:
    """The shipped client path, driven the way the page drives it.

    THERE IS NO PAGE HERE, AND THAT IS THE EDGE OF WHAT THIS BLOCK ANSWERS. E12
    replaced the Tk window with a webview: `Api` holds the conversation, the
    turn and the session, and pushes ONE JSON message per event onto a queue
    that pywebview hands to the page. Everything up to that queue is shipped
    code and is measured here. What the page's JavaScript then draws is not --
    so two things the Tk build could check are reported as NOT MEASURED rather
    than as held: folding a thought block open again, and typing into the input
    while a turn runs. Both live entirely in the page. A block that ticked them
    off from Python would be reporting on code it never ran.

    WHAT REPLACED `update()`. The Tk version pumped an event loop; here the
    queue is drained on this thread, which is what `Api.pump` does in the
    product. The messages are kept as they come, because they ARE what the
    window was told to show.
    """

    def __init__(self, base_url: str, *argv: str) -> None:
        import crow_gui

        self.gui = crow_gui
        args = crow_gui.build_parser().parse_args(["--base-url", base_url, *argv])
        self.api = crow_gui.Api(args)
        self.messages: list = []
        # What the page calls once it has loaded: the meta line, and the probe
        # that asks /health, /props and restores the session.
        self.api.ready()
        self.until(lambda: any(m.get("k") in ("up", "down") for m in self.messages),
                   30.0)
        self.pump(0.3)

    # -- driving it ----------------------------------------------------------

    def drain(self) -> int:
        """Take everything waiting, the way `pump` hands it to the page."""
        taken = 0
        while True:
            try:
                self.messages.append(self.api._out.get_nowait())
                taken += 1
            except Exception:                    # noqa: BLE001 - an empty queue
                return taken

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)

    def until(self, predicate, limit: float) -> tuple[bool, float]:
        """Drain until the predicate holds. (whether it did, seconds it took)"""
        started = time.monotonic()
        deadline = started + limit
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return True, time.monotonic() - started
            time.sleep(0.005)
        return False, time.monotonic() - started

    def ask(self, text: str) -> None:
        """Exactly what the page's `go()` calls when the user presses send."""
        self.api.send(text)

    def stop(self, wait: bool = True) -> float:
        """The abort, and how long the PAGE waited to be told the turn ended.

        NOT THE SAME NUMBER THE TK BUILD REPORTED, and it is not comparable to
        it. There the button was put back synchronously by the same click, so
        "the interface came back" was a few milliseconds by construction. The
        page cannot do that: it stays in its running state until `{"k": "idle"}`
        arrives, so the honest measurement is how long that took -- which is the
        core noticing the flag (it polls at 50 ms) and the turn ending.
        """
        started = time.monotonic()
        seen = len(self.messages)
        self.api.stop()
        if wait:
            self.until(lambda: any(m.get("k") == "idle"
                                   for m in self.messages[seen:]), ABORT_STUCK_S)
        return time.monotonic() - started

    # -- reading what the page was told ---------------------------------------

    def mark(self) -> int:
        """Where the current turn starts in the record, for the counts below.

        A COUNT SINCE A MARK, not since the window opened: F1 asks whether ONE
        answer arrived in pieces, and a total carried over from the turn before
        it would clear the floor on its own.
        """
        return len(self.messages)

    def answer(self, since: int = 0) -> str:
        """Every character the page was told to draw as answer text."""
        return "".join(m.get("t", "") for m in self.messages[since:]
                       if m.get("k") == "text")

    def draws(self, since: int = 0) -> int:
        """How many separate times the page was told to add answer text.

        THE WEBVIEW'S ANSWER TO F1. There is no widget state to count: each of
        these is one message across the boundary and one insert on the page. A
        window that filled once at the end has exactly one.
        """
        return sum(1 for m in self.messages[since:] if m.get("k") == "text")

    def thoughts(self, since: int = 0) -> int:
        return sum(1 for m in self.messages[since:] if m.get("k") == "think_open")

    def thought_chars(self, since: int = 0) -> int:
        return sum(len(m.get("t", "")) for m in self.messages[since:]
                   if m.get("k") == "think")

    def code_blocks(self, since: int = 0) -> list:
        """The code the page was told to frame, one string per block."""
        out, current = [], None
        for message in self.messages[since:]:
            kind = message.get("k")
            if kind == "code_open":
                current = []
            elif kind == "text" and current is not None:
                current.append(message.get("t", ""))
            elif kind == "code_close" and current is not None:
                out.append("".join(current))
                current = None
        return out

    def cost_line(self) -> str:
        """The last cost line the page was handed, as it stands."""
        for message in reversed(self.messages):
            if message.get("k") == "cost" and message.get("line"):
                return message["line"]
        return ""

    def model_chip(self) -> str:
        for message in reversed(self.messages):
            if message.get("k") == "up" and message.get("model"):
                return message["model"]
        return ""

    def shown(self, text: str) -> bool:
        """Whether the page was told to draw this, as a question or an answer."""
        return any(text in str(message.get("t", "")) for message in self.messages
                   if message.get("k") in ("user", "text"))

    # -- the objects the probes reach into ------------------------------------

    @property
    def conversation(self):
        return self.api._conversation

    @property
    def worker(self):
        return self.api._worker

    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def idle(self) -> bool:
        return not self.running()

    def save(self) -> str:
        """Write the session the way the window writes it, and say what landed."""
        self.api._persist_live(with_kv=True)
        held = len(self.conversation)
        return ("session saved -- %d messages" % held
                if os.path.exists(crow_core.SESSION_FILE) else "nothing was written")

    def close(self) -> None:
        crow_core.INTERRUPT.set()
        worker = self.worker
        if worker is not None:
            worker.join(5.0)
        self.drain()
        # The flag is right for a client that is going away and wrong for the
        # next probe in this process: `run_turn` would see it and report an
        # interrupted turn before its first round.
        crow_core.INTERRUPT.clear()


def clipboard_text() -> str:
    """What is on the system clipboard, read back OUTSIDE the client.

    `Api.copy` shells out to `clip`, so reading it back through the same door
    would prove only that the string survived a round trip inside this process.
    PowerShell is a second door onto the real clipboard.
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20)
    except Exception:                            # noqa: BLE001 - reported as empty
        return ""
    return (proc.stdout or "").replace("\r\n", "\n")


def toolkit_problem() -> str | None:
    """Whether the client path can be driven at all. Asked BEFORE anything is sent.

    IT NO LONGER ASKS ABOUT TK, and that is E12: the window is a webview, and
    the part of it this block drives is plain Python that needs no display. What
    is left to check is that the module imports and still offers the door the
    probes go through -- a rename of `Api` would otherwise surface as a probe
    dying in the middle of a paid run.
    """
    try:
        import crow_gui
    except Exception as exc:                     # noqa: BLE001
        return "cli/crow_gui.py does not import: %s" % exc
    for name in ("Api", "build_parser"):
        if not hasattr(crow_gui, name):
            return "cli/crow_gui.py has no %s" % name
    return None


# ----------------------------------------------------------------- the block --

class Block:
    """Everything the five probes share: the endpoint, the run directory, the log."""

    def __init__(self, args) -> None:
        self.args = args
        self.base_url = args.base_url
        self.model = args.model
        self.api_key = args.api_key
        self.timeout = args.timeout
        self.run_dir = args.run_dir
        self.props: dict = {}
        self.command_line = ""
        self.pid = ""
        self.started = time.monotonic()

    def log(self, text: str) -> None:
        print("[%7.1fs] %s" % (time.monotonic() - self.started, text), flush=True)

    def path(self, name: str) -> str:
        os.makedirs(self.run_dir, exist_ok=True)
        return os.path.join(self.run_dir, name)

    def write(self, name: str, text: str) -> str:
        target = self.path(name)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
        return os.path.relpath(target, REPO).replace("\\", "/")

    # -- one turn without a window, which is what (iii) and (iv) need ---------

    def turn(self, conversation: crow_core.Conversation, sink: list | None = None):
        """One streamed reply through the shipped path. Returns its timings."""
        payloads: list = [] if sink is None else sink
        with recording(payloads):
            text, reasoning, timings = crow_core.stream_reply(
                conversation,
                base_url=self.base_url,
                model=self.model,
                api_key=self.api_key,
                # THE SHIPPED SAMPLING, not the manifest's measurement
                # temperature of 0. This block is an acceptance run of the point
                # as it goes out; pinning temperature would measure a
                # configuration no user has.
                temperature=crow_core.TEMPERATURE,
                top_p=crow_core.TOP_P,
                min_p=crow_core.MIN_P,
                timeout=self.timeout)
        return text, reasoning, timings


# ------------------------------------------------------------------ probe iii --

def probe_iii(block: Block) -> Finding:
    """Does `timings_per_token: true` hang the block on every chunk or the last?

    The comment at cli/crow_core.py:1490-1492 says "final chunk", the field name
    says the opposite, and the reader is a loop that cannot see the difference.
    E12 carries two F7 fixtures because of it; this run says which one is the
    wire.
    """
    finding = Finding("iii", PROBE_ONELINE["iii"])
    conversation = crow_core.Conversation(crow_core.DEFAULT_SYSTEM)
    conversation.append("user", QUESTIONS["word"])
    payloads: list = []
    started = time.monotonic()
    _text, _reasoning, timings = block.turn(conversation, payloads)
    finding.seconds = time.monotonic() - started

    finding.run = block.write("iii-stream.sse", "\n".join(payloads) + "\n")
    raw, parsed = count_timings(payloads)
    predicted_n = timings.get("predicted_n")
    verdict, why = timings_shape(parsed, predicted_n)
    finding.say("chunks recorded: %d" % len(payloads))
    finding.say("grep -c '\"timings\"': %d, parsed timings blocks: %d" % (raw, parsed))
    if raw != parsed:
        finding.say("the two counts DIFFER by %d: the grep counts the FIELD, the"
                    " parse counts a timings BLOCK -- a chunk carrying"
                    " `\"timings\": null` is one and not the other, and the plan's"
                    " `grep -c` cannot tell them apart" % abs(raw - parsed))
    finding.say(why)
    finding.say("the fixture that describes the wire: cli/test_crow_gui.py's"
                " chunks_for(..., every=%s)" % ("True" if verdict == "every chunk"
                                                else "False"))
    numbers = dict(chunks=len(payloads), grep=raw, parsed=parsed,
                   predicted_n=predicted_n or 0)
    if verdict == "undecided":
        return finding.open("undecided: %s" % why, **numbers)
    return finding.answer("timings arrives on the %s (%d of %d chunks)"
                          % (verdict, parsed, len(payloads)), shape=verdict, **numbers)


# ------------------------------------------------------------------- probe iv --

# The second client's system prompt. NOT a second copy of DEFAULT_SYSTEM with a
# word changed for effect: what a client switch means for the prefix is exactly
# `prefix_fingerprint`, which hashes TOOLS and the system prompt, so any system
# prompt that is not the shipped one is the whole of the difference.
OTHER_SYSTEM = "Du bist ein anderer Client an demselben Slot."


def probe_iv(block: Block) -> Finding:
    """What two clients at one slot cost. Four turns, four prompt_n.

    Nobody has run the CLI and the window alternately against one server. The
    prefix break follows from `prefix_fingerprint` (cli/crow_core.py:412-427); the
    NUMBER for a client switch does not exist, and this is the run that makes it.
    """
    finding = Finding("iv", PROBE_ONELINE["iv"])
    messages: list[dict] = []
    prompt_ns: list[int] = []
    cached: list[int] = []
    context_after = 0
    transcript: list[dict] = []

    for label, system, question in (
            ("A cli", crow_core.DEFAULT_SYSTEM, QUESTIONS["prefix"]),
            ("B cli", crow_core.DEFAULT_SYSTEM, QUESTIONS["word"]),
            ("C other client", OTHER_SYSTEM, QUESTIONS["word"]),
            ("D cli again", crow_core.DEFAULT_SYSTEM, QUESTIONS["word"])):
        conversation = as_client(system, messages)
        conversation.append("user", question)
        started = time.monotonic()
        text, reasoning, timings = block.turn(conversation)
        conversation.append("assistant", text, reasoning=reasoning)
        messages = conversation.payload()
        prompt_n = int(timings.get("prompt_n") or 0)
        cached_n = int(timings.get("_cached_tokens") or 0)
        context_after = crow_core.next_context_tokens(context_after, timings)
        prompt_ns.append(prompt_n)
        cached.append(cached_n)
        transcript.append({"turn": label, "system": "shipped" if system ==
                           crow_core.DEFAULT_SYSTEM else "other",
                           "prompt_n": prompt_n, "cached": cached_n,
                           "predicted_n": int(timings.get("predicted_n") or 0),
                           "context_tokens": context_after,
                           "seconds": round(time.monotonic() - started, 2)})
        finding.say("%-14s prompt_n %6d   cached %6d   context %6d"
                    % (label, prompt_n, cached_n, context_after))
        finding.seconds += transcript[-1]["seconds"]

    finding.run = block.write("iv-prefix.json", json.dumps(transcript, indent=1))
    shape, why = prefix_verdict(prompt_ns, context_after)
    finding.say(why)
    numbers = dict(prompt_n=prompt_ns, cached=cached, shape=shape)
    if shape == "undecided":
        return finding.open("a client switch: %s" % why, **numbers)
    return finding.answer("a client switch costs %d prompt tokens against %d on the"
                          " held prefix (%s)" % (prompt_ns[3], prompt_ns[1], shape),
                          **numbers)


# -------------------------------------------------------------------- probe i --

def probe_i(block: Block) -> Finding:
    """F4, and the proof is the NEXT QUESTION, not the interface reacting.

    A flag can fake a button. At one slot the only thing that cannot be faked is
    a second question being answered while the first is supposed to be dead.
    """
    finding = Finding("i", PROBE_ONELINE["i"])
    started = time.monotonic()
    win = Window(block.base_url, "--no-session")
    try:
        # -- the long turn, aborted after 3 s --------------------------------
        win.ask(QUESTIONS["long"])
        win.pump(3.0)
        aborted_worker = win.worker
        freed = win.stop()
        finding.say("the page was told the turn had ended %.3f s after the abort"
                    " -- NOT the Tk build's 'interface came back', which the same"
                    " click did synchronously and which no page can do" % freed)

        # -- the one-word question, immediately, as a user would type it -----
        before = len(win.answer())
        win.ask(QUESTIONS["word"])
        asked_worker = win.worker
        refused = asked_worker is aborted_worker
        if refused:
            finding.say("THE SECOND QUESTION WAS NEVER SENT: `Api.send` returns"
                        " silently while a worker is alive, and the aborted one"
                        " still was -- which is P1 seen from the other end")
        mark = len(win.messages)
        released_early = {"seen": False}

        def answered() -> bool:
            if win.running() and any(m.get("k") == "idle"
                                     for m in win.messages[mark:]):
                # The aborted turn's `idle` reaching the page while the next turn
                # is streaming: the send button goes back to "send" over a
                # running answer. Recorded, not fixed -- E14 measures the
                # shipped window, it does not change it.
                released_early["seen"] = True
            return len(win.answer()) > before

        got, seconds = win.until(answered, ABORT_STUCK_S + 30.0)
        verdict, sentence = abort_verdict(seconds if got else ABORT_STUCK_S + 30.0)
        finding.say(sentence)
        finding.say("the reference this band is derived from: ttft 7.15 s against a"
                    " fresh server, n = 1, #90's measuring head")
        if released_early["seen"]:
            finding.say("AND THE PAGE WAS TOLD THE TURN WAS OVER WHILE ONE WAS"
                        " RUNNING: the aborted turn's `idle` arrived while the"
                        " next answer was streaming, so the button reads 'send'"
                        " over a turn in flight")
        if aborted_worker is not None and aborted_worker.is_alive():
            finding.say("the aborted reader was still alive when the next answer"
                        " arrived -- P1, and the read timeout is what bounds it")

        # -- the race before the first byte ----------------------------------
        runs = hits = leaked = 0
        race_started = time.monotonic()
        for _ in range(RACE_FLOOR):
            win.conversation.reset()
            win.ask(QUESTIONS["word"])
            worker = win.worker
            had_text = len(win.answer())
            # NOT WAITED OUT HERE. `stop()` normally waits for the page to be
            # told the turn ended; fifty of those at up to ABORT_STUCK_S each is
            # an hour of server time for a question this loop does not ask. What
            # it asks is whether the reader is gone within the grace, and that
            # is the `until` two lines down.
            win.stop(wait=False)
            runs += 1
            if len(win.answer()) == had_text:
                hits += 1
            gone, _ = win.until(lambda w=worker: w is None or not w.is_alive(),
                                block.args.grace)
            if not gone:
                leaked += 1
            win.pump(0.05)
        race_seconds = time.monotonic() - race_started
        race, race_sentence = race_verdict(runs, hits, leaked)
        finding.say(race_sentence)
        finding.say("%d repetitions in %.1f s" % (runs, race_seconds))
    finally:
        win.close()
        finding.seconds = time.monotonic() - started

    finding.run = block.write("i-abort.json", json.dumps(
        # `page_told_idle_s` RATHER THAN `interface_freed_s`: the Tk build's
        # number was the button being put back by the same click and is not
        # comparable to this one. A key that kept its old name would invite the
        # comparison the two numbers cannot carry.
        {"seconds_to_next_answer": round(seconds, 2), "verdict": verdict,
         "page_told_idle_s": round(freed, 3), "second_question_refused": refused,
         "race_runs": runs, "race_hits": hits, "race_leaked": leaked,
         "released_early": released_early["seen"]}, indent=1))
    numbers = dict(seconds=round(seconds, 2), idle_after=round(freed, 3),
                   refused=refused, runs=runs, hits=hits, leaked=leaked)
    if verdict == INVALID or race == INVALID:
        return finding.invalid("F4 undecided: %s" % sentence, **numbers)
    if verdict == OPEN or race == OPEN:
        return finding.open("F4 open: %s" % (sentence if verdict == OPEN
                                             else race_sentence), **numbers)
    return finding.answer("the next question answered %.2f s after the abort, and"
                          " %d aborts before the first byte left no reader behind"
                          % (seconds, runs), **numbers)


# ------------------------------------------------------------------- probe ii --

def probe_ii(block: Block) -> Finding:
    """One session, two doors -- the two LIVE directions E12 could not run.

    cli/crow.py leaves at :1259 with return 2 before it ever prints `resumed`
    at :1313 when no endpoint answers, so both directions need this block.
    """
    finding = Finding("ii", PROBE_ONELINE["ii"])
    started = time.monotonic()

    # BEFORE ANY SERVER TIME IS SPENT ON IT. Both doors have to open the same
    # file, and when they cannot this probe has nothing to measure -- saying so
    # costs nothing, while four turns against two different files cost the run
    # and look like an answer.
    env, problem = cli_environment()
    if problem:
        finding.say(problem)
        finding.seconds = time.monotonic() - started
        # A REFUSAL LEAVES A FILE TOO. The report links a run for every probe,
        # and the one that explains why nothing was measured is the one a reader
        # will look for -- an empty link there reads as a tool that fell over.
        finding.run = block.write("ii-roundtrip.json", json.dumps(
            {"measured": False, "why": problem,
             "session_dir": crow_core.SESSION_DIR,
             "default_session_dir": default_session_dir()}, indent=1))
        return finding.invalid("the two clients cannot be pointed at one session"
                               " file, so the round trip has no subject")

    # -- forward: two turns in the window, then the CLI reads the file -------
    win = Window(block.base_url)
    try:
        for question in (QUESTIONS["word"], QUESTIONS["prefix"]):
            win.ask(question)
            win.until(win.idle, block.args.turn_limit)
        wrote = win.save()
        in_window = len(win.conversation)
        finding.say("the window saved %d messages: %s" % (in_window, wrote))
    finally:
        win.close()

    forward = _cli(block, ["/exit"], block.args.turn_limit, env)
    said = resumed_count(forward["stdout"] + forward["stderr"])
    finding.say("the CLI reported: %s"
                % ("resumed: %d messages" % said if said is not None
                   else "no resumed line at all (exit %d)" % forward["code"]))
    forward_ok = said is not None and said == in_window

    # -- backward: two turns in the CLI, then the window has to SHOW them ----
    backward = _cli(block, [QUESTIONS["word"], QUESTIONS["prefix"], "/exit"],
                    block.args.turn_limit * 2, env)
    restored = crow_core.load_session(block.base_url, crow_core.DEFAULT_SYSTEM)
    in_file = len(restored[0]) if restored else 0
    finding.say("after two CLI turns the session file holds %d messages" % in_file)

    win = Window(block.base_url)
    try:
        win.until(lambda: len(win.conversation) >= in_file, 30.0)
        # WHAT THE PAGE WAS TOLD TO DRAW, not what the client is holding: a
        # window that restored the messages and drew none of them is the exact
        # failure this direction is here to catch, and holding them is not
        # showing them.
        shown = win.shown(QUESTIONS["word"]) and win.shown(QUESTIONS["prefix"])
        in_second_window = len(win.conversation)
    finally:
        win.close()
    backward_ok = in_second_window == in_file and shown and in_file > 0
    finding.say("the window resumed %d messages and %s both CLI questions"
                % (in_second_window, "shows" if shown else "DOES NOT show"))

    finding.seconds = time.monotonic() - started
    finding.run = block.write("ii-roundtrip.json", json.dumps(
        {"forward": {"window_messages": in_window, "cli_resumed": said,
                     "cli_exit": forward["code"]},
         "backward": {"file_messages": in_file, "window_messages": in_second_window,
                      "both_questions_drawn": shown, "cli_exit": backward["code"]}},
        indent=1))
    block.write("ii-cli-forward.log", forward["stdout"] + forward["stderr"])
    block.write("ii-cli-backward.log", backward["stdout"] + backward["stderr"])

    numbers = dict(window_messages=in_window, cli_resumed=said,
                   file_messages=in_file, window_after=in_second_window)
    if forward_ok and backward_ok:
        return finding.answer("both directions: %d messages out of the window into"
                              " the CLI, %d out of the CLI into the window"
                              % (in_window, in_file), **numbers)
    which = []
    if not forward_ok:
        which.append("forward")
    if not backward_ok:
        which.append("backward")
    return finding.open("the round trip is open in the %s direction"
                        % " and ".join(which), **numbers)


def default_session_dir() -> str:
    """Where crow_core puts the session when nobody has moved it."""
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "Crow", "session")


def cli_environment() -> tuple[dict | None, str | None]:
    """The environment cli/crow.py has to run in to see the SAME session file.

    THE CLI IS ANOTHER PROCESS, AND `--session-dir` NEVER REACHED IT. The flag
    rebinds a module global inside THIS process; crow_core derives SESSION_DIR
    from LOCALAPPDATA at import, so a second process reads the operator's real
    directory whatever this one was told.

    MEASURED 2026-08-13, and it did not look like a failure: probe (ii) reported
    "the window saved 5 messages, the CLI reported: resumed: 3". Two different
    files, compared as though they were one -- the 3 were the operator's own
    chat. The block's SessionBackup had been pointed at the given directory too,
    so the guard that exists for exactly this ran over the wrong files while the
    CLI wrote into the real ones.

    LOCALAPPDATA IS THE ONLY DOOR, and it only fits a directory that ends in
    `Crow/session`. Anything else comes back as a problem, because a round trip
    measured across two files is worse than one not measured at all.
    """
    session = os.path.normpath(crow_core.SESSION_DIR)
    if session == os.path.normpath(default_session_dir()):
        return os.environ.copy(), None
    head, tail = os.path.split(session)
    if tail.lower() == "session" and os.path.basename(head).lower() == "crow":
        env = os.environ.copy()
        env["LOCALAPPDATA"] = os.path.dirname(head)
        return env, None
    return None, ("--session-dir %s cannot be handed to cli/crow.py: crow_core"
                  " derives the path from LOCALAPPDATA, so the directory has to"
                  " end in Crow%sSession for both doors to open the same file"
                  % (crow_core.SESSION_DIR, os.sep))


def _cli(block: Block, lines: list[str], limit: float,
         env: dict | None = None) -> dict:
    """cli/crow.py as a user runs it, with its answers typed in from a pipe.

    --no-update-check and --no-font keep the run to the one thing it is for; the
    session, the endpoint and the resume line are untouched, because those are
    what is being measured. `env` carries the session directory -- see
    `cli_environment`, and why passing it is not optional.
    """
    argv = [sys.executable, os.path.join(REPO, "cli", "crow.py"),
            "--base-url", block.base_url, "--no-update-check", "--no-font",
            "--no-background"]
    proc = subprocess.run(argv, input="\n".join(lines) + "\n",
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=limit, env=env)
    return {"code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


# -------------------------------------------------------------------- probe v --

def probe_v(block: Block) -> Finding:
    """The seven capabilities, in one pass, in one session, against this server.

    F5 AND F7 ARE PROBED, NOT TICKED OFF. "It comes from the core" is a statement
    about ownership; the epic's criterion is about an application doing the seven
    things, and only a run says whether it does.
    """
    finding = Finding("v", PROBE_ONELINE["v"])
    started = time.monotonic()
    results: dict[str, bool] = {}
    # WHAT THIS PASS CANNOT SEE, carried to the report rather than dropped. The
    # webview's page is not running here, so two of the checks the Tk build made
    # have no subject. Silence would read as "held".
    not_measured: list[str] = []
    payloads: list = []

    win = Window(block.base_url)
    try:
        # F3 -- the chip against /props, character for character.
        chip = win.model_chip()
        try:
            open_model = crow_core.fetch_model_name(block.base_url)
        except Exception as exc:                 # noqa: BLE001 - recorded, not raised
            open_model = ""
            finding.say("F3  /props did not answer: %s" % exc)
        results["F3 connection"] = bool(chip) and chip == open_model
        finding.say("F3  chip %r against /props %r" % (chip, open_model))

        # F1, F2, F7 -- one recorded turn, three predicates.
        mark = win.mark()
        with recording(payloads):
            win.ask(QUESTIONS["prefix"])
            win.until(win.idle, block.args.turn_limit)
        draws = win.draws(mark)
        content, reasoning_chars, predicted_n, prompt_n = _from_stream(payloads)
        visible = win.answer(mark)
        results["F1 streaming"] = draws >= STATE_FLOOR
        finding.say("F1  the page was handed the answer in %d pieces, against a"
                    " floor of %d" % (draws, STATE_FLOOR))
        results["F2 reasoning"] = (len(visible) == len(content)
                                   and (reasoning_chars == 0
                                        or win.thoughts(mark) > 0))
        finding.say("F2  %d visible answer characters against %d content-delta"
                    " characters, %d thought characters in %d block(s)"
                    % (len(visible), len(content), reasoning_chars,
                       win.thoughts(mark)))
        # THE FOLD IS NOT MEASURED, and saying so is the point. In the Tk build
        # the block was a widget this tool could open and count; in the webview
        # it is a `<details>` on a page that is not running here. The thoughts
        # ARRIVING is measured above -- whether a click folds them open is not,
        # and a green line for it would be a claim about code nobody ran.
        not_measured.append("F2  folding a thought block open again -- page only")
        cost = win.cost_line()
        results["F7 cost line"] = bool(cost) and _cost_matches(cost, predicted_n)
        finding.say("F7  %r against predicted_n %d, prompt_n %d"
                    % (cost, predicted_n, prompt_n))

        # F6 -- a code block and its copy button.
        mark = win.mark()
        win.ask(QUESTIONS["code"])
        win.until(win.idle, block.args.turn_limit)
        blocks = win.code_blocks(mark)
        copied = ""
        if blocks:
            # THE BUTTON'S OWN PATH: the page calls `pywebview.api.copy`, which
            # shells out to `clip`, because `navigator.clipboard` refuses
            # silently outside a secure context. Read back through PowerShell,
            # which is a different door onto the same clipboard.
            if win.api.copy(blocks[0]):
                copied = clipboard_text()
        held_equal = bool(blocks) and copied.strip() == blocks[0].strip()
        results["F6 code block"] = bool(blocks) and bool(copied) and held_equal
        finding.say("F6  %d block(s), %d characters on the clipboard, equal: %s"
                    % (len(blocks), len(copied), held_equal))

        # F4 -- the abort as the page sees it. The slot half is probe (i), and
        # it is the half that decides F4; this one only says the page was told.
        win.ask(QUESTIONS["long"])
        win.pump(2.0)
        freed = win.stop()
        results["F4 abort"] = freed < ABORT_GRIP_S and win.idle()
        finding.say("F4  the page was told the turn had ended %.3f s after the"
                    " abort, and no worker was left running" % freed)
        not_measured.append("F4  typing into the input while a turn runs -- page only")
        win.until(win.idle, block.args.turn_limit)

        # F5 -- the session, through a restart of the window.
        saved = win.save()
        held = len(win.conversation)
        finding.say("F5  %s" % saved)
    finally:
        win.close()

    again = Window(block.base_url)
    try:
        again.until(lambda: len(again.conversation) >= held, 30.0)
        results["F5 history"] = (len(again.conversation) == held
                                 and again.shown(QUESTIONS["prefix"]))
        finding.say("F5  a second window resumed %d of %d messages and was told to"
                    " draw the first question again"
                    % (len(again.conversation), held))
    finally:
        again.close()

    for line in not_measured:
        finding.say("NOT MEASURED  %s" % line)

    finding.seconds = time.monotonic() - started
    finding.run = block.write("v-capabilities.json", json.dumps(
        {"held": results, "not_measured": not_measured}, indent=1))
    block.write("v-stream.sse", "\n".join(payloads) + "\n")
    held_count = sum(1 for ok in results.values() if ok)
    for name in sorted(results):
        finding.say("%-16s %s" % (name, "held" if results[name] else "DID NOT HOLD"))
    numbers = dict(held=held_count, of=len(results), results=results,
                   not_measured=len(not_measured))
    if held_count == len(results):
        return finding.answer("%d of %d capabilities held against this server"
                              % (held_count, len(results)), **numbers)
    return finding.open("%d of %d capabilities held; %s did not"
                        % (held_count, len(results),
                           ", ".join(n for n in sorted(results) if not results[n])),
                        **numbers)


def _from_stream(payloads: list[str]) -> tuple[str, int, int, int]:
    """(content, reasoning characters, predicted_n, prompt_n) out of a recording."""
    content: list[str] = []
    reasoning = 0
    predicted_n = prompt_n = 0
    for payload in payloads:
        try:
            chunk = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(chunk.get("timings"), dict):
            predicted_n = int(chunk["timings"].get("predicted_n") or predicted_n)
            prompt_n = int(chunk["timings"].get("prompt_n") or prompt_n)
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning += len(delta["reasoning_content"])
    return "".join(content), reasoning, predicted_n, prompt_n


def _cost_matches(cost: str, predicted_n: int) -> bool:
    """The cost line's token count against the stream's own predicted_n.

    F7 is "tok/s and tokens per turn, FROM THE RESPONSE'S timings field". A line
    that reads well and counts something else is the failure; this is the only
    predicate that connects the two.
    """
    if not predicted_n:
        return False
    match = re.search(r"([\d,]+) tok", cost)
    return bool(match) and int(match.group(1).replace(",", "")) == predicted_n


PROBES = {"iii": probe_iii, "iv": probe_iv, "i": probe_i, "ii": probe_ii, "v": probe_v}


# ------------------------------------------------------------------ the report --

def report(block: Block, findings: list[Finding], restored: list[str]) -> str:
    """The comment for #90: five numbers and the runs they came out of."""
    out = []
    out.append("### E14 -- the server block, against the shipped operating point")
    out.append("")
    out.append("Run %s, crow %s, one llama-server (pid %s) whose command line was"
               " held against manifests/operating-point.json before the first byte."
               % (time.strftime("%Y-%m-%d %H:%M"), crow.VERSION, block.pid or "?"))
    settings = block.props.get("default_generation_settings") or {}
    out.append("Open model: `%s`, n_ctx %s."
               % (block.props.get("model_path") or block.props.get("model") or "?",
                  settings.get("n_ctx") or block.props.get("n_ctx") or "?"))
    out.append("")
    out.append("| Probe | Question | The number | Run |")
    out.append("|---|---|---|---|")
    for finding in findings:
        out.append("| **(%s)** | %s | %s%s | `%s` |"
                   % (finding.probe, finding.question,
                      "" if finding.verdict == ANSWERED else "%s -- " % finding.verdict,
                      finding.headline or "not run", finding.run or "-"))
    out.append("")
    for finding in findings:
        out.append("**(%s) %s** -- %s, %.1f s"
                   % (finding.probe, finding.question, finding.verdict,
                      finding.seconds))
        for line in finding.lines:
            out.append("  - %s" % line)
        out.append("")
    out.append("**The return path:** " + "; ".join(restored) + ".")
    out.append("")
    out.append("**What this does not close.** Done criterion 1 is reachable, not"
               " closed: the window's tool posture hangs on #55 and #88, and while"
               " the first candidate SHOWS tool calls instead of running them the"
               " application does the seven things and is not an agent window.")
    return "\n".join(out) + "\n"


PROBE_COST = {
    "iii": "one short turn, recorded",
    "iv": "four short turns, prompt_n per turn",
    "i": "one long turn, one abort, one short turn, then %d aborts before the"
         " first byte" % RACE_FLOOR,
    "ii": "two window turns, then two CLI turns, both directions",
    "v": "three turns in one window plus a second window",
}


def select_probes(only: str) -> list[str]:
    """Which probes run, and in WHAT ORDER -- and the order is never the caller's.

    --only exists for a repeat of a run that came back INVALID (F4's band leaves
    that case open by construction). It picks which; the sequence stays the one
    the plan fixes, because the two recordings decide the fixtures the later
    probes are read against. Raises SetupError for a name that is not a probe.
    """
    wanted = [p.strip() for p in (only or "").split(",") if p.strip()]
    unknown = [p for p in wanted if p not in PROBES]
    if unknown:
        raise SetupError("no such probe: %s (the block is %s)"
                         % (", ".join(unknown), ", ".join(PROBE_ORDER)))
    if not wanted:
        return list(PROBE_ORDER)
    return [p for p in PROBE_ORDER if p in wanted]


def plan_lines(selected: list[str]) -> list[str]:
    """What would run, in the order it would run in, and what it costs."""
    out = ["THE BLOCK, in the order the plan fixes (iii -> iv -> i -> ii -> v):"]
    for probe in selected:
        out.append("  (%-3s) %-52s %s" % (probe, PROBE_ONELINE[probe],
                                          PROBE_COST[probe]))
    return out


# -------------------------------------------------------------------- main ----

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_server_block.py",
        description="E14's five probes against the shipped operating point.")
    parser.add_argument("--consent", action="store_true",
                        help="robin said go. Without it nothing is sent.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="print the plan and the gate, send nothing, exit 0")
    parser.add_argument("--only", default="",
                        help="comma-separated probes, for a REPEAT of an INVALID"
                             " run. The default is the whole block in order.")
    parser.add_argument("--base-url", default=crow_core.DEFAULT_BASE_URL)
    parser.add_argument("-m", "--model", default=crow_core.DEFAULT_MODEL)
    parser.add_argument("--api-key", default="local-no-provider")
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="the read timeout for the probes that do not use a"
                             " window; the window uses its own READ_TIMEOUT_S")
    parser.add_argument("--turn-limit", dest="turn_limit", type=float, default=900.0,
                        help="how long one turn may take before a probe gives up")
    parser.add_argument("--grace", type=float, default=5.0,
                        help="how long an aborted reader may take to die")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--session-dir", dest="session_dir", default="",
                        help="where session.json and crow-session.bin live."
                             " A seam for the negative control; the default is"
                             " the one both clients use.")
    parser.add_argument("--run-dir", dest="run_dir", default="",
                        help="where the recordings go."
                             " Default runs/<date>/e14-server-block")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    try:
        selected = select_probes(args.only)
    except SetupError as exc:
        print("SETUP ERROR: %s" % exc)
        return 2

    if args.session_dir:
        crow_core.SESSION_DIR = args.session_dir
        crow_core.SESSION_FILE = os.path.join(args.session_dir, "session.json")
    if not args.run_dir:
        args.run_dir = os.path.join(args.repo, "runs", time.strftime("%Y-%m-%d"),
                                    "e14-server-block")

    manifest_path = os.path.join(args.repo, "manifests", "operating-point.json")
    if not os.path.exists(manifest_path):
        print("SETUP ERROR: no manifest at %s" % manifest_path)
        return 2
    manifest = json.loads(point.read(manifest_path))

    for line in plan_lines(selected):
        print(line)
    print()
    print("COST: server time, and this is the only stage of #90's plan that costs"
          " any. One consent, for the whole block.")
    print()

    # -- the free half of the gate, which runs even without consent ----------
    try:
        servers = server_processes()
    except SetupError as exc:
        print("SETUP ERROR: %s" % exc)
        return 2
    problems = process_gate(servers)
    for problem in problems:
        print("  FAILED   process list        %s" % problem)
    if not problems:
        print("  OK       process list        exactly one llama-server (pid %s)"
              % servers[0][0])
        problems += command_line_gate(servers[0][1],
                                      point.expected(manifest["servers"]["operating-point"]))
        if problems:
            for problem in problems:
                print("  FAILED   command line        %s" % problem)
        else:
            print("  OK       command line        every flag agrees with the manifest")

    if problems:
        print()
        print("RESULT: the gate is red. Nothing was sent.")
        return 2

    if not (args.consent or args.dry_run):
        print()
        print("Nothing was measured. This block costs server time and needs"
              " robin's consent once, for the whole of it: pass --consent.")
        return 2
    if args.dry_run:
        print()
        print("RESULT: dry run. The gate is green and nothing was sent.")
        return 0

    # -- the half that needs the endpoint ------------------------------------
    block = Block(args)
    block.pid = servers[0][0]
    block.command_line = servers[0][1]
    try:
        crow_core.check_endpoint(args.base_url)
        block.props = fetch_props(args.base_url)
    except Exception as exc:                     # noqa: BLE001
        print("SETUP ERROR: the endpoint did not answer: %s" % exc)
        return 2
    props_problems = props_gate(block.props, manifest)
    for problem in props_problems:
        print("  FAILED   /props              %s" % problem)
    if props_problems:
        print()
        print("RESULT: the gate is red. Nothing was sent.")
        return 2
    print("  OK       /props              the shipped model at the shipped rung")

    if any(p in selected for p in ("i", "ii", "v")):
        toolkit = toolkit_problem()
        if toolkit:
            print("  FAILED   toolkit             %s" % toolkit)
            print()
            print("RESULT: probes i, ii and v drive the client. Nothing was sent.")
            return 2
        print("  OK       toolkit             the client path can be driven")

    # -- the return path, before the first byte ------------------------------
    backup = SessionBackup(crow_core.SESSION_DIR,
                           os.path.join(block.run_dir, "session-before"))
    findings: list[Finding] = []
    restored: list[str] = []
    print()
    for note in backup.take(block.log):
        print("  %s" % note)
    print()
    try:
        for probe in selected:
            block.log("probe (%s) starting" % probe)
            try:
                finding = PROBES[probe](block)
            except Exception as exc:             # noqa: BLE001 - a probe that
                # dies must not take the four that have not run yet with it, and
                # must not skip the restore below.
                finding = Finding(probe, PROBE_ONELINE[probe])
                finding.say("%s: %s" % (type(exc).__name__, exc))
                finding.open("the probe raised before it answered")
            findings.append(finding)
            block.log("probe (%s) %s -- %s" % (probe, finding.verdict,
                                               finding.headline))
    finally:
        restored = backup.put_back()
        print()
        for note in restored:
            print("  %s" % note)

    text = report(block, findings, restored)
    where = block.write("comment-90.md", text)
    print()
    print(text)
    print("written to %s" % where)
    open_questions = [f.probe for f in findings if f.verdict != ANSWERED]
    print("RESULT: %d of %d probes answered their question"
          % (len(findings) - len(open_questions), len(findings)))
    return 1 if open_questions else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
