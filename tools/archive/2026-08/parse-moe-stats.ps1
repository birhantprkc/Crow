<#
parse-moe-stats - read the MoE expert streaming statistics out of a llama-server log,
keep the two model roles apart, and turn cumulative counters into per-request figures.

WHY THIS EXISTS. Until #54 the counters were printed by llama-completion only, where one
process holds one streaming model and a block needs no label. The server holds two - target
and draft model own one manager each - and prints both after every finished request. That
turns reading the log into three questions a grep cannot answer:

    which model does this block belong to, which request does it belong to, and what did
    THIS request cost when the numbers are running totals?

WHAT IT DOES NOT DO, deliberately: it never resets anything and it never asks the server to.
The counters are cumulative per model instance and stay that way; the per-request figure is
the difference between two consecutive blocks, formed here, by the reader. A restarted server
starts at zero because the manager is new - that is not a reset and the tool does not treat a
drop to zero as one.

THE WARM-UP, stated because it is the one thing a reader gets wrong. The library warm-up does
not run at all under expert streaming (common_init_from_params switches params.warmup off when
moe_stream is on), so nothing pollutes the counters before the first request. The warm-up of a
MEASUREMENT SERIES is a discarded REQUEST, and its numbers ARE contained in every later block.
Subtracting it is the caller's job and this tool is what makes it possible: -Offset skips the
bytes written before the measurement began, and the first request inside the region carries no
delta because there is nothing to subtract it from.

TRAPS THIS TOOL IS BUILT AROUND, all of them measured on this machine:
  - [double]::Parse('68.17') on a de-DE machine yields 6817. Every number here goes through
    To-Num with InvariantCulture. A hit rate of 6817 % would have looked like a parser bug in
    the SERVER.
  - llama-server holds its log open, so a plain read throws and used to leave an EMPTY array
    behind - which reads exactly like "no blocks". Opened with FileShare::ReadWrite, decoded
    with replacement fallback: one raw 0xef in a -lv 5 log made strict utf-8 AND utf-16 fail.
  - a zero is not a finding without its denominator. Every "no blocks" answer is classified,
    not reported bare: a log with the streaming load line but no statistics lines is
    HIDDEN BY VERBOSITY (library INFO needs -lv 4 or higher), which is a different fact
    from streaming being off, and both are different from the server never having run.
  - a block is not a line count. The waves line prints only when the wave path ran and the
    locality lines only when a layer saw traffic, so 3 to 7 lines are all legal and a fixed
    expectation would be red on a correct run. Completeness is decided on the three
    unconditional lines instead.

Usage:
  parse-moe-stats.ps1 -Selftest
  parse-moe-stats.ps1 -Log <server.err>
  parse-moe-stats.ps1 -Log <server.err> -Offset 12345 -Json

Exit 0 = parsed (or every self-test case green).  1 = a case failed / problems found.
2 = setup error.
#>
param(
    [string]$Log     = '',
    [long]  $Offset  = 0,
    # what the CALLER knows about --moe-stream, which the log cannot always show; see
    # Get-RegionKind. 'unknown' keeps the tool honest when nobody knows.
    [ValidateSet('unknown', 'on', 'off')][string]$StreamingHint = 'unknown',
    [switch]$Json,
    [switch]$Selftest
)

$ErrorActionPreference = 'Continue'

# Every function is defined before its first use. A function called above its definition does
# not exist in a script file; the call fails non-terminally, the result stays $null, and
# $null.Count is 0 - which is how a suite once reported "4 of 4 cases green" without running a
# single case.

function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }

# InvariantCulture, always. The log writes 68.17 with a dot; the machine's culture may read a
# dot as a thousands separator and hand back 6817.
function To-Num([string]$s) {
    if ($null -eq $s -or $s -eq '') { return $null }
    $v = 0.0
    $ok = [double]::TryParse($s, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$v)
    if ($ok) { return $v }
    return $null
}
function To-Int([string]$s) {
    if ($null -eq $s -or $s -eq '') { return $null }
    $v = 0L
    $ok = [long]::TryParse($s, [Globalization.NumberStyles]::Integer, [Globalization.CultureInfo]::InvariantCulture, [ref]$v)
    if ($ok) { return $v }
    return $null
}

# The server keeps the log open; see the header. end is returned so a caller can measure one
# region and continue behind it without re-reading what it already parsed.
function Read-LogTail([string]$Path, [long]$Offset) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ ok = $false; text = ''; why = "log missing: $Path"; end = $Offset }
    }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    } catch {
        return [pscustomobject]@{ ok = $false; text = ''; why = ("cannot open log: " + $_.Exception.Message); end = $Offset }
    }
    try {
        $len = $fs.Length
        if ($Offset -gt $len) {
            return [pscustomobject]@{ ok = $false; text = ''; why = "offset $Offset beyond length $len"; end = $len }
        }
        [void]$fs.Seek($Offset, [IO.SeekOrigin]::Begin)
        $n = [int]($len - $Offset)
        $buf = New-Object byte[] $n
        $got = 0
        while ($got -lt $n) {
            $r = $fs.Read($buf, $got, $n - $got)
            if ($r -le 0) { break }
            $got += $r
        }
        $enc = [Text.Encoding]::GetEncoding('utf-8', [Text.EncoderFallback]::ReplacementFallback, [Text.DecoderFallback]::ReplacementFallback)
        return [pscustomobject]@{ ok = $true; text = $enc.GetString($buf, 0, $got); why = ''; end = ($Offset + $got) }
    } finally {
        $fs.Close()
    }
}

function Get-LogLength([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try { return [long]$fs.Length } finally { $fs.Close() }
    } catch { return -1 }
}

