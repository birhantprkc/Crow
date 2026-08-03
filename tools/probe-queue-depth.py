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

THREE ARMS ANSWER THE WINDOWS QUESTIONS THAT STAND BEFORE ANY DIRECT-IO BUILD.
The plan (Crow/02_docs/windows-braucht-zuerst-eine-positionierte-leseoperation.md,
stage 3) names three things that are documented or predicted but never measured on
this machine. Each is a switch here rather than a second program, because this file
already carries CreateFileW, FILE_FLAG_NO_BUFFERING, the page-aligned buffer, the
negative control and the ctypes trap that cost a day to find.

  --shared-handle   Arm 1. Same file, same offsets, same blocks, one SHARED handle
                    across all threads instead of one per thread. MS Learn says
                    Windows serialises I/O on a synchronous handle, which would put
                    queue depth at 1 no matter how many threads run. That sentence is
                    documentation, not a measurement. Reference numbers from the
                    per-thread form, 2026-08-02: 4812.2 MB/s at depth 1, 10523.9 MB/s
                    at depth 8. If the shared arm stays near depth 1, build form (A)
                    - one handle per thread - is proven with a number instead of a
                    citation. If it also reaches ~10500, the doc does not bite here
                    and the build form has to be chosen again.
                    Only the HANDLE is shared. Buffer and byte counter stay per
                    thread: ReadFile writes the byte count into that counter, so
                    sharing it would let one thread read another's result and the
                    throughput would rest on a corrupted numerator.

  --eof-tail        Arm 2. Read the last sector-multiple block of the file, the one
                    whose end lies PAST the logical EOF, under FILE_FLAG_NO_BUFFERING.
                    Not an edge case: none of the four model files ends on a sector
                    boundary. Prediction under test - Windows returns TRUE and reports
                    exactly the bytes up to EOF. Either outcome is usable; they lead
                    to different builds in stage 4.

  --sector-query    Arm 3. Ask the device for its sector sizes via
                    IOCTL_STORAGE_QUERY_PROPERTY instead of believing SECTOR = 4096.
                    Expected on this drive: 512 logical, 4096 physical. If that holds,
                    the constant is confirmed - but from then on as a query, not as
                    luck. This arm reports; it does not change what the sweep uses.

The arms run ONE AT A TIME, never combined. Mixing the shared handle and the EOF read
in one run leaves nobody able to say which arm produced the number.

Usage:  probe-queue-depth.py <big-file> [--seconds 4] [--block-kb 68]
                             [--negative-control <small-warm-file>]
                             [--direct] [--shared-handle]
                             [--eof-tail] [--sector-query]
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
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
MEM_COMMIT_RESERVE = 0x3000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
SECTOR = 4096
INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF

# Arm 3. The alignment duty of FILE_FLAG_NO_BUFFERING is the LOGICAL sector size;
# Microsoft's performance recommendation is the PHYSICAL one. SECTOR = 4096 above
# happens to be right on this drive and is also the better of the two - but it was
# never asked for, and the same constant is written a third time in PR #25294
# (MOE_STREAM_DIRECT_ALIGN). Three places, one truth, and none of them queried.
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
STORAGE_ACCESS_ALIGNMENT_PROPERTY = 6
PROPERTY_STANDARD_QUERY = 0

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
    _k32.DeviceIoControl.restype = ctypes.c_int
    _k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]


class STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [("PropertyId", ctypes.c_uint32),
                ("QueryType", ctypes.c_uint32),
                ("AdditionalParameters", ctypes.c_ubyte * 1)]


class STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_uint32),
                ("Size", ctypes.c_uint32),
                ("BytesPerCacheLine", ctypes.c_uint32),
                ("BytesOffsetForCacheAlignment", ctypes.c_uint32),
                ("BytesPerLogicalSector", ctypes.c_uint32),
                ("BytesPerPhysicalSector", ctypes.c_uint32),
                ("BytesOffsetForSectorAlignment", ctypes.c_uint32)]


