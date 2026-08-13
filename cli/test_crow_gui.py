#!/usr/bin/env python3
"""Suite for the window: cli/crow_gui.py against cli/crow_core.py.

Run:  python cli/test_crow_gui.py

WHY THIS FILE IS NAMED IN THE PLAN. #90's E12 asks for one positive case with a
predicate AND one case that has to fail, per capability -- and before this file
there was nowhere to put them. It is a `test_*.py`, so
tools/pack-release.ps1:156-159 matches it with Get-DevOnlyFiles and drops it from
the stage: the window ships, this does not, and the package count goes 27 -> 28
rather than 29.

WHAT EVERY CASE IN HERE IS BUILT AGAINST. A window can be wrong in two ways a
screenshot cannot show: it can draw something the core never said, and it can
fail to draw something the core did say. So the predicates read WIDGET STATE --
Tk's own `displaychars` count, Tk's own `dump`, the real clipboard -- and never a
shadow copy the window keeps beside the widget. A transcript that agreed with
itself while the screen showed something else is exactly the failure a test that
asks the window what it drew cannot see.

THE FIXTURES COST NOTHING. No model, no server, no network: every stream in here
is a list of recorded chunks fed through the REAL `crow_core.stream_reply` and
the REAL `crow_core.run_turn` by rebinding `_post_stream`, which is the only door
there is. Two things in E12 need a running server and are NOT here -- the abort
proof (the next question answering in under 30 s) and the two live directions of
the session round trip -- and they sit in E14 by the plan's own arrangement. What
IS here is the file-level round trip, which needs nothing and settles the part
that can be settled: one session file, two doors, the same messages.

TK IS REQUIRED, and a machine without it skips rather than passes. E9 measured Tk
8.6.15 under Python 3.13.3 here and made that the reference value; a skip on a
headless box is honest, a pass would not be.
"""

from __future__ import annotations

import difflib
import gc
import io
import json
import os
import queue
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import crow            # noqa: E402
import crow_core       # noqa: E402
import crow_gui        # noqa: E402

try:
    import tkinter as tk
    _ROOT_ERROR: str | None = None
    try:
        _probe = tk.Tk()
        _probe.destroy()
    except Exception as exc:                     # noqa: BLE001
        _ROOT_ERROR = str(exc)
except Exception as exc:                         # noqa: BLE001
    tk = None                                    # type: ignore[assignment]
    _ROOT_ERROR = str(exc)

needs_tk = unittest.skipUnless(_ROOT_ERROR is None,
                               "no Tk display: %s" % _ROOT_ERROR)


# ---------------------------------------------------------------- fixtures --

# THE STREAM E4's SEAM WAS CUT AGAINST, and the one both surfaces are held to
# below: reasoning, then an answer, in the shape the server sends them.
RECORDED = [
    {"reasoning_content": "the socket is the question here"},
    {"content": "The read sits in _post_stream.\n"},
    {"content": "A close from outside reaches the buffer.\n"},
]

# P3 IN FIVE DELTAS: think, answer, THINK AGAIN, answer again, inside one turn.
# Same fixture cli/test_crow_core.py cuts E10's state machine against, repeated
# here on purpose -- the window has to survive the identical shape, and a test
# file that invented its own would be measuring a different stream.
RE_ENTRY = [
    {"reasoning_content": "first I "},
    {"reasoning_content": "consider it"},
    {"content": "ANSWER ONE\n"},
    {"reasoning_content": "wait -- I should check"},
    {"content": "ANSWER TWO\n"},
]

# The timings block, in the two shapes the open question of #90 names: carried
# only on the last chunk, and carried on every one of them. Both have to produce
# the same cost line.
TIMINGS = {"predicted_n": 252, "predicted_ms": 17060.0, "predicted_per_second": 14.77,
           "prompt_n": 528, "prompt_ms": 7066.0, "prompt_per_second": 74.72}
USAGE = {"total_tokens": 11507, "prompt_tokens_details": {"cached_tokens": 10979}}


class _Clock:
    """`time` with a monotonic that does not move.

    The cost line ends in `waited`, which is wall clock. Two runs of the same
    fixture would differ there by a millisecond and a comparison demanding
    CHARACTER equality would be measuring the machine's mood. Everything else on
    the module is passed straight through, so nothing else changes behaviour.
    """

    def __init__(self, at: float = 1000.0) -> None:
        self._at = at

    def monotonic(self) -> float:
        return self._at

    def __getattr__(self, name: str):
        return getattr(time, name)


