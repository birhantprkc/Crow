<#
.SYNOPSIS
Moves named secrets out of the user environment into Crow's user-only store.

.DESCRIPTION
Why:

- A user environment variable is inherited by every child process.
- Any tool that prints `Env:` prints the value with it.
- Measured 2026-09-10: a measurement runner logged the shell environment and
  took `CROW_TAVILY_KEY` into two logs (Crow #193).

What this script does, per name, in this order:

| step | action |
|---|---|
| 1 | read the variable from the User scope |
| 2 | refuse when the store already holds that name with a different value, unless `-Force` |
| 3 | write the value into the store as JSON, UTF-8 without BOM |
| 4 | `icacls <store> /inheritance:r /grant:r "<user>:(R,W)"`, and stop when it fails |
| 5 | read the store back and compare the value byte for byte |
| 6 | remove the variable from the User scope, and only after step 5 passed |

What it never does:

- print, log or echo a value. Names and lengths only.
- touch the Machine or Process scope.
- remove a variable whose value did not read back out of the store.
- remove a variable when `icacls` did not exit 0.

Who reads the store: `crow_core.secret(name)`, the store first and the
environment second.

.PARAMETER Names
Variables to move. Default: the three found on 2026-09-10, of which only
`CROW_TAVILY_KEY` has a reader in Crow. Duplicates are dropped.

.PARAMETER Store
Store path. Default: `CROW_SECRETS_FILE` when that variable is set, else
`%LOCALAPPDATA%\Crow\secrets.json`. An explicit `-Store` naming a different
file than `CROW_SECRETS_FILE` is refused: `crow_core.secret()` reads
`CROW_SECRETS_FILE` first and would not find what was written elsewhere.

.PARAMETER Force
Overwrite a store entry that already holds a different value.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\migrate-secrets.ps1 -Names CROW_TAVILY_KEY -WhatIf

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\migrate-secrets.ps1 -Names CROW_TAVILY_KEY

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\migrate-secrets.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string[]] $Names = @("CROW_TAVILY_KEY", "FISH_API_KEY", "GEMINI_API_KEY"),
    # CROW_SECRETS_FILE FIRST, because the reader reads it first
    # (cli/crow_core.py:176). A default that ignored it would write the value
    # into %LOCALAPPDATA% while crow_core.secret() looked at the other path,
    # and the removal below would then take the last copy of the key with it.
    [string]   $Store = $(if ([string]::IsNullOrEmpty($env:CROW_SECRETS_FILE)) {
                              Join-Path $env:LOCALAPPDATA "Crow\secrets.json"
                          } else {
                              $env:CROW_SECRETS_FILE
                          }),
    [switch]   $Force
)

$ErrorActionPreference = "Stop"

# ONE STORE PER RUN. An explicit -Store while CROW_SECRETS_FILE names a
# different file is two answers to "where does the key live": this script would
# write one file and crow_core.secret() would read the other, and the removal
# step would destroy the last copy of a value nothing can find any more. Refuse
# rather than choose one of the two.
if ($PSBoundParameters.ContainsKey("Store") -and -not [string]::IsNullOrEmpty($env:CROW_SECRETS_FILE)) {
    $asked  = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($PWD.Path, $Store))
    $reader = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($PWD.Path, $env:CROW_SECRETS_FILE))
    if ($asked -ne $reader) {
        Write-Host "REFUSED: -Store and CROW_SECRETS_FILE name two different files."
        Write-Host ("-Store            : " + $asked)
        Write-Host ("CROW_SECRETS_FILE : " + $reader)
        Write-Host "crow_core.secret() reads CROW_SECRETS_FILE first, so a value written"
        Write-Host "to -Store would not be found. Clear CROW_SECRETS_FILE or pass the same"
        Write-Host "path. Nothing was written and nothing was removed."
        exit 1
    }
}

# TWO THINGS THE CALLER CANNOT GET RIGHT FROM OUTSIDE, both measured
# 2026-09-10 against Windows PowerShell 5.1:
#
# | input | what the script received | why |
# |---|---|---|
# | `-File ... -Names A,B` | ONE name, the string "A,B" | powershell.exe -File hands its arguments on as literal strings and does not build the array a [string[]] parameter would get when the script is called from inside a session |
# | `-Names A,A` | two rows for one name | `$report | Where-Object` then answered with an ARRAY, and assigning to $row.Action threw |
#
# So the commas are split here rather than trusted to the caller, and the list
# is reduced to one entry per name. Every name is looked up once, which is also
# the honest report.
$Names = @($Names |
    ForEach-Object { $_ -split "," } |
    ForEach-Object { $_.Trim() } |
    Where-Object { -not [string]::IsNullOrEmpty($_) } |
    Select-Object -Unique)
if ($Names.Count -eq 0) {
    Write-Host "REFUSED: -Names is empty."
    exit 1
}

# WHY THE FILE IS WRITTEN THROUGH .NET AND NOT THROUGH Out-File.
# Out-File -Encoding utf8 writes a BOM on PowerShell 5.1. Python's
# open(path, encoding="utf-8") hands that BOM to json.load as the first
# character, json.load raises, and crow_core.secret() reports the store as
# malformed and falls back to an environment this script has just emptied.
$UTF8_NO_BOM = New-Object System.Text.UTF8Encoding($false)

