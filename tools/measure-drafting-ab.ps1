<#
measure-drafting-ab - what does -md change at THIS operating point, and can the change
be attributed to anything measurable?

E11 proved the drafter works: 228 drafted, 194 accepted, acceptance 0.8509. The first
ABBA series then measured +2.84 % decode with the drafter over four evaluable runs per
side, with overlapping ranges - descriptive, not a verdict. This version does not try to
settle that with more repetitions. It adds the quantities that let a difference be
ATTRIBUTED instead of only observed.

WHAT IT CAN DECIDE, and nothing more: whether the SAME b10269 build with -md reads more
bytes and waits longer for the same answer. What it CANNOT decide is which model caused
which cache miss. The expert-level counters exist - llama_moe_stream::print_stats() in
src/llama-moe-stream.cpp holds hits, misses, cold misses, load stall, slot wait, waves
and locality - but llama-server never calls them. Measured 2026-08-05: the only callers
of common_perf_print are tools/completion and tools/tts, and the ABBA logs carry 0 of 8
blocks with a "moe stream:" line on either side. Making them visible is a change under
tools/server/ and is NOT part of this tool.

    THEREFORE: NOTHING HERE INFERS REQUEST TRAFFIC FROM THE TENSOR INVENTORY.

That the drafter brings 9 SSD-streamed expert tensors is a LOADING fact. It is not
evidence that those bytes are read per request. What is measured per request is the
process read-byte delta, and it is labelled as exactly that.

THE ORDER IS ALTERNATING AND FIXED BEFORE THE START. Running all of A and then all of B
charges every warm-up and every thermal drift to one side. The count is fixed too and it
is NOT extended after seeing a convenient or inconvenient interim result. That is the
difference between measuring and fishing.

ONE WARM-UP PER SERVER START, DISCARDED. -md is a start parameter, so every switch is a
new process with a cold prefix cache. E9 measured what that costs: run 1 prefills 17
tokens, runs 2+ hit the cache and prefill 4.

DECODE IS THE PRIMARY QUANTITY. tok/s over a whole request contains the prefill, and
speculation acts on the decode alone. E9 measured -0.29 % client against -0.66 % decode
in the same run. Prefill and total time are reported, never merged into the judgement.

REQUEST-LOCAL, NOT CUMULATIVE. The server log grows across warm-up and measured run, and
its acceptance counters are per request while the log is not. Every log-derived figure
here is parsed from the byte OFFSET taken immediately before the measured request, so a
warm-up can never be counted twice. Same for the raw performance counters: before and
after are read on the calling thread, around that one request.

TELEMETRY UNDER LOAD, not before the start. The previous version sampled once per block
BEFORE the server was started, which records the idle state of the machine and cannot
correlate a slow request with anything. Sampling now runs in a runspace for the duration
of the measured request, and the coverage of that sampling is reported beside the values,
because a median over two samples is not a median.

NO FOREIGN PROCESS IS TOUCHED. ollama and anything else on the GPU stay up and are
recorded per block, including what changed since the previous block. A foreign
llama-server before the series is a SETUP ERROR, not something to clean away: killing an
unknown server would destroy the very evidence that the run was not clean.

WHY NOT sample-counters.ps1. It exists and it is proven, but its contract is a different
one: 1 Hz, an idle gate that ABORTS before llama starts, its own exit codes, and no GPU
at all. Under that gate this series would never run, because the GPU is busy by design.
The raw-counter delta duplicated here is ~15 lines and is stated, not hidden.

A DEFECT IN THE PREVIOUS VERSION, measured before it was replaced: Test-Med was CALLED at
line 173 and DEFINED at line 190. In a script file a function does not exist before its
definition is reached, the error was non-terminating under $ErrorActionPreference =
'Continue', $medBad stayed $null, and $null.Count is 0 - so the tool printed "median
self-test: 4 of 4 cases green" without ever running a case. A self-test that cannot go
red. Every function here is defined before first use, and -Selftest is a phase of its own
that exits with its own code.

Usage:
  measure-drafting-ab.ps1 -Selftest                        # instrument proof, no model run
  measure-drafting-ab.ps1 -WT <tree> -Bin <reldir> -Blocks 'A,B,B,A'
  measure-drafting-ab.ps1 -Blocks 'A,B,B,A,A,B,B,A' -Tokens 512

Exit 0 = series complete and comparable.  1 = an abort criterion fired.  2 = setup error.
#>
param(
    [string]$WT      = 'C:\Users\robin\dev\crow-lab\wt-e12',
    [string]$Bin     = 'build-e12\bin\Release',
    [string]$Lab     = 'C:\Users\robin\dev\crow-lab',
    [string]$CROW    = 'C:\Users\robin\dev\Crow',
    [string]$Model   = 'models/UD-Q2_K_XL/DeepSeek-V4-Flash-UD-Q2_K_XL-00001-of-00003.gguf',
    [string]$Drafter = 'models/DSV4-Flash-DSpark-draft-bf16.gguf',
    [string]$SpecType = 'draft-dspark',
    # Fixed before the start. Two evaluable runs per side for the cause probe.
    #
    # A block is ONE server process: one discarded warm-up plus N evaluated requests. 'A' means
    # one evaluated request, 'A2' means two out of the same server. The default keeps the E12
    # shape (four blocks, one request each) so every existing caller measures what it did before.
    [string]$Blocks  = 'A,B,B,A',
    # TWO-BINARY MODE. E12 asked "same build, -md on or off", so A and B differed by a flag.
    # #53 asks "two BASES at the same operating point", where the variable is the executable and
    # -md is off on both sides. Set both and the harness switches the exe per block and adds no
    # -md at all; leave them empty and nothing about the E12 behaviour changes.
    [string]$ServerExeA = '',
    [string]$ServerExeB = '',
    # ONE BINARY, TWO PLACEMENTS. #24 asks what CPU-MoE and SSD streaming reach on this machine,
    # so the variable is the MoE flag set and the executable must NOT move - a difference in the
    # binary would put the placement question and the build question into one number. Set both
    # and the harness drops the fixed streaming flags from the base and appends the per-arm set
    # instead; leave them empty and nothing about the E12 or #53 behaviour changes.
    [string[]]$ArmFlagsA = @(),
    [string[]]$ArmFlagsB = @(),
    [int]   $Port    = 8081,
    [int]   $Ctx     = 4096,
    [int]   $Ngl     = 99,
    [int]   $Tokens  = 512,
    [int]   $Verbosity = 5,
    [int]   $HealthTimeoutSec = 420,
    [int]   $SampleMs = 500,
    [int]   $MinSamples = 5,
    [int]   $MaxGapMs = 2000,
    [string]$OutRoot = '',
    [switch]$Selftest
)

$ErrorActionPreference = 'Continue'
$INV = [Globalization.CultureInfo]::InvariantCulture

function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }
function N([object]$v, [int]$d) {
    if ($null -eq $v) { return '-' }
    return ([double]$v).ToString('F' + $d, $INV)
}

# --------------------------------------------------------------------------- statistics
# Even n averages the two middle values. The first version returned $s[floor(n/2)], the
# UPPER middle, and reported 95.03 and 92.54 for sets whose medians are 94.235 and 91.63 -
# figures labelled "median" that were not one.
function Med($xs) {
    $v = @($xs | Where-Object { $null -ne $_ })
    if ($v.Count -eq 0) { return $null }
    $s = @($v | Sort-Object)
    $n = $s.Count
    if ($n % 2 -eq 1) { return [double]$s[[int](($n - 1) / 2)] }
    return ([double]$s[$n/2 - 1] + [double]$s[$n/2]) / 2.0
}

function Stat($xs) {
    $v = @($xs | Where-Object { $null -ne $_ } | ForEach-Object { [double]$_ })
    if ($v.Count -eq 0) { return [pscustomobject]@{ n = 0; min = $null; med = $null; max = $null } }
    return [pscustomobject]@{
        n   = $v.Count
        min = ($v | Measure-Object -Minimum).Minimum
        med = (Med $v)
        max = ($v | Measure-Object -Maximum).Maximum
    }
}

# --------------------------------------------------------------------------- counters
# The raw classes are cumulative and language neutral. Measured on this machine
# 2026-08-05: Win32_PerfRawData_PerfProc_Process.IOReadBytesPersec and
# Win32_Process.ReadTransferCount return the SAME value for the same process, so one
# query is enough and the equality is a fact, not an assumption.
function Get-ProcCounter([int]$TargetPid) {
    $r = Get-CimInstance -Query "SELECT IOReadBytesPersec, IOOtherBytesPersec, Timestamp_PerfTime, Frequency_PerfTime FROM Win32_PerfRawData_PerfProc_Process WHERE IDProcess=$TargetPid" -ErrorAction SilentlyContinue
    if (-not $r) { return $null }
    return [pscustomobject]@{
        read_bytes  = [uint64]$r.IOReadBytesPersec
        other_bytes = [uint64]$r.IOOtherBytesPersec
        ts          = [uint64]$r.Timestamp_PerfTime
        freq        = [uint64]$r.Frequency_PerfTime
        at          = (Get-Date).ToString('o')
    }
}

function Get-DiskCounter {
    $r = Get-CimInstance -Query "SELECT DiskReadBytesPersec, Timestamp_PerfTime, Frequency_PerfTime FROM Win32_PerfRawData_PerfDisk_PhysicalDisk WHERE Name='_Total'" -ErrorAction SilentlyContinue
    if (-not $r) { return $null }
    return [pscustomobject]@{
        read_bytes = [uint64]$r.DiskReadBytesPersec
        ts         = [uint64]$r.Timestamp_PerfTime
        freq       = [uint64]$r.Frequency_PerfTime
        at         = (Get-Date).ToString('o')
    }
}

# A cumulative counter that goes DOWN means a reset or a different instance. Returning a
# negative or wrapped delta would look like a measurement; it is the absence of one.
function Get-CounterDelta([object]$Before, [object]$After) {
    if ($null -eq $Before -or $null -eq $After) {
        return [pscustomobject]@{ ok = $false; delta = $null; why = 'counter missing on one side' }
    }
    if ([uint64]$After.read_bytes -lt [uint64]$Before.read_bytes) {
        return [pscustomobject]@{ ok = $false; delta = $null; why = ('counter went backwards: {0} -> {1}' -f $Before.read_bytes, $After.read_bytes) }
    }
    return [pscustomobject]@{ ok = $true; delta = ([uint64]$After.read_bytes - [uint64]$Before.read_bytes); why = '' }
}