def chunks_for(deltas: list[dict], timings: dict | None = None,
               every: bool = False) -> list[str]:
    """A recorded stream as the wire carries it: one JSON payload per chunk."""
    out = []
    for delta in deltas:
        chunk: dict = {"choices": [{"delta": delta}]}
        if timings and every:
            chunk["timings"] = timings
        out.append(json.dumps(chunk))
    tail: dict = {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": USAGE}
    if timings:
        tail["timings"] = timings
    out.append(json.dumps(tail))
    return out


class GuiCase(unittest.TestCase):
    """A window, a scripted endpoint, and everything put back afterwards."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="crow-gui-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._post_before = crow_core._post_stream
        self._session_file_before = crow_core.SESSION_FILE
        self._session_dir_before = crow_core.SESSION_DIR
        self._time_before = crow_core.time
        self.addCleanup(self._restore)
        crow_core.SESSION_DIR = self.dir
        crow_core.SESSION_FILE = os.path.join(self.dir, "session.json")
        crow_core.INTERRUPT.clear()
        self.windows: list = []

    def _restore(self) -> None:
        crow_core._post_stream = self._post_before
        crow_core.SESSION_FILE = self._session_file_before
        crow_core.SESSION_DIR = self._session_dir_before
        crow_core.time = self._time_before
        crow_core.INTERRUPT.clear()
        # NEWEST FIRST. Tk keeps one default root per process, and destroying the
        # FIRST one while a later one is still alive aborts the interpreter with
        # "Tcl_AsyncDelete: async handler deleted by the wrong thread". Measured
        # here on the case that opens two windows.
        for window in reversed(getattr(self, "windows", [])):
            self.close(window)

    def close(self, window) -> None:
        """Give a window back: its probe joined, its tick cancelled, destroyed.

        AND COLLECTED, ON THIS THREAD. A destroyed `tk.Tk` still deletes its Tcl
        interpreter when the object itself is freed, and CPython frees it on
        whichever thread happens to trigger the collection -- a worker or a probe
        thread will do. Tcl answers that with "Tcl_AsyncDelete: async handler
        deleted by the wrong thread" and aborts the process, which is how this
        suite died twice before this line: not a failing case, a dead runner.
        """
        try:
            if window.prober is not None:
                window.prober.join(5.0)
            window._release()
            window.destroy()
        except Exception:
            pass
        if window in self.windows:
            self.windows.remove(window)
        # `_release` sets the interrupt flag, which is right for a window that is
        # going away and wrong for the next case in this process: `run_turn`
        # would see it and report an interrupted turn before its first round.
        # In production there is one window per process and no "next case".
        crow_core.INTERRUPT.clear()
        gc.collect()

    def window(self, *argv: str):
        """A real window, hidden, with its tick NOT running and its probe drained.

        THE TICK IS CANCELLED and every case drives `_drain()` itself: a suite
        that raced a timer would be measuring the scheduler, and a drain called
        by hand is the same code path the tick calls.

        THE STARTUP PROBE IS WAITED OUT AND DRAINED, because it is real: the
        window asks /health the moment it opens, and its answer is already in the
        queue before any case has put anything there. A case that counted it
        would be counting the window's own start.
        """
        # ONE AT A TIME. `CrowWindow` is a `tk.Tk`, which is right for a program
        # that is one window -- and two live roots in one process abort the Tcl
        # interpreter with "Tcl_AsyncDelete: async handler deleted by the wrong
        # thread" when the first is destroyed. Measured here twice before this
        # line existed.
        for open_window in list(self.windows):
            self.close(open_window)
        args = crow_gui.build_parser().parse_args(
            ["--no-session", "--base-url", "http://127.0.0.1:1/v1", *argv])
        window = crow_gui.CrowWindow(args)
        window.withdraw()
        if window._tick_id is not None:
            window.after_cancel(window._tick_id)
            window._tick_id = None
        if window.prober is not None:
            window.prober.join(5.0)
        while window._drain():
            pass
        window.transcript.states = []
        window.update()
        self.windows.append(window)
        return window

    def serve(self, payloads: list[str]) -> None:
        """Script the endpoint. The REAL stream loop runs behind it."""
        def fake(url, body, api_key, timeout):
            for payload in payloads:
                yield payload
        crow_core._post_stream = fake

    def freeze_clock(self) -> None:
        crow_core.time = _Clock()

    def run_turn_into(self, window, deltas: list[dict], timings: dict | None = None,
                      every: bool = False):
        """One turn, streamed into the window's queue, then drained per tick.

        Drained ONE CHUNK AT A TIME on purpose: that is what the tick sees at a
        real decode rate, and it is what makes the recorded widget states below
        mean anything.
        """
        self.serve(chunks_for(deltas, timings, every))
        # Cleared per turn, exactly where `_send` clears it: an interrupt left
        # over from the previous turn must not kill the next one before it
        # starts.
        crow_core.INTERRUPT.clear()
        window.conversation.append("user", "q")
        events = crow_gui.WindowTurnEvents(window.events)
        turn = crow_core.run_turn(
            window.conversation, base_url="http://x/v1", model="crow", api_key="k",
            temperature=0.0, top_p=0.95, min_p=0.01, timeout=1.0,
            max_tool_rounds=0, events=events)
        while window._drain():
            window.update()
        window.update()
        return turn


# ------------------------------------------------------------------- F1 -----

@needs_tk
class StreamingTests(GuiCase):
    """F1: "tokens appear while generated, not at the end".

    THE PREDICATE IS A COUNT OF RECORDED WIDGET STATES, not an impression. The
    window writes down what Tk is displaying after every tick that changed it
    (`Transcript.record_state`), so "it streamed" becomes a number.
    """

    # Written down here rather than derived, so a fixture that quietly shrinks
    # cannot make the predicate easier: N chunks in, at least K distinct states
    # on the way.
    N = 12
    K = 10

    def _drip(self, window, pieces: list[str]) -> None:
        """Put one delta in the queue, drain, repeat -- one chunk per tick.

        THE RECORDING STARTS AFTER THE TURN HAS OPENED, so every state counted
        below was caused by an ANSWER delta and not by the blank line that opens
        an assistant turn. Both cases in this class use the same driver, which is
        what makes the difference between them the stream and nothing else.
        """
        sink = crow_gui.WindowReplyEvents(window.events)
        sink.reply_started()
        window._drain()
        window.transcript.states = []
        for piece in pieces:
            sink.answer_text(piece)
            window._drain()
        sink.reply_finished()
        window._drain()
        window.update()

    def test_a_stream_of_n_chunks_leaves_at_least_k_visible_states(self):
        """POSITIVE. Twelve deltas, and the screen has to have been caught in at
        least ten different shapes on the way to the finished answer."""
        window = self.window()
        self._drip(window, ["tok%02d " % i for i in range(self.N)])
        states = window.transcript.states
        self.assertGreaterEqual(
            len(states), self.K,
            "%d recorded widget states for %d chunks -- the answer appeared in "
            "fewer steps than it was sent in" % (len(states), self.N))
        self.assertEqual(states, sorted(states), "the visible text shrank")
        self.assertIn("tok11", window.transcript.visible_answer())

    def test_one_chunk_fills_the_window_only_at_the_end(self):
        """NEGATIVE, and it is what stops the case above from measuring the
        toolkit. The same driver, the same number of drains, the whole answer in
        ONE delta: exactly one state, however often the tick runs. If this ever
        counted more, the positive case above was measuring Tk's own drawing
        latency and not the stream."""
        window = self.window()
        whole = "".join("tok%02d " % i for i in range(self.N))
        self._drip(window, [whole])
        for _ in range(self.N):
            window._drain()
            window.update()
        self.assertEqual(
            len(window.transcript.states), 1,
            "a single-chunk answer produced %d visible states -- the states are "
            "not coming from the stream" % len(window.transcript.states))
        self.assertEqual(window.transcript.visible_answer(), whole)


# ------------------------------------------------------------------- F2 -----

@needs_tk
class ThoughtBlockTests(GuiCase):
    """F2: reasoning and answer shown separately, the reasoning collapsible.

    THE STATE MACHINE IS NOT IN THIS FILE'S SUBJECT. Where a block begins, ends
    and BEGINS AGAIN is `crow_core.ReasoningBlocks` (E10). What is under test
    here is whether the window carries that decision to the screen without
    adding one of its own.
    """

    def test_a_re_entering_stream_folds_two_blocks_and_neither_eats_the_answer(self):
        """POSITIVE, against the re-entry fixture (P3).

        THE PREDICATE, and it is not open to interpretation: with every thought
        block FOLDED, the number of characters Tk is displaying inside the answer
        equals the sum of the `content` deltas. One character more and something
        that was not the answer is on screen; one less and part of the answer is
        inside a folded block, where nobody reads it.
        """
        window = self.window()
        self.run_turn_into(window, RE_ENTRY)
        expected = "".join(d["content"] for d in RE_ENTRY if "content" in d)
        self.assertEqual(window.transcript.visible_answer(), expected)
        self.assertEqual(window.transcript._thoughts, 2,
                         "the model re-entered reasoning and the window drew one block")
        for piece in ("first I", "wait -- I should check"):
            self.assertNotIn(piece, window.transcript.visible_answer())

    def test_unfolding_a_block_shows_it_and_folding_it_hides_it_again(self):
        """The collapsible half, read off the widget's own display count."""
        window = self.window()
        self.run_turn_into(window, RE_ENTRY)
        folded = window.transcript.visible_chars()
        window.transcript.toggle_thought("think1", "thinkhead1")
        window.update()
        opened = window.transcript.visible_chars()
        self.assertGreater(opened, folded, "unfolding showed nothing")
        window.transcript.toggle_thought("think1", "thinkhead1")
        window.update()
        self.assertEqual(window.transcript.visible_chars(), folded)

    def test_a_stream_without_reasoning_draws_no_empty_block(self):
        """NEGATIVE half one. A turn that never thinks must not leave an empty
        container on the screen -- a window that opened one per turn would put a
        fold control over nothing."""
        window = self.window()
        self.run_turn_into(window, [{"content": "PLAIN\n"}])
        self.assertEqual(window.transcript._thoughts, 0)
        self.assertEqual(window.transcript.visible_answer(), "PLAIN\n")

    def test_a_hard_coded_think_first_then_answer_wraps_the_answer_in_one_block(self):
        """NEGATIVE half two, and it is the one that proves the fixture bites.

        The same window, the same stream, with the core's state machine swapped
        for one that opens a block at the first thought and closes it at the end
        of the turn -- what anybody writes after watching one stream.

        THE PREDICATE IS THE BLOCK'S TAG RANGES, not the answer's length, and
        the difference is worth writing down. This window tags answer text
        separately, so folding a block cannot swallow a character of the answer
        -- the length predicate above holds either way, which would make it a
        useless counter-probe on its own. What the hard-coded version DOES cost
        is visible in the widget: block 1 is no longer one run of text. It is
        two, with the first answer line between them, so unfolding it drops a
        thought into the middle of the answer and the second block that should
        have carried it never exists.
        """
        window = self.window()
        original = crow_core.ReasoningBlocks
        crow_core.ReasoningBlocks = _ThinkFirstThenAnswer
        try:
            self.run_turn_into(window, RE_ENTRY)
        finally:
            crow_core.ReasoningBlocks = original
        self.assertEqual(window.transcript._thoughts, 1,
                         "the hard-coded version cannot open a second block")
        ranges = window.transcript.widget.tag_ranges("think1")
        self.assertGreater(
            len(ranges), 2,
            "block 1 is one contiguous run -- then the hard-coded version did "
            "not straddle the answer and this counter-probe proves nothing")
        self.assertIn("ANSWER ONE", window.transcript.widget.get(ranges[1], ranges[2]),
                      "the answer is not between the two halves of the block")

    def test_the_honest_state_machine_keeps_each_block_in_one_piece(self):
        """The positive half of the case above, so the predicate has both ends:
        with the core's automaton every block is ONE run and the answer follows
        it instead of standing inside it."""
        window = self.window()
        self.run_turn_into(window, RE_ENTRY)
        for tag in ("think1", "think2"):
            self.assertEqual(len(window.transcript.widget.tag_ranges(tag)), 2,
                             "%s is drawn in more than one piece" % tag)


class _ThinkFirstThenAnswer:
    """The version E10 ruled out, kept here to be driven and not to be used.

    Same surface as `crow_core.ReasoningBlocks`, so `stream_reply` can be driven
    against it by rebinding the module global: the counter-probe then runs
    through the REAL loop and the REAL window instead of beside them. The one
    line it is missing is `self.finish()` in `content_delta`.
    """

    def __init__(self, events=None) -> None:
        self._events = events or crow_core.ReplyEvents()
        self.blocks: list[str] = []
        self.open = False
        self.reasoning_chars = 0
        self.content_chars = 0
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.blocks) + "".join(self._parts)

    def reasoning_delta(self, piece: str) -> None:
        if not piece:
            return
        if not self.open:
            self.open = True
            self._events.reasoning_started(1)
        self._parts.append(piece)
        self.reasoning_chars += len(piece)
        self._events.reasoning_text(piece)

    def content_delta(self, piece: str) -> None:
        if not piece:
            return
        self.content_chars += len(piece)

    def finish(self) -> None:
        if not self.open:
            return
        self.blocks.append("".join(self._parts))
        self._parts = []
        self.open = False
        self._events.reasoning_finished()


