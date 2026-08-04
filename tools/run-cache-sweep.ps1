<#
Drives the expert-cache sweep: every slot count against every cache placement, in one
deterministic pass, so the order is written down instead of remembered.

WHY THIS EXISTS

On 2026-08-04 this matrix was run by hand, ten invocations. One of them was missing
-no-cnv and sat in interactive mode for four minutes instead of computing - it produced a
file that looked like a run and contained a prompt. The order of the arms lived only in
the transcript, so the series was not repeatable by the next session.

WHAT THE SWEEP ANSWERS, AND WHY ONE ARM COULD NOT

  - hit rate against slot count, at ONE placement -> does more cache buy hits
  - the same slot count at BOTH placements       -> what the slower memory costs

Both questions need arms that differ in exactly one value, and both are void the moment
the arms stop doing the same work. That is what the guards below check.

GUARDS, EACH ONE PAID FOR ALREADY

  - REMAP CALLS MUST BE CHARACTER-IDENTICAL ACROSS ALL ARMS. It is layers x graph runs and
    does not depend on cache size or placement. If it moves, the arms did different work
    and every difference between them is meaningless. Measured 2026-08-04: 11008 in all
    ten runs. A sweep whose arms disagree is VOID, not slow.
  - THE PLACEMENT IS READ BACK FROM THE LOG, NOT ASSUMED. alloc_bufs names the buffer type
    in clear text (CUDA0 / CUDA_Host / CPU) at -lv 4. Until 2026-08-03 llama_moe_stream_select_buft
    never consulted tensor_buft_overrides, so -ncmoe was accepted, reported as applied and
    silently dropped for exactly these tensors. An arm that lands somewhere other than
    requested is VOID - otherwise the sweep measures CUDA0 twice and calls one of them RAM.
  - A RUN WITHOUT print_stats IS INVALID, NOT EMPTY. Skipping it silently would shorten the
    series and leave a figure with a denominator nobody checked.
  - -no-cnv IS NOT OPTIONAL AND NOT A PARAMETER. Without it llama-completion waits on stdin.
  - stderr IS REDIRECTED SEPARATELY. print_stats and common_perf_print write there; a plain
    ">" catches stdout only, and that cost a whole series once.
  - CULTURE-INVARIANT NUMBERS. PowerShell formats with the system culture and on this German
    machine 5.04 becomes "5,04", which turns one CSV column into two.

Usage:
  run-cache-sweep.ps1 -Slots 24,32,40 -Placements cuda0,host -Repeats 2 -OutDir <dir>
  run-cache-sweep.ps1 -SelfTest <a known-good .err.txt>   # parser + guards, no model run
#>

param(
    [int[]]$Slots       = @(24, 32, 40),
    # cuda0 = no override, the cache lands on the card. host = -ncmoe 999, it lands in
    # pinned system RAM. The names are the request; the log says what actually happened.
    [string[]]$Placements = @('cuda0', 'host'),
    [int]$Repeats       = 2,
    [string]$OutDir     = '',
    [int]$Predict       = 256,
    [int]$Ctx           = 4096,
    [int]$IoThreads     = 8,
    [string]$Prompt     = 'C:\Users\robin\dev\Crow\tools\prompts\probe-f-coding.txt',
    [string]$Model      = 'C:\Users\robin\dev\crow-lab\models\DeepSeek-V4-Flash-MXFP4.gguf',
    [string]$Bin        = 'C:\Users\robin\dev\crow-lab\src\build-native\bin\Release',
    [string]$SelfTest   = ''
)

$inv = [System.Globalization.CultureInfo]::InvariantCulture
function Fmt([double]$v, [int]$d = 2) { return $v.ToString("F$d", $inv) }

