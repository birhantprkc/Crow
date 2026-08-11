# Changelog

Every entry says what changed and what was measured to justify it. A number here
carries the conditions it was taken under, or it says that it is unmeasured.

This file records the **released** history. The full reasoning behind each change
is in its commit message and on its issue; this is the short version.

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
