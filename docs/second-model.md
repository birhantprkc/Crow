# Second model — DeepSeek-V4-Flash-0731

Not the operating point. [`../README.md`](../README.md) is Qwen3.8-27B; this page is the other
server `install.ps1` sets up, kept current because the installer starts it.

| | |
|---|---|
| Model | `DeepSeek-V4-Flash-0731`, `UD-IQ2_XXS` |
| Architecture | MoE, 304B total, 13.3B active |
| Experts | streamed off the SSD, not resident |
| Port | 8081 |
| Context | `-c 200000`, one slot (`-np 1`) |
| KV | f16 |
| Template | `manifests/0731-chat-template.jinja` — the GGUF's embedded one fails its own golden vector 4 |
| Source of truth | [`../manifests/operating-point.json`](../manifests/operating-point.json), key `operating-point` |

---

## Model

```powershell
hf download unsloth/DeepSeek-V4-Flash-0731-GGUF --include "*UD-IQ2_XXS*" --local-dir $env:LOCALAPPDATA\Crow\models\0731-gguf
```

## Server

```powershell
$env:LOCALAPPDATA\Crow\bin\llama-server.exe `
  -m $env:LOCALAPPDATA\Crow\models\0731-gguf\UD-IQ2_XXS\DeepSeek-V4-Flash-0731-UD-IQ2_XXS-00001-of-00003.gguf `
  --port 8081 -c 200000 -ngl 99 -np 1 --jinja `
  --slot-save-path $env:LOCALAPPDATA\Crow\session `
  --chat-template-file $env:LOCALAPPDATA\Crow\manifests\0731-chat-template.jinja `
  --moe-stream --moe-stream-cache 58s --moe-stream-io-threads 8 --moe-stream-direct `
  --moe-stream-l2 32
```

## Client

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow.py
```

The terminal client defaults to `:8081`. The window reads the port off the running process.

---

## Flags this model needs and Qwen does not

| flag | value | why |
|---|---|---|
| `--moe-stream` | on | routes expert tensors through a slot cache. Qwen has no expert tensors |
| `--moe-stream-cache` | `58s` | 58 slots. Measured: 18.03 tok/s against 11.04 at the earlier value |
| `--moe-stream-io-threads` | `8` | |
| `--moe-stream-direct` | on | |
| `--moe-stream-l2` | `32` | computed by `install.ps1` from detected RAM; the manifest records this machine's value |
| `--chat-template-file` | path | the embedded template fails golden vector 4 |
| `--spec-type` | absent | its speculation path needs a separate draft model and costs 6.06 % |

## Reasoning levels

| rows offered | collapses |
|---|---|
| `low` (default), `max` | `off`, `low`, `high` all render the same prompt |

## Numbers

| | |
|---|---|
| decode, 1,653 tokens of context | 74.09 tok/s |
| decode, 35,984 tokens | 64.50 tok/s |
| expert cache at 58 slots vs the earlier value | 18.03 vs 11.04 tok/s |
| speculation | not used — 6.06 % cost, separate draft model |

## Not measured

| open | |
|---|---|
| this model under `--spec-type` | never run to completion |
| the host RAM tier's effect | flag present, contribution unseparated |

## Third model: Qwen3.8-Flash-Next (#140)

73.45 GiB in 3 shards, `UD-Q2_K_XL`, arch `qwen4exp` — 48 layers, 512 experts per
layer, 10 active, a shared expert per layer. Runs ONLY on the PR #27742 engine
(lab build 439, `250b614`); the shipped binary cannot load the architecture, and
the newer PR head `eaf9376` fails its own warmup 11 of 19 times. License is
`qwen-community-1.0`, not apache-2.0.

    C:\Users\robin\dev\crow-lab\wt-qwen-next\build-qn\bin\Release\llama-server.exe `
      -m <models>\qwen-next-gguf\UD-Q2_K_XL\Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf `
      --port 8083 -c 200000 -b 4096 -ub 4096 `
      -ctk q8_0 -ctv q8_0 -ncmoe 40 `
      --fit off --load-mode none -np 1 `
      --jinja

## Numbers (2026-08-28, build 439, driver 616.56, 10-boot series)

| | |
|---|---|
| prefill, 31,979 tokens cold | 964.8 tok/s mean of 10 (949.99–981.03) |
| decode | 28.61 tok/s mean of 10 (27.01–29.37) |
| VRAM after the turn | 28.4 GiB, 4.2 free |
| RAM | ~46.6 of 63.38 GiB |
| boots | 10/10 |

`--load-mode none` is the row that matters: mmap at the RAM ceiling reads the
NVMe into every token — identical lines spread 19–31 tok/s on page-cache luck
until the CPU experts sit in anonymous memory. Speculation buys nothing here:
the GGUF ships no MTP head, ngram nets −2 %, and a 27B drafter halves decode at
0.775 acceptance. Details: the three measurement comments on #140.
