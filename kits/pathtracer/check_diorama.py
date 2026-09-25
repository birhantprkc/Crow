#!/usr/bin/env python3
"""Check a built voxel diorama page the way Crow sees it (#299).

A goal's `check:` command for the voxel kit. It renders the page through Crow's
own render_page (crow_core.tool_render_page: the GPU only, the frozen page
clock of #293), reads the `render: {json}` line and the kit's console probe
`[crow-scene] {json}`, and prints one PASS/FAIL line per check:

  gpu        both captures rendered on a GPU (render_mode "gpu", not SwiftShader/llvmpipe)
  probe      the kit's [crow-scene] line came back from both captures, in the asked mode
  samples    photo mode reached >= --min-samples path-tracer samples in --wait-ms
  props      >= --min-props named props (g.prop) with voxels
  kinds      >= --min-kinds distinct prop kinds ('rock 3' and 'rock' are one kind)
  voxels     >= --min-voxels voxels (main grid + parts)
  coverage   terrain coverage >= --min-coverage (share of the terrain's top cells
             with a prop above them; voxel-kit.js coverage())
  motion     the scene is animated and every pair of the 4 live frames differs in
             more than 0.5 % of its pixels (a pixel differs when a channel moves by
             more than 25 of 255 -- #293's precheck line)
  repeat     a second live capture is byte-identical frame by frame (the
             animation reads only the page clock); --no-repeat skips it
  photo      the photo frame is not near-uniform and < 90 % near-black (precheck)
  errors     no page error: no 'Uncaught' / [crow-error] console line, errors == 0,
             no setScene failure

Exit 0 only when every check passes; 1 when one fails; 2 on a setup error.

  check_diorama.py <built index.html> [--min-props 40] [--min-voxels 60000]
                   [--min-coverage 0.5] [--min-kinds 12] [--min-samples 200]
                   [--photo-t 2] [--wait-ms 20000] [--frame-ms 500] [--size 1024]
                   [--reference DIR] [--crow-cli DIR] [--no-repeat]

With --reference DIR it also writes <photo>-vs-reference.png (the photo beside
the reference images, same height) for a judge's read_image, and prints each
image's mean luma and saturation as INFO lines (no verdict).
"""

import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {"min_props": 40, "min_voxels": 60000, "min_coverage": 0.5, "min_kinds": 12,
            "min_samples": 200}
MOTION_PCT = 0.5          # crow_core._PRECHECK_MOTION_PCT
MOTION_DELTA = 25         # crow_core._PRECHECK_DELTA
DARK_MAX_PCT = 90.0       # crow_core._PRECHECK_DARK_WARN_PCT
SOFTWARE = re.compile(r"swiftshader|llvmpipe|software|softpipe|microsoft basic", re.I)


# ---- parsing (pure; the suite drives these with fake render results) --------

def parse_record(text):
    """The `render: {json}` line of a render_page result, or None."""
    for line in (text or "").splitlines():
        if line.startswith("render: "):
            try:
                return json.loads(line[len("render: "):])
            except ValueError:
                return None
    return None


def parse_probe(text):
    """The LAST `[crow-scene] {json}` console line of a result, or None."""
    found = None
    for line in (text or "").splitlines():
        i = line.find("[crow-scene] ")
        if i < 0:
            continue
        body = line[i + len("[crow-scene] "):].strip()
        # Chromium's log may close the line with `", source: file:///... (12)`.
        m = re.match(r"(\{.*\})", body)
        if not m:
            continue
        try:
            found = json.loads(m.group(1))
        except ValueError:
            continue
    return found


def page_errors(text):
    """Console lines of a result that are page errors."""
    out = []
    for line in (text or "").splitlines():
        if not line.startswith("console: "):
            continue
        if "Uncaught" in line or "[crow-error]" in line or "setScene failed" in line:
            out.append(line[len("console: "):][:200])
    return out


def pair_diffs(frames):
    """Changed-pixel percentage of every frame pair, from decoded frames given as
    lists of (r, g, b) samples of equal length; [(i, j, pct)]."""
    out = []
    for i in range(len(frames)):
        for j in range(i + 1, len(frames)):
            a, b = frames[i], frames[j]
            n = len(a) or 1
            changed = sum(1 for p, q in zip(a, b)
                          if p != q and max(abs(p[0] - q[0]), abs(p[1] - q[1]), abs(p[2] - q[2])) > MOTION_DELTA)
            out.append((i, j, round(100.0 * changed / n, 3)))
    return out


