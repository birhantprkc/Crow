// Stage 5: read throughput measured through llama.cpp's own file path.
//
// Every read-throughput figure this project has so far came from a Python program
// asking the drive (probe-queue-depth.py). ISSUE-30 calls that "still synthetic on the
// numerator side". This tool closes that gap: it drives llama_file itself over a real
// model file and reports what IT reaches, not what the drive can do.
//
// It lives in Crow/tools/ rather than in llama.cpp/tests/ so the foreign clone keeps a
// clean CMake tree. llama_file is internal and llama.dll does not export it, so
// llama-mmap.cpp is compiled straight into this binary - the same trick
// tests/CMakeLists.txt uses. llama-impl.cpp comes along because llama-mmap.cpp needs
// two symbols from it: llama_log_internal() and format(). A dummy logger alone is not
// enough; format() would still be an unresolved external.
//
// WHY read_raw_at AND NOT read_raw
// Under direct I/O every read_raw() call goes through read_aligned_chunk, which does an
// _aligned_malloc plus a memcpy of the whole block on EVERY call - aligned or not. Over
// a 155 GB file that is ~11,900 allocations and 155 GB of extra copying, and the number
// that came out would be the bounce buffer's, not the read path's. read_raw_at is the
// raw form: the header states the caller owns the alignment of offset, length and
// buffer address, and this tool provides all three.
//
// WHY THE RATE IS bytes/s/1e6
// probe-queue-depth.py computes every rate as bytes / seconds / 1e6 (decimal MB), at
// :287, :400 and :438. The yardstick figures - 4812.2 MB/s at depth 1 and 10592.0 MB/s
// at depth 8 - are in that unit. Dividing by 1024*1024 instead would report the same
// drive roughly 4.8 % slower and make a hit look like a miss.
//
// THE TWO ARMS
//   default     unbuffered, use_direct_io = true. The measurement.
//   --buffered  buffered, use_direct_io = false. The control that must FAIL to be slow:
//               on a warm file it has to come out clearly FASTER, because it is reading
//               RAM. If it does not, FILE_FLAG_NO_BUFFERING never took hold and the
//               default arm's number is page cache, not disk. Two figures have already
//               been withdrawn in this project for exactly that missing control.
//               Point the control at a warm file that fits in RAM, not at the 155 GB
//               model - 63.4 GB of RAM cannot hold it and the run would measure disk
//               through a cache that keeps missing.
//
// Both arms read through the SAME primitive at the SAME offsets with the SAME block
// size. Only the buffering differs, which is the only way the comparison says anything.
//
// THE THIRD ARM: --threads N [--shared]
//   Time-boxed random reads, one std::thread per worker, each passing its own worker_id
//   into read_raw_at so that each gets a private handle out of llama_file's pool.
//
//   Why it exists: measured 2026-08-03 with probe-queue-depth.py, a private handle per
//   thread reaches 2.22x the single-thread rate at queue depth 8 while ONE SHARED handle
//   reaches 1.01x - through an OVERLAPPED offset just as much as through
//   SetFilePointerEx. Windows serialises on the file object. But that figure is a Python
//   program asking the drive; ISSUE-30's "still synthetic on the numerator side" applies
//   to it exactly as it applied to the block-size figure before stage 5. This arm asks
//   the same question through llama_file.
//
//   --shared is the control and it lives INSIDE this binary on purpose: same threads,
//   same offsets, same block size, worker_id forced to -1 so every worker lands on the
//   shared handle. If the shared arm does NOT collapse to about 1.0x, then the pool is
//   not what produces the difference and the 2.22x means something other than assumed.
//   A separate build would answer a different question - whether the feature exists -
//   and build-bench.ps1 already covers that by putting the b10223 header first.
//
//   Offsets follow probe-queue-depth.py:365-366 to the digit - span = size - block, a
//   large odd stride per thread - so both tools walk the same file in the same order.
//   Giving each thread a contiguous chunk instead would measure N sequential streams,
//   which is neither the Python baseline nor the sequential arm above, and a ratio
//   across two different load shapes is not a ratio.
//
// THE FOURTH ARM: --h2d
//   Host-to-device copy rate, pinned against pageable, at the same block size the other
//   arms read with. It answers one question and no other: what would a second cache
//   level in host RAM cost per expert, against fetching that expert off the SSD.
//
//   Why it is needed even though the RAM placement is already measured: -ncmoe 999 moves
//   the WHOLE cache into host memory and lets the GPU compute against it there. That is a
//   different operation from holding a copy in RAM and blitting it into a VRAM slot on a
//   miss. The measured +7.9 to +16.2 % decode surcharge (ISSUE-30, 2026-08-04) describes
//   the first and says nothing about the second, so the tier question is open on the
//   numbers even though the placement question is closed.
//
//   WHY cudaMemcpy AND NOT cudaMemcpyAsync
//   The decode path has no prefetch - the router of layer N+1 reads what N produced, so
//   there is nothing to overlap a transfer with. A tier-2 hit would sit on the critical
//   path and the compute thread would wait for it. The synchronous call is the honest
//   model of that; an async copy on a stream would measure a pipeline this system does
//   not have.
//
//   WHY AN ARENA AND NOT ONE BUFFER
//   Copying the same 2.5 MiB buffer in a loop measures the CPU's last-level cache, not
//   DRAM. A real tier-2 would hold tens of GB and every hit would come in cold. So the
//   source is an arena far larger than any L3 and each copy starts at a different offset
//   in it, walked with the same odd stride the read arm uses. This is the same failure
//   shape that made a warm 4 GB control file read FASTER than the drive's sequential
//   ceiling on 2026-08-02, and it cost a run then.
//
//   THE CONTROL IS A PROPERTY, NOT A THRESHOLD
//   pinned and pageable must be two different things, and the check for that asks the
//   driver rather than the clock: cudaHostGetFlags succeeds on host-registered memory and
//   returns cudaErrorInvalidValue on ordinary memory. Both are asserted. A threshold on
//   the measured ratio would be a criterion invented after seeing the number, and it
//   could not tell "the flag did nothing" from "the flag did nothing measurable here".
//
//   The second control is byte identity: a pattern is written into the source block,
//   copied up, copied back into a third buffer and compared. Without it a copy that moved
//   fewer bytes than asked - or none - would report as speed. This project has already
//   paid for that exact shape once, with a HANDLE truncated to 32 bits that made every
//   ReadFile return 0 bytes and look successful.

