<#
measure-24-gate - run the ten-task quality gate against ONE named placement, and keep the proof
that it was that placement.

WHY THIS EXISTS, and it is a measured gap rather than a precaution. The 10/9/9 gate series of
2026-08-05 is a valid quality result and it cannot be attributed to a configuration: the runner
talks to an endpoint on 127.0.0.1:8081 and never starts the server, so no artefact of that series
carries a command line. Searched on 2026-08-06 - the series log holds 66 run/task lines and ZERO
lines matching moe-stream, n-cpu-moe, ngl, temp or seed, and no server log exists in its 11:31-11:59
window. The quality number is real; "on streaming" is a memory.

    A QUALITY RESULT WITHOUT ITS CONFIGURATION BELONGS TO NO CONFIGURATION.

So this tool owns the server for the duration of the gate. It starts it with the flags it was
given, keeps stderr as a file, waits for health, runs the existing probe-suite.py, stops the
server, and then READS BACK out of that log which placement actually ran. The flags asked for are
not the evidence - the server's own report is.

WHAT IT DOES NOT DO: it does not implement a quality measure (probe-suite.py owns that, with its
own selftest of both colours), it does not run series or medians (measure-gate-stability.ps1 owns
that), and it does not decide whether a k/N is good. It produces one k/N with its configuration
nailed to it.

TRAPS IT IS BUILT AROUND, all previously measured on this machine:
  - llama-server keeps its log file open. ReadAllText throws on it; the read here goes through
    FileShare::ReadWrite so the log can be inspected while the process still holds it.
  - $ErrorActionPreference = 'Stop' turns a native command's stderr into a terminating error and
    then points at the wrong place. It stays 'Continue' and exit codes are checked instead.
  - PowerShell's *> redirection writes UTF-16, which grep reads as nothing. Start-Process
    -RedirectStandardError writes bytes as the process emits them, so the log stays greppable.
  - A zero without a positive control does not distinguish "absent" from "not searched". Every
    placement check below carries a term that MUST hit in the same read.

Usage:
  measure-24-gate.ps1 -Label cpu-moe   -Flags '--n-cpu-moe','999'
  measure-24-gate.ps1 -Label streaming -Flags '--moe-stream','--moe-stream-cache','64s',
                                              '--moe-stream-io-threads','8','--moe-stream-direct'
  measure-24-gate.ps1 -Selftest

Exit 0 = gate ran and its placement is proven.  1 = a check went red.  2 = setup error.
#>
param(
    [string]  $Exe       = 'C:\Users\robin\dev\crow-lab\wt-24\build-24\bin\Release\llama-server.exe',
    [string]  $Lab       = 'C:\Users\robin\dev\crow-lab',
    [string]  $CROW      = 'C:\Users\robin\dev\Crow',
    [string]  $Model     = 'models/UD-Q2_K_XL/DeepSeek-V4-Flash-UD-Q2_K_XL-00001-of-00003.gguf',
    [string]  $Label     = '',
    [string[]]$Flags     = @(),
    [int]     $Port      = 8081,
    [int]     $Ctx       = 4096,
    [int]     $Ngl       = 99,
    [int]     $MaxTokens = 4096,
    [int]     $HealthTimeoutSec = 420,
    [int]     $TimeoutSec = 900,
    # Graded tasks. Empty = all ten, which is only valid for a run that stands alone: two runs
    # sharing a task also share its cache state and stop being independent measurements.
    [string[]] $Only     = @(),
    # Tasks for a throwaway first pass, on a fresh server. Must not overlap $Only.
    [string[]] $Warm     = @(),
    [string]  $Python    = 'python',
    [string]  $OutRoot   = '',
    [switch]  $Selftest
)

$ErrorActionPreference = 'Continue'

function Say([string]$m) { Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

# Value function. The server holds this file open, so a plain ReadAllText throws and a caller that
# answers the exception with an empty string reports "no evidence" where it means "not read".
function Read-OpenLog([string]$Path) {
    if (-not (Test-Path $Path)) { return '' }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try { $sr = New-Object IO.StreamReader($fs); try { return $sr.ReadToEnd() } finally { $sr.Dispose() } }
        finally { $fs.Dispose() }
    } catch { return '' }
}

