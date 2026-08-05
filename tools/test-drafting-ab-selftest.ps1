<#
test-drafting-ab-selftest - can the instrument self-test of measure-drafting-ab.ps1 go RED?

WHY THIS EXISTS, and it is a measured failure of exactly this kind. The previous version of
measure-drafting-ab.ps1 CALLED its median self-test at line 173 and DEFINED it at line 190.
A function does not exist before its definition is reached, the error was non-terminating,
$medBad stayed $null, and $null.Count is 0 - so the tool printed

    median self-test: 4 of 4 cases green

without ever running a case. Twenty-five green cases prove nothing on their own; what proves
something is that a broken build makes them red, and that the RIGHT ones go red.

WHAT IT DOES. It copies the tool, injects five defects that are each a real historical or
plausible failure, runs -Selftest against the copy, and asserts that the expected cases turn
red and the unrelated ones stay green. The original is never modified; the copy lives in TEMP.

THE FIVE DEFECTS, and what each stands for:
  1 median returns the UPPER middle value      - the defect measured on 2026-08-05, which
                                                 reported 95.03 and 92.54 as "medians"
  2 a backwards counter is accepted             - a cumulative counter that resets would
                                                 otherwise yield a delta that looks like data
  3 an active-but-zero rate collapses to null   - erases the distinction between "drafted and
                                                 nothing accepted" and "draft path not active"
  4 strict decoding instead of replacement      - byte 0xef at offset 7339 made strict utf-8
                                                 AND utf-16 fail and returned [], the same
                                                 value as "never drafted"
  5 the minimum-sample gate is disabled         - a median over two samples is not a median

ONE CASE IS DELIBERATELY NOT ASSERTED AS RED: 'telemetry refuses too few samples'. With defect 5
the two-sample case still fails, but on the GAP check rather than the count check, so it stays
green with a different reason. That is honest behaviour of a checker with two independent gates,
and pretending otherwise would make this control lie about its own reach.

Exit 0 = the self-test can go red, and the right cases do.  1 = it cannot.  2 = setup error.
#>
param(
    [string]$Tool = 'C:\Users\robin\dev\Crow\tools\measure-drafting-ab.ps1',
    [string]$WorkDir = ''
)

$ErrorActionPreference = 'Continue'
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }

if (-not (Test-Path -LiteralPath $Tool)) { Die "tool not found: $Tool" }
if (-not $WorkDir) { $WorkDir = Join-Path $env:TEMP ("mdab-negctl-{0}" -f $PID) }
if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null }

# --- the healthy run first: without it, "red" could just mean "broken everywhere" ----------
Say 'baseline: the unmodified tool must be GREEN'
$outOk = & $Tool -Selftest 2>&1
$rcOk  = $LASTEXITCODE
$greenOk = @($outOk | Where-Object { $_ -match '\s+ok\s+' }).Count
$redOk   = @($outOk | Where-Object { $_ -match '\s+RED\s+' }).Count
Say ("  baseline exit {0}, {1} ok, {2} red" -f $rcOk, $greenOk, $redOk)

# --- the injected defects -------------------------------------------------------------------
$mutations = @(
    @{ name = 'median returns the upper middle'
       from = '    return ([double]$s[$n/2 - 1] + [double]$s[$n/2]) / 2.0'
       to   = '    return [double]$s[$n/2]' }
    @{ name = 'backwards counter accepted'
       from = '    if ([uint64]$After.read_bytes -lt [uint64]$Before.read_bytes) {'
       to   = '    if ($false) {' }
    @{ name = 'active-but-zero collapses to null'
       from = '        if ($out.drafted -gt 0) {'
       to   = '        if ($out.drafted -gt 0 -and $out.accepted -gt 0) {' }
    @{ name = 'strict decoding instead of replacement'
       from = "[Text.Encoding]::GetEncoding('utf-8', [Text.EncoderFallback]::ReplacementFallback, [Text.DecoderFallback]::ReplacementFallback)"
       to   = "[Text.Encoding]::GetEncoding('utf-8', [Text.EncoderFallback]::ExceptionFallback, [Text.DecoderFallback]::ExceptionFallback)" }
    @{ name = 'minimum-sample gate disabled'
       from = '    if ($n -lt $MinN) {'
       to   = '    if ($false) {' }
)

