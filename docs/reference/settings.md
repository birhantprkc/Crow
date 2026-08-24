# Settings

`Help → Settings` in the window.

| pane | |
|---|---|
| **Appearance** | theme: dark, light, crow |
| **Skills** | one row per skill, name and description, a switch. Off takes it out of the prompt; the file stays. Switching re-pins the open chat and says what the prefill costs |
| **Server** | connection state, the base URL as its title, and the tool-call switch |
| **MCPs** | one row per server, folded; per tool a switch and its class. Add with a command line, `ask again`, `remove`. See [MCP servers](../user-guide/mcp.md) |
| **Model** | provider and model, two folds. Picking a provider empties the chat. See [Remote models](../user-guide/remote-models.md) |
| **Subscriptions** | one tile per provider that can log in. Click opens the browser; `sign out` drops the login |
| **API Keys** | one key per provider. Stored in `provider_keys.json`, shown as a mask afterwards |
| **About** | version, the release check, and the button that installs it. A restart is needed afterwards |

Chat rail: right-click a chat to rename, move to a project, archive or delete; right-click the empty
space for a new chat or a new project. A project **is** a working directory. A chat belongs to one
when its `crow_root` points there, and nothing else records it.

---
