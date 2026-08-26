# Window

<div align="center">
<img src="../images/window.png" alt="Crow window: chat rail, the wireframe over an empty chat, and the composer" width="920">
</div>

| | |
|---|---|
| Composer | model and reasoning level as one chip, context readout, working directory, release level, dictation |
| Cost line | rounds, tokens, decode, prefill, cache hits, tool calls, wall clock |
| Thought blocks | folded, one per re-entry, each labelled with the turn's thinking share |
| Answers | headings, lists, tables, bold, italic, inline code and links, drawn when the turn ends |
| Rail | chats grouped by project, archive, fold state remembered |
| Code panel | on the right, mirrored from the rail: dragged between 260 and 720, folded from the title bar, width and state remembered. Starts at half the space beside the rail until somebody drags it once |
| Tool calls | top of the panel, one fold for the group and one per call. Open a call for its `arguments` and, under them, its `result` — 4,000 characters, the remainder counted. A failed call is marked on its head |
| Program code | under the calls, from `write_file` and `edit_file` only. The head is the **path**, the body the content — no JSON envelope. Readable while it is being written; the envelope is replaced once the arguments are whole |
| `clear all` | empties both halves and stays empty across a restart. The group's own `clear` takes the calls only |
| Code blocks | language, line count and `copy`. Fifteen lines or more can be folded away |

---
