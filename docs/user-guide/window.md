# Window

<div align="center">
<img src="../images/window.png" alt="Crow window: chat rail, the wireframe over an empty chat, and the composer" width="920">
</div>

| | |
|---|---|
| Composer | model and reasoning level as one chip, context readout, working directory, release level, dictation |
| Cost line | rounds, tokens, decode, prefill, cache hits, tool calls, wall clock |
| Thought blocks | folded, one per re-entry, each labelled with the turn's thinking share |
| Answers | headings, lists, tables, bold, italic, inline code and links, drawn when the turn ends |
| Rail | chats grouped by project, archive, fold state remembered |
| Code panel | on the right, mirrored from the rail: dragged between 260 and 720, folded from the title bar, width and state remembered. Starts at half the space beside the rail until somebody drags it once |
| Tool calls | top of the panel, one fold for the group and one per call. Open a call for its `arguments` and, under them, its `result` — 4,000 characters, the remainder counted. A failed call is marked on its head |
| Program code | under the calls, from `write_file` and `edit_file` only. Its own fold with a count, like the calls above it. The head of each block is the **path**, the body the content — no JSON envelope. Readable while it is being written; the envelope is replaced once the arguments are whole |
| `copy` per block | in the head of every code block, beside the path — it copies **that** block. The panel head has no `copy`: one button for calls and source together copied both to whoever wanted one of them (#156) |
| Git panel | in the CHAT since 2.0.0, under the goal panel — one flex column, so a goal that starts or ends moves it without a line of JavaScript. Toggled by the octocat in the title bar, still the only way to close it (#156, #173) |
| Browser panel | a globe in the title bar, beside code and git. Tabs, an address bar, per-tab history. A second frameless WebView2 over the panel rect, not an iframe — see [browser](browser.md) (#175) |
| `clear all` | empties both halves and stays empty across a restart. The group's own `clear` takes the calls only |
| Code blocks | language, line count and `copy`. Fifteen lines or more can be folded away |
| Images | drop `.png .jpg .jpeg .gif .webp .bmp` into the window, paste a screenshot (Ctrl+V), or `/image <path>`: a chip per image above the input, `×` removes one. They ride the next line, appear in the transcript, and are still there after a restart. Needs a server started with `--mmproj` — one without it refuses with a sentence before anything is sent. The bytes travel unresized; the server caps an image at 4,096 tokens (`--image-max-tokens`). Any other dropped file keeps the old behaviour: its path lands in the input for the model to `read_file`. The model opens one itself with [`read_image`](../reference/tools.md) (#170) |
| Delegation | a subtask is a card in the flow — spot, state, token count — and a child row under its root chat in the rail, marked `⑂`. Clicking either jumps to the card; a subtask is never opened as a chat. Cards keep breathing outside a turn, and Stop cancels the subtasks with the turn |
| `/delegate` mid-turn | the delegation pair (`/delegate`, `/subtasks`) passes the Stop gate: typed while a turn runs, the card starts beside it, the turn keeps streaming, and the composer stays on Stop. A **plain** line mid-turn still stops the turn, as does the button and Escape (#143 E3) |
| `/verify` | the conversation's own writes go to the checker spot with review instructions; the verdict comes back as a subtask card (#149) |
| OpenRouter page | its own pane in Settings, and it routes no turn: the switch parks or runs the broker — delegation, catalogue, favourites — while the machine keeps answering. The default is always the machine; turns leave it only through the Model page |
| Delegate favourites | on the OpenRouter page: three dropdowns over the whole catalogue, tried in your order before the free default — a paid favourite is your explicit pick on your own key, and what nobody chose never falls forward onto a bill. A spot that failed this session is skipped (#146, #148) |
| Budgets | `turn_token_budget` and `subtask_max_tokens` in settings.json, both opt-in (#145); a spent token budget ends the turn with the same protocol as the round budget |
| Self-healing | dies the server the window itself booted (`%LOCALAPPDATA%\Crow\booted.json`, kept across restarts), the turn reboots it — `booting it again (n/3)`, three per turn, then honestly red with the boot's own exit code. A server still loading (HTTP 503) is waited out once per turn |
| Boot logs | every boot Crow starts writes `runs\llama-server-<port>.out.log` / `.err.log` under the working directory, rewritten per boot — a silent death leaves its exit code and stderr there |
| Persistent subtasks | cards and `⑂` rows come back after a window restart (`session\subtasks-registry.json`): `running` becomes `interrupted`, numbering continues, deleting a chat deletes its subtasks |
| Scroll | the stream pulls to the end only for who IS at the end (80 px); scrolled up, nothing yanks you back — your own message does |
| Rollover | past 0.9 of the window the next line rolls BEFORE the turn — the archive is a complete conversation, your line opens the new context as carry. Mid-turn the roll happens at a round boundary, once per turn; a refused second roll is a red line, and the readout resets the moment a roll happens (#152). The note carries the model's own digest of the leg — asked on the still-warm prefix, marked as unverified model text, capped by `rollover_digest_tokens` (`0` off, #154); the leg runs with thinking off, because the model's thinking would otherwise eat the cap (#157) |

---

## Git panel (#156)

The octocat in the title bar opens it, and closes it again — there is no cross in
the panel, for the reason the code panel has none: a control inside the thing it
folds away leaves no way back.

| group | what it shows |
|---|---|
| Changes | branch against its upstream, every changed file with its status letter and `+`/`−`, the totals in the head. Untracked files are listed and counted separately — they are never swept into a commit |
| `⎇ <branch>` | the current branch, ahead/behind in the head, every local branch in the body |
| `⊸ Commit` | the tracked, changed files **by name**, a message field, and the button. It stages exactly those paths — no `-a`, no `.` |
| History | `◉` commit · `⑃` merge · `⇧` push · `⑂` fork · `◈` connect. Commits and merges come out of `git log`; pushes and connects out of Crow's own record (`%LOCALAPPDATA%\Crow\git_events.json`) — nothing is invented, a fork appears the day one happens |

The repository is the one the **working directory** is bound to, never the process's
cwd. No folder bound, or the folder is not a repository: the panel says so and shows
nothing else.

### Connecting GitHub

Settings → API Keys → GitHub. The client id belongs to an OAuth app **with device
flow enabled** and is not a secret — the device flow has none, which is why it may
be shipped or typed in plain. `Connect` puts a code in the chat; enter it at
github.com/login/device and the token is stored owner-only beside the provider keys
(`provider_keys.json`). One app covers every repository the account can reach.

`Disconnect` sits on that same page, and only there: the account button in the
panel head **only connects**. As a toggle it once read a stale label and
disconnected the account somebody was trying to connect.

**Pushing uses git's own credentials**, not that token — a token on a git command
line would be readable in the process list for the length of the call.

---