def evaluate(live, photo, opts, diffs=None, repeat=None):
    """[(name, ok, detail)] from two parsed captures.

    live / photo: {"text": result text, "record": parse_record, "probe": parse_probe}.
    diffs: pair_diffs of the live frames (None = not computable). repeat: None when
    skipped, else True/False (byte-identical second live capture)."""
    out = []
    lr, pr = live.get("record") or {}, photo.get("record") or {}
    lp, pp = live.get("probe"), photo.get("probe")

    def gpu_ok(rec):
        return rec.get("render_mode") == "gpu" and bool(rec.get("renderer")) \
            and not SOFTWARE.search(str(rec.get("renderer")))
    out.append(("gpu", gpu_ok(lr) and gpu_ok(pr),
                "live %s / photo %s: %s" % (lr.get("render_mode", "no record"), pr.get("render_mode", "no record"),
                                            str(pr.get("renderer") or lr.get("renderer") or lr.get("reason") or pr.get("reason"))[:120])))
    out.append(("probe", bool(lp) and bool(pp) and lp.get("mode") == "live" and pp.get("mode") == "photo",
                "live %s, photo %s" % ("mode=%s" % lp.get("mode") if lp else "NO [crow-scene] line",
                                       "mode=%s t=%s" % (pp.get("mode"), pp.get("t")) if pp else "NO [crow-scene] line")))
    scene = pp or lp or {}
    samples = (pp or {}).get("samples", 0) or 0
    out.append(("samples", samples >= opts["min_samples"],
                "%d samples (%s spp/s) in photo mode, need >= %d"
                % (samples, (pp or {}).get("samplesPerSecond", "?"), opts["min_samples"])))
    props = scene.get("props", 0) or 0
    out.append(("props", props >= opts["min_props"], "%d named props, need >= %d" % (props, opts["min_props"])))
    kinds = scene.get("propKinds", 0) or 0
    out.append(("kinds", kinds >= opts["min_kinds"], "%d prop kinds, need >= %d" % (kinds, opts["min_kinds"])))
    voxels = scene.get("voxels", 0) or 0
    out.append(("voxels", voxels >= opts["min_voxels"], "%d voxels, need >= %d" % (voxels, opts["min_voxels"])))
    cov = scene.get("coverage")
    out.append(("coverage", cov is not None and cov >= opts["min_coverage"],
                "coverage %s of %s terrain columns, need >= %s"
                % ("none (no terrain outside g.prop)" if cov is None else "%.3f" % cov,
                   scene.get("terrainColumns", "?"), opts["min_coverage"])))
    animated = bool((lp or {}).get("animated"))
    if diffs is None:
        out.append(("motion", False, "the live frames could not be compared (%d frames)"
                    % len(lr.get("frames") or [])))
    else:
        low = min((d[2] for d in diffs), default=0.0)
        ok = animated and len(diffs) >= 6 and low > MOTION_PCT
        out.append(("motion", ok, "animated=%s, %d parts; pairs changed %% min %.3f (need > %.1f): %s"
                    % (animated, (lp or {}).get("parts", 0), low, MOTION_PCT,
                       " ".join("%d-%d:%.2f" % (i + 1, j + 1, p) for i, j, p in diffs))))
    if repeat is not None:
        out.append(("repeat", bool(repeat), "second live capture byte-identical" if repeat
                    else "second live capture DIFFERS -- the animation reads something besides the page clock"))
    pre = pr.get("precheck") or {}
    ok = bool(pre) and not pre.get("uniform") and pre.get("dark_pct", 100.0) < DARK_MAX_PCT
    out.append(("photo", ok, "uniform=%s, %s colours, dark %s %%, clipped %s %%"
                % (pre.get("uniform"), pre.get("distinct_colours"), pre.get("dark_pct"), pre.get("clipped_pct"))
                if pre else "no precheck (no photo capture)"))
    errs = page_errors(live.get("text")) + page_errors(photo.get("text"))
    n = max((lp or {}).get("errors", 0) or 0, (pp or {}).get("errors", 0) or 0)
    failed = (pp or {}).get("error")
    out.append(("errors", not errs and n == 0 and not failed and bool(lp) and bool(pp),
                "; ".join(errs[:3]) or ("errors=%d" % n if n else "") or (str(failed)[:120] if failed else "")
                or ("no probe line" if not (lp and pp) else "none")))
    return out


