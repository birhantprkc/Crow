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
    [string]$Model = $null,
    # Names the log files. Without it two runs of the same load mode overwrite each
    # other. Same parameter name as run-probes.ps1 and measure-vram.ps1 on purpose:
    # these three answer nearly the same question and their switches should not
    # diverge, or callers trip over which one takes what.
    [string]$Label = '',
    # Pass -v to llama. Off by default because it makes the log ~550 KB, on when the
    # run has to PROVE where the tensors went. Without it the log carries no
    # load_tensors, no buffer size and no "overridden to" line, so any statement about
    # placement is arithmetic on the -ncmoe value, not a measurement. A curator found
    # exactly that hole in the B1 series of 2026-08-02.
    [switch]$LlamaVerbose,
    # Run tools\sample-counters.ps1 alongside this run. It is a SEPARATE tool and not
    # a mode of this one, for two reasons that do not go away: the idle baseline is a
    # sampler run with no process to attach to, and the loop below deliberately ticks
    # at 10 s while the counters need 1 Hz. Changing this loop's interval would make
    # every run incomparable with the B1 series already in the archive.
    # run-probes.ps1 and measure-vram.ps1 do NOT get this switch: neither holds a
    # process object (both call llama through cmd.exe and never see a pid), so there
    # is nothing for the sampler to attach to there.
    [switch]$Sample,
    # Idle thresholds handed to the sampler's gate below. Chosen, not measured: one
    # idle reading on 2026-08-02 gave 16.78 pages/s and 0.08 MB/s, rounded up roughly
    # tenfold. The 20 s baselines themselves produce the distribution that replaces them.
    [double]$MaxIdlePages = 200,
    [double]$MaxIdleDiskMBs = 10,
    # Hard ceiling for the sampler, not its normal end. It normally stops because this
    # script drops a stop file, or because the llama process is gone.
    [int]$SampleMaxSeconds = 1800
)

. "$PSScriptRoot\model-paths.ps1"
if (-not $Model) { $Model = Get-ModelPath 'mxfp4' }

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
if ($LlamaVerbose) { $cliArgs += '-v' }

