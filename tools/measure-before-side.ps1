<#
measure-before-side - the old model's before side, taken once, before it is replaced.

WHY THIS EXISTS. 0.1 switches the model to DeepSeek-V4-Flash-0731. The decode
before-side exists (runs/2026-08-09/pairs, 14.73 tok/s median of three pairs) --
prefill and quality have NO number at the operating point at all: every prefill
figure on record comes from 86-103-token prompts, and the quality series ran at
-c 65536 without --jinja. After the switch the old model leaves the disk, and
with it the last chance to measure. One driver, run once, and robins rule from
the vault: a series of more than one run gets its script BEFORE the series.

WHAT IT MEASURES, three parts, each its own directory and summary so an aborted
run leaves a half that says so and a second run fetches only what is missing:

  quality/    two measure-24-gate runs, four graded tasks each, all ten
              probe-suite tasks used exactly once across warm-up and grading.
              Temperature PINNED to 0.6 -- the value 0.0.6 ships -- because
              probe-suite's default is the manifest's, and that moved to 1.0
              (0731's value) on 2026-08-10. Inheriting it would grade the old
              model at the new model's sampling.
  prefill/    one fresh server, four prompts of growing size (~1k/8k/32k/128k
              tokens), max_tokens=1. The denominator is the SERVER's token
              count from its own print_timing line, not our estimate. Each
              prompt starts with a unique salt so no request shares a prefix
              with its predecessor -- a shared prefix would measure the cache.
  vram/       nvidia-smi memory.used of the prefill server, once right after
              /health (KV for -c 200000 is allocated at startup) and once after
              the largest prompt. Settles the 31,838-vs-32,008-MiB contradiction
              that has two documented values and no run behind either.

EVERY SERVER IS FRESH AND EVERY SERVER IS VERIFIED. #71: decode collapses on a
long-running server, so nothing here reuses one. And a run against the wrong
context is the documented failure of 2026-08-07 (half a measuring day), so
/props must answer n_ctx = 200192 and the model path must contain the expected
name, or the phase refuses to measure. That check is the abort criterion, not
politeness.

THE COUNTER-PROBE (-CounterProbe) runs the prefill series at -c 65536 WITHOUT
--jinja. E5 item 5: it must produce visibly different numbers, or this setup
does not measure the operating point. It writes to prefill-counter/ and never
into the real result.

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM
as ANSI, and a stray non-ASCII character breaks the parse in a misleading way.

Usage:
  measure-before-side.ps1                  # quality + prefill + vram
  measure-before-side.ps1 -OnlyPart prefill
  measure-before-side.ps1 -CounterProbe    # the -c 65536 no-jinja probe only
  measure-before-side.ps1 -Selftest

Exit 0 = every requested part measured and archived. 1 = a part failed.
2 = setup refused (wrong server, existing output, missing binary).
#>
param(
    [string] $Exe        = 'C:\Users\robin\dev\crow-lab\wt-25\build-25\bin\Release\llama-server.exe',
    [string] $Model      = $null,
    [string] $Lab        = 'C:\Users\robin\dev\crow-lab',
    [string] $CROW       = 'C:\Users\robin\dev\Crow',
    [string] $OutRoot    = '',
    # 0.6 and not the manifest default: the manifest carries 1.0 since E11 --
    # 0731's value. This driver grades the model 0.0.6 actually ships, at the
    # sampling 0.0.6 actually uses. Passed to the gate, recorded in summaries.
    [string] $Temperature = '0.6',
    [ValidateSet('', 'quality', 'prefill', 'vram')]
    [string] $OnlyPart   = '',
    [int]    $Port       = 8081,
    # Appended to the operating-point flag set, empty by default so the
    # before-side this driver was written for measures exactly what it measured.
    # It exists for the AFTER side: 0731 ships no Jinja template and its embedded
    # one fails the model's own golden vector 4, so a 0731 run needs
    # --chat-template-file or it prefills a prompt the product never sends.
    [string[]] $ExtraFlags = @(),
    # Server log level. 5 is what the before-side ran and stays the default so
    # that series is reproducible. It is a PARAMETER because it is not free:
    # measured 2026-08-10, the same operating point prefilled 1,374 tokens at
    # 5.05 tok/s under -lv 5 and the product's own start line answers at 62.68 --
    # ~40 debug lines per token cost more than the work they describe. A number
    # taken at -lv 5 does not describe what a user gets.
    [int]    $Verbosity  = 5,
    [switch] $CounterProbe,
    [switch] $Selftest
)

. "$PSScriptRoot\model-paths.ps1"
if (-not $Model) { $Model = Get-ModelPath 'operating-point' }
if (-not $OutRoot) { $OutRoot = Join-Path $CROW ('runs\{0}\before-0731' -f (Get-Date -Format 'yyyy-MM-dd')) }

$ErrorActionPreference = 'Continue'
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

# The operating point, spelled once (manifests/operating-point.json is the
# source; check_operating_point.py holds this file against it via install.ps1
# and README -- these flags are the same set the 2026-08-09 pairs ran, plus the
# tier as a FLAG, which doubles as the still-open #74 check: the server log must
# print "L2 host tier" from the flag path.
$OP_FLAGS = @('--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8',
              '--moe-stream-direct','--jinja','--moe-stream-l2','32') + $ExtraFlags
$OP_CTX   = 200000
$WANT_NCTX = 200192   # what /props answers for -c 200000; the 2026-08-07 guard

# All ten tasks, each exactly once. A task that appears twice shares cache state
# with its first run and stops being an independent measurement.
$QUALITY = @(
    @{ label = 'quality-a'; warm = @('two-sum');  graded = @('is-balanced','rotate-matrix','longest-common-prefix','group-anagrams') },
    @{ label = 'quality-b'; warm = @('flatten');  graded = @('binary-search','rle-encode','merge-intervals','top-k-frequent') }
)

# ---------------------------------------------------------------- value functions

function Read-PrefillLines {
    param([string]$Text)
    # slot print_timing: ... prompt eval time =  5134.98 ms /   100 tokens
    $rows = @()
    foreach ($line in ($Text -split "`n")) {
        if ($line -match 'prompt eval time =\s+([\d.]+) ms /\s+(\d+) tokens') {
            $ms = [double]$Matches[1]; $tok = [int]$Matches[2]
            if ($ms -gt 0 -and $tok -gt 0) {
                $rows += [pscustomobject]@{ tokens = $tok; ms = $ms; tok_s = $tok/($ms/1000.0) }
            }
        }
    }
    # ,@() so one row stays an array -- the one-element unwrap ate a .Count in
    # pack-release.ps1 on 2026-08-10 and would eat rows[0] here.
    return ,@($rows)
}

function Test-PropsAnswer {
    param($Props, [string]$ModelPath, [int]$WantCtx)
    # Refuses on the wrong server rather than measuring it. Both halves carry
    # their evidence in the message because "wrong server" alone does not say
    # which of the two guards tripped.
    if ($null -eq $Props) { return 'no /props answer at all' }
    $nctx = $Props.default_generation_settings.n_ctx
    if ($nctx -ne $WantCtx) { return ('n_ctx is {0}, the operating point answers {1}' -f $nctx, $WantCtx) }
    $leaf = Split-Path $ModelPath -Leaf
    $srvModel = $Props.model_path
    if ($srvModel -and ($srvModel -notlike ('*' + $leaf))) {
        return ('server model is {0}, expected {1}' -f $srvModel, $leaf)
    }
    return $null
}

function Read-VramCsv {
    param([string]$Csv)
    # "31838, 32607" from nvidia-smi --query-gpu=memory.used,memory.total
    if ($Csv -match '^\s*(\d+)\s*,\s*(\d+)\s*$') {
        return [pscustomobject]@{ used_mib = [int]$Matches[1]; total_mib = [int]$Matches[2] }
    }
    return $null
}

function New-SaltedPrompt {
    param([int]$TargetTokens, [string]$Salt)
    # ~1 token per short word for this tokenizer family; the SERVER's count is
    # the denominator, this only has to land in the right region. The salt leads
    # so no two prompts share their first byte -- a shared prefix measures the
    # slot cache, not prefill.
    $unit = 'alpha bravo charlie delta echo foxtrot golf hotel india juliet '
    $words = [int]($TargetTokens * 0.9 / 10)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append("[$Salt] Read the following log and answer with the single word done.`n")
    for ($i = 0; $i -lt $words; $i++) { [void]$sb.Append($unit) }
    return $sb.ToString()
}

# ---------------------------------------------------------------- selftest

if ($Selftest) {
    $pass = 0; $fail = 0
    function Check($Name, $Want, $Got) {
        if ([string]$Want -eq [string]$Got) { $script:pass++; "  PASS  $Name" }
        else { $script:fail++; "  FAIL  $Name  want=$Want got=$Got" }
    }
    Write-Output 'measure-before-side selftest'

    # Task plan invariants, by machine and not by reading the table.
    $all = @(); foreach ($q in $QUALITY) { $all += $q.warm; $all += $q.graded }
    Check 'all ten tasks used'      10 $all.Count
    Check 'no task appears twice'   $all.Count (($all | Select-Object -Unique).Count)

    $log = @"
0.34.754.780 I slot print_timing: id  0 | task 0 | prompt eval time =    5134.98 ms /   100 tokens (   51.35 ms per token,    19.47 tokens per second)
0.35.000.000 I slot print_timing: id  0 | task 1 | prompt eval time =   20000.00 ms /  8000 tokens (    2.50 ms per token,   400.00 tokens per second)
"@
    $rows = Read-PrefillLines -Text $log
    Check 'two prefill lines parse' 2 $rows.Count
    Check 'tokens are the denominator' 8000 $rows[1].tokens
    Check 'tok/s computed'          400 ([math]::Round($rows[1].tok_s,2))
    Check 'no lines -> empty, not null' 0 (Read-PrefillLines -Text 'nothing here').Count
    Check 'zero ms line is dropped' 0 (Read-PrefillLines -Text 'prompt eval time =    0.00 ms /   100 tokens').Count

    # The wrong-server guard, both directions. This is the 2026-08-07 failure.
    $good = [pscustomobject]@{ default_generation_settings = [pscustomobject]@{ n_ctx = 200192 }; model_path = 'C:\x\DeepSeek-V4-Flash-UD-IQ3_XXS-00001-of-00004.gguf' }
    Check 'the right server passes' '' ([string](Test-PropsAnswer -Props $good -ModelPath 'M\DeepSeek-V4-Flash-UD-IQ3_XXS-00001-of-00004.gguf' -WantCtx 200192))
    $bad = [pscustomobject]@{ default_generation_settings = [pscustomobject]@{ n_ctx = 65536 }; model_path = 'x.gguf' }
    Check 'a 65536 server is refused' $true ((Test-PropsAnswer -Props $bad -ModelPath 'x.gguf' -WantCtx 200192) -like 'n_ctx is 65536*')
    Check 'no props is refused'       'no /props answer at all' (Test-PropsAnswer -Props $null -ModelPath 'x' -WantCtx 200192)
    $wrongModel = [pscustomobject]@{ default_generation_settings = [pscustomobject]@{ n_ctx = 200192 }; model_path = 'C:\y\OTHER.gguf' }
    Check 'the wrong model is refused' $true ((Test-PropsAnswer -Props $wrongModel -ModelPath 'M\right.gguf' -WantCtx 200192) -like 'server model is*')

    $v = Read-VramCsv -Csv ' 31838, 32607 '
    Check 'vram csv parses'         31838 $v.used_mib
    Check 'garbage vram -> null'    $null (Read-VramCsv -Csv 'N/A, N/A')

    $p1 = New-SaltedPrompt -TargetTokens 1000 -Salt 'a'
    $p2 = New-SaltedPrompt -TargetTokens 1000 -Salt 'b'
    Check 'salts differ from byte 2' $false ($p1.Substring(0,5) -eq $p2.Substring(0,5))
    Check 'bigger target -> longer prompt' $true ((New-SaltedPrompt -TargetTokens 8000 -Salt 'c').Length -gt $p1.Length)

    Write-Output ''
    Write-Output "$pass passed, $fail failed"
    if ($fail -gt 0) { exit 1 }
    exit 0
}

# ---------------------------------------------------------------- shared lifecycle

if (-not (Test-Path $Exe))   { Write-Output "SETUP ERROR: no server exe at $Exe"; exit 2 }
if (-not (Test-Path $Model)) { Write-Output "SETUP ERROR: model not on disk: $Model"; exit 2 }

function Start-MeasuredServer {
    param([string]$Label, [int]$Ctx, [string[]]$Flags, [string]$ErrFile)
    $srvArgs = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl','99','-np','1','-lv',"$Verbosity") + $Flags
    Say ("[{0}] cmd `"{1}`" {2}" -f $Label, $Exe, ($srvArgs -join ' '))
    $p = Start-Process -FilePath $Exe -ArgumentList $srvArgs -WorkingDirectory $Lab `
                       -RedirectStandardError $ErrFile -RedirectStandardOutput "$ErrFile.out" -PassThru -NoNewWindow
    $deadline = (Get-Date).AddSeconds(900)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        if ($p.HasExited) { Say ("[{0}] server exited early, code {1}" -f $Label, $p.ExitCode); return $null }
        try { $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5; return $p } catch { }
    }
    Say ("[{0}] server never became healthy" -f $Label)
    try { Stop-Process -Id $p.Id -Force } catch { }
    return $null
}

function Stop-MeasuredServer {
    param($Proc)
    if ($Proc -and -not $Proc.HasExited) {
        Stop-Process -Id $Proc.Id -Force
        # The exe holds the model file; the next fresh server wants it released.
        Start-Sleep -Seconds 3
    }
}

function Get-Props {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/props" -TimeoutSec 30 } catch { return $null }
}

function Get-VramRow {
    $csv = & nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits 2>$null
    if ($csv -is [array]) { $csv = $csv[0] }
    return Read-VramCsv -Csv "$csv"
}

$failures = 0
$parts = if ($CounterProbe) { @('prefill-counter') }
         elseif ($OnlyPart) { @($OnlyPart) }
         else { @('quality','prefill','vram') }
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null

# ---------------------------------------------------------------- quality

if ($parts -contains 'quality') {
    $qDir = Join-Path $OutRoot 'quality'
    if (Test-Path $qDir) {
        # Refusal, not overwrite: idempotent is the FILING, not the number. A
        # second run of a quality gate produces different numbers by design.
        Write-Output "REFUSED: $qDir exists. Delete it yourself if you mean to re-measure."
        exit 2
    }
    New-Item -ItemType Directory -Force -Path $qDir | Out-Null
    $gate = Join-Path $CROW 'tools\measure-24-gate.ps1'
    foreach ($q in $QUALITY) {
        Say ("quality: {0}  graded {1}" -f $q.label, ($q.graded -join ' '))
        & $gate -Label $q.label -Exe $Exe -Model $Model -Ctx $OP_CTX -Flags $OP_FLAGS `
                -Temperature $Temperature -HealthTimeoutSec 900 -TimeoutSec 3600 `
                -Warm $q.warm -Only $q.graded -OutRoot (Join-Path $qDir $q.label)
        if ($LASTEXITCODE -ne 0) {
            Say ("quality: {0} exit {1}" -f $q.label, $LASTEXITCODE)
            $failures++
        }
        # #74's flag-path check rides along: the gate server ran with
        # --moe-stream-l2 32 as a FLAG, so its log must carry the tier line.
        $err = Join-Path $qDir ("{0}\{0}.err" -f $q.label)
        if (Test-Path $err) {
            $tier = Select-String -LiteralPath $err -Pattern 'L2 host tier' -Quiet
            Say ("quality: {0}  L2 tier line from the FLAG path: {1}" -f $q.label, $(if ($tier) { 'present' } else { 'MISSING' }))
            if (-not $tier) { $failures++ }
        }
    }
}

# ---------------------------------------------------------------- prefill (and its counter-probe)

foreach ($mode in @('prefill','prefill-counter')) {
    if ($parts -notcontains $mode) { continue }
    $pDir = Join-Path $OutRoot $mode
    if (Test-Path $pDir) { Write-Output "REFUSED: $pDir exists. Delete it yourself if you mean to re-measure."; exit 2 }
    New-Item -ItemType Directory -Force -Path $pDir | Out-Null

    $isCounter = ($mode -eq 'prefill-counter')
    # The counter-probe is E5 item 5 verbatim: -c 65536 WITHOUT --jinja must give
    # visibly different numbers, or this setup does not measure the operating point.
    $ctx   = if ($isCounter) { 65536 } else { $OP_CTX }
    $flags = if ($isCounter) { $OP_FLAGS | Where-Object { $_ -ne '--jinja' } } else { $OP_FLAGS }
    $wantCtx = if ($isCounter) { 65792 } else { $WANT_NCTX }
    $err = Join-Path $pDir 'server.err'

    $proc = Start-MeasuredServer -Label $mode -Ctx $ctx -Flags $flags -ErrFile $err
    if ($null -eq $proc) { $failures++; continue }
    try {
        $props = Get-Props
        $veto = Test-PropsAnswer -Props $props -ModelPath $Model -WantCtx $wantCtx
        if ($veto -and -not $isCounter) {
            # On the REAL run the wrong server is a refusal. On the counter-probe
            # a deviating n_ctx is the POINT; only "no answer" vetoes it.
            Write-Output "REFUSED: $veto"; $failures++; continue
        }
        $vramCold = Get-VramRow
        Say ("{0}: vram after load {1} MiB" -f $mode, $vramCold.used_mib)

        $points = @(1000, 8000, 32000, 128000)
        if ($isCounter) { $points = @(1000, 8000, 32000) }   # 128k does not fit -c 65536
        foreach ($target in $points) {
            $prompt = New-SaltedPrompt -TargetTokens $target -Salt ("{0}-{1}" -f $mode, $target)
            $body = @{ model = 'x'
                       messages = @(@{ role = 'user'; content = $prompt })
                       max_tokens = 1
                       temperature = [double]$Temperature } | ConvertTo-Json -Depth 4 -Compress
            Say ("{0}: sending ~{1}-token prompt" -f $mode, $target)
            try {
                $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post `
                        -ContentType 'application/json' -Body $body -TimeoutSec 3600
            } catch {
                Say ("{0}: request for ~{1} tokens FAILED: {2}" -f $mode, $target, $_.Exception.Message)
                $failures++
            }
        }
        $vramLoaded = Get-VramRow
        Stop-MeasuredServer -Proc $proc; $proc = $null

        $rows = Read-PrefillLines -Text (Get-Content -LiteralPath $err -Raw)
        Say ("{0}: {1} prefill lines from the server log" -f $mode, $rows.Count)
        foreach ($row in $rows) {
            '  {0,7} tokens   {1,10:N1} ms   {2,8:N2} tok/s' -f $row.tokens, $row.ms, $row.tok_s
        }
        if ($rows.Count -lt $points.Count) {
            Say ("{0}: FEWER LINES THAN PROMPTS ({1} of {2}) - a missing line is a failed request, not a fast one" -f $mode, $rows.Count, $points.Count)
            $failures++
        }
        [pscustomobject]@{
            label = $mode; model = $Model; ctx = $ctx; flags = $flags
            temperature = $Temperature
            n_ctx_props = $props.default_generation_settings.n_ctx
            vram_after_load = $vramCold; vram_after_biggest = $vramLoaded
            points = $rows
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $pDir 'summary.json') -Encoding utf8
    } finally {
        Stop-MeasuredServer -Proc $proc
    }
}

# ---------------------------------------------------------------- vram

if ($parts -contains 'vram') {
    # The number itself is taken during the prefill phase (same server, twice).
    # This part only files the verdict against the two documented values.
    $sum = Join-Path $OutRoot 'prefill\summary.json'
    if (-not (Test-Path $sum)) {
        Write-Output 'VRAM: no prefill summary yet - run the prefill part first; the number comes from its server.'
        $failures++
    } else {
        $s = Get-Content -LiteralPath $sum -Raw | ConvertFrom-Json
        $vDir = Join-Path $OutRoot 'vram'
        New-Item -ItemType Directory -Force -Path $vDir | Out-Null
        [pscustomobject]@{
            documented_vault  = 31838
            documented_readme = 32008
            measured_after_load    = $s.vram_after_load.used_mib
            measured_after_biggest = $s.vram_after_biggest.used_mib
            total                  = $s.vram_after_load.total_mib
        } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $vDir 'verdict.json') -Encoding utf8
        Say ("vram: after load {0} MiB, after biggest prompt {1} MiB (documented: 31838 vault / 32008 README)" -f `
             $s.vram_after_load.used_mib, $s.vram_after_biggest.used_mib)
    }
}

# ---------------------------------------------------------------- archive fingerprint

$manifestTool = Join-Path $CROW 'tools\manifest-runs.ps1'
if ((Test-Path $manifestTool) -and -not $CounterProbe -and $failures -eq 0) {
    # (d) of the abort criterion: the runs manifest covers the folder. -Day and
    # -Pattern explicitly: the tool joins RunsDir/Day itself, and its defaults
    # know only eNN directories and 2026-08-05. The first version of this call
    # passed the day directory AS RunsDir and the tool went looking for
    # runs/<today>/2026-08-05 -- a SETUP ERROR that this driver then did not
    # count as a failure, so (d) read as filed and was not.
    & $manifestTool -Day (Split-Path (Split-Path $OutRoot -Parent) -Leaf) -Pattern 'before-0731'
    if ($LASTEXITCODE -ne 0) { $failures++ }
}

Write-Output ''
if ($failures -gt 0) { Write-Output ("RESULT: FAIL - {0} part(s) did not measure; see above" -f $failures); exit 1 }
Write-Output 'RESULT: measured and filed'
exit 0
