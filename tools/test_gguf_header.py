"""Negative control for gguf_header.py.

A checker that only ever sees good input cannot tell "the file is fine" from "the
check does nothing". This suite feeds it three kinds of broken input and requires
it to reject each one, then feeds it good input and requires it to pass.

The broken fixtures are derived from a real GGUF so the failures are the ones that
would actually happen to a download, not synthetic ones. The array fixture is the
one exception and says why below.

Usage:  test_gguf_header.py <a-real-file.gguf>
Exit 0 = all five cases behaved as required.
"""

import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "gguf_header.py")

# Enough to hold the GGUF magic, version and both counts, and far too little to
# hold 52 KV pairs including the tokenizer arrays.
FIXTURE_BYTES = 64 * 1024


def run(args):
    p = subprocess.run([sys.executable, CHECKER] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def gguf_with_arrays():
    """A minimal GGUF carrying one short numeric array and one long one.

    Built from the format instead of cut from a real file, because this case turns
    on a KNOWN element count and KNOWN values. Against a real file "shows the
    values" cannot be told apart from "shows something"; here three zeros must
    appear as three zeros, and a count of three must appear as three. The short
    array has the shape of dflash.attention.compress_ratios, which is the loader
    condition this output exists to answer.
    """
    ARRAY, FLOAT32, INT32, STRING = 9, 6, 5, 8

    def s(text):
        b = text.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    out = b"GGUF" + struct.pack("<IQQ", 3, 0, 4)  # version, 0 tensors, 4 kv pairs
    out += s("test.short") + struct.pack("<I", ARRAY) + struct.pack("<IQ", FLOAT32, 3)
    out += struct.pack("<fff", 0.0, 0.0, 0.0)
    # The zero array above cannot catch a reader that prints zeros no matter what
    # it read, and "every ratio is 0" is exactly the answer such a reader would
    # fake. This one has to come back as 1, 0, 7 in that order.
    out += s("test.nonzero") + struct.pack("<I", ARRAY) + struct.pack("<IQ", INT32, 3)
    out += struct.pack("<3i", 1, 0, 7)
    out += s("test.long") + struct.pack("<I", ARRAY) + struct.pack("<IQ", INT32, 100)
    out += struct.pack("<100i", *range(100))
    out += s("general.architecture") + struct.pack("<I", STRING) + s("test")
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    real = argv[1]
    if not os.path.exists(real):
        print(f"SETUP ERROR: no such file: {real}")
        return 2
    if not os.path.exists(CHECKER):
        print(f"SETUP ERROR: checker not found next to this suite: {CHECKER}")
        return 2

    with open(real, "rb") as fh:
        head = fh.read(FIXTURE_BYTES)

    fail = 0
    with tempfile.TemporaryDirectory() as tmp:
        truncated = os.path.join(tmp, "truncated.gguf")
        with open(truncated, "wb") as fh:
            fh.write(head)

        bad_magic = os.path.join(tmp, "bad-magic.gguf")
        with open(bad_magic, "wb") as fh:
            fh.write(b"XGUF" + head[4:])

        arrays = os.path.join(tmp, "arrays.gguf")
        with open(arrays, "wb") as fh:
            fh.write(gguf_with_arrays())

        cases = [
            # label,                    args,                                    want_exit, want_text
            ("positive control",        [real, "general.architecture=deepseek4"], 0, "PASS"),
            ("truncated file",          [truncated],                              2, "truncated"),
            ("wrong magic",             [bad_magic],                              2, "not a GGUF"),
            ("expectation not met",     [real, "deepseek4.block_count=99"],       1, "MISMATCH"),
            # Both halves in one case: short arrays give up their values, long ones
            # must NOT — a checker that prints 130k token ids has stopped being one.
            # The count travels with the values so an empty array cannot read as
            # "all zero", which is the cheapest wrong answer this output can give.
            ("short arrays show values", [arrays], 0,
             ["[0.0, 0.0, 0.0] (3 values, type 6)",
              "[1, 0, 7] (3 values, type 5)",
              "<array of 100 type 5>"]),
        ]

        for label, args, want_exit, want_text in cases:
            code, out = run(args)
            wants = [want_text] if isinstance(want_text, str) else want_text
            ok_code = code == want_exit
            missing = [w for w in wants if w.lower() not in out.lower()]
            if ok_code and not missing:
                print(f"  OK       {label}: exit {code}, output contains "
                      + ", ".join(repr(w) for w in wants))
            else:
                fail = 1
                print(f"  FAILED   {label}: exit {code} (wanted {want_exit}), "
                      + (f"missing {', '.join(repr(w) for w in missing)}" if missing
                         else "all texts found"))
                print("           " + out.strip().replace("\n", "\n           ")[:600])

    print()
    print("RESULT: PASS - the checker rejects broken input and accepts good input"
          if not fail else
          "RESULT: FAIL - the checker cannot be relied on; see FAILED lines above")
    return fail


if __name__ == "__main__":
    sys.exit(main(sys.argv))
