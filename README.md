<div align="center">

<img src="docs/images/og-dark.png" alt="Crow: the wordmark and composer over the window answering about its own release" width="880">

<h1>CROW</h1>

<h3>Qwen3.8-Flash-Next at 200k context on one 32 GiB GPU.</h3>

<p><b>An agent, not a chat box:</b> 22 tools plus MCP servers, persistent memory, its own skills, a browser panel, and eyes.<br>Runs on this machine, or on a provider you choose.</p>

<p>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square&logo=opensourceinitiative&logoColor=white&labelColor=000000" alt="License"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/version-2.1.0-brightgreen?style=flat-square&logo=semver&logoColor=white&labelColor=000000" alt="Version"></a>
<a href="#requirements"><img src="https://img.shields.io/badge/platform-Windows%20x64%20%C2%B7%20Linux%20x86__64%20%C2%B7%20CUDA-555555?style=flat-square&logo=nvidia&logoColor=76b900&labelColor=000000" alt="Platform"></a>
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
<td align="center"><b>30,984 MiB</b><br><sub>VRAM in use</sub></td>
<td align="center"><b>41.76</b><br><sub>tok/s decode</sub></td>
<td align="center"><b>727.65</b><br><sub>tok/s prefill</sub></td>
<td align="center"><b>yes</b><br><sub>vision</sub></td>
</tr>
</table>

