## Tools

21 built in, plus whatever [MCP servers](../user-guide/mcp.md) are configured.

`read_file` `write_file` `edit_file` `list_dir` `find_files` `search_text` `run_command`
`web_search` `fetch_url` `memory` `skill` `session_search` `delegate` `subtasks` `collect`
`git_status` `git_diff` `git_log` `git_commit` `git_push` `github_connect`.

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
while a turn is running](../user-guide/window.md).

| release level | asks before |
|---|---|
| `auto` (default) | nothing |
| `allowedit` | executing |
| `manual` | writing and executing |

Reading never asks, at any level.

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

### Outside paths ask (#144)

`run_command` touching paths outside the working directory asks first, at every release
level — one card, every outside path named. An approval covers ALL outside paths of that
command, not just the first; `always` is kept in `%LOCALAPPDATA%\Crow\approvals.json` and
survives the restart. Directories the conversation was pointed at pass without asking.
An obfuscated path does not ask — the gate is a question, not a sandbox.

---

### /verify (#149)

The maker is not the checker. `/verify` (both surfaces) assembles what this conversation
wrote — `write_file` whole, `edit_file` as replaced/with, reads deliberately absent — and
delegates it to the remote spot with review instructions; `collect` fetches the verdict.
User-triggered on purpose: a maker that may skip its own checker will.
