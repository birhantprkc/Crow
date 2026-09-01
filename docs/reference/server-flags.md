## Server flags

| flag | value | why |
|---|---|---|
| `-c` | `200000` | a coding agent holds files and history. Rollover cuts at 0.9 of the window |
| `-np` | `1` | one user. `-np 4` splits the context four ways |
| `-ctk` / `-ctv` | `q8_0` | f16 left 332.8 MiB of headroom; q8_0 leaves 6,627 |
| `-ngl` | `99` | 16.35 GiB fits on the card whole |
| `--jinja` | on | without it llama.cpp uses its built-in template and the reasoning replay is dropped |
| `--slot-save-path` | `<install>\session` | the server refuses to start against a path that does not exist |
| `--mmproj` | `models\qwen38-gguf\mmproj-F16.gguf` | the vision projector (#142). Without it the same GGUF is a text model and `/props` says `vision: false`. Costs 1,124 MiB VRAM, leaves text prefill unchanged. One image is `(w/32)*(h/32)` tokens after resize, capped at 4,096 (`--image-max-tokens` moves the cap) |
| `--spec-type` | `draft-mtp` | the model's own MTP head. 1.85x decode, measured |
| `--spec-draft-n-max` | `3` (default) | measured, see below |

## Third model (Qwen3.8-Flash-Next, #140)

The same server, a different placement problem: 73.45 GiB of weights against 32 GiB of
card. Every value below is the measured optimum of the 2026-08-28 sweep, not a guess -- with `-ncmoe` and `-b`/`-ub` re-measured TOGETHER on 2026-09-01 (#182), which moved both.

| flag | value | why |
|---|---|---|
| `-ncmoe` | `30` | experts of the first 30 of 48 layers live in system RAM, the other 18 keep theirs on the card. MEASURED 2026-09-01 (#182) TOGETHER WITH `-b`/`-ub`, and that pairing is the whole point: the ubatch buffer holds VRAM the expert layers can carry, so neither value means anything measured against the wrong state of the other. Fenced from both sides at `-ub 2048`: 32 → 59.6 s, 28 → 97.5 s of wall clock per turn. Below 24 it spills into WDDM and prefill falls to ~27 tok/s — that floor is the expert weights themselves (24 layers on the GPU are ~22 GiB), and neither a smaller ubatch nor a q4_0 KV cache moves it |
| `--load-mode` | `none` | mmap at the RAM ceiling reads the NVMe into every token — identical lines spread 19–31 tok/s on page-cache luck. Anonymous memory pins the experts: two runs differ by 0.1 % |
| `--fit` | `off` | the manifest's placement is authoritative. The auto-fitter measured 953/26.5 and does not beat it |
| `-b` / `-ub` | `2048` | at 4096 only 8 of 48 expert layers fit on the card, at 2048 it is 18, and every layer that moves takes 10 × 1.7883 MiB = 17.9 MiB per token off the RAM bus and puts it on VRAM at ~20× the bandwidth. CORRECTED 2026-09-01 (#182): this row read “`-ub 2048` frees 6.3 GiB of VRAM and wins nothing”, which was measured at `-ncmoe 40` where VRAM was not the limit — true on its own and false next to the other half. Fenced from both sides at `-ncmoe 30`: 1536 → 55.5 s, 3072 → 98.8 s per turn |
| `-ctk` / `-ctv` | `q8_0` | same speed, 3.2 GiB more VRAM margin than f16 |
| `binary` (manifest key, not a flag) | lab build 439 | arch `qwen4exp` exists only in llama.cpp PR #27742; the shipped binary cannot load it. The newer PR head is 3–6 % faster and fails its own warmup 11 of 19 times — measured, not chosen |
| `--mmproj` | `models\qwen-next-gguf\mmproj-F16.gguf` | the vision projector (#170). The same repository ships it in its ROOT, so a download filtered to the quant folder misses it. **Loading is what the sources support, not answering:** llama.cpp issue #27886 is open with three reports of missing image content. Cost on this line is unmeasured — the model sits at the card edge under `-ncmoe 30`, closer than under the 40 this note was written at, and the headroom there is itself unmeasured (`tools/measure-vram.ps1` answers it in one boot) |

964.8 tok/s prefill / 28.61 decode over a 10-boot series, at the placement this page carried until 2026-09-01. At the current one, over a 33,494-token cold turn: **727.65 prefill / 41.76 decode, 51.3 s of wall clock per turn** (3 interleaved runs per arm, #182). DIFFERENT PROMPTS — the two pairs are not comparable value to value, only each against its own control. Conditions and the levers that
do **not** pay (MTP, ngram, a 27B drafter) in [measurements](../measurements/README.md).
