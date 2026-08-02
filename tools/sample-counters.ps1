<#
sample-counters - how much disk traffic does an mmap'd run actually cause?

The number every factor on #30 stands against is 445 MB/s, and it was read by hand
off Resource Monitor during a --load-mode dio run. There is no artefact, and the
mmap case was never measured. This tool produces that number with a file behind it.

WHY NOT Get-Counter: counter names are localized (documented by Microsoft), and on
this German machine "\Memory\Pages Input/sec" fails with PDH_CSTATUS_NO_OBJECT. That
is normal behaviour, not a broken counter register - do NOT run lodctr /R over it.
The WMI raw classes carry language-neutral property names, so this tool reads those.
Measured 2026-08-02: the English Get-Counter paths fail, Win32_PerfRawData_* works.

WHY RAW AND NOT FORMATTED - and the first version of this paragraph was wrong.
It said Win32_PerfFormattedData could not be trusted because a single call at idle
returned 0 for every counter. Measured 2026-08-02 against a known ~10,000 MB/s load,
all three paths agree within 4 %: raw delta 9983-10220, formatted 9826-10421,
Get-Counter on localized paths 9975-10213 MB/s. The idle zeros were idle, not a dead
counter, and THAT CLAIM IS WITHDRAWN.
The raw path stays, for reasons that survive the measurement: it is language-neutral
without resolving localized names, and it needs one query per sample rather than two.
Raw values are cumulative, so every rate here is a delta over Timestamp_PerfTime.
-WithFormatted still logs the formatted value alongside, now as a running cross check
rather than as an open question.

WHY NOT -Pid: PowerShell reserves $Pid for the current process and refuses to bind a
parameter of that name ("Die Variable Pid kann nicht ueberschrieben werden"). Measured
2026-08-02. The parameter is -TargetPid.

WHY THE PROCESS IS FILTERED BY PID AND NOT BY NAME: Microsoft documents that the #N
suffixes on duplicate instance names are reassigned between samples and then yield
WRONG formatted values rather than none. IDProcess is stable for the life of a process.

THE NEGATIVE CONTROL, and why these three: a sampler that only ever prints numbers
cannot be told from one that prints noise. -Selftest runs three cases that each MUST
come back red, and each one covers a different way this tool could be silently broken:

  1. -MaxIdlePages -1  ->  the idle gate must trip. Any non-negative rate violates a
     threshold of -1, so this is red under every condition. A threshold of 0 would
     NOT be: measured 2026-08-02, PagesInputPersec moved by 0 over 3.03 s at idle, so
     "0 > 0" is false and the case would have passed on a healthy machine.
  2. -TargetPid on a dead PID  ->  must exit 2 and write NO rows. A sampler that
     writes zeros for a process that does not exist manufactures data.
  3. -ExpectDisableValue 1  ->  the registry check must trip. Windows disables a
     counter DLL silently after a load error and sets the value itself; without a case
     that fails, the check is a green that was never tested.

WHAT THIS TOOL DOES NOT DO: it never starts llama. It attaches to a PID or runs free
standing, because the idle baseline is a run WITHOUT a process to attach to.

TWO PATHS A CURATOR FOUND UNTESTED, now measured 2026-08-02:
  * The stop file. It had never fired: with a pid attached the process-gone branch
    always won first, so "the sampler is asked to stop, not killed" was an untested
    claim. Run free standing with -Seconds 120 and the flag dropped at 8 s, it ended
    after 8.4 s with exit 0 and its closing line. The path works.
  * The pages half of the idle gate. -MaxIdlePages had only ever been set to -1
    (always red) or infinity (always green), so 200 was a threshold nobody had made
    fail. Under random mmap reads across the 155 GB file it read 135,322.8 pages/s
    and the gate went red. In the same run fault throughput was 528.60 MB/s against
    529.07 MB/s at the disk driver - 0.09 % apart, two independent counter paths.

