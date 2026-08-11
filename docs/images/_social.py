"""Render the GitHub social preview card, 1280x640, from the raven photograph.

A social card is read at thumbnail size, in a feed, in under a second. The three
attempts before this one each failed that differently: a generic tech banner, a
wall of 19px monospace that turns to grey mush when a feed scales it down, and a
drawn card that carried the idea but no image anyone would stop for.

So the photograph carries the card and the type gets out of its way. Three rules
hold it together:

  1. NOTHING IS PASTED ON TOP. The type sits in a band that is the photograph
     itself, blurred and darkened with a mask that fades to nothing upward. A
     flat black bar reads as a sticker; a defocused continuation of the picture
     reads as depth. The band is where the rock already is, so nothing covers
     the bird.
  2. THE ACCENT IS SAMPLED, NOT PICKED. The moss and the sparks in the frame are
     the only saturated thing in it, so the accent is measured off the image
     rather than chosen beside it -- see `sample_accent`. Change the photograph
     and the palette follows it.
  3. THE TYPE IS SLIGHTLY TRANSPARENT. Pure white on a photograph looks stamped.
     Everything here draws at 82-94 % alpha so the grain underneath stays
     faintly visible, which is what makes it sit IN the image.

EVERY NUMBER HERE IS THE README'S, and the version comes from `crow.VERSION`
rather than being retyped: the card that shipped before v0.0.5 still claimed
v0.0.3, because a literal in a picture is a literal nobody greps.

    python docs/images/_social.py
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "cli"))

import crow  # noqa: E402 - the single source of the version

SRC = HERE / "Crow_social_new.jpg"
OUT = HERE / "social-preview.png"
FONT = REPO / "cli" / "fonts" / "GoogleSansCode[MONO,wght].ttf"

W, H = 1280, 640
SS = 2                                   # supersample, then LANCZOS down
M = 72                                   # margin

TEXT  = (238, 243, 250)
MUTED = (150, 163, 182)

# The band the type sits in. Everything below BAND_TOP is progressively
# defocused and darkened; above it the photograph is untouched.
BAND_TOP = 372
BAND_BLUR = 10                           # radius at 1x, scaled by SS
BAND_DARK = 0.84                         # peak darkening at the bottom edge
BAND_EASE = 1.6                          # >1 holds the blur back, then commits

# README H3, verbatim, re-wrapped to two lines.
HEAD = ["A 304-billion-parameter coding model, at a 200k context.",
        "One graphics card. 64 GB of system RAM."]

# README stat table, as one letterspaced strip. Read as texture at thumbnail
# size and as figures at full size -- which is the only thing a fact row can
# honestly do in a feed.
FACTS = ["304B PARAMETERS", "200K CONTEXT, ONE SLOT",
         "18.03 TOK/S DECODE", "0 EUR SPENT"]

# 6 of 256, the README's own figure, fixed rather than random so the card is
# byte-reproducible. Kept off the outer ring: a lit cell carries a halo, and on
# the edge that halo is what pokes past the margin everything else keeps.
AWAKE = (35, 75, 118, 157, 194, 217)
GRID = 16


def load(size, weight=400.0):
    font = ImageFont.truetype(str(FONT), size * SS)
    try:
        font.set_variation_by_axes([weight, 1.0])
    except Exception:
        pass
    return font


def tracked(d, xy, text, font, fill, track=0.0):
    """Draw with letterspacing. Pillow has no tracking, and at display sizes a
    monospaced face without it looks cramped rather than deliberate."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + track * SS
    return x - xy[0]


def tracked_width(d, text, font, track=0.0):
    return sum(d.textlength(c, font=font) + track * SS for c in text)


