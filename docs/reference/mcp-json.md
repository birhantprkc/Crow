## The file

```json
{"servers": {"filesystem": {
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/dev/Crow"],
  "env": {},
  "enabled": true,
  "timeout": 60,
  "connect_timeout": 20,
  "tools": {"include": ["read_file"], "exclude": []},
  "schema": {"tools": [{"name": "read_file", "description": "...",
                        "inputSchema": {}, "annotations": {}}]},
  "classes": {"read_file": "reading"}
}}}
```

| key | |
|---|---|
| `schema` | what the server answered. Untrusted, per the specification |
| `classes` | what you confirmed. This is the half a release level acts on |
| no `tools` block | the open state, and what a freshly added server gets. Everything it offers is registered, including what it adds later |
| `include` | positive list, and it wins over `exclude`. Written only when you narrow it yourself -- a list naming every offered tool is a snapshot, not a filter, and is dropped |
| `exclude` | what a cleared tick writes. It names the refusal, so the rest of the server stays open |
| Globs | both lists take `*`, `?`, `[…]`. An entry without one is an exact name. `docs` excludes the tool `docs`, never `docs_search` |
| `enabled: false` | skipped. No connection attempted |
| `elicitation` | `false` stops this server asking, and stops the capability being declared |
| `timeout` · `connect_timeout` | seconds. Defaults 60 and 20 |
| `url` | Streamable HTTP endpoint, `http` or `https`. Not alongside `command` |
| `headers` | sent on every HTTP request. Never shown in either surface |
| `client_id` · `client_secret` | a pre-registered OAuth client, for servers that reject dynamic registration |
| `client_name` | what dynamic registration calls this client. Default `Crow` |
| `redirect_host` | `127.0.0.1` (default) or `localhost` in the redirect URI. The listener binds loopback either way |

The classification is pre-filled from `annotations`: `readOnlyHint: true` → `reading`,
`destructiveHint: false` → `writing`, anything else → `executing`. The specification's own defaults
are `readOnlyHint: false` and `destructiveHint: true`, so a server that says nothing gets the
strictest of the three.

## The block

```json
{"servers": {"context7": {
  "url": "https://mcp.context7.com/mcp",
  "headers": {"Authorization": "Bearer <token>"},
  "timeout": 60,
  "connect_timeout": 20,
  "tools": {"include": ["query-docs"], "exclude": []},
  "schema": {"tools": [{"name": "query-docs", "description": "...",
                        "inputSchema": {}, "annotations": {}}]},
  "classes": {"query-docs": "reading"}
}}}
```
