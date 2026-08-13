#!/usr/bin/env python3
"""Measure the three things a tkinter GUI would stand on, before one is built.

WHY THIS EXISTS. #90 lists under "Not claimed": "That tkinter is the right
toolkit ... Nothing has compared it against the alternatives." Whoever builds
the sketch in that ticket one to one has made that decision without measuring
anything. This file is the measurement, and it is a SCRIPT rather than a ticket
comment because a comment cannot go red. A decision that rests on a run can be
re-run by the next person; one that rests on a remembered afternoon cannot.

WHAT WAS ALREADY KNOWN AND IS NOT THE QUESTION: whether Tk runs here at all.
`python -c "import tkinter"` succeeds in this repository, Tk 8.6.15 under
Python 3.13.3 [measured 2026-08-12]. That number is also the reference value
E13's installer preflight is written against - not "some Tk".

THE QUESTION THIS ANSWERS was narrower: does tkinter.font.families() carry a
PER-USER registered face, and under WHICH NAME. install_font() writes into HKCU
and the per-user store; all that was measured before today is that
System.Drawing.FontFamily::Families lists the face afterwards, and Tk takes a
different road to the font list. If Tk cannot see it, the surface language of
this candidate is not reachable with tkinter, and that is a reason to pick a
different toolkit rather than a detail to fix later.

THE ANSWER, measured 2026-08-13 on this machine: Tk lists it, under exactly
"Google Sans Code Monospace" - the same string cli/crow_core.py hands a
terminal in FONT_FAMILY. tkinter.font.families() returned 327 families and that
one is among them.

AND A TRAP FOUND ON THE WAY, which is why TK_FACE_LIMIT below is checked rather
than assumed: Tk gets its Windows face names through LOGFONT.lfFaceName and
they arrive CUT AT 31 CHARACTERS. The siblings show it in the same run -
"Google Sans Code Light Monospac", "Google Sans Code Medium Monospa", both
exactly 31, both cut mid-word. FONT_FAMILY is 26 and survives whole. A GUI that
later asks for "Google Sans Code Medium Monospace" (33) would be asking for a
name Tk never lists, and Tk answers a name it does not know with a substitute
face and no complaint at all.

THE THREE POINTS, all of them free:

  (i)   FAMILY. tkinter.font.families() lists the family cli/crow_core.py names,
        and the name is short enough that Windows did not cut it. The name Tk
        lists is printed, because that is the string crow_gui.py will ask for.
  (ii)  GLYPHS. Every character any surface writes is in the cmap of every face
        this repository ships, and the three ranges the existing comments record
        still measure what those comments say. This point went red on its first
        run and the finding is real: cli/crow.py prints U+2692 as its tool-call
        marker and neither shipped face has that glyph. See KNOWN_UNCOVERED.
  (iii) TK VERSION. The patchlevel Tk reports, held against the lower bound E13
        ships (Tk 8.6).

WHY THE GLYPH RUN READS THE FILE AND NOT THE FONT SYSTEM. Windows would answer
"can you draw U+2801" with a substitute face - it always has one - so asking it
measures the machine's font fallback, not the file this repository ships. The
cmap of the shipped .ttf is the only place where "the user who installed our
package has this glyph" is a fact rather than a hope.

WHY STRING LITERALS COUNT AND COMMENTS DO NOT, and the distinction is not
pedantry: cli/crow.py carries the braille cells (u+280B and friends) inside the
COMMENT that records why the spinner avoids them. A scan that read comments
would be red at the very sentence explaining the rule, and a checker that is
permanently red gets stopped rather than read. Docstrings ARE scanned - they
reach the screen through --help and /help, so a character in one can be drawn.

WHAT THIS TOOL DOES NOT DO: it does not install anything, register anything or
write anything. Two runs in a row produce the same bytes; that is checkable with
`diff` and it is meant to be checked.

Usage:  check_gui_prereqs.py [--repo <dir>] [--family <name>] [--min-tk <ver>]
        --family runs point (i) against a name of your choosing. That is the
        NEGATIVE CONTROL: point at a face that is not installed and the run has
        to say "not listed" and exit non-zero. A lookup that finds something
        whatever it is asked for is not checking anything.
        --min-tk is there for the self-test; the shipped bound is MIN_TK below.

Exit 0 = all three points hold.  1 = at least one does not.  2 = setup error.
"""

