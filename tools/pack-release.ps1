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

function Get-DevOnlyFiles {
    <#
    Which of these files are developer equipment that must not ship?

    A predicate rather than a literal list at the copy site, and it lives ABOVE
    the selftest exit on purpose. The version this replaces would have made the
    decision below `if ($Selftest) { exit }`, where no check can reach it -- the
    same shape that let install.ps1 report "42 checks pass" on 2026-08-10 while
    the code those checks were written for sat 113 lines further down.

    Matching is on the file NAME, so a path separator or a parent directory
    cannot smuggle one through.

    RETURNS `,@(...)` AND NOT `@(...)`. PowerShell unwraps a one-element array on
    return, and one element is the normal case here -- exactly one suite ships in
    cli/. Without the comma the caller gets a String, `.Count` still answers 1,
    and `[0]` hands back the letter "C" of "C:\...". Caught by the check below on
    2026-08-10, which is the only reason this line reads the way it does.
    #>
    param([string[]] $Paths)
    return ,@($Paths | Where-Object {
        $null -ne $_ -and [System.IO.Path]::GetFileName($_) -like 'test_*.py'
    })
}

<#
    The interpreter the repository checkers run under.

    Resolved rather than assumed, and a miss is an ERROR at the call site rather
    than a skip: a gate that quietly passes when it cannot find Python is a gate
    that passes on exactly the machine where nobody would notice.
#>
function Find-PythonCommand {
    foreach ($c in @('python', 'python3')) {
        $hit = Get-Command $c -ErrorAction SilentlyContinue
        if ($hit) { return ,@($hit.Source) }
    }
    # The launcher needs the version argument; without it a machine with only
    # Python 2 registered would answer.
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) { return ,@($py.Source, '-3') }
    return ,@()
}

<#
    THE GATE, AND IT IS THE REASON check_shared_core.py IS NOT A SECOND OPINION.

    Measured 2026-08-12 while planning #90's E7: check_operating_point.py runs in
    no .github/workflows, no git hook and no call from this script -- it runs only
    when a human thinks of it. A second checker of the same build would inherit
    that defect on the day it was written.

    So both run HERE, before anything is staged. This is the cheapest place there
    is, because every release goes through this script anyway, and it is the only
    path somebody MUST take. A checker that does not run when a package is cut is
    an opinion.

    BEFORE THE STAGE DIRECTORY IS TOUCHED, not after: the caller's demand is that
    a red checker leaves NO stage behind, and a gate that fires after
    `Remove-Item $stage` has already destroyed the previous one.

    Returns the checkers that came back non-zero. -Names and -Root are parameters
    so the selftest can point it at a deliberately red script -- a gate whose own
    failure path is never exercised is the next thing to rot.
