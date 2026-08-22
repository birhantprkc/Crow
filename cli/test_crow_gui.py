#!/usr/bin/env python3
"""Suite for the window: cli/crow_gui.py against cli/crow_core.py.

Run:  python cli/test_crow_gui.py

WHAT CHANGED UNDER THIS FILE, AND WHAT DID NOT. The first version of this suite
read Tk widget state -- `displaychars`, the widget's own `dump`, the real
clipboard -- because the window was a `tk.Tk` and the only honest question was
"what is on the screen". E12 replaced that window with a pywebview page, so
every one of those predicates now asks a question about an object that no longer
exists. The BEHAVIOUR they were cut against did not change, and neither did the
cases: an abort against a blocked read, batching instead of one render per
event, reasoning that re-enters mid-answer, one session file through two doors.

SO THE SEAM MOVED FROM THE WIDGET TO THE MESSAGE. Everything the window draws
arrives as one JSON message pushed onto `Api._out` -- there is no second path to
the page and no shadow copy beside it, which is what made the widget rule worth
having in the first place. Reading that queue is reading what the page was told,
and `test_every_message_the_window_pushes_has_a_case_on_the_page` closes the
other half: a message the page has no `case` for is drawn by nobody. That hole
was real, and it had swallowed the reasoning share of every turn.

NO SERVER, NO MODEL, NO NETWORK, and no window either: `Api` is driven directly,
which is what the page does through `js_api`. The streams are recorded chunks
fed through the REAL `crow_core.stream_reply` and the REAL `crow_core.run_turn`
by rebinding `_post_stream`, the one door there is. The two proofs that need a
running server -- the abort's next question answered in under 30 s, and the two
live directions of the session round trip -- are E14's by the plan's own
arrangement.

NO TK, SO NOTHING SKIPS. The old suite skipped on a headless box, which was
honest and also meant the cases never ran anywhere they were not being watched.
"""

from __future__ import annotations

import difflib
import io
import inspect
import json
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from unittest import mock
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import crow            # noqa: E402
import crow_core       # noqa: E402
import crow_gui        # noqa: E402

# THE SUITE MAY NOT DEPEND ON WHAT THIS MACHINE HAS CONFIGURED (#130).
# `crow_core` reads %LOCALAPPDATA%\Crow\mcp.json at import and appends whatever
# it finds to TOOLS, TOOL_IMPL and TOOL_CLASS. Every case that enumerates the
# tool table therefore answered differently on a machine with an MCP server than
# on one without -- found on 2026-08-22, when `ReleaseLevelTests` went red
# against a real MCP install and nothing in this file had changed.
#
# A PATH WHOSE PARENT DOES NOT EXIST, not a temp file that might: the reader
# treats "no file" as the empty configuration, and that is the state the twelve
# built-in tools are the whole table in. Cases that WANT a configuration rebind
# `MCP_FILE` themselves and put it back.
crow_core.MCP_FILE = os.path.join(tempfile.gettempdir(),
                                  "crow-suite-has-no-mcp", "mcp.json")
crow_core.mcp_apply()
import crow_voice      # noqa: E402


# ---------------------------------------------------------------- fixtures --

# THE STREAM E4's SEAM WAS CUT AGAINST, and the one both surfaces are held to
# below: reasoning, then an answer, in the shape the server sends them.
RECORDED = [
    {"reasoning_content": "the socket is the question here"},
    {"content": "The read sits in _post_stream.\n"},
    {"content": "A close from outside reaches the buffer.\n"},
]

# P3 IN FIVE DELTAS: think, answer, THINK AGAIN, answer again, inside one turn.
# The same fixture cli/test_crow_core.py cuts E10's state machine against,
# repeated here on purpose -- the window has to survive the identical shape, and
# a test file that invented its own would be measuring a different stream.
RE_ENTRY = [
    {"reasoning_content": "first I "},
    {"reasoning_content": "consider it"},
    {"content": "ANSWER ONE\n"},
    {"reasoning_content": "wait -- I should check"},
    {"content": "ANSWER TWO\n"},
]

TIMINGS = {"predicted_n": 252, "predicted_ms": 17060.0, "predicted_per_second": 14.77,
           "prompt_n": 528, "prompt_ms": 7066.0, "prompt_per_second": 74.72}
USAGE = {"total_tokens": 11507, "prompt_tokens_details": {"cached_tokens": 10979}}


class _StoppedClock:
    """`time` with a monotonic that does not move.

    The live counter throttles on elapsed wall clock. A case about the throttle
    that let the clock run would be measuring the machine's mood; everything
    else on the module is passed straight through.
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


class ApiCase(unittest.TestCase):
    """An `Api` with its session directory in a temp dir, and nothing global left
    behind.

    BOTH `SESSION_FILE`s ARE REBOUND, and that is not belt and braces: the window
    imported the name at module load, so `crow_gui.SESSION_FILE` is a second
    binding to the same string. Patching only the core's would leave the rail
    reading one directory while the writes went to another -- and the suite would
    be green about a window nobody could have used.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="crow-gui-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.session = os.path.join(self.dir, "session.json")
        self._before = (crow_core._post_stream, crow_core.SESSION_FILE,
                        crow_core.SESSION_DIR, crow_gui.SESSION_FILE,
                        crow_gui.time)
        self.addCleanup(self._restore)
        crow_core.SESSION_DIR = self.dir
        crow_core.SESSION_FILE = self.session
        crow_gui.SESSION_FILE = self.session
        crow_core.INTERRUPT.clear()

    def _restore(self) -> None:
        (crow_core._post_stream, crow_core.SESSION_FILE, crow_core.SESSION_DIR,
         crow_gui.SESSION_FILE, crow_gui.time) = self._before
        crow_core.INTERRUPT.clear()

    def api(self, *argv: str, klass=None, session: bool = True):
        """The object the page talks to. No window, because the page is not here.

        `Api.push` only ever puts a dict on a queue; `pump` is the one method
        that touches the window, and it is a thread `main` starts. Driving the
        Api directly is therefore the same code path the page drives, minus the
        transport.
        """
        args = crow_gui.build_parser().parse_args(
            ["--base-url", "http://127.0.0.1:1/v1", *argv])
        args.session = session
        return (klass or crow_gui.Api)(args)

    def drained(self, api) -> list[dict]:
        """Everything the page would have been told, in order."""
        out = []
        while True:
            try:
                out.append(api._out.get_nowait())
            except Exception:
                return out

    def kinds(self, api) -> list[str]:
        return [m.get("k") for m in self.drained(api)]

    def answer_text(self, messages: list[dict]) -> str:
        return "".join(m["t"] for m in messages if m.get("k") == "text")

    def serve(self, payloads: list[str]) -> None:
        """Script the endpoint. The REAL stream loop runs behind it."""
        def fake(url, body, api_key, timeout):
            for payload in payloads:
                yield payload
        crow_core._post_stream = fake

    def sink_events(self, deltas: list[dict], klass=None,
                    live: bool = True) -> list[dict]:
        """One recorded stream through the real core into a real `Sink`."""
        collected: list[dict] = []
        sink = (klass or crow_gui.Sink)(collected.append, live=live)
        self.serve(chunks_for(deltas))
        crow_core.stream_reply(
            crow_core.Conversation("SYS"), base_url="http://x/v1", model="crow",
            api_key="k", temperature=0.0, timeout=1.0, events=sink)
        return collected

    def a_chat(self, api, first: str = "the first thing said",
               reply: str = "an answer") -> None:
        """A conversation with something in it, without a server."""
        api._conversation.append("user", first)
        api._conversation.append("assistant", reply)

    def rail(self, api) -> dict:
        """The last rail message the page would have received."""
        rails = [m for m in self.drained(api) if m.get("k") == "rail"]
        self.assertTrue(rails, "the rail was never drawn")
        return rails[-1]


# ----------------------------------------------------------------- F1, P3 ----

class TheStreamReachesThePageTests(ApiCase):
    """What the core said, and what the page was told, are the same thing."""

    def test_a_recorded_stream_arrives_as_text_and_thought_messages(self):
        """POSITIVE. The answer arrives as `text`, the reasoning as `think`, and
        the two are never the same message."""
        events = self.sink_events(RECORDED)
        self.assertEqual(self.answer_text(events),
                         "The read sits in _post_stream.\n"
                         "A close from outside reaches the buffer.\n")
        thoughts = "".join(m["t"] for m in events if m.get("k") == "think")
        self.assertEqual(thoughts, "the socket is the question here")

    def test_the_thoughts_are_not_in_the_answer(self):
        """The reasoning is 53 % of a turn at the shipped operating point. Merged
        into the answer it is indistinguishable from it."""
        self.assertNotIn("the socket is the question here",
                         self.answer_text(self.sink_events(RECORDED)))

    def test_reasoning_that_re_enters_opens_a_second_block(self):
        """P3, POSITIVE. Think, answer, think again, answer again -- inside one
        turn. One block per re-entry, in the order the stream had them."""
        events = self.sink_events(RE_ENTRY)
        shape = [m["k"] for m in events
                 if m["k"] in ("think_open", "think_close", "text")]
        self.assertEqual(shape.count("think_open"), 2,
                         "a re-entering stream produced %d thought blocks"
                         % shape.count("think_open"))
        self.assertEqual(shape.count("think_open"), shape.count("think_close"),
                         "a thought block was opened and never closed")
        # THE RUNS ARE NOT ONE MESSAGE EACH, and that is the core's decision:
        # `CodeFences.feed` looks at the first characters of a line singly --
        # that is the span in which it could still become a fence -- and hands
        # the rest over in whole runs. So the shape is compared by its blocks
        # rather than by counting `text` messages.
        blocks = [k for i, k in enumerate(shape)
                  if k != "text" or i == 0 or shape[i - 1] != "text"]
        self.assertEqual(blocks, ["think_open", "think_close", "text",
                                  "think_open", "think_close", "text"])
        self.assertEqual(self.answer_text(events), "ANSWER ONE\nANSWER TWO\n")

    def test_a_stream_with_one_thought_does_not_fake_a_re_entry(self):
        """NEGATIVE for the case above. A predicate that counted blocks without
        the stream having them would pass on anything."""
        events = self.sink_events(RECORDED)
        self.assertEqual(len([m for m in events if m["k"] == "think_open"]), 1)


class _ReasoningLeaksIntoTheAnswer(crow_gui.Sink):
    """A sink that reports the thoughts as answer text.

    The single most likely mistake in a window that shows reasoning: one stream
    of text with the thoughts merged in. It is here to be driven through the
    same comparison as the real one, because "the two clients answer the same
    question the same way" has to be able to fail.
    """

    def reasoning_text(self, piece: str) -> None:
        self.answer_text(piece)


class AcrossTheClientBoundaryTests(ApiCase):
    """E4's diff runs CLI against CLI. This runs CLI against the window.

    The same recorded stream through BOTH sinks, the visible answer of each
    written to a file, and `diff`. That is the sharper form of "both clients
    answer the same question the same way", and it costs nothing.
    """

    def _cli_text(self, deltas: list[dict]) -> str:
        out = io.StringIO()
        self.serve(chunks_for(deltas))
        crow_core.stream_reply(
            crow_core.Conversation("SYS"), base_url="http://x/v1", model="crow",
            api_key="k", temperature=0.0, timeout=1.0,
            events=crow.TerminalEvents(out=out, prefix=""))
        return out.getvalue()

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

    def test_both_clients_show_the_same_answer_for_the_same_stream(self):
        """POSITIVE, as two files and a diff."""
        cli_path, gui_path = self._files(
            self._cli_text(RECORDED), self.answer_text(self.sink_events(RECORDED)))
        self.assertEqual(self._diff(cli_path, gui_path), "")

    def test_a_sink_that_writes_reasoning_into_the_answer_fails_the_diff(self):
        """NEGATIVE, and the case the positive one is worthless without."""
        cli_path, gui_path = self._files(
            self._cli_text(RECORDED),
            self.answer_text(self.sink_events(RECORDED,
                                              klass=_ReasoningLeaksIntoTheAnswer)))
        self.assertNotEqual(self._diff(cli_path, gui_path), "",
                            "a sink that shows the thoughts as the answer passed "
                            "the comparison -- then the comparison checks nothing")


# --------------------------------------------------------------------- P2 ---

class ThrottleTests(ApiCase):
    """P2: "one event per token ... the fix is batching per tick, not rendering
    per event."

    THE WEBVIEW'S ANSWER TO P2 IS NOT TK'S. There is no tick to batch into: every
    message crosses to the page on its own. What would flood the page is the LIVE
    COUNTER, which has something new to say on every single delta, and it is
    throttled to five updates a second. The text is not throttled, and must not
    be -- a dropped delta is a hole in the answer.
    """

    def test_a_burst_of_deltas_produces_at_most_one_counter_update(self):
        """POSITIVE. Sixty deltas inside one 200 ms window leave one `live`
        message, not sixty."""
        crow_gui.time = _StoppedClock()
        collected: list[dict] = []
        sink = crow_gui.Sink(collected.append)
        sink.reply_started()
        for i in range(60):
            sink.answer_text("tok%02d " % i)
        live = [m for m in collected if m["k"] == "live"]
        self.assertLessEqual(len(live), 1,
                             "sixty deltas produced %d counter updates -- that is "
                             "one render per event" % len(live))

    def test_the_throttle_drops_no_answer_text(self):
        """NEGATIVE for the throttle: it may thin the counter and nothing else.
        A version that throttled the text would pass the case above and lose the
        answer."""
        crow_gui.time = _StoppedClock()
        collected: list[dict] = []
        sink = crow_gui.Sink(collected.append)
        sink.reply_started()
        for i in range(60):
            sink.answer_text("tok%02d " % i)
        sink.reply_finished()
        text = self.answer_text(collected)
        self.assertEqual(text.count("tok"), 60,
                         "%d of 60 deltas reached the page" % text.count("tok"))

    def test_a_replayed_chat_counts_nothing(self):
        """A restored conversation is drawn through the same sink with the
        counter off: a chat loaded from a file has no rate, and a counter
        climbing over an answer written yesterday is a lie about what is
        happening now."""
        collected: list[dict] = []
        sink = crow_gui.Sink(collected.append, live=False)
        sink.reply_started()
        for i in range(30):
            sink.answer_text("x")
        self.assertEqual([m for m in collected if m["k"] == "live"], [])

    def test_the_counter_moves_when_the_clock_does(self):
        """The throttle has to let something through, or it is not a throttle but
        a mute. Two bursts a second apart, on a clock that moved between them."""
        clock = _StoppedClock()
        crow_gui.time = clock
        collected: list[dict] = []
        sink = crow_gui.Sink(collected.append)
        sink.reply_started()
        for _ in range(5):
            sink.answer_text("a")
        clock._at += 1.0
        for _ in range(5):
            sink.answer_text("b")
        self.assertGreaterEqual(len([m for m in collected if m["k"] == "live"]), 1)


# --------------------------------------------------------------- P1, F4 -----

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

    THE EFFECT IS NOT THE PROOF, and E12 says so: an interface reacting can be
    faked with a flag. The proof that matters is the next question being
    answered, and it needs a server -- it is in E14. What is here is the CAUSE:
    the second abort path exists and is bounded by the read timeout the window
    ships, over a real socket.
    """

    def setUp(self) -> None:
        crow_core.INTERRUPT.clear()
        self.addCleanup(crow_core.INTERRUPT.clear)

    def _abort_after(self, timeout: float) -> float:
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
        """POSITIVE, END TO END OVER A REAL SOCKET.

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

    def test_the_timeout_the_turn_runs_under_is_the_one_the_window_declares(self):
        """The value is only a bound if the turn actually runs under it. A
        constant that nothing passes to `run_turn` bounds a comment."""
        self.assertGreater(crow_gui.READ_TIMEOUT_S, 0)
        self.assertLess(crow_gui.READ_TIMEOUT_S, 1800.0)
        source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.assertIn("timeout=READ_TIMEOUT_S", source,
                      "the window declares a read timeout it does not run under")

    def test_an_interrupted_turn_is_written_on_the_page(self):
        """The other half of P1: when the abort lands, the window SAYS SO. A
        window that went quiet is indistinguishable from one where the abort did
        nothing."""
        collected: list[dict] = []
        crow_gui.Turn(collected.append).turn_interrupted()
        self.assertEqual([m["k"] for m in collected], ["fail"])
        self.assertEqual(collected[0]["t"], crow_core.ABORT_NOTE)


# ------------------------------------------------------ the reasoning share --

class TheCostLineCarriesTheShareTests(ApiCase):
    """The share of a turn that was thinking, from the core to the cost line.

    IT USED TO BE PUSHED AS `{"k": "_round"}` AND READ BY NOBODY. The page's
    switch has no case for it, so it fell through; the cost line was then drawn
    from a local variable that nothing ever wrote, and every turn reported a
    share of null. Two things had to be true at once for that to stay invisible,
    and both are cases now: the value has to arrive, and no message may exist
    that the page cannot draw.
    """

    def test_the_share_is_computed_from_what_the_core_counted(self):
        """POSITIVE. 60 characters of thought against 40 of answer is 60 %."""
        turn = crow_gui.Turn(lambda m: None)
        turn.round_finished({"_reasoning_chars": 60, "_content_chars": 40})
        self.assertAlmostEqual(turn.share, 60.0)

    def test_a_round_that_counted_nothing_leaves_the_share_unset(self):
        """NEGATIVE. A share of 0 % and no share at all are different claims, and
        the page draws them differently."""
        turn = crow_gui.Turn(lambda m: None)
        turn.round_finished({})
        self.assertIsNone(turn.share)

    def test_the_share_is_the_turn_and_not_the_last_round(self):
        """THE ONE THAT WAS RED BEFORE #117. Each round used to overwrite the value, and the page
        then stamped the survivor onto every thought block under a label reading "% of the turn".

        90 % of thinking followed by 10 % is a turn of 50 %, not a turn of 10 %. The two rounds
        carry the same number of characters on purpose: taking the LAST round gives 10, a mean
        over rounds gives 50 as well, and summing gives 50 -- so the case is cut to separate
        last-round from the other two, and the case below separates summing from averaging.
        """
        turn = crow_gui.Turn(lambda m: None)
        turn.round_finished({"_reasoning_chars": 90, "_content_chars": 10})
        turn.round_finished({"_reasoning_chars": 10, "_content_chars": 90})
        self.assertAlmostEqual(turn.share, 50.0)

    def test_a_long_round_outweighs_a_short_one(self):
        """The denominator is CHARACTERS, not rounds. 900 thought against 100 answer, then a tool
        round of 1 against 99, is 901 of 1100 -- 82 %. A mean over the two rounds would say 45 and
        let a round that produced almost nothing halve the figure of the one that did the work."""
        turn = crow_gui.Turn(lambda m: None)
        turn.round_finished({"_reasoning_chars": 900, "_content_chars": 100})
        turn.round_finished({"_reasoning_chars": 1, "_content_chars": 99})
        self.assertAlmostEqual(turn.share, 100.0 * 901 / 1100)

    def test_the_share_does_not_leave_as_a_message_of_its_own(self):
        """It is read off the turn at the end. A message with no case on the page
        is a value that looks delivered and is not."""
        collected: list[dict] = []
        crow_gui.Turn(collected.append).round_finished(
            {"_reasoning_chars": 1, "_content_chars": 1})
        self.assertEqual([m["k"] for m in collected], [])


# ----------------------------------------------------------- the rail --------

class _ArchivesIntoTheLiveSession(crow_gui.Api):
    """The window as it was: a chat with no file of its own is written into
    session.json when it is left.

    Nothing lists session.json in the rail, so the chat had left the window; the
    first turn of the chat being opened then wrote over it. This is the shape of
    "the previous chat is completely overwritten", kept runnable so the case
    against it can fail.
    """

    def _archive(self) -> str | None:
        if self._current_path:
            return super()._archive()
        try:
            crow_core.save_session(self._conversation, self._args.base_url,
                                   self._context_tokens,
                                   path=crow_gui.SESSION_FILE, with_kv=False)
        except Exception:
            return None
        return crow_gui.SESSION_FILE


class _FilesTheRestoredSessionEveryStart(crow_gui.Api):
    """The window as it was: start-up hands the restored session a file whether
    it has one already or not. Five launches, five identical entries."""

    def _probe(self) -> None:
        super()._probe()
        self._current_path = None
        self._current_path = self._archive()


class _NeverStampsTheName(crow_gui.Api):
    """The window as it was: `crow_title` is written into one file and nowhere
    else, so the next write through the core drops it."""

    def _stamp(self, path: str, pointer: bool = False) -> None:
        return None