# --------------------------------------------------------------------------- identity
function Resolve-ServerExe([string]$Path, [string]$LabRoot) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ ok = $false; why = "not found: $Path" }
    }
    $full = (Resolve-Path -LiteralPath $Path).ProviderPath
    $prod = (Join-Path $LabRoot 'src\build-native') + '\'
    if ($full.StartsWith($prod, [StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject]@{ ok = $false; why = "refusing the production build: $full" }
    }
    return [pscustomobject]@{ ok = $true; why = ''; full = $full }
}

# PID alone is not an identity: Windows reuses process ids. The pair (pid, creation time)
# is one, and the executable path proves the binary under test is the binary running.
function Get-ProcIdentity([int]$TargetPid) {
    $p = Get-CimInstance -Query "SELECT ProcessId, ExecutablePath, CommandLine, CreationDate FROM Win32_Process WHERE ProcessId=$TargetPid" -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    return [pscustomobject]@{
        pid_        = [int]$p.ProcessId
        exe         = [string]$p.ExecutablePath
        commandline = [string]$p.CommandLine
        created     = $(if ($p.CreationDate) { ([datetime]$p.CreationDate).ToString('o') } else { '' })
    }
}

function Test-SameProcess([object]$A, [object]$B) {
    if ($null -eq $A -or $null -eq $B) { return $false }
    return (($A.pid_ -eq $B.pid_) -and ($A.created -eq $B.created))
}

function Get-Sha([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

# --------------------------------------------------------------------------- log reading
# llama-server holds the log open, so a plain read throws - and the throw used to leave an
# EMPTY array behind, which made seven checks report "PATTERN NOT FOUND" over a drafter
# that had loaded. Opened with FileShare::ReadWrite, and a read error is reported.
# Decoded with replacement, never strictly: a -lv 5 log carries raw token bytes and one
# 0xef made strict utf-8 AND utf-16 fail, returning [] - the same value as "never drafted".
function Read-LogTail([string]$Path, [long]$Offset) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ ok = $false; text = ''; why = "log missing: $Path"; end = $Offset }
    }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    } catch {
        return [pscustomobject]@{ ok = $false; text = ''; why = ("cannot open log: " + $_.Exception.Message); end = $Offset }
    }
    try {
        $len = $fs.Length
        if ($Offset -gt $len) {
            return [pscustomobject]@{ ok = $false; text = ''; why = "offset $Offset beyond length $len"; end = $len }
        }
        [void]$fs.Seek($Offset, [IO.SeekOrigin]::Begin)
        $n = [int]($len - $Offset)
        $buf = New-Object byte[] $n
        $got = 0
        while ($got -lt $n) {
            $r = $fs.Read($buf, $got, $n - $got)
            if ($r -le 0) { break }
            $got += $r
        }
        $enc = [Text.Encoding]::GetEncoding('utf-8', [Text.EncoderFallback]::ReplacementFallback, [Text.DecoderFallback]::ReplacementFallback)
        return [pscustomobject]@{ ok = $true; text = $enc.GetString($buf, 0, $got); why = ''; end = ($Offset + $got) }
    } finally {
        $fs.Close()
    }
}

function Get-LogLength([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try { return [long]$fs.Length } finally { $fs.Close() }
    } catch { return -1 }
}

# The server prints, per request that drafted anything:
#   draft acceptance = 0.85088 (  194 accepted /   228 generated), mean len =  3.55
# and, per verification step:
#   add accepted tokens: sampled=260, ids.size=4, n_draft=3
# ids.size is the tokens COMMITTED in that step, which is the accepted draft tokens plus
# the always-committed sampled one. Measured 2026-08-05 over the E11 log: 12x1 + 21x2 +
# 24x3 + 171x4 = 810 over 228 steps = 3.55, exactly the printed mean len, and
# sum(ids.size - 1) = 582 over three requests = 194 accepted each. So the per-step lines
# reconstruct the summary and give its distribution, which the summary alone does not.
#
# null and 0.0 are different answers and stay different: no acceptance line at all means
# the draft path was not active (null); a line with 0 accepted out of n>0 generated means
# it was active and nothing was taken (0.0).
function Parse-DraftLog([string]$Text) {
    $out = [pscustomobject]@{
        acceptance_lines = 0
        drafted          = $null
        accepted         = $null
        accept_rate      = $null
        mean_len         = $null
        block_size       = $null
        steps            = 0
        ids_sizes        = @()
        n_drafts         = @()
        note             = ''
    }
    if (-not $Text) { return $out }

    $ms = [regex]::Matches($Text, 'draft acceptance\s*=\s*([0-9.]+)\s*\(\s*([0-9]+)\s+accepted\s*/\s*([0-9]+)\s+generated\s*\)(?:,\s*mean len\s*=\s*([0-9.]+))?')
    $out.acceptance_lines = $ms.Count
    if ($ms.Count -gt 0) {
        $m = $ms[$ms.Count - 1]
        $out.accepted = [int]$m.Groups[2].Value
        $out.drafted  = [int]$m.Groups[3].Value
        if ($out.drafted -gt 0) {
            $out.accept_rate = [math]::Round([double]$out.accepted / [double]$out.drafted, 5)
        }
        if ($m.Groups[4].Success) {
            $out.mean_len = [double]::Parse($m.Groups[4].Value, $INV)
        }
    }
    if ($null -eq $out.mean_len -and $out.acceptance_lines -gt 0) {
        $out.note = "acceptance line present but no parsable 'mean len'"
    }

    $bs = [regex]::Match($Text, 'block_size\s*[=:]?\s*([0-9]+)')
    if ($bs.Success) { $out.block_size = [int]$bs.Groups[1].Value }

    $st = [regex]::Matches($Text, 'add accepted tokens:\s*sampled=[0-9]+,\s*ids\.size=([0-9]+),\s*n_draft=([0-9]+)')
    $out.steps = $st.Count
    if ($st.Count -gt 0) {
        $out.ids_sizes = @($st | ForEach-Object { [int]$_.Groups[1].Value })
        $out.n_drafts  = @($st | ForEach-Object { [int]$_.Groups[2].Value })
    }
    return $out
}

# --------------------------------------------------------------------------- telemetry
function Get-GpuSample {
    $csv = & nvidia-smi --query-gpu=utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,power.draw,temperature.gpu,clocks_throttle_reasons.active --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $csv) { return $null }
    $f = ([string]$csv).Split(',')
    if ($f.Count -lt 8) { return $null }
    return [pscustomobject]@{
        gpu_util = [double]::Parse($f[0].Trim(), $INV)
        mem_util = [double]::Parse($f[1].Trim(), $INV)
        vram_mib = [double]::Parse($f[2].Trim(), $INV)
        sm_mhz   = [double]::Parse($f[3].Trim(), $INV)
        mem_mhz  = [double]::Parse($f[4].Trim(), $INV)
        power_w  = [double]::Parse($f[5].Trim(), $INV)
        temp_c   = [double]::Parse($f[6].Trim(), $INV)
        throttle = $f[7].Trim()
    }
}

# A median over two samples is not a median, and a sampler that stopped early looks
# exactly like a quiet request. Coverage is therefore reported beside the values and a
# request with too few samples or too large a gap is REFUSED, not summarised.
function Get-TelemetryStats([object[]]$Samples, [datetime]$T0, [datetime]$T1, [int]$MinN, [int]$MaxGap) {
    $n = @($Samples).Count
    $res = [pscustomobject]@{
        ok = $false; why = ''; n = $n
        request_ms = [math]::Round(($T1 - $T0).TotalMilliseconds, 1)
        first_offset_ms = $null; last_before_end_ms = $null; max_gap_ms = $null
        gpu_util = $null; mem_util = $null; vram_mib = $null
        sm_mhz = $null; mem_mhz = $null; power_w = $null; temp_c = $null
        ws_mib = $null; ws_private_mib = $null; sys_avail_mib = $null; ws_peak_mib = $null
        throttle_nonzero = 0; throttle_values = @()
    }
    if ($n -lt $MinN) { $res.why = "only $n samples, want at least $MinN"; return $res }

    $times = @($Samples | ForEach-Object { [datetime]::Parse($_.at, $INV, [Globalization.DateTimeStyles]::RoundtripKind) })
    $res.first_offset_ms    = [math]::Round(($times[0] - $T0).TotalMilliseconds, 1)
    $res.last_before_end_ms = [math]::Round(($T1 - $times[$n-1]).TotalMilliseconds, 1)
    $gaps = @()
    for ($i = 1; $i -lt $n; $i++) { $gaps += ($times[$i] - $times[$i-1]).TotalMilliseconds }
    $edge = @($res.first_offset_ms, $res.last_before_end_ms) + $gaps
    $res.max_gap_ms = [math]::Round((@($edge) | Measure-Object -Maximum).Maximum, 1)
    if ($res.max_gap_ms -gt $MaxGap) { $res.why = "largest uncovered gap $($res.max_gap_ms) ms exceeds $MaxGap ms"; return $res }

    $res.gpu_util = Stat @($Samples | ForEach-Object { $_.gpu_util })
    $res.mem_util = Stat @($Samples | ForEach-Object { $_.mem_util })
    $res.vram_mib = Stat @($Samples | ForEach-Object { $_.vram_mib })
    $res.sm_mhz   = Stat @($Samples | ForEach-Object { $_.sm_mhz })
    $res.mem_mhz  = Stat @($Samples | ForEach-Object { $_.mem_mhz })
    $res.power_w  = Stat @($Samples | ForEach-Object { $_.power_w })
    $res.temp_c   = Stat @($Samples | ForEach-Object { $_.temp_c })
    $res.ws_mib         = Stat @($Samples | ForEach-Object { $_.ws_mib })
    $res.ws_private_mib = Stat @($Samples | ForEach-Object { $_.ws_private_mib })
    $res.sys_avail_mib  = Stat @($Samples | ForEach-Object { $_.sys_avail_mib })
    # Not a Stat: WorkingSetPeak is monotone over the life of the process, so a median of it
    # means nothing. Only its last value is a statement, and it is the process peak.
    $wsp = @($Samples | ForEach-Object { $_.ws_peak_mib } | Where-Object { $null -ne $_ })
    $res.ws_peak_mib = $(if ($wsp.Count) { ($wsp | Measure-Object -Maximum).Maximum } else { $null })
    $tv = @($Samples | ForEach-Object { $_.throttle } | Sort-Object -Unique)
    $res.throttle_values = $tv
    $res.throttle_nonzero = @($Samples | Where-Object { $_.throttle -and ($_.throttle -replace '0x0*', '') -ne '' }).Count
    $res.ok = $true
    return $res
}

