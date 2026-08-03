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

#include "llama-mmap.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>

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

int main(int argc, char ** argv) {
    // Unbuffered, and not for cosmetics: when the stage 4 test crashed, buffered stdout
    // was never flushed and the failure looked like it happened before main() started.
    setvbuf(stdout, NULL, _IONBF, 0);

    bool buffered = false;
    const char * path = nullptr;

    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        if (a == "--buffered") {
            buffered = true;
        } else if (path == nullptr) {
            path = argv[i];
        } else {
            fprintf(stderr, "unexpected argument: %s\n", argv[i]);
            return 2;
        }
    }

    if (path == nullptr) {
        fprintf(stderr, "usage: bench-loader.exe <file> [--buffered]\n");
        fprintf(stderr, "  default     unbuffered direct I/O - the measurement\n");
        fprintf(stderr, "  --buffered  buffered - the control, must be FASTER on a warm file\n");
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

        aligned_buffer buf(BLOCK_SIZE, buf_align);
        if (buf.p == nullptr) {
            fprintf(stderr, "\nABORT: _aligned_malloc of %zu bytes failed\n", BLOCK_SIZE);
            return 1;
        }
        // Touch the pages once, outside the timed window, so first-touch faults do not
        // land in the measurement.
        std::memset(buf.p, 0, BLOCK_SIZE);

        LARGE_INTEGER freq, t0, t1;
        QueryPerformanceFrequency(&freq);

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