COLD START: the first WMI query in a fresh PowerShell process costs about 6 s. A run
launched with Start-Process therefore loses its first few ticks; the schedule below
skips them rather than firing them late. On a 614 s run that is one percent, but a
short run started cold can come back with far fewer rows than its duration suggests.

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM as
ANSI, and a stray non-ASCII character breaks the parse in a misleading way.

Usage:
  sample-counters.ps1 -LogDir <path> -Label idle-base -Seconds 20
  sample-counters.ps1 -LogDir <path> -Label run-a -TargetPid 1234 -Seconds 700 `
                      -MaxIdlePages 200 -MaxIdleDiskMBs 10
  sample-counters.ps1 -LogDir <path> -Selftest

Exit 0 = sampled, every check held.
     1 = sampled, a stated criterion was violated.
     2 = SETUP ERROR, nothing was sampled.
#>
param(
    # 0 = free standing, no process columns. Not named -Pid: see header.
    [int]$TargetPid = 0,
    [Parameter(Mandatory = $true)][string]$LogDir,
    [string]$Label = '',
    [int]$Seconds = 60,
    [int]$IntervalMs = 1000,
    # PositiveInfinity = gate off. -1 = a threshold no non-negative rate can meet,
    # which is what -Selftest uses. Do not conflate the two.
    [double]$MaxIdlePages = [double]::PositiveInfinity,
    [double]$MaxIdleDiskMBs = [double]::PositiveInfinity,
    # Expected value of "Disable Performance Counters" under each Perf* service.
    # 0 or absent is healthy. -Selftest passes 1 so the check has a failing case.
    [int]$ExpectDisableValue = 0,
    # Log Win32_PerfFormattedData alongside the raw delta. Off by default because the
    # call costs 271 ms of a 1000 ms interval (measured, see Get-Snapshot). It is the
    # cross check for whether the formatted shortcut works at all - stage 2 sets it.
    [switch]$WithFormatted,
    [switch]$Selftest
)

$ErrorActionPreference = 'Continue'

# Every number written to the CSV goes through this. PowerShell's -f operator
# formats in the SYSTEM culture, which on this machine is German: 9874.61 comes out
# as "9874,61" and blows a comma-separated file apart one field per decimal. Measured
# 2026-08-02 - a 14-column header sat over rows that had 28 fields, and the file was
# unreadable while the console numbers were correct. An artefact nobody can parse is
# not an artefact, and artefacts are the whole reason this tool exists.
$inv = [System.Globalization.CultureInfo]::InvariantCulture

# ---------------------------------------------------------------- helpers

# Cost per class, measured on this machine 2026-08-02 (median of 5 calls):
#   PerfOS_Memory                     10 ms
#   PerfDisk_PhysicalDisk (_Total)    11 ms
#   PerfProc_Process -Filter WQL      72 ms
#   PerfProc_Process piped to Where  111 ms   <- why the WQL filter is used
#   PerfFormattedData_PerfOS_Memory  271 ms   <- why it is behind a switch
# Without the switch a sample costs ~93 ms with a pid, so a 1000 ms interval really
# is 1 Hz. With it, ~364 ms, and a 5 s run produced 3 rows instead of 5.
function Get-Snapshot {
    param([int]$ProcId, [switch]$WithFormatted)
    $mem  = Get-CimInstance Win32_PerfRawData_PerfOS_Memory -ErrorAction SilentlyContinue
    $disk = Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk -Filter "Name='_Total'" -ErrorAction SilentlyContinue
    $fmt  = $null
    if ($WithFormatted) {
        $fmt = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -ErrorAction SilentlyContinue
    }
    $proc = $null
    if ($ProcId -gt 0) {
        $proc = Get-CimInstance Win32_PerfRawData_PerfProc_Process -Filter "IDProcess=$ProcId" -ErrorAction SilentlyContinue |
                Select-Object -First 1
    }
    if ($null -eq $mem -or $null -eq $disk) { return $null }
    [pscustomobject]@{
        ts        = [double]$mem.Timestamp_PerfTime
        freq      = [double]$mem.Frequency_PerfTime
        ts100     = [double]$disk.Timestamp_Sys100NS
        pagesIn   = [double]$mem.PagesInputPersec
        pageReads = [double]$mem.PageReadsPersec
        memFaults = [double]$mem.PageFaultsPersec
        dTs       = [double]$disk.Timestamp_PerfTime
        dFreq     = [double]$disk.Frequency_PerfTime
        dBytes    = [double]$disk.DiskReadBytesPersec
        dReads    = [double]$disk.DiskReadsPersec
        dAvgB     = [double]$disk.AvgDiskBytesPerRead
        dAvgBBase = [double]$disk.AvgDiskBytesPerRead_Base
        dQueue    = [double]$disk.AvgDiskReadQueueLength
        fmtPages  = if ($null -ne $fmt) { [double]$fmt.PagesInputPersec } else { [double]::NaN }
        pIo       = if ($null -ne $proc) { [double]$proc.IOReadBytesPersec } else { [double]::NaN }
        pFaults   = if ($null -ne $proc) { [double]$proc.PageFaultsPersec } else { [double]::NaN }
        pWs       = if ($null -ne $proc) { [double]$proc.WorkingSet } else { [double]::NaN }
        pTs       = if ($null -ne $proc) { [double]$proc.Timestamp_PerfTime } else { [double]::NaN }
        pFreq     = if ($null -ne $proc) { [double]$proc.Frequency_PerfTime } else { [double]::NaN }
        procFound = ($null -ne $proc)
    }
}

# PERF_COUNTER_COUNTER: (v2-v1) / elapsed seconds.
function Get-Rate {
    param([double]$v1, [double]$v2, [double]$ts1, [double]$ts2, [double]$freq)
    if ($freq -le 0) { return [double]::NaN }
    $dt = ($ts2 - $ts1) / $freq
    if ($dt -le 0) { return [double]::NaN }
    return ($v2 - $v1) / $dt
}

# PERF_AVERAGE_BULK: (v2-v1) / (base2-base1). No time involved.
function Get-Average {
    param([double]$v1, [double]$v2, [double]$b1, [double]$b2)
    $db = $b2 - $b1
    if ($db -le 0) { return [double]::NaN }
    return ($v2 - $v1) / $db
}

# PERF_COUNTER_100NS_QUEUELEN: (v2-v1) / (t100ns2-t100ns1).
function Get-QueueLen {
    param([double]$v1, [double]$v2, [double]$t1, [double]$t2)
    $dt = $t2 - $t1
    if ($dt -le 0) { return [double]::NaN }
    return ($v2 - $v1) / $dt
}

# Windows turns a counter off by itself after a DLL load error and records the fact
# here. Absent means healthy. Compared against -ExpectDisableValue so the check has
# a case that can fail (-Selftest passes 1).
function Test-CounterRegistry {
    param([int]$Expect)
    $bad = @()
    foreach ($svc in @('PerfOS', 'PerfProc', 'PerfDisk')) {
        $key = "HKLM:\SYSTEM\CurrentControlSet\Services\$svc\Performance"
        $val = $null
        try {
            $p = Get-ItemProperty -Path $key -Name 'Disable Performance Counters' -ErrorAction Stop
            $val = [int]$p.'Disable Performance Counters'
        } catch { $val = $null }
        $seen = if ($null -eq $val) { 0 } else { $val }
        if ($seen -ne $Expect) { $bad += "$svc = $(if ($null -eq $val) { 'absent (treated as 0)' } else { $val }), expected $Expect" }
    }
    return , $bad
}

# The damage of a stray CSV happens when it is WRITTEN, not when it is committed, so
# the guard sits here and not in .gitignore. A LogDir outside the repo is fine; one
# inside the repo that git would pick up is not.
function Test-LogDirIgnored {
    param([string]$Path)
    $repo = Split-Path -Parent $PSScriptRoot
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith([System.IO.Path]::GetFullPath($repo), [StringComparison]::OrdinalIgnoreCase)) {
        return "outside-repo"
    }
    $probe = Join-Path $full 'counters-probe.csv'
    cmd.exe /c "git -C `"$repo`" check-ignore --quiet `"$probe`"" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { return "ignored" }
    return "TRACKED"
}

