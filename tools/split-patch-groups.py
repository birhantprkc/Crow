"""E3: split the worktree patch into the three groups of Frage 0.

WHY: worktree-on-b10223.patch mixes three changes with three different audiences.
Group (i) expert streaming is what robin's operating point runs; group (ii) token
timing is build diagnostics behind LLAMA_TOKEN_TIMING that no operating run sets;
group (iii) is a one-hunk dflash load-condition fix that the base jump drops
entirely. One file means every base jump carries all three, and the one that is
meant to be dropped has to be cut out of an otherwise valid patch by hand.

WORKS ON A -U0 DIFF ON PURPOSE. With default context, neighbouring hunks merge --
include/llama.h collapses 4 into 3 and src/llama-context.cpp 11 into 9 -- and a
merged block carries two groups at once, which is the thing this split undoes.
Produce the input with NAMED paths, never -A and never "." (parallel sessions
share the index):

    git add -N src/llama-moe-stream.cpp src/llama-moe-stream.h tests/test-llama-file.cpp
    git diff -U0 > u0.patch
    git reset -- src/llama-moe-stream.cpp src/llama-moe-stream.h tests/test-llama-file.cpp

WHY THE HUNKS ARE APPLIED HERE AND NOT BY `git apply --unidiff-zero`: that was the
first attempt and it silently misplaces them. A context-free hunk "@@ -35,0 +36,1 @@"
does not say whether the line goes after old line 35 or before old line 36, and git
picks one. Measured 2026-08-05: llama-moe-stream.cpp landed in line 36 of
src/CMakeLists.txt instead of 37, exit 0, no warning, line counts still matching --
only a hash caught it. So hunks are applied by line number, back to front, and every
removed line is checked against the pristine file first. That check is why this is
trustworthy: an off-by-one shows up as a mismatch instead of a plausible file.

THE ONE HUNK THAT CARRIES BOTH GROUPS is number 11 of src/llama-context.cpp:

    (i)   +          blank
    (i)   +    if (const auto * mstream = ...) {
    (i)   +        mstream->print_stats();
    (i)   +    }
    ---   +}                       <- closes llama_perf_context_print
    (ii)  +          blank, comment, int32_t llama_perf_context_eval_steps(...) ...

The file's original brace at old line 4136 ends up closing the NEW function, which
is why the hunk inserts one. Group (i) alone keeps that brace in its old job and
takes only the first four lines; group (ii) alone anchors one line later and brings
a brace of its own. Both orders yield identical text -- verified, see below.

MEASURED 2026-08-05, all against the live tree by SHA-256 after CR removal:
  group (i)  18 foreign files + 3 of our own, group (ii) 4, group (iii) 1
  each patch alone on pristine b10223: applies
  all three, all six orders, `git apply --3way`: 22 of 22 plus common/sampling.cpp,
    no conflict markers. sampling.cpp is the ONE approved deviation from the live
    tree: group (i) is re-anchored behind common_memory_breakdown_print, so the
    streaming stats print after the memory breakdown instead of before. Statements
    are identical - compared as a sorted, comment-stripped set - so nothing is lost
    or added, only the order and the comment differ.
  all three, all six orders, plain `git apply`: NONE work -- the second patch to
    touch llama-context.cpp or llama.h fails, because both were diffed against
    pristine and their context lines overlap. --3way IS MANDATORY for combining,
    and it works only because the b10223 blobs are in the same object store.
  second run of this tool: all three patches byte-identical

THREE FILES ARE SHARED between groups, and every one of them for the same reason:
the streaming change and the timing change wanted the same anchor. include/llama.h
splits by hunk, src/llama-context.cpp and common/sampling.cpp split by line.

Usage: split-patch-groups.py <u0.patch> <group: i|ii|iii> <srcdir> <outdir>
       Writes that group's target files into outdir; run `git diff` there to get
       the shippable patch with normal context.
Exit 1 if any hunk does not match the pristine file.
"""

import os
import re
import subprocess
import sys

WHOLE_FILE = {
    "src/llama-context.h": "ii",
    "src/models/dflash.cpp": "iii",
}
# 1-based hunk indices in the -U0 diff belonging to group (ii); the rest is (i).
HUNKS_II = {
    "include/llama.h": {3},
    "src/llama-context.cpp": {3, 4, 9, 10},
}

