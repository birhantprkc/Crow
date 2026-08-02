<p align="center">
  <img src="Crow.jpg" alt="Crow" width="420">
</p>

<h1 align="center">Crow</h1>

<p align="center">
  A frontier coding LLM, made runnable on a single consumer GPU.
</p>

---

## What this is

**The model is not trained. It is made runnable.**

A frontier-scale mixture-of-experts coding model, streamed layer by layer, on a card most people
already own. Plus an agent platform on top of it, in the spirit of `NousResearch/hermes-agent` —
that one is an example for scope, not a specification.

Target model: **`deepseek-ai/DeepSeek-V4-Flash`**, MIT licensed. Per its model card: 284 B total,
13 B active per token, LiveCodeBench 91.7. Those are the vendor's numbers about the vendor's
harness, kept here as such. Not the largest model wins, but the sparsest one that still scores at
frontier level on code.

The file in use is `ggml-org/DeepSeek-V4-Flash-GGUF` → `DeepSeek-V4-Flash-MXFP4.gguf`,
**154,991,536,896 bytes**, `sha256:78a9a077…`. Read from its header on 2026-08-02: 1328 tensors,
52 KV pairs, `block_count 43`, `expert_count 256`, `expert_used_count 6`, `hash_layer_count 3`.

## The machine, and the constraint that actually binds

Measured on the development machine, 2026-08-02:

| Tier | Capacity | Bandwidth |
|---|---|---|
| VRAM | 32,607 MiB on the development card | ~1,800 GB/s |
| RAM | 63.4 GB DDR5-5600 | **~45 GB/s measured** |
| NVMe | 574 GB free, Phison 2 TB | **5.8 GB/s measured** sequential, single thread, 4 MB blocks |