# The sampler runs in a runspace, because Invoke-RestMethod blocks the calling thread for
# the whole request and a sample taken before or after it says nothing about the load.
# The tick follows an ABSOLUTE schedule: "sleep X then work" drifts by the cost of the
# work itself, which for one WMI query plus one nvidia-smi call is ~170 ms of a 500 ms
# period. sample-counters.ps1 learned the same lesson at 1 Hz and delivered 3 rows in 5 s.
function Start-Sampler([int]$TargetPid, [int]$IntervalMs) {
    $bag  = [Collections.ArrayList]::Synchronized((New-Object Collections.ArrayList))
    $flag = [hashtable]::Synchronized(@{ stop = $false })
    $rs = [runspacefactory]::CreateRunspace()
    $rs.Open()
    $rs.SessionStateProxy.SetVariable('bag', $bag)
    $rs.SessionStateProxy.SetVariable('flag', $flag)
    $rs.SessionStateProxy.SetVariable('targetPid', $TargetPid)
    $rs.SessionStateProxy.SetVariable('intervalMs', $IntervalMs)
    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript({
        $inv = [Globalization.CultureInfo]::InvariantCulture
        $t0 = [DateTime]::UtcNow
        $i = 0
        while (-not $flag.stop) {
            $due = $t0.AddMilliseconds($i * $intervalMs)
            $wait = ($due - [DateTime]::UtcNow).TotalMilliseconds
            if ($wait -gt 0) { Start-Sleep -Milliseconds ([int][math]::Min($wait, $intervalMs)) }
            if ($flag.stop) { break }
            $i++

            $at = (Get-Date).ToString('o')
            $gpu = $null
            try {
                $csv = & nvidia-smi --query-gpu=utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,power.draw,temperature.gpu,clocks_throttle_reasons.active --format=csv,noheader,nounits 2>$null
                if ($csv) {
                    $f = ([string]$csv).Split(',')
                    if ($f.Count -ge 8) {
                        $gpu = [pscustomobject]@{
                            gpu_util = [double]::Parse($f[0].Trim(), $inv); mem_util = [double]::Parse($f[1].Trim(), $inv)
                            vram_mib = [double]::Parse($f[2].Trim(), $inv); sm_mhz   = [double]::Parse($f[3].Trim(), $inv)
                            mem_mhz  = [double]::Parse($f[4].Trim(), $inv); power_w  = [double]::Parse($f[5].Trim(), $inv)
                            temp_c   = [double]::Parse($f[6].Trim(), $inv); throttle = $f[7].Trim()
                        }
                    }
                }
            } catch { }

            $pr = $null; $dk = $null; $alive = $false
            $ws = $null; $wsPeak = $null; $wsPriv = $null; $availMib = $null
            try {
                # One query, four values. WorkingSetPeak is kept BESIDE the sampled WorkingSet on
                # purpose: the sampled series can only see peaks that happen to fall on a tick,
                # and the OS-maintained peak cannot miss one. If the two disagree, the sampling
                # rate was too coarse - which is a finding about the measurement, not noise.
                $q = Get-CimInstance -Query "SELECT IOReadBytesPersec,WorkingSet,WorkingSetPeak,WorkingSetPrivate FROM Win32_PerfRawData_PerfProc_Process WHERE IDProcess=$targetPid" -ErrorAction SilentlyContinue
                if ($q) {
                    $pr = [uint64]$q.IOReadBytesPersec; $alive = $true
                    $ws     = [math]::Round(([uint64]$q.WorkingSet)        / 1MB, 1)
                    $wsPeak = [math]::Round(([uint64]$q.WorkingSetPeak)    / 1MB, 1)
                    $wsPriv = [math]::Round(([uint64]$q.WorkingSetPrivate) / 1MB, 1)
                }
            } catch { }
            try {
                $q = Get-CimInstance -Query "SELECT DiskReadBytesPersec FROM Win32_PerfRawData_PerfDisk_PhysicalDisk WHERE Name='_Total'" -ErrorAction SilentlyContinue
                if ($q) { $dk = [uint64]$q.DiskReadBytesPersec }
            } catch { }
            try {
                # AvailableMBytes, not "free": standby pages count as available. Named accordingly
                # so nobody later reads it as unused memory - #38 already had that confusion once.
                $q = Get-CimInstance -Query "SELECT AvailableMBytes FROM Win32_PerfRawData_PerfOS_Memory" -ErrorAction SilentlyContinue
                if ($q) { $availMib = [double]$q.AvailableMBytes }
            } catch { }

            $row = [pscustomobject]@{
                at = $at; proc_alive = $alive
                proc_read_bytes = $pr; disk_read_bytes = $dk
                ws_mib = $ws; ws_peak_mib = $wsPeak; ws_private_mib = $wsPriv
                sys_avail_mib = $availMib
                gpu_util = $(if ($gpu) { $gpu.gpu_util } else { $null })
                mem_util = $(if ($gpu) { $gpu.mem_util } else { $null })
                vram_mib = $(if ($gpu) { $gpu.vram_mib } else { $null })
                sm_mhz   = $(if ($gpu) { $gpu.sm_mhz }   else { $null })
                mem_mhz  = $(if ($gpu) { $gpu.mem_mhz }  else { $null })
                power_w  = $(if ($gpu) { $gpu.power_w }  else { $null })
                temp_c   = $(if ($gpu) { $gpu.temp_c }   else { $null })
                throttle = $(if ($gpu) { $gpu.throttle } else { '' })
            }
            [void]$bag.Add($row)
        }
    })
    $handle = $ps.BeginInvoke()
    return [pscustomobject]@{ ps = $ps; rs = $rs; handle = $handle; bag = $bag; flag = $flag }
}

function Stop-Sampler([object]$S) {
    if (-not $S) { return @() }
    $S.flag.stop = $true
    try { [void]$S.ps.EndInvoke($S.handle) } catch { }
    try { $S.ps.Dispose() } catch { }
    try { $S.rs.Close(); $S.rs.Dispose() } catch { }
    return @($S.bag.ToArray())
}

# --------------------------------------------------------------------------- foreign load
function Get-ForeignProcs {
    $rows = @()
    foreach ($p in (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'ollama|llama|python|stable|comfy' })) {
        $start = ''
        try { $start = $p.StartTime.ToString('o') } catch { }
        $rows += [pscustomobject]@{ name = $p.ProcessName; pid_ = $p.Id; started = $start }
    }
    return @($rows | Sort-Object name, pid_)
}

function Diff-Foreign([object[]]$Prev, [object[]]$Now) {
    $key = { param($x) "$($x.name)/$($x.pid_)" }
    $p = @($Prev | ForEach-Object { & $key $_ })
    $n = @($Now  | ForEach-Object { & $key $_ })
    $added   = @($n | Where-Object { $p -notcontains $_ })
    $removed = @($p | Where-Object { $n -notcontains $_ })
    return [pscustomobject]@{ added = $added; removed = $removed }
}

# --------------------------------------------------------------------------- block spec
# 'A' is one evaluated request, 'A2' is two out of the SAME server process. A value function:
# it returns the parsed sequence or a refusal and narrates nothing, so a caller can test it.
#
# Parsed before anything starts. A typo in the sequence is otherwise discovered after the first
# twenty-minute block, by which time the machine state the run assumed is gone.
function Parse-Blocks([string]$Spec, [switch]$AllowUnbalanced) {
    $seq = @()
    foreach ($tok in ($Spec -split ',')) {
        $t = $tok.Trim().ToUpper()
        if ($t -notmatch '^([AB])(\d*)$') {
            return [pscustomobject]@{ ok = $false; why = "unknown block '$t' (expected A, B, A2, B4, ...)"; seq = @(); nA = 0; nB = 0 }
        }
        $n = $(if ($Matches[2]) { [int]$Matches[2] } else { 1 })
        if ($n -lt 1) {
            return [pscustomobject]@{ ok = $false; why = "block '$t' asks for $n requests"; seq = @(); nA = 0; nB = 0 }
        }
        $seq += [pscustomobject]@{ side = $Matches[1]; n = $n }
    }
    if ($seq.Count -eq 0) { return [pscustomobject]@{ ok = $false; why = 'empty sequence'; seq = @(); nA = 0; nB = 0 } }
    $nA = (@($seq | Where-Object { $_.side -eq 'A' } | ForEach-Object { $_.n }) | Measure-Object -Sum).Sum
    $nB = (@($seq | Where-Object { $_.side -eq 'B' } | ForEach-Object { $_.n }) | Measure-Object -Sum).Sum
    if (-not $nA) { $nA = 0 }
    if (-not $nB) { $nB = 0 }
    # An unbalanced sequence is refused HERE rather than noticed in the report. Six against four
    # is not a smaller comparison, it is a different one: the side with more requests carries more
    # of whatever drifted during the series, and no amount of care in the analysis undoes that.
    # -AllowUnbalanced exists so the refusal itself can be exercised from both directions.
    if (-not $AllowUnbalanced -and $nA -ne $nB) {
        return [pscustomobject]@{ ok = $false; why = "unbalanced sequence: A $nA against B $nB"; seq = @(); nA = $nA; nB = $nB }
    }
    if ($nA -eq 0 -or $nB -eq 0) {
        return [pscustomobject]@{ ok = $false; why = "one-sided sequence: A $nA, B $nB"; seq = @(); nA = $nA; nB = $nB }
    }
    return [pscustomobject]@{ ok = $true; why = ''; seq = $seq; nA = $nA; nB = $nB }
}

