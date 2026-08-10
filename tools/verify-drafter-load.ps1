<#
verify-drafter-load - E10. Does the DSpark drafter LOAD on the new base, and does the
pristine old base refuse it for the reason we claim?

WHAT IS CLAIMED HERE, and nothing beyond it: the drafter loads, the server becomes
ready, and one controlled request is answered. Whether the drafter DRAFTS is E11 and is
not asserted here. Whether the patch costs throughput is E9 and is not re-measured here.

THE NEGATIVE PROBE IS THE ETAPE, not an addition to it. A green positive run on its own
proves that a server starts - it does not prove that the base jump removed the cause.
And the specimen for that negative probe is not obvious: crow-lab/src/build-native is a
b10223 server, but it is built from the 23-path working tree, which carries
patches/dflash-on-b10223.patch - the patch that turns
dflash.attention.sliding_window_pattern from required into optional. Measured
2026-08-05: its dflash.cpp.obj references set_swa_pattern once while the pristine
b10223 source contains the name zero times.

    A NEGATIVE PROBE WHOSE SPECIMEN IS ALREADY REPAIRED PROVES NOTHING.

So the specimen is a separate pristine tree built by build-pristine-negative.ps1, and
this tool refuses to run against src/build-native at all.

THE ONE DIFFERENCE THAT CANNOT BE HELD INVARIANT, stated rather than hidden. The
operating point carries four --moe-stream* options. Those options exist only because of
our patch, so the pristine specimen does not know them and would exit on an unknown
argument - which would be a parser failure dressed up as a load failure. They are
therefore dropped for the negative run, and every difference between the two command
lines is printed as data. A comparison whose deltas are not enumerated is not a
comparison.

WHY NO --spec-type IS PASSED, checked rather than assumed. The default is
types = { COMMON_SPECULATIVE_TYPE_NONE } (common.h:370), which looks as if -md would be
ignored and nothing would be loaded - both probes would then be harmlessly green while
proving nothing. It is not so: has_dft() returns !draft.mparams.empty() (common.h:382-384),
so it hangs on the -md PATH alone and not on the type. server-context.cpp:1053 reads
has_draft from exactly that, and 1208/1220 load the drafter on the strength of it.
E10 claims loading and nothing else, so the type is left at its default - the smallest
possible intervention, identical on both sides.

WHAT THE MARKOV LINE DOES AND DOES NOT SAY. "DFlash with DSpark markov head (rank = N)"
is emitted by load_arch_tensors, and that function is byte-identical on b10223 and
b10269 [measured]. The line proves markov_w1.weight was found; it does NOT distinguish
the bases. What distinguishes them is load_arch_hparams: LLM_KV_HYPER_CONNECTION_COUNT
occurs 0 times on b10223 and 1 time on b10269 [measured]. The two statements are
reported separately and must not be merged.

TRAPS THIS TOOL IS BUILT AROUND, all measured on this machine:
  - asking and running are two statements. After Start-Process the path of the RUNNING
    process is read and held against the one requested; the real command line comes out
    of Win32_Process, not out of the variable we passed in.
  - a pipe swallows the return value. Nothing is piped. The process exit code is taken
    from the Process object and reported SEPARATELY from any text match, because a
    text hit in a log says nothing about how the process ended.
  - "pattern not found" is not "check passed". Every extraction that fails to find its
    pattern is RED and says so, instead of silently contributing nothing.
  - Measure-Object -Line counts 0 for an empty line; line counts come from
    [IO.File]::ReadAllLines().Length.
  - the first error in a log is the one that matters. The negative run reports WHICH
    error line came first, so "another failure masked the expected one" is decidable
    rather than assumed.
  - model artefacts are hashed before AND after both probes. If a hash moves, the
    comparison is void regardless of what the logs say.

Usage:
  verify-drafter-load.ps1
  verify-drafter-load.ps1 -Only positive
  verify-drafter-load.ps1 -Only negative

Exit 0 = every check green.  1 = at least one red.  2 = setup error.
#>
param(
    [string]$PosWT   = 'C:\Users\robin\dev\crow-lab\wt-e10',
    [string]$NegWT   = 'C:\Users\robin\dev\crow-lab\wt-e10-neg',
    [string]$PosBin  = 'build-e10\bin\Release',
    [string]$NegBin  = 'build-e10neg\bin\Release',
    [string]$Lab     = 'C:\Users\robin\dev\crow-lab',
    [string]$CROW    = 'C:\Users\robin\dev\Crow',
    # relative to -Lab, exactly as the operating point states it
    [string]$Model   = $null,
    [string]$Drafter = $null,
    [int]   $Port    = 8081,
    [int]   $Ctx     = 4096,
    [int]   $Ngl     = 99,
    [int]   $ExpectDraftTensors = 81,
    [int]   $ExpectDraftKv      = 53,
    [string]$DrafterFileName    = 'DSV4-Flash-DSpark-draft-bf16.gguf',
    # 3 = info is the DEFAULT and it is not enough: at 3 the server log contains no
    # loader block at all. 5 = debug. Identical on both sides, so it is not a delta.
    [int]   $Verbosity          = 5,
    [int]   $MaxTokens          = 160,
    [int]   $HealthTimeoutSec   = 420,
    [int]   $NegTimeoutSec      = 600,
    [ValidateSet('both','positive','negative')][string]$Only = 'both',
    [string]$OutRoot = ''
)

