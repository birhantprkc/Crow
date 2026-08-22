<div align="center">

<img src="docs/images/crow-qwen.png" alt="Crow" width="640">

<h1>CROW</h1>

<h3>Qwen3.8-27B at 200k context on one GPU.</h3>

<p><b>An agent, not a chat box:</b> 12 tools, persistent memory, its own skills.</p>

<p>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square&logo=opensourceinitiative&logoColor=white&labelColor=000000" alt="License"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/version-1.0.0-brightgreen?style=flat-square&logo=semver&logoColor=white&labelColor=000000" alt="Version"></a>
<a href="#requirements"><img src="https://img.shields.io/badge/platform-Windows%20x64%20%C2%B7%20CUDA-555555?style=flat-square&logo=nvidia&logoColor=76b900&labelColor=000000" alt="Platform"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/client-Python%20stdlib%20only-555555?style=flat-square&logo=python&logoColor=ffd43b&labelColor=000000" alt="Python"></a>
<a href="https://huggingface.co/unsloth/Qwen3.8-27B-GGUF"><img src="https://img.shields.io/badge/model-Qwen3.8--27B-orange?style=flat-square&logo=huggingface&logoColor=ffd21e&labelColor=000000" alt="Model"></a>
<a href="https://github.com/ggml-org/llama.cpp"><img src="https://img.shields.io/badge/engine-llama.cpp-555555?style=flat-square&logo=cplusplus&logoColor=00599c&labelColor=000000" alt="llama.cpp"></a>
<a href="#memory"><img src="https://img.shields.io/badge/memory-persistent-555555?style=flat-square&logo=sqlite&logoColor=003b57&labelColor=000000" alt="Memory"></a>
</p>

<table>
<tr>
<td align="center"><b>27B</b><br><sub>dense</sub></td>
<td align="center"><b>200k</b><br><sub>context, one slot</sub></td>
<td align="center"><b>16.35 GiB</b><br><sub>model on disk</sub></td>
<td align="center"><b>25.5 GiB</b><br><sub>VRAM in use</sub></td>
<td align="center"><b>123.05</b><br><sub>tok/s decode, 11-round turn</sub></td>
<td align="center"><b>133.18</b><br><sub>tok/s decode, warm turn</sub></td>
<td align="center"><b>2,262.96</b><br><sub>tok/s prefill</sub></td>
</tr>
</table>

</div>

---

## Contents

