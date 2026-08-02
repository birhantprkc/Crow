"""Negative control for gguf_header.py.

A checker that only ever sees good input cannot tell "the file is fine" from "the
check does nothing". This suite feeds it three kinds of broken input and requires
it to reject each one, then feeds it good input and requires it to pass.

The broken fixtures are derived from a real GGUF so the failures are the ones that
would actually happen to a download, not synthetic ones.

Usage:  test_gguf_header.py <a-real-file.gguf>
Exit 0 = all four cases behaved as required.
"""

import os
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

        cases = [
            # label,                    args,                                    want_exit, want_text
            ("positive control",        [real, "general.architecture=deepseek4"], 0, "PASS"),
            ("truncated file",          [truncated],                              2, "truncated"),
            ("wrong magic",             [bad_magic],                              2, "not a GGUF"),
            ("expectation not met",     [real, "deepseek4.block_count=99"],       1, "MISMATCH"),
        ]

        for label, args, want_exit, want_text in cases:
            code, out = run(args)
            ok_code = code == want_exit
            ok_text = want_text.lower() in out.lower()
            if ok_code and ok_text:
                print(f"  OK       {label}: exit {code}, output contains {want_text!r}")
            else:
                fail = 1
                print(f"  FAILED   {label}: exit {code} (wanted {want_exit}), "
                      f"{want_text!r} {'found' if ok_text else 'NOT found'} in output")
                print("           " + out.strip().replace("\n", "\n           ")[:600])

    print()
    print("RESULT: PASS - the checker rejects broken input and accepts good input"
          if not fail else
          "RESULT: FAIL - the checker cannot be relied on; see FAILED lines above")
    return fail


if __name__ == "__main__":
    sys.exit(main(sys.argv))
