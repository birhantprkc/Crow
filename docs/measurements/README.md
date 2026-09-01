# Measurements

One user, `-np 1`, identical prompt, server restarted cold per arm, cross-checked against the
server's own `eval time` blocks.

## Placement and ubatch, Flash-Next (#182)

Full map: [`flash-next-placement.md`](flash-next-placement.md) — raw rows
[`flash-next-placement-runs.csv`](flash-next-placement-runs.jsonl), 27 runs.
Harness: `tools/measure/try-one.py` (one run, one config) and
`tools/measure/measure-ngram-spec.py` (interleaved series).

Measured 2026-09-01, 33,494-token prompt, 200 tokens out, one boot per run, cold
cache. Three runs per arm for the verified rows.

| | operating point | **`-ncmoe 30 -b 2048 -ub 2048`** |
|---|---|---|
| decode | 35.74 tok/s (35.13-36.11) | **41.76 (40.34-42.76)** |
| prefill | 539.98 tok/s | **727.65** |
| wall clock per turn | 67.9 s | **51.3 s** |

**+16.8 % decode, +34.8 % prefill, -24.4 % wall clock.** No overlap on decode.
Confirmed from both sides: `-ncmoe 32` is worse (59.6 s), `-ncmoe 28` is worse
(97.5 s), `-ub 1536` and `-ub 3072` are worse.

**Not applied to the manifest.** The operating point still ships
`-ncmoe 40 -b 4096 -ub 4096`.

The trap: `-ncmoe 24 -ub 1024` gives the highest decode measured (46.59, +34 %)
and is **2.5x slower per turn** — prefill falls 62 %. Decode alone is the wrong
number for an agent that re-prefills every round.

## Speculation

## Decode against context depth, qwen4exp (#159)

Raw rows: [`qwen4exp-depth-407.csv`](qwen4exp-depth-407.csv) — 407 requests, one
per `slot print_timing` triple, paired with the context depth from each request's
own `release` line.

One agent run on 2026-08-30/31, 5 h 52 m, no restart. Flash-Next `UD-Q2_K_XL`,
`-c 200000 -ncmoe 40 --fit off --load-mode none -np 1`, RTX 5090, driver 616.56,
llama.cpp pin `6c84c7d5d` + PR #27992.

| | |
|---|---|
| requests | 407 |
| decode | 424,465 tokens @ 25.01 tok/s |
| prefill | 327,878 tokens @ 415.91 tok/s |
| model time | 4 h 55 m, 95.6 % of it decode |
| deepest context | 183,274 tokens |

```
ms/token = 30.740 ms + 0.0898 ms per 1,000 context tokens
r = 0.848   r2 = 0.720   n = 407   depths 9,588 - 183,274
```

| depth | n | decode |
|---|---|---|
| 0-30k | 63 | 31.57 tok/s |
| 30-60k | 71 | 28.86 tok/s |
| 60-90k | 55 | 26.40 tok/s |
| 90-120k | 59 | 24.58 tok/s |
| 120-150k | 68 | 23.26 tok/s |
| 150-200k | 91 | 22.29 tok/s |

At 180k the depth term is 16.2 ms of a 46.9 ms token -- 34.5 %.

**25.01 tok/s is a cross-section over every depth from 10k to 183k.** It is not
comparable to the 32.44 tok/s of the #159 A/B, which is one cold 31,979-token
turn. Single arm: no control was run at these depths.

## Speculation

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

## `--spec-draft-n-max`

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

## Context

| context | decode, no MTP |
|---|---|
| 1,653 tokens | 74.09 tok/s |
| 35,984 tokens | 64.50 tok/s |

## Prefill

| block | tok/s |
|---|---|
| 34 tokens | 209.71 |
| 890 | 2,091.73 |
| 4,339 | 3,298.49 |

Prefill is a function of block size, not a constant.

## Verification

