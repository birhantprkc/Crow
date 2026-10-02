#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# installer/build.sh -- the Linux twin of installer/build.ps1 (#342)
# ---------------------------------------------------------------------------
#
# Builds installer/dist/CrowSetup-linux-x64: the installer with the two Linux
# release packages' fetch list embedded, proven free of the builder's machine.
#
#   1. Writes installer/vendor/packages-linux-x64.json from the two tarballs:
#      asset, url (the GitHub release asset), bytes, sha256 (lower-case hex),
#      version -- `Packages` in installer/core/src/api.rs; the binary embeds it
#      (installer/app/src/bundle.rs). Linux embeds no Python: the installer
#      makes <root>/venv from the system python3 (installer/core/src/python.rs).
#   2. `cargo build --release -p crowsetup --features bundle` with
#      --remap-path-prefix for $HOME (the cargo registry lives there),
#      CARGO_HOME when it is elsewhere, and the repo root -- build.ps1's recipe
#      without +crt-static: the window links the system GTK 3 and WebKitGTK 4.1.
#   3. Runs `crowsetup --selftest` (exit 0 required; no network, no window).
#   4. The privacy gate over the binary: tools/pack-release.sh --gate, the same
#      gate as the Linux package (repack-release.py's patterns and allowlist,
#      the Linux namespace contexts, /home/<anyone>/). A refusal copies nothing.
#   5. Copies it to installer/dist/CrowSetup-linux-x64 and prints size and sha256.
#
# USAGE
#   installer/build.sh --crow-pack dist/crow-<v>-linux-x64.tar.gz \
#                      --engine-pack crow-nest-engine-<v>-linux-x64.tar.gz [--version V]
#   installer/build.sh --selftest
# --version fills a Crow package name that carries none and must agree with
# one that does; the engine's version always comes from its name.
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
CROW_RELEASES="https://github.com/nibor1896/Crow/releases/download"
ENGINE_RELEASES="https://github.com/nibor1896/crow-nest/releases/download"
# 3.0.0, or 0.8.0-3-g1a2b3c4 for an engine packed between tags
VERSION_RE='[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)*'

die() { echo "build.sh: $*" >&2; exit 1; }

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# package_version <file> <crow|engine> [version] -> the version, or dies
package_version() {
    local asset kind="$2" want="${3:-}" v=""
    [ -f "$1" ] || die "$kind package not found: $1"
    asset="$(basename "$1")"
    if [ "$kind" = crow ]; then
        [[ "$asset" =~ ^crow-($VERSION_RE)-linux-x64\.tar\.gz$ ]] && v="${BASH_REMATCH[1]}"
    else
        [[ "$asset" =~ ^crow-nest-engine-($VERSION_RE)-linux-x64\.tar\.gz$ ]] && v="${BASH_REMATCH[1]}"
    fi
    if [ -n "$want" ]; then
        [ -z "$v" ] || [ "$v" = "$want" ] || die "--version $want but $asset says $v"
        v="$want"
    fi
    [ -n "$v" ] || die "cannot read a version from '$asset' (expected $([ "$kind" = crow ] && echo 'crow-<version>-linux-x64.tar.gz' || echo 'crow-nest-engine-<version>-linux-x64.tar.gz'))"
    printf '%s' "$v"
}

# package_entry <file> <crow|engine> <version> -> one JSON object
package_entry() {
    local asset base
    asset="$(basename "$1")"
    base="$CROW_RELEASES"; [ "$2" = engine ] && base="$ENGINE_RELEASES"
    printf '{"asset": "%s", "url": "%s/v%s/%s", "bytes": %s, "sha256": "%s", "version": "%s"}' \
        "$asset" "$base" "$3" "$asset" "$(stat -c %s "$1")" "$(sha256_of "$1")" "$3"
}

# write_packages_json <crow> <engine> <version or ""> <out>
write_packages_json() {
    local cv ev
    cv="$(package_version "$1" crow "$3")" || exit 1
    ev="$(package_version "$2" engine)" || exit 1
    printf '{\n  "crow": %s,\n  "engine": %s\n}\n' "$(package_entry "$1" crow "$cv")" "$(package_entry "$2" engine "$ev")" > "$4"
}