class RailTests(ApiCase):
    """The three faults reported against the built window, each with the version
    that had them still runnable beside it.

    A CASE THAT ONLY THE FIX CAN PASS. Every positive here is followed by the
    same predicate against a subclass restoring the old behaviour: if the broken
    window passes too, the case is measuring nothing.
    """

    # -- symptom 3: switching away destroyed the chat being left ---------------

    def test_switching_chats_leaves_the_previous_one_in_the_rail(self):
        """POSITIVE. Two chats, a switch, and the one switched away from is
        still there -- with its own messages in it."""
        api = self.api()
        self.a_chat(api, "chat ONE speaking")
        ok, first = api._leave()
        self.assertTrue(ok)
        api._conversation.reset()
        api._current_path = None
        self.a_chat(api, "chat TWO speaking")
        self.drained(api)

        api.open(first)
        entry = self.rail(api)
        earlier = [r["title"] for r in entry["rollovers"]]
        self.assertIn("chat TWO speaking", earlier,
                      "the chat that was open when the switch happened is in no "
                      "list -- it was written where nothing reads")
        self.assertEqual(entry["title"], "chat ONE speaking")

    def test_the_chat_being_left_is_readable_afterwards(self):
        """The stronger half: listed is not the same as intact."""
        api = self.api()
        self.a_chat(api, "chat ONE speaking")
        ok, first = api._leave()
        self.assertTrue(ok)
        api._conversation.reset()
        api._current_path = None
        self.a_chat(api, "chat TWO speaking", "the second answer")
        self.drained(api)
        api.open(first)

        second = [r["path"] for r in self.rail(api)["rollovers"]
                  if r["title"] == "chat TWO speaking"]
        self.assertTrue(second, "no file to read back")
        restored = crow_core.load_session("http://127.0.0.1:1/v1", None, second[0])
        self.assertIsNotNone(restored, "the file the switch wrote is not a session")
        messages, _tokens, _kv = restored
        self.assertIn("the second answer", [m.get("content") for m in messages])

    def test_the_old_window_loses_the_chat_it_switched_away_from(self):
        """NEGATIVE. The same steps against the version that wrote it into
        session.json: the case above has to go red for it, or it proves nothing."""
        api = self.api(klass=_ArchivesIntoTheLiveSession)
        self.a_chat(api, "chat ONE speaking")
        ok, first = api._leave()
        self.assertTrue(ok)
        api._conversation.reset()
        api._current_path = None
        self.a_chat(api, "chat TWO speaking")
        self.drained(api)
        api.open(first)

        earlier = [r["title"] for r in self.rail(api)["rollovers"]]
        self.assertNotIn("chat TWO speaking", earlier,
                         "the old behaviour kept the chat -- then this suite is "
                         "not reproducing the fault it is cut against")

    # -- symptom 1: deleted chats came back on "new" --------------------------

    def test_a_deleted_chat_does_not_come_back_when_the_next_one_starts(self):
        """POSITIVE. Delete every chat under "Earlier", press "new", and the
        rail holds only what is actually still on disk."""
        api = self.api()
        self.a_chat(api, "the one to delete")
        ok, first = api._leave()
        self.assertTrue(ok)
        api._conversation.reset()
        api._current_path = None
        self.a_chat(api, "the one still open")
        self.drained(api)

        api.delete_chat(first)
        self.assertFalse(os.path.exists(first))
        api.reset()
        titles = [r["title"] for r in self.rail(api)["rollovers"]]
        self.assertNotIn("the one to delete", titles,
                         "a chat that was deleted is in the rail again")
        self.assertEqual(titles.count("the one still open"), 1,
                         "the chat put aside was written more than once")

    def test_deleting_the_open_chat_empties_the_window(self):
        """The open chat is deletable too, and "delete" has to mean it. Kept in
        memory, it was written straight back out by the next "new"."""
        api = self.api()
        self.a_chat(api, "the open one")
        ok, path = api._leave()
        self.assertTrue(ok)
        self.drained(api)

        api.delete_chat(path)
        self.assertIn("clear", [m["k"] for m in self.drained(api)] + ["clear"])
        api.reset()
        titles = [r["title"] for r in self.rail(api)["rollovers"]]
        self.assertNotIn("the open one", titles,
                         "the deleted chat was written back out by the next new")
        self.assertFalse(os.path.exists(self.session),
                         "session.json still holds the deleted conversation")

    def test_a_restored_session_is_not_copied_into_the_rail_on_every_start(self):
        """POSITIVE, and the root of the "deleted chats come back" report: what
        returned was not the old file but a fresh copy of the same conversation,
        written by the launch itself. Three starts, no entry under "Earlier"."""
        conversation = crow_core.Conversation(None)
        conversation.append("user", "the restored one")
        conversation.append("assistant", "an answer")
        crow_core.save_session(conversation, "http://127.0.0.1:1/v1", 12, with_kv=False)

        for _ in range(3):
            api = self.api()
            self._probe_without_a_server(api)
            entry = self.rail(api)
        self.assertEqual(entry["rollovers"], [],
                         "%d copies of the restored chat after three launches"
                         % len(entry["rollovers"]))
        self.assertEqual(entry["title"], "the restored one")

    def test_the_old_window_copied_the_restored_session_on_every_start(self):
        """NEGATIVE for the case above."""
        conversation = crow_core.Conversation(None)
        conversation.append("user", "the restored one")
        conversation.append("assistant", "an answer")
        crow_core.save_session(conversation, "http://127.0.0.1:1/v1", 12, with_kv=False)

        for _ in range(3):
            api = self.api(klass=_FilesTheRestoredSessionEveryStart)
            self._probe_without_a_server(api)
            entry = self.rail(api)
        self.assertGreater(len(entry["rollovers"]), 0,
                           "the old behaviour produced no copies -- then this "
                           "suite is not reproducing the fault")

    # -- symptom 2: a renamed chat lost its name when it was put aside --------

    def test_a_renamed_chat_keeps_its_name_when_it_is_put_aside(self):
        """POSITIVE. Name the open chat "Test IDE", press "new", and it is under
        "Earlier" as "Test IDE" -- not as its first line."""
        api = self.api()
        self.a_chat(api, "the first line of this chat")
        api.rename("", "Test IDE")
        self.drained(api)

        api.reset()
        titles = [r["title"] for r in self.rail(api)["rollovers"]]
        self.assertIn("Test IDE", titles,
                      "the renamed chat is listed as %s" % titles)
        self.assertNotIn("the first line of this chat", titles)

    def test_renaming_the_open_chat_does_not_file_it_away(self):
        """A rename is a label, not a decision to be finished with it. Naming the
        open chat used to archive it on the spot."""
        api = self.api()
        self.a_chat(api, "still working here")
        api.rename("", "a name")
        entry = self.rail(api)
        self.assertEqual(entry["title"], "a name")
        self.assertEqual(entry["rollovers"], [],
                         "renaming the open chat put it under Earlier")

    def test_the_name_survives_a_restart(self):
        """The name is written where the next launch reads it, or it is a label
        on this process only."""
        api = self.api()
        self.a_chat(api, "the first line of this chat")
        api.rename("", "Test IDE")
        api._persist_live()

        second = self.api()
        self._probe_without_a_server(second)
        self.assertEqual(self.rail(second)["title"], "Test IDE")

    # -- #100: a name given before the first turn ----------------------------

    def test_a_name_given_before_the_first_turn_survives_a_restart(self):
        """POSITIVE (#100). Naming a chat before typing into it is how people
        file things -- the name describes what the slot is FOR, not what is in
        it. Until now it lived only in memory: `save_session` refuses an empty
        conversation, so no file was written, so `_stamp` never ran.
        """
        api = self.api()
        api.rename("", "Einkaufsliste")            # no turn in this chat at all
        self.drained(api)

        second = self.api()
        self._probe_without_a_server(second)
        self.assertEqual(self.rail(second)["title"], "Einkaufsliste")

    def test_an_empty_chat_nobody_named_still_leaves_nothing(self):
        """THE NEGATIVE HALF, and the one that keeps the fix above from being a
        regression. `save_session`'s refusal is what stops an abandoned chat
        coming back on the next start; writing a file for EVERY empty chat would
        walk straight back into it. Only a name earns a file.
        """
        api = self.api()
        api._persist_live()
        self.assertFalse(os.path.isfile(self.session),
                         "an unnamed empty chat wrote a session file")

    def test_a_session_file_with_only_a_name_is_not_a_conversation(self):
        """THE SECOND NEGATIVE HALF. The file this fix creates carries a name and
        no messages. If the core read that as a chat, the window would come back
        holding an empty conversation it never had -- so this pins that the core
        answers "no session", and that it does not raise on the way there."""
        api = self.api()
        api.rename("", "nur ein Name")
        self.drained(api)
        self.assertTrue(os.path.isfile(self.session))
        self.assertIsNone(crow_core.load_session("http://127.0.0.1:1/v1", None))

    def test_a_window_that_does_not_stamp_the_name_loses_it(self):
        """NEGATIVE. The version that wrote `crow_title` into one file and left
        it there: the core's next write drops the key."""
        api = self.api(klass=_NeverStampsTheName)
        self.a_chat(api, "the first line of this chat")
        api.rename("", "Test IDE")
        self.drained(api)
        api.reset()
        titles = [r["title"] for r in self.rail(api)["rollovers"]]
        self.assertNotIn("Test IDE", titles,
                         "the old behaviour kept the name -- then the case above "
                         "is not measuring the stamp")

    # -- the rules the rail is drawn by --------------------------------------

    def test_the_open_chat_is_listed_once_and_marked(self):
        """It used to be drawn at the top AND filtered out of the list, so a
        click MOVED it -- out of where it was and into the live slot. It stays
        in the list now, marked where it sits.

        REPLACES `test_the_open_chat_is_not_listed_under_earlier`, which pinned
        the filter. The duplicate that one guarded against is what `unsaved`
        prevents: the top slot exists only for a chat with no file.
        """
        api = self.api()
        self.a_chat(api, "the open one")
        ok, path = api._leave()
        self.assertTrue(ok)
        api._reload_rail()
        entry = self.rail(api)
        listed = [r for r in entry["rollovers"] if r["path"] == path]
        self.assertEqual(len(listed), 1, "the open chat is listed once")
        self.assertTrue(listed[0]["active"])
        self.assertFalse(entry["unsaved"], "it has a file; no second slot on top")

    def test_a_chat_with_no_file_is_the_only_thing_on_top(self):
        """NEGATIVE HALF, and the case the old filter existed for: without this
        the live slot could be drawn beside the same chat's list entry."""
        api = self.api()
        self.a_chat(api, "never left")
        api._reload_rail()
        entry = self.rail(api)
        self.assertTrue(entry["unsaved"])
        self.assertEqual([r for r in entry["rollovers"] if r.get("active")], [])

    def test_a_chat_with_no_turn_in_it_gets_no_file(self):
        """Files are for conversations. A window opened and closed again must not
        leave anything in the rail."""
        api = self.api()
        ok, path = api._leave()
        self.assertTrue(ok)
        self.assertIsNone(path)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".json")], [])

    def test_leaving_a_chat_twice_writes_one_file(self):
        """Its file, not another one. This is what "new" on a chat that came out
        of the archive used to get wrong."""
        api = self.api()
        self.a_chat(api, "the same chat")
        ok, first = api._leave()
        self.assertTrue(ok)
        ok, again = api._leave()
        self.assertTrue(ok)
        self.assertEqual(first, again)
        self.assertEqual(len([n for n in os.listdir(self.dir)
                              if n.startswith("chat-")]), 1)

    def test_a_chat_that_was_archived_is_listed_in_the_drawer_and_not_above(self):
        """Archiving moves the file; the rail has to follow it in both
        directions."""
        api = self.api()
        self.a_chat(api, "put me away")
        ok, path = api._leave()
        self.assertTrue(ok)
        api._conversation.reset()
        api._current_path = None
        self.drained(api)

        api.archive_chat(path)
        entry = self.rail(api)
        self.assertEqual([r["title"] for r in entry["archived"]], ["put me away"])
        self.assertEqual([r["title"] for r in entry["rollovers"]], [])

        api.archive_chat(entry["archived"][0]["path"])
        entry = self.rail(api)
        self.assertEqual([r["title"] for r in entry["rollovers"]], ["put me away"])
        self.assertEqual(entry["archived"], [])

    def _probe_without_a_server(self, api) -> None:
        """`_probe` with the three endpoint calls answered from here.

        The window asks /health, the model name and n_ctx before it restores
        anything, and this suite has no server. Everything after those three
        lines is the real method, which is the part these cases are about.
        """
        before = (crow_gui.check_endpoint, crow_gui.fetch_model_name,
                  crow_gui.fetch_n_ctx)
        crow_gui.check_endpoint = lambda url: "ok"
        crow_gui.fetch_model_name = lambda url: "crow"
        crow_gui.fetch_n_ctx = lambda url: 200000
        try:
            api._probe()
        finally:
            (crow_gui.check_endpoint, crow_gui.fetch_model_name,
             crow_gui.fetch_n_ctx) = before


# ---------------------------------------------------- the session, twice ----

class SessionRoundTripTests(ApiCase):
    """ONE SESSION, TWO DOORS. #90's E12 point 4, with a file and a name.

    WHAT IS HERE AND WHAT IS IN E14. The two LIVE directions need a running
    server and are E14's. What can be settled without one is the part that
    decides it: whether the two doors write and read ONE file in ONE format. A
    window with a format of its own passes every live forward test and fails
    right here.
    """

    def test_what_the_window_wrote_is_what_the_cli_reads(self):
        """FORWARD. The window saves; `load_session` -- the same function
        cli/crow.py calls at start -- reads the same messages back."""
        api = self.api()
        self.a_chat(api, "was macht der Prefix-Cache", "Er haelt.")
        api._persist_live()
        self.assertTrue(os.path.exists(self.session))

        restored = crow_core.load_session("http://127.0.0.1:1/v1", None)
        self.assertIsNotNone(restored, "the CLI's reader sees no session")
        messages, _tokens, _kv = restored
        self.assertEqual([m["role"] for m in messages],
                         [m["role"] for m in api._conversation.payload()])
        self.assertEqual([m["content"] for m in messages],
                         [m["content"] for m in api._conversation.payload()])

    def test_what_the_cli_wrote_is_what_the_window_shows(self):
        """BACKWARD, and it is the direction a window with its own format fails.
        The file is written the way cli/crow.py writes one; the window has to
        SHOW it, not merely hold it."""
        conversation = crow_core.Conversation(None)
        conversation.append("user", "was macht der Prefix-Cache")
        conversation.append("assistant", "Er haelt, solange das Praefix gleich bleibt.")
        crow_core.save_session(conversation, "http://127.0.0.1:1/v1", 4711,
                               with_kv=False)

        api = self.api()
        RailTests._probe_without_a_server(self, api)
        drawn = self.drained(api)
        self.assertEqual(len(api._conversation), len(conversation))
        self.assertEqual(api._context_tokens, 4711)
        asked = [m.get("t") for m in drawn if m.get("k") == "user"]
        self.assertIn("was macht der Prefix-Cache", asked)
        self.assertIn("Er haelt, solange das Praefix gleich bleibt.",
                      self.answer_text(drawn))

    def test_a_restored_chat_is_drawn_through_the_same_sink_as_a_live_one(self):
        """A reopened chat cannot be allowed to look different from a typed one.
        It went straight to the page once, so a stored fence arrived as three
        backticks and a stored thought did not arrive at all."""
        conversation = crow_core.Conversation(None)
        conversation.append("user", "zeig mir code")
        conversation.append("assistant", "so:\n```python\nx = 1\n```\n",
                            reasoning="erst denken")
        crow_core.save_session(conversation, "http://127.0.0.1:1/v1", 1, with_kv=False)

        api = self.api()
        RailTests._probe_without_a_server(self, api)
        kinds = [m.get("k") for m in self.drained(api)]
        self.assertIn("code_open", kinds, "a stored fence was not cut by the core")
        self.assertIn("think_open", kinds, "a stored thought was not replayed")

    def test_the_window_stamps_the_file_with_the_version_the_cli_owns(self):
        """The same file from both doors means the same header from both doors."""
        api = self.api()
        self.a_chat(api, "x")
        api._persist_live()
        with io.open(self.session, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["version"], crow.VERSION)
        self.assertEqual(saved[crow_core.SESSION_FORMAT_KEY], crow_core.SESSION_FORMAT)

    def test_crows_own_keys_do_not_disturb_the_core(self):
        """`crow_title` and `crow_path` are added after the core has written. The
        file has to stay a session file both clients can open."""
        api = self.api()
        self.a_chat(api, "erste zeile")
        api.rename("", "ein name")
        restored = crow_core.load_session("http://127.0.0.1:1/v1", None)
        self.assertIsNotNone(restored, "the stamped file is no longer readable")
        with io.open(self.session, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["crow_title"], "ein name")

    def test_a_session_this_build_cannot_read_is_refused_and_left_alone(self):
        """The gate from E8, through the window's door. A refusal that still
        overwrote the file would be the data loss the gate exists against."""
        stranger = {crow_core.SESSION_FORMAT_KEY: "99",
                    "messages": [{"role": "user", "content": "hi"}]}
        with io.open(self.session, "w", encoding="utf-8") as fh:
            json.dump(stranger, fh)
        before = Path(self.session).read_bytes()

        api = self.api()
        RailTests._probe_without_a_server(self, api)
        said = " ".join(str(m.get("t", "")) for m in self.drained(api))
        self.assertIn("format", said.lower(),
                      "a refused session file was not reported to the page")
        self.assertEqual(Path(self.session).read_bytes(), before)
        # "Empty" is the system prompt and nothing else -- a fresh Conversation
        # already holds one, which is why this is not a comparison against 0.
        self.assertEqual(len(api._conversation),
                         1 if api._conversation.has_system else 0)


# --------------------------------------------------- the seam to the page ---

def _code_only(source: str) -> str:
    """The file with its comment lines dropped, Python's and the page's.

    THE SAME RULE tools/check_operating_point.py's `code_only` states: a
    sentence explaining a message that USED to be pushed is not a place that
    pushes it. Without this the seam check below reported `_round` as a live
    hole because the comment recording its removal names it.

    THREE COMMENT SYNTAXES, because this file is three languages in one: Python,
    the page's JavaScript, and its CSS -- and the CSS block comment is the one
    that carries the longest explanations, including the two traps below.
    """
    blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(line for line in blocks.splitlines()
                     if not line.lstrip().startswith(("#", "//")))


def _message_kinds(source: str) -> set:
    """Every `k` the Python side pushes."""
    return set(re.findall(r'\{"k":\s*"([a-z_]+)"', _code_only(source)))


def _drawn_kinds(source: str) -> set:
    """Every `k` the page has a case for."""
    return set(re.findall(r'case\s+"([a-z_]+)":', _code_only(source)))


class SlashCommandsReachTheWindowTests(ApiCase):
    """#94. The window handled `/tools` and nothing else.

    The other six travelled to the server as ordinary questions and came back as
    an answer about the word -- `/reset`, `/context`, `/thoughts`, `/mode`,
    `/exit`, `/quit`. That is the divergence #90 exists to prevent, in the shape
    no checker sees: both surfaces call the same core, and the difference is in
    what never reaches it.

    THE DECISION (robin, 2026-08-14) IS NOT "PORT THE COMMANDS". Four of the
    seven already have a widget here, so those point at it and only the ones
    without are executed. What is shared is the LIST, not the answer.
    """

    class _StubWindow:
        """`/exit` reaches `window.destroy()`, which a test has no window for."""

        def __init__(self):
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    def windowed(self, *argv):
        api = self.api(*argv)
        api._window = self._StubWindow()
        return api

    def test_every_shared_command_gets_an_answer(self):
        for command in crow_core.SLASH_COMMANDS:
            api = self.windowed()
            self.assertIsNotNone(api.slash_answer(command),
                                 f"{command} still travels to the model")

    # -- what /reset means, which is the whole reason this was rebuilt --------

    def test_reset_drops_the_context(self):
        """The TERMINAL's meaning: `conversation.reset()` and the releases go."""
        api = self.windowed()
        # THE BASELINE IS NOT ZERO: a Conversation carries its system message,
        # and `reset()` keeps it. Comparing against 0 would fail on a correct
        # reset and pass on one that threw the system prompt away.
        empty = len(api._conversation)
        api._conversation.append("user", "something")
        api._context_tokens = 4321
        crow_core.remember("write_file", json.dumps({"path": "x"}))
        api.slash_answer("/reset")
        self.assertEqual(len(api._conversation), empty)
        self.assertEqual(api._context_tokens, 0)
        self.assertFalse(crow_core.remembered("write_file",
                                              json.dumps({"path": "x"})))

    def test_reset_does_NOT_archive_the_chat(self):
        """THE CASE THIS WHOLE REBUILD IS FOR.

        The first version answered `/reset` with "that is the new button" -- and
        `new` archives the conversation into the rail and opens an empty one.
        `/reset` keeps the chat where it is. Two operations, and pointing one at
        the other is worse than not handling it at all: the user follows the
        instruction and files away a chat they meant to keep.
        """
        api = self.windowed()
        api._conversation.append("user", "something")
        before = sorted(os.listdir(self.dir))
        note = api.slash_answer("/reset")
        self.assertEqual(sorted(os.listdir(self.dir)), before,
                         "/reset wrote a file; that is what `new` does")
        self.assertNotIn("put aside", note)
        self.assertIn("prefill", note)

    def test_reset_survives_closing_the_window(self):
        """ROBIN'S REPORT, 2026-08-14: "/reset wird scheinbar nicht gespeichert
        wenn man crow schließt".

        He was right, and it was not the window's fault: `save_session` will not
        write an empty conversation, so the file from before the reset stayed
        and the next start restored it. The whole chain is driven here -- write a
        session, drop it, close, and ask what a restart would find -- because
        every link of it was individually green while the chain was broken.
        """
        api = self.windowed()
        api._conversation.append("user", "Lies aufgabe.txt")
        api._conversation.append("assistant", "ok")
        api._context_tokens = 1100
        crow_core.save_session(api._conversation, api._args.base_url, 1100,
                               with_kv=False)
        self.assertTrue(os.path.exists(self.session), "nothing was there to lose")

        api.slash_answer("/reset")
        api.close()
        self.assertIsNone(crow_core.load_session(api._args.base_url),
                          "the dropped conversation came back")

    def _opened_from_the_rail(self):
        """An Api holding a chat that came out of the rail, as `open()` leaves it."""
        api = self.windowed()
        path = os.path.join(self.dir, "chat-20260814-120000.json")
        talk = crow_core.Conversation("SYS")
        talk.append("user", "hey my friend")
        talk.append("assistant", "hi")
        crow_core.save_session(talk, api._args.base_url, 900,
                               path=path, with_kv=False)
        api._conversation = talk
        api._current_path = path
        api._context_tokens = 900
        return api, path

    def test_a_reset_lets_go_of_the_chat_it_came_from(self):
        """ROBIN, 2026-08-14: "/reset in einem EARLIER Fenster geht erst, aber
        nach Neustart ist der Text samt cache und context wieder da."

        A chat opened out of the rail keeps `_current_path`, and `close()`
        archives the open conversation THERE -- except `save_session` refuses an
        empty one, so the file kept its old messages and the next start found
        them. Removing `session.json` alone fixed the live case and left this
        one, which is one half of the same seam again.
        """
        api, path = self._opened_from_the_rail()
        api.slash_answer("/reset")
        self.assertIsNone(api._current_path, "still bound to the chat it dropped")
        api.close()
        self.assertIsNone(crow_core.load_session(api._args.base_url),
                          "the dropped conversation came back")

    def test_but_it_does_NOT_throw_the_saved_chat_away(self):
        """NEGATIVE HALF, and the more important one. `/reset` drops the
        context; it is not "delete my saved chat". A fix that removed the file
        would pass the case above and quietly destroy work."""
        api, path = self._opened_from_the_rail()
        api.slash_answer("/reset")
        api.close()
        self.assertTrue(os.path.exists(path), "/reset deleted a saved chat")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["messages"]), 3)

    def test_and_the_name_of_that_chat_survives_the_reset_too(self):
        """THE OTHER HALF OF THE SAME PROMISE. The case above pins the MESSAGES
        of a chat `/reset` let go of and says nothing about its IDENTITY -- and
        the name is the one thing about a chat the user typed themselves. A
        detached chat that came back nameless would be the same loss with the
        text still in it.

        WHAT THIS DOES NOT PROVE, and the commit says so rather than implying
        otherwise: it is not a negative probe for the `else: data.pop(...)`
        `_stamp` carried until now. That branch needed `_current_title` to be
        None while a file it was about to stamp still held a name, and `/reset`
        clears `_current_path` in the same breath -- so nothing stamps this file
        at all and the case is green either way. The branch was unreachable,
        which is why it went without a case of its own. This one holds the
        promise it had been standing next to.
        """
        api, path = self._opened_from_the_rail()
        self.assertTrue(api.rename(path, "Schmetterlinge"))
        api.slash_answer("/reset")
        api.close()
        self.assertIsNone(api._current_title, "the window kept a name it dropped")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh).get("crow_title"), "Schmetterlinge",
                             "/reset took the name off a chat it only let go of")

    def test_no_session_leaves_the_file_alone(self):
        """NEGATIVE HALF. `--no-session` means this client does not own that
        file, and a reset is not a licence to delete somebody else's."""
        api = self.windowed("--no-session")
        api._args.session = False
        talk = crow_core.Conversation("SYS")
        talk.append("user", "not ours to drop")
        crow_core.save_session(talk, api._args.base_url, 10, with_kv=False)
        api.slash_answer("/reset")
        self.assertTrue(os.path.exists(self.session))

    def test_reset_clears_the_page_too(self):
        """The conversation and what is on screen are two things, and only one
        of them is Python's. `new` clears the flow from the page side before it
        calls in; a command answered here has to say so on the queue."""
        api = self.windowed()
        api.slash_answer("/reset")
        self.assertIn("clear", [m.get("k") for m in self.drained(api)])

    def test_reset_is_refused_mid_turn(self):
        """`set_mode` and `set_tools` both refuse; dropping the context under a
        running turn is the same class and a louder failure."""
        api = self.windowed()

        class _Busy:
            def is_alive(self):
                return True

        api._worker = _Busy()
        api._conversation.append("user", "something")
        held = len(api._conversation)
        self.assertIn("mid-turn", api.slash_answer("/reset"))
        self.assertEqual(len(api._conversation), held)

    # -- the other four ------------------------------------------------------

    def test_context_reports_the_three_figures(self):
        api = self.windowed()
        api._conversation.append("user", "something")
        api._context_tokens = 1234
        api._n_ctx = 200000
        line = api.slash_answer("/context")
        self.assertIn("1234 tokens", line)
        self.assertIn("messages", line)
        self.assertIn("rolls over at", line)

    def test_mode_reports_and_switches(self):
        api = self.windowed()
        self.assertIn(api._args.mode, api.slash_answer("/mode"))
        api.slash_answer("/mode manual")
        self.assertEqual(api._args.mode, "manual")

    def test_a_switch_is_announced_once_and_not_twice(self):
        """`set_mode` pushes its own note. A second from the command put the
        switch on screen twice -- the same defect as the doubled echo, in a
        different half. Found by robin in the window, again."""
        api = self.windowed()
        api.send("/mode manual")
        notes = [m for m in self.drained(api) if m.get("k") == "note"]
        self.assertEqual(len(notes), 1, [n.get("t") for n in notes])
        self.assertIn("manual", notes[0]["t"])

    def test_an_empty_answer_is_handled_but_not_shown(self):
        """NEGATIVE HALF of the line above: "" means handled-and-already-said,
        None means not-ours. Confusing them sends `/mode manual` to the model."""
        api = self.windowed()
        self.assertEqual(api.slash_answer("/mode manual"), "")
        self.assertIs(api.send("/mode auto"), False)

    def test_an_unknown_level_is_named_rather_than_ignored(self):
        """NEGATIVE HALF of the switch: a typo that silently does nothing is a
        level the user believes they are on."""
        api = self.windowed()
        answer = api.slash_answer("/mode careful")
        self.assertIn("careful", answer)
        self.assertNotEqual(api._args.mode, "careful")

    def test_thoughts_folds_and_unfolds(self):
        api = self.windowed()
        first = api.slash_answer("/thoughts")
        second = api.slash_answer("/thoughts")
        folds = [m for m in self.drained(api) if m.get("k") == "thoughts"]
        self.assertEqual([m["open"] for m in folds], [True, False])
        self.assertIn("opened", first)
        self.assertIn("closed", second)

    def test_exit_closes_the_window(self):
        api = self.windowed()
        api.slash_answer("/exit")
        self.assertTrue(api._window.destroyed)

    def test_quit_does_the_same(self):
        api = self.windowed()
        api.slash_answer("/quit")
        self.assertTrue(api._window.destroyed)

    def test_the_help_listing_covers_every_one_of_them(self):
        listing = self.api().help_listing()
        for command in crow_core.SLASH_COMMANDS:
            self.assertIn(command, listing)
        self.assertNotIn("nothing here answers this yet", listing)

    def test_an_argument_does_not_send_it_to_the_model(self):
        """`/mode manual` is the form the terminal documents, so it is the form
        a user brings over."""
        self.assertIsNotNone(self.api().slash_answer("/mode manual"))

    def test_tools_still_answers_with_the_schema(self):
        """The one command that already worked has to keep working -- it is the
        one the input's own placeholder advertises."""
        self.assertIn("the model can call", self.api().slash_answer("/tools"))

    # -- the negative half --------------------------------------------------

    def test_an_unknown_slash_word_still_reaches_the_model(self):
        """NEGATIVE CONTROL. A window that swallows everything starting with a
        slash has taken a question away from the thing that could answer it --
        and it would pass every case above."""
        self.assertIsNone(self.api().slash_answer("/nonsense"))

    def test_a_question_about_a_path_still_reaches_the_model(self):
        """The case that makes the one above concrete rather than theoretical."""
        api = self.api()
        self.assertIsNone(api.slash_answer("/usr/bin/env is what?"))
        self.assertIsNone(api.slash_answer("/etc/hosts"))

    def test_an_empty_message_is_not_a_command(self):
        self.assertIsNone(self.api().slash_answer("   "))

    # -- the pointers have to point at something that exists ----------------

    def test_no_answer_describes_where_a_control_is(self):
        """THE RULE THAT REPLACED THE POINTERS, and it is the one a later change
        will be tempted to break.

        The first version answered each command with a sentence naming the
        widget that does the same job. One of those sentences was wrong about
        which side of the rail a button sits on, and the case meant to catch
        that only asserted the button's id appears in the page -- so it could
        not have. Prose about pixels cannot be tested, which is the argument
        against writing any.
        """
        api = self.windowed()
        answers = [api.slash_answer(c) or "" for c in crow_core.SLASH_COMMANDS]
        answers.append(api.help_listing())
        for said in answers:
            for word in ("button", "top left", "top right", "beside", "dropdown",
                         "title bar", "click"):
                self.assertNotIn(word, said.lower(),
                                 f"an answer describes a control: {said!r}")

    def test_a_multi_line_answer_survives_the_page(self):
        """`/help` and `/tools` build a column with their own line breaks. The
        default `white-space` collapsed all of it into one run-on paragraph --
        eight commands on a single line, true of `/tools` since the day the
        window answered it, and found by robin in the window rather than here.

        BOTH HALVES, because either alone is green and useless: the answer has
        to carry newlines, and the page has to keep them.
        """
        self.assertIn("\n", self.windowed().help_listing())
        # THE SELECTOR HAS TO BE ANCHORED. There are two `.note` rules and the
        # other one is `.tool .note{…white-space:nowrap}`, for the timing on a
        # tool row -- searching for ".note{" finds that one first and reads a
        # rule that is correctly the opposite of what this pins.
        rule = crow_gui.PAGE.split("\n.note{")[1].split("}")[0]
        self.assertIn("white-space:pre-wrap", rule,
                      "the page collapses a multi-line note again")

    def test_every_command_on_the_shared_list_is_described(self):
        """The help a user reads is built from the shared list, so a command
        added there and forgotten here shows up as a gap rather than silence."""
        for command in crow_core.SLASH_COMMANDS:
            self.assertIn(command, crow_gui.Api.WHAT_THEY_DO,
                          f"{command} has no line in the window's help")

    # -- the seam to the page, which the cases above could not see -------------
    #
    # THESE THREE REPLACE A CASE THAT PINNED THE DEFECT. It asserted the Api
    # pushes a `user` echo before the note, which is what the code did and what
    # robin's window showed to be wrong: the typed command appeared TWICE, and
    # the composer stayed on "Stop". Every case above passed throughout, because
    # they drive the Api with no page on the other side -- one half of a seam
    # measuring itself.

    def test_the_command_is_not_echoed_from_this_side(self):
        """The page draws the line before it calls us. A second echo here is the
        same command on screen twice -- wrong for `/tools` before #94, and wrong
        for all seven after it."""
        api = self.windowed()
        api.send("/reset")
        kinds = [m.get("k") for m in self.drained(api)]
        self.assertNotIn("user", kinds)
        self.assertIn("note", kinds)

    def test_a_command_reports_that_no_turn_started(self):
        """WHAT THE PAGE WAITS ON. `pywebview.api.*` resolves a promise when
        this returns, so the composer can be painted from the one fact only this
        side has -- whether there is anything to stop. It used to paint "Stop"
        on the way in and hope, which is how `/reset` left the window sitting on
        Stop with nothing behind it."""
        self.assertIs(self.api().send("/help"), False)

    def test_a_line_mid_turn_reports_no_turn_either(self):
        """The other way to start nothing. The page keeps a second line out on
        its own, but this is what lets it unlock if it ever gets that wrong."""
        api = self.api()

        class _Busy:
            def is_alive(self):
                return True

        api._worker = _Busy()
        self.assertIs(api.send("hello"), False)

    def test_an_ordinary_message_reports_that_one_did(self):
        """POSITIVE CONTROL. Without it the rule could be "always return False",
        which passes both cases above and leaves the button dead for real turns.
        """
        api = self.api()
        self.addCleanup(lambda: crow_core.INTERRUPT.set())
        self.assertIs(api.send("what is here?"), True)

    def test_the_page_paints_from_the_answer_and_not_before(self):
        """THE ANCHOR FOR ALL THREE ABOVE, and the half a python case cannot
        reach. They are only correct while `go()` draws the line itself, locks
        synchronously, and leaves the button to the promise. If that changes,
        the Api has to take one of those jobs back -- and a reader finds it here
        rather than in a screenshot, which is where it was found last time.
        """
        self.assertIn("this.user(text)", crow_gui.PAGE)
        self.assertIn("this.running=true", crow_gui.PAGE)
        self.assertIn("started => started ? this.busy() : this.idle()",
                      crow_gui.PAGE)
        # busy() may no longer be called on the way in -- that IS the defect.
        self.assertNotIn("this.user(text); this.busy()", crow_gui.PAGE)


