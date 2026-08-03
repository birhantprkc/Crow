<#
Stage 5 phase C: drives the per-token probe N times and asks the one question a single
run cannot answer.

WHAT THIS ANSWERS, AND WHY ONE RUN COULD NOT

Measured 2026-08-03, one run of 31 decode steps: 118.37 ms fastest, 354.34 ms slowest,
a spread of 2.99x INSIDE a single run. That killed both standing theories at once - the
steps are neither uniformly stretched nor a flat field with one stall in it. What it
could not say is whether the shape means anything:

  - if the SAME positions are slow in every run, the pattern is structural and points at
    something that depends on the step index - the expert set, the cache state, a warm-up
  - if the positions move but the spread stays, the step time is dispersed and the index
    carries no information

Those two lead to completely different next measurements, which is why this exists
before any of them.

THE DISCRIMINATOR, AND IT IS NOT A CORRELATION COEFFICIENT

  spread_within(run) = max over positions / min over positions, inside one run
  spread_at(index)   = max over runs      / min over runs,      at one position

Structural: spread_at stays near 1 while spread_within stays high - the same position is
slow every time. Dispersed: the two land close together, because a position is no more
predictable than any other. The script prints both medians and states which it saw. It
does not average the runs; the spread IS the finding, exactly as run-stage5-bench.ps1
says of its own arms.

TRAPS THIS SCRIPT AVOIDS, EACH ONE PAID FOR ALREADY

  - ONE RAW FILE PER RUN. Writing them all to the same path would have the parser read
    the last run N times and report perfect reproducibility. The files are compared
    against each other and an all-identical set is refused.
  - A RUN WITHOUT THE PROBE LINE IS INVALID, NOT EMPTY. Silently skipping it would
    shorten the series and leave a figure with a denominator nobody checked. This is the
    stage 2 guard from probe-queue-depth.py, one level up.
  - AN "INVALID" LINE FROM THE PROBE REJECTS THE RUN. It means several tokens collapsed
    into one measurement, so an entry is not a token and the row is not comparable.
  - THE COUNT IS CHECKED TWICE: against the probe's own summary line, and across runs.
    A ragged series would still produce a CSV, and the CSV would look fine.
  - CULTURE-INVARIANT NUMBERS. PowerShell formats with the system culture, and on this
    German machine 2.99 becomes "2,99" - which turns a CSV column into two. Every number
    written or printed here goes through InvariantCulture.
  - common_perf_print WRITES TO stderr. A plain ">" catches stdout only; that is how a
    whole measurement series was lost once. Each run redirects both, separately.

Usage:
  run-token-series.ps1 -Runs 5 -OutDir <dir>
  run-token-series.ps1 -SelfTest <a known-good raw file>    # parser check, no model run
#>

param(
    [int]$Runs = 5,
    [string]$OutDir = '',
    [int]$Predict = 32,
    [string]$Prompt = 'C:\Users\robin\dev\Crow\tools\prompts\probe-b-completion.txt',
    [string]$Model  = 'C:\Users\robin\dev\crow-lab\models\DeepSeek-V4-Flash-MXFP4.gguf',
    [string]$Bin    = 'C:\Users\robin\dev\crow-lab\src\build-native\bin\Release',
    [string]$Ncmoe  = '-ncmoe 999',
    # Runs the parser against one real file and against two deliberately broken ones, then
    # stops. Before this existed, the only evidence the regexes matched was the printf in
    # the C++ source - and a cause read from code is a guess.
    [string]$SelfTest = '',
    # Re-reads an existing series from its raw logs and redoes the summary, without
    # touching the model. Added 2026-08-03 to give a reporting fix a way to be checked
    # against data whose answer was already known - re-running five model passes to verify
    # a printed line would have measured the machine again instead of the change.
    [string]$Reanalyse = ''
)

$inv = [System.Globalization.CultureInfo]::InvariantCulture

function Format-Inv([double]$v, [int]$digits = 2) {
    return $v.ToString("F$digits", $inv)
}

