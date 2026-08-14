#!/usr/bin/env python3
"""The number cli/crow_gui.py's reader stands on, and what decides it.

WHY THIS EXISTS. #90's E12 carries a build rule against P1 ("`cancel()` against
a blocked `readline()` on Windows ... the abort button silently does nothing"):
whether a read timeout that fires MID-LINE costs bytes, and, once that answer
came back, WHAT WAKES A BLOCKED READ AT ALL. Together they decide where the
timeout may be armed and how short it may be.

0 EUR and no apparatus beyond a loopback socket: no model, no server, no
network.

AND IT IS A CHECKER, NOT A NOTEBOOK. Every point holds the measurement against
the constant the shipped code actually carries, read out of cli/crow_gui.py and
cli/crow_core.py as text. A measurement whose result lives only in a chat log is
the shape E9 was cut to avoid -- "a comment cannot go red".

WHAT POINT 1 WAS, AND WHY IT IS GONE (2026-08-14). It measured Tk queue
saturation: 4,000 deltas already queued when the first tick arrives, per-event
against per-tick drawing, held against TICK_MS and DRAIN_PER_TICK in the window.
Its result stands and is quoted where it is used --

    per event   4000 ticks | worst tick 9.046 ms | total 332.9 ms
    per tick       1 tick  | worst tick 4.841 ms | total   4.8 ms
    2,048 events fit one 16 ms half-frame on this machine

-- but 0.3.0 replaced the Tk window with a webview, which batches in the page and
carries neither constant. The point could no longer read its subject: it raised
`SETUP ERROR: crow_gui.py does not carry TICK_MS` and, because that error
returned from main, it took the two points BELOW it down with it. Twenty lines of
dead apparatus made 633 lines of live measurement unreachable for a day, and the
read-timeout probe -- the one that decides READ_TIMEOUT_S -- was among them.

THE POINTS KEEP THEIR NUMBERS. (2) and (3) are not renumbered to (1) and (2),
because they refer to each other by number in their own output and in
cli/crow_gui.py's comments. Renumbering would make every one of those references
quietly point at the wrong thing.

------------------------------------------------------------------- point 2 --

WHAT THE BYTE-LOSS PROBE FOUND, and it answers a different question than the one
the plan expected to ask. The rule was: count JSON decode errors, "more than
zero means the reader has to reassemble lines itself". The count is ZERO in
every arm -- and the reader still loses nearly everything. Three readers, each
against a loopback stream whose every line is cut into pieces with a pause
longer than the timeout in each gap, so the timeout is GUARANTEED to fire
mid-line (measured 2026-08-13):

  A  readline(), which is what cli/crow_core.py's reader does today, with the
     timeout caught and the read tried again:
         125 timeouts, 0 decode errors, 0 of 12 payloads, 12 missing
     The partial line the BufferedReader had accumulated is discarded when the
     raw read raises. Nothing torn ever reaches the decoder, so nothing is
     counted -- the line simply is not there.

  B  read1() with the lines assembled by hand, identity encoding:
         124 timeouts, 0 decode errors, 12 of 12 payloads, 0 missing
     Clean. This is the branch the plan's rule points at.

  C  the same reader against CHUNKED transfer encoding, which is what
     llama.cpp actually sends:
         10 timeouts, 0 decode errors, 1 of 12 payloads, 11 missing,
         http.client.IncompleteRead(0 bytes read)
     Assembling lines one layer up does not help, because the damage is one
     layer DOWN: http.client reads the chunk-size line with its own readline,
     and a timeout inside THAT loses the size and desynchronises the framing
     for the rest of the response.

  And the first attempt at B/C had to clear socket.SocketIO._timeout_occurred to
  read at all -- CPython refuses every later read on a timed-out socket object
  with "cannot read from timed out object".

FIRST CONCLUSION: a read timeout on this transport is a ONE-WAY DOOR. It cannot
be "a return to the loop", which is what the mockup's own sketch assumed (`raw =
resp.readline()  # Timeout gesetzt: kehrt zurueck`).

WHICH LEFT ONE WAY OUT -- arm it at the abort, where no further byte is wanted --
AND POINT 3 CLOSED IT. Against a loopback server that goes quiet for 10 s with
the reader blocked in recv, each of the three ways a process can try to end
somebody else's read [measured 2026-08-13]:

    settimeout(1.0)                the read woke after 9.7 s
    shutdown(SHUT_RDWR)            the read woke after 9.7 s
    the socket's own close()       the read woke after 9.7 s

All three woke when the SERVER hung up. None of them woke it. `resp.close()`
blocks with the reader as well -- it takes the BufferedReader's lock, which the
blocked read holds -- so the abort returns at the socket's ORIGINAL timeout:
1.96 s against a 2 s timeout, 11.95 s against a 30 s one. That is #90's P1 in
its purest form, and it means the second abort path can only be the socket
timeout AS IT STOOD WHEN THE READ STARTED.

SO THE NUMBER IS A CLIENT'S CHOICE IN FRONT OF THE TURN, and both values the plan
offered are ruled out by the first conclusion: a timeout under the longest
silence a HEALTHY turn is allowed cuts good answers, and that silence is 469.51 s
to the first token on a resumed 21k session [measured 2026-08-10,
cli/crow_core.py:581]. 20 s and 1 s are both far under it. cli/crow_gui.py ships
READ_TIMEOUT_S = 600 s: clear of the worst measured prefill, and a bound where
the CLI's 1800 s is effectively none.

Usage:  measure_gui_stream.py [--repo <dir>]

Exit 0 = every number measured and every constant holds.
     1 = a constant does not hold what this machine can do.
     2 = setup error (no loopback socket, missing source).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_operating_point import read  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What the probe's fake server sends. Long enough that one SSE line does not fit
# in a single TCP segment when it is deliberately cut up.
PROBE_CHUNKS = [json.dumps({"choices": [{"delta": {"content": "piece-%02d %s"
                                                   % (i, "x" * 40)}}]})
                for i in range(12)]


class SetupError(Exception):
    """The apparatus is not there. Exit 2, never a quiet pass."""


def constant(path: str, name: str) -> float:
    """Read `NAME = <number>` out of a source file, as text.

    RAW TEXT AND NOT AN IMPORT, the same reason tools/check_shared_core.py gives:
    importing cli/crow_gui.py would build a window, and a measurement that needs
    a window open to read a number is a measurement nobody runs twice.
    """
    try:
        text = read(path)
    except OSError as exc:
        raise SetupError("cannot read %s: %s" % (path, exc))
    found = re.search(r"^%s\s*(?::[^=\n]+)?=\s*([0-9.]+)" % re.escape(name),
                      text, re.M)
    if not found:
        raise SetupError("%s does not carry %s" % (os.path.basename(path), name))
    return float(found.group(1))


# --------------------------------------------------------------- point 2 ----

def _serve_split_lines(sock: socket.socket, gap: float, pieces: int,
                       chunked: bool) -> None:
    """Write a recorded SSE stream with every line cut into `pieces`.

    The cut is the apparatus: a timeout that only ever fires BETWEEN lines cannot
    be handed a torn one, so a probe built that way answers "no byte loss"
    without having looked.

    `chunked` is the arm that matters most, because it is the framing llama.cpp
    uses. The size line goes out in the same cut-up stream as the body, so a
    timeout can land inside it -- which is exactly where http.client loses it.
    """
    try:
        conn, _ = sock.accept()
    except Exception:
        return
    try:
        conn.recv(65536)
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        conn.sendall(head + (b"Transfer-Encoding: chunked\r\n\r\n" if chunked
                             else b"Connection: close\r\n\r\n"))
        for chunk in list(PROBE_CHUNKS) + ["[DONE]"]:
            body = ("data: " + chunk + "\n\n").encode("utf-8")
            frame = (("%x\r\n" % len(body)).encode() + body + b"\r\n") if chunked else body
            step = max(1, len(frame) // pieces)
            for at in range(0, len(frame), step):
                conn.sendall(frame[at:at + step])
                time.sleep(gap)
        if chunked:
            conn.sendall(b"0\r\n\r\n")
    except Exception:
        # The client hanging up mid-line IS the case under test. A server that
        # reported it as an error would print a red line for the expected result.
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _open_probe_stream(timeout: float, gap: float, pieces: int, chunked: bool):
    """A live response over loopback, plus the listening socket to close after."""
    try:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
    except Exception as exc:                     # noqa: BLE001
        raise SetupError("cannot open a loopback socket: %s" % exc)
    port = sock.getsockname()[1]
    threading.Thread(target=_serve_split_lines, args=(sock, gap, pieces, chunked),
                     daemon=True).start()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port, data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST")
    return urllib.request.urlopen(req, timeout=timeout), sock


def _count(seen: list[str], decode_errors: int, timeouts: int, broke: str | None) -> dict:
    missing = [c for c in PROBE_CHUNKS if c not in seen]
    return {"timeouts": timeouts, "payloads": len(seen), "of": len(PROBE_CHUNKS),
            "decode_errors": decode_errors, "missing": len(missing), "broke": broke}


def _resume(resp) -> bool:
    """Clear CPython's "this object timed out" latch. False when it is not there.

    socket.SocketIO.readinto sets `_timeout_occurred` and every LATER read on the
    object raises "cannot read from timed out object". Without clearing it, no
    arm below gets past its first timeout -- which would measure the latch rather
    than the bytes.
    """
    try:
        resp.fp.raw._timeout_occurred = False
        return True
    except Exception:
        return False


def probe_readline(timeout: float, gap: float, pieces: int) -> dict:
    """ARM A: readline() across the timeout -- what the core's reader does."""
    resp, sock = _open_probe_stream(timeout, gap, pieces, chunked=False)
    seen, timeouts, decode_errors, broke = [], 0, 0, None
    try:
        while True:
            try:
                raw = resp.readline()
            except TimeoutError:
                timeouts += 1
                if not _resume(resp):
                    broke = "the timeout latch cannot be cleared"
                    break
                continue
            except Exception as exc:             # noqa: BLE001 - the finding
                broke = "%s: %s" % (type(exc).__name__, exc)
                break
            if not raw:
                break
            payload = raw.decode("utf-8", "replace").strip()
            if not payload.startswith("data:"):
                continue
            payload = payload[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                json.loads(payload)
                seen.append(payload)
            except json.JSONDecodeError:
                decode_errors += 1
    finally:
        for closeable in (resp, sock):
            try:
                closeable.close()
            except Exception:
                pass
    return _count(seen, decode_errors, timeouts, broke)


def probe_assembled(timeout: float, gap: float, pieces: int, chunked: bool) -> dict:
    """ARMS B and C: read1() with the lines put back together by hand."""
    resp, sock = _open_probe_stream(timeout, gap, pieces, chunked)
    seen, timeouts, decode_errors, broke = [], 0, 0, None
    buf, done = b"", False
    try:
        while not done:
            try:
                block = resp.read1(4096)
            except TimeoutError:
                timeouts += 1
                if not _resume(resp):
                    broke = "the timeout latch cannot be cleared"
                    break
                continue
            except Exception as exc:             # noqa: BLE001 - the finding
                broke = "%s: %s" % (type(exc).__name__, exc)
                break
            if not block:
                break
            buf += block
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                payload = raw.decode("utf-8", "replace").strip()
                if not payload.startswith("data:"):
                    continue
                payload = payload[len("data:"):].strip()
                if payload == "[DONE]":
                    done = True
                    break
                try:
                    json.loads(payload)
                    seen.append(payload)
                except json.JSONDecodeError:
                    decode_errors += 1
    finally:
        for closeable in (resp, sock):
            try:
                closeable.close()
            except Exception:
                pass
    return _count(seen, decode_errors, timeouts, broke)


def _quiet_server(quiet_for: float) -> tuple[int, socket.socket]:
    """One chunk, then silence. The shape a prefill has from the client's side."""
    try:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
    except Exception as exc:                     # noqa: BLE001
        raise SetupError("cannot open a loopback socket: %s" % exc)

    def serve() -> None:
        try:
            conn, _ = sock.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Connection: close\r\n\r\n")
            conn.sendall(b"data: " + PROBE_CHUNKS[0].encode("utf-8") + b"\n\n")
            time.sleep(quiet_for)
            conn.close()
        except Exception:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1], sock


