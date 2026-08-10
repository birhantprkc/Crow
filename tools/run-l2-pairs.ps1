<#
run-l2-pairs - the host-RAM tier against no tier, PAIRED on identical tasks, one task set per pair.

WHY PAIRED, AND WHY A FRESH TASK SET EACH TIME. Two attempts on 2026-08-09 produced nothing:

  1. All ten gate tasks, repeated per run. `probe-suite.py` without `--only` runs the same ten
     prompts every time, so run two meets a cache run one warmed. With a 32 GiB host tier holding
     experts, that shared state IS the thing under test - the second run measures how well the
     tier warmed itself. Vault: der-betriebspunkt-ist-200k-kontext-auf-einem-slot.
  2. Different tasks per arm. That removes the cache carry-over and replaces it with a worse
     problem: the arms then solve differently hard problems, and a throughput difference cannot be
     told apart from a difference in answer length.

What works is both at once: BOTH ARMS GET THE SAME TASKS WITHIN A PAIR, and every pair gets tasks
no earlier pair has seen. Inside a pair the workload is identical, so the arms are comparable;
across pairs nothing repeats, so no cache is measured twice. Each arm starts its own server, so
the tier begins empty on both sides, and `--moe-stream-direct` keeps the OS page cache out of it.

A WARM-UP TASK PRECEDES EACH GRADED PASS, on a prompt the graded pass does not use. Without it the
first graded task pays for the cold model, the cold VRAM slots and the empty tier at once.

WHAT IT PRODUCES: per pair, both arms' tok/s and their load stall per miss. The second is the more
robust of the two - it depends on the load path rather than on how long an answer happens to get.

MEASURED WITH THIS ARRANGEMENT, 2026-08-09, 32 GiB tier:
  r1 15.89 vs 10.81 t/s (1.47x) | r2 14.73 vs 10.54 (1.40x) | r3 14.53 vs 10.09 (1.44x)
  stall per miss 0.728/0.745/0.752 ms with, 1.307/1.282/1.345 ms without - a band of 4 %.
Within-arm spread was 1.09x (tier) and 1.07x (base), i.e. narrower than the difference, which is
what makes the difference readable at all.

NOTE: ASCII-only. Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI.

Usage:
  run-l2-pairs.ps1
  run-l2-pairs.ps1 -Selftest
#>
param(
    [int]    $L2Gib   = 32,
    [string] $Exe     = 'C:\Users\robin\dev\crow-lab\wt-25\build-25\bin\Release\llama-server.exe',
    [string] $Model   = $null,
    [int]    $Ctx     = 200000,
    [string] $OutRoot = 'C:\Users\robin\dev\Crow\runs\2026-08-09\pairs',
    [string] $CROW    = 'C:\Users\robin\dev\Crow',
    [switch] $Selftest
)

. "$PSScriptRoot\model-paths.ps1"
if (-not $Model) { $Model = Get-ModelPath 'operating-point' }

$ErrorActionPreference = 'Continue'

# One pair per row. No task appears twice anywhere in this table - that property is what the
# selftest checks, because a duplicate would silently turn one pair into a cache measurement.
$PLAN = @(
    @{ pair = 1; warm = @('two-sum');         graded = @('is-balanced','rotate-matrix') },
    @{ pair = 2; warm = @('merge-intervals'); graded = @('longest-common-prefix','group-anagrams') },
    @{ pair = 3; warm = @('flatten');         graded = @('binary-search','rle-encode') }
)

# Value function: decode, stall and the tier's presence out of one server log.
function Read-ArmLog {
    param([string]$Text)
    $ms = 0.0; $tok = 0; $n = 0
    foreach ($line in ($Text -split "`n")) {
        if ($line -match '\|\s+eval time =\s+([\d.]+) ms /\s+(\d+) tokens') {
            $ms += [double]$Matches[1]; $tok += [int]$Matches[2]; $n++
        }
    }
    $stall = $null; $miss = $null
    foreach ($line in ($Text -split "`n")) {
        if ($line -match 'load stall = ([\d.]+) ms')  { $stall = [double]$Matches[1] }
        if ($line -match 'misses = (\d+) \(')         { $miss  = [int]$Matches[1] }
    }
    # No eval lines means the run failed, not that it was fast. Returning null is what keeps a
    # dead arm out of a comparison.
    if ($n -eq 0 -or $null -eq $stall -or $null -eq $miss -or $ms -le 0 -or $miss -le 0) { return $null }
    return [pscustomobject]@{
        answers = $n; tokens = $tok; decode = $ms
        tok_s = $tok/($ms/1000.0)
        stall = $stall; stall_pc = 100.0*$stall/$ms
        per_miss = $stall/$miss
        tier = [bool]($Text -match 'L2 host tier')
    }
}

