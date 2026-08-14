#!/usr/bin/env python3
"""Suite for cli/crow.py. Standard library only, same as the CLI itself.

Run:  python cli/test_crow.py

Every group carries at least one case that must FAIL if the behaviour it
guards regresses -- a suite that cannot go red proves nothing.
"""

from __future__ import annotations

import builtins
import contextlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crow  # noqa: E402
# THE MODULE ITSELF, beside the re-exports, and #92 is what needs it: `crow.py`
# binds names by VALUE, so `crow.ROOTS_FILE` is a copy and rebinding it moves
# nothing -- the core would go on reading its own. Redirecting a module-level
# path in a test has to happen where the functions read it.
import crow_core  # noqa: E402


class HealthUrlTests(unittest.TestCase):
    """The exact defect that shipped: base_url[:-3] + "health"."""

    def test_v1_suffix_is_replaced_with_health(self):
        self.assertEqual(
            crow.health_url("http://127.0.0.1:8081/v1"),
            "http://127.0.0.1:8081/health",
        )

    def test_trailing_slash_does_not_double(self):
        self.assertEqual(
            crow.health_url("http://127.0.0.1:8081/v1/"),
            "http://127.0.0.1:8081/health",
        )

    def test_bare_root_gets_health_appended(self):
        self.assertEqual(
            crow.health_url("http://127.0.0.1:8081"),
            "http://127.0.0.1:8081/health",
        )

    def test_port_stays_numeric(self):
        """The regression itself: '8081health' is an invalid port."""
        for base in ("http://127.0.0.1:8081/v1", "http://localhost:8081/v1/"):
            with self.subTest(base=base):
                self.assertNotIn("8081health", crow.health_url(base))
                self.assertIn(":8081/", crow.health_url(base))

    def test_the_old_formula_really_was_broken(self):
        """Positive control for the test above -- it caught a real bug."""
        broken = "http://127.0.0.1:8081/v1"[:-3] + "health"
        self.assertEqual(broken, "http://127.0.0.1:8081health")
        self.assertNotEqual(broken, crow.health_url("http://127.0.0.1:8081/v1"))


class ConversationTests(unittest.TestCase):
    def test_system_prompt_is_first_and_stays_first(self):
        conversation = crow.Conversation("SYS")
        conversation.append("user", "a")
        conversation.append("assistant", "b")
        payload = conversation.payload()
        self.assertEqual(payload[0], {"role": "system", "content": "SYS"})
        self.assertEqual([m["content"] for m in payload], ["SYS", "a", "b"])

    def test_prefix_is_byte_identical_as_it_grows(self):
        """The append-only condition #45 phase 0.2 rests on."""
        conversation = crow.Conversation("SYS")
        conversation.append("user", "one")
        first = json.dumps(conversation.payload())
        conversation.append("assistant", "two")
        conversation.append("user", "three")
        grown = json.dumps(conversation.payload())
        self.assertTrue(
            grown.startswith(first[:-1]),
            "the earlier turns changed -- the prompt cache would be lost",
        )

    def test_payload_is_a_copy(self):
        conversation = crow.Conversation("SYS")
        conversation.append("user", "a")
        stolen = conversation.payload()
        stolen[1]["content"] = "MUTATED"
        stolen.append({"role": "user", "content": "INJECTED"})
        self.assertEqual(conversation.payload()[1]["content"], "a")
        self.assertEqual(len(conversation), 2)

    def test_reset_keeps_the_system_prompt(self):
        conversation = crow.Conversation("SYS")
        conversation.append("user", "a")
        conversation.reset()
        self.assertEqual(conversation.payload(), [{"role": "system", "content": "SYS"}])

    def test_reset_without_system_prompt_is_empty(self):
        conversation = crow.Conversation()
        conversation.append("user", "a")
        conversation.reset()
        self.assertEqual(conversation.payload(), [])
        self.assertEqual(len(conversation), 0)

    def test_assistant_reasoning_rides_along(self):
        conversation = crow.Conversation("SYS")
        conversation.append("user", "a")
        conversation.append("assistant", "b", "THOUGHTS")
        self.assertEqual(conversation.payload()[2],
                         {"role": "assistant", "content": "b",
                          "reasoning_content": "THOUGHTS"})

    def test_a_turn_without_reasoning_carries_no_field(self):
        """An empty field would move the prefix for nothing."""
        conversation = crow.Conversation("SYS")
        conversation.append("assistant", "b")
        conversation.append("assistant", "c", "")
        self.assertNotIn("reasoning_content", conversation.payload()[1])
        self.assertNotIn("reasoning_content", conversation.payload()[2])

    def test_the_prefix_still_grows_only_at_the_end_with_reasoning(self):
        conversation = crow.Conversation("SYS")
        conversation.append("user", "one")
        conversation.append("assistant", "two", "THOUGHTS")
        first = json.dumps(conversation.payload())
        conversation.append("user", "three")
        self.assertTrue(json.dumps(conversation.payload()).startswith(first[:-1]))

    def test_no_edit_or_delete_api_exists(self):
        """Append-only by construction, not by discipline."""
        for forbidden in ("pop", "insert", "remove", "edit", "replace", "prepend"):
            self.assertFalse(
                hasattr(crow.Conversation, forbidden),
                f"Conversation.{forbidden} would break the append-only rule",
            )


class FormatTimingsTests(unittest.TestCase):
    def test_full_timings(self):
        line = crow.format_timings({
            "predicted_n": 150,
            "predicted_per_second": 11.03,
            "prompt_n": 17,
            "prompt_per_second": 9.47,
            "_client_ttft_s": 2.5,
            "_client_total_s": 16.1,
        })
        self.assertIn("150 tok @ 11.03 tok/s", line)
        self.assertIn("prefill 17 @ 9.47 tok/s", line)
        self.assertIn("ttft 2.5s", line)

    def test_empty_timings_produce_nothing(self):
        """No numbers must be invented when the server sent none."""
        self.assertEqual(crow.format_timings({}), "")

    def test_partial_timings_omit_what_is_missing(self):
        line = crow.format_timings({"predicted_n": 42})
        self.assertEqual(line, "42 tok")
        self.assertNotIn("tok/s", line)


class FormatClockTests(unittest.TestCase):
    def test_seconds_keep_a_decimal(self):
        self.assertEqual(crow.format_clock(4.27), "4.3s")

    def test_minutes(self):
        self.assertEqual(crow.format_clock(252.0), "4m12s")

    def test_hours(self):
        """4531.29 s is the real total of the 2026-08-08 run in #71."""
        self.assertEqual(crow.format_clock(4531.29), "1h15m31s")

    def test_the_boundary_is_not_off_by_one(self):
        self.assertEqual(crow.format_clock(59.9), "59.9s")
        self.assertEqual(crow.format_clock(60.0), "1m00s")


class TurnCostTests(unittest.TestCase):
    """The per-turn summary that replaced the per-round timing lines (#70)."""

    def _cost(self, rounds=2):
        cost = crow.TurnCost()
        for _ in range(rounds):
            cost.add_round({
                "predicted_n": 100, "prompt_n": 50,
                "_client_total_s": 10.0, "_cached_tokens": 900,
            })
        return cost

    def test_tokens_and_rounds_accumulate(self):
        line = self._cost().line()
        self.assertIn("2 rounds", line)
        self.assertIn("200 tok", line)
        self.assertIn("prefill 100", line)

    def test_the_rate_is_decode_speed_not_tokens_over_the_whole_round(self):
        """The numbers are the live run of 2026-08-11, server log in the issue thread.

        Round 1: 9,967 prompt tokens in 150.2 s, 136 decoded in 9.21 s -> 14.77 tok/s.
        Round 2: 53 prompt tokens in 2.00 s, 116 decoded in 7.05 s -> 16.46 tok/s.

        The first version divided 252 tokens by the 169 s the two rounds took in total and printed
        **1.49 tok/s** for a turn the server had just measured at 14.8 and 16.5. Prefill is not
        decode, and a rate that mixes them describes neither. The assertion pins both figures, so
        collapsing them back into one round total goes red here."""
        cost = crow.TurnCost()
        cost.add_round({"predicted_n": 136, "predicted_ms": 9209.80,
                        "prompt_n": 9967, "prompt_ms": 150196.81, "_client_total_s": 159.4})
        cost.add_round({"predicted_n": 116, "predicted_ms": 7046.93,
                        "prompt_n": 53, "prompt_ms": 2003.40, "_client_total_s": 9.05})
        line = cost.line()
        # 252 / (9.2098 + 7.04693) s = 15.50, which sits between the server's own 14.77 and 16.46
        # for the two rounds. Prefill: 10,020 / (150.19681 + 2.0034) s = 65.83, against 66.36 and
        # 26.46 per round -- the first round dominates because it carries 9,967 of the tokens.
        self.assertIn("252 tok @ 15.50 tok/s", line)
        self.assertIn("prefill 10,020 @ 65.83 tok/s", line)
        self.assertNotIn("1.49", line)

    def test_a_server_that_sends_only_rates_still_produces_one(self):
        """`*_ms` is llama.cpp's field. A server that reports the rate and not the duration must
        not silently drop to no rate at all -- that reads as 'not measured'."""
        cost = crow.TurnCost()
        cost.add_round({"predicted_n": 100, "predicted_per_second": 20.0,
                        "prompt_n": 400, "prompt_per_second": 80.0})
        line = cost.line()
        self.assertIn("100 tok @ 20.00 tok/s", line)
        self.assertIn("prefill 400 @ 80.00 tok/s", line)

    def test_cached_is_the_last_state_not_a_sum(self):
        """`cached` describes the prefix as it stands. Adding two rounds of it produces a number
        that means nothing -- and would silently look like a healthier cache than there is."""
        line = self._cost(rounds=3).line()
        self.assertIn("cached 900/950", line)
        self.assertNotIn("2,700", line)

    def test_the_total_is_wall_clock_not_the_sum_of_rounds(self):
        """THE POINT OF THE WHOLE CLASS. Tool time runs between the rounds and the user waits
        through it, so a total built from `_client_total_s` describes a turn nobody had.

        The negative control is in the same assertion: the model figure is printed too, so if
        someone ever wires `waited` to the round sum, these two collapse onto each other and this
        test goes red. Without the second half it would pass on the broken version."""
        cost = crow.TurnCost()
        cost.started -= 300.0                      # the turn began five minutes ago
        cost.add_round({"predicted_n": 10, "_client_total_s": 20.0})
        cost.add_tool(40.0, failed=False)
        line = cost.line()
        self.assertIn("waited 5m00s", line)        # wall clock, not 20 s and not 60 s
        self.assertIn("model 20.0s", line)
        self.assertIn("tools 40.0s", line)

    def test_failed_tool_calls_are_counted_not_hidden(self):
        cost = crow.TurnCost()
        cost.add_round({"predicted_n": 1})
        cost.add_tool(1.0, failed=True)
        cost.add_tool(1.0, failed=False)
        self.assertIn("2 tool calls, 1 failed", cost.line())

    def test_a_clean_turn_says_nothing_about_failures(self):
        """Counterpart to the one above: the words must not appear when nothing failed, or
        'failed' stops carrying information."""
        cost = crow.TurnCost()
        cost.add_round({"predicted_n": 1})
        cost.add_tool(1.0, failed=False)
        line = cost.line()
        self.assertIn("1 tool call", line)
        self.assertNotIn("failed", line)

    def test_a_turn_without_tools_omits_the_split(self):
        line = self._cost(rounds=1).line()
        self.assertNotIn("tools", line)
        self.assertNotIn("tool call", line)

    def test_the_cut_off_survives_into_the_turn_line(self):
        """It used to ride on the per-round line. With that line off by default it would have
        disappeared from a normal session entirely."""
        cost = crow.TurnCost()
        cost.add_round({"predicted_n": 5, "_finish_reason": "length"})
        self.assertIn("CUT OFF at the token budget", cost.line())

    def test_a_finished_turn_does_not_claim_a_cut_off(self):
        cost = crow.TurnCost()
        cost.add_round({"predicted_n": 5, "_finish_reason": "stop"})
        self.assertNotIn("CUT OFF", cost.line())


