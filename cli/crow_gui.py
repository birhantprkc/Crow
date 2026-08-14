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
import json
import os
import queue
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import crow_core  # noqa: E402

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
    MIN_P,
    model_display_name,
    ReplyEvents,
    run_turn,
    save_session,
    SESSION_FILE,
    TEMPERATURE,
    TOOLS,
    TOP_P,
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
<html lang="de"><head><meta charset="utf-8">
<style>
:root{
  --bg:__BG__; --accent:__ACCENT__; --bevel:__BEVEL__; --model:__TEXT__;
  --panel:#0e1220; --raised:#131829; --line:#1c2438; --line-soft:#161d2e;
  --dim:#6d7b95; --dimmer:#4a566d;
  --ok:#4ec98f; --warn:#e3b341; --bad:#f0655a;
  --mono:"Google Sans Code",ui-monospace,"Cascadia Mono",Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden}
body{background:var(--bg);color:var(--dim);font:13px/1.55 var(--mono);
  -webkit-font-smoothing:antialiased;display:flex;flex-direction:column;
  user-select:none}
::-webkit-scrollbar{width:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:99px}
::-webkit-scrollbar-thumb:hover{background:var(--bevel)}

/* -- title bar: ours, because the frame is off ------------------------- */
/* THE CAPTION DRAGS THE WINDOW, and the hook is pywebview's own class.
   `-webkit-app-region: drag` is Electron syntax; WebView2 ignores it, which
   is why the first frameless build could not be moved at all. */
#bar{display:flex;align-items:center;gap:10px;height:34px;flex:none;
  padding:0 0 0 13px;background:linear-gradient(180deg,#101528,var(--bg));
  border-bottom:1px solid var(--line)}
#mark,#ver{pointer-events:none}
#mark{font-weight:700;letter-spacing:.22em;font-size:11.5px;color:var(--accent)}
#mark span{color:var(--bevel)}
#ver{font-size:10.5px;color:var(--dimmer);letter-spacing:.04em}
#wbtns{margin-left:auto;display:flex;-webkit-app-region:no-drag}
/* The buttons sit inside the drag region, so they opt out of it again --
   without this a click on 'close' starts a drag instead of closing. */
.wb{width:42px;height:33px;display:grid;place-items:center;color:var(--dimmer);
  font-size:11px;cursor:default}
.wb:hover{background:#161d2e;color:#c8d4e8}
.wb.close:hover{background:#8b2b26;color:#fff}

#body{display:flex;flex:1;min-height:0}

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
#rail{width:242px;flex:none;border-right:1px solid var(--line);
  background:var(--panel);display:flex;flex-direction:column;min-height:0}
#railhead{display:flex;align-items:center;padding:11px 12px 9px;
  border-bottom:1px solid var(--line-soft)}
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
.sess .t{font-size:12px;color:#9fb0c9;display:block;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.sess .s{font-size:10.5px;color:var(--dimmer);display:block;margin-top:1px}
/* -- context menu -------------------------------------------------------- */
#menu{position:fixed;z-index:200;display:none;min-width:168px;padding:4px;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;
  box-shadow:0 12px 34px -10px rgba(0,0,0,.75)}
#menu.on{display:block}
#menu button{display:block;width:100%;text-align:left;font:inherit;
  font-size:11.5px;color:var(--dim);background:transparent;border:0;
  padding:6px 10px;border-radius:5px;cursor:pointer}
#menu button:hover{background:var(--raised);color:#c8d4e8}
#menu button.danger:hover{background:rgba(240,101,90,.14);color:#ffd9d4}
#menu .sep{height:1px;background:var(--line-soft);margin:4px 2px}
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
#main{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}
#status{display:flex;align-items:center;gap:9px;padding:8px 16px;flex:none;
  border-bottom:1px solid var(--line);background:rgba(14,18,32,.72);
  font-size:11.5px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;color:var(--dim);
  border:1px solid var(--line);border-radius:999px;padding:2px 10px;
  white-space:nowrap}
