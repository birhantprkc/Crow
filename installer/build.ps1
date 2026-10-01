<#
.SYNOPSIS
Builds installer\dist\CrowSetup.exe: the installer with Python, pip and the two release
packages' fetch list embedded, proven free of the builder's machine (#196, phase 2, P2-T5).

.DESCRIPTION
1. Fetches the Python 3.13 embeddable zip (amd64) and get-pip.py into installer\vendor\,
   each against a sha256 pinned in this script. A file already there with the right sha is
   kept; one with the wrong sha is replaced, and a download with the wrong sha is refused.
2. Writes installer\vendor\packages.json from the two release zips: asset, bytes, sha256
   (lower-case hex), version, url (the GitHub release asset). This is `Packages` in
   installer\core\src\api.rs; the exe embeds it.
3. `cargo build --release -p crowsetup --features bundle` with --remap-path-prefix for the
   profile (the cargo registry lives there), CARGO_HOME if it is elsewhere, and the repo
   root, and `-C target-feature=+crt-static`; the same recipe as crow-nest's
   tools\pack-engine.ps1.
4. Runs `crowsetup.exe --selftest` (exit code 0 is required; no network, no window).
5. The privacy gate over the built exe, and nothing is copied when it refuses. The gate is
   tools\repack-release.py's `privacy_gate` (the same patterns and the same scoped allowlist
   as tools\pack-release.ps1, which is not dot-sourceable: its pipeline starts at the top
   level of the script).
6. Copies the exe to installer\dist\CrowSetup.exe and prints its size and sha256.

Pinning, and why:
- Python: the 3.13.16 embeddable zip. Its sha256 is the one python.org prints on the 3.13.16
  release page, and it is the file the page links (python-3.13.16-embed-amd64.zip).
- get-pip.py: pypa/get-pip at a commit, not bootstrap.pypa.io/get-pip.py. The latter is
  whatever pip is newest today; the former cannot change, so the pinned sha256 keeps
  matching and a changed byte is an attack or a mistake, never a new pip release. The commit
  carries pip 26.2.1.
Moving either pin is a deliberate edit of the constants below.

.PARAMETER CrowZip
Crow's release package, crow-<version>-win-x64.zip (tools\pack-release.ps1).

.PARAMETER EngineZip
The engine package, crow-nest-engine-<version>-win-x64.zip (crow-nest tools\pack-engine.ps1).

.PARAMETER Version
Crow's version, for a package file whose name carries none. When the name carries one, a
different -Version is refused. The engine's version is always read from its file name.

