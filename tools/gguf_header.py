"""Read a GGUF header and check it against expected values.

Integrity check without hashing 155 GB: the header carries tensor count, KV count
and the architecture hyperparameters. A truncated or scrambled download shows up
here immediately, and reading it costs a few MB instead of the whole file.

Usage:  gguf_header.py <file.gguf> [--expect key=value ...]
Exit 0 = every expectation met. Exit 1 = at least one mismatch. Exit 2 = unreadable.
"""

import os
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
    # Numeric arrays short enough to read at a glance are printed with their values.
    # A loader condition like "every compress_ratio must be 0" cannot be checked
    # against "<array of 3 type 6>". Longer arrays keep their shape — a 130k token
    # list is not something anyone reads, and holding it costs memory for nothing.
    #
    # 64 and not 16: these arrays are per-layer, so the limit has to clear a real
    # layer count or the check silently stops working on the bigger model. The
    # 44-entry deepseek4.attention.compress_ratios in DeepSeek-V4-Flash-MXFP4.gguf
    # is the counter-sample this output is verified against, and at 16 it fell out.
    ARRAY_VALUE_LIMIT = 64

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
            # Long arrays are token lists and the like; keep the shape, not the payload.
            if elem_t == STRING:
                for _ in range(n):
                    self.string()
            elif elem_t == ARRAY:
                raise ValueError("nested arrays are not supported")
            elif n <= self.ARRAY_VALUE_LIMIT:
                vals = [self.fixed(elem_t) for _ in range(n)]
                if elem_t == BOOL:
                    vals = [bool(v) for v in vals]
                # The element count is printed even when the values are: an array
                # read as empty prints as [], and [] must never be mistakable for
                # "every value is 0" by whoever checks a loader condition.
                return f"{vals} ({n} values, type {elem_t})"
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

        # Tensor table. Sizes are derived from the gaps between consecutive data
        # offsets rather than from a hardcoded ggml type table: the file itself is
        # the authority on how much room each tensor takes, and a type table would
        # have to be kept in step with upstream. The gaps include GGUF's alignment
        # padding, so a size is an upper bound by at most `general.alignment` bytes.
        tensors = []
        for _ in range(n_tensors):
            name = r.string()
            ndim = r.fixed(UINT32)
            dims = [r.fixed(UINT64) for _ in range(ndim)]
            ttype = r.fixed(UINT32)
            offset = r.fixed(UINT64)
            tensors.append({"name": name, "dims": dims, "type": ttype, "offset": offset})

        alignment = int(kv.get("general.alignment", 32))
        pos = fh.tell()
        data_start = pos + (-pos % alignment)
        file_size = os.path.getsize(path)

        by_offset = sorted(tensors, key=lambda t: t["offset"])
        for i, t in enumerate(by_offset):
            nxt = by_offset[i + 1]["offset"] if i + 1 < len(by_offset) else file_size - data_start
            t["size"] = nxt - t["offset"]

        return version, n_tensors, n_kv, kv, tensors, data_start, file_size


def layer_of(name):
    """blk.17.ffn_gate_exps.weight -> 17; anything outside a block -> None."""
    parts = name.split(".")
    if len(parts) > 2 and parts[0] == "blk" and parts[1].isdigit():
        return int(parts[1])
    return None


def report_tensors(tensors, data_start, file_size, n_tensors):
    total = sum(t["size"] for t in tensors)
    exps = [t for t in tensors if "_exps" in t["name"]]
    resident = [t for t in tensors if "_exps" not in t["name"]]

    per_layer = {}
    for t in exps:
        li = layer_of(t["name"])
        per_layer.setdefault(li, 0)
        per_layer[li] += t["size"]

    gib = 1024 ** 3
    print()
    print("=== tensor table ===")
    print(f"  tensors              {len(tensors)} (header says {n_tensors})")
    print(f"  data starts at       {data_start}")
    print(f"  sum of tensor sizes  {total} ({total / gib:.2f} GiB)")
    print(f"  data_start + sum     {data_start + total}")
    print(f"  file size            {file_size}")
    print(f"  difference           {file_size - data_start - total}  "
          f"(must be 0 — the sizes account for the whole file)")
    print()
    print(f"  expert tensors (_exps)   {len(exps):>5}   {sum(t['size'] for t in exps) / gib:>9.2f} GiB")
    print(f"  everything else          {len(resident):>5}   {sum(t['size'] for t in resident) / gib:>9.2f} GiB")

    if per_layer:
        sizes = sorted(per_layer.values())
        layers = sorted(k for k in per_layer if k is not None)
        print()
        print(f"  layers carrying experts  {len(per_layer)}"
              + (f"  (blk {layers[0]}..{layers[-1]})" if layers else ""))
        print(f"  per layer, smallest      {sizes[0] / gib:.3f} GiB")
        print(f"  per layer, largest       {sizes[-1] / gib:.3f} GiB")
        print(f"  per layer, mean          {sum(sizes) / len(sizes) / gib:.3f} GiB")
    return per_layer, resident


def report_budget(per_layer, resident, budget_gib):
    """How many layers of experts fit in a VRAM budget, weights only.

    This is a PREDICTION from the file, not a measurement of what llama.cpp does.
    It counts weights and nothing else: no KV cache, no compute buffers, no CUDA
    context. Those are real and they are not small, so the layer count below is an
    upper bound. The load log is the authority; this exists to be held against it.
    """
    gib = 1024 ** 3
    budget = budget_gib * gib
    resident_bytes = sum(t["size"] for t in resident)
    layers = sorted((li for li in per_layer if li is not None), reverse=True)

    print()
    print(f"=== prediction for a {budget_gib:g} GiB budget (weights only) ===")
    print(f"  non-expert weights   {resident_bytes / gib:.2f} GiB")
    if resident_bytes >= budget:
        print(f"  VERDICT: the non-expert weights alone exceed {budget_gib:g} GiB — "
              f"no expert layer fits, and -ngl must drop below 99")
        return
    left = budget - resident_bytes
    print(f"  left for experts     {left / gib:.2f} GiB")

    fitted = 0
    used = 0
    for li in layers:  # llama.cpp offloads the last layers first under --n-cpu-moe
        if used + per_layer[li] > left:
            break
        used += per_layer[li]
        fitted += 1

    n_expert_layers = len(layers)
    ncmoe = n_expert_layers - fitted
    print(f"  expert layers fitting {fitted} of {n_expert_layers}  ({used / gib:.2f} GiB)")
    print(f"  => --n-cpu-moe {ncmoe}   (that many layers keep their experts on the CPU)")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    expect = {}
    want_tensors = False
    budgets = []
    for a in argv[2:]:
        if a == "--expect":
            continue
        if a == "--tensors":
            want_tensors = True
            continue
        if a.startswith("--budget-gib="):
            want_tensors = True
            budgets.append(float(a.split("=", 1)[1]))
            continue
        k, _, v = a.partition("=")
        expect[k] = v

    try:
        version, n_tensors, n_kv, kv, tensors, data_start, file_size = read_header(path)
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

    if want_tensors:
        per_layer, resident = report_tensors(tensors, data_start, file_size, n_tensors)
        for b in budgets:
            report_budget(per_layer, resident, b)

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
