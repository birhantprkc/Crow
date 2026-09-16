<#
verify-patch-builds - do the group patches BUILD and RUN on their own?

WHY THIS EXISTS: verify-patch-groups.sh proves the split reconstructs the tree.
That is not the same as each piece standing on its own. E3 asked for "builds alone",
and the answer was NO until measured: token-timing alone failed with

    common/sampling.cpp(622): error C3861: "llama_moe_stream_print_stats"

because the plan had put a group (i) line into group (ii). A reconstruction check
cannot find that. Only a compiler can.

WHAT IT DOES: for each patch combination, resets a b10223 worktree to pristine,
applies with --3way only, builds llama-server, runs --help, and counts the
statistics symbol in the freshly built sampling.cpp.obj.

THE WORKTREE IS THE POINT. It runs in a throwaway `git worktree` on b10223, never
in crow-lab/src, so a failed run cannot leave the production tree half reset. Create
it with:  git worktree add --detach <path> b10223

GGML_CUDA=OFF: no path of ours carries CUDA code - the only /cuda/ match across all
23 is a comment in llama-moe-stream.cpp:551. Every translation unit we touch is
still compiled and linked, and --help needs no GPU. That the tree builds with the
CUDA backend ON is NOT proven here; that is E8 on the target tag.

WHY THE OBJECT IS INSPECTED: the linker only complains about a MISSING symbol. A
call sitting in dead code links and prints nothing. So the symbol is counted in the
object file, and the counting is only trustworthy because of its negative control:
with moe-stream alone it must be 1, with token-timing alone it must be 0. Measured
2026-08-05 - 1 against 0, at 974408 B against 992942 B, i.e. genuinely rebuilt.
An earlier version of this check reported "referenced" for all four runs at one
identical size, because a broken function carried the first result forward. If both
cases ever report the same count, the check is broken, not the patches.

NEVER PIPE A BUILD. `cmake --build ... | tail` once reported exit 0 while cmake was
not even found. Exit codes here come from $LASTEXITCODE directly.

Usage:
  verify-patch-builds.ps1 -WT <b10223-worktree> [-Configure]

  -Configure runs the one-time cmake configure step. Measured on this machine:
  configure 7.4 s, first llama-server build 97.5 s, incremental 25-26 s.

Exit 0 = every case green.  1 = at least one red.  2 = setup error.
#>
param(
    [string]$WT       = 'C:\Users\robin\dev\crow-lab\wt-e3',
    [string]$SRC      = 'C:\Users\robin\dev\crow-lab\src',
    [string]$PATCHDIR = 'C:\Users\robin\dev\Crow\patches',
    [string]$CROW     = 'C:\Users\robin\dev\Crow',
    [switch]$Configure,
    [string]$LogDir
)

$ErrorActionPreference = 'Continue'
if (-not $LogDir) { $LogDir = Join-Path $env:TEMP 'verify-patch-builds' }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$BUILD = Join-Path $WT 'build-e3'
$EXE   = Join-Path $BUILD 'bin\Release\llama-server.exe'
$OBJ   = Join-Path $BUILD 'common\CMakeFiles\llama-common.dir\Release\sampling.cpp.obj'
$M     = Join-Path $PATCHDIR 'moe-stream-on-b10223.patch'
$T     = Join-Path $PATCHDIR 'token-timing-on-b10223.patch'
$D     = Join-Path $PATCHDIR 'dflash-on-b10223.patch'
$NEW   = @('src\llama-moe-stream.cpp', 'src\llama-moe-stream.h', 'tests\test-llama-file.cpp') |
         ForEach-Object { Join-Path $WT $_ }
$STATS     = 'llama_moe_stream_print_stats'
$DEVIATION = 'common/sampling.cpp'

