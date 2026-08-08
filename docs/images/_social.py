"""Render the GitHub social preview card, 1280x640.

The card is the product's own screen rather than an illustration of it: the
banner comes from `cli/crow.py` instead of being retyped, the two blues are the
ones the client paints with, and the typeface is the one that ships in the
package. Nothing here is drawn by hand and nothing is decorative -- if a number
is on the card it is measured and it is in the README.

    python docs/images/_social.py

Why the banner is imported and not pasted: it is block art, six lines wide, and
a copy would drift the first time the wordmark is touched. The bevel cell is a
separate character precisely so it can be painted a few steps darker, which is
what makes the letters read as raised.
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

BG      = "#0b0e17"   # the window background the client asks the terminal for
ACCENT  = "#7eb0f8"   # the blue of the wordmark
BEVEL   = "#2c5bac"   # its shaded edge
TEXT    = "#e8edf6"
MUTED   = "#7c8798"
RULE    = "#1b2333"

# Measured on one machine, RTX 5090 at -c 200000 -ngl 99 -np 1. Every one of
# these is in the README with the issue it came from; none is rounded for looks.
FACTS = [
    ("284B",     "parameters"),
    ("13B",      "active per token"),
    ("200k",     "context, one slot"),
    ("1.28 GiB", "peak host RAM"),
    ("95.9 GiB", "model on disk"),
]


def load(size, weight=400.0):
    font = ImageFont.truetype(str(FONT), size)
    try:
        # The file is variable and its default instance is already MONO=1, but
        # say it out loud: a proportional instance would break the block art,
        # because the wordmark is built out of cells that have to line up.
        font.set_variation_by_axes([weight, 1.0])
    except Exception:
        pass
    return font


def main() -> int:
    if not FONT.exists():
        print(f"missing typeface: {FONT}", file=sys.stderr)
        return 2

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    left = 88

    # The wordmark as FILLED CELLS, not as glyphs. The block characters are
    # rectangles; drawing them as text leaves hairline seams between cells,
    # because a glyph's ink is not exactly its advance width. Rectangles at the
    # cell pitch tile the way a terminal grid does, which is what the art is for.
    lines = [ln for ln in crow.BANNER.splitlines() if ln.strip() and "{version}" not in ln]
    indent = min(len(ln) - len(ln.lstrip()) for ln in lines)
    lines = [ln[indent:] for ln in lines]
    cw, ch = 17, 17
    top = 74
    for row, line in enumerate(lines):
        for col, glyph in enumerate(line):
            if glyph == " ":
                continue
            colour = BEVEL if glyph == crow.BANNER_SHADE else ACCENT
            x0 = left + col * cw
            y0 = top + row * ch
            d.rectangle([x0, y0, x0 + cw - 1, y0 + ch - 1], fill=colour)

    y = top + len(lines) * ch + 22
    d.text((left, y), f"v{crow.VERSION}", font=load(20), fill=MUTED)

    claim = load(31, 500.0)
    d.text((left, y + 52), "A 284-billion-parameter coding model", font=claim, fill=TEXT)
    d.text((left, y + 96), "on one graphics card.", font=claim, fill=TEXT)
    d.text((left, y + 148), "The experts stream off the SSD.", font=load(27), fill=ACCENT)

    rule_y = H - 168
    d.line([(left, rule_y), (W - left, rule_y)], fill=RULE, width=2)

    big = load(29, 600.0)
    small = load(16)
    x = left
    for value, label in FACTS:
        d.text((x, rule_y + 30), value, font=big, fill=TEXT)
        d.text((x, rule_y + 70), label, font=small, fill=MUTED)
        x += max(d.textlength(value, font=big), d.textlength(label, font=small)) + 44

    d.text((left, H - 46), "no cluster   ·   no 200 GB host   ·   no cloud",
           font=small, fill=MUTED)

    img.save(OUT, optimize=True)
    print(f"wrote {OUT}  {img.size[0]}x{img.size[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
