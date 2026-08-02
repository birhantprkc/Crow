<#
preserve-build - the return path, made a fact instead of a claim.

WHY THIS EXISTS: the probe-verified llama.cpp binaries live in exactly one place,
`build-native/bin/Release`, and the next ninja run overwrites them. Every later
stage of the load-path work rebuilds that tree. Without a copy taken BEFORE the
first change, "we can go back to b10223" is a sentence nobody has tested.

WHAT IT PRESERVES: the build output plus the source state that produced it. A
binary without its commit is an artefact of unknown origin.

THE ORDER MATTERS, and it is the reverse of the obvious one:
  copy -> hash both sides -> run probes -> ONLY THEN write the manifest.
The manifest is the claim "this is preserved". Written first, a crash mid-copy
leaves a folder that looks complete. Written last, a crash leaves a folder with
no manifest, and the next run redoes it. That is what makes this idempotent.

WHY HASHES AND NOT ONLY PROBES: probes a-e take about 21 minutes against the
155 GB model. If the copy is bit-identical to the source, the probe run already
made against the source applies to it - the hash is the stronger statement and
costs seconds. The probes still run, because they answer a different question:
whether the copy STARTS. A missing DLL is invisible to a per-file hash of the
files that are there.

THE CASE THAT MUST FAIL: point -BuildDir at a directory missing ggml-cuda.dll.
It must come back RED, not green with zero probes. A tool that reports success
when it verified nothing is worse than no tool: it manufactures confidence.
That is why $expected below is a hard list and why a probe count of zero is an
error rather than a pass.

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM
as ANSI, and a stray non-ASCII character breaks the parse in a misleading way.

Usage:
  preserve-build.ps1                                  # preserve the current build
  preserve-build.ps1 -SkipProbes                      # hash only, no model load
  preserve-build.ps1 -BuildDir <dir>                  # the failing case lives here

Exit 0 = preserved and verified.  1 = verification failed.  2 = setup error.
#>
param(
    [string]$BuildDir   = 'C:\Users\robin\dev\crow-lab\src\build-native\bin\Release',
    [string]$SrcDir     = 'C:\Users\robin\dev\crow-lab\src',
    [string]$ArchiveRoot = 'C:\Users\robin\dev\crow-lab\preserved',
    # Which probes to run against the copy. 'abc' is the startability check at 83 s.
    # 'abcde' is the full run at ~21 min and is only needed when the hashes differ.
    [string]$Probes     = 'abc',
    [switch]$SkipProbes,
    [switch]$Force
)

$ErrorActionPreference = 'Continue'

# The full set the linker produces, plus the CUDA runtime that has to sit beside
# it. Measured 2026-08-02: bin/Release carries no CUDA runtime of its own, so
# these three are copied in from the toolkit and belong to the preserved set.
$expected = @(
    'llama-completion.exe',
    'llama-completion-impl.dll',
    'llama-common.dll',
    'llama.dll',
    'ggml.dll',
    'ggml-base.dll',
    'ggml-cpu.dll',
    'ggml-cuda.dll',
    'cudart64_13.dll',
    'cublas64_13.dll',
    'cublasLt64_13.dll'
)

function Fail($msg) { Write-Output "  FAILED: $msg"; return $false }

Write-Output "preserve-build"
Write-Output "  build   $BuildDir"
Write-Output "  source  $SrcDir"
Write-Output ""

if (-not (Test-Path $BuildDir)) { Write-Output "SETUP ERROR: no build dir: $BuildDir"; exit 2 }
if (-not (Test-Path $SrcDir))   { Write-Output "SETUP ERROR: no source dir: $SrcDir"; exit 2 }

# --- source state ------------------------------------------------------------
Push-Location $SrcDir
$sha    = (git rev-parse HEAD 2>$null    | Out-String).Trim()
$short  = (git rev-parse --short HEAD 2>$null | Out-String).Trim()
$tag    = (git describe --tags 2>$null   | Out-String).Trim()
$dirty  = (git status --porcelain 2>$null | Out-String).Trim()
Pop-Location

if ($sha -eq '') { Write-Output "SETUP ERROR: $SrcDir is not a git tree"; exit 2 }
$clean = ($dirty -eq '')
Write-Output "=== source ==="
Write-Output "  commit  $sha"
Write-Output "  tag     $(if ($tag -ne '') { $tag } else { '(none)' })"
Write-Output "  clean   $clean"
if (-not $clean) {
    Write-Output "  NOTE: the tree carries uncommitted changes. The copy is still taken,"
    Write-Output "        but the commit alone does NOT reproduce this build."
}
Write-Output ""

