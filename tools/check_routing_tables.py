"""Count rows with a repeated expert id in a model's static routing tables.

WHY THIS EXISTS
`ggml-org/llama.cpp` #24591: passing non-unique ids to the CUDA `ggml_mul_mat_id()` reads out
of bounds and crashes with an illegal memory access. On 2026-08-05 a third reporter established
the source of the duplicates - REAP-pruned checkpoints remap 256 experts onto fewer without
deduplicating the slot lists, so roughly 3000 tokens per layer carry a repeat, and any prompt
containing one of those tokens crashes. It presents as a content-dependent race.

Architectures with `hash_layer_count > 0` read the first layers' routing from a static table
(`blk.N.ffn_gate_tid2eid.weight`, one row of `expert_used_count` ids per vocabulary token)
instead of computing top-k. That table is where a duplicate can sit, and it is readable without
running anything.

The same check was run on 2026-08-02 against the official conversion from a throwaway script.
It is in the repository now because it has been needed twice.

A ZERO IS ONLY A STATEMENT ONCE THE FAILURE MODE IS EXCLUDED
If the offset were wrong, the bytes would be arbitrary data that might happen to hold no
duplicates. So every run also reports:
  * the value range - ids must fall within [0, expert_count-1];
  * how many distinct ids occur - the whole expert population should;
  * the per-expert load, against the flat expectation;
  * and the chance baseline: if the slots were drawn independently, a known fraction of rows
    would contain a repeat anyway. Measuring far below that is what makes a zero mean
    "constructed to be duplicate-free" rather than "we got lucky".

WHAT IT DOES NOT COVER
Layers beyond the hash layers route through a learned top-k gate and carry no table. Whether
that path can emit a repeated index is a different question and is not answered here.

Usage:
  check_routing_tables.py <model.gguf> [<more-shards.gguf> ...]
  check_routing_tables.py --selftest
Exit 0 = tables read and no duplicate rows. 1 = duplicates found. 2 = could not read.
         3 = SELFTEST BROKEN.
"""

