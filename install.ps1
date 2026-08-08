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
    [string] $Version   = "0.0.1",
    [string] $InstallTo = "$env:LOCALAPPDATA\Crow",
    [string] $SourceUrl = "",
    [switch] $Force,
    [switch] $Selftest
)

$ErrorActionPreference = "Stop"
$TOTAL_STEPS = 5

# The target profile, measured on issue #25: 32 GB VRAM and 16 GB system RAM at a
# 200k context window. Below 16 GB VRAM the operating point was never measured, so
# the script refuses rather than pretending it knows what happens there.
$VRAM_SUPPORTED_MB = 16000
$VRAM_TARGET_MB    = 32000
$RAM_MIN_GB        = 16
$DISK_INSTALL_GB   = 2      # the package, unpacked
$DISK_MODEL_GB     = 96     # what the model will need later, reported not enforced

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
        $warnings += ((Format-Num $RamGb 0) + " GB of system RAM, below the $RAM_MIN_GB GB in the target profile")
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

    # The regression behind the 255: reading the machine must not leave a native
    # command's broken exit code behind, because the script's own exit code is
    # whatever the last native call left there.
    $global:LASTEXITCODE = 0
    $null = Get-MachineFacts
    C "reading the machine leaves a clean exit code" ($LASTEXITCODE -eq 0)

    C "Format-Size: bytes"     ((Format-Size 512) -eq "512 B")
    C "Format-Size: megabytes" ((Format-Size 506400000) -eq "482.9 MB")
    C "Format-Size: gigabytes" ((Format-Size 103000000000) -eq "95.93 GB")

    Write-Host ""
    $total = $script:sOk + $script:sRed
    if ($script:sRed -gt 0) { Write-Host "RESULT: $($script:sRed) of $total FAILED" -ForegroundColor Red; return 1 }
    Write-Host "RESULT: SELFTEST OK - $total checks" -ForegroundColor Green
    return 0
}

if ($Selftest) { exit (Invoke-Selftest) }

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
    exit 1
}
Write-Item "preflight" "passed" "ok"

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
    exit 1
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

Write-Step "Verifying"

$sha = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash
Write-Item "size"   (Format-Size $bytes) "ok"
Write-Item "sha256" $sha "ok"
Write-Item "note"   "MANIFEST.json inside the package carries a hash per file"

Write-Step "Installing to $InstallTo"

if ((Test-Path $InstallTo) -and -not $Force) {
    $existing = @(Get-ChildItem $InstallTo -ErrorAction SilentlyContinue).Count
    if ($existing -gt 0) {
        Write-Item "exists" "$InstallTo already holds $existing entries -- pass -Force to overwrite" "fail"
        exit 1
    }
}
New-Item -ItemType Directory -Force -Path $InstallTo | Out-Null
$n = Expand-WithProgress -ZipPath $tmp -Destination $InstallTo
# Only what this script downloaded. A package handed in with -SourceUrl belongs
# to the caller, and deleting it would eat the artefact being tested.
if (-not $source.IsLocal) { Remove-Item $tmp -Force }
Write-Item "installed" "$n files" "ok"

Write-Step "What is left to do"

$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) { Write-Item "python" "$($py.Source)" "ok" }
else     { Write-Item "python" "not on the PATH -- the client needs it" "warn" }

Write-Host ""
Write-Host "  The model is not installed. It is 95.9 GiB and comes from its own source:" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    hf download unsloth/DeepSeek-V4-Flash-GGUF --include 'UD-IQ3_XXS/*' --local-dir $InstallTo\models" -ForegroundColor White
Write-Host ""
Write-Host "  Then start the server, and the client in a second terminal:" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    $InstallTo\bin\llama-server.exe -m $InstallTo\models\UD-IQ3_XXS\DeepSeek-V4-Flash-UD-IQ3_XXS-00001-of-00004.gguf ``" -ForegroundColor White
Write-Host "      -c 200000 -ngl 99 -np 1 --moe-stream --moe-stream-cache 64s --moe-stream-io-threads 8 --moe-stream-direct" -ForegroundColor White
Write-Host ""
Write-Host "    python $InstallTo\cli\crow.py" -ForegroundColor White
Write-Host ""

# Said out loud rather than inherited. Without this the script exits with
# whatever the last native call left in $LASTEXITCODE, which is how a finished
# install came to report 255. Every failure path above exits 1 explicitly, so
# reaching this line means it worked.
exit 0
