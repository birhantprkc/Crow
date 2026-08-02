<#
run-probes - the correctness probe for Crow #33, at one --n-cpu-moe setting.

Three prompts per operating point:

  a  chat form, France        must contain "Paris"
  b  raw completion, France   must contain "Paris"
  c  NEGATIVE CONTROL, Japan  must contain "Tokyo" and must NOT contain "Paris"

Probe c is why a green a/b means anything. Without it, a criterion of "the output
contains Paris" could be satisfied by a prompt echo, a stuck decoder repeating the
context, or a test harness that greps the wrong file. If c ever says Paris, the
criterion is broken and the run proves nothing.

The reference run (-Ncmoe 999, all experts on CPU) produces the baseline: nobody
publishes a token-exact expected output for this model, and the one quoted upstream
is from a different quant on different hardware, so the baseline has to be made here.
Later runs are held against it character by character.

llama.cpp #25582 is a fault of MIXED placement - experts partly on CUDA, correct at
--n-cpu-moe 999. So the reference must be correct or nothing downstream is readable.

-ctk/-ctv are deliberately left at the f16 default: a quantised KV cache produces
mojibake on its own (llama.cpp #26423) and would contaminate this.

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM as
ANSI, and a stray non-ASCII character breaks the parse in a misleading way.

Usage:
  run-probes.ps1 -Ncmoe 999 -OutDir <dir>                 # make the baseline
  run-probes.ps1 -Ncmoe 42  -OutDir <dir> -Baseline <dir> # compare against it

Exit 0 = every probe behaved as required. Non-zero = read the VERDICT lines.
#>
param(
    [Parameter(Mandatory = $true)][int]$Ncmoe,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [string]$Baseline = '',
    [int]$Predict = 32,
    # Replaces "-ncmoe N" outright. For placements -ncmoe cannot express - e.g.
    # -ot on single tensors of specific blocks. $Ncmoe is then only a label.
    [string]$PlacementArgs = '',
    [string]$Label = '',
    # Probe ids to run, e.g. 'abc' or 'de'. Empty runs all of them.
    [string]$Only = ''
)

$ErrorActionPreference = 'Continue'

$bin   = 'C:\Users\robin\dev\crow-lab\bin'
$model = 'C:\Users\robin\dev\crow-lab\models\DeepSeek-V4-Flash-MXFP4.gguf'

$probes = @(
    @{ id = 'a'; file = 'probe-a-chat.txt';       want = 'Paris'; forbid = '';      kind = 'positive' },
    @{ id = 'b'; file = 'probe-b-completion.txt'; want = 'Paris'; forbid = '';      kind = 'positive' },
    @{ id = 'c'; file = 'probe-c-negative.txt';   want = 'Tokyo'; forbid = 'Paris'; kind = 'NEGATIVE CONTROL' },
    # d and e carry ~2000 tokens of neutral filler before the same question. Purpose:
    # a 14-token prompt is one MMQ call at the smallest tile. mul_mat_id dispatches by
    # token count (ggml-cuda.cu:1868-1907) and for MXFP4 on sm_120 the MMVQ ceiling is
    # 7 tokens (mmvq.cu:141-152), so short prompts only ever exercise the corner.
    # Cybertiron ran -ub 512. These fill the tile. The filler contains neither answer.
    @{ id = 'd'; file = 'probe-d-long-france.txt'; want = 'Paris'; forbid = '';      kind = 'positive' },
    @{ id = 'e'; file = 'probe-e-long-japan.txt';  want = 'Tokyo'; forbid = 'Paris'; kind = 'NEGATIVE CONTROL' }
)
if ($Only -ne '') { $probes = @($probes | Where-Object { $Only.Contains($_.id) }) }
if ($probes.Count -eq 0) { Write-Output "SETUP ERROR: -Only '$Only' selected no probes"; exit 2 }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
foreach ($p in @("$bin\llama-completion.exe", $model)) {
    if (-not (Test-Path $p)) { Write-Output "SETUP ERROR: not found: $p"; exit 2 }
}

$placement = if ($PlacementArgs -ne '') { $PlacementArgs } else { "-ncmoe $Ncmoe" }
$tag = if ($Label -ne '') { $Label } else { "ncmoe$Ncmoe" }

Write-Output "model    $model"
Write-Output "place    $placement"
Write-Output "args     -n $Predict --temp 0 --seed 1234 -c 4096 -np 1 -ngl 99"
Write-Output "KV cache left at the f16 default on purpose (see header)"
Write-Output ""

$fail = 0
foreach ($probe in $probes) {
    $promptFile = Join-Path $PSScriptRoot "prompts\$($probe.file)"
    if (-not (Test-Path $promptFile)) { Write-Output "SETUP ERROR: no prompt $promptFile"; exit 2 }

    $outFile = Join-Path $OutDir "$tag-probe$($probe.id).txt"
    $rawFile = Join-Path $OutDir "$tag-probe$($probe.id).raw.txt"

    $cliArgs = "-m `"$model`" -f `"$promptFile`" -no-cnv --no-display-prompt " +
               "--temp 0 --seed 1234 -n $Predict -c 4096 -np 1 --no-warmup -ngl 99 $placement"
    $started = Get-Date
    cmd.exe /c "`"$bin\llama-completion.exe`" $cliArgs > `"$outFile`" 2> `"$rawFile`""
    $code = $LASTEXITCODE
    $secs = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)

    $text = ''
    if (Test-Path $outFile) { $text = (Get-Content $outFile -Raw -Encoding UTF8) }
    if ($null -eq $text) { $text = '' }
    $shown = ($text -replace "`r", '' -replace "`n", '\n')

    Write-Output "--- probe $($probe.id)  [$($probe.kind)]  $($probe.file)  exit $code  ${secs}s"
    Write-Output "    output: $shown"

    if ($code -ne 0) {
        $fail = 1
        Write-Output "    VERDICT: FAILED - llama-completion exited $code; see $rawFile"
        continue
    }

    $hasWant = $text -match [regex]::Escape($probe.want)
    $ok = $hasWant
    $why = "contains '$($probe.want)': $hasWant"
    if ($probe.forbid -ne '') {
        $hasForbid = $text -match [regex]::Escape($probe.forbid)
        $ok = $hasWant -and (-not $hasForbid)
        $why += ", contains '$($probe.forbid)': $hasForbid (must be False)"
    }
    Write-Output "    criterion: $why"

    if ($ok) { Write-Output "    VERDICT: OK" }
    else {
        $fail = 1
        if ($probe.kind -eq 'NEGATIVE CONTROL') {
            Write-Output "    VERDICT: FAILED - the negative control did not behave. The criterion cannot tell a real answer from an artefact, so probes a and b prove nothing this run."
        } else {
            Write-Output "    VERDICT: FAILED - expected answer absent"
        }
    }

    if ($Baseline -ne '') {
        $found = Get-ChildItem -Path $Baseline -Filter "*-probe$($probe.id).txt" -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -notlike '*.raw.txt' } | Select-Object -First 1
        $baseFile = if ($found) { $found.FullName } else { '' }
        if ($baseFile -ne '' -and (Test-Path $baseFile)) {
            $baseText = (Get-Content $baseFile -Raw -Encoding UTF8)
            if ($null -eq $baseText) { $baseText = '' }
            if ($baseText -ceq $text) {
                Write-Output "    vs baseline: identical ($($text.Length) chars)"
            } else {
                $n = [math]::Min($baseText.Length, $text.Length)
                $i = 0
                while ($i -lt $n -and $baseText[$i] -ceq $text[$i]) { $i++ }
                Write-Output "    vs baseline: DIVERGES at character $i of $($baseText.Length)"
                $ctx = [math]::Max(0, $i - 20)
                Write-Output "      baseline: ...$($baseText.Substring($ctx, [math]::Min(50, $baseText.Length - $ctx)))"
                Write-Output "      this run: ...$($text.Substring($ctx, [math]::Min(50, $text.Length - $ctx)))"
            }
        } else {
            Write-Output "    vs baseline: no baseline file at $baseFile"
        }
    }
    Write-Output ""
}

Write-Output ""
if ($fail -eq 0) { Write-Output "RESULT: PASS - both positive probes answered and the negative control held" }
else { Write-Output "RESULT: FAIL - see the VERDICT lines above" }
exit $fail
