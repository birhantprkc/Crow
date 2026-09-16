<#
Measures the RESOLUTION of the quality gate: how far its own score moves between runs of an
identical configuration. Ausweg 1 on issue #46.

THE QUESTION
On 2026-08-04 two runs of the same model, the same operating point, --temp 0, seed 1234 and the
same ten prompts produced 10 of 10 and 8 of 10. A lever that costs one or two tasks is therefore
not distinguishable from the gate's own movement, and every k/N the gate prints carries that
caveat. This script replaces the single sample with a median and a spread, so the caveat becomes
a number instead of a warning.

NO WARM-UP RUN IS DISCARDED - AND THE REASON FIRST GIVEN FOR IT WAS AN ASSUMPTION
measure-slot-scaling.ps1 drops its first run, because a cold expert cache makes the first pass
slower and mixing it in would credit a batch for a cache effect. This script was written with
the argument that the same does not apply here, because what is measured is the verdict and
not the rate, and a cold cache "changes the wall clock, not the arithmetic".

That last clause was stated as a reason without ever having been measured. The first series
does not support it - and does not establish its opposite either:

  Measured 2026-08-05, three runs, one server session. Run 1 took 655 s against 548 and 529 -
  the expected warm-up on the clock. Run 1 is also the odd one out in CONTENT: runs 2 and 3
  agree on 10 of 10 verdicts and 7 of 10 byte-identical outputs, while run 1 against either of
  them gives 9 of 10 verdicts and 5 of 10 byte-identical. The single verdict that moved,
  top-k-frequent, is correct in run 1 and identically broken in runs 2 and 3 - so run 1 is the
  odd one out while also carrying the BEST aggregate.

  Neither claim is established: not "a cold pass changes only the clock", and not "the cold
  state changed the output". In a fixed-order series POSITION and WARMTH are confounded, and
  with three runs the odd one out is the first with probability 1/3 by chance alone.

  MORE RUNS IN THE SAME ORDER DO NOT SETTLE THIS. They add repetitions, not identification.
  A causal test needs the conditions varied against the position - alternating or randomised
  order, or a server deliberately warmed before run 1 - and that is a different measurement
  from this one. Until it exists, run 1 is kept and this caveat travels with every figure
  taken from a series.

  If a wall-clock or tokens/s figure is ever quoted FROM THESE RUNS, run 1 must be dropped
  first. The per-run seconds are recorded for exactly that reason and are not summarised here.

THE FAILURE CASE THIS MUST BE ABLE TO SHOW
That the gate is unstable. A spread of two tasks, or a task whose verdict differs across runs,
is the expected outcome given what #46 already measured - it is reported as-is and named per
task. A script that could only produce "stable" would measure nothing. The opposite result is
equally reachable: identical scores across all runs print a spread of 0.