# Value function. A k/N below N is a RESULT and not a malfunction, and this tool must not turn one
# into the other - "the quality gate produced a technical error rather than a factual one" is an
# abort criterion, so the two have to stay apart. probe-suite.py documents its codes: 0 every task
# judged and correct, 1 at least one judged task not correct, 2 harness could not run,
# 3 CHECKER BROKEN. Only 2 and 3 say the measurement did not happen.
#
# Measured 2026-08-06: the streaming gate returned 9 of 10 with 0 undecided and exit 1, and this
# tool reported FAIL over a perfectly good measurement whose one failing task is the documented
# unstable case from #46.
function Test-GateOutcome([int]$ExitCode) {
    switch ($ExitCode) {
        0 { return [pscustomobject]@{ measured = $true;  all_correct = $true;  why = 'every task judged and correct' } }
        1 { return [pscustomobject]@{ measured = $true;  all_correct = $false; why = 'at least one judged task not correct - a result, not a malfunction' } }
        2 { return [pscustomobject]@{ measured = $false; all_correct = $false; why = 'harness could not run' } }
        3 { return [pscustomobject]@{ measured = $false; all_correct = $false; why = 'CHECKER BROKEN - a wrong implementation passed' } }
        default { return [pscustomobject]@{ measured = $false; all_correct = $false; why = "unknown probe-suite exit $ExitCode" } }
    }
}

# Value function. Returns what the SERVER said about its own placement, plus the positive control
# that proves the log was readable at all. A caller may not read the counts without it.
function Get-Placement([string]$LogText) {
    return [pscustomobject]@{
        readable      = [bool]($LogText.Length -gt 0)
        control_hits  = ([regex]::Matches($LogText, 'llama_context')).Count
        stream_slots  = ([regex]::Matches($LogText, 'MoE expert streaming with \d+ cache slots')).Count
        direct_io     = ([regex]::Matches($LogText, 'moe_stream.*direct|direct I/O|O_DIRECT')).Count
        cpu_moe       = ([regex]::Matches($LogText, 'CPU_Mapped model buffer size')).Count
        cuda0_buffer  = ([regex]::Matches($LogText, 'CUDA0 model buffer size')).Count
        n_ctx         = ([regex]::Matches($LogText, 'n_ctx\s+=\s+(\d+)')).Count
        error_lines   = ([regex]::Matches($LogText, '(?m)^\S+\s+E\s')).Count
    }
}

