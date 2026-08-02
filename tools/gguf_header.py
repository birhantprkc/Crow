"""Read a GGUF header and check it against expected values.

Integrity check without hashing 155 GB: the header carries tensor count, KV count
and the architecture hyperparameters. A truncated or scrambled download shows up
here immediately, and reading it costs a few MB instead of the whole file.

Usage:  gguf_header.py <file.gguf> [--expect key=value ...]
Exit 0 = every expectation met. Exit 1 = at least one mismatch. Exit 2 = unreadable.
"""

import struct
import sys

# Tokenizer keys carry characters cp1252 cannot represent (DeepSeek uses U+FF5C in
# its chat template). Without this the script dies while printing a header it has
# already read correctly, which looks like a corrupt file and is not one.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# GGUF value type ids, from ggml/src/gguf.cpp
UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL, STRING, ARRAY, UINT64, INT64, FLOAT64 = range(13)

_FIXED = {
    UINT8: ("<B", 1), INT8: ("<b", 1),
    UINT16: ("<H", 2), INT16: ("<h", 2),
    UINT32: ("<I", 4), INT32: ("<i", 4), FLOAT32: ("<f", 4),
    BOOL: ("<B", 1),
    UINT64: ("<Q", 8), INT64: ("<q", 8), FLOAT64: ("<d", 8),
}


class Reader:
    def __init__(self, fh):
        self.fh = fh

    def raw(self, n):
        b = self.fh.read(n)
        if len(b) != n:
            raise EOFError(f"wanted {n} bytes, got {len(b)} — file is truncated")
        return b

    def fixed(self, t):
        fmt, size = _FIXED[t]
        return struct.unpack(fmt, self.raw(size))[0]

    def string(self):
        n = self.fixed(UINT64)
        return self.raw(n).decode("utf-8", errors="replace")

    def value(self, t):
        if t == STRING:
            return self.string()
        if t == ARRAY:
            elem_t = self.fixed(UINT32)
            n = self.fixed(UINT64)
            # Arrays here are token lists and the like; keep the shape, not the payload.
            if elem_t == STRING:
                for _ in range(n):
                    self.string()
            elif elem_t == ARRAY:
                raise ValueError("nested arrays are not supported")
            else:
                _, size = _FIXED[elem_t]
                self.raw(size * n)
            return f"<array of {n} type {elem_t}>"
        if t == BOOL:
            return bool(self.fixed(BOOL))
        return self.fixed(t)


def read_header(path):
    with open(path, "rb") as fh:
        r = Reader(fh)
        magic = r.raw(4)
        if magic != b"GGUF":
            raise ValueError(f"not a GGUF file: magic is {magic!r}, expected b'GGUF'")
        version = r.fixed(UINT32)
        n_tensors = r.fixed(UINT64)
        n_kv = r.fixed(UINT64)

        kv = {}
        for _ in range(n_kv):
            key = r.string()
            kv[key] = r.value(r.fixed(UINT32))
        return version, n_tensors, n_kv, kv


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    expect = {}
    for a in argv[2:]:
        if a == "--expect":
            continue
        k, _, v = a.partition("=")
        expect[k] = v

    try:
        version, n_tensors, n_kv, kv = read_header(path)
    except (EOFError, ValueError, OSError) as e:
        print(f"UNREADABLE: {e}")
        return 2

    print(f"file        {path}")
    print(f"gguf        version {version}")
    print(f"tensors     {n_tensors}")
    print(f"kv pairs    {n_kv}")
    print(f"arch        {kv.get('general.architecture', '<missing>')}")
    print()
    for k in sorted(kv):
        print(f"  {k} = {kv[k]}")

    if not expect:
        return 0

    print()
    print("=== expectations ===")
    fail = 0
    actual = dict(kv)
    actual["tensor_count"] = n_tensors
    actual["kv_count"] = n_kv
    for k, want in expect.items():
        if k not in actual:
            print(f"  MISSING  {k} — expected {want}")
            fail = 1
            continue
        got = actual[k]
        ok = str(got) == want
        print(f"  {'OK      ' if ok else 'MISMATCH'} {k} = {got}" + ("" if ok else f"  (expected {want})"))
        if not ok:
            fail = 1
    print()
    print("RESULT: PASS — every expectation met" if not fail else "RESULT: FAIL — see MISMATCH/MISSING above")
    return fail


if __name__ == "__main__":
    sys.exit(main(sys.argv))
