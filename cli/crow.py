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

# ASCII only, deliberately: the Windows console falls back to cp1252 and
# would mangle box-drawing or emoji mid-animation.
BANNER = r"""
        __
     __( o)>   c r o w
     \___)     v{version}
"""

# One perched raven, four frames: wings, then a blink. Three lines each,
# same width, so the redraw is a fixed cursor-up.
RAVEN_FRAMES = (
    (r"   __   ",
     r"__( o)> ",
     r"\___)   "),
    (r"   __   ",
     r"__( o)> ",
     r"\__/)   "),
    (r"   __   ",
     r"__( -)> ",
     r"\___)   "),
    (r"   __/  ",
     r"__( o)> ",
     r"\___)   "),
)


class CrowError(RuntimeError):
    """Raised when the endpoint cannot be reached or answers with an error."""


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


class Renderer:
    """Prints a streamed reply, setting fenced code apart from prose.

    STREAMING IS THE CONSTRAINT. Prose is written through character by
    character so the answer appears as it is produced. Only inside a fence is
    output held back to the end of a line, because a highlighter cannot colour
    half a token. A fence therefore costs at most one line of latency.

    The fence markers themselves are consumed: they are markup, and printing
    them alongside a drawn frame says the same thing twice.
    """

    WIDTH = 76

    def __init__(self, out=None) -> None:
        self._out = out or sys.stdout
        self._buf = ""
        self.in_code = False
        self.language = ""

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
        cannot leave the frame open."""
        if self._buf:
            self._line(self._buf)
            self._buf = ""
        if self.in_code:
            self._rule("+" + "-" * (self.WIDTH - 2) + "+")
            self.in_code = False

    def _rule(self, text: str) -> None:
        self._out.write(DIM + text + RESET + "\n")
        self._out.flush()

    def _line(self, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith("```"):
            if self.in_code:
                self._rule("+" + "-" * (self.WIDTH - 2) + "+")
                self.in_code = False
                self.language = ""
            else:
                self.language = stripped[3:].strip()
                label = f" {self.language} " if self.language else " code "
                bar = "-" * max(0, self.WIDTH - 3 - len(label))
                self._rule("+-" + label + bar + "+")
                self.in_code = True
            return
        if self.in_code:
            self._out.write(DIM + "| " + RESET + highlight(line, self.language) + "\n")
        else:
            self._out.write(line + "\n")
        self._out.flush()


class Raven:
    """A perched raven that flaps while the server is still thinking.

    It earns its place: a cold prefill of a long context takes minutes here,
    and a blank terminal is indistinguishable from a hung process. The bird
    stops at the first token and erases itself, so it never mixes into the
    reply text.

    Silent when stdout is not a terminal (pipes, CI, transcript capture).
    """

    HEIGHT = len(RAVEN_FRAMES[0])

    def __init__(self, stream=None, interval: float = 0.18, label: str = "thinking") -> None:
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
        for frame in itertools.cycle(RAVEN_FRAMES):
            if self._stop.is_set():
                return
            waited = time.monotonic() - started
            lines = list(frame)
            lines[1] = f"{lines[1]}  {self._label} {waited:5.1f}s"
            with _DRAW_LOCK:
                if self._stop.is_set():
                    return
                if self._drawn:
                    self._stream.write(f"\033[{self.HEIGHT}A")
                for line in lines:
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
                        raven.set_label("writing code")
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
        return f"{BOLD}{CYAN}you>{RESET} "

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
        return f"{DIM}[{RESET}{bar}{DIM}]{RESET} {size}/{limit} {DIM}|{RESET} {BOLD}{CYAN}you>{RESET} "

    return f"{size} {DIM}|{RESET} {BOLD}{CYAN}you>{RESET} "


def repl(args: argparse.Namespace) -> int:
    enable_ansi()
    print(BANNER.format(version=VERSION))

    try:
        status = check_endpoint(args.base_url)
    except CrowError as exc:
        print(f"crow: {exc}", file=sys.stderr)
        print("crow: start llama-server first, then retry.", file=sys.stderr)
        return 2

    n_ctx = fetch_n_ctx(args.base_url)
    room = f", {n_ctx / 1000:.0f}k context" if n_ctx else ""
    print(f"{BOLD}{args.model}{RESET} at {args.base_url} "
          f"{DIM}(health: {status}{room}){RESET}")
    print(f"{DIM}/help for commands, /exit to leave.{RESET}\n")

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
        try:
            reply, timings = stream_reply(
                conversation,
                base_url=args.base_url,
                model=args.model,
                api_key=args.api_key,
                temperature=args.temperature,
                timeout=args.timeout,
                prefix=f"{BOLD}{GREEN}crow>{RESET} ",
            )
        except CrowError as exc:
            print(f"\ncrow: {exc}\n", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            # The partial turn is discarded rather than appended: a truncated
            # assistant message would poison the prefix for every later turn.
            print("\n[interrupted -- turn discarded, context unchanged]\n")
            continue

        conversation.append("assistant", reply)
        prompt_n = timings.get("prompt_n")
        predicted_n = timings.get("predicted_n")
        if prompt_n is not None and predicted_n is not None:
            context_tokens = int(prompt_n) + int(predicted_n)
        line_out = format_timings(timings)
        print(f"\n\n[{line_out}]\n" if line_out else "\n")


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return repl(args)


if __name__ == "__main__":
    sys.exit(main())


