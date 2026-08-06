<#
test-moe-stream-wave-plan - does the ONE wave plan compute what it must, and is it still one?

E13 pulled the worst-case wave plan out of build_moe_ffn and graph_max_nodes, where it stood
twice and had already drifted in four ways. Two things therefore have to stay true, and they
fail differently:

  ARITHMETIC  the plan returns the right cap and wave count at the thresholds
  SINGULARITY neither consumer has grown its own copy back

A test for only the first passes happily over a re-duplicated formula. A test for only the
second passes over a formula that is wrong in one place.

THE YARDSTICK COMES OUT OF THE HEADER, NOT OUT OF THIS FILE. The two functions are extracted
verbatim from src/llama-moe-stream.h and compiled here. Retyping them would mean this suite
tests its own copy: it would stay green while the shipped function changed underneath, which
is the exact failure this project has already paid for twice.

WHAT IS DELIBERATELY NOT ASSERTED: any timing. Wave splitting is a prefill path - a
single-token decode has n_touch_max = n_expert_used and never enters the gate - and this suite
makes no statement about how long anything takes.

Exit 0 = arithmetic and singularity both hold, and the negative controls flipped.
       1 = at least one case failed.  2 = setup error.
#>
param(
    [string]$WT   = 'C:\Users\robin\dev\crow-lab\wt-e13',
    [string]$Work = ''
)

$ErrorActionPreference = 'Continue'
function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }

$HEADER = Join-Path $WT 'src\llama-moe-stream.h'
$GRAPH  = Join-Path $WT 'src\llama-graph.cpp'
$CTX    = Join-Path $WT 'src\llama-context.cpp'
foreach ($f in @($HEADER, $GRAPH, $CTX)) { if (-not (Test-Path -LiteralPath $f)) { Die "not found: $f" } }
if (-not $Work) { $Work = Join-Path $env:TEMP ("moe-wave-plan-{0}" -f $PID) }
if (-not (Test-Path $Work)) { New-Item -ItemType Directory -Path $Work -Force | Out-Null }

