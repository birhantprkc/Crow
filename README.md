<div align="center">

<h1>CROW</h1>

<h3>A 304-billion-parameter coding model, at a 200k context. One graphics card, from 16 GB VRAM. Measured on 64 GB of system RAM.</h3>

<p>Frontier mixture-of-experts inference, with the experts streamed off the SSD.<br>No cluster. No 200 GB host. No cloud.</p>

<p>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square&logo=opensourceinitiative&logoColor=white&labelColor=000000" alt="License"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/version-0.3.0-brightgreen?style=flat-square&logo=semver&logoColor=white&labelColor=000000" alt="Version"></a>
<a href="#requirements"><img src="https://img.shields.io/badge/platform-Windows%20x64%20%C2%B7%20CUDA-555555?style=flat-square&logo=nvidia&logoColor=76b900&labelColor=000000" alt="Platform"></a>
<a href="cli/crow.py"><img src="https://img.shields.io/badge/client-Python%20stdlib%20only-555555?style=flat-square&logo=python&logoColor=ffd43b&labelColor=000000" alt="Python"></a>
<a href="https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731"><img src="https://img.shields.io/badge/model-DeepSeek--V4--Flash--0731-orange?style=flat-square&logo=huggingface&logoColor=ffd21e&labelColor=000000" alt="Model"></a>
</p>

<table>
<tr>
<td align="center"><b>304B</b><br><sub>parameters</sub></td>
<td align="center"><b>13.3B</b><br><sub>active per token</sub></td>
<td align="center"><b>200k</b><br><sub>context, one slot</sub></td>
<td align="center"><b>84.6 GiB</b><br><sub>model on disk</sub></td>
<td align="center"><b>17.70 GiB</b><br><sub>expert cache in VRAM</sub></td>
<td align="center"><b>19.53</b><br><sub>tok/s decode, gate median of 3 runs</sub></td>
<td align="center"><b>133.10</b><br><sub>tok/s cold prefill, 1,884-token prompt</sub></td>
<td align="center"><b>0 EUR</b><br><sub>spent so far</sub></td>
</tr>
</table>

<br>

</div>

<br>

