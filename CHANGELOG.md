# Changelog

Every entry says what changed and what was measured to justify it. A number here
carries the conditions it was taken under, or it says that it is unmeasured.

This file records the **released** history. The full reasoning behind each change
is in its commit message and on its issue; this is the short version.

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
