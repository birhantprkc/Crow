[← README](../../README.md) · [Docs index](../README.md)

# Testing

## Suites

Run from `cli/`, one file per interpreter — each suite's preamble owns the sandbox
redirect, and a combined run leaves the isolation guards red. Case counts collected 2026-09-24
on `release-2026-09-24` (b9cac62) with `unittest`'s loader, under the runtime venv's Python.

```
python -m unittest test_crow
python -m unittest test_crow_core
python -m unittest test_crow_gui
python -m unittest test_crow_remote
```

| | cases | covers |
|---|---|---|
| `test_crow.py` | 476 | the terminal client |
| `test_crow_core.py` | 1455 | the shared core |
| `test_crow_gui.py` | 896 | the window's API and page |
| `test_crow_remote.py` | 53 | the phone mirror: server, pairing, devices, QR, the Tailscale state (#249, #290) |

No test writes into a real installation. `test_crow_gui.py` carries a case that
walks every path constant in both modules and rejects any pointing into a real
installation. It asks `crow_platform` where that is rather than naming a path:
one directory on Windows (`%LOCALAPPDATA%\Crow`), four on Linux (config, data,
state, cache). A guard that only knew the Windows spelling would not be strict
there — it would be empty.

Run them under the runtime venv (`~/.local/share/crow/venv/bin/python` on Linux), not the
system Python: that venv is made with `--system-site-packages`, which puts pip's `webview` and
the distribution's PyGObject in one interpreter. `just test` does exactly that, and `just check`
runs lint, the four suites, `check_shared_core`, `check_operating_point`, `check_gui_prereqs`
and `bash install.sh --selftest` in that order.

`AWriteParsesWhatItWroteTests` (#251) calls the real `node --check` and is skipped when `node`
is not on `PATH`; the skipped count in a run's `OK (skipped=N)` line includes it then.

CI (`.github/workflows/ci.yml`) runs ruff, the suites, `check_shared_core` and
`check_operating_point` on `ubuntu-latest` and `windows-latest` (Windows without
`test_crow_gui`), and `install.sh --selftest` on Linux only. `check_gui_prereqs` is not in CI.

The window's own suite needs `pywebview` importable; without it the folder-picker
cases error out on `No module named 'webview'` and the rest still runs.

Delete `__pycache__` before a counter-check. A stale bytecode of the same size
survives a revert and the check measures the version it was meant to disprove.

## Checkers

Run from the repo root.

| | |
|---|---|
| `tools/check_shared_core.py` | holds every surface against `manifests/shared-core.json`: shared names exist once, shared wordings are written once, and that once is in the core |
| `tools/check_operating_point.py` | the server command line was spelled out in three places and they disagreed. Held against `manifests/operating-point.json`. **It reads the documents as raw text**, not as a second copy of the manifest: `install.ps1` must carry every key, and every key must be printed correctly by at least one live page — today that is [`docs/operating-points.md`](../operating-points.md). `README.md` stays in the list for its version badge and for the day a server line comes back to it. `tools/test_check_operating_point.py` drives 20 cases, including the ones that must go red |
| `tools/check_chat_template.py` | DeepSeek-V4-Flash ships no Jinja template. The hand-written one is held against the vectors DeepSeek published, byte for byte |
| `tools/check_routing_tables.py` | REAP-pruned checkpoints can carry duplicate expert ids, which crashes CUDA `ggml_mul_mat_id()` on the tokens that hit them. Reads the static routing table without loading the model |
| `tools/check_gui_prereqs.py` | what the window stands on: font, glyph coverage, runtime versions. It checks the machine it runs on, not the code, and is not run in CI |

The checkers carry their own suites: `tools/test_check_operating_point.py`,
`tools/test_check_shared_core.py`, `tools/test_check_gui_prereqs.py`, plus
`tools/test_gguf_header.py` and `tools/test_run_server_block.py`.

## The manifest

`manifests/shared-core.json` has two classes, and an entry belonging to neither
is a setup error rather than a skip.

| class | predicate |
|---|---|
| names | exactly one definition in column 0 across core and surfaces, and it is in the core |
| wordings | the wording occurs exactly once across core and surfaces, and that once is in the core |

A rule that is not in the manifest is not checked. The level line
(`asks before …`) was written independently in both surfaces until 2026-08-24
for exactly that reason.