- [Operating point](#operating-point)
- [Requirements](#requirements)
- [Install](#install)
- [Start](#start)
- [Config](#config)
- [Memory](#memory)
- [Skills](#skills)
- [Session search](#session-search)
- [MCP servers](#mcp-servers)
- [Settings](#settings)
- [Measurements](#measurements)
- [Window](#window)
- [Repo](#repo)
- [Licence](#licence)

---

## Operating point

| | |
|---|---|
| Model | `Qwen3.8-27B-UD-Q4_K_XL.gguf`, 17,559,178,144 B |
| Architecture | dense, no `expert_count`; hybrid attention + SSM, `full_attention_interval 4` |
| Quant | `UD-Q4_K_XL`, Unsloth, imatrix 1,251 chunks |
| Context | `-c 200000`, one slot (`-np 1`) |
| KV | `q8_0` / `q8_0`, 6,647.00 MiB measured against 6,645.8 predicted |
| Speculation | `--spec-type draft-mtp`, head ships in the GGUF |
| GPU | RTX 5090, 32,607 MiB. 26,140 MiB in use |
| Build | llama.cpp server `1c3c967` |
| Source of truth | [`manifests/operating-point.json`](manifests/operating-point.json) |

---

## Requirements

| | |
|---|---|
| **GPU** | NVIDIA. 32 GB for this operating point. 16 GB is the installer's floor, unmeasured |
| **System RAM** | 32 GB |
| **Disk** | ~2 GB for Crow, **16.35 GiB for the model** — one file |
| **OS** | Windows x64 |
| **Python** | 3.8+. Terminal client uses the standard library only |
| **WebView2** | Window only. Ships with Windows 11 and with Edge |
| **pywebview** | Window only, ~2 MB. Installed by `install.ps1` |
| **Node** | Only for MCP servers started with `npx`. NOT installed by `install.ps1` |

---

## Install

```powershell
irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1 | iex
```

Preflight, download, extract, per-file sha256 against the release manifest, then the start lines
with paths resolved. No elevation. Everything under `%LOCALAPPDATA%\Crow`.

Model, separately:

```powershell
hf download unsloth/Qwen3.8-27B-GGUF --include "*UD-Q4_K_XL*" --local-dir $env:LOCALAPPDATA\Crow\models\qwen38-gguf
```

Check that one file of 17,559,178,144 B arrived. `hf` prints `✓ Downloaded` even when it could not
reach the repository.

---

## Start

### Server

```powershell
$env:LOCALAPPDATA\Crow\bin\llama-server.exe `
  -m $env:LOCALAPPDATA\Crow\models\qwen38-gguf\Qwen3.8-27B-UD-Q4_K_XL.gguf `
  --port 8082 -c 200000 -ctk q8_0 -ctv q8_0 -ngl 99 -np 1 --jinja `
  --slot-save-path $env:LOCALAPPDATA\Crow\session `
  --spec-type draft-mtp
```

### Clients

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow_gui.py
```

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow.py --base-url http://127.0.0.1:8082/v1
```

The window reads the `--port` off the running process. The terminal client needs `--base-url`.

---

## Config

### Server flags

| flag | value | why |
|---|---|---|
| `-c` | `200000` | a coding agent holds files and history. Rollover cuts at 0.9 of the window |
| `-np` | `1` | one user. `-np 4` splits the context four ways |
| `-ctk` / `-ctv` | `q8_0` | f16 left 332.8 MiB of headroom; q8_0 leaves 6,627 |
| `-ngl` | `99` | 16.35 GiB fits on the card whole |
| `--jinja` | on | without it llama.cpp uses its built-in template and the reasoning replay is dropped |
| `--slot-save-path` | `<install>\session` | the server refuses to start against a path that does not exist |
| `--spec-type` | `draft-mtp` | the model's own MTP head. 1.85x decode, measured |
| `--spec-draft-n-max` | `3` (default) | measured, see below |

### Client flags

| flag | default | |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8081/v1` | this model needs `:8082` |
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

### Reasoning levels

Per model, out of the manifest. Names that render the same prompt are one row in the window.

| rows offered | collapses |
|---|---|
| `high` (default), `low`, `medium` | `off` renders as `high` |

### Tools

12 built in, plus whatever [MCP servers](#mcp-servers) are configured.

`read_file` `write_file` `edit_file` `list_dir` `find_files` `search_text` `run_command`
`web_search` `fetch_url` `memory` `skill` `session_search`.

| release level | asks before |
|---|---|
| `auto` (default) | nothing |
| `allowedit` | executing |
| `manual` | writing and executing |

Reading never asks, at any level.

---

## Memory

Two files. Plain text, `§` on its own line between entries, editable by hand.

| path | limit | holds |
|---|---|---|
| `<working directory>\.crow\MEMORY.md` | 4,000 chars | this project: layout, conventions, commands, traps |
| `%LOCALAPPDATA%\Crow\USER.md` | 1,500 chars | who you are, how you want to be worked with |

| | |
|---|---|
| Limits come from | `MAX_TOOL_BYTES` — 16,000 B is ~4,000 tokens, so 4 chars buy 1 token |
| 4,000 chars is | a quarter of one tool read. Bigger than that and `read_file` is cheaper |
| Head cost, both stores plus one skill | 633 chars = 158 tokens = **0.09 %** of the usable window |
| Empty stores cost | nothing. No entries, no block, byte 0 unchanged |

### Rules

| | |
|---|---|
| Never trimmed for you | a write over the limit fails and returns the entries and both numbers |
| No `read` action | the content is already in the prompt |
| Exact duplicates | answered with success and one entry |
| Injection and invisible Unicode | refused before the entry is written |
| No working directory bound | `memory` is refused with a reason; `user` still works |

### The head is pinned

The rendered block is written into the chat file on first open and replayed **verbatim** from then
on. `prefix_fingerprint` hashes the system prompt, llama-server reuses a prompt by common token
prefix, and the KV cache lives on disk — so a head re-read at every start would go stale against
every saved cache. Binding a different folder re-pins and says what the prefill costs first.

### Who writes it

| | |
|---|---|
| Trigger | `MEMORY_REVIEW_AT` = **0.20 / 0.50 / 0.75** of the context window |
| Each mark fires | once. The mark is written to the chat file and travels with it |
| A turn crossing several marks | fires once, at the highest |
| Off with | `--no-review` |
| Before it writes | **it asks.** The proposed entries wait on a chip in the composer; nothing reaches the file until you press it |
| Nobody answers | they expire after **300 s** and are dropped. Nothing is ever written by a timer |
| Ask nothing, write always | `--no-memory-approval` |
| When it saves | one line in the chat, per entry, at the moment it lands |

### The gate

The review never writes on its own. What it wants to keep is staged and shown behind the composer,
and it stays there until you answer.

<div align="center">
<img src="docs/images/memory-consolidation.png" alt="Memory Consolidation: the staged writes behind the composer, +2 gained and -0 lost" width="900">
</div>

| | |
|---|---|
| Collapsed | the title, lines **gained** in green and **lost** in red. A `replace` is one entry and both |
| Click | opens every proposed entry in full, and the two answers |
| `save to memory` | writes through the same `memory` tool the model uses — the cap, the duplicate check and the injection scan all still answer |
| `discard` | nothing is written |
| No answer | the entries expire after 300 s and are dropped. **Nothing is ever written by a timer** |
| New chat | the questions go with it |
| Off | `--no-memory-approval`, and then the review writes unasked as it did before 1.0.0 |

It keeps breathing while it waits, because a question is still true until it is answered. The line
that reports a **finished** write glows once and settles — same colour, different grammar.

---

## Skills

Procedures the model keeps. Memory is what is **true**; a skill is what to **do**.

```
%LOCALAPPDATA%\Crow\skills\<name>\SKILL.md
---
name: llama-server-starten
description: Wenn Crow ein lokales LLM braucht (Port 8082) — exakte Flags, Wartesignal, Bind-Falle.
enabled: true
---
1. …
```

| | |
|---|---|
| In the prompt | name and description only, never the body |
| Body fetched with | `skill(action=read, name=…)`, one call |
| List limit | 2,000 chars for the **whole list**, 200 per description |
| Over the limit | the list says how many did not fit; it does not grow |
| `enabled` | in the file's own frontmatter. Absent means on |
| Written by | the same review at 0.20 / 0.50 / 0.75 — one pass decides both |

### Creating one

Crow ships with `skill-creator` and reads it before it writes. Seeded once, on the first run that
has no skills directory; deleted, it stays deleted.

```
Lies zuerst deinen Skill "skill-creator" und halte dich daran.
Speichere danach als Skill, wie man <Verfahren> ausführt: <Schritte, Flags wörtlich, die Falle>.
Nenne mir zum Schluss Name und Beschreibung, die du gespeichert hast.
```

| what `skill-creator` enforces | |
|---|---|
| Save only what worked **here** | not a plan, not general knowledge |
| The description says **when** | it is all the prompt carries; a description of itself is never chosen |
| Name the job, not the topic | `messreihe-fahren`, not `messungen` |
| Body | numbered steps, flags verbatim, what each step produces, the one trap that was hit |
| Rewrite under the same name | `save` replaces and keeps the on/off switch |
| Saying nothing | the normal outcome |

---

## Session search

```
session_search(query, limit=8)
```

| | |
|---|---|
| Covers | the open chat and everything under `session\archiv\` |
| Index | `%LOCALAPPDATA%\Crow\index.db`, SQLite FTS5 |
| The index is | derived. Delete it and the next search rebuilds it |
| Freshness | file mtime. A changed file loses all its rows and gets new ones |
| Returns | the real messages, clipped at 400 chars each. No summary |
| Query syntax | every word is quoted, so `--slot-save-path` is a search and not an error |
| Without FTS5 | the tool stays declared and answers that nothing was searched |

---

## MCP servers

```
/mcp add npx -y @modelcontextprotocol/server-filesystem C:\dev\Crow
/mcp add node C:\dev\pontifex\dist\index.js
/mcp add uvx mcp-server-fetch
```

The name comes out of the line: `filesystem`, `pontifex`, `fetch`.

| | |
|---|---|
| Config | `%LOCALAPPDATA%\Crow\mcp.json`, one block per server |
| Transport | stdio. `url`, HTTP and OAuth are not built |
| Schema | asked **once**, when the server is added, then written to disk |
| `TOOLS` at start | read from that file, never from a server |
| Connection | opened when a tool is first called, then kept until the config changes |
| Tool names | `mcp_<server>_<tool>` |
| Adding takes | every tool the server offers |
| Classes | empty until you set them. An unclassified tool is `executing` |
| Launcher | resolved through `PATH` + `PATHEXT` before it starts. `npx` is `npx.CMD` on Windows and `CreateProcess` does not look for it |
| Environment | a fixed base set plus the block's `env`, never the whole shell |
| Invisible U+E0000–U+E007F | stripped from names, descriptions, schemas and results. Emoji flags survive |

### Commands

| | |
|---|---|
| `/mcp` | what is configured, and its cost |
| `/mcp add <command line>` | add a server, take what it offers |
| `/mcp fetch <server>` | ask it again, keeping what was ticked |
| `/mcp use <server> <tool> <class>` | `reading`, `writing` or `executing` |
| `/mcp drop <server> <tool>` | take it out of the tool list |

Removing a server: `Help → Settings → MCPs`.

### The file

```json
{"servers": {"filesystem": {
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/dev/Crow"],
  "env": {},
  "enabled": true,
  "timeout": 60,
  "connect_timeout": 20,
  "tools": {"include": ["read_file"], "exclude": []},
  "schema": {"tools": [{"name": "read_file", "description": "...",
                        "inputSchema": {}, "annotations": {}}]},
  "classes": {"read_file": "reading"}
}}}
```

| key | |
|---|---|
| `schema` | what the server answered. Untrusted, per the specification |
| `classes` | what you confirmed. This is the half a release level acts on |
| `include` | positive list, and it wins over `exclude` |
| `enabled: false` | skipped. No connection attempted |
| `timeout` · `connect_timeout` | seconds. Defaults 60 and 20 |

The classification is pre-filled from `annotations`: `readOnlyHint: true` → `reading`,
`destructiveHint: false` → `writing`, anything else → `executing`. The specification's own defaults
are `readOnlyHint: false` and `destructiveHint: true`, so a server that says nothing gets the
strictest of the three.

### Cost

| | tools | chars in every prompt |
|---|---|---|
| built-in | 12 | 7,758 |
| `@modelcontextprotocol/server-filesystem` | 14 | 8,217 |
| `node pontifex/dist/index.js` | 14 | 7,131 |
| `mcp-server-fetch` | 1 | 1,137 |

Measured 2026-08-22. The tool list is rendered into the head of the prompt, so changing it moves
byte 0: the next turn and the first turn of every saved session pay a full prefill.

### Not built

| | |
|---|---|
| `sampling` | `capabilities` goes out empty. A server that asks for inference anyway gets `-32601` naming the missing capability |
| `elicitation` | not declared, same answer |
| `notifications/tools/list_changed` | ignored. A tool list that changed mid-chat would move byte 0 |
| HTTP · OAuth | stdio only |
| No catalog | no curated list of servers. You enter the command line |

---

## Settings

`Help → Settings` in the window.

| pane | |
|---|---|
| **Appearance** | theme: dark, light, crow |
| **Skills** | one row per skill, name and description, a switch. Off takes it out of the prompt; the file stays. Switching re-pins the open chat and says what the prefill costs |
| **Server** | connection state, the base URL as its title, and the tool-call switch |
| **MCPs** | one row per server, folded; per tool a switch and its class. Add with a command line, `ask again`, `remove`. See [MCP servers](#mcp-servers) |
| **Other providers** | coming soon — keys for models that are not on this machine |
| **About** | version |

Chat rail: right-click a chat to rename, move to a project, archive or delete; right-click the empty
space for a new chat or a new project. A project **is** a working directory — a chat belongs to one
when its `crow_root` points there, and nothing else records it.

---

## Measurements

One user, `-np 1`, identical prompt, server restarted cold per arm, cross-checked against the
server's own `eval time` blocks.

### Speculation

| prompt | without MTP | with MTP | factor |
|---|---|---|---|
| tool-heavy, 11 rounds | 66.51 tok/s | **123.05 tok/s** | 1.85 |
| warm follow-up, 1 round | 64.50 tok/s | **133.18 tok/s** | 2.07 |
| wall clock, tool-heavy | 2m07s | **1m22s** | 1.55 |

| mechanism | without MTP | with MTP |
|---|---|---|
| main-model passes/s | 65 | 41 |
| accepted tokens per pass | 1.00 | 2.98 |
| draft acceptance | n/a | 4,379 / 6,630 = 66 % |
| per-round acceptance | n/a | 52 % to 100 % |

### `--spec-draft-n-max`

| n_max | tokens | tok/s | acceptance | mean len | passes/s |
|---|---|---|---|---|---|
| 1 | 3,425 | 96.71 | 77.1 % | 1.77 | 54.6 |
| 2 | 2,344 | 105.17 | 59.4 % | 2.19 | 48.0 |
| **3** (default) | 7,402 | **121.76** | 66.1 % | 2.98 | 40.9 |
| 4 | 4,341 | 115.73 | 53.0 % | 3.12 | 37.1 |
| 6 | 3,727 | 119.79 | 46.6 % | 3.80 | 31.5 |
| 8 | 2,976 | 111.68 | 33.7 % | 3.69 | 30.3 |

One run per value. Output length varied 2,344 to 7,402 tokens; the gap between 3, 4 and 6 is not
separable.

### Context

| context | decode, no MTP |
|---|---|
| 1,653 tokens | 74.09 tok/s |
| 35,984 tokens | 64.50 tok/s |

### Prefill

| block | tok/s |
|---|---|
| 34 tokens | 209.71 |
| 890 | 2,091.73 |
| 4,339 | 3,298.49 |

Prefill is a function of block size, not a constant.

### Verification

| check | result |
|---|---|
| tokens, client vs server | 6,591 = sum of 11 `eval time` blocks |
| decode, client vs server | 6,591 / 53.564 s = 123.05 tok/s |
| prefill, client vs server | 20,490 = sum of 11 `prompt eval` lines |
| suite | 925 of 925 |
| `check_shared_core` | 60 / 60 |
| `check_operating_point` | 6 / 6 |
| `install.ps1 -Selftest` | 80 / 80 |

### Not measured

| open | |
|---|---|
| VRAM floor | 16 GB is the installer's floor, never run |
| contexts past 36k under MTP | without MTP that span costs 13 % |
| distribution fidelity at `temperature 1.0` | one graded answer is a sample |
| what the background review costs | it holds the single slot; occupancy and queueing never timed |

---

## Window

<div align="center">
<img src="docs/images/window.png" alt="Crow window: chat rail, the wireframe over an empty chat, and the composer" width="920">
</div>

| | |
|---|---|
| Composer | model and reasoning level as one chip, context readout, working directory, release level, dictation |
| Cost line | rounds, tokens, decode, prefill, cache hits, tool calls, wall clock |
| Thought blocks | folded, one per re-entry, each labelled with the turn's thinking share |
| Rail | chats grouped by project, archive, fold state remembered |

---

## Repo

| path | |
|---|---|
| `cli/crow.py` | terminal client |
| `cli/crow_gui.py` | window |
| `cli/crow_core.py` | conversation, request, SSE, tool loop, memory, skills, cost line |
| `tools/start-server.py` | model picker, becomes `llama-server` |
| `manifests/operating-point.json` | source of truth for every command line above |
| `tools/check_operating_point.py` | holds this file against that manifest |
| `docs/second-model.md` | the other server `install.ps1` sets up |

---

## Licence

MIT. See [LICENSE](LICENSE).

Model: [Qwen](https://huggingface.co/Qwen/Qwen3.8-27B) (Apache-2.0). Quantisation by
[Unsloth](https://huggingface.co/unsloth). Engine:
[llama.cpp](https://github.com/ggml-org/llama.cpp).

Earlier READMEs: [v0.5.1, Qwen-first](docs/README-v0.5.1-qwen.md) ·
[v0.5.1, the one before it](docs/README-v0.5.1-deepseek.md).

<div align="center">
<a href="https://ko-fi.com/nibor1896"><img src="https://img.shields.io/badge/support%20this%20on-ko--fi-ff5e5b?style=for-the-badge" alt="Ko-fi"></a>
</div>
