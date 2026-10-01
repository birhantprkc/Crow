#!/usr/bin/env python3
r"""The boot menu: start one of the three operating points, then open Crow.

#196 phase 1. One terminal menu that starts crow-nest's serve (and, for the
Image Stack, sd-server beside it), waits until it answers, writes the contract
file the window reads (`crow_core.write_active_point`) and opens the window
against it. Run it bare for the menu; the flags below do the same steps without
a menu, for a script or a test.

    python cli/crow_boot.py                       the menu
    python cli/crow_boot.py --status              what runs (exit 0), or nothing (exit 1)
    python cli/crow_boot.py --start 27b           start one point (flash-next, 27b, image-stack)
    python cli/crow_boot.py --stop                stop the running point
    python cli/crow_boot.py --start-crow          open the window against the running point
    python cli/crow_boot.py --create-shortcut DIR a Windows shortcut to this menu in DIR

Exit codes: 0 done, 1 failed (or, for --status, nothing runs), 2 setup error
(no stack.json, unknown point, no engine binary), 3 refused because another
point already runs.

THE DECISIONS, and why each is this way:

* EVERYTHING ABOUT A POINT COMES FROM manifests/stack.json: binary, env, argv,
  cwd, dirs, port, readiness probe, identity, menu text, the image server's
  argv and its platform argv, and the window's `crow_env`. Nothing here names
  a model file. `${INSTALL}` is Crow's install root
  (`crow_platform.install_dir()`, `--install-root` overrides) and `${MODELS}`
  is `crow_platform.models_dir(install)` -- `$CROW_MODELS`, else
  `<install>/models` (`--models` overrides). A value that held a placeholder
  is a path and is normalised for the platform; any other value (`te=cpu`,
  `8099`) is passed as written. stack.json is looked up beside cli/ at
  `../manifests/stack.json` (`--stack` overrides).
* THE ENGINE ENV IS THE STACK'S, NOT THE SHELL'S: every key any point declares
  is dropped from the inherited environment before the chosen point's own are
  set, so a `CROW_HOTSETS` left over from Flash-Next cannot reach the 27B.
* DETACHED: `crow_platform.spawn_kwargs(detached=True)` (Windows:
  CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, #158 -- a Ctrl+C in this menu or
  closing its console does not reach the server), plus
  CREATE_BREAKAWAY_FROM_JOB so a terminal that kills its job on close does not
  take the server along; where the job forbids breakaway, the start is retried
  without it. stdin is DEVNULL, stdout and stderr go to
  `<log dir>/serve-<port>.log` and `sd-server-<port>.log`, with the previous
  run kept as `.prev.log` (`crow_core._keep_previous_log`). The log dir is
  `crow_platform.log_dir()` (Windows: `runs\` under the current folder -- the
  shortcut starts the menu in the install root; if that folder cannot be
  created, `<install>/runs`).
* READINESS is the stack's probe: serve `GET /health` == `{"status": "ok"}`
  (it answers only once fully loaded), sd-server `GET /sdcpp/v1/capabilities`
  == 200; a 5xx from sd-server is final (#324), a 5xx from serve is waited
  out. Timeouts: Flash-Next 600 s (measured cold start ~83 s, 2026-09-11),
  the 27B and the Image Stack's serve 300 s (measured 10-20 s), sd-server
  300 s after serve is up. A process that exits early fails at once.
* THE IMAGE STACK STARTS sd-server ONLY AFTER serve IS READY, in
  `crow_core.image_server_workdir()` (an empty folder Crow owns, #324), with
  `crow_core._image_server_env()`. On failure, timeout or Ctrl+C, what this run
  started is ended by its handle (`crow_platform.terminate_tree`), the last 10
  log lines are shown, and no contract file is written.
* ONE POINT AT A TIME, checked before anything starts: the contract file
  (`crow_core.read_active_point`), then the process scan
  (`crow_core.running_servers(include_image=True)`; a serve is named by
  `crow_core.point_for_server`), then port 8099 itself in case the process list
  was unreadable. It holds in both directions: a llama-server blocks a
  baseline start and a baseline point blocks an optional one. The refusal
  names what runs and how to stop it: the menu entry, `--stop`, or the pid
  with `taskkill /PID <pid> /F` (`kill <pid>`).
* OPTIONAL llama.cpp LINES (the owner, 2026-10-01: the crow-nest points are
  the baseline). Crow's llama.cpp lines from manifests/operating-point.json
  (8081/8082/8083) are listed below the three points under "Optional
  (llama.cpp)", tagged "(optional)" and dimmed (the plain fallback keeps the
  tag). A line is listed only when `crow_core.server_command` resolves its
  binary and GGUF (`$CROW_MODELS`, `CROW_LLAMA_SERVER_<KEY>`); one that does
  not is left out, not shown as "not installed". Starting one uses
  crow_core.start_server's argv, env overlay, scope prefix, slot folder and
  `llama-server-<port>.out/.err.log`, with this menu's spawn, animation and
  wait (`/props` answers 200, 600 s). No contract file is written for it: its
  `point` is one of the three baseline ids. `--models` is handed on as
  `CROW_MODELS` so both kinds resolve under one root.
* STOP ends every model server the process scan sees (crow_core.stop_servers'
  set: serve, sd-server, llama-server), waits until each is torn down
  (`process_exists`: the process handle, not the exit code -- Windows tears
  a 13 GB server down for ~1.6 s after it has one) and no longer listed, up to
  30 s, and removes the contract file. A pid that only the contract file names is not
  killed (a pid outlives its process and may belong to another program by
  now); it is reported with the command.
* START CROW opens `crow_gui.py --base-url <the point's URL>` detached
  (pythonw.exe on Windows when it sits beside the interpreter), with the
  point's `crow_env` (the Image Stack's CROW_IMAGE_MODEL_DIR) in its
  environment; with a llama-server running, on `http://127.0.0.1:<its
  --port>/v1`, naming the server. Only when no model server with an address
  runs (nothing, or an sd-server alone) it says so and starts nothing.
* THE TERMINAL: stdout is forced to UTF-8; on Windows VT processing is
  switched on (SetConsoleMode, ENABLE_VIRTUAL_TERMINAL_PROCESSING). If that
  fails, or stdout is not a terminal, the output is plain: ASCII markers, no
  colour, no animation. NO_COLOR drops only the colour.
* ONE MENU, NOT TWO: Start Crow, the three points, the optional lines, Stop,
  Quit. "Landed" shows
  for 5 seconds and returns to this menu by itself; errors and refusals wait
  for Enter so they can be read.
* THE SHORTCUT (`--create-shortcut DIR`, Windows): `DIR\Crow.lnk`, written by
  PowerShell's WScript.Shell with every value passed through the environment
  (no quoting). Target: Windows Terminal (`wt.exe`) when it is on PATH, running
  this interpreter with this script; else the interpreter itself. Working
  folder: the install root. Icon: cli/crow.ico.

STANDARD LIBRARY ONLY, plus Crow's own crow_core and crow_platform.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import crow_core  # noqa: E402
import crow_platform  # noqa: E402

DEFAULT_STACK = os.path.normpath(os.path.join(HERE, os.pardir, "manifests", "stack.json"))
GUI_SCRIPT = os.path.join(HERE, "crow_gui.py")
ICON_FILE = os.path.join(HERE, "crow.ico")
PLATFORM_KEY = "windows" if crow_platform.IS_WINDOWS else "linux"

BOOT_TIMEOUT_S = {"flash-next": 600.0}
DEFAULT_BOOT_TIMEOUT_S = 300.0
IMAGE_TIMEOUT_S = 300.0
LLAMA_TIMEOUT_S = 600.0             # crow_core.start_server's own wait_s
LANDED_S = 5
OPENED_S = 3
POLL_S = 1.0
FRAME_S = 0.12
STOP_WAIT_S = 30.0
LOG_TAIL_LINES = 10

EXIT_OK, EXIT_FAILED, EXIT_SETUP, EXIT_REFUSED = 0, 1, 2, 3

CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# (fancy, plain): the plain half is what a console without VT gets.
ICONS = {
    "crow": ("\U0001F426", "*"),
    "nest": ("\U0001FAB9", "[nest]"),
    "landed": ("\U0001FABA", "[ok]"),
    "start": ("\U0001F680", ">"),
    "flash-next": ("\U0001F9E0", "-"),
    "27b": ("⚡", "-"),
    "image-stack": ("\U0001F3A8", "-"),
    "optional": ("\U0001F999", "-"),
    "stop": ("\U0001F6D1", "x"),
    "quit": ("\U0001F44B", "q"),
    "on": ("\U0001F7E2", "[on]"),
    "off": ("⚪", "[off]"),
    "warn": ("⚠️ ", "!"),
    "fail": ("❌", "x"),
    "brush": ("\U0001F58C️ ", "~"),
}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def kill_hint(pid) -> str:
    """The command that ends one process by hand on this platform."""
    return ("taskkill /PID %s /F" % pid) if crow_platform.IS_WINDOWS else ("kill %s" % pid)


def base_url_for(port: int) -> str:
    return "http://127.0.0.1:%d/v1" % int(port)


# ----------------------------------------------------------------- stack.json ---

class SetupError(Exception):
    """The stack or the install cannot be used as it is. Exit 2."""


def load_stack(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise SetupError("cannot read %s: %s" % (path, exc))
    except ValueError as exc:
        raise SetupError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("points"), list):
        raise SetupError("%s has no points list" % path)
    return doc


def resolve(value, install: str, models: str):
    """One stack value with ${INSTALL} and ${MODELS} filled in.

    A value that held a placeholder is a path: normalised for this platform.
    Anything else is passed as written.
    """
    text = str(value)
    if "${INSTALL}" not in text and "${MODELS}" not in text:
        return text
    text = text.replace("${MODELS}", models).replace("${INSTALL}", install)
    return os.path.normpath(text)


def _binary(spec: dict, point_id: str, what: str) -> str:
    binary = (spec.get("binary") or {}).get(PLATFORM_KEY)
    if not binary:
        raise SetupError("stack.json names no %s binary for %s on %s"
                         % (what, point_id, PLATFORM_KEY))
    return binary


def plan_point(stack: dict, point_id: str, install: str, models: str) -> dict:
    """Everything needed to start one point, placeholders resolved."""
    points = {p.get("id"): p for p in stack["points"]}
    point = points.get(point_id)
    if point is None:
        raise SetupError("unknown operating point %r (one of %s)"
                         % (point_id, ", ".join(p for p in points if p)))

    def r(value):
        return resolve(value, install, models)

    engine = point.get("engine") or {}
    port = int(engine.get("port") or crow_core.CROW_NEST_PORT)
    serve = {
        "argv": [r(_binary(engine, point_id, "engine"))]
                + [r(a) for a in engine.get("argv") or []],
        "env": {str(k): r(v) for k, v in (engine.get("env") or {}).items()},
        "cwd": r(engine.get("cwd") or "${INSTALL}"),
        "dirs": [r(d) for d in engine.get("dirs") or []],
        "port": port,
        "readiness": engine.get("readiness") or {"path": "/health", "status": 200},
    }
    image = None
    spec = point.get("image_server")
    if spec:
        image = {
            "argv": [r(_binary(spec, point_id, "image server"))]
                    + [r(a) for a in spec.get("argv") or []]
                    + [r(a) for a in (spec.get("argv_platform") or {}).get(PLATFORM_KEY) or []],
            "port": int(spec.get("port") or 8097),
            "readiness": spec.get("readiness") or {"path": "/", "status": 200},
        }
    menu = point.get("menu") or {}
    return {
        "id": point_id,
        "title": menu.get("title") or point_id,
        "line": menu.get("line") or "",
        "serve": serve,
        "image": image,
        "crow_env": {str(k): r(v) for k, v in (point.get("crow_env") or {}).items()},
        "base_url": base_url_for(port),
        "identity": engine.get("identity") or {},
        "timeout": BOOT_TIMEOUT_S.get(point_id, DEFAULT_BOOT_TIMEOUT_S),
    }


def engine_env_keys(stack: dict) -> set:
    """Every env key any point's engine declares."""
    keys = set()
    for point in stack["points"]:
        keys.update((point.get("engine") or {}).get("env") or {})
    return keys


