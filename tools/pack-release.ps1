<#
.SYNOPSIS
Packs the Crow release: the built binaries plus every runtime library they need,
and proves the result is complete before writing the archive.

.DESCRIPTION
The build tree is not shippable on its own. Measured 2026-08-08 (#57): ggml-cuda.dll
imports cublas64_13.dll, which imports cublasLt64_13.dll, and neither is in
bin/Release -- they sit in the installed CUDA toolkit, which an end user does not
have. The MSVC runtime is missing for the same reason.

So the interesting part of this tool is not the copying. It is the completeness
check: every import of every DLL in the package must resolve to a file that is
also in the package, or to a Windows system library. Anything else is a file the
user will be missing, and the tool refuses to write an archive rather than ship one
that fails on somebody else's machine.

.PARAMETER Selftest
Run the checks against synthetic cases, including ones that must fail, and exit.
#>
[CmdletBinding()]
param(
    [string] $BuildDir  = "C:\Users\robin\dev\crow-lab\wt-25\build-25\bin\Release",
    [string] $CudaBin   = "",
    [string] $OutDir    = "",
    [string] $Version   = "",
    [switch] $Selftest
)

$ErrorActionPreference = "Stop"

# Libraries that ship with Windows itself. A dependency resolving to one of these
# is fine; anything else has to be in the package. The api-ms-win-* set is the
# Universal CRT, present on Windows 10 and 11.
$SYSTEM_DLLS = @(
    'kernel32.dll', 'user32.dll', 'advapi32.dll', 'shell32.dll', 'ole32.dll',
    'oleaut32.dll', 'gdi32.dll', 'ws2_32.dll', 'crypt32.dll', 'bcrypt.dll',
    'dbghelp.dll', 'psapi.dll', 'setupapi.dll', 'cfgmgr32.dll', 'winmm.dll',
    'shlwapi.dll', 'version.dll', 'userenv.dll', 'ntdll.dll', 'rpcrt4.dll',
    'nvcuda.dll'      # installed by the NVIDIA display driver, not redistributable
)

function Test-SystemDll {
    param([string] $Name)
    $n = $Name.ToLowerInvariant()
    if ($n -like 'api-ms-win-*') { return $true }
    if ($n -like 'ext-ms-win-*') { return $true }
    return $SYSTEM_DLLS -contains $n
}

function Find-Dumpbin {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) { throw "vswhere.exe not found -- no Visual Studio installation" }
    $hit = & $vswhere -latest -products * -find "**\dumpbin.exe" 2>$null | Select-Object -First 1
    if (-not $hit) { throw "dumpbin.exe not found through vswhere" }
    return $hit
}

function Get-Imports {
    param([string] $Dumpbin, [string] $Path)
    $out = & $Dumpbin /dependents $Path 2>&1
    if ($LASTEXITCODE -ne 0) { throw "dumpbin failed on ${Path}: exit $LASTEXITCODE" }
    # dumpbin indents each imported name by four spaces. Section headers are not
    # indented that way, which is what keeps this from swallowing the summary.
    $names = @()
    foreach ($line in $out) {
        if ($line -match '^\s{4}(\S+\.dll)\s*$') { $names += $Matches[1] }
    }
    return $names
}

<#
    Locates a runtime library on this machine by name. Searches the CUDA toolkit
    and the MSVC redistributable directories -- deliberately NOT the PATH, because
    the PATH is exactly what an end user does not have and searching it would let
    the developer machine answer a question about somebody else's.
