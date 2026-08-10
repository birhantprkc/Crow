"""Negative control for rescue_gguf_meta.py.

An extractor that only ever sees good input cannot tell "the template was rescued"
from "some bytes were written". The whole point of the rescue is that the SIZE of
the output is the evidence - plan stage E10 tells the two DeepSeek template
variants apart by 5,754 against 13,098 bytes - so this suite checks the bytes, not
that a file appeared.

Six cases, and the ones that matter are 4 to 6:

  1 positive control on a real GGUF
  2 wrong magic       -> reported as failed, exit 1, and NAMED in the index
  3 truncated file    -> same
  4 no chat template  -> zero bytes recorded and NO .jinja written, so "has none"
                         stays apart from "was not read"
  5 template comes back byte-for-byte, including a lone \\n that must NOT become
    \\r\\n - the failure this file was written after, measured 2026-08-10: a
    checkout turned 5,754 bytes into 5,874, one CR per line
  6 long arrays keep their shape and their count; short ones keep their values

Usage:  test_rescue_gguf_meta.py <a-real-file.gguf>
Exit 0 = all six cases behaved as required.
"""

import json
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "rescue_gguf_meta.py")

# Enough for magic, version and both counts; nowhere near enough for the KV block.
FIXTURE_BYTES = 64 * 1024

# A template with the two properties that broke things in practice: LF line
# endings, and U+FF5C, which cp1252 cannot encode. 12 lines, so a CRLF rewrite
# would add exactly 12 bytes and the count below would no longer match.
FIXTURE_TEMPLATE = (
    "{%- if messages -%}\n"
    "<｜begin▁of▁sentence｜>\n"
    "{%- for m in messages -%}\n"
    "{%- if m.role == 'user' -%}\n"
    "<｜User｜>{{ m.content }}\n"
    "{%- else -%}\n"
    "<｜Assistant｜>{{ m.content }}\n"
    "{%- endif -%}\n"
    "{%- endfor -%}\n"
    "{%- endif -%}\n"
    "{#- reasoning_effort is what tells the two real variants apart -#}\n"
    "{{- reasoning_effort | default('low') -}}\n"
)

ARRAY, FLOAT32, INT32, STRING, UINT32 = 9, 6, 5, 8, 4


def _s(text):
    b = text.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _kv(key, type_id, payload):
    return _s(key) + struct.pack("<I", type_id) + payload


def gguf_fixture(with_template):
    """A minimal but real GGUF. Built from the format, not cut from a file, because
    these cases turn on KNOWN values: a known template, a known array of three, and
    a long array whose count must survive."""
    parts = [
        _kv("general.architecture", STRING, _s("testarch")),
        _kv("test.short", ARRAY, struct.pack("<IQ", INT32, 3) + struct.pack("<3i", 1, 0, 7)),
        _kv("test.long", ARRAY, struct.pack("<IQ", INT32, 600) + struct.pack("<600i", *range(600))),
    ]
    if with_template:
        parts.append(_kv("tokenizer.chat_template", STRING, _s(FIXTURE_TEMPLATE)))
    return b"GGUF" + struct.pack("<IQQ", 3, 0, len(parts)) + b"".join(parts)


