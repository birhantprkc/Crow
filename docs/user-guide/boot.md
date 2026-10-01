[← README](../../README.md) · [Docs index](../README.md)

# Boot menu

```
python cli\crow_boot.py
```

Starts one operating point, then Crow. Everything a point needs (binary, env, argv, port, readiness, menu text) comes from `manifests/stack.json`.

| Menu entry | What it does |
|---|---|
| Start Crow | opens the window with `--base-url` of the running point. If no point is running, it says so and starts nothing |
| Qwen3.8-Flash-Next | starts crow-nest's `serve` with the Flash-Next container (200k context) |
| Qwen3.8-27B | starts `serve` with the 27B container (65,536 context) |
| Image Stack | starts the 27B, then `sd-server` with Qwen-Image 2.1 once `serve` is ready |
| Stop the running point | ends `serve` and `sd-server` and removes the contract file |
| Quit | leaves the menu. A running point keeps running |

| | |
|---|---|
| While starting | an animated line instead of the server log. Timeouts: Flash-Next 600 s, the 27B 300 s, `sd-server` 300 s more |
| Landed | shows for 5 s, then the menu comes back by itself |
| Failure or timeout | what was started is stopped, the last 10 log lines are shown |
| One point at a time | a second start is refused. The message names the running point and how to stop it: the menu entry, `--stop`, or `taskkill /PID <pid> /F` |
| A llama.cpp server | counts as running and blocks a start. The menu never stops it; the message gives its pid |
| Contract file | `%LOCALAPPDATA%\Crow\active-point.json` (`point`, `base_url`, `started_at`, `pids`). The window reads it |
| Logs | `runs\serve-8099.log` and `runs\sd-server-8097.log` under the folder the menu was started from (the shortcut starts it in the install folder). The previous run is kept as `.prev.log` |
| Without a modern console | plain ASCII, no colours, no animation. `NO_COLOR` drops only the colours |

## Flags

| Flag | |
|---|---|
| *(none)* | the menu |
| `--status` | what runs. Exit 0, or 1 when nothing runs |
| `--start <point>` | `flash-next`, `27b` or `image-stack`. No animation when the output is not a terminal |
| `--stop` | stop the running point |
| `--start-crow` | open the window on the running point |
| `--create-shortcut <folder>` | write `<folder>\Crow.lnk` (Windows) |
| `--install-root <dir>` | the install root `${INSTALL}` (default `%LOCALAPPDATA%\Crow`) |
| `--models <dir>` | the models root `${MODELS}` (default `%CROW_MODELS%`, else `<install>\models`) |
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
