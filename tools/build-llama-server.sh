#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build-llama-server.sh -- build the Crow CUDA llama-server for Linux, no root
# ---------------------------------------------------------------------------
#
# WHAT
#   Produces $CROW_HOME/bin/llama-server: a CUDA llama-server that can load the
#   `qwen4exp` architecture (Qwen3.8-Flash-Next GGUF), built for Blackwell
#   (sm_120, RTX 5090) from a pinned llama.cpp tree plus two upstream patches.
#   Everything -- cmake, ninja, the CUDA toolkit, the source tree, the build --
#   lives under $CROW_HOME. Nothing is installed system-wide and nothing needs
#   sudo, because on the target machine nobody can type a root password.
#
# WHY THIS PIN AND NOT MASTER
#   The Windows operating point in README.md is measured at llama.cpp pin
#   6c84c7d5d (the squash-merge of PR #27742, "model: add Qwen3.8-Flash-Next
#   (qwen4exp)", 2026-08-27) plus PR #27880 and PR #28040. All three are merged
#   upstream now:
#       #27742 -> 6c84c7d5d8833c6e0df69628f75a0f599797934e  2026-08-27
#       #27880 -> 6fe74980162af0ed5e559870d5deccafaa034e7c  2026-08-28
#       #28040 -> b356fa2624643b6d5753162ae43efff8cdd4d8cb  2026-09-01
#   so a plain `master` checkout would also contain the qwen4exp support. We
#   deliberately do NOT take master or the latest release tag (b10997 at the
#   time of writing, 2026-09-16):
#
#     1. Parity. The decode/prefill numbers this project quotes were taken on
#        exactly this tree. A newer tree is a different engine and would
#        silently invalidate every measurement it is compared against.
#     2. The README records that b10687 and newer aborted during CUDA warmup on
#        this card on Windows, cause unattributed. That report is unresolved
#        from our side, so "newer" is not known-good here.
#     3. qwen4exp is young and still churning upstream. As of 2026-09-16 the
#        open issue list against newer builds includes #28734 (CUDA decode
#        slows linearly with context), #28497 (QSA indexer top-k picks a
#        different cell set each run on CUDA), #28280 (server slot livelock at
#        b10731) and #27780 (graph builder ggml_abort under sustained load).
#        None of these is a reason to move forward; several are reasons not to.
#
#   HOW CHECKED: PR/commit state read from the GitHub API; issue states read
#   from the same. The one directly-on-point Blackwell report, #28403 ("CUDA
#   SOFT_MAX invalid argument on qwen4_exp, compute_120a"), was closed as
#   completed on 2026-09-05 -- and its root cause was NOT the model code: the
#   reporter had compiled with nvcc 13.3 but linked against a system CUDA 12
#   runtime, because CMake was given CMAKE_CUDA_COMPILER without
#   CUDAToolkit_ROOT. See "CUDA RUNTIME PINNING" below -- that finding is why
#   this script pins the toolkit root and then verifies the link with ldd.
#
#   If you ever do want to move: set LLAMA_PIN to the new commit and clear
#   LLAMA_PRS. Everything else here is version-agnostic.
#
# WHY THE CUDA REDIST TARBALLS AND NOT THE .run INSTALLER
#   The obvious no-root route is the runfile with --toolkitpath into a user
#   directory. cuda_13.3.1_610.43.02_linux.run is 4,323,954,814 bytes. Measured
#   bandwidth to developer.download.nvidia.com from this machine was ~1.8 MB/s,
#   i.e. ~40 minutes, and >95% of that payload (nsight, cuda-gdb, cufft,
#   cusolver, cusparse, npp, the bundled driver) is dead weight for a
#   llama-server build. We instead fetch the individual component archives that
#   NVIDIA publishes at .../compute/cuda/redist/ -- the *same official binaries*
#   the runfile unpacks, just split per component and with no installer. The 15
#   components below total ~970 MB and merge into one ordinary CUDA layout
#   (bin/ include/ lib/ nvvm/), which is exactly what CMake's FindCUDAToolkit
#   expects. This is not the fragile pip-wheel route; the wheels are themselves
#   repackagings of these archives.
#   Set CUDA_USE_RUNFILE=1 to take the runfile path instead (prints the exact
#   command; still no root).
#
# WHY CUDA 13.3
#   The driver is 610.57.04 and nvidia-smi reports "CUDA UMD Version: 13.3", so
#   13.3 is the newest toolkit this driver serves without CUDA forward-compat
#   shims. RTX 5090 / sm_120 needs >= 12.8 in any case.
#
# HOST COMPILER
#   nvcc 13.3 refuses GNU > 15 (crt/host_config.h). This machine has gcc 16.2.1
#   and no root to install an older one, so we pass -allow-unsupported-compiler.
#   HOW CHECKED: a standalone sm_120 test kernel compiled with gcc 16.2.1 +
#   -allow-unsupported-compiler, linked, launched, and returned correct results
#   before this script was trusted with the real build. If a future gcc does
#   break the CCCL/libstdc++ mix, the no-root fallback is to unpack Arch's
#   `gcc15` package (extra/x86_64, ~48 MB, a plain zstd tarball) into
#   $CROW_HOME/tools and point CUDAHOSTCXX at it -- no pacman, no sudo.
#
# CUDA RUNTIME PINNING
#   Per upstream #28403 and #25060, compiling with one CUDA major version while
#   linking another's libcudart/libcublas produces exactly the kind of bogus
#   device-property and kernel-launch failures that look like a warmup abort.
#   So: CUDAToolkit_ROOT, CMAKE_CUDA_COMPILER and the RPATH are all pinned to
#   the same prefix, and the build is not accepted until `ldd` shows the binary
#   resolving libcudart/libcublas/libcublasLt to that prefix at .so.13.
#   Related: this machine was checked for the #25060 symptom directly --
#   cudaGetDeviceProperties reports sharedMemPerBlockOptin = 101376 (correct),
#   not the 4294967297 seen on the affected driver, so that trap is not armed
#   here.
#
# LINKAGE
#   BUILD_SHARED_LIBS=OFF, so ggml/llama go into the executable and there are no
#   loose libggml*.so to keep in sync next to the binary. The CUDA libraries stay
#   dynamic (libcublasLt alone is hundreds of MB; copying it per-binary is
#   silly), and are reached through a DT_RPATH -- not DT_RUNPATH -- pointing at
#   $CROW_HOME/cuda/lib, so a stray LD_LIBRARY_PATH cannot substitute a
#   different CUDA runtime underneath us. Consequence: $CROW_HOME/cuda must stay
#   where it is. Deleting it breaks the binary.
#
# USAGE
#   tools/build-llama-server.sh                # full build, reuses everything
#   JOBS=8 tools/build-llama-server.sh         # gentler on the machine
#   CLEAN=1 tools/build-llama-server.sh        # nuke the build dir first
#
# Idempotent: every step checks for its own output first. A re-run after a
# completed build downloads nothing, re-patches nothing, compiles nothing and
# reinstalls the same bytes -- it costs ~2 s and exists mainly to re-run the
# verification. `CLEAN=1` is the way to actually force a rebuild.
# ---------------------------------------------------------------------------

