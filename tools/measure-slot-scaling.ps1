<#
Measures whether concurrent llama-server slots raise AGGREGATE throughput on the
expert-streaming path.

THE QUESTION
The expert-cache manager hangs off the model, not the context (llama-model.cpp:1462), so all
slots of one server share one cache and one mutex (llama-moe-stream.h:165). The server decodes
every slot in a single llama_decode, so higher -np should mean more batch depth per remap call
and one expert load serving several sequences. That is the arithmetic in issue #1. This script
is what turns it from a reading of the code into a number.

WHY AGGREGATE AND NOT PER-REQUEST
Per-request tokens/s must FALL as slots are added - the same decode is shared. The claim under
test is that the SUM rises. Reporting per-request speed would make a working batch look broken.

WHY DIFFERENT PROMPTS
Identical prompts across slots would route to identical experts, which is the most favourable
case the cache can see and not the one an agent platform produces. Four distinct prompts are
the realistic case. The consequence is that this measures the harder side, not the flattering
one.

THE FAILURE CASE THIS MUST BE ABLE TO SHOW
If the streamer is the bottleneck rather than the batch, aggregate throughput stays flat or
falls as N rises. That outcome is reachable with this script and is reported as-is. A script
that can only produce "it scales" would measure nothing.

WARM-UP AND SPREAD
Every N is run -Repeats times. Run 1 is discarded as warm-up: it pays cold expert loads the
following ones do not, and mixing it in would credit the batch for a cache effect. The rest are
reported as MEDIAN plus min/max and the spread between them.

Reporting a single run per N was the first version of this script and it was wrong. Measured
2026-08-04 with 2 repeats: the two N=1 runs differed by 31 percent, which is more than the whole
gain claimed at N=2. A point estimate cannot say whether a step survived - only the spread can,
which is why it is now printed next to every figure rather than left to the reader.

Usage:
  measure-slot-scaling.ps1                          # N = 1,2,4 against port 8081
  measure-slot-scaling.ps1 -Concurrency 1,2 -Tokens 64
#>
param(
    [int]    $Port        = 8081,
    [int[]]  $Concurrency = @(1,2,4),
    [int]    $Tokens      = 128,
    [int]    $Repeats     = 2,
    [int]    $TimeoutSec  = 600
)

$ErrorActionPreference = 'Stop'

# Four distinct prompts. Index i is handed to slot i, so no two concurrent requests share one.
$Prompts = @(
    'Write a Python function that reverses a linked list. Code only.',
    'Explain in three sentences why B-trees suit disk storage.',
    'Write a SQL query that finds the second highest salary per department.',
    'Describe the difference between a mutex and a semaphore in three sentences.'
)

Add-Type -AssemblyName System.Net.Http

$client = New-Object System.Net.Http.HttpClient
$client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
$uri = "http://127.0.0.1:$Port/v1/chat/completions"

function Invoke-One {
    param([string]$Prompt)
    $payload = @{
        model       = 'crow'
        messages    = @(@{ role = 'user'; content = $Prompt })
        max_tokens  = $Tokens
        temperature = 0
        seed        = 1234
    } | ConvertTo-Json -Depth 5
    $content = New-Object System.Net.Http.StringContent($payload, [Text.Encoding]::UTF8, 'application/json')
    return $client.PostAsync($uri, $content)
}

# Reachability first. Without this a refused connection looks like zero throughput.
try {
    $probe = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 15
    Write-Output "health: $($probe.status)  port $Port"
} catch {
    Write-Output "UNREACHABLE on port $Port - $($_.Exception.Message)"
    exit 1
}
Write-Output "tokens/request = $Tokens, repeats = $Repeats (run 1 discarded as warm-up), distinct prompts"
Write-Output ""

$results = @()

function Get-Median {
    param([double[]]$Values)
    $s = $Values | Sort-Object
    $c = $s.Count
    if ($c -eq 0) { return 0 }
    if ($c % 2 -eq 1) { return $s[[math]::Floor($c / 2)] }
    return ($s[$c / 2 - 1] + $s[$c / 2]) / 2
}

foreach ($n in $Concurrency) {
    $kept = @()
    for ($rep = 1; $rep -le $Repeats; $rep++) {
        $tasks = @()
        $sw = [Diagnostics.Stopwatch]::StartNew()
        for ($i = 0; $i -lt $n; $i++) {
            $tasks += Invoke-One -Prompt $Prompts[$i % $Prompts.Count]
        }
        [Threading.Tasks.Task]::WaitAll($tasks)
        $sw.Stop()

        $completion = 0
        $prompt_t   = 0
        $stops      = 0
        foreach ($t in $tasks) {
            $json = $t.Result.Content.ReadAsStringAsync().Result | ConvertFrom-Json
            $completion += $json.usage.completion_tokens
            $prompt_t   += $json.usage.prompt_tokens
            if ($json.choices[0].finish_reason -eq 'length' -or $json.choices[0].finish_reason -eq 'stop') { $stops++ }
        }

        $secs = $sw.Elapsed.TotalSeconds
        $agg  = $completion / $secs
        # Run 1 is warm-up and never enters the statistics, but it IS printed - a warm-up that
        # differs wildly from the rest is itself a finding about cache state.
        if ($rep -gt 1) { $kept += $agg }
        Write-Output ("  N=$n rep $rep/$Repeats : {0,6:N2} s, {1} tokens, {2,6:N2} tok/s{3}" -f `
            $secs, $completion, $agg, $(if ($rep -eq 1) { '  (warm-up, discarded)' } else { '' }))
    }

    $med = Get-Median -Values $kept
    $mn  = ($kept | Measure-Object -Minimum).Minimum
    $mx  = ($kept | Measure-Object -Maximum).Maximum
    $results += [pscustomobject]@{
        N          = $n
        Runs       = $kept.Count
        Median     = [math]::Round($med, 2)
        Min        = [math]::Round($mn, 2)
        Max        = [math]::Round($mx, 2)
        Spread_pct = if ($mn -gt 0) { [math]::Round(($mx - $mn) / $mn * 100, 1) } else { 0 }
        PerReq     = [math]::Round($med / $n, 2)
        Answered   = "$stops/$n"
    }
}

Write-Output ""
$results | Format-Table -AutoSize

$baseRow = $results[0]
$base    = $baseRow.Median
Write-Output "Speedup against N=$($baseRow.N), medians:"
foreach ($r in $results) {
    # A step counts as SURVIVING only if its median clears the baseline's max. Anything inside
    # the baseline's own spread is indistinguishable from noise, and saying so is the point of
    # printing it rather than the bare factor.
    $verdict = if ($r.N -eq $baseRow.N) { '-' }
               elseif ($r.Median -gt $baseRow.Max) { 'clears baseline spread' }
               else { 'INSIDE baseline spread - not distinguishable from noise' }
    Write-Output ("  N={0}  median {1,7:N2} tok/s  factor {2,5:N2}  spread {3,5:N1}%  {4}" -f `
        $r.N, $r.Median, ($r.Median / $base), $r.Spread_pct, $verdict)
}
Write-Output ""
Write-Output ("baseline N={0}: median {1:N2}, min {2:N2}, max {3:N2} over {4} runs" -f `
    $baseRow.N, $baseRow.Median, $baseRow.Min, $baseRow.Max, $baseRow.Runs)

$client.Dispose()
