<#
probe-moe-server-stats - do the expert-streaming counters actually come out of llama-server,
labelled by model role, and is what comes out usable as a measurement?

WHY THIS EXISTS. #54 added two calls in server_slot::print_timings(). A green build proves the
calls compiled, not that a block ever reaches the log, not that the two models can be told
apart in it, and not that the numbers can be turned into a per-request figure. Those are four
different statements and this tool takes them one at a time.

THE ONE RULE THIS FILE IS BUILT AROUND, and it is here because it broke twice:

    A FUNCTION IS EITHER AN OUTPUT FUNCTION OR A VALUE FUNCTION, NEVER BOTH.

A PowerShell function returns everything it writes to the success stream. A value function that
also prints therefore hands its caller the transcript with the result buried at the end. It cost
two findings on 2026-08-06: a self-test that reported EXIT=0 without running a single case, and
an Ask() whose timing lines never reached the console while probe-runs.json grew request entries
that were LISTS of strings plus an object. Both looked like missing data and were pollution.
Every function below is therefore in exactly one of two groups, and $VALUE_FUNCS plus the
structural guard in -Selftest keep it that way: the guard walks this file's own syntax tree and
goes red on a Say inside any value function.

WHAT EACH PROBE ANSWERS, each with the control that makes its answer worth something:
  A  target model alone      -> a target block exists AND no drafter block was invented
  C  several requests        -> counters are cumulative, rise, and the difference between two
                                consecutive blocks is the per-request figure; the PID does not
                                change underneath, so it is one instance throughout
  B  target and drafter      -> both blocks, roles unique per request, the draft counters the
                                server already printed are still there, the effective answer is
                                the same one the target-only run produced, and nothing prints twice
  D  default verbosity       -> the server runs, the request succeeds, and NO block appears.
                                The streaming WARN line survives that verbosity, so the log
                                carries its own proof and the verdict is hidden-by-verbosity.

A AND C SHARE ONE SERVER, deliberately: C's question is whether several requests inside ONE
process behave, so its requests must be the same process A measured.

THE EFFECTIVE ANSWER. This model can spend a whole 48-token budget on reasoning and leave
message.content empty - measured 2026-08-06, where "the answer is unchanged" compared two empty
strings and was true for the wrong reason (SHA-256 of "" is e3b0c442...). The comparison source
is therefore chosen explicitly: content when non-empty, otherwise reasoning_content when
non-empty, otherwise the record is REJECTED. Which source was used is written into the record.

THE WARM-UP IS NOT SUBTRACTED. The library warm-up does not run under expert streaming at all,
so nothing precedes the first request. The discarded warm-up REQUEST is inside every later
block, and this tool shows that rather than hiding it: the region from offset 0 contains the
warm-up block, the region from the offset taken after it does not, and both are reported.

TIMES. Decode at this operating point is around 50-90 ms per token and a cold first request
costs far more. Every wait is sized for that, and prefill and decode are printed per request AND
written to the artefact, so a long run reads as a long run rather than a hang.

TRAPS THIS TOOL IS BUILT AROUND, all measured:
  - a foreign llama-server before the run is EVIDENCE that the machine is not in the assumed
    state. It is reported and the run refuses; killing it would destroy the finding.
  - Start-Process -PassThru releases the handle when the process ends and .ExitCode then reads
    empty. EnableRaisingEvents is set immediately after the start.
  - a freshly built tree carries NO CUDA runtime. The three DLLs are copied in and counted.
  - $rows is the check accumulator of this script. Assigning to it inside a probe replaced the
    whole result table with statistics rows and produced three phantom failures.
  - the server holds its log open, so the evaluation runs through parse-moe-stats.ps1, which
    opens with FileShare::ReadWrite. Reading it any other way returns an empty string, and an
    empty string parses as "no blocks" - the exact answer this probe exists to disprove.

Usage:
  probe-moe-server-stats.ps1 -Selftest
  probe-moe-server-stats.ps1
  probe-moe-server-stats.ps1 -Tokens 64 -Probes AC,B,D

Exit 0 = every probe green.  1 = at least one red.  2 = setup or artefact-structure error.
#>
param(
    [string]$Exe     = 'C:\Users\robin\dev\crow-lab\wt-54\build-54\bin\Release\llama-server.exe',
    [string]$Lab     = 'C:\Users\robin\dev\crow-lab',
    [string]$Model   = 'models/UD-Q2_K_XL/DeepSeek-V4-Flash-UD-Q2_K_XL-00001-of-00003.gguf',
    [string]$Drafter = 'models/DSV4-Flash-DSpark-draft-bf16.gguf',
    [string]$CROW    = 'C:\Users\robin\dev\Crow',
    [string]$CudaBin = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin\x64',
    [int]   $Port    = 8081,
    [int]   $Tokens  = 48,
    [int]   $Ctx     = 4096,
    [int]   $Ngl     = 99,
    [int]   $HealthTimeoutSec = 900,
    [int]   $ReqTimeoutSec    = 900,
    [string]$OutRoot = '',
    [string[]]$Probes = @('AC', 'B', 'D'),
    [switch]$Selftest
)

$ErrorActionPreference = 'Continue'

# The two groups. Anything named here must never write to the success stream; the structural
# guard in -Selftest enforces it against this file's own syntax tree.
$script:VALUE_FUNCS = @(
    'Get-Sha', 'Get-Sha16', 'Get-LogLength', 'Get-ProcIdentity', 'Stop-OurServer',
    'Read-Stats', 'Invoke-Ask', 'Start-Server', 'Get-EffectiveAnswer', 'Test-RequestRecord',
    'Test-ProbeArtifact', 'Get-StreamPollution', 'New-ServerArgs'
)
$script:REQUIRED_FIELDS = @(
    'probe', 'role', 'warmup', 'prompt_tokens', 'prompt_eval_ms', 'prefill_ms_per_token',
    'eval_tokens', 'eval_ms', 'decode_ms_per_token', 'completion_tokens',
    'content', 'reasoning_content', 'content_sha', 'reasoning_sha',
    'effective_answer_sha', 'answer_source', 'finish_reason'
)
$script:NUMERIC_FIELDS = @(
    'prompt_tokens', 'prompt_eval_ms', 'prefill_ms_per_token',
    'eval_tokens', 'eval_ms', 'decode_ms_per_token', 'completion_tokens'
)

