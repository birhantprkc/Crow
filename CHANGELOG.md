# Changelog

Every entry says what changed and what was measured to justify it. A number here
carries the conditions it was taken under, or it says that it is unmeasured.

This file records the **released** history. The full reasoning behind each change
is in its commit message and on its issue; this is the short version.

## 0.4.0 — 2026-08-14

**Web research: `web_search` and `fetch_url`, and nothing to configure (#96).** The model searches,
reads what it found, and continues the task. Six official keyless APIs are queried in parallel —
PyPI, crates.io, HuggingFace, Stack Overflow, GitHub, Wikipedia, plus DuckDuckGo's *documented*
instant-answer endpoint. No key, no account, no service. `CROW_TAVILY_KEY` or `CROW_SEARXNG_URL`
switch to a general index for whoever wants one.

**The obvious implementation does not work, and fails silently.** Measured 2026-08-14:
`duckduckgo.com/html/?q=` — the endpoint every model writes for this, Crow's own local model
included — answers **HTTP 202 with zero `result__a` matches** to both `Mozilla/5.0` and
`Crow/0.3.3`. 202 is a success status, `urlopen` does not raise, so a tool built on it reports
`no results` forever with nothing in any log. `lite.duckduckgo.com` still answers **200 with 10
results** to a browser user-agent and **202 to Crow's own**, one URL and one second apart: the only
working scrape requires misrepresenting the client. Six public SearXNG instances were probed the
same day (searx.be, search.inetol.net, priv.au, searxng.site, search.bus-hit.me, baresearch.org);
none served `format=json`.

**Three defects the live run found and the unit tests could not.** Three results came to **16,056
bytes** because one repository description was 15 KB — `_clip` then cut the tail, so the model paid
full prefill for one project's marketing and never saw results two and three; every snippet is now
capped at 240 bytes. Concatenating the sources put GitHub first unconditionally, so "requests
library current version" answered with a stranger's library-management project while PyPI's exact
`requests 2.34.2` sat further down; the merge is now round-robin in authority order. And the package
lookup fired on any identifier-looking word, so "llama.cpp moe stream flag" led with `pypi Moe
2.5.0`, a music library manager — a coincidental name match in the top slot is worse than noise
because it looks authoritative.

**HuggingFace carries the weight, not just the name.** `Qwen/Qwen3.5-27B` reports 2,734,049
downloads against 1,028 likes; `Qwen/Qwen3.8-27B` reports **2 downloads against 8,457 likes**, which
is the signature of a release published hours earlier. The same string from an official org path and
from a 0-download re-upload is not the same evidence, so the counts are printed. Its search takes the
model name and not the sentence: "Qwen3.5-27B model" returned nothing until the gate words were
stripped from the query they let through.

**Cost, measured through `/apply-template` and `/tokenize` on 2026-08-14.** The same five-token
message that sent 953 tokens with seven tools now sends **1,269**, of which **1,222 (96.3 %)** are
the nine declarations. The two web tools cost **313 tokens of prefix in every request**.

`network` is a fourth class in `TOOL_CLASS` and asks at **no** release level, `manual` included: the
search happens because a task was given, and giving the task is the release. `fetch_url` takes http
and https only — `file:` and `data:` would make it a disk read around #92's boundary rather than
through it. Extraction runs before the 16 KB clip, because clipping first keeps the markup and drops
the answer. 152 in `cli/test_crow_core.py`, 13 breakages each count-checked to a single site.

**The window's live tok/s counts the pauses, and that is now written down (#97).** Observed
2026-08-14: **9.5 tok/s** on screen beside a server logging **17.99–19.29 t/s** for the same turn.
The denominator runs from `reply_started`, so it contains the wait for the first token, every tool
call and the prefill of every tool result — and the web tools widened the gap, because a search is
exactly that kind of pause.

It reads like the defect `crow_core.TurnCost` fixed on 2026-08-11 ("printed 1.49 tok/s for a turn the
server had just measured at 14.77 and 16.46"). It was changed to sum only the gaps between deltas,
and changed straight back: **what the user waits through is wall clock.** A decode rate that ignores
the pauses answers a question the server already answers, and the server's own figure is the line
that lands underneath at the end of the turn — two figures, two meanings, both on screen.

The reason it looks like a bug is the reason it now has a guard.
`TheLiveRateIsWallClockOnPurposeTests` fails if the pauses ever stop counting, with the well-meant
repair as its negative control: summing only the inter-delta gaps lands back at the decode rate, and
a case that goes green there means the decision was reverted without anyone deciding to. 98 in
`cli/test_crow_gui.py`.

## 0.3.3 — 2026-08-14

**A working directory the model may not write outside of (#92).** `write_file` and `edit_file` took
any path; at `auto` — the default — nothing stood between the model and the disk. A release level
(#88) asks an attentive user at round 14 of 24; a boundary refuses without asking, which is the half
that protects the turn nobody was watching. The refusal names the root, so a user can see what the
boundary thought it was instead of guessing.

**The root is the nearest ancestor holding `.crow/root.json` — not `.crow/` itself.** That directory
is a by-product: `SPILL_DIR` creates it wherever crow runs. Measured on 2026-08-14,
`C:\Users\robin\.crow` already existed, dated 2026-08-08, from a single session started in the home
directory — treating it as the marker would have made the entire user profile a root and the
boundary decoration. A directory becomes a root when someone picks it, never by accident.

**Writes only, and both halves are recorded decisions.** `read_file` stays unbounded: a read boundary
blinds the model to its own installation, which is a real use, and a read destroys nothing.
`run_command` is not covered either — a `cwd` inside the root says nothing about what the command
does, `cd /d C:\ && del …` being one shell line — so it stands on #88's `executing` class instead. At
`auto` that gap is real, and it is named here rather than left to be discovered.

Three traps, all measured that day on Python 3.13.3: `os.path.ALLOW_MISSING` is in the 3.13
documentation and does not exist in this release; `commonpath` and `relpath` raise `ValueError`
across drive letters instead of answering "no"; and `"C:\root2\x".startswith("C:\root")` is `True`,
so a bare prefix check lets a sibling directory through.

The window has a folder picker with a recently-used list; the terminal has `--root`, which states a
root **and** creates it. The window does not walk up from its cwd — a shortcut decides that
directory, so a stray marker under it would outrank what the user picked.

**What this does not do: the choice does not survive closing the client.** A persistence path through
the session file was built and removed again — it never worked in the running window, and half of it
would read as "the boundary holds" when it does not. Nothing in the window has exercised the refusal
in a live turn either.

Held against thirteen deliberate breakages. The two that matter are complementary: with the boundary
switched off 9 cases go red, with it refusing everything 3 go red — the negative halves. Neither
"always refuse" nor "never refuse" passes both.

**`/reset` now survives closing the client, on both surfaces.** It never had. `save_session` refuses
a conversation with nothing in it — right for the case it guards, a client started and closed without
a word, since an empty file is worse than none — but the guard cannot tell that from *the user just
emptied it on purpose*. So a `/reset` followed by an exit wrote nothing, the file from before the
reset stayed, and the next start restored the conversation that had just been dropped.

Found by robin in the window on 2026-08-14 and confirmed against the live file: `session.json` still
held three messages, timestamped **before** the reset — the last turn's write, not the reset's.

The fix is not a change to the guard, which would delete archives on the same reasoning.
`forget_session()` in the core removes the file, and both `/reset` paths call it. `--no-session`
leaves it alone: a client that does not own that file has no business deleting it.

**And in the window it lets go of the chat it came from.** Removing `session.json` fixed the live
case and left the other half: a conversation opened out of the rail keeps `_current_path`, and
closing archives the open conversation *there* — where the same guard refused it, so the file kept
its old messages and the next start found them again. **Detached, not deleted:** `/reset` drops the
context, it is not *throw my saved chat away*, and the chat stays in the rail with everything in it.
Both halves are cases, and neither "stay bound" nor "delete the file" passes both.

**The window runs every slash command now (#94).** It handled `/tools`; the other six travelled to
the server as ordinary questions and came back as an answer about the word — `/reset`, `/context`,
`/thoughts`, `/mode`, `/exit`, `/quit`.

| typed | does |
|---|---|
| `/reset` | drops the context and the standing approvals. **The chat stays where it is** |
| `/context` | messages, tokens, and the rollover point the bar never names |
| `/mode`, `/mode <name>` | reports the release level, or switches it through `set_mode` |
| `/thoughts` | folds every reasoning block open, or closed again |
| `/exit`, `/quit` | closes the window |
| `/help`, `/tools` | the window's own list, and the tool schema |

**The first attempt answered them with a sentence naming the control that does the same job, and
that was wrong twice over.** *"/reset: that is the new button, top left of the chat rail"* — the
button is on the **right**, because `margin-left:auto` puts it there; and `new` **archives the
conversation into the rail and opens an empty one**, which is not what `/reset` does in the terminal
at all. A user who followed that instruction would file away a chat they meant to keep.

**A pointer is prose about pixels, and prose about pixels cannot be tested.** The case written to
catch a lying pointer only asserted that `id="new"` appears somewhere in the page, so it could never
have caught either error — green, and worthless, in the exact shape its own docstring warned about.
Running the command has neither failure mode: no prose to be wrong about, and no mapping to get
wrong. A case now forbids the words *button*, *top left*, *beside*, *dropdown* and *click* from every
answer the window gives.

**What is shared is the list, not the answer.** `crow_core.SLASH_COMMANDS` holds the names both
surfaces must cover; `crow.py` keeps the prose of `HELP` and is pinned against it. A command added to
one and not the other is a red test rather than a command the window has never heard of.

**A message that merely starts with a slash still reaches the model** — `/usr/bin/env is what?` is a
question, not a command.

Two more found in the window after that, both older than this change: **`/help` and `/tools` arrived
as one run-on paragraph**, because `.note` had no `white-space` and their columns were collapsed —
true of `/tools` since the day it was answered here; and **`/mode <name>` answered twice**, because
`set_mode` pushes its own note and the command returned a second one. An empty answer now means
*handled, and already said*, which is a third thing next to a note and a `None`.

24 cases in `cli/test_crow_gui.py` (74) and one in `cli/test_crow.py` (340). The one that matters
counts the files in the session directory before and after `/reset`: if it writes one, it has become
`new` again.

**Two defects came with it, both older than the change and both found in the window rather than by
the suite.** The Api pushed a `user` echo before its answer — but `go()` already draws that line
before it calls in, so the typed command appeared **twice**. And `go()` painted the composer "Stop"
with a read-timeout hint on the way in, which a turn normally takes back; a command answered in
Python starts no turn, so the window **sat on "Stop" with nothing running behind it**. Both were true
of `/tools` before this change and became true of all seven with it.

**`send` now answers the question the page was guessing at.** Every `pywebview.api.*` call resolves a
promise once the Python side returns — so `send` returns whether a **turn** started, and `go()` locks
synchronously but paints from the answer:

```js
this.user(text); this.running=true;
pywebview.api.send(text).then(started => started ? this.busy() : this.idle(),
                              () => this.idle());
```

One mechanism instead of two: no "Stop" flicker on a slash command, no correcting message pushed
after the fact, and a rejected call unlocks as well. The lock stays synchronous because the round
trip is a real window for a second click; only the **button** waits.

**Every one of the 57 cases passed through both defects.** They drive the Api with no page on the
other side — one half of a seam measuring itself, and every assertion was about what this half
pushed. Four cases pin the seam now, including the two lines of `go()` the Python half depends on, so
the next change to the page's side turns a test red instead of shipping.

**The tool cache was keyed on less than its inputs (#93).** `run_tool_cached` answered a repeated
call from the first one, on the stated grounds that *"re-running would produce the identical
failure"*. True for five of the seven tools and false for two, measured 2026-08-14 in the first real
agent run: a `write_file` refused for want of a read was replayed **after the read that lifted it**,
three times, until the model gave up and reached for `edit_file`; and a `run_command` after an
`edit_file` on the same file replayed the output from before the edit, until the model appended
`2>&1` to change the key rather than the command. 4 of that turn's 12 calls were replays of a state
that had already moved, and 2 of its 13 rounds existed only to get around them.

The fix is not to recognise refusal text — a cache keyed on less than its inputs is wrong whatever
the text says. The inputs now go into the key: `run_command` is never cached, `write_file` and
`edit_file` carry whether that path has been read this turn, and everything else is keyed as before.
That last part is what keeps the loop the cache was built for closed — it happened on `read_file`
for a path that does not exist, and a path does not start existing because it was asked for twice.

**A declined tool call is no longer counted as a failure (#95).** `DECLINED` begins with `error: ` on
purpose, because that prefix is what makes the model treat a refusal as recoverable rather than
terminal. The cost line decided what to call a malfunction with the same prefix, so a user's own
decision arrived as `1 failed` — seen in the run above, where the one "failure" was the
read-before-write rule holding. Counted and named separately now (`1 declined`). The prefix is
unchanged, and the screen still prints a declined call: only the count was split.

17 new cases (`cli/test_crow.py` 339, `cli/test_crow_core.py` 118), and they bracket the behaviour
rather than confirm it — held against four deliberate breakages, each red in a different place:

| breakage | red |
|---|---|
| the key ignores state again | 5 |
| the cache is removed altogether | 9, incl. the four cases that predate this change |
| a decline counts as a failure again | 3 |
| `DECLINED` loses its `error: ` prefix | 2 |

Neither "always cache" nor "never cache" passes both halves, which is the point: the second breakage
is the cheap fix that looks like success.

One test-harness defect fell out of it: `ToolLayerCase._install` **deleted** the entry it replaced
instead of restoring it, so a double installed over a shipped tool name removed that tool from
`TOOL_IMPL` for the rest of the process. Harmless until a case needed a double under a real name;
then an unrelated case went red with no visible connection to what broke it.

**`/mode` is in the header.** 0.3.2 shipped three release levels and advertised none of them: the
block beside the wordmark listed `/help`, `/tools` and `/exit`, so the only way to find `/mode` was
to already know it existed and type `/help`. A level nobody can find is the same as no level. It now
reads `/mode manual, allowedit or auto` — the modes on the line, because the header is where the
user learns what the prompt will do.

The column is budgeted against the wordmark's **five rows**: four commands plus a blank plus the
repository URL is six slots, the commands still land on the mark, and the URL moves down onto the
bevel row. A **fifth** command pushes the URL onto the version line — which is the one thing
`header_lines`' centring exists to prevent, and **no test noticed**:
`test_the_version_line_carries_no_command` iterates commands only, and the URL is the last entry, so
it is the one that falls off first. Now pinned by
`test_the_version_line_carries_nothing_from_the_column`, with
`test_a_fifth_command_pushes_the_url_onto_the_version` as its negative control — an assertion never
seen red cannot be told apart from one that cannot go red. 329 in `cli/test_crow.py`.

## 0.3.2 — 2026-08-14

**The CLI's tool-call marker is a glyph the shipped font actually has.** It was U+2692 `⚒`, and
neither shipped face covers it — Windows drew it from a substitute face, which is the exact fallback
`cli/crow.py` keeps its spinner away from braille to avoid. Found by E9 on 2026-08-13, declared in
`KNOWN_UNCOVERED` rather than swallowed, fixed here. It is now **U+25CF `●`** — the same marker the
window already draws for a tool call, so both surfaces mark a call the same way. Two call sites,
`cli/crow.py:935` and `:963`; nothing else used it.

The declaration was not deleted alongside the edit: `check_gui_prereqs.py` went red at it first
("U+2692 is declared in KNOWN_UNCOVERED and no surface writes it any more — drop the declaration"),
which is the half of point (ii) that makes the other half worth reading. `KNOWN_UNCOVERED` is now
empty.

**Release levels for tool calls (#88).** The seven tools ran unasked in both clients. They now run
under a level, and the level is visible in both surfaces.

| class | tools | manual | allowedit | auto |
|---|---|---|---|---|
| reading | `read_file`, `list_dir`, `find_files`, `search_text` | runs | runs | runs |
| writing | `write_file`, `edit_file` | **asks** | runs | runs |
| executing | `run_command` | **asks** | **asks** | runs |

- **Reading never asks, at any level.** A level that asks before `list_dir` is one nobody keeps
  switched on.
- **`auto` is the default**, because it is what every release up to 0.3.1 did. Making `manual` the
  default would change the behaviour of every existing session in a commit that adds a choice.
- **A declined call is a tool RESULT, not an abort** — `error: declined by the user`, and the turn
  continues. An assistant turn whose `tool_calls` have no `tool` message behind them is a broken
  prefix for every later turn, so this is a fourth trigger for the rule `run_turn` already keeps
  three times over, not a fourth implementation of it.
- **The terminal:** `/mode` reports the level, `/mode manual|allowedit|auto` switches it, `--mode` is
  the start value. The prompt prints the tool and its arguments, and offers "always for this
  directory / this program".
- **The window:** a dropdown beside `send`, coloured by level — manual white, allowedit green, auto
  yellow. A held-back call becomes a card in the transcript with three buttons; the card stays
  afterwards with the answer on it.
- **Standing approvals are per session and never written to disk.** Their scope is one directory for
  writes and one program for commands, so `git status` and `git log` share a key while `git` and `rm`
  do not. Dropped by `/reset`, by the window's new-chat button and by any level change — but **not**
  by a rollover, which resets the conversation while the user carries on with the same work.
- **The slash commands left `repl()`** into `run_slash()`. `test_repl_is_one_job_again` caps the loop
  at 220 lines so the five-job block the 0.3.0 split took apart cannot grow back; `/mode` pushed it
  to 227. Moved rather than rewritten. The command-coverage test now reads both functions — it was
  looking for the command names in `repl()`'s source alone and would have gone red at a refactor that
  changed no behaviour.

15 new cases in `cli/test_crow_core.py` (111 total), including the two #88 asks for by name — a
`write_file` refused under `manual`, a `run_command` refused under `allowedit` while the write in the
same turn runs — and the memory's negative half: a second directory and a second program must ask
again. Held against three deliberate breakages: a `needs_approval` that never asks goes red in 8
cases, a refusal that aborts the turn in 2, a memory with no scope in 1.

**Not in this change:** a working-directory boundary. #88 says why — a level asks a human, a boundary
refuses without asking, and mixing them into one ticket is how neither gets built.

**Both checkers that still measured tkinter now measure the window.** Neither was in the release
gate, so neither blocked anything — which is exactly why they could sit green and wrong.

- **`tools/measure_gui_stream.py` runs again.** Its point 1 measured Tk queue saturation against
  `TICK_MS` and `DRAIN_PER_TICK`, constants the webview does not carry, so it raised
  `SETUP ERROR: crow_gui.py does not carry TICK_MS` — and because that error returned from `main`,
  it took the two points **below** it down with it. Twenty lines of dead apparatus made 633 lines of
  live measurement unreachable, including the read-timeout probe that decides `READ_TIMEOUT_S`.
  Point 1 removed (99 lines, 3 functions); its result is kept in the docstring because
  `cli/crow_gui.py` quotes it. Points (2) and (3) keep their numbers — they refer to each other by
  number in their own output.
  **`READ_TIMEOUT_S = 600` now stands on a run rather than a note:** `3 of 3 numbers hold`, and the
  check is two-sided (`469.51 < bound < 1800`), so the previous 20 s would have gone red here.
- **`tools/check_gui_prereqs.py` point (iii) checks the window runtime**, not Tk 8.6.15. Both halves,
  because they fail separately: pywebview importable (6.2.1, read from package metadata — the module
  carries no `__version__`) **and** a WebView2 runtime in the registry (151.0.4129.78). All three
  views are read, because on this machine only `HKLM\WOW6432Node` answers; the GUID and the keys are
  `install.ps1:334-338`'s rather than a second set. **No floor is claimed for WebView2** — none has
  been measured, and an invented number is worse than none. `--min-webview2` exists for the negative
  control. Points (i) and (ii) are untouched — (ii) is what found the uncovered U+2692 marker fixed
  above, and its declaration list is empty again.
- **`tools/test_check_gui_prereqs.py` case 4 follows it** — it drove `--min-tk 99.0` and went green
  off the old point. Now `--min-webview2 999.0`: `2 of 3`, exit 1, with (i) and (ii) still green.
  8 of 8.

## 0.3.1 — 2026-08-14

**The window shipped in 0.3.0 was usable for one turn at a time.** Driving it for an afternoon found
four defects, three of which only appear once a turn is allowed to run longer than a single round.
Two of them were listed in 0.3.0's own *Known* section and are closed here.

### Changed

- **Tool calls now RUN in the window**, as they always have in the terminal. 0.3.0 shipped them as
  shown-only behind a chip, on the argument that behind a window nobody sees `run_command` start a
  shell. Driven live, that argument cut the other way: a user who asks for a file gets a tool call
  and no answer at all, every turn, with nothing on screen saying why. `--no-tools` is the new flag
  for the old behaviour, and the chip still names the mode in both states.
  **This does not add permission levels.** #88 (`/mode manual, allowedit, auto`) binds intent to
  permission, and it binds both clients or neither. `run_command` starts a shell in either one.

### Fixed

- **`READ_TIMEOUT_S` was 20 s** — listed as known in 0.3.0 and reached within minutes of tools being
  switched on. It is a **per-read** bound, so it only ever expires on a wait with no bytes in it: a
  prefill. A live turn died at `prefill 2,222 @ 51.21 tok/s`, about 43 s of silence, losing 12 rounds
  and 13 tool calls to `stream broke: timed out`. Now 600 s, which is what `README.md` and
  `tools/measure_gui_stream.py:106` had already been saying and what its own probe at `:636` requires
  (`> 469.51`, the worst prefill on record).
- **Maximising on a second monitor moved the window to the primary one.** `SPI_GETWORKAREA` returns
  the primary monitor's work area and nothing else; the window was being sent to coordinates that
  only exist over there. Now `MonitorFromWindow` + `GetMonitorInfoW`, which answer for the monitor
  the window is actually on. Verified by driving the real functions against a window placed on each
  of three screens, including one at scale 1.5, with the old path as the negative control.
- **The chat column hugged the left edge.** `max-width` without auto margins pins a column to the
  left and leaves the rest of a wide window empty. Text and composer now share one centred 900 px
  measure.
- **The download check named the wrong file count.** `README.md` told the reader to expect "four
  files totalling ~97 GiB" — that is `UD-IQ3_XXS`, replaced on 2026-08-12. `UD-IQ2_XXS` is three
  shards and 84.62 GiB (90,860,736,928 B). The line exists because `hf` prints `Downloaded` and
  returns the local directory when it could not reach the repository; the one check meant to catch a
  silent failure was producing one.
- **The architecture diagram's VRAM caption ran off both edges** — one 184-character line in a
  1132-wide box, and SVG does not wrap.

### Also

- **`README.md` is half its length**: 9,128 words to 4,686. Every measured figure, command block,
  table and citation stayed; the prose around them went.

### Known, and not fixed here

- **One aborted read in 50 outlived its grace** (`race_runs 50`, `race_leaked 1`, one run) —
  unchanged from 0.3.0.
- **Slash commands other than `/tools` do not exist in the window**; they go to the model as text.
- **`tools/measure_gui_stream.py` is red** and measures the tkinter build that 0.3.0 removed
  (`SETUP ERROR: crow_gui.py does not carry TICK_MS`). It owns the read-timeout probe, so the 600 s
  above clears a recorded floor rather than a re-run one.
- **`tools/check_gui_prereqs.py` still checks Tk 8.6.15** and reports 3 of 3 green. It is not in the
  release gate — that list names `check_shared_core.py` and `check_operating_point.py` — so it blocks
  nothing, but it is a checker that cannot go red for the toolkit this package actually ships.

### Measured

`cli/test_crow.py` 327/327 · `cli/test_crow_core.py` 96/96 · `cli/test_crow_gui.py` 47/47 ·
`check_shared_core` 44/44 · `check_operating_point` 4/4.

The operating point is unchanged from 0.2.0. Nothing in this release was measured against a live
server beyond the turns that produced the two defects above; the window itself was accepted by
driving it, not by a probe.

## 0.3.0 — 2026-08-13

**Crow gets a second client: a window, over the same core.** `cli/crow_gui.py` is a pywebview window
on the same conversation, the same session file and the same server as `cli/crow.py`. Neither wraps
the other, neither is needed to use the other, and both ship in the same package.

### New

- **The window.** Streaming with a live counter, foldable thought blocks, code blocks with a copy
  button, a chat list with rename / archive / delete, `/tools`, and a chip that says whether tools
  run or are only shown. Tools are **shown** by default — #55 and #88 are open, and a window that
  ran shell commands without saying so would answer that question silently.
- **`cli/crow_core.py`** now carries what both clients share: conversation, request body, SSE read,
  tool loop, cost line, where a thought block begins. `cli/crow.py` went from 2,942 to 1,701 lines.
- **The installer installs `pywebview`** into the interpreter it found. A failed pip does not fail
  the install — the terminal client is complete without it, and the exact command to finish the
  window by hand is printed with that interpreter's real path.
- **The preflight asks for the WebView2 runtime instead of Tk**, before the download, out of the
  registry — all three views, because on the development machine only the 32-bit one answers.

### Fixed

- **The chat list lost chats.** A chat was given its file when it was *restored* rather than when it
  was *left*, so every launch wrote another copy and deleting them brought one straight back. A chat
  with no file of its own was written into `session.json` when the user switched away — a file
  nothing lists and the next turn overwrites. A renamed chat lost its name because `save_session`
  writes six keys and the file whole.
- **A warm session was never saved.** The warm-cache flag was passed as the fourth argument of
  `save_session`, which is `path`; the call then died inside `os.path.dirname` and was swallowed.
- **Every cost line reported no thinking share** — it travelled as a message the page has no case for.
- **The taskbar button did nothing.** A frameless window is created without `WS_MINIMIZEBOX`, that
  bit is ignored without `WS_SYSMENU`, and the shell reads the style once, when it registers the button.

### Known, and not fixed here

- **One aborted read in 50 outlived its grace** (`race_runs 50`, `race_leaked 1`, one run). The
  normal path holds: the next question was answered 8.04 s after the abort, under the 30 s threshold.
- **`READ_TIMEOUT_S` is 20 s in the window**, while the worst prefill measured on a resumed 21k
  session is 469.51 s — a resumed session whose cache does not hold can be cut off mid-prefill.
- **Slash commands other than `/tools` do not exist in the window**; they go to the model as text.
- **`timings` arrives on almost every chunk**: 12 of 14 at `predicted_n 13`, one run.

### Measured

`cli/test_crow.py` 327/327 · `cli/test_crow_core.py` 96/96 · `cli/test_crow_gui.py` 47/47 ·
`tools/test_run_server_block.py` 24/24 · `check_shared_core` 44/44 · `check_operating_point` 4/4 ·
`install.ps1 -Selftest` 74 checks.

The operating point is unchanged from 0.2.0. E14 ran the window against it: 7 of 7 capabilities
held; two checks that live only in the page — folding a thought block, typing during a turn — are
reported as **not measured** rather than green.

## 0.2.0 — 2026-08-12

**The operating point moves to `UD-IQ2_XXS`.** Same verdicts on the gate, cheaper misses, and
3 GB more of the card left over. The binary is the one 0.1.0 shipped; what changed is which file
it opens.

### What was measured, #89

Three graded runs of the ten-task gate per rung, own server per run, one variable changed:

| | UD-IQ3_XXS | UD-IQ2_XXS |
|---|---:|---:|
| gate, three runs | 10/10 · 10/10 · 10/10 | 10/10 · **9/10** · 10/10 |
| decode, median | 18.63 tok/s | **19.53** |
| prefill, 1,884-token prompt | 118.26 tok/s | **133.10** |
| ms per miss | 0.7097 | **0.6470** |
| hit rate | 80.24 % | 80.13 % |
| VRAM after load | 31,074 MiB | **27,994 MiB** |

**The one 9/10 is the extractor, not the model.** `two-sum` in run 2 answered completely and
correctly and put `from bisect import bisect_right` above the function; the extractor takes only a
definition in column 0, so the call died on a `NameError`. Replayed against the extracted file to
confirm. `merge-intervals` — the task that failed 3 of 3 byte-identically on `UD-Q2_K_XL` in #28 —
is correct 3 of 3 here.

**The mechanism is bytes per miss, not cache slots.** The hit rate moved 0.11 points across a
13.38 % smaller slab. That is what a saturated cache looks like: 0731 covers 95 % of its selections
in 9.0 % of the experts, and 58 slots is 22.7 % resident.

### The file

84.62 GiB across three shards — 90,860,736,928 B. Routed experts 78.11 GiB of that, 92.3 %.
Resident tensors 6,378.40 MiB on CUDA0 plus 284.06 MiB of host buffers = 6.51 GiB. A slot costs
327,614,463 B per expert across 43 layers, so 58 slots is 18,121.38 MiB — the server's own line.

### Unmeasured on this rung, and marked wherever it appears

The slot ladder (56/58/60/62/64) and the host-tier pairing that produces the **1.63x** were taken
on `UD-IQ3_XXS` and are **not** repeated here. They describe a mechanism that did not change and
carry numbers that did. 58 slots is carried over unchanged for the reason it was chosen — the cache
is already past saturation, so the 4.6 GB the smaller slab frees would buy nothing.

Also unmeasured: the vendor KLD gap (0.30789 → 0.48487, top-1 81.93 % → 76.60 %) against anything
but ten algorithmic tasks. #46 puts the gate's own resolution at two tasks, so what is established
is the **absence of a detectable loss**, not the absence of a loss.

## 0.1.1 — 2026-08-11

**The operating point asked for more VRAM than the card has.** `--moe-stream-cache` goes from
**64s to 58s**. Nothing else about the product changed; the binary is the same one 0.1.0 shipped.

### What was wrong

A slot costs 360.69 MiB, so 64 slots need 32,062 MiB of a 32,607 MiB card and leave **545 MiB** for
everything the display does. This machine's desktop was read at **342, 543, 622 and 978 MiB within
one day**. Above the gap Windows moves the difference into host memory without printing anything,
and the affected request runs at half rate.

That is why it looked like a lottery rather than a fault: it depends on what is on the screen when a
request runs, so it hit some turns and not others, and never the short runs of the measurement
harness.

**No counter in the server can see it.** The halved request executed the same 195 graphs, took
comparable misses, and had the **lowest** load stall of its run — 7,807 ms against 7,872 / 8,651 /
9,041. Identical work, double the wall clock, and nothing in the streaming path to charge it to.

### Measured, 2026-08-11

Cold prefill of 1,374 tokens (3 runs) and decode of 200 tokens (8 runs), fresh server per run,
`runs/2026-08-11/`:

| cache | prefill | decode | VRAM used | free |
|---|---:|---:|---:|---:|
| 64 (0.1.0) | 15.28, spread **8.69x** | unusable | 32,014 | 545 |
| 62 | 114.92 | **7.07 among 15s** | 31,899 | 708 |
| 60 | 113.53 | 17.40 | 31,285 | 1,322 |
| **58 (0.1.1)** | **112.69** | **17.32** | **30,548** | **2,059** |
| 56 | 110.30 | 17.00 | 29,842 | 2,765 |

Throughput rises with the cache right up to the edge, so the fastest value is not the shippable one.
62 wins on paper and still halved one request in four. 58 costs 0.7 % of prefill and 0.5 % of decode
against 60 and triples the margin over the highest desktop reading.

**Driven by hand through the client**, the same cold 1,094-token prompt prefills at **60.44 tok/s**
against 15.09 at 64 slots, and four consecutive rounds of one turn decode at 14.97 / 14.97 / 17.00 /
14.61 — the halving is gone. The harness figures above use a repeated word list, which routes to
fewer distinct experts than real text; both are measured, only the second is what a user waits for.

### Also ruled out, so it is not tried again

`-ub` in both directions (8 → 74.73 … 512 → 98.76 … 2048 → 13.42, and 2048 falls off the same cliff
with the same fingerprint), a larger host tier, cache capacity (`slot wait` is 0.00 ms over 0 waits
in every block of a 13-round run), cold misses, context length, and server uptime.

### Not measured

- **The graded gate has not been run at 58 slots.** The 19.81 tok/s on record is 62's. The 17.32 here
  is a probe of eight requests, not the gate, and `README.md` says so.
- 58 is derived from **this** card's 32,607 MiB and a desktop that peaks near 1 GiB. A smaller card
  or a busier display needs a different value, and the failure is silent. Open as
  [#87](https://github.com/nibor1896/Crow/issues/87).
- Whether the two-hour decode collapse of [#71](https://github.com/nibor1896/Crow/issues/71) is the
  same mechanism at a larger scale. Nothing measured for this release ran that long.

## 0.1.0 — 2026-08-10

The model switch: DeepSeek-V4-Flash (preview) is replaced by **DeepSeek-V4-Flash-0731**,
and the release keeps the one promise it made — not slower than 0.0.6.

### The promise, measured

Same driver, same six graded tasks, fresh server per arm, both arms with the shipped
chat template. Raw runs `runs/2026-08-10/0731-pairs`, fingerprinted in
`manifests/runs-2026-08-10.json`.

| | 0.0.6 (preview, 2026-08-09) | 0.1.0 (0731, 2026-08-10) |
|---|---|---|
| decode, tier, median of 3 pairs | 14.73 tok/s | **19.13 tok/s** (+29.9 %) |
| decode, no tier, median | 10.54 | 12.84 |
| stall per miss, tier arms | 0.745 ms | 0.717–0.741 ms |

Even the worst 0731 tier arm (16.17) beats the old median. Within-arm spread 1.19x
against the baseline's 1.09x — the band is indicative, the direction clears the noise.

### Changed

- **New wordmark, and the commands moved beside it.** The banner is drawn in full blocks
  with a box-drawing shadow instead of the shaded bevel. Both ranges are covered by the
  bundled Google Sans Code — measured 2026-08-10 from its cmap: U+2500–257F is 128 of 128
  and U+2580–259F is 32 of 32, against Cascadia Mono at the same counts as a control. A
  glyph outside them falls back to another face and the columns stop lining up, which is
  why the covered range is a test and not a comment. `/help`, `/tools` and `/exit` now sit
  to the right of the mark, one per line, the name in the same yellow a slash command turns
  while it is typed. The column is computed from the widest banner row, so it follows the
  mark instead of being written down beside it.
- **Model: `unsloth/DeepSeek-V4-Flash-0731-GGUF`, UD-IQ3_XXS, 97.1 GiB.** Identical
  architecture (43 layers, 256 experts, top-6). 378,208,256 B per expert — 288 MiB more
  than the preview at 64 slots, inside the measured 599 MiB of headroom (311 MiB left).
  Measured twice: HTTP range requests over the tensor table before downloading, and the
  finished files. Ready-made quantisation on purpose: third-party conversions that do not
  preserve the native MXFP4 experts deviate from the official weights, and the abandoned
  in-house conversion path additionally cost 66 CPU-minutes and 52 GB of RAM for a dry run.
- **The chat template ships as a file** (`templates/0731-chat-template.jinja`) and the
  printed server line carries `--chat-template-file`. 0731 publishes no Jinja template; the
  one embedded in the GGUF fails the model's own golden vector 4 — an action turn opens a
  think block it never closes. The shipped template renders **all four golden vectors
  byte-identically** (jinja2), and the Crow-shaped conversation renders byte-identically
  under the server's own minja too; vectors with roles Crow never sends fail in llama.cpp's
  message canonicalisation before any template runs, which is recorded as the boundary.
- **Sampling follows the model it ships:** `temperature 1.0` (was 0.6 — the preview
  family's value), `top_p 0.95` and `min_p 0.01` sent explicitly for the first time —
  omitting them meant inheriting server defaults nobody chose. The card-vs-
  `generation_config.json` disagreement on `top_p` is recorded next to the value.
  `--reasoning-effort low|high|max` rides in `chat_template_kwargs`, only when set;
  low against max provably changes the rendered prompt at the effort marker.
- **An update removes what the package dropped**, and the first dropped file is the unit
  suite (73,792 B of developer equipment that shipped since 0.0.1). Removal is decided by
  the PREVIOUS package's manifest, never by a directory listing — the exception-list
  design before it deleted a user's own backup folder on its first real run, restored only
  because the deleted folder was itself a copy.
- **The operating point has one source**, `manifests/operating-point.json`:
  version, model, server flags, sampling, and the measured baselines. `README.md`,
  `install.ps1` and the vault page are held against it as raw text by
  `tools/check_operating_point.py`; model paths and sampling defaults are read from it by
  the measurement tools.

### Measured for the first time

- **A diagnostic flag, not the documented line, produced the 1 tok/s.** Decode from the
  README line on a fresh server: **16.05 tok/s** over **one** answer of 108 tokens. That is
  *below* the weakest measured arm (16.17 / 19.13 / 19.25), by 0.7 %, and it is a single
  observation with no run written under `runs/` — it settles the direction, not the number,
  and nothing here is claimed against it. The same line with `-lv 5` writing to an
  interactive console: **0.98 / 1.01 / 1.13 tok/s** over three runs — a factor of 14 to 16.
  The debug
  log is ~40 lines per token; between two consecutive lines the gap is **2.05 ms** into a
  redirected file and **20.3 ms** onto a console, and every CUDA graph launch pays it,
  prefill and decode alike. The card sat at 2895 MHz and 155 W of 575 throughout, which is
  what a GPU waiting on its host looks like. The six gate runs redirect their log to a file
  (`measure-24-gate.ps1`); a hand-started server does not, and nothing said so.
- **Cold against warm prefill on the same server:** 953 tokens at **12.79 tok/s** with the
  expert cache empty, 984 at **62.68 tok/s** once it is not. Within the cold run itself the
  rate climbs from 9.93 tok/s over the first 437 tokens to 17.1 over the remaining 512. The
  filled-context figures below are the warm case and do not describe a first start.
- **What a fresh turn actually sends:** 953 tokens, of which 5 are the message and 39 the
  system prompt. The other **909 — 95.4 %** are the seven tool declarations, measured
  through the server's own `/apply-template` and `/tokenize`. They are unchanged since
  0.0.1 and ride on every request by design: the model's template drops a replayed
  `reasoning_content` when `tools` is empty.
- **Prefill at filled context** (server-counted denominators, fresh server):
  96.13 tok/s at 1,374 tokens · 85.32 at 10,824 · 83.80 at 43,224 · **76.54 at 172,824**.
  The old "8–50 tok/s" came from 86–103-token prompts and does not describe filled
  context — large batches amortise expert fetches. **Measured on the PREVIEW model**, in
  the before-side run that had to happen before the weights left the disk
  (`runs/2026-08-10/before-0731/prefill/`, temperature 0.6, no `--chat-template-file`).
  0731 has not re-run it. The series was published under the 0731 heading until
  2026-08-10 and the attribution is corrected here rather than quietly dropped.
- **VRAM at 200k on one slot:** 31,899 MiB after load, 31,997 under a filled context, of
  32,607. The two previously documented values (31,838 / 32,008) were taken at different
  phases of the same thing; neither said which.
- Preview quality before side, taken before the model left the disk: two gates, all ten
  probe-suite tasks exactly once, temperature pinned 0.6, 8 of 8 graded correct
  (`runs/2026-08-10/before-0731`).

### Not measured, said out loud

- Quality of 0731 beyond the six graded pair tasks and the probe bundle — no like-for-like
  quality comparison against the preview exists, by robin's decision: the preview is
  replaced, not competed with.
- The host-RAM peak (33.73 GiB) and hit-rate figures in the README are preview-series
  measurements; 0731 has not re-run them. Marked as such where they appear.
- What `min_p 0.01` against 0.05 changes in output quality — the value is the
  quantiser's recommendation, not an in-house measurement.

## 0.0.6 — 2026-08-10

### Added

- **The window rolls over instead of hitting the wall.** The server's limit is not a slope: a
  request that arrives at or past `n_ctx` is refused outright and the turn is lost with it. At 90 %
  of the window (`--rollover-at`, `0` switches it off) Crow writes the conversation to
  `rollover-<stamp>.json` and `rollover-<stamp>.md`, empties it, and opens the next one with a note
  naming the transcript, its line count, and the paths the work had reached. `--resume FILE` picks
  an archive back up.

  Two properties came from watching it fail, driven live on 2026-08-10. The check also runs **inside
  the tool loop**, because one round was measured adding 5,253 tokens and a full-budget turn grew a
  single turn by 28,900 — more than the 20,000 that 0.9 leaves between the threshold and the wall.
  And the archive is written **without** the KV cache: the server's slot file has one fixed name, so
  saving it would put the archive's cache over the live one.

  The note points at the `.md` because the JSON is unreachable: `json.dump` writes one line, a real
  archive measured 104,618 bytes on it, and `read_file` caps at 16 KB. Pointed at the JSON, the
  model guessed a directory that does not exist, scanned a user profile twice, and spent **402 s
  across seven tool rounds** before it read anything.

- **`/tools`.** The seven tools were only ever visible in a request nobody reads. The listing is
  derived from `TOOLS` rather than written beside it. The header carries it, and the repository URL.

- **A slash command turns yellow as it is typed.** `input()` cannot do this — the console stays in
  cooked mode and hands nothing over until Enter — so the line is read one key at a time. Piped
  input and any platform without `msvcrt` or `termios` fall back to `input()`. Known cost: the
  console's own line editing goes with it. Backspace, Ctrl+C and Ctrl+D are handled; arrow keys and
  history are not.

- **`--max-tool-rounds`.** The limit that decides how long a turn runs was a constant with no flag,
  and the message it printed sent the reader looking for a knob that did not exist.

### Fixed

- **A spent tool budget ended in a bracket.** Driven live with `--max-tool-rounds 0`: the model
  produced 102 tokens, `thinking 100%`, and the user was shown nothing at all. One more round now
  goes out — tools still declared, or the template drops the replayed reasoning and the cache breaks
  (#60, 242.3 s against 1.6 s) — carrying a turn that says the budget is spent and asks for what was
  found, what was missed, and what comes next.

  Its first live run reported reading a line it had never read and described one that is blank, so
  the request names the case: if you ran nothing, say you ran nothing. Measured after the change on
  the same question: *"Ich habe nichts gelesen."*

- **Calls that will never run are no longer appended.** An assistant turn whose `tool_calls` have no
  `tool` message behind them is a broken prefix for every later turn, and the old bare `break` left
  one behind every time a budget ran out.

- **`cache warm` was a promise nobody checked.** Measured 2026-08-10: a start printed
  `resumed: 36 messages, cache warm` and the next turn came back `cached 0/21004` after **469.51 s**
  of prefill. A 200 from `action=restore` says the file was read, not that the slot holds the prefix
  about to be sent. The save now records the server's `n_saved`, the restore compares `n_restored`,
  and the first turn settles it: a warm claim followed by `cached 0` says so in one line. A server
  that reports neither figure is still believed — silence is not a contradiction.

- **An update can run while the server is up.** Windows locks a running binary, and the moment the
  client says a new version exists is the moment `llama-server` is up in the other terminal. The
  files in `bin\` are renamed to `.old` first — renaming a running executable is permitted, deleting
  it is not, both measured. What cannot be moved is named and the install stops there. The `.old`
  files that stay are reported as staying, not counted as removed.

  Driven end to end on 2026-08-10, **0.0.4 → 0.0.6 with the server serving throughout**: 17 files
  renamed, 26 extracted, 25 of 25 hashes matched, **2 `.old` removed and 15 reported as still held**
  — and 15 were still on disk afterwards, held by the process that was named. The version this
  replaces would have printed "17 stale .old files removed", which is false for 15 of them. The
  running server kept answering; the new binary took over on its next start.

### Tests

- `install.ps1 -Selftest`: 51 checks, up from 42, nine of them reaching the new code — two against a
  real lock rather than a simulation. The first version of that fix sat below the selftest's exit
  and reported 42 of 42 green without executing a line of itself.
- `cli/test_crow.py`: 201, up from 122.
- `tools/probe-rollover.py`: new. Drives the real CLI through a fake OpenAI endpoint at `n_ctx=100`
  — 35 checks in about a second, no model loaded.

### Not done

- Nobody has watched the 15 `.old` files leave. They are swept on the next install that finds them
  unheld, and `Move-LockedAside` clears a stale one before it renames over the same name — both
  covered by the selftest against real locked files, neither seen on a live machine after the server
  finally stopped.

## 0.0.5 — 2026-08-09

### Added

- **Crow acts.** The client executes the model's tool calls, hands the results back and asks
  again, up to 24 rounds: `read_file`, `write_file`, `edit_file`, `list_dir`, `find_files`,
  `search_text`, `run_command`. Until now a reply could only be printed and copied out by hand.

  Three properties carry a reason rather than a preference. `read_file` takes a line range and
  caps at 16 KB, because prefill is the cost that matters — a 100 KB file is ~25,000 tokens, and at
  the 8–50 tok/s prefill measures depending on cache state that is between eight and fifty minutes
  before the model has read a word of it. `write_file` and `edit_file`
  refuse a file this session has not read, because a model that writes what it has not read
  overwrites whatever it does not know about. And the system prompt deliberately carries no
  working directory: it is byte 0 of the prefix, so a session saved in one folder would be
  worthless resumed from another.

  Driven live on 2026-08-09 — `list_dir` → two `read_file` calls → a correct answer, five turns
  at 11.79–16.72 tok/s with the prompt cache holding (`cached 4140/4722` by the last turn).

- **`--moe-stream-l2 <GiB>`: an optional host-RAM tier below the VRAM slots.** A miss that finds
  its expert in page-locked host memory uploads at 47,357 MB/s instead of fetching it off the
  drive at 10,593 — **56.7 µs against 401.5 µs per work item, 7.08x.**

  Measured end to end, paired on identical tasks, 32 GiB tier: **15.89 / 14.73 / 14.53 tok/s with
  against 10.81 / 10.54 / 10.09 without — 1.40–1.47x**, and the cost of a miss falls from
  1.28-1.35 ms to 0.73-0.75, a factor of 1.79. Within-arm spread was 1.09x and 1.07x, narrower than the difference.

  **The arrangement is half the result, and two of them measured nothing.** Repeating the same
  ten gate tasks per run gave 7.65 and 15.77 tok/s at *identical* configuration, because the
  second run meets the cache the first warmed — with 32 GiB of experts held, that shared state is
  the subject. Giving each arm different tasks removed the carry-over and replaced it with arms
  solving differently hard problems. What works is both at once: same tasks within a pair, fresh
  tasks across pairs, each arm on its own server.

  **It costs 32 GiB of page-locked memory** — process peak goes from 1.28 GiB to 33.73 GiB, and
  that memory is unavailable to the rest of the machine until the server exits. The flag defaults
  to off; the installer puts it into the command it prints above 60 GB of detected RAM, because 32 GiB on a ~64 GB host
  is the only ratio that has been run.

  **Unmeasured:** any other tier size, and whether the factor survives a full 200k window. Every
  paired run stayed under 6k of context.

- **`--slot-save-path` is in the printed command, and the installer creates the directory.** The
  server refuses to start against a path that is not an existing directory, so the line it
  printed could fail on a fresh install. Without the flag a restart re-prefills the whole history.
  The 22 ms restore is measured; the ~35 minutes for 23,400 tokens is extrapolated from a run that
  was aborted at 10 %.

### Fixed

- **A cache race that no throughput number could show.** The tier's first version handed out a
  resident slot and released its lock; another worker took the same slot as an eviction victim
  and read a different expert into it mid-upload. The model emitted 8,191 characters of
  `<<<<<<<<` instead of an answer — at 31–35 tok/s, a fast run by every counter that existed.
  A slot is now pinned while it is read, and a filled one is published only after its bytes have
  left for the GPU.

- **A failed session restore repeated forever.** Point the server at a different
  `--slot-save-path` than the one a session was written to and the client asked for a KV state
  that was not there, on every start, printing two server errors each time. The claim is now
  withdrawn when it is disproved. The first failure still prints — the client cannot know whether
  the file exists, because the path belongs to the server.

- **The tool call line showed half its JSON.** A raw cut at 80 characters lands mid-string often
  enough to be the normal case, and `read_file({"path":"…","start_line":1,"` reads as a malformed
  call rather than a shortened one. Values are shown now, paths cut from the front.

- **The tier's allocation line was invisible.** At the default verbosity `llama-server` prints no
  INFO from `llama.dll` at all, so a user who passed the flag saw no confirmation anywhere. It is
  printed at WARN — a deliberate misuse of the level, because what it reports is that GiB-scale
  memory has been page-locked away from the rest of the machine.

### Changed

- **`probe-suite.py` defaults to temperature 0.6, matching the CLI.** At 0, under the model's own
  chat template, greedy decoding never leaves the reasoning block: sixteen answers in a row came
  back `finish_reason=length` with an empty content field. `--temperature 0` remains available for
  reproducing the older series and is now a deliberate act.

- **`measure-24-gate.ps1` gained `-Only` and `-Warm`.** Repeating a task measures the cache, not
  the configuration; a warm-up pass on tasks the graded pass does not use keeps the first graded
  task from paying for the cold model, cold slots and empty tier at once.

## 0.0.4 — 2026-08-08

### Fixed

- **There was no way to update.** `install.ps1` refused any non-empty target with
  `pass -Force to overwrite` and exit 1 — and the documented one-liner is
  `irm … | iex`, which cannot be given parameters at all. The advice it printed
  could not be followed by the person reading it. Moving from one version to the
  next meant deleting `%LOCALAPPDATA%\Crow` by hand, and nothing said so.

  The installer now reads the version out of the `cli\crow.py` it finds in the
  target — the same pattern `pack-release.ps1` stamps it with, so the two cannot
  disagree about where the number lives — and decides from it. An older install
  updates. The same version does nothing and exits **0**, not 1. A newer install
  is not overwritten, and a directory that does not identify itself as a Crow
  install is not touched; both refuse and print the `[scriptblock]::Create`
  invocation that *can* carry `-Force`, because naming a switch the user's command
  cannot pass is not a route.

  Driven end to end over the real 0.0.1 and 0.0.2 packages, not only in the
  selftest: install, update, same-version, downgrade-refused, stranger's-directory
  refused. The refusals left the target untouched.

- **Nothing told anyone a new version existed.** The client asks the release API
  on start and prints the version together with the command that installs it. The
  request is fired before the banner is drawn, so it overlaps work that happens
  anyway, and it is given at most 1.5 s of the start. Every failure — no network,
  rate limit, an answer we do not recognise — is silence rather than an error
  between the user and their prompt. `--no-update-check` switches it off.

  **This cannot reach installations that predate it.** 0.0.3 and earlier have no
  check in them and will never announce 0.0.4; that generation has to be updated
  by hand, once.

### Added

- **`crow --version`.** The number existed only inside the start banner.
- **The installer resolves the newest release itself** when no `-Version` is
  given, so the same one-liner installs the current version without anyone editing
  a default. The hard-coded number stays as the offline answer.
- **`Updating` in the README**, which said nothing about it before.

### Tests

Client suite 91 → 108. Installer selftest 24 → 37. The new cases include the ones
that must refuse: an equal version, a newer install, an unparseable version string.
A comparison that read garbage as `0.0.0` would announce an update to every user on
every start, which is worse than no notice at all.

### Not done

Nothing is deleted on update. The 95.9 GiB model lives under the install directory,
so a "clean" install would throw it away and re-download it over the user's
connection. Files a newer package no longer ships are therefore left behind.

## 0.0.3 — 2026-08-08

### Fixed

- **The server command the installer prints was missing `--port 8081`.** `llama-server`
  defaults to 8080 and the client defaults to 8081, so following the instructions
  exactly produced a server the client could not find — and on Windows 8080 is
  frequently already held by something else, which is how it surfaced: a bind
  failure rather than a silent mismatch. The operating-point page in the project's
  notes carried the flag all along; the shipped command had dropped it.

- **The installer verified nothing, and the word was on the screen anyway.** Step 3
  was called *Verifying*: it printed the archive's SHA256 and compared it with
  nothing, and the `MANIFEST.json` in the package — a hash per file — was never
  read.

  The assumption underneath was that a damaged archive would fail to extract.
  Measured 2026-08-08: it does not. A single flipped byte inside the compressed
  stream, at three different offsets, and `Expand-Archive` extracted all three
  **without an error** and wrote the wrong bytes to disk. TLS covers the wire;
  nothing covered the file. A damaged install would have surfaced later as a DLL
  that will not load and been diagnosed as anything but a bad download.

  Verification now happens after extraction, against the manifest, file by file.
  A mismatch names the file, says the install is damaged, and exits 1 instead of
  printing the next steps. Both directions are driven end to end in the suite:
  an honest package passes, a package whose manifest disagrees with its contents
  fails.

## 0.0.2 — 2026-08-08

The first release existed for about an hour before it was installed, and both
defects it shipped were found by running it rather than by reading it.

### Fixed

- **A finished install reported exit code 255.** `& nvidia-smi … | Select-Object -First 1`
  ends the pipeline after one line, PowerShell kills the process, and
  `$LASTEXITCODE` lands on `-1`. Nothing later touched it, so the installer
  handed that back after doing everything right. Any caller checking an exit
  code read a success as a failure.
- **The install closed the user's shell.** The fix for the above put an explicit
  `exit 0` at the end. In a script file `exit` leaves the script; in a string run
  through `iex` — which is how this is installed — it leaves the **host shell**.
  The window vanished the instant the install finished, before the three
  commands it had just printed could be read.
- **The command the installer prints was missing `--jinja`.** Without it
  `llama-server` uses its own built-in template instead of the model's, the
  client's replayed reasoning is dropped, and the prompt cache breaks on every
  turn: 138.8–242.3 s of re-prefill against 1.6–2.2 s. Following the installer
  gave the slow path while following the README gave the fast one.

### Added

- **The run ends on ENTER.** A console opened for the install closes with it, and
  the model to fetch and the two commands to run appear nowhere else on screen.
  `-NoPause` for a script driving the install; skipped when the host has no
  console, because a wait nobody can satisfy is a hang.
- **`-SourceUrl`** takes an http(s) URL or a local `.zip`. An installer whose only
  source is a release can never be tried before that release is published — the
  first person to run it would be the first person to test it.
- **The last screen names the model properly**: what it is, who quantised it,
  where it lives, and the one trap measured on 2026-08-07 — `hf` reports success
  even when it reached nothing.

### Changed — the client

- **An assistant turn now carries its reasoning back into the history.** The
  chat template renders a kept turn as `<think>…</think>`, so omitting the field
  left an empty think block and the prefix diverged exactly where the thoughts
  began. Everything behind that point was re-read, however short the thoughts
  were: 48 tokens of reasoning cost 2,018 tokens of prefill. Measured across
  three task sets — dropping it re-reads 0.909–0.986 of the previous turn's
  output, replaying it re-reads 0.008–0.016. Live: turns 2 and 3 prefilled 18 and
  19 tokens where they had cost about 4,256 before. ([#60](https://github.com/nibor1896/Crow/issues/60))
- **Every request carries a one-entry `tools` array**, for the prompt cache
  rather than for the tool. This template keeps a past turn's thoughts only while
  tools are present; with none, both variants render byte for byte the same.
  A returned tool call is reported, not executed — that is
  [#58](https://github.com/nibor1896/Crow/issues/58).
- **The context bar asks the server** instead of adding `prompt_n` and
  `predicted_n`, which was wrong three times over and ran the bar *backwards*
  while the conversation grew. It reads `usage.total_tokens` now.
  ([#60](https://github.com/nibor1896/Crow/issues/60))
- **The timing line carries `cached N/M`** — how much of the prompt the server
  did not have to read again, per turn, reported rather than inferred.

### Tests

Client suite 75 → 91. Installer selftest 13 → 20, including the two cases that
cover the shell it used to close. `tools/probe-prefix-cache.py` and its suite are
new: they are the measurement behind the reasoning decision, not a description of
it.

## 0.0.1 — 2026-08-08

First package: the patched `llama-server` with the expert-streaming path, every
runtime library it needs, and the Python client. 26 files, 506.4 MB,
self-contained — the packer refuses to write an archive whose binaries import
something the archive does not carry.

Superseded within the day by 0.0.2. It installs, and then reports a failure and
closes the window.