# --- completeness, BEFORE anything is copied ---------------------------------
# This is the check the failing case trips. It runs first on purpose: copying an
# incomplete build and then discovering it is incomplete wastes the copy and,
# worse, leaves a folder that looks preserved.
Write-Output "=== completeness ==="
$missing = @()
foreach ($f in $expected) {
    if (-not (Test-Path (Join-Path $BuildDir $f))) { $missing += $f }
}
if ($missing.Count -gt 0) {
    foreach ($m in $missing) { Write-Output "  MISSING  $m" }
    Write-Output ""
    Write-Output "RESULT: FAIL - $($missing.Count) of $($expected.Count) artefacts absent."
    Write-Output "Nothing was copied. A partial build preserved as complete is the one"
    Write-Output "outcome this tool exists to prevent."
    exit 1
}
Write-Output "  all $($expected.Count) artefacts present"
Write-Output ""

$target = Join-Path $ArchiveRoot "$short-$(if ($tag -ne '') { $tag } else { 'notag' })"
$manifest = Join-Path $target 'PRESERVED.txt'

# --- idempotence -------------------------------------------------------------
if ((Test-Path $manifest) -and (-not $Force)) {
    Write-Output "=== already preserved ==="
    Write-Output "  $target"
    Get-Content $manifest | Select-Object -First 6 | ForEach-Object { "  $_" }
    Write-Output ""
    Write-Output "RESULT: PASS - already preserved, nothing copied. Use -Force to redo."
    exit 0
}

# --- copy --------------------------------------------------------------------
Write-Output "=== copy ==="
if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
foreach ($f in $expected) {
    Copy-Item (Join-Path $BuildDir $f) (Join-Path $target $f) -Force
}
Write-Output "  $($expected.Count) files -> $target"
Write-Output ""

# --- hash both sides ---------------------------------------------------------
# This is the real proof that the probe run made against the source applies to
# the copy. Length alone would not do it: a truncated copy of a DLL can share a
# length with nothing, but a corrupted one can.
Write-Output "=== hashes ==="
$ok = $true
$lines = @()
foreach ($f in $expected) {
    $a = (Get-FileHash (Join-Path $BuildDir $f) -Algorithm SHA256).Hash
    $b = (Get-FileHash (Join-Path $target   $f) -Algorithm SHA256).Hash
    $same = ($a -eq $b)
    if (-not $same) { $ok = Fail "hash differs for $f" }
    $sz = (Get-Item (Join-Path $target $f)).Length
    $lines += ("{0}  {1,12}  {2}" -f $a.Substring(0,16), $sz, $f)
    "  {0}  {1,-26} {2}" -f $(if ($same) { 'OK  ' } else { 'DIFF' }), $f, $a.Substring(0,16)
}
Write-Output ""
if (-not $ok) {
    Write-Output "RESULT: FAIL - the copy is not bit-identical. No manifest written."
    exit 1
}

# --- probes ------------------------------------------------------------------
$probeResult = 'skipped'
if ($SkipProbes) {
    Write-Output "=== probes: SKIPPED by switch ==="
    Write-Output "  The copy is proven identical, but nothing proved it STARTS."
    Write-Output ""
} else {
    Write-Output "=== probes '$Probes' against the copy ==="
    $probeScript = Join-Path $PSScriptRoot 'run-probes.ps1'
    if (-not (Test-Path $probeScript)) { Write-Output "SETUP ERROR: no run-probes.ps1 beside this script"; exit 2 }
    $outDir = Join-Path $target 'probes'
    & $probeScript -Ncmoe 999 -OutDir $outDir -Bin $target -Only $Probes -Label 'preserved' 2>&1 |
        ForEach-Object { "  $_" }
    $rc = $LASTEXITCODE

    # A probe run that produced no output files is NOT a pass. This is the second
    # half of the failing case: green with zero probes is the silent failure.
    $produced = @(Get-ChildItem $outDir -Filter '*.txt' -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -notlike '*.raw.txt' })
    Write-Output ""
    Write-Output "  probe exit $rc, output files: $($produced.Count)"
    if ($produced.Count -eq 0) {
        Write-Output "RESULT: FAIL - the probe run produced no output. A zero without a"
        Write-Output "denominator is not a measurement. No manifest written."
        exit 1
    }
    if ($rc -ne 0) {
        Write-Output "RESULT: FAIL - probes did not pass against the copy. No manifest written."
        exit 1
    }
    $probeResult = "$Probes PASS ($($produced.Count) outputs)"
    Write-Output ""
}

# --- manifest LAST -----------------------------------------------------------
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$header = @(
    "preserved   $stamp",
    "commit      $sha",
    "tag         $(if ($tag -ne '') { $tag } else { '(none)' })",
    "tree clean  $clean",
    "source dir  $SrcDir",
    "build dir   $BuildDir",
    "probes      $probeResult",
    "",
    "sha256 (first 16)   bytes         file"
)
($header + $lines) | Out-File $manifest -Encoding utf8

Write-Output "=== manifest ==="
Write-Output "  $manifest"
Write-Output ""
Write-Output "RESULT: PASS - build preserved and verified."
Write-Output "Return path: copy this folder back, or rebuild from $short and compare hashes."
exit 0