function New-Block() {
    return [pscustomobject]@{
        role = ''; line0 = -1; lines = 0; bytes = 0
        n_calls = $null; n_hit = $null; n_miss = $null; n_miss_cold = $null; hit_rate = $null
        t_stall_ms = $null; t_stall_per_call_ms = $null
        t_victim_ms = $null; n_victim_waits = $null; victim_pct = $null
        n_wave_calls = $null; n_waves_run = $null; n_preload_issued = $null
        n_preload_ready = $null; t_stall_wave_ms = $null
        loc_layers = $null; loc_experts = $null; cov50 = $null; cov80 = $null; cov95 = $null; gini = $null
        has_remap = $false; has_stall = $false; has_wait = $false
        complete = $false
    }
}

# One line of a statistics block. Two things here are deliberate.
#
# TWO FUNCTION NAMES, not one. The block is written by two functions and %s carries __func__,
# so the locality lines arrive as "print_locality:" while the rest arrive as "print_stats:".
# Matching only the latter dropped three lines per block and ended the block early: measured
# 2026-08-06, a block reported as 4 lines / 494 B was really 7 lines / 921 B. The count of
# blocks and their completeness were unaffected - which is exactly why it survived a green
# run - but every volume figure was ~45 % too small.
#
# AN ALLOWLIST, not \w+. A generic prefix would also match prose like
# "srv note: the moe stream: counters are cumulative", and a log line that merely mentions the
# words is not a measurement.
#
# The role group stays OPTIONAL: llama-completion and llama-tts print unlabelled blocks and
# must keep parsing, otherwise this tool would report "no blocks" for every log written
# before #54.
$RX_LINE = '^(?<pre>.*?)(?<func>print_stats|print_locality):\s+(?:(?<role>[A-Za-z][A-Za-z0-9_-]*):\s+)?moe stream:\s*(?<body>.*)$'

function Parse-MoeStats([string]$Text) {
    $problems = @()
    $blocks   = @()
    $cur      = $null
    $moeLines = 0
    # Counted separately because "unlabelled" means two different things depending on who wrote
    # the line: legitimate for llama-completion, a defect for the server. Only the caller knows
    # which log it is holding, so the number is reported rather than judged here.
    $unlabelled = 0
    $lines    = @()
    if ($null -ne $Text -and $Text -ne '') { $lines = $Text -split "`r?`n" }

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $ln = $lines[$i]
        $m = [regex]::Match($ln, $RX_LINE)
        if (-not $m.Success) {
            # A non-statistics line ends the current block. Two blocks are printed back to
            # back, so anything in between means the block was cut off - which is exactly the
            # truncation case that must not be counted as a measurement.
            if ($null -ne $cur) { $blocks += $cur; $cur = $null }
            continue
        }
        $moeLines++
        $role = $m.Groups['role'].Value
        $body = $m.Groups['body'].Value
        if ($role -eq '') { $unlabelled++ }

        $isStart = $body -match '^remap calls\s*='
        if ($isStart) {
            if ($null -ne $cur) { $blocks += $cur }
            $cur = New-Block
            $cur.role  = $role
            $cur.line0 = $i
        }
        if ($null -eq $cur) {
            # a statistics line without its opening line: the head of the block is missing
            $problems += "line $i is a statistics line outside a block (block head missing)"
            continue
        }
        if ($role -ne $cur.role) {
            $problems += ("line {0} carries role '{1}' inside a '{2}' block" -f $i, $role, $cur.role)
        }
        $cur.lines++
        $cur.bytes += $ln.Length

        $mm = [regex]::Match($body, 'remap calls\s*=\s*(-?\d+),\s*expert hits\s*=\s*(-?\d+),\s*misses\s*=\s*(-?\d+)\s*\((-?\d+)\s*cold\),\s*hit rate\s*=\s*([-\d.]+)%')
        if ($mm.Success) {
            $cur.has_remap   = $true
            $cur.n_calls     = To-Int $mm.Groups[1].Value
            $cur.n_hit       = To-Int $mm.Groups[2].Value
            $cur.n_miss      = To-Int $mm.Groups[3].Value
            $cur.n_miss_cold = To-Int $mm.Groups[4].Value
            $cur.hit_rate    = To-Num $mm.Groups[5].Value
            continue
        }
        $mm = [regex]::Match($body, 'load stall\s*=\s*([-\d.]+) ms total\s*\(([-\d.]+) ms per remap call\)')
        if ($mm.Success) {
            $cur.has_stall           = $true
            $cur.t_stall_ms          = To-Num $mm.Groups[1].Value
            $cur.t_stall_per_call_ms = To-Num $mm.Groups[2].Value
            continue
        }
        $mm = [regex]::Match($body, 'slot wait\s*=\s*([-\d.]+) ms total over\s*(-?\d+) waits\s*\(([-\d.]+)% of the two stalls\)')
        if ($mm.Success) {
            $cur.has_wait        = $true
            $cur.t_victim_ms     = To-Num $mm.Groups[1].Value
            $cur.n_victim_waits  = To-Int $mm.Groups[2].Value
            $cur.victim_pct      = To-Num $mm.Groups[3].Value
            continue
        }
        $mm = [regex]::Match($body, 'waves\s*=\s*(-?\d+)\s*\((-?\d+) non-empty\), preloads issued\s*=\s*(-?\d+)\s*\(ready on arrival\s*=\s*(-?\d+)\), wave stall\s*=\s*([-\d.]+) ms')
        if ($mm.Success) {
            $cur.n_wave_calls     = To-Int $mm.Groups[1].Value
            $cur.n_waves_run      = To-Int $mm.Groups[2].Value
            $cur.n_preload_issued = To-Int $mm.Groups[3].Value
            $cur.n_preload_ready  = To-Int $mm.Groups[4].Value
            $cur.t_stall_wave_ms  = To-Num $mm.Groups[5].Value
            continue
        }
        $mm = [regex]::Match($body, 'expert locality over\s*(-?\d+) layers,\s*(-?\d+) experts each')
        if ($mm.Success) {
            $cur.loc_layers  = To-Int $mm.Groups[1].Value
            $cur.loc_experts = To-Int $mm.Groups[2].Value
            continue
        }
        $mm = [regex]::Match($body, '50% of selections covered by\s*([-\d.]+)% of experts, 80% by\s*([-\d.]+)%, 95% by\s*([-\d.]+)%')
        if ($mm.Success) {
            $cur.cov50 = To-Num $mm.Groups[1].Value
            $cur.cov80 = To-Num $mm.Groups[2].Value
            $cur.cov95 = To-Num $mm.Groups[3].Value
            continue
        }
        $mm = [regex]::Match($body, 'Gini\s*=\s*([-\d.]+)')
        if ($mm.Success) {
            $cur.gini = To-Num $mm.Groups[1].Value
            continue
        }
        $problems += "line $i is a statistics line the parser does not know: $body"
    }
    if ($null -ne $cur) { $blocks += $cur }

    # Completeness is decided on the three UNCONDITIONAL lines. waves and locality are printed
    # only when their path ran, so requiring them would be red on a correct run.
    foreach ($b in $blocks) {
        $b.complete = ($b.has_remap -and $b.has_stall -and $b.has_wait)
    }

    return [pscustomobject]@{
        blocks          = @($blocks)
        moe_lines       = $moeLines
        unlabelled_lines = $unlabelled
        problems        = @($problems)
    }
}