. "$PSScriptRoot\model-paths.ps1"
if (-not $Model) { $Model = Get-ModelPath 'q2-k-xl' }
if (-not $Drafter) { $Drafter = Get-ModelPath 'drafter-bf16' }

$ErrorActionPreference = 'Continue'

if (-not $OutRoot) { $OutRoot = Join-Path $CROW ("runs\{0}\e10-drafter-load" -f (Get-Date -Format 'yyyy-MM-dd')) }
if (-not (Test-Path $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null }

$PRODUCTION = Join-Path $Lab 'src\build-native'
$EXPECTED   = 'key not found in model: dflash.attention.sliding_window_pattern'

$script:rows = @()
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Note([string]$phase,[string]$name,$want,$got,[bool]$ok) {
    $script:rows += [pscustomobject]@{ Phase=$phase; Check=$name; Want="$want"; Got="$got"; OK=[bool]$ok }
    Say ("  {0,-4} {1,-56} want {2,-22} got {3}" -f $(if($ok){'ok'}else{'RED'}), $name, "$want", "$got")
}
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }
function Lines-Of([string]$p) {
    # [IO.File]::ReadAllLines THROWS while llama-server holds the file open, and the
    # throw leaves an EMPTY array behind - which then reads as "pattern not found" for
    # every single check downstream. Measured 2026-08-05: the positive probe reported
    # 7 red checks about a drafter that had loaded perfectly well, because its log was
    # locked. Opened with FileShare.ReadWrite so a live log can be read.
    if (-not [IO.File]::Exists($p)) { return @() }
    try {
        $fs = [IO.File]::Open($p, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $sr = New-Object IO.StreamReader($fs)
            try { return @($sr.ReadToEnd() -split "`r?`n") } finally { $sr.Dispose() }
        } finally { $fs.Dispose() }
    } catch {
        Say ("  LOG READ FAILED for {0}: {1}" -f $p, $_.Exception.Message)
        return @()
    }
}