import argparse
import ast
import os
import struct
import sys

# BORROWED, NOT COPIED. `read` already exists next door, and SURFACE_DIRS is the
# next door's answer to "where can a surface live" - a second copy of that tuple
# here is exactly the second truth the whole rebuild is against. check_shared_core
# discovers surfaces rather than taking them from a manifest, and the reason it
# gives holds here too: the file nobody remembered to add to a hand-kept list is
# the second surface, and that is the file this repository is about to grow.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_operating_point import read  # noqa: E402
from check_shared_core import SURFACE_DIRS  # noqa: E402

# The lower bound E13 ships, decided there rather than left open: Tk 8.6.
# Reference value on this machine 8.6.15 under Python 3.13.3 [measured
# 2026-08-12]. Written here as a constant and not read from a manifest because
# the installer is the place that enforces it against a user's machine; this
# tool checks the machine the GUI is being written on.
MIN_TK = "8.6"

# Windows hands Tk its face names through LOGFONT.lfFaceName, 32 wide characters
# INCLUDING the terminating NUL, so 31 usable. See the module docstring for the
# two truncated siblings that measure it.
TK_FACE_LIMIT = 31

# The three ranges the existing comments record, with the numbers those comments
# state. cli/crow.py:233-234 says U+2580-259F is 32 of 32 and U+2500-257F is 128
# of 128; cli/crow.py:318-319 and cli/crow_core.py:2177-2182 say braille
# U+2800-28FF is 0 of 256, measured 2026-08-07 against Cascadia Mono as a
# control. Re-measured here per shipped face. A deviation does not mean the GUI
# broke - it means one of those comments no longer describes the file beside it,
# and those comments are cited as evidence in the plan and in the suite.
RECORDED_RANGES = (
    (0x2500, 0x257F, "box drawing", 128),
    (0x2580, 0x259F, "block elements", 32),
    (0x2800, 0x28FF, "braille", 0),
)

# WHAT POINT (ii) FOUND ON ITS FIRST RUN, declared here rather than swallowed or
# quietly fixed. cli/crow.py:875 and :903 print U+2692 as the tool-call marker,
# and neither shipped face has that glyph: coverage stops at U+25D9 plus U+FFFD,
# 674 codepoints in each file [measured 2026-08-13]. Windows answers a missing
# glyph with a substitute face - the exact fallback cli/crow.py:317-321 keeps the
# spinner away from braille to avoid. It is a CLI defect, it predates this stage,
# and repairing it here would be the stage that was sent to MEASURE the client
# editing what it prints. So it is declared: named, reasoned, and printed on
# every run.
#
# THE DECLARATION IS NOT A SILENCER, and this half is what keeps it from becoming
# one: an entry no surface writes any more, or one the font has since gained, is
# RED. A list of exemptions that only ever grows is a rug; check_shared_core
# carries the same symmetric rule against its manifest, for the same reason.
KNOWN_UNCOVERED = (
    (0x2692, "the tool-call marker in cli/crow.py - found by this stage, not "
             "fixed by it; it wants its own CLI issue under #90"),
)


def sfnt_tables(data):
    """The table directory of a TrueType/OpenType file as {tag: (offset, len)}.

    Standard library only, like everything else here: fontTools would answer
    this in one line and would be the first third-party dependency in the
    package. The two tables this needs are 12 bytes of header and 16 bytes per
    entry, which is cheaper than the import would be.
    """
    if len(data) < 12:
        raise ValueError("not a font file: %d bytes" % len(data))
    tag, num, _, _, _ = struct.unpack(">IHHHH", data[:12])
    if tag not in (0x00010000, 0x74727565, 0x4F54544F):  # ttf, 'true', 'OTTO'
        raise ValueError("unknown sfnt tag 0x%08X" % tag)
    out = {}
    for i in range(num):
        off = 12 + i * 16
        if off + 16 > len(data):
            raise ValueError("table directory runs past the end of the file")
        name, _, toff, tlen = struct.unpack(">4sIII", data[off:off + 16])
        out[name.decode("latin-1")] = (toff, tlen)
    return out