def missing_files(plan: dict, install: str, models: str) -> list:
    """Placeholder paths the point needs that are not on disk (dirs excluded)."""
    roots = tuple(os.path.normpath(p) for p in (install, models))
    dirs = set(plan["serve"]["dirs"])
    wanted = [plan["serve"]["argv"][0]] + list(plan["serve"]["env"].values())
    wanted += [a for a in plan["serve"]["argv"][1:] if a not in dirs]
    if plan["image"]:
        wanted += plan["image"]["argv"]
    out = []
    for path in wanted:
        if not os.path.normpath(path).startswith(roots) or path in dirs:
            continue
        if not os.path.exists(path) and path not in out:
            out.append(path)
    return out


# ------------------------------------------------------------- the terminal ---

def _enable_vt(stream) -> bool:
    """Switch on VT processing for this console. False when it cannot be had."""
    if not crow_platform.IS_WINDOWS:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel32.GetStdHandle(-11)                 # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:                       # noqa: BLE001 - plain output is the answer
        return False


class Style:
    """How this terminal may be written to: icons, colour, an animated line."""

    def __init__(self, fancy: bool = False, colour: bool = False, animate: bool = False):
        self.fancy, self.colour, self.animate = fancy, colour, animate

    @classmethod
    def detect(cls, stream) -> "Style":
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                   # noqa: BLE001 - a wrapped stream keeps its own
            pass
        tty = bool(getattr(stream, "isatty", lambda: False)())
        vt = tty and _enable_vt(stream)
        return cls(fancy=vt, colour=vt and not os.environ.get("NO_COLOR"), animate=vt)

    def icon(self, key: str) -> str:
        fancy, plain = ICONS[key]
        return fancy if self.fancy else plain

    def paint(self, text: str, code: str) -> str:
        return "\x1b[%sm%s\x1b[0m" % (code, text) if self.colour else text

    def bold(self, text: str) -> str:
        return self.paint(text, "1")

    def dim(self, text: str) -> str:
        return self.paint(text, "2")