# Grouping. print_timings prints the target block and then, only if a draft model exists, the
# drafter block - so a new request begins at every block that is not a continuation of the
# current group's roles. The ORDER is not treated as a contract; the role token is.
function Group-Requests([object[]]$Blocks) {
    $requests = @()
    $problems = @()
    $cur = $null
    foreach ($b in $Blocks) {
        $startsNew = $false
        if ($null -eq $cur) {
            $startsNew = $true
        } elseif ($b.role -eq 'target') {
            $startsNew = $true
        } elseif ($cur.roles -contains $b.role) {
            $startsNew = $true
        }
        if ($startsNew) {
            if ($null -ne $cur) { $requests += $cur }
            $cur = [pscustomobject]@{ index = $requests.Count; roles = @(); blocks = @() }
        }
        if ($cur.roles -contains $b.role) {
            $problems += ("request {0} carries role '{1}' twice" -f $cur.index, $b.role)
        }
        $cur.roles  += $b.role
        $cur.blocks += $b
    }
    if ($null -ne $cur) { $requests += $cur }
    return [pscustomobject]@{ requests = @($requests); problems = @($problems) }
}

# The point of the whole tool: cumulative in, per request out. The first request of a region
# carries no delta - there is nothing to subtract it from, and inventing one would silently
# turn a running total into a first measurement.
function Get-Deltas([object[]]$Requests) {
    $rows = @()
    $problems = @()
    $prev = @{}
    foreach ($r in $Requests) {
        foreach ($b in $r.blocks) {
            $key = $b.role
            $row = [pscustomobject]@{
                request = $r.index; role = $key
                n_calls = $b.n_calls; n_hit = $b.n_hit; n_miss = $b.n_miss; n_miss_cold = $b.n_miss_cold
                t_stall_ms = $b.t_stall_ms; t_victim_ms = $b.t_victim_ms
                # read since the first version, handed out since #53 needed the wait side of the
                # question and not only the miss side
                n_victim_waits = $b.n_victim_waits
                n_wave_calls = $b.n_wave_calls; n_waves_run = $b.n_waves_run
                n_preload_issued = $b.n_preload_issued; n_preload_ready = $b.n_preload_ready
                t_stall_wave_ms = $b.t_stall_wave_ms
                d_calls = $null; d_hit = $null; d_miss = $null; d_miss_cold = $null
                d_stall_ms = $null; d_victim_ms = $null
                d_victim_waits = $null
                d_wave_calls = $null; d_waves_run = $null
                d_preload_issued = $null; d_preload_ready = $null; d_stall_wave_ms = $null
                complete = $b.complete
            }
            if ($prev.ContainsKey($key)) {
                $p = $prev[$key]
                $row.d_calls     = $b.n_calls     - $p.n_calls
                $row.d_hit       = $b.n_hit       - $p.n_hit
                $row.d_miss      = $b.n_miss      - $p.n_miss
                $row.d_miss_cold = $b.n_miss_cold - $p.n_miss_cold
                $row.d_stall_ms  = $b.t_stall_ms  - $p.t_stall_ms
                $row.d_victim_ms = $b.t_victim_ms - $p.t_victim_ms

                # The waves line prints ONLY when the wave path ran, so these are legally absent
                # on a correct run. A difference is formed only when BOTH sides carry a value:
                # $null - 5 is -5 in PowerShell, which would arrive below as a counter running
                # backwards and be reported as a fault that never happened.
                foreach ($pair in @(
                        @('d_victim_waits',   'n_victim_waits'),
                        @('d_wave_calls',     'n_wave_calls'),
                        @('d_waves_run',      'n_waves_run'),
                        @('d_preload_issued', 'n_preload_issued'),
                        @('d_preload_ready',  'n_preload_ready'),
                        @('d_stall_wave_ms',  't_stall_wave_ms'))) {
                    $dst = $pair[0]; $src = $pair[1]
                    if ($null -ne $b.$src -and $null -ne $p.$src) { $row.$dst = $b.$src - $p.$src }
                }

                foreach ($f in @('d_calls', 'd_hit', 'd_miss', 'd_miss_cold', 'd_stall_ms', 'd_victim_ms',
                                 'd_victim_waits', 'd_wave_calls', 'd_waves_run',
                                 'd_preload_issued', 'd_preload_ready', 'd_stall_wave_ms')) {
                    if ($null -ne $row.$f -and $row.$f -lt 0) {
                        # A cumulative counter cannot fall. Rejected rather than reported as a
                        # delta: a negative difference means the blocks were mismatched or the
                        # server was restarted mid-region, and both make the number worthless.
                        $problems += ("request {0} role '{1}': {2} went backwards ({3})" -f $r.index, $key, $f, $row.$f)
                    }
                }
            }
            $prev[$key] = $b
            $rows += $row
        }
    }
    return [pscustomobject]@{ rows = @($rows); problems = @($problems) }
}