set -euo pipefail

# --- parameters ------------------------------------------------------------
CROW_HOME="${CROW_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/crow}"

# Pin = squash-merge of PR #27742 (qwen4exp). See "WHY THIS PIN" above.
LLAMA_PIN="${LLAMA_PIN:-6c84c7d5d8833c6e0df69628f75a0f599797934e}"

# Patches applied on top, as "<pr-number>:<upstream-merge-commit>", in order.
# #27880 cherry-picks clean. #28040 conflicts in one hunk and is carried as a
# pre-resolved patch in this repo -- see apply_pr() and patches/linux/.
LLAMA_PRS="${LLAMA_PRS:-27880:6fe74980162af0ed5e559870d5deccafaa034e7c 28040:b356fa2624643b6d5753162ae43efff8cdd4d8cb}"

CUDA_ARCH="${CUDA_ARCH:-120}"          # sm_120, Blackwell / RTX 5090
JOBS="${JOBS:-$(nproc)}"
CLEAN="${CLEAN:-0}"
CUDA_USE_RUNFILE="${CUDA_USE_RUNFILE:-0}"

LLAMA_UPSTREAM="${LLAMA_UPSTREAM:-https://github.com/ggml-org/llama.cpp.git}"

# Derived layout.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$CROW_HOME/src/llama.cpp"
# Deliberately OUTSIDE the source tree: ensure_src() may delete and re-create
# SRC_DIR (see the shallow-fetch note there) and that must not cost a rebuild.
BUILD_DIR="$CROW_HOME/build/llama-cuda"
BIN_DIR="$CROW_HOME/bin"
CUDA_ROOT="$CROW_HOME/cuda"
VENV_DIR="$CROW_HOME/venv-build"
DL_DIR="$CROW_HOME/tools/cuda-dl"
PATCH_DIR="$REPO_DIR/patches/linux"
SRC_STAMP="$CROW_HOME/build/llama-src.stamp"

