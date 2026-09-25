[← README](../../README.md) · [Docs index](../README.md)

## Client flags

| flag | default | |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8083/v1` | Flash-Next's port, the default since 2.0.0. The 27B listens on `:8082`, 0731 on `:8081` |
| `--reasoning-effort` | unset | per chat via `/reasoning`. Levels come from the manifest |
| `--reasoning-budget N` | the model's | cap the thinking per request; per chat via `/budget`, `/budget off` lifts it. The default comes from the manifest, so nothing has to be typed. Carries its own end-of-thinking message -- a cap without one cuts the answer in half (#176) |
| `--rollover-at` | `0.9` | archive and start fresh at this share of the window. `0` disables |
| `--rollover-digest-tokens N` | `400` | cap for the model's own digest in the rollover note, asked on the still-warm prefix before the cut. The leg sends at least 2000 (#205); an answer cut off at the cap is marked as cut (#210). The leg asks with the same `--base-url`, `--model` and `--api-key` as the turn (#214; before, it sent no key and the model name read from the server). `0` disables (#154) |
| `--context-clear-at SHARE` | `0.65` local, off remote | replace tool results older than the last 5 rounds with a one-line stub at this share of the window, in batches that free at least 10 % of it; below the rollover, which stays the last resort. `0` disables (#263) |
| `--max-tool-rounds` | `24` | tool rounds per turn; after the last one the turn is told the budget is spent and answers in the next request (#278: no extra round is decoded and refused). `0` answers without running any tool |
| `--mode` | `auto` | `manual` asks before writing and executing, `allowedit` before executing, `yolo` asks for nothing and means it -- outside paths and git commit included; `git_push` still asks, and the level never outlives the process |
| `--no-review` | off | stop the model saving memories and skills after a turn |
| `--no-memory-approval` | off | let the review write to memory without asking. **The gate is on by default** |
| `--rounds` | off | full timing line after every tool round |
| `--show-reasoning` | off | stream the reasoning. `/thoughts` toggles it |
| `--no-session` | off | do not resume the last session, do not save this one |
| `--root DIR` | unset | the folder tool writes are confined to; states it and creates it (`.crow/root.json`). In the window it also binds the restored chat and becomes the folder the next start opens, as a pick in the chip does (#303, [window](../user-guide/window.md)) |
| `--language NAME` | `$CROW_LANGUAGE`, else unset | pin the language of the model's replies, e.g. `--language English`. Unset keeps the rule "reply in the language the user wrote in", which the FIRST message decides: a chat opened with "Hey" was answered in German and stayed German through an English task (2026-09-19, both engines). It replaces that sentence in the system prompt, so a session resumed under another language pays one full prefill |
| temperature / top_p / min_p | `1.0` / `0.95` / `0.01` | the floor, written once in `cli/crow_core.py`. The manifest's shared block and the served model's entry override it: the two Qwen3.8-Flash-Next points send `min_p` 0.0, `top_k` 20, `presence_penalty` 0.0 ([thinking and sampling](../operating-points.md#thinking-and-sampling-per-point)) |
| output cap | `16384` | `max_tokens` on every request, local and remote (`MAX_TOKENS`). No flag: `CROW_MAX_TOKENS` sets it once per process. It also sets how large a file one `write_file` carries ([tools](tools.md#write_file-and-append_file-244-251-252-254)) |
| goal step budget | `60` min, `10` nudges | the window's goal engine only (the terminal has none). No flag: `CROW_GOAL_STEP_MINUTES` and `CROW_GOAL_NO_PROGRESS_TURNS` override the `goal_step_minutes` / `goal_no_progress_turns` settings; `0` switches one off ([settings](settings.md#294--goal-step-budget), #294) |

## #145 — the terminal's budget flags

| flag | default | what |
|---|---|---|
| `--turn-token-budget N` | `0` (off) | decoded tokens one turn may spend before it is told to answer |
| `--bundler PATH` | unset | the esbuild `build_bundle` uses before its own search; the window's `bundler` setting (#274) |
| `--subtask-max-tokens N` | `0` (= the output cap, 16384 unless `CROW_MAX_TOKENS` sets it) | output cap for one delegated subtask |

The retry cap needs no flag: the fourth identical failing call to an uncached tool is refused
before it runs, always.
