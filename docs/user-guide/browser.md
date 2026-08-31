# Browser panel (#175)

A globe in the title bar, beside the code and git buttons. That button is also the only way to
close it, as it is for its two neighbours. The open state survives a restart (`browser_open` in
`settings.json`).

## What it is

A **second, frameless WebView2** laid over the panel rectangle — not an iframe.

| | |
|---|---|
| why not an iframe | `X-Frame-Options: DENY` and `frame-ancestors 'none'` refuse embedding. Measured 2026-08-31: claude.ai, github.com and google.com stayed blank; example.com, which sends no such header, loaded |
| why a window works | a window is a top-level browsing context. Neither header applies to one |
| geometry | the page reports the CSS rect of `#brbody`; Python adds the main window's corner. The main window is frameless, so its corner is the corner of the drawing area |
| follows | `moved`, `resized`, a `ResizeObserver` on the rect (grip, code panel), `minimized` hides, `restored` shows |
| folded away | hidden, not destroyed — a pane rebuilt from scratch loses the page somebody was on |

## Controls

| | |
|---|---|
| `+` | new tab |
| tab `×` | close; the neighbour takes over, not the first |
| `‹` `›` | per-tab history, kept in a list — `history.back()` on a foreign page throws cross-origin |
| `⟳` | reload |
| address | a URL, a bare host (`example.com` → `https://`), or a Windows path (`C:\dir\page.html` → `file:///`) |
| tab name | the hostname, or the file name for `file://`. The page title is cross-origin |

**One pane for all tabs**, so switching reloads the page. One window per tab would be one
WebView2 per tab in memory.

## Width

The column's 612 px cap is lifted while the browser is open — a page at 612 is a mobile layout.
The bound is then `innerWidth − 560 − rail`: `#main` keeps its `min-width: 560px`, so the
composer still never overflows. With the browser folded away the cap is back at 612 for the code
panel.

## render_page

`render_page(path)` (class `executing`) opens its result as a tab and shows **the screenshot**,
not the live page: the page can have changed since the model looked, and the chat would then
describe something else. The real address is in the bar — Enter loads it live.

See [tools](../reference/tools.md#render_page-175) for the flags, the caps and the measured
cases.