# build_flags <repo> <home> [cargo_home] -> CARGO_ENCODED_RUSTFLAGS (0x1f between flags).
# The last remap that matches wins and the repo lies inside $HOME, so it comes last.
build_flags() {
    local flags=("--remap-path-prefix=$2=~")
    if [ -n "${3:-}" ] && [[ "$3" != "$2"* ]]; then flags+=("--remap-path-prefix=$3=cargo"); fi
    flags+=("--remap-path-prefix=$1=crow")
    local IFS=$'\x1f'
    printf '%s' "${flags[*]}"
}

selftest() {
    set +e  # the checks below run commands that must fail
    local tmp okn=0 red=0
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    check() { if [ "$2" = 0 ]; then echo "  ok   $1"; okn=$((okn + 1)); else echo "  FAIL $1"; red=$((red + 1)); fi; }
    printf 'crow' > "$tmp/crow-9.9.9-linux-x64.tar.gz"
    printf 'engine' > "$tmp/crow-nest-engine-1.2.3-4-gabc1234-linux-x64.tar.gz"
    write_packages_json "$tmp/crow-9.9.9-linux-x64.tar.gz" "$tmp/crow-nest-engine-1.2.3-4-gabc1234-linux-x64.tar.gz" "" "$tmp/p.json"
    python3 - "$tmp/p.json" "$tmp" <<'PY'
import hashlib, json, sys
p = json.load(open(sys.argv[1]))
assert list(p) == ["crow", "engine"], p
for k in p:
    assert list(p[k]) == ["asset", "url", "bytes", "sha256", "version"], p[k]
c, e = p["crow"], p["engine"]
assert c["version"] == "9.9.9" and e["version"] == "1.2.3-4-gabc1234", p
assert c["url"] == "https://github.com/nibor1896/Crow/releases/download/v9.9.9/crow-9.9.9-linux-x64.tar.gz", c
assert e["url"].startswith("https://github.com/nibor1896/crow-nest/releases/download/v1.2.3-4-gabc1234/"), e
assert c["bytes"] == 4 and isinstance(c["bytes"], int)
assert c["sha256"] == hashlib.sha256(b"crow").hexdigest(), c
PY
    check "packages-linux-x64.json: shape, versions, urls, bytes, sha256" $?
    ( package_version "$tmp/crow-9.9.9-linux-x64.tar.gz" crow 1.0.0 ) >/dev/null 2>&1
    check "a --version that disagrees with the name is refused" $(( $? == 0 ))
    [ "$(package_version "$tmp/crow-9.9.9-linux-x64.tar.gz" crow 9.9.9)" = 9.9.9 ]
    check "a --version that agrees is fine" $?
    printf x > "$tmp/crow-package.tar.gz"
    ( package_version "$tmp/crow-package.tar.gz" crow ) >/dev/null 2>&1
    check "a name without a version needs --version" $(( $? == 0 ))
    [ "$(package_version "$tmp/crow-package.tar.gz" crow 3.0.0)" = 3.0.0 ]
    check "...and takes it" $?
    ( package_version "$tmp/crow-nest-engine-1.2.3-4-gabc1234-linux-x64.tar.gz" crow ) >/dev/null 2>&1
    check "the engine pack is not accepted as Crow's" $(( $? == 0 ))
    printf x > "$tmp/crow-9.9.9-win-x64.zip"
    ( package_version "$tmp/crow-9.9.9-win-x64.zip" crow ) >/dev/null 2>&1
    check "a Windows zip is not a Linux package" $(( $? == 0 ))
    [ "$(build_flags '/h/u x/dev/Crow' '/h/u x' '/opt/cargo' | tr '\037' '|')" = '--remap-path-prefix=/h/u x=~|--remap-path-prefix=/opt/cargo=cargo|--remap-path-prefix=/h/u x/dev/Crow=crow' ]
    check "remaps: home, cargo home, repo, in that order; a space stays in one flag" $?
    [ "$(build_flags /h/u/dev/Crow /h/u /h/u/.cargo | tr '\037' '|')" = '--remap-path-prefix=/h/u=~|--remap-path-prefix=/h/u/dev/Crow=crow' ]
    check "a cargo home under \$HOME needs no flag of its own" $?
    printf 'ELF built in /build/crowsetup' > "$tmp/crowsetup"
    bash "$REPO/tools/pack-release.sh" --gate "$tmp/crowsetup" >/dev/null 2>&1
    check "a binary with remapped paths only passes the gate" $?
    printf 'ELF panicked at %s/.cargo/registry/src/wry/lib.rs' "$HOME" > "$tmp/crowsetup"
    bash "$REPO/tools/pack-release.sh" --gate "$tmp/crowsetup" >/dev/null 2>&1
    check "a binary carrying \$HOME is refused" $(( $? == 0 ))
    if [ "$red" -gt 0 ]; then echo "RESULT: $red of $((okn + red)) FAILED"; return 1; fi
    echo "RESULT: SELFTEST OK - $((okn + red)) checks"
}

