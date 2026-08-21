#!/usr/bin/env python3
"""Suite for the split itself: cli/crow.py against cli/crow_core.py.

Run:  python cli/test_crow_core.py

cli/test_crow.py checks what the client DOES, and it goes on doing that
unchanged -- the move is invisible to it, which is the whole promise of the
re-export. What it cannot see is the move going half-way, and that is what this
file is for. Two failures are possible here that no behaviour test can reach:

  * the version literal leaving cli/crow.py, which makes every installed base
    un-updatable through the documented one-liner and shows up on nobody's
    screen until an update is attempted;
  * a module global existing TWICE, once in each file, so that a caller who
    rebinds one of them changes a name the code no longer reads. That failure
    is silent by construction: both halves work, they just work on different
    state.
  * the reply seam being wired up but not closed down -- `stream_reply` reports
    its events and the caller's `reply_finished` never runs, so the spill file
    stays open, the code block stays half drawn and the bird keeps flapping.
    cli/test_crow.py drives `crow.stream_reply`, which still takes `out` and
    `prefix` and still prints the same characters, so the whole seam is
    invisible to it: every one of its cases passes with the events object
    working and with it silently doing nothing at all.
  * the TOOL LOOP losing one of its four rules on the way out of `repl()`. That
    function was called by 0 tests before this stage -- `grep -c "crow.repl"
    cli/test_crow.py` answered 0 -- so every rule in it was unpinned, and the
    most dangerous of them has no visible effect inside the turn that breaks it.
    See `UnanswerableCallsTests`.

Standard library only, same as everything else here.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import crow        # noqa: E402
import crow_core   # noqa: E402

# THE PALETTE IS PINNED FOR THIS WHOLE MODULE (#102). `crow_core._TTY` is decided
# ONCE, at import, out of `sys.stdout.isatty()`, and the colour constants are
# materialised from it on the spot -- so this file answered differently in a
# console than through a pipe, with no line of code between the two runs. One
# case here compared a bare string against an escape sequence and was red on
# robin's machine while green in every automated run.
#
# DERIVED, NEVER LISTED: every module-level string beginning with ESC. A
# hard-coded palette in a test is a copy of the product that goes stale silently.
#
# BOTH MODULES, because `crow.py` re-exports by VALUE -- patching
# `crow_core.DIM` leaves `crow.DIM` untouched, and this file holds both.
#
# The positive direction -- a terminal still gets its colour -- is asked by
# `ThePaletteFollowsTheTerminalTests` below, which cannot use this fixture: it
# needs an import that has not happened yet.
_PINNED: dict = {}


def setUpModule() -> None:
    for module in (crow, crow_core):
        for name, value in list(vars(module).items()):
            if isinstance(value, str) and value.startswith("\033"):
                _PINNED[(module.__name__, name)] = value
                setattr(module, name, "")
        # AND THE FLAG WITH IT. An emptied palette beside a `_TTY` that still
        # says "terminal" is a state the product can never be in, and a case
        # that branches on the flag would then assert against the emptiness
        # this fixture created.
        if hasattr(module, "_TTY"):
            _PINNED[(module.__name__, "_TTY")] = module._TTY
            module._TTY = False


def tearDownModule() -> None:
    for (module_name, name), value in _PINNED.items():
        setattr(sys.modules[module_name], name, value)
    _PINNED.clear()


def _source(name: str) -> str:
    with io.open(HERE / name, encoding="utf-8") as fh:
        return fh.read()


class ThePaletteFollowsTheTerminalTests(unittest.TestCase):
    """#102's other half: the repair must not be "switch the colour off".

    The nine cases that ticket repaired are pinned to the COLOURLESS palette, so
    a change that disabled colour everywhere would leave every one of them green
    -- and take the product's colour with it, invisibly. Two checkers reporting
    the same thing for opposite reasons is the shape this project has already
    been bitten by; this is the case that goes red for exactly that.

    IT IMPORTS THE CORE AGAIN rather than patching the shared one. `_TTY` is read
    at import and never again, so the only honest way to ask "what does this
    module look like on a terminal" is to give it a terminal and import it. The
    two module objects below are private to this case -- neither is the one the
    rest of the suite holds, and neither outlives it.
    """

    @staticmethod
    def _imported_with_tty(is_tty: bool):
        class _Stdout:
            def __init__(self, real) -> None:
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def isatty(self) -> bool:
                return is_tty

        spec = importlib.util.spec_from_file_location(
            "crow_core_tty_%s" % is_tty, crow_core.__file__)
        module = importlib.util.module_from_spec(spec)
        real = sys.stdout
        sys.stdout = _Stdout(real)
        try:
            spec.loader.exec_module(module)
        finally:
            sys.stdout = real
        return module

    def test_a_terminal_gets_the_palette_and_a_pipe_gets_none(self):
        """POSITIVE and NEGATIVE in one run, which is what makes it worth having.

        The floor catches "colour switched off globally" -- the repair that
        passes every other case in this suite and quietly ships a grey client.
        The loop catches the opposite: escape sequences leaking into a redirected
        transcript, which is the thing `crow_core.py:310` exists to prevent and
        the reason the gate is there at all.
        """
        on = self._imported_with_tty(True)
        off = self._imported_with_tty(False)

        coloured = sorted(name for name, value in vars(on).items()
                          if isinstance(value, str) and value.startswith("\033"))
        self.assertGreaterEqual(
            len(coloured), 10,
            "a terminal got %d escape sequences: the colour was switched off "
            "globally, and every case pinned to the colourless palette stayed "
            "green while it happened" % len(coloured))
        self.assertTrue(on._TTY)
        self.assertFalse(off._TTY)
        for name in coloured:
            self.assertEqual(
                getattr(off, name), "",
                "%s carries an escape sequence through a pipe -- a redirected "
                "transcript is no longer greppable" % name)


class VersionStaysInTheClientTests(unittest.TestCase):
    """install.ps1:399-403 reads the installed version out of cli\\crow.py with
    ^VERSION\\s*=\\s*"([^"]+)". A miss returns $null, Resolve-InstallAction
    answers 'unknown' (install.ps1:428-431) and refuses with advice to pass
    -Force -- which `irm ... | iex` cannot pass. Measured on a throwaway copy:
    with the literal moved into the core, check_operating_point reports
    "cli/crow.py None" and exits 1, and Get-InstalledVersion returns NULL."""

    PATTERN = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.M)

    def test_the_installer_can_read_the_version_out_of_crow_py(self):
        found = self.PATTERN.search(_source("crow.py"))
        self.assertIsNotNone(found, "install.ps1 would read $null here")
        self.assertEqual(found.group(1), crow.VERSION)

    def test_the_core_declares_no_version_of_its_own(self):
        """The negative half. A second literal is a second thing to bump, and
        the one that goes stale is the one no release step reads."""
        self.assertFalse(hasattr(crow_core, "VERSION"),
                         "the version literal may live in cli/crow.py only")
        self.assertIsNone(self.PATTERN.search(_source("crow_core.py")))

    def test_the_client_hands_its_version_to_the_core(self):
        """Three places in the core need one: the session file's `version`
        field, the release check's User-Agent, and the update notice."""
        self.assertEqual(crow_core.CLIENT_VERSION, crow.VERSION)

    def test_a_core_that_was_told_nothing_announces_nothing(self):
        """The empty default is load-bearing, not a placeholder: a client that
        forgot to hand its version over must stay quiet rather than tell every
        user that an update is available."""
        self.assertFalse(crow_core.is_newer("9.9.9", ""))


