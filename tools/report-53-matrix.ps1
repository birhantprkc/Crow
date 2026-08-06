<#
report-53-matrix - join the #53 series artefact with the MoE stream blocks and print the matrix.

WHY THIS EXISTS. The harness records what the CLIENT and the process counters saw; the streaming
counters live in the server log. Neither half answers #53 alone: the question is whether decode
time and expert-stream waiting move together, and that needs the two joined per request.

THE JOIN. probe-runs.json carries (block, req_in_block); parse-moe-stats.ps1 numbers the blocks
inside one server log from 0, where 0 IS the discarded warm-up. So req_in_block r matches parser
request r, and the warm-up is skipped by construction rather than by an offset someone has to
remember. If a block file yields fewer requests than the artefact claims, that is a stop, not a
row to leave blank.

MEDIAN OF AN EVEN COUNT is the mean of the two middle values. Six requests per side means this is
the normal case here, not an edge case, and it has its own test below.

A FUNCTION IS EITHER AN OUTPUT FUNCTION OR A VALUE FUNCTION - the rule that cost two findings on
2026-08-06. Say writes to the host; every function that returns data narrates nothing.
#>
param(
    [string]$Series  = 'C:\Users\robin\dev\Crow\runs\2026-08-06\e53-series',
    [string]$Parser  = 'C:\Users\robin\dev\Crow\tools\parse-moe-stats.ps1',
    [switch]$Selftest
)

$ErrorActionPreference = 'Continue'
function Say([string]$m) { Write-Host $m }

function Get-Med([double[]]$xs) {
    if (-not $xs -or $xs.Count -eq 0) { return $null }
    $s = @($xs | Sort-Object)
    $n = $s.Count
    if ($n % 2 -eq 1) { return [double]$s[($n - 1) / 2] }
    return ([double]$s[$n/2 - 1] + [double]$s[$n/2]) / 2.0
}

function Get-Stat([double[]]$xs) {
    if (-not $xs -or $xs.Count -eq 0) {
        return [pscustomobject]@{ n = 0; med = $null; mean = $null; min = $null; max = $null; sd = $null }
    }
    $n    = $xs.Count
    $mean = ($xs | Measure-Object -Average).Average
    $sd   = 0.0
    if ($n -gt 1) {
        $ss = 0.0
        foreach ($x in $xs) { $ss += ($x - $mean) * ($x - $mean) }
        $sd = [math]::Sqrt($ss / ($n - 1))   # sample sd, n-1
    }
    return [pscustomobject]@{
        n = $n; med = (Get-Med $xs); mean = $mean
        min = ($xs | Measure-Object -Minimum).Minimum
        max = ($xs | Measure-Object -Maximum).Maximum
        sd  = $sd
    }
}

function Fmt([object]$v, [int]$dp) {
    if ($null -eq $v) { return 'null' }
    return [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:F$dp}", [double]$v)
}

if ($Selftest) {
    $n = 0; $bad = 0
    function Check([string]$name, $want, $got) {
        $script:n++
        $ok = ([string]$want -eq [string]$got)
        if (-not $ok) { $script:bad++ }
        Say ("  {0}  {1,-56} want={2} got={3}" -f $(if ($ok) { 'ok  ' } else { 'FAIL' }), $name, $want, $got)
    }
    Say 'PHASE selftest'
    Check 'median of an ODD count is the middle value'   3    (Get-Med @(1,3,9))
    Check 'median of an EVEN count is the mean of two middles' 4 (Get-Med @(1,3,5,9))
    Check 'median of six, unsorted input'                3.5  (Get-Med @(9,1,3,4,2,5))
    Check 'median is NOT the mean (they differ here)'    $true ((Get-Med @(1,2,90)) -ne (($1,2,90) | Measure-Object -Average).Average)
    $s = Get-Stat @(2,4,4,4,5,5,7,9)
    Check 'stat n'    8    $s.n
    Check 'stat mean' 5    $s.mean
    Check 'stat min'  2    $s.min
    Check 'stat max'  9    $s.max
    Check 'stat sd (sample, n-1)' '2.1381' (Fmt $s.sd 4)
    $e = Get-Stat @()
    Check 'empty input returns n=0, not 0.0' 0 $e.n
    Check 'empty input median is null, not zero' 'null' (Fmt $e.med 2)
    $one = Get-Stat @(5)
    Check 'single value: sd is 0, not an error' '0.0000' (Fmt $one.sd 4)
    Check 'InvariantCulture: 68.17 prints with a dot' '68.17' (Fmt 68.17 2)
    Say ('-' * 78)
    Say ("selftest: {0} of {1} cases green" -f ($n - $bad), $n)
    exit $(if ($bad -eq 0) { 0 } else { 1 })
}