CROW_PACK="" ENGINE_PACK="" VERSION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --crow-pack)   CROW_PACK="$2"; shift 2 ;;
        --engine-pack) ENGINE_PACK="$2"; shift 2 ;;
        --version)     VERSION="$2"; shift 2 ;;
        --selftest)    selftest; exit $? ;;
        -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
        *)             die "unknown argument $1" ;;
    esac
done
[ -n "$CROW_PACK" ] && [ -n "$ENGINE_PACK" ] || die "--crow-pack and --engine-pack are required (or --selftest)"
[ -n "${HOME:-}" ] || die "HOME is not set - the remap and the privacy gate need it"
command -v cargo >/dev/null 2>&1 || die "cargo not found on PATH"

VENDOR="$HERE/vendor"
DIST="$HERE/dist"
OUT="$DIST/CrowSetup-linux-x64"
# nothing from an earlier run may survive a refusal in this one
rm -f "$OUT"
mkdir -p "$VENDOR" "$DIST"

echo "1/5 packages-linux-x64.json"
write_packages_json "$CROW_PACK" "$ENGINE_PACK" "$VERSION" "$VENDOR/packages-linux-x64.json"
python3 -c 'import json,sys; [print("  %-6s %s  %s bytes  %s" % (k, v["version"], format(v["bytes"], ","), v["sha256"])) for k, v in json.load(open(sys.argv[1])).items()]' "$VENDOR/packages-linux-x64.json"

echo "2/5 cargo build --release -p crowsetup --features bundle"
CARGO_ENCODED_RUSTFLAGS="$(build_flags "$REPO" "${HOME%/}" "${CARGO_HOME:-}")"
export CARGO_ENCODED_RUSTFLAGS
unset RUSTFLAGS
( cd "$HERE" && cargo build --release -p crowsetup --features bundle )
BUILT="$HERE/target/release/crowsetup"
[ -f "$BUILT" ] || die "cargo reported success but $BUILT does not exist"

echo "3/5 crowsetup --selftest"
if ! "$BUILT" --selftest > "$DIST/selftest.log" 2>&1; then
    tail -n 15 "$DIST/selftest.log" | sed 's/^/  /'
    die "crowsetup --selftest exited non-zero"
fi
tail -n 3 "$DIST/selftest.log" | sed 's/^/  /'
rm -f "$DIST/selftest.log"

echo "4/5 privacy gate"
if ! bash "$REPO/tools/pack-release.sh" --gate "$BUILT" | sed 's/^/  /'; then
    die "privacy gate refused $BUILT - nothing was copied to $DIST"
fi

echo "5/5 dist"
cp "$BUILT" "$OUT"
chmod 755 "$OUT"
size="$(stat -c %s "$OUT")"
printf 'RESULT: %s  %s bytes (%s MiB)  sha256 %s\n' "$OUT" "$size" "$(python3 -c "print('%.1f' % ($size / 1048576))")" "$(sha256_of "$OUT")"