# Which tree an executable came out of, and at which commit. Recorded per request because
# "b10223" in a filename is a claim and `git rev-parse` in the tree that produced the binary is
# a measurement. Walks up from the exe until it finds the worktree root (.git is a FILE in a
# linked worktree, not a directory - both are accepted).
function Get-TreeIdentity([string]$ExePath) {
    $dir = Split-Path -Parent $ExePath
    for ($i = 0; $i -lt 8 -and $dir; $i++) {
        if (Test-Path (Join-Path $dir '.git')) {
            $sha = (& git -C $dir rev-parse HEAD 2>$null)
            $desc = (& git -C $dir describe --tags --exact-match HEAD 2>$null)
            return [pscustomobject]@{ ok = $true; root = $dir; commit = "$sha".Trim(); tag = "$desc".Trim() }
        }
        $dir = Split-Path -Parent $dir
    }
    return [pscustomobject]@{ ok = $false; root = ''; commit = ''; tag = '' }
}

# Which arguments a side gets. A VALUE function: it starts nothing and narrates nothing, so the
# three modes can be asserted without a server, a model or a GPU. Inline, this decision was three
# nested ifs in the block loop and the only way to check it was to read a 400 MB server log.
function Get-SideArgs {
    param(
        [string[]]$Base,
        [string]  $Side,
        [string]  $Mode,
        [string[]]$FlagsA = @(),
        [string[]]$FlagsB = @(),
        [string]  $DrafterPath = ''
    )
    switch ($Mode) {
        'arm-flags' { return @($Base) + @($(if ($Side -eq 'A') { $FlagsA } else { $FlagsB })) }
        'md-toggle' { return @($Base) + @($(if ($Side -eq 'A') { @('-md', $DrafterPath) } else { @() })) }
        # two-binary: the executable is the variable, so both sides get identical arguments.
        default     { return @($Base) }
    }
}

