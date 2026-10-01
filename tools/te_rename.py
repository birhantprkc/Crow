"""Derive Qwen-Image 2.1's `text_encoder_sdcli/` from upstream `text_encoder/`.

WHY THIS EXISTS. sd-server loads the Qwen3-VL text encoder under the names
stable-diffusion.cpp's conditioner expects (`text_encoders.llm.*`). Upstream
`Qwen/Qwen-Image-2.1` ships the transformers names (`model.language_model.*`,
`model.visual.*`, `lm_head.*`). The un-renamed encoder parses but fails the
conditioner's validation tensor by tensor (vault ISSUE-206/ISSUE-300). The fix
is a HEADER-ONLY rename; every tensor byte stays as downloaded.

THE RULE, and where each line comes from:

    model.language_model.  ->  text_encoders.llm.model.         (ISSUE-300)
    model.visual.          ->  text_encoders.llm.model.visual.  (ISSUE-300)
    lm_head.               ->  text_encoders.llm.lm_head.       (the owner's derived
                                copy, shard 4: `text_encoders.llm.lm_head.weight`)

A name with any other prefix is REFUSED by name. A silent pass-through would
hand sd-server a tensor under a name nothing asks for, and the failure would
surface as a conditioner error one process and 17.5 GB later.

`__metadata__` is DROPPED and the header is written compact, unpadded - the
shape of the derived copy sd-server is measured with (header bytes identical to
it, checked 2026-10-01 against the four upstream headers read by HTTP Range).

THE FORMAT (https://github.com/huggingface/safetensors#format): 8 bytes N, an
unsigned little-endian u64; N bytes of JSON header that MAY be trailing-padded
with spaces; then the payload. `data_offsets` are relative to the payload, so
they do not move when the header grows. The names DO grow (21 -> 24 chars, and
+18 for `model.visual.` and `lm_head.`), so the file cannot be patched in place:
each shard is written new - header, then the payload streamed across.

SAFE TO KILL. Each shard goes to `<name>.part` and is renamed only after it
verifies; a rerun skips every shard whose output exists and validates (header
parses, every tensor present with its dtype/shape/offsets, payload length
equal) and redoes the rest. The index is written LAST, so a present
`model.safetensors.index.json` in the output means every shard before it landed.

VERIFY after each write: the `.part` header parses, holds the same number of
tensors as the input with the same dtype/shape/offsets under the mapped name,
and its payload is byte-for-byte the input's payload (read back from disk).

Usage:  te_rename.py <src_dir> <dst_dir>
Exit 0 = every shard and the index are in place and verified.
     1 = the conversion or a verification failed (named).  2 = usage error.
"""

import json
import os
import struct
import sys

RULES = (
    ("model.language_model.", "text_encoders.llm.model."),
    ("model.visual.", "text_encoders.llm.model.visual."),
    ("lm_head.", "text_encoders.llm.lm_head."),
)
INDEX = "model.safetensors.index.json"
META = "__metadata__"
CHUNK = 16 << 20
# A safetensors header past this is not a header: refuse before allocating it.
MAX_HEADER = 100 << 20


class RenameError(Exception):
    """A named refusal: the message says which file and which tensor."""


def rename_key(name: str) -> str:
    for old, new in RULES:
        if name.startswith(old):
            return new + name[len(old):]
    raise RenameError("unknown tensor name prefix: %r (known: %s)"
                      % (name, ", ".join(old for old, _ in RULES)))