<sub>Decode and prefill: 2026-09-01 (#182), driver 616.56, pin <code>6c84c7d5d</code>, one 33,494-token cold turn per boot, three rounds interleaved against the previous placement (539.98&nbsp;/&nbsp;35.74). Decode range 40.34–42.76. The two engine patches that followed, PR&nbsp;#28040 and PR&nbsp;#27880, were each measured at parity against this line. VRAM: settled after load, 2026-09-01.</sub>

</div>

---

## Contents

| | |
|---|---|
| [Operating point](#operating-point) | what this build runs at |
| [Second operating point](#second-operating-point) | Qwen3.8-27B, still shipped |
| [Requirements](#requirements) | card, disk, the two operating systems |
| [Install](#install) | one line |
| [Start](#start) | server, then a client |
| [Linux](#linux) | the same window on Wayland, and what differs |
| [Documentation](#documentation) | everything else |

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
| GPU | RTX 5090, 32,607 MiB. **30,984 MiB in use**, 1,059 MiB left |
| Decode | **41.76 tok/s** (40.34–42.76) |
| Prefill | **727.65 tok/s** |
| Build | llama.cpp pin `6c84c7d5d` (PR #27742) + PR #28040 + PR #27880, local. 62 graph splits per decoded token |
| License | `qwen-community-1.0` — not Apache-2.0 |
| Source of truth | [`manifests/operating-point.json`](manifests/operating-point.json) |

Conditions: 2026-09-01, driver 616.56, one 33,494-token cold turn per boot, 200 tokens out,
three rounds interleaved against the previous placement `-ncmoe 40 -ub 4096` (539.98 / 35.74);
wall clock per turn 67.9 s → 51.3 s. Accepted live at 41.8 tok/s. Decode falls with context
depth: `ms/token = 24.06 + 0.0706 per 1,000 tokens` (r² 0.93), i.e. ~41 tok/s at 30k and ~28 at
175k. Not measured: decode at a full 200k window, and whether an image prefill fits in the
1,059 MiB left on the card.

**The engine is a local build.** The packaged `b10269` cannot load `qwen4exp` at all. The pin
carries two patches, both measured at parity or better on this line: PR #28040 (the PLE n-gram
lookup in O(log n) instead of a scan over every used KV cell) and PR #27880 (the PLE embedding
hoisted into the token embedding's graph split: 62 splits per decoded token instead of 64,
−0.99 ms on the fixed term in the one clean pair, inside the 1.35 ms spread of one arm).

**The engine has nothing more to give, and that is measured (#159, #186).** 67.1 % of CPU
cycles per token are synchronization at the 62 CPU↔GPU handoffs; the RAM bus runs at 33.5 %.
Every lever inside llama.cpp is dead by a direct measurement: bandwidth, expert cache, thread
count, `OMP_WAIT_POLICY`, `-ncmoe` below 30, and the barrier implementation itself
(`GGML_OPENMP=OFF` costs +4 to +6 ms per token).

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

## Third operating point (Rust)

Crow runs on the crow-nest engine, the Rust engine built for this project. Since v0.2.0 (2026-09-14) its decode is faster than llama.cpp on the same machine, with identical greedy outputs; vision is served from the container itself, no projector file.

| | |
|---|---|
| Engine | crow-nest `v0.2.0` ([repo](https://github.com/nibor1896/crow-nest), [release](https://github.com/nibor1896/crow-nest/releases/tag/v0.2.0)), Windows, own HTTP server, OpenAI-compatible |
| Model | `CNQ4.5-M`, the project's own quant: one 104.7 GB NVFP4 container of `Qwen3.8-Flash-Next` ([package](https://huggingface.co/nibor1896/Qwen3.8-Flash-Next-CNQ4.5-M)) |
| Context | 200,000, one slot |
| Vision | yes, from the container's own `vit` section (no `--mmproj`, nothing extra to download) |
| Decode | **45.1 tok/s** (22.18 ms/token) vs llama.cpp 44.9 on the same prompt; greedy ids bit-identical |
| Prefill | 771 tok/s default, **871 tok/s** with `CROW_PF_GEMM_B=1`, vs llama.cpp 922.5 (16k reference prompt) |
| Quality | ten-task suite unchanged (2/5/3) against the llama.cpp operating point's reading |
| Port | 8099 |
| GPU | RTX 5090 class (Blackwell `sm_120` required), 64 GB host RAM class, CUDA driver + NVRTC |

Measured 2026-09-13/14 on one RTX 5090, F49 pair-chain methodology; sources: crow-nest issues #62 (decode) and #10 (prefill), release notes of v0.2.0.

Start (PowerShell, two windows; engine repo root):

```powershell
# engine (from the crow-nest repo root)
$env:CROW_PF_GEMM_B = "1"
engine/target_srv/release/serve.exe --port 8099 --slot-save-path decode_out/session

# Crow (the window; pick the engine above, http://127.0.0.1:8099/v1, in its model menu)
python cli/crow_gui.py
```

---
---

## Requirements

| | |
|---|---|
| **GPU** | NVIDIA. 32 GB for this operating point. 16 GB is the installer's floor, unmeasured |
| **System RAM** | 32 GB for the 27B. **64 GB for Flash-Next** -- `-ncmoe 30` keeps the experts of 30 of 48 layers in system RAM |
| **Disk** | ~2 GB for Crow, **73.45 GiB for the model** (3 shards) plus 0.9 GiB for the projector. The 27B is 16.35 GiB plus 0.9 |
| **OS** | Windows x64 · Linux x86_64 ([Arch/Omarchy tested](#linux)) |
| **Python** | 3.9+ (`str.removesuffix` in the core). Terminal client uses the standard library only |
| **WebView2** | Window only, Windows. Ships with Windows 11 and with Edge |
| **WebKitGTK** | Window only, Linux. `webkit2gtk-4.1` + `python-gobject` from the distribution -- neither installer asks for root |
| **wl-clipboard** | Linux only, and only for pasting an image into the window. `xclip` under X11 |
| **pywebview** | Window only, ~2 MB. Installed by `install.ps1` and by `install.sh` |
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
& "$env:LOCALAPPDATA\Crow\bin\llama-server.exe" -m "$env:LOCALAPPDATA\Crow\models\qwen-next-gguf\UD-Q2_K_XL\Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf" --port 8083 -c 200000 -b 2048 -ub 2048 -ctk q8_0 -ctv q8_0 -ncmoe 30 --fit off --load-mode none -np 1 --mmproj "$env:LOCALAPPDATA\Crow\models\qwen-next-gguf\mmproj-F16.gguf" --jinja
```

`--load-mode none` is what makes it reproducible: the expert weights are read once at boot
(about a minute) instead of being paged off the disk during the turn.

**This one needs a local engine.** `qwen4exp` exists in llama.cpp only from PR #27742; the
packaged `b10269` cannot load it. Build the pin `6c84c7d5d` and apply PR #28040 (one hunk by
hand) and PR #27880 (applies cleanly) for the line above. Do not build `b10687` or newer: it
aborts during CUDA warmup on this card, and the cause is not attributed.

The second operating point runs on the packaged engine:

```powershell
& "$env:LOCALAPPDATA\Crow\bin\llama-server.exe" -m "$env:LOCALAPPDATA\Crow\models\qwen38-gguf\Qwen3.8-27B-UD-Q4_K_XL.gguf" --mmproj "$env:LOCALAPPDATA\Crow\models\qwen38-gguf\mmproj-F16.gguf" --port 8082 -c 200000 -ctk q8_0 -ctv q8_0 -ngl 99 -np 1 --jinja --slot-save-path "$env:LOCALAPPDATA\Crow\session" --spec-type draft-mtp
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

## Linux

Same window, same core, same manifest, the same operating point. Ported and run on Arch
(Omarchy), Hyprland 0.56.2 on Wayland, RTX 5090, driver 610.57. Nothing here needs root.

| | |
|---|---|
| **Architecture** | x86_64. Arch/Omarchy is what it was ported on and what the numbers below were taken on |
| **Window** | WebKitGTK 4.1 through PyGObject, where Windows has WebView2 |
| **Compositor** | Wayland. The window is frameless and has a hard minimum size, so it asks to be floated -- see the rule below |
| **Clipboard** | `wl-clipboard`; `xclip` is the X11 fallback |
| **Install root** | `${XDG_DATA_HOME:-~/.local/share}/crow`, nothing outside `$HOME` |
| **Settings** | `~/.config/crow`. Sessions, boots and server logs `~/.local/state/crow` |
| **Models** | `<install>/models`, a link to the tree -- 73.45 GiB does not belong under `~/.local/share` |
| **Engine** | built here: llama.cpp pin `6c84c7d5d` + PR #27880 + PR #28040, CUDA 13.3, `sm_120` |
| **Full page** | [`docs/user-guide/linux.md`](docs/user-guide/linux.md) |

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/nibor1896/Crow/main/install.sh | bash
```

From a checkout it is the same script and the same five steps:

```bash
bash install.sh --models ~/Projects/models/qwen3.8-flash-next
```

Preflight, the files, a per-file sha256 manifest it re-reads on the next run, a
`--system-site-packages` venv with pywebview in it, `$CROW_HOME/bin/crow`, the `.desktop` entry,
the hicolor icons and the Hyprland rule. It is idempotent and it removes nothing it did not
install. The GTK and WebKit bindings come from the distribution, so the installer prints the
line instead of running it -- a script piped from the internet does not get a root password:

```bash
sudo pacman -S --needed python-gobject gtk3 webkit2gtk-4.1 wl-clipboard
```

### Model

```bash
hf download unsloth/Qwen3.8-Flash-Next-GGUF --include "*UD-Q2_K_XL*" --local-dir ~/Projects/models/qwen3.8-flash-next
hf download unsloth/Qwen3.8-Flash-Next-GGUF mmproj-F16.gguf --local-dir ~/Projects/models/qwen3.8-flash-next
```

The second line is the vision projector and the glob of the first walks past it -- the same
warning as above, for the same reason.

`install.sh --models DIR` makes `$CROW_HOME/models` a **link** to that tree, and `<install>/models`
is where the core looks when nothing is set -- so the window, the terminal client and
`tools/start-server.py` all find it, including a window started from the desktop entry that
inherits no shell profile. A checkout is its own `<install>`, so give it the same link (the
installer prints this line when it ran from one):

```bash
ln -s ~/Projects/models/qwen3.8-flash-next ~/Projects/crow/models
```

To point one shell at a different tree, and for the by-hand line below:

```bash
export CROW_MODELS=~/Projects/models/qwen3.8-flash-next
```

### Engine

```bash
bash tools/build-llama-server.sh
```

The packaged Windows binary is an `.exe` and there is no Linux release asset, so the engine is
built rather than downloaded -- from the same three commits the Windows operating point was
measured at: pin `6c84c7d5d` (PR #27742, `qwen4exp`) plus PR #27880 and PR #28040. It unpacks a
CUDA 13.3 toolkit and cmake/ninja under `$CROW_HOME`, compiles for `sm_120`, and refuses the
result unless `ldd` resolves `libcudart`/`libcublas` to that same prefix. About 20 minutes, no
root, `$CROW_HOME/bin/llama-server` at the end.

### Start

```bash
crow
```

That is the window, and it boots the server itself from the model menu -- the terminal client
is never required. By hand, the default operating point, the same line
[`manifests/operating-point.json`](manifests/operating-point.json) declares:

```bash
$HOME/.local/share/crow/bin/llama-server -m $CROW_MODELS/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8083 -c 200000 -b 2048 -ub 2048 -ctk q8_0 -ctv q8_0 -ncmoe 31 -t 24 --fit off --load-mode none -np 1 --mmproj $CROW_MODELS/mmproj-F16.gguf --jinja
```

`python3 ~/.local/share/crow/tools/start-server.py flash-next-q2-k-xl` builds that line from the
manifest instead of repeating it.

Two flags differ from the Windows line, both measured here on 2026-09-16 and recorded as the
line's `linux` object in the manifest: `-ncmoe 31`, because the Wayland desktop already holds
about 1 GiB of the card and the Windows placement died on its first request (`cublasCreate`,
resource allocation failed); and `-t 24`, because llama.cpp's Linux default picked 4 threads on
the 24-core Ultra 9 285K. Decode on two 200-token turns: 26.4 / 25.6 tok/s at the default
threads, 34.4 / 31.3 at `-t 8`, **36.7 / 36.2 at `-t 24`**. The Windows line's 41.76 was a cold
33k-token turn at `-ncmoe 30`; the two are not the same measurement.

### The window on Hyprland

Tiled, the window behaves like any Omarchy window: it takes its tile, fills the workspace on
its own, goes fullscreen with the compositor's key (accepted live, 2026-09-16). The float rule
is **optional**, for a half-width tile where the layout's 1,130 px minimum cuts the composer
off. `install.sh` copies it to `~/.config/hypr/crow.lua` (Lua config) or
`~/.config/hypr/crow.conf` (ini config) and prints the one line to add -- it does not edit
your config:

```lua
require("hypr.crow")
```

```bash
hyprctl reload
```

If the window comes up blank, or dies before it maps with `Gdk-Message: Error 71`, the page on
Wayland has two escape hatches and one of them is already on: `__NV_DISABLE_EXPLICIT_SYNC=1` is
set by `cli/crow_gui.py` at import. The other is yours:

```bash
CROW_GDK_BACKEND=x11 crow
```

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
| [Linux](docs/user-guide/linux.md) | install, paths, the window on Wayland, troubleshooting |
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
