<#
verify-moe-stream-capacity - WHERE does an invalid expert-cache capacity get caught?

THE DEFECT THIS IS BUILT AROUND, and it is recorded, not hypothetical.
runs/2026-08-03/stream-a1-on/run-01.raw.txt carries:

    llama-graph.cpp:2007: MoE expert streaming: multi-pass expert GEMMs need an expert
    cache of at least 3*n_expert_used slots (have 16, need 18); increase --moe-stream-cache

"have 16" is exactly what llama_moe_stream_resolve_slots returns with NO --moe-stream-cache
at all: clamp(2*n_expert_used, 16, n_expert) = 16 for n_expert_used = 6, while the wave
path demands 3*n_expert_used = 18. The DEFAULT violates its own precondition, and nothing
between resolve_slots and the graph checks it. The abort fires deep inside graph building,
on the first batch whose n_touch_max exceeds the slot count.

WHAT THIS TOOL DECIDES: not whether the capacity is valid - the source says that - but at
which POINT an invalid one is caught, and whether the default is valid at all. Before E13
an invalid capacity dies at graph build; after E13 it must be refused where n_slots is
resolved, and the default must not be invalid in the first place. Same file, both states,
so "the error moved forward" is a measurement rather than a claim.

  -Mode before : default and 16 are expected to die LATE (graph build)
  -Mode after  : default is expected to RESOLVE to 3*n_expert_used and run;
                 an explicit 16 is expected to be refused EARLY, before any graph

THE GATE IS NOT A CONSTANT, which is why the prompt length is measured and not assumed.
Waves engage when n_touch_max > n_slots, i.e. n_tokens*n_expert_used > n_slots. At 64 slots
and n_expert_used 6 that is 11 tokens; at the default 16 slots it is 3. A prompt is therefore
only usable if its TOKENISED length - read back from the server, never counted in words -
clears the largest slot count under test.

PREFILL AND DECODE ARE NOT THE SAME PATH HERE. Wave splitting is reached from prefill only;
a single-token decode has n_touch_max = n_expert_used and never enters the gate. Every case
here therefore drives one prefill above the gate AND one decode, and reports them apart. A
number that mixes them says nothing about the code this stage changes.

TRAPS THIS TOOL IS BUILT AROUND, all measured earlier in this project:
  - Start-Process -PassThru releases the handle and .ExitCode then reads EMPTY. The process
    that ABORTS is the interesting one here, so EnableRaisingEvents is set right after start.
  - llama-server holds its log open; [IO.File]::ReadAllLines throws and leaves an EMPTY array,
    which reads exactly like "pattern not found". Opened with FileShare::ReadWrite.
  - a -lv 5 log carries raw token bytes; strict decoding failed on a single 0xef and returned
    nothing. Decoded with replacement.
  - a pipe swallows the exit code. Nothing here is piped.
  - the server writes NO loader block at default verbosity, so -lv 5 is mandatory on every side.

Exit 0 = every case landed where -Mode says it should.  1 = at least one did not.  2 = setup.
#>
param(
    [string]$Exe   = 'C:\Users\robin\dev\crow-lab\src\build-native\bin\Release\llama-server.exe',
    [string]$Lab   = 'C:\Users\robin\dev\crow-lab',
    [string]$CROW  = 'C:\Users\robin\dev\Crow',
    [string]$Model = $null,
    [ValidateSet('before', 'after')][string]$Mode = 'before',
    # Cases: label -> the --moe-stream-cache value, or '' for "no flag at all".
    [string]$Cases = ',16s,18s,64s',
    [int]   $Port  = 8081,
    [int]   $Ctx   = 4096,
    [int]   $Ngl   = 99,
    [int]   $Verbosity = 5,
    [int]   $HealthTimeoutSec = 420,
    [string]$OutRoot = ''
)

. "$PSScriptRoot\model-paths.ps1"
if (-not $Model) { $Model = Get-ModelPath 'q2-k-xl' }

$ErrorActionPreference = 'Continue'
$INV = [Globalization.CultureInfo]::InvariantCulture

function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }

