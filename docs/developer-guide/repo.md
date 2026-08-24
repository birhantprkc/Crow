# Repo

| path | |
|---|---|
| `cli/crow.py` | terminal client |
| `cli/crow_gui.py` | window |
| `cli/crow_core.py` | conversation, request, SSE, tool loop, memory, skills, cost line |
| `tools/start-server.py` | model picker, becomes `llama-server` |
| `manifests/operating-point.json` | source of truth for every command line above |
| `tools/check_operating_point.py` | holds this file against that manifest |
| `docs/second-model.md` | the other server `install.ps1` sets up |

---