function Say($m) { Write-Output ("[{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $m) }
function Secs($a, $b) { [math]::Round(($b - $a).TotalSeconds, 1) }

if (-not (Test-Path (Join-Path $WT '.git'))) { Say "SETUP ERROR: no worktree at $WT"; exit 2 }
foreach ($p in @($M, $T, $D)) { if (-not (Test-Path $p)) { Say "SETUP ERROR: missing $p"; exit 2 } }

# --- toolchain: cmake is in NO PATH, it comes out of Visual Studio ----------
if ($null -eq (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) { Say 'SETUP ERROR: no vswhere'; exit 2 }
    $vs = & $vswhere -latest -property installationPath 2>$null | Select-Object -First 1
    cmd.exe /c "`"$(Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat')`" && set" 2>$null | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
    }
    $env:PATH = (Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin') + ';' +
                (Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja') + ';' + $env:PATH
}
if ($null -eq (Get-Command cmake.exe -ErrorAction SilentlyContinue)) { Say 'SETUP ERROR: no cmake'; exit 2 }

if ($Configure) {
    $t = Get-Date
    Set-Location $WT
    & cmake.exe -S . -B build-e3 -G 'Ninja Multi-Config' `
        -DGGML_CUDA=OFF -DGGML_NATIVE=OFF -DGGML_CPU_ALL_VARIANTS=OFF `
        -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TOOLS=ON `
        -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_CURL=OFF `
        *> (Join-Path $LogDir 'configure.log')
    if ($LASTEXITCODE -ne 0) { Say 'SETUP ERROR: configure failed'; exit 2 }
    Say "configure: $(Secs $t (Get-Date)) s"
}
if (-not (Test-Path (Join-Path $BUILD 'CMakeCache.txt'))) { Say 'SETUP ERROR: not configured, use -Configure'; exit 2 }

$ALL = (& git -C $CROW show 'HEAD:patches/worktree-on-b10223.patch') |
       Select-String '^diff --git' | ForEach-Object { $_.Line -replace '^diff --git a/', '' -replace ' b/.*$', '' }

function Reset-Pristine {
    Set-Location $WT
    & git reset -q --hard HEAD
    foreach ($n in $NEW) { [IO.File]::Delete($n) }
    return (& git status --porcelain | Where-Object { $_ -notmatch 'build-e3' } | Measure-Object -Line).Lines
}
function Hash-NoCR($p) {
    $b = [byte[]]([IO.File]::ReadAllBytes($p) | Where-Object { $_ -ne 13 })
    [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($b))
}

# Each case names the paths NOT to compare. A run that applies only two of three
# patches must not be measured against the third - that reads as a finding and is
# an expectation error. A single group cannot reconstruct the tree at all.
$cases = @(
    @{ n = 'moe-stream alone';             patches = @($M);         compare = $false; wantSym = 1 },
    @{ n = 'token-timing alone';           patches = @($T);         compare = $false; wantSym = 0 },
    @{ n = 'moe-stream + token-timing';    patches = @($M, $T);     compare = $true;  skip = @('src/models/dflash.cpp'); wantSym = 1 },
    @{ n = 'token-timing + moe-stream';    patches = @($T, $M);     compare = $true;  skip = @('src/models/dflash.cpp'); wantSym = 1 },
    @{ n = 'all three';                    patches = @($M, $T, $D); compare = $true;  skip = @();                        wantSym = 1 }
)

$rc = 0
$rows = @()
foreach ($c in $cases) {
    Say ('=' * 64)
    Say "CASE $($c.n)"
    $dirty = Reset-Pristine
    if ($dirty -ne 0) { Say "SETUP ERROR: worktree not pristine ($dirty paths)"; exit 2 }

    Set-Location $WT
    $fail = ''
    foreach ($p in $c.patches) {
        & git apply --3way $p 2>&1 | Out-File (Join-Path $LogDir 'apply.log') -Append -Encoding ascii
        if ($LASTEXITCODE -ne 0) { $fail += (Split-Path $p -Leaf) + ' ' }
    }
    $touched = (& git status --porcelain | Where-Object { $_ -notmatch 'build-e3' } | Measure-Object -Line).Lines
    $markers = 0
    foreach ($p in $ALL) {
        $f = Join-Path $WT ($p -replace '/', '\')
        if ((Test-Path $f) -and (Select-String -Path $f -Pattern '^<<<<<<<' -Quiet)) { $markers++ }
    }
    if ($fail -or $markers -ne 0) { $rc = 1 }
    Say "  applied $touched paths, markers $markers$(if ($fail) { ", FAILED: $fail" })"

    $rec = 'single group - not applicable'
    if ($c.compare) {
        $ok = 0; $want = 0; $bad = @()
        foreach ($p in $ALL) {
            if ($p -eq $DEVIATION -or $c.skip -contains $p) { continue }
            $want++
            $a = Join-Path $WT ($p -replace '/', '\')
            if (-not (Test-Path $a)) { $bad += "$p MISSING"; continue }
            if ((Hash-NoCR $a) -eq (Hash-NoCR (Join-Path $SRC ($p -replace '/', '\')))) { $ok++ } else { $bad += $p }
        }
        $rec = "$ok of $want byte-identical (excl. $DEVIATION)"
        if ($bad.Count) { $rec += " | deviations: $($bad -join ', ')"; $rc = 1 }
    }
    Say "  reconstruction: $rec"

    # Delete both first: a stale exe or obj would pass a check the compiler never wrote.
    [IO.File]::Delete($EXE)
    [IO.File]::Delete($OBJ)
    $tb = Get-Date
    Set-Location $WT
    & cmake.exe --build build-e3 --config Release --target llama-server `
        *> (Join-Path $LogDir ("build-" + ($c.n -replace '[^a-z]', '') + '.log'))
    $brc = $LASTEXITCODE
    $bs = Secs $tb (Get-Date)
    if ($brc -ne 0) { $rc = 1 }
    Say "  build: exit $brc in $bs s"

    $sym = -1; $objInfo = 'OBJECT MISSING'
    if ([IO.File]::Exists($OBJ)) {
        $fi = Get-Item $OBJ
        $sym = ([regex]::Matches([Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($OBJ)), $STATS)).Count
        $objInfo = "$($fi.Length) B, written $($fi.LastWriteTime.ToString('HH:mm:ss'))"
    }
    $srcCalls = 0
    $df = Join-Path $WT ($DEVIATION -replace '/', '\')
    if (Test-Path $df) { $srcCalls = (Select-String -Path $df -Pattern "$STATS\(llama_get_model" -AllMatches).Matches.Count }
    if ($sym -ne $c.wantSym) { $rc = 1 }
    Say "  $STATS : $srcCalls in source, $sym in obj (want $($c.wantSym)) | $objInfo"

    $opts = @(); $srk = 'n/a'
    if (Test-Path $EXE) {
        $out = & $EXE --help 2>&1
        $srk = $LASTEXITCODE
        if ($srk -ne 0) { $rc = 1 }
        $opts = [regex]::Matches(($out -join "`n"), '--moe-stream[a-z-]*') |
                ForEach-Object { $_.Value } | Sort-Object -Unique
    } else { $rc = 1 }
    Say "  smoke: --help exit $srk, distinct options $($opts.Count): $($opts -join ' ')"

    $rows += [pscustomobject]@{
        Case = $c.n; Paths = $touched; Markers = $markers; Rec = $rec; BuildRc = $brc
        BuildS = $bs; SymObj = $sym; WantSym = $c.wantSym; SmokeRc = $srk; Options = $opts.Count
    }
}

Reset-Pristine | Out-Null
Say ('=' * 64)
$rows | Format-Table Case, Paths, Markers, BuildRc, BuildS, SymObj, WantSym, SmokeRc, Options -AutoSize |
        Out-String -Width 200 | Write-Output
$rows | ConvertTo-Json -Depth 3 | Out-File (Join-Path $LogDir 'summary.json') -Encoding ascii

# The negative control, stated as a criterion rather than left to the reader: if
# moe-stream-alone and token-timing-alone report the SAME symbol count, this tool
# measured nothing.
$a = ($rows | Where-Object { $_.Case -eq 'moe-stream alone' }).SymObj
$b = ($rows | Where-Object { $_.Case -eq 'token-timing alone' }).SymObj
if ($a -eq $b) { Say "RESULT: red - symbol count identical ($a) in both controls; the check is broken"; exit 1 }
if ($rc -eq 0) { Say "RESULT: green - every case builds, runs, and carries the expected symbol count ($a vs $b)" }
else           { Say 'RESULT: red - see the lines above' }
exit $rc
