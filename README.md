<div align="center">

<h1>Crow</h1>

<h3>A 304-billion-parameter coding model, at a 200k context. One graphics card. 64 GB of system RAM.</h3>

<p>Frontier mixture-of-experts inference, with the experts streamed off the SSD.<br>No cluster. No 200 GB host. No cloud.</p>

<p>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square&logo=opensourceinitiative&logoColor=white&labelColor=000000" alt="License"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/version-0.1.0-brightgreen?style=flat-square&logo=semver&logoColor=white&labelColor=000000" alt="Version"></a>
<a href="#requirements"><img src="https://img.shields.io/badge/platform-Windows%20x64%20%C2%B7%20CUDA-555555?style=flat-square&logo=nvidia&logoColor=76b900&labelColor=000000" alt="Platform"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/client-Python%20stdlib%20only-555555?style=flat-square&logo=python&logoColor=ffd43b&labelColor=000000" alt="Python"></a>
<a href="https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731"><img src="https://img.shields.io/badge/model-DeepSeek--V4--Flash--0731-orange?style=flat-square&logo=huggingface&logoColor=ffd21e&labelColor=000000" alt="Model"></a>
</p>

<table>
<tr>
<td align="center"><b>304B</b><br><sub>parameters</sub></td>
<td align="center"><b>13.3B</b><br><sub>active per token</sub></td>
<td align="center"><b>200k</b><br><sub>context, one slot</sub></td>
<td align="center"><b>97.1 GiB</b><br><sub>model on disk</sub></td>
<td align="center"><b>33.73 GiB</b><br><sub>peak host RAM, preview model</sub></td>
<td align="center"><b>19.13</b><br><sub>tok/s decode, median of 3 arms</sub></td>
<td align="center"><b>0 EUR</b><br><sub>spent so far</sub></td>
</tr>
</table>

</div>

<br>

## What this is, in one paragraph

**Crow runs a frontier-scale coding model on a single consumer graphics card by leaving most of the model on the SSD.**

A mixture-of-experts model is mostly asleep. Every token wakes only **6 of the 256 experts** in each of its 43 layers, and the routed experts that can be asleep are 90.17 of the file's 97.05 GiB — 92.9 %. Crow keeps the parts that *every* token needs in VRAM — attention, norms, shared experts, 6.88 GiB of them on 0731 — holds the 64 most useful experts per layer beside them in a slot cache, and reads whatever is missing straight off the drive while the GPU is still working. The host machine never holds the model: the shipped file is **97.1 GiB** and the server's process memory peaked at **33.73 GiB**, of which 32 are a cache it does not need — without it the same binary peaked at 26.99 GiB. *(Both host-memory peaks were measured on the preview model at this context length and have not been re-run on 0731; the architecture is identical, the file is 1.1 GiB larger, and no 0731 host peak is claimed here.)*

The context window is 200,000 tokens, on a single slot, and it costs **1,353.50 MiB = 1.32 GiB** of the card — 32.25 + 1029.00 + 35.00 + 257.25 MiB of KV buffers at `n_ctx = 200192`, so 6.92 KiB per token, measured on 0731 (`runs/2026-08-10/0731-pairs/r1-l2`). Compressed attention makes context the cheap part here. A coding session holds files and history, so a 16k or 64k window would be measuring a product nobody uses.

**Since 0.0.5 the host's spare RAM can be spent to make that cheaper.** A machine with 64 GB has tens of gigabytes doing nothing. `--moe-stream-l2 32` keeps expert weights there between the VRAM slots and the drive, so a miss that finds its expert in host memory never reaches the drive at all. On 0731, over three paired runs on 2026-08-10, the measured cost of a miss falls from **1.280–1.320 ms without the tier to 0.717–0.741 ms with it**, and decode goes from 12.84 to 19.13 tok/s at the median arm. The flag defaults to off; the installer puts it into the command it prints on any machine that has the RAM — [what it buys and what it costs](#5-the-host-ram-tier-optional).

That is the whole idea. Everything below is what it costs to make it actually run.

---

![Where every byte lives, what crosses between VRAM and the drive, and what it costs per token](docs/images/architecture.svg)