# Returns a hashtable: Ok, Reason, and the counters. The .err.txt is UTF-16 when written by
# PowerShell redirection - Get-Content handles that, grep does not.
function Read-Run([string]$path) {
    if (-not (Test-Path $path)) { return @{ Ok = $false; Reason = "no such file: $path" } }
    $t = Get-Content $path -Raw
    if ($null -eq $t) { $t = '' }

    $m = [regex]::Match($t, 'alloc_bufs:\s*(\S+)\s+expert cache size\s*=\s*([\d.]+) MiB \((\d+) slots')
    if (-not $m.Success) {
        return @{ Ok = $false; Reason = 'no alloc_bufs line - cannot tell where the cache landed' }
    }
    $s = [regex]::Match($t, 'moe stream: remap calls = (\d+), expert hits = (\d+), misses = (\d+) \((\d+) cold\)')
    if (-not $s.Success) {
        return @{ Ok = $false; Reason = 'no print_stats line - the run is invalid rather than empty' }
    }
    $w = [regex]::Match($t, 'moe stream: waves = (\d+) \((\d+) non-empty\), preloads issued = (\d+) \(ready on arrival = (\d+)\)')
    $l = [regex]::Match($t, 'moe stream: load stall = ([\d.]+) ms')
    $pe = [regex]::Match($t, 'prompt eval time =\s*([\d.]+) ms')
    $ev = [regex]::Match($t, 'eval time =\s*([\d.]+) ms /\s*(\d+) runs')

    return @{
        Ok         = $true
        Placement  = $m.Groups[1].Value
        CacheMiB   = [double]::Parse($m.Groups[2].Value, $inv)
        SlotsSeen  = [int]$m.Groups[3].Value
        Remap      = [int]$s.Groups[1].Value
        Hits       = [int]$s.Groups[2].Value
        Misses     = [int]$s.Groups[3].Value
        Cold       = [int]$s.Groups[4].Value
        Waves      = if ($w.Success) { [int]$w.Groups[1].Value } else { 0 }
        Preloads   = if ($w.Success) { [int]$w.Groups[3].Value } else { 0 }
        Ready      = if ($w.Success) { [int]$w.Groups[4].Value } else { 0 }
        LoadStall  = if ($l.Success) { [double]::Parse($l.Groups[1].Value, $inv) } else { 0 }
        PromptMs   = if ($pe.Success) { [double]::Parse($pe.Groups[1].Value, $inv) } else { 0 }
        EvalMs     = if ($ev.Success) { [double]::Parse($ev.Groups[1].Value, $inv) } else { 0 }
        EvalRuns   = if ($ev.Success) { [int]$ev.Groups[2].Value } else { 0 }
    }
}

# The buffer type the log must name for a given request. 'host' may legitimately degrade to
# plain CPU when the system refuses that much pinned memory - measured at 88 slots, 48 GB -
# so both are accepted for host, and the actual value is written to the CSV either way.
function Test-Placement([string]$requested, [string]$seen) {
    if ($requested -eq 'cuda0') { return $seen -eq 'CUDA0' }
    return ($seen -eq 'CUDA_Host') -or ($seen -eq 'CPU')
}

# The guard that decides the whole sweep. It lives in a function rather than inline so the
# self test can reach it - an inline guard is a guard nobody ever saw fail.
function Test-SameWork([int[]]$remapCounts) {
    $u = $remapCounts | Sort-Object -Unique
    return @{ Ok = ($u.Count -eq 1); Values = $u }
}