def sample_accent(img):
    """The greenest thing in the frame, normalised to a usable accent.

    The photograph is almost entirely blue-black; the moss and the sparks are
    the only saturated pixels in it. Averaging the top 0.05 % by green-excess
    gives a colour that provably belongs to the image instead of one picked to
    look good beside it. A fixed fallback keeps the card renderable if the
    photograph is ever replaced by something with no green in it at all.
    """
    small = img.convert("RGB").resize((320, 160), Image.BILINEAR)
    raw = small.tobytes()
    px = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    scored = sorted(px, key=lambda p: p[1] * 2 - p[0] - p[2], reverse=True)
    top = scored[: max(1, len(px) // 2000)]
    if top[0][1] * 2 - top[0][0] - top[0][2] < 40:
        return (122, 214, 122)
    r = sum(p[0] for p in top) // len(top)
    g = sum(p[1] for p in top) // len(top)
    b = sum(p[2] for p in top) // len(top)
    # Lift it to label brightness without letting it drift off its own hue.
    k = 235 / max(g, 1)
    return (min(255, int(r * k)), min(255, int(g * k)), min(255, int(b * k)))


def base_plate():
    """Crop the photograph to 2:1 and grade it.

    The frame is 16:9, so 2:1 costs 176 rows. They come off with an upward bias:
    the wing tips are 85 px from the top and the bottom is rock, so trimming
    evenly would clip the one silhouette the card exists for.
    """
    src = Image.open(SRC).convert("RGB")
    sw, sh = src.size
    ch = int(sw / 2)
    top = int((sh - ch) * 0.35)
    img = src.crop((0, top, sw, top + ch)).resize((W * SS, H * SS), Image.LANCZOS)

    # A dark, cool base under everything: the JPEG's blacks sit around 12-16 and
    # type at 82 % alpha needs them lower to hold contrast without a scrim.
    img = Image.blend(img, Image.new("RGB", img.size, (4, 7, 13)), 0.18)
    return img


def frosted_band(img):
    """Defocus and darken the lower band, with a mask that fades upward.

    This is the whole trick of the card. The mask is a vertical ramp, blurred so
    it has no visible start, so the transition happens over ~90 px of rock
    instead of on a line. Nothing about it is a rectangle the eye can find.

    The ramp is eased ABOVE linear on purpose. The bird's tail and the branch
    reach into the top of the band, and a linear ramp softens them enough to
    read as a mistake rather than as depth. Holding the blur back until the
    lower third puts the whole visible transition on rock.
    """
    blurred = img.filter(ImageFilter.GaussianBlur(BAND_BLUR * SS))

    ramp = Image.new("L", (1, H * SS), 0)
    for y in range(H * SS):
        t = (y - BAND_TOP * SS) / ((H - BAND_TOP) * SS)
        ramp.putpixel((0, y), 0 if t <= 0 else int(255 * min(1.0, t ** BAND_EASE)))
    mask = ramp.resize((W * SS, H * SS)).filter(ImageFilter.GaussianBlur(18 * SS))

    img = Image.composite(blurred, img, mask)

    dark = Image.new("RGB", img.size, (2, 4, 8))
    dmask = mask.point(lambda v: int(v * BAND_DARK))
    return Image.composite(dark, img, dmask)


def vignette(img):
    """Corner falloff, so the eye lands on the bird and not on an edge."""
    m = Image.new("L", (W * SS, H * SS), 0)
    ImageDraw.Draw(m).ellipse(
        (-int(W * 0.30) * SS, -int(H * 0.42) * SS,
         int(W * 1.30) * SS, int(H * 1.42) * SS), fill=255)
    m = m.filter(ImageFilter.GaussianBlur(90 * SS))
    return Image.composite(img, Image.new("RGB", img.size, (1, 2, 5)), m)


def expert_grid(d, x, y, accent, cell=4, gap=2):
    """256 cells, six lit -- the one picture that IS the idea, at the size a
    thumbnail can carry: not a count, just a dark field with a few live points."""
    for i in range(GRID * GRID):
        r, c = divmod(i, GRID)
        x0, y0 = (x + c * (cell + gap)) * SS, (y + r * (cell + gap)) * SS
        s = cell * SS
        if i in AWAKE:
            d.rectangle([x0 - SS, y0 - SS, x0 + s + SS, y0 + s + SS],
                        fill=accent + (70,))
            d.rectangle([x0, y0, x0 + s, y0 + s], fill=accent + (255,))
        else:
            # Faint enough to be texture rather than a grey tile in the sky:
            # what has to survive a thumbnail is the six lit cells, not the 250.
            d.rectangle([x0, y0, x0 + s, y0 + s], fill=(255, 255, 255, 20))
    return GRID * (cell + gap) - gap


def main() -> int:
    for path, what in ((SRC, "photograph"), (FONT, "typeface")):
        if not path.exists():
            print(f"missing {what}: {path}", file=sys.stderr)
            return 2

    plate = base_plate()
    accent = sample_accent(plate)
    img = vignette(frosted_band(plate)).convert("RGBA")

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # Top left: where the card goes when someone clicks it.
    tracked(d, (M * SS, (M - 26) * SS), "github.com/nibor1896/Crow",
            load(15), MUTED + (200,), track=1.1)

    # Top right: the expert grid, in the empty sky beside the wing.
    span = GRID * (4 + 2) - 2
    expert_grid(d, W - M - span, M - 30, accent)

    # The wordmark. 82 % alpha and wide tracking: at this size a solid white
    # word reads as a sticker laid on the photograph rather than part of it.
    mark = load(76, 700.0)
    tracked(d, (M * SS, 402 * SS), "CROW", mark, TEXT + (214,), track=9)

    # A short accent rule, sampled from the moss, keyed to the wordmark's left
    # edge -- the only saturated element on the card.
    d.rectangle([M * SS, 500 * SS, (M + 58) * SS, (500 + 3) * SS],
                fill=accent + (235,))

    head = load(21)
    for i, line in enumerate(HEAD):
        d.text((M * SS, (522 + i * 30) * SS), line, font=head, fill=TEXT + (206,))

    # The fact strip. Letterspaced small caps with a dim separator, so it reads
    # as one texture at thumbnail size and as four figures at full size.
    small = load(13, 500.0)
    x = M * SS
    for i, fact in enumerate(FACTS):
        if i:
            x += tracked(d, (x, 600 * SS), "  ·  ", small, MUTED + (120,), track=1.6)
        x += tracked(d, (x, 600 * SS), fact, small, MUTED + (225,), track=1.6)

    ver = load(13, 500.0)
    vtxt = f"v{crow.VERSION}"
    d.text(((W - M) * SS - tracked_width(d, vtxt, ver), 600 * SS),
           vtxt, font=ver, fill=MUTED + (190,))

    out = Image.alpha_composite(img, layer).convert("RGB")
    out = out.resize((W, H), Image.LANCZOS)
    out.save(OUT, optimize=True)
    print(f"wrote {OUT}  {out.size[0]}x{out.size[1]}  accent {accent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
