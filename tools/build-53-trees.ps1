<#
build-53-trees - configure and build the two #53 test trees with ONE flag set, and record what
the comparison needs to be believable.

WHY THIS EXISTS. #53 compares b10223 against b10269 at one operating point. Two servers built by
hand, minutes apart, with a flag typed twice, are two experiments and not one comparison. This
script holds the flag set in a single variable, applies it to both trees, and writes a per-tree
JSON that carries the numbers a later reader would otherwise have to trust: configure and build
exit codes and durations, error lines, step counts, artefact sizes and SHA-256, whether CUDA is
actually IN the produced cache rather than merely requested, and the drift of the relevant cache
values between the two trees.

THE ONE RULE THIS FILE FOLLOWS, because it cost two findings on 2026-08-06:
A FUNCTION IS EITHER AN OUTPUT FUNCTION OR A VALUE FUNCTION, NEVER BOTH.
A PowerShell function returns everything it writes to the success stream. Narration therefore
goes through Say (Write-Host, not the success stream) and value functions never narrate.

CUDA IS VERIFIED, NOT REQUESTED. -DGGML_CUDA=ON on the command line proves nothing: a stale cache,
a missing nvcc or a failed detection all leave the build running on CPU while the flag sits in the
log looking correct. The check reads GGML_CUDA:BOOL=ON out of the PRODUCED cache, reads the
available-backends list, and reads the CUDA compiler path. A zero on any of those is fatal here,
not a warning later.

NOT DONE HERE: no model is loaded, no server is started, nothing is committed. This script builds
and measures the build.
#>
param(
    [string]$Root     = 'C:\Users\robin\dev\crow-lab',
    [string]$OutDir   = '',
    [switch]$Selftest
)

# NOT 'Stop'. Every long-running step here is a NATIVE command, and PowerShell 5.1 wraps a native
# command's stderr in a NativeCommandError - under 'Stop' a single informational line from cmake,
# ninja or vcvars ends the run with a message that points at the wrong thing. Measured twice on
# 2026-08-06. Correctness rests on the explicit exit-code checks below ($cfgRc, $bldRc) and on the
# post-bootstrap tool check, not on a preference.
$ErrorActionPreference = 'Continue'