# ------------------------------------------------------------------- F3 -----

class _FakeProps:
    """A /props answer, in the shape urlopen hands one back."""

    def __init__(self, doc: dict) -> None:
        self._body = json.dumps(doc).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeProps":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


@needs_tk
class ConnectionTests(GuiCase):
    """F3: the connection state and the model the server has OPEN, from /props.

    Both halves, because either alone passes a client that lies. A window with a
    hard-coded model name passes the "goes empty when the server is gone" case
    for the wrong reason; a window that shows whatever it last saw passes the
    "shows the model" case for the wrong reason.
    """

    MODEL_PATH = (r"C:\models\DeepSeek-V4-Flash-0731-UD-IQ2_XXS-00001-of-00004.gguf")
    EXPECTED = "DeepSeek-V4-Flash-0731"

    def _answer(self, doc: dict | None) -> None:
        """Point every /health and /props call at a fixture, or at nothing."""
        self._urlopen_before = crow_core.urllib.request.urlopen

        def fake(url, timeout=None, **_kw):
            if doc is None:
                raise urllib.error.URLError("connection refused")
            if str(url).endswith("/health"):
                return _FakeProps({"status": "ok"})
            return _FakeProps(doc)

        crow_core.urllib.request.urlopen = fake
        self.addCleanup(setattr, crow_core.urllib.request, "urlopen",
                        self._urlopen_before)

    def test_the_chip_is_character_identical_to_the_field_from_props(self):
        """POSITIVE, and the half a hard-coded name would fail. The mockup shows
        DeepSeek-V3.1-UD-IQ2_XXS and the shipped model is a different one -- the
        chip carries whatever /props said, character for character."""
        self._answer({"model_path": self.MODEL_PATH,
                      "default_generation_settings": {"n_ctx": 200000}})
        window = self.window()
        window._probe()
        window._drain()
        window.update()
        self.assertEqual(window.model_chip.cget("text"), self.EXPECTED)
        self.assertEqual(window.state_chip.cget("text"), "verbunden (ok)")
        self.assertEqual(window.ctx_chip.cget("text"), "n_ctx 200k")

    def test_a_server_that_names_a_different_model_moves_the_chip(self):
        """The other direction of the same half: the chip follows the server
        rather than a value this file chose."""
        self._answer({"model": "Qwen3-Coder-30B-BF16"})
        window = self.window()
        window._probe()
        window._drain()
        window.update()
        self.assertEqual(window.model_chip.cget("text"), "Qwen3-Coder-30B")

    def test_no_server_leaves_the_model_chip_empty(self):
        """NEGATIVE. Stop the server, open the window: the chip stays EMPTY
        rather than showing the last name it knew. An empty chip is a question
        the user can act on; a remembered one is a confident wrong answer."""
        self._answer(None)
        window = self.window()
        window._probe()
        window._drain()
        window.update()
        self.assertEqual(window.model_chip.cget("text"), "")
        self.assertEqual(window.state_chip.cget("text"), "getrennt")

    def test_a_server_that_will_not_say_leaves_it_empty_too(self):
        """/props answered and named nothing. Same rule, and the reason it is a
        separate case: "reachable" and "willing to say" are two states, and only
        one of them is an error."""
        self._answer({"default_generation_settings": {}})
        window = self.window()
        window._probe()
        window._drain()
        window.update()
        self.assertEqual(window.model_chip.cget("text"), "")
        self.assertEqual(window.state_chip.cget("text"), "verbunden (ok)")


