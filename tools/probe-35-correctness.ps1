<#
probe-35-correctness - does this model give the SAME answer with experts on CUDA as with experts
on the CPU, using ONE binary for both runs?

WHY THIS EXISTS. #33 reports that deepseek4 emits garbage when MoE experts run on CUDA, and that
`--n-cpu-moe 999` is correct. Every throughput number in this project comes from a CUDA expert
path, and a tokens/s figure out of a configuration that emits garbage is not a measurement. #35
tried the two documented workarounds; both came back inconclusive for this machine. What was never
run here is the comparison itself.

THE ONE VARIABLE IS WHERE THE EXPERTS ARE COMPUTED. Both runs use the same executable, the same
DLLs, the same model files, the same prompt, temperature 0, the same seed, context, batch and token
budget, the same API path and the same hash rule.

THE FLAGS DIFFER IN TWO SWITCHES, AND THAT IS STATED RATHER THAN HIDDEN. On this card there is no
resident-expert configuration for this model: 96.8 GB of weights against 32 GB of VRAM, which is
why expert streaming exists at all. "Experts on CUDA" is therefore only reachable through
--moe-stream, and the CPU side has nothing to stream. So the GPU arm carries the streaming flags
and the CPU arm carries -ncmoe 999. The semantic variable stays one; the flag count does not.

WHAT THIS DOES NOT ANSWER. Nothing about speed. The CPU arm reads experts through mmap on a machine
with less RAM than the model, so its runtime is expected to be poor and is recorded, never judged.
This is a correctness probe.

WHAT COUNTS AS A PASS IS COHERENCE, NOT AN IDENTICAL HASH. Two backends do not produce bit-identical
logits; under greedy decoding one flipped token forks the whole sequence. Requiring the same answer
text across CPU and CUDA is a gate no correct implementation can pass, so this probe reports the
text comparison as an observation and decides on the structural read of each answer instead.

THE EMPTY ANSWER IS REFUSED, NOT HASHED. SHA-256 of "" is e3b0c442..., so two empty answers agree
perfectly. This model can spend its whole budget on reasoning and leave message.content empty, so
the effective answer is content, else reasoning_content, and both empty is red.
#>
param(
    [string]$Exe     = 'C:\Users\robin\dev\crow-lab\wt-35\build-53\bin\Release\llama-server.exe',
    [string]$Lab     = 'C:\Users\robin\dev\crow-lab',
    [string]$Model   = 'models/UD-Q2_K_XL/DeepSeek-V4-Flash-UD-Q2_K_XL-00001-of-00003.gguf',
    [int]   $Port    = 8081,
    [int]   $Ctx     = 4096,
    [int]   $Ngl     = 99,
    [int]   $Tokens  = 512,
    [int]   $Verbosity = 5,
    [int]   $ReadyTimeoutSec   = 900,
    [int]   $RequestTimeoutSec = 3600,
    [string]$OutRoot = '',
    # Run ONE arm. #35 gates the expensive CPU control on the GPU reference reproducing the known
    # answer first: running both regardless would spend a long mmap-bound run on a comparison that
    # was already void. Empty runs both, in order.
    [ValidateSet('', 'gpu-cuda0-cache', 'cpu-ncmoe999')][string]$Only = '',
    [switch]$Selftest
)

$ErrorActionPreference = 'Continue'
function Say([string]$m) { Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

$PROMPT_TEXT = 'Write a Python function that reverses a linked list. Code only.'

# ---- value functions: return data, never narrate ----------------------------

function Get-Sha16([string]$s) {
    $h = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s))
    return (($h | ForEach-Object { '{0:x2}' -f $_ }) -join '').Substring(0,16)
}

function Get-FileSha([string]$p) {
    if (-not (Test-Path $p)) { return '' }
    return (Get-FileHash $p -Algorithm SHA256).Hash
}

# content when it carries anything, else reasoning_content, else a refusal. Never the empty string.
function Get-EffectiveAnswer([string]$content, [string]$reason) {
    if ($content.Length -gt 0) { return [pscustomobject]@{ ok=$true;  source='content';           text=$content } }
    if ($reason.Length  -gt 0) { return [pscustomobject]@{ ok=$true;  source='reasoning_content'; text=$reason  } }
    return [pscustomobject]@{ ok=$false; source=''; text='' }
}

