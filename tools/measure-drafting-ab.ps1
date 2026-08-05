<#
measure-drafting-ab - is draft-dspark faster, neutral or slower at THIS operating point?

E11 proved the drafter works: 228 drafted, 194 accepted, acceptance 0.8509. It also
showed the decode was slower with the drafter than without - 106.74 against 92.44
ms/token over three runs - and that is a finding, not a verdict. Three runs taken all on
one side and then all on the other cannot separate the drafter from warm-up, temperature
and time drift. This tool exists to make that separation possible.

WHAT IT DECIDES, and nothing more: whether the SAME b10269 build with -md is faster,
neutral or slower than without. It does not explain why. If the drafter side stays
slower, the next step is a cause decomposition - drafting time, verification time,
synchronisation gaps, expert-stream waiting, VRAM pressure, block size, accepted tokens
per verification step - and that is a separate stage.

THE ORDER IS ALTERNATING AND FIXED BEFORE THE START: A B B A A B B A. Running all of A
and then all of B charges every warm-up and every thermal drift to one side. The count
is fixed too - four evaluable runs per side - and it is NOT extended after seeing a
convenient or inconvenient interim result. That is the difference between measuring and
fishing.

ONE WARM-UP PER SERVER START, DISCARDED. -md is a start parameter, so every switch is a
new process with a cold prefix cache. E9 measured what that costs: run 1 prefills 17
tokens, runs 2+ hit the cache and prefill 4. Keeping run 1 would compare a cold A against
a warm B wherever the sequence happens to fall.

DECODE IS THE PRIMARY QUANTITY. tok/s over a whole request contains the prefill, and
speculation acts on the decode alone - a mixed figure dilutes exactly the effect under
test. E9 measured -0.29 % client against -0.66 % decode in the same run. Prefill and
total time are reported, never merged into the judgement.

TELEMETRY BEFORE EACH BLOCK, and no foreign process is touched. If another process takes
the GPU mid-series, that has to be visible in the record rather than tidied away.

Usage:
  measure-drafting-ab.ps1
  measure-drafting-ab.ps1 -Blocks 'A,B,B,A,A,B,B,A' -Tokens 512

Exit 0 = series complete and comparable.  1 = an abort criterion fired.  2 = setup error.
#>
param(
    [string]$WT      = 'C:\Users\robin\dev\crow-lab\wt-e11',
    [string]$Bin     = 'build-e11\bin\Release',
    [string]$Lab     = 'C:\Users\robin\dev\crow-lab',
    [string]$CROW    = 'C:\Users\robin\dev\Crow',
    [string]$Model   = 'models/UD-Q2_K_XL/DeepSeek-V4-Flash-UD-Q2_K_XL-00001-of-00003.gguf',
    [string]$Drafter = 'models/DSV4-Flash-DSpark-draft-bf16.gguf',
    [string]$SpecType = 'draft-dspark',
    # Fixed before the start. Four evaluable runs per side, alternating.
    [string]$Blocks  = 'A,B,B,A,A,B,B,A',
    [int]   $Port    = 8081,
    [int]   $Ctx     = 4096,
    [int]   $Ngl     = 99,
    [int]   $Tokens  = 512,
    [int]   $Verbosity = 5,
    [int]   $HealthTimeoutSec = 420,
    [string]$OutRoot = ''
)

$ErrorActionPreference = 'Continue'
if (-not $OutRoot) { $OutRoot = Join-Path $CROW ("runs\{0}\e12-ab-drafting" -f (Get-Date -Format 'yyyy-MM-dd')) }
if (-not (Test-Path $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null }

function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }

$exe = Join-Path $WT (Join-Path $Bin 'llama-server.exe')
if (-not (Test-Path -LiteralPath $exe)) { Die "llama-server not found at $exe" }
$full = (Resolve-Path -LiteralPath $exe).ProviderPath
if ($full.StartsWith((Join-Path $Lab 'src\build-native') + '\', [StringComparison]::OrdinalIgnoreCase)) { Die "refusing the production build" }
$sha = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash

function Stop-Servers {
    Get-Process llama-server -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    for ($i=0; $i -lt 60; $i++) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}
function Telemetry {
    $q = (nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,clocks.sm,power.draw --format=csv,noheader,nounits) -join ''
    $foreign = @(Get-Process | Where-Object { $_.ProcessName -match 'ollama|python|llama' } | ForEach-Object { $_.ProcessName }) -join ','
    return [pscustomobject]@{ gpu=$q; foreign=$foreign; at=(Get-Date).ToString('o') }
}
function Ask {
    param([int]$MaxTokens)
    $body = @{ model='x'; messages=@(@{role='user'; content='Write a Python function that reverses a linked list. Code only.'})
               max_tokens=$MaxTokens; temperature=0; stream=$false } | ConvertTo-Json -Depth 5
    $t0 = Get-Date
    try { $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 900 }
    catch { return $null }
    $secs = ((Get-Date) - $t0).TotalSeconds
    $tm = $r.timings
    $content = [string]$r.choices[0].message.content
    $reason  = [string]$r.choices[0].message.reasoning_content
    $sha16 = { param($s) if (-not $s) { return '' }
               $h=[Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s))
               (($h | ForEach-Object { '{0:x2}' -f $_ }) -join '').Substring(0,16) }
    return [pscustomobject]@{
        wall_s        = [math]::Round($secs,3)
        prompt_ms     = [double]$tm.prompt_ms
        prompt_n      = [int]$tm.prompt_n
        predicted_ms  = [double]$tm.predicted_ms
        predicted_n   = [int]$tm.predicted_n
        decode_ms_tok = $(if ([int]$tm.predicted_n -gt 0) { [math]::Round([double]$tm.predicted_ms / [int]$tm.predicted_n, 4) } else { $null })
        prefill_ms_tok= $(if ([int]$tm.prompt_n -gt 0) { [math]::Round([double]$tm.prompt_ms / [int]$tm.prompt_n, 4) } else { $null })
        completion    = [int]$r.usage.completion_tokens
        drafted       = $(if ($null -ne $tm.draft_n) { [int]$tm.draft_n } else { 0 })
        accepted      = $(if ($null -ne $tm.draft_n_accepted) { [int]$tm.draft_n_accepted } else { 0 })
        accept_rate   = $(if ($null -ne $tm.draft_n -and [int]$tm.draft_n -gt 0) { [math]::Round([double]$tm.draft_n_accepted / [double]$tm.draft_n, 4) } else { $null })
        finish        = [string]$r.choices[0].finish_reason
        answer_sha    = (& $sha16 $content)
        answer_chars  = $content.Length
        reasoning_chars = $reason.Length
    }
}

$seq = @($Blocks -split ',' | ForEach-Object { $_.Trim().ToUpper() })
$baseArgs = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl',"$Ngl",'-np','1',
              '-lv', "$Verbosity",
              '--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8','--moe-stream-direct',
              '--spec-type', $SpecType)

Say ('=' * 78)
Say ("A/B DRAFTING   sequence {0}   tokens {1}   ONE discarded warm-up per block" -f ($seq -join ' '), $Tokens)
Say ("exe {0}" -f $full); Say ("sha {0}" -f $sha)
Say ("A = with -md {0}   B = same build without -md   (only variable)" -f $Drafter)
Say ('=' * 78)
if (-not (Stop-Servers)) { Die "port $Port still held" }

