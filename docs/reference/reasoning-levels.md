## Reasoning levels

Per model, out of the manifest. Names that render the same prompt are one row in the window.

| rows offered | collapses |
|---|---|
| `high` (default), `low`, `medium` | `off` renders as `high` |

## Thinking budget

**On by default, out of the manifest.** Flash-Next `UD-Q2_K_XL` ships `reasoning_budget`
1024; a model whose entry does not declare one stays uncapped, which is what every release
up to 1.7.0 did. `/budget <tokens>` overrides it for a chat, `/budget off` lifts it,
`--reasoning-budget N` does the same from the command line.

It is a sampler field and not a template one, so the prompt is unchanged and setting it
mid-chat costs no prefill -- unlike a level change.

The number is not the user's to guess, which is why it is a manifest entry and not a flag
with a default. 1024 rather than 256 for the reason the table below gives: at a cost that
does not separate, it removes the least thinking.

Measured 2026-08-31, Flash-Next `UD-Q2_K_XL` on pin `6c84c7d5d` + PR #27992, one server,
one prompt, n=3 per arm, paired by seed:

| budget | reasoning tok | answer tok | share | wall |
|---|---|---|---|---|
| off | 7870 | 4432 | 62 % | 352 s |
| 1024 | 1023 | 3134 | 25 % | 116 s |
| 512 | 511 | 4018 | 23 % | 125 s |
| 256 | 255 | 4271 | 6 % | 124 s |

Capping at all is worth ~2.8x wall clock, paired 3/3. **Where** the cap sits is not: 256,
512 and 1024 do not separate at n=3.

The cap never travels alone. `--reasoning-budget-message` defaults to none on the server,
so the thinking block is force-closed and the model keeps writing where it stood -- 2 of 9
capped answers arrived mid-word. Crow sends its own message with every cap: 0 of 6 on the
same seeds, and 185 s down to 81 s, because the cut answers were also the runaway ones.

`0` is refused as a number -- it is the `none` level under another name, and `none` measured
as the most expensive of four settings: 3.6x the time and 1.8x the tokens of `low`. Use
`/budget off` to lift the cap.

### It does nothing in goal mode

Measured 2026-08-31 with the cap wired in, one ten-round goal run on `high` -- the level
that thinks most. Reasoning blocks, through `/tokenize`:

```
181, 159, 122, 58, 50, 44, 17, 16, 15, 14      sum 676, largest 181
```

The 1024 cap never fired, and the end-of-thinking message appears nowhere in that chat's
archive. The 2.8x in the table was measured on a **single prompt that produced one
7870-token block**; goal mode does not have that shape, it thinks in many short bursts, and
a per-request cap cannot touch a share that is a sum of small parts.

So the cap is a brake on a single runaway block in an ordinary chat turn, and nothing else.

### 128 was tried, and it costs

Four goal runs, interleaved `off` / `128` / `off` / `128` on `high`, with the folder,
`.crow/MEMORY.md`, `approvals.json` and the goal reset before each:

| | off #1 | off #2 | 128 #1 | 128 #2 |
|---|---|---|---|---|
| wall | 71.0 s | 59.8 s | 59.6 s | 88.0 s |
| turn tokens | 2156 | 1779 | 1796 | 2703 |
| reasoning total | 917 | 658 | 834 | 1178 |
| rounds | 12 | 10 | 10 | 15 |
| tool calls | 15 | 13 | 11 | 17 |
| cap fired | -- | -- | 2x | 4x |

Means 65.4 s against 73.8 s, and the two paired differences point in **opposite**
directions (-11.4 s, +28.2 s). The capped arm also thought **more** in total. The run where
the cap fired four times needed 15 rounds and 17 tool calls -- take the notepad away and the
model replaces thinking with tool calls, exactly what made `none` the most expensive level.

So the entry is 1024 and not lower on purpose: across all four runs the largest uncapped
block was 324, so 1024 never fires here and cannot cost anything, while 128 fires constantly
and does.

**Not measured:** answer quality under a cap. Structure was judged -- does the answer begin
as an answer -- correctness was not.
