[← README](../../README.md) · [Docs index](../README.md)

## Tools

27 built in, plus whatever [MCP servers](../user-guide/mcp.md) are configured. `/tools` lists
them in either surface, derived from the declarations themselves rather than written beside them.

`read_file` `read_image` `render_page` `write_file` `append_file` `edit_file` `list_dir` `find_files`
`search_text` `run_command` `build_bundle` `web_search` `fetch_url` `memory` `skill` `session_search`
`delegate` `subtasks` `collect` `goal_set` `goal_step` `git_status` `git_diff` `git_log`
`git_commit` `git_push` `github_connect`.

An MCP tool joins the same list as `mcp_<server>_<tool>`, above the built-ins, and carries its
own class.

### `render_page` (#175)

`render_page(path, wait_ms=4000, width=1280, height=800)` — a page in a browser Crow owns.

| | |
|---|---|
| class | `executing` — it starts a process and writes a file |
| browser | Chrome, then Edge; every candidate resolved through environment variables |
| target | a file in the working area, or an `http(s)` URL |
| output | `<root>/.crow/renders/render-<stamp>.png`, plus the console lines from stderr |
| isolation | its own `--user-data-dir` per run. Without it Chrome hands the job to a running instance and returns exit 0 with no screenshot |
| driving (Linux) | `--remote-debugging-pipe` (fd 3/4, NUL-separated CDP JSON, no library): load, run `wait_ms` **real** milliseconds, `Page.captureScreenshot`. A page with no load event after 15 s is captured anyway |
| driving (Windows) | the command-line `--screenshot` with `--virtual-time-budget=wait_ms`; the pipe there needs handle inheritance nobody has measured yet |
| `wait_ms` | real time after load, 200–20,000. A larger value never rescues a page too heavy to draw |
| caps | one ceiling for the whole call: 15 s load + `wait_ms` + 10 s for the frame. It does not grow with anything the page does |
| rasterer | `gpu (angle)` when the card has ≥ 512 MiB free, else `software (swiftshader)`; named in every result |
| memory | Linux: its own user scope, `MemoryMax=6G`, swap 0 (#213) |
| kill | `proc.kill()` on its own handle, then its session. Never by name, never a process list (#158) |
| pipes | stdout and stderr go to a file: `communicate()` hangs on Windows after a kill when a grandchild holds the write end. The two DevTools pipes are Crow's own ends, read with `select` and a deadline |

A failed capture says which rasterer ran and that a larger `wait_ms` will not help —
the old `timed out after 20000 ms` read as "give it more", and a model escalated
6000 → 12000 → 20000 on a page whose cost did not depend on it.

Measured 2026-09-22, Chromium 152.0.7977.82, RTX 5090 free:

| case | wall clock | result |
|---|---|---|
| WebGL voxel diorama (23k voxels), software, old path, virtual budget 1000 / 2000 / 4000 | 32.7 / 32.8 / 32.8 s | flat: 0.345 s per software frame, ~90 frames before the CLI draws. The old deadline was 9 / 10 / 12 s |
| same page, software, `wait_ms=4000` | 6.9 s | 1280×720 capture of the scene |
| same page, GPU, `wait_ms=4000` | 5.7 s | capture; the page's own 90-frame loop settled |
| same page with an endless animation loop, software, `wait_ms=20000` | 22.8 s | capture (old path: once 31 s, once no image in 40 s) |
| page settling at 300 ms, `wait_ms=1500` | 1.8 s | settled text; "almost one colour" note (99.96 % white) |
| endless `fetch` loop, `wait_ms=1200` | 1.5 s | capture (old path, measured 2026-08-31: no image after 9.3 s) |
| `while(true){}` | 25.1 s | `error: ... no frame within 10 s of the capture request, and no load event within 15 s before it` plus the rasterer advice |

Under the old virtual clock the GPU arm drew **no** `requestAnimationFrame` frame at all
(a page-side counter stayed below 10 while the budget ran out in ~10 ms of real time);
real time is what an animated page actually needs.

The console line `GL Driver Message (OpenGL, Performance, GL_CLOSE_PATH_NV, High): GPU
stall due to ReadPixels` comes from Chromium's bundled ANGLE, not from the NVIDIA driver:
measured, it appears only in the SwiftShader arm, and the string is in the `chromium`
binary and in no `libnvidia-*`. The result opens as a tab in the
[browser panel](../user-guide/browser.md).

### `build_bundle` (#212)

`build_bundle(entry, out=<entry>.bundle.html|.js, global_name="", minify=true)` — a module
graph as ONE offline file, built by the esbuild already on the machine.

The rule it exists for: a page opened from `file://` has origin `null`, and Chromium fetches
module scripts in CORS mode, so every `import` between local files is refused ("Cross origin
requests are only supported for protocol schemes: … http, https"). Import maps do not change
that. A classic `<script>` is not affected — so the offline shape is one classic script holding
the whole graph, which is esbuild's `--format=iife`. The tool description says this to the
model, together with "never flatten a library by hand".

| | |
|---|---|
| class | `executing` — it starts a process and writes a file; an "always" is keyed to the tool, never to `run_command esbuild` |
| entry `.html` | every `<script type="module">` (`src` or inline) bundled and inlined at the end of `<body>` in document order (modules are deferred; a classic script in `<head>` would run before the canvas exists); the import map becomes `--alias` pairs (targets made absolute: esbuild resolves an alias in its working directory); local stylesheets bundled into `<style>`; local classic scripts inlined as they are |
| entry `.js/.mjs/.ts` | `out` `.js` → the IIFE (`global_name` names its exports; without it an IIFE's exports are unreachable, and when the same esm probe as below finds any, the result says so under the `built` line: "warn: no global_name -- app.js exports boot, and an IIFE without a global leaves it unreachable ..." — warned, not defaulted: a derived name would put `app`/`main` on `window` where it can shadow a page global, and a module that starts itself needs none; no exports or a failed probe say nothing. Measured on the diorama graph: 0.07 s → 0.13 s with the probe); `out` `.html` → the IIFE wrapped in an EMPTY page: no markup, no call to any export. The result then says so right under the `built` line and names the entry's exports ("app.js exports: boot -- nothing calls it"), read from a second, unminified `--format=esm --metafile` run into the temp directory — esbuild's metafile lists `exports` only for esm, for the IIFE it is `[]`. A failed probe says "could not be read" and costs the build nothing |
| a page = an `.html` entry | the tool description says it: write the page as HTML (canvas, markup) with a `<script type="module">` that imports and starts the app, then bundle THAT. A `.js` entry to `.html` is warned, not refused: a module that builds its own DOM and starts itself on load (the three.js-example shape) works through it, and nothing short of running it tells that apart from a `boot()`-shaped one |
| argv | `--bundle --format=iife --platform=browser --charset=utf8 --log-level=warning --log-limit=20`, `--minify` by default, text loader for `.glsl .vert .frag .vs .fs .wgsl .txt`, data URLs for images, fonts, `.glb .gltf .hdr .exr .ktx2 .bin .wasm` |
| esbuild, in order | `CROW_ESBUILD`; `node_modules` walking up from the entry (`@esbuild/<platform>`, `esbuild/bin`, `.bin`); `esbuild` on `PATH`; the deno cache (`$DENO_DIR/dl/esbuild-*/`) and the npx cache (`~/.npm/_npx/*/node_modules/@esbuild/`), newest version wins there. Every candidate must answer `--version` |
| none found | the result lists every place searched and says not to hand-flatten |
| caps | one clock for the whole build (`BUNDLE_TIMEOUT` = 120 s), the #207 capture cap in the reader threads, 64 MiB on the result, 8 MiB on the entry page. Every esbuild call, the `--version` probes included, runs through `_bounded_run` — the one runner `run_command` uses; a grandchild left holding the pipe (a node wrapper's shape) does not hold the clock |
| write | esbuild writes to a temporary directory; Crow writes `out` behind `write_file`'s fence. A file carrying the `crow build_bundle` mark (a `<meta name="generator">` / a first-line comment) is replaced freely; any other existing file only after a read in this conversation, unchanged on disk since ([Read before write](#read-before-write-215)) |
| result | path, bytes, errors, warnings, seconds, which esbuild and where from, what was inlined, and whether the page still loads anything from disk. On errors nothing is written and the esbuild log comes back |
| cache | never answered from the repeat cache: an edit to a source changes the result of the same call |

Measured 2026-09-22 on a copy of the diorama-test app graph (three.js 0.186 plus post-processing,
esbuild 0.28.2 from its `node_modules`): 957,335 bytes as an IIFE and 957,410 bytes as a page,
0 errors, 0 warnings, 0.07 s wall. The page rendered the scene through `render_page`; the
module source page next to it logged the CORS refusal above. The same graph with `src/app.js`
as the entry and an `.html` out built just as clean and rendered one colour: the app exports
`boot(canvas, opts)` and needs `<canvas id="c">`, and that page has neither — the trap the
warning above names (0.13 s with the exports probe, `exports: boot`).

### `read_image` (#170)

`read_image(path)` — the model's own way to a picture; `/image`, drop and Ctrl+V are the
user's.

| | |
|---|---|
| class | `reading` — asks at no level |
| types | `.png .jpg .jpeg .gif .webp .bmp`, other extensions refused by name |
| path | resolved against the working area, like every other reader (#177) |
| result | tool message content becomes `[{text}, {image_url}]` — the block a pasted image travels as; the server reads it in any role |
| no projector | `refuse_images` checks `/props` before the block is attached; without `--mmproj` the sentence comes back instead of an image (a picture to a blind server is HTTP 500, not a recoverable tool error) |
| size | none of its own — the server caps at `--image-max-tokens` (4,096) |

### Delegation (#143)

Parallelism is bought at a provider, not from the card: `delegate(task)` starts a second
session on a remote spot and returns its id immediately — the local slot (`-np 1`, warm
cache) is refused as a target, hard. `subtasks()` lists where things stand; `collect(id)`
(or `collect("all")`) blocks once and returns the result. A subtask sees only what was
sent to it. Stop cancels the local turn **and** the subtasks; whatever a stream still
delivers is dropped and the card ends `interrupted`. Tokens are counted from the remote's
`usage` block — remote endpoints send no llama timings. The default spot is the free
pool's best answer, pinned only after a model answered twice in a row and carried a real
delegation; the user's own `/delegate <task>` does the same from the composer, [also
while a turn is running](../user-guide/window.md). A failed spot falls forward by what
its error means: sick (429/5xx/timeout) and refusing (403, no endpoints, 402 on a paid
favourite) spots are skipped, while 401, a free spot's 402 and schema errors stop the chain.
The failure names every spot tried and why each one failed ([details](../user-guide/goals-and-subagents.md)).

| release level | asks before |
|---|---|
| `auto` (default) | nothing |
| `allowedit` | executing |
| `manual` | writing and executing |
| `yolo` | nothing, and it MEANS it: the outside-path ask (#144) and `git_commit` fall silent with it. `git_push` asks at **every** level, yolo's included -- checked before the dial, so no position of it can lie |

Reading never asks, at any level. `yolo` is a session's word: it never reaches the root file, and it dies with the process.

### Goals (#165)

`goal_set(title, steps)` writes the plan; `goal_step(step, status, note)` moves one step to
`running`, `done` or `failed`. Two tools and not one, because they cost different things: the
plan sits in the pinned head of every prompt, so writing one costs a full prefill, while ticking
a step off writes only `<root>/.crow/goal.json` and moves no byte of the prompt. The head carries
the plan, the file carries the state — see
[goals and subagents](../user-guide/goals-and-subagents.md).

### Git (#156)

`git_status` `git_diff` `git_log` read; `git_commit` `git_push` write. All five run a
fixed argument list **without a shell** — a branch or path that looks like an option
stays data — against the repository the working directory is bound to.

`git_commit` stages exactly the paths it is given: no `-a`, no `.`. `git_push` uses
git's own credentials on this machine; the GitHub token below is for the account, not
for the push.

| | |
|---|---|
| asks | `git_commit` and `git_push`, **at every release level, `auto` included** |
| `always` | impossible for those two — they have no approval scope, so no answer makes the next one silent |
| release level | cannot release them: they are not in the level table at all |

`github_connect` runs the OAuth **device flow**: it returns the code immediately and
polls in the background — the browser leg takes minutes and no tool call may hold the
turn that long. The token lands in `provider_keys.json`, owner-only, and is never
handed to a surface; what a surface shows is the login name. Needs a client id, see
[the window's git panel](../user-guide/window.md).

### Read before write (#215)

`write_file` (on an existing file) and `edit_file` refuse a file the model does not know.
Per path Crow keeps the `(mtime_ns, size)` the file had when `read_file` read it — a line
range counts — or when Crow itself last wrote it (`write_file`, `edit_file`, `build_bundle`;
`append_file` keeps an already-known file known). The call goes through while the file on
disk still carries that stamp.

| state | answer |
|---|---|
| never read in this conversation | `refusing to overwrite … without reading it first in this conversation` / `read … before editing it, in this conversation` |
| read, then changed on disk (another program, the user, a deletion) | `… it changed on disk since you read it -- read it again, then retry the call.` |
| read or written by Crow, unchanged | allowed, across any number of turns |

A read counts for the conversation, not the turn: goal mode's `[Goal mode ...]` nudges are
user messages, and until #215 each of them emptied the state (measured 2026-09-22: 4 of 15
read-rule refusals were edits of a file read one nudge earlier). The state empties where the
model stops holding the contents: a rollover (mid-turn too), a new chat or `/reset`, a
model switch, `--resume` and a chat switch in the window. A delegated subtask neither sees
nor changes it. A rewrite that keeps both the size and the modification time is not seen.

### Outside paths ask (#144)

`run_command` touching paths outside the working directory asks first, at every release
level — one card, every outside path named. An approval covers ALL outside paths of that
command, not just the first; `always` is kept in `approvals.json` — under
`%LOCALAPPDATA%\Crow\` on Windows, `~/.config/crow/` on Linux — and survives the restart. Directories the conversation was pointed at pass without asking.
An obfuscated path does not ask — the gate is a question, not a sandbox.

### Argument names (#207, #214, #215)

A tool is called with the names its declaration gives. Three cases fall outside that, and
each one is said, never swallowed.

**A sibling harness's name** for the same argument is taken — declared in
`ARGUMENT_ALIASES`, not guessed — and the result opens with what was taken:
`[took old_string as old, new_string as new]`.

| tool | taken as declared |
|---|---|
| `edit_file` | `file_path` → `path`, `old_string` / `old_str` → `old`, `new_string` / `new_str` → `new` |
| `read_file` `append_file` | `file_path` → `path` |
| `write_file` | `file_path` → `path`, `file_text` → `content` |
| `search_text` `find_files` | `path` → `root` |
| `memory` | `new_text` → `content` |

Two names for one argument with different values are an error, not a pick; the same value
twice runs, with `[dropped …]`.

**A key no declaration names** is ignored, and the result opens with
`[unknown argument(s) ignored: …]` (#207).

**A key that is unknown while a required one is missing** is a misnamed argument, and then
nothing runs. The answer names the signature, and it comes before the tool's own checks —
the read-before-edit gate included:

```
error: edit_file was called with unknown argument(s) replace, search and without the
required old, new -- nothing was run. Its arguments are: path, old, new.
```

A required key missing on its own gets the tool's own sentence; `edit_file` says a missing
`old` or `new` before its read rule (`new=""` deletes, a missing `new` no longer does).
`search_text` given a file as its root searches that file.

Measured 2026-09-22 after a rollover: 22 of 22 `edit_file` calls arrived as
`old_string`/`new_string`, all 22 failed, and 15 of them were first told to read the file —
so the model read it and sent the same wrong keys again. 4 of those 15 had read the file one
`[Goal mode ...]` nudge earlier — the read rule was per turn then, and every nudge opened
one. It now lasts the conversation and ends where the file changes
([Read before write](#read-before-write-215)).

The request after a rollover declares the same `tools` array, the same sampler and the same
thinking fields as the one before; only the messages and the pinned head differ (pinned by
`TheSeamKeepsTheRequestTests`).

Since #214 the messages after the cut also show calls that worked. Behind the rollover note
come the last 3 tool rounds before the cut, verbatim: each is the assistant's call(s) and
every matching result, with no dangling `tool_call_id`. A round is carried only when every
call names a declared tool with only declared keys and all required ones, and when no
result was an error (`error: ...`, also behind the bracket notes, or a non-zero `[exit N]`).
So an `old_string` call that #215 resolved is not carried, since it would teach the wrong
name. Reasoning and prose stay behind. Results start with `[carried across the cut]` and
are clipped to 2000 chars (`-- clipped to the first 2000 of N chars`). An image is replaced
by a sentence. The rounds share a budget of 3000 tokens (at 3 chars per token; measured
2.85 on the 17:12 archive). A round that does not fit is skipped whole, never cut. If none
of the three shows `edit_file`, `write_file` or `append_file`, the latest one that does
and fits takes the oldest one's place. The typed line comes after the rounds. Without
rounds the note and the line stay one message. `_READ` stays per turn, so a carried read
grants no edit. At the 17:12 cut this would have carried two `run_command` rounds and the
final `edit_file` (`path, old, new`): 3,755 JSON chars, about 1,250 tokens.

---

### /verify (#149)

The maker is not the checker. `/verify` (both surfaces) assembles what this conversation
wrote — `write_file` whole, `edit_file` as replaced/with, reads deliberately absent — and
delegates it to the remote spot with review instructions; `collect` fetches the verdict.
User-triggered on purpose: a maker that may skip its own checker will.