class TheSeamToThePageTests(unittest.TestCase):
    """Python speaks, the page listens, and nothing checks that they agree.

    THIS FOUND A REAL HOLE. `{"k": "_round"}` carried the reasoning share of
    every turn and the page's switch had no case for it, so it fell through and
    the cost line was drawn with a share of null. Nothing was broken enough to
    look broken: the window worked, and one number was silently always absent.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")

    def test_every_message_the_window_pushes_has_a_case_on_the_page(self):
        """POSITIVE."""
        holes = sorted(_message_kinds(self.source) - _drawn_kinds(self.source))
        self.assertEqual(holes, [],
                         "pushed to a page that cannot draw it: %s" % holes)

    def test_the_check_sees_a_hole_when_there_is_one(self):
        """NEGATIVE, against the predicate itself. A regex that matched nothing
        would report a clean seam forever."""
        holes = _message_kinds(self.source + '\nself.push({"k": "invented"})\n')
        self.assertIn("invented", holes - _drawn_kinds(self.source))

    def test_the_page_draws_nothing_the_window_never_sends(self):
        """The other direction is a weaker rule but the same drift: a case for a
        message no one sends is a feature that was removed from one side."""
        orphans = sorted(_drawn_kinds(self.source) - _message_kinds(self.source))
        self.assertEqual(orphans, [],
                         "the page has a case for messages nothing sends: %s"
                         % orphans)


# ------------------------------------------------------- the file itself ----

class TheWindowBorrowsAndDoesNotRebuildTests(unittest.TestCase):
    """The rules E12 states about the FILE, held against the file.

    tools/check_shared_core.py holds the same lines against the manifest and is
    the tool that has to stay green. These are the ones it cannot express.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        # COMMENTS OUT: a sentence explaining why a value may not be written
        # here is not a place that writes it.
        self.code = _code_only(self.source)

    def test_no_callback_ends_the_process(self):
        """`main` in cli/crow.py catches CrowError and returns 2. A `sys.exit`
        inside a callback is the wrong translation of that: it takes the window,
        the unsaved session and the running turn with it."""
        body = self.source.split('def main(')[0]
        self.assertNotIn("sys.exit(", body,
                         "a callback in this file can end the process")

    def test_every_function_declares_what_it_returns(self):
        """The type idiom: 85 of 88 functions in the existing client carry a
        return type. A new file is where that quietly stops being true."""
        import ast

        tree = ast.parse(self.source)
        missing = [node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.returns is None
                   and not node.name.startswith("<")]
        self.assertEqual(missing, [], "functions with no return type: %s" % missing)

    def test_the_brand_values_are_not_written_here(self):
        """`#0b0e17` and the accent come out of the core. Written here they would
        be a second copy to correct, which is what the manifest counts."""
        self.assertNotIn("#0b0e17", self.code)
        self.assertNotIn("#7eb0f8", self.code)
        self.assertIn("CROW_BG", self.code)
        self.assertIn("CROW_ACCENT_HEX", self.code)

    def test_the_version_literal_does_not_appear_here(self):
        """install.ps1 greps cli/crow.py for `^VERSION = "..."`. A second file
        carrying one is a second thing to bump, and the stale one is the one no
        release step reads."""
        self.assertIsNone(re.search(r'^VERSION\s*=\s*"', self.source, re.M))
        self.assertEqual(crow_gui.client_version(), crow.VERSION)

    def test_a_missing_client_file_leaves_the_version_empty(self):
        """The empty default is load-bearing: a window that could not read the
        version must stay quiet rather than stamp a session with a guess."""
        empty = tempfile.mkdtemp(prefix="crow-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertEqual(crow_gui.client_version(os.path.join(empty, "crow.py")), "")

    def test_the_drag_region_is_the_one_webview2_understands(self):
        """`-webkit-app-region: drag` is Electron's. WebView2 does not know it,
        and the window could not be moved at all. Measured 2026-08-13.

        The DECLARATION is what this rules out, not the string: the file also
        explains the trap in a comment, and a check that could not tell those
        apart would have to be deleted the first time anyone wrote it down.
        """
        self.assertIn("pywebview-drag-region", self.code)
        self.assertIsNone(re.search(r"-webkit-app-region\s*:\s*drag", self.code),
                          "the window is dragged by a property WebView2 ignores")

    def test_the_clipboard_does_not_go_through_the_page(self):
        """`navigator.clipboard` refuses silently outside a secure context, and
        the page is handed over as HTML rather than served over https: the copy
        button reported success over an empty clipboard. Measured 2026-08-13."""
        self.assertIsNone(re.search(r"navigator\.clipboard\s*\.", self.code),
                          "the copy button writes through a clipboard that "
                          "refuses without raising")
        self.assertIn("def copy(self, text: str) -> bool:", self.source)


class TheFolderPickerTests(ApiCase):
    """#92 in the window: the folder picker, and what it is allowed to do.

    THE PICKER IS THE ONLY THING THAT CREATES A ROOT, so these cases are the
    other half of the core's boundary rather than a second copy of it. What is
    checked here is the window's share: that the state reaches the page at all,
    that a directory the user never picked cannot become one, and that the
    boundary cannot move under a running turn.
    """

    def setUp(self) -> None:
        super().setUp()
        self._roots = crow_core.ROOTS_FILE
        crow_core.ROOTS_FILE = os.path.join(self.dir, "roots.json")
        self.addCleanup(setattr, crow_core, "ROOTS_FILE", self._roots)
        self.addCleanup(crow_core.set_root, None)
        crow_core.set_root(None)
        self.root = os.path.join(self.dir, "projekt")
        os.makedirs(self.root)

    def _root_msg(self, api):
        msgs = [m for m in self.drained(api) if m.get("k") == "root"]
        self.assertTrue(msgs, "the page was never told about the working directory")
        return msgs[-1]

    def test_ready_tells_the_page_there_is_none(self):
        """THE ABSENCE IS THE STATE THAT MUST BE VISIBLE. A window that says
        nothing when nothing is bound reads as "bounded and fine"."""
        api = self.api()
        api.ready()
        msg = self._root_msg(api)
        self.assertEqual(msg["path"], "")
        self.assertEqual(msg["roots"], [])

    def test_a_root_restored_by_the_session_reaches_the_page(self):
        """THE SEAM, and it is the one that bit three times on 2026-08-14.

        The restore runs on the probe thread, AFTER `ready()` has already told the
        page what it bound. Without a second push the boundary holds while the
        button still says something else -- screen and loop disagreeing, which is
        worse than either state alone. No Api-only case sees it; this one drives
        `_probe` itself.

        THE SETUP WAS REPLACED ON 2026-08-15 (#101), AND THE ASSERTIONS WERE NOT.
        It used to hand `load_session` a `side_effect` that called `set_root` --
        staging an internal call that has never existed: nothing outside
        `adopt_root` and the picker has ever bound the root, and the comment that
        claimed otherwise was removed the same day. The claim this case makes is
        unchanged and is now stricter, because the folder it checks for arrives
        the way the product actually delivers it: out of the chat's own file.
        """
        crow_core.write_root_mode(self.root, "auto")
        api = self.api()
        # The chat's file carries its root, written the way the window writes it.
        api._current_title = "ein Chat mit Ordner"
        crow_core.set_root(self.root)
        api._root_chosen = True                   # chosen for this chat, not borrowed
        api._stamp(crow_gui.SESSION_FILE, pointer=True)
        crow_core.set_root(None)
        api._current_title = None
        with mock.patch.object(crow_gui, "check_endpoint", return_value="ok"), \
             mock.patch.object(crow_gui, "model_display_name", return_value="m"), \
             mock.patch.object(crow_gui, "fetch_model_name", return_value="m"), \
             mock.patch.object(crow_gui, "fetch_n_ctx", return_value=1000), \
             mock.patch.object(crow_gui, "load_session",
                               return_value=([{"role": "user", "content": "hi"}],
                                             10, False)):
            api._probe()
        msg = self._root_msg(api)
        self.assertEqual(os.path.normcase(msg["path"]),
                         os.path.normcase(os.path.realpath(self.root)))
        self.assertEqual(msg["name"], "projekt")

    # -- #101: the working directory belongs to the chat ---------------------

    def _chat_with_root(self, name, root):
        """A chat file on disk that carries `root` as its own, the way `_stamp`
        writes it. Returns its path."""
        api = self.api()
        api._current_title = name
        crow_core.set_root(root)
        api._root_chosen = True                   # a person picked, for this chat
        api._stamp(crow_gui.SESSION_FILE, pointer=True)
        crow_core.set_root(None)
        path = os.path.join(self.dir, "chat-%s.json" % name)
        os.replace(crow_gui.SESSION_FILE, path)
        return path

    def test_a_chat_brings_its_own_working_directory(self):
        """POSITIVE (#101). robin: switching the folder in one chat used to move
        it for every chat, because the window bound one root for the process."""
        other = os.path.join(self.dir, "zweites-projekt")
        os.makedirs(other)
        crow_core.write_root_mode(self.root, "auto")
        crow_core.write_root_mode(other, "auto")
        theirs = self._chat_with_root("A", other)

        api = self.api()
        crow_core.set_root(self.root)                 # some other chat's folder
        api._adopt_chat_root(theirs)
        self.assertEqual(os.path.normcase(crow_core.get_root() or ""),
                         os.path.normcase(os.path.realpath(other)))

    def test_a_chat_that_chose_no_folder_keeps_that_across_a_switch(self):
        """"None" is a choice here too, and it is the chat's. Without the third
        state a chat deliberately working unbounded would be handed the template
        every time it was opened."""
        crow_core.write_root_mode(self.root, "auto")
        theirs = self._chat_with_root("ohne", None)
        crow_core.set_active_root(self.root)          # a template that must lose

        api = self.api()
        crow_core.set_root(self.root)
        api._adopt_chat_root(theirs)
        self.assertIsNone(crow_core.get_root())

    def test_a_chat_that_never_chose_takes_the_template_not_what_was_bound(self):
        """THE NEGATIVE HALF, and the whole defect in one case. Every chat file
        written before #101 has no root in it. Falling through to "whatever is
        bound" is exactly what made one chat's folder leak into all the others --
        so an unmarked chat takes the template, never the neighbour's."""
        leftover = os.path.join(self.dir, "vom-vorherigen-chat")
        os.makedirs(leftover)
        crow_core.write_root_mode(self.root, "auto")
        crow_core.write_root_mode(leftover, "auto")
        old = os.path.join(self.dir, "chat-alt.json")
        with open(old, "w", encoding="utf-8") as fh:
            json.dump({"messages": []}, fh)           # no crow_root at all
        crow_core.set_active_root(self.root)

        api = self.api()
        crow_core.set_root(leftover)                  # the neighbour's folder
        api._adopt_chat_root(old)
        self.assertEqual(os.path.normcase(crow_core.get_root() or ""),
                         os.path.normcase(os.path.realpath(self.root)))

    def test_a_new_chat_starts_from_the_template(self):
        """Same rule from the other side: "new chat" is a chat that never chose."""
        leftover = os.path.join(self.dir, "vorher")
        os.makedirs(leftover)
        crow_core.write_root_mode(self.root, "auto")
        crow_core.write_root_mode(leftover, "auto")
        crow_core.set_active_root(self.root)

        api = self.api()
        crow_core.set_root(leftover)
        api._adopt_chat_root(None)
        self.assertEqual(os.path.normcase(crow_core.get_root() or ""),
                         os.path.normcase(os.path.realpath(self.root)))

    def test_the_level_stays_with_the_folder_not_with_the_chat(self):
        """THE SECOND NEGATIVE HALF. The level is a statement about the project,
        so two chats in ONE folder share it. Move it into the chat and the same
        directory has different rights depending on which chat is open."""
        crow_core.write_root_mode(self.root, "manual")
        one = self._chat_with_root("eins", self.root)
        two = self._chat_with_root("zwei", self.root)

        api = self.api()
        api._adopt_chat_root(one)
        first = api._args.mode
        api._adopt_chat_root(two)
        self.assertEqual(api._args.mode, first)
        self.assertEqual(api._args.mode, "manual")

    def test_a_borrowed_root_is_never_written_into_the_chat(self):
        """THE CASE THAT MUST FAIL, and it is the defect robin hit within minutes
        of #101 landing: he picked a folder in one chat, switched to another, and
        the second chat -- a file from before the ticket, with no root of its own
        -- took the template and then OWNED it. A fallback is a guess, and the
        rule against writing a guess down is already in this file for the name:
        "that guess must never be stamped back into the file as though it had
        been chosen".
        """
        crow_core.write_root_mode(self.root, "auto")
        old = os.path.join(self.dir, "chat-ohne-wahl.json")
        with open(old, "w", encoding="utf-8") as fh:
            json.dump({"messages": [], "crow_title": "alt"}, fh)
        crow_core.set_active_root(self.root)

        api = self.api()
        api._adopt_chat_root(old)                  # borrows the template
        self.assertEqual(os.path.normcase(crow_core.get_root() or ""),
                         os.path.normcase(os.path.realpath(self.root)))
        api._current_title = "alt"
        api._stamp(old)

        with open(old, encoding="utf-8") as fh:
            self.assertNotIn("crow_root", json.load(fh),
                             "the borrowed folder was written into the chat")

    def test_a_named_empty_chat_survives_being_left(self):
        """POSITIVE. #100 kept a named empty chat across closing the window;
        switching away still dropped it, because `_leave` had nothing to archive
        and the next write put the other chat over session.json. robin lost a
        chat called "Schmetterlinge" that way, folder and all."""
        api = self.api()
        api.rename("", "Schmetterlinge")
        self.drained(api)
        ok, kept = api._leave()
        self.assertTrue(ok)
        self.assertIsNotNone(kept, "a named empty chat was not put anywhere")
        self.assertEqual(self._stored_title_of(kept), "Schmetterlinge")

    def test_a_named_empty_chat_can_be_opened_again(self):
        """THE ROUND TRIP, and the case whose absence cost three defects in a row.

        Writing is half a contract. `_leave` produced a file for a named empty
        chat and the suite was green -- while `open` refused that very file with
        "empty: chat-....json", because `load_session` answers None for anything
        with no messages. robin created a chat called "Youtube", switched away,
        and could never get back into it.

        THE RULE THIS CASE EXISTS TO ENFORCE: every path that writes state to
        disk needs, in the same commit, a case that reads that file back and puts
        the window into the state the user expects. Testing that the write
        happened is testing the mechanism, not the promise.
        """
        api = self.api()
        api.rename("", "Youtube")
        self.drained(api)
        ok, kept = api._leave()
        self.assertTrue(ok)

        second = self.api()
        second.open(kept)
        fails = [m for m in self.drained(second) if m.get("k") == "fail"]
        self.assertEqual(fails, [], "opening the reserved slot was refused")
        self.assertEqual(second._current_title, "Youtube")
        self.assertEqual(os.path.normcase(second._current_path or ""),
                         os.path.normcase(kept))
        spoken = [m["role"] for m in second._conversation.payload()
                  if m["role"] in ("user", "assistant")]
        self.assertEqual(spoken, [], "the reserved slot came back with a turn in it")

    def test_an_archive_nobody_named_and_with_nothing_in_it_is_still_refused(self):
        """THE NEGATIVE HALF of the round trip. Emptiness alone must not become a
        thing worth opening, or a truncated or half-written file reads as a chat
        and the user is handed a window that silently lost its contents."""
        broken = os.path.join(self.dir, "chat-kaputt.json")
        with open(broken, "w", encoding="utf-8") as fh:
            json.dump({"messages": []}, fh)
        api = self.api()
        api.open(broken)
        fails = [m for m in self.drained(api) if m.get("k") == "fail"]
        self.assertTrue(fails, "an unnamed empty archive was opened as a chat")
        self.assertIn("empty", fails[-1]["t"])

    def test_an_unnamed_empty_chat_is_still_dropped_when_it_is_left(self):
        """THE NEGATIVE HALF. A stray "new" click must still vanish, or the rail
        fills with conversations nobody started -- which is what the emptiness
        rule was for before the name became the line."""
        api = self.api()
        ok, kept = api._leave()
        self.assertTrue(ok)
        self.assertIsNone(kept, "an unnamed empty chat was filed away")

    @staticmethod
    def _stored_title_of(path):
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh).get("crow_title") or "").strip() or None

    def test_choosing_a_root_binds_it_and_tells_the_page(self):
        api = self.api()
        crow_core.write_root_mode(self.root, "auto")
        api.choose_root(self.root)
        self.assertEqual(os.path.normcase(crow_core.get_root()),
                         os.path.normcase(os.path.realpath(self.root)))
        self.assertEqual(self._root_msg(api)["name"], "projekt")

    def test_choosing_writes_the_marker_so_the_pick_survives(self):
        api = self.api()
        api.choose_root(self.root)
        self.assertTrue(os.path.isfile(crow_core.root_file(self.root)))

    def test_a_directory_that_is_gone_is_refused_and_the_page_is_resynced(self):
        api = self.api()
        api.choose_root(os.path.join(self.dir, "weg"))
        self.assertIsNone(crow_core.get_root())
        self.assertIn("fail", self.kinds(api))

    def test_the_level_follows_the_root(self):
        """robin's decision: opening a folder restores what it was allowed to do."""
        api = self.api()
        crow_core.write_root_mode(self.root, "manual")
        api.choose_root(self.root)
        self.assertEqual(api._args.mode, "manual")
        self.assertIn("mode", self.kinds(api))

    def test_clearing_the_root_is_offered_and_works(self):
        api = self.api()
        api.choose_root(self.root)
        api.clear_root()
        self.assertIsNone(crow_core.get_root())
        self.assertEqual(self._root_msg(api)["path"], "")

    def test_clearing_leaves_the_folder_in_the_menu(self):
        api = self.api()
        api.choose_root(self.root)
        api.clear_root()
        self.assertEqual([r["name"] for r in self._root_msg(api)["roots"]], ["projekt"])

    def test_the_root_does_not_move_mid_turn(self):
        """Same rule as `set_mode`: half a turn allowed to write where the other
        half may not is worse than either boundary alone."""
        api = self.api()
        api._worker = _AliveWorker()
        api.choose_root(self.root)
        self.assertIsNone(crow_core.get_root())

    def test_the_picker_does_not_open_mid_turn(self):
        api = self.api()
        api._worker = _AliveWorker()
        api._window = _RefusingWindow()          # would raise if it were called
        api.pick_root()
        self.assertIsNone(crow_core.get_root())

    def test_a_cancelled_dialog_changes_nothing_and_says_nothing(self):
        """Cancel is an answer, not a failure. A note on every cancel trains the
        user to ignore notes."""
        api = self.api()
        api._window = _PickingWindow(None)
        api.pick_root()
        self.assertIsNone(crow_core.get_root())
        self.assertEqual([k for k in self.kinds(api) if k in ("fail", "note")], [])

    def test_picking_a_folder_binds_it(self):
        api = self.api()
        api._window = _PickingWindow((self.root,))
        api.pick_root()
        self.assertEqual(os.path.normcase(crow_core.get_root()),
                         os.path.normcase(os.path.realpath(self.root)))

    def test_picking_a_folder_is_what_the_next_start_reads(self):
        """THE WIRING, not the rule -- the rule is `AdoptRootTests`'s. Without
        this case the core could restore correctly forever while the window never
        told it anything, which is the state #92 was in until 2026-08-15: fifteen
        lines of comment describing a restore, and nothing writing what to
        restore."""
        api = self.api()
        api._window = _PickingWindow((self.root,))
        api.pick_root()
        restored, problem = crow_core.restore_root()
        self.assertIsNone(problem)
        self.assertEqual(os.path.normcase(restored or ""),
                         os.path.normcase(os.path.realpath(self.root)))

    def test_choosing_no_folder_overwrites_the_remembered_one(self):
        """"None" is a choice and has to outlive the window. If `clear_root` only
        cleared the live boundary, the next start would restore the folder the
        user had just switched off -- and it would look like the button did
        nothing."""
        api = self.api()
        api._window = _PickingWindow((self.root,))
        api.pick_root()
        api.clear_root()
        restored, problem = crow_core.restore_root()
        self.assertIsNone(restored)
        self.assertIsNone(problem)

    def test_a_cancelled_dialog_leaves_the_remembered_choice_alone(self):
        """Cancel changes nothing -- and "nothing" now includes what the next
        start will bind. The existing case above pins the live state; this pins
        the stored one, which a cancel could otherwise quietly overwrite with a
        null."""
        api = self.api()
        api._window = _PickingWindow((self.root,))
        api.pick_root()
        api._window = _PickingWindow(None)
        api.pick_root()
        restored, _ = crow_core.restore_root()
        self.assertEqual(os.path.normcase(restored or ""),
                         os.path.normcase(os.path.realpath(self.root)))

    def test_a_dialog_that_throws_is_not_a_crash(self):
        api = self.api()
        api._window = _RefusingWindow()
        api.pick_root()
        self.assertIsNone(crow_core.get_root())

    def test_the_recent_list_only_offers_roots_that_still_declare_themselves(self):
        api = self.api()
        api.choose_root(self.root)
        os.remove(crow_core.root_file(self.root))
        api.push_root()
        self.assertEqual(self._root_msg(api)["roots"], [])

    def test_the_page_has_the_button_and_its_handler(self):
        """The seam #90 exists for: a bridge method with nothing calling it is a
        feature that does not exist, and no Api case can see the difference."""
        page = crow_gui.PAGE
        self.assertIn('id="root"', page)
        self.assertIn("crow.rootMenu()", page)
        self.assertIn("pywebview.api.pick_root()", page)
        self.assertIn("pywebview.api.choose_root(", page)
        self.assertIn('case "root":', page)

    def test_the_menu_never_interpolates_a_path_into_html(self):
        """A directory may be named `<img onerror=...>`. Paths reach the page as
        textContent and dataset only -- this pins that they are not concatenated
        into the menu's HTML string."""
        page = crow_gui.PAGE
        menu = page[page.index("rootMenu(){"):page.index("chooseRoot(p){")]
        self.assertNotIn("+x.path+", menu.replace(" ", ""))
        self.assertNotIn("+x.name+", menu.replace(" ", ""))
        self.assertIn("textContent", menu)


