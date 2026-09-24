[← README](../../README.md) · [Docs index](../README.md)

# Session search

```
session_search(query, limit=8)
```

| | |
|---|---|
| Covers | the open chat, chats put aside (`session/chat-*.json`), everything under `session/archiv/`, and the rollover segments (`rollover-*.json`, the part of a long chat before a cut) — `%LOCALAPPDATA%\Crow\` on Windows, `~/.local/state/crow/` on Linux |
| A segment hit | is labelled `<chat title> (before the cut, <date>)`; every hit names its file, for a following `read_file` |
| Index | `index.db`, SQLite FTS5 — `%LOCALAPPDATA%\Crow\` on Windows, `~/.local/share/crow/` on Linux |
| The index is | derived. Delete it and the next search rebuilds it |
| Freshness | file mtime. A changed file loses all its rows and gets new ones |
| Returns | the real messages, clipped at 400 chars each. No summary |
| Query syntax | every word is quoted, so `--slot-save-path` is a search and not an error |
| Without FTS5 | the tool stays declared and answers that nothing was searched |

---