# ---------------------------------------------------------------- the sampler

function Invoke-Sampling {
    param(
        [int]$ProcId, [string]$Dir, [string]$Tag, [int]$Secs, [int]$Ms,
        [double]$GatePages, [double]$GateDiskMBs, [int]$ExpectDisable,
        [switch]$Formatted, [switch]$Quiet
    )

    $result = [pscustomobject]@{
        Code = 2; Rows = 0; Csv = ''
        MedianPages = [double]::NaN; MedianDiskMBs = [double]::NaN
        MedianFaultMBs = [double]::NaN; MedianProcIoMBs = [double]::NaN
        MedianFmtPages = [double]::NaN; Invalid = 0; Reason = ''
        RawsNonZero = $false; MeanIntervalMs = [double]::NaN
    }

    $reg = Test-CounterRegistry -Expect $ExpectDisable
    if ($reg.Count -gt 0) {
        $result.Reason = "counter registry: " + ($reg -join '; ')
        if (-not $Quiet) { Write-Output "  SETUP ERROR: $($result.Reason)" }
        return $result
    }

    if (-not (Test-Path $Dir)) { New-Item -ItemType Directory -Path $Dir | Out-Null }
    $ign = Test-LogDirIgnored -Path $Dir
    if ($ign -eq 'TRACKED') {
        $result.Reason = "LogDir is inside the repo and NOT ignored by git: $Dir"
        if (-not $Quiet) { Write-Output "  SETUP ERROR: $($result.Reason)" }
        return $result
    }

    $csv = Join-Path $Dir "counters-$Tag.csv"
    if (Test-Path $csv) {
        $result.Reason = "output already exists, refusing to overwrite: $csv"
        if (-not $Quiet) { Write-Output "  SETUP ERROR: $($result.Reason)" }
        return $result
    }
    $result.Csv = $csv

    if ($ProcId -gt 0) {
        $probe = Get-Snapshot -ProcId $ProcId
        if ($null -eq $probe -or -not $probe.procFound) {
            $result.Reason = "no live process with PID $ProcId - writing no rows rather than zeros"
            if (-not $Quiet) { Write-Output "  SETUP ERROR: $($result.Reason)" }
            return $result
        }
    }

    $stopFlag = Join-Path $Dir "stop-$Tag.flag"
    if (Test-Path $stopFlag) { Remove-Item $stopFlag -Force }

    $header = 't_s,pages_in_per_s,page_reads_per_s,fault_mb_s,bytes_per_fault_read_kb,' +
              'disk_read_mb_s,disk_reads_per_s,disk_avg_bytes_per_read,disk_read_queue,' +
              'proc_io_read_mb_s,proc_page_faults_per_s,proc_ws_gb,formatted_pages_in,status'
    Set-Content -Path $csv -Value $header -Encoding utf8

    $pagesList = @(); $diskList = @(); $faultList = @(); $procIoList = @(); $fmtList = @()
    $started = Get-Date
    $prev = Get-Snapshot -ProcId $ProcId -WithFormatted:$Formatted
    if ($null -ne $prev -and $prev.pagesIn -gt 0 -and $prev.dBytes -gt 0) { $result.RawsNonZero = $true }
    $n = 0
    $tick = 0

    while ($true) {
        # Absolute schedule, not "sleep Ms then work". A sample costs 93-364 ms
        # depending on -WithFormatted, and sleeping a fixed Ms on top of that drifts:
        # a 5 s run at a nominal 1 Hz produced 3 rows, not 5. Measured 2026-08-02.
        #
        # AND IT DOES NOT CATCH UP. The first version of this schedule floored the
        # sleep at 20 ms, so after a stall it fired several samples in quick
        # succession. Measured 2026-08-02: a 2.86 s stall produced three samples
        # 0.11-0.17 s apart. The WMI counters do not refresh that fast, so those rows
        # read near zero and the whole backlog landed in the NEXT one - 10,627,477
        # pages/s, i.e. 41 GB/s on a drive that does 5. The corrupt row was not the
        # fast one but the one after it, which is exactly why dropping "too fast" rows
        # alone would have left the damage in place. Skipping whole ticks keeps every
        # interval at half the requested one or more, so no delta is ever cut short.
        $tick++
        $nowMs = ((Get-Date) - $started).TotalMilliseconds
        while ((($tick * $Ms) - $nowMs) -lt ($Ms * 0.5)) { $tick++ }
        Start-Sleep -Milliseconds ([int](($tick * $Ms) - $nowMs))

        $elapsed = ((Get-Date) - $started).TotalSeconds
        if ($elapsed -ge $Secs) { break }
        if (Test-Path $stopFlag) { break }
        if ($ProcId -gt 0) {
            $alive = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
            if ($null -eq $alive) { break }
        }

        $cur = Get-Snapshot -ProcId $ProcId -WithFormatted:$Formatted
        $dtSample = if ($null -ne $cur -and $null -ne $prev -and $cur.freq -gt 0) {
                        ($cur.ts - $prev.ts) / $cur.freq
                    } else { 0.0 }
        $status = 'ok'
        if ($null -eq $cur -or $null -eq $prev) {
            $status = 'invalid_null'
        } elseif ($cur.pagesIn -lt $prev.pagesIn -or $cur.dBytes -lt $prev.dBytes) {
            # A cumulative raw value that drops means the counter restarted. Every
            # delta across that point is garbage, so the row is marked, not skipped.
            $status = 'invalid_counter_reset'
        } elseif ($dtSample -lt ($Ms / 2000.0)) {
            # The net under the no-catch-up schedule above. If an interval still comes
            # out under half the requested one, the counters may not have refreshed
            # across it and any rate computed from it is noise, so the row is marked
            # rather than published as a number.
            $status = 'invalid_too_fast'
        }

        if ($status -ne 'ok') {
            $result.Invalid++
            Add-Content -Path $csv -Encoding utf8 -Value ([string]::Format($inv, "{0:F2},,,,,,,,,,,,,{1}", $elapsed, $status))
            $prev = $cur; $n++
            continue
        }

        $pagesRate = Get-Rate $prev.pagesIn   $cur.pagesIn   $prev.ts  $cur.ts  $cur.freq
        $readsRate = Get-Rate $prev.pageReads $cur.pageReads $prev.ts  $cur.ts  $cur.freq
        $dBytesRate= Get-Rate $prev.dBytes    $cur.dBytes    $prev.dTs $cur.dTs $cur.dFreq
        $dReadsRate= Get-Rate $prev.dReads    $cur.dReads    $prev.dTs $cur.dTs $cur.dFreq
        $avgBytes  = Get-Average $prev.dAvgB $cur.dAvgB $prev.dAvgBBase $cur.dAvgBBase
        $queue     = Get-QueueLen $prev.dQueue $cur.dQueue $prev.ts100 $cur.ts100

        # 4096 is measured on this machine ([System.Environment]::SystemPageSize),
        # not assumed - no counter definition fixes the page size.
        $pageSize  = [System.Environment]::SystemPageSize
        $faultMBs  = ($pagesRate * $pageSize) / 1MB
        $diskMBs   = $dBytesRate / 1MB
        # Microsoft's own quotient: pages per READ OPERATION, not per fault. Windows
        # clusters a fault into several contiguous pages, so naming this "bytes per
        # fault" reports a different quantity than it claims.
        $perRead   = if ($readsRate -gt 0) { ($pagesRate / $readsRate) * $pageSize / 1KB } else { [double]::NaN }

        $procIoMBs = [double]::NaN; $procFaults = [double]::NaN; $wsGb = [double]::NaN
        if ($ProcId -gt 0 -and $cur.procFound -and $prev.procFound) {
            $procIoMBs  = (Get-Rate $prev.pIo $cur.pIo $prev.pTs $cur.pTs $cur.pFreq) / 1MB
            $procFaults =  Get-Rate $prev.pFaults $cur.pFaults $prev.pTs $cur.pTs $cur.pFreq
            $wsGb       = $cur.pWs / 1GB
        }

        $pagesList += $pagesRate; $diskList += $diskMBs; $faultList += $faultMBs
        $fmtList += $cur.fmtPages
        if (-not [double]::IsNaN($procIoMBs)) { $procIoList += $procIoMBs }

        Add-Content -Path $csv -Encoding utf8 -Value ([string]::Format($inv,
            "{0:F2},{1:F1},{2:F1},{3:F2},{4:F1},{5:F2},{6:F1},{7:F0},{8:F3},{9:F2},{10:F1},{11:F2},{12:F1},ok",
            $elapsed, $pagesRate, $readsRate, $faultMBs, $perRead, $diskMBs, $dReadsRate,
            $avgBytes, $queue, $procIoMBs, $procFaults, $wsGb, $cur.fmtPages))

        $prev = $cur; $n++
    }

    # The closing line is what tells a finished CSV from a killed one. Without it the
    # run is treated as aborted and no .result.txt is produced - which is why this
    # sampler is never killed with Stop-Process while it can still be asked to stop.
    Add-Content -Path $csv -Encoding utf8 -Value ("# complete samples={0}" -f $n)
    if (Test-Path $stopFlag) { Remove-Item $stopFlag -Force -ErrorAction SilentlyContinue }

    function Median([double[]]$a) {
        if ($a.Count -eq 0) { return [double]::NaN }
        $s = $a | Sort-Object
        return $s[[math]::Floor($s.Count / 2)]
    }

    $result.Rows            = $n
    $result.MeanIntervalMs  = if ($n -gt 0) { ((Get-Date) - $started).TotalMilliseconds / $n } else { [double]::NaN }
    $result.MedianPages     = Median $pagesList
    $result.MedianDiskMBs   = Median $diskList
    $result.MedianFaultMBs  = Median $faultList
    $result.MedianProcIoMBs = Median $procIoList
    $result.MedianFmtPages  = Median $fmtList

    if ($n -eq 0) {
        $result.Code = 1
        $result.Reason = 'samples=0 - that is a failure, not an empty result'
        return $result
    }

    $violations = @()
    if ($result.MedianPages -gt $GatePages) {
        $violations += ("idle gate: median pages in {0:F1}/s over {1}" -f $result.MedianPages, $GatePages)
    }
    if ($result.MedianDiskMBs -gt $GateDiskMBs) {
        $violations += ("idle gate: median disk read {0:F2} MB/s over {1}" -f $result.MedianDiskMBs, $GateDiskMBs)
    }
    if ($violations.Count -gt 0) {
        $result.Code = 1; $result.Reason = ($violations -join '; ')
    } else {
        $result.Code = 0
    }
    return $result
}