class _AliveWorker:
    def is_alive(self):
        return True


class _PickingWindow:
    def __init__(self, result):
        self._result = result

    def create_file_dialog(self, *a, **k):
        return self._result


class _RefusingWindow:
    def create_file_dialog(self, *a, **k):
        raise RuntimeError("no dialog here")


class AReopenedChatKeepsItsToolRowsTests(unittest.TestCase):
    """#99. Switching chats redrew the thoughts and dropped every tool row."""

    def _drawn(self, messages):
        """The real `_replay`, bound to a stand-in that only records."""
        out = []

        class Recorder:
            def __init__(self):
                self._context_tokens = 0
                self._n_ctx = 0

            def push(self, message):
                out.append(message)

        crow_gui.Api._replay(Recorder(), messages)
        return out

    def test_a_turn_that_only_called_a_tool_is_not_skipped(self):
        """The message has no `content` at all -- the emptiness test used to
        drop it whole, which is why the reopened chat showed two thoughts with
        nothing between them."""
        drawn = self._drawn([{"role": "assistant", "content": "",
                              "reasoning_content": "let me look",
                              "tool_calls": [{"function": {
                                  "name": "web_search",
                                  "arguments": '{"query":"llama.cpp"}'}}]}])
        tools = [m for m in drawn if m["k"] == "tool"]
        self.assertEqual([t["name"] for t in tools], ["web_search"])
        self.assertIn("llama.cpp", tools[0]["args"])

    def test_two_calls_in_one_turn_keep_their_order(self):
        drawn = self._drawn([{"role": "assistant", "content": "",
                              "tool_calls": [
                                  {"function": {"name": "read_file",
                                                "arguments": '{"path":"a.py"}'}},
                                  {"function": {"name": "write_file",
                                                "arguments": '{"path":"b.py"}'}}]}])
        self.assertEqual([m["name"] for m in drawn if m["k"] == "tool"],
                         ["read_file", "write_file"])

    def test_written_code_is_what_the_row_carries(self):
        """The reason the rows matter: the code Crow wrote lives in a write_file
        argument and nowhere else in the transcript."""
        drawn = self._drawn([{"role": "assistant", "content": "",
                              "tool_calls": [{"function": {
                                  "name": "write_file",
                                  "arguments": '{"path":"C:/x/y.py","content":"def f(): pass"}'
                              }}]}])
        row = [m for m in drawn if m["k"] == "tool"][0]
        self.assertIn("y.py", row["args"])
        self.assertIn("def f(): pass", row["args"])

    def test_a_tool_result_is_not_drawn_as_an_answer(self):
        """NEGATIVE HALF. The `role: tool` payload is 16 KB of text the model
        already has. Drawing it would make a reopened chat longer than the live
        one it is supposed to reproduce."""
        drawn = self._drawn([{"role": "tool", "content": "x" * 5000}])
        self.assertEqual(drawn, [])

    def test_a_chat_without_tools_draws_what_it_always_did(self):
        """NEGATIVE HALF the other way: no tool call, no tool row, and the
        answer still comes through the fence renderer."""
        drawn = self._drawn([{"role": "user", "content": "hi"},
                             {"role": "assistant", "content": "```py\nx=1\n```"}])
        self.assertEqual([m for m in drawn if m["k"] == "tool"], [])
        self.assertTrue([m for m in drawn if m["k"] == "code_open"])

    def test_the_window_formats_arguments_instead_of_dumping_json(self):
        """The `hasattr` guard was never True: format_tool_args lived in
        cli/crow.py, so the window always took the raw-JSON fallback."""
        self.assertTrue(hasattr(crow_core, "format_tool_args"))
        out = []
        crow_gui.Turn(out.append).tool_started(
            "read_file", '{"path":"C:/very/long/path/to/a/file.py"}')
        row = [m for m in out if m["k"] == "tool"][0]
        self.assertNotIn('{"path"', row["args"])
        self.assertIn("path=", row["args"])


class TheLiveRateIsWallClockOnPurposeTests(unittest.TestCase):
    """The window's live tok/s counts the pauses, and that is the decision.

    It looks like the defect `crow_core.TurnCost` already fixed once -- "the
    first version divided tokens by the whole round and printed 1.49 tok/s for a
    turn the server had just measured at 14.77 and 16.46" -- and on 2026-08-14 it
    was changed to sum only the gaps between deltas, then changed straight back
    (robin, #97). What the user waits through is wall clock. The decode figure is
    the server's and lands underneath at the end of the turn; two figures with
    two meanings, both on screen.

    These cases exist so the next reading of that docstring does not turn into a
    second fix. They fail if the pauses ever stop counting.
    """

    def _rate(self, gaps):
        clock = [1000.0]
        real = crow_gui.time.monotonic
        crow_gui.time.monotonic = lambda: clock[0]
        try:
            out = []
            sink = crow_gui.Sink(out.append, live=True)
            sink.reply_started()
            for gap in gaps:
                clock[0] += gap
                sink.reasoning_text_counted()
            live = [m for m in out if m["k"] == "live"]
            self.assertTrue(live, "no live message was ever sent")
            return live[-1]["rate"]
        finally:
            crow_gui.time.monotonic = real

    def test_a_steady_stream_reports_its_throughput(self):
        """POSITIVE, and it has to hold before any claim about pauses means
        anything: fifty deltas 50 ms apart with nothing in between is 20 a
        second either way of measuring."""
        self.assertAlmostEqual(self._rate([0.05] * 50), 20.0, delta=1.0)

    def test_a_tool_call_lowers_the_figure_because_the_user_waited(self):
        """THE DECISION. The same fifty deltas with a fifteen-second web search
        between them: the model still generated at 20, and the user still waited
        32 s for 50 tokens. The window reports what was waited through."""
        self.assertLess(self._rate([0.05] * 25 + [15.0] + [0.05] * 25), 5.0)

    def test_the_pause_is_not_quietly_excluded(self):
        """NEGATIVE HALF, and the one that catches a well-meant repair: summing
        only the gaps between deltas lands back near 20 here. If this ever
        passes at 20, the decision was reverted without anyone deciding to."""
        with_pause = self._rate([0.05] * 25 + [15.0] + [0.05] * 25)
        without = self._rate([0.05] * 50)
        self.assertGreater(without - with_pause, 10.0,
                           "the pause stopped counting -- see #97 before changing this")



class TheReasoningSliderIsTheSameCommandTests(ApiCase):
    """#116, the window half. The terminal half is in test_crow.py, and BOTH
    exist for the reason the ticket names: #99 is the case where one surface was
    forgotten -- `format_tool_args` behind a hasattr guard that had been False
    since the split, so the feature worked in one client and not the other for
    months with nothing in the suite able to see it.
    """

    def _api(self, model="Qwen3.8-27B"):
        api = self.api()
        api._model = model
        return api

    def test_the_command_is_on_the_shared_list_and_described(self):
        self.assertIn("/reasoning", crow_core.SLASH_COMMANDS)
        self.assertIn("/reasoning", crow_gui.Api.WHAT_THEY_DO)
        self.assertNotIn("nothing here answers this yet", self.api().help_listing())

    def test_bare_reasoning_answers_rather_than_reaching_the_model(self):
        answer = self._api().slash_answer("/reasoning")
        self.assertIsNotNone(answer)
        self.assertIn("levels", answer)
        for level in crow_core.reasoning_levels_for("Qwen3.8-27B"):
            self.assertIn(level, answer)

    def test_setting_a_level_binds_it_and_states_the_prefill(self):
        api = self._api()
        answer = api.slash_answer("/reasoning medium")
        self.assertEqual(api._reasoning, "medium")
        self.assertIn(crow_core.REASONING_COST_NOTE, answer)

    def test_the_slider_types_the_command_rather_than_setting_the_level(self):
        """THE POINT OF THE WHOLE CLASS. A control wired separately from the
        command it duplicates is #99 all over again, so the slider goes through
        `_reasoning_command` and its refusal applies to both."""
        api = self._api()
        api.set_reasoning("high")
        self.assertEqual(api._reasoning, "high")
        api.set_reasoning("careful")
        self.assertEqual(api._reasoning, "high",
                         "the slider bypassed the refusal the command applies")

    def test_an_unknown_level_is_named_and_nothing_is_bound(self):
        """NEGATIVE PROOF. An invalid level does not fail here -- it fails on
        the server, after the prefill has been paid for."""
        api = self._api()
        api.slash_answer("/reasoning low")
        answer = api.slash_answer("/reasoning careful")
        self.assertIn("careful", answer)
        self.assertEqual(api._reasoning, "low")

    def test_off_returns_to_the_never_chosen_state(self):
        api = self._api()
        api.slash_answer("/reasoning high")
        api.slash_answer("/reasoning off")
        self.assertIsNone(api._reasoning)

    def test_a_change_tells_the_page_and_a_refusal_does_not(self):
        """The chip has to follow the value, and only when it moved: a push on
        every attempt would repaint the chip for a level that was refused."""
        api = self._api()
        api.slash_answer("/reasoning high")
        moved = [m for m in self.drained(api) if m.get("k") == "reasoning"]
        self.assertEqual([m["level"] for m in moved], ["high"])
        api.slash_answer("/reasoning careful")
        self.assertEqual([m for m in self.drained(api) if m.get("k") == "reasoning"], [])

    def test_the_page_has_a_case_for_the_message_it_is_sent(self):
        """#99's shape in the transport: a `k` the Python side pushes and the
        page has no case for is a message into a void."""
        self.assertIn("reasoning", _drawn_kinds(inspect.getsource(crow_gui)))

    def test_the_level_reaches_the_session_file_and_comes_back(self):
        api = self._api()
        api.slash_answer("/reasoning medium")
        api._conversation.append("user", "hello")
        api._conversation.append("assistant", "hi")
        crow_core.post_json = lambda url, body, timeout=0: {"n_saved": 3}
        api._persist_live()
        self.assertEqual(crow_core.session_reasoning(self.session), "medium")

    def test_a_level_the_new_model_refuses_is_dropped_and_said(self):
        """The model-switch criterion, at the level of the state: `max` is fine
        for 0731 and raises against unsloth's template."""
        with open(self.session, "w", encoding="utf-8") as fh:
            json.dump({"format_version": crow_core.SESSION_FORMAT,
                       "messages": [{"role": "user", "content": "x"}],
                       "reasoning": "max"}, fh)
        level, note = crow_core.reasoning_for_chat("Qwen3.8-27B", self.session)
        self.assertIsNone(level)
        self.assertIn("max", note)

    def test_the_level_names_are_not_interpolated(self):
        """The rootMenu rule, and the ticket names it: a level out of the
        manifest is text off the disk, so the page sets it with textContent.

        THE ELEMENT MOVED, THE RULE DID NOT (#117). This used to look for `#reasonlabel`, which
        was the label under the slider handle; the slider is gone and the names are now drawn as
        rows by reasonMenu. A level called `<img onerror=...>` still has to be DRAWN, not run.
        """
        source = inspect.getsource(crow_gui)
        self.assertIn('el.querySelector("b").textContent = name;', source)
        self.assertIn('el.querySelector(".what").textContent = bits.join', source)


class TheDictationButtonTests(unittest.TestCase):
    """The microphone: where it sits, and what it does with what it heard.

    THE FILE IS READ RATHER THAN DRIVEN, like the two classes above it: the
    button lives in a page this suite has no browser for, so the source it is
    built from is the evidence there is.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.code = _code_only(self.source)

    def _method(self, head: str) -> str:
        """One method of the page object, by its opening line.

        `  },` at two spaces closes a method and appears nowhere inside one --
        the bodies are indented four and six.
        """
        return self.code.split(head)[1].split("  },")[0]

    def test_the_mic_sits_between_the_level_and_the_arrow(self):
        """Where robin asked for it. #acts is a flex row with no ordering of its
        own, so source order IS the order on screen and there is nothing else to
        hold this to."""
        order = [self.code.index(m) for m in
                 ('id="mode" data-mode=', 'id="mic" onclick=', 'id="go" onclick=')]
        self.assertEqual(order, sorted(order),
                         "the microphone is not drawn between the level and the arrow")

    def test_a_dictation_lands_in_the_box(self):
        """POSITIVE. The text arrives as an event and is appended to whatever was
        already typed, so half a typed line survives a thought finished aloud.

        THE WRITING MOVED TO `attach` when the drop and the paste needed the same
        four lines; what this case is about is that a dictation still reaches it,
        and that whatever it reaches still writes."""
        self.assertIn("this.attach(e.text)", self._method("micState(e){"),
                      "a finished dictation does not reach the box")
        writer = self._method("attach(text){")
        self.assertIn("input.value", writer, "nothing is written into the box")
        self.assertIn("dispatchEvent", writer,
                      "the textarea is not told to regrow around the new text")

    def test_a_dictation_never_sends_itself(self):
        """NEGATIVE, and the one that matters. Whisper is wrong often enough that
        a dictation which submitted itself would be a message nobody could take
        back. The text goes in the box; a human presses the arrow."""
        block = self._method("micState(e){")
        for forbidden in ("pywebview.api.send", "crow.go()", "this.go()"):
            self.assertNotIn(forbidden, block,
                             "a finished dictation reaches %s" % forbidden)

    def test_recording_is_visible_without_reading_anything(self):
        """robin's words: you should be able to see that it is recording. A glow
        does that from across the room; a changed tooltip does not."""
        # AGAINST THE SOURCE, NOT self.code, and it is not sloppiness: _code_only
        # drops every line beginning with `#` as a Python comment, which takes
        # each CSS id selector with it. The two lines below are held against
        # self.code because neither starts with one.
        self.assertIn("#mic.rec{", self.source, "no recording state on the button")
        self.assertIn("@keyframes micglow", self.code,
                      "the recording state does not move")
        self.assertIn("animation:micglow", self.code,
                      "the glow is defined and never used")

    def test_the_button_has_exactly_two_states(self):
        """robin's rule: grey is snoozed, blue glow is recording. NEGATIVE,
        because the way this breaks is somebody ADDING a state. A third one was
        built once -- yellow, while the recogniser ran -- and cut on sight: a
        colour the user cannot act on is furniture with a colour."""
        self.assertNotIn("#mic.work", self.source, "a third button state is back")
        self.assertNotIn('"work"', self.source, "a third mic state is pushed")

    def test_recording_is_not_drawn_in_the_mute_colour(self):
        """Red is what every conference tool on this machine uses for MUTE. The
        first build painted recording red, so the one signal that had to be
        unambiguous said the opposite of what was happening."""
        rule = self.source.split("#mic.rec{")[1].split("}")[0]
        self.assertIn("var(--accent)", rule, "recording is not the accent colour")
        self.assertNotIn("var(--bad)", rule, "recording is drawn in the mute colour")

    def test_the_page_never_paints_itself_recording(self):
        """NEGATIVE for the state. The class comes back from Python once the
        stream is actually open: a button that reddened on click would lie for as
        long as it took PortAudio to refuse."""
        block = self._method("mic(){")
        self.assertNotIn("classList.add", block,
                         "the click paints the state instead of asking for it")

    def test_transcription_does_not_run_inside_the_bridge_call(self):
        """The first dictation loads 486 MB of model. A bridge call that did the
        work would hold the page's promise open for seconds with the button
        frozen between two states."""
        block = self.source.split("def dictate_stop(")[1].split("def _dictate_finish")[0]
        self.assertIn("threading.Thread", block, "the work is done on the bridge call")
        self.assertNotIn("crow_voice.stop()", block,
                         "dictate_stop transcribes before it returns")


class TheVoiceModuleTests(unittest.TestCase):
    """cli/crow_voice.py: what it promises on a machine that cannot deliver."""

    def test_a_missing_package_is_named_and_not_raised(self):
        """NEGATIVE. Both dependencies are optional the way pywebview is, so the
        answer to "no sounddevice" is a sentence the window can print -- not an
        exception that takes the click with it.

        `None` in sys.modules is what makes `import x` raise ImportError, which
        is the same shape as the package not being installed at all."""
        with mock.patch.dict(sys.modules, {"sounddevice": None}):
            why = crow_voice.available()
        self.assertIsInstance(why, str, "a missing package did not produce a reason")
        self.assertIn("sounddevice", why, "the reason does not name what is missing")

    def test_nothing_recorded_transcribes_to_nothing(self):
        """POSITIVE for the empty case, and it has to hold with NO optional
        package present: this used to import numpy first, which turned a button
        pressed twice by accident into an ImportError instead of a shrug."""
        crow_voice._blocks.clear()
        with mock.patch.dict(sys.modules, {"numpy": None, "faster_whisper": None}):
            self.assertEqual(crow_voice.stop(), "")

    def test_the_installer_and_the_client_name_one_directory(self):
        """TWO FILES DECIDE WHERE THE MODEL LIVES. If they drift, install.ps1
        fetches 486 MB into one directory and the window downloads the same bytes
        again because it looked in another."""
        ps = (HERE.parent / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('$WHISPER_DIRNAME   = "%s"' % crow_voice.MODEL_DIRNAME, ps,
                      "install.ps1 does not name the directory crow_voice.py reads")
        self.assertEqual(crow_voice.model_dir().parent.name, "models")
        self.assertEqual(crow_voice.model_dir().parent.parent,
                         Path(crow_voice.__file__).resolve().parent.parent,
                         "the model is not looked for beside the client")

class TheThemeAndTheSettingsSheetTests(unittest.TestCase):
    """Two palettes, one attribute, and the sheet that switches them."""

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def test_no_rule_carries_a_colour_of_its_own(self):
        """THE ONE THAT KEEPS A LIGHT MODE POSSIBLE, and it is a negative probe
        because this breaks by ADDING: one new rule with `color:#cfdaea` in it is
        a rule the light palette cannot reach, and nothing else would go red.

        Hex only. The rgba() tints left in the file are derived from the accent
        and the three state colours and read on both grounds; a flat hex does
        not."""
        import re

        palette, offenders = False, []
        for line in self.css.split("\n"):
            t = line.strip()
            if t.startswith(":root"):
                palette = True
            elif palette and t == "}":
                palette = False
            if palette or t.startswith(("/*", "*", "//")):
                continue
            if re.search(r"#[0-9a-fA-F]{6}\b", line):
                offenders.append(t[:80])
        self.assertEqual(offenders, [],
                         "a CSS rule names a colour the light palette cannot reach")

    def test_every_palette_defines_the_same_names(self):
        """A name defined in one theme and not the other is a colour that falls
        back to the dark value on a white ground -- invisible text, and only on
        the theme nobody develops in."""
        import re

        def names(block):
            return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

        base = names(self.css.split(":root{")[1].split("\n}")[0])
        # Structure rather than colour, and three deliberate carry-throughs: the
        # accent, the bevel and the model's own colour are brand values out of
        # the core, and a theme that redefined them would be inventing a second
        # brand rather than choosing a ground.
        structural = {"--mono", "--ui", "--barh", "--sbw"}
        carried = {"--accent", "--bevel", "--model"}
        for theme in ("light", "crow"):
            head = ':root[data-theme="%s"]{' % theme
            self.assertIn(head, self.css, "no palette for %s" % theme)
            got = names(self.css.split(head)[1].split("\n}")[0])
            missing = (base - got) - structural - carried
            self.assertEqual(missing, set(),
                             "the %s palette does not redefine %s"
                             % (theme, sorted(missing)))

    def test_the_theme_is_on_the_element_before_the_page_is_handed_over(self):
        """NOT applied by a script after load. A window that painted itself dark
        and then switched would show the wrong theme for a frame on every start,
        and the frame is exactly when somebody looks at it."""
        self.assertIn('<html lang="de" data-theme="__THEME__">', self.source)
        self.assertIn('.replace("__THEME__", current_theme())', self.source)

    def test_a_chosen_theme_comes_back(self):
        """PERSISTENCE IS A CONTRACT: whatever writes has to read in the same
        change. `set_theme` writes the file and `current_theme` is what the page
        is stamped from -- so the round trip is the test, not the write."""
        with tempfile.TemporaryDirectory() as tmp:
            before = crow_gui.SETTINGS_FILE
            crow_gui.SETTINGS_FILE = os.path.join(tmp, "settings.json")
            try:
                self.assertEqual(crow_gui.current_theme(), crow_gui.DEFAULT_THEME)
                api = crow_gui.Api.__new__(crow_gui.Api)
                self.assertTrue(api.set_theme("light"))
                self.assertEqual(crow_gui.current_theme(), "light")
                # NEGATIVE: a value this build does not have is refused, and the
                # refusal must not overwrite the one that works.
                self.assertFalse(api.set_theme("solarized"))
                self.assertEqual(crow_gui.current_theme(), "light")
            finally:
                crow_gui.SETTINGS_FILE = before

    def test_a_settings_file_that_is_rubbish_is_not_an_error(self):
        """NEGATIVE for the reader. A half-written file is a value this build
        does not have, and the answer to that is the value it does have."""
        with tempfile.TemporaryDirectory() as tmp:
            before = crow_gui.SETTINGS_FILE
            crow_gui.SETTINGS_FILE = os.path.join(tmp, "settings.json")
            try:
                io.open(crow_gui.SETTINGS_FILE, "w", encoding="utf-8").write("{oh no")
                self.assertEqual(crow_gui.current_theme(), crow_gui.DEFAULT_THEME)
            finally:
                crow_gui.SETTINGS_FILE = before

    def test_hilfe_sits_in_the_title_bar_and_leaves_the_drag_region(self):
        """The bar moves the window. Anything clickable in it has to opt out, or
        the click becomes a drag and the menu never opens."""
        self.assertIn('<div id="helpwrap" class="pywebview-no-drag">', self.source)
        self.assertIn('id="help" onclick="crow.helpMenu()"', self.source)
        mark = self.source.index('id="mark"')
        help_at = self.source.index('id="helpwrap"')
        wbtns = self.source.index('id="wbtns"')
        self.assertLess(mark, help_at, "Help is not drawn after the wordmark")
        self.assertLess(help_at, wbtns, "Help is drawn past the window buttons")

    def test_the_sheet_carries_the_three_categories(self):
        """Appearance, Skills, About -- named even where they are still empty,
        so the shape is visible before the contents are."""
        for cat in ("Appearance", "Skills", "About"):
            self.assertIn(">%s</button>" % cat, self.source,
                          "the settings sheet has no %s" % cat)
        for key in ("look", "skills", "about"):
            self.assertIn('data-cat="%s"' % key, self.source)

    def test_the_typeface_is_the_machine_own(self):
        """NEGATIVE. The shipped typeface must not be named by the page: a window
        that looks different depending on whether the user installed a font has
        two appearances and no way to say which is the real one."""
        # THE DECLARATIONS, NOT THE COMMENTS. The note above the stack names the
        # typeface it replaced, and a test that could not tell the two apart
        # would forbid writing down why the change happened.
        rules = chr(10).join(l for l in self.css.splitlines()
                        if not l.strip().startswith(("/*", "*", "//")))
        self.assertNotIn("Google Sans Code", rules,
                         "the page still asks for the shipped typeface")
        self.assertIn("--ui:system-ui", self.css)

class TheDropAndThePasteTests(unittest.TestCase):
    """Files into the box: dropped from Explorer, pasted from the clipboard."""

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.code = _code_only(self.source)

    # -- what the page has to do -------------------------------------------

    def test_dragover_is_prevented(self):
        """THE ONE EVERYBODY FORGETS. Without preventDefault on dragover the
        drop never reaches a listener at all: WebView2 has already decided to
        navigate to the file, and the window shows a picture instead of a chat."""
        self.assertIn('document.addEventListener("dragover"', self.code)
        block = self.code.split('document.addEventListener("dragover"')[1][:120]
        self.assertIn("preventDefault", block, "dragover does not stop the navigation")

    def test_text_is_pasted_the_way_it_always_was(self):
        """NEGATIVE, and it is the one that protects everyday use. Pasting a
        path, a log or a stack trace must not reach the bridge at all: the
        handler returns before preventDefault, so the browser's own paste runs."""
        block = self.code.split('document.addEventListener("paste"')[1].split("});")[0]
        want = 'if(types.indexOf("text/plain") !== -1) return;'
        self.assertIn(want, block, "a text paste is not let through untouched")
        self.assertLess(block.index(want), block.index("preventDefault"),
                        "the handler stops the browser before it checks for text")

    def test_a_path_with_a_space_is_quoted(self):
        """A Windows path with a space in it is the normal case. Unquoted it is
        two arguments to whatever reads the line next."""
        for caller in ("this.attach(paths.map(", "crow.attach(/"):
            self.assertIn(caller, self.code)
        self.assertEqual(self.code.count('+p+'), 1, "the drop path is not quoted")
        self.assertIn("'\"'+path+'\"'", self.code, "the pasted path is not quoted")

    def test_one_place_writes_into_the_box(self):
        """Three callers -- dictation, drop, paste -- and one `attach`. Three
        copies of the same four lines would drift the day one of them is fixed."""
        self.assertEqual(self.code.count("  attach(text){"), 1)
        for caller in ("this.attach(e.text)", "this.attach(paths.map(", "crow.attach(/"):
            self.assertIn(caller, self.code, "%s does not go through attach" % caller)

    # -- what Python has to do ---------------------------------------------

    def test_the_dropped_path_comes_from_pywebview_not_the_page(self):
        """The page is handed a File with a name and no location; pywebview puts
        the real one on this side. A drop entry without it is skipped rather than
        pushed as an empty string -- NEGATIVE, because an empty path in the box
        looks like a bug in the drop and is a bug in the reading."""
        api = crow_gui.Api.__new__(crow_gui.Api)
        seen = []
        api.push = seen.append
        api.on_drop({"dataTransfer": {"files": [
            {"name": "a.md", "pywebviewFullPath": r"C:\tmp\a.md"},
            {"name": "nameless.png"},
        ]}})
        self.assertEqual(seen, [{"k": "drop", "paths": [r"C:\tmp\a.md"]}])

    def test_a_drop_of_nothing_does_not_crash(self):
        """NEGATIVE. pywebview hands over whatever the event carried, and a drop
        of selected text carries no files at all."""
        api = crow_gui.Api.__new__(crow_gui.Api)
        seen = []
        api.push = seen.append
        for event in (None, {}, {"dataTransfer": None}, {"dataTransfer": {"files": None}}):
            api.on_drop(event)
        self.assertEqual(seen, [{"k": "drop", "paths": []}] * 4)

    def test_a_pasted_picture_is_written_and_named(self):
        """POSITIVE, and the round trip is the test: the path that comes back has
        to be a file that is really there, with the bytes that went in."""
        with tempfile.TemporaryDirectory() as tmp:
            before = crow_gui.PASTE_DIR
            crow_gui.PASTE_DIR = os.path.join(tmp, "pastes")
            try:
                raw = b"\x89PNG\r\n\x1a\n" + b"x" * 40
                path = crow_gui.write_paste(".png", raw)
                self.assertTrue(path, "nothing came back")
                self.assertTrue(path.endswith(".png"))
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(), raw)
                # THE SECOND PASTE IN THE SAME SECOND is not a rare case when the
                # clipboard is a keyboard shortcut. It must not eat the first.
                second = crow_gui.write_paste(".png", raw)
                self.assertNotEqual(second, path, "the second paste overwrote the first")
                self.assertTrue(os.path.isfile(path))
            finally:
                crow_gui.PASTE_DIR = before

    def test_nothing_and_too_much_are_both_refused(self):
        """NEGATIVE. Each returns "" and leaves the directory empty -- an empty
        answer is what the page checks before it writes into the box."""
        with tempfile.TemporaryDirectory() as tmp:
            before = crow_gui.PASTE_DIR
            crow_gui.PASTE_DIR = os.path.join(tmp, "pastes")
            try:
                self.assertEqual(crow_gui.write_paste(".png", b""), "")
                too_big = b"x" * (crow_gui.PASTE_MAX_BYTES + 1)
                self.assertEqual(crow_gui.write_paste(".png", too_big), "")
                self.assertFalse(os.path.isdir(crow_gui.PASTE_DIR) and
                                 os.listdir(crow_gui.PASTE_DIR),
                                 "something was written that should not have been")
            finally:
                crow_gui.PASTE_DIR = before

    def test_an_empty_clipboard_is_an_empty_answer(self):
        """NEGATIVE for the bridge call. Most of what people paste is text, so
        "" is the ORDINARY result here and must not raise or write."""
        before = crow_gui.clipboard_image
        crow_gui.clipboard_image = lambda: None
        try:
            api = crow_gui.Api.__new__(crow_gui.Api)
            self.assertEqual(api.paste_clipboard(), "")
        finally:
            crow_gui.clipboard_image = before

    # -- the DIB arithmetic, which is the part that can be quietly wrong ----

    def _dib(self, bits, comp=0, used=0, size=40, payload=64):
        return struct.pack("<IiiHHIIiiII", size, 8, 8, 1, bits, comp,
                           0, 0, 0, used, 0) + b"p" * payload

    def test_the_pixel_offset_is_arithmetic_and_not_a_constant(self):
        """A DIB carries its palette and its colour masks BETWEEN the header and
        the pixels. An offset that ignored them opens as a picture of noise --
        which is worse than failing, because it looks like it worked.

        The four cases are the four shapes a screenshot can arrive in."""
        cases = [
            (self._dib(32), 54),                    # BI_RGB, no palette
            (self._dib(24), 54),                    # 24-bit, no palette
            (self._dib(8), 54 + 256 * 4),           # 8-bit, implied 256 entries
            (self._dib(8, used=16), 54 + 16 * 4),   # 8-bit, biClrUsed wins
            (self._dib(32, comp=3), 54 + 12),       # BI_BITFIELDS, three masks
        ]
        for dib, want in cases:
            out = crow_gui.dib_to_bmp(dib)
            self.assertEqual(out[:2], b"BM")
            total, _r1, _r2, offset = struct.unpack_from("<IHHI", out, 2)
            self.assertEqual(offset, want, "wrong pixel offset for %d-bit" % dib[14])
            self.assertEqual(total, len(out), "the size field does not match the file")
            self.assertEqual(out[14:], dib, "the DIB was altered on the way through")

    def test_a_dib_too_short_to_have_a_header_is_refused(self):
        """NEGATIVE. GlobalSize can hand back less than a header; unpacking that
        would raise inside a bridge call, where nobody sees it."""
        self.assertEqual(crow_gui.dib_to_bmp(b""), b"")
        self.assertEqual(crow_gui.dib_to_bmp(b"x" * 39), b"")

    def test_pasted_pictures_land_outside_the_working_directory(self):
        """A screenshot is not part of the project it is about. Writing one into
        whatever folder happens to be bound would put Crow's own files into a
        user's repository."""
        self.assertIn("os.path.dirname(crow_core.SESSION_DIR)", self.source)
        self.assertTrue(crow_gui.PASTE_DIR.endswith("pastes"))