[Part II](#part-ii-how-it-works) builds every box in that diagram from the measurements that produced it.

---

## Contents

**[Part I: Getting started](#part-i-getting-started)**
&nbsp;&nbsp;[Requirements](#requirements) · [Quick start](#quick-start) · [Full setup](#full-setup) · [Using the CLI](#using-the-cli) · [Updating](#updating) · [Common questions](#common-questions)

**[Part II: How it works](#part-ii-how-it-works)**
&nbsp;&nbsp;[The problem](#the-problem-a-model-that-does-not-fit) · [Sparsity](#1-sparsity-most-of-the-model-is-asleep) · [Quantisation](#2-quantisation-and-where-it-breaks) · [The cache](#3-the-slot-cache-and-what-vram-buys) · [Reading the drive](#4-reading-the-drive-without-the-page-cache) · [The host tier](#5-the-host-ram-tier-optional) · [Cost per token](#what-it-costs-per-token) · [Against CPU offload](#against-cpu-offload) · [Batching](#batching-and-why-the-cli-does-not) · [What is not claimed](#what-is-not-claimed)

**[What's next](#whats-next)** · **[How this project works](#how-this-project-works)** · **[Building it yourself](#building-it-yourself)** · **[Licence](#licence)** · **[Credits](#credits)**

---

# Part I: Getting started

## Requirements

| | |
|---|---|
| **GPU** | NVIDIA, **16 GB VRAM minimum**, 32 GB for the measured operating point. Below 16 GB was never measured and is unsupported |
| **System RAM** | **64 GB for the operating point**, which spends 32 GiB on the [host tier](#5-the-host-ram-tier-optional). 32 GB runs without it, at a 26.99 GiB peak (preview model) and **1.49x less throughput** measured on 0731 |
| **Disk** | ~2 GB for Crow, **97.1 GiB for the model** — 104,207,848,032 B across four files, measured on the finished download |
| **OS** | Windows x64. The streaming path uses `FILE_FLAG_NO_BUFFERING` and a handle pool, both Windows-specific |
| **Python** | 3.8+, for the client only. Standard library, nothing to install |

The installer looks at all of this **before** it downloads anything, but only two of the rows can stop it: fewer than 16,000 MB of VRAM, and less than 2 GB free on the install drive. System RAM, room for the model and a missing Python are **reported as warnings and the install continues** (`install.ps1:154-181`) — the table is the measured profile, not a gate.

## Quick start

```powershell
irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1 | iex
```

Five steps, no elevation, everything under `%LOCALAPPDATA%\Crow`:

```console
  Crow 0.1.0

[1/5] Checking this machine
      GPU  NVIDIA GeForce RTX 5090, 32607 MB
      RAM  63.4 GB
      Disk  364.3 GB free on C:
      Windows  64-bit, PowerShell 5.1.26100.8875
      preflight  passed

[2/5] Downloading the package
      crow-0.1.0-win-x64.zip  [####################.....]  84%  424.1 MB / 506.4 MB  18.2 MB/s

[3/5] Verifying
      size  506.4 MB
      sha256  C51BF9B8...

[4/5] Installing to C:\Users\you\AppData\Local\Crow
        26/26  README.md                                  64 KB
      26 files extracted

[5/5] What is left to do
```

The archive figures are `dist/crow-0.1.0-win-x64.zip` as built: **531,013,143 B zipped, 26 files
unpacked, SHA-256 `c51bf9b8…`**. The line for `README.md` is this file's current size, so a repack
moves it and nothing else on the screen.

The model is **not** part of that download. It is 97.1 GiB, it belongs to somebody else, and an installer that spends hours on it before you have seen anything work is the wrong shape. The last step prints the one command that fetches it:

```powershell
hf download unsloth/DeepSeek-V4-Flash-0731-GGUF --include "UD-IQ3_XXS/*" --local-dir $env:LOCALAPPDATA\Crow\models
```

> **A trap worth knowing about.** Measured here on 2026-08-07: when `hf` cannot reach the repository it prints `✓ Downloaded` and returns the local directory — failure that looks exactly like success. Check that four files totalling ~97 GiB actually arrived.

> **`%LOCALAPPDATA%` is a `cmd.exe` form and PowerShell does not expand it.** Every block on this page is PowerShell, so it writes `$env:LOCALAPPDATA`. The installer prints both paths already resolved (`install.ps1:1144` and `:1154`), which is the copy to prefer.

## Full setup

### Step 1 — start the server

```powershell
$env:LOCALAPPDATA\Crow\bin\llama-server.exe `
  -m $env:LOCALAPPDATA\Crow\models\UD-IQ3_XXS\DeepSeek-V4-Flash-0731-UD-IQ3_XXS-00001-of-00004.gguf `
  --port 8081 -c 200000 -ngl 99 -np 1 --jinja `
  --slot-save-path $env:LOCALAPPDATA\Crow\session `
  --chat-template-file $env:LOCALAPPDATA\Crow\templates\0731-chat-template.jinja `
  --moe-stream --moe-stream-cache 64s --moe-stream-io-threads 8 --moe-stream-direct `
  --moe-stream-l2 32
```

The installer prints this line filled in for your machine. It leaves `--moe-stream-l2` out below 60 GB of detected RAM — Windows reports 63.4 on a 64 GB machine, so a threshold at the nominal size would exclude exactly the configuration this was measured on.

Every flag carries a reason, and none of them is taste:

| Flag | Why |
|---|---|
| `-c 200000` | A coding session holds files and history. 16k or 64k measures a product nobody uses. 200k fits on one slot: measured 2026-08-11 on 0731, **32,038 MiB of 32,607 after load** — 978 MiB of that is the desktop, read before the server started. Under a filled context the only pair on record is the preview model's (31,899 / 31,997); 0731 has not been read there |
| `--port 8081` | Not a preference. `llama-server` defaults to 8080 and the client defaults to 8081, so leaving it out gives a server the client cannot find — and on Windows 8080 is often already taken |
| `-np 1` | One user, one stream. `-np 4` splits the context into 4 × 50k and is the harness case, not the CLI |
| `--jinja` | Use the **model's** chat template instead of llama.cpp's built-in one. Without it the client's replayed reasoning is dropped and the prompt cache breaks on every turn: measured 138.8–242.3 s of re-prefill per turn against 1.6–2.2 s |
| `--moe-stream` | Route expert tensors through the slot cache instead of placing them |
| `--moe-stream-cache 64s` | 64 of 256 experts per layer. On 0731 that is **23,084 MiB = 22.54 GiB**, printed at load (`runs/2026-08-10/0731-pairs/r1-l2/r1-l2.err:7301`). 121 slots would cover 95 % of expert *selections* and need 41.6 GiB, which does not fit — coverage is not hit rate, because a first touch can never be cached |
| `--moe-stream-io-threads 8` | I/O workers. **With `--moe-stream-direct` each of them reads through its own file handle**; without it there is no handle pool and every worker goes through the one shared handle, which Windows serialises (`src/llama-mmap.cpp:266-277`, fallback at `:457`). The two flags are one setting with two names |
| `--moe-stream-direct` | Unbuffered reads, and the thing that opens the handle pool at all. Without it `read_raw_at` falls back to the shared handle and the pool delivers 1.01x instead of 2.22x |
| `--slot-save-path` | Where the server writes its KV state so a session survives a restart. Without it the next start re-prefills the whole history: the 22 ms restore is measured; the ~35 minutes for 23,400 tokens is extrapolated from a run aborted at 10 %. Must be an existing directory or the server refuses to start |
| `--chat-template-file` | 0731 ships no Jinja template, and the one embedded in the GGUF fails the model's own golden vector 4 (an action turn opens a think block it never closes). The shipped file renders all four vectors byte-identically; verified in jinja2 and against a live server |
| `--moe-stream-l2 32` | Optional [host-RAM tier](#5-the-host-ram-tier-optional), in GiB. On 0731, three pairs on 2026-08-10: **1.24x / 1.55x / 1.50x per pair, 1.49x median against median** (19.13 against 12.84 tok/s). The price is 32 GiB of page-locked memory. Leave it out and Crow streams exactly as it did before, at a 26.99 GiB peak measured on the preview |
| *no `-lv 5`* | Not a flag but the absence of one, and it is worth as much as any flag here. See below |

`--moe-stream-io-threads` is the number of *workers*, not the queue depth the drive sees. That one is measured, and it is **4.31**.

<a id="a-diagnostic-flag-costs-a-factor-of-fourteen"></a>
**Turning the verbosity up on a console costs a factor of 14 to 16.** Measured 2026-08-10 on a
fresh server: the start line above decodes at **16.05 tok/s over 108 tokens**, inside the measured
arms (16.17 / 19.13 / 19.25). The *same* line with `-lv 5` writing into an interactive console
decodes at **0.98 / 1.01 / 1.13 tok/s** across three runs. The debug log is about 40 lines per
token, and the gap between two consecutive lines is **2.05 ms into a redirected file against
20.3 ms onto a console**. Every CUDA graph launch pays it, prefill and decode alike; the card sat
at 2895 MHz and 155 W of a 575 W limit throughout, which is what a GPU waiting on its host looks
like. So a diagnostic run redirects stderr into a file — the six gate runs behind every figure on
this page do, which is why they carry `-lv 5` and still measure the operating point. Append
`2> server.err` to the start line above and the flag costs nothing worth measuring; leave the log
on the console and the number you take is the console's, not the model's.

### Step 2 — check that the right server is running

Two read-only queries, both in seconds:

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:8081/props').default_generation_settings.n_ctx
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
```

Expect `n_ctx = 200192` (llama.cpp rounds up) and about **32,038 of 32,607 MiB** — measured
2026-08-11 on 0731, five seconds after `/health` first answered, with 978 MiB of desktop already on
the card. Your own reading moves with whatever else is drawing on the GPU, which is the point of
looking: a second consumer is the difference between 14 and 5 tok/s on a cold prefill, and nothing
in the server's own log says so. Under a filled context the only pair on record is the preview
model's (31,899 / 31,997); 0731 has not been read there. (The previously documented 31,838 and
32,008 were two phases of that same preview measurement and neither said which; they are withdrawn.)
If `n_ctx` says 65536 or 16384, a measurement server is running and not the operating point.

### Step 3 — start the client

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow.py
```

It opens on the wordmark, with the commands beside it and the repository under them, then the
endpoint and the model the server actually has open:

```console
    ██████╗██████╗  ██████╗ ██╗    ██╗    /help for commands
   ██╔════╝██╔══██╗██╔═══██╗██║    ██║    /tools for what the model can call
   ██║     ██████╔╝██║   ██║██║ █╗ ██║    /exit to leave
   ██║     ██╔══██╗██║   ██║██║███╗██║
   ╚██████╗██║  ██║╚██████╔╝╚███╔███╔╝    https://github.com/nibor1896/Crow
    ╚═════╝╚═╝  ╚═╝ ╚═════╝  ╚══╝╚══╝
   v0.1.0


crow at http://127.0.0.1:8081/v1 (health: ok, 200k context)
DeepSeek-V4-Flash-0731
```

The last line is not the `-m` you passed — that is `crow`, at the front of the line above it. The
model name is read back out of the server's `/props`, so an endpoint serving something else says so
directly under its own address.

## Using the CLI

Standard library only, on purpose: it has to run before anything is installed.

| Command | |
|---|---|
| `/help` | the commands |
| `/tools` | the tools the model can call, read out of the schema it is sent |
| `/reset` | drop the conversation and start a new one |
| `/context` | how much of the window is used |
| `/exit`, `/quit` | leave — both spellings, because half the world types the other one |

A line that starts with `/` turns yellow as you type it, so a command is
distinguishable from a message before you commit to it. That needs the terminal
in raw mode; piped input and terminals that do not support it fall back to a
plain read, where colour is off anyway.

| Option | |
|---|---|
| `--base-url` | default `http://127.0.0.1:8081/v1` |
| `-m` | model name sent to the endpoint (default `crow`) |
| `--api-key` | placeholder, default `local-no-provider`. The local server does not check it |
| `--system` | replace the system prompt, `--no-system` removes it |
| `--temperature` | default **1.0**, see below |
| `--top-p` | default **0.95** — the model card's agentic figure. Its own `generation_config.json` says 1.0; the disagreement is real and is recorded at `cli/crow.py:2733` rather than resolved silently |
| `--min-p` | default **0.01**, unsloth's recommendation for this quantisation. Sent explicitly because llama.cpp's server default is 0.05, and not sending it means inheriting a third value nobody chose |
| `--reasoning-effort` | `low`, `high` or `max`. Rides in `chat_template_kwargs` and only when set; unset, the template picks `low` itself. This is the knob that moves the reasoning share below |
| `--timeout` | socket timeout in seconds (default **1800**) |
| `--rollover-at` | archive and start fresh at this share of the window, `0` switches it off (default **0.9**) |
| `--max-tool-rounds` | how many tool rounds one turn may take before it answers from what it has (default **24**) |
| `--resume FILE` | resume a named session file — an archive left by a rollover; a bare name is looked for among the session files |
| `--no-session` | do not resume the last session, and do not save this one |
| `--no-update-check` | do not ask GitHub whether a newer release exists |
| `--no-font`, `--no-background` | leave the terminal profile alone |

There is no `--max-tokens`, and one error message still says there is: a turn stopped by the
server's budget prints `CUT OFF at the token budget -- raise --max-tokens` (`cli/crow.py:1563`)
while the parser accepts no such option (`:2708-2777`). The client sends no `max_tokens` field at
all, so the budget that stopped the turn is the server's. Tracked as
[#63](https://github.com/nibor1896/Crow/issues/63), together with the rejected turn that stays in
the history behind it.

<a id="the-window-rolls-over-instead-of-hitting-the-wall"></a>
**The window rolls over instead of hitting the wall.** The server's limit is not
a slope: a request that arrives at or past `n_ctx` is refused outright, and the
turn is lost with it. At 90 % of the window Crow writes the conversation out to
`rollover-<stamp>.json` **and `rollover-<stamp>.md`**, empties it, and opens the
next one with a note naming the transcript, its line count, and the paths the
work had reached. The model has `read_file`, so the pointer is one it can follow.

Each of those three came from watching it fail. Driven live on 2026-08-10 at the
operating point, the first version pointed only at the JSON — which `json.dump`
writes as **one 104,618-byte line**, so `read_file`'s 16 KB cap could only ever
show the first 15 % of it, cut mid-field, from the oldest end. And it said
nothing about where the work had got to. The model guessed a directory that does
not exist, scanned a whole user profile twice, and spent **402 s across seven
tool rounds** before it read the archive at all. Hence the transcript, the line
count, and `Last worked on:`.

This does not break append-only. Nothing is edited inside a prefix that is still
in use — the old context is written out whole and dropped whole, and what
follows is a new prefix that has never been sent. An edit would leave the
server's cache matching a conversation that no longer exists.

Two details are load-bearing. The check also runs **inside** the tool loop, not
only between turns: one tool round has been measured adding 5,253 tokens and up
to 24 of them run without the user typing anything, so a turn that starts under
the threshold can still walk into the wall on its own. And an archive is written
**without** the KV cache — the server's slot file has one fixed name, so saving
it would put the archive's cache over the live one, and a save is ~1.3 GiB at a
full window onto the same drive the experts stream from. An archive resumes from
its messages and pays a prefill, which is the honest price.

If the server will not say how large the window is, `n_ctx` is 0 and the
rollover stays off — `0.9 × 0` would otherwise be a threshold every turn crosses.

<a id="a-resumed-cache-is-checked-not-announced"></a>
**A resumed cache is checked, not announced.** `POST /slots/0?action=restore`
returning 200 means the file was read, not that the slot now holds the prefix
about to be sent. Measured 2026-08-10: a start that printed `cache warm` was
followed by `cached 0/21004` and **469.51 s to the first token** — a full
re-prefill of a conversation the client had just promised was cached, with
nothing in between admitting it.

Two checks now stand between that promise and the user. The save records the
server's own `n_saved`; the restore compares it against `n_restored` and
withdraws the claim when the numbers disagree. And because a number can agree
while the cache still does not match, the first turn settles it for good: if the
start said `cache warm` and that turn comes back with `cached 0`, Crow says so
in one line instead of leaving eight minutes unexplained. A server that reports
neither figure is believed — silence is not a contradiction.

Five properties come from measurements rather than taste:

<a id="the-context-is-append-only-and-carries-its-reasoning"></a>
**The context is append-only, and every assistant turn carries its reasoning.** Nothing is ever inserted in front of, or edited inside, an existing message, because the prompt cache only survives while the prefix stays byte-identical.

The second half of that was the harder lesson. Until 2026-08-08 the client dropped `reasoning_content` from the history — it is display-only, so why send it back. But this model's template renders a kept turn as `<think>…</think>`, and an omitted field leaves an *empty* think block: the prefix then diverges exactly where the thoughts began, and everything behind that point is re-read. The cost therefore has nothing to do with how much the model thought. **48 tokens of reasoning cost 2,018 tokens of prefill**, because the whole answer sat behind them.

Measured across three task sets, `prompt_n` of turn 2 over what turn 1 generated:

| turn 2 sends | ratio | prefill |
|---|---|---|
| history **without** `reasoning_content` | 0.909 – 0.986 | 138.8 – 242.3 s |
| history **with** `reasoning_content` | 0.008 – 0.016 | 1.6 – 2.2 s |

It is not cumulative, it repeats: every turn pays the previous turn's output, so the penalty scales with answer length rather than session length. This is also why the client sends its `tools` array with **every** request — for the cache as much as for the tools. The template keeps a past turn's thoughts only while tools are present; with an empty array both variants render byte for byte the same, and the prefix diverges.

<a id="what-a-fresh-turn-sends"></a>
**What that costs on the wire: 909 of 953 tokens.** Measured 2026-08-10 through the server's own `/apply-template` and `/tokenize`, a first turn with a five-token message sends **953 tokens** — 5 of them the message, 39 the system prompt, and **909, or 95.4 %, the seven tool declarations**. They are unchanged since 0.0.1 and they ride on every request by design. It is also why a fresh turn's prefill is not free even when the conversation is empty.

**The output streams, and the raven shows the state.** On the **preview** model, over 30 stored answers, 88.2 % of everything generated was `reasoning_content` — 69,951 reasoning characters against 9,337 of content. That figure is unchanged since 0.0.1 and **has not been re-taken on 0731**, which is the more important caveat now that `--reasoning-effort low|high|max` exists: the share is a setting, not a property of the model. What holds either way is the consequence — a client that renders only `content` shows a blank screen for most of the wait. Crow shows `thinking`, then flips to `writing code` at the first content token, and the turn's timing line prints the actual `thinking %` for that turn.

**`--temperature` defaults to 1.0, not 0.** 1.0 is what DeepSeek-V4-Flash-0731 specifies — its model card runs its agentic benchmarks there and its `generation_config.json` agrees. (0.6 was the **preview** family's value and shipped in every release up to 0.0.6; a comparison across the model switch has to pin `--temperature` on both sides or it measures two changes as one.) 0 stays dangerous in either family: pure greedy decoding has no way out of a repetition attractor, and measured 2026-08-07 on a three.js task the model repeated *"Actually, let me…"* inside its reasoning block and never reached an answer. `--temperature 0` stays available so measurement runs get byte-identical output.

On first start the client installs its bundled typeface and writes `profiles.defaults.font.face` and `background` into Windows Terminal's `settings.json`, with a `.bak` beside it. It never overwrites a value it did not write itself. Both halves can be switched off.

## Updating

**The client tells you.** On start it asks GitHub whether a newer release exists and, if there is one, prints it above the prompt together with the command that installs it:

```
crow 0.1.1 is out (you have 0.1.0)
  irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1 | iex
```

That is the same one-liner that installs Crow in the first place. It reads the version out of the installation it finds, updates when its own is newer, and does nothing when you are already current. `crow --version` prints what you have.

The check runs in the background while the banner and the health probe do their work, and it is given at most 1.5 seconds of the start. It never blocks a turn, never prints an error, and stays silent on a machine with no network. `--no-update-check` switches it off.

**Your model is not touched.** The ~97 GiB under `%LOCALAPPDATA%\Crow\models` is not part of any package, so an update never deletes the install directory — it writes the new files over the old ones and leaves everything else alone.

**The server may keep running.** Windows locks a running binary, and the moment
the client tells you a new version exists is exactly the moment `llama-server`
is up in the other terminal. The installer renames the files in `bin\` to `.old`
first — Windows permits renaming a running executable, the process keeps its
handle, and the path is freed for the new file. Your session carries on with the
old binary; the new one takes over the next time you start the server. What
cannot be moved is named and the install stops there rather than failing halfway
through an extraction. The `.old` files are swept up afterwards, and the ones
still held are reported as still held — a running server's file cannot be
deleted, only renamed.

Two things it will not do without being asked. It will not overwrite a directory it cannot identify as a Crow install, and it will not put an older version over a newer one. Both refuse and print the invocation that forces it, because `irm … | iex` cannot be given a `-Force` switch:

```
&([scriptblock]::Create((irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1))) -Force
```

## Common questions

**Does it need the model in RAM?** No. The 97.1 GiB file is never held in host memory. The highest process peak ever measured here is **33.73 GiB**, and 32 of those are the optional tier; turn it off and the same binary peaked at **26.99 GiB** at this context length. Both peaks were taken on the **preview** model at 200k and have not been re-run on 0731. (The 1.28 GiB figure further down is [#24](https://github.com/nibor1896/Crow/issues/24), measured at `-c 4096`, and does not describe the operating point.)

**What does `--moe-stream-l2` do?** It keeps expert weights in page-locked host RAM between the VRAM slots and the drive. On **0731**, three pairs on 2026-08-10: decode **19.13 against 12.84 tok/s** at the median arm, 1.24x / 1.55x / 1.50x per pair, and the stall per miss falls from 1.280–1.320 ms to 0.717–0.741 ms. The microbenchmark behind that — 56.7 µs per weight tensor out of the tier against 401.5 µs through the drive — was taken on 2026-08-09 on the preview model and has not been re-run. The price is 32 GiB of memory the rest of the machine cannot use. The installer prints the flag above 60 GB of detected RAM, because 32 GiB on ~64 GB is the only ratio that has been run. [Details](#5-the-host-ram-tier-optional).

**Is the output the same as a resident model?** On the deterministic half of the coding gate, yes — six of six tasks byte-identical to the reference after the load-path rework, **measured on the preview model, without the host tier**, and not re-taken with it or on 0731. The other half of the gate produces three different programs across three runs at *identical* configuration, so that half cannot answer the question. See [what is not claimed](#what-is-not-claimed).

**Why Windows only?** The streaming path rests on `FILE_FLAG_NO_BUFFERING`, positional `OVERLAPPED` reads and a per-worker handle pool, because Windows serialises I/O on the file object. The POSIX side of the primitive exists and compiles; it has never been run.

**Can I use a smaller quantisation?** Measured on the **preview** model, and no: `UD-IQ1_S` did not write wrong code, it wrote none — no function block emitted at all. That file was deleted on 2026-08-10 (`manifests/operating-point.json` → `models.iq1-s.present = false`), so the result stands as a record and is not repeatable, and the break point has not been located again on 0731.

**Why not just buy more VRAM?** More VRAM does keep buying throughput; there is no knee across 18 to 64 cache slots. But the model is 97.1 GiB and no consumer card holds it, so streaming is not a workaround for a small card — it is the only shape that runs at all.

---

# Part II: How it works

## The problem: a model that does not fit

DeepSeek-V4-Flash-0731 is 304,180,418,494 parameters — counted from the safetensors headers of all
48 shards, and the figure HuggingFace's own API reports. **284.33 B of those are what Crow loads:**
the GGUF carries the trunk, and the three DSpark speculation blocks (19,845,850,983 parameters) ship
as a separate file that is never read unless it is asked for. Resident at bf16, the trunk alone is:

![Every parameter resident at bf16](docs/images/eq_naive_memory.png)

568 GB against 34.2 GB of VRAM and 63.4 GB of system RAM. Nothing about placement or scheduling closes a gap of eighteen times.

Two published paths exist and neither works here. Keeping the experts in host RAM needs a machine with 200 GB or more — ktransformers documents exactly that minimum, and this machine misses it by a factor of 3.2. Memory-mapping the file and letting the OS page it in was measured on 2026-08-02 and is a different operating regime, not a slower one: **1.8 % CPU, 0.3 GB of 63.4 GB free, 0.79 GiB/s effective against a drive that does 5.3 GB/s sequential.** The process was not computing. It was waiting on random 4 KiB faults with eviction pressure.

## 1. Sparsity: most of the model is asleep

A dense model reads every weight for every token. A mixture-of-experts model does not:

![Six of 256 experts fire per layer](docs/images/eq_sparsity.png)

43 layers, 256 experts each, 6 of them selected per token. The rest of that layer contributes nothing to this token and does not need to be anywhere near the GPU.

That leaves a set which *is* needed by every token — attention, norms, embeddings, the shared expert:

![The always-active set](docs/images/eq_resident_set.png)

On 0731: **6,625.00 MiB on CUDA0 plus 414.26 MiB of CUDA_Host buffers = 6.88 GiB, 7.08 % of the 97.05 GiB file** (`r1-l2.err:7307-7308`). The routed experts are the other **90.17 GiB** — 378,208,256 B per expert across all 43 layers, times 256 experts. It is GPU-resident by construction at `-ngl 99`; nothing had to be built for it. The remaining 92.9 % is the streaming problem.

Routing is concentrated enough for a cache to be worth anything, and that was measured before any of it was built on the preview model: 80 % of expert selections fell on **25.7 %** of the experts, Gini 0.713. Coding routes *wider* than prose (25.7 % against 21.4 %), and those 4.3 points showed up as 5.7 points less hit rate — so the workload this project cares about is the harder one. 0731 routes the same way, and the server prints it itself: across the three cumulative blocks of the `r1-l2` arm, 80 % of selections fall on **23.4 %, then 25.2 %, then 26.2 %** of the experts, at Gini 0.740 → 0.719 → 0.708. The distribution flattens as a run gets longer and more experts have been touched at all; it does not flatten enough to make the cache pointless.

## 2. Quantisation, and where it breaks

Fewer bits per weight means fewer bytes to move. The question is where the model stops being worth running, and it is answered by running it:

![Speed against quality across three rungs](docs/images/quant_ladder.png)

> **This whole section is the preview-model series.** Both rungs it compares against — `UD-Q2_K_XL`
> and `UD-IQ1_S` — were deleted on 2026-08-10 to make room for 0731 (`manifests/operating-point.json`
> → `models.*.present = false`). The measurements stand as a record of why IQ3_XXS is the rung that
> ships; they cannot be repeated, and no quantisation ladder has been run on 0731.

`UD-IQ1_S` was the fastest rung that fits and it was worthless — it emitted no function block at all, repeated the prompt and drifted into reasoning until the budget ran out. The interesting comparison was one rung up.

**IQ3 decoded 15 % slower per token than Q2 and needed 15 % fewer tokens. The two cancelled: 4.5 seconds difference over 29 minutes across 30 answers** — and IQ3 was right more often. `merge-intervals` failed on Q2_K_XL in 3 of 3 runs *byte-identically* (an aliasing bug on a shallow copy) and was correct on IQ3_XXS in 3 of 3, also byte-identically. Both sides deterministic, different hashes, different verdicts. The bug was quantisation-bound.

The failure mode this axis exists to prevent was inverted here: there was no speed win to report without its quality cost, there was a **quality win at no measurable cost in time.**

## 3. The slot cache, and what VRAM buys

Each streamed layer gets a fixed number of expert slots in VRAM. A hit means the weights are already there; a miss means fetching them while the compute thread waits.

![What VRAM buys across four cache sizes](docs/images/slot_ladder.png)

From 18 to 64 slots: **+15.50 GiB of VRAM for +4.67 tok/s** — measured on the **preview** model, and the slot sizes on the axis are that model's. A slot costs more on 0731: 23,084 MiB / 64 = **360.69 MiB per slot**, against 356.19 on the preview — the same 64-slot cache was 288 MiB smaller there — so the same ladder would be about 1.3 % wider in GiB. The shape is what this figure is for. The gain per GiB does not fall off across the range — 0.216, 0.363 and 0.319 tok/s per GiB — which is why no intermediate size is recommended as optimal. Size the cache to the safe limit of the card.

A second question the cache answers only partly: **cold misses cannot be cached away.** They are the working set every layer must touch once, and they stay constant across every cache size. On the preview's ten-task gate they were 5.0 % of misses (10,167 of 203,558). On **0731** the same counter reads **9,257 cold of 76,509 misses, 12.1 %**, at the end of the `r1-l2` arm — a larger share, because that arm is a shorter run and the share falls as a run gets longer: evictions accumulate and first touches do not. Growing the cache removes evictions, never first touches.

The cache also has a hard floor that the graph, not the option, imposes:

![The wave cap](docs/images/eq_wave_cap.png)

Multi-pass expert GEMMs need at least `3 x n_expert_used` slots. Upstream's default computed `2 x n_expert_used` clamped to 16, which is smaller than the 18 the graph requires. That did not fail at load: the `GGML_ABORT` sat inside `build_moe_ffn` and fired on the **first batch that touched more experts than the cache holds** — model loaded, request running, and far from the option that caused it (`src/llama-model.cpp:1338-1343`; measured 2026-08-03, `"have 16, need 18"`). Crow refuses at the option instead, at load time, with the number named.

**What a wave is.** A single token touches at most 6 experts per layer, so it always fits. A
*batch* does not: a ubatch of 512 tokens can select more experts in one layer than the 64 slots
hold, and there is no ordering of the loads that makes them all resident at once. So the graph
splits the touched experts of that layer into **waves** — passes of at most `plan_capacity`
experts each, run one at a time, with the experts of the wave that is not running masked out
(`src/llama-moe-stream.h:102-111`). Prefill is where this happens; decode at `-np 1` is one wave.

The part that matters for throughput is what happens *during* a wave: while wave *w* computes, the
slabs of wave *w+1* are already being fetched (`stage_wave_locked`, `:420-422`). The server counts
whether that paid off, and prints it per model instance:

```
moe stream: waves = 387 (206 non-empty), preloads issued = 4158 (ready on arrival = 586), wave stall = 3918.40 ms
```

That line is from `r1-l2.err:42670`, after the first graded task of the 0731 tier arm: 586 of 4,158
preloads had arrived by the time their wave ran, and 3.92 s were spent waiting for the rest. It is
a cumulative counter like every other one on this page — see [below](#the-statistics-lines-are-cumulative).

## 4. Reading the drive without the page cache

This is where most of the engineering went, and the numbers are not subtle.

**Page faults are the wrong instrument.** A fault is synchronous and per-thread: a thread touches a missing page, blocks, one read is issued, it resumes. Measured during prefill: queue depth **0.35**, 707 MB/s through the fault path, on a drive that reaches **10,592 MB/s** when asked properly — eight threads, private handles, positional reads, measured 2026-08-03 (`tools/probe-queue-depth.py:1055`).

**Windows serialises on the file object, not the file pointer.** Measured across both read mechanisms:

| handle | mechanism | depth 8 vs 1 |
|---|---|---:|
| shared | `SetFilePointerEx` | 0.98x |
| shared | `OVERLAPPED` offset | 1.01x |
| per-thread | `SetFilePointerEx` | 2.19x |
| **per-thread** | **`OVERLAPPED` offset** | **2.22x** |

The read mechanism makes no difference. Sharing the handle makes all of it. So `llama_file` opens **18 private handles** and the I/O workers each read through their own. At the operating point only 8 of the 18 are ever used, because `--moe-stream-io-threads 8` is where the drive saturates — depth 64 measured 2.15x against 2.22x at depth 8. The other ten are headroom with a purpose: a worker whose id is at or past the pool falls back to the shared handle and takes the serialisation with it, which is the one failure the pool exists to prevent (`src/llama-mmap.cpp:95-107`).

**One work item per weight tensor, not per expert.** An expert carries 2–3 weight tensors — this page also calls one of them a **slab**, and it is the unit everything below is counted in: one weight tensor of one expert in one layer, 43 × 256 × 3 = 33,024 of them in the file. Reading an expert's slabs in a loop keeps one request in flight at a time:

![Queue depth from 1.60 to 4.31](docs/images/eq_queue_depth.png)

Same bytes, same request size, 29 % more decode throughput. That change alone broke reproducibility — several workers began calling `ggml_backend_tensor_set` concurrently, which the single-worker loop never did, and `binary-search` started producing two different programs across three runs. A mutex held **only** around the upload restores it; the disk read stays outside the lock, which is where the gain lives.

This work produced a fix that went upstream on its own: `llama_file` on Windows had no positional unbuffered read at all, and `has_direct_io()` returned a hard `true` on a path that had never opened anything unbuffered — so external code logged "O_DIRECT, page cache bypassed" while reading through the page cache. Submitted as [ggml-org/llama.cpp#26541](https://github.com/ggml-org/llama.cpp/issues/26541) and [#26542](https://github.com/ggml-org/llama.cpp/pull/26542).

## 5. The host-RAM tier (optional)

Everything above spends VRAM and drive bandwidth. The third resource on the machine was sitting
idle: without the tier the server peaked at 26.99 GiB at 200k context (preview model), and a 64 GB
box still has room.

`--moe-stream-l2 32` puts a second cache level there, between the VRAM slots and the drive.

### What 0.1.0 measures, on DeepSeek-V4-Flash-0731

Three pairs on 2026-08-10, `runs/2026-08-10/0731-pairs`, fingerprinted in
`manifests/runs-2026-08-10.json`. Each arm is a fresh server; both arms of a pair solve the same
tasks; the pairs solve different ones.

| | with `--moe-stream-l2 32` | without |
|---|---:|---:|
| decode, three arms | 16.17 / **19.13** / 19.25 tok/s | 13.07 / 12.33 / **12.84** |
| median against median | **19.13** | **12.84** — a factor of **1.49** |
| per pair | 1.24x / 1.55x / 1.50x | |
| stall per miss | 0.741 / 0.730 / 0.717 ms | 1.320 / 1.280 / 1.303 |
| decode spent waiting on a miss | 57.6 / 69.1 / 70.5 % | 78.2 / 80.3 / 80.7 % |
| tier hit rate, in slabs | 35.67 / 34.82 / 30.62 % | — |
| graded tasks correct | 6 of 6 | 6 of 6 |

Every row reads pair 1 / pair 2 / pair 3, in that order.

**The denominators, because the headline is small.** The median tier arm is `r2-l2`: **3 answers,
1,428 decoded tokens, 74,644.72 ms**. The median base arm is `r3-base`: 3 answers, 1,002 tokens,
78,053.77 ms. Three answers per arm, six per pair, eighteen in the whole series.

Two things that number does *not* say on its own. First, **the three answers include the arm's
ungraded warm-up task** — `tools/run-l2-pairs.ps1:66-89` sums every `eval time` line in the log,
which is the honest reading of "what this server did", not "what it scored". Counting only the two
graded tasks per arm gives 16.63 / 19.18 / 20.29 with the tier and 13.50 / 12.30 / 12.57 without,
medians 19.18 and 12.57 — the headline survives, the wording had to. Second, **within-arm spread on
0731 is 1.19x with the tier and 1.06x without**, against an effect of 1.49x. The band is
indicative; the direction clears it, and it is not a tight measurement.

**Prefill at filled context** — server-counted denominators, fresh server: 96.13 tok/s at 1,374
tokens · 85.32 at 10,824 · 83.80 at 43,224 · **76.54 at 172,824**. These four were measured on the
**preview** model on 2026-08-10 at temperature 0.6 and without `--chat-template-file`
(`runs/2026-08-10/before-0731/prefill/summary.json`); they have **not** been re-run on 0731. They
replace an older "8–50 tok/s", which came from 86–103-token prompts and did not describe filled
context — large batches amortise expert fetches.

**Cold against warm prefill, on the same server.** The four figures above are all *warm*. Measured
2026-08-10: **953 tokens at 12.79 tok/s with the expert cache empty, 984 at 62.68 tok/s once it is
not.** Inside the cold run the rate climbs from 9.93 tok/s over the first 437 tokens to 17.1 over
the remaining 512. Nothing else on this page describes a first start.

**What 0731 costs in VRAM.** 378,208,256 B per expert across the 43 layers, so a 64-slot cache is
23,084 MiB — **288 MiB more than the preview** at the same slot count. Measured 2026-08-11 on 0731
at 200k on one slot: **32,038 MiB of 32,607 after load**, leaving **569 MiB**. That reading includes
978 MiB of desktop, taken on the same card before the server started — the server's own share is
about 31,060 MiB, and a machine with nothing else drawing has correspondingly more room.

Under a **filled** context 0731 has not been read. The only pair on record there is the preview
model's (`runs/2026-08-10/before-0731/`): 31,899 after load and 31,997 filled, a 98 MiB rise for
200k of KV. Carrying that rise across predicts roughly 32,136 on 0731 — arithmetic, not a reading.
The "599 MiB of headroom" quoted before was 32,607 − 32,008, and 32,008 is one of the two values
this release withdraws.

### The arrangement, and the preview series that established it

The prices below were measured **2026-08-09 on the preview model**, at the two block sizes 41 of
the 43 layers move — 2,686,976 B for `ffn_gate_exps`/`ffn_up_exps`, 3,211,264 B for
`ffn_down_exps`. Two layers carry larger experts (4,456,448 and 3,604,480 B) and were not in the
series. None of this has been re-run on 0731:

| | rate | one work item |
|---|---:|---:|
| SSD, pooled read | 10,592.7 MB/s | 253.7 µs |
| host → device, pageable | 18,175.5 MB/s | 147.8 µs |
| **host → device, pinned** | **47,357.4 MB/s** | **56.7 µs** |

One **work item** — a single slab — cost **56.7 µs** out of the tier against **401.5 µs**
through the drive path, 7.08x. Spread over twelve runs was 0.10–4.36 %.

That is the microbenchmark, not the product. An expert is two or three slabs and a miss is
queued behind seven other workers, so end to end a miss cost **1.28–1.35 ms without the tier and
0.73–0.75 ms with it** on the preview — the 0731 figures in the table above, 1.280–1.320 against
0.717–0.741 ms, are the same shape on the shipped model. Both numbers are real and they answer
different questions.

**How often it actually pays.** The tier's own hit rate, counted in **slabs rather than experts**,
at the end of each arm: **35.67 / 34.82 / 30.62 % on 0731**, against **35.94 / 31.86 / 28.90 %** on
the preview series — pair 1 / 2 / 3 in both. Roughly two thirds of what a token needs still reaches
the drive either way. (An earlier printing of this line gave 26.02 as the low end. That figure is
an intermediate block from a different run, `runs/2026-08-09/x-l2/x-l2.err:21281`, not one of the
three paired arms.)

At 32 GiB the tier holds **7,695 slots of 4,464,640 B** — one slot takes the largest slab in the
model plus its direct-I/O alignment slack, so any slab fits any slot and the allocator cannot
fragment (`src/llama-moe-stream.cpp:263`). Two numbers follow from that, and they are not the same
number:

- **7,695 of the file's 33,024 slabs: 23.3 %.** That is a share of the *count* of slabs, and it is
  the tier's **capacity**, not how full it is — at 35.67 % hit rate most of what a token wants is
  still elsewhere.
- **The payload that capacity can hold is about 21.0 GiB, in a 32.00 GiB allocation.** The average
  slab in this model is 378,208,256 B / 129 slabs per expert = 2,931,847 B, while every slot is
  4,464,640 B wide. 7,695 × 2,931,847 B = 22,560,562,665 B = **21.0 GiB of weights** inside an
  allocation of 7,695 × 4,464,640 B = 32.00 GiB; the remaining **~11.0 GiB is stride slack**, the
  price of an allocator that cannot fragment. "32 GiB" is what the flag takes from the machine, not
  what it stores.

**Filling it is free, and that is what makes it worth building.** The worker already read every
missing slab into a staging buffer and threw the buffer away. Now the read lands directly in a tier
slot and the upload sources from there: the same read, the same bytes, kept instead of discarded. A
design that copied into the tier afterwards would spend more per fill than a later hit returns.

![The host tier against no tier, paired on identical tasks](docs/images/host_tier.png)

**The preview series, and why it is shaped the way it is: 14.73 against 10.54 tok/s at the median,
1.40–1.47x per pair — and the arrangement is half the
result.** Two earlier attempts produced nothing. All
ten gate tasks repeated per run gave 7.65 and 15.77 tok/s at *identical* configuration — a 2.06x
spread, because the second run meets the cache the first warmed, and with 32 GiB of experts held
that shared state is the subject. Giving each arm different tasks removed the carry-over and
replaced it with arms solving differently hard problems. What works is both at once: the same tasks
within a pair, fresh tasks across pairs, each arm on its own server so the tier starts empty on
both sides. Within-arm spread then fell to 1.09x and 1.07x — narrower than the difference, which
is the only reason the difference could be read at all. 0731 inherited the arrangement unchanged;
its within-arm spread is wider (1.19x), and that is said above rather than here.

**What it costs.** 32 GiB of page-locked memory, held for the life of the process and unavailable
to everything else. Peak process memory went from 26.99 GiB to 33.73 GiB at the operating point —
measured on a live server on the **preview** model, not derived, and not re-run on 0731. That is why
the flag is off by default and why the installer prints it only
above 60 GB of detected RAM: 32 GiB on a ~64 GB machine is the only ratio that has been run.

**What it cannot do.** Catch a cold first touch. Those bytes have never been read, so no cache
holds them; over the preview's tier-era ten-task gate they were 10,167 of 203,558 misses, and on
0731's `r1-l2` arm 9,257 of 76,509. Everything else is an
eviction, and only those are addressable here.

**A defect worth recording, because no throughput number would have shown it.** The first version
handed out a resident slot and released its lock; another worker took the same slot as an eviction
victim and read a different expert into it mid-upload. The model emitted 8,191 characters of
`<<<<<<<<` instead of an answer — at 31–35 tok/s, a fast run by every counter that existed. A cache
race is invisible in throughput. The fix pins a slot while it is being read and commits a filled
one only after its bytes have left for the GPU.

The obvious suspect for the slowdown that followed was measured before anything was rebuilt: the
tier's global mutex costs 0.553 µs per operation, 1,469 ms over 2,655,285 operations, 0.16 % of
wall time. Sharding it would have been wasted work.

## What it costs per token

![Bytes per token](docs/images/eq_bytes_per_token.png)

And the honest headline about where a request's time actually goes:

![Wait share](docs/images/eq_wait_share.png)

**57.6, 69.1 and 70.5 % of decode is the thread waiting on a miss** — the three tier arms at the
operating point on 0731, 2026-08-10 — against **78.2, 80.3 and 80.7 %** on the same tasks without
the tier (`runs/2026-08-10/0731-pairs/l2-pairs.csv`, `load stall` over summed `eval time`). The
preview series read 56–59 % and 70–72 % on the same measure. The tier does not remove the wait, it
halves what each miss costs. The GPU idles most of that time — `utilization.gpu` at 28 %, the memory
controller at 7 %, no throttling in any sample, measured on
[#39](https://github.com/nibor1896/Crow/issues/39) before the tier existed and not re-taken since.
This is not a compute-bound system and it is not a bandwidth-bound one. It is a latency-bound one,
and every lever above acts on that.

<a id="the-statistics-lines-are-cumulative"></a>
**Every statistics line the server prints is cumulative, and nothing resets them.** `load stall`,
`slot wait`, `wave stall` and `L2 lock wait` — and the hit, miss and cold-miss counts beside them —
run over the whole life of the model instance, i.e. over the server process
(`src/llama-moe-stream.cpp:746-751`, `tools/server/server-context.cpp:686-690`). A request-local
figure is the **difference between two consecutive blocks**, which the reader can form and the
writer cannot. It is visible in one file: cold misses in the `r1-l2` arm read 8,252, then 8,730,
then 9,257 across the three blocks of a single run. Every stall and hit-rate figure on this page
that names an arm is the arm's last block, warm-up task included — read a single block as one
request and it will be wrong by everything before it.

Context is nearly free by comparison — 1,353.50 MiB of KV buffers for `n_ctx = 200192` on 0731,
6.92 KiB per token:

![A 200k context costs 1.32 GiB](docs/images/eq_kv_cost.png)

## Against CPU offload

The comparison that matters is not against another project. It is against what the *same executable* does with the experts left on the CPU — one binary, one quantisation, one prompt, placement as the only variable:

![Streaming against CPU offload](docs/images/against_cpu.png)

Streaming is **1.35x on decode, 2.14x on prefill, and needs 40x less host memory**, at roughly 3.2x the VRAM. The CPU side ran at **256 MiB of available system RAM** — the thrashing regime, measured on [#24](https://github.com/nibor1896/Crow/issues/24) at UD-Q2_K_XL and `-c 4096`. All of it is the preview-model era; no CPU-offload arm has been run on 0731, and none can be run at this operating point (see [what is not claimed](#what-is-not-claimed)).

No quality difference between the placements is demonstrable: they are one gate task apart, and the gate's aggregate detection limit is two.

**These figures are for Crow without the host tier**, which is what the comparison is about: CPU offload puts the experts in host RAM and computes against them there, and the point is that streaming beats it while barely touching that memory. Turning the tier on trades some of that advantage back deliberately — a **preview-model** peak of 33.73 GiB instead of the 1.28 GiB measured on [#24](https://github.com/nibor1896/Crow/issues/24) at `-c 4096`, so 1.5x less host memory than CPU offload rather than 40x, and on 0731 1.49x more throughput. Both host-memory numbers are preview-era and neither has been re-run on 0731. Two different products from one binary, and the flag says which one is running.

## Batching, and why the CLI does not

![Aggregate throughput against batch depth](docs/images/batch_curve.png)

Batching never buys throughput for free. From batch 1 to 8 the experts touched per call grow by a factor of 3.9 for 2.09x the aggregate. The break is at 8 and the source predicts it: `stream_wave_cap = (64-6)/2 = 29`, batch 4 asks 19.80 experts per call and stays in one wave, batch 8 asks 31.16, falls into two, and the hit rate drops for the first time.

**Batch 4 is the knee: 1.80x aggregate at unchanged hit rate.** And per request it is a loss — 8.88 down to 4.00 tok/s. An interactive client has one user, so the CLI runs `-np 1` by construction and none of this applies to it. These figures describe the harness case, which is deferred.

## What is not claimed

Written out because a page like this is easy to read as more than it says.

- **Half the coding gate is not deterministic**, measured on the **preview** model and not re-taken on 0731. Five of ten tasks produced three different programs across three runs at identical configuration, temperature 0, fixed seed. The split is clean and it tracks chain length: every stable task stays under 302 decoded tokens, every unstable one starts at 517. So `k of 10` must not be averaged, and a difference landing on the unstable half is not attributable to anything.
- **The gate resolves two tasks in the aggregate**, not one. A lever that costs a single task is not distinguishable from the gate's own movement.
- **There is no baseline at the operating point itself.** The CPU-offload comparison was measured at `-c 4096` on Q2_K_XL; nobody has run CPU offload at 200k with IQ3, and this machine has not the host memory to try.
- **Vendor model-card scores are statements about that vendor's harness**, not about the model, and none of them survives 2-bit or 3-bit quantisation. No published quality figure exists for the file this project runs.
- **Nothing here is compared to another project's number.** The nearest published figures for this class of workload differ from this operating point in at least two free variables each, so a "we are faster" line would be measuring the difference between two machines.
- **The upstream CUDA fault this project tracks was never reproduced here**, on this quantisation and this card. That is not a claim that it is fixed.
- **The headline 19.13 tok/s is the median of three arms with the host tier, not what every chat turn feels like.** One arm, three answers, 1,428 decoded tokens over 74,644.72 ms, at near-empty context, and the three answers include the arm's ungraded warm-up. Within-arm spread on 0731 is 1.19x against an effect of 1.49x, so the band is indicative rather than tight. Live turns in the client on 2026-08-09 — **preview model** — with 1–5k of conversation behind them, decoded at 11.79–16.72 tok/s; before the tier, comparable turns ran at 8.08–8.56. No live-turn series has been taken on 0731. The relationship between a gate arm and a chat turn is not measured, and the measurement that would settle it is a decode series against context length.
- **No VRAM figure has been read on 0731.** The 31,899 / 31,997 MiB pair at 200k is a preview-model measurement; the ~32,200 quoted for the operating point adds the cache's known +288 MiB to it and is arithmetic, not a reading. The measurement that would settle it is one `nvidia-smi` call against a loaded 0731 server.
- **A cold start is not on this page except where it says so.** Prefill at filled context, and every decode figure here, is the warm case. Cold against warm on the same server was 12.79 against 62.68 tok/s.
- **The tier's factor is measured at one size, on one machine, under 6k of context.** 32 GiB on a 63.4 GB host: 1.49x on 0731, 1.40–1.47x on the preview. No other tier size has been run, and nothing says the factor survives a full 200k window — the working set grows and the tier does not.
- **The gate resolves two tasks in aggregate, and the tier comparison rests on two graded tasks per arm.** On 0731 it is 6 of 6 correct with the tier against 6 of 6 without: *no difference found*, which at this resolution is what the design can produce and is not evidence about quality either way.
- **Decode falls over the server's uptime, and this page does not describe that.** Every figure here comes from a fresh server. On a server up 121 minutes the same operating point decoded at 0.97–1.19 tok/s, while a freshly started server at a resumed 63.9k context ran at 14.97 — so it is uptime and not context. Open as [#71](https://github.com/nibor1896/Crow/issues/71); the workaround is a restart, and there is no fix to claim.
- **The measured arms are not byte-for-byte the printed start line.** All six carry `-lv 5` into a redirected file and none carries `--slot-save-path`; the former is why the [logging note](#a-diagnostic-flag-costs-a-factor-of-fourteen) exists, and the latter buys session survival rather than throughput. Neither difference has been measured as a throughput effect.

---

## What's next

**Crow acts now.** Since 0.0.5 a reply can become an action: the client executes the call, hands the result back and asks again, up to 24 rounds — `--max-tool-rounds` moves that, and a turn that runs out answers from what it has instead of stopping. The agent core is [#55](https://github.com/nibor1896/Crow/issues/55), and it is **still open**: the loop ships and works, and what closes the ticket is the measurement below it, not the code.

**Open**

- [ ] **Decode collapses on a long-running server.** From ~15 to ~1 tok/s after about two hours at the operating point, restored completely by a restart. Not the context: a fresh server at a resumed 63.9k window runs at 14.97 tok/s. Paging, competing processes and memory growth were each measured out. Reproduced again in the 0.1.0 handover, in the first turn of a fresh server. **This hits the operating point this page documents, and the only remedy is to restart the server.** [#71](https://github.com/nibor1896/Crow/issues/71)
- [ ] **What the client does at the context wall.** A rejected turn stays in the history, and the message printed at the wall tells the user to raise `--max-tokens`, an option the client does not have. [#63](https://github.com/nibor1896/Crow/issues/63)
- [ ] **Edits that survive the file having moved on.** `edit_file` matches exactly and refuses an ambiguous or missing match — it fails loudly instead of guessing, which is the behaviour worth having first. What it does not do is recognise a change that is *already applied*, or an indentation that drifted. That needs approximate matching, and it is the one piece worth taking from hermes-agent rather than writing.
- [ ] **A list the model keeps for itself.** At this decode rate a long run drifts, and a visible list of what is done and what is left is what keeps it on course. Small — the value is the habit, not the code.
- [ ] **Measuring the loop rather than demonstrating it.** *A model that can express a tool call is not the same as a model that makes good ones.* One live session is not a figure. This is the open half of [#55](https://github.com/nibor1896/Crow/issues/55); the server-side half that had to come first, starting the server so the template can express a tool call at all, is closed as [#58](https://github.com/nibor1896/Crow/issues/58).

**Decided by measurement, not by preference**

- [ ] **Staying fast and keeping the context as a session grows.** Two acceptance criteria, neither measured beyond ten turns: a turn must not get slower as the session lengthens, and the context that matters must still be there at the end of the window. [#61](https://github.com/nibor1896/Crow/issues/61)
- [ ] **Whether the tier holds up at a full window.** Every paired run stayed under 6k of context. The 1.49x is measured there and nowhere else, and the tier's hit rate in one long session — rather than across short graded tasks — is unknown.
- [ ] **A name and a logo.** `Crow` is the project name, not a product name. Tracked as [#56](https://github.com/nibor1896/Crow/issues/56), with one hard constraint already measured: the bundled typeface has 0 of 256 braille glyphs, so a braille logo and this font cannot both ship.

**Deliberately not next**

Batching across parallel agents is the lever this whole architecture was built for — one expert load serving many tokens instead of one. It is measured, and the ticket that measured it is closed ([#31](https://github.com/nibor1896/Crow/issues/31)); it needs agents before it can batch them, so the lever sits behind the loop above rather than in front of it.

---
## How this project works

These rules are the actual product of the repository; the numbers follow from them.

- **Every number carries its denominator.** Anything unmeasured is marked as unmeasured, together with the one measurement that would settle it.
- **A cited number needs its suite in the same commit, and that suite needs a case that must fail.** A checker that cannot go red measures nothing.
- **A zero without a positive control is not a finding.** Every search includes a term that has to hit; otherwise "not present" cannot be told apart from "not looked for".
- **A criterion a correct implementation cannot meet manufactures faults instead of finding them.** Comparing answer hashes across two compute backends was such a criterion and was withdrawn.
- **Two numbers taken under different conditions are not a comparison**, however similar they look.
- **Raw protocols stay out of git**, but their fingerprints do not — `manifests/` records what a run produced even though the bytes are not versioned.
- **Closed work is not deleted.** Issues closed with a corrected goal say so on the ticket.

Every tool in `tools/` answers `-Selftest` (PowerShell) or `selftest` (Python) and refuses to report a verdict without passing it. The figures in this file are generated, not drawn: `docs/images/_eq.py` and `docs/images/_plots.py`.

| | |
|---|---|
| `install.ps1` | the one-command installer, with its own suite |
| `cli/` | the client and its seven tools, standard library only, 210 tests |
| `patches/` | the streaming patch against its pinned upstream tag |
| `tools/` | measuring, packing and verification tools, each with a selftest |
| `docs/images/` | the generators behind every figure above |
| `manifests/` | size and SHA-256 of every raw protocol, per day |
| `runs/` | raw protocols — **not** in git, recorded by the manifests |

### Building it yourself

The streaming path is a patch against `llama.cpp`, applied to a pinned tag, never to a moving branch:

```bash
git -C <llama.cpp-clone> worktree add --detach <tree> b10269
powershell -File tools/verify-patch-b10269.ps1 -WT <tree> -BuildDir build -UpTo build -NoNegative
```

That applies `patches/moe-stream-on-b10269.patch`, configures with CUDA, builds, and checks the result — the flags reaching the binary, the symbols surviving the link, the patched paths matching the expected count. Dropping `-UpTo build -NoNegative` runs the full verification including a control build on the unpatched base.

`tools/pack-release.ps1` builds the shipped archive and refuses one that is not self-contained: every import of every binary must resolve inside the package or to a Windows system library, and it iterates, because `cublas64_13.dll` only reveals that it needs `cublasLt64_13.dll` once it is itself in the set.

## Licence

MIT, see [`LICENSE`](LICENSE). Four components carry terms this project cannot grant, and [`NOTICE`](NOTICE) says which: `ggml-org/llama.cpp` (MIT, other copyright holders), `deepseek-ai/DeepSeek-V4-Flash` (MIT, fetched rather than shipped), the NVIDIA CUDA Toolkit the CUDA backend is built against, and Google Sans Code under the SIL Open Font License 1.1.

## Credits

Measured on one machine: RTX 5090 (32,607 MiB), 63.4 GB DDR5, 24 threads, one Phison NVMe. **Spent so far: 0 EUR** — no rented compute, no API calls.

<div align="center">
<br>
<a href="https://ko-fi.com/nibor1896"><img src="https://img.shields.io/badge/support%20this%20on-ko--fi-ff5e5b?style=for-the-badge" alt="Ko-fi"></a>
</div>
