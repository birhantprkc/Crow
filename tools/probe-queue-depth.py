"""Can this drive be fed? Random-read throughput against queue depth.

Everything the streaming direction rests on is one extrapolated sentence: "at queue
depth 32 the read rate would exceed the drive's rating". That is textbook NVMe
behaviour applied to a drive nobody measured in this mode. This measures it.

Why it matters (Crow #30): during a 1,659-token prefill the Resource Monitor showed
a disk queue length of 0.79 - fewer than one outstanding request on average - while
llama read 445 MB/s from a drive measured at 5,300 MB/s sequential. 6,540 hard
faults per second at 68 KB each. The drive was idle between requests, not saturated.
If concurrency is what is missing, issuing many reads at once should recover most of
the gap. If it does not, the whole "stream experts in parallel" direction is dead and
that is worth knowing in minutes rather than weeks.

CONTROL, and the reason a green result means anything: a sequential single-threaded
read must reach the drive's known sequential rate. If the harness cannot show a fast
case as fast, it cannot show a slow case as slow either, and the sweep below is void.
The threshold is deliberately generous - this checks the instrument, not the drive.

CAVEAT stated up front: this does not bypass the Windows page cache. After a large
model run part of the file sits in the standby list, so some reads hit RAM and every
absolute number here is optimistic. That inflation applies to every queue depth
roughly equally, so THE RATIO BETWEEN LEVELS is the finding; the absolute MB/s is not.

NEGATIVE CONTROL (--negative-control): the caveat above is exactly what could make the
whole finding an artefact. If the ratio comes from threads scaling rather than from the
drive being fed, a file that sits ENTIRELY in RAM would show the same climb - there is
no drive to feed there. So the same sweep runs against a small, deliberately warmed
file, and its ratio must stay well below the disk ratio. If it does not, this harness
measures Python's thread scaling and the disk finding is void. The sequential control
above proves the harness can show speed; this one proves the speed it shows is the
drive's. A green result without both is not evidence.

Usage:  probe-queue-depth.py <big-file> [--seconds 4] [--block-kb 68]
                             [--negative-control <small-warm-file>]
Exit 0 = both controls passed and the sweep completed.
     1 = a control failed, sweep void.
"""

import os
import sys
import threading
import time

CONTROL_MIN_MB_S = 3000.0   # generous against a drive measured at 5,300 MB/s
CONTROL_BLOCK = 4 * 1024 * 1024
SEQ_BYTES = 4 * 1024 * 1024 * 1024

# The disk sweep has to out-climb the cached sweep by at least this factor, or the
# climb is not about the disk. Set at 2.0 rather than "cached must be flat": reads
# from the page cache still cross a syscall boundary, so some thread scaling there is
# expected and honest. What must not happen is the two being comparable.
NEG_CONTROL_MIN_SEPARATION = 2.0


def human(mb_s):
    return f"{mb_s:8.1f} MB/s"


def sequential_control(path, block=CONTROL_BLOCK):
    """One thread, one handle, straight through. Must be fast or nothing below counts.

    Deliberately uses a LARGE block, unlike the sweep. A first attempt ran the control
    at the sweep's 68 KB and came in at 1,390 MB/s - which was Python's per-call
    overhead (about 47 us across 31,600 calls), not the drive. A control has to measure
    the thing it is controlling for, and here that is the drive plus the harness's
    ability to show speed at all, not the interpreter's syscall cost.
    """
    read = 0
    t0 = time.perf_counter()
    with open(path, "rb", buffering=0) as fh:
        while read < SEQ_BYTES:
            b = fh.read(block)
            if not b:
                break
            read += len(b)
    dt = time.perf_counter() - t0
    return read / dt / 1e6, read, dt


def random_reads(path, size, block, depth, seconds):
    """`depth` threads, one file handle each, random offsets, for `seconds`."""
    stop = time.perf_counter() + seconds
    counts = [0] * depth
    # Offsets are derived per thread from a fixed stride so runs are reproducible
    # without Math.random-style nondeterminism, and so no two threads walk in step.
    span = size - block

    def worker(idx):
        n = 0
        # A large odd stride per thread walks the whole file without repeating soon.
        stride = 1_000_003 * (idx * 2 + 1)
        pos = (idx * 7_919_357) % span
        with open(path, "rb", buffering=0) as fh:
            while time.perf_counter() < stop:
                fh.seek(pos)
                b = fh.read(block)
                n += len(b)
                pos = (pos + stride) % span
        counts[idx] = n

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(depth)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.perf_counter() - t0
    total = sum(counts)
    return total / dt / 1e6, total, dt


