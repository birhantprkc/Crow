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

import ctypes
import os
import sys
import threading
import time

# --- Windows unbuffered reads -------------------------------------------------
# Why this exists: at 68 KB blocks the page cache barely interferes, but at expert
# size (12.75 MiB) it dominates completely. Measured 2026-08-02: depth 1 returned
# 7499 MB/s on a drive that does 5813 MB/s sequentially - impossible from the
# platter, so it was RAM. The negative control caught it (separation 1.05x) and the
# sweep was declared void. FILE_FLAG_NO_BUFFERING is the same mechanism kimi-k3-in-c
# uses as O_DIRECT: the read goes to the device, never to the cache.
#
# Its price is alignment: offset, length AND buffer address must all be multiples of
# the sector size. VirtualAlloc returns page-aligned memory, which satisfies 4096.
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
MEM_COMMIT_RESERVE = 0x3000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
SECTOR = 4096

_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None

if _k32 is not None:
    # Declaring these is not optional on 64-bit. Without an explicit restype ctypes
    # assumes int, which truncates a HANDLE or a pointer to 32 bits. The truncated
    # handle is not -1, so CreateFileW appears to succeed and every ReadFile then
    # returns 0 bytes without raising. Cost of finding that out the hard way:
    # one ZeroDivisionError, 2026-08-02.
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_void_p]
    _k32.VirtualAlloc.restype = ctypes.c_void_p
    _k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.c_uint32, ctypes.c_uint32]
    _k32.VirtualFree.restype = ctypes.c_int
    _k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
    _k32.SetFilePointerEx.restype = ctypes.c_int
    _k32.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                                      ctypes.c_void_p, ctypes.c_uint32]
    _k32.ReadFile.restype = ctypes.c_int
    _k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _k32.CloseHandle.restype = ctypes.c_int
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]


class DirectReader:
    """One handle plus one page-aligned buffer, reading past the page cache."""

    def __init__(self, path, block):
        if block % SECTOR:
            raise ValueError(f"block {block} is not a multiple of {SECTOR}")
        self.block = block
        self.h = _k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None,
                                  OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
        if self.h is None or self.h == 0xFFFFFFFFFFFFFFFF:
            raise OSError(f"CreateFileW failed, error {ctypes.get_last_error()}")
        self.buf = _k32.VirtualAlloc(None, block, MEM_COMMIT_RESERVE, PAGE_READWRITE)
        if not self.buf:
            raise OSError(f"VirtualAlloc failed, error {ctypes.get_last_error()}")
        self._got = ctypes.c_uint32(0)

    def read_at(self, pos):
        """Read one block at `pos`, which is rounded DOWN to a sector boundary."""
        pos -= pos % SECTOR
        if not _k32.SetFilePointerEx(self.h, pos, None, 0):
            raise OSError(f"SetFilePointerEx failed, error {ctypes.get_last_error()}")
        if not _k32.ReadFile(self.h, self.buf, self.block,
                             ctypes.byref(self._got), None):
            raise OSError(f"ReadFile failed at {pos}, error {ctypes.get_last_error()}")
        return self._got.value

    def close(self):
        if self.buf:
            _k32.VirtualFree(ctypes.c_void_p(self.buf), ctypes.c_size_t(0), MEM_RELEASE)
            self.buf = None
        if self.h:
            _k32.CloseHandle(self.h)
            self.h = None

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
    # readinto into a preallocated buffer, NOT read(). Measured 2026-08-02 on the same
    # 155 GB file: read() gave 3041 MB/s, readinto gave 5851 MB/s - a factor of 1.92,
    # and the whole difference is Python allocating a fresh bytes object per call. The
    # earlier figure made the drive look slower than the 5300 MB/s on record and made
    # this very control fail twice on a healthy disk. A control that measures the
    # interpreter cannot vouch for the drive.
    buf = bytearray(block)
    mv = memoryview(buf)
    read = 0
    t0 = time.perf_counter()
    with open(path, "rb", buffering=0) as fh:
        while read < SEQ_BYTES:
            n = fh.readinto(mv)
            if not n:
                break
            read += n
    dt = time.perf_counter() - t0
    return read / dt / 1e6, read, dt


