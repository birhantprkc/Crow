"""Render the GitHub social preview card, 1280x640.

A social card is read at thumbnail size, in a feed, in under a second. That rules
out what the first two attempts did: one was a generic tech banner, the other a
wall of 19px monospace that turns to grey mush the moment a feed scales it down.

So this one carries three lines of large type, and beside them the one picture
that IS the idea: 256 experts per layer, six of them awake. At full size it is a
count; at thumbnail size it is a dark field with a few lit cells, which is what
the product does. The wordmark comes from `cli/crow.py` rather than being
retyped, and the colours are the ones the client asks the terminal for.

EVERY NUMBER HERE IS THE README'S. The headline is its H3 verbatim and the
figures are its stat table, because a card that drifts from the page it links to
is worse than no card. The version is read from crow.VERSION for the same
reason: the previous card shipped v0.0.3 into the 0.0.5 era, and it also still
claimed "1.28 GiB of RAM" -- a figure that had since been retired and the number
reused for the cost of a cache miss in milliseconds.

    python docs/images/_social.py
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "cli"))

import crow  # noqa: E402 - the single source of the wordmark

OUT = HERE / "social-preview.png"
FONT = REPO / "cli" / "fonts" / "GoogleSansCode[MONO,wght].ttf"

W, H = 1280, 640
M = 84                                   # margin, the same on every side

BG     = "#0b0e17"
ACCENT = "#7eb0f8"
BEVEL  = "#2c5bac"
TEXT   = "#f2f5fa"
MUTED  = "#7c8798"
RULE   = "#1c2536"
SLEEP  = "#161d2c"                       # an expert that is not awake this token

# README H3, verbatim. Three lines because a feed gives you one glance.
HEAD = [("284 billion parameters.", TEXT),
        ("One graphics card.",      TEXT),
        ("33 GB of system RAM.",    ACCENT)]

SUB = ["A 95.9 GiB mixture-of-experts model,",
       "read off the SSD while the GPU works."]

# README stat table. Each is in the README with the issue it came from.
FACTS = [("200k",  "context, one slot"),
         ("14.73", "tok/s decode, gate median"),
         ("0 EUR", "spent so far")]

# 6 of 256, the README's own figure. Fixed rather than random so the card is
# byte-reproducible -- a picture that changes on every run cannot be reviewed.
# None of them sits on the outer ring: a lit cell carries a one-pixel halo, and
# on the edge that halo is what pokes past the margin every other element keeps.
AWAKE = (35, 75, 118, 157, 194, 217)
GRID = 16                                # 16 x 16 = the 256 experts of a layer


def load(size, weight=400.0):
    font = ImageFont.truetype(str(FONT), size)
    try:
        font.set_variation_by_axes([weight, 1.0])
    except Exception:
        pass
    return font


def wordmark(d, x, y, cell):
    """The block art as filled cells. Glyphs leave seams; rectangles tile."""
    rows = [ln for ln in crow.BANNER.splitlines() if ln.strip() and "{version}" not in ln]
    indent = min(len(ln) - len(ln.lstrip()) for ln in rows)
    rows = [ln[indent:] for ln in rows]
    for r, line in enumerate(rows):
        for c, glyph in enumerate(line):
            if glyph == " ":
                continue
            colour = BEVEL if glyph == crow.BANNER_SHADE else ACCENT
            x0, y0 = x + c * cell, y + r * cell
            d.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=colour)
    return len(rows[0]) * cell, len(rows) * cell


def expert_grid(d, x, y, cell=13, gap=4):
    """256 cells, six lit. The same blocky vocabulary as the wordmark."""
    for i in range(GRID * GRID):
        r, c = divmod(i, GRID)
        x0, y0 = x + c * (cell + gap), y + r * (cell + gap)
        awake = i in AWAKE
        d.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1],
                    fill=ACCENT if awake else SLEEP)
        if awake:
            # A one-cell halo, so six dots survive a feed's downscale.
            d.rectangle([x0 - 2, y0 - 2, x0 + cell + 1, y0 + cell + 1],
                        outline=BEVEL, width=1)
    span = GRID * (cell + gap) - gap
    return span, span


def main() -> int:
    if not FONT.exists():
        print(f"missing typeface: {FONT}", file=sys.stderr)
        return 2

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    mark_w, mark_h = wordmark(d, M, M - 12, 8)
    d.text((M + mark_w + 20, M - 12 + mark_h - 22), f"v{crow.VERSION}",
           font=load(16), fill=MUTED)

    head = load(50, 600.0)
    y = M + 74
    for i, (line, colour) in enumerate(HEAD):
        d.text((M, y + i * 66), line, font=head, fill=colour)

    body = load(21)
    for i, line in enumerate(SUB):
        d.text((M, y + 216 + i * 30), line, font=body, fill=MUTED)

    # The picture, right of the type and clear of it: the longest headline line
    # ends near x=780 and the longest sub-line near x=540. Right-aligned to the
    # same margin as the rule below, so the block has an edge to sit against.
    span = GRID * (13 + 4) - 4
    gx, gy = W - M - span, y
    expert_grid(d, gx, gy)
    caption = load(15)
    for i, line in enumerate(("6 of 256 experts wake", "per layer, per token")):
        d.text((gx, gy + span + 16 + i * 21), line, font=caption, fill=MUTED)

    d.line([(M, H - 138), (W - M, H - 138)], fill=RULE, width=2)

    big, small = load(30, 600.0), load(16)
    x = M
    for value, label in FACTS:
        d.text((x, H - 112), value, font=big, fill=TEXT)
        d.text((x, H - 72),  label, font=small, fill=MUTED)
        x += max(d.textlength(value, font=big), d.textlength(label, font=small)) + 56

    url = load(19)
    d.text((W - M - d.textlength("github.com/nibor1896/Crow", font=url), H - 106),
           "github.com/nibor1896/Crow", font=url, fill=ACCENT)

    img.save(OUT, optimize=True)
    print(f"wrote {OUT}  {img.size[0]}x{img.size[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