# ------------------------------------------------------------- F4 and P1 ----

def _silent_server(after: int = 1) -> tuple[int, socket.socket]:
    """A loopback endpoint that sends `after` chunks and then says nothing.

    The shape P1 is about: the socket is alive, the read is blocked, and nothing
    will ever arrive to unblock it.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def serve() -> None:
        try:
            conn, _ = sock.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Connection: close\r\n\r\n")
            for i in range(after):
                conn.sendall(json.dumps({"choices": [{"delta": {"content": "x%d" % i}}]})
                             .encode("utf-8").join((b"data: ", b"\n\n")))
            # and then nothing at all, until the test is done with it
            time.sleep(30)
        except Exception:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1], sock


class AbortPathTests(unittest.TestCase):
    """P1: "`cancel()` against a blocked `readline()` ... the abort button
    SILENTLY DOES NOTHING and the turn runs to completion."

    THE EFFECT IS NOT THE PROOF, and E12 says so: the interface reacting can be
    faked with a flag. The proof that matters is the next question being
    answered, and it needs a server -- it is in E14. What is here is the CAUSE:
    the second abort path exists, it is armed where the core says it is, and a
    reader that survives anyway is written down instead of looking finished.
    """

    def setUp(self) -> None:
        crow_core.INTERRUPT.clear()
        self.addCleanup(crow_core.INTERRUPT.clear)

    def _abort_after(self, timeout: float) -> float:
        """One aborted turn against a server that has gone quiet. Seconds taken.

        One chunk arrives, then nothing; the flag is set and the generator is
        closed. What comes back is how long the abort took to return AND to
        leave no reader behind, which is the only thing "the abort took" can
        mean at the layer where P1 lives.
        """
        crow_core.INTERRUPT.clear()
        port, sock = _silent_server()
        self.addCleanup(sock.close)
        before = threading.active_count()
        stream = crow_core._post_stream("http://127.0.0.1:%d/v1/chat/completions" % port,
                                        {"x": 1}, "k", timeout)
        self.assertTrue(next(stream), "the fixture sent nothing")
        started = time.monotonic()
        crow_core.INTERRUPT.set()
        stream.close()
        deadline = started + timeout + 10.0
        while threading.active_count() > before and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertLessEqual(
            threading.active_count(), before,
            "the reader outlived even the read timeout -- P1 with no bound left")
        return time.monotonic() - started

    def test_the_read_timeout_is_what_bounds_an_aborted_reader(self):
        """THE SECOND ABORT PATH, END TO END OVER A REAL SOCKET.

        Two aborts against the same silent server, one with a 1 s read timeout
        and one with a 4 s one. If the close were what ended the read, both would
        return at the same moment; they do not, and the difference IS the read
        timeout doing the work. That is why cli/crow_gui.py ships one instead of
        inheriting the CLI's 1800 s.
        """
        quick = self._abort_after(1.0)
        slow = self._abort_after(4.0)
        self.assertLess(quick, 3.0, "a 1 s read timeout took %.1f s" % quick)
        self.assertGreater(slow, quick + 1.0,
                           "a 4 s read timeout returned as fast as a 1 s one -- "
                           "then something other than the timeout ended the read "
                           "and the window's bound is measuring nothing")

    def test_the_window_ships_a_read_timeout_the_cli_does_not(self):
        """The number itself, held between the two things that decided it: it has
        to clear the worst measured prefill (469.51 s to the first token on a
        resumed 21k session) and stay under the CLI's 1800 s, which bounds
        nothing."""
        self.assertGreater(crow_gui.READ_TIMEOUT_S, 469.51)
        self.assertLess(crow_gui.READ_TIMEOUT_S, 1800.0)
        self.assertEqual(crow_gui.build_parser().parse_args([]).timeout,
                         crow_gui.READ_TIMEOUT_S)


@needs_tk
class AbortTraceTests(GuiCase):
    """The other half of P1: when all three paths fail, the window SAYS SO."""

    def test_a_worker_that_outlives_the_abort_is_written_down(self):
        """POSITIVE. The failure P1 names is not a crash: the screen looks
        finished while the turn runs on and slot 0 stays held. A window that says
        nothing is indistinguishable from one where the abort worked."""
        window = self.window()
        stop = threading.Event()
        self.addCleanup(stop.set)
        worker = threading.Thread(target=stop.wait, daemon=True)
        worker.start()
        window.worker = worker
        window.running = True
        window._stop()
        self.assertFalse(window.running, "the interface stayed blocked")
        window.aborted_at = time.monotonic() - crow_gui.ABORT_GRACE_S - 0.1
        window._drain()
        window.update()
        text = window.transcript.widget.get("1.0", "end-1c")
        self.assertIn("der Leser lebt", text)
        self.assertIn("Slot 0 bleibt belegt", text)

    def test_a_worker_that_died_is_not_reported(self):
        """NEGATIVE. The trace has to be rare enough to be believed. A window
        that reported every abort would train the reader to skip the one line
        that matters."""
        window = self.window()
        worker = threading.Thread(target=lambda: None, daemon=True)
        worker.start()
        worker.join()
        window.worker = worker
        window.running = True
        window._stop()
        window.aborted_at = time.monotonic() - crow_gui.ABORT_GRACE_S - 0.1
        window._drain()
        window.update()
        self.assertNotIn("der Leser lebt",
                         window.transcript.widget.get("1.0", "end-1c"))
        self.assertIsNone(window.worker)


# ------------------------------------------------------------------- F6 -----

@needs_tk
class CodeBlockTests(GuiCase):
    """F6: code blocks rendered as such, with a copy button that copies.

    WHAT THE POSITIVE CASE DECIDES, because the plan leaves it to the case and
    not to the build: the file header is NOT copied. What goes on the clipboard
    is what would have to compile.
    """

    BLOCK = "def _lines(self, resp):\n    while not self.abort.is_set():\n        pass"

    def test_the_copy_button_puts_the_raw_block_on_the_clipboard(self):
        """POSITIVE. Press the button, read the real clipboard back, compare it
        against the raw text of the block, character for character."""
        window = self.window()
        self.run_turn_into(window, [{"content": "look:\n```python\n"},
                                    {"content": self.BLOCK + "\n"},
                                    {"content": "```\ndone\n"}])
        self.assertEqual(len(window.transcript.code_blocks), 1)
        self.assertTrue(window.transcript.copy_block(0))
        window.update()
        self.assertEqual(window.clipboard_get(), self.BLOCK)
        self.assertNotIn("python", window.clipboard_get(),
                         "the header rode along -- the clipboard is not the code")
        self.assertEqual(window.transcript.code_blocks[0][1], "python")

    def test_the_prose_around_a_block_is_not_in_it(self):
        """The other side of the same frame: what is outside stays outside."""
        window = self.window()
        self.run_turn_into(window, [{"content": "look:\n```python\n"},
                                    {"content": self.BLOCK + "\n"},
                                    {"content": "```\ndone\n"}])
        self.assertEqual(window.transcript.visible_answer(), "look:\ndone\n")

    def test_a_block_that_is_never_closed_is_still_framed_and_copyable(self):
        """NEGATIVE one. The answer stops mid-code -- cut off at the budget, or
        interrupted. That is exactly when the reader wants what there is, so the
        frame is drawn, the note says it was not closed, and the button works."""
        window = self.window()
        self.run_turn_into(window, [{"content": "```python\n"},
                                    {"content": "print(1)\n"}])
        self.assertEqual(len(window.transcript.code_blocks), 1)
        self.assertTrue(window.transcript.copy_block(0))
        window.update()
        self.assertEqual(window.clipboard_get(), "print(1)")
        self.assertIn("nicht geschlossen",
                      window.transcript.widget.get("1.0", "end-1c"))

    def test_prose_with_three_backticks_opens_no_block(self):
        """NEGATIVE two. "call it with ```json" is a sentence. A fence is a WHOLE
        line and what follows the backticks is a bare tag or nothing -- otherwise
        the window frames a paragraph as code and the answer disappears into
        it."""
        window = self.window()
        self.run_turn_into(window, [{"content": "call it with ```json here\n"},
                                    {"content": "``` and then stop\n"}])
        self.assertEqual(window.transcript.code_blocks, [])
        self.assertEqual(window.transcript.visible_answer(),
                         "call it with ```json here\n``` and then stop\n")

    def test_an_empty_block_does_not_report_an_empty_clipboard_as_success(self):
        """NEGATIVE three. A button that says "kopiert" over an empty clipboard
        has told the reader the opposite of what happened, and they find out by
        pasting nothing somewhere else."""
        window = self.window()
        window.clipboard_clear()
        window.clipboard_append("SOMETHING ELSE")
        self.run_turn_into(window, [{"content": "```python\n```\n"}])
        self.assertEqual(window.transcript.code_blocks, [("", "python")])
        self.assertFalse(window.transcript.copy_block(0))
        window.update()
        self.assertEqual(window.clipboard_get(), "SOMETHING ELSE",
                         "an empty block wiped the clipboard and called it a copy")


# ------------------------------------------------------------------- F7 -----

class _RoundTimeCost(crow_core.TurnCost):
    """The GUI sink #90's redundancy line 8 warns about: tok/s over the ROUND.

    Caught live on 2026-08-11: 252 tokens in 169 s of round time reads as 1.49
    tok/s while the server had just measured 14.77, because 150 of those seconds
    were prefill. It exists here to be driven, so that "the cost line is the
    core's" is a comparison and not a claim.
    """

    def line(self) -> str:
        bits = ["%d round%s" % (self.rounds, "" if self.rounds == 1 else "s")]
        if self.decoded:
            rate = self.decoded / self.model_s if self.model_s > 0 else None
            bits.append("%s tok%s" % ("{:,}".format(self.decoded),
                                      " @ %.2f tok/s" % rate if rate else ""))
        return " | ".join(bits)


class _SummedCacheCost(crow_core.TurnCost):
    """The other wrong sink: `cached` added up over the rounds.

    `cached` is a statement about the prefix AS IT STANDS, so a sum of it is a
    number about nothing. Over one round it agrees with the right answer, which
    is why the fixture below runs two.
    """

    def add_round(self, timings: dict) -> None:
        previous = self.cached or 0
        previous_of = self.cached_of or 0
        super().add_round(timings)
        if self.cached is not None:
            self.cached += previous
            self.cached_of += previous_of


@needs_tk
class CostLineTests(GuiCase):
    """F7: "tok/s and tokens per turn, from the response's `timings` field".

    "IT COMES FROM THE CORE" IS A STATEMENT ABOUT OWNERSHIP, NOT A CHECK. So the
    line the window draws is compared with `diff` against the line the terminal
    draws, over the same recorded stream -- the same instrument E4 uses on the
    two clients' answers.
    """

    def _cost_files(self, turn) -> tuple[str, str]:
        """What each surface puts on its screen for this turn, as two files."""
        window = self.window()
        window.transcript.cost(turn.cost.line())
        window.update()
        # THE COST LINE AND NOTHING ELSE, taken by its tag. The transcript also
        # carries the window's own start -- /health could not be reached in a
        # fixture run -- and comparing a whole transcript against one line would
        # be comparing two different things and calling the difference a defect.
        ranges = window.transcript.widget.tag_ranges("cost")
        gui_line = window.transcript.widget.get(ranges[0], ranges[1]).strip()
        # The terminal's, from cli/crow.py's repl: the dim escapes are empty
        # under a redirected stdout, so what is left is what the reader sees.
        cli_line = ("%s[%s]%s" % (crow.DIM, turn.cost.line(), crow.RESET)).strip()
        gui_path = os.path.join(self.dir, "cost-gui.txt")
        cli_path = os.path.join(self.dir, "cost-cli.txt")
        for path, text in ((gui_path, gui_line), (cli_path, cli_line)):
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        return gui_path, cli_path

    def _diff(self, left: str, right: str) -> str:
        with io.open(left, encoding="utf-8") as fh:
            a = fh.readlines()
        with io.open(right, encoding="utf-8") as fh:
            b = fh.readlines()
        return "".join(difflib.unified_diff(a, b, os.path.basename(left),
                                            os.path.basename(right)))

    def _turn(self, every: bool):
        self.freeze_clock()
        window = self.window()
        return self.run_turn_into(window, RECORDED, TIMINGS, every=every)

    def test_the_window_and_the_terminal_write_the_same_cost_line(self):
        """POSITIVE, checked with diff and not with eyes."""
        turn = self._turn(every=False)
        gui, cli = self._cost_files(turn)
        self.assertEqual(self._diff(gui, cli), "",
                         "the two surfaces assembled the same six numbers into "
                         "two different sentences")
        self.assertIn("14.77 tok/s", turn.cost.line())

    def test_timings_on_every_chunk_and_on_the_last_one_give_the_same_line(self):
        """The open `timings` question, answered by running both shapes. A window
        that accumulated per chunk instead of taking the last one would differ
        here and nowhere else."""
        last = self._turn(every=False).cost.line()
        every = self._turn(every=True).cost.line()
        self.assertEqual(last, every)

    def test_a_sink_that_prices_decode_by_round_time_goes_red(self):
        """NEGATIVE one, redundancy line 8: 1.49 tok/s printed for a turn the
        server measured at 14.77."""
        self.freeze_clock()
        window = self.window()
        original = crow_core.TurnCost
        crow_core.TurnCost = _RoundTimeCost
        try:
            wrong = self.run_turn_into(window, RECORDED, TIMINGS)
        finally:
            crow_core.TurnCost = original
        right = self._turn(every=False)
        self.assertNotEqual(wrong.cost.line(), right.cost.line())

    def test_a_sink_that_sums_cached_goes_red(self):
        """NEGATIVE two. `cached` is the LAST round's figure, not a total: two
        rounds of 10,979/11,507 must not read as 21,958/23,014."""
        self.freeze_clock()
        wrong = _SummedCacheCost()
        honest = crow_core.TurnCost()
        # TWO ROUNDS, because over one round the wrong sink agrees with the right
        # one and the case would pass for a version that has the defect.
        for _ in range(2):
            wrong.add_round(dict(TIMINGS, _cached_tokens=10979))
            honest.add_round(dict(TIMINGS, _cached_tokens=10979))
        self.assertNotEqual(wrong.line(), honest.line())
        self.assertIn("cached 10,979/11,507", honest.line())
        self.assertIn("cached 21,958/23,014", wrong.line())


# --------------------------------------------------- across the boundary ----

class _ReasoningLeaksIntoTheAnswer(crow_gui.WindowReplyEvents):
    """A GUI sink that reports the thoughts as answer text.

    The single most likely mistake in a window that wants to show reasoning: one
    sink, one stream of text, the thoughts merged in. It is here to be driven
    through the same comparison as the real one, because "the two clients answer
    the same question the same way" has to be able to fail.
    """

    def reasoning_text(self, piece: str) -> None:
        self._sink.put(("answer", piece))


@needs_tk
class AcrossTheClientBoundaryTests(GuiCase):
    """E4's diff runs CLI against CLI. This runs CLI against GUI.

    The same recorded stream through BOTH callback sinks, the visible answer text
    of each written to a file, and `diff`. That is the sharper form of "both
    clients answer the same question the same way", and it costs nothing.
    """

    def _cli_text(self, deltas: list[dict]) -> str:
        sink = io.StringIO()
        self.serve(chunks_for(deltas))
        text, _reasoning, _timings = crow_core.stream_reply(
            crow_core.Conversation("SYS"), base_url="http://x/v1", model="crow",
            api_key="k", temperature=0.0, timeout=1.0,
            events=crow.TerminalEvents(out=sink, prefix=""))
        del text
        return sink.getvalue()

    def _gui_text(self, deltas: list[dict], events_class=None) -> str:
        window = self.window()
        sink = (events_class or crow_gui.WindowReplyEvents)(window.events)
        self.serve(chunks_for(deltas))
        crow_core.stream_reply(
            crow_core.Conversation("SYS"), base_url="http://x/v1", model="crow",
            api_key="k", temperature=0.0, timeout=1.0, events=sink)
        while window._drain():
            window.update()
        window.update()
        return window.transcript.visible_answer()

    def _files(self, cli: str, gui: str) -> tuple[str, str]:
        cli_path = os.path.join(self.dir, "answer-cli.txt")
        gui_path = os.path.join(self.dir, "answer-gui.txt")
        for path, text in ((cli_path, cli), (gui_path, gui)):
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        return cli_path, gui_path

    def _diff(self, cli_path: str, gui_path: str) -> str:
        with io.open(cli_path, encoding="utf-8") as fh:
            a = fh.readlines()
        with io.open(gui_path, encoding="utf-8") as fh:
            b = fh.readlines()
        return "".join(difflib.unified_diff(a, b, "cli", "gui"))

    def test_both_sinks_show_the_same_answer_for_the_same_stream(self):
        """POSITIVE. The terminal's characters and the window's visible answer,
        over E4's recorded stream, as two files."""
        cli_path, gui_path = self._files(self._cli_text(RECORDED),
                                         self._gui_text(RECORDED))
        self.assertEqual(self._diff(cli_path, gui_path), "")

    def test_neither_sink_shows_the_reasoning(self):
        """The reasoning is 60-90 % of what this model produces. Merged into the
        answer it is indistinguishable from it -- in either client."""
        cli_path, gui_path = self._files(self._cli_text(RECORDED),
                                         self._gui_text(RECORDED))
        for path in (cli_path, gui_path):
            with io.open(path, encoding="utf-8") as fh:
                self.assertNotIn("the socket is the question here", fh.read())

    def test_a_gui_sink_that_writes_reasoning_into_the_answer_fails_the_diff(self):
        """NEGATIVE, and the case the positive one is worthless without."""
        cli_path, gui_path = self._files(
            self._cli_text(RECORDED),
            self._gui_text(RECORDED, _ReasoningLeaksIntoTheAnswer))
        self.assertNotEqual(self._diff(cli_path, gui_path), "",
                            "a sink that shows the thoughts as the answer passed "
                            "the comparison -- then the comparison checks nothing")


