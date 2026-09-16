#!/usr/bin/env python3
"""Suite for tools/probe-slot-persistence.py. Standard library only, no server.

It tests the two judgements the probe makes -- did the prefix hold, and how big
does the state get -- because those are what the numbers on #68 rest on. The
server is not exercised here; that is what the probe itself does.

Every group carries a case that must go red. The failure this guards against is
specific and was seen for real on 2026-08-08: a restore that returns HTTP 200
and `n_restored = 2517` while the server re-reads the whole context anyway. A
check that cannot tell those apart would have reported the broken run as a
success.

Run:  python tools/test_probe_slot_persistence.py
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

# The probe's filename has hyphens, so it cannot be imported by name.
_SPEC = importlib.util.spec_from_file_location(
    "probe_slot_persistence", Path(__file__).resolve().parent / "probe-slot-persistence.py")
probe = importlib.util.module_from_spec(_SPEC)
sys.modules["probe_slot_persistence"] = probe
_SPEC.loader.exec_module(probe)


class PrefixHeldTests(unittest.TestCase):
    """The verdict has to come from prompt_n, not from the HTTP status."""

    def test_the_measured_working_restore_passes(self):
        """2026-08-08: 2,517 tokens held, 18 re-read after a server restart."""
        self.assertTrue(probe.prefix_held(2517, 18))

    def test_the_measured_broken_restore_fails(self):
        """Same script without reasoning_content: 2,650 held, 2,487 re-read.

        This is the case that reported success at the HTTP layer. If this
        assertion ever flips, the probe has stopped being able to tell a
        restore that works from one that only claims to."""
        self.assertFalse(probe.prefix_held(2650, 2487))

    def test_a_full_re_read_fails(self):
        self.assertFalse(probe.prefix_held(2517, 2517))

    def test_a_new_user_turn_still_counts_as_held(self):
        """prompt_n is never zero -- the turn just typed is always read."""
        self.assertTrue(probe.prefix_held(2517, 60))

    def test_the_boundary_is_not_silently_inclusive(self):
        self.assertFalse(probe.prefix_held(1000, 200))
        self.assertTrue(probe.prefix_held(1000, 199))

    def test_an_empty_context_is_not_a_pass(self):
        """Nothing held cannot mean 'the prefix survived'."""
        self.assertFalse(probe.prefix_held(0, 0))
        self.assertFalse(probe.prefix_held(-1, 0))


class SizeModelTests(unittest.TestCase):
    """One point cannot see a constant, and the constant here is ~17 MiB."""

    # The two runs of 2026-08-08.
    POINTS = [(884, 23_927_612), (2517, 35_187_020)]

    def test_it_recovers_the_per_token_cost(self):
        _, per_token = probe.size_model(self.POINTS)
        self.assertAlmostEqual(per_token, 6895, delta=10)

    def test_it_recovers_the_fixed_part(self):
        fixed, _ = probe.size_model(self.POINTS)
        self.assertAlmostEqual(fixed / 1024 / 1024, 17.0, delta=0.5)

    def test_the_200k_extrapolation_is_about_1_3_gib(self):
        """The figure quoted on #68. A one-point extrapolation said 5 GiB."""
        fixed, per_token = probe.size_model(self.POINTS)
        gib = (fixed + per_token * 200_000) / 1024 / 1024 / 1024
        self.assertAlmostEqual(gib, 1.29, delta=0.05)

    def test_a_single_point_is_refused(self):
        """The mistake this function exists to prevent."""
        with self.assertRaises(ValueError):
            probe.size_model([(884, 23_927_612)])

    def test_two_points_at_the_same_size_are_refused(self):
        with self.assertRaises(ValueError):
            probe.size_model([(884, 23_927_612), (884, 23_927_612)])

    def test_order_does_not_matter(self):
        self.assertEqual(probe.size_model(self.POINTS),
                         probe.size_model(list(reversed(self.POINTS))))

    def test_a_purely_linear_series_reports_no_fixed_part(self):
        """Control: if there were no constant, it must report zero rather than
        inventing one."""
        fixed, per_token = probe.size_model([(100, 700_000), (200, 1_400_000)])
        self.assertAlmostEqual(fixed, 0.0, delta=1.0)
        self.assertAlmostEqual(per_token, 7000, delta=1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
