[← README](../../README.md) · [Docs index](../README.md)

# Boot menu

```
python cli\crow_boot.py
```

Starts one operating point, then Crow. The three crow-nest points are the baseline; everything they need (binary, env, argv, port, readiness, menu text) comes from `manifests/stack.json`. Crow's llama.cpp lines are optional and listed below them.

```
  🐦  C R O W  boot menu
  ----------------------------------------------------
  ⚪ No operating point is running.

   1  🚀 Start Crow                     open the window on the running point
   2  🧠 Qwen3.8-Flash-Next             200k context — great for coding & vision
   3  ⚡ Qwen3.8-27B                    128k context — great speed, awesome for coding & vision
   4  🎨 Image Stack                    27B + Qwen-Image 2.1 — create AWESOME pictures

  Optional (llama.cpp)
   5  🦙 Qwen3.8-27B (optional)         llama.cpp, port 8082
   6  🦙 Qwen3.8-Flash-Next (optional)  llama.cpp, port 8083

   7  🛑 Stop the running point
   0  👋 Quit
```

| Menu entry | What it does |
|---|---|
| Start Crow | opens the window with `--base-url` of what runs: a baseline point, or a llama-server (its port), and says which. Only when no model server runs it says so and starts nothing |
| Qwen3.8-Flash-Next | starts crow-nest's `serve` with the Flash-Next container (200k context) |
| Qwen3.8-27B | starts `serve` with the 27B container at 131,072 context (`CROW_CONTEXT`; 200k waits for an 8-bit KV cache in crow-nest) |
| Image Stack | starts the 27B at 65,536 context (room for Qwen-Image beside it), then `sd-server` with Qwen-Image 2.1 once `serve` is ready |
| *(optional)* lines | a llama.cpp line from `manifests/operating-point.json` (ports 8081/8082/8083), started like Crow's own `start_server`: ready when `/props` answers, 600 s. Drawn dimmed; plain consoles show only the tag |
| Stop the running point | ends every model server the process scan sees (`serve`, `sd-server`, `llama-server`), waits until each is torn down, removes the contract file |
| Quit | leaves the menu. A running point keeps running |

| | |
|---|---|
| While starting | an animated line instead of the server log. Timeouts: Flash-Next 600 s, the 27B 300 s, `sd-server` 300 s more, a llama.cpp line 600 s |
| Landed | shows for 5 s, then the menu comes back by itself |
| Failure or timeout | what was started is stopped, the last 10 log lines are shown |
| One point at a time | in both directions: a llama-server blocks a baseline start and a baseline point blocks an optional one. The message names what runs and how to stop it: the menu entry, `--stop`, or `taskkill /PID <pid> /F` |
| Which optional lines show | only those whose llama-server binary and GGUF are on disk (`%CROW_MODELS%` or `<install>\models`, `CROW_LLAMA_SERVER_<KEY>`); the others are left out |
| Contract file | `%LOCALAPPDATA%\Crow\active-point.json` (`point`, `base_url`, `started_at`, `pids`), for the baseline points only. The window reads it |
| Logs | `runs\serve-8099.log`, `runs\sd-server-8097.log`, `runs\llama-server-<port>.out.log` / `.err.log` under the folder the menu was started from (the shortcut starts it in the install folder). The previous run is kept as `.prev.log` |
| Without a modern console | plain ASCII, no colours, no animation. `NO_COLOR` drops only the colours |

## Flags

| Flag | |
|---|---|
| *(none)* | the menu |
| `--status` | what runs. Exit 0, or 1 when nothing runs |
| `--start <point>` | `flash-next`, `27b`, `image-stack`, or an optional line's key (`qwen35-q4-k-xl`, `flash-next-q2-k-xl`, `operating-point`). No animation when the output is not a terminal |
| `--stop` | stop the running point |
| `--start-crow` | open the window on the running point |
| `--create-shortcut <folder>` | write `<folder>\Crow.lnk` (Windows) |
| `--install-root <dir>` | the install root `${INSTALL}` (default `%LOCALAPPDATA%\Crow`) |
| `--models <dir>` | the models root `${MODELS}` (default `%CROW_MODELS%`, else `<install>\models`); also handed to the optional lines as `CROW_MODELS` |
| `--stack <file>` | another `stack.json` |

Exit codes: 0 done, 1 failed, 2 setup error (missing file, unknown point), 3 refused because a point already runs.

## Shortcut

```
python cli\crow_boot.py --create-shortcut "%USERPROFILE%\Desktop"
```

| | |
|---|---|
| Target | Windows Terminal (`wt.exe`) running this Python with `crow_boot.py`; plain `python.exe` when Windows Terminal is not installed |
| Working folder | the install root |
| Icon | `cli\crow.ico` |
| Other folders | any folder works: the Start menu is `%APPDATA%\Microsoft\Windows\Start Menu\Programs` |