# --------------------------------------------------------------------- P2 ---

@needs_tk
class BatchingTests(GuiCase):
    """P2: "one event per token ... the fix is batching per tick, not rendering
    per event."

    THE NUMBER COMES FROM A MEASUREMENT, not from taste: tools/measure_gui_stream.py
    drew 2,048 events inside a 16 ms half-frame on this machine, and 4,000
    single-event ticks cost 132 s of catching up at TICK_MS against one frame
    batched. Both facts have a line here that can go red.
    """

    def test_one_tick_writes_a_batch_with_one_insert(self):
        """POSITIVE. Sixty deltas in the queue when the tick arrives leave ONE
        state behind, not sixty: they were joined before they were drawn."""
        window = self.window()
        sink = crow_gui.WindowReplyEvents(window.events)
        sink.reply_started()
        for i in range(60):
            sink.answer_text("tok%02d " % i)
        drawn = window._drain()
        window.update()
        self.assertEqual(drawn, 61, "the tick did not take the whole queue")
        self.assertEqual(len(window.transcript.states), 1,
                         "sixty deltas produced %d states -- that is one render "
                         "per event" % len(window.transcript.states))
        self.assertEqual(window.transcript.visible_answer().count("tok"), 60)

    def test_a_tick_never_takes_more_than_the_measured_cap(self):
        """NEGATIVE. The cap is what stops one tick from freezing the window for
        an unbounded time. A drain that emptied any queue whatever its size would
        turn a burst into a hang, which is P2 with the sign flipped."""
        window = self.window()
        for i in range(crow_gui.DRAIN_PER_TICK + 40):
            window.events.put(("answer", "t%d " % i))
        self.assertEqual(window._drain(), crow_gui.DRAIN_PER_TICK)
        self.assertEqual(window._drain(), 40)

    def test_the_cap_is_inside_what_the_measurement_allows(self):
        """The number itself, held against the tool that measured it."""
        self.assertLessEqual(crow_gui.DRAIN_PER_TICK, 2048)
        self.assertGreater(crow_gui.DRAIN_PER_TICK, 0)
        self.assertGreater(crow_gui.TICK_MS, 0)

    def test_an_event_between_two_deltas_keeps_the_order(self):
        """The batching may not reorder. A thought that opened between two
        answer deltas has to be drawn between them, or the transcript tells a
        different story than the stream did."""
        window = self.window()
        sink = crow_gui.WindowReplyEvents(window.events)
        sink.reply_started()
        sink.answer_text("A")
        sink.reasoning_started(1)
        sink.reasoning_text("hmm")
        sink.reasoning_finished()
        sink.answer_text("B")
        window._drain()
        window.update()
        whole = window.transcript.widget.get("1.0", "end-1c")
        self.assertLess(whole.index("A"), whole.index("hmm"))
        self.assertLess(whole.index("hmm"), whole.index("B"))


