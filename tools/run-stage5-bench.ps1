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
    [string]$SelfTest = '',
    # Overrides the control file. The 49.98 GB default was chosen because it sits above
    # the 46.5 GB threshold the plan sets for the drive's own cache - but measured
    # 2026-08-03, Windows kept only 1.5 GiB of it resident after a full sequential pass,
    # so the control never got the warm file its verdict depends on. A smaller file trades
    # that threshold away for a control that can actually be resident. The trade is
    # conservative: if the drive's cache does inflate the unbuffered arm, the buffered arm
    # has to beat a HIGHER number, so a control that still holds holds honestly.
    [string]$ControlModel = '',
    # Stops after arm 1. The large-file passes mean nothing until the control holds.
    [switch]$ControlOnly,
    # Proves - or refutes - that direct I/O bypasses the page cache, on the file given.
    #
    # Measured 2026-08-03, and it is why this mode exists: on a file held 100 % resident,
    # a buffered read reached 6555.8 MB/s while an unbuffered read of the same file
    # reached 8463.2 MB/s. The page cache is SLOWER than this drive, so the original
    # control - "buffered on a warm file must win, it is reading RAM" - cannot pass here
    # no matter how well direct I/O works. That premise was wrong, not the code.
    #
    # This asks the question that does not depend on which path is faster: if direct I/O
    # really bypasses the cache, then whether the file is cached must not matter to it.
    # Read it direct with the cache flushed, fill the cache, read it direct again. Same
    # number means the cache is irrelevant to that path. A jump means it is leaking.
    [string]$BypassTest = '',
    # Read buffered to push the target out of the cache before the cold pass. Without it
    # step 1 measures a file that is still resident from an earlier run.
    [string]$EvictWith = ''
)

. "$PSScriptRoot\model-paths.ps1"

$ErrorActionPreference = 'Continue'

