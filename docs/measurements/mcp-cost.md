## Cost

| | tools | chars in every prompt |
|---|---|---|
| built-in | 12 | 7,758 |
| `@modelcontextprotocol/server-filesystem` | 14 | 8,217 |
| `mcp-server-fetch` | 1 | 1,137 |

Measured 2026-08-22. The tool list is rendered into the head of the prompt, so changing it moves
byte 0: the next turn and the first turn of every saved session pay a full prefill.

## Measured

Three servers, 2026-08-22:

| | protocol | answer | session |
|---|---|---|---|
| `mcp.context7.com` | 2025-06-18 | SSE | none |
| `mcp.deepwiki.com` | 2025-06-18 | SSE | none |
| `docs.mcp.cloudflare.com` | 2025-06-18 | SSE | none |

`docs.mcp.cloudflare.com` answers `Python-urllib` with `403`, error 1010, `browser_signature`. It is
the `User-Agent` that decides, not the protocol.

Driven end to end on 2026-08-22: static headers against `mcp.context7.com`, and the full OAuth leg
against `mcp.higgsfield.ai`, whose `/oauth2/authorize` hands off to Clerk. Its token endpoint
answers `"token_type": "bearer"` and its MCP endpoint refuses `bearer <token>` while accepting
`Bearer <token>`. The scheme is sent capitalised for that reason.
