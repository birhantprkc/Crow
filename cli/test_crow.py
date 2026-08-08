#!/usr/bin/env python3
"""Suite for cli/crow.py. Standard library only, same as the CLI itself.

Run:  python cli/test_crow.py

Every group carries at least one case that must FAIL if the behaviour it
guards regresses -- a suite that cannot go red proves nothing.
"""

from __future__ import annotations

import io
import json
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

    def _run(self, deltas, conversation=None, **kw):
        """Drive stream_reply against a canned SSE stream.

        Returns (text, reasoning, timings, printed). The body the caller would
        have sent is kept in self.sent_body -- what goes on the wire is part of
        the contract, not an implementation detail.
        """
        chunks = [json.dumps({"choices": [{"delta": d}]}) for d in deltas]
        chunks.append(json.dumps({"choices": [], "timings": {"predicted_n": 7}}))
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
        """Every non-ASCII cell in the wordmark must sit in the block elements,
        which the bundled font covers 32 of 32. A character outside that range
        would fall back to another face and break the alignment."""
        for ch in crow.BANNER:
            if ord(ch) > 127:
                self.assertTrue(0x2580 <= ord(ch) <= 0x259F,
                                f"U+{ord(ch):04X} is outside the block elements")

    def test_bevel_is_painted_apart_from_the_face(self):
        """Positive control for paint_banner: without a separate colour on the
        shade cell the wordmark is flat, and the test would not notice."""
        painted = crow.paint_banner(crow.BANNER)
        if crow._TTY:
            self.assertIn(crow.BANNER_BEVEL, painted)
        self.assertIn(crow.BANNER_SHADE, painted)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