# Hunks carrying BOTH groups, split by line. Two so far, and both for the same
# reason: the streaming change and the timing change wanted the same anchor.
BRACE_SPLIT = ("src/llama-context.cpp", 11)
SAMPLING_SPLIT = ("common/sampling.cpp", 1)
# The line that starts the group (i) tail of the sampling hunk. Matched on text and
# not on an index, because an index silently shifts when the comment above it grows.
SAMPLING_MARKER = "Upstream PR #25294"

# The group (i) tail is RE-ANCHORED one original line down, after this call.
# Why: both blocks originally insert at the same base line (b10223 sampling.cpp:549,
# in front of common_memory_breakdown_print), and two independent inserts at ONE base
# line cannot be carried by two patches that each have to apply on their own. Measured
# 2026-08-05: sharing the anchor left exactly one conflict marker in all six
# application orders, 22 of 23. Moving this block behind the call gives it an anchor
# of its own. The cost is the print order, and the comment now says so instead of
# claiming the old position.
SAMPLING_ANCHOR_AFTER = "common_memory_breakdown_print"
SAMPLING_TAIL = [
    "",
    "        // Expert-streaming statistics, printed AFTER common_memory_breakdown_print and",
    "        // not in front of it, where upstream PR #25294 places them. The per-token series",
    "        // above wants that same anchor, and two independent inserts at one base line",
    "        // cannot be split across two patches that each have to apply on their own:",
    "        // measured 2026-08-05, sharing it left a conflict in all six orders. Both still",
    "        // print, because the series explains the shape of a token's time while these",
    "        // stats explain how much of it went on fetching experts, and reading one without",
    "        // the other is how a stall gets blamed on the wrong tier. No-op while streaming",
    "        // is off.",
]

BASE_TAG = "b10223"

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class Hunk:
    def __init__(self, old_start, old_len):
        self.old_start, self.old_len = old_start, old_len
        self.removed, self.added = [], []
        # Optional: text the pristine line directly above this insert must contain.
        # A re-anchored hunk that is one line off would otherwise still apply, and
        # produce a file that looks right.
        self.expect_above = None


def parse(path):
    files, cur = [], None
    for line in open(path, encoding="utf-8", errors="surrogateescape").read().split("\n"):
        m = re.match(r"^diff --git a/(.+?) b/", line)
        if m:
            cur = {"name": m.group(1), "new": False, "hunks": []}
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur["new"] = True
            continue
        m = HUNK_RE.match(line)
        if m:
            a, b, _, _ = m.groups()
            cur["hunks"].append(Hunk(int(a), 1 if b is None else int(b)))
            continue
        if not cur["hunks"]:
            continue
        h = cur["hunks"][-1]
        if line.startswith("-"):
            h.removed.append(line[1:])
        elif line.startswith("+"):
            h.added.append(line[1:])
    return files


def split_brace(h):
    """llama-context.cpp hunk 11 -> (group i, group ii)."""
    if h.old_len != 0 or h.removed:
        sys.exit(f"FAIL: hunk {BRACE_SPLIT[1]} of {BRACE_SPLIT[0]} is not a pure insertion")
    if len(h.added) < 6 or h.added[4] != "}":
        sys.exit(f"FAIL: expected a closing brace at added line 5, got {h.added[4]!r}. "
                 "The file changed shape; re-read the hunk before trusting this split.")
    a = Hunk(h.old_start, 0)
    a.added = h.added[0:4]
    b = Hunk(h.old_start + 1, 0)
    b.added = h.added[5:] + ["}"]
    return a, b


