<#
verify-patch-b10269 - does the target-tag patch apply, build, test and CARRY ITS
FUNCTION under the real operational configuration?

WHY THIS EXISTS, and it is a measured failure, not a precaution. The 20-path state of
E7 built with exit 0 and zero error lines and had NO caller for
llama_moe_stream_print_stats: declaration present, definition present, 0 references in
the object. No build check found that. A count against the inventory did. Therefore:

    A GREEN BUILD DOES NOT PROVE THE FUNCTION IS THERE.

Everything below follows from that one sentence. Every positive probe here has a
negative probe, and if both report the same thing the CHECKER is broken, not the
patch - that has already happened once (see NEGATIVE PROBE below).

WHY NOT AN EXTENSION OF verify-patch-builds.ps1: that tool measures five patch
COMBINATIONS on b10223 and reconstructs them against the living tree in crow-lab/src.
Here there is one patch, a different base, a different flag inventory, and no living
tree to reconstruct against - crow-lab/src sits on b10223. The two case matrices are
disjoint; only the toolchain bootstrap is shared, and duplicating ~12 lines is cheaper
than putting a base switch through a tool that is proven as it stands.

WHAT IT CHECKS, in order:
   1  pristine base restored, worktree verifiably clean       (git reset --hard, never
                                                               checkout -- : --3way
                                                               writes to the INDEX)
   2  flag guard on the pristine header
   3  git apply --3way, exit code kept
   4  exactly $ExpectPaths touched paths
   5  zero conflict markers, CRLF-tolerant
   6  flag guard on the patched header
   7  TENSOR_ALLOW_RESHAPE = 1 << 4 and TENSOR_STREAMED = 1 << 5, each exactly once
   8  exactly one statistics call in common/sampling.cpp
   8a exactly one server call per role, the drafter call guarded by ctx_dft, and every
       statistics line carrying the role placeholder - plus the same three counts read on
       the PRISTINE base, where they must all be zero
   8b patch inventory split into own and foreign files, both counted
   9  configure: exit code, duration, CUDA detection out of the log, cache drift
       against the reference cache of the operational build
  10  scheduled build edges from a ninja dry run - measured, never assumed
  11  build: artifacts DELETED first, exit code, duration, error lines, static_assert
  12  freshness of every artifact by timestamp and size
  13  ctest: process exit code SEPARATELY from the counted ok lines
  14  smoke: --help exit code and four distinct --moe-stream* options
  15  exactly one reference in the freshly built sampling.cpp.obj, and two in the freshly
       built server-context.cpp.obj
  16  NEGATIVE PROBE on the pristine base: the same counters must report ZERO
  17  the negative probe RAN. A skipped probe left -1 behind and -1 is an initial value, not
       a measurement; it is now named as such and is red whenever the probe was due.

THE NEGATIVE PROBE IS THE POINT. The linker only complains about a MISSING symbol; a
call in dead code links and says nothing. So the symbol is counted in the object - and
that count is worth something only because the pristine base must produce 0 at a
demonstrably different object size and timestamp. On 2026-08-05 a broken PowerShell
function carried the first result forward and reported the same object size for four
runs, including a reference where none could exist. Hence: every counter here is a
pure function that re-reads its bytes, and identical positive/negative results are a
red result of this tool, not a finding about the patch.

TRAPS THIS TOOL IS BUILT AROUND, all of them measured:
  - a pipe swallows the return value. cmake --build ... | tail once reported exit 0
    while cmake was not even found. Nothing here is piped; every exit code comes from
    $LASTEXITCODE right after the call, output goes to a file via *>.
  - Measure-Object -Line counts 0 for an empty string, so a file of 417 lines measures
    as 381. Line counts here come from .Count or [IO.File]::ReadAllLines().Length.
  - Select-String -Encoding Byte does not exist in PowerShell 5.1. Binary search is
    [IO.File]::ReadAllBytes plus [Text.Encoding]::ASCII.GetString.
  - a marker regex written with \n reports "0 conflict blocks" in a CRLF tree. Marker
    search here is line-based.
  - Select-Object -First N truncated an error list from 18 to 12 and would have made a
    completeness statement false. Nothing here is truncated.
  - Ninja Multi-Config keeps its edges in build-Release.ninja, not build.ninja.
  - the RESULT: PASS line of the test carries NO counter. The ok lines are counted and
    held against the check() call sites in the SOURCE, which covers both directions: a
    skipped case gives too few, a repeated block gives too many. Counting DISTINCT
    assertion texts was the first attempt and it was wrong - two different assertions
    legitimately share the label "size() matches the file on disk", so 18 assertions
    yield 17 distinct texts.
  - the operational configuration re-runs one upstream custom command on every build
    ("Provisioning UI assets", no stamp file). Idempotence is therefore stated over the
    ARTEFACTS, not over "ninja builds nothing".

Usage:
  verify-patch-b10269.ps1                                   # full run, CUDA, llama-server
  verify-patch-b10269.ps1 -Target all                       # every target
  verify-patch-b10269.ps1 -Backend cpu -UpTo build          # cheap regression run
  verify-patch-b10269.ps1 -UpTo configure                   # apply + configure only
  verify-patch-b10269.ps1 -Base b10223 -Patch <file> -ExpectPaths 21   # other base

Exit 0 = every check green.  1 = at least one red.  2 = setup error.
#>
param(
    [string]$WT          = 'C:\Users\robin\dev\crow-lab\wt-e8',
    [string]$SRC         = 'C:\Users\robin\dev\crow-lab\src',
    [string]$CROW        = 'C:\Users\robin\dev\Crow',
    [string]$Base        = 'b10269',
    [string]$Patch       = '',
    [int]   $ExpectPaths = 22,
    [int]   $ExpectNewFiles = 3,
    [string]$BuildDir    = 'build-e8',
    [string]$Target      = 'llama-server',
    [ValidateSet('cuda', 'cpu')][string]$Backend = 'cuda',
    [string[]]$ExtraCMake = @(),
    [string]$RefCache    = 'C:\Users\robin\dev\crow-lab\src\build-native\CMakeCache.txt',
    [ValidateSet('apply', 'configure', 'build', 'test', 'all')][string]$UpTo = 'all',
    [string]$CtestRegex  = 'test-llama-file',
    [string]$TestTarget  = 'test-llama-file',
    [int]   $ExpectOk    = 18,
    [int]   $ExpectOptions = 4,
    [switch]$NoNegative,
    [string]$LogDir      = ''
)

$ErrorActionPreference = 'Continue'
# PowerShell variable names are NOT case sensitive: $UPTO and $UpTo are one variable.
# Assigning the rank to $UPTO therefore hit the ValidateSet on the parameter, the
# assignment failed non-terminally, and the gate then compared the STRING 'apply'
# against 2 - which is true, because letters sort after digits. Every phase ran while
# the tool reported "UPTO apply". A gate that cannot be red is the failure this whole
# tool exists to catch, so the rank now has a name of its own and is asserted.
$PHASE = @{ apply = 1; configure = 2; build = 3; test = 4; all = 5 }
$UpToRank = $PHASE[$UpTo]
if ($UpToRank -isnot [int]) { Write-Output "SETUP ERROR: phase rank for '$UpTo' is not an int"; exit 2 }

