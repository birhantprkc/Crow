<#
measure-loadmode - does bypassing the page cache change the thrashing regime?

The model is 147 GiB of mapping against 63.4 GB of RAM. A prefill ubatch touches
nearly the whole expert set, so each pass evicts what the last one faulted in.
Measured during a 1,659-token prefill under the default mmap mode:

    RAM 99.5 % in use, 0.3 GB free, process working set 52.7 GB, CPU 1.8 % of 24 cores

1.8 % CPU means the process was waiting, not computing. --load-mode dio bypasses
the page cache entirely, which in a regime where the cache is useless might remove
the eviction storm rather than the reads.

llama.cpp master has exactly one NVMe-near switch and this is it (common/arg.cpp);
--no-mmap and --mlock are deprecated in its favour. No published report measures it
on this model. See Crow #38.

WHAT MAKES THE RESULT READABLE is not the wall time on its own but the pair of it
with RAM and CPU. If free RAM stays high and CPU rises above 1.8 %, the mode
changed the regime. If the picture is unchanged, the drive is the ceiling and page
replacement was never the problem.

The negative control runs first and costs nothing: an invalid mode must be
rejected. Without it, a silently ignored flag would produce a second measurement of
the default and look like a null result.

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM as
ANSI, and a stray non-ASCII character breaks the parse in a misleading way.

Usage:  measure-loadmode.ps1 -LoadMode dio -LogDir <path> [-Ncmoe 999] [-Prompt probe-d-long-france.txt]
Exit 0 = the negative control was rejected and the run completed.
#>
param(
    [string]$LoadMode = 'dio',
    [Parameter(Mandatory = $true)][string]$LogDir,
    [int]$Ncmoe = 999,
    [string]$Prompt = 'probe-d-long-france.txt',
    [int]$Predict = 32,
    # Point at part 1 of a split GGUF; llama.cpp finds the rest.
    [string]$Model = 'C:\Users\robin\dev\crow-lab\models\DeepSeek-V4-Flash-MXFP4.gguf',
    # Names the log files. Without it two runs of the same load mode overwrite each
    # other. Same parameter name as run-probes.ps1 and measure-vram.ps1 on purpose:
    # these three answer nearly the same question and their switches should not
    # diverge, or callers trip over which one takes what.
    [string]$Label = ''
)

$ErrorActionPreference = 'Continue'

$bin    = 'C:\Users\robin\dev\crow-lab\bin\llama-completion.exe'
$model  = $Model
$prompt = Join-Path $PSScriptRoot "prompts\$Prompt"

foreach ($p in @($bin, $model, $prompt)) {
    if (-not (Test-Path $p)) { Write-Output "SETUP ERROR: not found: $p"; exit 2 }
}
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors

Write-Output "=== negative control: --load-mode not-a-mode (must be rejected) ==="
cmd.exe /c "`"$bin`" -m `"$model`" --load-mode not-a-mode -n 1 > nul 2>&1"
$negCode = $LASTEXITCODE
if ($negCode -eq 0) {
    Write-Output "  VERDICT: FAILED - an invalid mode was accepted, so --load-mode may be ignored."
    Write-Output "  Any measurement below would be a second run of the default. Stopping."
    exit 1
}
Write-Output "  VERDICT: OK - rejected, exit $negCode. The flag is parsed."
Write-Output ""

$tag     = if ($Label -ne '') { $Label } else { $LoadMode }
$outFile = Join-Path $LogDir "loadmode-$tag.out.txt"
$errFile = Join-Path $LogDir "loadmode-$tag.log.txt"
$cliArgs = @(
    '-m', $model, '-f', $prompt, '-no-cnv', '--no-display-prompt',
    '--temp', '0', '--seed', '1234', '-n', "$Predict", '-c', '4096', '-np', '1',
    '--no-warmup', '-ngl', '99', '-ncmoe', "$Ncmoe", '--load-mode', $LoadMode
)

Write-Output "=== run: --load-mode $LoadMode  -ncmoe $Ncmoe  prompt $Prompt ==="
$osB = Get-CimInstance Win32_OperatingSystem
$ramTotal = [math]::Round($osB.TotalVisibleMemorySize/1MB,1)
Write-Output "  RAM before: $([math]::Round($osB.FreePhysicalMemory/1MB,1)) GB free of $ramTotal GB"
Write-Output ""
$commitLimit = [math]::Round($osB.TotalVirtualMemorySize/1MB,1)
Write-Output "  commit limit: $commitLimit GB (RAM plus pagefile)"
Write-Output ""
Write-Output "   t[s]   RAM frei GB   WorkingSet GB   Commit GB   CPU % of $cores cores"