def flight_frame(i: int, fancy: bool, width: int = 16) -> str:
    """One frame of the in-line animation: the crow flying towards the nest."""
    if not fancy:
        return "|/-\\"[i % 4]
    pos = i % width
    track = "".join("\U0001F426" if n == pos else "·" for n in range(width))
    return track + " \U0001FAB9"


# ------------------------------------------------------------------ helpers ---

def http_get(url: str, timeout: float = 2.0):
    """(status, body) for one GET; (None, b"") when nothing answers."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:                   # noqa: BLE001
            body = b""
        exc.close()
        return exc.code, body
    except Exception:                       # noqa: BLE001 - not answering is the answer
        return None, b""


READY, WAIT, BROKEN = "ready", "wait", "broken"


def readiness_state(spec: dict, status, body: bytes, fatal_5xx: bool) -> str:
    if status is None:
        return WAIT
    if status == int(spec.get("status", 200)):
        want = spec.get("json")
        if not want:
            return READY
        try:
            doc = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return WAIT
        if isinstance(doc, dict) and all(doc.get(k) == v for k, v in want.items()):
            return READY
        return WAIT
    if status >= 500 and fatal_5xx:
        return BROKEN
    return WAIT


def log_tail(path: str, lines: int = LOG_TAIL_LINES) -> list:
    """The last lines of a server log, progress redraws and colour removed."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = [_ANSI.sub("", r).rstrip() for r in re.split(r"[\r\n]+", text)]
    return [r for r in rows if r.strip()][-lines:]


def process_exists(pid) -> bool:
    """True until the process is really gone, teardown included.

    NOT crow_platform.pid_alive, WHICH ANSWERS "HAS NO EXIT CODE YET". Measured
    2026-10-01 on a 27B llama-server (13 GB) after taskkill /F: pid_alive and the
    Get-CimInstance scan said gone after 0.08 s, the process handle was signalled
    and tasklist dropped it only after 1.61 s -- the time Windows needed to tear
    down its memory. A next point started in between shares the card with it.
    Windows: a SYNCHRONIZE handle, signalled once the process object is done.
    Elsewhere: pid_alive.
    """
    if not crow_platform.IS_WINDOWS:
        return crow_platform.pid_alive(pid)
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(0x00100000, False, number)          # SYNCHRONIZE
    if not handle:
        return ctypes.get_last_error() == 5                           # ACCESS_DENIED
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102       # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _detached_kwargs(breakaway: bool) -> dict:
    kw = dict(crow_platform.spawn_kwargs(detached=True))
    if crow_platform.IS_WINDOWS and breakaway:
        kw["creationflags"] = kw.get("creationflags", 0) | CREATE_BREAKAWAY_FROM_JOB
    return kw


def _quote(arg: str) -> str:
    return '"%s"' % arg if (not arg or " " in arg or "\t" in arg) else arg


class LlamaLines:
    """Crow's optional llama.cpp operating points, as crow_core knows them.

    The lines are manifests/operating-point.json's `servers` (8081/8082/8083);
    every answer is crow_core's: `bootable_models`, `server_command` (the GGUF
    under `$CROW_MODELS` or `<install>/models`, the binary from
    `CROW_LLAMA_SERVER_<KEY>` or the line's own or `<install>/bin`),
    `server_port`, `model_label`, `server_env`. A test hands in its own.
    """

    def __init__(self, install: str):
        self.install = install

    def keys(self) -> tuple:
        return crow_core.bootable_models()

    def command(self, key: str) -> list:
        """The argv; raises crow_core.ServerBootError when it does not resolve."""
        return crow_core.server_command(key, None, self.install)

    def port(self, key: str):
        return crow_core.server_port(key)

    def label(self, key: str) -> str:
        return crow_core.model_label(key)

    def env(self, key: str) -> dict:
        return crow_core.server_env(key)

    def installed(self) -> list:
        """[{key, title, port, argv}] for every line whose files resolve on disk.

        A line that does not resolve is left out of the menu, not shown as
        "not installed": the three baseline points are the product, and a list
        of models nobody downloaded is noise.
        """
        out = []
        for key in self.keys():
            try:
                argv = self.command(key)
            except Exception:              # noqa: BLE001 - not on disk is "not listed"
                continue
            port = self.port(key)
            if not port:
                continue
            out.append({"key": key, "title": self.label(key) or key,
                        "port": int(port), "argv": argv})
        return out


