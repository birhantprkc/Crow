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
import types
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

# EVERYTHING BELOW THIS LINE THAT IS NOT A TERMINAL LIVES IN crow_core.
#
# Named one by one rather than `from crow_core import *`, and the list is the
# point rather than the ceremony: a name the core stops exporting fails HERE,
# at import, with an ImportError naming it -- before a single line is drawn. A
# star import would bind whatever happens to be there and fail later, somewhere
# else, on a path a test may not reach.
#
# The names are re-exported on purpose. cli/test_crow.py reaches 332 times
# through `crow.X` over 72 names, and the suite is not touched by this stage.
import crow_core
from crow_core import (  # noqa: F401 -- re-exported for the CLI and its suite
    BANNER_BEVEL,
    BLUE,
    BOLD,
    BUDGET_SPENT,
    adopt_root,
    _c,
    check_endpoint,
    _clip,
    COMMAND_TIMEOUT,
    Conversation,
    CROW_ACCENT,
    CROW_BG,
    CROW_TEXT,
    CrowError,
    CYAN,
    DEFAULT_BASE_URL,
    DEFAULT_MODE,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM,
    DIM,
    _EXT,
    fetch_latest_version,
    fetch_model_name,
    fetch_n_ctx,
    find_root,
    _fn,
    FONT_DIR,
    FONT_FAMILY,
    font_files,
    font_installed,
    forget_session,
    format_clock,
    _GGUF_QUANT,
    _GGUF_SHARD,
    get_root,
    GREEN,
    health_url,
    install_font,
    install_interrupt_handler,
    INTERRUPT,
    is_newer,
    _key,
    _KEYWORDS,
    load_session,
    MAGENTA,
    MAX_HITS,
    MAX_TOOL_BYTES,
    MAX_TOOL_ROUNDS,
    MIN_P,
    model_display_name,
    next_context_tokens,
    _on_sigint,
    parse_version,
    post_json,
    _post_stream,
    prefix_fingerprint,
    _READ,
    recent_paths,
    RED,
    RELEASES_API,
    ReplyEvents,
    REPO,
    REPO_URL,
    RESET,
    RESET_DIM,
    resume_path,
    roll_over,
    ROLLOVER_AT,
    ROLLOVER_NOTE,
    rollover_path,
    ROOT_MARKER,
    ROOT_FILE,
    ROOTS_FILE,
    root_file,
    known_roots,
    remember_root,
    read_root_mode,
    write_root_mode,
    NEVER_CACHED,
    READ_GATED,
    SLASH_COMMANDS,
    run_tool,
    run_tool_cached,
    run_turn,
    save_session,
    _SEEN,
    SESSION_DIR,
    SESSION_FILE,
    session_file_problem,
    SESSION_FORMAT,
    SESSION_FORMAT_KEY,
    session_format_problem,
    SessionFormatError,
    set_root,
    should_roll,
    SLOT_FILE,
    start_update_check,
    stream_reply,
    _STR,
    TEMPERATURE,
    _TOKENS,
    tool_edit_file,
    tool_find_files,
    TOOL_IMPL,
    tool_list_dir,
    tool_read_file,
    tool_run_command,
    tool_search_text,
    tool_write_file,
    TOOLS,
    TOP_P,
    _TTY,
    TurnCost,
    TurnEvents,
    TurnResult,
    UPDATE_COMMAND,
    update_notice,
    write_transcript,
    YELLOW,
)

# THE VERSION LITERAL LIVES HERE AND MAY NOT MOVE.
#
# install.ps1:399-403 reads the installed version straight out of the shipped
# cli\crow.py with ^VERSION\s*=\s*"([^"]+)". With the literal the regex
# answers 0.2.0; with `from crow_core import VERSION` it answers nothing,
# Get-InstalledVersion returns $null, and Resolve-InstallAction takes its
# 'unknown' branch (install.ps1:428-431), which refuses and advises -Force --
# advice a run through `irm ... | iex` cannot follow, because a piped script
# takes no parameters. Every installed base would be un-updatable through the
# one line the README documents. tools/pack-release.ps1:254 reads the same
# pattern to stamp the package, and tools/check_operating_point.py holds it
# against manifests/operating-point.json.
VERSION = "0.3.3"

# The core carries no version of its own -- the owner of the literal hands it
# over. Three places in there need one: the session file's `version` field, the
# User-Agent of the release check, and the "you have X" of the update notice.
crow_core.CLIENT_VERSION = VERSION