def probe_wake(how: str, timeout: float, quiet_for: float) -> dict:
    """Does anything this process does end a read that is ALREADY blocked?

    The question the abort path turns on. A reader thread is put into a blocking
    `readline` against a server that has gone quiet, and then one of the three
    things a caller can reach for is tried. What is measured is WHEN the reader
    came back -- if that is the moment the server hung up, the call did nothing.
    """
    port, sock = _quiet_server(quiet_for)
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port,
                                 data=b"{}", method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    box: dict = {}

    def reader() -> None:
        started = time.monotonic()
        try:
            while True:
                if not resp.readline():
                    break
        except Exception as exc:                 # noqa: BLE001 - the finding
            box["raised"] = type(exc).__name__
        box["lived"] = round(time.monotonic() - started, 2)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.3)
    started = time.monotonic()
    tried = "ok"
    try:
        inner = resp.fp.raw._sock
        if how == "settimeout":
            inner.settimeout(1.0)
        elif how == "shutdown":
            inner.shutdown(socket.SHUT_RDWR)
        else:
            inner.close()
    except Exception as exc:                     # noqa: BLE001
        tried = "%s: %s" % (type(exc).__name__, exc)
    thread.join(quiet_for + 5.0)
    woke_after = round(time.monotonic() - started, 2)
    for closeable in (resp, sock):
        try:
            closeable.close()
        except Exception:
            pass
    return {"how": how, "quiet_for": quiet_for, "woke_after": woke_after,
            "tried": tried, **box}