# =========================================================================== output functions
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }
function Fmt([object]$v, [int]$d) {
    if ($null -eq $v) { return 'null' }
    return [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:F$d}", [double]$v)
}

$script:rows = @()
$script:rc   = 0
function Note([string]$probe, [string]$name, $want, $got, [bool]$ok) {
    $script:rows += [pscustomobject]@{ Probe = $probe; Check = $name; Want = "$want"; Got = "$got"; OK = $ok }
    if (-not $ok) { $script:rc = 1 }
    Say ("  {0,-4} [{1}] {2,-48} want {3,-12} got {4}" -f $(if ($ok) { 'ok' } else { 'RED' }), $probe, $name, "$want", "$got")
}

# The timing evidence, printed HERE and never inside the function that produced it.
function Show-Request([object]$r) {
    if ($null -eq $r) { Say '    (no request record)'; return }
    if (-not $r.ok) { Say ("    {0}: FAILED - {1}" -f $r.label, $r.why); return }
    Say ("    {0}: prefill {1} ms / {2} tok = {3} ms/tok   decode {4} ms / {5} tok = {6} ms/tok   wall {7} s" -f `
         $r.label, (Fmt $r.prompt_eval_ms 1), $r.prompt_tokens, (Fmt $r.prefill_ms_per_token 2),
         (Fmt $r.eval_ms 1), $r.eval_tokens, (Fmt $r.decode_ms_per_token 2), (Fmt $r.wall_s 1))
    Say ("         completion {0} tok, finish {1}, content {2} chars, reasoning {3} chars, hashed source '{4}', sha {5}" -f `
         $r.completion_tokens, $r.finish_reason, $r.content.Length, $r.reasoning_content.Length,
         $r.answer_source, $r.effective_answer_sha)
}

