# Skills

Procedures the model keeps. Memory is what is **true**; a skill is what to **do**.

```
%LOCALAPPDATA%\Crow\skills\<name>\SKILL.md
---
name: start-llama-server
description: When Crow needs a local LLM (port 8082). Exact flags, the wait signal, the bind trap.
enabled: true
---
1. …
```

| | |
|---|---|
| In the prompt | name and description only, never the body |
| Body fetched with | `skill(action=read, name=…)`, one call |
| List limit | 2,000 chars for the **whole list**, 200 per description |
| Over the limit | the list says how many did not fit; it does not grow |
| `enabled` | in the file's own frontmatter. Absent means on |
| Written by | the same review at 0.20 / 0.50 / 0.75. One pass decides both |

## Creating one

Crow ships with `skill-creator` and reads it before it writes. Seeded once, on the first run that
has no skills directory; deleted, it stays deleted.

```
Read your skill "skill-creator" first and follow it.
Then save, as a skill, how to <do the thing>: <steps, flags verbatim, the trap>.
Tell me at the end which name and description you saved.
```

| what `skill-creator` enforces | |
|---|---|
| Save only what worked **here** | not a plan, not general knowledge |
| The description says **when** | it is all the prompt carries; a description of itself is never chosen |
| Name the job, not the topic | `run-a-measurement-series`, not `measurements` |
| Body | numbered steps, flags verbatim, what each step produces, the one trap that was hit |
| Rewrite under the same name | `save` replaces and keeps the on/off switch |
| Saying nothing | the normal outcome |

---