# ---------------------------------------------------------------- the boot ---

class Boot:
    """The menu and its steps. Every outside effect is a parameter, for tests."""

    def __init__(self, stack: dict, install: str, models: str, out=None,
                 style: "Style | None" = None, popen=None, get=None,
                 sleep=None, clock=None, read=None, scan=None, active=None,
                 point_for=None, model_path=None, write_active=None,
                 terminate=None, kill=None, alive=None, log_dir=None,
                 forwarded_args=(), llama=None):
        self.stack, self.install, self.models = stack, install, models
        self.out = out or sys.stdout
        self.style = style or Style()
        self.popen = popen or subprocess.Popen
        self.get = get or http_get
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.read = read or input
        self.scan = scan or (lambda: crow_core.running_servers(include_image=True))
        self.active = active or crow_core.read_active_point
        self.point_for = point_for or crow_core.point_for_server
        self.model_path = model_path or crow_core.server_model_path
        self.write_active = write_active or crow_core.write_active_point
        self.terminate = terminate or crow_platform.terminate_tree
        self.kill = kill or crow_platform.kill_pid
        self.alive = alive or process_exists
        self._log_dir = log_dir
        self._note = ""
        self.forwarded_args = list(forwarded_args)
        self.llama = llama or LlamaLines(install)
        self._llama_cache = None
        self.titles = {p.get("id"): (p.get("menu") or {}).get("title") or p.get("id")
                       for p in stack["points"]}

    # -- output ------------------------------------------------------------
    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def _line(self, text: str) -> None:
        """Redraw the current line in place (animated terminals only)."""
        self.out.write("\r\x1b[2K" + text)
        self.out.flush()

    def _end_line(self) -> None:
        if self.style.animate:
            self.out.write("\r\x1b[2K")
            self.out.flush()

    def countdown(self, seconds: int, what: str) -> None:
        """Wait `seconds`, showing it; returns by itself."""
        for left in range(int(seconds), 0, -1):
            text = "   %s in %d s..." % (what, left)
            if self.style.animate:
                self._line(self.style.dim(text))
            elif left == seconds:
                self.say(text)
            self.sleep(1)
        self._end_line()

    def pause(self) -> None:
        try:
            self.read(self.style.dim("   Press Enter to return to the menu. "))
        except (EOFError, KeyboardInterrupt):
            pass

    def optional_lines(self) -> list:
        """The installed llama.cpp lines, resolved once per run."""
        if self._llama_cache is None:
            self._llama_cache = self.llama.installed()
        return self._llama_cache

    def label(self, point_id: "str | None") -> str:
        if not point_id:
            return "crow-nest (point not identified yet, it may still be loading)"
        return "%s (%s)" % (self.titles.get(point_id, point_id), point_id)

    def log_dir(self) -> str:
        if self._log_dir:
            return self._log_dir
        folder = crow_platform.log_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            folder = os.path.join(self.install, "runs")
        self._log_dir = folder
        return folder

    # -- what runs ---------------------------------------------------------
    def detect(self) -> "dict | None":
        """The running point (or other model server), or None."""
        doc = self.active()
        if doc:
            pids = doc.get("pids") or {}
            return {"kind": crow_platform.KIND_CROW_NEST, "point": doc.get("point"),
                    "source": "active-point",
                    "pids": {"serve": pids.get("serve"), "image": pids.get("image")},
                    "base_url": doc.get("base_url") or base_url_for(crow_core.CROW_NEST_PORT),
                    "since": doc.get("started_at")}
        found = self.scan() or []
        by_kind = {}
        for pid, line in found:
            by_kind.setdefault(crow_platform.server_kind(line), []).append((pid, line))
        serves = by_kind.get(crow_platform.KIND_CROW_NEST, [])
        images = by_kind.get(crow_platform.KIND_IMAGE, [])
        llamas = by_kind.get(crow_platform.KIND_LLAMA, [])
        image_pid = images[0][0] if images else None
        if serves:
            pid, line = serves[0]
            port = crow_platform.server_port(line) or crow_core.CROW_NEST_PORT
            url = base_url_for(port)
            return {"kind": crow_platform.KIND_CROW_NEST, "point": self.point_for(url),
                    "source": "scan", "pids": {"serve": pid, "image": image_pid},
                    "base_url": url}
        if llamas:
            pid, line = llamas[0]
            port = crow_platform.server_port(line)
            return {"kind": crow_platform.KIND_LLAMA, "point": None, "source": "scan",
                    "pids": {"serve": pid, "image": image_pid}, "port": port,
                    "base_url": base_url_for(port) if port else None,
                    "model": crow_core.served_model(line)}
        if images:
            return {"kind": crow_platform.KIND_IMAGE, "point": None, "source": "scan",
                    "pids": {"serve": None, "image": image_pid}}
        url = base_url_for(crow_core.CROW_NEST_PORT)
        point = self.point_for(url)
        if point:
            return {"kind": crow_platform.KIND_CROW_NEST, "point": point,
                    "source": "port", "pids": {"serve": None, "image": None},
                    "base_url": url}
        return None

    def describe(self, running: dict) -> str:
        """One phrase naming what runs."""
        kind = running["kind"]
        pids = running.get("pids") or {}
        if kind == crow_platform.KIND_LLAMA:
            where = " on port %s" % running["port"] if running.get("port") else ""
            model = " (%s)" % os.path.basename(running["model"]) if running.get("model") else ""
            return "a llama.cpp server%s%s, pid %s" % (model, where, pids.get("serve"))
        if kind == crow_platform.KIND_IMAGE:
            return "an image server (sd-server) without its language model, pid %s" % pids.get("image")
        bits = []
        if pids.get("serve"):
            bits.append("serve pid %s" % pids["serve"])
        if pids.get("image"):
            bits.append("sd-server pid %s" % pids["image"])
        return "%s%s" % (self.label(running.get("point")),
                         ", " + ", ".join(bits) if bits else "")

    def refusal(self, running: dict) -> str:
        """Why a second point is not started, and how to stop the first."""
        pids = running.get("pids") or {}
        lines = ["%s Another model server is already running: %s."
                 % (self.style.icon("warn"), self.describe(running)),
                 "   Only one operating point can run at a time."]
        lines.append('   To stop it: choose "Stop the running point" in this menu, or run')
        lines.append("     python %s --stop" % os.path.join("cli", "crow_boot.py"))
        hand = [kill_hint(p) for p in (pids.get("serve"), pids.get("image")) if p]
        if hand:
            lines.append("   or by hand: %s" % " and ".join(hand))
        return "\n".join(lines)

    # -- start -------------------------------------------------------------
    def _spawn(self, argv, **kw):
        try:
            return self.popen(argv, **kw, **_detached_kwargs(breakaway=True))
        except OSError as exc:
            if crow_platform.IS_WINDOWS and getattr(exc, "winerror", None) == 5:
                return self.popen(argv, **kw, **_detached_kwargs(breakaway=False))
            raise

    def _open_log(self, name: str):
        path = os.path.join(self.log_dir(), name)
        keep = getattr(crow_core, "_keep_previous_log", None)
        if keep is not None:
            keep(path)
        return path, open(path, "wb")

    def _wait(self, procs, port: int, readiness: dict, timeout: float,
              fatal_5xx: bool, message: str):
        """Poll until ready. None on success, else the reason."""
        url = "http://127.0.0.1:%d%s" % (port, readiness.get("path") or "/")
        start = self.clock()
        next_probe = start
        frame = 0
        if not self.style.animate:
            self.say("   %s (waiting for %s)" % (message, url))
        while True:
            now = self.clock()
            for name, proc in procs:
                code = proc.poll()
                if code is not None:
                    self._end_line()
                    return "%s exited with code %s before it answered" % (name, code)
            if now >= next_probe:
                status, body = self.get(url, 2.0)
                state = readiness_state(readiness, status, body, fatal_5xx)
                if state == READY:
                    self._end_line()
                    return None
                if state == BROKEN:
                    self._end_line()
                    return "%s answered HTTP %s on %s" % (procs[-1][0], status, url)
                next_probe = self.clock() + POLL_S
            elapsed = self.clock() - start
            if elapsed > timeout:
                self._end_line()
                return "%s did not answer within %.0f s" % (procs[-1][0], timeout)
            if self.style.animate:
                self._line("  %s  %s %s" % (flight_frame(frame, self.style.fancy),
                                            message, self.style.dim("%3.0f s" % elapsed)))
            frame += 1
            self.sleep(FRAME_S)

    def _fail(self, title: str, why: str, started, logs) -> int:
        """End what this run started, say why, show the logs' last lines."""
        for _name, proc in reversed(started):
            self.terminate(proc)
        self.say("%s %s did not land: %s." % (self.style.icon("fail"), title, why))
        if started:
            self.say("   What this run started was stopped.")
        for log in logs:
            tail = log_tail(log)
            if tail:
                self.say("   Last lines of %s:" % log)
                for row in tail:
                    self.say("     " + row)
        return EXIT_FAILED

    def start(self, point_id: str, interactive: bool = False) -> int:
        line = next((o for o in self.optional_lines() if o["key"] == point_id), None)
        if line is not None:
            return self.start_llama(line, interactive)
        try:
            plan = plan_point(self.stack, point_id, self.install, self.models)
        except SetupError as exc:
            self.say("%s %s" % (self.style.icon("fail"), exc))
            return EXIT_SETUP
        running = self.detect()
        if running:
            self.say(self.refusal(running))
            return EXIT_REFUSED
        for folder in plan["serve"]["dirs"]:
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                self.say("%s cannot create %s: %s" % (self.style.icon("fail"), folder, exc))
                return EXIT_SETUP
        missing = missing_files(plan, self.install, self.models)
        if missing:
            self.say("%s %s is not installed completely. Missing:"
                     % (self.style.icon("fail"), plan["title"]))
            for path in missing[:5]:
                self.say("     %s" % path)
            if len(missing) > 5:
                self.say("     ... and %d more" % (len(missing) - 5))
            return EXIT_SETUP

        self.say("%s Starting %s -- %s" % (self.style.icon("start"),
                                           self.style.bold(plan["title"]), plan["line"]))
        env = {k: v for k, v in os.environ.items() if k not in engine_env_keys(self.stack)}
        env.update(plan["serve"]["env"])
        started = []
        logs = []
        began = self.clock()
        try:
            log, sink = self._open_log("serve-%d.log" % plan["serve"]["port"])
            logs.append(log)
            try:
                serve = self._spawn(plan["serve"]["argv"], cwd=plan["serve"]["cwd"],
                                    env=env, stdin=subprocess.DEVNULL,
                                    stdout=sink, stderr=subprocess.STDOUT)
            finally:
                sink.close()
            started.append(("serve", serve))
            why = self._wait([("serve", serve)], plan["serve"]["port"],
                             plan["serve"]["readiness"], plan["timeout"], False,
                             "Please wait while flying to the nest...")
            image = None
            if why is None and plan["image"]:
                work = crow_core.image_server_workdir()
                os.makedirs(work, exist_ok=True)
                log, sink = self._open_log("sd-server-%d.log" % plan["image"]["port"])
                logs.append(log)
                try:
                    image = self._spawn(plan["image"]["argv"], cwd=work,
                                        env=crow_core._image_server_env(),
                                        stdin=subprocess.DEVNULL,
                                        stdout=sink, stderr=subprocess.STDOUT)
                finally:
                    sink.close()
                started.append(("sd-server", image))
                why = self._wait([("serve", serve), ("sd-server", image)],
                                 plan["image"]["port"], plan["image"]["readiness"],
                                 IMAGE_TIMEOUT_S, True,
                                 "%s Fetching the paint brushes (Qwen-Image)..."
                                 % self.style.icon("brush"))
        except KeyboardInterrupt:
            why = "cancelled"
        except OSError as exc:
            why = "could not be started: %s" % exc
        if why is not None:
            return self._fail(plan["title"], why, started, logs)

        seconds = self.clock() - began
        pids = {"serve": serve.pid, "image": image.pid if image is not None else None}
        try:
            where = self.write_active(plan["id"], plan["base_url"], pids)
        except (OSError, ValueError) as exc:
            where = None
            self.say("%s the contract file could not be written: %s" % (self.style.icon("warn"), exc))
        model = self.model_path(plan["base_url"]) or ""
        self.landed(plan, seconds, model, pids, where, interactive)
        return EXIT_OK

    def start_llama(self, line: dict, interactive: bool = False) -> int:
        """Start one optional llama.cpp line the way crow_core.start_server does.

        The argv, the env overlay, the scope prefix, the slot folder and the
        two logs (`llama-server-<port>.out.log` / `.err.log`) are
        crow_core.start_server's; the spawn, the animation and the wait are this menu's
        (detached like serve, ready when `/props` answers 200). NO CONTRACT FILE:
        its `point` is one of the three baseline ids, and the window keeps
        today's behaviour for a llama.cpp server.
        """
        title = "%s (optional)" % line["title"]
        running = self.detect()
        if running:
            self.say(self.refusal(running))
            return EXIT_REFUSED
        try:
            argv = self.llama.command(line["key"])
        except Exception as exc:          # noqa: BLE001 - ServerBootError names what is missing
            self.say("%s %s cannot start: %s" % (self.style.icon("fail"), title, exc))
            return EXIT_SETUP
        if "--slot-save-path" in argv:
            try:
                os.makedirs(argv[argv.index("--slot-save-path") + 1], exist_ok=True)
            except (OSError, IndexError):
                pass                      # the server refuses it in its own words
        port = line["port"]
        env = dict(os.environ)
        env.update(self.llama.env(line["key"]) or {})
        self.say("%s Starting %s -- llama.cpp on port %d"
                 % (self.style.icon("start"), self.style.bold(title), port))
        logs, started = [], []
        began = self.clock()
        try:
            out_log, out_sink = self._open_log("llama-server-%d.out.log" % port)
            err_log, err_sink = self._open_log("llama-server-%d.err.log" % port)
            logs += [err_log, out_log]
            try:
                proc = self._spawn(crow_platform.server_scope_prefix() + argv, env=env,
                                   stdin=subprocess.DEVNULL, stdout=out_sink, stderr=err_sink)
            finally:
                out_sink.close()
                err_sink.close()
            started.append(("llama-server", proc))
            why = self._wait([("llama-server", proc)], port, {"path": "/props", "status": 200},
                             LLAMA_TIMEOUT_S, False, "Please wait while flying to the nest...")
        except KeyboardInterrupt:
            why = "cancelled"
        except OSError as exc:
            why = "could not be started: %s" % exc
        if why is not None:
            return self._fail(title, why, started, logs[:1])
        url = base_url_for(port)
        plan = {"title": title, "base_url": url, "identity": {}, "process": "llama-server"}
        self.landed(plan, self.clock() - began, self.model_path(url) or "",
                    {"serve": proc.pid, "image": None}, None, interactive)
        return EXIT_OK

    def landed(self, plan, seconds, model, pids, where, interactive) -> None:
        self.say("%s %s %s is ready at %s after %.0f s."
                 % (self.style.icon("landed"), self.style.paint("Landed!", "1;32"),
                    plan["title"], plan["base_url"], seconds))
        want = (plan.get("identity") or {}).get("endswith") or ""
        if model:
            self.say("   model: %s" % model)
            if want and not model.replace("\\", "/").lower().endswith(want.lower()):
                self.say("   %s expected a model ending in %s" % (self.style.icon("warn"), want))
        bits = ["%s pid %s" % (plan.get("process", "serve"), pids["serve"])]
        if pids.get("image"):
            bits.append("sd-server pid %s" % pids["image"])
        self.say("   %s" % ", ".join(bits))
        if where:
            self.say(self.style.dim("   contract file: %s" % where))
        if interactive:
            self.say('   Choose "Start Crow" to open the window.')
            self.countdown(LANDED_S, "Back to the menu")

    # -- stop --------------------------------------------------------------
    def stop(self) -> int:
        """End every model server, wait until they are gone, remove the file.

        crow_core.stop_servers' set -- serve, sd-server and llama-server -- and
        ONLY WHAT THE SCAN SEES. The contract file's pids name the point, but a
        pid outlives its process: after a crash Windows may hand it to any
        program, and killing it by the file alone would end that program.
        """
        doc = self.active() or {}
        point = doc.get("point")
        targets, llama_names = [], []
        for pid, line in self.scan() or []:
            kind = crow_platform.server_kind(line)
            if kind == crow_platform.KIND_LLAMA:
                targets.append(("llama", str(pid)))
                llama_names.append(os.path.basename(crow_core.served_model(line)) or "llama.cpp")
                continue
            role = "image" if kind == crow_platform.KIND_IMAGE else "serve"
            targets.append((role, str(pid)))
            if role == "serve" and not point:
                port = crow_platform.server_port(line) or crow_core.CROW_NEST_PORT
                point = self.point_for(base_url_for(port))
        seen = {p for _r, p in targets}
        strays = [str(p) for p in (doc.get("pids") or {}).values()
                  if p and str(p) not in seen and self.alive(p)]
        path = crow_core.active_point_path()
        if not targets:
            removed = self._remove(path)
            self.say("%s Nothing to stop: no operating point is running.%s"
                     % (self.style.icon("off"),
                        " (a stale contract file was removed)" if removed else ""))
            self._mention_strays(strays)
            return EXIT_OK
        if not point and llama_names:
            point_text = "the llama.cpp server (%s)" % ", ".join(llama_names)
        else:
            point_text = self.label(point) if point else "the running point"
        for _role, pid in targets:
            self.kill(pid)
        began = self.clock()
        deadline = began + STOP_WAIT_S
        left = [p for _r, p in targets]
        while left and self.clock() < deadline:
            left = [p for p in left if self.alive(p)]
            if not left:
                # GONE IS "TORN DOWN AND NO LONGER LISTED", NOT "HAS AN EXIT
                # CODE": self.alive is process_exists (the handle, measured
                # 1.61 s after the exit code on a 13 GB llama-server), and the
                # scan is asked once more. The next point must not start beside
                # a process that still holds the card.
                listed = {str(pid) for pid, _line in self.scan() or []}
                left = [p for _r, p in targets if p in listed]
            if left:
                self.sleep(0.25)
        took = self.clock() - began
        self._remove(path)
        names = {"serve": "serve", "image": "sd-server", "llama": "llama-server"}
        done = ", ".join("%s pid %s" % (names[r], p) for r, p in targets)
        if left:
            self.say("%s Asked %s to stop (%s), but pid %s is still there. End it by hand: %s"
                     % (self.style.icon("warn"), point_text,
                        done, ", ".join(left), " and ".join(kill_hint(p) for p in left)))
            return EXIT_FAILED
        self.say("%s Stopped %s (%s) in %.0f s."
                 % (self.style.icon("stop"), point_text, done, took))
        self._mention_strays(strays)
        return EXIT_OK

    def _mention_strays(self, strays) -> None:
        for pid in strays:
            self.say("   pid %s from the contract file is alive but is not a model server "
                     "process the scan can see, so it was left alone. If it is one: %s"
                     % (pid, kill_hint(pid)))

    def _remove(self, path: str) -> bool:
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    # -- start crow --------------------------------------------------------
    def gui_command(self, base_url: str) -> list:
        exe = sys.executable
        if crow_platform.IS_WINDOWS:
            quiet = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.isfile(quiet):
                exe = quiet
        return [exe, GUI_SCRIPT, "--base-url", base_url]

    def no_point_hint(self, running: "dict | None") -> str:
        lines = ["%s No operating point is running." % self.style.icon("nest"),
                 "   Start one of the three operating points first:"]
        titles = [(p.get("menu") or {}).get("title") or p.get("id") for p in self.stack["points"]]
        width = max(len(t) for t in titles)
        for title, p in zip(titles, self.stack["points"]):
            lines.append("     - %s  %s" % (title.ljust(width), (p.get("menu") or {}).get("line") or ""))
        if self.optional_lines():
            lines.append("   or one of the optional llama.cpp lines in the menu.")
        if running:
            lines.append("   (Running now: %s.)" % self.describe(running))
        return "\n".join(lines)

    def start_crow(self) -> int:
        """Open the window on what runs: a baseline point, or a llama-server.

        The hint only when no model server with an address runs at all (an
        sd-server alone, or a llama-server whose port cannot be read, has none).
        """
        running = self.detect()
        if not running or not running.get("base_url") or running["kind"] not in (
                crow_platform.KIND_CROW_NEST, crow_platform.KIND_LLAMA):
            self.say(self.no_point_hint(running))
            return EXIT_FAILED
        url = running["base_url"]
        env = dict(os.environ)
        if running["kind"] == crow_platform.KIND_LLAMA:
            what = self.describe(running)
        else:
            what = self.label(running.get("point"))
        if running["kind"] == crow_platform.KIND_CROW_NEST and running.get("point"):
            try:
                plan = plan_point(self.stack, running["point"], self.install, self.models)
                env.update(plan["crow_env"])
            except SetupError:
                pass
        try:
            proc = self._spawn(self.gui_command(url), cwd=os.getcwd(), env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.say("%s The window could not be started: %s" % (self.style.icon("fail"), exc))
            return EXIT_FAILED
        self.say("%s Crow is opening (pid %s), connected to %s at %s."
                 % (self.style.icon("crow"), proc.pid, what, url))
        return EXIT_OK

    # -- status ------------------------------------------------------------
    def status(self) -> int:
        running = self.detect()
        if not running:
            self.say("%s No operating point is running." % self.style.icon("off"))
            return EXIT_FAILED
        self.say("%s Running: %s" % (self.style.icon("on"), self.describe(running)))
        if running.get("base_url"):
            self.say("   base URL  %s" % running["base_url"])
            model = self.model_path(running["base_url"])
            if model:
                self.say("   model     %s" % model)
        if running.get("since"):
            self.say("   since     %s" % running["since"])
        self.say("   found by  %s" % running["source"])
        return EXIT_OK

    def status_line(self) -> str:
        running = self.detect()
        if not running:
            return "%s %s" % (self.style.icon("off"), self.style.dim("No operating point is running."))
        return "%s %s" % (self.style.icon("on"), self.style.paint(self.describe(running), "32"))

    # -- the menu ----------------------------------------------------------
    def entries(self) -> list:
        """(key, icon, title, line, action, optional) in menu order.

        The three baseline points from stack.json first; below them, under a
        heading row (key "", action None), the installed optional llama.cpp
        lines, each tagged "(optional)" and drawn dimmed.
        """
        rows = [("1", "start", "Start Crow", "open the window on the running point",
                 ("crow", None), False)]
        for p in self.stack["points"]:
            menu = p.get("menu") or {}
            icon = p.get("id") if p.get("id") in ICONS else "start"
            rows.append((str(len(rows) + 1), icon, menu.get("title") or p.get("id"),
                         menu.get("line") or "", ("point", p.get("id")), False))
        n = len(rows)
        optional = self.optional_lines()
        if optional:
            rows.append(("", None, "Optional (llama.cpp)", "", None, True))
        for line in optional:
            n += 1
            rows.append((str(n), "optional", "%s (optional)" % line["title"],
                         "llama.cpp, port %d" % line["port"], ("point", line["key"]), True))
        rows.append((str(n + 1), "stop", "Stop the running point", "", ("stop", None), False))
        rows.append(("0", "quit", "Quit", "", ("quit", None), False))
        return rows

    def draw(self) -> None:
        if self.style.animate:
            self.out.write("\x1b[2J\x1b[H")
        s = self.style
        self.say()
        self.say("  %s  %s" % (s.icon("crow"), s.bold("C R O W") + s.dim("  boot menu")))
        self.say("  " + s.dim("-" * 52))
        self.say("  " + self.status_line())
        self.say()
        rows = self.entries()
        width = max(len(r[2]) for r in rows if r[0])
        after_optional = False
        for key, icon, title, line, action, optional in rows:
            if action is None:                      # the "Optional (llama.cpp)" heading
                self.say()
                self.say("  " + s.dim(title))
            elif optional:
                self.say(("   %s  %s %s" % (s.dim(key), s.icon(icon),
                                            s.dim("%s  %s" % (title.ljust(width), line)))).rstrip())
            else:
                if after_optional:
                    self.say()
                self.say(("   %s  %s %s  %s" % (s.bold(key), s.icon(icon), title.ljust(width),
                                               s.dim(line))).rstrip())
            after_optional = optional
        self.say()
        if self._note:
            self.say("  " + s.paint(self._note, "33"))
            self._note = ""

    def menu(self) -> int:
        while True:
            self.draw()
            try:
                # lstrip the BOM: PowerShell prefixes one to text it pipes in.
                choice = self.read("  Choose: ").strip().lstrip("﻿").lower()
            except (EOFError, KeyboardInterrupt):
                self.say()
                return EXIT_OK
            action = next((r[4] for r in self.entries() if r[0] and r[0] == choice), None)
            if choice in ("q", "quit", "exit"):
                action = ("quit", None)
            if action is None:
                self._note = "Unknown choice %r -- type one of the numbers." % choice
                continue
            what, arg = action
            self.say()
            if what == "quit":
                self.say("  %s Bye." % self.style.icon("quit"))
                return EXIT_OK
            if what == "crow":
                if self.start_crow() == EXIT_OK:
                    self.countdown(OPENED_S, "Back to the menu")
                else:
                    self.pause()
            elif what == "point":
                if self.start(arg, interactive=True) != EXIT_OK:
                    self.pause()
            elif what == "stop":
                self.stop()
                self.pause()

    # -- the shortcut ------------------------------------------------------
    def shortcut_spec(self, folder: str, which=shutil.which) -> dict:
        python = sys.executable
        if crow_platform.IS_WINDOWS and os.path.basename(python).lower() == "pythonw.exe":
            console = os.path.join(os.path.dirname(python), "python.exe")
            if os.path.isfile(console):
                python = console
        script = os.path.abspath(__file__)
        tail = [_quote(python), _quote(script)] + [_quote(a) for a in self.forwarded_args]
        terminal = which("wt") or which("wt.exe")
        if terminal:
            target = terminal
            args = " ".join(["--title", "Crow", "-d", _quote(self.install)] + tail)
        else:
            target = python
            args = " ".join(tail[1:])
        return {"path": os.path.join(os.path.abspath(folder), "Crow.lnk"),
                "target": target, "arguments": args, "workdir": self.install,
                "icon": ICON_FILE if os.path.isfile(ICON_FILE) else "",
                "description": "Crow boot menu: start an operating point, then Crow"}

    def create_shortcut(self, folder: str, run=subprocess.run, which=shutil.which) -> int:
        if not crow_platform.IS_WINDOWS:
            self.say("%s --create-shortcut writes a Windows .lnk; on this platform start "
                     "the menu with: python %s" % (self.style.icon("fail"), os.path.abspath(__file__)))
            return EXIT_SETUP
        if not os.path.isdir(folder):
            self.say("%s no such folder: %s" % (self.style.icon("fail"), folder))
            return EXIT_SETUP
        spec = self.shortcut_spec(folder, which)
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CROW_LNK_PATH); "
                  "$s.TargetPath = $env:CROW_LNK_TARGET; $s.Arguments = $env:CROW_LNK_ARGS; "
                  "$s.WorkingDirectory = $env:CROW_LNK_DIR; $s.Description = $env:CROW_LNK_DESC; "
                  "if ($env:CROW_LNK_ICON) { $s.IconLocation = $env:CROW_LNK_ICON + ',0' }; "
                  "$s.Save()")
        env = dict(os.environ, CROW_LNK_PATH=spec["path"], CROW_LNK_TARGET=spec["target"],
                   CROW_LNK_ARGS=spec["arguments"], CROW_LNK_DIR=spec["workdir"],
                   CROW_LNK_DESC=spec["description"], CROW_LNK_ICON=spec["icon"])
        done = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", script], env=env, capture_output=True, text=True,
                   stdin=subprocess.DEVNULL, timeout=60)
        if done.returncode != 0 or not os.path.isfile(spec["path"]):
            self.say("%s the shortcut could not be written: %s"
                     % (self.style.icon("fail"), (done.stderr or done.stdout or "").strip()))
            return EXIT_FAILED
        self.say("%s Shortcut written: %s" % (self.style.icon("crow"), spec["path"]))
        self.say("   target: %s %s" % (spec["target"], spec["arguments"]))
        return EXIT_OK


