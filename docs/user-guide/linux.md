# Linux

Same window, same core, same manifest. Ported and run on Arch (Omarchy), Hyprland 0.56.2 on
Wayland, RTX 5090, driver 610.57, Python 3.14. Nothing on this page needs root.

| | |
|---|---|
| Install | `bash install.sh` — five steps, a per-file sha256 manifest, idempotent |
| Window | WebKitGTK 4.1 through PyGObject, where Windows has WebView2 |
| Engine | built here, not downloaded: there is no Linux release asset |
| Clipboard | `wl-clipboard`; `xclip` is the X11 fallback |
| Models | `$CROW_MODELS`, else `<install>/models` |
| Source of truth | [`cli/crow_platform.py`](../../cli/crow_platform.py) — the one module that knows which OS this is |

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/nibor1896/Crow/main/install.sh | bash
```

or, from a checkout, the same script:

```bash
bash install.sh --models ~/Projects/models/qwen3.8-flash-next
```

| flag | |
|---|---|
| `--models DIR` | where the GGUFs live. Written into `$CROW_HOME/env`, which the launcher sources |
| `--voice` | also `faster-whisper` and `sounddevice` for the composer's microphone |
| `--build-engine` | build `llama-server` now instead of printing the line (~20 minutes) |
| `--no-desktop` | no `.desktop` entry, no icons, no Hyprland rule |
| `--no-engine` | do not look for the engine at all |
| `--to DIR` | install root, same as `CROW_HOME=DIR` |
| `--selftest` | drive the script's own checks, including the ones that must fail. Installs nothing |

It needs PyGObject from the distribution, because pip cannot build it here without the GObject
headers — so the installer **prints** the line and never runs it:

```bash
sudo pacman -S --needed python-gobject gtk3 webkit2gtk-4.1 wl-clipboard
```

The venv it creates is a `--system-site-packages` one, and that is the whole trick: pywebview
comes from pip, PyGObject and the typelibs come from pacman, and both have to be visible to one
interpreter. `pywebview[gtk]` is never installed — the extra pulls a PyGObject wheel that
shadows the distribution's build, which is the one with the typelibs beside it.

**Idempotent, and it removes nothing it did not install.** `$CROW_HOME/manifest.sha256` is
written at the end of every run and read at the start of the next, so a second run reports what
moved underneath it and replaces exactly that. The only files it deletes are the ones the
previous manifest listed and the new payload no longer ships — so `bin/`, `cuda/`, `src/`,
`build/`, `venv/` and a model tree beside them survive every re-run.

---

## Where things go

Every XDG variable is honoured when it is set and absolute. A relative value is treated as
unset, which is what the specification asks for.

| what | Linux | Windows, unchanged |
|---|---|---|
| install root | `${XDG_DATA_HOME:-~/.local/share}/crow` | `%LOCALAPPDATA%\Crow` |
| `secrets.json`, `settings.json`, `roots.json`, `USER.md`, `skills/`, `mcp.json`, `providers.json`, `approvals.json` | `~/.config/crow/` | `%LOCALAPPDATA%\Crow\` |
| `index.db` (session search) | `~/.local/share/crow/` | `%LOCALAPPDATA%\Crow\` |
| `session/`, `booted.json`, `git_events.json` | `~/.local/state/crow/` | `%LOCALAPPDATA%\Crow\` |
| `llama-server` boot logs | `~/.local/state/crow/log/` | `<cwd>\runs\` |
| models | `$CROW_MODELS`, else `<install>/models` | `<install>\models` |
| `llama-server` binary | `<install>/bin/`, then `PATH`, then `~/.local/share/crow/bin` | `<install>\bin\llama-server.exe` |
| fonts | `~/.local/share/fonts/crow/` + `fc-cache` | `%LOCALAPPDATA%\Microsoft\Windows\Fonts` + winreg |
| launcher | `$CROW_HOME/bin/crow`, symlinked into `~/.local/bin` if that is on `$PATH` | a Start-menu shortcut |
| desktop entry | `~/.local/share/applications/crow.desktop` | — |
| icons | `~/.local/share/icons/hicolor/<N>x<N>/apps/crow.png`, N = 16…512 | the packaged `.ico` |

---

## Models

```bash
hf download unsloth/Qwen3.8-Flash-Next-GGUF --include "*UD-Q2_K_XL*" --local-dir ~/Projects/models/qwen3.8-flash-next
hf download unsloth/Qwen3.8-Flash-Next-GGUF mmproj-F16.gguf --local-dir ~/Projects/models/qwen3.8-flash-next
```

The second line is the vision projector: it sits in the repository root, above the quant folder,
so the glob of the first walks past it. Without it the server boots the same model as a
text-only one and `read_image` refuses with a sentence. `hf` prints a tick even when it reached
nothing — check the byte counts (73.45 GiB over three shards, 904,004,000 B for the projector).

`$CROW_MODELS` is the whole mechanism and there is no second copy of it in a settings file: a
path two places can set is a path nobody can find. The installer writes it into
`$CROW_HOME/env`, which `$CROW_HOME/bin/crow` sources before the window starts — so a window
launched from the desktop entry, which inherits no shell profile, finds the same tree a terminal
does. Change it by re-running `install.sh --models DIR`, or by editing that one file.

The tree may be flat or nested: the core tries the manifest's path under the models root and
then the file's basename directly under it, so both
`<root>/qwen-next-gguf/UD-Q2_K_XL/…-00001-of-00003.gguf` and `<root>/…-00001-of-00003.gguf`
resolve.

---

## Engine

```bash
bash tools/build-llama-server.sh
```

There is no Linux release asset — the Windows package ships `llama-server.exe` — so the engine
is built from source, from the same three commits the operating point was measured at: llama.cpp
pin `6c84c7d5d` (PR #27742, `qwen4exp`), plus PR #27880 and PR #28040. It unpacks CUDA 13.3 and
cmake/ninja under `$CROW_HOME`, compiles for `sm_120`, and does not accept the result until
`ldd` resolves `libcudart`, `libcublas` and `libcublasLt` to that same prefix — mixing a
compiler of one CUDA major with another's runtime produces failures that look like a warmup
abort (upstream #28403, #25060).

| | |
|---|---|
| `JOBS=8` | gentler on the machine |
| `CLEAN=1` | throw the build directory away first |
| `LLAMA_PIN=<sha>` | move the pin; clear `LLAMA_PRS` with it |

`$CROW_HOME/cuda` must stay where it is: the binary reaches its CUDA libraries through a
`DT_RPATH` into that directory. Deleting it breaks the binary.

---

## The window

```bash
crow
```

It boots the server itself, from the model menu; the terminal client is never required. The
page, the tools, the memory and the browser pane are the ones [`window.md`](window.md)
describes. What Wayland makes different:

**It has to be told to float.** A Wayland client may not place, size or raise its own toplevel —
`gtk_window_move()` is a documented no-op, `set_keep_above()` does nothing, and a window created
at 500×250 came up tiled at 1261×688. Crow is frameless and its layout has a hard minimum of
1,130 px (520 rail + 560 chat + 50 column chrome), so tiled at a third of a screen the composer
is the first thing to go. The rule ships in both Hyprland dialects, because Hyprland picks its
parser from the config it finds:

| your config | what `install.sh` copies | the line to add, by hand |
|---|---|---|
| `~/.config/hypr/hyprland.lua` | `~/.config/hypr/crow.lua` | `require("hypr.crow")` |
| `~/.config/hypr/hyprland.conf` | `~/.config/hypr/crow.conf` | `source = ~/.config/hypr/crow.conf` |

Then `hyprctl reload`. The installer does not edit your config: a script that writes into
`hyprland.lua` has to parse it, and getting that wrong costs somebody their session on the next
reload.

**The title bar and the grips hand the gesture over.** `pywebview-drag-region` and
`window.move(x, y)` move nothing here, so a mousedown on the bar calls
`Gtk.Window.begin_move_drag` and one on any of the eight edge grips calls `begin_resize_drag`,
on the GTK main thread. The compositor runs the drag; Crow never computes a rectangle.

**The browser pane is a window of its own.** On Windows it is a second WebView2 over the panel
rect; under Wayland a child window cannot be glued to a parent's coordinates, so it is a
separate floating toplevel carrying the same `crow` app id — the float rule above catches it
too. `hl.window_rule({ ..., pin = true })` is commented out in `crow.lua`: "stays with me" is a
preference, not a requirement.

**The app id is `crow`.** `GLib.set_prgname("crow")` runs before the window opens, which is what
`xdg_toplevel.set_app_id` falls back to. Without it `hyprctl clients -j` reports the class as
`crow_gui.py`, the desktop entry matches nothing and the icon is generic. Under the X11 escape
hatch the same window is `Crow` — GTK capitalises `res_class` — so every rule matches
`^([Cc]row)$`.

---

## Clipboard

Pasting an image into the composer reads `wl-paste`, and `xclip` under X11. Without either, that
one gesture stops working; everything else is unaffected. It is in the pacman line above.

---

## Voice

```bash
bash install.sh --voice
```

`faster-whisper` and `sounddevice` into the same venv. The dictation model (`faster-whisper-small`,
~486 MB) is fetched by the window on the first click on the microphone, not by the installer.
`sounddevice` needs PortAudio from the distribution (`sudo pacman -S --needed portaudio`).

---

## Troubleshooting

| symptom | what it is | what to do |
|---|---|---|
| The process dies before anything appears, `Gdk-Message: Error 71 (Protocol error) dispatching to Wayland display` | WebKitGTK's DMA-BUF renderer turns on Wayland explicit sync and then commits a buffer without an acquire point; Hyprland answers with a protocol error and a protocol error kills the connection | Already handled: `cli/crow_gui.py` sets `__NV_DISABLE_EXPLICIT_SYNC=1` **at import**, before `webview` is loaded. If you start the module some other way, export it yourself |
| The window opens and stays blank | the same renderer, one layer down | `WEBKIT_DISABLE_DMABUF_RENDERER=1 crow` — it drops the accelerated path, which is why it is not the default |
| Anything that wants real window coordinates, or a window kept above the rest | Wayland does not offer either | `CROW_GDK_BACKEND=x11 crow` — it sets `GDK_BACKEND`, and under XWayland those work again. The app id becomes `Crow`; the shipped rule matches both spellings |
| The window is tiled and the composer is cut off | the float rule is not loaded | add the line from the table above and `hyprctl reload` |
| `crow: this window needs pywebview` | the venv is not the one the launcher points at | `bash install.sh` again — it reuses the venv and repairs the launcher |
| `no llama-server to run. Tried: …` | the engine has not been built | `bash tools/build-llama-server.sh` |
| `model 'flash-next-q2-k-xl' is not on disk. Tried: …` | `$CROW_MODELS` points somewhere else, or the download is not finished | the message names every path it tried; `install.sh --models DIR` rewrites the variable |
| A generic icon, or a window the launcher cannot name | the icon cache, or a `.desktop` entry from before the app id existed | `gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor`, then log out and in |
| `desktop-file-validate` hints about two main categories | `Categories=Development;Utility;` — the entry may appear in two menus | nothing. It is a hint, not an error |

---

## What was not verified

The pointer-driven drag and resize themselves: the bridge, the edge table and the GTK call are
covered by the suite, but synthesising a real button press against the compositor needs
privileges this machine does not have. The gestures were reasoned through and are unmeasured.
