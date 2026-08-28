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

## #145/#148 — budgets and favourites

| key | default | what |
|---|---|---|
| `turn_token_budget` | `0` (off) | decoded tokens one window turn may spend; spent forces the answer, same protocol as the round budget |
| `subtask_max_tokens` | `0` (= `REMOTE_MAX_TOKENS`, 8192) | output cap per delegated subtask; nonsense clamps to the default, never to unlimited |

The delegate favourites live in `providers.json` (`delegate_favorites`), not here — set them
from the OpenRouter page of the settings sheet.
