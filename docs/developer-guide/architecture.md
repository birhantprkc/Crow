[← README](../../README.md) · [Docs index](../README.md)

# Architecture

Five modules under `cli/`. Line counts measured 2026-09-16.

| | lines | holds |
|---|---|---|
| `crow_core.py` | 16652 | every rule both surfaces obey: tools, the turn loop, memory, skills, MCP, remote providers, sessions |
| `crow_gui.py` | 12012 | the window. Page, pywebview API, the browser pane, and nothing a rule depends on |
| `crow.py` | 2497 | the terminal client. Screen, slash commands, `VERSION` |
| `crow_platform.py` | 637 | the platform seam: where things live, how a process is found and killed, per OS |
| `crow_voice.py` | 238 | dictation: microphone and recogniser only |

## The split

A rule lives in `crow_core.py` and is called from a surface. A surface owns its
screen and nothing else.

| in the core | in a surface |
|---|---|
| what a tool does, what a level asks before, what a turn costs | how a row is drawn, which key closes a menu |
| the sentences a user reads | where on screen they appear |

**The window is the client.** `crow.py` is still shipped, still tested and still
the one that runs with the standard library alone, but nothing in the product
assumes it: the window boots the server, holds the goal, draws the panels and is
what every user-facing page describes first.

Two surfaces that write the same sentence agree with each other right up to the
day one is edited. `manifests/shared-core.json` names what may exist only once,
and `tools/check_shared_core.py` enforces it — see [Testing](testing.md).

## The platform seam (2.2.0)

`crow_platform.py` is the one module that answers "where" and "how" per operating
system: XDG directories, install and models roots, the server binary name and
search order, `/proc`-based server discovery without `psutil`, spawn flags, kill
by process group, the shell, the browser candidates, the font store, the updater
argv. Standard library only, and **one-way** — it never imports the core.
`crow_core.py` calls it at 45 sites and no longer mentions `sys.platform` or
`os.name`. Where the paths it resolves land:
[Install](../user-guide/install.md) and [Linux](../user-guide/linux.md).

## Registries

Rebuilt in place by `mcp_apply()`, never rebound: `crow.py` does
`from crow_core import TOOLS`, which binds the value.

| | |
|---|---|
| `TOOLS` | the declarations sent in each request. Built-ins first, MCP above |
| `TOOL_IMPL` | name → callable. A name here that is not in `TOOLS` is unreachable |
| `TOOL_CLASS` | name → `reading` \| `writing` \| `executing`. Absent means `executing` |

---

<div align="center">
<img src="../images/architecture.png" alt="The modules, the registries and the paths between them" width="820">
</div>
