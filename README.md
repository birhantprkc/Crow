<p align="center">
  <img src="Crow.jpg" alt="Crow" width="420">
</p>

<h1 align="center">Crow</h1>

<p align="center">
  A frontier coding LLM running on 12 GB of VRAM.
</p>

---

## What this is

**The model is not trained. It is made runnable.**

A frontier-scale mixture-of-experts coding model, streamed layer by layer, on a card most people
already own. Plus an agent platform on top of it, in the spirit of `NousResearch/hermes-agent` —
that one is an example for scope, not a specification.

Target model: **`deepseek-ai/DeepSeek-V4-Flash`** — 284 B total, **13 B active per token**, 155 GB
at Q4, MIT licensed, LiveCodeBench 91.7 per its model card. Not the largest model wins, but the
sparsest one that still scores at frontier level on code.

## Why it is not obviously impossible

Measured on the development machine, 2026-08-02:

| Tier | Capacity | Bandwidth |
|---|---|---|
| VRAM | 32.6 GB dev — **12 GB target** | ~1,800 GB/s |
| RAM | 63.4 GB DDR5-5600 | **~45 GB/s measured** |
| NVMe | 905 GB free | **~5.3 GB/s measured** |

Per token only the *active* parameters are read: 13 B × ~0.55 bytes ≈ **7.1 GB**. Straight out of
RAM that is 6.3 tok/s. With a hot expert set resident in VRAM at a 90 % hit rate it is ~52 tok/s.

The whole project is the fight for that hit rate — and Amdahl is merciless: at 80 % the misses eat
91 % of the time. The target is 95 %.

## Where the novelty actually sits

Stated plainly, because the opposite would be a claim we cannot hold: **three-tier expert caching is
not unprecedented.** llama.cpp RFC #20757 (March 2026) specifies it point for point; MoE-Infinity,
FlashMoE, DALI, WiSP and ReMoE have published and measured the family. Its pull request was
withdrawn, not refuted — it failed on review capacity.

Two things are genuinely open:

1. **Nobody has quantified expert locality for a coding workload.** Domain specialisation in MoE is
   established; coding specifically is not, across five literature queries. That measurement is free
   and it is where this project starts — [#23](https://github.com/nibor1896/Crow/issues/23).
2. **A working implementation** where the existing one stalled.

## Current state: measurement phase

No product code yet, deliberately. Every step measures something, and nothing is built until the
measurements say what to build. The rule the project runs on:

> Every expense needs a zero-cost measurement first that shows what it buys.

| Next | Question |
|---|---|
| [#23](https://github.com/nibor1896/Crow/issues/23) | Does a coding workload keep hitting the same experts? |
| [#24](https://github.com/nibor1896/Crow/issues/24) | What do llama.cpp and ktransformers already reach here? |
| [#25](https://github.com/nibor1896/Crow/issues/25) | 12 GB VRAM — and how much RAM may we assume? |
| [#26](https://github.com/nibor1896/Crow/issues/26) | One yardstick: tokens/s **and** a quality gate |

Three levers are measured separately and compared on that one yardstick
([#32](https://github.com/nibor1896/Crow/issues/32)): fewer bytes per token, higher effective
bandwidth, amortisation across tokens. None is discarded in advance.

Full plan: [project board](https://github.com/users/nibor1896/projects/7).

## Conventions

- Issues carry the knowledge. Each names its question, its first concrete move, the criterion that
  ends it, and the decision it gates.
- Every number carries its denominator. Anything unmeasured is marked as unmeasured, together with
  the one measurement that would settle it.
- A number from a vendor's own model card is a statement about that vendor's harness plus the model,
  not about the model. It is labelled as such.
- No issue without a parent. The two roots are #1 (platform) and #2 (model).
- Closed issues are not deleted. Fourteen of them record a project that was planned against a
  handover document which had narrowed the brief — that failure is worth keeping.

## Credits

`Crow.jpg` is a generated render; the prompt that produced it sits beside it in
`CrowJPG-Prompt.txt`.