if ($Selftest) {
    $pass = 0; $fail = 0
    function Check($Name, $Want, $Got) {
        if ([string]$Want -eq [string]$Got) { $script:pass++; "  PASS  $Name" }
        else { $script:fail++; "  FAIL  $Name  want=$Want got=$Got" }
    }
    Write-Output "run-l2-pairs selftest"

    # The plan's own invariant. A repeated task is the exact defect this arrangement exists to
    # avoid, so it is checked by machine and not by reading the table.
    $all = @()
    foreach ($p in $PLAN) { $all += $p.warm; $all += $p.graded }
    Check 'no task appears twice' $all.Count (($all | Select-Object -Unique).Count)
    Check 'every pair has graded tasks' $true (@($PLAN | Where-Object { $_.graded.Count -eq 0 }).Count -eq 0)

    $log = @"
alloc_l2: MoE L2 host tier = 32.00 GiB, 7695 slots of 4464640 bytes, PINNED (page-locked)
slot print_timing: id  0 | task 1 |        eval time =    1000.00 ms /     100 tokens (   10.00 ms per token,   100.00 tokens per second)
print_stats: moe stream: remap calls = 10, expert hits = 80, misses = 200 (5 cold), hit rate = 80.00%
print_stats: moe stream: load stall = 400.00 ms total (0.040 ms per remap call)
"@
    $r = Read-ArmLog -Text $log
    Check 'tok/s'          100  ([math]::Round($r.tok_s,2))
    Check 'stall pct'      40   ([math]::Round($r.stall_pc,2))
    Check 'stall per miss' 2    ([math]::Round($r.per_miss,3))
    Check 'tier detected'  $true $r.tier

    # A baseline log must parse AND must not claim a tier - that is the negative control on the
    # arm itself. Without it a forgotten environment variable looks like a result.
    $base = ($log -split "`n" | Where-Object { $_ -notmatch 'L2 host tier' }) -join "`n"
    $b = Read-ArmLog -Text $base
    Check 'baseline parses'  100   ([math]::Round($b.tok_s,2))
    Check 'baseline no tier' $false $b.tier

    Check 'no eval lines -> null' $null (Read-ArmLog -Text 'print_stats: moe stream: load stall = 1.0 ms total')
    Check 'no stall -> null'      $null (Read-ArmLog -Text ($log -split "`n")[1])
    Check 'zero misses -> null'   $null (Read-ArmLog -Text ($log -replace 'misses = 200','misses = 0'))
    Check 'empty -> null'         $null (Read-ArmLog -Text '')

    Write-Output ""
    Write-Output "$pass passed, $fail failed"
    if ($fail -gt 0) { exit 1 }
    exit 0
}

$gate = Join-Path $CROW 'tools\measure-24-gate.ps1'
if (-not (Test-Path $gate)) { Write-Output "MISSING: $gate"; exit 2 }
if (-not (Test-Path $Exe))  { Write-Output "MISSING: $Exe";  exit 2 }

$flags = @('--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8','--moe-stream-direct','--jinja')
$rows  = @()

foreach ($p in $PLAN) {
    foreach ($arm in @('l2','base')) {
        $label = "r$($p.pair)-$arm"
        if ($arm -eq 'l2') { $env:LLAMA_MOE_STREAM_L2_GIB = "$L2Gib" }
        else               { Remove-Item Env:\LLAMA_MOE_STREAM_L2_GIB -ErrorAction SilentlyContinue }

        Write-Output ("[{0}] {1}  graded: {2}" -f (Get-Date -Format 'HH:mm:ss'), $label, ($p.graded -join ' '))
        & $gate -Label $label -Exe $Exe -Model $Model -Ctx $Ctx `
                -HealthTimeoutSec 900 -TimeoutSec 3600 -Only $p.graded -Warm $p.warm `
                -OutRoot (Join-Path $OutRoot $label) | Out-Null

        $log = Join-Path $OutRoot "$label\$label.err"
        $m   = if (Test-Path $log) { Read-ArmLog -Text (Get-Content $log -Raw) } else { $null }

        if ($null -eq $m) {
            Write-Output "    INVALID - no usable counters in $log"
        } elseif ($m.tier -ne ($arm -eq 'l2')) {
            # The arm must BE its arm. Without this check the whole series could be six runs of
            # one configuration and nothing in the output would say so.
            Write-Output ("    INVALID - arm '{0}' but tier present = {1}" -f $arm, $m.tier)
        } else {
            $rows += ($m | Add-Member -NotePropertyName arm  -NotePropertyValue $arm  -PassThru |
                           Add-Member -NotePropertyName pair -NotePropertyValue $p.pair -PassThru)
            Write-Output ("    {0,6:N2} tok/s   stall {1,4:N1} %   {2:N3} ms je Fehlschlag" -f $m.tok_s, $m.stall_pc, $m.per_miss)
        }
    }
}
Remove-Item Env:\LLAMA_MOE_STREAM_L2_GIB -ErrorAction SilentlyContinue

if ($rows.Count -eq 0) { Write-Output "NO VALID RUNS"; exit 1 }
$rows | Export-Csv -Path (Join-Path $OutRoot 'l2-pairs.csv') -NoTypeInformation

Write-Output ""
Write-Output "pair  arm     tok/s   stall%   ms/miss   tokens"
foreach ($row in $rows) {
    '{0,4}  {1,-5} {2,7:N2} {3,8:N1} {4,9:N3} {5,8}' -f $row.pair, $row.arm, $row.tok_s, $row.stall_pc, $row.per_miss, $row.tokens
}

Write-Output ""
Write-Output "=== per pair, tier against base ==="
foreach ($p in $PLAN) {
    $a = $rows | Where-Object { $_.pair -eq $p.pair -and $_.arm -eq 'l2' }
    $b = $rows | Where-Object { $_.pair -eq $p.pair -and $_.arm -eq 'base' }
    if (-not $a -or -not $b) { Write-Output ("pair {0}: incomplete" -f $p.pair); continue }
    'pair {0}  throughput {1,5:N2}x   stall per miss {2,5:N2}x cheaper' -f $p.pair, ($a.tok_s/$b.tok_s), ($b.per_miss/$a.per_miss)
}

# The spread WITHIN each arm bounds what the pairs can resolve. Printed beside the ratios so a
# reader can see whether the difference clears the noise instead of being told that it does.
Write-Output ""
foreach ($arm in @('l2','base')) {
    $v = @($rows | Where-Object { $_.arm -eq $arm } | ForEach-Object { $_.tok_s }) | Sort-Object
    if ($v.Count -gt 1) { '{0,-5} within-arm spread {1,5:N2}x  (min {2:N2}, max {3:N2})' -f $arm, ($v[-1]/$v[0]), $v[0], $v[-1] }
}
exit 0