class OneStateNotTwoTests(unittest.TestCase):
    """The half-move the behaviour suite cannot see.

    _READ, _SEEN and INTERRUPT are the three module globals of this client that
    carry state between calls. If a block moved and one of them stayed behind,
    both files would hold a name that works -- on different objects -- and every
    existing test would still pass.
    """

    def test_the_read_set_is_one_object(self):
        self.assertIs(crow._READ, crow_core._READ)

    def test_the_seen_cache_is_one_object(self):
        self.assertIs(crow._SEEN, crow_core._SEEN)

    def test_the_interrupt_flag_is_one_object(self):
        """repl() clears it and _post_stream reads it. Two Events here means a
        Ctrl+C that is set in one file and never seen in the other."""
        self.assertIs(crow.INTERRUPT, crow_core.INTERRUPT)

    def test_a_write_through_the_client_reaches_the_core(self):
        """MEASURED, not theoretical. With the module class taken away, 11 of
        cli/test_crow.py's 224 cases fail: they redirect SESSION_DIR,
        SESSION_FILE, post_json and FONT_DIR at the module and then call code
        that now reads them from the core."""
        for name in ("SESSION_DIR", "SESSION_FILE", "post_json", "FONT_DIR", "_TTY"):
            saved = getattr(crow, name)
            marker = object()
            try:
                setattr(crow, name, marker)
                self.assertIs(getattr(crow_core, name), marker,
                              f"{name} would be two states under one name")
            finally:
                setattr(crow, name, saved)
            self.assertIs(getattr(crow_core, name), saved, f"{name} was not put back")

    def test_the_import_is_a_named_list_and_not_a_star(self):
        """A star import binds whatever happens to be there. A name the core
        stops exporting has to fail at import, naming itself, before anything
        is drawn.

        Read off the syntax tree rather than the text: the comment above the
        import block quotes the star form in order to say why it is not used,
        and a text search cannot tell the two apart."""
        tree = ast.parse(_source("crow.py"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "crow_core":
                for alias in node.names:
                    self.assertNotEqual(alias.name, "*", "the re-export is a star import")
                    names.append(alias.name)
        self.assertGreater(len(names), 80, "the re-export is incomplete")
        for name in names:
            self.assertTrue(hasattr(crow_core, name), f"{name} is not in the core")


class TheCoreStandsAloneTests(unittest.TestCase):
    def test_the_core_imports_without_the_client(self):
        """The direction of the dependency, checked rather than assumed. A core
        that reaches back into cli/crow.py could not be called by a second
        client at all -- which is the only reason this file exists."""
        code = ("import sys; sys.path.insert(0, %r); import crow_core; "
                "print('crow' in sys.modules)" % str(HERE))
        done = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "False",
                         "the core pulled the client in behind it")

    def test_nothing_in_the_core_writes_to_the_terminal(self):
        """The rule the whole stage is cut along: only blocks with 0 terminal
        lines moved. The two exceptions sit behind install_font(verbose=True),
        which nothing in this repository passes."""
        lines = _source("crow_core.py").splitlines()
        printing = [n for n, line in enumerate(lines, 1)
                    if re.search(r"\bprint\(", line) and not line.lstrip().startswith("#")]
        self.assertEqual(len(printing), 2,
                         f"unexpected print() in crow_core.py at lines {printing}")
        for n in printing:
            self.assertIn("verbose", "\n".join(lines[max(0, n - 3):n]),
                          f"the print at line {n} is not behind `verbose`")

    def test_the_module_is_prefixed_so_it_cannot_shadow_the_library(self):
        """`python <abs>\\cli\\crow.py` puts cli/ on sys.path[0]. A file called
        queue.py or json.py in here would shadow the standard library for every
        client that starts from this directory -- and _post_stream imports
        `queue`."""
        self.assertEqual(Path(crow_core.__file__).name, "crow_core.py")
        self.assertEqual(Path(crow_core.__file__).parent.name, "cli")


class _Recorder(crow_core.ReplyEvents):
    """Writes down what it was told, in the order it was told."""

    def __init__(self):
        self.log: list[tuple] = []

    def reply_started(self):
        self.log.append(("started",))

    def answer_started(self):
        self.log.append(("answer",))

    def answer_text(self, piece):
        self.log.append(("text", piece))

    def reply_finished(self):
        self.log.append(("finished",))

    @property
    def names(self) -> list[str]:
        return [entry[0] for entry in self.log]

    @property
    def printed(self) -> str:
        return "".join(entry[1] for entry in self.log if entry[0] == "text")


class ReplySeamTests(unittest.TestCase):
    """The seam E4 cut, and the only place it is tested.

    `stream_reply` carried thirteen terminal lines -- `out=sys.stdout` and
    `prefix: str = ""` in the signature, eleven statements in the body. They are
    four named events now. cli/test_crow.py cannot reach any of this: it calls
    `crow.stream_reply`, which still takes the two parameters and still prints
    the same characters, so it stays green whether the events fire or not.
    """

    def _run(self, deltas, events=None, breaks=False, **kw):
        """Drive crow_core.stream_reply against a canned stream.

        The transport is swapped by rebinding the module global rather than by
        passing one in: that is the only door there is, and this test is also
        what says so."""
        chunks = [json.dumps({"choices": [{"delta": d}]}) for d in deltas]
        chunks.append(json.dumps({"choices": [], "timings": {"predicted_n": 7}}))
        original = crow_core._post_stream

        def fake(url, body, key, timeout):
            self.sent_body = body
            for chunk in chunks:
                yield chunk
            if breaks:
                raise crow_core.CrowError("the socket went away mid-turn")

        crow_core._post_stream = fake
        try:
            return crow_core.stream_reply(
                crow_core.Conversation("SYS"), base_url="http://x/v1", model="crow",
                api_key="k", temperature=0.0, timeout=1.0, events=events, **kw)
        finally:
            crow_core._post_stream = original

    def test_the_core_takes_no_screen_at_all(self):
        """The negative half of the move. A second door back to a terminal --
        an `out` kept "just in case", a `prefix` left in the signature -- would
        be a second truth about who prints, and the one that goes stale is the
        one no test drives."""
        parameters = inspect.signature(crow_core.stream_reply).parameters
        self.assertNotIn("out", parameters)
        self.assertNotIn("prefix", parameters)
        self.assertIn("events", parameters)
        self.assertIsNone(parameters["events"].default,
                          "a caller that asks for nothing must get silence")

    def test_a_turn_without_events_is_silent_and_complete(self):
        """What a probe, a batch run or a test wants: the text, no screen."""
        text, reasoning, timings = self._run([{"reasoning_content": "thinking"},
                                              {"content": "ANSWER"}])
        self.assertEqual(text, "ANSWER")
        self.assertEqual(reasoning, "thinking")
        self.assertEqual(timings["_reasoning_chars"], len("thinking"))

    def test_the_four_events_fire_where_the_eleven_lines_stood(self):
        """Order is the contract: the renderer and the bird are built before
        the first byte, the switch to writing comes on the first CONTENT delta,
        and the close comes last."""
        events = _Recorder()
        self._run([{"reasoning_content": "hmm"}, {"content": "A"}, {"content": "B"}],
                  events=events)
        self.assertEqual(events.names,
                         ["started", "answer", "text", "text", "finished"])
        self.assertEqual(events.printed, "AB")

    def test_reasoning_never_reaches_the_sink(self):
        """It is 60-90 % of every answer this model gives. Reported as text it
        would bury the code -- which is what the terminal line it replaced was
        careful not to do."""
        events = _Recorder()
        self._run([{"reasoning_content": "SECRET THOUGHTS"}, {"content": "ANSWER"}],
                  events=events)
        self.assertEqual(events.printed, "ANSWER")
        self.assertNotIn("SECRET", str(events.log))

    def test_the_answer_starts_once_however_many_deltas_arrive(self):
        """The old line sat behind `if first_content_at is None`. Firing per
        delta would stop the bird and write the prompt on every chunk."""
        events = _Recorder()
        self._run([{"content": "A"}, {"content": "B"}, {"content": "C"}], events=events)
        self.assertEqual(events.names.count("answer"), 1)

    def test_a_turn_that_only_calls_tools_never_starts_an_answer(self):
        """No content delta, so the bird keeps flapping and no prompt is
        written -- and the turn still has to close."""
        events = _Recorder()
        _, _, timings = self._run(
            [{"tool_calls": [{"index": 0, "id": "c1",
                              "function": {"name": "list_dir", "arguments": "{}"}}]}],
            events=events)
        self.assertEqual(events.names, ["started", "finished"])
        self.assertEqual(timings["_tool_calls"][0]["name"], "list_dir")

    def test_a_broken_stream_still_finishes_the_reply(self):
        """THE HALF STATE THIS STAGE EXISTS TO PREVENT. `reply_finished` runs
        from the core's `finally`, so a turn that dies mid-stream still closes
        the renderer and stops the bird. Wire the event anywhere else and the
        spill file stays open, the code block stays half drawn, and the bird
        goes on flapping over the traceback."""
        events = _Recorder()
        with self.assertRaises(crow_core.CrowError):
            self._run([{"content": "half an ans"}], events=events, breaks=True)
        self.assertEqual(events.names[0], "started")
        self.assertEqual(events.names[-1], "finished")

    def test_the_default_events_object_swallows_everything(self):
        """`ReplyEvents` itself is the silent one. If a method here started
        printing, every caller that asked for silence would get a screen."""
        events = crow_core.ReplyEvents()
        self.assertIsNone(events.reply_started())
        self.assertIsNone(events.answer_started())
        self.assertIsNone(events.answer_text("x"))
        self.assertIsNone(events.reply_finished())


class TerminalSinkTests(unittest.TestCase):
    """The other side of the seam: cli/crow.py's `TerminalEvents`.

    These are the eleven statements, still in the client, still doing what they
    did. The counter-probe for this stage compares the two paths' output as
    files; what it cannot state is WHICH lines have to be there, so that is
    here."""

    def test_the_terminal_sink_is_the_cores_seam(self):
        self.assertTrue(issubclass(crow.TerminalEvents, crow_core.ReplyEvents))

    def test_the_terminal_sink_still_takes_out_and_prefix(self):
        """The two signature lines of the thirteen. `repl()` passes `prefix`
        and cli/test_crow.py passes `out`; a seam that renamed its caller's
        parameters would be a rewrite wearing a move's clothes.

        THE ADDRESS CHANGED ON 2026-08-13, THE PROMISE DID NOT. Until then this
        read the signature of a `crow.stream_reply` wrapper whose whole body was
        to build a TerminalEvents. `tools/check_shared_core.py` counted that
        wrapper as a second definition of a core name, so it was dissolved and
        the three terminal parameters moved to the callers. They are still named
        `out` and `prefix`, still with the same default -- one constructor along.
        """
        parameters = inspect.signature(crow.TerminalEvents).parameters
        self.assertIn("out", parameters)
        self.assertIn("prefix", parameters)
        self.assertEqual(parameters["prefix"].default, "")

    def test_the_prefix_is_written_once_and_before_the_answer(self):
        sink = io.StringIO()
        events = crow.TerminalEvents(out=sink, prefix="crow> ")
        events.reply_started()
        events.answer_started()
        events.answer_text("hello\n")
        events.reply_finished()
        self.assertEqual(sink.getvalue().count("crow> "), 1)
        self.assertTrue(sink.getvalue().startswith("crow> "))

    def test_an_empty_prefix_writes_nothing(self):
        """`if prefix:` was one of the eleven lines, and it is the reason a
        turn without a prompt does not start with a blank write."""
        sink = io.StringIO()
        events = crow.TerminalEvents(out=sink, prefix="")
        events.reply_started()
        events.answer_started()
        events.reply_finished()
        self.assertEqual(sink.getvalue(), "")

    def test_finishing_closes_the_renderer_and_stops_the_bird(self):
        """The pair that has to stay one piece. An unterminated fence is left
        half drawn if `close()` is missed, and the bird's thread is left running
        if `stop()` is."""
        sink = io.StringIO()
        events = crow.TerminalEvents(out=sink, prefix="")
        events.reply_started()
        events.answer_text("```python\nprint(1)\n")
        self.assertTrue(events._renderer.in_code, "the fence never opened")
        events.reply_finished()
        self.assertFalse(events._renderer.in_code, "the block was left half drawn")
        self.assertTrue(events._raven._stop.is_set(), "the bird is still flapping")

    def test_the_client_hands_the_core_a_terminal(self):
        """End to end over the real wrapper: what `crow.stream_reply` prints is
        what `TerminalEvents` was told, and nothing else."""
        chunks = [json.dumps({"choices": [{"delta": {"reasoning_content": "quiet"}}]}),
                  json.dumps({"choices": [{"delta": {"content": "LOUD\n"}}]})]
        original = crow._post_stream
        crow._post_stream = lambda url, body, key, timeout: iter(chunks)
        sink = io.StringIO()
        try:
            text, reasoning, _ = crow.stream_reply(
                crow.Conversation("SYS"), base_url="http://x/v1", model="crow",
                api_key="k", temperature=0.0, timeout=1.0,
                events=crow.TerminalEvents(out=sink, prefix="P>"))
        finally:
            crow._post_stream = original
        self.assertEqual(text, "LOUD\n")
        self.assertEqual(reasoning, "quiet")
        self.assertEqual(sink.getvalue(), "P>LOUD\n")


# THE FIXTURE E10 IS CUT AGAINST, and it is the whole reason the stage exists:
# think, answer, THINK AGAIN, answer again -- inside one turn. `reasoning_content`
# and `content` are two keys of the same delta object and either can follow
# either, so this shape is not exotic, it is what the model does whenever it
# reconsiders mid-answer. Failure point P3 of #90 in five deltas.
RE_ENTRY = [
    {"reasoning_content": "first I "},
    {"reasoning_content": "consider it"},
    {"content": "ANSWER ONE\n"},
    {"reasoning_content": "wait -- I should check"},
    {"content": "ANSWER TWO\n"},
]


class _BlockRecorder(crow_core.ReplyEvents):
    """Writes down the thought blocks AND the answer, in arrival order.

    Both, because the question this stage turns on is not "were there two
    blocks" alone -- it is whether the answer arrived INSIDE one of them. That
    can only be read off a log that carries the two together.

    Separate from `_Recorder` above on purpose: that one deliberately records
    only the four events of E4, which is what lets it assert that reasoning
    never reaches `answer_text`. Teaching it the three new ones would make that
    case pass for a different reason than it was written for.
    """

    def __init__(self):
        self.log: list[tuple] = []

    def reasoning_started(self, index):
        self.log.append(("open", index))

    def reasoning_text(self, piece):
        self.log.append(("thought", piece))

    def reasoning_finished(self):
        self.log.append(("close",))

    def answer_started(self):
        self.log.append(("answer",))

    def answer_text(self, piece):
        self.log.append(("text", piece))

    @property
    def opened(self) -> list[int]:
        return [entry[1] for entry in self.log if entry[0] == "open"]

    @property
    def blocks(self) -> list[str]:
        """The thoughts as the screen would group them: one string per block."""
        out, current = [], None
        for entry in self.log:
            if entry[0] == "open":
                current = []
            elif entry[0] == "thought" and current is not None:
                current.append(entry[1])
            elif entry[0] == "close" and current is not None:
                out.append("".join(current))
                current = None
        return out

    @property
    def answer_inside_a_block(self) -> list[str]:
        """Answer pieces that arrived while a block was OPEN.

        This is P3 made countable. On a surface that folds a block away, every
        string in this list is answer text the reader cannot see -- and an empty
        list is the only acceptable answer.
        """
        out, open_now = [], False
        for entry in self.log:
            if entry[0] == "open":
                open_now = True
            elif entry[0] == "close":
                open_now = False
            elif entry[0] == "text" and open_now:
                out.append(entry[1])
        return out


class _ThinkFirstThenAnswer:
    """The version E10 exists to rule out, built to be driven, not to be kept.

    It hard-codes the order the plan names: a block opens at the first thought
    and closes when the TURN ends. Nothing about it is stupid -- it is what a
    reasonable person writes after watching one stream, because one stream does
    look like "think, then answer". It is red at `RE_ENTRY` and green
    everywhere else, which is exactly why a fixture without re-entry proves
    nothing.

    Same surface as `crow_core.ReasoningBlocks`, so `stream_reply` can be driven
    against it by rebinding the module global -- the counter-probe then runs
    through the REAL loop instead of a copy of it.
    """

    def __init__(self, events=None):
        self._events = events or crow_core.ReplyEvents()
        self.blocks: list[str] = []
        self.open = False
        self.reasoning_chars = 0
        self.content_chars = 0
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.blocks) + "".join(self._parts)

    def reasoning_delta(self, piece):
        if not piece:
            return
        if not self.open:
            self.open = True
            self._events.reasoning_started(1)
        self._parts.append(piece)
        self.reasoning_chars += len(piece)
        self._events.reasoning_text(piece)

    def content_delta(self, piece):
        # THE ONE MISSING LINE. `self.finish()` belongs here; without it the
        # block that opened at the first thought is still open when the answer
        # arrives, and everything after it is inside the block.
        if not piece:
            return
        self.content_chars += len(piece)

    def finish(self):
        if not self.open:
            return
        self.blocks.append("".join(self._parts))
        self._parts = []
        self.open = False
        self._events.reasoning_finished()