import array
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_header_reader():
    """gguf_header.py already parses the header; the hyphen-free name is the only reason
    this needs a path import rather than a plain one."""
    spec = importlib.util.spec_from_file_location(
        "gguf_header", os.path.join(HERE, "gguf_header.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def duplicate_rows(values, n_slots):
    """Rows (of n_slots consecutive ids) that contain the same id twice.

    Returns (count, first_examples) with at most five examples as (row_index, row).
    """
    dup, examples = 0, []
    for i in range(0, len(values), n_slots):
        row = values[i:i + n_slots]
        if len(set(row)) != len(row):
            dup += 1
            if len(examples) < 5:
                examples.append((i // n_slots, list(row)))
    return dup, examples


def chance_rows_with_repeat(n_rows, n_slots, n_experts):
    """How many rows would contain a repeat if the slots were drawn independently.

    The birthday probability: 1 - prod_{i<k} (N-i)/N. Without this number a measured zero
    cannot be told from a table that is small or sparse enough to be duplicate-free by luck.
    """
    p_distinct = 1.0
    for i in range(n_slots):
        p_distinct *= (n_experts - i) / float(n_experts)
    return (1.0 - p_distinct) * n_rows, (1.0 - p_distinct)


def selftest():
    """Prove the counter can go red before any model is read."""
    failures = []
    clean = [0, 1, 2, 3, 4, 5] * 4
    n, _ = duplicate_rows(clean, 6)
    if n != 0:
        failures.append("a table without repeats reported %d duplicate rows" % n)

    dirty = [0, 1, 2, 3, 4, 5] + [7, 9, 7, 3, 4, 5] + [0, 1, 2, 3, 4, 5]
    n, ex = duplicate_rows(dirty, 6)
    if n != 1:
        failures.append("a table with exactly one repeated row reported %d" % n)
    elif ex[0][0] != 1:
        failures.append("the repeated row was located at index %d, expected 1" % ex[0][0])

    exp, p = chance_rows_with_repeat(129280, 6, 256)
    if not (0.05 < p < 0.06):
        failures.append("chance baseline for 6 of 256 came out at %.4f, expected ~0.057" % p)

    if failures:
        print("RESULT: SELFTEST BROKEN")
        for f in failures:
            print("  - %s" % f)
        return 3
    print("RESULT: SELFTEST OK - a clean table reads 0, a table with one repeat reads 1 at the")
    print("        right row, and the chance baseline for 6 of 256 is %.2f %% of rows." % (100 * p))
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    if argv[1] == "--selftest":
        return selftest()

    gh = _load_header_reader()
    paths = argv[1:]
    found_any = False
    worst = 0

    # A split GGUF carries the full KV block only in its first shard, while the tables live in
    # a later one. Read every header first and take the hyperparameters from whichever shard
    # has them, or the check dies on the very file it is meant to read.
    headers = {}
    arch, n_experts, n_slots = "?", 0, 0
    for path in paths:
        if not os.path.isfile(path):
            print("SETUP ERROR: no such file: %s" % path)
            return 2
        try:
            headers[path] = gh.read_header(path)
        except Exception as e:
            print("SETUP ERROR: %s: %s" % (os.path.basename(path), e))
            return 2
        kv = headers[path][3]
        a = kv.get("general.architecture")
        if a:
            arch = a
            n_experts = int(kv.get("%s.expert_count" % a, 0) or 0) or n_experts
            n_slots = int(kv.get("%s.expert_used_count" % a, 0) or 0) or n_slots

    for path in paths:
        _, _, _, kv, tensors, data_start, _ = headers[path]
        tables = [t for t in tensors if "tid2eid" in t["name"]]
        if not tables:
            continue
        found_any = True
        if not n_experts or not n_slots:
            print("SETUP ERROR: routing tables found, but no expert_count/expert_used_count "
                  "in any of the given shards (arch %r). Pass the shard carrying the KV block "
                  "as well." % arch)
            return 2

        print("%s  (arch %s, %d experts, %d used)"
              % (os.path.basename(path), arch, n_experts, n_slots))

        with open(path, "rb") as fh:
            for t in sorted(tables, key=lambda x: x["name"]):
                n_values = 1
                for d in t["dims"]:
                    n_values *= d
                fh.seek(data_start + t["offset"])
                raw = fh.read(n_values * 4)
                if len(raw) != n_values * 4:
                    print("  %-34s SHORT READ - %d of %d bytes"
                          % (t["name"], len(raw), n_values * 4))
                    return 2
                vals = array.array("i")
                vals.frombytes(raw)
                if sys.byteorder != "little":
                    vals.byteswap()

                n_rows = n_values // n_slots
                dup, examples = duplicate_rows(vals, n_slots)
                lo, hi = min(vals), max(vals)
                distinct = len(set(vals))
                load = [0] * n_experts
                in_range = True
                for v in vals:
                    if 0 <= v < n_experts:
                        load[v] += 1
                    else:
                        in_range = False
                expected, p = chance_rows_with_repeat(n_rows, n_slots, n_experts)

                worst = max(worst, dup)
                print("  %-34s rows %7d   rows with a repeat %7d" % (t["name"], n_rows, dup))
                print("      value range %d..%d %s, %d distinct ids of %d"
                      % (lo, hi, "(in range)" if in_range else "(OUT OF RANGE)",
                         distinct, n_experts))
                if in_range and distinct == n_experts:
                    print("      per-expert load %d..%d against a flat %d"
                          % (min(load), max(load), n_values // n_experts))
                print("      by chance alone %d rows (%.2f %%) would repeat" % (expected, 100 * p))
                for idx, row in examples:
                    print("      row %d: %s" % (idx, row))

    if not found_any:
        # Saying "no duplicates" about a file that carries no tables at all would be the
        # emptiest possible green.
        print("RESULT: no tid2eid tables in these files - nothing was checked.")
        return 2

    print()
    if worst:
        print("RESULT: DUPLICATES PRESENT - %d rows in the worst table. Prompts containing an "
              "affected token can crash the CUDA mul_mat_id path (llama.cpp #24591)." % worst)
        return 1
    print("RESULT: no duplicate rows in any static routing table of these files.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
