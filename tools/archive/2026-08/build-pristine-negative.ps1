<#
build-pristine-negative - build a PRISTINE base tree as a genuine negative specimen,
and prove at the object that the fix under test is absent from it.

WHY THIS EXISTS, and it is a measured near-miss, not a precaution. E10 needs a b10223
server that fails with

    key not found in model: dflash.attention.sliding_window_pattern

The only b10223 server on this machine is crow-lab/src/build-native. That build comes
out of the 23-path working tree, and that tree carries patches/dflash-on-b10223.patch -
the patch which turns exactly this key from required into optional and installs
set_swa_pattern(0) as the fallback. Measured 2026-08-05: dflash.cpp.obj in build-native
references set_swa_pattern once, while the pristine b10223 source contains the name zero
times. So that build CANNOT produce the message the negative probe is supposed to show.

    A NEGATIVE PROBE WHOSE SPECIMEN IS ALREADY REPAIRED PROVES NOTHING.

Hence a separate tree, on the bare tag, with no Crow patch applied at all.

WHY NOT verify-patch-b10269.ps1: that tool's first act is git apply, and its whole case
matrix is about a patch being present and effective. Here the case is the exact
opposite - nothing applied, and the invariant is an ABSENCE. An absence needs a
different kind of proof than a presence, which is the next paragraph.

HOW AN ABSENCE IS PROVEN HERE. A counter that reports 0 is indistinguishable from a
counter that is broken - that already happened on 2026-08-05, when Select-String
-SimpleMatch <var> ate the pattern (-SimpleMatch is a SWITCH) and every count came back
0, including counts that could not be 0. Therefore every zero in this tool is paired
with a non-zero from the SAME scanner in the SAME run:

  - source: the pristine 3-argument call must be present (1) while the patched
    4-argument form and set_swa_pattern are absent (0), and the same three counts are
    taken against a reference file that is known to be patched, where they must invert.
  - object: set_swa_pattern must be 0 in the freshly built dflash.cpp.obj, and non-zero
    in deepseek4.cpp.obj from the very same build. One object proves the scanner works,
    the other proves the fix is not there.

TRAPS THIS TOOL IS BUILT AROUND, all of them measured on this machine:
  - cmake is in NO PATH; it comes out of Visual Studio via vcvars64.bat.
  - a pipe swallows the return value. Nothing is piped; exit codes come from
    $LASTEXITCODE right after the call, output goes to a file with *>.
  - Measure-Object -Line counts 0 for an empty line. Line counts come from
    [IO.File]::ReadAllLines().Length.
  - Select-String -Encoding Byte does not exist in PowerShell 5.1. Binary search is
    [IO.File]::ReadAllBytes plus a latin-1 decode.
  - PowerShell variable names are not case sensitive; $UpTo and $UPTO are one variable.
    Nothing here shadows a parameter.
  - a freshly built tree carries NO CUDA runtime. cudart64_13.dll, cublas64_13.dll and
    cublasLt64_13.dll are copied in from the toolkit that was linked against.

WHAT IT DOES NOT DO: no patch, no ctest, no full build, no model is loaded and no token
is produced. Whether the specimen actually fails on the drafter is verify-drafter-load.

Usage:
  build-pristine-negative.ps1
  build-pristine-negative.ps1 -WT <tree> -Base b10223 -BuildDir build-neg
  build-pristine-negative.ps1 -UpTo verify        # invariants only, no build

Exit 0 = every check green.  1 = at least one red.  2 = setup error.
#>
param(
    [string]$WT        = 'C:\Users\robin\dev\crow-lab\wt-e10-neg',
    [string]$CROW      = 'C:\Users\robin\dev\Crow',
    [string]$Base      = 'b10223',
    [string]$BuildDir  = 'build-e10neg',
    [string]$Target    = 'llama-server',
    [ValidateSet('cuda','cpu')][string]$Backend = 'cuda',
    # A tree that is KNOWN to carry the fix. The source counts are taken against it as
    # well, and they must come out inverted - that is what makes the zeros mean
    # something.
    [string]$PatchedRef = 'C:\Users\robin\dev\crow-lab\src\src\models\dflash.cpp',
    [string]$RefCache  = 'C:\Users\robin\dev\crow-lab\src\build-native\CMakeCache.txt',
    [string]$CudaBin   = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin\x64',
    [ValidateSet('verify','configure','build')][string]$UpTo = 'build',
    [string]$LogDir    = ''
)