# A zero needs its denominator. "No blocks" has several different causes and they are
# different facts; collapsing them is how "the server never printed" becomes "streaming was
# off". FullText is the whole log, because the streaming load lines are written at startup and
# usually sit in front of the measured region.
#
# StreamingHint is not a convenience, it is what makes the verbosity verdict possible at all.
# The load lines are library INFO exactly like the statistics lines, so at the default
# verbosity BOTH are gone and the log alone cannot tell "streaming was off" from "streaming
# ran and nothing was printed". The caller knows - it wrote --moe-stream on the command line -
# and passing that in is the difference between a verdict and a guess. Without it the answer
# is 'no-streaming-evidence', which is what the log actually supports, and never
# 'streaming-off', which it does not.
function Get-RegionKind([string]$RegionText, [string]$FullText, [int]$BlockCount, [string]$StreamingHint = 'unknown') {
    if ($BlockCount -gt 0) { return 'blocks' }
    $sawServer = ($FullText -match 'srv ') -or ($FullText -match 'llama_server')
    if (-not $sawServer) { return 'no-server-output' }
    if ($StreamingHint -eq 'on') { return 'hidden-by-verbosity' }
    if ($StreamingHint -eq 'off') { return 'streaming-off' }
    $sawStreaming = ($FullText -match 'MoE expert streaming') -or ($FullText -match 'moe stream:')
    if ($sawStreaming) { return 'hidden-by-verbosity' }
    return 'no-streaming-evidence'
}