.chip b{font-weight:500;color:#a9bad3}
#dot{width:6px;height:6px;border-radius:50%;background:var(--dimmer)}
#dot.up{background:var(--ok);box-shadow:0 0 0 3px rgba(78,201,143,.14)}
#dot.down{background:var(--bad);box-shadow:0 0 0 3px rgba(240,101,90,.14)}
.chip.ghost{border-color:transparent;padding-left:2px}
#tools{cursor:pointer;transition:color .15s,border-color .15s}
#tools:hover{border-color:var(--bevel)}
#right{margin-left:auto;display:flex;gap:9px;align-items:center}

#flow{overflow-y:auto;padding:22px 0 26px;flex:1;min-height:0;
  scroll-behavior:smooth;user-select:text}
/* CENTRED, NOT LEFT-HUGGING. max-width alone pins the column to the left edge
   and leaves the rest of a wide window empty; the auto margins are what put it
   in the middle. 960 includes the 30px padding, so the text runs 900 wide --
   the same 900 #box is held to, which is what makes the two flush. */
.turn{padding:0 30px;max-width:960px;margin-inline:auto}
.turn+.turn{margin-top:26px}
.you{display:grid;grid-template-columns:38px 1fr;gap:2px}
.you .m{color:var(--accent);font-weight:700;font-size:12.5px;padding-top:1px}
.you .txt{color:#cfdaea;white-space:pre-wrap}
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
  color:#7b89a3;font-size:12px;line-height:1.65;background:rgba(19,24,41,.4);
  border-radius:0 6px 6px 0;white-space:pre-wrap}
.say{color:var(--model);line-height:1.62;white-space:pre-wrap}
.tool{margin:11px 0;border:1px solid var(--line);border-radius:8px;
  background:var(--panel);overflow:hidden}
.tool .hd{display:flex;align-items:center;gap:9px;padding:6px 11px;
  font-size:11.5px}
.tool .ico{color:var(--warn);font-size:9px}
.tool .name{color:#a9bad3}
.tool .arg{color:var(--dimmer);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.tool .note{margin-left:auto;color:var(--dimmer);white-space:nowrap}
.code{margin:12px 0;border:1px solid var(--line);border-radius:8px;
  background:#080b13;overflow:hidden}
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
  line-height:1.6;color:#c3d0e4;user-select:text}
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
.cursor{display:inline-block;width:7px;height:14px;background:var(--accent);
  vertical-align:-2px;margin-left:2px;animation:bl 1s steps(1,end) infinite}
@keyframes bl{50%{opacity:0}}

/* -- composer: the rounded, lifted box --------------------------------- */
#composer{flex:none;border-top:1px solid var(--line);padding:12px 30px 14px;
  background:rgba(11,14,23,.9)}
#box{border:1px solid var(--bevel);border-radius:8px;background:var(--panel);
  padding:9px 11px 8px;box-shadow:0 0 0 3px rgba(126,176,248,.06);
  transition:border-color .15s ease,box-shadow .15s ease;
  /* 900, not 960: .turn spends 30px of its 960 on padding either side, so its
     text starts at 900 wide. Matching that here puts this box's border on the
     same edge as the text above it. The composer's top rule stays full width. */
  max-width:900px;margin-inline:auto}
#box.focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(126,176,248,.13)}
#line{display:flex;align-items:flex-start;gap:8px}
#p{color:var(--accent);font-weight:700;font-size:12.5px;padding-top:1px}
#in{flex:1;background:transparent;border:0;outline:0;resize:none;color:#cfdaea;
  font:inherit;font-size:13px;line-height:1.5;max-height:140px;user-select:text}
#in::placeholder{color:var(--dimmer)}
#foot{display:flex;align-items:center;gap:10px;margin-top:9px;font-size:11px;
  color:var(--dimmer)}