.PARAMETER Selftest
Check this script's own logic on synthetic inputs, including ones that must fail. No network,
no cargo.
#>
[CmdletBinding()]
param(
    [string] $CrowZip   = "",
    [string] $EngineZip = "",
    [string] $Version   = "",
    [switch] $Selftest
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

# python.org/downloads/release/python-31316/ (2026-09-30), "Windows embeddable package (64-bit)"
$PY_VERSION = '3.13.16'
$PY_URL     = 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip'
$PY_SHA256  = '97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297'
# the name the exe embeds (installer\app), without the patch number
$PY_FILE    = 'python-3.13-embed-amd64.zip'

# github.com/pypa/get-pip, main at the merge of "Update to 26.2.1" (2026-08-04)
$GETPIP_URL    = 'https://raw.githubusercontent.com/pypa/get-pip/f6f644156f23dfe9acc06e7b9ca75eee311f2e37/public/get-pip.py'
$GETPIP_SHA256 = 'fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6'
$GETPIP_FILE   = 'get-pip.py'

$CROW_RELEASES   = 'https://github.com/nibor1896/Crow/releases/download'
$ENGINE_RELEASES = 'https://github.com/nibor1896/crow-nest/releases/download'

# 2.8.5, or 0.7.2-3-g1a2b3c4 for an engine zip packed between tags
$VERSION_RE = '\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)*'

# ---------------------------------------------------------------------------
# Functions (the selftest calls exactly these)
# ---------------------------------------------------------------------------

function Get-Sha256Hex {
    param([string] $Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-Download {
    param([string] $Url, [string] $OutFile)
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $saved = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'    # the progress bar makes Windows PowerShell 5.1 download slowly
    try { Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing }
    finally { $ProgressPreference = $saved }
}

function Get-PinnedFile {
    # $true when the file was fetched, $false when the one on disk already had the pinned sha256.
    # A file that does not match is never left at $Dest.
    param(
        [string] $Url, [string] $Dest, [string] $Sha256,
        [scriptblock] $Fetch = { param($u, $o) Invoke-Download -Url $u -OutFile $o }
    )
    if (Test-Path -LiteralPath $Dest) {
        if ((Get-Sha256Hex $Dest) -eq $Sha256) { return $false }
        Write-Host "  $Dest does not match the pinned sha256, fetching it again"
        Remove-Item -LiteralPath $Dest -Force
    }
    $part = "$Dest.part"
    if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force }
    & $Fetch $Url $part
    $got = Get-Sha256Hex $part
    if ($got -ne $Sha256) {
        Remove-Item -LiteralPath $part -Force
        throw "$Url has sha256 $got, pinned $Sha256 - refusing it"
    }
    Move-Item -LiteralPath $part -Destination $Dest
    return $true
}

function Assert-PinnedFile {
    # The file on disk is the pinned one. Run again right before the build: a vendor file
    # swapped between the fetch and the build must not end up in the exe.
    param([string] $Path, [string] $Sha256)
    if (-not (Test-Path -LiteralPath $Path)) { throw "$Path is missing" }
    $got = Get-Sha256Hex $Path
    if ($got -ne $Sha256) { throw "$Path has sha256 $got, pinned $Sha256 - refusing to build with it" }
}

function Get-PackageInfo {
    # One entry of packages.json. The version comes from the file name; -Version only fills a
    # name that carries none (Crow) and must agree with one that does.
    param([string] $Zip, [ValidateSet('crow', 'engine')] [string] $Kind, [string] $Version = "")
    if (-not (Test-Path -LiteralPath $Zip -PathType Leaf)) { throw "$Kind package not found: $Zip" }
    $asset = Split-Path -Leaf $Zip
    $re = if ($Kind -eq 'crow') { "^crow-(?<v>$VERSION_RE)-win-x64\.zip$" } else { "^crow-nest-engine-(?<v>$VERSION_RE)-win-x64\.zip$" }
    $m = [regex]::Match($asset, $re)
    $ver = $null
    if ($m.Success) { $ver = $m.Groups['v'].Value }
    if ($Version) {
        if ($ver -and $ver -ne $Version) { throw "-Version $Version but $asset says $ver" }
        $ver = $Version
    }
    if (-not $ver) {
        $want = if ($Kind -eq 'crow') { 'crow-<version>-win-x64.zip' } else { 'crow-nest-engine-<version>-win-x64.zip' }
        throw "cannot read a version from '$asset' (expected $want)"
    }
    $base = if ($Kind -eq 'crow') { $CROW_RELEASES } else { $ENGINE_RELEASES }
    return [ordered]@{
        asset   = $asset
        url     = "$base/v$ver/$asset"
        bytes   = [int64](Get-Item -LiteralPath $Zip).Length
        sha256  = Get-Sha256Hex $Zip
        version = $ver
    }
}

function Write-PackagesJson {
    # `Packages` in installer\core\src\api.rs. UTF-8 without a BOM: serde_json rejects one.
    param([string] $CrowZip, [string] $EngineZip, [string] $Version, [string] $OutFile)
    $pk = [ordered]@{
        crow   = Get-PackageInfo -Zip $CrowZip   -Kind crow   -Version $Version
        engine = Get-PackageInfo -Zip $EngineZip -Kind engine
    }
    $json = $pk | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($OutFile, $json + "`n", (New-Object Text.UTF8Encoding($false)))
    return $pk
}

function Get-BuildFlags {
    # The last remap that matches wins, and the repo lies inside the profile, so the repo
    # comes last. CARGO_HOME gets its own remap only when it is not under the profile.
    param([string] $Repo, [string] $ProfileDir, [string] $CargoHome = "")
    $flags = @("--remap-path-prefix=$ProfileDir=~")
    if ($CargoHome -and -not $CargoHome.StartsWith($ProfileDir, [StringComparison]::OrdinalIgnoreCase)) {
        $flags += "--remap-path-prefix=$CargoHome=cargo"
    }
    $flags += "--remap-path-prefix=$Repo=crow"
    $flags += '-Ctarget-feature=+crt-static'
    return ,$flags
}

function Find-Python {
    # The interpreter that runs the privacy gate: a command and the arguments before the script.
    foreach ($c in @(@('python'), @('py', '-3'))) {
        $cmd = Get-Command $c[0] -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $pre = @($c | Select-Object -Skip 1)
        $out = & $cmd.Source @pre --version
        if ("$out" -match '^Python 3\.') { return [pscustomobject]@{ Exe = $cmd.Source; Pre = $pre } }
    }
    throw "no Python 3 found (python, py -3): the privacy gate is Python (tools\repack-release.py)"
}

# privacy_gate() takes {path: bytes}; the exe is one file, scanned in memory.
$GATE_CODE = "import importlib.util,sys;s=importlib.util.spec_from_file_location('rr',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);sys.exit(0 if m.privacy_gate({'CrowSetup.exe':open(sys.argv[2],'rb').read()}) else 1)"

function Invoke-ExeGate {
    # $true when the exe is clean. Prints every hit; the caller must refuse otherwise.
    param([string] $Exe, [string] $Repo, [switch] $Quiet)
    $py = Find-Python
    $gate = Join-Path $Repo 'tools\repack-release.py'
    if (-not (Test-Path -LiteralPath $gate)) { throw "$gate not found" }
    $savedEnc = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = 'utf-8'
    try { $out = & $py.Exe @($py.Pre) -c $GATE_CODE $gate $Exe }
    finally { $env:PYTHONIOENCODING = $savedEnc }
    $code = $LASTEXITCODE
    if (-not $Quiet) { foreach ($l in $out) { Write-Host "  $l" } }
    return ($code -eq 0)
}

function Invoke-ExeSelftest {
    # Exit code and output of `<exe> <args>`. Start-Process, not &: a GUI-subsystem exe is not
    # waited for by & and has no console to write to.
    param([string] $Exe, [string[]] $ExeArgs = @('--selftest'))
    $o = [IO.Path]::GetTempFileName(); $e = [IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $ExeArgs -Wait -PassThru -NoNewWindow `
            -RedirectStandardOutput $o -RedirectStandardError $e
        $text = @(Get-Content -LiteralPath $o) + @(Get-Content -LiteralPath $e)
        return [pscustomobject]@{ Code = $p.ExitCode; Text = [string[]]$text }
    } finally { Remove-Item -LiteralPath $o, $e -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

$script:selftestOk  = 0
$script:selftestRed = 0

function Check {
    param([string] $Name, [bool] $Passed)
    if ($Passed) { Write-Host "  ok   $Name";                      $script:selftestOk++ }
    else         { Write-Host "  FAIL $Name" -ForegroundColor Red; $script:selftestRed++ }
}

function Test-Throws {
    param([scriptblock] $Body, [string] $Like = '*')
    try { & $Body | Out-Null } catch { return ($_.Exception.Message -like $Like) }
    return $false
}

function New-TestZip {
    param([string] $Path, [string] $Content)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::Open($Path, 'Create')
    try {
        $en = $z.CreateEntry('MANIFEST.json')
        $w = New-Object IO.StreamWriter($en.Open())
        $w.Write($Content); $w.Dispose()
    } finally { $z.Dispose() }
}

function Get-Sha256OfBytes {
    param([byte[]] $Bytes)
    $h = [Security.Cryptography.SHA256]::Create()
    try { return (($h.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') } finally { $h.Dispose() }
}

function Invoke-Selftest {
    $repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $tmp  = Join-Path ([IO.Path]::GetTempPath()) ("crowsetup-build-selftest-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        Write-Host "pins"
        Check "python sha256 is 64 lower-case hex"  ($PY_SHA256 -match '^[0-9a-f]{64}$')
        Check "get-pip sha256 is 64 lower-case hex" ($GETPIP_SHA256 -match '^[0-9a-f]{64}$')
        Check "python url is versioned and https"   ($PY_URL -like "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-embed-amd64.zip")
        Check "get-pip url names a commit"          ($GETPIP_URL -match '^https://raw\.githubusercontent\.com/pypa/get-pip/[0-9a-f]{40}/public/get-pip\.py$')
        Check "vendor names are the ones the exe embeds" ($PY_FILE -eq 'python-3.13-embed-amd64.zip' -and $GETPIP_FILE -eq 'get-pip.py')

        Write-Host "packages.json"
        $crow   = Join-Path $tmp 'crow-9.9.9-win-x64.zip'
        $engine = Join-Path $tmp 'crow-nest-engine-1.2.3-win-x64.zip'
        New-TestZip $crow   'crow'
        New-TestZip $engine 'engine'
        $out = Join-Path $tmp 'packages.json'
        $pk = Write-PackagesJson -CrowZip $crow -EngineZip $engine -Version "" -OutFile $out
        $raw = [IO.File]::ReadAllBytes($out)
        Check "no BOM (serde_json rejects one)" (-not ($raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF))
        $j = (Get-Content -LiteralPath $out -Raw) | ConvertFrom-Json
        Check "top level is exactly crow, engine" ((@($j.PSObject.Properties.Name) -join ',') -eq 'crow,engine')
        $want = 'asset,url,bytes,sha256,version'
        Check "crow has exactly asset, url, bytes, sha256, version"   ((@($j.crow.PSObject.Properties.Name) -join ',') -eq $want)
        Check "engine has exactly asset, url, bytes, sha256, version" ((@($j.engine.PSObject.Properties.Name) -join ',') -eq $want)
        Check "crow asset and version from the file name" ($j.crow.asset -eq 'crow-9.9.9-win-x64.zip' -and $j.crow.version -eq '9.9.9')
        Check "engine asset and version from the file name" ($j.engine.asset -eq 'crow-nest-engine-1.2.3-win-x64.zip' -and $j.engine.version -eq '1.2.3')
        Check "crow url" ($j.crow.url -ceq 'https://github.com/nibor1896/Crow/releases/download/v9.9.9/crow-9.9.9-win-x64.zip')
        Check "engine url" ($j.engine.url -ceq 'https://github.com/nibor1896/crow-nest/releases/download/v1.2.3/crow-nest-engine-1.2.3-win-x64.zip')
        Check "bytes are the file lengths, as numbers" ($j.crow.bytes -eq (Get-Item $crow).Length -and $j.engine.bytes -eq (Get-Item $engine).Length -and ($j.crow.bytes -is [long] -or $j.crow.bytes -is [int]))
        Check "sha256 is lower-case and independent of Get-FileHash" (
            $j.crow.sha256 -ceq (Get-Sha256OfBytes ([IO.File]::ReadAllBytes($crow))) -and
            $j.engine.sha256 -ceq (Get-Sha256OfBytes ([IO.File]::ReadAllBytes($engine))) -and
            $j.crow.sha256 -cmatch '^[0-9a-f]{64}$')
        Check "-Version that agrees with the file name is fine" ((Get-PackageInfo -Zip $crow -Kind crow -Version '9.9.9').version -eq '9.9.9')
        Check "-Version that disagrees is refused" (Test-Throws { Get-PackageInfo -Zip $crow -Kind crow -Version '1.0.0' } '*says 9.9.9*')
        $bare = Join-Path $tmp 'crow-package.zip'; New-TestZip $bare 'x'
        Check "a name without a version needs -Version" (Test-Throws { Get-PackageInfo -Zip $bare -Kind crow } '*cannot read a version*')
        Check "...and takes it" ((Get-PackageInfo -Zip $bare -Kind crow -Version '3.0.0').url -ceq 'https://github.com/nibor1896/Crow/releases/download/v3.0.0/crow-package.zip')
        Check "the engine zip is not accepted as Crow's package" (Test-Throws { Get-PackageInfo -Zip $engine -Kind crow } '*cannot read a version*')
        Check "a missing zip is refused" (Test-Throws { Get-PackageInfo -Zip (Join-Path $tmp 'nope.zip') -Kind crow } '*not found*')

        Write-Host "sha pinning"
        $payload = [Text.Encoding]::UTF8.GetBytes('the pinned payload')
        $pin = Get-Sha256OfBytes $payload
        $script:fetches = 0
        $good = { param($u, $o) $script:fetches++; [IO.File]::WriteAllBytes($o, [Text.Encoding]::UTF8.GetBytes('the pinned payload')) }
        $evil = { param($u, $o) $script:fetches++; [IO.File]::WriteAllBytes($o, [Text.Encoding]::UTF8.GetBytes('the tampered payload')) }
        $dest = Join-Path $tmp 'vendor-file'
        Check "a missing file is fetched" ((Get-PinnedFile -Url 'u' -Dest $dest -Sha256 $pin -Fetch $good) -eq $true -and $script:fetches -eq 1 -and (Get-Sha256Hex $dest) -eq $pin)
        Check "a file with the right sha is not fetched again" ((Get-PinnedFile -Url 'u' -Dest $dest -Sha256 $pin -Fetch $good) -eq $false -and $script:fetches -eq 1)
        [IO.File]::WriteAllBytes($dest, [Text.Encoding]::UTF8.GetBytes('tampered on disk'))
        Check "Assert-PinnedFile refuses a tampered vendor file" (Test-Throws { Assert-PinnedFile -Path $dest -Sha256 $pin } '*refusing to build*')
        Check "a tampered file is replaced by the pinned bytes" ((Get-PinnedFile -Url 'u' -Dest $dest -Sha256 $pin -Fetch $good) -eq $true -and (Get-Sha256Hex $dest) -eq $pin)
        [IO.File]::WriteAllBytes($dest, [Text.Encoding]::UTF8.GetBytes('tampered on disk'))
        Check "a download that does not match is refused" (Test-Throws { Get-PinnedFile -Url 'u' -Dest $dest -Sha256 $pin -Fetch $evil } '*refusing it*')
        Check "...and nothing tampered is left behind" (-not (Test-Path $dest) -and -not (Test-Path "$dest.part"))
        Check "a missing vendor file is refused at build time" (Test-Throws { Assert-PinnedFile -Path $dest -Sha256 $pin } '*is missing*')

        Write-Host "build flags"
        $f = Get-BuildFlags -Repo 'C:\Users\x y\dev\Crow' -ProfileDir 'C:\Users\x y' -CargoHome 'D:\cargo'
        Check "profile, cargo home, repo, crt-static, in that order" (($f -join '|') -ceq '--remap-path-prefix=C:\Users\x y=~|--remap-path-prefix=D:\cargo=cargo|--remap-path-prefix=C:\Users\x y\dev\Crow=crow|-Ctarget-feature=+crt-static')
        Check "a path with a space stays one flag" ($f.Count -eq 4 -and $f[0] -ceq '--remap-path-prefix=C:\Users\x y=~')
        $g = Get-BuildFlags -Repo 'C:\Users\x\dev\Crow' -ProfileDir 'C:\Users\x' -CargoHome 'C:\Users\x\.cargo'
        Check "a cargo home under the profile needs no flag of its own" ($g.Count -eq 3 -and $g[1] -ceq '--remap-path-prefix=C:\Users\x\dev\Crow=crow')

        Write-Host "exe selftest runner"
        $ok  = Invoke-ExeSelftest -Exe $env:ComSpec -ExeArgs @('/c', 'echo RESULT: OK&exit 0')
        $bad = Invoke-ExeSelftest -Exe $env:ComSpec -ExeArgs @('/c', 'echo broken&exit 3')
        Check "exit 0 and the output are reported" ($ok.Code -eq 0 -and ($ok.Text -join "`n") -like '*RESULT: OK*')
        Check "a nonzero exit is reported" ($bad.Code -eq 3 -and ($bad.Text -join "`n") -like '*broken*')

        Write-Host "privacy gate"
        $prof = $env:USERPROFILE
        if (-not $prof) { throw "USERPROFILE is not set - the gate scans for it" }
        $clean = Join-Path $tmp 'clean.exe'
        [IO.File]::WriteAllBytes($clean, [Text.Encoding]::UTF8.GetBytes('MZ crowsetup panicked at crowsetup\core\src\plan.rs:1 ~\.cargo\registry\src\wry-0.57\lib.rs'))
        Check "an exe that carries only remapped paths passes" (Invoke-ExeGate -Exe $clean -Repo $repo -Quiet)
        $fake = Join-Path $tmp 'fake.exe'
        [IO.File]::WriteAllBytes($fake, [Text.Encoding]::UTF8.GetBytes("MZ panicked at $prof\.cargo\registry\src\wry-0.57\lib.rs:1"))
        Check "an exe that contains the profile path (UTF-8) is refused" (-not (Invoke-ExeGate -Exe $fake -Repo $repo -Quiet))
        $fake16 = Join-Path $tmp 'fake16.exe'
        [IO.File]::WriteAllBytes($fake16, ([byte[]](0x4D, 0x5A)) + [Text.Encoding]::Unicode.GetBytes("$prof\x"))
        Check "an exe that contains the profile path (UTF-16LE) is refused" (-not (Invoke-ExeGate -Exe $fake16 -Repo $repo -Quiet))
        $user = $env:USERNAME
        if ($user -and $user.Length -ge 4) {
            $fakeName = Join-Path $tmp 'fakename.exe'
            [IO.File]::WriteAllBytes($fakeName, [Text.Encoding]::UTF8.GetBytes("MZ built for $user by hand"))
            Check "the bare user name is refused (the allowlist names other files)" (-not (Invoke-ExeGate -Exe $fakeName -Repo $repo -Quiet))
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
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
# The build
# ---------------------------------------------------------------------------

if (-not $CrowZip -or -not $EngineZip) { throw "-CrowZip and -EngineZip are required (or -Selftest)" }
if (-not $env:USERPROFILE) { throw "USERPROFILE is not set - the remap and the privacy gate need it" }
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { throw "cargo not found on PATH" }

$repo      = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path.TrimEnd('\')
$installer = Join-Path $repo 'installer'
$vendor    = Join-Path $installer 'vendor'
$dist      = Join-Path $installer 'dist'
$exeOut    = Join-Path $dist 'CrowSetup.exe'
$pyZip     = Join-Path $vendor $PY_FILE
$getPip    = Join-Path $vendor $GETPIP_FILE
$pkgJson   = Join-Path $vendor 'packages.json'

# nothing from an earlier run may survive a refusal in this one
if (Test-Path -LiteralPath $exeOut) { Remove-Item -LiteralPath $exeOut -Force }
New-Item -ItemType Directory -Force -Path $vendor, $dist | Out-Null

Write-Host "1/6 vendor files"
foreach ($v in @(@($PY_URL, $pyZip, $PY_SHA256), @($GETPIP_URL, $getPip, $GETPIP_SHA256))) {
    $fetched = Get-PinnedFile -Url $v[0] -Dest $v[1] -Sha256 $v[2]
    Write-Host ("  {0}  {1}" -f $(if ($fetched) { 'fetched' } else { 'kept   ' }), (Split-Path -Leaf $v[1]))
}

Write-Host "2/6 packages.json"
$pk = Write-PackagesJson -CrowZip $CrowZip -EngineZip $EngineZip -Version $Version -OutFile $pkgJson
foreach ($k in 'crow', 'engine') {
    Write-Host ("  {0,-6} {1}  {2:N0} bytes  {3}" -f $k, $pk[$k].version, $pk[$k].bytes, $pk[$k].sha256)
}
if ($pk.engine.version -match '-g[0-9a-f]+$' -or $pk.engine.version -match '-dirty$') {
    Write-Host "  NOTE: engine version $($pk.engine.version) is not a release tag; its url answers 404 until that release exists"
}

Write-Host "3/6 cargo build --release -p crowsetup --features bundle"
Assert-PinnedFile -Path $pyZip  -Sha256 $PY_SHA256
Assert-PinnedFile -Path $getPip -Sha256 $GETPIP_SHA256
$flags = Get-BuildFlags -Repo $repo -ProfileDir $env:USERPROFILE.TrimEnd('\') -CargoHome ([string]$env:CARGO_HOME).TrimEnd('\')
$savedFlags = $env:CARGO_ENCODED_RUSTFLAGS
$savedPlain = $env:RUSTFLAGS
# CARGO_ENCODED_RUSTFLAGS (0x1f between flags) keeps a path with a space one argument
$env:CARGO_ENCODED_RUSTFLAGS = $flags -join [char]0x1f
Remove-Item Env:RUSTFLAGS -ErrorAction SilentlyContinue
Push-Location $installer
try {
    & cargo build --release -p crowsetup --features bundle
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed: exit $LASTEXITCODE" }
} finally {
    Pop-Location
    $env:CARGO_ENCODED_RUSTFLAGS = $savedFlags
    if ($null -eq $savedFlags) { Remove-Item Env:CARGO_ENCODED_RUSTFLAGS -ErrorAction SilentlyContinue }
    if ($savedPlain) { $env:RUSTFLAGS = $savedPlain }
}
$built = Join-Path $installer 'target\release\crowsetup.exe'
if (-not (Test-Path -LiteralPath $built)) { throw "cargo reported success but $built does not exist" }

Write-Host "4/6 crowsetup.exe --selftest"
$st = Invoke-ExeSelftest -Exe $built
foreach ($l in ($st.Text | Select-Object -Last 15)) { Write-Host "  $l" }
if ($st.Code -ne 0) { throw "crowsetup.exe --selftest exited $($st.Code)" }

Write-Host "5/6 privacy gate"
if (-not (Invoke-ExeGate -Exe $built -Repo $repo)) { throw "privacy gate refused $built - nothing was copied to $dist" }

Write-Host "6/6 dist"
Copy-Item -LiteralPath $built -Destination $exeOut -Force
$item = Get-Item -LiteralPath $exeOut
Write-Host ("RESULT: {0}  {1:N0} bytes ({2:N1} MiB)  sha256 {3}" -f $exeOut, $item.Length, ($item.Length / 1MB), (Get-Sha256Hex $exeOut))
