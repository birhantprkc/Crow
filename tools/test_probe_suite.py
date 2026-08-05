"""Contract suite for probe-suite.py: the verdicts, the denominator and the exit codes.

WHY THIS EXISTS
On 2026-08-05 truncation stopped being a failure and became its own verdict (#46,
Ausweg 2). That change is worth exactly as much as the evidence behind it, and the
claims it makes are claims about EXIT CODES and a SHRINKING DENOMINATOR - the two
things nobody notices when they drift:

  * a run with an undecided task must not be able to exit 0, because a caller would
    read that as all green over an incomplete measurement;
  * the printed denominator must be the number of tasks actually judged, and the
    undecided ones must be named every time. A denominator that shrinks quietly is
    how a gate starts looking better than it is.

Neither is provable by reading the source. This suite drives the REAL runner over a
canned endpoint, then compares the exit code and the printed line against a table.

THE ENDPOINT IS FAKE, THE GATE IS NOT
A local HTTP server answers /v1/chat/completions with responses chosen per task, so
every shape can be produced on demand: a correct answer, a wrong one, one with no code
at all, and a turn cut off by the budget. Everything downstream - selftest, extractor,
the per-task subprocess, the scoring - is the real probe-suite.py.

WHAT THIS DOES NOT PROVE
Nothing about the model. No quality figure for any quantisation, no throughput. It
proves that the harness reports what it claims to report.

Usage:  test_probe_suite.py
Exit 0 = every case behaved as required.
"""

import http.server
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "probe-suite.py")
PROMPT_DIR = os.path.join(HERE, "prompts", "tasks")


