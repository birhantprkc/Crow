#!/usr/bin/env bash
#
# verify-patch-groups - do the three group patches still add up to today's tree?
#
# WHY THIS EXISTS: E3 split worktree-on-b10223.patch into three patches with three
# audiences. Splitting a patch is only safe if the pieces still reconstruct the
# whole, and if each piece stands on its own -- otherwise the "separately
# droppable" property that justified the split is a claim, not a fact.
#
# WHAT IT CHECKS, all against the live tree by SHA-256 after CR removal:
#   1. every path of the full patch is covered by exactly one group patch
#   2. each group patch applies to pristine b10223 ON ITS OWN
#   3. all three together reconstruct the tree, in every one of the six orders
#
# It works in a throwaway mini repo holding only the patched paths, never in
# crow-lab/src. The mini repo is committed with core.autocrlf=false so its blob
# SHAs equal the b10223 ones -- that is what lets `git apply --3way` work here,
# and it is the same precondition E6 relies on in the real tree.
#
# THE FINDING THIS TOOL EXISTS TO KEEP MEASURED (2026-08-05): plain `git apply`
# CANNOT combine them. moe-stream and token-timing both touch llama-context.cpp
# and include/llama.h, both were diffed against pristine, and their context lines
# overlap around the split hunk, so whichever comes second fails. With --3way all
# six orders reconstruct the tree exactly. So: order does not matter, --3way does.
# Do not "simplify" a caller to plain apply on the strength of a green exit code.
#
# THE CASE THAT MUST FAIL: point -Patches at a directory with one patch missing.
# Coverage must come back short and the reconstruction must not reach the full
# count. A run that reports success over two of three patches would make the
# dropped one invisible, which is the whole risk of splitting.
#
# Usage:
#   verify-patch-groups.sh [workdir]
#
# Env overrides: SRC (crow-lab/src), CROW (this repo), PATCHDIR (CROW/patches),
#                FULL (name of the reference patch inside PATCHDIR)
#
# Exit 0 = all three checks green.  1 = at least one red.  2 = setup error.

set -u

SRC=${SRC:-/c/Users/robin/dev/crow-lab/src}
CROW=${CROW:-/c/Users/robin/dev/Crow}
PATCHDIR=${PATCHDIR:-$CROW/patches}
FULL=${FULL:-worktree-on-b10223.patch}
BASE=${BASE:-b10223}
WORK=${1:-${TMPDIR:-/tmp}/verify-patch-groups}

GROUP_PATCHES="moe-stream-on-b10223.patch token-timing-on-b10223.patch dflash-on-b10223.patch"
NEWFILES="src/llama-moe-stream.cpp src/llama-moe-stream.h tests/test-llama-file.cpp"

[ -d "$SRC/.git" ] || { echo "SETUP ERROR: not a git tree: $SRC"; exit 2; }
for g in $GROUP_PATCHES; do
    [ -f "$PATCHDIR/$g" ] || { echo "SETUP ERROR: missing $PATCHDIR/$g"; exit 2; }
done

paths_of() { grep "^diff --git" "$1" | sed 's|^diff --git a/||; s| b/.*$||'; }

ALL=$(cd "$CROW" && git show "HEAD:patches/$FULL" | paths_of /dev/stdin)
[ -n "$ALL" ] || { echo "SETUP ERROR: no paths in HEAD:patches/$FULL"; exit 2; }
TOTAL=$(echo "$ALL" | wc -l | tr -d ' ')

echo "=== verify-patch-groups ==="
echo "reference: HEAD:patches/$FULL -- $TOTAL paths"
rc=0

# --- 1. coverage ------------------------------------------------------------
# A path in NO group is lost on reconstruction. A path in two is expected for
# exactly the two files whose hunks were split between groups, and for nothing
# else - naming them means an unexpected duplicate is still an error instead of
# being waved through by a loosened rule.
SHARED="include/llama.h src/llama-context.cpp"
echo
echo "--- coverage ---"
for p in $ALL; do
    n=0
    for g in $GROUP_PATCHES; do
        paths_of "$PATCHDIR/$g" | grep -qxF "$p" && n=$((n + 1))
    done
    want=1
    for s in $SHARED; do [ "$p" = "$s" ] && want=2; done
    if [ "$n" -ne "$want" ]; then
        printf '  %-34s in %s group patches, want %s\n' "$p" "$n" "$want"
        rc=1
    fi
