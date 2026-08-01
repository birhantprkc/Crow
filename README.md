<p align="center">
  <img src="Crow.jpg" alt="Crow" width="420">
</p>

<h1 align="center">Crow</h1>

<p align="center">
  A local coding model in one specific domain, and the agent platform that feeds it.
</p>

---

## What this is

One product, two parts:

- **Part A — the model.** A locally running coding model that performs at frontier level *within a
  narrow domain* (Python, ONNX export, F5-TTS, Obsidian tooling, wakeword training), at a fraction
  of the running cost. Not a generalist, and not an attempt to rebuild a frontier model.
- **Part B — the platform.** GUI, agent and multi-agent orchestration at the scope of
  `NousResearch/hermes-agent`.

The two are one product because of the loop between them: the agent generates the verified training
examples while it works, and those examples improve the model that drives the agent.

## Current state: measurement phase

**Nothing is built yet, and that is deliberate.** Every step in this phase measures something; no
product code is written until the measurements say what to write. The rule the project runs on:

> Every expense needs a zero-cost measurement first that shows what it buys.

The order was set on 2026-08-02 (see [#3](https://github.com/nibor1896/Crow/issues/3)): the
**verifier comes first**, then both parts. It belongs to neither side — Part A needs it as the
yardstick for any claim of improvement, Part B needs it as the correctness signal that
`hermes-agent` does not supply on its own.

| Step | Question it answers |
|---|---|
| [#12](https://github.com/nibor1896/Crow/issues/12) | Can one runner produce a trustworthy pass/fail across five different test forms? |
| [#13](https://github.com/nibor1896/Crow/issues/13) | Is there a task set from real repositories that can show both colours? |
| [#14](https://github.com/nibor1896/Crow/issues/14) | At what level does a ready-made local model actually play, and how does it fail? |
| [#15](https://github.com/nibor1896/Crow/issues/15) | How many verified issue-to-commit pairs already exist? |

Full plan and dependencies: [project board](https://github.com/users/nibor1896/projects/7).

## Conventions

- Issues carry the knowledge. Every issue names its question, its first concrete move, the criterion
  that ends it, and the decision it gates.
- Every number carries its denominator. Anything unmeasured is marked as unmeasured, together with
  the one measurement that would settle it.
- No issue exists without a parent. The two roots are #1 (Part B) and #2 (Part A).

## Credits

`Crow.jpg` is a generated render; the prompt that produced it is kept alongside it in
`CrowJPG-Prompt.txt`.