# ---------------------------------------------------------------- selftest

if ($Selftest) {
    $dir = Join-Path $LogDir ("selftest-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Write-Output "=== selftest: three cases, every one of them MUST come back red ==="
    Write-Output "    dir: $dir"
    Write-Output ""
    $failedToFail = @()

    Write-Output "--- case 1: idle gate with -MaxIdlePages -1 (no non-negative rate can meet it) ---"
    $c1 = Invoke-Sampling -ProcId 0 -Dir $dir -Tag 'case1' -Secs 5 -Ms 1000 `
                          -GatePages -1 -GateDiskMBs ([double]::PositiveInfinity) `
                          -ExpectDisable $ExpectDisableValue -Quiet
    Write-Output ("    exit $($c1.Code), rows $($c1.Rows), median pages/s {0:F1}" -f $c1.MedianPages)
    Write-Output ("    reason: " + $(if ($c1.Reason) { $c1.Reason } else { '(none)' }))
    if ($c1.Code -eq 0) { $failedToFail += 'case 1 (idle gate)' ; Write-Output "    VERDICT: FAILED - the gate stayed green" }
    else { Write-Output "    VERDICT: OK - red, as required" }
    Write-Output ""

    Write-Output "--- case 2: -TargetPid on a dead PID (must exit 2 and write NO rows) ---"
    $deadPid = 999999
    while (Get-Process -Id $deadPid -ErrorAction SilentlyContinue) { $deadPid-- }
    $c2 = Invoke-Sampling -ProcId $deadPid -Dir $dir -Tag 'case2' -Secs 5 -Ms 1000 `
                          -GatePages ([double]::PositiveInfinity) -GateDiskMBs ([double]::PositiveInfinity) `
                          -ExpectDisable $ExpectDisableValue -Quiet
    $c2File = Join-Path $dir 'counters-case2.csv'
    $c2Wrote = Test-Path $c2File
    Write-Output ("    exit $($c2.Code), rows $($c2.Rows), csv written: $c2Wrote")
    Write-Output ("    reason: " + $(if ($c2.Reason) { $c2.Reason } else { '(none)' }))
    if ($c2.Code -ne 2 -or $c2Wrote) { $failedToFail += 'case 2 (dead PID)'; Write-Output "    VERDICT: FAILED - it sampled a process that does not exist" }
    else { Write-Output "    VERDICT: OK - refused, as required" }
    Write-Output ""

    Write-Output "--- case 3: -ExpectDisableValue 1 against a registry that reads 0/absent ---"
    $c3 = Invoke-Sampling -ProcId 0 -Dir $dir -Tag 'case3' -Secs 5 -Ms 1000 `
                          -GatePages ([double]::PositiveInfinity) -GateDiskMBs ([double]::PositiveInfinity) `
                          -ExpectDisable 1 -Quiet
    Write-Output ("    exit $($c3.Code), rows $($c3.Rows)")
    Write-Output ("    reason: " + $(if ($c3.Reason) { $c3.Reason } else { '(none)' }))
    if ($c3.Code -ne 2) { $failedToFail += 'case 3 (registry check)'; Write-Output "    VERDICT: FAILED - the registry check has no failing case" }
    else { Write-Output "    VERDICT: OK - red, as required" }
    Write-Output ""

    # NOT "must move". At idle the fault side legitimately reads 0 pages/s - measured
    # 2026-08-02, PagesInputPersec moved by 0 over 3.03 s - so a movement check here
    # would go red on a healthy machine. What IS testable at idle: rows arrive on
    # schedule, none are invalid, and the cumulative raws are alive. Whether the
    # counters move under load is what the stage-2 run against probe-queue-depth
    # answers, and nothing in this selftest may be read as having answered it.
    Write-Output "=== positive side: 6 s baseline must produce rows ON SCHEDULE with live raws ==="
    $p = Invoke-Sampling -ProcId 0 -Dir $dir -Tag 'positive' -Secs 6 -Ms 1000 `
                         -GatePages ([double]::PositiveInfinity) -GateDiskMBs ([double]::PositiveInfinity) `
                         -ExpectDisable $ExpectDisableValue -Quiet
    Write-Output ("    exit $($p.Code), rows $($p.Rows), invalid $($p.Invalid), raws alive: $($p.RawsNonZero)")
    Write-Output ("    mean interval {0:F0} ms (asked for 1000)" -f $p.MeanIntervalMs)
    Write-Output ("    median pages/s {0:F1}  disk {1:F2} MB/s  fault {2:F2} MB/s" -f `
                  $p.MedianPages, $p.MedianDiskMBs, $p.MedianFaultMBs)
    $posFails = @()
    if ($p.Code -ne 0)        { $posFails += "exit $($p.Code)" }
    if ($p.Rows -lt 5)        { $posFails += "only $($p.Rows) rows in 6 s at 1 Hz" }
    if ($p.Invalid -gt 0)     { $posFails += "$($p.Invalid) invalid rows" }
    if (-not $p.RawsNonZero)  { $posFails += "cumulative raws read 0 - WMI is not delivering" }
    if ($p.MeanIntervalMs -gt 1300) { $posFails += ("mean interval {0:F0} ms, over the 1300 ms tolerance" -f $p.MeanIntervalMs) }
    $posOk = ($posFails.Count -eq 0)
    if (-not $posOk) { Write-Output ("    VERDICT: FAILED - " + ($posFails -join '; ')) }
    else { Write-Output "    VERDICT: OK" }
    Write-Output ""

    if ($failedToFail.Count -gt 0) {
        Write-Output "RESULT: FAIL - the negative control passed, so this checker proves nothing."
        Write-Output ("  cases that stayed green: " + ($failedToFail -join ', '))
        exit 1
    }
    if (-not $posOk) { Write-Output "RESULT: FAIL - all three negatives are red but the positive does not run."; exit 1 }
    Write-Output "RESULT: PASS - the three checks go red when they should, and the sampler keeps its schedule."
    Write-Output "  NOT shown by this test: that the counters rise under load. That is the stage-2 run."
    Write-Output "  artefacts: $dir"
    exit 0
}

# ---------------------------------------------------------------- normal run

$tag = if ($Label -ne '') { $Label } else { "pid$TargetPid" }
Write-Output "=== sample-counters: tag $tag, $Seconds s at $IntervalMs ms, pid $TargetPid ==="
$r = Invoke-Sampling -ProcId $TargetPid -Dir $LogDir -Tag $tag -Secs $Seconds -Ms $IntervalMs `
                     -GatePages $MaxIdlePages -GateDiskMBs $MaxIdleDiskMBs -ExpectDisable $ExpectDisableValue `
                     -Formatted:$WithFormatted

if ($r.Code -eq 2) { Write-Output "  exit 2 - nothing sampled."; exit 2 }

Write-Output ""
Write-Output "=== result ==="
Write-Output ("  samples              {0}  ({1} invalid)" -f $r.Rows, $r.Invalid)
Write-Output ("  mean interval        {0:F0} ms  (asked for {1})" -f $r.MeanIntervalMs, $IntervalMs)
Write-Output ("  median pages in      {0:F1} /s" -f $r.MedianPages)
Write-Output ("  median fault read    {0:F2} MB/s" -f $r.MedianFaultMBs)
Write-Output ("  median disk read     {0:F2} MB/s" -f $r.MedianDiskMBs)
Write-Output ("  median proc io read  {0}" -f $(if ([double]::IsNaN($r.MedianProcIoMBs)) { 'not sampled - no pid' } else { "{0:F2} MB/s" -f $r.MedianProcIoMBs }))
Write-Output ("  median formatted     {0}" -f $(if ($WithFormatted) { "{0:F1} /s  (cross check against pages in)" -f $r.MedianFmtPages } else { 'not collected - pass -WithFormatted' }))
Write-Output ("  csv                  {0}" -f $r.Csv)
if ($r.Reason) { Write-Output ("  VERDICT: FAILED - " + $r.Reason) } else { Write-Output "  VERDICT: OK" }
exit $r.Code