# --------------------------------------------------------------------------- selftest
# Every case below must be able to go RED. Where a case asserts an absence, a presence
# from the same run stands beside it - a zero without its non-zero is not a measurement.
function Invoke-Selftest {
    function Case([string]$name, [bool]$pass, [string]$detail) {
        $script:cases += [pscustomobject]@{ name = $name; pass = $pass; detail = $detail }
    }
    $script:cases = @()

    # 1 + 2  server path accepted / rejected
    $tmp = Join-Path $env:TEMP ("mdab-selftest-{0}" -f $PID)
    if (-not (Test-Path $tmp)) { New-Item -ItemType Directory -Path $tmp -Force | Out-Null }
    $fake = Join-Path $tmp 'llama-server.exe'
    'x' | Out-File -Encoding ascii $fake
    $r1 = Resolve-ServerExe $fake $Lab
    Case 'server path accepted' ($r1.ok -eq $true) $r1.why
    $r2 = Resolve-ServerExe (Join-Path $tmp 'does-not-exist.exe') $Lab
    Case 'wrong server path rejected' ($r2.ok -eq $false) $r2.why
    $prodPath = Join-Path $Lab 'src\build-native\bin\Release\llama-server.exe'
    $r2b = Resolve-ServerExe $prodPath $Lab
    Case 'production build refused' ($r2b.ok -eq $false) $r2b.why

    # 2b  block spec: counts, the preserved default, and two controls that MUST refuse
    $bp1 = Parse-Blocks 'A2,B4,A3,B2,A1'
    $sumA = (@($bp1.seq | Where-Object { $_.side -eq 'A' } | ForEach-Object { $_.n }) | Measure-Object -Sum).Sum
    $sumB = (@($bp1.seq | Where-Object { $_.side -eq 'B' } | ForEach-Object { $_.n }) | Measure-Object -Sum).Sum
    Case 'block spec: 5 blocks parsed' ($bp1.ok -and $bp1.seq.Count -eq 5) ("blocks=" + $bp1.seq.Count)
    Case 'block spec: 6 requests per side' ($sumA -eq 6 -and $sumB -eq 6) ("A=$sumA B=$sumB")
    $bp2 = Parse-Blocks 'A,B,B,A'
    $sumA2 = (@($bp2.seq | Where-Object { $_.side -eq 'A' } | ForEach-Object { $_.n }) | Measure-Object -Sum).Sum
    Case 'block spec: bare letter still means one request' ($bp2.ok -and $bp2.seq.Count -eq 4 -and $sumA2 -eq 2) ("A=$sumA2 blocks=" + $bp2.seq.Count)
    $bp3 = Parse-Blocks 'A,C'
    Case 'block spec: unknown side REFUSED' ($bp3.ok -eq $false) $bp3.why
    $bp4 = Parse-Blocks 'A0'
    Case 'block spec: zero requests REFUSED' ($bp4.ok -eq $false) $bp4.why
    $bp5 = Parse-Blocks 'A2,B4'
    Case 'block spec: unbalanced REFUSED' ($bp5.ok -eq $false) $bp5.why
    $bp6 = Parse-Blocks 'A2,B4' -AllowUnbalanced
    Case 'block spec: same input accepted when imbalance is allowed' ($bp6.ok -eq $true -and $bp6.nA -eq 2 -and $bp6.nB -eq 4) ("A=" + $bp6.nA + " B=" + $bp6.nB)
    $bp7 = Parse-Blocks 'A2,A4'
    Case 'block spec: one-sided REFUSED' ($bp7.ok -eq $false) $bp7.why

    # 3  pid change / pid reuse
    $a = [pscustomobject]@{ pid_ = 4242; exe = 'x'; created = '2026-08-05T23:00:00.0000000Z' }
    $b = [pscustomobject]@{ pid_ = 4242; exe = 'x'; created = '2026-08-05T23:30:00.0000000Z' }
    Case 'same process recognised'  ((Test-SameProcess $a $a) -eq $true)  'identical pair'
    Case 'pid reuse detected'       ((Test-SameProcess $a $b) -eq $false) 'same pid, different creation time'

    # 4 + 5  counter delta and counter reset
    $d1 = Get-CounterDelta ([pscustomobject]@{ read_bytes = [uint64]100 }) ([pscustomobject]@{ read_bytes = [uint64]350 })
    Case 'monotone counter gives delta' (($d1.ok -eq $true) -and ($d1.delta -eq 250)) ("delta=" + $d1.delta)
    $d2 = Get-CounterDelta ([pscustomobject]@{ read_bytes = [uint64]350 }) ([pscustomobject]@{ read_bytes = [uint64]100 })
    Case 'counter reset rejected' ($d2.ok -eq $false) $d2.why

    # 6  mean len read from a real line, and from one with the 0xef that broke strict decode
    $line = 'draft acceptance = 0.85088 (  194 accepted /   228 generated), mean len =  3.55'
    $p1 = Parse-DraftLog $line
    Case 'mean_accepted_len = 3.55 read' (($null -ne $p1.mean_len) -and ([math]::Abs($p1.mean_len - 3.55) -lt 0.0005)) ("got=" + (N $p1.mean_len 2))
    Case 'accepted/drafted read'         (($p1.accepted -eq 194) -and ($p1.drafted -eq 228)) ("$($p1.accepted)/$($p1.drafted)")
    $logf = Join-Path $tmp 'tolerant.log'
    $bytes = [Text.Encoding]::ASCII.GetBytes("noise`n") + @([byte]0xef) + [Text.Encoding]::ASCII.GetBytes("`n$line`n")
    [IO.File]::WriteAllBytes($logf, $bytes)
    $tail = Read-LogTail $logf 0
    $p1b = Parse-DraftLog $tail.text
    Case 'tolerant decode survives 0xef' (($tail.ok -eq $true) -and ($null -ne $p1b.mean_len)) ("ok=" + $tail.ok + " mean=" + (N $p1b.mean_len 2))

    # 7 + 8  null is not 0.0
    $p2 = Parse-DraftLog "nothing about drafting here`nslot released`n"
    Case 'no acceptance line gives null' (($null -eq $p2.accept_rate) -and ($p2.acceptance_lines -eq 0)) 'rate=null'
    $p3 = Parse-DraftLog 'draft acceptance = 0.00000 (    0 accepted /   228 generated), mean len =  1.00'
    $isNumericZero = ($null -ne $p3.accept_rate) -and ([double]$p3.accept_rate -eq 0.0)
    Case 'active-but-zero stays numeric 0.0' $isNumericZero ("rate=" + $(if ($null -eq $p3.accept_rate) { 'null' } else { N $p3.accept_rate 5 }))

    # 9 + 10  median, both parities
    Case 'median even 1,2,3,4 = 2.5' ([math]::Abs((Med @(1,2,3,4)) - 2.5) -lt 0.0005) ("got=" + (N (Med @(1,2,3,4)) 3))
    Case 'median odd 1,2,3 = 2'      ([math]::Abs((Med @(1,2,3))   - 2.0) -lt 0.0005) ("got=" + (N (Med @(1,2,3)) 3))
    Case 'median of the E11 A-side = 94.235' ([math]::Abs((Med @(89.75,95.03,93.44,97.18)) - 94.235) -lt 0.0005) ("got=" + (N (Med @(89.75,95.03,93.44,97.18)) 3))

    # 11  telemetry refuses too few samples, and accepts enough of them
    $t0 = Get-Date
    $few = @(1,2 | ForEach-Object { [pscustomobject]@{ at = $t0.AddMilliseconds(500*$_).ToString('o'); gpu_util=1; mem_util=1; vram_mib=1; sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0' } })
    $t1 = $t0.AddSeconds(5)
    $s1 = Get-TelemetryStats $few $t0 $t1 $MinSamples $MaxGapMs
    Case 'telemetry refuses too few samples' ($s1.ok -eq $false) $s1.why
    $many = @(1..10 | ForEach-Object { [pscustomobject]@{ at = $t0.AddMilliseconds(500*$_).ToString('o'); gpu_util=$_; mem_util=1; vram_mib=1; sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0' } })
    $s2 = Get-TelemetryStats $many $t0 $t0.AddMilliseconds(5200) $MinSamples $MaxGapMs
    Case 'telemetry accepts a covered request' (($s2.ok -eq $true) -and ($s2.gpu_util.med -eq 5.5)) ("n=$($s2.n) med=" + (N $s2.gpu_util.med 2) + " why=" + $s2.why)
    $gappy = @(
        [pscustomobject]@{ at = $t0.AddMilliseconds(100).ToString('o'); gpu_util=1; mem_util=1; vram_mib=1; sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0' }
        [pscustomobject]@{ at = $t0.AddMilliseconds(600).ToString('o'); gpu_util=1; mem_util=1; vram_mib=1; sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0' }
        [pscustomobject]@{ at = $t0.AddMilliseconds(1100).ToString('o'); gpu_util=1; mem_util=1; vram_mib=1; sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0' }
        [pscustomobject]@{ at = $t0.AddMilliseconds(1600).ToString('o'); gpu_util=1; mem_util=1; vram_mib=1; sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0' }
        [pscustomobject]@{ at = $t0.AddMilliseconds(9000).ToString('o'); gpu_util=1; mem_util=1; vram_mib=1; sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0' }
    )
    $s3 = Get-TelemetryStats $gappy $t0 $t0.AddMilliseconds(9200) $MinSamples $MaxGapMs
    Case 'telemetry refuses an uncovered gap' ($s3.ok -eq $false) $s3.why

    # 12  cumulative against request-local
    $twoBlocks = "warmup noise`n" + 'draft acceptance = 0.50000 (  100 accepted /   200 generated), mean len =  2.00' + "`nMARK`n" + $line + "`n"
    $whole = Parse-DraftLog $twoBlocks
    $mark = $twoBlocks.IndexOf('MARK')
    $local = Parse-DraftLog $twoBlocks.Substring($mark)
    $distinguishable = ($whole.acceptance_lines -eq 2) -and ($local.acceptance_lines -eq 1) -and ($local.drafted -eq 228)
    Case 'request-local parse excludes the warm-up' $distinguishable ("whole=$($whole.acceptance_lines) local=$($local.acceptance_lines) drafted=$($local.drafted)")

    # 13  per-step lines reconstruct the summary - the check that the step parser is real
    $steps = @()
    foreach ($x in (1..12)) { $steps += 'add accepted tokens: sampled=1, ids.size=1, n_draft=3' }
    foreach ($x in (1..21)) { $steps += 'add accepted tokens: sampled=1, ids.size=2, n_draft=3' }
    foreach ($x in (1..24)) { $steps += 'add accepted tokens: sampled=1, ids.size=3, n_draft=3' }
    foreach ($x in (1..171)) { $steps += 'add accepted tokens: sampled=1, ids.size=4, n_draft=3' }
    $ps4 = Parse-DraftLog ($steps -join "`n")
    $sum = ($ps4.ids_sizes | Measure-Object -Sum).Sum
    $reconstructed = [math]::Round($sum / $ps4.steps, 2)
    Case 'per-step lines reconstruct mean len 3.55' (($ps4.steps -eq 228) -and ([math]::Abs($reconstructed - 3.55) -lt 0.005)) ("steps=$($ps4.steps) mean=" + (N $reconstructed 2))
    $noSteps = Parse-DraftLog 'draft acceptance = 0.85088 (  194 accepted /   228 generated), mean len =  3.55'
    Case 'no per-step lines gives zero steps' ($noSteps.steps -eq 0) ("steps=" + $noSteps.steps)

    # 14  the raw counters are actually readable on this machine, with a live and a dead pid
    $live = Get-ProcCounter $PID
    Case 'process counter readable for a live pid' (($null -ne $live) -and ($live.read_bytes -ge 0)) ("read_bytes=" + $(if ($live) { $live.read_bytes } else { 'null' }))
    $dead = Get-ProcCounter 999999
    Case 'process counter null for a dead pid' ($null -eq $dead) 'no row, as it must be'
    $disk = Get-DiskCounter
    Case 'disk counter readable' (($null -ne $disk) -and ($disk.read_bytes -gt 0)) ("read_bytes=" + $(if ($disk) { $disk.read_bytes } else { 'null' }))

    # 15  the GPU query this machine actually answers
    $g = Get-GpuSample
    Case 'gpu sample parses all eight fields' ($null -ne $g) $(if ($g) { "util=$($g.gpu_util) temp=$($g.temp_c) throttle=$($g.throttle)" } else { 'nvidia-smi query failed' })

    # 16  arm-flags mode: one binary, the MoE placement is the only variable. Each case has its
    #     counterpart - a mode that gave both sides the same arguments would pass an "A carries
    #     its flags" check on its own, so "B does NOT carry A's flags" stands beside it.
    $bA = @('-m','model.gguf','-c','4096','-ngl','99','-np','1','--spec-type','none')
    $fa = @('--n-cpu-moe','999')
    $fb = @('--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8','--moe-stream-direct')
    $sA = Get-SideArgs -Base $bA -Side 'A' -Mode 'arm-flags' -FlagsA $fa -FlagsB $fb
    $sB = Get-SideArgs -Base $bA -Side 'B' -Mode 'arm-flags' -FlagsA $fa -FlagsB $fb
    Case 'arm-flags: A carries the CPU-MoE set'      (($sA -join ' ').Contains('--n-cpu-moe 999')) ($sA -join ' ')
    Case 'arm-flags: A does NOT carry the stream set' (-not ($sA -contains '--moe-stream'))        'no --moe-stream on A'
    Case 'arm-flags: B carries the stream set'        ($sB -contains '--moe-stream-direct')        ($sB -join ' ')
    Case 'arm-flags: B does NOT carry the CPU-MoE set' (-not ($sB -contains '--n-cpu-moe'))        'no --n-cpu-moe on B'
    Case 'arm-flags: the shared base is identical on both sides' `
         ((($sA | Select-Object -First $bA.Count) -join ' ') -eq (($sB | Select-Object -First $bA.Count) -join ' ')) `
         'base survives on both arms'
    Case 'arm-flags: neither side gets -md'           ((-not ($sA -contains '-md')) -and (-not ($sB -contains '-md'))) 'drafting stays out of this mode'

    # 17  the two other modes are unchanged - the negative control for case 16. If arm-flags had
    #     leaked into them, these would go red and the E12/#53 callers would be measuring something
    #     other than what they measured before.
    $mA = Get-SideArgs -Base $bA -Side 'A' -Mode 'md-toggle' -DrafterPath 'draft.gguf'
    $mB = Get-SideArgs -Base $bA -Side 'B' -Mode 'md-toggle' -DrafterPath 'draft.gguf'
    Case 'md-toggle unchanged: A gets -md, B does not' `
         (($mA -contains '-md') -and (-not ($mB -contains '-md'))) ("A=" + $mA.Count + " B=" + $mB.Count)
    $tA = Get-SideArgs -Base $bA -Side 'A' -Mode 'two-binary'
    $tB = Get-SideArgs -Base $bA -Side 'B' -Mode 'two-binary'
    Case 'two-binary unchanged: both sides identical' (($tA -join ' ') -eq ($tB -join ' ')) 'arguments shared, exe is the variable'

    # 18  memory telemetry. ws_peak_mib is monotone, so it must be the MAXIMUM and never a median -
    #     a median would silently under-report the peak this issue asks for. The falling series is
    #     physically impossible for a peak counter and exists to prove the aggregation is not
    #     "take the last value", which would return 10 here.
    $t0m = [DateTime]::UtcNow
    $memRows = @(0..5 | ForEach-Object {
        [pscustomobject]@{
            at = $t0m.AddMilliseconds(500*$_).ToString('o')
            gpu_util=1; mem_util=1; vram_mib=(1000+$_); sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0'
            ws_mib=(100*$_); ws_private_mib=(90*$_); sys_avail_mib=(5000-$_)
            ws_peak_mib=$(if ($_ -eq 3) { 999 } else { 10 })
        }
    })
    $ms = Get-TelemetryStats $memRows $t0m $t0m.AddMilliseconds(3000) 5 2000
    Case 'memory telemetry: stats computed at all'  ($ms.ok -eq $true) $ms.why
    Case 'ws_peak_mib is the maximum, not the last' ($ms.ws_peak_mib -eq 999) ("got=" + $ms.ws_peak_mib)
    Case 'ws_mib aggregated with a real spread'     ($ms.ws_mib.max -eq 500 -and $ms.ws_mib.min -eq 0) ("min=" + $ms.ws_mib.min + " max=" + $ms.ws_mib.max)
    Case 'sys_avail_mib aggregated'                 ($ms.sys_avail_mib.max -eq 5000) ("max=" + $ms.sys_avail_mib.max)
    # The negative control: all-null memory columns must yield null, not 0. A zero would read as
    # "the process used no memory" and is indistinguishable from a counter that never answered.
    $nullRows = @($memRows | ForEach-Object {
        [pscustomobject]@{
            at = $_.at; gpu_util=1; mem_util=1; vram_mib=1; sm_mhz=1; mem_mhz=1; power_w=1; temp_c=1; throttle='0x0'
            ws_mib=$null; ws_private_mib=$null; sys_avail_mib=$null; ws_peak_mib=$null
        }
    })
    $ns = Get-TelemetryStats $nullRows $t0m $t0m.AddMilliseconds(3000) 5 2000
    Case 'absent memory counters give null, not zero' ($null -eq $ns.ws_peak_mib) ("got=" + $(if ($null -eq $ns.ws_peak_mib) { 'null' } else { $ns.ws_peak_mib }))

    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

    Write-Output ''
    Say ('=' * 78)
    Say 'INSTRUMENT SELFTEST - every case below can go red'
    Say ('=' * 78)
    $bad = 0
    foreach ($c in $script:cases) {
        $tag = $(if ($c.pass) { 'ok  ' } else { 'RED ' })
        if (-not $c.pass) { $bad++ }
        Say ("  {0} {1,-46} {2}" -f $tag, $c.name, $c.detail)
    }
    Write-Output ''
    if ($bad -eq 0) {
        Write-Output ("RESULT: PASS - {0} of {0} instrument cases green." -f $script:cases.Count)
        exit 0
    }
    Write-Output ("RESULT: FAIL - {0} of {1} instrument cases red." -f $bad, $script:cases.Count)
    exit 1
}

# =========================================================================== selftest phase
if ($Selftest) { Invoke-Selftest }

# =========================================================================== setup
if (-not $OutRoot) { $OutRoot = Join-Path $CROW ("runs\{0}\e12-cause-probe" -f (Get-Date -Format 'yyyy-MM-dd')) }
if (-not (Test-Path $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null }

$TwoBinary = [bool]($ServerExeA -and $ServerExeB)
# The two modes answer different questions and cannot be combined: two-binary holds the flags
# fixed and moves the executable, arm-flags holds the executable fixed and moves the flags.
# Setting both would move both and the result would attribute nothing.
$ArmFlags  = [bool]($ArmFlagsA.Count -gt 0 -and $ArmFlagsB.Count -gt 0)
if ($ArmFlags -and $TwoBinary) {
    Write-Output "SETUP ERROR: -ArmFlagsA/-ArmFlagsB and -ServerExeA/-ServerExeB are exclusive; both would move two variables at once"
    exit 2
}
if (($ArmFlagsA.Count -gt 0) -xor ($ArmFlagsB.Count -gt 0)) {
    Write-Output "SETUP ERROR: -ArmFlagsA and -ArmFlagsB must both be set; one alone makes the arms differ in an unstated way"
    exit 2
}

$exeReq = $(if ($TwoBinary) { $ServerExeA } else { Join-Path $WT (Join-Path $Bin 'llama-server.exe') })
$res = Resolve-ServerExe $exeReq $Lab
if (-not $res.ok) { Die $res.why }
$full = $res.full
$implDll = Join-Path (Split-Path -Parent $full) 'llama-server-impl.dll'
$shaExe  = Get-Sha $full
$shaDll  = Get-Sha $implDll

# The B binary gets the same treatment as A, including the refusal to measure the production
# build. Two exes that hash the same would make the whole comparison a null experiment, so that
# is checked here rather than discovered in the numbers.
$fullB = ''; $shaExeB = ''; $shaDllB = ''
if ($TwoBinary) {
    $resB = Resolve-ServerExe $ServerExeB $Lab
    if (-not $resB.ok) { Die $resB.why }
    $fullB   = $resB.full
    $implDllB= Join-Path (Split-Path -Parent $fullB) 'llama-server-impl.dll'
    $shaExeB = Get-Sha $fullB
    $shaDllB = Get-Sha $implDllB
    if ($full -eq $fullB)       { Die "two-binary mode with one path: $full" }
    if ($shaDll -eq $shaDllB)   { Die "two-binary mode but llama-server-impl.dll is identical on both sides ($shaDll) - there is nothing to compare" }
}

# A foreign llama-server before the series is evidence, not litter. Killing it would
# destroy the only sign that the machine was not in the state this run assumes.
$pre = @(Get-Process llama-server -ErrorAction SilentlyContinue)
if ($pre.Count -gt 0) { Die ("a llama-server is already running (pid " + (($pre | ForEach-Object { $_.Id }) -join ',') + ") - not touching it, and not measuring against it") }
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { Die "port $Port is already held" }

function Stop-OurServer([object]$Proc) {
    if ($null -eq $Proc) { return [pscustomobject]@{ how = 'no process'; exit_code = $null; port_free = $true } }
    $code = $null
    try { if (-not $Proc.HasExited) { $Proc.Kill() } } catch { }
    try { [void]$Proc.WaitForExit(30000) } catch { }
    try { $Proc.Refresh(); $code = $Proc.ExitCode } catch { }
    $free = $false
    for ($i = 0; $i -lt 60; $i++) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { $free = $true; break }
        Start-Sleep -Milliseconds 500
    }
    return [pscustomobject]@{ how = 'Kill+WaitForExit'; exit_code = $code; port_free = $free }
}

# ONE prompt for every request on both sides. Hoisted out of Ask so it can be hashed once and
# recorded per request: "both sides got the same prompt" is otherwise a claim about the source.
$PROMPT_TEXT = 'Write a Python function that reverses a linked list. Code only.'
function Get-Sha16([string]$s) {
    $h = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s))
    return (($h | ForEach-Object { '{0:x2}' -f $_ }) -join '').Substring(0,16)
}
$PROMPT_SHA = Get-Sha16 $PROMPT_TEXT

function Ask {
    param([int]$MaxTokens, [int]$TargetPid, [string]$LogPath, [object]$Identity)

    $logOffset = Get-LogLength $LogPath
    if ($logOffset -lt 0) { $logOffset = 0 }   # length unreadable: parse the whole file rather than seek to -1
    $before    = Get-ProcCounter $TargetPid
    $diskBefore= Get-DiskCounter
    $sampler   = Start-Sampler $TargetPid $SampleMs

    $body = @{ model='x'; messages=@(@{role='user'; content=$PROMPT_TEXT})
               max_tokens=$MaxTokens; temperature=0; stream=$false } | ConvertTo-Json -Depth 5
    $t0 = Get-Date
    $r = $null
    try { $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 900 }
    catch { }
    $t1 = Get-Date

    $samples   = Stop-Sampler $sampler
    $after     = Get-ProcCounter $TargetPid
    $diskAfter = Get-DiskCounter
    $idNow     = Get-ProcIdentity $TargetPid

    if ($null -eq $r) { return [pscustomobject]@{ ok = $false; why = 'request failed' } }

    $tm = $r.timings
    $content = [string]$r.choices[0].message.content
    $reason  = [string]$r.choices[0].message.reasoning_content
    # THE EFFECTIVE ANSWER. content when it carries anything, otherwise reasoning_content. Both
    # empty is refused and never hashed: SHA-256 of "" is e3b0c442..., so two empty answers agree
    # perfectly and "unchanged across the bases" would be true for the wrong reason.
    $answerSrc = ''
    $answer    = ''
    if ($content.Length -gt 0)     { $answerSrc = 'content';           $answer = $content }
    elseif ($reason.Length -gt 0)  { $answerSrc = 'reasoning_content'; $answer = $reason }
    if ($answerSrc -eq '') {
        return [pscustomobject]@{ ok = $false; why = 'content and reasoning_content are both empty: no comparable answer' }
    }
    $sha16 = Get-Sha16 $answer

    $tail = Read-LogTail $LogPath $logOffset
    $draft = Parse-DraftLog $tail.text

    $pd = Get-CounterDelta $before $after
    $dd = Get-CounterDelta $diskBefore $diskAfter
    $tel = Get-TelemetryStats $samples $t0 $t1 $MinSamples $MaxGapMs

    $completion = [int]$r.usage.completion_tokens
    $bytesPerTok = $null
    if ($pd.ok -and $completion -gt 0) { $bytesPerTok = [math]::Round([double]$pd.delta / [double]$completion, 1) }
    $diskPerTok = $null
    if ($dd.ok -and $completion -gt 0) { $diskPerTok = [math]::Round([double]$dd.delta / [double]$completion, 1) }
    $ratio = $null
    if ($pd.ok -and $dd.ok -and [double]$dd.delta -gt 0) { $ratio = [math]::Round([double]$pd.delta / [double]$dd.delta, 4) }

    # API counters are per request; log counters are per request only because the tail is
    # read from the offset taken above. Both are kept, and they must agree.
    $apiDrafted = $(if ($null -ne $tm.draft_n) { [int]$tm.draft_n } else { 0 })
    $apiAcc     = $(if ($null -ne $tm.draft_n_accepted) { [int]$tm.draft_n_accepted } else { 0 })
    $apiRate    = $null
    if ($null -ne $tm.draft_n -and [int]$tm.draft_n -gt 0) { $apiRate = [math]::Round([double]$tm.draft_n_accepted / [double]$tm.draft_n, 5) }

    return [pscustomobject]@{
        ok            = $true
        why           = ''
        started       = $t0.ToString('o')
        ended         = $t1.ToString('o')
        wall_s        = [math]::Round(($t1 - $t0).TotalSeconds, 3)
        prompt_ms     = [double]$tm.prompt_ms
        prompt_n      = [int]$tm.prompt_n
        prefill_ms_tok= $(if ([int]$tm.prompt_n -gt 0) { [math]::Round([double]$tm.prompt_ms / [int]$tm.prompt_n, 4) } else { $null })
        predicted_ms  = [double]$tm.predicted_ms
        predicted_n   = [int]$tm.predicted_n
        decode_ms_tok = $(if ([int]$tm.predicted_n -gt 0) { [math]::Round([double]$tm.predicted_ms / [int]$tm.predicted_n, 4) } else { $null })
        completion    = $completion
        finish        = [string]$r.choices[0].finish_reason
        answer_sha    = $sha16
        answer_source = $answerSrc
        prompt_sha    = $PROMPT_SHA
        prompt_text   = $PROMPT_TEXT
        answer_chars  = $content.Length
        reasoning_chars = $reason.Length

        drafted       = $apiDrafted
        accepted      = $apiAcc
        accept_rate   = $apiRate
        log_drafted   = $draft.drafted
        log_accepted  = $draft.accepted
        log_rate      = $draft.accept_rate
        mean_accepted_len = $draft.mean_len
        block_size    = $draft.block_size
        acceptance_lines = $draft.acceptance_lines
        verify_steps  = $draft.steps
        ids_sizes     = $draft.ids_sizes
        n_drafts      = $draft.n_drafts
        log_note      = $draft.note
        log_read_ok   = $tail.ok
        log_read_why  = $tail.why
        log_offset    = $logOffset
        log_end       = $tail.end

        proc_read_before = $(if ($before) { $before.read_bytes } else { $null })
        proc_read_after  = $(if ($after)  { $after.read_bytes }  else { $null })
        proc_read_delta  = $pd.delta
        proc_read_ok     = $pd.ok
        proc_read_why    = $pd.why
        proc_bytes_per_token = $bytesPerTok
        disk_read_before = $(if ($diskBefore) { $diskBefore.read_bytes } else { $null })
        disk_read_after  = $(if ($diskAfter)  { $diskAfter.read_bytes }  else { $null })
        disk_read_delta  = $dd.delta
        disk_read_ok     = $dd.ok
        disk_read_why    = $dd.why
        disk_bytes_per_token = $diskPerTok
        proc_share_of_disk   = $ratio

        sample_ms     = $SampleMs
        samples       = @($samples).Count
        telemetry     = $tel
        identity_after= $idNow
        identity_stable = (Test-SameProcess $Identity $idNow)
    }
}

# =========================================================================== run
$parsed = Parse-Blocks $Blocks
if (-not $parsed.ok) { Die ("-Blocks: " + $parsed.why) }
$seq = $parsed.seq
$planA = $parsed.nA
$planB = $parsed.nB

# The mode is written into every record. The two meanings of A/B - a flag on one build, or two
# builds - produce artefacts that look alike and answer different questions; a reader six months
# from now has only this field to tell them apart.
$AbMode = $(if ($TwoBinary) { 'two-binary' } elseif ($ArmFlags) { 'arm-flags' } else { 'md-toggle' })
$treeA  = Get-TreeIdentity $full
$treeB  = $(if ($TwoBinary) { Get-TreeIdentity $fullB } else { $treeA })
Say ("mode {0}" -f $AbMode)
Say ("tree A {0}  commit {1}  tag {2}" -f $treeA.root, $treeA.commit, $treeA.tag)
if ($TwoBinary) { Say ("tree B {0}  commit {1}  tag {2}" -f $treeB.root, $treeB.commit, $treeB.tag) }


$baseArgs = @('-m', $Model, '--host','127.0.0.1','--port',"$Port",'-c',"$Ctx",'-ngl',"$Ngl",'-np','1',
              '-lv', "$Verbosity")
# In arm-flags mode the MoE placement IS the variable, so it must not sit in the shared base.
# Everything above stays shared, and each side appends exactly what it was given.
if (-not $ArmFlags) {
    $baseArgs += @('--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8','--moe-stream-direct')
}
$baseArgs += @('--spec-type', $SpecType)

Say ('=' * 78)
Say ("A/B CAUSE PROBE   sequence {0}   tokens {1}   ONE discarded warm-up per block" -f
     (@($seq | ForEach-Object { "$($_.side)x$($_.n)" }) -join ' '), $Tokens)
Say ("planned evaluated requests: A {0}   B {1}   blocks {2}" -f $planA, $planB, $seq.Count)
Say ("exe A {0}" -f $full)
Say ("sha exe A {0}" -f $shaExe)
Say ("sha impl-dll A {0}" -f $shaDll)
if ($TwoBinary) {
    Say ("exe B {0}" -f $fullB)
    Say ("sha exe B {0}" -f $shaExeB)
    Say ("sha impl-dll B {0}" -f $shaDllB)
    Say  "A and B are two BUILDS at the same operating point; -md is off on both sides"
} else {
    Say ("A = with -md {0}   B = same build without -md   (only variable)" -f $Drafter)
}
Say ("sampling every {0} ms, at least {1} samples per request, largest gap {2} ms" -f $SampleMs, $MinSamples, $MaxGapMs)
Say ('=' * 78)

$results = @(); $aborts = @(); $blockNo = 0; $prevForeign = @()
foreach ($blk in $seq) {
    $side = $blk.side
    $nReq = $blk.n
    $blockNo++
    $foreign = Get-ForeignProcs
    $fdiff = Diff-Foreign $prevForeign $foreign
    $prevForeign = $foreign
    Say ("block {0}  side {1}   foreign[{2}]  added[{3}]  gone[{4}]" -f $blockNo, $side,
         ((@($foreign | ForEach-Object { "$($_.name):$($_.pid_)" }) -join ',')), ($fdiff.added -join ','), ($fdiff.removed -join ','))

    # In two-binary mode the executable IS the variable and neither side gets a drafter.
    $sideExe = $(if ($TwoBinary -and $side -eq 'B') { $fullB } else { $full })
    $sideShaExe = $(if ($TwoBinary -and $side -eq 'B') { $shaExeB } else { $shaExe })
    $sideShaDll = $(if ($TwoBinary -and $side -eq 'B') { $shaDllB } else { $shaDll })
    $srvArgs = Get-SideArgs -Base $baseArgs -Side $side -Mode $AbMode `
                            -FlagsA $ArmFlagsA -FlagsB $ArmFlagsB -DrafterPath $Drafter
    $logBase = Join-Path $OutRoot ("block{0}-{1}" -f $blockNo, $side)
    $errLog  = "$logBase.err"
    $p = Start-Process -FilePath $sideExe -ArgumentList $srvArgs -WorkingDirectory $Lab `
            -RedirectStandardOutput "$logBase.out" -RedirectStandardError $errLog -PassThru -WindowStyle Hidden
    # Start-Process -PassThru releases the handle when the process ends and .ExitCode then
    # reads EMPTY - no exception, nothing. This flag keeps the handle alive.
    try { $p.EnableRaisingEvents = $true } catch { }
    $startedAt = (Get-Date).ToString('o')

    $healthy = $false
    for ($i = 0; $i -lt ($HealthTimeoutSec*2); $i++) {
        $p.Refresh(); if ($p.HasExited) { break }
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3; if ($h.status -eq 'ok') { $healthy = $true; break } } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $healthy) {
        $aborts += "block $blockNo ($side): server never became ready"
        [void](Stop-OurServer $p); continue
    }

    $ident = Get-ProcIdentity $p.Id
    if ($null -eq $ident) {
        $aborts += "block $blockNo ($side): no Win32_Process row for pid $($p.Id)"
        [void](Stop-OurServer $p); continue
    }
    if ($ident.exe -and ($ident.exe -ne $sideExe)) {
        $aborts += "block $blockNo ($side): running exe '$($ident.exe)' is not the requested '$sideExe'"
        [void](Stop-OurServer $p); continue
    }
    $others = @(Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $p.Id })
    if ($others.Count -gt 0) {
        $aborts += ("block $blockNo ($side): a second llama-server is running (pid " + (($others | ForEach-Object { $_.Id }) -join ',') + ")")
        [void](Stop-OurServer $p); continue
    }
    Say ("  pid {0}  created {1}" -f $ident.pid_, $ident.created)
    Say ("  cmd {0}" -f $ident.commandline)

    $warm = Ask -MaxTokens $Tokens -TargetPid $p.Id -LogPath $errLog -Identity $ident
    if (-not $warm.ok) {
        $aborts += "block $blockNo ($side): warm-up request failed"
        [void](Stop-OurServer $p); continue
    }
    Say ("  warm-up (discarded)  decode {0} ms/tok   prefill {1} ms / {2} tok   drafted {3}" -f (N $warm.decode_ms_tok 2), (N $warm.prompt_ms 1), $warm.prompt_n, $warm.drafted)

    # N evaluated requests out of THIS server process. The discarded warm-up above is not
    # removed from anything - its counters stay inside every later block, which is why each
    # request records its own log offset and the per-request figure is a difference.
    $blockRuns = @(); $blockFailed = $false
    for ($rq = 1; $rq -le $nReq; $rq++) {
        $one = Ask -MaxTokens $Tokens -TargetPid $p.Id -LogPath $errLog -Identity $ident
        if (-not $one.ok) {
            $aborts += "block $blockNo ($side): evaluated request $rq of $nReq failed"
            $blockFailed = $true; break
        }
        $one | Add-Member -NotePropertyName req_in_block -NotePropertyValue $rq -Force
        $blockRuns += $one
        Say ("  request {0}/{1}  decode {2} ms/tok   prefill {3} ms / {4} tok" -f
             $rq, $nReq, (N $one.decode_ms_tok 2), (N $one.prompt_ms 1), $one.prompt_n)
    }

    $stop = Stop-OurServer $p
    if ($blockFailed) { continue }

    foreach ($m in $blockRuns) {
    $m | Add-Member -NotePropertyName side       -NotePropertyValue $side -Force
    $m | Add-Member -NotePropertyName block      -NotePropertyValue $blockNo -Force
    $m | Add-Member -NotePropertyName server_pid -NotePropertyValue $ident.pid_ -Force
    $m | Add-Member -NotePropertyName server_created -NotePropertyValue $ident.created -Force
    $m | Add-Member -NotePropertyName server_exe  -NotePropertyValue $sideExe -Force
    $m | Add-Member -NotePropertyName server_cmd  -NotePropertyValue $ident.commandline -Force
    $m | Add-Member -NotePropertyName sha_exe     -NotePropertyValue $sideShaExe -Force
    $m | Add-Member -NotePropertyName sha_impl_dll -NotePropertyValue $sideShaDll -Force
    $m | Add-Member -NotePropertyName ab_mode      -NotePropertyValue $AbMode -Force
    $m | Add-Member -NotePropertyName base_tag     -NotePropertyValue $(if ($side -eq 'B') { $treeB.tag } else { $treeA.tag }) -Force
    $m | Add-Member -NotePropertyName base_commit  -NotePropertyValue $(if ($side -eq 'B') { $treeB.commit } else { $treeA.commit }) -Force
    $m | Add-Member -NotePropertyName tree_root    -NotePropertyValue $(if ($side -eq 'B') { $treeB.root } else { $treeA.root }) -Force
    $m | Add-Member -NotePropertyName server_started -NotePropertyValue $startedAt -Force
    $m | Add-Member -NotePropertyName server_stop  -NotePropertyValue $stop -Force
    $m | Add-Member -NotePropertyName foreign      -NotePropertyValue $foreign -Force
    $m | Add-Member -NotePropertyName foreign_added -NotePropertyValue $fdiff.added -Force
    $m | Add-Member -NotePropertyName warmup_decode_ms_tok -NotePropertyValue $warm.decode_ms_tok -Force
    $results += $m

    Say ("  MEASURED  decode {0} ms/tok   prefill {1} ms / {2} tok   completion {3}   sha {4}" -f (N $m.decode_ms_tok 2), (N $m.prompt_ms 1), $m.prompt_n, $m.completion, $m.answer_sha)
    Say ("            drafted {0}  accepted {1}  rate {2}  mean len {3}  steps {4}  block_size {5}" -f `
         $m.drafted, $m.accepted, $(if ($null -eq $m.accept_rate) { 'null' } else { N $m.accept_rate 5 }), `
         $(if ($null -eq $m.mean_accepted_len) { 'null' } else { N $m.mean_accepted_len 2 }), $m.verify_steps, $(if ($null -eq $m.block_size) { '-' } else { $m.block_size }))
    Say ("            proc read delta {0} B ({1} B/token)   disk delta {2} B   proc share {3}" -f `
         $(if ($m.proc_read_ok) { $m.proc_read_delta } else { 'REJECTED: ' + $m.proc_read_why }), (N $m.proc_bytes_per_token 0), `
         $(if ($m.disk_read_ok) { $m.disk_read_delta } else { 'REJECTED' }), (N $m.proc_share_of_disk 3))
    if ($m.telemetry.ok) {
        Say ("            gpu util med {0} %   mem-ctl med {1} %   vram med {2} MiB   sm {3} MHz   power {4} W   temp {5} C   samples {6}   max gap {7} ms" -f `
             (N $m.telemetry.gpu_util.med 1), (N $m.telemetry.mem_util.med 1), (N $m.telemetry.vram_mib.med 0), `
             (N $m.telemetry.sm_mhz.med 0), (N $m.telemetry.power_w.med 1), (N $m.telemetry.temp_c.med 0), $m.telemetry.n, (N $m.telemetry.max_gap_ms 0))
    } else {
        Say ("            TELEMETRY REFUSED: {0}" -f $m.telemetry.why)
    }

    # abort criteria, checked per request rather than at the end
    if (-not $TwoBinary) {
        if ($side -eq 'A' -and $m.drafted -le 0)  { $aborts += "block $blockNo (A): drafted 0 - the drafter side did not draft" }
        if ($side -eq 'B' -and $m.drafted -ne 0)  { $aborts += "block $blockNo (B): drafted $($m.drafted) - the control side drafted" }
        if ($side -eq 'A' -and $null -eq $m.mean_accepted_len) { $aborts += "block $blockNo (A): no mean accepted len parsed while the side drafted" }
        if ($side -eq 'B' -and $null -ne $m.log_rate)          { $aborts += "block $blockNo (B): acceptance line present on the control side" }
    } else {
        # two builds, -md off on both: any drafting at all means the operating point is not what
        # the comparison assumes, and that is a stop, not a footnote
        if ($m.drafted -ne 0) { $aborts += "block $blockNo ($side): drafted $($m.drafted) while -md is off on both sides" }
    }
    if ($m.finish -ne 'stop')                 { $aborts += "block $blockNo ($side): finish_reason $($m.finish)" }
    if (-not $m.identity_stable)              { $aborts += "block $blockNo ($side): server identity changed during the request" }
    if (-not $m.log_read_ok)                  { $aborts += "block $blockNo ($side): log unreadable - $($m.log_read_why)" }
    if (-not $m.proc_read_ok)                 { $aborts += "block $blockNo ($side): process read counter unusable - $($m.proc_read_why)" }
    if (-not $m.telemetry.ok)                 { $aborts += "block $blockNo ($side): telemetry unusable - $($m.telemetry.why)" }
    if ($m.drafted -gt 0 -and $null -ne $m.log_drafted -and $m.log_drafted -ne $m.drafted) {
        $aborts += "block $blockNo ($side): api drafted $($m.drafted) against log drafted $($m.log_drafted)"
    }
    }

    # per-BLOCK checks: one server, one log, one port - counting them per request would report
    # the same fact N times and make a single fault look like N faults
    if (-not $stop.port_free) { $aborts += "block $blockNo ($side): port $Port still held after stop" }
    $err = @([IO.File]::ReadAllLines($errLog) | Where-Object { $_ -match 'CUDA error|out of memory|cudaMalloc failed|failed to load' }).Count
    if ($err -gt 0) { $aborts += "block $blockNo ($side): $err GPU/load error lines" }
}

# =========================================================================== report
$results | ConvertTo-Json -Depth 8 | Out-File (Join-Path $OutRoot 'probe-runs.json') -Encoding ascii

$A = @($results | Where-Object { $_.side -eq 'A' })
$B = @($results | Where-Object { $_.side -eq 'B' })

function Line([string]$label, [object[]]$vals, [int]$dp) {
    return ("{0,-30} {1}" -f $label, ((@($vals | ForEach-Object { N $_ $dp })) -join ' , '))
}

Write-Output ''
Say ('=' * 78)
Say ("MECHANISM MATRIX   A n={0}   B n={1}" -f $A.Count, $B.Count)
Say ('=' * 78)
Write-Output ''
Write-Output '-- function (must be identical, or the sides are not comparable)'
Write-Output ("{0,-30} {1}" -f 'A answer_sha', ((@($A | ForEach-Object { $_.answer_sha })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'B answer_sha', ((@($B | ForEach-Object { $_.answer_sha })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'A completion tokens', ((@($A | ForEach-Object { $_.completion })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'B completion tokens', ((@($B | ForEach-Object { $_.completion })) -join ' , '))
Write-Output ''
Write-Output '-- speculation (A only; null on B means the draft path was not active)'
Write-Output ("{0,-30} {1}" -f 'A drafted/accepted', ((@($A | ForEach-Object { "$($_.accepted)/$($_.drafted)" })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'A accept_rate', ((@($A | ForEach-Object { if ($null -eq $_.accept_rate) { 'null' } else { N $_.accept_rate 5 } })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'A mean accepted len', ((@($A | ForEach-Object { if ($null -eq $_.mean_accepted_len) { 'null' } else { N $_.mean_accepted_len 2 } })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'A verification steps', ((@($A | ForEach-Object { $_.verify_steps })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'A block_size', ((@($A | ForEach-Object { $_.block_size })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'B accept_rate', ((@($B | ForEach-Object { if ($null -eq $_.accept_rate) { 'null' } else { N $_.accept_rate 5 } })) -join ' , '))
Write-Output ("{0,-30} {1}" -f 'B verification steps', ((@($B | ForEach-Object { $_.verify_steps })) -join ' , '))
Write-Output ''
Write-Output '-- decode (primary)'
Write-Output (Line 'A decode ms/token' @($A | ForEach-Object { $_.decode_ms_tok }) 2)
Write-Output (Line 'B decode ms/token' @($B | ForEach-Object { $_.decode_ms_tok }) 2)
$ma = Med (@($A | ForEach-Object { $_.decode_ms_tok })); $mb = Med (@($B | ForEach-Object { $_.decode_ms_tok }))
Write-Output ("{0,-30} A {1}   B {2}" -f 'decode median ms/token', (N $ma 3), (N $mb 3))
if ($null -ne $ma -and $null -ne $mb -and $mb -ne 0) {
    Write-Output ("{0,-30} {1} %  (positive = A slower)" -f 'decode A vs B', (N (100.0*($ma-$mb)/$mb) 2))
}
Write-Output ''
Write-Output '-- process read-byte delta under direct I/O (NOT "expert bytes from SSD")'
Write-Output (Line 'A proc read delta B' @($A | ForEach-Object { $_.proc_read_delta }) 0)
Write-Output (Line 'B proc read delta B' @($B | ForEach-Object { $_.proc_read_delta }) 0)
Write-Output (Line 'A proc B per token' @($A | ForEach-Object { $_.proc_bytes_per_token }) 0)
Write-Output (Line 'B proc B per token' @($B | ForEach-Object { $_.proc_bytes_per_token }) 0)
Write-Output (Line 'A disk _Total delta B' @($A | ForEach-Object { $_.disk_read_delta }) 0)
Write-Output (Line 'B disk _Total delta B' @($B | ForEach-Object { $_.disk_read_delta }) 0)
Write-Output (Line 'A proc share of disk' @($A | ForEach-Object { $_.proc_share_of_disk }) 3)
Write-Output (Line 'B proc share of disk' @($B | ForEach-Object { $_.proc_share_of_disk }) 3)
Write-Output ''
Write-Output '-- gpu under load (medians per request)'
Write-Output (Line 'A gpu util %' @($A | ForEach-Object { $_.telemetry.gpu_util.med }) 1)
Write-Output (Line 'B gpu util %' @($B | ForEach-Object { $_.telemetry.gpu_util.med }) 1)
Write-Output (Line 'A mem-ctl util %' @($A | ForEach-Object { $_.telemetry.mem_util.med }) 1)
Write-Output (Line 'B mem-ctl util %' @($B | ForEach-Object { $_.telemetry.mem_util.med }) 1)
Write-Output (Line 'A vram MiB' @($A | ForEach-Object { $_.telemetry.vram_mib.med }) 0)
Write-Output (Line 'B vram MiB' @($B | ForEach-Object { $_.telemetry.vram_mib.med }) 0)
Write-Output (Line 'A sm MHz' @($A | ForEach-Object { $_.telemetry.sm_mhz.med }) 0)
Write-Output (Line 'B sm MHz' @($B | ForEach-Object { $_.telemetry.sm_mhz.med }) 0)
Write-Output (Line 'A power W' @($A | ForEach-Object { $_.telemetry.power_w.med }) 1)
Write-Output (Line 'B power W' @($B | ForEach-Object { $_.telemetry.power_w.med }) 1)
Write-Output (Line 'A temp C' @($A | ForEach-Object { $_.telemetry.temp_c.med }) 0)
Write-Output (Line 'B temp C' @($B | ForEach-Object { $_.telemetry.temp_c.med }) 0)
Write-Output (Line 'A samples' @($A | ForEach-Object { $_.telemetry.n }) 0)
Write-Output (Line 'B samples' @($B | ForEach-Object { $_.telemetry.n }) 0)
Write-Output (Line 'A max gap ms' @($A | ForEach-Object { $_.telemetry.max_gap_ms }) 0)
Write-Output (Line 'B max gap ms' @($B | ForEach-Object { $_.telemetry.max_gap_ms }) 0)
Write-Output ("{0,-30} A {1}   B {2}" -f 'throttle samples nonzero', (@($A | ForEach-Object { $_.telemetry.throttle_nonzero }) -join ','), (@($B | ForEach-Object { $_.telemetry.throttle_nonzero }) -join ','))
Write-Output ''
Write-Output '-- secondary (never merged into the judgement)'
Write-Output (Line 'A prefill ms' @($A | ForEach-Object { $_.prompt_ms }) 1)
Write-Output (Line 'B prefill ms' @($B | ForEach-Object { $_.prompt_ms }) 1)
Write-Output (Line 'A wall s' @($A | ForEach-Object { $_.wall_s }) 2)
Write-Output (Line 'B wall s' @($B | ForEach-Object { $_.wall_s }) 2)
Write-Output ''
Write-Output '-- what this probe cannot say, by construction'
Write-Output '   target-model misses against drafter misses; cache hits and evictions per model;'
Write-Output '   the union of experts inside one draft block; expert bytes per verification step;'
Write-Output '   stall time split by model. Those counters exist in llama_moe_stream::print_stats()'
Write-Output '   and llama-server has no caller for them.'
Write-Output ("   raw: {0}" -f $OutRoot)

Write-Output ''
if ($aborts.Count -eq 0) {
    Write-Output ("RESULT: PASS - probe complete, {0} evaluable request(s) per side in a fixed alternating order." -f $A.Count)
    exit 0
} else {
    Write-Output ("RESULT: FAIL - {0} abort criteria fired." -f $aborts.Count)
    foreach ($a in $aborts) { Write-Output "  ABORT  $a" }
    exit 1
}