# Returns a hashtable: Ok, Reason, Steps (int64[]), Summary (the probe's own count).
function Read-Steps([string]$path) {
    if (-not (Test-Path $path)) {
        return @{ Ok = $false; Reason = "no such file: $path" }
    }
    $text = Get-Content $path -Raw
    if ($null -eq $text) { $text = '' }

    if ($text -match 'per-token eval = INVALID') {
        return @{ Ok = $false; Reason = 'probe reported INVALID - steps were merged, one entry is not one token' }
    }

    $head = [regex]::Match($text, 'per-token eval = (\d+) steps')
    if (-not $head.Success) {
        return @{ Ok = $false; Reason = 'no "per-token eval" line - the probe did not report, so this run is invalid rather than empty' }
    }
    $claimed = [int]$head.Groups[1].Value

    $steps = New-Object System.Collections.Generic.List[int64]
    foreach ($m in [regex]::Matches($text, 'steps \[us\]:([0-9 ]+)')) {
        foreach ($tok in ($m.Groups[1].Value -split '\s+')) {
            if ($tok -ne '') { $steps.Add([int64]::Parse($tok, $inv)) }
        }
    }

    if ($steps.Count -ne $claimed) {
        return @{ Ok = $false; Reason = "parsed $($steps.Count) values against $claimed in the probe's own summary" }
    }
    if ($steps.Count -eq 0) {
        return @{ Ok = $false; Reason = 'zero values parsed' }
    }
    return @{ Ok = $true; Steps = $steps.ToArray(); Summary = $claimed }
}

# --- self test ---------------------------------------------------------------------
if ($SelfTest -ne '') {
    Write-Output "=== SELFTEST: the parser must accept a real run and refuse two broken ones ==="
    Write-Output ""
    $fail = 0

    $good = Read-Steps $SelfTest
    if ($good.Ok) {
        Write-Output "  ok    a real run parses: $($good.Steps.Count) values"
    } else {
        Write-Output "  FAIL  a real run did not parse: $($good.Reason)"
        $fail = 1
    }

    $tmp = Join-Path $env:TEMP "token-series-selftest"
    if (-not (Test-Path $tmp)) { New-Item -ItemType Directory -Path $tmp | Out-Null }

    # Broken 1: the probe never reported. Must be INVALID, not "0 values".
    $b1 = Join-Path $tmp 'no-probe-line.txt'
    "common_perf_print:        eval time =    6612.30 ms /    31 runs" | Set-Content $b1 -Encoding utf8
    $r1 = Read-Steps $b1
    if (-not $r1.Ok) {
        Write-Output "  ok    a run without the probe line is refused: $($r1.Reason)"
    } else {
        Write-Output "  FAIL  a run without the probe line came back valid"
        $fail = 1
    }

    # Broken 2: the probe itself declared the mapping void.
    $b2 = Join-Path $tmp 'invalid-line.txt'
    "common_perf_print: per-token eval = INVALID - 12 steps timed against 31 in n_eval" | Set-Content $b2 -Encoding utf8
    $r2 = Read-Steps $b2
    if (-not $r2.Ok) {
        Write-Output "  ok    an INVALID run is refused: $($r2.Reason)"
    } else {
        Write-Output "  FAIL  an INVALID run came back valid"
        $fail = 1
    }

    # Broken 3: header and body disagree - the shape a truncated log would have.
    $b3 = Join-Path $tmp 'count-mismatch.txt'
    @(
        "common_perf_print: per-token eval = 31 steps, min 118.37 ms, max 354.34 ms, max/min 2.99x"
        "common_perf_print:    steps [us]:   354338   343849   238105"
    ) | Set-Content $b3 -Encoding utf8
    $r3 = Read-Steps $b3
    if (-not $r3.Ok) {
        Write-Output "  ok    a truncated body is refused: $($r3.Reason)"
    } else {
        Write-Output "  FAIL  a truncated body came back valid"
        $fail = 1
    }

    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Write-Output ""
    if ($fail -ne 0) {
        Write-Output "RESULT: FAIL - the parser cannot tell a good run from a broken one."
        exit 1
    }
    Write-Output "RESULT: PASS - the parser accepts what it must and refuses what it must."
    exit 0
}

