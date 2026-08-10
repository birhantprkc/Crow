<#
measure-vram - what does llama.cpp actually put in VRAM at a given --n-cpu-moe?

gguf_header.py --budget-gib predicts from the tensor table, counting weights only.
This measures the same load two independent ways at once:

  1. llama.cpp's own buffer accounting, at -v verbosity
  2. nvidia-smi sampled once a second across the whole load

Two numbers for one load. If they disagree, that gap is itself the finding: one of
them is not measuring what its name suggests. See Crow #33.

The difference between either measurement and the prediction is the overhead the
prediction deliberately omits - KV cache, compute buffers, CUDA context. That
overhead is what decides Crow's operating point, so it is the number this script
exists to produce. (It used to say "12 GB operating point" here. That target was
dropped by robin on 2026-08-02 - spending more VRAM measured 1.7x SLOWER - and no
number has replaced it yet, deliberately. See #25.)

NEGATIVE CONTROL, and why it is not the obvious one: a -ot rule that matches nothing
is accepted in silence, so a run can look like a clean null result while measuring the
default. The tempting check - "did any tensor get overridden?" - proves nothing,
because --n-cpu-moe is itself implemented as an override and produces hundreds of such
lines on its own. What separates them is the DESTINATION: -ncmoe sends experts to
CUDA_Host, while a "...=CUDA0" rule must produce at least one "overridden to CUDA0".
So every destination named in -PlacementArgs has to appear in the log, or the run is
declared void here rather than quietly written down.

NOTE: this file is deliberately ASCII-only. Windows PowerShell 5.1 reads a .ps1
without a BOM as ANSI, and a stray em dash breaks the parse in a way that looks
like a missing brace.

Usage:  measure-vram.ps1 [-Ncmoe 36] [-LogDir <path>] [-Model <path to part 1>]
Exit 0 = the load succeeded, a peak was captured, and every placement took effect.
#>
param(
    [int]$Ncmoe = 36,
    [string]$LogDir = $env:TEMP,
    # Replaces "-ncmoe N" outright, for placements -ncmoe cannot express.
    # A placement rule that matches nothing fails silently - see the negative control
    # in the header. Checked below, not assumed.
    [string]$PlacementArgs = '',
    # Point at part 1 of a split GGUF; llama.cpp finds the rest. Same switch name as
    # run-probes.ps1 and measure-loadmode.ps1 on purpose. Until 2026-08-02 this path
    # was hard-coded to MXFP4, which meant the one quant that actually fits this
    # machine - UD-IQ1_S at 76.87 GiB - could not be measured by this tool at all.
    [string]$Model = $null,
    [string]$Label = ''
)

. "$PSScriptRoot\model-paths.ps1"
if (-not $Model) { $Model = Get-ModelPath 'mxfp4' }

$ErrorActionPreference = 'Continue'

$bin    = 'C:\Users\robin\dev\crow-lab\bin'
$model  = $Model
$prompt = Join-Path $PSScriptRoot 'prompts\probe-a-chat.txt'

foreach ($p in @("$bin\llama-completion.exe", $model, $prompt)) {
    if (-not (Test-Path $p)) { Write-Output "SETUP ERROR: not found: $p"; exit 2 }
}
# Same as measure-loadmode.ps1 does. Without it a missing -LogDir surfaces as
# "the system cannot find the path" from cmd.exe after the sampler has already
# started, which reads like a broken measurement rather than a missing folder.
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$placement = if ($PlacementArgs -ne '') { $PlacementArgs } else { "-ncmoe $Ncmoe" }
$tag       = if ($Label -ne '') { $Label } else { "ncmoe$Ncmoe" }
Write-Output "placement  $placement"

$smiLog   = Join-Path $LogDir "vram-smi-$tag.txt"
$llamaLog = Join-Path $LogDir "vram-llama-$tag.txt"

$baseline = [int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | Select-Object -First 1)
$total    = [int](nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1)
Write-Output "baseline VRAM in use   $baseline MiB of $total MiB (desktop and whatever else is running)"
Write-Output "sampling nvidia-smi once a second while the model loads"

$smi = Start-Process -FilePath 'nvidia-smi' `
    -ArgumentList '--query-gpu=memory.used --format=csv,noheader,nounits -l 1' `
    -RedirectStandardOutput $smiLog -PassThru -WindowStyle Hidden

# llama.cpp logs to stderr; routing it through cmd keeps PowerShell from wrapping
# each line in an ErrorRecord and reporting a failure the program did not have.
$cliArgs = "-m `"$model`" -f `"$prompt`" -no-cnv --temp 0 --seed 1234 -n 1 -c 4096 -np 1 --no-warmup -ngl 99 $placement -v"
$started = Get-Date
cmd.exe /c "`"$bin\llama-completion.exe`" $cliArgs > `"$llamaLog`" 2>&1"
$code = $LASTEXITCODE
$elapsed = ((Get-Date) - $started).TotalSeconds

Start-Sleep -Milliseconds 1200   # let the sampler catch the tail of the load
if (-not $smi.HasExited) { Stop-Process -Id $smi.Id -Force }

Write-Output ""
Write-Output "llama-completion exit  $code   wall $([math]::Round($elapsed,1)) s"

$samples = @(Get-Content $smiLog -ErrorAction SilentlyContinue |
             ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ })
if ($samples.Count -eq 0) {
    Write-Output "MEASUREMENT FAILED: nvidia-smi produced no samples"
    exit 1
}
$peak = ($samples | Measure-Object -Maximum).Maximum

Write-Output ""
Write-Output "=== 1. nvidia-smi, external ==="
Write-Output "  samples                   $($samples.Count) over $([math]::Round($elapsed,1)) s"
Write-Output "  peak VRAM in use          $peak MiB"
Write-Output "  attributable to this run  $($peak - $baseline) MiB  ($([math]::Round(($peak - $baseline)/1024.0,2)) GiB)"
Write-Output "  headroom left             $($total - $peak) MiB"

Write-Output ""
Write-Output "=== 2. llama.cpp's own accounting ==="
$acct = Select-String -Path $llamaLog -Pattern 'buffer size|buffer type|offload|KV self|compute buffer|model size|n_ctx =' -ErrorAction SilentlyContinue
if ($acct) { $acct | ForEach-Object { "  " + ($_.Line.Trim()) } | Select-Object -First 40 }
else { Write-Output "  nothing matched - this build does not print buffer sizes even at -v" }

Write-Output ""
Write-Output "=== 3. did the placement take effect? ==="
$placementOk = $true
if ($PlacementArgs -match '-ot\s') {
    # Every "=DEST" named in the rule must show up as "overridden to DEST". Counting
    # overrides alone is not enough: -ncmoe produces them too, just to CUDA_Host.
    $dests = [regex]::Matches($PlacementArgs, '=([A-Za-z0-9_]+)') |
             ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique
    if ($dests.Count -eq 0) {
        Write-Output "  FAILED: -ot given but no '=DESTINATION' found in it. Cannot verify."
        $placementOk = $false
    }
    foreach ($d in $dests) {
        $hits = @(Select-String -Path $llamaLog -Pattern "buffer type overridden to $d" -ErrorAction SilentlyContinue).Count
        if ($hits -gt 0) {
            Write-Output "  ok      $hits tensors overridden to $d"
        } else {
            Write-Output "  FAILED  0 tensors overridden to $d - the rule matched nothing"
            $placementOk = $false
        }
    }
    if (-not $placementOk) {
        Write-Output ""
        Write-Output "  A rule that matches nothing is accepted in silence, so this run measured"
        Write-Output "  the default placement, not the one asked for. Recording it would put a"
        Write-Output "  wrong label on a real number. Declared void."
    }
} else {
    $hostHits = @(Select-String -Path $llamaLog -Pattern 'buffer type overridden to CUDA_Host' -ErrorAction SilentlyContinue).Count
    Write-Output "  -ncmoe $Ncmoe sent $hostHits tensors to CUDA_Host (informational; -ncmoe cannot silently miss)"
}

Write-Output ""
Write-Output "generated output:"
Select-String -Path $llamaLog -Pattern 'Answer briefly' -ErrorAction SilentlyContinue |
    ForEach-Object { "  " + $_.Line.Trim() } | Select-Object -First 3

Write-Output ""
Write-Output "logs: $smiLog"
Write-Output "      $llamaLog"
exit $(if ($code -eq 0 -and $placementOk) { 0 } else { 1 })
