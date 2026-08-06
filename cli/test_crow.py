#!/usr/bin/env python3
"""Suite for cli/crow.py. Standard library only, same as the CLI itself.

Run:  python cli/test_crow.py

Every group carries at least one case that must FAIL if the behaviour it
guards regresses — a suite that cannot go red proves nothing.
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
        """Positive control for the test above — it caught a real bug."""
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
            "the earlier turns changed — the prompt cache would be lost",
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


class RavenTests(unittest.TestCase):
    def test_silent_when_not_a_terminal(self):
        """Piped output must stay clean — no frames in captured transcripts."""
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
        self.assertEqual(args.temperature, 0.0)

    def test_base_url_override(self):
        args = crow.build_parser().parse_args(["--base-url", "http://x:9/v1", "-m", "other"])
        self.assertEqual(args.base_url, "http://x:9/v1")
        self.assertEqual(args.model, "other")


class EndpointFailureTests(unittest.TestCase):
    def test_dead_port_raises_crow_error(self):
        """Negative control: a port nothing listens on must not look healthy."""
        with self.assertRaises(crow.CrowError):
            crow.check_endpoint("http://127.0.0.1:9/v1", timeout=2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
