<div align="center">

<h1>Crow</h1>

<h3>A 284-billion-parameter coding model, at a 200k context. One graphics card. 33 GB of system RAM.</h3>

<p>Frontier mixture-of-experts inference, with the experts streamed off the SSD.<br>No cluster. No 200 GB host. No cloud.</p>

<p>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square&logo=opensourceinitiative&logoColor=white&labelColor=000000" alt="License"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/version-0.0.5-brightgreen?style=flat-square&logo=semver&logoColor=white&labelColor=000000" alt="Version"></a>
<a href="#requirements"><img src="https://img.shields.io/badge/platform-Windows%20x64%20%C2%B7%20CUDA-555555?style=flat-square&logo=nvidia&logoColor=76b900&labelColor=000000" alt="Platform"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/client-Python%20stdlib%20only-555555?style=flat-square&logo=python&logoColor=ffd43b&labelColor=000000" alt="Python"></a>
<a href="https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash"><img src="https://img.shields.io/badge/model-DeepSeek--V4--Flash-orange?style=flat-square&logo=huggingface&logoColor=ffd21e&labelColor=000000" alt="Model"></a>
</p>

<table>
<tr>
<td align="center"><b>284B</b><br><sub>parameters</sub></td>
<td align="center"><b>13B</b><br><sub>active per token</sub></td>
<td align="center"><b>200k</b><br><sub>context, one slot</sub></td>
<td align="center"><b>95.9 GiB</b><br><sub>model on disk</sub></td>
<td align="center"><b>33.73 GiB</b><br><sub>peak host RAM, measured</sub></td>
<td align="center"><b>14.73</b><br><sub>tok/s decode, gate median</sub></td>
<td align="center"><b>0 EUR</b><br><sub>spent so far</sub></td>
</tr>
</table>

</div>

<br>

## What this is, in one paragraph

**Crow runs a frontier-scale coding model on a single consumer graphics card by leaving most of the model on the SSD.**

A mixture-of-experts model is mostly asleep. Every token wakes only **6 of the 256 experts** in each of its 43 layers, so 92.7 % of the file is untouched at any given moment. Crow keeps the parts that *every* token needs in VRAM — attention, norms, shared experts, 6.88 GiB of them — holds the 64 most useful experts per layer beside them in a slot cache, and reads whatever is missing straight off the drive while the GPU is still working. The host machine never holds the model: **33.73 GiB of process memory for a 95.9 GiB file**, and 32 of those are a cache it does not need — without it the server peaks at 26.99 GiB and runs 4 tok/s slower.

The context window is 200,000 tokens, on a single slot, and it costs 1.32 GiB of the card — compressed attention makes context the cheap part here. A coding session holds files and history, so a 16k or 64k window would be measuring a product nobody uses.

**Since 0.0.5 the host's spare RAM can be spent to make that cheaper.** A machine with 64 GB has tens of gigabytes doing nothing. `--moe-stream-l2 32` keeps expert weights there between the VRAM slots and the drive: a miss that finds its expert in host memory uploads at 47,357 MB/s instead of fetching it at 10,593, and the measured cost of a miss falls from 1.28–1.35 ms to 0.73–0.75. The flag defaults to off; the installer puts it into the command it prints on any machine that has the RAM — [what it buys and what it costs](#5-the-host-ram-tier-optional).

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

