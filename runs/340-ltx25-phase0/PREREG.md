# PREREG — #340 Phase 0: LTX-2.5 distilled int8, image-to-video, ComfyUI headless

Written 2026-10-02T18:45+0200, before ComfyUI was ever started on this machine and before any clip exists.
Ticket: https://github.com/nibor1896/Crow/issues/340 (body *Expected result*; runtime decision comment
https://github.com/nibor1896/Crow/issues/340#issuecomment-5950774928; file status
https://github.com/nibor1896/Crow/issues/340#issuecomment-5958674703).
Changes to this file are allowed only as a dated addendum at the end, written before the result they judge.

## Acceptance criteria (verbatim from the ticket, *Expected result*)

1. 5 s clip at ≥1920x1080: completes 5/5 without OOM, median wall time ≤ 6 min, no gray frames.
2. 10 s clip at ≥1920x1080: completes 5/5 without OOM, median ≤ 12 min.
3. robin rates identity held in ≥4/5 clips and motion present in ≥4/5.
4. 20 s: either 3/3 completed (single pass or chunked) or documented as not supported with the reason; not a gate.
5. Higher rungs (2560x1408, 3840x2176): measured only if 1-3 pass; reported, not gated.
If LTX-2.5 fails 3 on identity, LTX-2.3 (same runtime, same pipeline) is measured as the control arm before anything else is changed.

Amendment already in force before this PREREG (decision comment, 2026-10-02): one arm only (ComfyUI);
the "arms alternated" and "on a tie, sd.cpp" parts are void. sd.cpp stays the fallback if ComfyUI fails 1 or 2.

## How the criteria are read (fixed now, not after the result)

- **Criterion 3** is evaluated separately for the 5 s set and for the 10 s set, and holds only if it holds for both
  (≥4/5 identity AND ≥4/5 motion in each set). The 20 s clips are rated and reported, not gated.
- **Blind rating** with one arm: all 10 gated clips (plus the 20 s clips) are presented to robin in a shuffled
  order under neutral names (`clip-01.mp4` …), each next to its input still; the key (name → still, length, seed)
  is written to `rating-key.json` before the sheet is shown and opened only after all ratings are in. Per clip robin
  answers two yes/no questions: *identity held* (the clip still looks like the still) and *motion present* (not
  frozen, not zoom-only).
- **Gray frame**, objective part: a frame of the output MP4 whose luma standard deviation is < 6.0 (0–255 scale)
  is flagged. Any flagged frame, or any gray/corrupt frame robin sees during rating, fails criterion 1 for that clip.
- **OOM / failure**: a clip that ends in a ComfyUI execution error (incl. `OutOfMemoryError` / "Allocation on device"),
  that crashes the server, or that runs longer than 30 min (then `/interrupt`) counts as not completed. A failure is a
  result row, not a reason to stop: the series continues with the next clip after a server restart.
- **Wall time** = from `POST /prompt` until `/history/{id}` reports the prompt complete (MP4 written). Every clip
  starts after `POST /free {"unload_models": true, "free_memory": true}`, so each wall time includes loading the
  models into VRAM (from the OS file cache or disk). This matches the planned use, where the LLM ran in between.
  The first (smoke) clip additionally contains the cold load from disk and is therefore not counted.
- Median and range over n=5 per length. No re-runs of individual clips to replace a bad value.

## Runtime and settings

- Hardware: RTX 5090 32,607 MiB, 63.4 GiB host RAM, Windows 11. GPU baseline before start: 1,726 MiB used
  (desktop only), no `llama-server`, no `sd-server`, no Crow server running. One engine at a time; Crow's Image Stack
  stays off for the whole series.
- ComfyUI v0.38.0 portable (`6b747c0`), python 3.13.14, torch 2.14.0+cu130, launched as `run_nvidia_gpu.bat` does
  (`python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build`) plus `--disable-auto-launch`; no
  attention or VRAM flags. Server stdout/stderr go to `server.log` in this folder.