def read_header(path: str) -> "tuple[int, dict]":
    """(N, header) of one safetensors file; the payload starts at 8 + N."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) != 8:
            raise RenameError("%s: shorter than the 8-byte header length" % path)
        n = struct.unpack("<Q", head)[0]
        if n > MAX_HEADER or 8 + n > size:
            raise RenameError("%s: header length %d does not fit a %d-byte file"
                              % (path, n, size))
        raw = f.read(n)
    if not raw.startswith(b"{"):
        raise RenameError("%s: header does not start with '{'" % path)
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RenameError("%s: header is not JSON (%s)" % (path, e))
    if not isinstance(header, dict):
        raise RenameError("%s: header is not a JSON object" % path)
    payload = size - 8 - n
    end = 0
    for name, entry in header.items():
        if name == META:
            continue
        try:
            a, b = entry["data_offsets"]
            entry["dtype"], entry["shape"]
        except (KeyError, TypeError, ValueError):
            raise RenameError("%s: tensor %r lacks dtype/shape/data_offsets" % (path, name))
        if not 0 <= a <= b <= payload:
            raise RenameError("%s: tensor %r offsets [%s, %s] outside a %d-byte payload"
                              % (path, name, a, b, payload))
        end = max(end, b)
    if end != payload:
        raise RenameError("%s: tensors end at %d, the payload holds %d bytes"
                          % (path, end, payload))
    return n, header


def tensors(header: dict) -> dict:
    return {k: v for k, v in header.items() if k != META}


def rename_header(header: dict) -> dict:
    """The output header: every tensor under its mapped name, in input order."""
    out = {}
    for name, entry in tensors(header).items():
        new = rename_key(name)
        if new in out:
            raise RenameError("two tensors map to %r" % new)
        out[new] = {"dtype": entry["dtype"], "shape": entry["shape"],
                    "data_offsets": entry["data_offsets"]}
    return out


def encode_header(header: dict) -> bytes:
    return json.dumps(header, separators=(",", ":")).encode("utf-8")


def check_output(src: str, dst: str) -> None:
    """Structural check of an output shard against its input; raises when it fails."""
    sn, sh = read_header(src)
    dn, dh = read_header(dst)
    want = rename_header(sh)
    got = tensors(dh)
    if len(got) != len(want):
        raise RenameError("%s: %d tensors, the input %s has %d"
                          % (dst, len(got), src, len(want)))
    for name, entry in want.items():
        g = got.get(name)
        if g is None:
            raise RenameError("%s: tensor %r missing" % (dst, name))
        for field in ("dtype", "shape", "data_offsets"):
            if g[field] != entry[field]:
                raise RenameError("%s: tensor %r %s %r, expected %r"
                                  % (dst, name, field, g[field], entry[field]))
    if os.path.getsize(dst) - 8 - dn != os.path.getsize(src) - 8 - sn:
        raise RenameError("%s: payload length differs from %s" % (dst, src))


def compare_payload(src: str, src_n: int, dst: str, dst_n: int) -> None:
    """Byte-for-byte payload equality, read back from disk."""
    with open(src, "rb") as a, open(dst, "rb") as b:
        a.seek(8 + src_n)
        b.seek(8 + dst_n)
        pos = 0
        while True:
            x = a.read(CHUNK)
            y = b.read(CHUNK)
            if x != y:
                raise RenameError("%s: payload differs from %s near payload byte %d"
                                  % (dst, src, pos))
            if not x:
                return
            pos += len(x)


def convert_shard(src: str, dst: str) -> str:
    """Write one renamed shard; returns 'skipped' or 'written'."""
    if os.path.exists(dst):
        try:
            check_output(src, dst)
            return "skipped"
        except RenameError:
            pass  # a stale or damaged output is redone below
    n, header = read_header(src)
    new = encode_header(rename_header(header))  # refuses before anything is written
    part = dst + ".part"
    with open(src, "rb") as fin, open(part, "wb") as fout:
        fout.write(struct.pack("<Q", len(new)))
        fout.write(new)
        fin.seek(8 + n)
        while True:
            buf = fin.read(CHUNK)
            if not buf:
                break
            fout.write(buf)
        fout.flush()
        os.fsync(fout.fileno())
    try:
        check_output(src, part)
        compare_payload(src, n, part, len(new))
    except RenameError:
        os.remove(part)
        raise
    os.replace(part, dst)
    return "written"


def rename_index(index: dict) -> dict:
    out = dict(index)
    out["weight_map"] = {rename_key(k): v for k, v in index["weight_map"].items()}
    return out


def load_index(src_dir: str) -> dict:
    path = os.path.join(src_dir, INDEX)
    if not os.path.isfile(path):
        raise RenameError("%s: no %s" % (src_dir, INDEX))
    with open(path, "rb") as f:
        index = json.loads(f.read().decode("utf-8"))
    if not isinstance(index.get("weight_map"), dict) or not index["weight_map"]:
        raise RenameError("%s: no weight_map" % path)
    return index


def convert_dir(src_dir: str, dst_dir: str, log=print) -> "list[tuple[str, str]]":
    """Convert every shard the index names, then the index. Returns (file, action)."""
    index = load_index(src_dir)
    shards = sorted(set(index["weight_map"].values()))
    # the index and the shard headers must name the same tensors, or sd-server
    # resolves a name to a shard that does not hold it
    by_shard = {}
    for shard in shards:
        path = os.path.join(src_dir, shard)
        if not os.path.isfile(path):
            raise RenameError("%s names %s, which is not in %s" % (INDEX, shard, src_dir))
        by_shard[shard] = set(tensors(read_header(path)[1]))
    for name, shard in index["weight_map"].items():
        if name not in by_shard[shard]:
            raise RenameError("%s maps %r to %s, whose header does not hold it"
                              % (INDEX, name, shard))
    if sum(len(s) for s in by_shard.values()) != len(index["weight_map"]):
        raise RenameError("the shard headers hold %d tensors, %s maps %d"
                          % (sum(len(s) for s in by_shard.values()), INDEX,
                             len(index["weight_map"])))
    new_index = encode_index(rename_index(index))
    os.makedirs(dst_dir, exist_ok=True)
    done = []
    for shard in shards:
        action = convert_shard(os.path.join(src_dir, shard), os.path.join(dst_dir, shard))
        log("%-8s %s" % (action, shard))
        done.append((shard, action))
    dst_index = os.path.join(dst_dir, INDEX)
    part = dst_index + ".part"
    with open(part, "wb") as f:
        f.write(new_index)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, dst_index)
    log("written  %s (%d tensors)" % (INDEX, len(index["weight_map"])))
    done.append((INDEX, "written"))
    return done


def encode_index(index: dict) -> bytes:
    # json.dumps' default separators: the derived index sd-server is measured with
    return json.dumps(index).encode("utf-8")


def main(argv: "list[str]") -> int:
    if len(argv) != 2 or argv[0].startswith("-"):
        print("usage: te_rename.py <src_dir> <dst_dir>", file=sys.stderr)
        return 2
    src_dir, dst_dir = argv
    if os.path.abspath(src_dir) == os.path.abspath(dst_dir):
        print("te_rename: <src_dir> and <dst_dir> must differ", file=sys.stderr)
        return 2
    try:
        convert_dir(src_dir, dst_dir)
    except (RenameError, OSError) as e:
        print("te_rename: FAILED: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