# --- toolchain, the same bootstrap the build uses: cl.exe is in no PATH by default -----------
if ($null -eq (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) { Die 'vswhere not found, cannot locate the toolchain' }
    $vs = & $vswhere -latest -property installationPath
    $bat = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
    if (-not (Test-Path $bat)) { Die "vcvars64.bat not found under $vs" }
    cmd /c "`"$bat`" >nul 2>&1 && set" | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path ("Env:" + $matches[1]) -Value $matches[2] }
    }
}
if ($null -eq (Get-Command cl.exe -ErrorAction SilentlyContinue)) { Die 'no cl.exe after the vcvars bootstrap' }

# --- extract the two functions verbatim -------------------------------------------------------
$src = [IO.File]::ReadAllText($HEADER)
function Extract([string]$Text, [string]$Signature) {
    $i = $Text.IndexOf($Signature)
    if ($i -lt 0) { return $null }
    $open = $Text.IndexOf('{', $i)
    if ($open -lt 0) { return $null }
    $depth = 0
    for ($j = $open; $j -lt $Text.Length; $j++) {
        if ($Text[$j] -eq '{') { $depth++ }
        elseif ($Text[$j] -eq '}') { $depth--; if ($depth -eq 0) { return $Text.Substring($i, $j - $i + 1) } }
    }
    return $null
}
$fnMin  = Extract $src 'static inline uint32_t llama_moe_stream_min_slots'
$fnPlan = Extract $src 'static inline llama_moe_stream_wave_budget llama_moe_stream_wave_plan'
$stStru = Extract $src 'struct llama_moe_stream_wave_budget'
if (-not $fnMin)  { Die 'llama_moe_stream_min_slots not found in the header - this suite is stale, repair it rather than skip it' }
if (-not $fnPlan) { Die 'llama_moe_stream_wave_plan not found in the header - this suite is stale, repair it rather than skip it' }
if (-not $stStru) { Die 'llama_moe_stream_wave_budget not found in the header' }
Say ("extracted {0} + {1} + {2} chars verbatim from the header" -f $stStru.Length, $fnMin.Length, $fnPlan.Length)

# --- the cases. Expectations are computed here, the ANSWERS come from the compiled header ------
# PSCustomObjects on purpose: PowerShell FLATTENS nested @(...) literals, so an array of arrays
# is silently iterated element by element and every case reads as an empty row. Measured while
# building this suite - the run reported 39 failures over cases that were single characters.
function Case([int]$slots, [int]$expert, [int]$eu, [int]$tokens, [int]$split, [int]$cap, [int]$waves, [string]$label) {
    return [pscustomobject]@{
        n_slots = $slots; n_expert = $expert; n_eu = $eu; n_tokens = $tokens
        want_split = $split; want_cap = $cap; want_waves = $waves; label = $label
    }
}
$CASES = @(
    (Case 64 256 6    1 0  0  1 'operating point, decode: one token never reaches the gate')
    (Case 64 256 6    4 0  0  1 'operating point, cached prefill of 4 tokens')
    (Case 64 256 6   10 0  0  1 'operating point, 10 tokens: 60 touched, still fits 64')
    (Case 64 256 6   11 1 29  3 'operating point, 11 tokens: 66 touched, the gate opens')
    (Case 64 256 6   17 1 29  4 'operating point, cold prefill of 17 tokens')
    (Case 64 256 6   43 1 29  9 'operating point, 43 tokens: n_touch_max saturates at n_expert')
    (Case 64 256 6 2048 1 29  9 'operating point, full batch: still 9, saturation holds')
    (Case 18 256 6    1 0  0  1 'new default, decode')
    (Case 18 256 6    3 0  0  1 'new default, exactly 18 touched: not > 18, so no split')
    (Case 18 256 6    4 1  6  4 'new default, 24 touched: the gate opens at cap 6')
    (Case 18 256 6   43 1  6 43 'new default, saturated: 256/6 rounded up')
    (Case 24 256 6   43 1  9 29 '24 slots, saturated')
    (Case 88 256 6   43 1 41  7 '88 slots, saturated')
)

function Build-And-Run([string]$PlanBody, [string]$Tag) {
    $cpp = @()
    $cpp += '#include <algorithm>'
    $cpp += '#include <cstdint>'
    $cpp += '#include <cstdio>'
    $cpp += '#include <cstdlib>'
    # the real header asserts through ggml; here the assert must be observable, not fatal-silent
    $cpp += 'static int g_assert_hits = 0;'
    $cpp += '#define GGML_ASSERT(x) do { if (!(x)) { g_assert_hits++; } } while (0)'
    $cpp += $stStru
    $cpp += ';'
    $cpp += $fnMin
    $cpp += $PlanBody
    $cpp += @'
int main(int argc, char ** argv) {
    if (argc != 5) { return 2; }
    const uint32_t n_slots = (uint32_t) atoi(argv[1]);
    const uint32_t n_exp   = (uint32_t) atoi(argv[2]);
    const uint32_t n_eu    = (uint32_t) atoi(argv[3]);
    const uint32_t n_tok   = (uint32_t) atoi(argv[4]);
    const llama_moe_stream_wave_budget b = llama_moe_stream_wave_plan(n_slots, n_exp, n_eu, n_tok);
    printf("%d %u %u %u %d\n", b.split ? 1 : 0, b.cap, b.n_waves, llama_moe_stream_min_slots(n_eu), g_assert_hits);
    return 0;
}
'@
    $cppPath = Join-Path $Work ("wave-plan-$Tag.cpp")
    $exePath = Join-Path $Work ("wave-plan-$Tag.exe")
    [IO.File]::WriteAllLines($cppPath, $cpp)
    Push-Location $Work
    & cl.exe /nologo /std:c++17 /EHsc /W4 /Fe:$exePath $cppPath > (Join-Path $Work "cl-$Tag.log") 2>&1
    $rc = $LASTEXITCODE
    Pop-Location
    if ($rc -ne 0) { return @{ ok = $false; why = "cl.exe exit $rc, see cl-$Tag.log"; exe = '' } }
    return @{ ok = $true; why = ''; exe = $exePath }
}

function Run-Cases([string]$Exe) {
    $rows = @()
    foreach ($c in $CASES) {
        $out = & $Exe $c.n_slots $c.n_expert $c.n_eu $c.n_tokens
        $f = ([string]$out).Trim() -split '\s+'
        if ($f.Count -lt 5) { throw "the compiled plan printed '$out' for $($c.label)" }
        $rows += [pscustomobject]@{
            label = $c.label; n_slots = $c.n_slots; n_expert = $c.n_expert; n_eu = $c.n_eu; n_tokens = $c.n_tokens
            want_split = $c.want_split; want_cap = $c.want_cap; want_waves = $c.want_waves
            got_split = [int]$f[0]; got_cap = [int]$f[1]; got_waves = [int]$f[2]
            got_min = [int]$f[3]; asserts = [int]$f[4]
        }
    }
    return $rows
}

# --- 1 arithmetic, against the real function ---------------------------------------------------
Say 'compiling the header functions verbatim'
$b = Build-And-Run $fnPlan 'real'
if (-not $b.ok) { Die $b.why }
$rows = Run-Cases $b.exe

$bad = @()
Write-Output ''
Say ('=' * 96)
Say 'WAVE PLAN, computed by the function that ships'
Say ('=' * 96)
Write-Output ("{0,7} {1,8} {2,5} {3,8}  {4,-13} {5,-13} {6,7}  {7}" -f 'slots','n_expert','n_eu','n_tokens','split w/g','cap w/g','waves','case')
foreach ($r in $rows) {
    $okSplit = $r.got_split -eq $r.want_split
    $okCap   = $r.got_cap   -eq $r.want_cap
    $okWaves = $r.got_waves -eq $r.want_waves
    if (-not ($okSplit -and $okCap -and $okWaves)) {
        $bad += ("{0}: want split={1} cap={2} waves={3}, got split={4} cap={5} waves={6}" -f $r.label, $r.want_split, $r.want_cap, $r.want_waves, $r.got_split, $r.got_cap, $r.got_waves)
    }
    Write-Output ("{0,7} {1,8} {2,5} {3,8}  {4,-13} {5,-13} {6,3}/{7,-3}  {8}" -f `
        $r.n_slots, $r.n_expert, $r.n_eu, $r.n_tokens, ("$($r.want_split)/$($r.got_split)"), ("$($r.want_cap)/$($r.got_cap)"), $r.want_waves, $r.got_waves, $r.label)
}
$minOk = @($rows | Where-Object { $_.got_min -ne 3*$_.n_eu }).Count -eq 0
if (-not $minOk) { $bad += 'llama_moe_stream_min_slots does not return 3*n_expert_used' }
$assertHits = @($rows | Where-Object { $_.asserts -gt 0 }).Count
if ($assertHits -gt 0) { $bad += "$assertHits case(s) tripped the precondition assert - a valid capacity must not" }
Say ("min_slots = 3*n_expert_used in every case: {0}   precondition asserts tripped: {1}" -f $minOk, $assertHits)