done
echo "  shared by design (hunks split): $SHARED"
for g in $GROUP_PATCHES; do
    printf '  %-32s %2s paths\n' "$g" "$(paths_of "$PATCHDIR/$g" | wc -l | tr -d ' ')"
done
[ "$rc" -eq 0 ] && echo "  all $TOTAL paths covered, each in its expected number of group patches"

# --- mini repo --------------------------------------------------------------
rm -rf "$WORK"
mkdir -p "$WORK" || { echo "SETUP ERROR: cannot create $WORK"; exit 2; }
VERSIONED=""
for p in $ALL; do
    if (cd "$SRC" && git cat-file -e "$BASE:$p" 2>/dev/null); then
        mkdir -p "$WORK/$(dirname "$p")"
        (cd "$SRC" && git show "$BASE:$p") > "$WORK/$p"
        VERSIONED="$VERSIONED $p"
    fi
done
cd "$WORK" || exit 2
git init -q .
git config core.autocrlf false
git config user.email verify@local
git config user.name verify
git add $VERSIONED
git commit -qm "pristine $BASE"

for p in $VERSIONED; do
    a=$(git rev-parse "HEAD:$p")
    b=$(cd "$SRC" && git rev-parse "$BASE:$p")
    [ "$a" = "$b" ] || { echo "SETUP ERROR: blob SHA differs for $p; --3way would be meaningless"; exit 2; }
done

reset_pristine() {
    # --hard, not `checkout --`: --3way writes the index, and checkout would then
    # restore the PATCHED state and every later result would be quietly wrong.
    git reset -q --hard HEAD
    for n in $NEWFILES; do [ -f "$n" ] && rm -f "$n"; done
    [ -z "$(git status --porcelain)" ] || { echo "  SETUP ERROR: mini repo not pristine"; exit 2; }
}

count_ok() {
    local n=0
    for p in $ALL; do
        [ -f "$p" ] || continue
        [ "$(tr -d '\r' < "$p" | sha256sum | cut -d' ' -f1)" = \
          "$(tr -d '\r' < "$SRC/$p" | sha256sum | cut -d' ' -f1)" ] && n=$((n + 1))
    done
    echo $n
}

# --- 2. each patch alone ----------------------------------------------------
echo
echo "--- each group patch alone on pristine $BASE ---"
for g in $GROUP_PATCHES; do
    reset_pristine
    if err=$(git apply "$PATCHDIR/$g" 2>&1); then
        printf '  %-32s applies, %2s paths changed\n' "$g" "$(git status --porcelain | wc -l | tr -d ' ')"
    else
        printf '  %-32s FAILED: %s\n' "$g" "$err"
        rc=1
    fi
done

# --- 3. all three, every order ----------------------------------------------
set -- $GROUP_PATCHES
M=$1 T=$2 D=$3
echo
echo "--- all three, six orders ---"
for order in "M T D" "M D T" "T M D" "T D M" "D M T" "D T M"; do
    for mode in "" "--3way"; do
        reset_pristine
        fail=""
        for k in $order; do
            eval "pf=\$$k"
            git apply $mode "$PATCHDIR/$pf" >/dev/null 2>&1 || fail="$fail$k"
        done
        ok=$(count_ok)
        marks=$(grep -rl '^<<<<<<<' $VERSIONED 2>/dev/null | wc -l | tr -d ' ')
        label=$(echo "$order" | tr -d ' ' | tr 'MTD' 'mtd')
        printf '  %-4s %-7s apply %-9s markers %s  %s of %s\n' \
            "$label" "${mode:-plain}" "$([ -n "$fail" ] && echo "FAILED:$fail" || echo ok)" \
            "$marks" "$ok" "$TOTAL"
        # Only --3way is required to reconstruct. Plain apply failing is the
        # measured finding, not a defect - see the header.
        if [ "$mode" = "--3way" ] && { [ "$ok" -ne "$TOTAL" ] || [ -n "$fail" ] || [ "$marks" != "0" ]; }; then
            rc=1
        fi
    done
done

reset_pristine
echo
if [ "$rc" -eq 0 ]; then
    echo "RESULT: green - coverage exact, each patch stands alone, --3way reconstructs $TOTAL of $TOTAL in all six orders"
else
    echo "RESULT: red - see the lines above"
fi
exit $rc
