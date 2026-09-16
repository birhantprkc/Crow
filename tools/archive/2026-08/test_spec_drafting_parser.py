#!/usr/bin/env python
"""Contract for read_mean_lengths in probe-spec-drafting.py.

WHY THIS EXISTS, and it is a measured silent failure. The mean accepted length is
printed by the server on every request that drafted anything, and the E11 run
extracted it from 0 of 3 runs while the lines were plainly in the log. The regex was
never the problem: the decode was. A server log at -lv 5 carries raw token bytes, and
one invalid sequence made strict utf-8 AND utf-16 both fail, after which the function
returned [] - the same value it returns when the server drafted nothing at all.

    A PARSER THAT RETURNS "NOTHING FOUND" FOR "COULD NOT READ" IS NOT A PARSER.

Measured 2026-08-05: byte 0xef at offset 7339 of a 1,938,549-byte log. Both encodings
raised, values silently vanished, and the run still reported PASS because mean length
is not a gate. That is the dangerous shape - a number that quietly stops existing.

The raw line and the parsed value are kept SEPARATE in every case below, so a wrong
extraction is visible as a difference rather than hidden behind a rewritten expectation.

Usage:  python tools/test_spec_drafting_parser.py
Exit 0 = all cases green.  1 = at least one red.
"""

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module():
    path = os.path.join(HERE, "probe-spec-drafting.py")
    spec = importlib.util.spec_from_file_location("psd", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The real line, verbatim from runs/2026-08-05/e11-drafting/positive.err. Kept as the
# raw bytes the server actually wrote, not as a paraphrase of it.
REAL_LINE = (
    "0.36.952.235 I slot print_timing: id  0 | task 0 | "
    "draft acceptance = 0.85088 (  194 accepted /   228 generated), mean len =  3.55\n"
)
# A no-draft log: the acceptance line is ABSENT entirely, which is what the server does
# when speculation never ran. Not "mean len = 0" - absent.
NO_DRAFT_LINE = (
    "0.29.531.717 I slot print_timing: id  0 | task 0 |        "
    "eval time =   24913.30 ms /   271 tokens (   91.93 ms per token,    10.88 tokens per second)\n"
)


def write(tmp, name, data):
    p = os.path.join(tmp, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def main():
    mod = load_module()
    f = mod.read_mean_lengths
    ok = 0
    bad = []

    def check(label, got, want, raw_note):
        nonlocal ok
        if got == want:
            ok_line = "ok   %-56s -> %s" % (label, got)
            print("  " + ok_line)
            return True
        bad.append("%s: want %r, got %r  [raw: %s]" % (label, want, got, raw_note))
        print("  FAIL %-56s want %r got %r" % (label, want, got))
        return False

    tmp = tempfile.mkdtemp(prefix="specparse-")
    try:
        # 1 POSITIVE: one real line, one value
        p = write(tmp, "one.err", REAL_LINE.encode("utf-8"))
        ok += check("one real acceptance line", f(p), [3.55], "mean len =  3.55")

        # 2 POSITIVE: three identical requests, three values in order
        p = write(tmp, "three.err", (REAL_LINE * 3).encode("utf-8"))
        ok += check("three lines yield three values", f(p), [3.55, 3.55, 3.55], "3x the real line")

        # 3 THE DEFECT: a byte that is not valid utf-8, before the line. Strict decoding
        #   returned [] here and that is the failure this suite exists for.
        broken = NO_DRAFT_LINE.encode("utf-8") + b"\xef\xbf raw token noise \xff\xfe" + REAL_LINE.encode("utf-8")
        p = write(tmp, "broken.err", broken)
        ok += check("invalid byte before the line does not eat it", f(p), [3.55], "0xef injected mid-file")

        # 4 NEGATIVE: no acceptance line at all -> empty, and it must NOT invent 0.0
        p = write(tmp, "nodraft.err", (NO_DRAFT_LINE * 3).encode("utf-8"))
        ok += check("no acceptance line yields [] and never [0.0]", f(p), [], "eval-time lines only")

        # 5 NEGATIVE: empty file
        p = write(tmp, "empty.err", b"")
        ok += check("empty log yields []", f(p), [], "zero bytes")

        # 6 NEGATIVE: path that does not exist
        ok += check("missing path yields []", f(os.path.join(tmp, "nope.err")), [], "no such file")

        # 7 NEGATIVE: empty path argument
        ok += check("empty path argument yields []", f(""), [], "''")

        # 8 SEPARATION: a differently-shaped number must still parse, so the case above
        #   proves the VALUE and not just the presence of the string.
        other = REAL_LINE.replace("mean len =  3.55", "mean len = 12.75")
        p = write(tmp, "other.err", other.encode("utf-8"))
        ok += check("a different value parses as that value", f(p), [12.75], "mean len = 12.75")

        # 9 SELF-TEST OF THE SUITE: a case that must NOT match, so a parser returning a
        #   constant would be caught here.
        p = write(tmp, "decoy.err", b"mean length = 3.55\nmean_len = 3.55\n")
        ok += check("near-miss spellings are not matched", f(p), [], "'mean length' / 'mean_len'")
    finally:
        for n in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, n))
            except OSError:
                pass
        os.rmdir(tmp)

    total = 9
    print("")
    if ok == total and not bad:
        print("RESULT: PASS - %d of %d cases green. The parser distinguishes "
              "'nothing drafted' from 'could not read'." % (ok, total))
        return 0
    print("RESULT: FAIL - %d of %d cases green." % (ok, total))
    for b in bad:
        print("  " + b)
    return 1


if __name__ == "__main__":
    sys.exit(main())