# --- 2 singularity: has a consumer grown its own copy back? ------------------------------------
Write-Output ''
Say ('=' * 96)
Say 'SINGULARITY of the plan'
Say ('=' * 96)
$consumers = @(@{ name = 'src/llama-graph.cpp'; path = $GRAPH }, @{ name = 'src/llama-context.cpp'; path = $CTX })
$FORBIDDEN = @(
    @{ pat = '- *\(uint32_t\) *n_expert_used\) */ *2|- *n_eu\) */ *2';        why = 'a second copy of the cap formula' }
    @{ pat = 'max<uint32_t>\( *cap *,';                                       why = 'clamping cap instead of refusing an impossible capacity' }
    @{ pat = 'min<uint32_t>\( *[A-Za-z_.]*n_expert *,';                       why = 'n_touch_max computed in 32 bit' }
    @{ pat = 'n_waves *\* *\(uint32_t\) *mstream->layers\.size\(\)|24u\*n_waves\*\(uint32_t\) *mstream->layers\.size\(\)'; why = 'budgeting every layer instead of the streamed ones' }
)
foreach ($c in $consumers) {
    $text = [IO.File]::ReadAllText($c.path)
    foreach ($f in $FORBIDDEN) {
        if ([regex]::IsMatch($text, $f.pat)) {
            $bad += ("{0}: {1}" -f $c.name, $f.why)
            Say ("  RED  {0,-24} {1}" -f $c.name, $f.why)
        }
    }
    $calls = ([regex]::Matches($text, 'llama_moe_stream_wave_plan\s*\(')).Count
    Say ("  ok   {0,-24} calls the shared plan {1}x, carries no copy" -f $c.name, $calls)
    if ($calls -lt 1) { $bad += "$($c.name): does not call llama_moe_stream_wave_plan at all" }
}
$hdrDefs = ([regex]::Matches($src, 'llama_moe_stream_wave_plan\s*\(\s*$|static inline llama_moe_stream_wave_budget llama_moe_stream_wave_plan')).Count
Say ("  ok   header defines the plan {0}x" -f $hdrDefs)
if ($hdrDefs -ne 1) { $bad += "the header defines the plan $hdrDefs times, want exactly 1" }