$results = @(); $aborts = @(); $blockNo = 0
foreach ($side in $seq) {
    $blockNo++
    if ($side -ne 'A' -and $side -ne 'B') { Die "unknown block '$side'" }
    $tele = Telemetry
    Say ("block {0}  side {1}   gpu[{2}]  foreign[{3}]" -f $blockNo, $side, $tele.gpu, $tele.foreign)
    $args = $(if ($side -eq 'A') { $baseArgs + @('-md', $Drafter) } else { $baseArgs })
    $logBase = Join-Path $OutRoot ("block{0}-{1}" -f $blockNo, $side)
    $p = Start-Process -FilePath $full -ArgumentList $args -WorkingDirectory $Lab `
            -RedirectStandardOutput "$logBase.out" -RedirectStandardError "$logBase.err" -PassThru -WindowStyle Hidden
    try { $p.EnableRaisingEvents = $true } catch { }
    $healthy = $false
    for ($i=0; $i -lt ($HealthTimeoutSec*2); $i++) {
        $p.Refresh(); if ($p.HasExited) { break }
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3; if ($h.status -eq 'ok') { $healthy = $true; break } } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $healthy) { $aborts += "block $blockNo ($side): server never became ready"; $null = Stop-Servers; continue }

    $warm = Ask -MaxTokens $Tokens          # discarded: cold prefix cache after every start
    if ($null -eq $warm) { $aborts += "block $blockNo ($side): warm-up request failed"; $null = Stop-Servers; continue }
    Say ("  warm-up (discarded)  decode {0} ms/tok  prefill {1} ms / {2} tok" -f $warm.decode_ms_tok, $warm.prompt_ms, $warm.prompt_n)

    $m = Ask -MaxTokens $Tokens
    if ($null -eq $m) { $aborts += "block $blockNo ($side): measured request failed"; $null = Stop-Servers; continue }
    $m | Add-Member -NotePropertyName side  -NotePropertyValue $side -Force
    $m | Add-Member -NotePropertyName block -NotePropertyValue $blockNo -Force
    $m | Add-Member -NotePropertyName telemetry -NotePropertyValue $tele -Force
    $results += $m
    Say ("  MEASURED  decode {0,8} ms/tok   prefill {1,8} ms / {2,2} tok   completion {3}   drafted {4}   sha {5}" -f `
         $m.decode_ms_tok, $m.prompt_ms, $m.prompt_n, $m.completion, $m.drafted, $m.answer_sha)

    # abort criteria, checked per block rather than at the end
    if ($side -eq 'A' -and $m.drafted -le 0)  { $aborts += "block $blockNo (A): drafted 0 - the drafter side did not draft" }
    if ($side -eq 'B' -and $m.drafted -ne 0)  { $aborts += "block $blockNo (B): drafted $($m.drafted) - the control side drafted" }
    if ($m.finish -ne 'stop')                 { $aborts += "block $blockNo ($side): finish_reason $($m.finish)" }
    $err = @(Get-Content "$logBase.err" -ErrorAction SilentlyContinue | Where-Object { $_ -match 'CUDA error|out of memory|cudaMalloc failed|failed to load' }).Count
    if ($err -gt 0) { $aborts += "block $blockNo ($side): $err GPU/load error lines" }
    $null = Stop-Servers
}

$medBad = Test-Med
if ($medBad.Count -gt 0) { foreach ($b in $medBad) { Say "  MEDIAN SELF-TEST FAILED: $b" }; $aborts += "median implementation is wrong" }
else { Say "  median self-test: 4 of 4 cases green (even and odd n)" }
$results | ConvertTo-Json -Depth 6 | Out-File (Join-Path $OutRoot 'ab-runs.json') -Encoding ascii
$A = @($results | Where-Object { $_.side -eq 'A' })
$B = @($results | Where-Object { $_.side -eq 'B' })
function Med($xs) {
    # The first version returned $s[floor(n/2)], which for an even n is the UPPER of the
    # two middle values, not the median. Measured 2026-08-05: it reported 95.03 and 92.54
    # for four-value sets whose medians are 94.235 and 91.630 - a figure labelled "median"
    # that was not one. Even n averages the two middle values.
    if (-not $xs -or $xs.Count -eq 0) { return $null }
    $s = @($xs | Sort-Object)
    $n = $s.Count
    if ($n % 2 -eq 1) { return [double]$s[[int](($n - 1) / 2)] }
    return ([double]$s[$n/2 - 1] + [double]$s[$n/2]) / 2.0
}
function Test-Med {
    # Both parities, because the defect only showed at even n.
    $cases = @(@{ x=@(1,2,3,4); want=2.5 }, @{ x=@(1,2,3); want=2 },
               @{ x=@(89.75,95.03,93.44,97.18); want=94.235 },
               @{ x=@(90.61,92.54,94.75,90.72); want=91.63 })
    $bad = @()
    foreach ($c in $cases) {
        $got = Med $c.x
        if ([Math]::Abs($got - $c.want) -gt 0.0005) { $bad += ("Med({0}) = {1}, want {2}" -f ($c.x -join ','), $got, $c.want) }
    }
    return $bad
}