def open_direct_handle(path):
    """One unbuffered handle. Separate from DirectReader so Arm 1 can share it.

    Arm 1 needs exactly one of these for all threads, opened before they start and
    closed after they join - which is why it cannot live inside the reader that each
    thread builds for itself.
    """
    h = _k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None,
                         OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
    if h is None or h == INVALID_HANDLE:
        raise OSError(f"CreateFileW failed, error {ctypes.get_last_error()}")
    return h


class DirectReader:
    """One handle plus one page-aligned buffer, reading past the page cache.

    Pass `handle` to borrow someone else's handle instead of opening one - that is
    Arm 1. The borrowed handle is NOT closed here; whoever opened it closes it.
    Buffer and byte counter stay private per instance either way. Sharing the
    counter would be worse than a race on the file pointer: ReadFile writes the
    byte count into it, so two threads reading it back would each report whichever
    value landed last, and the sweep's numerator would be fiction.
    """

    def __init__(self, path, block, handle=None):
        if block % SECTOR:
            raise ValueError(f"block {block} is not a multiple of {SECTOR}")
        self.block = block
        self._owns_handle = handle is None
        if handle is None:
            self.h = open_direct_handle(path)
        else:
            self.h = handle
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

    def read_probe(self, pos, length):
        """Arm 2's read: exact position, exact length, and it does NOT raise.

        read_at above rounds the position down and turns a failure into an
        exception, which is right for a sweep and wrong here - the whole question
        is what Windows DOES at the end of the file, so both outcomes have to come
        back as data. Returns (ok, bytes_reported, win32_error).
        """
        if length > self.block:
            raise ValueError(f"length {length} exceeds the reader's buffer {self.block}")
        if not _k32.SetFilePointerEx(self.h, pos, None, 0):
            raise OSError(f"SetFilePointerEx failed, error {ctypes.get_last_error()}")
        self._got.value = 0
        ok = _k32.ReadFile(self.h, self.buf, length, ctypes.byref(self._got), None)
        err = 0 if ok else ctypes.get_last_error()
        return bool(ok), self._got.value, err

    def close(self):
        if self.buf:
            _k32.VirtualFree(ctypes.c_void_p(self.buf), ctypes.c_size_t(0), MEM_RELEASE)
            self.buf = None
        if self.h:
            if self._owns_handle:
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


def random_reads(path, size, block, depth, seconds, direct=False, fail_thread=None,
                 shared_handle=False):
    """`depth` threads, one file handle each, random offsets, for `seconds`.

    shared_handle flips that to ONE handle for all threads - Arm 1. Everything
    else stays identical: same file, same offset sequence, same block size, same
    flag. Only the handle changes, so the difference in the number is the answer
    and nothing else.

    fail_thread is for the failing case only: that thread raises on purpose, so
    the guards below can be shown to fire. Never set it in a real measurement.
    """
    if shared_handle and not direct:
        # Checked here and not only in main(): a shared handle without the flag
        # would fall through to the buffered path and quietly measure the ordinary
        # sweep a second time, under Arm 1's name.
        raise ValueError("shared_handle requires direct=True (FILE_FLAG_NO_BUFFERING)")
    stop = time.perf_counter() + seconds
    counts = [0] * depth
    # One slot per thread. threading swallows exceptions into stderr and the
    # joiner learns nothing, so each worker has to hand its own failure back.
    errors = [None] * depth
    # Offsets are derived per thread from a fixed stride so runs are reproducible
    # without Math.random-style nondeterminism, and so no two threads walk in step.
    span = size - block

    if direct:
        # Opened here, before any thread starts, and closed after they all join.
        # Each worker still builds its own DirectReader around it, so buffer and
        # byte counter stay private - see the class docstring for why that is not
        # negotiable.
        shared_h = open_direct_handle(path) if shared_handle else None

        def worker_direct(idx):
            n = 0
            stride = 1_000_003 * (idx * 2 + 1)
            pos = (idx * 7_919_357) % span
            rd = DirectReader(path, block, handle=shared_h)
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
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            dt = time.perf_counter() - t0
        finally:
            # In a finally because a raising join would otherwise leak the handle
            # into every later depth level of the same sweep.
            if shared_h is not None:
                _k32.CloseHandle(shared_h)
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


