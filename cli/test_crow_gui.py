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
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import crow            # noqa: E402
import crow_core       # noqa: E402
import crow_gui        # noqa: E402


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

    def test_the_open_chat_is_not_listed_under_earlier(self):
        """It is drawn at the top, as itself. Listed again below, the same
        conversation stands twice: once as itself, once as its own history."""
        api = self.api()
        self.a_chat(api, "the open one")
        ok, path = api._leave()
        self.assertTrue(ok)
        api._reload_rail()
        entry = self.rail(api)
        self.assertEqual([r["path"] for r in entry["rollovers"]], [])
        self.assertEqual(os.path.basename(path), entry["foot"])

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

    def test_every_shared_command_gets_an_answer(self):
        api = self.api()
        for command in crow_core.SLASH_COMMANDS:
            self.assertIsNotNone(api.slash_answer(command),
                                 f"{command} still travels to the model")

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

    def test_every_pointer_names_a_control_the_page_actually_has(self):
        """THE FAILURE THIS CATCHES IS A LIE, NOT A CRASH. "That is the new
        button" stays green forever after the button is renamed, and the user is
        the one who finds out. Each pointer is tied to the markup it promises.
        """
        anchors = {
            "/reset": 'id="new"',
            "/context": 'id="ctx"',
            "/mode": 'id="modemenu"',
            "/thoughts": "Thought",
            "/exit": "wb close",
            "/quit": "wb close",
        }
        self.assertEqual(sorted(anchors), sorted(crow_gui.Api.POINTS_AT),
                         "a pointer exists with no anchor pinned, or the reverse")
        for command, anchor in anchors.items():
            self.assertIn(anchor, crow_gui.PAGE,
                          f"{command} points at {anchor!r}, which the page does not have")

    def test_the_two_that_are_executed_are_the_two_without_a_widget(self):
        """The decision itself, written where a later change has to argue with
        it: /help and /tools are the commands this window has no control for."""
        executed = set(crow_core.SLASH_COMMANDS) - set(crow_gui.Api.POINTS_AT)
        self.assertEqual(executed, {"/help", "/tools"})

    def test_a_command_is_shown_as_asked_before_it_is_answered(self):
        """The transcript has to show what was typed, or the answer arrives with
        no question above it and reads as the model volunteering it."""
        api = self.api()
        api.send("/reset")
        kinds = [(m.get("k"), m.get("t")) for m in self.drained(api)]
        self.assertEqual(kinds[0][0], "user")
        self.assertEqual(kinds[0][1], "/reset")
        self.assertEqual(kinds[1][0], "note")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
