"""Does every label in a hand-written SVG fit the box it sits in?

WHY IT EXISTS. Editing text in architecture.svg is editing a fixed-width layout with no reflow: a
label that grows past its rectangle simply draws over the border, and nothing in the file complains.
On 2026-08-09 three edits did exactly that and the defect was found by a human looking at the
picture, twice.

WHY IT MEASURES INSTEAD OF ESTIMATING. A first version multiplied the character count by an average
width. That is wrong by 30 % between "MMM" and "iii", and it also picked the wrong box: it took the
last rectangle whose x-range contained the text, which on this file is one of the 30px expert
squares. Both mistakes made it report 50 false positives - a checker that cries wolf is worse than
none. This one asks the real font for the real string, and finds the box by x AND y.

    python tools/check-svg-fit.py docs/images/architecture.svg
    python tools/check-svg-fit.py --selftest

Exit 0 = everything fits. 1 = at least one label overflows. 2 = setup error.
"""

import re
import sys
from pathlib import Path

try:
    from PIL import ImageFont
except ImportError:
    print("SETUP ERROR: Pillow is required (pip install pillow)")
    raise SystemExit(2)

# The stylesheet's font sizes, by class. Read from the file rather than assumed, because a class
# whose size changes and is not mirrored here would silently pass.
FONT = r"C:\Windows\Fonts\segoeui.ttf"
BOLD = r"C:\Windows\Fonts\segoeuib.ttf"
PAD = 16          # what a label needs to clear the rounded border on each side


def font_sizes(svg: str) -> dict:
    out = {}
    for cls, body in re.findall(r"\.(\w+)\s*\{([^}]*)\}", svg):
        m = re.search(r"font-size:\s*(\d+)px", body)
        if m:
            w = re.search(r"font-weight:\s*(\d+|bold)", body)
        out[cls] = (int(m.group(1)), bool(w) and w.group(1) in ("bold", "600", "700", "800", "900"))
    return out


def boxes(svg: str):
    """Every rect as (x, y, w, h). Order preserved; the smallest containing one wins."""
    return [tuple(int(v) for v in m)
            for m in re.findall(r'<rect x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', svg)]


def containing(bs, x, y):
    """Smallest box containing the point, or None. Size is the tie-breaker: a label sits in its
    own card, not in the zone that card is drawn on."""
    hit = [b for b in bs if b[0] <= x <= b[0] + b[2] and b[1] <= y <= b[1] + b[3]]
    return min(hit, key=lambda b: b[2] * b[3]) if hit else None


def check(path: Path):
    svg = path.read_text(encoding="utf-8")
    sizes, bs = font_sizes(svg), boxes(svg)
    bad = 0
    n = 0
    for m in re.finditer(r'<text x="(\d+)" y="(\d+)"[^>]*?class="(\w+)"[^>]*>([^<]+)</text>', svg):
        x, y, cls, txt = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        if cls not in sizes:
            continue
        size, is_bold = sizes[cls]
        box = containing(bs, x, y)
        if box is None:
            continue
        n += 1
        f = ImageFont.truetype(BOLD if is_bold else FONT, size)
        w = f.getlength(txt)
        avail = box[2] - 2 * PAD
        if w > avail:
            bad += 1
            print(f"  OVERFLOW  {w:5.0f}px in {avail}px  (.{cls}, box {box[2]}px)  {txt[:66]}")
    print(f"\n{n} labels checked, {bad} overflowing")
    return 1 if bad else 0


def selftest() -> int:
    """The checker has to be able to go red, and to find the RIGHT box."""
    svg = """<svg><defs><style>.sub{font-size:17px;fill:#000;}.tb{font-size:21px;font-weight:600;}</style></defs>
  <rect x="0" y="0" width="1200" height="900"/>
  <rect x="66" y="100" width="200" height="60"/>
  <text x="166" y="130" class="sub">short</text>
  <text x="166" y="150" class="sub">a label far too long for two hundred pixels of card</text>
  <text x="600" y="500" class="tb">on the outer box only</text>
</svg>"""
    p = Path("__fit_selftest.svg")
    p.write_text(svg, encoding="utf-8")
    try:
        sizes, bs = font_sizes(svg), boxes(svg)
        ok = 0
        fail = 0

        def c(name, want, got):
            nonlocal ok, fail
            if want == got:
                ok += 1
                print(f"  PASS  {name}")
            else:
                fail += 1
                print(f"  FAIL  {name}  want={want} got={got}")

        c("font sizes read from the stylesheet", (17, False), sizes["sub"])
        c("bold detected", True, sizes["tb"][1])
        # The inner card must win over the 1200px page rect - that was the original defect.
        c("smallest containing box wins", (66, 100, 200, 60), containing(bs, 166, 130))
        c("a point outside every card falls back to the page", (0, 0, 1200, 900), containing(bs, 600, 500))
        c("a point outside everything is None", None, containing(bs, 5000, 5000))
        rc = check(p)
        c("the long label is caught", 1, rc)
        print(f"\n{ok} passed, {fail} failed")
        return 1 if fail else 0
    finally:
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(check(Path(sys.argv[1])))