# ---------------------------------------------------- the session, twice ----

@needs_tk
class SessionRoundTripTests(GuiCase):
    """ONE SESSION, TWO DOORS. #90's E12 point 4, with a file and a name.

    It stood in the first draft of the plan as prose -- as an intention, which is
    what the epic rules out ("that is a test, not an intention"). So it is a
    class, it is named in the plan, and it is here.

    WHAT IS HERE AND WHAT IS IN E14. The two LIVE directions need a running
    server and are E14's: `python cli/crow.py` exits at :2365 with return 2
    before it ever prints `resumed: N messages` at :2406 when no endpoint
    answers, so the forward proof cannot be run for nothing. What can be settled
    without a server is the part that actually decides it: whether the two doors
    write and read ONE file in ONE format. A window with a format of its own
    passes every live forward test and fails right here.
    """

    def _session(self) -> str:
        return crow_core.SESSION_FILE

    def test_two_turns_in_the_window_are_readable_by_the_core_the_cli_uses(self):
        """FORWARD. The window runs two turns and saves; `load_session` -- the
        same function cli/crow.py calls at start -- reads the same messages back,
        and the count is the number the CLI would print as `resumed: N`."""
        window = self.window("--base-url", "http://127.0.0.1:1/v1")
        window.args.session = True
        for _ in range(2):
            self.run_turn_into(window, [{"content": "ANSWER\n"}])
        note = window._save()
        self.assertIsNotNone(note, "nothing was written")
        self.assertTrue(os.path.exists(self._session()))

        restored = crow_core.load_session("http://127.0.0.1:1/v1", window.args.system)
        self.assertIsNotNone(restored, "the CLI's reader sees no session")
        messages, _tokens, _kv = restored
        self.assertEqual(len(messages), len(window.conversation))
        self.assertEqual([m["role"] for m in messages],
                         [m["role"] for m in window.conversation.payload()])

    def test_a_session_written_by_the_cli_path_is_shown_in_the_window(self):
        """BACKWARD, and it is the direction a window with its own format fails.

        The file is written by `crow_core.save_session` against a conversation
        built the way cli/crow.py builds one; then a fresh window resumes it and
        has to SHOW it, not merely hold it.
        """
        conversation = crow_core.Conversation(crow_core.DEFAULT_SYSTEM)
        conversation.append("user", "was macht der Prefix-Cache")
        conversation.append("assistant", "Er haelt, solange das Praefix gleich bleibt.")
        crow_core.save_session(conversation, "http://127.0.0.1:1/v1", 4711)
        self.assertTrue(os.path.exists(self._session()))

        window = self.window()
        window.args.session = True
        window._probe()
        for _ in range(4):
            window._drain()
        window.update()
        self.assertEqual(len(window.conversation), len(conversation))
        self.assertEqual(window.context_tokens, 4711)
        drawn = window.transcript.widget.get("1.0", "end-1c")
        self.assertIn("was macht der Prefix-Cache", drawn)
        self.assertIn("Er haelt, solange das Praefix gleich bleibt.", drawn)
        self.assertIn("wiederhergestellt", drawn)

    def test_a_second_start_shows_the_same_history(self):
        """IDEMPOTENCE. Two starts against one file draw the same transcript --
        the promise the plan states as "the second start shows the same history
        from the same session.json"."""
        conversation = crow_core.Conversation(crow_core.DEFAULT_SYSTEM)
        conversation.append("user", "eins")
        conversation.append("assistant", "zwei")
        crow_core.save_session(conversation, "http://127.0.0.1:1/v1", 12)

        drawn = []
        for _ in range(2):
            # ONE WINDOW AT A TIME, because that is what "a second start" is --
            # and because two live Tk roots in one process is a different
            # experiment than the one this case is about.
            window = self.window()
            window.args.session = True
            window._probe()
            for _ in range(4):
                window._drain()
            window.update()
            drawn.append(window.transcript.widget.get("1.0", "end-1c"))
            self.close(window)
        self.assertEqual(drawn[0], drawn[1])

    def test_the_window_stamps_the_file_with_the_version_the_cli_owns(self):
        """The same file from both doors means the same header from both doors.
        A window that never handed the core a version would write
        `"version": ""` and the two sessions would differ in the one field a
        human reads first."""
        conversation = crow_core.Conversation(None)
        conversation.append("user", "x")
        crow_core.save_session(conversation, "http://127.0.0.1:1/v1", 1)
        with io.open(self._session(), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["version"], crow.VERSION)
        self.assertEqual(saved[crow_core.SESSION_FORMAT_KEY],
                         crow_core.SESSION_FORMAT)

    def test_a_session_this_build_cannot_read_is_refused_and_left_alone(self):
        """The gate from E8, through the window's door. A refusal that still
        overwrote the file would be the data loss the gate exists against."""
        stranger = {crow_core.SESSION_FORMAT_KEY: "99", "messages": [{"role": "user",
                                                                     "content": "hi"}]}
        with io.open(self._session(), "w", encoding="utf-8") as fh:
            json.dump(stranger, fh)
        before = Path(self._session()).read_bytes()

        window = self.window()
        window.args.session = True
        window._probe()
        for _ in range(4):
            window._drain()
        window.update()
        self.assertIn("session format", window.transcript.widget.get("1.0", "end-1c"))
        self.assertEqual(Path(self._session()).read_bytes(), before)
        self.assertEqual(len(window.conversation), 1)