> **The operating point this release ships is `DeepSeek-V4-Flash-0731` at `UD-IQ2_XXS`,
> `--moe-stream-cache 58s`, on an RTX 5090 (32,607 MiB) with 63.4 GB of DDR5.** Everything about
> the model itself — file size, expert share, slab and cache size, resident tensors — is read from
> the tensor tables of all three shards and confirmed against the server's own printed line. The
> throughput and quality figures on this page were measured on 2026-08-12 (#89): three graded runs
> of the ten-task gate per rung, own server per run, one variable changed.
>
> **Two sets of figures on this page were measured on the previous rung, `UD-IQ3_XXS`, and have NOT
> been repeated here — they are marked where they appear:** the slot ladder (56/58/60/62/64) and the
> host-tier pairing that produces the **1.63x**. They describe the mechanism, not this file, and the
> mechanism did not change; the numbers would. 58 slots is carried over unchanged for the same
> reason it was chosen: 0731 covers 95 % of selections in 9.0 % of its experts, so at 22.7 % resident
> the cache is already saturated and more slots buy nothing measurable.
>
> 16 GB of VRAM is the installer's floor, not a second measured point: below 32 GB expect fewer
> cache slots, and at 200k about 1.4 GiB of that budget goes to KV instead — a trade that was never
> run (#25). Nothing below 63.4 GB of system RAM has been run either. Raw protocols are under
> `runs/2026-08-12/` and `runs/2026-08-11/`.

## What this is, in one paragraph

**Crow runs a frontier-scale coding model on a single consumer graphics card by leaving most of the model on the SSD.**

A mixture-of-experts model is mostly asleep. Every token wakes only **6 of the 256 experts** in each of its 43 layers, and the routed experts that can be asleep are 78.11 of the file's 84.62 GiB — 92.3 %. Crow keeps the parts that *every* token needs in VRAM — attention, norms, shared experts, **6,378.40 MiB on CUDA0 plus 284.06 MiB of host buffers = 6.51 GiB, 7.69 % of the file** — holds the **58** most useful experts per layer beside them in a slot cache of **18,121.38 MiB = 17.70 GiB**, and reads whatever is missing straight off the drive while the GPU is still working.

The context window is 200,000 tokens, on a single slot, and it costs **1,353.50 MiB = 1.32 GiB** of the card — 32.25 + 1029.00 + 35.00 + 257.25 MiB of KV buffers at `n_ctx = 200192`, so 6.92 KiB per token. Compressed attention makes context the cheap part here. A coding session holds files and history, so a 16k or 64k window would be measuring a product nobody uses.

**The host's spare RAM can be spent to make the misses cheaper.** A machine with 64 GB has tens of gigabytes doing nothing. `--moe-stream-l2 32` keeps expert weights there between the VRAM slots and the drive, so a miss that finds its expert in host memory never reaches the drive at all. Over three paired runs the measured cost of a miss falls from **1.331–1.383 ms without the tier to 0.713–0.722 ms with it**, and decode goes from **11.04 to 18.03 tok/s** at the median arm — a factor of **1.63** *(measured on the previous rung, `UD-IQ3_XXS`, not repeated on this one)*. With the tier on, this rung measures **0.6470 ms per miss** (2026-08-12). The price is 32 GiB of page-locked memory. The flag defaults to off; the installer puts it into the command it prints on any machine that has the RAM — [what it buys and what it costs](#4-the-host-ram-tier-optional).

That is the whole idea. Everything below is what it costs to make it actually run.

---

![Where every byte lives, what crosses between VRAM and the drive, and what it costs per token](docs/images/architecture.svg)

[Part II](#part-ii-how-it-works) builds every box in that diagram from the measurements that produced it.

---

## Contents

**[Part I: Getting started](#part-i-getting-started)**
&nbsp;&nbsp;[Requirements](#requirements) · [Quick start](#quick-start) · [Full setup](#full-setup) · [Using the CLI](#using-the-cli) · [Using the window](#using-the-window) · [Updating](#updating) · [Common questions](#common-questions)

**[Part II: How it works](#part-ii-how-it-works)**
&nbsp;&nbsp;[The problem](#the-problem-a-model-that-does-not-fit) · [Sparsity](#1-sparsity-most-of-the-model-is-asleep) · [The cache](#2-the-slot-cache-and-what-vram-buys) · [Reading the drive](#3-reading-the-drive-without-the-page-cache) · [The host tier](#4-the-host-ram-tier-optional) · [Cost per token](#what-it-costs-per-token)

**[Licence](#licence)** · **[Credits](#credits)**

---

# Part I: Getting started

## Requirements

| | |
|---|---|
| **GPU** | NVIDIA, **16 GB VRAM minimum**, 32 GB for the measured operating point. Below 16 GB was never measured and is unsupported |
| **System RAM** | **64 GB for the operating point**, which spends 32 GiB on the [host tier](#4-the-host-ram-tier-optional). 32 GB runs without it, at **1.63x less throughput** *(measured on the previous rung, `UD-IQ3_XXS`, not repeated on this one)* |
| **Disk** | ~2 GB for Crow, **84.6 GiB for the model** — 90,860,736,928 B across three files, measured on the finished download |
| **OS** | Windows x64. The streaming path uses `FILE_FLAG_NO_BUFFERING` and a handle pool, both Windows-specific |
| **Python** | 3.8+, for the clients. **The terminal client needs the standard library and nothing else** |
| **WebView2** | For the window (`cli/crow_gui.py`) only. Ships with Windows 11 and with every Edge install; measured here as `151.0.4129.78`. The terminal client never touches it |
| **pywebview** | For the window only, ~2 MB. **The installer runs `pip install pywebview` for you** — this is the one dependency Crow does not carry itself, and it is the price of a window that renders the design instead of approximating it |

The installer looks at all of this **before** it downloads anything, but only two of the rows can stop it: fewer than 16,000 MB of VRAM, and less than 2 GB free on the install drive. System RAM, room for the model, a missing Python and a missing WebView2 runtime are **reported as warnings and the install continues** — the table is the measured profile, not a gate.

All five of those warnings are raised in the preflight, which is the step before the download. That was not always true of the Python row: until 0.2.0 it was asked in the last step, so a machine without Python heard *"the client needs it"* after fetching 506 MB. It is the same sentence either way and only one of the two placings charges half a gigabyte for it.

## Quick start

```powershell
irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1 | iex
```

Five steps, no elevation, everything under `%LOCALAPPDATA%\Crow`:

```console
  Crow 0.2.0

[1/5] Checking this machine
      GPU  NVIDIA GeForce RTX 5090, 32607 MB
      RAM  63.4 GB
      Disk  364.3 GB free on C:
      Windows  64-bit, PowerShell 5.1.26100.8875
      Python  C:\Users\you\AppData\Local\Programs\Python\Python313\python.exe
      WebView2  151.0.4129.78, the window renders in it
      preflight  passed

[2/5] Downloading the package
      from  https://github.com/nibor1896/Crow/releases/download/v0.2.0/crow-0.2.0-win-x64.zip
      crow-0.2.0-win-x64.zip  [####################.....]  84%  <done> / <total>  18.2 MB/s
      size  <the release's, compared against the manifest>
      sha256  <the release's, compared against the manifest>

[3/5] Installing to C:\Users\you\AppData\Local\Crow
        <n>/<n>  README.md
      <n> files extracted
      installed  <n> files

[4/5] Verifying what landed
      sha256 per file  <n> of <n> match
      nothing to remove  <n> files the previous package installed are all still shipped

[5/5] What is left to do
```

**The count, the size and the hash are all placeholders above, and each is one for its own reason.**
The hash has always been one, and that reason is circular: this file sits inside the archive it
would be quoting, so printing the real digest here changes the archive and invalidates the digest.
The count and the size became placeholders for a duller reason — nothing in this repository checks
them. They were true of 0.2.0 as it shipped (26 files, 506.4 MB in `dist/crow-0.2.0-win-x64.zip`,
measured on the package) and the next one that ships a file more makes them quietly false, in the
one document a reviewer reads. That next one is this one: 0.3.0 carries two clients and a shared
core, so the same archive packs more files than the sentence above ever named — which is the
argument happening to its own example. A number that no check can defend either moves into `tools/check_operating_point.py`
beside the flags and the version literal, or it comes out of the README; letting it go stale is the
one option that is not available. This one came out. What is on your screen is compared against the
release's own manifest by the installer, file by file and hash by hash, which is where those numbers
belong.

*Steps 3 and 4 used to be printed here the other way round — "Verifying" before "Installing". The
installer has always unpacked first and hashed what landed afterwards, which is the only order in
which the hashes mean anything; the sample above was simply wrong about it. Read off a real run,
2026-08-13.*

The model is **not** part of that download. It is 84.6 GiB, it belongs to somebody else, and an installer that spends hours on it before you have seen anything work is the wrong shape. The last step prints the one command that fetches it:

```powershell
hf download unsloth/DeepSeek-V4-Flash-0731-GGUF --include "UD-IQ2_XXS/*" --local-dir $env:LOCALAPPDATA\Crow\models
```

> **A trap worth knowing about.** When `hf` cannot reach the repository it prints `✓ Downloaded` and returns the local directory — failure that looks exactly like success. Check that four files totalling ~97 GiB actually arrived.

> **`%LOCALAPPDATA%` is a `cmd.exe` form and PowerShell does not expand it.** Every block on this page is PowerShell, so it writes `$env:LOCALAPPDATA`. The installer prints every one of these commands with the paths already resolved for your machine — `install.ps1:1474` for the server, `:1505` and `:1526` for the two clients — and that is the copy to prefer.

## Full setup

### Step 1 — start the server

```powershell
$env:LOCALAPPDATA\Crow\bin\llama-server.exe `
  -m $env:LOCALAPPDATA\Crow\models\UD-IQ2_XXS\DeepSeek-V4-Flash-0731-UD-IQ2_XXS-00001-of-00003.gguf `
  --port 8081 -c 200000 -ngl 99 -np 1 --jinja `
  --slot-save-path $env:LOCALAPPDATA\Crow\session `
  --chat-template-file $env:LOCALAPPDATA\Crow\templates\0731-chat-template.jinja `
  --moe-stream --moe-stream-cache 58s --moe-stream-io-threads 8 --moe-stream-direct `
  --moe-stream-l2 32
```

The installer prints this line filled in for your machine. It leaves `--moe-stream-l2` out below 60 GB of detected RAM — Windows reports 63.4 on a 64 GB machine, so a threshold at the nominal size would exclude exactly the configuration this was measured on.

Every flag carries a reason, and none of them is taste:

| Flag | Why |
|---|---|
| `-c 200000` | A coding session holds files and history. 16k or 64k measures a product nobody uses. 200k fits on one slot: **30,548 MiB of 32,607 after load**, three runs reading the same value |
| `--port 8081` | Not a preference. `llama-server` defaults to 8080 and the client defaults to 8081, so leaving it out gives a server the client cannot find — and on Windows 8080 is often already taken |
| `-np 1` | One user, one stream. `-np 4` splits the context into 4 × 50k and is the harness case, not the CLI |
| `--jinja` | Use the **model's** chat template instead of llama.cpp's built-in one. Without it the client's replayed reasoning is dropped and the prompt cache breaks on every turn |
| `--moe-stream` | Route expert tensors through the slot cache instead of placing them |
| `--moe-stream-cache 58s` | 58 of 256 experts per layer, **18,121.38 MiB = 17.70 GiB**. Not 64: six slots fewer is the difference between a stable machine and one where single requests halve at random — see [the ceiling](#the-cache-has-a-ceiling-and-it-is-the-card) below |
| `--moe-stream-io-threads 8` | I/O workers. **With `--moe-stream-direct` each of them reads through its own file handle**; without it there is no handle pool and every worker goes through the one shared handle, which Windows serialises (`src/llama-mmap.cpp:266-277`, fallback at `:457`). The two flags are one setting with two names |
| `--moe-stream-direct` | Unbuffered reads, and the thing that opens the handle pool at all. Without it `read_raw_at` falls back to the shared handle and the pool is gone |
| `--slot-save-path` | Where the server writes its KV state so a session survives a restart. Without it the next start re-prefills the whole history. Must be an existing directory or the server refuses to start |
| `--chat-template-file` | 0731 ships no Jinja template, and the one embedded in the GGUF fails the model's own golden vector 4 (an action turn opens a think block it never closes). The shipped file renders all four vectors byte-identically; verified in jinja2 and against a live server |
| `--moe-stream-l2 32` | Optional [host-RAM tier](#4-the-host-ram-tier-optional), in GiB. Three pairs on 2026-08-11: **1.65x / 1.63x / 1.63x per pair, 1.63x median against median** (18.03 against 11.04 tok/s). The price is 32 GiB of page-locked memory |
| *no `-lv 5`* | Not a flag but the absence of one, and it is worth as much as any flag here. See below |

<a id="a-diagnostic-flag-costs-a-factor-of-fourteen"></a>
**Verbosity into a file is free; verbosity onto a console is not.** The debug log is about 40 lines
per token and every CUDA graph launch waits for them to be written, prefill and decode alike, with
the card at full clock and a fraction of its power limit — which is what a GPU waiting on its host
looks like. So a diagnostic run redirects stderr into a file: the six gate runs behind every figure
on this page do, which is why they carry `-lv 5` and still measure the operating point. Append
`2> server.err` to the start line above and the flag costs nothing worth measuring; leave the log
on the console and the number you take is the console's, not the model's. *(The size of that
penalty is a single observation with no run written under `runs/`, so no factor is claimed here —
only the direction, and the redirect that removes it.)*

### Step 2 — check that the right server is running

Two read-only queries, both in seconds:

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:8081/props').default_generation_settings.n_ctx
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
```

Expect `n_ctx = 200192` (llama.cpp rounds up) and about **30,548 of 32,607 MiB**.

**This second reading is not a formality, it is the one that decides your throughput.** What is left
of the card has to cover everything your display does. If your reading is much higher than 30,548,
something else is holding VRAM; if the remaining headroom goes to nothing, the driver starts moving
the expert cache into host memory and single requests halve at random —
see [the ceiling](#the-cache-has-a-ceiling-and-it-is-the-card). A second `llama-server` on the same
card is the same failure in its loudest form, and nothing in the server's own log says so.

If `n_ctx` says 65536 or 16384, a measurement server is running and not the operating point.

**Every figure on this page comes from a fresh server.** The one long-running server on record fell
from ~15 to ~1 tok/s after 121 minutes and recovered only on a restart; it was not the context — a
fresh server at a resumed 63.9k window ran at 14.97. That report has never been re-run at the
shipped 58 slots. Open as [#71](https://github.com/nibor1896/Crow/issues/71); the remedy is a
restart, and there is no fix to claim.

### Step 3 — start a client, and there are two of them

**Crow ships two clients.** `cli/crow.py` is the terminal client and the one this README documents
end to end; `cli/crow_gui.py` is a window over the same core, the same session file and the same
server. Neither is a wrapper around the other, and neither is required to use the other.

The installer puts **both** on disk and starts neither. There is no opt-in switch and no "GUI
edition": `tools/pack-release.ps1` copies `cli/` into the package whole, so both files are in the
download either way, and a switch could only have decided whether the installer mentions the second
one. What it does instead is print both start lines on its last screen, the terminal one first.

The terminal client, which needs nothing but Python:

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
   v0.3.0


crow at http://127.0.0.1:8081/v1 (health: ok, 200k context)
DeepSeek-V4-Flash-0731
```

The last line is not the `-m` you passed — that is `crow`, at the front of the line above it. The
model name is read back out of the server's `/props`, so an endpoint serving something else says so
directly under its own address.

Or the window, which needs `pywebview` as well — the installer puts it there, and what the window
shows and where it differs is [its own section](#using-the-window):

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow_gui.py
```

**One thing is worth knowing before you open both at once.** The server runs at `-np 1`: one slot,
one stream. Two clients against one server is fine and the session file is shared between them, but
a turn started in one holds slot 0 until it finishes, and a question asked in the other queues
behind it. Closing the window mid-turn does not release it either — the server keeps computing —
and `crow_gui.py` says so on the way out rather than leaving you to wonder why the terminal is
suddenly slow.

## Using the CLI

Standard library only, on purpose: it has to run before anything is installed. Everything in this
section is `cli/crow.py`; the window is [below](#using-the-window) and shares this client's flags
where they mean the same thing.

| Command | |
|---|---|
| `/help` | the commands |
| `/tools` | the tools the model can call, read out of the schema it is sent |
| `/thoughts` | show the model's reasoning as it arrives, or hide it again — the same switch as `--show-reasoning`, mid-session |
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
| `--reasoning-effort` | `low`, `high` or `max`. Rides in `chat_template_kwargs` and only when set; unset, the template picks `low` itself |
| `--show-reasoning` | print the reasoning as it streams, in its own dim block. Off by default: it is 60–90 % of every answer this model gives — 88.2 % of every generated character over the 2026-08-07 reference run — and shown by default it buries the code you asked for. The block closes when the answer starts and **opens again** if the model thinks a second time mid-answer, which it does |
| `--timeout` | socket timeout in seconds (default **1800**) |
| `--rollover-at` | archive and start fresh at this share of the window, `0` switches it off (default **0.9**) |
| `--max-tool-rounds` | how many tool rounds one turn may take before it answers from what it has (default **24**) |
| `--no-run-tools` | report the tool calls the model asks for instead of running them; the turn ends after one round. Not the same as `--max-tool-rounds 0`, which still spends the budget and buys a forced round to say where things stood. The declarations stay in the request either way — emptying them is what makes the model's template drop the previous turn's thoughts |
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

Each of those three came from watching it fail. Driven live on 2026-08-10, the
first version pointed only at the JSON — which `json.dump` writes as **one
104,618-byte line**, so `read_file`'s 16 KB cap could only ever show the first
15 % of it, cut mid-field, from the oldest end. And it said nothing about where
the work had got to. The model guessed a directory that does not exist, scanned
a whole user profile twice, and spent **402 s across seven tool rounds** before
it read the archive at all. Hence the transcript, the line count, and
`Last worked on:`.

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

<a id="the-context-is-append-only-and-carries-its-reasoning"></a>
**The context is append-only, and every assistant turn carries its reasoning.** Nothing is ever inserted in front of, or edited inside, an existing message, because the prompt cache only survives while the prefix stays byte-identical.

The second half of that was the harder lesson. The client used to drop `reasoning_content` from the history — it is display-only, so why send it back. But this model's template renders a kept turn as `<think>…</think>`, and an omitted field leaves an *empty* think block: the prefix then diverges exactly where the thoughts began, and everything behind that point is re-read. The cost therefore has nothing to do with how much the model thought — the whole answer sits behind the thoughts, so the whole answer is re-prefilled.

This is also why the client sends its `tools` array with **every** request — for the cache as much as for the tools. The template keeps a past turn's thoughts only while tools are present; with an empty array both variants render byte for byte the same, and the prefix diverges.

<a id="what-a-fresh-turn-sends"></a>
**What that costs on the wire: 909 of 953 tokens.** Measured 2026-08-10 through the server's own `/apply-template` and `/tokenize`, a first turn with a five-token message sends **953 tokens** — 5 of them the message, 39 the system prompt, and **909, or 95.4 %, the seven tool declarations**. They ride on every request by design. It is also why a fresh turn's prefill is not free even when the conversation is empty.

**The output streams, and the raven shows the state.** A client that renders only `content` shows a blank screen for most of the wait, because most of what this model emits is `reasoning_content`. Crow shows `thinking`, then flips to `writing code` at the first content token, and the turn's timing line prints the actual `thinking %` for that turn. The share is a setting rather than a property of the model — `--reasoning-effort low|high|max` moves it.

**`--temperature` defaults to 1.0, not 0.** 1.0 is what DeepSeek-V4-Flash-0731 specifies — its model card runs its agentic benchmarks there and its `generation_config.json` agrees. 0 stays dangerous: pure greedy decoding has no way out of a repetition attractor, and a model that repeats *"Actually, let me…"* inside its reasoning block never reaches an answer. `--temperature 0` stays available so measurement runs get byte-identical output.

On first start the client installs its bundled typeface and writes `profiles.defaults.font.face` and `background` into Windows Terminal's `settings.json`, with a `.bak` beside it. It never overwrites a value it did not write itself. Both halves can be switched off.

## Using the window

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow_gui.py
```

`cli/crow_gui.py` is the second surface, and the rule it is written to is narrow: **every decision
is the core's, every pixel is the window's.** Where a thought block begins, what counts as a code
fence, what one turn cost, how a session is saved and resumed — all of that is `cli/crow_core.py`,
the same module `cli/crow.py` calls. The window draws it and decides none of it, which is why
switching clients cannot change an answer. `tools/check_shared_core.py` holds that against
`manifests/shared-core.json` and now has two surfaces to hold it against instead of one.

What is on the screen, and each of these is the same behaviour the terminal has:

| | |
|---|---|
| **the status bar** | the connection, the model read back out of `/props`, and how much of the window is used. The model chip starts **empty** and stays empty until `/props` answers — never a default, never the last known name |
| **the transcript** | reasoning in its own dim block that closes when the answer starts and opens again if the model thinks a second time mid-answer, then the answer, then the turn's cost line |
| **code blocks** | drawn in a frame with a copy button. An unclosed block is still framed and still copyable — the model does not always finish its fence |
| **the composer** | ENTER sends, SHIFT+ENTER makes a new line. The read timeout is printed beside the send button, read off the running configuration rather than typed in |
| **the sessions rail** | one entry, because there is one `session.json`. It is the same file `cli/crow.py` writes, so a conversation started in the terminal opens in the window and back again |

**The abort is a button that tells the truth, which is rarer than a button that works.** ESCAPE or
the same button that sent the turn stops it, and there are three paths under it: the interrupt flag
the core polls every 50 ms, the socket its `finally` closes, and the socket timeout. The third one is
the reason the window ships **600 s** where the terminal ships 1800: measured 2026-08-13 by
`tools/measure_gui_stream.py`, *nothing this process does wakes a `recv` that is already blocked* —
`settimeout`, `shutdown` and closing the socket each came back only when the server itself hung up.
The only bound on such a reader is the timeout **as it stood when the read started**, so it has to be
chosen in front of the turn. 600 s clears the worst prefill on record (469.51 s to the first token on
a resumed 21k session) with room, and is a bound where 1800 is effectively none. If the reader is
still alive two seconds after an abort, the window writes that into the transcript instead of
pretending the turn ended — during a prefill silence that will happen, and it is true when it does.

**The drawing is batched per frame, not per token.** One `after()` tick at 30 fps takes up to 512
events off the queue and writes them with one insert per tag run. Measured over 4,000 deltas: per
event, 4,000 ticks and 332.9 ms of inserts; per tick, 1 tick and 4.8 ms. The milliseconds are not the
point — the ticks are, because a tick costs wall clock whether it drew one character or a thousand.

The flags are the terminal client's wherever they mean the same thing (`--base-url`, `-m`,
`--system`, `--temperature`, `--top-p`, `--min-p`, `--reasoning-effort`, `--rollover-at`,
`--max-tool-rounds`, `--no-run-tools`, `--no-session`), and `--timeout` is the one that differs, for
the reason above. What the window does **not** have: the `/` commands — it has buttons instead — and
the terminal-profile writing, which belongs to the client that lives in a terminal.

## Updating

**The client tells you.** On start it asks GitHub whether a newer release exists and, if there is one, prints it above the prompt together with the command that installs it:

```
crow 0.3.1 is out (you have 0.3.0)
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

**Does it need the model in RAM?** No. The 84.6 GiB file is never held in host memory. What the host holds is the optional tier — 32 GiB of page-locked memory when `--moe-stream-l2 32` is on, and nothing when it is off.

**What does `--moe-stream-l2` do?** It keeps expert weights in page-locked host RAM between the VRAM slots and the drive. Three pairs on 2026-08-11: decode **18.03 against 11.04 tok/s** at the median arm, 1.65x / 1.63x / 1.63x per pair, and the stall per miss falls from 1.331–1.383 ms to 0.713–0.722 ms. The price is 32 GiB of memory the rest of the machine cannot use. The installer prints the flag above 60 GB of detected RAM, because 32 GiB on ~64 GB is the only ratio that has been run. [Details](#4-the-host-ram-tier-optional).

**Does the host tier change the answers?** Not at this resolution: **6 of 6 graded tasks correct with the tier and 6 of 6 without**, across the three pairs. Six tasks resolve a difference of about two, so this is *no difference found* rather than a proof of equivalence.

**Why Windows only?** The streaming path rests on `FILE_FLAG_NO_BUFFERING`, positional `OVERLAPPED` reads and a per-worker handle pool, because Windows serialises I/O on the file object. The POSIX side of the primitive exists and compiles; it has never been run.

**Why not just buy more VRAM?** The model is 84.6 GiB and no consumer card holds it, so streaming is not a workaround for a small card — it is the only shape that runs at all. And more cache is not free either: past a point it stops fitting beside what the display needs, which is [the ceiling](#the-cache-has-a-ceiling-and-it-is-the-card).

---

# Part II: How it works

## The problem: a model that does not fit

DeepSeek-V4-Flash-0731 is **304,180,418,494 parameters** — counted from the safetensors headers of
all 48 shards, and the figure HuggingFace's own API reports. Resident at bf16, that is:

![Every parameter resident at bf16](docs/images/eq_naive_memory.png)

608 GB against 34.2 GB of VRAM and 63.4 GB of system RAM. Nothing about placement or scheduling closes a gap of eighteen times.

## 1. Sparsity: most of the model is asleep

A dense model reads every weight for every token. A mixture-of-experts model does not:

![Six of 256 experts fire per layer](docs/images/eq_sparsity.png)

43 layers, 256 experts each, 6 of them selected per token. The rest of that layer contributes nothing to this token and does not need to be anywhere near the GPU.

That leaves a set which *is* needed by every token — attention, norms, embeddings, the shared expert:

![The always-active set](docs/images/eq_resident_set.png)

**6,378.40 MiB on CUDA0 plus 284.06 MiB of CUDA_Host buffers = 6.51 GiB, 7.69 % of the 84.62 GiB file.** The routed experts are the other **90.17 GiB** — 378,208,256 B per expert across all 43 layers, times 256 experts. It is GPU-resident by construction at `-ngl 99`; nothing had to be built for it. The remaining 92.9 % is the streaming problem.

Routing is concentrated enough for a cache to be worth anything, and the server prints it itself — per layer, then averaged over the 43. Across the three cumulative blocks of the `r1-l2` arm, the share of experts needed to cover 80 % of selections reads **23.0 %, then 24.6 %, then 25.7 %**; for 50 % it is 7.7 → 8.5 → 8.7 %, and for 95 % it is 43.0 → 45.4 → 48.4 %, at Gini **0.744 → 0.725 → 0.711**. Every one of those widens with run length: the distribution flattens as more experts have been touched at all. It does not flatten enough to make the cache pointless, and a single block quoted alone understates it.

## 2. The slot cache, and what VRAM buys

Each streamed layer gets a fixed number of expert slots in VRAM. A hit means the weights are already there; a miss means fetching them while the compute thread waits. At the shipped 58 slots the cache is **20,919.88 MiB = 20.43 GiB**, and the server's own hit rate over a graded arm is **80.10 – 81.81 %**.

<a id="the-cache-has-a-ceiling-and-it-is-the-card"></a>
**The cache has a ceiling, and it is the card itself.** A slot costs **312.44 MiB** — the server
prints the cache size at every step and the figure is constant to five digits across all of them.
More slots is more throughput right up to the edge, and then the allocation stops fitting beside
what the display needs, the driver moves the difference into host memory without printing anything,
and the cost is not a constant slowdown but an unpredictable one:


> **These figures were measured on `UD-IQ3_XXS`, the rung before this one, and have not been
> repeated on `UD-IQ2_XXS`.** The mechanism they describe is unchanged; the numbers are not this
> file's. On the shipped rung a slot costs 312.44 MiB rather than 360.69, so every cache size in
> the table below is about 13.4 % smaller and the VRAM column correspondingly lower — how the
> ladder's shape moves with that has not been run (#89).

| cache | cache size | prefill, 1,374 tok | decode, 200 tok | VRAM used | **free** |
|---|---:|---:|---:|---:|---:|
| 64 slots | 23,084.00 MiB | **15.28** median of 8, spread **8.69x** | not measured | 32,014 | 593 |
| 62 slots | 22,362.62 MiB | 114.92 median of 5 | 14.62 median of 4, **one at 7.07** | 31,683 | 924 |
| 60 slots | 21,641.25 MiB | 113.53 median of 5 | 17.43 median of 8 | 30,954 | 1,653 |
| **58 slots** | **20,919.88 MiB** | **112.69 median of 3** | **17.32 median of 8** | **30,548** | **2,059** |
| 56 slots | 20,198.50 MiB | 110.30 median of 3 | 17.09 median of 8 | 29,842 | 2,765 |

Measured 2026-08-11, fresh server per run, `runs/2026-08-11/`. Read the VRAM column down and the
arithmetic closes: 62 slots read 31,683 MiB and 60 read 30,954 — **729 MiB for two slots**, against
the 721 MiB the printed slot size predicts. **64 must therefore read about 32,404, and it reads
32,014. The 390 missing MiB are the ones the driver moved out**, and they are the whole defect. At
64 slots the same 1,374-token prompt ran between 3.83 and 33.29 tok/s across eight runs; at 58 the
three runs span 111.38 to 112.82.

Two cautions this table carries on its face. The prefill and decode columns are not one series —
58 and 56 were taken in one session and 62 and 60 in another, and a VRAM reading always contains
whatever else the machine had on the card at that moment. And **58 against 60 is not a throughput
difference**: 112.69 against 113.53 on prefill and 17.32 against 17.43 on decode, while repeating
the *same* configuration eight times spans 1.09x. What separates them is the margin, not the rate.

**58 is this card's number, not a constant.** It comes from 32,607 MiB of VRAM and whatever the
display is doing beside it. A smaller card, or a busier display, needs a smaller value; deriving it
from the machine the way `--moe-stream-l2` already is, is tracked as
[#87](https://github.com/nibor1896/Crow/issues/87) rather than guessed at.

**The failure leaves no trace in any counter.** A halved request executes the same graphs, takes the
same misses, and can have the *lowest* load stall of its run. Identical work, double the wall clock,
nothing in the streaming path to blame — which is exactly what a silent host-memory migration looks
like.

A second question the cache answers only partly: **cold misses cannot be cached away.** They are the
working set every layer must touch once, and no cache size removes them. On the `r1-l2` arm's
**warm-up** task, against an empty cache, they are **7,988 of 25,678 misses, 31.1 %**. The first
graded task behind it — the difference of two cumulative blocks, which is the only honest way to
read one — takes **487 cold of 7,517, 6.5 %**, and by the end of the arm the running figure is
**9,435 of 98,769, 9.6 %**. The share collapses once the cache is warm, because evictions accumulate
and first touches do not. Growing the cache removes evictions, never first touches.

The cache also has a hard floor that the graph, not the option, imposes:

![The wave cap](docs/images/eq_wave_cap.png)

Multi-pass expert GEMMs need at least `3 x n_expert_used` slots. Upstream's default computed `2 x n_expert_used` clamped to 16, which is smaller than the 18 the graph requires. That did not fail at load: the `GGML_ABORT` sat inside `build_moe_ffn` and fired on the **first batch that touched more experts than the cache holds** — model loaded, request running, and far from the option that caused it (`src/llama-model.cpp:1338-1343`). Crow refuses at the option instead, at load time, with the number named.

**What a wave is.** A single token touches at most 6 experts per layer, so it always fits. A
*batch* does not: a ubatch of 512 tokens can select more experts in one layer than the 58 slots
hold, and there is no ordering of the loads that makes them all resident at once. So the graph
splits the touched experts of that layer into **waves** — passes of at most `plan_capacity`
experts each, run one at a time, with the experts of the wave that is not running masked out
(`src/llama-moe-stream.h:102-111`). Prefill is where this happens; decode at `-np 1` is one wave.

The part that matters for throughput is what happens *during* a wave: while wave *w* computes, the
slabs of wave *w+1* are already being fetched (`stage_wave_locked`, `:420-422`). The server counts
whether that paid off, and prints it per model instance:

```
moe stream: waves = 430 (228 non-empty), preloads issued = 4303 (ready on arrival = 474), wave stall = 4032.35 ms
```

That line is from `r1-l2.err`, after the first graded task: 474 of 4,303 preloads had arrived by the
time their wave ran, and 4.03 s were spent waiting for the rest. It is a cumulative counter like
every other one on this page — see [below](#the-statistics-lines-are-cumulative).

## 3. Reading the drive without the page cache

**Page faults are the wrong instrument.** A fault is synchronous and per-thread: a thread touches a missing page, blocks, one read is issued, it resumes. That path leaves the drive at a queue depth near one, on hardware that only reaches its rated speed when asked for many reads at once.

**Windows serialises on the file object, not the file pointer.** Sharing one handle across threads keeps the drive at depth 1 however many workers there are; giving each worker its own handle does not. The read mechanism — `SetFilePointerEx` against positional `OVERLAPPED` — makes no measurable difference; sharing the handle makes all of it. So `llama_file` opens **18 private handles** and the I/O workers each read through their own. At the operating point only 8 of the 18 are ever used, because `--moe-stream-io-threads 8` is where the drive saturates. The other ten are headroom with a purpose: a worker whose id is at or past the pool falls back to the shared handle and takes the serialisation with it, which is the one failure the pool exists to prevent (`src/llama-mmap.cpp:95-107`).

**One work item per weight tensor, not per expert.** An expert carries 2–3 weight tensors — this page also calls one of them a **slab**, and it is the unit everything below is counted in: one weight tensor of one expert in one layer, 43 × 256 × 3 = 33,024 of them in the file. Reading an expert's slabs in a loop keeps one request in flight at a time; issuing them independently keeps the drive busy.

That change also broke reproducibility once, and the fix is worth naming: several workers began calling `ggml_backend_tensor_set` concurrently, which the single-worker loop never did, and one task started producing two different programs across three runs. A mutex held **only** around the upload restores it; the disk read stays outside the lock, which is where the gain lives.

This work produced a fix that went upstream on its own: `llama_file` on Windows had no positional unbuffered read at all, and `has_direct_io()` returned a hard `true` on a path that had never opened anything unbuffered — so external code logged "O_DIRECT, page cache bypassed" while reading through the page cache. Submitted as [ggml-org/llama.cpp#26541](https://github.com/ggml-org/llama.cpp/issues/26541) and [#26542](https://github.com/ggml-org/llama.cpp/pull/26542).

## 4. The host-RAM tier (optional)

Everything above spends VRAM and drive bandwidth. The third resource on the machine was sitting
idle, and a 64 GB box has room. `--moe-stream-l2 32` puts a second cache level there, between the
VRAM slots and the drive.

### What 0.3.0 measures

**Measured on `UD-IQ3_XXS`, the rung before this one, and not repeated on `UD-IQ2_XXS`.** With the
tier on, the shipped rung measures 0.6470 ms per miss (#89); what the pairing looks like there is
unrun.

Three pairs on 2026-08-11, `runs/2026-08-11/slot58-pairs`. Each arm is a fresh server at
`--moe-stream-cache 58s`; both arms of a pair solve the same tasks; the pairs solve different ones.

| | with `--moe-stream-l2 32` | without |
|---|---:|---:|
| decode, three arms | 18.93 / 17.33 / **18.03** tok/s | 11.44 / 10.63 / **11.04** |
| median against median | **18.03** | **11.04** — a factor of **1.63** |
| per pair | 1.65x / 1.63x / 1.63x | |
| stall per miss | 0.713 / 0.722 / 0.713 ms | 1.363 / 1.383 / 1.331 |
| decode spent waiting on a miss | 65.2 / 67.4 / 70.0 % | 78.6 / 79.3 / 81.0 % |
| expert cache hit rate | 81.81 / 80.10 / 80.33 % | 80.99 / 80.06 / 79.73 % |
| tier hit rate, in slabs | 39.96 / 36.14 / 33.33 % | — |
| graded tasks correct | 6 of 6 | 6 of 6 |

Every row reads pair 1 / pair 2 / pair 3, in that order.

**The denominators, because the headline is small.** The median tier arm is `r3-l2`: **3 answers,
837 decoded tokens, 46,434.23 ms**. The median base arm is `r3-base`: 3 answers, 1,139 tokens,
103,196.15 ms. Three answers per arm, six per pair, eighteen in the whole series. The three answers
include the arm's **ungraded warm-up task** — `tools/run-l2-pairs.ps1` sums every `eval time` line
in the log, which is the honest reading of "what this server did", not "what it scored".

**Within-arm spread is 1.09x with the tier and 1.08x without, against an effect of 1.63x.** That is
the reason the difference can be read at all: the band each arm occupies is narrower than the gap
between the arms. It is still three arms a side.

**Cold prefill on `UD-IQ3_XXS` at 58 slots: 112.69 tok/s** — the rung before this one, kept because
the method is the point. On `UD-IQ2_XXS` the same cold start measures **133.10 tok/s**, on a
1,884-token prompt rather than 1,374, so the two are not one series (#89). Over three runs at 1,374 tokens, spread
1.013x, fresh server each (`runs/2026-08-11/slot58-prefill/`). This is a first start on an empty
expert cache, which is the case a user actually meets. **It is a harness prompt** — the generator
repeats a short word list, which routes to far fewer distinct experts than real text, so it is the
upper end of what a cold start does, not the middle.

**What it costs in VRAM.** 327,614,463 B per expert across the 43 layers, so the 58-slot cache is
18,121.38 MiB. Measured at 200k on one slot: **30,548 MiB of 32,607 after load**, leaving
**2,059 MiB**. Everything your display does has to fit in that remainder, which is what the section
above is about.

### The arrangement, and why it is shaped this way

Two simpler arrangements produce nothing. All ten gate tasks repeated per run means the second run
meets the cache the first warmed, and with 32 GiB of experts held that shared state *is* the subject.
Giving each arm different tasks removes the carry-over and replaces it with arms solving differently
hard problems. What works is both at once: the same tasks within a pair, fresh tasks across pairs,
each arm on its own server so the tier starts empty on both sides. A warm-up task precedes each
graded pass, on a prompt the graded pass does not use — without it the first graded task pays for
the cold model, the cold VRAM slots and the empty tier at once.

At 32 GiB the tier holds **7,695 slots of 4,464,640 B** — one slot takes the largest slab in the
model plus its direct-I/O alignment slack, so any slab fits any slot and the allocator cannot
fragment (`src/llama-moe-stream.cpp:263`). Two numbers follow from that, and they are not the same
number:

- **7,695 of the file's 33,024 slabs: 23.3 %.** That is a share of the *count* of slabs, and it is
  the tier's **capacity**, not how full it is — at a 33–40 % hit rate most of what a token wants is
  still elsewhere.
- **The payload that capacity can hold is about 21.0 GiB, in a 32.00 GiB allocation.** The average
  slab in this model is 327,614,463 B / 129 slabs per expert = 2,539,647 B, while every slot is
  4,464,640 B wide. 7,695 × 2,931,847 B = 22,560,562,665 B = **21.0 GiB of weights** inside an
  allocation of 7,695 × 4,464,640 B = 32.00 GiB; the remaining **~11.0 GiB is stride slack**, the
  price of an allocator that cannot fragment. "32 GiB" is what the flag takes from the machine, not
  what it stores.

**Filling it is free, and that is what makes it worth building.** The worker already read every
missing slab into a staging buffer and threw the buffer away. Now the read lands directly in a tier
slot and the upload sources from there: the same read, the same bytes, kept instead of discarded. A
design that copied into the tier afterwards would spend more per fill than a later hit returns.

![The host tier against no tier, paired on identical tasks](docs/images/host_tier.png)

**What it costs.** 32 GiB of page-locked memory, held for the life of the process and unavailable
to everything else. That is why the flag is off by default and why the installer prints it only
above 60 GB of detected RAM: 32 GiB on a ~64 GB machine is the only ratio that has been run.

**What it cannot do.** Catch a cold first touch. Those bytes have never been read, so no cache holds
them — 9,435 of the `r1-l2` arm's 98,769 misses. Everything else is an eviction, and only those are
addressable here.

**A defect worth recording, because no throughput number would have shown it.** The first version
handed out a resident slot and released its lock; another worker took the same slot as an eviction
victim and read a different expert into it mid-upload. The model emitted 8,191 characters of
`<<<<<<<<` instead of an answer — at a fast rate by every counter that existed. A cache race is
invisible in throughput. The fix pins a slot while it is being read and commits a filled one only
after its bytes have left for the GPU.

The obvious suspect for the slowdown that followed was measured before anything was rebuilt: the
tier's global mutex costs **0.539 µs per operation, 457.20 ms over 848,297 operations** in the
`r1-l2` arm — a fraction of a percent of wall time. Sharding it would have been wasted work.

## What it costs per token

![Bytes per token](docs/images/eq_bytes_per_token.png)

And the honest headline about where a request's time actually goes:

![Wait share](docs/images/eq_wait_share.png)

**65.2, 67.4 and 70.0 % of decode is the thread waiting on a miss** — the three tier arms at the
operating point — against **78.6, 79.3 and 81.0 %** on the same tasks without the tier
(`runs/2026-08-11/slot58-pairs/l2-pairs.csv`, `load stall` over summed `eval time`). The tier does
not remove the wait, it halves what each miss costs. This is not a compute-bound system and it is
not a bandwidth-bound one. It is a latency-bound one, and every lever above acts on that.

<a id="the-statistics-lines-are-cumulative"></a>
**Every statistics line the server prints is cumulative, and nothing resets them.** `load stall`,
`slot wait`, `wave stall` and `L2 lock wait` — and the hit, miss and cold-miss counts beside them —
run over the whole life of the model instance, i.e. over the server process
(`src/llama-moe-stream.cpp:746-751`, `tools/server/server-context.cpp:686-690`). A request-local
figure is the **difference between two consecutive blocks**, which the reader can form and the
writer cannot. It is visible in one file: cold misses in the `r1-l2` arm read 7,988, then 8,475,
then 9,435 across the three blocks of a single run. Every stall and hit-rate figure on this page
that names an arm is the arm's last block, warm-up task included — read a single block as one
request and it will be wrong by everything before it.

Context is nearly free by comparison — 1,353.50 MiB of KV buffers for `n_ctx = 200192`,
6.92 KiB per token:

![A 200k context costs 1.32 GiB](docs/images/eq_kv_cost.png)

---

## Licence

MIT, see [`LICENSE`](LICENSE). Four components carry terms this project cannot grant, and [`NOTICE`](NOTICE) says which: `ggml-org/llama.cpp` (MIT, other copyright holders), `deepseek-ai/DeepSeek-V4-Flash` (MIT, fetched rather than shipped), the NVIDIA CUDA Toolkit the CUDA backend is built against, and Google Sans Code under the SIL Open Font License 1.1.

## Credits

Measured on one machine: RTX 5090 (32,607 MiB), 63.4 GB DDR5, 24 threads, one Phison NVMe. **Spent so far: 0 EUR** — no rented compute, no API calls.

<div align="center">
<br>
<a href="https://ko-fi.com/nibor1896"><img src="https://img.shields.io/badge/support%20this%20on-ko--fi-ff5e5b?style=for-the-badge" alt="Ko-fi"></a>
</div>