# ---- load ------------------------------------------------------------------
$runsPath = Join-Path $Series 'probe-runs.json'
if (-not (Test-Path $runsPath)) { Say "no probe-runs.json under $Series"; exit 2 }
$runs = @((Get-Content $runsPath -Raw | ConvertFrom-Json))

# one parser pass per block log, cached by block number
$moe = @{}
foreach ($f in (Get-ChildItem $Series -Filter '*.err' | Sort-Object Name)) {
    if ($f.Name -notmatch '^block(\d+)-([AB])\.err$') { continue }
    $bn = [int]$Matches[1]
    $o = (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Parser -Log $f.FullName -StreamingHint on -Json | Out-String) | ConvertFrom-Json
    $moe[$bn] = $o
}

$rows = @()
$dropped = @()
foreach ($r in $runs) {
    $o = $moe[[int]$r.block]
    if ($null -eq $o) { $dropped += ("block {0}: no parsed log" -f $r.block); continue }
    $mr = @($o.rows | Where-Object { $_.request -eq $r.req_in_block -and $_.role -eq 'target' })
    if ($mr.Count -ne 1) {
        $dropped += ("block {0} request {1}: {2} matching target rows" -f $r.block, $r.req_in_block, $mr.Count)
        continue
    }
    $m = $mr[0]
    $rows += [pscustomobject]@{
        side = $r.side; block = $r.block; req = $r.req_in_block
        base = $r.base_tag; commit = $r.base_commit; pid_ = $r.server_pid
        sha_exe = ([string]$r.sha_exe).Substring(0,16)
        decode = [double]$r.decode_ms_tok; prefill = [double]$r.prefill_ms_tok
        prompt_sha = $r.prompt_sha; answer_sha = $r.answer_sha; src = $r.answer_source
        tokens = $r.completion; finish = $r.finish
        d_calls = $m.d_calls; d_hit = $m.d_hit; d_miss = $m.d_miss; d_cold = $m.d_miss_cold
        d_stall = $m.d_stall_ms; d_victim = $m.d_victim_ms; d_waits = $m.d_victim_waits
        d_wave_calls = $m.d_wave_calls; d_waves_run = $m.d_waves_run
        d_pre_issued = $m.d_preload_issued; d_pre_ready = $m.d_preload_ready
        d_wave_stall = $m.d_stall_wave_ms
        proc_read = $r.proc_read_delta; disk_read = $r.disk_read_delta
        gpu = $r.telemetry.gpu_util.med; memctl = $r.telemetry.mem_util.med
        vram = $r.telemetry.vram_mib.med; sm = $r.telemetry.sm_mhz.med
        pw = $r.telemetry.power_w.med; temp = $r.telemetry.temp_c.med
        samples = $r.telemetry.n; throttle = $r.telemetry.throttle_nonzero
        foreign = (@($r.foreign | ForEach-Object { "$($_.name):$($_.pid_)" }) -join ',')
    }
}

Say ''
Say ('=' * 118)
Say ("#53 MATRIX   {0} evaluated requests   dropped {1}" -f $rows.Count, $dropped.Count)
Say ('=' * 118)
foreach ($d in $dropped) { Say ("  DROPPED: {0}" -f $d) }

Say ''
Say ('{0,-4} {1,-3} {2,-3} {3,-8} {4,-6} {5,-8} {6,-8} {7,-7} {8,-6} {9,-7} {10,-7} {11,-6} {12,-11}' -f `
     'side','blk','req','base','pid','decode','prefill','tokens','fin','d_calls','d_miss','d_cold','d_stall_ms')
Say ('-' * 118)
foreach ($x in ($rows | Sort-Object side, block, req)) {
    Say ('{0,-4} {1,-3} {2,-3} {3,-8} {4,-6} {5,-8} {6,-8} {7,-7} {8,-6} {9,-7} {10,-7} {11,-6} {12,-11}' -f `
         $x.side, $x.block, $x.req, $x.base, $x.pid_, (Fmt $x.decode 3), (Fmt $x.prefill 2),
         $x.tokens, $x.finish, $x.d_calls, $x.d_miss, $x.d_cold, (Fmt $x.d_stall 2))
}