def load_gate():
    """Import probe-suite.py by path - the hyphen keeps it out of normal imports."""
    spec = importlib.util.spec_from_file_location("probe_suite", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATEMOD = load_gate()


# ---------------------------------------------------------------- canned answers

def correct_source(task):
    """The reference implementation, renamed to the name the prompt asks for.

    Replacing every occurrence of the _ref_ name rather than only the def line keeps
    recursive references (flatten) intact.
    """
    src = inspect.getsource(GATEMOD.REFERENCES[task["name"]])
    return src.replace("_ref_" + task["func"], task["func"])


def wrong_source(task):
    """Right name, right arity, useless answer: the shape of RUNS BUT WRONG."""
    return "def %s(*args, **kwargs):\n    return None\n" % task["func"]


def answer(kind, task):
    """Build the server's reply for one task. Returns the OpenAI-shaped body."""
    if kind == "truncated":
        # The measured shape: the budget ran out inside the reasoning block, so content
        # is empty even though the turn produced tokens.
        return {"choices": [{"finish_reason": "length",
                             "message": {"content": "",
                                         "reasoning_content": "x" * 3930}}],
                "usage": {"completion_tokens": 4096}}
    if kind == "nocode":
        return {"choices": [{"finish_reason": "stop",
                             "message": {"content": "I would rather not.",
                                         "reasoning_content": ""}}],
                "usage": {"completion_tokens": 12}}
    body = correct_source(task) if kind == "correct" else wrong_source(task)
    return {"choices": [{"finish_reason": "stop",
                         "message": {"content": "```python\n" + body + "```",
                                     "reasoning_content": "thinking"}}],
            "usage": {"completion_tokens": 200}}


class Endpoint:
    """A local /v1/chat/completions that answers by task, chosen per scenario."""

    def __init__(self, plan, default="correct"):
        # A plan value is either one kind for every call, or a list consumed per attempt -
        # that second form is what lets a task be cut off first and answered on the rerun.
        self.plan = plan              # {task-name: kind or [kind, kind, ...]}
        self.default = default
        self.calls = {}
        self.lock = threading.Lock()
        self.budgets = []             # every max_tokens the gate asked for, in order
        self.by_prompt = {}
        for task in GATEMOD.TASKS:
            path = os.path.join(PROMPT_DIR, task["prompt"])
            with open(path, "r", encoding="utf-8") as fh:
                self.by_prompt[fh.read()] = task
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                prompt = payload["messages"][0]["content"]
                task = outer.by_prompt.get(prompt)
                if task is None:
                    self.send_error(404, "unknown prompt")
                    return
                kind = outer.plan.get(task["name"], outer.default)
                with outer.lock:
                    outer.budgets.append(payload.get("max_tokens"))
                    if isinstance(kind, (list, tuple)):
                        n = outer.calls.get(task["name"], 0)
                        outer.calls[task["name"]] = n + 1
                        kind = kind[min(n, len(kind) - 1)]
                data = json.dumps(answer(kind, task)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass                  # the suite's output is the only output

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------- driving the gate

def run_gate(args):
    p = subprocess.run([sys.executable, GATE] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def do_run(out_dir, plan, max_tokens=4096):
    """Drive the real runner over a canned endpoint. Returns (exit, output, budgets)."""
    with Endpoint(plan) as ep:
        code, out = run_gate(["run", "--url", "http://127.0.0.1:%d" % ep.port,
                              "--out", out_dir, "--timeout", "30",
                              "--max-tokens", str(max_tokens)])
        return code, out, list(ep.budgets)


# ---------------------------------------------------------------- the table

def report(label, code, out, want_exit, want_text, want_absent=()):
    ok_code = code == want_exit
    missing = [t for t in want_text if t.lower() not in out.lower()]
    present = [t for t in want_absent if t.lower() in out.lower()]
    if ok_code and not missing and not present:
        print("  OK       %-38s exit %d" % (label, code))
        return 0
    print("  FAILED   %-38s exit %d (wanted %d)" % (label, code, want_exit))
    for t in missing:
        print("           missing from output: %r" % t)
    for t in present:
        print("           must NOT be in output but is: %r" % t)
    print("           " + out.strip().replace("\n", "\n           ")[-900:])
    return 1


def main():
    if not os.path.isfile(GATE):
        print("SETUP ERROR: probe-suite.py not found next to this suite")
        return 2

    fail = 0
    tmp = tempfile.mkdtemp(prefix="probe-contract-")
    try:
        # --- the pure rule, without a server ------------------------------------
        # The trigger must fire on the measured shape and on nothing else. The third
        # case is the one that matters: an empty answer that finished normally is a
        # real model failure, and calling it undecided would hide it.
        rule = [
            ("length + empty content -> undecided", ("length", "", "x" * 100), True),
            ("length + real content  -> judged",    ("length", "def f(): pass", ""), False),
            ("stop + empty content   -> judged",    ("stop", "", "x" * 100), False),
        ]
        for label, args, want_reason in rule:
            got = GATEMOD.truncation_reason(*args)
            if (got is not None) == want_reason:
                print("  OK       %-38s %s" % (label, "reason" if got else "None"))
            else:
                fail = 1
                print("  FAILED   %-38s got %r" % (label, got))

        # --- the run contract ---------------------------------------------------
        all_ok = os.path.join(tmp, "all-ok")
        code, out, budgets = do_run(all_ok, {})
        # "UNDECIDED:" with the colon is the listing line for an undecided task. Matching
        # the bare word would also hit the "0 undecided" of a clean summary.
        fail |= report("all ten correct", code, out, 0,
                       ["10 of 10 judged correct, 0 undecided"],
                       want_absent=["UNDECIDED:", "NO TASK WAS JUDGED",
                                    "asking again", "rerun:"])
        # Negative control on the rerun itself: ten answers, ten requests. A harness that
        # asked twice for turns that were never cut off would inflate every run's cost.
        if budgets != [4096] * 10:
            fail = 1
            print("  FAILED   %-38s asked %d times: %r"
                  % ("no rerun when nothing was cut off", len(budgets), budgets))
        else:
            print("  OK       %-38s 10 requests, all at 4096" % "no rerun without truncation")

        # THE CASE THE DESIGN WAS CHOSEN FOR: cut off first, answered on the rerun. The
        # denominator stays at ten and the task gets a real verdict.
        rerun_ok = os.path.join(tmp, "rerun-ok")
        code, out, budgets = do_run(rerun_ok, {"two-sum": ["truncated", "correct"]})
        fail |= report("truncated once, answered on rerun", code, out, 0,
                       ["asking again at 8192",
                        "[rerun at 8192 tokens]",
                        "10 of 10 judged correct, 0 undecided (of 10 tasks)",
                        "rerun:", "two-sum"],
                       want_absent=["UNDECIDED:", "9 of 9"])
        # Eleven requests: ten at the reference budget plus ONE at twice that. The rerun
        # sits where the cut-off task sits (two-sum is second), not at the end, so this
        # counts rather than indexes.
        if (len(budgets), budgets.count(4096), budgets.count(8192)) != (11, 10, 1):
            fail = 1
            print("  FAILED   %-38s budgets asked: %r" % ("rerun doubles the budget",
                                                          budgets))
        else:
            print("  OK       %-38s 11 requests, exactly one at 8192"
                  % "rerun doubles the budget")
        if not os.path.isfile(os.path.join(rerun_ok, "two-sum-rerun.json")):
            fail = 1
            print("  FAILED   %-38s no two-sum-rerun.json" % "both transcripts kept")
        else:
            print("  OK       %-38s truncated turn kept beside the rerun"
                  % "both transcripts kept")

        one_cut = os.path.join(tmp, "one-cut")
        code, out, _ = do_run(one_cut, {"two-sum": "truncated"})
        fail |= report("truncated twice, stays undecided", code, out, 4,
                       ["9 of 9 judged correct, 1 undecided (of 10 tasks)",
                        "UNDECIDED", "two-sum", "also at 8192 tokens",
                        "Still undecided after the rerun"],
                       # A cut-off turn is not a failed one, and the score must not still
                       # read as ten judged tasks.
                       want_absent=["10 of 10 judged", "DOES NOT RUN"])

        one_wrong = os.path.join(tmp, "one-wrong")
        code, out, _ = do_run(one_wrong, {"two-sum": "wrong"})
        fail |= report("one wrong, rest correct", code, out, 1,
                       ["9 of 10 judged correct, 0 undecided", "RUNS BUT WRONG"],
                       want_absent=["asking again"])

        mixed = os.path.join(tmp, "mixed")
        code, out, _ = do_run(mixed, {"two-sum": "wrong", "flatten": "truncated"})
        fail |= report("one wrong AND one truncated", code, out, 1,
                       ["8 of 9 judged correct, 1 undecided", "RUNS BUT WRONG",
                        "UNDECIDED"])

        no_code = os.path.join(tmp, "no-code")
        code, out, _ = do_run(no_code, {"two-sum": "nocode"})
        # An answer that finished normally and simply carries no code is a MODEL failure,
        # not a budget one. It must stay red and must NOT be asked again; if it drifted into
        # "undecided" the gate would have grown a way to make real failures disappear.
        fail |= report("one answer without code", code, out, 1,
                       ["9 of 10 judged correct, 0 undecided", "DOES NOT RUN"],
                       want_absent=["UNDECIDED:", "asking again"])

        all_cut = os.path.join(tmp, "all-cut")
        code, out, _ = do_run(all_cut, {t["name"]: "truncated" for t in GATEMOD.TASKS})
        fail |= report("all ten truncated, twice", code, out, 4,
                       ["NO TASK WAS JUDGED"],
                       # A zero without a denominator is not a statement.
                       want_absent=["0 of 0"])

        # --- the comparison contract --------------------------------------------
        code, out = run_gate(["compare", all_ok, one_cut])
        # one_cut is the run whose two-sum stayed undecided even after the rerun.
        fail |= report("compare: undecided is not a flip", code, out, 0,
                       ["9 of 9 verdicts unchanged",
                        "Outside the statement: 1 of 10", "two-sum"],
                       want_absent=["NOT verdict-stable"])

        code, out = run_gate(["compare", all_ok, all_cut])
        fail |= report("compare: nothing left to compare", code, out, 2,
                       ["NOTHING COMPARABLE"],
                       want_absent=["verdicts unchanged"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("RESULT: PASS - verdicts, denominator and exit codes are what the gate claims"
          if not fail else
          "RESULT: FAIL - the gate does not report what it claims; see FAILED lines")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