#include "llama-mmap.h"

#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <windows.h>
#include <malloc.h>

// 12.75 MiB - one expert of one layer in DeepSeek-V4-Flash-MXFP4, from the tensor table.
// llama.cpp today faults 65.8 KB at a time, which is what makes this the request size the
// plan is about. 13369344 = 12.75 * 1024 * 1024, a multiple of 4096, so it is
// sector-aligned on this drive. That is checked at runtime rather than assumed.
//
// IT IS THE DEFAULT AND NOT THE OPERATING POINT, corrected 2026-08-09. The shipped model
// is UD-IQ3_XXS, where one expert is 8.188 MiB across three tensors and the streamer
// moves ONE TENSOR per work item: ffn_gate_exps and ffn_up_exps are 2,686,976 B each,
// ffn_down_exps is 3,211,264 B (read from blk.0 of shard 2 with tools/gguf_header.py).
// Both divide by 4096. Every figure this tool has published so far stands on 12.75 MiB
// blocks, so --block-bytes exists to ask the same questions at the size the product
// actually uses - and the default is left alone so those figures keep their shape.
static const size_t BLOCK_SIZE = 13369344;

struct aligned_buffer {
    void * p = nullptr;

    aligned_buffer(size_t size, size_t align) {
        p = _aligned_malloc(size, align);
    }
    ~aligned_buffer() {
        if (p) {
            _aligned_free(p);
        }
    }
    aligned_buffer(const aligned_buffer &) = delete;
    aligned_buffer & operator=(const aligned_buffer &) = delete;
};

struct worker_result {
    size_t      bytes  = 0;
    size_t      blocks = 0;
    bool        failed = false;
    std::string error;
};

