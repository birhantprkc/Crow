<#
model-paths - one table, and every measurement script reads it.

WHY THIS EXISTS: measured 2026-08-10, 26 lines across 18 files spelled out a GGUF
path, and 22 of those 26 pointed at files that had just been deleted to make room
for the 0731 download. Not one script was broken by it. They all take -Model as a
parameter, so a dead default costs nothing until somebody runs one without the
parameter and measures against a model that is not there -- or until a model
switch has to find eighteen files.

THE FAILURE THIS MUST NOT HAVE: falling back to a built-in path when the table
cannot be read. A script that quietly keeps measuring the old model produces
numbers that look exactly like the new ones. So an unknown key throws, a missing
manifest throws, and -MustExist throws with the PATH IT ACTUALLY TRIED in the
message, not the one it expected.

  . "$PSScriptRoot\model-paths.ps1"
  $model = Get-ModelPath operating-point            # absolute, no existence check
  $model = Get-ModelPath q2-k-xl -MustExist         # throws, naming the file
  $rel   = Get-ModelPath iq3-xxs -Relative          # models/UD-IQ3_XXS/...

NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 without a BOM as
ANSI, and a stray non-ASCII character breaks the parse in a misleading way.

Usage:  model-paths.ps1 -Selftest     # 0 = every case behaved as required
        model-paths.ps1 -List         # what is in the table, and what is on disk

THIS FILE HAS NO param() BLOCK, AND THAT IS LOAD-BEARING. A dot-sourced script
shares the caller's scope, so its param() declarations OVERWRITE the caller's
variables of the same name. Measured 2026-08-10: with `param([switch] $Selftest)`
here, every one of the seven tools that dot-sources this file had its own
$Selftest silently reset to $false. `measure-24-gate.ps1 -Selftest` ran 16 green
checks before the change and afterwards walked straight past its selftest branch
into the normal path, where it died on a missing -Label. Nothing said the switch
had been eaten.

$args is read instead: when this file is dot-sourced the caller's parameters are
already bound, so $args is empty and neither branch fires. When it is run
directly, the switch lands in $args.
#>

$script:ModelManifest = Join-Path $PSScriptRoot "..\manifests\operating-point.json"

function Get-ModelTable {
    <#
    The table, read fresh. Not cached: the counter-probe for this stage edits the
    manifest between runs, and a cache would make the second run answer with the
    first run's data -- a checker that cannot see a change it was written to see.
    #>
    param([string] $ManifestPath = $script:ModelManifest)

    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        throw "model table not found: $ManifestPath"
    }
    $raw = Get-Content -LiteralPath $ManifestPath -Raw
    try { $json = $raw | ConvertFrom-Json }
    catch { throw "model table is not readable JSON: $ManifestPath -- $($_.Exception.Message)" }
    if (-not $json.models -or -not $json.models.entries) {
        throw "model table has no models.entries: $ManifestPath"
    }
    return $json.models
}

function Get-ModelPath {
    <#
    .SYNOPSIS
        The path for one key from the table.
    .DESCRIPTION
        An unknown key throws and lists the keys that exist, because a typo that
        silently returns nothing turns into a server started without -m, and that
        error arrives minutes later wearing a different face.
    #>
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string] $Key,
        [switch] $Relative,
        [switch] $MustExist,
        [string] $ManifestPath = $script:ModelManifest
    )

    $models = Get-ModelTable -ManifestPath $ManifestPath
    $entry = $models.entries.PSObject.Properties | Where-Object { $_.Name -eq $Key }
    if (-not $entry) {
        $known = ($models.entries.PSObject.Properties.Name | Sort-Object) -join ', '
        throw "unknown model key '$Key'. The table has: $known"
    }

    $rel = $entry.Value.path
    if ($Relative) { return "models/$rel" }

    $root = $models._root
    $full = Join-Path $root ($rel -replace '/', '\')

    if ($MustExist -and -not (Test-Path -LiteralPath $full)) {
        # The path it TRIED, not the one it wanted. A message naming the expected
        # file cannot tell "the table is wrong" from "the file is gone".
        throw "model '$Key' is not on disk: $full"
    }
    return $full
}

function Get-SamplingDefault {
    <#
    .SYNOPSIS
        One sampling default from the manifest.
    .DESCRIPTION
        The temperature stood in six files. Five had it because somebody copied a
        working probe, and none of the five carried the reason -- that lived in a
        comment in cli/crow.py which had not been copied along. When 0731 moves
        the value, six edits would have to agree or the probes measure something
        the client never does.

        An unknown key throws rather than returning nothing: a request built
        without the field is answered by the server with ITS default, and the run
        then looks like it measured what was asked for.
    #>
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string] $Key,
        [string] $ManifestPath = $script:ModelManifest
    )

    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        throw "operating point manifest not found: $ManifestPath"
    }
    $json = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if (-not $json.sampling) { throw "manifest has no 'sampling' block: $ManifestPath" }
    $prop = $json.sampling.PSObject.Properties | Where-Object { $_.Name -eq $Key }
    if (-not $prop) {
        $known = ($json.sampling.PSObject.Properties.Name | Where-Object { $_ -notlike '_*' } | Sort-Object) -join ', '
        throw "unknown sampling key '$Key'. The manifest has: $known"
    }
    return $prop.Value
}

# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

function Invoke-ModelPathSelftest {
    $ok = 0; $red = 0
    function C { param([string]$N, [bool]$P)
        if ($P) { Write-Host "  ok   $N"; $script:mOk++ } else { Write-Host "  FAIL $N" -ForegroundColor Red; $script:mRed++ } }
    $script:mOk = 0; $script:mRed = 0

    Write-Host "model-paths selftest"

    $t = Get-ModelTable
    C "the table reads"                          ($null -ne $t.entries)
    C "and carries the operating point"          ($null -ne ($t.entries.PSObject.Properties | Where-Object Name -eq 'operating-point'))

    $p = Get-ModelPath operating-point
    C "a known key gives an absolute path"       ($p -like '*UD-IQ3_XXS*' -and $p -match '^[A-Za-z]:')
    # The 0731 path, not the preview one. Until 2026-08-11 this line asserted the
    # preview GGUF and passed -- which is what a stale operating point looks like
    # from inside its own suite: green, and pointing at the model the release had
    # already replaced.
    C "and -Relative gives the models/ form"     ((Get-ModelPath operating-point -Relative) -eq 'models/0731-gguf/UD-IQ3_XXS/DeepSeek-V4-Flash-0731-UD-IQ3_XXS-00001-of-00004.gguf')

    # The half that must refuse. A table lookup that answers for anything is the
    # same as no table: every typo becomes a silent empty path.
    $threw = $false; $msg = ''
    try { Get-ModelPath 'no-such-model' | Out-Null } catch { $threw = $true; $msg = $_.Exception.Message }
    C "an unknown key throws"                    $threw
    C "and the message lists the known keys"     ($msg -like '*operating-point*' -and $msg -like '*mxfp4*')

    # -MustExist against a model that really is gone, and the message must carry
    # the path so "table wrong" and "file deleted" can be told apart.
    $threw = $false; $msg = ''
    try { Get-ModelPath 'mxfp4' -MustExist | Out-Null } catch { $threw = $true; $msg = $_.Exception.Message }
    C "-MustExist throws for a deleted model"    $threw
    C "and names the path it tried"              ($msg -like '*DeepSeek-V4-Flash-MXFP4.gguf*')

    # Without -MustExist the same key answers, because a script may want the path
    # in order to report that it is missing.
    $silent = $true
    try { Get-ModelPath 'mxfp4' | Out-Null } catch { $silent = $false }
    C "without -MustExist it answers anyway"     $silent

    # The operating point IS there, so -MustExist must pass. Otherwise the switch
    # above proves nothing: a check that throws for everything is not a check.
    $passed = $true
    try { Get-ModelPath operating-point -MustExist | Out-Null } catch { $passed = $false }
    C "-MustExist passes for a model on disk"    $passed

    # A missing table throws rather than returning nothing.
    $threw = $false
    try { Get-ModelPath operating-point -ManifestPath (Join-Path $env:TEMP 'crow-no-such-manifest.json') | Out-Null }
    catch { $threw = $true }
    C "a missing table throws"                   $threw

    # Sampling, same shape and the same reason. 1.0/0.95 are 0731's values --
    # the manifest's _why carries the card-vs-generation_config disagreement.
    C "the temperature comes from the manifest"  ((Get-SamplingDefault temperature) -eq 1.0)
    C "and so does top_p"                        ((Get-SamplingDefault top_p) -eq 0.95)
    C "and so does min_p"                        ((Get-SamplingDefault min_p) -eq 0.01)
    $threw = $false; $msg = ''
    try { Get-SamplingDefault 'no-such-knob' | Out-Null } catch { $threw = $true; $msg = $_.Exception.Message }
    C "an unknown sampling key throws"           $threw
    C "and names the keys that exist"            ($msg -like '*temperature*')

    Write-Host ""
    if ($script:mRed -eq 0) {
        Write-Host "RESULT: SELFTEST OK - $($script:mOk) checks" -ForegroundColor Green
        return 0
    }
    Write-Host "RESULT: $($script:mRed) of $($script:mOk + $script:mRed) FAILED" -ForegroundColor Red
    return 1
}

if ($args -contains '-Selftest') { exit (Invoke-ModelPathSelftest) }

if ($args -contains '-List') {
    $t = Get-ModelTable
    Write-Host ("{0,-16} {1,-9} {2}" -f 'key', 'on disk', 'path')
    foreach ($p in $t.entries.PSObject.Properties) {
        $full = Join-Path $t._root ($p.Value.path -replace '/', '\')
        $here = if (Test-Path -LiteralPath $full) { 'yes' } else { 'NO' }
        Write-Host ("{0,-16} {1,-9} {2}" -f $p.Name, $here, $p.Value.path)
    }
    exit 0
}