**[What's next](#whats-next)** · **[How this project works](#how-this-project-works)** · **[Licence](#licence)**

---

# Part I: Getting started

## Requirements

| | |
|---|---|
| **GPU** | NVIDIA, **16 GB VRAM minimum**, 32 GB for the measured operating point. Below 16 GB was never measured and is unsupported |
| **System RAM** | **64 GB for the operating point**, which spends 32 GiB on the [host tier](#5-the-host-ram-tier-optional). 32 GB runs without it, at a 26.99 GiB peak and ~1.4x less throughput |
| **Disk** | ~2 GB for Crow, **~96 GB for the model** |
| **OS** | Windows x64. The streaming path uses `FILE_FLAG_NO_BUFFERING` and a handle pool, both Windows-specific |
| **Python** | 3.8+, for the client only. Standard library, nothing to install |

The installer checks all of this **before** it downloads anything.

## Quick start

```powershell
irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1 | iex
```

Five steps, no elevation, everything under `%LOCALAPPDATA%\Crow`:

```console
  Crow 0.0.5

[1/5] Checking this machine
      GPU  NVIDIA GeForce RTX 5090, 32607 MB
      RAM  63.4 GB
      Disk  364.3 GB free on C:
      Windows  64-bit, PowerShell 5.1.26100.8875
      preflight  passed

[2/5] Downloading the package
      crow-0.0.5-win-x64.zip  [####################.....]  84%  424.1 MB / 506.4 MB  18.2 MB/s

[3/5] Verifying
      size  506.4 MB
      sha256  A31F...

[4/5] Installing to C:\Users\you\AppData\Local\Crow
        26/26  README.md                                  27 KB
      26 files extracted

[5/5] What is left to do
```

The model is **not** part of that download. It is 95.9 GiB, it belongs to somebody else, and an installer that spends hours on it before you have seen anything work is the wrong shape. The last step prints the one command that fetches it:

```powershell
hf download unsloth/DeepSeek-V4-Flash-GGUF --include "UD-IQ3_XXS/*" --local-dir %LOCALAPPDATA%\Crow\models
```

> **A trap worth knowing about.** Measured here on 2026-08-07: when `hf` cannot reach the repository it prints `✓ Downloaded` and returns the local directory — failure that looks exactly like success. Check that four files totalling ~96 GiB actually arrived.

## Full setup

### Step 1 — start the server

```powershell
%LOCALAPPDATA%\Crow\bin\llama-server.exe `
  -m %LOCALAPPDATA%\Crow\models\UD-IQ3_XXS\DeepSeek-V4-Flash-UD-IQ3_XXS-00001-of-00004.gguf `
  --port 8081 -c 200000 -ngl 99 -np 1 --jinja `
  --slot-save-path %LOCALAPPDATA%\Crow\session `
  --moe-stream --moe-stream-cache 64s --moe-stream-io-threads 8 --moe-stream-direct `
  --moe-stream-l2 32
```

The installer prints this line filled in for your machine. It leaves `--moe-stream-l2` out below 60 GB of detected RAM — Windows reports 63.4 on a 64 GB machine, so a threshold at the nominal size would exclude exactly the configuration this was measured on.

Every flag carries a reason, and none of them is taste:

| Flag | Why |
|---|---|
| `-c 200000` | A coding session holds files and history. 16k or 64k measures a product nobody uses. Measured: 200k loads on one slot at 32,008 of 32,607 MiB |
| `--port 8081` | Not a preference. `llama-server` defaults to 8080 and the client defaults to 8081, so leaving it out gives a server the client cannot find — and on Windows 8080 is often already taken |
| `-np 1` | One user, one stream. `-np 4` splits the context into 4 × 50k and is the harness case, not the CLI |
| `--jinja` | Use the **model's** chat template instead of llama.cpp's built-in one. Without it the client's replayed reasoning is dropped and the prompt cache breaks on every turn: measured 138.8–242.3 s of re-prefill per turn against 1.6–2.2 s |
| `--moe-stream` | Route expert tensors through the slot cache instead of placing them |
| `--moe-stream-cache 64s` | 64 of 256 experts per layer, **22.0 GiB**. 121 slots would cover 95 % of expert *selections* and need 41.6 GiB, which does not fit — coverage is not hit rate, because a first touch can never be cached |
| `--moe-stream-io-threads 8` | I/O workers, **each with its own file handle**. Windows serialises on the file object, so a shared handle stays at queue depth 1 whatever you do |
| `--moe-stream-direct` | Unbuffered reads. Without it `read_raw_at` falls back to the shared handle and the pool delivers 1.01x instead of 2.22x |
| `--slot-save-path` | Where the server writes its KV state so a session survives a restart. Without it the next start re-prefills the whole history: the 22 ms restore is measured; the ~35 minutes for 23,400 tokens is extrapolated from a run aborted at 10 %. Must be an existing directory or the server refuses to start |
| `--moe-stream-l2 32` | Optional [host-RAM tier](#5-the-host-ram-tier-optional), in GiB. 1.40–1.47x throughput on this machine, at the price of 32 GiB of page-locked memory. Leave it out and Crow streams exactly as it did before, at a 26.99 GiB peak |

`--moe-stream-io-threads` is the number of *workers*, not the queue depth the drive sees. That one is measured, and it is **4.31**.

### Step 2 — check that the right server is running

Two read-only queries, both in seconds:

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:8081/props').default_generation_settings.n_ctx
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
```

Expect `n_ctx = 200192` (llama.cpp rounds up) and about **31,800 of 32,607 MiB**. If it says 65536 or 16384, a measurement server is running and not the operating point.

### Step 3 — start the client

```powershell
python %LOCALAPPDATA%\Crow\cli\crow.py
```

## Using the CLI

Standard library only, on purpose: it has to run before anything is installed.

| Command | |
|---|---|
| `/help` | the commands |
| `/tools` | the tools the model can call, read out of the schema it is sent |
| `/reset` | drop the conversation and start a new one |
| `/context` | how much of the window is used |
| `/exit` | leave |

A line that starts with `/` turns yellow as you type it, so a command is
distinguishable from a message before you commit to it. That needs the terminal
in raw mode; piped input and terminals that do not support it fall back to a
plain read, where colour is off anyway.

| Option | |
|---|---|
| `--base-url` | default `http://127.0.0.1:8081/v1` |
| `-m` | model name sent to the endpoint |
| `--system` | replace the system prompt, `--no-system` removes it |
| `--temperature` | default **0.6**, see below |
| `--rollover-at` | archive and start fresh at this share of the window, `0` switches it off (default **0.9**) |
| `--resume FILE` | resume a named session file — an archive left by a rollover; a bare name is looked for among the session files |
| `--no-font`, `--no-background` | leave the terminal profile alone |

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

Four properties come from measurements rather than taste:

<a id="the-context-is-append-only-and-carries-its-reasoning"></a>
**The context is append-only, and every assistant turn carries its reasoning.** Nothing is ever inserted in front of, or edited inside, an existing message, because the prompt cache only survives while the prefix stays byte-identical.

The second half of that was the harder lesson. Until 2026-08-08 the client dropped `reasoning_content` from the history — it is display-only, so why send it back. But this model's template renders a kept turn as `<think>…</think>`, and an omitted field leaves an *empty* think block: the prefix then diverges exactly where the thoughts began, and everything behind that point is re-read. The cost therefore has nothing to do with how much the model thought. **48 tokens of reasoning cost 2,018 tokens of prefill**, because the whole answer sat behind them.

Measured across three task sets, `prompt_n` of turn 2 over what turn 1 generated:

| turn 2 sends | ratio | prefill |
|---|---|---|
| history **without** `reasoning_content` | 0.909 – 0.986 | 138.8 – 242.3 s |
| history **with** `reasoning_content` | 0.008 – 0.016 | 1.6 – 2.2 s |

It is not cumulative, it repeats: every turn pays the previous turn's output, so the penalty scales with answer length rather than session length. This is also why the client sends a one-entry `tools` array with every request — **for the cache, not for the tool.** The template keeps a past turn's thoughts only while tools are present; with an empty array both variants render byte for byte the same.

**The output streams, and the raven shows the state.** 88.2 % of everything this model generates is `reasoning_content` — measured over 30 stored answers, 69,951 reasoning characters against 9,337 of content. A client that renders only `content` shows a blank screen for most of the wait. Crow shows `thinking`, then flips to `writing code` at the first content token.

**`--temperature` defaults to 0.6, not 0.** Pure greedy decoding has no way out of a repetition attractor, and this model loops inside the reasoning block and never reaches an answer. `--temperature 0` stays available so measurement runs get byte-identical output.

On first start the client installs its bundled typeface and writes `profiles.defaults.font.face` and `background` into Windows Terminal's `settings.json`, with a `.bak` beside it. It never overwrites a value it did not write itself. Both halves can be switched off.

## Updating

**The client tells you.** On start it asks GitHub whether a newer release exists and, if there is one, prints it above the prompt together with the command that installs it:

```
crow 0.0.6 is out (you have 0.0.5)
  irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1 | iex
```

That is the same one-liner that installs Crow in the first place. It reads the version out of the installation it finds, updates when its own is newer, and does nothing when you are already current. `crow --version` prints what you have.

The check runs in the background while the banner and the health probe do their work, and it is given at most 1.5 seconds of the start. It never blocks a turn, never prints an error, and stays silent on a machine with no network. `--no-update-check` switches it off.

**Your model is not touched.** The 95.9 GiB under `%LOCALAPPDATA%\Crow\models` is not part of any package, so an update never deletes the install directory — it writes the new files over the old ones and leaves everything else alone.

Two things it will not do without being asked. It will not overwrite a directory it cannot identify as a Crow install, and it will not put an older version over a newer one. Both refuse and print the invocation that forces it, because `irm … | iex` cannot be given a `-Force` switch:

```
&([scriptblock]::Create((irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1))) -Force
```

## Common questions

**Does it need the model in RAM?** No. The 95.9 GiB file is never held in host memory: the operating point peaks at 33.73 GiB, and 32 of those are the optional tier. Turn it off and the same binary peaks at **26.99 GiB** at this context length — the 1.28 GiB figure below is #24, measured at `-c 4096`, and does not describe the operating point.

**What does `--moe-stream-l2` do?** It keeps expert weights in page-locked host RAM between the VRAM slots and the drive. One weight tensor costs 56.7 µs out of the tier instead of 401.5 through the drive; end to end a miss costs 1.79x less. Measured: **1.40–1.47x decode** over three paired runs, at the price of 32 GiB of memory the rest of the machine cannot use, and a process that peaks at 33.73 GiB instead of 1.28. The installer prints the flag above 60 GB of detected RAM, because 32 GiB on ~64 GB is the only ratio that has been run. [Details](#5-the-host-ram-tier-optional).

**Is the output the same as a resident model?** On the deterministic half of the coding gate, yes — six of six tasks byte-identical to the reference after the load-path rework, **measured without the host tier**, and not re-taken with it. The other half of the gate produces three different programs across three runs at *identical* configuration, so that half cannot answer the question. See [what is not claimed](#what-is-not-claimed).

**Why Windows only?** The streaming path rests on `FILE_FLAG_NO_BUFFERING`, positional `OVERLAPPED` reads and a per-worker handle pool, because Windows serialises I/O on the file object. The POSIX side of the primitive exists and compiles; it has never been run.

**Can I use a smaller quantisation?** Measured, and no: `UD-IQ1_S` does not write wrong code, it writes none — no function block emitted at all. The break point is above IQ1_S.

**Why not just buy more VRAM?** More VRAM does keep buying throughput; there is no knee across 18 to 64 cache slots. But the model is 95.9 GiB and no consumer card holds it, so streaming is not a workaround for a small card — it is the only shape that runs at all.

---

# Part II: How it works

## The problem: a model that does not fit

DeepSeek-V4-Flash is 284 billion parameters. Resident at bf16, that is:

![Every parameter resident at bf16](docs/images/eq_naive_memory.png)

568 GB against 34.2 GB of VRAM and 63.4 GB of system RAM. Nothing about placement or scheduling closes a gap of eighteen times.

Two published paths exist and neither works here. Keeping the experts in host RAM needs a machine with 200 GB or more — ktransformers documents exactly that minimum, and this machine misses it by a factor of 3.2. Memory-mapping the file and letting the OS page it in was measured on 2026-08-02 and is a different operating regime, not a slower one: **1.8 % CPU, 0.3 GB of 63.4 GB free, 0.79 GiB/s effective against a drive that does 5.3 GB/s sequential.** The process was not computing. It was waiting on random 4 KiB faults with eviction pressure.

## 1. Sparsity: most of the model is asleep

A dense model reads every weight for every token. A mixture-of-experts model does not:

![Six of 256 experts fire per layer](docs/images/eq_sparsity.png)

43 layers, 256 experts each, 6 of them selected per token. The rest of that layer contributes nothing to this token and does not need to be anywhere near the GPU.

That leaves a set which *is* needed by every token — attention, norms, embeddings, the shared expert:

![The always-active set](docs/images/eq_resident_set.png)

**6.88 GiB, 7.17 % of the 95.93 GiB of tensors** — 89.05 GiB of them routed experts. It is GPU-resident by construction at `-ngl 99`; nothing had to be built for it. The other 92.7 % is the streaming problem.

Routing is concentrated enough for a cache to be worth anything, and that was measured before any of it was built: 80 % of expert selections fall on **25.7 %** of the experts, Gini 0.713. Coding routes *wider* than prose (25.7 % against 21.4 %), and those 4.3 points show up as 5.7 points less hit rate — so the workload this project cares about is the harder one.

## 2. Quantisation, and where it breaks

Fewer bits per weight means fewer bytes to move. The question is where the model stops being worth running, and it is answered by running it:

![Speed against quality across three rungs](docs/images/quant_ladder.png)

`UD-IQ1_S` is the fastest rung that fits and it is worthless — it emits no function block at all, repeats the prompt and drifts into reasoning until the budget runs out. The interesting comparison is one rung up.

**IQ3 decodes 15 % slower per token than Q2 and needs 15 % fewer tokens. The two cancel: 4.5 seconds difference over 29 minutes across 30 answers** — and IQ3 is right more often. `merge-intervals` fails on Q2_K_XL in 3 of 3 runs *byte-identically* (an aliasing bug on a shallow copy) and is correct on IQ3_XXS in 3 of 3, also byte-identically. Both sides deterministic, different hashes, different verdicts. The bug was quantisation-bound.

The failure mode this axis exists to prevent is inverted here: there is no speed win to report without its quality cost, there is a **quality win at no measurable cost in time.**

## 3. The slot cache, and what VRAM buys

Each streamed layer gets a fixed number of expert slots in VRAM. A hit means the weights are already there; a miss means fetching them while the compute thread waits.

![What VRAM buys across four cache sizes](docs/images/slot_ladder.png)

From 18 to 64 slots: **+15.50 GiB of VRAM for +4.67 tok/s.** The gain per GiB does not fall off across the range — 0.216, 0.363 and 0.319 tok/s per GiB — which is why no intermediate size is recommended as optimal. Size the cache to the safe limit of the card.

A second question the cache answers only partly: **cold misses cannot be cached away.** They are the working set every layer must touch once, they stay constant across every cache size. At the operating point they are 5.0 % of misses (10,167 of 203,558) — the share falls as a run gets longer, because evictions accumulate and first touches do not. Growing the cache removes evictions, never first touches.

The cache also has a hard floor that the graph, not the option, imposes:

![The wave cap](docs/images/eq_wave_cap.png)

Multi-pass expert GEMMs need at least `3 x n_expert_used` slots. Upstream's default computed `2 x n_expert_used` clamped to 16, which is smaller than the 18 the graph requires — so the default aborted during graph build on every model with 6 or more active experts. Crow refuses at the option instead, at load time, with the number named.

## 4. Reading the drive without the page cache

This is where most of the engineering went, and the numbers are not subtle.

**Page faults are the wrong instrument.** A fault is synchronous and per-thread: a thread touches a missing page, blocks, one read is issued, it resumes. Measured during prefill: queue depth **0.35**, 707 MB/s through the fault path, on a drive that reaches 10,533 MB/s when asked properly.

**Windows serialises on the file object, not the file pointer.** Measured across both read mechanisms:

| handle | mechanism | depth 8 vs 1 |
|---|---|---:|
| shared | `SetFilePointerEx` | 0.98x |
| shared | `OVERLAPPED` offset | 1.01x |
| per-thread | `SetFilePointerEx` | 2.19x |
| **per-thread** | **`OVERLAPPED` offset** | **2.22x** |

The read mechanism makes no difference. Sharing the handle makes all of it. So `llama_file` holds 18 private handles, and the I/O workers each read through their own.

**One work item per weight tensor, not per expert.** An expert carries 2–3 weight tensors, and reading them in a loop keeps one request in flight at a time:

![Queue depth from 1.60 to 4.31](docs/images/eq_queue_depth.png)

Same bytes, same request size, 29 % more decode throughput. That change alone broke reproducibility — several workers began calling `ggml_backend_tensor_set` concurrently, which the single-worker loop never did, and `binary-search` started producing two different programs across three runs. A mutex held **only** around the upload restores it; the disk read stays outside the lock, which is where the gain lives.

This work produced a fix that went upstream on its own: `llama_file` on Windows had no positional unbuffered read at all, and `has_direct_io()` returned a hard `true` on a path that had never opened anything unbuffered — so external code logged "O_DIRECT, page cache bypassed" while reading through the page cache. Submitted as [ggml-org/llama.cpp#26541](https://github.com/ggml-org/llama.cpp/issues/26541) and [#26542](https://github.com/ggml-org/llama.cpp/pull/26542).

## 5. The host-RAM tier (optional)

Everything above spends VRAM and drive bandwidth. The third resource on the machine was sitting
idle: without the tier the server peaks at 26.99 GiB at 200k context, and a 64 GB box still has room.

`--moe-stream-l2 32` puts a second cache level there, between the VRAM slots and the drive. The
prices, measured 2026-08-09 at the two block sizes 41 of the 43 layers move — 2,686,976 B for
`ffn_gate_exps`/`ffn_up_exps`, 3,211,264 B for `ffn_down_exps`. Two layers carry larger experts
(4,456,448 and 3,604,480 B) and were not in the series:

| | rate | one work item |
|---|---:|---:|
| SSD, pooled read | 10,592.7 MB/s | 253.7 µs |
| host → device, pageable | 18,175.5 MB/s | 147.8 µs |
| **host → device, pinned** | **47,357.4 MB/s** | **56.7 µs** |

One **work item** — a single weight tensor — costs **56.7 µs** out of the tier against **401.5 µs**
through the drive path, 7.08x. Spread over twelve runs was 0.10–4.36 %.

That is the microbenchmark, not the product. An expert is two or three work items and a miss is
queued behind seven other workers, so end to end **a miss costs 1.28–1.35 ms without the tier and
0.73–0.75 ms with it: 1.79x**. Both numbers are real and they answer different questions.

**How often it actually pays: 26–36 %.** That is the tier's own hit rate over the three paired
runs (26.02 / 31.86 / 35.94 %), counted in slabs rather than experts. Roughly two thirds of what a
token needs still reaches the drive.

At 32 GiB the tier holds **7,695 slots** of 4,464,640 B — one slot takes the largest slab in the
model plus its alignment slack, so any weight fits any slot and the allocator cannot fragment.
That is 7,695 of the 33,024 slabs in the file: **23.3 %** of the model resident in host RAM.

**Filling it is free, and that is what makes it worth building.** The worker already read every
missing slab into a staging buffer and threw the buffer away. Now the read lands directly in a tier
slot and the upload sources from there: the same read, the same bytes, kept instead of discarded. A
design that copied into the tier afterwards would spend more per fill than a later hit returns.

![The host tier against no tier, paired on identical tasks](docs/images/host_tier.png)

**14.73 against 10.54 tok/s at the median, 1.40–1.47x per pair — and the arrangement is half the
result.** Two earlier attempts produced nothing. All
ten gate tasks repeated per run gave 7.65 and 15.77 tok/s at *identical* configuration — a 2.06x
spread, because the second run meets the cache the first warmed, and with 32 GiB of experts held
that shared state is the subject. Giving each arm different tasks removed the carry-over and
replaced it with arms solving differently hard problems. What works is both at once: the same tasks
within a pair, fresh tasks across pairs, each arm on its own server so the tier starts empty on
both sides. Within-arm spread then falls to 1.09x and 1.07x — narrower than the difference, which
is the only reason the difference can be read at all.

**What it costs.** 32 GiB of page-locked memory, held for the life of the process and unavailable
to everything else. Peak process memory goes from 26.99 GiB to 33.73 GiB at the operating point — measured on a live
server, not derived. That is why the flag is off by default and why the installer prints it only
above 60 GB of detected RAM: 32 GiB on a ~64 GB machine is the only ratio that has been run.

**What it cannot do.** Catch a cold first touch. Those bytes have never been read, so no cache
holds them; over the tier-era ten-task gate they were 10,167 of 203,558 misses. Everything else is an
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

**56–59 % of decode is the thread waiting on the drive** at the operating point, over three paired runs — and **70–72 %** without the host tier. The tier does not remove the wait, it halves what each miss costs. The GPU idles most of that time — `utilization.gpu` at 28 %, the memory controller at 7 %, no throttling in any sample, measured on #39 before the tier existed and not re-taken since. This is not a compute-bound system and it is not a bandwidth-bound one. It is a latency-bound one, and every lever above acts on that.

Context is nearly free by comparison:

![A 200k context costs 1.32 GiB](docs/images/eq_kv_cost.png)

## Against CPU offload

The comparison that matters is not against another project. It is against what the *same executable* does with the experts left on the CPU — one binary, one quantisation, one prompt, placement as the only variable:

![Streaming against CPU offload](docs/images/against_cpu.png)

Streaming is **1.35x on decode, 2.14x on prefill, and needs 40x less host memory**, at roughly 3.2x the VRAM. The CPU side ran at **256 MiB of available system RAM** — the thrashing regime, measured on #24 at UD-Q2_K_XL and `-c 4096`.

No quality difference between the placements is demonstrable: they are one gate task apart, and the gate's aggregate detection limit is two.

**These figures are for Crow without the host tier**, which is what the comparison is about: CPU offload puts the experts in host RAM and computes against them there, and the point is that streaming beats it while barely touching that memory. Turning the tier on trades some of that advantage back deliberately — 33.73 GiB instead of 1.28, so 1.5x less host memory than CPU offload rather than 40x, and 1.4x more throughput. Two different products from one binary, and the flag says which one is running.

## Batching, and why the CLI does not

![Aggregate throughput against batch depth](docs/images/batch_curve.png)

Batching never buys throughput for free. From batch 1 to 8 the experts touched per call grow by a factor of 3.9 for 2.09x the aggregate. The break is at 8 and the source predicts it: `stream_wave_cap = (64-6)/2 = 29`, batch 4 asks 19.80 experts per call and stays in one wave, batch 8 asks 31.16, falls into two, and the hit rate drops for the first time.

**Batch 4 is the knee: 1.80x aggregate at unchanged hit rate.** And per request it is a loss — 8.88 down to 4.00 tok/s. An interactive client has one user, so the CLI runs `-np 1` by construction and none of this applies to it. These figures describe the harness case, which is deferred.

## What is not claimed

Written out because a page like this is easy to read as more than it says.

- **Half the coding gate is not deterministic.** Five of ten tasks produce three different programs across three runs at identical configuration, temperature 0, fixed seed. The split is clean and it tracks chain length: every stable task stays under 302 decoded tokens, every unstable one starts at 517. So `k of 10` must not be averaged, and a difference landing on the unstable half is not attributable to anything.
- **The gate resolves two tasks in the aggregate**, not one. A lever that costs a single task is not distinguishable from the gate's own movement.
- **There is no baseline at the operating point itself.** The CPU-offload comparison was measured at `-c 4096` on Q2_K_XL; nobody has run CPU offload at 200k with IQ3, and this machine has not the host memory to try.
- **Vendor model-card scores are statements about that vendor's harness**, not about the model, and none of them survives 2-bit or 3-bit quantisation. No published quality figure exists for the file this project runs.
- **Nothing here is compared to another project's number.** The nearest published figures for this class of workload differ from this operating point in at least two free variables each, so a "we are faster" line would be measuring the difference between two machines.
- **The upstream CUDA fault this project tracks was never reproduced here**, on this quantisation and this card. That is not a claim that it is fixed.
- **The headline 14.73 tok/s is the median of three paired runs with the host tier, not what every chat turn feels like.** It was measured over two graded coding tasks per pair at the operating point — near-empty context. Live turns in the client on 2026-08-09, with 1–5k of conversation behind them, decoded at 11.79–16.72 tok/s; before the tier, comparable turns ran at 8.08–8.56. All of these are measured; the relationship between them is not, and the measurement that would settle it is a decode series against context length.
- **The tier's 1.40–1.47x is measured at one size, on one machine, under 6k of context.** 32 GiB on a 63.4 GB host. No other tier size has been run, and nothing says the factor survives a full 200k window — the working set grows and the tier does not.
- **The gate resolves two tasks in aggregate, and the tier comparison rests on two graded tasks per pair.** 6 of 6 correct with the tier against 5 of 6 without is *no difference found*, not *no difference* — and certainly not evidence that the tier improves quality.

---

## What's next

**Crow acts now.** Since 0.0.5 a reply can become an action: the client executes the call, hands the result back and asks again, up to 24 rounds. That was [#55](https://github.com/nibor1896/Crow/issues/55), and it changes what the remaining list is about.

**Open**

- [ ] **Edits that survive the file having moved on.** `edit_file` matches exactly and refuses an ambiguous or missing match — it fails loudly instead of guessing, which is the behaviour worth having first. What it does not do is recognise a change that is *already applied*, or an indentation that drifted. That needs approximate matching, and it is the one piece worth taking from hermes-agent rather than writing.
- [ ] **A list the model keeps for itself.** At this decode rate a long run drifts, and a visible list of what is done and what is left is what keeps it on course. Small — the value is the habit, not the code.
- [ ] **Measuring the loop rather than demonstrating it.** *A model that can express a tool call is not the same as a model that makes good ones.* One live session is not a figure. [#58](https://github.com/nibor1896/Crow/issues/58)

**Decided by measurement, not by preference**

- [ ] **Staying fast and keeping the context as a session grows.** Two acceptance criteria, neither measured beyond ten turns: a turn must not get slower as the session lengthens, and the context that matters must still be there at the end of the window. [#61](https://github.com/nibor1896/Crow/issues/61)
- [ ] **Whether the tier holds up at a full window.** Every paired run stayed under 6k of context. 1.40–1.47x is measured there and nowhere else, and the tier's hit rate in one long session — rather than across short graded tasks — is unknown.
- [ ] **A name and a logo.** `Crow` is the project name, not a product name. Tracked as [#56](https://github.com/nibor1896/Crow/issues/56), with one hard constraint already measured: the bundled typeface has 0 of 256 braille glyphs, so a braille logo and this font cannot both ship.

**Deliberately not next**

Batching across parallel agents is the lever this whole architecture was built for — one expert load serving many tokens instead of one — and it is measured and waiting ([#31](https://github.com/nibor1896/Crow/issues/31)). It needs agents before it can batch them, so it sits behind the loop above.

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
| `cli/` | the client and its seven tools, standard library only, 122 tests |
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
