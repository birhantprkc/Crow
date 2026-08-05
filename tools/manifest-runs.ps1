<#
manifest-runs - a versioned fingerprint of the raw run protocols, which are NOT
versioned themselves.

WHY THIS EXISTS. runs/ is in .gitignore (line 10). Every raw protocol quoted by E9 and
E10 therefore lives on one machine and in no commit. That is a deliberate choice - a
single CUDA build log is ~224 MB - but it has a consequence nobody can see from the
repository: if a file is later deleted, truncated or overwritten, nothing says so. The
numbers in the vault would keep pointing at a path whose content has silently changed.

    A PROTOCOL THAT NOTHING VERIFIES IS INDISTINGUISHABLE FROM A MISSING ONE.

This tool does not archive and does not copy. It writes the one thing that is small
enough to version: path, size, SHA-256 and stage for every file, plus an explicit note
that the files are not in git. That turns "the protocol is gone" from an unnoticed loss
into a red check.

WHAT IT DOES NOT DO, on purpose:
  - it does not copy or compress anything. Deciding to archive 450 MB of build logs is a
    separate decision with a separate cost, and taking it silently here would be the
    kind of helpfulness nobody asked for.
  - it does not touch .gitignore. runs/ stays ignored.
  - it makes no claim that the protocols are "secured". The manifest proves that a file
    is UNCHANGED since the manifest was written, and nothing more. If the disk dies, the
    manifest dies with the repository's ability to detect it - it detects drift, not loss
    of the machine.

THE VERIFY MODE IS THE POINT. A manifest that is only ever written is a list. -Verify
holds it against the disk and reports three kinds of difference separately, because they
mean different things: MISSING (a file that was recorded and is gone), CHANGED (recorded
and different), UNTRACKED (present but not in the manifest - usually a newer run, which
is not an error but must not pass unnoticed either).

TRAPS THIS TOOL IS BUILT AROUND:
  - Measure-Object -Line counts 0 for an empty line; counts here come from .Count.
  - a pipe swallows the return value; nothing here is piped into a command whose exit
    code matters.
  - Get-Content in PS 5.1 decodes with the ANSI codepage, so JSON is read and written
    through [IO.File] with an explicit UTF8Encoding($false) - a BOM would make the file
    unreadable to other tools that expect plain UTF-8.

Usage:
  manifest-runs.ps1                          # write manifests/runs-<date>.json
  manifest-runs.ps1 -Verify                  # hold the newest manifest against disk
  manifest-runs.ps1 -Verify -Manifest <file>
  manifest-runs.ps1 -Pattern '^e1[01]'       # only E10/E11 directories

Exit 0 = written, or verified with no difference.  1 = differences found.  2 = setup error.
#>
param(
    [string]$CROW     = 'C:\Users\robin\dev\Crow',
    [string]$RunsDir  = '',
    [string]$OutDir   = '',
    [string]$Manifest = '',
    # Which run directories to fingerprint. The default used to be '^e9|^e10', which
    # SILENTLY dropped e11 and e12 - the E11 and ABBA protocols were recorded as manifested
    # while the default run never touched them. A filter that omits without saying so is the
    # same failure as a counter that returns 0: it looks like coverage. Default now spans
    # every stage that has produced runs.
    [string]$Pattern  = '^e9|^e1[012]',
    [string]$Day      = '2026-08-05',
    [switch]$Verify
)

$ErrorActionPreference = 'Continue'

if (-not $RunsDir) { $RunsDir = Join-Path $CROW 'runs' }
if (-not $OutDir)  { $OutDir  = Join-Path $CROW 'manifests' }

function Say([string]$m) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m) { Write-Output "SETUP ERROR: $m"; exit 2 }
function Utf8NoBom { return (New-Object System.Text.UTF8Encoding($false)) }

if (-not (Test-Path $RunsDir)) { Die "runs directory not found: $RunsDir" }
$dayDir = Join-Path $RunsDir $Day
if (-not (Test-Path $dayDir)) { Die "no run directory for $Day under $RunsDir" }