def run_sweep(path, size, block, seconds, label):
    """The sweep itself. Returns (ratio_at_max_depth, rows) so a caller can compare two."""
    print(f"=== sweep: random reads against queue depth - {label} ===")
    print("  depth        rate        vs depth 1")
    base = None
    rows = []
    for depth in (1, 4, 8, 16, 32, 64):
        mb, got, dt = random_reads(path, size, block, depth, seconds)
        if base is None:
            base = mb
        ratio = mb / base
        rows.append((depth, mb, ratio))
        print(f"  {depth:5}   {human(mb)}   {ratio:8.2f}x")
    return rows[-1][2], rows


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    seconds = 4.0
    block_kb = 68
    neg_path = None
    for i, a in enumerate(argv):
        if a == "--seconds" and i + 1 < len(argv):
            seconds = float(argv[i + 1])
        if a == "--block-kb" and i + 1 < len(argv):
            block_kb = int(argv[i + 1])
        if a == "--negative-control" and i + 1 < len(argv):
            neg_path = argv[i + 1]
    if not os.path.exists(path):
        print(f"SETUP ERROR: no such file: {path}")
        return 2
    if neg_path and not os.path.exists(neg_path):
        print(f"SETUP ERROR: no such negative-control file: {neg_path}")
        return 2

    block = block_kb * 1024
    size = os.path.getsize(path)
    print(f"file        {path}")
    print(f"size        {size:,} bytes")
    print(f"block       {block_kb} KB   (the measured average hard-fault size)")
    print(f"per level   {seconds} s")
    print()

    print(f"=== control: sequential, single thread, {CONTROL_BLOCK//1024//1024} MB blocks, must be fast ===")
    mb, got, dt = sequential_control(path)
    print(f"  {human(mb)}   ({got/1e9:.2f} GB in {dt:.1f} s)")
    if mb < CONTROL_MIN_MB_S:
        print(f"  VERDICT: FAILED - below {CONTROL_MIN_MB_S:.0f} MB/s.")
        print("  The harness cannot show a fast case as fast, so it cannot show a slow")
        print("  case as slow either. The sweep below would prove nothing. Stopping.")
        return 1
    print(f"  VERDICT: OK - above {CONTROL_MIN_MB_S:.0f} MB/s, the harness can measure speed.")
    print()

    disk_ratio, _ = run_sweep(path, size, block, seconds, "the real file, on disk")
    print()
    print("The ratio column is the finding. Absolute values are optimistic because")
    print("this does not bypass the page cache - see the caveat in the header.")
    print()

    if neg_path is None:
        print("NOTE: no --negative-control given. The sweep above cannot distinguish")
        print("between feeding the drive and Python threads scaling. Treat it as")
        print("indicative, not as evidence.")
        return 0

    neg_size = os.path.getsize(neg_path)
    print(f"=== negative control: {neg_path} ===")
    print(f"  size        {neg_size:,} bytes - small enough to sit entirely in RAM")
    print("  warming it so every read below is a cache hit, not a disk read")
    warm = 0
    with open(neg_path, "rb", buffering=0) as fh:
        while True:
            b = fh.read(CONTROL_BLOCK)
            if not b:
                break
            warm += len(b)
    print(f"  warmed      {warm/1e9:.2f} GB")
    print()
    neg_ratio, _ = run_sweep(neg_path, neg_size, block, seconds, "warm file, no disk involved")
    print()

    separation = disk_ratio / neg_ratio if neg_ratio else float("inf")
    print("=== verdict ===")
    print(f"  disk sweep    depth 64 vs 1: {disk_ratio:6.2f}x")
    print(f"  cached sweep  depth 64 vs 1: {neg_ratio:6.2f}x")
    print(f"  separation:                  {separation:6.2f}x   (must be >= {NEG_CONTROL_MIN_SEPARATION})")
    if separation < NEG_CONTROL_MIN_SEPARATION:
        print()
        print("  VERDICT: FAILED - a file that never touches the drive climbs about as")
        print("  much as the real one. That makes this harness a thread-scaling meter,")
        print("  not a disk meter, and the concurrency finding on #30 rests on nothing.")
        return 1
    print()
    print("  VERDICT: OK - the climb needs the drive. A RAM-resident file does not")
    print("  reproduce it, so the ratio above is about feeding the disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
