<#
.SYNOPSIS
Installs Crow: one command, five steps, no elevation.

.DESCRIPTION
    irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1 | iex

Everything lands under %LOCALAPPDATA%\Crow. Nothing is written to Program Files,
the registry or the PATH by this script.

The model is NOT downloaded here. It is 95.9 GiB, it is not ours to redistribute,
and an installer that spends hours on somebody else's file before the user has
seen anything work is the wrong shape. The last step prints the one command that
fetches it.

Order matters: every check that can reject this machine runs BEFORE the 506 MB
download starts. Finding out afterwards that the card is too small is the most
expensive possible failure.

.PARAMETER Selftest
Run the checks against synthetic inputs, including ones that must fail, and exit.
Downloads nothing.

.PARAMETER NoPause
Do not wait for ENTER at the end. The wait exists so the last screen -- the model
to fetch and the two commands to run -- is still there to read; a script driving
this installation does not want it.

.PARAMETER SourceUrl
Where to take the package from, instead of the GitHub release. Accepts an
http(s) URL or a path to a local .zip.

This exists because an installer whose only source is a release can never be
tried before that release is published -- the first person to run it would be
the first person to test it. With a local path the whole thing runs end to end
against dist\crow-<version>-win-x64.zip, and only the download step is skipped.
A local package is never deleted afterwards; a downloaded one is.
#>
[CmdletBinding()]
param(
    [string] $Version   = "0.1.1",
    [string] $InstallTo = "$env:LOCALAPPDATA\Crow",
    [string] $SourceUrl = "",
    [switch] $Force,
    [switch] $NoPause,
    [switch] $Selftest
)

$ErrorActionPreference = "Stop"
$TOTAL_STEPS = 5

# Where this script lives when it is piped into iex. Printed back at the user in
# the one place where the run needs to be repeated with a switch, because
# `irm <url> | iex` cannot take parameters and telling somebody to "pass -Force"
# without the invocation that accepts it is advice they cannot act on.
$INSTALL_URL = "https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1"

# The target profile, decided on issue #25: 32 GB VRAM at a 200k context window. Below
# 16 GB VRAM the operating point was never measured, so the script refuses rather than
# pretending it knows what happens there.
# $RAM_MIN_GB is a warning threshold, not a measurement. Nothing below the 63.4 GB of the
# machine every figure was taken on has been run. 32 is what the README names as the
# configuration that runs without the host tier, at 1.63x less throughput -- the 16 that
# stood here came from #25's assumption about what a 16 GB-VRAM machine ships with.
$VRAM_SUPPORTED_MB = 16000
$VRAM_TARGET_MB    = 32000
$RAM_MIN_GB        = 32

# The host-RAM expert tier. 32 GiB is the ONLY size measured (2026-08-09, on a 63.4 GB machine:
# 1.40-1.47x throughput over three paired runs), and page-locked memory is taken away from the
# rest of the machine for the life of the process. So the installer offers it at exactly the
# ratio it was measured at and stays silent below that, rather than scaling a number nobody has
# run. A user with 48 GB can pass --moe-stream-l2 themselves; that is their measurement to make.
$L2_GIB            = 32
# 60 and not 64: Windows reports 63.4 GB on the 64 GB machine this was measured on, because
# firmware and hardware reservations come off the top before the OS ever sees the memory. A
# threshold at the nominal size excludes exactly the configuration that produced the figure.
$L2_RAM_REQUIRED_GB = 60
$DISK_INSTALL_GB   = 2      # the package, unpacked
# 85 and not 84: the 0731 UD-IQ2_XXS GGUF is 84.62 GiB across three shards (90,860,736,928 B,
# read from the tensor tables of all three, 2026-08-12). The line this feeds and the download hint
# below it disagreed by a gigabyte on the previous rung until that was fixed; keep them in step.
$DISK_MODEL_GB     = 85     # what the model will need later, reported not enforced

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

$script:StepNo = 0

function Write-Step {
    param([string] $Title)
    $script:StepNo++
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $script:StepNo, $TOTAL_STEPS, $Title) -ForegroundColor Cyan
}

function Write-Item {
    param([string] $Text, [string] $Detail = "", [string] $Status = "")
    $line = "      $Text"
    if ($Detail) { $line += "  $Detail" }
    switch ($Status) {
        "ok"   { Write-Host $line -ForegroundColor Green }
        "warn" { Write-Host $line -ForegroundColor Yellow }
        "fail" { Write-Host $line -ForegroundColor Red }
        default { Write-Host $line -ForegroundColor DarkGray }
    }
}

# Invariant culture on purpose: PowerShell's -f operator formats with the system
# culture, so the same number prints as "482.9 MB" here and "482,9 MB" on a German
# machine. An installer's output should not depend on where it is run, and a test
# asserting one of the two forms would be red on half the world's desktops.
function Format-Num {
    param([double] $Value, [int] $Decimals = 1)
    return [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:N$Decimals}", $Value)
}

