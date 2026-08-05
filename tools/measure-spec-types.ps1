<#
Measures every draft-model-free speculative decoding method llama.cpp offers, against a
baseline that differs from them in exactly one value. Issue #29.

THE QUESTION
#29 asks how much speculative decoding amortises the weight read when weights are offloaded.
Every method on record needs a draft model - and our draft model does not load (dflash expects
dense Qwen3 attention, DSpark has MLA). But `--spec-type` also carries five methods that need
NO draft model at all: ngram-simple, ngram-map-k, ngram-map-k4v, ngram-mod, ngram-cache. They
have never been run here. They are the only lever in stock that lowers the NUMBER of decode
steps rather than the wait per step, and they cost no download.

WHY THE ACCEPTANCE RATE AND NOT ONLY THE FACTOR
#29's "done when" names both, and the reason is transferability: a factor describes this model
on this machine, an acceptance rate describes the method. The server reports draft_n and
draft_n_accepted in `timings`, but ONLY when draft_n > 0 (server-task.cpp:259).

  "no draft" is therefore NOT an acceptance rate of zero. It means the method never proposed
  a token. Proposing nothing and being refused everything are different findings with the same
  effect on the clock, and only one of them says the method is unsuited to the workload.

THE BASELINE DIFFERS IN ONE VALUE, NOT IN SHAPE
The baseline runs `--spec-type none` rather than omitting the flag. Same command line, one
value different - otherwise the comparison also carries whatever else the missing flag changes.

WHAT THIS MEASURES ABOUT THE WORKLOAD, NOT ONLY ABOUT THE METHOD
n-gram methods draft by looking up repetitions of the context. On a workload whose output does
not repeat its input they have nothing to propose, and a low or absent acceptance rate is then
a statement about these four prompts, not about the method in general. The prompts are the ones
in measure-slot-scaling.ps1; whatever comes out has to be quoted with them.

THE FAILURE CASE THIS MUST BE ABLE TO SHOW
That speculation costs throughput. Verification runs the target model over drafted tokens, so a
method that proposes badly is slower than none at all. That outcome is reachable here and is
reported as-is, including a negative factor against the baseline.