def _validate_sweep(errors, counts, depth):
    """Turn a swallowed thread exception - or a reader that read nothing - into a
    named invalidity.

    Called after join() and before any division. The order matters: dividing
    first is what turned a dead thread into a plausible-looking slow number.

    TWO failure shapes, and the second was found by running the first one under
    load on 2026-08-03. A reader can also finish without raising and without
    reading a single byte, when setting up its handle outruns the time budget.
    Nothing is dead then, errors[] is empty - and the sweep still yields 0 MB/s,
    which becomes the divisor for every later depth. That is the ZeroDivisionError
    the handover recorded as "sporadic": not sporadic at all, but load-dependent.
    """
    dead = [(i, e) for i, e in enumerate(errors) if e is not None]
    if dead:
        detail = "; ".join(f"thread {i}: {type(e).__name__}: {e}" for i, e in dead)
        raise SweepInvalid(f"{len(dead)} of {depth} readers died - {detail}")

    empty = [i for i, n in enumerate(counts) if n == 0]
    if empty:
        raise SweepInvalid(
            f"{len(empty)} of {depth} readers read 0 bytes (threads {empty}) - "
            f"the time budget expired before they read anything. Raise --seconds, "
            f"or take the machine out of whatever else it is doing."
        )


class SweepInvalid(Exception):
    """A thread died, so the sweep measured fewer readers than it reported.

    This exists because the alternative is worse than an error: before it, a
    thread that raised left counts[idx] at zero, threading printed the traceback
    to stderr, and the sweep divided by a total that was missing one reader's
    bytes. The run then looked SLOW rather than BROKEN - and at depth 1 it
    divided by zero instead, which is how the defect was first noticed. A sweep
    with a dead reader is not a slow sweep; it is not a measurement at all.
    """