# Numbers printed here get quoted into issues. On a German Windows the default culture
# renders 2491.4 as "2.491,4", which reads as two thousand four hundred - or as 2.4914,
# depending on who is reading. The figures are the product of this script, so they are
# formatted culture-invariantly and there is nothing to misread.
[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture

$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BenchExe = Join-Path $ToolsDir 'build-bench\bench-loader.exe'
$GgmlDll  = Join-Path $ToolsDir 'build-bench\ggml-base.dll'

$ModelLarge = (Get-ModelPath 'mxfp4')
$ModelSmall = (Get-ModelPath 'iq1-s')
if ($ControlModel -ne '') { $ModelSmall = $ControlModel }

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
    # GiB, and named GiB. PowerShell's 1GB is 1073741824, while the throughput line and
    # the file sizes above are decimal. Mixing the two silently made a 49.98 GB file
    # appear as "46.5 GB" in the same run - the units have to say which they are.
    return [pscustomobject]@{
        FreeGiB    = [double]$os.FreePhysicalMemory * 1KB / 1GB
        StandbyGiB = $standby / 1GB
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

if ($BypassTest -ne '') {
    if ($EvictWith -eq '') { $EvictWith = $ModelSmall }
    if (-not (Test-Path $BypassTest)) { Write-Output "no such file: $BypassTest"; exit 1 }
    if (-not (Test-Path $EvictWith))  { Write-Output "no such file: $EvictWith";  exit 1 }

    $tgtGiB = (Get-Item $BypassTest).Length / 1GB
    Write-Output ""
    Write-Output "--- bypass test: does the cache state change the direct path? ---"
    Write-Output ("target  {0:N0} bytes ({1:N1} GiB)  {2}" -f (Get-Item $BypassTest).Length, $tgtGiB, $BypassTest)
    Write-Output ("evict   {0}" -f $EvictWith)

    # 1. Flush. The target is resident from any earlier run, so a "cold" pass without
    #    this step is not cold at all - it just looks like a baseline.
    Write-Output ""
    Write-Output "  1. flushing the target out of the cache (buffered pass over another file)"
    $mBefore = Get-MemSnapshot
    Show-Bench (Invoke-Bench -Model $EvictWith -Buffered $true -LogName 'bypass-evict')
    $mFlushed = Get-MemSnapshot
    Write-Output ("     standby cache {0:N1} -> {1:N1} GiB" -f $mBefore.StandbyGiB, $mFlushed.StandbyGiB)

    # 2. Cold direct.
    Write-Output ""
    Write-Output "  2. direct read, cache flushed"
    $rCold = Invoke-Bench -Model $BypassTest -Buffered $false -LogName 'bypass-direct-cold'
    Show-Bench $rCold

    # 3. Fill the cache with the target and prove it landed.
    #
    # TWO passes, and two independent witnesses, because either one alone can be blind.
    # Standby GROWTH only shows residency while the cache has room to grow: measured
    # 2026-08-03, after the flush above left it at 51.0 GiB the cache merely REPLACED
    # pages, grew 0.5 GiB against a 30.3 GiB file, and the test called itself
    # inconclusive while the file may well have been resident.
    # The second witness does not care: if pass 2 is clearly faster than pass 1, pass 2
    # was served from memory. That is residency showing itself rather than being counted.
    Write-Output ""
    Write-Output "  3. two buffered reads to make the target resident"
    $mPre = Get-MemSnapshot
    $fill1 = Invoke-Bench -Model $BypassTest -Buffered $true -LogName 'bypass-warm-fill1'
    Show-Bench $fill1
    $fill2 = Invoke-Bench -Model $BypassTest -Buffered $true -LogName 'bypass-warm-fill2'
    Show-Bench $fill2
    $mPost = Get-MemSnapshot
    $grew = $mPost.StandbyGiB - $mPre.StandbyGiB
    $resident = 100 * $grew / $tgtGiB
    Write-Output ("     standby cache {0:N1} -> {1:N1} GiB, grew {2:N1} GiB against {3:N1} GiB -> {4:N0} % by growth" -f $mPre.StandbyGiB, $mPost.StandbyGiB, $grew, $tgtGiB, $resident)

    $speedup = 0.0
    if ($null -ne $fill1.MBs -and $null -ne $fill2.MBs) {
        $speedup = 100.0 * ($fill2.MBs - $fill1.MBs) / $fill1.MBs
        Write-Output ("     buffered pass 2 against pass 1: {0:+0.0;-0.0;0.0} %" -f $speedup)
    }
    if ($resident -lt 80 -and $speedup -lt 15.0) {
        Write-Output "     neither witness shows residency"
    } else {
        # Either witness is enough; they answer the same question by different means.
        $resident = 100
    }

    # 4. Warm direct.
    Write-Output ""
    Write-Output "  4. direct read, target now resident"
    $rWarm = Invoke-Bench -Model $BypassTest -Buffered $false -LogName 'bypass-direct-warm'
    Show-Bench $rWarm

    Write-Output ""
    Write-Output "=== bypass verdict ==="
    if ($null -eq $rCold.MBs -or $null -eq $rWarm.MBs) {
        Write-Output "INCONCLUSIVE: a pass did not produce a figure."
        exit 1
    }
    if ($resident -lt 80) {
        # Without residency the warm pass is not warm and the comparison says nothing.
        Write-Output "INCONCLUSIVE: the target did not become resident by either witness, so the"
        Write-Output "              warm pass was not warm and proves nothing either way."
        exit 1
    }
    $delta = 100.0 * ($rWarm.MBs - $rCold.MBs) / $rCold.MBs
    Write-Output ("cold {0:N1} MB/s   warm {1:N1} MB/s   delta {2:+0.00;-0.00;0.00} %" -f $rCold.MBs, $rWarm.MBs, $delta)
    if ([math]::Abs($delta) -le 5.0) {
        Write-Output "BYPASS PROVEN: filling the cache did not change the direct path. Whether the"
        Write-Output "               data sits in RAM is invisible to it, which is what"
        Write-Output "               FILE_FLAG_NO_BUFFERING is supposed to mean."
        exit 0
    }
    if ($delta -gt 5.0) {
        Write-Output "LEAK: the direct path got faster once the data was in RAM. It is reading"
        Write-Output "      cache, and every direct figure in this project is suspect."
        exit 1
    }
    Write-Output "UNEXPECTED: the direct path got slower with the data resident. Not a leak,"
    Write-Output "            but not explained either - do not quote a figure on this."
    exit 1
}

Write-Output ""
Write-Output ("--- arm 1: control on {0} ---" -f (Split-Path -Leaf $ModelSmall))

$before = Get-MemSnapshot
Write-Output ("  memory before warm-up: free {0:N1} GiB, standby cache {1:N1} GiB" -f $before.FreeGiB, $before.StandbyGiB)

Write-Output "  warming the file into the page cache (buffered pass, not measured)"
Show-Bench (Invoke-Bench -Model $ModelSmall -Buffered $true -LogName 'small-warmup')

$after = Get-MemSnapshot
$cachedGiB = $after.StandbyGiB - $before.StandbyGiB
$smallGiB  = (Get-Item $ModelSmall).Length / 1GB
# An approximation, and named as one: the standby cache grows for other reasons too.
# It is still a measurement rather than the assumption that the file "is warm".
Write-Output ("  memory after  warm-up: free {0:N1} GiB, standby cache {1:N1} GiB" -f $after.FreeGiB, $after.StandbyGiB)
Write-Output ("  standby cache grew by {0:N1} GiB against a {1:N1} GiB file -> roughly {2:N0} % resident (approximation)" -f $cachedGiB, $smallGiB, (100 * $cachedGiB / $smallGiB))

$rBuffered = Invoke-Bench -Model $ModelSmall -Buffered $true  -LogName 'small-buffered'
Show-Bench $rBuffered
$rDirect   = Invoke-Bench -Model $ModelSmall -Buffered $false -LogName 'small-direct'
Show-Bench $rDirect

$ctlBuffered = $rBuffered.MBs
$ctlDirect   = $rDirect.MBs

# --------------------------------------------------------------------------------
$runs = @()
if (-not $ControlOnly) {
    Write-Output ""
    Write-Output "--- arm 2: three unbuffered passes over the 155 GB model ---"

    for ($i = 1; $i -le 3; $i++) {
        if ($i -gt 1) {
            Write-Output "  pause 5 s"
            Start-Sleep -Seconds 5
        }
        $r = Invoke-Bench -Model $ModelLarge -Buffered $false -LogName ("large-direct-run$i")
        Show-Bench $r
        $runs += ,$r.MBs
    }
}

# --------------------------------------------------------------------------------
Write-Output ""
Write-Output "=== results ==="
Write-Output ("control buffered (RAM)   {0}" -f $(if ($null -eq $ctlBuffered) { 'FAILED' } else { '{0:N1} MB/s' -f $ctlBuffered }))
Write-Output ("control direct   (disk)  {0}" -f $(if ($null -eq $ctlDirect)   { 'FAILED' } else { '{0:N1} MB/s' -f $ctlDirect }))
for ($i = 0; $i -lt $runs.Count; $i++) {
    Write-Output ("large run $($i+1)             {0}" -f $(if ($null -eq $runs[$i]) { 'FAILED' } else { '{0:N1} MB/s' -f $runs[$i] }))
}
if ($ControlOnly) { Write-Output "large runs               skipped (-ControlOnly)" }

$ok = $true

# The control decides whether the main figure means anything, so it is judged first.
if ($null -eq $ctlBuffered -or $null -eq $ctlDirect) {
    Write-Output ""
    Write-Output "VOID: the control did not complete. The main figures are not interpretable."
    $ok = $false
} else {
    # The first version of this gate demanded buffered >= 1.5x direct, on the assumption
    # that a warm file is read from RAM and RAM wins. Measured 2026-08-03 on a file held
    # 100 % resident: buffered 6555.8, direct 8463.2 - the page cache is slower than this
    # drive, so that gate could never pass here however well the flag worked. It was the
    # premise that was wrong.
    #
    # What the two arms CAN settle without assuming which is faster: if the flag did
    # nothing, both would be the same code path and would land on the same number.
    # Separation in either direction means two paths; no separation means one. Which
    # path is faster is a finding, not a pass criterion.
    $ratio = $ctlBuffered / $ctlDirect
    $sep   = [math]::Max($ctlBuffered, $ctlDirect) / [math]::Min($ctlBuffered, $ctlDirect)
    Write-Output ""
    Write-Output ("control ratio buffered/direct: {0:N2}x   separation: {1:N2}x" -f $ratio, $sep)
    if ($sep -lt 1.05) {
        Write-Output "VOID: the two arms landed within noise of each other, so they are taking"
        Write-Output "      the same path. FILE_FLAG_NO_BUFFERING is not doing anything and the"
        Write-Output "      figures must not be quoted."
        $ok = $false
    } else {
        Write-Output "control HELD: the arms are measurably different paths."
        if ($ratio -lt 1.0) {
            Write-Output ("      Direct is the faster of the two ({0:N2}x). Not the direction the" -f (1 / $ratio))
            Write-Output "      first design expected - see the note above. Separation, not"
            Write-Output "      direction, is what this gate tests. Whether direct actually"
            Write-Output "      bypasses the cache is settled by -BypassTest, not here."
        }
    }
}

$good = @($runs | Where-Object { $null -ne $_ })
if ($ControlOnly) {
    Write-Output "spread: not applicable, the large-file passes were skipped."
} elseif ($good.Count -eq 3) {
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
