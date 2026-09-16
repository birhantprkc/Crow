#!/usr/bin/env bash
#
# verify-patch-roundtrip - is the working tree recoverable from the patch file?
#
# WHY THIS EXISTS: crow-lab/src is a SHALLOW clone with one commit and one tag.
# There is no history to rebase onto, so `worktree-on-b10223.patch` is the only
# storage our working tree has. Before any base jump, `git checkout --detach`
# demands a clean tree, three of our paths are unversioned, and `git stash`
# without -u does not touch those. If the patch is incomplete, the jump is data
# loss and nothing says so until it is too late.
#
# WHAT IT DOES: rebuilds the tree from pristine b10223 blobs in a scratch
# directory, applies the patch there, and hashes every path against the live
# tree. It never writes inside SRC.
#
# THE YARDSTICK COMES FROM GIT, NOT FROM THE PATCH UNDER TEST. This is the whole
# point of the tool. Counting the paths in the patch you are testing means a
# patch that lost a file also loses it from its own denominator, and reports
# success over whatever is left. The path list is read from the committed patch
# in the Crow repo; the file passed on the command line is only ever applied.
#
# CR REMOVAL BEFORE HASHING: git stores LF, the working tree is checked out CRLF
# on this machine (core.autocrlf=true), and `git diff` writes LF. Comparing raw
# bytes would report 23 of 23 deviations for the most boring reason there is.
#
# THE CASE THAT MUST FAIL: drop one block from the patch and run again. It must
# come back with fewer than the full count. Both kinds of loss have to show:
#
#   awk -v d=tests/test-llama-file.cpp '
#     /^diff --git / { c=$0; sub(/^diff --git a\//,"",c); sub(/ b\/.*$/,"",c); skip=(c==d) }
#     !skip' full.patch > short.patch      # an unversioned path -> MISSING
#
#   ... with d=common/arg.cpp                                    # a versioned path -> DIFFERS
#
# A run that cannot come back red is not a check. Measured 2026-08-05: full patch
# 23 of 23, both shortened variants 22 of 23.
#
# Usage:
#   verify-patch-roundtrip.sh <patchfile> <workdir> [label]
#
# Env overrides:  SRC (default crow-lab/src), CROW (default this repo)
#
# Exit 0 = every path byte-identical.  1 = at least one deviation.  2 = setup error.

set -u

SRC=${SRC:-/c/Users/robin/dev/crow-lab/src}
CROW=${CROW:-/c/Users/robin/dev/Crow}
REFERENCE_PATCH=${REFERENCE_PATCH:-HEAD:patches/worktree-on-b10223.patch}

if [ $# -lt 2 ]; then
    sed -n '2,/^# Exit 0/p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi

PATCHFILE="$1"
WORK="$2"
LABEL="${3:-verify-patch-roundtrip}"

[ -f "$PATCHFILE" ] || { echo "SETUP ERROR: no such patch: $PATCHFILE"; exit 2; }
[ -d "$SRC/.git" ] || { echo "SETUP ERROR: not a git tree: $SRC"; exit 2; }

PATHS=$(cd "$CROW" && git show "$REFERENCE_PATCH" \
        | grep "^diff --git" | sed 's|^diff --git a/||; s| b/.*$||')
[ -n "$PATHS" ] || { echo "SETUP ERROR: no paths in $REFERENCE_PATCH"; exit 2; }
TOTAL=$(echo "$PATHS" | wc -l | tr -d ' ')

echo "=== $LABEL ==="
echo "patch    : $PATCHFILE ($(wc -c < "$PATCHFILE" | tr -d ' ') B)"
echo "yardstick: $TOTAL paths from $REFERENCE_PATCH"

rm -rf "$WORK"
mkdir -p "$WORK" || { echo "SETUP ERROR: cannot create $WORK"; exit 2; }

# Pristine b10223 for every path the tag knows. The unversioned files are absent
# here on purpose - the patch has to create them, and if it does not, they show
# up as MISSING rather than quietly passing.
extracted=0
for p in $PATHS; do
    if (cd "$SRC" && git cat-file -e "b10223:$p" 2>/dev/null); then
        mkdir -p "$WORK/$(dirname "$p")"
        (cd "$SRC" && git show "b10223:$p") > "$WORK/$p"
        extracted=$((extracted + 1))
    fi
done
echo "pristine : $extracted of $TOTAL extracted from tag b10223"

( cd "$WORK" && git apply "$PATCHFILE" ) 2>&1 | sed 's/^/  apply: /'
rc=${PIPESTATUS[0]}
echo "git apply exit: $rc"
echo

ok=0
bad=0
for p in $PATHS; do
    if [ ! -f "$WORK/$p" ]; then
        printf '  %-34s MISSING in rebuild\n' "$p"
        bad=$((bad + 1))
        continue
    fi
    a=$(tr -d '\r' < "$WORK/$p" | sha256sum | cut -d' ' -f1)
    b=$(tr -d '\r' < "$SRC/$p"  | sha256sum | cut -d' ' -f1)
    if [ "$a" = "$b" ]; then
        ok=$((ok + 1))
    else
        printf '  %-34s DIFFERS  %s vs %s\n' "$p" "${a:0:12}" "${b:0:12}"
        bad=$((bad + 1))
    fi
done

echo
echo "RESULT: $ok of $TOTAL byte-identical after CR removal ($bad deviations)"
if [ "$ok" -eq "$TOTAL" ]; then echo "VERDICT: green"; exit 0; fi
echo "VERDICT: red"
exit 1