#>
function Find-RuntimeLibrary {
    param([string] $Name)

    $roots = @()
    if ($CudaBin) { $roots += $CudaBin }
    $roots += (Get-ChildItem "$env:ProgramFiles\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue |
               Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'bin\x64' })

    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vsRoot = & $vswhere -latest -products * -property installationPath 2>$null
        if ($vsRoot) {
            $roots += (Get-ChildItem (Join-Path $vsRoot 'VC\Redist\MSVC') -Directory -ErrorAction SilentlyContinue |
                       Sort-Object Name -Descending |
                       ForEach-Object { Get-ChildItem $_.FullName -Directory -Filter 'x64.*' -ErrorAction SilentlyContinue } |
                       ForEach-Object { $_.FullName })
        }
    }

    foreach ($r in $roots) {
        if (-not $r -or -not (Test-Path $r)) { continue }
        $hit = Get-ChildItem $r -Recurse -File -Filter $Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

<#
    The completeness check. Returns the list of unresolved imports; empty means
    the package stands on its own.
#>
function Test-PackageComplete {
    param([string] $Dumpbin, [string[]] $Files)

    $present = @{}
    foreach ($f in $Files) { $present[[IO.Path]::GetFileName($f).ToLowerInvariant()] = $true }

    $missing = @()
    foreach ($f in $Files) {
        if ($f -notmatch '\.(dll|exe)$') { continue }
        foreach ($imp in (Get-Imports -Dumpbin $Dumpbin -Path $f)) {
            $k = $imp.ToLowerInvariant()
            if ($present.ContainsKey($k)) { continue }
            if (Test-SystemDll $imp)      { continue }
            $missing += [pscustomobject]@{ Needs = $imp; RequiredBy = [IO.Path]::GetFileName($f) }
        }
    }
    return $missing
}

$script:selftestOk  = 0
$script:selftestRed = 0

function Check {
    param([string] $Name, [bool] $Passed)
    if ($Passed) { Write-Host "  ok   $Name";                          $script:selftestOk++ }
    else         { Write-Host "  FAIL $Name" -ForegroundColor Red;     $script:selftestRed++ }
}

function Invoke-Selftest {

    Write-Host "pack-release selftest"

    # System-library classification, both directions. A classifier that says yes to
    # everything would make the completeness check vacuous, so both are asserted.
    Check "KERNEL32.dll counts as a system library"      (Test-SystemDll 'KERNEL32.dll')
    Check "api-ms-win-crt-math-l1-1-0.dll counts too"    (Test-SystemDll 'api-ms-win-crt-math-l1-1-0.dll')
    Check "nvcuda.dll counts (driver, not redistributable)" (Test-SystemDll 'nvcuda.dll')
    Check "cublas64_13.dll does NOT count"          (-not (Test-SystemDll 'cublas64_13.dll'))
    Check "MSVCP140.dll does NOT count"             (-not (Test-SystemDll 'MSVCP140.dll'))
    Check "ggml-base.dll does NOT count"            (-not (Test-SystemDll 'ggml-base.dll'))

    $dumpbin = Find-Dumpbin
    Check "dumpbin located" ([bool]$dumpbin)

    # Against the real build tree: the package must be INCOMPLETE, because that is
    # exactly the finding this tool exists for. A green result here would mean the
    # check cannot see the problem it was built to catch.
    if (Test-Path $BuildDir) {
        $built  = (Get-ChildItem $BuildDir -File | Where-Object { $_.Extension -in '.dll', '.exe' }).FullName
        $bare   = Test-PackageComplete -Dumpbin $dumpbin -Files $built
        Check "bin\Release alone is incomplete (the #57 finding)" ($bare.Count -gt 0)
        $names  = ($bare.Needs | Sort-Object -Unique)
        Check "cublas64_13.dll is among the missing"  ($names -contains 'cublas64_13.dll')
        Write-Host ("       missing: " + ($names -join ', '))

        # The positive control, and it has to be the real one: add exactly the files
        # the check just named as missing, and the set must come back closed. A
        # checker that only ever reports "incomplete" would pass every case above
        # while being useless for the job it exists to do.
        # Resolving has to iterate: cublas64_13.dll only reveals that it needs
        # cublasLt64_13.dll once it is itself in the set. A single pass would
        # produce a package that is one file short and looks finished.
        $extra  = @()
        $rounds = 0
        $set    = $built
        while ($true) {
            $gap = Test-PackageComplete -Dumpbin $dumpbin -Files $set
            if ($gap.Count -eq 0) { break }
            $rounds++
            if ($rounds -gt 8) { break }
            $added = $false
            foreach ($n in ($gap.Needs | Sort-Object -Unique)) {
                $src = Find-RuntimeLibrary -Name $n
                if ($src -and ($extra -notcontains $src)) { $extra += $src; $set = $built + $extra; $added = $true }
            }
            if (-not $added) { break }
        }

        $final = Test-PackageComplete -Dumpbin $dumpbin -Files ($built + $extra)
        Check "resolving needed more than one pass (transitive imports)" ($rounds -gt 1)
        Check "build tree plus resolved libraries is complete"            ($final.Count -eq 0)
        Write-Host ("       resolved in $rounds rounds, " + $extra.Count + " libraries added:")
        foreach ($e in $extra) { Write-Host ("         " + [IO.Path]::GetFileName($e)) }
        if ($final.Count -gt 0) {
            Write-Host ("       STILL MISSING: " + (($final.Needs | Sort-Object -Unique) -join ', ')) -ForegroundColor Red
        }
    } else {
        Write-Host "  skip build-tree cases -- $BuildDir not present" -ForegroundColor Yellow
    }

    Write-Host ""
    $total = $script:selftestOk + $script:selftestRed
    if ($script:selftestRed -gt 0) {
        Write-Host "RESULT: $($script:selftestRed) of $total FAILED" -ForegroundColor Red
        return 1
    }
    Write-Host "RESULT: SELFTEST OK - $total checks" -ForegroundColor Green
    return 0
}

if ($Selftest) { exit (Invoke-Selftest) }

# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------

if (-not $Version) {
    $cli = Join-Path $PSScriptRoot '..\cli\crow.py'
    $m = Select-String -Path $cli -Pattern '^VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $m) { throw "cannot read VERSION from $cli -- pass -Version" }
    $Version = $m.Matches[0].Groups[1].Value
}
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot "..\dist" }

