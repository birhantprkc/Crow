<#
verify-drafting - E11. Does the loaded drafter actually PRODUCE drafts, and does the
target model verify them?

E10 proved the drafter loads. That is not the same statement. In E10 the server ran with
-md but WITHOUT --spec-type, so speculative.types stayed { NONE } and drafted would have
been zero by construction - the drafter sat in memory and was never asked. E11 sets
--spec-type draft-dspark (common/speculative.cpp:37), which the runtime only activates
when a draft context also exists (:2409). Both are required; either alone proves nothing.

THE NEGATIVE CONTROL IS THE SAME BUILD, not another base. E10 had to compare across
bases because the question was about the base. Here the question is about the drafter,
so the only variable is -md: same binary, same model, same prompt, same seed, same
sampling. Comparing against pristine b10223 again would re-measure the base jump and
answer a question nobody asked.

WHAT "NO DRAFT" LOOKS LIKE, and why it is not a rate of zero. draft_n and
draft_n_accepted are written into the timings object ONLY when draft_n > 0
(tools/server/server-task.cpp:259-262). With no speculation the fields are ABSENT, not
zero. probe-spec-drafting.py already models this correctly - accept_rate becomes None,
never 0.0 - and this tool must not undo that distinction when it judges.

COUNTERS ARE PER REQUEST, checked rather than assumed. n_draft_total and
n_draft_accepted are slot fields cleared on slot reset (server-context.cpp:357-361), and
the acceptance line is printed before that reset. This tool still asserts it at the
object: the per-request numbers must not equal a running sum across the series.

WHAT IS NOT CLAIMED HERE. That the drafter makes the operating point FASTER. That needs
A/B runs with repetitions and separated prefill/decode timings under identical GPU and
cache conditions, and it is a separate step. A throughput figure from this run would mix
a cold first request with warm later ones and would carry the prefill inside it - the
mistake E9 had to unwind.

Usage:
  verify-drafting.ps1
  verify-drafting.ps1 -Repeats 3 -Tokens 512
  verify-drafting.ps1 -Only positive        # diagnostic single run first

Exit 0 = every check green.  1 = at least one red.  2 = setup error.
#>
param(
    [string]$WT      = 'C:\Users\robin\dev\crow-lab\wt-e11',
    [string]$Bin     = 'build-e11\bin\Release',
    [string]$Lab     = 'C:\Users\robin\dev\crow-lab',
    [string]$CROW    = 'C:\Users\robin\dev\Crow',
    [string]$Model   = 'models/UD-Q2_K_XL/DeepSeek-V4-Flash-UD-Q2_K_XL-00001-of-00003.gguf',
    [string]$Drafter = 'models/DSV4-Flash-DSpark-draft-bf16.gguf',
    [string]$SpecType = 'draft-dspark',
    [int]   $Port    = 8081,
    [int]   $Ctx     = 4096,
    [int]   $Ngl     = 99,
    [int]   $Repeats = 3,
    [int]   $Tokens  = 512,
    [int]   $Verbosity = 5,
    [int]   $HealthTimeoutSec = 420,
    [ValidateSet('both','positive','negative')][string]$Only = 'both',
    [string]$OutRoot = ''
)

