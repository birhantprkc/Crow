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
    [int]    $TimeoutSec  = 600,
    # Machine-readable copy of the result rows. A caller that has to parse the printed lines
    # breaks the moment a word in them changes, and the printed lines exist for a human.
    [string] $OutJson     = '',
    # Measured 2026-08-05: at 128 tokens this model spends the whole budget inside its
    # reasoning block and returns finish_reason=length with an EMPTY content. The throughput
    # is real, the workload is "the model thinks and never answers" - and an n-gram method
    # re-drafting a repeated thinking sequence looked like a 2x lever because of it (#29).
    # With -RequireAnswer a run is only kept once it produced a visible answer that was not
    # cut off; the budget doubles until it does or until -MaxTokens is reached.
    [switch] $RequireAnswer,
    [int]    $MaxTokens   = 4096
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
    param([string]$Prompt, [int]$Budget = 0)
    if ($Budget -le 0) { $Budget = $Tokens }
    $payload = @{
        model       = 'crow'
        messages    = @(@{ role = 'user'; content = $Prompt })
        max_tokens  = $Budget
        temperature = 0
        seed        = 1234
    } | ConvertTo-Json -Depth 5
    $content = New-Object System.Net.Http.StringContent($payload, [Text.Encoding]::UTF8, 'application/json')
    return $client.PostAsync($uri, $content)
}

function Get-RunClass {
    <#
      Five classes, because they answer different questions and collapsing them is how a
      throughput number starts describing work that never happened:
        complete-answer  - visible answer, and it was allowed to end on its own
        truncated-answer - visible answer, cut off by the budget
        reasoning-only   - no answer at all, the budget ran out inside the thinking block
        empty-generation - neither answer nor thinking
        request-failure  - the request itself did not come back
    #>
    param($Json)
    $c       = $Json.choices[0]
    $answer  = [string]$c.message.content
    $thought = [string]$c.message.reasoning_content
    $fin     = [string]$c.finish_reason
    if ($answer.Trim().Length -gt 0) {
        if ($fin -eq 'length') { return 'truncated-answer' }
        return 'complete-answer'
    }
    if ($thought.Trim().Length -gt 0) { return 'reasoning-only' }
    return 'empty-generation'
}

# Reachability first. Without this a refused connection looks like zero throughput.
try {
    $probe = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 15
    Write-Output "health: $($probe.status)  port $Port"
} catch {
    Write-Output "UNREACHABLE on port $Port - $($_.Exception.Message)"
    exit 1
}
# At N=1 there is only prompt index 0, so every repeat sends the SAME prompt. "distinct prompts"
# describes what happens across concurrent slots, not across repeats, and saying otherwise made
# the N=1 rows read as if they had varied the input.
$promptNote = if (($Concurrency | Measure-Object -Maximum).Maximum -gt 1) {
    "distinct prompts across slots, the same prompt across repeats"
} else { "one slot: the SAME prompt in every repeat" }
Write-Output "tokens/request = $Tokens, repeats = $Repeats (run 1 discarded as warm-up), $promptNote"
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

if ($RequireAnswer -and (($Concurrency | Measure-Object -Maximum).Maximum -gt 1)) {
    # Raising the budget for one request while others are in flight would mix a retry into the
    # wall clock the concurrent run is measuring. Refuse rather than report a polluted number.
    throw "-RequireAnswer is only supported at one slot; escalating a budget mid-flight would pollute the concurrent timing."
}