class ReasoningBlockTests(unittest.TestCase):
    """E10's state machine: where a thought begins, ends, and BEGINS AGAIN.

    It sits in the core rather than in either surface because both need it --
    the terminal shows and hides a block, the window folds and unfolds one --
    and a second copy of this decision is the second truth the whole split
    exists to prevent.
    """

    def _run(self, deltas, events=None, automaton=None, **kw):
        """Drive the REAL `crow_core.stream_reply` against a canned stream.

        `automaton` swaps the state machine the same way `_post_stream` is
        swapped: by rebinding the module global, which is the only door there
        is. That is what lets the counter-probe below run a hard-coded
        implementation through this loop rather than beside it.
        """
        chunks = [json.dumps({"choices": [{"delta": d}]}) for d in deltas]
        chunks.append(json.dumps({"choices": [], "timings": {"predicted_n": 7}}))
        original_post = crow_core._post_stream
        original_blocks = crow_core.ReasoningBlocks

        def fake(url, body, key, timeout):
            return iter(chunks)

        crow_core._post_stream = fake
        if automaton is not None:
            crow_core.ReasoningBlocks = automaton
        try:
            return crow_core.stream_reply(
                crow_core.Conversation("SYS"), base_url="http://x/v1", model="crow",
                api_key="k", temperature=0.0, timeout=1.0, events=events, **kw)
        finally:
            crow_core._post_stream = original_post
            crow_core.ReasoningBlocks = original_blocks

    def test_a_re_entering_stream_is_two_blocks_and_not_one(self):
        """THE RED CASE OF THIS STAGE. Two thoughts with an answer between them
        are two blocks; anything that reports one has folded the answer in."""
        events = _BlockRecorder()
        _, _, timings = self._run(RE_ENTRY, events=events)
        self.assertEqual(events.opened, [1, 2])
        self.assertEqual(timings["_reasoning_blocks"], 2)

    def test_the_answer_between_two_thoughts_is_in_neither(self):
        """P3, stated as the thing that goes wrong: answer text inside a
        collapsed reasoning block, where nobody reads it."""
        events = _BlockRecorder()
        self._run(RE_ENTRY, events=events)
        self.assertEqual(events.answer_inside_a_block, [])
        self.assertEqual(events.blocks,
                         ["first I consider it", "wait -- I should check"])
        for block in events.blocks:
            self.assertNotIn("ANSWER", block)

    def test_a_hard_coded_think_first_then_answer_swallows_the_answer(self):
        """COUNTER-PROBE (a). The same fixture through the same loop, with the
        state machine replaced by one that opens once and closes at the end.

        It must report ONE block and it must have both answers inside it. If
        this case ever goes green while the two above stay green, the fixture
        stopped distinguishing the two implementations and every case here is
        worth nothing.
        """
        events = _BlockRecorder()
        _, _, timings = self._run(RE_ENTRY, events=events,
                                  automaton=_ThinkFirstThenAnswer)
        self.assertEqual(events.opened, [1], "it cannot open a second block")
        self.assertEqual(timings["_reasoning_blocks"], 1)
        self.assertEqual(events.answer_inside_a_block, ["ANSWER ONE\n", "ANSWER TWO\n"],
                         "the whole answer is inside the block a surface folds away")

    def test_the_two_implementations_return_the_same_prefix(self):
        """WHY THE SUITE HAD TO GROW A NEW PREDICATE. The hard-coded version is
        not wrong about anything the OLD tests measured: same text, same
        reasoning, same characters back to the server on the next turn. It is
        wrong about exactly one thing, and nothing before this stage looked at
        it."""
        good_text, good_reasoning, _ = self._run(RE_ENTRY)
        bad_text, bad_reasoning, _ = self._run(RE_ENTRY, automaton=_ThinkFirstThenAnswer)
        self.assertEqual(good_text, bad_text)
        self.assertEqual(good_reasoning, bad_reasoning)

    def test_the_returned_reasoning_is_still_the_whole_stream(self):
        """The blocks are a view for a reader; the string is the PREFIX. Joined
        with anything, or reordered, the next turn's cache would miss -- 242.3 s
        of prefill against 1.6 s, measured 2026-08-08."""
        _, reasoning, _ = self._run(RE_ENTRY)
        self.assertEqual(reasoning, "first I consider itwait -- I should check")

    def test_a_stream_without_reasoning_opens_no_block(self):
        """COUNTER-PROBE (b), core half: no thoughts, no empty block. A state
        machine that opens one per turn would have every surface drawing an
        empty container around nothing."""
        events = _BlockRecorder()
        _, reasoning, timings = self._run([{"content": "PLAIN\n"}], events=events)
        self.assertEqual(events.opened, [])
        self.assertEqual(events.blocks, [])
        self.assertEqual(reasoning, "")
        self.assertNotIn("_reasoning_blocks", timings)

    def test_a_turn_that_only_thinks_still_closes_its_block(self):
        """The tool-call turn: it thinks, it asks for a tool, no content ever
        arrives. The block is closed from the core's `finally`, or a surface is
        left holding one open for the rest of the session."""
        events = _BlockRecorder()
        _, _, timings = self._run(
            [{"reasoning_content": "I need the file"},
             {"tool_calls": [{"index": 0, "id": "c1",
                              "function": {"name": "list_dir", "arguments": "{}"}}]}],
            events=events)
        self.assertEqual(events.opened, [1])
        self.assertEqual(events.blocks, ["I need the file"])
        self.assertEqual(timings["_reasoning_blocks"], 1)

    def test_a_block_closes_before_the_answer_is_announced(self):
        """ORDER, not just membership. `answer_started` is where the terminal
        stops the bird and writes its prompt; fired before the block closes, the
        prompt lands inside the block and the window folds the first line of the
        answer away with the thought."""
        events = _BlockRecorder()
        self._run(RE_ENTRY, events=events)
        names = [entry[0] for entry in events.log]
        self.assertLess(names.index("close"), names.index("answer"))

    def test_the_share_is_counted_by_the_machine_that_shows_it(self):
        """THE SECOND HALF-STATE THE PLAN NAMES: the flag shows reasoning while
        `thinking NN%` goes on counting its own way, and the two say different
        things about the same turn. Both numbers come off the same object."""
        _, _, timings = self._run(RE_ENTRY)
        shown = sum(len(d["reasoning_content"]) for d in RE_ENTRY if "reasoning_content" in d)
        answered = sum(len(d["content"]) for d in RE_ENTRY if "content" in d)
        self.assertEqual(timings["_reasoning_chars"], shown)
        self.assertEqual(timings["_content_chars"], answered)

    def test_the_default_events_object_swallows_the_three_new_ones_too(self):
        """`ReplyEvents` is the silent one, all seven of it. A method here that
        started printing would give a screen to every caller that asked for
        none -- a probe, a batch run, the GUI's own tests."""
        events = crow_core.ReplyEvents()
        self.assertIsNone(events.reasoning_started(1))
        self.assertIsNone(events.reasoning_text("x"))
        self.assertIsNone(events.reasoning_finished())

    def test_the_machine_can_be_driven_without_a_stream_at_all(self):
        """It is a state machine, not a stream reader -- which is what lets the
        window feed it from a queue on the Tk thread instead of from a socket."""
        blocks = crow_core.ReasoningBlocks()
        blocks.reasoning_delta("a")
        blocks.content_delta("X")
        blocks.reasoning_delta("b")
        blocks.finish()
        self.assertEqual(blocks.blocks, ["a", "b"])
        self.assertEqual(blocks.text, "ab")
        self.assertEqual((blocks.reasoning_chars, blocks.content_chars), (2, 1))

    def test_closing_twice_closes_once(self):
        """`finish()` runs from a `finally` and may already have run. A second
        close that appended a second, empty block would put one on every
        surface at the end of every turn that answered anything."""
        blocks = crow_core.ReasoningBlocks()
        blocks.reasoning_delta("a")
        blocks.finish()
        blocks.finish()
        self.assertEqual(blocks.blocks, ["a"])

    def test_an_empty_delta_neither_opens_nor_counts(self):
        """Servers send `{"content": ""}` as a keepalive. Counted, it would open
        a block on nothing and add a zero to the share."""
        events = _BlockRecorder()
        blocks = crow_core.ReasoningBlocks(events)
        blocks.reasoning_delta("")
        blocks.content_delta("")
        blocks.finish()
        self.assertEqual(events.log, [])
        self.assertEqual(blocks.blocks, [])


class _TurnRecorder(crow_core.TurnEvents):
    """Writes down what `run_turn` was told, in the order it was told.

    The reply events stay None, so the stream inside each round is silent: what
    is under test here is the LOOP, not the twelve lines the CLI draws from it.
    """

    def __init__(self):
        self.log: list[tuple] = []

    def turn_failed(self, message):
        self.log.append(("failed", message))

    def turn_interrupted(self):
        self.log.append(("interrupted",))

    def round_finished(self, timings):
        self.log.append(("round", timings))

    def cache_promise_broken(self):
        self.log.append(("cache_broken",))

    def budget_spent(self, budget):
        self.log.append(("budget", budget))

    def tool_started(self, name, arguments):
        self.log.append(("tool_start", name, arguments))

    def tool_finished(self, name, seconds, repeated):
        self.log.append(("tool_done", name, repeated))

    def tool_failed(self, name, result):
        self.log.append(("tool_failed", name, result))

    def tools_finished(self):
        self.log.append(("tools_done",))

    def tools_reported(self, calls):
        self.log.append(("reported", [c["name"] for c in calls]))

    def rolled_over(self, tokens, path):
        self.log.append(("rolled", tokens, path))

    def rollover_refused(self):
        self.log.append(("refused",))

    @property
    def names(self) -> list[str]:
        return [entry[0] for entry in self.log]


def _call_delta(name: str, arguments: str, index: int = 0, cid: str | None = None) -> dict:
    """One streamed `tool_calls` delta, in the shape the server sends."""
    return {"tool_calls": [{"index": index, "id": cid or f"c{index}",
                            "function": {"name": name, "arguments": arguments}}]}


def _dangling(messages: list[dict]) -> list[str]:
    """Tool call ids with no `tool` message behind them.

    THIS IS WHAT A BROKEN PREFIX LOOKS LIKE FROM OUTSIDE. An assistant turn that
    announces a call the conversation never answers is rejected by the template
    that renders it, and it is rejected on every LATER turn of the session --
    not on the one that appended it. A fake endpoint cannot refuse it for us, so
    the check is made on the message list itself, which is the thing the server
    would have been sent.
    """
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    return [c["id"] for m in messages for c in (m.get("tool_calls") or [])
            if c["id"] not in answered]