if ($Selftest) {
    $cases = @()
    function Case([string]$n, [bool]$p, [string]$d) { $script:cases += [pscustomobject]@{ n=$n; p=$p; d=$d } }
    $script:cases = @()

    # A streaming log and a CPU-MoE log, each asserted in BOTH directions. One direction alone
    # would pass for a parser that reports everything, or for one that reports nothing.
    $streamLog = @'
0.00.566.723 I llama_context: MoE expert streaming with 64 cache slots, n_ubatch = 512
0.00.566.727 I llama_context: n_ctx         = 4096
0.00.600.000 I load_tensors:   CUDA0 model buffer size =  6917.82 MiB
'@
    $cpuLog = @'
0.00.100.000 I llama_context: n_ctx         = 4096
0.18.608.325 I load_tensors:   CPU_Mapped model buffer size = 46738.52 MiB
0.18.608.326 I load_tensors:   CPU_Mapped model buffer size = 45174.68 MiB
'@
    $s = Get-Placement $streamLog
    $c = Get-Placement $cpuLog
    Case 'streaming log: cache slots found'      ($s.stream_slots -eq 1) ("got " + $s.stream_slots)
    Case 'streaming log: NO cpu-moe buffer'      ($s.cpu_moe -eq 0)      ("got " + $s.cpu_moe)
    Case 'cpu-moe log: mapped buffers found'     ($c.cpu_moe -eq 2)      ("got " + $c.cpu_moe)
    Case 'cpu-moe log: NO cache slots'           ($c.stream_slots -eq 0) ("got " + $c.stream_slots)
    Case 'positive control hits on both logs'    ($s.control_hits -ge 1 -and $c.control_hits -ge 1) ("s=" + $s.control_hits + " c=" + $c.control_hits)
    # The control that must fail: an empty log must NOT look like a clean CPU-MoE run. Without
    # this, "0 cache slots" from an unreadable file would read as a placement statement.
    $e = Get-Placement ''
    Case 'empty log is not readable'             ($e.readable -eq $false) 'readable=false'
    Case 'empty log has no positive control'     ($e.control_hits -eq 0)  'control=0, so its zeros mean nothing'
    # Error counting must see a real error line and must not invent one.
    $errLog = "0.00.1 E  something failed`n0.00.2 I llama_context: fine"
    Case 'error line counted when present'       ((Get-Placement $errLog).error_lines -eq 1) 'one E line'
    Case 'no error line counted when absent'     ($s.error_lines -eq 0) 'clean log stays clean'
    # Reading an open file: write one, hold it open, read it anyway.
    $tmp = Join-Path $env:TEMP ("m24gate-{0}.log" -f $PID)
    'held open by its writer' | Out-File -Encoding ascii $tmp
    $held = [IO.File]::Open($tmp, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    try { $txt = Read-OpenLog $tmp; Case 'log readable while still held open' ($txt -match 'held open') ("chars=" + $txt.Length) }
    finally { $held.Dispose(); Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    Case 'missing file reads as empty, not as a throw' ((Read-OpenLog (Join-Path $env:TEMP 'no-such-file-here.log')) -eq '') 'empty string'

    # A k/N below N is a result; a broken harness is not. Both directions are asserted, because a
    # tool that called everything "measured" would be as useless as the one that called 9 of 10 a
    # failure - which is what this tool did on 2026-08-06 before these four cases existed.
    $o0 = Test-GateOutcome 0; $o1 = Test-GateOutcome 1; $o2 = Test-GateOutcome 2; $o3 = Test-GateOutcome 3
    Case 'exit 0: measured and all correct'      ($o0.measured -and $o0.all_correct)          $o0.why
    Case 'exit 1: MEASURED, not all correct'     ($o1.measured -and -not $o1.all_correct)     $o1.why
    Case 'exit 2: NOT measured'                  (-not $o2.measured)                          $o2.why
    Case 'exit 3: NOT measured (checker broken)' (-not $o3.measured)                          $o3.why
    Case 'an unknown exit code is not measured'  (-not (Test-GateOutcome 99).measured)        (Test-GateOutcome 99).why

    Write-Output ''
    Say ('=' * 78)
    Say 'INSTRUMENT SELFTEST - every case below can go red'
    Say ('=' * 78)
    $bad = 0
    foreach ($c2 in $script:cases) {
        $tag = $(if ($c2.p) { 'ok  ' } else { 'RED '; })
        if (-not $c2.p) { $bad++ }
        Say ("  {0} {1,-46} {2}" -f $tag, $c2.n, $c2.d)
    }
    Write-Output ''
    if ($bad -eq 0) { Write-Output ("RESULT: PASS - {0} of {0} instrument cases green." -f $script:cases.Count); exit 0 }
    Write-Output ("RESULT: FAIL - {0} of {1} instrument cases red." -f $bad, $script:cases.Count)
    exit 1
}

if (-not $Label)        { Write-Output 'SETUP ERROR: -Label is required; the artefact is named after the placement it proves'; exit 2 }
if ($Flags.Count -eq 0) { Write-Output 'SETUP ERROR: -Flags is required; an empty flag set is a placement nobody stated'; exit 2 }
if (-not (Test-Path $Exe)) { Write-Output "SETUP ERROR: no server at $Exe"; exit 2 }
if (-not $OutRoot) { $OutRoot = Join-Path $CROW ("runs\{0}\e24-gate" -f (Get-Date -Format 'yyyy-MM-dd')) }
$outDir = Join-Path $OutRoot $Label
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
$logPath = Join-Path $OutRoot ("{0}.err" -f $Label)

$srvArgs = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl',"$Ngl",'-np','1',
             '-lv','5','--spec-type','none') + $Flags

Say ('=' * 78)
Say ("GATE ON PLACEMENT '{0}'" -f $Label)
Say ("exe   {0}" -f $Exe)
Say ("sha   {0}" -f (Get-FileHash $Exe -Algorithm SHA256).Hash)
Say ("flags {0}" -f ($Flags -join ' '))
Say ("log   {0}" -f $logPath)
Say ('=' * 78)

$p = Start-Process -FilePath $Exe -ArgumentList $srvArgs -WorkingDirectory $Lab `
                   -RedirectStandardError $logPath -RedirectStandardOutput (Join-Path $OutRoot ("{0}.out" -f $Label)) `
                   -PassThru -WindowStyle Hidden
Say ("pid {0}" -f $p.Id)
Say ("cmd `"{0}`" {1}" -f $Exe, ($srvArgs -join ' '))

$ok = $false
$deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
while ((Get-Date) -lt $deadline) {
    if ($p.HasExited) { break }
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
        if ($h) { $ok = $true; break }
    } catch { Start-Sleep -Seconds 3 }
}
if (-not $ok) {
    Say 'server never became healthy'
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Write-Output 'RESULT: FAIL - no endpoint'; exit 1
}
Say 'endpoint healthy'

$suite = Join-Path $CROW 'tools\probe-suite.py'

# -Warm runs a throwaway pass FIRST, on tasks the graded pass does not use. Without it the first
# graded task pays the whole cold cost of the run - the model file, the VRAM slots and, since
# 2026-08-09, a 32 GiB host tier that starts empty. Its output is written to a separate directory
# and never read.
if ($Warm.Count -gt 0) {
    Say ("warm-up (not graded): {0}" -f ($Warm -join ' '))
    & $Python $suite run --url "http://127.0.0.1:$Port" --out "$outDir-warm" `
        --max-tokens $MaxTokens --timeout $TimeoutSec --only @Warm | Out-Null
    Say ("warm-up exit {0}" -f $LASTEXITCODE)
}

Say ("running {0}" -f $suite)
# -Only exists because repeating a task measures the cache, not the configuration. Two runs that
# share a task share its warmed state, and with a host tier holding 32 GiB of experts that shared
# state IS the thing under test. Vault: der-betriebspunkt-ist-200k-kontext-auf-einem-slot.
if ($Only.Count -gt 0) {
    Say ("graded tasks: {0}" -f ($Only -join ' '))
    & $Python $suite run --url "http://127.0.0.1:$Port" --out $outDir --max-tokens $MaxTokens --timeout $TimeoutSec --only @Only
} else {
    & $Python $suite run --url "http://127.0.0.1:$Port" --out $outDir --max-tokens $MaxTokens --timeout $TimeoutSec
}
$gateExit = $LASTEXITCODE
Say ("probe-suite exit {0}" -f $gateExit)

if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }
Say 'server stopped'

