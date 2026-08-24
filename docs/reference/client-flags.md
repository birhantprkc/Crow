## Client flags

| flag | default | |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8082/v1` | Qwen's port. 0731 listens on `:8081` |
| `--reasoning-effort` | unset | per chat via `/reasoning`. Levels come from the manifest |
| `--rollover-at` | `0.9` | archive and start fresh at this share of the window. `0` disables |
| `--max-tool-rounds` | `24` | `0` answers without running any tool |
| `--mode` | `auto` | `manual` asks before writing and executing, `allowedit` before executing |
| `--no-review` | off | stop the model saving memories and skills after a turn |
| `--no-memory-approval` | off | let the review write to memory without asking. **The gate is on by default** |
| `--rounds` | off | full timing line after every tool round |
| `--show-reasoning` | off | stream the reasoning. `/thoughts` toggles it |
| `--no-session` | off | do not resume the last session, do not save this one |
| temperature / top_p / min_p | `1.0` / `0.95` / `0.01` | written once, in `cli/crow_core.py` |
