[← README](../../README.md) · [Docs index](../README.md)

# Settings

`Help → Settings` in the window.

| pane | |
|---|---|
| **Appearance** | theme: dark, light, crow |
| **Skills** | one row per skill, name and description, a switch. Off takes it out of the prompt; the file stays. Switching re-pins the open chat and says what the prefill costs |
| **Server** | connection state, the base URL as its title, and the tool-call switch |
| **MCPs** | one row per server, folded; per tool a switch and its class. Add with a command line, `ask again`, `remove`. See [MCP servers](../user-guide/mcp.md) |
| **Model** | provider and model, two folds. Picking a provider empties the chat. See [Remote models](../user-guide/remote-models.md) |
| **OpenRouter** | the broker on its own page, and the page routes no turn: the operation switch parks or runs delegation, the three delegate favourites and the model pick configure it. Turns stay local until a provider is picked on the Model page |
| **Subscriptions** | one tile per provider that can log in. Click opens the browser; `sign out` drops the login |
| **API Keys** | one key per provider. Stored in `provider_keys.json`, shown as a mask afterwards |
| **About** | version, the release check, and the button that installs it. A restart is needed afterwards |

Chat rail: right-click a chat to rename, move to a project, archive or delete; right-click the empty
space for a new chat or a new project. A project **is** a working directory. A chat belongs to one
when its `crow_root` points there, and nothing else records it.

---

## #145/#148/#154 — budgets, favourites, digest

| key | default | what |
|---|---|---|
| `turn_token_budget` | `0` (off) | decoded tokens one window turn may spend; spent forces the answer, same protocol as the round budget |
| `subtask_max_tokens` | `0` (= `REMOTE_MAX_TOKENS`, 8192) | output cap per delegated subtask; nonsense clamps to the default, never to unlimited |
| `rollover_digest_tokens` | `400` | cap for the model's own digest in the rollover note, asked on the still-warm prefix before the cut; the leg sends at least 2000 (#205), and an answer cut off at the cap is marked as cut (#210); `0` switches it off (#154) |

The delegate favourites live in `providers.json` (`delegate_favorites`), not here — set them
from the OpenRouter page of the settings sheet.

---

## The secret store (#193)

A key in an environment variable is inherited by every child process and printed by every tool
that dumps `Env:`. Measured 2026-09-10: a measurement runner in another repository printed its
shell environment into a log, and `CROW_TAVILY_KEY` went into the log with it. So the value
lives in a file instead.

| | |
|---|---|
| path | `%LOCALAPPDATA%\Crow\secrets.json` on Windows, `~/.config/crow/secrets.json` on Linux — beside `approvals.json` and the rest of what you configured |
| shape | a flat `{"NAME": "value"}` object, read as `utf-8-sig` so a BOM from Notepad or PowerShell 5.1 does not make it unreadable |
| order | the store first, the environment second. An entry that is present but **empty** counts as not set |
| the environment still works | and says so once, on a console, naming the file to move the value into. Every installation that exists today has the variable and no file |
| what is in it today | `CROW_TAVILY_KEY`, the general web index for `web_search`. `CROW_SEARXNG_URL` is a URL, not a secret, and stays an environment variable |
| writing it | `powershell -ExecutionPolicy Bypass -File tools\migrate-secrets.ps1 -Names CROW_TAVILY_KEY` — it writes the file, sets the ACL and takes the variable out of the user scope |
| writing it on Linux | by hand: `{"CROW_TAVILY_KEY": "..."}` in `~/.config/crow/secrets.json` (`$XDG_CONFIG_HOME/crow/` when that is set). The migration script is Windows-only |
| what the texts say (#194) | `web_search`'s upgrade hint, its refusal and the SearXNG hint name this file by its resolved path first and the environment as the fallback; a refused Tavily key says whether it came from the store or the environment |
| moving it | `CROW_SECRETS_FILE`; the only caller that needs it is a test |

A broken store is reported by **path** and never by content: a parser error that quoted the line
it choked on would print the key it was written to hide. Provider keys and logins are a different
pair of files — see [Remote models](../user-guide/remote-models.md).
