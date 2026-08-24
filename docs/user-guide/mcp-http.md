# MCP over HTTP

Transport `Streamable HTTP`, specification `2025-06-18`. A block with a `url` uses it; a block with
a `command` does not.

| | |
|---|---|
| Endpoint | one URL, `http` or `https`. `POST` only. No `GET` stream is opened |
| Per message | one `POST`, `Content-Type: application/json` |
| `Accept` | `application/json, text/event-stream`. Both, on every request |
| Answer | a JSON object **or** an SSE stream (`event: message` / `data: {…}`). Both are normal; context7 answers `tools/list` as a stream |
| SSE reader | own thread, stops at the first message carrying `result` or `error`. `:` comment lines and notifications ahead of the answer are skipped |
| Notification · client response | `202`, empty body, nothing enqueued |
| Session | `Mcp-Session-Id` off the `initialize` response where the server sets one, then on every request. **No id is a valid state**, not a fault |
| `404` on a session | expiry. The session is dropped, `initialize` runs again, the call is retried **once** |
| Close | `HTTP DELETE` with the session id. `405` and any other refusal are ignored |
| Server → client requests | arrive on the open stream, answered by a separate `POST` |

## Headers

Three layers. Later ones overwrite earlier ones.

| layer | |
|---|---|
| 1 · identity | `User-Agent: Crow/<version> (+<repo>)` |
| 2 · block | everything in `headers`, e.g. `Authorization` |
| 3 · transport | `Content-Type`, `Accept`, `Mcp-Session-Id`, `MCP-Protocol-Version` |

`MCP-Protocol-Version` is sent only after `initialize` has come back, never on it.

`headers` is not in `mcp_view()` and appears in no listing, no sheet and no log.

## Adding one

```
/mcp add https://mcp.context7.com/mcp
/mcp add https://mcp.example.com/mcp --header Authorization: Bearer <token>
/mcp add https://mcp.example.com/mcp -H X-Api-Key: <key> -H X-Org: acme
```

| | |
|---|---|
| Name | from the host: `mcp.context7.com` → `context7`, `docs.mcp.cloudflare.com` → `cloudflare_docs` |
| `--header` · `-H` | `name: value`, may repeat. Everything after the flag up to the next flag is the value, so a value may contain spaces |
| Refused | a URL with arguments after it, `--header` without a `:`, a header carrying a control character, `--header` on a command line |

## OAuth

A server that answers `401` is authorised in the browser. `/mcp add <url>` does it by itself, and
so does re-fetching a configured server; `/mcp auth <server>` repeats it on its own.

| | |
|---|---|
| Discovery | `WWW-Authenticate: resource_metadata=...` when the `401` carries it, else `/.well-known/oauth-protected-resource<path>`, else `/.well-known/oauth-protected-resource` |
| Authorization server | every entry in `authorization_servers`, in the order the document lists them. Metadata from `/.well-known/oauth-authorization-server` then `/.well-known/openid-configuration`, path-inserted first where the issuer has a path |
| `issuer` | in the metadata document, compared against the URL it was fetched from. A mismatch is refused |
| PKCE | `S256`, required. `code_challenge_methods_supported` without it, or absent, is refused |
| Client | RFC 7591 dynamic registration, `token_endpoint_auth_method: none`. `client_id` + `client_secret` in the block are used instead where the server rejects registration. Google Drive answers `400`, GitHub Copilot advertises no endpoint at all |
| `client_name` | `Crow`, overridable. Figma's endpoint allowlists registration by exact name and `403`s one it does not know |
| Redirect | `http://127.0.0.1:<port>/callback`, listener bound to loopback only. `redirect_host: localhost` changes only the name. Some authorization servers sit behind a WAF that `403`s a literal `127.0.0.1` |
| `state` | sent and compared. This is what binds the answer to the request |
| `iss` | read, not enforced. It guards mix-up, which needs a client talking to several authorization servers in one flow; this one talks to exactly one and takes the token endpoint from metadata fetched before the browser opened. Enforcing it refuses every brokered login. Clerk, Auth0 and Okta all stamp their own domain |
| Several `authorization_servers` | tried in the order the metadata lists them |
| `resource` | RFC 8707. The `resource` the metadata names, checked against the endpoint's host first; the canonical URI of the endpoint where it names none. On the authorization request and the token request |
| Transport | every endpoint must be `https`, or loopback. Anything else is refused before a token moves |
| Tokens | `%LOCALAPPDATA%\Crow\mcp_tokens.json`, never in `mcp.json` and never in a view. `0600` where the platform means it. Dropped with the server |
| Refresh | inside a tool call, silently, `60 s` before expiry and on a `401`. A browser never opens during a turn. The call fails naming `/mcp auth <server>` |