def run_sweep(path, size, block, seconds, label, direct=False, fail_thread=None,
              shared_handle=False):
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
            mb, got, dt = random_reads(path, size, block, depth, seconds, direct,
                                       fail_thread, shared_handle)
        except SweepInvalid as exc:
            print(f"  {depth:5}   INVALID - {exc}")
            raise
        if base is None:
            base = mb
        ratio = mb / base
        rows.append((depth, mb, ratio))
        print(f"  {depth:5}   {human(mb)}   {ratio:8.2f}x")
    return rows[-1][2], rows


def eof_tail_probe(path, block):
    """Arm 2. Read the last sector-multiple block, whose end lies past the EOF.

    Not a corner case anyone can defer: none of the four model files on this drive
    ends on a sector boundary, so every direct-IO mechanism meets this on every
    file it touches.

    Under test is a PREDICTION, not a measurement: that Windows tolerates the read,
    returns TRUE, and reports exactly the bytes up to the logical EOF. It comes from
    Stack Overflow material that carries its own "depends on the driver stack"
    clause. Both outcomes are usable and they lead to different builds in stage 4 -
    so this reports what happened rather than asserting what should have.

    The control read comes FIRST and it is what makes the tail read mean anything:
    a read of the same length at the same alignment, but wholly inside the file,
    must return the full block. If a short count comes back there too, then a short
    count at the tail says nothing about EOF - it says the harness is short-reading.
    """
    size = os.path.getsize(path)
    rem = size % SECTOR
    print(f"=== Arm 2: read past EOF under FILE_FLAG_NO_BUFFERING ===")
    print(f"  file        {path}")
    print(f"  size        {size:,} bytes")
    print(f"  size % {SECTOR}  {rem}")
    if rem == 0:
        print()
        print("  VERDICT: NOT APPLICABLE - this file ends exactly on a sector")
        print("  boundary, so there is no tail to read past. Nothing was measured.")
        print("  Pick a file whose size is not a multiple of the sector size.")
        return 1

    last_aligned = (size // SECTOR) * SECTOR
    start = last_aligned - block + SECTOR
    if start < 0:
        print()
        print(f"  SETUP ERROR: block {block:,} B is larger than the file. A tail read")
        print("  needs a block that fits before the end.")
        return 2

    rd = DirectReader(path, block)
    try:
        control_start = start - block
        if control_start < 0:
            control_start = 0
        print()
        print("  control: same length, same alignment, wholly INSIDE the file")
        print(f"    offset    {control_start:,}")
        print(f"    length    {block:,}")
        ok_c, got_c, err_c = rd.read_probe(control_start, block)
        print(f"    ReadFile  {'TRUE' if ok_c else 'FALSE'}, reported {got_c:,} bytes"
              + ("" if ok_c else f", error {err_c}"))
        if not ok_c or got_c != block:
            print()
            print("  VERDICT: FAILED - the control did not return a full block. A short")
            print("  count at the tail would then be the harness, not the end of the")
            print("  file, and the answer this arm exists to give would be wrong.")
            return 1
        print("    control OK - a full block comes back when the read fits.")

        print()
        print("  tail: last sector-multiple block, end lies past the logical EOF")
        print(f"    offset    {start:,}")
        print(f"    length    {block:,}")
        print(f"    end       {start + block:,}   ({start + block - size:,} bytes past EOF)")
        print(f"    expected if the prediction holds: TRUE, {size - start:,} bytes")
        ok_t, got_t, err_t = rd.read_probe(start, block)
        print(f"    ReadFile  {'TRUE' if ok_t else 'FALSE'}, reported {got_t:,} bytes"
              + ("" if ok_t else f", error {err_t}"))
    finally:
        rd.close()

    print()
    print("=== verdict ===")
    if ok_t and got_t == size - start:
        print("  PREDICTION HOLDS - Windows tolerated the read past EOF and reported")
        print(f"  exactly the {got_t:,} bytes up to the logical end of the file.")
        print("  Stage 4 needs no separate edge-case path: read the aligned block and")
        print("  believe the returned count.")
        return 0
    if ok_t:
        print(f"  PREDICTION MISSED - the read succeeded but reported {got_t:,} bytes,")
        print(f"  not the {size - start:,} up to EOF. Stage 4 cannot derive the valid")
        print("  length from the return value and needs its own edge-case path.")
        return 0
    print(f"  PREDICTION DOES NOT HOLD - the read failed with error {err_t}.")
    print("  Stage 4 needs an explicit edge-case path for the last sector, and this")
    print("  is the point where k3_st_read_aligned cannot be copied 1:1 to Windows.")
    return 0


def query_sector_sizes(volume):
    """Arm 3. Ask the device, do not believe the constant.

    Deliberately has NO fallback to SECTOR. A query that silently returns 4096
    when the device did not answer is the exact failure shape this project keeps
    finding: a result that looks like success. If the device does not answer, this
    raises and the caller has nothing to quote.
    """
    h = _k32.CreateFileW(volume, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                         OPEN_EXISTING, 0, None)
    if h is None or h == INVALID_HANDLE:
        raise OSError(f"CreateFileW on {volume} failed, error {ctypes.get_last_error()}")
    try:
        q = STORAGE_PROPERTY_QUERY()
        q.PropertyId = STORAGE_ACCESS_ALIGNMENT_PROPERTY
        q.QueryType = PROPERTY_STANDARD_QUERY
        desc = STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR()
        returned = ctypes.c_uint32(0)
        ok = _k32.DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY,
                                  ctypes.byref(q), ctypes.sizeof(q),
                                  ctypes.byref(desc), ctypes.sizeof(desc),
                                  ctypes.byref(returned), None)
        if not ok:
            raise OSError("IOCTL_STORAGE_QUERY_PROPERTY failed on "
                          f"{volume}, error {ctypes.get_last_error()}")
        if returned.value < ctypes.sizeof(desc):
            raise OSError(f"descriptor truncated: {returned.value} of "
                          f"{ctypes.sizeof(desc)} bytes returned")
        return desc
    finally:
        _k32.CloseHandle(h)


