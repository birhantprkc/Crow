<#
verify-e13-evidence - the three E13 claims that the first pass asserted without evidence.

Written after a review found the E13 report too strong in three places. Each phase here
exists because a specific claim had no checkable backing, and each says plainly what it
measures and what it does not.

  -Phase object   Did the E13 change reach the compiled object, and does a pristine build
                  of the SAME object demonstrably NOT contain it? The first pass reported
                  "patched 1 vs pristine -1", and -1 was the INITIALISER of a variable, not
                  a measurement. A negative probe that never ran must not print a number.

  -Phase clamp    std::clamp(v, lo, hi) with lo > hi is undefined, and lo > hi holds for
                  every model with fewer than 16 experts. The replacement was committed
                  without a single case that exercises it. This extracts the resolver
                  VERBATIM and runs it against n_expert < 16.

  -Phase patch    "IDENTICAL" was reported from two hash files in TEMP that no longer
                  exist. A reconstruction proof that cannot be run again is an anecdote.
                  This applies the patch twice from pristine and prints the hashes.

WHAT THE OBJECT PHASE CANNOT SHOW, and why it does not pretend otherwise:
llama_moe_stream_wave_plan is `static inline` in a header, so it has no external symbol to
count - the tool MEASURES that rather than assuming it, and uses the one artefact the change
does leave in the object: the new error string. Searching for a symbol that cannot exist and
reporting 0 on both sides would be a checker that cannot go red.

Exit 0 = every phase that ran held.  1 = at least one did not.  2 = setup error.
#>
param(
    [string]$WT      = 'C:\Users\robin\dev\crow-lab\wt-e13v',
    [string]$CROW    = 'C:\Users\robin\dev\Crow',
    [string]$BuildDir= 'build-e13v',
    [string]$Base    = 'b10269',
    [string]$Patch   = '',
    [ValidateSet('object', 'clamp', 'patch', 'all')][string]$Phase = 'all',
    [int]   $ExpectPaths = 21,
    [string]$Work    = ''
)

$ErrorActionPreference = 'Continue'
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }

if (-not $Patch) { $Patch = Join-Path $CROW ("patches\moe-stream-on-{0}.patch" -f $Base) }
if (-not (Test-Path -LiteralPath $Patch)) { Die "patch not found: $Patch" }
if (-not (Test-Path (Join-Path $WT '.git'))) { Die "no worktree at $WT (git worktree add --detach $WT $Base)" }
if (-not $Work) { $Work = Join-Path $env:TEMP ("e13-evidence-{0}" -f $PID) }
if (-not (Test-Path $Work)) { New-Item -ItemType Directory -Path $Work -Force | Out-Null }

$BUILD   = Join-Path $WT $BuildDir
$OBJ     = Join-Path $BUILD 'src\CMakeFiles\llama.dir\Release\llama-model.cpp.obj'
$HEADER  = Join-Path $WT 'src\llama-moe-stream.h'
$MODEL   = Join-Path $WT 'src\llama-model.cpp'
$GRAPH   = Join-Path $WT 'src\llama-graph.cpp'
$CTX     = Join-Path $WT 'src\llama-context.cpp'
# The one artefact the E13 change leaves in a compiled object: its new error text.
$MARKER  = 'invalid MoE stream cache capacity'
$INLINEFN= 'llama_moe_stream_wave_plan'

# The three own files of the patch: git reset --hard does not remove them.
$NEWFILES = @('src/llama-moe-stream.cpp', 'src/llama-moe-stream.h', 'tests/test-llama-file.cpp')

$bad = @()
$notes = @()
function Note([string]$phase, [string]$check, $want, $got, [bool]$ok) {
    $script:notes += [pscustomobject]@{ Phase = $phase; Check = $check; Want = $want; Got = $got; OK = $ok }
    if (-not $ok) { $script:bad += "$phase / $check : want $want, got $got" }
    Say ("  {0}  {1,-52} want {2,-22} got {3}" -f $(if ($ok) { 'ok  ' } else { 'RED ' }), $check, $want, $got)
}