// One worker of the parallel arm. Buffer, counters and error slot are its own; the
// llama_file and the file behind it are shared, and that sharing is the thing under
// test rather than an accident.
//
// The error comes back through `out` instead of escaping: std::thread turns an escaping
// exception into std::terminate, which would end the run with no output at all and look
// like a crash in the read path rather than a failure in one worker.
static void bench_worker(llama_file & file, worker_result & out,
                         int idx, int worker_id,
                         size_t span, size_t alignment, size_t block, LONGLONG stop_ticks) {
    // probe-queue-depth.py:365-366, digit for digit. A large odd stride per thread walks
    // the whole file without repeating soon and keeps no two threads in step.
    const size_t stride = size_t(1000003) * (size_t(idx) * 2 + 1);
    size_t       pos    = (size_t(idx) * 7919357) % span;
    const size_t align  = alignment > 1 ? alignment : 1;

    // One aligned buffer PER worker. The sequential arm's single buffer would be a data
    // race here, and a race inside the instrument is the one defect that cannot show up
    // in the instrument's own output.
    aligned_buffer buf(block, alignment > 1 ? alignment : 4096);
    if (buf.p == nullptr) {
        out.failed = true;
        out.error  = "_aligned_malloc failed";
        return;
    }
    // Touched once outside the timed window, same reason as the sequential arm.
    std::memset(buf.p, 0, block);

    LARGE_INTEGER now;
    for (;;) {
        QueryPerformanceCounter(&now);
        if (now.QuadPart >= stop_ticks) {
            break;
        }
        // Round DOWN to a sector. Direct I/O refuses an unaligned offset outright with
        // ERROR_INVALID_PARAMETER - measured 2026-08-03 - and this is the same rounding
        // probe-queue-depth.py:212 does before every read.
        const size_t at = pos - (pos % align);
        try {
            const size_t got = file.read_raw_at(buf.p, block, at, worker_id);
            if (got != block) {
                // span keeps every read wholly inside the file, so a short count here is
                // not the tail. It is a defect, and adding it to the total would average
                // it away into a number that still looks like a measurement.
                out.failed = true;
                out.error  = "short read of " + std::to_string(got) +
                             " bytes at offset " + std::to_string(at);
                return;
            }
            out.bytes += got;
            out.blocks++;
        } catch (const std::exception & e) {
            out.failed = true;
            out.error  = e.what();
            return;
        }
        pos = (pos + stride) % span;
    }
}

// Every CUDA call is checked. An unchecked failure leaves the destination untouched and
// the loop then times an operation that did not happen - the fastest possible transfer
// rate, and a false one.
#define CU_OK(call, what)                                                              \
    do {                                                                               \
        const cudaError_t _e = (call);                                                 \
        if (_e != cudaSuccess) {                                                       \
            fprintf(stderr, "\nABORT: %s failed: %s\n", (what), cudaGetErrorString(_e));\
            return 1;                                                                  \
        }                                                                              \
    } while (0)

// One timed sweep of host-to-device copies out of `src_arena`, each one starting at a
// different offset. Returns the rate in bytes/s/1e6, or a negative value on failure.
static double h2d_sweep(void * dst, const unsigned char * src_arena, size_t arena,
                        size_t block, double seconds, size_t * out_copies) {
    // Same stride construction as bench_worker, so the source is walked the way the file
    // is walked and neither arm gets a friendlier access pattern than the other.
    const size_t span   = arena - block;
    const size_t stride = size_t(1000003);
    size_t       pos    = 0;
    size_t       copies = 0;

    LARGE_INTEGER freq, t0, t1, now;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);
    const LONGLONG stop_ticks = t0.QuadPart + LONGLONG(seconds * double(freq.QuadPart));

    for (;;) {
        QueryPerformanceCounter(&now);
        if (now.QuadPart >= stop_ticks) {
            break;
        }
        const size_t at = (pos % span) & ~size_t(4095);
        const cudaError_t e = cudaMemcpy(dst, src_arena + at, block, cudaMemcpyHostToDevice);
        if (e != cudaSuccess) {
            fprintf(stderr, "\nABORT: cudaMemcpy H2D failed: %s\n", cudaGetErrorString(e));
            return -1.0;
        }
        copies++;
        pos += stride;
    }
    // Nothing may still be in flight when the clock stops, or the last copies land
    // outside the window and the rate comes out high by however much is queued.
    if (cudaDeviceSynchronize() != cudaSuccess) {
        fprintf(stderr, "\nABORT: cudaDeviceSynchronize failed after the sweep\n");
        return -1.0;
    }
    QueryPerformanceCounter(&t1);

    const double dt = double(t1.QuadPart - t0.QuadPart) / double(freq.QuadPart);
    if (dt <= 0.0 || copies == 0) {
        return -1.0;
    }
    *out_copies = copies;
    return double(copies) * double(block) / dt / 1e6;
}

