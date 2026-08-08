"""Render the GitHub social preview card, 1280x640.

The card is a terminal, because the product is a terminal. Everything on it was
on a real screen: the wordmark is imported from `cli/crow.py` rather than
retyped, the colours are the ones the client asks the terminal for, the typeface
is the one that ships in the package, and the two timing lines are copied
verbatim from the first session run out of an installed copy on 2026-08-08.

The version it drew before this was a card ABOUT the software -- big number,
tagline, whitespace. It looked like every other generated tech banner, and it
showed nothing that could not have been invented. This one shows the thing
working, and the interesting part is a number in the second line.

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

BG     = "#0b0e17"   # the window background the client asks the terminal for
ACCENT = "#7eb0f8"   # the blue of the wordmark
BEVEL  = "#2c5bac"   # its shaded edge
TEXT   = "#e8edf6"
MUTED  = "#6f7a8b"
GREEN  = "#89d185"

# A real coding turn, not a greeting: the card has to show what the thing is
# for. Taken from the session of 2026-08-08 in which the client was asked how to
# count its own context window -- the answer it gave is the code that now counts
# it. The prompt is cut with an ellipsis; nothing else is edited.
#
# The last line is the whole point of the picture. Eighteen tokens re-read out
# of a four-thousand-token conversation: before that day it was all four
# thousand, and the first word arrived six and a half minutes later.
SESSION = [
    (MUTED,  "crow at http://127.0.0.1:8081/v1  (health: ok, 200k context)"),
    (None,   ""),
    (ACCENT, "you> take variant 2 and write me the finished function"),
    (None,   ""),
    (TEXT,   "crow> Here's the finished function, using the server's own"),
    (TEXT,   "      reported usage:"),
    (None,   ""),
    (TEXT,   "  def get_context_usage("),
    (TEXT,   "      server_response: dict,"),
    (TEXT,   "      max_ctx: int = 200_000,"),
    (TEXT,   "  ) -> int:"),
    (MUTED,  "  ..."),
    (None,   ""),
    (GREEN,  "[1262 tok @ 8.56 tok/s | prefill 18 | ttft 1.73s | thinking 44%]"),
]


def load(size, weight=400.0):
    font = ImageFont.truetype(str(FONT), size)
    try:
        # MONO=1 said out loud: a proportional instance would break the block
        # art, which is built out of cells that have to line up.
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
    left = 64

    # The wordmark as filled cells rather than glyphs: drawing the block
    # characters as text leaves hairline seams, because a glyph's ink is not
    # exactly its advance width.
    lines = [ln for ln in crow.BANNER.splitlines() if ln.strip() and "{version}" not in ln]
    indent = min(len(ln) - len(ln.lstrip()) for ln in lines)
    lines = [ln[indent:] for ln in lines]
    cw = ch = 9
    top = 44
    for row, line in enumerate(lines):
        for col, glyph in enumerate(line):
            if glyph == " ":
                continue
            colour = BEVEL if glyph == crow.BANNER_SHADE else ACCENT
            x0, y0 = left + col * cw, top + row * ch
            d.rectangle([x0, y0, x0 + cw - 1, y0 + ch - 1], fill=colour)

    d.text((left + len(lines[0]) * cw + 22, top + 16), f"v{crow.VERSION}",
           font=load(17), fill=MUTED)

    mono = load(19)
    y = top + len(lines) * ch + 34
    for colour, line in SESSION:
        if line:
            d.text((left, y), line, font=mono, fill=colour)
        y += 27

    # One sentence, and it is the only thing on the card that is not a screen.
    d.text((left, H - 92), "284 billion parameters on one graphics card.",
           font=load(25, 500.0), fill=TEXT)
    d.text((left, H - 56), "The experts stream off the SSD, and the prompt cache holds.",
           font=load(25), fill=MUTED)

    img.save(OUT, optimize=True)
    print(f"wrote {OUT}  {img.size[0]}x{img.size[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
