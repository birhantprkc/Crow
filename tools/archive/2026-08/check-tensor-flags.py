"""Guard: are the llama_model_loader tensor flag bits what this base expects?

WHY THIS EXISTS, and it is a measured failure and not a precaution. Our
TENSOR_STREAMED was written against b10223, where bit 4 was free. Upstream then took
bit 4 for TENSOR_ALLOW_RESHAPE in #26577 - the very commit b10269 is tagged on, the
one we jump for. Both changes were correct on their own base, and nothing noticed
until the patch met the newer tree, as a merge conflict in three files. A bit is not
a local text constant, it is a shared resource, and a shared resource needs an
invariant that a machine checks.

WHAT IT DOES NOT DO: pick the next free bit by itself. An automatic "find a free
slot" would make the conflict disappear and could silently change a serialisation
format, ABI behaviour, or any other base-dependent meaning. On an unexpected layout
this aborts and says what it found.

BASE-SPECIFIC ON PURPOSE. The expectation is declared per base below, because
b10223 and b10269 genuinely have different inventories. The patches are therefore
base-specific too, and there is no claim that one unchanged patch file serves both.

Usage:
  check-tensor-flags.py --ref <git-ref> --state pristine   [--src <llama.cpp tree>]
  check-tensor-flags.py --file <header> --state patched --base b10269
  check-tensor-flags.py --file <header> --state pristine --base b10269

  --ref resolves the ref in --src, looks the COMMIT up in the table below, and reads
  the header from git. An unknown commit is an abort, not a warning: an unrecognised
  base means nobody has stated what the flags there should be.

Exit 0 = the inventory matches the declared expectation.  1 = it does not.
2 = setup error (unknown base, unreadable header, bad arguments).
"""

import argparse
import os
import re
import subprocess
import sys

HEADER = "src/llama-model-loader.h"

# Declared expectation per base. `upstream` is what the base brings by itself;
# `streamed_bit` is the bit OUR TENSOR_STREAMED is allowed to occupy there.
BASES = {
    "11924d4c17abc27383376a1ac6a24fa3e36c1c0c": {
        "name": "b10223",
        "upstream": {
            "TENSOR_NOT_REQUIRED": 0,
            "TENSOR_DUPLICATED": 1,
            "TENSOR_SKIP": 2,
            "TENSOR_SKIP_IF_VIRTUAL": 3,
        },
        "streamed_bit": 4,
    },
    "1c3c9674de4d455f1e571bed808252af54932767": {
        "name": "b10269",
        "upstream": {
            "TENSOR_NOT_REQUIRED": 0,
            "TENSOR_DUPLICATED": 1,
            "TENSOR_SKIP": 2,
            "TENSOR_SKIP_IF_VIRTUAL": 3,
            "TENSOR_ALLOW_RESHAPE": 4,
        },
        "streamed_bit": 5,
    },
}
BY_NAME = {v["name"]: k for k, v in BASES.items()}

OURS = "TENSOR_STREAMED"
MAX_BIT = 30  # the flags are `int`; anything at or above the sign bit is an error

# Deliberately tolerant about the expression, so that a decimal value, a shifted
# multiple like `3 << 4`, or a hex literal is CAPTURED and then rejected, instead of
# slipping past a pattern that only understands `1 << n`.
DECL = re.compile(r"^\s*static const int\s+(TENSOR_[A-Z_0-9]+)\s*=\s*([^;]+);")


def read_header(args):
    if args.file:
        if not os.path.exists(args.file):
            sys.exit(f"SETUP ERROR: no such file: {args.file}")
        with open(args.file, encoding="utf-8", errors="replace") as fh:
            return fh.read(), args.file
    out = subprocess.run(["git", "-C", args.src, "show", f"{args.ref}:{HEADER}"],
                         capture_output=True)
    if out.returncode != 0:
        sys.exit(f"SETUP ERROR: cannot read {args.ref}:{HEADER} from {args.src}")
    return out.stdout.decode("utf-8", "replace"), f"{args.ref}:{HEADER}"


def parse_value(expr):
    """-> (bit, note). bit is None when the expression is not a single-bit shift."""
    expr = expr.strip()
    m = re.fullmatch(r"1\s*<<\s*(\d+)", expr)
    if m:
        return int(m.group(1)), None
    try:
        val = int(expr, 0)
    except ValueError:
        return None, f"not a constant this guard understands: {expr!r}"
    if val > 0 and (val & (val - 1)) == 0:
        return val.bit_length() - 1, f"written as {expr!r} rather than a 1<<n shift"
    return None, f"value {expr!r} does not have exactly one bit set"


