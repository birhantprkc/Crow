#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh -- Crow on Linux: one command, five steps, no root.
# ---------------------------------------------------------------------------
#
#     curl -fsSL https://raw.githubusercontent.com/nibor1896/Crow/main/install.sh | bash
#
# THE CONTRACT IS install.ps1's, TRANSLATED, NOT REINVENTED. Same five steps in
# the same order (preflight, source, install, runtime, what is left to do), same
# per-file sha256 verification, same refusal to elevate, same refusal to
# download the model. What differs is only what the platform makes differ:
#
#   %LOCALAPPDATA%\Crow   ->  ${XDG_DATA_HOME:-~/.local/share}/crow
#   WebView2              ->  WebKitGTK 4.1 through PyGObject
#   Win32 clipboard       ->  wl-clipboard
#   a Start-menu shortcut ->  a .desktop entry, hicolor icons, a Hyprland rule
#   a packaged engine     ->  tools/build-llama-server.sh, built here from a pin
#
# ORDER MATTERS, and it is install.ps1's reason verbatim: every check that can
# reject this machine runs BEFORE anything is downloaded. Finding out afterwards
# that the card is too small is the most expensive possible failure.
#
# NO ROOT, ANYWHERE. Nothing is written outside $HOME. The one thing this script
# cannot do without a password -- installing the GTK/WebKit bindings from pacman
# -- it does not attempt: it prints the line and says the window needs it. A
# script that runs sudo behind a pipe from the internet is the wrong shape.
#
# THE MODEL IS NOT DOWNLOADED HERE. 73.45 GiB, not ours to redistribute, and an
# installer that spends an hour on somebody else's file before the user has seen
# anything work is the wrong shape. The last step prints the two commands.
#
# IDEMPOTENT. Run it twice and the second run copies the same bytes, reuses the
# venv, reuses the engine, and reports what changed underneath it since the last
# run -- that is what $CROW_HOME/manifest.sha256 is for. It never deletes
# anything it did not install: bin/, cuda/, src/, build/, venv/ and the model
# tree survive every re-run, because the only files it removes are the ones the
# PREVIOUS manifest listed and the new payload no longer ships. That direction
# is install.ps1's Find-DroppedFiles, and the direction is the whole design --
# asked the other way round ("what is on disk that the manifest does not name?")
# it deletes the user's data and needs a list of exceptions nobody can write.
#
# USAGE
#   bash install.sh                      from a checkout: install what is here
#   curl -fsSL <raw>/install.sh | bash   no checkout: fetch CROW_REF (main)
#   bash install.sh --models DIR         where the GGUFs live ($CROW_MODELS)
#   bash install.sh --voice              also faster-whisper + sounddevice
#   bash install.sh --build-engine       build llama-server now (~20 min)
#   bash install.sh --selftest           check this script, install nothing
#
# ENVIRONMENT
#   CROW_HOME          install root, default ${XDG_DATA_HOME:-~/.local/share}/crow
#   CROW_MODELS        model root; --models writes it into $CROW_HOME/env
#   CROW_REF           branch or tag to fetch when there is no checkout (main)
#   CROW_BUILD_ENGINE  1 is --build-engine
# ---------------------------------------------------------------------------

set -euo pipefail

# --- what this machine has to bring ----------------------------------------
# The numbers are install.ps1's, and they are the same numbers because they
# describe the same operating point on the same card. 16000 is the floor below
# which nothing was ever measured; 32000 is the profile every figure in
# README.md was taken at.
VRAM_SUPPORTED_MB=16000
VRAM_TARGET_MB=32000
# 60 and not 64: this machine reports 62 GiB of a nominal 64, because firmware
# and hardware reservations come off the top before the kernel sees the memory.
# A threshold at the nominal size would warn exactly the configuration every
# measurement was taken on. install.ps1 carries the same 60 for -ncmoe's L2.
RAM_WARN_GB=60
DISK_INSTALL_GB=2
DISK_MODEL_GB=85          # reported, never enforced -- the model is a later step
PY_MIN="3.9"              # measured: cli/crow_core.py uses str.removesuffix (3.9)

REPO_SLUG="nibor1896/Crow"
PACMAN_LINE="sudo pacman -S --needed python-gobject gtk3 webkit2gtk-4.1 wl-clipboard"

# The payload. Directories are copied whole minus the exclusions in
# payload_paths(); files are copied if they exist.
PAYLOAD_DIRS="cli manifests tools patches"
PAYLOAD_FILES="README.md LICENSE NOTICE CHANGELOG.md install.sh"

ICON_SIZES="16 24 32 48 64 128 256 512"