#ctx{font-size:11.5px;white-space:nowrap}
#ctx .fill{color:var(--ok)} #ctx .fill.w{color:var(--warn)} #ctx .fill.b{color:var(--bad)}
#ctx .rest,#ctx .br{color:var(--dimmer)}
#ctx .n{color:var(--dim);margin-left:5px}
#acts{margin-left:auto;display:flex;gap:8px;align-items:center}
#hint{color:var(--dimmer);font-size:10.5px}
#go{font:inherit;font-size:11.5px;cursor:pointer;border-radius:6px;
  padding:3px 13px;background:transparent;border:1px solid var(--line);
  color:var(--dim)}
#go:hover{border-color:var(--bevel);color:var(--accent)}
#go.stop{color:#ffd9d4;background:rgba(240,101,90,.10);
  border-color:rgba(240,101,90,.45)}
#go.stop:hover{background:rgba(240,101,90,.18)}

/* -- #88: one held-back call, put to the user -------------------------- */
.askcard{border:1px solid rgba(229,192,75,.40);border-radius:10px;
  background:rgba(229,192,75,.05);padding:11px 13px}
.asktop{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.asktop b{color:#e5c04b;font-weight:600}
.asktop code{color:var(--dim);font-size:11.5px;word-break:break-all}
.askrow{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.askrow button{font:inherit;font-size:11.5px;cursor:pointer;border-radius:6px;
  padding:4px 12px;background:transparent;border:1px solid var(--line);
  color:var(--dim)}
.askrow button.yes{color:#4ec98f;border-color:rgba(78,201,143,.45)}
.askrow button.yes:hover{background:rgba(78,201,143,.12)}
.askrow button.no{color:#ffd9d4;border-color:rgba(240,101,90,.45)}
.askrow button.no:hover{background:rgba(240,101,90,.12)}
.askrow button.always em{font-style:normal;color:var(--accent)}
.askrow button:hover{border-color:var(--bevel)}
.askdone{color:var(--dimmer);font-size:11px}

/* -- #88: the release level, beside send ------------------------------- */
/* THE COLOUR IS THE STATE. robin's three: manual white, allowedit green,
   auto yellow -- brightest where the least is held back, because the level
   that runs a shell unasked is the one worth noticing across the room. */
#modewrap{position:relative}
#mode{font:inherit;font-size:11.5px;cursor:pointer;border-radius:6px;
  padding:3px 11px;background:transparent;border:1px solid var(--line);
  color:var(--dim);display:flex;align-items:center;gap:6px}
#mode:hover{border-color:var(--bevel)}
#mode .dot{width:7px;height:7px;border-radius:50%;background:currentColor;
  flex:none}
#mode[data-mode="manual"]{color:#e8eef8;border-color:rgba(232,238,248,.40)}
#mode[data-mode="allowedit"]{color:#4ec98f;border-color:rgba(78,201,143,.45)}
#mode[data-mode="auto"]{color:#e5c04b;border-color:rgba(229,192,75,.45)}

/* UPWARDS, because the composer sits at the bottom of the window: a menu
   that opened downwards would be drawn outside it. */
#modemenu{position:absolute;bottom:calc(100% + 6px);right:0;min-width:266px;
  background:var(--panel);border:1px solid var(--bevel);border-radius:8px;
  padding:5px;box-shadow:0 8px 26px rgba(0,0,0,.45);z-index:40}
#modemenu[hidden]{display:none}
#modemenu button{display:block;width:100%;text-align:left;font:inherit;
  font-size:11.5px;cursor:pointer;background:transparent;border:0;
  border-radius:6px;padding:7px 9px;color:var(--dim)}
#modemenu button:hover{background:rgba(126,176,248,.10)}
#modemenu button b{display:block;font-weight:600;font-size:12px}
#modemenu button .what{color:var(--dimmer);font-size:10.5px}
#modemenu button[data-mode="manual"] b{color:#e8eef8}
#modemenu button[data-mode="allowedit"] b{color:#4ec98f}
#modemenu button[data-mode="auto"] b{color:#e5c04b}
#modemenu button .tick{float:right;color:var(--accent)}
</style></head><body>

<div id="bar" class="pywebview-drag-region" ondblclick="pywebview.api.maximise()">
  <span id="mark">CR<span>O</span>W</span><span id="ver"></span>
  <div id="wbtns" class="pywebview-no-drag">
    <div class="wb" onclick="pywebview.api.minimise()">&#8211;</div>
    <div class="wb" onclick="pywebview.api.maximise()">&#9633;</div>
    <div class="wb close" onclick="pywebview.api.close()">&#10005;</div>
  </div>
</div>

<div id="menu">
  <button onclick="crow.menuRename()">rename</button>
  <button data-act="arch" onclick="crow.menuArchive()">archive</button>
  <div class="sep"></div>
  <button class="danger" onclick="crow.menuDelete()">delete</button>
</div>

<div class="grip" id="g-n"></div><div class="grip" id="g-s"></div>
<div class="grip" id="g-w"></div><div class="grip" id="g-e"></div>
<div class="grip" id="g-nw"></div><div class="grip" id="g-ne"></div>
<div class="grip" id="g-sw"></div><div class="grip" id="g-se"></div>

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
    <div id="status">
      <span class="chip"><span id="dot"></span><span id="state">connecting …</span></span>
      <span class="chip" id="model" hidden></span>
      <span class="chip" id="nctx" hidden></span>
      <div id="right">
        <span class="chip ghost" id="url"></span>
        <span class="chip" id="tools"></span>
      </div>
    </div>
    <div id="flow"></div>
    <div id="composer">
      <div id="box">
        <div id="line"><span id="p">you&gt;</span
          ><textarea id="in" rows="1" placeholder="Message, or /tools for what the model can call"></textarea></div>
        <div id="foot">
          <span id="ctx"></span><span id="turnstate"></span>
          <div id="acts"><span id="hint"></span>
            <div id="modewrap">
              <button id="mode" data-mode="auto" onclick="crow.modeMenu()"
                      title="release level for tool calls">
                <span class="dot"></span><span id="modename">auto</span></button>
              <div id="modemenu" hidden></div>
            </div>
            <button id="go" onclick="crow.go()">send</button></div>
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

  turn(cls){ const d=document.createElement("div"); d.className="turn "+cls;
    flow.appendChild(d); return d; },

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
    const c=$("#tools"), s=c.querySelector("span");
    s.textContent = on ? ", running" : ", shown only";
    c.style.color = on ? "var(--warn)" : "var(--dimmer)";
    c.style.borderColor = on ? "rgba(227,179,65,.45)" : "var(--line)";
    c.title = on ? "Tools run. Click to only show them."
                 : "Tools are only shown. Click to let them run.";
  },
  toggleTools(){ if(this.running) return; pywebview.api.set_tools(!this.execute); },

  idle(){ this.running=false; go.textContent="send"; go.classList.remove("stop");
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
  reset(){ if(this.running) return; flow.innerHTML=""; pywebview.api.reset(); },
  open(path){ if(this.running) return; flow.innerHTML=""; pywebview.api.open(path); },

  // RIGHT-CLICK ON A CHAT. Three things a list of saved conversations has to
  // offer, and none of them is reachable from a left click: rename it, put it
  // out of the way, throw it out.
  menu(e,entry,row,archived){
    e.preventDefault(); this.target=entry; this.targetRow=row;
    const m=$("#menu");
    // An archived chat is put BACK, not away again; the open one has nowhere to
    // be restored from. One menu, three labels.
    m.querySelector("[data-act=arch]").textContent =
      archived ? "restore" : "archive";
    m.classList.add("on");
    // Kept inside the window: a menu opened near the bottom edge would
    // otherwise hang off it with its last item unreachable.
    const w=m.offsetWidth||170, h=m.offsetHeight||110;
    m.style.left=Math.min(e.clientX,innerWidth-w-6)+"px";
    m.style.top=Math.min(e.clientY,innerHeight-h-6)+"px";
  },
  closeMenu(){ $("#menu").classList.remove("on"); },

  menuRename(){
    this.closeMenu();
    const row=this.targetRow, entry=this.target;
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

  menuArchive(){ this.closeMenu();
    if(this.target) pywebview.api.archive_chat(this.target.path); },

  // TWO CLICKS FOR A DELETE, because there is no undo behind it. The second
  // click is on a button that says what it does, not on a generic "yes".
  menuDelete(){
    const m=$("#menu"), btn=m.querySelector(".danger"), entry=this.target;
    if(btn.dataset.armed==="1"){ btn.dataset.armed=""; btn.textContent="delete";
      this.closeMenu(); if(entry) pywebview.api.delete_chat(entry.path); return; }
    btn.dataset.armed="1"; btn.textContent="really delete?";
    setTimeout(()=>{ btn.dataset.armed=""; btn.textContent="delete"; },4000);
  },

  ctx(tokens,limit){
    const el=$("#ctx"); if(tokens<=0){ el.innerHTML=""; return; }
    const size = tokens<1000 ? String(tokens) : (tokens/1000).toFixed(1)+"k";
    if(limit<=0){ el.innerHTML='<span class="n">'+size+'</span>'; return; }
    const share=Math.min(1,tokens/limit), filled=Math.round(share*10);
    const cls = share<0.5 ? "fill" : (share<0.85 ? "fill w" : "fill b");
    el.innerHTML='<span class="br">[</span><span class="'+cls+'">'+"#".repeat(filled)+
      '</span><span class="rest">'+"-".repeat(10-filled)+
      '</span><span class="br">]</span><span class="n">'+size+"/"+
      (limit/1000).toFixed(0)+'k</span>';
  },

  // ONE ENTRY, because there is one session.json. A rail that listed more would
  // promise a switch this build cannot make.
  rail(title,meta,rollovers,foot){
    $("#sessions").innerHTML =
      '<button class="sess on"><span class="t"></span><span class="s"></span></button>';
    $("#sessions .t").textContent=title;
    $("#sessions .s").textContent=meta;
    // THE OPEN CHAT GETS THE SAME MENU. It was the one entry without one, which
    // meant the chat you are actually looking at was the only one you could not
    // rename -- the exact opposite of what a name is for.
    const live=$("#sessions .sess.on");
    live.title="right-click for more";
    live.oncontextmenu=e=>crow.menu(e,{path:null,title:title},live);
    if(rollovers && rollovers.length){
      const h=document.createElement("div"); h.id="railsep";
      h.textContent="Earlier"; $("#sessions").appendChild(h);
      rollovers.forEach(r=>{ const b=document.createElement("button");
        b.className="sess";
        b.innerHTML='<span class="t"></span><span class="s"></span>';
        b.querySelector(".t").textContent=r.title || r;
        b.querySelector(".s").textContent=r.meta || "";
        b.title="open · right-click for more";
        if(r.path){ b.onclick=()=>crow.open(r.path);
          b.oncontextmenu=e=>crow.menu(e,r,b); }
        $("#sessions").appendChild(b); });
    }
  },

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
      b.oncontextmenu=e=>crow.menu(e,r,b,true);
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
        if(e.model){ $("#model").hidden=false; $("#model").innerHTML="<b></b>";
          $("#model b").textContent=e.model; }
        if(e.n_ctx){ $("#nctx").hidden=false;
          $("#nctx").innerHTML="n_ctx <b>"+(e.n_ctx/1000).toFixed(0)+"k</b>"; }
        this.ctx(e.tokens||0,e.n_ctx||0); break;
      case "down": $("#dot").className="down";
        $("#state").textContent=e.why||"no server"; break;
      case "meta": $("#ver").textContent=e.version;
        $("#url").textContent=e.url;
        $("#tools").innerHTML="<b></b> tools<span></span>";
        $("#tools b").textContent=e.tools;
        $("#tools").onclick=()=>crow.toggleTools();
        this.tools(e.execute); break;
      case "tools": this.tools(e.on); break;
      case "mode": this.modeIs(e.name, e.modes); break;
      case "ask": this.ask(e.name, e.args, e.scope); break;
      case "rail": this.rail(e.title,e.meta,e.rollovers,e.foot);
        this.archive(e.archived||[]); break;
      // THE PAGE CLEARS ITSELF ON "new", because the click is here. A DELETE of
      // the chat being read starts on the page too but is decided in Python --
      // it may fail -- so the emptying has to come back from there.
      case "clear": flow.innerHTML=""; this.cost("",null); break;
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
      case "fail": this.fail(e.t); break;
      case "live":
        $("#turnstate").textContent =
          e.n + " tok · " + e.rate.toFixed(1) + " tok/s";
        break;
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

window.addEventListener("mousedown",e=>{
  if(!e.target.closest("#menu")) crow.closeMenu(); });
window.addEventListener("contextmenu",e=>{
  if(!e.target.closest(".sess")) e.preventDefault(); });

window.addEventListener("pywebviewready",()=>{ pywebview.api.ready(); input.focus(); });
</script></body></html>
"""


def shell_buttons(title: str) -> bool:
    """Give the frameless window the styles the taskbar reads. True when set.

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
        # THE REASONING'S SHARE OF THE ROUND, KEPT RATHER THAN PUSHED. It used
        # to leave here as `{"k": "_round"}` -- a message the page has no case
        # for, so it fell through the switch and was gone, while the cost line
        # was drawn from a variable nothing had written: every turn reported a
        # share of null. A number that only this window computes belongs to the
        # turn object, and the turn object is read when the turn ends.
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
            self.share = 100.0 * rc / (rc + cc)

    def cache_promise_broken(self) -> None:
        self._put({"k": "note",
                   "t": "the restored cache did not hold -- "
                        "that prefill was the whole history"})

    def tool_started(self, name: str, arguments: str) -> None:
        self._put({"k": "tool", "name": name,
                   "args": crow_core.format_tool_args(arguments)
                   if hasattr(crow_core, "format_tool_args") else (arguments or "")})

    def tools_reported(self, calls: list) -> None:
        for call in calls or []:
            self.tool_started(call.get("name", "?"), call.get("arguments", ""))

    def rolled_over(self, tokens: int, path: str) -> None:
        self._put({"k": "note", "t": "rolled over at %d tokens -> %s"
                                     % (tokens, os.path.basename(path))})


class Api:
    """What the page may call. Nothing here touches a widget; it queues."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        # UNDERSCORED ON PURPOSE. pywebview walks the public attributes of the
        # js_api object to expose them to the page; a window object among them
        # is walked too, and `window.native.AccessibilityObject.Bounds.Empty…`
        # recurses until the stack gives out. Private names are skipped.
        self._window = None
        self._out: "queue.Queue" = queue.Queue()
        self._conversation = Conversation(args.system)
        self._context_tokens = 0
        self._n_ctx = 0
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
        # #88: the level and its menu, in the same breath as the rest of the
        # header. The button has to show what is live before the first turn --
        # a release level nobody can see is one nobody can trust.
        self.push({"k": "mode", "name": getattr(self._args, "mode", DEFAULT_MODE),
                   "modes": self.mode_menu()})
        threading.Thread(target=self._probe, daemon=True).start()

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
            # NOT THE ONE THAT IS OPEN. Naming the current chat gives it a file,
            # and without this line that file came straight back as a second
            # entry under "earlier" -- the same conversation listed twice, once
            # as itself and once as its own history.
            if self._current_path and os.path.abspath(path) == os.path.abspath(
                    self._current_path):
                continue
            out.append(self._entry_of(path, name))
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
                "meta": ("%d messages · %s" % (len(messages), kind)
                         if messages else kind)}

    def _probe(self) -> None:
        try:
            state = check_endpoint(self._args.base_url)
            name = model_display_name(fetch_model_name(self._args.base_url))
            self._n_ctx = fetch_n_ctx(self._args.base_url)
            self.push({"k": "up", "model": name, "n_ctx": self._n_ctx,
                       "tokens": self._context_tokens, "state": state})
        except Exception as exc:           # noqa: BLE001 - shown, never raised
            self.push({"k": "down", "why": str(exc)[:120]})
            return
        if not self._args.session:
            return
        try:
            restored = load_session(self._args.base_url, self._args.system)
        except Exception as exc:           # noqa: BLE001
            self.push({"k": "note", "t": "session not readable: %s" % exc})
            return
        if not restored:
            self._reload_rail()
            return
        messages, tokens, kv = restored
        self._conversation.restore(messages)
        self._context_tokens, self._promised_warm = tokens, kv
        # THE IDENTITY IS READ BACK, NOT MINTED. This line used to archive the
        # restored session, which handed it a file it did not need -- and handed
        # it another one on the next launch, and the one after that, until the
        # same conversation stood in the rail as many times as the window had
        # been opened. session.json is where the two things worth remembering
        # about the open chat are kept: which archive file it belongs to, if it
        # has one yet, and what the user named it.
        self._current_path, self._current_title = self._pointer()
        self._replay(messages)
        self._reload_rail()
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
        self._context_tokens = 0
        self._promised_warm = False
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
        if len(self._conversation) <= (1 if self._conversation.has_system else 0):
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
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if self._current_title:
                data["crow_title"] = self._current_title
            else:
                data.pop("crow_title", None)
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
                         self._context_tokens, with_kv=with_kv)
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
            restored = load_session(self._args.base_url, self._args.system, path)
        except Exception as exc:           # noqa: BLE001
            self.push({"k": "fail", "t": "not readable: %s" % exc})
            return
        if not restored:
            self.push({"k": "fail", "t": "empty: %s" % os.path.basename(path)})
            return
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
        self._context_tokens, self._promised_warm = tokens, kv
        self._replay(messages)
        # SESSION.JSON FOLLOWS THE SWITCH AT ONCE. Still pointing at the chat
        # just closed, a window shut before the next turn would come back up
        # holding it -- and list the chat the user was actually reading as a
        # second entry beside it.
        self._persist_live()
        self._reload_rail()
        self.push({"k": "cost", "line": "", "share": None,
                   "tokens": self._context_tokens, "n_ctx": self._n_ctx})

    ARCHIVE_DIR = "archiv"

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
            if not body.strip() and not thought.strip():
                continue
            sink = Sink(self.push, live=False)
            sink.reply_started()
            if thought.strip():
                sink.reasoning_started(0)
                self.push({"k": "think", "t": thought})
                sink.reasoning_finished()
            if body:
                sink.answer_text(body)
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
                   "rollovers": self._archives(),
                   "archived": self._archived(),
                   "foot": os.path.basename(self._current_path)
                   if self._current_path else ""})

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
        self._conversation.append("user", text)
        events = Turn(self.push)
        try:
            result = run_turn(
                self._conversation, base_url=self._args.base_url,
                model=self._args.model, api_key=self._args.api_key,
                temperature=TEMPERATURE, top_p=TOP_P, min_p=MIN_P,
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
    parser.add_argument("--no-tools", dest="execute_tools", action="store_false",
                        help="show tool calls instead of running them")
    # #88, the same flag the terminal client takes: the START level, with the
    # dropdown beside `send` as the same switch during a session.
    parser.add_argument("--mode", choices=crow_core.MODES, default=DEFAULT_MODE,
                        help="release level for tool calls: manual asks before writing"
                             " and executing, allowedit asks before executing, auto asks"
                             " for nothing (default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
                .replace("__TIMEOUT__", "%.0f" % READ_TIMEOUT_S))

    api = Api(args)
    # FRAMELESS, because the title bar is part of the design: the caption is
    # drawn in the page with the wordmark in it, the way the mockup shows it.
    title = "CROW %s" % (client_version() or "")
    window = webview.create_window(
        title, html=page, js_api=api,
        width=1180, height=800, min_size=(760, 520), frameless=True,
        easy_drag=False, background_color=CROW_BG)
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

    webview.start(styles, window)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
