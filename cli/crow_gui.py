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
import threading
import time

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


def client_version(path: str | None = None) -> str:
    """The client version, read out of cli/crow.py. "" when unreadable."""
    try:
        with open(path or os.path.join(HERE, "crow.py"), encoding="utf-8") as fh:
            found = _VERSION_LITERAL.search(fh.read())
    except OSError:
        return ""
    return found.group(1) if found else ""


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
#wbtns{margin-left:auto;display:flex;-webkit-app-region:no-drag}
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
#settings .sheet{width:min(760px,92vw);height:min(560px,88vh);display:flex;
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
#rail{width:242px;flex:none;
  background:var(--rail);display:flex;flex-direction:column;min-height:0}
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
#railtoggle{font:inherit;color:var(--dimmer);background:transparent;
  border:1px solid transparent;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;padding:3px 5px;margin-right:2px}
#railtoggle:hover{color:var(--accent);border-color:var(--bevel)}

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
#main{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;
  background:var(--bg);border-top-left-radius:12px;overflow:hidden;
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
#flow{overflow-y:auto;padding:22px 0 26px;flex:1;min-height:0;
  scroll-behavior:smooth;user-select:text;scrollbar-gutter:stable}
/* CENTRED, NOT LEFT-HUGGING. max-width alone pins the column to the left edge
   and leaves the rest of a wide window empty; the auto margins are what put it
   in the middle. 960 includes the 30px padding, so the text runs 900 wide --
   the same 900 #box is held to, which is what makes the two flush. */
.turn{padding:0 30px;max-width:960px;margin-inline:auto}
.turn+.turn{margin-top:26px}
.you{display:grid;grid-template-columns:38px 1fr;gap:2px}
.you .m{color:var(--accent);font-weight:700;font-size:12.5px;padding-top:1px}
.you .txt{color:var(--text);white-space:pre-wrap}
.as{display:grid;grid-template-columns:38px 1fr;gap:2px}
.as .m{color:var(--bevel);padding-top:1px}
.col{min-width:0}

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
.copy{margin-left:auto;font:inherit;font-size:11px;color:var(--dimmer);
  background:transparent;border:1px solid var(--line);border-radius:5px;
  padding:1.5px 9px;cursor:pointer}
.copy:hover{border-color:var(--bevel);color:var(--accent)}
.copy.done{color:var(--ok);border-color:rgba(78,201,143,.4)}
.code pre{margin:0;padding:11px 13px;overflow-x:auto;font-size:12px;
  line-height:1.6;color:var(--code);user-select:text;font-family:var(--mono)}
/* Anything that names a path, a flag or a symbol is code and keeps the mono
   stack; the body around it does not. */
code,.asktop code,#url,.cost{font-family:var(--mono)}
.cost{margin-top:11px;font-size:10.5px;color:var(--dimmer);
  border-top:1px dashed var(--line);padding-top:7px;overflow-x:auto;
  white-space:nowrap}
.fail{color:var(--bad);font-size:11.5px;margin-top:8px}
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
/* #122. THE MEMORY LINE, AND IT IS NOT A NOTE. A note is grey because what
   notes say may be skimmed past; this one is the only sign a person gets that
   something entered the head of their next session, and with no approval gate
   in front of it there is no second chance to notice.
   THE GLOW RUNS ONCE AND SETTLES. `forwards` on both animations is the whole
   trick: the gradient sweeps across on arrival, the halo fades out, and what
   is left afterwards is a quiet accent-tinted row. A glow that kept pulsing
   would be a thing to switch off, and this line may not be switchable.
   THE COLOURS COME FROM THE PALETTE, never from a literal, so all three themes
   answer for it -- `--accent` is the brand value the core hands in. */
.memnote{font-size:11.5px;white-space:pre-wrap;color:var(--accent);
  padding:3px 8px;border-radius:6px;border:1px solid transparent;
  background:linear-gradient(90deg,transparent 0%,color-mix(in srgb,var(--accent) 22%,transparent) 50%,transparent 100%);
  background-size:220% 100%;
  animation:memsweep .9s ease-out 1 forwards, memglow 1.6s ease-out 1 forwards}
