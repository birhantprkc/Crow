# Session search

```
session_search(query, limit=8)
```

| | |
|---|---|
| Covers | the open chat and everything under `session\archiv\` |
| Index | `%LOCALAPPDATA%\Crow\index.db`, SQLite FTS5 |
| The index is | derived. Delete it and the next search rebuilds it |
| Freshness | file mtime. A changed file loses all its rows and gets new ones |
| Returns | the real messages, clipped at 400 chars each. No summary |
| Query syntax | every word is quoted, so `--slot-save-path` is a search and not an error |
| Without FTS5 | the tool stays declared and answers that nothing was searched |

---
