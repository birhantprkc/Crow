#!/usr/bin/env python3
"""Hold the reasoning levels Crow OFFERS against the ones the model's template ACCEPTS.

WHY THIS EXISTS. On 2026-08-30 (#160) the flash-next manifest entry carried no
`reasoning_levels`, so `reasoning_levels_for` fell back to the union and the window
offered `max` on a model whose template answers `max` with HTTP 500. Nothing in the
repo could see that: the manifest is internally consistent, every suite was green,
and the only thing that knew was the running server. The entry's own note had said
so in words -- "one offered level can be fatal -- measure before offering" -- and
words are not a check.

THE FAILURE MODE THIS TOOL MUST NOT HAVE is the mirror of check_operating_point's:
it must not read a second copy of the manifest and compare it to the first. So it
asks the SERVER what each level renders, through /apply-template, and holds that
against what `crow_core` would put in the menu. One side comes off the disk, the
other off the wire.

BOTH DIRECTIONS ARE CHECKED, and the second is the one that keeps a fix honest:

    offered -> must render.        A level in the menu that 500s is #160 itself.
    dropped -> must still refuse.  A level left out because it was fatal has to be
                                   shown fatal AFTER the fix, or the fix is a rename.

A dropped level that renders is NOT a failure -- `xhigh` on the 27B renders and is
deliberately not offered, because it is `high` under another name. It is reported,
because a level that quietly started working is a manifest decision to revisit.

THE GROUPING IS CHECKED TOO, in both directions for the same reason: levels sharing
a `reasoning_groups` entry must render the same bytes, and levels in different
entries must not. A grouping nobody verifies is what makes the window promise a
free switch that costs a full prefill, or bill one that costs nothing.

THE TWO MESSAGES ARE THE ONES #160 MEASURED WITH ("S" and "U"), so the hashes this
prints compare directly against the sha256 values in the manifest's notes. Changing
them would not make the tool wrong, but it would make its output incomparable to
every number already written down.

Usage:  probe_reasoning_levels.py [--base URL] [--quiet]
        --base defaults to http://127.0.0.1:8083, the shipped operating point's port.

Exit 0 = the menu and the template agree.  1 = they do not.  2 = setup error.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli"))
import crow_core  # noqa: E402

# EVERY VALUE CROW CAN PRODUCE, not only the ones it offers today. The dropped half
# of the audit needs candidates to probe, and the union alone would never reach the
# strings #160 found fatal: `minimal` is not in REASONING_LEVELS, and an explicit
# `off` is not a level at all -- it is what a client sends if it mistakes Crow's
# "off" for a value rather than for the absence of the key.
CANDIDATES = tuple(dict.fromkeys(crow_core.REASONING_LEVELS
                                 + ("xhigh", "minimal", "none", "off")))

MESSAGES = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]

UNSET = "UNSET"


def render_over_http(base, timeout=60.0):
    """A renderer bound to a running server, for the real run.

    Returned as a closure rather than called directly so the audit below takes
    ANY renderer -- which is what lets its suite drive it without a server and
    show it going red, the one thing a checker has to be able to do.
    """
    def render(level):
        payload = {"messages": MESSAGES}
        if level is not None:
            payload["chat_template_kwargs"] = {"reasoning_effort": level}
        req = urllib.request.Request(base.rstrip("/") + "/apply-template",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                text = json.loads(fh.read().decode("utf-8")).get("prompt", "")
        except urllib.error.HTTPError as exc:
            return None, "HTTP %d" % exc.code
        return (hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], len(text)), ""
    return render


def model_of(base, timeout=15.0):
    """What the server has open, as the display name the manifest is keyed by."""
    with urllib.request.urlopen(base.rstrip("/") + "/props", timeout=timeout) as fh:
        props = json.loads(fh.read().decode("utf-8"))
    return crow_core.model_display_name(props.get("model_path") or "")


def audit(levels, groups, render, candidates=CANDIDATES):
    """Every judgement this tool makes. -> (rows, ok)

    A row is (verdict, subject, detail) where verdict is OK / FAIL / NOTE. `ok` is
    False as soon as one FAIL is in there; NOTE never changes it, because a NOTE is
    a fact for a person to decide about and a FAIL is a contradiction.
    """
    rows = []
    seen = {}

    def look(level):
        key = UNSET if level is None else level
        if key not in seen:
            seen[key] = render(level)
        return seen[key]

    # THE UNSET CASE FIRST, because it is the state every chat starts in and the
    # only one whose prompt is byte-identical to a client without this feature. If
    # it does not render, nothing else about the menu matters.
    shape, err = look(None)
    if shape is None:
        rows.append(("FAIL", UNSET, "the unset case does not render: %s" % err))
    else:
        rows.append(("OK", UNSET, "%s, %d chars" % shape))

    for level in levels:
        shape, err = look(level)
        if shape is None:
            rows.append(("FAIL", level,
                         "OFFERED and refused: %s -- the menu names a level that "
                         "kills the turn" % err))
        else:
            rows.append(("OK", level, "offered, renders %s, %d chars" % shape))

    for level in candidates:
        if level in levels:
            continue
        shape, err = look(level)
        if shape is None:
            rows.append(("OK", level, "not offered, still refused: %s" % err))
        else:
            rows.append(("NOTE", level,
                         "not offered but renders %s, %d chars -- either it is a "
                         "duplicate of an offered level or the entry is too narrow"
                         % shape))

    rows.extend(_group_rows(groups, look))
    return rows, not any(r[0] == "FAIL" for r in rows)


def _group_rows(groups, look):
    """Within a group the bytes must match; across groups they must differ.

    EMPTY MEANS UNMEASURED AND IS NOT A FAILURE. An entry without reasoning_groups
    gets the old behaviour -- one row per level, nothing collapsed -- and inventing
    a grouping here would be the one mistake that field exists to prevent.
    """
    if not groups:
        return [("NOTE", "groups", "none declared -- nothing to hold, the window "
                                   "collapses no rows for this model")]
    rows = []
    shape_of = {}
    for group in groups:
        shapes = {}
        for level in group:
            shape, err = look(None if level == "off" else level)
            shapes[level] = shape
            if shape is None:
                rows.append(("FAIL", "/".join(group),
                             "%s is grouped but does not render: %s" % (level, err)))
        distinct = {s for s in shapes.values() if s is not None}
        name = "/".join(group)
        if len(distinct) > 1:
            rows.append(("FAIL", name, "grouped together but render differently: %s"
                         % ", ".join("%s=%s" % (k, (v or ("-",))[0])
                                     for k, v in sorted(shapes.items()))))
        elif distinct:
            shape_of[name] = distinct.pop()
            rows.append(("OK", name, "one rendering, %s, %d chars" % shape_of[name]))

    names = sorted(shape_of)
    for i, one in enumerate(names):
        for other in names[i + 1:]:
            if shape_of[one] == shape_of[other]:
                rows.append(("FAIL", "%s vs %s" % (one, other),
                             "separate groups that render identically (%s) -- the "
                             "window promises a prefill for a switch that moves no "
                             "byte" % shape_of[one][0]))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8083",
                    help="server to ask, default the operating point's port")
    ap.add_argument("--quiet", action="store_true", help="only the result line")
    args = ap.parse_args(argv)

    try:
        model = model_of(args.base)
    except Exception as exc:                                          # noqa: BLE001
        print("SETUP: no server at %s (%s)" % (args.base, str(exc)[:70]))
        return 2
    if not model:
        print("SETUP: %s answers /props without a model_path" % args.base)
        return 2

    levels = crow_core.reasoning_levels_for(model)
    groups = crow_core.reasoning_groups_for(model)
    declared = crow_core.model_key_for(model)
    rows, ok = audit(levels, groups, render_over_http(args.base))

    if not args.quiet:
        print("server   %s" % args.base)
        print("model    %s -> %s" % (model, declared or "NO MANIFEST ENTRY"))
        print("offers   %s%s" % (", ".join(levels),
                                 "" if declared and levels != crow_core.REASONING_LEVELS
                                 else "   (the union fallback, not a measurement)"))
        print()
        for verdict, subject, detail in rows:
            print("  %-5s %-14s %s" % (verdict, subject, detail))
        print()
    notes = sum(1 for r in rows if r[0] == "NOTE")
    print("RESULT: %s -- %d checks, %d note%s"
          % ("the menu and the template agree" if ok else "THEY DISAGREE",
             sum(1 for r in rows if r[0] != "NOTE"), notes, "" if notes == 1 else "s"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