def inventory(text):
    """-> (list of (name, bit, raw, note), list of problems)"""
    flags, problems = [], []
    for line in text.splitlines():
        m = DECL.match(line)
        if not m:
            continue
        name, expr = m.group(1), m.group(2)
        bit, note = parse_value(expr)
        if bit is None:
            problems.append(f"{name} = {expr.strip()}: {note}")
            continue
        if bit > MAX_BIT:
            problems.append(f"{name} = 1 << {bit}: beyond bit {MAX_BIT} for an int flag")
            continue
        flags.append((name, bit, expr.strip(), note))
    return flags, problems


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--ref")
    ap.add_argument("--file")
    ap.add_argument("--base", choices=sorted(BY_NAME))
    ap.add_argument("--state", required=True, choices=("pristine", "patched"))
    ap.add_argument("--src", default=r"C:\Users\robin\dev\crow-lab\src")
    args = ap.parse_args()
    if bool(args.ref) == bool(args.file):
        sys.exit("SETUP ERROR: give exactly one of --ref or --file")

    # Which base are we talking about? With --ref the COMMIT decides, so a tag that
    # has moved cannot quietly select the wrong expectation.
    if args.ref:
        sha = subprocess.run(["git", "-C", args.src, "rev-parse", f"{args.ref}^{{commit}}"],
                             capture_output=True, text=True)
        if sha.returncode != 0:
            sys.exit(f"SETUP ERROR: cannot resolve {args.ref} in {args.src}")
        sha = sha.stdout.strip()
        if sha not in BASES:
            sys.exit(f"ABORT: commit {sha} is not a known base. Add its flag expectation "
                     f"before transferring a patch onto it - an unknown base means "
                     f"nobody has stated what its flags should be.")
        base_sha = sha
    else:
        if not args.base:
            sys.exit("SETUP ERROR: --file needs --base, the commit cannot be derived")
        base_sha = BY_NAME[args.base]

    spec = BASES[base_sha]
    text, origin = read_header(args)
    flags, problems = inventory(text)
    got = {n: b for n, b, _, _ in flags}

    print(f"base   : {spec['name']} ({base_sha[:12]})")
    print(f"state  : {args.state}")
    print(f"header : {origin}")
    print("inventory:")
    for name, bit, raw, note in flags:
        print(f"  {name:<26} 1 << {bit:<2}  0x{1 << bit:02x}" + (f"   NOTE: {note}" if note else ""))

    rc = 0
    for p in problems:
        print(f"  MALFORMED {p}")
        rc = 1

    # 1. every name once, every value once
    for name in sorted(got):
        n = sum(1 for f in flags if f[0] == name)
        if n != 1:
            print(f"  FAIL {name} declared {n} times, want 1")
            rc = 1
    seen = {}
    for name, bit, _, _ in flags:
        if bit in seen:
            print(f"  FAIL bit {bit} claimed by both {seen[bit]} and {name} "
                  f"-- two owners, one slot; this is the failure #26577 caused")
            rc = 1
        seen[bit] = name

    # 2. the base's own flags, unchanged
    for name, bit in sorted(spec["upstream"].items(), key=lambda kv: kv[1]):
        if name not in got:
            print(f"  FAIL {name} missing; {spec['name']} is expected to declare it")
            rc = 1
        elif got[name] != bit:
            print(f"  FAIL {name} = 1 << {got[name]}, expected 1 << {bit}")
            rc = 1

    # 3. our flag, according to state
    want_bit = spec["streamed_bit"]
    if args.state == "pristine":
        if OURS in got:
            print(f"  FAIL {OURS} present on a pristine base")
            rc = 1
        owner = seen.get(want_bit)
        if owner:
            print(f"  ABORT: intended {OURS} bit {want_bit} is occupied by {owner}. "
                  f"Not choosing another bit automatically - a different bit can change "
                  f"base-dependent meaning. Decide explicitly and update BASES.")
            rc = 1
        else:
            print(f"  ok   bit {want_bit} is free for {OURS}")
    else:
        if OURS not in got:
            print(f"  FAIL {OURS} missing after the patch")
            rc = 1
        elif got[OURS] != want_bit:
            print(f"  FAIL {OURS} = 1 << {got[OURS]}, expected 1 << {want_bit} on {spec['name']}")
            rc = 1
        else:
            print(f"  ok   {OURS} = 1 << {want_bit}")

    # 4. nothing unexpected crept in
    known = set(spec["upstream"]) | {OURS}
    for name in sorted(set(got) - known):
        print(f"  FAIL unexpected flag {name} = 1 << {got[name]}; the inventory for "
              f"{spec['name']} grew without this guard being told")
        rc = 1

    expect_n = len(spec["upstream"]) + (1 if args.state == "patched" else 0)
    if len(flags) != expect_n:
        print(f"  FAIL {len(flags)} flags declared, expected {expect_n}")
        rc = 1

    print("RESULT: green" if rc == 0 else "RESULT: red - see the lines above")
    return rc


if __name__ == "__main__":
    sys.exit(main())