WHAT IT DOES NOT DO
It does not touch the operating point, does not load a draft model, and does not judge quality.
A speed number from a lever that changed the output is only half a measurement - the quality
gate (#46) is the other half and is run separately.

COST
One server restart plus one measurement per method. About 2 minutes per method at the default
repeats, so roughly 12 to 15 minutes for all six. EUR 0.

Usage:
  measure-spec-types.ps1
  measure-spec-types.ps1 -Types none,ngram-cache -Repeats 5
#>
param(
    [int]      $Port    = 8081,
    [string[]] $Types   = @('none','ngram-simple','ngram-map-k','ngram-map-k4v','ngram-mod','ngram-cache'),
    [int]      $Repeats = 4,
    [int]      $Tokens  = 128,
    [string]   $Model   = 'models/UD-Q2_K_XL/DeepSeek-V4-Flash-UD-Q2_K_XL-00001-of-00003.gguf',
    [string]   $OutRoot = ''
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Lab      = 'C:\Users\robin\dev\crow-lab'
$Server   = Join-Path $Lab 'src\build-native\bin\Release\llama-server.exe'
$Scaling  = Join-Path $PSScriptRoot 'measure-slot-scaling.ps1'

if (-not (Test-Path $Server))  { throw "llama-server not found at $Server" }
if (-not (Test-Path $Scaling)) { throw "measure-slot-scaling.ps1 not found at $Scaling" }
if ($Types.Count -lt 2)        { throw "Need at least a baseline and one method to compare." }

if ([string]::IsNullOrWhiteSpace($OutRoot)) {
    $OutRoot = Join-Path $RepoRoot ("runs/" + (Get-Date -Format 'yyyy-MM-dd') + "/spec-types")
}
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null

function Stop-Server {
    Get-Process llama-server -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    # Wait for the port to actually free. Starting the next server against a socket still held
    # by the old one fails with a message that looks like a bad argument.
    for ($i = 0; $i -lt 30; $i++) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "port $Port is still held after stopping llama-server"
}

function Start-Server {
    param([string] $SpecType, [string] $LogPath)
    # NOT $args - that is an automatic variable inside a function and assigning to it is a trap.
    $serverArgs = @(
        '-m', $Model,
        '--host', '127.0.0.1', '--port', "$Port",
        '-c', '4096', '-ngl', '99', '-np', '1',
        '--moe-stream', '--moe-stream-cache', '64s',
        '--moe-stream-io-threads', '8', '--moe-stream-direct',
        '--spec-type', $SpecType
    )
    $p = Start-Process -FilePath $Server -ArgumentList $serverArgs -WorkingDirectory $Lab `
            -RedirectStandardOutput $LogPath -RedirectStandardError "$LogPath.err" `
            -PassThru -WindowStyle Hidden
    for ($i = 0; $i -lt 120; $i++) {
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
            if ($h.status -eq 'ok') { return $p }
        } catch { }
        if ($p.HasExited) { throw "llama-server exited during load, see $LogPath.err" }
        Start-Sleep -Milliseconds 500
    }
    throw "llama-server did not become healthy within 60 s for --spec-type $SpecType"
}

Write-Host ""
Write-Host ("Speculative decoding without a draft model: {0} configurations" -f $Types.Count)
Write-Host ("Repeats {0} (run 1 discarded), {1} tokens per request, one slot" -f $Repeats, $Tokens)
Write-Host ("Output: {0}" -f $OutRoot)

$rows = @()
foreach ($t in $Types) {
    Write-Host ""
    Write-Host ("=== --spec-type $t ===")
    Stop-Server
    $log  = Join-Path $OutRoot ("server-$t.log")
    $json = Join-Path $OutRoot ("scaling-$t.json")
    $proc = Start-Server -SpecType $t -LogPath $log

    # NOT Out-Null. The sub-script prints one line per run, and those lines are the only place
    # a rising or falling sequence is visible. Throwing them away once already cost the
    # explanation of a 152 % spread.
    $scalingLog = Join-Path $OutRoot ("scaling-$t.log")
    & $Scaling -Port $Port -Concurrency 1 -Repeats $Repeats -Tokens $Tokens -OutJson $json |
        Tee-Object -FilePath $scalingLog | Out-Null

    if (-not (Test-Path $json)) { throw "no result json for --spec-type $t" }
    $r = (Get-Content $json -Raw | ConvertFrom-Json).rows
    if ($r -is [array]) { $r = $r[0] }
    $rows += [pscustomobject]@{
        Type       = $t
        Median     = [double]$r.Median
        Min        = [double]$r.Min
        Max        = [double]$r.Max
        SpreadPct  = [double]$r.Spread_pct
        Drafted    = [int]$r.Drafted
        Accepted   = [int]$r.Accepted
    }
    Write-Host ("  -> median {0,6:N2} tok/s, spread {1,4:N1} %, drafted {2}, accepted {3}" -f `
        [double]$r.Median, [double]$r.Spread_pct, [int]$r.Drafted, [int]$r.Accepted)
}

Stop-Server

# ---------------------------------------------------------------- the comparison

$base = $rows | Where-Object { $_.Type -eq $Types[0] } | Select-Object -First 1

Write-Host ""
Write-Host "=================================================================="
Write-Host ("RESULT - baseline is --spec-type {0}" -f $base.Type)
Write-Host "=================================================================="
Write-Host ("  {0,-14} {1,8} {2,7} {3,8} {4,10}  {5}" -f `
    'spec-type', 'median', 'factor', 'spread', 'accepted', 'verdict')
foreach ($r in $rows) {
    $factor = if ($base.Median -gt 0) { $r.Median / $base.Median } else { 0 }
    # The same rule the slot measurement uses: a step counts only if its median clears the
    # baseline's MAX. Anything inside the baseline's own spread is not distinguishable from
    # noise, and printing the bare factor would hide that.
    $verdict =
        if ($r.Type -eq $base.Type) { 'baseline' }
        elseif ($r.Median -gt $base.Max) { 'clears baseline spread' }
        elseif ($r.Median -lt $base.Min) { 'SLOWER than baseline' }
        else { 'inside baseline spread - not distinguishable' }
    $acc = if ($r.Drafted -gt 0) { "{0}/{1}" -f $r.Accepted, $r.Drafted } else { 'no draft' }
    Write-Host ("  {0,-14} {1,8:N2} {2,7:N2} {3,7:N1}% {4,10} {5}" -f `
        $r.Type, $r.Median, $factor, $r.SpreadPct, $acc, $verdict)
}

Write-Host ""
Write-Host "Acceptance, the number that transfers:"
foreach ($r in $rows) {
    if ($r.Type -eq $base.Type) { continue }
    if ($r.Drafted -gt 0) {
        Write-Host ("  {0,-14} {1} of {2} drafted tokens accepted = {3:P1}" -f `
            $r.Type, $r.Accepted, $r.Drafted, ($r.Accepted / $r.Drafted))
    } else {
        Write-Host ("  {0,-14} NO DRAFT - proposed nothing at all. Not an acceptance rate of zero." -f $r.Type)
    }
}
Write-Host ""
Write-Host "  Quoted with the four prompts of measure-slot-scaling.ps1: an n-gram method drafts"
Write-Host "  from repetitions of its context, so this is a statement about the method AND the"
Write-Host "  workload. It is not a quality measurement - #46 is run separately."
Write-Host ""
Write-Host ("Raw: {0}" -f $OutRoot)

exit 0
