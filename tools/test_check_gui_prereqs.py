#!/usr/bin/env python3
"""Negative control for check_gui_prereqs.py.

The stage this belongs to decides a toolkit, and a decision is worth what its
measurement is worth. A lookup that finds something whatever it is asked for
measures nothing, so every case below breaks exactly ONE of the three points and
requires the tool to go red and to NAME it - plus the two cases that must stay
green, because a checker that is red at the wrong thing gets stopped rather than
read.

CASE 5 IS THE ONE THAT PAID FOR ITSELF. The fixture writes its braille cell as
"\\u280b" - seven ASCII characters in the file. The first version of the tool
scanned TOKEN TEXT and would have called that fixture clean; worse, Python 3.12
splits f-strings into FSTRING_START/MIDDLE/END, so a token scan misses every
character inside an f-string, which is where cli/crow.py:875 keeps the marker
that turned out to be uncovered. The tool parses now. This case is what says so.

CASE 6 IS ITS PAIR AND IS NOT OPTIONAL. The same braille cell inside a COMMENT
must stay green: cli/crow.py:317-321 records the braille measurement WITH the
sample cells in it, and a checker red at that sentence would be a checker
someone deletes.

CASE 7 GUARDS THE ESCAPE HATCH. KNOWN_UNCOVERED lets a found-but-not-fixed
defect be declared instead of silently swallowed; an entry nobody writes any
more has to be red, or the list quietly becomes a rug.

Usage:  test_check_gui_prereqs.py
Exit 0 = every case behaved as required.  1 = at least one did not.
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(HERE, "check_gui_prereqs.py")

sys.path.insert(0, HERE)
import check_gui_prereqs as tool  # noqa: E402

# A braille cell, written the way a spinner would be written by someone who
# copied it out of a blog post. Kept as an escape ON PURPOSE - see case 5.
BRAILLE_LITERAL = 'SPINNER = "\\u280b"\n'
BRAILLE_COMMENT = '# not braille (\\u280b), which this font does not carry\n'


def run(*args):
    """The tool as a user runs it, decoded the way it writes."""
    proc = subprocess.run([sys.executable, TOOL] + list(args),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout + proc.stderr


def fixture_repo(tmp, extra_name=None, extra_text=None):
    """A throwaway repository: the real surfaces, the real faces, one new file.

    The whole of cli/ rather than a hand-built stub, and the first draft of this
    file learned why the hard way. It copied crow_core.py and the fonts only -
    and the run went red on a case that was supposed to be GREEN, because
    KNOWN_UNCOVERED declares U+2692 and no surface in that stripped repository
    wrote it any more. The stale-declaration rule was right; the fixture was
    lying about the repository. The suite files are left out, since they are not
    surfaces and the tool does not read them either.

    Everything is a copy, so no case can touch the tree it is measuring.
    """
    cli = os.path.join(tmp, "cli")
    os.makedirs(cli, exist_ok=True)
    for name in sorted(os.listdir(os.path.join(REPO, "cli"))):
        if name.endswith(".py") and not name.startswith("test_"):
            shutil.copy2(os.path.join(REPO, "cli", name), cli)
    shutil.copytree(os.path.join(REPO, "cli", "fonts"),
                    os.path.join(cli, "fonts"), dirs_exist_ok=True)
    if extra_name:
        with open(os.path.join(cli, extra_name), "w", encoding="utf-8") as fh:
            fh.write(extra_text)
    return tmp


def main():
    fail = 0

    def case(num, what, ok, detail=""):
        nonlocal fail
        if ok:
            print("  OK       case %d  %s" % (num, what))
        else:
            fail += 1
            print("  FAILED   case %d  %s" % (num, what))
            for line in detail.strip().splitlines():
                print("             %s" % line)

    # 1 -- the positive. Without it the seven red cases below would also pass on
    # a tool that is red at everything.
    code, out = run()
    case(1, "this repository: all three points hold",
         code == 0 and "RESULT: 3 of 3" in out, out[-800:])

    # 2 -- point (i), the control the stage names: a face that is not installed.
    code, out = run("--family", "Nonexistent Face 12345")
    case(2, "(i) a face nobody installed is not found",
         code == 1 and "none of them 'Nonexistent Face 12345'" in out
         and "FAILED   (i)" in out, out[-800:])

    # 3 -- point (i), the trap this stage found: Windows cuts face names at 31
    # characters, so a 33-character name can never be listed under that name.
    code, out = run("--family", "Google Sans Code Medium Monospace")
    case(3, "(i) a 33-character face name is named as too long",
         code == 1 and "is 33 characters" in out and "lfFaceName" in out,
         out[-800:])

    # 4 -- point (iii), and it must be the ONLY point that falls: a floor above
    # what is installed says nothing about the font.
    code, out = run("--min-tk", "99.0")
    case(4, "(iii) a floor above the installed Tk goes red, alone",
         code == 1 and "below the floor 99.0" in out
         and "OK       (i)" in out and "OK       (ii)" in out, out[-800:])

    # 5 -- point (ii), written as an escape so a token scan would miss it.
    with tempfile.TemporaryDirectory() as tmp:
        fixture_repo(tmp, "crow_spin.py", BRAILLE_LITERAL)
        code, out = run("--repo", tmp)
        case(5, "(ii) a braille cell written as \\u280b is caught",
             code == 1 and "U+280B" in out and "FAILED   (ii)" in out
             and "crow_spin.py:1" in out, out[-800:])

    # 6 -- and the same cell in a comment must NOT be caught.
    with tempfile.TemporaryDirectory() as tmp:
        fixture_repo(tmp, "crow_spin.py", BRAILLE_COMMENT)
        code, out = run("--repo", tmp)
        case(6, "(ii) the same cell in a comment stays green",
             code == 0 and "U+280B" not in out, out[-800:])

    # 7 -- the escape hatch, in process: a declaration nobody writes is red.
    faces = []
    fdir = os.path.join(REPO, "cli", "fonts")
    for name in sorted(os.listdir(fdir)):
        if name.lower().endswith((".ttf", ".otf")):
            with open(os.path.join(fdir, name), "rb") as fh:
                faces.append((name, fh.read()))
    keep = tool.KNOWN_UNCOVERED
    try:
        tool.KNOWN_UNCOVERED = ((0x2603, "a snowman no surface has ever drawn"),)
        problems, _ = tool.check_glyphs(REPO, faces)
    finally:
        tool.KNOWN_UNCOVERED = keep
    case(7, "(ii) a declaration no surface writes any more is red",
         any("U+2603" in p and "drop the declaration" in p for p in problems),
         "\n".join(problems) or "no problem reported at all")

    # 8 -- the stage's own idempotence probe, kept rather than remembered.
    code_a, out_a = run()
    code_b, out_b = run()
    case(8, "two runs produce the same bytes",
         code_a == code_b and out_a == out_b,
         "the two runs differ")

    print()
    print("RESULT: PASS - the toolkit measurement can go red at each of its "
          "three points, and stays green at prose"
          if not fail else
          "RESULT: FAIL - see the FAILED lines above")
    return 1 if fail else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