def run(args):
    p = subprocess.run([sys.executable, TOOL] + args, capture_output=True, text=True,
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
    if not os.path.exists(TOOL):
        print(f"SETUP ERROR: tool not found next to this suite: {TOOL}")
        return 2

    want_template = FIXTURE_TEMPLATE.encode("utf-8")
    # Anchored on purpose. Deriving the expectation only from the fixture would
    # check the suite against itself; a silently edited fixture must show up here.
    # 330 is measured, not predicted - the first guess said 434 and this line is
    # what caught it. 12 lines, so a CRLF rewrite would report 342.
    if len(want_template) != 330:
        print(f"SETUP ERROR: fixture template is {len(want_template)} B, suite expects 330")
        return 2

    fail = 0

    def check(label, cond, detail=""):
        nonlocal fail
        if cond:
            print(f"  OK       {label}")
        else:
            fail = 1
            print(f"  FAILED   {label}{': ' + detail if detail else ''}")

    with tempfile.TemporaryDirectory() as tmp:
        with open(real, "rb") as fh:
            head = fh.read(FIXTURE_BYTES)

        good = os.path.join(tmp, "good.gguf")
        with open(good, "wb") as fh:
            fh.write(gguf_fixture(True))
        bare = os.path.join(tmp, "no-template.gguf")
        with open(bare, "wb") as fh:
            fh.write(gguf_fixture(False))
        bad_magic = os.path.join(tmp, "bad-magic.gguf")
        with open(bad_magic, "wb") as fh:
            fh.write(b"XGUF" + head[4:])
        truncated = os.path.join(tmp, "truncated.gguf")
        with open(truncated, "wb") as fh:
            fh.write(head)

        # --- 1: positive control against a real model -------------------------
        out1 = os.path.join(tmp, "out1")
        code, text = run([out1, real])
        idx1 = json.load(open(os.path.join(out1, "INDEX.json"), encoding="utf-8"))
        check("1 positive control: real GGUF rescued, exit 0",
              code == 0 and len(idx1) == 1 and "error" not in idx1[0],
              f"exit {code}, index {idx1[0].get('error', 'no error')}")

        # --- 2 and 3: broken input is reported, not swallowed -----------------
        out2 = os.path.join(tmp, "out2")
        code, text = run([out2, bad_magic, truncated])
        idx2 = json.load(open(os.path.join(out2, "INDEX.json"), encoding="utf-8"))
        errs = [r for r in idx2 if "error" in r]
        check("2 wrong magic and 3 truncated: exit 1, both named in the index",
              code == 1 and len(idx2) == 2 and len(errs) == 2
              and any("not a GGUF" in r["error"] for r in errs)
              and any("truncated" in r["error"] for r in errs),
              f"exit {code}, {len(errs)} of {len(idx2)} carry an error")

        # --- 4: no template is a fact, not a silence --------------------------
        out4 = os.path.join(tmp, "out4")
        code, text = run([out4, bare])
        idx4 = json.load(open(os.path.join(out4, "INDEX.json"), encoding="utf-8"))
        jinjas = [f for f in os.listdir(out4) if f.endswith(".jinja")]
        check("4 model without template: 0 bytes recorded and no .jinja written",
              code == 0 and idx4[0]["chat_template_bytes"] == 0 and not jinjas,
              f"exit {code}, bytes {idx4[0].get('chat_template_bytes')}, files {jinjas}")

        # --- 5: THE case. Bytes in, same bytes out. ---------------------------
        # Against the real model this cannot be checked: its template is whatever
        # it is. Only a fixture whose bytes we chose can answer "unchanged".
        out5 = os.path.join(tmp, "out5")
        code, text = run([out5, good])
        idx5 = json.load(open(os.path.join(out5, "INDEX.json"), encoding="utf-8"))[0]
        tpath = os.path.join(out5, idx5.get("chat_template_file", "MISSING"))
        got = open(tpath, "rb").read() if os.path.exists(tpath) else b""
        # Computed outside the f-string on purpose: written inline as b'\\r' it
        # searches for a backslash followed by r, always says False, and the one
        # line meant to explain a CRLF failure becomes the thing that hides it.
        n_cr = got.count(b"\r")
        check("5 template survives byte-for-byte, LF stays LF",
              got == want_template
              and idx5["chat_template_bytes"] == len(want_template)
              and n_cr == 0
              and idx5["chat_template_has_reasoning_effort"] is True,
              f"wrote {len(got)} B, wanted {len(want_template)}; {n_cr} CR bytes")

        # --- 6: arrays -------------------------------------------------------
        hdr = json.load(open(os.path.join(out5, idx5["header_file"]), encoding="utf-8"))
        short, long_ = hdr["kv"]["test.short"], hdr["kv"]["test.long"]
        check("6 short array keeps values, long array keeps shape and count",
              short == [1, 0, 7]
              and isinstance(long_, dict) and long_["n"] == 600 and len(long_["head"]) == 8,
              f"short={short!r}, long={long_ if isinstance(long_, dict) else type(long_).__name__}")

    print()
    print("RESULT: PASS - the rescue reproduces its input and reports what it could not read"
          if not fail else
          "RESULT: FAIL - the rescue cannot be relied on; see FAILED lines above")
    return fail


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