# ---- identity of a binary, as data rather than as an assumption --------------
function Assert-Exe {
    param([string]$Path, [string]$Under, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) { Die "$Label not found at $Path" }
    $full = (Resolve-Path -LiteralPath $Path).ProviderPath
    $u    = (Resolve-Path -LiteralPath $Under).ProviderPath.TrimEnd('\')
    if (-not $full.StartsWith($u + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Die "$Label at $full is not under $u - refusing, this would test a different tree than declared"
    }
    if ($full.StartsWith($PRODUCTION + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Die "$Label at $full is under the production build $PRODUCTION - refusing the silent fallback"
    }
    $fi  = Get-Item -LiteralPath $full
    $sha = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
    Say ("  {0,-10} {1}" -f $Label, $full)
    Say ("             {0} B   {1}   {2}" -f $fi.Length, $fi.LastWriteTime.ToString('o'), $sha)
    return [pscustomobject]@{ label=$Label; path=$full; bytes=$fi.Length
                              written=$fi.LastWriteTime.ToString('o'); sha256=$sha }
}

# ---- model artefacts, hashed before and after -------------------------------
function Hash-Artefacts {
    $out = @()
    $first = Join-Path $Lab ($Model -replace '/','\')
    $dir   = Split-Path $first -Parent
    foreach ($f in (Get-ChildItem $dir -File -Filter '*.gguf' | Sort-Object Name)) {
        $out += [pscustomobject]@{ name=$f.Name; bytes=$f.Length
                                   written=$f.LastWriteTime.ToString('o')
                                   sha256=(Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash }
    }
    $d = Join-Path $Lab ($Drafter -replace '/','\')
    $fi = Get-Item -LiteralPath $d
    $out += [pscustomobject]@{ name=$fi.Name; bytes=$fi.Length
                               written=$fi.LastWriteTime.ToString('o')
                               sha256=(Get-FileHash -LiteralPath $d -Algorithm SHA256).Hash }
    return $out
}
function Artefact-Key($a) { return (($a | ForEach-Object { "$($_.name):$($_.bytes):$($_.sha256)" }) -join '|') }

function Stop-Servers {
    Get-Process llama-server -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 60; $i++) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# ---- start, prove identity, watch until ready OR dead ------------------------
function Run-Server {
    param([string]$Exe, [string[]]$ServerArgs, [string]$LogBase, [int]$TimeoutSec, [string]$Tag)

    $out = "$LogBase.out"; $err = "$LogBase.err"
    ($ServerArgs -join ' ') | Out-File "$LogBase.cmdline.txt" -Encoding ascii

    $p = Start-Process -FilePath $Exe -ArgumentList $ServerArgs -WorkingDirectory $Lab `
            -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -WindowStyle Hidden
    # WITHOUT THIS THE EXIT CODE IS UNREADABLE, and it fails silently rather than loudly.
    # PowerShell 5.1 releases the process handle once the process ends, and .ExitCode then
    # yields empty - no exception, just nothing. Measured 2026-08-05 with both colours: a
    # process forced to exit 3 reported an empty ExitCode through Start-Process -PassThru,
    # while the same call with EnableRaisingEvents set read 7 from a process forced to
    # exit 7. Setting it keeps the handle alive. The negative probe's whole job is to say
    # HOW the process ended, so an unreadable exit code is a broken checker.
    try { $p.EnableRaisingEvents = $true } catch { Say "  could not set EnableRaisingEvents: $($_.Exception.Message)" }

    # asking and running are two different statements
    $actual = $null
    try { $actual = (Get-Process -Id $p.Id -ErrorAction Stop).Path } catch { $actual = $null }
    $realCmd = ''
    try { $realCmd = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $p.Id) -ErrorAction Stop).CommandLine } catch { $realCmd = '' }
    if ($realCmd) { $realCmd | Out-File "$LogBase.win32-commandline.txt" -Encoding ascii }

    $healthy = $false; $exited = $false; $exitCode = $null
    $t0 = Get-Date
    for ($i = 0; $i -lt ($TimeoutSec * 2); $i++) {
        $p.Refresh()
        if ($p.HasExited) { $exited = $true; break }
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
            if ($h.status -eq 'ok') { $healthy = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    # Reading .ExitCode straight after .HasExited returned $null on 2026-08-05, so the
    # negative probe could not state how the process ended - the one thing it exists to
    # state. WaitForExit() settles the object before the field is read.
    if ($exited) {
        try { $p.WaitForExit(5000) | Out-Null } catch { Say "  WaitForExit threw: $($_.Exception.Message)" }
        # The exception is PRINTED, not swallowed. A silent catch here is what let an
        # unreadable exit code look like a property of the process for two runs.
        try { $p.Refresh(); $exitCode = $p.ExitCode } catch { Say "  ExitCode threw: $($_.Exception.Message)"; $exitCode = $null }
    }
    if (-not $healthy -and -not $exited) {
        Say ("  $Tag : neither healthy nor exited within $TimeoutSec s - stopping it")
    }
    $secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    return [pscustomobject]@{
        pid = $p.Id; proc = $p; requestedExe = $Exe; actualExe = $actual; realCommandLine = $realCmd
        healthy = $healthy; exited = $exited; exitCode = $exitCode; seconds = $secs
        outLog = $out; errLog = $err
    }
}

# ---- log scanning. "not found" is never "passed". ---------------------------
function Log-Text([object]$r) {
    return ((Lines-Of $r.outLog) + (Lines-Of $r.errLog)) -join "`n"
}
function Log-Lines([object]$r) {
    # stderr first: llama.cpp writes its load protocol there
    return @(Lines-Of $r.errLog) + @(Lines-Of $r.outLog)
}
function First-Match([string[]]$lines, [string]$pattern) {
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match $pattern) { return @{ idx=$i; line=$lines[$i] } } }
    return $null
}
function Count-Match([string[]]$lines, [string]$pattern) {
    return @($lines | Where-Object { $_ -match $pattern }).Count
}

# ============================ setup =========================================
$posExePath = Join-Path $PosWT (Join-Path $PosBin 'llama-server.exe')
$negExePath = Join-Path $NegWT (Join-Path $NegBin 'llama-server.exe')

$T0 = Get-Date
Say ('=' * 78)
Say "E10 DRAFTER LOAD PROOF   only=$Only   port=$Port"
Say ('=' * 78)

Say 'PHASE identity'
$idPos = $null; $idNeg = $null
if ($Only -ne 'negative') {
    $idPos  = Assert-Exe -Path $posExePath -Under $PosWT -Label 'POS exe'
    $idPosD = Assert-Exe -Path (Join-Path $PosWT (Join-Path $PosBin 'llama-server-impl.dll')) -Under $PosWT -Label 'POS impl'
    $idPosL = Assert-Exe -Path (Join-Path $PosWT (Join-Path $PosBin 'llama.dll'))             -Under $PosWT -Label 'POS llama'
}
if ($Only -ne 'positive') {
    $idNeg  = Assert-Exe -Path $negExePath -Under $NegWT -Label 'NEG exe'
    $idNegD = Assert-Exe -Path (Join-Path $NegWT (Join-Path $NegBin 'llama-server-impl.dll')) -Under $NegWT -Label 'NEG impl'
    $idNegL = Assert-Exe -Path (Join-Path $NegWT (Join-Path $NegBin 'llama.dll'))             -Under $NegWT -Label 'NEG llama'
}

Say '  hashing model artefacts (before)'
$artBefore = Hash-Artefacts
foreach ($a in $artBefore) { Say ("    {0,-52} {1,14} B  {2}" -f $a.name, $a.bytes, $a.sha256) }
$artBefore | ConvertTo-Json -Depth 4 | Out-File (Join-Path $OutRoot 'artefacts-before.json') -Encoding ascii

if (-not (Stop-Servers)) { Die "port $Port still held before the run" }
$preProcs = @(Get-Process llama-server -ErrorAction SilentlyContinue).Count
Note 'identity' 'no llama-server running before the probes' 0 $preProcs ($preProcs -eq 0)

# ---- the two command lines, and every difference between them ---------------
# -lv 5 is NOT cosmetic and it is not optional. At the default threshold (verbosity 3)
# the server log carries no loader output at all: measured against
# runs/2026-08-05/e9b-b10269/server-none.log.err, 687 lines, which contains
# "load_model: loading model" and "model loaded" and NOTHING in between - no
# llama_model_loader:, no load_tensors:, no print_info:. The markov line and the tensor
# count live in exactly that missing block. Searching for them at threshold 3 would have
# reported "pattern not found" for a drafter that loaded perfectly well - a checker
# looking in the wrong place. Set identically on BOTH sides, so it is not a delta.
$posArgs = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl',"$Ngl",'-np','1',
             '-lv', "$Verbosity",
             '--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8','--moe-stream-direct',
             '-md', $Drafter)
# The pristine specimen does not know --moe-stream*; passing them would abort in the
# argument parser and that failure would look like a load failure. Dropped on purpose,
# and the delta is printed rather than buried.
$negArgs = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl',"$Ngl",'-np','1',
             '-lv', "$Verbosity",
             '-md', $Drafter)
$delta = @(Compare-Object $posArgs $negArgs | ForEach-Object { "{0} {1}" -f $_.SideIndicator, $_.InputObject })
Say '  command-line delta positive vs negative (<= only positive, => only negative):'
foreach ($d in $delta) { Say "    $d" }
$delta | Out-File (Join-Path $OutRoot 'commandline-delta.txt') -Encoding ascii
Say ("  working directory for both: {0}" -f $Lab)

# ============================ POSITIVE ======================================
$pos = $null
if ($Only -ne 'negative') {
    Say 'PHASE positive (b10269 + full patch)'
    $pos = Run-Server -Exe $idPos.path -ServerArgs $posArgs -LogBase (Join-Path $OutRoot 'positive') -TimeoutSec $HealthTimeoutSec -Tag 'POS'
    $pl  = Log-Lines $pos

    Note 'positive' 'running process path equals the requested exe' $idPos.path $pos.actualExe `
         ($null -ne $pos.actualExe -and $pos.actualExe.Equals($idPos.path, [StringComparison]::OrdinalIgnoreCase))
    Note 'positive' 'real command line readable from Win32_Process' 'non-empty' `
         $(if ($pos.realCommandLine) { 'yes' } else { 'no' }) ([bool]$pos.realCommandLine)
    Note 'positive' 'server reached the ready state' $true $pos.healthy $pos.healthy
    Say ("  time to ready: {0} s" -f $pos.seconds)

    $keyNotFound = Count-Match $pl ([regex]::Escape($EXPECTED))
    Note 'positive' 'the b10223 key error does NOT appear' 0 $keyNotFound ($keyNotFound -eq 0)

    $anyKeyNotFound = Count-Match $pl 'key not found in model'
    Note 'positive' 'no key-not-found of any kind' 0 $anyKeyNotFound ($anyKeyNotFound -eq 0)

    # PRECONDITION, and it is its own check. Everything below reads the loader block, and
    # at the default verbosity that block is absent from the log entirely. Without this
    # line a missing block would be reported as "markov line not found" - a statement
    # about the drafter, made from a log that never contained the answer.
    $loaderLines = Count-Match $pl 'llama_model_loader:'
    Note 'positive' 'load protocol present in the log at all (precondition)' 'ge 1' $loaderLines ($loaderLines -ge 1)
    if ($loaderLines -eq 0) {
        Say '  the loader block is missing - raise -Verbosity. Every check below reads that block.'
    }

    # Both models announce themselves on one line each, with their own counts. The
    # drafter is picked by FILE NAME, so the main model cannot satisfy this check.
    $mMain = First-Match $pl 'llama_model_loader: loaded meta data with .* tensors from'
    Note 'positive' 'main model metadata read' 'found' $(if($mMain){'found'}else{'PATTERN NOT FOUND'}) ($null -ne $mMain)

    $draftKv = -1; $draftTensors = -1; $draftLine = ''
    foreach ($l in $pl) {
        if ($l -match ('loaded meta data with (\d+) key-value pairs and (\d+) tensors from .*' + [regex]::Escape($DrafterFileName))) {
            $draftKv = [int]$Matches[1]; $draftTensors = [int]$Matches[2]; $draftLine = $l.Trim()
        }
    }
    if ($draftLine) { Say ("  drafter loader line: {0}" -f $draftLine) }
    Note 'positive' 'drafter announced by its own loader line' 'found' `
         $(if($draftLine){'found'}else{'PATTERN NOT FOUND'}) ([bool]$draftLine)
    Note 'positive' 'drafter tensor count' $ExpectDraftTensors $draftTensors ($draftTensors -eq $ExpectDraftTensors)
    Note 'positive' 'drafter key-value pair count' $ExpectDraftKv $draftKv ($draftKv -eq $ExpectDraftKv)

    # The markov line proves markov_w1.weight was FOUND. load_arch_tensors is
    # byte-identical on b10223 and b10269 [measured], so this line does NOT separate the
    # bases and must never be quoted as if it did. What separates them is
    # load_arch_hparams, and that is what the negative probe addresses.
    $mMarkov = First-Match $pl 'DFlash with DSpark markov head \(rank = '
    Note 'positive' 'markov head line present (tensor find, NOT a base proof)' 'found' `
         $(if($mMarkov){$mMarkov.line.Trim()}else{'PATTERN NOT FOUND'}) ($null -ne $mMarkov)

    # The drafter is announced by the speculative path itself (speculative.cpp:2334),
    # which is a different statement from the loader line: one says "asked to load it",
    # the other says "read its metadata".
    $mLoadDft = First-Match $pl "loading draft model"
    Note 'positive' 'speculative path announces the draft model' 'found' `
         $(if($mLoadDft){'found'}else{'PATTERN NOT FOUND'}) ($null -ne $mLoadDft)

    # speculative.cpp:237-243 throws when target and draft vocabs disagree. Checked at
    # the object beforehand (both 129280 tokens, gpt2/joyai-llm, bos 0, eos 1), so this
    # is expected to hold - which is exactly why it is asserted rather than assumed.
    $vocabBad = Count-Match $pl 'vocabs are not compatible|vocab type must match|vocab size|bos tokens must match|eos tokens must match'
    Note 'positive' 'no draft/target vocab incompatibility' 0 $vocabBad ($vocabBad -eq 0)

    # no CPU fallback, no CUDA/alloc failure
    $cudaErr = Count-Match $pl 'CUDA error|cudaMalloc failed|failed to allocate|out of memory|ggml_backend_cuda.*error'
    Note 'positive' 'no CUDA / allocation error lines' 0 $cudaErr ($cudaErr -eq 0)
    $loadFail = Count-Match $pl 'failed to load model|failed to load draft model|error loading model'
    Note 'positive' 'no model load failure lines' 0 $loadFail ($loadFail -eq 0)
    $archWarn = Count-Match $pl 'unknown architecture|unsupported architecture|missing tensor'
    Note 'positive' 'no architecture / missing-tensor warnings' 0 $archWarn ($archWarn -eq 0)

    # one controlled request
    if ($pos.healthy) {
        # 16 tokens was too small: this model opens with a thinking block, so the budget
        # ran out inside it and content stayed "" while reasoning_content held the text.
        # Measured 2026-08-05, and it is the same split E9 recorded (313 answer vs 834
        # reasoning characters). Both fields are counted, and the hard gate is the token
        # counter rather than a string length.
        $body = @{ model='x'; messages=@(@{role='user'; content='Reply with the single word: ready'})
                   max_tokens=$MaxTokens; temperature=0; stream=$false } | ConvertTo-Json -Depth 5
        $rc = 0; $answer = ''; $reason = ''; $nTok = -1; $finish = ''
        $tPrompt = -1.0; $tPredict = -1.0; $nPrompt = -1; $nPredict = -1
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post `
                        -ContentType 'application/json' -Body $body -TimeoutSec 600
            $resp | ConvertTo-Json -Depth 8 | Out-File (Join-Path $OutRoot 'positive-response.json') -Encoding utf8
            $answer = [string]$resp.choices[0].message.content
            $reason = [string]$resp.choices[0].message.reasoning_content
            $finish = [string]$resp.choices[0].finish_reason
            if ($resp.usage)   { $nTok = [int]$resp.usage.completion_tokens }
            if ($resp.timings) {
                $tPrompt  = [double]$resp.timings.prompt_ms
                $tPredict = [double]$resp.timings.predicted_ms
                $nPrompt  = [int]$resp.timings.prompt_n
                $nPredict = [int]$resp.timings.predicted_n
            }
        } catch { $rc = 1; $_.Exception.Message | Out-File (Join-Path $OutRoot 'positive-request-error.txt') -Encoding ascii }
        Note 'positive' 'one controlled request answered' 0 $rc ($rc -eq 0)
        Note 'positive' 'completion tokens produced' 'gt 0' $nTok ($nTok -gt 0)
        Note 'positive' 'text produced in content or reasoning_content' 'gt 0 chars' `
             ("content $($answer.Length) + reasoning $($reason.Length)") (($answer.Length + $reason.Length) -gt 0)
        Say ("  finish_reason: {0}" -f $finish)
        if ($answer) { Say ("  content:   {0}" -f ($answer -replace '\s+',' ')) }
        if ($reason) { Say ("  reasoning: {0}" -f ($reason -replace '\s+',' ')) }

        # PREFILL AND DECODE ARE TWO NUMBERS AND THEY STAY TWO NUMBERS. tok/s over a whole
        # request contains the prefill, so a faster prefill makes the figure look better
        # than the decode alone - E9 measured that exactly: -0.29 % client hid -0.66 %
        # decode. Reported here as CONTEXT ONLY and deliberately NOT compared against the
        # E9 operating point: this run differs in four ways at once (-lv 5 debug logging
        # in the decode path, a drafter resident in memory, a different prompt and token
        # count, and a single run without a median). A number from here placed next to
        # E9 would be the same error in a new coat.
        if ($tPrompt -ge 0) {
            $ppt = $(if ($nPrompt  -gt 0) { $tPrompt  / $nPrompt }  else { [double]::NaN })
            $dpt = $(if ($nPredict -gt 0) { $tPredict / $nPredict } else { [double]::NaN })
            Say ("  PREFILL  {0,10:N3} ms / {1,4} tok = {2,8:N2} ms/tok" -f $tPrompt,  $nPrompt,  $ppt)
            Say ("  DECODE   {0,10:N3} ms / {1,4} tok = {2,8:N2} ms/tok" -f $tPredict, $nPredict, $dpt)
            Say  "  context only - NOT a performance statement and NOT comparable to E9"
            [pscustomobject]@{ prompt_ms=$tPrompt; prompt_n=$nPrompt; predicted_ms=$tPredict
                               predicted_n=$nPredict; finish_reason=$finish
                               note='context only; -lv 5 debug logging, drafter resident, single run, not comparable to E9' } |
                ConvertTo-Json -Depth 3 | Out-File (Join-Path $OutRoot 'positive-timings.json') -Encoding ascii
        } else {
            Note 'positive' 'server reported separate prefill/decode timings' 'present' 'ABSENT' $false
        }
    }

    $null = Stop-Servers
}

# ============================ NEGATIVE ======================================
$neg = $null
if ($Only -ne 'positive') {
    Say 'PHASE negative (pristine b10223, no Crow patch)'
    $neg = Run-Server -Exe $idNeg.path -ServerArgs $negArgs -LogBase (Join-Path $OutRoot 'negative') -TimeoutSec $NegTimeoutSec -Tag 'NEG'
    $nl  = Log-Lines $neg

    Note 'negative' 'running process path equals the requested exe' $idNeg.path $neg.actualExe `
         ($null -ne $neg.actualExe -and $neg.actualExe.Equals($idNeg.path, [StringComparison]::OrdinalIgnoreCase))
    Note 'negative' 'real command line readable from Win32_Process' 'non-empty' `
         $(if ($neg.realCommandLine) { 'yes' } else { 'no' }) ([bool]$neg.realCommandLine)

    # Same precondition as on the positive side: an empty or truncated log would make
    # "expected message absent" mean two very different things.
    $nLines = $nl.Count
    Note 'negative' 'process produced a log at all (precondition)' 'ge 1' $nLines ($nLines -ge 1)

    # THE point: the expected message, from the target process
    $mExp = First-Match $nl ([regex]::Escape($EXPECTED))
    Note 'negative' 'expected key error present in the process log' 'found' `
         $(if($mExp){"line $($mExp.idx)"}else{'PATTERN NOT FOUND'}) ($null -ne $mExp)
    if ($mExp) { Say ("  expected: {0}" -f $mExp.line.Trim()) }

    # and it must not be preceded by a different failure. The pattern is deliberately
    # WIDE: a narrow one would miss the very failure this check exists to detect. The
    # first few hits are printed so a red result can be judged rather than guessed.
    $mAnyErr = First-Match $nl 'error|failed|abort|exception|out of memory|CUDA error'
    $errHits = @($nl | Where-Object { $_ -match 'error|failed|abort|exception|out of memory|CUDA error' })
    $errHits | Out-File (Join-Path $OutRoot 'negative-error-lines.txt') -Encoding utf8
    Say ("  error-ish lines in the negative log: {0}" -f $errHits.Count)
    foreach ($h in ($errHits | Select-Object -First 5)) { Say ("    | {0}" -f $h.Trim()) }
    if ($mAnyErr) { Say ("  first error-ish line (idx {0}): {1}" -f $mAnyErr.idx, $mAnyErr.line.Trim()) }
    $firstIsExpected = ($null -ne $mExp) -and ($null -ne $mAnyErr) -and ($mAnyErr.idx -ge $mExp.idx)
    Note 'negative' 'no earlier, different failure masks the expected one' 'expected is first' `
         $(if($firstIsExpected){'yes'}else{"first at $($mAnyErr.idx), expected at $(if($mExp){$mExp.idx}else{'n/a'})"}) $firstIsExpected

    # WHICH model the expected error is about. The pristine specimen has no expert
    # streaming, so the 96.8 GB main model cannot fit 32.6 GiB of VRAM and fails too -
    # measured 2026-08-05, "unable to allocate CUDA0 buffer" at 0.29.53. That is a
    # SECOND, independent failure and it must not be allowed to stand in for the first.
    $mdEvidence = Count-Match $nl '\[spec\] failed to measure draft model memory|failed to load draft model'
    Note 'negative' 'expected error is about the -md model (spec path says so)' 'ge 1' $mdEvidence ($mdEvidence -ge 1)

    # THE SUCCESS CRITERIA MUST MATCH THE CLAIM, and the claim is narrow: the key stops
    # the DRAFTER, not the process. So the drafter's absence is asserted directly instead
    # of being inferred from the error text - and each zero is paired with the non-zero
    # the positive probe produced in this same run, otherwise a zero proves nothing.
    $negMarkov  = Count-Match $nl 'markov head'
    $negDftTens = Count-Match $nl 'create_tensor.*(dspark|markov)'
    $negLoadDft = Count-Match $nl 'loading draft model'
    $posMarkov  = $(if ($pos) { Count-Match (Log-Lines $pos) 'markov head' } else { -1 })
    Note 'negative' 'zero markov initialisation on the drafter'  0 $negMarkov  ($negMarkov  -eq 0)
    Note 'negative' 'zero drafter tensors created'               0 $negDftTens ($negDftTens -eq 0)
    Note 'negative' 'speculative path never announced the draft' 0 $negLoadDft ($negLoadDft -eq 0)
    if ($Only -eq 'both') {
        # the control for those three zeros: the same counter, same run, other specimen
        Note 'negative' 'markov counter can be non-zero (positive probe, same run)' 'ge 1' $posMarkov ($posMarkov -ge 1)
    }

    # The fatal allocation must be attributable to the MAIN model, otherwise "independent
    # follow-on failure" would be an interpretation rather than a finding.
    $oomMain = Count-Match $nl "failed to load model 'models/UD-Q2_K_XL"
    $oomDft  = Count-Match $nl "failed to load model 'models/DSV4-Flash-DSpark"
    Note 'negative' 'fatal load failure names the MAIN model' 'ge 1' $oomMain ($oomMain -ge 1)
    Note 'negative' 'fatal load failure does NOT name the drafter file' 0 $oomDft ($oomDft -eq 0)

    $mVram = First-Match $nl 'unable to allocate|cudaMalloc failed|out of memory'
    $mLast = $null
    for ($i = $nl.Count - 1; $i -ge 0; $i--) { if ($nl[$i] -match ' E ' -or $nl[$i] -match 'exiting due to') { $mLast = @{ idx=$i; line=$nl[$i] }; break } }
    Say  '  --- cause of termination, reported as data and NOT merged with the above ---'
    if ($mVram) { Say ("  VRAM failure present at idx {0}: {1}" -f $mVram.idx, $mVram.line.Trim()) }
    if ($mLast) { Say ("  last error line   at idx {0}: {1}" -f $mLast.idx, $mLast.line.Trim()) }
    $termIsDrafter = ($null -ne $mLast) -and ($mLast.line -match 'sliding_window_pattern|draft')
    Say ("  process died on the DRAFTER path: {0}   (if no, the exit code carries a second cause)" -f $termIsDrafter)
    [pscustomobject]@{
        expectedIdx      = $(if ($mExp)  { $mExp.idx }  else { -1 })
        vramFailureIdx   = $(if ($mVram) { $mVram.idx } else { -1 })
        lastErrorIdx     = $(if ($mLast) { $mLast.idx } else { -1 })
        lastErrorLine    = $(if ($mLast) { $mLast.line.Trim() } else { '' })
        terminationOnDrafterPath = $termIsDrafter
    } | ConvertTo-Json -Depth 3 | Out-File (Join-Path $OutRoot 'negative-termination.json') -Encoding utf8

    # ready state and exit code are two SEPARATE statements
    Note 'negative' 'server did NOT reach the ready state' $false $neg.healthy (-not $neg.healthy)
    Note 'negative' 'process terminated on its own' $true $neg.exited $neg.exited
    Say ("  process exit code: {0}   (recorded separately from any text match)" -f `
         $(if ($null -ne $neg.exitCode) { $neg.exitCode } else { 'still running / unknown' }))
    Note 'negative' 'process exit code is non-zero' 'ne 0' `
         $(if ($null -ne $neg.exitCode) { $neg.exitCode } else { 'unknown' }) `
         (($null -ne $neg.exitCode) -and ($neg.exitCode -ne 0))

    $null = Stop-Servers
}