# CUDA 13.3.1 redistributable components. Versions are the ones listed in
# https://developer.download.nvidia.com/compute/cuda/redist/redistrib_13.3.1.json
# (release_date 2026-06-29). Sizes are the compressed .tar.xz, from that JSON.
CUDA_REDIST_BASE="https://developer.download.nvidia.com/compute/cuda/redist"
CUDA_COMPONENTS="
cuda_nvcc/linux-x86_64/cuda_nvcc-linux-x86_64-13.3.73-archive.tar.xz
cuda_crt/linux-x86_64/cuda_crt-linux-x86_64-13.3.73-archive.tar.xz
cuda_cudart/linux-x86_64/cuda_cudart-linux-x86_64-13.3.29-archive.tar.xz
cccl/linux-x86_64/cccl-linux-x86_64-13.3.3.4.1-archive.tar.xz
libcublas/linux-x86_64/libcublas-linux-x86_64-13.6.0.2-archive.tar.xz
libnvjitlink/linux-x86_64/libnvjitlink-linux-x86_64-13.3.33-archive.tar.xz
libnvvm/linux-x86_64/libnvvm-linux-x86_64-13.3.73-archive.tar.xz
libnvptxcompiler/linux-x86_64/libnvptxcompiler-linux-x86_64-13.3.73-archive.tar.xz
cuda_cuobjdump/linux-x86_64/cuda_cuobjdump-linux-x86_64-13.3.73-archive.tar.xz
cuda_nvprune/linux-x86_64/cuda_nvprune-linux-x86_64-13.3.29-archive.tar.xz
cuda_profiler_api/linux-x86_64/cuda_profiler_api-linux-x86_64-13.3.27-archive.tar.xz
cuda_nvtx/linux-x86_64/cuda_nvtx-linux-x86_64-13.3.29-archive.tar.xz
cuda_culibos/linux-x86_64/cuda_culibos-linux-x86_64-13.3.33-archive.tar.xz
libcuobjclient/linux-x86_64/libcuobjclient-linux-x86_64-1.2.0.68-archive.tar.xz
cuda_nvrtc/linux-x86_64/cuda_nvrtc-linux-x86_64-13.3.33-archive.tar.xz
"
CUDA_RUNFILE_URL="https://developer.download.nvidia.com/compute/cuda/13.3.1/local_installers/cuda_13.3.1_610.43.02_linux.run"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
run() { printf '    $ %s\n' "$*"; "$@"; }