# --- 3 negative control: a reintroduced defect must turn the arithmetic red --------------------
Write-Output ''
Say ('=' * 96)
Say 'NEGATIVE CONTROL - the same suite against a reintroduced defect'
Say ('=' * 96)
$mutated = $fnPlan.Replace('if (n_touch_max <= (uint64_t) n_slots) {', 'if (false) {')
if ($mutated -eq $fnPlan) { Die 'could not inject the missing-gate defect - the control is stale' }
$b2 = Build-And-Run $mutated 'nogate'
if (-not $b2.ok) { Die $b2.why }
$rows2 = Run-Cases $b2.exe
$flipped = 0
foreach ($r in $rows2) {
    if ($r.got_split -ne $r.want_split -or $r.got_cap -ne $r.want_cap -or $r.got_waves -ne $r.want_waves) { $flipped++ }
}
Say ("dropping the gate flips {0} of {1} cases" -f $flipped, $rows2.Count)
if ($flipped -lt 4) { $bad += "the missing-gate defect flipped only $flipped cases - these cases cannot detect it" }

$mut2 = $fnPlan.Replace('b.cap     = (n_slots - n_expert_used)/2;', 'b.cap     = (n_slots - n_expert_used)/2 + 1;')
if ($mut2 -eq $fnPlan) { Die 'could not inject the cap-off-by-one defect - the control is stale' }
$b3 = Build-And-Run $mut2 'capoff'
if (-not $b3.ok) { Die $b3.why }
$rows3 = Run-Cases $b3.exe
$flipped2 = 0
foreach ($r in $rows3) { if ($r.got_cap -ne $r.want_cap -or $r.got_waves -ne $r.want_waves) { $flipped2++ } }
Say ("an off-by-one in cap flips {0} of {1} cases" -f $flipped2, $rows3.Count)
if ($flipped2 -lt 4) { $bad += "the cap off-by-one flipped only $flipped2 cases" }

Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
if ($bad.Count -eq 0) {
    Write-Output ("RESULT: PASS - {0} threshold cases from the shipped function, both consumers copy-free, and two reintroduced defects turn it red." -f $rows.Count)
    exit 0
}
Write-Output ("RESULT: FAIL - {0} problem(s)." -f $bad.Count)
foreach ($x in $bad) { Write-Output "  $x" }
exit 1