Say ''
Say 'constant across every evaluated request (a value that never varies is stated once, not twelve times):'
foreach ($f in @('prompt_sha','answer_sha','src','tokens','finish','d_calls','d_cold','d_victim','d_waits','d_wave_calls','d_waves_run','d_pre_issued','d_pre_ready','d_wave_stall','throttle','foreign')) {
    $vals = @($rows | ForEach-Object { [string]$_.$f } | Sort-Object -Unique)
    if ($vals.Count -eq 1) { Say ("  {0,-14} = {1}" -f $f, $vals[0]) }
    else { Say ("  {0,-14} VARIES: {1}" -f $f, ($vals -join ' | ')) }
}

# ---- per side ---------------------------------------------------------------
$METRICS = @(
    @('decode ms/token', 'decode', 3),
    @('prefill ms/token','prefill',3),
    @('d load stall ms', 'd_stall', 2),
    @('d hits',          'd_hit',   0),
    @('d misses',        'd_miss',  0),
    @('proc read bytes', 'proc_read', 0),
    @('gpu util %',      'gpu',     1),
    @('vram MiB',        'vram',    0),
    @('sm MHz',          'sm',      0),
    @('power W',         'pw',      1),
    @('temp C',          'temp',    0)
)
Say ''
Say ('{0,-17} {1,-2} {2,-13} {3,-13} {4,-13} {5,-13} {6,-11}' -f 'metric','','median','mean','min','max','sd')
Say ('-' * 118)
$summary = @{}
foreach ($mm in $METRICS) {
    $label = $mm[0]; $field = $mm[1]; $dp = [int]$mm[2]
    foreach ($side in 'A','B') {
        $xs = @($rows | Where-Object { $_.side -eq $side } | ForEach-Object { [double]$_.$field })
        $st = Get-Stat $xs
        $summary["$field-$side"] = $st
        Say ('{0,-17} {1,-2} {2,-13} {3,-13} {4,-13} {5,-13} {6,-11}' -f `
             $(if ($side -eq 'A') { $label } else { '' }), $side,
             (Fmt $st.med $dp), (Fmt $st.mean $dp), (Fmt $st.min $dp), (Fmt $st.max $dp), (Fmt $st.sd $dp))
    }
    $a = $summary["$field-A"]; $b = $summary["$field-B"]
    if ($null -ne $a.med -and $null -ne $b.med -and [double]$a.med -ne 0) {
        $abs = [double]$b.med - [double]$a.med
        $pct = 100.0 * $abs / [double]$a.med
        Say ('{0,-17} {1,-2} {2}' -f '', 'D', ("B - A = {0}  ({1} %)   sign: positive = B larger" -f (Fmt $abs $dp), (Fmt $pct 3)))
    }
    Say ''
}

Say 'single values, in block order:'
foreach ($side in 'A','B') {
    $xs = @($rows | Where-Object { $_.side -eq $side } | Sort-Object block, req)
    Say ("  {0} decode  {1}" -f $side, ((@($xs | ForEach-Object { Fmt $_.decode 3 })) -join ' , '))
    Say ("  {0} stall   {1}" -f $side, ((@($xs | ForEach-Object { Fmt $_.d_stall 2 })) -join ' , '))
    Say ("  {0} blocks  {1}" -f $side, ((@($xs | ForEach-Object { "b$($_.block)r$($_.req)" })) -join ' , '))
}

# decode against stall, both sides pooled and per side. Reported as a rank agreement rather than
# a fitted line: twelve points do not carry a regression, and the question is only whether the
# slower request is the one that waited longer.
Say ''
Say 'decode against load stall, sorted by decode (does the slower request wait longer?):'
foreach ($side in 'A','B') {
    $xs = @($rows | Where-Object { $_.side -eq $side } | Sort-Object decode)
    $stalls = @($xs | ForEach-Object { [double]$_.d_stall })
    $mono = $true
    for ($i = 1; $i -lt $stalls.Count; $i++) { if ($stalls[$i] -lt $stalls[$i-1]) { $mono = $false } }
    Say ("  {0}: decode {1}" -f $side, ((@($xs | ForEach-Object { Fmt $_.decode 2 })) -join ' < '))
    Say ("     stall  {0}" -f ((@($stalls | ForEach-Object { Fmt $_ 0 })) -join '   '))
    Say ("     stall rises monotonically with decode: {0}" -f $(if ($mono) { 'YES' } else { 'no' }))
}

$out = [pscustomobject]@{ rows = $rows; dropped = $dropped }
$out | ConvertTo-Json -Depth 6 | Out-File (Join-Path $Series 'matrix-53.json') -Encoding utf8
Say ''
Say ("JSON: {0}" -f (Join-Path $Series 'matrix-53.json'))
exit 0
