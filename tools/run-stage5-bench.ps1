<#
Stage 5: drives bench-loader.exe through the whole measurement in one deterministic
pass, so the order of the runs is written down instead of remembered.

WHAT EACH ARM PROVES

Arm 1 runs on the 49.98 GB file and is the CONTROL. The same file is read twice, once
buffered and once unbuffered, back to back while it sits warm in RAM:
  - buffered   must come out CLEARLY FASTER - it is reading RAM
  - unbuffered must stay down at disk speed - that is FILE_FLAG_NO_BUFFERING working
If the two land close together, the flag never took hold, the main figure would be page
cache, and the run is VOID. Two figures have already been withdrawn in this project for
exactly the missing half of this check.

That file was picked over the 32.56 GB one on purpose: at 49.98 GB it is above the
46.5 GB threshold the plan sets, so the drive's own cache cannot serve the unbuffered
arm, and it is the same file stage 3 already warmed successfully.

Arm 2 runs the 155 GB model three times unbuffered. One measurement is a difference,
not a statement.

VARIANCE IS A FINDING, NOT NOISE TO BE REMOVED
If the three runs do not land close together, that spread IS the result and is reported
as such. Decode was measured at 91 % spread between two identical runs; averaging that
away would have hidden the only thing worth knowing. This script prints every single
run and never replaces them with a median.

TRAPS THIS SCRIPT AVOIDS, ALL PAID FOR ALREADY
  - bench-loader.exe lives in tools/build-bench/, not in tools/
  - the model files live under models/UD-IQ1_S/, and carry the DeepSeek-V4-Flash- prefix
  - the throughput line reads "RESULT   <n> MB/s", there is no "RESULT THROUGHPUT:"
    anywhere; a pattern that never matches turns every run into a silent 0.0
  - the tool writes its refusals to stderr, so a plain ">" would log a failed run with
    no reason in it. The exe is called through cmd.exe so the merge happens there:
    PowerShell 5.1 wraps a native command's stderr in ErrorRecords and would trip
    $ErrorActionPreference on a run that merely printed a warning.
  - exit codes are checked. bench-loader returns 1 when it refuses to measure, and a
    refusal that is read as 0.0 MB/s looks exactly like a slow drive.

EXPECTED DURATION is a function of the number being measured. At the 4812 MB/s target
the large runs are about 32 s each; if the read path turns out to be slow, they scale
with it - at 700 MB/s a single pass takes about 3.7 minutes. Watch the first run.

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI,
and a stray non-ASCII character breaks the parse in a misleading way.
#>
param(
    [string]$LogDir = '',
    # Runs the chain - call, exit code, log, regex - against one small file and stops.
    # The throughput figure it prints is meaningless; what is verified is that a real run
    # parses AND that a broken run does not come back as 0.0 MB/s. Before this existed,
    # the only evidence the parser matched was the printf in the C++ source, and a cause
    # read from code is a guess.
    [string]$SelfTest = ''
)

$ErrorActionPreference = 'Continue'

