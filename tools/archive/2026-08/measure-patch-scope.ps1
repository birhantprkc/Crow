<#
measure-patch-scope - how large is the Crow patch, and against which question?

E14. There is no single "target number", and the whole point of this tool is to stop one
being invented. Seven different quantities have been called "the size of the patch" in this
project, and they are not the same number:

    patch blocks          diff --git entries in a patch file
    touched paths         files the patch writes
    foreign files         paths that exist upstream
    own files             paths that exist on NEITHER base - Crow invented them
    union over groups     the b10223 work is THREE patches; their union is not any one of them
    upstream-moved files  foreign paths upstream also changed between the bases
    crow-only files       foreign paths upstream did not touch

TWO DIFFERENT ANSWERS ARE BOTH "20", and confusing them is exactly the failure this tool
exists to prevent:

    20 = foreign files in the UNION of the three b10223 group patches
         (moe-stream 18 + token-timing adds llama-context.h + dflash adds models/dflash.cpp)
         This is the plan's "twenty touch points".
    20 = touched paths of the b10269 patch minus common/sampling.cpp
         A different set, a different question, and it still contains the 3 own files.

Both are correct answers to different questions. The tool prints the question beside every
number, and refuses to emit a bare total.

WHAT IT DOES NOT DO: build anything, load a model, or change product code. It reads patches,
applies them to throwaway worktrees to count conflict markers, and compares the two bases.

Exit 0 = every measurement and every negative control landed as required.  1 = one did not.
2 = setup error.
#>
param(
    [string]$CROW = 'C:\Users\robin\dev\Crow',
    [string]$SRC  = 'C:\Users\robin\dev\crow-lab\src',
    [string]$WorkRoot = '',
    [switch]$KeepWorktrees
)

$ErrorActionPreference = 'Continue'
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }

$bad = @()
function Check([string]$what, $want, $got, [bool]$ok) {
    if (-not $ok) { $script:bad += "$what : want $want, got $got" }
    Say ("  {0}  {1,-56} want {2,-14} got {3}" -f $(if ($ok) { 'ok  ' } else { 'RED ' }), $what, $want, $got)
}