# Structural read of the answer. Not a judgement of style - the question is whether the model
# produced an iterative linked-list reversal or something damaged.
function Test-ReversalShape([string]$t) {
    $findings = @()
    if ($t.Length -eq 0)                        { $findings += 'empty' }
    if ($t -notmatch 'def\s+\w+\s*\(')          { $findings += 'no function definition' }
    if ($t -notmatch 'while|for')               { $findings += 'no loop' }
    if ($t -notmatch '\.next')                  { $findings += 'no next pointer' }
    if ($t -notmatch 'return')                  { $findings += 'no return' }
    if ($t -match '[\uFFFD]')                   { $findings += 'replacement characters' }
    return [pscustomobject]@{ ok = ($findings.Count -eq 0); findings = $findings }
}

# Which expert placement the server actually used, read from its own log rather than from the
# flags we passed. A flag that was accepted and silently ignored is exactly the defect #35 found
# in tensor_buft_overrides, so the intent is never trusted here.
function Get-PlacementEvidence([string]$logPath) {
    if (-not (Test-Path $logPath)) { return [pscustomobject]@{ ok=$false; why='no log' } }
    # llama-server holds its log OPEN. A plain ReadAllText throws, and on 2026-08-06 this function
    # answered that throw with three zeros - which reads exactly like "no placement evidence" and
    # is instead "the read never happened". Same trap parse-moe-stats.ps1 carries in its header.
    # Shared read, and any failure is a refusal with a reason, never a zero.
    $txt = $null
    try {
        $fs = [IO.File]::Open($logPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $sr = New-Object IO.StreamReader($fs)
        $txt = $sr.ReadToEnd()
        $sr.Close(); $fs.Close()
    } catch {
        return [pscustomobject]@{ ok=$false; why=("log unreadable: " + $_.Exception.Message) }
    }
    $off = [regex]::Match($txt, 'offloaded\s+(\d+)/(\d+) layers to GPU')
    return [pscustomobject]@{
        ok            = $true
        why           = ''
        bytes         = $txt.Length
        stream        = ([regex]::Matches($txt, 'MoE expert streaming')).Count
        direct_io     = ([regex]::Matches($txt, 'uses O_DIRECT')).Count
        cuda0_assign  = ([regex]::Matches($txt, 'assigned to device CUDA0')).Count
        cuda0_any     = ([regex]::Matches($txt, 'CUDA0')).Count
        offloaded     = $(if ($off.Success) { $off.Value } else { '' })
        cpu_buffer    = ([regex]::Matches($txt, 'CPU_Mapped model buffer size|CPU model buffer size')).Count
        cpu_buf_lines = @([regex]::Matches($txt, '.*(CPU_Mapped|CPU) model buffer size *= *[0-9.]+ MiB') | ForEach-Object { $_.Value.Trim() } | Select-Object -First 3)
        overrides     = ([regex]::Matches($txt, 'buffer type overridden|overriding tensor type|tensor_buft_override')).Count
    }
}

# ---- self-test --------------------------------------------------------------

if ($Selftest) {
    $n = 0; $bad = 0
    function Check([string]$name, $want, $got) {
        $script:n++
        $ok = ([string]$want -eq [string]$got)
        if (-not $ok) { $script:bad++ }
        Say ("  {0}  {1,-58} want={2} got={3}" -f $(if ($ok) { 'ok  ' } else { 'FAIL' }), $name, $want, $got)
    }
    Say 'PHASE selftest'

    Check 'sha16 of a known string' '2cf24dba5fb0a30e' (Get-Sha16 'hello')
    Check 'sha16 of "" is the empty-string digest, which is why it is never used' 'e3b0c44298fc1c14' (Get-Sha16 '')

    $a1 = Get-EffectiveAnswer 'code here' 'thoughts'
    Check 'content wins when non-empty' 'content' $a1.source
    $a2 = Get-EffectiveAnswer '' 'thoughts'
    Check 'reasoning_content is the fallback' 'reasoning_content' $a2.source
    $a3 = Get-EffectiveAnswer '' ''
    Check 'both empty is REFUSED, not hashed' $false $a3.ok

    # single-quoted here-string: a backtick is PowerShell's ESCAPE character, so a
    # fenced ```python block inside a double-quoted string silently eats itself.
    $good = @'
class ListNode:
    def __init__(self, val=0, next=None):
        self.next = next

def rev(head):
    prev = None
    curr = head
    while curr:
        t = curr.next
        curr.next = prev
        prev = curr
        curr = t
    return prev
'@
    $r1 = Test-ReversalShape $good
    Check 'a real reversal passes the shape read' $true $r1.ok
    $r2 = Test-ReversalShape 'zzz qqq garbled nonsense without code'
    Check 'garbage FAILS the shape read' $false $r2.ok
    $r3 = Test-ReversalShape ''
    Check 'empty FAILS the shape read' $false $r3.ok
    $near = "def rev(head):" + [char]10 + "    return head"
    $r4 = Test-ReversalShape $near
    Check 'a function without a loop FAILS (the check can go red on near-misses)' $false $r4.ok

    $tmp = Join-Path $env:TEMP ("p35-" + [guid]::NewGuid().ToString('N') + '.log')
    'llama_model_load: MoE expert streaming is enabled', 'CUDA0 model buffer size = 1 MiB' | Set-Content $tmp -Encoding ascii
    $p1 = Get-PlacementEvidence $tmp
    Check 'placement: streaming line seen' 1 $p1.stream
    Check 'placement: CUDA0 seen' 1 $p1.cuda0_any
    $p2 = Get-PlacementEvidence (Join-Path $env:TEMP 'does-not-exist-p35.log')
    Check 'placement: missing log is refused, not reported as zeros' $false $p2.ok

    # THE case this function exists for. llama-server keeps its log open while running; a plain
    # ReadAllText throws there, and on 2026-08-06 the throw was answered with zeros that read like
    # "no evidence". Held open here the same way, the read must SUCCEED.
    $held = [IO.File]::Open($tmp, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    $p3 = Get-PlacementEvidence $tmp
    $held.Close()
    Check 'placement: a log held open by its writer is still readable' $true $p3.ok
    Check 'placement: and it still finds the streaming line' 1 $p3.stream
    Remove-Item $tmp -Force

    Say ('-' * 78)
    Say ("selftest: {0} of {1} cases green" -f ($n - $bad), $n)
    exit $(if ($bad -eq 0) { 0 } else { 1 })
}

# ---- setup ------------------------------------------------------------------

if (-not $OutRoot) { $OutRoot = Join-Path 'C:\Users\robin\dev\Crow\runs' ((Get-Date -Format 'yyyy-MM-dd') + '\e35-correctness') }
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null

if (-not (Test-Path $Exe)) { Say "exe not found: $Exe"; exit 2 }
$binDir = Split-Path -Parent $Exe
$prod = (Join-Path $Lab 'src\build-native') + '\'
if ($Exe.StartsWith($prod, [StringComparison]::OrdinalIgnoreCase)) { Say "refusing the production build: $Exe"; exit 2 }

$pre = @(Get-Process llama-server -ErrorAction SilentlyContinue)
if ($pre.Count -gt 0) { Say ("a llama-server is already running (pid " + (($pre | ForEach-Object { $_.Id }) -join ',') + ") - not touching it"); exit 2 }
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { Say "port $Port is already held"; exit 2 }

$ident = [pscustomobject]@{
    exe            = (Resolve-Path $Exe).ProviderPath
    sha_exe        = Get-FileSha $Exe
    sha_impl_dll   = Get-FileSha (Join-Path $binDir 'llama-server-impl.dll')
    sha_llama_dll  = Get-FileSha (Join-Path $binDir 'llama.dll')
    sha_ggml_cuda  = Get-FileSha (Join-Path $binDir 'ggml-cuda.dll')
}
$modelParts = @(Get-ChildItem (Join-Path $Lab (Split-Path $Model -Parent)) -Filter '*.gguf' | Sort-Object Name |
                ForEach-Object { [pscustomobject]@{ name=$_.Name; bytes=$_.Length; mtime=$_.LastWriteTime.ToString('o') } })

Say ('=' * 92)
Say '#35 CORRECTNESS PROBE - one binary, two expert placements, one request each'
Say ("exe            {0}" -f $ident.exe)
Say ("sha exe        {0}" -f $ident.sha_exe)
Say ("sha impl-dll   {0}" -f $ident.sha_impl_dll)
Say ("sha llama.dll  {0}" -f $ident.sha_llama_dll)
Say ("sha ggml-cuda  {0}" -f $ident.sha_ggml_cuda)
foreach ($m in $modelParts) { Say ("model          {0}  {1:N0} B" -f $m.name, $m.bytes) }
Say ("prompt sha     {0}" -f (Get-Sha16 $PROMPT_TEXT))
Say ('=' * 92)

$COMMON = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl',"$Ngl",'-np','1','-lv',"$Verbosity")

$ARMS = @(
    [pscustomobject]@{ name='gpu-cuda0-cache'; args = $COMMON + @('--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8','--moe-stream-direct','--spec-type','none') },
    [pscustomobject]@{ name='cpu-ncmoe999';    args = $COMMON + @('--n-cpu-moe','999','--spec-type','none') }
)

if ($Only) {
    $ARMS = @($ARMS | Where-Object { $_.name -eq $Only })
    if ($ARMS.Count -ne 1) { Say "no such arm: $Only"; exit 2 }
    Say ("single-arm mode: {0}" -f $Only)
}

$results = @()
foreach ($arm in $ARMS) {
    Say ("ARM {0}" -f $arm.name)
    $logBase = Join-Path $OutRoot $arm.name
    $p = Start-Process -FilePath $Exe -ArgumentList $arm.args -WorkingDirectory $Lab `
            -RedirectStandardOutput "$logBase.out" -RedirectStandardError "$logBase.err" -PassThru -WindowStyle Hidden
    try { $p.EnableRaisingEvents = $true } catch { }
    $t0 = Get-Date

    $ready = $false
    for ($i = 0; $i -lt ($ReadyTimeoutSec*2); $i++) {
        $p.Refresh(); if ($p.HasExited) { break }
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3; if ($h.status -eq 'ok') { $ready = $true; break } } catch { }
        if ($i % 60 -eq 0 -and $i -gt 0) { Say ("  loading ... {0} s" -f [int]((Get-Date) - $t0).TotalSeconds) }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        Say '  ABORT: server never became ready'
        try { if (-not $p.HasExited) { $p.Kill() } } catch { }
        $results += [pscustomobject]@{ arm=$arm.name; ok=$false; why='server never ready' }
        continue
    }
    $loadSecs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $cim = Get-CimInstance -Query "SELECT ProcessId, ExecutablePath, CommandLine FROM Win32_Process WHERE ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
    Say ("  pid {0}  ready in {1} s" -f $p.Id, $loadSecs)
    Say ("  cmd {0}" -f $cim.CommandLine)

    $body = @{ model='x'; messages=@(@{role='user'; content=$PROMPT_TEXT}); max_tokens=$Tokens; temperature=0; seed=0; stream=$false } | ConvertTo-Json -Depth 5
    $r = $null; $rt0 = Get-Date
    try { $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec $RequestTimeoutSec } catch { Say ("  request failed: {0}" -f $_.Exception.Message) }
    $reqSecs = [math]::Round(((Get-Date) - $rt0).TotalSeconds, 1)

    $rec = [pscustomobject]@{ arm=$arm.name; ok=$false; why='' }
    if ($null -ne $r) {
        $content = [string]$r.choices[0].message.content
        $reason  = [string]$r.choices[0].message.reasoning_content
        $eff = Get-EffectiveAnswer $content $reason
        $shape = Test-ReversalShape $eff.text
        $rec = [pscustomobject]@{
            arm = $arm.name; ok = $eff.ok; why = $(if ($eff.ok) { '' } else { 'content and reasoning_content both empty' })
            exe = $ident.exe; sha_exe = $ident.sha_exe; sha_impl_dll = $ident.sha_impl_dll
            sha_llama_dll = $ident.sha_llama_dll; sha_ggml_cuda = $ident.sha_ggml_cuda
            pid_ = $p.Id; cmdline = $cim.CommandLine; load_s = $loadSecs; request_s = $reqSecs
            prompt_sha = (Get-Sha16 $PROMPT_TEXT)
            answer_source = $eff.source; answer_sha = $(if ($eff.ok) { Get-Sha16 $eff.text } else { '' })
            answer_text = $eff.text; answer_chars = $eff.text.Length
            content_chars = $content.Length; reasoning_chars = $reason.Length
            completion = [int]$r.usage.completion_tokens; prompt_tokens = [int]$r.usage.prompt_tokens
            finish = [string]$r.choices[0].finish_reason
            shape_ok = $shape.ok; shape_findings = $shape.findings
        }
        Say ("  tokens {0}  finish {1}  source {2}  sha {3}" -f $rec.completion, $rec.finish, $rec.answer_source, $rec.answer_sha)
        Say ("  shape ok {0} {1}" -f $rec.shape_ok, ($rec.shape_findings -join '; '))
    } else {
        $rec = [pscustomobject]@{ arm=$arm.name; ok=$false; why='request failed'; request_s=$reqSecs }
    }

    try { if (-not $p.HasExited) { $p.Kill() } } catch { }
    try { [void]$p.WaitForExit(60000) } catch { }
    # only now is the log closed by its writer
    $placeAfter = Get-PlacementEvidence "$logBase.err"
    $rec | Add-Member -NotePropertyName placement -NotePropertyValue $placeAfter -Force
    Say ("  placement: ok={0} streaming={1} O_DIRECT={2} CUDA0-assign={3} offloaded='{4}' cpu-buffer={5} overrides={6}" -f `
         $placeAfter.ok, $placeAfter.stream, $placeAfter.direct_io, $placeAfter.cuda0_assign, $placeAfter.offloaded, $placeAfter.cpu_buffer, $placeAfter.overrides)
    for ($i = 0; $i -lt 120; $i++) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    $err = @([IO.File]::ReadAllLines("$logBase.err") | Where-Object { $_ -match 'CUDA error|out of memory|cudaMalloc failed|failed to load|error loading model' }).Count
    $rec | Add-Member -NotePropertyName error_lines -NotePropertyValue $err -Force
    Say ("  error lines in log: {0}" -f $err)
    $results += $rec
}

$jsonName = $(if ($Only) { "probe-35-$Only.json" } else { 'probe-35.json' })
$results | ConvertTo-Json -Depth 8 | Out-File (Join-Path $OutRoot $jsonName) -Encoding utf8
Say ("JSON: {0}" -f (Join-Path $OutRoot $jsonName))

Say ('=' * 92)
if ($Only) {
    $one = $results[0]
    Say ("ARM {0}: ok={1} sha={2} tokens={3} finish={4} shape_ok={5}" -f $one.arm, $one.ok, $one.answer_sha, $one.completion, $one.finish, $one.shape_ok)
    exit $(if ($one.ok) { 0 } else { 1 })
}
$g = @($results | Where-Object { $_.arm -eq 'gpu-cuda0-cache' })[0]
$c = @($results | Where-Object { $_.arm -eq 'cpu-ncmoe999'    })[0]
if ($g.ok -and $c.ok) {
    # THE VERDICT IS COHERENCE, NOT TEXT IDENTITY. CUDA and CPU do not compute bit-identical
    # logits, and under greedy decoding a difference in the last bit flips one token and forks the
    # rest of the sequence. Cross-backend hash equality is therefore a gate that a fully correct
    # implementation cannot pass, and reporting its failure as a fault would manufacture a defect.
    # Measured 2026-08-06: both arms produced correct iterative reversals whose loop bodies were
    # character-identical, while the hashes differed. What #33 describes is GARBAGE output, and
    # that is what the shape read looks for.
    $textSame = ($g.answer_text -eq $c.answer_text)
    $bothCoherent = $g.shape_ok -and $c.shape_ok -and ($g.error_lines -eq 0) -and ($c.error_lines -eq 0) -and
                    ($g.finish -eq 'stop') -and ($c.finish -eq 'stop')
    Say ("GPU  sha {0}  tokens {1}  finish {2}  source {3}  shape_ok {4}" -f $g.answer_sha, $g.completion, $g.finish, $g.answer_source, $g.shape_ok)
    Say ("CPU  sha {0}  tokens {1}  finish {2}  source {3}  shape_ok {4}" -f $c.answer_sha, $c.completion, $c.finish, $c.answer_source, $c.shape_ok)
    Say ("text identical across the backends: {0}  (an observation, NOT the gate)" -f $textSame)
    if ($bothCoherent) {
        Say 'VERDICT: BOTH COHERENT - the garbled-output symptom is not reproduced for this quant, card and prompt.'
        Say '         This is not a claim of numerical equality between the backends, and not a general CUDA correctness result.'
        exit 0
    }
    Say 'VERDICT: NOT COHERENT - at least one arm failed the shape read, ended abnormally, or logged errors.'
    Say '         Inspect the raw answer before calling this a CUDA fault; check the CPU arm first.'
    exit 3
}
Say 'VERDICT: incomplete - at least one arm did not produce a usable answer'
exit 1