function Reset-Pristine {
    & git -C $WT reset -q --hard HEAD
    foreach ($n in $NEWFILES) {
        $f = Join-Path $WT ($n -replace '/', '\')
        if ([IO.File]::Exists($f)) { [IO.File]::Delete($f) }
    }
    return @(& git -C $WT status --porcelain | Where-Object { $_ -notmatch [regex]::Escape($BuildDir) }).Count
}
function Apply-Patch {
    & git -C $WT apply --3way $Patch 2>&1 | Out-Null
    return @(& git -C $WT status --porcelain | Where-Object { $_ -notmatch [regex]::Escape($BuildDir) }).Count
}
# Binary search, not Select-String: -Encoding Byte does not exist in PowerShell 5.1.
function Count-InBinary([string]$Path, [string]$Needle) {
    if (-not [IO.File]::Exists($Path)) { return -1 }
    $bytes = [IO.File]::ReadAllBytes($Path)
    $text  = [Text.Encoding]::ASCII.GetString($bytes)
    $n = 0; $i = 0
    while (($i = $text.IndexOf($Needle, $i)) -ge 0) { $n++; $i += $Needle.Length }
    return $n
}
function Stat-Of([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return 'MISSING' }
    $f = Get-Item -LiteralPath $Path
    return ("{0} B @ {1}" -f $f.Length, $f.LastWriteTime.ToString('HH:mm:ss.fff'))
}
function Sha16([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return 'MISSING' }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.Substring(0,16).ToLower()
}
function Ensure-Toolchain {
    if ($null -ne (Get-Command cmake.exe -ErrorAction SilentlyContinue)) { return }
    $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) { Die 'vswhere not found' }
    $vs = & $vswhere -latest -property installationPath
    cmd /c "`"$vs\VC\Auxiliary\Build\vcvars64.bat`" >nul 2>&1 && set" | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path ("Env:" + $matches[1]) -Value $matches[2] }
    }
    $env:Path = "$vs\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;$vs\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja;$env:Path"
    if ($null -eq (Get-Command cmake.exe -ErrorAction SilentlyContinue)) { Die 'no cmake.exe after the vcvars bootstrap' }
}

# ===================================================================== object
function Phase-Object {
    Say ('=' * 96)
    Say 'PHASE object - does the change reach the object, and is its absence measurable?'
    Say ('=' * 96)
    if (-not (Test-Path $BUILD)) { Die "no configured build dir at $BUILD - this phase builds, it does not configure" }
    Ensure-Toolchain

    # --- patched -------------------------------------------------------------------------
    $dirty = Reset-Pristine
    Note 'object' 'worktree pristine before apply' 0 $dirty ($dirty -eq 0)
    $p = Apply-Patch
    Note 'object' 'touched paths after apply' $ExpectPaths $p ($p -eq $ExpectPaths)

    if ([IO.File]::Exists($OBJ)) { [IO.File]::Delete($OBJ) }
    Note 'object' 'object deleted before the patched build' 'MISSING' (Stat-Of $OBJ) ((Stat-Of $OBJ) -eq 'MISSING')
    & cmake.exe --build $BUILD --config Release --target llama *> (Join-Path $Work 'build-patched.log')
    $rcPos = $LASTEXITCODE
    Note 'object' 'patched build exit code' 0 $rcPos ($rcPos -eq 0)
    if (-not [IO.File]::Exists($OBJ)) { Die "the patched build produced no $OBJ - refusing to report a number for a missing object" }
    $statPos = Stat-Of $OBJ; $shaPos = Sha16 $OBJ
    $mPos = Count-InBinary $OBJ $MARKER
    $sPos = Count-InBinary $OBJ $INLINEFN
    Note 'object' "E13 marker in the patched object" '>=1' $mPos ($mPos -ge 1)

    # --- pristine ------------------------------------------------------------------------
    $dirty2 = Reset-Pristine
    Note 'object' 'worktree pristine again' 0 $dirty2 ($dirty2 -eq 0)
    if ([IO.File]::Exists($OBJ)) { [IO.File]::Delete($OBJ) }
    & cmake.exe --build $BUILD --config Release --target llama *> (Join-Path $Work 'build-pristine.log')
    $rcNeg = $LASTEXITCODE
    Note 'object' 'pristine build exit code' 0 $rcNeg ($rcNeg -eq 0)
    if (-not [IO.File]::Exists($OBJ)) { Die "the pristine build produced no $OBJ - refusing to report a number for a missing object" }
    $statNeg = Stat-Of $OBJ; $shaNeg = Sha16 $OBJ
    $mNeg = Count-InBinary $OBJ $MARKER
    $sNeg = Count-InBinary $OBJ $INLINEFN
    Note 'object' "E13 marker on the pristine base" 0 $mNeg ($mNeg -eq 0)

    # Both sides must really be two different compilations, or the two counts prove nothing.
    Note 'object' 'object genuinely recompiled (size/time differ)' 'differs' ("$statPos | $statNeg") ($statPos -ne $statNeg)
    Note 'object' 'object hashes differ' 'differs' ("$shaPos | $shaNeg") ($shaPos -ne $shaNeg)
    if ($mPos -eq $mNeg) { $script:bad += 'object / patched and pristine report the SAME marker count - this phase measured nothing' }

    # Measured, not assumed: the shared plan is `static inline`, so no external symbol.
    Say ("  note  '{0}' as a symbol in the object: patched {1}, pristine {2} - it is static inline," -f $INLINEFN, $sPos, $sNeg)
    Say  "        so a symbol count is NOT the evidence here; the error string is."

    Reset-Pristine | Out-Null
    Apply-Patch    | Out-Null
    # No return value on purpose. In PowerShell everything a function writes to the pipeline IS
    # its return value, so a caller that discards the result with [void](...) or $null = ...
    # discards the whole PHASE OUTPUT with it. That happened here on the first run: the object
    # phase ran, recorded its 11 checks and printed nothing a reader could see. The project
    # handbook already carried this trap from a different tool; it cost a run anyway.
}

# ====================================================================== clamp
# The resolver is extracted VERBATIM and compiled against minimal stubs. Retyping its
# arithmetic here would test this file's copy of it.
function Extract([string]$Text, [string]$Signature) {
    $i = $Text.IndexOf($Signature)
    if ($i -lt 0) { return $null }
    $open = $Text.IndexOf('{', $i)
    if ($open -lt 0) { return $null }
    $depth = 0
    for ($j = $open; $j -lt $Text.Length; $j++) {
        if ($Text[$j] -eq '{') { $depth++ }
        elseif ($Text[$j] -eq '}') { $depth--; if ($depth -eq 0) { return $Text.Substring($i, $j - $i + 1) } }
    }
    return $null
}

function Phase-Clamp {
    Say ('=' * 96)
    Say 'PHASE clamp - n_expert < 16, where std::clamp(lo > hi) was undefined'
    Say ('=' * 96)
    Ensure-Toolchain
    $dirty = Reset-Pristine
    if ($dirty -ne 0) { Die 'worktree not pristine' }
    [void](Apply-Patch)

    $hdr = [IO.File]::ReadAllText($HEADER)
    $mdl = [IO.File]::ReadAllText($MODEL)
    $fnMin  = Extract $hdr 'static inline uint32_t llama_moe_stream_min_slots'
    $fnRes  = Extract $mdl 'static bool llama_moe_stream_resolve_slots'
    if (-not $fnMin) { Die 'llama_moe_stream_min_slots not found - this suite is stale' }
    if (-not $fnRes) { Die 'llama_moe_stream_resolve_slots not found (or still returns uint32_t) - this suite is stale' }
    Say ("extracted min_slots ({0} chars) and resolve_slots ({1} chars) verbatim" -f $fnMin.Length, $fnRes.Length)

    $cpp = @()
    $cpp += '#include <algorithm>'
    $cpp += '#include <cstdint>'
    $cpp += '#include <cstdio>'
    $cpp += '#include <cstdlib>'
    $cpp += '#include <map>'
    $cpp += '#include <string>'
    # minimal stubs: only the members the resolver touches. weights_map stays EMPTY, so the
    # GiB path degenerates - that is stated in the report, not hidden.
    $cpp += 'struct llama_model_params { uint32_t moe_stream_slots = 0; uint64_t moe_stream_budget = 0; };'
    $cpp += 'struct llama_hparams { uint32_t n_expert = 0; uint32_t n_expert_used = 0; };'
    $cpp += 'struct stub_tensor { long long ne[4] = {0,0,0,0}; };'
    $cpp += 'struct stub_weight { stub_tensor * tensor = nullptr; };'
    $cpp += 'struct llama_model_loader { std::map<std::string, stub_weight> weights_map; };'
    $cpp += 'static bool llama_moe_stream_is_exps_name(const std::string &) { return false; }'
    $cpp += 'static size_t ggml_nbytes(const stub_tensor *) { return 0; }'
    $cpp += 'static int g_warn = 0, g_err = 0;'
    $cpp += '#define LLAMA_LOG_WARN(...)  do { g_warn++; } while (0)'
    $cpp += '#define LLAMA_LOG_ERROR(...) do { g_err++;  } while (0)'
    $cpp += '#define __func__ "resolve"'
    $cpp += $fnMin
    $cpp += $fnRes
    $cpp += @'
int main(int argc, char ** argv) {
    if (argc != 4) { return 2; }
    llama_hparams h; h.n_expert = (uint32_t) atoi(argv[1]); h.n_expert_used = (uint32_t) atoi(argv[2]);
    llama_model_params p; p.moe_stream_slots = (uint32_t) atoi(argv[3]);
    llama_model_loader ml;
    uint32_t out = 0;
    g_warn = 0; g_err = 0;
    const bool ok = llama_moe_stream_resolve_slots(p, h, ml, out);
    printf("%d %u %d %d\n", ok ? 1 : 0, out, g_warn, g_err);
    return 0;
}
'@
    $cppPath = Join-Path $Work 'resolve.cpp'
    $exePath = Join-Path $Work 'resolve.exe'
    [IO.File]::WriteAllLines($cppPath, $cpp)
    Push-Location $Work
    & cl.exe /nologo /std:c++17 /EHsc /Fe:$exePath $cppPath > (Join-Path $Work 'cl-resolve.log') 2>&1
    $rc = $LASTEXITCODE
    Pop-Location
    if ($rc -ne 0) { Die "cl.exe exit $rc compiling the extracted resolver, see $Work\cl-resolve.log" }

    # n_expert, n_expert_used, explicit slots (0 = default), want_ok, want_slots, label
    function CCase([int]$e, [int]$eu, [int]$sl, [int]$wok, [int]$wslots, [string]$label) {
        return [pscustomobject]@{ n_expert=$e; n_eu=$eu; slots=$sl; want_ok=$wok; want_slots=$wslots; label=$label }
    }
    $CC = @(
        (CCase  1 1 0 1 0 'n_expert 1: default lands >= n_expert, streaming disabled')
        (CCase  4 2 0 1 0 'n_expert 4: default lands >= n_expert, streaming disabled')
        (CCase  8 6 0 1 0 'n_expert 8, the old clamp(18,16,8) case: disabled, not undefined')
        (CCase 12 2 0 1 0 'n_expert 12: default lands >= n_expert, streaming disabled')
        (CCase 15 4 0 1 0 'n_expert 15: default lands >= n_expert, streaming disabled')
        (CCase 16 2 0 1 0 'n_expert 16: default 16 covers all experts, disabled')
        (CCase 32 2 0 1 16 'n_expert 32, n_eu 2: default is the 16 floor')
        (CCase 256 6 0 1 18 'the operating point: default is 3*n_expert_used')
        (CCase 256 6 16 0 0 'explicit 16 below the minimum: refused')
        (CCase 256 6 18 1 18 'explicit 18 at the minimum: accepted')
        (CCase 256 6 64 1 64 'explicit 64: accepted unchanged')
        (CCase 256 0 0 1 0 'n_expert_used 0: not a MoE model, disabled without error')
        (CCase 0 6 0 1 0 'n_expert 0: not a MoE model, disabled without error')
    )
    Write-Output ''
    Write-Output ("{0,9} {1,5} {2,7}  {3,-11} {4,-13}  {5}" -f 'n_expert','n_eu','slots','ok w/g','out w/g','case')
    foreach ($c in $CC) {
        $o = & $exePath $c.n_expert $c.n_eu $c.slots
        $f = ([string]$o).Trim() -split '\s+'
        if ($f.Count -lt 4) { Die "the extracted resolver printed '$o'" }
        $gok = [int]$f[0]; $gslots = [int]$f[1]; $gerr = [int]$f[3]
        $good = ($gok -eq $c.want_ok) -and ($gslots -eq $c.want_slots)
        if (-not $good) { $script:bad += ("clamp / {0}: want ok={1} slots={2}, got ok={3} slots={4}" -f $c.label, $c.want_ok, $c.want_slots, $gok, $gslots) }
        if ($c.want_ok -eq 0 -and $gerr -lt 1) { $script:bad += ("clamp / {0}: refused without an error message" -f $c.label) }
        Write-Output ("{0,9} {1,5} {2,7}  {3,-11} {4,-13}  {5}  {6}" -f $c.n_expert, $c.n_eu, $c.slots, "$($c.want_ok)/$gok", "$($c.want_slots)/$gslots", $(if ($good) { 'ok ' } else { 'RED' }), $c.label)
    }

    # Negative control: the committed line put back to std::clamp. With lo > hi that is
    # UNDEFINED, so this control does not assert a value - it records what this compiler did,
    # which is the honest form of "undefined".
    $old = $fnRes.Replace('n_slots = std::min<uint32_t>(std::max<uint32_t>(n_min, 16), hparams.n_expert);',
                          'n_slots = std::clamp<uint32_t>(n_min, 16, hparams.n_expert);')
    if ($old -eq $fnRes) {
        $script:bad += 'clamp / could not inject the old std::clamp line - this control is stale'
    } else {
        $cppList = New-Object Collections.Generic.List[string]
        foreach ($l in $cpp) { if ($l -eq $fnRes) { $cppList.Add($old) } else { $cppList.Add($l) } }
        $cppPath2 = Join-Path $Work 'resolve-oldclamp.cpp'
        $exePath2 = Join-Path $Work 'resolve-oldclamp.exe'
        [IO.File]::WriteAllLines($cppPath2, $cppList)
        Push-Location $Work
        & cl.exe /nologo /std:c++17 /EHsc /Fe:$exePath2 $cppPath2 > (Join-Path $Work 'cl-oldclamp.log') 2>&1
        $rc2 = $LASTEXITCODE
        Pop-Location
        if ($rc2 -ne 0) {
            Say '  note  the old std::clamp form does not compile here - recorded, not asserted'
        } else {
            $diff = 0
            foreach ($c in $CC) {
                $a = (([string](& $exePath  $c.n_expert $c.n_eu $c.slots)).Trim() -split '\s+')[1]
                $b = (([string](& $exePath2 $c.n_expert $c.n_eu $c.slots)).Trim() -split '\s+')[1]
                if ($a -ne $b) { $diff++ }
            }
            Say ("  note  old std::clamp vs new min/max: {0} of {1} cases differ in the resolved slot count" -f $diff, $CC.Count)
            Say  "        lo > hi is UNDEFINED, so equality here is this compiler's behaviour, not a guarantee."
            Say  "        The replacement removes undefined behaviour; it is NOT claimed to change an outcome."
        }
    }
    Reset-Pristine | Out-Null
    Apply-Patch    | Out-Null
}

# ====================================================================== patch
function Phase-Patch {
    Say ('=' * 96)
    Say 'PHASE patch - is the reconstruction reproducible, not a one-off IDENTICAL'
    Say ('=' * 96)
    $files = @('src/llama-moe-stream.h', 'src/llama-graph.cpp', 'src/llama-context.cpp', 'src/llama-model.cpp')
    $markers = @{
        'src/llama-moe-stream.h'  = 'llama_moe_stream_wave_plan'
        'src/llama-graph.cpp'     = 'llama_moe_stream_wave_plan'
        'src/llama-context.cpp'   = 'n_streamed'
        'src/llama-model.cpp'     = 'invalid MoE stream cache capacity'
    }
    $runs = @()
    foreach ($pass in 1, 2) {
        $d = Reset-Pristine
        Note 'patch' "pristine before pass $pass" 0 $d ($d -eq 0)
        $p = Apply-Patch
        Note 'patch' "touched paths, pass $pass" $ExpectPaths $p ($p -eq $ExpectPaths)
        $conf = 0
        foreach ($f in $files) {
            $full = Join-Path $WT ($f -replace '/', '\')
            if (-not [IO.File]::Exists($full)) { Die "missing after apply: $f" }
            $conf += @([IO.File]::ReadAllLines($full) | Where-Object { $_ -match '^(<<<<<<<|=======|>>>>>>>)' }).Count
        }
        Note 'patch' "conflict marker lines, pass $pass" 0 $conf ($conf -eq 0)
        $h = @{}
        foreach ($f in $files) { $h[$f] = Sha16 (Join-Path $WT ($f -replace '/', '\')) }
        $runs += $h
    }
    $same = $true
    Write-Output ''
    Write-Output ("{0,-26} {1,-18} {2,-18} {3}" -f 'file', 'sha16 pass 1', 'sha16 pass 2', 'E13 marker present')
    foreach ($f in $files) {
        $full = Join-Path $WT ($f -replace '/', '\')
        $txt  = [IO.File]::ReadAllText($full)
        $hasMarker = $txt.Contains($markers[$f])
        if (-not $hasMarker) { $script:bad += "patch / $f does not carry its E13 marker '$($markers[$f])'" }
        if ($runs[0][$f] -ne $runs[1][$f]) { $same = $false }
        Write-Output ("{0,-26} {1,-18} {2,-18} {3}" -f $f, $runs[0][$f], $runs[1][$f], $hasMarker)
    }
    Note 'patch' 'two independent applications agree' 'identical' $(if ($same) { 'identical' } else { 'DIFFER' }) $same
    Say  '  the hashes above are the reconstruction proof and are printed on every run,'
    Say  '  so the claim can be checked again instead of quoted from a lost temp file.'
}

# ======================================================================= run
Say ('=' * 96)
Say ("E13 EVIDENCE   worktree {0}   patch {1}   phase {2}" -f $WT, (Split-Path $Patch -Leaf), $Phase)
Say ('=' * 96)
if ($Phase -eq 'object' -or $Phase -eq 'all') { Phase-Object }
if ($Phase -eq 'clamp'  -or $Phase -eq 'all') { Phase-Clamp }
if ($Phase -eq 'patch'  -or $Phase -eq 'all') { Phase-Patch }

Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue
Write-Output ''
if ($bad.Count -eq 0) {
    Write-Output ("RESULT: PASS - {0} checks, every phase that ran carried its own evidence." -f $notes.Count)
    exit 0
}
Write-Output ("RESULT: FAIL - {0} problem(s)." -f $bad.Count)
foreach ($b in $bad) { Write-Output "  $b" }
exit 1
