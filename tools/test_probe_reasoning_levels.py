#!/usr/bin/env python3
"""Negative control for probe_reasoning_levels.py.

A checker that cannot go red is a decoration, and #159 paid for that lesson twice:
two checker suites had been unable to fail for weeks, and three regressions had
stacked up behind them. So every case here breaks exactly ONE thing about what a
server answers and requires the tool to name it -- plus the cases that must NOT
fire, because a checker that is red at harmless facts gets ignored, and an ignored
checker is worse than none.

WHY A FAKE SERVER AND NOT A FAKE FUNCTION. The tool's whole point is that one side
of its comparison comes off the wire. Driving `audit()` directly would leave the
HTTP handling, the /props lookup and the model resolution untested -- and the model
resolution is what decides which levels are even claimed. So these cases run the
REAL tool as a subprocess against a stdlib HTTP server that answers /props and
/apply-template from a table. No llama-server, no GPU, no network.

CASE 2 IS #160 ITSELF: a level the menu offers returns HTTP 500. If this suite
cannot make the tool red here, the tool would have passed on the very defect it
was written for.

CASE 7 IS THE ONE THAT PROVES THE TOOL WOULD HAVE CAUGHT IT: a model with no
manifest entry, so the union fallback applies and `max` is offered -- against a
template that refuses `max`. That is the exact state flash-next shipped in until
2026-08-30, and the tool has to go red on it without knowing anything about that
model.

Usage:  test_probe_reasoning_levels.py
Exit 0 = every case behaved as required.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(HERE, "probe_reasoning_levels.py")
MANIFEST = os.path.join(REPO, "manifests", "operating-point.json")

# THE MODEL PATH IS READ, NEVER TYPED. A hard-coded name here would keep passing
# after the manifest renamed the entry, and the case would go quietly vacuous --
# the failure the neighbouring suite records for a hard-coded version literal.
with open(MANIFEST, encoding="utf-8-sig") as _fh:
    _ENTRIES = (json.load(_fh).get("models") or {}).get("entries") or {}
FLASH_PATH = _ENTRIES["flash-next-q2-k-xl"]["path"]

# What a healthy flash-next answers: three distinct renderings, the unset case on
# top of `high`, and everything else refused. Values are the rendered prompt; the
# tool hashes them itself, so any two distinct strings make two distinct groups.
# #176: `none` GEHOERT DAZU, SEIT DAS MANIFEST ES ANBIETET. Der gesunde Server
# ist der, der genau die Stufen rendert, die der Eintrag nennt -- fehlt eine, ist
# nicht die Sonde falsch, sondern diese Tabelle veraltet. Eigener Text, weil
# `none` gemessen als einzige Stufe mit nichts sonst Bytes teilt (101 Zeichen
# gegen high 299, low 228, medium 90).
HEALTHY = {None: "HIGH-TEXT", "high": "HIGH-TEXT", "low": "LOW-TEXT",
           "medium": "MEDIUM-TEXT", "none": "NONE-TEXT"}


class _Handler(BaseHTTPRequestHandler):
    table: dict = {}
    model_path: str = ""

    def log_message(self, *a):                                        # noqa: D102
        pass                       # the suite's output is the report, not the server's

    def do_GET(self):
        if self.path != "/props":
            self.send_error(404)
            return
        self._json({"model_path": self.model_path})

    def do_POST(self):
        if self.path != "/apply-template":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)))
        # #176: DIESELBE TUER WIE DER ECHTE SERVER. Er liest das OBERSTE Feld
        # (`tools/server/server-common.cpp:1323`) und faengt dort `none` ab; die
        # kwargs gehen an jinja vorbei. Solange dieses Double die kwargs las,
        # ahmte es einen Server nach, den es nicht gibt -- und deckte damit genau
        # den Fehler, den die Sonde finden soll.
        level = body.get("reasoning_effort")
        if level not in self.table:
            self.send_error(500)
            return
        self._json({"prompt": self.table[level]})

    def _json(self, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ProbeCase(unittest.TestCase):
    """One fake server per case, on a port the OS picks."""

    def run_tool(self, table, model_path=FLASH_PATH, extra=()):
        handler = type("H", (_Handler,), {"table": dict(table),
                                          "model_path": model_path})
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ)
            # NOTHING THIS SUITE RUNS MAY TOUCH THE INSTALLATION. crow_core reads
            # %LOCALAPPDATA%\Crow at import; a fresh empty directory is the state
            # its readers treat as "no configuration".
            env["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="crow-probe-suite-")
            out = subprocess.run(
                [sys.executable, TOOL, "--base",
                 "http://127.0.0.1:%d" % server.server_port] + list(extra),
                capture_output=True, text=True, env=env, timeout=120)
        finally:
            server.shutdown()
            server.server_close()
        return out.returncode, out.stdout + out.stderr

    # -- the green case, without which every red one below proves nothing -------

    def test_1_a_healthy_server_agrees_with_the_menu(self):
        code, said = self.run_tool(HEALTHY)
        self.assertEqual(code, 0, said)
        self.assertIn("agree", said)

    # -- #160 itself -----------------------------------------------------------

    def test_2_an_offered_level_that_500s_is_named(self):
        """The defect this tool exists for. Red, and it says WHICH level."""
        table = dict(HEALTHY)
        del table["medium"]
        code, said = self.run_tool(table)
        self.assertEqual(code, 1, said)
        self.assertIn("DISAGREE", said)
        self.assertIn("medium", said)
        self.assertIn("OFFERED and refused", said)

    def test_3_the_unset_case_that_500s_is_named(self):
        """The state every chat starts in. A menu is worthless if it fails."""
        table = dict(HEALTHY)
        del table[None]
        code, said = self.run_tool(table)
        self.assertEqual(code, 1, said)
        self.assertIn("UNSET", said)

    # -- the grouping, both directions -----------------------------------------

    def test_4_a_group_whose_members_render_differently_goes_red(self):
        """`off` and `high` share a group. If the server stops agreeing, the window
        is promising a free switch that costs a full prefill."""
        table = dict(HEALTHY)
        table[None] = "SOMETHING-ELSE"
        code, said = self.run_tool(table)
        self.assertEqual(code, 1, said)
        self.assertIn("render differently", said)

    def test_5_two_groups_that_render_identically_go_red(self):
        """The other direction: the window bills a prefill for a switch that moves
        no byte. Without this case the tool would only ever catch one half."""
        table = dict(HEALTHY)
        table["medium"] = table["low"]
        code, said = self.run_tool(table)
        self.assertEqual(code, 1, said)
        self.assertIn("identically", said)

    # -- what must NOT go red --------------------------------------------------

    def test_6_a_dropped_level_that_renders_is_a_note_not_a_failure(self):
        """`xhigh` renders on the real model and is deliberately not offered --
        it is `high` under another name. A tool that failed here would be red on
        the shipped operating point every single run, and would be ignored."""
        table = dict(HEALTHY)
        table["xhigh"] = table["high"]
        code, said = self.run_tool(table)
        self.assertEqual(code, 0, said)
        self.assertIn("NOTE", said)
        self.assertIn("xhigh", said)

    # -- the state flash-next actually shipped in ------------------------------

    def test_7_the_union_fallback_over_a_fatal_level_goes_red(self):
        """A model with no manifest entry gets the union, which contains `max`.
        Against a template that refuses `max` that is exactly the #160 defect, and
        the tool has to find it knowing nothing about the model."""
        code, said = self.run_tool(HEALTHY, model_path="Nobody-Ever-Measured.gguf")
        self.assertEqual(code, 1, said)
        self.assertIn("max", said)
        self.assertIn("union fallback", said)
        self.assertIn("NO MANIFEST ENTRY", said)

    def test_8_no_server_is_a_setup_error_not_a_verdict(self):
        """Exit 2, because "nothing answered" is not "they disagree". A tool that
        returned 1 here would look like a finding in any script that reads codes."""
        out = subprocess.run([sys.executable, TOOL, "--base", "http://127.0.0.1:1"],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("SETUP", out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
