# Testing

## Suites

Run from `cli/`. Counts measured 2026-08-24.

```
python -m unittest test_crow test_crow_core test_crow_gui
```

| | cases | covers |
|---|---|---|
| `test_crow.py` | 418 | the terminal client |
| `test_crow_core.py` | 592 | the shared core |
| `test_crow_gui.py` | 391 | the window's API and page |

No test writes into a real installation. `test_crow_gui.py` carries a case that
walks every path constant in both modules and rejects any pointing into
`%LOCALAPPDATA%\Crow`.

Delete `__pycache__` before a counter-check. A stale bytecode of the same size
survives a revert and the check measures the version it was meant to disprove.

## Checkers

Run from the repo root.

| | |
|---|---|
| `tools/check_shared_core.py` | holds every surface against `manifests/shared-core.json`: shared names exist once, shared wordings are written once, and that once is in the core |
| `tools/check_operating_point.py` | the server command line was spelled out in three places and they disagreed. Held against `manifests/operating-point.json`. **It reads `README.md` as raw text** — the nine flags under *Operating point* have to stay there, and stay literal |
| `tools/check_chat_template.py` | DeepSeek-V4-Flash ships no Jinja template. The hand-written one is held against the vectors DeepSeek published, byte for byte |
| `tools/check_routing_tables.py` | REAP-pruned checkpoints can carry duplicate expert ids, which crashes CUDA `ggml_mul_mat_id()` on the tokens that hit them. Reads the static routing table without loading the model |
| `tools/check_gui_prereqs.py` | what the window stands on: font, glyph coverage, runtime versions |

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