# WHY THIS MODULE GETS A CLASS OF ITS OWN, and it is not decoration.
#
# `from crow_core import SESSION_FILE` binds the VALUE, not the name. Rebinding
# `crow.SESSION_FILE` afterwards would leave crow_core.SESSION_FILE pointing at
# the old one -- two states under one name, which is exactly the half-move this
# stage is supposed to make impossible. It is not hypothetical: cli/test_crow.py
# redirects four of these names at the module and then calls code that now lives
# in the core -- SESSION_DIR and SESSION_FILE (three classes), post_json (13
# assignments) and FONT_DIR (three cases). Without the write-through those tests
# would keep pointing at a temporary directory while save_session, load_session
# and font_files went on reading the real one.
#
# So a rebind through the module object is written to the core as well. Module
# BODY assignments do not come through here -- those are plain dictionary stores
# -- which is why this catches monkeypatching and nothing else.
#
# _FROM_CORE is derived, never typed a second time: a name is in it exactly when
# this module and the core currently hold the SAME object under it. A list
# written out by hand would be a third copy to keep in step.
_FROM_CORE = frozenset(
    name for name, value in list(globals().items())
    if not name.startswith("__")
    and name != "annotations"                      # the __future__ import
    and not isinstance(value, types.ModuleType)
    and name in vars(crow_core)
    and vars(crow_core)[name] is value
)