class TheModelMenuSurvivesASwitchTests(ApiCase):
    """#115's menu, after the switch it exists to perform.

    THE DEFECT THIS CLASS IS CUT FOR: two places pushed `models` and they pushed
    two different shapes. The probe sent `[key, label]` pairs; the switch sent
    bare keys. The page keeps ONE `this.models` and the last payload wins, so the
    first switch replaced pairs with strings -- and `modelMenu`, which indexes
    x[0] and x[1], drew the second LETTER of each key. `operating-point` became
    `p`, `qwen35-q4-k-xl` became `w`, the running row said "restarts the server"
    because "o" never equals "operating-point", and a click sent "o" into
    `choose_model`, where `model_command` refused it as a typo. Nothing on disk
    went red: no test named the shape.
    """

    def _switched(self, api):
        """Drive `/model <other>` with the boot stubbed out, and hand back the
        `up` payload the page would have received."""
        with mock.patch.object(crow_core, "model_command",
                               return_value=("up", "http://127.0.0.1:2/v1", True)), \
             mock.patch.object(crow_gui, "fetch_model_name", return_value="m"), \
             mock.patch.object(crow_gui, "model_display_name", return_value="m"), \
             mock.patch.object(crow_gui, "fetch_n_ctx", return_value=1000):
            api._model_command(["qwen35-q4-k-xl"])
        ups = [m for m in self.drained(api) if m.get("k") == "up" and "models" in m]
        self.assertTrue(ups, "the switch told the page nothing about the models")
        return ups[-1]

    def test_the_switch_sends_pairs_not_bare_keys(self):
        """Every row the menu draws needs BOTH halves: the key goes into dataset
        and comes back on the click, the label is what a person recognises."""
        rows = self._switched(self.api())["models"]
        self.assertEqual(rows, [[k, crow_core.model_label(k)]
                                for k in crow_core.bootable_models()])
        for row in rows:
            self.assertEqual(len(row), 2, "a row that is not a pair draws letters")
            self.assertIn(row[0], crow_core.bootable_models())
            self.assertNotEqual(row[0], row[1],
                                "the key is the table's word, the label is the model's")

    def test_the_key_that_comes_back_is_one_the_table_knows(self):
        """THE CONSEQUENCE THE LABELS HID. A wrong shape does not just misspell
        the row -- it puts x[0] into dataset.k, and a single letter is refused by
        `model_command` as a typo, so the menu stops switching entirely."""
        for key, _label in self._switched(self.api())["models"]:
            said, _url, switched = crow_core.model_command(
                key, "http://127.0.0.1:1/v1")
            self.assertNotIn("no model", said,
                             "the menu would send a key the table refuses")
            del switched

    def test_a_bare_key_producer_would_be_caught(self):
        """NEGATIVE PROBE for the two above, and the guard against a THIRD
        producer: the positives only see the switch, so they would stay green
        while a new `push` somewhere else re-introduced the bare list."""
        source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        pairs = len(re.findall(r'"models":\s*\[\[k,', _code_only(source)))
        total = len(re.findall(r'"models":', _code_only(source)))
        self.assertEqual(pairs, total,
                         "a place that pushes models does not push pairs")
        salted = _code_only(source) + '\n"models": list(bootable_models()),\n'
        self.assertNotEqual(len(re.findall(r'"models":\s*\[\[k,', salted)),
                            len(re.findall(r'"models":', salted)),
                            "the check cannot see a bare-key producer")


class TheComposerControlsAreOneHeightTests(unittest.TestCase):
    """The four controls in #acts -- folder, level, microphone, arrow.

    THEY SAT AT FOUR HEIGHTS because each was as tall as whatever it held: the
    folder and the level are an 11.5px line box at the inherited 1.55 factor
    (25.83px once padding and border are counted), the arrow is 14px at a 1.2
    line-height (22.8), and the microphone is an SVG that declares height="13"
    (19). `align-items:center` let every one of them keep its own number.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def rule(self, selector: str) -> str:
        """The declarations of one rule, by its exact selector. Against the CSS
        slice of `source` and never against `_code_only`: that helper drops every
        line starting with `#`, which is every id selector in this file."""
        m = re.search(r"(?m)^%s\{(.*?)\}" % re.escape(selector), self.css, re.S)
        self.assertIsNotNone(m, "no rule for %s" % selector)
        return m.group(1)

    def test_the_row_stretches_so_one_height_reaches_all_four(self):
        """`stretch` is the initial value of align-items; the row gets ONE height
        from its tallest control and the rest adopt it. Pinning a px number on
        each button instead would go stale the first time a font-size moves."""
        acts = self.rule("#acts")
        self.assertIn("align-items:stretch", acts)
        self.assertNotIn("align-items:center", acts,
                         "center is what let the four keep four heights")

    def test_the_stretch_reaches_the_buttons_inside_their_wrappers(self):
        """#root and #mode are not children of #acts -- their menu wrappers are.
        A stretch that stops at a transparent div moves nothing."""
        for selector in ("#rootwrap", "#modewrap"):
            body = self.rule(selector)
            self.assertIn("display:flex", body,
                          "%s does not pass the stretch on" % selector)
            self.assertIn("position:relative", body,
                          "%s must stay the menu's containing block" % selector)

    def test_the_glyph_stays_on_the_centre_line_once_stretched(self):
        """A stretched button grows at the BOTTOM. #mic was already a centred
        flex box; #go was not, and its arrow would have ridden high."""
        self.assertIn("align-items:center", self.rule("#go"))
        self.assertIn("align-items:center", self.rule("#mic"))

    def test_the_hint_is_the_one_child_that_does_not_stretch(self):
        """NEGATIVE SIDE of the stretch: it is a bare word with no border to line
        up, and a stretched span puts its text at the top of the box."""
        self.assertIn("align-self:center", self.rule("#hint"))

    def test_no_control_pins_a_height_of_its_own(self):
        """The whole point of the fix. A `height:` on any of the four would be
        the number that disagrees with the row the next time the font moves."""
        for selector in ("#root", "#mode", "#mic", "#go"):
            self.assertIsNone(re.search(r"(?<![-a-z])height:\s*\d", self.rule(selector)),
                              "%s pins its own height" % selector)


