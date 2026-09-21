# Acceptance protocol — issue #207

**search_text walked the whole work tree as text — the 105 GB CNQ container included —
and the turn hung mid-pair: spinner forever, no follow-up request, only killing the app ended it.**

Issue: https://github.com/nibor1896/Crow/issues/207
State: **LIVE-ACCEPTED 2026-09-21, attempt 2** — evidence below and in §"Attempt 2 result"

## What happened (measured, 2026-09-21 live session)

The min_p 0.01 retest session: Crow GUI against crow-nest serve on 127.0.0.1:8099, mode `yolo`.
The engine finished request 3 at 10:54:28 UTC — `finish tool_calls`, two calls — and closed the
stream cleanly (`200 OK`). `run_command` (call_0) was answered. `search_text` (call_1) never
returned: no follow-up POST for 5+ minutes, the GUI kept drawing the turn as running, and only
closing both processes ended it.

`session.json` ends mid-pair — the last assistant turn carries both calls, only call_0 has a
tool message. That is the broken-prefix class, seen live from Crow's own side.

The cause is in `tool_search_text`: `glob` defaults to `"*"`; the walk pruned `target/` and the
model containers not at all; the file was opened `errors="replace"`, which reads a 105 GB NVFP4
blob as "text" without ever raising; and the `MAX_HITS`/`MAX_TOOL_BYTES` caps fire only when a
hit is found — the pattern had no hits in binary, so the tool read every byte. Not a hang — a
multi-hour read, indistinguishable from one.

The same call carried the generation corruption of the turn: `pattern`, `pattern_2:
"placeholder"`, `regex` — three keys for one argument, two declared by no tool, both silently
absorbed by `**_`.

## The fix (ripgrep's three defaults, plus one note)