# ------------------------------------------------------- the file itself ----

class TheWindowBorrowsAndDoesNotRebuildTests(unittest.TestCase):
    """The rules E12 states about the FILE, held against the file.

    tools/check_shared_core.py holds the same lines against the manifest and is
    the tool that has to stay green. These are the two it cannot express: the
    error idiom, and the type idiom the existing 88 functions carry.
    """

    def setUp(self) -> None:
        with io.open(HERE / "crow_gui.py", encoding="utf-8") as fh:
            self.source = fh.read()
        # COMMENTS OUT, same rule tools/check_operating_point.py's `code_only`
        # states: a sentence explaining why a value may not be written here is
        # not a place that writes it. Its cheap half is enough for this file --
        # a line whose first non-space character is # is a comment in every
        # Python that ever parsed.
        self.code = "\n".join(line for line in self.source.splitlines()
                              if not line.lstrip().startswith("#"))

    def test_no_tk_callback_ends_the_process(self):
        """`main` in cli/crow.py catches CrowError and returns 2. A `sys.exit`
        inside a Tk callback is the wrong translation of that: it takes the
        window, the unsaved session and the running turn with it."""
        body = self.source.split('if __name__ == "__main__":')[0]
        self.assertNotIn("sys.exit(", body,
                         "a callback in this file can end the process")

    def test_every_function_declares_what_it_returns(self):
        """The type idiom: 85 of 88 functions in the existing client carry a
        return type and 80 are annotated throughout. A new file is where that
        quietly stops being true."""
        import ast

        tree = ast.parse(self.source)
        missing = [node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.returns is None
                   and not node.name.startswith("<")]
        self.assertEqual(missing, [], "functions with no return type: %s" % missing)

    def test_the_three_resources_are_released_in_one_place(self):
        """The window adds a reader thread, a socket and an `after()` loop --
        three things the existing client does not have. All three come back in
        `_release`, which the close path and `main`'s `finally` both run."""
        self.assertIn("def _release(self) -> None:", self.source)
        self.assertIn("after_cancel", self.source)
        self.assertIn("INTERRUPT.set()", self.source)
        self.assertIn("window._release()", self.source)

    def test_the_brand_values_are_not_written_here(self):
        """`#0b0e17` and the accent come out of the core. Written here they would
        be a second copy to correct, which is what the manifest counts."""
        self.assertNotIn("#0b0e17", self.code)
        self.assertNotIn("#7eb0f8", self.code)
        self.assertIn("CROW_BG", self.code)
        self.assertIn("CROW_ACCENT_HEX", self.code)

    def test_the_version_literal_does_not_appear_here(self):
        """install.ps1:399-403 greps cli/crow.py for `^VERSION = "..."`. A second
        file carrying one is a second thing to bump, and the stale one is the one
        no release step reads."""
        self.assertIsNone(re.search(r'^VERSION\s*=\s*"', self.source, re.M))
        self.assertEqual(crow_gui.client_version(), crow.VERSION)

    def test_a_missing_client_file_leaves_the_version_empty(self):
        """The empty default is load-bearing: a window that could not read the
        version must stay quiet rather than stamp a session with a guess."""
        self.assertEqual(crow_gui.client_version(os.path.join(self.dir_of_nothing(),
                                                              "crow.py")), "")

    def dir_of_nothing(self) -> str:
        return tempfile.mkdtemp(prefix="crow-empty-")