function Format-Size {
    param([double] $Bytes)
    $inv = [Globalization.CultureInfo]::InvariantCulture
    if ($Bytes -ge 1GB) { return [string]::Format($inv, "{0:N2} GB", ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return [string]::Format($inv, "{0:N1} MB", ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return [string]::Format($inv, "{0:N0} KB", ($Bytes / 1KB)) }
    return "$Bytes B"
}

# ---------------------------------------------------------------------------
# Preflight -- everything that can say no, before anything is downloaded
# ---------------------------------------------------------------------------

<#
    How many GiB of host-RAM expert tier this machine gets, or 0 for none.

    Deliberately NOT a formula. Half the RAM, or RAM minus a constant, would produce a number for
    every machine and a measurement for none - and this one is page-locked, so a wrong guess does
    not degrade gracefully, it takes memory away from everything else until the process exits.
    Only the ratio that was actually run is offered.
#>
function Get-L2Gib {
    param([double] $RamGb)
    if ($RamGb -ge $L2_RAM_REQUIRED_GB) { return $L2_GIB }
    return 0
}

<#
    Returns a verdict object rather than writing output, so the selftest can feed
    it synthetic values. A check that can only be exercised by owning the hardware
    it checks for is a check nobody ever tests.
#>
function Test-Preflight {
    param(
        [int]    $VramMb,
        [double] $RamGb,
        [double] $FreeDiskGb,
        [bool]   $HasNvidiaSmi,
        [bool]   $Is64Bit,
        [version] $PsVersion
    )

    $problems = @()
    $warnings = @()

    if (-not $Is64Bit)                     { $problems += "64-bit Windows is required" }
    if ($PsVersion.Major -lt 5)            { $problems += "PowerShell 5.1 or newer is required (found $PsVersion)" }
    if (-not $HasNvidiaSmi)                { $problems += "no NVIDIA driver found -- nvidia-smi is not on the PATH. Crow runs its experts on CUDA" }
    elseif ($VramMb -lt $VRAM_SUPPORTED_MB) {
        $problems += ("{0} MB of VRAM, below the {1} MB minimum. Operation below that was never measured and is unsupported" -f $VramMb, $VRAM_SUPPORTED_MB)
    }
    elseif ($VramMb -lt $VRAM_TARGET_MB) {
        $warnings += ("{0} MB of VRAM. The measured target profile is {1} MB; expect fewer cache slots and lower throughput" -f $VramMb, $VRAM_TARGET_MB)
    }

    if ($RamGb -gt 0 -and $RamGb -lt $RAM_MIN_GB) {
        $warnings += ((Format-Num $RamGb 0) + " GB of system RAM, below the $RAM_MIN_GB GB the README names. Every figure was taken on 63.4 GB; below that nothing has been run")
    }
    if ($FreeDiskGb -gt 0 -and $FreeDiskGb -lt $DISK_INSTALL_GB) {
        $problems += ((Format-Num $FreeDiskGb) + " GB free on the install drive, $DISK_INSTALL_GB GB needed for the package")
    }
    elseif ($FreeDiskGb -gt 0 -and $FreeDiskGb -lt ($DISK_INSTALL_GB + $DISK_MODEL_GB)) {
        $warnings += ((Format-Num $FreeDiskGb 0) + " GB free. The package fits, but the model needs about $DISK_MODEL_GB GB more")
    }

    return [pscustomobject]@{
        Ok       = ($problems.Count -eq 0)
        Problems = $problems
        Warnings = $warnings
    }
}

function Get-MachineFacts {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    $vram = 0
    $gpu  = "none"
    if ($smi) {
        # Collect first, take the first line second. `| Select-Object -First 1`
        # directly on a native command ends the pipeline early, PowerShell kills
        # the process, and $LASTEXITCODE lands on -1 -- measured 2026-08-08.
        # The script then exited 255 after a completely successful install,
        # which any caller reads as a failure.
        $lines = @(& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null)
        $q = if ($lines.Count) { $lines[0] } else { $null }
        if ($q) {
            $parts = $q -split ',\s*'
            $gpu   = $parts[0]
            $vram  = [int]$parts[1]
        }
    }
    $ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
    $drive = (Split-Path $InstallTo -Qualifier)
    $free  = (Get-PSDrive ($drive -replace ':','') -ErrorAction SilentlyContinue).Free
    $freeGb = if ($free) { [math]::Round($free / 1GB, 1) } else { 0 }

    return [pscustomobject]@{
        Gpu          = $gpu
        VramMb       = $vram
        RamGb        = $ramGb
        FreeDiskGb   = $freeGb
        HasNvidiaSmi = [bool]$smi
        Is64Bit      = [Environment]::Is64BitOperatingSystem
        PsVersion    = $PSVersionTable.PSVersion
    }
}

# ---------------------------------------------------------------------------
# Leaving
# ---------------------------------------------------------------------------

function Exit-Run {
    <#
    End the run with a status, without taking the user's shell down with it.

    `exit` inside a script FILE leaves the script. Inside a string executed by
    `iex` -- which is the documented way to install this, `irm ... | iex` -- it
    leaves the HOST SHELL. Measured 2026-08-08 on the first public install: the
    window closed the instant the installer finished, before anyone could read
    the server command it had just printed. The install had worked; there was
    simply no way to tell.

    So: exit only when there is a file to exit from. Otherwise set the status
    and let the caller `return`. $PSCommandPath is the file being run and is
    empty for a text run through iex, which is exactly the distinction needed.
    #>
    param([int] $Code = 0)

    if (Test-CanExitProcess $PSCommandPath) { exit $Code }
    $global:LASTEXITCODE = $Code
}

function Compare-Manifest {
    <#
    Hold what was installed against what the package says it should be.

    Takes entries that already carry their computed hash, so the decision is a
    pure function the selftest can drive both ways -- including the case that
    must fail. Reading files is the caller's job.

    Each entry: Path, Expected, Actual. Actual $null means the file is not there.
    #>
    param([object[]] $Entries)

    $missing    = @()
    $mismatched = @()
    foreach ($e in $Entries) {
        if ($null -eq $e.Actual)                          { $missing    += $e.Path; continue }
        if ($e.Actual.ToUpperInvariant() -ne $e.Expected.ToUpperInvariant()) { $mismatched += $e.Path }
    }
    return [pscustomobject]@{
        Ok         = (($missing.Count + $mismatched.Count) -eq 0)
        Missing    = $missing
        Mismatched = $mismatched
        Checked    = $Entries.Count
    }
}

function Find-DroppedFiles {
    <#
    Which files did the PREVIOUS package install that this one no longer ships?

    The update path knew "unchanged", "changed" and "new" and had no case for
    "removed", so a file dropped from a later package stayed on a machine
    forever. The verification above cannot see it: it walks the manifest and asks
    the disk, never the other way round.

    THE DIRECTION OF THIS QUESTION IS THE WHOLE DESIGN, and the first version got
    it backwards. It asked "what is on disk that the new manifest does not name?"
    and protected a hand-written list of exceptions -- session/, MANIFEST.json,
    *.old. Measured 2026-08-10 on a real install: that deleted
    %LOCALAPPDATA%\Crow\session-backup\, three files and 473 MB, made by the user
    minutes earlier on our own advice. It did not match `session/*`, so it was
    treated as rubbish. Every exception list has this failure in it, because it
    has to enumerate what a user might put in a directory, and nobody can.

    Asked in this direction the answer is exact and needs no exceptions: a file
    is removed only if a Crow package put it there and the new one does not. User
    data was never in a manifest, so it can never be selected. MANIFEST.json is
    not listed in itself, and *.old files are created by the rename step -- both
    fall out for free rather than by being remembered.

    Pure, like Compare-Manifest: two lists in, a verdict out. Whether the file is
    still on disk is the caller's question.

    Comparison is case-insensitive and separator-agnostic: NTFS does not care
    about case, and the manifest is written by one tool and read back by another.
    #>
    param(
        [string[]] $PreviousPaths,
        [string[]] $CurrentPaths
    )

    $current = @{}
    foreach ($p in $CurrentPaths) {
        if ($null -ne $p) { $current[($p -replace '\\', '/').ToLowerInvariant()] = $true }
    }

    $dropped = @()
    $seen    = @{}
    foreach ($raw in $PreviousPaths) {
        if ($null -eq $raw) { continue }
        $rel = $raw -replace '\\', '/'
        $key = $rel.ToLowerInvariant()
        if ($current.ContainsKey($key) -or $seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $dropped += $rel
    }

    return [pscustomobject]@{
        # @() on purpose: PowerShell hands back a bare String when a list holds
        # exactly one item, and one dropped file is the ordinary case. A caller
        # indexing [0] would then get a single character -- measured the same day
        # in pack-release.ps1, where the comma operator fixes the same thing.
        Dropped = @($dropped)
        # The denominator. "0 removed" out of nothing examined reads exactly like
        # "0 removed" out of a clean update, and one of those is a broken run.
        Checked = @($PreviousPaths).Count
    }
}

function Test-CanExitProcess {
    <#
    The decision Exit-Run rests on, split out so the selftest can drive both
    answers. Testing it through Exit-Run is impossible by construction: the
    branch under test ends the process.
    #>
    param([string] $CommandPath)
    return [bool]$CommandPath
}

# ---------------------------------------------------------------------------
# Where the package comes from
# ---------------------------------------------------------------------------

function Resolve-PackageSource {
    <#
    Returns @{ Uri = <url or full path>; IsLocal = <bool> }.

    Kept apart from the download so it can be exercised by the selftest without
    a network: everything that decides WHERE the package comes from is here, and
    everything that moves bytes is in Get-FileWithProgress.
    #>
    param([string] $SourceUrl, [string] $Asset, [string] $Version)

    if (-not $SourceUrl) {
        return @{ Uri = "https://github.com/nibor1896/Crow/releases/download/v$Version/$Asset"; IsLocal = $false }
    }
    if ($SourceUrl -match '^https?://') {
        return @{ Uri = $SourceUrl; IsLocal = $false }
    }

    $path = $SourceUrl
    if ($path -match '^file:///') { $path = ([Uri] $SourceUrl).LocalPath }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        # Named, not silently downloaded from the release instead: a typo in a
        # local path that quietly falls back would install a different package
        # than the one being tested.
        throw "no package at $path -- -SourceUrl wants an existing .zip or an http(s) URL"
    }
    return @{ Uri = (Resolve-Path -LiteralPath $path).Path; IsLocal = $true }
}

# ---------------------------------------------------------------------------
# Updating an installation that is already there
# ---------------------------------------------------------------------------

function Get-InstalledVersion {
    <#
    The version of the installation sitting in $Dir, or $null.

    Read out of the shipped cli\crow.py with the same pattern pack-release.ps1
    uses to stamp the package, so the two can never disagree about where the
    number lives. MANIFEST.json carries hashes, not a version.

    $null means "there is something here but it does not identify itself" --
    which is NOT the same as "nothing is installed", and the caller has to keep
    those apart before it overwrites a stranger's directory.
    #>
    param([string] $Dir)

    $cli = Join-Path $Dir "cli\crow.py"
    if (-not (Test-Path -LiteralPath $cli)) { return $null }
    $m = Select-String -LiteralPath $cli -Pattern '^VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $m) { return $null }
    return $m.Matches[0].Groups[1].Value
}

function Resolve-InstallAction {
    <#
    Returns @{ Action = 'install'|'update'|'uptodate'|'downgrade'|'unknown'; Message = <string> }.

    A pure function on purpose, like Resolve-PackageSource: the selftest drives
    every branch, including the ones that must refuse, without a filesystem and
    without a network.

    WHY THIS EXISTS. Until 2026-08-08 the installer refused any non-empty target
    with "pass -Force to overwrite" and exit 1. The documented one-liner is
    `irm ... | iex`, and a piped script cannot be given parameters at all -- so
    the advice it printed could not be followed by the person reading it. There
    was no way to move from one version to the next short of deleting the
    directory by hand. An installed base that cannot be updated is worse than
    one that was never installed.

    'unknown' is deliberately NOT treated as "install anyway": a directory whose
    contents we cannot identify may be somebody's own files, and overwriting it
    on a guess is not ours to do.
    #>
    param([string] $Installed, [string] $Target, [bool] $Force)

    if (-not $Installed) {
        if ($Force) { return @{ Action = 'install';  Message = "overwriting an unidentified install because -Force was passed" } }
        return @{ Action = 'unknown'; Message = "something is already in the target and does not identify itself -- pass -Force to overwrite it" }
    }

    $a = $null; $b = $null
    if (-not ([version]::TryParse($Installed, [ref] $a)) -or -not ([version]::TryParse($Target, [ref] $b))) {
        if ($Force) { return @{ Action = 'install'; Message = "version strings not comparable ($Installed -> $Target), proceeding because -Force was passed" } }
        return @{ Action = 'unknown'; Message = "cannot compare versions ($Installed -> $Target) -- pass -Force to install anyway" }
    }

    if ($b -gt $a) { return @{ Action = 'update';    Message = "$Installed -> $Target" } }
    if ($b -eq $a) {
        if ($Force) { return @{ Action = 'install';  Message = "reinstalling $Target because -Force was passed" } }
        return @{ Action = 'uptodate'; Message = "$Target is already installed -- nothing to do" }
    }
    if ($Force) { return @{ Action = 'install';      Message = "downgrading $Installed -> $Target because -Force was passed" } }
    return @{ Action = 'downgrade'; Message = "a newer version is installed ($Installed, this installs $Target) -- pass -Force to go back" }
}

function Resolve-LatestVersion {
    <#
    The newest published release tag, or $Fallback when the API cannot be reached.

    The one-liner fetches install.ps1 from main, so whatever number is hard-coded
    here is only as fresh as the last push to that file. Asking the release API
    makes the SAME one-liner install the newest version without anyone editing a
    default -- and the hard-coded value stays as the offline answer rather than
    the primary one.
    #>
    param([string] $Fallback)

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/nibor1896/Crow/releases/latest" `
                                 -Headers @{ "User-Agent" = "crow-installer" } -TimeoutSec 10
        $tag = "$($rel.tag_name)".TrimStart("v")
        if ($tag) { return $tag }
    } catch { }
    return $Fallback
}

# ---------------------------------------------------------------------------
# Download with a progress line that says what is happening
# ---------------------------------------------------------------------------

function Get-FileWithProgress {
    param([string] $Uri, [string] $OutFile, [string] $Label)

    # Windows PowerShell 5.1 does not load System.Net.Http by default, and the
    # failure is a TypeNotFound at the moment the download starts -- i.e. after
    # the preflight has already told the user everything is fine.
    Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $client = [System.Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromHours(2)
    try {
        $resp = $client.GetAsync($Uri, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (-not $resp.IsSuccessStatusCode) {
            throw "HTTP $([int]$resp.StatusCode) $($resp.ReasonPhrase) for $Uri"
        }
        $total = $resp.Content.Headers.ContentLength
        $src   = $resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $dst   = [System.IO.File]::Create($OutFile)
        try {
            $buf  = New-Object byte[] 1048576
            $done = 0L
            $sw   = [Diagnostics.Stopwatch]::StartNew()
            $lastPaint = 0
            while (($n = $src.Read($buf, 0, $buf.Length)) -gt 0) {
                $dst.Write($buf, 0, $n)
                $done += $n
                # Repaint at most ten times a second: a progress line redrawn per
                # 1 MB block costs more wall clock than the copy on a fast link.
                if ($sw.ElapsedMilliseconds - $lastPaint -ge 100) {
                    $lastPaint = $sw.ElapsedMilliseconds
                    $rate = $done / [math]::Max($sw.Elapsed.TotalSeconds, 0.001)
                    if ($total) {
                        $pct = [int](100 * $done / $total)
                        $bar = ("#" * [int]($pct / 4)).PadRight(25, '.')
                        Write-Host ("`r      {0}  [{1}] {2,3}%  {3} / {4}  {5}/s   " -f `
                            $Label, $bar, $pct, (Format-Size $done), (Format-Size $total), (Format-Size $rate)) -NoNewline
                    } else {
                        Write-Host ("`r      {0}  {1}  {2}/s   " -f $Label, (Format-Size $done), (Format-Size $rate)) -NoNewline
                    }
                }
            }
            $sw.Stop()
            Write-Host ("`r      " + $Label + "  " + (Format-Size $done) + " in " + (Format-Num $sw.Elapsed.TotalSeconds 0) + "s" + (" " * 40))
            return $done
        } finally { $dst.Dispose(); $src.Dispose() }
    } finally { $client.Dispose() }
}

# ---------------------------------------------------------------------------
# Unpack, naming each file as it lands
# ---------------------------------------------------------------------------

function Expand-WithProgress {
    param([string] $ZipPath, [string] $Destination)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entries = @($zip.Entries | Where-Object { $_.Name })   # skip directory entries
        $i = 0
        foreach ($e in $entries) {
            $i++
            $target = Join-Path $Destination $e.FullName
            $dir    = Split-Path $target -Parent
            if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
            Write-Host ("`r      {0,4}/{1}  {2,-42} {3}   " -f $i, $entries.Count, `
                        $e.Name.Substring(0, [Math]::Min(42, $e.Name.Length)), (Format-Size $e.Length)) -NoNewline
        }
        Write-Host ("`r      {0} files extracted{1}" -f $entries.Count, (" " * 60))
        return $entries.Count
    } finally { $zip.Dispose() }
}

# ---------------------------------------------------------------------------
# A running server holds its own files
# ---------------------------------------------------------------------------
#
# These are functions rather than inline script for one reason: everything below
# the selftest's exit is unreachable to it. The first version of this fix sat at
# line 744, the selftest returns at 631, and it reported 42 of 42 green without
# executing a single line of it.

function Test-RunsFromDir {
    <#
    .SYNOPSIS
        Is this executable inside that directory?
    .DESCRIPTION
        The process NAME is not enough, and the difference is not academic: a
        development build of llama-server.exe running from somewhere else has the
        same name and holds nothing here. Matching on the name alone renames a
        bin\ directory that nothing is locking.
    #>
    param([string] $ExePath, [string] $Dir)
    if (-not $ExePath -or -not $Dir) { return $false }
    $prefix = $Dir.TrimEnd('\', '/') + '\'
    return $ExePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-LockingServer {
    <# The llama-server running out of $BinDir, or $null when none is. #>
    param([string] $BinDir)
    foreach ($p in @(Get-Process -Name "llama-server" -ErrorAction SilentlyContinue)) {
        # .Path throws for a process this user cannot open. Not being able to
        # look is not the same as nothing being there, but it is all we have.
        $exe = $null
        try { $exe = $p.Path } catch { $exe = $null }
        if (Test-RunsFromDir -ExePath $exe -Dir $BinDir) { return $p }
    }
    return $null
}

function Move-LockedAside {
    <#
    .SYNOPSIS
        Rename every file in a directory to *.old so an extraction can write over it.
    .DESCRIPTION
        Windows locks a running binary against WRITING, not against renaming: the
        process keeps its handle to the file it opened, and the path is freed for
        the new one. Measured 2026-08-10 against a running executable -- the
        rename succeeded.

        Moved is counted by asking the filesystem afterwards, and Failed names
        what is still sitting there. A rename that quietly fails would hand the
        original IOException to the extraction with a layer of misdirection in
        front of it, which is worse than the bug it was meant to fix.
    #>
    param([string] $BinDir)
    $moved  = 0
    $failed = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $BinDir -File -ErrorAction SilentlyContinue)) {
        if ($f.Name -like "*.old") { continue }
        $oldPath = $f.FullName + ".old"
        # An interrupted update may have left one behind; it is in the way now.
        if (Test-Path -LiteralPath $oldPath) {
            Remove-Item -LiteralPath $oldPath -Force -ErrorAction SilentlyContinue
        }
        Rename-Item -LiteralPath $f.FullName -NewName ($f.Name + ".old") -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $f.FullName) { $failed += $f.Name } else { $moved++ }
    }
    return [pscustomobject]@{ Moved = $moved; Failed = $failed }
}

function Remove-StaleOld {
    <#
    .SYNOPSIS
        Delete the *.old files left behind, and report what actually went.
    .DESCRIPTION
        A file the running server still has mapped CANNOT be deleted. Measured
        2026-08-10: the rename succeeds, the delete is denied, the file stays.
        And that is the NORMAL case here, because the reason it was renamed is
        that the server is still running.

        So Removed is the difference between before and after, taken by looking.
        Counting what was found and calling it "removed" prints a green line in
        exactly the situation where nothing was removed.
    #>
    param([string] $BinDir)
    $before = @(Get-ChildItem -LiteralPath $BinDir -Filter "*.old" -File -ErrorAction SilentlyContinue)
    foreach ($f in $before) {
        Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
    }
    $kept = @(Get-ChildItem -LiteralPath $BinDir -Filter "*.old" -File -ErrorAction SilentlyContinue |
              ForEach-Object { $_.Name })
    return [pscustomobject]@{ Removed = ($before.Count - $kept.Count); Kept = $kept }
}


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

function Invoke-Selftest {
    $ok = 0; $red = 0
    function C { param([string]$N, [bool]$P)
        if ($P) { Write-Host "  ok   $N"; $script:sOk++ } else { Write-Host "  FAIL $N" -ForegroundColor Red; $script:sRed++ } }
    $script:sOk = 0; $script:sRed = 0

    Write-Host "install.ps1 selftest"

    $good = @{ VramMb=32607; RamGb=63.4; FreeDiskGb=450; HasNvidiaSmi=$true; Is64Bit=$true; PsVersion=[version]"5.1" }

    C "the development machine passes"            ((Test-Preflight @good).Ok)
    C "and passes without warnings"               ((Test-Preflight @good).Warnings.Count -eq 0)

    # Each rejection, one value moved from the passing case. A preflight that
    # accepts everything would pass the first two checks and protect nobody.
    $noGpu = $good.Clone(); $noGpu.HasNvidiaSmi = $false; $noGpu.VramMb = 0
    C "no NVIDIA driver is rejected"              (-not (Test-Preflight @noGpu).Ok)

    $small = $good.Clone(); $small.VramMb = 12000
    C "12 GB VRAM is rejected as unmeasured"      (-not (Test-Preflight @small).Ok)

    $mid = $good.Clone(); $mid.VramMb = 24000
    C "24 GB VRAM passes with a warning"          ((Test-Preflight @mid).Ok -and (Test-Preflight @mid).Warnings.Count -gt 0)

    $x86 = $good.Clone(); $x86.Is64Bit = $false
    C "32-bit Windows is rejected"                (-not (Test-Preflight @x86).Ok)

    $oldPs = $good.Clone(); $oldPs.PsVersion = [version]"4.0"
    C "PowerShell 4 is rejected"                  (-not (Test-Preflight @oldPs).Ok)

    $noDisk = $good.Clone(); $noDisk.FreeDiskGb = 1
    C "1 GB free disk is rejected"                (-not (Test-Preflight @noDisk).Ok)

    $tight = $good.Clone(); $tight.FreeDiskGb = 5
    C "5 GB free passes, warns about the model"   ((Test-Preflight @tight).Ok -and (Test-Preflight @tight).Warnings.Count -gt 0)

    $lowRam = $good.Clone(); $lowRam.RamGb = 8
    C "8 GB RAM warns but does not block"         ((Test-Preflight @lowRam).Ok -and (Test-Preflight @lowRam).Warnings.Count -gt 0)

    # The host tier is offered at exactly one ratio. Both directions are checked, because a rule
    # that only ever says yes is not a rule - and the 63.4 GB case is the development machine,
    # which must NOT get it: 63.4 is below 64, and rounding it up would hand out page-locked
    # memory on a machine the measurement did not cover.
    C "63.4 GB - the measured machine - gets 32"  ((Get-L2Gib -RamGb 63.4) -eq 32)
    C "128 GB RAM gets the same 32, not more"     ((Get-L2Gib -RamGb 128) -eq 32)
    C "48 GB gets no tier - never measured"       ((Get-L2Gib -RamGb 48) -eq 0)
    C "32 GB gets no tier"                        ((Get-L2Gib -RamGb 32) -eq 0)
    C "16 GB gets no tier"                        ((Get-L2Gib -RamGb 16) -eq 0)

    # Where the package comes from. No network: the decision is a pure function.
    $noSrc = Resolve-PackageSource -SourceUrl "" -Asset "crow-9.9.9-win-x64.zip" -Version "9.9.9"
    C "no -SourceUrl still points at the release" `
      ((-not $noSrc.IsLocal) -and $noSrc.Uri -eq "https://github.com/nibor1896/Crow/releases/download/v9.9.9/crow-9.9.9-win-x64.zip")

    $httpSrc = Resolve-PackageSource -SourceUrl "https://example.invalid/x.zip" -Asset "a.zip" -Version "9.9.9"
    C "an http source overrides the release"     ((-not $httpSrc.IsLocal) -and $httpSrc.Uri -eq "https://example.invalid/x.zip")

    $probe = Join-Path $env:TEMP ("crow-selftest-" + [guid]::NewGuid().ToString("N") + ".zip")
    Set-Content -LiteralPath $probe -Value "not really a zip" -Encoding ascii
    try {
        $localSrc = Resolve-PackageSource -SourceUrl $probe -Asset "a.zip" -Version "9.9.9"
        C "a local package is accepted"          ($localSrc.IsLocal -and $localSrc.Uri -eq $probe)
    } finally {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }

    # The negative half: a path that is not there must stop the run rather than
    # fall back to the release, which would install something else than asked for.
    $missed = $false
    try   { Resolve-PackageSource -SourceUrl (Join-Path $env:TEMP "crow-does-not-exist.zip") -Asset "a.zip" -Version "9.9.9" | Out-Null }
    catch { $missed = $true }
    C "a missing local package is rejected"      $missed

    # Updating. Every branch, because the defect this replaced was a single
    # refusal that treated "an older Crow is here" the same as "a stranger's
    # files are here" -- and gave both the same unusable advice.
    C "an older install updates"                 ((Resolve-InstallAction "0.0.4" "0.0.5" $false).Action -eq 'update')
    C "a two-step gap updates"                   ((Resolve-InstallAction "0.0.1" "0.0.5" $false).Action -eq 'update')
    C "the same version does nothing"            ((Resolve-InstallAction "0.0.5" "0.0.5" $false).Action -eq 'uptodate')
    C "the same version reinstalls with -Force"  ((Resolve-InstallAction "0.0.5" "0.0.5" $true).Action  -eq 'install')
    C "a newer install is NOT overwritten"       ((Resolve-InstallAction "0.0.6" "0.0.5" $false).Action -eq 'downgrade')
    C "and goes back only with -Force"           ((Resolve-InstallAction "0.0.6" "0.0.5" $true).Action  -eq 'install')

    # The cases that must refuse. An unidentified directory is somebody's data
    # until proven otherwise; a version string that will not parse must not be
    # silently read as 0.0.0, which would make every unknown look like "older".
    C "an unidentified target refuses"           ((Resolve-InstallAction $null "0.0.5" $false).Action -eq 'unknown')
    C "and is overwritten only with -Force"      ((Resolve-InstallAction $null "0.0.5" $true).Action  -eq 'install')
    C "an unparseable version refuses"           ((Resolve-InstallAction "not-a-version" "0.0.5" $false).Action -eq 'unknown')
    C "an unparseable TARGET refuses too"        ((Resolve-InstallAction "0.0.3" "garbage" $false).Action -eq 'unknown')

    # Reading the version back out of a shipped tree, and the two ways that fails.
    $vdir = Join-Path $env:TEMP ("crow-selftest-v-" + [guid]::NewGuid().ToString("N"))
    try {
        New-Item -ItemType Directory -Force -Path (Join-Path $vdir "cli") | Out-Null
        Set-Content -LiteralPath (Join-Path $vdir "cli\crow.py") -Encoding utf8 `
                    -Value @('#!/usr/bin/env python', 'VERSION = "1.2.3"', 'DEFAULT_MODEL = "crow"')
        C "the installed version is read"        ((Get-InstalledVersion $vdir) -eq "1.2.3")

        Set-Content -LiteralPath (Join-Path $vdir "cli\crow.py") -Encoding utf8 -Value @('no version line here')
        C "a file without VERSION reads as null" ($null -eq (Get-InstalledVersion $vdir))
    } finally {
        Remove-Item -LiteralPath $vdir -Recurse -Force -ErrorAction SilentlyContinue
    }
    C "an empty directory reads as null"         ($null -eq (Get-InstalledVersion (Join-Path $env:TEMP "crow-selftest-absent")))

    # The regression behind the 255: reading the machine must not leave a native
    # command's broken exit code behind, because the script's own exit code is
    # whatever the last native call left there.
    $global:LASTEXITCODE = 0
    $null = Get-MachineFacts
    C "reading the machine leaves a clean exit code" ($LASTEXITCODE -eq 0)

    # The regression that closed the first public install's window: `exit` in a
    # text run through iex takes the host shell with it.
    C "a run from a file may exit the process"   (Test-CanExitProcess "C:\x\install.ps1")
    C "a run through iex must not"               (-not (Test-CanExitProcess ""))

    # The check that did not exist until a flipped byte proved Expand-Archive
    # will not catch one. Both answers driven, because a verifier that cannot
    # report damage is a decoration.
    $good = @(
        [pscustomobject]@{ Path = "a.dll"; Expected = "AABB"; Actual = "aabb" }
        [pscustomobject]@{ Path = "b.exe"; Expected = "CCDD"; Actual = "CCDD" }
    )
    $v = Compare-Manifest $good
    C "a matching install passes"                 ($v.Ok -and $v.Checked -eq 2)
    C "and the hash compare ignores case"         ($v.Mismatched.Count -eq 0)

    $corrupt = @([pscustomobject]@{ Path = "a.dll"; Expected = "AABB"; Actual = "BEEF" })
    $v = Compare-Manifest $corrupt
    C "one wrong hash fails the install"          ((-not $v.Ok) -and $v.Mismatched -contains "a.dll")

    $gone = @([pscustomobject]@{ Path = "a.dll"; Expected = "AABB"; Actual = $null })
    $v = Compare-Manifest $gone
    C "a file that did not land fails too"        ((-not $v.Ok) -and $v.Missing -contains "a.dll")

    # The case the update path did not have until now: a file the package USED to
    # ship. Compare-Manifest above cannot see it -- it walks the current manifest,
    # and a dropped file is not in it by definition.
    $was = @("bin/llama.dll", "cli/crow.py", "cli/test_crow.py")
    $now = @("bin/llama.dll", "cli/crow.py")
    $d = Find-DroppedFiles -PreviousPaths $was -CurrentPaths $now
    C "a file the package dropped is found"       ($d.Dropped -contains "cli/test_crow.py" -and $d.Dropped.Count -eq 1)
    C "and the count carries its denominator"     ($d.Checked -eq 3)

    # THE REGRESSION, and it is not hypothetical. The first version of this asked
    # "what is on disk that the manifest does not name?" and kept an exception
    # list. On 2026-08-10 it deleted %LOCALAPPDATA%\Crow\session-backup\ -- three
    # files, 473 MB, created by the user minutes earlier on our own instructions.
    # It was not session/, so it was not on the list, so it was rubbish.
    # Asked in this direction the folder cannot be selected: no Crow package ever
    # installed it, so it is in neither manifest.
    $d = Find-DroppedFiles -PreviousPaths $was -CurrentPaths $now
    C "a folder no package installed is untouchable" (
        ($d.Dropped -notcontains "session-backup/session.json") -and
        ($d.Dropped -notcontains "session/crow-session.bin") -and
        ($d.Dropped -notcontains "notes.txt"))

    # Nothing dropped is a real answer and must not be confused with "nothing
    # examined" -- hence the denominator on the quiet path too.
    $d = Find-DroppedFiles -PreviousPaths $was -CurrentPaths $was
    C "an unchanged package drops nothing"        ($d.Dropped.Count -eq 0 -and $d.Checked -eq 3)

    # First install: no previous manifest exists, so there is nothing to remove.
    # The dangerous reading of an empty list is "everything is dropped".
    $d = Find-DroppedFiles -PreviousPaths @() -CurrentPaths $now
    C "a first install drops nothing"             ($d.Dropped.Count -eq 0 -and $d.Checked -eq 0)

    # The manifest is written by one tool and read back by another, and the two
    # do not agree on separators; NTFS does not agree on case with anyone. A
    # mismatch here would delete a file that is still shipped.
    $d = Find-DroppedFiles -PreviousPaths @("bin\llama.dll") -CurrentPaths @("bin/LLAMA.dll")
    C "separator and case do not drop a kept file" ($d.Dropped.Count -eq 0)

    # A path listed twice must be removed once, or the count reports work twice.
    $d = Find-DroppedFiles -PreviousPaths @("a.dll", "a.dll") -CurrentPaths @()
    C "a duplicate entry is dropped once"         ($d.Dropped.Count -eq 1)

    # Whose server is it. Both directions, because a rule that only ever says
    # yes renames a directory nothing is holding.
    C "an exe inside the install dir is ours"     (Test-RunsFromDir -ExePath "C:\Crow\bin\llama-server.exe" -Dir "C:\Crow\bin")
    C "case does not decide it"                   (Test-RunsFromDir -ExePath "c:\crow\BIN\llama-server.exe" -Dir "C:\Crow\bin")
    C "a dev build elsewhere is NOT ours"         (-not (Test-RunsFromDir -ExePath "C:\dev\wt-25\bin\llama-server.exe" -Dir "C:\Crow\bin"))
    C "a sibling directory is not inside it"      (-not (Test-RunsFromDir -ExePath "C:\Crow\bin-old\llama-server.exe" -Dir "C:\Crow\bin"))
    C "a process we cannot read is not a match"   (-not (Test-RunsFromDir -ExePath $null -Dir "C:\Crow\bin"))

    # Against real files with a real lock, because the entire question is what
    # Windows does with one. FileShare::None is the strict case: it cannot even
    # be renamed, which is the failure path that must not stay silent.
    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("crow-selftest-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    $held = $null
    try {
        Set-Content -LiteralPath (Join-Path $tmpDir "free.dll") -Value "x" -Encoding Ascii
        Set-Content -LiteralPath (Join-Path $tmpDir "held.dll") -Value "x" -Encoding Ascii
        $held = [System.IO.File]::Open((Join-Path $tmpDir "held.dll"), 'Open', 'Read', 'None')

        $aside = Move-LockedAside -BinDir $tmpDir
        C "an unheld file is moved aside"          ($aside.Moved -eq 1)
        C "a held file is REPORTED, not swallowed" ($aside.Failed -contains "held.dll")

        $held.Close(); $held = $null
        Set-Content -LiteralPath (Join-Path $tmpDir "stuck.dll.old") -Value "x" -Encoding Ascii
        $held = [System.IO.File]::Open((Join-Path $tmpDir "stuck.dll.old"), 'Open', 'Read', 'None')

        $swept = Remove-StaleOld -BinDir $tmpDir
        C "a free .old file is really removed"     ($swept.Removed -eq 1)
        C "and one still held is NOT called gone"  ($swept.Kept -contains "stuck.dll.old")
    } finally {
        if ($held) { $held.Close() }
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    C "Format-Size: bytes"     ((Format-Size 512) -eq "512 B")
    C "Format-Size: megabytes" ((Format-Size 506400000) -eq "482.9 MB")
    C "Format-Size: gigabytes" ((Format-Size 103000000000) -eq "95.93 GB")

    Write-Host ""
    $total = $script:sOk + $script:sRed
    if ($script:sRed -gt 0) { Write-Host "RESULT: $($script:sRed) of $total FAILED" -ForegroundColor Red; return 1 }
    Write-Host "RESULT: SELFTEST OK - $total checks" -ForegroundColor Green
    return 0
}

if ($Selftest) { Exit-Run (Invoke-Selftest); return }

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "  Crow $Version" -ForegroundColor Cyan
Write-Host "  A frontier coding LLM, made runnable by streaming its experts off the SSD." -ForegroundColor DarkGray

Write-Step "Checking this machine"

$facts = Get-MachineFacts
Write-Item "GPU"      $(if ($facts.HasNvidiaSmi) { "$($facts.Gpu), $($facts.VramMb) MB" } else { "none detected" }) $(if ($facts.HasNvidiaSmi) { "ok" } else { "fail" })
Write-Item "RAM"      ((Format-Num $facts.RamGb) + " GB")
Write-Item "Disk"     ((Format-Num $facts.FreeDiskGb) + " GB free on " + (Split-Path $InstallTo -Qualifier))
Write-Item "Windows"  $(if ($facts.Is64Bit) { "64-bit, PowerShell $($facts.PsVersion)" } else { "32-bit" })

$pf = Test-Preflight -VramMb $facts.VramMb -RamGb $facts.RamGb -FreeDiskGb $facts.FreeDiskGb `
                     -HasNvidiaSmi $facts.HasNvidiaSmi -Is64Bit $facts.Is64Bit -PsVersion $facts.PsVersion

foreach ($w in $pf.Warnings) { Write-Item "warning:" $w "warn" }
if (-not $pf.Ok) {
    Write-Host ""
    foreach ($p in $pf.Problems) { Write-Item "cannot install:" $p "fail" }
    Write-Host ""
    Write-Host "  Nothing was downloaded." -ForegroundColor DarkGray
    Exit-Run 1
    return
}
Write-Item "preflight" "passed" "ok"

# Asked AFTER the preflight and only when it can matter: a machine that is about
# to be refused should not wait on a network call first, and a run pointed at a
# local package by -SourceUrl has already been told which bytes to install.
# An explicit -Version wins over the release -- somebody naming a version means it.
if (-not $PSBoundParameters.ContainsKey('Version') -and -not $SourceUrl) {
    $latest = Resolve-LatestVersion -Fallback $Version
    if ($latest -ne $Version) {
        Write-Item "release" "$latest is the newest published (this file defaults to $Version)" "ok"
        $Version = $latest
    }
}

Write-Step "Downloading the package"

$asset = "crow-$Version-win-x64.zip"
# Caught rather than thrown on: every other refusal in this script prints one
# line and exits 1, and a raw PowerShell error record here would be the only
# place a user meets a stack trace.
try {
    $source = Resolve-PackageSource -SourceUrl $SourceUrl -Asset $asset -Version $Version
} catch {
    Write-Item "cannot install:" $_.Exception.Message "fail"
    Write-Host ""
    Write-Host "  Nothing was downloaded." -ForegroundColor DarkGray
    Exit-Run 1
    return
}
Write-Item "from" $source.Uri
if ($source.IsLocal) {
    $tmp   = $source.Uri
    $bytes = (Get-Item -LiteralPath $tmp).Length
    Write-Item "local package" "nothing downloaded" "ok"
} else {
    $tmp   = Join-Path $env:TEMP $asset
    $bytes = Get-FileWithProgress -Uri $source.Uri -OutFile $tmp -Label $asset
}

$sha = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash
Write-Item "size"   (Format-Size $bytes) "ok"
Write-Item "sha256" $sha

Write-Step "Installing to $InstallTo"

if (Test-Path $InstallTo) {
    $existing = @(Get-ChildItem $InstallTo -ErrorAction SilentlyContinue).Count
    if ($existing -gt 0) {
        $decision = Resolve-InstallAction -Installed (Get-InstalledVersion $InstallTo) `
                                          -Target $Version -Force ([bool] $Force)
        switch ($decision.Action) {
            'uptodate' {
                Write-Item "already current" $decision.Message "ok"
                Write-Host ""
                Write-Host "  Nothing was changed. Pass -Force to reinstall the same version." -ForegroundColor DarkGray
                Exit-Run 0
                return
            }
            'update'   { Write-Item "updating" $decision.Message "ok" }
            'install'  { Write-Item "overwriting" $decision.Message "ok" }
            default    {
                # 'unknown' and 'downgrade': both refuse, and both print a route
                # that a piped one-liner can actually take. `irm | iex` cannot be
                # given parameters, so "pass -Force" alone is advice nobody
                # reading it is able to follow.
                Write-Item "not installing" $decision.Message "fail"
                Write-Host ""
                Write-Host "  The one-liner cannot pass -Force. To force it:" -ForegroundColor DarkGray
                Write-Host "    &([scriptblock]::Create((irm $INSTALL_URL))) -Force" -ForegroundColor White
                Exit-Run 1
                return
            }
        }
    }
}
# NOTHING IS DELETED HERE, and that is not laziness. The last step of this
# script tells the user to fetch the model into $InstallTo\models -- 95.9 GiB
# that is not part of any package. An update that "cleaned" the target first
# would throw that away and re-download it over the user's connection. Files
# that a newer package no longer ships are therefore left behind; the manifest
# check below verifies what SHOULD be there, and says nothing about extras.
New-Item -ItemType Directory -Force -Path $InstallTo | Out-Null

# Windows locks a running executable and its loaded DLLs. An update started
# while the server is up -- which is exactly the state the user is in when the
# client prints the one-liner -- hits a locked file and fails with an
# IOException that nothing catches. The fix: rename locked files before
# extraction. Windows allows renaming a running binary; the process keeps its
# handle to the old name and the path is freed for the new file.
$binDir = Join-Path $InstallTo "bin"
$holder = if (Test-Path -LiteralPath $binDir) { Get-LockingServer -BinDir $binDir } else { $null }
if ($holder) {
    Write-Item "server running" "llama-server.exe (pid $($holder.Id)) is holding $binDir" "warn"
    $aside = Move-LockedAside -BinDir $binDir
    if ($aside.Moved -gt 0) {
        Write-Item "moved aside" "$($aside.Moved) files renamed -- the running server keeps the old ones" "ok"
    }
    # Stopping here rather than letting the extraction discover it: below, the
    # same condition arrives as an IOException with a filename in it and no idea
    # why, and the user has already waited for a download by then.
    if ($aside.Failed.Count -gt 0) {
        Write-Item "could not move" "$($aside.Failed -join ', ')" "fail"
        Write-Item "stop llama-server and run this again" "those files cannot be overwritten while they are held" "fail"
        Exit-Run 1
        return
    }
}

# Read BEFORE extraction: the archive overwrites MANIFEST.json, and after that
# there is no record left of what the previous package put here. Without this the
# removal step below has to guess from the directory contents, which is how it
# deleted a user's own backup folder on 2026-08-10.
$previousManifest = @()
$prevPath = Join-Path $InstallTo "MANIFEST.json"
if (Test-Path -LiteralPath $prevPath) {
    try {
        $previousManifest = @((Get-Content -LiteralPath $prevPath -Raw | ConvertFrom-Json) |
                              ForEach-Object { $_.path })
    } catch {
        # A manifest we cannot read means we do not know what the last package
        # left. Saying so and removing nothing is the only safe answer.
        Write-Item "previous MANIFEST.json unreadable" "nothing will be removed this run" "warn"
        $previousManifest = @()
    }
}

try {
    $n = Expand-WithProgress -ZipPath $tmp -Destination $InstallTo
} catch [System.IO.IOException] {
    $lockedFile = $_.Exception.Message
    Write-Item "extraction failed" "a file is still locked: $lockedFile" "fail"
    Write-Host ""
    Write-Item "try again after stopping llama-server" "or restart your machine to release all file handles" "fail"
    Exit-Run 1
    return
} catch {
    Write-Item "extraction failed" "$($_.Exception.Message)" "fail"
    Exit-Run 1
    return
}
# Only what this script downloaded. A package handed in with -SourceUrl belongs
# to the caller, and deleting it would eat the artefact being tested.
if (-not $source.IsLocal) { Remove-Item $tmp -Force }
Write-Item "installed" "$n files" "ok"

Write-Step "Verifying what landed"

# This is the only real check in the run, and until 2026-08-08 it did not exist.
# Measured that day: Expand-Archive extracts a zip with a flipped byte WITHOUT an
# error and writes the wrong bytes to disk -- three offsets inside the compressed
# stream, three silent successes. TLS covers the wire and nothing covered the
# file, so a damaged install would have surfaced later as a DLL that will not
# load, and been diagnosed as anything but a bad download.
#
# The package carries a hash per file. Reading it is the whole fix.
$manifestPath = Join-Path $InstallTo "MANIFEST.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    Write-Item "MANIFEST.json" "not in the package -- nothing to verify against" "fail"
    Exit-Run 1
    return
}

$entries = @()
foreach ($e in (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json)) {
    # -LiteralPath throughout: two of the shipped files are named
    # GoogleSansCode[MONO,wght].ttf, and Test-Path reads [...] as a wildcard.
    # A check that reports those two as missing on every single install would
    # have been worse than no check at all.
    $file = Join-Path $InstallTo $e.path
    $actual = if (Test-Path -LiteralPath $file) {
        (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    } else { $null }
    $entries += [pscustomobject]@{ Path = $e.path; Expected = $e.sha256; Actual = $actual }
}

$verdict = Compare-Manifest $entries
if (-not $verdict.Ok) {
    foreach ($p in $verdict.Missing)    { Write-Item "missing" $p "fail" }
    foreach ($p in $verdict.Mismatched) { Write-Item "corrupt" $p "fail" }
    Write-Host ""
    Write-Item "the install is damaged" "delete $InstallTo and run this again" "fail"
    Exit-Run 1
    return
}
Write-Item "sha256 per file" "$($verdict.Checked) of $($verdict.Checked) match" "ok"

# The other direction. Everything above walks the manifest and asks the disk; a
# file the package USED to ship and no longer does is invisible to it and stays
# forever. Order matters and is the same as everywhere else here: the files were
# written and read back first, and only a verified install gets anything deleted.
#
# Both lists are manifests, never a directory listing. A file this installer did
# not put here is not in either one and cannot be selected -- which is the entire
# difference to the version that deleted a user's backup folder.
$drop = Find-DroppedFiles -PreviousPaths $previousManifest -CurrentPaths $entries.Path
$targets = @($drop.Dropped | Where-Object { Test-Path -LiteralPath (Join-Path $InstallTo $_) })
foreach ($rel in $targets) {
    Remove-Item -LiteralPath (Join-Path $InstallTo $rel) -Force -ErrorAction SilentlyContinue
}
# Counted by looking afterwards, not by counting what was found -- the same
# lesson as Remove-StaleOld below: a locked or read-only file survives the call
# without raising, and reporting it as removed prints a green line for work that
# did not happen.
$stillThere = @($targets | Where-Object { Test-Path -LiteralPath (Join-Path $InstallTo $_) })
$gone = $targets.Count - $stillThere.Count
if ($gone -gt 0) {
    Write-Item "removed" "$gone files the previous package left, of $($drop.Checked) it had installed" "ok"
}
if ($stillThere.Count -gt 0) {
    Write-Item "could not remove" "$($stillThere.Count) of $($targets.Count): $($stillThere -join ', ')" "warn"
}
if ($targets.Count -eq 0) {
    # With the denominator, always. Without it this line is indistinguishable
    # from a run that had no previous manifest to compare against at all.
    Write-Item "nothing to remove" "$($drop.Checked) files the previous package installed are all still shipped" "ok"
}

# The .old files from the rename above. The ones the running server still has
# mapped cannot be deleted -- and that is the normal case, because the reason
# they were renamed is that it is still running. So the counts come from looking
# afterwards, and what stays is said out loud rather than counted as removed.
if (Test-Path -LiteralPath $binDir) {
    $swept = Remove-StaleOld -BinDir $binDir
    if ($swept.Removed -gt 0) {
        Write-Item "cleaned" "$($swept.Removed) stale .old files removed" "ok"
    }
    if ($swept.Kept.Count -gt 0) {
        Write-Item "still held" "$($swept.Kept.Count) .old files stay until llama-server stops" "warn"
    }
}

# --slot-save-path refuses to start against a path that is not an existing
# directory, so the server would fail on the very command this script prints.
# Created here rather than by the client, because the server needs it first.
New-Item -ItemType Directory -Force -Path (Join-Path $InstallTo "session") | Out-Null

Write-Step "What is left to do"

$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) { Write-Item "python" "$($py.Source)" "ok" }
else     { Write-Item "python" "not on the PATH -- the client needs it" "warn" }

Write-Host ""
Write-Host "  1. The model. It is NOT part of this install: 84.6 GiB, three files, and it" -ForegroundColor DarkGray
Write-Host "     belongs to somebody else." -ForegroundColor DarkGray
Write-Host ""
Write-Host "       DeepSeek-V4-Flash-0731, quantised by unsloth to UD-IQ2_XXS" -ForegroundColor White
Write-Host "       https://huggingface.co/unsloth/DeepSeek-V4-Flash-0731-GGUF" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    hf download unsloth/DeepSeek-V4-Flash-0731-GGUF --include 'UD-IQ2_XXS/*' --local-dir $InstallTo\models" -ForegroundColor White
Write-Host ""
# Measured 2026-08-07: when hf cannot reach the repository it prints a tick and
# returns the local directory. Failure that looks like success is worth one line
# here, because the next thing the user does is start a server against nothing.
Write-Host "     If that finishes suspiciously fast, check that four files and ~97 GiB" -ForegroundColor DarkGray
Write-Host "     actually arrived -- hf reports success even when it reached nothing." -ForegroundColor DarkGray
Write-Host ""
# WHY --moe-stream-cache SAYS 58 AND NOT 64, and this note sits HERE rather than beside the flag on
# purpose: check_operating_point.py reads a 2000-character region starting at "llama-server.exe" to
# find the command line, and a long comment inside that window pushes the last flags out of it and
# reports this installer as broken. Measured the hard way on 2026-08-11, which is also when the
# number itself changed.
#
# A slot costs 360.69 MiB on 0731 -- the server prints the cache size at every step and the figure is
# constant to five digits. Past a point the allocation no longer fits beside what the display holds,
# the driver moves the difference into host memory without printing anything, and the cost is not a
# constant slowdown but an unpredictable one: single requests halve at random, with identical graph
# executions, identical misses and the LOWEST load stall of the run.
#
# Measured 2026-08-11, with the number of runs behind each cell, VRAM read after load of 32,607 MiB:
#   64 -> prefill 15.28 median of 8, spread 8.69x / no decode series / 32,014 used /   593 free
#   62 -> 114.92 median of 5 / 14.62 median of 4, one request at 7.07 / 31,683      /   924
#   60 -> 113.53 median of 5 / 17.43 median of 8                     / 30,954      / 1,653
#   58 -> 112.69 median of 3 / 17.32 median of 8                     / 30,548      / 2,059  <- shipped
#   56 -> 110.30 median of 3 / 17.09 median of 8                     / 29,842      / 2,765
#
# THE ARITHMETIC IS THE EVIDENCE. 62 reads 31,683 and 60 reads 30,954 -- 729 MiB for two slots,
# against the 721 the printed slot size predicts. So 64 must read about 32,404, and it reads 32,014.
# Those 390 MiB are the ones the driver moved out. 58 against 60 is NOT a throughput difference
# (112.69 vs 113.53, 17.32 vs 17.43, while repeating one configuration eight times spans 1.09x);
# what separates them is the margin.
#
# THIS NUMBER IS DERIVED FROM 32,607 MiB OF VRAM AND WHATEVER THE DISPLAY HOLDS BESIDE IT. A smaller
# card, or a busier display, needs a smaller value. Deriving it from the machine the way
# --moe-stream-l2 is derived is tracked as #87 rather than guessed at here.
Write-Host "  2. Then start the server, and the client in a second terminal:" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    $InstallTo\bin\llama-server.exe -m $InstallTo\models\UD-IQ2_XXS\DeepSeek-V4-Flash-0731-UD-IQ2_XXS-00001-of-00003.gguf ``" -ForegroundColor White
# --jinja is not optional here. Without it llama-server uses its own built-in
# template instead of the model's, the client's replayed reasoning is dropped,
# and the prompt cache breaks on every turn -- measured 2026-08-08, 138.8-242.3 s
# of re-prefill per turn against 1.6-2.2 s. This is the line people copy, so it
# is the line that has to carry the flag.
Write-Host "      --port 8081 -c 200000 -ngl 99 -np 1 --jinja ``" -ForegroundColor White
# --slot-save-path is what lets a session survive at all. Without it the client
# can keep its messages but not the server's KV cache, and the next start pays a
# full prefill for the whole history -- measured 2026-08-08: 23,400 tokens took
# about 35 minutes, while restoring the same state took 22 ms.
Write-Host "      --slot-save-path $InstallTo\session ``" -ForegroundColor White
# 0731 ships no Jinja chat template, and the one embedded in the GGUF fails the
# model's own golden vector 4 (an action turn opens a think block it never
# closes). The verified template ships with this package; without the flag the
# server silently uses the embedded one and nothing looks wrong.
Write-Host "      --chat-template-file $InstallTo\templates\0731-chat-template.jinja ``" -ForegroundColor White
# 58 and not 64 -- the reason is above the block, outside the region the checker reads.
#
# The host-RAM tier is printed only on a machine with the RAM it was measured on. On anything
# smaller the line is left out entirely rather than carrying a smaller, unmeasured number - a flag
# in the command people copy is a recommendation, and this project does not recommend figures it
# has not run.
$l2 = Get-L2Gib -RamGb $facts.RamGb
if ($l2 -gt 0) {
    Write-Host "      --moe-stream --moe-stream-cache 58s --moe-stream-io-threads 8 --moe-stream-direct ``" -ForegroundColor White
    Write-Host "      --moe-stream-l2 $l2" -ForegroundColor White
} else {
    Write-Host "      --moe-stream --moe-stream-cache 58s --moe-stream-io-threads 8 --moe-stream-direct" -ForegroundColor White
}
Write-Host ""
Write-Host "    python $InstallTo\cli\crow.py" -ForegroundColor White
Write-Host ""

# The last screen is the only place these three commands appear, and a console
# that was opened for the install closes with it. So the run ends on a keypress
# rather than on its own -- measured the hard way on 2026-08-08, when the first
# public install finished correctly and the window was gone before anyone could
# read what it had just printed.
#
# Skipped when there is nobody to press it: -NoPause for a script driving this,
# and UserInteractive for a host with no console at all. A wait that cannot be
# satisfied is not a safety net, it is a hang.
if (-not $NoPause -and [Environment]::UserInteractive) {
    Write-Host "  Press ENTER to finish." -ForegroundColor DarkGray
    try { [void](Read-Host) } catch { }
    Write-Host ""
}

# Said out loud rather than inherited. Without this the run ends with whatever
# the last native call left in $LASTEXITCODE, which is how a finished install
# came to report 255. Every failure path above sets 1 explicitly, so reaching
# this line means it worked.
Exit-Run 0