@keyframes memsweep{from{background-position:120% 0}to{background-position:-40% 0}}
@keyframes memglow{
  0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 45%,transparent)}
  35%{box-shadow:0 0 14px 2px color-mix(in srgb,var(--accent) 38%,transparent)}
  100%{box-shadow:0 0 0 0 transparent}}
/* A reader who asked the system not to animate gets the colour and no motion.
   The row still has to be visible -- so the sweep is replaced by a flat tint,
   not by nothing. */
@media (prefers-reduced-motion: reduce){
  .memnote{animation:none;
    background:color-mix(in srgb,var(--accent) 14%,transparent)}}
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
  padding:26px 30px 14px;
  background:linear-gradient(to bottom,transparent,var(--bg) 26px)}
#box{border:1px solid var(--bevel);border-radius:8px;background:var(--panel);
  padding:9px 11px 8px;box-shadow:0 0 0 3px rgba(126,176,248,.06);
  transition:border-color .15s ease,box-shadow .15s ease;
  /* 900, not 960: .turn spends 30px of its 960 on padding either side, so its
     text starts at 900 wide. Matching that here puts this box's border on the
     same edge as the text above it. */
  max-width:900px;margin-inline:auto}
#box.focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(126,176,248,.13)}
/* THE WHOLE BOX, NOT A SEPARATE ZONE. A drop target that is smaller than the
   thing it looks like is a target people miss; the window takes a file anywhere
   and the box is what says so. */
#box.drag{border-color:var(--accent);background:rgba(126,176,248,.07);
  box-shadow:0 0 0 3px rgba(126,176,248,.20)}
/* NO PROMPT MARK. `you>` named the typist in a box only the typist can type
   in; the turn above already carries it where it says something. The gap
   went with it -- one child has nothing to be spaced from. */
#line{display:flex;align-items:flex-start}
#in{flex:1;background:transparent;border:0;outline:0;resize:none;color:var(--text);
  font:inherit;font-size:13px;line-height:1.5;max-height:140px;user-select:text}
#in::placeholder{color:var(--dimmer)}
#foot{display:flex;align-items:center;gap:10px;margin-top:9px;font-size:11px;
  color:var(--dimmer)}
#ctx{font-size:11.5px;white-space:nowrap}
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
#acts{margin-left:auto;display:flex;gap:8px;align-items:stretch}
/* CENTRED, NOT STRETCHED: it is a bare word with no border to line up, and a
   stretched span puts its text at the top of the box instead of on the row. */
#hint{color:var(--dimmer);font-size:10.5px;align-self:center}
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
#model{border-radius:6px;padding:3px 11px;font-size:11.5px;gap:0}
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
</style></head><body data-rail="__RAIL__">

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
        <button data-cat="providers" onclick="crow.settingsCat('providers')">Other providers</button>
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
          <p class="shint">Tool servers Crow could borrow tools from, the way it
             borrows none today: everything it can call is built in.</p>
          <p class="empty">Coming soon.</p>
        </section>
        <section data-cat="providers" hidden>
          <h3>Other providers</h3>
          <p class="shint">Keys for models that are not on this machine —
             Anthropic, OpenAI, OpenRouter and the rest. Crow talks to one local
             endpoint today; this is where a second, remote one would be named.</p>
          <p class="empty">Coming soon.</p>
        </section>
        <section data-cat="about" hidden>
          <h3>About</h3>
          <p class="about">CROW <span id="aboutver"></span></p>
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
  <div id="main">
    <!-- TWO CHIPS, AND THE ADDRESS IS NOT ONE OF THEM. The bar carried five:
         state, model, level, n_ctx and the base URL. Three of them said things
         that belong where the typing happens -- the model and its level are a
         choice, and the window size is already the denominator of the context
         readout in the composer. The URL is not a choice at all: it is the one
         fact you look up when something is wrong, so it is the connected
         chip's title and costs no width until asked for. -->
    <div id="flow"></div>
    <div id="composer">
      <div id="pendbar" hidden onclick="crow.pendToggle(event)"></div>
      <div id="box">
        <div id="line"><textarea id="in" rows="1"
            placeholder="Message, or /tools for what the model can call"></textarea></div>
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
</div>