# --- self test ----------------------------------------------------------------------
if ($SelfTest -ne '') {
    Write-Output "=== SELFTEST: accept one real run, refuse three broken ones ==="
    $fail = 0

    $good = Read-Run $SelfTest
    if ($good.Ok) {
        Write-Output "  ok    a real run parses: $($good.Remap) remap calls, $($good.Placement), $($good.SlotsSeen) slots"
    } else {
        Write-Output "  FAIL  a real run did not parse: $($good.Reason)"; $fail = 1
    }

    $tmp = Join-Path $env:TEMP 'cache-sweep-selftest'
    if (-not (Test-Path $tmp)) { New-Item -ItemType Directory -Path $tmp | Out-Null }

    # Broken 1: no print_stats. Must be invalid, not "0 hits".
    $b1 = Join-Path $tmp 'no-stats.txt'
    'alloc_bufs:        CUDA0 expert cache size = 21930.00 MiB (40 slots per layer)' | Set-Content $b1 -Encoding utf8
    $r1 = Read-Run $b1
    if (-not $r1.Ok) { Write-Output "  ok    a run without print_stats is refused: $($r1.Reason)" }
    else { Write-Output "  FAIL  a run without print_stats parsed"; $fail = 1 }

    # Broken 2: no alloc_bufs. Placement would otherwise be assumed rather than read.
    $b2 = Join-Path $tmp 'no-placement.txt'
    'print_stats: moe stream: remap calls = 11008, expert hits = 47995, misses = 22545 (8274 cold), hit rate = 68.04%' | Set-Content $b2 -Encoding utf8
    $r2 = Read-Run $b2
    if (-not $r2.Ok) { Write-Output "  ok    a run without alloc_bufs is refused: $($r2.Reason)" }
    else { Write-Output "  FAIL  a run without alloc_bufs parsed"; $fail = 1 }

    # Broken 3: the placement guard itself. A host request landing on CUDA0 is the failure
    # that looks like success - it is what the override bug did before 2026-08-03.
    if (-not (Test-Placement 'host' 'CUDA0')) { Write-Output "  ok    a host request landing on CUDA0 is rejected" }
    else { Write-Output "  FAIL  a host request landing on CUDA0 was accepted"; $fail = 1 }
    if (Test-Placement 'host' 'CUDA_Host') { Write-Output "  ok    a host request landing on CUDA_Host is accepted" }
    else { Write-Output "  FAIL  a correct host arm was rejected"; $fail = 1 }

    # Broken 4: the sweep-wide guard. Arms that did different work must void the sweep, not
    # average into a difference. This is the one that decides every figure the sweep prints.
    $w1 = Test-SameWork @(11008, 11008, 11008)
    if ($w1.Ok) { Write-Output "  ok    equal remap counts pass the same-work guard" }
    else { Write-Output "  FAIL  equal remap counts were rejected"; $fail = 1 }
    $w2 = Test-SameWork @(11008, 11008, 10752)
    if (-not $w2.Ok) { Write-Output "  ok    differing remap counts void the sweep ($($w2.Values -join ', '))" }
    else { Write-Output "  FAIL  differing remap counts passed - every printed difference would be meaningless"; $fail = 1 }

    Write-Output ""
    if ($fail -eq 0) { Write-Output 'RESULT: PASS'; exit 0 }
    Write-Output 'RESULT: FAIL - the parser cannot tell a good run from a broken one'; exit 1
}

# --- sweep --------------------------------------------------------------------------
if ($OutDir -eq '') { Write-Output 'ERROR: -OutDir is required'; exit 1 }
if (-not (Test-Path $Prompt)) { Write-Output "ERROR: prompt not found: $Prompt"; exit 1 }
$exe = Join-Path $Bin 'llama-completion.exe'
if (-not (Test-Path $exe)) { Write-Output "ERROR: binary not found: $exe"; exit 1 }
New-Item -ItemType Directory -Force $OutDir | Out-Null

$rows = New-Object System.Collections.Generic.List[object]
$void = New-Object System.Collections.Generic.List[string]
$total = $Slots.Count * $Placements.Count * $Repeats
$n = 0

