<#
Drives the tier-2 question: what does an expert cost as a block copy out of host RAM,
against fetching the same expert off the SSD.

WHY A SCRIPT AND NOT SIX COMMAND LINES
The comparison is only readable if both arms run in one session against a warm machine.
This box drifted 13.2 % over 95 minutes on 2026-08-02, so all of arm A followed by all of
arm B would put that drift entirely into the ratio. The arms therefore ALTERNATE, and
that ordering is the reason this file exists rather than a note telling somebody to run
the binary twice.

WHY TWO BLOCK SIZES AND NOT ONE
The streamer moves one weight tensor per work item, not one expert. In UD-IQ3_XXS those
are 2,686,976 B (ffn_gate_exps, ffn_up_exps) and 3,211,264 B (ffn_down_exps). Every
figure bench-loader published before 2026-08-09 stands on 12.75 MiB blocks, which is the
MXFP4 expert size and not a request this product ever issues.

WHAT IT DOES NOT ANSWER
Whether the time it measures is on the critical path. Eight I/O workers read while the
decode thread computes; this script measures the cost of a transfer, not how much of that
cost the pipeline already hides. Read the ratio as an upper bound on what a tier-2 could
return, not as a throughput prediction.

NOTE: ASCII-only. Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI, and one
non-ASCII character breaks the parse somewhere unrelated.

Usage:
  run-h2d-series.ps1 -Model <shard.gguf> [-OutDir <dir>] [-Reps 3] [-Seconds 4]
  run-h2d-series.ps1 -Selftest
#>
param(
    [string]$Model    = '',
    [string]$OutDir   = '',
    [string]$Exe      = '',
    [int]   $Reps     = 3,
    [double]$Seconds  = 4.0,
    [switch]$Selftest
)

$ErrorActionPreference = 'Continue'
$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Exe -eq '') { $Exe = Join-Path $ToolsDir 'build-bench\bench-loader.exe' }

# gate/up and down. Both divide by 4096; the binary rejects anything that does not.
$Blocks = @(2686976, 3211264)

# Parses one bench-loader run. Kept apart from the running so the selftest can drive it
# with captured text instead of a drive.
function Read-Result {
    param([string]$Text, [int]$ExitCode)
    if ($ExitCode -ne 0) { return $null }
    $vals = @{}
    foreach ($line in ($Text -split "`n")) {
        if ($line -match 'RESULT\s+pageable\s+([\d.]+)\s+MB/s') { $vals['pageable'] = [double]$Matches[1]; continue }
        if ($line -match 'RESULT\s+pinned\s+([\d.]+)\s+MB/s')   { $vals['pinned']   = [double]$Matches[1]; continue }
        if ($line -match 'RESULT\s+([\d.]+)\s+MB/s')            { $vals['ssd']      = [double]$Matches[1] }
    }
    if ($vals.Count -eq 0) { return $null }
    return $vals
}

function Invoke-Arm {
    # NOT $Args. That is an automatic PowerShell variable: a param() of that name is
    # silently not bound, the binary starts with no arguments at all, prints its usage
    # and exits 2. Cost one full series on 2026-08-09. The empty-row guard caught it -
    # every row came back blank rather than carrying a number from a run that never
    # happened - which is the only reason it was noticed at once.
    param([string]$Label, [string[]]$ArgList, [string]$Log)
    $raw  = & $Exe @ArgList 2>&1 | Out-String
    $code = $LASTEXITCODE
    Add-Content $Log "===== $Label  (exit $code)"
    Add-Content $Log $raw
    return (Read-Result -Text $raw -ExitCode $code)
}

# ---------------------------------------------------------------------------------
# Selftest. Drives the parser with captured output, including the shapes that must NOT
# produce a figure. A parser that cannot return null cannot tell a failed run from a
# slow one, and this script's whole job is to put numbers in a table.
# ---------------------------------------------------------------------------------
if ($Selftest) {
    $pass = 0; $fail = 0
    function Check {
        param($Name, $Want, $Got)
        if ($Want -eq $Got) { $script:pass++; "  PASS  $Name" }
        else                { $script:fail++; "  FAIL  $Name  want=$Want got=$Got" }
    }

    $ssdText = @"
arm         DIRECT I/O (measurement)
blocks      3205
read        42848747520 bytes
elapsed     4.007 s

RESULT      10694.6 MB/s   (8 threads, handle per worker, bytes/s/1e6, 42.85 GB)
"@
    $h2dText = @"
arm         H2D (host to device copy)
control 1   pinned is page-locked, pageable is not  PASS
control 2   2686976 bytes survive H2D then D2H unchanged  PASS

RESULT      pageable  17791.4 MB/s   (bytes/s/1e6)
RESULT      pinned    47581.5 MB/s   (bytes/s/1e6)
RESULT      ratio     2.674x  pinned over pageable
"@
    $abortText = @"
arm         H2D (host to device copy)

ABORT: arena is not larger than one block. No figure.
"@

    Write-Output "run-h2d-series selftest"

    $r = Read-Result -Text $ssdText -ExitCode 0
    Check 'ssd arm parsed'            10694.6 $r['ssd']
    Check 'ssd arm has no pinned key' $null   $r['pinned']

    $r = Read-Result -Text $h2dText -ExitCode 0
    Check 'h2d pageable parsed'       17791.4 $r['pageable']
    Check 'h2d pinned parsed'         47581.5 $r['pinned']
    # The h2d output carries three RESULT lines and the generic pattern matches none of
    # the first two. If it ever did, a copy rate would be filed as a drive rate.
    Check 'h2d does not fill ssd key' $null   $r['ssd']

    # The cases that must produce NO figure.
    Check 'nonzero exit yields null'  $null (Read-Result -Text $ssdText   -ExitCode 1)
    Check 'ABORT yields null'         $null (Read-Result -Text $abortText -ExitCode 1)
    Check 'usage text yields null'    $null (Read-Result -Text 'usage: bench-loader.exe <file>' -ExitCode 2)
    Check 'empty output yields null'  $null (Read-Result -Text ''         -ExitCode 0)

    Write-Output ""
    Write-Output "$pass passed, $fail failed"
    if ($fail -gt 0) { exit 1 }
    exit 0
}