<script>
const $ = s => document.querySelector(s);
const flow = $("#flow"), input = $("#in"), go = $("#go"), box = $("#box");

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
    const t=this.turn(""); t.innerHTML=
      '<div class="you"><span class="m">you&gt;</span><div class="txt"></div></div>';
    t.querySelector(".txt").textContent=text; this.bottom();
  },

  start(){
    const t=this.turn("");
    t.innerHTML='<div class="as"><span class="m">&#9679;</span><div class="col"></div></div>';
    this.col=t.querySelector(".col"); this.say=null; this.think=null;
    this.fence=null; this.blocks=[];
    // ONE CURSOR IN THE WHOLE FLOW. `reply_started` fires once per ROUND, not
    // once per turn, so a turn with a tool call opens two -- and the first was
    // left blinking in a finished answer. Every existing one goes before a new
    // one is made, which also covers the round that ended without an idle.
    document.querySelectorAll(".cursor").forEach(c=>c.remove());
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

  codeOpen(lang){
    this.say=null; this.fenceLang=lang||"code";
    const d=document.createElement("div"); d.className="code";
    d.innerHTML='<div class="hd"><span class="lang"></span>'+
      '<button class="copy">copy</button></div><pre></pre>';
    d.querySelector(".lang").textContent=this.fenceLang;
    const pre=d.querySelector("pre"), btn=d.querySelector(".copy");
    // THROUGH PYTHON, NOT navigator.clipboard. The page is handed to WebView2 as
    // HTML rather than served over https, so it is not a secure context and the
    // async clipboard API silently refuses -- the button said "copied" over an
    // empty clipboard, which is worse than a button that does nothing.
    btn.onclick=()=>{ if(!pre.textContent){ btn.textContent="empty"; return; }
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
    this.fence=null;
  },

  tool(name,args){
    const d=document.createElement("div"); d.className="tool";
    d.innerHTML='<div class="hd"><span class="ico">&#9679;</span>'+
      '<span class="name"></span><span class="arg"></span>'+
      '<span class="note"></span></div>';
    d.querySelector(".note").textContent =
      this.execute ? "ran" : "shown, not run";
    d.querySelector(".name").textContent=name;
    d.querySelector(".arg").textContent=args||"";
    this.col.insertBefore(d,this.cursor); this.bottom();
  },

  cost(line,share){
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
      d.textContent=line; this.col.appendChild(d); }
    this.bottom();
  },

  fail(msg){ const d=document.createElement("div"); d.className="fail";
    d.textContent=msg; (this.col||flow).appendChild(d); this.bottom(); },
  note(msg){ const t=this.turn(""); const d=document.createElement("div");
    d.className="note"; d.textContent=msg; t.appendChild(d); this.bottom(); },
  alarm(msg){ const t=this.turn(""); const d=document.createElement("div");
    d.className="alarm"; d.textContent=msg; t.appendChild(d); this.bottom(); },
  // #122. THE ONLY NOTICE THAT SOMETHING WAS REMEMBERED. There is no approval
  // gate, so this line is where a person finds out -- it glows once on arrival
  // and then sits there like any other row.
  memory(msg,n){ const t=this.turn(""); const d=document.createElement("div");
    d.className="memnote"; d.textContent = n>1 ? msg+" ("+n+")" : msg;
    t.appendChild(d); this.bottom(); },

  // THE CURSOR ALWAYS SITS LAST. Every insert goes BEFORE it, so after a tool
  // row or a code frame it has to be moved back to the end -- otherwise it is
  // stranded above whatever arrived next, blinking in the middle of the answer.
  tail(){ if(this.cursor && this.col) this.col.appendChild(this.cursor); },
  bottom(){ this.tail(); flow.scrollTop=flow.scrollHeight; },

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
  toggleSkill(name,row,sw){
    // PAINTED FIRST, WRITTEN SECOND, the way `setTheme` does it: the write is a
    // file, and a click that waits for one feels broken.
    const on=!sw.classList.contains("on");
    sw.classList.toggle("on",on);
    row.classList.toggle("off",!on);
    sw.title=on?"in the prompt":"not in the prompt";
    pywebview.api.toggle_skill(name,on);
  },
  closeSettings(){ $("#settings").hidden=true; },
  // THE BACKDROP CLOSES, THE SHEET DOES NOT. Without the target test a click on
  // anything inside the panel bubbles up here and shuts it.
  settingsBackdrop(e){ if(e.target.id==="settings") this.closeSettings(); },

  settingsCat(name){
    // #126. THE KEY IS ON THE BUTTON, not in a list beside it. It used to be a
    // positional array -- a fifth button with a four-name list would mark the
    // wrong tab, and the fault would look like a CSS problem. The panes below
    // were already keyed this way; now both halves read the same attribute.
    document.querySelectorAll("#scats button").forEach(
      b => b.classList.toggle("on", b.dataset.cat===name));
    document.querySelectorAll("#spane section").forEach(
      sec => sec.hidden = sec.dataset.cat!==name);
  },

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
    // QUOTED WHEN IT HAS TO BE. A Windows path with a space in it is the normal
    // case, not the exception, and an unquoted one is two arguments to whatever
    // reads the line next.
    this.attach(paths.map(p => /\s/.test(p) ? '"'+p+'"' : p).join(" "));
  },

  idle(){ this.running=false; go.textContent="↑"; go.classList.remove("stop");
    $("#turnstate").textContent=""; $("#hint").textContent="";
    document.querySelectorAll(".cursor").forEach(c=>c.remove());
    this.cursor=null; },

  busy(){ this.running=true; go.textContent="■ Stop"; go.classList.add("stop");
    $("#turnstate").textContent="…";
    $("#hint").textContent="read timeout __TIMEOUT__ s"; },

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
  go(){ if(this.running){ pywebview.api.stop(); return; }
    const text=input.value.trim(); if(!text) return;
    input.value=""; input.style.height="auto";
    this.user(text); this.running=true;
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
    const box=$("#sessions");
    // SAME CHATS, SAME ORDER -> MOVE THE MARK, DO NOT REBUILD. Every update used
    // to throw the list away and remake it, so a click exchanged every node under
    // the cursor.
    //
    // THE PROJECTS ARE IN THE SHAPE, and every chat's ROOT with them. Without
    // that, folding a project or moving a chat into one would land on the fast
    // path and change nothing on screen -- the list would be right in Python and
    // stale in the window, which is the worst of the three possible states.
    const shape=(rollovers||[]).map(r=>(r.path||"")+">"+(r.root||"")).join("\n")
      +"|"+(unsaved?"live":"")+"|"+this.liveRoot
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
        this.pendState([]); break;
      case "hello": this.hello(e.t); break;
      // #94. /thoughts in the terminal shows or hides the reasoning; here it is
      // always rendered and folded, so the same question is open-or-closed.
      // EVERY block, not just the ones on screen -- a fold that only reached
      // the last answer would read as broken on the one above it.
      case "thoughts": document.querySelectorAll("details.think")
        .forEach(d=>{ d.open=e.open; }); break;
      case "user": this.user(e.t); break;
      case "start": this.start(); break;
      case "think_open": this.thinkOpen(); break;
      case "think": this.thinkText(e.t); break;
      case "think_close": this.thinkClose(); break;
      case "text": this.answer(e.t); break;
      case "code_open": this.codeOpen(e.lang); break;
      case "code_close": this.codeClose(e.closed); break;
      case "tool": this.tool(e.name,e.args); break;
      case "cost": this.cost(e.line,e.share); this.ctx(e.tokens,e.n_ctx); break;
      case "note": this.note(e.t); break;
      case "memory": this.memory(e.t,e.n); break;
      case "alarm": this.alarm(e.t); break;
      case "fail": this.fail(e.t); break;
      case "live":
        $("#turnstate").textContent =
          e.n + " tok · " + e.rate.toFixed(1) + " tok/s";
        break;
      case "pend": this.pendState(e.items); break;
      case "mic": this.micState(e); break;
      case "drop": this.dropped(e.paths); break;
      case "idle": this.idle(); break;
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
                 ["#modewrap","#modemenu"], ["#rootwrap","#rootmenu"]];
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
        self._deltas = 0
        self._started = 0.0
        self._last = 0.0

    def reply_started(self) -> None:
        self._put({"k": "start"})
        self._fences = CodeFences(self)
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

    # -- FenceEvents: what the core decided, drawn --------------------------

    def prose(self, piece: str) -> None:
        self._put({"k": "text", "t": piece})

    def code_started(self, language: str) -> None:
        self._put({"k": "code_open", "lang": language or ""})

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
        self._put({"k": "tool", "name": name,
                   "args": crow_core.format_tool_args(arguments)})

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
                        "run_command is not bounded by it"})

    def tools_reported(self, calls: list) -> None:
        for call in calls or []:
            self.tool_started(call.get("name", "?"), call.get("arguments", ""))

    def rolled_over(self, tokens: int, path: str) -> None:
        self._put({"k": "note", "t": "rolled over at %d tokens -> %s"
                                     % (tokens, os.path.basename(path))})

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
        self._conversation = Conversation(args.system)
        self._context_tokens = 0
        self._n_ctx = 0
        # What /props last said the server has open. Kept beside _n_ctx and for
        # the same reason: both are answers from the endpoint that the session
        # path needs later, and asking twice would be two answers to one
        # question. Empty until _probe has run -- #113 treats that as "unknown",
        # which drops a cache rather than restoring the wrong one.
        self._model = ""
        # #116. The chat's thinking level, and `None` is a value: "never chosen",
        # which sends no `reasoning_effort` at all and keeps the prompt
        # byte-identical to a window that predates the slider. Bound from the
        # session file once the model is known, because which levels are legal
        # is the model's answer, not this window's.
        self._reasoning: str | None = None
        self._promised_warm = False
        self._rolled = False
        self._worker: threading.Thread | None = None
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

    def ready(self) -> None:
        self.push({"k": "meta", "version": client_version() or "",
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
        for message in messages or []:
            if message.get("role") == "user":
                first = (message.get("content") or "").strip().splitlines()
                if first and first[0]:
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

    def _probe(self) -> None:
        try:
            state = check_endpoint(self._args.base_url)
            name = model_display_name(fetch_model_name(self._args.base_url))
            self._model = name
            self._n_ctx = fetch_n_ctx(self._args.base_url)
            # #116. BOUND HERE BECAUSE THIS IS WHERE THE MODEL BECOMES KNOWN,
            # and which levels are legal is the model's answer. A level the new
            # model does not take comes back as None with a line, and is left in
            # the file untouched -- the user may go back to the model it was
            # valid for.
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
                       "model_key": crow_core.model_key_for(name),
                       "reasoning": self._reasoning or "",
                       "levels": list(crow_core.reasoning_levels_for(name)),
                       # #117. Which of those levels render the SAME prompt, measured. Empty
                       # means unmeasured, and the page collapses nothing on an empty list.
                       "groups": [list(g) for g in crow_core.reasoning_groups_for(name)]})
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
                self._args.base_url,
                crow_core.system_with_memory(self._args.system,
                                             crow_core.session_memory(SESSION_FILE)),
                model=self._model)
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
        "/mode": "the release level; /mode manual|allowedit|auto to switch.",
        "/model": "the model that is up; /model <key> restarts on another one.",
        "/reasoning": "this chat's thinking level; /reasoning <level>|off to set it.",
        "/thoughts": "fold the reasoning blocks open, or closed again.",
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
        self.close()          # /exit, /quit
        return "closing."

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
        said, url, switched = crow_core.model_command(
            " ".join(rest), self._args.base_url,
            log=lambda msg: self.push({"k": "note", "t": msg}))
        if not switched:
            return said
        # THE SAME FOUR THINGS `/reset` DOES, because the cache the context was
        # cheap against belonged to a process that no longer exists. The chat
        # stays in the rail with everything in it; only the context goes.
        self._conversation.reset()
        crow_core.forget_approvals()
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
        # A LINE ARRIVING MID-TURN IS ALSO NOT A NEW TURN. The page keeps a
        # second one out on its own, but saying so here is what lets it unlock
        # if it ever gets that wrong -- the guard belongs to the half that knows.
        if self._worker and self._worker.is_alive():
            return False
        INTERRUPT.clear()
        self._worker = threading.Thread(target=self._run, args=(text,), daemon=True)
        self._worker.start()
        return True

    def stop(self) -> None:
        INTERRUPT.set()

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
        out = []
        for name in crow_core.MODES:
            asks = [t for t in sorted(crow_core.TOOL_IMPL)
                    if crow_core.needs_approval(t, name)]
            out.append({"name": name,
                        "what": ("asks before " + ", ".join(asks)) if asks
                                else "every tool runs unasked"})
        return out

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
                         self._context_tokens, path=path, with_kv=False)
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
            save_session(self._conversation, self._args.base_url,
                         self._context_tokens, with_kv=with_kv,
                         model=self._model, reasoning=self._reasoning)
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
                self._args.base_url,
                crow_core.system_with_memory(self._args.system,
                                             crow_core.session_memory(path)),
                path, model=self._model)
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
        self.push({"k": "clear"})     # the page no longer guesses; see crow.open
        self._hello()
        self._replay(messages)
        # SESSION.JSON FOLLOWS THE SWITCH AT ONCE. Still pointing at the chat
        # just closed, a window shut before the next turn would come back up
        # holding it -- and list the chat the user was actually reading as a
        # second entry beside it.
        self._persist_live()
        self._reload_rail()
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
        for message in messages:
            role = message.get("role")
            body = (message.get("content") or "")
            if role == "user":
                if body.strip():
                    self.push({"k": "user", "t": body})
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

    # -- the turn ----------------------------------------------------------

    def _run(self, text: str) -> None:
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
        self._conversation.append("user", text)
        # THE RAIL LEARNS THE CHAT EXISTS NOW, NOT AFTER THE TURN. Every other
        # caller of `_reload_rail` ends something, so an entry kept "new chat ·
        # no turn yet" beside a running turn. The title is the first user line,
        # knowable exactly here.
        self._reload_rail()
        events = Turn(self.push)
        # #112: RESOLVED PER TURN, NOT PER LAUNCH, and per model rather than
        # per client. `self._model` is what /props last reported; the core turns
        # that into the model's own four numbers, or into the three constants
        # when it has never heard of this server. Per turn because the window
        # outlives a server restart -- the endpoint can be pointed at a
        # different model while the window stays open, and a value cached at
        # launch would keep sending the old model's min_p.
        sampling = sampling_for(self._model)
        try:
            result = run_turn(
                self._conversation, base_url=self._args.base_url,
                model=self._args.model, api_key=self._args.api_key,
                temperature=sampling["temperature"], top_p=sampling["top_p"],
                min_p=sampling["min_p"], top_k=sampling.get("top_k"),
                # #116: None sends nothing, which is the "never chosen" state.
                reasoning_effort=self._reasoning,
                timeout=READ_TIMEOUT_S, context_tokens=self._context_tokens,
                n_ctx=self._n_ctx, promised_warm=self._promised_warm,
                rolled=self._rolled, execute_tools=self._args.execute_tools,
                mode=getattr(self._args, "mode", DEFAULT_MODE),
                approve=self._ask_page,
                events=events)
        except CrowError as exc:
            self.push({"k": "fail", "t": str(exc)})
            self.push({"k": "idle"})
            return
        except Exception as exc:           # noqa: BLE001 - a window survives its turns
            self.push({"k": "fail", "t": "%s: %s" % (type(exc).__name__, exc)})
            self.push({"k": "idle"})
            return

        self._context_tokens = getattr(result, "context_tokens", self._context_tokens)
        self._promised_warm = getattr(result, "promised_warm", self._promised_warm)
        self._rolled = getattr(result, "rolled", self._rolled)
        cost = getattr(result, "cost", None)
        line = cost.line() if cost is not None and getattr(cost, "rounds", 0) else ""
        self.push({"k": "cost", "line": "[" + line + "]" if line else "",
                   "share": events.share, "tokens": self._context_tokens,
                   "n_ctx": self._n_ctx})
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
                self._conversation, base_url=self._args.base_url,
                model=self._args.model, api_key=self._args.api_key,
                temperature=sampling["temperature"], top_p=sampling["top_p"],
                min_p=sampling["min_p"], top_k=sampling.get("top_k"),
                reasoning_effort=self._reasoning,
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
                # #119. THE SAME REASON THE THEME IS STAMPED HERE: a rail that
                # was drawn open and then folded away by a script after load
                # would do it on every start, and that frame is the moment
                # somebody is looking at the window.
                .replace("__RAIL__", "open" if rail_open() else "shut")
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
    window = webview.create_window(
        title, html=page, js_api=api,
        width=1180, height=800, min_size=(760, 520), frameless=True,
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