def _cmap_format4(data, off):
    """Segment-mapped, the BMP subtable every Windows font carries."""
    segx2 = struct.unpack(">H", data[off + 6:off + 8])[0]
    seg = segx2 // 2
    base = off + 14
    ends = struct.unpack(">%dH" % seg, data[base:base + segx2])
    starts = struct.unpack(">%dH" % seg, data[base + segx2 + 2:base + segx2 * 2 + 2])
    deltas = struct.unpack(">%dh" % seg, data[base + segx2 * 2 + 2:base + segx2 * 3 + 2])
    ro_off = base + segx2 * 3 + 2
    ranges = struct.unpack(">%dH" % seg, data[ro_off:ro_off + segx2])
    out = set()
    for i in range(seg):
        if starts[i] > ends[i]:
            continue
        for cp in range(starts[i], min(ends[i], 0xFFFF) + 1):
            if ranges[i] == 0:
                glyph = (cp + deltas[i]) & 0xFFFF
            else:
                # The idRangeOffset indirection, and it is measured from the
                # position of the entry itself - not from the start of the
                # table. Getting this wrong reports a font as covering
                # everything, which is a false GREEN and the failure this whole
                # file exists to avoid.
                gi = ro_off + i * 2 + ranges[i] + (cp - starts[i]) * 2
                if gi + 2 > len(data):
                    continue
                glyph = struct.unpack(">H", data[gi:gi + 2])[0]
                if glyph:
                    glyph = (glyph + deltas[i]) & 0xFFFF
            # Glyph 0 is .notdef. A codepoint that maps there is NOT covered,
            # and a reader that counted it would report a font as carrying
            # every character in the range it happens to span.
            if glyph:
                out.add(cp)
    return out


def _cmap_format12(data, off):
    """Segmented coverage beyond the BMP. Neither shipped face uses one today
    (both carry format 4 twice, platform 0/3 and 3/1, measured 2026-08-13), but
    a font with an astral range would be silently half-read without this."""
    ngroups = struct.unpack(">I", data[off + 12:off + 16])[0]
    out = set()
    for i in range(ngroups):
        s, e, g = struct.unpack(">III", data[off + 16 + i * 12:off + 16 + i * 12 + 12])
        if g == 0:
            continue
        out.update(range(s, e + 1))
    return out


def cmap_codepoints(data):
    """Every codepoint the file maps to a real glyph, out of every subtable.

    The union across subtables rather than one chosen subtable: a font may carry
    the same coverage twice under two platform ids, and picking "the right one"
    is a judgement this does not need to make. A format nobody here parses is
    skipped, and if that leaves NOTHING parsed the caller is told - an empty set
    read as "covers nothing" would report every character as missing, which is a
    false red, but an empty set read as an error is the truth.
    """
    tables = sfnt_tables(data)
    if "cmap" not in tables:
        raise ValueError("font has no cmap table")
    coff = tables["cmap"][0]
    ntab = struct.unpack(">H", data[coff + 2:coff + 4])[0]
    out, parsed, seen = set(), 0, []
    for i in range(ntab):
        pid, eid, soff = struct.unpack(">HHI", data[coff + 4 + i * 8:coff + 4 + i * 8 + 8])
        sub = coff + soff
        fmt = struct.unpack(">H", data[sub:sub + 2])[0]
        seen.append(fmt)
        if fmt == 4:
            out |= _cmap_format4(data, sub)
            parsed += 1
        elif fmt == 12:
            out |= _cmap_format12(data, sub)
            parsed += 1
    if not parsed:
        raise ValueError("no readable cmap subtable, formats present: %s"
                         % ", ".join(str(f) for f in seen))
    return out


