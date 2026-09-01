<div align="center">

<img src="docs/images/og-dark.png" alt="Crow: the wordmark and composer over the window answering about its own release" width="880">

<h1>CROW</h1>

<h3>Qwen3.8-Flash-Next at 200k context on one 32 GiB GPU.</h3>

<p><b>An agent, not a chat box:</b> 22 tools plus MCP servers, persistent memory, its own skills, a browser panel, and eyes.<br>Runs on this machine, or on a provider you choose.</p>

<p>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square&logo=opensourceinitiative&logoColor=white&labelColor=000000" alt="License"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/version-2.0.0-brightgreen?style=flat-square&logo=semver&logoColor=white&labelColor=000000" alt="Version"></a>
<a href="#requirements"><img src="https://img.shields.io/badge/platform-Windows%20x64%20%C2%B7%20CUDA-555555?style=flat-square&logo=nvidia&logoColor=76b900&labelColor=000000" alt="Platform"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/client-Python%20stdlib%20only-555555?style=flat-square&logo=python&logoColor=ffd43b&labelColor=000000" alt="Python"></a>
<a href="https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF"><img src="https://img.shields.io/badge/model-Qwen3.8--Flash--Next-orange?style=flat-square&logo=huggingface&logoColor=ffd21e&labelColor=000000" alt="Model"></a>
<a href="#second-operating-point"><img src="https://img.shields.io/badge/second-Qwen3.8--27B-orange?style=flat-square&logo=huggingface&logoColor=ffd21e&labelColor=000000" alt="Second model"></a>
<a href="docs/reference/tools.md"><img src="https://img.shields.io/badge/vision-read__image-9b59d0?style=flat-square&logo=image&logoColor=white&labelColor=000000" alt="Vision"></a>
<a href="docs/user-guide/window.md"><img src="https://img.shields.io/badge/browser-built--in-9b59d0?style=flat-square&logo=googlechrome&logoColor=white&labelColor=000000" alt="Browser"></a>
<a href="https://github.com/ggml-org/llama.cpp"><img src="https://img.shields.io/badge/engine-llama.cpp-555555?style=flat-square&logo=cplusplus&logoColor=00599c&labelColor=000000" alt="llama.cpp"></a>
<a href="#memory"><img src="https://img.shields.io/badge/memory-persistent-555555?style=flat-square&logo=sqlite&logoColor=003b57&labelColor=000000" alt="Memory"></a>
</p>

<table>
<tr>
<td align="center"><b>MoE</b><br><sub>512 experts, 10 active</sub></td>
<td align="center"><b>200k</b><br><sub>context, one slot</sub></td>
<td align="center"><b>73.45 GiB</b><br><sub>model on disk</sub></td>
<td align="center"><b>27,707 MiB</b><br><sub>VRAM in use</sub></td>
<td align="center"><b>32.44</b><br><sub>tok/s decode</sub></td>
<td align="center"><b>970.44</b><br><sub>tok/s prefill</sub></td>
<td align="center"><b>yes</b><br><sub>vision</sub></td>
</tr>
</table>

<sub>Decode and prefill: 2026-08-30, driver 616.56, pin <code>6c84c7d5d</code> + PR&nbsp;#27992, one 31,979-token cold turn per boot, three rounds interleaved against a same-session control (964.92&nbsp;/&nbsp;29.05). Ranges 941.07–985.32 and 31.06–33.20.</sub>

</div>

---

## Contents