# THE IDLE GATE, and it has to sit HERE - above Start-Process, not below it. A run
# that begins with foreign load in the background cannot be told apart afterwards
# from one where llama caused the traffic, because the memory counters have no
# process dimension at all. The gate costs 20 s and saves an 11 minute run that
# would have produced an unusable number.
# Placing it after the process starts would make it useless AND make its own check
# untestable: a failure there could never be observed without a llama log next to it.
$sampler = $null
if ($Sample) {
    $samplerTool = Join-Path $PSScriptRoot 'sample-counters.ps1'
    if (-not (Test-Path $samplerTool)) { Write-Output "SETUP ERROR: not found: $samplerTool"; exit 2 }
    Write-Output "=== idle gate: 20 s baseline, must stay under $MaxIdlePages pages/s and $MaxIdleDiskMBs MB/s ==="
    & $samplerTool -LogDir $LogDir -Label "$tag-idle" -Seconds 20 `
                   -MaxIdlePages $MaxIdlePages -MaxIdleDiskMBs $MaxIdleDiskMBs
    $gateCode = $LASTEXITCODE
    if ($gateCode -ne 0) {
        Write-Output "  VERDICT: FAILED - the machine is not quiet, exit $gateCode."
        Write-Output "  llama was NOT started. A measurement taken under foreign load cannot be"
        Write-Output "  separated from it afterwards, so there is nothing to salvage by running anyway."
        exit 2
    }
    Write-Output "  VERDICT: OK - quiet enough to attribute the traffic to this run."
    Write-Output ""
}

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

# The sampler attaches AFTER the handle grab, because before it there is no pid to
# attach to. It is started with Start-Process and not with & so this script can go on
# to its own 10 s loop; the two run side by side at different intervals on purpose.
$samplerCsv = ''
if ($Sample) {
    $samplerCsv = Join-Path $LogDir "counters-$tag.csv"
    $sampler = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'sample-counters.ps1'),
                      '-LogDir', $LogDir, '-Label', $tag, '-TargetPid', $proc.Id,
                      '-Seconds', $SampleMaxSeconds `
        -RedirectStandardOutput (Join-Path $LogDir "counters-$tag.out.txt") `
        -PassThru -WindowStyle Hidden
    $null = $sampler.Handle
    Write-Output "  sampler pid $($sampler.Id) -> $samplerCsv"
}

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

# Ask the sampler to stop; do NOT kill it. measure-vram.ps1:89-90 kills nvidia-smi
# with Stop-Process -Force, and copying that here would break three promises at once:
# the sampler could never write its closing "# complete samples=<n>" line, so EVERY
# csv would count as aborted; its exit value would carry no meaning; and the check
# that a counter object was actually queried rests on exactly that closing line.
# Force stays as a last resort after 30 s, and when it fires it is written down.
$samplerNote = 'not sampled'
if ($Sample -and $null -ne $sampler) {
    $flag = Join-Path $LogDir "stop-$tag.flag"
    Set-Content -Path $flag -Value 'stop' -Encoding utf8
    if (-not $sampler.WaitForExit(30000)) {
        Write-Output "  WARNUNG: sampler did not stop within 30 s, forcing it. The csv is incomplete."
        try { Stop-Process -Id $sampler.Id -Force -ErrorAction Stop } catch {}
        $samplerNote = 'forced - csv incomplete'
    } else {
        $samplerNote = "exit $($sampler.ExitCode)"
    }
    if (Test-Path $flag) { Remove-Item $flag -Force -ErrorAction SilentlyContinue }
}

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

# The result block goes to a FILE, not only to the console. Until 2026-08-02 these four
# numbers existed nowhere on disk: the console scrolled away and the run was gone. Three
# of the five columns in the B1 series had no artefact for exactly this reason, which
# makes them assertions rather than measurements. key=value so a later run can be diffed
# against this one without re-reading prose.
$resultFile = Join-Path $LogDir "loadmode-$tag.result.txt"
$hostLines = @(Select-String -Path $errFile -Pattern 'buffer type overridden to CUDA_Host' -ErrorAction SilentlyContinue).Count
$gpuLines  = @(Select-String -Path $errFile -Pattern 'buffer type overridden to CUDA0' -ErrorAction SilentlyContinue).Count
$fitWarn   = @(Select-String -Path $errFile -Pattern 'failed to fit params to free device memory' -ErrorAction SilentlyContinue).Count
$result = @(
    "tag=$tag"
    "load_mode=$LoadMode"
    "ncmoe=$Ncmoe"
    "model=$model"
    "prompt=$Prompt"
    "predict=$Predict"
    "verbose=$([bool]$LlamaVerbose)"
    "exit=$code"
    "wall_s=$wall"
    "peak_working_set_gb=$maxWs"
    "lowest_free_ram_gb=$(if ($cpuRows.Count -eq 0) { 'not sampled - run ended before the first 10 s tick' } else { $minFree })"
    "ram_total_gb=$ramTotal"
    "mean_cpu_pct=$meanCpu"
    "samples=$($cpuRows.Count)"
    # Placement, measured rather than derived from the -ncmoe value - but only if -v ran.
    "overridden_to_cuda_host=$(if ($LlamaVerbose) { $hostLines } else { 'unknown - rerun with -LlamaVerbose' })"
    "overridden_to_cuda0=$(if ($LlamaVerbose) { $gpuLines } else { 'unknown - rerun with -LlamaVerbose' })"
    # llama.cpp's own verdict on whether the placement fits free VRAM. Its absence is
    # as much a finding as its presence: it appeared at -ncmoe 29 and 27 on 2026-08-02
    # and not at 34 or 31, which is where the VRAM boundary sits.
    "fit_warning=$fitWarn"
    "contains_paris=$($text -match 'Paris')"
    # Counter capture. A csv without the closing "# complete samples=" line is an
    # aborted run and its numbers do not count - that is why the line is recorded
    # here rather than just the file name.
    "counters_csv=$(if ($Sample) { $samplerCsv } else { 'not sampled - pass -Sample' })"
    "counters_sampler=$samplerNote"
    "counters_complete=$(if ($Sample -and (Test-Path $samplerCsv)) { [bool]((Get-Content $samplerCsv -Tail 1) -match '^# complete samples=') } else { 'n/a' })"
)
$result += @(Select-String -Path $errFile -Pattern 'prompt eval time|eval time|load time' -ErrorAction SilentlyContinue |
             ForEach-Object { "perf: " + ($_.Line -replace '^[0-9.]+ I ','').Trim() })
$result | Set-Content -Path $resultFile -Encoding UTF8

Write-Output ""
Write-Output "  logs:   $outFile"
Write-Output "          $errFile"
Write-Output "  result: $resultFile"
exit $(if ($code -eq 0) { 0 } else { 1 })