# --- 1. cmake + ninja ------------------------------------------------------
# No root, so no pacman. A venv with the cmake and ninja wheels is the smallest
# dependable way to get both; the wheels ship real upstream binaries.
ensure_cmake_ninja() {
    say "cmake + ninja"
    if [ ! -x "$VENV_DIR/bin/cmake" ] || [ ! -x "$VENV_DIR/bin/ninja" ]; then
        [ -d "$VENV_DIR" ] || run python3 -m venv "$VENV_DIR"
        run "$VENV_DIR/bin/python" -m ensurepip --upgrade
        run "$VENV_DIR/bin/pip" install --quiet cmake ninja
    fi
    CMAKE="$VENV_DIR/bin/cmake"
    NINJA="$VENV_DIR/bin/ninja"
    echo "    cmake $("$CMAKE" --version | head -1 | awk '{print $3}')  ninja $("$NINJA" --version)"
}

# --- 2. CUDA toolkit -------------------------------------------------------
# A prefix is only usable if it has the compiler AND the libraries ggml-cuda
# links. Checking for nvcc alone is not enough: an interrupted component
# download leaves a prefix with a working nvcc and no cuBLAS, and the failure
# then surfaces much later and much less legibly, as
# `target_link_libraries ... CUDA::cublas but the target was not found`.
cuda_prefix_complete() {
    local root="$1"
    [ -x "$root/bin/nvcc" ]                 || return 1
    [ -f "$root/include/cuda_runtime.h" ]   || return 1
    ls "$root"/lib*/libcublas.so*   >/dev/null 2>&1 || return 1
    ls "$root"/lib*/libcublasLt.so* >/dev/null 2>&1 || return 1
    ls "$root"/lib*/libcudart.so*   >/dev/null 2>&1 || return 1
    return 0
}

ensure_cuda() {
    say "CUDA toolkit"

    # Prefer a system toolkit if someone did install one, but only if it is the
    # same major version the driver serves -- see CUDA RUNTIME PINNING.
    if command -v nvcc >/dev/null 2>&1 && [ "$CUDA_USE_RUNFILE" != "1" ]; then
        local sysroot; sysroot="$(dirname "$(dirname "$(command -v nvcc)")")"
        echo "    found system nvcc: $(command -v nvcc)"
        CUDA_ROOT="$sysroot"
    elif cuda_prefix_complete "$CUDA_ROOT"; then
        echo "    reusing $CUDA_ROOT"
    elif [ "$CUDA_USE_RUNFILE" = "1" ]; then
        # Fallback path: NVIDIA's runfile, unpacked into a user directory.
        # 4,323,954,814 bytes (~4.3 GB) -- 4.5x the component route for the
        # same compiler. --override is needed because the installer otherwise
        # refuses an unsupported host compiler; the --no-* flags keep it from
        # touching anything outside --toolkitpath.
        mkdir -p "$DL_DIR" "$CUDA_ROOT" "$DL_DIR/tmp"
        local rf="$DL_DIR/$(basename "$CUDA_RUNFILE_URL")"
        echo "    download: 4,323,954,814 bytes (4.3 GB)"
        [ -s "$rf" ] || run curl -fL --retry 5 -C - -o "$rf" "$CUDA_RUNFILE_URL"
        run sh "$rf" --silent --toolkit --toolkitpath="$CUDA_ROOT" \
            --tmpdir="$DL_DIR/tmp" --no-man-page --no-drm --no-opengl-libs --override
    else
        mkdir -p "$DL_DIR" "$CUDA_ROOT"
        echo "    download: 15 CUDA 13.3.1 redist components, ~970 MB total"
        echo "    (libcublas alone is 818 MB of that)"
        local rel f
        for rel in $CUDA_COMPONENTS; do
            f="$DL_DIR/$(basename "$rel")"
            if [ ! -s "$f" ]; then
                # -C - so an interrupted run resumes instead of restarting;
                # the big component is a ~7 minute download on a slow link.
                run curl -fL --retry 5 --retry-delay 3 -C - -o "$f" "$CUDA_REDIST_BASE/$rel"
            fi
        done
        # Each archive is <component>-<ver>-archive/{bin,include,lib,...}, so
        # --strip-components=1 merges them all into one normal CUDA prefix.
        for rel in $CUDA_COMPONENTS; do
            run tar -xf "$DL_DIR/$(basename "$rel")" -C "$CUDA_ROOT" --strip-components=1
        done
        # The redist archives use lib/; some CMake/CUDA paths still look for
        # lib64/ as a plain installation would have.
        ln -sfn lib "$CUDA_ROOT/lib64"
    fi

    NVCC="$CUDA_ROOT/bin/nvcc"
    cuda_prefix_complete "$CUDA_ROOT" || {
        echo "incomplete CUDA prefix at $CUDA_ROOT (need nvcc, cuda_runtime.h, cudart, cublas, cublasLt)" >&2
        exit 1
    }
    echo "    $("$NVCC" --version | tail -2 | head -1)"
}