# ------------------------------------------------------------------ report --

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args(argv[1:])

    gui = os.path.join(args.repo, "cli", "crow_gui.py")
    failed, total = 0, 0

    print("(2) BYTE LOSS UNDER A MID-LINE READ TIMEOUT -- P1, what decides "
          "where the timeout may be armed")
    try:
        arms = [
            ("A readline, identity", probe_readline(0.05, 0.12, 6)),
            ("B assembled, identity", probe_assembled(0.05, 0.12, 6, False)),
            ("C assembled, chunked", probe_assembled(0.05, 0.12, 6, True)),
        ]
    except SetupError as exc:
        print("    SETUP ERROR: %s" % exc)
        return 2
    for label, row in arms:
        print("    %-22s %4d timeouts | %2d/%2d payloads | %d decode errors | "
              "%2d missing%s"
              % (label, row["timeouts"], row["payloads"], row["of"],
                 row["decode_errors"], row["missing"],
                 " | " + row["broke"] if row["broke"] else ""))
    torn = sum(row["decode_errors"] for _, row in arms)
    lost = sum(row["missing"] for _, row in arms)
    print("    %d decode errors and %d LOST payloads: the count the plan's "
          "rule reads is zero, and the loss is total anyway -- a discarded "
          "line is never handed to a decoder" % (torn, lost))
    total += 1
    if lost > 0:
        print("    OK       a read timeout on this transport is a one-way "
              "door -- so it may never sit under a healthy turn's longest "
              "silence; point 3 decides where it can sit at all")
    else:
        failed += 1
        print("    FAILED   no arm lost a payload -- the probe never fired "
              "mid-line and this run decided nothing")
    print("")
    print("(3) WHAT WAKES A READ THAT IS ALREADY BLOCKED -- P1, and the "
          "reason the timeout cannot sit at the abort")
    quiet = 6.0
    try:
        wakes = [probe_wake(how, 30.0, quiet)
                 for how in ("settimeout", "shutdown", "close")]
    except SetupError as exc:
        print("    SETUP ERROR: %s" % exc)
        return 2
    for row in wakes:
        print("    %-12s woke the read after %5.2f s of a %.1f s silence%s"
              % (row["how"], row["woke_after"], row["quiet_for"],
                 " | " + row["raised"] if row.get("raised") else ""))
    useless = [r for r in wakes if r["woke_after"] >= quiet - 1.0]
    total += 1
    if len(useless) == len(wakes):
        print("    OK       none of the three woke it -- every one came back "
              "when the SERVER hung up, so the only bound is the socket "
              "timeout as it stood when the read started")
    else:
        failed += 1
        woke = ", ".join(r["how"] for r in wakes if r not in useless)
        print("    FAILED   %s woke a blocked read on this platform -- the "
              "abort could be armed at the abort after all, and "
              "cli/crow_gui.py's READ_TIMEOUT_S can be tightened" % woke)

    total += 1
    try:
        bound = constant(gui, "READ_TIMEOUT_S")
    except SetupError as exc:
        print("    SETUP ERROR: %s" % exc)
        return 2
    # Two-sided, because both sides have cost a measurement. Under 469.51 s
    # it cuts a healthy resumed prefill [measured 2026-08-10]; at the CLI's
    # 1800 s it is not a bound at all.
    if 469.51 < bound < 1800.0:
        print("    OK       READ_TIMEOUT_S %.0f s clears the worst measured "
              "prefill (469.51 s) and still bounds a leaked reader" % bound)
    else:
        failed += 1
        print("    FAILED   READ_TIMEOUT_S %.0f s is either under the worst "
              "measured prefill of 469.51 s -- which cuts good answers -- or "
              "at the CLI's 1800 s, which bounds nothing" % bound)
    print("")

    print("RESULT: %d of %d numbers hold" % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