# --------------------------------------------------------------------------- self-test
function Invoke-Selftest {
    $script:cases = @()
    function Case([string]$name, [bool]$pass, [string]$detail) {
        $script:cases += [pscustomobject]@{ Case = $name; OK = $pass; Detail = $detail }
        Say ("  {0,-4} {1,-52} {2}" -f $(if ($pass) { 'ok' } else { 'RED' }), $name, $detail)
    }

    function Blk([string]$role, [int]$calls, [int]$hit, [int]$miss, [int]$cold, [double]$stall, [double]$wait, [int]$waits) {
        $r = ''
        if ($role -ne '') { $r = "$role" + ': ' }
        $rate = 0.0
        if (($hit + $miss) -gt 0) { $rate = 100.0 * $hit / ($hit + $miss) }
        $l1 = "0.01.000.001 I print_stats: {0}moe stream: remap calls = {1}, expert hits = {2}, misses = {3} ({4} cold), hit rate = {5}%" -f `
              $r, $calls, $hit, $miss, $cold, ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:F2}', $rate))
        $l2 = "0.01.000.002 I print_stats: {0}moe stream: load stall = {1} ms total (0.500 ms per remap call)" -f `
              $r, ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:F2}', $stall))
        $l3 = "0.01.000.003 I print_stats: {0}moe stream: slot wait = {1} ms total over {2} waits (10.0% of the two stalls)" -f `
              $r, ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:F2}', $wait)), $waits
        return ($l1, $l2, $l3) -join "`n"
    }

    Say 'PHASE selftest'

    # 1 - target only
    $t1 = (Blk 'target' 100 900 100 40 12.5 1.5 3) + "`n" + '0.01.000.004 I srv  update_slots: all slots are idle'
    $p1 = Parse-MoeStats $t1
    $g1 = Group-Requests $p1.blocks
    Case '1 target only: one block, one request' `
        ($p1.blocks.Count -eq 1 -and $g1.requests.Count -eq 1 -and ($g1.requests[0].roles -join ',') -eq 'target') `
        ("blocks={0} requests={1} roles={2}" -f $p1.blocks.Count, $g1.requests.Count, ($g1.requests[0].roles -join ','))
    Case '1b target only: no drafter block invented' `
        (-not ($p1.blocks.role -contains 'drafter')) ("roles seen: " + (($p1.blocks | ForEach-Object { $_.role }) -join ','))

    # 2 - target and drafter
    $t2 = (Blk 'target' 100 900 100 40 12.5 1.5 3) + "`n" + (Blk 'drafter' 30 200 60 20 4.0 0.5 1)
    $p2 = Parse-MoeStats $t2
    $g2 = Group-Requests $p2.blocks
    Case '2 target and drafter: two blocks, ONE request' `
        ($p2.blocks.Count -eq 2 -and $g2.requests.Count -eq 1 -and ($g2.requests[0].roles -join ',') -eq 'target,drafter') `
        ("blocks={0} requests={1} roles={2}" -f $p2.blocks.Count, $g2.requests.Count, ($g2.requests[0].roles -join ','))

    # 3 - the target role carries the target numbers
    $b3 = @($p2.blocks | Where-Object { $_.role -eq 'target' })[0]
    Case '3 role target: values belong to the target block' `
        ($b3.n_calls -eq 100 -and $b3.n_hit -eq 900 -and $b3.n_miss -eq 100 -and $b3.n_miss_cold -eq 40 -and $b3.complete) `
        ("calls={0} hit={1} miss={2} cold={3} complete={4}" -f $b3.n_calls, $b3.n_hit, $b3.n_miss, $b3.n_miss_cold, $b3.complete)

    # 4 - and the drafter role the drafter's, not the target's
    $b4 = @($p2.blocks | Where-Object { $_.role -eq 'drafter' })[0]
    Case '4 role drafter: values are NOT the target values' `
        ($b4.n_calls -eq 30 -and $b4.n_hit -eq 200 -and $b4.n_miss -eq 60 -and $b4.n_calls -ne $b3.n_calls) `
        ("calls={0} hit={1} miss={2}" -f $b4.n_calls, $b4.n_hit, $b4.n_miss)

    # 5 - several requests, cumulative
    $t5 = (Blk 'target' 100 900 100 40 12.5 1.5 3) + "`n" + (Blk 'drafter' 30 200 60 20 4.0 0.5 1) + "`n" +
          (Blk 'target' 250 2200 260 55 30.0 2.5 5) + "`n" + (Blk 'drafter' 70 500 140 25 9.0 0.8 2)
    $p5 = Parse-MoeStats $t5
    $g5 = Group-Requests $p5.blocks
    Case '5 two requests, counters cumulative and rising' `
        ($g5.requests.Count -eq 2 -and $p5.blocks.Count -eq 4 -and $g5.problems.Count -eq 0) `
        ("requests={0} blocks={1} problems={2}" -f $g5.requests.Count, $p5.blocks.Count, $g5.problems.Count)

    # 6 - and the request-local difference is the hand-computed one
    $d6 = Get-Deltas $g5.requests
    $r6t = @($d6.rows | Where-Object { $_.request -eq 1 -and $_.role -eq 'target' })[0]
    $r6d = @($d6.rows | Where-Object { $_.request -eq 1 -and $_.role -eq 'drafter' })[0]
    $r6first = @($d6.rows | Where-Object { $_.request -eq 0 -and $_.role -eq 'target' })[0]
    Case '6 request-local delta = difference of consecutive blocks' `
        ($r6t.d_calls -eq 150 -and $r6t.d_miss -eq 160 -and $r6d.d_calls -eq 40 -and $r6d.d_miss -eq 80 -and $d6.problems.Count -eq 0) `
        ("target d_calls={0} d_miss={1}; drafter d_calls={2} d_miss={3}" -f $r6t.d_calls, $r6t.d_miss, $r6d.d_calls, $r6d.d_miss)
    Case '6b first request carries NO delta (nothing to subtract from)' `
        ($null -eq $r6first.d_calls) ("d_calls={0}" -f $(if ($null -eq $r6first.d_calls) { 'null' } else { $r6first.d_calls }))

    # 7 - no block at all, and streaming was never on
    $t7 = "0.00.100.000 I srv  update_slots: all slots are idle`n0.00.200.000 I srv  log_server_r: request: POST /completion"
    $p7 = Parse-MoeStats $t7
    $k7 = Get-RegionKind $t7 $t7 $p7.blocks.Count
    Case '7 missing block without evidence is NOT called streaming-off' `
        ($p7.blocks.Count -eq 0 -and $k7 -eq 'no-streaming-evidence') ("blocks={0} kind={1}" -f $p7.blocks.Count, $k7)
    # POSITIVE CONTROL for case 7: the input was not empty and the parser did see the log
    Case '7b positive control: the same input carries server lines' `
        ($t7 -match 'srv ') 'srv marker present, so blocks=0 is a statement about the code'

    # 8 - no block, but streaming demonstrably loaded: that is VISIBILITY, not absence
    $t8 = "0.00.022.134 W llama_model_load: disabling mmap because MoE expert streaming is enabled (mmap -> none)`n" +
          "0.03.511.673 I llama_context: MoE expert streaming with 40 cache slots, n_ubatch = 512`n" +
          "0.10.000.000 I srv  update_slots: all slots are idle"
    $p8 = Parse-MoeStats $t8
    $k8 = Get-RegionKind $t8 $t8 $p8.blocks.Count
    Case '8 no block despite streaming = hidden-by-verbosity' `
        ($p8.blocks.Count -eq 0 -and $k8 -eq 'hidden-by-verbosity') ("blocks={0} kind={1}" -f $p8.blocks.Count, $k8)
    Case '8b the two zero-block cases are NOT the same verdict' `
        ($k7 -ne $k8) ("case7={0} case8={1}" -f $k7, $k8)

    # 8c - the case probe D actually produces: default verbosity hides the load lines TOO, so
    #      the log carries no streaming evidence at all and only the caller's command line can
    #      settle it. Without the hint this is 'no-streaming-evidence'; with it, verbosity.
    $t8c = "0.00.100.000 I srv  update_slots: all slots are idle`n0.00.200.000 I srv  log_server_r: request: POST /completion"
    $p8c = Parse-MoeStats $t8c
    $k8cNo   = Get-RegionKind $t8c $t8c $p8c.blocks.Count 'unknown'
    $k8cHint = Get-RegionKind $t8c $t8c $p8c.blocks.Count 'on'
    Case '8c streaming known on from the command line = hidden-by-verbosity' `
        ($k8cHint -eq 'hidden-by-verbosity' -and $k8cNo -eq 'no-streaming-evidence') `
        ("without hint={0} with hint={1}" -f $k8cNo, $k8cHint)

    # 9 - truncated block: head present, the rest cut off
    $t9 = "0.01.000.001 I print_stats: target: moe stream: remap calls = 100, expert hits = 900, misses = 100 (40 cold), hit rate = 90.00%"
    $p9 = Parse-MoeStats $t9
    Case '9 truncated block parses but is NOT complete' `
        ($p9.blocks.Count -eq 1 -and -not $p9.blocks[0].complete -and $p9.blocks[0].has_remap) `
        ("blocks={0} complete={1} has_remap={2}" -f $p9.blocks.Count, $p9.blocks[0].complete, $p9.blocks[0].has_remap)

    # 10 - the same role twice inside one request
    $t10 = (Blk 'target' 100 900 100 40 12.5 1.5 3) + "`n" + (Blk 'drafter' 30 200 60 20 4.0 0.5 1) + "`n" + (Blk 'drafter' 31 210 61 21 4.5 0.6 1)
    $p10 = Parse-MoeStats $t10
    $g10 = Group-Requests $p10.blocks
    Case '10 duplicate role does not silently merge into one request' `
        ($g10.requests.Count -eq 2 -and $p10.blocks.Count -eq 3) `
        ("requests={0} blocks={1}" -f $g10.requests.Count, $p10.blocks.Count)

    # 11 - an UNLABELLED block still parses. Without this the tool would report "no blocks"
    #      for every llama-completion log written before roles existed.
    $t11 = Blk '' 10 90 10 4 1.0 0.1 1
    $p11 = Parse-MoeStats $t11
    Case '11 unlabelled block (llama-completion) still parses' `
        ($p11.blocks.Count -eq 1 -and $p11.blocks[0].role -eq '' -and $p11.blocks[0].n_calls -eq 10) `
        ("blocks={0} role='{1}' calls={2}" -f $p11.blocks.Count, $p11.blocks[0].role, $p11.blocks[0].n_calls)

    # 12 - over-matching guard: prose mentioning the words is not a block
    $t12 = "0.00.100.000 I srv  note: the moe stream: counters are cumulative`n0.00.200.000 I main: moe stream statistics follow"
    $p12 = Parse-MoeStats $t12
    Case '12 prose mentioning the words is not counted as a block' `
        ($p12.blocks.Count -eq 0) ("blocks={0}" -f $p12.blocks.Count)

    # 13 - a counter running backwards is rejected, not reported as a delta
    $t13 = (Blk 'target' 250 2200 260 55 30.0 2.5 5) + "`n" + (Blk 'target' 100 900 100 40 12.5 1.5 3)
    $g13 = Group-Requests (Parse-MoeStats $t13).blocks
    $d13 = Get-Deltas $g13.requests
    Case '13 counter running backwards is reported as a problem' `
        ($d13.problems.Count -ge 1) ("problems={0}" -f $d13.problems.Count)

    # 14 - decimals do not go through the machine's culture
    Case '14 68.17 parses as 68.17, not 6817 (InvariantCulture)' `
        ((To-Num '68.17') -eq 68.17) ("got {0}" -f (To-Num '68.17'))

    # 15 - the waves and locality lines are optional and must not break completeness
    # The locality lines carry print_locality, not print_stats - that is what the server really
    # writes, because %s is __func__ and print_stats delegates to print_locality. A suite that
    # spelled them print_stats would have stayed green over the defect that dropped them.
    $t15 = (Blk 'target' 100 900 100 40 12.5 1.5 3) + "`n" +
           '0.01.000.004 I print_stats: target: moe stream: waves = 10 (7 non-empty), preloads issued = 20 (ready on arrival = 5), wave stall = 3.25 ms' + "`n" +
           '0.01.000.005 I print_locality: target: moe stream: expert locality over 40 layers, 256 experts each' + "`n" +
           '0.01.000.006 I print_locality: target: moe stream:   50% of selections covered by 12.5% of experts, 80% by 33.0%, 95% by 61.0%' + "`n" +
           '0.01.000.007 I print_locality: target: moe stream:   Gini = 0.412 (0 = flat, 1 = one expert takes everything)'
    $p15 = Parse-MoeStats $t15
    Case '15 seven-line block: waves and locality parsed, still one block' `
        ($p15.blocks.Count -eq 1 -and $p15.blocks[0].lines -eq 7 -and $p15.blocks[0].complete -and `
         $p15.blocks[0].n_waves_run -eq 7 -and $p15.blocks[0].gini -eq 0.412 -and $p15.blocks[0].cov50 -eq 12.5 -and `
         $p15.problems.Count -eq 0) `
        ("lines={0} waves_run={1} gini={2} problems={3}" -f $p15.blocks[0].lines, $p15.blocks[0].n_waves_run, $p15.blocks[0].gini, $p15.problems.Count)

    # 15b - the six counters #53 needs are differenced like the rest. A wave line on both
    #       requests, so every one of them has a defined delta.
    $wv = { param($w,$nr,$pi,$pr,$ws)
            "0.01.000.004 I print_stats: target: moe stream: waves = $w ($nr non-empty), preloads issued = $pi (ready on arrival = $pr), wave stall = $ws ms" }
    $t15b = (Blk 'target' 100 900 100 40 12.5 1.5 3)  + "`n" + (& $wv 10 7 20 5 '3.25') + "`n" +
            (Blk 'target' 250 2200 260 55 30.0 2.5 8) + "`n" + (& $wv 26 19 44 12 '9.75')
    $g15b = Group-Requests (Parse-MoeStats $t15b).blocks
    $d15b = Get-Deltas $g15b.requests
    $r15b = $d15b.rows[1]
    Case '15b wave, preload and slot-wait counters are differenced' `
        ($r15b.d_wave_calls -eq 16 -and $r15b.d_waves_run -eq 12 -and $r15b.d_preload_issued -eq 24 -and `
         $r15b.d_preload_ready -eq 7 -and $r15b.d_stall_wave_ms -eq 6.5 -and $r15b.d_victim_waits -eq 5 -and `
         $d15b.problems.Count -eq 0) `
        ("waves={0} run={1} pi={2} pr={3} wstall={4} waits={5} problems={6}" -f `
         $r15b.d_wave_calls, $r15b.d_waves_run, $r15b.d_preload_issued, $r15b.d_preload_ready, `
         $r15b.d_stall_wave_ms, $r15b.d_victim_waits, $d15b.problems.Count)

    # 15c - NEGATIVE CONTROL for 15b. Without a waves line the counters are absent, and absent
    #       must stay absent: $null - 5 is -5 in PowerShell, so a careless difference would
    #       invent a wave delta AND report the counter as running backwards. The slot-wait count
    #       stands beside it as a non-null, so a blanket "everything is null" cannot pass either.
    $t15c = (Blk 'target' 100 900 100 40 12.5 1.5 3) + "`n" + (Blk 'target' 250 2200 260 55 30.0 2.5 8)
    $g15c = Group-Requests (Parse-MoeStats $t15c).blocks
    $d15c = Get-Deltas $g15c.requests
    $r15c = $d15c.rows[1]
    Case '15c no waves line: wave deltas stay NULL, slot-wait delta does not' `
        ($null -eq $r15c.d_wave_calls -and $null -eq $r15c.d_waves_run -and `
         $null -eq $r15c.d_preload_issued -and $null -eq $r15c.d_stall_wave_ms -and `
         $r15c.d_victim_waits -eq 5 -and $d15c.problems.Count -eq 0) `
        ("wave_calls={0} waits={1} problems={2}" -f `
         $(if ($null -eq $r15c.d_wave_calls) { 'null' } else { $r15c.d_wave_calls }), `
         $r15c.d_victim_waits, $d15c.problems.Count)

    # 16 - the volume figures, per block rather than averaged. A single "lines per block" cannot
    #      say whether the drafter block or the target block is the large one.
    $t16 = (Blk 'target' 100 900 100 40 12.5 1.5 3) + "`n" + (Blk 'drafter' 30 200 60 20 4.0 0.5 1) + "`n" + (Blk '' 10 90 10 4 1.0 0.1 1)
    $p16 = Parse-MoeStats $t16
    $lens = @($p16.blocks | ForEach-Object { $_.lines })
    $byts = @($p16.blocks | ForEach-Object { $_.bytes })
    Case '16 per-block lines and bytes reported, unlabelled counted' `
        ($p16.blocks.Count -eq 3 -and ($lens -join ',') -eq '3,3,3' -and $p16.unlabelled_lines -eq 3 -and `
         (@($byts | Where-Object { $_ -gt 0 }).Count -eq 3)) `
        ("lines={0} bytes={1} unlabelled={2}" -f ($lens -join ','), ($byts -join ','), $p16.unlabelled_lines)
    Case '16b a labelled log reports ZERO unlabelled lines' `
        ((Parse-MoeStats $t2).unlabelled_lines -eq 0) ("unlabelled={0}" -f (Parse-MoeStats $t2).unlabelled_lines)

    # 17 - the defect that hid three lines per block: the locality lines belong to the SAME
    #      block even though they carry a different __func__. Measured against a real block
    #      taken verbatim from probeB.err on 2026-08-06 (7 target lines, 6 drafter lines).
    $t17 = @(
      '1.10.229.080 I print_stats: target: moe stream: remap calls = 1462, expert hits = 16974, misses = 5619 (3946 cold), hit rate = 75.13%'
      '1.10.229.081 I print_stats: target: moe stream: load stall = 5114.45 ms total (3.498 ms per remap call)'
      '1.10.229.082 I print_stats: target: moe stream: slot wait = 0.00 ms total over 0 waits (0.0% of the two stalls)'
      '1.10.229.083 I print_stats: target: moe stream: waves = 129 (86 non-empty), preloads issued = 378 (ready on arrival = 328), wave stall = 1066.41 ms'
      '1.10.229.200 I print_locality: target: moe stream: expert locality over 43 layers, 256 experts each'
      '1.10.229.202 I print_locality: target: moe stream:   50% of selections covered by 6.7% of experts, 80% by 16.0%, 95% by 27.4%'
      '1.10.229.202 I print_locality: target: moe stream:   Gini = 0.818 (0 = flat, 1 = one expert takes everything)'
      '1.10.229.203 I print_stats: drafter: moe stream: remap calls = 93, expert hits = 880, misses = 157 (157 cold), hit rate = 84.86%'
      '1.10.229.204 I print_stats: drafter: moe stream: load stall = 287.58 ms total (3.092 ms per remap call)'
      '1.10.229.204 I print_stats: drafter: moe stream: slot wait = 0.00 ms total over 0 waits (0.0% of the two stalls)'
      '1.10.229.212 I print_locality: drafter: moe stream: expert locality over 3 layers, 256 experts each'
      '1.10.229.212 I print_locality: drafter: moe stream:   50% of selections covered by 3.8% of experts, 80% by 9.0%, 95% by 16.3%'
      '1.10.229.213 I print_locality: drafter: moe stream:   Gini = 0.895 (0 = flat, 1 = one expert takes everything)'
    ) -join "`n"
    $p17 = Parse-MoeStats $t17
    $g17 = Group-Requests $p17.blocks
    $t17t = @($p17.blocks | Where-Object { $_.role -eq 'target' })[0]
    $t17d = @($p17.blocks | Where-Object { $_.role -eq 'drafter' })[0]
    Case '17 locality lines join their own block (real server block)' `
        ($p17.blocks.Count -eq 2 -and $t17t.lines -eq 7 -and $t17d.lines -eq 6 -and $g17.requests.Count -eq 1 -and `
         $t17t.gini -eq 0.818 -and $t17d.gini -eq 0.895 -and $t17t.loc_layers -eq 43 -and $t17d.loc_layers -eq 3) `
        ("blocks={0} target={1} lines drafter={2} lines requests={3} gini={4}/{5}" -f `
         $p17.blocks.Count, $t17t.lines, $t17d.lines, $g17.requests.Count, $t17t.gini, $t17d.gini)
    Case '17b prose is still not a block after widening the prefix' `
        ((Parse-MoeStats "0.00.100.000 I srv  note: the moe stream: counters are cumulative").blocks.Count -eq 0) `
        'a generic prefix would have matched "note:"'

    Say ('-' * 78)
    $bad = @($script:cases | Where-Object { -not $_.OK })
    Say ("selftest: {0} of {1} cases green" -f ($script:cases.Count - $bad.Count), $script:cases.Count)

    # THE FUNCTION EXITS, it does not return. A PowerShell function returns EVERYTHING it
    # wrote to the success stream, so `exit (Invoke-Selftest)` hands `exit` the whole Say
    # transcript with the status code buried at its end - measured here on 2026-08-06: the
    # suite printed nothing at all and reported EXIT=0 while a formatting error fired
    # fifteen times. A suite that cannot be red is worse than no suite.
    if ($script:cases.Count -eq 0) {
        Say 'selftest: NO CASE RAN - reporting red rather than an empty green'
        exit 1
    }
    if ($bad.Count -gt 0) {
        foreach ($b in $bad) { Say ("  RED  {0}: {1}" -f $b.Case, $b.Detail) }
        exit 1
    }
    exit 0
}