class TurnLoopCase(unittest.TestCase):
    """A scripted endpoint plus the tool state E1 pins, for driving `run_turn`.

    THE TRANSPORT IS THE ONLY DOOR, same as in `ReplySeamTests`: the rounds are
    fed in as `_post_stream` payloads, so the real `stream_reply` runs and the
    real tool layer runs behind it. A double for `stream_reply` itself would
    make every one of these cases a test of the double.

    `_READ` and `_SEEN` are emptied IN PLACE and put back, for the reason
    cli/test_crow.py's `ToolLayerCase` writes out at length: they are one object
    shared by both modules, and rebinding either name would leave the tools
    consulting the original.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crow-turn-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sessions = os.path.join(self.dir, "session")
        self.work = os.path.join(self.dir, "work")
        os.makedirs(self.work)

        self._read_before = set(crow_core._READ)
        self._seen_before = dict(crow_core._SEEN)
        self._session_dir_before = crow_core.SESSION_DIR
        self._post_stream_before = crow_core._post_stream
        self.addCleanup(self._restore)
        crow_core._READ.clear()
        crow_core._SEEN.clear()
        crow_core.SESSION_DIR = self.sessions
        crow_core.INTERRUPT.clear()

        # Every round the endpoint is scripted to answer with, and every body it
        # was asked with. The bodies are the prefix as the server saw it.
        self.script: list[tuple] = []
        self.bodies: list[dict] = []
        self.events = _TurnRecorder()
        crow_core._post_stream = self._serve

    def _restore(self):
        crow_core._post_stream = self._post_stream_before
        crow_core.SESSION_DIR = self._session_dir_before
        crow_core._READ.clear()
        crow_core._READ.update(self._read_before)
        crow_core._SEEN.clear()
        crow_core._SEEN.update(self._seen_before)
        crow_core.INTERRUPT.clear()

    def _serve(self, url, body, api_key, timeout):
        self.bodies.append(json.loads(json.dumps(body)))
        if not self.script:
            raise AssertionError(
                f"the loop asked for round {len(self.bodies)}; only "
                f"{len(self.bodies) - 1} were scripted")
        deltas, timings, raises = self.script.pop(0)
        for delta in deltas:
            yield json.dumps({"choices": [{"delta": delta}]})
        yield json.dumps({"choices": [], "timings": timings})
        if raises is not None:
            raise raises

    def serve(self, deltas, timings=None, raises=None):
        """Add one round to the script. Returns self, so calls chain."""
        self.script.append((deltas, timings or {"predicted_n": 1}, raises))
        return self

    def conversation(self, question: str = "what is here?") -> crow_core.Conversation:
        talk = crow_core.Conversation("SYS")
        talk.append("user", question)
        return talk

    def turn(self, talk, **kw):
        kw.setdefault("base_url", "http://x/v1")
        kw.setdefault("model", "crow")
        kw.setdefault("api_key", "k")
        # Spelled out because `run_turn` refuses to carry a default for them:
        # the operating point wants each sampling value written once, and that
        # once is `stream_reply`.
        kw.setdefault("temperature", 0.0)
        kw.setdefault("top_p", 1.0)
        kw.setdefault("min_p", 0.0)
        kw.setdefault("timeout", 1.0)
        kw.setdefault("events", self.events)
        return crow_core.run_turn(talk, **kw)

    def assertPrefixIsWhole(self, talk, why=""):
        self.assertEqual(_dangling(talk.payload()), [],
                         why or "the prefix announces calls nothing answers")


class UnanswerableCallsTests(TurnLoopCase):
    """Rule (a): calls that will never run are not appended.

    THE ONE WHOSE ABSENCE IS INVISIBLE INSIDE THE TURN THAT BREAKS IT. Remove
    the rule and this turn still produces its text, still runs no tool, still
    prints the same thing. What changes is the message list it leaves behind,
    and that is only paid for the NEXT time anything is sent -- which is why
    every case here checks after the turn and not in it. The comment the rule
    carries says the first half of it was once forgotten and a probe found it.
    """

    def _budget_is_spent_on_the_first_round(self, talk):
        self.serve([{"content": "let me look"},
                    _call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "I got nowhere"}])
        return self.turn(talk, max_tool_rounds=0)

    def test_the_text_of_the_refused_round_is_kept(self):
        talk = self.conversation()
        self._budget_is_spent_on_the_first_round(talk)
        said = [m["content"] for m in talk.payload() if m["role"] == "assistant"]
        self.assertIn("let me look", said)

    def test_the_calls_of_the_refused_round_are_not(self):
        talk = self.conversation()
        self._budget_is_spent_on_the_first_round(talk)
        self.assertEqual([m for m in talk.payload() if m.get("tool_calls")], [])

    def test_the_prefix_is_whole_after_the_turn(self):
        talk = self.conversation()
        self._budget_is_spent_on_the_first_round(talk)
        self.assertPrefixIsWhole(talk)

    def test_a_later_turn_against_that_prefix_still_goes_through(self):
        """THE CASE THE RULE EXISTS FOR, and it has to be a second turn.

        The turn that drops the calls is unharmed by keeping them. The one after
        it sends the whole conversation again, and a dangling `tool_calls` in
        there is what the server refuses -- for every turn of the session, not
        just one."""
        talk = self.conversation()
        self._budget_is_spent_on_the_first_round(talk)
        self.serve([{"content": "second answer"}])
        talk.append("user", "and now?")
        later = self.turn(talk, max_tool_rounds=4)
        self.assertFalse(later.stopped)
        self.assertEqual(_dangling(self.bodies[-1]["messages"]), [],
                         "the second turn sent a prefix the server would refuse")

    def test_the_forced_round_drops_its_calls_too(self):
        """The second half of the rule. The forced round is answered with tools
        still declared, so it may ask for one -- and nothing will run it."""
        talk = self.conversation()
        self.serve([{"content": "looking"},
                    _call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "one more try"},
                    _call_delta("read_file", json.dumps({"path": "x"}), cid="c9")])
        self.turn(talk, max_tool_rounds=0)
        self.assertPrefixIsWhole(talk)
        self.assertEqual([m for m in talk.payload() if m.get("tool_calls")], [])

    def test_a_round_inside_the_budget_keeps_its_calls_and_answers_them(self):
        """The positive half, without which the rule could be "drop them all".
        A loop that never appended a call would never run a tool either."""
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "empty"}])
        self.turn(talk, max_tool_rounds=4)
        with_calls = [m for m in talk.payload() if m.get("tool_calls")]
        self.assertEqual(len(with_calls), 1)
        self.assertPrefixIsWhole(talk)
        self.assertIn("tool", [m["role"] for m in talk.payload()])


class SpentBudgetTests(TurnLoopCase):
    """Rule (b): the budget buys tool rounds, not the turn.

    Until 2026-08-10 the spent budget was a bare `break`, and a turn that ran
    out ended on a bracket: driven live with --max-tool-rounds 0 the model
    produced 102 tokens, `thinking 100%`, and the user was shown nothing at all.
    """

    def test_a_spent_budget_buys_one_more_round(self):
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "here is where I got to"}])
        result = self.turn(talk, max_tool_rounds=0)
        self.assertEqual(result.cost.rounds, 2)
        self.assertEqual(len(self.bodies), 2)

    def test_the_extra_round_is_told_what_it_is_for(self):
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "here is where I got to"}])
        self.turn(talk, max_tool_rounds=0)
        asked = [m["content"] for m in talk.payload() if m["role"] == "user"]
        self.assertIn(crow_core.BUDGET_SPENT, asked)

    def test_the_user_ends_the_turn_with_words_and_not_a_bracket(self):
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "here is where I got to"}])
        self.turn(talk, max_tool_rounds=0)
        said = [m["content"] for m in talk.payload() if m["role"] == "assistant"]
        self.assertEqual(said[-1], "here is where I got to")

    def test_the_refused_round_runs_nothing(self):
        """It is a REFUSAL, not a last chance. A tool that ran here would be a
        round more than the budget allows, bought silently."""
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "nothing ran"}])
        self.turn(talk, max_tool_rounds=0)
        self.assertNotIn("tool_start", self.events.names)
        self.assertEqual(len(crow_core._SEEN), 0)

    def test_the_event_names_the_budget_that_was_spent(self):
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "done"}])
        self.turn(talk, max_tool_rounds=0)
        self.assertIn(("budget", 0), self.events.log)

    def test_a_turn_that_stays_inside_the_budget_never_says_it(self):
        talk = self.conversation()
        self.serve([{"content": "no tools needed"}])
        self.turn(talk, max_tool_rounds=4)
        self.assertNotIn("budget", self.events.names)


class MidTurnRolloverTests(TurnLoopCase):
    """Rules (c) and (d): the window is checked at the end of every round, and
    a second rollover inside one turn stops the turn.

    One tool round has been measured adding 5,253 tokens, and up to
    MAX_TOOL_ROUNDS of them run without the user typing anything -- so a turn
    that starts under the threshold can still walk into the server's wall
    inside itself, and the wall costs the whole turn.
    """

    def _fills_the_window_after_one_tool_round(self):
        # prompt_n + predicted_n is the fallback path of next_context_tokens,
        # which is enough to cross a 100-token window at 0.9.
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))],
                   {"prompt_n": 95, "predicted_n": 0})

    def test_the_window_is_checked_at_the_end_of_a_round(self):
        talk = self.conversation()
        self._fills_the_window_after_one_tool_round()
        self.serve([{"content": "carrying on"}], {"prompt_n": 1, "predicted_n": 0})
        result = self.turn(talk, n_ctx=100, rollover_at=0.9, carry="the question")
        self.assertIn("rolled", self.events.names)
        self.assertTrue(result.rolled)
        self.assertEqual([e for e in self.events.log if e[0] == "rolled"][0][1], 95)

    def test_the_archive_holds_a_whole_conversation_and_not_half_of_one(self):
        """The reason the check sits at the END of a round. Archived in the
        middle, the last assistant turn would announce calls that the archive
        does not answer -- and `crow --resume` would restore that."""
        talk = self.conversation()
        self._fills_the_window_after_one_tool_round()
        self.serve([{"content": "carrying on"}], {"prompt_n": 1, "predicted_n": 0})
        self.turn(talk, n_ctx=100, rollover_at=0.9, carry="the question")
        archives = sorted(f for f in os.listdir(self.sessions) if f.endswith(".json"))
        self.assertEqual(len(archives), 1, archives)
        with io.open(os.path.join(self.sessions, archives[0]), encoding="utf-8") as fh:
            saved = json.load(fh)["messages"]
        self.assertIn("tool", [m["role"] for m in saved])
        self.assertEqual(_dangling(saved), [])

    def test_the_fresh_conversation_carries_the_question(self):
        """Without `carry` the rollover archives the question along with
        everything else and leaves the model answering a note about a file."""
        talk = self.conversation()
        self._fills_the_window_after_one_tool_round()
        self.serve([{"content": "carrying on"}], {"prompt_n": 1, "predicted_n": 0})
        self.turn(talk, n_ctx=100, rollover_at=0.9, carry="the question")
        opening = [m for m in talk.payload() if m["role"] == "user"][0]["content"]
        self.assertIn("the question", opening)
        self.assertIn("archived", opening)

    def test_the_count_is_zeroed_and_the_turn_goes_on(self):
        talk = self.conversation()
        self._fills_the_window_after_one_tool_round()
        self.serve([{"content": "carrying on"}], {"prompt_n": 1, "predicted_n": 0})
        result = self.turn(talk, n_ctx=100, rollover_at=0.9, carry="the question")
        self.assertFalse(result.stopped)
        self.assertEqual(result.context_tokens, 1)
        self.assertEqual(len(self.bodies), 2, "the turn stopped instead of carrying on")

    def test_a_window_that_is_not_full_is_not_rolled(self):
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))],
                   {"prompt_n": 10, "predicted_n": 0})
        self.serve([{"content": "done"}], {"prompt_n": 1, "predicted_n": 0})
        result = self.turn(talk, n_ctx=100, rollover_at=0.9)
        self.assertNotIn("rolled", self.events.names)
        self.assertFalse(result.rolled)
        self.assertFalse(os.path.isdir(self.sessions))

    def test_a_second_rollover_inside_one_turn_stops_the_turn(self):
        """Twice in one turn means the question itself does not fit. Rolling
        again would archive the note and ask the same thing again, forever."""
        talk = self.conversation()
        self._fills_the_window_after_one_tool_round()
        result = self.turn(talk, n_ctx=100, rollover_at=0.9, rolled=True,
                           carry="the question")
        self.assertIn("refused", self.events.names)
        self.assertNotIn("rolled", self.events.names)
        self.assertTrue(result.stopped)

    def test_the_refused_second_rollover_archives_nothing(self):
        talk = self.conversation()
        self._fills_the_window_after_one_tool_round()
        self.turn(talk, n_ctx=100, rollover_at=0.9, rolled=True, carry="the question")
        self.assertFalse(os.path.isdir(self.sessions))

    def test_the_refused_second_rollover_asks_for_no_further_round(self):
        talk = self.conversation()
        self._fills_the_window_after_one_tool_round()
        self.turn(talk, n_ctx=100, rollover_at=0.9, rolled=True, carry="the question")
        self.assertEqual(len(self.bodies), 1, "it asked the same question again")


class ReportedNotRunTests(TurnLoopCase):
    """The operating mode this stage added, and the gate for the CLI change.

    A client that sends `tools` gets tool calls back whether it can run them or
    not, and it MUST send them: this model's template keeps a previous turn's
    thoughts only while the array is non-empty -- measured 2026-08-08 over the
    server's own /apply-template, 132 characters against 132 without tools. So
    the rule about what happens to the prefix then is shared behaviour, and it
    lives with the loop rather than in whichever surface met it first.
    """

    def _reports_one_call(self, talk, **kw):
        self.serve([{"content": "I would look at that"},
                    _call_delta("list_dir", json.dumps({"path": self.work}))])
        return self.turn(talk, execute_tools=False, **kw)

    def test_the_calls_come_back_to_the_caller(self):
        talk = self.conversation()
        result = self._reports_one_call(talk)
        self.assertEqual([c["name"] for c in result.reported], ["list_dir"])
        self.assertIn(("reported", ["list_dir"]), self.events.log)

    def test_nothing_is_executed(self):
        talk = self.conversation()
        self._reports_one_call(talk)
        self.assertNotIn("tool_start", self.events.names)
        self.assertEqual(len(crow_core._SEEN), 0, "a tool ran and was cached")
        self.assertNotIn("tool", [m["role"] for m in talk.payload()])

    def test_the_appended_assistant_turn_is_free_of_tool_calls(self):
        """The half of the gate that is checked IN the turn."""
        talk = self.conversation()
        self._reports_one_call(talk)
        self.assertEqual([m for m in talk.payload() if m.get("tool_calls")], [])
        said = [m["content"] for m in talk.payload() if m["role"] == "assistant"]
        self.assertEqual(said, ["I would look at that"])

    def test_a_later_turn_against_the_same_prefix_goes_through(self):
        """The half that is checked AFTER it, and the one that decides whether
        this mode is usable at all: a surface that reports its calls has to be
        able to keep talking."""
        talk = self.conversation()
        self._reports_one_call(talk)
        self.assertPrefixIsWhole(talk)
        talk.append("user", "never mind, just answer")
        self.serve([{"content": "then here is the answer"}])
        later = self.turn(talk, execute_tools=False)
        self.assertFalse(later.stopped)
        self.assertEqual(later.cost.rounds, 1)
        self.assertEqual(_dangling(self.bodies[-1]["messages"]), [])

    def test_the_request_still_declares_the_tools(self):
        """The reason the mode is not `--no-tools`. Empty the array and the
        template drops the replayed reasoning, and the prefix diverges where
        the thoughts began."""
        talk = self.conversation()
        self._reports_one_call(talk)
        self.assertEqual(self.bodies[-1]["tools"], crow_core.TOOLS)

    def test_the_replayed_reasoning_survives_into_the_next_turn(self):
        """What the declarations are being kept FOR, end to end: the thoughts
        of the reported turn are still in the body of the one after it."""
        talk = self.conversation()
        self.serve([{"reasoning_content": "a thought worth keeping"},
                    {"content": "I would look at that"},
                    _call_delta("list_dir", json.dumps({"path": self.work}))])
        self.turn(talk, execute_tools=False)
        self.serve([{"content": "second"}])
        talk.append("user", "go on")
        self.turn(talk, execute_tools=False)
        replayed = [m.get("reasoning_content") for m in self.bodies[-1]["messages"]]
        self.assertIn("a thought worth keeping", replayed)

    def test_the_turn_is_one_round_however_many_calls_come_back(self):
        """There is no tool result to feed back, so a second round would ask
        the same question against the same prefix and get the same answer."""
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}), 0, "a"),
                    _call_delta("find_files", json.dumps({"pattern": "*"}), 1, "b")])
        result = self.turn(talk, execute_tools=False, max_tool_rounds=8)
        self.assertEqual(len(self.bodies), 1)
        self.assertEqual(result.cost.rounds, 1)
        self.assertEqual([c["name"] for c in result.reported], ["list_dir", "find_files"])

    def test_a_turn_with_no_calls_reports_nothing_and_still_answers(self):
        talk = self.conversation()
        self.serve([{"content": "no tools needed"}])
        result = self.turn(talk, execute_tools=False)
        self.assertEqual(result.reported, [])
        self.assertNotIn("reported", self.events.names)
        self.assertFalse(result.stopped)

    def test_the_default_mode_still_runs_them(self):
        """The negative half. Without it "reported, not run" could be the only
        behaviour there is and every case above would still pass."""
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))])
        self.serve([{"content": "empty in there"}])
        result = self.turn(talk, max_tool_rounds=4)
        self.assertEqual(result.reported, [])
        self.assertIn("tool_start", self.events.names)
        self.assertIn("tool", [m["role"] for m in talk.payload()])


class ReleaseLevelTests(TurnLoopCase):
    """#88's three levels, and the four cases its "Done when" asks for.

    THE LEVELS ARE NOT THE HARD PART -- the refusal is. A declined call has to
    come back as a tool RESULT, because an assistant turn whose `tool_calls`
    have no `tool` message behind it is a broken prefix for every later turn of
    the session. That is the same invariant `run_turn` already keeps for a spent
    budget and for reported-not-run, and #88 adds a fourth trigger to it rather
    than a fourth implementation.
    """

    def _asks_for(self, tool, arguments, mode, answer="no"):
        """One round that calls `tool`, run at `mode` with a scripted answer."""
        self.asked = []

        def approve(name, args):
            self.asked.append((name, args))
            return answer

        self.serve([{"content": "on it"}, _call_delta(tool, arguments)])
        self.serve([{"content": "done"}])
        talk = self.conversation()
        return talk, self.turn(talk, mode=mode, approve=approve)

    # -- the table itself ---------------------------------------------------

    def test_reading_never_asks_at_any_level(self):
        """"A level that asks before list_dir is a level nobody keeps switched
        on, and a protection everyone turns off protects nothing.\""""
        for mode in crow_core.MODES:
            for tool in ("read_file", "list_dir", "find_files", "search_text"):
                self.assertFalse(crow_core.needs_approval(tool, mode),
                                 f"{tool} asks at {mode}")

    def test_the_table_matches_the_ticket(self):
        asks = {m: sorted(t for t in crow_core.TOOL_IMPL
                          if crow_core.needs_approval(t, m))
                for m in crow_core.MODES}
        self.assertEqual(asks["manual"], ["edit_file", "run_command", "write_file"])
        self.assertEqual(asks["allowedit"], ["run_command"])
        self.assertEqual(asks["auto"], [])

    def test_an_unknown_tool_is_treated_as_the_strictest_class(self):
        """A tool nobody has classified is one nobody has thought about, and
        guessing "safe" for it is the one guess with a cost."""
        self.assertTrue(crow_core.needs_approval("some_new_tool", "manual"))
        self.assertTrue(crow_core.needs_approval("some_new_tool", "allowedit"))

    # -- the two cases that must be refused ---------------------------------

    def test_a_write_under_manual_is_refused(self):
        target = os.path.join(self.work, "no.txt")
        talk, _ = self._asks_for("write_file",
                                 json.dumps({"path": target, "content": "x"}),
                                 "manual")
        self.assertEqual([n for n, _ in self.asked], ["write_file"])
        self.assertFalse(os.path.exists(target), "the file was written anyway")

    def test_a_command_under_allowedit_is_refused_while_a_write_runs(self):
        """The level's whole shape in one case: allowedit releases the disk and
        holds the shell."""
        target = os.path.join(self.work, "yes.txt")
        self.serve([{"content": "writing"},
                    _call_delta("write_file",
                                json.dumps({"path": target, "content": "hello"}))])
        self.serve([{"content": "now the shell"},
                    _call_delta("run_command", json.dumps({"command": "echo hi"}))])
        self.serve([{"content": "done"}])
        asked = []
        talk = self.conversation()
        self.turn(talk, mode="allowedit",
                  approve=lambda n, a: asked.append(n) or "no")
        self.assertEqual(asked, ["run_command"], "the wrong call was put to the user")
        self.assertTrue(os.path.exists(target), "allowedit did not release the write")

    # -- the refusal is a result, not an abort ------------------------------

    def test_a_declined_call_comes_back_as_a_tool_result(self):
        talk, _ = self._asks_for("run_command", json.dumps({"command": "rm -rf /"}),
                                 "manual")
        tools = [m for m in talk.payload() if m["role"] == "tool"]
        self.assertEqual([m["content"] for m in tools], [crow_core.DECLINED])

    def test_the_turn_continues_after_a_refusal(self):
        """Not "the turn ends": the model gets the refusal and answers around
        it. A refusal that ended the turn would cost the session, not the call."""
        talk, _ = self._asks_for("run_command", json.dumps({"command": "ls"}),
                                 "manual")
        said = [m["content"] for m in talk.payload() if m["role"] == "assistant"]
        self.assertEqual(said, ["on it", "done"])

    def test_the_prefix_survives_a_refusal(self):
        """#88's own test for this: a SECOND turn after the refusal. Every
        assistant turn carrying tool_calls has a tool message behind it, or the
        server re-reads the whole conversation from the divergence on."""
        talk, _ = self._asks_for("write_file",
                                 json.dumps({"path": os.path.join(self.work, "a"),
                                             "content": "x"}), "manual")
        self.serve([{"content": "second turn"}])
        talk.append("user", "and now?")
        self.turn(talk, mode="manual", approve=lambda n, a: "no")
        for message in talk.payload():
            if message.get("tool_calls"):
                behind = [m for m in talk.payload()
                          if m["role"] == "tool"
                          and m.get("tool_call_id") in
                          [c["id"] for c in message["tool_calls"]]]
                self.assertTrue(behind, "an assistant turn has calls with no result")

    def test_no_approver_declines_rather_than_runs(self):
        """A half-wired surface refuses. The other half of that choice -- run it
        because nobody could be asked -- is the failure this ticket exists for."""
        target = os.path.join(self.work, "nobody.txt")
        self.serve([{"content": "x"},
                    _call_delta("write_file", json.dumps({"path": target,
                                                          "content": "x"}))])
        self.serve([{"content": "done"}])
        talk = self.conversation()
        self.turn(talk, mode="manual", approve=None)
        self.assertFalse(os.path.exists(target))

    def test_auto_runs_without_an_approver(self):
        """The negative half of the case above: at `auto` nothing is held back,
        so a missing approver changes nothing. Without this, "declined" could be
        the only behaviour there is and every case above would still pass."""
        target = os.path.join(self.work, "auto.txt")
        self.serve([{"content": "x"},
                    _call_delta("write_file", json.dumps({"path": target,
                                                          "content": "x"}))])
        self.serve([{"content": "done"}])
        talk = self.conversation()
        self.turn(talk, mode="auto", approve=None)
        self.assertTrue(os.path.exists(target), "auto held a write back")

    # -- the memory, and what it must NOT cover -----------------------------

    def test_always_stops_the_second_question_in_the_same_directory(self):
        first = os.path.join(self.work, "one.txt")
        second = os.path.join(self.work, "two.txt")
        self.serve([{"content": "a"},
                    _call_delta("write_file", json.dumps({"path": first,
                                                          "content": "1"}))])
        self.serve([{"content": "b"},
                    _call_delta("write_file", json.dumps({"path": second,
                                                          "content": "2"}))])
        self.serve([{"content": "done"}])
        asked = []
        talk = self.conversation()
        self.addCleanup(crow_core.forget_approvals)
        self.turn(talk, mode="manual",
                  approve=lambda n, a: asked.append(a) or "always")
        self.assertEqual(len(asked), 1, "the same directory was asked about twice")
        self.assertTrue(os.path.exists(second))

    def test_always_does_not_widen_to_another_directory(self):
        """#88: "or the memory silently widens into auto"."""
        other = os.path.join(self.dir, "elsewhere")
        os.makedirs(other)
        self.serve([{"content": "a"},
                    _call_delta("write_file",
                                json.dumps({"path": os.path.join(self.work, "x"),
                                            "content": "1"}))])
        self.serve([{"content": "b"},
                    _call_delta("write_file",
                                json.dumps({"path": os.path.join(other, "y"),
                                            "content": "2"}))])
        self.serve([{"content": "done"}])
        asked = []
        talk = self.conversation()
        self.addCleanup(crow_core.forget_approvals)
        self.turn(talk, mode="manual",
                  approve=lambda n, a: asked.append(a) or "always")
        self.assertEqual(len(asked), 2, "a second directory rode in on the first")

    def test_always_does_not_widen_to_another_program(self):
        self.assertNotEqual(
            crow_core.approval_scope("run_command", json.dumps({"command": "git status"})),
            crow_core.approval_scope("run_command", json.dumps({"command": "rm -rf /"})))
        self.assertEqual(
            crow_core.approval_scope("run_command", json.dumps({"command": "git status"})),
            crow_core.approval_scope("run_command", json.dumps({"command": "git log"})),
            "two git calls should share one key")

    def test_a_call_with_no_scope_can_never_be_remembered(self):
        """Unparseable or empty arguments: every occurrence asks again, which is
        the safe direction for a case nobody has thought about."""
        self.assertIsNone(crow_core.approval_scope("run_command", "{not json"))
        self.assertIsNone(crow_core.approval_scope("run_command", json.dumps({})))
        self.assertIsNone(crow_core.approval_scope("read_file",
                                                   json.dumps({"path": "x"})))

    def test_forget_approvals_empties_the_memory(self):
        crow_core.remember("run_command", json.dumps({"command": "git status"}))
        self.assertTrue(crow_core.remembered("run_command",
                                             json.dumps({"command": "git log"})))
        crow_core.forget_approvals()
        self.assertFalse(crow_core.remembered("run_command",
                                              json.dumps({"command": "git log"})))