class StreamReplyTests(unittest.TestCase):
    """The two-stream contract, measured 2026-08-07.

    The server sends thoughts in delta["reasoning_content"] and the answer in
    delta["content"]. Reading content alone discarded 88.2 % of every
    generated character and made ttft include the whole reasoning decode.
    There was no test over stream_reply at all, which is why it survived.
    """

    def _run(self, deltas, conversation=None, usage=None, **kw):
        """Drive stream_reply against a canned SSE stream.

        Returns (text, reasoning, timings, printed). The body the caller would
        have sent is kept in self.sent_body -- what goes on the wire is part of
        the contract, not an implementation detail.
        """
        chunks = [json.dumps({"choices": [{"delta": d}]}) for d in deltas]
        final = {"choices": [], "timings": {"predicted_n": 7}}
        # Its own chunk on purpose: the server sends usage on the last one, and
        # reading it off the same object as the timings would pass a test the
        # real stream would fail.
        chunks.append(json.dumps(final))
        if usage is not None:
            chunks.append(json.dumps({"choices": [], "usage": usage}))
        original = crow._post_stream

        def fake(url, body, key, timeout):
            self.sent_body = body
            return iter(chunks)

        crow._post_stream = fake
        sink = io.StringIO()
        try:
            show = kw.pop("show_reasoning", False)
            text, reasoning, timings = crow.stream_reply(
                conversation if conversation is not None else crow.Conversation("SYS"),
                base_url="http://x/v1", model="crow",
                api_key="k", temperature=0.0, timeout=1.0,
                events=crow.TerminalEvents(out=sink, show_reasoning=show), **kw)
        finally:
            crow._post_stream = original
        return text, reasoning, timings, sink.getvalue()

    def test_reasoning_is_counted_but_not_printed(self):
        """It is 60-90 % of every answer; printed in full it buries the code."""
        _, _, timings, printed = self._run([{"reasoning_content": "let me think"},
                                            {"content": "ANSWER"}])
        self.assertNotIn("let me think", printed)
        self.assertIn("ANSWER", printed)
        self.assertEqual(timings["_reasoning_chars"], len("let me think"))

    def test_reasoning_never_enters_the_returned_text(self):
        """It travels as its own field, never merged into the answer."""
        text, reasoning, _, _ = self._run([{"reasoning_content": "SECRET THOUGHTS"},
                                           {"content": "ANSWER"}])
        self.assertEqual(text, "ANSWER")
        self.assertNotIn("SECRET", text)
        self.assertEqual(reasoning, "SECRET THOUGHTS")

    def test_reasoning_is_returned_whole_across_deltas(self):
        """It arrives in pieces and has to go back in one piece."""
        _, reasoning, _, _ = self._run([{"reasoning_content": "one "},
                                        {"reasoning_content": "two"},
                                        {"content": "A"}])
        self.assertEqual(reasoning, "one two")

    def test_the_request_carries_0731_sampling(self):
        """top_p goes on the wire, not into the server's default.

        0731's card runs agentic work at top_p 0.95 while its own
        generation_config.json says 1.0 -- and llama.cpp has a third default.
        Whichever is right, a measurement must know which one it got, so the
        body carries the value explicitly."""
        self._run([{"content": "hi"}], top_p=0.95)
        self.assertEqual(self.sent_body.get("top_p"), 0.95)

    def test_min_p_rides_explicitly(self):
        """min_p goes on the wire too. unsloth recommends 0.01, llama.cpp
        defaults to 0.05, the card is silent -- whichever is right, a request
        that omits the field inherits a value nobody chose."""
        self._run([{"content": "hi"}], min_p=0.01)
        self.assertEqual(self.sent_body.get("min_p"), 0.01)

    def test_reasoning_effort_rides_as_template_kwargs(self):
        """The effort level lands in the template, and ONLY when asked for.

        Sent: chat_template_kwargs carries exactly the key. Not sent: the field
        is absent entirely -- the template treats missing as "low", and an
        empty dict would still change the request against every client that
        predates the switch."""
        self._run([{"content": "hi"}], reasoning_effort="max")
        self.assertEqual(self.sent_body.get("chat_template_kwargs"),
                         {"reasoning_effort": "max"})
        self._run([{"content": "hi"}])
        self.assertNotIn("chat_template_kwargs", self.sent_body)

    def test_the_request_carries_tools(self):
        """Without them this model's template drops a replayed reasoning field
        and both variants render byte for byte the same -- measured 2026-08-08
        via /apply-template, 132 characters either way."""
        self._run([{"content": "A"}])
        self.assertTrue(self.sent_body.get("tools"), "no tools -- the replay would be inert")
        names = [t["function"]["name"] for t in self.sent_body["tools"]]
        self.assertIn("read_file", names)

    def test_ttft_counts_the_first_token_of_any_kind(self):
        """The defect: ttft used to start at the first CONTENT token, so it
        silently contained the entire thinking phase."""
        _, _, timings, _ = self._run([{"reasoning_content": "x" * 50},
                                      {"content": "A"}])
        self.assertIn("_client_ttft_s", timings)
        self.assertIn("_client_answer_s", timings)
        self.assertLessEqual(timings["_client_ttft_s"], timings["_client_answer_s"])

    def test_thinking_share_is_reported(self):
        _, _, timings, _ = self._run([{"reasoning_content": "1234567890" * 9},
                                      {"content": "1234567890"}])
        self.assertEqual(timings["_reasoning_chars"], 90)
        self.assertEqual(timings["_content_chars"], 10)
        self.assertIn("thinking 90%", crow.format_timings(timings))

    def test_a_reply_without_reasoning_still_works(self):
        """Endpoints that do not split the field must behave exactly as before."""
        text, reasoning, timings, printed = self._run([{"content": "PLAIN"}])
        self.assertEqual(text, "PLAIN")
        self.assertEqual(reasoning, "")
        self.assertIn("PLAIN", printed)
        self.assertNotIn("_reasoning_chars", timings)
        self.assertNotIn("thinking", crow.format_timings(timings))

    def test_server_timings_survive(self):
        _, _, timings, _ = self._run([{"content": "A"}])
        self.assertEqual(timings["predicted_n"], 7)

    def test_turn_two_sends_turn_ones_thoughts_back(self):
        """The case #60 asks for: red on the code of 206da71.

        Measured 2026-08-08 over ten-turn sessions: leaving the field out costs
        the size of the previous turn's output on EVERY turn -- 55.0 s against
        33.3 s of total prefill on short answers, 242.3 s against 1.6 s on a
        turn that had generated 2046 tokens.
        """
        conversation = crow.Conversation("SYS")
        conversation.append("user", "one")
        text, reasoning, _, _ = self._run(
            [{"reasoning_content": "THOUGHTS OF TURN ONE"}, {"content": "ANSWER ONE"}],
            conversation=conversation)
        conversation.append("assistant", text, reasoning)
        conversation.append("user", "two")

        self._run([{"content": "ANSWER TWO"}], conversation=conversation)
        assistant = [m for m in self.sent_body["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["reasoning_content"], "THOUGHTS OF TURN ONE")
        self.assertEqual(assistant[0]["content"], "ANSWER ONE")


class ShowReasoningTests(unittest.TestCase):
    """E10: the CLI can show the thinking, and by default it still does not.

    UNTIL THIS STAGE THE REASONING WAS SHOWN NOWHERE. It was read, counted and
    sent back, and 60-90 % of everything the model produced never reached the
    user -- 88.2 % of every generated character over the 2026-08-07 reference
    run. That is tolerable while nothing else can display it either; it stops
    being tolerable the moment a window can, because then the terminal is the
    second-class client of the same core, which is the one thing #90's
    acceptance criterion refuses.

    THE DEFAULT DOES NOT MOVE. Off, this file's other 300-odd cases print what
    they always printed, and the first case below is the promise in one line.
    """

    # The same shape as the core's RE_ENTRY fixture, and for the same reason:
    # think, answer, think AGAIN, answer again. A fixture that only thinks first
    # cannot tell the two implementations apart.
    RE_ENTRY = [
        {"reasoning_content": "first I "},
        {"reasoning_content": "consider it"},
        {"content": "ANSWER ONE\n"},
        {"reasoning_content": "wait -- I should check"},
        {"content": "ANSWER TWO\n"},
    ]
    ANSWER = "ANSWER ONE\nANSWER TWO\n"

    def _run(self, deltas, **kw):
        """Drive `crow.stream_reply` -- the wrapper with the terminal on it.

        `prefix=""` on purpose: the prompt is the CLI's, not the stream's, and
        leaving it out makes the screen comparable to the deltas character for
        character, which is what the predicate below needs.
        """
        chunks = [json.dumps({"choices": [{"delta": d}]}) for d in deltas]
        chunks.append(json.dumps({"choices": [], "timings": {"predicted_n": 7}}))
        original = crow._post_stream
        crow._post_stream = lambda url, body, key, timeout: iter(chunks)
        sink = io.StringIO()
        try:
            show = kw.pop("show_reasoning", False)
            text, reasoning, timings = crow.stream_reply(
                crow.Conversation("SYS"), base_url="http://x/v1", model="crow",
                api_key="k", temperature=0.0, timeout=1.0,
                events=crow.TerminalEvents(out=sink, prefix="", show_reasoning=show), **kw)
        finally:
            crow._post_stream = original
        return text, reasoning, timings, sink.getvalue()

    @staticmethod
    def _plain(screen: str) -> str:
        import re as _re
        return _re.sub(r"\033\[[0-9;]*m", "", screen)

    def _split(self, screen: str) -> tuple[str, list[str]]:
        """The screen cut into (everything outside a block, the block bodies).

        A block runs from its `--- thinking` rule to the next full rule of
        dashes, both included. Cutting it out is how "the answer is not inside a
        block" becomes something a test can state about the SCREEN rather than
        about an event log.
        """
        outside, bodies, current = [], [], None
        for line in self._plain(screen).splitlines(keepends=True):
            bare = line.rstrip("\n")
            if current is None and bare.startswith("--- thinking"):
                current = []
                continue
            if current is not None:
                if bare and set(bare) == {"-"}:
                    bodies.append("".join(current))
                    current = None
                else:
                    current.append(line)
                continue
            outside.append(line)
        if current is not None:                      # an unclosed block, which is a defect
            bodies.append("".join(current))
        return "".join(outside), bodies

    def test_without_the_flag_the_terminal_prints_what_it_always_printed(self):
        """IDEMPOTENCE, and it is the whole promise of the stage. The same
        stream through the same wrapper: not one character more than before
        E10. The byte-level half of this ran as a file `diff` against the
        pre-change client over the same fixture."""
        _, _, _, printed = self._run(self.RE_ENTRY)
        self.assertEqual(printed, self.ANSWER)

    def test_the_visible_answer_is_the_sum_of_the_content_deltas(self):
        """THE PREDICATE, and it is not open to interpretation: with the
        reasoning hidden, the length of the visible answer EQUALS the sum of the
        content deltas. A block that leaked one character, a rule drawn for a
        turn that showed nothing, a swallowed newline -- all of them are this
        one number."""
        _, _, _, printed = self._run(self.RE_ENTRY)
        self.assertEqual(len(printed),
                         sum(len(d["content"]) for d in self.RE_ENTRY if "content" in d))

    def test_the_flag_puts_the_thoughts_on_the_screen(self):
        _, _, _, printed = self._run(self.RE_ENTRY, show_reasoning=True)
        self.assertIn("first I consider it", printed)
        self.assertIn("wait -- I should check", printed)

    def test_two_thoughts_are_two_blocks_on_the_screen(self):
        """The re-entry case as the reader sees it. The second block is
        labelled, because a reader who sees one `thinking` and then more
        thinking below the answer has to be told it is a NEW thought and not the
        old one continuing."""
        _, _, _, printed = self._run(self.RE_ENTRY, show_reasoning=True)
        plain = self._plain(printed)
        # "--- thinking ---" and not "--- thinking ": the second block's label
        # starts with the first one's, and a count on the shorter string finds
        # both and says nothing.
        self.assertEqual(plain.count("--- thinking ---"), 1)
        self.assertEqual(plain.count("--- thinking again (2) ---"), 1)
        order = [plain.index(token) for token in
                 ("--- thinking ---", "first I consider it", "ANSWER ONE",
                  "--- thinking again (2) ---", "wait -- I should check", "ANSWER TWO")]
        self.assertEqual(order, sorted(order), "the screen is out of stream order")

    def test_the_answer_is_not_inside_a_block_on_the_screen(self):
        """P3 on the screen itself: cut both blocks out, rules included, and
        what is left has to be the answer WHOLE. A version that never closes the
        first block leaves both answers inside it, and this comparison is what
        notices."""
        _, _, _, printed = self._run(self.RE_ENTRY, show_reasoning=True)
        outside, bodies = self._split(printed)
        self.assertEqual(outside, self.ANSWER)
        self.assertEqual([b.rstrip("\n") for b in bodies],
                         ["first I consider it", "wait -- I should check"])

    def test_a_stream_without_reasoning_gets_no_block_at_all(self):
        """COUNTER-PROBE (b), against false green: the flag ON and nothing to
        show must print an empty screen's worth of block -- that is, none. An
        implementation that opens a block per turn draws an empty container on
        every endpoint that does not split the field."""
        _, _, _, printed = self._run([{"content": "PLAIN\n"}], show_reasoning=True)
        self.assertEqual(printed, "PLAIN\n")
        self.assertNotIn("thinking", printed)

    def test_the_shown_characters_and_the_counted_ones_are_one_number(self):
        """THE SECOND HALF-STATE: the flag shows the reasoning while
        `thinking NN%` goes on counting its own way, and the display and the
        percentage describe the same turn differently.

        The fixture's thoughts carry no newline of their own, so the single
        newline the closing rule needs is the only one stripped here."""
        _, _, timings, printed = self._run(self.RE_ENTRY, show_reasoning=True)
        _, bodies = self._split(printed)
        self.assertEqual(sum(len(b.rstrip("\n")) for b in bodies),
                         timings["_reasoning_chars"])
        self.assertIn("thinking ", crow.format_timings(timings))

    def test_the_flag_is_off_by_default(self):
        self.assertFalse(crow.build_parser().parse_args([]).show_reasoning)

    def test_the_flag_turns_it_on(self):
        self.assertTrue(crow.build_parser().parse_args(["--show-reasoning"]).show_reasoning)

    def test_the_turn_sink_hands_the_switch_down_to_the_stream(self):
        """THE HALF-STATE THE PLAN NAMES FIRST: the state machine in the core
        and the CLI not wiring it, which moves the defect instead of fixing it.
        The switch has to survive both seams -- turn sink, then reply sink."""
        out = io.StringIO()
        events = crow.TerminalTurnEvents(out=out, show_reasoning=True)
        self.assertTrue(events.reply_events()._show)
        self.assertFalse(crow.TerminalTurnEvents(out=out).reply_events()._show)

    def test_repl_carries_the_switch_into_every_turn(self):
        """`/thoughts` flips it BETWEEN turns, so it may not be read once at
        start and remembered inside the sink."""
        self.assertIn("show_reasoning=show_reasoning", inspect.getsource(crow.repl))
        self.assertIn('"/thoughts"', inspect.getsource(crow.run_slash))

    def test_every_command_help_promises_is_handled_in_the_loop(self):
        """The general form of "documented ahead of the code", and the case that
        made it worth writing: /thoughts is offered in two places -- the flag
        and the list -- and neither of them runs it.

        BOTH FUNCTIONS ARE READ, because the commands moved out of repl() on
        2026-08-14 and this test would otherwise have gone red at a refactor
        that changed no behaviour -- while still passing, later, for a command
        that really was unhandled. What it checks is "somewhere in the loop's
        code", and the loop is now two functions: repl() reads the line and
        owns the two that leave, run_slash() owns the rest.
        """
        import re as _re
        source = inspect.getsource(crow.repl) + inspect.getsource(crow.run_slash)
        for command in sorted(set(_re.findall(r"/\w+", crow.HELP))):
            self.assertIn(f'"{command}"', source,
                          f"{command} is in /help and nothing handles it")

    def test_help_and_the_shared_list_name_the_same_commands(self):
        """#94 put the LIST in the core so the window could cover it, and this
        is what stops the two drifting.

        The window builds its answers from `SLASH_COMMANDS`. A command added to
        HELP alone would be documented, handled in the terminal, and unknown to
        the window -- which is exactly the state #94 was filed for. A command
        added to the tuple alone would show up in the window's help and in no
        terminal listing.
        """
        import re as _re
        self.assertEqual(sorted(set(_re.findall(r"/\w+", crow.HELP))),
                         sorted(crow.SLASH_COMMANDS))


class ContextCounterTests(unittest.TestCase):
    """The bar has to grow with the conversation. It used to shrink.

    Measured live on 2026-08-08 before this changed: 4.7k -> 1.3k -> 792 across
    three turns that each added to the context. `context_tokens = prompt_n +
    predicted_n` assigned rather than accumulated, and on a warm cache prompt_n
    is small precisely because nothing had to be re-read.
    """

    USAGE = {"completion_tokens": 20, "prompt_tokens": 29, "total_tokens": 49,
             "prompt_tokens_details": {"cached_tokens": 11}}

    def test_the_servers_own_total_is_used(self):
        self.assertEqual(crow.next_context_tokens(999, {"_context_tokens": 49}), 49)

    def test_the_total_wins_over_the_timing_fields(self):
        """prompt_n is the processed remainder, not the prompt length."""
        self.assertEqual(
            crow.next_context_tokens(0, {"_context_tokens": 49, "prompt_n": 18,
                                         "predicted_n": 20}),
            49)

    def test_without_usage_it_accumulates_rather_than_assigns(self):
        """The old line assigned, which is how the bar ran backwards."""
        first = crow.next_context_tokens(0, {"prompt_n": 300, "predicted_n": 200})
        second = crow.next_context_tokens(first, {"prompt_n": 18, "predicted_n": 200})
        self.assertEqual(first, 500)
        self.assertGreater(second, first)

    def test_nothing_reported_leaves_the_figure_alone(self):
        """An invented number is worse than a stale one."""
        self.assertEqual(crow.next_context_tokens(500, {}), 500)

    def test_two_turns_grow_the_counter(self):
        """The case #60 asks for, red on 206da71 and on 2ee9be0."""
        after_one = crow.next_context_tokens(0, {"_context_tokens": 4659,
                                                 "prompt_n": 403, "predicted_n": 4256})
        after_two = crow.next_context_tokens(after_one, {"_context_tokens": 5939,
                                                         "prompt_n": 18, "predicted_n": 1262})
        self.assertGreater(after_two, after_one,
                           "the bar shrank while the conversation grew")

    def test_the_bar_and_context_read_the_same_number(self):
        """Whatever it counts, /context and the prompt must not disagree."""
        tokens = crow.next_context_tokens(0, {"_context_tokens": 4659})
        self.assertIn("4.7k", crow.format_prompt(tokens, 200000))
        self.assertEqual(tokens, 4659)


class UsageFromTheStreamTests(unittest.TestCase):
    """Getting the number out of a STREAMED response at all."""

    def _stream(self, usage):
        chunks = [json.dumps({"choices": [{"delta": {"content": "A"}}]}),
                  json.dumps({"choices": [], "timings": {"prompt_n": 18, "predicted_n": 20}})]
        if usage is not None:
            chunks.append(json.dumps({"choices": [], "usage": usage}))
        original = crow._post_stream
        sent = {}

        def fake(url, body, key, timeout):
            sent.update(body)
            return iter(chunks)

        crow._post_stream = fake
        try:
            _, _, timings = crow.stream_reply(
                crow.Conversation("SYS"), base_url="http://x/v1", model="crow",
                api_key="k", temperature=0.0, timeout=1.0,
                events=crow.TerminalEvents(out=io.StringIO()))
        finally:
            crow._post_stream = original
        return timings, sent

    def test_the_request_asks_for_usage(self):
        """A streamed response carries no token counts unless this is set."""
        _, sent = self._stream(None)
        self.assertEqual(sent.get("stream_options"), {"include_usage": True})

    def test_total_and_cached_reach_the_caller(self):
        timings, _ = self._stream({"total_tokens": 49, "prompt_tokens": 29,
                                   "prompt_tokens_details": {"cached_tokens": 11}})
        self.assertEqual(timings["_context_tokens"], 49)
        self.assertEqual(timings["_cached_tokens"], 11)

    def test_an_endpoint_without_usage_still_works(self):
        timings, _ = self._stream(None)
        self.assertNotIn("_context_tokens", timings)
        self.assertEqual(timings["prompt_n"], 18)

    def test_the_cache_reading_is_reported(self):
        timings, _ = self._stream({"total_tokens": 49, "prompt_tokens": 29,
                                   "prompt_tokens_details": {"cached_tokens": 11}})
        self.assertIn("cached 11/29", crow.format_timings(timings))


class RendererTests(unittest.TestCase):
    """Fenced code has to be framed WHILE it streams, not after."""

    def setUp(self):
        """Every render spills into a throwaway directory. A test that writes
        into the repo leaves .crow/ behind on whoever runs the suite."""
        self._tmp = tempfile.mkdtemp(prefix="crow-test-")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _render(self, text, chunk=1):
        sink = io.StringIO()
        r = crow.Renderer(out=sink, spill_dir=self._tmp)
        for i in range(0, len(text), chunk):
            r.feed(text[i:i + chunk])
        r.close()
        return sink.getvalue()

    def test_prose_passes_through(self):
        self.assertIn("hello world", self._render("hello world\n"))

    def test_fence_markers_are_consumed(self):
        out = self._render("```python\nx = 1\n```\n")
        self.assertNotIn("```", out)
        self.assertIn("x = 1", out)

    def test_code_is_set_apart_and_named(self):
        out = self._render("```python\nx = 1\n```\n")
        self.assertIn("python", out)
        self.assertIn("---", out)

    def test_no_prefix_in_front_of_code_lines(self):
        """The defect that shipped: a "| " on every line looked tidy and made
        the block unusable, because selecting it in the terminal copies the
        prefix too. The code line must start with the code."""
        out = self._render("```python\nx = 1\ny = 2\n```\n")
        for line in out.splitlines():
            if "x = 1" in line or "y = 2" in line:
                bare = __import__("re").sub(r"\033\[[0-9;]*m", "", line)
                self.assertTrue(bare.startswith(("x = 1", "y = 2")),
                                f"code line carries a prefix: {bare!r}")

    def test_long_block_is_cut_and_written_to_file(self):
        """Past the threshold the rest goes to a file instead of the scrollback,
        and the FILE holds the whole block, not just the hidden part."""
        body = "\n".join(f"line{i}" for i in range(40))
        out = self._render(f"```python\n{body}\n```\n")
        self.assertIn("line0", out)
        self.assertNotIn("line39", out)
        self.assertIn("more lines", out)
        written = Path(self._tmp) / "block-001.py"
        self.assertTrue(written.is_file())
        saved = written.read_text(encoding="utf-8")
        self.assertIn("line0", saved)
        self.assertIn("line39", saved)

    def test_short_block_is_shown_whole(self):
        """Positive control for the cut: below the threshold nothing is hidden."""
        out = self._render("```python\na = 1\nb = 2\n```\n")
        self.assertIn("a = 1", out)
        self.assertIn("b = 2", out)
        self.assertNotIn("more lines", out)

    def test_short_block_writes_no_file(self):
        """A one-liner like `pip install psutil` must not leave a script behind.
        The file exists to catch long blocks, not every snippet."""
        self._render("```bash\npip install psutil\n```\n")
        self.assertEqual(list(Path(self._tmp).iterdir()), [],
                         "a short block created a file")

    def test_the_file_starts_at_the_first_line_not_the_cut(self):
        """It is written only once the block gets long, so the lines seen
        before that have to be held and handed over - otherwise the saved file
        begins in the middle."""
        body = "\n".join(f"line{i}" for i in range(40))
        self._render(f"```python\n{body}\n```\n")
        saved = (Path(self._tmp) / "block-001.py").read_text(encoding="utf-8")
        self.assertTrue(saved.startswith("line0\n"), saved[:40])
        self.assertEqual(len(saved.splitlines()), 40)

    def test_a_second_turn_does_not_overwrite_the_first(self):
        """The defect: Renderer is built per turn, so `blocks` restarts at 1 and
        turn 2 wrote block-001 over turn 1's answer."""
        for marker in ("first", "second", "third"):
            body = "\n".join(f"{marker}{i}" for i in range(40))
            self._render(f"```python\n{body}\n```\n")

        files = sorted(p.name for p in Path(self._tmp).glob("block-*.py"))
        self.assertEqual(files, ["block-001.py", "block-002.py", "block-003.py"])
        # Each holds its own turn, in order -- the point of the fix.
        for name, marker in zip(files, ("first", "second", "third")):
            with self.subTest(name=name):
                self.assertIn(f"{marker}0", (Path(self._tmp) / name).read_text(encoding="utf-8"))

    def test_blocks_already_on_disk_are_stepped_over(self):
        """A session started where an earlier one left files must not eat them."""
        (Path(self._tmp) / "block-001.py").write_text("from an earlier session", encoding="utf-8")
        body = "\n".join(f"line{i}" for i in range(40))
        self._render(f"```python\n{body}\n```\n")

        self.assertEqual((Path(self._tmp) / "block-001.py").read_text(encoding="utf-8"),
                         "from an earlier session")
        self.assertTrue((Path(self._tmp) / "block-002.py").is_file())

    def test_a_different_language_gets_its_own_numbering(self):
        """The extension is part of the name, so .py and .js do not collide."""
        body = "\n".join(f"line{i}" for i in range(40))
        self._render(f"```python\n{body}\n```\n")
        self._render(f"```javascript\n{body}\n```\n")
        self.assertTrue((Path(self._tmp) / "block-001.py").is_file())
        self.assertTrue((Path(self._tmp) / "block-001.js").is_file())

    def test_the_cut_still_shows_something_happening(self):
        """A block past the cut keeps streaming, sometimes for minutes. Printing
        nothing there looks exactly like a model that stopped mid-block - which
        is how this was first reported. Something must reach the screen."""
        body = "\n".join(f"line{i}" for i in range(40))
        out = self._render(f"```python\n{body}\n```\n")
        after_cut = out.split("line17", 1)[1]
        self.assertTrue(after_cut.strip(), "nothing is printed past the cut")

    def test_same_output_regardless_of_chunk_size(self):
        """The reply arrives token by token; a renderer that only works on
        whole lines would break on the real stream."""
        text = "before\n```js\nconst a = 1;\n```\nafter\n"
        one = self._render(text, chunk=1)
        big = self._render(text, chunk=999)
        self.assertEqual(one, big)

    def test_unterminated_fence_is_closed_on_exit(self):
        """An interrupted answer must not leave the block half-open."""
        out = self._render("```python\nx = 1\n")
        self.assertIn("x = 1", out)
        self.assertFalse(crow.Renderer(out=io.StringIO()).in_code)
        rules = [l for l in out.splitlines() if l.strip().startswith("---")]
        self.assertGreaterEqual(len(rules), 2, "opening and closing rule expected")

    def test_language_is_tracked(self):
        r = crow.Renderer(out=io.StringIO())
        r.feed("```python\n")
        self.assertTrue(r.in_code)
        self.assertEqual(r.language, "python")
        r.feed("```\n")
        self.assertFalse(r.in_code)


class HighlightTests(unittest.TestCase):
    def test_unknown_language_is_untouched(self):
        """A false colour reads as meaning -- worse than no colour."""
        self.assertEqual(crow.highlight("def x(): pass", "brainfuck"),
                         "def x(): pass")

    def test_plain_text_survives_round_trip(self):
        """Whatever is painted, stripping the codes must give the source back."""
        import re as _re
        line = 'def f(x):  # note "quoted" 42'
        painted = crow.highlight(line, "python")
        self.assertEqual(_re.sub(r"\033\[[0-9;]*m", "", painted), line)

    def test_keyword_inside_a_string_is_not_a_keyword(self):
        painted = crow.highlight('s = "def class return"', "python")
        stripped = __import__("re").sub(r"\033\[[0-9;]*m", "", painted)
        self.assertEqual(stripped, 's = "def class return"')


class PromptTests(unittest.TestCase):
    """The context counter in the prompt."""

    def test_fresh_session_shows_no_number(self):
        self.assertEqual(crow.format_prompt(0), "you> ")

    def test_small_context_is_exact(self):
        """'0k' would be useless at the start of a session."""
        self.assertEqual(crow.format_prompt(342), "342 | you> ")

    def test_large_context_is_abbreviated(self):
        self.assertEqual(crow.format_prompt(12345), "12.3k | you> ")

    def test_the_number_is_visible_at_all(self):
        """Negative control: a prompt that never shows the count is the bug
        this was added for."""
        self.assertNotEqual(crow.format_prompt(5000), crow.format_prompt(0))

    def test_bar_appears_only_when_the_limit_is_known(self):
        """A bar against an invented limit is worse than no bar."""
        self.assertNotIn("#", crow.format_prompt(5000, 0))
        self.assertIn("#", crow.format_prompt(5000, 10000))

    def test_bar_fills_with_the_share_used(self):
        half = crow.format_prompt(5000, 10000)
        full = crow.format_prompt(9500, 10000)
        self.assertLess(half.count("#"), full.count("#"))
        self.assertIn("/10k", half)


class RavenTests(unittest.TestCase):
    def test_label_can_change_while_flapping(self):
        """The bird is the only progress signal now that reasoning is not
        printed -- a fixed label could not show the switch to writing."""
        raven = crow.Raven(stream=io.StringIO())
        raven.set_label("writing code")
        self.assertEqual(raven._label, "writing code")

    def test_silent_when_not_a_terminal(self):
        """Piped output must stay clean -- no frames in captured transcripts."""
        sink = io.StringIO()  # StringIO has no isatty() returning True
        with crow.Raven(stream=sink):
            pass
        self.assertEqual(sink.getvalue(), "")

    def test_disabled_by_environment_variable(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        sink = FakeTTY()
        import os

        os.environ["CROW_NO_RAVEN"] = "1"
        try:
            with crow.Raven(stream=sink):
                pass
        finally:
            del os.environ["CROW_NO_RAVEN"]
        self.assertEqual(sink.getvalue(), "")

    def test_a_tty_would_draw(self):
        """Positive control: without the guards the raven does write."""
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        sink = FakeTTY()
        raven = crow.Raven(stream=sink, interval=0.01)
        self.assertTrue(raven._enabled, "raven should be enabled on a tty")

    def test_banner_lines_are_equal_width(self):
        """A ragged wordmark is visible at a glance; the bevel depends on the
        columns lining up."""
        lines = [l for l in crow.BANNER.splitlines() if l.strip() and "{" not in l]
        widths = {len(l.rstrip()) for l in lines}
        self.assertLessEqual(len(widths), 2, f"wordmark is ragged: {sorted(widths)}")

    def test_banner_uses_only_covered_glyphs(self):
        """Every non-ASCII cell in the wordmark must sit in the box drawing or
        the block elements, which the bundled font covers 128 of 128 and 32 of
        32 - measured 2026-08-10 from its cmap, against Cascadia Mono as a
        control. A character outside that range would fall back to another face
        and break the alignment."""
        for ch in crow.BANNER:
            if ord(ch) > 127:
                self.assertTrue(0x2500 <= ord(ch) <= 0x259F,
                                f"U+{ord(ch):04X} is outside the covered range")

    def test_the_gguf_path_reduces_to_the_model_name(self):
        self.assertEqual(
            crow.model_display_name(
                r"C:\models\0731-gguf\UD-IQ3_XXS"
                r"\DeepSeek-V4-Flash-0731-UD-IQ3_XXS-00001-of-00004.gguf"),
            "DeepSeek-V4-Flash-0731")

    def test_a_name_the_patterns_do_not_know_is_left_whole(self):
        """THE NEGATIVE PROBE. A greedy strip would eat part of a name it does
        not recognise, and a header that quietly shortens the model is worse
        than one that shows a suffix -- only one of the two says so."""
        self.assertEqual(crow.model_display_name("Some-Other-Model-v3.gguf"),
                         "Some-Other-Model-v3")
        self.assertEqual(crow.model_display_name(""), "")

    def test_the_header_names_the_loaded_model_not_the_sent_label(self):
        """`--model` is a label in the request body. Printing it would confirm
        the client's own argument while the server ran something else."""
        real = crow.urllib.request.urlopen
        try:
            crow.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
                OSError("no server"))
            self.assertEqual(crow.fetch_model_name("http://127.0.0.1:8081/v1"), "")
        finally:
            crow.urllib.request.urlopen = real

    def test_every_command_sits_beside_the_wordmark(self):
        lines = crow.header_lines("v9.9.9")
        for name, what in crow.HEADER_COMMANDS:
            carrier = [l for l in lines if name in l]
            self.assertEqual(len(carrier), 1, f"{name} is not on exactly one line")
            self.assertIn(what, carrier[0])
            self.assertIn("█", carrier[0], f"{name} is not beside the wordmark")

    def test_the_commands_start_in_one_column(self):
        """A ragged right block is the same defect as a ragged wordmark, and it
        is invisible in a diff. The column is measured off the widest banner row,
        so this fails the moment the mark changes width without the padding
        following it."""
        import re as _re
        bare = _re.compile(r"\033\[[0-9;]*m")
        plain = [bare.sub("", l) for l in crow.header_lines("v9.9.9")]
        starts = {l.index(name) for l in plain
                  for name, _ in crow.HEADER_COMMANDS if name in l}
        self.assertEqual(len(starts), 1, f"commands do not line up: {sorted(starts)}")

    def test_the_version_line_carries_no_command(self):
        """Centred against the wordmark, not against the block -- otherwise the
        third command lands next to the version and reads as part of it."""
        carrier = [l for l in crow.header_lines("v9.9.9") if "v9.9.9" in l]
        self.assertEqual(len(carrier), 1)
        for name, _ in crow.HEADER_COMMANDS:
            self.assertNotIn(name, carrier[0])

    def _version_line(self):
        lines = crow.header_lines("v9.9.9")
        carrier = [l for l in lines if "v9.9.9" in l]
        self.assertEqual(len(carrier), 1)
        return carrier[0]

    def test_the_version_line_carries_nothing_from_the_column(self):
        """The command check above is not the whole invariant.

        The right column is commands + a blank + the repository URL, and the
        centring is budgeted against the wordmark's five rows. The URL is the
        LAST entry, so it is the one that falls off the bottom first -- and
        `test_the_version_line_carries_no_command` iterates commands only, so it
        stays green while the URL sits beside the version. Found on 2026-08-14
        when /mode became the fourth command: four fit, five do not, and nothing
        said so.
        """
        self.assertNotIn(crow.REPO_URL, self._version_line())

    def test_a_fifth_command_pushes_the_url_onto_the_version(self):
        """NEGATIVE CONTROL for the test above, and the reason it is worth having.

        Without this, the assertion is one that has never been seen red, so it
        cannot be told apart from one that cannot go red. Five commands is the
        case the comment at HEADER_COMMANDS warns about; if this ever stops
        failing, the wordmark grew and the budget in that comment is stale.
        """
        was = crow.HEADER_COMMANDS
        try:
            crow.HEADER_COMMANDS = was + (("/spare", "one too many"),)
            self.assertIn(crow.REPO_URL, self._version_line())
        finally:
            crow.HEADER_COMMANDS = was

    def test_bevel_is_painted_apart_from_the_face(self):
        """Positive control for paint_banner: without a separate colour on the
        shadow cells the wordmark is flat, and the test would not notice."""
        painted = crow.paint_banner(crow.BANNER)
        if crow._TTY:
            self.assertIn(crow.BANNER_BEVEL, painted)
        for ch in crow.BANNER_SHADE:
            self.assertIn(f"{crow.BANNER_BEVEL}{ch}" if crow._TTY else ch, painted,
                          f"shadow cell {ch!r} is not painted apart from the face")


class ParserTests(unittest.TestCase):
    def test_defaults_point_at_the_local_endpoint(self):
        args = crow.build_parser().parse_args([])
        self.assertEqual(args.base_url, "http://127.0.0.1:8081/v1")
        self.assertEqual(args.model, "crow")

    def test_default_temperature_is_not_greedy(self):
        """Measured 2026-08-07: at 0.0 the model looped inside its reasoning
        block on a three.js task and never reached the answer. Greedy decoding
        walks into a repetition attractor and cannot leave it."""
        self.assertGreater(crow.build_parser().parse_args([]).temperature, 0.0)

    def test_temperature_can_still_be_pinned_to_zero(self):
        """Measurement runs need byte-identical output and must be able to
        ask for it explicitly."""
        args = crow.build_parser().parse_args(["--temperature", "0"])
        self.assertEqual(args.temperature, 0.0)

    def test_base_url_override(self):
        args = crow.build_parser().parse_args(["--base-url", "http://x:9/v1", "-m", "other"])
        self.assertEqual(args.base_url, "http://x:9/v1")
        self.assertEqual(args.model, "other")

    def test_a_system_prompt_is_sent_by_default(self):
        """Without one the model answered in Chinese to an English 'yo'."""
        args = crow.build_parser().parse_args([])
        self.assertEqual(args.system, crow.DEFAULT_SYSTEM)
        self.assertIn("same language", args.system)

    def test_no_system_switches_it_off(self):
        args = crow.build_parser().parse_args(["--no-system"])
        self.assertIsNone(args.system)

    def test_explicit_system_wins(self):
        args = crow.build_parser().parse_args(["--system", "CUSTOM"])
        self.assertEqual(args.system, "CUSTOM")

    def test_default_system_is_one_line(self):
        """It is prefilled on every cold start; keep it cheap."""
        self.assertNotIn("\n", crow.DEFAULT_SYSTEM)
        self.assertLess(len(crow.DEFAULT_SYSTEM), 200)


class EndpointFailureTests(unittest.TestCase):
    def test_dead_port_raises_crow_error(self):
        """Negative control: a port nothing listens on must not look healthy."""
        with self.assertRaises(crow.CrowError):
            crow.check_endpoint("http://127.0.0.1:9/v1", timeout=2.0)


class FontTests(unittest.TestCase):
    """Nothing here installs anything: the tests never touch the font store or
    the registry. What they cover is the file side and the failure modes."""

    def test_bundled_faces_are_present(self):
        names = crow.font_files()
        self.assertTrue(names, "cli/fonts carries no .ttf - the bundle is empty")
        self.assertTrue(any("GoogleSansCode" in n for n in names))

    def test_licence_travels_with_the_font(self):
        """OFL 1.1 permits bundling only if the licence ships with it. A missing
        OFL.txt makes the redistribution non-compliant, and nothing else notices."""
        self.assertTrue((Path(crow.FONT_DIR) / "OFL.txt").is_file())

    def test_only_font_files_are_listed(self):
        """OFL.txt sits in the same directory and must not be handed to the
        installer as a face."""
        self.assertNotIn("OFL.txt", crow.font_files())

    def test_empty_directory_yields_no_faces(self):
        """Negative control: with no directory there are no faces, and the
        installer has to say so rather than report success over nothing."""
        old = crow.FONT_DIR
        try:
            crow.FONT_DIR = str(Path(old) / "does-not-exist")
            self.assertEqual(crow.font_files(), [])
        finally:
            crow.FONT_DIR = old

    def test_ensure_font_is_silent_and_safe_without_a_bundle(self):
        """It runs on every start before the first prompt. On a tree without
        fonts it must do nothing and must not raise - a typeface may never keep
        the CLI from starting."""
        old = crow.FONT_DIR
        try:
            crow.FONT_DIR = str(Path(old) / "does-not-exist")
            crow.ensure_font()
        finally:
            crow.FONT_DIR = old

    def test_install_reports_failure_without_a_bundle(self):
        """Negative control: no faces means no success. A zero here would let
        ensure_font print 'installed' over an empty directory."""
        old = crow.FONT_DIR
        try:
            crow.FONT_DIR = str(Path(old) / "does-not-exist")
            self.assertNotEqual(crow.install_font(), 0)
        finally:
            crow.FONT_DIR = old

    def test_face_name_is_the_instance_not_the_family(self):
        """The defect that shipped: "Google Sans Code" is the typographic family
        in the file, but the variable font resolves into named instances and
        Windows registers THOSE. Asking for the family gets the "font not found"
        dialog. Measured 2026-08-07 from the installed families."""
        self.assertEqual(crow.FONT_FAMILY, "Google Sans Code Monospace")

    def test_the_old_wrong_face_may_be_corrected(self):
        """A face we wrote ourselves gets fixed; anything else is the user's."""
        self.assertIn("Google Sans Code", crow._OUR_OLD_FACES)
        self.assertNotIn("Cascadia Mono", crow._OUR_OLD_FACES)

    def test_font_install_is_on_by_default_and_can_be_declined(self):
        """A font nobody knows to ask for never gets installed, so it happens on
        first start. --no-font is the way out for anyone who does not want it."""
        self.assertTrue(crow.build_parser().parse_args([]).font)
        self.assertFalse(crow.build_parser().parse_args(["--no-font"]).font)


class SpinnerTests(unittest.TestCase):
    def test_four_frames_of_one_cell(self):
        """Every frame must be exactly one cell wide, or the line jitters."""
        self.assertEqual(len(crow.SPINNER_FRAMES), 4)
        for f in crow.SPINNER_FRAMES:
            self.assertEqual(len(f), 1)

    def test_frames_are_block_elements_not_braille(self):
        """Measured 2026-08-07: the bundled Google Sans Code has 0 of 256 braille
        codepoints and 32 of 32 block elements. A braille spinner would swap to a
        substitute face mid-animation and the cell advance would jump with it."""
        for f in crow.SPINNER_FRAMES:
            cp = ord(f)
            self.assertTrue(0x2580 <= cp <= 0x259F,
                            f"U+{cp:04X} is outside the block elements")
            self.assertFalse(0x2800 <= cp <= 0x28FF, "braille is not covered")

    def test_animation_occupies_a_single_line(self):
        """It sits where the prompt sits; three lines would push the conversation
        up the screen on every turn."""
        self.assertEqual(crow.Raven.HEIGHT, 1)


class BackgroundTests(unittest.TestCase):
    def test_background_is_on_by_default_and_can_be_declined(self):
        self.assertTrue(crow.build_parser().parse_args([]).background)
        self.assertFalse(crow.build_parser().parse_args(["--no-background"]).background)

    def test_brand_colours_are_the_measured_values(self):
        """The blue of the wordmark and a white reply. Truecolour, so the user's
        theme cannot reinterpret them."""
        self.assertEqual(crow.CROW_BG, "#0b0e17")
        if crow._TTY:
            self.assertIn("126;176;248", crow.CROW_ACCENT)
            self.assertIn("255;255;255", crow.CROW_TEXT)

    def test_reset_is_reachable_without_a_terminal(self):
        """It runs from the finally in main(); it must never raise there."""
        crow.reset_background()


class JsoncTests(unittest.TestCase):
    """settings.json ships WITH comments. Stripping them wrongly is how an
    editor eats a user's configuration, so the cases are pinned down here."""

    def test_line_and_block_comments_go(self):
        src = '{\n  // a\n  "x": 1, /* b */\n  "y": 2\n}'
        self.assertEqual(json.loads(crow._strip_jsonc(src)), {"x": 1, "y": 2})

    def test_slashes_inside_strings_survive(self):
        """The case that breaks a naive regex: a path or URL is not a comment."""
        src = '{"p": "C:\\\\x//y", "u": "https://example.com/a"}'
        got = json.loads(crow._strip_jsonc(src))
        self.assertEqual(got["p"], "C:\\x//y")
        self.assertEqual(got["u"], "https://example.com/a")

    def test_escaped_quote_does_not_end_the_string(self):
        src = '{"q": "he said \\" // not a comment"}'
        self.assertIn("//", json.loads(crow._strip_jsonc(src))["q"])


class VersionCompareTests(unittest.TestCase):
    """The update notice is only as good as this comparison.

    Its failure mode is not a crash: it is a line that tells every user on the
    newest build that they are out of date, on every single start. That is worse
    than no notice at all, because it trains people to ignore the one that matters.
    """

    def test_a_higher_patch_is_newer(self):
        self.assertTrue(crow.is_newer("0.0.4", "0.0.3"))

    def test_a_higher_minor_is_newer(self):
        self.assertTrue(crow.is_newer("0.1.0", "0.0.9"))

    def test_ten_beats_nine(self):
        """String comparison gets this wrong: "0.0.10" < "0.0.9" as text."""
        self.assertTrue(crow.is_newer("0.0.10", "0.0.9"))

    def test_a_v_prefix_is_tolerated(self):
        """Release tags carry it, the VERSION constant does not."""
        self.assertTrue(crow.is_newer("v0.0.4", "0.0.3"))

    def test_shorter_and_longer_forms_compare(self):
        self.assertTrue(crow.is_newer("0.1", "0.0.9"))
        self.assertFalse(crow.is_newer("0.0.3", "0.0.3.0"))

    # The half that must go red. Each of these, answered the other way, puts a
    # permanent "update available" in front of somebody who is already current.
    def test_the_same_version_is_not_newer(self):
        self.assertFalse(crow.is_newer("0.0.3", "0.0.3"))

    def test_an_older_version_is_not_newer(self):
        self.assertFalse(crow.is_newer("0.0.2", "0.0.3"))

    def test_garbage_is_never_newer(self):
        for junk in ("", "latest", "0.0.x", "main", "0..1", "1.2.3.4.5", None):
            with self.subTest(junk=junk):
                self.assertFalse(crow.is_newer(junk or "", "0.0.3"))

    def test_an_unparseable_current_version_silences_the_check(self):
        """If we cannot read our OWN version, we have nothing to compare against."""
        self.assertFalse(crow.is_newer("9.9.9", "not-a-version"))

    def test_parse_returns_none_rather_than_zeroes(self):
        """(0,0,0) would sort below every release and announce an update always."""
        self.assertIsNone(crow.parse_version("not-a-version"))
        self.assertEqual(crow.parse_version("1.2.3"), (1, 2, 3))


class UpdateNoticeTests(unittest.TestCase):
    """What the user actually sees, including when they must see nothing."""

    @staticmethod
    def _answered(value):
        import queue
        q = queue.Queue(maxsize=1)
        q.put(value)
        return q

    def test_a_newer_release_names_the_command(self):
        line = crow.update_notice(self._answered("9.9.9"), wait=0.01)
        self.assertIsNotNone(line)
        self.assertIn("9.9.9", line)
        self.assertIn(crow.UPDATE_COMMAND, line)

    def test_the_current_version_says_nothing(self):
        self.assertIsNone(crow.update_notice(self._answered(crow.VERSION), wait=0.01))

    def test_a_failed_lookup_says_nothing(self):
        """fetch_latest_version returns None on every error; None is not a notice."""
        self.assertIsNone(crow.update_notice(self._answered(None), wait=0.01))

    def test_a_disabled_check_says_nothing(self):
        self.assertIsNone(crow.update_notice(None, wait=0.01))

    def test_a_slow_answer_does_not_hold_the_start(self):
        """An empty queue must time out and yield, not block on the network."""
        import queue
        import time
        started = time.monotonic()
        self.assertIsNone(crow.update_notice(queue.Queue(maxsize=1), wait=0.05))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_check_can_be_switched_off_from_the_command_line(self):
        args = crow.build_parser().parse_args(["--no-update-check"])
        self.assertFalse(args.update_check)
        self.assertIsNone(crow.start_update_check(args.update_check))

    def test_it_is_on_by_default(self):
        self.assertTrue(crow.build_parser().parse_args([]).update_check)


class ToolArgLineTests(unittest.TestCase):
    """The one line that says what a tool call is doing.

    It replaced a raw JSON cut at 80 characters, which landed mid-string in the common case and
    read as a malformed call rather than a shortened one.
    """

    def test_a_path_keeps_its_file_name(self):
        line = crow.format_tool_args(json.dumps(
            {"path": r"C:\Users\robin\dev\Crow\tools\manifest-runs.ps1", "start_line": 1, "end_line": 60}))
        self.assertIn("manifest-runs.ps1", line)
        self.assertIn("start_line=1", line)
        self.assertIn("end_line=60", line)

    def test_no_dangling_json(self):
        """The actual defect: the old output ended in `,"` and looked broken."""
        line = crow.format_tool_args(json.dumps(
            {"path": r"C:\Users\robin\dev\Crow\tools\manifest-runs.ps1", "start_line": 1}))
        self.assertFalse(line.rstrip().endswith(',"'))
        self.assertNotIn('{"', line)

    def test_a_long_text_argument_is_summarised_not_shown(self):
        line = crow.format_tool_args(json.dumps({"path": "a.txt", "content": "x" * 500}))
        self.assertIn("path=a.txt", line)
        self.assertIn("<500 chars>", line)
        self.assertNotIn("xxxxxxxxxx", line)

    def test_broken_json_still_produces_something(self):
        """Arguments arrive over a stream and may be cut off. A half-object must not raise."""
        line = crow.format_tool_args('{"path":"C:\\\\tmp\\\\a.txt","start')
        self.assertTrue(line)
        self.assertTrue(line.endswith("...") or len(line) <= 78)

    def test_empty_and_none(self):
        self.assertEqual(crow.format_tool_args(None), "")
        self.assertEqual(crow.format_tool_args(""), "")

    def test_the_line_stays_within_its_width(self):
        line = crow.format_tool_args(json.dumps({f"k{i}": f"value{i}" for i in range(40)}), width=78)
        self.assertLessEqual(len(line), 81)  # 78 plus the ellipsis


class ForgettingASessionTests(unittest.TestCase):
    """`/reset` has to reach the disk, and `save_session` will not take it there.

    MEASURED 2026-08-14, and true since `/reset` existed: robin dropped the
    context in the window and closed it. `save_session` refuses a conversation
    with nothing in it -- right for the case it was written for, a client closed
    without a word -- so it wrote nothing, `session.json` still held the three
    messages the last turn had put there, and the next start restored the
    conversation he had just dropped. Both surfaces, because the guard is in the
    core.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crow-forget-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "session.json")

    def _write_one(self):
        talk = crow.Conversation("SYS")
        talk.append("user", "something worth keeping")
        talk.append("assistant", "kept")
        crow.save_session(talk, "http://127.0.0.1:1/v1", 1100,
                          path=self.path, with_kv=False)
        return talk

    def test_a_written_session_is_removed_and_reported(self):
        self._write_one()
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(crow.forget_session(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_a_session_that_was_never_there_is_not_an_error(self):
        """`/reset` on a first run reaches this, and it is not a failure."""
        self.assertFalse(crow.forget_session(self.path))

    def test_the_defect_it_exists_for(self):
        """THE MEASUREMENT, kept as a case so the reason cannot be argued away.

        Emptying the conversation and saving does NOT clear the file: the guard
        returns None and the old messages stay. That is correct for its own
        purpose and wrong for a reset, which is why the removal is a separate
        call rather than a change to the guard.
        """
        talk = self._write_one()
        talk.reset()
        self.assertIsNone(crow.save_session(talk, "http://127.0.0.1:1/v1", 0,
                                            path=self.path, with_kv=False))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["messages"]), 3,
                             "the guard changed; the reset no longer needs help")

    def test_a_conversation_with_something_in_it_still_writes(self):
        """NEGATIVE HALF. A fix that made every save a removal would pass the
        cases above and lose every session anyone ever had."""
        self._write_one()
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["messages"]), 3)

    def _at_the_default_path(self):
        """`forget_session()` with no argument reads crow_core's OWN binding.

        `crow.SESSION_FILE` is a second name for the same string, and rebinding
        it leaves the core resolving the original -- the trap ToolLayerCase
        writes out at length for `_READ`. So the core's is what moves here.
        """
        import crow_core     # this suite drives `crow`; the binding is the core's
        before = crow_core.SESSION_FILE
        crow_core.SESSION_FILE = self.path
        self.addCleanup(setattr, crow_core, "SESSION_FILE", before)

    def test_the_terminal_reset_forgets_it_too(self):
        """The guard is in the core, so the CLI had the same defect and the same
        fix. Driven through `run_slash` rather than asserted about its source."""
        self._at_the_default_path()
        talk = self._write_one()
        crow.run_slash("/reset", conversation=talk, mode="auto",
                       show_reasoning=False, context_tokens=1100,
                       n_ctx=200000, rollover_at=0.9)
        self.assertFalse(os.path.exists(self.path))

    def test_the_terminal_leaves_it_alone_without_sessions(self):
        """NEGATIVE HALF of the line above: `--no-session` means the file is not
        this client's to delete."""
        self._at_the_default_path()
        talk = self._write_one()
        crow.run_slash("/reset", conversation=talk, mode="auto",
                       show_reasoning=False, context_tokens=1100,
                       n_ctx=200000, rollover_at=0.9, session=False)
        self.assertTrue(os.path.exists(self.path))


class SessionRestoreTests(unittest.TestCase):
    """What load_session does when the server cannot produce the KV state.

    The case is real rather than theoretical: point llama-server at a different --slot-save-path
    than the one a session was written to, and every start prints two red error lines. Nothing was
    broken by it - the messages still load - but it repeated forever, because the file kept
    claiming a cache that was gone.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._real_dir, self._real_file = crow.SESSION_DIR, crow.SESSION_FILE
        crow.SESSION_DIR = self.dir
        crow.SESSION_FILE = str(Path(self.dir) / "session.json")

    def tearDown(self):
        crow.SESSION_DIR, crow.SESSION_FILE = self._real_dir, self._real_file
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, kv: bool, system=None):
        with open(crow.SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump({"version": crow.VERSION, "kv": kv, "context_tokens": 42,
                       "prefix": crow.prefix_fingerprint(system),
                       "messages": [{"role": "user", "content": "hi"}]}, fh)

    def _stored_kv(self):
        with open(crow.SESSION_FILE, encoding="utf-8") as fh:
            return json.load(fh)["kv"]

    # self.fail() inside the fake post_json CANNOT work here, and finding that out is the reason
    # this comment exists: load_session wraps the call in `except Exception`, which swallows the
    # AssertionError that fail() raises. The test then passes while the thing it forbids happened.
    # Counting the calls puts the assertion outside that except, where it can be seen.
    def _counting_post(self, raises=None):
        calls = []

        def fake(*a, **k):
            calls.append(a[0] if a else None)
            if raises:
                raise raises
            return {}

        crow.post_json = fake
        return calls

    def test_no_session_file_means_no_request_at_all(self):
        """The first start after an install must not talk to /slots. That is why a new user
        never sees the error in the first place."""
        calls = self._counting_post()
        self.assertIsNone(crow.load_session("http://127.0.0.1:8081"))
        self.assertEqual(calls, [], "no session file must mean no request")

    def test_a_failed_restore_withdraws_the_claim(self):
        self._write(kv=True)
        crow.post_json = lambda *a, **k: (_ for _ in ()).throw(OSError("no such file"))
        result = crow.load_session("http://127.0.0.1:8081")
        self.assertIsNotNone(result)
        messages, tokens, kv = result
        self.assertEqual(len(messages), 1, "the messages survive - they are still worth a prefill")
        self.assertEqual(tokens, 42)
        self.assertFalse(kv)
        self.assertFalse(self._stored_kv(), "the file must no longer claim a warm cache")

    def test_the_second_start_sends_nothing(self):
        """The point of the whole change: the error happens once, not on every start."""
        self._write(kv=True)
        first = self._counting_post(raises=OSError("no such file"))
        crow.load_session("http://127.0.0.1:8081")
        self.assertEqual(len(first), 1, "the first start must try once")

        second = self._counting_post(raises=OSError("no such file"))
        self.assertIsNotNone(crow.load_session("http://127.0.0.1:8081"))
        self.assertEqual(second, [], "the second start must not try again")

    def test_a_working_restore_keeps_the_claim(self):
        """The negative control. If this passed while the code always cleared the flag, the
        test above would prove nothing."""
        self._write(kv=True)
        crow.post_json = lambda *a, **k: {}
        _, _, kv = crow.load_session("http://127.0.0.1:8081")
        self.assertTrue(kv)
        self.assertTrue(self._stored_kv())

    def test_an_unwritable_session_file_does_not_break_the_start(self):
        """Correcting a cache hint is not worth refusing to start over."""
        self._write(kv=True)
        crow.post_json = lambda *a, **k: (_ for _ in ()).throw(OSError("no such file"))
        real_open = builtins.open

        def deny(path, mode="r", *a, **k):
            if str(path) == crow.SESSION_FILE and "w" in mode:
                raise PermissionError("read-only")
            return real_open(path, mode, *a, **k)

        builtins.open = deny
        try:
            result = crow.load_session("http://127.0.0.1:8081")
        finally:
            builtins.open = real_open
        self.assertIsNotNone(result)
        self.assertFalse(result[2])


def _keys(text: str):
    """A keystroke source for read_coloured, ending in Enter."""
    seq = list(text)

    def getch() -> str:
        return seq.pop(0) if seq else "\r"

    return getch


class ToolsListingTests(unittest.TestCase):
    """/tools is derived from TOOLS, so the two cannot drift apart."""

    def test_every_registered_tool_is_listed(self):
        listing = crow.format_tools()
        for entry in crow.TOOLS:
            self.assertIn(entry["function"]["name"], listing)

    def test_the_count_matches_the_registry(self):
        self.assertIn(f"{len(crow.TOOLS)} tools", crow.format_tools())

    def test_a_tool_added_to_the_registry_appears(self):
        """The listing reads the schema; it is not a second list kept by hand."""
        extra = crow._fn("zzz_probe", "A probe tool.", {}, [])
        self.assertIn("zzz_probe", crow.format_tools(crow.TOOLS + [extra]))

    def test_a_tool_that_is_not_registered_does_not_appear(self):
        """The case that must fail: a hand-written listing would not care."""
        self.assertNotIn("zzz_probe", crow.format_tools())

    def test_required_arguments_are_bare_and_optional_ones_bracketed(self):
        listing = crow.format_tools()
        for line in listing.splitlines():
            if line.strip().startswith("read_file"):
                self.assertIn("path", line)
                self.assertIn("[start_line]", line)
                self.assertNotIn("[path]", line)
                return
        self.fail("read_file was not listed at all")

    def test_only_the_first_sentence_of_a_description_is_shown(self):
        listing = crow.format_tools()
        self.assertIn("Read a UTF-8 text file.", listing)
        self.assertNotIn("search_text returns line numbers", listing)

    def test_an_empty_registry_says_so_instead_of_crashing(self):
        self.assertIn("no tools", crow.format_tools([]))


class CommandSurfaceTests(unittest.TestCase):
    """What the header and /help promise has to exist."""

    def test_tools_is_offered_in_help(self):
        self.assertIn("/tools", crow.HELP)

    def test_help_does_not_promise_a_command_that_was_never_built(self):
        """The case that must fail if someone documents ahead of the code."""
        self.assertNotIn("/models", crow.HELP)

    def test_the_repository_is_spelled_once(self):
        """A rename has to move one literal, not three."""
        self.assertIn(crow.REPO, crow.REPO_URL)
        self.assertIn(crow.REPO, crow.UPDATE_COMMAND)
        self.assertIn(crow.REPO, crow.RELEASES_API)

    def test_the_repo_url_is_a_github_page_not_an_api_endpoint(self):
        self.assertTrue(crow.REPO_URL.startswith("https://github.com/"))
        self.assertNotIn("api.github.com", crow.REPO_URL)


class ColouredInputTests(unittest.TestCase):
    """A slash command turns yellow while it is typed, a message does not."""

    def setUp(self):
        self._saved = (crow.YELLOW, crow.CROW_TEXT, crow.RESET)
        # Sentinels rather than real escapes: this guards WHEN a colour is
        # emitted, which is the part that broke, not which byte it is.
        crow.YELLOW, crow.CROW_TEXT, crow.RESET = "<Y>", "<W>", "<R>"

    def tearDown(self):
        crow.YELLOW, crow.CROW_TEXT, crow.RESET = self._saved

    def _typed(self, text):
        out = io.StringIO()
        line = crow.read_coloured("you> ", _keys(text), out)
        return line, out.getvalue()

    def test_a_slash_command_is_returned_unchanged(self):
        line, _ = self._typed("/tools")
        self.assertEqual(line, "/tools")

    def test_the_colour_is_emitted_before_the_slash_is_echoed(self):
        _, painted = self._typed("/tools")
        self.assertLess(painted.index("<Y>"), painted.index("/"))

    def test_a_plain_message_is_never_painted_yellow(self):
        """The case that must fail if the trigger is ever widened."""
        line, painted = self._typed("hello there")
        self.assertEqual(line, "hello there")
        self.assertNotIn("<Y>", painted)

    def test_a_slash_that_is_not_first_does_not_paint(self):
        _, painted = self._typed("read a/b")
        self.assertNotIn("<Y>", painted)

    def test_deleting_the_slash_leaves_yellow_again(self):
        line, painted = self._typed("/\x08x")
        self.assertEqual(line, "x")
        self.assertLess(painted.index("<Y>"), painted.index("<W>"))

    def test_backspace_on_an_empty_line_is_harmless(self):
        line, _ = self._typed("\x08\x08hi")
        self.assertEqual(line, "hi")

    def test_the_line_always_ends_with_a_reset(self):
        _, painted = self._typed("/tools")
        self.assertIn("<R>", painted)

    def test_ctrl_c_raises_so_the_caller_can_leave(self):
        with self.assertRaises(KeyboardInterrupt):
            self._typed("/too\x03ls")

    def test_ctrl_d_on_an_empty_line_is_end_of_file(self):
        with self.assertRaises(EOFError):
            self._typed("\x04")

    def test_ctrl_d_with_text_typed_is_ignored(self):
        """Otherwise a stray Ctrl+D would end a session mid-sentence."""
        line, _ = self._typed("hi\x04there")
        self.assertEqual(line, "hithere")

    def test_a_key_without_a_character_is_ignored(self):
        """Arrow keys reach the reader as "" and must not land in the buffer."""
        out = io.StringIO()
        seq = ["/", "", "t", ""]

        def getch():
            return seq.pop(0) if seq else "\r"

        self.assertEqual(crow.read_coloured("you> ", getch, out), "/t")

    def test_control_characters_do_not_reach_the_buffer(self):
        line, _ = self._typed("a\tb")
        self.assertEqual(line, "ab")

    def test_the_prompt_is_written_before_anything_is_read(self):
        _, painted = self._typed("hi")
        self.assertTrue(painted.startswith("you> "))


class RawKeyFallbackTests(unittest.TestCase):
    """Without a terminal there is no raw mode, and input() has to take over."""

    def test_a_non_tty_yields_no_reader(self):
        saved = crow._TTY
        crow._TTY = False
        try:
            with crow._raw_keys() as getch:
                self.assertIsNone(getch)
        finally:
            crow._TTY = saved

    def test_read_line_falls_back_to_input_when_there_is_no_reader(self):
        saved_tty, saved_input = crow._TTY, builtins.input
        crow._TTY = False
        builtins.input = lambda prompt="": "typed by input()"
        try:
            self.assertEqual(crow.read_line("you> "), "typed by input()")
        finally:
            crow._TTY, builtins.input = saved_tty, saved_input


class ShouldRollTests(unittest.TestCase):
    """The threshold, and the two ways it must refuse to fire."""

    def test_it_rolls_once_the_share_is_reached(self):
        self.assertTrue(crow.should_roll(180_000, 200_000, 0.9))

    def test_the_boundary_itself_rolls(self):
        """>= not >: at exactly the mark there is no reason to wait a turn."""
        self.assertTrue(crow.should_roll(180_000, 200_000, 0.9))
        self.assertFalse(crow.should_roll(179_999, 200_000, 0.9))

    def test_a_half_empty_window_does_not_roll(self):
        self.assertFalse(crow.should_roll(21_000, 200_000, 0.9))

    def test_an_unknown_window_never_rolls(self):
        """THE BUG THIS GUARD EXISTS FOR.

        fetch_n_ctx returns 0 when the server will not say. Without the guard
        `context_tokens >= 0 * 0.9` is true on every turn, including the very
        first, and the client archives and resets forever.
        """
        self.assertFalse(crow.should_roll(0, 0, 0.9))
        self.assertFalse(crow.should_roll(1, 0, 0.9))
        self.assertFalse(crow.should_roll(500_000, 0, 0.9))

    def test_a_threshold_of_zero_means_off_not_always(self):
        self.assertFalse(crow.should_roll(199_999, 200_000, 0.0))

    def test_a_negative_threshold_is_also_off(self):
        self.assertFalse(crow.should_roll(199_999, 200_000, -1.0))


class RolloverTests(unittest.TestCase):
    """Archiving, and what an archive is deliberately NOT allowed to do."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._real_dir, self._real_file = crow.SESSION_DIR, crow.SESSION_FILE
        self._real_post = crow.post_json
        crow.SESSION_DIR = self.dir
        crow.SESSION_FILE = str(Path(self.dir) / "session.json")
        self.posted = []
        crow.post_json = lambda url, body, timeout=30.0: self.posted.append(url) or {}

    def tearDown(self):
        crow.SESSION_DIR, crow.SESSION_FILE = self._real_dir, self._real_file
        crow.post_json = self._real_post
        shutil.rmtree(self.dir, ignore_errors=True)

    def _conversation(self):
        c = crow.Conversation("system prompt")
        c.append("user", "what is in this repo")
        c.append("assistant", "a lot")
        return c

    def _archive(self):
        return str(Path(self.dir) / "rollover-test.json")

    def test_a_named_path_is_written_instead_of_the_live_file(self):
        crow.save_session(self._conversation(), "http://x/v1", 99,
                          path=self._archive(), with_kv=False)
        self.assertTrue(os.path.exists(self._archive()))
        self.assertFalse(os.path.exists(crow.SESSION_FILE))

    def test_an_archive_never_writes_the_servers_slot(self):
        """SLOT_FILE is one fixed name: a second save would overwrite the cache
        the live session is still going to resume from."""
        crow.save_session(self._conversation(), "http://x/v1", 99,
                          path=self._archive(), with_kv=False)
        self.assertEqual(self.posted, [])

    def test_a_rollover_does_not_write_the_servers_slot_either(self):
        """The call SITE, not just the function.

        Added because a mutation that flipped roll_over's with_kv to True was
        caught by nothing: the test above calls save_session directly, so it
        proved the parameter works and said nothing about who passes it.
        """
        crow.roll_over(self._conversation(), "http://x/v1", 180_000, path=self._archive())
        self.assertEqual(self.posted, [])

    def test_the_live_session_still_saves_its_slot(self):
        """The case that must fail if with_kv is ever defaulted the wrong way."""
        crow.save_session(self._conversation(), "http://x/v1", 99)
        self.assertTrue(any("action=save" in url for url in self.posted))

    def test_an_archive_records_that_it_has_no_cache(self):
        crow.save_session(self._conversation(), "http://x/v1", 99,
                          path=self._archive(), with_kv=False)
        with open(self._archive(), encoding="utf-8") as fh:
            self.assertFalse(json.load(fh)["kv"])

    def test_the_archive_holds_the_messages_verbatim(self):
        crow.save_session(self._conversation(), "http://x/v1", 99,
                          path=self._archive(), with_kv=False)
        with open(self._archive(), encoding="utf-8") as fh:
            saved = json.load(fh)["messages"]
        self.assertEqual([m["content"] for m in saved],
                         ["system prompt", "what is in this repo", "a lot"])

    def test_load_session_reads_the_named_path(self):
        crow.save_session(self._conversation(), "http://x/v1", 99,
                          path=self._archive(), with_kv=False)
        restored = crow.load_session("http://x/v1", "system prompt", path=self._archive())
        self.assertIsNotNone(restored)
        self.assertEqual(len(restored[0]), 3)
        self.assertFalse(restored[2])

    def test_a_missing_archive_is_none_rather_than_a_crash(self):
        self.assertIsNone(crow.load_session("http://x/v1", None,
                                            path=str(Path(self.dir) / "gone.json")))

    def test_roll_over_empties_the_conversation_and_keeps_the_system_prompt(self):
        c = self._conversation()
        crow.roll_over(c, "http://x/v1", 180_000, path=self._archive())
        self.assertEqual(len(c), 2)
        self.assertEqual(c.payload()[0]["role"], "system")

    def test_the_note_names_the_archive_and_the_size(self):
        c = self._conversation()
        crow.roll_over(c, "http://x/v1", 180_000, path=self._archive())
        note = c.payload()[1]["content"]
        self.assertIn(self._archive(), note)
        self.assertIn("180000", note)

    def test_a_readable_transcript_is_written_beside_the_json(self):
        c = self._conversation()
        crow.roll_over(c, "http://x/v1", 180_000, path=self._archive())
        self.assertTrue(os.path.exists(self._archive()[:-5] + ".md"))

    def test_the_note_points_at_the_transcript_with_its_line_count(self):
        """The JSON is unreachable through read_file's cap; the note has to send
        the reader somewhere that is not."""
        c = self._conversation()
        crow.roll_over(c, "http://x/v1", 180_000, path=self._archive())
        note = c.payload()[1]["content"]
        self.assertIn(self._archive()[:-5] + ".md", note)
        self.assertIn("lines", note)

    def test_the_note_says_where_the_work_had_got_to(self):
        c = crow.Conversation("system prompt")
        c.append("user", "look at the installer")
        c.append("assistant", "", tool_calls=[
            {"id": "1", "name": "read_file", "arguments": '{"path": "C:/Crow/install.ps1"}'}])
        c.append("tool", "...", tool_call_id="1")
        crow.roll_over(c, "http://x/v1", 180_000, path=self._archive())
        self.assertIn("C:/Crow/install.ps1", c.payload()[1]["content"])

    def test_a_conversation_without_tools_gets_no_empty_where_line(self):
        """The case that must fail if the line is printed unconditionally."""
        c = self._conversation()
        crow.roll_over(c, "http://x/v1", 180_000, path=self._archive())
        self.assertNotIn("Last worked on:", c.payload()[1]["content"])

    def test_the_archive_json_is_no_longer_one_line(self):
        c = self._conversation()
        crow.roll_over(c, "http://x/v1", 180_000, path=self._archive())
        text = Path(self._archive()).read_text(encoding="utf-8")
        self.assertGreater(text.count("\n"), 4)

    def test_the_carried_turn_shares_one_message_with_the_note(self):
        """Two user messages in a row are merged or refused depending on the
        template, and 180k tokens in is the wrong place to discover which."""
        c = self._conversation()
        crow.roll_over(c, "http://x/v1", 180_000, carry="and now?", path=self._archive())
        payload = c.payload()
        self.assertEqual([m["role"] for m in payload], ["system", "user"])
        self.assertIn("and now?", payload[1]["content"])

    def test_without_a_carry_the_note_stands_alone(self):
        c = self._conversation()
        crow.roll_over(c, "http://x/v1", 180_000, path=self._archive())
        self.assertNotIn("and now?", c.payload()[1]["content"])

    def test_an_empty_conversation_is_not_archived(self):
        """Otherwise a rollover on a fresh start writes a file holding nothing
        and resets a conversation that had not begun."""
        empty = crow.Conversation("system prompt")
        self.assertIsNone(crow.roll_over(empty, "http://x/v1", 0, path=self._archive()))
        self.assertFalse(os.path.exists(self._archive()))

    def test_rollover_paths_carry_a_stamp(self):
        self.assertIn("rollover-", crow.rollover_path("20260810-074500"))
        self.assertTrue(crow.rollover_path("20260810-074500").endswith(".json"))

    def test_two_rollovers_do_not_share_a_file(self):
        self.assertNotEqual(crow.rollover_path("20260810-074500"),
                            crow.rollover_path("20260810-074501"))


class SessionFormatGateTests(unittest.TestCase):
    """The gate on the shared session file, before a second writer exists.

    WHY IT COULD NOT WAIT FOR THE SECOND WRITER. session.json and the server's
    one fixed crow-session.bin are written by whoever runs; the moment a window
    runs beside the terminal, "the format changed" is a silent data change
    rather than an error. The gate has to be in the file BEFORE that, because
    the files it has to be gentle with are the ones already on disk.

    FOUR CASES, and the third and fourth are the way back rather than the way
    forward:

      (a) an unknown stamp is refused, visibly, WITHOUT the file being touched;
      (b) a known stamp loads;
      (c) a file with no stamp -- every session file every installation is
          holding today -- is accepted and stamped by the next save. Refusing
          those would take the history off every existing user on the day they
          update;
      (d) a file this build stamped is still readable by an older one. The
          claim is that the five `saved.get(...)` reads of 0.2.0 ignore a key
          they do not know; it is written down here as a case rather than left
          as an assumption, because a gate nobody can come back through is a
          one-way door.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._real_dir, self._real_file = crow.SESSION_DIR, crow.SESSION_FILE
        self._real_post = crow.post_json
        crow.SESSION_DIR = self.dir
        crow.SESSION_FILE = str(Path(self.dir) / "session.json")
        self.posted = []
        crow.post_json = lambda url, body, timeout=30.0: self.posted.append(url) or {}
        # #92: this class is the only one that calls `repl()`, and `repl` binds a
        # working directory -- from the REAL roots.json, because that is what a
        # user's crow does. Left standing it reaches ACROSS MODULE BOUNDARIES:
        # measured 2026-08-14, three ReleaseLevelTests in test_crow_core went red
        # for it when the three suites ran in one process, on a machine where
        # somebody had picked a folder once. A suite's colour may not depend on
        # what the person running it chose in the window yesterday.
        self._root_before = crow.get_root()

    def tearDown(self):
        crow.SESSION_DIR, crow.SESSION_FILE = self._real_dir, self._real_file
        crow.post_json = self._real_post
        crow.set_root(self._root_before)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _conversation(self):
        c = crow.Conversation("system prompt")
        c.append("user", "what is in this repo")
        c.append("assistant", "a lot")
        return c

    def _body(self, **extra):
        """A session file as the shipped client writes one, minus the stamp."""
        body = {"version": "0.2.0", "kv": False, "kv_tokens": 0,
                "context_tokens": 42,
                "prefix": crow.prefix_fingerprint("system prompt"),
                "messages": [{"role": "user", "content": "hi"}]}
        body.update(extra)
        return body

    def _write(self, saved):
        with open(crow.SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        return crow.SESSION_FILE

    def _bytes(self):
        with open(crow.SESSION_FILE, "rb") as fh:
            return fh.read()

    @staticmethod
    def _read_like_0_2_0(path):
        """The five reads the shipped 0.2.0 load_session does, and no others.

        Copied out of cli/crow.py at f7b2765 rather than called: the point of
        case (d) is what a build WITHOUT this gate does with a file that has
        been through it, and that build is not importable from here.
        """
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        return (saved.get("messages") or [],
                saved.get("kv"),
                saved.get("prefix"),
                int(saved.get("kv_tokens") or 0),
                int(saved.get("context_tokens") or 0))

    # --- (a) an unknown stamp ------------------------------------------------

    def test_an_unknown_stamp_is_refused_on_the_read_path(self):
        self._write(self._body(**{crow.SESSION_FORMAT_KEY: "99"}))
        before = self._bytes()
        with self.assertRaises(crow.SessionFormatError):
            crow.load_session("http://x/v1", "system prompt")
        self.assertEqual(self._bytes(), before, "a refused file is left alone")
        self.assertEqual(self.posted, [], "and the server was not asked either")

    def test_the_refusal_names_the_file_and_both_formats(self):
        """Visibly refused, not quietly. `None` out of load_session already
        means 'no session here', so the refusal has to arrive as something the
        surface can tell apart from that."""
        path = self._write(self._body(**{crow.SESSION_FORMAT_KEY: "99"}))
        with self.assertRaises(crow.SessionFormatError) as caught:
            crow.load_session("http://x/v1", "system prompt")
        text = str(caught.exception)
        self.assertIn(path, text)
        self.assertIn("99", text)
        self.assertIn(crow.SESSION_FORMAT, text)
        self.assertEqual(caught.exception.path, path)

    def test_the_gate_sits_before_the_write_on_the_read_path(self):
        """load_session WRITES while reading: a promised cache that turns out
        to be gone is withdrawn by rewriting the file. A gate placed after that
        has already changed the stranger's file before refusing it."""
        self._write(self._body(kv=True, **{crow.SESSION_FORMAT_KEY: "99"}))
        before = self._bytes()
        crow.post_json = lambda *a, **k: (_ for _ in ()).throw(OSError("no such file"))
        with self.assertRaises(crow.SessionFormatError):
            crow.load_session("http://x/v1", "system prompt")
        self.assertEqual(self._bytes(), before,
                         "the withdrawal rewrite ran on a file the gate refuses")

    def test_an_unknown_stamp_is_refused_before_anything_is_saved(self):
        """The other half of the same promise. A gate only on the read path
        refuses to READ a stranger's file and then flattens it on exit."""
        self._write(self._body(**{crow.SESSION_FORMAT_KEY: "99"}))
        before = self._bytes()
        with self.assertRaises(crow.SessionFormatError):
            crow.save_session(self._conversation(), "http://x/v1", 99)
        self.assertEqual(self._bytes(), before)
        self.assertEqual(self.posted, [],
                         "the slot save is a write too, and SLOT_FILE is one fixed name")

    def test_a_refused_file_survives_a_second_attempt_unchanged(self):
        """Idempotence, and it is the file that has to be idempotent here."""
        self._write(self._body(**{crow.SESSION_FORMAT_KEY: "99"}))
        before = self._bytes()
        for _ in range(2):
            with self.assertRaises(crow.SessionFormatError):
                crow.load_session("http://x/v1", "system prompt")
            with self.assertRaises(crow.SessionFormatError):
                crow.save_session(self._conversation(), "http://x/v1", 99)
        self.assertEqual(self._bytes(), before)

    # --- (b) a known stamp ---------------------------------------------------

    def test_a_known_stamp_loads(self):
        self._write(self._body(**{crow.SESSION_FORMAT_KEY: crow.SESSION_FORMAT}))
        restored = crow.load_session("http://x/v1", "system prompt")
        self.assertIsNotNone(restored)
        self.assertEqual(len(restored[0]), 1)
        self.assertEqual(restored[1], 42)

    def test_what_this_build_writes_is_what_this_build_reads(self):
        crow.save_session(self._conversation(), "http://x/v1", 99)
        restored = crow.load_session("http://x/v1", "system prompt")
        self.assertIsNotNone(restored)
        self.assertEqual(len(restored[0]), 3)

    # --- (c) no stamp: the file every installation is holding today ----------

    def test_a_file_without_the_stamp_is_accepted(self):
        saved = self._body()
        del saved["version"]
        self._write(saved)
        restored = crow.load_session("http://x/v1", "system prompt")
        self.assertIsNotNone(restored, "an unstamped file is today's format, not a foreign one")
        self.assertEqual(len(restored[0]), 1)

    def test_a_file_as_0_2_0_actually_wrote_it_is_accepted(self):
        """THE PREMISE THIS CASE WAS PLANNED ON IS NOT WHAT IS ON DISK. The
        stage says a 0.2.0 session file has no `version` key. It has one:
        cli/crow.py at f7b2765 writes `"version": VERSION` into every save, so
        every file out there carries "0.2.0" -- read by nobody, but there. A
        gate that had taken THAT field as its format number would have refused
        every existing session on the first start after an update, which is the
        exact harm this case forbids. Hence a key of its own, and hence this
        case beside the keyless one."""
        self._write(self._body())
        restored = crow.load_session("http://x/v1", "system prompt")
        self.assertIsNotNone(restored)
        self.assertEqual(len(restored[0]), 1)

    def test_the_next_save_stamps_an_unstamped_file(self):
        self._write(self._body())
        crow.save_session(self._conversation(), "http://x/v1", 99)
        with open(crow.SESSION_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[crow.SESSION_FORMAT_KEY], crow.SESSION_FORMAT)
        self.assertEqual(saved["version"], crow.VERSION,
                         "`version` keeps meaning the client that wrote the file")

    def test_the_stamped_file_then_loads_without_a_word(self):
        """The round trip of (c): accepted, stamped, and still ours next time."""
        self._write(self._body())
        crow.save_session(self._conversation(), "http://x/v1", 99)
        self.assertIsNotNone(crow.load_session("http://x/v1", "system prompt"))

    # --- (d) the way back ----------------------------------------------------

    def test_a_stamped_file_is_still_readable_by_an_older_build(self):
        crow.save_session(self._conversation(), "http://x/v1", 4242)
        messages, kv, prefix, kv_tokens, context = self._read_like_0_2_0(crow.SESSION_FILE)
        self.assertEqual([m["content"] for m in messages],
                         ["system prompt", "what is in this repo", "a lot"])
        self.assertEqual(context, 4242)
        self.assertEqual(prefix, crow.prefix_fingerprint("system prompt"))
        self.assertTrue(kv)
        self.assertEqual(kv_tokens, 0)

    def test_the_stamp_is_an_added_key_and_not_a_renamed_one(self):
        """Why (d) holds at all: the older build reads five keys and ignores
        the rest. It only goes on holding while every one of those five is
        still there under its own name."""
        crow.save_session(self._conversation(), "http://x/v1", 99)
        with open(crow.SESSION_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        was = {"version", "kv", "kv_tokens", "context_tokens", "prefix", "messages"}
        self.assertEqual(was - set(saved), set(), "a key an older build reads went missing")
        self.assertIn(crow.SESSION_FORMAT_KEY, saved)

    def test_a_file_an_older_build_wrote_back_is_taken_again(self):
        """The full round trip down and up: this build stamps, 0.2.0 saves over
        it and drops the stamp it never knew about, this build reads it again."""
        crow.save_session(self._conversation(), "http://x/v1", 99)
        with open(crow.SESSION_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        del saved[crow.SESSION_FORMAT_KEY]
        self._write(saved)
        self.assertIsNotNone(crow.load_session("http://x/v1", "system prompt"))

    # --- the rule on its own -------------------------------------------------

    def test_the_rule_itself(self):
        self.assertIsNone(crow.session_format_problem({}))
        self.assertIsNone(crow.session_format_problem(
            {crow.SESSION_FORMAT_KEY: crow.SESSION_FORMAT}))
        self.assertIsNotNone(crow.session_format_problem({crow.SESSION_FORMAT_KEY: "99"}))

    def test_the_stamp_is_a_string_because_one_equals_true(self):
        """`1 == True` in Python. An integer stamp would take a JSON `true` for
        this build's own work."""
        self.assertIsNotNone(crow.session_format_problem({crow.SESSION_FORMAT_KEY: True}))
        self.assertIsNotNone(crow.session_format_problem({crow.SESSION_FORMAT_KEY: 1}))

    def test_a_file_that_is_not_there_is_not_a_problem(self):
        self.assertIsNone(crow.session_file_problem(str(Path(self.dir) / "gone.json")))

    def test_a_corrupt_file_is_not_a_foreign_format(self):
        """Unreadable is not a stranger's format. Refusing to overwrite garbage
        would strand a user with a broken file and no way to start over."""
        with open(crow.SESSION_FILE, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.assertIsNone(crow.session_file_problem(crow.SESSION_FILE))
        self.assertIsNone(crow.load_session("http://x/v1", "system prompt"))
        self.assertIsNotNone(crow.save_session(self._conversation(), "http://x/v1", 99))

    def test_an_archive_gets_the_same_gate(self):
        """A rollover archive is the same format in a different file, and
        --resume reads it with the same function."""
        archive = str(Path(self.dir) / "rollover-test.json")
        with open(archive, "w", encoding="utf-8") as fh:
            json.dump(self._body(**{crow.SESSION_FORMAT_KEY: "99"}), fh)
        with self.assertRaises(crow.SessionFormatError):
            crow.load_session("http://x/v1", "system prompt", path=archive)

    # --- the surface, where "visibly" is either true or it is not ------------

    def test_the_cli_says_it_and_stops_rather_than_starting_over_the_file(self):
        """The core refuses; the surface has to TELL somebody. An uncaught
        raise would hold the file just as well and reach the user as a
        traceback, and starting anyway would build a session that leave() then
        refuses to write -- the same loss, one hour later.

        Driven through repl() rather than read off its source: what is being
        checked is the exit code and the sentence, and neither is in the text.
        """
        self._write(self._body(**{crow.SESSION_FORMAT_KEY: "99"}))
        before = self._bytes()
        args = crow.build_parser().parse_args([])
        args.font = False
        args.background = False
        args.update_check = False
        saved = {name: getattr(crow, name) for name in
                 ("check_endpoint", "fetch_n_ctx", "fetch_model_name", "read_line")}
        crow.check_endpoint = lambda *a, **k: "ok"
        crow.fetch_n_ctx = lambda *a, **k: 0
        crow.fetch_model_name = lambda *a, **k: ""
        crow.read_line = lambda prompt: "/exit"
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = crow.repl(args)
        finally:
            for name, value in saved.items():
                setattr(crow, name, value)
        self.assertEqual(code, 2, "a session it will not touch is not a start")
        self.assertIn("99", err.getvalue())
        self.assertIn("--no-session", err.getvalue(),
                      "the way out has to be in the sentence")
        self.assertEqual(self._bytes(), before)


class TranscriptTests(unittest.TestCase):
    """The archive a model pointed at it can actually read."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = str(Path(self.dir) / "t.md")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _conversation(self):
        c = crow.Conversation("system prompt")
        c.append("user", "where is the installer")
        c.append("assistant", "looking", reasoning="a long private deliberation",
                 tool_calls=[{"id": "1", "name": "list_dir",
                              "arguments": '{"path": "C:/x"}'}])
        c.append("tool", "install.ps1", tool_call_id="1")
        return c

    def test_it_has_lines_which_is_the_entire_point(self):
        """THE DEFECT THIS EXISTS FOR: json.dump writes ONE line. A 104,618-byte
        archive on one line is unreachable through read_file's byte cap."""
        lines = crow.write_transcript(self._conversation(), self.path)
        self.assertGreater(lines, 4)
        self.assertGreater(Path(self.path).read_text(encoding="utf-8").count("\n"), 4)

    def test_the_reported_line_count_matches_the_file(self):
        lines = crow.write_transcript(self._conversation(), self.path)
        text = Path(self.path).read_text(encoding="utf-8")
        self.assertEqual(lines, text.count("\n") + 1)

    def test_reasoning_is_left_out(self):
        """The case that must fail if the transcript ever just dumps messages:
        reasoning is the bulk of the bytes and none of the recall."""
        crow.write_transcript(self._conversation(), self.path)
        self.assertNotIn("a long private deliberation",
                         Path(self.path).read_text(encoding="utf-8"))

    def test_tool_calls_are_visible(self):
        crow.write_transcript(self._conversation(), self.path)
        text = Path(self.path).read_text(encoding="utf-8")
        self.assertIn("list_dir", text)
        self.assertIn("install.ps1", text)


class RecentPathsTests(unittest.TestCase):
    """Where the archived conversation had got to."""

    def _with(self, *calls):
        c = crow.Conversation("s")
        for i, args in enumerate(calls):
            c.append("assistant", "", tool_calls=[
                {"id": str(i), "name": "read_file", "arguments": args}])
        return c

    def test_paths_and_roots_are_both_collected(self):
        c = self._with('{"path": "C:/a"}', '{"root": "C:/b"}')
        self.assertEqual(crow.recent_paths(c), ["C:/a", "C:/b"])

    def test_the_newest_wins_and_nothing_repeats(self):
        c = self._with('{"path": "C:/a"}', '{"path": "C:/b"}', '{"path": "C:/a"}')
        self.assertEqual(crow.recent_paths(c), ["C:/b", "C:/a"])

    def test_only_the_last_few_are_kept(self):
        c = self._with(*[f'{{"path": "C:/{i}"}}' for i in range(9)])
        self.assertEqual(crow.recent_paths(c, limit=3), ["C:/6", "C:/7", "C:/8"])

    def test_a_conversation_without_tools_yields_nothing(self):
        """The case that must fail if this ever starts inventing paths."""
        c = crow.Conversation("s")
        c.append("user", "hello")
        self.assertEqual(crow.recent_paths(c), [])

    def test_unparseable_arguments_are_skipped_rather_than_fatal(self):
        c = self._with("not json at all", '{"path": "C:/ok"}')
        self.assertEqual(crow.recent_paths(c), ["C:/ok"])


class WarmCacheClaimTests(unittest.TestCase):
    """A 200 says the file was read. It does not say the cache is ours."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._real_dir, self._real_file = crow.SESSION_DIR, crow.SESSION_FILE
        self._real_post = crow.post_json
        crow.SESSION_DIR = self.dir
        crow.SESSION_FILE = str(Path(self.dir) / "session.json")

    def tearDown(self):
        crow.SESSION_DIR, crow.SESSION_FILE = self._real_dir, self._real_file
        crow.post_json = self._real_post
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, kv_tokens):
        with open(crow.SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump({"version": crow.VERSION, "kv": True, "kv_tokens": kv_tokens,
                       "context_tokens": 21004,
                       "prefix": crow.prefix_fingerprint(None),
                       "messages": [{"role": "user", "content": "hi"}]}, fh)

    def _stored_kv(self):
        with open(crow.SESSION_FILE, encoding="utf-8") as fh:
            return json.load(fh)["kv"]

    def test_a_save_records_what_the_server_wrote(self):
        crow.post_json = lambda *a, **k: {"n_saved": 4242}
        c = crow.Conversation("s")
        c.append("user", "hi")
        crow.save_session(c, "http://x/v1", 99)
        with open(crow.SESSION_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["kv_tokens"], 4242)

    def test_a_matching_restore_keeps_the_claim(self):
        self._write(kv_tokens=21004)
        crow.post_json = lambda *a, **k: {"n_restored": 21004}
        self.assertTrue(crow.load_session("http://x/v1")[2])

    def test_a_restore_of_the_wrong_size_withdraws_it(self):
        """THE MEASURED CASE, 2026-08-10: 'cache warm' followed by cached 0/21004."""
        self._write(kv_tokens=21004)
        crow.post_json = lambda *a, **k: {"n_restored": 7}
        self.assertFalse(crow.load_session("http://x/v1")[2])

    def test_a_restore_of_nothing_withdraws_it(self):
        self._write(kv_tokens=21004)
        crow.post_json = lambda *a, **k: {"n_restored": 0}
        self.assertFalse(crow.load_session("http://x/v1")[2])

    def test_a_withdrawn_claim_is_written_back(self):
        """Otherwise the same false promise is made on every start."""
        self._write(kv_tokens=21004)
        crow.post_json = lambda *a, **k: {"n_restored": 7}
        crow.load_session("http://x/v1")
        self.assertFalse(self._stored_kv())

    def test_a_server_that_says_nothing_is_still_believed(self):
        """Silence is not a contradiction -- refusing here would reject a good
        cache on every endpoint that does not report the field."""
        self._write(kv_tokens=21004)
        crow.post_json = lambda *a, **k: {}
        self.assertTrue(crow.load_session("http://x/v1")[2])

    def test_an_old_session_without_the_field_is_still_believed(self):
        """Sessions written before kv_tokens existed carry no expectation."""
        self._write(kv_tokens=0)
        crow.post_json = lambda *a, **k: {"n_restored": 21004}
        self.assertTrue(crow.load_session("http://x/v1")[2])


class ResumePathTests(unittest.TestCase):
    """--resume takes a bare name or a path, and must not confuse the two."""

    def test_a_bare_name_is_looked_for_among_the_archives(self):
        self.assertEqual(crow.resume_path("rollover-1.json"),
                         os.path.join(crow.SESSION_DIR, "rollover-1.json"))

    def test_a_path_with_a_separator_is_left_alone(self):
        self.assertEqual(crow.resume_path(os.path.join("sub", "s.json")),
                         os.path.join("sub", "s.json"))

    def test_an_absolute_path_is_left_alone(self):
        absolute = os.path.abspath(os.path.join("tmp", "s.json"))
        self.assertEqual(crow.resume_path(absolute), absolute)


class RolloverWiringTests(unittest.TestCase):
    """The flags exist and default the way the code above assumes."""

    def test_rollover_at_defaults_to_the_constant(self):
        args = crow.build_parser().parse_args([])
        self.assertEqual(args.rollover_at, crow.ROLLOVER_AT)

    def test_rollover_can_be_switched_off_from_the_command_line(self):
        args = crow.build_parser().parse_args(["--rollover-at", "0"])
        self.assertFalse(crow.should_roll(199_999, 200_000, args.rollover_at))

    def test_resume_is_absent_unless_asked_for(self):
        self.assertIsNone(crow.build_parser().parse_args([]).resume)

    def test_resume_takes_a_file(self):
        args = crow.build_parser().parse_args(["--resume", "rollover-1.json"])
        self.assertEqual(args.resume, "rollover-1.json")

    def test_max_tool_rounds_defaults_to_the_constant(self):
        self.assertEqual(crow.build_parser().parse_args([]).max_tool_rounds,
                         crow.MAX_TOOL_ROUNDS)

    def test_max_tool_rounds_can_be_raised(self):
        self.assertEqual(
            crow.build_parser().parse_args(["--max-tool-rounds", "60"]).max_tool_rounds, 60)

    def test_max_tool_rounds_is_a_number_not_a_string(self):
        """range() takes an int. A string default would fail at the first turn,
        which is the worst place to find out."""
        self.assertIsInstance(
            crow.build_parser().parse_args(["--max-tool-rounds", "3"]).max_tool_rounds, int)

    def test_no_session_did_not_become_a_path(self):
        """--no-session stays a switch; --resume is the one that takes a name."""
        args = crow.build_parser().parse_args(["--no-session"])
        self.assertFalse(args.session)


class ToolLayerCase(unittest.TestCase):
    """Base for every tool case: a temp directory, and a reset that is not optional.

    THE HALF-STATE THAT MATTERS HERE IS NOT THE TEMP DIRECTORY, IT IS `_READ`.
    It is module-global (crow.py:1684), has six occurrences (:1684, 1739, 1767,
    1772, 1781, 1792) and nothing ever empties it -- no clear(), no del. A case
    that reads a file and leaves the key behind makes the NEXT case green where
    it has to be red, so the suite would pollute itself and the damage would look
    like success. `_SEEN` (:1935) has the same shape: the one place that clears
    it is repl() (:2478), and repl() does not run here.

    Both are emptied IN PLACE and put back the way they were, never rebound.
    `crow._READ = set()` works today and stops working the moment the tool layer
    moves to crow_core.py and crow.py re-exports the name: the tools would go on
    consulting the object the core holds, and the reset would silently stop
    resetting -- green, and meaningless. Same reason TOOL_IMPL is mutated in
    place below instead of being swapped out.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crow-tools-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._read_before = set(crow._READ)
        self._seen_before = dict(crow._SEEN)
        # #92 JOINS THE LIST, and it had to: `_ROOT` is the third piece of global
        # tool state, and unlike the two above it can be set by a case that never
        # touches a tool -- `SessionFormatGateTests` calls `repl()`, which binds
        # the last folder the USER picked, read from the real roots.json. Left
        # standing, every later write case runs inside a boundary it never asked
        # for: measured 2026-08-14, two cases red for that reason alone, on any
        # machine where somebody had once chosen a directory.
        #
        # REBOUND THROUGH set_root, not by assigning `crow._ROOT`. It is a string
        # in crow_core and `crow.py` binds names by value, so an assignment here
        # would move a copy while the tools went on reading the core's -- the
        # trap this docstring describes for `_READ`, in the one shape where
        # in-place mutation cannot save it.
        self._root_before = crow.get_root()
        self.addCleanup(self._restore_tool_state)
        crow._READ.clear()
        crow._SEEN.clear()
        crow.set_root(None)

    def _restore_tool_state(self):
        crow._READ.clear()
        crow._READ.update(self._read_before)
        crow._SEEN.clear()
        crow._SEEN.update(self._seen_before)
        crow.set_root(self._root_before)

    def _path(self, name):
        return os.path.join(self.dir, name)

    def _make(self, name, text):
        """A file that exists WITHOUT going through the tools -- so it is unread.

        Writing it with tool_write_file would register the key and hand every
        read-before-write case the answer it is supposed to earn.
        """
        path = self._path(name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return path

    def _text(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _install(self, name, impl):
        """Add an implementation to TOOL_IMPL and put the old one back afterwards.

        IT USED TO pop() ON CLEANUP, which is right for a name that did not
        exist and destructive for one that did: installing a double over a REAL
        tool deleted it from TOOL_IMPL for the rest of the process, and the next
        case to ask "is every offered tool implemented" went red with no
        connection to what broke it. Found 2026-08-14 the first time a case
        needed a double under a shipped name (`run_command`, #93). Restoring is
        the same shape as `_READ` and `_SEEN` above, and for the same reason.
        """
        missing = object()
        before = crow.TOOL_IMPL.get(name, missing)
        crow.TOOL_IMPL[name] = impl

        def restore():
            if before is missing:
                crow.TOOL_IMPL.pop(name, None)
            else:
                crow.TOOL_IMPL[name] = before

        self.addCleanup(restore)


class ReadBeforeWriteTests(ToolLayerCase):
    """write_file BLOCKS an unread existing file rather than warning about it.

    The damage is data loss: #10 measured hermes-agent resolving the same
    situation to last-write-wins in two independent code paths, one of which
    returns a warning string while the other performs the write anyway
    (crow.py:1679-1683). A rebuild that warns has the defect, not the rule.
    """

    def test_an_existing_unread_file_is_refused(self):
        path = self._make("notes.txt", "the work that must survive")
        self.assertIn("refusing to overwrite", crow.tool_write_file(path, "overwritten"))

    def test_the_refusal_leaves_the_bytes_alone(self):
        """A refusal that has already written is not a refusal."""
        path = self._make("notes.txt", "the work that must survive")
        crow.tool_write_file(path, "overwritten")
        self.assertEqual(self._text(path), "the work that must survive")

    def test_the_refusal_says_what_to_do_next(self):
        path = self._make("notes.txt", "x")
        self.assertIn("read_file", crow.tool_write_file(path, "y"))

    def test_a_file_that_does_not_exist_is_written_without_reading_it(self):
        """Deliberate, and the detail a second implementation gets wrong: the rule
        is read-before-OVERWRITE. A file that does not exist has nothing to
        destroy, and demanding a read of it would make the model read an error.
        """
        path = self._path("new.txt")
        self.assertIn("wrote 5 bytes", crow.tool_write_file(path, "fresh"))
        self.assertEqual(self._text(path), "fresh")

    def test_a_read_unlocks_the_overwrite(self):
        path = self._make("notes.txt", "old")
        crow.tool_read_file(path)
        self.assertIn("wrote", crow.tool_write_file(path, "new"))
        self.assertEqual(self._text(path), "new")

    def test_reading_a_range_also_counts_as_reading(self):
        """Two branches register the key -- the range one at crow.py:1739, the
        whole-file one at :1767. One rule, and both halves have to hold it."""
        path = self._make("notes.txt", "one\ntwo\nthree\n")
        crow.tool_read_file(path, start_line=1, end_line=2)
        self.assertIn("wrote", crow.tool_write_file(path, "new"))

    def test_a_read_that_returned_nothing_unlocks_nothing(self):
        """The empty-range error returns BEFORE _READ.add (crow.py:1736-1739).
        A read that showed the model nothing must not count as having seen it."""
        path = self._make("notes.txt", "one\n")
        self.assertIn("error", crow.tool_read_file(path, start_line=50, end_line=60))
        self.assertIn("refusing to overwrite", crow.tool_write_file(path, "new"))

    def test_a_missing_file_read_unlocks_nothing_either(self):
        gone = self._path("gone.txt")
        self.assertIn("no such file", crow.tool_read_file(gone))
        self.assertNotIn(crow._key(gone), crow._READ)

    def test_a_write_counts_as_a_read_afterwards(self):
        """crow.py:1781. The process wrote the bytes, so it knows them."""
        path = self._path("new.txt")
        crow.tool_write_file(path, "first")
        self.assertIn("wrote", crow.tool_write_file(path, "second"))
        self.assertEqual(self._text(path), "second")

    def test_the_key_is_what_the_rule_hangs_on(self):
        path = self._make("notes.txt", "x")
        self.assertNotIn(crow._key(path), crow._READ)
        crow.tool_read_file(path)
        self.assertIn(crow._key(path), crow._READ)

    def test_reading_one_file_does_not_unlock_another(self):
        first = self._make("a.txt", "alpha")
        second = self._make("b.txt", "beta")
        crow.tool_read_file(first)
        self.assertIn("refusing to overwrite", crow.tool_write_file(second, "x"))


class EditFileHitCountTests(ToolLayerCase):
    """Exact match, exactly one hit, and a refusal that changes nothing.

    A patch format would be more expressive and needs fuzzy matching to survive a
    model that mis-remembers whitespace; exact match plus a uniqueness check fails
    loudly instead of guessing (crow.py:1786-1790).
    """

    def test_an_unread_file_is_refused(self):
        path = self._make("code.py", "alpha\n")
        self.assertIn("before editing it", crow.tool_edit_file(path, old="alpha", new="beta"))

    def test_the_unread_refusal_leaves_the_bytes_alone(self):
        path = self._make("code.py", "alpha\n")
        crow.tool_edit_file(path, old="alpha", new="beta")
        self.assertEqual(self._text(path), "alpha\n")

    def test_zero_hits_is_refused(self):
        path = self._make("code.py", "alpha\n")
        crow.tool_read_file(path)
        self.assertIn("does not appear", crow.tool_edit_file(path, old="gamma", new="beta"))

    def test_the_zero_hit_refusal_leaves_the_bytes_alone(self):
        path = self._make("code.py", "alpha\n")
        crow.tool_read_file(path)
        crow.tool_edit_file(path, old="gamma", new="beta")
        self.assertEqual(self._text(path), "alpha\n")

    def test_two_hits_are_refused(self):
        path = self._make("code.py", "alpha\nalpha\n")
        crow.tool_read_file(path)
        result = crow.tool_edit_file(path, old="alpha", new="beta")
        self.assertIn("appears 2 times", result)
        self.assertIn("make it unique", result)

    def test_the_ambiguous_edit_writes_nothing(self):
        """The one that costs work: replacing the first of two matches silently
        edits the wrong line, and the model is told it succeeded."""
        path = self._make("code.py", "alpha\nalpha\n")
        crow.tool_read_file(path)
        crow.tool_edit_file(path, old="alpha", new="beta")
        self.assertEqual(self._text(path), "alpha\nalpha\n")

    def test_exactly_one_hit_is_written(self):
        path = self._make("code.py", "alpha\nbeta\n")
        crow.tool_read_file(path)
        self.assertIn("replaced 1 occurrence",
                      crow.tool_edit_file(path, old="alpha", new="gamma"))
        self.assertEqual(self._text(path), "gamma\nbeta\n")

    def test_one_hit_across_several_lines_is_still_one_hit(self):
        path = self._make("code.py", "a\nb\nc\n")
        crow.tool_read_file(path)
        self.assertIn("replaced 1 occurrence",
                      crow.tool_edit_file(path, old="a\nb\n", new="a\nB\n"))
        self.assertEqual(self._text(path), "a\nB\nc\n")

    def test_an_empty_old_is_sent_to_write_file(self):
        path = self._make("code.py", "alpha\n")
        crow.tool_read_file(path)
        self.assertIn("use write_file", crow.tool_edit_file(path, old="", new="x"))

    def test_the_count_is_on_raw_text_with_no_whitespace_tolerance(self):
        """MEASURED BEHAVIOUR, and the first thing a fuzzy rebuild changes:
        'call(a,  b)' with two spaces does not match 'call(a, b)'."""
        path = self._make("code.py", "call(a,  b)\n")
        crow.tool_read_file(path)
        self.assertIn("does not appear",
                      crow.tool_edit_file(path, old="call(a, b)", new="x"))

    def test_the_gate_does_not_ask_whether_the_file_exists(self):
        """The _READ check comes first (crow.py:1792) and carries no
        os.path.exists: an unread MISSING file is refused with 'read it first',
        not with 'no such file'. Pinned because the two answers send a model in
        different directions -- one says read, the other says look elsewhere."""
        self.assertIn("before editing it",
                      crow.tool_edit_file(self._path("gone.py"), old="a", new="b"))

    def test_reading_one_file_does_not_unlock_editing_another(self):
        first = self._make("a.py", "alpha\n")
        second = self._make("b.py", "alpha\n")
        crow.tool_read_file(first)
        self.assertIn("before editing it",
                      crow.tool_edit_file(second, old="alpha", new="beta"))


class ReadKeyTests(ToolLayerCase):
    """_key is normcase+abspath (crow.py:1687), so what counts as "the same file"
    is decided by the platform and not by the string the model happened to send.
    """

    def test_a_detour_through_a_parent_is_the_same_file(self):
        path = self._make("a.py", "alpha\n")
        crow.tool_read_file(path)
        detour = os.path.join(self.dir, "sub", "..", "a.py")
        self.assertIn("wrote", crow.tool_write_file(detour, "new"))

    def test_a_relative_path_is_the_same_file(self):
        """abspath, so a read as 'a.py' and a write as the full path are one key.
        Without it the model has to spell the path the same way twice."""
        self._make("a.py", "alpha\n")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.dir)
        crow.tool_read_file("a.py")
        self.assertIn("wrote", crow.tool_write_file(self._path("a.py"), "new"))

    def test_two_different_files_are_two_keys(self):
        """Positive control for the folding cases below: the key still separates
        files that really are different."""
        self.assertNotEqual(crow._key(self._path("a.py")), crow._key(self._path("b.py")))

    @unittest.skipUnless(os.name == "nt", "normcase folds case on Windows only")
    def test_the_key_folds_case_on_windows(self):
        self.assertEqual(crow._key(self._path("a.py")), crow._key(self._path("A.PY")))

    @unittest.skipUnless(os.name == "nt", "normcase folds case on Windows only")
    def test_a_shouted_path_after_a_lowercase_read_counts_as_read(self):
        """THE WINDOWS CASE: read C:\\x\\a.py, then write C:\\x\\A.PY. It is one
        file on this filesystem, os.path.exists says so for both spellings, and
        normcase folds the key -- so the write goes through instead of refusing a
        file the model demonstrably read. A rebuild keyed on the raw string
        refuses here, and the model has no way to see why.
        """
        path = self._make("a.py", "alpha\n")
        crow.tool_read_file(path)
        self.assertIn("wrote", crow.tool_write_file(self._path("A.PY"), "new"))
        self.assertEqual(self._text(path), "new")

    @unittest.skipUnless(os.name == "nt", "normcase folds case on Windows only")
    def test_the_shouted_path_edits_the_same_file_too(self):
        path = self._make("a.py", "alpha\n")
        crow.tool_read_file(path)
        self.assertIn("replaced 1 occurrence",
                      crow.tool_edit_file(self._path("A.PY"), old="alpha", new="beta"))
        self.assertEqual(self._text(path), "beta\n")


class RunToolResultTests(ToolLayerCase):
    """EVERY FAILURE IS A RESULT, NOT AN EXCEPTION (crow.py:1960-1983).

    A tool that raises kills the turn and costs the whole prefix; a tool that
    returns "no such file" lets the model correct itself in the next round. At
    ~10 tok/s a lost turn is minutes, so the difference is not cosmetic.
    """

    def test_a_raised_exception_becomes_result_text(self):
        def boom(**_):
            raise RuntimeError("the disk went away")

        self._install("boom", boom)
        result = crow.run_tool("boom", "{}")
        self.assertIn("boom failed", result)
        self.assertIn("the disk went away", result)

    def test_an_exception_type_nobody_anticipated_is_still_a_result(self):
        class Odd(Exception):
            pass

        def boom(**_):
            raise Odd("something new")

        self._install("odd", boom)
        self.assertIn("odd failed", crow.run_tool("odd", "{}"))

    def test_an_oserror_from_a_real_tool_is_a_result(self):
        """Not an injected tool: read_file on a directory comes back as text."""
        self.assertIn("error", crow.run_tool("read_file", json.dumps({"path": self.dir})))

    def test_arguments_that_are_not_json_are_a_result(self):
        self.assertIn("not valid JSON", crow.run_tool("read_file", "{not json"))

    def test_a_json_array_is_a_result(self):
        self.assertIn("must be a JSON object", crow.run_tool("read_file", "[1, 2]"))

    def test_an_unknown_tool_names_the_ones_that_exist(self):
        result = crow.run_tool("delete_everything", "{}")
        self.assertIn("no tool named", result)
        self.assertIn("read_file", result)

    def test_a_missing_required_argument_is_a_result(self):
        self.assertIn("wrong arguments for read_file", crow.run_tool("read_file", "{}"))

    def test_no_arguments_at_all_mean_an_empty_object(self):
        seen = {}

        def spy(**kwargs):
            seen.update(kwargs)
            return "ok"

        self._install("spy", spy)
        self.assertEqual(crow.run_tool("spy", ""), "ok")
        self.assertEqual(seen, {})

    def test_an_invented_argument_is_swallowed_by_the_tool(self):
        """Every signature ends in **_ . A model that adds a plausible argument
        gets its answer instead of losing the turn to a TypeError."""
        result = crow.run_tool("list_dir", json.dumps({"path": self.dir, "colour": "blue"}))
        self.assertNotIn("error", result)

    def test_a_typeerror_from_inside_a_tool_is_reported_as_wrong_arguments(self):
        """MEASURED BEHAVIOUR, not a wish: the TypeError branch (crow.py:1980) sits
        in front of the general one and cannot tell a bad call signature from a
        TypeError raised deep inside the implementation. A tool that trips over
        its own types therefore tells the model to fix arguments that were fine.
        Pinned so the move to crow_core.py cannot change it by accident -- and if
        it is ever changed on purpose, that is a CLI change with its own measurement.
        """
        def confused(**_):
            return 1 + "one"

        self._install("confused", confused)
        result = crow.run_tool("confused", "{}")
        self.assertIn("wrong arguments for confused", result)
        self.assertNotIn("confused failed", result)

    def test_the_dispatcher_covers_every_name_in_the_tools_list(self):
        """TOOLS is what the model is offered and TOOL_IMPL is what runs. A name
        in one and not the other is a tool the model will call and never reach."""
        offered = {t["function"]["name"] for t in crow.TOOLS}
        self.assertEqual(offered, set(crow.TOOL_IMPL))


class ToolResultCeilingTests(ToolLayerCase):
    """Every tool result is bounded -- and NOT every one of them is bounded by _clip.

    _clip's docstring says "EVERY tool result goes through here. No exceptions,
    and that is the point." Measured against the code that holds for read_file
    (:1740, :1768), list_dir (:1835) and run_command (:1919). find_files (:1854)
    and search_text (:1887) do NOT call _clip: they enforce the same two ceilings
    inline -- MAX_HITS and MAX_TOOL_BYTES -- and return their own "[stopped ...]"
    marker; write_file and edit_file return one short fixed line. So the invariant
    that actually holds is "bounded", not "clipped", and that is what is pinned
    here. The reason the ceiling exists at all is prefill: 200 hits of a common
    word are ~20,000 tokens, which was eight minutes at 38 tok/s.
    """

    # Room for the marker _clip appends, which is 55 characters today.
    SLACK = 100

    def _crowd(self, count=160, width=116):
        """A directory that overruns the byte ceiling before the hit ceiling."""
        for i in range(count):
            with open(self._path(f"{'n' * width}{i:03d}.txt"), "w", encoding="utf-8") as fh:
                fh.write("needle here\n")

    def test_short_text_comes_back_unchanged(self):
        self.assertEqual(crow._clip("hello"), "hello")

    def test_text_exactly_at_the_limit_is_not_touched(self):
        self.assertEqual(crow._clip("x" * 10, limit=10), "x" * 10)

    def test_one_byte_over_the_limit_is_cut(self):
        result = crow._clip("x" * 11, limit=10)
        self.assertTrue(result.startswith("x" * 10))
        self.assertIn("[cut at 10 bytes", result)

    def test_the_cut_says_what_to_do_about_it(self):
        self.assertIn("narrow the query", crow._clip("x" * 11, limit=10))

    def test_the_default_ceiling_is_the_constant_not_a_typed_number(self):
        result = crow._clip("x" * (crow.MAX_TOOL_BYTES + 1))
        self.assertIn(f"[cut at {crow.MAX_TOOL_BYTES} bytes", result)

    def test_read_file_is_clipped(self):
        path = self._make("big.txt", "x" * (crow.MAX_TOOL_BYTES + 5_000))
        result = crow.tool_read_file(path)
        self.assertIn(f"[cut at {crow.MAX_TOOL_BYTES} bytes", result)
        self.assertLessEqual(len(result), crow.MAX_TOOL_BYTES + self.SLACK)

    def test_a_line_range_is_clipped_too(self):
        """The range is the point, not a convenience -- but a range of 400 long
        lines is still 50,000 tokens, so it meets the same ceiling."""
        path = self._make("big.txt", "".join(f"{'y' * 120}\n" for _ in range(400)))
        result = crow.tool_read_file(path, start_line=1, end_line=400)
        self.assertIn(f"[cut at {crow.MAX_TOOL_BYTES} bytes", result)
        self.assertLessEqual(len(result), crow.MAX_TOOL_BYTES + self.SLACK)

    def test_list_dir_is_clipped(self):
        self._crowd()
        result = crow.tool_list_dir(self.dir)
        self.assertIn(f"[cut at {crow.MAX_TOOL_BYTES} bytes", result)
        self.assertLessEqual(len(result), crow.MAX_TOOL_BYTES + self.SLACK)

    def test_run_command_is_clipped(self):
        command = f'"{sys.executable}" -c "print(\'x\' * {crow.MAX_TOOL_BYTES + 5_000})"'
        result = crow.tool_run_command(command, cwd=self.dir)
        self.assertIn(f"[cut at {crow.MAX_TOOL_BYTES} bytes", result)
        self.assertLessEqual(len(result), crow.MAX_TOOL_BYTES + self.SLACK)

    def test_find_files_stops_on_bytes_and_says_so(self):
        """Its own marker, not _clip's: it stops walking rather than walking to
        the end and throwing the tail away."""
        self._crowd()
        result = crow.tool_find_files(self.dir, "*.txt")
        self.assertIn("[stopped", result)
        self.assertNotIn("[cut at", result)
        self.assertLess(len(result), crow.MAX_TOOL_BYTES * 2)

    def test_search_text_stops_on_bytes_and_says_so(self):
        self._crowd()
        result = crow.tool_search_text(self.dir, "needle")
        self.assertIn("[stopped", result)
        self.assertNotIn("[cut at", result)
        self.assertLess(len(result), crow.MAX_TOOL_BYTES * 2)

    def test_a_hit_count_alone_would_not_have_bounded_the_size(self):
        """The measured defect behind both ceilings: 160 hits is well under
        MAX_HITS and still overruns the byte budget, so the byte one is the one
        that fires here."""
        self._crowd()
        self.assertLess(160, crow.MAX_HITS)
        self.assertIn("[stopped", crow.tool_find_files(self.dir, "*.txt"))

    def test_the_short_results_need_no_ceiling(self):
        """write_file and edit_file return one fixed line, so they are bounded by
        construction and not by a call to _clip."""
        path = self._path("new.txt")
        self.assertLess(len(crow.tool_write_file(path, "x" * 50_000)), 200)
        crow.tool_read_file(path)
        self.assertLess(len(crow.tool_edit_file(path, old="x" * 50_000, new="y")), 200)


class RunToolCachedTests(ToolLayerCase):
    """The loop this prevents, observed 2026-08-09 (crow.py:1938-1950): the model
    asked for a file that does not exist, got an error, and asked for the same
    path again -- eight times, twice within a single round.
    """

    def _counter(self, name):
        calls = []

        def impl(**kwargs):
            calls.append(kwargs)
            return f"ran {len(calls)}"

        self._install(name, impl)
        return calls

    def test_the_first_call_runs_and_is_not_a_repeat(self):
        self._counter("count")
        result, repeated = crow.run_tool_cached("count", "{}")
        self.assertEqual(result, "ran 1")
        self.assertFalse(repeated)

    def test_the_second_identical_call_is_marked_as_a_repeat(self):
        self._counter("count")
        crow.run_tool_cached("count", "{}")
        result, repeated = crow.run_tool_cached("count", "{}")
        self.assertTrue(repeated)
        self.assertIn("you already called count", result)

    def test_the_repeat_carries_the_first_result_back(self):
        self._counter("count")
        crow.run_tool_cached("count", "{}")
        self.assertIn("ran 1", crow.run_tool_cached("count", "{}")[0])

    def test_the_tool_does_not_run_a_second_time(self):
        """The whole point: re-running produces the identical failure and pays a
        second prefill for it."""
        calls = self._counter("count")
        crow.run_tool_cached("count", "{}")
        crow.run_tool_cached("count", "{}")
        self.assertEqual(len(calls), 1)

    def test_different_arguments_are_not_a_repeat(self):
        calls = self._counter("count")
        crow.run_tool_cached("count", '{"path": "a"}')
        _, repeated = crow.run_tool_cached("count", '{"path": "b"}')
        self.assertFalse(repeated)
        self.assertEqual(len(calls), 2)

    def test_the_same_arguments_to_another_tool_are_not_a_repeat(self):
        self._counter("count")
        self._counter("other")
        crow.run_tool_cached("count", "{}")
        self.assertFalse(crow.run_tool_cached("other", "{}")[1])

    def test_the_measured_case_a_missing_file_asked_for_twice(self):
        args = json.dumps({"path": self._path("server-context.c")})
        first, repeated_first = crow.run_tool_cached("read_file", args)
        second, repeated_second = crow.run_tool_cached("read_file", args)
        self.assertFalse(repeated_first)
        self.assertIn("no such file", first)
        self.assertTrue(repeated_second)
        self.assertIn("no such file", second)


class CacheKeyIsTheRealInputTests(ToolLayerCase):
    """#93. The cache keyed on (name, arguments); two tools depend on more.

    MEASURED 2026-08-14, in the run that closed #55 -- the first real agent run
    written down. `write_file` was refused for want of a read, `read_file`
    supplied it, and the identical `write_file` came back as a repeat carrying
    the OLD REFUSAL. Three times, until the model said "the write tool is being
    stubborn about the read-before-write ordering" and reached for `edit_file`.
    In the same turn a `run_command` after an `edit_file` replayed the output
    from before the edit; the model appended `2>&1` to change the key rather
    than the command. 4 of that turn's 12 calls were replays of a state that had
    moved, and two of its 13 rounds existed only to get around them.

    None of it was visible to 487 green tests, because every one of them
    repeated a call with nothing happening in between.
    """

    def _args(self, path, **rest):
        return json.dumps(dict(path=path, **rest))

    # -- the two measured cases ---------------------------------------------

    def test_a_read_between_two_writes_lets_the_second_one_run(self):
        """THE CASE FROM THE RUN. The refusal names the way out; taking it has
        to work, or the refusal is a dead end wearing instructions."""
        path = self._make("target.txt", "old")
        args = self._args(path, content="new")

        first, repeated = crow.run_tool_cached("write_file", args)
        self.assertIn("refusing to overwrite", first)
        self.assertFalse(repeated)

        crow.run_tool_cached("read_file", self._args(path))

        second, repeated = crow.run_tool_cached("write_file", args)
        self.assertFalse(repeated, "the refusal was replayed after the read lifted it")
        self.assertIn("wrote", second)

    def test_and_the_file_on_disk_actually_changes(self):
        """The result text is not the point -- the byte on disk is. A fix that
        returns a fresh success while writing nothing passes the case above."""
        path = self._make("target.txt", "old")
        args = self._args(path, content="new")
        crow.run_tool_cached("write_file", args)
        crow.run_tool_cached("read_file", self._args(path))
        crow.run_tool_cached("write_file", args)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "new")

    def test_edit_file_follows_the_same_rule(self):
        path = self._make("target.txt", "alpha")
        args = json.dumps({"path": path, "old": "alpha", "new": "beta"})
        self.assertIn("before editing it", crow.run_tool_cached("edit_file", args)[0])
        crow.run_tool_cached("read_file", self._args(path))
        result, repeated = crow.run_tool_cached("edit_file", args)
        self.assertFalse(repeated)
        self.assertNotIn("before editing it", result)

    def test_run_command_is_never_answered_from_the_cache(self):
        """Its result is a function of the whole filesystem, so the arguments
        were never the whole key."""
        calls = []

        def impl(**kwargs):
            calls.append(kwargs)
            return f"ran {len(calls)}"

        self._install("run_command", impl)
        self.assertEqual(crow.run_tool_cached("run_command", "{}")[0], "ran 1")
        result, repeated = crow.run_tool_cached("run_command", "{}")
        self.assertEqual(result, "ran 2")
        self.assertFalse(repeated)
        self.assertEqual(len(calls), 2)

    def test_run_command_leaves_nothing_behind_to_answer_from(self):
        """"Do not cache" has to mean the write-back too. Storing the result
        while refusing to read it is a key that becomes live the moment somebody
        simplifies the read side."""
        self._install("run_command", lambda **k: "out")
        crow.run_tool_cached("run_command", "{}")
        self.assertEqual([k for k in crow._SEEN if k[0] == "run_command"], [])

    # -- the negative half: what MUST still be cached ------------------------

    def test_the_2026_08_09_loop_stays_closed(self):
        """NEGATIVE CONTROL, and the reason this ticket is not "delete the cache".

        The loop that built the cache happened on `read_file` for a path that
        does not exist -- and a path does not start existing because it was
        asked for twice. A fix that stops caching goes green on everything above
        and red here.
        """
        missing = self._path("server-context.c")
        first, repeated = crow.run_tool_cached("read_file", self._args(missing))
        self.assertIn("no such file", first)
        self.assertFalse(repeated)
        second, repeated = crow.run_tool_cached("read_file", self._args(missing))
        self.assertTrue(repeated, "the 2026-08-09 loop is open again")
        self.assertIn("you already called read_file", second)

    def test_a_write_repeated_with_nothing_in_between_is_still_a_repeat(self):
        """The loop prevention still covers the write tools. Only a CHANGE of
        the state they depend on reopens them, not the mere fact of being one."""
        path = self._make("target.txt", "old")
        args = self._args(path, content="new")
        crow.run_tool_cached("write_file", args)
        result, repeated = crow.run_tool_cached("write_file", args)
        self.assertTrue(repeated)
        self.assertIn("refusing to overwrite", result)

    def test_a_read_of_a_DIFFERENT_file_does_not_reopen_the_write(self):
        """The key carries whether THIS path was read, not whether any read
        happened. Otherwise one read unlocks every pending refusal at once."""
        target = self._make("target.txt", "old")
        other = self._make("other.txt", "x")
        args = self._args(target, content="new")
        crow.run_tool_cached("write_file", args)
        crow.run_tool_cached("read_file", self._args(other))
        self.assertTrue(crow.run_tool_cached("write_file", args)[1])

    def test_a_call_with_no_usable_path_is_still_cached(self):
        """Its failure IS a function of its arguments -- bad arguments stay bad
        -- so it belongs in the cache like any other argument error."""
        crow.run_tool_cached("write_file", "{not json")
        self.assertTrue(crow.run_tool_cached("write_file", "{not json")[1])

    def test_every_name_in_the_rules_is_a_tool_that_exists(self):
        """A rule naming a tool nobody ships is a rule that never fires, and it
        would read as protection. Catches a rename that misses these sets."""
        known = set(crow.TOOL_IMPL)
        self.assertLessEqual(crow.NEVER_CACHED, known)
        self.assertLessEqual(crow.READ_GATED, known)


class ToolStateLifetimeTests(ToolLayerCase):
    """What "already read" means today, pinned before anything gives it a scope.

    Today it means "read in this PROCESS". _READ is module-global with six
    occurrences and no clear() and no del anywhere (crow.py:1684); _SEEN is
    emptied in exactly one place and that place is repl() (:2478), which neither
    a tool nor the dispatcher touches. Whether "read" should mean the process,
    the session or the turn is a change in what the CLI does, not a move -- so
    these three cases go red the moment the lifetime changes, which is the whole
    reason they are here rather than left implicit.
    """

    def test_no_tool_empties_the_read_set(self):
        first = self._make("a.txt", "alpha")
        second = self._make("b.txt", "beta")
        crow.tool_read_file(first)
        crow.tool_read_file(second)
        crow.tool_write_file(self._path("c.txt"), "gamma")
        crow.tool_list_dir(self.dir)
        crow.tool_find_files(self.dir, "*.txt")
        self.assertEqual(len(crow._READ), 3)

    def test_the_dispatcher_does_not_empty_it_either(self):
        path = self._make("a.txt", "alpha")
        crow.run_tool("read_file", json.dumps({"path": path}))
        crow.run_tool("list_dir", json.dumps({"path": self.dir}))
        self.assertIn(crow._key(path), crow._READ)

    def test_nothing_in_the_tool_layer_empties_the_seen_map(self):
        crow.run_tool_cached("list_dir", json.dumps({"path": self.dir}))
        crow.run_tool_cached("find_files", json.dumps({"root": self.dir}))
        crow.run_tool("list_dir", json.dumps({"path": self.dir}))
        self.assertEqual(len(crow._SEEN), 2)


def _tool_call_delta(name, arguments, index=0, cid=None):
    """One streamed `tool_calls` delta, in the shape the server sends."""
    return {"tool_calls": [{"index": index, "id": cid or f"c{index}",
                            "function": {"name": name, "arguments": arguments}}]}


class ReadScopeIsOneTurnTests(ToolLayerCase):
    """E6: how long "already read" lasts, and it is checked in BOTH directions.

    A ONE-DIRECTION CASE HERE WOULD CHECK NOTHING. "The write is refused after
    the boundary" is satisfied by a rule that refuses always, and "the write goes
    through before it" is satisfied by a rule that never refuses. Only the pair
    pins a SCOPE rather than a rule, which is why every boundary below is spelled
    out twice -- once from each side.

    THE SCOPE IS ONE USER TURN, and it was chosen by a measurement whose
    threshold was written down first: null cases of a write landing on a file
    last read in an earlier turn makes the turn scope free, one or more and the
    scope is the session. Counted 2026-08-12 over 3 distinct rollover/session
    files, 25 user turns, 31 read_file calls and 4 write_file/edit_file calls:
    RESULT 0. The numbers are kept beside `_READ` in cli/crow_core.py, because a
    measurement that lives only in a chat is gone by the next reading.

    THESE RUN THROUGH `run_turn`, NOT THROUGH THE TOOLS. That is the whole
    difference between this class and `ToolStateLifetimeTests` above, and both
    are true at once: nothing in the TOOL LAYER empties the set -- no tool, no
    dispatcher -- and the TURN LOOP one level up empties it on the way in. A case
    that called `tool_read_file` and `tool_write_file` directly would never cross
    a turn boundary and would stay green whatever the scope became.
    """

    def setUp(self):
        super().setUp()
        # Patched on `crow_core`, never on `crow`: `run_turn` and `stream_reply`
        # both live there and look the transport up as a module global at call
        # time. Rebinding `crow._post_stream` would leave the real one in place
        # and the test would reach for a socket.
        self._post_stream_before = crow.crow_core._post_stream
        self.addCleanup(self._restore_transport)
        crow.crow_core._post_stream = self._serve
        self.script = []

    def _restore_transport(self):
        crow.crow_core._post_stream = self._post_stream_before

    def _serve(self, url, body, api_key, timeout):
        if not self.script:
            raise AssertionError("the loop asked for a round that was not scripted")
        deltas = self.script.pop(0)
        for delta in deltas:
            yield json.dumps({"choices": [{"delta": delta}]})
        yield json.dumps({"choices": [], "timings": {"predicted_n": 1}})

    def serve(self, deltas):
        """Add one scripted round, as the list of deltas it streams."""
        self.script.append(list(deltas))
        return self

    def turn(self, talk, line="go"):
        """One USER turn, the way `repl()` runs one: append the line, then loop."""
        talk.append("user", line)
        return crow.run_turn(
            talk, base_url="http://x/v1", model="crow", api_key="k",
            temperature=0.0, top_p=1.0, min_p=0.0, timeout=1.0)

    def _results(self, talk):
        return [m["content"] for m in talk.payload() if m["role"] == "tool"]

    def _reads(self, path):
        return _tool_call_delta("read_file", json.dumps({"path": path}))

    def _writes(self, path, content):
        return _tool_call_delta("write_file", json.dumps({"path": path,
                                                          "content": content}))

    # ---- the boundary, from the side where the write must still go through ----

    def test_a_read_and_a_write_in_the_same_turn_go_through(self):
        """The half that stops "refuse everything" from passing as a scope.

        This is the ordinary working case and it must not have been broken by
        giving the set a lifetime: within ONE turn the rule behaves exactly as
        it did when nothing ever emptied it.
        """
        path = self._make("notes.txt", "old")
        talk = crow.Conversation("SYS")
        self.serve([self._reads(path)])
        self.serve([self._writes(path, "new")])
        self.serve([{"content": "done"}])
        self.turn(talk)
        self.assertIn("wrote", self._results(talk)[-1])
        self.assertEqual(self._text(path), "new")

    def test_a_second_read_in_the_new_turn_wins_the_write_back(self):
        """E6 point 4: the way out, and it is the grip the rule already asks for.

        Without this the new refusal would be a dead end, and a dead end is what
        would have forced a force-overwrite flag onto a rule whose failure mode
        is losing someone's work. Reading the file again is enough.
        """
        path = self._make("notes.txt", "old")
        talk = crow.Conversation("SYS")
        self.serve([self._reads(path)])
        self.serve([{"content": "read it"}])
        self.turn(talk)

        self.serve([self._reads(path)])
        self.serve([self._writes(path, "new")])
        self.serve([{"content": "done"}])
        self.turn(talk, "now write it")
        self.assertIn("wrote", self._results(talk)[-1])
        self.assertEqual(self._text(path), "new")

    # ---- and from the side where it must now be refused --------------------

    def test_a_write_in_the_next_turn_is_refused(self):
        """THE ADDED REFUSAL, where there was none before E6.

        The file was read -- in the turn before. Under the process scope this
        write went through; under the turn scope it does not, and this case is
        the one that goes red if the clear is taken back out.
        """
        path = self._make("notes.txt", "the work that must survive")
        talk = crow.Conversation("SYS")
        self.serve([self._reads(path)])
        self.serve([{"content": "read it"}])
        self.turn(talk)

        self.serve([self._writes(path, "overwritten")])
        self.serve([{"content": "I could not"}])
        self.turn(talk, "now write it")
        self.assertIn("refusing to overwrite", self._results(talk)[-1])
        self.assertEqual(self._text(path), "the work that must survive")

    def test_the_refusal_names_the_turn_it_means(self):
        """A refusal that says "without reading it first" to someone who DID read
        it reads as a bug. The message has to name the scope it is enforcing."""
        path = self._make("notes.txt", "x")
        talk = crow.Conversation("SYS")
        self.serve([self._reads(path)])
        self.serve([{"content": "read it"}])
        self.turn(talk)

        self.serve([self._writes(path, "y")])
        self.serve([{"content": "I could not"}])
        self.turn(talk, "now write it")
        said = self._results(talk)[-1]
        self.assertIn("in this turn", said)
        self.assertIn("read_file", said)

    def test_an_edit_in_the_next_turn_is_refused_too(self):
        """`edit_file` consults the same set through a different door
        (crow_core.py, the `_key(path) not in _READ` check). One scope, and both
        callers have to be under it."""
        path = self._make("code.py", "alpha\n")
        talk = crow.Conversation("SYS")
        self.serve([self._reads(path)])
        self.serve([{"content": "read it"}])
        self.turn(talk)

        self.serve([_tool_call_delta("edit_file", json.dumps(
            {"path": path, "old": "alpha", "new": "beta"}))])
        self.serve([{"content": "I could not"}])
        self.turn(talk, "now edit it")
        self.assertIn("before editing it", self._results(talk)[-1])
        self.assertEqual(self._text(path), "alpha\n")

    def test_a_new_session_refuses_what_the_old_one_had_read(self):
        """THE CASE THE STAGE WAS NAMED FOR, and under the turn scope it is a
        superset rather than a separate rule: a new session's first turn is a new
        turn, so the boundary has already been crossed.

        A session is modelled here as what `repl()` actually holds one of -- a
        `Conversation`. The point of the case is not the object: it is that
        "read" must not survive into a window that a second surface can open
        without the process ever ending.
        """
        path = self._make("notes.txt", "the work that must survive")
        first = crow.Conversation("SYS")
        self.serve([self._reads(path)])
        self.serve([{"content": "read it"}])
        self.turn(first)
        self.assertIn(crow._key(path), crow._READ)

        second = crow.Conversation("SYS")
        self.serve([self._writes(path, "overwritten")])
        self.serve([{"content": "I could not"}])
        self.turn(second, "write it")
        self.assertIn("refusing to overwrite", self._results(second)[-1])
        self.assertEqual(self._text(path), "the work that must survive")

    # ---- the two names share one lifetime, which is the half-state ----------

    def test_the_result_cache_is_emptied_on_the_same_boundary(self):
        """THE HALF-STATE, CHECKED RATHER THAN TRUSTED. `_READ` emptied without
        `_SEEN` refuses the write correctly while still handing back a tool
        result produced in the turn before. Same boundary, both names, one pair
        of statements in the core."""
        talk = crow.Conversation("SYS")
        args = json.dumps({"path": self.dir})
        self.serve([_tool_call_delta("list_dir", args)])
        self.serve([{"content": "looked"}])
        self.turn(talk)

        self.serve([_tool_call_delta("list_dir", args)])
        self.serve([{"content": "looked again"}])
        self.turn(talk, "again")
        self.assertNotIn("you already called", self._results(talk)[-1])

    def test_both_names_are_empty_once_the_turn_has_started(self):
        """The direct reading of the same fact, so a failure says WHICH name.

        The tools run inside the turn, so the state is sampled from one of them
        rather than from outside: after the turn, both have been filled again.
        """
        seen = {}

        def probe(**kw):
            seen["read"] = set(crow._READ)
            seen["cached"] = dict(crow._SEEN)
            return "probed"

        self._install("probe", probe)
        path = self._make("notes.txt", "old")
        talk = crow.Conversation("SYS")
        self.serve([self._reads(path)])
        self.serve([{"content": "read it"}])
        self.turn(talk)
        self.assertIn(crow._key(path), crow._READ)

        self.serve([_tool_call_delta("probe", "{}")])
        self.serve([{"content": "done"}])
        self.turn(talk, "probe")
        self.assertEqual(seen["read"], set())
        self.assertEqual(seen["cached"], {})


class WorkingDirectoryBoundaryTests(ToolLayerCase):
    """#92: a write outside the root is refused WITHOUT asking, at every level.

    THE NEGATIVE HALF IS THE ONLY REASON THIS SUITE MEANS ANYTHING. A boundary
    that refuses every path passes "a write above the root is refused", "a write
    through `..` is refused" and "a write through a symlink is refused" -- all
    three, perfectly, while making the tool useless. So every refusal below is
    paired with `test_a_deep_path_inside_the_root_is_written`, and a change that
    breaks the pairing shows up there rather than in a green run.

    THE ROOT IS NOT SET BY DEFAULT. `_ROOT` starts as None and a surface that
    never calls `set_root` keeps the behaviour every release up to 0.3.2 had, so
    `test_without_a_root_nothing_is_refused` pins that the core does not invent a
    policy nobody asked for. It is the second negative half.

    Reset via `crow.set_root(None)`, never by touching `_ROOT`: the name is a
    module-level string in crow_core, so `crow.py` re-exporting it would bind the
    VALUE and a rebinding here would move a copy while the tools went on reading
    the core's. That is the trap `ToolLayerCase` documents for `_READ`, in the
    one shape where it actually bites -- `_READ` is a set and survives in-place
    mutation; a string does not.
    """

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.dir, "projekt")
        os.makedirs(self.root)
        crow.write_root_mode(self.root, "auto")
        self.addCleanup(crow.set_root, None)
        crow.set_root(self.root)

    def _in_root(self, *parts):
        return os.path.join(self.root, *parts)

    # --- refused ---------------------------------------------------------

    def test_a_write_above_the_root_is_refused(self):
        out = crow.tool_write_file(os.path.join(self.dir, "daneben.txt"), "x")
        self.assertIn("refusing to write outside", out)

    def test_a_write_reached_through_dotdot_is_refused(self):
        """`abspath` would normalise this to the same place -- and still be wrong
        the moment a link is in the way, which is why `_resolve` uses realpath."""
        out = crow.tool_write_file(self._in_root("..", "raus.txt"), "x")
        self.assertIn("refusing to write outside", out)

    def test_a_sibling_whose_name_merely_starts_with_the_root_is_refused(self):
        """MEASURED 2026-08-14: `"C:\\root2\\x".startswith("C:\\root")` is True.

        A boundary written with a bare startswith passes every other case in this
        class and lets this one through, which is why the separator is part of
        the comparison in `_inside`.
        """
        out = crow.tool_write_file(os.path.join(self.dir, "projekt2", "x.txt"), "x")
        self.assertIn("refusing to write outside", out)

    def test_another_drive_is_refused_rather_than_raising(self):
        """`commonpath`/`relpath` raise ValueError across drives instead of
        answering "no" -- measured 2026-08-14. An escaping exception does not
        refuse the write, it ends the turn."""
        out = crow.tool_write_file(r"Z:\evil.txt", "x")
        self.assertIn("refusing to write outside", out)

    def test_a_write_through_a_symlink_pointing_out_is_refused(self):
        link = self._in_root("link")
        try:
            os.symlink(self.dir, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"no symlink privilege here: {exc}")
        out = crow.tool_write_file(os.path.join(link, "raus.txt"), "x")
        self.assertIn("refusing to write outside", out)

    def test_edit_file_is_bounded_too(self):
        path = os.path.join(self.dir, "fremd.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("alt")
        crow.tool_read_file(path)                       # reads are NOT bounded
        out = crow.tool_edit_file(path, old="alt", new="neu")
        self.assertIn("refusing to write outside", out)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "alt")

    def test_the_refused_bytes_are_untouched(self):
        path = os.path.join(self.dir, "wichtig.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("die arbeit")
        crow.tool_write_file(path, "weg")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "die arbeit")

    # --- and NOT refused -------------------------------------------------

    def test_a_deep_path_inside_the_root_is_written(self):
        """THE NEGATIVE HALF. Without it a boundary that refuses everything passes."""
        path = self._in_root("a", "b", "c", "tief.py")
        out = crow.tool_write_file(path, "print(1)")
        self.assertNotIn("refusing", out)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "print(1)")

    def test_the_root_itself_is_inside_itself(self):
        out = crow.tool_write_file(self._in_root("oben.txt"), "x")
        self.assertNotIn("refusing", out)

    def test_without_a_root_nothing_is_refused(self):
        """The second negative half: no boundary is a valid state, not a bug."""
        crow.set_root(None)
        out = crow.tool_write_file(os.path.join(self.dir, "frei.txt"), "x")
        self.assertNotIn("refusing", out)

    def test_reads_are_not_bounded(self):
        """robin's decision on #92: a read boundary makes the model blind to its
        own installation, and a read destroys nothing."""
        path = os.path.join(self.dir, "draussen.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("lesbar")
        self.assertEqual(crow.tool_read_file(path), "lesbar")

    def test_run_command_is_not_bounded(self):
        """RECORDED DECISION, not an oversight (#92 point 3). A `cwd` inside the
        root says nothing about what the command does, so run_command stands on
        #88's `executing` class instead. If this case ever goes red, the decision
        changed and the ticket has to say so."""
        self.assertNotIn("refusing to write outside",
                         crow.tool_run_command("cd", cwd=self.dir))

    # --- shape of the refusal -------------------------------------------

    def test_the_refusal_names_the_root(self):
        out = crow.tool_write_file(os.path.join(self.dir, "x.txt"), "x")
        self.assertIn(self.root, out)

    def test_the_refusal_is_a_tool_result_not_an_exception(self):
        """Same invariant as #88's decline: an assistant turn whose tool_calls
        have no `tool` message behind them is a broken prefix for every later
        turn."""
        out = crow.tool_write_file(r"Z:\nope\x.txt", "x")
        self.assertIsInstance(out, str)
        self.assertTrue(out.startswith("error: "))

    def test_the_boundary_answers_before_read_before_write(self):
        """An existing, unread file OUTSIDE the root gets the boundary, not
        "read it first" -- otherwise the model reads it (reads are allowed) and
        pays a second round for a refusal that needed no state."""
        path = os.path.join(self.dir, "fremd.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("alt")
        out = crow.tool_write_file(path, "neu")
        self.assertIn("refusing to write outside", out)
        self.assertNotIn("refusing to overwrite", out)

    # --- finding the root ------------------------------------------------

    def test_find_root_walks_up_to_the_marker(self):
        deep = self._in_root("a", "b")
        os.makedirs(deep)
        self.assertEqual(os.path.normcase(crow.find_root(deep)),
                         os.path.normcase(os.path.realpath(self.root)))

    def test_find_root_is_none_without_a_marker(self):
        plain = os.path.join(self.dir, "ohne")
        os.makedirs(plain)
        self.assertIsNone(crow.find_root(plain))

    def test_find_root_takes_the_nearest_marker_not_the_highest(self):
        inner = self._in_root("unter")
        os.makedirs(inner)
        crow.write_root_mode(inner, "auto")
        self.assertEqual(os.path.normcase(crow.find_root(inner)),
                         os.path.normcase(os.path.realpath(inner)))

    def test_a_bare_crow_directory_is_NOT_a_root(self):
        """THE CORRECTION OF 2026-08-14, and the case that caught it.

        `.crow/` is created by SPILL_DIR wherever crow runs. Measured that day:
        `C:\\Users\\robin\\.crow` existed, dated 2026-08-08, from one session
        started in the home directory -- so a `.crow/`-is-the-marker rule made the
        entire user profile a root and the boundary decoration. A directory
        becomes a root when someone declares it, never as a side effect.
        """
        spill = os.path.join(self.dir, "zufall")
        os.makedirs(os.path.join(spill, crow.ROOT_MARKER))       # by-product only
        self.assertIsNone(crow.find_root(spill))

    def test_a_root_remembers_its_level(self):
        crow.write_root_mode(self.root, "manual")
        self.assertEqual(crow.read_root_mode(self.root), "manual")

    def test_a_root_with_an_unreadable_file_answers_none_rather_than_raising(self):
        """The boundary is the security mechanism; the remembered level is a
        convenience. A broken convenience must not take the boundary down."""
        with open(crow.root_file(self.root), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        self.assertIsNone(crow.read_root_mode(self.root))
        self.assertIn("refusing to write outside",
                      crow.tool_write_file(os.path.join(self.dir, "x.txt"), "x"))

    def test_an_unknown_level_in_the_file_is_not_trusted(self):
        with open(crow.root_file(self.root), "w", encoding="utf-8") as fh:
            json.dump({"mode": "godmode"}, fh)
        self.assertIsNone(crow.read_root_mode(self.root))


class RootSurvivesTheSessionTests(unittest.TestCase):
    """#92: the chosen directory is part of the session, so reopening restores it.

    robin's requirement, stated while this was being built: "die auswahl muss je
    session dann persistent gespeichert sein". A boundary that has to be re-picked
    on every start is one people turn off.

    THE FIELD IS ADDED, NOT SUBSTITUTED, and `SessionFormatGateTests` case (d) is
    why that is allowed: an older build reads five keys and ignores the rest, so a
    session written here still opens in 0.3.2 -- simply unbounded, which is the
    state that build was already in. `test_an_older_build_still_reads_it` holds
    that claim as a case rather than leaving it an assumption.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crow-rootsess-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._real = (crow.SESSION_DIR, crow.SESSION_FILE, crow.post_json,
                      crow_core.ROOTS_FILE)
        self.addCleanup(self._restore)
        self.addCleanup(crow.set_root, None)
        crow.SESSION_DIR = self.dir
        crow.SESSION_FILE = os.path.join(self.dir, "session.json")
        crow_core.ROOTS_FILE = os.path.join(self.dir, "roots.json")
        crow.post_json = lambda *a, **k: {}
        self.root = os.path.join(self.dir, "projekt")
        os.makedirs(self.root)
        crow.write_root_mode(self.root, "allowedit")

    def _restore(self):
        (crow.SESSION_DIR, crow.SESSION_FILE, crow.post_json,
         crow_core.ROOTS_FILE) = self._real

    def _talk(self):
        talk = crow.Conversation("SYS")
        talk.append("user", "hi")
        return talk

    def _saved(self):
        with open(crow.SESSION_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_standing_inside_a_declared_project_beats_the_last_pick(self):
        """THE NEGATIVE HALF of the fallback: it fills a silence, it does not
        overrule where you actually are. Without this, opening a terminal inside
        project B would bind project A because A was picked last."""
        other = os.path.join(self.dir, "hier")
        os.makedirs(other)
        crow.write_root_mode(other, "auto")
        crow.write_root_mode(self.root, "auto")
        crow.remember_root(self.root)                  # picked last, elsewhere
        crow.set_root(None)
        with mock.patch.object(crow_core, "find_root", return_value=other):
            root, _, _ = crow.adopt_root(None, None)
        self.assertEqual(os.path.normcase(root or ""),
                         os.path.normcase(other))

    def test_an_older_build_still_reads_it(self):
        """Case (d) of the format gate, for this field: the five keys 0.2.0 reads
        are all still there and mean what they meant."""
        crow.set_root(self.root)
        crow.save_session(self._talk(), "http://127.0.0.1:8081", 42)
        saved = self._saved()
        for key in ("version", "kv", "kv_tokens", "context_tokens", "prefix",
                    "messages"):
            self.assertIn(key, saved)
        self.assertEqual([m["content"] for m in saved["messages"]][-1], "hi")


class AdoptRootTests(unittest.TestCase):
    """#92: one rule for both surfaces -- what `--root` and the picker resolve to."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crow-adopt-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._roots = crow_core.ROOTS_FILE
        crow_core.ROOTS_FILE = os.path.join(self.dir, "roots.json")
        self.addCleanup(setattr, crow_core, "ROOTS_FILE", self._roots)
        self.addCleanup(crow.set_root, None)
        crow.set_root(None)

    def test_a_stated_directory_becomes_a_root(self):
        target = os.path.join(self.dir, "neu")
        os.makedirs(target)
        root, mode, problem = crow.adopt_root(target, None)
        self.assertIsNone(problem)
        self.assertTrue(os.path.isfile(crow.root_file(target)))
        self.assertEqual(os.path.normcase(root),
                         os.path.normcase(os.path.realpath(target)))
        self.assertEqual(mode, crow.DEFAULT_MODE)

    def test_a_stated_directory_that_is_not_there_is_a_problem_not_a_crash(self):
        root, _, problem = crow.adopt_root(os.path.join(self.dir, "gibtsnicht"), None)
        self.assertIsNone(root)
        self.assertIn("no such directory", problem)

    def test_a_stated_level_is_stored_with_the_root(self):
        target = os.path.join(self.dir, "mit-level")
        os.makedirs(target)
        crow.adopt_root(target, "manual")
        self.assertEqual(crow.read_root_mode(target), "manual")

    def test_the_stored_level_fills_a_silence(self):
        target = os.path.join(self.dir, "erinnert")
        os.makedirs(target)
        crow.write_root_mode(target, "manual")
        crow.set_root(None)
        _, mode, _ = crow.adopt_root(target, None)
        self.assertEqual(mode, "manual")

    def test_a_stated_level_beats_the_stored_one(self):
        """THE NEGATIVE HALF of the case above: a memory may fill a silence, never
        overrule a flag typed this minute."""
        target = os.path.join(self.dir, "ueberschrieben")
        os.makedirs(target)
        crow.write_root_mode(target, "manual")
        crow.set_root(None)
        _, mode, _ = crow.adopt_root(target, "auto")
        self.assertEqual(mode, "auto")
        self.assertEqual(crow.read_root_mode(target), "auto")

    def test_nothing_stated_and_nothing_declared_leaves_it_unbounded(self):
        with mock.patch.object(crow_core, "find_root", return_value=None):
            root, mode, problem = crow.adopt_root(None, None)
        self.assertIsNone(root)
        self.assertIsNone(problem)
        self.assertEqual(mode, crow.DEFAULT_MODE)


if __name__ == "__main__":
    unittest.main(verbosity=2)

