<#
Builds bench-loader.exe, the stage 5 measuring instrument.

Why a script and not a bare cl.exe line: the compile needs four include paths, three
source files and a C++17 switch, and getting any one of them wrong produces a linker
error that reads like a missing feature rather than a missing flag.

WHY THREE SOURCES
llama_file is internal and llama.dll does not export it, so llama-mmap.cpp is compiled
into the binary instead of linked - the same reason tests/CMakeLists.txt does it that
way. llama-impl.cpp comes along because llama-mmap.cpp calls two symbols from it,
llama_log_internal() and format(). A dummy logger alone leaves format() unresolved.

WHY ggml-base.lib
Measured on the first build, 2026-08-03: those three sources alone leave 12 unresolved
externals at link time - ggml_abort and ggml_fopen from llama-mmap.cpp, and ggml_time_us,
ggml_log_get, ggml_log_set plus seven gguf_* from llama-impl.cpp. All twelve live in
ggml-base.lib (verified with dumpbin), so the tool links the import library from the
existing native build. Consequence: the binary needs ggml-base.dll beside it at run time,
which is why this script copies it. Second consequence: build-native is overwritten by
the next ninja run, so a rebuilt tree means a rebuilt tool.

WHY /std:c++17
MSVC defaults to C++14. llama.cpp requires 17; without the switch the compile fails in
headers rather than in our code, which points the search at the wrong place.

-MmapDir is the counterproof switch. Point it at a directory holding the ORIGINAL
b10223 llama-mmap.h and llama-mmap.cpp and the build must FAIL: read_raw_at does not
exist there. A build that succeeds against b10223 would mean this tool never touched
the stage 4 work at all.

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI,
and a stray non-ASCII character breaks the parse in a misleading way.

Usage:
  build-bench.ps1                                   # normal build
  build-bench.ps1 -MmapDir <dir> -OutName x.exe     # counterproof against b10223
#>
param(
    [string]$MmapDir = '',
    [string]$OutName = 'bench-loader.exe'
)

$ErrorActionPreference = 'Continue'

$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root     = Join-Path $ToolsDir '..\..\crow-lab\src'
$Root     = [System.IO.Path]::GetFullPath($Root)
$Src      = Join-Path $Root 'src'

if ($MmapDir -eq '') {
    $MmapDir = $Src
} else {
    $MmapDir = [System.IO.Path]::GetFullPath($MmapDir)
}

$OutDir = Join-Path $ToolsDir 'build-bench'
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir | Out-Null
}
$OutExe = Join-Path $OutDir $OutName

# Every input is checked before the compiler is even looked for. A missing path here
# produces a clear line; found later it produces a header error that sends you hunting.
$GgmlLib = Join-Path $Root 'build-native\ggml\src\Release\ggml-base.lib'
$GgmlDll = Join-Path $Root 'build-native\bin\Release\ggml-base.dll'

$needed = @(
    (Join-Path $ToolsDir 'bench-loader.cpp'),
    (Join-Path $MmapDir  'llama-mmap.h'),
    (Join-Path $MmapDir  'llama-mmap.cpp'),
    (Join-Path $Src      'llama-impl.cpp'),
    (Join-Path $Root     'include\llama.h'),
    (Join-Path $Root     'ggml\include\ggml.h'),
    $GgmlLib
)
$missing = @()
foreach ($f in $needed) {
    if (-not (Test-Path $f)) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Output "MISSING INPUTS:"
    foreach ($m in $missing) { Write-Output "  $m" }
    exit 1
}

Write-Output "root      $Root"
Write-Output "mmap from $MmapDir"
Write-Output "output    $OutExe"

# Remove the previous binary FIRST. Otherwise a failed build leaves the old exe in place
# and the check at the end passes on a file the compiler never wrote. Exit 0 lies; the
# object on disk does not.
if (Test-Path $OutExe) { Remove-Item $OutExe -Force }

if ($null -eq (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        # -First 1: vswhere prints one line per installation, and an array here builds a
        # path that cmd.exe then reports as a mistyped command.
        $vsPath = & $vswhere -latest -property installationPath 2>$null | Select-Object -First 1
        $vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
        if (Test-Path $vcvars) {
            Write-Output "loading x64 native tools environment"
            # No stray spaces around the quoted path: cmd.exe reparses the whole /c
            # string, and a leading blank made it report the batch file as a mistyped
            # command on every run - noise that would hide a real failure later.
            cmd.exe /c "`"$vcvars`" && set" 2>$null | ForEach-Object {
                if ($_ -match '^([^=]+)=(.*)$') {
                    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
                }
            }
        }
    }
}
if ($null -eq (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    Write-Output "cl.exe not found and vcvars64.bat could not be loaded."
    exit 1
}

Push-Location $OutDir
# /I order matters: $MmapDir first, so the counterproof picks up the b10223 header
# rather than the patched one sitting next to it.
& cl.exe /nologo /O2 /EHsc /MD /std:c++17 `
    /I $MmapDir /I $Src /I (Join-Path $Root 'include') /I (Join-Path $Root 'ggml\include') `
    (Join-Path $ToolsDir 'bench-loader.cpp') `
    (Join-Path $MmapDir 'llama-mmap.cpp') `
    (Join-Path $Src 'llama-impl.cpp') `
    /link /OUT:$OutExe $GgmlLib
$code = $LASTEXITCODE
Pop-Location

Write-Output ""
Write-Output "cl exit code: $code"

# The verdict is the file, not the exit code.
if (Test-Path $OutExe) {
    $fi = Get-Item $OutExe
    Write-Output "BUILT: $($fi.FullName)  $($fi.Length) bytes  $($fi.LastWriteTime)"
    # The exe imports from ggml-base.dll. Without it beside the binary the tool dies at
    # start with a loader error, which reads like a broken build rather than a missing
    # file.
    if (Test-Path $GgmlDll) {
        Copy-Item $GgmlDll $OutDir -Force
        Write-Output "COPIED: ggml-base.dll beside the binary"
    } else {
        Write-Output "WARNING: ggml-base.dll not found at $GgmlDll - the tool will not start"
    }
    exit 0
} else {
    Write-Output "NO BINARY PRODUCED at $OutExe"
    exit 1
}
