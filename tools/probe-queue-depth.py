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

Usage:  probe-queue-depth.py <big-file> [--seconds 4] [--block-kb 68]
Exit 0 = the control passed and the sweep completed. 1 = control failed, sweep void.
"""

import os
import sys
import threading
import time

CONTROL_MIN_MB_S = 3000.0   # generous against a drive measured at 5,300 MB/s
CONTROL_BLOCK = 4 * 1024 * 1024
SEQ_BYTES = 4 * 1024 * 1024 * 1024


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


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    seconds = 4.0
    block_kb = 68
    for i, a in enumerate(argv):
        if a == "--seconds" and i + 1 < len(argv):
            seconds = float(argv[i + 1])
        if a == "--block-kb" and i + 1 < len(argv):
            block_kb = int(argv[i + 1])
    if not os.path.exists(path):
        print(f"SETUP ERROR: no such file: {path}")
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

    print("=== sweep: random reads against queue depth ===")
    print("  depth        rate        vs depth 1")
    base = None
    for depth in (1, 4, 8, 16, 32, 64):
        mb, got, dt = random_reads(path, size, block, depth, seconds)
        if base is None:
            base = mb
        print(f"  {depth:5}   {human(mb)}   {mb/base:8.2f}x")
    print()
    print("The ratio column is the finding. Absolute values are optimistic because")
    print("this does not bypass the page cache - see the caveat in the header.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