# Numbers printed here get quoted into issues. On a German Windows the default culture
# renders 2491.4 as "2.491,4", which reads as two thousand four hundred - or as 2.4914,
# depending on who is reading. The figures are the product of this script, so they are
# formatted culture-invariantly and there is nothing to misread.
[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture

$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BenchExe = Join-Path $ToolsDir 'build-bench\bench-loader.exe'
$GgmlDll  = Join-Path $ToolsDir 'build-bench\ggml-base.dll'

$ModelLarge = 'C:\Users\robin\dev\crow-lab\models\DeepSeek-V4-Flash-MXFP4.gguf'
$ModelSmall = 'C:\Users\robin\dev\crow-lab\models\UD-IQ1_S\DeepSeek-V4-Flash-UD-IQ1_S-00002-of-00003.gguf'

if ($LogDir -eq '') {
    $LogDir = Join-Path $ToolsDir ('..\runs\' + (Get-Date -Format 'yyyy-MM-dd') + '\stage5')
}
$LogDir = [System.IO.Path]::GetFullPath($LogDir)

# --------------------------------------------------------------------------------
# Preflight. Every input is checked before anything is read, because finding a missing
# file after the first 155 GB pass costs the whole pass.
# --------------------------------------------------------------------------------
$missing = @()
foreach ($f in @($BenchExe, $GgmlDll, $ModelLarge, $ModelSmall)) {
    if (-not (Test-Path $f)) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Output "MISSING INPUTS:"
    foreach ($m in $missing) { Write-Output "  $m" }
    Write-Output "Build the tool first: tools\build-bench.ps1"
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Get-MemSnapshot {
    $os  = Get-CimInstance Win32_OperatingSystem
    # CIM raw counters rather than Get-Counter: counter PATHS are localised on a German
    # Windows and '\Memory\Cache Bytes' does not exist there, while the class name does.
    $mem = Get-CimInstance Win32_PerfRawData_PerfOS_Memory
    $standby = [double]$mem.StandbyCacheNormalPriorityBytes +
               [double]$mem.StandbyCacheReserveBytes +
               [double]$mem.StandbyCacheCoreBytes
    return [pscustomobject]@{
        FreeGB    = [double]$os.FreePhysicalMemory * 1KB / 1GB
        StandbyGB = $standby / 1GB
    }
}

function Invoke-Bench {
    param(
        [string]$Model,
        [bool]$Buffered,
        [string]$LogName
    )

    $flag = ''
    if ($Buffered) { $flag = ' --buffered' }
    $logPath = Join-Path $LogDir "$LogName.txt"

    # This function prints NOTHING. Write-Output inside a PowerShell function goes to the
    # pipeline, so every progress line would end up in the return value and the caller
    # would get a block of text where it expected a number. Caught by the self test on
    # 2026-08-03, which is the whole reason the self test exists. Printing is Show-Bench's
    # job; this one only measures.

    # cmd.exe does the 2>&1 merge. Doing it in PowerShell wraps every stderr line in a
    # NativeCommandError and makes a healthy run look like a failure.
    $cmdline = '"' + $BenchExe + '" "' + $Model + '"' + $flag + ' 2>&1'
    $out  = & cmd.exe /c $cmdline
    $code = $LASTEXITCODE
    $text = ($out | Out-String)

    # WriteAllText rather than ">": PowerShell's redirect writes a UTF-8 BOM, and the
    # raw log is meant to be readable by anything, including a later json step.
    [System.IO.File]::WriteAllText($logPath, $text)

    $mbs = $null
    $m = [regex]::Match($text, 'RESULT\s+([0-9]+(?:\.[0-9]+)?)\s+MB/s')
    if ($m.Success) { $mbs = [double]$m.Groups[1].Value }

    # A refusal read as 0.0 MB/s is indistinguishable from a slow drive, so MBs stays
    # $null unless the run both exited clean and produced a parsable line.
    if ($code -ne 0) { $mbs = $null }

    return [pscustomobject]@{
        Label    = "$(Split-Path -Leaf $Model)$flag"
        MBs      = $mbs
        ExitCode = $code
        LogPath  = $logPath
        Tail     = ($text -split "`n" | Select-Object -Last 8)
    }
}

function Show-Bench {
    param($Result)
    Write-Output "  $($Result.Label)"
    if ($null -eq $Result.MBs) {
        Write-Output "  FAILED: exit=$($Result.ExitCode), no usable RESULT line. See $($Result.LogPath)"
        Write-Output "  ---- tail ----"
        $Result.Tail | ForEach-Object { Write-Output "    $($_.TrimEnd())" }
    } else {
        Write-Output ("  -> {0:N1} MB/s   (log: {1})" -f $Result.MBs, (Split-Path -Leaf $Result.LogPath))
    }
}

# --------------------------------------------------------------------------------
Write-Output "=== stage 5 measurement suite ==="
Write-Output "started   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Output "logs      $LogDir"
$exeInfo = Get-Item $BenchExe
Write-Output "binary    $($exeInfo.FullName)  $($exeInfo.Length) bytes  $($exeInfo.LastWriteTime)"
Push-Location $ToolsDir
$sha = (& git rev-parse --short HEAD)
Pop-Location
Write-Output "Crow HEAD $sha"
Write-Output ("small     {0:N0} bytes  {1}" -f (Get-Item $ModelSmall).Length, $ModelSmall)
Write-Output ("large     {0:N0} bytes  {1}" -f (Get-Item $ModelLarge).Length, $ModelLarge)

# --------------------------------------------------------------------------------
if ($SelfTest -ne '') {
    Write-Output ""
    Write-Output "--- self test: the chain, not the drive ---"

    $pos = Invoke-Bench -Model $SelfTest -Buffered $false -LogName 'selftest-positive'
    Show-Bench $pos

    # The case that must fail. A path that does not exist makes the constructor throw;
    # the tool writes to stderr and exits 1. If that came back as a number instead of a
    # failure, every later refusal would be logged as a slow drive.
    $neg = Invoke-Bench -Model ($SelfTest + '.does-not-exist') -Buffered $false -LogName 'selftest-negative'
    Show-Bench $neg

    Write-Output ""
    if ($null -ne $pos.MBs -and $null -eq $neg.MBs) {
        Write-Output "SELF TEST PASS - a real run parsed, a broken run did not become a number."
        exit 0
    }
    Write-Output "SELF TEST FAIL"
    if ($null -eq $pos.MBs) { Write-Output "  the positive run produced no figure" }
    if ($null -ne $neg.MBs) { Write-Output "  the broken run produced a figure: $($neg.MBs)" }
    exit 1
}

Write-Output ""
Write-Output "--- arm 1: control on the 49.98 GB file ---"

$before = Get-MemSnapshot
Write-Output ("  memory before warm-up: free {0:N1} GB, standby cache {1:N1} GB" -f $before.FreeGB, $before.StandbyGB)

Write-Output "  warming the file into the page cache (buffered pass, not measured)"
Show-Bench (Invoke-Bench -Model $ModelSmall -Buffered $true -LogName 'small-warmup')

$after = Get-MemSnapshot
$cached = $after.StandbyGB - $before.StandbyGB
$smallGB = (Get-Item $ModelSmall).Length / 1GB
# An approximation, and named as one: the standby cache grows for other reasons too.
# It is still a measurement rather than the assumption that the file "is warm".
Write-Output ("  memory after  warm-up: free {0:N1} GB, standby cache {1:N1} GB" -f $after.FreeGB, $after.StandbyGB)
Write-Output ("  standby cache grew by {0:N1} GB against a {1:N1} GB file -> roughly {2:N0} % resident (approximation)" -f $cached, $smallGB, (100 * $cached / $smallGB))

$rBuffered = Invoke-Bench -Model $ModelSmall -Buffered $true  -LogName 'small-buffered'
Show-Bench $rBuffered
$rDirect   = Invoke-Bench -Model $ModelSmall -Buffered $false -LogName 'small-direct'
Show-Bench $rDirect

$ctlBuffered = $rBuffered.MBs
$ctlDirect   = $rDirect.MBs

# --------------------------------------------------------------------------------
Write-Output ""
Write-Output "--- arm 2: three unbuffered passes over the 155 GB model ---"

$runs = @()
for ($i = 1; $i -le 3; $i++) {
    if ($i -gt 1) {
        Write-Output "  pause 5 s"
        Start-Sleep -Seconds 5
    }
    $r = Invoke-Bench -Model $ModelLarge -Buffered $false -LogName ("large-direct-run$i")
    Show-Bench $r
    $runs += ,$r.MBs
}

# --------------------------------------------------------------------------------
Write-Output ""
Write-Output "=== results ==="
Write-Output ("control buffered (RAM)   {0}" -f $(if ($null -eq $ctlBuffered) { 'FAILED' } else { '{0:N1} MB/s' -f $ctlBuffered }))
Write-Output ("control direct   (disk)  {0}" -f $(if ($null -eq $ctlDirect)   { 'FAILED' } else { '{0:N1} MB/s' -f $ctlDirect }))
for ($i = 0; $i -lt 3; $i++) {
    Write-Output ("large run $($i+1)             {0}" -f $(if ($null -eq $runs[$i]) { 'FAILED' } else { '{0:N1} MB/s' -f $runs[$i] }))
}

$ok = $true

# The control decides whether the main figure means anything, so it is judged first.
if ($null -eq $ctlBuffered -or $null -eq $ctlDirect) {
    Write-Output ""
    Write-Output "VOID: the control did not complete. The main figures are not interpretable."
    $ok = $false
} else {
    $ratio = $ctlBuffered / $ctlDirect
    Write-Output ""
    Write-Output ("control ratio buffered/direct: {0:N2}x" -f $ratio)
    if ($ratio -lt 1.5) {
        Write-Output "VOID: the buffered control did not break out above the unbuffered read."
        Write-Output "      Either the file never got warm, or FILE_FLAG_NO_BUFFERING is not"
        Write-Output "      taking hold - in both cases the large-file figures may be page"
        Write-Output "      cache and must not be quoted."
        $ok = $false
    } else {
        Write-Output "control HELD: buffered reads RAM, unbuffered does not."
    }
}

$good = @($runs | Where-Object { $null -ne $_ })
if ($good.Count -eq 3) {
    $mx = ($good | Measure-Object -Maximum).Maximum
    $mn = ($good | Measure-Object -Minimum).Minimum
    $spread = 100.0 * ($mx - $mn) / $mn
    Write-Output ("spread across the three passes: {0:N2} %" -f $spread)
    if ($spread -gt 5.0) {
        # Not a failure. The spread is the finding, exactly as it was for decode.
        Write-Output "NOTE: above the 5 % noise line. The spread is the result here, not"
        Write-Output "      something to average away. Report all three figures."
    }
} else {
    Write-Output "spread: not computed, not all three passes produced a figure."
    $ok = $false
}

Write-Output ""
Write-Output "raw logs in $LogDir"
Write-Output "finished  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

if ($ok) { exit 0 } else { exit 1 }