class TheBarLostThreeChipsAndTheMenuGainedASubmenuTests(unittest.TestCase):
    """#119: what the status bar carries, and where the model is chosen.

    THE FILE IS READ RATHER THAN DRIVEN, like the dictation and theme classes:
    the suite has no browser for this page, so the source it is built from is
    the evidence there is. What that buys is still real -- every claim below is
    about WHERE a control is written, and a control written in the wrong block
    is drawn in the wrong place.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]
        body = self.source[self.source.index("</style>"):]
        # #125. THE STATUS BAR IS GONE, so `bar` is the title ribbon that is left
        # and `server` is where its two chips moved. The claims below did not
        # change -- only the block they are made about, which is the point of
        # slicing by marker rather than by line.
        self.bar = body[body.index('<div id="bar"'):body.index('<div id="body">')]
        self.server = body[body.index('<section data-cat="server"'):
                           body.index('<section data-cat="skills"')]
        self.composer = body[body.index('<div id="composer">'):body.index("<script>")]

    def test_the_bar_is_gone_and_nothing_was_left_hidden(self):
        """#125. An empty bar is a band of nothing between the ribbon and the
        first line of the chat, so it was removed rather than emptied -- and
        with it the rule it drew, which was half the seam."""
        self.assertNotIn('id="status"', self.source)
        self.assertNotIn("#status{", self.css)
        self.assertNotIn('id="right"', self.source)

    def test_the_bar_carries_neither_the_address_nor_the_window_size(self):
        """Both were chips that said something better said elsewhere: the URL is
        looked up when something is wrong, and n_ctx is already the denominator
        the composer prints."""
        self.assertNotIn('id="url"', self.bar)
        self.assertNotIn('id="nctx"', self.bar)
        self.assertNotIn("n_ctx", self.bar)

    def test_the_address_survives_as_the_connected_chip_s_title(self):
        """REMOVED IS NOT THE SAME AS GONE. Dropping the chip without keeping the
        address anywhere would take away the one fact worth having when the dot
        goes red."""
        self.assertIn('id="conn"', self.server)
        self.assertIn('$("#conn").title=e.url;', self.source)
        self.assertIn("#conn{cursor:help}", self.css,
                      "a native title with no cursor hint is a fact nobody finds")

    def test_the_model_is_chosen_in_the_composer_and_not_in_the_bar(self):
        """robin's placement: beside the number it decides. The window size the
        context is measured against comes from the model."""
        self.assertIn('id="model"', self.composer)
        self.assertIn('id="modelmenu"', self.composer)
        self.assertNotIn('id="model"', self.bar)
        self.assertNotIn('id="modelmenu"', self.bar)

    def test_the_menu_opens_upwards_now_that_it_sits_at_the_bottom(self):
        """The same reason #modemenu and #rootmenu do: a menu that opened
        downwards from the composer would be drawn past the window edge."""
        rule = re.search(r"(?m)^#modelmenu\{(.*?)\}", self.css, re.S)
        self.assertIsNotNone(rule, "no #modelmenu rule")
        self.assertIn("bottom:calc(100% + 6px)", rule.group(1))
        self.assertNotIn("top:calc", rule.group(1),
                         "downwards is what put it past the edge")

    def test_there_is_one_panel_and_the_second_one_is_not_renamed(self):
        """NEGATIVE PROBE for the merge: a `#reasonmenu` left anywhere -- element,
        rule or handler -- means the two panels still exist and only one of them
        is reachable, which is worse than two that both are."""
        self.assertNotIn("#reasonmenu", self.source)
        self.assertNotIn("reasonMenu(", self.source)
        self.assertNotIn('id="reasoning"', self.source)

    def test_the_levels_hang_only_under_the_running_model(self):
        """THE ONE INVARIANT THE MERGE COULD HAVE LOST. `levels` and `groups`
        describe the model that ANSWERED the probe; drawing them under the other
        row would name steps nobody measured there, and a click on one would put
        17 GB on the card for a setting."""
        plan = self.source[self.source.index("modelPlan(){"):
                           self.source.index("modelMenu(){")]
        guard = plan.index("if(!running || !(this.levels||[]).length) return;")
        self.assertLess(guard, plan.index('kind:"level"'),
                        "the level rows are pushed before the guard that limits them")
        self.assertIn('const running = (x[0] === this.modelKey);', plan,
                      "running is decided by the key the probe reported, not by order")

    def test_the_chip_names_the_model_and_what_it_does(self):
        """#117 SURVIVES THE MERGE. `high` means somebody chose it; `high
        (default)` means nothing was chosen and the template lands there anyway.
        The word is six characters and it is the entire finding."""
        self.assertIn('c.querySelector("b").textContent = this.modelName;', self.source)
        self.assertIn('c.querySelector(".lvl").textContent = this.levelLabel();',
                      self.source)
        self.assertIn('" (default)"', self.source)

    def test_the_chip_borrows_the_shape_of_its_new_neighbours(self):
        """The rule the microphone was built under: a neighbour with its own
        radius and padding reads as an accident. #root and #mode are 6px and
        3px/11px at 11.5px, and this control now stands beside them."""
        rule = re.search(r"(?m)^#model\{(.*?)\}", self.css, re.S)
        self.assertIsNotNone(rule, "no #model rule")
        for decl in ("border-radius:6px", "padding:3px 11px", "font-size:11.5px"):
            self.assertIn(decl, rule.group(1),
                          "the chip keeps the bar's pill shape in the composer")

    # -- a click elsewhere closes every popup, not two of the five -----------

    def test_every_popup_is_in_the_dismiss_table(self):
        """A menu that stays open when you look away is the one the last click
        left behind. Two of the five closed on an outside click and three did
        not, so the window had two behaviours and no rule for which was which."""
        table = self.source[self.source.index("const DISMISS = ["):
                            self.source.index("window.addEventListener(\"mousedown\"")]
        for wrap, menu in (("#helpwrap", "#helpmenu"), ("#modelwrap", "#modelmenu"),
                           ("#modewrap", "#modemenu"), ("#rootwrap", "#rootmenu")):
            self.assertIn('["%s","%s"]' % (wrap, menu), table,
                          "%s does not close on a click elsewhere" % menu)

    def test_the_guard_is_the_wrapper_and_never_the_panel(self):
        """THE TRAP THIS PAIRING EXISTS FOR. Guarding on the panel alone closes
        the menu on the mousedown that lands on its own chip, and the click a
        moment later finds it hidden and toggles it back open -- so the chip
        could open a menu and never close it.

        Both halves are asserted: every guard is a *wrap, and every wrapper
        really contains the panel it is paired with in the page."""
        table = self.source[self.source.index("const DISMISS = ["):
                            self.source.index("window.addEventListener(\"mousedown\"")]
        for wrap in re.findall(r'\["(#\w+)","#\w+"\]', table):
            self.assertTrue(wrap.endswith("wrap"),
                            "%s is not a wrapper -- the chip would re-open it" % wrap)
        body = self.source[self.source.index("</style>"):]
        for wrap, menu in re.findall(r'\["#(\w+)","#(\w+)"\]', table):
            start = body.index('id="%s"' % wrap)
            self.assertLess(start, body.index('id="%s"' % menu),
                            "%s is not inside %s in the page" % (menu, wrap))

    def test_the_table_names_every_menu_the_page_has(self):
        """NEGATIVE PROBE, and it breaks by ADDING: a fifth wrapper-and-panel
        pair introduced later is a fifth menu that stays open, and nothing else
        on disk would go red about it."""
        body = self.source[self.source.index("</style>"):]
        wraps = {m for m in re.findall(r'id="(\w+wrap)"', body)}
        table = self.source[self.source.index("const DISMISS = ["):
                            self.source.index("window.addEventListener(\"mousedown\"")]
        listed = {m for m in re.findall(r'\["#(\w+)","#\w+"\]', table)}
        self.assertEqual(sorted(wraps - listed), [],
                         "a wrapper in the page is not in the dismiss table")


class ProjectsInTheRailTests(ApiCase):
    """#119: chats grouped by the directory they are bound to.

    DRIVEN, NOT READ, wherever the answer is Python's. The grouping itself is
    the page's arithmetic and is held by the class below this one; everything
    here -- what a rail payload carries, what a move writes into a chat file,
    what comes back after a restart -- runs.
    """

    def setUp(self) -> None:
        super().setUp()
        self._roots = (crow_core.ROOTS_FILE, crow_gui.SETTINGS_FILE)
        self.addCleanup(self._put_back)
        crow_core.ROOTS_FILE = os.path.join(self.dir, "roots.json")
        crow_gui.SETTINGS_FILE = os.path.join(self.dir, "settings.json")

    def _put_back(self) -> None:
        (crow_core.ROOTS_FILE, crow_gui.SETTINGS_FILE) = self._roots

    def project(self, name: str) -> str:
        path = os.path.join(self.dir, name)
        os.makedirs(path, exist_ok=True)
        crow_core.add_project(path)
        return path

    def chat(self, name: str, root: str | None = "", messages: int = 1) -> str:
        """A chat file on disk. `root=""` writes no key -- nobody ever chose."""
        path = os.path.join(self.dir, "chat-%s.json" % name)
        data = {"format_version": crow_core.SESSION_FORMAT,
                "crow_title": name,
                "messages": [{"role": "user", "content": "x"}] * messages}
        if root != "":
            data["crow_root"] = root
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def rail_of(self, api) -> dict:
        api._reload_rail()
        rails = [m for m in self.drained(api) if m.get("k") == "rail"]
        self.assertTrue(rails, "the window told the page no rail")
        return rails[-1]

    # -- what the rail payload carries --------------------------------------

    def test_the_rail_carries_the_projects_and_every_chat_s_root(self):
        """ONE PAYLOAD, ONE GROUPING. A rail drawn against a project list that
        arrived on its own message would, for one frame, put a chat under a
        heading that is not there."""
        root = self.project("Crow")
        self.chat("eins", root)
        msg = self.rail_of(self.api())
        self.assertEqual([p["name"] for p in msg["projects"]], ["Crow"])
        entry = [r for r in msg["rollovers"] if r["title"] == "eins"][0]
        self.assertEqual(os.path.normcase(entry["root"]), os.path.normcase(root))

    def test_a_chat_carries_no_project_key_of_its_own(self):
        """THE INVARIANT THE WHOLE DESIGN RESTS ON. A label beside the directory
        is a second place for one fact, and the two part company the first time
        either is written alone. Membership is the boundary, or it is nothing."""
        root = self.project("Crow")
        path = self.chat("eins", root)
        self.rail_of(self.api())
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertNotIn("crow_project", data)
        self.assertIn("crow_root", data)
        # `_code_only`, because this file explains in prose that the key does
        # not exist -- and a sentence saying so is not a place that writes one.
        self.assertNotIn("crow_project", _code_only(
            (HERE / "crow_gui.py").read_text(encoding="utf-8")))

    def test_the_project_name_is_the_folder_s(self):
        """Nowhere else it could come from without a second key to hold it -- and
        a rename in the file manager reaches the rail with nothing told."""
        root = self.project("Obsidian-Vault")
        msg = self.rail_of(self.api())
        self.assertEqual(msg["projects"][0]["name"], os.path.basename(root))

    # -- moving one chat -----------------------------------------------------

    def test_moving_a_chat_writes_the_root_into_its_own_file(self):
        root = self.project("Crow")
        path = self.chat("eins")
        self.api().set_chat_root(path, root)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["crow_root"], root)

    def test_leaving_a_project_writes_null_and_not_a_missing_key(self):
        """#101's three states. An absent key means nobody ever chose here, and
        collapsing that with an explicit "no folder" is how a decision to be
        unbounded comes back as a folder on the next read."""
        root = self.project("Crow")
        path = self.chat("eins", root)
        self.api().set_chat_root(path, "")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("crow_root", data)
        self.assertIsNone(data["crow_root"])

    def test_moving_a_chat_keeps_everything_else_in_its_file(self):
        """NEGATIVE PROBE for the writer: a read-modify-write that missed would
        take the conversation with it, and a rail that still listed the title
        would look entirely healthy."""
        root = self.project("Crow")
        path = self.chat("eins", messages=3)
        self.api().set_chat_root(path, root)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data["messages"]), 3)
        self.assertEqual(data["crow_title"], "eins")
        self.assertEqual(data["format_version"], crow_core.SESSION_FORMAT)

    def test_a_chat_that_is_gone_is_reported_and_not_raised(self):
        api = self.api()
        api.set_chat_root(os.path.join(self.dir, "weg.json"), self.project("Crow"))
        # THE MESSAGE, NOT MERELY A `fail`. Without the guard the open() below
        # raises into the broad except and reports "could not write that chat",
        # which is also a fail -- so a test that only counted the kind could not
        # tell the guard from the crash it exists to prevent.
        said = [m["t"] for m in self.drained(api) if m.get("k") == "fail"]
        self.assertEqual(said, ["that chat is gone"])

    def test_a_directory_that_is_gone_is_refused(self):
        """NEGATIVE for the mover: binding a chat to a hole would hand it a
        boundary that does not exist, which is worse than no boundary."""
        api = self.api()
        path = self.chat("eins")
        api.set_chat_root(path, os.path.join(self.dir, "nirgends"))
        self.assertIn("fail", self.kinds(api))
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn("crow_root", json.load(fh))

    def test_moving_the_OPEN_chat_writes_the_root_into_its_file_too(self):
        """THE BUG robin SAW: the boundary bound, the note appeared, and the row
        stayed where it was.

        The live chat goes through `_bind_root`, which sets the boundary in
        MEMORY and marks it chosen -- but `crow_root` reached the file only on
        the next save. `_reload_rail` reads the file, so the rail was drawn from
        a copy that did not know yet. A window whose screen disagrees with its
        own disk is the state that is hardest to report, because both halves
        look right on their own.
        """
        root = self.project("Crow")
        path = self.chat("offen")
        api = self.api()
        api._current_path = path
        api._current_title = "offen"
        self.addCleanup(crow_core.set_root, None)
        api.set_chat_root(path, root)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh).get("crow_root"), root,
                             "the open chat's own file was never told")
        entry = [r for r in self.rail_of(api)["rollovers"]
                 if os.path.abspath(r["path"]) == os.path.abspath(path)][0]
        self.assertEqual(os.path.normcase(entry["root"]), os.path.normcase(root),
                         "the rail would draw it outside the project")

    def test_the_open_chat_leaving_a_project_is_written_too(self):
        """NEGATIVE HALF of the case above: the same gap in the other direction
        would leave a chat drawn inside a project it had just been taken out of."""
        root = self.project("Crow")
        path = self.chat("offen", root)
        api = self.api()
        api._current_path = path
        api._current_title = "offen"
        self.addCleanup(crow_core.set_root, None)
        crow_core.set_root(root)
        api._root_chosen = True
        api.set_chat_root(path, "")
        with open(path, encoding="utf-8") as fh:
            self.assertIsNone(json.load(fh).get("crow_root", "missing"),
                              "the open chat kept a boundary it was released from")

    # -- the three robin found on the built window ---------------------------

    def test_a_new_chat_starts_bound_to_nothing(self):
        """robin, on sight: "ein neuer Chat soll immer wurzellos sein".

        THIS OVERTURNS A DECISION #101 WROTE DOWN. `_adopt_chat_root(None)` fell
        back to the template in roots.json, and `active` is rewritten by every
        bind -- so moving one chat into a project made that project the ground
        every later chat started on. The template still answers at LAUNCH, which
        is the case #92 added it for; a new chat is no longer one of its callers.
        """
        root = self.project("Crow")
        api = self.api()
        self.addCleanup(crow_core.set_root, None)
        api._bind_root(root)                      # a move, which also sets active
        self.assertEqual(crow_core.get_root(), root)
        api.reset()
        self.assertIsNone(crow_core.get_root(),
                          "the new chat inherited the last project")
        self.assertFalse(api._root_chosen,
                         "unbound is not the same as chosen to be unbound")

    def test_the_launch_template_still_answers(self):
        """NEGATIVE HALF of the case above, and the reason it is a separate one:
        the fix must not reach `restore_root`. A window that came up bound to
        nothing would have thrown away #92 to fix a different event."""
        root = self.project("Crow")
        crow_core.set_active_root(root)
        self.addCleanup(crow_core.set_root, None)
        self.assertEqual(crow_core.restore_root()[0], root)
        api = self.api()
        api._adopt_chat_root(None)
        self.assertEqual(crow_core.get_root(), root)

    def test_binding_a_boundary_redraws_the_rail(self):
        """robin: moving a chat in only showed up after folding the project.

        A chat with no file goes through `choose_root`, and that door bound the
        boundary, printed the note and stopped. Nothing redrew, so the row sat
        where it was until some UNRELATED thing reloaded the rail -- folding a
        project, which is exactly what he found. It cost nothing while the
        boundary was invisible in the list; it costs from the moment the list is
        GROUPED by it.
        """
        root = self.project("Crow")
        api = self.api()
        self.addCleanup(crow_core.set_root, None)
        api.choose_root(root)
        self.assertIn("rail", self.kinds(api),
                      "the chat moved and the list was never told")

    def test_releasing_a_boundary_redraws_the_rail_too(self):
        """The other direction, and it has its own case because it is a
        different door: a chat taken out of a project must leave the group on
        the same click that took it out."""
        root = self.project("Crow")
        api = self.api()
        self.addCleanup(crow_core.set_root, None)
        api._bind_root(root)
        self.drained(api)
        api.clear_root()
        self.assertIn("rail", self.kinds(api))

    def test_the_live_chat_without_a_file_can_be_discarded(self):
        """robin: the new chat could not be deleted.

        It has no file, so `delete_chat` had nothing to remove -- the row armed,
        said "really delete?" and then did nothing at all, which is the one
        outcome worse than refusing. A chat that exists only in the window is
        still a chat somebody can want rid of.
        """
        api = self.api()
        api._current_path = None
        api._current_title = "neu"
        api._conversation.append("user", "hallo")
        self.assertTrue(api.discard_live())
        # THE SAME ARITHMETIC `_reload_rail` DOES: the system prompt is not a
        # turn, and `reset()` keeps it on purpose.
        spare = 1 if api._conversation.has_system else 0
        self.assertEqual(len(api._conversation) - spare, 0)
        self.assertIsNone(api._current_title)
        kinds = self.kinds(api)
        self.assertIn("clear", kinds)
        self.assertIn("rail", kinds)

    def test_discarding_refuses_when_the_chat_has_a_file(self):
        """NEGATIVE PROBE: this door drops a conversation WITHOUT archiving it,
        which is only safe for one that was never written. A chat with a file is
        `delete_chat`'s business, and letting this one answer for it would be a
        second way to lose a saved conversation."""
        path = self.chat("gespeichert")
        api = self.api()
        api._current_path = path
        self.assertFalse(api.discard_live())
        self.assertTrue(os.path.isfile(path))

    # -- what comes back after a restart ------------------------------------

    def test_the_rail_state_is_written_and_read_back(self):
        """PERSISTENCE IS A CONTRACT. A setting only ever written is one nobody
        has proved comes back -- the rule `set_theme` states, held here as a
        case rather than as a sentence."""
        api = self.api()
        self.assertTrue(crow_gui.rail_open(), "the default is open")
        self.assertTrue(api.set_rail_open(False))
        self.assertFalse(crow_gui.rail_open())
        self.assertTrue(api.set_rail_open(True))
        self.assertTrue(crow_gui.rail_open())

    def test_a_folded_project_is_written_and_read_back(self):
        root = self.project("Crow")
        api = self.api()
        self.assertTrue(self.rail_of(api)["projects"][0]["open"])
        api.set_project_open(root, False)
        self.assertFalse(self.rail_of(api)["projects"][0]["open"])
        api.set_project_open(root, True)
        self.assertTrue(self.rail_of(api)["projects"][0]["open"])

    def test_a_project_removed_and_added_again_comes_back_unfolded(self):
        """THE CLOSED ONES ARE THE LIST, so a new row is open like every other
        new row rather than carrying a state from a project that is gone."""
        root = self.project("Crow")
        api = self.api()
        api.set_project_open(root, False)
        api.drop_project(root)
        crow_core.add_project(root)
        self.assertTrue(self.rail_of(api)["projects"][0]["open"],
                        "it came back folded from a life it no longer has")

    def test_dropping_a_project_keeps_its_chats_where_they_are(self):
        """The row goes and nothing else does: the chats stay listed, still
        bound, and the marker stays on the directory."""
        root = self.project("Crow")
        path = self.chat("eins", root)
        api = self.api()
        api.drop_project(root)
        msg = self.rail_of(api)
        self.assertEqual(msg["projects"], [])
        # THE MARKER IS THE HALF THAT MATTERS. A boundary that disappeared
        # because a list was tidied is the failure the root mechanism exists to
        # prevent, and every chat here is still bound to this directory.
        self.assertTrue(os.path.isfile(crow_core.root_file(root)))
        entry = [r for r in msg["rollovers"] if r["title"] == "eins"][0]
        self.assertEqual(os.path.normcase(entry["root"]), os.path.normcase(root))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["crow_root"], root)


class TheEmptyChatSaysSomethingTests(ApiCase):
    """#119: the greeting under a chat with nothing in it.

    THE CLOCK IS INJECTED, so these are cases and not samples. `greeting` takes
    a timestamp for exactly this reason -- see its docstring on why it is not
    `random`.
    """

    def at(self, h: int, m: int = 0, name: str = "Robin") -> str:
        return crow_gui.greeting(time.mktime((2026, 8, 21, h, m, 0, 0, 0, -1)), name)

    def test_the_hour_decides_which_group_the_line_comes_from(self):
        """A line that says "Good morning" at eight in the evening is worse than
        no line at all."""
        self.assertEqual([crow_gui.daypart(h) for h in (0, 4, 5, 10, 11, 17, 18, 22, 23)],
                         ["night", "night", "morning", "morning", "day", "day",
                          "evening", "evening", "night"])
        self.assertIn("morning", self.at(8).lower())
        self.assertIn("evening", self.at(20).lower())

    def test_the_line_changes_without_the_hour_changing(self):
        """robin asked for it in as many words: not always the same one. Four
        consecutive minutes inside one group have to produce four lines."""
        lines = {self.at(8, m) for m in range(4)}
        self.assertEqual(len(lines), len(crow_gui.GREETINGS["morning"]))

    def test_the_same_minute_gives_the_same_line(self):
        """NEGATIVE HALF, and the reason `random` was refused: without this the
        only testable claim left is "it is one of four", which is not the
        behaviour anybody asked for and would hide a line that never varied."""
        # TWENTY, NOT TWO. A `random.choice` over four lines repeats itself one
        # time in four, so a pair of calls agrees a quarter of the time -- the
        # test would have passed on the very implementation it exists to refuse.
        first = self.at(8, 2)
        self.assertEqual({self.at(8, 2) for _ in range(20)}, {first})

    def test_the_name_is_the_login_s_first_part(self):
        """`DOMAIN\\robin` and `robin@host` are both logins somebody really has."""
        for raw in ("robin", "CORP\\robin", "robin@rechner", "robin.ludwig"):
            with mock.patch.object(crow_gui.getpass, "getuser", return_value=raw):
                self.assertEqual(crow_gui.user_first_name(), "Robin")

    def test_a_machine_that_will_not_say_still_gets_a_greeting(self):
        """NEGATIVE PROBE. "Hello, user." is worse than "Hello." -- and a
        greeting that came out as "Hello, ." would read as a defect."""
        with mock.patch.object(crow_gui.getpass, "getuser",
                               side_effect=OSError("no such user")):
            self.assertEqual(crow_gui.user_first_name(), "")
        for h in (8, 14, 20, 2):
            for m in range(4):
                line = crow_gui.greeting(
                    time.mktime((2026, 8, 21, h, m, 0, 0, 0, -1)), "")
                self.assertNotIn("%s", line)
                self.assertNotIn(" ,", line)
                self.assertNotIn(", .", line)
                self.assertTrue(line[:1].isupper(), line)
                # THE CLAIM THIS CASE IS ABOUT, and it was missing: the name is
                # DROPPED, not filled with a stand-in. Every check above passes
                # happily on "Hello, user." -- only the length says which of
                # the two happened.
                named = crow_gui.greeting(
                    time.mktime((2026, 8, 21, h, m, 0, 0, 0, -1)), "Robin")
                self.assertLess(len(line), len(named), line)

    def test_a_new_chat_is_greeted(self):
        api = self.api()
        api.reset()
        self.assertIn("hello", self.kinds(api))

    def test_a_launch_with_nothing_to_restore_is_greeted_too(self):
        """THE CALLER WITH NO CLEAR TO HANG ON, which is why the greeting is its
        own message rather than a field on `clear`."""
        api = self.api()
        with mock.patch.object(crow_gui, "load_session", return_value=None), \
             mock.patch.object(crow_gui, "check_endpoint", return_value="ok"), \
             mock.patch.object(crow_gui, "model_display_name", return_value="m"), \
             mock.patch.object(crow_gui, "fetch_model_name", return_value="m"), \
             mock.patch.object(crow_gui, "fetch_n_ctx", return_value=1000):
            api._probe()
        self.assertIn("hello", self.kinds(api))

    def test_the_first_turn_takes_the_line_back_off(self):
        """Hooked to `turn()`, the ONE place a row is appended -- not to the
        user's message. A chat restored from disk, a tool card and an error are
        three shapes that are not `user()` and all mean the chat is not empty."""
        source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.assertIn('turn(cls){ const g=$("#hello"); if(g) g.remove();', source)

    def test_the_line_is_drawn_and_never_run(self):
        """It carries a login name off the machine.

        THE CLAIM IS UNCHANGED, THE LINE MOVED. #127 put a drawing above the
        greeting, so the text is its own element inside the block -- which is
        why this reads the whole `hello()` body for `innerHTML` instead of
        matching one statement that a layout change can shift.
        """
        source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        hello = source[source.index("  hello(text){"):]
        hello = hello[:hello.index("  user(text){")]
        self.assertIn("p.textContent=text;", hello)
        self.assertNotIn("innerHTML", hello)

    def test_the_line_is_centred_by_the_box_and_not_by_a_number(self):
        """It stood at `16vh`, which is a guess about one window at one size.
        `min-height:100%` fills #flow's content box -- which already excludes
        the bottom padding the ResizeObserver reserves for the composer -- so
        the line sits on the middle of the space that is free, at any height."""
        source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        css = source[source.index("<style>"):source.index("</style>")]
        rule = re.search(r"(?m)^#hello\{(.*?)\}", css, re.S)
        self.assertIsNotNone(rule)
        body = rule.group(1)
        self.assertIn("min-height:100%", body)
        self.assertIn("align-items:center", body)
        self.assertIsNone(re.search(r"margin:\s*\d+vh", body),
                          "a tuned offset is a guess about one window size")


class TheRailDrawsTheGroupsTests(unittest.TestCase):
    """#119, the page's half: how the rail arranges what that payload carries.

    READ RATHER THAN DRIVEN, like every other page class here -- there is no
    browser in this suite. What that still buys is real: each case names a rule
    the arithmetic has to obey, and a rule deleted from the source is a rule
    nothing else on disk would miss.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]
        body = self.source[self.source.index("</style>"):]
        self.bar = body[body.index('<div id="bar"'):body.index('<div id="menu"')]
        self.rail = body[body.index('<aside id="rail">'):body.index('<div id="main">')]

    def test_the_fold_toggle_survives_the_rail_it_folds(self):
        """THE ONE CONTROL THAT MUST NOT LIVE IN THE THING IT HIDES. Inside the
        rail it would go away with it, and there would be no way back."""
        self.assertIn('id="railtoggle"', self.bar)
        self.assertNotIn('id="railtoggle"', self.rail)

    def test_the_fold_state_is_stamped_before_the_page_is_handed_over(self):
        """The theme's rule, for the same reason: a rail drawn open and folded
        by a script after load would do it on every start, and that frame is
        exactly when somebody is looking at the window."""
        self.assertIn('<body data-rail="__RAIL__">', self.source)
        self.assertIn('.replace("__RAIL__"', self.source)
        self.assertIn('body[data-rail="shut"] #rail{width:0', self.css)

    def test_a_folded_project_draws_no_rows_rather_than_hidden_ones(self):
        """Rows built and then hidden stay in the tree, and the fast path that
        moves the active mark would find a node nobody can see."""
        self.assertIn("if(p.open) mine.forEach(r=>box.appendChild(rowFor(r)));",
                      self.source)

    def test_folding_and_moving_are_both_in_the_rail_s_shape(self):
        """THE FAST PATH IS THE TRAP HERE. It skips the rebuild when the shape
        is unchanged, so a shape blind to roots and fold state would leave the
        list right in Python and stale on screen."""
        shape = self.source[self.source.index("const shape=(rollovers"):]
        shape = shape[:shape.index("if(box.dataset.shape===shape)")]
        self.assertIn('(r.root||"")', shape, "a move would not redraw")
        self.assertIn('p.open?"+":"-"', shape, "a fold would not redraw")

    def test_the_empty_space_below_the_chats_answers_a_right_click(self):
        """Otherwise the two things done least often -- start a chat, make a
        project -- are the two with no way in from the list."""
        self.assertIn('if(e.target.closest("#sessions")){ crow.railMenu(e); return; }',
                      self.source)
        plan = self.source[self.source.index("railPlan(kind,entry,archived){"):]
        plan = plan[:plan.index("menuDo(act,arg){")]
        for act in ('act:"newchat"', 'act:"newproj"', 'act:"dropproj"',
                    'act:"toproj"'):
            self.assertIn(act, plan)

    def test_no_menu_row_carries_a_snippet_of_code(self):
        """A project name is a folder name off the disk. The rows carry an ACTION
        NAME and are wired from the plan, so a folder called `'); alert('` is a
        label and cannot become anything else -- the modelMenu rule verbatim."""
        self.assertIn('el.querySelector("b").textContent=p.label;', self.source)
        self.assertIn("el.onclick=()=>crow.menuDo(p.act,p.arg);", self.source)
        menu = self.source[self.source.index("menu(e,kind,entry,row,archived){"):]
        menu = menu[:menu.index("closeMenu()")]
        self.assertNotIn("onclick=\\\"", menu,
                         "a handler is being interpolated into the row's HTML")

    def test_a_project_a_chat_is_already_in_is_not_offered(self):
        """A row that changes nothing reads as a row that failed."""
        self.assertIn(
            "const others=(this.projects||[]).filter(p=>!this.sameDir(p.path,entry.root));",
            self.source)

    def test_membership_is_an_exact_match_and_not_an_ancestor_walk(self):
        """`find_root` takes the NEAREST marker and not the highest, so a
        sub-directory that declares itself is its own root. The page compares
        the whole path, trailing separator and case folded, and nothing else."""
        fn = self.source[self.source.index("sameDir(a,b){"):]
        fn = fn[:fn.index("projectOf(root)")]
        self.assertIn("toLowerCase()", fn)
        self.assertNotIn("indexOf", fn, "a prefix test is an ancestor walk")
        self.assertNotIn("startsWith", fn, "a prefix test is an ancestor walk")

    def test_no_two_methods_on_the_page_share_a_name(self):
        """THE BUG THIS CLASS DID NOT CATCH, and the reason it could not: the page
        is one object literal, so a second `menuPlan` silently replaced the first
        and the model chip called the context menu's planner with no arguments.
        Nothing threw until a click, and no source assertion about either method
        was false -- both strings were still in the file.

        Held here rather than by reading either method, because the failure is
        not in a method. It is in the pair.
        """
        page = self.source[self.source.index("const crow = {"):
                           self.source.index("const composer =")]
        names = re.findall(r"(?m)^  ([A-Za-z_][A-Za-z0-9_]*)\(", page)
        # `if(` and friends sit at this indent too and are not definitions.
        keywords = {"if", "for", "while", "switch", "catch", "return", "function"}
        names = [n for n in names if n not in keywords]
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [],
                         "a later definition silently replaces the earlier one")

    def test_the_wordmark_takes_its_colour_from_the_palette(self):
        """It was the accent in all three themes -- Crow's own blue, which is
        right on the dark blue ground it was drawn for and a coloured word
        floating on a neutral or a white one. robin: white on dark, dark on
        light, unchanged in `crow`.

        TWO NAMES, because only `crow` splits the O off; the other two set both
        to one value, which is what makes the word solid rather than holed."""
        rule = re.search(r"(?m)^#mark\{(.*?)\}", self.css, re.S)
        self.assertIsNotNone(rule)
        self.assertIn("color:var(--mark)", rule.group(1))
        self.assertNotIn("var(--accent)", rule.group(1))
        self.assertIn("#mark span{color:var(--mark-o)}", self.css)
        # THE HEX IS NEVER HERE. The palette test beside this one holds that as
        # a rule; naming it again is what makes THIS case about the wordmark.
        for name in ("--mark:", "--mark-o:"):
            self.assertEqual(self.css.count(name), 3,
                             "%s is not defined in all three palettes" % name)

    def test_a_project_heading_carries_more_weight_than_its_chats(self):
        """A heading and its children at one weight is a list with an indent,
        not a group."""
        proj = re.search(r"(?m)^\.proj \.t\{(.*?)\}", self.css, re.S)
        sess = re.search(r"(?m)^\.sess \.t\{(.*?)\}", self.css, re.S)
        self.assertIsNotNone(proj)
        self.assertIsNotNone(sess)
        self.assertIn("font-weight:600", proj.group(1))
        self.assertNotIn("font-weight", sess.group(1))

    def test_the_delete_still_takes_two_clicks_in_one_menu(self):
        """NEGATIVE SIDE of building the rows from a plan: `menuDo` shuts the
        panel before it dispatches, and delete had to be exempted or the gesture
        would have quietly become two right-clicks."""
        self.assertIn('if(act==="del") return this.deleteTarget(entry);',
                      self.source)
        self.assertIn('btn.dataset.armed="1";', self.source)
        # BOTH WORDS, because the armed label now names which of the two acts it
        # is: a file is deleted, a chat that was never written is discarded.
        self.assertIn('"really delete?"', self.source)
        self.assertIn('"really discard?"', self.source)


class TheHeldWriteBarTests(unittest.TestCase):
    """#128: the strip that pops up over the composer while a write waits."""

    def setUp(self):
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def _bar(self):
        return self.css[self.css.index("#pendbar{"):self.css.index("#pendbar[hidden]")]

    def test_it_wears_the_memory_line_own_look(self):
        """NOT A SECOND VISUAL LANGUAGE FOR THE SAME SUBJECT. `.memnote` announces
        a write that HAPPENED; this announces one that WANTS to. Same accent,
        same gradient, same halo -- a reader should not have to learn two
        appearances for one thing."""
        bar = self._bar()
        self.assertIn("var(--accent)", bar)
        self.assertIn("linear-gradient", bar)
        self.assertIn("@keyframes pendglow", self.css)

    def test_it_breathes_because_it_is_a_state(self):
        """`.memnote` settles after one pass because a save is over. A question
        is still true until it is answered, so this one does not stop."""
        bar = self._bar()
        self.assertIn("infinite", bar)
        self.assertNotIn("forwards", bar)

    def test_it_sits_over_the_composer_and_not_in_the_button_row(self):
        """THE ROW IS FOR CONTROLS THAT ARE ALWAYS THERE, and this is not one: it
        pops in when there is something to answer and leaves again. Asserted by
        position in the page, because "it looks right" is not something a file
        on disk can say."""
        body = self.source[self.source.index("</style>"):]
        self.assertLess(body.index('id="pendbar"'), body.index('id="line"'))
        self.assertNotIn('id="pendwrap"', body)

    def test_a_reader_who_asked_for_no_motion_still_sees_it(self):
        """Motion off, colour stays -- the strip is the only sign there is, so it
        may not vanish for the people who asked for fewer moving things."""
        block = self.css[self.css.index("#pendbar{"):]
        block = block[block.index("prefers-reduced-motion"):]
        block = block[:block.index("}}") + 2]
        self.assertIn("animation:none", block)
        self.assertIn("var(--accent)", block)

    def test_the_entries_are_drawn_and_never_interpolated(self):
        """A staged note is model-written text out of a conversation and may
        contain anything at all; the page draws it rather than running it."""
        js = self.source[self.source.index("pendState(items){"):
                         self.source.index("pendAnswer(yes)")]
        self.assertIn("textContent", js)

    def test_it_lies_behind_the_composer(self):
        """BEHIND, NOT ABOVE, and the two look the same in source order -- what
        separates them is the negative margin that tucks the tile under the box
        and the layer that lifts the box over it. Without the z-index the
        overlap happens the wrong way round and the tile sits ON the input."""
        bar = self._bar()
        self.assertIn("margin:0 auto -14px", bar)
        box = self.css[self.css.index("#box{position:relative"):]
        self.assertIn("z-index:1", box[:80])

    def test_collapsed_it_is_a_title_and_two_numbers(self):
        """A tile that printed every entry would cover the chat it lies behind.
        Collapsed it says what it is and how much is coming; the text is one
        click away."""
        js = self.source[self.source.index("pendState(items){"):
                         self.source.index("pendToggle(e)")]
        self.assertIn("Memory Consolidation", js)
        self.assertIn('class="plus"', js)
        self.assertIn('class="minus"', js)
        body = self.css[self.css.index("#pendbar .body{"):]
        self.assertIn("display:none", body[:60])

    def test_gained_is_green_and_lost_is_red(self):
        """THE VALUE, NOT THE NAME -- and the first version of this case is why.
        It asserted `var(--bad-text)` and passed while the minus rendered WHITE:
        `--bad-text` is #ffd9d4, a pale pink meant as text ON a red ground, and
        on this dark surface it is indistinguishable from the text colour. The
        name was right in the file and the colour was wrong on the screen.

        Same failure the light drawing had, and the same fix: resolve what the
        palette actually gives and look at it. A checker that compares colour
        names cannot see an invisible colour."""
        import re
        CLOSE = chr(125)

        def value(css, token):
            m = re.search(re.escape(token) + r":\s*(#[0-9a-fA-F]{6})", css)
            self.assertIsNotNone(m, "%s is not defined" % token)
            h = m.group(1).lstrip("#")
            return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))

        # The dark palette is the shipped default and the one robin looked at.
        dark = self.css[self.css.index("--bg:#181818"):]
        dark = dark[:dark.index(CLOSE)]
        used = {}
        for cls in ("plus", "minus"):
            rule = self.css[self.css.index("#pendbar .%s{" % cls):]
            m = re.search(r"color:var\((--[a-z-]+)\)", rule[:120])
            self.assertIsNotNone(m, "%s has no palette colour" % cls)
            used[cls] = m.group(1)

        r, g, b = value(dark, used["minus"])
        self.assertGreater(r, g + 40, "the minus is not red: %r" % (used["minus"],))
        self.assertGreater(r, b + 40, "the minus is not red: %r" % (used["minus"],))
        self.assertLess(min(g, b), 200,
                        "%s is near-white, not a red anyone can see" % used["minus"])

        r, g, b = value(dark, used["plus"])
        self.assertGreater(g, r + 40, "the plus is not green: %r" % (used["plus"],))

    def test_a_replace_counts_as_one_gained_and_one_lost(self):
        """NEGATIVE PROBE FOR THE COUNTER. `replace` is ONE entry and TWO
        changes; counting it once would understate what is about to happen to
        the file, and the tile's whole job is to say how much is coming."""
        js = self.source[self.source.index("pendState(items){"):
                         self.source.index("pendToggle(e)")]
        block = js[js.index('a === "replace"'):]
        self.assertIn("plus++", block[:60])
        self.assertIn("minus++", block[:60])

    def test_a_click_on_a_button_does_not_collapse_the_tile(self):
        """The buttons live inside the tile, so their click bubbles out through
        it. Without this guard answering the question would fold the thing shut
        on the way out -- and on a decline, fold away the only evidence of what
        was just discarded."""
        js = self.source[self.source.index("pendToggle(e){"):]
        self.assertIn('closest("button")', js[:160])

    def test_the_bar_goes_when_the_chat_goes(self):
        """The Python side drops the staged writes inside `forget_approvals`;
        without this the strip would keep glowing about notes that are gone."""
        clear = self.source[self.source.index('case "clear":'):]
        self.assertIn("pendState([])", clear[:200])


class TheMemoryLineTests(unittest.TestCase):
    """#122: the one sign a person gets that something was remembered.

    THERE IS NO APPROVAL GATE IN FRONT OF IT -- robin declined it on 2026-08-21
    -- so this line is not decoration. It is the whole of the user's control
    over an automatic writer, which is why several of these cases are about it
    being impossible to miss and impossible to switch off.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def test_the_event_reaches_the_page_as_its_own_kind(self):
        """Not a `note`. A note is grey because what notes say may be skimmed
        past, and this one may not be."""
        seen = []
        crow_gui.Turn(seen.append).memory_saved(["add memory", "add user"])
        self.assertEqual([m["k"] for m in seen], ["memory"])
        self.assertEqual(seen[0]["n"], 2)

    def test_the_line_is_never_a_turn(self):
        """NEGATIVE PROBE, and the expensive one: a line that slipped into the
        history would move the head of the next prompt and cost the full
        prefill the whole feature is built to avoid. `Turn` only queues."""
        seen = []
        turn = crow_gui.Turn(seen.append)
        turn.memory_saved(["add memory"])
        self.assertFalse(hasattr(turn, "_conversation"))
        self.assertTrue(all("role" not in m for m in seen))

    def test_the_page_draws_it_with_a_glow_that_settles(self):
        """`forwards` on both animations is the trick. A glow that kept pulsing
        would be a thing to switch off, and this line may not be switchable."""
        self.assertIn('case "memory": this.memory(e.t,e.n); break;', self.source)
        self.assertIn(".memnote{", self.css)
        self.assertIn("@keyframes memsweep", self.css)
        self.assertIn("@keyframes memglow", self.css)
        self.assertEqual(self.css.count("1 forwards"), 2)

    def test_a_reader_who_asked_for_no_motion_still_sees_it(self):
        """NEGATIVE PROBE for the animation: with motion off the row must keep
        its tint. Replacing the sweep with nothing would hide the only notice
        there is from exactly the people who asked for fewer moving things."""
        block = self.css[self.css.index("prefers-reduced-motion"):]
        block = block[:block.index("}}") + 2]
        self.assertIn("animation:none", block)
        self.assertIn("var(--accent)", block)

    def test_the_window_speaks_one_language_and_it_is_english(self):
        """robin, 2026-08-21: "wenn ich englisch mit crow rede, ist der output
        dann englisch ... es wird ja mehr englisch als deutsche user geben".

        THERE IS NO LOCALISATION TO FALL BACK ON. `locale`, `gettext` and
        `getdefaultlocale` appear zero times in all three modules, so a German
        line is not the German version -- it is the ONLY version, shown to
        everyone. Two of them had shipped: the greeting, and the memory notice.

        DOCSTRINGS ARE EXEMPT ON PURPOSE. They quote robin, in German, on why
        several of these decisions were made; a case that could not tell
        documentation from an interface would forbid its own evidence.
        """
        import ast

        tree = ast.parse(self.source)
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)                         and isinstance(first.value.value, str):
                    docs.add(id(first.value))
        german = set("äöüÄÖÜß")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str)                     and id(node) not in docs:
                hit = german & set(node.value)
                self.assertFalse(hit, "German in a user-facing string, line %d: %r"
                                      % (node.lineno, node.value[:60]))
        self.assertIn("Memory updated", self.source)
        self.assertIn("Good morning, %s.", self.source)

    def test_the_window_speaks_one_language_and_it_is_english(self):
        """robin, 2026-08-21: "es wird ja mehr englisch als deutsche user geben".

        THERE IS NO LOCALISATION TO FALL BACK ON. `locale`, `gettext` and
        `getdefaultlocale` appear zero times in all three modules, so a German
        line is not the German version -- it is the ONLY version, shown to
        everyone who installs this. Fourteen of them had shipped: the greeting,
        the memory notice, `Hilfe`, `Einstellungen`, `Aussehen`, the two theme
        buttons, the rail tooltip, the empty-skills line and five context-menu
        rows. They were found one screenshot at a time, which is why this case
        exists rather than another pair of eyes.

        DOCSTRINGS AND COMMENTS ARE EXEMPT ON PURPOSE. They quote robin, in
        German, on why several of these decisions were made; a case that could
        not tell documentation from an interface would forbid its own evidence.
        The first draft of this case did exactly that -- it also banned the word
        `gettext`, and the comment explaining that Crow has no `gettext` failed
        it.

        IT DOES NOT FORBID LOCALISATION EITHER. What it holds is that ONE
        language ships today. If a second one is ever built, this case is the
        one to rewrite, not to route around.
        """
        import ast

        for module in ("crow_gui.py", "crow.py", "crow_core.py"):
            text = (HERE / module).read_text(encoding="utf-8")
            tree = ast.parse(text)
            docs = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)) and node.body:
                    first = node.body[0]
                    if isinstance(first, ast.Expr)                             and isinstance(first.value, ast.Constant)                             and isinstance(first.value.value, str):
                        docs.add(id(first.value))
            german = set("äöüÄÖÜß")
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant)                         and isinstance(node.value, str) and id(node) not in docs:
                    self.assertFalse(german & set(node.value),
                                     "German in a user-facing string, %s:%d -- %r"
                                     % (module, node.lineno, node.value[:60]))
        self.assertIn("Memory updated", self.source)
        self.assertIn("Good morning, %s.", self.source)
        self.assertIn(">Help</button>", self.source)
        self.assertIn("<h2>Settings</h2>", self.source)

    def test_the_line_cannot_be_switched_off(self):
        """`--no-review` stops the WRITING; there is no flag that keeps the
        writing and hides the notice. A silent learner is a system nobody can
        correct."""
        self.assertIn("--no-review", self.source)
        for hidden in ("--no-memory-notice", "memory_notifications", "quiet_memory"):
            self.assertNotIn(hidden, self.source)


class TheBirdTests(unittest.TestCase):
    """#127: the drawing under the greeting, and the icon on the taskbar."""

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def test_both_drawings_ship_and_are_read_from_disk(self):
        """Two files rather than one recoloured by CSS: it is a wireframe with
        five stroke colours, and `currentColor` carries one."""
        for background in ("dark", "light"):
            self.assertTrue((HERE / crow_gui.MARK_FILES[background]).is_file(), background)
            self.assertIn("<svg", crow_gui.mark_svg(background))
        self.assertNotEqual(crow_gui.mark_svg("dark"), crow_gui.mark_svg("light"))

    def test_every_stroke_is_legible_on_the_ground_it_is_drawn_on(self):
        """MEASURED, NOT MATCHED AGAINST A LIST OF COLOURS, because the defect
        this exists for is a colour that was simply left behind: the light
        drawing shipped with 66 of its 118 strokes still carrying the DARK
        version's values, at 1.8:1 and 1.3:1 on white -- present in the file,
        invisible on the screen, and a case that compared literals would have
        passed while robin was looking at a blank space.

        3:1 is the floor for a graphic that has to be made out, not read.
        """
        import re

        def lum(colour):
            def channel(c):
                c = int(colour[c:c + 2], 16) / 255
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            return (0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5))

        def contrast(a, b):
            high, low = sorted((lum(a), lum(b)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        grounds = {"dark": ["#181818", crow_core.CROW_BG], "light": ["#ffffff"]}
        for background, on in grounds.items():
            strokes = set(re.findall(r'stroke="(#[0-9a-f]{6})"',
                                     crow_gui.mark_svg(background)))
            self.assertTrue(strokes, background)
            for stroke in sorted(strokes):
                for ground in on:
                    self.assertGreaterEqual(
                        contrast(stroke, ground), 3.0,
                        "%s on %s is %.1f:1" % (stroke, ground,
                                                contrast(stroke, ground)))

    def test_the_two_drawings_are_the_same_bird(self):
        """NEGATIVE HALF of the case above: the light one is a RECOLOURING, so
        every path must still be there. A version that dropped the strokes it
        could not colour would also pass a contrast check."""
        import re

        paths = lambda which: re.findall(r'd="([^"]+)"', crow_gui.mark_svg(which))
        self.assertEqual(paths("dark"), paths("light"))

    def test_a_missing_drawing_is_empty_and_not_an_error(self):
        """NEGATIVE PROBE. A greeting without a bird is a greeting; a window
        that refused to open because a decoration was absent is not a window."""
        self.assertEqual(crow_gui.mark_svg("gibtsnicht"), "")

    def test_the_page_carries_both_and_the_stylesheet_picks(self):
        """No JavaScript and no second copy of which theme is live. Dark is the
        default because two of the three themes are dark; only `light` swaps."""
        self.assertIn("__MARKDARK__", self.source)
        self.assertIn("__MARKLIGHT__", self.source)
        self.assertIn(".mk-light{display:none}", self.css)
        self.assertIn(':root[data-theme="light"] .mk-dark{display:none}', self.css)

    def test_the_drawing_is_cloned_and_never_moved(self):
        """A `<template>` and `cloneNode`. Moved, the second greeting of a
        session would find the drawing gone -- and `hello()` builds its block
        fresh every time it is called."""
        self.assertIn('<template id="marktpl">', self.source)
        self.assertIn("t.content.cloneNode(true)", self.source)

    def test_the_icon_ships_and_is_found_beside_the_client(self):
        """A path built from `__file__`, so an installed copy finds its own."""
        self.assertTrue(os.path.isfile(crow_gui.ICON_FILE))
        self.assertTrue(crow_gui.ICON_FILE.endswith("crow.ico"))
        with open(crow_gui.ICON_FILE, "rb") as fh:
            self.assertEqual(fh.read(4), b"\x00\x00\x01\x00", "not an ICO")

    def test_the_application_id_is_set_before_the_window_exists(self):
        """Without one the taskbar groups this under the interpreter and draws
        Python's icon over Crow's name. The shell reads the id when it registers
        the button and never looks again -- the same rule the style bits hit."""
        main = self.source[self.source.index("    api = Api(args)"):]
        main = main[:main.index("webview.start(")]
        self.assertIn("taskbar_identity()", main)
        self.assertLess(main.index("taskbar_identity()"),
                        main.index("webview.create_window("))

    def test_the_icon_rides_the_search_that_already_found_the_window(self):
        """Finding the window is the hard half -- four things had to be right at
        once -- and a second EnumWindows is a second chance to pick the HELPER
        window, which looks like a working icon on a window nobody sees."""
        buttons = self.source[self.source.index("def shell_buttons"):]
        buttons = buttons[:buttons.index("\ndef ", 10)]
        self.assertIn("set_icon(hwnd)", buttons)
        self.assertEqual(self.source.count("EnumWindows"), 3,
                         "a second window search appeared")


class TheRibbonAndTheChatRunIntoOneTests(unittest.TestCase):
    """#125: what the window lost, and the one divider it kept."""

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def test_both_chips_moved_and_neither_was_dropped(self):
        """REMOVED IS NOT THE SAME AS GONE, the rule the address already had.
        The bar is deleted, so its two facts had to land somewhere a person can
        still reach -- and the tool switch is a SWITCH there, not a chip that
        has to shout in colour which of two modes is live."""
        self.assertIn('<section data-cat="server"', self.source)
        server = self.source[self.source.index('<section data-cat="server"'):
                             self.source.index('<section data-cat="skills"')]
        self.assertIn('id="conn"', server)
        self.assertIn('id="dot"', server)
        self.assertIn('id="tools"', server)
        self.assertIn('class="sw" id="toolsw"', server)
        self.assertIn('$("#toolsw").onclick=()=>crow.toggleTools();', self.source)

    def test_every_category_has_a_button_a_pane_and_the_same_key(self):
        """A section nobody can click does not exist; a button whose pane is
        missing opens nothing. #126 took the KEY off a positional array and put
        it on the button, so this compares the two sets instead of an order:
        a fifth button with a four-name list used to mark the wrong tab, and the
        fault read like a CSS problem."""
        import re

        nav = self.source[self.source.index('<nav id="scats">'):]
        nav = nav[:nav.index("</nav>")]
        buttons = set(re.findall(r'<button[^>]*data-cat="([a-z]+)"', nav))
        panes = set(re.findall(r'<section data-cat="([a-z]+)"', self.source))
        self.assertEqual(buttons, panes)
        self.assertEqual(buttons, {"look", "skills", "server", "mcp",
                                   "providers", "about"})
        self.assertIn("b.dataset.cat===name", self.source)
        self.assertNotIn('["look","skills"', self.source,
                         "the positional list is back")

    def test_the_empty_one_says_what_it_will_be(self):
        """#126. "Coming soon" alone is a dead end. It says what the section is
        FOR, so the placeholder is a promise rather than a shrug -- and so the
        next person to open it knows what belongs there.

        #129 TOOK MCP OUT OF THIS PAIR, and the rule it pins is untouched by
        that: the case is about what an UNBUILT section says, and MCPs is built.
        What used to be asserted here is asserted below instead, from the other
        side -- the pane has a list, a form and no promise in it.
        """
        for key, word in (("providers", "OpenRouter"),):
            pane = self.source[self.source.index('<section data-cat="%s"' % key):]
            pane = pane[:pane.index("</section>")]
            self.assertIn("Coming soon.", pane, key)
            self.assertIn(word, pane, key)

    def test_no_rule_is_drawn_between_the_ribbon_the_rail_and_the_chat(self):
        """NEGATIVE PROBE, and it is the whole of robin's second request: the
        three used to be separated by three 1px lines."""
        bar = self.css[self.css.index("#bar{"):]
        bar = bar[:bar.index("}") + 1]
        self.assertNotIn("border-bottom", bar)
        rail = self.css[self.css.index("#rail{"):]
        rail = rail[:rail.index("}") + 1]
        self.assertNotIn("border-right", rail)
        head = self.css[self.css.index("#railhead{"):]
        head = head[:head.index("}") + 1]
        self.assertNotIn("border-bottom", head)

    def test_the_corner_is_what_separates_them_instead(self):
        """With every rule gone the chat would blur into the panel. The radius
        separates them the way paper on a desk is separate -- by lying on top.
        `#body` must carry the rail colour or the corner has nothing to cut away
        to and reads as a notch."""
        main = self.css[self.css.index("#main{"):]
        main = main[:main.index("}") + 1]
        self.assertIn("border-top-left-radius", main)
        self.assertIn("background:var(--bg)", main)
        self.assertIn("overflow:hidden", main)
        body = self.css[self.css.index("#body{"):]
        body = body[:body.index("}") + 1]
        self.assertIn("background:var(--rail)", body)

    def test_the_ribbon_is_the_same_surface_as_the_rail(self):
        """One panel, one colour. A gradient ending in the chat's background
        would put the seam back in, only softer and harder to name."""
        bar = self.css[self.css.index("#bar{"):]
        bar = bar[:bar.index("}") + 1]
        self.assertIn("background:var(--rail)", bar)
        self.assertNotIn("gradient", bar)

    def test_the_version_is_only_where_it_is_looked_up(self):
        """It sat beside the wordmark and was copied into the sheet on open.
        The ribbon is a name and three window buttons now, so the number goes
        straight to About -- and the element it was copied FROM is gone, which
        is the half that would otherwise rot."""
        self.assertNotIn('id="ver"', self.source)
        self.assertNotIn('$("#ver")', self.source)
        self.assertIn('$("#aboutver").textContent=e.version;', self.source)

    def test_nothing_still_points_at_a_palette_entry_that_was_removed(self):
        """`--status-bg` and `--titlebar` had exactly one reader each, and both
        readers went with the bar. A palette value nothing reads is a value that
        drifts from the three themes it is written in three times."""
        for dead in ("--status-bg", "--titlebar"):
            self.assertNotIn(dead, self.source, dead)


class TheSkillSheetTests(unittest.TestCase):
    """#124: the switch in the settings sheet, and what it is allowed to be."""

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def test_the_sheet_has_a_row_per_skill_and_a_switch(self):
        """The category existed with "Nothing here yet." since the sheet was
        built; this is the ticket that fills it."""
        self.assertIn('<div id="skilllist"></div>', self.source)
        self.assertIn(".srow{", self.css)
        self.assertIn(".sw{", self.css)
        self.assertIn(".sw.on{", self.css)

    def test_a_skill_name_can_never_become_code(self):
        """A skill name is MODEL-WRITTEN TEXT. The rail learned this in #119 --
        a project called `'); alert('` is a label and must stay one -- and the
        same rule holds here, where the writer is not even a person."""
        self.assertIn("sw.onclick=()=>this.toggleSkill(", self.source)
        self.assertNotIn('onclick="crow.toggleSkill', self.source)
        drawn = self.source[self.source.index("drawSkills(){"):]
        drawn = drawn[:drawn.index("toggleSkill(name,row,sw)")]
        self.assertNotIn("innerHTML", drawn)

    def test_the_list_is_asked_for_every_time_the_sheet_opens(self):
        """It changes behind the window's back: the background review writes
        skills without anybody clicking anything, so a list cached in the page
        would show yesterday's set."""
        opener = self.source[self.source.index("openSettings(){"):]
        opener = opener[:opener.index("closeSettings()")]
        self.assertIn("this.drawSkills();", opener)

    def test_switching_one_off_repins_and_says_what_it_costs(self):
        """Without the re-pin the switch looks broken in the most confusing way
        available: the row flips, the file changes, and the running conversation
        keeps the head it was pinned with -- so nothing the model does changes
        until the next chat."""
        toggle = self.source[self.source.index("def toggle_skill"):]
        toggle = toggle[:toggle.index("\n    def ", 10)]
        self.assertIn("crow_core.set_skill_enabled", toggle)
        self.assertIn("repin_memory(crow_core.prompt_head())", toggle)
        self.assertIn("SKILL_COST_NOTE", toggle)
        self.assertLess(toggle.index("set_skill_enabled"), toggle.index("repin_memory"),
                        "the head is rebuilt before the file it is built from")

    def test_the_sheet_is_shown_the_disabled_ones_too(self):
        """NEGATIVE PROBE: a sheet that listed only the enabled skills would
        have no row to click for the others, so switching one back on would
        need a text editor."""
        api = self.source[self.source.index("    def skills(self)"):]
        api = api[:api.index("\n    def ", 10)]
        self.assertIn("crow_core.skills()", api)
        self.assertNotIn("enabled\"]", api.split("return")[0])
        self.assertNotIn('sk["body"]', api)

    def test_the_pinned_head_is_built_in_one_place_by_both_surfaces(self):
        """Memory and skills are two stores and ONE head. Two composers would be
        two byte-different heads for one set of facts, and neither chat could
        reuse the other's cache.

        BOTH FILES, and that is not thoroughness -- it is the defect. The window
        was moved to `prompt_head()` and the terminal's two lines were left on
        `memory_block()` for one commit: no crash, no failing case, just a
        terminal whose prompt silently carried no skills while the window's did.
        A surface-by-surface check is the only kind that sees it.
        """
        self.assertEqual(self.source.count("crow_core.prompt_head()"), 3)
        terminal = (HERE / "crow.py").read_text(encoding="utf-8")
        self.assertEqual(terminal.count("crow_core.prompt_head()"), 2)
        for name, text in (("crow_gui.py", self.source), ("crow.py", terminal)):
            self.assertNotIn("crow_core.memory_block()", text, name)
            self.assertNotIn("crow_core.skill_block()", text, name)


class TheMemoryPinWiringTests(unittest.TestCase):
    """#121 in the window: the pin is taken AFTER the boundary, never before.

    ALL THREE DOORS ARE CHECKED BY POSITION rather than by running them,
    because the failure is an ordering one and it is invisible at runtime: a pin
    taken too early is a perfectly valid head -- the template's -- and it is
    wrong for exactly the chats that have a project.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")

    def _after(self, bind: str, pin: str) -> bool:
        return self.source.index(pin) > self.source.index(bind)

    def test_every_pin_sits_below_the_line_that_binds(self):
        """The launch, and opening an archived chat. Both bind the chat's own
        directory first; a pin above either would be the template's memory.

        `rindex` FOR THE LAUNCH, because it pins in two branches: the one that
        restored a chat, below the bind, and the early return where there was
        nothing to restore -- which never binds at all, since `ready()` has
        already put the template up and there is no chat to correct it with.
        """
        self.assertGreater(self.source.rindex("self._pin_memory(SESSION_FILE)"),
                           self.source.index("self._adopt_chat_root(SESSION_FILE)"))
        self.assertTrue(self._after("self._adopt_chat_root(path)",
                                    "self._pin_memory(path)"))
        self.assertEqual(self.source.count("self._pin_memory("), 5,
                         "a caller pins somewhere this case has not read")

    def test_a_new_chat_pins_after_it_is_made_rootless(self):
        """`reset()` makes a new chat rootless on purpose (#119). The pin has to
        happen after that, or a new chat would inherit the last one's project."""
        self.assertTrue(self._after("self._adopt_chat_root(None, fresh=True)",
                                    "self._pin_memory(None)"))

    def test_a_turn_never_starts_from_an_unpinned_chat(self):
        """`_probe` has THREE ways to return before it reaches its own pin: the
        endpoint would not answer, `--no-session`, or an unreadable session
        file. The first is ordinary -- a window opened while the server is still
        starting -- and such a chat was never pinned at all, so its memory never
        entered a single prompt.

        SILENT BY CONSTRUCTION, which is why it needed finding rather than
        failing: an unpinned head is a VALID head, only one without the memory
        in it. Found on 2026-08-21 in a session.json that had no `memory` key
        after a whole conversation.

        BEFORE THE USER MESSAGE IS APPENDED, so the pin still meets an empty
        conversation and no prefix exists yet to move.
        """
        run = self.source[self.source.index("    def _run(self, text: str)"):]
        run = run[:run.index("\n    def ", 10)]
        self.assertIn("if self._conversation.memory is None:", run)
        self.assertLess(run.index("self._pin_memory("),
                        run.index('self._conversation.append("user"'),
                        "the chat is pinned after its own first message")

    def test_the_pin_is_read_before_the_payload_at_both_readers(self):
        """`load_session` needs the COMPOSED system prompt to decide whether the
        saved KV still fits, and that cannot be read out of messages nobody has
        opened yet."""
        self.assertEqual(self.source.count("crow_core.system_with_memory("), 2)
        # THE HELPER IS THE THIRD READER and it is the one that must not be
        # duplicated: `_pin_memory` is the single place that decides between a
        # stored pin and a fresh block. Two such decisions is two answers.
        self.assertEqual(self.source.count("crow_core.session_memory("), 3)
        self.assertEqual(self.source.count("def _pin_memory"), 1)

    def test_binding_a_folder_announces_the_prefill_before_it_is_paid(self):
        """The same shape `REASONING_COST_NOTE` already has. A cost said
        afterwards is not a warning, it is an excuse."""
        self.assertIn("crow_core.MEMORY_COST_NOTE", self.source)
        self.assertIn("if self._conversation.repin_memory(", self.source)

    def test_the_archive_folder_is_named_once(self):
        """#123 walks the same folder the rail is drawn from. A second
        `\"archiv\"` typed in the core would be a chat that is in the rail and
        not in the index, the first time either spelling changed."""
        self.assertIn("ARCHIVE_DIR = crow_core.ARCHIVE_DIR", self.source)
        # The ASSIGNMENT, not the word: the sentence above says "archiv" while
        # explaining why nothing here may define it, and a case that cannot tell
        # a comment from a declaration would forbid its own reason.
        self.assertNotIn('ARCHIVE_DIR = "archiv"', self.source)


class TheMcpSheetTests(ApiCase):
    """E4 in the window: the checklist, and the two columns that ARE the stage.

    One column says whether a tool is taken at all, the other what Crow treats
    it as. Both are states in a file, and the second one is the whole reason the
    stage exists -- a class is what decides whether a release level stops and
    asks before a foreign process runs.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]
        self._real_mcp = crow_core.MCP_FILE
        self.addCleanup(self._restore_mcp)
        crow_core.MCP_FILE = os.path.join(self.dir, "mcp.json")
        crow_core.mcp_apply()

    def _restore_mcp(self) -> None:
        crow_core.forget_mcp_servers()
        crow_core.MCP_FILE = self._real_mcp
        crow_core.mcp_apply()

    def _pane(self) -> str:
        pane = self.source[self.source.index('<section data-cat="mcp"'):]
        return pane[:pane.index("</section>")]

    def _configure(self) -> None:
        """A server on disk with a schema, without ever starting one: the sheet
        reads the file, and E3's cases are where a process belongs."""
        with open(crow_core.MCP_FILE, "w", encoding="utf-8") as fh:
            json.dump({"servers": {"fake": {
                "command": "npx", "args": ["-y", "server-fake"],
                "schema": {"tools": [
                    {"name": "look", "description": "Look at something.",
                     "inputSchema": {"type": "object"},
                     "annotations": {"readOnlyHint": True}},
                    {"name": "burn", "description": "Burn it down.",
                     "inputSchema": {"type": "object"}}]},
                "tools": {"include": []}}}}, fh)
        crow_core.mcp_apply()

    # ---- the pane is built rather than promised

    def test_the_pane_is_a_list_and_one_field(self):
        """robin, 2026-08-22: no prose in the sheet. What was there described
        behaviour, and one of the sentences had already outlived the behaviour
        it described."""
        pane = self._pane()
        self.assertIn('id="mcplist"', pane)
        self.assertIn('id="mcpline"', pane)
        for gone in ("mcpname", "mcpcmd", "mcpargs", "mcpbound", "Coming soon",
                     "nothing reaches the model", "boundary"):
            self.assertNotIn(gone, pane, gone)
        self.assertLess(len(pane), 700, "the pane grew prose again")

    def test_the_sheet_asks_for_the_list_when_it_opens(self):
        opened = self.source[self.source.index("openSettings(){"):]
        self.assertIn("this.drawMcp()", opened[:opened.index("},")])

    def test_a_server_name_never_becomes_markup(self):
        """#119's lesson, and it applies harder here: a server's name and its
        tool descriptions are written by a stranger, not by the user."""
        js = self.source[self.source.index("drawMcpServer(box,sv,classes){"):
                         self.source.index("tickMcp(server,tool,row,sw){")]
        self.assertNotIn("innerHTML", js)
        self.assertIn("name.textContent=sv.enabled?sv.name", js)
        # AND NO LITERAL GLYPH EITHER: `check_gui_prereqs` resolves string
        # literals against the shipped face, and the caret is not in it.
        self.assertIn("String.fromCharCode(9654)", js)

    # ---- the two columns

    def test_a_decision_and_a_proposal_do_not_look_alike(self):
        """THE VALUE, NOT THE NAME -- the 2026-08-22 lesson. A class the server
        merely suggested may never read as one a person picked, so the two are
        separated by the EDGE as well as the colour: a theme where `--dim` and
        `--accent` happen to sit close would otherwise render them identical."""
        import re as _re
        CLOSE = chr(125)

        def rule(selector):
            found = self.css[self.css.index(selector):]
            return found[:found.index(CLOSE)]

        guess, chosen = rule(".seg button.guess{"), rule(".seg button.on{")
        self.assertIn("border-style:dashed", guess)
        self.assertNotIn("dashed", chosen)

        # THE BRAND VALUES ARE NOT IN THE STYLESHEET. `--accent` and `--bevel`
        # are placeholders the page fills from the core at render time, so a
        # reader that only searched the CSS would report "not defined" for the
        # one colour this case is actually about.
        FROM_CORE = {"--accent": crow_core.CROW_ACCENT_HEX,
                     "--bevel": crow_core.BANNER_BEVEL_HEX}

        def value(css, token):
            hexed = FROM_CORE.get(token)
            if hexed is None:
                m = _re.search(_re.escape(token) + r":\s*(#[0-9a-fA-F]{6})", css)
                self.assertIsNotNone(m, "%s is not defined" % token)
                hexed = m.group(1)
            h = hexed.lstrip("#")
            return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))

        dark = self.css[self.css.index("--bg:#181818"):]
        dark = dark[:dark.index(CLOSE)]
        picked = _re.search(r"color:var\((--[a-z-]+)\)", chosen)
        proposed = _re.search(r"color:var\((--[a-z-]+)\)", guess)
        self.assertIsNotNone(picked)
        self.assertIsNotNone(proposed)
        apart = sum(abs(a - b) for a, b in zip(value(dark, picked.group(1)),
                                               value(dark, proposed.group(1))))
        self.assertGreater(apart, 60,
                           "a proposal and a decision are the same colour")

    def test_the_classes_come_from_the_core_not_from_the_page(self):
        """Three buttons typed into the JS would be three buttons to fix the day
        a fourth class exists -- and one of them would be missed."""
        self.assertIn("classes.forEach", self.source)
        self.assertEqual(crow_core.mcp_view()["classes"],
                         list(crow_core.MCP_TOOL_CLASSES))

    def test_every_element_the_drawing_looks_up_exists(self):
        """A mistyped id draws nothing and reads as a CSS problem -- the shape
        #126's positional-list defect had. This suite never executes the page,
        so the lookups are held against the markup instead of against a render.
        """
        js = self.source[self.source.index("drawMcp(){"):
                         self.source.index("toggleSkill(name,row,sw){")]
        looked_up = sorted(set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js)))
        self.assertTrue(looked_up, "the drawing looks nothing up")
        in_page = set(re.findall(r'id="([A-Za-z0-9_-]+)"', self.source))
        self.assertEqual([i for i in looked_up if i not in in_page], [])

    # ---- what robin asked to see, 2026-08-22

    def test_the_install_line_is_drawn_in_the_chat_not_in_the_sheet(self):
        """robin, twice: the tile belongs where the command was typed. It is
        drawn from `go()`, after the typed line and before the call."""
        go = self.source[self.source.index("  go(){"):self.source.index("  modeMenu(){")]
        self.assertIn("installBar()", go)
        self.assertLess(go.index("this.user(text)"), go.index("installBar()"))
        self.assertLess(go.index("installBar()"), go.index("pywebview.api.send"))
        self.assertNotIn("mcpbusy", self.source)

    def test_only_adding_a_server_gets_the_tile(self):
        """NEGATIVE: `/mcp` on its own answers instantly. A tile in front of an
        instant answer is four seconds of nothing."""
        import re as _re
        pattern = _re.search(r"if\((/[^)]+/i)\.test\(text\)\) this\.installBar",
                             self.source)
        self.assertIsNotNone(pattern, "the tile is not gated on the command")
        rule = pattern.group(1)
        self.assertIn("add", rule)
        self.assertIn("mcp", rule)

    def test_it_wears_the_memory_gates_animation_rather_than_a_copy(self):
        """Two sweeps written out twice drift the first time one is touched."""
        CLOSE = chr(125)
        bar = self.css[self.css.index(".installbar{"):]
        bar = bar[:bar.index(CLOSE)]
        self.assertIn("pendsweep", bar)
        self.assertIn("pendglow", bar)
        self.assertEqual(self.css.count("@keyframes pendsweep"), 1)

    def test_it_says_install_mcp_and_stands_for_four_seconds(self):
        js = self.source[self.source.index("  installBar(){"):
                         self.source.index("  note(msg){")]
        self.assertIn('textContent="Install MCP"', js)
        self.assertIn("Date.now()+4000", js)
        held = self.source[self.source.index("  note(msg){"):
                           self.source.index("  drawNote(msg){")]
        self.assertIn("this.installUntil - Date.now()", held)
        self.assertIn("Math.max(0", held)

    def test_a_server_folds_away(self):
        """One ordinary server is a dozen tools and already outruns the sheet. Twenty
        servers unfolded is a scroll nobody finishes."""
        CLOSE = chr(125)
        shut = self.css[self.css.index(".mcptools{"):]
        self.assertIn("display:none", shut[:shut.index(CLOSE)])
        open_ = self.css[self.css.index(".mcptools.open{"):]
        self.assertIn("display:block", open_[:open_.index(CLOSE)])
        js = self.source[self.source.index("  drawMcpServer(box,sv,classes){"):
                         self.source.index("  mcpRow(sv,t,classes){")]
        self.assertIn("this.mcpOpen", js)
        # THE BUTTONS SIT INSIDE THE HEAD. Without this a click on "remove"
        # would also fold the server away -- the trap the memory tile hit.
        self.assertEqual(js.count("stopPropagation()"), 2)

    def test_the_sheet_got_room_for_a_server_list(self):
        CLOSE = chr(125)
        rule = self.css[self.css.index("#settings .sheet{"):]
        rule = rule[:rule.index(CLOSE)]
        import re as _re
        width = int(_re.search(r"width:min\((\d+)px", rule).group(1))
        height = int(_re.search(r"height:min\((\d+)px", rule).group(1))
        self.assertGreater(width, 760)
        self.assertGreater(height, 560)

    def test_what_the_user_said_is_a_bubble_in_every_theme(self):
        """THE VALUE, NOT THE NAME. A bubble drawn in a literal colour would be
        right in the theme it was picked in and wrong in the other two; these
        tokens are defined three times, once per palette."""
        CLOSE = chr(125)
        rule = self.css[self.css.index(".you .txt{"):]
        rule = rule[:rule.index(CLOSE)]
        self.assertIn("border-radius", rule)
        self.assertIn("padding", rule)
        # HUGGING THE TEXT, not filling the column: without this a two-word
        # message is a full-width slab.
        self.assertIn("justify-self:start", rule)
        import re as _re
        used = _re.findall(r"var\((--[a-z-]+)\)", rule)
        self.assertIn("--raised", used)
        for token in set(used):
            self.assertEqual(self.css.count(token + ":"), 3,
                             "%s is not defined in all three palettes" % token)
        self.assertFalse(_re.findall(r"#[0-9a-fA-F]{3,6}", rule),
                         "the bubble names a colour of its own")

    # ---- what the api actually does

    def test_the_view_reaches_the_page_unchanged(self):
        self._configure()
        view = self.api().mcp_view()
        self.assertEqual(view["file"], crow_core.MCP_FILE)
        self.assertEqual([t["tool"] for t in view["servers"][0]["tools"]],
                         ["look", "burn"])
        self.assertEqual(view["servers"][0]["tools"][0]["proposed"], "reading")
        self.assertEqual(view["servers"][0]["tools"][1]["proposed"], "executing")

    def test_a_tick_writes_and_names_the_bill(self):
        self._configure()
        api = self.api()
        self.assertEqual(api.mcp_confirm("fake", "look", True, "reading"), "")
        self.assertIn("mcp_fake_look", [t["function"]["name"] for t in crow_core.TOOLS])
        self.assertEqual(crow_core.TOOL_CLASS["mcp_fake_look"], "reading")
        self.assertTrue(any(m.get("t") == crow_core.MCP_COST_NOTE
                            for m in self.drained(api)))

    def test_a_refused_tick_says_why_and_bills_nothing(self):
        """NEGATIVE: the page paints the click before the file takes it, so a
        refusal has to come back as a REASON -- and must not announce a prefill
        that nothing is going to cost."""
        self._configure()
        api = self.api()
        said = api.mcp_confirm("fake", "invented", True, "reading")
        self.assertIn("invented", said)
        self.assertFalse(any(m.get("t") == crow_core.MCP_COST_NOTE
                             for m in self.drained(api)))

    def test_moving_one_column_leaves_the_other_alone(self):
        """The switch and the class are two decisions. Un-ticking a tool must
        not throw away what somebody said it does, or re-ticking asks again."""
        self._configure()
        api = self.api()
        api.mcp_confirm("fake", "look", True, "writing")
        api.mcp_confirm("fake", "look", False, None)
        with open(crow_core.MCP_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["servers"]["fake"]["classes"],
                             {"look": "writing"})

    def test_one_line_is_the_whole_form(self):
        """A command line is what people already have -- from a README, from
        another client's config. Three fields is three chances to get it wrong."""
        self.assertIn("splitlines()", "") if False else None
        source = inspect.getsource(crow_gui.Api.mcp_add)
        self.assertIn("mcp_add_line", source)
        said = self.api().mcp_add("")
        self.assertIn("command line", said)