foreach ($n in $Concurrency) {
    $kept = @()
    $draftN = 0        # accumulated over the KEPT runs only, same as the throughput statistics
    $draftAcc = 0
    $classes = @()
    $budgets = @()
    $unusable = 0
    $acqSecs = 0.0
    $acqTokens = 0
    $scoredSecs = 0.0
    $scoredTokens = 0
    $attemptCounts = @()
    $attemptBudgets = @()
    for ($rep = 1; $rep -le $Repeats; $rep++) {
        $budget   = $Tokens
        $attempts = 0
        # Two clocks, on purpose. The SCORED one is the final attempt and is what a method
        # comparison may use - charging a method for the budget rule would make it look slow
        # for a decision the harness took. The ACQUISITION one is every attempt together and
        # is the honest operating cost of the adaptive rule. Dropping either one hides
        # something: the first would blame the method, the second would hide the bill.
        $repAcqSecs   = 0.0
        $repAcqTokens = 0
        $repBudgets   = @()
        do {
            $attempts++
            $repBudgets += $budget
            $tasks = @()
            $sw = [Diagnostics.Stopwatch]::StartNew()
            for ($i = 0; $i -lt $n; $i++) {
                $tasks += Invoke-One -Prompt $Prompts[$i % $Prompts.Count] -Budget $budget
            }
            [Threading.Tasks.Task]::WaitAll($tasks)
            $sw.Stop()
            $repAcqSecs += $sw.Elapsed.TotalSeconds
            # The class of a rep is the worst class among its requests.
            $order = @{ 'complete-answer' = 0; 'truncated-answer' = 1; 'reasoning-only' = 2; 'empty-generation' = 3 }
            $repClass = 'complete-answer'
            foreach ($t in $tasks) {
                $j = $t.Result.Content.ReadAsStringAsync().Result | ConvertFrom-Json
                $c = Get-RunClass -Json $j
                if ($order[$c] -gt $order[$repClass]) { $repClass = $c }
                $repAcqTokens += [int]$j.usage.completion_tokens
            }
            $needsMore = $RequireAnswer -and ($repClass -ne 'complete-answer') -and ($budget -lt $MaxTokens)
            if ($needsMore) {
                # Report the budget that FAILED, not the doubled one derived from it - after a
                # cap by Min() the halved value would no longer be the budget that was used.
                $failed = $budget
                $budget = [math]::Min($budget * 2, $MaxTokens)
                Write-Output ("  N=$n rep $rep/$Repeats : $repClass at $failed tokens - retrying at $budget")
            }
        } while ($needsMore)
        # Only the FINAL attempt is timed and counted. Including the discarded attempts would
        # charge the budget rule to the method under test.
        $classes  += $repClass
        $budgets  += $budget
        $acqSecs   += $repAcqSecs
        $acqTokens += $repAcqTokens
        $attemptCounts  += $attempts
        $attemptBudgets += ,@($repBudgets)
        if ($RequireAnswer -and $repClass -ne 'complete-answer') { $unusable++ }

        $completion = 0
        $prompt_t   = 0
        $stops      = 0
        $repDraftN   = 0
        $repDraftAcc = 0
        foreach ($t in $tasks) {
            $json = $t.Result.Content.ReadAsStringAsync().Result | ConvertFrom-Json
            $completion += $json.usage.completion_tokens
            $prompt_t   += $json.usage.prompt_tokens
            if ($json.choices[0].finish_reason -eq 'length' -or $json.choices[0].finish_reason -eq 'stop') { $stops++ }

            # Speculative decoding: the server adds draft_n / draft_n_accepted to `timings`
            # ONLY when draft_n > 0 (server-task.cpp:259). Their absence is therefore a
            # measurement in itself - the method drafted nothing at all - and must not be
            # confused with an acceptance rate of zero, where it drafted and was refused.
            # A factor without this number is not transferable to another model (#29).
            if ($null -ne $json.timings -and $null -ne $json.timings.draft_n) {
                $repDraftN   += [int]$json.timings.draft_n
                $repDraftAcc += [int]$json.timings.draft_n_accepted
            }
        }

        $secs = $sw.Elapsed.TotalSeconds
        $agg  = $completion / $secs
        # Run 1 is warm-up and never enters the statistics, but it IS printed - a warm-up that
        # differs wildly from the rest is itself a finding about cache state.
        if ($rep -gt 1) {
            $kept += $agg
            # Same population as the throughput statistic. Counting drafts from the discarded
            # warm-up would put two numbers from two different sets side by side.
            $draftN       += $repDraftN
            $draftAcc     += $repDraftAcc
            $scoredSecs   += $secs
            $scoredTokens += $completion
        }
        $repDraft = if ($repDraftN -gt 0) {
            "  draft {0}/{1} = {2,5:P1}" -f $repDraftAcc, $repDraftN, ($repDraftAcc / $repDraftN)
        } else { "" }
        Write-Output ("  N=$n rep $rep/$Repeats : {0,6:N2} s, {1} tokens, {2,6:N2} tok/s  [{5} @{6}]{3}{4}" -f `
            $secs, $completion, $agg, $(if ($rep -eq 1) { '  (warm-up, discarded)' } else { '' }), `
            $repDraft, $repClass, $budget)
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
        # The individual kept runs, in order. A median plus a spread cannot tell a rising
        # sequence from a random one, and on 2026-08-05 exactly that gap cost the explanation
        # of a 152 % spread: one run of ngram-mod hit twice the baseline and the summary could
        # not say whether it was the last.
        Runs_kept  = @($kept | ForEach-Object { [math]::Round($_, 2) })
        Classes    = @($classes)
        Budgets    = @($budgets)
        Unusable   = $unusable
        # Scored: the final attempts only, over the kept runs - the basis for a comparison.
        # Acquisition: every attempt of every rep, warm-up included - the operating cost.
        Scored_seconds      = [math]::Round($scoredSecs, 2)
        Scored_tokens       = $scoredTokens
        Acquisition_seconds = [math]::Round($acqSecs, 2)
        Acquisition_tokens  = $acqTokens
        Attempt_counts      = @($attemptCounts)
        Attempt_budgets     = @($attemptBudgets)
        # A row only describes ANSWER throughput when every rep actually produced an answer
        # that was allowed to end. Otherwise it describes generation, and says so.
        Measures   = if (@($classes | Where-Object { $_ -ne 'complete-answer' }).Count -eq 0) {
                         'answer throughput'
                     } else { 'generation throughput only - not complete answers' }
        Drafted    = $draftN
        Accepted   = $draftAcc
        AcceptRate = if ($draftN -gt 0) { "{0:P1}" -f ($draftAcc / $draftN) } else { "no draft" }
    }
}