# --- collect: relative path, size, sha256, stage -----------------------------
function Collect-Entries {
    $out = @()
    foreach ($d in (Get-ChildItem $dayDir -Directory | Where-Object { $_.Name -match $Pattern } | Sort-Object Name)) {
        # The stage comes from the directory name, which is how the runs were laid out.
        # An unmatched name is labelled 'unknown' rather than guessed into a stage.
        $stage = switch -Regex ($d.Name) {
            '^e9'  { 'E9';      break }
            '^e10' { 'E10';     break }
            '^e11' { 'E11';     break }
            '^e12' { 'E12';     break }
            default { 'unknown' }
        }
        foreach ($f in (Get-ChildItem $d.FullName -Recurse -File | Sort-Object FullName)) {
            $rel = $f.FullName.Substring($RunsDir.Length).TrimStart('\', '/') -replace '\\', '/'
            $out += [pscustomobject]@{
                path   = $rel
                stage  = $stage
                bytes  = $f.Length
                sha256 = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
            }
        }
    }
    return $out
}

# ============================ VERIFY ========================================
if ($Verify) {
    if (-not $Manifest) {
        if (-not (Test-Path $OutDir)) { Die "no manifest directory at $OutDir - write one first" }
        $newest = Get-ChildItem $OutDir -File -Filter 'runs-*.json' | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $newest) { Die "no manifest found in $OutDir - write one first" }
        $Manifest = $newest.FullName
    }
    if (-not (Test-Path $Manifest)) { Die "manifest not found: $Manifest" }

    $doc = [IO.File]::ReadAllText($Manifest, (Utf8NoBom)) | ConvertFrom-Json
    Say ("verifying {0}  ({1} entries, written {2})" -f (Split-Path $Manifest -Leaf), $doc.entries.Count, $doc.written)

    $missing = @(); $changed = @()
    foreach ($e in $doc.entries) {
        $full = Join-Path $RunsDir ($e.path -replace '/', '\')
        if (-not [IO.File]::Exists($full)) { $missing += $e.path; continue }
        $fi = Get-Item -LiteralPath $full
        $sha = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
        if ($fi.Length -ne $e.bytes -or $sha -ne $e.sha256) {
            $changed += ("{0}  recorded {1} B / {2}  now {3} B / {4}" -f $e.path, $e.bytes, $e.sha256.Substring(0,12), $fi.Length, $sha.Substring(0,12))
        }
    }
    # present on disk but not recorded: not an error, but never silent
    $recorded = @{}
    foreach ($e in $doc.entries) { $recorded[$e.path] = $true }
    $untracked = @()
    foreach ($e in (Collect-Entries)) { if (-not $recorded.ContainsKey($e.path)) { $untracked += $e.path } }

    Say ("MISSING   {0}" -f $missing.Count);   foreach ($m in $missing)   { Say "  - $m" }
    Say ("CHANGED   {0}" -f $changed.Count);   foreach ($c in $changed)   { Say "  ~ $c" }
    Say ("UNTRACKED {0}   (present but not recorded - usually a newer run)" -f $untracked.Count)
    foreach ($u in ($untracked | Select-Object -First 20)) { Say "  + $u" }
    if ($untracked.Count -gt 20) { Say ("  + … and {0} more" -f ($untracked.Count - 20)) }

    Write-Output ''
    if ($missing.Count -eq 0 -and $changed.Count -eq 0) {
        Write-Output ("RESULT: PASS - {0} of {0} recorded files present and unchanged. {1} untracked." -f $doc.entries.Count, $untracked.Count)
        Write-Output "  This proves the files are UNCHANGED since the manifest was written. It does not"
        Write-Output "  make them versioned, backed up or recoverable - runs/ is still in .gitignore."
        exit 0
    } else {
        Write-Output ("RESULT: FAIL - {0} missing, {1} changed, of {2} recorded." -f $missing.Count, $changed.Count, $doc.entries.Count)
        exit 1
    }
}

# ============================ WRITE =========================================
$entries = Collect-Entries
if ($entries.Count -eq 0) { Die "no files matched pattern '$Pattern' under $dayDir" }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$out = Join-Path $OutDir ("runs-{0}.json" -f $Day)

$byStage = @{}
foreach ($e in $entries) { if (-not $byStage.ContainsKey($e.stage)) { $byStage[$e.stage] = 0 }; $byStage[$e.stage]++ }

$doc = [pscustomobject]@{
    note    = 'Fingerprint only. These files are NOT in git - runs/ is ignored (.gitignore:10). This manifest detects deletion and modification; it is not a backup and does not make the protocols recoverable.'
    day     = $Day
    pattern = $Pattern
    root    = 'Crow/runs'
    written = (Get-Date).ToString('o')
    files   = $entries.Count
    bytes   = ($entries | Measure-Object bytes -Sum).Sum
    stages  = $byStage
    entries = $entries
}
[IO.File]::WriteAllText($out, ($doc | ConvertTo-Json -Depth 6), (Utf8NoBom))

Say ("wrote {0}" -f $out)
Say ("{0} files, {1:N0} B" -f $entries.Count, $doc.bytes)
foreach ($k in ($byStage.Keys | Sort-Object)) { Say ("  {0,-8} {1,4} files" -f $k, $byStage[$k]) }
Write-Output ''
Write-Output ("RESULT: PASS - manifest written for {0} files. Verify later with -Verify." -f $entries.Count)
Write-Output "  The protocols themselves remain outside git. This records what they were, not a copy."
exit 0