# ------------------------------------------------------------------- main ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Crow's boot menu: start an operating point, then Crow.")
    ap.add_argument("--install-root", help="Crow's install root (default: the installer's)")
    ap.add_argument("--models", help="the models root (default: $CROW_MODELS, else <install>/models)")
    ap.add_argument("--stack", help="manifests/stack.json (default: beside cli/)")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="say what runs (exit 0) or not (exit 1)")
    group.add_argument("--start", metavar="POINT", help="start one operating point")
    group.add_argument("--stop", action="store_true", help="stop the running operating point")
    group.add_argument("--start-crow", action="store_true", help="open the window on the running point")
    group.add_argument("--create-shortcut", metavar="DIR", help="write a Windows shortcut to this menu")
    args = ap.parse_args(argv)

    style = Style.detect(sys.stdout)
    install = os.path.abspath(args.install_root) if args.install_root else crow_platform.install_dir()
    models = os.path.abspath(args.models) if args.models else crow_platform.models_dir(install)
    if args.models:
        # One models root for everything this run starts: the optional llama.cpp
        # lines resolve through crow_platform.models_dir, which reads it here.
        os.environ["CROW_MODELS"] = models
    forwarded = []
    for flag, value in (("--install-root", args.install_root and install),
                        ("--models", args.models and models),
                        ("--stack", args.stack and os.path.abspath(args.stack))):
        if value:
            forwarded += [flag, value]
    try:
        stack = load_stack(os.path.abspath(args.stack) if args.stack else DEFAULT_STACK)
    except SetupError as exc:
        print("%s %s" % (style.icon("fail"), exc))
        return EXIT_SETUP
    boot = Boot(stack, install, models, style=style, forwarded_args=forwarded)
    if args.status:
        return boot.status()
    if args.start:
        return boot.start(args.start)
    if args.stop:
        return boot.stop()
    if args.start_crow:
        return boot.start_crow()
    if args.create_shortcut:
        return boot.create_shortcut(args.create_shortcut)
    return boot.menu()


if __name__ == "__main__":
    sys.exit(main())
