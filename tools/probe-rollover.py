#!/usr/bin/env python3
"""Drive the real CLI through a rollover, against a fake endpoint.

WHY THIS EXISTS AND THE UNIT SUITE DOES NOT COVER IT. cli/test_crow.py proves
should_roll, save_session, load_session and roll_over in isolation. None of that
says the repl() loop CALLS them, in the right order, at the right moment -- and
the wiring is where the interesting mistakes live. A mutation that flipped
roll_over's with_kv to True was caught by nothing until a test went through the
call site rather than the function.

The endpoint here is ~60 lines of http.server that answers /health, /props and
/v1/chat/completions with canned SSE. n_ctx is 100, so the 90 % mark is 90
tokens and a single turn crosses it. No model is loaded and no GPU is touched:
this runs in about a second, which is the point -- a probe nobody runs proves
nothing.

Run:  python tools/probe-rollover.py
Exit: 0 when every case holds, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "cli" / "crow.py"
N_CTX = 100
TOTAL_TOKENS = [95, 96, 97, 98]     # 95 >= 0.9 * 100, so turn one crosses it
SLOT_CALLS: list[str] = []
_turn = [0]
_lock = threading.Lock()

# How many replies ask for a tool before one answers. One puts the threshold
# crossing inside a turn -- the case the design exists for, and the one a check
# that runs only between turns misses entirely. Two makes the same turn cross it
# AGAIN after it has already rolled, which is the branch that has to give up
# rather than archive the note and ask the same question forever.
TOOL_ROUNDS = [0]
TOOL_DIR = [""]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json({"status": "ok"})
        elif self.path.startswith("/props"):
            self._json({"default_generation_settings": {"n_ctx": N_CTX}})
        else:
            self._json({}, 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if "/slots/" in self.path:
            SLOT_CALLS.append(self.path)
            return self._json({"result": "ok"})
        if "/chat/completions" not in self.path:
            return self._json({}, 404)

        with _lock:
            i = _turn[0]
            _turn[0] += 1

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        if i < TOOL_ROUNDS[0]:
            first = {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call-0", "type": "function",
                 "function": {"name": "list_dir",
                              "arguments": json.dumps({"path": TOOL_DIR[0]})}}]},
                "finish_reason": None}]}
            last_reason = "tool_calls"
        else:
            first = {"choices": [{"delta": {"content": f"reply {i}"}, "finish_reason": None}]}
            last_reason = "stop"

        for obj in (first,
                    {"choices": [{"delta": {}, "finish_reason": last_reason}],
                     "usage": {"total_tokens": TOTAL_TOKENS[min(i, len(TOTAL_TOKENS) - 1)]}}):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_cli(port: int, appdata: str, turns: list[str], *extra: str):
    env = dict(os.environ, LOCALAPPDATA=appdata, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(CLI), "--base-url", f"http://127.0.0.1:{port}/v1",
         "--no-font", "--no-background", "--no-update-check", *extra],
        input="".join(f"{t}\n" for t in turns) + "/exit\n",
        capture_output=True, text=True, env=env, timeout=120)
    return proc


def sessions(appdata: str) -> Path:
    return Path(appdata) / "Crow" / "session"


def main() -> int:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    root = tempfile.mkdtemp()
    results: list[tuple[bool, str]] = []

    def check(ok: bool, what: str):
        results.append((bool(ok), what))

    try:
        # --- the rollover itself -------------------------------------------
        app = os.path.join(root, "a")
        os.makedirs(app)
        out = run_cli(port, app, ["first question", "second question"]).stdout
        check("rolled over at 95 tokens" in out, "it rolls over at 95 of 100 tokens")

        archives = sorted(sessions(app).glob("rollover-*.json"))
        check(len(archives) == 1, "exactly one archive is written")
        if archives:
            saved = json.loads(archives[0].read_text(encoding="utf-8"))
            check(saved["kv"] is False, "the archive records that it has no cache")
            check([m["content"] for m in saved["messages"] if m["role"] != "system"]
                  == ["first question", "reply 0"],
                  "the archive holds the conversation verbatim")

        live = sessions(app) / "session.json"
        check(live.exists(), "the live session file still exists beside the archive")
        if live.exists():
            messages = json.loads(live.read_text(encoding="utf-8"))["messages"]
            after = [m for m in messages if m["role"] != "system"]
            check(after[0]["role"] == "user" and "second question" in after[0]["content"],
                  "the typed turn is carried into the fresh conversation")
            check(sum(1 for m in after[:2] if m["role"] == "user") == 1,
                  "note and carried turn share ONE user message")
            check(archives and str(archives[0].name) in after[0]["content"],
                  "the note names the archive it can be read from")

        # THE NEGATIVE HALF. Each of these must NOT happen, and a probe that
        # only shows the feature working cannot tell working from wired-always-on.
        check(len(SLOT_CALLS) == 1,
              f"the archive does not write the server's slot (saw {len(SLOT_CALLS)}, "
              f"expected 1: the save on exit)")

        # --- switched off ---------------------------------------------------
        app_off = os.path.join(root, "b")
        os.makedirs(app_off)
        out_off = run_cli(port, app_off, ["one", "two"], "--rollover-at", "0").stdout
        check("rolled over" not in out_off, "--rollover-at 0 does not roll at 95 of 100")
        check(not list(sessions(app_off).glob("rollover-*.json")),
              "--rollover-at 0 writes no archive")

        # --- a named session that is not there --------------------------------
        missing = run_cli(port, app, [], "--resume", "not-here.json")
        check(missing.returncode == 2, "a missing --resume exits 2 rather than starting empty")
        check("no session at" in missing.stderr, "and says which path it looked at")

        # --- a named session that is there ------------------------------------
        if archives:
            back = run_cli(port, app, [], "--resume", archives[0].name)
            check(back.returncode == 0, "an existing --resume starts")
            check("resumed from" in back.stdout, "and reports where it resumed from")

        # --- THE CASE THE DESIGN EXISTS FOR ------------------------------------
        # The threshold is crossed INSIDE a turn: the model asks for a tool, the
        # result lands, and the window is full before the user has typed again.
        # A check that only runs between turns sees none of this.
        app_loop = os.path.join(root, "c")
        os.makedirs(app_loop)
        _turn[0] = 0
        TOOL_DIR[0] = app_loop
        TOOL_ROUNDS[0] = 1
        try:
            loop_out = run_cli(port, app_loop, ["ask something that needs a tool"]).stdout
        finally:
            TOOL_ROUNDS[0] = 0
        check("mid-turn" in loop_out, "it rolls over INSIDE a turn, after a tool round")
        check("list_dir" in loop_out, "and the tool round really ran before it did")
        loop_archives = sorted(sessions(app_loop).glob("rollover-*.json"))
        check(len(loop_archives) == 1, "the mid-turn rollover writes its archive")
        if loop_archives:
            kept = json.loads(loop_archives[0].read_text(encoding="utf-8"))["messages"]
            check(any(m["role"] == "tool" for m in kept),
                  "the archive is a COMPLETE round -- the tool result is in it")
        live_loop = sessions(app_loop) / "session.json"
        if live_loop.exists():
            after = [m for m in json.loads(live_loop.read_text(encoding="utf-8"))["messages"]
                     if m["role"] != "system"]
            check(after and "ask something that needs a tool" in after[0]["content"],
                  "the question survives a mid-turn rollover instead of being archived away")
            note = after[0]["content"] if after else ""
            check(".md" in note, "the note points at the transcript, not only the JSON")
            check(app_loop in note, "the note says where the work had got to")

        if loop_archives:
            md = loop_archives[0].with_suffix(".md")
            check(md.exists(), "a readable transcript is written beside the archive")
            if md.exists():
                # The whole reason it exists: json.dump writes ONE line, and
                # read_file caps at MAX_TOOL_BYTES, so a single-line archive is
                # unreachable past the first few kilobytes.
                check(md.read_text(encoding="utf-8").count("\n") > 4,
                      "the transcript has lines, so a range can reach its end")
                check("reasoning" not in md.read_text(encoding="utf-8").lower(),
                      "and it leaves the reasoning out")
            check(loop_archives[0].read_text(encoding="utf-8").count("\n") > 4,
                  "the archive JSON is no longer a single line either")

        # --- and the branch that has to GIVE UP ---------------------------------
        # The same turn fills the window a second time. Rolling again would
        # archive the note, re-ask the same question and fill it again: the loop
        # would never end and every pass would leave a file behind.
        app_twice = os.path.join(root, "d")
        os.makedirs(app_twice)
        _turn[0] = 0
        TOOL_DIR[0] = app_twice
        TOOL_ROUNDS[0] = 3
        try:
            twice = run_cli(port, app_twice, ["a question that never fits"]).stdout
        finally:
            TOOL_ROUNDS[0] = 0
        check("filled again inside this turn" in twice,
              "a turn that fills the window twice stops instead of looping")
        check(len(list(sessions(app_twice).glob("rollover-*.json"))) == 1,
              "and it leaves ONE archive behind, not one per pass")
    finally:
        server.shutdown()
        shutil.rmtree(root, ignore_errors=True)

    width = max(len(what) for _, what in results)
    for ok, what in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {what.ljust(width)}")
    failed = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - failed}/{len(results)} checks"
          + (" -- all green" if not failed else f", {failed} RED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