function Show-Volume([string]$Probe, [object]$Stats) {
    if ($null -eq $Stats) { Say '    (no statistics)'; return }
    $det = @($Stats.blockDetail)
    Say ("    volume: {0} blocks, {1} lines, {2} B total; unlabelled statistics lines {3}" -f `
         $det.Count, $Stats.blockLinesTotal, $Stats.blockBytesTotal, $Stats.unlabelledLines)
    $i = 0
    foreach ($b in $det) {
        Say ("      block {0} role {1,-8} {2} lines {3,6} B  complete={4}" -f `
             $i, $(if ($b.role) { $b.role } else { '(none)' }), $b.lines, $b.bytes, $b.complete)
        $i++
    }
    foreach ($role in @($det | ForEach-Object { $_.role } | Sort-Object -Unique)) {
        $g = @($det | Where-Object { $_.role -eq $role })
        Say ("      role {0,-8} {1} blocks, {2} lines, {3} B" -f `
             $(if ($role) { $role } else { '(none)' }), $g.Count,
             ($g | Measure-Object -Property lines -Sum).Sum, ($g | Measure-Object -Property bytes -Sum).Sum)
    }
    $incomplete = $det.Count - @($det | Where-Object { $_.complete }).Count
    Note $Probe 'unlabelled statistics lines' 0 $Stats.unlabelledLines ($Stats.unlabelledLines -eq 0)
    Note $Probe 'incomplete blocks'           0 $incomplete            ($incomplete -eq 0)
}

# =========================================================================== value functions
function Get-Sha([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 'MISSING' }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}
function Get-Sha16([string]$s) {
    $h = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s))
    return (($h | ForEach-Object { '{0:x2}' -f $_ }) -join '').Substring(0, 16)
}
function Get-LogLength([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try { return [long]$fs.Length } finally { $fs.Close() }
    } catch { return 0 }
}

# The answer source, chosen by an explicit rule rather than by whatever happens to be non-empty
# first. An empty string has a constant hash, so falling back to it silently turns "the answers
# match" into a statement about nothing.
function Get-EffectiveAnswer([string]$Content, [string]$Reasoning) {
    if ($null -ne $Content -and $Content.Length -gt 0) {
        return [pscustomobject]@{ ok = $true; source = 'content'; text = $Content; why = '' }
    }
    if ($null -ne $Reasoning -and $Reasoning.Length -gt 0) {
        return [pscustomobject]@{ ok = $true; source = 'reasoning_content'; text = $Reasoning; why = '' }
    }
    return [pscustomobject]@{ ok = $false; source = 'none'; text = ''
                              why = 'content and reasoning_content are both empty: no comparable answer' }
}

# The REAL command line, from the process table rather than from what we meant to pass.
function Get-ProcIdentity([int]$TargetPid) {
    try { $p = Get-CimInstance Win32_Process -Filter "ProcessId = $TargetPid" -ErrorAction Stop }
    catch { return $null }
    if ($null -eq $p) { return $null }
    return [pscustomobject]@{
        pid_ = [int]$p.ProcessId; exe = [string]$p.ExecutablePath
        commandline = [string]$p.CommandLine
        created = $(if ($p.CreationDate) { ([datetime]$p.CreationDate).ToString('o') } else { $null })
    }
}

function Stop-OurServer([object]$Proc, [int]$OnPort) {
    if ($null -eq $Proc) { return [pscustomobject]@{ how = 'no process'; exit_code = $null; port_free = $true } }
    $code = $null
    try { if (-not $Proc.HasExited) { $Proc.Kill() } } catch { }
    try { [void]$Proc.WaitForExit(60000) } catch { }
    try { $Proc.Refresh(); $code = $Proc.ExitCode } catch { }
    $free = $true
    try { if (Get-NetTCPConnection -State Listen -LocalPort $OnPort -ErrorAction SilentlyContinue) { $free = $false } } catch { }
    return [pscustomobject]@{ how = 'killed'; exit_code = $code; port_free = $free }
}

# The evaluation runs as a CHILD PROCESS, not dot-sourced: parse-moe-stats.ps1 owns the reader
# and the block grammar, and sourcing it here would run its main section.
function Read-Stats([string]$Tool, [string]$LogPath, [long]$Offset, [string]$Hint) {
    $out = & $Tool -Log $LogPath -Offset $Offset -StreamingHint $Hint -Json 2>&1
    try { return (($out | Out-String) | ConvertFrom-Json) } catch { return $null }
}

function New-ServerArgs([string]$ModelPath, [int]$Verbosity, [string[]]$Extra, [int]$OnPort, [int]$NCtx, [int]$NGpuLayers) {
    $a = @('-m', $ModelPath, '--host', '127.0.0.1', '--port', "$OnPort", '-c', "$NCtx", '-ngl', "$NGpuLayers", '-np', '1')
    if ($Verbosity -ge 0) { $a += @('-lv', "$Verbosity") }
    $a += @('--moe-stream', '--moe-stream-cache', '64s', '--moe-stream-io-threads', '8', '--moe-stream-direct',
            '--spec-type', 'draft-dspark')
    return ($a + $Extra)
}

function Start-Server([string]$ExePath, [string]$WorkDir, [string[]]$ServerArgs, [string]$LogBase, [int]$OnPort, [int]$TimeoutSec) {
    $err = "$LogBase.err"
    $p = Start-Process -FilePath $ExePath -ArgumentList $ServerArgs -WorkingDirectory $WorkDir `
         -RedirectStandardOutput "$LogBase.out" -RedirectStandardError $err -PassThru -WindowStyle Hidden
    # set IMMEDIATELY: -PassThru releases the handle on exit and .ExitCode then reads empty
    try { $p.EnableRaisingEvents = $true } catch { }
    $healthy = $false
    for ($i = 0; $i -lt ($TimeoutSec * 2); $i++) {
        $p.Refresh()
        if ($p.HasExited) { break }
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$OnPort/health" -TimeoutSec 3; if ($h.status -eq 'ok') { $healthy = $true; break } } catch { }
        Start-Sleep -Milliseconds 500
    }
    return [pscustomobject]@{ proc = $p; err = $err; healthy = $healthy; args = $ServerArgs }
}

# ONE object out, nothing to the console. Everything a reader would want to see is a field.
function Invoke-Ask([int]$MaxTokens, [string]$LogPath, [string]$Label, [string]$Probe, [string]$Role,
                    [bool]$IsWarmup, [int]$OnPort, [int]$TimeoutSec) {
    $offset = Get-LogLength $LogPath
    $body = @{ model = 'x'
               messages = @(@{ role = 'user'; content = 'Write a Python function that reverses a linked list. Code only.' })
               max_tokens = $MaxTokens; temperature = 0; stream = $false } | ConvertTo-Json -Depth 5
    $t0 = Get-Date
    $r = $null; $err = ''
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$OnPort/v1/chat/completions" -Method Post `
             -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSec
    } catch { $err = $_.Exception.Message }
    $t1 = Get-Date
    if ($null -eq $r) {
        return [pscustomobject]@{ ok = $false; label = $Label; probe = $Probe; role = $Role; warmup = $IsWarmup
                                  offset = $offset; why = ("request failed: " + $err) }
    }
    $tm      = $r.timings
    $content = [string]$r.choices[0].message.content
    $reason  = [string]$r.choices[0].message.reasoning_content
    $eff     = Get-EffectiveAnswer $content $reason
    $pn      = [int]$tm.prompt_n
    $en      = [int]$tm.predicted_n
    return [pscustomobject]@{
        ok = $eff.ok; label = $Label; probe = $Probe; role = $Role; warmup = $IsWarmup
        why = $eff.why
        offset = $offset; offset_end = (Get-LogLength $LogPath)
        wall_s = [double][math]::Round(($t1 - $t0).TotalSeconds, 3)
        prompt_tokens = $pn
        prompt_eval_ms = [double]$tm.prompt_ms
        prefill_ms_per_token = $(if ($pn -gt 0) { [double][math]::Round([double]$tm.prompt_ms / $pn, 4) } else { [double]0 })
        eval_tokens = $en
        eval_ms = [double]$tm.predicted_ms
        decode_ms_per_token = $(if ($en -gt 0) { [double][math]::Round([double]$tm.predicted_ms / $en, 4) } else { [double]0 })
        completion_tokens = [int]$r.usage.completion_tokens
        finish_reason = [string]$r.choices[0].finish_reason
        content = $content
        reasoning_content = $reason
        content_sha = Get-Sha16 $content
        reasoning_sha = Get-Sha16 $reason
        effective_answer_sha = $(if ($eff.ok) { Get-Sha16 $eff.text } else { '' })
        answer_source = $eff.source
        drafted  = $(if ($null -ne $tm.draft_n) { [int]$tm.draft_n } else { $null })
        accepted = $(if ($null -ne $tm.draft_n_accepted) { [int]$tm.draft_n_accepted } else { $null })
    }
}

# The artefact is checked BEFORE it is written. A file that mixes strings into its request list
# is not a weaker artefact, it is a different one, and a later reader has no way to tell.
function Test-RequestRecord([object]$r) {
    $p = @()
    if ($null -eq $r) { return @('record is null') }
    if ($r -is [string]) { return @('record is a string, not an object') }
    if ($r -is [array])  { return @('record is an array, not an object') }
    foreach ($f in $script:REQUIRED_FIELDS) {
        if (-not ($r.PSObject.Properties.Name -contains $f)) { $p += "missing field '$f'" }
    }
    foreach ($f in $script:NUMERIC_FIELDS) {
        if ($r.PSObject.Properties.Name -contains $f) {
            $v = $r.$f
            if ($null -eq $v -or -not (($v -is [int]) -or ($v -is [long]) -or ($v -is [double]) -or ($v -is [decimal]))) {
                $p += "field '$f' is not numeric"
            }
        }
    }
    if ($r.PSObject.Properties.Name -contains 'answer_source') {
        if ($r.answer_source -notin @('content', 'reasoning_content')) { $p += "answer_source '$($r.answer_source)' is not a usable source" }
    }
    if ($r.PSObject.Properties.Name -contains 'effective_answer_sha') {
        if ([string]::IsNullOrEmpty($r.effective_answer_sha)) { $p += 'effective_answer_sha is empty' }
        if ($r.effective_answer_sha -eq (Get-Sha16 '')) { $p += 'effective_answer_sha is the hash of the empty string' }
    }
    return $p
}