def literal_chars(text):
    """Non-ASCII characters inside string literals, as {char: [line, ...]}.

    PARSED, NOT TOKENIZED, and the difference is a hole rather than a style
    preference: the token text of "\\u280b" is seven ASCII characters, so a scan
    of token text reports a braille spinner written that way as clean. The AST
    hands over the value, which is the cell that gets drawn. Case 5 of the
    self-test writes it exactly that way for this reason.

    Comments carry no AST node at all, which is the separation this rule needs
    anyway: cli/crow.py keeps its braille sample inside the comment that records
    why the spinner avoids braille, and a checker red at that sentence would be
    stopped rather than read. Docstrings DO carry one - they reach the screen
    through --help and /help, so a character in one can be drawn.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        # A file the parser cannot finish is not silently skipped: the caller
        # gets None and reports it, because "we could not look" and "we looked
        # and it was clean" are different answers and only one of them is green.
        return None
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        line = getattr(node, "lineno", 0)
        for ch in node.value:
            if ord(ch) > 0x7F:
                out.setdefault(ch, [])
                if line not in out[ch]:
                    out[ch].append(line)
    return {ch: sorted(lines) for ch, lines in out.items()}


def surface_files(repo):
    """Every client source under the surface directories, sorted, as (rel, path).

    Test files are left out on purpose: a suite is not a surface, and
    cli/test_crow.py quotes the wordmark inside an assertion. A directory that
    does not exist yet (gui/) is not an error - it is where the second surface
    lands, and this tool picks it up the day it does.
    """
    out = []
    for d in SURFACE_DIRS:
        full = os.path.join(repo, d)
        if not os.path.isdir(full):
            continue
        for name in sorted(os.listdir(full)):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            out.append((d + "/" + name, os.path.join(full, name)))
    return out


def version_at_least(patchlevel, minimum):
    """Compare only as many components as the bound names.

    "8.6.15" against a bound of "8.6" compares (8, 6) with (8, 6) and holds; a
    bound of "8.6.16" would compare all three and would not. Anything that is
    not a dotted number is False rather than an exception - an unreadable
    version is not a version that passed.
    """
    def parts(s):
        out = []
        for piece in str(s).split("."):
            digits = ""
            for ch in piece:
                if not ch.isdigit():
                    break
                digits += ch
            if not digits:
                return None
            out.append(int(digits))
        return tuple(out) if out else None

    got, want = parts(patchlevel), parts(minimum)
    if got is None or want is None:
        return False
    return got[:len(want)] >= want


def neighbours(family, families):
    """The families Tk lists whose name starts with the first word of the one
    asked for. This is what makes the negative control readable: against a face
    that is not installed the list is empty, and the report says so instead of
    just saying no."""
    head = family.split()[0].lower() if family.split() else ""
    if not head:
        return []
    return [f for f in families if f.lower().startswith(head)]


def family_problems(family, families):
    """Point (i) as a pure function, so it can be exercised without a display."""
    problems = []
    if len(family) > TK_FACE_LIMIT:
        problems.append("%r is %d characters; Windows hands Tk at most %d "
                        "(LOGFONT.lfFaceName), so Tk can never list it under "
                        "this name" % (family, len(family), TK_FACE_LIMIT))
    if family not in families:
        problems.append("tkinter.font.families() lists %d families, none of "
                        "them %r" % (len(families), family))
    return problems


def tk_probe():
    """Ask Tk itself, in a root that is never mapped.

    withdraw() immediately: the root exists the moment Tk() returns, and a
    checker that flashes a grey square across the screen is a checker people
    stop running. destroy() in a finally, so a raising families() call does not
    leave an interpreter with a live window behind this process.
    """
    import tkinter
    from tkinter import font as tkfont
    root = tkinter.Tk()
    try:
        root.withdraw()
        patchlevel = str(root.tk.call("info", "patchlevel"))
        # Sorted and de-duplicated: Tk returns the list in whatever order the
        # platform enumerator produced, and this report is diffed against a
        # second run of itself.
        families = tuple(sorted(set(str(f) for f in tkfont.families(root))))
    finally:
        root.destroy()
    return patchlevel, families


def check_glyphs(repo, faces):
    """Point (ii): coverage of what the surfaces write, and the recorded ranges.

    Returns (problems, notes). Both are lists of finished lines, so the caller
    prints and does not decide.
    """
    problems, notes = [], []

    sources = surface_files(repo)
    if not sources:
        problems.append("no surface source found in %s - nothing was checked"
                        % ", ".join(d + "/" for d in SURFACE_DIRS))
    wanted = {}
    for rel, path in sources:
        found = literal_chars(read(path))
        if found is None:
            problems.append("%s could not be tokenized - its literals were not "
                            "read" % rel)
            continue
        for ch, lines in found.items():
            wanted.setdefault(ch, [])
            # Every site, not just the first in the file: U+2692 is written at
            # cli/crow.py:875 AND :903, and a report that named one of them
            # would send the reader to fix half a defect.
            for line in lines:
                wanted[ch].append((rel, line))

    declared = dict(KNOWN_UNCOVERED)

    # The stale half of the declaration, checked once rather than per face: a
    # codepoint nobody writes any more has no business being exempt.
    for cp, why in sorted(declared.items()):
        if not any(ord(ch) == cp for ch in wanted):
            problems.append("U+%04X is declared in KNOWN_UNCOVERED (%s) and no "
                            "surface writes it any more - drop the declaration"
                            % (cp, why))

    for name, data in faces:
        try:
            covered = cmap_codepoints(data)
        except (ValueError, struct.error) as exc:
            problems.append("%s: %s" % (name, exc))
            continue

        # The other stale half: the font gained the glyph, so the exemption is
        # now a note about nothing.
        for cp, why in sorted(declared.items()):
            if cp in covered:
                problems.append("%s covers U+%04X, which is still declared in "
                                "KNOWN_UNCOVERED (%s) - drop the declaration"
                                % (name, cp, why))

        missing = sorted((ch for ch in wanted if ord(ch) not in covered),
                         key=ord)
        for ch in missing:
            # Capped, because a punctuation character can be written on fifty
            # lines and a report nobody can read is a report nobody reads. The
            # cap is on the PRINTED sites, never on the rule.
            sites = sorted(wanted[ch])
            where = ", ".join("%s:%d" % (rel, line) for rel, line in sites[:4])
            if len(sites) > 4:
                where += " and %d more" % (len(sites) - 4)
            line = "%s does not cover U+%04X %r, written at %s" % (
                name, ord(ch), ch, where)
            if ord(ch) in declared:
                notes.append(line + " [declared: %s]" % declared[ord(ch)])
            else:
                problems.append(line)
        if not [ch for ch in missing if ord(ch) not in declared]:
            notes.append("%s covers %d of the %d characters the surfaces write, "
                         "%d codepoints in the file"
                         % (name, len(wanted) - len(missing), len(wanted),
                            len(covered)))

        for lo, hi, label, recorded in RECORDED_RANGES:
            got = sum(1 for cp in range(lo, hi + 1) if cp in covered)
            total = hi - lo + 1
            if got != recorded:
                problems.append("%s: %s U+%04X-%04X is %d of %d, the comments "
                                "in cli/ record %d - one of them is now stale"
                                % (name, label, lo, hi, got, total, recorded))
            else:
                notes.append("%s: %s U+%04X-%04X %d of %d, as recorded"
                             % (name, label, lo, hi, got, total))
    return problems, notes


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--family", default=None,
                    help="face to look for; default is cli/crow_core.py's "
                         "FONT_FAMILY. Point it at something not installed to "
                         "run the negative control.")
    ap.add_argument("--min-tk", default=MIN_TK)
    args = ap.parse_args(argv[1:])

    # The family and the shipped faces come from the client, not from a second
    # list written here. A checker that carries its own copy of FONT_FAMILY goes
    # green while the two names drift apart, which is the exact defect the
    # comment above FONT_FAMILY was written after.
    sys.path.insert(0, os.path.join(args.repo, "cli"))
    try:
        import crow_core
    except ImportError as exc:
        print("SETUP ERROR: cannot import cli/crow_core.py from %s (%s)"
              % (args.repo, exc))
        return 2

    family = args.family or crow_core.FONT_FAMILY
    names = crow_core.font_files()
    if not names:
        print("SETUP ERROR: no font files under %s" % crow_core.FONT_DIR)
        return 2
    faces = []
    for name in names:
        path = os.path.join(crow_core.FONT_DIR, name)
        with open(path, "rb") as fh:
            faces.append((name, fh.read()))

    failed = 0
    print("crow GUI prerequisites")
    print()

    # (i) -----------------------------------------------------------------
    patchlevel, families, tk_error = None, (), None
    try:
        patchlevel, families = tk_probe()
    except Exception as exc:                      # noqa: BLE001 - see below
        # Deliberately broad: Tk answers a missing display with TclError, a
        # missing tcl library with ImportError, and a broken installation with
        # whatever the loader raises. All three are the same answer to the
        # question this tool asks - tkinter is not available here - and turning
        # any of them into a traceback would report the toolkit as unmeasured
        # rather than as unavailable.
        tk_error = "%s: %s" % (type(exc).__name__, exc)

    if tk_error is not None:
        failed += 1
        print("  FAILED   %-30s tkinter did not start" % "(i) family in Tk")
        print("             %s" % tk_error)
    else:
        problems = family_problems(family, families)
        if problems:
            failed += 1
            print("  FAILED   %-30s %r not usable" % ("(i) family in Tk", family))
            for p in problems:
                print("             %s" % p)
            near = neighbours(family, families)
            if near:
                print("             Tk does list, near that name:")
                for f in near:
                    print("               %-34s %d chars" % (f, len(f)))
            else:
                print("             and no family Tk lists begins with %r"
                      % (family.split()[0] if family.split() else family))
        else:
            print("  OK       %-30s %r, %d chars, out of %d families"
                  % ("(i) family in Tk", family, len(family), len(families)))
            near = [f for f in neighbours(family, families) if f != family]
            for f in near:
                mark = "  <-- cut at %d" % TK_FACE_LIMIT if len(f) == TK_FACE_LIMIT else ""
                print("             sibling %-34s %d chars%s" % (f, len(f), mark))

    # (ii) ----------------------------------------------------------------
    problems, notes = check_glyphs(args.repo, faces)
    if problems:
        failed += 1
        print("  FAILED   %-30s %d problem(s) over %d shipped face(s)"
              % ("(ii) glyphs the surface needs", len(problems), len(faces)))
        for p in problems:
            print("             %s" % p)
    else:
        print("  OK       %-30s %d shipped face(s), %d declared exception(s)"
              % ("(ii) glyphs the surface needs", len(faces),
                 len(KNOWN_UNCOVERED)))
    for n in notes:
        print("             %s" % n)

    # (iii) ---------------------------------------------------------------
    if tk_error is not None:
        failed += 1
        print("  FAILED   %-30s no Tk to ask" % "(iii) Tk version")
    elif not version_at_least(patchlevel, args.min_tk):
        failed += 1
        print("  FAILED   %-30s Tk %s is below the floor %s"
              % ("(iii) Tk version", patchlevel, args.min_tk))
    else:
        print("  OK       %-30s Tk %s, floor %s"
              % ("(iii) Tk version", patchlevel, args.min_tk))

    print()
    print("RESULT: %d of 3 prerequisites hold" % (3 - failed))
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        # The report prints the characters it found missing. Without this a
        # console on cp1252 raises UnicodeEncodeError on exactly the run that
        # had something to say.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
