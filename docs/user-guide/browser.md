[← README](../../README.md) · [Docs index](../README.md)

# Browser panel (#175)

A globe in the title bar, beside the code and git buttons. That button is also the only way to
close it, as it is for its two neighbours. The open state survives a restart (`browser_open` in
`settings.json`).

## What it is

A **second web view with its own top-level browsing context**, not an iframe.

- **Linux (#201):** a second `WebKitWebView` *inside Crow's own GtkWindow*. pywebview's content
  is wrapped in a `GtkOverlay` at `before_show`, and the view is an overlay child placed on the
  CSS rect of `#brbody`. It is not a window, so `hyprctl clients` shows one Crow and nothing else,
  and the compositor has nothing to float, tile or centre.
- **Windows:** still a second frameless WebView2 window laid over the panel rect (#175). Moving it
  works there.

`CROW_PANE_WINDOW=1` brings back the old separate window on Linux for comparison.

| | |
|---|---|
| why not an iframe | `X-Frame-Options` / `frame-ancestors` refuse embedding. Measured 2026-09-23: 15 of 20 common sites refuse a foreign frame (#201) |
| own profile (#226) | cookies and storage in `~/.local/state/crow/browser`, separate from Crow's UI; logins survive a restart |
| memory ceiling (#226) | WebKit kills the panel's page above 2048 MB (checked every second) and the chat says so |
| sandbox (#226) | the panel's web process runs under `bwrap` when bwrap is installed (`CROW_PANE_SANDBOX=0` turns it off). `file://` pages still load |
| no bridge | foreign pages get no `window.pywebview` |
| geometry | the page reports the CSS rect of `#brbody`. On Linux that rect *is* the overlay position; on Windows Python adds the main window's corner |
| follows | a `ResizeObserver` on the rect (window size, grip, code panel). Measured under Broadway: the view matched `#brbody` exactly after a window resize and after the code panel took half the column |
| Crow's own sheets | when Settings or the context menu opens over the panel, the page is hidden until the sheet closes; a native view would otherwise cover them (Linux only) |
| one view for all tabs | switching tabs reloads the page |
| folded away | hidden, not destroyed, and **no page kept** (#279): the view goes to `about:blank`, so no WebGL keeps the card. Unfolding or restoring the window loads the tab's entry again (a render tab: its capture). A render while folded fills the render tab and does not unfold the panel |
| crash (#279) | a crashed page (`web-process-terminated`) is taken out of the window and the window is redrawn. A page someone was looking at is reloaded once; a second crash of the same load is not reloaded (#204). The line goes to `crow.log`, not the chat |
| during a turn (#279) | while a turn runs on the local model server, the panel's view uses WebKit's `hardware-acceleration-policy` `NEVER` (no GPU, CPU rendering; a WebGL page may look wrong or slow until the turn ends), then `ALWAYS` again. The chat page keeps the GPU. A remote endpoint leaves the panel alone |
| render_page (#279, #293) | an open panel, or one holding a page, counts as a second GPU client: render_page takes the card only with ≥ 1,536 MiB free instead of 512, and otherwise returns an ENVIRONMENT error with no image (it renders on the GPU only) |

## Controls

| | |
|---|---|
| `+` | new tab |
| tab `×` | close; the neighbour takes over, not the first |
| `‹` `›` | per-tab history, kept in a list. On Linux, links clicked inside the page, redirects and `pushState` land in it too (#227) |
| `⟳` | reload |
| address | a URL, a bare host (`example.com` → `https://`), or a local path (`C:\dir\page.html`, `/home/you/page.html` → `file:///`) |
| tab name | the hostname, or the file name for `file://`. The page title is cross-origin |

**One pane for all tabs**, so switching reloads the page. Measured 2026-09-23 (#201): a Chromium
tab per page grew by about 73 MB per page with no bound (271 → 1012 MB PSS over 10 pages). The one
Linux pane stayed flat at about 370 MB for Crow and the pane together after the fourth of 10
cross-site loads.

## Links

On Linux, a link in an answer opens as a **new tab in the panel** (#201). **Ctrl**-click or
middle-click still sends it to the system browser. The GitHub sign-in code always opens in the
system browser, because that is where you are signed in to GitHub. Inside the panel,
`target=_blank` and `window.open` load in the panel instead of starting the system browser (#227).
On Windows, links still open in the system browser (not verified there, #247).

## Width

The column's 612 px cap is lifted while the browser is open — a page at 612 is a mobile layout.
The bound is then `innerWidth − 560 − rail`: `#main` keeps its `min-width: 560px`, so the
composer still never overflows. With the browser folded away the cap is back at 612 for the code
panel.

## render_page

`render_page(path)` (class `executing`) shows its result in **one render tab**. Each render is a
new entry in that tab's history, not a new tab (#201). It shows **the screenshot**,
not the live page: the page can have changed since the model looked, and the chat would then
describe something else. The real address is in the bar — Enter loads it live.

See [tools](../reference/tools.md#render_page-175) for the flags, the caps and the measured
cases.

## Offline pages

A page opened from a file cannot load ES modules — the browser refuses every `import` between
local files. `build_bundle` turns the page and its modules into one self-contained file with the
esbuild already on the machine; render that file, not the module source. See
[tools](../reference/tools.md#build_bundle-212).

## Not verified on Windows (#247)

Development and acceptance of #201, #226, #228, #229 and #238 ran on Linux (Hyprland) only. The
Windows code paths have never run on Windows:

| | |
|---|---|
| the pane | Windows keeps the old second frameless WebView2 window (`on_top=True`). By the Win32 documentation that makes it TopMost over **every** application, not only over Crow; not observed |
| profile | the Windows pane shares the main window's WebView2 profile; #226's own profile, memory kill and sandbox are Linux only |
| copy and menu | Ctrl+C relies on WebView2 still passing editing keys with browser accelerators off (Microsoft's documentation); `Api.copy` goes through `clip`. Not run |
| Show in file manager | `explorer /select,<path>`; a path with a space is not verified |
| edge grips | one geometry call in flight with the newest rectangle (#238); not run |