$ErrorActionPreference = 'Continue'
if (-not $OutRoot) { $OutRoot = Join-Path $CROW ("runs\{0}\e11-drafting" -f (Get-Date -Format 'yyyy-MM-dd')) }
if (-not (Test-Path $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null }

$PRODUCTION = Join-Path $Lab 'src\build-native'
$script:rows = @()
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Note([string]$phase,[string]$name,$want,$got,[bool]$ok) {
    $script:rows += [pscustomobject]@{ Phase=$phase; Check=$name; Want="$want"; Got="$got"; OK=[bool]$ok }
    Say ("  {0,-4} {1,-52} want {2,-18} got {3}" -f $(if($ok){'ok'}else{'RED'}), $name, "$want", "$got")
}
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }
function Lines-Of([string]$p) {
    # FileShare::ReadWrite - the server holds its log open, and ReadAllLines throws on it,
    # leaving an empty array that reads as "nothing found" downstream. Measured in E10.
    if (-not [IO.File]::Exists($p)) { return @() }
    try {
        $fs = [IO.File]::Open($p, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try { $sr = New-Object IO.StreamReader($fs); try { return @($sr.ReadToEnd() -split "`r?`n") } finally { $sr.Dispose() } }
        finally { $fs.Dispose() }
    } catch { Say ("  LOG READ FAILED {0}: {1}" -f $p, $_.Exception.Message); return @() }
}
function Count-Match([string[]]$l,[string]$p) { return @($l | Where-Object { $_ -match $p }).Count }

# PREFILL AND DECODE STAY TWO NUMBERS. tok_per_s in probe-spec-drafting.py is
# completion_tokens over the WHOLE request and therefore contains the prefill - E9 measured
# what that hides: -0.29 % client against -0.66 % decode in the same run. Speculation acts
# on the DECODE only and never on the prefill, so a mixed figure dilutes exactly the effect
# this stage is about. Pulled per run from the server's own timing lines and reported apart.
function Timings-Of([string[]]$lines) {
    $pre = @(); $dec = @()
    foreach ($l in $lines) {
        if ($l -match 'prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens') {
            $pre += [pscustomobject]@{ ms=[double]$Matches[1]; n=[int]$Matches[2] }
        } elseif ($l -match '^\s*.*\beval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens') {
            $dec += [pscustomobject]@{ ms=[double]$Matches[1]; n=[int]$Matches[2] }
        }
    }
    return [pscustomobject]@{ prefill=$pre; decode=$dec }
}
function Report-Timings([string]$tag, [string[]]$lines) {
    $t = Timings-Of $lines
    Say ("  --- {0}: prefill and decode, SEPARATE, context only ---" -f $tag)
    for ($i=0; $i -lt [Math]::Max($t.prefill.Count, $t.decode.Count); $i++) {
        $p = $(if ($i -lt $t.prefill.Count) { $t.prefill[$i] } else { $null })
        $d = $(if ($i -lt $t.decode.Count)  { $t.decode[$i] }  else { $null })
        Say ("  run {0}  PREFILL {1,10} ms / {2,4} tok   DECODE {3,10} ms / {4,4} tok" -f ($i+1),
             $(if($p){'{0:N2}' -f $p.ms}else{'-'}), $(if($p){$p.n}else{'-'}),
             $(if($d){'{0:N2}' -f $d.ms}else{'-'}), $(if($d){$d.n}else{'-'}))
    }
    if ($t.decode.Count -gt 0) {
        $dpt = @($t.decode | ForEach-Object { $_.ms / [Math]::Max($_.n,1) })
        $med = ($dpt | Sort-Object)[[int]([Math]::Floor($dpt.Count/2))]
        Say ("  decode ms/token median: {0:N2}   (NOT a speed claim - see the tool header)" -f $med)
    }
    $t | ConvertTo-Json -Depth 4 | Out-File (Join-Path $OutRoot "$tag-timings.json") -Encoding ascii
    # NO return value: "$null = Report-Timings ..." would swallow every Say above, and the
    # numbers would be collected and thrown away. Measured 2026-08-05 - the prefill/decode
    # block was computed and never printed.
}

$exe = Join-Path $WT (Join-Path $Bin 'llama-server.exe')
if (-not (Test-Path -LiteralPath $exe)) { Die "llama-server not found at $exe - build the E11 tree first" }
$full = (Resolve-Path -LiteralPath $exe).ProviderPath
if ($full.StartsWith($PRODUCTION + '\', [StringComparison]::OrdinalIgnoreCase)) { Die "refusing the production build at $full" }
$fi = Get-Item -LiteralPath $full
$sha = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
$py = 'python'

function Stop-Servers {
    Get-Process llama-server -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    for ($i=0; $i -lt 60; $i++) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Start-Server {
    param([string[]]$ServerArgs, [string]$LogBase)
    ($ServerArgs -join ' ') | Out-File "$LogBase.cmdline.txt" -Encoding ascii
    $p = Start-Process -FilePath $full -ArgumentList $ServerArgs -WorkingDirectory $Lab `
            -RedirectStandardOutput "$LogBase.out" -RedirectStandardError "$LogBase.err" -PassThru -WindowStyle Hidden
    try { $p.EnableRaisingEvents = $true } catch { }   # without it .ExitCode reads empty on PS 5.1
    $actual = $null
    try { $actual = (Get-Process -Id $p.Id -ErrorAction Stop).Path } catch { }
    $healthy = $false
    for ($i=0; $i -lt ($HealthTimeoutSec*2); $i++) {
        $p.Refresh(); if ($p.HasExited) { break }
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3; if ($h.status -eq 'ok') { $healthy = $true; break } } catch { }
        Start-Sleep -Milliseconds 500
    }
    return [pscustomobject]@{ proc=$p; pid=$p.Id; actualExe=$actual; healthy=$healthy; errLog="$LogBase.err"; outLog="$LogBase.out" }
}

$baseArgs = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl',"$Ngl",'-np','1',
              '-lv', "$Verbosity",
              '--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8','--moe-stream-direct')
$posArgs = $baseArgs + @('--spec-type', $SpecType, '-md', $Drafter)
# The ONLY variable is -md. --spec-type stays, because the runtime activates the method
# only when a draft context exists anyway (speculative.cpp:2409) - so leaving it in keeps
# the command lines one token apart instead of two.
$negArgs = $baseArgs + @('--spec-type', $SpecType)

Say ('=' * 78)
Say "E11 DRAFTING PROOF   only=$Only  spec-type=$SpecType  repeats=$Repeats  tokens=$Tokens"
Say ('=' * 78)
Say ("server exe {0}" -f $full)
Say ("           {0} B  {1}  {2}" -f $fi.Length, $fi.LastWriteTime.ToString('o'), $sha)
Say ("cmdline delta positive vs negative: {0}" -f ((Compare-Object $posArgs $negArgs | ForEach-Object { "$($_.SideIndicator) $($_.InputObject)" }) -join '  '))
if (-not (Stop-Servers)) { Die "port $Port still held" }

function Run-Side {
    param([string]$Tag, [string[]]$ServerArgs, [string]$Label)
    Say "PHASE $Tag"
    $logBase = Join-Path $OutRoot $Tag
    $srv = Start-Server -ServerArgs $ServerArgs -LogBase $logBase
    Note $Tag 'process path equals the requested exe' 'match' `
        $(if($srv.actualExe -and $srv.actualExe.Equals($full,[StringComparison]::OrdinalIgnoreCase)){'match'}else{"$($srv.actualExe)"}) `
        ($null -ne $srv.actualExe -and $srv.actualExe.Equals($full,[StringComparison]::OrdinalIgnoreCase))
    Note $Tag 'server reached the ready state' $true $srv.healthy $srv.healthy
    if (-not $srv.healthy) { $null = Stop-Servers; return $null }

    $outDir = Join-Path $OutRoot "$Tag-probe"
    & $py (Join-Path $CROW 'tools\probe-spec-drafting.py') --url "http://127.0.0.1:$Port" --out $outDir `
        --repeats $Repeats --tokens $Tokens --label $Label --server-log $srv.errLog *> (Join-Path $OutRoot "$Tag-probe.log")
    $probeRc = $LASTEXITCODE
    Note $Tag 'probe-spec-drafting exit code' 0 $probeRc ($probeRc -eq 0)

    $sum = Join-Path $outDir 'summary.json'
    if (-not (Test-Path $sum)) { Note $Tag 'probe summary written' 'present' 'MISSING' $false; $null = Stop-Servers; return $null }
    $doc = Get-Content $sum -Raw | ConvertFrom-Json
    $null = Stop-Servers
    return [pscustomobject]@{ doc=$doc; srv=$srv; errLog=$srv.errLog }
}

$pos = $null; $neg = $null
if ($Only -ne 'negative') { $pos = Run-Side -Tag 'positive' -ServerArgs $posArgs -Label "spec-type=$SpecType +md" }
if ($Only -ne 'positive') { $neg = Run-Side -Tag 'negative' -ServerArgs $negArgs -Label "spec-type=$SpecType no-md" }

# ---- judge ------------------------------------------------------------------
if ($pos) {
    $runs = @($pos.doc.runs)
    $drafted  = @($runs | ForEach-Object { [int]$_.drafted })
    $accepted = @($runs | ForEach-Object { [int]$_.accepted })
    $ok = @($runs | Where-Object { $_.drafted -gt 0 }).Count
    Say ("  per-run drafted : {0}" -f ($drafted -join ', '))
    Say ("  per-run accepted: {0}" -f ($accepted -join ', '))
    Note 'positive' 'drafted > 0 in every run' $runs.Count $ok ($ok -eq $runs.Count -and $runs.Count -gt 0)
    $totAcc = ($accepted | Measure-Object -Sum).Sum
    Note 'positive' 'accepted > 0 somewhere (the stronger result)' 'gt 0' $totAcc ($totAcc -gt 0)
    $rates = @($runs | ForEach-Object { $_.accept_rate })
    Say ("  accept_rate     : {0}" -f (($rates | ForEach-Object { if ($null -eq $_) { 'null' } else { "$_" } }) -join ', '))
    $withRate = @($runs | Where-Object { $null -ne $_.accept_rate }).Count
    Note 'positive' 'acceptance rate present with its denominator' $runs.Count $withRate ($withRate -eq $runs.Count)

    # Per request, not cumulative. The FIRST version of this check asked for
    # "not monotonic", which was the wrong question: a deterministic prompt at temp 0
    # yields the identical count every run (measured: 228, 228, 228), and identical is
    # monotonic. It flagged a healthy result. The right question is whether the values
    # ARE the running sum - cumulative counters would read 228, 456, 684.
    # A cumulative counter over N identical deterministic requests reads X, 2X, 3X.
    # Second wrong attempt, kept as a warning: summing the values and comparing does NOT
    # detect that - it compares 456 against 684 and concludes "fine". Compare against the
    # multiple of the first value instead.
    $looksCumulative = ($drafted.Count -gt 1) -and ($drafted[0] -gt 0)
    for ($i=0; $i -lt $drafted.Count; $i++) { if ($drafted[$i] -ne (($i+1) * $drafted[0])) { $looksCumulative = $false } }
    Note 'positive' 'counters are per request, not a running sum' 'per request' `
        $(if($looksCumulative){"CUMULATIVE: $($drafted -join ',')"}else{"per request: $($drafted -join ',')"}) `
        (-not $looksCumulative)

    $pl = Lines-Of $pos.errLog
    $accLines = Count-Match $pl 'draft acceptance ='
    Note 'positive' 'server logged a draft-acceptance line per run' $runs.Count $accLines ($accLines -ge $runs.Count)
    $meanLen = @($runs | Where-Object { $null -ne $_.mean_accepted_len }).Count
    Say ("  mean accepted len present in {0} of {1} runs" -f $meanLen, $runs.Count)
    Report-Timings 'positive' $pl
    $errs = Count-Match $pl 'CUDA error|cudaMalloc failed|failed to load|out of memory|error loading model'
    Note 'positive' 'no CUDA / load errors' 0 $errs ($errs -eq 0)
    $cls = @($runs | ForEach-Object { $_.class })
    Say ("  classes         : {0}" -f ($cls -join ', '))
    $good = @($runs | Where-Object { $_.finish_reason -eq 'stop' }).Count
    Note 'positive' 'every run finished normally' $runs.Count $good ($good -eq $runs.Count)
}
if ($neg) {
    $runs = @($neg.doc.runs)
    $drafted = @($runs | ForEach-Object { [int]$_.drafted })
    Say ("  per-run drafted : {0}" -f ($drafted -join ', '))
    $zero = @($runs | Where-Object { $_.drafted -eq 0 }).Count
    Note 'negative' 'drafted = 0 in every run without -md' $runs.Count $zero ($zero -eq $runs.Count -and $runs.Count -gt 0)
    $nullRate = @($runs | Where-Object { $null -eq $_.accept_rate }).Count
    Note 'negative' 'accept_rate is null, NOT 0.0' $runs.Count $nullRate ($nullRate -eq $runs.Count)
    $nl = Lines-Of $neg.errLog
    $accLines = Count-Match $nl 'draft acceptance ='
    Note 'negative' 'no draft-acceptance line in the log at all' 0 $accLines ($accLines -eq 0)
    Report-Timings 'negative' $nl
    $good = @($runs | Where-Object { $_.finish_reason -eq 'stop' }).Count
    Note 'negative' 'every run still finished normally' $runs.Count $good ($good -eq $runs.Count)
}
if ($pos -and $neg) {
    # the control that makes the negative zeros mean something
    $posDraft = (@($pos.doc.runs | ForEach-Object { [int]$_.drafted }) | Measure-Object -Sum).Sum
    Note 'control' 'the same counter is non-zero on the positive side' 'gt 0' $posDraft ($posDraft -gt 0)
}

Write-Output ''
$script:rows | Format-Table Phase, Check, Want, Got, OK -AutoSize | Out-String -Width 200 | Write-Output
$red = @($script:rows | Where-Object { -not $_.OK })
$total = $script:rows.Count
Say ("raw: {0}" -f $OutRoot)
if ($red.Count -eq 0) {
    Write-Output ("RESULT: PASS - {0} of {0} checks green, scope '{1}'." -f $total, $Only)
    Write-Output "  Claimed: the drafter produces drafts and the target verifies them; without -md the"
    Write-Output "  same build drafts nothing and the counters are ABSENT rather than zero."
    Write-Output "  NOT claimed: that speculation makes the operating point faster. That needs A/B runs"
    Write-Output "  with repetitions and separated prefill/decode timings, and it is a separate step."
    exit 0
} else {
    Write-Output ("RESULT: FAIL - {0} of {1} checks red." -f $red.Count, $total)
    foreach ($r in $red) { Write-Output ("  RED  {0} / {1}: want {2}, got {3}" -f $r.Phase, $r.Check, $r.Want, $r.Got) }
    exit 1
}
