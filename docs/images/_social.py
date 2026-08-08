"""Render the GitHub social preview card, 1280x640.

A social card is read at thumbnail size, in a feed, in under a second. That rules
out what the first two attempts did: one was a generic tech banner, the other a
wall of 19px monospace that turns to grey mush the moment a feed scales it down.

So this one carries three lines of large type and one contrast, because the
contrast IS the story -- a 95.9 GiB model held in 1.28 GiB of host memory. The
wordmark comes from `cli/crow.py` rather than being retyped, and the colours are
the ones the client asks the terminal for.

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

# Measured on one machine, RTX 5090 at -c 200000 -ngl 99 -np 1. Each of these is
# in the README with the issue it came from.
FACTS = [
    ("200k",     "context, one slot"),
    ("12.08",    "tok/s, gate median"),
    ("0 EUR",    "spent so far"),
]


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


def main() -> int:
    if not FONT.exists():
        print(f"missing typeface: {FONT}", file=sys.stderr)
        return 2

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    mark_w, mark_h = wordmark(d, M, M - 12, 8)
    d.text((M + mark_w + 20, M - 12 + mark_h - 22), f"v{crow.VERSION}",
           font=load(16), fill=MUTED)

    # The headline. Three lines, and the third is the one that makes people
    # look twice -- a 95.9 GiB model that the host machine never holds.
    head = load(50, 600.0)
    y = M + 74
    d.text((M, y),      "284 billion parameters.", font=head, fill=TEXT)
    d.text((M, y + 66), "One graphics card.",      font=head, fill=TEXT)
    d.text((M, y + 132), "1.28 GiB of RAM.",       font=head, fill=ACCENT)

    body = load(21)
    d.text((M, y + 216),
           "A 95.9 GiB mixture-of-experts model, with the experts read", font=body, fill=MUTED)
    d.text((M, y + 246),
           "off the SSD while the GPU is still working.", font=body, fill=MUTED)

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