#>
function Invoke-RepoCheckers {
    param(
        [string[]] $Names = @('check_shared_core.py', 'check_operating_point.py'),
        [string]   $Root  = $PSScriptRoot
    )

    $prefix = Find-PythonCommand
    if ($prefix.Count -eq 0) {
        throw "no Python interpreter found (tried python, python3, py -3) -- the release checkers cannot run, and a release that skips them is not checked"
    }
    $exe  = $prefix[0]
    $rest = @()
    if ($prefix.Count -gt 1) { $rest = $prefix[1..($prefix.Count - 1)] }

    $red = @()
    foreach ($name in $Names) {
        $tool = Join-Path $Root $name
        if (-not (Test-Path $tool)) { throw "release checker missing: $tool" }
        Write-Host "  $name"
        $argv = $rest + @($tool)
        & $exe @argv | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) { $red += "$name (exit $LASTEXITCODE)" }
    }
    return ,@($red)
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

    # What ships and what does not. Both directions again, because a predicate
    # that says no to everything would quietly put the suite back in the package
    # and this check would still be green.
    $cli = @('C:\r\cli\crow.py', 'C:\r\cli\test_crow.py', 'C:\r\cli\fonts\OFL.txt')
    $dev = Get-DevOnlyFiles -Paths $cli
    Check "the unit suite is developer-only"        ($dev.Count -eq 1 -and $dev[0] -like '*test_crow.py')
    Check "the client itself is NOT"                ($dev -notcontains 'C:\r\cli\crow.py')
    Check "and neither is the font licence"         ($dev -notcontains 'C:\r\cli\fonts\OFL.txt')
    # A nested copy must not slip past on its parent directory.
    Check "a suite in a subdirectory is caught too" ((Get-DevOnlyFiles -Paths @('C:\r\cli\sub\test_x.py')).Count -eq 1)
    Check "an empty list is not an error"           ((Get-DevOnlyFiles -Paths @()).Count -eq 0)

    # The release gate, both directions. A gate that has only ever been seen
    # green is a gate nobody has watched refuse, and refusing is its whole job.
    $python = Find-PythonCommand
    Check "a Python interpreter is available for the checkers" ($python.Count -gt 0)
    if ($python.Count -gt 0) {
        $gateDir = Join-Path ([IO.Path]::GetTempPath()) ("crow-gate-" + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $gateDir | Out-Null
        try {
            Set-Content -LiteralPath (Join-Path $gateDir 'green.py') -Encoding utf8 `
                -Value 'import sys; print("RESULT: fixture agrees"); sys.exit(0)'
            Set-Content -LiteralPath (Join-Path $gateDir 'red.py') -Encoding utf8 `
                -Value 'import sys; print("RESULT: fixture disagrees"); sys.exit(1)'
            $ok  = Invoke-RepoCheckers -Names @('green.py') -Root $gateDir
            Check "a checker that exits 0 does not stop the pack" ($ok.Count -eq 0)
            $bad = Invoke-RepoCheckers -Names @('red.py', 'green.py') -Root $gateDir
            Check "a checker that exits 1 is collected and named" ($bad.Count -eq 1 -and $bad[0] -like 'red.py*')
            # Both checkers run even when the first is red: a gate that stops at
            # the first failure hides the second, and the person fixing this
            # would then cut a package to find out.
            $both = Invoke-RepoCheckers -Names @('red.py', 'red.py') -Root $gateDir
            Check "a red checker does not short-circuit the rest" ($both.Count -eq 2)
        } finally {
            Remove-Item $gateDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    # Named rather than assumed: this is the list the packing path below runs,
    # and #90's E7 exists because check_operating_point.py was in no such list.
    # Read off the function itself, so dropping one from the default list is
    # caught here rather than at the next release nobody checked.
    $gateBody = ${function:Invoke-RepoCheckers}.ToString()
    Check "the gate's default list names check_shared_core.py"     ($gateBody -match 'check_shared_core\.py')
    Check "the gate's default list names check_operating_point.py" ($gateBody -match 'check_operating_point\.py')

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
# 0 - the repository checkers, before one byte is staged
# ---------------------------------------------------------------------------
#
# #90's third done criterion is "shared behaviour lives in exactly one place, and
# there is a check that says so". A check that says so only when a human
# remembers to type it does not say so. This is where it gets said, because a
# release cannot be cut without coming through here.
#
# THE PRICE IS PAID ON PURPOSE, and it is not small: a red checker blocks a
# release, and manifests/shared-core.json is hand-written, so that file now
# stands between robin and a package. The alternative is a criterion that is met
# on paper.
Write-Host "checking the repository before packing"
$redCheckers = Invoke-RepoCheckers
if ($redCheckers.Count -gt 0) {
    throw ("release checkers failed, nothing was staged: " + ($redCheckers -join ', ') +
           " -- fix the finding, or say in the issue why the package ships against it")
}
Write-Host "  checkers: OK"
Write-Host ""

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

# The unit suite is developer equipment, and -Recurse above takes it along: 73,792
# bytes shipped to every user, in a package whose own installer never runs it and
# whose README never mentions it. Nothing outside this repository refers to it.
# It is also the first file the removal path in install.ps1 has to deal with --
# every install from 0.0.1 on has a copy sitting in cli/.
$cliFiles = @(Get-ChildItem (Join-Path $stage 'cli') -Recurse -File | ForEach-Object { $_.FullName })
$devFiles = Get-DevOnlyFiles -Paths $cliFiles
foreach ($p in $devFiles) { Remove-Item -LiteralPath $p -Force }
Write-Host ("  dropped $($devFiles.Count) developer files of $($cliFiles.Count) under cli/")
# Looked at again rather than trusted: a file that failed to delete leaves the
# count above saying it went.
$devLeft = Get-DevOnlyFiles -Paths @(Get-ChildItem (Join-Path $stage 'cli') -Recurse -File | ForEach-Object { $_.FullName })
if ($devLeft.Count -gt 0) { throw ("test code left in the package: " + (($devLeft | Split-Path -Leaf) -join ', ')) }
foreach ($f in @('LICENSE', 'NOTICE', 'README.md')) {
    Copy-Item -LiteralPath (Join-Path $repo $f) -Destination $stage
}

# The chat template is part of the product since 0.1.0: the installer's printed
# server line points at it, and without the file that line fails on start. It
# ships byte-exact -- the golden-vector proof is a byte comparison, and the
# repo's .gitattributes carries -text for the same reason.
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'templates') | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'manifests\0731-chat-template.jinja') `
          -Destination (Join-Path $stage 'templates\0731-chat-template.jinja')
if ((Get-Item (Join-Path $stage 'templates\0731-chat-template.jinja')).Length -ne
    (Get-Item (Join-Path $repo 'manifests\0731-chat-template.jinja')).Length) {
    throw "template changed size on the way into the package -- a byte-checked file may not do that"
}

# THE OPERATING POINT SHIPS SINCE #112, AND THE DIRECTORY NAME IS LOAD-BEARING.
# cli/crow_core.py resolves it as `..\manifests\operating-point.json` from its
# own location, which is the same relative step in the repo and in an install --
# so there is no "am I installed?" branch anywhere, and exactly one path can be
# wrong. Renaming this directory the way the template was renamed to templates/
# would break the client silently: a missing manifest is not an error, it is the
# 0.5.1 behaviour, so the model's own sampling would just quietly stop arriving.
#
# WHY IT SHIPS AT ALL: per-model sampling has to come from data rather than from
# a second set of literals in the client, because tools/check_operating_point.py
# counts the places a default is written and allows exactly one. A table in the
# core spelling min_p twice is the drift this project keeps finding, so the
# numbers that differ per model live here.
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'manifests') | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'manifests\operating-point.json') `
          -Destination (Join-Path $stage 'manifests\operating-point.json')
# Read back as JSON rather than checked for existence: this file is now product,
# and a truncated copy would install cleanly and answer every sampling question
# with the fallback -- which looks exactly like a correct old installation.
try { Get-Content -LiteralPath (Join-Path $stage 'manifests\operating-point.json') -Raw | ConvertFrom-Json | Out-Null }
catch { throw "manifests/operating-point.json did not survive staging as readable JSON: $_" }

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
# {0} IS THE MANIFEST COUNT, NOT THE DIRECTORY COUNT, and the wording says so
# because the two differ by exactly one and that one is not an error: MANIFEST.json
# cannot list and hash itself, so a package of N manifest entries always holds N+1
# files. Read as "N files staged" the line disagrees with `dir` every single time,
# and someone eventually goes looking for the missing file.
Write-Host ("RESULT: {0} files in the manifest (+ MANIFEST.json = {1} in the package), {2:N1} MB staged, {3:N1} MB zipped" -f $manifest.Count, ($manifest.Count + 1), ($total/1MB), ((Get-Item $zip).Length/1MB)) -ForegroundColor Green
Write-Host "  $zip"
exit 0