- Workflow: official template `video_ltx2_5_i2v.json` from `comfyui_workflow_templates_json` 0.1.96, unchanged except:
  - `TextGenerateLTX2Prompt` bypassed (the motion prompt is written by hand; `gemma4_e2b_it_int8_convrot` is not
    installed). The template's own "Enable Prompt Enhance" input is already `False`.
  - `ResolutionSelector` megapixels 0.9 → **2.0** (16:9, multiple 32 → **1920x1088**, the template's own table);
    stage 1 runs at 960x544, x2 latent upsampler to 1920x1088. 1920x1080 = crop 8 rows at delivery, not in the run.
  - Duration 5 or 10 (or 20) s at 24 fps → 121 / 241 / 481 frames (template formula `a*b+1`).
  - Stage-1 noise seed per still (table below, `fixed`); stage-2 seed stays the template's `42`.
  - Motion prompt per still (table below); the template's negative prompt is kept.
  - Load First Frame = the still (the template resizes the longer side to 1536 px, Lanczos, CRF-18 preprocess).
  - Everything else as shipped: int8-convrot transformer and Gemma TE, DiffVAE `ltx-2.5-video-vae-bf16`, audio VAE,
    8 + 3 distilled sigmas, `euler_ancestral`, CFG 1/1, image strength 1.0 / 0.7, `VAEDecodeTiled` 512/64/64/16.
  The exported API-format prompt is saved as `workflow-api-<still>-<seconds>s.json` per clip with its sha256 in
  `results.jsonl`.

## Inputs — 5 Qwen-Image 2.1 stills, 2752x1536 (16:9), copied to `inputs/`

| Still | Role (ticket) | sha256 | Bytes | Source (`Desktop\ct\images\`) | Seed |
|---|---|---|---|---|---|
| A | portrait with a face | `41f315f6b1e1d3a09ec89520b0ce04b6ac67282659179e941b746476ca3142be` | 8,990,433 | `20260930-222844-turn-this-girl-into-a-friendly-colorful.png` | 3400001 |
| B | landscape | `c3eeede311567ade173d11c6b5e268a25ce032c9bb7d1ee7d8a3655e53389e53` | 11,586,363 | `20260930-230651-breathtaking-1990s-style-anime-key.png` | 3400002 |
| C | small figures | `f3be7b88e64a2d30869592c9e522a6924da355ebbdf0e678bb33238aafdca233` | 8,633,047 | `20260930-212745-tilt-shift-macro-photograph-of-a-tiny.png` | 3400003 |
| D | photoreal scene | `b8faf0a07c55a964a121a1258523f56eda8e36b33d3b57c7f7500121667e1f1e` | 9,655,237 | `20260930-111341-a-mechanical-raven-perched-on-a-rain.png` | 3400004 |
| E | full figure + animal | `347949c4344c4da5ebe88e1d5321e7caebb6f27dd252d1f46778311d5449aa5f` | 11,056,279 | `20261002-084445-ultra-high-resolution-wide-16-9.png` | 3400005 |

## Motion prompts (same prompt and seed for 5 s, 10 s and 20 s)

- **A:** Use the provided start image as the first frame. The black-haired girl keeps pointing at the laptop screen and laughs, her shoulders bouncing slightly; the black crow beside her tilts its head twice, blinks and flaps one wing. The camera holds still at table height with a gentle slow push in. Soft indoor light. No text, no black frames.
- **B:** Use the provided start image as the first frame. The girl in the yellow raincoat lets the paper plane go; it glides away towards the whale-shaped cloud, which drifts slowly to the right while the clouds around it move. Wind ripples across the flooded rice fields and her hood flutters. The camera pans slowly to the right following the plane. No text, no black frames.
- **C:** Use the provided start image as the first frame. Inside the open suitcase the tiny market comes alive: the miniature people walk between the stalls, steam rises from the food stands and the string lights flicker warmly. The camera slowly dollies from left to right along the market, the tilt-shift focus staying on the stalls. No text, no black frames.
- **D:** Use the provided start image as the first frame. Rain falls steadily through the neon alley; the mechanical raven turns its head to the left, its blue eye glows brighter, and it opens and folds its metal wings once. Ripples spread through the puddles and the neon sign reflections shimmer. The camera stays static. No text, no black frames.
- **E:** Use the provided start image as the first frame. The white-haired woman in black turns her head slowly towards the crow on the grass; the crow hops two steps closer and caws. Wind moves through the tall grass, her hair and the ribbon, and the clouds drift across the golden evening sky. The camera holds still with a very slow push in. No text, no black frames.

## Metrics per clip (`results.jsonl`, one line per clip)

still, seconds, frames, seed, workflow sha256, start/end timestamps, wall time (s), status (ok / oom / error / timeout),
error text, peak VRAM used (MiB, `nvidia-smi --query-gpu=memory.used --format=csv -lms 500`, whole GPU, baseline
noted), peak host RAM used (system, psutil every 0.5 s) and peak RSS of the ComfyUI process, output file, width x height
and frame count read back from the MP4, flagged gray frames (index, luma std), ComfyUI's own execution time from the log.

## Order

1. **Smoke clip** (not counted): still A, 5 s, seed 3400001, cold start. Checks: log shows torch/CUDA and the
   RTX 5090; output is 1920x1088, 121 frames; time, VRAM peak, gray-frame scan. If the smoke clip fails, the cause is
   recorded and fixed before the series; a fix that changes a setting above is a dated addendum.
2. **5 s series:** A, B, C, D, E (seeds as above).
3. **10 s series:** A, B, C, D, E.
4. **20 s (criterion 4):** A, B, C, single pass; chunking only if single pass fails, stated as such.
5. Blind rating by robin (criterion 3).
6. Criterion 5 (2560x1408, 3840x2176) only if 1-3 pass.

Results go to #340 as a comment and to the vault; raw files stay in this folder (git-ignored), this PREREG is
committed (forced past the `runs/` ignore rule) so its time and content are fixed in git before the first clip.