$ErrorActionPreference = 'Continue'

# Own name plus an assertion, because $UpTo would collide with its own ValidateSet.
$PHASE    = @{ verify = 1; configure = 2; build = 3 }
$UpToRank = $PHASE[$UpTo]
if ($UpToRank -isnot [int]) { Write-Output "SETUP ERROR: phase rank for '$UpTo' is not an int"; exit 2 }

if (-not $LogDir) { $LogDir = Join-Path $CROW ("runs\{0}\e10-neg-{1}" -f (Get-Date -Format 'yyyy-MM-dd'), $Base) }
$BUILD = Join-Path $WT $BuildDir
# The directory is created AFTER the setup checks, not before. Creating it first left an
# empty runs/<date>/e10-neg-b99999 behind when the base-refusal probe was exercised - a
# checker that litters the repository while proving it refuses correctly.
$BIN   = Join-Path $BUILD 'bin\Release'

$script:rows = @()
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Note([string]$phase,[string]$name,$want,$got,[bool]$ok) {
    $script:rows += [pscustomobject]@{ Phase=$phase; Check=$name; Want="$want"; Got="$got"; OK=[bool]$ok }
    Say ("  {0,-5} {1,-52} want {2,-14} got {3}" -f $(if($ok){'ok'}else{'RED'}), $name, "$want", "$got")
}
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }
function Secs($a,$b) { [math]::Round(($b-$a).TotalSeconds,1) }

# --- pure counters: each re-reads its input, nothing is carried forward -------
function Count-In([string]$text,[string]$needle) {
    $n = 0; $i = 0
    while (($i = $text.IndexOf($needle, $i)) -ge 0) { $n++; $i += $needle.Length }
    return $n
}
function Count-File([string]$path,[string]$needle) {
    if (-not [IO.File]::Exists($path)) { return -1 }
    return Count-In ([IO.File]::ReadAllText($path)) $needle
}
function Count-Obj([string]$path,[string]$needle) {
    # Select-String -Encoding Byte does not exist in PS 5.1.
    if (-not [IO.File]::Exists($path)) { return -1 }
    $b = [IO.File]::ReadAllBytes($path)
    return Count-In ([Text.Encoding]::GetEncoding(28591).GetString($b)) $needle
}
function Lines-Of([string]$p) { if ([IO.File]::Exists($p)) { return [IO.File]::ReadAllLines($p) } else { return @() } }

# --- setup -------------------------------------------------------------------
if (-not (Test-Path $WT))         { Die "worktree not found: $WT" }
if (-not (Test-Path $PatchedRef)) { Die "patched reference not found: $PatchedRef (needed as the positive control)" }
$baseSha = (& git -C $WT rev-parse "$Base^{commit}" 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $baseSha) { Die "cannot resolve $Base in $WT" }
$headSha = (& git -C $WT rev-parse HEAD)

# every setup check has passed - only now is anything written to disk
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

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
foreach ($t in @('cmake.exe','ninja.exe')) {
    if ($null -eq (Get-Command $t -ErrorAction SilentlyContinue)) { Die "no $t after the vcvars bootstrap" }
}

$T0 = Get-Date
Say ('=' * 78)
Say "PRISTINE NEGATIVE SPECIMEN   BASE $Base ($baseSha)  TARGET $Target  BACKEND $Backend  UPTO $UpTo"
Say ('=' * 78)

# ======================= 1  the tree is bare =================================
Say 'PHASE verify'
Note 'verify' 'HEAD is the base commit' $baseSha $headSha ($headSha -eq $baseSha)