# A prompt long enough to clear the gate at EVERY slot count under test. Its tokenised length
# is read back from the server and asserted; the word count here is a starting point, not the
# measurement.
$PROMPT = 'Explain in detail, step by step and with complete sentences, how a linked list is reversed in place, and then describe why the iterative version needs exactly three pointers.'

if (-not (Test-Path -LiteralPath $Exe)) { Die "server not found: $Exe" }
$full = (Resolve-Path -LiteralPath $Exe).ProviderPath
$sha  = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
if (-not $OutRoot) { $OutRoot = Join-Path $CROW ("runs\{0}\e13-capacity" -f (Get-Date -Format 'yyyy-MM-dd')) }
if (-not (Test-Path $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null }

$pre = @(Get-Process llama-server -ErrorAction SilentlyContinue)
if ($pre.Count -gt 0) { Die ("a llama-server is already running (pid " + (($pre | ForEach-Object { $_.Id }) -join ',') + ") - not touching it, and not measuring against it") }

function Read-LogText([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    } catch { return '' }
    try {
        $n = [int]$fs.Length
        $buf = New-Object byte[] $n
        $got = 0
        while ($got -lt $n) { $r = $fs.Read($buf, $got, $n - $got); if ($r -le 0) { break }; $got += $r }
        $enc = [Text.Encoding]::GetEncoding('utf-8', [Text.EncoderFallback]::ReplacementFallback, [Text.DecoderFallback]::ReplacementFallback)
        return $enc.GetString($buf, 0, $got)
    } finally { $fs.Close() }
}

function Stop-Servers {
    Get-Process llama-server -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 60; $i++) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# One case = one server process. The classification is the product of this tool: not "did it
# work" but WHERE it stopped, because that is the thing E13 moves.
function Invoke-Case {
    param([string]$Label, [string]$CacheArg)

    $logBase = Join-Path $OutRoot ("case-{0}" -f $Label)
    $srvArgs = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl',"$Ngl",'-np','1',
                 '-lv', "$Verbosity", '--moe-stream', '--moe-stream-io-threads','8','--moe-stream-direct')
    if ($CacheArg) { $srvArgs += @('--moe-stream-cache', $CacheArg) }

    $p = Start-Process -FilePath $full -ArgumentList $srvArgs -WorkingDirectory $Lab `
            -RedirectStandardOutput "$logBase.out" -RedirectStandardError "$logBase.err" -PassThru -WindowStyle Hidden
    try { $p.EnableRaisingEvents = $true } catch { }

    $healthy = $false; $diedEarly = $false
    for ($i = 0; $i -lt ($HealthTimeoutSec*2); $i++) {
        $p.Refresh()
        if ($p.HasExited) { $diedEarly = $true; break }
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3; if ($h.status -eq 'ok') { $healthy = $true; break } } catch { }
        Start-Sleep -Milliseconds 500
    }

    $prefill = $null; $decode = $null; $reqOk = $false; $promptN = $null; $completion = $null
    if ($healthy) {
        $body = @{ model='x'; messages=@(@{role='user'; content=$PROMPT}); max_tokens=64; temperature=0; stream=$false } | ConvertTo-Json -Depth 5
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 900
            $reqOk = $true
            $promptN    = [int]$r.timings.prompt_n
            $completion = [int]$r.usage.completion_tokens
            $prefill    = [double]$r.timings.prompt_ms
            $decode     = $(if ([int]$r.timings.predicted_n -gt 0) { [math]::Round([double]$r.timings.predicted_ms / [int]$r.timings.predicted_n, 3) } else { $null })
        } catch { }
        # a decode-only follow-up: single token, must never reach the wave gate
        if ($reqOk) {
            $body2 = @{ model='x'; messages=@(@{role='user'; content=$PROMPT}); max_tokens=1; temperature=0; stream=$false } | ConvertTo-Json -Depth 5
            try { $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -ContentType 'application/json' -Body $body2 -TimeoutSec 300 } catch { }
        }
    }

    $p.Refresh()
    $exited = $p.HasExited
    $code = $null
    if ($exited) { try { $code = $p.ExitCode } catch { } }
    [void](Stop-Servers)
    if (-not $exited) { try { $p.Refresh(); $code = $p.ExitCode } catch { } }

    $log = Read-LogText "$logBase.err"
    $resolved = $null
    $m = [regex]::Match($log, 'MoE expert SSD streaming enabled,\s*(\d+)\s*of\s*(\d+)')
    if ($m.Success) { $resolved = [int]$m.Groups[1].Value }
    $nExpertUsed = $null
    $m2 = [regex]::Match($log, 'n_expert_used\s*=\s*(\d+)')
    if ($m2.Success) { $nExpertUsed = [int]$m2.Groups[1].Value }
    $lateAbort  = [regex]::IsMatch($log, 'multi-pass expert GEMMs need an expert cache')
    $earlyRefuse= [regex]::IsMatch($log, 'invalid MoE stream cache capacity')
    $sawGraph   = [regex]::IsMatch($log, 'sched_reserve:|graph nodes')
    $abortLine  = ''
    $m3 = [regex]::Match($log, '.*(multi-pass expert GEMMs need an expert cache[^\r\n]*)')
    if ($m3.Success) { $abortLine = $m3.Groups[1].Value.Trim() }
    $m4 = [regex]::Match($log, '.*(invalid MoE stream cache capacity[^\r\n]*)')
    if ($m4.Success) { $abortLine = $m4.Groups[1].Value.Trim() }

    $class = 'unknown'
    if     ($earlyRefuse)            { $class = 'refused-early' }
    elseif ($lateAbort)              { $class = 'aborted-at-graph' }
    elseif ($reqOk)                  { $class = 'ran' }
    elseif ($diedEarly)              { $class = 'died-early-other' }
    elseif (-not $healthy)           { $class = 'never-ready' }

    return [pscustomobject]@{
        label = $Label; cache_arg = $(if ($CacheArg) { $CacheArg } else { '<none>' })
        resolved_slots = $resolved; n_expert_used = $nExpertUsed
        min_required = $(if ($nExpertUsed) { 3*$nExpertUsed } else { $null })
        class = $class; exit_code = $code
        graph_reached = $sawGraph; request_ok = $reqOk
        prompt_tokens = $promptN; completion_tokens = $completion
        prefill_ms = $prefill; decode_ms_tok = $decode
        message = $abortLine
        log = "$logBase.err"
    }
}

Say ('=' * 78)
Say ("MoE STREAM CAPACITY - where is an invalid capacity caught?   mode {0}" -f $Mode)
Say ("exe {0}" -f $full)
Say ("sha {0}" -f $sha)
Say ("cases {0}" -f ($Cases -replace ',,', ',<none>,'))
Say ('=' * 78)

$rows = @()
foreach ($c in ($Cases -split ',')) {
    $label = $(if ($c) { $c } else { 'default' })
    Say ("case {0}   --moe-stream-cache {1}" -f $label, $(if ($c) { $c } else { '(flag absent)' }))
    $r = Invoke-Case -Label $label -CacheArg $c
    $rows += $r
    Say ("  resolved {0} slots, n_expert_used {1}, min required {2}" -f $r.resolved_slots, $r.n_expert_used, $r.min_required)
    Say ("  class {0}   exit {1}   graph reached {2}   prompt tokens {3}" -f $r.class, $r.exit_code, $r.graph_reached, $r.prompt_tokens)
    if ($r.message) { Say ("  msg  {0}" -f $r.message) }
    if ($r.request_ok) { Say ("  prefill {0} ms / {1} tok   decode {2} ms/tok   completion {3}" -f $r.prefill_ms, $r.prompt_tokens, $r.decode_ms_tok, $r.completion_tokens) }
}
$rows | ConvertTo-Json -Depth 5 | Out-File (Join-Path $OutRoot 'capacity-cases.json') -Encoding ascii

# --- the gate, per mode -------------------------------------------------------------------
$bad = @()
$byLabel = @{}
foreach ($r in $rows) { $byLabel[$r.label] = $r }

# The prompt must clear the gate at the LARGEST slot count that actually ran, otherwise a
# green case proves only that the wave path was never entered.
$ran = @($rows | Where-Object { $_.class -eq 'ran' -and $_.prompt_tokens })
if ($ran.Count -gt 0) {
    $maxSlots = (@($ran | ForEach-Object { $_.resolved_slots }) | Measure-Object -Maximum).Maximum
    $eu = ($ran | Where-Object { $_.n_expert_used } | Select-Object -First 1).n_expert_used
    $tok = ($ran | Select-Object -First 1).prompt_tokens
    if ($eu -and $maxSlots) {
        $need = [math]::Floor($maxSlots / $eu) + 1
        Say ("gate check: prompt measured {0} tokens; {1} needed to exceed {2} slots at n_expert_used {3}" -f $tok, $need, $maxSlots, $eu)
        if ($tok -lt $need) { $bad += "prompt is $tok tokens, needs at least $need to enter the wave gate at $maxSlots slots" }
    }
}

foreach ($r in $rows) {
    switch ($Mode) {
        'before' {
            if ($r.label -eq 'default' -and $r.class -ne 'aborted-at-graph') { $bad += "default: expected aborted-at-graph, got $($r.class)" }
            if ($r.label -eq '16s'     -and $r.class -ne 'aborted-at-graph') { $bad += "16s: expected aborted-at-graph, got $($r.class)" }
            if ($r.label -eq '18s'     -and $r.class -ne 'ran')              { $bad += "18s: expected ran, got $($r.class)" }
            if ($r.label -eq '64s'     -and $r.class -ne 'ran')              { $bad += "64s: expected ran, got $($r.class)" }
            if ($r.label -eq 'default' -and $r.resolved_slots -ne 16)        { $bad += "default: expected 16 resolved slots, got $($r.resolved_slots)" }
        }
        'after' {
            if ($r.label -eq 'default' -and $r.class -ne 'ran')              { $bad += "default: expected ran, got $($r.class)" }
            if ($r.label -eq 'default' -and $r.min_required -and $r.resolved_slots -lt $r.min_required) { $bad += "default: resolved $($r.resolved_slots) below the minimum $($r.min_required)" }
            if ($r.label -eq '16s'     -and $r.class -ne 'refused-early')    { $bad += "16s: expected refused-early, got $($r.class)" }
            if ($r.label -eq '16s'     -and $r.graph_reached)                { $bad += "16s: a graph was built although the capacity is invalid" }
            if ($r.label -eq '18s'     -and $r.class -ne 'ran')              { $bad += "18s: expected ran, got $($r.class)" }
            if ($r.label -eq '64s'     -and $r.class -ne 'ran')              { $bad += "64s: expected ran, got $($r.class)" }
        }
    }
    if ($r.class -eq 'ran' -and -not $r.decode_ms_tok) { $bad += "$($r.label): ran but no decode figure - prefill and decode must both be reported" }
}

Write-Output ''
Say ('=' * 78)
Say 'CAPACITY MATRIX'
Say ('=' * 78)
Write-Output ("{0,-9} {1,-12} {2,9} {3,6} {4,6} {5,-18} {6,5} {7,7} {8,10} {9,12}" -f 'case','cache','resolved','n_eu','min','class','exit','tokens','prefill_ms','decode_ms_t')
foreach ($r in $rows) {
    Write-Output ("{0,-9} {1,-12} {2,9} {3,6} {4,6} {5,-18} {6,5} {7,7} {8,10} {9,12}" -f `
        $r.label, $r.cache_arg, $r.resolved_slots, $r.n_expert_used, $r.min_required, $r.class, $r.exit_code, $r.prompt_tokens,
        $(if ($null -ne $r.prefill_ms) { [math]::Round($r.prefill_ms,1) } else { '-' }),
        $(if ($null -ne $r.decode_ms_tok) { $r.decode_ms_tok } else { '-' }))
}
Write-Output ''
Write-Output 'prefill and decode are reported apart on purpose: wave splitting is a prefill path.'
Write-Output 'A single-token decode has n_touch_max = n_expert_used and never enters the gate, so a'
Write-Output 'mixed figure cannot show whether this stage changed anything.'
Write-Output ("raw: {0}" -f $OutRoot)

Write-Output ''
if ($bad.Count -eq 0) {
    Write-Output ("RESULT: PASS - {0} of {0} cases landed where mode '{1}' requires." -f $rows.Count, $Mode)
    exit 0
}
Write-Output ("RESULT: FAIL - {0} deviation(s) in mode '{1}'." -f $bad.Count, $Mode)
foreach ($b in $bad) { Write-Output "  $b" }
exit 1
