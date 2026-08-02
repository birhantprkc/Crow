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
overhead is what decides Crow's 12 GB operating point, so it is the number this
script exists to produce.

NOTE: this file is deliberately ASCII-only. Windows PowerShell 5.1 reads a .ps1
without a BOM as ANSI, and a stray em dash breaks the parse in a way that looks
like a missing brace.

Usage:  measure-vram.ps1 [-Ncmoe 36] [-LogDir <path>]
Exit 0 = the load succeeded and a peak was captured.
#>
param(
    [int]$Ncmoe = 36,
    [string]$LogDir = $env:TEMP
)

$ErrorActionPreference = 'Continue'

$bin    = 'C:\Users\robin\dev\crow-lab\bin'
$model  = 'C:\Users\robin\dev\crow-lab\models\DeepSeek-V4-Flash-MXFP4.gguf'
$prompt = Join-Path $PSScriptRoot 'prompts\probe-a-chat.txt'

foreach ($p in @("$bin\llama-completion.exe", $model, $prompt)) {
    if (-not (Test-Path $p)) { Write-Output "SETUP ERROR: not found: $p"; exit 2 }
}

$smiLog   = Join-Path $LogDir "vram-smi-ncmoe$Ncmoe.txt"
$llamaLog = Join-Path $LogDir "vram-llama-ncmoe$Ncmoe.txt"

$baseline = [int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | Select-Object -First 1)
$total    = [int](nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1)
Write-Output "baseline VRAM in use   $baseline MiB of $total MiB (desktop and whatever else is running)"
Write-Output "sampling nvidia-smi once a second while the model loads"

$smi = Start-Process -FilePath 'nvidia-smi' `
    -ArgumentList '--query-gpu=memory.used --format=csv,noheader,nounits -l 1' `
    -RedirectStandardOutput $smiLog -PassThru -WindowStyle Hidden

# llama.cpp logs to stderr; routing it through cmd keeps PowerShell from wrapping
# each line in an ErrorRecord and reporting a failure the program did not have.
$cliArgs = "-m `"$model`" -f `"$prompt`" -no-cnv --temp 0 --seed 1234 -n 1 -c 4096 -np 1 --no-warmup -ngl 99 -ncmoe $Ncmoe -v"
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
Write-Output "generated output:"
Select-String -Path $llamaLog -Pattern 'Answer briefly' -ErrorAction SilentlyContinue |
    ForEach-Object { "  " + $_.Line.Trim() } | Select-Object -First 3

Write-Output ""
Write-Output "logs: $smiLog"
Write-Output "      $llamaLog"
exit $(if ($code -eq 0) { 0 } else { 1 })
