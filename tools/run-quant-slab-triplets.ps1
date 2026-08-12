<#
run-quant-slab-triplets - is the miss stall paid in BYTES or in LATENCY? Issue #89.

THE QUESTION, AND WHY IT IS NOT THE ONE #89 WAS OPENED WITH
The issue was opened on the idea that a smaller slab buys more cache slots. That idea is dead
on 0731 and the repository already contained the evidence: print_locality on this model reads
50% of selections covered by 4.7% of experts, 80% by 7.6%, 95% by 9.0%, Gini 0.909
(runs/2026-08-11/*/server.err). The shipped cache holds 58 of 256 = 22.7%, roughly 2.5x past
the point where that curve goes flat - which is why _moe_stream_cache_note records 58 against
60 as NOT a throughput difference. Slot count is saturated. Buying more of it buys nothing.

What a smaller slab still moves is BYTES PER MISS, and the locality curve says nothing about
that. Measured from the GGUF tensor tables, 63 routed-expert tensors present in both files:
UD-IQ2_XXS is 11.44% smaller than UD-IQ3_XXS, and only two of the three tensors move -
ffn_down_exps is byte-identical between the rungs, ffn_gate_exps and ffn_up_exps are 10.8%
smaller each. Slot: 360.69 MiB against a predicted 319.42 MiB.

So: with 11.44% fewer bytes to fetch per miss, does the stall fall by about 11%, or not at all?

  BANDWIDTH-BOUND -> stall per miss falls roughly in proportion. The lever is real, and it
                     also lifts the COLD-miss floor, which no cache size reaches (4,646 cold
                     misses identical across 24, 32 and 40 slots, #44).
  LATENCY-BOUND   -> stall per miss barely moves. 11.44% fewer bytes buys nothing, the quant
                     is a pure quality loss, and the ladder in #28 is confirmed on a new model
                     for a new reason.

BOTH OUTCOMES ARE REACHABLE AND BOTH ARE WORTH THE RUN. A script that could only produce the
first would measure nothing.

WHY THREE ARMS
  iq3-58    the operating point, re-measured on today's code and today's model
  iq2-58    the same slot count, the same cache budget, ONE variable: the file  <- the question
  iq2-max   the slots the smaller slab makes room for

iq2-58 is the arm that answers the question, because it is the only pair that changes exactly
one thing. iq2-max is kept because the server is already up and it costs one more pass: it
says whether the extra slots are FREE, not whether they PAY. Reading iq2-max against iq3-58
would move quantisation and slot count together - the defect #44 named in its own mmap
comparison, where the arms differed in backend as well as in streaming.

WHY TRIPLETS AND NOT REPEATS - inherited from run-l2-pairs.ps1, which paid for it twice
  1. All ten gate tasks per arm: probe-suite without --only runs the same prompts every time,
     so arm two meets a cache arm one warmed. With a host tier holding experts that shared
     state IS the thing under test.
  2. Different tasks per arm: removes the carry-over and replaces it with a worse problem -
     the arms then solve differently hard problems, and a throughput difference cannot be told
     apart from a difference in answer length.
Both at once is what works: EVERY ARM OF A TRIPLET GETS THE SAME TASKS, and every triplet gets
tasks no earlier triplet has seen. Each arm starts its own server, so every cache begins empty.

THE GUARD THAT MATTERS, AND WHY IT IS NOT OPTIONAL
The two arms differ only in the -m path. A typo, a stale manifest entry or a half-finished
download and the series is six runs of one model, with nothing in the output saying so - every
number would look exactly like a result. So each arm is held against what the SERVER printed:

    alloc_bufs:  CUDA0 expert cache size = 20919.88 MiB (58 slots per layer)

That line yields MiB-per-slot by division, and the arms MUST NOT agree on it. 360.69 is IQ3,
about 319 is IQ2; equal values across quants means one file ran twice. The loaded path is
checked too, but the printed slot size is the stronger check - it comes from the tensors, not
from the command line that could be wrong in the same way twice.

A third check holds the GENERATION, and it is not hypothetical either: a manifest key that
pointed at the preview while the file above it said 0731 is what put a prefill series under a
0731 heading (models.entries.operating-point). general.version reads '0731' here and is ABSENT
on the preview, whose general.name 'Deepseek-V4-Flash' is a PREFIX of this generation's - so a
name comparison alone would pass it on a substring match. The absent version is what refuses
it. More than one distinct general.name in a log means a second model was loaded, and then no
single row describes the arm; that is refused rather than attributed to whichever loaded first.

WHAT THIS DELIBERATELY DOES NOT DO
It does not run the quality gate. Quality is a property of the QUANTISATION, not of the slot
count: llama-moe-stream.h states the remap "never changes which experts the router selected,
so streaming affects latency, not outputs". Running the ten tasks per arm would spend three
run-hours re-measuring a constant. The quality axis is measure-gate-stability.ps1, once per
quantisation, and it is a separate invocation on purpose.

It also does not average the arms of a triplet into one number. Three triplets bound the
movement; they do not estimate a distribution, and the per-triplet rows are printed so the
spread is visible instead of smoothed away.

WHAT THE NUMBER MEANS
ms per miss = load stall / misses, per arm, per triplet. The comparison is iq2-58 against
iq3-58 within a triplet, because only there is the workload identical. Against the measured
0.713-0.722 ms per miss with the host tier on, a proportional saving would be about 0.08 ms.
Whether that is inside the run-to-run spread is what the three triplets say.

COST
Six server starts plus three (iq2-max), each loading 84-97 GiB from disk. Roughly 25-40 min
per arm including load, so about 4 to 6 hours for the full series. EUR 0.

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI, and a
stray non-ASCII character breaks the parse in a misleading way.
#>
param(
    [string]  $Exe        = 'C:\Users\robin\dev\crow-lab\wt-25\build-25\bin\Release\llama-server.exe',
    [string]  $CROW       = 'C:\Users\robin\dev\Crow',
    # Both resolved from manifests/operating-point.json when left empty. Nothing here spells a
    # GGUF path out - see tools/model-paths.ps1 for why that rule exists.
    [string]  $ModelIq3   = '',
    [string]  $ModelIq2   = '',
    [int]     $Ctx        = 200000,
    # The shipped operating point. Both quantisations run it, or the arms differ in two things.
    [string]  $SlotsBase  = '58s',
    # What the measured 319.42 MiB slab fits into the 20,919.88 MiB the operating point spends.
    # 65 and not 70: 70 would spend the largest cache ever OBSERVED working on this card, which
    # is not a ceiling anyone derived, and #86 is what a cache one slot too large looks like.
    [string]  $SlotsMax   = '65s',
    [int]     $IoThreads  = 8,
    # general.version as the GGUF carries it. Empty switches the generation check off, which is
    # only right for a model that has no such key -- and then the arms are not comparable to
    # anything in runs/2026-08-11 anyway.
    [string]  $ModelVersion = '0731',
    # 0731 embeds a chat template that fails its own golden vector 4. Every arm gets the file or
    # the series compares templates instead of quantisations.
    [string[]]$ExtraFlags = @(),
    [string]  $OutRoot    = '',
    [switch]  $Selftest
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# The plan. Nine of the ten gate tasks; no task appears twice, and the selftest checks that
# by machine rather than by reading the table.
# ---------------------------------------------------------------------------
$PLAN = @(
    @{ t = 1; warm = @('two-sum');         graded = @('is-balanced','rotate-matrix') },
    @{ t = 2; warm = @('merge-intervals'); graded = @('longest-common-prefix','group-anagrams') },
    @{ t = 3; warm = @('flatten');         graded = @('binary-search','rle-encode') }
)

$ARMS = @(
    @{ name = 'iq3-58';  quant = 'iq3'; slots = { $SlotsBase } },
    @{ name = 'iq2-58';  quant = 'iq2'; slots = { $SlotsBase } },
    @{ name = 'iq2-max'; quant = 'iq2'; slots = { $SlotsMax  } }
)

function Read-ArmLog {
    <#
    Every figure here comes out of what the server printed. Returns $null when the run did not
    produce usable counters - a dead arm must stay out of the comparison rather than enter it
    as a zero.
    #>
    param([string]$Text)

    $ms = 0.0; $tok = 0; $n = 0
    $stall = $null; $miss = $null; $cold = $null; $hit = $null
    $cacheMib = 0.0; $slots = 0; $modelFile = ''
    $modelName = ''; $modelVer = ''; $names = @()

    foreach ($line in ($Text -split "`n")) {
        if ($line -match '\|\s+eval time =\s+([\d.]+) ms /\s+(\d+) tokens') {
            $ms += [double]$Matches[1]; $tok += [int]$Matches[2]; $n++
        }
        if ($line -match 'load stall = ([\d.]+) ms')             { $stall = [double]$Matches[1] }
        if ($line -match 'misses = (\d+) \((\d+) cold\)')        { $miss = [int]$Matches[1]; $cold = [int]$Matches[2] }
        if ($line -match 'hit rate = ([\d.]+)%')                 { $hit  = [double]$Matches[1] }
        # The largest printed cache wins: the server prints a 0.00 MiB line before the buffers
        # are allocated, and taking the last or the first would take that one half the time.
        if ($line -match 'expert cache size =\s+([\d.]+) MiB \((\d+) slots per layer\)') {
            $c = [double]$Matches[1]
            if ($c -gt $cacheMib) { $cacheMib = $c; $slots = [int]$Matches[2] }
        }
        if ($line -match 'loaded meta data with .* from (.+\.gguf)') {
            if ($modelFile -eq '') { $modelFile = [System.IO.Path]::GetFileName($Matches[1].Trim()) }
        }
        # The generation, straight out of the GGUF metadata the server echoes. general.version is
        # '0731' on this generation and ABSENT on the preview, which is what makes it decisive:
        # the preview's general.name is 'Deepseek-V4-Flash', a prefix of this one, so a name test
        # alone would pass a substring match on the wrong model.
        if ($line -match '- kv\s+\d+:\s+general\.name\s+str\s+=\s+(.+?)\s*$') {
            $v = $Matches[1].Trim()
            if ($modelName -eq '') { $modelName = $v }
            if ($names -notcontains $v) { $names += $v }
        }
        if ($line -match '- kv\s+\d+:\s+general\.version\s+str\s+=\s+(.+?)\s*$') {
            if ($modelVer -eq '') { $modelVer = $Matches[1].Trim() }
        }
    }

    if ($n -eq 0 -or $null -eq $stall -or $null -eq $miss -or $ms -le 0 -or $miss -le 0) { return $null }
    if ($slots -le 0 -or $cacheMib -le 0) { return $null }

    return [pscustomobject]@{
        answers   = $n
        tokens    = $tok
        decode    = $ms
        tok_s     = $tok / ($ms / 1000.0)
        stall     = $stall
        stall_pc  = 100.0 * $stall / $ms
        misses    = $miss
        cold      = $cold
        cold_pc   = if ($miss -gt 0) { 100.0 * $cold / $miss } else { 0.0 }
        hit_rate  = $hit
        per_miss  = $stall / $miss
        cache_mib = $cacheMib
        slots     = $slots
        slot_mib  = $cacheMib / $slots
        model     = $modelFile
        name      = $modelName
        version   = $modelVer
        n_names   = $names.Count
    }
}

function Get-ArmFlags {
    <#
    REFUSES a second --moe-stream-cache rather than appending one. llama.cpp takes the last
    occurrence and prints a DEPRECATED warning that is easy to scroll past, so a duplicate would
    run a slot count no summary in the series names - and the arms would carry a label they did
    not measure.
    #>
    param([string]$CacheSlots, [int]$Threads, [string[]]$Extra = @())
    if ($CacheSlots -notmatch '^\d+s?$') {
        throw "CacheSlots must be a slot count like '58s', got '$CacheSlots'"
    }
    if ($Extra -contains '--moe-stream-cache') {
        throw "--moe-stream-cache belongs in -SlotsBase/-SlotsMax, not -ExtraFlags: a second one silently wins"
    }
    return @('--moe-stream','--moe-stream-cache',$CacheSlots,
             '--moe-stream-io-threads',"$Threads",'--moe-stream-direct','--jinja') + $Extra
}

function Test-ArmIdentity {
    <#
    The arm must BE its arm. Returns a reason string when it is not, and $null when it is.
    The checks exist because they fail differently: a wrong -m gives the wrong file name, a
    right -m against a half-written download gives the right name and the wrong slab, and a
    manifest key pointing at the previous generation gives the right file name for the wrong
    model -- which is not hypothetical, it is what put a prefill series under a 0731 heading
    (manifests/operating-point.json, models.entries.operating-point).
    #>
    param($Row, [string]$WantFile, [int]$WantSlots, [string]$WantVersion = '0731')
    if ($Row.n_names -gt 1)          { return "$($Row.n_names) distinct models in one log - the arm is not one model" }
    if ($Row.model -ne $WantFile)    { return "loaded $($Row.model), expected $WantFile" }
    if ($Row.slots -ne $WantSlots)   { return "server used $($Row.slots) slots, asked for $WantSlots" }
    if ($WantVersion -ne '' -and $Row.version -ne $WantVersion) {
        $got = if ($Row.version -eq '') { '<absent, i.e. the preview>' } else { $Row.version }
        return "general.version = $got, expected $WantVersion (model was '$($Row.name)')"
    }
    return $null
}

# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
if ($Selftest) {
    $script:pass = 0; $script:fail = 0
    function Check($Name, $Want, $Got) {
        if ([string]$Want -eq [string]$Got) { $script:pass++; "  PASS  $Name" }
        else { $script:fail++; "  FAIL  $Name  want=$Want got=$Got" }
    }
    Write-Output "run-quant-slab-triplets selftest"

    $all = @()
    foreach ($p in $PLAN) { $all += $p.warm; $all += $p.graded }
    Check 'no task appears twice'      $all.Count (($all | Select-Object -Unique).Count)
    Check 'every triplet is graded'    $true (@($PLAN | Where-Object { $_.graded.Count -eq 0 }).Count -eq 0)
    Check 'three arms'                 3 $ARMS.Count
    Check 'exactly one arm is iq3'     1 (@($ARMS | Where-Object { $_.quant -eq 'iq3' }).Count)

    $log = @"
llama_model_loader: loaded meta data with 74 key-value pairs and 1328 tensors from C:\x\DeepSeek-V4-Flash-0731-UD-IQ3_XXS-00001-of-00004.gguf (version GGUF V3 (latest))
llama_model_loader: - kv   5:                               general.name str              = Deepseek-V4-Flash-0731
llama_model_loader: - kv   6:                            general.version str              = 0731
alloc_bufs:        CUDA0 expert cache size =     0.00 MiB (58 slots per layer)
alloc_bufs:        CUDA0 expert cache size = 20919.88 MiB (58 slots per layer)
slot print_timing: id  0 | task 1 |        eval time =    1000.00 ms /     100 tokens (   10.00 ms per token,   100.00 tokens per second)
print_stats: moe stream: remap calls = 10, expert hits = 800, misses = 200 (50 cold), hit rate = 80.00%
print_stats: moe stream: load stall = 400.00 ms total (0.040 ms per remap call)
"@
    $r = Read-ArmLog -Text $log
    Check 'tok/s'                 100    ([math]::Round($r.tok_s,2))
    Check 'stall pct'             40     ([math]::Round($r.stall_pc,2))
    Check 'ms per miss'           2      ([math]::Round($r.per_miss,3))
    Check 'cold share pct'        25     ([math]::Round($r.cold_pc,2))
    Check 'hit rate'              80     $r.hit_rate
    Check 'slots'                 58     $r.slots
    # The 0.00 MiB line must lose. If it wins, every slab check in the series compares zeroes
    # and passes for the wrong reason.
    Check 'slot MiB ignores the 0.00 line' 360.69 ([math]::Round($r.slot_mib,2))
    Check 'model file'            'DeepSeek-V4-Flash-0731-UD-IQ3_XXS-00001-of-00004.gguf' $r.model
    Check 'general.name'          'Deepseek-V4-Flash-0731' $r.name
    Check 'general.version'       '0731' $r.version
    Check 'one model in the log'  1 $r.n_names

    # The generation check, and the case it exists for. The preview calls itself
    # 'Deepseek-V4-Flash' - a PREFIX of this generation's name - and carries no general.version
    # at all. A name comparison alone would let it through on a substring match; the absent
    # version is what refuses it.
    $prev = ($log -split "`n" | Where-Object { $_ -notmatch 'general\.version' }) -join "`n"
    $prev = $prev.Replace('general.name str              = Deepseek-V4-Flash-0731',
                          'general.name str              = Deepseek-V4-Flash')
    $rp = Read-ArmLog -Text $prev
    Check 'preview parses'                 100 ([math]::Round($rp.tok_s,2))
    Check 'preview has no version'         ''  $rp.version
    Check 'identity refuses the preview'   $true `
          ((Test-ArmIdentity -Row $rp -WantFile $want -WantSlots 58 -WantVersion '0731') -ne $null)

    # Two models in one log means a drafter came along, and then no single row describes the
    # arm. It must be refused rather than silently attributed to the first one loaded.
    $two = $log + "`nllama_model_loader: - kv   4:                               general.name str              = Deepseek-V4-Flash`n"
    $rt  = Read-ArmLog -Text $two
    Check 'two models are counted'         2 $rt.n_names
    Check 'identity refuses two models'    $true `
          ((Test-ArmIdentity -Row $rt -WantFile $want -WantSlots 58 -WantVersion '0731') -ne $null)

    # Negative controls. Each one is a defect this script exists to catch, injected on purpose.
    $noEval = ($log -split "`n" | Where-Object { $_ -notmatch 'eval time' }) -join "`n"
    Check 'a run with no answers is rejected' $true ($null -eq (Read-ArmLog -Text $noEval))

    $noCache = ($log -split "`n" | Where-Object { $_ -notmatch 'expert cache size' }) -join "`n"
    Check 'a run with no cache line is rejected' $true ($null -eq (Read-ArmLog -Text $noCache))

    $want = 'DeepSeek-V4-Flash-0731-UD-IQ3_XXS-00001-of-00004.gguf'
    Check 'identity passes on its own arm'  $null (Test-ArmIdentity -Row $r -WantFile $want -WantSlots 58)
    Check 'identity catches the wrong file' $true ((Test-ArmIdentity -Row $r -WantFile 'OTHER.gguf' -WantSlots 58) -ne $null)
    Check 'identity catches wrong slots'    $true ((Test-ArmIdentity -Row $r -WantFile $want -WantSlots 65) -ne $null)

    # The slab check itself, both colours. Two arms that print the same MiB per slot ran the
    # same file, whatever their labels say.
    $iq2 = $log.Replace('20919.88', '18526.36').Replace('UD-IQ3_XXS','UD-IQ2_XXS')
    $r2  = Read-ArmLog -Text $iq2
    Check 'iq2 slab differs'      $true  ([math]::Abs($r2.slot_mib - $r.slot_mib) -gt 1.0)
    Check 'iq2 slab is ~319 MiB'  319.42 ([math]::Round($r2.slot_mib,2))
    Check 'same log = same slab'  $true  ([math]::Abs((Read-ArmLog -Text $log).slot_mib - $r.slot_mib) -lt 0.001)

    $f = Get-ArmFlags -CacheSlots '58s' -Threads 8 -Extra @('--chat-template-file','X')
    Check 'cache size reaches the flags' '58s' $f[2]
    Check 'io threads reach the flags'   '8'   $f[4]
    $threw = $false
    try { Get-ArmFlags -CacheSlots '58s' -Threads 8 -Extra @('--moe-stream-cache','9s') } catch { $threw = $true }
    Check 'a duplicate cache flag throws' $true $threw
    $threw = $false
    try { Get-ArmFlags -CacheSlots 'lots' -Threads 8 } catch { $threw = $true }
    Check 'a nonsense slot count throws'  $true $threw

    Write-Output ""
    Write-Output ("{0} passed, {1} failed" -f $script:pass, $script:fail)
    exit ([int]($script:fail -gt 0))
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
. (Join-Path $PSScriptRoot 'model-paths.ps1')
if ($ModelIq3 -eq '') { $ModelIq3 = Get-ModelPath '0731-iq3-xxs' -MustExist }
if ($ModelIq2 -eq '') { $ModelIq2 = Get-ModelPath '0731-iq2-xxs' -MustExist }

$gate = Join-Path $CROW 'tools\measure-24-gate.ps1'
if (-not (Test-Path $gate)) { Write-Output "MISSING: $gate"; exit 2 }
if (-not (Test-Path $Exe))  { Write-Output "MISSING: $Exe";  exit 2 }

if ($OutRoot -eq '') {
    $OutRoot = Join-Path $CROW ("runs\" + (Get-Date -Format 'yyyy-MM-dd') + "\quant-slab")
}
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null

$MODEL_OF = @{ iq3 = $ModelIq3; iq2 = $ModelIq2 }
$FILE_OF  = @{ iq3 = [System.IO.Path]::GetFileName($ModelIq3)
               iq2 = [System.IO.Path]::GetFileName($ModelIq2) }

Write-Output ("out      {0}" -f $OutRoot)
Write-Output ("iq3      {0}" -f $FILE_OF.iq3)
Write-Output ("iq2      {0}" -f $FILE_OF.iq2)
Write-Output ("arms     {0}" -f (($ARMS | ForEach-Object { $_.name }) -join ', '))
Write-Output ""

# ---------------------------------------------------------------------------
# The series
# ---------------------------------------------------------------------------
$rows = @()
foreach ($p in $PLAN) {
    foreach ($arm in $ARMS) {
        $slots = & $arm.slots
        $label = "t$($p.t)-$($arm.name)"
        $flags = Get-ArmFlags -CacheSlots $slots -Threads $IoThreads -Extra $ExtraFlags

        Write-Output ("[{0}] {1}  graded: {2}" -f (Get-Date -Format 'HH:mm:ss'), $label, ($p.graded -join ' '))

        & $gate -Label $label -Exe $Exe -Model $MODEL_OF[$arm.quant] -Ctx $Ctx -Flags $flags `
                -HealthTimeoutSec 900 -TimeoutSec 3600 -Only $p.graded -Warm $p.warm `
                -OutRoot (Join-Path $OutRoot $label) | Out-Null

        $log = Join-Path $OutRoot "$label\$label.err"
        $m   = if (Test-Path $log) { Read-ArmLog -Text (Get-Content $log -Raw) } else { $null }

        if ($null -eq $m) {
            Write-Output "    INVALID - no usable counters in $log"
            continue
        }
        $why = Test-ArmIdentity -Row $m -WantFile $FILE_OF[$arm.quant] `
                                -WantSlots ([int]($slots -replace 's$','')) -WantVersion $ModelVersion
        if ($why) {
            Write-Output "    INVALID - $label $why"
            continue
        }
        $rows += ($m | Add-Member -NotePropertyName arm     -NotePropertyValue $arm.name -PassThru |
                       Add-Member -NotePropertyName quant   -NotePropertyValue $arm.quant -PassThru |
                       Add-Member -NotePropertyName triplet -NotePropertyValue $p.t -PassThru)
        Write-Output ("    {0,6:N2} tok/s   {1,7:N3} ms/miss   stall {2,4:N1} %   hit {3,5:N2} %   slab {4,6:N2} MiB" `
                      -f $m.tok_s, $m.per_miss, $m.stall_pc, $m.hit_rate, $m.slot_mib)
    }
}

if ($rows.Count -eq 0) { Write-Output "NO VALID RUNS"; exit 1 }
$rows | Export-Csv -Path (Join-Path $OutRoot 'quant-slab.csv') -NoTypeInformation

# The whole-series identity check. Two quantisations that printed the same slab ran one file,
# and every comparison built on those rows would be a model against itself.
$slabs = @{}
foreach ($r in $rows) { $slabs[$r.quant] = [math]::Round($r.slot_mib, 2) }
if ($slabs.Count -eq 2 -and $slabs['iq3'] -eq $slabs['iq2']) {
    Write-Output ""
    Write-Output ("SERIES INVALID - both quantisations printed {0} MiB per slot. One file ran twice." -f $slabs['iq3'])
    exit 1
}

Write-Output ""
Write-Output "triplet  arm        tok/s   ms/miss   stall%    hit%   cold%   misses   tokens   slab MiB"
foreach ($r in $rows) {
    '{0,7}  {1,-9} {2,6:N2} {3,9:N3} {4,8:N1} {5,7:N2} {6,7:N1} {7,8} {8,8} {9,10:N2}' -f `
        $r.triplet, $r.arm, $r.tok_s, $r.per_miss, $r.stall_pc, $r.hit_rate, $r.cold_pc, $r.misses, $r.tokens, $r.slot_mib
}

# The question, answered per triplet. Only within a triplet is the workload identical, so this
# is the only place the two arms may be divided by one another.
Write-Output ""
Write-Output "iq2-58 against iq3-58, within each triplet - the one comparison that changes one variable:"
Write-Output "triplet   ms/miss iq3   ms/miss iq2   change    bytes/miss change   tokens iq3   tokens iq2"
foreach ($p in $PLAN) {
    $a = $rows | Where-Object { $_.triplet -eq $p.t -and $_.arm -eq 'iq3-58' }
    $b = $rows | Where-Object { $_.triplet -eq $p.t -and $_.arm -eq 'iq2-58' }
    if (-not $a -or -not $b) { Write-Output ("{0,7}   incomplete" -f $p.t); continue }
    $dStall = 100.0 * ($b.per_miss / $a.per_miss - 1.0)
    $dBytes = 100.0 * ($b.slot_mib / $a.slot_mib - 1.0)
    '{0,7} {1,13:N3} {2,14:N3} {3,8:N1} % {4,17:N1} % {5,12} {6,12}' -f `
        $p.t, $a.per_miss, $b.per_miss, $dStall, $dBytes, $a.tokens, $b.tokens
}

Write-Output ""
Write-Output "READ THIS BEFORE THE NUMBERS: a stall change near the bytes change is bandwidth-bound;"
Write-Output "a stall change near zero is latency-bound and the smaller slab bought nothing. Three"
Write-Output "triplets bound the movement, they do not estimate a distribution. Quality is NOT in"
Write-Output "this table - it is measure-gate-stability.ps1, once per quantisation."
