# Architecture

Four modules under `cli/`. Line counts measured 2026-08-31.

| | lines | holds |
|---|---|---|
| `crow_core.py` | 16407 | every rule both surfaces obey: tools, the turn loop, memory, skills, MCP, remote providers, sessions |
| `crow_gui.py` | 11353 | the window. Page, pywebview API, the browser pane, and nothing a rule depends on |
| `crow.py` | 2488 | the terminal client. Screen, slash commands, `VERSION` |
| `crow_voice.py` | 238 | dictation: microphone and recogniser only |

## The split

A rule lives in `crow_core.py` and is called from a surface. A surface owns its
screen and nothing else.

| in the core | in a surface |
|---|---|
| what a tool does, what a level asks before, what a turn costs | how a row is drawn, which key closes a menu |
| the sentences a user reads | where on screen they appear |

Two surfaces that write the same sentence agree with each other right up to the
day one is edited. `manifests/shared-core.json` names what may exist only once,
and `tools/check_shared_core.py` enforces it — see [Testing](testing.md).

## Registries

Rebuilt in place by `mcp_apply()`, never rebound: `crow.py` does
`from crow_core import TOOLS`, which binds the value.

| | |
|---|---|
| `TOOLS` | the declarations sent in each request. Built-ins first, MCP above |
| `TOOL_IMPL` | name → callable. A name here that is not in `TOOLS` is unreachable |
| `TOOL_CLASS` | name → `reading` \| `writing` \| `executing`. Absent means `executing` |