$pl = Get-Placement (Read-OpenLog $logPath)
Write-Output ''
Say ('=' * 78)
Say 'PLACEMENT, READ BACK OUT OF THE SERVER LOG (not out of the flags asked for)'
Say ('=' * 78)
Say ("  log readable                 {0}" -f $pl.readable)
Say ("  positive control (llama_context lines)  {0}" -f $pl.control_hits)
Say ("  MoE streaming cache slots    {0}" -f $pl.stream_slots)
Say ("  CPU_Mapped model buffers     {0}" -f $pl.cpu_moe)
Say ("  CUDA0 model buffers          {0}" -f $pl.cuda0_buffer)
Say ("  server error lines           {0}" -f $pl.error_lines)

$summary = [pscustomobject]@{
    label = $Label; flags = $Flags; cmd = ('"{0}" {1}' -f $Exe, ($srvArgs -join ' '))
    exe = $Exe; sha_exe = (Get-FileHash $Exe -Algorithm SHA256).Hash
    model = $Model; ctx = $Ctx; ngl = $Ngl; max_tokens = $MaxTokens
    gate_exit = $gateExit; out_dir = $outDir; log = $logPath
    placement = $pl
}
$summary | ConvertTo-Json -Depth 6 | Out-File (Join-Path $OutRoot ("{0}-summary.json" -f $Label)) -Encoding ascii

$outcome = Test-GateOutcome $gateExit
Say ("  gate outcome                 {0}" -f $outcome.why)

if (-not $pl.readable -or $pl.control_hits -lt 1) {
    Write-Output 'RESULT: FAIL - the server log could not be read, so its zeros state nothing'; exit 1
}
if (-not $outcome.measured) {
    Write-Output ("RESULT: FAIL - the gate did not measure: {0} (probe-suite exit {1})" -f $outcome.why, $gateExit); exit 1
}
Write-Output ("RESULT: PASS - gate ran on '{0}', placement read back from the log. Quality verdict: {1}" -f $Label, $outcome.why)
exit 0
