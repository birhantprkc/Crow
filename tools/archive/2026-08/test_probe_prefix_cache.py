"""Contract suite for probe-prefix-cache.py: the two judgements it makes.

WHY THIS EXISTS
The probe answers #60 by printing numbers, and two of those numbers are not read
off the wire but computed here: the RATIO that decides whether the cache held,
and the OVERLAP that decides whether the model looped. A probe whose arithmetic
is wrong reports a clean result over a broken measurement, and nothing about the
output would look unusual.

The third thing under test is the one difference between the two arms. The whole
experiment reduces to a single field on a single message, so if `assistant_message`
ever stopped putting `reasoning_content` in the history, both arms would measure
the same thing and agree beautifully.

WHAT THIS DOES NOT PROVE
Nothing about the server, the cache, or the model. No prefill figure. It proves
that the probe's own arithmetic distinguishes held from broken, and repeated from
fresh -- the negative cases are in here for exactly that reason.

Usage:  test_probe_prefix_cache.py
Exit 0 = every case behaved as required.
"""

import importlib.util
import math
import os
import sys
import unittest

PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe-prefix-cache.py")


def load():
    """Import probe-prefix-cache.py by path - the hyphen keeps it out of normal imports."""
    spec = importlib.util.spec_from_file_location("probe_prefix_cache", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = load()


class AssistantMessageTests(unittest.TestCase):
    """The single field the two arms differ in."""

    REPLY = {"content": "ANSWER", "reasoning": "THOUGHTS"}

    def test_the_replay_arm_carries_the_reasoning(self):
        message = probe.assistant_message(self.REPLY, True)
        self.assertEqual(message["reasoning_content"], "THOUGHTS")
        self.assertEqual(message["content"], "ANSWER")

    def test_the_drop_arm_carries_no_reasoning_field(self):
        """Not an empty string: the template tests for presence."""
        message = probe.assistant_message(self.REPLY, False)
        self.assertNotIn("reasoning_content", message)

    def test_a_turn_without_thoughts_never_gets_the_field(self):
        """Otherwise the replay arm would send reasoning_content='' and the two
        arms would silently stop differing."""
        message = probe.assistant_message({"content": "A", "reasoning": ""}, True)
        self.assertNotIn("reasoning_content", message)


class RatioTests(unittest.TestCase):
    """Near 0 = the cache held. Near 1 = the whole of turn 1 was read again."""

    def test_a_held_cache_is_near_zero(self):
        self.assertLess(probe.ratio(17, 2046), 0.02)

    def test_a_broken_cache_is_near_one(self):
        self.assertGreater(probe.ratio(2018, 2046), 0.9)

    def test_the_two_verdicts_cannot_be_confused(self):
        """The failure the first version of this probe walked into: with a tiny
        turn 1 both arms land near zero and the negative control cannot go red."""
        held, broken = probe.ratio(17, 2046), probe.ratio(2018, 2046)
        self.assertGreater(broken / max(held, 1e-9), 50)

    def test_no_generation_is_not_a_zero(self):
        """A missing denominator must not read as a perfect cache hit."""
        self.assertTrue(math.isnan(probe.ratio(17, 0)))
        self.assertTrue(math.isnan(probe.ratio(17, None)))


class OverlapTests(unittest.TestCase):
    """The loop check: a repeated answer must not look like a fresh one."""

    LONG = ("Insertion sort builds the final sorted array one element at a time. "
            "It takes each element and inserts it into its correct position among "
            "the already sorted elements to its left, which is why it is quadratic.")

    def test_a_verbatim_repeat_is_near_a_hundred(self):
        self.assertGreater(probe.overlap(self.LONG, self.LONG), 95.0)

    def test_a_different_answer_is_near_zero(self):
        other = ("Hoare partitioning uses two pointers moving toward each other "
                 "from opposite ends of the array, swapping elements that sit on "
                 "the wrong side of the pivot until the pointers cross.")
        self.assertLess(probe.overlap(self.LONG, other), 5.0)

    def test_whitespace_is_not_a_difference(self):
        self.assertGreater(probe.overlap(self.LONG, self.LONG.replace(" ", "\n  ")), 95.0)

    def test_an_empty_predecessor_is_zero_not_a_crash(self):
        self.assertEqual(probe.overlap("", self.LONG), 0.0)


class ToolsTests(unittest.TestCase):
    def test_the_probe_always_sends_tools(self):
        """Without them the template drops a replayed reasoning field and both
        arms render byte for byte the same -- the probe would measure nothing."""
        self.assertTrue(probe.TOOLS)
        self.assertEqual(probe.TOOLS[0]["function"]["name"], "read_file")

    def test_temperature_is_not_greedy(self):
        """0.0 is the attractor the model looped in on 2026-08-07; a probe there
        would measure that instead of the cache."""
        self.assertGreater(probe.TEMPERATURE, 0.0)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