$dirty = @(& git -C $WT status --porcelain | Where-Object { $_ -notmatch [regex]::Escape($BuildDir) })
Note 'verify' 'no modified or untracked paths (build dir excluded)' 0 $dirty.Count ($dirty.Count -eq 0)
if ($dirty.Count -gt 0) { $dirty | Out-File (Join-Path $LogDir 'dirty.txt') -Encoding ascii }

# ======================= 2  the fix is absent, and the scanner works =========
# REQUIRED semantics is the pristine 3-argument call; the patch replaces it with a
# 4-argument call plus a set_swa_pattern(0) fallback. Both forms are counted in BOTH
# files, so a scanner that always returns 0 fails visibly here rather than silently
# confirming the absence it was asked about.
$DFLASH   = Join-Path $WT 'src\models\dflash.cpp'
$REQUIRED = 'LLM_KV_ATTENTION_SLIDING_WINDOW_PATTERN, hparams.is_swa_impl, hparams.n_layer());'
$OPTIONAL = 'LLM_KV_ATTENTION_SLIDING_WINDOW_PATTERN, hparams.is_swa_impl, hparams.n_layer(), false)'

if (-not [IO.File]::Exists($DFLASH)) { Die "no src/models/dflash.cpp in $WT" }

$specimen = @{
    required = Count-File $DFLASH   $REQUIRED
    optional = Count-File $DFLASH   $OPTIONAL
    swapat   = Count-File $DFLASH   'set_swa_pattern'
    hyper    = Count-File $DFLASH   'LLM_KV_HYPER_CONNECTION_COUNT'
    anchor   = Count-File $DFLASH   'llama_model_dflash'
    never    = Count-File $DFLASH   'ZZZ_never_present_in_any_source'
}
$control = @{
    required = Count-File $PatchedRef $REQUIRED
    optional = Count-File $PatchedRef $OPTIONAL
    swapat   = Count-File $PatchedRef 'set_swa_pattern'
    anchor   = Count-File $PatchedRef 'llama_model_dflash'
}

# scanner self-test first: if these two fail, every count below is meaningless
Note 'verify' 'scanner finds a string that must exist (specimen)'  'ge 1' $specimen.anchor ($specimen.anchor -ge 1)
Note 'verify' 'scanner finds a string that must exist (control)'   'ge 1' $control.anchor  ($control.anchor  -ge 1)
Note 'verify' 'scanner reports 0 for a string that cannot exist'   0      $specimen.never  ($specimen.never  -eq 0)

# the specimen: the fix is not there
Note 'verify' 'REQUIRED-semantics call present in specimen'        1 $specimen.required ($specimen.required -eq 1)
Note 'verify' 'OPTIONAL-semantics fallback ABSENT in specimen'     0 $specimen.optional ($specimen.optional -eq 0)
Note 'verify' 'set_swa_pattern ABSENT in specimen source'          0 $specimen.swapat   ($specimen.swapat   -eq 0)

# the control: the same three counts must invert on a tree that has the fix
Note 'verify' 'control has NO required-semantics call'             0      $control.required ($control.required -eq 0)
Note 'verify' 'control HAS the optional fallback'                  'ge 1' $control.optional ($control.optional -ge 1)
Note 'verify' 'control HAS set_swa_pattern'                        'ge 1' $control.swapat   ($control.swapat  -ge 1)

# reported, not judged: the DSpark/HC branch is a property of the base, not of the fix
Say ("  LLM_KV_HYPER_CONNECTION_COUNT in the specimen source: {0}  (base property, reported not judged)" -f $specimen.hyper)

