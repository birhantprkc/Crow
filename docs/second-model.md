# The other models — DeepSeek-V4-Flash-0731

Not the operating point. Since 2.0.0 that is **Qwen3.8-Flash-Next** on `:8083`
([`../README.md`](../README.md)); **Qwen3.8-27B** on `:8082` is the second one and has its own
table there. This page is the third server `install.ps1` sets up, kept current because the
installer starts it.

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
layer, 10 active, a shared expert per layer. **No fork any more:** PR #27742
merged into mainline llama.cpp on 2026-08-29, and the line below runs that merge
commit, `6c84c7d5d`. The shipped binary still cannot load it -- that one is
`b10269` from 2026-08-06. License is `qwen-community-1.0`, not apache-2.0.

**The commit is pinned, not the tag.** `b10687`, one day younger, aborts during
warmup with `ggml_cuda_compute_forward: MUL_MAT failed`. The only `qwen4exp`
change between the two is #27880 "reduce number of graph splits", which hoists
the PLE embedding out of the layers into one shared split and then multiplies it
by per-layer weights -- and under `-ncmoe` those weights sit on the CPU. Measured
2026-08-30 under one variable: `6c84c7d5d` runs, `6fe749801` aborts. The pin moves
when that is fixed upstream, not when a newer tag appears.

Ten boots on the bare merge commit, one 31,979-token turn each on a cold cache:
**10/10 clean, prefill 959.81 tok/s mean (945.67-995.29), decode 28.60
(27.31-29.74), VRAM 27,988 MiB, load 71.8 s** -- inside the spreads of build 439
on both figures. Raw rows:
`crow-lab/runs/2026-08-30-merge-qwen4exp/boot-series.csv`.

**The pin carries one patch: PR #27992.** qwen4exp's PLE n-gram embedding called
`get_prev_tokens()` on every graph build, and that walked *every* used cell of the
KV cache testing up to 256 sequence bits -- once per decoded token, on the critical
path. The PR indexes `(seq, pos)` cells instead. The engine measured its own cost
under the PR's `verify` mode at this depth: **scan 4450.6 us against index 14.8 us**,
0 mismatches over 250 calls.

Measured 2026-08-30 on the shipped binary, three rounds **interleaved against a
same-session control**: **decode 32.44 tok/s mean (31.06-33.20) against 29.05, i.e.
+11.7 % with no overlap between the ranges; prefill 970.44 against 964.92, i.e.
flat.** Per-token saving 3.59 ms. Raw rows:
`crow-lab/runs/2026-08-30-levers-159/levers.csv`.

**The gain is proportional to context depth**, because the scan is `O(n_kv)`: about
3 % at the ten-task gate's few-hundred-token depth, +11.7 % at 31,979 tokens, larger
and unmeasured at the 200k window. Correctness twice: the PR's own unit test (9,480
lookups, 0 failures) and 0 live mismatches; the ten-task gate is 10/10 twice with
token counts byte-identical to the control, so the change cannot move what the model
writes.

**It is a draft PR** whose author notes it charges every other architecture a little
for qwen4exp's benefit. If it is rejected upstream, drop the patch and the line falls
back to the bare pin above.

*Interleaved on purpose.* This machine drifted -5.0 % prefill and -5.5 % decode
within one day -- larger than the effect and enough to flip its sign. On 2026-08-30
a control from another session would have produced the wrong verdict three times
over. No arm is compared against a control from another session.

    C:\Users\robin\dev\crow-lab\wt-27992\build-27992\bin\Release\llama-server.exe `
      -m <models>\qwen-next-gguf\UD-Q2_K_XL\Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf `
      --port 8083 -c 200000 -b 2048 -ub 2048 `
      -ctk q8_0 -ctv q8_0 -ncmoe 30 `
      --fit off --load-mode none -np 1 `
      --mmproj <models>\qwen-next-gguf\mmproj-F16.gguf `
      --jinja

No env prelude: `CUDA_CACHE_DISABLE=1` stood here for three hours on
2026-08-28 night and was measured WORSE -- without the driver cache every
fresh kernel shape hits the driver JIT, and this machine's JIT is what rolls
CUDA 303. The corrupt cache that killed boots that evening was set aside
(`ComputeCache.korrupt-2026-08-28`); a fresh cache boots and serves without
any flag. The manifest's `_env_history` carries the numbers.

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