WHAT IT DELIBERATELY DOES NOT DO
It does not change how a task is judged, and it does not average verdicts into a smoother
number. A scoring rule invented to make the figure look stable would be the worst possible
outcome of this measurement (#46).

WHAT THE NUMBER MEANS
With -Runs 3 the median is the middle of three, and the spread is max minus min over three -
a denominator small enough that it is printed next to every figure. Three runs bound the
movement; they do not estimate a distribution.

COST
Roughly 13 min per run when nothing is cut off, up to ~24 min when two-sum needs its rerun at
the doubled budget. Three runs: about 40 to 72 minutes. EUR 0.

Usage:
  measure-gate-stability.ps1                    # 3 runs against port 8081
  measure-gate-stability.ps1 -Runs 5 -Port 8081
#>
param(
    [int]    $Port       = 8081,
    [int]    $Runs       = 3,
    [int]    $MaxTokens  = 4096,
    [int]    $TimeoutSec = 900,
    [string] $Python     = 'python',
    [string] $OutRoot    = ''
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Gate     = Join-Path $PSScriptRoot 'probe-suite.py'
$Url      = "http://127.0.0.1:$Port"

if (-not (Test-Path $Gate)) { throw "probe-suite.py not found at $Gate" }
if ($Runs -lt 2) { throw "A single run cannot show a spread. -Runs must be at least 2." }

if ([string]::IsNullOrWhiteSpace($OutRoot)) {
    $stamp   = (Get-Date -Format 'yyyy-MM-dd')
    $OutRoot = Join-Path $RepoRoot "runs/$stamp/quality-stability"
}
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null

Write-Host ""
Write-Host "Gate resolution: $Runs identical runs, max_tokens $MaxTokens, $Url"
Write-Host "Output: $OutRoot"
Write-Host ""

# The endpoint is checked once, before anything long starts. A run series that dies on run 1
# because nothing is listening wastes the operator's attention, not the machine's.
try {
    $health = Invoke-RestMethod -Uri "$Url/health" -TimeoutSec 10
    Write-Host ("  endpoint  {0}" -f ($health | ConvertTo-Json -Compress))
} catch {
    throw "No endpoint on $Url - start llama-server first. ($($_.Exception.Message))"
}

$summaries = @()
for ($i = 1; $i -le $Runs; $i++) {
    $dir = Join-Path $OutRoot ("run-{0}" -f $i)
    Write-Host ""
    Write-Host ("=== run {0} of {1} ===" -f $i, $Runs)
    $started = Get-Date

    & $Python $Gate run --url $Url --out $dir --max-tokens $MaxTokens --timeout $TimeoutSec
    $code = $LASTEXITCODE

    $elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    $file = Join-Path $dir 'summary.json'
    if (-not (Test-Path $file)) {
        # Exit 3 is CHECKER BROKEN: the harness refused to judge, so there is nothing to
        # aggregate and continuing would average over a gap.
        throw "run $i produced no summary.json (exit $code). Aborting the series."
    }
    $s = Get-Content $file -Raw | ConvertFrom-Json
    $summaries += [pscustomobject]@{
        Run       = $i
        Exit      = $code
        Correct   = $s.correct
        Judged    = $s.judged
        Total     = $s.total
        Undecided = $s.undecided
        Reran     = @($s.reran_tasks)
        Seconds   = $elapsed
        Results   = $s.results
    }
    Write-Host ("  -> {0} of {1} judged correct, {2} undecided, exit {3}, {4}s" -f `
        $s.correct, $s.judged, $s.undecided, $code, $elapsed)
}

# ---------------------------------------------------------------- the figures

function Get-Median([double[]] $values) {
    $sorted = $values | Sort-Object
    $n = $sorted.Count
    if ($n % 2 -eq 1) { return $sorted[[int](($n - 1) / 2)] }
    return ($sorted[$n / 2 - 1] + $sorted[$n / 2]) / 2
}

$scores    = @($summaries | ForEach-Object { [double]$_.Correct })
$judged    = @($summaries | ForEach-Object { [double]$_.Judged })
$median    = Get-Median $scores
$minScore  = ($scores | Measure-Object -Minimum).Minimum
$maxScore  = ($scores | Measure-Object -Maximum).Maximum
$spread    = $maxScore - $minScore

Write-Host ""
Write-Host "=================================================================="
Write-Host ("RESULT over {0} identical runs" -f $Runs)
Write-Host "=================================================================="
foreach ($s in $summaries) {
    $rr = if ($s.Reran.Count) { " rerun: " + ($s.Reran -join ',') } else { "" }
    Write-Host ("  run {0}   {1} of {2} correct   {3} undecided   exit {4}   {5}s{6}" -f `
        $s.Run, $s.Correct, $s.Judged, $s.Undecided, $s.Exit, $s.Seconds, $rr)
}
Write-Host ""
Write-Host ("  MEDIAN   {0} correct" -f $median)
Write-Host ("  SPREAD   {0} .. {1}  (span {2} task(s) over {3} runs)" -f `
    $minScore, $maxScore, $spread, $Runs)

# The denominator is not assumed to be ten. If a rerun was itself cut off, that run judged
# fewer tasks, and a median taken over unequal denominators would be a different quantity.
$judgedSet = @($judged | Sort-Object -Unique)
if ($judgedSet.Count -gt 1) {
    Write-Host ("  WARNING  the denominator moved too: judged = {0}. The scores above are" -f `
        ($judgedSet -join ', '))
    Write-Host  "           not over the same number of tasks and must not be compared directly."
} else {
    Write-Host ("  denominator {0} tasks, identical in every run" -f $judgedSet[0])
}

# ---------------------------------------------------------------- per task

Write-Host ""
Write-Host "Per task, across all runs:"
$taskNames = @($summaries[0].Results | ForEach-Object { $_.task })
$unstable = @()
foreach ($name in $taskNames) {
    $verdicts = @()
    foreach ($s in $summaries) {
        $r = $s.Results | Where-Object { $_.task -eq $name }
        if ($r) { $verdicts += $r.verdict } else { $verdicts += 'MISSING' }
    }
    $distinct = @($verdicts | Sort-Object -Unique)
    if ($distinct.Count -gt 1) {
        $unstable += $name
        Write-Host ("  CHANGED  {0,-22} {1}" -f $name, ($verdicts -join ' | '))
    } else {
        Write-Host ("  stable   {0,-22} {1}" -f $name, $distinct[0])
    }
}

Write-Host ""
if ($unstable.Count -eq 0) {
    Write-Host ("  Every task kept its verdict across {0} runs. That is 0 of {0} observed changes" -f $Runs)
    Write-Host  "  per task - good practical stability, NOT a proven spontaneous failure rate of zero."
} else {
    Write-Host ("  NOT verdict-stable in {0} of {1} tasks: {2}" -f `
        $unstable.Count, $taskNames.Count, ($unstable -join ', '))
}
# Deliberately a RANGE and not a "+/-": a span observed over a handful of runs is not a
# symmetric uncertainty and not a confidence interval, and writing it as one would claim a
# statistic that three runs cannot carry.
Write-Host ("  Observed aggregate range: {0} to {1} of {2} over {3} runs (span {4})." -f `
    $minScore, $maxScore, $judgedSet[0], $Runs, $spread)
Write-Host ("  Detection limit in the aggregate: at least {0} task(s), if a result must fall" -f `
    ($spread + 1))
Write-Host  "  clearly outside the observed range."
Write-Host  "  On a task that did not move here, a change is DETECTABLE but not yet ATTRIBUTABLE:"
Write-Host  "  replicate that one task under both conditions before calling it a lever effect."
if ($unstable.Count -gt 0) {
    Write-Host ("  A change confined to {0} is not interpretable as a lever effect while it" -f `
        ($unstable -join ', '))
    Write-Host  "  lies inside the baseline behaviour already observed without any lever."
}

Write-Host ""
Write-Host ("Raw runs: {0}" -f $OutRoot)

# Finding movement IS a successful measurement, so the series exits 0 whenever it completed.
# Without this the script would pass through the exit code of the LAST run - a run that found
# a wrong answer would look like a script that failed. An aborted series throws instead.
exit 0