| check | result |
|---|---|
| tokens, client vs server | 6,591 = sum of 11 `eval time` blocks |
| decode, client vs server | 6,591 / 53.564 s = 123.05 tok/s |
| prefill, client vs server | 20,490 = sum of 11 `prompt eval` lines |
| suite | 1,298 of 1,298 |
| `check_shared_core` | 60 / 60 |
| `check_operating_point` | 6 / 6 |
| `install.ps1 -Selftest` | 85 / 85 |

## Not measured

| open | |
|---|---|
| VRAM floor | 16 GB is the installer's floor, never run |
| contexts past 36k under MTP | without MTP that span costs 13 % |
| distribution fidelity at `temperature 1.0` | one graded answer is a sample |
| what the background review costs | it holds the single slot; occupancy and queueing never timed |

---

## Hybrid offload (#140, Qwen3.8-Flash-Next)

125B-A6B, `UD-Q2_K_XL`, 73.45 GiB in 3 shards against a 32 GB card: experts of the first
`-ncmoe` layers in system RAM (DDR5-5600, 89.6 GB/s dual channel). Engine: llama.cpp
PR #27742, lab build 439 (`250b614`). One real turn per row over `/v1/chat/completions`,
the same 31,979-token prompt, cold cache, fresh server per row, no second llama-server;
server default `n_slots = 4, kv_unified` (`-np 1` measured: −1 %). Raw rows and a log
pair per run: `crow-lab/runs/2026-08-28-flash-next/`; the full story in four comments
on [#140](https://github.com/nibor1896/Crow/issues/140).

| line @ 200k ctx | prefill tok/s | decode tok/s | VRAM MiB |
|---|---|---|---|
| **`-ncmoe 40 --load-mode none -ctk/-ctv q8_0`** (10-boot series, driver 616.56) | **964.8** mean (950.0–981.0) | **28.61** mean (27.01–29.37) | 28,4xx |
| same, mmap instead of `--load-mode none` | 310.6 / 322.9 | 23.90 / 25.21 | 30,7xx |
| `-ncmoe 36` + q8 (the edge) | 981.0 once | 29.52 once | 31,865 — boots unreliably |
| `-ncmoe 24` (does not fit) | 98.5 / 32.2 | **2.70 / 3.40** — WDDM spill | 32,04x |
| PR head `eaf9376` (build 453), same champion line | 996.6 | 28.65 | 27,604 — fails warmup 11 of 19 |

`--load-mode none` is the finding that reorders the rest: mmap at the RAM ceiling
(63.03–63.19 of 63.38 GiB in every mmap row) evicts expert pages and the NVMe joins the
per-token read path — `tg_3s` dips 24 → 3.8 tok/s, identical configs spread 19–31 tok/s.
Anonymous memory pins them (~47 GiB used, 16 free); two runs of the same line differ by
0.16 % prefill / 0.1 % decode. The load pays once: ~68 s instead of ~13 s.

### The decode ceiling, probed

48 layers, 512 experts each, 10 active — at `-ncmoe 40` that is ~1.14 GiB of expert rows
per token from DDR5: naive ceiling ~78 tok/s, measured 28.6 ≈ 36 % of it. The clock sits
in the 40 GPU↔CPU transitions per token, not in bandwidth.

| lever | measured |
|---|---|
| threads | `-t 8` 21.21 · `-t 16` 27.12 · auto-24 **28.07** — the 285K's 16 E-cores carry real bandwidth |
| MTP | no `nextn` in any shard header (positive control: the same scan finds it in the 27B), no sidecar in the repo |
| ngram speculation | acceptance 0.306, net −2 % |
| draft-simple, 27B `UD-IQ1_S` drafter | acceptance **0.775** — and decode halves (24.98 → 14.07): a 27B drafts on the time scale the target verifies. Live again the day a small Qwen3.8 GGUF exists |
| KV `q8_0` | −0–3 % decode, +3.2 GiB VRAM margin |
| `--fit on` | 953.3 / 26.48 — does not beat manual placement |

Against the 27B operating point (123.05 / 2,262.96): decode ×0.23, prefill ×0.43 — the
trade is model size, and it is bought at the RAM interface, not the card.