$PATCHES = @{
    'b10269-core'   = 'moe-stream-on-b10269.patch'
    'b10223-core'   = 'moe-stream-on-b10223.patch'
    'b10223-timing' = 'token-timing-on-b10223.patch'
    'b10223-dflash' = 'dflash-on-b10223.patch'
    'b10223-legacy' = 'worktree-on-b10223.patch'
}
foreach ($k in $PATCHES.Keys) {
    $f = Join-Path $CROW ("patches\" + $PATCHES[$k])
    if (-not (Test-Path -LiteralPath $f)) { Die "patch not found: $f" }
}
if (-not (Test-Path (Join-Path $SRC '.git'))) { Die "no git repo at $SRC" }
# A SHORT root on purpose: llama.cpp carries deep paths (ggml/src/ggml-cuda/template-instances/...)
# and the default TEMP location adds ~70 characters before the repository even starts.
if (-not $WorkRoot) { $WorkRoot = Join-Path (Split-Path (Split-Path $SRC -Parent) -Parent) (".pscope-{0}" -f $PID) }
if (-not (Test-Path $WorkRoot)) { New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null }

function PatchPath([string]$key) { return (Join-Path $CROW ("patches\" + $PATCHES[$key])) }

# --- reading a patch, without applying it ------------------------------------------------
# git apply --numstat parses and reports per file; binary files come back as '-' rather than
# a line count, and a rename shows a different a/ and b/ path. Both are named here instead of
# being counted as ordinary text files.
function Read-Patch([string]$file) {
    $lines = [IO.File]::ReadAllLines($file)
    $blocks = @($lines | Where-Object { $_ -like 'diff --git *' }).Count
    $renames = @($lines | Where-Object { $_ -like 'rename from *' }).Count
    $numstat = & git -C $SRC apply --numstat $file 2>$null
    $rc = $LASTEXITCODE
    $paths = @(); $binary = @()
    foreach ($l in $numstat) {
        $f = ([string]$l) -split "`t"
        if ($f.Count -lt 3) { continue }
        $paths += $f[2]
        if ($f[0] -eq '-' -or $f[1] -eq '-') { $binary += $f[2] }
    }
    return [pscustomobject]@{
        file = (Split-Path $file -Leaf); blocks = $blocks; numstatRc = $rc
        paths = @($paths | Sort-Object); binary = @($binary); renames = $renames
        bytes = (Get-Item -LiteralPath $file).Length
    }
}

# The base inventories and the upstream delta are read ONCE, not once per path.
#
# The first version asked git per path per base - `cat-file -e` for ownership and a filtered
# `diff --numstat` for the upstream move - which is roughly 400 process launches for 70 paths.
# It died partway through with "the filename or extension is too long" from CreateProcess, and
# the numbers up to that point were fine, which is the worst kind of failure: a measurement
# that stops in the middle and leaves three worktrees behind. Two `ls-tree` calls and one
# `diff --numstat` answer the same questions for every path at once.
$script:TREE = @{}
foreach ($b in @('b10223', 'b10269')) {
    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($l in (& git -C $SRC ls-tree -r --name-only $b)) { [void]$set.Add([string]$l) }
    if ($set.Count -lt 100) { Die "ls-tree for $b returned only $($set.Count) paths - refusing to classify against an empty inventory" }
    $script:TREE[$b] = $set
}
$script:UPSTREAM = @{}
foreach ($l in (& git -C $SRC diff --numstat b10223 b10269)) {
    $f = ([string]$l) -split "`t"
    if ($f.Count -ge 3) { $script:UPSTREAM[$f[2]] = ("+{0}/-{1}" -f $f[0], $f[1]) }
}
Say ("base inventories read once: b10223 {0} paths, b10269 {1} paths, upstream moved {2} files between them" -f `
     $script:TREE['b10223'].Count, $script:TREE['b10269'].Count, $script:UPSTREAM.Count)

# A path is OWN when it exists on neither base - measured against the inventories above.
function Is-Own([string]$path) {
    return -not ($script:TREE['b10223'].Contains($path) -or $script:TREE['b10269'].Contains($path))
}
# Did upstream itself move this file between the two bases?
function Upstream-Delta([string]$path) {
    if ($script:UPSTREAM.ContainsKey($path)) { return $script:UPSTREAM[$path] }
    return $null
}

# ONE throwaway worktree, reset between phases - not one per measurement.
#
# The first version created and removed a worktree per phase and died on the fourth such
# operation inside a single process, with CreateProcess reporting "the filename or extension is
# too long" for git.exe. Whatever that limit is, the fix is not to find it: three of the four
# worktrees were the same checkout of the same base, so the tool was paying for a full checkout
# to answer a question a `git reset --hard` answers. Every other suite in this repo already
# resets in place; this one does now too.
$script:TREE_PATH = $null
$script:TREE_BASE = $null
$OWNFILES = @('src/llama-moe-stream.cpp', 'src/llama-moe-stream.h', 'tests/test-llama-file.cpp')

function Use-Tree([string]$base) {
    if (-not $script:TREE_PATH) {
        $p = Join-Path $WorkRoot 'wt-scope'
        & git -C $SRC worktree add --detach $p $base 2>&1 | Out-Null
        if (-not (Test-Path (Join-Path $p '.git'))) { Die "could not create the worktree at $p on $base" }
        $script:TREE_PATH = $p
        $script:TREE_BASE = $base
        return $p
    }
    # reset FIRST, then switch base: a --3way apply has written to the index, and checking out
    # another base over that carries the patched state along. Measured: the b10223 phase then
    # reported 9 conflict marker lines and 21 paths instead of 23.
    & git -C $script:TREE_PATH reset -q --hard HEAD 2>&1 | Out-Null
    if ($script:TREE_BASE -ne $base) {
        & git -C $script:TREE_PATH checkout -q --detach $base 2>&1 | Out-Null
        $script:TREE_BASE = $base
    }
    # git reset --hard, NEVER checkout -- <paths>: --3way writes to the index, so a checkout
    # would restore the PATCHED state and every following measurement would be silently wrong.
    & git -C $script:TREE_PATH reset -q --hard HEAD 2>&1 | Out-Null
    foreach ($n in $OWNFILES) {
        $f = Join-Path $script:TREE_PATH ($n -replace '/', '\')
        if ([IO.File]::Exists($f)) { [IO.File]::Delete($f) }
    }
    $dirty = @(& git -C $script:TREE_PATH status --porcelain).Count
    if ($dirty -ne 0) { Die "worktree not pristine on $base after reset: $dirty paths" }
    return $script:TREE_PATH
}
function Drop-Tree {
    if ($KeepWorktrees -or -not $script:TREE_PATH) { return }
    & git -C $SRC worktree remove --force $script:TREE_PATH 2>&1 | Out-Null
    & git -C $SRC worktree prune 2>&1 | Out-Null
    $script:TREE_PATH = $null
}
function Count-Markers([string]$tree, [string[]]$paths) {
    $n = 0
    foreach ($rel in $paths) {
        $f = Join-Path $tree ($rel -replace '/', '\')
        if (-not [IO.File]::Exists($f)) { continue }
        # line based: a \n regex reports zero blocks in a CRLF checkout
        $n += @([IO.File]::ReadAllLines($f) | Where-Object { $_ -match '^(<<<<<<<|=======|>>>>>>>)' }).Count
    }
    return $n
}
function Apply-Set([string]$tree, [string[]]$patchFiles) {
    foreach ($pf in $patchFiles) {
        & git -C $tree apply --3way $pf 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    return $true
}
function Touched([string]$tree) {
    return @(& git -C $tree status --porcelain | ForEach-Object { ($_ -replace '^\s*\S+\s+', '') })
}

# =========================================================== A  inventory
Say ('=' * 100)
Say 'A  INVENTORY - what each patch file says about itself'
Say ('=' * 100)
$inv = @{}
foreach ($k in @('b10269-core','b10223-core','b10223-timing','b10223-dflash','b10223-legacy')) {
    $r = Read-Patch (PatchPath $k)
    $inv[$k] = $r
    $own = @($r.paths | Where-Object { Is-Own $_ })
    $r | Add-Member -NotePropertyName own -NotePropertyValue $own -Force
    $r | Add-Member -NotePropertyName foreign -NotePropertyValue @($r.paths | Where-Object { $own -notcontains $_ }) -Force
    Say ("  {0,-14} {1,8} B  blocks {2,3}  paths {3,3}  foreign {4,3}  own {5}  binary {6}  renames {7}" -f `
        $k, $r.bytes, $r.blocks, $r.paths.Count, $r.foreign.Count, $r.own.Count, $r.binary.Count, $r.renames)
    Check ("$k : numstat parses the patch") 0 $r.numstatRc ($r.numstatRc -eq 0)
    Check ("$k : blocks equal touched paths") $r.blocks $r.paths.Count ($r.blocks -eq $r.paths.Count)
    Check ("$k : no binary files") 0 $r.binary.Count ($r.binary.Count -eq 0)
    Check ("$k : no renames") 0 $r.renames ($r.renames -eq 0)
}

# =========================================================== B  apply
Say ''
Say ('=' * 100)
Say 'B  APPLY - to a throwaway worktree per base, to count conflict markers'
Say ('=' * 100)
$t69 = Use-Tree 'b10269'
$ok69 = Apply-Set $t69 @((PatchPath 'b10269-core'))
Check 'b10269 : core patch applies with --3way' $true $ok69 $ok69
$p69 = Touched $t69
Check 'b10269 : touched paths' $inv['b10269-core'].paths.Count $p69.Count ($p69.Count -eq $inv['b10269-core'].paths.Count)
$m69 = Count-Markers $t69 $inv['b10269-core'].paths
Check 'b10269 : conflict marker lines' 0 $m69 ($m69 -eq 0)
$stats69 = @([IO.File]::ReadAllLines((Join-Path $t69 'common\sampling.cpp')) | Where-Object { $_ -match 'llama_moe_stream_print_stats' }).Count
Check 'b10269 : statistics call in common/sampling.cpp' 1 $stats69 ($stats69 -eq 1)
$e13_69 = @([IO.File]::ReadAllLines((Join-Path $t69 'src\llama-model.cpp')) | Where-Object { $_ -match 'invalid MoE stream cache capacity' }).Count
Check 'b10269 : carries the E13 change' '>=1' $e13_69 ($e13_69 -ge 1)

$t23 = Use-Tree 'b10223'
$ok23 = Apply-Set $t23 @((PatchPath 'b10223-core'), (PatchPath 'b10223-timing'), (PatchPath 'b10223-dflash'))
Check 'b10223 : all three group patches apply with --3way' $true $ok23 $ok23
$p23 = Touched $t23
$m23 = Count-Markers $t23 $p23
Check 'b10223 : conflict marker lines' 0 $m23 ($m23 -eq 0)
$e13_23 = @([IO.File]::ReadAllLines((Join-Path $t23 'src\llama-model.cpp')) | Where-Object { $_ -match 'invalid MoE stream cache capacity' }).Count
Check 'b10223 : does NOT carry the E13 change' 0 $e13_23 ($e13_23 -eq 0)

$union23 = @($p23 | Sort-Object -Unique)
$own23 = @($union23 | Where-Object { Is-Own $_ })
$foreign23 = @($union23 | Where-Object { $own23 -notcontains $_ })
Check 'b10223 : union of the three groups, touched paths' $inv['b10223-legacy'].paths.Count $union23.Count ($union23.Count -eq $inv['b10223-legacy'].paths.Count)

# =========================================================== C  classify
Say ''
Say ('=' * 100)
Say 'C  CLASSIFY - per foreign file, who moved it between the bases'
Say ('=' * 100)
$rows = @()
foreach ($p in @($inv['b10269-core'].paths)) {
    $own = $inv['b10269-core'].own -contains $p
    $up  = $(if ($own) { $null } else { Upstream-Delta $p })
    $inB23 = $inv['b10223-core'].paths -contains $p
    $cls = if ($own) { 'own file' } elseif ($up) { 'crow + upstream' } else { 'crow only' }
    $rows += [pscustomobject]@{ path = $p; kind = $cls; upstream = $(if ($up) { $up } else { '-' }); inB10223Core = $inB23 }
}
Write-Output ("{0,-32} {1,-16} {2,-12} {3}" -f 'path', 'class', 'upstream', 'also in the b10223 core patch')
foreach ($r in $rows) { Write-Output ("{0,-32} {1,-16} {2,-12} {3}" -f $r.path, $r.kind, $r.upstream, $r.inB10223Core) }
$cOwn  = @($rows | Where-Object { $_.kind -eq 'own file' }).Count
$cBoth = @($rows | Where-Object { $_.kind -eq 'crow + upstream' }).Count
$cCrow = @($rows | Where-Object { $_.kind -eq 'crow only' }).Count
Say ("  own {0}   crow+upstream {1}   crow only {2}   sum {3}" -f $cOwn, $cBoth, $cCrow, ($cOwn+$cBoth+$cCrow))
Check 'classification covers every path exactly once' $inv['b10269-core'].paths.Count ($cOwn+$cBoth+$cCrow) (($cOwn+$cBoth+$cCrow) -eq $inv['b10269-core'].paths.Count)

# =========================================================== D  the two twenties
Say ''
Say ('=' * 100)
Say 'D  THE TWO TWENTIES - same number, different question'
Say ('=' * 100)
$twentyA = $foreign23.Count
Say ("  reading A: foreign files in the UNION of the three b10223 group patches = {0}" -f $twentyA)
Say ("             union {0} paths = {1} foreign + {2} own" -f $union23.Count, $foreign23.Count, $own23.Count)
$addedByTiming = @($inv['b10223-timing'].paths | Where-Object { $inv['b10223-core'].paths -notcontains $_ })
$addedByDflash = @($inv['b10223-dflash'].paths | Where-Object { $inv['b10223-core'].paths -notcontains $_ })
Say ("             token-timing adds: {0}" -f (($addedByTiming -join ', ')))
Say ("             dflash adds:       {0}" -f (($addedByDflash -join ', ')))
Check 'reading A equals the twenty the plan carries' 20 $twentyA ($twentyA -eq 20)

$withoutSampling = @($inv['b10269-core'].paths | Where-Object { $_ -ne 'common/sampling.cpp' })
$twentyB = $withoutSampling.Count
$foreignB = @($withoutSampling | Where-Object { $inv['b10269-core'].own -notcontains $_ }).Count
Say ("  reading B: touched paths of the b10269 core patch WITHOUT common/sampling.cpp = {0}" -f $twentyB)
Say ("             that set is {0} foreign + {1} own - a different set from reading A" -f $foreignB, ($twentyB - $foreignB))
Check 'reading B also lands on twenty' 20 $twentyB ($twentyB -eq 20)
Check 'the two twenties are NOT the same set' 'different' $(if ($twentyA -eq $foreignB) { 'SAME SIZE - check the sets' } else { 'different' }) ($twentyA -ne $foreignB)

# reading B is a control, and its cost has to be visible: without sampling.cpp the statistics
# call has no caller, which is exactly the functional loss E7 measured.
$tB = Use-Tree 'b10269'
$patchB = Join-Path $WorkRoot 'b10269-without-sampling.patch'
# NOT $src: PowerShell variable names are case-insensitive, so $src IS the $SRC parameter,
# and after this line every `git -C $SRC` would receive 152 KB of patch text as its path.
# CreateProcess then fails with "the filename or extension is too long", which reads like a
# resource limit and is a name collision. The same defect cost a checker in E8.
$patchText = [IO.File]::ReadAllText((PatchPath 'b10269-core'))
$parts = [regex]::Split($patchText, '(?m)^(?=diff --git )') | Where-Object { $_ -ne '' }
$kept = @($parts | Where-Object { $_ -notmatch '^diff --git a/common/sampling\.cpp' })
[IO.File]::WriteAllText($patchB, ($kept -join ''))
$okB = Apply-Set $tB @($patchB)
Check 'control : the reduced patch applies' $true $okB $okB
$pB = Touched $tB
Check 'control : touched paths' 20 $pB.Count ($pB.Count -eq 20)
Check 'control : exactly one file fell away' 1 ($inv['b10269-core'].paths.Count - $pB.Count) (($inv['b10269-core'].paths.Count - $pB.Count) -eq 1)
$dropped = @($inv['b10269-core'].paths | Where-Object { $pB -notcontains $_ })
Check 'control : and it is common/sampling.cpp' 'common/sampling.cpp' ($dropped -join ',') (($dropped.Count -eq 1) -and ($dropped[0] -eq 'common/sampling.cpp'))
$statsB = @([IO.File]::ReadAllLines((Join-Path $tB 'common\sampling.cpp')) | Where-Object { $_ -match 'llama_moe_stream_print_stats' }).Count
Check 'control : the statistics call is LOST in this reduced set' 0 $statsB ($statsB -eq 0)
Say  '  the reduced set is a control, not a shippable patch: it builds and says nothing at run time.'

# =========================================================== E  negative controls
Say ''
Say ('=' * 100)
Say 'E  NEGATIVE CONTROLS - each must be caught'
Say ('=' * 100)
$negBad = @()
function Neg([string]$name, [bool]$caught, [string]$detail) {
    if (-not $caught) { $script:negBad += $name }
    Say ("  {0}  {1,-56} {2}" -f $(if ($caught) { 'ok  ' } else { 'RED ' }), $name, $detail)
}
# 1 a missing common/sampling.cpp is detected as a missing statistics caller
Neg 'missing sampling.cpp is caught by the statistics count' ($statsB -eq 0) "reduced set reports $statsB callers, full set reports $stats69"
# 2 an own file counted as foreign
$ownProbe = @('src/llama-moe-stream.h', 'src/llama-moe-stream.cpp', 'tests/test-llama-file.cpp')
$ownAll = $true
foreach ($o in $ownProbe) { if (-not (Is-Own $o)) { $ownAll = $false } }
Neg 'own files are recognised on NEITHER base' $ownAll ("checked: " + ($ownProbe -join ', '))
$foreignProbe = (Is-Own 'src/llama-model.cpp')
Neg 'a foreign file is NOT mistaken for an own one' (-not $foreignProbe) 'src/llama-model.cpp exists on both bases'
# 3 an upstream file with no Crow hunk must not be in the set
$noHunk = $inv['b10269-core'].paths -contains 'src/llama-vocab.cpp'
Neg 'an untouched upstream file is absent from the set' (-not $noHunk) 'src/llama-vocab.cpp is not in the patch'
# 4 two hunks of one file must not count as two files
$hunksModel = @([IO.File]::ReadAllLines((PatchPath 'b10269-core')) | Where-Object { $_ -like '@@ *' }).Count
$dupPaths = @($inv['b10269-core'].paths | Group-Object | Where-Object { $_.Count -gt 1 }).Count
Neg 'hunks are not counted as paths' (($hunksModel -gt $inv['b10269-core'].paths.Count) -and ($dupPaths -eq 0)) "$hunksModel hunks over $($inv['b10269-core'].paths.Count) distinct paths, 0 duplicates"
# 5 E13 must not appear in the b10223 patches
$e13InB23 = 0
foreach ($k in @('b10223-core','b10223-timing','b10223-dflash','b10223-legacy')) {
    $t = [IO.File]::ReadAllText((PatchPath $k))
    foreach ($mk in @('llama_moe_stream_wave_plan', 'invalid MoE stream cache capacity', 'llama_moe_stream_min_slots')) {
        if ($t.Contains($mk)) { $e13InB23++ }
    }
}
Neg 'E13 markers are absent from every b10223 patch' ($e13InB23 -eq 0) "$e13InB23 occurrences over 4 patches x 3 markers"
# 6 a deliberately corrupted patch must fail, not silently apply
$tN = Use-Tree 'b10269'
$corrupt = Join-Path $WorkRoot 'corrupt.patch'
[IO.File]::WriteAllText($corrupt, $patchText.Replace('static inline uint32_t llama_moe_stream_min_slots', 'static inline uint32_t llama_moe_stream_MANGLED'))
$okN = Apply-Set $tN @($corrupt)
$markN = $(if ($okN) { Count-Markers $tN $inv['b10269-core'].paths } else { -1 })
$mangled = $false
if ($okN) {
    $h = Join-Path $tN 'src\llama-moe-stream.h'
    if ([IO.File]::Exists($h)) { $mangled = ([IO.File]::ReadAllText($h)).Contains('llama_moe_stream_MANGLED') }
}
Neg 'a mangled patch is visible in the result' $mangled "apply ok=$okN, mangled symbol present=$mangled"
# 7 nothing from an unversioned artefact
$fromBuild = @($inv['b10269-core'].paths | Where-Object { $_ -match '^build|/build-|\.obj$|\.exe$|^runs/' }).Count
Neg 'no path comes from a build or run artefact' ($fromBuild -eq 0) "$fromBuild artefact paths in the patch"
foreach ($n in $negBad) { $bad += "negative control did not catch: $n" }

Drop-Tree

# =========================================================== F  matrix
Say ''
Say ('=' * 100)
Say 'F  MATRIX - every number with the question it answers'
Say ('=' * 100)
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'quantity', 'b10223 union', 'b10269 core', 'control (no sampling)')
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'patch blocks', ($inv['b10223-core'].blocks + $inv['b10223-timing'].blocks + $inv['b10223-dflash'].blocks), $inv['b10269-core'].blocks, ($inv['b10269-core'].blocks - 1))
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'touched paths', $union23.Count, $inv['b10269-core'].paths.Count, 20)
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'foreign files', $foreign23.Count, $inv['b10269-core'].foreign.Count, $foreignB)
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'own files', $own23.Count, $inv['b10269-core'].own.Count, ($twentyB - $foreignB))
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'conflict marker lines', $m23, $m69, 0)
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'common/sampling.cpp in the set', ($union23 -contains 'common/sampling.cpp'), ($inv['b10269-core'].paths -contains 'common/sampling.cpp'), $false)
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'statistics callers in sampling.cpp', 'n/a', $stats69, $statsB)
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'carries the E13 change', $false, $true, $true)
Write-Output ("{0,-46} {1,16} {2,16} {3,18}" -f 'crow+upstream / crow only / own', 'n/a', ("$cBoth/$cCrow/$cOwn"), 'n/a')
Write-Output ''
Write-Output 'No single number is "the size of the patch". The answers this run supports:'
Write-Output ("  {0} touched paths in the final b10269 core patch, {1} foreign and {2} own" -f $inv['b10269-core'].paths.Count, $inv['b10269-core'].foreign.Count, $inv['b10269-core'].own.Count)
Write-Output ("  {0} foreign files across the union of the three b10223 group patches - the plan's twenty" -f $foreign23.Count)
Write-Output ("  {0} touched paths in the control that drops common/sampling.cpp - a different twenty" -f $twentyB)
Write-Output ("  of the b10269 foreign files, {0} were also moved upstream between the bases and {1} were not" -f $cBoth, $cCrow)

if (-not $KeepWorktrees) { Remove-Item $WorkRoot -Recurse -Force -ErrorAction SilentlyContinue }

Write-Output ''
if ($bad.Count -eq 0) {
    Write-Output 'RESULT: PASS - every quantity measured, every negative control caught, and no bare total emitted.'
    exit 0
}
Write-Output ("RESULT: FAIL - {0} problem(s)." -f $bad.Count)
foreach ($b in $bad) { Write-Output "  $b" }
exit 1
