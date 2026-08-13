#!/usr/bin/env python3
"""Negative control for run_server_block.py -- and it costs nothing to run.

WHY A SERVERLESS SUITE FOR A TOOL WHOSE WHOLE POINT IS SERVER TIME. Exactly
because of that: the block runs ONCE, with robin's consent, and every mistake in
it is paid for in server time nobody gets back. So the two things that decide
whether that one run is worth anything are settled here, for free:

  * THE GATE CAN GO RED, at each of its four points and at the right one. A gate
    that is green whatever it is shown would let the block measure two servers,
    a hand-started server, or a rung the release does not ship -- and the numbers
    would look exactly the same as good ones.
  * EVERY PREDICATE IS A FUNCTION, and every function is driven here against the
    values that decide it. F4's band has three outcomes and two edges; the
    timings question has two shapes and a third that is neither; the round trip
    has a line that is not there, which is not the same as a zero.

AND THE ONE CASE THAT IS ABOUT SPENDING: a run without --consent must not touch
the network. It is asserted rather than believed -- `urllib.request.urlopen` is
replaced with something that raises, and the run has to finish anyway.

Usage:  test_run_server_block.py
Exit 0 = every case behaved as required.  1 = at least one did not.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "cli"))

import crow_core                     # noqa: E402
import check_operating_point as point  # noqa: E402
import run_server_block as tool      # noqa: E402

MANIFEST = json.loads(point.read(os.path.join(REPO, "manifests",
                                              "operating-point.json")))


def shipped_line(**override) -> str:
    """A command line built OUT OF the manifest, not typed beside it.

    Typed out by hand it would be a second copy of the operating point in a
    third place, and the day the manifest moves this fixture would keep the
    gate green against a line nobody ships any more. Built from the manifest it
    can only ever drift the way the product drifts.
    """
    server = dict(MANIFEST["server"])
    server.update(override)
    return (
        "\"C:\\Users\\robin\\AppData\\Local\\Crow\\bin\\llama-server.exe\""
        " -m C:\\Users\\robin\\AppData\\Local\\Crow\\models\\%s\\%s"
        " --port %d -c %d -ngl %d -np %d --jinja"
        " --slot-save-path C:\\Users\\robin\\AppData\\Local\\Crow\\session"
        " --moe-stream --moe-stream-cache %s --moe-stream-io-threads %d"
        " --moe-stream-direct --moe-stream-l2 32"
        " --chat-template-file C:\\Users\\robin\\AppData\\Local\\Crow"
        "\\templates\\0731-chat-template.jinja"
        % (MANIFEST["model"]["quant"], MANIFEST["model"]["first_shard"],
           server["port"], server["ctx"], server["ngl"], server["parallel"],
           server["moe_stream_cache"], server["moe_stream_io_threads"]))


def listing(*command_lines: str) -> str:
    """A process list with the given llama-servers on it."""
    return "".join("%d\t%s\n" % (4000 + i, line)
                   for i, line in enumerate(command_lines))


def shipped_props() -> dict:
    model = MANIFEST["model"]
    return {"model_path": "C:\\Users\\robin\\AppData\\Local\\Crow\\models\\%s\\%s"
                          % (model["quant"], model["first_shard"]),
            "default_generation_settings": {"n_ctx": MANIFEST["server"]["ctx"]}}


def run_main(argv, machine: str, tmp: str):
    """tool.main() over a machine of our choosing, with the network cut.

    THE NETWORK IS CUT FOR EVERY CASE IN HERE, not only for the consent one. No
    case below is entitled to a server, so a run that reached for one is a
    failure whatever else it printed -- and it would be one that only shows up
    on a machine where a server happens to be up.
    """
    keep_query, keep_open = tool._run_process_query, urllib.request.urlopen

    def boom(*_args, **_kwargs):
        raise AssertionError("this run touched the network")

    tool._run_process_query = lambda: machine
    urllib.request.urlopen = boom
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = tool.main(["run_server_block.py",
                              "--run-dir", os.path.join(tmp, "run"),
                              "--session-dir", os.path.join(tmp, "session"),
                              *argv])
    except AssertionError as exc:
        return -1, buf.getvalue() + "\nRAISED: %s" % exc
    finally:
        tool._run_process_query = keep_query
        urllib.request.urlopen = keep_open
    return code, buf.getvalue()


# The stream every probe is walked against below: a thought, an answer, and a
# code fence -- the three things the live pass reads. Same shape as the fixtures
# in cli/test_crow_gui.py, on purpose: a second invented shape would be a second
# opinion about what the wire looks like.
RECORDED_DELTAS = [
    {"reasoning_content": "erst denken"},
    {"content": "Hier ist der Block:\n"},
    {"content": "```python\n"},
    {"content": "print(\"hi\")\n"},
    {"content": "```\n"},
]
RECORDED_TIMINGS = {"predicted_n": 5, "predicted_ms": 260.0,
                    "predicted_per_second": 19.13, "prompt_n": 12,
                    "prompt_ms": 106.0, "prompt_per_second": 112.69}


def recorded() -> list[str]:
    out = [json.dumps({"choices": [{"delta": delta}]}) for delta in RECORDED_DELTAS]
    out.append(json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                           "usage": {"total_tokens": 41,
                                     "prompt_tokens_details": {"cached_tokens": 29}},
                           "timings": RECORDED_TIMINGS}))
    return out


RECORDED = recorded()


@contextlib.contextmanager
def scripted(payloads):
    """The endpoint, replaced by a list. The REAL stream loop runs behind it."""
    keep = crow_core._post_stream

    def fake(_url, _body, _api_key, _timeout):
        for payload in payloads:
            yield payload

    crow_core._post_stream = fake
    try:
        yield
    finally:
        crow_core._post_stream = keep


def fixture_block(tmp: str):
    """A Block pointed at nothing, with the session directory moved aside.

    Returns (block, restore). The session globals are module state of
    crow_core -- the same two the tool's --session-dir writes -- so they are put
    back by hand rather than left pointing into a temporary directory.
    """
    session = os.path.join(tmp, "walk-session")
    os.makedirs(session, exist_ok=True)
    keep_dir, keep_file = crow_core.SESSION_DIR, crow_core.SESSION_FILE
    crow_core.SESSION_DIR = session
    crow_core.SESSION_FILE = os.path.join(session, "session.json")
    args = tool.build_parser().parse_args([
        "--base-url", "http://127.0.0.1:1/v1",
        "--run-dir", os.path.join(tmp, "walk"),
        "--turn-limit", "20", "--grace", "1", "--timeout", "5"])

    def restore():
        crow_core.SESSION_DIR, crow_core.SESSION_FILE = keep_dir, keep_file
        crow_core.INTERRUPT.clear()

    return tool.Block(args), restore


# The two shapes E12 wrote a fixture for, on the wire.
def stream(every: bool, predicted_n: int) -> list[str]:
    timings = {"predicted_n": predicted_n, "prompt_n": 528,
               "predicted_per_second": 19.13}
    out = []
    for i in range(predicted_n):
        chunk = {"choices": [{"delta": {"content": "x"}}]}
        if every:
            chunk["timings"] = timings
        out.append(json.dumps(chunk))
    out.append(json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                           "timings": timings}))
    return out


def main() -> int:
    fail = 0
    skipped: list[int] = []

    def case(num, what, ok, detail=""):
        nonlocal fail
        if ok:
            print("  OK       case %-2d %s" % (num, what))
        else:
            fail += 1
            print("  FAILED   case %-2d %s" % (num, what))
            for line in str(detail).strip().splitlines():
                print("             %s" % line)

    tmp = tempfile.mkdtemp(prefix="crow-e14-")
    try:
        # -- the gate --------------------------------------------------------

        # 1 -- the positive. Without it every red case below would also pass on
        # a gate that is red at everything.
        code, out = run_main(["--dry-run"], listing(shipped_line()), tmp)
        case(1, "one shipped server: the gate is green and nothing was sent",
             code == 0 and "OK       command line" in out
             and "RESULT: dry run" in out, out)

        # 2 -- the money case. No consent, no bytes, and no run directory.
        code, out = run_main([], listing(shipped_line()), tmp)
        case(2, "without --consent nothing is measured and nothing is written",
             code == 2 and "needs" in out and "consent" in out
             and not os.path.exists(os.path.join(tmp, "run")), out)

        # 3 -- the plan's own precondition, first half.
        code, out = run_main(["--consent"], listing(), tmp)
        case(3, "no llama-server: red before anything is sent",
             code == 2 and "no llama-server is running" in out
             and "Nothing was sent" in out, out)

        # 4 -- and its second half, the one the plan says no counter reveals.
        code, out = run_main(["--consent"],
                             listing(shipped_line(), shipped_line()), tmp)
        case(4, "two llama-servers: the block would measure the pair",
             code == 2 and "measure the PAIR" in out, out)

        # 5 -- stronger than the plan asks and free: a server that is RUNNING but
        # is not the shipped point. One process, right name, wrong slot count.
        code, out = run_main(["--consent"], listing(shipped_line(parallel=4)), tmp)
        case(5, "a hand-started server is named by the flag that differs",
             code == 2 and "parallel: 4" in out and "manifest says 1" in out, out)

        # 6 -- the case Windows produces on its own: an elevated server whose
        # command line this user cannot read. Silence there would be a green gate.
        code, out = run_main(["--consent"], "9999\t\n", tmp)
        case(6, "a command line that cannot be read is red, not skipped",
             code == 2 and ("came back empty" in out
                            or "no llama-server is running" in out), out)

        # 7 -- ORDER IS NOT THE CALLER'S.
        ordered = tool.select_probes("v,iii")
        default = tool.select_probes("")
        try:
            tool.select_probes("nope")
            raised = ""
        except tool.SetupError as exc:
            raised = str(exc)
        case(7, "--only picks which probes run, never in what order",
             ordered == ["iii", "v"] and default == list(tool.PROBE_ORDER)
             and "no such probe" in raised,
             "ordered=%r default=%r raised=%r" % (ordered, default, raised))

        # 8 -- /props: the rung, the name and the window.
        green = tool.props_gate(shipped_props(), MANIFEST)
        iq3 = shipped_props()
        iq3["model_path"] = iq3["model_path"].replace("UD-IQ2_XXS", "UD-IQ3_XXS")
        wrong_rung = tool.props_gate(iq3, MANIFEST)
        short = shipped_props()
        short["default_generation_settings"] = {"n_ctx": 65536}
        wrong_ctx = tool.props_gate(short, MANIFEST)
        silent = tool.props_gate({}, MANIFEST)
        case(8, "/props: the shipped rung is green, another rung is not",
             not green and any("UD-IQ2_XXS" in p for p in wrong_rung)
             and any("n_ctx is 65536" in p for p in wrong_ctx)
             and any("names no model" in p for p in silent),
             "green=%r rung=%r ctx=%r silent=%r"
             % (green, wrong_rung, wrong_ctx, silent))

        # -- the verdicts ----------------------------------------------------

        # 9 -- probe (iii), the question the whole stage is ordered around.
        last_raw, last_parsed = tool.count_timings(stream(False, 252))
        every_raw, every_parsed = tool.count_timings(stream(True, 252))
        last = tool.timings_shape(last_parsed, 252)
        every = tool.timings_shape(every_parsed, 252)
        middle = tool.timings_shape(7, 252)
        nothing = tool.timings_shape(1, None)
        case(9, "(iii) both shapes are recognised and a third is not rounded off",
             last[0] == "final chunk only" and every[0] == "every chunk"
             and middle[0] == "undecided" and nothing[0] == "undecided"
             and (last_raw, last_parsed) == (1, 1)
             and (every_raw, every_parsed) == (253, 253),
             "last=%r every=%r middle=%r none=%r" % (last, every, middle, nothing))

        # 10 -- and the reason the tool counts twice: `grep -c '"timings"'` counts
        # the FIELD and the question is about the BLOCK. An endpoint that knows
        # the field and has nothing to say sends the first without the second.
        talky = stream(False, 3)
        talky[0] = json.dumps({"choices": [{"delta": {"content": "x"}}],
                               "timings": None})
        raw, parsed = tool.count_timings(talky)
        case(10, "(iii) a grep count and a parsed count disagree where they must",
             raw == 2 and parsed == 1, "raw=%d parsed=%d" % (raw, parsed))

        # 11 -- F4's band, at both edges. The first draft of the plan left a
        # region in which the case neither passed nor failed; this is that region
        # named, and it must not decide anything.
        grip = tool.abort_verdict(12.0)
        stuck = tool.abort_verdict(75.0)
        between = tool.abort_verdict(45.0)
        edge_low = tool.abort_verdict(tool.ABORT_GRIP_S)
        edge_high = tool.abort_verdict(tool.ABORT_STUCK_S)
        case(11, "(i) under 30 s is a grip, over 60 s is the old turn, between is void",
             grip[0] == tool.ANSWERED and stuck[0] == tool.OPEN
             and between[0] == tool.INVALID and edge_low[0] == tool.INVALID
             and edge_high[0] == tool.INVALID,
             "%r %r %r %r %r" % (grip, stuck, between, edge_low, edge_high))

        # 12 -- the race, including the floor the plan calls a floor.
        short_run = tool.race_verdict(tool.RACE_FLOOR - 1, 5, 0)
        never = tool.race_verdict(tool.RACE_FLOOR, 0, 0)
        leaked = tool.race_verdict(tool.RACE_FLOOR, 5, 1)
        held = tool.race_verdict(tool.RACE_FLOOR, 1, 0)
        case(12, "(i) 49 repetitions is not 50, and one leaked reader is P1",
             short_run[0] == tool.OPEN and never[0] == tool.INVALID
             and leaked[0] == tool.OPEN and held[0] == tool.ANSWERED,
             "%r %r %r %r" % (short_run, never, leaked, held))

        # 13 -- the round trip's one line, through the colour it is printed in.
        coloured = "\x1b[2mresumed: 4 messages, cache warm\x1b[0m\n"
        archive = "resumed from rollover-20260812.json: 12 messages, messages only"
        case(13, "(ii) `resumed: N` is read through ANSI, and absent is not zero",
             tool.resumed_count(coloured) == 4
             and tool.resumed_count(archive) == 12
             and tool.resumed_count("crow: no endpoint at ...") is None,
             "%r %r %r" % (tool.resumed_count(coloured), tool.resumed_count(archive),
                           tool.resumed_count("crow: no endpoint at ...")))

        # 14 -- the prefix classification, and the honest third answer.
        held_prefix = tool.prefix_verdict([528, 18, 540, 22], 11507)
        re_read = tool.prefix_verdict([528, 18, 540, 9000], 11507)
        undecided = tool.prefix_verdict([528, 18, 540, 2000], 11507)
        partial = tool.prefix_verdict([528, 18], 11507)
        case(14, "(iv) held, re-read, and 'the numbers are the answer' in between",
             held_prefix[0] == "held" and re_read[0] == "re-read"
             and undecided[0] == "undecided" and partial[0] == "undecided",
             "%r %r %r %r" % (held_prefix, re_read, undecided, partial))

        # -- the return path -------------------------------------------------

        # 15 -- the stage's own way back, driven the way a probe would break it.
        session = os.path.join(tmp, "live-session")
        os.makedirs(session, exist_ok=True)
        with open(os.path.join(session, "session.json"), "w", encoding="utf-8") as fh:
            fh.write('{"messages": ["before"]}')
        before = open(os.path.join(session, "session.json"), "rb").read()
        backup = tool.SessionBackup(session, os.path.join(tmp, "backup"))
        backup.take()
        with open(os.path.join(session, "session.json"), "w", encoding="utf-8") as fh:
            fh.write('{"messages": ["the block scribbled here"]}')
        with open(os.path.join(session, crow_core.SLOT_FILE), "wb") as fh:
            fh.write(b"a KV state that was not there before")
        notes = backup.put_back()
        after = open(os.path.join(session, "session.json"), "rb").read()
        case(15, "the session is put back byte for byte, and a new slot file goes",
             after == before
             and not os.path.exists(os.path.join(session, crow_core.SLOT_FILE)),
             "\n".join(notes))

        # -- the predicates the live pass reads ------------------------------

        # 16 -- F7: "from the response's timings field" is a claim about WHERE
        # the number came from, so the predicate compares it to the stream.
        line = "1 round | 1,252 tok @ 19.13 tok/s | prefill 528 | waited 1m5s"
        case(16, "(v) F7's cost line is held against the stream's predicted_n",
             tool._cost_matches(line, 1252)
             and not tool._cost_matches(line, 1253)
             and not tool._cost_matches(line, 0), line)

        # 17 -- what the live F1/F2 predicates are read out of.
        content, reasoning, predicted_n, prompt_n = tool._from_stream([
            json.dumps({"choices": [{"delta": {"reasoning_content": "denk"}}]}),
            json.dumps({"choices": [{"delta": {"content": "ANT"}}]}),
            json.dumps({"choices": [{"delta": {"content": "WORT"}}]}),
            json.dumps({"choices": [{"delta": {}}],
                        "timings": {"predicted_n": 9, "prompt_n": 3}}),
        ])
        case(17, "(v) the recorded stream yields content, thoughts and both counts",
             content == "ANTWORT" and reasoning == 4 and predicted_n == 9
             and prompt_n == 3,
             "%r %d %d %d" % (content, reasoning, predicted_n, prompt_n))

        # 18 -- probe (iv)'s fixture is a real client switch, not a relabelled
        # one: what breaks a prefix IS the fingerprint, so the two clients have
        # to differ in it or the probe measures nothing.
        first = tool.as_client(crow_core.DEFAULT_SYSTEM,
                               [{"role": "user", "content": "eins"}])
        second = tool.as_client(tool.OTHER_SYSTEM, first.payload())
        same = crow_core.prefix_fingerprint(crow_core.DEFAULT_SYSTEM)
        other = crow_core.prefix_fingerprint(tool.OTHER_SYSTEM)
        case(18, "(iv) the second client differs where the prefix is decided",
             same != other
             and [m["content"] for m in second.payload() if m["role"] == "user"]
             == ["eins"]
             and second.payload()[0]["content"] == tool.OTHER_SYSTEM,
             "%s vs %s" % (same, other))

        # 19 -- idempotence of the half that costs nothing.
        code_a, out_a = run_main(["--dry-run"], listing(shipped_line()), tmp)
        code_b, out_b = run_main(["--dry-run"], listing(shipped_line()), tmp)
        case(19, "two dry runs produce the same bytes",
             code_a == code_b == 0 and out_a == out_b, "the two runs differ")

        # -- the half-state this stage cannot afford -------------------------
        #
        # THE WORST FAILURE THIS TOOL HAS IS NOT A WRONG NUMBER, it is a
        # NameError in probe (v) after probes (iii), (iv), (i) and (ii) have been
        # paid for. So every probe is walked end to end against a scripted stream
        # first: no server, no model, no network, and every line of every probe
        # body executed.
        block, restore = fixture_block(tmp)
        try:
            with scripted(RECORDED):
                third = tool.probe_iii(block)
                fourth = tool.probe_iv(block)
        finally:
            restore()
        case(20, "(iii) and (iv) walk end to end against a scripted stream",
             third.verdict == tool.ANSWERED and fourth.verdict in
             (tool.ANSWERED, tool.OPEN) and third.run and fourth.run
             and third.numbers.get("parsed") == 1,
             "iii=%r %r / iv=%r %r" % (third.verdict, third.headline,
                                       fourth.verdict, fourth.headline))

        # 21 -- and the report, which is written AFTER all five have been paid
        # for: a crash here loses the run and keeps the cost.
        block.props = shipped_props()
        block.pid = "4000"
        text = tool.report(block, [third, fourth,
                                   tool.Finding("i", tool.PROBE_ONELINE["i"]),
                                   tool.Finding("ii", tool.PROBE_ONELINE["ii"]),
                                   tool.Finding("v", tool.PROBE_ONELINE["v"])],
                           ["session.json restored, 1.0 KiB"])
        case(21, "the report renders answered and open probes and the way back",
             text.count("| **(") == 5 and "OPEN" in text
             and "The return path:" in text and "#55 and #88" in text,
             text)

        # 22 -- the three probes that drive the client, walked the same way.
        # THEY NO LONGER NEED A DISPLAY: E12 made the window a webview, and what
        # these probes drive is `Api` -- plain Python that runs anywhere. The
        # skip is kept for the one thing that can still stop them, a crow_gui
        # that does not import, because a walk that cannot start must not read
        # as a walk that passed.
        toolkit = tool.toolkit_problem()
        if toolkit:
            print("  SKIPPED  case 22 the client path cannot be driven: %s" % toolkit)
            skipped.append(22)
        else:
            block, restore = fixture_block(tmp)
            try:
                with scripted(RECORDED):
                    first = tool.probe_i(block)
                    second = tool.probe_ii(block)
                    fifth = tool.probe_v(block)
            finally:
                restore()
            case(22, "(i), (ii) and (v) walk end to end, window and all",
                 all(f.verdict in (tool.ANSWERED, tool.OPEN, tool.INVALID)
                     and f.run and f.lines for f in (first, second, fifth)),
                 "i=%r/%r ii=%r/%r v=%r/%r"
                 % (first.verdict, first.run, second.verdict, second.run,
                    fifth.verdict, fifth.run))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fail:
        print("RESULT: FAIL - see the FAILED lines above")
    else:
        print("RESULT: PASS - the gate goes red at each of its four points, every"
              " predicate decides at its edges, and every probe walks%s"
              % (" (%d skipped)" % len(skipped) if skipped else ""))
    return 1 if fail else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