# Cases that MUST turn red once the defects are in.
$wantRed = @(
    'counter reset rejected'
    'tolerant decode survives 0xef'
    'active-but-zero stays numeric 0.0'
    'median even 1,2,3,4 = 2.5'
    'median of the E11 A-side = 94.235'
    'telemetry accepts a covered request'
)
# Cases that must stay green: the defects are targeted, not a demolition.
$wantGreen = @(
    'server path accepted'
    'wrong server path rejected'
    'production build refused'
    'pid reuse detected'
    'monotone counter gives delta'
    'request-local parse excludes the warm-up'
    'per-step lines reconstruct mean len 3.55'
    'median odd 1,2,3 = 2'
)

$text = [IO.File]::ReadAllText($Tool)
$applied = @()
foreach ($m in $mutations) {
    if (-not $text.Contains($m.from)) {
        Die ("anchor for '{0}' not found in the tool - this control is stale and must be repaired, not skipped" -f $m.name)
    }
    $text = $text.Replace($m.from, $m.to)
    $applied += $m.name
}
$copy = Join-Path $WorkDir 'broken-measure-drafting-ab.ps1'
[IO.File]::WriteAllText($copy, $text)
Say ("injected {0} defects into a copy at {1}" -f $applied.Count, $copy)
foreach ($a in $applied) { Say ("  defect: {0}" -f $a) }

# --- run the broken copy ---------------------------------------------------------------------
Say 'broken copy: the self-test must be RED'
$out = & $copy -Selftest 2>&1
$rc  = $LASTEXITCODE

$state = @{}
foreach ($line in $out) {
    $s = [string]$line
    $m = [regex]::Match($s, '^\[[0-9:]+\]\s+(ok|RED)\s{2,}(.+?)\s{2,}')
    if (-not $m.Success) { $m = [regex]::Match($s, '^\[[0-9:]+\]\s+(ok|RED)\s{2,}(.+?)\s*$') }
    if ($m.Success) { $state[$m.Groups[2].Value.Trim()] = $m.Groups[1].Value }
}

Write-Output ''
Say ('=' * 78)
Say 'NEGATIVE CONTROL of the instrument self-test'
Say ('=' * 78)
$bad = @()
if ($rcOk -ne 0)   { $bad += "baseline self-test was not green (exit $rcOk)" }
if ($redOk -ne 0)  { $bad += "baseline self-test reported $redOk red cases" }
if ($rc -eq 0)     { $bad += "broken copy still exited 0 - the self-test cannot go red" }

foreach ($c in $wantRed) {
    if (-not $state.ContainsKey($c)) { $bad += "case not found in output: $c"; Say ("  MISSING  {0}" -f $c); continue }
    if ($state[$c] -eq 'RED') { Say ("  ok       red as required   {0}" -f $c) }
    else { $bad += "case stayed green under its own defect: $c"; Say ("  FAIL     stayed green    {0}" -f $c) }
}
foreach ($c in $wantGreen) {
    if (-not $state.ContainsKey($c)) { $bad += "case not found in output: $c"; Say ("  MISSING  {0}" -f $c); continue }
    if ($state[$c] -eq 'ok') { Say ("  ok       green as required {0}" -f $c) }
    else { $bad += "unrelated case went red: $c"; Say ("  FAIL     went red        {0}" -f $c) }
}

$redCount = @($state.Values | Where-Object { $_ -eq 'RED' }).Count
$okCount  = @($state.Values | Where-Object { $_ -eq 'ok' }).Count
Say ("broken copy: exit {0}, {1} ok, {2} red   (baseline: exit {3}, {4} ok, {5} red)" -f $rc, $okCount, $redCount, $rcOk, $greenOk, $redOk)
Say "note: 'telemetry refuses too few samples' is NOT asserted red - under defect 5 it still fails, but on the gap gate, and says so in its detail column."

Remove-Item $WorkDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
if ($bad.Count -eq 0) {
    Write-Output ("RESULT: PASS - the self-test is green when healthy and red when broken, and the {0} targeted cases are the ones that flip." -f $wantRed.Count)
    exit 0
}
Write-Output ("RESULT: FAIL - {0} problem(s)." -f $bad.Count)
foreach ($b in $bad) { Write-Output "  $b" }
exit 1