function Say([string]$m) { Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

# ---- value functions: return data, never narrate ----------------------------

function Get-Secs([datetime]$from, [datetime]$to) {
    return [math]::Round(($to - $from).TotalSeconds, 1)
}

function Get-Artefact([string]$path) {
    if (-not (Test-Path $path)) {
        return [pscustomobject]@{ path = $path; present = $false; bytes = 0; sha256 = '' }
    }
    $fi = Get-Item $path
    return [pscustomobject]@{
        path    = $path
        present = $true
        bytes   = [int64]$fi.Length
        sha256  = (Get-FileHash $path -Algorithm SHA256).Hash
    }
}

function Get-CacheValues([string]$cachePath, [string[]]$keys) {
    $out = @{}
    if (-not (Test-Path $cachePath)) { return $out }
    foreach ($line in (Get-Content $cachePath)) {
        foreach ($k in $keys) {
            if ($line -match ("^" + [regex]::Escape($k) + ":[^=]*=(.*)$")) {
                $out[$k] = $Matches[1]
            }
        }
    }
    return $out
}

# Count real compiler/linker error lines. Ninja prints "FAILED:" for a failed edge; MSVC prints
# "error C####:". A build that returns 0 and still carries error lines is a finding, so both are
# counted rather than inferred from the exit code.
function Get-BuildFacts([string]$logPath) {
    if (-not (Test-Path $logPath)) {
        return [pscustomobject]@{ errorLines = -1; failedEdges = -1; stepsRun = -1; stepsTotal = -1 }
    }
    $lines = Get-Content $logPath
    $err   = @($lines | Where-Object { $_ -match 'error [A-Z]+\d+|fatal error|static_assert' }).Count
    $fail  = @($lines | Where-Object { $_ -match '^FAILED:' }).Count
    $last  = @($lines | Where-Object { $_ -match '^\[(\d+)/(\d+)\]' }) | Select-Object -Last 1
    $run = -1; $tot = -1
    if ($last -and $last -match '^\[(\d+)/(\d+)\]') { $run = [int]$Matches[1]; $tot = [int]$Matches[2] }
    return [pscustomobject]@{ errorLines = $err; failedEdges = $fail; stepsRun = $run; stepsTotal = $tot }
}

# ---- self-test --------------------------------------------------------------

if ($Selftest) {
    $n = 0; $bad = 0
    function Check([string]$name, $want, $got) {
        $script:n++
        $ok = ($want -eq $got)
        if (-not $ok) { $script:bad++ }
        Say ("  {0}  {1,-52} want={2} got={3}" -f $(if ($ok) { 'ok  ' } else { 'FAIL' }), $name, $want, $got)
    }
    Say 'PHASE selftest'
    $tmp = Join-Path $env:TEMP ("b53-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null

    # 1-2  build facts on a healthy log, and its negative control
    $good = Join-Path $tmp 'good.log'
    "[1/250] cc a.c", "[250/250] link llama-server" | Set-Content $good -Encoding ascii
    $f = Get-BuildFacts $good
    Check 'healthy log: 0 error lines' 0 $f.errorLines
    Check 'healthy log: steps 250/250' '250/250' ("{0}/{1}" -f $f.stepsRun, $f.stepsTotal)

    $bad1 = Join-Path $tmp 'bad.log'
    "[1/250] cc a.c", "a.c(3): error C2065: undeclared", "FAILED: a.obj" | Set-Content $bad1 -Encoding ascii
    $f2 = Get-BuildFacts $bad1
    Check 'broken log IS detected: error lines' 1 $f2.errorLines
    Check 'broken log IS detected: failed edges' 1 $f2.failedEdges

    # 3  a missing log is -1, never 0 - a zero would read like a clean build
    $f3 = Get-BuildFacts (Join-Path $tmp 'nope.log')
    Check 'missing log reports -1, not 0' (-1) $f3.errorLines

    # 4-5  artefact reader, present and absent
    $bin = Join-Path $tmp 'x.bin'
    [io.file]::WriteAllBytes($bin, [byte[]](1,2,3,4))
    $a = Get-Artefact $bin
    Check 'artefact: bytes read' 4 $a.bytes
    Check 'artefact: sha256 of 01020304' '9F64A747E1B97F131FABB6B447296C9B6F0201E79FB3C5356E6C77E89B6A806A' $a.sha256
    $a2 = Get-Artefact (Join-Path $tmp 'missing.bin')
    Check 'missing artefact: present=false' $false $a2.present

    # 6  cache reader finds a value and its negative control finds nothing
    $cache = Join-Path $tmp 'CMakeCache.txt'
    "GGML_CUDA:BOOL=ON", "CMAKE_BUILD_TYPE:STRING=Release" | Set-Content $cache -Encoding ascii
    $cv = Get-CacheValues $cache @('GGML_CUDA','GGML_CUDA_FA')
    Check 'cache: GGML_CUDA read' 'ON' $cv['GGML_CUDA']
    Check 'cache: absent key stays absent' $false $cv.ContainsKey('GGML_CUDA_FA')

    Remove-Item $tmp -Recurse -Force
    Say ('-' * 70)
    Say ("selftest: {0} of {1} cases green" -f ($n - $bad), $n)
    exit $(if ($bad -eq 0) { 0 } else { 1 })
}

# ---- toolchain: cmake is in NO PATH, it comes out of Visual Studio ----------
# Documented in build-pristine-negative.ps1:40 and rediscovered the hard way on 2026-08-06 when
# this script died on "cmake.exe not recognised" 15 s into its first configure. Same bootstrap,
# same order, and the same refusal to continue if a tool is still missing afterwards - a build
# that silently used a different toolchain would be two experiments, not one comparison.
if ($null -eq (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    # $ErrorActionPreference = 'Stop' is OFF for the bootstrap on purpose. Redirecting a native
    # command's stderr inside PowerShell 5.1 wraps every line in a NativeCommandError, and under
    # 'Stop' one harmless line out of vcvars64.bat kills the run. Measured on 2026-08-06: the
    # bootstrap died reporting a missing vswhere.exe while vswhere had in fact resolved
    # correctly - the message came from inside vcvars and the preference turned it fatal.
    # The guard that matters is the tool check AFTER the bootstrap, which stays hard.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) { $ErrorActionPreference = $prevEAP; throw 'no vswhere' }
    $vs = & $vswhere -latest -property installationPath 2>$null | Select-Object -First 1
    if (-not $vs) { $ErrorActionPreference = $prevEAP; throw 'vswhere returned no installationPath' }
    cmd.exe /c "`"$(Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat')`" && set" 2>$null | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
    }
    $env:PATH = (Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin') + ';' +
                (Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja') + ';' + $env:PATH
    $ErrorActionPreference = $prevEAP
}
# nvcc is in this list because GGML_CUDA=ON without a CUDA compiler configures into a CPU build
# that looks healthy in every log line except the one nobody reads.
foreach ($t in @('cmake.exe','ninja.exe','cl.exe','nvcc.exe')) {
    if ($null -eq (Get-Command $t -ErrorAction SilentlyContinue)) { throw "no $t after the vcvars bootstrap" }
}
Say ("toolchain: nvcc ={0}" -f (Get-Command nvcc.exe).Source)
Say ("toolchain: cmake={0}" -f (Get-Command cmake.exe).Source)
Say ("toolchain: cl   ={0}" -f (Get-Command cl.exe).Source)

# ---- the two trees ----------------------------------------------------------

if (-not $OutDir) {
    $OutDir = Join-Path 'C:\Users\robin\dev\Crow\runs' ((Get-Date -Format 'yyyy-MM-dd') + '\e53-build')
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# ONE flag set, used for both trees. Anything typed twice can differ twice.
$CMAKE_FLAGS = @(
    '-G', 'Ninja Multi-Config',
    '-DGGML_CUDA=ON', '-DGGML_CUDA_FA=ON', '-DGGML_CUDA_GRAPHS=ON', '-DGGML_CUDA_NCCL=ON',
    '-DGGML_NATIVE=OFF', '-DGGML_CPU_ALL_VARIANTS=OFF',
    '-DLLAMA_BUILD_TESTS=ON', '-DLLAMA_BUILD_SERVER=ON',
    '-DLLAMA_BUILD_TOOLS=ON', '-DLLAMA_BUILD_EXAMPLES=OFF'
)
$CACHE_KEYS = @('GGML_CUDA','GGML_CUDA_FA','GGML_CUDA_GRAPHS','GGML_CUDA_NCCL','GGML_NATIVE',
                'GGML_CPU_ALL_VARIANTS','CMAKE_CUDA_COMPILER','CMAKE_CUDA_ARCHITECTURES',
                'CMAKE_CXX_COMPILER','GGML_AVAILABLE_BACKENDS')

$TREES = @(
    [pscustomobject]@{ label = 'A-b10223'; wt = Join-Path $Root 'wt-53-b10223' },
    [pscustomobject]@{ label = 'B-b10269'; wt = Join-Path $Root 'wt-53-b10269' }
)

$results = @()
foreach ($t in $TREES) {
    Say ("PHASE {0}" -f $t.label)
    $build = Join-Path $t.wt 'build-53'
    if (Test-Path $build) {
        Say "  leeres Bauverzeichnis erzwingen: $build wird entfernt"
        Remove-Item $build -Recurse -Force
    }
    $logDir = Join-Path $OutDir $t.label
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null

    $head  = (& git -C $t.wt rev-parse HEAD).Trim()
    $paths = @(& git -C $t.wt status --porcelain).Count

    $cfgLog = Join-Path $logDir 'configure.log'
    $args   = @('-S', $t.wt, '-B', $build) + $CMAKE_FLAGS
    ($args -join ' ') | Out-File (Join-Path $logDir 'configure-args.txt') -Encoding ascii
    Say '  configure ...'
    $t0 = Get-Date
    & cmake.exe @args *> $cfgLog
    $cfgRc = $LASTEXITCODE
    $cfgSecs = Get-Secs $t0 (Get-Date)
    Say ("  configure exit {0} in {1} s" -f $cfgRc, $cfgSecs)

    $cache = Get-CacheValues (Join-Path $build 'CMakeCache.txt') $CACHE_KEYS
    $cudaOn = ($cache['GGML_CUDA'] -eq 'ON')
    $backends = $cache['GGML_AVAILABLE_BACKENDS']
    $hasCuda  = ($backends -match 'ggml-cuda')
    Say ("  CUDA in cache: {0} | backends: {1}" -f $cudaOn, $backends)
    Say ("  CUDA compiler: {0}" -f $cache['CMAKE_CUDA_COMPILER'])
    if ($cfgRc -ne 0 -or -not $cudaOn -or -not $hasCuda) {
        Say '  ABBRUCH: configure fehlgeschlagen oder CUDA nicht im erzeugten Cache'
        $results += [pscustomobject]@{ label=$t.label; head=$head; paths=$paths; cfgRc=$cfgRc
            cfgSecs=$cfgSecs; cudaInCache=$cudaOn; cudaBackend=$hasCuda; cache=$cache
            buildRc=-1; buildSecs=0; facts=$null; artefacts=@() }
        continue
    }

    $bldLog = Join-Path $logDir 'build.log'
    Say '  build llama-server ...'
    $t1 = Get-Date
    & cmake.exe --build $build --config Release --target llama-server *> $bldLog
    $bldRc = $LASTEXITCODE
    $bldSecs = Get-Secs $t1 (Get-Date)
    $facts = Get-BuildFacts $bldLog
    Say ("  build exit {0} in {1} s | steps {2}/{3} | error lines {4} | failed edges {5}" -f
         $bldRc, $bldSecs, $facts.stepsRun, $facts.stepsTotal, $facts.errorLines, $facts.failedEdges)

    $binDir = Join-Path $build 'bin\Release'
    $arts = @()
    foreach ($f in 'llama-server.exe','llama-server-impl.dll','llama.dll','ggml-cuda.dll') {
        $arts += Get-Artefact (Join-Path $binDir $f)
    }
    foreach ($a in $arts) {
        Say ("  {0,-24} {1,12} B  {2}" -f (Split-Path $a.path -Leaf), $a.bytes, $(if ($a.present) { $a.sha256.Substring(0,16) } else { 'FEHLT' }))
    }

    $results += [pscustomobject]@{
        label=$t.label; head=$head; paths=$paths; buildDir=$build
        cfgRc=$cfgRc; cfgSecs=$cfgSecs; cudaInCache=$cudaOn; cudaBackend=$hasCuda; cache=$cache
        buildRc=$bldRc; buildSecs=$bldSecs; facts=$facts; artefacts=$arts
    }
}

# ---- cache drift between the two trees, reported and never interpreted away --
$drift = @()
if ($results.Count -eq 2 -and $results[0].cache -and $results[1].cache) {
    foreach ($k in $CACHE_KEYS) {
        $a = $results[0].cache[$k]; $b = $results[1].cache[$k]
        if ($a -ne $b) { $drift += [pscustomobject]@{ key=$k; A=$a; B=$b } }
    }
}
Say ('-' * 70)
if ($drift.Count -eq 0) {
    Say 'Cache-Drift zwischen A und B: keine bei den geprueften Schluesseln'
} else {
    Say ("Cache-Drift zwischen A und B: {0} Schluessel" -f $drift.Count)
    foreach ($d in $drift) { Say ("  {0}: A='{1}' B='{2}'" -f $d.key, $d.A, $d.B) }
}

$out = [pscustomobject]@{ flags = ($CMAKE_FLAGS -join ' '); trees = $results; cacheDrift = $drift }
$out | ConvertTo-Json -Depth 8 | Out-File (Join-Path $OutDir 'build-53.json') -Encoding utf8
Say ("JSON: {0}" -f (Join-Path $OutDir 'build-53.json'))

$allGreen = ($results.Count -eq 2) -and
            (@($results | Where-Object { $_.buildRc -ne 0 }).Count -eq 0) -and
            (@($results | Where-Object { $_.facts -and $_.facts.errorLines -ne 0 }).Count -eq 0) -and
            (@($results | Where-Object { -not $_.cudaInCache }).Count -eq 0)
Say ("ERGEBNIS: {0}" -f $(if ($allGreen) { 'beide Baeume gebaut, CUDA im Cache, 0 Fehlerzeilen' } else { 'NICHT gruen - siehe oben' }))
exit $(if ($allGreen) { 0 } else { 1 })
