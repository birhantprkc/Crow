<#
test-server-identity - can measure-spec-types.ps1 refuse the WRONG build?

WHY THIS EXISTS. Until E9 the server path in measure-spec-types.ps1 was hard-wired to
crow-lab/src/build-native. Measuring a different tree therefore meant rebuilding the
production tree - and that tree is the one thing that has to stay untouched, because it
carries the 23-path working state that the return path depends on. -ServerExe fixes
that, and -RequireUnder is what makes the fix worth anything: without it a typo, a stale
default or a copy-pasted call would measure the production build while the protocol said
b10269, and NOTHING in the output would say so.

A permission check is only worth what its refusals are worth, so four of the five cases
here must go RED. The single green case is the old call with no -ServerExe at all: the
default has to keep working, or this change breaks every existing caller.

WHAT IS NOT COVERED. This does not start a server, touch a GPU or run the measurement
loop. The process-path check inside Start-Server (asking for a path and running one are
two different statements) needs a live process and is exercised by the E9-B run itself,
where -StartOnly writes server-identity.json before any number is taken.

Usage:
  test-server-identity.ps1 [-Tool <path>] [-Prod <path>]

Exit 0 = every case behaved as required.  1 = at least one did not.  2 = setup error.
#>
param(
    [string]$Tool = (Join-Path $PSScriptRoot 'measure-spec-types.ps1'),
    [string]$Prod = 'C:\Users\robin\dev\crow-lab\src\build-native\bin\Release\llama-server.exe',
    [string]$Lab  = 'C:\Users\robin\dev\crow-lab'
)

$ErrorActionPreference = 'Continue'
if (-not (Test-Path $Tool)) { Write-Output "SETUP ERROR: no tool at $Tool"; exit 2 }
if (-not (Test-Path $Prod)) { Write-Output "SETUP ERROR: no reference exe at $Prod"; exit 2 }

$rc   = 0
$rows = @()

function Case {
    param([string]$Name, [string]$WantRed, [scriptblock]$Body)
    # -StopOnly is used throughout: it reaches the identity assertion and then does
    # nothing but stop a server that is not running, so no case here can start a load.
    $msg = ''
    $red = $false
    try { & $Body | Out-Null } catch { $red = $true; $msg = $_.Exception.Message }
    $shouldBeRed = ($WantRed -ne '')
    $ok = ($red -eq $shouldBeRed)
    if ($shouldBeRed -and $red -and ($msg -notlike "*$WantRed*")) {
        $ok = $false
        $msg = "red, but for the wrong reason (wanted '*$WantRed*'): $msg"
    }
    if (-not $ok) { $script:rc = 1 }
    $script:rows += [pscustomobject]@{
        Case = $Name; Want = $(if ($shouldBeRed) { 'red' } else { 'green' })
        Got = $(if ($red) { 'red' } else { 'green' }); OK = $ok; Message = $msg
    }
    Write-Output ("  {0,-5} {1,-46} {2}" -f $(if ($ok) { 'ok' } else { 'FAIL' }), $Name, $msg)
}

Write-Output "test-server-identity - four refusals and one backward-compatible pass"
Write-Output ''

Case 'exe path does not exist' 'not found' {
    & $Tool -StopOnly -ServerExe 'C:\nope\llama-server.exe'
}
Case 'exe outside -RequireUnder' 'is not under' {
    & $Tool -StopOnly -ServerExe $Prod -RequireUnder (Join-Path $Lab 'wt-e9')
}
Case 'exe under src\build-native despite -RequireUnder' 'production build' {
    & $Tool -StopOnly -ServerExe $Prod -RequireUnder (Join-Path $Lab 'src')
}
Case '-RequireUnder points at nothing' 'points at nothing' {
    & $Tool -StopOnly -ServerExe $Prod -RequireUnder 'C:\nope'
}
# The one that must stay green: the call as it existed before -ServerExe.
Case 'default path, no -ServerExe (backward compatible)' '' {
    & $Tool -StopOnly
}

Write-Output ''
$rows | Format-Table Case, Want, Got, OK -AutoSize | Out-String -Width 160 | Write-Output

# Stated as a criterion rather than left to the reader: if nothing went red, this suite
# proved that the guard runs, not that it guards.
$reds = @($rows | Where-Object { $_.Got -eq 'red' }).Count
if ($reds -eq 0) {
    Write-Output "RESULT: red - no case was refused at all; the guard cannot tell a wrong build from a right one"
    exit 1
}
if ($rc -eq 0) { Write-Output ("RESULT: PASS - {0} of {0} cases behaved as required ({1} refusals)" -f $rows.Count, $reds) }
else           { Write-Output "RESULT: FAIL - see the table above" }
exit $rc
