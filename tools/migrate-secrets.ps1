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
| 4 | `icacls <store> /inheritance:r /grant:r "<user>:(R,W)"` |
| 5 | read the store back and compare the value byte for byte |
| 6 | remove the variable from the User scope, and only after step 5 passed |

What it never does:

- print, log or echo a value. Names and lengths only.
- touch the Machine or Process scope.
- remove a variable whose value did not read back out of the store.

Who reads the store: `crow_core.secret(name)`, the store first and the
environment second.

.PARAMETER Names
Variables to move. Default: the three found on 2026-09-10.

.PARAMETER Store
Store path. Default: `%LOCALAPPDATA%\Crow\secrets.json`.

.PARAMETER Force
Overwrite a store entry that already holds a different value.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\migrate-secrets.ps1 -WhatIf

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\migrate-secrets.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string[]] $Names = @("CROW_TAVILY_KEY", "FISH_API_KEY", "GEMINI_API_KEY"),
    [string]   $Store = (Join-Path $env:LOCALAPPDATA "Crow\secrets.json"),
    [switch]   $Force
)

$ErrorActionPreference = "Stop"

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

if ($PSCmdlet.ShouldProcess($Store, "restrict the ACL to " + $env:USERNAME)) {
    # /inheritance:r drops what the parent handed down, /grant:r replaces any
    # entry this user already had. Both are needed: a grant on top of inherited
    # Users:(RX) leaves the file readable by every account on the machine.
    $acl = & icacls $Store /inheritance:r /grant:r ("{0}:(R,W)" -f $env:USERNAME)
    $acl | ForEach-Object { Write-Host ("icacls: " + $_) }
    Write-Host ""
    $shown = & icacls $Store
    $shown | ForEach-Object { Write-Host ("acl   : " + $_) }
    Write-Host ""
}

# THE READ-BACK IS THE GATE ON THE REMOVAL, and it reads the file from disk
# rather than trusting the hashtable that was just written. A value the store
# does not actually hold, byte for byte, is a value the removal below would
# destroy: there is no second copy anywhere.
$readback = Read-Store -Path $Store

foreach ($name in $pending) {
    $row = $report | Where-Object { $_.Name -eq $name }
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