function Test-ProbeArtifact([object[]]$Runs) {
    $p = @()
    if ($null -eq $Runs -or $Runs.Count -eq 0) { return @('no runs recorded') }
    foreach ($run in $Runs) {
        if (-not ($run.PSObject.Properties.Name -contains 'requests')) { $p += "run '$($run.probe)' has no requests"; continue }
        $reqs = $run.requests
        if ($reqs -isnot [array]) { $p += "run '$($run.probe)': requests is not an array"; continue }
        $i = 0
        foreach ($r in $reqs) {
            foreach ($x in (Test-RequestRecord $r)) { $p += ("run '{0}' request {1}: {2}" -f $run.probe, $i, $x) }
            $i++
        }
    }
    return $p
}

# The structural guard: does any VALUE function write to the success stream? Answered against
# the syntax tree rather than by reading, so a renamed helper cannot slip past.
function Get-StreamPollution([string]$Path, [string[]]$ValueFuncs) {
    $hits = @()
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$null)
    if ($null -eq $ast) { return @("cannot parse $Path") }
    $fns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
    foreach ($f in $fns) {
        if ($ValueFuncs -notcontains $f.Name) { continue }
        $cmds = $f.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
        foreach ($c in $cmds) {
            $name = $c.GetCommandName()
            if ($name -in @('Say', 'Write-Output', 'Write-Host', 'Show-Request', 'Show-Volume', 'Note')) {
                $hits += ("{0} calls {1} at line {2}" -f $f.Name, $name, $c.Extent.StartLineNumber)
            }
        }
    }
    return $hits
}

