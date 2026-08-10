"""Extract a GGUF's header and chat template so they outlive the model file.

WHY THIS EXISTS: on 2026-08-10 the 5,754-byte chat template - the one WITHOUT
reasoning_effort - existed on this machine in exactly two places, and both were
model files about to be deleted to make room for the 0731 download. The 0.1 plan
uses that template as the negative control for the hand-written 0731 template.
Deleting a 155 GB file would have taken a 5.7 KB control with it.

The header carries everything a model comparison needs - architecture, layer
count, expert count, compress_ratios, the tokenizer's chat template - and it sits
in the first few MB. Rescuing it costs seconds. Re-downloading the model to read
it back costs the whole file.

DIFFERENCE TO gguf_header.py, which lives next to this file: that one CHECKS a
header against expectations and answers yes/no. This one EXTRACTS it and answers
"here it is". Neither replaces the other, and this one deliberately has no
--expect: a rescue that refuses to run because a value surprised it would lose
exactly the file that surprised it.

WHAT IS WRITTEN, per model:
  header-<stem>.json    every KV pair, plus tensor and KV counts
  chat_template-<stem>.jinja   the raw template bytes, only if the model has one
  INDEX.json            one record per model - written LAST, see below

THE ORDER MATTERS: INDEX.json is the claim "these models were rescued". Written
first, a crash mid-run leaves an index that names files that are not there.
Written last, a crash leaves headers without an index and the next run redoes
them. Same reason preserve-build.ps1 writes its manifest last.

LONG ARRAYS KEEP ONLY THEIR SHAPE. A 129,280-entry token list helps nobody and
costs memory to hold. Short ones keep their values, because a per-layer array
like compress_ratios IS the answer somebody came here for. The cut is at 512,
which clears any real layer count with room to spare.

Usage:  rescue_gguf_meta.py <outdir> <file.gguf> [<file.gguf> ...]
Exit 0 = every file rescued.  1 = at least one failed.  2 = setup error.
"""

import hashlib
import json
import os
import struct
import sys

# GGUF value type ids, from ggml/src/gguf.cpp - the same table gguf_header.py uses.
UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL, STRING, ARRAY, UINT64, INT64, FLOAT64 = range(13)

_FIXED = {
    UINT8: ("<B", 1), INT8: ("<b", 1),
    UINT16: ("<H", 2), INT16: ("<h", 2),
    UINT32: ("<I", 4), INT32: ("<i", 4), FLOAT32: ("<f", 4),
    BOOL: ("<B", 1),
    UINT64: ("<Q", 8), INT64: ("<q", 8), FLOAT64: ("<d", 8),
}

# Arrays at or below this length are written out with their values.
ARRAY_VALUE_LIMIT = 512


class Reader:
    def __init__(self, fh):
        self.fh = fh

    def raw(self, n):
        b = self.fh.read(n)
        if len(b) != n:
            raise EOFError(f"truncated: wanted {n} bytes, got {len(b)}")
        return b

    def num(self, type_id):
        fmt, size = _FIXED[type_id]
        return struct.unpack(fmt, self.raw(size))[0]

    def string(self):
        return self.raw(self.num(UINT64)).decode("utf-8", "replace")

    def value(self, type_id):
        if type_id in _FIXED:
            v = self.num(type_id)
            return bool(v) if type_id == BOOL else v
        if type_id == STRING:
            return self.string()
        if type_id == ARRAY:
            elem = self.num(UINT32)
            count = self.num(UINT64)
            if elem == STRING:
                vals = [self.string() for _ in range(count)]
            else:
                vals = [self.value(elem) for _ in range(count)]
            if count <= ARRAY_VALUE_LIMIT:
                return vals
            # The count travels with the shape. Without it an array that was read
            # as empty is indistinguishable from one that is genuinely short.
            return {"_array_of": elem, "n": count, "head": vals[:8]}
        raise ValueError(f"unknown GGUF value type {type_id}")


def read_header(path):
    with open(path, "rb") as fh:
        r = Reader(fh)
        magic = r.raw(4)
        if magic != b"GGUF":
            raise ValueError(f"not a GGUF file: magic is {magic!r}")
        out = {"gguf_version": r.num(UINT32)}
        out["n_tensors"] = r.num(UINT64)
        n_kv = r.num(UINT64)
        out["n_kv"] = n_kv
        kv = {}
        for _ in range(n_kv):
            key = r.string()
            kv[key] = r.value(r.num(UINT32))
        out["kv"] = kv
        return out


def rescue_one(path, outdir):
    """Return the index record for one model. Raises nothing - failures land in the record."""
    name = os.path.basename(path)
    rec = {"file": os.path.abspath(path), "name": name}
    try:
        rec["size_bytes"] = os.path.getsize(path)
        header = read_header(path)
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec

    kv = header["kv"]
    rec["n_tensors"] = header["n_tensors"]
    rec["n_kv"] = header["n_kv"]
    rec["arch"] = kv.get("general.architecture")
    rec["version"] = kv.get("general.version")

    stem = name[:-5] if name.endswith(".gguf") else name

    template = kv.get("tokenizer.chat_template")
    if isinstance(template, str):
        blob = template.encode("utf-8")
        tpath = os.path.join(outdir, f"chat_template-{stem}.jinja")
        # Binary mode on purpose: text mode on Windows would turn every \n into
        # \r\n and the byte count - which is how the two template variants are
        # told apart - would be wrong by the number of lines.
        with open(tpath, "wb") as fh:
            fh.write(blob)
        rec["chat_template_bytes"] = len(blob)
        rec["chat_template_sha256"] = hashlib.sha256(blob).hexdigest()
        rec["chat_template_file"] = os.path.basename(tpath)
        rec["chat_template_has_reasoning_effort"] = "reasoning_effort" in template
    else:
        # Not an error. Plenty of GGUFs carry no template, and saying so
        # explicitly is what keeps "has none" apart from "was not read".
        rec["chat_template_bytes"] = 0

    hpath = os.path.join(outdir, f"header-{stem}.json")
    with open(hpath, "w", encoding="utf-8") as fh:
        json.dump(header, fh, ensure_ascii=False, indent=1)
    rec["header_file"] = os.path.basename(hpath)
    return rec


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    outdir, paths = argv[1], argv[2:]
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as exc:
        print(f"SETUP ERROR: cannot create {outdir}: {exc}")
        return 2

    index, failed = [], 0
    for path in paths:
        rec = rescue_one(path, outdir)
        index.append(rec)
        if "error" in rec:
            failed += 1
            print(f"  FAILED   {rec['name']}: {rec['error']}")
        else:
            print("  OK       {name}: arch={arch}, {n_kv} kv, template {tb} B".format(
                name=rec["name"], arch=rec["arch"], n_kv=rec["n_kv"],
                tb=rec["chat_template_bytes"]))

    # LAST, and only now: the index is the claim that the rest is on disk.
    with open(os.path.join(outdir, "INDEX.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)

    print()
    # The denominator travels with the count. "0 failed" out of 0 files attempted
    # is a run that did nothing and reads exactly like a clean one.
    print(f"RESULT: {len(index) - failed} of {len(index)} rescued -> {outdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        # DeepSeek's chat template carries U+FF5C, which cp1252 cannot encode. Without
        # this the script dies while PRINTING a header it read correctly - which looks
        # like a corrupt model and is not one. Same guard as gguf_header.py.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