# --- output ----------------------------------------------------------------
if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[90m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; Z=$'\033[0m'
else B=""; D=""; G=""; Y=""; R=""; Z=""; fi

STEP=0
TOTAL_STEPS=5
step()  { STEP=$((STEP + 1)); printf '\n%s[%d/%d] %s%s\n' "$B" "$STEP" "$TOTAL_STEPS" "$*" "$Z"; }
ok()    { printf '  %sok%s    %s\n'   "$G" "$Z" "$*"; }
warn()  { printf '  %swarn%s  %s\n'   "$Y" "$Z" "$*"; }
bad()   { printf '  %sno%s    %s\n'   "$R" "$Z" "$*"; }
note()  { printf '        %s%s%s\n'   "$D" "$*" "$Z"; }
cmd()   { printf '    %s\n' "$*"; }
die()   { printf '\n%serror:%s %s\n' "$R" "$Z" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Pure helpers. Everything the selftest drives lives here, and everything here
# is a function of its arguments -- install.ps1's rule, for install.ps1's
# reason: a check that can only be run by performing the install is a check
# nobody runs.
# ---------------------------------------------------------------------------

# The version literal, read from the one place that owns it.
# tools/check_operating_point.py holds cli/crow.py, install.ps1 and the README
# badge against the manifest. This script deliberately carries NO copy of the
# number: a fourth place nobody checks is a fourth place that drifts.
version_from_crow_py() {
    [ -f "$1" ] || return 0
    # awk and not `sed | head`: `pipefail` is on, and a producer that is still
    # writing when head closes the pipe turns a correct answer into exit 141.
    awk '/^VERSION[ \t]*=[ \t]*"/ { s = $0
        sub(/^VERSION[ \t]*=[ \t]*"/, "", s); sub(/".*/, "", s); print s; exit }' "$1"
}

# Where to fetch from when there is no checkout. A ref that looks like a release
# is a TAG, anything else is a BRANCH -- the Linux port has no tag yet, so the
# default is a branch and the release path is exercised the day there is one.
# NOT the Windows release asset: crow-<version>-win-x64.zip carries llama-server.exe.
tarball_url() {
    local ref="$1"
    case "$ref" in
        v[0-9]*.[0-9]*.[0-9]*|[0-9]*.[0-9]*.[0-9]*)
            printf 'https://github.com/%s/archive/refs/tags/v%s.tar.gz\n' \
                   "$REPO_SLUG" "${ref#v}" ;;
        *)
            printf 'https://github.com/%s/archive/refs/heads/%s.tar.gz\n' \
                   "$REPO_SLUG" "$ref" ;;
    esac
}

# The three preflight verdicts, as pure functions so the selftest can drive the
# rejecting side of each without a machine that fails.
vram_verdict() {
    if   [ "$1" -lt "$VRAM_SUPPORTED_MB" ]; then echo fail
    elif [ "$1" -lt "$VRAM_TARGET_MB" ];    then echo warn
    else echo ok; fi
}
ram_verdict()  { [ "$1" -lt "$RAM_WARN_GB" ] && echo warn || echo ok; }
disk_verdict() {
    if   [ "$1" -lt "$DISK_INSTALL_GB" ]; then echo fail
    elif [ "$1" -lt $((DISK_INSTALL_GB + DISK_MODEL_GB)) ]; then echo warn
    else echo ok; fi
}

# The .desktop entry is a TEMPLATE and @CROW_LAUNCHER@ is its contract -- see the
# comment at the head of cli/crow.desktop. A template that does not carry the
# placeholder is a template that lost it, and writing it out unsubstituted would
# install a launcher entry whose Exec line is the literal string.
desktop_substitute() {
    local template="$1" launcher="$2"
    grep -q '@CROW_LAUNCHER@' "$template" \
        || { echo "no @CROW_LAUNCHER@ in $template" >&2; return 1; }
    # | as the separator: a path may contain / and never |.
    sed "s|@CROW_LAUNCHER@|$launcher|g" "$template"
}

sha_of() { sha256sum "$1" | cut -d' ' -f1; }