class DeclineIsNotAFailureTests(TurnLoopCase):
    """#95. The cost line called a user's own decision a malfunction.

    `DECLINED` begins with "error: " ON PURPOSE -- that prefix is what makes the
    model treat a refusal as recoverable rather than terminal, and
    `ReleaseLevelTests` pins it. The counter decided what to call a failure with
    the same prefix, so the two could not be told apart.

    MEASURED 2026-08-14 in the run that closed #55: `12 tool calls, 1 failed`,
    where the one "failure" was the read-before-write rule holding exactly as
    designed.
    """

    def _declining_turn(self, tool="write_file", answer="no"):
        self.serve([{"content": "on it"},
                    _call_delta(tool, json.dumps({"path": "x", "content": "y"}))])
        self.serve([{"content": "done"}])
        talk = self.conversation()
        return self.turn(talk, mode="manual", approve=lambda n, a: answer)

    def test_a_declined_call_is_not_counted_as_a_failure(self):
        cost = self._declining_turn().cost
        self.assertEqual(cost.tool_calls, 1)
        self.assertEqual(cost.tool_errors, 0)
        self.assertEqual(cost.tool_declined, 1)

    def test_the_cost_line_names_it_rather_than_hiding_it(self):
        """"No failures" was not the only option -- silence was. A call the user
        stopped is part of why the turn went the way it did."""
        line = self._declining_turn().cost.line()
        self.assertIn("1 declined", line)
        self.assertNotIn("failed", line)

    def test_a_real_failure_is_still_counted_as_one(self):
        """NEGATIVE CONTROL. A fix that stops counting anything passes every
        case above and cannot be told apart from one that works."""
        missing = os.path.join(self.work, "nothing-here.txt")
        self.serve([_call_delta("read_file", json.dumps({"path": missing}))])
        self.serve([{"content": "not there"}])
        cost = self.turn(self.conversation(), max_tool_rounds=4).cost
        self.assertEqual(cost.tool_errors, 1)
        self.assertEqual(cost.tool_declined, 0)
        line = cost.line()
        self.assertIn("1 failed", line)
        self.assertNotIn("declined", line)

    def test_a_turn_with_both_reports_both_separately(self):
        missing = os.path.join(self.work, "nothing-here.txt")
        self.serve([_call_delta("read_file", json.dumps({"path": missing}))])
        self.serve([_call_delta("write_file", json.dumps({"path": "x", "content": "y"}))])
        self.serve([{"content": "done"}])
        cost = self.turn(self.conversation(), mode="manual",
                         approve=lambda n, a: "no", max_tool_rounds=4).cost
        self.assertEqual((cost.tool_errors, cost.tool_declined), (1, 1))
        line = cost.line()
        self.assertIn("1 failed", line)
        self.assertIn("1 declined", line)

    def test_the_decline_still_reaches_the_screen(self):
        """DELIBERATE, and the narrower half of the change: only the COUNT was
        split. A declined call is still printed, because the reason the turn
        took as long as it did is not less relevant for having been the user's
        own choice. If this is ever changed it is its own decision."""
        self._declining_turn()
        self.assertIn("tool_failed", self.events.names)

    def test_declined_keeps_the_prefix_88_depends_on(self):
        """The whole defect came from that prefix, and removing it is the fix
        that must NOT be made: it is what stops the model treating a refusal as
        an abort."""
        self.assertTrue(crow_core.DECLINED.startswith("error: "))

    def test_a_declined_call_still_answers_its_tool_call(self):
        """The invariant underneath all of this: an assistant turn whose
        tool_calls have no tool message behind them is a broken prefix for every
        later turn. Counting differently must not change what is appended."""
        self.serve([{"content": "on it"},
                    _call_delta("write_file", json.dumps({"path": "x", "content": "y"}))])
        self.serve([{"content": "done"}])
        talk = self.conversation()
        self.turn(talk, mode="manual", approve=lambda n, a: "no")
        self.assertPrefixIsWhole(talk)


