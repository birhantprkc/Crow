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
VERSION = "0.0.0.0.1"

# Without a system prompt the model picks its own language -- measured: "yo"
# came back in Chinese. Kept to one short line on purpose: it sits at the head
# of every context, so it is paid for in prefill exactly once and then cached,
# but only while it stays byte-identical.
DEFAULT_SYSTEM = (
    "You are Crow, a local coding assistant. "
    "Always reply in the same language the user wrote in."
)

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


class CrowError(RuntimeError):
    """Raised when the endpoint cannot be reached or answers with an error."""


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
        """Open the file the rest of this block goes to. Failure is not fatal:
        a read-only directory must cost the file, not the answer."""
        try:
            os.makedirs(self._spill_dir, exist_ok=True)
            ext = _EXT.get(self.language.lower(), "txt")
            self._spill_path = os.path.join(self._spill_dir, f"block-{self.blocks:03d}.{ext}")
            self._spill_file = open(self._spill_path, "w", encoding="utf-8")
        except Exception:
            self._spill_path = None
            self._spill_file = None

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
        if self._spill_path and self._code_lines > SPILL_AFTER:
            hidden = self._code_lines - SPILL_AFTER
            note = f"  {hidden} more lines -> {self._spill_path}"
        elif self._spill_path:
            note = f"  {self._spill_path}"
        self._rule("-" * self.WIDTH + note)
        self.in_code = False
        self.language = ""
        self._code_lines = 0
        self._spill_path = None

    def _line(self, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith("```"):
            if self.in_code:
                self._end_block()
            else:
                self.language = stripped[3:].strip()
                self.blocks += 1
                self._code_lines = 0
                self.in_code = True
                self._open_spill()
                label = f" {self.language}" if self.language else " code"
                self._rule("-" * 3 + label + " " + "-" * max(0, self.WIDTH - 5 - len(label)))
            return

        if not self.in_code:
            self._out.write(line + "\n")
            self._out.flush()
            return

        # Every code line goes to the file, whether it is shown or not, so the
        # saved block is the WHOLE block and not the visible part of it.
        if self._spill_file is not None:
            try:
                self._spill_file.write(line + "\n")
            except Exception:
                pass
        self._code_lines += 1
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
    """

    def __init__(self, system: str | None = None) -> None:
        self._system = system
        self._messages: list[dict[str, str]] = []
        if system:
            self._messages.append({"role": "system", "content": system})

    def append(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})

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
    timeout: float,
    out=sys.stdout,
    prefix: str = "",
) -> tuple[str, dict]:
    """Stream one assistant turn. Returns (text, timings).

    The reply is appended to the conversation by the caller, not here -- a
    turn that was interrupted must not silently become part of the prefix.

    THE SERVER SENDS TWO STREAMS, NOT ONE, and until 2026-08-07 this read
    only one of them. `server_chat_msg_diff_to_json_oaicompat` puts thoughts
    in `delta["reasoning_content"]` and the answer in `delta["content"]` --
    two keys of the same object. Measured over the 30 stored answers of that
    day's reference run: 30 of 30 carried reasoning, and 88.2 % of every
    generated character sat in it. Reading `content` alone therefore threw
    away most of what the model produced and left the user watching a bird.

    Only `content` is returned for the context. Reasoning is display-only:
    the chat template does not replay a previous turn's thoughts, so feeding
    them back would change the prefix for no gain and break the prompt cache.
    """
    body = {
        "model": model,
        "messages": conversation.payload(),
        "temperature": temperature,
        "stream": True,
        # llama.cpp extension: makes the server attach its own timing block
        # to the final chunk. Ignored by endpoints that do not know it.
        "timings_per_token": True,
    }

    text_parts: list[str] = []
    reasoning_chars = 0
    timings: dict = {}
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

            for choice in chunk.get("choices") or []:
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}

                thought = delta.get("reasoning_content")
                if thought:
                    # Counted, not printed. The reasoning is 60-90 % of every
                    # answer this model gives; printed in full it buries the
                    # code. The bird carries the state instead.
                    reasoning_chars += len(thought)
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
    if reasoning_chars:
        timings.setdefault("_reasoning_chars", reasoning_chars)
        timings.setdefault("_content_chars", sum(len(p) for p in text_parts))
    if finish_reason:
        timings.setdefault("_finish_reason", finish_reason)
    return "".join(text_parts), timings


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
  /reset         drop the context (costs a full re-prefill)
  /context       message count in the current context
  /exit, /quit   leave
"""


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


def repl(args: argparse.Namespace) -> int:
    enable_ansi()
    install_interrupt_handler()
    if getattr(args, "background", True):
        set_background()
    print(paint_banner(BANNER.format(version=f"v{VERSION}")))

    # Before the endpoint check, not after: the font has nothing to do with the
    # server. Behind the check it would never install on a machine where the
    # user starts the CLI before llama-server, which is the normal order.
    if getattr(args, "font", True):
        ensure_font()

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
    print(f"{DIM}/help for commands, /exit to leave.{RESET}")
    print("")

    conversation = Conversation(args.system)
    # Tokens the server actually charged for last turn, not an estimate: it
    # reports prompt_n (everything it re-read) and predicted_n (what it added).
    # Their sum is what the NEXT turn starts from. Counting characters here
    # instead would drift from the tokeniser and quietly mislead.
    context_tokens = 0

    while True:
        try:
            line = input(format_prompt(context_tokens, n_ctx)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/help":
            print(HELP)
            continue
        if line == "/reset":
            conversation.reset()
            context_tokens = 0
            print("context dropped -- the next turn pays a full prefill.\n")
            continue
        if line == "/context":
            print(f"{len(conversation)} messages, {context_tokens} tokens\n")
            continue
        conversation.append("user", line)
        print()
        # Cleared per turn: an interrupt from the PREVIOUS turn must not kill
        # the next one before it starts.
        INTERRUPT.clear()
        try:
            reply, timings = stream_reply(
                conversation,
                base_url=args.base_url,
                model=args.model,
                api_key=args.api_key,
                temperature=args.temperature,
                timeout=args.timeout,
                prefix=f"{BOLD}{CROW_TEXT}crow>{RESET} ",
            )
        except CrowError as exc:
            print(f"\ncrow: {exc}\n", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            # The partial turn is discarded rather than appended: a truncated
            # assistant message would poison the prefix for every later turn.
            INTERRUPT.clear()
            print("\n[interrupted -- turn discarded, context unchanged]\n")
            continue

        # The generator returns quietly on an interrupt rather than raising,
        # so the flag is what tells a stopped turn from a finished one.
        if INTERRUPT.is_set():
            INTERRUPT.clear()
            print("\n[interrupted -- turn discarded, context unchanged]\n")
            continue

        conversation.append("assistant", reply)
        prompt_n = timings.get("prompt_n")
        predicted_n = timings.get("predicted_n")
        if prompt_n is not None and predicted_n is not None:
            context_tokens = int(prompt_n) + int(predicted_n)
        line_out = format_timings(timings)
        print(f"\n\n[{line_out}]\n" if line_out else "\n")


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
    # 0.0 is greedy decoding, and greedy is where reasoning models loop:
    # measured 2026-08-07 on a three.js task, the model repeated "Actually,
    # let me..." inside its reasoning block and never reached the answer.
    # Always taking the single most likely token walks into that attractor and
    # cannot walk back out. DeepSeek specifies 0.6 for this model family.
    # Measurement runs that need byte-identical output pass --temperature 0
    # explicitly; the interactive default has to be able to finish a turn.
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="socket timeout in seconds (default: 1800)")
    parser.add_argument("--no-font", dest="font", action="store_false",
                        help=f"do not install the bundled {FONT_FAMILY} on first start")
    parser.add_argument("--no-background", dest="background", action="store_false",
                        help="leave the terminal background alone")
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