function Read-Store {
    # A HASHTABLE AND NOT AN [ordered] ONE, and the reason is a real failure:
    # PowerShell unrolls an OrderedDictionary on the way out of a function, so
    # the caller got a string back and `$table[$name]` said "cannot apply an
    # index to an object of type System.String". A hashtable is never unrolled.
    param([string] $Path)
    $table = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $table }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($raw)) { return $table }
    $parsed = $raw | ConvertFrom-Json
    foreach ($property in $parsed.PSObject.Properties) {
        $table[$property.Name] = [string] $property.Value
    }
    return $table
}

function Write-Store {
    param([string] $Path, $Table)
    # NOT `Split-Path -LiteralPath $Path -Parent`: Windows PowerShell 5.1
    # puts those two in different parameter sets and answers
    # "AmbiguousParameterSet" rather than a directory. Measured here.
    $parent = [System.IO.Path]::GetDirectoryName($Path)
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    # Sorted, so two runs over the same names produce the same bytes and a
    # diff of the store is readable by whoever has to look at one.
    $ordered = [ordered]@{}
    foreach ($key in ($Table.Keys | Sort-Object)) { $ordered[$key] = $Table[$key] }
    $json = ConvertTo-Json -InputObject $ordered -Depth 3
    [System.IO.File]::WriteAllText($Path, $json, $UTF8_NO_BOM)
}

Write-Host ("store : " + $Store)
Write-Host ("names : " + ($Names -join ", "))
Write-Host ""

# $store and $Store would be ONE variable here: PowerShell names are case
# insensitive, so the local reading of the file would overwrite the path the
# parameter carries. Hence $current.
$current = Read-Store -Path $Store
$report = @()
$pending = @()

foreach ($name in $Names) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ([string]::IsNullOrEmpty($value)) {
        $report += [pscustomobject]@{ Name = $name; Chars = 0; Action = "not set in the User scope" }
        continue
    }
    $known = $null
    if ($current.ContainsKey($name)) { $known = [string] $current[$name] }
    if ($null -ne $known -and $known.Length -gt 0 -and -not ($known -ceq $value) -and -not $Force) {
        $report += [pscustomobject]@{ Name = $name; Chars = $value.Length; Action = "REFUSED: store holds a different value, pass -Force" }
        continue
    }
    $current[$name] = $value
    $pending += $name
    $report += [pscustomobject]@{ Name = $name; Chars = $value.Length; Action = "queued for the store" }
}

if ($pending.Count -eq 0) {
    $report | Format-Table -AutoSize
    Write-Host "nothing to move."
    return
}

if ($PSCmdlet.ShouldProcess($Store, "write " + $pending.Count + " secret(s)")) {
    Write-Store -Path $Store -Table $current
    Write-Host ("wrote : " + $Store)
} else {
    $report | Format-Table -AutoSize
    Write-Host "-WhatIf: nothing was written and nothing was removed."
    return
}

# THE ACL IS A GATE ON THE REMOVAL AND NOT A DECORATION, and its result has to
# be READ: icacls is a native program, so $ErrorActionPreference = "Stop" never
# sees a failure of it, only $LASTEXITCODE does. Without this check a failed
# icacls was followed by the removal of the only other copy of the value, which
# leaves the key in a file every account on the machine can read.
$aclApplied = $false
if ($PSCmdlet.ShouldProcess($Store, "restrict the ACL to " + $env:USERNAME)) {
    # /inheritance:r drops what the parent handed down, /grant:r replaces any
    # entry this user already had. Both are needed: a grant on top of inherited
    # Users:(RX) leaves the file readable by every account on the machine.
    $acl = & icacls $Store /inheritance:r /grant:r ("{0}:(R,W)" -f $env:USERNAME)
    $code = $LASTEXITCODE
    $acl | ForEach-Object { Write-Host ("icacls: " + $_) }
    Write-Host ("icacls: exit code " + $code)
    Write-Host ""
    if ($code -eq 0) {
        $aclApplied = $true
        $shown = & icacls $Store
        $shown | ForEach-Object { Write-Host ("acl   : " + $_) }
        Write-Host ""
    }
}

if (-not $aclApplied) {
    foreach ($name in $pending) {
        $row = $report | Where-Object { $_.Name -eq $name } | Select-Object -First 1
        $row.Action = "KEPT in the User scope: the ACL was not applied"
    }
    $report | Format-Table -AutoSize
    Write-Host "The ACL was NOT applied, so the store may be readable by other accounts."
    Write-Host "Nothing was removed from the User scope, so no value was lost."
    Write-Host ("Check the permissions of this file, or delete it, then run again:")
    Write-Host ("  " + $Store)
    exit 1
}

# THE READ-BACK IS THE GATE ON THE REMOVAL, and it reads the file from disk
# rather than trusting the hashtable that was just written. A value the store
# does not actually hold, byte for byte, is a value the removal below would
# destroy: there is no second copy anywhere.
$readback = Read-Store -Path $Store

foreach ($name in $pending) {
    $row = $report | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ([string]::IsNullOrEmpty($value)) {
        $row.Action = "in the store; the User scope was already empty"
        continue
    }
    $stored = $null
    if ($readback.ContainsKey($name)) { $stored = [string] $readback[$name] }
    if ($null -eq $stored -or -not ($stored -ceq $value)) {
        $row.Action = "KEPT: the store did not read back equal"
        continue
    }
    if ($PSCmdlet.ShouldProcess($name, "remove from the User environment")) {
        [Environment]::SetEnvironmentVariable($name, $null, "User")
        $row.Action = "moved to the store, removed from the User scope"
    } else {
        $row.Action = "in the store; the User scope was left alone"
    }
}

$report | Format-Table -AutoSize

Write-Host "This window still holds the old values in its own environment."
Write-Host "Open a new shell before checking; crow reads the store from now on."