class ToolCallsLeaveTheReadingColumnTests(unittest.TestCase):
    """#131. A 24-round turn put 24 tool rows between the question and the
    answer, and the answer is what a person came back for.

    THE PAGE IS NOT EXECUTED BY THIS SUITE, so these hold the drawing against
    the markup and the stylesheet -- the same way the rail's and the memory
    tile's structure is held. What they cannot see is what it looks like.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def _rule(self, selector: str) -> str:
        found = self.css[self.css.index(selector):]
        return found[:found.index(chr(125))]

    def test_a_tool_row_is_appended_to_the_tile_and_not_to_the_turn(self):
        js = self.source[self.source.index("  tool(name,args){"):
                         self.source.index("  toolsCount(){")]
        self.assertIn('$("#tclist")', js)
        self.assertNotIn("this.col.insertBefore", js)

    def test_the_tile_is_in_the_page_before_anything_is_called(self):
        """"Immer da, auch im leeren Chat" -- a tile that appears with the first
        call is one nobody has learned to look at."""
        self.assertIn('id="toolcalls"', self.source)
        self.assertIn("crow.toolsCount();", self.source)
        js = self.source[self.source.index("  toolsCount(){"):
                         self.source.index("  toolsToggle(){")]
        self.assertIn("Nothing called yet.", js)

    def test_it_opens_and_shuts_on_the_plus(self):
        self.assertIn("display:none", self._rule("#toolcalls.shut .tcbody{"))
        js = self.source[self.source.index("  toolsToggle(){"):
                         self.source.index("  toolsClear(e){")]
        self.assertIn('classList.toggle("shut")', js)
        # THE SIGN FOLLOWS THE STATE. A plus on an open tile is a control that
        # lies about what pressing it will do.
        self.assertIn('shut ? "+"', js)

    def test_open_it_is_as_wide_as_its_widest_row(self):
        """A tool line is a path plus arguments. A fixed width would ellipsis
        away the half that says which file, which is the half worth reading."""
        self.assertIn("width:max-content", self._rule("#toolcalls{"))
        self.assertIn("overflow:visible", self._rule("#toolcalls .tool .arg{"))

    def test_it_wears_the_bubble_of_whichever_skin_is_on(self):
        """robin, 2026-08-22. NOT a literal that matches today's dark bubble --
        the same TOKENS, so the tile follows the skin that is on rather than the
        one it was picked in. Both are defined once per palette."""
        tile, bubble = self._rule("#toolcalls{"), self._rule(".you .txt{")
        import re as _re
        for prop in ("background", "border"):
            want = _re.search(prop + r"[^;]*var\((--[a-z-]+)\)", bubble)
            self.assertIsNotNone(want, prop)
            self.assertIn("var(%s)" % want.group(1), tile, prop)
            self.assertEqual(self.css.count(want.group(1) + ":"), 3,
                             "%s is not in all three palettes" % want.group(1))
        self.assertFalse(_re.findall(r"#[0-9a-fA-F]{3,6}", tile),
                         "the tile names a colour of its own")

    def test_it_sits_clear_of_the_edge(self):
        """It was 14 and 18 px off the corner and read as stuck to it."""
        import re as _re
        tile = self._rule("#toolcalls{")
        top = int(_re.search(r"top:(\d+)px", tile).group(1))
        right = int(_re.search(r"right:(\d+)px", tile).group(1))
        self.assertGreaterEqual(top, 24)
        self.assertGreaterEqual(right, 30)

    def test_clearing_does_not_also_fold_the_tile_away(self):
        """NEGATIVE, and the trap the memory tile already hit: the button sits
        inside the head, so without catching the click it would bubble up to the
        fold and hide the list it had just emptied."""
        js = self.source[self.source.index("  toolsClear(e){"):
                         self.source.index("  toolsReset(){")]
        self.assertIn("stopPropagation()", js)
        self.assertIn('$("#tclist").textContent=""', js)

    def test_a_new_chat_empties_it(self):
        """The tile belongs to the conversation, not to the window."""
        drawn = self.source[self.source.index('case "clear":'):]
        self.assertIn("this.toolsReset()", drawn[:drawn.index("break;")])


class TheUserBubbleTests(unittest.TestCase):
    """#131. robin, 2026-08-22: the label goes, and the bubble has to break."""

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def test_the_label_is_gone_from_the_markup_and_the_stylesheet(self):
        js = self.source[self.source.index("  user(text){"):
                         self.source.index("  start(){")]
        self.assertNotIn("you&gt;", js)
        self.assertNotIn(".you .m{", self.css, "a rule with no wearer is left")

    def test_the_bubble_breaks_before_it_fills_the_column(self):
        """A bubble that runs the full width is a slab, and nothing about it
        reads as one side of a conversation."""
        rule = self.css[self.css.index(".you .txt{"):]
        rule = rule[:rule.index(chr(125))]
        self.assertIn("max-width:75%", rule)

    def test_the_model_keeps_its_own_column(self):
        """NEGATIVE: the cap is the user's line only. Narrowing the answer would
        cost the reader a quarter of every page of it."""
        rule = self.css[self.css.index(".as{"):]
        rule = rule[:rule.index(chr(125))]
        self.assertNotIn("max-width", rule)
        say = self.css[self.css.index(".say{"):]
        self.assertNotIn("max-width", say[:say.index(chr(125))])