Write-Output ''
Say ('=' * 78)
Say ("A (with -md)    n={0}   decode ms/tok: {1}" -f $A.Count, (($A | ForEach-Object { '{0:N2}' -f $_.decode_ms_tok }) -join ', '))
Say ("B (no -md)      n={0}   decode ms/tok: {1}" -f $B.Count, (($B | ForEach-Object { '{0:N2}' -f $_.decode_ms_tok }) -join ', '))
$ma = Med (@($A | ForEach-Object { $_.decode_ms_tok })); $mb = Med (@($B | ForEach-Object { $_.decode_ms_tok }))
Say ("PRIMARY  decode ms/token  median A {0}   median B {1}" -f $(if($ma){'{0:N2}' -f $ma}else{'-'}), $(if($mb){'{0:N2}' -f $mb}else{'-'}))
if ($ma -and $mb) {
    $delta = 100.0 * ($ma - $mb) / $mb
    Say ("         A vs B: {0:+0.00;-0.00;0.00} %  (positive = A slower)" -f $delta)
    $aMin=($A|ForEach-Object{$_.decode_ms_tok}|Measure-Object -Minimum).Minimum
    $aMax=($A|ForEach-Object{$_.decode_ms_tok}|Measure-Object -Maximum).Maximum
    $bMin=($B|ForEach-Object{$_.decode_ms_tok}|Measure-Object -Minimum).Minimum
    $bMax=($B|ForEach-Object{$_.decode_ms_tok}|Measure-Object -Maximum).Maximum
    Say ("         spread A [{0:N2} .. {1:N2}]   spread B [{2:N2} .. {3:N2}]" -f $aMin,$aMax,$bMin,$bMax)
    # The same rule the slot measurement uses: overlapping spreads are not a difference.
    # Overlapping ranges are a DESCRIPTIVE observation. They do not prove "indistinguishable
    # from noise" - that would need a pre-declared statistical test, a sufficient sample or
    # an equivalence interval, none of which four runs per side provide. Say what is seen.
    $sep = ($aMin -gt $bMax) -or ($bMin -gt $aMax)
    Say ("         ranges {0}" -f $(if($sep){'are disjoint'}else{'OVERLAP'}))
    Say  "         At n=4 per side this supports NEITHER a proven slowdown NOR proven equivalence."
}
Say ("SECONDARY prefill ms  A: {0}" -f (($A | ForEach-Object { '{0:N0}' -f $_.prompt_ms }) -join ', '))
Say ("SECONDARY prefill ms  B: {0}" -f (($B | ForEach-Object { '{0:N0}' -f $_.prompt_ms }) -join ', '))
Say ("SECONDARY wall s      A: {0}" -f (($A | ForEach-Object { '{0:N1}' -f $_.wall_s }) -join ', '))
Say ("SECONDARY wall s      B: {0}" -f (($B | ForEach-Object { '{0:N1}' -f $_.wall_s }) -join ', '))
Say ("answer sha  A: {0}" -f ((@($A | ForEach-Object { $_.answer_sha }) | Sort-Object -Unique) -join ', '))
Say ("answer sha  B: {0}" -f ((@($B | ForEach-Object { $_.answer_sha }) | Sort-Object -Unique) -join ', '))
Say ("completion  A: {0}   B: {1}" -f ((@($A|ForEach-Object{$_.completion})|Sort-Object -Unique) -join ','), ((@($B|ForEach-Object{$_.completion})|Sort-Object -Unique) -join ','))
Say ("acceptance  A: {0}" -f (($A | ForEach-Object { "$($_.accepted)/$($_.drafted)" }) -join ', '))
Say ("raw: {0}" -f $OutRoot)

Write-Output ''
if ($aborts.Count -eq 0) {
    Write-Output ("RESULT: PASS - series complete, {0} evaluable runs per side in a fixed alternating order." -f $A.Count)
    Write-Output "  This answers only: faster, neutral or slower at THIS operating point. It does not"
    Write-Output "  explain a difference - that needs a cause decomposition and is a separate stage."
    exit 0
} else {
    Write-Output ("RESULT: FAIL - {0} abort criteria fired." -f $aborts.Count)
    foreach ($a in $aborts) { Write-Output "  ABORT  $a" }
    exit 1
}