# An absolute path, WITHOUT asking the filesystem. The first version resolved
# the parent with `cd "$(dirname X)" && pwd`, which is correct for a directory
# that exists and silently wrong for one that does not: `--models
# /does/not/exist/models` became `/models`, because the failing `cd` was not
# what the assignment took its exit status from. A path the user names may
# legitimately not exist yet -- that is the ordinary case for a model root the
# download has not created -- so this is pure string work.
abspath() {
    case "$1" in
        /*)  printf '%s\n' "$1" ;;
        "~") printf '%s\n' "$HOME" ;;
        "~/"*) printf '%s\n' "$HOME/${1#\~/}" ;;
        *)   printf '%s\n' "$PWD/$1" ;;
    esac
}

# The files this package ships, as paths relative to ROOT, sorted.
#
# test_*.py IS NOT SHIPPED, and that is pack-release.ps1's rule rather than a
# new one: the suites are developer equipment, nothing outside the repository
# refers to them, and cli/test_crow_gui.py alone is 900 kB in every install.
# __pycache__ for the reason the Windows packager states: -Recurse once shipped
# a .pyc of the packaging machine's Python version.
payload_paths() {
    local root="$1" d f
    {
        for d in $PAYLOAD_DIRS; do
            [ -d "$root/$d" ] || continue
            ( cd "$root" && find "$d" -type f \
                ! -path '*/__pycache__/*' \
                ! -name '*.pyc' ! -name '*.pyo' \
                ! -name 'test_*.py' -print )
        done
        for f in $PAYLOAD_FILES; do
            # `if` and not `[ ... ] && printf`: under `set -e` a loop whose last
            # command is a false AND-list ends the subshell, and `pipefail` then
            # carries that 1 out through the sort.
            if [ -f "$root/$f" ]; then printf '%s\n' "$f"; fi
        done
    } | LC_ALL=C sort
}

# `<sha256>  <path>` per file, i.e. sha256sum's own format, so a user who wants
# a second opinion can run `sha256sum -c manifest.sha256` and get one.
manifest_of() {
    local root="$1" rel
    payload_paths "$root" | while IFS= read -r rel; do
        printf '%s  %s\n' "$(sha_of "$root/$rel")" "$rel"
    done
}

# What happened to the files the last run installed, asked BEFORE this run
# overwrites them. Prints one `<state> <path>` line per file that is not as the
# manifest left it; silent when nothing moved.
manifest_audit() {
    local root="$1" manifest="$2" want rel
    [ -f "$manifest" ] || return 0
    while read -r want rel; do
        [ -n "${rel:-}" ] || continue
        if [ ! -f "$root/$rel" ]; then printf 'missing %s\n' "$rel"
        elif [ "$(sha_of "$root/$rel")" != "$want" ]; then printf 'changed %s\n' "$rel"
        fi
    done < "$manifest"
    return 0
}

# Which files did the PREVIOUS package install that this one no longer ships?
# The direction is install.ps1's Find-DroppedFiles and it is the whole design:
# asked this way the answer is exact and needs no exception list, so a file the
# user put in $CROW_HOME can never be selected -- it was never in a manifest.
manifest_dropped() {
    local prev="$1" next="$2"
    LC_ALL=C comm -23 \
        <(awk '{print $2}' "$prev" | LC_ALL=C sort -u) \
        <(awk '{print $2}' "$next" | LC_ALL=C sort -u)
}

# What this package brings that the last one did not: "<added> <changed>".
# install.ps1 reports unchanged / changed / new / removed, and the count is what
# turns "161 files copied" into a sentence about this run rather than about the
# size of the package.
manifest_changes() {
    awk 'NR == FNR { seen[$2] = $1; next }
         { if (!($2 in seen)) added++; else if (seen[$2] != $1) changed++ }
         END { printf "%d %d\n", added + 0, changed + 0 }' "$1" "$2"
}

# ---------------------------------------------------------------------------
# Selftest -- checks that must pass and checks that must fail. Downloads
# nothing, writes only into its own temp directory.
# ---------------------------------------------------------------------------
selftest() {
    local pass=0 fail=0 tmp
    check() {  # check <description> <verdict 0|1> [detail]
        if [ "$2" -eq 0 ]; then pass=$((pass + 1)); printf '  %sOK%s      %s\n' "$G" "$Z" "$1"
        else fail=$((fail + 1)); printf '  %sFAILED%s  %s\n      %s\n' "$R" "$Z" "$1" "${3:-}"; fi
    }
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN

    printf '%sinstall.sh selftest%s\n\n' "$B" "$Z"

    # 1-2. The version literal. The regex has to find the real one and has to
    # find nothing in a file that does not declare one -- a regex that answers
    # something for every input would silently tag a release "".
    printf 'VERSION = "9.9.9"\n' > "$tmp/crow.py"
    local v; v="$(version_from_crow_py "$tmp/crow.py")"
    check "the version regex reads the literal out of cli/crow.py" \
          "$([ "$v" = "9.9.9" ] && echo 0 || echo 1)" "got '$v'"
    printf '# no version here\nVERSIONS = ["1.2.3"]\n' > "$tmp/noversion.py"
    v="$(version_from_crow_py "$tmp/noversion.py")"
    check "NEGATIVE: a file without the literal yields nothing, not a guess" \
          "$([ -z "$v" ] && echo 0 || echo 1)" "got '$v'"
    # And against the real file, which is the one that matters.
    if [ -n "$REPO" ]; then
        v="$(version_from_crow_py "$REPO/cli/crow.py")"
        check "the regex finds this repository's own version ($v)" \
              "$(printf '%s' "$v" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' && echo 0 || echo 1)" \
              "got '$v'"
    fi

    # 3-4. The source URL. A release ref is a tag, anything else is a branch.
    check "a release ref resolves to the tag tarball" \
          "$([ "$(tarball_url 2.1.0)" = "https://github.com/$REPO_SLUG/archive/refs/tags/v2.1.0.tar.gz" ] && echo 0 || echo 1)" \
          "$(tarball_url 2.1.0)"
    check "a branch ref resolves to the branch tarball" \
          "$([ "$(tarball_url linux)" = "https://github.com/$REPO_SLUG/archive/refs/heads/linux.tar.gz" ] && echo 0 || echo 1)" \
          "$(tarball_url linux)"

    # 5-7. The preflight verdicts, each driven over its own boundary.
    check "VRAM: 8 GB refused, 24 GB warned, 32 GB accepted" \
          "$([ "$(vram_verdict 8000)" = fail ] && [ "$(vram_verdict 24000)" = warn ] \
             && [ "$(vram_verdict 32607)" = ok ] && echo 0 || echo 1)" \
          "$(vram_verdict 8000)/$(vram_verdict 24000)/$(vram_verdict 32607)"
    check "RAM: 62 GB is fine, 48 GB warns" \
          "$([ "$(ram_verdict 62)" = ok ] && [ "$(ram_verdict 48)" = warn ] && echo 0 || echo 1)" \
          "$(ram_verdict 62)/$(ram_verdict 48)"
    check "disk: 1 GB refused, 20 GB warned, 200 GB accepted" \
          "$([ "$(disk_verdict 1)" = fail ] && [ "$(disk_verdict 20)" = warn ] \
             && [ "$(disk_verdict 200)" = ok ] && echo 0 || echo 1)" \
          "$(disk_verdict 1)/$(disk_verdict 20)/$(disk_verdict 200)"

    # 8-9. The desktop substitution, both ways.
    printf '[Desktop Entry]\nExec=@CROW_LAUNCHER@\nIcon=crow\n' > "$tmp/t.desktop"
    local out; out="$(desktop_substitute "$tmp/t.desktop" "/opt/x y/crow")"
    check "the .desktop template takes the launcher path" \
          "$(printf '%s' "$out" | grep -qxF 'Exec=/opt/x y/crow' && echo 0 || echo 1)" "$out"
    printf '[Desktop Entry]\nExec=/usr/bin/crow\n' > "$tmp/bad.desktop"
    check "NEGATIVE: a template that lost @CROW_LAUNCHER@ is refused, not shipped" \
          "$(desktop_substitute "$tmp/bad.desktop" /x >/dev/null 2>&1 && echo 1 || echo 0)"

    # A path the user names may not exist yet, and that must not change it.
    check "an absolute path survives, a relative one is anchored, ~ is expanded" \
          "$([ "$(abspath /does/not/exist/models)" = "/does/not/exist/models" ] \
             && [ "$(abspath "~/m")" = "$HOME/m" ] \
             && [ "$(abspath m)" = "$PWD/m" ] && echo 0 || echo 1)" \
          "$(abspath /does/not/exist/models) | $(abspath "~/m") | $(abspath m)"

    # 10-13. The sha256 manifest, round-tripped in a temp tree: it has to be
    # silent about an untouched install, and it has to name a file that was
    # edited, a file that was deleted, and a file the new payload dropped.
    # Silence is the interesting half -- a manifest that reports drift on a
    # clean install is one nobody reads by the third run.
    mkdir -p "$tmp/src/cli" "$tmp/dst"
    printf 'print(1)\n' > "$tmp/src/cli/crow.py"
    printf 'print(2)\n' > "$tmp/src/cli/crow_gui.py"
    printf 'x\n'        > "$tmp/src/LICENSE"
    printf 'print(3)\n' > "$tmp/src/cli/test_crow.py"      # must not ship
    ( cd "$tmp/src" && tar cf - . ) | ( cd "$tmp/dst" && tar xf - )
    rm -f "$tmp/dst/cli/test_crow.py"
    manifest_of "$tmp/src" > "$tmp/dst/manifest.sha256"
    check "the manifest is silent about an install nobody touched" \
          "$([ -z "$(manifest_audit "$tmp/dst" "$tmp/dst/manifest.sha256")" ] && echo 0 || echo 1)" \
          "$(manifest_audit "$tmp/dst" "$tmp/dst/manifest.sha256")"
    check "test_*.py is not in the payload" \
          "$(grep -q 'test_crow.py' "$tmp/dst/manifest.sha256" && echo 1 || echo 0)"
    printf 'print(99)\n' > "$tmp/dst/cli/crow_gui.py"
    rm -f "$tmp/dst/LICENSE"
    out="$(manifest_audit "$tmp/dst" "$tmp/dst/manifest.sha256")"
    check "an edited file reads 'changed' and a deleted one 'missing'" \
          "$(printf '%s' "$out" | grep -q '^changed cli/crow_gui.py$' \
             && printf '%s' "$out" | grep -q '^missing LICENSE$' && echo 0 || echo 1)" "$out"
    rm -f "$tmp/src/cli/crow_gui.py"
    manifest_of "$tmp/src" > "$tmp/next.sha256"
    out="$(manifest_dropped "$tmp/dst/manifest.sha256" "$tmp/next.sha256")"
    check "a file the new payload no longer ships is reported dropped" \
          "$([ "$out" = "cli/crow_gui.py" ] && echo 0 || echo 1)" "$out"

    out="$(manifest_changes "$tmp/dst/manifest.sha256" "$tmp/next.sha256")"
    check "a package that dropped one file and changed none reads '0 0'" \
          "$([ "$out" = "0 0" ] && echo 0 || echo 1)" "$out"
    printf 'print(4)\n' > "$tmp/src/cli/crow.py"
    printf 'new\n'      > "$tmp/src/cli/crow_extra.py"
    manifest_of "$tmp/src" > "$tmp/next2.sha256"
    out="$(manifest_changes "$tmp/dst/manifest.sha256" "$tmp/next2.sha256")"
    check "one new file and one edited file read '1 1'" \
          "$([ "$out" = "1 1" ] && echo 0 || echo 1)" "$out"

    printf '\n%s%d checks, %d failed%s\n' "$B" "$((pass + fail))" "$fail" "$Z"
    [ "$fail" -eq 0 ] || return 1
    printf 'RESULT: PASS\n'
}

# ---------------------------------------------------------------------------
# 1 -- the machine
# ---------------------------------------------------------------------------
preflight() {
    step "Checking this machine"
    local problems=0

    # Python. BOTH clients need it; the terminal one needs nothing else.
    if [ -z "$PY" ]; then
        bad "no python3 on the PATH. Both clients are Python"
        problems=$((problems + 1))
    else
        local pv; pv="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
        if [ "$("$PY" -c 'import sys;print(int(sys.version_info[:2]>=(3,9)))')" = 1 ]; then
            ok "python $pv at $PY"
        else
            bad "python $pv, and $PY_MIN or newer is required"
            problems=$((problems + 1))
        fi
        if "$PY" -c 'import venv' 2>/dev/null; then
            ok "venv module present"
        else
            bad "python has no venv module (Debian/Ubuntu: python3-venv)"
            problems=$((problems + 1))
        fi
    fi

    # The window's half of the promise, and it is a WARNING rather than a
    # refusal for install.ps1's reason: a missing runtime costs one client, not
    # the install. The terminal client runs on the standard library alone.
    #
    # WE CANNOT INSTALL IT AND DO NOT PRETEND TO. PyGObject is not a wheel that
    # pip can build here without the GObject headers, and the headers come from
    # the distribution. So the line is printed; typing it is the user's.
    if [ -n "$PY" ] && "$PY" - <<'EOF' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2  # noqa: F401
EOF
    then
        ok "PyGObject with Gtk 3.0 and WebKit2 4.1 -- the window can render"
    else
        warn "no PyGObject / Gtk 3.0 / WebKit2 4.1. cli/crow.py runs in a terminal without"
        note "them; cli/crow_gui.py is the window and renders in WebKitGTK. Install them"
        note "with your package manager -- this script never asks for a root password:"
        cmd "$PACMAN_LINE"
    fi

    # Clipboard. crow_platform reaches for wl-paste/wl-copy first and falls back
    # to xclip under X11; without either, pasting an image into the window is
    # the one thing that stops working.
    if command -v wl-paste >/dev/null 2>&1; then ok "wl-clipboard ($(command -v wl-paste))"
    elif command -v xclip >/dev/null 2>&1;   then warn "no wl-clipboard; xclip found -- fine under X11, not under Wayland"
    else warn "no wl-clipboard and no xclip: pasting an image into the window will not work"
    fi

    # The card. This one CAN refuse the machine, and it is asked before a byte
    # is downloaded.
    if command -v nvidia-smi >/dev/null 2>&1; then
        local line name vram
        line="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || true)"
        name="${line%%,*}"; vram="${line##*,}"; vram="${vram// /}"
        case "$vram" in ''|*[!0-9]*) vram=0 ;; esac
        VRAM_MB="$vram"
        case "$(vram_verdict "$vram")" in
            fail) bad "$name, $vram MB of VRAM -- below the $VRAM_SUPPORTED_MB MB minimum. Operation below that was never measured"
                  problems=$((problems + 1)) ;;
            warn) warn "$name, $vram MB of VRAM. The measured profile is $VRAM_TARGET_MB MB; expect fewer slots and less throughput" ;;
            ok)   ok "$name, $vram MB of VRAM" ;;
        esac
    else
        bad "no nvidia-smi on the PATH. Crow runs its experts on CUDA"
        problems=$((problems + 1))
    fi

    # RAM. -ncmoe 31 (30 on Windows) keeps the experts of 31 of 48 layers in system memory, so
    # this is not a comfort figure; it is where the operating point lives.
    local ramgb; ramgb="$(awk '/^MemTotal:/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "$(ram_verdict "$ramgb")" = ok ]; then ok "$ramgb GB of system RAM"
    else warn "$ramgb GB of system RAM. Flash-Next wants 64; below 60 nothing has been run"; fi

    # Disk, on the filesystem the install will land on.
    local target diskgb
    target="$CROW_HOME"; while [ ! -d "$target" ] && [ "$target" != "/" ]; do target="$(dirname "$target")"; done
    diskgb="$(df -Pk "$target" | awk 'NR==2 {printf "%d", $4/1024/1024}')"
    case "$(disk_verdict "$diskgb")" in
        fail) bad "$diskgb GB free on $target, $DISK_INSTALL_GB GB needed for the package"
              problems=$((problems + 1)) ;;
        warn) warn "$diskgb GB free on $target. The package fits; the model needs about $DISK_MODEL_GB GB more" ;;
        ok)   ok "$diskgb GB free on $target" ;;
    esac

    # The engine is a build here, not a download, and the build needs a compiler.
    if command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1; then
        ok "a C compiler for tools/build-llama-server.sh"
    else
        warn "no cc/gcc: tools/build-llama-server.sh cannot build the CUDA engine here"
    fi

    [ "$problems" -eq 0 ] || die "$problems thing(s) above have to be fixed first. Nothing was installed."
}

# ---------------------------------------------------------------------------
# 2 -- the source: this checkout, or the tarball of a ref
# ---------------------------------------------------------------------------
resolve_source() {
    step "Getting the files"
    if [ -n "$REPO" ]; then
        SOURCE="$REPO"
        SOURCE_TMP=""
        VERSION="$(version_from_crow_py "$REPO/cli/crow.py")"
        ok "from this checkout: $REPO (version ${VERSION:-unknown})"
        return
    fi
    command -v curl >/dev/null 2>&1 || die "no curl, and there is no checkout to install from"
    local url; url="$(tarball_url "$CROW_REF")"
    SOURCE_TMP="$(mktemp -d)"
    ok "no checkout here -- fetching $CROW_REF"
    note "$url"
    curl -fsSL "$url" -o "$SOURCE_TMP/crow.tar.gz" \
        || die "could not download $url"
    tar -xzf "$SOURCE_TMP/crow.tar.gz" -C "$SOURCE_TMP"
    # GitHub's archive wraps everything in Crow-<ref>/.
    SOURCE="$(find "$SOURCE_TMP" -mindepth 1 -maxdepth 1 -type d | sed -n 1p)"
    [ -f "$SOURCE/cli/crow.py" ] || die "the tarball does not look like Crow: no cli/crow.py"
    VERSION="$(version_from_crow_py "$SOURCE/cli/crow.py")"
    ok "unpacked version ${VERSION:-unknown}"
}

# ---------------------------------------------------------------------------
# 3 -- the files, and a per-file sha256 that says what moved
# ---------------------------------------------------------------------------
install_payload() {
    step "Installing into $CROW_HOME"
    mkdir -p "$CROW_HOME"
    local manifest="$CROW_HOME/manifest.sha256" audit rel dropped n

    # BEFORE overwriting anything: what happened to the last install?
    if [ -f "$manifest" ]; then
        audit="$(manifest_audit "$CROW_HOME" "$manifest" || true)"
        if [ -z "$audit" ]; then
            ok "the previous install is exactly as it was left ($(wc -l < "$manifest") files)"
        else
            warn "$(printf '%s\n' "$audit" | wc -l) file(s) differ from the last install and are being replaced:"
            printf '%s\n' "$audit" | awk 'NR <= 10' | while read -r state rel; do note "$state  $rel"; done
        fi
    fi

    payload_paths "$SOURCE" > "$CROW_HOME/.manifest.paths.$$"
    n=0
    while IFS= read -r rel; do
        mkdir -p "$CROW_HOME/$(dirname "$rel")"
        cp -p "$SOURCE/$rel" "$CROW_HOME/$rel"
        n=$((n + 1))
    done < "$CROW_HOME/.manifest.paths.$$"
    rm -f "$CROW_HOME/.manifest.paths.$$"
    ok "$n files copied"

    manifest_of "$SOURCE" > "$CROW_HOME/manifest.sha256.new"
    if [ -f "$manifest" ]; then
        set -- $(manifest_changes "$manifest" "$CROW_HOME/manifest.sha256.new")
        if [ "$1" = 0 ] && [ "$2" = 0 ]; then ok "the same bytes as the last run -- nothing in the package moved"
        else ok "$1 new file(s), $2 changed since the last run"; fi
        dropped="$(manifest_dropped "$manifest" "$CROW_HOME/manifest.sha256.new" || true)"
        if [ -n "$dropped" ]; then
            printf '%s\n' "$dropped" | while IFS= read -r rel; do
                [ -n "$rel" ] && rm -f "$CROW_HOME/$rel"
            done
            ok "$(printf '%s\n' "$dropped" | wc -l) file(s) the new package no longer ships were removed"
        fi
    fi
    mv "$CROW_HOME/manifest.sha256.new" "$manifest"

    # Read back rather than trusted. install.ps1 verifies every file against the
    # package manifest after extracting, and a copy that silently truncated is
    # exactly the failure that installs cleanly and breaks at run time.
    audit="$(manifest_audit "$CROW_HOME" "$manifest" || true)"
    [ -z "$audit" ] || die "verification failed right after copying: $audit"
    ok "sha256 verified, all $n files"
}

# ---------------------------------------------------------------------------
# 4 -- the runtime: venv, engine, launcher, desktop
# ---------------------------------------------------------------------------
install_venv() {
    # --system-site-packages IS THE POINT AND NOT A CONVENIENCE. PyGObject comes
    # from the distribution (it needs the GObject headers to build), and a venv
    # that cannot see it cannot render the window at all. pywebview is installed
    # PLAIN and never as pywebview[gtk]: the extra pulls in a PyGObject wheel
    # that then shadows the system one with a build that has no typelibs.
    if [ ! -x "$CROW_HOME/venv/bin/python" ]; then
        "$PY" -m venv --system-site-packages "$CROW_HOME/venv" \
            || die "could not create the venv at $CROW_HOME/venv"
        ok "venv created at $CROW_HOME/venv"
    else
        ok "venv reused at $CROW_HOME/venv"
    fi
    "$CROW_HOME/venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    if "$CROW_HOME/venv/bin/python" -m pip install --quiet --upgrade pywebview; then
        ok "pywebview $("$CROW_HOME/venv/bin/python" -m pip show pywebview 2>/dev/null | sed -n 's/^Version: //p')"
    else
        warn "pip could not install pywebview. The terminal client is unaffected:"
        cmd "$PY $CROW_HOME/cli/crow.py"
    fi
    if [ "$WITH_VOICE" = 1 ]; then
        if "$CROW_HOME/venv/bin/python" -m pip install --quiet --upgrade faster-whisper sounddevice; then
            ok "voice extra: faster-whisper + sounddevice"
            note "the dictation model (~486 MB) is fetched by the window on the first click"
        else
            warn "the voice extra did not install; everything else is fine"
        fi
    fi
}

install_engine() {
    local builder="$CROW_HOME/tools/build-llama-server.sh"
    if [ -x "$CROW_HOME/bin/llama-server" ] || [ -f "$CROW_HOME/bin/llama-server" ]; then
        ok "llama-server is already at $CROW_HOME/bin/llama-server"
        return
    fi
    if [ "$BUILD_ENGINE" = 1 ]; then
        ok "building llama-server -- this takes about 20 minutes"
        CROW_HOME="$CROW_HOME" bash "$builder" \
            || die "tools/build-llama-server.sh failed. Its output above says where."
        return
    fi
    warn "no engine at $CROW_HOME/bin/llama-server. Nothing can be served until there is one."
    note "It is built here, from llama.cpp pin 6c84c7d5d + PR #27880 + PR #28040, with a"
    note "CUDA toolkit unpacked under \$CROW_HOME -- no root, ~20 minutes, ~8 GB:"
    cmd "CROW_HOME=$CROW_HOME bash $builder"
}

write_launcher() {
    mkdir -p "$CROW_HOME/bin"
    # The launcher is GENERATED and says so, because the two things it decides --
    # which interpreter and which model root -- are decided at install time and
    # a user who edits them here loses them on the next run. $CROW_HOME/env is
    # the file that survives.
    cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Crow's window. Written by install.sh -- edit $CROW_HOME/env, not this file.
set -euo pipefail
CROW_HOME="$CROW_HOME"
[ -r "\$CROW_HOME/env" ] && . "\$CROW_HOME/env"
exec "\$CROW_HOME/venv/bin/python" "\$CROW_HOME/cli/crow_gui.py" "\$@"
EOF
    chmod +x "$LAUNCHER"
    ok "launcher at $LAUNCHER"

    # $CROW_MODELS and nowhere else. crow_platform.models_dir() reads exactly
    # this variable, and the comment there is the reason there is no second copy
    # in a settings file: a path two places can set is a path nobody can find.
    cat > "$CROW_HOME/env" <<EOF
# Sourced by $LAUNCHER before the window starts. Written by install.sh.
# Change the model root here, or re-run install.sh --models DIR.
export CROW_MODELS="$MODELS_DIR"
EOF
    ok "model root: $MODELS_DIR  (\$CROW_HOME/env)"
    [ -d "$MODELS_DIR" ] || note "it does not exist yet -- step 5 prints what to download into it"

    # ~/.local/bin only if it is already on the PATH. Putting a directory on
    # somebody's PATH is editing their shell profile, and this script does not
    # edit files it did not write.
    case ":$PATH:" in
        *":$HOME/.local/bin:"*)
            mkdir -p "$HOME/.local/bin"
            # THE LAST INSTALL WINS, AND IT SAYS SO. `--to DIR` and a throwaway
            # $CROW_HOME both come through here, so `crow` would otherwise
            # silently start pointing at whichever tree was installed last --
            # the failure being a shortcut into a directory somebody deleted.
            local was=""
            [ -L "$HOME/.local/bin/crow" ] && was="$(readlink "$HOME/.local/bin/crow")"
            ln -sf "$LAUNCHER" "$HOME/.local/bin/crow"
            if [ -n "$was" ] && [ "$was" != "$LAUNCHER" ]; then
                warn "\`crow\` on your PATH now means this install"
                note "~/.local/bin/crow  $was  ->  $LAUNCHER"
            else
                ok "\`crow\` on your PATH (~/.local/bin/crow -> $LAUNCHER)"
            fi ;;
        *)
            note "~/.local/bin is not on your PATH, so no \`crow\` shortcut was made."
            note "Add it, or start the window with the full path above." ;;
    esac
}

install_desktop() {
    [ "$WITH_DESKTOP" = 1 ] || { note "desktop integration skipped (--no-desktop)"; return; }
    local apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    local icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
    local src="$CROW_HOME/cli"

    if [ -f "$src/crow.desktop" ]; then
        mkdir -p "$apps"
        desktop_substitute "$src/crow.desktop" "$LAUNCHER" > "$apps/crow.desktop"
        chmod 644 "$apps/crow.desktop"
        ok "launcher entry at $apps/crow.desktop"
        # Captured rather than let loose: desktop-file-validate prints hints at
        # exit 0 (ours draws one for two main categories), and a hint landing
        # between two green lines reads like a failure that was ignored.
        if command -v desktop-file-validate >/dev/null 2>&1; then
            local said; said="$(desktop-file-validate "$apps/crow.desktop" 2>&1)" && {
                if [ -z "$said" ]; then ok "desktop-file-validate: clean"
                else ok "desktop-file-validate: valid, with a hint"
                     note "${said#*: }"; fi
            } || { warn "desktop-file-validate rejected the entry:"; note "$said"; }
        fi
    fi

    local n=0 size
    for size in $ICON_SIZES; do
        [ -f "$src/icons/crow-$size.png" ] || continue
        mkdir -p "$icons/${size}x${size}/apps"
        cp -p "$src/icons/crow-$size.png" "$icons/${size}x${size}/apps/crow.png"
        n=$((n + 1))
    done
    [ "$n" -gt 0 ] && ok "$n icons under $icons"
    # PNG and not SVG, and the .desktop file says why: this machine has no SVG
    # gdk-pixbuf loader, so a themed SVG icon cannot be opened at all.
    if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ "$n" -gt 0 ]; then
        if gtk-update-icon-cache -f -t "$icons" >/dev/null 2>&1; then
            ok "icon cache refreshed"
        else
            # Not a failure: a user icon directory with no index.theme has no
            # cache to refresh, and every launcher still reads the files.
            note "gtk-update-icon-cache found no theme index here -- the icons are still read"
        fi
    fi
}

install_hyprland() {
    [ "$WITH_DESKTOP" = 1 ] || return 0
    local hypr="${XDG_CONFIG_HOME:-$HOME/.config}/hypr"
    local src="$CROW_HOME/cli"
    # A FRAMELESS WINDOW WITH A HARD MINIMUM CANNOT ASK FOR ITS OWN FRAME. On
    # Wayland a client may not place, size or raise its toplevel; the rule is
    # how you talk to the compositor. See the head of cli/hyprland-crow.lua.
    #
    # THE USER'S OWN CONFIG IS NOT EDITED. One line has to be added by hand, and
    # it is printed rather than appended: a script that writes into hyprland.lua
    # is a script that has to parse it, and getting that wrong costs somebody
    # their session on the next reload.
    if [ -f "$hypr/hyprland.lua" ] && [ -f "$src/hyprland-crow.lua" ]; then
        cp -p "$src/hyprland-crow.lua" "$hypr/crow.lua"
        ok "Hyprland rule at $hypr/crow.lua (Lua config detected)"
        HYPR_LINE='require("hypr.crow")'
        HYPR_WHERE="$hypr/hyprland.lua"
    elif [ -f "$hypr/hyprland.conf" ] && [ -f "$src/hyprland-crow.conf" ]; then
        cp -p "$src/hyprland-crow.conf" "$hypr/crow.conf"
        ok "Hyprland rule at $hypr/crow.conf (ini config detected)"
        HYPR_LINE="source = $hypr/crow.conf"
        HYPR_WHERE="$hypr/hyprland.conf"
    else
        note "no Hyprland config found -- the window opens under any compositor, but"
        note "under a tiling one it will be tiled. cli/hyprland-crow.{lua,conf} has the rule."
    fi
}

# ---------------------------------------------------------------------------
# 5 -- what is left to do
# ---------------------------------------------------------------------------
# The by-hand server line is ASKED OF THE CORE, not written down here. README.md
# and install.ps1 carry copies of it and tools/check_operating_point.py holds
# both against manifests/operating-point.json; a third copy in this file would
# be a third thing to keep in step. crow_core.server_command() builds the argv
# from the manifest, so what is printed here cannot drift by construction.
resolved_server_line() {
    "$CROW_HOME/venv/bin/python" - "$CROW_HOME" "$MODELS_DIR" <<'EOF' 2>/dev/null || true
import os, shlex, sys
home, models = sys.argv[1], sys.argv[2]
os.environ.setdefault("CROW_MODELS", models)
sys.path.insert(0, os.path.join(home, "cli"))
try:
    import crow_core
    print(shlex.join(crow_core.server_command("flash-next-q2-k-xl", install=home)))
except Exception as exc:
    print("!%s" % exc)
EOF
}

final_screen() {
    step "What is left to do"
    local line

    printf '\n'
    printf '  1. The model. It is NOT part of this install: 73.45 GiB in 3 shards, plus\n'
    printf '     904,004,000 B for the vision projector, and it belongs to somebody else.\n\n'
    cmd "hf download unsloth/Qwen3.8-Flash-Next-GGUF --include '*UD-Q2_K_XL*' --local-dir $MODELS_DIR"
    cmd "hf download unsloth/Qwen3.8-Flash-Next-GGUF mmproj-F16.gguf --local-dir $MODELS_DIR"
    printf '\n'
    note "The second line is the projector and the glob of the first walks past it --"
    note "it sits in the repository root, above the quant folder. hf prints a tick even"
    note "when it reached nothing, so check the byte counts."
    printf '\n'

    printf '  2. Then open the window. It boots the server itself, from the model menu:\n\n'
    cmd "crow"
    printf '\n'
    note "or the full path, if ~/.local/bin is not on your PATH:"
    cmd "$LAUNCHER"
    printf '\n'

    line="$(resolved_server_line)"
    if [ -n "$line" ] && [ "${line#!}" = "$line" ]; then
        printf '     By hand, the same line the window would run:\n\n'
        cmd "$line"
        printf '\n'
    else
        printf '     By hand, once the model is on disk:\n\n'
        cmd "$PY $CROW_HOME/tools/start-server.py flash-next-q2-k-xl"
        printf '\n'
        [ -n "$line" ] && note "(not resolvable yet: ${line#!})"
        printf '\n'
    fi

    if [ -n "$HYPR_LINE" ]; then
        printf '  3. Hyprland floats the window once this line is in %s:\n\n' "$HYPR_WHERE"
        cmd "$HYPR_LINE"
        printf '\n'
        note "then, in a terminal:"
        cmd "hyprctl reload"
        printf '\n'
    fi

    printf '  %sPaths%s\n' "$B" "$Z"
    note "install    $CROW_HOME"
    note "models     $MODELS_DIR  (\$CROW_MODELS, from $CROW_HOME/env)"
    note "settings   ${XDG_CONFIG_HOME:-$HOME/.config}/crow"
    note "sessions   ${XDG_STATE_HOME:-$HOME/.local/state}/crow"
    note "boot logs  ${XDG_STATE_HOME:-$HOME/.local/state}/crow/log"
    printf '\n'
    note "The terminal client needs nothing but Python:  $PY $CROW_HOME/cli/crow.py"
    note "The Linux page -- paths, the float rule, the escape hatches, troubleshooting:"
    note "https://github.com/$REPO_SLUG/blob/main/docs/user-guide/linux.md"
    printf '\n'
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
usage() {
    sed -n '/^# USAGE/,/^# ---/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'
}

CROW_HOME="${CROW_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/crow}"
CROW_REF="${CROW_REF:-main}"
BUILD_ENGINE="${CROW_BUILD_ENGINE:-0}"
WITH_VOICE=0
WITH_DESKTOP=1
WITH_ENGINE=1
MODELS_ARG=""
SELFTEST=0
HYPR_LINE=""
HYPR_WHERE=""
VERSION=""
SOURCE=""
SOURCE_TMP=""
VRAM_MB=0

while [ $# -gt 0 ]; do
    case "$1" in
        --to)           CROW_HOME="$2"; shift 2 ;;
        --models)       MODELS_ARG="$2"; shift 2 ;;
        --ref)          CROW_REF="$2"; shift 2 ;;
        --voice)        WITH_VOICE=1; shift ;;
        --build-engine) BUILD_ENGINE=1; shift ;;
        --no-engine)    WITH_ENGINE=0; shift ;;
        --no-desktop)   WITH_DESKTOP=0; shift ;;
        --selftest)     SELFTEST=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option: $1  (--help lists them)" ;;
    esac
done

# Where this script is, IF it is in a checkout. Piped through bash there is no
# such path, and that is the branch that fetches a tarball instead.
REPO=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "$REPO/cli/crow.py" ] || REPO=""
fi

PY="$(command -v python3 || command -v python || true)"

if [ "$SELFTEST" = 1 ]; then
    selftest
    exit $?
fi

# The model root. Nothing is asked: the tree this machine already has wins, then
# the default beside the install. 80-110 GiB do not belong under ~/.local/share,
# which is why $CROW_MODELS exists at all.
if [ -n "$MODELS_ARG" ]; then
    MODELS_DIR="$(abspath "$MODELS_ARG")"
elif [ -n "${CROW_MODELS:-}" ]; then
    MODELS_DIR="$(abspath "$CROW_MODELS")"
elif [ -d "$HOME/Projects/models/qwen3.8-flash-next" ]; then
    MODELS_DIR="$HOME/Projects/models/qwen3.8-flash-next"
else
    MODELS_DIR="$CROW_HOME/models"
fi

CROW_HOME="$(abspath "$CROW_HOME")"
LAUNCHER="$CROW_HOME/bin/crow"

printf '\n%sCrow%s -- the window, the clients and the manifest, under %s\n' "$B" "$Z" "$CROW_HOME"

preflight
resolve_source
install_payload

step "The runtime"
install_venv
[ "$WITH_ENGINE" = 1 ] && install_engine || note "engine check skipped (--no-engine)"
write_launcher
install_desktop
install_hyprland

final_screen

[ -n "$SOURCE_TMP" ] && rm -rf "$SOURCE_TMP"
exit 0
