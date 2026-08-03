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

#include "llama-mmap.h"

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

// 12.75 MiB - the bytes of one expert in one layer, from the tensor table. This is the
// request size the whole plan is about; llama.cpp today faults 65.8 KB at a time.
// 13369344 = 12.75 * 1024 * 1024, and a multiple of 4096, so it is sector-aligned on
// this drive. That is checked at runtime rather than assumed.
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
                         size_t span, size_t alignment, LONGLONG stop_ticks) {
    // probe-queue-depth.py:365-366, digit for digit. A large odd stride per thread walks
    // the whole file without repeating soon and keeps no two threads in step.
    const size_t stride = size_t(1000003) * (size_t(idx) * 2 + 1);
    size_t       pos    = (size_t(idx) * 7919357) % span;
    const size_t align  = alignment > 1 ? alignment : 1;

    // One aligned buffer PER worker. The sequential arm's single buffer would be a data
    // race here, and a race inside the instrument is the one defect that cannot show up
    // in the instrument's own output.
    aligned_buffer buf(BLOCK_SIZE, alignment > 1 ? alignment : 4096);
    if (buf.p == nullptr) {
        out.failed = true;
        out.error  = "_aligned_malloc failed";
        return;
    }
    // Touched once outside the timed window, same reason as the sequential arm.
    std::memset(buf.p, 0, BLOCK_SIZE);

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
            const size_t got = file.read_raw_at(buf.p, BLOCK_SIZE, at, worker_id);
            if (got != BLOCK_SIZE) {
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
        } else if (path == nullptr) {
            path = argv[i];
        } else {
            fprintf(stderr, "unexpected argument: %s\n", argv[i]);
            return 2;
        }
    }

    if (path == nullptr) {
        fprintf(stderr, "usage: bench-loader.exe <file> [--buffered]\n");
        fprintf(stderr, "                              [--threads N [--shared]] [--seconds S]\n");
        fprintf(stderr, "  default     unbuffered direct I/O, one sequential pass - the measurement\n");
        fprintf(stderr, "  --buffered  buffered - the control, must be FASTER on a warm file\n");
        fprintf(stderr, "  --threads N N workers, random offsets, time-boxed; each gets its own\n");
        fprintf(stderr, "              handle out of the pool\n");
        fprintf(stderr, "  --shared    the control for --threads: same workers, same offsets,\n");
        fprintf(stderr, "              every one of them forced onto the SHARED handle\n");
        return 2;
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
    printf("block       %zu bytes (12.75 MiB)\n", BLOCK_SIZE);

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
        if (direct && (BLOCK_SIZE % alignment) != 0) {
            fprintf(stderr, "\nABORT: block size %zu is not a multiple of the device's %zu.\n",
                    BLOCK_SIZE, alignment);
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
            if (file_size <= BLOCK_SIZE) {
                fprintf(stderr, "\nABORT: file is not larger than one block. No figure.\n");
                return 1;
            }
            // span, exactly as the Python sweep defines it: every read lands wholly
            // inside the file, so no short count can enter the numerator.
            const size_t span = file_size - BLOCK_SIZE;

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
                                i, wid, span, alignment, stop_ticks);
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

        aligned_buffer buf(BLOCK_SIZE, buf_align);
        if (buf.p == nullptr) {
            fprintf(stderr, "\nABORT: _aligned_malloc of %zu bytes failed\n", BLOCK_SIZE);
            return 1;
        }
        // Touch the pages once, outside the timed window, so first-touch faults do not
        // land in the measurement.
        std::memset(buf.p, 0, BLOCK_SIZE);

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
            const size_t got = file.read_raw_at(buf.p, BLOCK_SIZE, offset);
            if (got == 0) {
                break;
            }
            if (got < BLOCK_SIZE && offset + got < file_size) {
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
