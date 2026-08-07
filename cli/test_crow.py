#!/usr/bin/env python3
"""Suite for cli/crow.py. Standard library only, same as the CLI itself.

Run:  python cli/test_crow.py

Every group carries at least one case that must FAIL if the behaviour it
guards regresses -- a suite that cannot go red proves nothing.
"""

from __future__ import annotations

import io
import json
import sys
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

    def _run(self, deltas, **kw):
        """Drive stream_reply against a canned SSE stream. Returns (text, timings, printed)."""
        chunks = [json.dumps({"choices": [{"delta": d}]}) for d in deltas]
        chunks.append(json.dumps({"choices": [], "timings": {"predicted_n": 7}}))
        original = crow._post_stream
        crow._post_stream = lambda url, body, key, timeout: iter(chunks)
        sink = io.StringIO()
        try:
            text, timings = crow.stream_reply(
                crow.Conversation("SYS"), base_url="http://x/v1", model="crow",
                api_key="k", temperature=0.0, timeout=1.0, out=sink, **kw)
        finally:
            crow._post_stream = original
        return text, timings, sink.getvalue()

    def test_reasoning_is_counted_but_not_printed(self):
        """It is 60-90 % of every answer; printed in full it buries the code."""
        _, timings, printed = self._run([{"reasoning_content": "let me think"},
                                         {"content": "ANSWER"}])
        self.assertNotIn("let me think", printed)
        self.assertIn("ANSWER", printed)
        self.assertEqual(timings["_reasoning_chars"], len("let me think"))

    def test_reasoning_never_enters_the_returned_text(self):
        """It is display-only -- feeding it back would change the cached prefix."""
        text, _, _ = self._run([{"reasoning_content": "SECRET THOUGHTS"},
                                {"content": "ANSWER"}])
        self.assertEqual(text, "ANSWER")
        self.assertNotIn("SECRET", text)

    def test_ttft_counts_the_first_token_of_any_kind(self):
        """The defect: ttft used to start at the first CONTENT token, so it
        silently contained the entire thinking phase."""
        _, timings, _ = self._run([{"reasoning_content": "x" * 50},
                                   {"content": "A"}])
        self.assertIn("_client_ttft_s", timings)
        self.assertIn("_client_answer_s", timings)
        self.assertLessEqual(timings["_client_ttft_s"], timings["_client_answer_s"])

    def test_thinking_share_is_reported(self):
        _, timings, _ = self._run([{"reasoning_content": "1234567890" * 9},
                                   {"content": "1234567890"}])
        self.assertEqual(timings["_reasoning_chars"], 90)
        self.assertEqual(timings["_content_chars"], 10)
        self.assertIn("thinking 90%", crow.format_timings(timings))

    def test_a_reply_without_reasoning_still_works(self):
        """Endpoints that do not split the field must behave exactly as before."""
        text, timings, printed = self._run([{"content": "PLAIN"}])
        self.assertEqual(text, "PLAIN")
        self.assertIn("PLAIN", printed)
        self.assertNotIn("_reasoning_chars", timings)
        self.assertNotIn("thinking", crow.format_timings(timings))

    def test_server_timings_survive(self):
        _, timings, _ = self._run([{"content": "A"}])
        self.assertEqual(timings["predicted_n"], 7)


class RendererTests(unittest.TestCase):
    """Fenced code has to be framed WHILE it streams, not after."""

    def _render(self, text, chunk=1):
        sink = io.StringIO()
        r = crow.Renderer(out=sink)
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

    def test_code_gets_a_frame_naming_the_language(self):
        out = self._render("```python\nx = 1\n```\n")
        self.assertIn("python", out)
        self.assertIn("+-", out)
        self.assertIn("| ", out)

    def test_same_output_regardless_of_chunk_size(self):
        """The reply arrives token by token; a renderer that only works on
        whole lines would break on the real stream."""
        text = "before\n```js\nconst a = 1;\n```\nafter\n"
        one = self._render(text, chunk=1)
        big = self._render(text, chunk=999)
        self.assertEqual(one, big)

    def test_unterminated_fence_is_closed_on_exit(self):
        """An interrupted answer must not leave the frame hanging open."""
        out = self._render("```python\nx = 1\n")
        self.assertIn("x = 1", out)
        self.assertFalse(crow.Renderer(out=io.StringIO()).in_code)
        self.assertGreaterEqual(out.count("+"), 2)

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

    def test_all_frames_have_equal_shape(self):
        """The redraw is a fixed cursor-up; ragged frames would smear."""
        heights = {len(f) for f in crow.RAVEN_FRAMES}
        self.assertEqual(len(heights), 1, "frames differ in line count")
        for frame in crow.RAVEN_FRAMES:
            widths = {len(line) for line in frame}
            self.assertEqual(len(widths), 1, f"ragged frame: {frame}")

    def test_frames_are_ascii(self):
        """cp1252 consoles would mangle anything else."""
        for frame in crow.RAVEN_FRAMES:
            for line in frame:
                line.encode("ascii")  # raises on non-ascii


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