class TurnStateTests(TurnLoopCase):
    """What the loop carries across rounds, and what it must not carry across
    turns."""

    def test_the_seen_cache_is_emptied_at_the_start_of_every_turn(self):
        """THE ONE LINE OF THE LOOP THAT COULD HAVE BEEN LEFT BEHIND.

        `_SEEN.clear()` sat in `repl()`. With the loop moved and the line not,
        the core would answer every repeated call of every later turn out of a
        result from a turn that had already ended -- and nothing would say so,
        because a stale hit and a fresh one read identically."""
        talk = self.conversation()
        args = json.dumps({"path": self.work})
        self.serve([_call_delta("list_dir", args)])
        self.serve([{"content": "first"}])
        self.turn(talk, max_tool_rounds=4)
        self.assertEqual([e for e in self.events.log if e[0] == "tool_done"],
                         [("tool_done", "list_dir", False)])

        talk.append("user", "again please")
        self.events.log.clear()
        self.serve([_call_delta("list_dir", args, cid="c2")])
        self.serve([{"content": "second"}])
        self.turn(talk, max_tool_rounds=4)
        self.assertEqual([e for e in self.events.log if e[0] == "tool_done"],
                         [("tool_done", "list_dir", False)],
                         "the second turn was answered out of the first turn's cache")

    def test_the_same_call_twice_inside_one_turn_is_a_repeat(self):
        """The other direction, without which the clear above could be a
        `_SEEN` that never fills at all."""
        talk = self.conversation()
        args = json.dumps({"path": self.work})
        self.serve([_call_delta("list_dir", args, 0, "a")])
        self.serve([_call_delta("list_dir", args, 0, "b")])
        self.serve([{"content": "done"}])
        self.turn(talk, max_tool_rounds=4)
        self.assertEqual([e[2] for e in self.events.log if e[0] == "tool_done"],
                         [False, True])

    def test_a_failing_tool_is_reported_and_counted(self):
        talk = self.conversation()
        missing = os.path.join(self.work, "nothing-here.txt")
        self.serve([_call_delta("read_file", json.dumps({"path": missing}))])
        self.serve([{"content": "it was not there"}])
        result = self.turn(talk, max_tool_rounds=4)
        self.assertIn("tool_failed", self.events.names)
        self.assertEqual(result.cost.tool_errors, 1)
        self.assertEqual(result.cost.tool_calls, 1)

    def test_the_cache_promise_is_settled_on_the_first_round_only(self):
        talk = self.conversation()
        self.serve([_call_delta("list_dir", json.dumps({"path": self.work}))],
                   {"predicted_n": 1, "prompt_n": 5})
        self.serve([{"content": "done"}], {"predicted_n": 1, "prompt_n": 5})
        result = self.turn(talk, max_tool_rounds=4, promised_warm=True)
        self.assertEqual(self.events.names.count("cache_broken"), 0,
                         "no cached figure came back, so nothing was withdrawn")
        self.assertFalse(result.promised_warm, "the claim was not settled")

    def test_a_cache_that_did_not_hold_is_said_once(self):
        talk = self.conversation()
        self.serve([{"content": "done"}], {"predicted_n": 1})
        # stream_reply reads _cached_tokens off the usage block; the scripted
        # tail carries timings only, so the figure is fed in directly.
        original = crow_core.stream_reply

        def cold(conversation, **kw):
            text, reasoning, timings = original(conversation, **kw)
            timings["_cached_tokens"] = 0
            return text, reasoning, timings

        crow_core.stream_reply = cold
        try:
            result = self.turn(talk, max_tool_rounds=4, promised_warm=True)
        finally:
            crow_core.stream_reply = original
        self.assertEqual(self.events.names.count("cache_broken"), 1)
        self.assertFalse(result.promised_warm)

    def test_an_endpoint_error_stops_the_turn_without_appending_anything(self):
        talk = self.conversation()
        before = len(talk)
        crow_core._post_stream = self._raises(crow_core.CrowError("no endpoint"))
        result = self.turn(talk, max_tool_rounds=4)
        self.assertTrue(result.stopped)
        self.assertEqual(len(talk), before)
        self.assertEqual(self.events.log, [("failed", "no endpoint")])

    def test_an_interrupt_discards_the_partial_turn(self):
        """A truncated assistant message would poison the prefix for every
        later turn -- so nothing is appended, and the flag is cleared for the
        turn after this one."""
        talk = self.conversation()
        before = len(talk)
        crow_core._post_stream = self._raises(KeyboardInterrupt())
        result = self.turn(talk, max_tool_rounds=4)
        self.assertTrue(result.stopped)
        self.assertEqual(len(talk), before)
        self.assertEqual(self.events.names, ["interrupted"])
        self.assertFalse(crow_core.INTERRUPT.is_set())

    def test_an_interrupt_that_only_sets_the_flag_is_caught_too(self):
        """`_post_stream` returns quietly on Ctrl+C rather than raising, so the
        flag is what tells a stopped turn from a finished one."""
        talk = self.conversation()
        before = len(talk)

        def quiet(url, body, api_key, timeout):
            crow_core.INTERRUPT.set()
            return iter(())

        crow_core._post_stream = quiet
        result = self.turn(talk, max_tool_rounds=4)
        self.assertTrue(result.stopped)
        self.assertEqual(len(talk), before)
        self.assertEqual(self.events.names, ["interrupted"])
        self.assertFalse(crow_core.INTERRUPT.is_set())

    def _raises(self, exc):
        def transport(url, body, api_key, timeout):
            raise exc
            yield  # pragma: no cover -- makes this a generator

        return transport


class TheLoopLeftTheReplTests(unittest.TestCase):
    """The move itself, which no behaviour case can see.

    `repl()` was 292 lines and was called by 0 tests. Both halves of that are
    the reason this class exists: a loop that stayed behind would keep every
    other case here green, because every other case calls the core directly.
    """

    def test_repl_no_longer_streams_a_reply_or_runs_a_tool(self):
        source = inspect.getsource(crow.repl)
        self.assertIn("run_turn(", source)
        self.assertNotIn("stream_reply(", source)
        self.assertNotIn("run_tool_cached(", source)

    def test_repl_is_one_job_again(self):
        """292 lines when this stage started; 179 after it. The ceiling is
        generous on purpose -- what it forbids is the loop coming back, not a
        line of input handling being added."""
        self.assertLess(len(inspect.getsource(crow.repl).splitlines()), 220)

    def test_the_seen_cache_is_cleared_by_the_core_and_by_nobody_else(self):
        """`_SEEN.clear()` moved WITH the loop. Left in cli/crow.py it would go
        on being cleared for the CLI and never for a second surface."""
        self.assertNotIn("_SEEN.clear()", _source("crow.py"))
        self.assertIn("_SEEN.clear()", _source("crow_core.py"))

    def test_the_cost_line_is_assembled_in_one_place(self):
        """Its fields and their order are `TurnCost.line`, in the core. A second
        surface that spelled the same six numbers out again would be the second
        truth this whole plan exists to prevent -- and `_client_answer_s` is
        already mis-named (it holds a POINT IN TIME, not a duration), so the day
        that is corrected it must be correctable in one client."""
        self.assertNotIn("waited {format_clock", _source("crow.py"))
        self.assertIn("waited {format_clock", _source("crow_core.py"))
        self.assertNotIn("TurnCost()", _source("crow.py"),
                         "the client builds its own cost object")
        self.assertIn("cost.line()", inspect.getsource(crow.repl))


class TerminalTurnSinkTests(unittest.TestCase):
    """The other side of the loop's seam: cli/crow.py's `TerminalTurnEvents`.

    Twelve print statements, still in the client, still doing what they did.
    """

    def _sink(self, rounds=False):
        out = io.StringIO()
        return out, crow.TerminalTurnEvents(rounds=rounds, out=out)

    def test_the_sink_is_the_cores_seam(self):
        self.assertTrue(issubclass(crow.TerminalTurnEvents, crow_core.TurnEvents))

    def test_every_event_of_the_core_has_a_line_here(self):
        """A method the core reports and the CLI does not implement is a line
        that silently stopped being printed."""
        for name, value in vars(crow_core.TurnEvents).items():
            if name.startswith("_") or not callable(value):
                continue
            self.assertIn(name, vars(crow.TerminalTurnEvents),
                          f"{name} fires in the core and lands nowhere in the CLI")

    def test_the_round_line_is_a_bare_newline_without_the_switch(self):
        """--rounds does not decide whether the event fires, only whether the
        figures are printed. The newline was printed either way before."""
        out, events = self._sink(rounds=False)
        events.round_finished({"predicted_n": 7, "predicted_per_second": 3.0})
        self.assertEqual(out.getvalue(), "\n\n")

    def test_the_round_line_carries_the_figures_with_it(self):
        out, events = self._sink(rounds=True)
        events.round_finished({"predicted_n": 7, "predicted_per_second": 3.0})
        self.assertIn("7 tok @ 3.00 tok/s", out.getvalue())

    def test_a_tool_call_is_named_before_it_runs_and_left_open(self):
        out, events = self._sink()
        events.tool_started("read_file", json.dumps({"path": "a.txt"}))
        self.assertIn("read_file(", out.getvalue())
        self.assertFalse(out.getvalue().endswith("\n"),
                         "the outcome has to land on the same line")

    def test_a_sub_second_call_prints_no_duration(self):
        out, events = self._sink()
        events.tool_finished("read_file", 0.01, False)
        self.assertEqual(out.getvalue().strip(), "")

    def test_a_slow_call_prints_its_clock(self):
        out, events = self._sink()
        events.tool_finished("read_file", 2.0, False)
        self.assertIn("2.0s", out.getvalue())

    def test_a_repeated_call_says_so(self):
        out, events = self._sink()
        events.tool_finished("read_file", 0.0, True)
        self.assertIn("repeat", out.getvalue())

    def test_a_failed_call_shows_one_line_of_the_reason(self):
        out, events = self._sink()
        events.tool_failed("read_file", "error: no such file: x\nand more\nand more")
        self.assertIn("error: no such file: x", out.getvalue())
        self.assertNotIn("and more", out.getvalue())

    def test_reported_calls_read_like_calls_that_ran(self):
        out, events = self._sink()
        events.tools_reported([{"name": "list_dir", "arguments": json.dumps({"path": "."})}])
        self.assertIn("list_dir(", out.getvalue())
        self.assertIn("not run", out.getvalue())

    def test_the_reply_sink_carries_the_prompt_prefix(self):
        _, events = self._sink()
        reply = events.reply_events()
        self.assertIsInstance(reply, crow.TerminalEvents)
        self.assertIn("crow>", reply._prefix)

    def test_a_fresh_reply_sink_per_round(self):
        """A `TerminalEvents` owns a Renderer and a Raven, and those are per
        stream. Handing the same one to two rounds would feed the second round
        into a renderer that was already closed."""
        _, events = self._sink()
        self.assertIsNot(events.reply_events(), events.reply_events())


class ReportedNotRunIsAlsoTheCLIsTests(unittest.TestCase):
    """Done-criterion 2 for this stage: the CLI GAINS a mode, it loses none.

    "a change that makes the CLI second-class, or that moves shared behaviour
    into the GUI, fails here" -- so the mode is reachable from the command line
    and the default is what it always was.
    """

    def test_the_default_runs_tools(self):
        self.assertTrue(crow.build_parser().parse_args([]).run_tools)

    def test_the_flag_turns_them_off(self):
        self.assertFalse(crow.build_parser().parse_args(["--no-run-tools"]).run_tools)

    def test_the_core_defaults_to_running_them_too(self):
        """A surface that says nothing gets the behaviour the CLI has always
        had -- the mode is opt-in on both sides of the seam."""
        default = inspect.signature(crow_core.run_turn).parameters["execute_tools"].default
        self.assertIs(default, True)

    def test_repl_hands_the_flag_through(self):
        self.assertIn("execute_tools=", inspect.getsource(crow.repl))