class _CoreBacked(types.ModuleType):
    """The client module, with the core behind the names it borrowed."""

    def __setattr__(self, name, value):
        if name in _FROM_CORE:
            setattr(crow_core, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CoreBacked


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


# The wordmark, drawn rather than typed: no terminal lets a program pick a
# display face, so a banner that should look like anything has to be built out
# of cells. Block elements and box drawing, not ASCII - measured 2026-08-10 from
# the bundled Google Sans Code's cmap, U+2580-259F is 32 of 32 and U+2500-257F
# is 128 of 128, against Cascadia Mono's 32 of 32 and 128 of 128 as a control.
# The face is the full block; the shadow outline carries the bevel, and painted
# in a darker blue it reads as depth.
#
# BANNER_SHADE holds every shadow cell, so the caller can colour the two apart.
BANNER_SHADE = "═║╔╗╚╝"
BANNER = """
    ██████╗██████╗  ██████╗ ██╗    ██╗
   ██╔════╝██╔══██╗██╔═══██╗██║    ██║
   ██║     ██████╔╝██║   ██║██║ █╗ ██║
   ██║     ██╔══██╗██║   ██║██║███╗██║
   ╚██████╗██║  ██║╚██████╔╝╚███╔███╔╝
    ╚═════╝╚═╝  ╚═╝ ╚═════╝  ╚══╝╚══╝
   {version}
"""


def paint_banner(text: str) -> str:
    """Two blues: the face in the wordmark colour, the bevel a few steps down."""
    if not _TTY:
        return text
    shaded = "".join(
        f"{BANNER_BEVEL}{ch}{CROW_ACCENT}" if ch in BANNER_SHADE else ch
        for ch in text
    )
    return f"{CROW_ACCENT}{shaded}{RESET}"


# The three commands sit BESIDE the wordmark, one per line. Stacked underneath
# they cost three lines of a header that is already four, and the mark is 38
# columns of a terminal that has at least 80 -- the space was there the whole
# time.
#
# The name is painted in the same yellow a slash command turns while it is being
# typed (see read_line). One colour for one thing: the header is where the user
# learns what the prompt will do, so it has to look like the prompt.
HEADER_COMMANDS = (
    ("/help", "for commands"),
    ("/tools", "for what the model can call"),
    ("/mode", "manual, allowedit or auto"),
    ("/exit", "to leave"),
)
BANNER_GAP = 4


def header_lines(version: str) -> list[str]:
    """The wordmark and the commands as one block, already painted.

    The column is measured off the widest banner row rather than written down,
    so the commands do not drift when the mark changes -- which it just did.

    Painted per line, not once around the whole block: wrapping the join would
    leave every line but the first without its opening colour.
    """
    raw = BANNER.format(version=version).splitlines()
    width = max((len(line.rstrip()) for line in raw), default=0)

    # The right column in order: the commands, a blank, the repository.
    # The blank is a REAL entry rather than an offset, so the gap survives a
    # command being added or taken away.
    #
    # THE BUDGET IS THE WORDMARK'S HEIGHT, and it is nearly spent. Five rows
    # carry the mark, so four commands + blank + URL is six slots: the commands
    # still land on the mark, and the URL moves down onto the bevel row. A FIFTH
    # command pushes the URL onto the version line, which is the one thing the
    # centring below exists to prevent. Adding one means shortening the list or
    # growing the mark, not editing this tuple alone.
    column = [f"{YELLOW}{name}{RESET}{DIM} {what}{RESET}"
              for name, what in HEADER_COMMANDS]
    column += ["", f"{CROW_ACCENT}{REPO_URL}{RESET}"]

    # Centred against the WORDMARK, not against the whole block, so nothing ever
    # lands beside the version line.
    marks = [i for i, line in enumerate(raw) if "█" in line]
    start = marks[0] + max(0, (len(marks) - len(column) + 1) // 2) if marks else 0

    out = []
    for i, line in enumerate(raw):
        painted = paint_banner(line)
        slot = i - start
        if 0 <= slot < len(column) and column[slot]:
            pad = " " * (width - len(line.rstrip()) + BANNER_GAP)
            painted += pad + column[slot]
        out.append(painted)
    # Two blank lines under the version: the header is a block, and the endpoint
    # below it is a different statement.
    return out + ["", ""]

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


class TerminalEvents(ReplyEvents):
    """The screen on the far side of the core's seam. Eleven statements.

    THIS IS WHAT `stream_reply` USED TO DO ITSELF, moved rather than rewritten:
    a Renderer and a Raven built before the first byte is asked for, the switch
    from thinking to writing on the first content delta, one `feed` per delta,
    and a `close()`/`stop()` pair at the end. `crow_core.stream_reply` calls
    these four in exactly the places the eleven lines used to sit, so the
    terminal sees character for character what it saw before.

    THE LAST TWO ARE ONE PIECE AND MAY NOT BE SPLIT. `reply_finished` runs from
    the core's `finally`, so it also runs when the turn raises and when Ctrl+C
    lands mid-stream. `close()` flushes the tail, ends an unterminated fence and
    shuts the spill file; `stop()` wipes the line the bird drew. Half of that is
    an open spill file, a half-drawn code block and a bird still flapping over
    the traceback.

    The other two of the thirteen are the parameters: `out` is the sink the old
    signature carried as `out=sys.stdout`, `prefix` the one it carried as
    `prefix: str = ""`.

    THE THIRD PARAMETER IS E10's, AND IT IS PURE DISPLAY. `show_reasoning`
    decides whether the core's three thought events reach the screen; it decides
    nothing about where a block begins or ends, because that decision is
    `crow_core.ReasoningBlocks` and a window is about to make the same one.
    OFF, every one of the three returns before it writes a character, which is
    the whole idempotence promise of the stage: the terminal prints what it
    printed before E10, byte for byte.
    """

    def __init__(self, out=None, prefix: str = "", show_reasoning: bool = False) -> None:
        self._out = out or sys.stdout
        self._prefix = prefix
        self._show = show_reasoning
        self._renderer: Renderer | None = None
        self._raven: Raven | None = None
        # Whether the bird is still up. Without the thought blocks it always was
        # until `answer_started`; with them it goes at the first block, and the
        # switch has to be remembered rather than asked of the bird, or the
        # "writing" frame below would be slept through a second time.
        self._flying = False
        # Whether the cursor sits at column 0. Tracked, not asked: the Renderer
        # writes prose through character by character and knows, but a thought
        # that reopens mid-line has to break the line before its rule and the
        # only thing that has seen both streams is this object. Worst case
        # inside an unfinished code line is one blank line, never a lost one.
        self._at_line_start = True

    def reply_started(self) -> None:
        self._renderer = Renderer(out=self._out)
        self._raven = Raven(stream=self._out, label="thinking")
        self._raven.__enter__()
        self._flying = True
        self._at_line_start = True

    def answer_started(self) -> None:
        # One frame of the new state before the bird goes, so
        # the switch from thinking to writing is visible.
        if self._flying:
            self._raven.set_label("writing")
            time.sleep(self._raven._interval)
            self._raven.stop()
            self._flying = False
        if self._prefix:
            self._out.write(self._prefix)
            self._at_line_start = self._prefix.endswith("\n")

    def answer_text(self, piece: str) -> None:
        self._renderer.feed(piece)
        self._at_line_start = piece.endswith("\n")

    def reply_finished(self) -> None:
        self._renderer.close()
        self._raven.stop()
        self._flying = False

    def _break_line(self) -> None:
        """Start the rule on a line of its own, without leaving a blank one."""
        if not self._at_line_start:
            self._out.write("\n")
            self._at_line_start = True

    def reasoning_started(self, index: int) -> None:
        """Open a thought block on screen. Same rule as a code fence, on
        purpose: the terminal gets one vocabulary for "a block starts here",
        and the label is what says which kind. The label counts, because the
        second block is the one a "think first, then answer" client cannot
        produce -- if it ever reads `thinking again (2)`, the state machine
        under it is doing what it was built for."""
        if not self._show:
            return
        # The bird covered the silence. There is none once the thoughts
        # themselves are on the screen, and a spinner drawn with cursor-up
        # escapes over streaming text is two writers on one line.
        if self._flying:
            self._raven.stop()
            self._flying = False
        self._break_line()
        label = " thinking" if index == 1 else f" thinking again ({index})"
        self._out.write(DIM + "-" * 3 + label + " "
                        + "-" * max(0, Renderer.WIDTH - 5 - len(label)) + RESET + "\n")
        self._out.flush()

    def reasoning_text(self, piece: str) -> None:
        """The thought itself, dim and verbatim.

        NOT THROUGH THE RENDERER, and that is not an oversight: a fence inside
        the reasoning would open a code block that the answer then has to close,
        and the model reasons ABOUT code all day. Dim is what tells the reader
        this is not the answer."""
        if not self._show:
            return
        self._out.write(DIM + piece + RESET)
        self._out.flush()
        self._at_line_start = piece.endswith("\n")

    def reasoning_finished(self) -> None:
        if not self._show:
            return
        self._break_line()
        self._out.write(DIM + "-" * Renderer.WIDTH + RESET + "\n")
        self._out.flush()


# `stream_reply` IS THE CORE'S, RE-EXPORTED -- there is no wrapper here any more.
#
# Until 2026-08-13 this file defined one: same name, same signature plus `out`,
# `prefix` and `show_reasoning`, whose whole body was to build a TerminalEvents
# and hand it over as `events`. It read as a convenience and cost nothing to
# call, and `tools/check_shared_core.py` still counted it as what it was --
# `stream_reply` defined twice, once here and once in the core. A name that
# means two things is the second truth this whole stage exists to prevent, and a
# checker that made an exception for the comfortable case would make one for the
# next case too.
#
# So the three terminal parameters moved to the callers, where they were always
# the caller's business: whoever wants this screen passes
# `events=TerminalEvents(out=..., prefix=..., show_reasoning=...)`, and whoever
# wants none passes no events at all.


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
    #
    # NO ADVICE ATTACHED, deliberately. This line used to end in "raise
    # --max-tokens". There is no such flag: build_parser never had one and the
    # request body carries no max_tokens, so the server runs on its own default.
    # Telling someone to turn a knob that does not exist is worse than saying
    # nothing -- they go looking for it. If a knob is added, name it here again.
    if timings.get("_finish_reason") == "length":
        bits.append(crow_core.CUT_OFF_NOTE)
    return " | ".join(bits)


class TerminalTurnEvents(TurnEvents):
    """The screen on the far side of the tool loop's seam. Twelve prints.

    THIS IS WHAT `repl()` USED TO DO ITSELF, moved rather than rewritten: every
    print statement of the loop stands here with the characters it always had,
    fired from the place in `crow_core.run_turn` where it used to sit. The loop
    is somewhere else now; the terminal is not supposed to be able to tell.

    `reply_events` is the thirteenth line of the OTHER seam: the prompt prefix
    the answer is written behind. It is built fresh for every round because a
    `TerminalEvents` owns a Renderer and a Raven, and those are per stream.

    `rounds` is `--rounds`. It does not decide whether the event fires -- the
    core reports every round -- only whether the figures are printed. The
    newline is printed either way, which is what the line it replaced did.

    `show_reasoning` is handed straight down to the reply sink and read nowhere
    else here. It is a per-TURN value rather than a per-session one because
    `/thoughts` may flip it between turns, and `repl()` builds this object once
    per turn for exactly that reason.
    """

    def __init__(self, rounds: bool = False, out=None, show_reasoning: bool = False) -> None:
        self._rounds = rounds
        self._out = out or sys.stdout
        self._show_reasoning = show_reasoning

    def reply_events(self) -> ReplyEvents:
        return TerminalEvents(out=self._out, prefix=f"{BOLD}{CROW_TEXT}crow>{RESET} ",
                              show_reasoning=self._show_reasoning)

    def turn_failed(self, message: str) -> None:
        print(f"\ncrow: {message}\n", file=sys.stderr)

    def turn_interrupted(self) -> None:
        print(f"\n{crow_core.ABORT_NOTE}\n", file=self._out)

    def round_finished(self, timings: dict) -> None:
        line_out = format_timings(timings) if self._rounds else ""
        print(f"\n\n[{line_out}]\n" if line_out else "\n", file=self._out)

    def cache_promise_broken(self) -> None:
        print(f"{DIM}[the restored cache did not hold -- that prefill was the whole "
              f"conversation, not a resume]{RESET}\n", file=self._out)

    def budget_spent(self, budget: int) -> None:
        print(f"{DIM}[tool budget spent after {budget} rounds -- answering from what it "
              f"has; --max-tool-rounds raises it]{RESET}\n", file=self._out)

    def tool_started(self, name: str, arguments: str) -> None:
        arg_note = format_tool_args(arguments)
        # PRINTED BEFORE THE CALL RUNS, and that is the fix rather than a detail (#70).
        # Until here the order on screen was: run the tool, then say what was run. A slow
        # call left the terminal silent for its whole duration with nothing naming what it
        # was waiting on -- and the previous round's six figures as the last thing visible.
        # No newline, so the outcome lands on the same line when it comes back.
        print(f"{DIM}  ● {name}({arg_note})", end="", flush=True, file=self._out)

    def tool_finished(self, name: str, seconds: float, repeated: bool) -> None:
        marks = []
        if repeated:
            marks.append("repeat")
        # Sub-second calls are the cache answering; printing 0.0s for those adds a number
        # per line and says nothing.
        if seconds >= 0.05:
            marks.append(format_clock(seconds))
        note = (" -- " + ", ".join(marks)) if marks else ""
        print(f"{note}{RESET}", file=self._out)

    def tool_failed(self, name: str, result: str) -> None:
        print(f"{DIM}    {result.splitlines()[0]}{RESET}", file=self._out)

    def tools_finished(self) -> None:
        print("", file=self._out)

    def tools_reported(self, calls: list[dict]) -> None:
        """The one line with no history behind it: --no-run-tools.

        Same shape as a call that ran, and deliberately so -- the reader is
        looking at the same list either way and the difference has to be the
        thing that stands out, not the layout.
        """
        for call in calls:
            arg_note = format_tool_args(call["arguments"])
            print(f"{DIM}  ● {call['name']}({arg_note}) -- reported, not run{RESET}",
                  file=self._out)
        print("", file=self._out)

    def rolled_over(self, tokens: int, path: str) -> None:
        print(f"{DIM}context rolled over at {tokens} tokens mid-turn -- "
              f"archived to {path}{RESET}\n", file=self._out)

    def rollover_refused(self) -> None:
        print(f"{DIM}[the window filled again inside this turn -- stopping here."
              f" Ask for a narrower slice, or /reset]{RESET}\n", file=self._out)


HELP = """commands:
  /help          this list
  /tools         the tools the model can call
  /mode          the release level, /mode manual|allowedit|auto to switch
  /thoughts      show the model's reasoning as it arrives, or hide it again
  /reset         drop the context (costs a full re-prefill)
  /context       message count in the current context
  /exit, /quit   leave
"""


class SlashResult(NamedTuple):
    """What a slash command did: whether it ran, and the switches it moved.

    A TUPLE RATHER THAN A STATE OBJECT, deliberately. repl() reads
    context_tokens in eleven places and hands it to run_turn; wrapping it in an
    object to satisfy one command would rewrite all eleven for the benefit of
    none of them. Four names in, four names out, and the caller keeps its own
    variables.
    """
    handled: bool
    mode: str
    show_reasoning: bool
    context_tokens: int


def run_slash(line: str, *, conversation, mode: str, show_reasoning: bool,
              context_tokens: int, n_ctx: int, rollover_at: float,
              session: bool = True) -> SlashResult:
    """Every slash command except the two that leave. Prints its own output.

    OUT OF repl() BECAUSE THE SUITE ASKED, and it asked for a reason worth
    keeping: `test_repl_is_one_job_again` caps repl() at 220 lines, so that the
    five-job block the 0.3.0 split took apart cannot quietly grow back. Reading
    a key, deciding a turn and formatting seven commands is three jobs; this is
    the third one, moved rather than rewritten -- every branch below is the one
    that stood in the loop, character for character where it prints.

    /exit and /quit STAY in repl(): they call leave(), which closes over the
    session file and the arguments, and dragging that out here would move the
    session's whole lifetime with it.
    """
    if line == "/help":
        print(HELP)
        return SlashResult(True, mode, show_reasoning, context_tokens)

    if line == "/tools":
        print(format_tools())
        return SlashResult(True, mode, show_reasoning, context_tokens)

    if line == "/thoughts":
        # A TOGGLE AND NOT TWO COMMANDS: whoever wants to see the thoughts
        # wants to stop seeing them again two questions later, and a pair
        # of names would be two things to remember for one decision. The
        # answer says which way it went, because a switch that flips in
        # silence is indistinguishable from one that did not take.
        show_reasoning = not show_reasoning
        print("the model's reasoning is shown as it arrives.\n" if show_reasoning
              else "the model's reasoning is hidden again -- "
                   "the bird carries the state.\n")
        return SlashResult(True, mode, show_reasoning, context_tokens)

    if line == "/mode" or line.startswith("/mode "):
        mode, said = switch_mode(line, mode)
        print(said)
        return SlashResult(True, mode, show_reasoning, context_tokens)

    if line == "/reset":
        conversation.reset()
        crow_core.forget_approvals()   # #88: the chat goes, its releases go
        # AND THE DISK, NOT ONLY THE OBJECT. `save_session` refuses to write an
        # empty conversation, so a `/reset` followed by `/exit` used to leave
        # the file from before the reset in place and the next start restored
        # it. Measured 2026-08-14 in the window; the guard is in the core, so it
        # was true here too and had been since `/reset` existed.
        if session:
            crow_core.forget_session()
        print("context dropped -- the next turn pays a full prefill.\n")
        return SlashResult(True, mode, show_reasoning, 0)

    if line == "/context":
        # The rollover point is shown here or nowhere: it is the number that
        # decides when the conversation ends, and a user who cannot see it
        # cannot plan around it.
        enabled = n_ctx > 0 and rollover_at > 0
        room = f", rolls over at {int(n_ctx * rollover_at)}" if enabled else ""
        print(f"{len(conversation)} messages, {context_tokens} tokens{room}\n")
        return SlashResult(True, mode, show_reasoning, context_tokens)

    return SlashResult(False, mode, show_reasoning, context_tokens)


def switch_mode(line: str, mode: str) -> tuple[str, str]:
    """#88's `/mode`: report the level, or switch it. Returns (mode, what to say).

    OUT HERE RATHER THAN IN repl(), and the suite is what said so: repl() has a
    220-line ceiling (`test_repl_is_one_job_again`) precisely so it does not
    grow back into the five-job block the split took apart. A command that
    formats output and holds a rule belongs beside format_tools(), not inside
    the loop that reads a key.

    BOTH FORMS PRINT WHAT IS LIVE. "A release level nobody can see is one nobody
    can trust" -- so `/mode` alone reports rather than staying silent, and a
    switch names what it now holds back rather than only its own name.
    """
    wanted = line[len("/mode"):].strip().lower()
    if wanted and wanted not in crow_core.MODES:
        return mode, (f"{DIM}no mode named {wanted!r}. "
                      f"one of: {', '.join(crow_core.MODES)}{RESET}\n")

    dropped = ""
    if wanted:
        mode = wanted
        # A LEVEL CHANGE DROPS STANDING APPROVALS. Switching to `manual` while
        # keeping the directories released under `allowedit` would hand back a
        # level that asks less than its name says.
        crow_core.forget_approvals()
        dropped = f"\n{DIM}standing approvals dropped{RESET}"

    asks = [t for t in sorted(crow_core.TOOL_IMPL)
            if crow_core.needs_approval(t, mode)]
    what = f"asks before {', '.join(asks)}" if asks else "every tool runs unasked"
    return mode, f"mode {mode} -- {what}{dropped}\n"


def ask_approval(name: str, arguments: str) -> str:
    """#88: put one held-back call to the user. "yes", "no" or "always".

    ASKED BETWEEN ROUNDS, NOT DURING ONE, and that is why this is a plain read
    rather than a second reader fighting the raw-mode line editor. The ticket
    budgets its estimate for "asking mid-turn", but the tool loop only reaches a
    call once `stream_reply` has returned: the answer is complete, nothing is
    arriving, and the terminal is idle. Measured against the loop's own order in
    cli/crow_core.py, not assumed.

    THE PROMPT SHOWS WHAT IT RELEASES -- #88 point 2. A prompt that only says
    `run_command?` is a keystroke, not a decision, so the arguments are printed
    the way the model sent them, cut at a length that still fits a line.

    Anything that is not y/a is no. A misread key must not release a shell.
    """
    try:
        args = json.loads(arguments or "{}")
        detail = ", ".join(f"{k}={v!r}" for k, v in args.items())
    except (json.JSONDecodeError, AttributeError):
        detail = arguments or ""
    if len(detail) > 300:
        detail = detail[:297] + "..."

    scope = crow_core.approval_scope(name, arguments)
    always = f", {YELLOW}a{RESET}{DIM}lways for {scope[1]}{RESET}" if scope else ""
    print(f"\n{YELLOW}  {name}{RESET}{DIM}({detail}){RESET}")
    try:
        answer = input(f"  run it? {YELLOW}y{RESET}es / {YELLOW}n{RESET}o"
                       f"{always}{DIM} [n]{RESET} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "no"
    if answer in ("y", "yes"):
        return "yes"
    if answer in ("a", "always") and scope:
        return "always"
    return "no"


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


def repl(args: argparse.Namespace) -> int:
    enable_ansi()
    install_interrupt_handler()
    # First thing, before anything is drawn: the request then runs while the
    # banner, the font check and /health do their work, and is usually answered
    # by the time anyone looks at the queue.
    updates = start_update_check(getattr(args, "update_check", True))
    if getattr(args, "background", True):
        set_background()
    for line in header_lines(f"v{VERSION}"):
        print(line)

    # Before the endpoint check, not after: the font has nothing to do with the
    # server. Behind the check it would never install on a machine where the
    # user starts the CLI before llama-server, which is the normal order.
    if getattr(args, "font", True):
        ensure_font()

    # #92: the working directory, bound before anything can write. The rule is
    # `adopt_root` in the core, because the window decides the same thing and a
    # rule written twice is the divergence `check_shared_core` cannot see.
    here, args.mode, problem = adopt_root(getattr(args, "root", None),
                                          getattr(args, "mode", None))
    if problem:
        print(f"crow: {problem}", file=sys.stderr)
        return 2
    if here:
        print(f"{DIM}working directory: {here}{RESET}")

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
    # Under the address, because that is the pair: this endpoint, that model.
    loaded = fetch_model_name(args.base_url)
    if loaded:
        print(f"{CROW_ACCENT}{loaded}{RESET}")
    # The repository used to be printed here. It sits beside the wordmark now,
    # under the commands, so the endpoint block is the endpoint and the model.
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
    # --show-reasoning is the START value, /thoughts is the same switch during
    # the session -- one variable, or the flag and the command would disagree
    # about the same turn. Nothing else in here reads it: it is handed to the
    # turn's sink and decides display, never what is sent, kept or counted.
    show_reasoning = bool(getattr(args, "show_reasoning", False))

    mode = getattr(args, "mode", crow_core.DEFAULT_MODE)   # #88, see switch_mode()

    # Before the first turn, so no prefix exists yet to break.
    wanted = getattr(args, "resume", None)
    if getattr(args, "session", True) or wanted:
        source = resume_path(wanted) if wanted else None
        try:
            restored = load_session(args.base_url, args.system, path=source)
        except SessionFormatError as exc:
            # REFUSING TO START IS THE POINT, and starting anyway would be the
            # quiet version of the same loss: this run would build a session,
            # leave() would refuse to write it over a file it cannot read, and
            # the whole conversation would go at exit instead of at the start.
            # The two lines say what is there and what the two ways out are.
            print(f"crow: {exc}", file=sys.stderr)
            print("crow: nothing was read and nothing was written. Move that file"
                  " aside, or start with --no-session to leave it where it is.",
                  file=sys.stderr)
            reset_background()
            return 2
        if restored:
            messages, context_tokens, kv = restored
            conversation.restore(messages)
            promised_warm = kv
            how = "cache warm" if kv else crow_core.RESUME_COLD_NOTE
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
            try:
                note = save_session(conversation, args.base_url, context_tokens)
            except SessionFormatError as exc:
                # Reachable even though the start path refuses such a file:
                # --resume reads an ARCHIVE and this writes the live
                # session.json, which nothing on the way in ever looked at. The
                # file can also be replaced while the client is running.
                # stderr rather than the dim note line -- this one says a
                # session was NOT written, and it must not read like the four
                # sentences beside it that say one was.
                print(f"crow: {exc}", file=sys.stderr)
                return 0
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
        slash = run_slash(line, conversation=conversation, mode=mode,
                          show_reasoning=show_reasoning,
                          context_tokens=context_tokens, n_ctx=n_ctx,
                          rollover_at=args.rollover_at, session=args.session)
        mode, show_reasoning = slash.mode, slash.show_reasoning
        context_tokens = slash.context_tokens
        if slash.handled:
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

        # THE TOOL LOOP LIVES IN THE CORE (E5). What is left here is the screen
        # and the two decisions above it: what the user typed, and which of the
        # slash commands it was. `crow_core.run_turn` owns the four rules that
        # used to stand in this function -- calls that will never run are not
        # appended, a spent budget buys one forced round, the window is checked
        # at the end of every round, and a second rollover inside one turn stops
        # it -- and it owns them for every surface, not just this one.
        turn = run_turn(
            conversation,
            base_url=args.base_url,
            model=args.model,
            api_key=args.api_key,
            temperature=args.temperature,
            top_p=args.top_p,
            min_p=args.min_p,
            reasoning_effort=args.reasoning_effort,
            timeout=args.timeout,
            carry=line,
            context_tokens=context_tokens,
            n_ctx=n_ctx,
            rollover_at=args.rollover_at,
            max_tool_rounds=args.max_tool_rounds,
            promised_warm=promised_warm,
            rolled=rolled,
            execute_tools=getattr(args, "run_tools", True),
            mode=mode,
            approve=ask_approval,
            events=TerminalTurnEvents(rounds=args.rounds, show_reasoning=show_reasoning),
        )
        # The three the loop wrote through while it was a block of this
        # function. They are carried between turns here, which is the one job
        # `repl()` still has that a second surface cannot do for it.
        context_tokens = turn.context_tokens
        promised_warm = turn.promised_warm
        rolled = turn.rolled
        cost = turn.cost
        stopped = turn.stopped

        # ONE LINE PER USER TURN, where twelve to twenty-four used to be (#70). Not printed for a
        # turn that was interrupted or died on an error: those already say what happened, and a
        # cost line under them would read like a completed turn.
        if not stopped and cost.rounds:
            print(f"{DIM}[{cost.line()}]{RESET}\n")

        if stopped:
            continue


# Values we wrote ourselves in earlier versions and may correct without asking.
# Anything else in the user's settings is their choice and stays.
_OUR_OLD_FACES = frozenset({"Google Sans Code"})
_OUR_OLD_BACKGROUNDS = frozenset({CROW_BG})


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
    # THE THREE VALUES AND THEIR REASONS MOVED TO crow_core, TOGETHER. They were
    # written here AND in `stream_reply`'s signature AND in the manifest, and
    # tools/check_operating_point.py counts rather than compares for exactly that
    # reason: two clients that both hard-write 0.95 agree with each other right
    # up to the day one of them is edited. With a window beside this file that
    # stopped being a hypothetical, so the numbers live in the core and every
    # client reads them. The measurements behind each one moved with it.
    parser.add_argument("--temperature", type=float, default=TEMPERATURE)
    parser.add_argument("--top-p", dest="top_p", type=float, default=TOP_P)
    parser.add_argument("--min-p", dest="min_p", type=float, default=MIN_P)
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
    # NOT REMOVED, MOVED BEHIND A SWITCH (#70). The per-round line is the instrument this loop was
    # built with -- it is what showed the prefix holding round by round. Deleting it would cost the
    # next person debugging the cache the only view they had; leaving it on cost every user twelve
    # lines per question. So it stays, off by default.
    parser.add_argument("--rounds", dest="rounds", action="store_true",
                        help="print the full timing line after every tool round, not just the"
                             " one-line summary at the end of the turn")
    # E10, AND IT IS OFF BY DEFAULT FOR A MEASURED REASON. Reasoning is 60-90 %
    # of every answer this model gives -- 88.2 % of every generated character
    # over the 30 stored answers of the 2026-08-07 reference run. Shown by
    # default it buries the code the user asked for. Not shown AT ALL, which is
    # what the CLI did until this flag, it hides most of what the machine spent
    # its minutes on and leaves the terminal a second-class client the moment a
    # window can display it. `/thoughts` is the same switch inside a session.
    parser.add_argument("--show-reasoning", dest="show_reasoning", action="store_true",
                        help="print the model's reasoning as it streams, in its own dim"
                             " block; /thoughts turns it on and off during a session")
    # Raising this raises what ONE turn can add to the window: 24 rounds were
    # measured on 2026-08-10 growing a turn by 28,900 tokens, which is more than
    # the 20,000 that --rollover-at 0.9 leaves between the threshold and the
    # wall. The two settings are one setting with two names.
    parser.add_argument("--max-tool-rounds", dest="max_tool_rounds", type=int,
                        default=MAX_TOOL_ROUNDS, metavar="N",
                        help="how many tool rounds one turn may take before it stops"
                             f" (default: {MAX_TOOL_ROUNDS}, 0 answers without running any)")
    # THE OPERATING MODE E5 ADDED, AND IT IS NOT --max-tool-rounds 0.
    #
    # That one still runs the loop: the budget is spent on the first round, the
    # calls are refused, and a second forced round is bought to say where things
    # stood. This one does not spend anything -- one round, the calls are handed
    # back instead of run, and the turn is over.
    #
    # The tools stay in the REQUEST either way, and that is the whole reason the
    # mode exists rather than a `--no-tools` that empties the array: this model's
    # template keeps a previous turn's thoughts only while `tools` is non-empty,
    # measured 2026-08-08 over /apply-template at 132 characters against 132
    # without them. A client that drops the declarations to stop the calls pays
    # for it in a re-read prefix on every later turn.
    parser.add_argument("--no-run-tools", dest="run_tools", action="store_false",
                        help="report the tool calls the model asks for instead of running"
                             " them; the turn then ends after one round")
    # #88. The START value; /mode is the same switch during a session. `auto`
    # is what every release up to 0.3.1 did, so the default changes nothing for
    # anyone who does not ask for a level.
    # DEFAULT None, NOT `auto`, and the default is still auto -- `repl` resolves
    # it. The parser is the only place that can distinguish "the user typed auto"
    # from "the user typed nothing", and #92 needs that difference: a level
    # remembered for a working directory may fill a silence, never overrule a
    # flag. Nothing downstream sees None; `repl` sets it before the first turn.
    parser.add_argument("--mode", choices=crow_core.MODES, default=None,
                        help="release level for tool calls: manual asks before writing"
                             " and executing, allowedit asks before executing, auto asks"
                             " for nothing (default, unless the working directory"
                             " remembers another)")
    parser.add_argument("--root", default=None,
                        help="the directory tool writes are confined to. States it"
                             " AND creates it: writes .crow/root.json, which is what"
                             " makes a directory a root. Without this, crow adopts a"
                             " root declared above the working directory, or runs"
                             " unbounded if there is none")
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