# ============================ invariants ====================================
Say 'PHASE invariants'
$artAfter = Hash-Artefacts
$artAfter | ConvertTo-Json -Depth 4 | Out-File (Join-Path $OutRoot 'artefacts-after.json') -Encoding ascii
$same = (Artefact-Key $artBefore) -eq (Artefact-Key $artAfter)
Note 'invariants' 'model artefacts unchanged across both probes' 'identical' `
     $(if($same){'identical'}else{'CHANGED'}) $same

$leftOver = @(Get-Process llama-server -ErrorAction SilentlyContinue).Count
Note 'invariants' 'no llama-server left running' 0 $leftOver ($leftOver -eq 0)

$prodTouched = @(& git -C (Join-Path $Lab 'src') status --porcelain).Count
Note 'invariants' 'crow-lab/src still at 23 paths' 23 $prodTouched ($prodTouched -eq 23)

# ============================ result ========================================
Write-Output ''
$script:rows | Format-Table Phase, Check, Want, Got, OK -AutoSize | Out-String -Width 220 | Write-Output
$red   = @($script:rows | Where-Object { -not $_.OK })
$total = $script:rows.Count
$summary = [pscustomobject]@{
    only = $Only; checks = $total; red = $red.Count
    positive = $(if ($pos) { @{ pid=$pos.pid; healthy=$pos.healthy; exited=$pos.exited; exitCode=$pos.exitCode; seconds=$pos.seconds; exe=$idPos.path; sha256=$idPos.sha256 } } else { $null })
    negative = $(if ($neg) { @{ pid=$neg.pid; healthy=$neg.healthy; exited=$neg.exited; exitCode=$neg.exitCode; seconds=$neg.seconds; exe=$idNeg.path; sha256=$idNeg.sha256 } } else { $null })
}
$summary | ConvertTo-Json -Depth 6 | Out-File (Join-Path $OutRoot 'summary.json') -Encoding ascii

Write-Output ('=' * 78)
Say ("wall {0} s   raw: {1}" -f [math]::Round(((Get-Date)-$T0).TotalSeconds,1), $OutRoot)
if ($red.Count -eq 0) {
    Write-Output ("RESULT: PASS - {0} of {0} checks green, scope '{1}'." -f $total, $Only)
    Write-Output "  Claimed: the drafter LOADS on b10269 and reaches a ready server; pristine b10223 reports"
    Write-Output "  'key not found in model: dflash.attention.sliding_window_pattern' as its FIRST failure, on the -md model."
    Write-Output "  NOT claimed: that it DRAFTS (E11), any performance statement (E9), or that the negative"
    Write-Output "  process exit code carries only this one cause - see negative-termination.json."
    exit 0
} else {
    Write-Output ("RESULT: FAIL - {0} of {1} checks red." -f $red.Count, $total)
    foreach ($r in $red) { Write-Output ("  RED  {0} / {1}: want {2}, got {3}" -f $r.Phase, $r.Check, $r.Want, $r.Got) }
    exit 1
}