# --- 3. source tree --------------------------------------------------------
# A shallow fetch of the exact commits we need: GitHub serves arbitrary full
# SHAs, so there is no reason to clone history we will never read (a full clone
# is ~400 MB, this is ~40 MB). Abbreviated SHAs are NOT fetchable this way,
# which is why LLAMA_PIN is spelled out in full.
#
# Depth 50, not 1, because apply_pr() cherry-picks: computing a commit's patch
# needs that commit's PARENT tree, and a depth-1 fetch stops at the commit.

# True when every commit we need, and the parent of each PR commit, is already
# an object in the local repo -- in which case we can skip the network entirely
# and the script re-runs offline.
src_have_all() {
    local pr
    git -C "$SRC_DIR" cat-file -e "${LLAMA_PIN}^{commit}" 2>/dev/null || return 1
    for pr in $LLAMA_PRS; do
        git -C "$SRC_DIR" cat-file -e "${pr#*:}^{commit}"  2>/dev/null || return 1
        git -C "$SRC_DIR" cat-file -e "${pr#*:}^^{commit}" 2>/dev/null || return 1
    done
    return 0
}

# Identity of "the tree this script would produce": the pin, the PR list and the
# contents of every patch we might apply. If that key is unchanged and the
# worktree is still sitting on the commit we last produced, there is nothing to
# do -- and we must NOT redo it anyway. `git reset --hard` plus re-applying the
# patches rewrites src/llama-kv-cells.h, which ~170 translation units include
# transitively, so a "no-op" run would otherwise recompile half of libllama.
src_stamp_key() {
    {
        echo "$LLAMA_PIN"
        echo "$LLAMA_PRS"
        ls "$PATCH_DIR"/*.patch >/dev/null 2>&1 && cat "$PATCH_DIR"/*.patch
    } | sha256sum | cut -d' ' -f1
}

src_settled() {
    [ -f "$SRC_STAMP" ] || return 1
    local key head
    read -r key head < "$SRC_STAMP"
    [ "$key" = "$(src_stamp_key)" ] || return 1
    [ "$head" = "$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null)" ] || return 1
    [ -z "$(git -C "$SRC_DIR" status --porcelain 2>/dev/null)" ] || return 1
    return 0
}

src_init() {
    mkdir -p "$(dirname "$SRC_DIR")"
    git init -q "$SRC_DIR"
    git -C "$SRC_DIR" remote add origin "$LLAMA_UPSTREAM"
}

ensure_src() {
    say "llama.cpp source"
    [ -d "$SRC_DIR/.git" ] || src_init

    # A previous run killed mid-fetch or mid-cherry-pick leaves lock files and
    # a half-finished sequencer state behind, and every later git command then
    # refuses to run. This script owns this tree, so clearing them is safe.
    rm -f "$SRC_DIR/.git/shallow.lock" "$SRC_DIR/.git/index.lock"
    git -C "$SRC_DIR" cherry-pick --abort >/dev/null 2>&1 || true
    rm -rf "$SRC_DIR/.git/sequencer"

    local want="$LLAMA_PIN" pr
    for pr in $LLAMA_PRS; do want="$want ${pr#*:}"; done

    if src_have_all; then
        echo "    all commits already present, skipping fetch"
    else
        # shellcheck disable=SC2086
        if ! git -C "$SRC_DIR" fetch --depth 50 -q origin $want; then
            # Deepening an EXISTING shallow repo that already carries grafts
            # fails with "Failed to traverse parents" / "remote did not send
            # all necessary objects" -- git and the server disagree about what
            # the client can reach. Re-fetching into a clean repo always works
            # and only costs ~40 MB, so do not try to negotiate: start over.
            echo "    shallow deepen failed, re-initialising $SRC_DIR"
            rm -rf "$SRC_DIR"
            src_init
            # shellcheck disable=SC2086
            run git -C "$SRC_DIR" fetch --depth 50 -q origin $want
        fi
    fi

    run git -C "$SRC_DIR" checkout -q --detach "$LLAMA_PIN"
    run git -C "$SRC_DIR" reset -q --hard "$LLAMA_PIN"
    run git -C "$SRC_DIR" clean -qfd
    echo "    pin: $(git -C "$SRC_DIR" log -1 --format='%h %s' )"
}

# Apply one "<pr>:<sha>". Cherry-pick first, because a clean cherry-pick is the
# honest thing and it keeps upstream's commit message. If it conflicts, fall
# back to a pre-resolved patch committed in this repo -- generated once, by
# hand, and round-trip checked to produce a tree identical to the manual
# resolution. We do not attempt to resolve conflicts programmatically at build
# time; a build script silently guessing at a merge is how wrong binaries ship.
apply_pr() {
    local prnum="${1%%:*}" sha="${1#*:}"
    local patch; patch="$(ls "$PATCH_DIR/$prnum"-*.patch 2>/dev/null | head -1 || true)"

    # Take the identity and the timestamps from the upstream commit rather than
    # from "now". Without this, every re-run produces a different commit SHA for
    # the same content, llama.cpp bakes that SHA into build-info, and
    # `llama-server --version` reports a different commit each build -- which
    # both defeats the point of a pin and forces a needless relink. With it, the
    # applied tree is a pure function of the pin plus the patch list, so two
    # runs a week apart yield byte-identical commit ids.
    local an ae ad
    an="$(git -C "$SRC_DIR" show -s --format=%an "$sha")"
    ae="$(git -C "$SRC_DIR" show -s --format=%ae "$sha")"
    ad="$(git -C "$SRC_DIR" show -s --format=%aI "$sha")"
    export GIT_AUTHOR_NAME="$an" GIT_AUTHOR_EMAIL="$ae" GIT_AUTHOR_DATE="$ad"
    export GIT_COMMITTER_NAME="$an" GIT_COMMITTER_EMAIL="$ae" GIT_COMMITTER_DATE="$ad"

    if git -C "$SRC_DIR" cherry-pick -x "$sha" >/dev/null 2>&1; then
        echo "    PR #$prnum ($sha): cherry-picked clean"
        return
    fi
    git -C "$SRC_DIR" cherry-pick --abort >/dev/null 2>&1 || true
    if [ -z "$patch" ]; then
        echo "PR #$prnum ($sha) does not apply and no patches/linux/$prnum-*.patch exists" >&2
        exit 1
    fi
    run git -C "$SRC_DIR" apply "$patch"
    git -C "$SRC_DIR" commit -qam \
        "PR #$prnum rebased onto $LLAMA_PIN (patches/linux/$(basename "$patch"))"
    echo "    PR #$prnum ($sha): cherry-pick conflicted, applied $(basename "$patch")"
}

apply_prs() {
    say "patches"
    local pr
    for pr in $LLAMA_PRS; do apply_pr "$pr"; done
    unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_AUTHOR_DATE
    unset GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL GIT_COMMITTER_DATE
    # This SHA is what `llama-server --version` reports. It is deterministic --
    # see apply_pr() -- so it is safe to quote in docs as the engine identity.
    echo "    HEAD: $(git -C "$SRC_DIR" log -1 --format='%h')"
    mkdir -p "$(dirname "$SRC_STAMP")"
    echo "$(src_stamp_key) $(git -C "$SRC_DIR" rev-parse HEAD)" > "$SRC_STAMP"
    git -C "$SRC_DIR" log --oneline "$LLAMA_PIN..HEAD" | sed 's/^/      /'
}

# --- 4. configure + build --------------------------------------------------
configure_build() {
    say "configure"
    [ "$CLEAN" = "1" ] && rm -rf "$BUILD_DIR"
    run "$CMAKE" -S "$SRC_DIR" -B "$BUILD_DIR" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" \
        -DCMAKE_CUDA_COMPILER="$NVCC" \
        -DCUDAToolkit_ROOT="$CUDA_ROOT" \
        -DCMAKE_CUDA_FLAGS="-allow-unsupported-compiler" \
        -DGGML_NATIVE=ON \
        -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_TOOLS=ON \
        -DBUILD_SHARED_LIBS=OFF \
        -DCMAKE_MAKE_PROGRAM="$NINJA" \
        -DCMAKE_INSTALL_RPATH="$CUDA_ROOT/lib" \
        -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
        -DCMAKE_EXE_LINKER_FLAGS="-Wl,--disable-new-dtags"

    say "build (llama-server, -j$JOBS)"
    run "$CMAKE" --build "$BUILD_DIR" --target llama-server -j "$JOBS"
}

# --- 5. install + verify ---------------------------------------------------
install_verify() {
    say "install"
    mkdir -p "$BIN_DIR"
    run install -m 0755 "$BUILD_DIR/bin/llama-server" "$BIN_DIR/llama-server"

    say "verify"
    # The link check is the point of this step, not a formality: see
    # CUDA RUNTIME PINNING. A .so.12 here, or a "not found", means the binary
    # will misbehave at warmup rather than at startup.
    echo "    -- ldd (CUDA libraries) --"
    ldd "$BIN_DIR/llama-server" | grep -Ei 'cuda|cublas|nvjit' | sed 's/^/    /' || true
    if ldd "$BIN_DIR/llama-server" | grep -qE '=> not found'; then
        echo "unresolved shared libraries" >&2; ldd "$BIN_DIR/llama-server" | grep 'not found' >&2; exit 1
    fi
    if ldd "$BIN_DIR/llama-server" | grep -qE 'libcudart\.so\.12|libcublas\.so\.12'; then
        echo "linked against a CUDA 12 runtime while compiling with $("$NVCC" --version | tail -2 | head -1)" >&2
        exit 1
    fi
    echo "    -- --version --"
    "$BIN_DIR/llama-server" --version 2>&1 | sed 's/^/    /'
    echo "    -- --list-devices --"
    "$BIN_DIR/llama-server" --list-devices 2>&1 | sed 's/^/    /'
}

main() {
    say "crow llama-server build  (CROW_HOME=$CROW_HOME)"
    mkdir -p "$CROW_HOME"
    ensure_cmake_ninja
    ensure_cuda
    if src_settled; then
        say "llama.cpp source"
        echo "    already at $(git -C "$SRC_DIR" rev-parse --short HEAD) with the expected patches, untouched"
    else
        ensure_src
        apply_prs
    fi
    configure_build
    install_verify
    say "done: $BIN_DIR/llama-server"
}

main "$@"