# =========================================================================== self-test
function Invoke-Selftest {
    $script:cases = @()
    function Case([string]$name, [bool]$pass, [string]$detail) {
        $script:cases += [pscustomobject]@{ Case = $name; OK = $pass; Detail = $detail }
        Say ("  {0,-4} {1,-56} {2}" -f $(if ($pass) { 'ok' } else { 'RED' }), $name, $detail)
    }
    function New-Rec([string]$content, [string]$reason) {
        $eff = Get-EffectiveAnswer $content $reason
        return [pscustomobject]@{
            ok = $eff.ok; label = 'r'; probe = 'A'; role = 'target-only'; warmup = $false; why = $eff.why
            wall_s = [double]1.5
            prompt_tokens = [int]17; prompt_eval_ms = [double]1746.6; prefill_ms_per_token = [double]102.74
            eval_tokens = [int]48; eval_ms = [double]2621.9; decode_ms_per_token = [double]54.62
            completion_tokens = [int]48; finish_reason = 'length'
            content = $content; reasoning_content = $reason
            content_sha = Get-Sha16 $content; reasoning_sha = Get-Sha16 $reason
            effective_answer_sha = $(if ($eff.ok) { Get-Sha16 $eff.text } else { '' })
            answer_source = $eff.source
        }
    }

    Say 'PHASE selftest'
    $self = $PSCommandPath
    $tmp  = [IO.Path]::GetTempPath()

    # 1 - a value function returns exactly one object
    $one = @(Get-EffectiveAnswer 'abc' '')
    Case '1 value function returns exactly one object' ($one.Count -eq 1 -and $one[0].source -eq 'content') `
        ("objects={0} source={1}" -f $one.Count, $one[0].source)

    # 2 - and its internal diagnostics add no pipeline objects
    $two = @(Get-EffectiveAnswer '' '')
    Case '2 diagnostics produce no extra pipeline objects' ($two.Count -eq 1 -and -not $two[0].ok -and $two[0].why.Length -gt 0) `
        ("objects={0} why='{1}'" -f $two.Count, $two[0].why)

    # 3 - requests must contain objects only
    $recOk = New-Rec 'def f(): pass' ''
    $bad = [pscustomobject]@{ probe = 'A'; requests = @('[12:00:00] prefill 1 ms', $recOk) }
    $good = [pscustomobject]@{ probe = 'A'; requests = @($recOk) }
    $pBad  = Test-ProbeArtifact @($bad)
    $pGood = Test-ProbeArtifact @($good)
    Case '3 a string inside requests is rejected' ($pBad.Count -ge 1 -and $pGood.Count -eq 0) `
        ("polluted problems={0}, clean problems={1}" -f $pBad.Count, $pGood.Count)

    # 4 - JSON round trip keeps numbers numeric and every required field
    $rt = ($good | ConvertTo-Json -Depth 8) | ConvertFrom-Json
    $rtProblems = Test-ProbeArtifact @($rt)
    # DECIMAL BELONGS IN THIS LIST. ConvertFrom-Json on PowerShell 5.1 hands back Decimal for
    # every non-integer number, not Double - measured 2026-08-06, where leaving it out made this
    # case report "not numeric" for a perfectly good artefact. A type check that does not know
    # the deserialiser's types tests the checker, not the data.
    $numOk = $true; $numTypes = @()
    foreach ($f in $script:NUMERIC_FIELDS) {
        $v = $rt.requests[0].$f
        $numTypes += ("{0}:{1}" -f $f, $(if ($null -eq $v) { 'NULL' } else { $v.GetType().Name }))
        if ($null -eq $v -or -not (($v -is [int]) -or ($v -is [long]) -or ($v -is [double]) -or ($v -is [decimal]))) { $numOk = $false }
    }
    Case '4 JSON round trip keeps types and required fields' ($rtProblems.Count -eq 0 -and $numOk -and ($rt.requests -is [array])) `
        ("problems={0} numeric_ok={1} array={2}" -f $rtProblems.Count, $numOk, ($rt.requests -is [array]))

    # 5 - non-empty content wins
    $r5 = New-Rec 'def f(): pass' 'thinking about it'
    Case '5 non-empty content is the answer source' ($r5.answer_source -eq 'content' -and $r5.effective_answer_sha -eq (Get-Sha16 'def f(): pass')) `
        ("source={0}" -f $r5.answer_source)

    # 6 - empty content falls back to reasoning
    $r6 = New-Rec '' 'thinking about it'
    Case '6 empty content falls back to reasoning_content' ($r6.answer_source -eq 'reasoning_content' -and $r6.effective_answer_sha -eq (Get-Sha16 'thinking about it')) `
        ("source={0}" -f $r6.answer_source)

    # 7 - both empty is red, and specifically NOT the empty-string hash
    $r7 = New-Rec '' ''
    $p7 = Test-RequestRecord $r7
    Case '7 both empty is rejected, not hashed as ""' ((-not $r7.ok) -and $p7.Count -ge 1 -and $r7.effective_answer_sha -ne (Get-Sha16 '')) `
        ("ok={0} problems={1}" -f $r7.ok, $p7.Count)

    # 8 - different reasoning must not collapse to one hash
    $r8a = New-Rec '' 'answer one'
    $r8b = New-Rec '' 'answer two'
    Case '8 different reasoning gives different hashes' ($r8a.effective_answer_sha -ne $r8b.effective_answer_sha) `
        ("{0} vs {1}" -f $r8a.effective_answer_sha, $r8b.effective_answer_sha)

    # 9 - prefill and decode reach BOTH the console and the artefact
    $printed = @(Show-Request $recOk) -join "`n"
    $inJson = ((($good | ConvertTo-Json -Depth 8) | ConvertFrom-Json).requests[0].prefill_ms_per_token -gt 0) -and
              ((($good | ConvertTo-Json -Depth 8) | ConvertFrom-Json).requests[0].decode_ms_per_token -gt 0)
    Case '9 prefill and decode on the console AND in the artefact' `
        (($printed -match 'prefill') -and ($printed -match 'decode') -and $inJson) `
        ("console={0} json={1}" -f (($printed -match 'prefill') -and ($printed -match 'decode')), $inJson)

    # 10 - $rows cannot be overwritten by a probe body
    $srcText = [IO.File]::ReadAllText($self)
    $rowsAssign = ([regex]::Matches($srcText, '(?m)^\s*\$rows\s*=')).Count
    Case '10 nothing assigns to the check accumulator $rows' ($rowsAssign -eq 0) ("assignments={0}" -f $rowsAssign)

    # 11 / 12 - the verbosity verdict, through the real parser on real-shaped logs
    $parser = Join-Path $PSScriptRoot 'parse-moe-stats.ps1'
    $logWarn = Join-Path $tmp 'moe-probe-selftest-warn.log'
    $logBare = Join-Path $tmp 'moe-probe-selftest-bare.log'
    [IO.File]::WriteAllText($logWarn, "0.00.346.577 W llama_model_load: disabling mmap because MoE expert streaming is enabled (mmap -> none)`n0.10.000.000 I srv  update_slots: all slots are idle`n")
    [IO.File]::WriteAllText($logBare, "0.10.000.000 I srv  update_slots: all slots are idle`n")
    $k11 = Read-Stats $parser $logWarn 0 'unknown'
    $k12 = Read-Stats $parser $logBare 0 'unknown'
    Case '11 streaming WARN + no block = hidden-by-verbosity' `
        ($null -ne $k11 -and $k11.kind -eq 'hidden-by-verbosity' -and $k11.blocks -eq 0) `
        ("kind={0} blocks={1}" -f $(if ($k11) { $k11.kind } else { 'null' }), $(if ($k11) { $k11.blocks } else { '-' }))
    Case '12 no WARN and no external evidence = no-streaming-evidence' `
        ($null -ne $k12 -and $k12.kind -eq 'no-streaming-evidence') `
        ("kind={0}" -f $(if ($k12) { $k12.kind } else { 'null' }))
    foreach ($f in @($logWarn, $logBare)) { if (Test-Path $f) { Remove-Item $f -Force } }

    # 13 - THE STRUCTURAL GUARD on this file
    $pollution = Get-StreamPollution $self $script:VALUE_FUNCS
    Case '13 no value function writes to the success stream' ($pollution.Count -eq 0) `
        ("hits={0}{1}" -f $pollution.Count, $(if ($pollution.Count) { ' -> ' + ($pollution -join '; ') } else { '' }))

    # 14 - and the guard's own negative control: inject one Say and it must go red
    # The anchor is the first BODY line of Invoke-Ask, not its signature: the signature wraps
    # over two lines, so a pattern ending at the opening brace never matched and the control
    # reported "no hits" over a file it had never modified. The case therefore asserts the
    # injection happened before it judges the guard.
    $copy = Join-Path $tmp 'moe-probe-guard-negative.ps1'
    $anchor = '    $offset = Get-LogLength $LogPath'
    $injected = $srcText.Replace($anchor, ($anchor + "`r`n    Say 'injected for the negative control'"))
    $didInject = ($injected -ne $srcText)
    [IO.File]::WriteAllText($copy, $injected)
    $negHits = Get-StreamPollution $copy $script:VALUE_FUNCS
    Case '14 the guard goes red on an injected Say' ($didInject -and $negHits.Count -ge 1) `
        ("injected={0} hits={1}" -f $didInject, $negHits.Count)
    if (Test-Path $copy) { Remove-Item $copy -Force }

    Say ('-' * 78)
    $bad2 = @($script:cases | Where-Object { -not $_.OK })
    Say ("selftest: {0} of {1} cases green" -f ($script:cases.Count - $bad2.Count), $script:cases.Count)
    # The function EXITS, it does not return: a returned value would carry this transcript.
    if ($script:cases.Count -eq 0) { Say 'selftest: NO CASE RAN - red rather than an empty green'; exit 1 }
    if ($bad2.Count -gt 0) { foreach ($b in $bad2) { Say ("  RED  {0}: {1}" -f $b.Case, $b.Detail) }; exit 1 }
    exit 0
}