// The fourth arm. See the header block for why it exists and what it does not answer.
static int run_h2d_arm(size_t block, double seconds, size_t arena) {
    int devices = 0;
    CU_OK(cudaGetDeviceCount(&devices), "cudaGetDeviceCount");
    if (devices < 1) {
        fprintf(stderr, "\nABORT: no CUDA device. No figure.\n");
        return 1;
    }
    CU_OK(cudaSetDevice(0), "cudaSetDevice");

    cudaDeviceProp prop;
    CU_OK(cudaGetDeviceProperties(&prop, 0), "cudaGetDeviceProperties");

    // The PCIe generation and width are NOT read here. There is no cudaDevAttr for the
    // negotiated link, and the nearest-looking ones describe the VRAM bus instead - a
    // number that would print in the right place and mean the wrong thing. nvidia-smi
    // reports the link; this tool reports the rate.
    printf("arm         H2D (host to device copy)\n");
    printf("device      %s\n", prop.name);
    printf("block       %zu bytes (%.3f MiB)\n", block, double(block) / (1024.0 * 1024.0));
    printf("arena       %zu bytes (%.0f MiB)\n", arena, double(arena) / (1024.0 * 1024.0));
    printf("seconds     %.1f per sweep\n", seconds);

    if (arena <= block) {
        fprintf(stderr, "\nABORT: arena is not larger than one block. No figure.\n");
        return 1;
    }

    void * dst = nullptr;
    CU_OK(cudaMalloc(&dst, block), "cudaMalloc");

    // Pageable source: ordinary aligned host memory, the shape a staging buffer has today.
    aligned_buffer pageable(arena, 4096);
    if (pageable.p == nullptr) {
        fprintf(stderr, "\nABORT: _aligned_malloc of %zu bytes failed. No figure.\n", arena);
        cudaFree(dst);
        return 1;
    }
    // Pinned source: what a tier-2 cache would have to be to reach DMA rates at all.
    void * pinned = nullptr;
    const cudaError_t pe = cudaHostAlloc(&pinned, arena, cudaHostAllocDefault);
    if (pe != cudaSuccess) {
        fprintf(stderr, "\nABORT: cudaHostAlloc of %zu bytes failed: %s\n",
                arena, cudaGetErrorString(pe));
        fprintf(stderr, "       This is itself a finding - the tier needs pinned memory -\n");
        fprintf(stderr, "       but it is not a rate. No figure.\n");
        cudaFree(dst);
        return 1;
    }

    // CONTROL 1 - the two sources must be two different kinds of memory, asked of the
    // driver and not of the clock.
    unsigned int flags = 0;
    if (cudaHostGetFlags(&flags, pinned) != cudaSuccess) {
        fprintf(stderr, "\nABORT: cudaHostGetFlags failed on the PINNED buffer, so it is\n");
        fprintf(stderr, "       not page-locked and both arms would be the same memory.\n");
        cudaFreeHost(pinned); cudaFree(dst);
        return 1;
    }
    const cudaError_t should_fail = cudaHostGetFlags(&flags, pageable.p);
    if (should_fail == cudaSuccess) {
        fprintf(stderr, "\nABORT: cudaHostGetFlags SUCCEEDED on the pageable buffer, so it\n");
        fprintf(stderr, "       is registered after all and the comparison is void.\n");
        cudaFreeHost(pinned); cudaFree(dst);
        return 1;
    }
    cudaGetLastError();   // clear the error the deliberate failure just set
    printf("control 1   pinned is page-locked, pageable is not  PASS\n");

    // CONTROL 2 - byte identity. A copy that moves nothing is the fastest copy there is.
    {
        unsigned char * src = (unsigned char *) pageable.p;
        for (size_t i = 0; i < block; i++) {
            src[i] = (unsigned char) ((i * 31u + 7u) & 0xFF);
        }
        aligned_buffer back(block, 4096);
        if (back.p == nullptr) {
            fprintf(stderr, "\nABORT: could not allocate the readback buffer. No figure.\n");
            cudaFreeHost(pinned); cudaFree(dst);
            return 1;
        }
        std::memset(back.p, 0, block);
        CU_OK(cudaMemcpy(dst, src, block, cudaMemcpyHostToDevice), "control cudaMemcpy H2D");
        CU_OK(cudaMemcpy(back.p, dst, block, cudaMemcpyDeviceToHost), "control cudaMemcpy D2H");
        if (std::memcmp(src, back.p, block) != 0) {
            fprintf(stderr, "\nABORT: the round trip did not preserve the block. Whatever the\n");
            fprintf(stderr, "       loop below would time, it is not this transfer. No figure.\n");
            cudaFreeHost(pinned); cudaFree(dst);
            return 1;
        }
        printf("control 2   %zu bytes survive H2D then D2H unchanged  PASS\n", block);
    }

    // Fill both arenas outside the timed windows so first-touch faults do not land in a
    // rate, and so neither arm is timed against untouched pages the other already has.
    std::memset(pageable.p, 0xA5, arena);
    std::memset(pinned,     0xA5, arena);

    size_t copies_pageable = 0, copies_pinned = 0;
    const double rate_pageable = h2d_sweep(dst, (const unsigned char *) pageable.p, arena,
                                           block, seconds, &copies_pageable);
    const double rate_pinned   = h2d_sweep(dst, (const unsigned char *) pinned, arena,
                                           block, seconds, &copies_pinned);

    cudaFreeHost(pinned);
    cudaFree(dst);

    if (rate_pageable < 0.0 || rate_pinned < 0.0) {
        return 1;
    }

    printf("\ncopies      %zu pageable, %zu pinned\n", copies_pageable, copies_pinned);
    printf("\nRESULT      pageable  %.1f MB/s   (bytes/s/1e6)\n", rate_pageable);
    printf("RESULT      pinned    %.1f MB/s   (bytes/s/1e6)\n", rate_pinned);
    printf("RESULT      ratio     %.3fx  pinned over pageable\n", rate_pinned / rate_pageable);
    return 0;
}