class _FakeResponse:
    """What urlopen returns, reduced to the four things _http_text touches."""

    def __init__(self, body: bytes, ctype: str = "text/html", charset: str = "utf-8"):
        self._body, self._ctype, self._charset = body, ctype, charset

    class _Headers:
        def __init__(self, ctype, charset):
            self._ctype, self._charset = ctype, charset

        def get(self, name, default=None):
            return self._ctype if name == "Content-Type" else default

        def get_content_charset(self):
            return self._charset

    @property
    def headers(self):
        return self._Headers(self._ctype, self._charset)

    def read(self, limit=None):
        return self._body[:limit] if limit else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Urlopen:
    """Swaps crow_core's urlopen for the length of a `with`, and records the URL."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.seen: list[str] = []
        self.reqs: list = []

    def __enter__(self):
        self._real = crow_core.urllib.request.urlopen

        def fake(req, timeout=None):
            self.reqs.append(req)
            url = req.full_url if hasattr(req, "full_url") else req
            self.seen.append(url)
            # A callable outcome lets one test answer several hosts differently,
            # which the keyless federation needs: it asks five at once.
            out = self._outcome(url) if callable(self._outcome) else self._outcome
            if isinstance(out, Exception):
                raise out
            return out

        crow_core.urllib.request.urlopen = fake
        return self

    def __exit__(self, *exc):
        crow_core.urllib.request.urlopen = self._real
        return False


class WebExtractionTests(unittest.TestCase):
    """#96. What comes back from a page, and what must not."""

    def test_the_answer_survives_and_the_furniture_does_not(self):
        title, text = crow_core._extract_text(
            "<html><head><title>llama.cpp flags</title>"
            "<style>body{" + "x" * 500 + "}</style>"
            "<script>var a=" + "1" * 500 + ";</script></head>"
            "<body><nav>Home About Contact</nav><header>junk</header>"
            "<h1>--moe-stream</h1><p>Streams expert tensors from disk.</p>"
            "<footer>(c) nobody</footer></body></html>")
        self.assertEqual(title, "llama.cpp flags")
        self.assertIn("--moe-stream", text)
        self.assertIn("Streams expert tensors", text)
        for gone in ("var a", "Home About", "(c) nobody", "body{"):
            self.assertNotIn(gone, text)

    def test_an_extractor_that_returns_nothing_must_not_pass(self):
        """THE NEGATIVE HALF. Every case above is satisfied by returning "" --
        no script, no nav, no footer. Without this assertion the suite would
        certify an extractor that deletes the page."""
        _, text = crow_core._extract_text("<p>Streams expert tensors from disk.</p>")
        self.assertGreater(len(text), 20)

    def test_extraction_runs_before_the_clip_and_not_after(self):
        """The one that decides whether the tool works at all. 40 KB of script
        ahead of the answer: clip first and the model receives markup and no
        answer, because in BYTES the answer is below the fold even when it sits
        at the top of the screen."""
        page = ("<html><head><script>" + "z" * (crow_core.MAX_TOOL_BYTES * 3)
                + "</script></head><body><p>The flag is --moe-stream.</p></body></html>")
        self.assertGreater(len(page), crow_core.MAX_TOOL_BYTES)
        with _Urlopen(_FakeResponse(page.encode())):
            out = crow_core.tool_fetch_url("https://example.org/doc")
        self.assertIn("--moe-stream", out)
        self.assertLessEqual(len(out), crow_core.MAX_TOOL_BYTES + 200)

    def test_a_stray_closing_tag_does_not_disable_the_skip(self):
        """HTMLParser tracks no nesting -- its own docs say so -- so the depth
        counter is ours to keep. Unclamped it would go negative here and every
        later skip would be a no-op."""
        _, text = crow_core._extract_text(
            "</script></nav><p>visible</p><script>hidden</script>")
        self.assertIn("visible", text)
        self.assertNotIn("hidden", text)

    def test_character_references_arrive_as_characters(self):
        _, text = crow_core._extract_text("<p>b4321 &amp; later</p>")
        self.assertIn("b4321 & later", text)


class WebFetchTests(unittest.TestCase):
    """#96. Every way out of tool_fetch_url is a string the model can read."""

    def test_a_non_http_scheme_is_refused(self):
        """file:// would make this an unbounded read of the disk that goes
        AROUND #92's boundary rather than through it -- reads are unbounded
        there by design, so the refusal has to happen here."""
        for url in ("file:///C:/Windows/win.ini", "data:text/html,<p>x", "ftp://h/f"):
            self.assertTrue(crow_core.tool_fetch_url(url).startswith("error:"), url)

    def test_an_unreachable_host_is_a_tool_result_and_not_an_exception(self):
        """The invariant DECLINED already keeps: an assistant turn whose
        tool_calls have no tool message behind them is a broken prefix for every
        later turn. Offline is a normal state on this machine."""
        with _Urlopen(crow_core.urllib.error.URLError("getaddrinfo failed")):
            out = crow_core.tool_fetch_url("https://example.org/")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("offline", out)

    def test_an_http_error_names_its_code_rather_than_the_host(self):
        """HTTPError is a SUBCLASS of URLError. Caught in the other order, a 404
        would report as an unreachable host."""
        exc = crow_core.urllib.error.HTTPError("https://example.org/", 404,
                                               "Not Found", None, None)
        with _Urlopen(exc):
            out = crow_core.tool_fetch_url("https://example.org/")
        self.assertIn("404", out)
        self.assertNotIn("offline", out)

    def test_a_page_with_no_readable_text_says_so(self):
        with _Urlopen(_FakeResponse(b"<html><body><script>app()</script></body></html>")):
            self.assertIn("no readable text", crow_core.tool_fetch_url("https://e.org/"))

    def test_a_plain_text_body_is_passed_through(self):
        with _Urlopen(_FakeResponse(b"--moe-stream: stream experts",
                                    ctype="text/plain")):
            self.assertIn("--moe-stream", crow_core.tool_fetch_url("https://e.org/f.txt"))


class _Backend:
    """Points crow_core at one provider for the length of a `with`."""

    def __init__(self, *, tavily="", searxng=""):
        self._want = (tavily, searxng)

    def __enter__(self):
        self._had = (crow_core.TAVILY_KEY, crow_core.SEARXNG_URL)
        crow_core.TAVILY_KEY, crow_core.SEARXNG_URL = self._want
        return self

    def __exit__(self, *exc):
        crow_core.TAVILY_KEY, crow_core.SEARXNG_URL = self._had
        return False


class WebSearchTests(unittest.TestCase):
    """#96. The search is the capability; the snippets are the product."""

    def _json(self, payload):
        return _FakeResponse(json.dumps(payload).encode(), ctype="application/json")

    # -- no backend, which is what a fresh installation has --------------------

    # -- the keyless federation, which is what a fresh install runs -----------

    def _federation(self, **by_host):
        """Answers each keyless host from `by_host`, everything else empty."""
        def pick(url):
            for host, payload in by_host.items():
                if host in url:
                    return self._json(payload)
            return self._json({})
        return pick

    def test_a_fresh_install_searches_without_being_configured_first(self):
        """THE CASE THIS DESIGN EXISTS FOR. Crow arrives through one line of
        PowerShell; a capability that begins with "first create an account" is
        one nobody switches on. So with nothing set, the search still runs."""
        with _Backend(), _Urlopen(self._federation(
                **{"api.github.com/search/repositories": {"items": [
                    {"full_name": "ggml-org/llama.cpp", "html_url": "https://gh/l",
                     "description": "LLM inference in C/C++", "stargazers_count": 7}]}})) as spy:
            out = crow_core.tool_web_search("llama.cpp")
        self.assertIn("ggml-org/llama.cpp", out)
        self.assertIn("[github]", out)
        self.assertTrue(any("api.github.com" in u for u in spy.seen))
        self.assertTrue(any("wikipedia.org" in u for u in spy.seen))

    def test_no_source_is_asked_to_identify_as_a_browser(self):
        """MEASURED 2026-08-14, and the reason the open web is not among these
        sources: lite.duckduckgo.com answered 200 with 10 results to a browser
        user-agent and 202 with none to Crow's own, one URL and one second
        apart. A search that works only while Crow misrepresents itself breaks
        everywhere at once the day that check tightens."""
        with _Backend(), _Urlopen(self._federation()) as spy:
            crow_core.tool_web_search("anything")
        self.assertTrue(spy.reqs)
        for req in spy.reqs:
            agent = req.get_header("User-agent") or ""
            self.assertIn("Crow/", agent)
            self.assertNotIn("Mozilla", agent)
            self.assertNotIn("Chrome", agent)

    def test_one_dead_source_does_not_sink_the_search(self):
        """Five hosts are asked at once; one being down, slow or rate-limited is
        the normal case and must cost nothing but its own results."""
        def pick(url):
            if "stackexchange" in url:
                return crow_core.urllib.error.HTTPError(url, 429, "Too Many", None, None)
            if "wikipedia.org" in url:
                return self._json({"query": {"search": [{"title": "Llama.cpp",
                                                         "snippet": "a <b>C++</b> port"}]}})
            return self._json({})
        with _Backend(), _Urlopen(pick):
            out = crow_core.tool_web_search("llama.cpp")
        self.assertIn("Llama.cpp", out)
        self.assertIn("[wikipedia]", out)

    def test_the_same_url_from_two_sources_is_listed_once(self):
        """The payload parses as BOTH a repository and an issue, so two sources
        really do produce the same URL -- which is what the deduplication is
        for. Without the second parse this test passes with no duplicate to
        remove, and certifies nothing."""
        both = {"items": [{"full_name": "a/b", "html_url": "https://same",
                           "description": "", "stargazers_count": 0,
                           "number": 7, "title": "t", "state": "open"}]}
        with _Backend(), _Urlopen(self._federation(**{"api.github.com": both})):
            out = crow_core.tool_web_search("x")
        self.assertIn("https://same", out)
        self.assertEqual(out.count("https://same"), 1, out)

    def test_the_exact_answer_outranks_the_keyword_match(self):
        """MEASURED 2026-08-14: concatenating the sources put github first
        unconditionally, so "requests library current version" answered with a
        stranger's library-management project while pypi's exact `requests
        2.34.2` sat further down. The merge is round-robin in authority order,
        so line 1 is the best ANSWER and not the first source's first guess."""
        with _Backend(), _Urlopen(self._federation(**{
                "pypi.org": {"info": {"name": "requests", "version": "2.34.2",
                                      "summary": "Python HTTP for Humans."}},
                "api.github.com/search/repositories": {"items": [
                    {"full_name": "someone/library-manager", "html_url": "https://gh/x",
                     "description": "unrelated", "stargazers_count": 14}]}})):
            out = crow_core.tool_web_search("requests library current version")
        self.assertLess(out.index("pypi requests 2.34.2"),
                        out.index("someone/library-manager"), out)

    def test_an_empty_keyless_search_offers_the_upgrade_instead_of_a_fault(self):
        """These sources are code, packages and reference -- not the open web.
        A question outside them is a reason to offer more, not to report a
        breakage."""
        with _Backend(), _Urlopen(self._federation()):
            out = crow_core.tool_web_search("who won the 1974 world cup")
        self.assertIn("CROW_TAVILY_KEY", out)
        self.assertIn("not the open web", out)

    def test_a_version_question_reaches_the_package_registries(self):
        with _Backend(), _Urlopen(self._federation(**{"pypi.org": {"info": {
                "name": "requests", "version": "2.34.2",
                "summary": "Python HTTP for Humans."}}})) as spy:
            out = crow_core.tool_web_search("requests library current version")
        self.assertIn("pypi requests 2.34.2", out)
        self.assertTrue(any("pypi.org/pypi/requests" in u for u in spy.seen))

    def test_a_query_that_is_not_about_a_package_never_asks_a_registry(self):
        """MEASURED 2026-08-14, and the reason the gate exists: "llama.cpp moe
        stream flag" matched a music library manager on PyPI, and because this
        source ranks first, `Moe 2.5.0` led the results. A coincidental name
        match in the top slot is worse than noise -- it looks authoritative."""
        with _Backend(), _Urlopen(self._federation()) as spy:
            crow_core.tool_web_search("llama.cpp moe stream flag")
        self.assertFalse([u for u in spy.seen if "pypi.org" in u or "crates.io" in u],
                         "asked a package registry about a query with no package question")

    def test_one_long_description_cannot_eat_the_whole_result(self):
        """MEASURED the same day: three results came to 16,056 bytes because a
        single repository description was 15 KB. _clip then cut the tail, so the
        model paid full prefill for one project's marketing and never saw
        results two and three. A result count is not a size."""
        with _Backend(), _Urlopen(self._federation(
                **{"api.github.com/search/repositories": {"items": [
                    {"full_name": "a/b", "html_url": "https://gh/1",
                     "description": "x" * 20_000, "stargazers_count": 1}]}})):
            out = crow_core.tool_web_search("anything")
        self.assertLess(len(out), 2_000, "one snippet filled the budget")
        self.assertIn("a/b", out)

    def test_a_configured_backend_does_not_get_that_hint(self):
        """NEGATIVE HALF: the hint is for someone who has not upgraded. Printed
        to someone who has, it is noise that reads as a failure."""
        with _Backend(tavily="k"), _Urlopen(self._json({"results": []})):
            self.assertNotIn("CROW_TAVILY_KEY", crow_core.tool_web_search("x"))

    # -- tavily, the shipping default ----------------------------------------

    def test_the_key_goes_in_the_header_and_never_in_the_url(self):
        with _Backend(tavily="tvly-secret"), _Urlopen(self._json({"results": []})) as spy:
            crow_core.tool_web_search("moe stream")
        self.assertEqual(len(spy.reqs), 1)
        req = spy.reqs[0]
        self.assertEqual(req.full_url, crow_core.TAVILY_URL)
        self.assertEqual(req.get_header("Authorization"), "Bearer tvly-secret")
        self.assertNotIn("tvly-secret", req.full_url)
        self.assertIn(b"moe stream", req.data)

    def test_a_refused_key_says_which_variable_to_fix(self):
        exc = crow_core.urllib.error.HTTPError(crow_core.TAVILY_URL, 401,
                                               "Unauthorized", None, None)
        with _Backend(tavily="tvly-wrong"), _Urlopen(exc):
            out = crow_core.tool_web_search("x")
        self.assertIn("CROW_TAVILY_KEY", out)
        self.assertIn("401", out)

    def test_tavilys_finished_answer_is_printed_first(self):
        with _Backend(tavily="k"), _Urlopen(self._json(
                {"answer": "b4321", "results": [{"title": "t", "url": "u",
                                                 "content": "c"}]})):
            out = crow_core.tool_web_search("when was it added")
        self.assertTrue(out.startswith("answer: b4321"), out[:40])

    # -- searxng, for a machine that already runs one -------------------------

    def test_a_set_instance_url_wins_over_a_key(self):
        """Whoever set the URL meant it, and their instance has no ceiling."""
        with _Backend(tavily="k", searxng="http://127.0.0.1:8888"), \
                _Urlopen(self._json({"results": []})) as spy:
            crow_core.tool_web_search("moe stream")
        self.assertIn("format=json", spy.seen[0])
        self.assertTrue(spy.seen[0].startswith("http://127.0.0.1:8888"))

    def test_results_carry_snippets_so_a_fetch_is_not_needed(self):
        with _Backend(searxng="http://s"), _Urlopen(self._json({"results": [
                {"title": "moe-stream", "url": "https://ex.org/a",
                 "content": "Streams expert tensors from disk."}]})):
            out = crow_core.tool_web_search("moe stream")
        self.assertIn("https://ex.org/a", out)
        self.assertIn("Streams expert tensors", out)

    def test_an_instance_answering_html_says_what_to_change(self):
        """Its DEFAULT state: searxng ships `formats: [html]`, so format=json
        returns a page. Measured 2026-08-14: six public instances were probed
        and not one served JSON. A JSONDecodeError here would report a parser
        fault for a configuration nobody has made yet."""
        with _Backend(searxng="http://s"), \
                _Urlopen(_FakeResponse(b"<html><body>results</body></html>")):
            out = crow_core.tool_web_search("anything")
        self.assertIn("search.formats", out)
        self.assertIn("settings.yml", out)

    def test_a_dead_backend_does_not_read_as_the_web_knowing_nothing(self):
        """Zero results and every engine rate-limited look identical from here,
        and they are not the same answer."""
        with _Backend(searxng="http://s"), _Urlopen(self._json(
                {"results": [], "unresponsive_engines": [["google", "429"]]})):
            out = crow_core.tool_web_search("moe stream")
        self.assertIn("did not answer", out)
        with _Backend(searxng="http://s"), _Urlopen(self._json(
                {"results": [], "unresponsive_engines": []})):
            plain = crow_core.tool_web_search("moe stream")
        self.assertNotIn("did not answer", plain)

    def test_an_unreachable_instance_names_the_address_it_tried(self):
        with _Backend(searxng="http://127.0.0.1:8888"), \
                _Urlopen(crow_core.urllib.error.URLError("refused")):
            out = crow_core.tool_web_search("x")
        self.assertIn("127.0.0.1:8888", out)
        self.assertIn("CROW_TAVILY_KEY", out)


