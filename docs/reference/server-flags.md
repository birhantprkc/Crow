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
card. Every value below is the measured optimum of the 2026-08-28 sweep, not a guess.

| flag | value | why |
|---|---|---|
| `-ncmoe` | `40` | experts of the first 40 of 48 layers live in system RAM. Fewer rides the VRAM edge: at ≥ ~31.9 GiB used the prefill collapses 941 → 135–250 tok/s, and `-ncmoe 24` boots into WDDM spill at 2.70 tok/s |
| `--load-mode` | `none` | mmap at the RAM ceiling reads the NVMe into every token — identical lines spread 19–31 tok/s on page-cache luck. Anonymous memory pins the experts: two runs differ by 0.1 % |
| `--fit` | `off` | the manifest's placement is authoritative. The auto-fitter measured 953/26.5 and does not beat it |
| `-b` / `-ub` | `4096` | the measured prefill batch. `-ub 2048` frees 6.3 GiB of VRAM and wins nothing |
| `-ctk` / `-ctv` | `q8_0` | same speed, 3.2 GiB more VRAM margin than f16 |
| `binary` (manifest key, not a flag) | lab build 439 | arch `qwen4exp` exists only in llama.cpp PR #27742; the shipped binary cannot load it. The newer PR head is 3–6 % faster and fails its own warmup 11 of 19 times — measured, not chosen |

964.8 tok/s prefill / 28.61 decode over a 10-boot series — conditions and the levers that
do **not** pay (MTP, ngram, a 27B drafter) in [measurements](../measurements/README.md).