def split_sampling(h):
    """common/sampling.cpp hunk 1 -> (group i tail, group ii head).

    The plan calls this file "group (ii) only, 74 of 74 lines". Measured 2026-08-05
    the hunk is 66 to 8: the tail is a comment plus llama_moe_stream_print_stats(),
    which is group (i). Leaving it in (ii) cost two things - token-timing alone
    failed to compile with C3861, and the core patch lost the only print path that
    survives default verbosity, so after the jump the streaming statistics would be
    silent.

    Two line counts, and they are NOT the same number. 66/8 is how the original hunk
    divides. What ships is 66 for group (ii) unchanged and 11 for group (i), because
    re-anchoring rewrites the comment - measured: sampling.cpp goes from 887 pristine
    lines to 953 under (ii) alone and to 898 under (i) alone.

    Split on the marker text rather than an index: the comment above it will grow.
    """
    if h.old_len != 0 or h.removed:
        sys.exit(f"FAIL: hunk {SAMPLING_SPLIT[1]} of {SAMPLING_SPLIT[0]} is not a pure insertion")
    at = [i for i, l in enumerate(h.added) if SAMPLING_MARKER in l]
    if len(at) != 1:
        sys.exit(f"FAIL: expected {SAMPLING_MARKER!r} exactly once in the sampling hunk, "
                 f"found {len(at)}. Re-read the hunk before trusting this split.")
    cut = at[0]
    head, tail = h.added[:cut], h.added[cut:]
    # The check that would have caught the original mis-grouping: the group (i)
    # symbol must end up in group (i) and nowhere else.
    call = [l for l in tail if "llama_moe_stream_print_stats" in l]
    if len(call) != 1:
        sys.exit(f"FAIL: expected exactly one llama_moe_stream_print_stats call in the "
                 f"group (i) tail, found {len(call)}")
    if any("llama_moe_stream_print_stats" in l for l in head):
        sys.exit("FAIL: llama_moe_stream_print_stats is still in the group (ii) head")

    # Group (i): re-anchored one original line down, with a rewritten comment. The
    # call line itself is carried over verbatim rather than retyped.
    a = Hunk(h.old_start + 1, 0)
    a.added = SAMPLING_TAIL + call
    a.expect_above = SAMPLING_ANCHOR_AFTER
    # Group (ii): unchanged, still at the original anchor.
    b = Hunk(h.old_start, 0)
    b.added = head
    return a, b


def pick(f, group):
    name = f["name"]
    if name in WHOLE_FILE:
        return list(f["hunks"]) if WHOLE_FILE[name] == group else []
    if group == "iii":
        return []
    ii = HUNKS_II.get(name, set())
    out = []
    for idx, h in enumerate(f["hunks"], start=1):
        if (name, idx) == BRACE_SPLIT:
            out.append(split_brace(h)[0 if group == "i" else 1])
        elif (name, idx) == SAMPLING_SPLIT:
            out.append(split_sampling(h)[0 if group == "i" else 1])
        elif (idx in ii) == (group == "ii"):
            out.append(h)
    return out


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    u0, group, srcdir, outdir = sys.argv[1:5]
    if group not in ("i", "ii", "iii"):
        return print(f"FAIL: unknown group {group!r}") or 2

    rc = 0
    for f in parse(u0):
        hunks = pick(f, group)
        if not hunks:
            continue
        name = f["name"]
        if f["new"]:
            lines = []
        else:
            blob = subprocess.run(["git", "-C", srcdir, "show", f"{BASE_TAG}:{name}"],
                                  capture_output=True, check=True).stdout
            lines = blob.decode("utf-8", "surrogateescape").split("\n")
            if lines and lines[-1] == "":
                lines.pop()

        for h in sorted(hunks, key=lambda x: x.old_start, reverse=True):
            if h.old_len:
                at = h.old_start - 1
                have = lines[at:at + h.old_len]
                if have != h.removed:
                    print(f"  MISMATCH {name} @{h.old_start}: pristine has {have!r}, "
                          f"patch expects {h.removed!r}")
                    rc = 1
                    continue
                lines[at:at + h.old_len] = h.added
            else:
                if h.expect_above:
                    above = lines[h.old_start - 1] if h.old_start >= 1 else ""
                    if h.expect_above not in above:
                        print(f"  MISMATCH {name} @{h.old_start}: expected the line above a "
                              f"re-anchored insert to contain {h.expect_above!r}, found {above!r}")
                        rc = 1
                        continue
                lines[h.old_start:h.old_start] = h.added

        dest = os.path.join(outdir, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"  {name}: {len(hunks)} hunks -> {len(lines)} lines")
    return rc


if __name__ == "__main__":
    sys.exit(main())