class WebToolsAreDeclaredTests(unittest.TestCase):
    """#96. The wiring, and the one entry whose absence is silent."""

    def test_both_tools_are_offered_and_implemented(self):
        offered = [t["function"]["name"] for t in crow_core.TOOLS]
        for name in ("web_search", "fetch_url"):
            self.assertIn(name, offered)
            self.assertIn(name, crow_core.TOOL_IMPL)

    def test_a_missing_class_entry_would_be_silent_at_auto(self):
        """needs_approval treats an unknown tool as `executing`, so a forgotten
        TOOL_CLASS entry is survivable at manual and wrong at auto -- the
        default. This asserts the entry itself, not the behaviour it produces."""
        for name in ("web_search", "fetch_url"):
            self.assertIn(name, crow_core.TOOL_CLASS)
            self.assertEqual(crow_core.TOOL_CLASS[name], "network")

    def test_the_network_class_asks_at_no_level(self):
        """robin, 2026-08-14: a search happens because a task was given, and
        giving the task is the release."""
        for mode in crow_core.MODES:
            for name in ("web_search", "fetch_url"):
                self.assertFalse(crow_core.needs_approval(name, mode), f"{name}/{mode}")

    def _search_description(self):
        return next(t["function"]["description"] for t in crow_core.TOOLS
                    if t["function"]["name"] == "web_search")

    def test_the_search_tool_says_a_list_of_links_is_not_an_answer(self):
        """The description is the only place the model learns that finding a
        URL is not the job. A wording that drops it produces exactly the turn
        #96 was opened against."""
        desc = self._search_description()
        self.assertIn("not an answer", desc)
        self.assertIn("name the sources", desc)

    def test_the_wording_makes_the_model_weigh_its_sources(self):
        """OBSERVED IN THE WINDOW, 2026-08-14. Asked about "Qwen3.8-27B" the
        model found three third-party pull requests and wrote a specification
        from them -- release date, licence, context window, vision projector.
        It was broadly right, and that is the uncomfortable part: the same
        procedure over the same class of source produces a confident answer
        whether or not it happens to be true. The instruction has to separate
        what a source establishes from what it merely mentions, because the
        model cannot tell the two apart from the result list alone."""
        desc = self._search_description()
        self.assertIn("unconfirmed", desc)
        self.assertIn("was not found", desc)

    def test_the_rule_carries_its_exception_for_a_user_who_asked(self):
        """THE OTHER FAILURE, and the reason "never give a link" is the wrong
        rule: someone who asks for the URL, the docs page or the source wants
        exactly that. A description that bans links outright is broken in the
        opposite direction and passes the test above."""
        self.assertIn("unless the user asked", self._search_description())


class AProjectIsAWorkingDirectoryTests(unittest.TestCase):
    """#119: the project list in roots.json, and what it refuses to be.

    A PROJECT IS NOT A NEW CONCEPT. It is a root somebody promoted, so every
    rule the boundary already has applies unchanged -- which is the whole reason
    these cases can be driven rather than read off the source.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="crow-proj-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._real = crow_core.ROOTS_FILE
        self.addCleanup(self._restore)
        crow_core.ROOTS_FILE = os.path.join(self.dir, "roots.json")

    def _restore(self) -> None:
        crow_core.ROOTS_FILE = self._real

    def _dir(self, name: str) -> str:
        path = os.path.join(self.dir, name)
        os.makedirs(path, exist_ok=True)
        return path

    def _doc(self) -> dict:
        with open(crow_core.ROOTS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_adding_one_declares_it_a_root_and_lists_it(self):
        """Both halves, because either alone is a project that does not work: a
        listed directory with no marker is filtered straight back out, and a
        marked one nobody listed never reaches the rail."""
        path = self._dir("Crow")
        self.assertTrue(crow_core.add_project(path))
        self.assertTrue(os.path.isfile(crow_core.root_file(path)))
        self.assertEqual([os.path.normcase(p) for p in crow_core.projects()],
                         [os.path.normcase(path)])

    def test_a_directory_that_is_already_a_root_keeps_its_mode(self):
        """NEGATIVE for the writer: promoting a root must not reset the release
        level somebody chose for it. `write_root_mode` is only called when there
        is no marker yet, and this is the case that holds it to that."""
        path = self._dir("bound")
        crow_core.write_root_mode(path, "allowedit")
        crow_core.add_project(path)
        self.assertEqual(crow_core.read_root_mode(path), "allowedit")

    def test_adding_twice_lists_it_once(self):
        """A repeated click is a repeated click, not a second project."""
        path = self._dir("Crow")
        crow_core.add_project(path)
        crow_core.add_project(path)
        self.assertEqual(len(crow_core.projects()), 1)
        # THE FILE, NOT ONLY THE READER. `projects()` dedupes on the way out, so
        # a writer that appended every time would still LOOK right -- and the
        # list would grow without bound behind a correct-looking rail.
        self.assertEqual(len(self._doc()["projects"]), 1)

    def test_the_order_is_the_order_they_were_added(self):
        """UNLIKE `recent`, which is newest-first because a picker wants the last
        thing. A rail that reordered itself under the mouse cannot be aimed at."""
        first, second = self._dir("aaa"), self._dir("zzz")
        crow_core.add_project(first)
        crow_core.add_project(second)
        self.assertEqual([os.path.basename(p) for p in crow_core.projects()],
                         ["aaa", "zzz"])

    def test_a_project_whose_marker_is_gone_is_not_listed(self):
        """The filter `known_roots` applies, for the same reason: a directory
        that no longer declares itself would offer a boundary that is not there.
        NEGATIVE PROBE for `test_adding_one...` -- without the filter that case
        passes on a directory somebody deleted."""
        path = self._dir("Crow")
        crow_core.add_project(path)
        os.remove(crow_core.root_file(path))
        self.assertEqual(crow_core.projects(), [])
        self.assertIn(path, self._doc()["projects"],
                      "the entry is filtered on read, not deleted on sight")

    def test_projects_and_recent_are_separate_keys(self):
        """THE WHOLE REASON THIS KEY EXISTS. `recent` is capped at eight, so a
        project on that list would fall out of the rail on the ninth folder
        anybody ever opened."""
        path = self._dir("Crow")
        crow_core.add_project(path)
        for n in range(9):
            other = self._dir("other%d" % n)
            crow_core.write_root_mode(other, crow_core.DEFAULT_MODE)
            crow_core.remember_root(other)
        self.assertNotIn(os.path.normcase(path),
                         [os.path.normcase(p) for p in crow_core.known_roots()],
                         "the cap did not evict it -- this case proves nothing")
        self.assertEqual([os.path.normcase(p) for p in crow_core.projects()],
                         [os.path.normcase(path)])

    def test_remembering_a_root_does_not_delete_the_projects(self):
        """The read-modify-write `_write_roots` was fixed for once already, when
        it dropped `active`. A third key is a third thing it can drop."""
        path = self._dir("Crow")
        crow_core.add_project(path)
        other = self._dir("anders")
        crow_core.write_root_mode(other, crow_core.DEFAULT_MODE)
        crow_core.remember_root(other)
        crow_core.set_active_root(other)
        self.assertEqual(len(crow_core.projects()), 1)

    def test_a_subdirectory_of_a_project_is_not_in_it(self):
        """`find_root` takes the NEAREST marker and not the highest, so a
        sub-directory that declares itself is its own root. Folding it into the
        project above would contradict the rule the boundary is built on."""
        top = self._dir("Crow")
        crow_core.add_project(top)
        inner = self._dir(os.path.join("Crow", "cli"))
        self.assertTrue(crow_core.is_project(top))
        self.assertFalse(crow_core.is_project(inner))

    def test_dropping_one_leaves_the_marker_and_the_directory(self):
        """A boundary that disappeared because a list was tidied is the failure
        the root mechanism exists to prevent. The row goes; nothing else does."""
        path = self._dir("Crow")
        crow_core.add_project(path)
        crow_core.drop_project(path)
        self.assertEqual(crow_core.projects(), [])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(os.path.isfile(crow_core.root_file(path)))
        self.assertFalse(crow_core.is_project(path))

    def test_nothing_is_a_project_before_anybody_says_so(self):
        """NEGATIVE PROBE for `is_project`: a directory that merely HAS a marker
        -- and `.crow/` appears wherever crow runs -- is not a project."""
        path = self._dir("zufall")
        crow_core.write_root_mode(path, crow_core.DEFAULT_MODE)
        self.assertFalse(crow_core.is_project(path))
        self.assertFalse(crow_core.is_project(None))
        self.assertEqual(crow_core.projects(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