class TheTraceFoldsFinishedRoundsTests(unittest.TestCase):
    """#131, variant A. `reply_started` fires once per ROUND, so a 24-round turn
    drew 24 blocks of thoughts and running commentary -- and the answer, which is
    what somebody came back for, was below the fold.

    THE LIVE ROUND IS NOT FOLDED, and that is the half that makes it usable: the
    interim text is the only sign of life a long turn has, so hiding it too
    would leave a blank screen for minutes.
    """

    def setUp(self) -> None:
        self.source = (HERE / "crow_gui.py").read_text(encoding="utf-8")
        self.css = self.source[self.source.index("<style>"):self.source.index("</style>")]

    def _js(self, start: str, end: str) -> str:
        return self.source[self.source.index(start):self.source.index(end)]

    def test_a_new_round_folds_the_one_before_it(self):
        """The only signal there is: nothing tells the page a round was the LAST
        one, so the last round is the one nobody folded."""
        start = self._js("  start(){", "  say_(")  if False else self._js(
            "  start(){", "  thinkOpen(")
        self.assertIn("this.fold()", start)
        self.assertIn("this.round=t", start)

    def test_the_running_round_stays_where_it_is(self):
        """NEGATIVE, and it is the whole difference between variant A and B: only
        `fold` moves anything, and it moves `this.round` -- which `start` has
        just replaced with the new one."""
        fold = self._js("  fold(){", "  start(){")
        self.assertIn("const done=this.round; this.round=null;", fold)
        self.assertIn('.tb").appendChild(done)', fold)

    def test_an_empty_round_is_dropped_rather_than_counted(self):
        """A round that produced nothing but a tool call has no text and no
        thought left in the column -- the rows went to the tile. Counting it
        would make `Trace 24 rounds` out of a turn with four things in it."""
        fold = self._js("  fold(){", "  start(){")
        self.assertIn("done.remove()", fold)

    def test_it_says_how_many_rounds_it_holds(self):
        fold = self._js("  fold(){", "  start(){")
        self.assertIn('this.traceN===1 ? " round" : " rounds"', fold)

    def test_a_new_user_line_starts_a_new_trace(self):
        """One trace per turn. Without this the second question's rounds would
        pile into the first question's block."""
        user = self._js("  user(text){", "  fold(){")
        self.assertIn("this.endTrace()", user)
        drawn = self.source[self.source.index('case "clear":'):]
        self.assertIn("this.endTrace()", drawn[:drawn.index("break;")])

    def test_folded_it_costs_one_line(self):
        CLOSE = chr(125)
        body = self.css[self.css.index("details.trace .tb{"):]
        self.assertIn("border-left", body[:body.index(CLOSE)])
        summary = self.css[self.css.index("details.trace>summary{"):]
        self.assertIn("inline-flex", summary[:summary.index(CLOSE)])


class ADismissedToolRowStaysDismissedTests(ApiCase):
    """#131. robin, after rebooting the window: the calls he had deleted were
    back. A reopened chat replays its tool rows, and clearing had only emptied
    the page.

    THE CONVERSATION IS NOT TOUCHED. The model keeps every call it made; what is
    remembered is a VIEW fact -- how far the user has dismissed.
    """

    def test_the_watermark_is_a_count_of_what_the_conversation_holds(self):
        """COUNTED FROM THE CONVERSATION, not from what the page was showing:
        clearing twice would otherwise set it to the SECOND batch and bring the
        first one back."""
        api = self.api()
        api._conversation.append("user", "go")
        api._conversation.append(
            "assistant", "",
            tool_calls=[{"id": "a", "name": "read_file", "arguments": "{}"},
                        {"id": "b", "name": "list_dir", "arguments": "{}"}])
        self.assertEqual(api.tools_cleared(), 2)
        self.assertEqual(api._tools_cleared, 2)

    def test_a_replay_draws_only_what_came_after_it(self):
        api = self.api()
        api._tools_cleared = 1
        messages = [{"role": "user", "content": "go"},
                    {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}},
                                    {"function": {"name": "list_dir", "arguments": "{}"}}]}]
        api._replay(messages)
        rows = [m for m in self.drained(api) if m.get("k") == "tool"]
        self.assertEqual([r["name"] for r in rows], ["list_dir"])

    def test_without_a_watermark_everything_is_drawn(self):
        """NEGATIVE: the skip may only ever hide what somebody dismissed."""
        api = self.api()
        messages = [{"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}},
                                    {"function": {"name": "list_dir", "arguments": "{}"}}]}]
        api._replay(messages)
        rows = [m for m in self.drained(api) if m.get("k") == "tool"]
        self.assertEqual([r["name"] for r in rows], ["read_file", "list_dir"])

    def test_it_survives_the_file(self):
        """Written and READ BACK. A watermark only ever written is one nobody
        has proved comes back -- and coming back is its entire job."""
        path = os.path.join(self.dir, "chat.json")
        talk = crow_core.Conversation("SYS")
        talk.append("user", "go")
        talk.append("assistant", "done")
        crow_core.save_session(talk, "http://127.0.0.1:1/v1", 10, path=path,
                               with_kv=False, tools_cleared=3)
        self.assertEqual(crow_core.session_tools_cleared(path), 3)

    def test_a_file_that_never_cleared_carries_no_key(self):
        """NEGATIVE, and the three-state rule this file already keeps: absent is
        its own value, and every session written before this build is absent."""
        path = os.path.join(self.dir, "chat.json")
        talk = crow_core.Conversation("SYS")
        talk.append("user", "go")
        talk.append("assistant", "done")
        crow_core.save_session(talk, "http://127.0.0.1:1/v1", 10, path=path,
                               with_kv=False)
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn(crow_core.SESSION_TOOLS_CLEARED_KEY, json.load(fh))
        self.assertEqual(crow_core.session_tools_cleared(path), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
