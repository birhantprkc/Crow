#!/usr/bin/env python3
"""Suite for cli/crow.py. Standard library only, same as the CLI itself.

Run:  python cli/test_crow.py

Every group carries at least one case that must FAIL if the behaviour it
guards regresses -- a suite that cannot go red proves nothing.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crow  # noqa: E402


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
            text, reasoning, timings = crow.stream_reply(
                conversation if conversation is not None else crow.Conversation("SYS"),
                base_url="http://x/v1", model="crow",
                api_key="k", temperature=0.0, timeout=1.0, out=sink, **kw)
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
                api_key="k", temperature=0.0, timeout=1.0, out=io.StringIO())
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