if (-not $Patch)  { $Patch  = Join-Path $CROW ("patches\moe-stream-on-{0}.patch" -f $Base) }
if (-not $LogDir) { $LogDir = Join-Path $env:TEMP ("verify-patch-{0}" -f $Base) }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$BUILD  = Join-Path $WT $BuildDir
$GUARD  = Join-Path $CROW 'tools\check-tensor-flags.py'
$STATS  = 'llama_moe_stream_print_stats'
$HEADER = Join-Path $WT 'src\llama-model-loader.h'
$SAMPL  = Join-Path $WT 'common\sampling.cpp'
$SRVSRC = Join-Path $WT 'tools\server\server-context.cpp'
$MOESRC = Join-Path $WT 'src\llama-moe-stream.cpp'
$EXE    = Join-Path $BUILD 'bin\Release\llama-server.exe'
$OBJ    = Join-Path $BUILD 'common\CMakeFiles\llama-common.dir\Release\sampling.cpp.obj'
$OBJASRT= Join-Path $BUILD 'src\CMakeFiles\llama.dir\Release\llama-model-loader.cpp.obj'

function Say($m) { Write-Output ("[{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $m) }
function Secs($a, $b) { [math]::Round(($b - $a).TotalSeconds, 1) }
function Die($m) { Say "SETUP ERROR: $m"; exit 2 }

# --- every check lands here, so nothing can be green by not being reported ----
$script:rows = @()
$script:rc   = 0
function Note($phase, $name, $want, $got, $ok) {
    $script:rows += [pscustomobject]@{ Phase = $phase; Check = $name; Want = "$want"; Got = "$got"; OK = [bool]$ok }
    if (-not $ok) { $script:rc = 1 }
    Say ("  {0,-5} {1,-42} want {2,-14} got {3}" -f $(if ($ok) { 'ok' } else { 'RED' }), $name, "$want", "$got")
}

# --- pure counters: each re-reads its input, nothing is carried forward -------
function Count-InBinary([string]$path, [string]$needle) {
    if (-not [IO.File]::Exists($path)) { return -1 }
    $txt = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($path))
    return ([regex]::Matches($txt, [regex]::Escape($needle))).Count
}
function Stat-Of([string]$path) {
    if (-not [IO.File]::Exists($path)) { return 'MISSING' }
    $fi = Get-Item $path
    return ("{0} B @ {1}" -f $fi.Length, $fi.LastWriteTime.ToString('HH:mm:ss.fff'))
}
function Lines-Of([string]$path) {
    if (-not [IO.File]::Exists($path)) { return @() }
    return [IO.File]::ReadAllLines($path)
}
# -1, never 0, when the file is not there: a missing file must not look like a clean absence.
# That distinction is the whole point on the pristine base, where 0 is the expected result and
# an unreadable path would otherwise deliver it for the wrong reason.
function Count-Src([string]$path, [string]$pattern) {
    if (-not [IO.File]::Exists($path)) { return -1 }
    return @([IO.File]::ReadAllLines($path) | Where-Object { $_ -match $pattern }).Count
}
# Object files move with the generator's layout, so the path is searched rather than typed.
# Anything other than exactly one hit returns '' and is reported as MISSING, i.e. red - an
# ambiguous match must not be resolved by picking the first one.
function Find-One([string]$root, [string]$leaf) {
    if (-not (Test-Path $root)) { return '' }
    $hits = @(Get-ChildItem -Path $root -Filter $leaf -Recurse -File -ErrorAction SilentlyContinue)
    if ($hits.Count -eq 1) { return $hits[0].FullName }
    return ''
}

# --- setup -------------------------------------------------------------------
if (-not (Test-Path (Join-Path $WT '.git'))) { Die "no worktree at $WT (git worktree add --detach $WT $Base)" }
if (-not (Test-Path $Patch))  { Die "no patch at $Patch" }
if (-not (Test-Path $GUARD))  { Die "no flag guard at $GUARD" }
if ($null -eq (Get-Command python -ErrorAction SilentlyContinue)) { Die 'no python on PATH, the flag guard cannot run' }

$baseSha = (& git -C $SRC rev-parse "$Base^{commit}") 2>$null
if ($LASTEXITCODE -ne 0) { Die "cannot resolve $Base in $SRC" }
$headSha = (& git -C $WT rev-parse HEAD)
if ($headSha -ne $baseSha) { Die "worktree HEAD $headSha is not $Base ($baseSha)" }

# The paths under test come from the patch itself here - unlike the roundtrip check,
# where the yardstick must come from elsewhere. The difference is deliberate: there the
# question is "is the patch complete", and a patch that counts itself cannot answer it.
# Here the patch IS the subject and $ExpectPaths is the independent yardstick.
$patchLines = Lines-Of $Patch
$PATHS = @()
$NEWFILES = @()
for ($i = 0; $i -lt $patchLines.Length; $i++) {
    if ($patchLines[$i] -match '^diff --git a/(.+) b/(.+)$') {
        $p = $Matches[1]
        $PATHS += $p
        if (($i + 1) -lt $patchLines.Length -and $patchLines[$i + 1] -match '^new file mode') { $NEWFILES += $p }
    }
}
Say ("patch {0}: {1} blocks, {2} new files" -f (Split-Path $Patch -Leaf), $PATHS.Count, $NEWFILES.Count)

# --- toolchain: cmake is in NO PATH, it comes out of Visual Studio -----------
if ($null -eq (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) { Die 'no vswhere' }
    $vs = & $vswhere -latest -property installationPath 2>$null | Select-Object -First 1
    cmd.exe /c "`"$(Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat')`" && set" 2>$null | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
    }
    $env:PATH = (Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin') + ';' +
                (Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja') + ';' + $env:PATH
}
foreach ($t in @('cmake.exe', 'ninja.exe', 'ctest.exe')) {
    if ($null -eq (Get-Command $t -ErrorAction SilentlyContinue)) { Die "no $t after the vcvars bootstrap" }
}

function Reset-Pristine {
    # git reset --hard, NEVER git checkout -- <paths>: --3way writes to the index, so a
    # checkout restores the PATCHED state and every following measurement is silently
    # wrong. Measured on 2026-08-05.
    & git -C $WT reset -q --hard HEAD
    foreach ($n in $NEWFILES) {
        $f = Join-Path $WT ($n -replace '/', '\')
        if ([IO.File]::Exists($f)) { [IO.File]::Delete($f) }
    }
    $dirty = @(& git -C $WT status --porcelain | Where-Object { $_ -notmatch [regex]::Escape($BuildDir) })
    return $dirty.Count
}
function Guard([string]$state) {
    $out = & python $GUARD --file $HEADER --base $Base --state $state 2>&1
    $code = $LASTEXITCODE
    $out | Out-File (Join-Path $LogDir "guard-$state.log") -Encoding ascii
    return $code
}
function Count-Markers {
    $n = 0
    foreach ($p in $PATHS) {
        $f = Join-Path $WT ($p -replace '/', '\')
        if (-not [IO.File]::Exists($f)) { continue }
        # line-based on purpose: a \n regex reports 0 blocks in a CRLF tree
        $n += @(Select-String -Path $f -Pattern '^(<<<<<<<|=======|>>>>>>>)' -AllMatches).Count
    }
    return $n
}
function Options-Of([string]$exe) {
    if (-not [IO.File]::Exists($exe)) { return @() }
    $out = & $exe --help 2>&1
    $script:smokeRc = $LASTEXITCODE
    return @([regex]::Matches(($out -join "`n"), '--moe-stream[a-z-]*') | ForEach-Object { $_.Value } | Sort-Object -Unique)
}

$T0 = Get-Date
Say ('=' * 78)
Say "BASE $Base ($baseSha)  TARGET $Target  BACKEND $Backend  UPTO $UpTo"
Say ('=' * 78)

# ============================ 1  pristine ====================================
Say 'PHASE apply'
$dirty = Reset-Pristine
if ($dirty -ne 0) { Die "worktree not pristine after reset ($dirty paths)" }
Note 'apply' 'worktree pristine before apply' 0 $dirty ($dirty -eq 0)

$gp = Guard 'pristine'
Note 'apply' 'flag guard, pristine header' 0 $gp ($gp -eq 0)

$preStreamed = @(Select-String -Path $HEADER -Pattern 'TENSOR_STREAMED' -AllMatches).Count
Note 'apply' 'TENSOR_STREAMED absent on pristine base' 0 $preStreamed ($preStreamed -eq 0)

# The missing call, counted on the base BEFORE the patch touches it. Two after the patch means
# nothing unless the same counter reads zero here, on the same file, through the same function -
# and the anchor line beside it is the positive control that makes the zero a statement about the
# CODE rather than about an unreadable path.
$preSrv = Count-Src $SRVSRC ([regex]::Escape($STATS) + '\(')
Note 'apply' 'server statistics call absent on pristine base' 0 $preSrv ($preSrv -eq 0)
$preAnchor = Count-Src $SRVSRC 'void print_timings\(\) const'
Note 'apply' 'print_timings anchor readable on pristine base' 1 $preAnchor ($preAnchor -eq 1)

# ============================ 2  apply =======================================
& git -C $WT apply --3way $Patch *> (Join-Path $LogDir 'apply.log')
$applyRc = $LASTEXITCODE
Note 'apply' 'git apply --3way exit code' 0 $applyRc ($applyRc -eq 0)

$touched = @(& git -C $WT status --porcelain | Where-Object { $_ -notmatch [regex]::Escape($BuildDir) })
Note 'apply' 'touched paths' $ExpectPaths $touched.Count ($touched.Count -eq $ExpectPaths)

$markers = Count-Markers
Note 'apply' 'conflict marker lines' 0 $markers ($markers -eq 0)

$gq = Guard 'patched'
Note 'apply' 'flag guard, patched header' 0 $gq ($gq -eq 0)

$hdr = Lines-Of $HEADER
$nReshape = @($hdr | Where-Object { $_ -match '^\s*static const int\s+TENSOR_ALLOW_RESHAPE\s*=\s*1\s*<<\s*4\s*;' }).Count
$nStream  = @($hdr | Where-Object { $_ -match '^\s*static const int\s+TENSOR_STREAMED\s*=\s*1\s*<<\s*5\s*;' }).Count
Note 'apply' 'TENSOR_ALLOW_RESHAPE = 1 << 4' 1 $nReshape ($nReshape -eq 1)
Note 'apply' 'TENSOR_STREAMED = 1 << 5' 1 $nStream ($nStream -eq 1)

$srcCalls = @(Select-String -Path $SAMPL -Pattern "$STATS\(llama_get_model" -AllMatches).Count
Note 'apply' 'statistics call in common/sampling.cpp' 1 $srcCalls ($srcCalls -eq 1)

# --- the server call, per role ------------------------------------------------
# Counted per role rather than in total: a total of 2 is also what a copy-paste of the target
# call twice would produce, and that reads "drafter" nowhere.
$patTgt = [regex]::Escape($STATS) + '\(llama_get_model\(ctx_tgt\), "target"\)'
$patDft = [regex]::Escape($STATS) + '\(llama_get_model\(ctx_dft\), "drafter"\)'
$nTgt = Count-Src $SRVSRC $patTgt
$nDft = Count-Src $SRVSRC $patDft
$nSrv = Count-Src $SRVSRC ([regex]::Escape($STATS) + '\(')
Note 'apply' 'server call for the target model' 1 $nTgt ($nTgt -eq 1)
Note 'apply' 'server call for the draft model'  1 $nDft ($nDft -eq 1)
Note 'apply' 'server statistics calls in total' 2 $nSrv ($nSrv -eq 2)

# WHERE the two calls sit, because the count alone would be green for a call inside the decode
# loop - which is the one placement that must not exist. Required order: the print_timings
# anchor, then the speculative block, then target, then drafter, with no closing brace of the
# method in between. The drafter call additionally sits behind its ctx_dft guard and the target
# call in front of it, so a server without a drafter cannot print a drafter block.
$srvLines = Lines-Of $SRVSRC
$iAnchor = -1; $iSpec = -1; $iTgt = -1; $iDft = -1; $iGuard = -1; $iClose = -1
for ($i = 0; $i -lt $srvLines.Length; $i++) {
    if ($iAnchor -lt 0 -and $srvLines[$i] -match 'void print_timings\(\) const')            { $iAnchor = $i }
    if ($iAnchor -ge 0 -and $iSpec -lt 0 -and $srvLines[$i] -match 'common_speculative_print_stats\(spec\)') { $iSpec = $i }
    if ($iTgt -lt 0 -and $srvLines[$i] -match $patTgt) { $iTgt = $i }
    if ($iDft -lt 0 -and $srvLines[$i] -match $patDft) { $iDft = $i }
}
if ($iDft -gt 0) {
    for ($i = $iDft - 1; ($i -ge 0) -and ($i -ge $iDft - 3); $i--) {
        if ($srvLines[$i] -match '^\s*if \(ctx_dft\)') { $iGuard = $i }
    }
}
if ($iSpec -ge 0 -and $iDft -gt $iSpec) {
    for ($i = $iSpec; $i -lt $iDft; $i++) { if ($srvLines[$i] -match '^\s{4}\}\s*$') { $iClose = $i; break } }
}
Note 'apply' 'both calls sit inside print_timings' 'anchor<spec<tgt<dft' ("$iAnchor<$iSpec<$iTgt<$iDft") `
    (($iAnchor -ge 0) -and ($iSpec -gt $iAnchor) -and ($iTgt -gt $iSpec) -and ($iDft -gt $iTgt))
Note 'apply' 'no end of method between the speculative block and the drafter call' 'none' `
    $(if ($iClose -eq -1) { 'none' } else { "line $iClose" }) ($iClose -eq -1)
Note 'apply' 'drafter call behind its ctx_dft guard, target call in front of it' 'tgt<guard<dft' ("$iTgt<$iGuard<$iDft") `
    (($iGuard -ge 0) -and ($iTgt -ge 0) -and ($iTgt -lt $iGuard) -and ($iDft -gt $iGuard))

# --- the role token reaches EVERY line of a block -----------------------------
# A block whose first line is labelled and whose locality lines are not cannot be attributed
# when two models print into one log. Both directions are counted: every statistics line must
# carry the role placeholder, and none may carry the unlabelled form.
$roleLines  = Count-Src $MOESRC 'LLAMA_LOG_INFO\("%s: %smoe stream:'
$plainLines = Count-Src $MOESRC 'LLAMA_LOG_INFO\("%s: moe stream:'
Note 'apply' 'statistics lines carrying the role placeholder' 7 $roleLines ($roleLines -eq 7)
Note 'apply' 'statistics lines without a role placeholder' 0 $plainLines ($plainLines -eq 0)
$roleSig = Count-Src $MOESRC 'void llama_moe_stream::print_stats\(const char \* role\) const'
Note 'apply' 'print_stats takes the role' 1 $roleSig ($roleSig -eq 1)

# THE DEFECT THIS EXISTS FOR, measured 2026-08-06. The role parameter was added and one caller
# kept the zero-argument form: src/llama-context.cpp, inside llama_perf_context_print, reached
# through the C++ METHOD rather than the C API - which is exactly why a grep for the API name
# did not find it and why the build was the first thing to notice. There are TWO print sites in
# this patch, not one, and both have to carry a role decision. The sweep therefore runs over
# every path the patch touches, and it counts unreadable paths separately: a file that cannot
# be read must not contribute a comfortable zero to a check that expects zero.
$zeroArg = 0; $unreadable = 0
foreach ($p in $PATHS) {
    $c = Count-Src (Join-Path $WT ($p -replace '/', '\')) 'print_stats\(\)'
    if ($c -lt 0) { $unreadable++ } elseif ($c -gt 0) { $zeroArg += $c }
}
Note 'apply' 'every patched path readable for the sweep' 0 $unreadable ($unreadable -eq 0)
Note 'apply' 'zero-argument print_stats() calls left'    0 $zeroArg    ($zeroArg -eq 0)
$ctxCall = Count-Src (Join-Path $WT 'src\llama-context.cpp') 'mstream->print_stats\(nullptr\)'
Note 'apply' 'llama_perf_context_print states its role explicitly' 1 $ctxCall ($ctxCall -eq 1)

# --- inventory, split ---------------------------------------------------------
# The path count alone cannot say whether the 22nd path is a foreign file or another own one,
# and the whole objective this patch is measured against is the FOREIGN number.
$ownFiles     = $NEWFILES.Count
$foreignFiles = $PATHS.Count - $NEWFILES.Count
Note 'apply' 'own files in the patch'     $ExpectNewFiles $ownFiles ($ownFiles -eq $ExpectNewFiles)
Note 'apply' 'foreign files in the patch' ($ExpectPaths - $ExpectNewFiles) $foreignFiles ($foreignFiles -eq ($ExpectPaths - $ExpectNewFiles))

# --- source invariants that a green build cannot show --------------------------
# Upstream behaviour we must not displace is compared against the BASE, not against a
# number typed in here: a hard-coded 2 would still be green if upstream changed to 3 and
# our patch dropped one of them.
function Count-Pattern([string[]]$lines, [string]$pat) {
    return @($lines | Where-Object { $_ -match $pat }).Count
}
function Base-Lines([string]$rel) {
    $t = & git -C $WT show ("{0}:{1}" -f $baseSha, $rel) 2>$null
    if ($LASTEXITCODE -ne 0) { return @() }
    return @($t)
}
foreach ($rel in @('src/llama-graph.cpp', 'src/llama-context.cpp')) {
    $wasN = Count-Pattern (Base-Lines $rel) 'dsv4_hc_mult'
    $isN  = Count-Pattern (Lines-Of (Join-Path $WT ($rel -replace '/', '\'))) 'dsv4_hc_mult'
    Note 'apply' ("dsv4_hc_mult in {0} unchanged vs base" -f (Split-Path $rel -Leaf)) $wasN $isN ($wasN -eq $isN)
}

$graph = Lines-Of (Join-Path $WT 'src\llama-graph.cpp')
$loraCalls = Count-Pattern $graph 'build_lora_mm_id\(.*ids_gemm'
Note 'apply' 'build_lora_mm_id call sites carrying ids_gemm' 4 $loraCalls ($loraCalls -eq 4)
$loraDecl = Count-Pattern (Lines-Of (Join-Path $WT 'src\llama-graph.h')) 'build_lora_mm_id'
Note 'apply' 'build_lora_mm_id declared once in llama-graph.h' 1 $loraDecl ($loraDecl -eq 1)

# The mstream surcharge must sit BEHIND the if/else and the arch branch must no longer
# return early - otherwise our model takes the n_tokens*40 branch and never receives the
# surcharge. That is a data-flow property; no build and no exit code shows it.
$ctx = Lines-Of (Join-Path $WT 'src\llama-context.cpp')
$s = -1; $e = -1
for ($i = 0; $i -lt $ctx.Length; $i++) { if ($ctx[$i] -match '^uint32_t llama_context::graph_max_nodes') { $s = $i; break } }
if ($s -ge 0) { for ($i = $s + 1; $i -lt $ctx.Length; $i++) { if ($ctx[$i] -match '^\}') { $e = $i; break } } }
if ($s -ge 0 -and $e -gt $s) {
    $body = @($ctx[$s..$e])
    $nRet = Count-Pattern $body '^\s*return\b'
    $iSur = -1; $iRet = -1; $iArch = -1
    for ($i = 0; $i -lt $body.Count; $i++) {
        # 'res += 24u*' and not the old 'res += 24u*n_waves': E13 moved the wave plan into
        # llama_moe_stream_wave_plan, so the local is now wb.n_waves. The property under test is
        # WHERE the surcharge is added, not what the variable is called - a yardstick pinned to a
        # spelling reports red for a rename and green for a moved line.
        if (($iSur -lt 0)  -and ($body[$i] -match 'res \+= 24u\*'))                        { $iSur = $i }
        if (($iRet -lt 0)  -and ($body[$i] -match '^\s*return res;'))                      { $iRet = $i }
        if (($iArch -lt 0) -and ($body[$i] -match 'res = std::max<uint32_t>\(n_tokens \* 40')) { $iArch = $i }
    }
    Note 'apply' 'graph_max_nodes has exactly one return' 1 $nRet ($nRet -eq 1)
    Note 'apply' 'arch branch assigns instead of returning early' 'found' $iArch ($iArch -ge 0)
    Note 'apply' 'mstream surcharge sits behind the if/else, before the return' 'surcharge<return' ("$iSur<$iRet") (($iSur -ge 0) -and ($iRet -gt $iSur) -and ($iArch -ge 0) -and ($iSur -gt $iArch))
} else {
    Note 'apply' 'graph_max_nodes body located' 'yes' 'no' $false
}

# ============================ 3  configure ===================================
$cfgRc = -1; $cfgSecs = 0; $edges = -1
$script:offeredTargets = -1; $script:hasAll = -1; $script:stepsRun = -1; $script:serverBefore = 'unread'
if ($UpToRank -ge $PHASE['configure']) {
    Say 'PHASE configure'
    $cmakeArgs = @('-S', $WT, '-B', $BUILD, '-G', 'Ninja Multi-Config')
    if ($Backend -eq 'cuda') {
        $cmakeArgs += @('-DGGML_CUDA=ON', '-DGGML_CUDA_FA=ON', '-DGGML_CUDA_GRAPHS=ON', '-DGGML_CUDA_NCCL=ON')
    } else {
        $cmakeArgs += @('-DGGML_CUDA=OFF')
    }
    $cmakeArgs += @('-DGGML_NATIVE=OFF', '-DGGML_CPU_ALL_VARIANTS=OFF',
                    '-DLLAMA_BUILD_TESTS=ON', '-DLLAMA_BUILD_SERVER=ON',
                    '-DLLAMA_BUILD_TOOLS=ON', '-DLLAMA_BUILD_EXAMPLES=OFF')
    $cmakeArgs += $ExtraCMake
    ($cmakeArgs -join ' ') | Out-File (Join-Path $LogDir 'configure-args.txt') -Encoding ascii

    $t = Get-Date
    & cmake.exe @cmakeArgs *> (Join-Path $LogDir 'configure.log')
    $cfgRc = $LASTEXITCODE
    $cfgSecs = Secs $t (Get-Date)
    Note 'configure' 'configure exit code' 0 $cfgRc ($cfgRc -eq 0)
    Say "  configure took $cfgSecs s"

    # CUDA must be DETECTED, not merely requested. A silent fallback to CPU would leave
    # the option ON in the cache and prove nothing.
    $cfgLog = Lines-Of (Join-Path $LogDir 'configure.log')
    $cudaLines = @($cfgLog | Where-Object { $_ -match 'CUDA|nvcc|cuda architectures|CMAKE_CUDA' })
    $cudaLines | Out-File (Join-Path $LogDir 'configure-cuda.txt') -Encoding ascii
    foreach ($l in $cudaLines) { Say "  cfg| $l" }

    $newCache = Join-Path $BUILD 'CMakeCache.txt'
    $cudaOn = @(Select-String -Path $newCache -Pattern '^GGML_CUDA:BOOL=ON$').Count
    $backends = @(Select-String -Path $newCache -Pattern '^GGML_AVAILABLE_BACKENDS:INTERNAL=(.*)$' | ForEach-Object { $_.Matches[0].Groups[1].Value })
    $wantCuda = $(if ($Backend -eq 'cuda') { 1 } else { 0 })
    Note 'configure' 'GGML_CUDA=ON in the produced cache' $wantCuda $cudaOn ($cudaOn -eq $wantCuda)
    $hasCudaBackend = $(if ($backends.Count -gt 0 -and $backends[0] -match 'ggml-cuda') { 1 } else { 0 })
    Note 'configure' 'ggml-cuda among the available backends' $wantCuda $hasCudaBackend ($hasCudaBackend -eq $wantCuda)
    Say ("  available backends: {0}" -f ($backends -join ' '))

    $nvcc = @(Select-String -Path $newCache -Pattern '^CMAKE_CUDA_COMPILER:FILEPATH=(.*)$' | ForEach-Object { $_.Matches[0].Groups[1].Value })
    Say ("  CUDA compiler: {0}" -f $(if ($nvcc.Count) { $nvcc[0] } else { 'none in cache' }))

    # cache drift against the operational build, reported and never interpreted away
    if (Test-Path $RefCache) {
        function Cache-Map([string]$p) {
            $m = @{}
            foreach ($l in (Lines-Of $p)) {
                if ($l -match '^([A-Za-z_0-9]+):(BOOL|STRING|PATH|FILEPATH)=(.*)$') { $m[$Matches[1]] = $Matches[3] }
            }
            return $m
        }
        $a = Cache-Map $RefCache
        $b = Cache-Map $newCache
        $diff = @(); $onlyRef = @(); $onlyNew = @()
        foreach ($k in $a.Keys) {
            if ($b.ContainsKey($k)) { if ($a[$k] -ne $b[$k]) { $diff += "$k : ref=$($a[$k]) new=$($b[$k])" } }
            else { $onlyRef += $k }
        }
        foreach ($k in $b.Keys) { if (-not $a.ContainsKey($k)) { $onlyNew += $k } }
        # Paths differ by construction (different build dir), so they are listed, not judged.
        $diffNoPath = @($diff | Where-Object { $_ -notmatch 'ref=[A-Za-z]:/|ref=C:\\|_DIR|PREFIX' })
        ($diff + @('--- only in reference ---') + $onlyRef + @('--- only in new ---') + $onlyNew) |
            Out-File (Join-Path $LogDir 'cache-drift.txt') -Encoding ascii
        Say ("  cache drift vs operational: {0} differing keys ({1} non-path), {2} only-ref, {3} only-new" -f `
             $diff.Count, $diffNoPath.Count, $onlyRef.Count, $onlyNew.Count)
        foreach ($d in $diffNoPath) { Say "  drift| $d" }
    }

    # The switch whose absence caused the historical LNK1181 on a full build. Recorded
    # before and after, so a later claim about that failure has a value behind it.
    $srvBefore = @(Select-String -Path $newCache -Pattern '^LLAMA_BUILD_SERVER:BOOL=(.*)$' | ForEach-Object { $_.Matches[0].Groups[1].Value })
    $script:serverBefore = $(if ($srvBefore.Count) { $srvBefore[0] } else { 'absent' })
    Say ("  LLAMA_BUILD_SERVER before the build: {0}" -f $script:serverBefore)

    # TARGETS OFFERED BY THE GENERATOR. This is a target count and it is NOT the edge
    # count below. An edge count must never stand in for it - the historical "382" is a
    # target claim and only a target count can confirm or refute it.
    & cmake.exe --build $BUILD --config Release --target help *> (Join-Path $LogDir 'targets-help.log')
    $helpRc = $LASTEXITCODE
    $helpLines = Lines-Of (Join-Path $LogDir 'targets-help.log')
    $offered = @($helpLines | Where-Object { $_ -match '^[^\s:]+:\s*\S' })
    $script:offeredTargets = $offered.Count
    $script:hasAll = @($helpLines | Where-Object { $_ -match '^all:' }).Count
    Note 'configure' 'target list from the generator readable' 0 $helpRc ($helpRc -eq 0)
    Note 'configure' "target 'all' offered by the generator" 1 $script:hasAll ($script:hasAll -eq 1)
    Say ("  targets offered by the generator: {0}   (NOT the same quantity as ninja edges)" -f $script:offeredTargets)

    # OUT-OF-DATE NINJA EDGES for the requested target, measured by a dry run.
    # STATE DEPENDENT, and the label matters: ninja -n lists what is out of date in THIS
    # build directory right now, not the edge count of a build from scratch. Measured
    # 2026-08-05: 444 for llama-server in an empty directory, 382 for all in a directory
    # that had just been built. The second number coincides with the "382 targets" the
    # plan carries, and that coincidence is worth nothing - it is a different quantity
    # measured under a different condition. Only an empty build directory yields the
    # from-scratch number, and no edge count ever answers a question about targets.
    & cmake.exe --build $BUILD --config Release --target $Target -- -n *> (Join-Path $LogDir 'dryrun.log')
    $dryRc = $LASTEXITCODE
    $mx = 0
    foreach ($l in (Lines-Of (Join-Path $LogDir 'dryrun.log'))) {
        if ($l -match '^\[(\d+)/(\d+)\]') { $v = [int]$Matches[2]; if ($v -gt $mx) { $mx = $v } }
    }
    $edges = $mx
    Note 'configure' 'ninja dry run exit code' 0 $dryRc ($dryRc -eq 0)
    Say "  out-of-date ninja EDGES for target '$Target': $edges  (state dependent, not a target count)"
}

# ============================ 4  build =======================================
$bldRc = -1; $bldSecs = 0; $errCount = -1; $firstErr = ''
$objBefore = ''; $objAfter = ''; $symPos = -1; $exeAfter = ''
$SRVOBJ = ''; $srvObjAfter = ''; $symSrvPos = -1; $roleSrvPos = -1
if ($UpToRank -ge $PHASE['build'] -and $cfgRc -eq 0) {
    Say 'PHASE build'
    # Delete first. A stale exe or obj would pass a check the compiler never wrote -
    # that is the pattern preserve-build.ps1 established and the reason it holds.
    $srvObjPre = Find-One $BUILD 'server-context.cpp.obj'
    foreach ($f in @($EXE, $OBJ, $OBJASRT, $srvObjPre)) { if ($f -and [IO.File]::Exists($f)) { [IO.File]::Delete($f) } }
    $objBefore = Stat-Of $OBJ
    Note 'build' 'sampling.cpp.obj removed before the build' 'MISSING' $objBefore ($objBefore -eq 'MISSING')

    $t = Get-Date
    & cmake.exe --build $BUILD --config Release --target $Target *> (Join-Path $LogDir 'build.log')
    $bldRc = $LASTEXITCODE
    $bldSecs = Secs $t (Get-Date)
    Note 'build' 'build exit code' 0 $bldRc ($bldRc -eq 0)
    Say "  build took $bldSecs s"

    $bl = Lines-Of (Join-Path $LogDir 'build.log')
    $errs = @($bl | Where-Object { $_ -match 'error [A-Z]+[0-9]+|error:|LNK[0-9]+' })
    $errCount = $errs.Count
    if ($errCount -gt 0) { $firstErr = $errs[0] }
    Note 'build' 'error lines in the build log' 0 $errCount ($errCount -eq 0)
    if ($errCount -gt 0) {
        Say "  first error: $firstErr"
        $errs | Out-File (Join-Path $LogDir 'build-errors.txt') -Encoding ascii   # never truncated
    }
    $sa = @($bl | Where-Object { $_ -match 'static assertion failed|static_assert' }).Count
    Note 'build' 'static_assert violations in the log' 0 $sa ($sa -eq 0)
    # Executed steps, counted from the progress lines themselves rather than taken from
    # the denominator - a build that stops early has a denominator it never reached.
    $steps = @($bl | Where-Object { $_ -match '^\[(\d+)/(\d+)\]' }).Count
    $built = 0
    foreach ($l in $bl) { if ($l -match '^\[(\d+)/(\d+)\]') { $v = [int]$Matches[2]; if ($v -gt $built) { $built = $v } } }
    $script:stepsRun = $steps
    Say "  build steps executed: $steps of $built announced (dry run planned $edges edges)"

    # E8 (4) idempotence. The plan words it as "nothing is built", but measured on
    # 2026-08-05 the operational configuration always re-runs ONE upstream custom
    # command - "Provisioning UI assets" from tools/ui/CMakeLists.txt, which carries no
    # stamp file. That is a property of the foreign tree, not of our patch, and no
    # switch here is flipped to make it disappear. So the criterion is stated as what
    # actually matters and can still go red: the ARTEFACTS must not change. The re-run
    # steps are reported by name rather than counted against zero.
    $idemBefore = @((Stat-Of $EXE), (Stat-Of $OBJ), (Stat-Of $OBJASRT))
    & cmake.exe --build $BUILD --config Release --target $Target *> (Join-Path $LogDir 'build-again.log')
    $againRc = $LASTEXITCODE
    $again = Lines-Of (Join-Path $LogDir 'build-again.log')
    $againSteps = @($again | Where-Object { $_ -match '^\[(\d+)/(\d+)\]' })
    $idemAfter = @((Stat-Of $EXE), (Stat-Of $OBJ), (Stat-Of $OBJASRT))
    Note 'build' 'idempotence: second run exit code' 0 $againRc ($againRc -eq 0)
    Note 'build' 'idempotence: artefacts unchanged by the second run' ($idemBefore -join ' | ') ($idemAfter -join ' | ') (($idemBefore -join '|') -eq ($idemAfter -join '|'))
    Say ("  steps re-run on the second build: {0}" -f $againSteps.Count)
    foreach ($s in $againSteps) { Say "  again| $s" }

    $srvAfter = @(Select-String -Path (Join-Path $BUILD 'CMakeCache.txt') -Pattern '^LLAMA_BUILD_SERVER:BOOL=(.*)$' | ForEach-Object { $_.Matches[0].Groups[1].Value })
    $sa2 = $(if ($srvAfter.Count) { $srvAfter[0] } else { 'absent' })
    Note 'build' 'LLAMA_BUILD_SERVER unchanged by the build' $script:serverBefore $sa2 ($sa2 -eq $script:serverBefore)

    $objAfter = Stat-Of $OBJ
    $exeAfter = Stat-Of $EXE
    Note 'build' 'sampling.cpp.obj freshly written' 'present' $objAfter ($objAfter -ne 'MISSING')
    Note 'build' 'llama-model-loader.cpp.obj compiled (static_assert)' 'present' (Stat-Of $OBJASRT) ((Stat-Of $OBJASRT) -ne 'MISSING')
    Note 'build' 'llama-server.exe freshly written' 'present' $exeAfter ($exeAfter -ne 'MISSING')

    $symPos = Count-InBinary $OBJ $STATS
    Note 'build' "$STATS in fresh sampling.cpp.obj" 1 $symPos ($symPos -eq 1)

    # The server object, and the yardstick is deliberately ">= 1" rather than "= 2". Two call
    # sites do NOT produce two occurrences of the name: a COFF holds one symbol table entry and
    # the relocations point at its index. Demanding 2 would be a checker that is red for a
    # correct object. What the object CAN carry twice over is the role, and "drafter" appears
    # exactly once in this source, so its presence in the binary is a statement about the label
    # having survived compilation - which the source count cannot make.
    $SRVOBJ = Find-One $BUILD 'server-context.cpp.obj'
    $srvObjAfter = Stat-Of $SRVOBJ
    Note 'build' 'server-context.cpp.obj located, exactly one hit' 'present' $srvObjAfter ($srvObjAfter -ne 'MISSING')
    $symSrvPos = Count-InBinary $SRVOBJ $STATS
    $roleSrvPos = Count-InBinary $SRVOBJ 'drafter'
    Note 'build' "$STATS in fresh server-context.cpp.obj" '>= 1' $symSrvPos ($symSrvPos -ge 1)
    Note 'build' 'role literal drafter in fresh server-context.cpp.obj' '>= 1' $roleSrvPos ($roleSrvPos -ge 1)

    $script:smokeRc = -1
    $opts = Options-Of $EXE
    Note 'build' 'smoke: --help exit code' 0 $script:smokeRc ($script:smokeRc -eq 0)
    Note 'build' 'distinct --moe-stream* options' $ExpectOptions $opts.Count ($opts.Count -eq $ExpectOptions)
    Say ("  options: {0}" -f ($opts -join ' '))
}

# ============================ 5  ctest =======================================
$ctRc = -1; $okLines = -1; $okDistinct = -1; $notOk = -1; $skips = -1
$testBldRc = -1; $testBldSecs = 0; $testSteps = -1; $testErrs = -1; $testExeStat = ''
if ($UpToRank -ge $PHASE['test'] -and $bldRc -eq 0) {
    Say 'PHASE test'

    # CTEST CANNOT PASS WHAT WAS NEVER BUILT. The default target here is llama-server, which
    # does not carry test-llama-file, so on 2026-08-06 the ctest phase reported exit 8 and zero
    # ok lines against a build that was entirely green - a red that says nothing about the
    # patch and everything about this tool. The test binary is therefore built here, as its own
    # step with its own exit code, rather than by widening the build target to 'all': the build
    # phase above is also the measurement of the patch's rebuild scope, and mixing an unrelated
    # target into it would destroy that number.
    $t = Get-Date
    & cmake.exe --build $BUILD --config Release --target $TestTarget *> (Join-Path $LogDir 'build-test.log')
    $testBldRc = $LASTEXITCODE
    $testBldSecs = Secs $t (Get-Date)
    $tb = Lines-Of (Join-Path $LogDir 'build-test.log')
    $testSteps = @($tb | Where-Object { $_ -match '^\[(\d+)/(\d+)\]' }).Count
    $testErrs  = @($tb | Where-Object { $_ -match 'error [A-Z]+[0-9]+|error:|LNK[0-9]+' }).Count
    Note 'test' "build of $TestTarget exit code"    0 $testBldRc ($testBldRc -eq 0)
    Note 'test' "error lines building $TestTarget"  0 $testErrs  ($testErrs -eq 0)
    $testExe = Join-Path $BUILD ("bin\Release\{0}.exe" -f $TestTarget)
    $testExeStat = Stat-Of $testExe
    Note 'test' "$TestTarget.exe present before ctest" 'present' $testExeStat ($testExeStat -ne 'MISSING')
    Say ("  $TestTarget built in $testBldSecs s, $testSteps steps")

    & ctest.exe --test-dir $BUILD -C Release -R $CtestRegex --output-on-failure *> (Join-Path $LogDir 'ctest.log')
    $ctRc = $LASTEXITCODE
    Note 'test' 'ctest process exit code' 0 $ctRc ($ctRc -eq 0)

    # The full protocol lives in LastTest.log; ctest -V would prefix every line with
    # "1: " and the count would then depend on the prefix.
    $last = Join-Path $BUILD 'Testing\Temporary\LastTest.log'
    if ([IO.File]::Exists($last)) {
        Copy-Item $last (Join-Path $LogDir 'LastTest.log') -Force
        $tl = Lines-Of $last
        $okRaw = @($tl | Where-Object { $_ -match '^\s{1,4}ok\s{2,}\S' })
        $okLines = $okRaw.Count
        $okDistinct = @($okRaw | ForEach-Object { ($_ -replace '^\s*ok\s+', '').Trim() } | Sort-Object -Unique).Count

        # THE YARDSTICK COMES FROM THE SOURCE, not from a number typed in here. Counting
        # distinct assertion TEXTS was the first attempt and it was wrong: two genuinely
        # different assertions share the label "size() matches the file on disk", once in
        # the buffered and once in the unbuffered section, so 18 assertions produce 17
        # distinct texts. Comparing against the call sites covers both directions - fewer
        # ok lines means a case was skipped, more means something printed twice.
        $tsrc = [IO.File]::ReadAllText((Join-Path $WT 'tests\test-llama-file.cpp'))
        $callSites = ([regex]::Matches($tsrc, 'check\(')).Count `
                   - ([regex]::Matches($tsrc, 'static void check\(')).Count `
                   - ([regex]::Matches($tsrc, 'check\(s\)')).Count
        Note 'test' 'ok lines match the check() call sites in the source' $callSites $okLines ($okLines -eq $callSites)
        Say ("  distinct assertion texts: {0} of {1} ok lines (labels may legitimately repeat)" -f $okDistinct, $okLines)
        # RESULT: FAIL starts at column 0 and would slip past an indent-anchored pattern
        $notOk = @($tl | Where-Object { $_ -match '^\s*not ok|^\s{1,4}FAIL\s|^RESULT: FAIL' }).Count
        $skips = @($tl | Where-Object { $_ -match '^\s{1,4}SKIP\s' }).Count
        $pass  = @($tl | Where-Object { $_ -match '^RESULT: PASS' }).Count
        Note 'test' 'counted ok lines' $ExpectOk $okLines ($okLines -eq $ExpectOk)
        Note 'test' 'not ok / FAIL lines' 0 $notOk ($notOk -eq 0)
        Note 'test' 'SKIP lines' 0 $skips ($skips -eq 0)
        Note 'test' 'RESULT: PASS line (carries NO counter)' 1 $pass ($pass -eq 1)
    } else {
        Note 'test' 'LastTest.log present' 'yes' 'no' $false
    }
}

# ============================ 6  negative probe ==============================
$symNeg = -1; $objNeg = ''; $optsNeg = -1; $negBldRc = -1
$symSrvNeg = -1; $roleSrvNeg = -1
$script:negRan = $false; $script:negBldSecs = -1; $script:negBldSteps = -1
if ($UpToRank -ge $PHASE['all'] -and -not $NoNegative -and $bldRc -eq 0) {
    $script:negRan = $true
    Say 'PHASE negative probe (pristine base, same build dir)'
    $d2 = Reset-Pristine
    Note 'negative' 'worktree pristine again' 0 $d2 ($d2 -eq 0)
    $gp2 = Guard 'pristine'
    Note 'negative' 'flag guard, pristine after reset' 0 $gp2 ($gp2 -eq 0)

    $srvObjNegPre = Find-One $BUILD 'server-context.cpp.obj'
    foreach ($f in @($EXE, $OBJ, $srvObjNegPre)) { if ($f -and [IO.File]::Exists($f)) { [IO.File]::Delete($f) } }
    # Timed like the forward build: these two builds ARE the measurement of the patch's rebuild
    # scope, one per direction, and a duration for only one of them cannot be generalised to the
    # other - they do not even compile the same set (llama-moe-stream.cpp exists in one tree only).
    $tn = Get-Date
    & cmake.exe --build $BUILD --config Release --target $Target *> (Join-Path $LogDir 'build-negative.log')
    $negBldRc = $LASTEXITCODE
    $script:negBldSecs = Secs $tn (Get-Date)
    $nb = Lines-Of (Join-Path $LogDir 'build-negative.log')
    $script:negBldSteps = @($nb | Where-Object { $_ -match '^\[(\d+)/(\d+)\]' }).Count
    Say ("  pristine rebuild took {0} s, {1} steps" -f $script:negBldSecs, $script:negBldSteps)
    Note 'negative' 'pristine build exit code' 0 $negBldRc ($negBldRc -eq 0)

    $objNeg = Stat-Of $OBJ
    $symNeg = Count-InBinary $OBJ $STATS
    Note 'negative' "$STATS on the pristine base" 0 $symNeg ($symNeg -eq 0)
    $script:smokeRc = -1
    $on = Options-Of $EXE
    $optsNeg = $on.Count
    Note 'negative' 'distinct --moe-stream* on the pristine base' 0 $optsNeg ($optsNeg -eq 0)
    Note 'negative' 'object genuinely rebuilt (size/time differ)' 'differs' ("$objAfter | $objNeg") ($objAfter -ne $objNeg)

    # The server object, same discipline. The role literal is the sharper of the two: a build
    # that somehow kept the call but lost the label would still show the symbol.
    $srvObjNeg = Find-One $BUILD 'server-context.cpp.obj'
    $symSrvNeg  = Count-InBinary $srvObjNeg $STATS
    $roleSrvNeg = Count-InBinary $srvObjNeg 'drafter'
    Note 'negative' "$STATS in server-context.cpp.obj on the pristine base" 0 $symSrvNeg ($symSrvNeg -eq 0)
    Note 'negative' 'role literal drafter on the pristine base' 0 $roleSrvNeg ($roleSrvNeg -eq 0)
    Note 'negative' 'server object genuinely rebuilt (size/time differ)' 'differs' ("$srvObjAfter | $(Stat-Of $srvObjNeg)") ($srvObjAfter -ne (Stat-Of $srvObjNeg))
}

# THE PROBE MUST HAVE RUN. -1 is the initial value of every counter above; a phase that was
# skipped leaves it in place, and a reader who sees "symbolPristine: -1" beside a green RESULT
# line has been told nothing. So the skip is a check of its own whenever the probe was due.
if ($UpToRank -ge $PHASE['all'] -and -not $NoNegative) {
    Note 'negative' 'negative probe actually ran' $true $script:negRan $script:negRan
}

# ============================ report =========================================
Reset-Pristine | Out-Null
Say ('=' * 78)
$rows | Format-Table Phase, Check, Want, Got, OK -AutoSize | Out-String -Width 200 | Write-Output

# THE THREE PRINT PATHS, named so that a later sweep for "every call needs a role" cannot
# quietly relabel the two that are deliberately roleless. Two of them existed before #54 and
# keep their exact output; only the third is new.
Say 'print paths and their role semantics:'
Say '  common/sampling.cpp      common_perf_print         -> NULL, existing generic format (llama-completion, llama-tts)'
Say '  src/llama-context.cpp    llama_perf_context_print  -> NULL, existing generic format (llama-bench, perplexity, imatrix, batched-bench, mtmd-cli)'
Say '  tools/server/...         server_slot::print_timings-> "target" and "drafter", role separated, NEW'

$summary = [pscustomobject]@{
    base = $Base; baseSha = $baseSha; patch = (Split-Path $Patch -Leaf); target = $Target
    backend = $Backend; upTo = $UpTo; paths = $touched.Count; markers = $markers
    applyRc = $applyRc; configureRc = $cfgRc; configureSecs = $cfgSecs
    # four separate quantities, never one standing in for another
    targetsOfferedByGenerator = $script:offeredTargets; targetAllOffered = $script:hasAll
    outOfDateEdgesAtDryRun = $edges; buildStepsExecuted = $script:stepsRun
    llamaBuildServer = $script:serverBefore
    buildRc = $bldRc; buildSecs = $bldSecs; errorLines = $errCount; firstError = $firstErr
    ctestRc = $ctRc; okLines = $okLines; okDistinct = $okDistinct; notOk = $notOk; skips = $skips
    testTarget = $TestTarget; testBuildRc = $testBldRc; testBuildSecs = $testBldSecs
    testBuildSteps = $testSteps; testBuildErrors = $testErrs; testExe = $testExeStat
    symbolPatched = $symPos; symbolPristine = $symNeg
    objPatched = $objAfter; objPristine = $objNeg
    # -1 anywhere below means "not measured", never "zero". negativeProbeRan says which it is.
    serverSymbolPatched = $symSrvPos; serverSymbolPristine = $symSrvNeg
    serverRolePatched = $roleSrvPos; serverRolePristine = $roleSrvNeg
    serverObjPatched = $srvObjAfter
    negativeProbeRan = $script:negRan
    # rebuild scope, one entry per direction. They are not interchangeable.
    rebuildForwardSecs = $bldSecs; rebuildForwardSteps = $script:stepsRun
    rebuildBackSecs = $script:negBldSecs; rebuildBackSteps = $script:negBldSteps
    ownFiles = $ownFiles; foreignFiles = $foreignFiles
    serverCallsTarget = $nTgt; serverCallsDrafter = $nDft; serverCallsPristine = $preSrv
    wallSecs = (Secs $T0 (Get-Date)); checks = $rows.Count
    red = @($rows | Where-Object { -not $_.OK }).Count
}
$summary | ConvertTo-Json -Depth 4 | Out-File (Join-Path $LogDir 'summary.json') -Encoding ascii
Say "summary written to $(Join-Path $LogDir 'summary.json')"

# The negative control as a criterion, not left to the reader: if the patched and the
# pristine run report the SAME symbol count, this tool measured nothing at all.
if ($symPos -ge 0 -and $symNeg -ge 0 -and $symPos -eq $symNeg) {
    Say "RESULT: red - symbol count identical ($symPos) in both probes; the CHECKER is broken, not the patch"
    exit 1
}
if ($symSrvPos -ge 0 -and $symSrvNeg -ge 0 -and $symSrvPos -eq $symSrvNeg) {
    Say "RESULT: red - server symbol count identical ($symSrvPos) in both probes; the CHECKER is broken, not the patch"
    exit 1
}
if ($roleSrvPos -ge 0 -and $roleSrvNeg -ge 0 -and $roleSrvPos -eq $roleSrvNeg) {
    Say "RESULT: red - role literal count identical ($roleSrvPos) in both probes; the CHECKER is broken, not the patch"
    exit 1
}
# -1 is the initial value of these counters and it is NOT a reference count. Printing it raw
# next to "green" is how a phase that never ran reads like a phase that measured zero, so the
# line names the difference instead of showing a number that has none.
function Ref-Str([int]$n) { if ($n -lt 0) { return 'not measured' } return "$n" }
if ($script:rc -eq 0) {
    Say ("RESULT: green - {0} checks, sampling.cpp.obj patched {1} vs pristine {2}, server-context.cpp.obj patched {3} vs pristine {4}" -f `
         $rows.Count, (Ref-Str $symPos), (Ref-Str $symNeg), (Ref-Str $symSrvPos), (Ref-Str $symSrvNeg))
}
else { Say ("RESULT: red - {0} of {1} checks failed, see the table above" -f $summary.red, $rows.Count) }
exit $script:rc