# Position of the extreme value, 1-based. Written as a loop rather than with
# [array]::IndexOf, which returned -1 here on 2026-08-03: Measure-Object hands back a
# value whose type does not match the int64 array, IndexOf finds nothing, and 1 + (-1)
# printed "#0" for every run. That reads as a position that reproduces perfectly, which
# is exactly the shape of a finding - and it was an artefact of a failed lookup.
function Find-ArgExtreme([int64[]]$v, [switch]$Min) {
    $best = 0
    for ($i = 1; $i -lt $v.Count; $i++) {
        if ($Min) { if ($v[$i] -lt $v[$best]) { $best = $i } }
        else      { if ($v[$i] -gt $v[$best]) { $best = $i } }
    }
    return $best + 1
}

# --- the series --------------------------------------------------------------------
if ($OutDir -eq '' -and $Reanalyse -eq '') {
    Write-Output "SETUP ERROR: -OutDir is required"
    exit 2
}
if ($Reanalyse -ne '') { $OutDir = $Reanalyse }
if ($Runs -lt 2) {
    Write-Output "SETUP ERROR: -Runs must be at least 2. One run is an observation, not a series."
    exit 2
}
foreach ($p in @($Prompt, $Model, (Join-Path $Bin 'llama-completion.exe'))) {
    if (-not (Test-Path $p)) { Write-Output "SETUP ERROR: no such path: $p"; exit 2 }
}
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

Write-Output "model     $Model"
Write-Output "prompt    $Prompt"
Write-Output "args      -n $Predict --temp 0 --seed 1234 -c 4096 -np 1 --no-warmup -ngl 99 $Ncmoe"
Write-Output "runs      $Runs, LLAMA_TOKEN_TIMING=1"
Write-Output ""

$env:LLAMA_TOKEN_TIMING = '1'
$cliArgs = "-m `"$Model`" -f `"$Prompt`" -no-cnv --no-display-prompt --temp 0 --seed 1234 " +
           "-n $Predict -c 4096 -np 1 --no-warmup -ngl 99 $Ncmoe"

if ($Reanalyse -ne '') {
    $Runs = (Get-ChildItem -Path $OutDir -Filter 'run-*.raw.txt' | Measure-Object).Count
    if ($Runs -lt 2) {
        Write-Output "SETUP ERROR: found $Runs raw logs in $OutDir; a series needs at least 2"
        exit 2
    }
    Write-Output "REANALYSE  $Runs existing logs, no model run"
    Write-Output ""
}