# ------------------------------------------------------------ the fences ----

class _FenceLog(crow_core.FenceEvents):
    """What `CodeFences` reported, in order."""

    def __init__(self) -> None:
        self.log: list[tuple] = []

    def prose(self, piece: str) -> None:
        self.log.append(("prose", piece))

    def code_started(self, language: str) -> None:
        self.log.append(("open", language))

    def code_text(self, line: str) -> None:
        self.log.append(("code", line))

    def code_finished(self, closed: bool) -> None:
        self.log.append(("close", closed))

    @property
    def prose_text(self) -> str:
        return "".join(entry[1] for entry in self.log if entry[0] == "prose")


class FenceStateMachineTests(unittest.TestCase):
    """The core half of F6, driven a character at a time.

    It sits in cli/crow_core.py and not here because both surfaces need it, and
    it is driven from THIS file because the window is the only surface wired to
    it today -- manifests/shared-core.json says so with a reason.
    """

    def _run(self, pieces: list[str]) -> tuple[crow_core.CodeFences, _FenceLog]:
        log = _FenceLog()
        fences = crow_core.CodeFences(log)
        for piece in pieces:
            fences.feed(piece)
        fences.finish()
        return fences, log

    def test_a_fence_split_across_deltas_is_still_one_fence(self):
        """The shape a stream actually arrives in: the three backticks and the
        language can land in three different chunks."""
        fences, log = self._run(["before\n", "``", "`py", "thon\n", "x = 1\n",
                                 "```\n", "after\n"])
        self.assertEqual(fences.blocks, ["x = 1"])
        self.assertEqual(fences.languages, ["python"])
        self.assertEqual(log.prose_text, "before\nafter\n")

    def test_prose_flows_before_the_line_is_over(self):
        """STREAMING. Only a line that could still become a fence is held, and
        that is at most three characters at the start of a line."""
        log = _FenceLog()
        fences = crow_core.CodeFences(log)
        fences.feed("hello")
        self.assertEqual(log.prose_text, "hello")
        del fences

    def test_an_unclosed_block_reports_that_it_was_not_closed(self):
        fences, log = self._run(["```py\n", "x = 1\n"])
        self.assertEqual(fences.blocks, ["x = 1"])
        self.assertIn(("close", False), log.log)

    def test_a_closed_block_reports_that_it_was(self):
        _fences, log = self._run(["```py\n", "x = 1\n", "```\n"])
        self.assertIn(("close", True), log.log)

    def test_a_sentence_with_backticks_is_not_a_fence(self):
        fences, log = self._run(["call it with ```json here\n"])
        self.assertEqual(fences.blocks, [])
        self.assertEqual(log.prose_text, "call it with ```json here\n")

    def test_backticks_followed_by_words_are_not_a_fence(self):
        """"``` and then" is prose. An opening fence carries a bare tag or
        nothing; everything else on the line means it was a sentence."""
        fences, _log = self._run(["``` and then stop\n"])
        self.assertEqual(fences.blocks, [])

    def test_an_empty_block_is_an_empty_string_and_not_a_missing_one(self):
        """The window needs the difference: no block at all draws no frame, an
        EMPTY block draws a frame whose button must refuse."""
        fences, _log = self._run(["```py\n", "```\n"])
        self.assertEqual(fences.blocks, [""])


if __name__ == "__main__":
    unittest.main(verbosity=2)
