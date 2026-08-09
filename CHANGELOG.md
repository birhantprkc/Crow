# Changelog

Every entry says what changed and what was measured to justify it. A number here
carries the conditions it was taken under, or it says that it is unmeasured.

This file records the **released** history. The full reasoning behind each change
is in its commit message and on its issue; this is the short version.

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