**How much VRAM the target profile assumes is open** and is decided on
[#25](https://github.com/nibor1896/Crow/issues/25), on measurements rather than on a number carried
forward from a document. The figures below are the measured footprints of specific configurations,
not a specification.

**The binding constraint is RAM, not VRAM.** 63.4 GB is smaller than every usable quantisation of
this model — the smallest published one is `unsloth/UD-IQ1_S` at 82.5 GB, and the file in use is
155 GB. So the expert weights cannot live in RAM, and the NVMe is in the loop on every token.

Read from the tensor table, then confirmed against llama.cpp's own load accounting:

| | measured |
|---|---:|
| expert tensors per layer | 3 × 1088 MiB = **3,264 MiB** (all 256 experts) |
| per expert, per layer | 12.75 MiB |
| **expert weights read per token** | 6 active × 43 layers ≈ **3.21 GiB** |
| non-expert weights, GPU-resident | 6,917.82 MiB |
| KV cache | 8.06 KiB per token — 32.25 MiB at `-c 4096` |
| KV + compute buffers + CUDA context | 1,295.15 MiB at `-c 4096` |

That per-token figure sets the ceiling: entirely out of RAM at 45 GB/s it would be ~13 tok/s;
entirely off the NVMe at 5.3 GB/s, ~1.5 tok/s.

## What it actually does today

Measured 2026-08-02, `--temp 0 --seed 1234 -n 32 -c 4096 -np 1 -ngl 99`, batch 1, single
unrepeated runs. `-ncmoe N` keeps the experts of the first N of 43 blocks on the CPU.

| `-ncmoe` | expert layers on GPU | VRAM | generation |
|---|---|---|---|
| 999 | 0 | 9.5 GiB | 4.57 tok/s |
| **42** | 1 | **11.21 GiB** | **4.78 tok/s** |
| 37 | 6 | ~27.8 GiB | 2.87 tok/s |
| 36 | 7 — the most this card holds | 30.33 GiB | 2.76 tok/s |

**Filling the card with experts made it slower, by a factor of 1.7.** `--n-cpu-moe` moves the
computation and not only the weights, so mixed placement pays PCIe round-trips per layer per token
that the all-CPU configuration never pays. That reading is unverified; the numbers are not.

The consequence is worth stating plainly: **the fastest configuration measured here occupies
11.21 GiB, and spending the rest of a 32 GiB card made it slower.** Where VRAM could still pay is
KV cache and batching across parallel agent runs — and batch scaling has not been measured at all
([#38](https://github.com/nibor1896/Crow/issues/38)). Until it is, more VRAM has no measured
argument on this model.

## The upstream blocker, and what measuring it actually settled

`ggml-org/llama.cpp` [#25582](https://github.com/ggml-org/llama.cpp/issues/25582): `deepseek4`
produces garbage when MoE experts run on CUDA, correct only at `--n-cpu-moe 999`. That is exactly
the configuration this project needs, so it was measured before anything was built
([#33](https://github.com/nibor1896/Crow/issues/33)).

Across four placements plus a targeted `-ot` run that put the experts of blocks 34–42 — the
reporter's own failure region — on CUDA, **every output was character-identical to the all-CPU
reference**, including a 146-character continuation. A negative control asking for a different
capital held every time.

That is a narrower result than it first looks, and the difference matters:

- **Only one quantisation was tested.** Quant dependence is documented upstream on one machine and
  one build: `UD-IQ3_XXS` broken, `UD-Q2_K_XL` clean. MXFP4 is not one of the quants anyone has
  reported on, in either direction.
- **283 builds separate us from the report.** It was filed against b9940, this was measured on
  b10223, and nine CUDA commits sit in between. "Fixed in the meantime" fits the evidence as well as
  "does not occur here".
- **Short prompts only exercise the smallest kernel tile.** `mul_mat_id` dispatches by token count;
  for MXFP4 on sm_120 the MMVQ ceiling is 7 tokens, above that it is MMQ.
- **Character equality is a coarse detector.** At `--temp 0` identical text only means no difference
  was large enough to flip an argmax.

Nothing has been closed on the strength of it.

## Where the novelty actually sits

Stated plainly, because the opposite would be a claim we cannot hold: **three-tier expert caching is
not unprecedented.** llama.cpp RFC #20757 specifies it point for point; MoE-Infinity, FlashMoE and
others have published on the family. Its pull request was withdrawn, not refuted.

What remains genuinely open:

1. **Expert locality for this model is unpublished.** Not merely unmeasured by us — no source
   quantifies how often DeepSeek-V4-Flash reuses experts across consecutive tokens. And the evidence
   from other architectures splits: one coder model concentrates 80 % of hits in 28 % of its
   experts, while `gpt-oss-120B` routes so flatly that nearly doubling cache coverage bought 2.3 %.
   Which of the two this model is decides whether the whole caching lever is worth anything —
   [#23](https://github.com/nibor1896/Crow/issues/23).
2. **Nobody has run this file.** The MXFP4 build has a few hundred downloads and no published
   correctness or throughput result in either direction.
3. **A working implementation** where the existing one stalled.

## Current state

Measurement phase. No product code, deliberately — `tools/` holds measuring instruments and
nothing else. The rule the project runs on:

> Every expense needs a zero-cost measurement first that shows what it buys.

Every instrument here carries a case that has to fail. A probe that has only ever seen good input
cannot be told apart from one that checks nothing.

| Tool | What it establishes | Its failing case |
|---|---|---|
| `probe-89.bat` | whether `quantize.cu` compiles for `compute_89` | `compute_50`, dropped in CUDA 13, must be rejected |
| `gguf_header.py` | the download is intact, from the header, without hashing 155 GB | its failing case lives in `test_gguf_header.py`, next row |
| `test_gguf_header.py` | that the header checker can fail | truncated file, broken magic, wrong expectation |
| `vram-calibrate.bat` | that VRAM is the binding constraint | 43 expert layers on a 31 GiB card must OOM |
| `measure-vram.ps1` | what llama.cpp actually places, two instruments on one load | a `-ot` rule matching nothing must void the run — checked by destination, since `--n-cpu-moe` emits override lines too |
| `measure-loadmode.ps1` | whether bypassing the page cache changes the thrashing regime | an invalid `--load-mode` must be rejected before anything is measured |
| `probe-queue-depth.py` | random read rate against queue depth — the number the streaming direction rests on | a warm, RAM-resident file must NOT climb the same way; with `--direct`, it must not even read fast |
| `run-probes.ps1` | correctness against a self-made baseline | asks for a different capital, must not answer Paris |
| `sample-counters.ps1` | how much disk traffic a run causes through hard page faults — the denominator the 445 MB/s on [#30](https://github.com/nibor1896/Crow/issues/30) was borrowed for | three, and each covers a different silent failure: a threshold of `-1` must trip the idle gate, a dead PID must write no rows rather than zeros, and `-ExpectDisableValue 1` must trip the registry check that would otherwise be a green nobody tested |
| `wait_share.py` | what share of a run is spent on disk traffic and at what **queue depth** — throughput alone cannot tell a saturated drive from one that is merely asked for one block at a time ([#39](https://github.com/nibor1896/Crow/issues/39)) | pass the run's own CSV as the idle control: a baseline that does not separate from the run means the tool is reading noise, and every number above it is void |

### Open

| | Question |
|---|---|
| [#23](https://github.com/nibor1896/Crow/issues/23) | Does a coding workload keep hitting the same experts? |
| [#25](https://github.com/nibor1896/Crow/issues/25) | What VRAM and RAM may the target profile assume? |
| [#26](https://github.com/nibor1896/Crow/issues/26) | One yardstick: tokens/s **and** a quality gate |
| [#33](https://github.com/nibor1896/Crow/issues/33) | Does the upstream MoE fault affect us, on quants we have not tried? |
| [#38](https://github.com/nibor1896/Crow/issues/38) | Does throughput scale with batch size, and does VRAM then pay? |

Unmeasured and named as such: prefill throughput, anything above batch 1, any quantisation other
than MXFP4, longer contexts, and quality — no benchmark has been run, and a model answering two
capital-city questions is not a quality statement.

Full plan: [project board](https://github.com/users/nibor1896/projects/7).

## Conventions

- Issues carry the knowledge. Each names its question, its first concrete move, the criterion that
  ends it, and the decision it gates.
- Every number carries its denominator. Anything unmeasured is marked as unmeasured, together with
  the one measurement that would settle it.
- A number from a vendor's own model card is a statement about that vendor's harness plus the model,
  not about the model. It is labelled as such.
- A cited number needs its suite in the same commit, and that suite needs a case that fails.
- No issue without a parent. The two roots are #1 (platform) and #2 (model).
- Closed issues are not deleted. Fourteen of them record a project that was planned against a
  handover document which had narrowed the brief — that failure is worth keeping.

## Credits

`Crow.jpg` is a generated render; the prompt that produced it sits beside it in
`CrowJPG-Prompt.txt`.