# ---- rendering (needs crow_core) ---------------------------------------------

def load_core(cli_dir):
    """Import crow_core from `cli_dir`, or the cli beside this kit, or the
    installed one; raise ImportError with the places tried."""
    tried = [d for d in (cli_dir, os.path.normpath(os.path.join(HERE, "..", "..", "cli")),
                         os.path.expanduser("~/.local/share/crow/cli")) if d]
    for d in tried:
        if os.path.isfile(os.path.join(d, "crow_core.py")):
            sys.path.insert(0, d)
            import crow_core  # noqa: E402
            return crow_core
    raise ImportError("no crow_core.py in " + ", ".join(tried))


def capture(core, page, **kw):
    text = core.tool_render_page(page, **kw)
    return {"text": text, "record": parse_record(text), "probe": parse_probe(text)}


def frame_samples(core, paths):
    out = []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                img = core._png_pixels(fh.read())
        except OSError:
            img = None
        if img is None:
            return None
        out.append(core._frame_sample(img))
    if len({len(s) for s in out}) != 1:
        return None
    return out


def sha(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _scaled_rows(img, height):
    """Nearest-neighbour rows of `img` scaled to `height`, RGB bytes."""
    w, h, _c, ch, rows = img
    width = max(1, round(w * height / h))
    cols = [min(w - 1, int(x * w / width)) * ch for x in range(width)]
    out = []
    for y in range(height):
        src = rows[min(h - 1, int(y * h / height))]
        row = bytearray(3 * width)
        for k, c in enumerate(cols):
            if ch >= 3:
                row[3 * k:3 * k + 3] = src[c:c + 3]
            else:
                row[3 * k] = row[3 * k + 1] = row[3 * k + 2] = src[c]
        out.append(row)
    return width, out


def luma_sat(img):
    """(mean Rec.709 luma, mean HSV saturation) over a sample of the image."""
    w, h, _c, ch, rows = img
    step = max(1, (w * h) // 50000)
    lum = sat = n = 0
    for y in range(0, h, max(1, int(step ** 0.5))):
        r = rows[y]
        for x in range(0, w, max(1, int(step ** 0.5))):
            i = x * ch
            p = (r[i], r[i + 1], r[i + 2]) if ch >= 3 else (r[i],) * 3
            mx, mn = max(p), min(p)
            lum += 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]
            sat += (mx - mn) / mx if mx else 0
            n += 1
    return (lum / n if n else 0.0), (sat / n if n else 0.0)


def reference_sheet(core, photo_path, ref_dir, height=512):
    """Write <photo>-vs-reference.png; return (path, [(name, luma, sat)])."""
    imgs = []
    for name in [photo_path] + sorted(os.path.join(ref_dir, f) for f in os.listdir(ref_dir)
                                      if f.lower().endswith(".png")):
        try:
            with open(name, "rb") as fh:
                img = core._png_pixels(fh.read())
        except OSError:
            img = None
        if img is not None:
            imgs.append((name, img))
    stats = [(os.path.basename(n), *luma_sat(img)) for n, img in imgs]
    if not imgs:
        return None, stats
    parts = [_scaled_rows(img, height) for _n, img in imgs]
    width = sum(w for w, _ in parts)
    rows = [b"".join(bytes(p[1][y]) for p in parts) for y in range(height)]
    out = os.path.splitext(photo_path)[0] + "-vs-reference.png"
    with open(out, "wb") as fh:
        fh.write(core._png_encode(width, height, 2, rows))
    return out, stats


DEFAULT_SERVE = "http://127.0.0.1:8099/v1"


def bind_lend_spot(core, serve):
    """#304: give crow_core the endpoint a turn would have, so render_page can
    lend from a local serve. 'none' (or empty) leaves it unbound."""
    if not serve or str(serve).lower() == "none":
        return False
    if not hasattr(core, "_TURN_SPOT"):
        return False
    core._TURN_SPOT = {"base_url": serve, "remote": False}
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("page", help="the BUILT page (build_bundle's out), e.g. index.html")
    ap.add_argument("--min-props", type=int, default=DEFAULTS["min_props"])
    ap.add_argument("--min-voxels", type=int, default=DEFAULTS["min_voxels"])
    ap.add_argument("--min-coverage", type=float, default=DEFAULTS["min_coverage"])
    ap.add_argument("--min-kinds", type=int, default=DEFAULTS["min_kinds"])
    ap.add_argument("--min-samples", type=int, default=DEFAULTS["min_samples"])
    ap.add_argument("--photo-t", type=float, default=2.0, help="the pose of the photo, seconds")
    ap.add_argument("--wait-ms", type=int, default=20000, help="photo mode's real time to converge")
    ap.add_argument("--frame-ms", type=int, default=500, help="page time between the 4 live frames")
    ap.add_argument("--size", type=int, default=1024, help="viewport width = height (the canvas size)")
    ap.add_argument("--reference", help="folder of reference PNGs for a side-by-side sheet")
    ap.add_argument("--crow-cli", help="the cli folder holding crow_core.py")
    ap.add_argument("--no-repeat", action="store_true", help="skip the second live capture")
    ap.add_argument("--serve", default=os.environ.get("CROW_SERVE_URL", DEFAULT_SERVE),
                    help="the local crow-nest serve render_page may borrow VRAM from "
                         "(#117/#297); 'none' = never borrow")
    a = ap.parse_args(argv)

    page = os.path.abspath(a.page)
    if not os.path.isfile(page):
        print("SETUP ERROR: no such page: %s" % page)
        return 2
    try:
        core = load_core(a.crow_cli)
    except ImportError as exc:
        print("SETUP ERROR: %s" % exc)
        return 2
    core.set_root(os.path.dirname(page))
    # #304: this checker runs in its own process, outside any Crow turn, so
    # render_page had no endpoint to borrow VRAM from and refused every capture
    # with ENVIRONMENT while serve held the card (live 2026-09-25 15:3x, 280 MiB
    # free). Name the local serve the same way a turn does.
    bind_lend_spot(core, a.serve)
    name = os.path.basename(page)
    opts = {"min_props": a.min_props, "min_voxels": a.min_voxels, "min_coverage": a.min_coverage,
            "min_kinds": a.min_kinds, "min_samples": a.min_samples}

    live = capture(core, name + "?mode=live&ui=0", frames=4, frame_ms=a.frame_ms, wait_ms=2000,
                   width=a.size, height=a.size)
    frames = (live["record"] or {}).get("frames") or []
    samples = frame_samples(core, frames) if len(frames) == 4 else None
    diffs = pair_diffs(samples) if samples else None
    repeat = None
    if not a.no_repeat and len(frames) == 4:
        first = [sha(f) for f in frames]
        again = capture(core, name + "?mode=live&ui=0", frames=4, frame_ms=a.frame_ms, wait_ms=2000,
                        width=a.size, height=a.size)
        second = [sha(f) for f in ((again["record"] or {}).get("frames") or [])]
        repeat = len(second) == 4 and None not in first and first == second
    photo = capture(core, "%s?mode=photo&t=%g&ui=0" % (name, a.photo_t), wait_ms=a.wait_ms,
                    width=a.size, height=a.size)

    results = evaluate(live, photo, opts, diffs=diffs, repeat=repeat)
    for check, ok, detail in results:
        print("%-9s %s  %s" % (check, "PASS" if ok else "FAIL", detail), flush=True)
    pframes = (photo["record"] or {}).get("frames") or []
    print("INFO      live sheet: %s" % ((live["record"] or {}).get("contact_sheet") or "none"))
    print("INFO      photo: %s" % (pframes[0] if pframes else "none"))
    lp = live["probe"] or {}
    print("INFO      scene: %s" % json.dumps({k: lp.get(k) for k in ("voxels", "triangles", "props", "propKinds",
                                                                   "propVoxels", "coverage", "terrainColumns",
                                                                   "parts", "animated")}))
    if a.reference and pframes:
        if os.path.isdir(a.reference):
            out, stats = reference_sheet(core, pframes[0], a.reference)
            print("INFO      reference sheet: %s" % (out or "none (no readable PNG)"))
            for n, lum, sat in stats:
                print("INFO      %-40s mean luma %5.1f  mean saturation %.3f" % (n[:40], lum, sat))
        else:
            print("INFO      reference: no such folder %s" % a.reference)
    if not all(ok for _c, ok, _d in results):
        for label, cap in (("live", live), ("photo", photo)):
            if not cap["record"] or not cap["probe"]:
                print("--- %s result (head) ---\n%s" % (label, (cap["text"] or "")[:1500]))
    passed = sum(1 for _c, ok, _d in results if ok)
    print("RESULT: %d of %d checks pass" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