# ======================= 3  configure ========================================
$cfgRc = -1; $cfgSecs = 0
if ($UpToRank -ge $PHASE['configure']) {
    Say 'PHASE configure'
    $cmakeArgs = @('-S', $WT, '-B', $BUILD, '-G', 'Ninja Multi-Config')
    if ($Backend -eq 'cuda') {
        $cmakeArgs += @('-DGGML_CUDA=ON','-DGGML_CUDA_FA=ON','-DGGML_CUDA_GRAPHS=ON','-DGGML_CUDA_NCCL=ON')
    } else {
        $cmakeArgs += @('-DGGML_CUDA=OFF')
    }
    $cmakeArgs += @('-DGGML_NATIVE=OFF','-DGGML_CPU_ALL_VARIANTS=OFF',
                    '-DLLAMA_BUILD_TESTS=ON','-DLLAMA_BUILD_SERVER=ON',
                    '-DLLAMA_BUILD_TOOLS=ON','-DLLAMA_BUILD_EXAMPLES=OFF')
    ($cmakeArgs -join ' ') | Out-File (Join-Path $LogDir 'configure-args.txt') -Encoding ascii

    $t = Get-Date
    & cmake.exe @cmakeArgs *> (Join-Path $LogDir 'configure.log')
    $cfgRc = $LASTEXITCODE
    $cfgSecs = Secs $t (Get-Date)
    Note 'configure' 'configure exit code' 0 $cfgRc ($cfgRc -eq 0)
    Say "  configure took $cfgSecs s"

    $cfgLog = Lines-Of (Join-Path $LogDir 'configure.log')
    $cudaLines = @($cfgLog | Where-Object { $_ -match 'CUDA|nvcc|cuda architectures|CMAKE_CUDA' })
    $cudaLines | Out-File (Join-Path $LogDir 'configure-cuda.txt') -Encoding ascii
    foreach ($l in $cudaLines) { Say "  cfg| $l" }

    $newCache = Join-Path $BUILD 'CMakeCache.txt'
    $cudaOn   = @(Select-String -Path $newCache -Pattern '^GGML_CUDA:BOOL=ON$').Count
    $backends = @(Select-String -Path $newCache -Pattern '^GGML_AVAILABLE_BACKENDS:INTERNAL=(.*)$' | ForEach-Object { $_.Matches[0].Groups[1].Value })
    $wantCuda = $(if ($Backend -eq 'cuda') { 1 } else { 0 })
    Note 'configure' 'GGML_CUDA=ON in the produced cache' $wantCuda $cudaOn ($cudaOn -eq $wantCuda)
    $hasCuda = $(if ($backends.Count -gt 0 -and $backends[0] -match 'ggml-cuda') { 1 } else { 0 })
    Note 'configure' 'ggml-cuda among the available backends' $wantCuda $hasCuda ($hasCuda -eq $wantCuda)
    Say ("  available backends: {0}" -f ($backends -join ' '))

    # Cache drift against the operational build, reported and never interpreted away.
    # Some drift is EXPECTED here: this is a different base, so upstream option sets can
    # legitimately differ. Reported with names so it can be read, not silently accepted.
    if (Test-Path $RefCache) {
        function Cache-Map([string]$p) {
            $m = @{}
            foreach ($l in (Lines-Of $p)) {
                if ($l -match '^([A-Za-z_0-9]+):(BOOL|STRING|PATH|FILEPATH)=(.*)$') { $m[$Matches[1]] = $Matches[3] }
            }
            return $m
        }
        $a = Cache-Map $RefCache; $b = Cache-Map $newCache
        $diff = @(); $onlyRef = @(); $onlyNew = @()
        foreach ($k in $a.Keys) {
            if ($b.ContainsKey($k)) { if ($a[$k] -ne $b[$k]) { $diff += "$k : ref=$($a[$k]) new=$($b[$k])" } }
            else { $onlyRef += $k }
        }
        foreach ($k in $b.Keys) { if (-not $a.ContainsKey($k)) { $onlyNew += $k } }
        $diffNoPath = @($diff | Where-Object { $_ -notmatch 'ref=[A-Za-z]:/|ref=C:\\|_DIR|PREFIX' })
        ($diff + @('--- only in reference ---') + $onlyRef + @('--- only in new ---') + $onlyNew) |
            Out-File (Join-Path $LogDir 'cache-drift.txt') -Encoding ascii
        Say ("  cache drift vs operational: {0} differing keys ({1} non-path), {2} only-ref, {3} only-new" -f `
             $diff.Count, $diffNoPath.Count, $onlyRef.Count, $onlyNew.Count)
        foreach ($d in $diffNoPath) { Say "  drift| $d" }
    }
}

# ======================= 4  build ============================================
$bldRc = -1; $bldSecs = 0; $errCount = -1
if ($UpToRank -ge $PHASE['build'] -and $cfgRc -eq 0) {
    Say 'PHASE build'
    # Delete first: otherwise the check at the end passes on a file the compiler never
    # wrote. preserve-build.ps1 established this pattern.
    foreach ($n in @('llama-server.exe','llama-server-impl.dll','llama.dll')) {
        $p = Join-Path $BIN $n
        if ([IO.File]::Exists($p)) { [IO.File]::Delete($p) }
    }
    $exeGone = -not [IO.File]::Exists((Join-Path $BIN 'llama-server.exe'))
    Note 'build' 'llama-server.exe removed before the build' 'MISSING' $(if($exeGone){'MISSING'}else{'STILL THERE'}) $exeGone

    $t = Get-Date
    & cmake.exe --build $BUILD --config Release --target $Target *> (Join-Path $LogDir 'build.log')
    $bldRc = $LASTEXITCODE
    $bldSecs = Secs $t (Get-Date)
    Note 'build' 'build exit code' 0 $bldRc ($bldRc -eq 0)
    Say "  build took $bldSecs s"

    $bl = Lines-Of (Join-Path $LogDir 'build.log')
    $errs = @($bl | Where-Object { $_ -match 'error [A-Z]+[0-9]+|LNK[0-9]+: |fatal error' })
    $errCount = $errs.Count
    $errs | Out-File (Join-Path $LogDir 'build-errors.txt') -Encoding ascii
    Note 'build' 'compiler and linker error lines' 0 $errCount ($errCount -eq 0)
    if ($errCount -gt 0) { foreach ($e in $errs) { Say "  err| $e" } }

    # ---- the object-level proof of the absence, with its positive control ----
    $objDflash = Join-Path $BUILD 'src\CMakeFiles\llama.dir\Release\models\dflash.cpp.obj'
    $objCtrl   = Join-Path $BUILD 'src\CMakeFiles\llama.dir\Release\models\deepseek4.cpp.obj'
    $nDflash   = Count-Obj $objDflash 'set_swa_pattern'
    $nCtrl     = Count-Obj $objCtrl   'set_swa_pattern'
    $szD = $(if ([IO.File]::Exists($objDflash)) { (Get-Item $objDflash).Length } else { -1 })
    $szC = $(if ([IO.File]::Exists($objCtrl))   { (Get-Item $objCtrl).Length }   else { -1 })
    Say ("  dflash.cpp.obj    {0} B" -f $szD)
    Say ("  deepseek4.cpp.obj {0} B" -f $szC)
    Note 'build' 'object scanner works (control object references it)' 'ge 1' $nCtrl ($nCtrl -ge 1)
    Note 'build' 'set_swa_pattern ABSENT in built dflash.cpp.obj'      0      $nDflash ($nDflash -eq 0)

    # ---- the CUDA runtime a fresh tree does not carry ------------------------
    $copied = 0
    foreach ($d in @('cudart64_13.dll','cublas64_13.dll','cublasLt64_13.dll')) {
        $dst = Join-Path $BIN $d
        if (-not [IO.File]::Exists($dst)) {
            $srcDll = Join-Path $CudaBin $d
            if ([IO.File]::Exists($srcDll)) { Copy-Item $srcDll $dst -Force; $copied++ }
        }
    }
    $haveRt = @(@('cudart64_13.dll','cublas64_13.dll','cublasLt64_13.dll') | Where-Object { [IO.File]::Exists((Join-Path $BIN $_)) }).Count
    Say ("  CUDA runtime DLLs copied in this run: {0}" -f $copied)
    Note 'build' 'CUDA runtime DLLs next to the exe' 3 $haveRt ($haveRt -eq 3)

    # ---- identity of what was produced --------------------------------------
    $ident = @()
    foreach ($n in @('llama-server.exe','llama-server-impl.dll','llama.dll')) {
        $p = Join-Path $BIN $n
        if ([IO.File]::Exists($p)) {
            $fi = Get-Item $p
            $sha = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash
            $ident += [pscustomobject]@{ name=$n; path=$fi.FullName; bytes=$fi.Length
                                         written=$fi.LastWriteTime.ToString('o'); sha256=$sha }
            Say ("  {0,-24} {1,12} B  {2}  {3}" -f $n, $fi.Length, $fi.LastWriteTime.ToString('HH:mm:ss'), $sha)
        } else {
            Say ("  {0,-24} MISSING" -f $n)
        }
    }
    Note 'build' 'all three artefacts written' 3 $ident.Count ($ident.Count -eq 3)
    $ident | ConvertTo-Json -Depth 4 | Out-File (Join-Path $LogDir 'negative-identity.json') -Encoding ascii

    # ---- smoke, and the negative half of it ---------------------------------
    # This tree must NOT know --moe-stream: it carries no Crow patch. Zero here is a
    # second, independent statement that nothing of ours got into this build.
    $exe = Join-Path $BIN 'llama-server.exe'
    if ([IO.File]::Exists($exe)) {
        $help = & $exe --help 2>&1
        $smokeRc = $LASTEXITCODE
        ($help -join "`n") | Out-File (Join-Path $LogDir 'help.txt') -Encoding ascii
        $opts = @([regex]::Matches(($help -join "`n"), '--moe-stream[a-z-]*') | ForEach-Object { $_.Value } | Sort-Object -Unique)
        $mdOpt = @([regex]::Matches(($help -join "`n"), '--model-draft') | ForEach-Object { $_.Value }).Count
        Note 'build' '--help exit code' 0 $smokeRc ($smokeRc -eq 0)
        Note 'build' 'distinct --moe-stream* options (must be none)' 0 $opts.Count ($opts.Count -eq 0)
        Note 'build' '--model-draft offered (needed by the probe)' 'ge 1' $mdOpt ($mdOpt -ge 1)
    }
}

# ======================= result ==============================================
Write-Output ''
$script:rows | Format-Table Phase, Check, Want, Got, OK -AutoSize | Out-String -Width 200 | Write-Output
$red = @($script:rows | Where-Object { -not $_.OK })
$total = $script:rows.Count
Write-Output ('=' * 78)
Say ("configure {0} s, build {1} s, wall {2} s" -f $cfgSecs, $bldSecs, (Secs $T0 (Get-Date)))
Say ("logs: {0}" -f $LogDir)
if ($red.Count -eq 0) {
    # The claim must not exceed the phase that actually ran. -UpTo verify builds nothing
    # and reads no object, and a summary line that says "built" anyway is exactly the
    # failure this tool exists to prevent, one level up.
    $claim = switch ($UpTo) {
        'verify'    { "fix absent in the SOURCE of pristine $Base. Nothing was configured, built or read at object level." }
        'configure' { "fix absent in the SOURCE of pristine $Base, configuration reproduced. NOTHING WAS BUILT." }
        default     { "pristine $Base specimen built, fix absent at SOURCE and at OBJECT." }
    }
    Write-Output ("RESULT: PASS - {0} of {0} checks green, phase reached '{1}'. {2}" -f $total, $UpTo, $claim)
    exit 0
} else {
    Write-Output ("RESULT: FAIL - {0} of {1} checks red." -f $red.Count, $total)
    foreach ($r in $red) { Write-Output ("  RED  {0} / {1}: want {2}, got {3}" -f $r.Phase, $r.Check, $r.Want, $r.Got) }
    exit 1
}