$started = Get-Date
$proc = Start-Process -FilePath $bin -ArgumentList $cliArgs `
    -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
    -PassThru -WindowStyle Hidden
# Touch .Handle once while the process is still alive. This is the fix, and it is the
# cause rather than the second guess: .NET only caches the process handle if something
# asks for it. Without that cache the OS handle is released when the process exits and
# ExitCode has nothing left to read - WaitForExit() below does not help, because by then
# there is nothing to wait on a handle for. Measured 2026-08-02: a successful 30.3 s run
# (14 tokens prefilled, "Paris" generated, content check green) still reported exit -1
# with WaitForExit() alone in place.
$null = $proc.Handle

$lastCpu = 0.0
$lastT   = $started
$minFree = 9999.0
$maxWs   = 0.0
$cpuRows = @()

while (-not $proc.HasExited) {
    Start-Sleep -Seconds 10
    try { $proc.Refresh() } catch { break }
    if ($proc.HasExited) { break }
    $now  = Get-Date
    $secs = ($now - $lastT).TotalSeconds
    $cpu  = $proc.CPU
    $pct  = if ($secs -gt 0) { [math]::Round((($cpu - $lastCpu)/$secs/$cores)*100,1) } else { 0 }
    $lastCpu = $cpu; $lastT = $now
    $osN  = Get-CimInstance Win32_OperatingSystem
    $free = [math]::Round($osN.FreePhysicalMemory/1MB,1)
    $commit = [math]::Round(($osN.TotalVirtualMemorySize - $osN.FreeVirtualMemory)/1MB,1)
    $ws   = [math]::Round($proc.WorkingSet64/1GB,1)
    if ($free -lt $minFree) { $minFree = $free }
    if ($ws   -gt $maxWs)   { $maxWs   = $ws }
    $cpuRows += $pct
    Write-Output ("{0,7}   {1,11}   {2,13}   {3,9}   {4,10}" -f [math]::Round(($now-$started).TotalSeconds,0), $free, $ws, $commit, $pct)
    # Warn only. This script never stops the run on its own - that decision is robin's,
    # and a measurement killed without asking costs the number it was there to produce.
    if ($commit -gt ($commitLimit * 0.97)) {
        Write-Output "         ^^ WARNUNG: Commit $commit GB von $commitLimit GB. Sag Bescheid, ob abgebrochen werden soll."
    }
}

$wall = [math]::Round(((Get-Date) - $started).TotalSeconds,1)
# WaitForExit() makes sure the process is really finished before ExitCode is read. It is
# NOT what makes ExitCode readable - that is the .Handle touch right after Start-Process.
# Keep both: the handle so the value exists, this so it is final.
$proc.WaitForExit()
$code = $proc.ExitCode
if ($null -eq $code) { $code = -1; Write-Output "  (exit code unavailable, reported as -1)" }

$meanCpu = if ($cpuRows.Count -gt 0) { [math]::Round(($cpuRows | Measure-Object -Average).Average,1) } else { 0 }

Write-Output ""
Write-Output "=== result ==="
Write-Output "  exit                 $code"
Write-Output "  wall                 $wall s"
Write-Output "  peak working set     $maxWs GB"
Write-Output "  lowest free RAM      $minFree GB  of $ramTotal GB"
Write-Output "  mean CPU             $meanCpu %  of $cores cores"

$perf = Select-String -Path $errFile -Pattern 'prompt eval time|eval time|load time' -ErrorAction SilentlyContinue
if ($perf) { Write-Output ""; $perf | ForEach-Object { "  " + ($_.Line -replace '^[0-9.]+ I ','').Trim() } }

$text = ''
if (Test-Path $outFile) { $text = (Get-Content $outFile -Raw -Encoding UTF8) }
if ($null -eq $text) { $text = '' }
Write-Output ""
Write-Output ("  generated: " + ($text -replace "`r",'' -replace "`n",'\n'))
Write-Output ("  contains 'Paris': " + ($text -match 'Paris'))

Write-Output ""
Write-Output "  logs: $outFile"
Write-Output "        $errFile"
exit $(if ($code -eq 0) { 0 } else { 1 })
