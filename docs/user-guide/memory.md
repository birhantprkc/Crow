# Memory

Two files. Plain text, `§` on its own line between entries, editable by hand.

| path | limit | holds |
|---|---|---|
| `<working directory>\.crow\MEMORY.md` | 4,000 chars | this project: layout, conventions, commands, traps |
| `%LOCALAPPDATA%\Crow\USER.md` | 1,500 chars | who you are, how you want to be worked with |

| | |
|---|---|
| Limits come from | `MAX_TOOL_BYTES`. 16,000 B is ~4,000 tokens, so 4 chars buy 1 token |
| 4,000 chars is | a quarter of one tool read. Bigger than that and `read_file` is cheaper |
| Head cost, both stores plus one skill | 633 chars = 158 tokens = **0.09 %** of the usable window |
| Empty stores cost | nothing. No entries, no block, byte 0 unchanged |

## Rules

| | |
|---|---|
| Never trimmed for you | a write over the limit fails and returns the entries and both numbers |
| No `read` action | the content is already in the prompt |
| Exact duplicates | answered with success and one entry |
| Injection and invisible Unicode | refused before the entry is written |
| No working directory bound | `memory` is refused with a reason; `user` still works |

## The head is pinned

The rendered block is written into the chat file on first open and replayed **verbatim** from then
on. `prefix_fingerprint` hashes the system prompt, llama-server reuses a prompt by common token
prefix, and the KV cache lives on disk, so a head re-read at every start would go stale against
every saved cache. Binding a different folder re-pins and says what the prefill costs first.

## Who writes it

| | |
|---|---|
| Trigger | `MEMORY_REVIEW_AT` = **0.20 / 0.50 / 0.75** of the context window |
| Each mark fires | once. The mark is written to the chat file and travels with it |
| A turn crossing several marks | fires once, at the highest |
| Off with | `--no-review` |
| Before it writes | **it asks.** The proposed entries wait on a chip in the composer; nothing reaches the file until you press it |
| Nobody answers | they expire after **300 s** and are dropped. Nothing is ever written by a timer |
| Ask nothing, write always | `--no-memory-approval` |
| When it saves | one line in the chat, per entry, at the moment it lands |

## The gate

The review never writes on its own. What it wants to keep is staged and shown behind the composer,
and it stays there until you answer.

<div align="center">
<img src="../images/memory-consolidation.png" alt="Memory Consolidation: the staged writes behind the composer, +2 gained and -0 lost" width="900">
</div>

| | |
|---|---|
| Collapsed | the title, lines **gained** in green and **lost** in red. A `replace` is one entry and both |
| Click | opens every proposed entry in full, and the two answers |
| `save to memory` | writes through the same `memory` tool the model uses. The cap, the duplicate check and the injection scan all still answer |
| `discard` | nothing is written |
| No answer | the entries expire after 300 s and are dropped. **Nothing is ever written by a timer** |
| New chat | the questions go with it |
| Typing while it runs | the line is **queued**, not dropped. The composer says `queued -- the memory review is finishing` and the turn starts by itself |
| Off | `--no-memory-approval`, and then the review writes unasked as it did before 1.0.0 |

It keeps breathing while it waits, because a question is still true until it is answered. The line
that reports a **finished** write glows once and settles: same colour, different grammar.

---