# --------------------------------------------------------------------------- main
if ($Selftest) { Invoke-Selftest }

if (-not $Log) { Die 'give -Log <server log> or -Selftest' }

$len = Get-LogLength $Log
$tail = Read-LogTail $Log $Offset
if (-not $tail.ok) { Die $tail.why }
$full = Read-LogTail $Log 0
if (-not $full.ok) { Die $full.why }

$parsed = Parse-MoeStats $tail.text
$grouped = Group-Requests $parsed.blocks
$deltas  = Get-Deltas $grouped.requests
$kind    = Get-RegionKind $tail.text $full.text $parsed.blocks.Count $StreamingHint

$allProblems = @($parsed.problems) + @($grouped.problems) + @($deltas.problems)

$blockLines = 0
$blockBytes = 0
foreach ($b in $parsed.blocks) { $blockLines += $b.lines; $blockBytes += $b.bytes }

$report = [pscustomobject]@{
    log             = $Log
    logBytes        = $len
    offset          = $Offset
    regionBytes     = ($tail.end - $Offset)
    kind            = $kind
    moeLines        = $parsed.moe_lines
    unlabelledLines = $parsed.unlabelled_lines
    # per block, so a caller can state volume per request and per role instead of one average
    blockDetail     = @($parsed.blocks | ForEach-Object {
                          [pscustomobject]@{ role = $_.role; lines = $_.lines; bytes = $_.bytes; complete = $_.complete }
                      })
    blocks          = $parsed.blocks.Count
    blocksComplete  = @($parsed.blocks | Where-Object { $_.complete }).Count
    requests        = $grouped.requests.Count
    roles           = @($parsed.blocks | ForEach-Object { $_.role } | Sort-Object -Unique)
    blockLinesTotal = $blockLines
    blockBytesTotal = $blockBytes
    linesPerBlockMin = $(if ($parsed.blocks.Count) { (@($parsed.blocks | ForEach-Object { $_.lines }) | Measure-Object -Minimum).Minimum } else { $null })
    linesPerBlockMax = $(if ($parsed.blocks.Count) { (@($parsed.blocks | ForEach-Object { $_.lines }) | Measure-Object -Maximum).Maximum } else { $null })
    problems        = $allProblems
    rows            = $deltas.rows
}

if ($Json) {
    $report | ConvertTo-Json -Depth 6
} else {
    Say ("log {0}  bytes {1}  region from {2} ({3} B)" -f (Split-Path $Log -Leaf), $len, $Offset, $report.regionBytes)
    Say ("kind {0}  moe lines {1}  blocks {2} ({3} complete)  requests {4}  roles [{5}]" -f `
         $kind, $parsed.moe_lines, $parsed.blocks.Count, $report.blocksComplete, $grouped.requests.Count, ($report.roles -join ','))
    Say ("block size: {0}-{1} lines, {2} lines and {3} B over all blocks" -f `
         $report.linesPerBlockMin, $report.linesPerBlockMax, $blockLines, $blockBytes)
    if ($deltas.rows.Count -gt 0) {
        $deltas.rows | Format-Table request, role, n_calls, n_miss, n_miss_cold, d_calls, d_miss, d_miss_cold, d_stall_ms, complete -AutoSize |
            Out-String -Width 200 | Write-Output
    }
    foreach ($p in $allProblems) { Say "  PROBLEM: $p" }
}

if ($allProblems.Count -gt 0) { exit 1 }
exit 0