$repo  = Resolve-Path (Join-Path $PSScriptRoot '..')
$stage = Join-Path $OutDir "crow-$Version-win-x64"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
# Resolve after creating: the manifest strips this prefix off FullName, and an
# unresolved path carrying ".." is a different string from the one Get-ChildItem
# returns -- which is a crash after the copy, at the least useful moment.
$stage  = (Resolve-Path $stage).Path
$OutDir = (Resolve-Path $OutDir).Path

Write-Host "packing crow $Version"
Write-Host "  build : $BuildDir"
Write-Host "  stage : $stage"

if (-not (Test-Path $BuildDir)) { throw "build directory not found: $BuildDir" }
$dumpbin = Find-Dumpbin

# 1 - the built binaries
$binOut = Join-Path $stage 'bin'
New-Item -ItemType Directory -Force -Path $binOut | Out-Null
$built = @()
foreach ($f in Get-ChildItem $BuildDir -File) {
    Copy-Item -LiteralPath $f.FullName -Destination $binOut
    $built += (Join-Path $binOut $f.Name)
}
Write-Host ("  copied $($built.Count) build files")

# 2 - every runtime library they need, resolved transitively
$extra  = @()
$rounds = 0
while ($true) {
    $gap = Test-PackageComplete -Dumpbin $dumpbin -Files (Get-ChildItem $binOut -File).FullName
    if ($gap.Count -eq 0) { break }
    $rounds++
    if ($rounds -gt 8) { throw "dependency resolution did not settle after 8 rounds" }
    $added = $false
    foreach ($n in ($gap.Needs | Sort-Object -Unique)) {
        $src = Find-RuntimeLibrary -Name $n
        if (-not $src) { throw "required library not found on this machine: $n (needed by $(($gap | Where-Object Needs -eq $n).RequiredBy -join ', '))" }
        $dest = Join-Path $binOut ([IO.Path]::GetFileName($src))
        if (-not (Test-Path $dest)) { Copy-Item -LiteralPath $src -Destination $dest; $extra += $dest; $added = $true }
    }
    if (-not $added) { throw "resolution stalled with $($gap.Count) imports unresolved" }
}
Write-Host ("  added $($extra.Count) runtime libraries in $rounds rounds")

# 3 - the client, and the terms it must ship under.
#     Copy-Item -Recurse takes __pycache__ with it, which shipped a
#     crow.cpython-313.pyc into the first package built here -- a compiled
#     artefact of this machine's Python version, useless to anyone else.
Copy-Item -LiteralPath (Join-Path $repo 'cli') -Destination (Join-Path $stage 'cli') -Recurse
Get-ChildItem (Join-Path $stage 'cli') -Recurse -Directory -Filter '__pycache__' |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force }
$stray = Get-ChildItem (Join-Path $stage 'cli') -Recurse -File -Include '*.pyc', '*.pyo'
if ($stray) { throw ("compiled Python left in the package: " + ($stray.Name -join ', ')) }
foreach ($f in @('LICENSE', 'NOTICE', 'README.md')) {
    Copy-Item -LiteralPath (Join-Path $repo $f) -Destination $stage
}

# The OFL is not a formality: without it, redistributing the typeface is a licence
# violation. Refuse rather than ship a package that breaks it.
if (-not (Test-Path (Join-Path $stage 'cli\fonts\OFL.txt'))) {
    throw "cli/fonts/OFL.txt missing from the package -- the typeface may not be redistributed without it"
}

# 4 - the gate: nothing in the package may need something outside it
$final = Test-PackageComplete -Dumpbin $dumpbin -Files (Get-ChildItem $binOut -File).FullName
if ($final.Count -gt 0) {
    throw ("package is incomplete: " + (($final.Needs | Sort-Object -Unique) -join ', '))
}
Write-Host "  completeness: OK"

# 5 - manifest, then archive. The manifest is written before the zip so a crash
#     leaves a staging folder without one, rather than an archive nobody can verify.
$manifest = Get-ChildItem $stage -Recurse -File | ForEach-Object {
    [pscustomobject]@{
        path   = $_.FullName.Substring($stage.Length + 1)
        bytes  = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
}
$manifest | ConvertTo-Json -Depth 3 | Set-Content (Join-Path $stage 'MANIFEST.json') -Encoding utf8

$zip = Join-Path $OutDir "crow-$Version-win-x64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal

$total = ($manifest | Measure-Object bytes -Sum).Sum
Write-Host ""
Write-Host ("RESULT: {0} files, {1:N1} MB staged, {2:N1} MB zipped" -f $manifest.Count, ($total/1MB), ((Get-Item $zip).Length/1MB)) -ForegroundColor Green
Write-Host "  $zip"
exit 0