$all = @()
for ($r = 1; $r -le $Runs; $r++) {
    $outFile = Join-Path $OutDir ("run-{0:D2}.txt"    -f $r)
    $rawFile = Join-Path $OutDir ("run-{0:D2}.raw.txt" -f $r)
    if ($Reanalyse -ne '') {
        $code = 0
        $secs = 0.0
    } else {
        $started = Get-Date
        cmd.exe /c "`"$Bin\llama-completion.exe`" $cliArgs > `"$outFile`" 2> `"$rawFile`""
        $code = $LASTEXITCODE
        $secs = ((Get-Date) - $started).TotalSeconds
    }

    if ($code -ne 0) {
        Write-Output "--- run $r  exit $code  $(Format-Inv $secs 1)s"
        Write-Output "    ABORT: llama-completion exited $code. The series is invalid, not short."
        exit 1
    }

    $parsed = Read-Steps $rawFile
    if (-not $parsed.Ok) {
        Write-Output "--- run $r  exit 0  $(Format-Inv $secs 1)s"
        Write-Output "    ABORT: $($parsed.Reason)"
        Write-Output "    A run that did not report is invalid. Dropping it would shorten the"
        Write-Output "    series and leave a figure whose denominator nobody checked."
        exit 1
    }

    $steps = $parsed.Steps
    $mn = ($steps | Measure-Object -Minimum).Minimum
    $mx = ($steps | Measure-Object -Maximum).Maximum
    $ratio = if ($mn -gt 0) { [double]$mx / [double]$mn } else { 0.0 }
    # Positions of the extremes, 1-based. If the pattern is structural these repeat.
    $argmax = Find-ArgExtreme $steps
    $argmin = Find-ArgExtreme $steps -Min

    Write-Output ("--- run {0}  exit 0  {1}s   {2} steps, min {3} ms (#{7}), max {4} ms (#{6}), max/min {5}x" -f `
        $r, (Format-Inv $secs 1), $steps.Count, (Format-Inv ($mn/1000.0)), (Format-Inv ($mx/1000.0)), (Format-Inv $ratio), $argmax, $argmin)

    $all += ,@{ Run = $r; Steps = $steps; Min = $mn; Max = $mx; Ratio = $ratio; ArgMax = $argmax; ArgMin = $argmin; Raw = $rawFile }
}

Write-Output ""

# Every run read the same file back? Then the parser read one run N times.
$hashes = $all | ForEach-Object { (Get-FileHash $_.Raw -Algorithm SHA256).Hash }
if (($hashes | Select-Object -Unique).Count -eq 1) {
    Write-Output "ABORT: all $Runs raw logs are byte-identical. Either they were written to the"
    Write-Output "       same path or the runs never happened. No figure."
    exit 1
}

$counts = $all | ForEach-Object { $_.Steps.Count } | Select-Object -Unique
if ($counts.Count -ne 1) {
    Write-Output "ABORT: the runs produced different step counts ($($counts -join ', '))."
    Write-Output "       A ragged series still yields a CSV, and the CSV still looks fine."
    exit 1
}
$n = $counts[0]

# --- CSV: one row per position, one column per run ---------------------------------
$csv = Join-Path $OutDir 'token-series.csv'
$header = "token," + (($all | ForEach-Object { "run$($_.Run)_us" }) -join ',')
$lines = @($header)
for ($i = 0; $i -lt $n; $i++) {
    $row = @([string]($i + 1))
    foreach ($a in $all) { $row += [string]$a.Steps[$i] }   # integers, no separator to mangle
    $lines += ($row -join ',')
}
Set-Content -Path $csv -Value $lines -Encoding utf8
Write-Output "CSV       $csv"
Write-Output ""

# --- the discriminator --------------------------------------------------------------
function Median([double[]]$v) {
    $s = $v | Sort-Object
    $c = $s.Count
    if ($c -eq 0) { return 0.0 }
    if ($c % 2 -eq 1) { return [double]$s[[int](($c - 1) / 2)] }
    return ([double]$s[$c/2 - 1] + [double]$s[$c/2]) / 2.0
}

$within = @()
foreach ($a in $all) { $within += $a.Ratio }

$atIndex = @()
for ($i = 0; $i -lt $n; $i++) {
    $col = @()
    foreach ($a in $all) { $col += [double]$a.Steps[$i] }
    $cmin = ($col | Measure-Object -Minimum).Minimum
    $cmax = ($col | Measure-Object -Maximum).Maximum
    if ($cmin -gt 0) { $atIndex += ($cmax / $cmin) }
}

$medWithin = Median ([double[]]$within)
$medAt     = Median ([double[]]$atIndex)

Write-Output "=== does the POSITION reproduce, or only the spread? ==="
Write-Output ("  spread within a run, median over runs        {0}x" -f (Format-Inv $medWithin))
Write-Output ("  spread at a fixed position, median over it   {0}x" -f (Format-Inv $medAt))
Write-Output ("  slowest position per run                     {0}" -f (($all | ForEach-Object { "#$($_.ArgMax)" }) -join ' '))
Write-Output ("  fastest position per run                     {0}" -f (($all | ForEach-Object { "#$($_.ArgMin)" }) -join ' '))
Write-Output ""

# The threshold is deliberately blunt and stated rather than tuned: half. A position that
# is genuinely structural has to be far more stable across runs than the run is across
# positions; anything closer than that is not evidence either way and says so.
if ($medAt -lt ($medWithin / 2.0)) {
    Write-Output "  READING: the position carries information. A given step is far more stable"
    Write-Output "  across runs than the run is across steps, so the shape is structural and the"
    Write-Output "  next question is what depends on the index."
} elseif ($medAt -ge $medWithin) {
    Write-Output "  READING: the position carries nothing. A fixed step varies across runs as"
    Write-Output "  much as the run varies across steps, so the step time is dispersed and the"
    Write-Output "  index is not the handle."
} else {
    Write-Output "  READING: INCONCLUSIVE. The two spreads are within a factor of two of each"
    Write-Output "  other, which is not evidence either way. More runs, or a different question."
}
Write-Output ""
Write-Output "Every run is printed above and none was averaged away. The spread IS the finding."
exit 0