| Bound | Where | Behavior |
|---|---|---|
| Binary skip | `search_text`, before the text open | A NUL byte in the first `SEARCH_SNIFF_BYTES` (4 KiB) means the file is skipped. The containers are NUL-dense from byte one. |
| Size cap before open | `search_text`, `os.path.getsize` first | Files over `SEARCH_MAX_FILE_BYTES` (2 MiB) are never opened; the result says how many were skipped. |
| Wall-clock deadline | `search_text` and `find_files`, checked per directory | Past `SEARCH_DEADLINE` (30 s) the walk stops and **returns** what it has plus `[stopped after Ns -- … narrow the root, or pass a glob]` — a result, not an exception (the OpenAI Agents SDK non-fatal default; also `run_tool`'s standing contract). |
| Prune list | both tools, shared `SEARCH_SKIP_DIRS` | `target` and `.cache` join the set; the two tools now read one list instead of two copies. |
| Unknown-key note | `run_tool` | `[unknown argument(s) ignored: pattern_2, regex]` — one bracket note, the shape `coerce_declared_containers` already uses. No declaration, no note. |

Documented residual (ticket §4): a tool blocked in the kernel (FIFO `open()`, dead mount)
cannot be interrupted from another Python thread; the deadline is cooperative. A turn-level
watchdog at the GUI seam is a separate ticket if the class recurs.

## Local verification (this machine, capped at 1–1.5 GiB)

- `SearchIsBoundedTests` — 6 new tests (binary skip, size cap, prune, deadline-as-result,
  unknown-key note, clean-call negative): **6/6 OK** in 1 ms.
- Full `cli/test_crow_core.py`: **978/978 OK** (104 s).
- `cli/test_crow_gui.py`, tool-filtered: **35/35 OK**.
- `tools/check_shared_core.py`: 78/79 — the one failure (`tool_append_file` undeclared) is
  pre-existing, identical on the unmodified tree (verified via stash).

## robin's live acceptance (Crow GUI — no curl)

### Attempt 1 (2026-09-21, 13:23–13:26) — rounds 1–5 passed; a sibling defect ended the session

Five tool rounds completed in seconds each with the search bounds live — then round 6's `run_command`
printed into the GiB scale, `capture_output=True` gathered all of it, Crow's python ballooned until
13.6 GiB of it was swapped, and the kernel's global OOM killer shot `serve` (46.8 GiB pinned, the
biggest RSS). Same ticket class, one layer up: the capture, not the result, is what fills the
machine. Fixed in `d22f3a2` (32 MiB cap inside the reader threads, kill + result the model can act
on; `read_image` refuses oversized files before reading; clock arm and result contracts unchanged;
982/982 core, 46/46 GUI tests). Full account in the
[ticket comment](https://github.com/nibor1896/Crow/issues/207#issuecomment-5759863185).
No reboot was needed — the pinned tier was released cleanly.

### Attempt 2 (after d22f3a2, reinstalled)

Same setup as the min_p retest:

1. Terminal 1: `crow-nest` (serve on 8099, default tier).
2. Terminal 2: `crow --base-url http://127.0.0.1:8099/v1`
3. Working area on the crow-nest repo, mode as you like (`yolo` was the incident's mode).

**Test 1 — the incident's shape.** Send:

> Search this repository for the string `quuxblorb` and report every file that contains it.

Expected: the round **completes** in seconds. `search_text` returns `no match for quuxblorb`
plus a `[skipped N file(s) …]` line (the containers and `target/` are skipped); the turn goes
on; the engine log shows the follow-up POST. No endless spinner at any point.

**Test 1b — the second incident's shape (capture bound).** Send:

> Run `python3 -c "print('x' * 500000000)"` and tell me what came back.

Expected: the command is killed at the 32 MiB cap within a moment; the tool result is
`error: command printed more than 32 MiB and was killed -- pipe it through head, or write it to a
file and read the range: …`; the round continues; serve stays alive; the machine's memory is
untouched (no zram spike).

**Test 2 — a real search still finds.** Send:

> Find where the sampler applies min_p and quote the line.

Expected: hits from `engine/src/sample.rs` (and/or kernels.rs) — real hits, no skipped-line
surprises on text files, turn completes.

**Test 3 — unknown args become visible (opportunistic).** If a turn's tool call ever carries
deformed keys again (the `pattern_2` class), the tool result begins with
`[unknown argument(s) ignored: …]` in the GUI transcript. No action needed; this is the
corruption detector riding along.

After the tests: engine log clean, no request left unanswered, and the session file ends on a
`tool` message, not mid-pair.

### Attempt 2 result (2026-09-21, ~14:0x) — ALL TESTS PASSED

Evidence from robin's screenshots and the session file (`session.json`, 36 messages):

- **Test 1 PASS, verbatim.** The tool result in the transcript:
  `no match for quuxblorb` + `[skipped 373 file(s) over 2 MiB or binary -- a hit in them is not
  a hit you can use this way]`. The round completed in seconds, follow-up requests flowed
  (5 rounds, 4 tool calls, ~1m31s wall including the model's own second-opinion grep over
  43,994 files). No stall, no spinner.
- **Test 1b PASS.** The model ran the 500 MB command **redirected to /dev/null** and hashed the
  same 500 MB instead of capturing it (its own trace text: "same 500 MB, hashed instead of
  captured") — the pipe never carried the bomb, so the 32 MiB cap stood as the untriggered
  backstop. The harness survived, the round completed (4 rounds, 35.9 s), serve stayed alive,
  and the model itself verified "62 GiB total, 9.2 GiB available, so no OOM". The cap's firing
  behavior is covered by `CommandCaptureIsBoundedTests` (unit) rather than this live run —
  recorded here honestly.
- **Test 2 PASS.** Exact hits with line numbers: `sample.rs:720-727` (the min_p filter on the
  logits), `sample.rs:321-323` (`ln_min_p` computed host-side), the device twin via
  `kernels.rs` params, and serve's parsing — chain position (typical → min_p → temperature →
  top_p → XTC) quoted correctly. 5 rounds, completed.
- **Bonus: the memory self-healed correctly.** After the wipe, the model's review wrote ONE
  entry: "search_text skips files >2MiB/binary (reported as 'skipped N file(s)') — for a truly
  exhaustive string search use `grep -rl --exclude-dir=.git <str> .` instead." The poisoned
  "rotated extensions" entry did not come back.
- Two trivia claims in the model's Test-2 answer were checked and are NOT in the code: no stale
  "minp accepted and ignored" log line exists in `serve.rs` (only the historical doc note at
  line 310 that the era is over), and no `ths_sampler` test-name typo exists.