if ($Selftest) { Invoke-Selftest }

# =========================================================================== setup
$PARSER = Join-Path $PSScriptRoot 'parse-moe-stats.ps1'
if (-not (Test-Path $PARSER)) { Die "no parser at $PARSER" }
if (-not (Test-Path $Exe))    { Die "no server exe at $Exe" }
$BIN = Split-Path -Parent $Exe

$copied = 0
foreach ($d in @('cudart64_13.dll', 'cublas64_13.dll', 'cublasLt64_13.dll')) {
    $dst = Join-Path $BIN $d
    if (-not [IO.File]::Exists($dst)) {
        $src = Join-Path $CudaBin $d
        if ([IO.File]::Exists($src)) { Copy-Item $src $dst -Force; $copied++ }
    }
}
$haveRt = @(@('cudart64_13.dll', 'cublas64_13.dll', 'cublasLt64_13.dll') | Where-Object { [IO.File]::Exists((Join-Path $BIN $_)) }).Count
if ($haveRt -ne 3) { Die "CUDA runtime next to the exe: $haveRt of 3 (copied $copied this run)" }

foreach ($m in @($Model, $Drafter)) {
    $full = Join-Path $Lab ($m -replace '/', '\')
    if (-not (Test-Path $full)) { Die "model not found: $full" }
}
$pre = @(Get-Process llama-server -ErrorAction SilentlyContinue)
if ($pre.Count -gt 0) {
    Die ("a llama-server is already running (pid " + (($pre | ForEach-Object { $_.Id }) -join ',') + ") - not touching it, and not measuring against it")
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { Die "port $Port is already held" }

if (-not $OutRoot) { $OutRoot = Join-Path $CROW ("runs\{0}\e54-server-stats" -f (Get-Date -Format 'yyyy-MM-dd')) }
if (-not (Test-Path $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null }

$shaExe = Get-Sha $Exe
$shaDll = Get-Sha (Join-Path $BIN 'llama-server-impl.dll')

Say ('=' * 78)
Say "MoE server statistics probe   tokens $Tokens   port $Port"
Say "exe  $Exe"
Say "sha  $shaExe"
Say "impl $shaDll"
Say "out  $OutRoot"
Say ("CUDA runtime DLLs next to the exe: {0} of 3 ({1} copied in this run)" -f $haveRt, $copied)
Say ('=' * 78)

$runs = @()
$shaTargetOnly = ''

# =========================================================================== probe A + C
if ($Probes -contains 'AC') {
    Say 'PROBE A/C  target model only, -lv 5, warm-up + two measured requests'
    $sArgs = New-ServerArgs $Model 5 @() $Port $Ctx $Ngl
    $s = Start-Server $Exe $Lab $sArgs (Join-Path $OutRoot 'probeAC') $Port $HealthTimeoutSec
    if (-not $s.healthy) {
        Note 'A' 'server became ready' $true $false $false
        [void](Stop-OurServer $s.proc $Port)
    } else {
        $id = Get-ProcIdentity $s.proc.Id
        Note 'A' 'server ready and identified' 'pid+cmd' $(if ($id) { "pid $($id.pid_)" } else { 'no row' }) ($null -ne $id)
        Say ("    cmd {0}" -f $(if ($id) { $id.commandline } else { '-' }))

        $warm = Invoke-Ask $Tokens $s.err 'warm-up (discarded)' 'A/C' 'target-only' $true $Port $ReqTimeoutSec
        Show-Request $warm
        $offAfterWarm = Get-LogLength $s.err
        $r1 = Invoke-Ask $Tokens $s.err 'request 1' 'A/C' 'target-only' $false $Port $ReqTimeoutSec
        Show-Request $r1
        $r2 = Invoke-Ask $Tokens $s.err 'request 2' 'A/C' 'target-only' $false $Port $ReqTimeoutSec
        Show-Request $r2
        $idAfter = Get-ProcIdentity $s.proc.Id

        $statsAll  = Read-Stats $PARSER $s.err 0 'on'
        $statsPost = Read-Stats $PARSER $s.err $offAfterWarm 'on'
        $stop = Stop-OurServer $s.proc $Port

        $allOk = ($warm.ok -and $r1.ok -and $r2.ok)
        Note 'A' 'all three requests succeeded, answers comparable' $true $allOk $allOk
        Note 'A' 'effective answer source named'  'content|reasoning_content' $r1.answer_source ($r1.answer_source -in @('content','reasoning_content'))
        if ($null -eq $statsAll) {
            Note 'A' 'statistics parsed' $true $false $false
        } else {
            $roles = @($statsAll.roles)
            Note 'A' 'a target block exists'          $true ($roles -contains 'target') ($roles -contains 'target')
            Note 'A' 'NO drafter block without -md'   $false ($roles -contains 'drafter') (-not ($roles -contains 'drafter'))
            Note 'A' 'region classified as blocks'    'blocks' $statsAll.kind ($statsAll.kind -eq 'blocks')
            Note 'A' 'every block complete'           $statsAll.blocks $statsAll.blocksComplete ($statsAll.blocks -eq $statsAll.blocksComplete -and $statsAll.blocks -gt 0)
            Note 'A' 'one block per finished request' 3 $statsAll.blocks ($statsAll.blocks -eq 3)
            $firstRow = @($statsAll.rows)[0]
            Note 'A' 'counters are not empty' 'calls>0' $(if ($firstRow) { $firstRow.n_calls } else { 'none' }) ($null -ne $firstRow -and $firstRow.n_calls -gt 0)

            # NOT $rows - that is the check accumulator of this script.
            $statRows = @($statsAll.rows)
            Note 'C' 'three requests, three blocks in one process' 3 $statsAll.blocks ($statsAll.blocks -eq 3)
            Note 'C' 'PID unchanged across the requests' $s.proc.Id $(if ($idAfter) { $idAfter.pid_ } else { 'gone' }) ($null -ne $idAfter -and $idAfter.pid_ -eq $s.proc.Id)
            Note 'C' 'no problems reported by the evaluation' 0 @($statsAll.problems).Count (@($statsAll.problems).Count -eq 0)
            $rising = $true
            for ($i = 1; $i -lt $statRows.Count; $i++) { if ($statRows[$i].n_calls -lt $statRows[$i-1].n_calls) { $rising = $false } }
            Note 'C' 'cumulative counters never fall' $true $rising $rising
            $withDelta = @($statRows | Where-Object { $null -ne $_.d_calls })
            Note 'C' 'request-local deltas computed' 2 $withDelta.Count ($withDelta.Count -eq 2)
            $posDelta = @($withDelta | Where-Object { $_.d_calls -gt 0 })
            Note 'C' 'each delta is a real request, not zero' $withDelta.Count $posDelta.Count ($posDelta.Count -eq $withDelta.Count)
            $negDelta = @($withDelta | Where-Object { $_.d_calls -lt 0 -or $_.d_miss -lt 0 })
            Note 'C' 'no negative delta' 0 $negDelta.Count ($negDelta.Count -eq 0)
            Note 'C' 'warm-up block dropped by the offset' ($statsAll.blocks - 1) $statsPost.blocks ($statsPost.blocks -eq ($statsAll.blocks - 1))
            Say ("    from offset 0: {0} blocks; from behind the warm-up: {1} blocks" -f $statsAll.blocks, $statsPost.blocks)
            foreach ($r in $statRows) {
                Say ("    req {0} role {1,-8} calls {2,-8} miss {3,-8} cold {4,-8} d_calls {5,-8} d_miss {6}" -f `
                     $r.request, $r.role, $r.n_calls, $r.n_miss, $r.n_miss_cold,
                     $(if ($null -eq $r.d_calls) { '-' } else { $r.d_calls }), $(if ($null -eq $r.d_miss) { '-' } else { $r.d_miss }))
            }
            Show-Volume 'A/C' $statsAll
        }
        $shaTargetOnly = $r1.effective_answer_sha
        $runs += [pscustomobject]@{ probe = 'A/C'; args = $s.args; identity = $id; identity_after = $idAfter
                                    stop = $stop; sha_exe = $shaExe; sha_impl = $shaDll
                                    requests = @($warm, $r1, $r2); stats_all = $statsAll; stats_after_warmup = $statsPost }
    }
}

# =========================================================================== probe B
if ($Probes -contains 'B') {
    Say 'PROBE B  target and draft model, -lv 5'
    $sArgs = New-ServerArgs $Model 5 @('-md', $Drafter) $Port $Ctx $Ngl
    $s = Start-Server $Exe $Lab $sArgs (Join-Path $OutRoot 'probeB') $Port $HealthTimeoutSec
    if (-not $s.healthy) {
        Note 'B' 'server became ready' $true $false $false
        [void](Stop-OurServer $s.proc $Port)
    } else {
        $id = Get-ProcIdentity $s.proc.Id
        Note 'B' 'server ready and identified' 'pid+cmd' $(if ($id) { "pid $($id.pid_)" } else { 'no row' }) ($null -ne $id)
        Say ("    cmd {0}" -f $(if ($id) { $id.commandline } else { '-' }))

        $warm = Invoke-Ask $Tokens $s.err 'warm-up (discarded)' 'B' 'target+drafter' $true $Port $ReqTimeoutSec
        Show-Request $warm
        $offAfterWarm = Get-LogLength $s.err
        $r1 = Invoke-Ask $Tokens $s.err 'request 1' 'B' 'target+drafter' $false $Port $ReqTimeoutSec
        Show-Request $r1
        $stats = Read-Stats $PARSER $s.err $offAfterWarm 'on'
        $stop = Stop-OurServer $s.proc $Port

        Note 'B' 'both requests succeeded, answers comparable' $true ($warm.ok -and $r1.ok) ($warm.ok -and $r1.ok)
        if ($null -eq $stats) {
            Note 'B' 'statistics parsed' $true $false $false
        } else {
            $roles = @($stats.roles)
            Note 'B' 'target block present'  $true ($roles -contains 'target')  ($roles -contains 'target')
            Note 'B' 'drafter block present' $true ($roles -contains 'drafter') ($roles -contains 'drafter')
            Note 'B' 'exactly two blocks for one request' 2 $stats.blocks ($stats.blocks -eq 2)
            Note 'B' 'one request, not two'  1 $stats.requests ($stats.requests -eq 1)
            Note 'B' 'roles unique, no duplicate output' 0 @($stats.problems).Count (@($stats.problems).Count -eq 0)
            Note 'B' 'every block complete'  $stats.blocks $stats.blocksComplete ($stats.blocks -eq $stats.blocksComplete -and $stats.blocks -gt 0)
            $t = @($stats.rows | Where-Object { $_.role -eq 'target' })[0]
            $d = @($stats.rows | Where-Object { $_.role -eq 'drafter' })[0]
            Note 'B' 'the two blocks carry different numbers' 'differ' `
                 $(if ($t -and $d) { "$($t.n_calls) vs $($d.n_calls)" } else { 'missing' }) `
                 ($null -ne $t -and $null -ne $d -and $t.n_calls -ne $d.n_calls)
            foreach ($r in @($stats.rows)) {
                Say ("    req {0} role {1,-8} calls {2,-8} hit {3,-8} miss {4,-8} cold {5}" -f `
                     $r.request, $r.role, $r.n_calls, $r.n_hit, $r.n_miss, $r.n_miss_cold)
            }
            Show-Volume 'B' $stats
        }
        Note 'B' 'draft counters still reported by the server' 'drafted>0' $r1.drafted ($null -ne $r1.drafted -and $r1.drafted -gt 0)
        Note 'B' 'the compared answer has content' 'source named' $r1.answer_source ($r1.answer_source -in @('content','reasoning_content'))
        if ($shaTargetOnly) {
            Note 'B' 'effective answer identical to the target-only run' $shaTargetOnly $r1.effective_answer_sha ($r1.effective_answer_sha -eq $shaTargetOnly)
        }
        $runs += [pscustomobject]@{ probe = 'B'; args = $s.args; identity = $id; stop = $stop
                                    sha_exe = $shaExe; sha_impl = $shaDll
                                    requests = @($warm, $r1); stats = $stats }
    }
}

# =========================================================================== probe D
if ($Probes -contains 'D') {
    Say 'PROBE D  same build, DEFAULT verbosity - the visibility limit'
    $sArgs = New-ServerArgs $Model -1 @() $Port $Ctx $Ngl
    $s = Start-Server $Exe $Lab $sArgs (Join-Path $OutRoot 'probeD') $Port $HealthTimeoutSec
    if (-not $s.healthy) {
        Note 'D' 'server became ready' $true $false $false
        [void](Stop-OurServer $s.proc $Port)
    } else {
        $id = Get-ProcIdentity $s.proc.Id
        Note 'D' 'server ready and identified' 'pid+cmd' $(if ($id) { "pid $($id.pid_)" } else { 'no row' }) ($null -ne $id)
        $r1 = Invoke-Ask $Tokens $s.err 'request 1' 'D' 'target-only' $false $Port $ReqTimeoutSec
        Show-Request $r1
        $stats      = Read-Stats $PARSER $s.err 0 'on'
        $statsBlind = Read-Stats $PARSER $s.err 0 'unknown'
        $stop = Stop-OurServer $s.proc $Port

        Note 'D' 'request succeeded, answer comparable' $true $r1.ok $r1.ok
        $cmdProof = $(if ($id -and $id.commandline -match '--moe-stream') { 1 } else { 0 })
        Note 'D' 'command line proves streaming' 1 $cmdProof ($cmdProof -eq 1)
        if ($null -eq $stats) {
            Note 'D' 'log readable' $true $false $false
        } else {
            Note 'D' 'no statistics lines at default verbosity' 0 $stats.moeLines ($stats.moeLines -eq 0)
            Note 'D' 'no blocks'                               0 $stats.blocks   ($stats.blocks -eq 0)
            Note 'D' 'classified as a visibility limit'        'hidden-by-verbosity' $stats.kind ($stats.kind -eq 'hidden-by-verbosity')
            Note 'D' 'NOT classified as streaming being off'   'not streaming-off' $stats.kind ($stats.kind -ne 'streaming-off')
            # The premise this probe started from was wrong and the log corrected it: the
            # streaming load line is a WARNING, and warnings are not demoted the way library
            # INFO is, so it survives the default verbosity. The log carries its own proof and
            # the verdict is the same with or without the caller's hint. The
            # 'no-streaming-evidence' branch stays reachable - the self-test covers it on a log
            # that has no WARN line - it is simply not this configuration.
            $warnMarker = @(Select-String -Path $s.err -Pattern 'MoE expert streaming' -AllMatches).Count
            Note 'D' 'WARN-level streaming marker survives default verbosity' 'ge 1' $warnMarker ($warnMarker -ge 1)
            Note 'D' 'verdict the same with and without the command-line hint' $stats.kind $statsBlind.kind ($statsBlind.kind -eq $stats.kind)
            Show-Volume 'D' $stats
        }
        $runs += [pscustomobject]@{ probe = 'D'; args = $s.args; identity = $id; stop = $stop
                                    sha_exe = $shaExe; sha_impl = $shaDll
                                    requests = @($r1); stats = $stats; stats_blind = $statsBlind }
    }
}

# =========================================================================== report
Say ('=' * 78)
$rows | Format-Table Probe, Check, Want, Got, OK -AutoSize | Out-String -Width 200 | Write-Output

# THE ARTEFACT IS CHECKED BEFORE IT IS WRITTEN. A file whose request list mixes strings into the
# objects is not a weaker artefact, it is a different one, and no later reader can tell.
$artefactProblems = Test-ProbeArtifact $runs
$summary = [pscustomobject]@{
    exe = $Exe; shaExe = $shaExe; shaImplDll = $shaDll
    tokens = $Tokens; port = $Port; outRoot = $OutRoot
    probes = $Probes; checks = $rows.Count
    red = @($rows | Where-Object { -not $_.OK }).Count
    requiredFields = $script:REQUIRED_FIELDS
    artefactProblems = $artefactProblems
    runs = $runs
}
if ($artefactProblems.Count -gt 0) {
    $rej = Join-Path $OutRoot 'probe-runs.rejected.json'
    $summary | ConvertTo-Json -Depth 10 | Out-File $rej -Encoding utf8
    Say ("ARTEFACT REJECTED - {0} structural problems, written to {1} instead:" -f $artefactProblems.Count, $rej)
    foreach ($p in $artefactProblems) { Say "  $p" }
    exit 2
}
$summary | ConvertTo-Json -Depth 10 | Out-File (Join-Path $OutRoot 'probe-runs.json') -Encoding utf8
Say ("artefact structure verified: {0} runs, every request an object with {1} required fields" -f $runs.Count, $script:REQUIRED_FIELDS.Count)
Say ("raw logs and probe-runs.json in {0}" -f $OutRoot)

if ($rows.Count -eq 0) { Say 'RESULT: red - no check ran at all'; exit 1 }
if ($script:rc -eq 0) { Say ("RESULT: green - {0} checks" -f $rows.Count) }
else { Say ("RESULT: red - {0} of {1} checks failed" -f $summary.red, $rows.Count) }
exit $script:rc