def sector_query_probe(path):
    """Arm 3's report. Compares the answer against the constant this file uses."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        print(f"  SETUP ERROR: cannot derive a volume from {path}")
        return 2
    volume = f"\\\\.\\{drive}"
    print("=== Arm 3: sector sizes queried at runtime ===")
    print(f"  volume      {volume}   (derived from {path})")
    print(f"  constant    SECTOR = {SECTOR}   (what this file uses today)")
    print()
    try:
        desc = query_sector_sizes(volume)
    except OSError as exc:
        print(f"  VERDICT: FAILED - {exc}")
        print("  No number is reported. Falling back to the constant here would")
        print("  produce exactly the kind of result that looks like an answer.")
        return 1
    print(f"  BytesPerLogicalSector          {desc.BytesPerLogicalSector}"
          "    <- the alignment DUTY of FILE_FLAG_NO_BUFFERING")
    print(f"  BytesPerPhysicalSector         {desc.BytesPerPhysicalSector}"
          "   <- Microsoft's performance RECOMMENDATION")
    print(f"  BytesPerCacheLine              {desc.BytesPerCacheLine}")
    print(f"  BytesOffsetForCacheAlignment   {desc.BytesOffsetForCacheAlignment}")
    print(f"  BytesOffsetForSectorAlignment  {desc.BytesOffsetForSectorAlignment}")
    print()
    print("=== verdict ===")
    if desc.BytesPerPhysicalSector == SECTOR:
        print(f"  The constant {SECTOR} matches the physical sector size. It was right,")
        print("  and from here on it is an answer rather than luck.")
    else:
        print(f"  The constant {SECTOR} does NOT match the physical sector size "
              f"{desc.BytesPerPhysicalSector}.")
        print("  Every read aligned to the constant costs read-modify-write on the")
        print("  controller. Stage 4 must take the alignment from this query.")
    if desc.BytesPerLogicalSector != desc.BytesPerPhysicalSector:
        print(f"  Logical {desc.BytesPerLogicalSector} against physical "
              f"{desc.BytesPerPhysicalSector}: this drive is 512e. Aligning to the")
        print("  logical size is PERMITTED and slower; the physical size is the one to use.")
    return 0


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
    shared_handle = False
    eof_tail = False
    sector_query = False
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
        if a == "--shared-handle":
            shared_handle = True
        if a == "--eof-tail":
            eof_tail = True
        if a == "--sector-query":
            sector_query = True

    # One arm at a time. Two arms in one run leaves nobody able to say which one
    # produced the number - and each of the three answers a different question, so
    # there is no reading of a combined run that is worth having.
    arms = [n for n, on in (("--shared-handle", shared_handle),
                            ("--eof-tail", eof_tail),
                            ("--sector-query", sector_query)) if on]
    if len(arms) > 1:
        print(f"SETUP ERROR: the arms run one at a time; got {', '.join(arms)}")
        return 2
    if shared_handle and not direct:
        print("SETUP ERROR: --shared-handle needs --direct. The question is whether")
        print("Windows serialises unbuffered reads on one synchronous handle; without")
        print("the flag the page cache answers instead of the drive.")
        return 2

    needs_windows = direct or eof_tail or sector_query
    needs_alignment = direct or eof_tail
    if needs_windows and sys.platform != "win32":
        print("SETUP ERROR: --direct, --eof-tail and --sector-query are Windows-only")
        return 2
    if needs_alignment and (block_kb * 1024) % SECTOR:
        print(f"SETUP ERROR: unbuffered reads need a block that is a multiple of "
              f"{SECTOR} B; {block_kb} KB is not")
        return 2
    if not os.path.exists(path):
        print(f"SETUP ERROR: no such file: {path}")
        return 2
    if neg_path and not os.path.exists(neg_path):
        print(f"SETUP ERROR: no such negative-control file: {neg_path}")
        return 2

    block = block_kb * 1024
    size = os.path.getsize(path)

    # Arms 2 and 3 are their own runs, not variants of the sweep. They return
    # before the sequential control because that control reads 4 GB to prove the
    # harness can show speed - and neither of these arms reports a speed.
    if sector_query:
        return sector_query_probe(path)
    if eof_tail:
        return eof_tail_probe(path, block)
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
    handles = "ONE SHARED handle for all threads (Arm 1)" if shared_handle \
              else "one handle per thread"
    print(f"  mode        {mode}")
    print(f"  handles     {handles}")
    if shared_handle:
        print("              buffer and byte counter stay per thread; only the handle")
        print("              is shared, so the numerator of every rate below is sound")
        print("  reference   per-thread form, 2026-08-02: 4812.2 MB/s at depth 1,")
        print("              10523.9 MB/s at depth 8")
    print()
    if fail_thread is not None:
        print(f"=== SELFTEST: thread {fail_thread} will raise on purpose ===")
        print("  Required outcome: a named invalidity and exit 1. NOT 0 MB/s, NOT a")
        print("  ZeroDivisionError, NOT a plausible-looking slow number.")
        print()
        try:
            run_sweep(path, size, block, seconds, "selftest", direct, fail_thread,
                      shared_handle)
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
        disk_label = "the real file, on disk, ONE SHARED handle" if shared_handle \
                     else "the real file, on disk"
        disk_ratio, disk_rows = run_sweep(path, size, block, seconds, disk_label,
                                          direct, None, shared_handle)
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
    if shared_handle:
        neg_label += ", ONE SHARED handle"
    # The control runs in the SAME handle form as the sweep it vouches for. Run it
    # per-thread while the sweep is shared and the two are no longer comparable.
    neg_ratio, neg_rows = run_sweep(neg_path, neg_size, block, seconds, neg_label,
                                    direct, None, shared_handle)
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