Write-Output ""
# AcceptRate is deliberately NOT in this table: Format-Table -AutoSize drops trailing columns
# when the row gets wide, and it silently dropped exactly this one on the first run. A number
# that decides whether a factor is transferable does not go where the formatter may eat it.
$results | Format-Table -AutoSize N, Runs, Median, Min, Max, Spread_pct, PerReq, Answered

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
Write-Output "What each row actually measured:"
foreach ($r in $results) {
    Write-Output ("  N={0}  {1}" -f $r.N, $r.Measures)
    Write-Output ("        classes: {0}" -f (($r.Classes | Group-Object | ForEach-Object { "$($_.Count)x $($_.Name)" }) -join ', '))
    Write-Output ("        budgets: {0}" -f (($r.Budgets | Sort-Object -Unique) -join ', '))
    if ($r.Acquisition_seconds -gt $r.Scored_seconds) {
        # The gap is what the budget rule cost. It is not charged to the method, and it is
        # not swept away either.
        Write-Output ("        scored {0,7:N1} s / {1} tokens  vs  acquisition {2,7:N1} s / {3} tokens over {4} attempt(s)" -f `
            $r.Scored_seconds, $r.Scored_tokens, $r.Acquisition_seconds, $r.Acquisition_tokens, `
            (($r.Attempt_counts | Measure-Object -Sum).Sum))
    }
}
# reasoning-only means the model never reached its answer. A throughput number from such a run
# is a real rate over generated tokens and NOT a statement about answering work - #29 was
# nearly answered with exactly that confusion.

Write-Output ""
Write-Output "Speculative drafting, over the kept runs:"
foreach ($r in $results) {
    # "no draft" is NOT 0 %. It means the method never proposed a token, which is a different
    # finding from proposing and being refused - and for an n-gram method on a workload whose
    # output does not repeat its input, it is the expected one. #29 wants this number because
    # the factor alone does not transfer to another model; the acceptance rate does.
    if ($r.Drafted -gt 0) {
        Write-Output ("  N={0}  accepted {1} of {2} drafted = {3}" -f `
            $r.N, $r.Accepted, $r.Drafted, $r.AcceptRate)
    } else {
        Write-Output ("  N={0}  NO DRAFT - the server reported no draft_n at all, so nothing was" -f $r.N)
        Write-Output  "        proposed. This is not an acceptance rate of zero."
    }
}
Write-Output ""
Write-Output ("baseline N={0}: median {1:N2}, min {2:N2}, max {3:N2} over {4} runs" -f `
    $baseRow.N, $baseRow.Median, $baseRow.Min, $baseRow.Max, $baseRow.Runs)

if (-not [string]::IsNullOrWhiteSpace($OutJson)) {
    $dir = Split-Path -Parent $OutJson
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $payload = @{
        tokens_per_request = $Tokens
        repeats            = $Repeats
        kept_runs          = $Repeats - 1
        port               = $Port
        rows               = $results
    } | ConvertTo-Json -Depth 5
    # NOT Out-File -Encoding utf8: Windows PowerShell 5.1 writes a BOM with it, and Python's
    # json.load then dies on "Unexpected UTF-8 BOM" reading a file this script just wrote.
    [IO.File]::WriteAllText($OutJson, $payload, (New-Object Text.UTF8Encoding($false)))
    Write-Output ("json: {0}" -f $OutJson)
}

$client.Dispose()