foreach ($p in $Placements) {
    foreach ($s in $Slots) {
        for ($r = 1; $r -le $Repeats; $r++) {
            $n++
            $tag = "$p-${s}s-r$r"
            $o = Join-Path $OutDir $tag
            $args = @(
                '-m', $Model, '-f', $Prompt, '-c', $Ctx, '-n', $Predict,
                '--temp', '0', '--seed', '1234', '-ngl', '99', '-lv', '4', '-no-cnv'
            )
            if ($p -eq 'host') { $args += @('-ncmoe', '999') }
            $args += @('--moe-stream', '--moe-stream-cache', "${s}s",
                       '--moe-stream-io-threads', $IoThreads, '--moe-stream-direct')

            Write-Output "[$n/$total] $tag ..."
            $sw = [Diagnostics.Stopwatch]::StartNew()
            & $exe @args 1> "$o.out.txt" 2> "$o.err.txt"
            $sw.Stop()

            $run = Read-Run "$o.err.txt"
            if (-not $run.Ok) {
                $void.Add("$tag : $($run.Reason)")
                Write-Output "      INVALID: $($run.Reason)"
                continue
            }
            if (-not (Test-Placement $p $run.Placement)) {
                $void.Add("$tag : requested $p, landed on $($run.Placement)")
                Write-Output "      INVALID: requested $p, landed on $($run.Placement)"
                continue
            }
            $hr = if (($run.Hits + $run.Misses) -gt 0) { 100.0 * $run.Hits / ($run.Hits + $run.Misses) } else { 0 }
            $run['Tag'] = $tag; $run['Requested'] = $p; $run['SlotsAsked'] = $s
            $run['Repeat'] = $r; $run['HitRate'] = $hr; $run['WallS'] = $sw.Elapsed.TotalSeconds
            $rows.Add([pscustomobject]$run)
            Write-Output ("      {0}, {1} slots, hit {2} %, eval {3} ms, {4} s wall" -f `
                $run.Placement, $run.SlotsSeen, (Fmt $hr), (Fmt $run.EvalMs 1), [int]$sw.Elapsed.TotalSeconds)
        }
    }
}

Write-Output ''
if ($rows.Count -eq 0) { Write-Output 'RESULT: VOID - no arm produced a usable run'; exit 1 }

# The guard that decides the whole sweep: same work in every arm.
$work = Test-SameWork ($rows | ForEach-Object { $_.Remap })
if (-not $work.Ok) {
    Write-Output "RESULT: VOID - remap calls differ across arms ($($work.Values -join ', '))."
    Write-Output '  The arms did different work, so no difference between them is readable.'
    exit 1
}
Write-Output "Guard: remap calls = $($work.Values[0]) in all $($rows.Count) arms - same work."

$csv = Join-Path $OutDir 'cache-sweep.csv'
'tag,requested,placement,slots,repeat,hits,misses,cold,hit_rate,waves,preloads,ready,load_stall_ms,prompt_ms,eval_ms,eval_runs,wall_s' |
    Set-Content $csv -Encoding utf8
foreach ($x in $rows) {
    ('{0},{1},{2},{3},{4},{5},{6},{7},{8},{9},{10},{11},{12},{13},{14},{15},{16}' -f `
        $x.Tag, $x.Requested, $x.Placement, $x.SlotsSeen, $x.Repeat, $x.Hits, $x.Misses, $x.Cold,
        (Fmt $x.HitRate), $x.Waves, $x.Preloads, $x.Ready, (Fmt $x.LoadStall 2), (Fmt $x.PromptMs 2),
        (Fmt $x.EvalMs 2), $x.EvalRuns, (Fmt $x.WallS 1)) | Add-Content $csv -Encoding utf8
}
Write-Output "CSV: $csv"

Write-Output ''
Write-Output 'placement  slots  hit%    eval ms    prompt ms   spread'
foreach ($p in $Placements) {
    foreach ($s in $Slots) {
        $g = $rows | Where-Object { $_.Requested -eq $p -and $_.SlotsAsked -eq $s }
        if ($g.Count -eq 0) { continue }
        $ev = ($g | Measure-Object EvalMs -Average).Average
        $pm = ($g | Measure-Object PromptMs -Average).Average
        $sp = if ($g.Count -gt 1) {
            $mx = ($g | Measure-Object EvalMs -Maximum).Maximum
            $mn = ($g | Measure-Object EvalMs -Minimum).Minimum
            "{0} %" -f (Fmt (100.0 * ($mx - $mn) / $mn) 1)
        } else { 'single run' }
        Write-Output ("{0,-10} {1,5}  {2,6}  {3,9}  {4,10}   {5}" -f `
            $g[0].Placement, $s, (Fmt ($g | Measure-Object HitRate -Average).Average), (Fmt $ev 1), (Fmt $pm 1), $sp)
    }
}

if ($void.Count -gt 0) {
    Write-Output ''
    Write-Output "RESULT: PARTIAL - $($rows.Count) of $total arms usable, $($void.Count) void:"
    foreach ($v in $void) { Write-Output "  $v" }
    exit 1
}
Write-Output ''
Write-Output "RESULT: PASS - $($rows.Count) of $total arms usable"
exit 0
