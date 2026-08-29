#!/usr/bin/env python3
"""Crow's window. The surface is the mockup, rendered by a webview.

WHY NOT TKINTER, and this is the decision E9 of #90 asked for and never got.
The design for this window exists as an HTML page -- rounded corners, a ring
around the input, pill-shaped status chips, a folding thought block. tkinter can
draw none of it: every widget is a rectangle whose only edge is
`highlightthickness`, one flat colour, no radius and no shadow. A tkinter build
was made first and rejected on sight, five times, and each rejection was right.

WHAT THE PRODUCTS THIS IS MODELLED ON DO. OpenAI's Codex desktop app is Electron;
the community clients around it are Tauri. Both are the same answer: HTML and CSS
rendered inside an application window. pywebview is that answer at 2 MB instead of
Electron's 150 -- on Windows it hosts the system's own WebView2, which is already
on the machine.

WHAT IS IN HERE AND WHAT IS NOT. Every DECISION belongs to cli/crow_core.py --
the conversation, the request body, the SSE read, the tool loop, the cost line,
where a thought block begins. This file owns pixels and nothing else. The core
speaks through `TurnEvents`/`ReplyEvents`; each callback becomes one JSON message
handed to the page. tools/check_shared_core.py holds that split against
manifests/shared-core.json.

TOOL CALLS RUN, AS THEY DO IN THE TERMINAL. The window starts with
`execute_tools=True` and carries the switch as the tools chip: one click, and it
only shows them instead. It matches `cli/crow.py`, which runs them unless given
`--no-run-tools`; a window that showed them instead would answer the same
question differently from the terminal, and that difference is what #90 exists
to exclude. Shown-only remains one click away, and the chip names the mode in
both states, so the answer is never silent.

The permission question is NOT answered here: #88 (`/mode manual, allowedit,
auto`) is what binds intent to permission, and it binds both clients or neither.
`run_command` still starts a shell in either one.

    python cli/crow_gui.py

Needs pywebview (`pip install pywebview`). That is a dependency the terminal
client does not have, and it is the price of the design: it is written down here
rather than discovered at import time.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import re
import sys
import struct
import subprocess
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import crow_core  # noqa: E402
import crow_voice  # noqa: E402

from crow_core import (  # noqa: E402
    BANNER_BEVEL_HEX,
    CodeFences,
    Conversation,
    CROW_ACCENT_HEX,
    CROW_BG,
    CROW_TEXT_HEX,
    CrowError,
    DEFAULT_BASE_URL,
    DEFAULT_MODE,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM,
    check_endpoint,
    fetch_model_name,
    fetch_n_ctx,
    FenceEvents,
    INTERRUPT,
    load_session,
    model_display_name,
    ReplyEvents,
    run_turn,
    sampling_for,
    save_session,
    SESSION_FILE,
    TOOLS,
    TurnEvents,
)

_VERSION_LITERAL = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.M)

# The window ships a read timeout where the terminal runs without one. Measured
# 2026-08-13: a recv that is ALREADY blocked is not woken by closing the socket
# from another thread, so the only bound on it is the timeout it started with.
#
# 600, AND IT WAS 20 UNTIL 2026-08-14. This is a per-read bound, not a per-turn
# one: it starts again on every chunk, so it only ever expires on a wait with no
# bytes in it -- a prefill. 20 s was therefore under the prefill of any turn
# whose history had grown, and a live turn hit it: 12 rounds, 13 tool calls,
# prefill 2,222 tokens at 51.21 tok/s (about 43 s of silence) -> `stream broke:
# timed out`, with the answer and the round lost.
#
# The floor this has to clear is the worst prefill on record, 469.51 s to the
# first token on a resumed 21k session (2026-08-10). tools/measure_gui_stream.py
# encodes that floor at :636 as `> 469.51`, README.md has said 600 since 0.3.0,
# and the docstring at measure_gui_stream.py:106 says 600 -- the shipped 20 was
# the only place that disagreed, and no check reads this constant.
READ_TIMEOUT_S = 600.0

# WINDOW SETTINGS, AND THEY ARE THE WINDOW'S ALONE. The terminal client has no
# theme to pick, so this does not belong in the core -- check_shared_core would
# be right to call a copy of it there a second decision. It sits beside
# roots.json for the same reason roots.json sits there: it is remembered ACROSS
# chats, so it cannot live in a chat file.
SETTINGS_FILE = os.path.join(os.path.dirname(crow_core.SESSION_DIR), "settings.json")
THEMES = ("dark", "light", "crow")
DEFAULT_THEME = "dark"

# THE GROUND PYWEBVIEW PAINTS BEFORE THE PAGE EXISTS, and it has to be the same
# one the page will paint a moment later. A window filled with one ground and
# repainted in another is a flash of the wrong product on every start -- the
# same failure the data-theme attribute exists to avoid, one layer further out.
#
# CROW_BG IS A VALUE HERE, NOT A FALLBACK. `crow` is the theme the window shipped
# with, so the brand ground is what that theme paints; the dict's default only
# catches a name no build has.
THEME_BG = {"dark": "#181818", "light": "#ffffff", "crow": CROW_BG}

# PASTED PICTURES LAND OUTSIDE THE WORKING DIRECTORY, ON PURPOSE. A screenshot
# is not part of the project it is about, and writing one into whatever folder
# happens to be bound would put Crow's own files in a user's repository. Beside
# roots.json is where things that outlive a chat already live.
#
# READING IS NOT BOUNDED -- the guard in the core covers `write_file` and
# `edit_file` -- so a path here is one the model's own tools can still open.
PASTE_DIR = os.path.join(os.path.dirname(crow_core.SESSION_DIR), "pastes")

# 20 MB. A screenshot is under one; anything past this is a paste nobody meant.
PASTE_MAX_BYTES = 20 * 1024 * 1024

# NOTHING IS EVER DELETED HERE, and that is a decision rather than an
# oversight. A 30-day sweep was built on 2026-08-21 and switched off again
# the same day: the PATH of a pasted picture lives in the conversation, so
# deleting the file breaks what an old chat points at, and a chat that
# silently stops working is worse than a folder that grows. The machinery
# is in commit 81a913d if it is ever wanted back.

# CF_DIB. The number is Windows', not ours.
CF_DIB = 8


def dib_to_bmp(dib: bytes) -> bytes:
    """A clipboard DIB wrapped in the 14 bytes that make it a .bmp file.

    THE CLIPBOARD STORES A DIB WITHOUT ITS FILE HEADER, because inside Windows
    nothing needs one. Prepending it is the whole conversion -- no pixels move.

    THE OFFSET IS THE PART THAT CAN BE WRONG, and it is arithmetic rather than a
    constant: the pixels begin after the header AND after whatever sits between,
    which is a palette below 8 bits and three colour masks when the compression
    is BI_BITFIELDS. Get it wrong and the file opens as a picture of noise, which
    is worse than failing.
    """
    if len(dib) < 40:
        return b""
    (size, _w, _h, _planes, bits, comp,
     _sizeimage, _xppm, _yppm, used, _important) = struct.unpack_from("<IiiHHIIiiII", dib, 0)
    palette = (used * 4) if used else ((1 << bits) * 4 if bits <= 8 else 0)
    if comp == 3:                                     # BI_BITFIELDS
        palette += 12
    offset = 14 + size + palette
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib


def clipboard_image() -> "tuple[str, bytes] | None":
    """`(suffix, bytes)` for a picture on the Windows clipboard, else None.

    READ HERE AND NOT IN THE PAGE, and that is measured rather than preferred.
    On 2026-08-21 a Windows screenshot sat on the clipboard as `PNG` and `Bitmap`
    -- 296,135 bytes of valid PNG, pulled by exactly this code -- and the page's
    own `paste` event handed over no image item at all. The bytes were there the
    whole time; what was missing was a reader that WebView2 did not have to
    volunteer.

    NO NEW DEPENDENCY. This is ctypes against user32 and kernel32, both of which
    are already on every machine that can run the window.
    """
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # EVERY SIGNATURE IS DECLARED. A handle is 64 bits and ctypes defaults to a
    # C int, so an undeclared GlobalUnlock raises "int too long to convert" on
    # the one call that releases the memory -- caught the first time this ran.
    u32.OpenClipboard.restype, u32.OpenClipboard.argtypes = wintypes.BOOL, [wintypes.HWND]
    u32.CloseClipboard.restype = wintypes.BOOL
    u32.RegisterClipboardFormatW.restype = wintypes.UINT
    u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    u32.GetClipboardData.restype, u32.GetClipboardData.argtypes = wintypes.HANDLE, [wintypes.UINT]
    k32.GlobalLock.restype, k32.GlobalLock.argtypes = ctypes.c_void_p, [wintypes.HANDLE]
    k32.GlobalSize.restype, k32.GlobalSize.argtypes = ctypes.c_size_t, [wintypes.HANDLE]
    k32.GlobalUnlock.restype, k32.GlobalUnlock.argtypes = wintypes.BOOL, [wintypes.HANDLE]

    def grab(fmt: int) -> bytes:
        if not fmt or not u32.IsClipboardFormatAvailable(fmt):
            return b""
        handle = u32.GetClipboardData(fmt)
        if not handle:
            return b""
        pointer = k32.GlobalLock(handle)
        if not pointer:
            return b""
        try:
            return ctypes.string_at(pointer, k32.GlobalSize(handle))
        finally:
            k32.GlobalUnlock(handle)

    if not u32.OpenClipboard(None):
        return None
    try:
        # PNG FIRST, because the screenshot tool already registered one and
        # anything this module built out of a DIB would be a second-hand copy.
        png = grab(u32.RegisterClipboardFormatW("PNG"))
        if png[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png", png
        bmp = dib_to_bmp(grab(CF_DIB))
        return (".bmp", bmp) if bmp else None
    except Exception:                  # noqa: BLE001 - reported as None
        return None
    finally:
        u32.CloseClipboard()


def theme_bg(name: str) -> str:
    """The window's ground for a theme, and the brand's for anything else."""
    return THEME_BG.get(name, CROW_BG)


def read_settings() -> dict:
    """The settings document, or an empty one. Never raises."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_settings(doc: dict) -> bool:
    """Write it back. False when the disk said no, and the caller says so."""
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        return True
    except OSError:
        return False


def write_paste(suffix: str, raw: bytes) -> str:
    """Put `raw` in PASTE_DIR under a name nothing else has, and return it.

    "" when it did not land. The caller says nothing rather than inventing a
    reason: a paste that fails on a full disk and a paste of an empty clipboard
    look the same from the box, and neither is worth a sentence.
    """
    if not raw or len(raw) > PASTE_MAX_BYTES:
        return ""
    try:
        os.makedirs(PASTE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # THE SECOND PASTE IN THE SAME SECOND IS NOT A RARE CASE when the
        # clipboard is a keyboard shortcut. The counter is what keeps the first
        # one from disappearing under the second.
        for n in range(1, 1000):
            name = "paste-%s%s%s" % (stamp, "" if n == 1 else "-%d" % n, suffix)
            path = os.path.join(PASTE_DIR, name)
            if not os.path.exists(path):
                break
        else:
            return ""
        with open(path, "wb") as fh:
            fh.write(raw)
    except OSError:
        return ""
    return path


def current_theme() -> str:
    """The remembered theme, and DEFAULT_THEME for anything else.

    A file holding "solarized" is not an error worth a message: it is a value
    this build does not have, and the answer to that is the one it does have.
    """
    name = read_settings().get("theme")
    return name if name in THEMES else DEFAULT_THEME


# #119. THE EMPTY CHAT SAYS SOMETHING. Four groups because the hour is the only
# thing here that is actually known -- the greeting is not a mood, it is a clock
# reading with a name attached, and a line that says "good morning" at eight in
# the evening is worse than no line.
#
# ENGLISH, LIKE EVERY OTHER LINE THIS CLIENT SPEAKS. These four groups were
# written in German on 2026-08-21 and robin caught it the same evening: Crow has
# no localisation at all -- `grep -c "locale\|gettext" cli/*.py` answers 0 three
# times -- so a German line is not "the German version", it is the ONLY version,
# shown to every user of a repository whose README, issues and every other
# message are English.
#
# THE MODEL'S ANSWERS ARE A DIFFERENT QUESTION and stay as they were:
# `DEFAULT_SYSTEM` tells it to reply in the language the user wrote in. What the
# CLIENT says about itself is not part of that conversation.
GREETINGS = {
    "morning": ("Good morning, %s.", "Morning, %s.", "Up early, %s?",
                "Morning, %s — where do we start?"),
    "day":     ("Hello, %s.", "What's on, %s?", "There you are, %s.",
                "What are we working on, %s?"),
    "evening": ("Good evening, %s.", "Evening, %s.", "Still up, %s?",
                "Winding down, or still something on, %s?"),
    "night":   ("Still at it, %s?", "Good night would be an option too, %s.",
                "Night shift, %s?", "Quiet here at this hour, %s."),
}


def user_first_name() -> str:
    """The person's first name, or "" when the machine will not say.

    `getpass.getuser()` RATHER THAN THE FULL DISPLAY NAME, and that is a limit
    rather than a preference: the display name lives behind `GetUserNameEx` on
    Windows and nowhere at all on the other two platforms, so it would be one
    more ctypes signature for a nicety. The login name is what every platform
    has, and on a personal machine it IS the first name.

    A LOGIN THAT IS NOT A NAME still comes back -- `svc-build` reads oddly and
    an empty greeting reads as a defect. The caller drops the name, not the line.
    """
    try:
        raw = getpass.getuser()
    except Exception:                      # noqa: BLE001 - a nicety, never fatal
        return ""
    # `DOMAIN\robin` and `robin@host` are both logins somebody really has -- and
    # the name is on OPPOSITE SIDES of the two separators. Taking the last part
    # of both turns `robin@rechner` into the machine's name.
    for sep in ("\\", "/"):
        raw = raw.rsplit(sep, 1)[-1]
    raw = raw.split("@", 1)[0].split(".")[0].strip()
    return raw[:1].upper() + raw[1:] if raw else ""


def daypart(hour: int) -> str:
    """Which of the four the clock is in. The boundaries are ordinary ones."""
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 18:
        return "day"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def greeting(now: float | None = None, name: str | None = None) -> str:
    """One line for an empty chat: the hour decides the group, the minute the line.

    NOT `random`. The line has to change -- robin asked for that in as many words
    -- but a random one cannot be held to anything: no case could say WHICH line
    a given moment produces, so the only testable claim left would be "it is one
    of four", which is not the behaviour. The minute is a clock somebody can set,
    so this is driven rather than sampled.

    WITHOUT A NAME IT IS STILL A GREETING. The `%s` is dropped rather than filled
    with a placeholder: "Hello, user." is worse than "Hello.".
    """
    stamp = time.localtime(now) if now is not None else time.localtime()
    lines = GREETINGS[daypart(stamp.tm_hour)]
    line = lines[int(now if now is not None else time.time()) // 60 % len(lines)]
    who = name if name is not None else user_first_name()
    if not who:
        # ", %s." and " %s?" and " %s —" all have to come off cleanly.
        return re.sub(r"[,\s—-]*%s", "", line).replace(" ?", "?") % ()
    return line % who


def rail_open() -> bool:
    """Is the chat rail unfolded? True for anything this build does not have.

    #119. THE DEFAULT IS OPEN, and it is the default for the absent key as well
    as for a broken one: a window that came up with no rail and no memory of why
    would look like the list had been lost, and the way back is a button that is
    also not there yet when someone first looks for it.
    """
    return read_settings().get("rail_open") is not False


def code_open() -> bool:
    """Is the code panel unfolded? False for anything this build does not have.

    THE OPPOSITE DEFAULT TO THE RAIL, and the difference is what each one holds.
    The rail carries the chats, which exist before the first turn. This carries
    what a tool is writing, which does not -- so a pane that opens on every start
    would take 280 px to show an empty box. The button in the title bar is what
    makes it findable instead.
    """
    return read_settings().get("code_open") is True


def open_projects() -> dict:
    """Which project rows are unfolded, by path. Absent means unfolded.

    A DICT AND NOT A LIST OF THE CLOSED ONES, because a project that is removed
    and added again should come back the way every other new one does. Keyed by
    the path as it was written; the reader normalises, so a folder reached
    through a different spelling is still the same row.
    """
    doc = read_settings().get("projects_shut")
    if not isinstance(doc, list):
        return {}
    return {os.path.normcase(p): False for p in doc if isinstance(p, str)}


# WIE SCHMAL UND WIE BREIT DIE RAIL WERDEN DARF. Beides geklemmt, und beides
# auch in Python: der Wert kommt aus einer Maus, und eine Seite, die sich
# vertut, schriebe ihn sonst in die Einstellungen.
RAIL_MIN, RAIL_MAX, RAIL_DEFAULT = 180, 520, 242


def rail_width_setting() -> int:
    """Die gespeicherte Rail-Breite, oder die Vorgabe.

    Ein Wert ausserhalb der Grenzen wird zur Vorgabe und nicht zur Grenze: er
    stammt dann nicht aus dieser Geste, und ihn auf 520 zu ziehen waere eine
    Entscheidung, die niemand getroffen hat.
    """
    value = read_settings().get("rail_width")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return RAIL_DEFAULT
    return int(value) if RAIL_MIN <= value <= RAIL_MAX else RAIL_DEFAULT


# DIE GRENZEN DES CODE-PANELS, gespiegelt zur Rail und aus demselben Grund in
# Python geklemmt: der Wert kommt aus einer Maus.
#
# DIE VORGABE IST DAS MINIMUM, robin am 2026-08-27: "der codepanel ist viel zu
# breit. Default werte bitte wie in Screenshot 2" -- und sein gespeicherter
# Zug stand auf exakt 260. Der Vorgaenger (380, dann #138c "halbe Flaeche")
# entschied die Startbreite fuer ihn und lag zweimal daneben; wer mehr Panel
# will, zieht den Griff, und DIESE Entscheidung bleibt gespeichert.
# CODE_MAX war 720; robin am 2026-08-27: "Codepanel maximale Breite -15%" --
# 720 * 0,85 = 612. Die Seite klemmt dieselbe Geste ein zweites Mal, siehe
# codeDrag; beide Kopien tragen denselben Wert.
CODE_MIN, CODE_MAX, CODE_DEFAULT = 260, 612, 260

# WIEVIEL EINER ANTWORT DAS PANEL ZEIGT. Der Kern reicht sie ganz herueber --
# `tool_result` sagt im eigenen Docstring, dass die Menge eine Entscheidung des
# Bildschirms ist -- und HIER ist dieser Bildschirm. Ein `read_file` ueber eine
# grosse Datei oder ein `fetch_url` auf eine lange Seite legte sonst Megabyte in
# eine Seite, die den ganzen Verlauf einer Sitzung haelt.
#
# GILT JEDEM WERKZEUG GLEICH. Kein Name steht in dieser Regel, so wie kein
# Servername im MCP-Filter steht.
TOOL_RESULT_SHOWN = 4000

# WELCHE WERKZEUGE PROGRAMMCODE SCHREIBEN, und damit die einzigen, die im
# oberen Abschnitt des Panels landen. `read_file` steht bewusst nicht hier:
# Crow liest viel, was es nicht aendert, und ein Abschnitt "Programmcode" voller
# fremder Dateien beantwortet die Frage nicht mehr, fuer die er da ist --
# was hat dieser Zug an meinem Programm geaendert.
CODE_TOOLS = ("write_file", "edit_file")


def code_width_setting() -> int:
    """Die gespeicherte Breite des Code-Panels, oder die Vorgabe.

    Dieselbe Regel wie bei der Rail: ein Wert ausserhalb der Grenzen wird zur
    Vorgabe und nicht zur Grenze, weil er dann nicht aus dieser Geste stammt.
    """
    value = read_settings().get("code_width")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return CODE_DEFAULT
    return int(value) if CODE_MIN <= value <= CODE_MAX else CODE_DEFAULT


def client_version(path: str | None = None) -> str:
    """The client version, read out of cli/crow.py. "" when unreadable."""
    try:
        with open(path or os.path.join(HERE, "crow.py"), encoding="utf-8") as fh:
            found = _VERSION_LITERAL.search(fh.read())
    except OSError:
        return ""
    return found.group(1) if found else ""


# THE MEMORY MARK, BAKED IN. `docs/` is not in the package: an installed
# Crow has cli, bin, models and templates and no docs directory, so a path
# into it would render here and nowhere else. 48x48 out of the 512x512
# original, 1,774 bytes -- the row draws it at 17px.
MEMORY_ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAaDSURBVGhDvZp16G1FEMe/dgv2E7vF9tkFJqKCDSoGttgYYDysvyzs7lawO7DF7u7Ebn3YzecyK/Mbds/Zc+/9vS8s996dObs7e2an9kqDYXFJF0r6QtK/kn6WdK+kzSKj9d1nPPB+LukCSYtFxgmFnW0htCclXSbpTkl/Wt/ljvcK6/tD0h3G+5R7fkfHO0GwgU38iqRlA20WSecb/WRJp9r3cyXNHHjHSnrV6OsH2qhgaUk7mcp8JWmmyOBwtdvhKyPRAaG+NpVi7KUiwzCwpKQH3IJou0WmgLlMnVCbOV3/1JJWkDSb69sjjH2/pCUcfSCsIuk3G/gUSet02KV3Jb3tfnPo37OxxkvayNEYc11Jpxn9F0krOXpfmF7SZ5J+sl3ritdMxxPukfS3pAMkfSjpS0mTO3rCyibAJ5Kmi8QuYCJ2Y6tIqMTrJoT/zaJZ1N029jSO7rGt0feLhC54QtK3kiaKhEpEATikLOp3+8RKlTCxpO8lPRoJXcArfDx2dkAUAHD4Wfw5oT8H/MtHsbMWC5v+swh2I2JWSXtLOk/SepFo4Fl/BsBqJsB2oT+COd+wTWQt1cDKIHkyab9KmiHwbCzpR8dzeKAnPGPqgiCpYVppi0TmgBmd9aOhzmtFpohkj7HdeNCtbcf8GcCZwcPbQRA8bwlJAN4CqkQjVmKT2sCczL2NmdYUohT9D6YLhhckzR2JDsQymMKa15pToX7Bml60Na4YieAx2605IsEBL8oAeOUaIADxUhvwN0dIOlvSqpHogEdHOx6JBBbNwi6KhICpJL0p6dBIKACVgR99LjWMwcNO13m7TUJcanyz+86kPrv6zgImjR0N8CFzWzvEhOH7JXEgh92NZ4QaLWedtTtbi+cl/SDp4oaWcgXCDNSI70fHgRywePAskzo47RxMOnHfwwR2/OXYmUEKWWicrykig8P2xndb6kgxx7Ej+arA4WsCAtQcYsB5mN++LyDpFjPDObN5vK0ZE6unJX0nabLI1YIj7cCxQPxHDtDetzPW1Aib8S+E2xzgDyT9Y4EfC90ijEsEi2ricPWXpBsCQw1wRmRdODQmwTpEeI/ete1pY/D9qjAuuNmcW4+h6dS3gd0gHmKcXQLtJROQN1Tb9rH8g7eQQu69wriAwgC03gTD8JafSnorhB21hziCogGqRyx0esF042MI9HoxOZKsGTkcJrG8eMFIcLjJxvEZVL8CAKLRaWOnYW2b6yR+4DyQhMpALgbCbJH+8QCHfcrIYGDQcSZsAipEtQF9zjVUg5y7C+axtbLmXiCJ9eHVE2PEHT7QFv6xpGMkbdgxO6s5xFQgumAhO7yEKD3V2t8G2iEwIgz9xOGlV9mGZEZx+7FhOvn05ZVaUMljbfvyg9dMoSoCOw/T8pHQAV0c2XyZKl8JaAE+gtC/5weuixwuRul390HtIT7IqdRDmQwwhxu9HyCoiljUdH4QpGAOJ4fdjo1+nBRruMsFapy3NqSwWt+Yno8GuoTTh0kaY99rHCshEPXZXuGVh6hlDhspoUElSg1T+KATBJVuM62Ye3h5i70SNz9ujVwF4AfwDZu3VKdBbU6M88OHnGHBXRtutzX/nxOcYB1cSrQdIM5G2i1uW1LQlUOtALVgbcm4HBeJZxkBrzsi3wzAhBFWkEdgInmGNC8HXxfCItFIWEqFsCaQt1OlY74zIzHhKGOgzF0D1IkdppJMQhLh60Kp8Rs9bytsRXCDw9pIO4vATcN0YiQ0gOIXz2wZCQUVImGBv620GEHgxnNka43Au7GjmLQacMtIZpbLp3PF3VT9486sFqg0Jc7nIiGH1W0C7Gy8lCsBdchdVEQB0s0mi+GTG582YGafNf6metEIkBXxAI4CK0Oc0g/iDQ0CEXdxqZGyrVKoQoJPuE3oDF8uK2vEpu7U0yj7dQXZXi9rMrBokvWDbWyEyb057g7SvFjFTSJDLajPIAi7SL7Q5iM8MLVpEek2ns93rI/yfC7WwjkSpGGiWXhOwM6gAsGk5Ke1uN4JcK3rp75KJbBUlsfG88wakTAokvvmILaBEiW8ePhUhKL22QZqs1HgoQH1IbZnAnZp3shgl9PXGA+pYko/qXvSRx2J4lUEY3HG4CENLd1cDgyE8KqBqSWeZ8ewOKmf/0p43SV3JVBLdHh5hmfx1qmf6HLUFu/BmWByakFMjCOjEMXfZ5qiSXSfmIsbfMIJnsMacS9RbeM9/gMPTRevSt+HdgAAAABJRU5ErkJggg=="


# ---------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="de" data-theme="__THEME__"><head><meta charset="utf-8">
<style>
:root{
  --accent:__ACCENT__; --bevel:__BEVEL__; --model:__TEXT__;

  /* THE DARK GROUND IS NEUTRAL, and that is robin's call on 2026-08-21: the
     window was the wordmark's own ground, and next to the reference he gave it
     read as a colour rather than as a background. The brand value did not
     change and it did not go away -- it is the third theme below. The hex is
     NOT written here: it comes from the core, and the manifest counts copies.

     EVERY COLOUR HAS A NAME, and that is what makes a second theme possible at
     all: a rule can only be switched if what it points at can be redefined.
     Before this, 52 lines of CSS carried a literal and no palette could reach
     any of them. A test holds that: no rule may name a colour of its own. */
  --bg:#181818; --rail:#141414; --panel:#1f1f1f; --raised:#2a2a2b;
  --line:#2e2e30; --line-soft:#242426;
  --dim:#a3a3a6; --dimmer:#6f6f73;
  --ok:#4ec98f; --warn:#e3b341; --bad:#f0655a;
  --gold:#e5c04b; --bad-text:#ffd9d4;
  /* #143. Delegation is its own channel and wears the logo cyan -- cards, rail
     children and the chip all draw from this ONE name. Written once, in the
     base palette: the themed roots inherit it, so every theme carries it. */
  --sub:#39c6d8;
  --mark:var(--text-hi); --mark-o:var(--text-hi);
  --text:#ffffff; --text-strong:#ffffff; --text-soft:#c9c9cc;
  --text-faint:#9a9a9e; --text-hi:#ffffff; --text-hover:#ffffff;
  --think:#8e8e93; --think-bg:rgba(255,255,255,.045);
  --code:#d4d4d8; --code-bg:#111111;
  --hover:#2a2a2b; --close-bg:#c0362b; --on-solid:#ffffff;
  --shadow:rgba(0,0,0,.5); --shadow-strong:rgba(0,0,0,.7);
  /* THE SYSTEM'S OWN, NOT OURS. The page used to name a shipped typeface first
     and fall back; it still ships in cli/fonts for anyone who wants it, but a
     window that looks different depending on whether the user installed a font
     has two appearances and no way to say which is the real one. system-ui IS
     whatever this machine calls its interface font.
     TWO STACKS, because "system default" means different things for prose and
     for code: Windows' interface font is proportional, and a diff or a code
     fence set in it stops lining up. The monospace stack is the system's as
     well -- Cascadia and Consolas ship with Windows, neither is ours. */
  --ui:system-ui,"Segoe UI",-apple-system,Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"Cascadia Mono",Consolas,"Courier New",monospace;
  /* The height of the two bars across the top, ONCE. The rail's head and the status bar sit side
     by side and were 3 px apart: 11+9 padding against 8+8, plus a button that builds 0.8 px taller
     than a chip. Matching them by arithmetic works until a font changes; matching them against one
     number cannot drift. Both take it as min-height, so the status bar may still grow when its
     chips wrap -- and then the rail head is SUPPOSED to stay put. */
  --barh:41px;
  /* The scrollbar's width, ONCE. #flow reserves it, the composer stops short of
     it, and .turn centres inside it -- three rules that have to agree or the
     input box sits 5 px off the text above it. */
  --sbw:10px;
}

/* -- light ------------------------------------------------------------- */
/* THE SAME NAMES, OTHER VALUES. Nothing below this line knows a theme exists;
   it is one attribute on <html>, written by Python before the page is handed
   over so there is no flash of the wrong one on start.
   --bg, --accent, --bevel and --model arrive from the core as the brand values
   and are REDEFINED here rather than written twice: the model's own text colour
   is a dark-on-dark choice, and on white it would be a paragraph nobody can
   read. The accent survives both grounds and stays what the core says. */
:root[data-theme="light"]{
  --bevel:#d3d7dd; --model:#1a1c1f;
  /* THE RAIL IS NOT THE PAGE. On the first build both were #ffffff and the chat
     list dissolved into the conversation beside it -- the reference robin gave
     sets its sidebar off against the content, and one border was not enough to
     do that on white. */
  --bg:#ffffff; --rail:#f7f7f8; --panel:#ffffff; --raised:#eeeef0;
  --line:#e4e4e7; --line-soft:#ededf0;
  --dim:#5b6472; --dimmer:#8b93a1;
  --ok:#12855a; --warn:#8a6400; --bad:#c0362b;
  --gold:#8a6400; --bad-text:#8c241b;
  /* #143. The delegation channel, darkened the way every accent is here:
     the logo cyan reads as haze on white. */
  --sub:#0e7a8a;
  --mark:var(--text-hi); --mark-o:var(--text-hi);
  --text:#1a1c1f; --text-strong:#0f1114; --text-soft:#3f4550;
  --text-faint:#6b7280; --text-hi:#0f1114; --text-hover:#1a1c1f;
  --think:#5b6472; --think-bg:rgba(26,28,31,.05);
  --code:#24292f; --code-bg:#f6f8fa;
  --hover:#ececed; --close-bg:#c0362b; --on-solid:#ffffff;
  --shadow:rgba(15,17,20,.14); --shadow-strong:rgba(15,17,20,.30);
}

/* -- crow ---------------------------------------------------------------- */
/* THE WINDOW AS IT SHIPPED, kept as a choice rather than as history. Every
   value below is the one that was in the file before the palette existed, so
   this theme is not a new design -- it is the old one, given a name.
   --bg is the ONLY colour here that is not written down: it is the wordmark's
   ground and it comes from the core, because the manifest counts every place a
   brand value is spelled out. */
:root[data-theme="crow"]{
  --bg:__BG__; --rail:#0e1220; --panel:#0e1220; --raised:#131829;
  --line:#1c2438; --line-soft:#161d2e;
  --dim:#6d7b95; --dimmer:#4a566d;
  --ok:#4ec98f; --warn:#e3b341; --bad:#f0655a;
  --gold:#e5c04b; --bad-text:#ffd9d4;
  /* #143. The logo cyan at home: this theme is the wordmark's own ground. */
  --sub:#39c6d8;
  --mark:var(--accent); --mark-o:var(--bevel);
  --text:#cfdaea; --text-strong:#e8eef8; --text-soft:#a9bad3;
  --text-faint:#9fb0c9; --text-hi:#ffffff; --text-hover:#c8d4e8;
  --think:#7b89a3; --think-bg:rgba(19,24,41,.4);
  --code:#c3d0e4; --code-bg:#080b13;
  --hover:#161d2e; --close-bg:#8b2b26; --on-solid:#ffffff;
  --shadow:rgba(0,0,0,.45); --shadow-strong:rgba(0,0,0,.75);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden}
body{background:var(--bg);color:var(--dim);font:13px/1.55 var(--ui);
  -webkit-font-smoothing:antialiased;display:flex;flex-direction:column;
  user-select:none}
::-webkit-scrollbar{width:var(--sbw)}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:99px}
::-webkit-scrollbar-thumb:hover{background:var(--bevel)}

/* -- title bar: ours, because the frame is off ------------------------- */
/* THE CAPTION DRAGS THE WINDOW, and the hook is pywebview's own class.
   `-webkit-app-region: drag` is Electron syntax; WebView2 ignores it, which
   is why the first frameless build could not be moved at all. */
/* #125. ONE SURFACE WITH THE RAIL, and therefore its colour and no rule under
   it. The ribbon, the rail head and the chat list are the same panel; the chat
   is the thing that sits ON it, which is what the rounded corner below says. */
#bar{display:flex;align-items:center;gap:10px;height:34px;flex:none;
  padding:0 0 0 13px;background:var(--rail)}
/* #119. THE WORDMARK IS A PALETTE ENTRY NOW. It was the accent in all three
   themes, which is Crow's own blue -- right on the dark blue ground it was
   drawn for, and a coloured word floating on a neutral or a white one.
   robin: white on dark, dark on light, unchanged in `crow`.
   TWO NAMES, because only `crow` splits the O off. Setting both to the same
   value in the other two is what makes them one solid word there rather than
   a word with a hole in it. */
#mark{font-weight:700;letter-spacing:.22em;font-size:11.5px;color:var(--mark);pointer-events:none}
#mark span{color:var(--mark-o)}
/* #138. DER FREIE PLATZ WIRD EINEN PLATZ FRUEHER VERTEILT. Der Code-Knopf
   gehoert nach robins Vorgabe links neben die Fensterknoepfe, also traegt ER
   das `auto` und `#wbtns` folgt ihm dicht. Zweimal `auto` nebeneinander waere
   falsch: Flex teilt den Rest dann gleichmaessig und schoebe den Knopf in die
   Mitte der Leiste. */
#codetoggle{margin-left:auto}
#wbtns{display:flex;-webkit-app-region:no-drag}
/* The buttons sit inside the drag region, so they opt out of it again --
   without this a click on 'close' starts a drag instead of closing. */
.wb{width:42px;height:33px;display:grid;place-items:center;color:var(--dimmer);
  font-size:11px;cursor:default}
.wb:hover{background:var(--hover);color:var(--text-hover)}
.wb.close:hover{background:var(--close-bg);color:var(--on-solid)}

/* -- Hilfe, and the sheet it opens -------------------------------------- */
/* INSIDE THE DRAG REGION AND OPTED OUT OF IT, the same way #wbtns is: the title
   bar moves the window, so anything clickable in it has to say it is not that.
   Without the opt-out a click on Hilfe starts a drag and the menu never opens. */
#helpwrap{position:relative;-webkit-app-region:no-drag}
#help{font:inherit;font-size:11px;cursor:pointer;border:1px solid transparent;
  border-radius:6px;padding:2px 9px;background:transparent;color:var(--dim)}
#help:hover{border-color:var(--line);color:var(--text-hover)}
/* DOWNWARDS, unlike the four menus at the bottom of the window: this one hangs
   off the title bar, and a menu that opened upwards from there would be drawn
   outside the window. */
#helpmenu{position:absolute;top:calc(100% + 5px);left:0;min-width:150px;
  background:var(--panel);border:1px solid var(--bevel);border-radius:8px;
  padding:5px;box-shadow:0 8px 26px var(--shadow);z-index:60}
#helpmenu[hidden]{display:none}
#helpmenu button{display:block;width:100%;text-align:left;font:inherit;
  font-size:11.5px;cursor:pointer;background:transparent;border:0;
  border-radius:6px;padding:7px 9px;color:var(--dim)}
#helpmenu button:hover{background:var(--hover);color:var(--text-hover)}

/* A LAYER IN THIS WINDOW, NOT A SECOND WINDOW. A second pywebview window would
   need its own bridge, its own theme attribute and its own close path, for a
   panel that is only ever open on top of this one. */
#settings{position:fixed;inset:0;z-index:80;display:grid;place-items:center;
  background:var(--shadow-strong)}
#settings[hidden]{display:none}
#settings .sheet{width:min(1040px,94vw);height:min(780px,90vh);display:flex;
  flex-direction:column;background:var(--panel);border:1px solid var(--bevel);
  border-radius:12px;box-shadow:0 24px 60px var(--shadow-strong);overflow:hidden}
#settings .shead{display:flex;align-items:center;gap:10px;padding:13px 16px;
  border-bottom:1px solid var(--line)}
#settings .shead h2{margin:0;font-size:13px;font-weight:600;color:var(--text-strong)}
#settings .sclose{margin-left:auto;font:inherit;font-size:12px;cursor:pointer;
  background:transparent;border:0;color:var(--dimmer);padding:2px 7px;border-radius:6px}
#settings .sclose:hover{background:var(--hover);color:var(--text-hover)}
#settings .sbody{display:flex;flex:1;min-height:0}
#scats{flex:none;width:170px;border-right:1px solid var(--line);padding:10px 8px;
  display:flex;flex-direction:column;gap:2px}
#scats button{font:inherit;font-size:12px;text-align:left;cursor:pointer;
  background:transparent;border:0;border-radius:6px;padding:7px 10px;color:var(--dim)}
#scats button:hover{background:var(--hover);color:var(--text-hover)}
#scats button.on{background:var(--raised);color:var(--text-strong)}
#spane{flex:1;min-width:0;overflow-y:auto;padding:16px 20px;user-select:text}
#spane h3{margin:0 0 11px;font-size:11px;font-weight:600;color:var(--text-soft);
  letter-spacing:.08em;text-transform:uppercase}
#spane .empty{color:var(--dimmer);font-size:12px}
#spane .about{color:var(--text);font-size:12px;line-height:1.75}
#spane .about b{color:var(--text-strong);font-weight:600}
#themes{display:flex;gap:10px}
#themes button{font:inherit;font-size:12px;cursor:pointer;border-radius:8px;
  padding:9px 15px;background:transparent;border:1px solid var(--line);color:var(--dim)}
#themes button:hover{border-color:var(--bevel);color:var(--text-hover)}
#themes button.on{border-color:var(--accent);color:var(--accent);
  box-shadow:0 0 0 3px rgba(126,176,248,.13)}

/* #125. THE RAIL COLOUR SITS HERE, not only on the rail, because it is what
   the chat's rounded corner cuts away to. Without it the corner would expose
   whatever `body` happens to be and read as a notch rather than an edge. */
#body{display:flex;flex:1;min-height:0;background:var(--rail)}

/* -- resize grips ------------------------------------------------------- */
/* A FRAMELESS WINDOW HAS NO BORDERS TO GRAB. Windows draws the resize edges as
   part of the frame that was switched off, so they have to be drawn here: eight
   invisible strips that report where the pointer went and let Python move and
   resize the window. Six pixels, because that is roughly what the native frame
   offers and anything thinner is a game of skill. */
.grip{position:fixed;z-index:99}
#g-n{top:0;left:6px;right:6px;height:5px;cursor:ns-resize}
#g-s{bottom:0;left:6px;right:6px;height:5px;cursor:ns-resize}
#g-w{left:0;top:6px;bottom:6px;width:5px;cursor:ew-resize}
#g-e{right:0;top:6px;bottom:6px;width:5px;cursor:ew-resize}
#g-nw{top:0;left:0;width:8px;height:8px;cursor:nwse-resize}
#g-ne{top:0;right:0;width:8px;height:8px;cursor:nesw-resize}
#g-sw{bottom:0;left:0;width:8px;height:8px;cursor:nesw-resize}
#g-se{bottom:0;right:0;width:8px;height:8px;cursor:nwse-resize}

/* -- rail --------------------------------------------------------------- */
#rail{width:var(--railw,242px);flex:none;
  background:var(--rail);display:flex;flex-direction:column;min-height:0}
/* ZIEHBAR, UND DER GRIFF IST BREITER ALS DIE LINIE, DIE ER BEWEGT. Fuenf Pixel
   trifft man mit der Maus, einen nicht -- und die negativen Raender legen ihn
   ueber die Kante, statt dem Chat Platz wegzunehmen. */
#railgrip{flex:none;width:5px;margin:0 -2px;z-index:3;cursor:col-resize;
  background:transparent;transition:background .12s ease}
#railgrip:hover,#railgrip.on{background:var(--bevel)}
/* WAEHREND DES ZIEHENS OHNE UEBERGANG. Die Rail blendet ihre Breite sonst mit
   .16s ein, und das macht aus einer Geste ein Nachlaufen. */
#rail.dragging{transition:none}

/* -- code panel (#138) --------------------------------------------------- */
/* DIE RAIL GESPIEGELT. Jede Regel hier hat ihre Zwillingsregel links, und wo
   sie abweicht, steht der Grund daneben. */
/* DAS PANEL GIBT NACH, NIE DER COMPOSER. robin, 2026-08-27: "die Icons
   duerfen niemals aus der chateingabemaske rausgucken rechts". Die Knoepfe
   geben nicht nach (#138c, mit Grund), also muss es das Panel: `min-width:0`
   statt `flex:none`, damit die Mindestbreite von #main gewinnt, wenn Fenster,
   Rail und gezogene Panelbreite zusammen nicht passen. `width` bleibt die
   Wunschbreite; gequetscht wird nur, was nicht hineinpasst. */
#code{width:var(--codew,__CODEW__px);flex:0 1 auto;min-width:0;background:var(--rail);
  display:flex;flex-direction:column;min-height:0;overflow:hidden}
#codegrip{flex:none;width:5px;margin:0 -2px;z-index:3;cursor:col-resize;
  background:transparent;transition:background .12s ease}
#codegrip:hover,#codegrip.on{background:var(--bevel)}
#code.dragging{transition:none}
#code{transition:width .16s ease}
body[data-code="shut"] #code{width:0;overflow:hidden}
/* DER GRIFF GEHT MIT. Ein Anfasser an einer Flaeche von null Pixeln ist ein
   Streifen, der nichts bewegt -- und er saesse genau auf der Kante, die das
   geschlossene Panel gerade sauber macht. */
body[data-code="shut"] #codegrip{display:none}
#codehead{display:flex;align-items:center;gap:8px;padding:0 12px;
  min-height:var(--barh);flex:none}
#codehead h2{margin:0;font-size:10.5px;font-weight:600;letter-spacing:.13em;
  text-transform:uppercase;color:var(--dimmer)}
#codehead .n{color:var(--dimmer);font-size:11px;font-variant-numeric:tabular-nums}
#codecopy{margin-left:auto;font:inherit;font-size:11px;color:var(--dim);
  background:transparent;border:1px solid var(--line);border-radius:6px;
  padding:2px 9px;cursor:pointer}
#codecopy:hover{border-color:var(--bevel);color:var(--accent)}
/* #138b. `margin-left:auto` WANDERT AUF DEN ERSTEN DER BEIDEN, sonst schoeben
   sich zwei Knoepfe je einmal nach rechts und der Abstand zwischen ihnen
   waere der ganze Rest der Leiste. */
#codewipe{margin-left:auto;font:inherit;font-size:11px;color:var(--dimmer);
  background:transparent;border:1px solid var(--line);border-radius:6px;
  padding:2px 9px;cursor:pointer}
#codewipe:hover{border-color:var(--bevel);color:var(--text-hover)}
#codecopy{margin-left:0}
#codebody{overflow-y:auto;padding:0 8px 10px;flex:1;min-height:0}
/* #138b. DER QUELLTEXT UNTER EIGENEM NAMEN, damit der Abschnitt beantwortet,
   wofuer er da ist. Versteckt, solange nichts geschrieben wurde -- eine
   Ueberschrift ueber einer leeren Flaeche behauptet einen Inhalt. */
#codefiles[hidden]{display:none}
#codefiles .cfh{font-size:10px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--dimmer);padding:8px 2px 0}
/* #138. EIN BLOCK JE AUFRUF. Dieselbe Sprache wie ein Codeblock in der Antwort
   -- Rahmen, Kopfzeile, Monospace -- weil es dasselbe ist: Text, den ein Modell
   schreibt und den jemand lesen will. */
.cw{margin:8px 0 0;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);overflow:hidden}
.cwh{padding:5px 9px;font-size:10.5px;font-weight:600;letter-spacing:.06em;
  color:var(--dimmer);border-bottom:1px solid var(--line)}
/* UMBRECHEND UND NICHT SCROLLEND. Eine Zeile JSON ist tausend Zeichen lang; ein
   waagerechter Balken je Block macht aus dem Mitlesen ein Schieben. */
.cwp{margin:0;padding:8px 9px;font-family:var(--mono);font-size:11px;
  line-height:1.5;white-space:pre-wrap;word-break:break-word;
  color:var(--text-faint);max-height:none}
#railhead{display:flex;align-items:center;padding:0 12px;min-height:var(--barh)}
#railhead h2{margin:0;font-size:10.5px;font-weight:600;letter-spacing:.13em;
  text-transform:uppercase;color:var(--dimmer)}
#new{margin-left:auto;font:inherit;font-size:11px;color:var(--dim);
  background:transparent;border:1px solid var(--line);border-radius:6px;
  padding:2px 9px;cursor:pointer}
#new:hover{border-color:var(--bevel);color:var(--accent)}
#sessions{overflow-y:auto;padding:6px 6px 10px;flex:1;min-height:0}
.sess{position:relative;display:block;width:100%;text-align:left;border:0;
  background:transparent;font:inherit;color:inherit;padding:7px 9px 8px 12px;
  border-radius:6px}
.sess.on{background:var(--raised)}
.sess.on .t{color:var(--model)}
.sess.on::before{content:"";position:absolute;left:2px;top:8px;bottom:8px;
  width:2px;background:var(--accent);border-radius:2px}
.sess .t{font-size:12px;color:var(--text-faint);display:block;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.sess .s{font-size:10.5px;color:var(--dimmer);display:block;margin-top:1px}
/* -- #119: the line an empty chat carries -------------------------------- */
/* CENTRED IN THE COLUMN THE CHAT WILL FILL, not in the window: the first turn
   lands in `.turn`, which is 960 wide and centred, so a greeting centred on
   the window would sit off the axis every message after it uses.
   AND CENTRED IN THE HEIGHT, WITHOUT A TUNED NUMBER. It stood at `16vh`, which
   is a guess about one window at one size -- `min-height:100%` fills #flow's
   CONTENT box, and that box already excludes the bottom padding the
   ResizeObserver reserves for the composer. So the line sits on the middle of
   the space that is actually free, at any window height, and moves when the
   composer grows.
   QUIET. It is a hello, not a headline -- and it is the only thing on screen,
   which does the emphasis by itself. */
/* #127. A COLUMN NOW, because the greeting is a drawing with a line under it.
   Everything else about the block is unchanged -- it still fills the empty
   flow and still centres in both directions. */
#hello{min-height:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:26px;
  max-width:960px;margin-inline:auto;padding:0 30px;text-align:center;
  font-size:19px;color:var(--text-faint);letter-spacing:.01em}
/* SIZED IN THE PAGE, NOT IN THE FILE. Both drawings are 1024 square; a width
   here keeps them from filling the window, and `height:auto` keeps the square
   without the file having to know how big it is drawn. */
.mk{width:200px;max-width:46vw;line-height:0}
.mk svg{width:100%;height:auto;display:block}
/* WHICH ONE IS VISIBLE IS A CSS QUESTION, so switching theme needs no
   JavaScript and no second copy of which theme is live. Dark is the default
   because two of the three themes are dark; only `light` swaps. */
.mk-light{display:none}
:root[data-theme="light"] .mk-dark{display:none}
:root[data-theme="light"] .mk-light{display:block}
/* -- context menu -------------------------------------------------------- */
#menu{position:fixed;z-index:200;display:none;min-width:168px;padding:4px;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;
  box-shadow:0 12px 34px -10px var(--shadow-strong)}
#menu.on{display:block}
#menu button{display:block;width:100%;text-align:left;font:inherit;
  font-size:11.5px;color:var(--dim);background:transparent;border:0;
  padding:6px 10px;border-radius:5px;cursor:pointer}
#menu button:hover{background:var(--raised);color:var(--text-hover)}
#menu button.danger:hover{background:rgba(240,101,90,.14);color:var(--bad-text)}
#menu .sep{height:1px;background:var(--line-soft);margin:4px 2px}
/* #119. THE LABEL IS IN A <b> because the rows are built from a plan and set by
   textContent -- NOT to make it bold, which is why the weight is put back. */
#menu button b{font-weight:400}
#menu button .what{display:block;color:var(--dimmer);font-size:10px;margin-top:1px}
#menu .mhead{color:var(--dimmer);font-size:9.5px;text-transform:uppercase;
  letter-spacing:.09em;padding:3px 10px 2px}
/* THE INDENT SAYS THESE ARE ARGUMENTS, not commands: "Crow" under "zu Projekt"
   is a destination, and at the same offset as `rename` it would read as one
   more thing the menu does. Same claim the level rows make in #modelmenu. */
#menu button.indent{padding-left:22px}

/* -- #119: the rail folds away, and so does every project ---------------- */
/* THE ATTRIBUTE IS ON <body>, written by Python before the page is handed over,
   for the reason the theme sits on <html>: a rail that unfolded and then collapsed would do it
   on every single start, and that frame is when somebody is looking.
   WIDTH TO ZERO AND NOT display:none: the transition is what tells the eye the
   list went somewhere rather than that the window redrew. `overflow:hidden`
   is what keeps the contents from spilling across the chat while it closes. */
#rail{transition:width .16s ease}
body[data-rail="shut"] #rail{width:0;overflow:hidden}
/* IN THE TITLE BAR, so it survives the rail it hides. It is the one control
   that must not live in the thing it folds away. */
#railtoggle,#codetoggle{font:inherit;color:var(--dimmer);background:transparent;
  border:1px solid transparent;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;padding:3px 5px;margin-right:2px}
#railtoggle:hover,#codetoggle:hover{color:var(--accent);border-color:var(--bevel)}

/* A PROJECT ROW IS NOT A CHAT ROW, and the difference has to survive a glance:
   it is a heading you can fold, so it carries a caret and a count and never the
   two-line shape a chat has. */
.proj{position:relative;display:flex;align-items:center;gap:7px;width:100%;
  text-align:left;border:0;background:transparent;font:inherit;color:inherit;
  padding:6px 9px 6px 8px;border-radius:6px;cursor:pointer;margin-top:2px}
.proj:hover{background:var(--raised)}
/* BOLD, robin on sight: a heading and its children at one weight is a list
   with an indent, not a group. The chats under it stay at 400. */
.proj .t{font-size:11.5px;font-weight:600;color:var(--text-hi);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;flex:1}
.proj .n{font-size:10px;color:var(--dimmer);flex:none}
/* THE CARET IS THE STATE, so it turns rather than swapping glyph: a rotation
   reads as the same thing moving, and two different arrows read as two things.
   The archive drawer beside this uses the same mark for the same reason. */
.proj .caret{font-size:8px;color:var(--dimmer);flex:none;
  transition:transform .14s ease}
.proj.open .caret{transform:rotate(90deg)}
/* THE INDENT IS THE MEMBERSHIP. A chat under a heading at the same offset as one
   below it says nothing about which of the two it belongs to -- and a project
   with its rows folded away leaves no other trace that they were there. */
.sess.inproj{padding-left:24px}
.sess.inproj::after{content:"";position:absolute;left:14px;top:0;bottom:0;
  width:1px;background:var(--line-soft)}
/* The rename field replaces the row in place, so the list never jumps. */
.sess input{width:100%;font:inherit;font-size:12px;color:var(--model);
  background:var(--bg);border:1px solid var(--accent);border-radius:4px;
  padding:2px 5px;outline:0}
#railsep{padding:12px 12px 4px;font-size:10.5px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--dimmer)}
/* THE FOOTER IS GONE. It said which file the chat came from, which is a thing
   the rail entry already says and nobody reads twice -- and it cost a bordered
   box in the corner for it. */
#archbar{display:flex;align-items:center;gap:6px;width:100%;font:inherit;
  font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--dimmer);background:transparent;border:0;cursor:pointer;
  padding:12px 12px 4px}
#archbar:hover{color:var(--dim)}
#archbar .caret{font-size:9px;color:var(--bevel);transition:transform .12s ease}
#arch.open + #archbar .caret,#archbar.open .caret{transform:rotate(90deg)}
#archbar .count{margin-left:auto;letter-spacing:0;text-transform:none}
#arch{display:none}
#arch.open{display:block}

/* -- main --------------------------------------------------------------- */
/* #125. THE CORNER IS THE ONLY DIVIDER LEFT. With every rule removed the chat
   would blur into the panel; a radius separates them the way a sheet of paper
   on a desk is separate -- by lying on top, not by having a line drawn round
   it. `#body` carries the rail colour so there is something for the corner to
   cut away to, and `overflow:hidden` is what stops the flow's own background
   from squaring it off again at the first scroll. */
/* DIE MINDESTBREITE DER MASKE, robin am 2026-08-27, zweite und endgueltige
   Ansage am Abend: "DIE CHATEINGABEMASKE DARF NIEMALS KLEINER WERDEN ALS IN
   SCREENSHOT 2." Dort trug die Chatspalte ~570 px -- Modell-Chip ungekuerzt,
   alle Knoepfe innen. 380 liess den Chip abschneiden, sobald ein gezogenes
   Code-Panel drueckte; 560 ist die Grenze, gegen die das PANEL nachgibt
   (min-width:0 dort). Die Fenster-Mindestbreite unten haelt die Rechnung im
   SCHLIMMSTEN Zustand: RAIL_MAX 520 + 560 Chat + 50 Spalten-Chrome = 1130.
   Ein Fall prueft beide Zahlen gegeneinander -- keine darf allein wandern. */
#main{flex:1;display:flex;flex-direction:column;min-width:560px;min-height:0;
  background:var(--bg);border-top-left-radius:12px;
  /* #138. DIE ZWEITE KANTE, gespiegelt. Sie gehoert #main und nicht dem
     Panel: die Chatspalte ist das Blatt, das vor den Seitenflaechen liegt,
     und sie hat eine Ecke auf jeder Seite, sobald es beide gibt. Der Radius
     bleibt auch bei geschlossenem Panel stehen -- dahinter liegt dann der
     Fensterhintergrund, und eine Ecke, die beim Zuklappen aufspringt, macht
     aus dem Falten eine Formaenderung. */
  border-top-right-radius:12px;overflow:hidden;
  position:relative}
/* #125. THE STATUS BAR IS GONE, not hidden. Both chips it carried moved into
   the settings sheet -- the connection with its address, and the tool switch --
   and an empty bar is a band of nothing between the ribbon and the first line
   of the chat. The rule it drew is gone with it, which is half of the seam
   robin asked to remove. */
.chip{display:inline-flex;align-items:center;gap:6px;color:var(--dim);
  border:1px solid var(--line);border-radius:999px;padding:2px 10px;
  white-space:nowrap}
.chip b{font-weight:500;color:var(--text-soft)}
#dot{width:6px;height:6px;border-radius:50%;background:var(--dimmer)}
#dot.up{background:var(--ok);box-shadow:0 0 0 3px rgba(78,201,143,.14)}
#dot.down{background:var(--bad);box-shadow:0 0 0 3px rgba(240,101,90,.14)}
/* THE ADDRESS IS A TOOLTIP NOW (#119), so the chip that used to draw it borderless is gone with
   it and `.ghost` went the same way -- a class with no wearer is the kind of thing that gets
   copied onto the next element by somebody reading the file for a pattern.
   `cursor:help` IS THE ONLY HINT THERE IS. A native title has no affordance of its own; without
   this the base URL is a fact nobody discovers, which is worse than the width it saved. */
#conn{cursor:help}
#tools{cursor:pointer;transition:color .15s,border-color .15s}
#tools:hover{border-color:var(--bevel)}


/* STABLE GUTTER, so the column does not shift sideways the moment a chat grows
   past one screen -- and so the composer below can line up against one number
   instead of against a scrollbar that comes and goes. */
/* LUFT AN BEIDEN SEITEN (robin, 2026-08-23). Links steht die Rail, rechts der
   Scrollbalken; der Rinnstein daneben haelt die Spalte ruhig, aber er ist kein
   Abstand. Zehn Pixel sind es, und `#composer` traegt dieselben zehn, damit
   Spalte und Eingabemaske weiter auf derselben Kante stehen. */
#flow{overflow-y:auto;padding:22px 0 26px;padding-inline:10px;flex:1;
  min-height:0;scroll-behavior:smooth;user-select:text;
  scrollbar-gutter:stable}
/* CENTRED, NOT LEFT-HUGGING. max-width alone pins the column to the left edge
   and leaves the rest of a wide window empty; the auto margins are what put it
   in the middle. 960 includes the 30px padding, so the text runs 900 wide --
   the same 900 #box is held to, which is what makes the two flush. */
.turn{padding:0 30px;max-width:960px;margin-inline:auto}
.turn+.turn{margin-top:26px}
/* #131. NO LABEL. The bubble says whose the line is; a three-letter prefix in
   front of it says it a second time, and the model's own turns never had one. */
.you{display:grid;grid-template-columns:1fr;gap:2px}
/* #130. ONE BUBBLE PER MESSAGE. What the user said and what the model said
   used to differ only by the colour of a three-letter label, and in a long
   scroll that is not a boundary anyone sees. The bubble is drawn out of the
   palette -- `--raised` and `--line` are defined in all three themes -- so
   light, dark and crow all answer for it without a second rule.
   `justify-self:start` is what makes it hug the text: without it the bubble
   fills the grid column and a two-word message is a full-width slab. */
.you .txt{color:var(--text);white-space:pre-wrap;background:var(--raised);
  border:1px solid var(--line);border-radius:12px;padding:9px 13px;
  justify-self:start;max-width:75%;box-sizing:border-box}
.as{display:grid;grid-template-columns:38px 1fr;gap:2px}
.as .m{color:var(--bevel);padding-top:1px}
.col{min-width:0}

/* #131. THE TRACE. Same furniture as a reasoning block, one level up: it holds
   the ROUNDS, each of which holds its own thoughts. Folded it is one line, so a
   turn of any length costs the reader one line before the answer. */
details.trace{margin:0 0 14px}
details.trace>summary{list-style:none;cursor:pointer;display:inline-flex;
  align-items:center;gap:8px;font-size:11.5px;color:var(--dimmer);padding:2px 0}
details.trace>summary::-webkit-details-marker{display:none}
details.trace>summary:hover{color:var(--dim)}
details.trace[open]>summary .caret{transform:rotate(90deg)}
details.trace .tl{color:var(--text-soft)}
details.trace .tn{font-variant-numeric:tabular-nums}
details.trace .tb{margin-top:8px;padding-left:11px;
  border-left:2px solid var(--line)}
/* The rounds inside carry their own turn padding, and a second column width
   inside a column is an indent nobody asked for. */
details.trace .tb .turn{padding:0;max-width:none;margin-inline:0}

details.think{margin:0 0 10px}
details.think>summary{list-style:none;cursor:pointer;display:inline-flex;
  align-items:center;gap:7px;font-size:11.5px;color:var(--dimmer);padding:1px 0}
details.think>summary::-webkit-details-marker{display:none}
details.think>summary:hover{color:var(--dim)}
.caret{display:inline-block;transition:transform .12s ease;font-size:9px;
  color:var(--bevel)}
details.think[open] .caret{transform:rotate(90deg)}
.pct{color:var(--dimmer);border-left:1px solid var(--line);padding-left:7px}
.tbody{margin:8px 0 2px;padding:10px 13px;border-left:2px solid var(--line);
  color:var(--think);font-size:12px;line-height:1.65;background:var(--think-bg);
  border-radius:0 6px 6px 0;white-space:pre-wrap}
.say{color:var(--model);line-height:1.62;white-space:pre-wrap}
/* THE FORMATTED ANSWER. `pre-wrap` moves off the box and onto the paragraph:
   inside one, a single newline is still a line break -- this client has never
   folded them and folding them now would reflow every answer robin has read --
   while a table or a list wraps the way its own box says. */
.say.md{white-space:normal}
.md p{margin:0 0 9px;white-space:pre-wrap}
.md p:last-child,.md ul:last-child,.md ol:last-child,.md table:last-child{
  margin-bottom:0}
.md .mdh{color:var(--text-hi);font-weight:600;margin:13px 0 6px;line-height:1.3}
.md .mdh:first-child{margin-top:0}
.md .mdh1{font-size:16px}
.md .mdh2{font-size:14.5px}
.md .mdh3,.md .mdh4,.md .mdh5,.md .mdh6{font-size:13px}
.md ul,.md ol{margin:0 0 9px;padding-left:21px}
.md li{margin:2px 0}
.md strong{color:var(--text-hi);font-weight:600}
.md code{font-family:var(--mono);font-size:12px;background:var(--raised);
  border-radius:4px;padding:1px 5px}
.md a.lnk{color:var(--accent);text-decoration:underline;cursor:pointer}
/* A WIDE TABLE SCROLLS INSIDE ITSELF rather than widening the chat: the column
   is what every other block is measured against. */
.md table{display:block;overflow-x:auto;border-collapse:collapse;margin:0 0 9px;
  font-size:12.5px}
.md th,.md td{border:1px solid var(--line);padding:5px 9px;text-align:left;
  vertical-align:top}
.md th{background:var(--raised);color:var(--text-hi);font-weight:600}
/* #131. THE TOOL-CALL TILE. Collapsed it is a title, a count and a plus; open
   it is every call this chat has made. It sizes to the WIDEST row it holds --
   `width:max-content` -- because a tool line is a path plus arguments and a
   fixed width would ellipsis away the half that says which file.
   ABSOLUTE AND NOT STICKY: sticky inside `#flow` scrolls with the first turn
   until it hits the top, which is a tile that moves for no reason. */
/* THE SAME GROUND AS THE USER'S BUBBLE, and by the same tokens rather than by
   a matching literal: `--raised` and `--line` are defined once per palette, so
   the tile follows the skin that is on instead of following one that was. */
/* #138. IM PANEL, NICHT MEHR UEBER DEM CHAT. Als Kachel schwebte sie absolut
   ueber der Leseflaeche und musste sich auf 44vw begrenzen, um sie nicht zu
   verdecken. In einer eigenen Spalte verdeckt sie nichts, also nimmt sie die
   volle Breite und traegt weder Schatten noch eigene Ecke -- beides war die
   Sprache eines Elements, das ueber etwas anderem liegt. */
#toolcalls{width:100%;border:1px solid var(--line);border-radius:8px;
  background:var(--raised);font-size:11.5px;overflow:hidden}
#toolcalls .tchd{display:flex;align-items:center;gap:9px;padding:7px 12px;
  cursor:pointer;user-select:none}
#toolcalls .tct{font-weight:600;color:var(--text-soft)}
#toolcalls .tcn{color:var(--dimmer);font-variant-numeric:tabular-nums}
#toolcalls .tcx{margin-left:auto;color:var(--dimmer);font-size:15px;
  line-height:1;width:11px;text-align:center}
#toolcalls.shut .tcbody{display:none}
#toolcalls .tcbody{border-top:1px solid var(--line);padding:8px}
#toolcalls .tool{margin:0 0 6px}
/* INSIDE THE TILE THE ARGUMENTS ARE THE POINT, so they are not clipped -- the
   tile grew to fit them. In the flow they were one line of a column that had
   other things to show. */
#toolcalls .tool .arg{overflow:visible;text-overflow:clip}
#toolcalls .tcclear{font:inherit;font-size:10.5px;cursor:pointer;
  border-radius:5px;padding:2px 9px;background:transparent;
  border:1px solid var(--line);color:var(--dimmer)}
#toolcalls .tcclear:hover{border-color:var(--bevel);color:var(--text-hover)}
#toolcalls .empty{margin:0;padding:2px 4px}

/* #138b. JEDE ZEILE IST IHRE EIGENE KLAPPE. Der Kopf bleibt eine Zeile -- er
   ist der Index -- und alles, was Platz braucht, liegt darunter und nur dann,
   wenn jemand danach gefragt hat. */
#toolcalls .tool{border:1px solid transparent;border-radius:6px}
#toolcalls .tool .hd{cursor:pointer;user-select:none}
#toolcalls .tool:not(.shut){border-color:var(--line);background:var(--panel)}
#toolcalls .tool.shut .tbody{display:none}
#toolcalls .tool .tx{margin-left:6px;color:var(--dimmer);width:9px;
  text-align:center;flex:none}
/* EIN FEHLGESCHLAGENER AUFRUF TRAEGT ES AM KOPF, nicht erst im Rumpf: sonst
   muesste man neunzig Zeilen aufklappen, um den einen zu finden, der der
   Grund war. */
#toolcalls .tool.bad .ico{color:var(--bad,#e06c75)}
#toolcalls .tbody{padding:2px 8px 8px}
#toolcalls .tsec{margin-top:6px}
#toolcalls .tsh{font-size:10px;letter-spacing:.04em;text-transform:uppercase;
  color:var(--dimmer);margin-bottom:3px}
/* DIE ANTWORT IN EINEM EIGENEN BLOCK, abgesetzt gegen die Argumente. Beides
   in einem Kasten war die Form, die aus dem Panel eine JSON-Wand gemacht hat:
   was hineinging und was herauskam sahen gleich aus. */
#toolcalls .tsp{margin:0;padding:6px 8px;border-radius:5px;background:var(--rail);
  border:1px solid var(--line);white-space:pre-wrap;word-break:break-word;
  max-height:260px;overflow:auto;font-size:11px;line-height:1.45}
#toolcalls .res .tsp{background:var(--raised)}
#toolcalls .tsmore{font-size:10px;color:var(--dimmer);margin-top:3px}

.tool{margin:11px 0;border:1px solid var(--line);border-radius:8px;
  background:var(--panel);overflow:hidden}
.tool .hd{display:flex;align-items:center;gap:9px;padding:6px 11px;
  font-size:11.5px}
.tool .ico{color:var(--warn);font-size:9px}
.tool .name{color:var(--text-soft)}
.tool .arg{color:var(--dimmer);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.tool .note{margin-left:auto;color:var(--dimmer);white-space:nowrap}
.code{margin:12px 0;border:1px solid var(--line);border-radius:8px;
  background:var(--code-bg);overflow:hidden}
.code .hd{display:flex;align-items:center;gap:8px;padding:6px 8px 6px 12px;
  background:var(--panel);border-bottom:1px solid var(--line);font-size:11.5px}
.code .lang{color:var(--dimmer);font-size:10.5px;letter-spacing:.06em;
  text-transform:uppercase}
/* robin, 2026-08-24: lange Bloecke sind klappbar. Die Zeilenzahl steht neben
   der Sprache und sagt zugeklappt, wieviel darunter liegt -- eine Falte ohne
   Etikett ist eine Kiste. */
.code .n{color:var(--dimmer);font-size:10.5px}
.code.foldable .hd{cursor:pointer;user-select:none}
.code.foldable .hd:hover .lang,.code.foldable .hd:hover .n{color:var(--accent)}
/* DER KOPF BLEIBT, der Rumpf geht. Ein Block, der ganz verschwindet, nimmt den
   Weg zurueck mit sich. */
.code.shut pre{display:none}
.code.shut{border-style:dashed}
.copy{margin-left:auto;font:inherit;font-size:11px;color:var(--dimmer);
  background:transparent;border:1px solid var(--line);border-radius:5px;
  padding:1.5px 9px;cursor:pointer}
.copy:hover{border-color:var(--bevel);color:var(--accent)}
.copy.done{color:var(--ok);border-color:rgba(78,201,143,.4)}
/* #138. DIE FARBEN KOMMEN AUS DER PALETTE, nie aus einem Literal -- die Regel,
   die alle drei Themes am Leben haelt. Vier Klassen reichen: was erklaert,
   was Text ist, was zaehlt, was Struktur ist. */
.hl-c{color:var(--dimmer);font-style:italic}
.hl-s{color:var(--ok)}
.hl-n{color:var(--model)}
.hl-k{color:var(--accent)}
.hl-t{color:var(--accent)}
.code pre{margin:0;padding:11px 13px;overflow-x:auto;font-size:12px;
  line-height:1.6;color:var(--code);user-select:text;font-family:var(--mono)}
/* Anything that names a path, a flag or a symbol is code and keeps the mono
   stack; the body around it does not. */
code,.asktop code,#url,.cost{font-family:var(--mono)}
.cost{margin-top:11px;font-size:10.5px;color:var(--dimmer);
  border-top:1px dashed var(--line);padding-top:7px;overflow-x:auto;
  white-space:nowrap}
.fail{color:var(--bad);font-size:11.5px;margin-top:8px;
  /* `failure_line` puts advice on a second line. Without this the
     newline renders as a space and the advice reads as part of the
     error -- seen live 2026-08-24. */
  white-space:pre-wrap}
/* PRE-WRAP, NOT NORMAL. A note is not always one sentence: /help and /tools
   build a column with their own line breaks and their own padding, and the
   default collapsed all of it into a paragraph -- one run-on line of eight
   commands. True of /tools since the day the window answered it. `pre-wrap`
   rather than `pre` so a long single-line note still wraps at the column
   instead of running off the side. */
.note{color:var(--dimmer);font-size:11.5px;white-space:pre-wrap}
/* #124. THE SKILL ROWS AND THEIR SWITCH. A switch rather than a checkbox
   because what it controls is a STATE kept in a file, not a choice inside a
   form -- and because the row has to read as on or off from across the sheet.
   Every colour comes from the palette, so all three themes answer for it. */
.shint{color:var(--dimmer);font-size:11.5px;margin:0 0 10px}
#updbtn{margin:0 0 9px;padding:5px 12px;border:1px solid var(--line);
  border-radius:6px;background:var(--raised);color:var(--accent);
  font-size:12px;cursor:pointer}
#updbtn:hover{border-color:var(--accent)}
#updbtn:disabled{color:var(--dimmer);border-color:var(--line);cursor:default}
.srow{display:flex;align-items:flex-start;gap:10px;padding:8px 0;
  border-top:1px solid var(--raised)}
.srow:first-child{border-top:none}
.srow .stext{flex:1;min-width:0}
.srow .sname{font-weight:600;font-size:12.5px}
.srow .sdesc{color:var(--dim);font-size:11.5px;margin-top:2px}
.srow.off .sname,.srow.off .sdesc{color:var(--dimmer)}
.sw{flex:0 0 auto;width:34px;height:19px;border-radius:10px;border:none;
  background:var(--raised);position:relative;cursor:pointer;padding:0;
  transition:background .15s ease}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:13px;height:13px;
  border-radius:50%;background:var(--dimmer);
  transition:transform .15s ease,background .15s ease}
.sw.on{background:color-mix(in srgb,var(--accent) 40%,transparent)}
.sw.on::after{transform:translateX(15px);background:var(--accent)}
/* THE SERVER BAR, first thing inside an unfolded server. It carries the two
   controls the head has no room for: the head is where every button has to
   stop its click from folding the row away, and the case that guards the fold
   counts exactly two of those. */
.mcpbar{display:flex;gap:9px;align-items:center;padding:9px 0 11px;
  border-bottom:1px solid var(--line);margin-bottom:7px}
.swlabel{font-size:11px;color:var(--dim);white-space:nowrap}
.mcpkey{font:inherit;font-size:12px;font-family:var(--mono);flex:1;
  min-width:0;margin-left:auto;color:var(--text);background:var(--raised);
  border:1px solid var(--line);border-radius:6px;padding:6px 9px}
.mcpkey:focus{outline:none;border-color:var(--accent)}
/* #129. THE MCP CHECKLIST, AND ITS TWO COLUMNS ARE THE WHOLE STAGE. A switch
   says whether a tool is taken at all; the segment beside it says what Crow
   treats it as, which is what decides whether a release level stops and asks.
   Both are states in a file, so both are switches rather than form controls.

   A DASHED EDGE IS A PROPOSAL, A SOLID ONE IS A DECISION, and the difference
   has to be visible without reading a legend: the specification calls a
   server's annotation untrusted, so what it suggested may never look the same
   as what a person confirmed. Colour alone would not do it -- one of the three
   themes would always render the two too close -- so the EDGE carries it.

   Every colour is a palette variable, never a literal, or only the theme it was
   picked in answers for it. */
.seg{display:flex;flex:0 0 auto}
.seg button{font:inherit;font-size:10.5px;cursor:pointer;padding:3px 9px;
  background:transparent;border:1px solid var(--line);color:var(--dimmer)}
.seg button:first-child{border-radius:6px 0 0 6px}
.seg button:last-child{border-radius:0 6px 6px 0}
.seg button+button{border-left:none}
.seg button:hover{color:var(--text-hover)}
.seg button.guess{color:var(--dim);border-style:dashed}
.seg button.on{color:var(--accent);border-color:var(--accent);
  background:color-mix(in srgb,var(--accent) 12%,transparent)}
.mcphead{display:flex;align-items:baseline;gap:8px;margin:15px 0 2px;
  border-top:1px solid var(--raised);padding-top:11px}
.mcphead .sname{flex:0 0 auto}
.mcphead .cmd{flex:1;min-width:0;color:var(--dimmer);font-family:var(--mono);
  font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mcphead button{font:inherit;font-size:10.5px;cursor:pointer;padding:2px 9px;
  border-radius:5px;background:transparent;border:1px solid var(--line);
  color:var(--dimmer)}
.mcphead button:hover{border-color:var(--bevel);color:var(--text-hover)}
.mcpcost{color:var(--dimmer);font-size:10.5px;margin:5px 0 0}
/* THE TWO FOLDS ON THE MODEL PAGE. Provider and model are one page because
   picking the first is most of picking the second, and they fold because a
   provider list nobody is changing should not push the model out of view. The
   head is the same shape `.mcphead` already draws, so the sheet keeps one idea
   of what a foldable heading looks like. */
.fold{display:flex;align-items:baseline;gap:8px;margin:14px 0 2px;cursor:pointer;
  border-top:1px solid var(--raised);padding-top:11px}
.fold:first-child{border-top:none;margin-top:0;padding-top:0}
.fold .sname{flex:0 0 auto}
.fold .count{margin-left:auto;color:var(--dimmer);font-size:10.5px}
.fold .caret{color:var(--dimmer)}
.fold.open .caret{transform:rotate(90deg)}
.foldbody{display:none}
.foldbody.open{display:block}
/* THE MODEL LIST IS A SELECT AND NOT A COLUMN OF ROWS. OpenRouter answers with
   hundreds of slugs; drawn as rows they would be the longest thing in the
   window and none of them easier to find. Monospace because a slug is an
   identifier -- `:free` at its end is part of the id and decides which bill the
   turn lands on. */
.msel{font:inherit;font-size:12px;font-family:var(--mono);width:100%;
  color:var(--text);background:var(--raised);border:1px solid var(--line);
  border-radius:6px;padding:7px 9px}
.msel:focus{outline:none;border-color:var(--accent)}
.keyrow{display:flex;gap:7px;align-items:center;margin-top:6px}
.keyrow input{font:inherit;font-size:12px;font-family:var(--mono);flex:1;
  min-width:0;color:var(--text);background:var(--raised);
  border:1px solid var(--line);border-radius:6px;padding:7px 9px}
.keyrow input:focus{outline:none;border-color:var(--accent)}
.keyrow button{font:inherit;font-size:11px;cursor:pointer;padding:6px 12px;
  border-radius:6px;background:transparent;border:1px solid var(--line);
  color:var(--dim)}
.keyrow button:hover{border-color:var(--bevel);color:var(--text-hover)}
/* THE SUBSCRIPTION TILES. A tile rather than a row because what it offers is
   one act -- sign in -- and a row with a switch would say the state is
   something the page can set. It is not: it is the outcome of a browser leg
   that happens somewhere else and may not come back for minutes.
   Every colour is a palette variable, so all three themes answer for it. */
#subs{display:flex;flex-wrap:wrap;gap:10px}
.sub{flex:1 1 190px;display:flex;flex-direction:column;gap:6px;cursor:pointer;
  border:1px solid var(--line);border-radius:10px;padding:13px 14px;
  background:transparent;color:inherit;font:inherit;text-align:left}
.sub:hover{border-color:var(--bevel)}
.sub.on{border-color:var(--accent);
  background:color-mix(in srgb,var(--accent) 8%,transparent)}
/* THE BRAND COLOUR IS THE SKIN'S, and `--text-hi` is already what robin asked
   for: #ffffff under dark and crow, #0f1114 under light. A mark that took the
   accent when signed in would be recolouring somebody else's logo to say
   something about Crow's state -- the tile's own border says that. */
.sub .mark{width:26px;height:26px;color:var(--text-hi)}
.sub .sname{font-weight:600;font-size:12.5px}
.sub .sdesc{color:var(--dim);font-size:11.5px}
.sub .state{font-size:10.5px;color:var(--dimmer);letter-spacing:.04em;
  text-transform:uppercase}
.sub.on .state{color:var(--accent)}
.subout{align-self:flex-start;font:inherit;font-size:10.5px;cursor:pointer;
  padding:2px 9px;border-radius:5px;background:transparent;
  border:1px solid var(--line);color:var(--dimmer)}
.subout:hover{border-color:var(--bevel);color:var(--text-hover)}
.mcpbad{color:var(--bad);font-size:11.5px;margin:0 0 7px;white-space:pre-wrap}
.mcpsaid{color:var(--dim);font-size:11.5px;margin:0 0 7px;white-space:pre-wrap}
/* ONE ARGUMENT PER LINE, not a space-separated string: half of every MCP
   example is a path, and half of the paths on this machine have a space in
   them. A textarea is the only field shape that can carry them. */
.sform{margin-top:18px;border-top:1px solid var(--raised);padding-top:13px;
  display:flex;flex-direction:column;gap:7px}
.sform input,.sform textarea{font:inherit;font-size:12px;font-family:var(--mono);
  color:var(--text);background:var(--raised);border:1px solid var(--line);
  border-radius:6px;padding:7px 9px;resize:vertical}
.sform input:focus,.sform textarea:focus{outline:none;border-color:var(--accent)}
.sform button{font:inherit;font-size:12px;cursor:pointer;border-radius:8px;
  padding:8px 15px;background:transparent;border:1px solid var(--line);
  color:var(--dim);align-self:flex-start}
.sform button:hover{border-color:var(--bevel);color:var(--text-hover)}
/* #122. THE MEMORY LINE, AND IT IS NOT A NOTE. A note is grey because what
   notes say may be skimmed past; this one is the only sign a person gets that
   something entered the head of their next session, and with no approval gate
   in front of it there is no second chance to notice.
   THE GLOW RUNS ONCE AND SETTLES. `forwards` on both animations is the whole
   trick: the gradient sweeps across on arrival, the halo fades out, and what
   is left afterwards is a quiet accent-tinted row. A glow that kept pulsing
   would be a thing to switch off, and this line may not be switchable.
   THE COLOURS COME FROM THE PALETTE, never from a literal, so all three themes
   answer for it -- `--accent` is the brand value the core hands in.
   A RESTING STATE IS NOT A STOPPED ANIMATION. Until 2026-08-24 one gradient
   did both jobs, so when the sweep finished it parked where `forwards` left
   it: a bright middle between two transparent ends, which reads as two tiles
   rather than one row. The row now rests on a FLAT fill and the sweep is a
   layer of its own that fades to nothing. */
.memnote{font-size:11.5px;white-space:pre-wrap;color:var(--accent);
  display:flex;align-items:center;gap:7px;
  padding:7px 10px;border-radius:6px;border:1px solid transparent;
  position:relative;overflow:hidden;
  background-color:color-mix(in srgb,var(--accent) 13%,transparent);
  animation:memglow 1.6s ease-out 1 forwards}
/* THE MOVING LAYER, and it leaves nothing behind. */
.memnote::before{content:"";position:absolute;inset:0;pointer-events:none;
  background-image:linear-gradient(90deg,transparent 0%,color-mix(in srgb,var(--accent) 26%,transparent) 50%,transparent 100%);
  background-size:220% 100%;
  animation:memsweep .9s ease-out 1 forwards}
/* THE MARK IS BAKED IN, NOT LOADED. `docs/` is not in the package -- an
   installed Crow has cli, bin, models and templates and no docs directory at
   all, so a path into it would work on the machine it was written on and be a
   broken image everywhere else.
   IT IS A MASK, NOT A PICTURE. The file is black line art on transparent, so
   drawn as an image it is invisible on the dark theme and wrong on the crow
   one. As a mask it takes `currentColor`, which in this row is `--accent` --
   the same rule the rest of this block follows: colours come from the palette,
   never from a literal, so all three themes answer for it. */
.memicon{flex:0 0 auto;width:17px;height:17px;position:relative;
  background-color:currentColor;opacity:.92;
  -webkit-mask-image:url("__MEMICON__");mask-image:url("__MEMICON__");
  -webkit-mask-size:contain;mask-size:contain;
  -webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;
  -webkit-mask-position:center;mask-position:center}
@keyframes memsweep{from{background-position:120% 0;opacity:1}
  to{background-position:-40% 0;opacity:0}}
@keyframes memglow{
  0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 45%,transparent)}
  35%{box-shadow:0 0 14px 2px color-mix(in srgb,var(--accent) 38%,transparent)}
  100%{box-shadow:0 0 0 0 transparent}}
/* A reader who asked the system not to animate gets the colour and no motion.
   The row still has to be visible -- so the sweep is replaced by a flat tint,
   not by nothing. */
@media (prefers-reduced-motion: reduce){
  .memnote{animation:none;
    background-color:color-mix(in srgb,var(--accent) 14%,transparent)}
  .memnote::before{animation:none;opacity:0}}
/* #128. THE HELD-BACK WRITES, as a tile BEHIND the composer.
   IT IS A SIBLING OF #box AND TUCKED UNDER IT -- negative margin below, extra
   padding to pay for it, and #box lifted one layer. That is what makes it read
   as something lying behind the input rather than another row inside it, which
   is the whole shape robin asked for.
   THE LOOK IS `.memnote`'s, not a second visual language for one subject: same
   accent, same sweep gradient, same halo. `.memnote` settles after one pass
   because a save is OVER; this keeps breathing because a question is still
   true until it is answered.
   COLLAPSED IT IS TWO NUMBERS. Lines gained in green, lines lost in red -- the
   shape everyone already reads on a diff. The text of every entry is one click
   away; a tile that printed all of it would cover the chat it lies behind. */
.askcard .elicwhat{display:block;color:var(--dim);white-space:pre-wrap;
  margin:2px 0 9px;font-size:12px}
.askcard .elicfield{display:flex;align-items:baseline;gap:9px;margin:0 0 7px}
.askcard .eliclabel{flex:0 0 30%;min-width:0;color:var(--dim);font-size:11.5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.askcard .eliclabel em{font-style:normal;color:var(--bad)}
.askcard .elicfield input[type=text],.askcard .elicfield input[type=number],
.askcard .elicfield select{flex:1;min-width:0;font:inherit;font-size:11.5px;
  padding:4px 7px;border-radius:6px;border:1px solid var(--bevel);
  background:var(--sunken);color:var(--text)}
.askcard .elicfield input[type=checkbox]{margin:0}
.askcard .elichint{color:var(--dimmer);font-size:10.5px;margin:-4px 0 8px
  calc(30% + 9px)}
#pendbar{max-width:900px;margin:0 auto -14px;padding:9px 13px 22px;
  border:1px solid color-mix(in srgb,var(--accent) 32%,transparent);
  border-radius:10px;color:var(--accent);font-size:11.5px;cursor:pointer;
  background:linear-gradient(90deg,transparent 0%,color-mix(in srgb,var(--accent) 20%,transparent) 50%,transparent 100%),var(--raised);
  background-size:220% 100%,auto;
  animation:pendsweep 2.6s ease-in-out infinite, pendglow 2.6s ease-in-out infinite}
#pendbar[hidden]{display:none}
#pendbar .top{display:flex;align-items:center;gap:10px}
#pendbar .title{font-weight:600}
#pendbar .plus{color:var(--ok);font-variant-numeric:tabular-nums}
/* `--bad`, NOT `--bad-text`. The latter is #ffd9d4 -- a pale pink meant as
   TEXT ON A RED GROUND, and on this dark surface it reads as white. Robin
   saw a white minus and a green plus. The name looked right in the file and
   the colour was wrong on the screen, which is the same failure the light
   drawing had: a check that compares colour NAMES cannot see it. */
#pendbar .minus{color:var(--bad);font-variant-numeric:tabular-nums}
#pendbar .hint{color:var(--dimmer);margin-left:auto;font-size:10.5px}
/* Only while it is open, so the collapsed tile stays two lines high no matter
   how much the review wants to write. */
#pendbar .body{display:none;margin-top:9px}
#pendbar.open .body{display:block}
#pendbar .what{display:block;color:var(--dim);white-space:pre-wrap;
  word-break:break-word;margin-bottom:6px;line-height:1.4}
#pendbar .acts{display:flex;gap:8px;margin-top:9px}
#pendbar button{font:inherit;font-size:11.5px;cursor:pointer;border-radius:6px;
  padding:4px 12px;background:transparent;border:1px solid var(--line);
  color:var(--dim)}
#pendbar button.yes{color:var(--ok);border-color:rgba(78,201,143,.45)}
#pendbar button.yes:hover{background:rgba(78,201,143,.12)}
#pendbar button.no:hover{border-color:var(--bevel)}
@keyframes pendsweep{
  0%{background-position:120% 0,0 0}
  100%{background-position:-40% 0,0 0}}
@keyframes pendglow{
  0%,100%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 26%,transparent)}
  50%    {box-shadow:0 0 14px 2px color-mix(in srgb,var(--accent) 30%,transparent)}}
/* Motion off, colour stays. The tile is the only sign there is, so it may not
   vanish for the people who asked for fewer moving things. */
@media (prefers-reduced-motion: reduce){
  #pendbar{animation:none;
    background:color-mix(in srgb,var(--accent) 14%,transparent)}}
/* #130. THE INSTALL LINE, IN THE CHAT, WHERE THE COMMAND WAS TYPED. `/mcp add`
   is the one slash command that takes real time -- a process starts, a
   handshake runs, a schema comes back -- so it wears the memory gate's tile
   rather than answering out of nowhere half a second later.
   THE KEYFRAMES ARE `#pendbar`'s, NOT COPIES. Two sweeps written out twice are
   two things to fix, and they drift the first time one of them is touched.
   A FLOOR OF FOUR SECONDS (robin, 2026-08-22) lives in the page, not here: an
   answer that arrives in 300 ms would flash past and read as nothing having
   happened at all. */
.installbar{max-width:900px;margin:0 auto;padding:9px 13px;
  border:1px solid color-mix(in srgb,var(--accent) 32%,transparent);
  border-radius:10px;color:var(--accent);font-size:11.5px;
  background:linear-gradient(90deg,transparent 0%,color-mix(in srgb,var(--accent) 20%,transparent) 50%,transparent 100%),var(--raised);
  background-size:220% 100%,auto;
  animation:pendsweep 2.6s ease-in-out infinite, pendglow 2.6s ease-in-out infinite}
.installbar .top{display:flex;align-items:center;gap:10px}
.installbar .title{font-weight:600}
.installbar .hint{color:var(--dimmer);margin-left:auto;font-size:10.5px}
@media (prefers-reduced-motion: reduce){
  .installbar{animation:none;
    background:color-mix(in srgb,var(--accent) 14%,transparent)}}

/* #130. A SERVER IS ONE ROW UNTIL IT IS OPENED. One ordinary server is a dozen
   tools and already outruns the sheet; twenty servers would be a scroll nobody finishes.
   The head stays, the tools fold. */
.mcphead{cursor:pointer}
.mcphead .caret{font-size:9px;color:var(--bevel);transition:transform .12s ease}
.mcphead.open .caret{transform:rotate(90deg)}
.mcphead .count{color:var(--dimmer);font-size:10.5px;white-space:nowrap}
.mcptools{display:none}
.mcptools.open{display:block}

/* ONE LAYER UP, so the composer covers the tile's lower edge instead of the
   other way round. Without this the negative margin would only overlap them
   in source order and the tile would sit ON the box. */
#box{position:relative;z-index:1}

/* NOT A DIM NOTE, AND THAT IS THE WHOLE REASON IT IS A SECOND CLASS (#98).
   `--warn` is already `auto`'s colour in the level dropdown, so the line that
   names the limit of `auto`'s guarantee is drawn in the colour of the level it
   is about. A marker for a rare event has to look unlike the furniture around
   it, or it becomes furniture. */
.alarm{color:var(--warn);font-size:11.5px;white-space:pre-wrap;margin-top:6px}
.cursor{display:inline-block;width:7px;height:14px;background:var(--accent);
  vertical-align:-2px;margin-left:2px;animation:bl 1s steps(1,end) infinite}
@keyframes bl{50%{opacity:0}}

/* -- composer: the rounded, lifted box --------------------------------- */
/* IN THE FLOW, NOT UNDER IT. This was a docked strip with a 1px rule on top;
   the rule was the ENTIRE separation, because the strip's own
   `rgba(11,14,23,.9)` was CROW_BG's hex at 90% sitting on CROW_BG -- the same
   colour twice. Now it is lifted out of the column and drawn over the bottom of
   #flow, so text scrolls behind it the way it does in every chat window.
   RIGHT STOPS AT THE GUTTER. Full width would run the box under the scrollbar
   and cut its track; stopping at --sbw also puts this box's centre on .turn's
   centre, which the docked version got wrong by 5 px whenever a chat overflowed.
   THE FADE IS THE TOP 26px. `transparent` interpolates in premultiplied alpha
   in Chromium, so it fades to nothing rather than through grey -- and 26px of it
   is why the gap #flow keeps below is measured from offsetHeight, which
   INCLUDES the fade: the last line comes to rest above it, never inside it. */
#composer{position:absolute;left:0;right:var(--sbw);bottom:0;
  padding:26px 40px 14px;
  background:linear-gradient(to bottom,transparent,var(--bg) 26px)}
/* DAS BAND LIEGT UEBER DEM PLATZHALTER, NICHT UEBER DER ZEILE (robin,
   2026-08-23). Eine eigene Zeile machte die Maske hoeher, sobald jemand zu
   sprechen anfaengt, und schoebe alles darunter -- dieselbe Bewegung, die der
   Mikrofonknopf mit seinem Ring von Anfang an vermeidet. Absolut im `#line`,
   also kostet es keine Hoehe, und `pointer-events:none`, damit das Textfeld
   darunter anklickbar bleibt.
   GESPIEGELT UM DIE MITTE: `align-items:center` laesst jeden Balken nach oben
   UND unten wachsen, was eine Stimme ist -- vom Boden nach oben ist ein
   Balkendiagramm. Drei Pixel breit und vier Pixel Abstand, weil `flex:1` sie
   sonst ueber die ganze Zeile zieht und aus der Welle Kloetze macht.
   VOLL GERUNDET, und das ist der Unterschied zwischen einer Sprachnotiz und
   einem Diagramm: bei `border-radius:99px` ist ein lauter Balken eine Pille
   und ein leiser ein Punkt -- Stille zeichnet sich als Punktreihe, nicht als
   Luecke. Genau die Form, die robin am 2026-08-23 als Vorbild geschickt hat. */
#voice{position:absolute;inset:0;display:flex;align-items:center;gap:4px;
  pointer-events:none;overflow:hidden}
#voice i{width:3px;flex:none;height:3px;border-radius:99px;
  background:var(--accent);opacity:.45;
  transition:height .07s linear,opacity .07s linear}
/* UND DIESE ZEILE IST NICHT ZIER. Die Regel darueber ist spezifischer als das
   `display:none`, das der Browser an `[hidden]` haengt -- ohne sie stuende das
   Band immer da, und das `hidden` im Markup saehe aus, als taete es etwas. */
#voice[hidden]{display:none}
#box{border:1px solid var(--bevel);border-radius:8px;background:var(--panel);
  padding:9px 11px 8px;box-shadow:0 0 0 3px rgba(126,176,248,.06);
  transition:border-color .15s ease,box-shadow .15s ease;
  /* 900, not 960: .turn spends 30px of its 960 on padding either side, so its
     text starts at 900 wide. Matching that here puts this box's border on the
     same edge as the text above it.
     ES WAR EINEN ABEND LANG 675 (robin, 2026-08-23), also ein Viertel schmaler,
     und robin hat es am selben Abend zurueckgenommen: gesehen ist die Maske,
     die unter ihrer eigenen Spalte steht, die ruhigere. Die Zahl steht hier
     mit ihrer Geschichte, damit sie niemand ein zweites Mal probiert. */
  max-width:900px;margin-inline:auto}
/* KEIN PLATZHALTER, WAEHREND GESPROCHEN WIRD. Das Band liegt ueber der Zeile,
   also stuenden sonst beide uebereinander und die ruhenden Punkte laesen sich
   als Zeichen im Satz -- genau so sah es am 2026-08-23 bei robin aus. */
#box.rec #in::placeholder{color:transparent}
#box.focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(126,176,248,.13)}
/* THE WHOLE BOX, NOT A SEPARATE ZONE. A drop target that is smaller than the
   thing it looks like is a target people miss; the window takes a file anywhere
   and the box is what says so. */
#box.drag{border-color:var(--accent);background:rgba(126,176,248,.07);
  box-shadow:0 0 0 3px rgba(126,176,248,.20)}
/* NO PROMPT MARK. `you>` named the typist in a box only the typist can type
   in; the turn above already carries it where it says something. The gap
   went with it -- one child has nothing to be spaced from. */
#line{display:flex;position:relative;align-items:flex-start}
/* #142: staged images in the composer, and a sent one in the transcript. */
#stage{display:flex;gap:6px;flex-wrap:wrap;padding:8px 8px 0}
#stage .imgchip{position:relative}
#stage .imgchip img{height:46px;border-radius:6px;display:block;
  border:1px solid var(--bevel)}
#stage .imgchip button{position:absolute;top:-6px;right:-6px;width:16px;
  height:16px;line-height:14px;padding:0;border-radius:50%;font-size:11px;
  border:1px solid var(--bevel);background:var(--panel);color:inherit;
  cursor:pointer}
.you img.sent{display:block;max-width:min(320px,70%);border-radius:8px;
  margin-top:6px;border:1px solid var(--bevel)}
#in{flex:1;background:transparent;border:0;outline:0;resize:none;color:var(--text);
  font:inherit;font-size:13px;line-height:1.5;max-height:140px;user-select:text}
#in::placeholder{color:var(--dimmer)}
/* #138c. NICHTS RAGT AUS DER MASKE, UND ZWAR BEI JEDER BREITE.
   robin, 2026-08-26: "die Icons gucken immer noch ausserhalb der Eingabemaske".
   Der Pfeil stand rechts NEBEN dem Rahmen statt darin.

   DAS IST KEINE FRAGE DER PANEL-BREITE GEWESEN, auch wenn es so aussah. Ein
   Flex-Kind hat `min-width:auto` und schrumpft deshalb NICHT unter die Breite
   seines Inhalts; jedes Kind hier traegt zusaetzlich `white-space:nowrap`. Also
   schrumpfte keines, die Summe blieb breiter als die Zeile, und der Ueberlauf
   fiel auf das letzte Element -- `#go`. Eine breitere Spalte haette das nur
   zufaellig verdeckt und beim naechsten schmalen Fenster wieder gezeigt.

   WER NACHGIBT, IST EINE RANGFOLGE UND KEINE ZUFAELLIGKEIT: zuerst der Hinweis
   (blosser Text), dann der Modell-Chip (der laengste, "Qwen3.8-27B high
   (default)"), dann die Kontextzahl. Die Knoepfe geben NIE nach -- sie sind
   das, was angeklickt wird, und ein halber Knopf ist schlimmer als ein
   gekuerztes Wort.

   KEIN `flex-wrap`. Umbrechen wuerde die Maske hoeher machen, sobald ein Chip
   nicht passt -- und "das darf sich nicht verschieben" war die zweite Haelfte
   der Ansage. */
#foot{display:flex;align-items:center;gap:10px;margin-top:9px;font-size:11px;
  color:var(--dimmer);min-width:0}
#ctx{font-size:11.5px;white-space:nowrap;min-width:0;overflow:hidden;
  text-overflow:ellipsis;flex:0 1 auto}
#modelwrap{min-width:0;flex:0 1 auto;display:inline-flex}
/* JEDES KIND, NICHT NUR DAS ERSTE. Der Chip ist ein `inline-flex` aus zwei
   Teilen -- dem Modellnamen und dem Grad dahinter -- und eine Regel nur auf
   `b` haette den laengeren der beiden ungekuerzt gelassen.
   DIE REGEL FUER `#model` SELBST STEHT WEITER UNTEN, bei seiner Form. Eine
   zweite `#model{...}` hier war die naheliegende Stelle und die falsche:
   `test_the_chip_borrows_the_shape_of_its_new_neighbours` liest die ERSTE
   Regel dieses Selektors und fand statt der Pillenform zwei Zeilen Flexbox. */
#model>*{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#ctx .fill{color:var(--ok)} #ctx .fill.w{color:var(--warn)} #ctx .fill.b{color:var(--bad)}
#ctx .rest{color:var(--dimmer)}
#ctx .n{color:var(--dim);margin-left:5px}
/* STRETCH, NOT CENTER, AND THAT IS THE WHOLE FIX. Four controls sat here at
   four heights because each one was as tall as whatever it happened to hold:
   the level and the folder are a 11.5px line box at the inherited 1.55 factor
   (25.83px with padding and border), the arrow is 14px at 1.2 (22.8), and the
   microphone is an SVG that declares height="13" (19). Pinning a number on
   each would be the fix that goes stale the first time a font-size moves --
   `stretch` is the initial value of align-items for a reason: the row gets ONE
   height, from its tallest control, and the rest adopt it. */
#acts{margin-left:auto;display:flex;gap:8px;align-items:stretch;
  min-width:0;flex:0 1 auto}
/* #138c. DIE KNOEPFE GEBEN NICHT NACH. Sie sind das Angeklickte, und ein
   halber Knopf ist schlimmer als ein gekuerztes Wort -- also schrumpft in
   dieser Reihe nur der Hinweis. */
#acts>#rootwrap,#acts>#modewrap,#acts>#mic,#acts>#go{flex:none}
/* CENTRED, NOT STRETCHED: it is a bare word with no border to line up, and a
   stretched span puts its text at the top of the box instead of on the row. */
#hint{color:var(--dimmer);font-size:10.5px;align-self:center;
  /* #138c. GIBT ALS ERSTES NACH, bis auf null. Er sagt "read timeout 600 s"
     oder worauf eine gepufferte Zeile wartet -- entbehrlich neben einem Knopf,
     den jemand treffen muss. */
  min-width:0;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
/* A FLEX BOX SO THE GLYPH STAYS ON THE CENTRE LINE once #acts stretches this
   button past its own line box. Left as a plain button it would grow at the
   bottom and the arrow would ride high in it. */
#go{font:inherit;font-size:11.5px;cursor:pointer;border-radius:6px;
  padding:3px 13px;background:transparent;border:1px solid var(--line);
  color:var(--dim);display:flex;align-items:center;justify-content:center}
/* THE ARROW IS THE LABEL, so the button is one glyph wide plus its padding --
   about half of what "send" needed. The larger size applies only while idle:
   "Stop" keeps 11.5px, because a word set at 14px would grow the button in the
   one state where it is already the wider of the two. */
#go:not(.stop){font-size:14px;line-height:1.2;padding:2px 11px}
#go:hover{border-color:var(--bevel);color:var(--accent)}
#go.stop{color:var(--bad-text);background:rgba(240,101,90,.10);
  border-color:rgba(240,101,90,.45)}
#go.stop:hover{background:rgba(240,101,90,.18)}

/* -- the microphone, between the level and the arrow ------------------- */
/* IT BORROWS #go's SHAPE rather than inventing one. The two sit against each
   other, and a neighbour with its own radius and padding reads as an accident.
   The glyph is an inline SVG because the mono stack has no microphone: a font
   that lacks it would draw a box, and an emoji would drag its own colour into
   a row that takes every colour from state. `currentColor` means the drawing
   follows the button, including into the recording red. */
#mic{font:inherit;cursor:pointer;border-radius:6px;padding:2px 10px;
  background:transparent;border:1px solid var(--line);color:var(--dim);
  display:flex;align-items:center;
  transition:color .15s ease,border-color .15s ease,background .15s ease}
#mic:hover:not([disabled]){border-color:var(--bevel);color:var(--accent)}
#mic[disabled]{cursor:default;color:var(--dimmer);border-color:var(--line-soft)}
/* TWO STATES AND NO THIRD. Grey is snoozed, blue glow is recording -- robin's
   rule, and both halves of it were earned. The first build painted recording
   RED, which is the colour every conference tool on the machine uses for MUTE:
   the one signal that had to be unambiguous said the opposite of what was
   happening. It also had a third, yellow state while the recogniser worked, and
   a state nobody can act on is furniture with a colour.
   THE GLOW, NOT A SWAPPED GLYPH: a breathing ring is visible across the room
   while the icon stays the icon it was, and `box-shadow` spreads past the border
   without touching layout, so the row beside it does not move while someone
   speaks. */
#mic.rec{color:var(--accent);border-color:rgba(126,176,248,.55);
  background:rgba(126,176,248,.10);animation:micglow 1.8s ease-in-out infinite}
@keyframes micglow{
  0%,100%{box-shadow:0 0 0 0 rgba(126,176,248,.32)}
  50%    {box-shadow:0 0 0 6px rgba(126,176,248,0)}}

/* -- #88: one held-back call, put to the user -------------------------- */
.askcard{border:1px solid rgba(229,192,75,.40);border-radius:10px;
  background:rgba(229,192,75,.05);padding:11px 13px}
.asktop{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.asktop b{color:var(--gold);font-weight:600}
.asktop code{color:var(--dim);font-size:11.5px;word-break:break-all}
.askrow{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.askrow button{font:inherit;font-size:11.5px;cursor:pointer;border-radius:6px;
  padding:4px 12px;background:transparent;border:1px solid var(--line);
  color:var(--dim)}
.askrow button.yes{color:var(--ok);border-color:rgba(78,201,143,.45)}
.askrow button.yes:hover{background:rgba(78,201,143,.12)}
.askrow button.no{color:var(--bad-text);border-color:rgba(240,101,90,.45)}
.askrow button.no:hover{background:rgba(240,101,90,.12)}
.askrow button.always em{font-style:normal;color:var(--accent)}
.askrow button:hover{border-color:var(--bevel)}
.askdone{color:var(--dimmer);font-size:11px}

/* -- #88: the release level, beside send ------------------------------- */
/* THE COLOUR IS THE STATE. robin's three: manual white, allowedit green,
   auto yellow -- brightest where the least is held back, because the level
   that runs a shell unasked is the one worth noticing across the room. */
/* FLEX SO THE BUTTON FILLS IT. The wrapper is what #acts stretches; without
   this the button inside keeps its own height and the stretch stops at a
   transparent div. The menu is position:absolute and is not a flex item, so
   it is untouched by this. */
#modewrap{position:relative;display:flex}
#mode{font:inherit;font-size:11.5px;cursor:pointer;border-radius:6px;
  padding:3px 11px;background:transparent;border:1px solid var(--line);
  color:var(--dim);display:flex;align-items:center;gap:6px}
#mode:hover{border-color:var(--bevel)}
#mode .dot{width:7px;height:7px;border-radius:50%;background:currentColor;
  flex:none}
#mode[data-mode="manual"]{color:var(--text-strong);border-color:var(--bevel)}
#mode[data-mode="allowedit"]{color:var(--ok);border-color:rgba(78,201,143,.45)}
#mode[data-mode="auto"]{color:var(--gold);border-color:rgba(229,192,75,.45)}

/* UPWARDS, because the composer sits at the bottom of the window: a menu
   that opened downwards would be drawn outside it. */
#modemenu{position:absolute;bottom:calc(100% + 6px);right:0;min-width:266px;
  background:var(--panel);border:1px solid var(--bevel);border-radius:8px;
  padding:5px;box-shadow:0 8px 26px var(--shadow);z-index:40}
#modemenu[hidden]{display:none}
#modemenu button{display:block;width:100%;text-align:left;font:inherit;
  font-size:11.5px;cursor:pointer;background:transparent;border:0;
  border-radius:6px;padding:7px 9px;color:var(--dim)}
#modemenu button:hover{background:rgba(126,176,248,.10)}
#modemenu button b{display:block;font-weight:600;font-size:12px}
#modemenu button .what{color:var(--dimmer);font-size:10.5px}
#modemenu button[data-mode="manual"] b{color:var(--text-strong)}
#modemenu button[data-mode="allowedit"] b{color:var(--ok)}
#modemenu button[data-mode="auto"] b{color:var(--gold)}
#modemenu button .tick{float:right;color:var(--accent)}

/* #92: the working directory, beside the level and deliberately quieter than
   it. The level is the loud control -- it decides whether a shell runs unasked;
   the boundary decides where, and only ever refuses. Same shape so the two read
   as one row of controls, no colour of its own so it does not compete. */
#rootwrap{position:relative;display:flex}
#root{font:inherit;font-size:11.5px;cursor:pointer;border-radius:6px;
  padding:3px 11px;background:transparent;border:1px solid var(--line);
  color:var(--dimmer);max-width:150px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
#root:hover{border-color:var(--bevel)}
/* UNBOUND IS THE STATE THAT MUST BE VISIBLE, so it is the dashed one: a solid
   quiet button reads as "set and fine", and "no folder" is neither. */
#root[data-bound="0"]{border-style:dashed}
#root[data-bound="1"]{color:var(--dim)}

#rootmenu{position:absolute;bottom:calc(100% + 6px);right:0;min-width:300px;
  max-width:420px;background:var(--panel);border:1px solid var(--bevel);
  border-radius:8px;padding:5px;box-shadow:0 8px 26px var(--shadow);z-index:40}
#rootmenu[hidden]{display:none}
#rootmenu .head{color:var(--dimmer);font-size:10px;text-transform:uppercase;
  letter-spacing:.06em;padding:5px 9px 3px}
#rootmenu .none{color:var(--dimmer);font-size:11px;padding:2px 9px 7px}
#rootmenu .sep{height:1px;background:var(--line);margin:4px 6px}
#rootmenu button{display:block;width:100%;text-align:left;font:inherit;
  font-size:11.5px;cursor:pointer;background:transparent;border:0;
  border-radius:6px;padding:7px 9px;color:var(--dim)}
#rootmenu button:hover{background:rgba(126,176,248,.10)}
#rootmenu button b{display:block;font-weight:600;font-size:12px;color:var(--text)}
/* The full path wraps rather than truncates: a path with the middle cut out is
   one the reader cannot check, and checking it is the entire purpose here. */
#rootmenu button .what{color:var(--dimmer);font-size:10.5px;
  word-break:break-all;line-height:1.35}
#rootmenu button .tick{float:right;color:var(--accent)}
/* #115. The model chip becomes a picker, and it borrows #rootmenu's rules
   rather than inventing a second look -- one menu shape for one kind of
   decision. It opens DOWNWARD because this chip sits in the top bar while the
   root button sits at the bottom; everything else here is the same list. */
.chipwrap{position:relative;display:inline-block}
.chip.pick{cursor:pointer}
.chip.pick:hover{border-color:var(--bevel)}
/* #119. IT BORROWS #mode's SHAPE, not the bar's. `.chip` is a pill because the status bar is a
   row of pills; this control now stands in the composer beside #root and #mode, and the rule the
   microphone was built under applies unchanged: a neighbour with its own radius and padding
   reads as an accident. Same 6px, same 3px/11px, same 11.5px as the two on the other side. */
#model{border-radius:6px;padding:3px 11px;font-size:11.5px;gap:0;
  /* #138c. DER LAENGSTE TEXT DER REIHE gibt nach, damit die Knoepfe rechts
     nicht aus der Maske gedraengt werden. */
  min-width:0;overflow:hidden}
/* THE LEVEL IS DIMMER THAN THE MODEL because it is the setting, not the subject -- the same
   split #modelmenu draws between a model row and the level rows under it. */
#model .lvl{color:var(--dimmer)}
/* #117 LEFT ITS SLIDER HERE AND #119 TOOK THE SECOND PANEL WITH IT. There were two menus with
   one rule set; now there is one menu with two kinds of row, because a thinking level was never
   a second subject -- it is how the model in the row above it thinks.
   UPWARDS, because this chip moved into the composer at the bottom of the window. A menu that
   opened downwards from there would be drawn past the edge; #modemenu and #rootmenu beside it
   have opened this way since they got there. */
#modelmenu{position:absolute;bottom:calc(100% + 6px);left:0;min-width:300px;
  background:var(--panel);border:1px solid var(--line);border-radius:9px;
  padding:5px 0;z-index:40;box-shadow:0 10px 26px var(--shadow)}
#modelmenu[hidden]{display:none}
#modelmenu .head{color:var(--dimmer);font-size:10px;text-transform:uppercase;
  letter-spacing:.08em;padding:4px 9px 5px}
#modelmenu .none{color:var(--dimmer);font-size:11px;padding:2px 9px 7px}
#modelmenu button{display:block;width:100%;text-align:left;font:inherit;
  background:none;border:0;color:var(--text);padding:5px 9px;cursor:pointer}
#modelmenu button:hover{background:rgba(126,176,248,.10)}
#modelmenu button b{display:block;font-weight:600;font-size:12px;color:var(--text)}
#modelmenu button .what{color:var(--dimmer);font-size:10.5px}
#modelmenu button .tick{float:right;color:var(--accent)}
/* THE INDENT IS THE WHOLE CLAIM OF A SUBMENU, so it has to be unmistakable at a glance: a level
   row is a child of the model above it and does nothing to any other model. The rule is drawn
   rather than implied -- a border down the left says "these belong to that one" without a second
   panel to clip against the window edge.
   SMALLER, DIMMER, NO BOLD: the model is the decision, the level is a setting inside it. Making
   the two look alike is what made robin ask for one control instead of two. */
#modelmenu button.lvlrow{padding-left:26px;position:relative}
#modelmenu button.lvlrow b{font-weight:500;font-size:11.5px;color:var(--text-hi)}
#modelmenu button.lvlrow::before{content:"";position:absolute;left:15px;top:0;bottom:0;
  width:1px;background:var(--line)}
#modelmenu button.lvlrow:last-child::before{bottom:50%}
/* ONLY THE RUNNING MODEL CARRIES LEVELS, and the reason is in the payload rather than in taste:
   `levels` and `groups` are measured for the model that ANSWERED the probe, so they describe one
   model and no other. Hanging them under the row that would boot 17 GB would be inventing them
   for a model nobody has asked a question -- and #116's rule is that nothing is invented. */
/* ---- #143 delegation. One card per subtask in the flow, a child row per
   subtask in the rail, one chip over the composer -- all fed by the same
   `subs` snapshot, all in the --sub channel, and VISIBLE FROM THE START:
   the running state is the one that must be seen, not the finished one. */
.subcard{border:1px solid var(--line);border-left:3px solid var(--sub);border-radius:10px;
  background:var(--panel);padding:10px 14px;margin:0 0 10px;max-width:760px}
.subcard .shead{display:flex;align-items:center;gap:9px;font-size:12.5px;flex-wrap:wrap}
.subcard .glyph{color:var(--sub);font-weight:700}
.subcard .sname{font-family:var(--mono);font-size:11.5px;color:var(--dim)}
/* robins letzte Kartenform 2026-08-29: klassische Kopfzeile, Task darunter;
   der Output bleibt zu, die KARTE ist die Klickflaeche. */
.subcard .stask{margin:5px 0 0;font-size:13px;color:var(--text-soft);white-space:pre-wrap}
.subcard.can{cursor:pointer}
.subcard .sstat{margin-left:auto;display:flex;align-items:center;gap:7px;
  font-family:var(--mono);font-size:11px;color:var(--dimmer)}
.subcard .sstat .okword{color:var(--ok)}
.subcard .sstat .badword{color:var(--bad)}
/* robins finale Fassung 2026-08-28 nachts: KEINE Flaechenanimation -- die
   Karte steht still, nur ihr linker Balken lebt: ein Bernstein-Verlauf
   (--warn, die Palette in allen drei Themes), der von oben nach unten
   durchatmet. Der Verlauf ist kachelbar (hell-dunkel-hell), darum wandert
   die Position nahtlos. Fertig faellt die Klasse und der stille --sub-Rand
   der Basisregel steht wieder.
   Nachtrag 2026-08-29: der Balken sitzt EXAKT wie dieser Done-Rand. Als
   freistehender 3px-Streifen voller Hoehe stand er an den Ecken ueber die
   Kontur (ein 10px-Radius kollabiert bei 3px Breite auf 3px); darum spannt
   das Pseudo jetzt die ganze Kartenkontur auf und eine Mask zeigt nur die
   linke Randspalte -- gleiche Silhouette, gleiche Spitzen wie der echte
   Rand. pointer-events:none, denn die Karte selbst ist die Klickflaeche. */
.subcard.run{position:relative;border-left-color:transparent}
.subcard.run::before{content:"";position:absolute;
  top:-1px;right:-1px;bottom:-1px;left:-3px;
  border-radius:10px;pointer-events:none;
  background:linear-gradient(180deg,
    color-mix(in srgb,var(--warn) 90%,transparent) 0%,
    color-mix(in srgb,var(--warn) 20%,transparent) 50%,
    color-mix(in srgb,var(--warn) 90%,transparent) 100%);
  background-size:100% 300%;
  -webkit-mask:linear-gradient(90deg,#000 3px,transparent 3px);
  mask:linear-gradient(90deg,#000 3px,transparent 3px);
  animation:subflow 2.6s ease-in-out infinite}
@keyframes subflow{0%{background-position:0 0}100%{background-position:0 300%}}
/* Motion off: der Balken steht bernstein, die Aussage bleibt. */
@media (prefers-reduced-motion: reduce){
  .subcard.run::before{animation:none;
    background:color-mix(in srgb,var(--warn) 60%,transparent)}}
.subcard .sresult{font-size:12.5px;color:var(--text-soft);margin-top:8px;white-space:pre-wrap;
  border-left:2px solid var(--line);padding-left:12px;overflow-wrap:anywhere}
/* Its own dot class: `.dot` already belongs to the mode chip, and a shared
   name would hand the pulse to a control that must not breathe. */
.sdot{width:7px;height:7px;border-radius:50%;flex:none}
.sdot.run{background:var(--sub);animation:subpulse 1.2s ease-in-out infinite}
.sdot.ok{background:var(--ok)}
.sdot.bad{background:var(--bad)}
@keyframes subpulse{0%,100%{opacity:1}50%{opacity:.25}}
.subchat{display:flex;align-items:center;gap:7px;margin:1px 0 1px 18px;padding:4px 8px;
  width:calc(100% - 18px);background:none;border:0;border-left:1px solid var(--line);
  border-radius:0 6px 6px 0;font:inherit;font-size:11.5px;color:var(--dim);
  text-align:left;cursor:default}
.subchat[data-open]{cursor:pointer}
.subchat:hover{background:var(--panel)}
.subchat .glyph{color:var(--sub);font-weight:600}
/* THE TASK IS THE ROW'S SUBJECT and keeps the space: measured 2026-08-27 in
   the 236px rail, an unclamped mono model slug ate the whole line and the
   title rendered as nothing. The model may truncate; the task may not vanish. */
.subchat .stitle{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  flex:1 1 auto;min-width:0}
.subchat .who{color:var(--dimmer);font-family:var(--mono);font-size:10.5px;
  margin-left:auto;flex:0 1 auto;max-width:38%;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
#subwrap{position:relative;display:inline-flex}
#subwrap[hidden]{display:none}
/* robin, 2026-08-27: ohne AKTIVE Subtasks zeigt der Chip 0 und traegt den
   gedimmten Rahmen jedes ruhenden Chips; hell (--sub) plus blinkender Punkt
   NUR solange etwas laeuft. Der Zustand ist die Klasse `live`. */
#subchip{cursor:pointer;display:inline-flex;gap:6px;align-items:center}
#subchip.live{border-color:var(--sub);color:var(--sub)}
#submenu{position:absolute;bottom:calc(100% + 8px);left:0;width:280px;
  background:var(--panel);border:1px solid var(--line);border-radius:9px;
  padding:5px;box-shadow:0 10px 26px var(--shadow);z-index:40}
#submenu[hidden]{display:none}
#submenu .row{display:flex;align-items:center;gap:8px;padding:7px 9px;border-radius:6px;
  font-size:12px;color:var(--text-soft);width:100%;background:none;border:0;
  font:inherit;text-align:left;cursor:default}
#submenu .row[data-open]{cursor:pointer}
#submenu .row:hover{background:var(--hover)}
#submenu .row .stitle{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#submenu .row .who{margin-left:auto;font-family:var(--mono);font-size:10.5px;
  color:var(--dimmer);flex:none}
#submenu .hint{font-size:10.5px;color:var(--dimmer);padding:5px 9px 3px;
  border-top:1px solid var(--line);margin-top:3px}
.cost .subshare{color:var(--sub)}
.tool.sub .ico{color:var(--sub)}
</style></head><body data-rail="__RAIL__" data-code="__CODE__">

<div id="bar" class="pywebview-drag-region" ondblclick="pywebview.api.maximise()">
  <!-- #119. LEFT OF THE WORDMARK, and it has to live in the TITLE BAR rather
       than in the rail head: a button inside the rail goes away with the rail,
       and then there is no way back. The glyph is the side-panel mark from
       robin's reference -- a frame with one column filled, which is the panel
       it toggles. Inline SVG for the reason the microphone is: the system font
       has no such character, and an emoji would drag its own colour in. -->
  <button id="railtoggle" class="pywebview-no-drag" onclick="crow.toggleRail()"
          title="Show or hide the chat rail">
    <svg viewBox="0 0 20 20" width="15" height="15" fill="none"
         stroke="currentColor" stroke-width="1.6" aria-hidden="true">
      <rect x="2.5" y="4" width="15" height="12" rx="2"></rect>
      <line x1="8" y1="4" x2="8" y2="16"></line></svg></button>
  <span id="mark">CR<span>O</span>W</span>
  <div id="helpwrap" class="pywebview-no-drag">
    <button id="help" onclick="crow.helpMenu()">Help</button>
    <div id="helpmenu" hidden>
      <button onclick="crow.openSettings()">Settings</button>
    </div>
  </div>
  <!-- #138. LINKS VON DEN FENSTERKNOEPFEN, aus dem Grund, den #119 fuer den
       Rail-Knopf aufgeschrieben hat: ein Knopf im Panel geht mit dem Panel weg,
       und dann gibt es keinen Weg zurueck. Das Zeichen ist robins Terminal-Marke
       aus dem Noun Project, aber NACHGEZEICHNET statt eingebettet -- die Datei
       besteht aus gefuellten Pfaden ohne jeden Strich, und `stroke-width` haette
       dort nichts, worauf es wirken koennte. Als Striche traegt es dieselbe 1.6
       wie der Rail-Knopf daneben, nimmt `currentColor` wie alles hier, und
       braucht keine Namensnennung. Derselbe Rahmen wie links, nur ist innen ein
       Prompt statt einer Spalte. -->
  <button id="codetoggle" class="pywebview-no-drag" onclick="crow.toggleCode()"
          title="Show or hide the code panel">
    <svg viewBox="0 0 20 20" width="15" height="15" fill="none"
         stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <rect x="2.5" y="4" width="15" height="12" rx="2"></rect>
      <path d="M6.2 8.6 8.6 11 6.2 13.4"></path>
      <line x1="10.8" y1="13.4" x2="14" y2="13.4"></line></svg></button>
  <div id="wbtns" class="pywebview-no-drag">
    <div class="wb" onclick="pywebview.api.minimise()">&#8211;</div>
    <div class="wb" onclick="pywebview.api.maximise()">&#9633;</div>
    <div class="wb close" onclick="pywebview.api.close()">&#10005;</div>
  </div>
</div>

<div id="settings" hidden onclick="crow.settingsBackdrop(event)">
  <div class="sheet">
    <div class="shead">
      <h2>Settings</h2>
      <button class="sclose" onclick="crow.closeSettings()" title="close">&#10005;</button>
    </div>
    <div class="sbody">
      <nav id="scats">
        <button class="on" data-cat="look" onclick="crow.settingsCat('look')">Appearance</button>
        <button data-cat="skills" onclick="crow.settingsCat('skills')">Skills</button>
        <button data-cat="server" onclick="crow.settingsCat('server')">Server</button>
        <button data-cat="mcp" onclick="crow.settingsCat('mcp')">MCPs</button>
        <button data-cat="model" onclick="crow.settingsCat('model')">Model</button>
        <button data-cat="openrouter" onclick="crow.settingsCat('openrouter')">OpenRouter</button>
        <button data-cat="subs" onclick="crow.settingsCat('subs')">Subscriptions</button>
        <button data-cat="keys" onclick="crow.settingsCat('keys')">API Keys</button>
        <button data-cat="about" onclick="crow.settingsCat('about')">About</button>
      </nav>
      <div id="spane">
        <section data-cat="look">
          <h3>Theme</h3>
          <div id="themes">
            <button data-theme="dark"  onclick="crow.setTheme('dark')">Dark</button>
            <button data-theme="light" onclick="crow.setTheme('light')">Light</button>
            <button data-theme="crow"  onclick="crow.setTheme('crow')">Crow</button>
          </div>
        </section>
        <section data-cat="server" hidden>
          <h3>Server</h3>
          <div class="srow">
            <div class="stext">
              <div class="sname">Connection</div>
              <div class="sdesc" id="conn" title="…"><span id="dot"></span><span
                   id="state">connecting …</span></div>
            </div>
          </div>
          <div class="srow">
            <div class="stext">
              <div class="sname">Tool calls</div>
              <div class="sdesc" id="tools"></div>
            </div>
            <button class="sw" id="toolsw"></button>
          </div>
        </section>
        <section data-cat="skills" hidden>
          <h3>Skills</h3>
          <p class="shint">What Crow has worked out and kept. Switching one off
             takes it out of the prompt; the file stays.</p>
          <div id="skilllist"></div>
        </section>
        <section data-cat="mcp" hidden>
          <h3>MCPs</h3>
          <div id="mcpproblems"></div>
          <div class="mcpsaid" id="mcpsaid"></div>
          <div id="mcplist"></div>
          <div class="sform">
            <input id="mcpline"
                   placeholder="npx -y @modelcontextprotocol/server-github or https://mcp.example.com/mcp">
            <button onclick="crow.addMcp()">Add</button>
          </div>
        </section>
        <section data-cat="model" hidden>
          <h3>Model</h3>
          <p class="shint">Where a turn goes. This machine, or a provider you
             brought a key for.</p>
          <div class="fold open" id="provfold" onclick="crow.foldPane('prov')">
            <span class="caret">&#9654;</span>
            <div class="sname">Provider</div>
            <div class="count" id="provcount"></div>
          </div>
          <div class="foldbody open" id="provbody"></div>
          <div class="fold open" id="modfold" onclick="crow.foldPane('mod')">
            <span class="caret">&#9654;</span>
            <div class="sname">Model</div>
            <div class="count" id="modcount"></div>
          </div>
          <div class="foldbody open" id="modbody"></div>
          <p class="mcpsaid" id="provsaid"></p>
          <p class="mcpcost" id="provnote"></p>
        </section>
        <!-- robins Korrektur vom 2026-08-28: der Broker KOMPLETT raus aus der
             Model-Seite, auf eine eigene. Sein Schalter parkt nichts anderes:
             die Maschine antwortet weiter, waehrend die Delegation hier
             konfiguriert wird -- beide laufen parallel. -->
        <section data-cat="openrouter" hidden>
          <h3>OpenRouter</h3>
          <p class="shint">The broker, on its own page. Its switch moves no
             turn — the machine keeps answering while delegation uses what is
             set here.</p>
          <div id="orbody"></div>
          <p class="mcpsaid" id="orsaid"></p>
        </section>
        <section data-cat="subs" hidden>
          <h3>Subscriptions</h3>
          <p class="shint">Sign in with an account you already pay for. The
             browser opens at the provider; Crow keeps what comes back.</p>
          <div id="subs"></div>
          <p class="mcpsaid" id="subsaid"></p>
        </section>
        <section data-cat="keys" hidden>
          <h3>API Keys</h3>
          <p class="shint">One key per provider. It is kept in its own file that
             no view reads back — what a box shows after that is a mask.</p>
          <div id="keylist"></div>
          <p class="mcpsaid" id="keysaid"></p>
        </section>
        <section data-cat="about" hidden>
          <h3>About</h3>
          <p class="about">CROW <span id="aboutver"></span></p>
          <p class="mcpsaid" id="updsaid"></p>
          <button id="updbtn" hidden onclick="crow.updateRun()"></button>
          <p class="shint">An update replaces the installed copy under
             %LOCALAPPDATA%\Crow. Crow keeps running the version it started
             with, so it has to be restarted afterwards.</p>
        </section>
      </div>
    </div>
  </div>
</div>

<!-- #119: EMPTY, AND BUILT PER OPEN. It used to be three fixed buttons because
     there was one thing to right-click. There are three now -- a chat, a project
     heading, the empty rail below them -- and each offers different things, so
     the rows are drawn from a plan. Project names are folder names off the disk,
     which is why they go in by textContent and never into an HTML string; the
     same rule modelMenu is built under. -->
<div id="menu"></div>

<div class="grip" id="g-n"></div><div class="grip" id="g-s"></div>
<div class="grip" id="g-w"></div><div class="grip" id="g-e"></div>
<div class="grip" id="g-nw"></div><div class="grip" id="g-ne"></div>
<div class="grip" id="g-sw"></div><div class="grip" id="g-se"></div>

<!-- #127. A TEMPLATE, NOT A HIDDEN DIV. `hello()` builds its block fresh every
     time it is called, so the drawing has to be clonable rather than moved --
     moved, the second greeting of a session would find it gone. Both
     backgrounds ship; the stylesheet picks, so switching theme needs no
     JavaScript and no second copy of which theme is live. -->
<template id="marktpl">
  <div class="mk mk-dark">__MARKDARK__</div>
  <div class="mk mk-light">__MARKLIGHT__</div>
</template>
<div id="body">
  <aside id="rail">
    <div id="railhead"><h2>Chats</h2>
      <button id="new" onclick="crow.reset()">new</button></div>
    <div id="sessions"></div>
    <button id="archbar" onclick="crow.toggleArchive()">
      <span class="caret">&#9654;</span>Archive<span class="count"></span></button>
    <div id="arch"></div>
  </aside>
  <!-- #132. Der Griff zwischen Rail und Chat. Ein eigenes Element und nicht ein
       `resize` auf der Rail: CSS-resize zeichnet einen Anfasser in die Ecke und
       kennt weder Minimum noch Gedaechtnis. -->
  <div id="railgrip" onmousedown="crow.railDrag(event)" title="drag to resize"></div>
  <div id="main">
    <!-- TWO CHIPS, AND THE ADDRESS IS NOT ONE OF THEM. The bar carried five:
         state, model, level, n_ctx and the base URL. Three of them said things
         that belong where the typing happens -- the model and its level are a
         choice, and the window size is already the denominator of the context
         readout in the composer. The URL is not a choice at all: it is the one
         fact you look up when something is wrong, so it is the connected
         chip's title and costs no width until asked for. -->
    <!-- #131. TOOL CALLS DO NOT BELONG IN THE READING COLUMN. A turn of 24
         rounds put 24 rows between the question and the answer, and what a
         person came back for was the answer. They are collected here instead:
         one tile, per chat, always present so there is somewhere to look even
         before the first call. ABSOLUTE, so it does not scroll away from the
         reader who wants it. -->
    <div id="flow"></div>
    <div id="composer">
      <div id="pendbar" hidden onclick="crow.pendToggle(event)"></div>
      <div id="box">
        <!-- #142. One chip per staged image, drawn from what stage_image
             returns. Hidden while empty so the composer keeps its height. -->
        <div id="stage" hidden></div>
        <div id="line"><textarea id="in" rows="1"
            placeholder="Message, or /tools for what the model can call"></textarea>
          <div id="voice" hidden></div></div>
        <div id="foot">
          <span id="ctx"></span>
          <!-- BESIDE THE NUMBER IT DECIDES. The model sets the window the
               context is measured against, and the level sets what a turn
               costs inside it; both belong next to the readout rather than in
               a bar at the other end of the screen. ONE CHIP, NOT TWO: the
               level is not a second subject, it is how the model thinks, so
               it is a submenu under the model that runs -- see modelMenu. -->
          <span class="chipwrap" id="modelwrap"><span class="chip pick" id="model" hidden
                onclick="crow.modelMenu()" title="model and thinking level"></span>
            <div id="modelmenu" hidden></div></span>
          <!-- #143. The subtask chip: beside the model chip, visible as soon
               as anything was delegated, pulsing while anything runs. Click
               opens the list; a finished row jumps into its child chat. -->
          <span class="chipwrap" id="subwrap" hidden><span class="chip" id="subchip"
                onclick="crow.subsMenu()" title="delegated subtasks"></span>
            <div id="submenu" hidden></div></span>
          <span id="turnstate"></span>
          <div id="acts"><span id="hint"></span>
            <div id="rootwrap">
              <button id="root" data-bound="0" onclick="crow.rootMenu()">no folder</button>
              <div id="rootmenu" hidden></div>
            </div>
            <div id="modewrap">
              <button id="mode" data-mode="auto" onclick="crow.modeMenu()"
                      title="release level for tool calls">
                <span class="dot"></span><span id="modename">auto</span></button>
              <div id="modemenu" hidden></div>
            </div>
            <button id="mic" onclick="crow.mic()" title="dictate">
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none"
                   stroke="currentColor" stroke-width="2" stroke-linecap="round"
                   stroke-linejoin="round" aria-hidden="true">
                <rect x="9" y="2" width="6" height="11" rx="3"></rect>
                <path d="M5 10a7 7 0 0 0 14 0"></path>
                <line x1="12" y1="17" x2="12" y2="21"></line></svg></button>
            <button id="go" onclick="crow.go()" title="send">&#8593;</button></div>
        </div>
      </div>
    </div>
  </div>
  <!-- #138. Der Griff zwischen Chat und Code, gespiegelt zu #railgrip und aus
       demselben Grund ein eigenes Element. -->
  <div id="codegrip" onmousedown="crow.codeDrag(event)" title="drag to resize"></div>
  <!-- #138. DAS CODE-PANEL. Es haelt, was ein Werkzeug gerade schreibt, und die
       Liste der Aufrufe -- eine Flaeche, nicht zwei: die Aufrufe SIND der Index
       zu dem, was hier steht, und nebeneinander waeren beide schmaler. -->
  <aside id="code">
    <div id="codehead">
      <h2>Code</h2><span class="n"></span>
      <!-- #138b. LEERT ALLES, was in diesem Panel steht -- Quelltext UND
           Aufrufe. Der `clear` in der Gruppe leert nur die Aufrufe; wer den
           Verlauf loswerden will, meint beides und soll dafuer nicht zweimal
           an zwei Stellen klicken. -->
      <button id="codewipe" onclick="crow.codeWipe(event)"
              title="clear everything in this panel">clear all</button>
      <button id="codecopy" onclick="crow.codeCopy(event)"
              title="copy what is shown">copy</button>
    </div>
    <div id="codebody">
      <!-- #138b. DIE AUFRUFE ZUERST, DER QUELLTEXT DARUNTER, und die
           Reihenfolge ist eine Entscheidung von robin am 2026-08-26.
           Umgekehrt gebaut wanderte die Klappe mit jeder geschriebenen Datei
           weiter nach unten: sie ist der INDEX, und ein Index, den man suchen
           muss, ist keiner. Der Quelltext ist das Lange und darf wachsen. -->
      <div id="toolcalls" class="shut">
        <div class="tchd" onclick="crow.toolsToggle()">
          <span class="tct">Tool-Calls</span><span class="tcn"></span>
          <span class="tcx">+</span>
        </div>
        <div class="tcbody">
          <div id="tclist"></div>
          <button class="tcclear" onclick="crow.toolsClear(event)">clear</button>
        </div>
      </div>
      <!-- #138b. UNTER EIGENEM NAMEN. Vorher lagen die Bloecke jedes Werkzeugs
           hier, `web_search` neben `write_file`, und das Panel beantwortete
           nicht mehr die Frage, wofuer es da ist: was hat dieser Zug an meinem
           Programm geaendert. -->
      <section id="codefiles" hidden>
        <div class="cfh">Program code</div>
        <div id="cflist"></div>
      </section>
    </div>
  </aside>
</div>

<script>
const $ = s => document.querySelector(s);
const flow = $("#flow"), input = $("#in"), go = $("#go"), box = $("#box");

// #138. FARBE JE SPRACHE, eingebettet und ohne Abhaengigkeit.
//
// WARUM NICHT highlight.js. Die Seite laedt nichts von aussen -- Crow wird
// offline installiert und redet mit einem lokalen Server. Eine Bibliothek
// muesste also mitreisen: rund 30 kB minifiziert fuer eine Handvoll Sprachen,
// dazu ihre Lizenz und ihre Aktualisierungen, in einer Datei, die sonst aus
// lesbaren Zeilen besteht. Fuer das, was hier gebraucht wird -- Kommentar,
// Zeichenkette, Zahl, Schluesselwort -- sind es achtzig Zeilen.
//
// EINE ZUSAMMENGESETZTE REGEX UND NICHT ZEICHENWEISE. Ein `slice` je Zeichen
// waere quadratisch; ein Block von 37 kB, wie ihn robins Landingpage erzeugt
// hat, braeuchte damit Milliarden Zeichenkopien. So laeuft es einmal durch.
//
// DIE REIHENFOLGE IST DIE ENTSCHEIDUNG: Kommentar und Zeichenkette stehen vorn,
// also gewinnt, was zuerst beginnt. Ein `#` in einer URL faerbt damit nicht den
// Rest der Zeile, und ein Apostroph in `# don't` oeffnet keine Zeichenkette.
//
// NUR NICHT-EINFANGENDE GRUPPEN in den Teilen, sonst verschieben sich die
// Indizes, an denen die Klasse abgelesen wird.
const HL = {
  alias: {py:"python", python3:"python", js:"javascript", jsx:"javascript",
          ts:"javascript", tsx:"javascript", typescript:"javascript",
          node:"javascript", xml:"html", svg:"html", htm:"html",
          sh:"bash", shell:"bash", zsh:"bash", console:"bash",
          ps1:"bash", powershell:"bash", pwsh:"bash", bat:"bash", cmd:"bash",
          yml:"yaml", scss:"css", less:"css",
          c:"clike", cpp:"clike", cc:"clike", cxx:"clike", h:"clike",
          hpp:"clike", hxx:"clike", java:"clike", cs:"clike",
          csharp:"clike", go:"clike", golang:"clike", rust:"clike",
          rs:"clike", php:"clike", swift:"clike", kt:"clike",
          kotlin:"clike", scala:"clike", dart:"clike", groovy:"clike",
          objc:"clike", m:"clike", mm:"clike"},

  parts: {
    python: [["#[^\\n]*", "c"],
             ["[\"']{3}[\s\S]*?[\"']{3}", "s"],
             ["\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'", "s"],
             ["\\b(?:def|class|return|if|elif|else|for|while|import|from|as|with|try|except|finally|raise|lambda|yield|and|or|not|in|is|None|True|False|pass|break|continue|global|nonlocal|assert|del|async|await|self)\\b", "k"],
             ["\\b(?:0[xXbBoO][0-9a-fA-F_]+|\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)\\b", "n"]],
    javascript: [["//[^\\n]*|/\\*[\\s\\S]*?\\*/", "c"],
             ["`(?:\\\\.|[^`\\\\])*`|\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'", "s"],
             ["\\b(?:const|let|var|function|return|if|else|for|while|of|in|new|class|extends|import|export|from|default|async|await|try|catch|finally|throw|typeof|instanceof|null|undefined|true|false|this|switch|case|break|continue|do|delete|yield)\\b", "k"],
             ["\\b(?:0[xXbBoO][0-9a-fA-F_]+|\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)\\b", "n"]],
    json: [["\"(?:\\\\.|[^\"\\\\])*\"", "s"],
             ["\\b(?:true|false|null)\\b", "k"],
             ["-?\\b\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b", "n"]],
    html: [["<!--[\\s\\S]*?-->", "c"],
             ["\"(?:[^\"]*)\"|'(?:[^']*)'", "s"],
             ["</?[A-Za-z][\\w:-]*|/?>", "t"],
             ["\\b(?:\\d+(?:\\.\\d+)?)\\b", "n"]],
    css: [["/\\*[\\s\\S]*?\\*/", "c"],
             ["\"(?:[^\"]*)\"|'(?:[^']*)'", "s"],
             ["#[0-9a-fA-F]{3,8}\\b|\\b\\d+(?:\\.\\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg)?\\b", "n"],
             ["@[a-z-]+|\\b(?:important|inherit|initial|unset|none|auto)\\b", "k"]],
    bash: [["#[^\\n]*", "c"],
             ["\"(?:\\\\.|[^\"\\\\])*\"|'(?:[^'])*'", "s"],
             ["\\$[A-Za-z_][\\w]*|\\$\\{[^}]*\\}", "k"],
             ["\\b(?:if|then|else|elif|fi|for|in|do|done|while|case|esac|function|return|export|local|echo|cd|exit|param|foreach)\\b", "k"],
             ["\\b\\d+\\b", "n"]],
    // EIN SATZ FUER DIE GANZE C-FAMILIE. robin am 2026-08-24: "fuer alle
    // programmiersprachen". Einen Satz je Sprache zu schreiben endet nie -- aber
    // C, C++, Java, C#, Go, Rust, PHP, Swift, Kotlin und Dart teilen sich `//`,
    // `/* */`, Zeichenketten und Zahlen, und die Schluesselwoerter darunter sind
    // die Schnittmenge, die in allen dasselbe bedeutet. Was nur eine davon kennt,
    // bleibt ungefaerbt: lieber kein Wort markiert als das falsche.
    clike: [["//[^\\n]*|/\\*[\\s\\S]*?\\*/", "c"],
             ["\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'", "s"],
             ["^\\s*#\\s*[a-z]+", "k"],
             ["\\b(?:auto|bool|break|case|catch|char|class|const|continue|default|delete|do|double|else|enum|extern|false|final|float|fn|for|func|go|goto|if|impl|import|inline|int|interface|let|long|mut|namespace|new|nil|null|nullptr|package|private|protected|public|pub|return|short|signed|sizeof|static|struct|switch|template|this|throw|true|try|type|typedef|typename|union|unsigned|use|using|var|virtual|void|while)\\b", "k"],
             ["\\b(?:0[xXbB][0-9a-fA-F_]+|\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?[fFlLuU]*)\\b", "n"]],
    yaml: [["#[^\\n]*", "c"],
             ["\"(?:[^\"]*)\"|'(?:[^']*)'", "s"],
             ["\\b(?:true|false|null|yes|no)\\b", "k"],
             ["\\b\\d+(?:\\.\\d+)?\\b", "n"]],
  },

  built: {},

  // EIN ETIKETT WIRD LOCKER GENOMMEN. Ein Modell schreibt in die Fence, was ihm
  // einfaellt -- `py`, `Python`, `PYTHON`, `python3` meinen dasselbe.
  lang(name){
    const key=String(name||"").toLowerCase().replace(/[^a-z0-9]/g,"");
    return this.parts[key] ? key : (this.alias[key]||"");
  },

  rules(key){
    if(this.built[key]) return this.built[key];
    const parts=this.parts[key];
    if(!parts) return null;
    const re=new RegExp(parts.map(p=>"("+p[0]+")").join("|"),"g");
    return (this.built[key]={re:re, cls:parts.map(p=>p[1])});
  },

  parse(code, lang){
    const key=this.lang(lang), built=key && this.rules(key);
    // WAS NIEMAND KENNT, WIRD NICHT GERATEN. Falsche Farbe ist schlechter als
    // keine: sie behauptet eine Struktur, die es nicht gibt.
    if(!built) return [["", code]];
    const out=[]; let last=0, m;
    built.re.lastIndex=0;
    while((m=built.re.exec(code))){
      if(!m[0]){ built.re.lastIndex++; continue; }
      if(m.index>last) out.push(["", code.slice(last,m.index)]);
      let cls="";
      for(let i=0;i<built.cls.length;i++){ if(m[i+1]!==undefined){ cls=built.cls[i]; break; } }
      out.push([cls, m[0]]);
      last=built.re.lastIndex;
    }
    if(last<code.length) out.push(["", code.slice(last)]);
    return out;
  },

  // NUR AUF EINEN FERTIGEN BLOCK. Waehrend des Stroems ist jede zweite
  // Zeichenkette offen, und die Farbe spraenge bei jedem Fragment um.
  paint(pre, lang){
    const code=pre.textContent;
    if(!code) return;
    const spans=this.parse(code, lang);
    if(spans.length<2) return;
    pre.textContent="";
    spans.forEach(([cls,text])=>{
      if(!cls){ pre.appendChild(document.createTextNode(text)); return; }
      const el=document.createElement("span");
      el.className="hl-"+cls; el.textContent=text;
      pre.appendChild(el); });
  }
};

const crow = {
  running:false, col:null, say:null, think:null, fence:null, fenceLang:"",
  cursor:null, blocks:[],

  esc(t){ const d=document.createElement("div"); d.textContent=t; return d.innerHTML; },

  // #119: AND THE GREETING GOES HERE, because this is the ONE place a turn is
  // appended. Hooking it to the user's first message instead would leave it
  // standing under a chat restored from disk, under a tool card, and under an
  // error -- three shapes that are not `user()` and all of which mean the chat
  // is no longer empty.
  turn(cls){ const g=$("#hello"); if(g) g.remove();
    const d=document.createElement("div"); d.className="turn "+cls;
    flow.appendChild(d); return d; },

  // WHAT `fold` WILL MOVE NEXT TIME. Set by `start`, cleared by anything that
  // ends the trace: a new user line, a reset, a reopened chat.
  round: null, trace: null, traceN: 0,
  endTrace(){ this.round=null; this.trace=null; this.traceN=0; },

  // DRAWN, NOT RUN: the line carries the user's login name, which is a string
  // off the machine. textContent, like every other name in this file.
  hello(text){ const g=$("#hello"); if(g) g.remove();
    if(!text) return;
    const d=document.createElement("div"); d.id="hello";
    // #127. THE DRAWING FIRST, THE LINE UNDER IT, and the template is CLONED so
    // a second greeting in the same session still has one to clone.
    const t=$("#marktpl"); if(t) d.appendChild(t.content.cloneNode(true));
    const p=document.createElement("div"); p.className="hellotext";
    p.textContent=text; d.appendChild(p);
    flow.appendChild(d); },

  user(text){
    this.endTrace();
    const t=this.turn(""); t.innerHTML=
      '<div class="you"><div class="txt"></div></div>';
    t.querySelector(".txt").textContent=text; this.bottom(true);
  },

  // #142. A SEPARATE CALL, NOT A SECOND PARAMETER: four cases anchor the whole
  // `user(text){` signature, and the same lesson is already written into
  // v1.3.0's tool_result -- a new seam lands BESIDE an anchored one. The image
  // is IN the transcript, or somebody sends a picture they cannot see; same
  // renderer live and restored.
  userImages(urls){
    if(!urls || !urls.length){ return; }
    const you=[...document.querySelectorAll(".you")].pop();
    if(!you){ return; }
    urls.forEach(u=>{ const im=document.createElement("img");
      im.className="sent"; im.src=u; you.appendChild(im); });
    this.bottom();
  },

  // #131. VARIANT A (robin, 2026-08-22). A 24-round turn put 24 rounds of
  // thoughts and running commentary between the question and the answer, and
  // the answer was below the fold when it finally arrived.
  //
  // EVERY FINISHED ROUND FOLDS, THE RUNNING ONE DOES NOT. Hiding the live round
  // too would leave a blank screen for minutes -- the interim text is the only
  // sign of life a long turn has. So what is on screen is one `Trace` line plus
  // whatever is happening right now, and the answer ends up at the top of it.
  //
  // A NEW ROUND IS WHAT FOLDS THE OLD ONE, which is the only signal there is:
  // nothing tells this page that a round was the LAST one until the turn ends,
  // so the last round is simply the one nobody folded.
  fold(){
    if(!this.round || !this.round.isConnected) { this.round=null; return; }
    const done=this.round; this.round=null;
    if(!done.textContent.trim() && !done.querySelector("details")){
      done.remove(); return; }
    if(!this.trace || !this.trace.isConnected){
      const t=this.turn("");
      const d=document.createElement("details"); d.className="trace";
      d.innerHTML='<summary><span class="caret"></span>'
        + '<span class="tl">Trace</span><span class="tn"></span></summary>'
        + '<div class="tb"></div>';
      d.querySelector(".caret").textContent=String.fromCharCode(9654);
      t.appendChild(d); this.trace=d; this.traceN=0; }
    this.trace.querySelector(".tb").appendChild(done);
    this.traceN++;
    this.trace.querySelector(".tn").textContent =
      this.traceN + (this.traceN===1 ? " round" : " rounds"); },

  start(){
    this.fold();
    const t=this.turn("");
    t.innerHTML='<div class="as"><span class="m">&#9679;</span><div class="col"></div></div>';
    this.col=t.querySelector(".col"); this.say=null; this.think=null;
    this.fence=null; this.blocks=[];
    // ONE CURSOR IN THE WHOLE FLOW. `reply_started` fires once per ROUND, not
    // once per turn, so a turn with a tool call opens two -- and the first was
    // left blinking in a finished answer. Every existing one goes before a new
    // one is made, which also covers the round that ended without an idle.
    document.querySelectorAll(".cursor").forEach(c=>c.remove());
    this.round=t;
    this.cursor=document.createElement("span"); this.cursor.className="cursor";
    this.col.appendChild(this.cursor); this.bottom();
  },

  // A NEW BLOCK PER OPEN, never one reused: the model re-enters reasoning
  // mid-answer, and a single block would swallow the answer that came between.
  thinkOpen(){
    const d=document.createElement("details"); d.className="think";
    d.innerHTML='<summary><span class="caret">&#9654;</span><span class="dur">'+
      (this.col.querySelectorAll("details").length? "Thought again":"Thought")+
      '</span></summary><div class="tbody"></div>';
    this.col.insertBefore(d,this.cursor); this.think=d.querySelector(".tbody");
    this.say=null; this.bottom();
  },
  thinkText(p){ if(!this.think) this.thinkOpen();
    this.think.textContent+=p; this.bottom(); },
  thinkClose(){ this.think=null; },

  answer(p){
    this.think=null;
    if(this.fence!==null){ this.fence.textContent+=p; this.bottom(); return; }
    if(!this.say){ this.say=document.createElement("div"); this.say.className="say";
      this.col.insertBefore(this.say,this.cursor); }
    this.say.textContent+=p; this.bottom();
  },

  // -- markdown, drawn from what the core cut ------------------------------
  //
  // NOTHING FROM THE WIRE BECOMES MARKUP. Every piece arrives named and its
  // text goes in through textContent, the same rule the rest of this file
  // follows. A single line of raw markup in here would turn a model's answer
  // into a place to put script, and a case in test_crow_gui.py reads this whole
  // region and refuses the one property that would allow it.
  span(sp){
    let node=document.createTextNode(sp.s||"");
    if(sp.c){ const c=document.createElement("code"); c.textContent=sp.s||""; node=c; }
    if(sp.b){ const b=document.createElement("strong"); b.appendChild(node); node=b; }
    if(sp.i){ const i=document.createElement("em"); i.appendChild(node); node=i; }
    // THE SECOND GATE ON A TARGET. The core already refuses to name anything
    // but http and https; this one is here because the text is a stranger's and
    // a link that navigates would replace the whole window, which has no way
    // back -- so it never navigates, it asks the browser outside.
    if(sp.href && /^https?:\/\//i.test(sp.href)){
      const a=document.createElement("a"); a.className="lnk"; a.title=sp.href;
      a.appendChild(node);
      a.onclick=ev=>{ ev.preventDefault(); pywebview.api.open_url(sp.href); };
      node=a; }
    return node; },
  spansInto(el,spans){ (spans||[]).forEach(sp=>el.appendChild(this.span(sp)));
    return el; },
  cellsInto(row,cells,tag){ (cells||[]).forEach(cell=>this.spansInto(
    row.appendChild(document.createElement(tag)),cell)); },
  block(b){
    if(b.t==="h"){ const h=document.createElement("div");
      h.className="mdh mdh"+b.n; return this.spansInto(h,b.spans); }
    if(b.t==="ul"||b.t==="ol"){ const l=document.createElement(b.t);
      (b.items||[]).forEach(item=>this.spansInto(
        l.appendChild(document.createElement("li")),item));
      return l; }
    if(b.t==="table"){ const t=document.createElement("table");
      this.cellsInto(t.appendChild(document.createElement("thead"))
        .appendChild(document.createElement("tr")),b.head,"th");
      const body=t.appendChild(document.createElement("tbody"));
      (b.rows||[]).forEach(r=>this.cellsInto(
        body.appendChild(document.createElement("tr")),r,"td"));
      return t; }
    return this.spansInto(document.createElement("p"),b.spans); },
  format(blocks){
    const box=this.say;
    if(!box||!blocks||!blocks.length) return;
    box.textContent=""; box.classList.add("md");
    blocks.forEach(b=>box.appendChild(this.block(b)));
    this.say=null; this.bottom(); },
  // -- end markdown --------------------------------------------------------

  codeOpen(lang){
    this.say=null; this.fenceLang=lang||"code";
    const d=document.createElement("div"); d.className="code";
    d.innerHTML='<div class="hd"><span class="lang"></span>'+
      '<span class="n"></span>'+
      '<button class="copy">copy</button></div><pre></pre>';
    d.querySelector(".lang").textContent=this.fenceLang;
    const pre=d.querySelector("pre"), btn=d.querySelector(".copy");
    // #138 / robin 2026-08-24. DER KOPF IST DIE FALTE, und er bleibt sichtbar,
    // wenn der Rumpf weg ist -- sonst ist der Weg zurueck weg. Die Falte wird
    // erst beim Schliessen freigeschaltet (`.foldable`), weil vorher niemand
    // weiss, wie lang der Block wird; hier haengt nur der Griff schon bereit.
    d.querySelector(".hd").onclick=()=>{
      if(d.classList.contains("foldable")) d.classList.toggle("shut"); };
    // THROUGH PYTHON, NOT navigator.clipboard. The page is handed to WebView2 as
    // HTML rather than served over https, so it is not a secure context and the
    // async clipboard API silently refuses -- the button said "copied" over an
    // empty clipboard, which is worse than a button that does nothing.
    btn.onclick=(ev)=>{
      // NICHT DURCHBLUBBERN LASSEN. Der Knopf sitzt im Kopf, und der Kopf ist
      // die Falte -- dieselbe Stelle, an der die Werkzeug-Kachel schon einmal
      // weggeklappt hat, was gerade geleert wurde.
      ev.stopPropagation();
      if(!pre.textContent){ btn.textContent="empty"; return; }
      pywebview.api.copy(pre.textContent).then(ok=>{
        btn.textContent = ok ? "copied" : "failed";
        btn.classList.toggle("done", !!ok);
        setTimeout(()=>{btn.textContent="copy";btn.classList.remove("done");},1400);});};
    this.col.insertBefore(d,this.cursor); this.fence=pre; this.bottom();
  },
  codeClose(closed){
    if(!closed && this.fence){ const n=document.createElement("div");
      n.className="note"; n.textContent="… the block was never closed";
      this.col.insertBefore(n,this.cursor); }
    // robin, 2026-08-24: ab funfzehn Zeilen klappbar. ERST HIER, weil die
    // Laenge vorher nicht feststeht -- ein Block, der beim dritten Zeichen
    // einen Klappknopf bekaeme, haette ihn oft umsonst.
    //
    // FREIGESCHALTET, NICHT ZUGEKLAPPT. Wer zusieht, wie etwas geschrieben
    // wird, will es sehen; ihm den Inhalt im Moment des Fertigwerdens
    // wegzunehmen waere die Umkehrung dessen, wofuer die Falte da ist.
    if(this.fence){
      HL.paint(this.fence, this.fenceLang);
      const box=this.fence.closest(".code");
      const lines=(this.fence.textContent.match(/\n/g)||[]).length+1;
      if(box && lines>=15){
        box.classList.add("foldable");
        const n=box.querySelector(".n");
        if(n) n.textContent=lines+" lines";
      }
    }
    this.fence=null;
  },

  // #138. EIN LAUFENDER AUFRUF SCHLIESST SEINEN BLOCK. Ab hier sind die
  // Argumente vollstaendig -- was jetzt noch kaeme, gehoert zur naechsten Runde.
  tool(name,args,raw,code){
    // #138. Der Block im Panel ist fertig, sobald der Aufruf laeuft -- ab hier
    // darf er Farbe bekommen. Die Sprache steht im Pfad, den das Werkzeug
    // schreibt; ohne erkennbare Endung bleibt es einfarbig.
    //
    // #138b. UND AB HIER STEHT DER REINE INHALT DARIN, nicht die Huelle.
    // Waehrend des Stroms ist das JSON unvollstaendig und nicht zu zerlegen --
    // deshalb laeuft es roh durch, damit man mitlesen kann, und wird genau
    // hier ersetzt, wo die Argumente zum ersten Mal ganz sind.
    if(this.live) this.codeFinish(name,raw);
    this.live=null;
    const n=$("#codehead .n"); if(n) n.textContent="";
    const d=document.createElement("div"); d.className="tool shut";
    d.innerHTML='<div class="hd"><span class="ico">&#9679;</span>'+
      '<span class="name"></span><span class="arg"></span>'+
      '<span class="note"></span><span class="tx">+</span></div>'+
      '<div class="tbody"></div>';
    d.querySelector(".note").textContent =
      this.execute ? "ran" : "shown, not run";
    d.querySelector(".name").textContent=name;
    d.querySelector(".arg").textContent=args||"";
    // #143. The delegation calls wear their own glyph and channel, so a fan-out
    // is readable in the panel without opening a single row.
    if(name==="delegate"||name==="collect"||name==="subtasks"){
      d.classList.add("sub"); d.querySelector(".ico").textContent="⑂"; }
    // #138b. JEDE ZEILE KLAPPT FUER SICH. Vorher klappte nur die ganze Gruppe,
    // und "aufklappen" hiess: neunzig Zeilen auf einmal, von denen eine die
    // gesuchte war.
    //
    // DER KLICK WIRD GEFANGEN. Der Rumpf ist ein Geschwister des Gruppenkopfes
    // und blubbert heute an keinen Handler -- aber `.tcbody` einen zu geben ist
    // eine Zeile, und dann faltete ein Klick auf eine Zeile die ganze Gruppe
    // weg. Dieselbe Falle, die die Memory-Kachel am 2026-08-22 getroffen hat.
    d.querySelector(".hd").addEventListener("click", ev=>{
      ev.stopPropagation();
      const shut=d.classList.toggle("shut");
      d.querySelector(".tx").textContent = shut ? "+" : "−"; });
    // #131. INTO THE TILE, NOT INTO THE COLUMN. A 24-round turn used to put 24
    // of these between the question and the answer.
    const list=$("#tclist");
    const empty=list.querySelector(".empty"); if(empty) empty.remove();
    list.appendChild(d); this.toolsCount();
    // DIE ARGUMENTE IN VOLLER LAENGE, in den Rumpf. Die Kopfzeile traegt die
    // gekuerzte Fassung, weil sie eine Zeile ist; wer aufklappt, will das,
    // was das Werkzeug wirklich bekommen hat.
    if(raw) this.toolArgBlock(d, name, raw);
    // DER OFFENE AUFRUF, an den Uhr und Antwort gehen. Die drei Ereignisse
    // kommen je Aufruf in dieser Reihenfolge ueber die Naht, also ist der
    // zuletzt angelegte immer der gemeinte. `tools_reported` legt Zeilen an,
    // die nie ein Ergebnis bekommen -- die naechste `tool` ueberschreibt sie,
    // und keine fremde Antwort landet in einer fremden Zeile.
    this.openCall=d;
    this.tail();
  },

  // #138b. Der Strom war die Huelle, dies ist der Inhalt.
  //
  // DIE ERSTE NOCH OFFENE BOX DIESES NAMENS, nicht irgendeine: zwei
  // `write_file` in einer Runde kommen in derselben Reihenfolge an, in der
  // ihre Bloecke angelegt wurden, also trifft die Reihenfolge zu. Eine Box,
  // die schon ihren Inhalt hat, wird nie ein zweites Mal ueberschrieben --
  // sonst traegt die zweite Datei den Text der ersten.
  codeFinish(name,raw){
    const boxes=Object.keys(this.live).map(i=>this.live[i])
      .filter(b=>b && b.dataset.nm===name && b.dataset.done!=="1");
    const box=boxes[0];
    if(!box){ Object.keys(this.live).forEach(i=>{
      const p=this.live[i] && this.live[i].querySelector(".cwp");
      if(p) HL.paint(p, this.langOf(p.textContent)); }); return; }
    const pre=box.querySelector(".cwp");
    let lang="";
    try{
      const got=JSON.parse(raw||"{}");
      // `content` schreibt eine Datei, `new` aendert eine. Beides ist der
      // Text, der nachher auf der Platte steht -- `old` ist es nicht und
      // gehoert deshalb in die Argumente der Aufrufzeile, nicht hierher.
      const body = (got.content!==undefined) ? got.content : got.new;
      if(typeof body==="string" && pre) pre.textContent=body;
      if(got.path){
        const head=box.querySelector(".cwh");
        if(head) head.textContent=got.path;
        const dot=String(got.path).lastIndexOf(".");
        if(dot>=0) lang=String(got.path).slice(dot+1); }
    }catch(err){ /* unvollstaendig oder nicht JSON: der rohe Strom bleibt */ }
    box.dataset.done="1";
    if(pre) HL.paint(pre, lang || this.langOf(pre.textContent)); },

  // #138b. Die Argumente als Block, mit Farbe wo es sich lohnt.
  //
  // JSON IST DIE SPRACHE DIESES BLOCKS, immer -- die Argumente eines Werkzeugs
  // sind ein JSON-Objekt, egal welches Werkzeug. Das ist der eine Fall, in dem
  // die Sprache nicht geraten werden muss.
  toolArgBlock(row, name, raw){
    const wrap=document.createElement("div"); wrap.className="tsec";
    const head=document.createElement("div"); head.className="tsh";
    head.textContent="arguments";
    const pre=document.createElement("pre"); pre.className="tsp";
    let shown=raw;
    try{ shown=JSON.stringify(JSON.parse(raw),null,2); }catch(err){ shown=raw; }
    pre.textContent=shown;
    HL.paint(pre,"json");
    wrap.appendChild(head); wrap.appendChild(pre);
    row.querySelector(".tbody").appendChild(wrap); },

  toolEnd(name,seconds,repeated){
    const row=this.openCall;
    if(!row || row.querySelector(".name").textContent!==name) return;
    const note=row.querySelector(".note");
    // DIE UHR NUR, WENN SIE ETWAS SAGT. Unter einer Zehntelsekunde ist "0.0s"
    // Laerm; dieselbe Grenze, die das Terminal seit #70 zieht.
    const parts=[];
    if(repeated) parts.push("repeat");
    if(seconds>=0.1) parts.push(seconds.toFixed(1)+"s");
    if(parts.length) note.textContent=parts.join(" · "); },

  toolRes(name,text,cut){
    const row=this.openCall;
    if(!row || row.querySelector(".name").textContent!==name) return;
    const bad=String(text||"").startsWith("error: ");
    if(bad) row.classList.add("bad");
    const wrap=document.createElement("div"); wrap.className="tsec res";
    const head=document.createElement("div"); head.className="tsh";
    head.textContent = bad ? "error" : "result";
    const pre=document.createElement("pre"); pre.className="tsp";
    pre.textContent = text || "(nothing)";
    wrap.appendChild(head); wrap.appendChild(pre);
    if(cut>0){
      const more=document.createElement("div"); more.className="tsmore";
      more.textContent = cut.toLocaleString() + " more characters not shown";
      wrap.appendChild(more); }
    row.querySelector(".tbody").appendChild(wrap);
    this.openCall=null; },

  toolsCount(){
    const n=$("#tclist").querySelectorAll(".tool").length;
    $("#toolcalls .tcn").textContent = n ? String(n) : "";
    if(!n && !$("#tclist").querySelector(".empty")){
      const p=document.createElement("p");
      p.className="empty"; p.textContent="Nothing called yet.";
      $("#tclist").appendChild(p); } },

  // #138. WAS GERADE GESCHRIEBEN WIRD, im Panel rechts.
  //
  // EIN BLOCK JE AUFRUF, und `index` ist der Schluessel: zwei Aufrufe in einer
  // Runde teilen sich den Strom und waeren sonst ein Reissverschluss aus zwei
  // Dateien. Die Karte wird geleert, sobald ein Aufruf LAEUFT (`tool`), denn
  // dann ist die Runde fertig und die naechste faengt bei index 0 wieder an.
  toolArg(i,name,piece,code){
    // #138b. NUR WAS PROGRAMMCODE SCHREIBT bekommt hier einen Block. Vorher
    // legte JEDES Werkzeug einen an, und ein `web_search` stellte seine
    // JSON-Huelle neben den Quelltext -- so wurde aus dem Code-Panel ein
    // Werkzeug-Panel. Ein `run_command` gehoert in die Liste der Aufrufe.
    if(!code) return;
    if(!this.live) this.live={};
    let box=this.live[i];
    if(!box || box.dataset.nm!==name){
      const body=$("#cflist");
      $("#codefiles").hidden=false;
      box=document.createElement("div"); box.className="cw"; box.dataset.nm=name;
      const hd=document.createElement("div"); hd.className="cwh";
      hd.textContent=name;
      const pre=document.createElement("pre"); pre.className="cwp";
      box.appendChild(hd); box.appendChild(pre);
      body.appendChild(box);
      this.live[i]=box;
      // NUR WENN DAS PANEL OFFEN IST. Es aufzureissen, weil ein Werkzeug
      // laeuft, waere eine Entscheidung, die der Leser getroffen hat und die
      // ihm hier wieder abgenommen wuerde.
      if(document.body.dataset.code!=="shut") box.scrollIntoView({block:"end"});
    }
    const pre=box.querySelector(".cwp");
    pre.textContent += piece;
    if(document.body.dataset.code!=="shut"){
      const body=$("#codebody");
      // AM ENDE BLEIBEN, ABER NUR WENN JEMAND DORT WAR. Wer hochgescrollt hat,
      // liest etwas -- ihn zurueckzuziehen ist die haerteste Art, ihn dabei zu
      // stoeren. Vier Pixel Spiel gegen Rundung.
      const atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      if(atEnd) body.scrollTop = body.scrollHeight;
    }
    const n=$("#codehead .n");
    if(n) n.textContent = Object.keys(this.live).length ? "writing" : ""; },

  // DIE SPRACHE STEHT IM PFAD, den das Werkzeug gerade schreibt. Kein Raten
  // aus dem Inhalt: eine falsch erkannte Sprache faerbt Struktur, die es nicht
  // gibt, und das ist schlechter als grau.
  langOf(text){
    const m=String(text||"").match(/"path"\s*:\s*"([^"]+)"/);
    if(!m) return "";
    const dot=m[1].lastIndexOf(".");
    return dot<0 ? "" : m[1].slice(dot+1); },

  toolsToggle(){
    const box=$("#toolcalls"), shut=box.classList.toggle("shut");
    $("#toolcalls .tcx").textContent = shut ? "+" : "−";
    if(!shut) this.toolsCount(); },

  // THE CLICK IS CAUGHT, or it bubbles to the head and folds the tile away --
  // the trap the memory tile hit on 2026-08-22, where `discard` would have
  // hidden the very thing it discarded.
  toolsClear(e){
    if(e) e.stopPropagation();
    $("#tclist").textContent=""; this.toolsCount();
    // AND IT STAYS CLEARED ACROSS A RESTART. The chat replays its tool rows on
    // open, so without a watermark on the Python side a list somebody emptied
    // comes back at the next start -- found by robin after rebooting the window.
    pywebview.api.tools_cleared(); },

  // A NEW CHAT IS A NEW LIST. The tile belongs to the conversation, not to the
  // window, so switching or resetting empties it with everything else.
  toolsReset(){
    $("#tclist").textContent="";
    $("#toolcalls").classList.add("shut");
    $("#toolcalls .tcx").textContent="+";
    this.codeReset();
    this.toolsCount(); },

  // #138b. Der Quelltext-Abschnitt, geleert und wieder versteckt.
  //
  // `this.live` MUSS MIT. Es zeigt auf Boxen, die es nach dem Leeren nicht
  // mehr gibt; ein laufender Strom haenge sonst Zeichen an ein Element, das
  // in keinem Dokument mehr steht -- sichtbar waere davon nichts, und genau
  // deshalb faende man es nie.
  codeReset(){
    $("#cflist").textContent="";
    $("#codefiles").hidden=true;
    this.live=null;
    this.openCall=null;
    const n=$("#codehead .n"); if(n) n.textContent=""; },

  // #138b. Alles im Panel, in einem Griff.
  codeWipe(e){
    if(e) e.stopPropagation();
    this.codeReset();
    $("#tclist").textContent="";
    this.toolsCount();
    // DERSELBE WASSERSTAND WIE BEIM `clear` DER GRUPPE. Ohne ihn holt der
    // Chat seine Werkzeugzeilen beim naechsten Start aus dem Verlauf zurueck
    // -- gefunden, nachdem robin das Fenster neu gestartet hatte.
    pywebview.api.tools_cleared(); },

  cost(line,share,sub){
    if(this.cursor){ this.cursor.remove(); this.cursor=null; }
    // THE SAME NUMBER ON EVERY BLOCK IS CORRECT, because the number is the TURN's and the label
    // says so. It was not until #117: the turn object kept the LAST ROUND's ratio, and this loop
    // spread that over all of them. Blocks cannot carry a per-round figure anyway -- the model
    // re-enters reasoning mid-answer, so one round can open two of them and there is no index
    // that maps one to the other.
    if(share!==null && share!==undefined){
      this.col.querySelectorAll("details.think .dur").forEach(el=>{
        if(!el.parentNode.querySelector(".pct")){
          const s=document.createElement("span"); s.className="pct";
          s.textContent=share.toFixed(0)+" % of the turn";
          el.parentNode.appendChild(s);} });
    }
    if(line){ const d=document.createElement("div"); d.className="cost";
      // #143. The delegation share rides the cost line in its own channel:
      // the closing bracket moves into the coloured span so the share sits
      // INSIDE the brackets, exactly as the agreed mockup draws it.
      if(sub){ d.textContent=line.replace(/\]$/," | ");
        const s=document.createElement("span"); s.className="subshare";
        s.textContent=sub+"]"; d.appendChild(s); }
      else d.textContent=line;
      this.col.appendChild(d); }
    this.bottom();
  },

  fail(msg){ const d=document.createElement("div"); d.className="fail";
    d.textContent=msg; (this.col||flow).appendChild(d); this.bottom(); },
  // #130. THE INSTALL LINE, AND WHY THE NOTE WAITS FOR IT. `/mcp add` starts a
  // foreign process and waits for a handshake -- the one slash command that
  // takes real time. It gets the memory gate's tile, in the chat, under the
  // command that was typed.
  //
  // FOUR SECONDS AT LEAST (robin, 2026-08-22), even when the server answers in
  // three hundred milliseconds: a tile that flashes past is indistinguishable
  // from nothing having happened, which is the whole complaint it answers.
  installBar(){
    const t=this.turn(""); const d=document.createElement("div");
    d.className="installbar";
    const top=document.createElement("span"); top.className="top";
    const title=document.createElement("span");
    title.className="title"; title.textContent="Install MCP";
    const hint=document.createElement("span");
    hint.className="hint"; hint.textContent="starting the server …";
    top.appendChild(title); top.appendChild(hint);
    d.appendChild(top); t.appendChild(d);
    this.installTurn=t; this.installUntil=Date.now()+4000; this.bottom(); },

  // THE ANSWER IS HELD, NOT THE CALL. Python pushes its note the moment the
  // schema is on disk; delaying it HERE keeps the whole floor in one place and
  // leaves the core with nothing to know about an animation.
  note(msg){
    if(this.installUntil){
      const wait=Math.max(0, this.installUntil - Date.now());
      this.installUntil=0;
      setTimeout(() => {
        if(this.installTurn){ this.installTurn.remove(); this.installTurn=null; }
        this.drawNote(msg); }, wait);
      return; }
    this.drawNote(msg); },

  drawNote(msg){ const t=this.turn(""); const d=document.createElement("div");
    d.className="note"; d.textContent=msg; t.appendChild(d); this.bottom(); },
  alarm(msg){ const t=this.turn(""); const d=document.createElement("div");
    d.className="alarm"; d.textContent=msg; t.appendChild(d); this.bottom(); },
  // #122. THE ONLY NOTICE THAT SOMETHING WAS REMEMBERED. There is no approval
  // gate, so this line is where a person finds out -- it glows once on arrival
  // and then sits there like any other row.
  memory(msg,n){ const t=this.turn(""); const d=document.createElement("div");
    d.className="memnote";
    // THE MARK FIRST: it labels the row, so it reads before the sentence does.
    const ico=document.createElement("span"); ico.className="memicon";
    const say=document.createElement("span");
    say.textContent = n>1 ? msg+" ("+n+")" : msg;
    d.appendChild(ico); d.appendChild(say);
    t.appendChild(d); this.bottom(); },

  // THE CURSOR ALWAYS SITS LAST. Every insert goes BEFORE it, so after a tool
  // row or a code frame it has to be moved back to the end -- otherwise it is
  // stranded above whatever arrived next, blinking in the middle of the answer.
  tail(){ if(this.cursor && this.col) this.col.appendChild(this.cursor); },
  // robins Regel vom 2026-08-28 nachts: USERSCROLL > ALLES. Waehrend Crow
  // streamte, zog jeder Chunk die Sicht ans Ende -- hochscrollen war
  // unmoeglich. Angeheftet ist nur, wer unten IST (80px Toleranz); wer
  // hochscrollt, loest sich, wer ans Ende zurueckkehrt, heftet wieder.
  // Nur die eigene Nachricht erzwingt das Ende (bottom(true)).
  atBottom(){ return flow.scrollHeight-flow.scrollTop-flow.clientHeight<80; },
  bottom(force){ this.tail();
    if(force||this.atBottom()) flow.scrollTop=flow.scrollHeight; },

  // THE SWITCH IS THE CHIP, not a start-up flag. A decision the user has to
  // spell on the command line every time is a decision they stop making; one
  // they can see and click is one they keep. What it may never be is silent --
  // the chip says which of the two modes is running, in both states.
  tools(on){
    this.execute=on;
    const c=$("#tools"), s=c.querySelector("span"), w=$("#toolsw");
    if(s) s.textContent = on ? ", running" : ", shown only";
    // #125. A SWITCH IN THE SHEET, not a coloured chip in a bar. The chip had to
    // shout which of two modes was live, because it sat in the corner of every
    // screen; a row in a panel somebody opened on purpose says it in words and
    // needs no colour to be read.
    if(w){ w.classList.toggle("on", on);
           w.title = on ? "Tool calls run" : "Tool calls are only shown"; }
    c.title = on ? "Tools run. Switch off to only show them."
                 : "Tools are only shown. Switch on to let them run.";
  },
  toggleTools(){ if(this.running) return; pywebview.api.set_tools(!this.execute); },

  helpMenu(){ const m=$("#helpmenu"); m.hidden=!m.hidden; },

  openSettings(){
    $("#helpmenu").hidden=true;
    // THE ATTRIBUTE IS THE TRUTH, not a variable beside it. Python wrote it onto
    // <html> before the page was handed over, so the sheet marks whatever is
    // actually on screen rather than what a second copy of the state believes.
    const now=document.documentElement.dataset.theme || "dark";
    document.querySelectorAll("#themes button").forEach(
      b => b.classList.toggle("on", b.dataset.theme===now));
    this.drawSkills();
    this.drawMcp();
    this.drawProviders();
    this.drawSubs();
    $("#settings").hidden=false;
  },
  // #124. ASKED FOR EVERY TIME THE SHEET OPENS, never cached in the page: the
  // list changes behind the window's back, because the background review writes
  // skills without anybody clicking anything.
  drawSkills(){
    pywebview.api.skills().then(list => {
      const box=$("#skilllist"); box.textContent="";
      if(!list.length){
        const p=document.createElement("p");
        p.className="empty"; p.textContent="Nothing here yet.";
        box.appendChild(p); return; }
      list.forEach(sk => {
        const row=document.createElement("div");
        row.className="srow"+(sk.enabled?"":" off");
        const text=document.createElement("div"); text.className="stext";
        const n=document.createElement("div"); n.className="sname"; n.textContent=sk.name;
        const d=document.createElement("div"); d.className="sdesc";
        d.textContent=sk.description||"(no description)";
        text.appendChild(n); text.appendChild(d);
        // A BUTTON WIRED FROM THE OBJECT, never an onclick string: a skill name
        // is model-written text, and the rail learned that lesson in #119.
        const sw=document.createElement("button");
        sw.className="sw"+(sk.enabled?" on":"");
        sw.title=sk.enabled?"in the prompt":"not in the prompt";
        sw.onclick=()=>this.toggleSkill(sk.name,row,sw);
        row.appendChild(text); row.appendChild(sw);
        box.appendChild(row); }); }); },
  // #129. THE SHEET ASKS EVERY TIME IT OPENS, and again after every tick. The
  // cost line is the reason it cannot be cached in the page: it is a count of
  // characters that changes with the tick that was just made, and a stale one
  // would understate what the next turn is about to pay.
  drawMcp(){
    pywebview.api.mcp_view().then(view => {
      const bad=$("#mcpproblems"); bad.textContent="";
      view.problems.forEach(p => {
        const line=document.createElement("p");
        line.className="mcpbad"; line.textContent=p; bad.appendChild(line); });
      const box=$("#mcplist"); box.textContent="";
      if(!view.servers.length){
        const p=document.createElement("p");
        p.className="empty"; p.textContent="Nothing here yet.";
        box.appendChild(p); return; }
      view.servers.forEach(sv => this.drawMcpServer(box,sv,view.classes)); }); },

  drawMcpServer(box,sv,classes){
    const head=document.createElement("div");
    const open=this.mcpOpen.has(sv.name);
    head.className="mcphead"+(open?" open":"");
    const caret=document.createElement("span");
    caret.className="caret"; caret.textContent=String.fromCharCode(9654);
    // A SERVER NAME AND ITS COMMAND ARE OFF THE DISK, so they go in by
    // textContent and never into an HTML string -- the rail learned that in #119.
    const name=document.createElement("div");
    name.className="sname";
    name.textContent=sv.enabled?sv.name:sv.name+"  (switched off)";
    const cmd=document.createElement("div"); cmd.className="cmd";
    // THE URL WHERE THERE IS ONE, and there is never both -- one block is one
    // transport. A token is not in here: `headers` never reaches a view.
    cmd.textContent=sv.url||[sv.command].concat(sv.args).join(" ");
    cmd.title=cmd.textContent;
    const count=document.createElement("div"); count.className="count";
    count.textContent=sv.tools.length+" tools · "+sv.cost+" chars";
    const again=document.createElement("button");
    again.textContent="ask again";
    again.onclick=(e)=>{ e.stopPropagation(); this.refreshMcp(sv.name); };
    const gone=document.createElement("button");
    gone.textContent="remove";
    gone.onclick=(e)=>{ e.stopPropagation(); this.removeMcp(sv.name); };
    [caret,name,cmd,count,again,gone].forEach(el => head.appendChild(el));
    const tools=document.createElement("div");
    tools.className="mcptools"+(open?" open":"");
    // THE BUTTONS SIT INSIDE THE HEAD, so a click on one bubbles up to the fold
    // -- the same trap the memory tile hit on 2026-08-22, where `discard` would
    // have folded away the only record of what was being discarded.
    head.onclick=()=>{ const now=!this.mcpOpen.has(sv.name);
      if(now) this.mcpOpen.add(sv.name); else this.mcpOpen.delete(sv.name);
      head.classList.toggle("open",now); tools.classList.toggle("open",now); };
    box.appendChild(head);
    // THE TWO CONTROLS LIVE IN THE BODY, and that is forced rather than
    // chosen. Every button in the head has to stop its click from folding the
    // row away, and the case that guards the fold counts exactly two of those.
    // The body is a SIBLING of the head, so a click here never reaches the
    // fold and needs no third one.
    const bar=document.createElement("div"); bar.className="mcpbar";
    const sw=document.createElement("button");
    sw.className="sw"+(sv.enabled?" on":"");
    sw.title=sv.enabled?"its tools are in the prompt":"not in the prompt";
    const what=document.createElement("span"); what.className="swlabel";
    what.textContent=sv.enabled?"in the prompt":"switched off";
    sw.onclick=()=>this.toggleServer(sv.name,sw,what);
    bar.appendChild(sw); bar.appendChild(what);
    // A KEY IS AN HTTP IDEA. A stdio server is a local subprocess that carries
    // no headers at all, so a field there would take a secret and drop it on
    // the floor -- the same thing the reference says about stdio transports.
    if(sv.url){
      const key=document.createElement("input");
      key.type="password"; key.className="mcpkey";
      key.placeholder=sv.key?"key stored":"api key";
      key.title="sent as Authorization: Bearer, unless the server has a token "
               +"or mcp.json sets that header itself";
      // ON COMMIT, NOT ON EVERY KEYSTROKE, and the box is emptied afterwards:
      // what is stored is never read back into the page.
      key.onchange=()=>{ this.setServerKey(sv.name,key.value,key); };
      bar.appendChild(key); }
    tools.appendChild(bar);
    if(!sv.tools.length){
      const p=document.createElement("p");
      p.className="empty"; p.textContent="It offered nothing.";
      tools.appendChild(p); }
    sv.tools.forEach(t => tools.appendChild(this.mcpRow(sv,t,classes)));
    box.appendChild(tools); },

  mcpRow(sv,t,classes){
    const row=document.createElement("div");
    row.className="srow"+(t.included?"":" off");
    const text=document.createElement("div"); text.className="stext";
    const name=document.createElement("div");
    name.className="sname"; name.textContent=t.name;
    const desc=document.createElement("div");
    desc.className="sdesc"; desc.textContent=t.description||"(no description)";
    text.appendChild(name); text.appendChild(desc);
    const seg=document.createElement("div"); seg.className="seg";
    classes.forEach(k => {
      const b=document.createElement("button"); b.textContent=k;
      // SOLID IS A DECISION, DASHED IS THE SERVER'S GUESS. Nothing is marked as
      // chosen that nobody chose -- and where nobody has, the strict default is
      // what actually applies, because needs_approval has never heard the name.
      if(t.class===k) b.classList.add("on");
      else if(!t.class && t.proposed===k){ b.classList.add("guess");
        b.title="what the server suggests. Nothing is stored until you pick one"; }
      b.onclick=()=>this.setMcpClass(sv.name,t,k);
      seg.appendChild(b); });
    const sw=document.createElement("button");
    sw.className="sw"+(t.included?" on":"");
    sw.title=t.included?"the model can call it":"not in the tool list";
    sw.onclick=()=>this.tickMcp(sv.name,t,row,sw);
    row.appendChild(text); row.appendChild(seg); row.appendChild(sw);
    return row; },

  tickMcp(server,tool,row,sw){
    // PAINTED FIRST, WRITTEN SECOND, the way toggleSkill does it -- and PUT BACK
    // if the write is refused, which toggleSkill has no way to be.
    const on=!sw.classList.contains("on");
    sw.classList.toggle("on",on); row.classList.toggle("off",!on);
    pywebview.api.mcp_confirm(server,tool.tool,on,null).then(said => {
      if(said){ sw.classList.toggle("on",!on); row.classList.toggle("off",on);
                this.mcpSaid(said); return; }
      this.mcpSaid(""); this.drawMcp(); }); },

  setMcpClass(server,tool,klass){
    pywebview.api.mcp_confirm(server,tool.tool,tool.included,klass).then(said => {
      this.mcpSaid(said||""); if(!said) this.drawMcp(); }); },

  addMcp(){
    const line=$("#mcpline").value.trim();
    if(!line){ this.mcpSaid("a command line or a URL."); return; }
    // A URL MAY TAKE MINUTES AND A BROWSER, and the sheet has to say so. A
    // server that answers 401 sends this off to its own consent page, and until
    // somebody finishes it there this call does not return -- "asking ..." alone
    // reads as frozen.
    this.mcpSaid(/^https?:/i.test(line)
      ? "asking … if it wants a login, a browser opens; finish it there"
      : "asking …");
    pywebview.api.mcp_add(line).then(said => {
      this.mcpSaid(said||"");
      if(!said) $("#mcpline").value="";
      this.drawMcp(); }); },

  refreshMcp(name){
    // ASK AGAIN IS ALSO THE AUTHORISE PATH: it goes through `mcp_add_server`,
    // which answers a 401 by running the browser leg. So the window needs no
    // second control for it, and the head keeps its two buttons.
    this.mcpSaid("asking "+name+" again …");
    pywebview.api.mcp_refresh(name).then(said => {
      this.mcpSaid(said||""); this.drawMcp(); }); },

  removeMcp(name){
    this.mcpOpen.delete(name);
    pywebview.api.mcp_remove(name).then(said => {
      this.mcpSaid(said||""); this.drawMcp(); }); },

  mcpSaid(text){ $("#mcpsaid").textContent=text; },
  // WHICH SERVERS ARE UNFOLDED, kept in the page rather than on disk: it is
  // where somebody is looking right now, not a setting they chose.
  mcpOpen: new Set(),

  toggleSkill(name,row,sw){
    // PAINTED FIRST, WRITTEN SECOND, the way `setTheme` does it: the write is a
    // file, and a click that waits for one feels broken.
    const on=!sw.classList.contains("on");
    sw.classList.toggle("on",on);
    row.classList.toggle("off",!on);
    sw.title=on?"in the prompt":"not in the prompt";
    pywebview.api.toggle_skill(name,on);
  },
  // THE SHEET IS REDRAWN AFTERWARDS, unlike toggleSkill. A server's tools are
  // worth hundreds of thousands of characters, and the row prints that number
  // -- painting the switch and leaving a stale "0 chars" beside it is the one
  // way this control could lie about what it just did.
  toggleServer(name,sw,label){
    const on=!sw.classList.contains("on");
    sw.classList.toggle("on",on);
    sw.title=on?"its tools are in the prompt":"not in the prompt";
    label.textContent=on?"in the prompt":"switched off";
    pywebview.api.toggle_server(name,on).then(()=>this.drawMcp());
  },
  setServerKey(name,value,field){
    // EMPTIED EITHER WAY. A stored key is never read back into the page, so an
    // untouched field showing dots would be the page inventing a value it does
    // not have.
    pywebview.api.set_server_key(name,value).then(()=>{
      field.value=""; this.drawMcp(); });
  },
  closeSettings(){ $("#settings").hidden=true; },
  // THE BACKDROP CLOSES, THE SHEET DOES NOT. Without the target test a click on
  // anything inside the panel bubbles up here and shuts it.
  settingsBackdrop(e){ if(e.target.id==="settings") this.closeSettings(); },

  // THE MODEL PAGE AND THE KEY PAGE READ THE SAME VIEW, because they are two
  // halves of one fact: a provider with no key cannot be chosen, and a key with
  // no provider is a string in a file. Asked for on every open rather than kept
  // in the page -- a key entered on the second tab changes what the first one
  // may offer.
  foldPane(which){
    const head=$("#"+which+"fold"), body=$("#"+which+"body");
    const now=!head.classList.contains("open");
    head.classList.toggle("open",now); body.classList.toggle("open",now); },

  // THE TWO MARKS, HELD HERE AND NOT FETCHED. The page has no external host to
  // load an image from -- and on the one screen where somebody is about to sign
  // in, a remote asset is also a request that says when they opened it. The
  // geometry is the providers own: the wordmark off anthropic.com and the knot
  // out of openai.com/favicon.svg, whose box is the PATH's own bounds rather
  // than the file's 180-square -- most of that square is a background this does
  // not draw, and inside a 26px tile the padding would have eaten the mark.
  //
  // `currentColor` AND NOTHING ELSE, so the colour stays a palette decision one
  // level up. A literal here would be the next theme's problem.
  MARKS: {
    anthropic: {box: "0 0 35 24", d: ["M24.5475 0H19.3384L28.8374 24H34.0465L24.5475 0Z", "M9.49897 0L0 24H5.31125L7.25395 18.96H17.1914L19.1341 24H24.4454L14.9464 0H9.49897ZM8.97193 14.5029L12.2227 6.06857L15.4735 14.5029H8.97193Z"]},
    openai: {box: "29 29 122 122", d: ["M75.91 73.628V62.232c0-.96.36-1.68 1.199-2.16l22.912-13.194c3.119-1.8 6.838-2.639 10.676-2.639 14.394 0 23.511 11.157 23.511 23.032 0 .839 0 1.799-.12 2.758l-23.752-13.914c-1.439-.84-2.879-.84-4.318 0L75.91 73.627Zm53.499 44.383v-27.23c0-1.68-.72-2.88-2.159-3.719L97.142 69.55l9.836-5.638c.839-.48 1.559-.48 2.399 0l22.912 13.195c6.598 3.839 11.035 11.995 11.035 19.912 0 9.116-5.397 17.513-13.915 20.992v.001Zm-60.577-23.99-9.836-5.758c-.84-.48-1.2-1.2-1.2-2.16v-26.39c0-12.834 9.837-22.55 23.152-22.55 5.039 0 9.716 1.679 13.676 4.678L70.993 55.516c-1.44.84-2.16 2.039-2.16 3.719v34.787-.002Zm21.173 12.234L75.91 98.339V81.546l14.095-7.917 14.094 7.917v16.793l-14.094 7.916Zm9.056 36.467c-5.038 0-9.716-1.68-13.675-4.678l23.631-13.676c1.439-.839 2.159-2.038 2.159-3.718V85.863l9.956 5.757c.84.48 1.2 1.2 1.2 2.16v26.389c0 12.835-9.957 22.552-23.27 22.552v.001Zm-28.43-26.75L47.72 102.778c-6.599-3.84-11.036-11.996-11.036-19.913 0-9.236 5.518-17.513 14.034-20.992v27.35c0 1.68.72 2.879 2.16 3.718l29.989 17.393-9.837 5.638c-.84.48-1.56.48-2.399 0Zm-1.318 19.673c-13.555 0-23.512-10.196-23.512-22.792 0-.959.12-1.919.24-2.879l23.63 13.675c1.44.84 2.88.84 4.32 0l30.108-17.392v11.395c0 .96-.361 1.68-1.2 2.16l-22.912 13.194c-3.119 1.8-6.837 2.639-10.675 2.639Zm29.748 14.274c14.515 0 26.63-10.316 29.39-23.991 13.434-3.479 22.071-16.074 22.071-28.91 0-8.396-3.598-16.553-10.076-22.43.6-2.52.96-5.039.96-7.557 0-17.153-13.915-29.99-29.989-29.99-3.239 0-6.358.48-9.477 1.56-5.398-5.278-12.835-8.637-20.992-8.637-14.515 0-26.63 10.316-29.39 23.991-13.434 3.48-22.07 16.074-22.07 28.91 0 8.396 3.598 16.553 10.075 22.431-.6 2.519-.96 5.038-.96 7.556 0 17.154 13.915 29.989 29.99 29.989 3.238 0 6.357-.479 9.476-1.559 5.397 5.278 12.835 8.637 20.992 8.637Z"]},
  },

  subMark(name){
    const NS="http://www.w3.org/2000/svg";
    const spec=this.MARKS[name];
    const svg=document.createElementNS(NS,"svg");
    svg.setAttribute("class","mark");
    svg.setAttribute("fill","currentColor");
    if(!spec) return svg;
    svg.setAttribute("viewBox",spec.box);
    spec.d.forEach(d => { const p=document.createElementNS(NS,"path");
      p.setAttribute("d",d); svg.appendChild(p); });
    return svg; },

  drawSubs(){
    pywebview.api.provider_view().then(view => {
      const box=$("#subs"); box.textContent="";
      (view.subscriptions||[]).forEach(s => {
        const tile=document.createElement("button");
        tile.className="sub"+(s.signed_in?" on":"");
        const n=document.createElement("div");
        n.className="sname"; n.textContent=s.label;
        const d=document.createElement("div");
        // WHAT IS MISSING IS ON THE TILE, not behind the click. A provider that
        // cannot open a browser yet has one reason and it is the same one every
        // time -- and it names the file and the key, not the concept.
        // ONE SENTENCE, WHEREVER IT BELONGS. The tile says what it needs; the
        // line under the sheet does not repeat it, because a message that
        // arrives on click and already stands two inches above it reads as
        // nothing having happened.
        //
        // FOUR STATES, AND THE ORDER IS THE ORDER THE CREDENTIAL RESOLVER USES:
        // Crow's own sign-in, then the one another program on this machine
        // holds, then a client_id waiting to be used, then nothing yet. A tile
        // that offered the borrowed login while Crow had its own would be
        // offering the weaker of two.
        d.className="sdesc";
        const st=document.createElement("div");
        st.className="state";
        if(s.signed_in){
          d.textContent=s.from_env?("Signed in through "+s.name.toUpperCase()
            +"'s environment variable."):s.blurb;
          st.textContent="signed in"; }
        else if(s.command){
          // THE DOCUMENTED WAY IN, and it is a command rather than a browser:
          // `claude setup-token` mints a long-lived token FOR another program,
          // which is what Crow is here. No client_id borrowed from a product
          // that never granted one, and nothing refreshed that belongs to
          // somebody else.
          d.textContent="Run " + s.command + " in a terminal and paste what it "
            + "prints. It is a token your subscription mints for other programs.";
          st.textContent="paste a token"; }
        else if(s.borrowing){
          // THE STALE LINE IS A WARNING AND NOT A STATE. The token is sent
          // either way; what a provider does with it is the provider's answer,
          // and this only says why a refusal would not be a surprise.
          d.textContent=s.stale||("Using the "+s.product+" sign-in on this "
            +"machine. Crow reads it and never writes it.");
          st.textContent="signed in · "+s.product; }
        else if(s.borrowable){
          d.textContent="There is a "+s.product+" sign-in on this machine. One "
            +"click uses it — requests then carry its grant.";
          st.textContent="use "+s.product; }
        else if(s.ready){ d.textContent=s.blurb; st.textContent="connect"; }
        else{
          d.textContent="Needs a client_id — "+(s.discovers
            ?"this one publishes the rest itself."
            :"and the two endpoints, which it does not publish.");
          st.textContent="set it up"; }
        tile.appendChild(this.subMark(s.name));
        tile.appendChild(n); tile.appendChild(d); tile.appendChild(st);
        // A CLICK IS ALWAYS THE NEXT STEP, never a refusal. Whichever of the
        // four states the tile is in, the control does the thing that state is
        // one step away from.
        tile.onclick=()=>{
          if(s.signed_in || s.borrowing) return;
          if(s.command) this.tokenForm(s);
          else if(s.borrowable) this.borrowSub(s.name,true);
          else if(s.ready) this.connectSub(s.name);
          else this.subForm(s); };
        box.appendChild(tile);
        if(s.signed_in || s.borrowing){
          const out=document.createElement("button");
          out.className="subout";
          out.textContent=s.borrowing?"stop using it":"sign out";
          // THE BUTTON SITS BESIDE THE TILE, NOT INSIDE IT. Inside, a click on
          // it would bubble into the tile and start the browser leg it was
          // meant to end -- the trap the MCP head already carries a note about.
          out.onclick=()=>{ if(s.borrowing) this.borrowSub(s.name,false);
                            else this.signOutSub(s.name); };
          box.appendChild(out); } }); }); },

  borrowSub(name,on){
    pywebview.api.provider_borrow(name,on).then(said => {
      $("#subsaid").textContent=said||"";
      this.drawSubs(); this.drawProviders(); }); },

  // WHAT `claude setup-token` PRINTS, pasted. The command is shown rather than
  // run: it is the other product's, it wants a terminal of its own, and a
  // client that ran it for somebody would be deciding on their behalf what to
  // mint against their subscription.
  tokenForm(s){
    const old=$("#subform");
    if(old && old.dataset.name===s.name){ old.remove(); return; }
    if(old) old.remove();
    const box=document.createElement("div");
    box.id="subform"; box.className="sform"; box.dataset.name=s.name;
    const hint=document.createElement("p");
    hint.className="shint";
    hint.textContent="Run this in a terminal, then paste what it prints:";
    const cmd=document.createElement("input");
    cmd.value=s.command; cmd.readOnly=true;
    cmd.onclick=()=>cmd.select();
    const field=document.createElement("input");
    field.type="password"; field.placeholder=s.hint||"the token";
    field.onkeydown=(e)=>{ if(e.key==="Enter") save(); };
    const save=()=>{
      // THE SAME TRAP, and the same answer: signing out is the side button, not
      // an empty field somebody pressed Save on.
      const value=field.value.trim();
      if(!value){
        $("#subsaid").textContent="nothing pasted. `sign out` is what clears one.";
        return; }
      pywebview.api.provider_token(s.name, value).then(said => {
        field.value="";
        $("#subsaid").textContent=said||"";
        if(!said){ box.remove(); this.drawSubs(); this.drawProviders(); } }); };
    const go=document.createElement("button");
    go.textContent="Save"; go.onclick=save;
    [hint, cmd, field, go].forEach(el => box.appendChild(el));
    // THE BORROWED SESSION STAYS REACHABLE and stays second: it is built and
    // tested, and it is the one that came back 429.
    if(s.borrowable){
      const other=document.createElement("button");
      other.className="subout";
      other.textContent="or use this machine's "+s.product+" sign-in";
      other.onclick=()=>{ box.remove(); this.borrowSub(s.name,true); };
      box.appendChild(other); }
    $("#subs").after(box); },

  // THE FORM THAT REPLACES AN EDITOR. It is drawn on demand rather than always:
  // a provider that is set up has nothing to fill in, and three empty boxes
  // under a working tile would read as three things still to do.
  subForm(s){
    const old=$("#subform");
    if(old && old.dataset.name===s.name){ old.remove(); return; }
    if(old) old.remove();
    const box=document.createElement("div");
    box.id="subform"; box.className="sform"; box.dataset.name=s.name;
    const hint=document.createElement("p");
    hint.className="shint";
    hint.textContent=s.label+" — paste the values, then click the tile again "
      + "to sign in.";
    box.appendChild(hint);
    const fields={};
    const LABEL={client_id:"client_id", authorize:"authorization endpoint",
                 token:"token endpoint"};
    s.wants.forEach(key => {
      const input=document.createElement("input");
      input.placeholder=LABEL[key]+(s.has[key]?"  (stored)":"");
      input.onkeydown=(e)=>{ if(e.key==="Enter") save(); };
      fields[key]=input; box.appendChild(input); });
    const save=()=>{
      const out={};
      // AN UNTOUCHED BOX IS NOT AN EMPTY ONE. Sending "" for a field somebody
      // left alone would clear a value they never meant to remove -- and the
      // stored ones are never read back into the page, so a blank box is the
      // normal state of a filled key.
      Object.keys(fields).forEach(k => { const v=fields[k].value.trim();
        if(v) out[k]=v; });
      pywebview.api.provider_oauth(s.name,out).then(said => {
        $("#subsaid").textContent=said||"";
        if(!said){ box.remove(); this.drawSubs(); } }); };
    const go=document.createElement("button");
    go.textContent="Save"; go.onclick=save;
    box.appendChild(go);
    $("#subs").after(box); },

  connectSub(name){
    // A BROWSER LEG IS MINUTES, and the sheet has to say so. Somebody reading a
    // consent screen is not somebody watching a frozen window.
    $("#subsaid").textContent="the browser is opening \u2026";
    pywebview.api.provider_authorise(name).then(said => {
      $("#subsaid").textContent=said||"";
      this.drawSubs(); this.drawProviders(); }); },

  signOutSub(name){
    pywebview.api.provider_signout(name).then(said => {
      $("#subsaid").textContent=said||"";
      this.drawSubs(); this.drawProviders(); }); },

  drawProviders(){
    pywebview.api.provider_view().then(view => {
      this.provView=view;
      const box=$("#provbody"); box.textContent="";
      // robins Korrektur 2026-08-28: der Broker wird auf seiner EIGENEN Seite
      // gezeichnet, KOMPLETT raus aus der Model-Seite.
      view.providers.filter(p => p.name!=="openrouter")
        .forEach(p => box.appendChild(this.provRow(p,view.active)));
      const on=view.providers.find(p => p.name===view.active) || null;
      $("#provcount").textContent=on?on.label:"";
      this.drawModels(view);
      this.drawOpenRouter(view);
      this.drawKeys(view);
      // THE ONE LINE A REMOTE ENDPOINT OWES, and only where it is true. Under
      // the local server it is absent rather than negated: a screen that says
      // what is NOT happening teaches nobody anything.
      $("#provnote").textContent=(on&&on.remote)?view.note:""; }); },

  provRow(p,active){
    const row=document.createElement("div");
    row.className="srow"+(p.name===active?"":" off");
    const text=document.createElement("div"); text.className="stext";
    const n=document.createElement("div"); n.className="sname"; n.textContent=p.label;
    const d=document.createElement("div"); d.className="sdesc";
    // WHAT IS MISSING IS SAID ON THE ROW, not after the click -- and a provider
    // that is signed in is not missing anything. Reading only the key box left
    // the row saying "needs a key first" under a switch that was already on,
    // which is the sheet contradicting itself in one line.
    const held=p.has_key||p.signed_in||p.borrowing;
    d.textContent=p.blurb+(p.needs_key&&!held?"  ·  needs a key or a sign-in":"");
    text.appendChild(n); text.appendChild(d);
    const sw=document.createElement("button");
    sw.className="sw"+(p.name===active?" on":"");
    sw.title=p.name===active?"turns go here":"use this one";
    sw.onclick=()=>this.pickProvider(p.name);
    row.appendChild(text); row.appendChild(sw);
    return row; },

  // robins Regel vom 2026-08-28, in ihrer dritten und letzten Form: der
  // Broker hat seine EIGENE Seite, und DIE SEITE ROUTET GAR NICHTS. Default
  // ist immer lokal, bis der User auf der Model-Seite etwas anderes waehlt --
  // der Schalter parkt nur das Subsystem (Delegation, Katalog, Favoriten),
  // der Picker konfiguriert, und keine Zeile hier bewegt einen Turn. Die
  // Turns-Zeile der Zwischenfassung erlebte robin als "automatisch" und flog
  // am selben Abend wieder raus. #148s Dropdowns: GANZER Katalog, ein
  // bezahlter Favorit ist Nutzerwahl.
  drawOpenRouter(view){
    const box=$("#orbody"); if(!box) return; box.textContent="";
    const p=view.providers.find(x => x.name==="openrouter");
    if(!p) return;
    const lit=view.openrouter_on!==false;
    const r=document.createElement("div"); r.className="srow"+(lit?"":" off");
    const t=document.createElement("div"); t.className="stext";
    const n=document.createElement("div"); n.className="sname";
    n.textContent="OpenRouter";
    const d=document.createElement("div"); d.className="sdesc";
    const held=p.has_key||p.signed_in||p.borrowing;
    d.textContent=p.blurb+(p.needs_key&&!held?"  ·  needs a key or a sign-in":"");
    t.appendChild(n); t.appendChild(d);
    const osw=document.createElement("button");
    osw.className="sw"+(lit?" on":""); osw.id="orsw";
    osw.title=lit?"in operation":"parked";
    osw.onclick=()=>this.orSwitch(!lit);
    r.appendChild(t); r.appendChild(osw); box.appendChild(r);
    const all=(p.models||[]);
    const favs=view.delegate_favorites||[];
    const cfg=document.createElement("div"); cfg.className="orcfg";
    const head=document.createElement("p"); head.className="mcpcost";
    head.textContent="Delegate favourites — tried in this order; unset means the free default:";
    cfg.appendChild(head);
    for(let i=0;i<3;i++){
      const fsel=document.createElement("select");
      fsel.className="msel favsel";
      const none=document.createElement("option");
      none.value=""; none.textContent="— favourite "+(i+1)+" —";
      fsel.appendChild(none);
      all.forEach(m => {
        const o=document.createElement("option");
        o.value=m.id; o.textContent=m.id;
        if(m.id===favs[i]) o.selected=true;
        fsel.appendChild(o); });
      fsel.onchange=()=>{
        const picked=[...cfg.querySelectorAll(".favsel")]
          .map(s => s.value).filter(Boolean);
        pywebview.api.delegate_favorites_set(picked).then(
          why => { if(why) this.note(why); else this.drawProviders(); }); };
      cfg.appendChild(fsel); }
    if(!all.length){
      const q=document.createElement("p"); q.className="empty";
      q.textContent="No model list yet — add the OpenRouter key, the list follows it.";
      cfg.appendChild(q); }
    box.appendChild(cfg);
    // The picker CONFIGURES, it does not route: a free pick here is what the
    // delegate default reads, and the Turns row is the only mover. Slugs are
    // drawn by value and textContent, the #119 rule verbatim.
    const mods=document.createElement("div"); mods.className="orcfg";
    if(all.length){
      const mh=document.createElement("p"); mh.className="mcpcost";
      mh.textContent="Model — "+(p.count||all.length)+" in the catalogue:";
      mods.appendChild(mh);
      const sel=document.createElement("select"); sel.className="msel";
      const none=document.createElement("option");
      none.value=""; none.textContent="— pick a model —";
      if(!p.model) none.selected=true;
      sel.appendChild(none);
      all.forEach(m => {
        const o=document.createElement("option");
        o.value=m.id; o.textContent=m.id;
        if(m.id===p.model) o.selected=true;
        sel.appendChild(o); });
      sel.onchange=()=>this.orModel(sel.value);
      mods.appendChild(sel);
      const ctx=document.createElement("p"); ctx.className="mcpcost";
      const mrow=all.find(m => m.id===p.model);
      ctx.textContent=!p.model ? ""
        : (mrow&&mrow.context ? mrow.context.toLocaleString("en-US")+" tokens declared"
                              : "no context declared — the bar stays off");
      mods.appendChild(ctx); }
    const again=document.createElement("div"); again.className="keyrow";
    const slug=document.createElement("input");
    slug.placeholder=p.model||"model id, as the provider lists it";
    slug.onkeydown=(e)=>{ if(e.key==="Enter"&&slug.value.trim())
      this.orModel(slug.value.trim()); };
    const use=document.createElement("button");
    use.textContent="Use"; use.onclick=()=>{ if(slug.value.trim())
      this.orModel(slug.value.trim()); };
    const b=document.createElement("button");
    b.textContent=all.length?"ask again":"ask for the list";
    b.onclick=()=>this.orAsk();
    again.appendChild(slug); again.appendChild(use); again.appendChild(b);
    mods.appendChild(again);
    box.appendChild(mods); },

  drawModels(view){
    const box=$("#modbody"); box.textContent="";
    const p=view.providers.find(x => x.name===view.active);
    $("#modcount").textContent="";
    if(!p) return;
    if(p.name==="openrouter"){
      // KOMPLETT raus (robin, 2026-08-28): the broker's picker lives on its
      // own page, and no page routes turns there any more. This state is a
      // leftover file or a core-level pick; the fold says the way home that
      // exists -- the chip's local boot writes the provider back.
      const q=document.createElement("p"); q.className="empty";
      q.textContent="Turns go to OpenRouter — booting a local model on the chip brings them home.";
      box.appendChild(q); return; }
    if(!p.listable){
      // THE LOCAL SERVER IS NOT ASKED WHAT IT COULD SERVE. It has one model
      // open and /props says which, so a picker here would offer a choice the
      // endpoint cannot take.
      const q=document.createElement("p"); q.className="empty";
      q.textContent="Whatever llama-server has open — the chip at the top says which.";
      box.appendChild(q); return; }
    $("#modcount").textContent=p.count?p.count+" models":"";
    if(!p.models.length){
      const q=document.createElement("p"); q.className="empty";
      q.textContent="No list yet."; box.appendChild(q); }
    else{
      const sel=document.createElement("select"); sel.className="msel";
      const none=document.createElement("option");
      none.value=""; none.textContent="— pick a model —";
      if(!p.model) none.selected=true;
      sel.appendChild(none);
      p.models.forEach(m => {
        const o=document.createElement("option");
        // BY value AND textContent, never into an HTML string: a slug is a
        // foreign name off a foreign catalogue, the rule #119 was cut for.
        o.value=m.id; o.textContent=m.id;
        if(m.id===p.model) o.selected=true;
        sel.appendChild(o); });
      sel.onchange=()=>this.pickModel(p.name,sel.value);
      box.appendChild(sel);
      const ctx=document.createElement("p"); ctx.className="mcpcost";
      const row=p.models.find(m => m.id===p.model);
      // DECLARED, AND IT SAYS SO. /props reports what a server allocated; this
      // is what a catalogue claims, and the two are not the same kind of number.
      ctx.textContent=!p.model ? ""
        : (row&&row.context ? row.context.toLocaleString("en-US")+" tokens declared"
                            : "no context declared — the bar stays off");
      box.appendChild(ctx); }
    // TYPING A SLUG IS ALWAYS OPEN, and that is not a fallback -- it is what
    // keeps the picker from depending on a list somebody else has to hand over.
    // Measured 2026-08-23: Anthropic's /v1/models answered a borrowed sign-in
    // with 401, and a Model page that can only offer what a catalogue returned
    // is a page with no way forward the moment one refuses. Crow sends the slug
    // as typed either way.
    const again=document.createElement("div"); again.className="keyrow";
    const slug=document.createElement("input");
    slug.placeholder=p.model||"model id, as the provider lists it";
    slug.onkeydown=(e)=>{ if(e.key==="Enter"&&slug.value.trim())
      this.pickModel(p.name,slug.value.trim()); };
    const use=document.createElement("button");
    use.textContent="Use"; use.onclick=()=>{ if(slug.value.trim())
      this.pickModel(p.name,slug.value.trim()); };
    const b=document.createElement("button");
    b.textContent=p.models.length?"ask again":"ask for the list";
    b.onclick=()=>this.refreshProvider(p.name);
    again.appendChild(slug); again.appendChild(use); again.appendChild(b);
    box.appendChild(again); },

  drawKeys(view){
    const box=$("#keylist"); box.textContent="";
    view.providers.filter(p => p.needs_key).forEach(p => {
      const row=document.createElement("div"); row.className="srow";
      const text=document.createElement("div"); text.className="stext";
      const n=document.createElement("div"); n.className="sname"; n.textContent=p.label;
      const d=document.createElement("div"); d.className="sdesc";
      d.textContent=p.has_key?p.key:"not set";
      text.appendChild(n); text.appendChild(d);
      const wrap=document.createElement("div"); wrap.className="keyrow";
      const box2=document.createElement("input");
      box2.type="password"; box2.placeholder=p.key_hint;
      // ENTER SAVES, because a key is pasted and a paste ends with a return.
      box2.onkeydown=(e)=>{ if(e.key==="Enter") this.saveKey(p.name,box2); };
      const save=document.createElement("button");
      save.textContent="Save"; save.onclick=()=>this.saveKey(p.name,box2);
      wrap.appendChild(box2); wrap.appendChild(save);
      if(p.has_key){
        const gone=document.createElement("button");
        gone.textContent="Remove";
        gone.onclick=()=>this.clearKey(p.name);
        wrap.appendChild(gone); }
      text.appendChild(wrap);
      box.appendChild(row); row.appendChild(text); }); },

  // AN UNTOUCHED BOX IS NOT AN EMPTY ONE, and getting that wrong here cost
  // robin his stored key on 2026-08-23: the stored value is never read back
  // into the field, so a BLANK box is the normal state of a key that is set --
  // and Save sent the blank on, which the core reads as "clear it". Only
  // `Remove` clears now, and it says so.
  saveKey(name,box){
    const value=box.value.trim();
    if(!value){
      $("#keysaid").textContent="nothing typed. `Remove` is what clears a stored key.";
      return; }
    pywebview.api.provider_key(name,value).then(said => {
      box.value="";
      $("#keysaid").textContent=said||"";
      // A KEY LANDING IS ALSO A CATALOGUE ARRIVING, and asking for it here is
      // the only moment a person expects to wait. The list is on disk after
      // this, so no later screen has to reach the network to open.
      if(!said) this.drawProviders(); }); },

  clearKey(name){
    pywebview.api.provider_key(name,"").then(said => {
      $("#keysaid").textContent=said||"";
      if(!said){ this.drawProviders(); this.drawSubs(); } }); },

  pickProvider(name){
    $("#provsaid").textContent="";
    pywebview.api.provider_pick(name,null).then(said => {
      $("#provsaid").textContent=said||""; this.drawProviders(); }); },

  pickModel(name,slug){
    $("#provsaid").textContent="";
    pywebview.api.provider_pick(name,slug).then(said => {
      $("#provsaid").textContent=said||""; this.drawProviders(); }); },

  refreshProvider(name){
    $("#provsaid").textContent="asking …";
    pywebview.api.provider_refresh(name).then(said => {
      $("#provsaid").textContent=said||""; this.drawProviders(); }); },

  // The broker page's doors. Said-lines land on ITS page, not the Model
  // page's -- two pages sharing one mouth would blame the wrong one.
  orSwitch(on){
    $("#orsaid").textContent="";
    pywebview.api.openrouter_set(on).then(said => {
      $("#orsaid").textContent=said||""; this.drawProviders(); }); },

  orModel(slug){
    $("#orsaid").textContent="";
    pywebview.api.provider_model_set("openrouter",slug).then(said => {
      $("#orsaid").textContent=said||""; this.drawProviders(); }); },

  orAsk(){
    $("#orsaid").textContent="asking …";
    pywebview.api.provider_refresh("openrouter").then(said => {
      $("#orsaid").textContent=said||""; this.drawProviders(); }); },

  railDrag(ev){
    // AUF document UND NICHT AUF DEM GRIFF. Die Maus verlaesst fuenf Pixel in
    // der ersten Bewegung, und ein Listener auf dem Griff verloere sie dort.
    ev.preventDefault();
    const rail=$("#rail"), grip=$("#railgrip");
    grip.classList.add("on"); rail.classList.add("dragging");
    const left=rail.getBoundingClientRect().left;
    const move=e=>{
      // GEKLEMMT IN DER SEITE UND NOCH EINMAL IN PYTHON. Der Wert kommt aus
      // einer Maus, und eine Rail von zwoelf Pixeln ist keine Rail.
      const w=Math.max(180,Math.min(520,Math.round(e.clientX-left)));
      document.documentElement.style.setProperty("--railw",w+"px"); };
    const up=()=>{
      document.removeEventListener("mousemove",move);
      document.removeEventListener("mouseup",up);
      grip.classList.remove("on"); rail.classList.remove("dragging");
      pywebview.api.rail_width(Math.round(
        rail.getBoundingClientRect().width)); };
    document.addEventListener("mousemove",move);
    document.addEventListener("mouseup",up); },

  // #138. GESPIEGELT: die Rail misst von ihrer linken Kante nach rechts, das
  // Panel von seiner rechten Kante nach links. Sonst ist es dieselbe Geste, und
  // die Grenzen sind auch hier zweimal da -- in der Seite und in Python.
  codeDrag(ev){
    ev.preventDefault();
    const panel=$("#code"), grip=$("#codegrip");
    grip.classList.add("on"); panel.classList.add("dragging");
    const right=panel.getBoundingClientRect().right;
    const move=e=>{
      const w=Math.max(260,Math.min(612,Math.round(right-e.clientX)));
      document.documentElement.style.setProperty("--codew",w+"px"); };
    const up=()=>{
      document.removeEventListener("mousemove",move);
      document.removeEventListener("mouseup",up);
      grip.classList.remove("on"); panel.classList.remove("dragging");
      pywebview.api.code_width(Math.round(
        panel.getBoundingClientRect().width)); };
    document.addEventListener("mousemove",move);
    document.addEventListener("mouseup",up); },

  // DIE STIMME ALS ZEILE. Der Pegel kommt aus Python, weil die Seite kein
  // sicherer Kontext ist und `getUserMedia` dort scheitert -- dieselbe Naht,
  // ueber die der fertige Text zurueckkommt.
  voice(e){
    const band=$("#voice");
    // SO VIELE BALKEN, WIE HINEINPASSEN. Drei Pixel breit, vier Abstand, also
    // einer je sieben -- und neu gebaut, sobald die Maske ihre Breite aendert.
    // Eine feste Zahl liess das Band bei jeder anderen Fensterbreite entweder
    // auslaufen oder auf halber Strecke enden.
    const want=Math.max(8,Math.floor((band.clientWidth||300)/7));
    if(band.childElementCount!==want){
      band.textContent="";
      for(let i=0;i<want;i++) band.appendChild(document.createElement("i")); }
    const bars=band.children;
    for(let i=0;i<bars.length-1;i++){
      bars[i].style.height=bars[i+1].style.height;
      bars[i].style.opacity=bars[i+1].style.opacity; }
    // DIE SKALA MISST SICH SELBST. Ein fester Faktor muesste die Verstaerkung
    // des Mikrofons raten: float32-Sprache liegt bei 0,05 bis 0,3, und der
    // erste Entwurf rechnete `level*22` -- vier Pixel, also der Boden, waehrend
    // robin sprach und der Text danach sauber zurueckkam. Ein mitlaufender
    // Spitzenwert mit Abklingen ist, was ein Aussteuerungsmesser tut: laut
    // zieht ihn hoch, Stille laesst ihn um drei Prozent je Bild sinken. Der
    // Boden von 0,02 ist es, was verhindert, dass Rauschen auf volle Hoehe
    // normiert wird.
    this.vpeak=Math.max(e.level,(this.vpeak||0.02)*0.97,0.02);
    const share=e.level/this.vpeak;
    // DREI PIXEL BODEN, UND DAS IST DIE BREITE: so tief gerundet ist der
    // ruhende Balken ein Punkt und keine kurze Linie. Die halbe Deckkraft
    // dazu trennt Stille von Sprache, ohne eine zweite Farbe einzufuehren.
    const h=Math.max(3,Math.min(20,Math.round(3+share*17)));
    const last=bars[bars.length-1];
    last.style.height=h+"px";
    last.style.opacity=h>5?"0.9":"0.45";
    if(e.level>0) band.hidden=false; },

  settingsCat(name){
    // #126. THE KEY IS ON THE BUTTON, not in a list beside it. It used to be a
    // positional array -- a fifth button with a four-name list would mark the
    // wrong tab, and the fault would look like a CSS problem. The panes below
    // were already keyed this way; now both halves read the same attribute.
    document.querySelectorAll("#scats button").forEach(
      b => b.classList.toggle("on", b.dataset.cat===name));
    document.querySelectorAll("#spane section").forEach(
      sec => sec.hidden = sec.dataset.cat!==name);
    // ASKED WHEN THE PANE OPENS, not on every start. It is a call to
    // github.com, and this window already refuses to spend one on a catalogue
    // nobody asked to see.
    if(name==="about") this.updateCheck();
  },

  updateCheck(){
    const said=$("#updsaid"), btn=$("#updbtn");
    said.textContent="checking github.com \u2026"; btn.hidden=true;
    pywebview.api.update_check().then(s=>{
      if(!s.latest){ said.textContent="could not reach github.com"; return; }
      // WHICH DIRECTORY IS ABOUT TO CHANGE, said before the button is offered.
      // A checkout runs from wherever it was cloned and the installer writes
      // %LOCALAPPDATA%\Crow either way, so without this line the reader would
      // press update and watch an unchanged copy.
      const where=s.installed_here?"":", and this window runs from a copy outside "
        +s.install_dir;
      said.textContent=(s.newer ? s.latest+" is out, this is "+s.current
                                 : s.latest+" is the newest release")+where;
      // OFFERED EITHER WAY, and the label is the difference. install.ps1
      // decides for itself what a run means: a newer release is an update, the
      // same one answers "already installed -- nothing to do" and exits before
      // it downloads anything. So the button is also the repair for a copy that
      // was interrupted, and it costs a second to find out.
      btn.textContent=(s.newer?"install ":"reinstall ")+s.latest;
      btn.disabled=false; btn.hidden=false; }); },

  updateRun(){
    const btn=$("#updbtn");
    btn.disabled=true;
    $("#updsaid").textContent="installing \u2026";
    pywebview.api.update_start().then(why=>{
      if(why){ btn.disabled=false; $("#updsaid").textContent=why; } }); },

  updated(e){
    $("#updsaid").textContent=e.t;
    if(e.done){ $("#updbtn").hidden=true; } },

  setTheme(name){
    // PAINTED FIRST, WRITTEN SECOND. The attribute swap is one frame; the write
    // is a file. A version that waited for Python made the click feel broken.
    document.documentElement.dataset.theme=name;
    document.querySelectorAll("#themes button").forEach(
      b => b.classList.toggle("on", b.dataset.theme===name));
    pywebview.api.set_theme(name);
  },

  // ONE BUTTON, TWO ERRANDS, and the class is the truth about which. The page
  // never decides that a recording started -- it asks, and Python says so on
  // the way back. A button that painted itself red on click would lie for as
  // long as it took PortAudio to refuse.
  mic(){ const b=$("#mic"); if(b.disabled) return;
    if(b.classList.contains("rec")) pywebview.api.dictate_stop();
    else pywebview.api.dictate_start(); },

  micState(e){
    const b=$("#mic");
    b.classList.toggle("rec", e.state==="rec");
    box.classList.toggle("rec", e.state==="rec");
    // DAS BAND GEHT MIT DEM KNOPF. Es einzublenden ist Sache des ersten
    // Pegels; es auszublenden gehoert hierher, weil "es wird nicht mehr
    // aufgenommen" genau dieser Zustand ist.
    // ZURUECKGESETZT MIT DEM KNOPF, nicht mitgeschleppt: ein Spitzenwert aus
    // der letzten Aufnahme druecke die naechste flach, bis er abgeklungen ist.
    if(e.state==="rec"){ this.vpeak=0.02; }
    if(e.state!=="rec"){ $("#voice").hidden=true; }
    b.disabled=!!e.blocked;
    b.title = e.blocked ? e.blocked
            : e.state==="rec" ? "stop and write it down"
            : "dictate";
    // INTO THE BOX, NEVER STRAIGHT OUT. Whisper is wrong often enough that a
    // dictation which submitted itself would be a message nobody could take
    // back.
    if(e.text){ this.attach(e.text); }
    if(e.note){ this.note(e.note); }
  },

  // ONE PLACE THAT WRITES INTO THE BOX, and three callers use it: a finished
  // dictation, a dropped file, a pasted picture. Appended rather than assigned,
  // so half a typed line survives whatever arrives next.
  attach(text){
    const had=input.value.replace(/\s*$/,"");
    input.value = had ? had+" "+text : text;
    // The autogrow listener owns the height; firing its event is cheaper than a
    // second copy of the same three lines that could drift from it.
    input.dispatchEvent(new Event("input"));
    input.focus();
    input.selectionStart=input.selectionEnd=input.value.length;
  },

  // THE PAGE NEVER SEES THE PATH. A browser hands a drop over as a File with a
  // name and bytes and no location on disk; pywebview puts the real one on the
  // Python side as `pywebviewFullPath`. So the page's whole job here is to stop
  // WebView2 from navigating to the file and to say that a drop is happening --
  // the paths come back through `on(...)` a moment later.
  dragging(on){ box.classList.toggle("drag", !!on); },

  dropped(paths){
    this.dragging(false);
    if(!paths || !paths.length){ return; }
    // #142. AN IMAGE IS AN ATTACHMENT, NOT A PATH IN THE COMPOSER. The path
    // goes straight back to the Python side (which had it first -- see the
    // drop comment above) and a chip comes back; the model gets the pixels,
    // not a filename `read_file` cannot open as words. Everything else keeps
    // the old behaviour: the path lands in the input for the model to read.
    const img=/\.(png|jpe?g|gif|webp|bmp)$/i;
    // The strip redraws through the "chips" event the Python side pushes --
    // the same channel /image uses, so there is exactly one renderer call.
    paths.filter(p=>img.test(p)).forEach(p=>pywebview.api.stage_image(p));
    paths=paths.filter(p=>!img.test(p));
    if(!paths.length){ return; }
    // QUOTED WHEN IT HAS TO BE. A Windows path with a space in it is the normal
    // case, not the exception, and an unquoted one is two arguments to whatever
    // reads the line next.
    this.attach(paths.map(p => /\s/.test(p) ? '"'+p+'"' : p).join(" "));
  },

  // #142. The chip strip is REDRAWN WHOLE from what the Python side returns --
  // both stage calls hand the full list back, so there is no index to drift
  // when a middle chip is removed.
  stageRender(chips){
    const s=$("#stage"); if(!s){ return; }
    s.innerHTML="";
    (chips||[]).forEach((c,i)=>{
      const w=document.createElement("span"); w.className="imgchip";
      const im=document.createElement("img"); im.src=c.url; im.title=c.name;
      const x=document.createElement("button"); x.textContent="×";
      x.title="remove "+c.name;
      x.onclick=()=>pywebview.api.unstage_image(i);
      w.appendChild(im); w.appendChild(x); s.appendChild(w);
    });
    s.hidden=!(chips&&chips.length);
  },

  stagedUrls(){
    return [...document.querySelectorAll("#stage img")].map(i=>i.src);
  },

  idle(){ this.running=false; go.textContent="↑"; go.classList.remove("stop");
    $("#turnstate").textContent=""; $("#hint").textContent="";
    document.querySelectorAll(".cursor").forEach(c=>c.remove());
    this.cursor=null; },

  busy(){ this.running=true; go.textContent="■ Stop"; go.classList.add("stop");
    $("#turnstate").textContent="…";
    $("#hint").textContent="read timeout __TIMEOUT__ s"; },

  // #138c. Die Zeile ist angenommen und wartet -- auf den Memory-Nachlauf, der
  // nach `idle` noch auf demselben Thread laeuft.
  //
  // GESAGT WIRD ES, WEIL SONST NICHTS ES SAGT. Ohne diese Zeile stuende der
  // Composer auf `Stop` fuer etwas, das der Leser nie angestossen hat, und
  // seine Frage laege sichtbar im Verlauf, ohne dass sich etwas bewegt --
  // dieselbe Lage wie der Fehler, nur mit gesperrtem Knopf.
  queuedLine(){ this.running=true; go.textContent="■ Stop";
    go.classList.add("stop");
    $("#turnstate").textContent="…";
    $("#hint").textContent="queued -- the memory review is finishing"; },

  // THE LOCK IS SYNCHRONOUS, THE PAINT IS NOT, and that split is what the
  // bridge is for. Every pywebview.api.* call resolves a promise once the
  // Python side returns, and only that side knows whether a turn started or the
  // line was a slash command answered on the spot. This used to paint "Stop"
  // on the way in unconditionally -- so /reset left the window sitting on Stop
  // with nothing behind it, which is what robin found.
  //
  // `running` is still set here rather than in the callback: it is what keeps a
  // second click out during the round trip, and that window is real even when
  // it is short. The BUTTON only becomes Stop if there is something to stop.
  // A rejected call unlocks too -- an api that threw leaves no turn running.
  // #143 E3, SECOND HALF. send() answers slash lines ahead of the busy buffer
  // -- but go()'s gate turned EVERY submit during a turn into a stop, so the
  // line never got there: robin typed /delegate mid-turn on 2026-08-28 and
  // the RUNNING TURN died. Its own method rather than a branch in go(), so
  // the ordinary path keeps its shape (the install-tile order is pinned on
  // go()'s first occurrences). The composer stays on Stop either way: the
  // LOCAL turn is what the button is about, and it is still running behind
  // the slash answer.
  fanout(text){
    input.value=""; input.style.height="auto";
    this.user(text);
    pywebview.api.send(text).then(()=>this.busy(), ()=>this.busy()); },

  // Only the delegation pair passes the gate -- a /reset or /model through it
  // would yank state under a running pump -- and the stop gesture (button,
  // plain line, Escape) stays exactly what it was.
  go(){ const text=input.value.trim();
    if(this.running && /^\/(delegate|subtasks)\b/i.test(text)){ this.fanout(text); return; }
    if(this.running){ pywebview.api.stop(); return; }
    if(!text) return;
    input.value=""; input.style.height="auto";
    this.user(text);
    // #142. The staged images travel with THIS line: drawn into it here,
    // consumed by the Python side in the same send. The strip empties either
    // way -- accepted or refused, what was drawn is what was spent.
    this.userImages(this.stagedUrls());
    this.stageRender([]);
    // #130. AFTER the typed line, before the call: the tile belongs under the
    // command it is about, and the answer to `/mcp add` waits for it.
    if(/^\/mcp\s+add\s+\S/i.test(text)) this.installBar();
    this.running=true;
    pywebview.api.send(text).then(
      started => started ? this.busy() : this.idle(),
      () => this.idle()); },

  // #88: THE RELEASE LEVEL, and the menu is built from what the CORE says the
  // levels are -- never from a list written out here. A second copy of the
  // three names in the page is a second place to forget one.
  modeMenu(){ const m=$("#modemenu");
    if(!m.hidden){ m.hidden=true; return; }
    m.innerHTML = (this.modes||[]).map(x =>
      '<button data-mode="'+x.name+'" onclick="crow.setMode(\''+x.name+'\')">'
      + (x.name===this.mode ? '<span class="tick">&#10003;</span>' : '')
      + '<b>'+x.name+'</b><span class="what">'+x.what+'</span></button>').join("");
    m.hidden=false; },

  setMode(name){ $("#modemenu").hidden=true; pywebview.api.set_mode(name); },

  // #128. THE HELD-BACK WRITES, as a tile behind the composer.
  //
  // THE ENTRIES ARE DRAWN, NEVER INTERPOLATED. A staged note is model-written
  // text out of a conversation and may contain anything at all; the page shows
  // it rather than running it. Same rule the root menu keeps for directory
  // names that could be called `<img onerror=...>`.
  //
  // OPEN SURVIVES A REDRAW. The review can stage a second time while the tile
  // is already open, and collapsing it under the reader's hands would hide the
  // thing they were in the middle of reading.
  pendState(items){
    const bar = $("#pendbar"), list = items || [];
    if(!list.length){ bar.hidden = true; bar.classList.remove("open");
                      bar.innerHTML = ""; return; }
    // `replace` is a line gained AND a line lost -- it is one entry and two
    // changes, and a count that showed it as one would understate what is
    // about to happen to the file.
    let plus = 0, minus = 0;
    list.forEach(x => { const a = x.action || "add";
      if(a === "remove"){ minus++; }
      else if(a === "replace"){ plus++; minus++; }
      else { plus++; } });
    bar.innerHTML =
      '<span class="top"><span class="title">Memory Consolidation</span>'
      + '<span class="plus"></span><span class="minus"></span>'
      + '<span class="hint"></span></span>'
      + '<span class="body">'
      + list.map(()=>'<span class="what"></span>').join("")
      + '<span class="acts">'
      + '<button class="yes" onclick="crow.pendAnswer(true)">save to memory</button>'
      + '<button class="no" onclick="crow.pendAnswer(false)">discard</button>'
      + '</span></span>';
    bar.querySelector(".plus").textContent = "+" + plus;
    bar.querySelector(".minus").textContent = "\u2212" + minus;
    bar.querySelector(".hint").textContent =
      bar.classList.contains("open") ? "click to collapse" : "click to review";
    const rows = bar.querySelectorAll(".what");
    list.forEach((x,i)=>{ if(rows[i]) rows[i].textContent = x.text || ""; });
    bar.hidden = false; },

  // The buttons live inside the tile, so a click on one would bubble up and
  // toggle it shut on the way out.
  pendToggle(e){ if(e && e.target.closest("button")) return;
    const bar = $("#pendbar");
    bar.classList.toggle("open");
    const hint = bar.querySelector(".hint");
    if(hint) hint.textContent =
      bar.classList.contains("open") ? "click to collapse" : "click to review"; },

  pendAnswer(yes){ pywebview.api.answer_memory(!!yes); },

  // #92: THE WORKING DIRECTORY. The button shows the folder's NAME and carries
  // the full path as its tooltip -- a rail-width button cannot hold
  // C:\Users\...\project and a truncated path is a path nobody can check.
  //
  // "none" IS DRAWN, not left blank. An empty button reads as "no boundary
  // needed"; the state that has to be legible is exactly the one where writes
  // are unbounded.
  rootIs(path, name, roots){
    this.root = path || ""; this.roots = roots || [];
    const b = $("#root");
    b.textContent = name || "no folder";
    b.title = path || "writes are not restricted -- pick a folder to bound them";
    b.dataset.bound = path ? "1" : "0"; },

  rootMenu(){ const m=$("#rootmenu");
    if(!m.hidden){ m.hidden=true; return; }
    const rows = (this.roots||[]).map(x =>
      '<button class="rootrow" onclick="crow.chooseRoot(this.dataset.p)">'
      + (x.path===this.root ? '<span class="tick">&#10003;</span>' : '')
      + '<b></b><span class="what"></span></button>');
    m.innerHTML = '<div class="head">recently used</div>'
      + (rows.length ? rows.join("") : '<div class="what none">none yet</div>')
      + '<div class="sep"></div>'
      + '<button onclick="crow.pickRoot()"><b>open folder&#8230;</b></button>'
      + (this.root ? '<button onclick="crow.clearRoot()"><b>no folder</b>'
                     + '<span class="what">writes go anywhere again</span></button>' : "");
    // TEXT AND dataset, NEVER AN HTML STRING, for anything that came off the
    // disk: a directory may be named `<img onerror=...>` or hold a quote, and
    // the page has to draw it rather than run it. Nothing here is interpolated.
    const els = m.querySelectorAll("button.rootrow");
    (this.roots||[]).forEach((x,i) => { const el = els[i];
      if(!el) return;
      el.dataset.p = x.path;
      el.querySelector("b").textContent = x.name;
      el.querySelector(".what").textContent = x.path; });
    m.hidden=false; },

  chooseRoot(p){ $("#rootmenu").hidden=true; pywebview.api.choose_root(p); },

  // #115. THE SAME SHAPE AS rootMenu, AND FOR THE SAME REASON: a model key and
  // a model name are text that came off the disk -- out of the manifest, in
  // this case -- so they are set with textContent and dataset and never
  // interpolated into an HTML string. A model file named `<img onerror=...>`
  // has to be DRAWN, not run.
  // #119. ONE MENU, TWO KINDS OF ROW. The thinking level had its own chip and its own panel
  // beside this one; it is a setting INSIDE a model, not a second subject, so it is drawn as
  // rows indented under the model it belongs to.
  //
  // THE PLAN IS BUILT BEFORE ANY HTML IS, and that is not tidiness: the rows carry names off the
  // disk, so the skeleton goes in as HTML and every name goes in by textContent afterwards. Two
  // row shapes means two skeletons, and the pairing of plan to element has to survive that --
  // hence one flat list with a `kind` on each entry rather than nested loops over two arrays.
  // NAMED FOR ITS MENU, not for the idea of a plan. It was `menuPlan` and so was
  // the context menu's builder below -- one object literal, so the later
  // definition silently replaced this one and the chip called the wrong
  // planner with no arguments. Nothing threw until somebody clicked.
  modelPlan(){ const out = [];
    (this.models||[]).forEach(x => {
      const running = (x[0] === this.modelKey);
      out.push({kind:"model", k:x[0], name:x[1],
                what: running ? "running" : "restarts the server"});
      // ONLY UNDER THE RUNNING ONE. `levels` and `groups` describe the model that answered the
      // probe; hanging them under the other row would name steps nobody has measured there.
      if(!running || !(this.levels||[]).length) return;
      const now = this.reasoning || "off";
      this.reasonGroups().forEach(g => {
        const bits = [];
        if(g.indexOf("off") >= 0) bits.push("default");
        out.push({kind:"level", k:this.reasonName(g), name:this.reasonName(g),
                  bits: bits, tick: (g.indexOf(now) >= 0)}); }); });
    return out; },

  modelMenu(){ const m=$("#modelmenu");
    if(!m.hidden){ m.hidden=true; return; }
    const plan = this.modelPlan();
    const rows = plan.map(p => p.kind === "model"
      ? '<button class="modelrow" onclick="crow.chooseModel(this.dataset.k)">'
        + '<b></b><span class="what"></span></button>'
      : '<button class="lvlrow" onclick="crow.chooseReason(this.dataset.k)">'
        + '<span class="tick"></span><b></b><span class="what"></span></button>');
    m.innerHTML = '<div class="head">model</div>'
      + (rows.length ? rows.join("") : '<div class="what none">none in the manifest</div>');
    const els = m.querySelectorAll("button");
    // A model entry is [key, label]: the KEY goes into dataset and comes back on the click, the
    // LABEL is what the row reads as -- `operating-point` is the table's word for the row,
    // `DeepSeek-V4-Flash-0731` is the model. A level entry carries its own name for both. All of
    // them are set by textContent and never interpolated; they came off the disk.
    plan.forEach((p,i) => { const el = els[i];
      if(!el) return;
      el.dataset.k = p.k;
      const name = p.name;
      el.querySelector("b").textContent = name;
      if(p.kind === "model"){ el.querySelector(".what").textContent = p.what; return; }
      // The escape rather than the character: the rows above write &#10003; into their HTML, and
      // this one sets textContent, so the escape keeps the source ASCII either way.
      el.querySelector(".tick").textContent = p.tick ? "✓" : "";
      // ONLY `default`, AND THE SWALLOWED NAMES ARE NOT LISTED. The row used to read
      // "default - high renders the same"; robin cut it against the built window on 0731, where
      // three names collapse into one row: naming a step the menu does not offer is the defect
      // #117 is about, and it does not stop being one because the sentence explains itself.
      const bits = p.bits;
      el.querySelector(".what").textContent = bits.join(" · "); });
    m.hidden=false; },

  chooseModel(k){ $("#modelmenu").hidden=true; pywebview.api.choose_model(k); },

  // #117. ONE ROW PER RENDERING, not one per name. The manifest carries which levels produce the
  // SAME prompt, measured through /apply-template, and the window draws the groups rather than
  // the names: on Qwen `off` and `high` are one row, on 0731 `off`, `low` and `high` are.
  //
  // UNMEASURED COLLAPSES NOTHING. A model whose entry has no reasoning_groups gets every level as
  // its own row and `off` as its own row -- merging two steps on no evidence would be inventing
  // the very fact this field exists to record, and #116's rule holds here too: no manifest entry,
  // nothing made up.
  reasonGroups(){ const g = this.groups||[];
    return g.length ? g : [["off"]].concat((this.levels||[]).map(l=>[l])); },

  // WHAT A GROUP IS CALLED: its first member that is not `off`. `off` never names a row. It is
  // the absence of a setting, and every template has a default for the absent key -- so a row
  // called `off` beside the step it IS would be the defect #117 was cut for: the chip read
  // `reasoning off` while Qwen reasoned at xhigh, the dearest setting on that model.
  reasonName(g){ for(const n of g){ if(n!=="off") return n; } return "off"; },

  // THE CHIP NAMES THE ROW, ALWAYS -- not the level the chat happens to have stored. A level
  // bound under another name still runs whatever its group renders as: on 0731 a chat set to
  // `high` runs the `low` row, and a chip reading `high` while the tick sits on `low` names a
  // step the menu does not offer. Measured there, seen on screen, cut on sight (#117).
  //
  // EMPTY WHEN THE MODEL DECLARES NO LEVELS, rather than a name with nothing behind it: #116's
  // second negative proof is that no manifest entry means no invented levels.
  levelLabel(){ if(!(this.levels||[]).length) return "";
    const now = this.reasoning || "off";
    const g = this.reasonGroups().filter(x => x.indexOf(now) >= 0)[0];
    if(!g) return " · " + now;
    return " · " + this.reasonName(g)
           + ((g.indexOf("off") >= 0) ? " (default)" : ""); },

  // #119. ONE CHIP FOR BOTH, and `(default)` survives the merge on purpose: it is the whole
  // finding of #117. `high` means somebody chose it; `high (default)` means nothing was chosen
  // and the template lands there anyway. Dropping the word to save six characters would put the
  // chip back to naming a setting instead of naming what the model DOES.
  showModel(){ const c = $("#model");
    if(!this.modelName){ c.hidden = true; return; }
    c.hidden = false;
    c.innerHTML = '<b></b><span class="lvl"></span>';
    c.querySelector("b").textContent = this.modelName;
    c.querySelector(".lvl").textContent = this.levelLabel(); },

  // KEPT AS A NAME the two `case` arms already call, so the merge did not have to touch the
  // seam between the Python side and the page. It sets the level and redraws the one chip.
  showReason(level){ this.reasoning = level || ""; this.showModel(); },

  // #119: `reasonMenu` IS GONE, not renamed. Its rows moved into modelMenu above, keeping the
  // rule the panel existed to hold: a click chooses AND applies, and every name is drawn with
  // textContent because it came off the disk. What is left is the door it knocked on, and that
  // door is unchanged -- `set_reasoning` is still the command `/reasoning` uses, which is the
  // #99 precedent for why a control is never wired separately from the command it duplicates.
  chooseReason(name){ $("#modelmenu").hidden=true; pywebview.api.set_reasoning(name); },
  pickRoot(){ $("#rootmenu").hidden=true; pywebview.api.pick_root(); },
  clearRoot(){ $("#rootmenu").hidden=true; pywebview.api.clear_root(); },

  // #88 point 2: THE PROMPT SHOWS WHAT IT RELEASES. A card that only said
  // "run_command?" would be a keystroke, not a decision, so the arguments are
  // drawn as the model sent them. Rendered as text, never as HTML -- the
  // arguments come from the model and a path with a tag in it must not become
  // one. It lands in the flow rather than over it: the turn it belongs to is
  // above it, and a modal would hide the very context the answer needs.
  ask(name, args, scope){
    const d=document.createElement("div");
    d.className="turn ask";
    d.innerHTML='<div class="askcard"><div class="asktop"><b></b><code></code></div>'
      + '<div class="askrow">'
      + '<button class="yes" onclick="crow.answered(this,\'yes\')">run it</button>'
      + '<button class="no" onclick="crow.answered(this,\'no\')">decline</button>'
      + (scope ? '<button class="always" onclick="crow.answered(this,\'always\')">'
                 + 'always for <em></em></button>' : '')
      + '</div></div>';
    d.querySelector("b").textContent=name;
    d.querySelector("code").textContent=args||"";
    if(scope) d.querySelector(".always em").textContent=scope;
    flow.appendChild(d); this.bottom(); },

  // #135. A SERVER ASKING FOR INPUT, drawn by Crow and never by the server.
  //
  // WHAT ARRIVES IS A SCHEMA, NOT A RENDERING: a list of fields with types, and
  // every label goes in by textContent. There is no markup, no link and no
  // button here that came off the wire -- which is the whole reason this is
  // allowed at all. The card lands IN THE FLOW like the approval card, because
  // it belongs to the turn that caused it and the answer belongs in the record.
  elicit(ask){
    if(!ask) return;
    const d=document.createElement("div");
    d.className="turn ask";
    d.innerHTML='<div class="askcard" data-elicit=""><div class="asktop">'
      + '<b></b><code></code></div><span class="elicwhat"></span>'
      + '<div class="elicfields"></div>'
      + '<div class="askrow">'
      + '<button class="yes" onclick="crow.elicitAnswer(this,\'accept\')">send</button>'
      + '<button class="no" onclick="crow.elicitAnswer(this,\'decline\')">decline</button>'
      + '<button class="no" onclick="crow.elicitAnswer(this,\'cancel\')">dismiss</button>'
      + '</div></div>';
    const card=d.querySelector(".askcard");
    card.dataset.elicit=String(ask.id);
    card.querySelector("b").textContent=ask.server||"a server";
    card.querySelector("code").textContent="is asking";
    card.querySelector(".elicwhat").textContent=ask.message||"";
    const box=card.querySelector(".elicfields");
    (ask.fields||[]).forEach(f => {
      const row=document.createElement("div"); row.className="elicfield";
      const label=document.createElement("label"); label.className="eliclabel";
      label.textContent=f.title||f.name;
      if(f.required){ const star=document.createElement("em");
                      star.textContent=" *"; label.appendChild(star); }
      let input;
      if(f.enum){ input=document.createElement("select");
        (f.enum||[]).forEach(c => { const o=document.createElement("option");
          o.value=c; o.textContent=c; input.appendChild(o); }); }
      else if(f.type==="boolean"){ input=document.createElement("input");
        input.type="checkbox"; }
      else { input=document.createElement("input");
        input.type=(f.type==="number"||f.type==="integer")?"number":"text"; }
      input.dataset.field=f.name;
      input.dataset.kind=f.type;
      row.appendChild(label); row.appendChild(input); box.appendChild(row);
      if(f.description){ const hint=document.createElement("div");
        hint.className="elichint"; hint.textContent=f.description;
        box.appendChild(hint); } });
    flow.appendChild(d); this.bottom(); },

  // `decline` and `dismiss` are two answers and not one. The specification
  // separates them -- a refusal is a decision, a dismissal is not -- and a
  // server is entitled to treat them differently.
  elicitAnswer(btn, action){
    const card=btn.closest(".askcard");
    const values={};
    card.querySelectorAll("[data-field]").forEach(el => {
      values[el.dataset.field] =
        el.type==="checkbox" ? el.checked : el.value; });
    pywebview.api.answer_elicit(Number(card.dataset.elicit), action, values)
      .then(said => {
        if(said){ let bad=card.querySelector(".askdone");
          if(!bad){ bad=document.createElement("span"); bad.className="askdone";
                    card.querySelector(".askrow").appendChild(bad); }
          bad.textContent=said; return; }
        card.querySelector(".elicfields").querySelectorAll("[data-field]")
          .forEach(el => { el.disabled=true; });
        card.querySelector(".askrow").innerHTML=
          '<span class="askdone"></span>';
        card.querySelector(".askdone").textContent=
          action==="accept" ? "sent" : action==="decline" ? "declined"
                                                          : "dismissed"; }); },

  // The card stays, with the answer on it: a question that vanishes leaves no
  // record of what was released, and the transcript is where that belongs.
  answered(btn, what){
    const card=btn.closest(".askcard");
    card.querySelector(".askrow").innerHTML=
      '<span class="askdone">'
      + (what==="no" ? "declined" : what==="always" ? "allowed, and from now on"
                                                    : "allowed")
      + '</span>';
    pywebview.api.answer(what); },

  // Called back by the core's answer, never set optimistically: the button
  // shows what the client IS running, not what was clicked.
  modeIs(name, modes){ this.mode=name; if(modes) this.modes=modes;
    $("#mode").dataset.mode=name; $("#modename").textContent=name; },

  // "neu" ARCHIVES, it does not discard. The old conversation is written to its
  // own file and appears in the rail; clicking it loads it back. Without that a
  // click on "neu" is an unlabelled delete button.
  // NEITHER CLEARS THE FLOW HERE. The page cannot know whether Python will
  // refill it: `open` bails out when the chat is already the open one, and the
  // clear had already happened -- so clicking the chat you are reading emptied
  // the window. The side that knows whether it will replay is the side that
  // clears, and it does, on the queue, in order.
  reset(){ if(this.running) return; pywebview.api.reset(); },
  open(path){ if(this.running) return; pywebview.api.open(path); },

  // RIGHT-CLICK ON A CHAT. Three things a list of saved conversations has to
  // offer, and none of them is reachable from a left click: rename it, put it
  // out of the way, throw it out.
  // #119. WHAT THE MENU OFFERS DEPENDS ON WHAT WAS CLICKED, so it is a plan
  // rather than a fixed set of buttons. Rows carry an ACTION NAME and never a
  // snippet of code: `onclick` is wired from this table, so a project called
  // `'); doSomething('` is a label and cannot become one.
  railPlan(kind,entry,archived){
    if(kind==="rail")
      return [{act:"newchat", label:"new chat"},
              {act:"newproj", label:"new project"}];
    if(kind==="project")
      return [{act:"dropproj", label:"remove project", arg:entry.path,
               note:"chats and folder stay"}];
    const rows=[{act:"rename", label:"rename"}];
    // MOVING AN UNSAVED CHAT IS OFFERED TOO: it binds the live boundary, which
    // is `choose_root`'s job and works without a file. What it must not do is
    // list a project the chat is already in -- a row that changes nothing reads
    // as a row that failed.
    const here=this.projectOf(entry.root);
    const others=(this.projects||[]).filter(p=>!this.sameDir(p.path,entry.root));
    if(others.length) rows.push({sep:true, head:"to project"});
    others.forEach(p=>rows.push({act:"toproj", label:p.name, arg:p.path,
                                 indent:true}));
    if(here) rows.push({act:"toproj", label:"out of project", arg:"",
                        sep:!others.length});
    rows.push({sep:true});
    rows.push({act:"arch", label:archived ? "restore" : "archive"});
    rows.push({act:"del", label:"delete", danger:true});
    return rows; },

  menuDo(act,arg){
    const entry=this.target;
    // DELETE OWNS THE PANEL, every other row is done with it. Two clicks for a
    // delete is not a flourish -- there is no undo behind it -- and the second
    // click has to land on a button that says what it does. Shutting the menu
    // first would turn that into two right-clicks, which is a different gesture
    // and one nobody was taught.
    if(act==="del") return this.deleteTarget(entry);
    this.closeMenu();
    if(act==="newchat") return this.reset();
    if(act==="newproj") return pywebview.api.create_project();
    if(act==="dropproj") return pywebview.api.drop_project(arg);
    if(act==="rename") return this.renameTarget(entry);
    if(act==="arch") return this.archiveTarget(entry);
    if(act!=="toproj") return;
    // AN UNSAVED CHAT HAS NO FILE TO WRITE, so it goes through the door that
    // binds the LIVE boundary. Python decides which of the two a path is; this
    // only picks the door, and `set_chat_root` checks again on the other side.
    if(!entry || !entry.path){
      if(arg) return pywebview.api.choose_root(arg);
      return pywebview.api.clear_root(); }
    return pywebview.api.set_chat_root(entry.path,arg); },

  menu(e,kind,entry,row,archived){
    e.preventDefault(); this.target=entry; this.targetRow=row;
    const m=$("#menu");
    const plan=this.railPlan(kind,entry,archived);
    m.innerHTML=plan.map(p=>
      (p.sep ? '<div class="sep"></div>' : "")
      + (p.head ? '<div class="mhead"></div>' : "")
      + (p.act ? '<button class="'+(p.danger ? "danger" : "")
                 + (p.indent ? " indent" : "")+'"><b></b>'
                 + (p.note ? '<span class="what"></span>' : "")+'</button>' : "")
    ).join("");
    // NAMES IN BY textContent, HANDLERS FROM THE PLAN. A project name is a
    // folder name off the disk, which is the modelMenu rule verbatim.
    const heads=m.querySelectorAll(".mhead"), btns=m.querySelectorAll("button");
    let hi=0, bi=0;
    plan.forEach(p=>{
      if(p.head) heads[hi++].textContent=p.head;
      if(!p.act) return;
      const el=btns[bi++];
      el.querySelector("b").textContent=p.label;
      if(p.note) el.querySelector(".what").textContent=p.note;
      el.onclick=()=>crow.menuDo(p.act,p.arg); });
    m.classList.add("on");
    // Kept inside the window: a menu opened near the bottom edge would
    // otherwise hang off it with its last item unreachable.
    const w=m.offsetWidth||170, h=m.offsetHeight||110;
    m.style.left=Math.min(e.clientX,innerWidth-w-6)+"px";
    m.style.top=Math.min(e.clientY,innerHeight-h-6)+"px";
  },
  closeMenu(){ $("#menu").classList.remove("on"); },

  renameTarget(entry){
    const row=this.targetRow;
    if(!row||!entry) return;
    const label=row.querySelector(".t"), was=label.textContent;
    const field=document.createElement("input");
    field.value=was; label.replaceWith(field); field.focus(); field.select();
    const done=keep=>{
      const name=field.value.trim();
      const back=document.createElement("span");
      back.className="t"; back.textContent=(keep&&name)?name:was;
      field.replaceWith(back);
      if(keep&&name&&name!==was) pywebview.api.rename(entry.path,name);
    };
    field.onkeydown=ev=>{ if(ev.key==="Enter"){ev.preventDefault();done(true);}
                          if(ev.key==="Escape"){ev.preventDefault();done(false);} };
    field.onblur=()=>done(true);
  },

  archiveTarget(entry){
    if(entry && entry.path) pywebview.api.archive_chat(entry.path); },

  // TWO CLICKS FOR A DELETE, because there is no undo behind it. The second
  // click is on a button that says what it does, not on a generic "yes".
  // TWO CLICKS FOR A DELETE, because there is no undo behind it. The second
  // click is on a button that says what it does, not on a generic "yes". The
  // label sits in a <b> now that the rows are built from a plan; the arming
  // itself is unchanged.
  deleteTarget(entry){
    const btn=$("#menu").querySelector("button.danger");
    if(!btn) return;
    const label=btn.querySelector("b");
    if(btn.dataset.armed==="1"){ btn.dataset.armed=""; label.textContent="delete";
      this.closeMenu();
      if(!entry) return;
      // NO FILE, NO delete_chat. It has nothing to remove and used to return
      // silently -- the row robin could not get rid of. `discard_live` is the
      // door for a conversation that was never written, and it refuses one
      // that was.
      if(entry.path) pywebview.api.delete_chat(entry.path);
      else pywebview.api.discard_live();
      return; }
    btn.dataset.armed="1";
    // A CHAT WITH NO FILE SAYS SO, because the two are not the same act: one
    // removes a file, the other throws away something that was never written.
    label.textContent=(entry && entry.path) ? "really delete?" : "really discard?";
    setTimeout(()=>{ if(!btn.isConnected) return;
      btn.dataset.armed=""; label.textContent="delete"; },4000);
  },

  ctx(tokens,limit){
    const el=$("#ctx"); if(tokens<=0){ el.innerHTML=""; return; }
    const size = tokens<1000 ? String(tokens) : (tokens/1000).toFixed(1)+"k";
    if(limit<=0){ el.innerHTML='<span class="n">'+size+'</span>'; return; }
    // THE NUMBER, NOT A BAR OF HASHES. Ten cells could only ever say the share
    // to within 10%, and the figure that answers "how much room is left" was
    // already being printed beside them. The colour is the only thing the bar
    // carried that the number did not, so it moved onto the number.
    const share=Math.min(1,tokens/limit);
    const cls = share<0.5 ? "fill" : (share<0.85 ? "fill w" : "fill b");
    el.innerHTML='<span class="'+cls+'">'+size+'</span>'+
      '<span class="rest"> / '+(limit/1000).toFixed(0)+'k</span>';
  },

  // A CHAT KEEPS ITS PLACE WHEN YOU OPEN IT. The open one was drawn in a slot of
  // its own AND filtered out of the list, so a click moved it. Now the list holds
  // every chat with a file and the open one is marked `on` where it sits; the top
  // slot is only for a chat with no file yet, which is what `unsaved` says.
  // #119. WHICH PROJECT A CHAT IS IN IS NOT STORED ANYWHERE -- it is its working
  // directory, compared against the project list. A `crow_project` key beside
  // `crow_root` would be a second place for one fact, and the two would part
  // company the first time either was written alone.
  //
  // EXACT, NOT AN ANCESTOR WALK. `find_root` takes the NEAREST marker and not the
  // highest on purpose, so a sub-directory that declares itself is its own root;
  // folding it into the project above would contradict the rule the boundary is
  // built on. The core says the same in `is_project`.
  sameDir(a,b){ if(!a||!b) return false;
    return a.replace(/[\\\/]+$/,"").toLowerCase()
        === b.replace(/[\\\/]+$/,"").toLowerCase(); },

  projectOf(root){ if(!root) return null;
    return (this.projects||[]).filter(p=>this.sameDir(p.path,root))[0] || null; },

  // ONE ROW, DRAWN THE SAME WAY WHEREVER IT SITS. A chat under a project and a
  // chat below them are the same thing to a reader and to a click, so they are
  // the same builder -- the indent is a class, not a second implementation.
  // ---- #143 delegation. ONE snapshot feeds three drawings: the cards in the
  // flow, the child rows in the rail, the chip over the composer. Everything
  // is drawn from `items` and nothing keeps its own idea of subtask state --
  // the registry in the core speaks once, through `subtask_view`.
  subs(items){
    this.subItems=items||[];
    this.subItems.forEach(it=>this.subCard(it));
    this.subChip(this.subItems);
    this.subMenuDraw(this.subItems);
    this.subRail();
    // A jump that had to open the parent chat first lands here, one snapshot
    // later, when the replayed cards exist again.
    if(this.subPending){
      const d=flow.querySelector('.subcard[data-sub="'
        +CSS.escape(this.subPending)+'"]');
      if(d){ d.scrollIntoView({behavior:"smooth",block:"center"});
        this.subPending=null; }
    }
  },

  // The status words, written once for the card and the menu. TOKEN COUNTS
  // ONLY -- no money figure anywhere on a subtask, robin's call 2026-08-27.
  subStat(it){
    const tok=(it.tok||0).toLocaleString("en-US")+" tok";
    if(it.st==="running") return "running · "+Math.round(it.s)+" s · "+tok;
    if(it.st==="done")    return "✓ done · "+tok;
    return "✗ "+it.st+" · "+Math.round(it.s)+" s";
  },

  // A card exists FROM THE MOMENT delegate runs -- the running state is the
  // one that must be seen -- and is then updated in place, never rebuilt, so
  // an open result fold survives every tick.
  subCard(it){
    let d=flow.querySelector('.subcard[data-sub="'+CSS.escape(it.i)+'"]');
    if(!d){
      // ONLY THE OPEN CHAT'S OWN SUBTASKS GET A CARD. Python computes `here`
      // from parent and open chat in the same breath -- the page comparing
      // its own cached copy was the frame that drew no card at all.
      if(!it.here) return;
      d=document.createElement("div"); d.className="subcard"; d.dataset.sub=it.i;
      // robins letzte Kartenform 2026-08-29: die klassische Kopfzeile --
      // ⑂ delegate · dN, Modell, Status rechts -- und der Task darunter.
      // Der volle Output bleibt ZU; die Karte selbst ist die Klickflaeche.
      d.innerHTML='<div class="shead"><span class="glyph">⑂</span>'
        +'<span class="dlabel"></span><span class="sname"></span>'
        +'<span class="sstat"></span></div><div class="stask"></div>';
      d.querySelector(".dlabel").textContent="delegate · "+it.i;
      d.querySelector(".sname").textContent=it.model||"";
      d.querySelector(".stask").textContent=it.task||"";
      // INTO THE FLOW, NEVER INTO A ROUND'S COLUMN. A card appended to
      // `this.col` folded away with its round the moment the next one began
      // -- robin, 2026-08-27: "die sollen bleiben". `fold()` only ever moves
      // the round element it tracks, so an own `.turn` wrapper is safe from
      // it -- and it is WHAT ALIGNS THE CARD TO THE CHAT: same centred
      // column, same padding as every other block ("jetzt nur noch an den
      // chat ausrichten").
      const wrap=document.createElement("div"); wrap.className="turn subrow";
      wrap.appendChild(d); flow.appendChild(wrap); this.bottom();
    }
    // robins letzte Fassung 2026-08-28: die LAUFENDE Karte atmet als Zeile;
    // fertig steht sie still. Die Klasse traegt den Zustand, das CSS den Atem.
    d.classList.toggle("run", it.st==="running");
    const stat=d.querySelector(".sstat");
    if(it.st==="running"){
      stat.innerHTML='<span class="sdot run"></span><span></span>';
      stat.lastChild.textContent=this.subStat(it);
    } else if(it.st==="done"){
      stat.innerHTML='<span class="okword"></span>';
      stat.firstChild.textContent=this.subStat(it);
    } else {
      stat.innerHTML='<span class="badword"></span>';
      stat.firstChild.textContent=this.subStat(it);
    }
    if(it.st!=="running" && it.res && !d.querySelector(".sresult")){
      // ZU PER DEFAULT (robin, 2026-08-28): der Hauptchat zeigt die Karte,
      // der volle Output wohnt hinter dem Klick auf sie. Fuer den ganzen
      // Text gibt es KEINEN Subtask-Chat; dieser eine Ort ist es.
      const res=document.createElement("div"); res.className="sresult";
      res.hidden=true; res.textContent=it.res;
      d.appendChild(res);
      d.classList.add("can"); d.title="click for the result";
      d.onclick=()=>{ res.hidden=!res.hidden; };
    }
  },

  subChip(items){
    const wrap=$("#subwrap"), chip=$("#subchip");
    if(!wrap||!chip) return;
    if(!items.length){ wrap.hidden=true; return; }
    wrap.hidden=false;
    // THE NUMBER IS THE ACTIVE COUNT, NOT THE TOTAL -- robin, 2026-08-27:
    // with nothing running the chip reads 0 and rests in the dim frame; the
    // bright border and the pulsing dot belong to a live fan-out alone.
    const running=items.filter(x=>x.st==="running").length;
    chip.classList.toggle("live", running>0);
    chip.innerHTML="";
    if(running){ const dot=document.createElement("span");
      dot.className="sdot run"; chip.appendChild(dot); }
    chip.appendChild(document.createTextNode("⑂ "+running));
  },

  subsMenu(){ const m=$("#submenu"); if(m) m.hidden=!m.hidden; },

  subMenuDraw(items){
    const m=$("#submenu"); if(!m) return;
    m.innerHTML="";
    items.forEach(it=>{
      const r=document.createElement("button"); r.className="row";
      const dot=document.createElement("span");
      dot.className="sdot "+(it.st==="running"?"run":it.st==="done"?"ok":"bad");
      r.appendChild(dot);
      const t=document.createElement("span"); t.className="stitle";
      t.textContent=it.task||""; r.appendChild(t);
      const w=document.createElement("span"); w.className="who";
      w.textContent=it.model||""; r.appendChild(w);
      r.dataset.open="1";
      r.onclick=()=>{ m.hidden=true; crow.subJump(it.i); };
      m.appendChild(r); });
    const h=document.createElement("div"); h.className="hint";
    h.textContent="click a row to jump to its card"; m.appendChild(h);
  },

  // The rail's child rows: each subtask hangs FIXED under the chat that
  // spawned it -- robin, 2026-08-27: "diese duerfen niemals mitwandern". A
  // parent of "" means THE LIVE CHAT WITHOUT A FILE and nothing else; there
  // is deliberately no fallback to the active row, because that fallback was
  // the wandering: open a subtask, and every child re-hung under it. A parent
  // row that is not drawn simply keeps its children undrawn.
  subRail(){
    document.querySelectorAll("#sessions .subchat").forEach(x=>x.remove());
    const items=this.subItems||[]; if(!items.length) return;
    const box=$("#sessions"); if(!box) return;
    const groups={};
    items.forEach(it=>{ const key=it.parent||"";
      (groups[key]=groups[key]||[]).push(it); });
    Object.keys(groups).forEach(parent=>{
      const row=parent
        ? box.querySelector('[data-path="'+CSS.escape(parent)+'"]')
        : box.querySelector(".sess:not([data-path])");
      if(!row) return;
      groups[parent].slice().reverse().forEach(it=>{
        const b=document.createElement("button"); b.className="subchat";
        const g=document.createElement("span"); g.className="glyph";
        g.textContent="⑂"; b.appendChild(g);
        const dot=document.createElement("span");
        dot.className="sdot "+(it.st==="running"?"run":it.st==="done"?"ok":"bad");
        b.appendChild(dot);
        const t=document.createElement("span"); t.className="stitle";
        t.textContent=it.task||""; b.appendChild(t);
        const w=document.createElement("span"); w.className="who";
        w.textContent=it.model||""; b.appendChild(w);
        // A SUBCHAT IS NEVER OPENED AS A CHAT. Opening one made it the live
        // conversation, put the real chat aside and re-hung every child --
        // the exact break robin filmed. The click goes to the CARD, which
        // already holds the task, the clock and the folded result.
        b.dataset.open="1"; b.title="jump to its card";
        b.onclick=()=>crow.subJump(it.i);
        row.after(b); });
    });
  },

  subJump(i){
    const d=flow.querySelector('.subcard[data-sub="'+CSS.escape(i)+'"]');
    if(d){ d.scrollIntoView({behavior:"smooth",block:"center"}); return; }
    // No card in this flow: the subtask belongs to another chat. Open THAT
    // chat -- the parent, never the subtask itself -- and finish the jump
    // when the replayed snapshot has drawn its cards.
    const it=(this.subItems||[]).find(x=>x.i===i);
    if(it && it.parent){ this.subPending=i; crow.open(it.parent); }
  },

  chatRow(r,inproj){ const b=document.createElement("button");
    b.className=(r.active ? "sess on" : "sess")+(inproj ? " inproj" : "");
    if(r.path) b.dataset.path=r.path;   // what the mark is moved by, below
    b.innerHTML='<span class="t"></span><span class="s"></span>';
    b.querySelector(".t").textContent=r.title || r;
    b.querySelector(".s").textContent=r.meta || "";
    b.title="open · right-click for more";
    // EVERY ENTRY IS CLICKABLE, the open one included. Clicking it is a no-op in
    // Python -- and that is where the decision belongs, not in whether a handler
    // exists.
    if(r.path){ b.onclick=()=>crow.open(r.path);
      b.oncontextmenu=e=>crow.menu(e,"chat",r,b); }
    return b; },

  projectRow(p,count){ const h=document.createElement("button");
    h.className="proj"+(p.open ? " open" : "");
    h.dataset.path=p.path;
    h.innerHTML='<span class="caret">&#9654;</span><span class="t"></span>'
               +'<span class="n"></span>';
    h.querySelector(".t").textContent=p.name;
    // THE COUNT IS THE ONLY THING THAT SURVIVES FOLDING. A shut project with no
    // number is a heading that says nothing about what is inside it, which is
    // the state that makes people open every one of them to look.
    h.querySelector(".n").textContent=count ? String(count) : "";
    h.title=p.path;
    h.onclick=()=>crow.toggleProject(p.path,!p.open);
    h.oncontextmenu=e=>crow.menu(e,"project",p,h);
    return h; },

  rail(e){
    const title=e.title, meta=e.meta, rollovers=e.rollovers, unsaved=e.unsaved;
    this.projects=e.projects||[];
    this.liveRoot=e.live_root||"";
    // #143. The rail payload carries the subtasks, so a redraw cannot lose
    // their rows; the ticker's own event updates the same list between rails.
    if(e.subs!==undefined) this.subItems=e.subs||[];
    const box=$("#sessions");
    // SAME CHATS, SAME ORDER -> MOVE THE MARK, DO NOT REBUILD. Every update used
    // to throw the list away and remake it, so a click exchanged every node under
    // the cursor.
    //
    // THE PROJECTS ARE IN THE SHAPE, and every chat's ROOT with them. Without
    // that, folding a project or moving a chat into one would land on the fast
    // path and change nothing on screen -- the list would be right in Python and
    // stale in the window, which is the worst of the three possible states.
    // DIE TITEL GEHOEREN IN DIE SHAPE (robin, 2026-08-28 abends): der Name
    // eines geloeschten Chats stand weiter in der Rail, bis ein Rename kam --
    // eine reine Titelaenderung landete auf dem Schnellpfad und bewegte kein
    // Pixel; erst der Rename baute neu, weil er den PFAD bewegt. Das meta
    // bleibt draussen: es aendert sich jede Runde, und die shape existiert,
    // damit nicht jede Runde die Liste neu baut.
    const shape=(rollovers||[]).map(r=>(r.path||"")+">"+(r.root||"")+">"+(r.title||"")).join("\n")
      +"|"+(unsaved?"live>"+title:"")+"|"+this.liveRoot
      +"|"+this.projects.map(p=>p.path+(p.open?"+":"-")).join("\n");
    if(box.dataset.shape===shape){
      (rollovers||[]).forEach(r=>{ if(!r.path) return;
        const b=box.querySelector('[data-path="'+CSS.escape(r.path)+'"]');
        if(!b) return;
        b.classList.toggle("on", !!r.active);
        // THE HANDLER MOVES WITH THE MARK. Toggling the class alone left the
        // entry that was active at the first draw without one, forever -- and
        // that is a chat you cannot click.
        b.onclick=()=>crow.open(r.path);
        b.title="open · right-click for more"; });
      this.subRail();
      return;
    }
    box.dataset.shape=shape;
    box.innerHTML="";

    // THE LIVE CHAT WITHOUT A FILE is drawn from the window's own copy: it has
    // no entry in `rollovers` yet because the core writes nothing for a chat
    // with no turn in it. It goes under its project like any other, because a
    // new chat started inside one belongs there from its first line -- not from
    // its first save.
    const live=unsaved ? {path:null,title:title,meta:meta,active:true,
                          root:this.liveRoot} : null;
    const all=(rollovers||[]).slice();
    const rowFor=r=>{
      if(r!==live) return this.chatRow(r,!!this.projectOf(r.root));
      const b=this.chatRow(r,!!this.projectOf(r.root));
      b.title="right-click for more";
      b.oncontextmenu=ev=>crow.menu(ev,"chat",r,b);
      return b; };
    if(live) all.unshift(live);

    const taken=new Set();
    this.projects.forEach(p=>{
      const mine=all.filter(r=>this.sameDir(p.path,r.root));
      mine.forEach(r=>taken.add(r));
      box.appendChild(this.projectRow(p,mine.length));
      // FOLDED MEANS NOT DRAWN, not drawn and hidden. A shut project that still
      // built its rows would keep every one of them in the tree, and the mark
      // that moves on the fast path above would find a node nobody can see.
      if(p.open) mine.forEach(r=>box.appendChild(rowFor(r)));
    });

    const loose=all.filter(r=>!taken.has(r));
    if(loose.length){
      // THE HEADING ONLY MEANS SOMETHING WITH A PROJECT ABOVE IT. Without one
      // these are simply the chats, and a word over the whole list is furniture.
      if(this.projects.length){ const h=document.createElement("div");
        h.id="railsep"; h.textContent="Chats"; box.appendChild(h); }
      loose.forEach(r=>box.appendChild(rowFor(r)));
    }
    this.subRail();
  },

  // #119. THE EMPTY SPACE IS A TARGET TOO. Right-clicking the list where no chat
  // is has to answer something, or the two things you do least often -- start a
  // chat, make a project -- are the two with no way in from here.
  railMenu(e){ if(e.target.closest(".sess,.proj")) return;
    this.menu(e,"rail",null,null); },

  toggleRail(){ const el=document.body;
    const open=el.dataset.rail!=="shut";
    el.dataset.rail=open ? "shut" : "open";
    pywebview.api.set_rail_open(!open); },

  // #138. DASSELBE FUER RECHTS. Eigene Methode statt eines Parameters: die
  // beiden Panels teilen ihr Aussehen und sonst nichts -- verschiedene Grenzen,
  // verschiedene Vorgabe, verschiedene Einstellung.
  toggleCode(){ const el=document.body;
    const open=el.dataset.code!=="shut";
    el.dataset.code=open ? "shut" : "open";
    pywebview.api.set_code_open(!open); },

  // WAS DA STEHT, IN DIE ZWISCHENABLAGE. `innerText` und nicht `textContent`:
  // das eine liefert, was zu sehen ist, das andere auch den Inhalt von allem,
  // was gerade zugeklappt ist -- und kopiert wird, worauf jemand sieht.
  codeCopy(ev){
    ev.stopPropagation();
    const body=$("#codebody"), btn=$("#codecopy");
    // UEBER PYTHON, NICHT UEBER DIE SEITE. Die Seite ist als HTML geladen und
    // damit kein sicherer Kontext -- `navigator.clipboard` lehnt dort ab, ohne
    // zu werfen, und der Knopf saegte "copied" ueber eine leere Ablage. Dieselbe
    // Naht, die der Knopf am Codeblock schon nimmt.
    // `innerText` und nicht `textContent`: das eine liefert, was zu sehen ist,
    // das andere auch den Inhalt von allem, was gerade zugeklappt ist.
    const text = body ? body.innerText.trim() : "";
    if(!text){ btn.textContent="empty";
      setTimeout(()=>{btn.textContent="copy";},1400); return; }
    pywebview.api.copy(text).then(ok=>{
      btn.textContent = ok ? "copied" : "failed";
      setTimeout(()=>{btn.textContent="copy";},1400); }); },

  toggleProject(path,open){ pywebview.api.set_project_open(path,open); },

  // THE ARCHIVE IS A DRAWER, shut by default. It holds what the user put out of
  // the way, so opening it has to be their move -- a section that is always
  // there is the list they were trying to shorten.
  archive(items){
    const box=$("#arch"), bar=$("#archbar");
    bar.querySelector(".count").textContent = items.length ? items.length : "";
    bar.style.display = items.length ? "flex" : "none";
    box.innerHTML="";
    items.forEach(r=>{
      const b=document.createElement("button"); b.className="sess";
      b.innerHTML='<span class="t"></span><span class="s"></span>';
      b.querySelector(".t").textContent=r.title;
      b.querySelector(".s").textContent=r.meta;
      b.title="open · right-click for more";
      b.onclick=()=>crow.open(r.path);
      b.oncontextmenu=e=>crow.menu(e,"chat",r,b,true);
      box.appendChild(b);
    });
    if(!items.length) $("#arch").classList.remove("open");
  },
  toggleArchive(){ $("#arch").classList.toggle("open");
    $("#archbar").classList.toggle("open"); },

  on(msg){
    const e=typeof msg==="string" ? JSON.parse(msg) : msg;
    switch(e.k){
      case "up": $("#dot").className="up"; $("#state").textContent="connected";
        // #115: kept on the page rather than asked for when the menu opens, so
        // a click answers from what the last probe SAW instead of racing it.
        if(e.models){ this.models=e.models; }
        if(e.model_key!==undefined){ this.modelKey=e.model_key; }
        if(e.levels){ this.levels=e.levels; }
        // #117. BEFORE showReason, not after: the chip's own text depends on the grouping now,
        // so a chip drawn first would name `off` and be corrected a frame later.
        if(e.groups!==undefined){ this.groups=e.groups; }
        // #119. STORED, THEN DRAWN ONCE. Both halves live on one chip now, so a payload that
        // carries the model and the level must not paint twice -- the first paint would name
        // the level against the OLD model for a frame, and a switch is exactly when the two
        // disagree. `n_ctx` is no longer a chip of its own: it is the denominator the composer
        // already prints, and printing it twice is the bloat this bar was cut for.
        if(e.reasoning!==undefined){ this.reasoning=e.reasoning||""; }
        if(e.model){ this.modelName=e.model; }
        this.showModel();
        this.ctx(e.tokens||0,e.n_ctx||0); break;
      case "reasoning": if(e.levels){ this.levels=e.levels; }
        if(e.groups!==undefined){ this.groups=e.groups; }
        this.showReason(e.level); break;
      case "down": $("#dot").className="down";
        $("#state").textContent=e.why||"no server"; break;
      // #125. STRAIGHT TO ABOUT. The version used to sit beside the wordmark and
      // be copied into the sheet when it opened; the ribbon is a name and three
      // window buttons now, so the number goes where somebody looks it up.
      case "meta": $("#aboutver").textContent=e.version;
        if(e.rail) document.documentElement.style.setProperty(
          "--railw", e.rail+"px");
        // THE TITLE, NOT A CHIP (#119). Set rather than interpolated for the same reason every
        // other name here is: it is a string that arrived over the bridge.
        $("#conn").title=e.url;
        $("#tools").innerHTML="<b></b> tools<span></span>";
        $("#tools b").textContent=e.tools;
        $("#toolsw").onclick=()=>crow.toggleTools();
        this.tools(e.execute); break;
      case "tools": this.tools(e.on); break;
      case "mode": this.modeIs(e.name, e.modes); break;
      case "root": this.rootIs(e.path, e.name, e.roots); break;
      case "ask": this.ask(e.name, e.args, e.scope); break;
      case "rail": this.rail(e);
        this.archive(e.archived||[]); break;
      // THE PAGE CLEARS ITSELF ON "new", because the click is here. A DELETE of
      // the chat being read starts on the page too but is decided in Python --
      // it may fail -- so the emptying has to come back from there.
      // #128: THE CHIP GOES WITH THE CHAT. `forget_approvals` drops the staged
      // writes on the Python side; without this the page would keep breathing
      // about notes that no longer exist. One place, because `clear` is already
      // the one event that means "this conversation is gone".
      case "clear": flow.innerHTML=""; this.cost("",null);
        this.pendState([]); this.toolsReset(); this.endTrace(); break;
      case "hello": this.hello(e.t); break;
      // #94. /thoughts in the terminal shows or hides the reasoning; here it is
      // always rendered and folded, so the same question is open-or-closed.
      // EVERY block, not just the ones on screen -- a fold that only reached
      // the last answer would read as broken on the one above it.
      case "thoughts": document.querySelectorAll("details.think")
        .forEach(d=>{ d.open=e.open; }); break;
      case "user": this.user(e.t); if(e.i) this.userImages(e.i); break;
      case "start": this.start(); break;
      case "think_open": this.thinkOpen(); break;
      case "think": this.thinkText(e.t); break;
      case "think_close": this.thinkClose(); break;
      case "text": this.answer(e.t); break;
      case "code_open": this.codeOpen(e.lang); break;
      case "format": this.format(e.blocks); break;
      case "update": this.updated(e); break;
      case "voice": this.voice(e); break;
      case "code_close": this.codeClose(e.closed); break;
      case "tool": this.tool(e.name,e.args,e.raw,e.code); break;
      case "toolend": this.toolEnd(e.name,e.s,e.rep); break;
      case "toolres": this.toolRes(e.name,e.t,e.cut); break;
      case "cost": this.cost(e.line,e.share,e.sub); this.ctx(e.tokens,e.n_ctx); break;
      case "ctx": this.ctx(e.tokens,e.n_ctx); break;
      case "subs": this.subs(e.items); break;
      case "note": this.note(e.t); break;
      case "chips": this.stageRender(e.c); break;
      case "memory": this.memory(e.t,e.n); break;
      case "toolarg": this.toolArg(e.i,e.name,e.t,e.code); break;
      case "alarm": this.alarm(e.t); break;
      case "fail": this.fail(e.t); break;
      case "live":
        $("#turnstate").textContent =
          e.n + " tok · " + e.rate.toFixed(1) + " tok/s";
        break;
      case "pend": this.pendState(e.items); break;
      case "elicit": this.elicit(e.ask); break;
      case "mic": this.micState(e); break;
      case "drop": this.dropped(e.paths); break;
      case "idle": this.idle(); break;
      case "busy": this.busy(); break;
      case "queued": this.queuedLine(); break;
    }
  }
};
window.crow = crow;

input.addEventListener("input",()=>{ input.style.height="auto";
  input.style.height=Math.min(input.scrollHeight,140)+"px"; });
input.addEventListener("focus",()=>box.classList.add("focus"));
input.addEventListener("blur",()=>box.classList.remove("focus"));
input.addEventListener("keydown",e=>{
  if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); crow.go(); }
  if(e.key==="Escape" && crow.running) pywebview.api.stop(); });

// BOTH HAVE TO BE PREVENTED, and dragover is the one people forget: without it
// the drop never reaches a listener at all, because WebView2 has already
// decided to navigate to the file and show it instead of the window.
document.addEventListener("dragover", e => { e.preventDefault(); crow.dragging(true); });
document.addEventListener("drop",     e => { e.preventDefault(); crow.dragging(false); });
// LEAVING THE DOCUMENT, not an element: dragleave fires for every child the
// pointer crosses, and `relatedTarget === null` is what tells the two apart.
document.addEventListener("dragleave", e => { if(!e.relatedTarget) crow.dragging(false); });

// CTRL+V, AND THE PICTURE IS FETCHED RATHER THAN RECEIVED. The first build read
// `clipboardData.items` and found nothing: measured 2026-08-21, a Windows
// screenshot sat on the clipboard as PNG and Bitmap while the paste event handed
// the page no image item at all. So the page stops digging and asks Python,
// which reads the same clipboard through user32.
//
// TEXT WINS AND IS LEFT ALONE. `types` carrying text/plain means somebody pasted
// words, and that path is untouched -- no preventDefault, no bridge call.
//
// ON THE DOCUMENT, NOT THE BOX: Ctrl+V should work when the focus sits on a chip
// or on nothing, and `attach` puts the caret back where the typing happens.
document.addEventListener("paste", e => {
  const dt = e.clipboardData;
  const types = dt ? Array.prototype.slice.call(dt.types || []) : [];
  if(types.indexOf("text/plain") !== -1) return;
  e.preventDefault();
  pywebview.api.paste_clipboard().then(path => {
    if(path) crow.attach(/\s/.test(path) ? '"'+path+'"' : path);
  });
});

// THE GAP UNDER THE FLOW IS THE COMPOSER'S OWN HEIGHT, measured and never
// guessed. robin's rule for the floating box is absolute: THE LAST LINE MUST
// NEVER COME TO REST BEHIND IT, because a line you cannot read is a line that
// was not printed. A constant would break in exactly the cases that matter --
// the textarea grows to 140px as you type, and the foot row wraps to two lines
// on a narrow window. `offsetHeight` is the whole box INCLUDING the 26px fade,
// so the last line stops above the fade rather than inside it.
const composer = $("#composer");
const fitFlow = () => {
  // MEASURE FIRST, THEN GROW. Reading the scroll position after the padding
  // changed would ask whether we are at a bottom that has already moved.
  const atBottom = flow.scrollHeight - flow.scrollTop - flow.clientHeight < 4;
  flow.style.paddingBottom = (composer.offsetHeight + 10) + "px";
  // RE-PIN, or typing a third line pushes the answer you were reading up and
  // out of sight -- the padding grows downwards and the view does not follow.
  if(atBottom) flow.scrollTop = flow.scrollHeight;
};
// The observer covers the window resize too: a narrower window rewraps the foot
// row, which changes the composer's height, which is the thing being watched.
new ResizeObserver(fitFlow).observe(composer);
// The grips: press, drag, and Python moves the window. `screenX/screenY` are
// used rather than clientX so the numbers stay right while the window itself is
// moving underneath the pointer.
(function grips(){
  const map={"g-n":"n","g-s":"s","g-w":"w","g-e":"e","g-nw":"nw","g-ne":"ne",
             "g-sw":"sw","g-se":"se"};
  let drag=null;
  Object.keys(map).forEach(id=>{
    const el=document.getElementById(id);
    el.addEventListener("mousedown",e=>{
      e.preventDefault();
      drag={edge:map[id],x:e.screenX,y:e.screenY};
      pywebview.api.geometry().then(g=>{ if(drag) drag.start=g; });
    });
  });
  window.addEventListener("mousemove",e=>{
    if(!drag||!drag.start) return;
    const dx=e.screenX-drag.x, dy=e.screenY-drag.y, s=drag.start, k=drag.edge;
    let x=s.x, y=s.y, w=s.w, h=s.h;
    if(k.includes("e")) w=s.w+dx;
    if(k.includes("s")) h=s.h+dy;
    if(k.includes("w")){ x=s.x+dx; w=s.w-dx; }
    if(k.includes("n")){ y=s.y+dy; h=s.h-dy; }
    pywebview.api.set_geometry(Math.round(x),Math.round(y),
                               Math.round(w),Math.round(h));
  });
  window.addEventListener("mouseup",()=>{ drag=null; });
})();

// A TABLE, NOT FOUR IF-LINES. Only the chat menu and the help menu closed on a click elsewhere;
// the three in the composer never joined, so the window had two dismissal behaviours and no rule
// saying which control got which. A menu that stays open when you look away is not a mode anybody
// chose -- it is the one the last click left behind.
//
// THE WRAPPER, NOT THE PANEL, and that is the whole reason each of these has a wrapper. Guarding
// on `#modelmenu` alone would close the menu on the mousedown that lands on its own chip, and the
// click a moment later would find it hidden and toggle it straight back open -- so the chip could
// open the menu and never close it. `#helpwrap` was written this way first; the other three now
// match it.
//
// `mousedown` RATHER THAN `click`, unchanged from what was here: the panel is gone before
// whatever sits under it does its work, and a row inside a panel is caught by its own wrapper.
const DISMISS = [["#helpwrap","#helpmenu"], ["#modelwrap","#modelmenu"],
                 ["#modewrap","#modemenu"], ["#rootwrap","#rootmenu"],
                 ["#subwrap","#submenu"]];
window.addEventListener("mousedown",e=>{
  if(!e.target.closest("#menu")) crow.closeMenu();
  DISMISS.forEach(pair => {
    if(!e.target.closest(pair[0])) $(pair[1]).hidden=true; }); });
// #119. THE LIST ITSELF ANSWERS NOW, not only the rows in it. A right-click
// on the empty space below the chats used to be swallowed by this guard --
// which was right when the rail had nothing to offer there, and is the
// reason "new chat" and "new project" had nowhere to live.
window.addEventListener("contextmenu",e=>{
  if(e.target.closest("#sessions")){ crow.railMenu(e); return; }
  if(!e.target.closest(".sess")) e.preventDefault(); });

// #131. THE TILE IS THERE BEFORE THE FIRST CALL IS, because "always present"
// is what makes it a place to look rather than something that appears once and
// is missed.
crow.toolsCount();
// KEINE STARTBREITEN-AUTOMATIK MEHR. #138c richtete eine nie gezogene Breite
// an der halben Flaeche aus -- auf robins Fenster am 2026-08-27 war genau das
// der zu breite Start, und die Icons standen wieder neben der Maske. Seine
// Ansage ersetzt das Feature: die Vorgabe ist CODE_DEFAULT (das Minimum), und
// wer mehr Panel will, zieht den Griff einmal. Dass der Composer bei KEINER
// Breite ueberlaeuft, garantiert seitdem das Layout selbst: #code gibt nach
// (min-width:0), #main traegt die Mindestbreite der Maske.

window.addEventListener("pywebviewready",()=>{ pywebview.api.ready(); input.focus(); });
</script></body></html>
"""


ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crow.ico")

# #127. THE BIRD UNDER THE GREETING. Two files rather than one recoloured by
# CSS: it is a low-poly drawing with five stroke colours, and `currentColor`
# can carry one. They are named for the BACKGROUND they are legible on, not for
# the file they came from -- pale strokes vanish on white, dark ones vanish on
# black, and that is a fact about contrast rather than a preference.
MARK_FILES = {"dark": "mark-on-dark.svg", "light": "mark-on-light.svg"}


def mark_svg(background: str) -> str:
    """The wireframe drawn for that background, or "" when it is not on disk.

    ABSENT IS EMPTY, NOT AN ERROR. A greeting without a bird is a greeting; a
    window that refused to open because a drawing was missing is not a window.
    """
    name = MARK_FILES.get(background)
    if not name:
        return ""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
                  encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:                      # noqa: BLE001 - see the docstring
        return ""


def taskbar_identity() -> bool:
    """Tell the shell this process is Crow, before the window exists.

    WITHOUT IT THE TASKBAR SHOWS PYTHON. A button is grouped and labelled by the
    Application User Model ID, and a process that never sets one inherits the
    interpreter's -- so the icon set on the window below is drawn under Python's
    name, beside every other script the user has running.

    BEFORE THE WINDOW, not after: the shell reads the ID when it registers the
    button, the same way it reads the style bits in `shell_buttons`, and neither
    is looked at again afterwards.
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Crow.Window")
        return True
    except Exception:                      # noqa: BLE001 - cosmetic, never fatal
        return False


def set_icon(hwnd) -> bool:
    """Hang `crow.ico` on one window. True when both sizes went on.

    pywebview CANNOT DO THIS ON WINDOWS. Its `icon=` is a parameter of
    `start()`, documented in its own source as "supported only on GTK and QT";
    on Windows the icon is meant to be baked in when the app is frozen, and Crow
    runs as a script. So the icon goes on the way every other Win32 thing here
    goes on: through the handle the search above already found.

    TWO SIZES, LOADED SEPARATELY. `ICON_BIG` is what Alt-Tab and the taskbar
    draw, `ICON_SMALL` is the caption and the Alt-Tab strip; asking for one and
    letting Windows scale it gives a soft 16px from a 256px source. The metrics
    are asked for rather than hard-coded because they change with the display
    scaling.

    A MISSING FILE IS NOT AN ERROR. The window works without an icon, and a
    client that refused to open because a decoration was absent would be the
    worse failure.
    """
    if not os.path.isfile(ICON_FILE):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        SM_CXICON, SM_CYICON, SM_CXSMICON, SM_CYSMICON = 11, 12, 49, 50
        user32 = ctypes.windll.user32
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        ctypes.c_ssize_t, ctypes.c_ssize_t]
        both = True
        for which, cx, cy in ((ICON_BIG, SM_CXICON, SM_CYICON),
                              (ICON_SMALL, SM_CXSMICON, SM_CYSMICON)):
            handle = user32.LoadImageW(None, ICON_FILE, IMAGE_ICON,
                                       user32.GetSystemMetrics(cx),
                                       user32.GetSystemMetrics(cy), LR_LOADFROMFILE)
            if not handle:
                both = False
                continue
            user32.SendMessageW(hwnd, WM_SETICON, which, handle)
        return both
    except Exception:                      # noqa: BLE001 - cosmetic, never fatal
        return False


def shell_buttons(title: str) -> bool:
    """Give the frameless window the styles and the icon the taskbar reads.

    THE ICON RIDES THIS SEARCH RATHER THAN DOING ITS OWN. Finding the window is
    the hard half -- four things had to be right at once, see below -- and a
    second EnumWindows would be a second chance to pick the HELPER window, which
    looks like a working icon on a window nobody sees.

    CLICKING A RUNNING APP'S TASKBAR BUTTON MINIMISES IT -- but only when the
    window says it can be minimised. The shell decides that from WS_MINIMIZEBOX
    in the window style, and a frameless window is created without it, because
    the style normally travels with the caption buttons that were switched off.
    The result is a button that raises the window and then does nothing on the
    second click, which reads as a hang rather than as a missing style bit.

    WS_MAXIMIZEBOX goes in with it for the same reason one step further out:
    Aero Snap and Win+Up both ask the same question of the same style.

    FOUR THINGS HAD TO BE RIGHT AT ONCE, measured 2026-08-13:

      * `FindWindowW(None, title)` never found this window -- twenty seconds of
        searching for the caption the process reports as its own returned
        nothing. The window is found through its PROCESS instead.
      * Without `argtypes` ctypes truncates a 64-bit HWND to 32 bits, so every
        call named a window that does not exist and reported success.
      * The first visible window of a pywebview process is a HELPER; only the
        one with a caption is the window the user sees.
      * WS_MINIMIZEBOX IS IGNORED WITHOUT WS_SYSMENU, and a frameless window is
        created without either.

    AND THE SHELL HAD ALREADY MADE UP ITS MIND. It reads the style when it
    registers the taskbar button; a style changed afterwards is not looked at
    again. Hiding and re-showing the window forces the button to be registered
    once more, which is what finally made the click fold the window away.

    NOT `SetWindowPos(SWP_FRAMECHANGED)`: it recalculates the non-client area,
    and a frameless window loses the region `pywebview-drag-region` hangs on --
    that cost the drag and the maximise on the first attempt.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        GWL_STYLE = -16
        WS_MINIMIZEBOX, WS_SYSMENU = 0x00020000, 0x00080000
        SW_HIDE, SW_SHOW = 0, 5
        user32 = ctypes.windll.user32
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_ssize_t
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_long.restype = ctypes.c_ssize_t
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
        callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback, wintypes.LPARAM]

        mine = ctypes.windll.kernel32.GetCurrentProcessId()
        windows: list = []

        def visit(hwnd, _lparam) -> bool:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if (owner.value == mine and user32.IsWindowVisible(hwnd)
                    and user32.GetWindowTextLengthW(hwnd) > 0):
                windows.append(hwnd)
            return True

        user32.EnumWindows(callback(visit), 0)
        for hwnd in windows:
            style = get_long(hwnd, GWL_STYLE)
            set_long(hwnd, GWL_STYLE, style | WS_SYSMENU | WS_MINIMIZEBOX)
            if not get_long(hwnd, GWL_STYLE) & WS_MINIMIZEBOX:
                continue
            set_icon(hwnd)
            user32.ShowWindow(hwnd, SW_HIDE)
            user32.ShowWindow(hwnd, SW_SHOW)
            return True
        return False
    except Exception:                      # noqa: BLE001 - cosmetic, never fatal
        return False


# ---------------------------------------------------------------- the bridge

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
            '"': '"', "\\": "\\", "/": "/"}


class Unescaper:
    """JSON-String-Escapes entfalten, waehrend der Text noch stroemt.

    #138. Was ueber die Leitung kommt, ist ein JSON-String und nicht Quelltext:
    `\\n` sind zwei Zeichen, `\\"` sind zwei Zeichen. robin am 2026-08-24, als das
    Mitlesen zum ersten Mal lief -- lesbar, aber muehsam.

    SPRACHUNABHAENGIG OHNE ZUTUN. Die Escapes gehoeren dem Transport, nicht der
    Sprache; `\\n` sieht in Python, SVG, Rust und Bash gleich aus. "Fuer alle
    Programmiersprachen" ist deshalb keine Arbeit, sondern die Eigenschaft
    dieser Schicht.

    ZEICHENWEISE UND NICHT DURCH ERSETZEN. Wer erst `\\n` durch einen Umbruch
    ersetzt und danach `\\\\` durch einen Backslash, macht aus `C:\\\\new` einen
    Zeilenumbruch -- und trifft damit genau die Zeichenketten, die einen
    Backslash MEINEN: Windows-Pfade, regulaere Ausdruecke, LaTeX.

    EIN OBJEKT UND KEINE FUNKTION, weil ein `\\` das letzte Zeichen eines
    Fragments sein kann und sein Partner im naechsten kommt. Der Rest wartet
    hier, bis er vollstaendig ist. Ein unbekanntes Escape (`\\d` aus einem
    regulaeren Ausdruck) bleibt unangetastet: es zu schlucken hiesse, den
    Ausdruck still zu aendern.
    """

    def __init__(self) -> None:
        self._rest = ""

    def feed(self, piece: str) -> str:
        text, self._rest = self._rest + (piece or ""), ""
        out, i, n = [], 0, len(text)
        while i < n:
            ch = text[i]
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            if i + 1 >= n:                      # Backslash am Rand: warten
                self._rest = text[i:]
                break
            nxt = text[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "u":
                if i + 6 > n:                   # \uXX... noch nicht ganz da
                    self._rest = text[i:]
                    break
                hexes = text[i + 2:i + 6]
                try:
                    out.append(chr(int(hexes, 16)))
                except ValueError:
                    out.append(ch)              # kein gueltiges \u: stehen lassen
                    i += 1
                    continue
                i += 6
                continue
            out.append(ch)                      # unbekannt: unangetastet
            i += 1
        return "".join(out)


class Sink(ReplyEvents, FenceEvents):
    """The core's reply callbacks, turned into messages for the page.

    THE ANSWER GOES THROUGH `crow_core.CodeFences`, never straight to the page.
    Without it a fenced block arrives as its literal characters -- three
    backticks, the language, the code, three backticks -- and the reader gets
    markdown source instead of a frame with a copy button. Where a block begins
    and ends is a decision, and every decision in this window belongs to the
    core: the terminal asks the same class the same question, so both surfaces
    cut the same stream in the same places.
    """

    def __init__(self, put, live: bool = True) -> None:
        self._put = put
        self._live = live
        self._fences: CodeFences | None = None
        # THE PROSE OF THE RUN THAT IS OPEN, kept so it can be cut into blocks
        # once it is over. Code lines are NOT in here: they arrive through
        # `code_text` and already have a frame of their own.
        self._prose: list[str] = []
        self._deltas = 0
        self._started = 0.0
        self._last = 0.0

    def reply_started(self) -> None:
        self._put({"k": "start"})
        self._fences = CodeFences(self)
        self._prose = []
        self._deltas = 0
        self._started = self._last = time.monotonic()

    def _tick(self) -> None:
        """A live count while the answer is being written.

        WHAT IS COUNTED IS DELTAS, NOT TOKENS, and the difference is not
        pedantry: llama.cpp sends one content delta per token in practice, but
        nothing in the protocol promises it, and #90's E14 probe (iii) -- does
        `timings_per_token` attach a block to every chunk or only the last --
        has never been run. So this is the client's own count, shown while the
        turn is open, and the line that lands underneath at the end is the
        SERVER's. If the two ever disagree, both are on screen to be compared.

        FIVE UPDATES A SECOND AT MOST. The point of batching per tick is not to
        hand the same work back through a different queue.

        THE DENOMINATOR IS WALL CLOCK, AND THAT IS A DECISION -- NOT THE BUG IT
        LOOKS LIKE (robin, #97, 2026-08-14). Divided this way the figure is
        always LOWER than the server's `tg`, because `elapsed` also holds the
        wait for the first token, every tool call and the prefill of every tool
        result: 9.5 tok/s on screen beside a server logging 17.99-19.29 t/s for
        the same turn, and web research widened the gap because a search is
        exactly that kind of pause.

        It was changed to sum only the gaps between deltas and changed straight
        back. What the user waits through is the wall clock; a decode rate that
        ignores the pauses answers a question the server already answers, and the
        line underneath at the end IS the server's. Two figures, two meanings,
        both on screen -- which is what the paragraph above already says.

        SO DO NOT "FIX" THIS. `TheLiveRateIsWallClockOnPurposeTests` fails if the
        pauses stop counting, and `crow_core.TurnCost` is where the decode figure
        lives if that is what is wanted.
        """
        if not self._live:
            return
        self._deltas += 1
        now = time.monotonic()
        if now - self._last < 0.2:
            return
        self._last = now
        elapsed = now - self._started
        self._put({"k": "live", "n": self._deltas,
                   "rate": (self._deltas / elapsed) if elapsed > 0.05 else 0.0})

    def answer_text(self, piece: str) -> None:
        if self._fences is None:
            self._fences = CodeFences(self)
        self._tick()
        self._fences.feed(piece)

    def reasoning_text_counted(self) -> None:
        self._tick()

    def reply_finished(self) -> None:
        """Closes a block the answer stopped inside -- cut off, aborted, dropped
        socket. The frame is still drawn and still copyable; the note beside it
        is what stops it reading as a finished block."""
        if self._fences is not None:
            self._fences.finish()
            self._fences = None
        self._formatted()

    # -- FenceEvents: what the core decided, drawn --------------------------

    def prose(self, piece: str) -> None:
        self._prose.append(piece)
        self._put({"k": "text", "t": piece})

    def code_started(self, language: str) -> None:
        # BEFORE THE FENCE, NOT AFTER IT. The page hangs the blocks on the prose
        # element it is holding and `codeOpen` lets go of that element, so a
        # `format` arriving afterwards would find nothing and the bold above the
        # code would stay stars.
        self._formatted()
        self._put({"k": "code_open", "lang": language or ""})

    def _formatted(self) -> None:
        """The run of prose that just ended, cut into blocks.

        NOT WHILE IT STREAMS. Half of `**bold` is not bold yet, and a page fed
        deltas would flicker between two readings of one sentence. `CodeFences`
        can stream because a fence is a whole line; emphasis is not.

        NOTHING TO SAY IS NOTHING SENT: an answer that was cut off before it
        spoke must not leave an empty frame on the screen.
        """
        text, self._prose = "".join(self._prose), []
        blocks = crow_core.markdown_blocks(text)
        if blocks:
            self._put({"k": "format", "blocks": blocks})

    def code_text(self, line: str) -> None:
        self._put({"k": "text", "t": line + "\n"})

    def code_finished(self, closed: bool) -> None:
        self._put({"k": "code_close", "closed": bool(closed)})

    def reasoning_started(self, index: int) -> None:
        self._put({"k": "think_open"})

    def reasoning_text(self, piece: str) -> None:
        # Reasoning counts. It is 53 % of a turn at the shipped operating point,
        # and a counter that ignored it would sit at zero through the longest
        # part of the wait -- exactly when the user wants to see something move.
        self._tick()
        self._put({"k": "think", "t": piece})

    def reasoning_finished(self) -> None:
        self._put({"k": "think_close"})

    def tool_arguments(self, index: int, name: str, piece: str) -> None:
        """#138. Ein Stueck der Argumente, sofort weitergereicht.

        OHNE `_tick`. Der Zaehler misst, was das Modell fuer die ANTWORT
        erzeugt; Werkzeugargumente in dieselbe Rate zu werfen liesse sie
        springen, sobald ein `write_file` anfaengt -- und die Rate ist genau
        das, woran jemand ablesen will, ob es noch laeuft.
        """
        # EIN ENTFALTER JE AUFRUF. Zwei Aufrufe einer Runde teilen sich den
        # Strom; ein gemeinsamer Puffer schoebe das haengende Zeichen des einen
        # in den anderen.
        if not hasattr(self, "_folds"):
            self._folds = {}
        fold = self._folds.get(index)
        if fold is None:
            fold = self._folds[index] = Unescaper()
        # OB DARAUS PROGRAMMCODE WIRD, sagt die Naht -- ZEIGEN entscheidet die
        # Seite. Der Filter sass hier zuerst als `return`, und
        # `test_two_calls_do_not_share_a_pending_backslash` hat es gefangen:
        # ein Fall mit `read_file` als zweitem Aufruf bekam keinen Strom mehr.
        # Er hatte recht aus einem groesseren Grund als seinem eigenen -- eine
        # Naht, die nach Werkzeugnamen aussortiert, nimmt jeder kuenftigen
        # Ansicht die Moeglichkeit, denselben Strom anders zu zeigen. Dieselbe
        # Regel wie bei `tool_result`: hinueber geht alles, der Bildschirm
        # entscheidet.
        self._put({"k": "toolarg", "i": index, "name": name,
                   "code": name in CODE_TOOLS,
                   "t": fold.feed(piece)})


class Turn(TurnEvents):
    """The core's turn callbacks. Every one of them ends up on the screen."""

    def __init__(self, put) -> None:
        self._put = put
        self._sink = Sink(put)
        # THE REASONING'S SHARE OF THE TURN, KEPT RATHER THAN PUSHED. It used
        # to leave here as `{"k": "_round"}` -- a message the page has no case
        # for, so it fell through the switch and was gone, while the cost line
        # was drawn from a variable nothing had written: every turn reported a
        # share of null. A number that only this window computes belongs to the
        # turn object, and the turn object is read when the turn ends.
        #
        # OF THE TURN, NOT OF THE LAST ROUND, and that was a real defect until #117. Each round
        # OVERWROTE this with its own ratio, and the page then stamped whatever survived onto
        # EVERY thought block of the turn -- so an eleven-round turn wore round eleven's number
        # eleven times under a label reading "% of the turn". The characters are summed instead
        # and the ratio taken once, which is what the label always claimed.
        #
        # SUMMED AND NOT AVERAGED: a round of 6,000 thought characters and a round of 40 are not
        # worth the same, and a mean over rounds would let a short tool round drag the figure of
        # a long one. The denominator is the turn's characters, so every one counts once.
        self._reasoning_chars = 0
        self._content_chars = 0
        self.share: float | None = None

    def reply_events(self) -> ReplyEvents:
        return self._sink

    def turn_failed(self, message: str) -> None:
        self._put({"k": "fail", "t": message})

    def turn_note(self, message: str) -> None:
        # 2026-08-28: der Selbstheilungs-Reboot spricht waehrend der ~70 s
        # sichtbar ("stopping/starting/server ready") -- ein stiller Haenger
        # saehe exakt aus wie der Absturz, den er gerade repariert.
        self._put({"k": "note", "t": message})

    def turn_interrupted(self) -> None:
        self._put({"k": "fail",
                   "t": crow_core.ABORT_NOTE})

    def round_finished(self, timings: dict) -> None:
        rc, cc = timings.get("_reasoning_chars"), timings.get("_content_chars")
        if isinstance(rc, int) and isinstance(cc, int) and rc + cc > 0:
            self._reasoning_chars += rc
            self._content_chars += cc
            total = self._reasoning_chars + self._content_chars
            self.share = 100.0 * self._reasoning_chars / total

    def cache_promise_broken(self) -> None:
        self._put({"k": "note",
                   "t": "the restored cache did not hold -- "
                        "that prefill was the whole history"})

    def tool_started(self, name: str, arguments: str) -> None:
        # DIE ROHEN ARGUMENTE REISEN MIT, nicht nur die gekuerzte Zeile. Die
        # Zeile ist der Kopf einer Klappe; was darin steht, wenn jemand sie
        # oeffnet, ist das, was das Werkzeug wirklich bekommen hat.
        self._put({"k": "tool", "name": name,
                   "args": crow_core.format_tool_args(arguments),
                   "raw": arguments or "",
                   "code": name in CODE_TOOLS})

    def tool_finished(self, name: str, seconds: float, repeated: bool) -> None:
        """#138b. Die Uhr an die Klappe, die der Aufruf schon hat.

        DAS FENSTER HAT DIESE ZEILE VORHER NICHT ABGEHOERT. Sie stand nur im
        Terminal, und im Panel sah ein Aufruf, der zwei Minuten lief, aus wie
        einer, der sofort zurueckkam.
        """
        self._put({"k": "toolend", "name": name,
                   "s": round(float(seconds), 2), "rep": bool(repeated)})

    def tool_result(self, name: str, result: str) -> None:
        """#138b. Was der Aufruf geantwortet hat, gedeckelt fuer den Bildschirm.

        HIER WIRD GESCHNITTEN, NICHT IM KERN. Die Naht traegt die ganze Antwort
        und ueberlaesst dem Bildschirm, wieviel davon passt -- das Terminal
        zeigt eine Zeile, dieses Fenster viertausend Zeichen, und beide sind
        Antworten auf dieselbe Frage statt zwei verschiedene Wahrheiten.
        """
        said = str(result or "")
        cut = len(said) - TOOL_RESULT_SHOWN
        self._put({"k": "toolres", "name": name,
                   "t": said[:TOOL_RESULT_SHOWN],
                   # DIE ZAHL, NICHT NUR EIN ZEICHEN. Drei Punkte sagen, dass
                   # etwas fehlt; sie sagen nicht, ob es zwei Zeilen oder zwei
                   # Megabyte sind, und das ist der Unterschied zwischen
                   # "gleich zu Ende gelesen" und "hier steht ein Bruchteil".
                   "cut": cut if cut > 0 else 0})

    def boundary_escaped(self, name: str, refused: list) -> None:
        """#98, and it is drawn in `auto`'s own colour rather than as a note.

        The window is where this matters most: the terminal at least shows the
        shell line, while here the tool row says `run_command` and the argument
        column is one truncated string. Without this the only account of the
        working area being left is the model's own closing sentence.
        """
        for path in refused or []:
            self._put({"k": "alarm",
                       "t": "! the working area was refused for %s, "
                            "and %s ran anyway" % (path, name)})
        self._put({"k": "note",
                   "t": "write_file and edit_file stay inside the root; "
                        "an outside path named in run_command asks first (#144) -- "
                        "this one was released, or not named plainly"})

    def tools_reported(self, calls: list) -> None:
        for call in calls or []:
            self.tool_started(call.get("name", "?"), call.get("arguments", ""))

    def rolled_over(self, tokens: int, path: str) -> None:
        self._put({"k": "note", "t": "rolled over at %d tokens -> %s"
                                     % (tokens, os.path.basename(path))})
        # robins Live-Test 2026-08-29: unten links stand bis zum Turn-Ende der
        # Fuellstand von VOR dem Roll. Der Kern zaehlt ab dem Roll von 0, also
        # geht dieselbe Zahl sofort an die Seite -- als EIGENES Ereignis, nicht
        # als `cost`: cost() raeumt den Stream-Cursor ab, und der Turn laeuft
        # noch. Das Turn-Ende schreibt danach den echten neuen Fuellstand.
        self._put({"k": "ctx", "tokens": 0, "n_ctx": 0})

    def rollover_refused(self) -> None:
        # #152: die Verweigerung war ein No-op der Basisklasse -- der Turn
        # endete wortlos, und robin sah nur Zuege, die "einfach aufhoerten".
        # Ehrlich rot, mit Grund.
        self._put({"k": "fail",
                   "t": "rollover already spent this turn -- "
                        "stopping before the context wall"})

    def memory_saved(self, what: list) -> None:
        """#122. Its OWN kind, not a `note`, and the page draws it with a glow.

        A note is grey and easy to skim past, which is right for the things
        notes say. This one is the only sign a person gets that something has
        entered the head of their next session, so it has to catch the eye once
        and then stay out of the way -- a gradient that runs and settles, not a
        colour that keeps shouting.

        IT IS A MESSAGE, NOT A TURN. `_put` queues it for the page; nothing
        appends it to the conversation. A line that slipped into the history
        would move the head of the next prompt and cost the full prefill this
        whole feature is built to avoid.
        """
        self._put({"k": "memory", "t": "Memory updated",
                   "n": len(what or [])})

    def memory_pending(self, what: list) -> None:
        """#128. The gate held the review's writes. Nothing is on disk yet.

        NOT A ROW IN THE CHAT, and that is the difference from `memory_saved`.
        A saved note happened at a point in the conversation, so it belongs in
        the conversation. A held-back note is a CONDITION that is still true --
        it has no place in the transcript, it has a place behind the composer,
        and it stays there until it is answered or expires.
        """
        self._put({"k": "pend", "items": crow_core.pending_view()})


class Api:
    """What the page may call. Nothing here touches a widget; it queues."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        # #92: `--mode` parses to None when nobody typed it, because that is the
        # only way to tell a silence from a typed `auto`. NOTHING ELSE MAY SEE
        # THE None: `_mode_command` prints it, `run_turn` decides with it, and a
        # None reaching either is a crash rather than a default. So the silence
        # is recorded here as a flag and the field is filled immediately;
        # `ready()` passes the flag on to `adopt_root`, which is the one place
        # allowed to let a directory answer for a silent user.
        self._mode_stated = getattr(args, "mode", None) is not None
        if not self._mode_stated:
            args.mode = DEFAULT_MODE
        # UNDERSCORED ON PURPOSE. pywebview walks the public attributes of the
        # js_api object to expose them to the page; a window object among them
        # is walked too, and `window.native.AccessibilityObject.Bounds.Empty…`
        # recurses until the stack gives out. Private names are skipped.
        self._window = None
        self._out: "queue.Queue" = queue.Queue()
        # #135. THE WINDOW IS WHERE A SERVER'S QUESTION LANDS. Installed once,
        # here, because `crow_core` reads the name at call time -- and read from
        # the MCP thread, which is why the plug only queues.
        crow_core.ELICIT_ANNOUNCE = self.announce_elicit
        self._conversation = Conversation(args.system)
        self._context_tokens = 0
        self._n_ctx = 0
        # #142. Images dropped and not yet sent. They ride the NEXT send: the
        # page shows a chip per entry, and `_run` folds them into the user
        # message through `user_content`. Held here and not in the page because
        # the page never sees the path -- and consumed on send, accepted or
        # refused, so a stale image cannot ride a later, unrelated line.
        self._staged_images: list = []
        # What /props last said the server has open. Kept beside _n_ctx and for
        # the same reason: both are answers from the endpoint that the session
        # path needs later, and asking twice would be two answers to one
        # question. Empty until _probe has run -- #113 treats that as "unknown",
        # which drops a cache rather than restoring the wrong one.
        self._model = ""
        # #131. HOW MANY TOOL ROWS THIS CHAT HAS HAD DISMISSED. A view fact, not
        # a conversation fact: the model keeps every call it made.
        self._tools_cleared = 0
        # #116. The chat's thinking level, and `None` is a value: "never chosen",
        # which sends no `reasoning_effort` at all and keeps the prompt
        # byte-identical to a window that predates the slider. Bound from the
        # session file once the model is known, because which levels are legal
        # is the model's answer, not this window's.
        self._reasoning: str | None = None
        self._promised_warm = False
        self._worker: threading.Thread | None = None
        # #138c. EINE ZEILE, DIE WAEHREND DES NACHLAUFS GETIPPT WURDE.
        #
        # `_busy` IST NICHT `_worker.is_alive()`, und der Unterschied ist der
        # Grund, dass es dieses Feld gibt: ein Thread lebt nach seinem `return`
        # noch einen Moment, und in genau diesem Moment landete eine Zeile in
        # einem Puffer, den niemand mehr liest. Der Worker loescht dieses Flag
        # UNTER demselben Lock, unter dem `send` puffert.
        #
        # DIE ANDEREN PRUEFER BLEIBEN BEI `is_alive`. Sie fragen "darf ich das
        # jetzt tun" -- Modell wechseln, Ordner wechseln -- und waehrend eines
        # Nachlaufs lautet die Antwort weiter nein. Nur das Senden ist anders:
        # eine Zeile kann warten, ein Modellwechsel nicht.
        self._busy = False
        self._queued: str | None = None
        self._queue_lock = threading.Lock()
        # #143 E2. WHICH CHAT SPAWNED WHICH SUBTASK, recorded the first time an
        # ident is seen -- during the spawning turn, so the chat live at that
        # moment is the parent. "" is the live chat without a file. And the last
        # snapshot signature, so the ticker pushes on change instead of forever.
        self._sub_parent: dict = {}
        self._subs_sig = "[]"
        # ONE INSTALLER AT A TIME. Two of them writing the same
        # directory is the one way an update leaves a broken copy.
        self._updating = False
        # #88: one open question at a time. The worker waits on the Event, the
        # page's click sets it. Not a Queue -- there is never a second question
        # in flight, because the loop that asks is the one that blocks.
        self._asked = threading.Event()
        self._answer = "no"
        self._restore: tuple | None = None
        # WHICH FILE THE OPEN CHAT ALREADY HAS, or None while it has none.
        #
        # A CHAT GETS ITS FILE WHEN IT IS LEFT, NOT WHEN IT IS OPENED. Writing
        # one eagerly is what put a copy of the restored session in the rail on
        # every single launch: five starts, five identical entries under
        # "Earlier", and deleting them only meant the next launch wrote another.
        # When the value IS set, the chat came out of that file and leaving it
        # UPDATES the file rather than writing a second one.
        self._current_path: str | None = None
        # THE NAME LIVES HERE, NOT ONLY IN A FILE. `save_session` serialises six
        # keys and drops the rest, so a `crow_title` written into a file lasted
        # exactly until the core wrote that file again -- the renamed chat then
        # turned up under "Earlier" labelled with its first line. Held in the
        # object, the name survives every file the chat is written to.
        self._current_title: str | None = None
        # #101, SECOND ATTEMPT. Whether the bound root was CHOSEN for this chat
        # or merely BORROWED from the template. The first version wrote whatever
        # was bound into the chat's file, so a chat that had never chosen took
        # the template once and then owned it forever -- measured the same day:
        # a chat from before this ticket ended up permanently holding a folder
        # picked in a different chat.
        #
        # The rule was already written down two screens above, for the name:
        # "that guess must never be stamped back into the file as though it had
        # been chosen, because from then on it would outrank the real opening
        # line forever". A borrowed root is that same guess.
        self._root_chosen: bool = False

    # -- outward -----------------------------------------------------------

    def push(self, message: dict) -> None:
        self._out.put(message)

    def announce_elicit(self, asks: list) -> None:
        """The core says a server is asking; the page gets the newest question.

        THE PLUG, AND THERE IS ONLY ONE. `crow_core` owns the staging, the
        waiting, the schema check and the answer -- this says "now", because a
        window has to push and a terminal has to prompt in line.
        """
        if asks:
            self.push({"k": "elicit", "ask": asks[-1]})

    def answer_elicit(self, ident: int, action: str, values) -> str:
        """What a person typed, handed back to the waiting server. "" when it
        went through; the sentence why, when it did not."""
        return crow_core.answer_elicitation(int(ident), str(action), values) or ""

    def pump(self) -> None:
        """One thread, forever: queue -> page. The only place JS is called."""
        while True:
            message = self._out.get()
            if message is None:
                return
            try:
                self._window.evaluate_js(
                    "window.crow.on(%s)" % json.dumps(json.dumps(message)))
            except Exception:              # noqa: BLE001 - a closed window, nothing else
                return

    # -- inward ------------------------------------------------------------

    def tools_cleared(self) -> int:
        """The page emptied the tool tile. Remember how far, and say so.

        COUNTED HERE AND NOT IN THE PAGE, because the page only holds what it
        was shown -- clearing twice would otherwise set the watermark to the
        SECOND batch and bring the first one back. The conversation is the only
        place that knows how many calls there have been.
        """
        self._tools_cleared = sum(
            len(m.get("tool_calls") or [])
            for m in self._conversation.payload()
            if m.get("role") == "assistant")
        return self._tools_cleared

    def ready(self) -> None:
        self.push({"k": "meta", "rail": rail_width_setting(),
                   "version": client_version() or "",
                   "url": self._args.base_url, "tools": len(TOOLS),
                   "execute": bool(self._args.execute_tools)})
        threading.Thread(target=self._mic_probe, daemon=True).start()
        # #88: the level and its menu, in the same breath as the rest of the
        # header. The button has to show what is live before the first turn --
        # a release level nobody can see is one nobody can trust.
        self.push({"k": "mode", "name": getattr(self._args, "mode", DEFAULT_MODE),
                   "modes": self.mode_menu()})
        # #92: which directory this window may write in, before the first turn
        # for the same reason the level is -- a boundary nobody can see is one
        # nobody can trust, and its ABSENCE is the state that has to be visible.
        #
        # BOUND HERE, NOT ONLY DRAWN. The window has no `--root` and its cwd is
        # whatever the shortcut handed it, so without this call it started
        # unbounded every single time and the folder picked yesterday was gone.
        # `adopt_root` is the same rule the terminal uses; the window simply has
        # nothing to state, so it takes the remembered one.
        _, mode, problem = crow_core.adopt_root(
            getattr(self._args, "root", None),
            self._args.mode if self._mode_stated else None,
            walk_up=False)
        if problem:
            self.push({"k": "fail", "t": problem})
        self._args.mode = mode
        self.push({"k": "mode", "name": mode, "modes": self.mode_menu()})
        self.push_root()
        threading.Thread(target=self._probe, daemon=True).start()

    # ---- #92: the working directory ------------------------------------

    def push_root(self) -> None:
        root = crow_core.get_root()
        self.push({"k": "root",
                   "path": root or "",
                   "name": os.path.basename(root) if root else "",
                   "roots": [{"path": p, "name": os.path.basename(p) or p}
                             for p in crow_core.known_roots()]})

    def _bind_root(self, path: str, mode: str | None = None) -> None:
        """Declare `path` a root, remember it, and adopt the level stored there.

        THE LEVEL FOLLOWS THE ROOT (robin, #92): opening a directory restores
        what it was last allowed to do. `_args.mode` is the same field
        `set_mode` writes, so the two ways of changing the level end in one
        place rather than two -- the divergence #90 exists to prevent.
        """
        stored = crow_core.read_root_mode(path)
        wanted = mode or stored or getattr(self._args, "mode", DEFAULT_MODE)
        crow_core.write_root_mode(path, wanted)
        crow_core.set_root(path)
        self._root_chosen = True                 # #101: a person picked, for THIS chat
        crow_core.remember_root(path)
        # #92: AND THIS IS WHERE THE NEXT START READS FROM. `remember_root` fills
        # the menu, which is a different fact -- the terminal writes that list too,
        # and letting it decide where the window opens tomorrow was the coupling
        # `active` was added to avoid.
        crow_core.set_active_root(path)
        if wanted != getattr(self._args, "mode", DEFAULT_MODE):
            self._args.mode = wanted
            crow_core.forget_approvals()
            self.push({"k": "mode", "name": wanted, "modes": self.mode_menu()})
        # #119: AND THE CHAT'S OWN FILE IS TOLD, HERE, rather than at the next
        # save. `_root_chosen` above is what makes `_stamp` write `crow_root`,
        # and until this line the two were separated by however long it took the
        # user to type again -- the boundary lived in memory while the file said
        # nothing. It cost nothing while a chat's directory was only ever read
        # back at start-up. It costs the moment anything reads the FILE to
        # decide where the chat belongs: the rail is drawn from `_entry_of`, so
        # moving the open chat into a project bound the boundary, printed the
        # note, and left the row exactly where it was. A window whose screen
        # disagrees with its own disk is the hardest state to report, because
        # each half looks right on its own.
        #
        # ALL THREE DOORS, not just the project move: the folder chip and the
        # picker adopt a boundary the same way and had the same gap.
        if self._current_path:
            self._stamp(self._current_path)
        # #121: AND THE MEMORY FOLLOWS THE BOUNDARY, through this one door with
        # the rest. Binding a folder to an open chat is the user saying which
        # project this conversation is about; leaving it on the old project's
        # notes until the next chat would be a rule nobody asked for.
        #
        # THE COST IS SAID BEFORE THE CHANGE, never after -- `REASONING_COST_NOTE`
        # sets that shape for the level and the mechanism here is identical: the
        # head moves, so the next turn is a full prefill. A bind that changes no
        # memory says nothing, which is what the return value is for.
        if self._conversation.memory is not None:
            if self._conversation.repin_memory(crow_core.prompt_head()):
                self.push({"k": "note", "t": crow_core.MEMORY_COST_NOTE})
        self.push_root()
        # #119: AND THE LIST IS REDRAWN, because the boundary is now WHERE a chat
        # is drawn and not merely what it may write. This door bound the root,
        # printed the note and stopped -- so a chat moved into a project stayed
        # in place until something unrelated reloaded the rail, which is how
        # robin found it: folding the project was what finally showed the move.
        self._reload_rail()
        self.push({"k": "note", "t": "working directory: %s (%s)" % (path, wanted)})

    def _pin_memory(self, chat: str | None) -> None:
        """Pin this chat's memory head ONCE, after its boundary is known.

        THE ORDER IS THE WHOLE CONTRACT. A pin taken before `_adopt_chat_root`
        would be the template's memory, not the chat's, and it would be wrong
        for exactly the chats that have a project. Every caller here sits below
        the line that binds.

        A FILE THAT CARRIES A PIN WINS OVER THE FOLDER. That is #121: what this
        chat was sent last time is what it is sent again, so the KV cache saved
        against it still fits. Only a chat with no pin -- every chat written
        before this build, and every new one -- takes a fresh block.
        """
        if self._conversation.memory is not None:
            return
        pinned = crow_core.session_memory(chat) if chat else None
        self._conversation.pin_memory(
            pinned if pinned is not None else crow_core.prompt_head())
        # #122. THE REVIEW MARKS COME BACK WITH THE CHAT. Without this a
        # conversation reopened at 80% would be reviewed at 0.50 and 0.75 all
        # over again -- twice per OPENING instead of twice per window.
        if chat:
            self._conversation.mark_reviewed(crow_core.session_reviewed(chat))

    def _adopt_chat_root(self, chat: str | None, fresh: bool = False) -> None:
        """Bind the boundary THIS chat chose, and take the level that goes with it.

        #101. One place, because three events needed the same answer: opening
        another chat, starting a new one, and restoring the live one at launch.
        Three copies of it would drift the first time one of them was edited, and
        the symptom would be a boundary that depends on how you got here.

        #119: TWO OF THE THREE STILL DO. `fresh` is the new chat, and it binds
        NOTHING -- robin's rule once the rail was grouped by the boundary, because
        `active` is rewritten by every bind and the template therefore carried the
        last project into every chat started after it. It stays one place: the
        difference is a parameter, not a second copy.

        A chat that never chose falls back to the template in `roots.json` --
        which is what a NEW chat is, and what every file written before this
        ticket looks like. Never to "whatever happened to be bound", which is the
        defect this ticket exists to remove.

        THE LEVEL FOLLOWS THE FOLDER, NOT THE CHAT (robin's rule, #101): it is a
        statement about the project, so two chats in one folder share it. Put it
        in the chat and the same directory has different rights depending on
        which conversation is open.
        """
        root, chosen = self._stored_root(chat) if chat else (None, False)
        # UNBOUND, AND NOT CHOSEN TO BE. `chosen` stays False so `_stamp` writes
        # no `crow_root` at all: absent means nobody ever picked for this chat,
        # which is what a chat one second old is. An explicit null would be the
        # user's "no folder" and would survive being opened again.
        if not chosen and not fresh:
            root, _ = crow_core.restore_root()
        # BORROWED, AND IT STAYS BORROWED. The template may be shown and worked
        # in; it is not written into the chat until a person picks for this chat.
        self._root_chosen = chosen
        crow_core.set_root(root)
        wanted = (crow_core.read_root_mode(root) if root else None) or DEFAULT_MODE
        if wanted != getattr(self._args, "mode", DEFAULT_MODE):
            self._args.mode = wanted
            crow_core.forget_approvals()
            self.push({"k": "mode", "name": wanted, "modes": self.mode_menu()})
        self.push_root()

    def choose_root(self, path: str) -> None:
        """Switch to a root already on the list. Never mid-turn.

        Refused while a turn runs for the same reason `set_mode` is: `run_turn`
        reads the boundary through the tools as it goes, and moving it underneath
        a running turn would let the first half of a turn write where the second
        half may not.
        """
        if self._worker and self._worker.is_alive():
            self.push({"k": "note", "t": "the working directory does not change mid-turn"})
            return
        if not path or not os.path.isdir(path):
            self.push({"k": "fail", "t": "that directory is gone"})
            self.push_root()
            return
        self._bind_root(path)

    def choose_model(self, key: str) -> None:
        """The chip's rows (#115). Same door as `/model <key>`, deliberately.

        NOT ITS OWN PATH. A menu that switched models by some other route would
        be a second answer to "may I do this right now" and a second sentence
        about the lost context -- and #99 is the precedent for what that costs:
        a command that worked in the terminal and not in the window, for months,
        because the two halves were wired separately.
        """
        said = self._model_command([key] if key else [])
        if said:
            self.push({"k": "note", "t": said})

    def set_reasoning(self, level: str) -> None:
        """The slider (#116). Same door as `/reasoning <level>`, deliberately.

        #99 is the precedent this obeys: a control wired separately from the
        command it duplicates is one that works in one surface and not the
        other, and nothing in the suite can see it. So the slider does not set
        the level -- it types the command.
        """
        said = self._reasoning_command([level] if level else [])
        if said:
            self.push({"k": "note", "t": said})

    def pick_root(self) -> None:
        """The native folder dialog, and the ONLY thing that creates a root.

        `.crow/` appears wherever crow runs -- measured 2026-08-14, the home
        directory had one from a single session in August -- so a root is never
        inferred from the disk. Someone picks it here, and that pick writes
        `root.json`.

        Called from the bridge thread, which is where pywebview expects a dialog
        to be raised. NOT `evaluate_js` from a worker: that one blocks its caller
        and deadlocks on some backends, which is why the queue plus `pump()`
        exists in the first place.
        """
        if self._worker and self._worker.is_alive():
            self.push({"k": "note", "t": "the working directory does not change mid-turn"})
            return
        # IMPORTED HERE, not at module level, and that is the house rule rather
        # than a shortcut: `main()` imports webview the same way, so `crow_gui`
        # can be imported -- by the suite, by `check_shared_core` -- on a machine
        # where the runtime is missing. Writing `webview.` against a module-level
        # name that does not exist raises NameError only when a user clicks, and
        # no green suite would have seen it.
        import webview

        start = crow_core.get_root() or os.getcwd()
        try:
            picked = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=start)
        except Exception:                       # noqa: BLE001 -- a cancelled or
            picked = None                       # unavailable dialog is not a crash
        if not picked:
            return                              # cancelled: nothing changes, no note
        self._bind_root(picked[0] if isinstance(picked, (list, tuple)) else str(picked))

    def create_project(self) -> None:
        """#119. A project is a working directory somebody named by picking it.

        THE SAME DIALOG AND THE SAME WRITER AS `pick_root`, deliberately. A
        project that created its root any other way would be a second answer to
        "what makes a directory a root", and the first answer is the security
        boundary -- `write_root_mode` has been the only thing that creates one
        since 2026-08-14, and `add_project` calls it rather than writing the
        file itself.

        IT DOES NOT BIND. Creating a project puts a row in the rail; it does not
        move the open chat into it and does not touch `active`. Those are two
        decisions and a click that made both would be the one nobody could undo
        by looking at it.
        """
        import webview

        start = crow_core.get_root() or os.getcwd()
        try:
            picked = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=start)
        except Exception:                       # noqa: BLE001 - same as pick_root
            picked = None
        if not picked:
            return                              # cancelled: nothing changes, no note
        path = picked[0] if isinstance(picked, (list, tuple)) else str(picked)
        if not crow_core.add_project(path):
            self.push({"k": "fail", "t": "could not mark %s as a project"
                       % os.path.basename(path)})
            return
        self._reload_rail()

    def drop_project(self, path: str) -> None:
        """Take a project row out of the rail. Chats and directory are untouched.

        THE OFFER EXISTS BECAUSE THE PICK CANNOT BE UNDONE OTHERWISE. A folder
        chosen by mistake would otherwise sit in the rail for good, and the only
        way out would be editing roots.json by hand.

        WHAT IT DOES NOT DO is the important half: the marker stays, so every
        chat bound to that directory keeps its boundary, and the chats
        themselves are not touched at all. They simply stop being drawn under a
        heading. The core says the same thing in `drop_project`.
        """
        if not path:
            return
        crow_core.drop_project(path)
        # THE FOLD STATE GOES WITH THE ROW. Left behind, it would be waiting for
        # a project that no longer exists -- and the same folder added again
        # would come back folded from a life it does not have. `set_project_open`
        # states this as the reason the CLOSED ones are the list; without this
        # the sentence was true of the reader and not of the writer.
        doc = read_settings()
        shut = [p for p in (doc.get("projects_shut") or []) if isinstance(p, str)]
        key = os.path.normcase(path)
        kept = [p for p in shut if os.path.normcase(p) != key]
        if len(kept) != len(shut):
            doc["projects_shut"] = kept
            write_settings(doc)
        self._reload_rail()

    def set_chat_root(self, path: str, root: str) -> None:
        """Move ONE chat into a project, or out of every project with root="".

        NOT `choose_root`, AND THE DIFFERENCE IS WHICH CHAT. `choose_root` binds
        the boundary of the conversation in the window; this writes `crow_root`
        into the file of a chat that is not open, which changes nothing about
        what may be written right now.

        THE OPEN CHAT GOES THROUGH `_bind_root` INSTEAD. Writing its file behind
        its back would be overwritten by the next `_stamp` -- the window holds
        the authoritative copy of the live chat and re-stamps it after every
        save. Two writers on one file, and the loser is whichever wrote first.
        """
        if not path or not os.path.isfile(path):
            self.push({"k": "fail", "t": "that chat is gone"})
            self._reload_rail()
            return
        if root and not os.path.isdir(root):
            self.push({"k": "fail", "t": "that directory is gone"})
            self._reload_rail()
            return
        live = (self._current_path
                and os.path.abspath(path) == os.path.abspath(self._current_path))
        if live:
            if self._worker and self._worker.is_alive():
                self.push({"k": "note",
                           "t": "the working directory does not change mid-turn"})
                return
            # BOTH DOORS RELOAD THE RAIL THEMSELVES NOW, so a third call here
            # would only draw the same list twice.
            if root:
                self._bind_root(root)
            else:
                self.clear_root()
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            # NULL RATHER THAN A MISSING KEY when it leaves a project, the same
            # three states #101 wrote down: absent means nobody ever chose here,
            # null means somebody chose "no folder". Dropping the key would put
            # the chat back into "never chosen", and the next thing that reads
            # it would be free to bind it to the template.
            data["crow_root"] = root or None
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:                  # noqa: BLE001 - reported, never raised
            self.push({"k": "fail", "t": "could not write that chat"})
        self._reload_rail()

    def clear_root(self) -> None:
        """Work without a boundary again -- the state every release up to 0.3.2 had.

        It is offered rather than hidden: a user who cannot turn it off works
        around it instead, and a boundary worked around teaches nothing.
        """
        if self._worker and self._worker.is_alive():
            return
        crow_core.set_root(None)
        self._root_chosen = True                 # #101: "none" is a choice too
        # #92: "NONE" IS A CHOICE AND SURVIVES A RESTART. Written as an explicit
        # null rather than by deleting the key: an absent key means nobody ever
        # chose, and collapsing the two would make this decision evaporate on the
        # next start -- which is how "no folder" would come back as a folder.
        # A cancelled picker never reaches here and still changes nothing.
        crow_core.set_active_root(None)
        # #119: THE OTHER DIRECTION OF `_bind_root`'s LINE, and it has to be here
        # too. `_stamp` writes `crow_root` from `get_root()`, which is None now --
        # an explicit null, which is the state "somebody chose no folder", not the
        # absent key that means nobody ever chose. Without this a chat taken out
        # of a project stays drawn inside it until the next save.
        if self._current_path:
            self._stamp(self._current_path)
        self.push_root()
        self._reload_rail()          # the other direction of the line above
        self.push({"k": "note", "t": "no working directory -- writes are unbounded"})

    ARCHIVE_PREFIX = "chat-"

    def _archives(self) -> list:
        """Every kept conversation, newest first, with something to read.

        BOTH KINDS ARE LISTED. `roll_over` writes `rollover-*.json` when a session
        runs into the context wall; "neu" writes `chat-*.json` when the user puts
        one aside. They are the same file format and the same thing to a reader --
        a conversation that is not the current one -- so they share a list.
        """
        folder = os.path.dirname(SESSION_FILE) or "."
        out = []
        try:
            names = sorted(os.listdir(folder), reverse=True)
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            if not (name.startswith(self.ARCHIVE_PREFIX)
                    or name.startswith("rollover-")):
                continue
            path = os.path.join(folder, name)
            if not os.path.isfile(path):   # the archiv/ folder, and anything like it
                continue
            # THE OPEN ONE STAYS IN THE LIST, MARKED WHERE IT IS. Filtering it
            # out here made a click MOVE the chat out of the list and into the
            # live slot. The duplicate that filter guarded against cannot happen
            # now: the page draws the live slot only for a chat with no file.
            entry = self._entry_of(path, name)
            entry["active"] = bool(
                self._current_path and os.path.abspath(path) == os.path.abspath(
                    self._current_path))
            out.append(entry)
            if len(out) >= 12:
                break
        return out

    TITLE_MAX = 52

    @classmethod
    def _first_line(cls, messages: list | None) -> str | None:
        """The first thing the user said, which is what they will recognise.

        A file name is a timestamp, and nobody remembers which conversation
        happened at 07:29. The opening line is the only label a chat carries
        before anyone names it -- and it is read the same way whether the
        conversation is on disk or still in the window.
        """
        # #153: die Rollover-Note ist die erste User-Zeile jeder Fortsetzung
        # und jedes Folge-Archivs -- als Titel gelesen sieht das Archiv aus
        # wie "die ganze Session", direkt neben dem offenen Chat. Das Praefix
        # kommt aus dem Kern-Template, keine zweite Kopie des Wortlauts.
        note = crow_core.ROLLOVER_NOTE.split("{", 1)[0]
        for message in messages or []:
            if message.get("role") == "user":
                # #142: blocks title by their words, like everywhere else.
                first = crow_core.message_text(
                    message.get("content") or "").strip().splitlines()
                if first and first[0]:
                    if first[0].startswith(note):
                        continue
                    return first[0][:cls.TITLE_MAX]
        return None

    @classmethod
    def _stored_title(cls, path: str) -> str | None:
        """The name the USER gave a chat, or None when they never gave one.

        SEPARATE FROM THE LABEL ON PURPOSE. `_entry_of` always returns
        something to draw, falling back to the opening line; that guess must
        never be stamped back into the file as though it had been chosen,
        because from then on it would outrank the real opening line forever.
        """
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:                  # noqa: BLE001 - no name, not an error
            return None
        return (data.get("crow_title") or "").strip()[:cls.TITLE_MAX] or None

    @staticmethod
    def _stored_root(path: str) -> "tuple[str | None, bool]":
        """A chat's own working directory: `(root, chosen)`.

        THREE STATES, LIKE `active` IN roots.json AND FOR THE SAME REASON (#101):

          key absent   -> `(None, False)`  nobody ever chose for this chat. Every
                          file written before this ticket is in this state, so it
                          must NOT read as "no folder" -- that would silently
                          unbind the boundary for every existing chat on update.
                          The caller falls back to the template.
          null         -> `(None, True)`   "no folder" was chosen here, and that
                          choice is the chat's own. It outlives the switch.
          a path       -> `(path, True)`   bind it, if it still declares itself.

        A stored root whose `root.json` is gone answers `(None, True)`: the chat
        chose, and what it chose is not there any more. Falling back to the
        template would put the chat somewhere nobody picked for it.
        """
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:                  # noqa: BLE001 - no chat, no choice
            return (None, False)
        if "crow_root" not in data:
            return (None, False)
        root = data["crow_root"]
        if not isinstance(root, str) or not os.path.isfile(crow_core.root_file(root)):
            return (None, True)
        return (root, True)

    @classmethod
    def _entry_of(cls, path: str, name: str) -> dict:
        """One rail entry, from ONE read of the file.

        The title and the meta line used to open the file separately, so a rail
        of twelve chats cost twenty-four reads to draw -- and drew its two
        halves from two different snapshots, which a write landing in between
        could pull apart.

        A NAME THE USER TYPED WINS OVER THE ONE WE GUESSED. `crow_title` is
        Crow's own key; the core ignores what it does not know, so the file
        stays a session file that both clients can open.
        """
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:                  # noqa: BLE001 - the file name still works
            return {"path": path, "title": name, "meta": ""}
        messages = data.get("messages") or []
        given = (data.get("crow_title") or "").strip()[:cls.TITLE_MAX]
        kind = "rolled over" if name.startswith("rollover-") else "put aside"
        return {"path": path,
                "title": given or cls._first_line(messages) or name,
                # #119. THE CHAT'S OWN BOUNDARY IS ITS PROJECT MEMBERSHIP, and it
                # is read here because this is the one read of the file. There is
                # no `crow_project` key and there must not be: a label beside the
                # directory is a second place for the same fact, and the two
                # would part company the first time either was written alone.
                "root": data.get("crow_root") or "",
                "meta": ("%d messages · %s" % (len(messages), kind)
                         if messages else kind)}

    def _endpoint(self) -> dict:
        """Where a turn goes. ONE resolution, read by every path that sends.

        The command line is the LOCAL provider's default and reaches nothing
        else: a remote endpoint's URL and key come off disk, never from an
        argument somebody typed months ago.
        """
        return crow_core.provider_endpoint(self._args.base_url, self._args.model,
                                           self._args.api_key)

    def _look(self, spot: dict) -> tuple:
        """(state, model, window) for whatever endpoint is chosen.

        THE THREE LOCAL QUESTIONS ARE NOT PUT TO A REMOTE ENDPOINT. /health and
        /props belong to llama-server; asking a provider for them costs a round
        trip to learn nothing, and answering out of them would put a MEASURED
        number where only a declared one exists. What a remote endpoint's state
        actually is -- a key, a slug -- is on disk, so it is read there.
        """
        if not spot["remote"]:
            return (check_endpoint(spot["base_url"]),
                    model_display_name(fetch_model_name(spot["base_url"])),
                    fetch_n_ctx(spot["base_url"]))
        if not spot["model"]:
            return ("no model picked", spot["label"], 0)
        return ("ready", spot["model"],
                crow_core.provider_context(spot["provider"], spot["model"]))

    def _probe(self) -> None:
        spot = self._endpoint()
        try:
            state, name, window = self._look(spot)
            self._model = name
            self._n_ctx = window
            # #116. BOUND HERE BECAUSE THIS IS WHERE THE MODEL BECOMES KNOWN,
            # and which levels are legal is the model's answer. A level the new
            # model does not take comes back as None with a line, and is left in
            # the file untouched -- the user may go back to the model it was
            # valid for.
            #
            # NOT ASKED OF A REMOTE SLUG. The levels come from a manifest of
            # models this machine can boot, and `z-ai/glm-5.2:free` is not one of
            # them -- a lookup there would answer about a model nobody is running.
            if spot["remote"]:
                self._reasoning, note = None, ""
            else:
                self._reasoning, note = crow_core.reasoning_for_chat(name, SESSION_FILE)
            if note:
                self.push({"k": "note", "t": note})
            self.push({"k": "up", "model": name, "n_ctx": self._n_ctx,
                       "tokens": self._context_tokens, "state": state,
                       # #115: the chip's list travels with the probe that
                       # learned the name, so the two can never disagree about
                       # which model is the running one.
                       # [key, label] -- the key is what the menu sends back,
                       # the label is what a person recognises. See model_label.
                       "models": [[k, crow_core.model_label(k)]
                                  for k in crow_core.bootable_models()],
                       "model_key": "" if spot["remote"] else crow_core.model_key_for(name),
                       "reasoning": self._reasoning or "",
                       "levels": [] if spot["remote"] else list(crow_core.reasoning_levels_for(name)),
                       # #117. Which of those levels render the SAME prompt, measured. Empty
                       # means unmeasured, and the page collapses nothing on an empty list.
                       "groups": [] if spot["remote"] else
                                 [list(g) for g in crow_core.reasoning_groups_for(name)]})
        except Exception as exc:           # noqa: BLE001 - shown, never raised
            self.push({"k": "down", "why": str(exc)[:120]})
            return
        if not self._args.session:
            return
        try:
            # #121. The pin is read before the payload -- see the same two lines
            # in `crow.py`. A file without one composes to what every release up
            # to here sent, so no existing cache is disturbed by this.
            restored = load_session(
                spot["base_url"],
                crow_core.system_with_memory(self._args.system,
                                             crow_core.session_memory(SESSION_FILE)),
                model=self._model, with_kv=not spot["remote"])
        except Exception as exc:           # noqa: BLE001
            self.push({"k": "note", "t": "session not readable: %s" % exc})
            return
        # THE IDENTITY IS READ BACK, NOT MINTED. It used to be archived here
        # instead, which handed the restored session a file it did not need --
        # and another one on the next launch, until the same conversation stood
        # in the rail as many times as the window had been opened. session.json
        # is where the two things worth remembering about the open chat are kept:
        # which archive file it belongs to, if it has one yet, and what the user
        # named it.
        #
        # READ BEFORE THE MESSAGES ARE, and #100 is why the order matters: a chat
        # named before its first turn has a session.json carrying the name and no
        # messages, so `load_session` correctly answers "no session" -- and while
        # this line sat below that early return, the name was never read back.
        # Identity and content are two questions, and only one of them depends on
        # the conversation being non-empty.
        self._current_path, self._current_title = self._pointer()
        if not restored:
            # #119: THE ONE CALLER WITH NO CLEAR TO HANG ON. A launch that finds
            # nothing to restore leaves an empty flow that was never emptied --
            # which is why the greeting is its own message and not a field.
            self._pin_memory(SESSION_FILE)
            self._hello()
            self._reload_rail()
            return
        messages, tokens, kv = restored
        self._tools_cleared = crow_core.session_tools_cleared()
        self._conversation.restore(messages)
        self._context_tokens, self._promised_warm = tokens, kv
        # An empty restored chat is still an empty chat; `turn()` takes the line
        # back off the moment `_replay` puts a row in.
        self._hello()
        self._replay(messages)
        self._reload_rail()
        # #101: THE RESTORED CHAT BRINGS ITS OWN BOUNDARY, and this is the second
        # of the two bindings a launch does. `ready()` has already bound the
        # template from `roots.json`, so the window is never unbounded while this
        # thread waits on the endpoint; here the chat's own choice replaces it and
        # the button is corrected. A visible correction beats an invisible gap.
        #
        # The line this replaces claimed `load_session` "may have bound the root
        # the restored chat was working in". It never did -- nothing outside
        # `adopt_root` and the picker has ever called `set_root`.
        self._adopt_chat_root(SESSION_FILE)
        # #121. AFTER THE BOUNDARY, NEVER BEFORE IT. A chat with no pin yet is
        # pinned from the folder it stands in, and the line above is where that
        # folder stops being the template and becomes the chat's own.
        self._pin_memory(SESSION_FILE)
        self.push({"k": "up", "model": None, "n_ctx": self._n_ctx,
                   "tokens": self._context_tokens})

    @staticmethod
    def tools_listing() -> str:
        """What the model can call, derived from TOOLS rather than written here.

        THE SAME SOURCE THE TERMINAL USES. `crow.py`'s `format_tools` builds its
        listing out of the same list; a second one typed by hand would drift the
        first time a tool is added, and the window would name something the
        model does not have.
        """
        lines = []
        for entry in TOOLS:
            spec = entry.get("function") or {}
            head = (spec.get("description") or "").strip().split(". ")[0].rstrip(".")
            lines.append("  %-14s %s" % (spec.get("name", "?"), head))
        return "the model can call:\n" + "\n".join(lines)

    # #94. THE WINDOW RUNS THE COMMAND. The first attempt did not, and both
    # ways it failed were found by robin in the window inside a minute.
    #
    # It answered each one with a sentence naming the control that does the same
    # job -- "/reset: that is the new button, top left of the chat rail".
    #
    #   1. A POINTER IS PROSE ABOUT PIXELS, AND PROSE ABOUT PIXELS CANNOT BE
    #      TESTED. That one was wrong: `margin-left:auto` puts the button on the
    #      RIGHT. The case meant to catch a lying pointer only asserted that
    #      `id="new"` appears somewhere in the page, so it could never have
    #      caught it -- green, and worthless, in the exact shape its own
    #      docstring warned about.
    #   2. WORSE, IT NAMED THE WRONG CONTROL. `/reset` in the terminal drops the
    #      context and keeps the chat where it is. The `new` button ARCHIVES the
    #      conversation into the rail and opens an empty one. Two different
    #      operations, and the answer asserted they were one.
    #
    # Running the command has neither failure mode: no prose to be wrong about,
    # and no mapping to get wrong. What each one does here is what the same word
    # does in `crow.py`'s `run_slash`, which is the only definition either
    # surface gets to have.
    WHAT_THEY_DO = {
        "/help": "this list.",
        "/tools": "what the model can call.",
        "/mcp": "the tool servers; /mcp fetch|use|drop <server> to change them.",
        "/mode": "the release level; /mode manual|allowedit|auto to switch.",
        "/model": "the model that is up; /model <key> restarts on another one.",
        "/reasoning": "this chat's thinking level; /reasoning <level>|off to set it.",
        "/thoughts": "fold the reasoning blocks open, or closed again.",
        "/image": "hold an image for the next line; /image <path>, or drop one.",
        "/delegate": "hand a task to the remote subtask model; /delegate <task>. "
                     "The local turn keeps running.",
        "/subtasks": "where every delegated subtask stands.",
        "/verify": "delegate this conversation's changes to the checker spot; "
                   "collect fetches the verdict.",
        "/reset": "drop the context. The chat stays where it is.",
        "/context": "how much of the window the conversation is using.",
        "/exit": "close the window.",
        "/quit": "close the window.",
    }

    def help_listing(self) -> str:
        """The window's own list. NOT crow.py's HELP, which promises a terminal.

        Built from `crow_core.SLASH_COMMANDS` rather than from this class's own
        keys, so a command added to the shared list and forgotten here shows up
        as a gap in the help the user is reading rather than as silence.
        """
        width = max(len(c) for c in crow_core.SLASH_COMMANDS)
        return "\n".join(
            "  %-*s %s" % (width, command,
                           self.WHAT_THEY_DO.get(
                               command, "— nothing here answers this yet."))
            for command in crow_core.SLASH_COMMANDS)

    def slash_answer(self, text: str) -> str | None:
        """Run a slash command and return the line to show, or None to send on.

        NONE IS A REAL ANSWER AND THE REASON THIS IS NOT A PREFIX TEST. A user
        asking the model about `/usr/bin/env` opens their message with a slash,
        and a window that swallows everything shaped like a command has taken a
        question away from the thing that could answer it. Only the names on the
        shared list are ours.

        The first word decides, so `/mode manual` is one command with an
        argument rather than a sentence for the model.
        """
        stripped = text.strip()
        if not stripped:
            return None
        parts = stripped.split()
        word = parts[0].lower()
        if word not in crow_core.SLASH_COMMANDS:
            return None
        if word == "/tools":
            return self.tools_listing()
        if word == "/mcp":
            return crow_core.mcp_command(parts[1:])
        if word == "/help":
            return self.help_listing()
        if word == "/reset":
            return self._drop_context()
        if word == "/context":
            return self._context_line()
        if word == "/mode":
            return self._mode_command(parts[1:])
        if word == "/model":
            return self._model_command(parts[1:])
        if word == "/reasoning":
            return self._reasoning_command(parts[1:])
        if word == "/thoughts":
            return self._fold_thoughts()
        if word == "/image":
            # THE REST OF THE LINE, NOT parts[1]: a Windows path with a space
            # is the normal case, and split() has already cut it in two.
            return self._image_command(stripped[len("/image"):].strip())
        # #143 E3. Answered BEFORE the busy buffer by construction -- slash
        # commands run ahead of it in send() -- which is the whole feature:
        # the user starts a second session while the local turn is running.
        if word == "/delegate":
            return self._delegate_command(stripped[len("/delegate"):].strip())
        if word == "/subtasks":
            return crow_core.tool_subtasks()
        # #149. Same answer as the terminal's, word for word; the watcher
        # afterwards keeps the card breathing outside a turn, like /delegate's.
        if word == "/verify":
            answer = crow_core.verify_start(self._conversation)
            self._sub_watch()
            return answer
        self.close()          # /exit, /quit
        return "closing."

    def _delegate_command(self, task: str) -> str:
        """`/delegate <task>`: the user's own fan-out, no model in the loop.

        THE ANSWER IS THE TOOL'S, word for word -- terminal and window may not
        describe one delegation differently. The watcher afterwards is what
        draws the card: outside a turn no ticker is running, and a card nobody
        updates would freeze on "running" forever.
        """
        if not task:
            return "what should it do? /delegate <task>"
        answer = crow_core.tool_delegate(task=task)
        self._sub_watch()
        return answer

    def _sub_watch(self) -> None:
        """An idle-time ticker: keeps the cards breathing when a delegation
        runs OUTSIDE a turn. During a turn `_pump`'s own ticker is already
        up -- and it keeps running past the turn while anything is out -- so
        this starts nothing beside it; the signature guard would swallow
        duplicate pushes anyway, this just spares the thread."""
        if self._busy:
            return
        self._push_subs()
        if not crow_core.subtasks_running():
            return

        def watch() -> None:
            while crow_core.subtasks_running():
                self._push_subs()
                time.sleep(0.8)
            self._push_subs()

        threading.Thread(target=watch, daemon=True).start()

    def _image_command(self, rest: str) -> str:
        """`/image <path>` stages like a drop does; bare `/image` says what is
        held. The window's own way in stays the drop -- this exists so the
        SHARED command answers in both surfaces (#99 is the case where one
        surface was forgotten)."""
        rest = rest.strip().strip('"')
        if not rest:
            chips = self._image_chips()
            if not chips:
                return "no image staged -- drop one into the window, or /image <path>"
            return "staged: " + ", ".join(c["name"] for c in chips) + \
                   " -- sends with the next line"
        before = len(self._staged_images)
        self.stage_image(rest)
        if len(self._staged_images) == before:
            return ""          # refused; stage_image already pushed the note
        return "staged %s -- sends with the next line" % os.path.basename(rest)

    # -- what each one actually does ----------------------------------------

    def _drop_context(self) -> str:
        """`/reset`: the TERMINAL's meaning, which is not the `new` button's.

        `run_slash` does `conversation.reset()` and `forget_approvals()` and
        says the next turn pays a full prefill. The chat is NOT archived and
        does NOT leave the rail -- that is what `new` is for, and conflating the
        two is the defect this method exists to correct.
        """
        if self._worker and self._worker.is_alive():
            return "the context does not change mid-turn"
        self._conversation.reset()
        crow_core.forget_approvals()   # #88: the chat goes, its releases go
        self._tools_cleared = 0        # #131: and so do the dismissed rows
        self._context_tokens = 0
        self._promised_warm = False
        # AND IT LETS GO OF THE FILE THE CHAT CAME FROM. A conversation opened
        # out of the rail keeps `_current_path`, and on the way out `_archive()`
        # writes the open conversation THERE -- except `save_session` refuses an
        # empty one, so the file kept its old messages and the next start found
        # them again. robin, 2026-08-14: "/reset in einem EARLIER Fenster geht
        # erst, aber nach Neustart ist der Text samt cache und context wieder
        # da." Reproduced before this line existed.
        #
        # DETACHED, NOT DELETED. `/reset` drops the context; it is not "throw my
        # saved chat away", and a command that quietly did would be the worst
        # kind of surprise. The chat stays in the rail with everything in it --
        # this window simply stops being it.
        self._current_path = None
        self._current_title = None
        # AND THE LIVE FILE GOES, because `save_session` will not write an empty
        # conversation over it: leaving that to the way out means the file from
        # before the reset survives. See `forget_session`.
        if self._args.session:
            crow_core.forget_session()
        self.push({"k": "clear"})
        self._hello()
        self.push({"k": "up", "model": None, "n_ctx": self._n_ctx, "tokens": 0})
        self._reload_rail()
        return "context dropped -- the next turn pays a full prefill."

    def _context_line(self) -> str:
        """`/context`: the same three figures the terminal prints.

        The rollover point is shown here or nowhere -- it is the number that
        decides when the conversation ends, and the window's bar shows the
        fraction without ever naming the threshold.
        """
        room = ""
        if self._n_ctx > 0 and crow_core.ROLLOVER_AT > 0:
            room = ", rolls over at %d" % int(self._n_ctx * crow_core.ROLLOVER_AT)
        return "%d messages, %d tokens%s" % (
            len(self._conversation), self._context_tokens, room)

    def _mode_command(self, rest: list) -> str:
        """`/mode` reports, `/mode <name>` switches -- through `set_mode`.

        NOT a second implementation: `set_mode` owns the refusal mid-turn and
        the dropping of standing approvals, and a copy of either here would be
        the divergence #90 is about.
        """
        if not rest:
            return "release level: %s" % self._args.mode
        name = rest[0].lower()
        if name not in crow_core.MODES:
            return "no level called %r. There is %s." % (
                rest[0], ", ".join(crow_core.MODES))
        # EMPTY, NOT A SENTENCE. `set_mode` pushes its own note -- the new level
        # and what it holds back -- and it also owns the refusal mid-turn. A
        # line from here as well put both on screen, which is the same defect as
        # the doubled echo one commit ago: a half that speaks without asking
        # what the other half already said.
        self.set_mode(name)
        return ""

    def _model_command(self, rest: list) -> str:
        """`/model` reports, `/model <key>` restarts on the other one (#115).

        NOT A SECOND IMPLEMENTATION. `crow_core.model_command` owns which models
        exist, how a typo is refused and the one sentence about the lost
        context; a copy here would be the divergence #90 is about, and this is
        the command where a divergence costs 17 GB on the card.

        MID-TURN IS REFUSED, like `/reset` and for a stronger reason: the turn
        in flight is streaming from the process this would kill.
        """
        if self._worker and self._worker.is_alive():
            return "the model does not change mid-turn"
        # NAMING A LOCAL MODEL IS COMING BACK TO THE LOCAL PROVIDER, so the
        # choice is written rather than left to disagree with the server that is
        # about to answer. Refusing here instead would leave the chip -- the one
        # control that names the models this machine can run -- dead for as long
        # as a provider is chosen, with no way back except the sheet.
        #
        # ONLY FOR A COMMAND THAT NAMES ONE. A bare `/model` REPORTS and a typo
        # is REFUSED; both come back with `switched` false, and a provider
        # written before the call moved the endpoint out from under the
        # conversation for a word the user never meant -- silently, because the
        # early return below never says anything, never empties the chat and
        # never pushes an `up`. The chip would still have read `1049k` while the
        # turns went to a 200k server. `bootable_models` is READ here, not
        # decided: `model_command` refuses out of the same list.
        wanted = " ".join(rest).strip()
        back = (wanted in crow_core.bootable_models()
                and crow_core.provider_active() != crow_core.LOCAL_PROVIDER)
        if back:
            crow_core.provider_pick(crow_core.LOCAL_PROVIDER)
        said, url, switched = crow_core.model_command(
            wanted, self._args.base_url,
            log=lambda msg: self.push({"k": "note", "t": msg}))
        if not switched:
            # THE SERVER NEEDED NO BOOT AND THE ENDPOINT STILL MOVED. "already
            # the one running" is about the process, not about where the last
            # turn went -- so the context is dropped and said out loud here, the
            # same four things the boot path does below.
            if back:
                self._args.base_url = url
                self._endpoint_changed(reset=True)
            return said
        # THE SAME FOUR THINGS `/reset` DOES, because the cache the context was
        # cheap against belonged to a process that no longer exists. The chat
        # stays in the rail with everything in it; only the context goes.
        self._conversation.reset()
        crow_core.forget_approvals()
        self._tools_cleared = 0        # #131: an empty chat has dismissed nothing
        self._context_tokens = 0
        self._promised_warm = False
        self._args.base_url = url
        # ASKED, NOT ASSUMED. The window size and the name belong to the server
        # that is up NOW; carrying the old ones over would leave the chip naming
        # a model that is gone and `should_roll` measuring against a window that
        # is not there.
        self._model = model_display_name(fetch_model_name(url))
        self._n_ctx = fetch_n_ctx(url)
        self.push({"k": "up", "model": self._model, "n_ctx": self._n_ctx,
                   "tokens": 0, "state": "ok",
                   # THE SAME SHAPE THE PROBE SENDS, and it has to be: the page
                   # keeps ONE `this.models` and the last payload wins, so a
                   # bare list of keys here silently replaced the [key, label]
                   # pairs the probe had put there. `modelMenu` then indexed a
                   # STRING -- x[0] and x[1] became letters -- and the menu drew
                   # `p` and `w`, marked the running model "restarts the
                   # server", and sent "o"/"q" back into choose_model, where
                   # model_command refused them as typos. Two producers, one
                   # consumer, one shape.
                   "models": [[k, crow_core.model_label(k)]
                              for k in crow_core.bootable_models()],
                   "model_key": crow_core.model_key_for(self._model)})
        return said

    def _reasoning_command(self, rest: list) -> str:
        """`/reasoning` reports, `/reasoning <level>|off` binds it (#116).

        NOT A SECOND IMPLEMENTATION: `crow_core.reasoning_command` owns which
        levels exist, how a typo is refused and the sentence about the prefill a
        change costs. The slider goes through this same method for the reason
        #115 gives about the model chip -- a control with its own path is a
        second answer to the same question.

        MID-TURN IS ALLOWED, unlike `/model`. The level is read when the NEXT
        request is built; nothing about the turn in flight changes, and refusing
        would be a rule with no failure behind it.
        """
        said, level, changed = crow_core.reasoning_command(
            " ".join(rest), self._model, self._reasoning)
        if changed:
            self._reasoning = level
            self.push({"k": "reasoning", "level": level or "",
                       "levels": list(crow_core.reasoning_levels_for(self._model)),
                       "groups": [list(g)
                                  for g in crow_core.reasoning_groups_for(self._model)]})
        return said

    def _fold_thoughts(self) -> str:
        """`/thoughts`: the window renders reasoning always, folded.

        So the terminal's show-or-hide becomes open-or-closed here. It is the
        same question -- do I want to read this -- answered in the idiom the
        surface has, rather than a second switch bolted on beside the fold.
        """
        self._thoughts_open = not getattr(self, "_thoughts_open", False)
        self.push({"k": "thoughts", "open": self._thoughts_open})
        return "reasoning blocks %s" % (
            "opened" if self._thoughts_open else "closed")

    def send(self, text: str) -> bool:
        """Take a line from the composer. True if a TURN was started.

        THE RETURN VALUE IS THE POINT, and it is what pywebview's bridge is for:
        every `pywebview.api.*` call resolves a promise once this returns, so
        the page can wait for the one fact only this side knows -- whether there
        is anything to stop. It used to paint "Stop" on the way in and hope.

        NO `user` ECHO FROM HERE. `go()` draws the typed line before it calls
        in. Pushing one from this side put the command on screen TWICE -- wrong
        for `/tools` since the day it was handled here, and wrong for all seven
        after #94. Found by robin in the window, not by the cases that drive
        this Api with no page on the other side.
        """
        # SLASH COMMANDS ARE ANSWERED HERE, NOT BY THE MODEL. Typed into the
        # window they used to travel to the server as an ordinary question --
        # the input's own placeholder offers /tools, so the one it names is
        # answered where it is typed. #94 widened that to all of them.
        answer = self.slash_answer(text)
        if answer is not None:
            # AN EMPTY ANSWER IS "HANDLED, AND ALREADY SAID". `/mode <name>`
            # goes through `set_mode`, which pushes its own note; a second one
            # from here would put the switch on screen twice. None still means
            # "not ours, send it on" -- the two are different answers and the
            # empty string is the one that must not be confused with either.
            if answer:
                self.push({"k": "note", "t": answer})
            return False
        # #138c. EINE ZEILE MITTEN IM ZUG WIRD GEHALTEN, NICHT VERWORFEN.
        #
        # Vorher stand hier `return False`, und das war richtig, solange der
        # Worker nur den Zug fuhr: die Seite haelt selbst eine zweite Zeile
        # zurueck, solange sie `Stop` zeigt. Seit #122 zeigt sie das aber NICHT
        # mehr die ganze Zeit -- `_run` meldet `idle` und faehrt danach den
        # Memory-Nachlauf auf demselben Thread. In diesem Fenster sagt die Seite
        # frei und diese Zeile sagte besetzt, und die Zeile fiel dazwischen.
        #
        # SIE FIEL AUCH NICHT STILL: `go()` malt die getippte Zeile, BEVOR es
        # hier ankommt. robin sah am 2026-08-26 dieselbe Frage zweimal im
        # Verlauf, `Memory updated (2)` dazwischen, und nur die zweite lief.
        #
        # `True` HEISST HIER "ANGENOMMEN", nicht "gestartet". Die Seite laesst
        # ihre Sperre stehen, und das stimmt: es laeuft etwas, und danach laeuft
        # diese Zeile. Der Hinweis darunter sagt, worauf gewartet wird -- ein
        # `Stop` ohne Erklaerung waere ein Knopf fuer einen Nachlauf, den
        # niemand gemeint hat.
        with self._queue_lock:
            if self._busy:
                self._queued = text
                self.push({"k": "queued"})
                return True
            self._busy = True
            INTERRUPT.clear()
            self._worker = threading.Thread(target=self._pump, args=(text,),
                                            daemon=True)
            self._worker.start()
            return True

    def stop(self) -> None:
        # #143 E2: STOP REACHES THE SUBTASKS TOO. The flag stops the local
        # turn; the cancel promises the record -- whatever a subtask's stream
        # still delivers is dropped and its card ends "interrupted".
        INTERRUPT.set()
        crow_core.cancel_subtasks()

    # ------------------------------------------------------------ #142 images

    def stage_image(self, path: str) -> dict:
        """Read one dropped image and hold it for the next send.

        THE PAGE NEVER SEES THE BYTES ON DISK, only what comes back here:
        name and data URL for the chip. A refusal (wrong extension, unreadable
        file) is pushed as a note and the chips stay as they were -- the same
        sentence `image_part` writes, said where the drop happened.
        """
        try:
            part = crow_core.image_part(path)
        except crow_core.CrowError as exc:
            self.push({"k": "note", "t": str(exc)})
            return {"chips": self._image_chips()}
        self._staged_images.append({"part": part,
                                    "name": os.path.basename(path)})
        # PUSHED AS WELL AS RETURNED: a drop reads the return value, but
        # `/image <path>` goes through slash_answer, which returns a sentence
        # -- the strip learns about its chip through this event either way.
        self.push({"k": "chips", "c": self._image_chips()})
        return {"chips": self._image_chips()}

    def unstage_image(self, index) -> dict:
        """Drop one staged image, from its chip. Out-of-range is a no-op: the
        page redraws from what returns, so a stale click cannot desync it."""
        try:
            self._staged_images.pop(int(index))
        except (IndexError, ValueError, TypeError):
            pass
        self.push({"k": "chips", "c": self._image_chips()})
        return {"chips": self._image_chips()}

    def _image_chips(self) -> list:
        """What the page draws: one {name, url} per staged image, in order.
        The list IS the protocol -- both stage calls return it whole, so the
        page rerenders instead of tracking indices that shift on removal."""
        return [{"name": s["name"], "url": s["part"]["image_url"]["url"]}
                for s in self._staged_images]

    def set_tools(self, on: bool) -> None:
        """Switch tool execution, from the chip. Never mid-turn.

        The flag stays the STARTING value and this is the running one, so a user
        who wants it on from the first turn still has `--tools` and everyone else
        has one click. Refused while a turn runs: `run_turn` reads the setting
        once at the top, and changing it underneath would put the screen and the
        loop into two different opinions about what just happened.
        """
        if self._worker and self._worker.is_alive():
            return
        self._args.execute_tools = bool(on)
        self.push({"k": "tools", "on": self._args.execute_tools})
        self.push({"k": "note", "t": "tools now run"
                   if self._args.execute_tools else
                   "tools are now only shown"})

    def _ask_page(self, name: str, arguments: str) -> str:
        """#88: put one held-back call to the page and WAIT for the answer.

        THE WORKER THREAD BLOCKS HERE, and that is correct rather than a
        compromise: the tool loop cannot go on without the answer, and the page
        is not blocked -- it is drawing, and its click comes back through
        `answer()` on the pywebview thread. An Event is the whole apparatus.

        NO TIMEOUT THAT SAYS YES. If the window is closed while a question is
        open, the wait ends and the answer is "no": a call that runs because
        nobody was there to refuse it is the failure this ticket exists to
        prevent. The turn then carries a declined result, which is a shape the
        loop already handles.
        """
        self._answer = "no"
        self._asked.clear()
        scope = crow_core.approval_scope(name, arguments)
        self.push({"k": "ask", "name": name, "args": arguments,
                   "scope": scope[1] if scope else ""})
        # Woken by answer(); the flag is also set when the window goes away, so
        # a closed window is a refusal rather than a hang.
        self._asked.wait()
        return self._answer

    def answer(self, what: str) -> None:
        """The page's click on an open question. Anything unknown is "no"."""
        self._answer = what if what in ("yes", "always") else "no"
        self._asked.set()

    def mode_menu(self) -> list:
        """#88's levels, for the dropdown. Built from the core, not from a list.

        The page renders whatever this returns, so `manual` and `allowedit` are
        described by what they actually hold back -- read out of the same table
        `run_turn` decides with. A menu that spelled the three names out in the
        HTML would be a second copy of the levels, and the one that goes stale
        is the one the user reads.
        """
        return [{"name": name, "what": crow_core.mode_description(name)}
                for name in crow_core.MODES]

    def answer_memory(self, yes: bool) -> None:
        """#128. The user answered the held-back writes. Yes writes, no drops.

        NOT REFUSED MID-TURN, unlike `set_mode`. The level has to hold still
        during a turn because `run_turn` read it once at the top and the loop
        and the screen would otherwise disagree; a staged memory write is not
        part of any turn and no running loop is holding an opinion about it.
        Refusing here would mean the chip breathes at somebody who is not
        allowed to press it.

        THE GLOW LINE STILL FIRES on yes, through the same `memory` kind the
        ungated path uses. The gate changed who decides, not what a person sees
        afterwards -- a write that happened is still announced.
        """
        if yes:
            saved = crow_core.approve_pending()
            if saved:
                self.push({"k": "memory", "t": "Memory updated",
                           "n": len(saved)})
        else:
            crow_core.decline_pending()
        self.push({"k": "pend", "items": crow_core.pending_view()})

    def set_mode(self, name: str) -> None:
        """Switch the release level, from the dropdown. Never mid-turn.

        Refused while a turn runs, for the same reason `set_tools` is:
        `run_turn` reads the level once at the top of the turn, and changing it
        underneath would leave the screen and the loop with two different
        opinions about what was released.

        SWITCHING DROPS STANDING APPROVALS. Going to `manual` while keeping the
        directories released under `allowedit` would hand back a level that asks
        less than its name says.
        """
        if name not in crow_core.MODES:
            return
        if self._worker and self._worker.is_alive():
            self.push({"k": "note", "t": "the level does not change mid-turn"})
            return
        self._args.mode = name
        crow_core.forget_approvals()
        self.push({"k": "mode", "name": name, "modes": self.mode_menu()})
        self.push({"k": "note", "t": "mode %s -- %s" % (
            name, next(m["what"] for m in self.mode_menu() if m["name"] == name))})

    def reset(self) -> None:
        """Put the current conversation aside and start an empty one.

        IT IS ARCHIVED BEFORE IT IS DROPPED, and that ordering is the whole
        point: the file is written and read back before `reset()` touches the
        conversation, so a failed write leaves the user with the chat they had
        rather than with neither.
        """
        if self._worker and self._worker.is_alive():
            return
        ok, kept = self._leave()
        if not ok:
            self.push({"k": "fail",
                       "t": "the chat could not be put aside -- nothing changed"})
            return
        if kept:
            self.push({"k": "note", "t": "put aside as %s"
                                         % os.path.basename(kept)})
        self._conversation.reset()
        self._current_path = None
        self._current_title = None
        # #143. The fresh chat needs a fresh delegation frame at once: without
        # this push the page kept the previous chat's `here` values and drew
        # no card for anything delegated in here -- measured 2026-08-27 as a
        # fan-out with rail rows and no cards. Same drop `open()` does.
        self._subs_sig = ""
        self._push_subs()
        # #119 OVERTURNS #101's ANSWER FOR THIS ONE EVENT. A new chat used to
        # start from the template in roots.json; robin, on the built window:
        # "ein neuer Chat soll immer wurzellos sein".
        #
        # WHY THE TEMPLATE STOPPED BEING HARMLESS. `_bind_root` writes `active`,
        # so moving one chat into a project made that project the ground every
        # later chat started on -- and with the rail GROUPED by the boundary,
        # that is a new chat appearing inside a project nobody put it in. The
        # template was invisible while it only decided what a tool could write.
        #
        # THE OTHER TWO CALLERS ARE UNTOUCHED: opening a chat that never chose
        # still falls back to the template, and so does the launch -- which is
        # the case #92 added it for. `_adopt_chat_root` is no longer one answer
        # for three events, and the docstring there says so.
        self._adopt_chat_root(None, fresh=True)
        # #121. `Conversation.reset` dropped the old chat's pin; this takes the
        # new one. A fresh chat is rootless by the decision above, so what it
        # gets is the profile and a line saying there is no project -- until the
        # user moves it into one, which re-pins through `_bind_root`.
        self._pin_memory(None)
        self._context_tokens = 0
        self._promised_warm = False
        self.push({"k": "clear"})     # the page no longer guesses; see crow.reset
        self._hello()
        # SESSION.JSON GOES WITH IT, and only after the chat has been read back
        # off disk above. It still holds the conversation just put aside; left
        # there, the next launch would restore it as the open chat AND list the
        # archive file next to it -- the same chat twice, from one click.
        self._forget_live()
        self._reload_rail()
        self.push({"k": "cost", "line": "", "share": None, "tokens": 0,
                   "n_ctx": self._n_ctx})

    def _archive(self) -> str | None:
        """Write the open conversation to its own file. Its path, or None.

        WHERE IT GOES FOLLOWS FROM WHETHER IT ALREADY HAS A FILE, and from
        nothing else. That is the entire rule, and it used to be a flag the
        callers set: "neu" always asked for a NEW file, so a chat opened from
        the archive was written a second time next to itself, and switching
        always asked for the EXISTING one, so a chat that had none was written
        into session.json.

        NEVER session.json, and that half was the data loss. Nothing in the
        rail lists the live session file, so a chat put there had vanished from
        the window; the first turn of the chat being opened then wrote over it.
        A chat leaving the window either has a file of its own or is given one
        here, at the moment it is left -- not before, or the archive fills up
        with copies of a conversation nobody has finished yet.

        The user's name for it is added afterwards -- see `_stamp`.
        """
        folder = os.path.dirname(SESSION_FILE) or "."
        if self._current_path:
            path = self._current_path
        else:
            # A SECOND IS NOT UNIQUE ENOUGH. Two chats put aside inside the same
            # second landed on the same name and the second overwrote the first --
            # a silent loss, because both writes succeeded. The suffix is added
            # only when it is needed, so the common case keeps the plain stamp.
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(folder, "%s%s.json" % (self.ARCHIVE_PREFIX, stamp))
            extra = 2
            while os.path.exists(path):
                path = os.path.join(folder, "%s%s-%d.json"
                                    % (self.ARCHIVE_PREFIX, stamp, extra))
                extra += 1
        try:
            os.makedirs(folder, exist_ok=True)
            save_session(self._conversation, self._args.base_url,
                         self._context_tokens, path=path, with_kv=False,
                         tools_cleared=self._tools_cleared)
            # #101: FOR A NAMED EMPTY CHAT THE CORE WROTE NOTHING, and the
            # read-back below would then fail and report the chat as unsaveable.
            # `_stamp` creates it from the metadata -- the same door #100 opened
            # for session.json, used here for the chat's own file.
            if not os.path.isfile(path):
                self._stamp(path)
            # Read back before the caller drops the original. A write that
            # returned without raising is not the same as a file on disk.
            with open(path, encoding="utf-8") as fh:
                json.load(fh)
        except Exception:                  # noqa: BLE001 - reported as None
            return None
        self._stamp(path)
        return path

    def _leave(self) -> tuple[bool, str | None]:
        """Get the open chat onto disk before the window lets go of it.

        `(True, path)` when it was written, `(True, None)` when there was
        nothing worth writing, `(False, None)` when the write failed -- and on
        a failure the caller must change NOTHING, which is why it is reported
        rather than swallowed. Every exit from a chat goes through here, so
        there is one answer to "does this chat have a file yet" instead of one
        per caller.
        """
        empty = len(self._conversation) <= (1 if self._conversation.has_system else 0)
        # #101: THE LINE IS THE NAME, NOT THE EMPTINESS. An unnamed empty chat is
        # a stray click and is meant to vanish -- that is what keeps the rail from
        # filling with conversations nobody started. A NAMED empty chat is a
        # declaration of intent, a slot the user reserved, and losing it on the
        # next switch is the same defect #100 fixed one door further along.
        if empty and not self._current_title:
            return (True, None)
        path = self._archive()
        if not path:
            return (False, None)
        self._current_path = path
        # #143. The live chat just gained its file: every subtask it spawned
        # while it had none follows it there, or their rows would fall off the
        # rail the moment their parent stops being "the live chat".
        self._sub_adopt("", path)
        return (True, path)

    def _stamp(self, path: str, pointer: bool = False) -> None:
        """Put back what the core drops: the name, and on session.json the file.

        `save_session` serialises six keys and writes the file whole, so
        anything Crow's window keeps about a chat survives exactly until the
        next save. Re-stamped after every write, from the copy in this object,
        which is the only one that cannot be overwritten by the core.

        `pointer` marks the live session file: it also records WHICH archive
        file the open chat belongs to, so the next launch picks that chat up
        instead of minting a fresh copy of it.
        """
        if not path:
            return
        fresh = False
        if not os.path.isfile(path):
            # #100: THE CORE WRITES NOTHING FOR A CHAT WITH NO TURN IN IT, so the
            # name the user just typed has no file to live in and dies with the
            # window. `save_session` refuses an empty conversation on purpose --
            # that refusal is what stops a `/reset` chat coming back on the next
            # start -- so the file is created here instead of loosening it.
            #
            # ONLY FOR A NAME, and that is the whole gate. An empty chat NOBODY
            # named still leaves nothing behind -- the negative half, and without
            # it this would resurrect exactly the abandoned chat the refusal
            # exists to prevent.
            #
            # THE `pointer` REQUIREMENT CAME OFF WITH #101. It had limited this to
            # session.json, so a named empty chat survived closing the window and
            # vanished the moment the user switched to another chat: `_leave` had
            # nothing to archive and the next `_persist_live` wrote the other chat
            # over the only copy. A reserved slot is reserved either way.
            if not self._current_title:
                return
            fresh = True
        try:
            data = {}
            if not fresh:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            # KEYS ARE ONLY WRITTEN IF KNOWN, AND NEVER REMOVED BY SILENCE.
            # `_current_title` at None means "this window does not know a name",
            # not "this chat has no name": `/reset` empties it while the chat's
            # own file keeps its title on purpose -- detached, not deleted.
            #
            # AN `else: data.pop("crow_title", None)` STOOD HERE UNTIL NOW. It
            # could only stay harmless as long as `_current_path` happened to be
            # cleared in the same breath as the name -- written out by hand at
            # four separate places, and nothing enforces it. That is a
            # coincidence, not an invariant, and this is the one method that
            # both CREATES a chat's file and writes its identity into it.
            if self._current_title:
                data["crow_title"] = self._current_title
            # #101: THE BOUNDARY IS THE CHAT'S -- BUT ONLY IF IT WAS CHOSEN FOR IT.
            # A borrowed root is left out entirely rather than written as null:
            # absent means "nobody ever chose here", and that state has to stay
            # reachable, or a chat that merely displayed the template once would
            # own it from then on. A root this window did not read is left
            # exactly as it lies -- silence is not a decision to erase one.
            if self._root_chosen:
                data["crow_root"] = crow_core.get_root()
            if pointer:
                if self._current_path:
                    data["crow_path"] = self._current_path
                else:
                    data.pop("crow_path", None)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:                  # noqa: BLE001 - cosmetic, never fatal
            pass

    def _pointer(self) -> tuple[str | None, str | None]:
        """What session.json remembers about the chat it holds: file, name.

        A path that no longer exists comes back as None -- the user deleted
        that archive file, and a chat pointing at a hole would be written to it
        on the next switch and then not be listed anywhere.
        """
        try:
            with open(SESSION_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:                  # noqa: BLE001 - no pointer, no harm
            return (None, None)
        path = (data.get("crow_path") or "").strip() or None
        if path and not os.path.isfile(path):
            path = None
        return (path, (data.get("crow_title") or "").strip() or None)

    def _persist_live(self, with_kv: bool = False) -> None:
        """session.json holds the open chat -- after every turn, not only at exit.

        WITHOUT THE CACHE PER TURN, WITH IT ON THE WAY OUT. A slot save is one
        fixed filename on the server and ~1.3 GiB at the operating point; per
        turn it would write the same cache over itself onto the disk the
        experts are streamed from. Per turn it is the messages that have to
        survive a crash. The cache is saved once, when the window closes, which
        is what the CLI does too.
        """
        if not self._args.session:
            return
        try:
            # A REMOTE ENDPOINT HAS NO SLOT TO SAVE. `with_kv` is the caller's
            # half of the same contract `load_session` now reads: the messages
            # are still written, the cache half is not attempted, and the file
            # says `kv: false` so the next start does not try to restore one.
            spot = self._endpoint()
            save_session(self._conversation, spot["base_url"],
                         self._context_tokens,
                         with_kv=with_kv and not spot["remote"],
                         model=self._model, reasoning=self._reasoning,
                         tools_cleared=self._tools_cleared)
        except Exception:                  # noqa: BLE001 - a turn survives it
            return
        self._stamp(SESSION_FILE, pointer=True)

    @staticmethod
    def _forget_live() -> None:
        """Drop session.json, once its chat is safe somewhere else.

        Only ever called after a successful `_leave()` or a delete the user
        asked for twice: an empty conversation writes no file at all, so
        without this the abandoned chat would still be sitting there at the
        next launch.
        """
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass

    def open(self, path: str) -> None:
        """Load an archived conversation back into the window."""
        if self._worker and self._worker.is_alive():
            return
        if self._current_path and os.path.abspath(path) == os.path.abspath(
                self._current_path):
            return
        # THE OTHER CHAT IS READ FIRST, and only then is this one let go of. The
        # order used to be the other way round, so an archive that turned out to
        # be unreadable had already cost the open chat a write.
        try:
            # #121. The pin decides the head this file was written under, so it
            # is read before the payload -- the fingerprint cannot be taken from
            # messages nobody has opened yet.
            restored = load_session(
                self._endpoint()["base_url"],
                crow_core.system_with_memory(self._args.system,
                                             crow_core.session_memory(path)),
                path, model=self._model,
                with_kv=not self._endpoint()["remote"])
        except Exception as exc:           # noqa: BLE001
            self.push({"k": "fail", "t": "not readable: %s" % exc})
            return
        if not restored:
            # #101: EMPTY IS NOT BROKEN WHEN SOMEBODY NAMED IT. `load_session`
            # answers None for any file with no messages, which is correct and is
            # exactly what a reserved slot looks like -- so reading that as "this
            # archive is damaged" locked the user out of the chat they had just
            # created: it stood in the rail and refused every click.
            #
            # The line is the name, the same one `_leave` draws. A file nobody
            # named and with nothing in it is a leftover and stays refused.
            if not self._stored_title(path):
                self.push({"k": "fail", "t": "empty: %s" % os.path.basename(path)})
                return
            restored = ([], 0, False)
        ok, _ = self._leave()
        if not ok:
            self.push({"k": "fail", "t": "the open chat could not be "
                                         "put aside -- nothing changed"})
            return
        messages, tokens, kv = restored
        self._conversation = Conversation(self._args.system)
        self._conversation.restore(messages)
        self._current_path = path
        self._current_title = self._stored_title(path)
        # #101: THE BOUNDARY TRAVELS WITH THE CHAT. Before this line the window
        # kept whatever the previous chat had left bound, so switching to a chat
        # moved it into the last chat's project without saying anything.
        self._adopt_chat_root(path)
        self._pin_memory(path)        # #121, and below the bind for that reason
        self._context_tokens, self._promised_warm = tokens, kv
        # #131. THE WATERMARK BELONGS TO THE CHAT, so it is read from the chat's
        # own file rather than carried over from the one just closed.
        self._tools_cleared = crow_core.session_tools_cleared(path)
        self.push({"k": "clear"})     # the page no longer guesses; see crow.open
        self._hello()
        self._replay(messages)
        # SESSION.JSON FOLLOWS THE SWITCH AT ONCE. Still pointing at the chat
        # just closed, a window shut before the next turn would come back up
        # holding it -- and list the chat the user was actually reading as a
        # second entry beside it.
        self._persist_live()
        self._reload_rail()
        # #143. THE CARDS ARE PART OF THE REPLAY. They are drawn from the
        # registry, not from the history, so a reopened chat came back without
        # them and a subtask row had nothing to jump to -- robin, 2026-08-27:
        # "Ich kann die Subtasks nicht mehr anklicken." The signature is
        # dropped so the push fires even though nothing changed.
        self._subs_sig = ""
        self._push_subs()
        self.push({"k": "cost", "line": "", "share": None,
                   "tokens": self._context_tokens, "n_ctx": self._n_ctx})

    # #123 MOVED THE LITERAL, NOT THE MEANING. The search index has to walk the
    # same folder the rail is drawn from, and a second `"archiv"` typed in the
    # core would be a chat that is in the rail and not in the index, or the
    # reverse, the first time either spelling changed.
    ARCHIVE_DIR = crow_core.ARCHIVE_DIR

    def _replay(self, messages: list) -> None:
        """Draw a restored conversation the way a live one is drawn.

        THROUGH THE SAME SINK, and that is the whole fix. The first version
        pushed the stored text straight at the page, so a reopened chat lost
        every code frame and every thought block: fences are found by
        `crow_core.CodeFences`, and nothing was running it. Reasoning was worse
        than unstyled -- it was not sent at all, because the restore only ever
        looked at `content`.

        One renderer for both paths means a reopened chat cannot drift from the
        one that was just typed.
        """
        seen = 0
        # #131. READ ONCE, AND OPTIONAL. A chat that never cleared anything has
        # no watermark -- the normal state, and also what a bare replay harness
        # hands in.
        cleared = getattr(self, "_tools_cleared", 0)
        for message in messages:
            role = message.get("role")
            body = (message.get("content") or "")
            if role == "user":
                # #142. A restored turn may carry blocks: the words go back as
                # the line, the images go back as images -- the ticket's "the
                # same image is still there after a restart".
                words = crow_core.message_text(body)
                urls = [u for u in
                        (((p.get("image_url") or {}).get("url") or "")
                         for p in crow_core.message_images(body)) if u]
                if words.strip() or urls:
                    entry = {"k": "user", "t": words}
                    if urls:
                        entry["i"] = urls
                    self.push(entry)
                continue
            if role != "assistant":
                continue
            thought = (message.get("reasoning_content") or "")
            # #99. THE TOOL ROWS ARE PART OF THE TURN, and a turn that only
            # called a tool has no `content` at all -- so the emptiness test has
            # to know about them, or the whole message is skipped and the
            # reopened chat shows two thoughts with nothing between them.
            calls = message.get("tool_calls") or []
            if not body.strip() and not thought.strip() and not calls:
                continue
            sink = Sink(self.push, live=False)
            sink.reply_started()
            if thought.strip():
                sink.reasoning_started(0)
                self.push({"k": "think", "t": thought})
                sink.reasoning_finished()
            if body:
                sink.answer_text(body)
            # After the answer, because that is the live order: the model says
            # what it is about to do, then the calls run. Through `Turn`, which
            # is where `tool_started` lives and where the live path draws these
            # rows -- a second `{"k": "tool"}` written here is exactly the drift
            # this method exists to prevent.
            rows = Turn(self.push)
            for call in calls:
                # #131. A ROW THE USER DISMISSED STAYS DISMISSED. `seen` counts
                # every call in the conversation, in order, and everything up to
                # the watermark is drawn by nobody -- the message itself is
                # untouched, so the model still has the call it made.
                seen += 1
                if seen <= cleared:
                    continue
                function = call.get("function") or {}
                rows.tool_started(function.get("name") or "?",
                                  function.get("arguments") or "")
            sink.reply_finished()
            self.push({"k": "cost", "line": "", "share": None,
                       "tokens": self._context_tokens, "n_ctx": self._n_ctx})

    def _archived(self) -> list:
        """What the user put away, out of .crow/archiv/. Same shape as the rest."""
        folder = os.path.join(os.path.dirname(SESSION_FILE) or ".", self.ARCHIVE_DIR)
        out = []
        try:
            names = sorted(os.listdir(folder), reverse=True)
        except OSError:
            return out
        for name in names:
            path = os.path.join(folder, name)
            if not name.endswith(".json") or not os.path.isfile(path):
                continue
            out.append(self._entry_of(path, name))
        return out

    def _live_title(self) -> str:
        """What the open chat is called: the user's name for it, else its
        opening line, else the label a chat with no turn in it deserves."""
        return (self._current_title
                or self._first_line(self._conversation.payload())
                or "new chat")

    def _reload_rail(self) -> None:
        """THE ONE PLACE THE RAIL IS DRAWN.

        Four callers used to assemble this message themselves, each out of
        whatever it happened to be holding: start-up counted the restored
        messages, a switch read the file's title, "neu" hard-coded "new chat".
        The same chat therefore had a different name depending on which event
        had drawn it last. They all come through here now, so the rail cannot
        contradict itself.
        """
        spare = 1 if self._conversation.has_system else 0
        turns = len(self._conversation) - spare
        self.push({"k": "rail",
                   "title": self._live_title(),
                   "meta": ("no turn yet" if turns <= 0 else
                            "%d messages%s" % (turns, " · cache warm"
                                               if self._promised_warm else "")),
                   # With a file the live chat is already in the list below,
                   # marked; without one the page draws it on top. Never two.
                   "unsaved": self._current_path is None,
                   "rollovers": self._archives(),
                   "archived": self._archived(),
                   # #119. THE PROJECTS TRAVEL WITH THE RAIL, not on their own
                   # message, because a rail drawn against a stale project list
                   # puts a chat under a heading that is no longer there. One
                   # payload, one grouping, no frame where the two disagree.
                   # The live chat's own root rides along so the page can mark
                   # the project it belongs to before it has a file.
                   "projects": self._projects(),
                   "live_root": crow_core.get_root() or "",
                   # #143. The subtasks ride the rail payload, so a redraw can
                   # never lose their rows to a repaint the ticker missed.
                   "subs": self._subs_items(),
                   "foot": os.path.basename(self._current_path)
                   if self._current_path else ""})

    @staticmethod
    def _projects() -> list:
        """The project rows: path, and the name a person recognises.

        THE NAME IS THE FOLDER'S, and there is nowhere else it could come from
        without inventing a second key to hold it. `os.path.basename` is what
        the root picker beside this already shows, so a folder reads the same
        in both lists -- and a rename in the file manager reaches the rail
        without anything here being told.
        """
        shut = open_projects()
        return [{"path": p, "name": os.path.basename(p) or p,
                 "open": shut.get(os.path.normcase(p), True)}
                for p in crow_core.projects()]

    def rename(self, path: str, title: str) -> bool:
        """Give a saved chat a name. The file keeps its own name.

        THE TITLE GOES INSIDE THE FILE, not into its filename. A rename that
        moved the file would break `_current_path` for an open chat, and every
        path the rail is holding, for a label. One key, added to the JSON the
        core wrote, ignored by everything that does not look for it.
        """
        title = (title or "").strip()[:self.TITLE_MAX]
        if not title:
            return False
        if not path:
            # THE OPEN CHAT IS NAMED IN MEMORY, NOT BY BEING FILED AWAY. Naming
            # it used to archive it -- a chat the user had merely labelled was
            # moved in among the ones they were finished with -- and the name
            # did not survive even that, because the next write through the core
            # dropped the key again. It is stamped on session.json here, on the
            # chat's own file if it has one yet, and on every file `_archive`
            # writes for it from now on.
            #
            # THAT FIRST HALF WAS NOT TRUE UNTIL #100. With no turn in the chat
            # the core wrote no session.json at all, so there was nothing to
            # stamp and the name died with the window -- while this comment said
            # otherwise. `_stamp` creates the file for a named chat now.
            self._current_title = title
            self._persist_live()
            self._stamp(self._current_path or "")
            self._reload_rail()
            return True
        if not os.path.isfile(path):
            return False
        if self._current_path and os.path.abspath(path) == os.path.abspath(
                self._current_path):
            self._current_title = title
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            data["crow_title"] = title
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:                  # noqa: BLE001
            self.push({"k": "fail", "t": "rename failed"})
            return False
        self._reload_rail()
        return True

    def archive_chat(self, path: str) -> bool:
        """Move a chat into .crow/archiv/, or back out of it. Nothing is lost.

        ONE CALL, BOTH DIRECTIONS. Which way it goes follows from where the file
        currently is, so the page does not have to keep a second method straight
        -- it only changes the label on the menu item.
        """
        if not path:
            ok, path = self._leave()
            if not ok:
                self.push({"k": "fail", "t": "the open chat could not be put aside"})
                return False
            if not path:
                self.push({"k": "note", "t": "nothing to archive yet"})
                return False
        if not os.path.isfile(path):
            return False
        here = os.path.dirname(path)
        if os.path.basename(here) == self.ARCHIVE_DIR:
            folder = os.path.dirname(here)          # back into .crow/
        else:
            folder = os.path.join(here, self.ARCHIVE_DIR)
        target = os.path.join(folder, os.path.basename(path))
        try:
            os.makedirs(folder, exist_ok=True)
            os.replace(path, target)
        except Exception:                  # noqa: BLE001
            self.push({"k": "fail", "t": "archiving failed"})
            return False
        if self._current_path and os.path.abspath(path) == os.path.abspath(
                self._current_path):
            # THE POINTER MOVES WITH THE FILE. session.json records which
            # archive file the open chat belongs to; left naming the old
            # location, that pointer reads as "this chat has no file yet" on the
            # next launch, and a second one is written beside the moved original.
            self._current_path = target
            self._stamp(SESSION_FILE, pointer=True)
        # #143. And the subtask parents move with it, by the same rule.
        self._sub_adopt(path, target)
        back = os.path.basename(os.path.dirname(target)) != self.ARCHIVE_DIR
        self.push({"k": "note",
                   "t": ("restored: %s" if back else "archived: %s")
                        % os.path.basename(path)})
        self._reload_rail()
        return True

    def _hello(self) -> None:
        """Put the greeting under an empty chat. #119.

        ITS OWN MESSAGE RATHER THAN A FIELD ON `clear`, because the start-up
        case has no clear to hang it on: a window that comes up on a chat with
        no turns in it needs the line too, and that path never empties anything.

        SAFE AFTER EVERY CLEAR, INCLUDING `open()`, which clears and then
        replays a conversation on top. The page drops the greeting inside
        `turn()` -- the one place a turn is appended -- so a replay removes it
        on its first row and an empty chat keeps it. The caller does not have to
        know which of the two it is.
        """
        self.push({"k": "hello", "t": greeting()})

    def discard_live(self) -> bool:
        """#119. Throw away the open chat that was never written. True when done.

        THE ROW THAT COULD NOT BE DELETED. A chat with no file has no path, so
        `delete_chat` had nothing to remove: the menu armed, said "really
        delete?", and then did nothing at all -- which is worse than refusing,
        because a refusal at least reaches the user.

        IT REFUSES A CHAT THAT HAS A FILE, and that guard is the whole safety of
        this door: it drops a conversation WITHOUT archiving it, which is only
        ever right for one that was never on disk. A saved chat is
        `delete_chat`'s business, and a second way to lose one is a second way
        to lose one.

        NOT `reset`, WHICH ARCHIVES. That is the difference the user is asking
        for: "new" puts the chat aside, "delete" does not keep it.
        """
        if self._worker and self._worker.is_alive():
            self.push({"k": "note", "t": "not while a turn is running"})
            return False
        if self._current_path:
            return False
        # Discarding the live chat discards its subtasks with it -- the same
        # rule delete_chat keeps, for the chat that never had a file.
        self._drop_chat_subtasks("")
        self._conversation.reset()
        self._current_title = None
        self._context_tokens = 0
        self._promised_warm = False
        # SESSION.JSON GOES WITH IT, for the reason `delete_chat` states below:
        # left there, the next launch restores the very chat that was discarded.
        self._forget_live()
        self.push({"k": "clear"})
        self._hello()
        self._reload_rail()
        return True

    def delete_chat(self, path: str) -> bool:
        """Remove a saved chat. There is no undo, which is why the page asks
        twice before it gets here."""
        if not os.path.isfile(path):
            return False
        try:
            os.remove(path)
        except Exception:                  # noqa: BLE001
            self.push({"k": "fail", "t": "deleting failed"})
            return False
        # robin, 2026-08-28 abends: der geloeschte Chat nimmt seine Subtasks
        # mit -- Registry, Rail-Zeilen, Chip und Transkripte. BEFORE the
        # open-branch below clears `_current_path`: unstamped rows still
        # stamp to the chat they were spawned in.
        self._drop_chat_subtasks(path)
        if self._current_path and os.path.abspath(path) == os.path.abspath(
                self._current_path):
            # THE OPEN CHAT WAS THE ONE DELETED, so it is dropped rather than
            # merely unhooked from its file. Kept in memory it came straight
            # back: the next "new" wrote it out under a fresh name, and the
            # chat the user had deleted twice over was in the rail again a
            # click later. session.json goes with it for the same reason -- it
            # still held the whole conversation.
            self._current_path = None
            self._current_title = None
            self._conversation.reset()
            self._context_tokens = 0
            self._promised_warm = False
            self._forget_live()
            self.push({"k": "clear"})
            self._hello()
        self.push({"k": "note", "t": "deleted: %s" % os.path.basename(path)})
        self._reload_rail()
        return True

    def minimise(self) -> None:
        self._window.minimize()

    def geometry(self) -> dict:
        return {"x": self._window.x, "y": self._window.y,
                "w": self._window.width, "h": self._window.height}

    def set_geometry(self, x: int, y: int, w: int, h: int) -> None:
        """One drag step from a resize grip. Below the floor, nothing moves."""
        if w < 760 or h < 520:
            return
        self._window.move(int(x), int(y))
        self._window.resize(int(w), int(h))

    def _hwnd(self) -> int | None:
        """This window's HWND, or None.

        `native` is a System.Windows.Forms.Form and exists only after
        before_show; `.Handle` is pywebview's documented route to the HWND.

        `.ToInt64()`, NOT `int()`. Measured 2026-08-14: `int()` on that value
        raises `TypeError: int() argument must be a string, a bytes-like object
        or a real number, not 'IntPtr'`. The first version of this method caught
        that and returned None, so every caller silently took the no-handle
        path -- the monitor lookup below never ran once, and the bug it was
        written to fix looked unchanged. An except that swallows the one error
        worth seeing is worse than no except: it turns a defect into a feature
        nobody can find.
        """
        handle = None
        try:
            handle = self._window.native.Handle
            return handle.ToInt64()
        except AttributeError:
            pass                           # not an IntPtr -- try the plain way
        except Exception:                  # noqa: BLE001 - native not up yet
            return None
        try:
            return int(handle)
        except Exception:                  # noqa: BLE001
            return None

    @staticmethod
    def _work_area(hwnd=None) -> tuple:
        """The work area OF THE MONITOR THIS WINDOW IS ON, minus the taskbar.

        A frameless window told to fill the screen fills the SCREEN -- taskbar
        included, which is how the first maximise buried it.

        SPI_GETWORKAREA IS THE PRIMARY MONITOR'S RECTANGLE AND NOTHING ELSE.
        That is not a quirk, it is what the call is defined to return, and it is
        why a window on a second screen jumped back to the main one on every
        double-click: it was told to move to coordinates that only exist over
        there. MonitorFromWindow(MONITOR_DEFAULTTONEAREST) plus GetMonitorInfoW
        gives the rectangle of the monitor the window actually sits on, taskbar
        already subtracted in rcWork.

        argtypes on both calls, because a handle passed without them is
        truncated to 32 bits and the call then names a monitor that does not
        exist -- silently, returning 0 rather than raising.
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            if hwnd:
                class MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD),
                                ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT),
                                ("dwFlags", wintypes.DWORD)]

                user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
                user32.MonitorFromWindow.restype = wintypes.HMONITOR
                user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR,
                                                   ctypes.POINTER(MONITORINFO)]
                user32.GetMonitorInfoW.restype = wintypes.BOOL

                monitor = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    work = info.rcWork
                    return (work.left, work.top,
                            work.right - work.left, work.bottom - work.top)

            # No handle yet (before before_show), or the monitor query failed.
            # The primary monitor is wrong on a second screen, but it is a
            # rectangle that exists -- which the 1280x800 below is not.
            rect = wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return (rect.left, rect.top,
                        rect.right - rect.left, rect.bottom - rect.top)
        except Exception:                  # noqa: BLE001 - cosmetic
            pass
        return (0, 0, 1280, 800)

    def maximise(self) -> None:
        """Fill the work area, or go back to where the window was before.

        THE PREVIOUS SIZE IS REMEMBERED HERE because nothing else remembers it:
        a frameless window has no restore state of its own, so a maximise that
        did not write the old rectangle down would be a one-way trip.
        """
        area = self._work_area(self._hwnd())
        now = (self._window.x, self._window.y, self._window.width, self._window.height)
        filled = (abs(now[2] - area[2]) < 4 and abs(now[3] - area[3]) < 4)
        if filled and self._restore:
            target, self._restore = self._restore, None
        else:
            self._restore, target = now, area
        self._window.move(int(target[0]), int(target[1]))
        self._window.resize(int(target[2]), int(target[3]))

    def copy(self, text: str) -> bool:
        """Put text on the Windows clipboard. True when it arrived.

        The page cannot: it is loaded as HTML rather than served, so it is not a
        secure context and `navigator.clipboard` refuses without raising. `clip`
        is on every Windows since XP and takes UTF-16LE on stdin.
        """
        if not text:
            return False
        try:
            import subprocess

            proc = subprocess.run(
                ["clip"], input=text.encode("utf-16-le"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            return proc.returncode == 0
        except Exception:                  # noqa: BLE001 - reported as False
            return False

    def update_check(self) -> dict:
        """What the About pane asks when it opens. Never raises.

        The versions are not decided here: `update_state` is a thin line over
        `fetch_latest_version` and `is_newer`, which the terminal has used since
        0.0.6. What this adds is the pair a button needs, `installed_here` and
        `install_dir`, because the answer to "which copy is this" is often no.
        """
        try:
            return crow_core.update_state(current=client_version())
        except Exception:                  # noqa: BLE001 - a pane, never fatal
            return {"current": client_version(), "latest": None,
                    "newer": False, "installed_here": False,
                    "install_dir": crow_core.install_dir()}

    def update_start(self) -> str:
        """Press the button. Empty when it started, otherwise why it did not.

        ONE AT A TIME. Two installers writing the same directory at once is the
        one way this leaves a broken copy behind, and the button is reachable
        again the moment the first press fails.
        """
        if self._updating:
            return "an update is already running"
        self._updating = True
        threading.Thread(target=self._update_run, daemon=True).start()
        return ""

    def _update_run(self) -> None:
        """Fetch install.ps1, run it, and say what happened.

        ITS OWN LINES ARE THE PROGRESS. The package is around half a gigabyte,
        so this takes minutes; a window that said "installing" and then nothing
        for four of them is indistinguishable from a window that has hung.

        A NON-ZERO EXIT PROMISES NOTHING. The files may be half replaced, and
        "restart to use it" there sends the reader to find out alone.
        """
        script = ""
        try:
            script = crow_core.fetch_install_script()
            argv = crow_core.update_argv(script)
            # NO CONSOLE OF ITS OWN. Started from a shortcut this process has
            # none, and a child that asks for one puts a black window in front
            # of the reader for the length of the download.
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # stdin=DEVNULL for the reason `nvidia_smi` carries: a child
                # that inherits this one's stdin reads it.
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", creationflags=flags)
            last = ""
            for line in proc.stdout:
                line = line.strip()
                if line:
                    last = line
                    self.push({"k": "update", "t": line, "done": False})
            code = proc.wait()
        except Exception as exc:           # noqa: BLE001 - reported, never raised
            self.push({"k": "update", "done": True,
                       "t": "the update could not run: %s" % exc})
            self._updating = False
            return
        finally:
            if script:
                try:
                    os.remove(script)
                except OSError:
                    pass
        if code == 0:
            # THE INSTALLER'S OWN VERDICT, not a sentence of this window's. It
            # answers "nothing to do" when the same version is already there,
            # and a fixed "installed" would be wrong exactly then.
            self.push({"k": "update", "done": True,
                       "t": ("%s -- restart Crow to run it." % last if last
                             else "done. restart Crow to run it.")})
        else:
            self.push({"k": "update", "done": True,
                       "t": "the installer stopped with %d: %s" % (code, last)})
        self._updating = False

    def rail_width(self, px) -> bool:
        """Die gezogene Breite merken. False, wenn es keine brauchbare ist.

        GEKLEMMT, OBWOHL DIE SEITE SCHON KLEMMT. Zwei Tore fuer denselben Wert,
        weil er aus einer Maus kommt und in eine Datei geht, die beim naechsten
        Start gelesen wird -- eine kaputte Zahl dort ueberlebt jedes Fenster.
        """
        if isinstance(px, bool) or not isinstance(px, (int, float)):
            return False
        width = int(px)
        if width < RAIL_MIN or width > RAIL_MAX:
            return False
        doc = read_settings()
        doc["rail_width"] = width
        return write_settings(doc)

    def open_url(self, url: str) -> bool:
        """A link in an answer, opened OUTSIDE this window. True when it went.

        NEVER IN THE WEBVIEW. Following a link in here would replace the client
        with a web page, and a frameless window has no back button -- the chat,
        the composer and the rail would simply be gone until Crow was restarted.

        THE SCHEME IS CHECKED AGAIN, and the core already checked it. Two gates
        for one decision, because this text was written by somebody else's model
        and `javascript:` and `data:` are script while `file:` is the disk of
        whoever is reading.
        """
        if not isinstance(url, str):
            return False
        if not url.lower().startswith(("http://", "https://")):
            return False
        try:
            return bool(webbrowser.open(url))
        except Exception:                  # noqa: BLE001 - a link, never fatal
            return False

    # -- dictation (crow_voice) --------------------------------------------
    #
    # THE PAGE CANNOT DO THIS ITSELF, and that is measured rather than assumed:
    # the window is handed to WebView2 as HTML rather than served, so it is not
    # a secure context, and `getUserMedia` refuses there exactly as
    # `navigator.clipboard` did on 2026-08-13. So the button lives on the page
    # and the microphone lives here -- the same seam `copy` above uses.

    def dictate_start(self) -> None:
        """The mic button, pressed. Recording begins, or the reason is said."""
        why = crow_voice.available()
        if why:
            self.push({"k": "mic", "state": "off", "blocked": why, "note": why})
            return
        try:
            crow_voice.start()
        except Exception as exc:           # noqa: BLE001 - said, not raised
            # THE DEVICE'S OWN WORDS. A microphone can fail for reasons this
            # window cannot name -- in use by something else, a rate it will
            # not take, a driver that went away -- and a guess printed in place
            # of the real message is the guess the user then tries to fix.
            self.push({"k": "mic", "state": "off",
                       "note": "the microphone did not open: %s" % exc})
            return
        self.push({"k": "mic", "state": "rec"})
        threading.Thread(target=self._voice_meter, daemon=True).start()

    def _voice_tick(self) -> None:
        """Eine Ablesung. Null, sobald der Strom zu ist -- das ist das Zeichen,
        an dem die Seite das Band stehen laesst."""
        self.push({"k": "voice",
                   "level": crow_voice.level() if crow_voice.recording() else 0.0})

    def _voice_meter(self) -> None:
        """Solange aufgenommen wird, etwa sechzehnmal je Sekunde.

        AUF EINEM FADEN UND NICHT IM CALLBACK. PortAudios eigener Faden fuellt
        die Bloecke; wer von dort aus in die Queue der Seite schriebe, haengte
        die Aufnahme an das Zeichentempo eines Fensters.
        """
        while crow_voice.recording():
            self._voice_tick()
            time.sleep(0.06)
        self._voice_tick()

    def dictate_stop(self) -> None:
        """Stop, then transcribe on a worker.

        NOT IN THIS CALL. The first dictation loads 486 MB of model, and every
        one after it still runs the recogniser; a bridge call that did the work
        would hold the page's promise open for seconds with the button frozen
        mid-state. This returns at once and the result arrives as an event, the
        way every other slow thing in this window reports.
        """
        # `off` THE MOMENT THE STREAM CLOSES, not when the text arrives. The
        # button has two states and recording is over, so it goes grey now --
        # an in-between colour for "the machine is busy" was built once, in
        # yellow, and robin cut it: a state nobody can act on is furniture.
        self.push({"k": "mic", "state": "off"})
        threading.Thread(target=self._dictate_finish, daemon=True).start()

    def _dictate_finish(self) -> None:
        try:
            text = crow_voice.stop()
        except Exception as exc:           # noqa: BLE001 - said, not raised
            self.push({"k": "mic", "state": "off",
                       "note": "dictation failed: %s" % exc})
            return
        if not text:
            # SAID, NOT SWALLOWED. An empty result and a broken microphone look
            # identical from the button, and a user who gets neither text nor a
            # sentence has to guess which of the two just happened.
            self.push({"k": "mic", "state": "off", "note": "nothing was said"})
            return
        self.push({"k": "mic", "state": "off", "text": text})

    def skills(self) -> list:
        """Every skill on disk, for the settings sheet. Off ones included.

        A SHEET THAT ONLY LISTED THE ENABLED ONES would have no row to click for
        the disabled ones, so switching one back on would need a text editor.
        The body is left out: the sheet shows what a skill IS FOR, and the steps
        are the model's business.
        """
        return [{"name": sk["name"], "description": sk["description"],
                 "enabled": sk["enabled"]} for sk in crow_core.skills()]

    def toggle_skill(self, name: str, enabled: bool) -> bool:
        """Put a skill into the prompt or take it out. True when the file moved.

        THE OPEN CHAT IS RE-PINNED HERE, and the cost is announced before it is
        paid -- the same shape `_bind_root` uses. Without it the switch would
        look broken in the most confusing way possible: the row flips, the file
        changes, and the running conversation keeps the head it was pinned with,
        so nothing about the model's behaviour changes until the next chat.
        """
        if not crow_core.set_skill_enabled(name, bool(enabled)):
            return False
        if self._conversation.memory is not None:
            if self._conversation.repin_memory(crow_core.prompt_head()):
                self.push({"k": "note", "t": crow_core.SKILL_COST_NOTE})
        return True

    # #129. THE MCP SHEET. Every one of these is a thin pass to `crow_core`,
    # and that is deliberate rather than lazy: `/mcp` in both clients runs the
    # same functions, so the terminal and the window cannot describe one
    # configuration differently. The window adds exactly one thing the terminal
    # does not need -- a shape the page can draw.
    def mcp_view(self) -> dict:
        """What the sheet draws. Asked for on every open and after every tick."""
        return crow_core.mcp_view()

    def mcp_confirm(self, name: str, tool: str, included, klass) -> str:
        """One tick or one class. Returns the reason it was refused, or "".

        A STRING RATHER THAN A BOOLEAN, because the page painted the click before
        the file took it and has to be able to say WHY it is putting the row
        back. "Refused" alone would leave a switch flicking for no stated reason.

        `klass=None` MEANS "DO NOT TOUCH IT", which is not the same as clearing
        it: the switch column and the class column are two decisions, and moving
        one may not silently discard the other.
        """
        choice = {"included": bool(included)}
        if klass is not None:
            choice["class"] = klass or None
        problem = crow_core.mcp_confirm(name, {tool: choice})
        if problem:
            return problem
        # THE BILL IS SAID WHERE IT IS INCURRED, the shape `SKILL_COST_NOTE`
        # already has. The tool list is rendered into the HEAD of the prompt, so
        # a tick moves byte 0 -- for this chat and for every saved one.
        self.push({"k": "note", "t": crow_core.MCP_COST_NOTE})
        return ""

    def mcp_add(self, line: str) -> str:
        """One line in -- a command or a URL -- and a working server out.

        "" when it worked. The field takes both because a server is a server:
        which transport it happens to speak is the first token's business, not
        something anybody should have to declare in a second control.
        """
        _, _, problem = crow_core.mcp_add_line(line)
        return problem or ""

    def mcp_refresh(self, name: str) -> str:
        return crow_core.mcp_refresh_server(name) or ""

    def mcp_remove(self, name: str) -> str:
        return crow_core.mcp_remove_server(name) or ""

    def toggle_server(self, name: str, enabled: bool) -> bool:
        """Put a server's tools on the table or take them off.

        IT DOES NOT RE-PIN, AND THAT IS THE DIFFERENCE FROM `toggle_skill`. A
        skill is text inside the pinned head, so flipping one has to move the
        pin or the running chat keeps a head that no longer matches the file. A
        server's tools are not in the head at all -- they are the declarations
        `mcp_apply` rebuilds, which travel in the request beside it. Re-pinning
        here would rewrite a head that did not change, and charge a full prefill
        for the privilege.

        WHAT THE USER SEES INSTEAD is the row's own character count, which the
        sheet redraws after this returns. That number IS the cost, and it is
        already on screen.
        """
        return crow_core.set_mcp_enabled(name, bool(enabled))

    def set_server_key(self, name: str, value: str) -> str:
        """Keep a static key for one HTTP server, or forget it when cleared.

        IT GOES TO THE TOKEN STORE, NOT TO `mcp.json`. robin decided that on
        2026-08-24, and the reference agrees: Hermes writes keys to `.env` and
        leaves the configuration carrying references. The configuration is the
        file people copy; the token store is the one with 0o600 on it.
        """
        return crow_core.mcp_key_set(name, value) or ""

    # THE MODEL PAGE AND THE KEY PAGE. Thin passes to `crow_core` for the reason
    # the MCP block above is: which endpoint a turn goes to is not a window
    # decision, and a second copy of it here would be a second answer to it.
    def provider_view(self) -> dict:
        """What both pages draw. A key is in it as a mask or not at all."""
        return crow_core.provider_view()

    def provider_pick(self, name: str, model=None) -> str:
        """Choose the endpoint, and the model on it. The refusal, or "".

        REFUSED MID-TURN, the same answer `/model` gives: the loop read its
        endpoint at the top and changing it underneath would send the second
        half of a turn somewhere else.

        A CHANGED PROVIDER EMPTIES THE CHAT, and it has to. The context was
        cheap against a cache that belonged to the endpoint being left; carried
        across, it would be re-read from byte 0 by a server that never saw it --
        or by a provider that bills for reading it.
        """
        if self._worker and self._worker.is_alive():
            return "the endpoint does not change mid-turn"
        before = crow_core.provider_active()
        problem = crow_core.provider_pick(name, model)
        if problem:
            return problem
        self._endpoint_changed(reset=name != before)
        return ""

    def provider_key(self, name: str, key: str) -> str:
        """Store or clear one key, then fetch what it unlocks. Refusal or "".

        THE CATALOGUE IS FETCHED HERE and nowhere later: this is the one moment
        a person is expecting to wait for a provider. A failed fetch is not a
        failed key -- the key is already stored, and the line says which of the
        two went wrong.
        """
        problem = crow_core.provider_key_set(name, key)
        if problem:
            return problem
        if not (key or "").strip():
            return ""
        return crow_core.provider_refresh(name) or ""

    def delegate_favorites_set(self, models=None) -> str:
        """#148: store the three delegate favourites. The refusal, or ""."""
        return crow_core.delegate_favorites_set(models or []) or ""

    def openrouter_set(self, on) -> str:
        """Park or unpark the broker (its own page, 2026-08-28). Refusal or "".

        ON NEVER MOVES A TURN -- robins Regel: no endpoint change, so it is
        allowed mid-turn and the machine keeps answering. OFF while turns sit
        on the broker IS an endpoint change and walks `provider_pick`'s road:
        refused mid-turn, both lines said, the chat emptied -- and refused
        WHOLE, so a blocked park leaves the flag standing.
        """
        if not on and crow_core.provider_active() == "openrouter":
            said = self.provider_pick(crow_core.LOCAL_PROVIDER, None)
            if said:
                return said
        return crow_core.openrouter_set(bool(on)) or ""

    def provider_model_set(self, name: str, slug: str = "") -> str:
        """Remember a provider's slug WITHOUT routing turns. Refusal, or "".

        The broker page writes through this: a free pick is what the delegate
        default reads, and `provider_pick` stays the only mover of turns.
        """
        return crow_core.provider_model_set(name, slug or "") or ""

    def provider_refresh(self, name: str) -> str:
        """Ask a provider for its model list again. The problem, or ""."""
        return crow_core.provider_refresh(name) or ""

    def provider_authorise(self, name: str) -> str:
        """The Subscriptions tile. Opens the browser and waits. Problem, or "".

        IT BLOCKS FOR AS LONG AS THE PERSON TAKES, up to five minutes -- the
        same shape `mcp_add` has, and for the same reason: the flow is not done
        until somebody has finished at the provider, and a call that returned
        early would leave the page saying "signed in" while nothing was.

        REFUSED MID-TURN: the credential this replaces is the one the running
        turn is authenticating with.
        """
        if self._worker and self._worker.is_alive():
            return "the sign-in does not run mid-turn"
        return crow_core.provider_authorise(name) or ""

    def provider_oauth(self, name: str, fields: dict) -> str:
        """The setup form on a Subscriptions tile. The problem, or "".

        THE VALUES ARRIVE FROM THE PAGE AS A DICT and are filtered in the core,
        not here: which keys a login may carry is not a window decision.
        """
        return crow_core.provider_oauth_set(name, dict(fields or {})) or ""

    def provider_borrow(self, name: str, on: bool) -> str:
        """Use, or stop using, another program's sign-in. Problem, or "".

        REFUSED MID-TURN, like every other credential change: the running turn
        is authenticating with the one this replaces.
        """
        if self._worker and self._worker.is_alive():
            return "the sign-in does not change mid-turn"
        return crow_core.provider_borrow_set(name, bool(on)) or ""

    def provider_token(self, name: str, token: str) -> str:
        """What `claude setup-token` printed, kept. The problem, or "".

        REFUSED MID-TURN like every other credential change.
        """
        if self._worker and self._worker.is_alive():
            return "the sign-in does not change mid-turn"
        problem = crow_core.provider_token_paste(name, token)
        if problem:
            return problem
        if (token or "").strip():
            # THE CATALOGUE IS ASKED FOR HERE, the one moment somebody expects
            # to wait -- and a refusal from it is not a refusal of the token.
            crow_core.provider_refresh(name)
        return ""

    def provider_signout(self, name: str) -> str:
        """Drop one stored login. The problem, or "".

        THE PASTED KEY, IF THERE IS ONE, SURVIVES. They are two credentials in
        two files, and signing out of a subscription is not the same act as
        forgetting a key somebody typed.
        """
        return crow_core.provider_token_drop(name) or ""

    def _endpoint_changed(self, reset: bool) -> None:
        """Re-read the endpoint and tell the page. Empties the chat if asked.

        THE TWO LINES ARE SAID BEFORE THE SCREEN CHANGES, not after: a person
        who reads them once the window has emptied has been informed of a loss
        instead of warned about one. MODEL_SWITCH_NOTE is reused rather than
        rewritten -- it is already the one answer to "where did my context go".

        AND NOTHING IS CLEARED, which is the whole reason they are readable. A
        first version pushed `clear` at the end of this block; the page answers
        that with `flow.innerHTML=""`, so both lines were wiped by the message
        that followed them. The queue still carried them and the case that read
        the queue was green -- a note nobody can read is not a note.

        THE FOUR THINGS `/model` DOES, and only those. There the transcript
        stays on screen while the conversation empties, which is the honest
        picture: the chat is still in the rail with everything in it, and it is
        the CONTEXT that went.
        """
        spot = self._endpoint()
        if reset:
            self.push({"k": "note", "t": crow_core.MODEL_SWITCH_NOTE})
            if spot["remote"]:
                self.push({"k": "note", "t": crow_core.REMOTE_ENDPOINT_NOTE})
            self._conversation.reset()
            crow_core.forget_approvals()
            self._tools_cleared = 0
            self._context_tokens = 0
            self._promised_warm = False
        try:
            state, name, window = self._look(spot)
        except Exception as exc:           # noqa: BLE001 - shown, never raised
            self.push({"k": "down", "why": str(exc)[:120]})
            return
        self._model = name
        self._n_ctx = window
        self.push({"k": "up", "model": name, "n_ctx": self._n_ctx,
                   "tokens": self._context_tokens, "state": state,
                   "models": [[k, crow_core.model_label(k)]
                              for k in crow_core.bootable_models()],
                   "model_key": "" if spot["remote"] else crow_core.model_key_for(name),
                   "reasoning": self._reasoning or "",
                   "levels": [] if spot["remote"] else
                             list(crow_core.reasoning_levels_for(name)),
                   "groups": [] if spot["remote"] else
                             [list(g) for g in crow_core.reasoning_groups_for(name)]})

    def set_theme(self, name: str) -> bool:
        """The picker in Aussehen. True when the choice reached the disk.

        WRITTEN HERE AND READ AT START, in the same change: a setting that is
        only ever written is a setting nobody has proved comes back. The reader
        is `current_theme`, and the page is stamped from it above.
        """
        if name not in THEMES:
            return False
        doc = read_settings()
        doc["theme"] = name
        return write_settings(doc)

    def set_rail_open(self, open_: bool) -> bool:
        """#119. Remember whether the chat rail is folded away.

        WRITTEN HERE AND READ AT START, the rule `set_theme` above states: a
        setting only ever written is a setting nobody has proved comes back. The
        reader is `rail_open`, and the page is stamped from it before it is
        handed over -- the same trick the theme uses, and for the same reason. A
        rail that unfolded and then collapsed would do it on every single start,
        and that frame is exactly when somebody is looking.
        """
        doc = read_settings()
        doc["rail_open"] = bool(open_)
        return write_settings(doc)

    def set_code_open(self, open_: bool) -> bool:
        """#138. Remember whether the code panel is folded away.

        Written here, read by `code_open`, and stamped onto the element before
        the page is handed over -- the same three steps the rail takes, for the
        reason `set_rail_open` writes out above.
        """
        doc = read_settings()
        doc["code_open"] = bool(open_)
        return write_settings(doc)

    def code_width(self, px) -> bool:
        """#138. Remember how wide the code panel is. False when refused.

        CLAMPED AGAIN HERE, and that is not belt and braces: the page clamps a
        live gesture, this clamps what gets WRITTEN. A number that arrives out of
        bounds did not come from the grip, and storing it would hand the next
        start a panel nobody dragged.
        """
        try:
            width = int(px)
        except (TypeError, ValueError):
            return False
        if width < CODE_MIN or width > CODE_MAX:
            return False
        doc = read_settings()
        doc["code_width"] = width
        return write_settings(doc)

    def set_project_open(self, path: str, open_: bool) -> bool:
        """Remember one project row folded. Read back by `open_projects`.

        THE CLOSED ONES ARE THE LIST, so a project nobody has touched is open
        like every other new one, and a project removed and re-added comes back
        the way a new one does rather than carrying a state from before.
        """
        if not path:
            return False
        doc = read_settings()
        shut = [p for p in (doc.get("projects_shut") or []) if isinstance(p, str)]
        key = os.path.normcase(path)
        shut = [p for p in shut if os.path.normcase(p) != key]
        if not open_:
            shut.append(path)
        doc["projects_shut"] = shut
        ok = write_settings(doc)
        # REDRAWN FROM THE DISK, not toggled on the page. The rail is built from
        # one payload on purpose, and a page that folded a row on its own would
        # be the second place that knows which rows are folded -- the state the
        # settings file exists to be the only holder of. If the write failed,
        # this redraw is what puts the row back where it really is.
        self._reload_rail()
        return ok

    def on_drop(self, event) -> None:
        """A file was dropped on the window. Its real path goes to the page.

        THE PATH IS THE POINT, not the bytes. The model reads files with its own
        tools, so handing it a location costs one line and works for a 40 MB log
        exactly as it does for a note; copying the content in would spend the
        context window on something the model can fetch itself, and would have
        to invent a limit at which it stops.
        """
        files = ((event or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files
                 if isinstance(f, dict) and f.get("pywebviewFullPath")]
        self.push({"k": "drop", "paths": paths})

    def paste_clipboard(self) -> str:
        """Ctrl+V. Writes the picture on the clipboard down, returns its path.

        THE CLIPBOARD HAS NO PATH. A screenshot exists only as bytes, so there is
        nothing to hand over until somebody writes it down -- and the file has to
        outlive the turn, because the model may not read it until several turns
        later.

        "" IS THE ORDINARY ANSWER, not a failure: most of what people paste is
        text, and the page only reaches here when the clipboard carried none.
        """
        found = clipboard_image()
        return write_paste(*found) if found else ""

    def _mic_probe(self) -> None:
        """Whether dictation can run, answered off the opening path.

        `available()` asks PortAudio for the device list, and that starts an
        audio host. A window that waited for it would open slower for everyone,
        including every user who never presses the button.
        """
        self.push({"k": "mic", "state": "off", "blocked": crow_voice.available() or ""})

    def close(self) -> None:
        """Both copies of the open chat, then the window.

        `self._promised_warm` USED TO BE PASSED AS THE FOURTH ARGUMENT, and the
        fourth argument of `save_session` is `path`, not `with_kv`. So a warm
        cache made the call `save_session(..., path=True)`, which died inside
        `os.path.dirname(True)` and was swallowed by the `except` right below
        it: the better a session had been going, the more certainly it was
        never written down. A cold one silently took the other branch and wrote
        the server's KV slot on every single turn -- the one thing the core
        says to do once, on the way out.
        """
        INTERRUPT.set()
        try:
            if self._current_path:
                self._archive()            # the rail's copy, else a turn behind
        except Exception:                  # noqa: BLE001 - closing anyway
            pass
        try:
            self._persist_live(with_kv=True)
        except Exception:                  # noqa: BLE001 - closing anyway
            pass
        self._window.destroy()

    # -- #143 E2: the delegation snapshot ----------------------------------

    def _sub_adopt(self, old: str, new: str) -> None:
        """The parent stamp follows the chat's FILE, so a child can never lose
        its root. "" adopts only "" -- the live chat gaining its first file --
        and a real path matches by absolute path, for the archive move that
        relocates a chat. Nothing else is touched: a child re-hung under
        whatever is active was the wandering robin filmed on 2026-08-27.
        """
        for ident, parent in list(self._sub_parent.items()):
            if old == "":
                if parent == "":
                    self._sub_parent[ident] = new
            elif parent and os.path.abspath(parent) == os.path.abspath(old):
                self._sub_parent[ident] = new

    def _drop_chat_subtasks(self, parent: str) -> None:
        """#143-Nachtrag (robin, 2026-08-28 abends): ein geloeschter Chat
        nimmt seine Subtasks mit. Unstamped rows are stamped first, the way
        `_subs_items` would, so a delete before the first tick loses nothing
        to timing; "" is the live chat without a file, as everywhere."""
        for row in crow_core.subtask_view():
            self._sub_parent.setdefault(row["i"], self._current_path or "")
        want = os.path.abspath(parent) if parent else ""
        idents = [i for i, par in list(self._sub_parent.items())
                  if (os.path.abspath(par) if par else "") == want]
        crow_core.drop_subtasks(idents)
        for ident in idents:
            self._sub_parent.pop(ident, None)

    def _subs_items(self) -> list:
        """The core's subtask snapshot, dressed for the page.

        THE PARENT IS STAMPED AT FIRST SIGHT. A subtask is spawned by the chat
        that is live while its `delegate` runs, and that is the only moment
        anybody can say so -- recorded here, kept in `_sub_parent`, unchanged
        by every later chat switch. "" is the live chat without a file. The
        result is clipped to what the panel already shows for a tool answer:
        the full text is in the transcript, one click away.
        """
        items = []
        for row in crow_core.subtask_view():
            parent = self._sub_parent.setdefault(
                # 2026-08-28 spaetnachts: der Record bringt seinen Eltern-Chat
                # aus der Registry-Datei mit -- erst wenn er keinen hat, ist
                # es ein frisch gespawnter des offenen Chats.
                row["i"], row.get("parent") or (self._current_path or ""))
            # Und der Stempel wandert zurueck in den Record und auf die
            # Platte -- idempotent, heilt je Tick (Umzug, Umbenennung).
            crow_core.subtask_parent_set(row["i"], parent)
            if row["res"]:
                row["res"] = row["res"][:TOOL_RESULT_SHOWN]
            row["parent"] = parent
            # WHETHER THE CARD BELONGS IN THE OPEN FLOW, decided HERE and on
            # every snapshot anew. The page used to compare its own cached
            # `live` against `parent` -- two values frozen on two sides of the
            # seam at two different moments, and the mismatch drew no card at
            # all in a fresh chat (robin, 2026-08-27, screenshot 2). One side
            # computing both in the same breath cannot drift, and a wrong
            # frame heals on the next tick.
            row["here"] = parent == (self._current_path or "")
            items.append(row)
        return items

    def _push_subs(self) -> None:
        """One `subs` event per CHANGE, never per tick. The signature carries
        the clock's whole second, so a running subtask updates about once a
        second and a settled registry is silent."""
        items = self._subs_items()
        # `here` is in the signature: a chat switch flips it without touching
        # state or clock, and the page must hear about exactly that.
        sig = json.dumps([[r["i"], r["st"], int(r["s"]), r["tok"],
                           bool(r["path"]), r["here"]] for r in items])
        if sig != self._subs_sig:
            self._subs_sig = sig
            self.push({"k": "subs", "items": items})

    def _subs_ticker(self, stopped: "threading.Event") -> None:
        """Beside the turn, because the turn thread is the one that blocks in
        `collect`. It keeps ticking after the turn while anything still runs --
        a subtask that survives an interrupt keeps its card breathing -- and
        returns once the turn is over and the registry is quiet."""
        while True:
            self._push_subs()
            if stopped.is_set() and not crow_core.subtasks_running():
                return
            time.sleep(0.8)

    def _sub_share(self, before: "set") -> str:
        """The cost line's delegation share: THIS turn's subtasks, by token
        count and nothing else -- no money figure, robin's call 2026-08-27."""
        turn = [r for r in crow_core.subtask_view() if r["i"] not in before]
        if not turn:
            return ""
        remote = sum(r["tok"] for r in turn)
        noun = "subtask" if len(turn) == 1 else "subtasks"
        return "⑂ %d %s · %s tok remote" % (len(turn), noun,
                                            format(remote, ","))

    # -- the turn ----------------------------------------------------------

    def _token_budget(self) -> int:
        """#145: the turn's token cap from settings.json, and the subtask cap
        set on the way past. Read per turn, so the sheet needs no restart;
        nonsense reads as 0, and 0 is off -- the caps are opt-in."""
        doc = read_settings()
        try:
            crow_core.subtask_budget_set(int(doc.get("subtask_max_tokens") or 0))
        except (TypeError, ValueError):
            crow_core.subtask_budget_set(0)
        try:
            return max(0, int(doc.get("turn_token_budget") or 0))
        except (TypeError, ValueError):
            return 0

    def _pump(self, text: str) -> None:
        """#138c. Ein Zug nach dem anderen, auf einem Thread.

        DIE SCHLEIFE IST DER GANZE MECHANISMUS. `_run` endet erst nach dem
        Memory-Nachlauf; was in dieser Zeit getippt wurde, liegt in `_queued`
        und wird hier gefahren, statt eine zweite Taste zu brauchen.

        DAS FLAG FAELLT UNTER DEM LOCK, und das ist die tragende Zeile. Faende
        `send` den Puffer frei, nachdem diese Schleife ihn zuletzt gelesen hat,
        aber bevor sie sich abmeldet, dann legte es eine Zeile in einen Puffer,
        den niemand mehr liest -- von aussen ununterscheidbar von dem Fehler,
        den das hier behebt.

        UND ER FAELLT AUCH, WENN ES KRACHT. Ein Zug, der wirft, liesse das Flag
        sonst stehen; das Fenster naehme danach jede Zeile an und fuehre keine
        einzige mehr, und nur ein Neustart loeste das.
        """
        try:
            while True:
                self._run(text)
                with self._queue_lock:
                    text, self._queued = self._queued, None
                    if text is None:
                        self._busy = False
                        return
                INTERRUPT.clear()
                # DIE SEITE ERFAEHRT, DASS DAS WARTEN VORBEI IST. Sie steht
                # seit `send` auf gesperrt, aber mit dem Wartehinweis darunter;
                # ohne diese Zeile bliebe er ueber dem ganzen Zug stehen und
                # behauptete ein Warten, das laengst laeuft.
                self.push({"k": "busy"})
        except BaseException:
            with self._queue_lock:
                self._busy = False
                self._queued = None
            raise

    def _run(self, text: str) -> None:
        # #152, zweiter Akt -- robins Retest: der Kern prueft `should_roll`
        # erst am ENDE einer Runde. Eine Session, die schon UEBER der
        # Schwelle steht (die RT-Session: 200,2k von 200.192), scheitert
        # aber an der ERSTEN Anfrage (HTTP 400 exceed_context_size) und
        # erreicht den Check nie -- der Roll war unerreichbar. Wie repl()
        # rollt das Fenster deshalb VOR dem Turn: das Archiv ist eine
        # vollstaendige Konversation, und die getippte Zeile eroeffnet als
        # carry die neue -- darum unten KEIN zweites Append. Der Sink
        # existiert schon hier, damit der Roll dieselbe Notiz und dasselbe
        # Zaehler-Reset bekommt wie ein Mid-Turn-Roll.
        events = Turn(self.push)
        rolled = False
        if crow_core.should_roll(self._context_tokens, self._n_ctx,
                                 crow_core.ROLLOVER_AT):
            archived = crow_core.roll_over(
                self._conversation, self._endpoint()["base_url"],
                self._context_tokens, carry=text)
            if archived:
                events.rolled_over(self._context_tokens, archived)
                self._context_tokens = 0
                # Der neue Prefix hat keinen warmen Slot -- das Versprechen
                # waere eine Luege, die der naechste Turn bezahlt.
                self._promised_warm = False
                rolled = True
        # #121. THE LAST LINE OF DEFENCE FOR THE PIN, and it is here because
        # `_probe` has three ways to return before it reaches its own pin: the
        # endpoint would not answer, `--no-session`, or the session file could
        # not be read. The first of those is ordinary -- a window opened while
        # the server is still starting -- and until this line such a chat was
        # never pinned at all, so its memory never entered a single prompt. It
        # was silent by construction: an unpinned head is a VALID head, just one
        # without the memory in it. Found on 2026-08-21 in a session.json that
        # had no `memory` key after a full conversation.
        #
        # BEFORE THE USER MESSAGE IS APPENDED, so `pin_memory` still meets the
        # empty conversation `restore()` and `__init__` leave behind, and no
        # prefix exists yet to move. `pin_memory` refuses a second call, so the
        # guard is what keeps this from reaching past a pin already taken.
        if self._conversation.memory is None:
            self._pin_memory(self._current_path)
        # #142. STAGED IMAGES ARE CONSUMED HERE, ACCEPTED OR REFUSED, and the
        # gate runs BEFORE the append: a refused image must never enter the
        # history, or it rides the next, unrelated line. Refusal is /props'
        # answer (`refuse_images`), asked only of a local server -- a remote
        # provider answers for itself, with its own error, on its own bill.
        # Bei einem Vor-Turn-Roll reiste die Zeile schon als carry; gestagte
        # Bilder bleiben dann STEHEN (Chips sichtbar) und reiten die naechste
        # Zeile -- nichts verfaellt still.
        if not rolled:
            staged, self._staged_images = self._staged_images, []
            if staged:
                early = self._endpoint()
                refuse = (None if early["remote"]
                          else crow_core.refuse_images(early["base_url"]))
                if refuse:
                    self.push({"k": "fail", "t": refuse})
                    self.push({"k": "idle"})
                    return
                text = crow_core.user_content(text, [s["part"] for s in staged])
            self._conversation.append("user", text)
        # THE RAIL LEARNS THE CHAT EXISTS NOW, NOT AFTER THE TURN. Every other
        # caller of `_reload_rail` ends something, so an entry kept "new chat ·
        # no turn yet" beside a running turn. The title is the first user line,
        # knowable exactly here.
        self._reload_rail()
        # #112: RESOLVED PER TURN, NOT PER LAUNCH, and per model rather than
        # per client. `self._model` is what /props last reported; the core turns
        # that into the model's own four numbers, or into the three constants
        # when it has never heard of this server. Per turn because the window
        # outlives a server restart -- the endpoint can be pointed at a
        # different model while the window stays open, and a value cached at
        # launch would keep sending the old model's min_p.
        sampling = sampling_for(self._model)
        # RESOLVED ONCE FOR THE WHOLE TURN, and handed to BOTH senders below.
        # The reply is the one a person is waiting for; the review at the end is
        # the one that goes without being asked, with its own body and its own
        # Authorization header. Two resolutions could disagree the moment a
        # provider is switched mid-turn, and the one that would be wrong is the
        # one nobody is watching.
        spot = self._endpoint()
        # ONE BLOCK, READ BY BOTH SENDERS. Worked out once here rather than at
        # each call, because the whole point of the sticky key is that the turn
        # and the review that follows it carry the SAME one -- two expressions
        # would be two chances to drift, and the review is the one nobody sees.
        routing = crow_core.turn_routing(spot, self._args.session)
        if spot["remote"] and not spot["model"]:
            self.push({"k": "fail", "t": "%s has no model picked -- Settings, "
                                         "Model" % spot["label"]})
            self.push({"k": "idle"})
            return
        # #143 E2. THE TICKER RUNS BESIDE THE TURN, because the turn thread is
        # the one that blocks in `collect` -- nothing else could draw a card
        # while it waits. What was in the registry BEFORE the turn is kept so
        # the cost line can name exactly this turn's delegation share.
        sub_before = set(crow_core.SUBTASKS)
        sub_stop = threading.Event()
        threading.Thread(target=self._subs_ticker, args=(sub_stop,),
                         daemon=True).start()
        try:
            result = run_turn(
                self._conversation, base_url=spot["base_url"],
                model=spot["model"], api_key=spot["api_key"],
                extra_headers=spot.get("headers") or None,
                transport=spot.get("transport") or crow_core.TRANSPORT_CHAT,
                # A PROVIDER RESERVES AND PRICES THE MAXIMUM when the body names
                # no cap -- measured 2026-08-23, `HTTP 402 ... you requested up
                # to 65536 tokens, but can only afford 313`. The local server
                # reserves nothing, so it is sent nothing.
                max_tokens=crow_core.REMOTE_MAX_TOKENS if spot["remote"] else None,
                # THE SAME ANSWER THE CAP IS READ FROM, so the two can never
                # disagree about which endpoint this turn is going to.
                remote=spot["remote"],
                routing=routing,
                temperature=sampling["temperature"], top_p=sampling["top_p"],
                min_p=sampling["min_p"], top_k=sampling.get("top_k"),
                # #116: None sends nothing, which is the "never chosen" state.
                reasoning_effort=self._reasoning,
                timeout=READ_TIMEOUT_S, context_tokens=self._context_tokens,
                n_ctx=self._n_ctx, promised_warm=self._promised_warm,
                # #152: frisch je Turn, gesetzt allein vom Vor-Turn-Roll oben
                # -- nie aus Session-Zustand: als Dauer-True verweigerte der
                # Ein-Turn-Waechter jeden ZWEITEN Rollover der Sitzung,
                # stumm, bis der Server bei n_ctx ablehnte.
                rolled=rolled, execute_tools=self._args.execute_tools,
                mode=getattr(self._args, "mode", DEFAULT_MODE),
                approve=self._ask_page,
                # #145: the two opt-in caps, read from settings.json per turn so
                # a change in the sheet holds without a restart. The subtask cap
                # is module state like the root -- a delegation starts deep
                # inside the turn where no argument can reach it.
                token_budget=self._token_budget(),
                events=events)
        except CrowError as exc:
            # THE SENTENCE IS THE CORE'S, not a second copy of the rule. This
            # `except` is the rarer door -- `run_turn` reports a failed turn
            # through `turn_failed` and only escapes as an exception when it
            # fails outside the loop -- and both doors have to read alike.
            self.push({"k": "fail", "t": crow_core.failure_line(exc)})
            self.push({"k": "idle"})
            return
        except Exception as exc:           # noqa: BLE001 - a window survives its turns
            self.push({"k": "fail", "t": "%s: %s" % (type(exc).__name__, exc)})
            self.push({"k": "idle"})
            return
        finally:
            # The ticker outlives the turn only while something still runs --
            # a subtask that survives an interrupt keeps its card breathing.
            sub_stop.set()

        self._context_tokens = getattr(result, "context_tokens", self._context_tokens)
        self._promised_warm = getattr(result, "promised_warm", self._promised_warm)
        cost = getattr(result, "cost", None)
        line = cost.line() if cost is not None and getattr(cost, "rounds", 0) else ""
        self.push({"k": "cost", "line": "[" + line + "]" if line else "",
                   "share": events.share, "tokens": self._context_tokens,
                   "n_ctx": self._n_ctx,
                   # #143. This turn's delegation share, token counts only.
                   "sub": self._sub_share(sub_before) if line else ""})
        self._persist_live()
        # THE RAIL FOLLOWS THE TURN. A chat that had just been given its first
        # message went on calling itself "new chat" until something else
        # happened to redraw the list.
        self._reload_rail()
        self.push({"k": "idle"})
        # #122. AFTER `idle`, AND THAT ORDER IS THE FIX FOR A DEFECT robin found
        # live on 2026-08-21. The review sat inside `run_turn`, so the turn did
        # not end until it had thought about the whole conversation at the
        # chat's reasoning level: the answer stood complete on screen, the cost
        # line never came, and the composer still said `Stop`.
        #
        # WHAT A PERSON WAITS FOR IS THE ANSWER. Everything above has already
        # happened by the time this line runs -- cost, rail, idle -- so the
        # review costs the reader nothing but the slot, and the glow line
        # arrives on its own whenever it arrives.
        # TWICE PER WINDOW, NOT PER TURN (robin, 2026-08-21): "es soll ja auch
        # nicht jede neue Zeile ins MEMORY, sondern nur was wichtig ist pro
        # Unterhaltung". `review_due` answers with the share this turn crossed,
        # or None.
        #
        # THE MARK IS SET AND PERSISTED BEFORE THE REQUEST. A review that dies
        # on the endpoint has still used its slot; leaving the mark unset would
        # make it try again on the next turn, and the one after that, for the
        # rest of the window -- which is the every-turn behaviour this replaces,
        # arriving through the failure path.
        due = crow_core.review_due(self._context_tokens, self._n_ctx,
                                   self._conversation.reviewed)
        if due is not None and getattr(self._args, "review", True) \
                and not result.stopped and self._args.execute_tools:
            self._conversation.mark_reviewed(due)
            self._persist_live()
            crow_core.review_turn(
                self._conversation, base_url=spot["base_url"],
                model=spot["model"], api_key=spot["api_key"],
                extra_headers=spot.get("headers") or None,
                transport=spot.get("transport") or crow_core.TRANSPORT_CHAT,
                # A PROVIDER RESERVES AND PRICES THE MAXIMUM when the body names
                # no cap -- measured 2026-08-23, `HTTP 402 ... you requested up
                # to 65536 tokens, but can only afford 313`. The local server
                # reserves nothing, so it is sent nothing.
                max_tokens=crow_core.REMOTE_MAX_TOKENS if spot["remote"] else None,
                remote=spot["remote"],
                # THE SAME BLOCK THE TURN CARRIED, not a second one built here.
                routing=routing,
                temperature=sampling["temperature"], top_p=sampling["top_p"],
                min_p=sampling["min_p"], top_k=sampling.get("top_k"),
                reasoning_effort=self._reasoning,
                incidents=result.incidents,
                gate=getattr(self._args, "memory_approval",
                             crow_core.MEMORY_APPROVAL_DEFAULT),
                events=events)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crow in a window.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default="local-no-provider")
    parser.add_argument("--system", default=DEFAULT_SYSTEM)
    parser.add_argument("--no-session", dest="session", action="store_false",
                        default=True)
    # ON BY DEFAULT SINCE 2026-08-13, and the reason is the other client.
    # cli/crow.py runs tool calls unless told otherwise (--no-run-tools), so a
    # window that shows them instead answers the same question differently --
    # which is the failure mode #90 exists to exclude, not a safety margin. The
    # earlier default was off because behind a window nobody sees `run_command`
    # start a shell; driven live on 2026-08-13 that argument turned out to cut
    # the other way. A user who asks for a file gets a tool call and no answer,
    # every turn, with nothing on screen saying why -- and the chip that would
    # have said so is one nobody thinks to click.
    #
    # WHAT THIS DOES NOT CHANGE: there are still no permission levels. #88
    # (/mode manual, allowedit, auto) is what binds intent to permission, and it
    # binds BOTH clients or neither. Until it lands, the window runs what the
    # terminal runs and names the mode on its face in either state.
    parser.add_argument("--tools", dest="execute_tools", action="store_true",
                        default=True,
                        help="run tool calls (the default)")
    # #128. THE MEMORY GATE IS ON, and this flag is how it comes off.
    #
    # THE FLAG IS THE EXIT, NOT THE ENTRANCE -- see MEMORY_APPROVAL_DEFAULT for
    # why the usual "do not change existing behaviour" rule loses to it here. A
    # review that writes into the head of every later session while nobody is
    # at the keyboard is exactly the thing a person should have been asked
    # about, and a gate that has to be discovered protects nobody.
    parser.add_argument("--no-memory-approval", dest="memory_approval",
                        action="store_false",
                        default=crow_core.MEMORY_APPROVAL_DEFAULT,
                        help="let the review write to memory without asking")
    parser.add_argument("--no-review", dest="review", action="store_false",
                        help="do not let the model save memories after a turn")
    parser.add_argument("--no-tools", dest="execute_tools", action="store_false",
                        help="show tool calls instead of running them")
    # #88, the same flag the terminal client takes: the START level, with the
    # dropdown beside `send` as the same switch during a session.
    # DEFAULT None, NOT `auto`, and the terminal's parser says the same. It is
    # the only place that can tell "the user typed auto" from "the user typed
    # nothing", and #92 needs the difference: a level remembered for a working
    # directory fills a silence and never overrules a flag. `ready()` resolves
    # it through `adopt_root` before the first turn, so nothing downstream sees
    # None.
    parser.add_argument("--mode", choices=crow_core.MODES, default=None,
                        help="release level for tool calls: manual asks before writing"
                             " and executing, allowedit asks before executing, auto asks"
                             " for nothing (default, unless the working directory"
                             " remembers another)")
    parser.add_argument("--root", default=None,
                        help="the directory tool writes are confined to. States it AND"
                             " creates it, the same as picking a folder in the window")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # THE WINDOW FINDS THE SERVER THAT IS RUNNING. 8081 is 0731's port and was
    # the only one until a second model arrived on 8082; a window opened while
    # Qwen is up would otherwise knock on an empty port and say "no endpoint"
    # about a server the user can see running. An explicit --base-url is a
    # decision and is never overridden -- see running_base_url.
    if args.base_url == DEFAULT_BASE_URL:
        args.base_url = crow_core.running_base_url(args.base_url)
    try:
        import webview
    except ImportError:
        sys.stderr.write(
            "crow: this window needs pywebview.\n"
            "      pip install pywebview\n"
            "      The terminal client needs nothing: python cli/crow.py\n")
        return 2

    page = (PAGE.replace("__BG__", CROW_BG)
                .replace("__ACCENT__", CROW_ACCENT_HEX)
                .replace("__BEVEL__", BANNER_BEVEL_HEX)
                .replace("__TEXT__", CROW_TEXT_HEX)
                .replace("__TIMEOUT__", "%.0f" % READ_TIMEOUT_S)
                # ON THE ELEMENT BEFORE THE PAGE IS HANDED OVER, not applied by
                # a script after load. A window that painted itself dark and
                # then switched would show the wrong theme for a frame on every
                # single start -- and the frame is exactly the moment somebody
                # looks at it.
                .replace("__THEME__", current_theme())
                .replace("__MEMICON__", MEMORY_ICON)
                # #119. THE SAME REASON THE THEME IS STAMPED HERE: a rail that
                # was drawn open and then folded away by a script after load
                # would do it on every start, and that frame is the moment
                # somebody is looking at the window.
                .replace("__RAIL__", "open" if rail_open() else "shut")
                .replace("__CODE__", "open" if code_open() else "shut")
                .replace("__CODEW__", str(code_width_setting()))
                .replace("__MARKDARK__", mark_svg("dark"))
                .replace("__MARKLIGHT__", mark_svg("light")))

    api = Api(args)
    # #127. BEFORE THE WINDOW: the shell reads the application id when it
    # registers the taskbar button, and an id set afterwards is not looked at
    # again -- the same rule the style bits in `shell_buttons` run into.
    taskbar_identity()
    # FRAMELESS, because the title bar is part of the design: the caption is
    # drawn in the page with the wordmark in it, the way the mockup shows it.
    title = "CROW %s" % (client_version() or "")
    # DIE MINDESTBREITE IST DIE GARANTIE DER MASKE (robin, 2026-08-27, "zum
    # 10000000x"): selbst mit der Rail am Anschlag (RAIL_MAX 520) und jedem
    # erlaubten Code-Panel bleibt der Chatspalte ihr min-width von 560 --
    # 520 + 560 + 50 Spalten-Chrome = 1130. Das Code-Panel hat KEIN hartes
    # Minimum (min-width:0), es gibt zuerst nach; die Maske nie.
    window = webview.create_window(
        title, html=page, js_api=api,
        width=1180, height=800, min_size=(1130, 520), frameless=True,
        easy_drag=False, background_color=theme_bg(current_theme()))
    api._window = window
    threading.Thread(target=api.pump, daemon=True).start()
    # The styles can only be set once the window exists, so this runs as the
    # start-up callback rather than beside create_window.
    def styles(*_) -> None:
        # RETRIED, because one attempt is too early: at the moment this callback
        # first runs the window has no caption yet, so the search below skips it
        # and sets nothing. The loop stops the moment the style is in.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if shell_buttons(title):
                return
            time.sleep(0.2)

    # THE DROP EVENT IS SUBSCRIBED FROM PYTHON, and that is the only way to get
    # a path at all: the page receives a File with a name and no location, while
    # pywebview adds `pywebviewFullPath` on this side. Wired on `loaded` rather
    # than beside create_window, because window.dom needs a document.
    def wire_drop(*_) -> None:
        try:
            from webview.dom import DOMEventHandler

            window.dom.document.events.drop += DOMEventHandler(
                api.on_drop, prevent_default=True)
        except Exception:              # noqa: BLE001 - the window still works
            # SAID, NOT SWALLOWED SILENTLY: dropping is a convenience, and a
            # window that opens without it is still a window. The page keeps its
            # own dragover guard either way, so a file never navigates it away.
            api.push({"k": "note", "t": "dropping files is unavailable in this "
                                        "pywebview build -- typing a path works"})

    window.events.loaded += wire_drop
    webview.start(styles, window)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