int main(int argc, char ** argv) {
    // Unbuffered, and not for cosmetics: when the stage 4 test crashed, buffered stdout
    // was never flushed and the failure looked like it happened before main() started.
    setvbuf(stdout, NULL, _IONBF, 0);

    bool buffered = false;
    const char * path = nullptr;

    // 0 keeps the sequential arm exactly as it was - that arm carries stage 5's figure
    // and must not change shape underneath a number already published on ISSUE-30.
    int    threads = 0;
    bool   shared  = false;
    double seconds = 4.0;   // the dwell probe-queue-depth.py uses per depth level

    bool   h2d      = false;
    size_t block    = BLOCK_SIZE;
    // 1 GiB. It has to clear the largest last-level cache in the machine by a wide margin,
    // or the copy loop reports SRAM. 24 threads of consumer Zen is well under 128 MiB of
    // L3, so a gigabyte is not a close call - which is the point of picking it here rather
    // than tuning it later against a number somebody already saw.
    size_t arena_mb = 1024;

    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        if (a == "--buffered") {
            buffered = true;
        } else if (a == "--threads" && i + 1 < argc) {
            threads = atoi(argv[++i]);
        } else if (a == "--shared") {
            shared = true;
        } else if (a == "--seconds" && i + 1 < argc) {
            seconds = atof(argv[++i]);
        } else if (a == "--h2d") {
            h2d = true;
        } else if (a == "--block-bytes" && i + 1 < argc) {
            block = (size_t) _strtoui64(argv[++i], nullptr, 10);
        } else if (a == "--arena-mb" && i + 1 < argc) {
            arena_mb = (size_t) _strtoui64(argv[++i], nullptr, 10);
        } else if (path == nullptr) {
            path = argv[i];
        } else {
            fprintf(stderr, "unexpected argument: %s\n", argv[i]);
            return 2;
        }
    }

    if (path == nullptr && !h2d) {
        fprintf(stderr, "usage: bench-loader.exe <file> [--buffered]\n");
        fprintf(stderr, "                              [--threads N [--shared]] [--seconds S]\n");
        fprintf(stderr, "                              [--block-bytes N]\n");
        fprintf(stderr, "       bench-loader.exe --h2d [--block-bytes N] [--arena-mb N] [--seconds S]\n");
        fprintf(stderr, "  default       unbuffered direct I/O, one sequential pass - the measurement\n");
        fprintf(stderr, "  --buffered    buffered - the control, must be FASTER on a warm file\n");
        fprintf(stderr, "  --threads N   N workers, random offsets, time-boxed; each gets its own\n");
        fprintf(stderr, "                handle out of the pool\n");
        fprintf(stderr, "  --shared      the control for --threads: same workers, same offsets,\n");
        fprintf(stderr, "                every one of them forced onto the SHARED handle\n");
        fprintf(stderr, "  --block-bytes request size, default 13369344 (12.75 MiB). The operating\n");
        fprintf(stderr, "                point moves 2686976 or 3211264 per work item\n");
        fprintf(stderr, "  --h2d         host-to-device copy rate, pinned against pageable. Takes no\n");
        fprintf(stderr, "                file: it measures the link, not the drive\n");
        return 2;
    }

    // The arms answer different questions and share no output line. Running two at once
    // would print one verdict for two measurements, and nobody could say afterwards which
    // one produced it - the rule probe-queue-depth.py already enforces for its four.
    if (h2d && (path != nullptr || threads > 0 || buffered)) {
        fprintf(stderr, "SETUP ERROR: --h2d is its own arm. It takes no file, no --threads\n");
        fprintf(stderr, "             and no --buffered - it never touches the drive.\n");
        return 2;
    }
    if (block == 0 || (block % 4096) != 0) {
        fprintf(stderr, "SETUP ERROR: --block-bytes must be a positive multiple of 4096.\n");
        fprintf(stderr, "             Direct I/O rejects an unaligned length outright, and the\n");
        fprintf(stderr, "             two arms have to move the same bytes to be comparable.\n");
        return 2;
    }

    if (h2d) {
        if (seconds <= 0.0) {
            fprintf(stderr, "SETUP ERROR: --seconds must be positive\n");
            return 2;
        }
        if (arena_mb == 0) {
            fprintf(stderr, "SETUP ERROR: --arena-mb must be positive\n");
            return 2;
        }
        return run_h2d_arm(block, seconds, arena_mb * 1024 * 1024);
    }

    if (shared && threads == 0) {
        fprintf(stderr, "SETUP ERROR: --shared needs --threads. It answers what the private\n");
        fprintf(stderr, "             handles buy, and with one worker there is nothing to share.\n");
        return 2;
    }
    if (threads < 0) {
        fprintf(stderr, "SETUP ERROR: --threads must be positive\n");
        return 2;
    }
    if (threads > 0 && seconds <= 0.0) {
        fprintf(stderr, "SETUP ERROR: --seconds must be positive\n");
        return 2;
    }

    printf("arm         %s\n", buffered ? "BUFFERED (control)" : "DIRECT I/O (measurement)");
    printf("file        %s\n", path);
    printf("block       %zu bytes (%.3f MiB)%s\n", block,
           double(block) / (1024.0 * 1024.0),
           block == BLOCK_SIZE ? "  (default)" : "  (--block-bytes)");

    try {
        llama_file file(path, "rb", !buffered);

        const size_t file_size = file.size();
        const size_t alignment = file.read_alignment();
        const bool   direct    = file.has_direct_io();

        printf("size        %zu bytes\n", file_size);
        printf("alignment   %zu\n", alignment);
        printf("direct_io   %s\n", direct ? "true" : "false");

        // Each arm has to actually BE its arm. A direct run that quietly fell back to
        // buffered would report the page cache as if it were the drive, and a control
        // that came up unbuffered would not be a control at all.
        if (!buffered && !direct) {
            fprintf(stderr, "\nABORT: direct I/O was requested and is not in effect.\n");
            fprintf(stderr, "       The constructor fell back to buffered reads, so this\n");
            fprintf(stderr, "       run would measure the page cache. No figure is printed.\n");
            return 1;
        }
        if (buffered && direct) {
            fprintf(stderr, "\nABORT: the control arm came up with direct I/O in effect.\n");
            fprintf(stderr, "       It would not be a control. No figure is printed.\n");
            return 1;
        }

        // Direct I/O demands offset, length and buffer address all be sector multiples.
        // The block size is fixed, so the only thing that can go wrong is a device whose
        // sector size does not divide it - which is a reason to stop, not to round.
        const size_t buf_align = (alignment > 1) ? alignment : 4096;
        if (direct && (block % alignment) != 0) {
            fprintf(stderr, "\nABORT: block size %zu is not a multiple of the device's %zu.\n",
                    block, alignment);
            return 1;
        }

        LARGE_INTEGER freq, t0, t1;
        QueryPerformanceFrequency(&freq);

        // ---------------------------------------------------------------------------
        // The parallel arm. Returns from here; the sequential arm below is untouched.
        // ---------------------------------------------------------------------------
        if (threads > 0) {
            const size_t pool = file.direct_io_handles();
            printf("threads     %d\n", threads);
            printf("handles     %zu private%s\n", pool,
                   shared ? "  (IGNORED - every worker forced onto the shared handle)"
                          : "  (one per worker)");
            printf("seconds     %.1f\n", seconds);

            // Legal but ruinous. Surplus workers fall back to the shared handle, so the
            // figure would be part pool and part serialisation and would belong to
            // neither arm - the shape of result this project keeps having to withdraw.
            if (!shared && pool > 0 && size_t(threads) > pool) {
                fprintf(stderr, "\nABORT: %d workers against %zu private handles. The surplus\n",
                        threads, pool);
                fprintf(stderr, "       would read through the shared handle and the figure would\n");
                fprintf(stderr, "       mix both arms. No figure is printed.\n");
                return 1;
            }
            if (file_size <= block) {
                fprintf(stderr, "\nABORT: file is not larger than one block. No figure.\n");
                return 1;
            }
            // span, exactly as the Python sweep defines it: every read lands wholly
            // inside the file, so no short count can enter the numerator.
            const size_t span = file_size - block;

            std::vector<worker_result> results((size_t) threads);
            std::vector<std::thread>   ts;
            ts.reserve((size_t) threads);

            QueryPerformanceCounter(&t0);
            const LONGLONG stop_ticks = t0.QuadPart + LONGLONG(seconds * double(freq.QuadPart));

            for (int i = 0; i < threads; i++) {
                // The single difference between the two arms, and it is this one value.
                // Same threads, same offsets, same block size, same primitive.
                const int wid = shared ? -1 : i;
                ts.emplace_back(bench_worker, std::ref(file),
                                std::ref(results[(size_t) i]),
                                i, wid, span, alignment, block, stop_ticks);
            }
            for (std::thread & t : ts) {
                t.join();
            }
            QueryPerformanceCounter(&t1);

            // The stage 2 guard, carried down from probe-queue-depth.py. A worker that
            // died, or one that finished without reading a byte, makes the run INVALID -
            // not slow. Dividing first is what once turned a dead reader into a
            // plausible-looking number and, at one worker, into a division by zero.
            for (int i = 0; i < threads; i++) {
                const worker_result & r = results[(size_t) i];
                if (r.failed) {
                    fprintf(stderr, "\nABORT: worker %d failed: %s\n", i, r.error.c_str());
                    fprintf(stderr, "       The run is invalid, not slow. No figure.\n");
                    return 1;
                }
                if (r.bytes == 0) {
                    fprintf(stderr, "\nABORT: worker %d read 0 bytes - the time budget expired\n", i);
                    fprintf(stderr, "       before it read anything. Raise --seconds. No figure.\n");
                    return 1;
                }
            }

            size_t total_bytes  = 0;
            size_t total_blocks = 0;
            for (const worker_result & r : results) {
                total_bytes  += r.bytes;
                total_blocks += r.blocks;
            }

            const double dt = double(t1.QuadPart - t0.QuadPart) / double(freq.QuadPart);
            if (dt <= 0.0) {
                fprintf(stderr, "\nABORT: elapsed time is not positive. No figure.\n");
                return 1;
            }
            const double mb_s = double(total_bytes) / dt / 1e6;

            printf("blocks      %zu\n", total_blocks);
            printf("read        %zu bytes\n", total_bytes);
            printf("elapsed     %.3f s\n", dt);
            printf("\nRESULT      %.1f MB/s   (%d threads, %s, bytes/s/1e6, %.2f GB)\n",
                   mb_s, threads,
                   shared ? "SHARED handle" : "handle per worker",
                   double(total_bytes) / 1e9);
            return 0;
        }

        aligned_buffer buf(block, buf_align);
        if (buf.p == nullptr) {
            fprintf(stderr, "\nABORT: _aligned_malloc of %zu bytes failed\n", block);
            return 1;
        }
        // Touch the pages once, outside the timed window, so first-touch faults do not
        // land in the measurement.
        std::memset(buf.p, 0, block);

        size_t offset = 0;
        size_t total  = 0;
        size_t blocks = 0;
        bool   short_before_eof = false;

        // Nothing is written to stdout between t0 and t1. A progress dot per block would
        // put console latency inside the number.
        QueryPerformanceCounter(&t0);
        while (offset < file_size) {
            // The full aligned block is requested even at the tail. Under direct I/O an
            // aligned request necessarily overshoots a file whose size is not a sector
            // multiple - measured on all four model files here, ReadFile returns TRUE
            // and reports exactly the bytes up to the logical EOF. Clamping to the
            // unaligned remainder instead would hand the kernel a length it rejects.
            const size_t got = file.read_raw_at(buf.p, block, offset);
            if (got == 0) {
                break;
            }
            if (got < block && offset + got < file_size) {
                // A short count anywhere but at the end leaves the next offset unaligned
                // and the run half-done. Stop, and print no figure.
                short_before_eof = true;
                break;
            }
            total  += got;
            offset += got;
            blocks++;
        }
        QueryPerformanceCounter(&t1);

        printf("blocks      %zu\n", blocks);
        printf("read        %zu bytes\n", total);

        // The result line is written only if the pass was complete. A partial read has
        // no denominator, and a rate computed from one looks exactly like a real figure.
        if (short_before_eof) {
            fprintf(stderr, "\nABORT: short read before end of file at offset %zu. No figure.\n", offset);
            return 1;
        }
        if (total != file_size) {
            fprintf(stderr, "\nABORT: read %zu bytes of %zu. No figure.\n", total, file_size);
            return 1;
        }

        const double dt = double(t1.QuadPart - t0.QuadPart) / double(freq.QuadPart);
        if (dt <= 0.0) {
            fprintf(stderr, "\nABORT: elapsed time is not positive. No figure.\n");
            return 1;
        }

        // bytes / s / 1e6 - decimal MB, the unit probe-queue-depth.py reports in.
        const double mb_s = double(total) / dt / 1e6;

        printf("elapsed     %.3f s\n", dt);
        printf("\nRESULT      %.1f MB/s   (%s, bytes/s/1e6, %.2f GB)\n",
               mb_s, buffered ? "buffered control" : "direct I/O",
               double(total) / 1e9);
        return 0;

    } catch (const std::exception & e) {
        // Catching is the measurement, not politeness. An uncaught C++ exception ends in
        // MSVC's abort(), which raises __fastfail(FAST_FAIL_FATAL_APP_EXIT) and is
        // reported as 0xC0000409 - the same code a stack buffer overrun produces. That
        // confusion cost this project two attempts in stage 4.
        fprintf(stderr, "\nEXCEPTION: %s\n", e.what());
        return 1;
    }
}