# ---------------------------------------------------------------------------------
# The series.
# ---------------------------------------------------------------------------------
if ($Model -eq '')  { Write-Output "SETUP ERROR: -Model is required (a .gguf shard carrying expert tensors)"; exit 2 }
if ($OutDir -eq '') { Write-Output "SETUP ERROR: -OutDir is required"; exit 2 }
if (-not (Test-Path $Exe))   { Write-Output "MISSING: $Exe  - run build-bench.ps1 first"; exit 2 }
if (-not (Test-Path $Model)) { Write-Output "MISSING: $Model"; exit 2 }
if ($Reps -lt 1)      { Write-Output "SETUP ERROR: -Reps must be at least 1"; exit 2 }
if ($Seconds -le 0.0) { Write-Output "SETUP ERROR: -Seconds must be positive"; exit 2 }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
$Log = Join-Path $OutDir 'h2d-series-raw.txt'
Remove-Item $Log -ErrorAction SilentlyContinue

$rows = @()
foreach ($b in $Blocks) {
    for ($r = 1; $r -le $Reps; $r++) {
        Write-Output "block $b  rep $r  ssd..."
        $s = Invoke-Arm -Label "ssd b=$b r=$r" -Log $Log -ArgList @(
            $Model, '--threads', '8', '--seconds', "$Seconds", '--block-bytes', "$b")
        Write-Output "block $b  rep $r  h2d..."
        $h = Invoke-Arm -Label "h2d b=$b r=$r" -Log $Log -ArgList @(
            '--h2d', '--block-bytes', "$b", '--arena-mb', '1024', '--seconds', "$Seconds")
        $rows += [pscustomobject]@{
            block    = $b
            rep      = $r
            ssd      = if ($s) { $s['ssd'] }      else { $null }
            pageable = if ($h) { $h['pageable'] } else { $null }
            pinned   = if ($h) { $h['pinned'] }   else { $null }
        }
    }
}

Write-Output ""
Write-Output "block        rep     ssd MB/s   pageable MB/s   pinned MB/s"
foreach ($row in $rows) {
    '{0,-12} {1,3}  {2,11:N1} {3,15:N1} {4,13:N1}' -f $row.block, $row.rep, $row.ssd, $row.pageable, $row.pinned
}

# A run with a hole in it is reported as a hole. Averaging over the rows that happened to
# come back would hide which arm failed and still print a table that looks complete.
$holes = @($rows | Where-Object { $null -eq $_.ssd -or $null -eq $_.pinned -or $null -eq $_.pageable })
if ($holes.Count -gt 0) {
    Write-Output ""
    Write-Output "INVALID: $($holes.Count) of $($rows.Count) rows are incomplete. See $Log."
    Write-Output "         No medians are printed - the series has to be repeated."
    exit 1
}

Write-Output ""
Write-Output "=== medians and spread per block ==="
foreach ($b in $Blocks) {
    $g = @($rows | Where-Object { $_.block -eq $b })
    foreach ($k in @('ssd','pageable','pinned')) {
        $v = @($g | ForEach-Object { $_.$k }) | Sort-Object
        $med = $v[[int]([math]::Floor($v.Count / 2))]
        $spread = ($v[-1] - $v[0]) / $v[0] * 100
        '{0,-10} {1,-9} median {2,10:N1} MB/s   spread {3,5:N2} %' -f $b, $k, $med, $spread
    }
}

# Microseconds per work item, which is the form the decision is made in. The SSD and the
# pageable copy are the two legs of today's path; pinned is what a tier-2 hit would cost
# instead. They are ADDED, and that addition is the optimistic assumption in the whole
# calculation - see the header.
Write-Output ""
Write-Output "=== per work item ==="
Write-Output "block          today (ssd + h2d pageable)      tier-2 (h2d pinned)     factor"
foreach ($b in $Blocks) {
    $g = @($rows | Where-Object { $_.block -eq $b })
    $med = {
        param($k)
        $v = @($g | ForEach-Object { $_.$k }) | Sort-Object
        $v[[int]([math]::Floor($v.Count / 2))]
    }
    $us_ssd  = $b / ((& $med 'ssd')      * 1e6) * 1e6
    $us_page = $b / ((& $med 'pageable') * 1e6) * 1e6
    $us_pin  = $b / ((& $med 'pinned')   * 1e6) * 1e6
    $today   = $us_ssd + $us_page
    '{0,-12} {1,8:N1} + {2,6:N1} = {3,7:N1} us {4,15:N1} us {5,12:N2}x' -f `
        $b, $us_ssd, $us_page, $today, $us_pin, ($today / $us_pin)
}

$rows | Export-Csv -Path (Join-Path $OutDir 'h2d-series.csv') -NoTypeInformation
Write-Output ""
Write-Output "raw: $Log"
exit 0