def random_reads(path, size, block, depth, seconds, direct=False, fail_thread=None):
    """`depth` threads, one file handle each, random offsets, for `seconds`.

    fail_thread is for the failing case only: that thread raises on purpose, so
    the guards below can be shown to fire. Never set it in a real measurement.
    """
    stop = time.perf_counter() + seconds
    counts = [0] * depth
    # One slot per thread. threading swallows exceptions into stderr and the
    # joiner learns nothing, so each worker has to hand its own failure back.
    errors = [None] * depth
    # Offsets are derived per thread from a fixed stride so runs are reproducible
    # without Math.random-style nondeterminism, and so no two threads walk in step.
    span = size - block

    if direct:
        def worker_direct(idx):
            n = 0
            stride = 1_000_003 * (idx * 2 + 1)
            pos = (idx * 7_919_357) % span
            rd = DirectReader(path, block)
            try:
                reads = 0
                while time.perf_counter() < stop:
                    if fail_thread == idx and reads == 3:
                        raise RuntimeError(f"selftest: thread {idx} failing on purpose")
                    n += rd.read_at(pos)
                    reads += 1
                    pos = (pos + stride) % span
            except BaseException as exc:      # noqa: BLE001 - re-raised via errors[]
                errors[idx] = exc
            finally:
                # BOTH assignments belong in finally. counts[idx] used to sit
                # after the try block, so a raising thread left it at zero and
                # its bytes vanished from the total without a word.
                counts[idx] = n
                rd.close()

        threads = [threading.Thread(target=worker_direct, args=(i,)) for i in range(depth)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dt = time.perf_counter() - t0
        _validate_sweep(errors, counts, depth)
        total = sum(counts)
        return total / dt / 1e6, total, dt

    def worker(idx):
        n = 0
        # A large odd stride per thread walks the whole file without repeating soon.
        stride = 1_000_003 * (idx * 2 + 1)
        pos = (idx * 7_919_357) % span
        # One buffer per thread, allocated once. See the note in sequential_control:
        # read() costs 1.92x here, and it costs most exactly where the drive is
        # fastest - at high queue depth - which would flatten the ratio this whole
        # sweep exists to measure.
        buf = bytearray(block)
        mv = memoryview(buf)
        try:
            reads = 0
            with open(path, "rb", buffering=0) as fh:
                while time.perf_counter() < stop:
                    if fail_thread == idx and reads == 3:
                        raise RuntimeError(f"selftest: thread {idx} failing on purpose")
                    fh.seek(pos)
                    n += fh.readinto(mv)
                    reads += 1
                    pos = (pos + stride) % span
        except BaseException as exc:          # noqa: BLE001 - re-raised via errors[]
            errors[idx] = exc
        finally:
            # See worker_direct: this used to sit after the with block.
            counts[idx] = n

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(depth)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.perf_counter() - t0
    _validate_sweep(errors, counts, depth)
    total = sum(counts)
    return total / dt / 1e6, total, dt


def run_sweep(path, size, block, seconds, label, direct=False, fail_thread=None):
    """The sweep itself. Returns (ratio_at_max_depth, rows) so a caller can compare two.

    Raises SweepInvalid if any reader died. It does not return a partial table:
    a sweep with one depth level missing its readers cannot be compared against
    the others, and a ratio computed across it is a number without a meaning.
    """
    print(f"=== sweep: random reads against queue depth - {label} ===")
    print("  depth        rate        vs depth 1")
    base = None
    rows = []
    for depth in (1, 4, 8, 16, 32, 64):
        try:
            mb, got, dt = random_reads(path, size, block, depth, seconds, direct, fail_thread)
        except SweepInvalid as exc:
            print(f"  {depth:5}   INVALID - {exc}")
            raise
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
    direct = False
    fail_thread = None
    for i, a in enumerate(argv):
        if a == "--seconds" and i + 1 < len(argv):
            seconds = float(argv[i + 1])
        if a == "--block-kb" and i + 1 < len(argv):
            block_kb = int(argv[i + 1])
        if a == "--negative-control" and i + 1 < len(argv):
            neg_path = argv[i + 1]
        if a == "--direct":
            direct = True
        # The failing case. A guard that has never fired is indistinguishable
        # from one that is not wired up, so it needs a way to be made to fire.
        if a == "--selftest-fail-thread" and i + 1 < len(argv):
            fail_thread = int(argv[i + 1])
    if direct and sys.platform != "win32":
        print("SETUP ERROR: --direct is Windows-only (FILE_FLAG_NO_BUFFERING)")
        return 2
    if direct and (block_kb * 1024) % SECTOR:
        print(f"SETUP ERROR: --direct needs a block that is a multiple of {SECTOR} B; "
              f"{block_kb} KB is not")
        return 2
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

    mode = "UNBUFFERED, past the page cache" if direct else "buffered, page cache in play"
    print(f"  mode        {mode}")
    print()
    if fail_thread is not None:
        print(f"=== SELFTEST: thread {fail_thread} will raise on purpose ===")
        print("  Required outcome: a named invalidity and exit 1. NOT 0 MB/s, NOT a")
        print("  ZeroDivisionError, NOT a plausible-looking slow number.")
        print()
        try:
            run_sweep(path, size, block, seconds, "selftest", direct, fail_thread)
        except SweepInvalid as exc:
            print()
            print(f"  caught: SweepInvalid: {exc}")
            print("  RESULT: PASS - the guard fires and the sweep is refused.")
            return 1
        print()
        print("  RESULT: FAIL - the sweep completed although a reader was told to die.")
        print("  The guard is not wired up; every number this tool produces is unverified.")
        return 1

    try:
        disk_ratio, disk_rows = run_sweep(path, size, block, seconds, "the real file, on disk", direct)
    except SweepInvalid as exc:
        print()
        print(f"  VERDICT: FAILED - {exc}")
        print("  The sweep is invalid, not slow. A total that is missing a reader's bytes")
        print("  divided by the full wall time understates the drive by exactly the share")
        print("  that died - and says nothing about it. No ratio is reported.")
        return 1
    print()
    # A rate above the sequential ceiling cannot come from the platter. Without --direct
    # this is the loudest sign that the cache is answering; with --direct it means the
    # flag did not take.
    fastest = max(r[1] for r in disk_rows)
    if fastest > mb:
        print(f"  NOTE: peak {fastest:.0f} MB/s exceeds the sequential ceiling of {mb:.0f} MB/s.")
        print("  A platter cannot beat its own sequential rate on random reads, so part of")
        print("  this came from RAM." + ("" if direct else " Try --direct."))
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
    neg_label = "warm file, read UNBUFFERED - warmth must not help" if direct \
                else "warm file, no disk involved"
    neg_ratio, neg_rows = run_sweep(neg_path, neg_size, block, seconds, neg_label, direct)
    print()

    print("=== verdict ===")
    if direct:
        # With --direct the control asks a different question. The point is no longer
        # "does a RAM file climb less" - it is "does the flag actually bypass the
        # cache". A deliberately warmed file read through FILE_FLAG_NO_BUFFERING must
        # NOT reach RAM speed. If it does, the flag was ignored and every number above
        # is a cache measurement wearing a different label.
        neg_depth1 = neg_rows[0][1]
        print(f"  warm file, unbuffered, depth 1: {neg_depth1:8.1f} MB/s")
        print(f"  sequential ceiling:             {mb:8.1f} MB/s")
        if neg_depth1 > mb:
            print()
            print("  VERDICT: FAILED - a file that is entirely in RAM still read faster")
            print("  than the drive's sequential rate. FILE_FLAG_NO_BUFFERING did not")
            print("  take effect, so these numbers are the page cache under a new name.")
            return 1
        print()
        print("  VERDICT: OK - the flag took. Even a fully warmed file had to come from")
        print("  the device, so the sweep above is a property of the drive.")
        print(f"  Depth 64 vs 1 on the real file: {disk_ratio:.2f}x")
        return 0

    separation = disk_ratio / neg_ratio if neg_ratio else float("inf")
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