| | |
|---|---|
| [Operating point](#operating-point) | what this build runs at |
| [Second operating point](#second-operating-point) | Qwen3.8-27B, still shipped |
| [Requirements](#requirements) | card, disk, Windows |
| [Install](#install) | one line |
| [Start](#start) | server, then a client |
| [Documentation](#documentation) | everything else |
| [Screenshots](#screenshots) | the window |

## Operating point

Default since 2.0.0. `DEFAULT_BASE_URL` is `http://127.0.0.1:8083/v1`.

| | |
|---|---|
| Model | `Qwen3.8-Flash-Next-UD-Q2_K_XL`, 3 shards, 73.45 GiB |
| Architecture | `qwen4exp` MoE: 48 layers, 512 experts per layer, 10 active, full attention every 4th |
| Quant | `UD-Q2_K_XL`, Unsloth |
| Context | `-c 200000`, one slot (`-np 1`) |
| Placement | `-ncmoe 30 --fit off --load-mode none`, `-b 2048 -ub 2048` |
| KV | `q8_0` / `q8_0` |
| Vision | `--mmproj mmproj-F16.gguf`, 904,004,000 B (#170) |
| Reasoning | `low` `medium` `high`; `max`, `minimal` and an explicit `off` return HTTP 500 (#160) |
| Thinking cap | `reasoning_budget` 1024 per request, from the manifest (#176) |
| GPU | RTX 5090, 32,607 MiB. **27,707 MiB in use** |
| Decode | **32.44 tok/s** (31.06–33.20) |
| Prefill | **970.44 tok/s** (941.07–985.32) |
| Build | llama.cpp pin `6c84c7d5d` (PR #27742) + PR #27992, local |
| License | `qwen-community-1.0` — not Apache-2.0 |
| Source of truth | [`manifests/operating-point.json`](manifests/operating-point.json) |

Conditions: 2026-08-30, driver 616.56, one 31,979-token cold turn per boot, three rounds
interleaved against a same-session control (964.92 / 29.05). Not measured: decode at a full
200k window, and the projector's VRAM cost on this line.

**The engine is a local build.** The packaged `b10269` cannot load `qwen4exp` at all. Fall back
to the bare pin and the numbers are 959.81 / 28.60 over ten boots.

---

## Second operating point

Qwen3.8-27B is still shipped, still measured, and still bootable from the model menu — it is the
faster one per token and the smaller download.

| | |
|---|---|
| Model | `Qwen3.8-27B-UD-Q4_K_XL.gguf`, 17,559,178,144 B |
| Architecture | dense, no `expert_count`; hybrid attention + SSM, `full_attention_interval 4` |
| Quant | `UD-Q4_K_XL`, Unsloth, imatrix 1,251 chunks |
| Context | `-c 200000`, one slot (`-np 1`) |
| KV | `q8_0` / `q8_0`, 6,647.00 MiB measured against 6,645.8 predicted |
| Vision | `--mmproj mmproj-F16.gguf`, 927,607,488 B; +1,124 MiB VRAM, text prefill unchanged |
| Speculation | `--spec-type draft-mtp`, head ships in the GGUF |
| GPU | RTX 5090, 32,607 MiB. 26,140 MiB in use |
| Decode | 123.05 tok/s (11-round turn) · 133.18 (warm turn) |
| Prefill | 2,262.96 tok/s |
| Port | 8082 |
| Build | llama.cpp server `1c3c967` — the packaged engine runs it |
| License | Apache-2.0 |

---

## Requirements

| | |
|---|---|
| **GPU** | NVIDIA. 32 GB for this operating point. 16 GB is the installer's floor, unmeasured |
| **System RAM** | 32 GB for the 27B. **64 GB for Flash-Next** -- `-ncmoe 30` keeps the experts of 30 of 48 layers in system RAM |
| **Disk** | ~2 GB for Crow, **73.45 GiB for the model** (3 shards) plus 0.9 GiB for the projector. The 27B is 16.35 GiB plus 0.9 |
| **OS** | Windows x64 |
| **Python** | 3.8+. Terminal client uses the standard library only |
| **WebView2** | Window only. Ships with Windows 11 and with Edge |
| **pywebview** | Window only, ~2 MB. Installed by `install.ps1` |
| **Node** | Only for MCP servers started with `npx` or `node`. Reported by the preflight, never required. NOT installed by `install.ps1` |

---

## Install

```powershell
irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1 | iex
```

Preflight, download, extract, per-file sha256 against the release manifest, then the start lines
with paths resolved. No elevation. Everything under `%LOCALAPPDATA%\Crow`.

Model, separately. Flash-Next, the default:

```powershell
hf download unsloth/Qwen3.8-Flash-Next-GGUF --include "*UD-Q2_K_XL*" --local-dir $env:LOCALAPPDATA\Crow\models\qwen-next-gguf
hf download unsloth/Qwen3.8-Flash-Next-GGUF mmproj-F16.gguf --local-dir $env:LOCALAPPDATA\Crow\models\qwen-next-gguf
```

Three shards of 73.45 GiB total, plus 904,004,000 B for the projector.

The 27B, the second operating point:

```powershell
hf download unsloth/Qwen3.8-27B-GGUF --include "*UD-Q4_K_XL*" --local-dir $env:LOCALAPPDATA\Crow\models\qwen38-gguf
hf download unsloth/Qwen3.8-27B-GGUF mmproj-F16.gguf --local-dir $env:LOCALAPPDATA\Crow\models\qwen38-gguf
```

One file of 17,559,178,144 B and one of 927,607,488 B.

**The second line of each pair is the vision projector, and the glob of the first does not catch
it** — it sits in the repository ROOT, above the quant folder. Without it the server starts as a
text model and `read_image` refuses with a sentence. `hf` prints `✓ Downloaded` even when it could
not reach the repository, so check the byte counts.

---

## Start

### Server

Crow boots it for you, from the manifest, with the log and the process group it needs:

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow.py --serve flash-next-q2-k-xl
```

Without a name it lists what is bootable. The window does the same from the model menu, and
records the boot so a later window can revive the server.

By hand, the default operating point:

```powershell
C:\path\to\your\llama-server.exe `
  -m $env:LOCALAPPDATA\Crow\models\qwen-next-gguf\UD-Q2_K_XL\Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf `
  --port 8083 -c 200000 -b 2048 -ub 2048 -ctk q8_0 -ctv q8_0 `
  -ncmoe 30 --fit off --load-mode none -np 1 `
  --mmproj $env:LOCALAPPDATA\Crow\models\qwen-next-gguf\mmproj-F16.gguf --jinja
```

`--load-mode none` is what makes it reproducible: the expert weights are read once at boot
(about a minute) instead of being paged off the disk during the turn.

**This one needs a local engine.** `qwen4exp` exists in llama.cpp only from PR #27742; the
packaged `b10269` cannot load it. Build the pin `6c84c7d5d` and apply PR #27992 for the decode
numbers above.

The second operating point runs on the packaged engine:

```powershell
$env:LOCALAPPDATA\Crow\bin\llama-server.exe `
  -m $env:LOCALAPPDATA\Crow\models\qwen38-gguf\Qwen3.8-27B-UD-Q4_K_XL.gguf `
  --mmproj $env:LOCALAPPDATA\Crow\models\qwen38-gguf\mmproj-F16.gguf `
  --port 8082 -c 200000 -ctk q8_0 -ctv q8_0 -ngl 99 -np 1 --jinja `
  --slot-save-path $env:LOCALAPPDATA\Crow\session `
  --spec-type draft-mtp
```

### Clients

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow_gui.py
```

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow.py
```

The window reads the `--port` off the running process. The terminal client defaults to
`http://127.0.0.1:8083/v1` since 2.0.0; `--base-url http://127.0.0.1:8082/v1` points it at the
second operating point.

---

## Documentation

Configuration, features and measurements live under [`docs/`](docs/).

| | |
|---|---|
| [Server flags](docs/reference/server-flags.md) | what `llama-server` is started with |
| [Client flags](docs/reference/client-flags.md) | what `crow` and the window take |
| [Reasoning levels](docs/reference/reasoning-levels.md) | `low`, `medium`, `high`, `off` |
| [Tools](docs/reference/tools.md) | the twenty-two built in -- git, `read_image`, `render_page` among them |
| [Settings](docs/reference/settings.md) | `settings.json` |
| [mcp.json](docs/reference/mcp-json.md) | every key, both transports |
| [Memory](docs/user-guide/memory.md) | what is written, by whom, and the gate |
| [Skills](docs/user-guide/skills.md) | using and writing one |
| [Session search](docs/user-guide/session-search.md) | the index |
| [MCP servers](docs/user-guide/mcp.md) | stdio, elicitation, commands |
| [MCP over HTTP](docs/user-guide/mcp-http.md) | headers, OAuth |
| [Remote models](docs/user-guide/remote-models.md) | subscriptions, dialects, routing |
| [Window](docs/user-guide/window.md) | the GUI |
| [Measurements](docs/measurements/README.md) | every number with its conditions |
| [Other models](docs/second-model.md) | DeepSeek 0731 and Qwen3.8-27B, each with its measured line |
| [Browser panel](docs/user-guide/browser.md) | tabs, the address bar, and what `render_page` does |
| [Architecture](docs/developer-guide/architecture.md) | the four modules and the core/surface split |
| [Testing](docs/developer-guide/testing.md) | three suites, five checkers, the manifest |
| [Repo](docs/developer-guide/repo.md) | layout |
| [Not built](docs/developer-guide/not-built.md) | decided against, and why |

## Licence

MIT. See [LICENSE](LICENSE).

Model: [Qwen](https://huggingface.co/Qwen/Qwen3.8-27B) (Apache-2.0). Quantisation by
[Unsloth](https://huggingface.co/unsloth). Engine:
[llama.cpp](https://github.com/ggml-org/llama.cpp). The optional third model,
Qwen3.8-Flash-Next, is licensed qwen-community-1.0 — read it before redistributing;
Crow does not ship the weights.

Earlier READMEs: [v0.5.1, Qwen-first](docs/README-v0.5.1-qwen.md) ·
[v0.5.1, the one before it](docs/README-v0.5.1-deepseek.md).

<div align="center">
<a href="https://ko-fi.com/nibor1896"><img src="https://img.shields.io/badge/support%20this%20on-ko--fi-ff5e5b?style=for-the-badge" alt="Ko-fi"></a>
</div>


---

## Screenshots

Taken on the shipped build. Windows, `Qwen3.8-27B` on the local llama-server unless a shot says
otherwise.

<div align="center">
<img src="docs/images/Crow.png" alt="Crow's empty chat: the rail on the left, the wireframe raven, the composer" width="920">
</div>

An empty chat. The rail groups chats by working directory, the composer carries the model, the
release level, the working directory and dictation.

<div align="center">
<img src="docs/images/CrowToolCallsAndTraceInChat.png" alt="A turn with the trace open and the tool-call panel listing what ran" width="920">
</div>

A turn with its trace open and the code panel on the right. Calls fold one at a time into their
arguments and their result; the source a turn writes stands under them, under its own path.

<div align="center">
<img src="docs/images/CrowVoiceInput.png" alt="The composer while dictating, with the voice line in the input row" width="920">
</div>

Dictation. The voice line sits in the input row and costs no height; the level scale calibrates
itself against a running peak.

### Settings

<div align="center">
<img src="docs/images/CrowModelLocal.png" alt="The Model pane with This machine selected" width="920">
</div>

Where a turn goes. `This machine` is the llama-server on this box: warm slot, no bill.

<div align="center">
<img src="docs/images/CrowModelOpen.png" alt="The Model pane with OpenRouter selected and its catalogue open" width="920">
</div>

The same pane on OpenRouter, catalogue open. The count beside it is what the provider last
reported, read from disk rather than fetched per open.

<div align="center">
<img src="docs/images/CrowMCP.png" alt="The MCPs pane with a server folded open and a class per tool" width="920">
</div>

MCP servers. Per tool a switch and a class -- reading, writing, executing -- pre-filled from the
server's annotations and decided by the user.

<div align="center">
<img src="docs/images/CrowSkills.png" alt="The Skills pane, one row per skill with a switch" width="920">
</div>

Skills. One row per skill; off takes it out of the prompt and leaves the file where it is.

<div align="center">
<img src="docs/images/CrowAPI.png" alt="The API Keys pane, one key per provider, shown as a mask" width="920">
</div>

API keys. One key per provider, kept in its own file that no view reads back -- what a box shows
afterwards is a mask.

### Themes

<div align="center">
<img src="docs/images/Skin13.png" alt="The dark theme" width="920">
</div>

Dark.

<div align="center">
<img src="docs/images/Skin23.png" alt="The light theme" width="920">
</div>

Light.

<div align="center">
<img src="docs/images/Skin33.png" alt="The crow theme" width="920">
</div>

Crow.
